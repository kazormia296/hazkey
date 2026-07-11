#!/usr/bin/env python3
"""Packaging contracts for the standalone Grimodex Fcitx5 product.

The suite is intentionally stdlib-only so Debian and Arch packaging jobs can
run it before installing any project dependencies.  Set GRIMODEX_STAGED_ROOT
to validate a DESTDIR/package root and GRIMODEX_PRODUCT_ARTIFACTS to an
os.pathsep-separated list of release artifacts for binary-content checks.
"""

from __future__ import annotations

import fnmatch
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEBIAN_CONTROL = REPOSITORY_ROOT / "debian/control"
AUR_DIRECTORY = REPOSITORY_ROOT / "packaging/aur/fcitx5-grimodex-bin"
INSTALL_MANIFEST = (
    REPOSITORY_ROOT / "packaging/manifests/fcitx5-grimodex.install-paths"
)
UNINSTALL_MANIFEST = (
    REPOSITORY_ROOT / "packaging/manifests/fcitx5-grimodex.uninstall-paths"
)
HAZKEY_REFERENCE_MANIFEST = (
    REPOSITORY_ROOT / "packaging/manifests/fcitx5-hazkey.reference-paths"
)
BUILD_WORKFLOW = REPOSITORY_ROOT / ".github/workflows/build.yml"
PACKAGE_WORKFLOW = REPOSITORY_ROOT / ".github/workflows/grimodex-package-ci.yml"
INTEGRATION_WORKFLOW = REPOSITORY_ROOT / ".github/workflows/grimodex-spike-ci.yml"
LICENSE_COLLECTOR = (
    REPOSITORY_ROOT / "packaging/scripts/collect_third_party_licenses.py"
)
SWIFT_HUB_AUDITOR = (
    REPOSITORY_ROOT / "packaging/scripts/audit_swift_hub_offline.py"
)
SWIFT_TOKENIZERS_REVISION = "4a606f66e0cc4d7d9f0197649e812f7fc86a4c34"

FORBIDDEN_ARTIFACT_MARKERS = (
    b"qt6network",
    b"qnetworkaccessmanager",
    b"qnetworkrequest",
    b"qnetworkreply",
    b"foundationnetworking",
    b"urlsession",
    b"nsurlconnection",
    b"asynchttpclient",
    b"niohttpclient",
    b"curl_easy_",
    b"libcurl.so",
)

REQUIRED_PACKAGED_PATHS = (
    "/usr/bin/fcitx5-grimodex-server",
    "/usr/bin/fcitx5-grimodex-settings",
    "/usr/lib/{,*/}fcitx5/fcitx5-grimodex.so",
    "/usr/lib/{,*/}fcitx5-grimodex/fcitx5-grimodex-server",
    "/usr/lib/{,*/}fcitx5-grimodex/fcitx5-grimodex-settings",
    "/usr/share/applications/fcitx5-grimodex-settings.desktop",
    "/usr/share/fcitx5/addon/grimodex.conf",
    "/usr/share/fcitx5/inputmethod/grimodex.conf",
    "/usr/share/icons/hicolor/scalable/apps/fcitx5-grimodex.svg",
    "/usr/share/licenses/fcitx5-grimodex/LICENSE",
    "/usr/share/licenses/fcitx5-grimodex/NOTICE.md",
    "/usr/share/licenses/fcitx5-grimodex/third-party/azookey-dictionary/**",
    "/usr/share/licenses/fcitx5-grimodex/third-party/azookey-emoji/**",
    "/usr/share/licenses/fcitx5-grimodex/third-party/llama.cpp/**",
    "/usr/share/licenses/fcitx5-grimodex/third-party/protobuf/**",
    "/usr/share/licenses/fcitx5-grimodex/third-party/swift-packages/**",
    "/usr/share/licenses/fcitx5-grimodex/third-party/swift-runtime/**",
    "/usr/share/metainfo/com.miyakey.grimodex.ime.fcitx5.metainfo.xml",
)


def read_non_comment_lines(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def parse_debian_paragraphs(path: Path) -> list[dict[str, str]]:
    paragraphs: list[dict[str, str]] = []
    current: dict[str, str] = {}
    current_key: str | None = None
    for raw_line in path.read_text(encoding="utf-8").splitlines() + [""]:
        if not raw_line.strip():
            if current:
                paragraphs.append(current)
                current = {}
                current_key = None
            continue
        if raw_line[0].isspace():
            if current_key is None:
                raise AssertionError(f"orphan continuation in {path}: {raw_line}")
            current[current_key] += " " + raw_line.strip()
            continue
        key, separator, value = raw_line.partition(":")
        if not separator:
            raise AssertionError(f"invalid Debian control line: {raw_line}")
        current_key = key.lower()
        current[current_key] = value.strip()
    return paragraphs


def parse_path_manifest(path: Path) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for line in read_non_comment_lines(path):
        kind, separator, pattern = line.partition(":")
        if separator != ":" or kind not in {"required", "optional"}:
            raise AssertionError(
                f"{path}: expected required:/path or optional:/path, got {line!r}"
            )
        if not pattern.startswith("/"):
            raise AssertionError(f"{path}: package path must be absolute: {pattern}")
        entries.append((kind, pattern))
    return entries


def expand_manifest_pattern(pattern: str) -> list[str]:
    marker = "{,*/}"
    if marker not in pattern:
        return [pattern]
    if pattern.count(marker) != 1:
        raise AssertionError(f"manifest pattern has multiple optional libdir markers: {pattern}")
    return [pattern.replace(marker, ""), pattern.replace(marker, "*/")]


def manifest_path_matches(path: str, pattern: str) -> bool:
    """Match package paths without allowing * to cross path separators."""
    path_parts = path.removeprefix("/").split("/")
    pattern_parts = pattern.removeprefix("/").split("/")

    def match(path_index: int, pattern_index: int) -> bool:
        if pattern_index == len(pattern_parts):
            return path_index == len(path_parts)

        pattern_part = pattern_parts[pattern_index]
        if pattern_part == "**":
            return match(path_index, pattern_index + 1) or (
                path_index < len(path_parts)
                and match(path_index + 1, pattern_index)
            )
        if "**" in pattern_part:
            raise AssertionError(
                f"recursive wildcard must occupy a complete path segment: {pattern}"
            )
        return (
            path_index < len(path_parts)
            and fnmatch.fnmatchcase(path_parts[path_index], pattern_part)
            and match(path_index + 1, pattern_index + 1)
        )

    return match(0, 0)


def staged_public_paths(root: Path) -> list[str]:
    return sorted(
        "/" + path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() or path.is_symlink()
    )


def validate_artifact_bytes(path: Path) -> None:
    content = path.read_bytes().lower()
    present = [marker.decode("ascii") for marker in FORBIDDEN_ARTIFACT_MARKERS if marker in content]
    if present:
        raise AssertionError(
            f"{path} contains forbidden product-network marker(s): {', '.join(present)}"
        )


def validate_staged_root(root: Path, entries: list[tuple[str, str]]) -> None:
    paths = staged_public_paths(root)
    if not paths:
        raise AssertionError(f"staged root is empty: {root}")

    patterns = [
        expanded
        for _, pattern in entries
        for expanded in expand_manifest_pattern(pattern)
    ]
    for path in paths:
        if "hazkey" in path.lower():
            raise AssertionError(f"Hazkey public path leaked into Grimodex package: {path}")
        if not any(manifest_path_matches(path, pattern) for pattern in patterns):
            raise AssertionError(f"unowned staged package path: {path}")

    for kind, pattern in entries:
        expanded_patterns = expand_manifest_pattern(pattern)
        if kind == "required" and not any(
            manifest_path_matches(path, expanded)
            for path in paths
            for expanded in expanded_patterns
        ):
            raise AssertionError(f"required packaged path is missing: {pattern}")

    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            validate_artifact_bytes(path)


def validate_staged_install_and_uninstall(
    root: Path,
    install_entries: list[tuple[str, str]],
    uninstall_entries: list[tuple[str, str]],
) -> None:
    """Validate both ownership manifests against a real staged install tree."""
    validate_staged_root(root, install_entries)
    paths = staged_public_paths(root)

    uninstall_patterns = [
        expanded
        for _, pattern in uninstall_entries
        for expanded in expand_manifest_pattern(pattern)
    ]
    for path in paths:
        if not any(
            manifest_path_matches(path, pattern) for pattern in uninstall_patterns
        ):
            raise AssertionError(f"uninstall manifest does not own staged path: {path}")

    for kind, pattern in uninstall_entries:
        expanded_patterns = expand_manifest_pattern(pattern)
        if kind == "required" and not any(
            manifest_path_matches(path, expanded)
            for path in paths
            for expanded in expanded_patterns
        ):
            raise AssertionError(f"required uninstall path is missing: {pattern}")

    with tempfile.TemporaryDirectory() as temporary_directory:
        simulated_root = Path(temporary_directory) / "root"
        shutil.copytree(root, simulated_root, symlinks=True)
        for path in paths:
            (simulated_root / path.removeprefix("/")).unlink()

        remaining_paths = staged_public_paths(simulated_root)
        if remaining_paths:
            raise AssertionError(
                "manifest uninstall left staged path(s): "
                + ", ".join(remaining_paths)
            )


class LinuxClientLifecycleContractTests(unittest.TestCase):
    def test_session_properties_are_destroyed_before_server_connector(self) -> None:
        header = (REPOSITORY_ROOT / "fcitx5-hazkey/src/hazkey_engine.h").read_text(
            encoding="utf-8"
        )
        connector = (
            REPOSITORY_ROOT / "fcitx5-hazkey/src/hazkey_server_connector.cpp"
        ).read_text(encoding="utf-8")

        self.assertLess(
            header.index("HazkeyServerConnector server_;"),
            header.index("FactoryFor<HazkeyState> factory_;"),
        )
        self.assertIn("sessionClient_.close(session_, false)", connector)


class PackageMetadataContractTests(unittest.TestCase):
    def test_release_gates_watch_every_staged_install_input(self) -> None:
        release_workflow = BUILD_WORKFLOW.read_text(encoding="utf-8")
        package_workflow = PACKAGE_WORKFLOW.read_text(encoding="utf-8")
        integration_workflow = INTEGRATION_WORKFLOW.read_text(encoding="utf-8")

        for path_filter in (
            "CMakeLists.txt",
            "fcitx5-hazkey/**",
            "hazkey-server/**",
            "hazkey-settings/**",
            "linux-shared/**",
            "protocol/**",
            "LICENSE",
            "NOTICE.md",
        ):
            with self.subTest(path_filter=path_filter):
                self.assertIn(f'- "{path_filter}"', package_workflow)

        self.assertIn("pull_request:", integration_workflow)
        self.assertIn("pull_request:", release_workflow)

    def test_integration_ci_validates_reused_real_cmake_installs(self) -> None:
        workflow = INTEGRATION_WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("cmake --install fcitx5-hazkey/build-ci", workflow)
        self.assertIn("cmake --install hazkey-settings/build-ci", workflow)
        self.assertIn("cmake --install hazkey-server/build-ci", workflow)
        self.assertIn("grimodex-staged-client-${{ github.sha }}", workflow)
        self.assertIn("grimodex-staged-server-${{ github.sha }}", workflow)
        self.assertIn("real-staged-package-contract:", workflow)
        self.assertIn("needs: [linux-client-tests, swift-tests]", workflow)
        self.assertIn(
            "GRIMODEX_STAGED_ROOT: ${{ runner.temp }}/grimodex-staged-root",
            workflow,
        )
        self.assertIn("python3 packaging/tests/package_contract_test.py", workflow)
        self.assertEqual(workflow.count("--target build_hazkey_server"), 1)

    def test_integration_ci_records_release_benchmark_telemetry(self) -> None:
        workflow = INTEGRATION_WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("--configuration release", workflow)
        self.assertIn("GRIMODEX_BENCHMARK entries=", workflow)
        self.assertIn('test "$benchmark_count" -eq 5', workflow)
        self.assertIn("$GITHUB_STEP_SUMMARY", workflow)

    def test_debian_source_and_binary_identity(self) -> None:
        paragraphs = parse_debian_paragraphs(DEBIAN_CONTROL)
        source = next(paragraph for paragraph in paragraphs if "source" in paragraph)
        binary = next(paragraph for paragraph in paragraphs if "package" in paragraph)
        self.assertEqual(source["source"], "fcitx5-grimodex")
        self.assertEqual(binary["package"], "fcitx5-grimodex")

        for field in ("conflicts", "replaces", "provides"):
            self.assertNotIn("fcitx5-hazkey", binary.get(field, "").lower())

    def test_debian_source_metadata_is_complete_and_honest(self) -> None:
        required = (
            "debian/changelog",
            "debian/control",
            "debian/copyright",
            "debian/fcitx5-grimodex.install",
            "debian/rules",
            "debian/source/format",
            "debian/README.source",
        )
        for relative_path in required:
            with self.subTest(path=relative_path):
                self.assertTrue((REPOSITORY_ROOT / relative_path).is_file())

        readme = (REPOSITORY_ROOT / "debian/README.source").read_text(
            encoding="utf-8"
        ).lower()
        self.assertIn("not offline-buildable", readme)
        self.assertIn("swiftpm", readme)
        self.assertIn("submodule", readme)

    def test_aur_binary_metadata_is_consistent(self) -> None:
        pkgbuild = (AUR_DIRECTORY / "PKGBUILD").read_text(encoding="utf-8")
        srcinfo = (AUR_DIRECTORY / ".SRCINFO").read_text(encoding="utf-8")
        self.assertRegex(pkgbuild, r"(?m)^pkgname=fcitx5-grimodex-bin$")
        self.assertRegex(srcinfo, r"(?m)^pkgname = fcitx5-grimodex-bin$")
        self.assertIn("license=('MIT')", pkgbuild)
        self.assertIn("license = MIT", srcinfo)
        self.assertIn("fcitx5-grimodex", pkgbuild)
        self.assertIn("URLSession", pkgbuild)
        self.assertIn("FoundationNetworking", pkgbuild)
        self.assertNotRegex(pkgbuild, r"(?i)huggingface|hf\.co")

        relationship_pattern = re.compile(
            r"(?im)^(?:conflicts|replaces|provides)(?:\s*=|=).*fcitx5-hazkey"
        )
        self.assertIsNone(relationship_pattern.search(pkgbuild))
        self.assertIsNone(relationship_pattern.search(srcinfo))

    def test_installed_and_uninstalled_paths_are_identical_and_isolated(self) -> None:
        installed = parse_path_manifest(INSTALL_MANIFEST)
        uninstalled = parse_path_manifest(UNINSTALL_MANIFEST)
        self.assertEqual(installed, uninstalled)
        self.assertEqual(len(installed), len(set(installed)))

        patterns = {pattern for _, pattern in installed}
        for required_pattern in REQUIRED_PACKAGED_PATHS:
            self.assertIn(required_pattern, patterns)
        for pattern in patterns:
            self.assertNotIn("hazkey", pattern.lower())

        hazkey_paths = set(read_non_comment_lines(HAZKEY_REFERENCE_MANIFEST))
        self.assertTrue(hazkey_paths)
        self.assertTrue(all("hazkey" in path.lower() for path in hazkey_paths))
        self.assertTrue(patterns.isdisjoint(hazkey_paths))

    def test_packaging_files_preserve_license_and_notice(self) -> None:
        debian_install = (REPOSITORY_ROOT / "debian/fcitx5-grimodex.install").read_text(
            encoding="utf-8"
        )
        pkgbuild = (AUR_DIRECTORY / "PKGBUILD").read_text(encoding="utf-8")
        self.assertIn("usr/share/licenses/fcitx5-grimodex", debian_install)
        self.assertIn("LICENSE", pkgbuild)
        self.assertIn("NOTICE.md", pkgbuild)

    def test_release_workflow_uses_grimodex_artifact_identity(self) -> None:
        workflow = BUILD_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn(
            'archive="fcitx5-grimodex-${GRIMODEX_IME_VERSION}-${{ matrix.arch }}.tar.zst"',
            workflow,
        )
        self.assertIn(
            "name: fcitx5-grimodex-release-${{ matrix.arch }}",
            workflow,
        )
        self.assertIn('sha256sum "$archive"', workflow)
        self.assertIn("gh release upload", workflow)
        self.assertNotIn("packages/fcitx5-hazkey-", workflow)
        self.assertNotIn("name: fcitx5-hazkey-", workflow)

    def test_release_workflow_collects_licenses_and_validates_canonical_tree(self) -> None:
        workflow = BUILD_WORKFLOW.read_text(encoding="utf-8")

        collector = "python3 packaging/scripts/collect_third_party_licenses.py"
        validator = "python3 packaging/tests/package_contract_test.py"
        archive = 'tar --zstd -cf "${{ github.workspace }}/packages/$archive" ./usr'
        self.assertIn(collector, workflow)
        self.assertIn("hazkey-server/build/swift-build/checkouts", workflow)
        self.assertIn("swift-6.2-RELEASE/LICENSE.txt", workflow)
        self.assertIn("protobuf/v21.12/LICENSE", workflow)
        self.assertLess(workflow.index(collector), workflow.index(validator))
        self.assertLess(workflow.index(validator), workflow.index(archive))

    def test_real_swift_builds_audit_the_exact_pinned_hub_sources(self) -> None:
        auditor = "python3 packaging/scripts/audit_swift_hub_offline.py"
        for workflow_path, checkout in (
            (
                BUILD_WORKFLOW,
                "hazkey-server/build/swift-build/checkouts/swift-tokenizers",
            ),
            (
                INTEGRATION_WORKFLOW,
                "hazkey-server/build-ci/swift-build/checkouts/swift-tokenizers",
            ),
        ):
            with self.subTest(workflow=workflow_path.name):
                workflow = workflow_path.read_text(encoding="utf-8")
                self.assertIn(auditor, workflow)
                self.assertIn(checkout, workflow)
                self.assertIn(SWIFT_TOKENIZERS_REVISION, workflow)

    def test_license_collector_copies_every_resolved_and_bundled_license(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = root / "repository"
            checkouts = root / "checkouts"
            destination = root / "stage"
            (repository / "hazkey-server/azooKey_dictionary_storage").mkdir(parents=True)
            (repository / "hazkey-server/azooKey_dictionary_storage/LICENSE").write_text("dictionary")
            (repository / "hazkey-server/azooKey_emoji_dictionary_storage/data").mkdir(parents=True)
            (repository / "hazkey-server/azooKey_emoji_dictionary_storage/data/README.md").write_text("emoji notice")
            (repository / "hazkey-server/llama.cpp").mkdir(parents=True)
            (repository / "hazkey-server/llama.cpp/LICENSE").write_text("llama")
            (repository / "hazkey-server/Package.resolved").write_text(
                json.dumps({"pins": [{"identity": "converter"}, {"identity": "swift-util"}]}),
                encoding="utf-8",
            )
            for identity in ("converter", "swift-util"):
                (checkouts / identity).mkdir(parents=True)
                (checkouts / identity / "LICENSE").write_text(identity)
            inputs = {}
            for name in ("swift", "protobuf", "mozc", "unicode"):
                inputs[name] = root / f"{name}-LICENSE"
                inputs[name].write_text(name)

            result = subprocess.run(
                [
                    "python3", str(LICENSE_COLLECTOR),
                    "--repository-root", str(repository),
                    "--swift-checkouts", str(checkouts),
                    "--swift-runtime-license", str(inputs["swift"]),
                    "--protobuf-license", str(inputs["protobuf"]),
                    "--emoji-mozc-license", str(inputs["mozc"]),
                    "--emoji-unicode-license", str(inputs["unicode"]),
                    "--destination-root", str(destination),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            license_root = destination / "usr/share/licenses/fcitx5-grimodex/third-party"
            for relative_path in (
                "azookey-dictionary/LICENSE",
                "azookey-emoji/SOURCE-NOTICE.md",
                "azookey-emoji/MOZC-LICENSE",
                "azookey-emoji/UNICODE-LICENSE",
                "llama.cpp/LICENSE",
                "protobuf/LICENSE",
                "swift-runtime/LICENSE.txt",
                "swift-packages/converter/LICENSE",
                "swift-packages/swift-util/LICENSE",
            ):
                self.assertTrue((license_root / relative_path).is_file(), relative_path)

    def test_license_collector_fails_closed_when_a_resolved_license_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            repository = root / "repository"
            server = repository / "hazkey-server"
            checkouts = root / "checkouts"
            destination = root / "stage"
            (server / "azooKey_dictionary_storage").mkdir(parents=True)
            (server / "azooKey_dictionary_storage/LICENSE").write_text("dictionary")
            (server / "azooKey_emoji_dictionary_storage/data").mkdir(parents=True)
            (server / "azooKey_emoji_dictionary_storage/data/README.md").write_text(
                "emoji notice"
            )
            (server / "llama.cpp").mkdir(parents=True)
            (server / "llama.cpp/LICENSE").write_text("llama")
            (server / "Package.resolved").write_text(
                json.dumps({"pins": [{"identity": "missing"}]}), encoding="utf-8"
            )
            (checkouts / "missing").mkdir(parents=True)
            inputs = {}
            for name in ("swift", "protobuf", "mozc", "unicode"):
                inputs[name] = root / f"{name}-LICENSE"
                inputs[name].write_text(name)

            result = subprocess.run(
                [
                    "python3", str(LICENSE_COLLECTOR),
                    "--repository-root", str(repository),
                    "--swift-checkouts", str(checkouts),
                    "--swift-runtime-license", str(inputs["swift"]),
                    "--protobuf-license", str(inputs["protobuf"]),
                    "--emoji-mozc-license", str(inputs["mozc"]),
                    "--emoji-unicode-license", str(inputs["unicode"]),
                    "--destination-root", str(destination),
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(
                "no license document found for Swift package missing",
                result.stderr,
            )

    def test_settings_and_desktop_use_the_packaged_grimodex_icon(self) -> None:
        settings_cmake = (
            REPOSITORY_ROOT / "hazkey-settings/CMakeLists.txt"
        ).read_text(encoding="utf-8")
        resource = (REPOSITORY_ROOT / "hazkey-settings/hazkey-icon.qrc").read_text(
            encoding="utf-8"
        )
        window = (REPOSITORY_ROOT / "hazkey-settings/mainwindow.ui").read_text(
            encoding="utf-8"
        )

        self.assertIn('set(GRIMODEX_ICON_NAME "fcitx5-grimodex")', settings_cmake)
        self.assertIn('alias="grimodex.svg"', resource)
        self.assertNotIn("<file>hazkey.svg</file>", resource)
        self.assertIn(":/images/grimodex.svg", window)
        self.assertNotIn(":/images/hazkey.svg", window)


class ProductArtifactContractTests(unittest.TestCase):
    def test_staged_validator_accepts_canonical_and_multiarch_grimodex_paths(self) -> None:
        entries = parse_path_manifest(INSTALL_MANIFEST)
        for multiarch in ("", "x86_64-linux-gnu/"):
            with self.subTest(multiarch=multiarch):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    for pattern in REQUIRED_PACKAGED_PATHS:
                        path = (
                            pattern.replace("{,*/}", multiarch)
                            .replace("**", "fixture")
                            .replace("*", "fixture")
                        )
                        destination = root / path.removeprefix("/")
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination.write_bytes(b"local Grimodex product")
                    validate_staged_root(root, entries)

    def test_staged_validator_accepts_explicit_recursive_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            dictionary = root / "usr/share/fcitx5-grimodex/Dictionary/p/pc_1.csv"
            dictionary.parent.mkdir(parents=True)
            dictionary.write_bytes(b"local product")

            validate_staged_root(
                root,
                [("required", "/usr/share/fcitx5-grimodex/**")],
            )

    def test_staged_validator_rejects_hazkey_public_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            path = root / "usr/lib/x86_64-linux-gnu/fcitx5/fcitx5-hazkey.so"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"local product")
            with self.assertRaisesRegex(AssertionError, "Hazkey public path"):
                validate_staged_root(root, [("optional", "/usr/lib/*/fcitx5/*")])

    def test_staged_validator_rejects_nested_multiarch_libdir(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            addon = root / "usr/lib/vendor/x86_64-linux-gnu/fcitx5/fcitx5-grimodex.so"
            addon.parent.mkdir(parents=True)
            addon.write_bytes(b"local product")

            with self.assertRaisesRegex(AssertionError, "unowned staged package path"):
                validate_staged_root(
                    root,
                    [("required", "/usr/lib/{,*/}fcitx5/fcitx5-grimodex.so")],
                )

    def test_artifact_validator_accepts_domain_only_dependency_metadata(self) -> None:
        with tempfile.NamedTemporaryFile() as artifact:
            Path(artifact.name).write_bytes(
                b"docs\0https://huggingface.co/Miwa-Keita/zenz-v3-small-gguf\0"
            )
            validate_artifact_bytes(Path(artifact.name))

    def test_artifact_validator_rejects_concrete_network_clients(self) -> None:
        for marker in FORBIDDEN_ARTIFACT_MARKERS:
            with self.subTest(marker=marker):
                with tempfile.NamedTemporaryFile() as artifact:
                    Path(artifact.name).write_bytes(b"prefix\0" + marker + b"\0suffix")
                    with self.assertRaisesRegex(AssertionError, "forbidden"):
                        validate_artifact_bytes(Path(artifact.name))

    def test_swift_hub_source_audit_accepts_local_config_with_domain_metadata(self) -> None:
        auditor = self._load_swift_hub_auditor()
        with tempfile.TemporaryDirectory() as temporary_directory:
            hub = Path(temporary_directory) / "Sources/Hub"
            hub.mkdir(parents=True)
            for name in ("Downloader.swift", "Hub.swift"):
                (hub / name).write_text("import Foundation\n", encoding="utf-8")
            (hub / "HubApi.swift").write_text(
                'let endpoint = "https://huggingface.co"\n'
                "let data = try Data(contentsOf: fileURL)\n",
                encoding="utf-8",
            )
            auditor.audit_hub_sources(hub)

    def test_swift_hub_source_audit_rejects_network_capabilities(self) -> None:
        auditor = self._load_swift_hub_auditor()
        for source in (
            "let task = URLSession.shared.dataTask(with: request)\n",
            "import FoundationNetworking\n",
            "let client = HTTPClient(eventLoopGroupProvider: .createNew)\n",
            "curl_easy_perform(handle)\n",
        ):
            with self.subTest(source=source):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    hub = Path(temporary_directory) / "Sources/Hub"
                    hub.mkdir(parents=True)
                    for name in ("Downloader.swift", "Hub.swift"):
                        (hub / name).write_text("import Foundation\n", encoding="utf-8")
                    (hub / "HubApi.swift").write_text(source, encoding="utf-8")
                    with self.assertRaisesRegex(
                        auditor.OfflineHubAuditError,
                        "network capability",
                    ):
                        auditor.audit_hub_sources(hub)

    def test_swift_hub_checkout_audit_is_exact_and_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            checkout = root / "checkouts/swift-tokenizers"
            hub = checkout / "Sources/Hub"
            hub.mkdir(parents=True)
            for name in ("Downloader.swift", "Hub.swift"):
                (hub / name).write_text("import Foundation\n", encoding="utf-8")
            (hub / "HubApi.swift").write_text(
                "let data = try Data(contentsOf: fileURL)\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "init", "-q", checkout], check=True)
            subprocess.run(
                ["git", "-C", checkout, "config", "user.email", "ci@example.invalid"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", checkout, "config", "user.name", "CI"],
                check=True,
            )
            subprocess.run(["git", "-C", checkout, "add", "."], check=True)
            subprocess.run(
                ["git", "-C", checkout, "commit", "-q", "-m", "fixture"],
                check=True,
            )
            revision = subprocess.run(
                ["git", "-C", checkout, "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            resolved = root / "Package.resolved"
            resolved.write_text(
                json.dumps(
                    {
                        "pins": [
                            {
                                "identity": "swift-tokenizers",
                                "state": {"revision": revision},
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            command = [
                "python3",
                str(SWIFT_HUB_AUDITOR),
                "--package-resolved",
                str(resolved),
                "--checkout",
                str(checkout),
                "--expected-revision",
                revision,
            ]
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

            wrong_revision = "0" * 40
            rejected = subprocess.run(
                [*command[:-1], wrong_revision], capture_output=True, text=True
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("revision", rejected.stderr.lower())

    @staticmethod
    def _load_swift_hub_auditor():
        spec = importlib.util.spec_from_file_location(
            "audit_swift_hub_offline",
            SWIFT_HUB_AUDITOR,
        )
        if spec is None or spec.loader is None:
            raise AssertionError(f"cannot load {SWIFT_HUB_AUDITOR}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_staged_install_and_uninstall_manifests_own_the_real_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            server = root / "usr/bin/fcitx5-grimodex-server"
            server.parent.mkdir(parents=True)
            server.write_bytes(b"real staged server")
            entries = [("required", "/usr/bin/fcitx5-grimodex-server")]

            validate_staged_install_and_uninstall(root, entries, entries)

    def test_staged_install_rejects_a_path_not_owned_on_uninstall(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            server = root / "usr/bin/fcitx5-grimodex-server"
            server.parent.mkdir(parents=True)
            server.write_bytes(b"real staged server")
            install_entries = [("required", "/usr/bin/fcitx5-grimodex-server")]

            with self.assertRaisesRegex(AssertionError, "uninstall manifest"):
                validate_staged_install_and_uninstall(root, install_entries, [])

    def test_optional_release_artifacts(self) -> None:
        configured = os.environ.get("GRIMODEX_PRODUCT_ARTIFACTS", "")
        for raw_path in filter(None, configured.split(os.pathsep)):
            artifact = Path(raw_path)
            self.assertTrue(artifact.is_file(), f"artifact does not exist: {artifact}")
            validate_artifact_bytes(artifact)

    def test_optional_staged_install(self) -> None:
        configured = os.environ.get("GRIMODEX_STAGED_ROOT")
        if configured:
            validate_staged_install_and_uninstall(
                Path(configured),
                parse_path_manifest(INSTALL_MANIFEST),
                parse_path_manifest(UNINSTALL_MANIFEST),
            )


if __name__ == "__main__":
    unittest.main()
