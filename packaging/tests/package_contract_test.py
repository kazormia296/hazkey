#!/usr/bin/env python3
"""Packaging contracts for the standalone Grimodex Fcitx5 product.

The suite is intentionally stdlib-only so Debian and Arch packaging jobs can
run it before installing any project dependencies.  Set GRIMODEX_STAGED_ROOT
to validate a DESTDIR/package root and GRIMODEX_PRODUCT_ARTIFACTS to an
os.pathsep-separated list of release artifacts for binary-content checks.
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path
import re
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

FORBIDDEN_ARTIFACT_MARKERS = (
    b"qt6network",
    b"qnetworkaccessmanager",
    b"qnetworkrequest",
    b"qnetworkreply",
    b"huggingface",
    b"huggingface.co",
    b"hf.co",
)

REQUIRED_PACKAGED_PATHS = (
    "/usr/bin/fcitx5-grimodex-server",
    "/usr/bin/fcitx5-grimodex-settings",
    "/usr/lib/*/fcitx5/fcitx5-grimodex.so",
    "/usr/lib/*/fcitx5-grimodex/fcitx5-grimodex-server",
    "/usr/lib/*/fcitx5-grimodex/fcitx5-grimodex-settings",
    "/usr/share/applications/fcitx5-grimodex-settings.desktop",
    "/usr/share/fcitx5/addon/grimodex.conf",
    "/usr/share/fcitx5/inputmethod/grimodex.conf",
    "/usr/share/icons/hicolor/scalable/apps/fcitx5-grimodex.svg",
    "/usr/share/licenses/fcitx5-grimodex/LICENSE",
    "/usr/share/licenses/fcitx5-grimodex/NOTICE.md",
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

    patterns = [pattern for _, pattern in entries]
    for path in paths:
        if "hazkey" in path.lower():
            raise AssertionError(f"Hazkey public path leaked into Grimodex package: {path}")
        if not any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns):
            raise AssertionError(f"unowned staged package path: {path}")

    for kind, pattern in entries:
        if kind == "required" and not any(
            fnmatch.fnmatchcase(path, pattern) for path in paths
        ):
            raise AssertionError(f"required packaged path is missing: {pattern}")

    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            validate_artifact_bytes(path)


class PackageMetadataContractTests(unittest.TestCase):
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


class ProductArtifactContractTests(unittest.TestCase):
    def test_staged_validator_accepts_grimodex_paths(self) -> None:
        entries = parse_path_manifest(INSTALL_MANIFEST)
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for pattern in REQUIRED_PACKAGED_PATHS:
                path = pattern.replace("*", "x86_64-linux-gnu")
                destination = root / path.removeprefix("/")
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"local Grimodex product")
            validate_staged_root(root, entries)

    def test_staged_validator_rejects_hazkey_public_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            path = root / "usr/lib/x86_64-linux-gnu/fcitx5/fcitx5-hazkey.so"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"local product")
            with self.assertRaisesRegex(AssertionError, "Hazkey public path"):
                validate_staged_root(root, [("optional", "/usr/lib/*/fcitx5/*")])

    def test_artifact_validator_rejects_network_clients_and_huggingface(self) -> None:
        for marker in FORBIDDEN_ARTIFACT_MARKERS:
            with self.subTest(marker=marker):
                with tempfile.NamedTemporaryFile() as artifact:
                    Path(artifact.name).write_bytes(b"prefix\0" + marker + b"\0suffix")
                    with self.assertRaisesRegex(AssertionError, "forbidden"):
                        validate_artifact_bytes(Path(artifact.name))

    def test_optional_release_artifacts(self) -> None:
        configured = os.environ.get("GRIMODEX_PRODUCT_ARTIFACTS", "")
        for raw_path in filter(None, configured.split(os.pathsep)):
            artifact = Path(raw_path)
            self.assertTrue(artifact.is_file(), f"artifact does not exist: {artifact}")
            validate_artifact_bytes(artifact)

    def test_optional_staged_install(self) -> None:
        configured = os.environ.get("GRIMODEX_STAGED_ROOT")
        if configured:
            validate_staged_root(Path(configured), parse_path_manifest(INSTALL_MANIFEST))


if __name__ == "__main__":
    unittest.main()
