#!/usr/bin/env python3
"""Packaging contracts for the standalone Grimodex Fcitx5 product.

The suite is intentionally stdlib-only so Debian and Arch packaging jobs can
run it before installing any project dependencies.  Set GRIMODEX_STAGED_ROOT
to validate a DESTDIR/package root and GRIMODEX_PRODUCT_ARTIFACTS to an
os.pathsep-separated list of release artifacts for binary-content checks.
"""

from __future__ import annotations

import fnmatch
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock


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
PRODUCT_NETWORK_AUDITOR = (
    REPOSITORY_ROOT / "packaging/scripts/audit_product_network_capabilities.py"
)
AUR_RELEASE_RENDERER = (
    REPOSITORY_ROOT / "packaging/scripts/render_aur_release.py"
)
TOP_LEVEL_CMAKE = REPOSITORY_ROOT / "CMakeLists.txt"
FCITX_CMAKE = REPOSITORY_ROOT / "fcitx5-hazkey/CMakeLists.txt"
FCITX_SOURCE_CMAKE = REPOSITORY_ROOT / "fcitx5-hazkey/src/CMakeLists.txt"
DEBIAN_CHANGELOG = REPOSITORY_ROOT / "debian/changelog"
SWIFT_TOKENIZERS_REVISION = "4a606f66e0cc4d7d9f0197649e812f7fc86a4c34"
SERVER_CMAKE = REPOSITORY_ROOT / "hazkey-server/CMakeLists.txt"

SWIFT_RUNTIME_RESOURCES = (
    (
        "AzooKeyKanaKanjiConverter_EfficientNGram.resources",
        "tokenizer/tokenizer.json",
    ),
    (
        "swift-transformers_Hub.resources",
        "gpt2_tokenizer_config.json",
    ),
)

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
    b"niohttp1",
    b"curl_easy_",
    b"curl_multi_",
    b"curl_share_",
    b"curl_url_",
    b"libcurl.so",
)

REQUIRED_PACKAGED_PATHS = (
    "/usr/bin/fcitx5-grimodex-server",
    "/usr/bin/fcitx5-grimodex-settings",
    "/usr/lib/{,*/}fcitx5/fcitx5-grimodex.so",
    "/usr/lib/{,*/}fcitx5-grimodex/fcitx5-grimodex-server",
    "/usr/lib/{,*/}fcitx5-grimodex/fcitx5-grimodex-settings",
    "/usr/lib/{,*/}fcitx5-grimodex/AzooKeyKanaKanjiConverter_EfficientNGram.resources/tokenizer/tokenizer.json",
    "/usr/lib/{,*/}fcitx5-grimodex/swift-transformers_Hub.resources/gpt2_tokenizer_config.json",
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


_PRODUCT_NETWORK_AUDITOR_MODULE = None


def load_product_network_auditor():
    global _PRODUCT_NETWORK_AUDITOR_MODULE
    if _PRODUCT_NETWORK_AUDITOR_MODULE is None:
        spec = importlib.util.spec_from_file_location(
            "audit_product_network_capabilities_for_contract",
            PRODUCT_NETWORK_AUDITOR,
        )
        if spec is None or spec.loader is None:
            raise AssertionError(f"cannot load {PRODUCT_NETWORK_AUDITOR}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _PRODUCT_NETWORK_AUDITOR_MODULE = module
    return _PRODUCT_NETWORK_AUDITOR_MODULE


def validate_artifact_bytes(path: Path) -> None:
    auditor = load_product_network_auditor()
    try:
        auditor.audit_artifact(path)
    except auditor.ProductNetworkAuditError as error:
        raise AssertionError(str(error)) from error


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

    auditor = load_product_network_auditor()
    try:
        auditor.audit_tree(root, require_elf=False)
    except auditor.ProductNetworkAuditError as error:
        raise AssertionError(str(error)) from error


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

    def test_all_release_versions_are_consistent(self) -> None:
        def match(pattern: str, text: str, label: str) -> str:
            result = re.search(pattern, text, re.MULTILINE)
            self.assertIsNotNone(result, f"cannot resolve {label} version")
            assert result is not None
            return result.group(1)

        top_level_version = match(
            r"^project\(grimodex-ime VERSION ([0-9]+\.[0-9]+\.[0-9]+)\)$",
            TOP_LEVEL_CMAKE.read_text(encoding="utf-8"),
            "top-level CMake",
        )
        component_version = match(
            r"^project\(fcitx5-grimodex VERSION ([0-9]+\.[0-9]+\.[0-9]+)\)$",
            FCITX_CMAKE.read_text(encoding="utf-8"),
            "Fcitx CMake",
        )
        pkgbuild_version = match(
            r"^pkgver=([0-9]+\.[0-9]+\.[0-9]+)$",
            (AUR_DIRECTORY / "PKGBUILD").read_text(encoding="utf-8"),
            "PKGBUILD",
        )
        srcinfo_version = match(
            r"^\s*pkgver = ([0-9]+\.[0-9]+\.[0-9]+)$",
            (AUR_DIRECTORY / ".SRCINFO").read_text(encoding="utf-8"),
            ".SRCINFO",
        )
        debian_version = match(
            r"^fcitx5-grimodex \(([0-9]+\.[0-9]+\.[0-9]+)-[^)]+\)",
            DEBIAN_CHANGELOG.read_text(encoding="utf-8"),
            "Debian changelog",
        )

        self.assertEqual(
            {
                top_level_version,
                component_version,
                pkgbuild_version,
                srcinfo_version,
                debian_version,
            },
            {top_level_version},
        )

    def test_aur_template_defers_extraction_and_requires_release_hashes(self) -> None:
        pkgbuild = (AUR_DIRECTORY / "PKGBUILD").read_text(encoding="utf-8")
        srcinfo = (AUR_DIRECTORY / ".SRCINFO").read_text(encoding="utf-8")

        self.assertNotIn("SKIP", pkgbuild)
        self.assertNotIn("SKIP", srcinfo)
        for architecture in ("x86_64", "aarch64"):
            archive = f'"${{_pkgname}}-${{pkgver}}-{architecture}.tar.zst"'
            self.assertIn(archive, pkgbuild)
            self.assertIn(
                f"noextract = fcitx5-grimodex-0.2.1-{architecture}.tar.zst",
                srcinfo,
            )
        self.assertLess(pkgbuild.index("noextract=("), pkgbuild.index("prepare()"))
        self.assertIn("bsdtar --no-same-owner -xf", pkgbuild)
        self.assertIn('test -L "${pkgdir}/usr/bin/fcitx5-grimodex-settings"', pkgbuild)
        self.assertIn("od -An -tx1 -N4", pkgbuild)
        self.assertIn('[[ "${magic}" == "7f454c46" ]]', pkgbuild)

    def test_release_addon_install_path_is_overridable_without_changing_default(self) -> None:
        cmake = FCITX_CMAKE.read_text(encoding="utf-8")
        source_cmake = FCITX_SOURCE_CMAKE.read_text(encoding="utf-8")
        workflow = BUILD_WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("set(GRIMODEX_FCITX_ADDON_INSTALL_DIR", cmake)
        self.assertIn('"${FCITX_INSTALL_LIBDIR}/fcitx5"', cmake)
        self.assertIn("CACHE STRING", cmake)
        self.assertIn(
            'DESTINATION "${GRIMODEX_FCITX_ADDON_INSTALL_DIR}"',
            source_cmake,
        )
        self.assertIn(
            "-DGRIMODEX_FCITX_ADDON_INSTALL_DIR=${{ matrix.install_libdir }}/fcitx5",
            workflow,
        )

    def test_release_archives_have_reproducible_root_owned_metadata(self) -> None:
        workflow = BUILD_WORKFLOW.read_text(encoding="utf-8")

        for argument in (
            "--sort=name",
            '--mtime="@${SOURCE_DATE_EPOCH}"',
            "--owner=0",
            "--group=0",
            "--numeric-owner",
            "--pax-option=delete=atime,delete=ctime",
        ):
            with self.subTest(argument=argument):
                self.assertIn(argument, workflow)

    def test_publish_waits_for_real_package_manager_transactions(self) -> None:
        workflow = BUILD_WORKFLOW.read_text(encoding="utf-8")

        for contract in (
            "debian-payload-transaction:",
            "arch-package-transaction:",
            "dpkg --install",
            "dpkg --verify",
            "dpkg --remove",
            "makepkg --cleanbuild",
            "pacman --upgrade",
            "pacman --check",
            "pacman --remove",
            "render_aur_release.py",
            "hazkey-sentinels.sha256",
        ):
            with self.subTest(contract=contract):
                self.assertIn(contract, workflow)
        self.assertIn(
            "needs: [build, debian-payload-transaction, arch-package-transaction]",
            workflow,
        )

    def test_aur_release_renderer_pins_archive_and_sidecar_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            assets = root / "assets"
            output = root / "output"
            assets.mkdir()
            expected: dict[str, tuple[str, str]] = {}
            for architecture in ("x86_64", "aarch64"):
                archive = assets / f"fcitx5-grimodex-0.2.1-{architecture}.tar.zst"
                archive.write_bytes(f"synthetic {architecture} payload".encode())
                archive_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
                sidecar = archive.with_name(archive.name + ".sha256")
                sidecar.write_text(
                    f"{archive_hash}  {archive.name}\n",
                    encoding="utf-8",
                )
                expected[architecture] = (
                    archive_hash,
                    hashlib.sha256(sidecar.read_bytes()).hexdigest(),
                )

            result = subprocess.run(
                [
                    "python3",
                    str(AUR_RELEASE_RENDERER),
                    "--template-dir",
                    str(AUR_DIRECTORY),
                    "--asset-dir",
                    str(assets),
                    "--output-dir",
                    str(output),
                    "--version",
                    "0.2.1",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            rendered = (output / "PKGBUILD").read_text(encoding="utf-8")
            self.assertNotIn("RELEASE_", rendered)
            self.assertNotIn("SKIP", rendered)
            for architecture, hashes in expected.items():
                self.assertIn(
                    f"sha256sums_{architecture}=('{hashes[0]}' '{hashes[1]}')",
                    rendered,
                )
            self.assertEqual(
                (output / "fcitx5-grimodex.install").read_bytes(),
                (AUR_DIRECTORY / "fcitx5-grimodex.install").read_bytes(),
            )

    def test_aur_release_renderer_rejects_a_tampered_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            assets = root / "assets"
            output = root / "output"
            assets.mkdir()
            for architecture in ("x86_64", "aarch64"):
                archive = assets / f"fcitx5-grimodex-0.2.1-{architecture}.tar.zst"
                archive.write_bytes(architecture.encode())
                sidecar = archive.with_name(archive.name + ".sha256")
                sidecar.write_text(f"{'0' * 64}  {archive.name}\n", encoding="utf-8")

            result = subprocess.run(
                [
                    "python3",
                    str(AUR_RELEASE_RENDERER),
                    "--template-dir",
                    str(AUR_DIRECTORY),
                    "--asset-dir",
                    str(assets),
                    "--output-dir",
                    str(output),
                    "--version",
                    "0.2.1",
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("checksum", result.stderr.lower())
            self.assertFalse(output.exists())

    def test_aur_network_gate_scans_binary_payloads_case_insensitively(self) -> None:
        pkgbuild = (AUR_DIRECTORY / "PKGBUILD").read_text(encoding="utf-8")
        self.assertIn("readelf -dW", pkgbuild)
        self.assertIn("readelf -sW", pkgbuild)
        self.assertIn("-type f -o -type l", pkgbuild)
        self.assertIn("readlink --", pkgbuild)
        self.assertIn("unsafe symbolic link", pkgbuild)
        self.assertIn("grep -aEqi 'libcurl", pkgbuild)
        command = re.search(
            r"if grep (?P<flags>-\w+) '(?P<pattern>[^']+)' "
            r'<<<"\$\{metadata\}"',
            pkgbuild,
        )
        self.assertIsNotNone(command, "cannot find AUR ELF metadata gate")
        assert command is not None

        for marker in FORBIDDEN_ARTIFACT_MARKERS:
            with self.subTest(marker=marker):
                result = subprocess.run(
                    [
                        "grep",
                        command.group("flags"),
                        command.group("pattern"),
                    ],
                    input=marker.decode("ascii"),
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(
                    result.returncode,
                    0,
                    f"AUR metadata gate skipped marker {marker!r}",
                )

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

    def test_release_build_audits_elf_metadata_before_stripping_symbols(self) -> None:
        workflow = BUILD_WORKFLOW.read_text(encoding="utf-8")
        audit = (
            "python3 packaging/scripts/audit_product_network_capabilities.py "
            "--root ${{ github.workspace }}/workdir"
        )
        strip_step = "- name: Strip binaries"

        self.assertIn(audit, workflow)
        self.assertLess(workflow.index(audit), workflow.index(strip_step))
        for source in (
            "fcitx5-hazkey/src",
            "hazkey-settings",
            "hazkey-server/Sources",
            "linux-shared",
            "protocol",
            "hazkey-server/hazkey-server.sh.in",
        ):
            with self.subTest(source=source):
                self.assertIn(source, workflow)

    def test_swift_runtime_resources_are_installed_and_owned(self) -> None:
        cmake = SERVER_CMAKE.read_text(encoding="utf-8")
        install_entries = parse_path_manifest(INSTALL_MANIFEST)
        uninstall_entries = parse_path_manifest(UNINSTALL_MANIFEST)

        self.assertIn("HAZKEY_SERVER_SWIFT_RESOURCE_BUNDLES", cmake)
        self.assertIn(
            "${CMAKE_CURRENT_BINARY_DIR}/swift-build/${SWIFT_BUILD_TYPE}/${_bundle}",
            cmake,
        )
        self.assertIn(
            "DESTINATION ${CMAKE_INSTALL_FULL_LIBDIR}/fcitx5-grimodex",
            cmake,
        )

        for bundle, sentinel in SWIFT_RUNTIME_RESOURCES:
            with self.subTest(bundle=bundle):
                self.assertIn(bundle, cmake)
                recursive = (
                    f"/usr/lib/{{,*/}}fcitx5-grimodex/{bundle}/**"
                )
                required = (
                    "required",
                    f"/usr/lib/{{,*/}}fcitx5-grimodex/{bundle}/{sentinel}",
                )
                self.assertIn(("optional", recursive), install_entries)
                self.assertIn(required, install_entries)
                self.assertIn(("optional", recursive), uninstall_entries)
                self.assertIn(required, uninstall_entries)

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

    def test_staged_validator_accepts_owned_internal_binary_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            target = root / "usr/lib/fcitx5-grimodex/fcitx5-grimodex-settings"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"packaged target")
            link = root / "usr/bin/fcitx5-grimodex-settings"
            link.parent.mkdir(parents=True)
            link.symlink_to(
                "/usr/lib/fcitx5-grimodex/fcitx5-grimodex-settings"
            )

            validate_staged_root(
                root,
                [
                    ("required", "/usr/bin/fcitx5-grimodex-settings"),
                    (
                        "required",
                        "/usr/lib/fcitx5-grimodex/fcitx5-grimodex-settings",
                    ),
                ],
            )

    def test_staged_validator_rejects_external_binary_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            link = root / "usr/bin/fcitx5-grimodex-server"
            link.parent.mkdir(parents=True)
            link.symlink_to("/usr/bin/curl")

            with self.assertRaisesRegex(AssertionError, "symbolic link"):
                validate_staged_root(
                    root,
                    [("required", "/usr/bin/fcitx5-grimodex-server")],
                )

    def test_artifact_validator_accepts_domain_only_dependency_metadata(self) -> None:
        with tempfile.NamedTemporaryFile() as artifact:
            Path(artifact.name).write_bytes(
                b"docs\0https://huggingface.co/Miwa-Keita/zenz-v3-small-gguf\0"
            )
            validate_artifact_bytes(Path(artifact.name))

    def test_staged_validator_does_not_treat_notice_text_as_capability(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            notice = root / "usr/share/licenses/fcitx5-grimodex/NOTICE.md"
            notice.parent.mkdir(parents=True)
            notice.write_text(
                "This notice documents URLSession without providing networking.\n",
                encoding="utf-8",
            )
            validate_staged_root(
                root,
                [("required", "/usr/share/licenses/fcitx5-grimodex/NOTICE.md")],
            )

    def test_elf_audit_uses_metadata_instead_of_rodata_substrings(self) -> None:
        auditor = self._load_product_network_auditor()
        with tempfile.NamedTemporaryFile() as artifact:
            path = Path(artifact.name)
            path.write_bytes(b"\x7fELF\0harmless URLSession documentation\0")
            clean = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="NEEDED Shared library: [libc.so.6]\n",
                stderr="",
            )
            with mock.patch.object(auditor.subprocess, "run", return_value=clean):
                auditor.audit_artifact(path)

            transitive_posix = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="UND getaddrinfo\nUND getnameinfo\nUND inet_pton\n",
                stderr="",
            )
            with mock.patch.object(
                auditor.subprocess,
                "run",
                return_value=transitive_posix,
            ):
                auditor.audit_artifact(path)

            path.write_bytes(b"\x7fELF\0libcurl.so.4\0curl_easy_perform\0")
            with mock.patch.object(auditor.subprocess, "run", return_value=clean):
                with self.assertRaisesRegex(
                    auditor.ProductNetworkAuditError,
                    "network capability identifier",
                ):
                    auditor.audit_artifact(path)

    def test_elf_audit_marker_set_matches_package_contract(self) -> None:
        auditor = self._load_product_network_auditor()
        self.assertEqual(
            tuple(marker.decode("ascii") for marker in FORBIDDEN_ARTIFACT_MARKERS),
            auditor.FORBIDDEN_ELF_METADATA_MARKERS,
        )

    def test_elf_audit_rejects_network_dependencies_and_symbols(self) -> None:
        auditor = self._load_product_network_auditor()
        for marker in FORBIDDEN_ARTIFACT_MARKERS:
            with self.subTest(marker=marker):
                with tempfile.NamedTemporaryFile() as artifact:
                    path = Path(artifact.name)
                    path.write_bytes(b"\x7fELF")
                    metadata = subprocess.CompletedProcess(
                        args=[],
                        returncode=0,
                        stdout=f"Symbol or NEEDED entry: {marker.decode('ascii')}\n",
                        stderr="",
                    )
                    with mock.patch.object(
                        auditor.subprocess,
                        "run",
                        return_value=metadata,
                    ):
                        with self.assertRaisesRegex(
                            auditor.ProductNetworkAuditError,
                            "network capability",
                        ):
                            auditor.audit_artifact(path)

    def test_elf_audit_fails_closed_when_metadata_is_unreadable(self) -> None:
        auditor = self._load_product_network_auditor()
        with tempfile.NamedTemporaryFile() as artifact:
            path = Path(artifact.name)
            path.write_bytes(b"\x7fELF")
            failed = subprocess.CompletedProcess(
                args=[],
                returncode=1,
                stdout="",
                stderr="not an auditable ELF",
            )
            with mock.patch.object(auditor.subprocess, "run", return_value=failed):
                with self.assertRaisesRegex(
                    auditor.ProductNetworkAuditError,
                    "cannot inspect ELF metadata",
                ):
                    auditor.audit_artifact(path)

    def test_executable_script_audit_rejects_download_commands(self) -> None:
        auditor = self._load_product_network_auditor()
        with tempfile.NamedTemporaryFile() as artifact:
            path = Path(artifact.name)
            path.write_text("#!/bin/sh\ncurl https://example.invalid/model\n")
            path.chmod(0o755)
            with self.assertRaisesRegex(
                auditor.ProductNetworkAuditError,
                "network command",
            ):
                auditor.audit_artifact(path)

    def test_source_audit_allows_only_local_unix_socket_code(self) -> None:
        auditor = self._load_product_network_auditor()
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "connector.cpp"
            source.write_text(
                "int fd = socket(AF_UNIX, SOCK_STREAM, 0);\n",
                encoding="utf-8",
            )
            auditor.audit_source_file(source)

            for capability in (
                "int fd = socket(AF_INET, SOCK_STREAM, 0);\n",
                'void *h = dlopen("libcurl.so.4", RTLD_NOW);\n',
                'auto task = URLSession.shared.dataTask(with: request);\n',
            ):
                with self.subTest(capability=capability):
                    source.write_text(capability, encoding="utf-8")
                    with self.assertRaisesRegex(
                        auditor.ProductNetworkAuditError,
                        "source capability",
                    ):
                        auditor.audit_source_file(source)

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
            "let fd = socket(AF_INET, SOCK_STREAM, 0)\n",
            "getaddrinfo(host, port, &hints, &result)\n",
            "let process = Process()\n",
            'popen("curl https://example.invalid", "r")\n',
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

    @staticmethod
    def _load_product_network_auditor():
        return load_product_network_auditor()

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
