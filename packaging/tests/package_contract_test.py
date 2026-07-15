#!/usr/bin/env python3
"""Packaging contracts for the standalone Grimodex Fcitx5 product.

The suite is intentionally stdlib-only so Debian and Arch packaging jobs can
run it before installing any project dependencies.  Set GRIMODEX_STAGED_ROOT
to validate a DESTDIR/package root and GRIMODEX_PRODUCT_ARTIFACTS to an
os.pathsep-separated list of release artifacts for binary-content checks.
"""

from __future__ import annotations

import argparse
import errno
import fnmatch
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
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
MOZC_ARTIFACT_VERIFIER = (
    REPOSITORY_ROOT / "packaging/scripts/verify_mozc_artifact_bundle.py"
)
MOZC_ARTIFACT_BUILDER = (
    REPOSITORY_ROOT / "tools/mozc/build_fixed_sidecar_bundle.py"
)
MOZC_RUNTIME_SCRIPT = REPOSITORY_ROOT / "scripts/grimodex-ime_mozc.sh"
MOZC_SIDECAR_PROTO = REPOSITORY_ROOT / "protocol/mozc_sidecar.proto"
MOZC_SIDECAR_OVERLAY_PROTO = (
    REPOSITORY_ROOT
    / "third_party/fcitx-mozkey/overlay/grimodex_mozc_sidecar/mozc_sidecar.proto"
)
MOZC_SIDECAR_HELPER_SOURCE = (
    REPOSITORY_ROOT
    / "third_party/fcitx-mozkey/overlay/grimodex_mozc_sidecar/mozc_sidecar_helper.cc"
)
MOZC_SIDECAR_B1_OVERLAY_PROTO = (
    REPOSITORY_ROOT
    / "third_party/fcitx-mozkey/overlay/grimodex_mozc_sidecar_b1/mozc_sidecar.proto"
)
MOZC_SIDECAR_B1_HELPER_SOURCE = (
    REPOSITORY_ROOT
    / "third_party/fcitx-mozkey/overlay/grimodex_mozc_sidecar_b1/mozc_sidecar_helper.cc"
)
DEFAULT_RUNTIME_SCRIPT = REPOSITORY_ROOT / "scripts/grimodex-ime.sh"
TOP_LEVEL_CMAKE = REPOSITORY_ROOT / "CMakeLists.txt"
FCITX_CMAKE = REPOSITORY_ROOT / "fcitx5-hazkey/CMakeLists.txt"
FCITX_SOURCE_CMAKE = REPOSITORY_ROOT / "fcitx5-hazkey/src/CMakeLists.txt"
FCITX_TEST_CMAKE = REPOSITORY_ROOT / "fcitx5-hazkey/tests/CMakeLists.txt"
FCITX_FULL_STACK_RUNNER = (
    REPOSITORY_ROOT / "fcitx5-hazkey/tests/run_fcitx_full_stack_test.py"
)
DEBIAN_CHANGELOG = REPOSITORY_ROOT / "debian/changelog"
SWIFT_TOKENIZERS_REVISION = "4a606f66e0cc4d7d9f0197649e812f7fc86a4c34"
SERVER_CMAKE = REPOSITORY_ROOT / "hazkey-server/CMakeLists.txt"
SWIFT_BUILD_DRIVER = REPOSITORY_ROOT / "hazkey-server/build_swift.cmake"
AZOOKEY_PREPARER = (
    REPOSITORY_ROOT / "hazkey-server/prepare_azookey_dependency.cmake"
)

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
    "/usr/lib/{,*/}fcitx5-grimodex/fcitx5-grimodex-model",
    "/usr/lib/{,*/}fcitx5-grimodex/AzooKeyKanaKanjiConverter_EfficientNGram.resources/tokenizer/tokenizer.json",
    "/usr/lib/{,*/}fcitx5-grimodex/swift-transformers_Hub.resources/gpt2_tokenizer_config.json",
    "/usr/share/applications/fcitx5-grimodex-settings.desktop",
    "/usr/share/fcitx5/addon/grimodex.conf",
    "/usr/share/fcitx5/inputmethod/grimodex.conf",
    "/usr/share/icons/hicolor/scalable/apps/fcitx5-grimodex.svg",
    "/usr/share/licenses/fcitx5-grimodex/LICENSE",
    "/usr/share/licenses/fcitx5-grimodex/NOTICE.md",
    "/usr/share/licenses/fcitx5-grimodex/THIRDPARTYLICENSE",
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
_FCITX_FULL_STACK_RUNNER_MODULE = None


def load_fcitx_full_stack_runner():
    global _FCITX_FULL_STACK_RUNNER_MODULE
    if _FCITX_FULL_STACK_RUNNER_MODULE is None:
        module_name = "fcitx_full_stack_runner_for_contract"
        spec = importlib.util.spec_from_file_location(
            module_name,
            FCITX_FULL_STACK_RUNNER,
        )
        if spec is None or spec.loader is None:
            raise AssertionError(f"cannot load {FCITX_FULL_STACK_RUNNER}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(module_name, None)
            raise
        _FCITX_FULL_STACK_RUNNER_MODULE = module
    return _FCITX_FULL_STACK_RUNNER_MODULE


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

    metadata_roots = (
        root / "usr/share/fcitx5",
        root / "usr/share/applications",
        root / "usr/share/metainfo",
    )
    placeholder = re.compile(r"@[A-Z][A-Z0-9_]+@")
    for metadata_root in metadata_roots:
        if not metadata_root.exists():
            continue
        for metadata in metadata_root.rglob("*"):
            if metadata.is_file():
                text = metadata.read_text(encoding="utf-8")
                match = placeholder.search(text)
                if match:
                    raise AssertionError(
                        f"unexpanded CMake placeholder in {metadata}: {match.group(0)}"
                    )

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
        auditor.audit_tree(
            root,
            require_elf=False,
            allow_network_artifacts={
                path
                for path in root.rglob("fcitx5-grimodex-model")
                if path.is_file()
            },
        )
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


class MozcArtifactBundleContractTests(unittest.TestCase):
    SOURCE_REVISION = "462cbbf04886e32096bc318833e974ccc43d9fc8"
    SOURCE_TREE = "95365a39134949f5d68f565e1ce451085b5965a8"
    BAZELISKRC_SHA256 = "59acd943a0d15254345f3e176f42786af2b4fba83b1657341cf56e017a7db19a"
    MODULE_LOCK_SHA256 = "ab6b647b1c12072eee26ec2370fa928b2ac7c3146e72daf232010dfe254ed972"
    OVERLAY_SHA256 = "26cf5430b39dcdc04c1f91a6ce473554c3f1ba3f04c2defdcf146f859b6776d6"
    ELF_INTERPRETER = "/lib64/ld-linux-x86-64.so.2"
    GLIBC_VERSION = "GLIBC_2.38"
    GLIBCXX_VERSION = "GLIBCXX_3.4.32"
    CXXABI_VERSION = "CXXABI_1.3.15"
    LICENSE_NAMES = (
        "MOZC-LICENSE",
        "FCITX-MOZKEY-THIRD-PARTY-NOTICES.md",
        "DICTIONARY-OSS-NOTICE.txt",
        "ABSEIL-LICENSE",
        "PROTOBUF-LICENSE",
        "UTF8-RANGE-LICENSE",
        "JAPANESE-USAGE-DICTIONARY-LICENSE",
    )

    def test_fcitx_mozc_integration_tests_are_opt_in_and_isolated(self) -> None:
        server_cmake = SERVER_CMAKE.read_text(encoding="utf-8")
        test_cmake = FCITX_TEST_CMAKE.read_text(encoding="utf-8")
        runner = FCITX_FULL_STACK_RUNNER.read_text(encoding="utf-8")

        self.assertIn("GRIMODEX_MOZC_STAGED_GENERATION", server_cmake)
        self.assertIn("GRIMODEX_MOZC_ARTIFACT_VERIFIER", server_cmake)
        self.assertIn(
            "option(GRIMODEX_ENABLE_MOZC_INTEGRATION_TESTS",
            test_cmake,
        )
        self.assertIn(
            '"Enable opt-in Mozc Fcitx full-stack and soak tests" OFF',
            test_cmake,
        )
        opt_in_block = test_cmake.index(
            "if(GRIMODEX_ENABLE_MOZC_INTEGRATION_TESTS)",
            test_cmake.index("hazkey-fcitx-full-stack-test"),
        )
        self.assertLess(
            opt_in_block,
            test_cmake.index("hazkey-fcitx-mozc-full-stack-test"),
        )
        self.assertLess(
            opt_in_block,
            test_cmake.index("hazkey-fcitx-mozc-soak-test"),
        )
        self.assertIn("--converter-backend hazkey", test_cmake)
        self.assertIn("--converter-backend mozc", test_cmake)
        self.assertNotIn("file(GLOB", test_cmake)

        self.assertIn('"--verify-host-runtime"', runner)
        self.assertIn('"--prepare-installed-runtime"', runner)
        self.assertIn('"GGML_BACKEND_DIR": str(llama_lib_dir)', runner)
        self.assertIn("exact_process_identities", runner)
        self.assertIn("create_launch_wrapper", runner)
        self.assertIn("os.pidfd_open", runner)
        self.assertIn("start_new_session=True", runner)
        self.assertNotIn("os.environ.copy()", runner)
        self.assertNotIn("grimodex-ime_mozc.sh", test_cmake + runner)
        self.assertNotIn("/usr/lib/fcitx5-grimodex", test_cmake + runner)

    def test_fcitx_full_stack_runner_bounds_and_process_identity(self) -> None:
        runner = load_fcitx_full_stack_runner()

        self.assertEqual(runner.restart_cycles("1"), 1)
        self.assertEqual(runner.restart_cycles("1000"), 1000)
        self.assertEqual(runner.timeout_seconds("1"), 1)
        self.assertEqual(runner.timeout_seconds("86400"), 86400)
        self.assertEqual(runner.nonnegative_integer("0"), 0)
        self.assertEqual(runner.nonnegative_integer("1000000"), 1000000)
        self.assertEqual(runner.product_source_ref("a" * 40), "a" * 40)
        self.assertEqual(runner.lowercase_sha256("b" * 64), "b" * 64)
        self.assertEqual(runner.product_server_size("1"), 1)
        self.assertEqual(
            runner.product_server_size(str(runner.MAXIMUM_PRODUCT_SERVER_BYTES)),
            runner.MAXIMUM_PRODUCT_SERVER_BYTES,
        )
        for parser, values in (
            (runner.restart_cycles, ("0", "1001")),
            (runner.timeout_seconds, ("0", "86401")),
            (runner.nonnegative_integer, ("-1", "1000001")),
        ):
            for value in ("", "+1", "01", " 1", "1 ", *values):
                with self.subTest(parser=parser.__name__, value=value):
                    with self.assertRaises(argparse.ArgumentTypeError):
                        parser(value)
        for value in ("", "a" * 39, "a" * 41, "A" * 40, "g" * 40):
            with self.subTest(product_source_ref=value):
                with self.assertRaises(argparse.ArgumentTypeError):
                    runner.product_source_ref(value)
        for value in ("", "a" * 63, "a" * 65, "A" * 64, "g" * 64):
            with self.subTest(lowercase_sha256=value):
                with self.assertRaises(argparse.ArgumentTypeError):
                    runner.lowercase_sha256(value)
        for value in (
            "0",
            "01",
            str(runner.MAXIMUM_PRODUCT_SERVER_BYTES + 1),
        ):
            with self.subTest(product_server_size=value):
                with self.assertRaises(argparse.ArgumentTypeError):
                    runner.product_server_size(value)

        suffix = [
            "S", "1", "42", "42", "0", "-1", "0", "0", "0", "0",
            "0", "0", "0", "0", "0", "20", "0", "1", "0", "999",
        ]
        with mock.patch.object(runner.os, "readlink", return_value="/fixture"), \
                mock.patch.object(
                    runner.Path,
                    "read_text",
                    return_value="123 (worker) nested) " + " ".join(suffix),
                ):
            parsed = runner.read_process_identity(123)
        self.assertEqual(
            parsed,
            runner.ProcessIdentity(123, "/fixture", 42, 42, "999"),
        )

        current = runner.read_process_identity(os.getpid())
        self.assertIsNotNone(current)
        assert current is not None
        self.assertEqual(current.process_group, os.getpgrp())
        self.assertIn(
            current,
            runner.exact_process_identities(
                Path(current.executable),
                current.process_group,
            ),
        )
        self.assertNotIn(
            current,
            runner.exact_process_identities(
                Path(current.executable),
                current.process_group + 10_000_000,
            ),
        )

    def test_fcitx_mozc_missing_artifacts_fail_instead_of_skip(self) -> None:
        base = [
            sys.executable,
            str(FCITX_FULL_STACK_RUNNER),
            "--harness", "/definitely/missing/harness",
            "--addon", "/definitely/missing/addon",
            "--server", "/definitely/missing/server",
            "--dictionary", "/definitely/missing/dictionary",
            "--addon-config", "/definitely/missing/addon.conf",
            "--input-method-config", "/definitely/missing/im.conf",
            "--system-test-addon-dir", "/definitely/missing/testing",
            "--llama-lib-dir", "/definitely/missing/lib",
        ]
        hazkey = subprocess.run(base, capture_output=True, text=True)
        self.assertEqual(hazkey.returncode, 77)
        self.assertIn("SKIP:", hazkey.stdout)

        mozc = subprocess.run(
            [
                *base,
                "--converter-backend", "mozc",
                "--mozc-verifier", "/definitely/missing/verifier.py",
                "--mozc-generation", "/definitely/missing/generation",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(mozc.returncode, 1)
        self.assertIn("ERROR: required Mozc", mozc.stdout)

        missing_source_ref = subprocess.run(
            [
                *base,
                "--result-output",
                "/tmp/grimodex-fcitx-missing-source-ref.json",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(missing_source_ref.returncode, 2)
        self.assertIn(
            "--result-output requires --product-source-ref",
            missing_source_ref.stderr,
        )

    def test_fcitx_runner_drains_descendants_after_leader_exit(self) -> None:
        runner = load_fcitx_full_stack_runner()
        leader = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import os, subprocess; "
                "child = subprocess.Popen(['/bin/sleep', '30'], "
                "preexec_fn=os.setpgrp); print(child.pid, flush=True)",
            ],
            start_new_session=True,
            stdout=subprocess.PIPE,
            text=True,
        )
        child_pid: int | None = None
        child_identity = None
        try:
            assert leader.stdout is not None
            child_pid = int(leader.stdout.readline().strip())
            leader.wait(timeout=5)
            deadline = time.monotonic() + 2
            descendants = set()
            while time.monotonic() < deadline:
                child_identity = runner.read_process_identity(child_pid)
                descendants = runner.session_process_identities(leader.pid)
                if child_identity is not None and descendants:
                    break
                time.sleep(0.025)
            self.assertIsNotNone(child_identity)
            self.assertIn(child_identity, descendants)
            assert child_identity is not None
            self.assertNotEqual(child_identity.process_group, leader.pid)
            self.assertEqual(child_identity.session_id, leader.pid)
            self.assertTrue(runner.drain_process_session(leader.pid))
            self.assertFalse(runner.session_process_identities(leader.pid))
        finally:
            if child_identity is None and child_pid is not None:
                child_identity = runner.read_process_identity(child_pid)
            if child_identity is not None:
                runner.stop_exact_processes({child_identity})
            if leader.poll() is None:
                try:
                    os.killpg(leader.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                leader.wait(timeout=3)
            if leader.stdout is not None:
                leader.stdout.close()

    def test_fcitx_launch_audit_tracks_and_stops_exact_exec(self) -> None:
        runner = load_fcitx_full_stack_runner()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            audit = root / "launches.log"
            wrapper = root / "launch"
            target = Path("/bin/sleep").resolve(strict=True)
            runner.create_launch_wrapper(wrapper, target, audit)
            process = subprocess.Popen(
                [str(wrapper), "30"],
                start_new_session=True,
            )
            try:
                deadline = time.monotonic() + 3
                launches = []
                identities = set()
                while time.monotonic() < deadline:
                    launches = runner.load_process_launches(audit)
                    identities = runner.identities_for_launches(
                        launches,
                        target,
                    )
                    if identities:
                        break
                    time.sleep(0.01)
                self.assertEqual(len(launches), 1)
                self.assertEqual({identity.pid for identity in identities}, {process.pid})

                lock_file = root / "server.lock"
                lock_file.write_text(f"{process.pid}\n", encoding="utf-8")
                self.assertIsNone(
                    runner.identity_from_lock(
                        lock_file,
                        target,
                        [runner.ProcessLaunch(process.pid, "1")],
                    )
                )
                self.assertIn(
                    runner.identity_from_lock(lock_file, target, launches),
                    identities,
                )

                self.assertTrue(runner.stop_process_launches(launches))
                process.wait(timeout=3)
                self.assertFalse(runner.identities_for_launches(launches))
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=3)

    def test_fcitx_launch_audit_rejects_tampering(self) -> None:
        runner = load_fcitx_full_stack_runner()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            audit = root / "launches.log"
            wrapper = root / "launch"
            runner.create_launch_wrapper(wrapper, Path("/bin/true"), audit)

            audit.write_bytes(b"01 2\n")
            with self.assertRaisesRegex(
                runner.ProcessInspectionError,
                "invalid record",
            ):
                runner.load_process_launches(audit)

            audit.write_bytes(b"1 2")
            with self.assertRaisesRegex(
                runner.ProcessInspectionError,
                "incomplete record",
            ):
                runner.load_process_launches(audit)

            audit.write_bytes(b"")
            audit.chmod(0o644)
            with self.assertRaisesRegex(
                runner.ProcessInspectionError,
                "unsafe metadata",
            ):
                runner.load_process_launches(audit)

    def test_fcitx_formal_launch_wrapper_executes_only_the_bound_inode(self) -> None:
        runner = load_fcitx_full_stack_runner()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            target = root / "bound-true"
            shutil.copy2("/bin/true", target)
            target.chmod(0o755)
            audit = root / "bound-launches.log"
            wrapper = root / "bound-launch"
            runner.create_launch_wrapper(
                wrapper,
                target,
                audit,
                runner.file_binding(target),
            )
            completed = subprocess.run(
                [str(wrapper)],
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(len(runner.load_process_launches(audit)), 1)

            tampered_target = root / "tampered-true"
            shutil.copy2("/bin/true", tampered_target)
            tampered_target.chmod(0o755)
            tampered_audit = root / "tampered-launches.log"
            tampered_wrapper = root / "tampered-launch"
            binding = runner.file_binding(tampered_target)
            runner.create_launch_wrapper(
                tampered_wrapper,
                tampered_target,
                tampered_audit,
                binding,
            )
            tampered_bytes = bytearray(tampered_target.read_bytes())
            tampered_bytes[-1] ^= 1
            tampered_target.write_bytes(tampered_bytes)
            tampered_target.chmod(0o755)
            rejected = subprocess.run(
                [str(tampered_wrapper)],
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn(
                "launch target does not match its exact binding",
                rejected.stderr,
            )
            self.assertEqual(runner.load_process_launches(tampered_audit), [])

    def test_fcitx_mozc_runtime_fingerprint_matches_abprobe_contract(self) -> None:
        runner = load_fcitx_full_stack_runner()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            helper = root / "fcitx5-grimodex-mozc-helper"
            data = root / "mozc.data"
            manifest = root / "manifest.json"
            helper.write_bytes(b"helper-fixture\n")
            data.write_bytes(b"data-fixture\x00")
            manifest.write_bytes(b'{"schema":"fixture"}\n')
            (root / "licenses").mkdir()
            (root / "licenses/LICENSE").write_bytes(b"not fingerprinted\n")
            inputs = {
                helper.name: helper,
                data.name: data,
                manifest.name: manifest,
            }

            self.assertEqual(
                runner.mozc_runtime_fingerprint(inputs),
                "sha256:db5be6247665085c2763fc4f8b45443f7f773a97d0793121"
                "18c9c70e365170b4",
            )
            helper.write_bytes(b"different helper bytes\n")
            self.assertNotEqual(
                runner.mozc_runtime_fingerprint(inputs),
                "sha256:db5be6247665085c2763fc4f8b45443f7f773a97d0793121"
                "18c9c70e365170b4",
            )

            with self.assertRaisesRegex(
                runner.ResultEvidenceError,
                "requires exactly helper/data/manifest",
            ):
                runner.mozc_runtime_fingerprint(
                    {name: path for name, path in inputs.items() if name != data.name}
                )
            manifest.unlink()
            with self.assertRaisesRegex(
                runner.ResultEvidenceError,
                "cannot resolve evidence input",
            ):
                runner.mozc_runtime_fingerprint(inputs)

    def test_fcitx_formal_input_snapshot_is_complete_and_immutable(self) -> None:
        runner = load_fcitx_full_stack_runner()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source"
            private_root = root / "private"
            source.mkdir()
            private_root.mkdir(mode=0o700)

            harness = source / "harness"
            addon = source / "addon.so"
            server = source / "server"
            addon_config = source / "addon.conf"
            input_method_config = source / "input-method.conf"
            verifier = source / "verifier.py"
            harness.write_bytes(b"harness fixture\n")
            harness.chmod(0o755)
            addon.write_bytes(b"addon fixture\n")
            server_bytes = b"product server fixture\n"
            server.write_bytes(server_bytes)
            server.chmod(0o755)
            addon_config.write_bytes(b"[Addon]\n")
            input_method_config.write_bytes(b"[InputMethod]\n")
            shutil.copy2(MOZC_ARTIFACT_VERIFIER, verifier)

            dictionary = source / "dictionary"
            dictionary.mkdir()
            (dictionary / "entries.bin").write_bytes(b"dictionary fixture\n")
            llama = source / "llama"
            llama.mkdir()
            (llama / "libggml.so").write_bytes(b"llama fixture\n")
            system_addons = source / "system-addons"
            system_addons.mkdir()
            for name in ("testfrontend.conf", "testui.conf", "testim.conf"):
                (system_addons / name).write_bytes(f"{name}\n".encode())
            bundle, _, _ = self._write_bundle(source)

            args = argparse.Namespace(
                harness=harness,
                addon=addon,
                server=server,
                dictionary=dictionary,
                addon_config=addon_config,
                input_method_config=input_method_config,
                system_test_addon_dir=system_addons,
                llama_lib_dir=llama,
                converter_backend="mozc",
                mozc_verifier=verifier,
                mozc_generation=bundle,
                result_output=root / "result.json",
                product_source_ref="c" * 40,
                product_server_sha256=hashlib.sha256(server_bytes).hexdigest(),
                product_server_size=len(server_bytes),
            )
            snapshot_args, snapshot = runner.create_input_snapshot(
                args,
                private_root,
            )

            for path in (
                snapshot_args.harness,
                snapshot_args.addon,
                snapshot_args.server,
                snapshot_args.dictionary,
                snapshot_args.addon_config,
                snapshot_args.input_method_config,
                snapshot_args.system_test_addon_dir,
                snapshot_args.llama_lib_dir,
                snapshot_args.mozc_verifier,
                snapshot_args.mozc_generation,
            ):
                self.assertTrue(path.is_relative_to(snapshot.root), path)
            relative_paths = {entry.relative_path for entry in snapshot.entries}
            self.assertIn("dictionary/entries.bin", relative_paths)
            self.assertIn("llama-lib/libggml.so", relative_paths)
            self.assertIn("config/addon.conf", relative_paths)
            self.assertIn("config/input-method.conf", relative_paths)
            for license_name in self.LICENSE_NAMES:
                self.assertIn(
                    f"mozc/generation/licenses/{license_name}",
                    relative_paths,
                )
            self.assertTrue(
                all(
                    stat.S_IMODE((snapshot.root / entry.relative_path).stat().st_mode)
                    == entry.mode
                    for entry in snapshot.entries
                )
            )
            self.assertTrue(
                all(
                    stat.S_IMODE(
                        (
                            snapshot.root
                            if relative == "."
                            else snapshot.root / relative
                        ).stat().st_mode
                    )
                    == 0o555
                    for relative in snapshot.directories
                )
            )

            server.write_bytes(b"original path changed after snapshot\n")
            self.assertEqual(snapshot_args.server.read_bytes(), server_bytes)
            runner.verify_input_snapshot(snapshot)

            runtime_generation = (
                private_root / "runtime" / ("sha256-" + "1" * 64)
            )
            runtime_generation.mkdir(parents=True)
            runtime_helper = runtime_generation / "fcitx5-grimodex-mozc-helper"
            runtime_data = runtime_generation / "mozc.data"
            shutil.copy2(
                snapshot_args.mozc_generation
                / "fcitx5-grimodex-mozc-helper",
                runtime_helper,
            )
            shutil.copy2(snapshot_args.mozc_generation / "mozc.data", runtime_data)
            runtime_generation.chmod(0o555)
            bindings = runner.capture_evidence_bindings(
                snapshot_args,
                runtime_generation,
                snapshot,
            )
            original_runtime_data = runtime_data.read_bytes()
            runtime_data.chmod(0o600)
            runtime_data.write_bytes(b"X" * len(original_runtime_data))
            with self.assertRaisesRegex(
                runner.ResultEvidenceError,
                "post-run integrity mismatch",
            ):
                runner.verify_post_run_evidence(snapshot, bindings)
            runtime_data.write_bytes(original_runtime_data)
            runtime_data.chmod(0o444)
            runner.verify_post_run_evidence(snapshot, bindings)
            self.assertTrue(
                bindings["input_snapshot"]["integrity"]["post_run_verified"]
            )
            self.assertEqual(
                bindings["runtime_integrity"]["verified_artifacts"],
                ["mozc_helper", "mozc_data"],
            )

            snapshot_args.server.chmod(0o755)
            snapshot_args.server.write_bytes(b"X" * len(server_bytes))
            snapshot_args.server.chmod(0o555)
            with self.assertRaisesRegex(
                runner.ResultEvidenceError,
                "integrity mismatch",
            ):
                runner.verify_input_snapshot(snapshot)
            snapshot_args.server.chmod(0o755)
            snapshot_args.server.write_bytes(server_bytes)
            snapshot_args.server.chmod(0o555)
            runner.verify_input_snapshot(snapshot)

            snapshot.root.chmod(0o755)
            (snapshot.root / "unexpected").write_bytes(b"extra\n")
            snapshot.root.chmod(0o555)
            with self.assertRaisesRegex(
                runner.ResultEvidenceError,
                "file set changed",
            ):
                runner.verify_input_snapshot(snapshot)

            runner.make_runtime_writable(private_root)
            shutil.rmtree(private_root)
            self.assertFalse(private_root.exists())

    def test_fcitx_formal_snapshot_rejects_server_mismatch_and_special_files(
        self,
    ) -> None:
        runner = load_fcitx_full_stack_runner()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            snapshot_root = root / "snapshot"
            snapshot_root.mkdir()
            fifo = root / "fifo"
            os.mkfifo(fifo)
            symlink = root / "symlink"
            symlink.symlink_to("/bin/true")
            unix_socket = root / "socket"
            socket_handle = socket.socket(socket.AF_UNIX)
            try:
                special_paths = [fifo, symlink]
                try:
                    socket_handle.bind(str(unix_socket))
                except PermissionError:
                    # Some hermetic CI sandboxes prohibit AF_UNIX entirely.
                    pass
                else:
                    special_paths.append(unix_socket)
                for source in special_paths:
                    with self.subTest(source=source.name), self.assertRaisesRegex(
                        runner.ResultEvidenceError,
                        "regular non-symlink",
                    ):
                        runner.copy_snapshot_file(
                            source,
                            snapshot_root / source.name,
                            input_id="special",
                            snapshot_root=snapshot_root,
                        )
            finally:
                socket_handle.close()

            source = root / "source"
            private = root / "private"
            source.mkdir()
            private.mkdir()
            paths = {}
            for name in (
                "harness",
                "addon.so",
                "server",
                "addon.conf",
                "input-method.conf",
                "verifier.py",
            ):
                paths[name] = source / name
                paths[name].write_bytes((name + "\n").encode())
            paths["harness"].chmod(0o755)
            paths["server"].chmod(0o755)
            dictionary = source / "dictionary"
            dictionary.mkdir()
            (dictionary / "entry").write_bytes(b"entry\n")
            llama = source / "llama"
            llama.mkdir()
            (llama / "library").write_bytes(b"library\n")
            system_addons = source / "system"
            system_addons.mkdir()
            for name in ("testfrontend.conf", "testui.conf", "testim.conf"):
                (system_addons / name).write_bytes(b"config\n")
            bundle, _, _ = self._write_bundle(source)
            server_bytes = paths["server"].read_bytes()
            args = argparse.Namespace(
                harness=paths["harness"],
                addon=paths["addon.so"],
                server=paths["server"],
                dictionary=dictionary,
                addon_config=paths["addon.conf"],
                input_method_config=paths["input-method.conf"],
                system_test_addon_dir=system_addons,
                llama_lib_dir=llama,
                converter_backend="mozc",
                mozc_verifier=paths["verifier.py"],
                mozc_generation=bundle,
                product_server_sha256="0" * 64,
                product_server_size=len(server_bytes),
            )
            with self.assertRaisesRegex(
                runner.ResultEvidenceError,
                "does not match the explicit SHA-256/size binding",
            ):
                runner.create_input_snapshot(args, private)
            runner.make_runtime_writable(private)

            size_mismatch_private = root / "size-mismatch-private"
            size_mismatch_private.mkdir()
            size_mismatch_args = argparse.Namespace(**vars(args))
            size_mismatch_args.product_server_sha256 = hashlib.sha256(
                server_bytes
            ).hexdigest()
            size_mismatch_args.product_server_size = len(server_bytes) + 1
            with self.assertRaisesRegex(
                runner.ResultEvidenceError,
                "does not match the explicit SHA-256/size binding",
            ):
                runner.create_input_snapshot(
                    size_mismatch_args,
                    size_mismatch_private,
                )
            runner.make_runtime_writable(size_mismatch_private)

            extra_bundle_private = root / "extra-bundle-private"
            extra_bundle_private.mkdir()
            (bundle / "unexpected-empty-directory").mkdir()
            exact_args = argparse.Namespace(**vars(size_mismatch_args))
            exact_args.product_server_size = len(server_bytes)
            with self.assertRaisesRegex(
                runner.ResultEvidenceError,
                "does not match the fixed artifact set",
            ):
                runner.create_input_snapshot(exact_args, extra_bundle_private)
            runner.make_runtime_writable(extra_bundle_private)

    def test_fcitx_native_result_uses_milestone_and_launch_audit_counts(self) -> None:
        runner = load_fcitx_full_stack_runner()
        with tempfile.TemporaryDirectory() as temporary_directory:
            stderr = Path(temporary_directory) / "harness.stderr"
            stderr.write_text(
                "grimodex-fcitx-full-stack: scenario callback started\n"
                "grimodex-fcitx-full-stack: same-session conversion soak "
                "passed: 7 iterations\n",
                encoding="utf-8",
            )
            self.assertEqual(runner.completed_conversions(stderr), 7)

            stderr.write_text(
                stderr.read_text(encoding="utf-8")
                + "grimodex-fcitx-full-stack: same-session conversion soak "
                "passed: 7 iterations\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                runner.ResultEvidenceError,
                "exactly one conversion-count milestone",
            ):
                runner.completed_conversions(stderr)

        def observation(
            cycle: int,
            conversions: int,
            server_launches: int,
            helper_launches: int,
        ):
            server = tuple(
                runner.ProcessLaunch(9_000_000 + cycle * 100 + index, str(index + 1))
                for index in range(server_launches)
            )
            helper = tuple(
                runner.ProcessLaunch(9_100_000 + cycle * 100 + index, str(index + 1))
                for index in range(helper_launches)
            )
            server_identity = runner.ProcessIdentity(
                server[0].pid,
                "/fixture/server",
                10 + cycle,
                10 + cycle,
                server[0].start_time,
            )
            helper_identity = runner.ProcessIdentity(
                helper[0].pid,
                "/fixture/helper",
                10 + cycle,
                10 + cycle,
                helper[0].start_time,
            )
            return runner.CycleObservation(
                cycle=cycle,
                conversions=conversions,
                server_launches=server,
                server_identities=(server_identity,),
                helper_launches=helper,
                helper_identities=(helper_identity,),
                lock_owner_observed=True,
                max_concurrent_helpers=1,
                server_cleanup_ok=True,
                helper_cleanup_ok=True,
                process_group_cleanup_ok=True,
            )

        args = argparse.Namespace(
            converter_backend="mozc",
            soak_iterations=100,
            cycles=2,
            timeout=90,
            product_source_ref="c" * 40,
            product_server_sha256="e" * 64,
            product_server_size=123,
        )
        observations = [observation(1, 7, 1, 2), observation(2, 9, 2, 3)]
        result = runner.build_structured_result(
            args,
            observations,
            {
                "producer": {"path": "runner.py", "sha256": "a" * 64},
                "source": {
                    "repository_root": "/fixture",
                    "git_head": "b" * 40,
                    "worktree_clean": True,
                },
                "artifacts": {
                    "mozc_generation": {
                        "artifact_fingerprint": "sha256:" + "d" * 64,
                    }
                },
                "input_snapshot": {
                    "integrity": {"post_run_verified": True},
                    "entries": [
                        {
                            "input_id": "product_server",
                            "sha256": "e" * 64,
                            "size": 123,
                        }
                    ],
                },
                "runtime_integrity": {"post_run_verified": True},
            },
            ["python3", "runner.py"],
            0,
        )
        self.assertEqual(result["schema"], runner.RESULT_SCHEMA)
        self.assertEqual(result["version"], 1)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["product_source_ref"], "c" * 40)
        self.assertEqual(
            result["product_server"],
            {"sha256": "e" * 64, "size": 123},
        )
        self.assertEqual(result["artifact_fingerprint"], "sha256:" + "d" * 64)
        self.assertEqual(result["conversions"], 16)
        self.assertEqual(result["cycles"], 2)
        self.assertEqual(result["server_launches"], 3)
        self.assertEqual(result["helper_launches"], 5)
        self.assertEqual(result["server_recoveries"], 1)
        self.assertEqual(result["helper_recoveries"], 3)
        self.assertEqual(result["residue_count"], 0)
        self.assertEqual(result["configuration"]["iterations"], 100)
        self.assertEqual(
            result["cycle_results"][0]["server"]["launches"][0],
            {"pid": 9_000_100, "start_time": "1"},
        )
        self.assertEqual(
            result["cycle_results"][0]["helper"]["observed_identities"][0]
            ["executable"],
            "/fixture/helper",
        )

    def test_fcitx_native_result_is_atomic_and_never_overwrites(self) -> None:
        runner = load_fcitx_full_stack_runner()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            output = root / "result.json"
            first = {"schema": runner.RESULT_SCHEMA, "value": 1}
            destination = runner.open_result_output(output)
            try:
                runner.atomic_publish_json(destination, first)
                published = output.read_bytes()
                self.assertEqual(json.loads(published), first)

                with self.assertRaisesRegex(FileExistsError, "already exists"):
                    runner.atomic_publish_json(
                        destination,
                        {"schema": runner.RESULT_SCHEMA, "value": 2},
                    )
            finally:
                destination.close()
            self.assertEqual(output.read_bytes(), published)
            self.assertFalse(list(root.glob(".result.json.*.tmp")))

            raced_output = root / "raced-result.json"
            original_link = runner.os.link

            def race_destination(source, destination, **kwargs):
                descriptor = os.open(
                    destination,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=kwargs["dst_dir_fd"],
                )
                try:
                    os.write(descriptor, b"racing writer\n")
                finally:
                    os.close(descriptor)
                return original_link(source, destination, **kwargs)

            destination = runner.open_result_output(raced_output)
            try:
                with mock.patch.object(
                    runner.os,
                    "link",
                    side_effect=race_destination,
                ), self.assertRaisesRegex(FileExistsError, "already exists"):
                    runner.atomic_publish_json(
                        destination,
                        {"schema": runner.RESULT_SCHEMA, "value": 3},
                    )
            finally:
                destination.close()
            self.assertEqual(raced_output.read_bytes(), b"racing writer\n")
            self.assertFalse(list(root.glob(".raced-result.json.*.tmp")))

    def test_fcitx_result_output_pins_parent_and_rejects_unsafe_parent(self) -> None:
        runner = load_fcitx_full_stack_runner()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            parent = root / "evidence"
            parent.mkdir()
            output = parent / "result.json"
            destination = runner.open_result_output(output)
            pinned_parent = root / "pinned-evidence"
            parent.rename(pinned_parent)
            parent.mkdir()
            try:
                runner.atomic_publish_json(
                    destination,
                    {"schema": runner.RESULT_SCHEMA, "value": "pinned"},
                )
            finally:
                destination.close()
            self.assertTrue((pinned_parent / "result.json").is_file())
            self.assertFalse((parent / "result.json").exists())
            with self.assertRaises(OSError):
                os.fstat(destination.parent_fd)

            unsafe = root / "unsafe"
            unsafe.mkdir()
            unsafe.chmod(0o777)
            with self.assertRaisesRegex(
                runner.ResultEvidenceError,
                "stable user-owned directory",
            ):
                runner.open_result_output(unsafe / "result.json")

    def test_fcitx_result_output_rolls_back_on_directory_fsync_failure(self) -> None:
        runner = load_fcitx_full_stack_runner()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            output = root / "result.json"
            destination = runner.open_result_output(output)
            original_fsync = runner.os.fsync
            calls = 0

            def fail_first_directory_sync(descriptor):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError(errno.EIO, "injected directory fsync failure")
                return original_fsync(descriptor)

            try:
                with mock.patch.object(
                    runner.os,
                    "fsync",
                    side_effect=fail_first_directory_sync,
                ), self.assertRaises(OSError):
                    runner.atomic_publish_json(
                        destination,
                        {"schema": runner.RESULT_SCHEMA, "value": "rollback"},
                    )
            finally:
                destination.close()
            self.assertFalse(output.exists())
            self.assertFalse(list(root.glob(".result.json.*.tmp")))

    def test_fcitx_result_output_fails_closed_on_temp_name_exhaustion(self) -> None:
        runner = load_fcitx_full_stack_runner()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            output = root / "result.json"
            collision = root / ".result.json.collision.tmp"
            collision.write_bytes(b"preexisting\n")
            destination = runner.open_result_output(output)
            try:
                with mock.patch.object(
                    runner.secrets,
                    "token_hex",
                    return_value="collision",
                ), self.assertRaisesRegex(
                    runner.ResultEvidenceError,
                    "allocate a private result temp file",
                ):
                    runner.atomic_publish_json(
                        destination,
                        {"schema": runner.RESULT_SCHEMA},
                    )
            finally:
                destination.close()
            self.assertFalse(output.exists())
            self.assertEqual(collision.read_bytes(), b"preexisting\n")

    def test_fcitx_result_output_rejects_late_symlink_destination(self) -> None:
        runner = load_fcitx_full_stack_runner()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            output = root / "result.json"
            destination = runner.open_result_output(output)
            original_link = runner.os.link

            def race_symlink(source, target, **kwargs):
                os.symlink(
                    "/definitely/not/touched",
                    target,
                    dir_fd=kwargs["dst_dir_fd"],
                )
                return original_link(source, target, **kwargs)

            try:
                with mock.patch.object(
                    runner.os,
                    "link",
                    side_effect=race_symlink,
                ), self.assertRaisesRegex(FileExistsError, "already exists"):
                    runner.atomic_publish_json(
                        destination,
                        {"schema": runner.RESULT_SCHEMA},
                    )
            finally:
                destination.close()
            self.assertTrue(output.is_symlink())
            self.assertEqual(os.readlink(output), "/definitely/not/touched")
            self.assertFalse(list(root.glob(".result.json.*.tmp")))

    def test_fcitx_native_result_publishes_only_after_final_cleanup(self) -> None:
        runner = load_fcitx_full_stack_runner()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            private_root = root / "private-test-root"
            private_root.mkdir()
            output = root / "result.json"
            args = argparse.Namespace(
                converter_backend="mozc",
                soak_iterations=7,
                cycles=1,
                timeout=90,
                result_output=output,
                product_source_ref="c" * 40,
                product_server_sha256="e" * 64,
                product_server_size=123,
            )
            observation = runner.CycleObservation(
                cycle=1,
                conversions=7,
                server_launches=(),
                server_identities=(),
                helper_launches=(),
                helper_identities=(),
                lock_owner_observed=True,
                max_concurrent_helpers=1,
                server_cleanup_ok=True,
                helper_cleanup_ok=True,
                process_group_cleanup_ok=True,
            )
            bindings = {
                "producer": {"path": "runner.py", "sha256": "a" * 64},
                "source": {
                    "repository_root": "/fixture",
                    "git_head": "b" * 40,
                    "worktree_clean": True,
                },
                "artifacts": {
                    "mozc_generation": {
                        "artifact_fingerprint": "sha256:" + "d" * 64,
                    }
                },
                "input_snapshot": {
                    "integrity": {"post_run_verified": True},
                    "entries": [
                        {
                            "input_id": "product_server",
                            "sha256": "e" * 64,
                            "size": 123,
                        }
                    ],
                },
                "runtime_integrity": {"post_run_verified": True},
            }
            destination = runner.open_result_output(output)
            try:
                with self.assertRaisesRegex(
                    runner.ResultEvidenceError,
                    "final residue",
                ):
                    runner.publish_success_result(
                        args,
                        private_root,
                        [observation],
                        bindings,
                        ["python3", "runner.py"],
                        destination,
                    )
                self.assertFalse(output.exists())

                private_root.rmdir()
                runner.publish_success_result(
                    args,
                    private_root,
                    [observation],
                    bindings,
                    ["python3", "runner.py"],
                    destination,
                )
            finally:
                destination.close()
            published = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(published["residue_count"], 0)
            self.assertEqual(published["conversions"], 7)

    def test_sidecar_remains_conversion_only_with_history_disabled(self) -> None:
        helper = MOZC_SIDECAR_HELPER_SOURCE.read_text(encoding="utf-8")
        b1_helper = MOZC_SIDECAR_B1_HELPER_SOURCE.read_text(encoding="utf-8")
        root_proto = MOZC_SIDECAR_PROTO.read_bytes()
        overlay_proto = MOZC_SIDECAR_OVERLAY_PROTO.read_bytes()
        b1_overlay_proto = MOZC_SIDECAR_B1_OVERLAY_PROTO.read_bytes()
        self.assertEqual(
            overlay_proto,
            root_proto,
            "the fixed helper build input must match the runtime-side protocol",
        )
        self.assertEqual(
            b1_overlay_proto,
            root_proto,
            "the B1 helper must use the unchanged private sidecar protocol",
        )
        sidecar_proto = overlay_proto.decode("utf-8")

        for source in (helper, b1_helper):
            self.assertIn("options.enable_user_history_for_conversion = false;", source)
            self.assertIn("options.incognito_mode = true;", source)
        self.assertEqual(
            re.findall(
                r"^\s*(OPERATION_[A-Z_]+)\s*=",
                sidecar_proto,
                flags=re.MULTILINE,
            ),
            ["OPERATION_UNSPECIFIED", "OPERATION_CONVERT", "OPERATION_PING"],
        )

    @staticmethod
    def _elf_helper(
        *,
        machine: int = 62,
        interpreter: str = ELF_INTERPRETER,
        glibc_version: str = GLIBC_VERSION,
        glibcxx_version: str = GLIBCXX_VERSION,
        cxxabi_version: str = CXXABI_VERSION,
        marker: bytes = b"fixed helper fixture",
    ) -> bytes:
        strings = bytearray(b"\0")

        def add_string(value: str) -> int:
            offset = len(strings)
            strings.extend(value.encode("ascii") + b"\0")
            return offset

        libc_name = add_string("libc.so.6")
        glibc_name = add_string(glibc_version)
        libstdcxx_name = add_string("libstdc++.so.6")
        glibcxx_name = add_string(glibcxx_version)
        cxxabi_name = add_string(cxxabi_version)

        requirements = b"".join(
            (
                struct.pack("<HHIII", 1, 1, libc_name, 16, 32),
                struct.pack("<IHHII", 0, 0, 0, glibc_name, 0),
                struct.pack("<HHIII", 1, 2, libstdcxx_name, 16, 0),
                struct.pack("<IHHII", 0, 0, 0, glibcxx_name, 16),
                struct.pack("<IHHII", 0, 0, 0, cxxabi_name, 0),
            )
        )
        interpreter_bytes = interpreter.encode("ascii") + b"\0"
        program_offset = 64
        interpreter_offset = program_offset + 56
        string_offset = interpreter_offset + len(interpreter_bytes)
        requirement_offset = string_offset + len(strings)
        section_offset = (requirement_offset + len(requirements) + 7) & ~7

        ident = bytearray(16)
        ident[:4] = b"\x7fELF"
        ident[4] = 2  # ELFCLASS64
        ident[5] = 1  # ELFDATA2LSB
        ident[6] = 1  # EV_CURRENT
        header = bytes(ident) + struct.pack(
            "<HHIQQQIHHHHHH",
            3,
            machine,
            1,
            0,
            program_offset,
            section_offset,
            0,
            64,
            56,
            1,
            64,
            3,
            0,
        )
        program_header = struct.pack(
            "<IIQQQQQQ",
            3,
            4,
            interpreter_offset,
            0,
            0,
            len(interpreter_bytes),
            len(interpreter_bytes),
            1,
        )
        padding = b"\0" * (
            section_offset - requirement_offset - len(requirements)
        )
        null_section = bytes(64)
        string_section = struct.pack(
            "<IIQQQQIIQQ",
            0,
            3,
            0,
            0,
            string_offset,
            len(strings),
            0,
            0,
            1,
            0,
        )
        requirement_section = struct.pack(
            "<IIQQQQIIQQ",
            0,
            0x6FFFFFFE,
            0,
            0,
            requirement_offset,
            len(requirements),
            1,
            2,
            4,
            16,
        )
        return b"".join(
            (
                header,
                program_header,
                interpreter_bytes,
                bytes(strings),
                requirements,
                padding,
                null_section,
                string_section,
                requirement_section,
                marker,
                b"\n",
            )
        )

    @staticmethod
    def _write_bundle(root: Path) -> tuple[Path, bytes, bytes]:
        bundle = root / "bundle"
        bundle.mkdir()
        helper = MozcArtifactBundleContractTests._elf_helper()
        data = b"fixed Mozc OSS data fixture\n"
        helper_path = bundle / "fcitx5-grimodex-mozc-helper"
        helper_path.write_bytes(helper)
        helper_path.chmod(0o755)
        (bundle / "mozc.data").write_bytes(data)
        license_dir = bundle / "licenses"
        license_dir.mkdir()
        licenses = {
            name: f"fixture {name}\n".encode()
            for name in MozcArtifactBundleContractTests.LICENSE_NAMES
        }
        for name, payload in licenses.items():
            (license_dir / name).write_bytes(payload)
        manifest = {
            "schema": "grimodex.mozc-artifact-bundle.v1",
            "target": {
                "system": "linux",
                "architecture": "x86_64",
                "elf": {
                    "class": 64,
                    "endianness": "little",
                    "machine": "EM_X86_64",
                },
                "runtime": {
                    "interpreter": MozcArtifactBundleContractTests.ELF_INTERPRETER,
                    "required_symbol_versions": {
                        "glibc": MozcArtifactBundleContractTests.GLIBC_VERSION,
                        "glibcxx": MozcArtifactBundleContractTests.GLIBCXX_VERSION,
                        "cxxabi": MozcArtifactBundleContractTests.CXXABI_VERSION,
                    },
                },
            },
            "source": {
                "repository": "https://github.com/Masterisk-F/fcitx-mozkey",
                "revision": MozcArtifactBundleContractTests.SOURCE_REVISION,
                "tree": MozcArtifactBundleContractTests.SOURCE_TREE,
                "bazel_version": "9.0.2",
                "bazeliskrc_sha256": MozcArtifactBundleContractTests.BAZELISKRC_SHA256,
                "module_lock_sha256": MozcArtifactBundleContractTests.MODULE_LOCK_SHA256,
                "overlay_sha256": MozcArtifactBundleContractTests.OVERLAY_SHA256,
            },
            "artifacts": {
                "fcitx5-grimodex-mozc-helper": {
                    "sha256": hashlib.sha256(helper).hexdigest(),
                    "size": len(helper),
                },
                "mozc.data": {
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "size": len(data),
                },
            },
            "licenses": {
                name: {
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "size": len(payload),
                }
                for name, payload in licenses.items()
            },
        }
        (bundle / "manifest.json").write_text(
            json.dumps(manifest),
            encoding="utf-8",
        )
        return bundle, helper, data

    @staticmethod
    def _load_verifier_for_fixture(bundle: Path):
        spec = importlib.util.spec_from_file_location(
            "mozc_artifact_verifier_fixture",
            MOZC_ARTIFACT_VERIFIER,
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        data = (bundle / "mozc.data").read_bytes()
        module.FIXED_DATA_SIZE = len(data)
        module.FIXED_DATA_SHA256 = hashlib.sha256(data).hexdigest()
        helper = (bundle / "fcitx5-grimodex-mozc-helper").read_bytes()
        module.FIXED_HELPER_SIZE = len(helper)
        module.FIXED_HELPER_SHA256 = hashlib.sha256(helper).hexdigest()
        module.LICENSE_HASHES = {
            name: hashlib.sha256((bundle / "licenses" / name).read_bytes()).hexdigest()
            for name in MozcArtifactBundleContractTests.LICENSE_NAMES
        }
        return module

    @staticmethod
    def _load_builder():
        spec = importlib.util.spec_from_file_location(
            "mozc_artifact_builder_fixture",
            MOZC_ARTIFACT_BUILDER,
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def _verify(bundle: Path, stage: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "python3",
                str(MOZC_ARTIFACT_VERIFIER),
                "--bundle",
                str(bundle),
                "--stage-root",
                str(stage),
            ],
            capture_output=True,
            text=True,
        )

    def test_verifier_stages_only_hash_matched_fixed_source_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            bundle, helper, data = self._write_bundle(root)
            stage = root / "stage"

            verifier = self._load_verifier_for_fixture(bundle)
            generation = verifier.verify_and_stage(bundle, stage)

            self.assertEqual(generation.parent, stage.resolve())
            self.assertRegex(generation.name, r"^sha256-[0-9a-f]{64}$")
            self.assertEqual(
                (generation / "fcitx5-grimodex-mozc-helper").read_bytes(),
                helper,
            )
            self.assertEqual((generation / "mozc.data").read_bytes(), data)
            self.assertTrue(
                (generation / "fcitx5-grimodex-mozc-helper").stat().st_mode
                & 0o111
            )
            self.assertEqual(
                {path.name for path in (generation / "licenses").iterdir()},
                set(self.LICENSE_NAMES),
            )

            helper_inode = (generation / "fcitx5-grimodex-mozc-helper").stat().st_ino
            repeated = verifier.verify_and_stage(bundle, stage)
            self.assertEqual(repeated, generation)
            self.assertEqual(
                (repeated / "fcitx5-grimodex-mozc-helper").stat().st_ino,
                helper_inode,
            )
            verifier.verify_staged_generation(generation)
            ping_payload = (
                b"\x08\x01\x10\x01\x18\x01\x3a\x40"
                + verifier.FIXED_DATA_SHA256.encode("ascii")
            )
            ping_result = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=struct.pack(">I", len(ping_payload)) + ping_payload,
                stderr=b"",
            )
            with mock.patch.object(
                verifier.subprocess,
                "run",
                return_value=ping_result,
            ):
                self.assertEqual(
                    verifier.verify_host_runtime(generation),
                    generation.name,
                )
            failed_ping = subprocess.CompletedProcess(
                args=[],
                returncode=127,
                stdout=b"",
                stderr=b"missing runtime",
            )
            with mock.patch.object(
                verifier.subprocess,
                "run",
                return_value=failed_ping,
            ), self.assertRaisesRegex(
                verifier.BundleVerificationError,
                "GLIBC_2.38",
            ):
                verifier.verify_host_runtime(generation)

            staged_helper = generation / "fcitx5-grimodex-mozc-helper"
            staged_helper.chmod(0o644)
            staged_helper.write_bytes(self._elf_helper(marker=b"tampered stage"))
            with self.assertRaisesRegex(
                verifier.BundleVerificationError,
                "staged artifact",
            ):
                verifier.verify_and_stage(bundle, stage)
            with mock.patch.object(
                verifier,
                "_require_linux_x86_64_elf",
                side_effect=AssertionError("ELF parser ran before identity check"),
            ) as elf_parser, self.assertRaisesRegex(
                verifier.BundleVerificationError,
                "staged artifact",
            ):
                verifier.verify_staged_generation(generation)
            elf_parser.assert_not_called()

    def test_installed_runtime_verifier_pings_hash_matched_private_copies(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            bundle, _, _ = self._write_bundle(root)
            verifier = self._load_verifier_for_fixture(bundle)
            helper = bundle / "fcitx5-grimodex-mozc-helper"
            data = bundle / "mozc.data"
            ping_payload = (
                b"\x08\x01\x10\x01\x18\x01\x3a\x40"
                + verifier.FIXED_DATA_SHA256.encode("ascii")
            )
            ping_result = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=struct.pack(">I", len(ping_payload)) + ping_payload,
                stderr=b"",
            )

            with mock.patch.object(
                verifier.subprocess,
                "run",
                return_value=ping_result,
            ) as runtime:
                self.assertEqual(
                    verifier.verify_installed_runtime(helper, data),
                    verifier.FIXED_DATA_SHA256,
                )
            command = runtime.call_args.args[0]
            self.assertNotEqual(command[0], str(helper))
            self.assertNotEqual(command[1], f"--data_file={data}")

            data.write_bytes(b"x" * verifier.FIXED_DATA_SIZE)
            with mock.patch.object(
                verifier,
                "_require_linux_x86_64_elf",
                side_effect=AssertionError("ELF parser ran before data identity check"),
            ) as elf_parser, mock.patch.object(
                verifier.subprocess,
                "run",
            ) as runtime, self.assertRaisesRegex(
                verifier.BundleVerificationError,
                "installed artifact identity mismatch",
            ):
                verifier.verify_installed_runtime(helper, data)
            elf_parser.assert_not_called()
            runtime.assert_not_called()

    def test_prepared_runtime_survives_source_replacement_and_fails_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            bundle, helper_bytes, _ = self._write_bundle(root)
            verifier = self._load_verifier_for_fixture(bundle)
            helper = bundle / "fcitx5-grimodex-mozc-helper"
            data = bundle / "mozc.data"
            runtime_root = root / "runtime"
            ping_payload = (
                b"\x08\x01\x10\x01\x18\x01\x3a\x40"
                + verifier.FIXED_DATA_SHA256.encode("ascii")
            )
            ping_result = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=struct.pack(">I", len(ping_payload)) + ping_payload,
                stderr=b"",
            )

            with mock.patch.object(
                verifier.subprocess,
                "run",
                return_value=ping_result,
            ):
                generation = verifier.prepare_installed_runtime(
                    helper,
                    data,
                    runtime_root,
                )
                helper_inode = (
                    generation / "fcitx5-grimodex-mozc-helper"
                ).stat().st_ino
                helper.write_bytes(b"replaced after preparation")
                helper.chmod(0o755)
                repeated = verifier.prepare_installed_runtime(
                    helper,
                    data,
                    runtime_root,
                )

            self.assertEqual(repeated, generation)
            self.assertEqual(
                (generation / "fcitx5-grimodex-mozc-helper").read_bytes(),
                helper_bytes,
            )
            self.assertEqual(
                (generation / "fcitx5-grimodex-mozc-helper").stat().st_ino,
                helper_inode,
            )
            self.assertEqual(stat.S_IMODE(runtime_root.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(generation.stat().st_mode), 0o555)

            generation.chmod(0o755)
            prepared_helper = generation / "fcitx5-grimodex-mozc-helper"
            prepared_helper.chmod(0o755)
            prepared_helper.write_bytes(b"tampered prepared helper")
            generation.chmod(0o555)
            with self.assertRaisesRegex(
                verifier.BundleVerificationError,
                "prepared runtime artifact",
            ):
                verifier.prepare_installed_runtime(helper, data, runtime_root)

    def test_verifier_fails_closed_on_identity_and_machine_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            bundle, _, _ = self._write_bundle(root)
            verifier = self._load_verifier_for_fixture(bundle)
            raw_mismatch_root = root / "raw-mismatch"
            raw_mismatch_root.mkdir()
            raw_mismatch_bundle, raw_helper, _ = self._write_bundle(
                raw_mismatch_root
            )
            raw_verifier = self._load_verifier_for_fixture(raw_mismatch_bundle)
            raw_helper_path = (
                raw_mismatch_bundle / "fcitx5-grimodex-mozc-helper"
            )
            mutated_helper = bytearray(raw_helper)
            mutated_helper[-1] ^= 0x01
            raw_helper_path.write_bytes(mutated_helper)
            raw_helper_path.chmod(0o755)
            with mock.patch.object(
                raw_verifier,
                "_require_linux_x86_64_elf",
                side_effect=AssertionError("ELF parser ran before identity check"),
            ) as elf_parser, self.assertRaisesRegex(
                raw_verifier.BundleVerificationError,
                "SHA-256 mismatch",
            ):
                raw_verifier.verify_and_stage(
                    raw_mismatch_bundle,
                    raw_mismatch_root / "stage",
                )
            elf_parser.assert_not_called()

            helper_path = bundle / "fcitx5-grimodex-mozc-helper"
            arbitrary_helper = self._elf_helper(marker=b"self-consistent arbitrary")
            helper_path.write_bytes(arbitrary_helper)
            helper_path.chmod(0o755)
            manifest_path = bundle / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            helper_entry = manifest["artifacts"]["fcitx5-grimodex-mozc-helper"]
            helper_entry["size"] = len(arbitrary_helper)
            helper_entry["sha256"] = hashlib.sha256(arbitrary_helper).hexdigest()
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            stage = root / "stage"

            with self.assertRaisesRegex(
                verifier.BundleVerificationError,
                "helper identity",
            ):
                verifier.verify_and_stage(bundle, stage)
            self.assertFalse(stage.exists())

            wrong_root = root / "wrong-machine"
            wrong_root.mkdir()
            wrong_bundle, _, _ = self._write_bundle(wrong_root)
            wrong_helper = self._elf_helper(machine=183)
            wrong_helper_path = wrong_bundle / "fcitx5-grimodex-mozc-helper"
            wrong_helper_path.write_bytes(wrong_helper)
            wrong_helper_path.chmod(0o755)
            wrong_manifest_path = wrong_bundle / "manifest.json"
            wrong_manifest = json.loads(
                wrong_manifest_path.read_text(encoding="utf-8")
            )
            wrong_entry = wrong_manifest["artifacts"][
                "fcitx5-grimodex-mozc-helper"
            ]
            wrong_entry["size"] = len(wrong_helper)
            wrong_entry["sha256"] = hashlib.sha256(wrong_helper).hexdigest()
            wrong_manifest_path.write_text(
                json.dumps(wrong_manifest),
                encoding="utf-8",
            )
            wrong_verifier = self._load_verifier_for_fixture(wrong_bundle)
            with self.assertRaisesRegex(
                wrong_verifier.BundleVerificationError,
                "EM_X86_64",
            ):
                wrong_verifier.verify_and_stage(
                    wrong_bundle,
                    wrong_root / "stage",
                )

            runtime_variants = {
                "interpreter": self._elf_helper(interpreter="/lib64/ld-linux.so.2"),
                "symbol-version": self._elf_helper(glibc_version="GLIBC_2.39"),
            }
            for label, runtime_helper in runtime_variants.items():
                with self.subTest(runtime=label):
                    runtime_root = root / f"wrong-runtime-{label}"
                    runtime_root.mkdir()
                    runtime_bundle, _, _ = self._write_bundle(runtime_root)
                    runtime_helper_path = (
                        runtime_bundle / "fcitx5-grimodex-mozc-helper"
                    )
                    runtime_helper_path.write_bytes(runtime_helper)
                    runtime_helper_path.chmod(0o755)
                    runtime_manifest_path = runtime_bundle / "manifest.json"
                    runtime_manifest = json.loads(
                        runtime_manifest_path.read_text(encoding="utf-8")
                    )
                    runtime_entry = runtime_manifest["artifacts"][
                        "fcitx5-grimodex-mozc-helper"
                    ]
                    runtime_entry["size"] = len(runtime_helper)
                    runtime_entry["sha256"] = hashlib.sha256(
                        runtime_helper
                    ).hexdigest()
                    runtime_manifest_path.write_text(
                        json.dumps(runtime_manifest),
                        encoding="utf-8",
                    )
                    runtime_verifier = self._load_verifier_for_fixture(
                        runtime_bundle
                    )
                    with self.assertRaisesRegex(
                        runtime_verifier.BundleVerificationError,
                        "runtime ABI",
                    ):
                        runtime_verifier.verify_and_stage(
                            runtime_bundle,
                            runtime_root / "stage",
                        )

            pathological_helper = bytearray(self._elf_helper())
            section_offset = struct.unpack_from("<Q", pathological_helper, 40)[0]
            requirement_section = section_offset + 2 * 64
            requirement_offset = struct.unpack_from(
                "<Q", pathological_helper, requirement_section + 24
            )[0]
            struct.pack_into("<H", pathological_helper, requirement_offset + 2, 0xffff)
            builder = self._load_builder()
            for parser, error_type, arguments in (
                (
                    verifier._inspect_elf_runtime_contract,
                    verifier.BundleVerificationError,
                    (bytes(pathological_helper), "pathological-helper"),
                ),
                (
                    builder._inspect_elf_runtime_contract,
                    builder.BuildError,
                    (bytes(pathological_helper),),
                ),
            ):
                with self.subTest(parser=parser.__module__), self.assertRaisesRegex(
                    error_type,
                    "work budget",
                ):
                    parser(*arguments)

    def test_verifier_rejects_unmanifested_bundle_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            bundle, _, _ = self._write_bundle(root)
            (bundle / "unlocked-dictionary.tsv").write_text("mutable input")

            result = self._verify(bundle, root / "stage")

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unknown=unlocked-dictionary.tsv", result.stderr)

    def test_verifier_rejects_a_different_source_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            bundle, _, _ = self._write_bundle(root)
            manifest_path = bundle / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["source"]["revision"] = "0" * 40
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            result = self._verify(bundle, root / "stage")

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unexpected source revision", result.stderr)

    def test_verifier_rejects_an_oversized_helper_before_copying(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            bundle, _, _ = self._write_bundle(root)
            verifier = self._load_verifier_for_fixture(bundle)
            manifest_path = bundle / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["artifacts"]["fcitx5-grimodex-mozc-helper"]["size"] = (
                verifier.MAX_HELPER_BYTES + 1
            )
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaisesRegex(verifier.BundleVerificationError, "helper identity"):
                verifier.verify_and_stage(bundle, root / "stage")
            self.assertFalse((root / "stage").exists())

    def test_cmake_import_is_disabled_by_default_and_installs_private_paths(self) -> None:
        cmake = SERVER_CMAKE.read_text(encoding="utf-8")

        self.assertIn('set(HAZKEY_SERVER_MOZC_ARTIFACT_DIR "" CACHE PATH', cmake)
        self.assertIn("verified-mozc-artifacts", cmake)
        self.assertIn('--stage-root "${_HAZKEY_SERVER_MOZC_STAGING_ROOT}"', cmake)
        self.assertGreaterEqual(cmake.count("--verify-host-runtime"), 2)
        for requirement in (
            "/lib64/ld-linux-x86-64.so.2",
            "GLIBC_2.38",
            "GLIBCXX_3.4.32",
            "CXXABI_1.3.15",
        ):
            self.assertIn(requirement, cmake)
        self.assertIn('CMAKE_SYSTEM_NAME STREQUAL "Linux"', cmake)
        self.assertIn('CMAKE_SYSTEM_PROCESSOR STREQUAL "x86_64"', cmake)
        self.assertLess(cmake.index("install(CODE"), cmake.index("# install hazkey-server"))
        self.assertIn("fcitx5-grimodex-mozc-helper", cmake)
        self.assertIn("${CMAKE_INSTALL_FULL_DATADIR}/fcitx5-grimodex/mozc", cmake)
        for manifest_path in (INSTALL_MANIFEST, UNINSTALL_MANIFEST):
            self.assertIn(
                "optional:/usr/lib/{,*/}fcitx5-grimodex/"
                "fcitx5-grimodex-mozc-helper",
                manifest_path.read_text(encoding="utf-8"),
            )

    def test_mozc_runtime_script_is_an_exact_and_isolated_opt_in(self) -> None:
        script = MOZC_RUNTIME_SCRIPT.read_text(encoding="utf-8")
        default_script = DEFAULT_RUNTIME_SCRIPT.read_text(encoding="utf-8")
        top_level_cmake = TOP_LEVEL_CMAKE.read_text(encoding="utf-8")
        syntax = subprocess.run(
            ["bash", "-n", str(MOZC_RUNTIME_SCRIPT)],
            capture_output=True,
            text=True,
        )

        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        help_result = subprocess.run(
            [str(MOZC_RUNTIME_SCRIPT), "--help"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn(
            "Usage: scripts/grimodex-ime_mozc.sh <command>", help_result.stdout
        )

        self.assertIn(
            'BUILD_DIR=${BUILD_DIR:-"${REPO_ROOT}/build-grimodex-mozc"}', script
        )
        self.assertIn(
            '-DHAZKEY_SERVER_MOZC_ARTIFACT_DIR="${artifact_dir}"', script
        )
        self.assertIn("export FCITX5_GRIMODEX_CONVERTER=mozc", script)
        self.assertIn("--prepare-installed-runtime", script)
        self.assertIn("fcitx5-grimodex-mozc-helper", script)
        self.assertIn("fcitx5-grimodex/mozc/mozc.data", script)
        self.assertIn("elif [[ ${prefix} == / ]]", script)
        for directory in ("BINDIR", "LIBDIR", "DATADIR"):
            self.assertIn(f"GRIMODEX_INSTALL_FULL_{directory}", top_level_cmake)
        self.assertLess(
            script.index("--prepare-installed-runtime"),
            script.index('nohup "${SERVER}" --replace'),
        )
        self.assertLess(
            script.index("export FCITX5_GRIMODEX_CONVERTER=mozc"),
            script.index('nohup "${SERVER}" --replace'),
        )
        self.assertLess(
            script.index("export FCITX5_GRIMODEX_CONVERTER=mozc"),
            script.index("fcitx5 -rd"),
        )
        self.assertNotIn("FCITX5_GRIMODEX_CONVERTER=mozc", default_script)

        with tempfile.TemporaryDirectory() as temporary_directory:
            environment = os.environ.copy()
            environment["BUILD_DIR"] = temporary_directory
            environment.pop("MOZC_ARTIFACT_DIR", None)
            environment.pop("HAZKEY_SERVER_MOZC_ARTIFACT_DIR", None)
            missing_bundle = subprocess.run(
                [str(MOZC_RUNTIME_SCRIPT), "build"],
                env=environment,
                capture_output=True,
                text=True,
            )
        self.assertEqual(missing_bundle.returncode, 2)
        self.assertIn("Mozc artifact bundle is required", missing_bundle.stderr)

    def test_mozc_runtime_script_propagates_paths_and_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            build_dir = root / "build"
            install_prefix = root / "install root"
            command_dir = root / "commands"
            config_home = root / "config"
            build_dir.mkdir()
            command_dir.mkdir()
            config_home.mkdir()

            server = install_prefix / "custom-bin/fcitx5-grimodex-server"
            helper = (
                install_prefix
                / "lib/x86_64-linux-gnu/fcitx5-grimodex/"
                "fcitx5-grimodex-mozc-helper"
            )
            data = install_prefix / "share/fcitx5-grimodex/mozc/mozc.data"
            server.parent.mkdir(parents=True)
            helper.parent.mkdir(parents=True)
            data.parent.mkdir(parents=True)
            helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            helper.chmod(0o755)
            data.write_bytes(b"fixed test data")

            server.write_text(
                """#!/usr/bin/env bash
{
    printf 'CONVERTER=%s\\n' "${FCITX5_GRIMODEX_CONVERTER:-}"
    printf 'HELPER=%s\\n' "${FCITX5_GRIMODEX_MOZC_HELPER:-}"
    printf 'DATA=%s\\n' "${FCITX5_GRIMODEX_MOZC_DATA:-}"
    printf 'ARGS=%s\\n' "$*"
    printf 'PID=%s\\n' "$$"
} >"${MOCK_SERVER_ENV_LOG:?}"
exec /bin/sleep 30
""",
                encoding="utf-8",
            )
            server.chmod(0o755)

            fcitx = command_dir / "fcitx5"
            fcitx.write_text(
                """#!/usr/bin/env bash
{
    printf 'CONVERTER=%s\\n' "${FCITX5_GRIMODEX_CONVERTER:-}"
    printf 'HELPER=%s\\n' "${FCITX5_GRIMODEX_MOZC_HELPER:-}"
    printf 'DATA=%s\\n' "${FCITX5_GRIMODEX_MOZC_DATA:-}"
    printf 'ARGS=%s\\n' "$*"
} >"${MOCK_FCITX_ENV_LOG:?}"
""",
                encoding="utf-8",
            )
            remote = command_dir / "fcitx5-remote"
            remote.write_text(
                """#!/usr/bin/env bash
if [[ ${1:-} == -n ]]; then
    printf 'grimodex\\n'
fi
""",
                encoding="utf-8",
            )
            short_sleep = command_dir / "sleep"
            short_sleep.write_text(
                "#!/usr/bin/env bash\nexec /bin/sleep 0.1\n", encoding="utf-8"
            )
            for command in (fcitx, remote, short_sleep):
                command.chmod(0o755)

            runtime_verifier = root / "runtime-verifier.py"
            runtime_verifier.write_text(
                """import json
import os
from pathlib import Path
import shutil
import sys

with open(os.environ["MOCK_VERIFIER_LOG"], "w", encoding="utf-8") as output:
    json.dump(sys.argv[1:], output)
arguments = sys.argv[1:]
source_helper = Path(arguments[arguments.index("--helper") + 1])
source_data = Path(arguments[arguments.index("--data") + 1])
generation = Path(arguments[arguments.index("--runtime-root") + 1]) / "sha256-test-runtime"
generation.mkdir(parents=True)
shutil.copyfile(source_helper, generation / "fcitx5-grimodex-mozc-helper")
shutil.copyfile(source_data, generation / "mozc.data")
(generation / "fcitx5-grimodex-mozc-helper").chmod(0o555)
(generation / "mozc.data").chmod(0o444)
source_helper.write_text("replaced after preparation\\n", encoding="utf-8")
print(generation)
""",
                encoding="utf-8",
            )

            (build_dir / "CMakeCache.txt").write_text(
                "\n".join(
                    (
                        f"CMAKE_INSTALL_PREFIX:PATH={install_prefix}",
                        "CMAKE_INSTALL_BINDIR:PATH=custom-bin",
                        "CMAKE_INSTALL_LIBDIR:PATH=lib/x86_64-linux-gnu",
                        "CMAKE_INSTALL_DATAROOTDIR:PATH=share",
                        "CMAKE_INSTALL_DATADIR:PATH=",
                        f"GRIMODEX_INSTALL_FULL_BINDIR:INTERNAL={server.parent}",
                        f"GRIMODEX_INSTALL_FULL_LIBDIR:INTERNAL={install_prefix / 'lib/x86_64-linux-gnu'}",
                        f"GRIMODEX_INSTALL_FULL_DATADIR:INTERNAL={install_prefix / 'share'}",
                    )
                )
                + "\n",
                encoding="utf-8",
            )

            server_environment_log = root / "server.env"
            fcitx_environment_log = root / "fcitx.env"
            verifier_log = root / "verifier.json"
            runtime_root = root / "runtime"
            environment = os.environ.copy()
            for name in (
                "GRIMODEX_SERVER",
                "FCITX5_GRIMODEX_MOZC_HELPER",
                "FCITX5_GRIMODEX_MOZC_DATA",
                "INSTALL_PREFIX",
            ):
                environment.pop(name, None)
            environment.update(
                {
                    "BUILD_DIR": str(build_dir),
                    "PATH": f"{command_dir}{os.pathsep}{environment['PATH']}",
                    "XDG_CONFIG_HOME": str(config_home),
                    "GRIMODEX_RESTART_LOG": str(root / "restart.log"),
                    "GRIMODEX_MOZC_VERIFIER": str(runtime_verifier),
                    "GRIMODEX_MOZC_RUNTIME_ROOT": str(runtime_root),
                    "PYTHON3": sys.executable,
                    "MOCK_SERVER_ENV_LOG": str(server_environment_log),
                    "MOCK_FCITX_ENV_LOG": str(fcitx_environment_log),
                    "MOCK_VERIFIER_LOG": str(verifier_log),
                }
            )

            result = subprocess.run(
                [str(MOZC_RUNTIME_SCRIPT), "restart"],
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
            )
            server_environment = dict(
                line.split("=", 1)
                for line in server_environment_log.read_text(
                    encoding="utf-8"
                ).splitlines()
            )
            fcitx_environment = dict(
                line.split("=", 1)
                for line in fcitx_environment_log.read_text(
                    encoding="utf-8"
                ).splitlines()
            )
            verifier_arguments = json.loads(verifier_log.read_text(encoding="utf-8"))
            try:
                os.kill(int(server_environment["PID"]), signal.SIGTERM)
            except ProcessLookupError:
                pass

            self.assertEqual(result.returncode, 0, result.stderr)
            prepared_helper = (
                runtime_root
                / "sha256-test-runtime/fcitx5-grimodex-mozc-helper"
            )
            prepared_data = runtime_root / "sha256-test-runtime/mozc.data"
            expected_environment = {
                "CONVERTER": "mozc",
                "HELPER": str(prepared_helper),
                "DATA": str(prepared_data),
            }
            for name, value in expected_environment.items():
                self.assertEqual(server_environment[name], value)
                self.assertEqual(fcitx_environment[name], value)
            self.assertEqual(server_environment["ARGS"], "--replace")
            self.assertEqual(fcitx_environment["ARGS"], "-rd")
            self.assertEqual(
                verifier_arguments,
                [
                    "--prepare-installed-runtime",
                    "--helper",
                    str(helper),
                    "--data",
                    str(data),
                    "--runtime-root",
                    str(runtime_root),
                ],
            )
            self.assertEqual(
                helper.read_text(encoding="utf-8"),
                "replaced after preparation\n",
            )
            self.assertEqual(
                prepared_helper.read_text(encoding="utf-8"),
                "#!/bin/sh\nexit 0\n",
            )
            self.assertIn("Requested converter backend: mozc", result.stdout)

    def test_mozc_runtime_script_rejects_an_early_server_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            command_dir = root / "commands"
            config_home = root / "config"
            command_dir.mkdir()
            config_home.mkdir()
            for name in ("fcitx5", "fcitx5-remote"):
                (command_dir / name).symlink_to("/bin/true")
            short_sleep = command_dir / "sleep"
            short_sleep.write_text(
                "#!/usr/bin/env bash\nexec /bin/sleep 0.1\n", encoding="utf-8"
            )
            short_sleep.chmod(0o755)
            helper = root / "fcitx5-grimodex-mozc-helper"
            helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            helper.chmod(0o755)
            data = root / "mozc.data"
            data.write_bytes(b"fixed test data")
            runtime_verifier = root / "runtime-verifier.py"
            runtime_verifier.write_text(
                """from pathlib import Path
import shutil
import sys

arguments = sys.argv[1:]
source_helper = Path(arguments[arguments.index("--helper") + 1])
source_data = Path(arguments[arguments.index("--data") + 1])
generation = Path(arguments[arguments.index("--runtime-root") + 1]) / "prepared"
generation.mkdir(parents=True)
shutil.copyfile(source_helper, generation / "fcitx5-grimodex-mozc-helper")
shutil.copyfile(source_data, generation / "mozc.data")
(generation / "fcitx5-grimodex-mozc-helper").chmod(0o555)
(generation / "mozc.data").chmod(0o444)
print(generation)
""",
                encoding="utf-8",
            )

            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": f"{command_dir}{os.pathsep}{environment['PATH']}",
                    "XDG_CONFIG_HOME": str(config_home),
                    "GRIMODEX_SERVER": "/bin/false",
                    "FCITX5_GRIMODEX_MOZC_HELPER": str(helper),
                    "FCITX5_GRIMODEX_MOZC_DATA": str(data),
                    "GRIMODEX_RESTART_LOG": str(root / "restart.log"),
                    "GRIMODEX_MOZC_VERIFIER": str(runtime_verifier),
                    "GRIMODEX_MOZC_RUNTIME_ROOT": str(root / "runtime"),
                    "PYTHON3": sys.executable,
                }
            )
            result = subprocess.run(
                [str(MOZC_RUNTIME_SCRIPT), "restart"],
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("Mozc server exited during startup", result.stderr)
            self.assertNotIn("Requested converter backend", result.stdout)

    def test_builder_contract_matches_the_import_verifier(self) -> None:
        builder = self._load_builder()
        verifier_spec = importlib.util.spec_from_file_location(
            "mozc_artifact_verifier_contract",
            MOZC_ARTIFACT_VERIFIER,
        )
        assert verifier_spec is not None and verifier_spec.loader is not None
        verifier = importlib.util.module_from_spec(verifier_spec)
        verifier_spec.loader.exec_module(verifier)

        for name in (
            "SCHEMA",
            "SOURCE_REPOSITORY",
            "SOURCE_REVISION",
            "SOURCE_TREE",
            "BAZEL_VERSION",
            "BAZELISKRC_SHA256",
            "MODULE_LOCK_SHA256",
            "OVERLAY_SHA256",
            "TARGET_CONTRACT",
            "FIXED_HELPER_SIZE",
            "FIXED_HELPER_SHA256",
            "FIXED_DATA_SIZE",
            "FIXED_DATA_SHA256",
            "LICENSE_HASHES",
            "MAX_HELPER_BYTES",
            "MAX_LICENSE_BYTES",
        ):
            with self.subTest(name=name):
                self.assertEqual(getattr(builder, name), getattr(verifier, name))
        self.assertEqual(set(builder.LICENSE_LOCATIONS), set(self.LICENSE_NAMES))
        self.assertEqual(
            builder.LICENSE_LOCATIONS["FCITX-MOZKEY-THIRD-PARTY-NOTICES.md"],
            ("source", Path("THIRD_PARTY_NOTICES.md")),
        )
        builder.verify_overlay()

        self.assertEqual(builder.PROFILE_NAMES, ("b0", "b1"))
        self.assertEqual(verifier.PROFILE_NAMES, ("b0", "b1"))
        for name in (
            "B1_OVERLAY_SHA256",
            "B1_FIXED_HELPER_SIZE",
            "B1_FIXED_HELPER_SHA256",
        ):
            with self.subTest(name=name):
                self.assertEqual(getattr(builder, name), getattr(verifier, name))

        builder.activate_profile("b1")
        verifier.activate_profile("b1")
        self.assertEqual(builder.OVERLAY_DIRECTORY.name, "grimodex_mozc_sidecar_b1")
        self.assertEqual(builder.OVERLAY_FILES, builder.B1_OVERLAY_FILES)
        self.assertEqual(builder.HELPER_OUTPUT, builder.B1_HELPER_OUTPUT)
        self.assertEqual(builder.BUILD_TARGETS, builder.B1_BUILD_TARGETS)
        self.assertEqual(builder.TEST_TARGETS, builder.B1_TEST_TARGETS)
        self.assertEqual(builder.OVERLAY_SHA256, verifier.OVERLAY_SHA256)
        self.assertEqual(builder.FIXED_HELPER_SIZE, verifier.FIXED_HELPER_SIZE)
        self.assertEqual(builder.FIXED_HELPER_SHA256, verifier.FIXED_HELPER_SHA256)
        builder.verify_overlay()

        builder.activate_profile("b0")
        verifier.activate_profile("b0")
        self.assertEqual(builder.OVERLAY_DIRECTORY.name, "grimodex_mozc_sidecar")
        self.assertEqual(builder.OVERLAY_SHA256, self.OVERLAY_SHA256)
        self.assertEqual(verifier.OVERLAY_SHA256, self.OVERLAY_SHA256)

    def test_b1_artifact_acceptance_requires_explicit_profile_selection(self) -> None:
        builder = self._load_builder()
        verifier_spec = importlib.util.spec_from_file_location(
            "mozc_artifact_verifier_profiles",
            MOZC_ARTIFACT_VERIFIER,
        )
        assert verifier_spec is not None and verifier_spec.loader is not None
        verifier = importlib.util.module_from_spec(verifier_spec)
        verifier_spec.loader.exec_module(verifier)

        builder_args = builder.parse_args(
            [
                "--checkout", "/tmp/checkout",
                "--bazel", "/tmp/bazel",
                "--output", "/tmp/output",
            ]
        )
        verifier_args = verifier.parse_args(["--verify-only", "/tmp/generation"])
        self.assertEqual(builder_args.profile, "b0")
        self.assertEqual(verifier_args.profile, "b0")
        self.assertEqual(builder.FIXED_HELPER_SHA256, verifier.FIXED_HELPER_SHA256)

        explicit_builder = builder.parse_args(
            [
                "--checkout", "/tmp/checkout",
                "--bazel", "/tmp/bazel",
                "--output", "/tmp/output",
                "--profile", "b1",
            ]
        )
        explicit_verifier = verifier.parse_args(
            ["--profile", "b1", "--verify-only", "/tmp/generation"]
        )
        self.assertEqual(explicit_builder.profile, "b1")
        self.assertEqual(explicit_verifier.profile, "b1")

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            b1_root = root / "b1"
            b1_root.mkdir()
            b1_bundle, _, _ = self._write_bundle(b1_root)
            b1_helper = self._elf_helper(marker=b"fixed B1 helper fixture")
            b1_helper_path = b1_bundle / "fcitx5-grimodex-mozc-helper"
            b1_helper_path.write_bytes(b1_helper)
            b1_helper_path.chmod(0o755)
            b1_manifest_path = b1_bundle / "manifest.json"
            b1_manifest = json.loads(b1_manifest_path.read_text(encoding="utf-8"))
            b1_manifest["source"]["overlay_sha256"] = verifier.B1_OVERLAY_SHA256
            b1_manifest["artifacts"]["fcitx5-grimodex-mozc-helper"] = {
                "sha256": hashlib.sha256(b1_helper).hexdigest(),
                "size": len(b1_helper),
            }
            b1_manifest_path.write_text(json.dumps(b1_manifest), encoding="utf-8")

            b1_verifier = self._load_verifier_for_fixture(b1_bundle)
            b1_verifier.B1_FIXED_HELPER_SIZE = len(b1_helper)
            b1_verifier.B1_FIXED_HELPER_SHA256 = hashlib.sha256(b1_helper).hexdigest()
            with self.assertRaisesRegex(
                b1_verifier.BundleVerificationError,
                "overlay_sha256",
            ):
                b1_verifier.verify_and_stage(b1_bundle, root / "default-stage")
            b1_verifier.activate_profile("b1")
            generation = b1_verifier.verify_and_stage(
                b1_bundle,
                root / "b1-stage",
            )
            self.assertEqual(
                (generation / "fcitx5-grimodex-mozc-helper").read_bytes(),
                b1_helper,
            )

            b0_root = root / "b0"
            b0_root.mkdir()
            b0_bundle, _, _ = self._write_bundle(b0_root)
            b0_verifier = self._load_verifier_for_fixture(b0_bundle)
            b0_verifier.B1_FIXED_HELPER_SIZE = len(b1_helper)
            b0_verifier.B1_FIXED_HELPER_SHA256 = hashlib.sha256(b1_helper).hexdigest()
            b0_verifier.activate_profile("b1")
            with self.assertRaisesRegex(
                b0_verifier.BundleVerificationError,
                "overlay_sha256|helper identity",
            ):
                b0_verifier.verify_and_stage(b0_bundle, root / "inverse-stage")

    def test_builder_emits_an_atomic_verifier_compatible_fixture(self) -> None:
        builder = self._load_builder()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            inputs, helper, data = self._write_bundle(root)
            license_sources = {
                name: inputs / "licenses" / name for name in self.LICENSE_NAMES
            }
            fixture_hashes = {
                name: hashlib.sha256(path.read_bytes()).hexdigest()
                for name, path in license_sources.items()
            }
            output = root / "emitted"

            manifest = builder.emit_bundle(
                output,
                helper=inputs / "fcitx5-grimodex-mozc-helper",
                data=inputs / "mozc.data",
                licenses=license_sources,
                expected_helper_size=len(helper),
                expected_helper_sha256=hashlib.sha256(helper).hexdigest(),
                expected_data_size=len(data),
                expected_data_sha256=hashlib.sha256(data).hexdigest(),
                license_hashes=fixture_hashes,
            )

            self.assertEqual(
                manifest["artifacts"]["fcitx5-grimodex-mozc-helper"]["sha256"],
                hashlib.sha256(helper).hexdigest(),
            )
            verifier = self._load_verifier_for_fixture(output)
            generation = verifier.verify_and_stage(output, root / "verified")
            self.assertEqual(
                (generation / "fcitx5-grimodex-mozc-helper").read_bytes(),
                helper,
            )

            with self.assertRaisesRegex(builder.BuildError, "replace existing"):
                builder.emit_bundle(
                    output,
                    helper=inputs / "fcitx5-grimodex-mozc-helper",
                    data=inputs / "mozc.data",
                    licenses=license_sources,
                    expected_helper_size=len(helper),
                    expected_helper_sha256=hashlib.sha256(helper).hexdigest(),
                    expected_data_size=len(data),
                    expected_data_sha256=hashlib.sha256(data).hexdigest(),
                    license_hashes=fixture_hashes,
                )

    def test_builder_does_not_publish_a_wrong_b0_dataset(self) -> None:
        builder = self._load_builder()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            inputs, helper, data = self._write_bundle(root)
            license_sources = {
                name: inputs / "licenses" / name for name in self.LICENSE_NAMES
            }
            fixture_hashes = {
                name: hashlib.sha256(path.read_bytes()).hexdigest()
                for name, path in license_sources.items()
            }
            output = root / "must-not-exist"

            with self.assertRaisesRegex(builder.BuildError, "fixed B0"):
                builder.emit_bundle(
                    output,
                    helper=inputs / "fcitx5-grimodex-mozc-helper",
                    data=inputs / "mozc.data",
                    licenses=license_sources,
                    expected_helper_size=len(helper),
                    expected_helper_sha256=hashlib.sha256(helper).hexdigest(),
                    expected_data_size=len(data),
                    expected_data_sha256="0" * 64,
                    license_hashes=fixture_hashes,
                )
            self.assertFalse(output.exists())
            self.assertFalse(any(root.glob(".must-not-exist-*")))

            arbitrary_helper = self._elf_helper(marker=b"different helper")
            helper_path = inputs / "fcitx5-grimodex-mozc-helper"
            helper_path.write_bytes(arbitrary_helper)
            helper_path.chmod(0o755)
            with self.assertRaisesRegex(builder.BuildError, "fixed linux-x86_64"):
                builder.emit_bundle(
                    root / "wrong-helper",
                    helper=helper_path,
                    data=inputs / "mozc.data",
                    licenses=license_sources,
                    expected_helper_size=len(helper),
                    expected_helper_sha256=hashlib.sha256(helper).hexdigest(),
                    expected_data_size=len(data),
                    expected_data_sha256=hashlib.sha256(data).hexdigest(),
                    license_hashes=fixture_hashes,
                )

            wrong_machine = self._elf_helper(machine=183)
            helper_path.write_bytes(wrong_machine)
            helper_path.chmod(0o755)
            with self.assertRaisesRegex(builder.BuildError, "EM_X86_64"):
                builder.emit_bundle(
                    root / "wrong-machine",
                    helper=helper_path,
                    data=inputs / "mozc.data",
                    licenses=license_sources,
                    expected_helper_size=len(wrong_machine),
                    expected_helper_sha256=hashlib.sha256(wrong_machine).hexdigest(),
                    expected_data_size=len(data),
                    expected_data_sha256=hashlib.sha256(data).hexdigest(),
                    license_hashes=fixture_hashes,
                )

    def test_builder_bazel_plan_is_nonpersistent_and_lockfile_closed(self) -> None:
        builder = self._load_builder()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source"
            output_base = root / "output-base"
            source.mkdir()
            output_base.mkdir()
            commands = []

            def fake_run(argv, *, cwd, capture_output=True):
                command = [str(value) for value in argv]
                commands.append((command, cwd, capture_output))
                if "version" in command:
                    return f"bazel {builder.BAZEL_VERSION}"
                if "info" in command:
                    return str(output_base)
                return ""

            with mock.patch.object(builder, "_run_checked", side_effect=fake_run):
                self.assertEqual(
                    builder._build_with_bazel(
                        Path("/fixture/bazelisk"),
                        root / "bazel-root",
                        source,
                    ),
                    output_base,
                )

            self.assertEqual(len(commands), 4)
            for command, cwd, _ in commands:
                self.assertIn("--batch", command)
                self.assertIn("--output_user_root=" + str(root / "bazel-root"), command)
                self.assertIn(
                    "--noexperimental_collect_system_network_usage",
                    command,
                )
                self.assertEqual(cwd, source)
            build_command = next(command for command, _, _ in commands if "build" in command)
            self.assertIn("--stamp=no", build_command)
            self.assertIn("--lockfile_mode=error", build_command)
            for target in builder.BUILD_TARGETS:
                self.assertIn(target, build_command)
            test_command = next(command for command, _, _ in commands if "test" in command)
            self.assertIn("--stamp=no", test_command)
            self.assertIn("--lockfile_mode=error", test_command)
            self.assertIn("--test_output=errors", test_command)
            for target in builder.TEST_TARGETS:
                self.assertIn(target, test_command)

    def test_builder_verifies_revision_tree_and_generated_module_lock(self) -> None:
        builder = self._load_builder()
        with tempfile.TemporaryDirectory() as temporary_directory:
            checkout = Path(temporary_directory) / "checkout"
            source = checkout / "src"
            source.mkdir(parents=True)
            (checkout / ".gitignore").write_text(
                "MODULE.bazel.lock\n",
                encoding="utf-8",
            )
            (checkout / "README.md").write_text("fixed tree\n", encoding="utf-8")
            bazeliskrc = b"USE_BAZEL_VERSION=fixture-bazel\n"
            (source / ".bazeliskrc").write_bytes(bazeliskrc)

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
            tree = subprocess.run(
                ["git", "-C", checkout, "rev-parse", "HEAD^{tree}"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            module_lock = b"fixed generated module lock\n"
            (source / "MODULE.bazel.lock").write_bytes(module_lock)

            with mock.patch.multiple(
                builder,
                SOURCE_REVISION=revision,
                SOURCE_TREE=tree,
                BAZEL_VERSION="fixture-bazel",
                BAZELISKRC_SHA256=hashlib.sha256(bazeliskrc).hexdigest(),
                MODULE_LOCK_SHA256=hashlib.sha256(module_lock).hexdigest(),
            ):
                builder.verify_checkout(checkout)
                (checkout / "README.md").write_text(
                    "dirty tree\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(builder.BuildError, "changes"):
                    builder.verify_checkout(checkout)


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
            "tools/**",
            "third_party/**",
            "LICENSE",
            "NOTICE.md",
            "THIRDPARTYLICENSE",
        ):
            with self.subTest(path_filter=path_filter):
                expected = f'- "{path_filter}"'
                self.assertIn(expected, release_workflow)
                self.assertIn(expected, package_workflow)
                self.assertIn(expected, integration_workflow)

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

    def test_aur_declares_and_installs_the_vulkan_runtime_provider(self) -> None:
        pkgbuild = (AUR_DIRECTORY / "PKGBUILD").read_text(encoding="utf-8")
        srcinfo = (AUR_DIRECTORY / ".SRCINFO").read_text(encoding="utf-8")
        workflow = BUILD_WORKFLOW.read_text(encoding="utf-8")

        self.assertRegex(
            pkgbuild,
            r"(?m)^depends=\([^\n]*'vulkan-icd-loader'[^\n]*\)$",
        )
        self.assertRegex(
            srcinfo,
            r"(?m)^\s*depends = vulkan-icd-loader$",
        )
        arch_job = workflow.split("  arch-package-transaction:", 1)[1].split(
            "  publish-release:", 1
        )[0]
        self.assertRegex(
            arch_job,
            r"(?m)^\s+vulkan-icd-loader\s*\\?$",
        )

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
        for component in ("Core", "Utils", "Config"):
            self.assertIn(f"find_package(Fcitx5{component} 5.0.4 REQUIRED)", cmake)

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
            "pacman --query --check --check",
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
        self.assertIn("THIRDPARTYLICENSE", pkgbuild)

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

    def test_swift_build_patches_the_pinned_azookey_checkout_idempotently(self) -> None:
        driver = SWIFT_BUILD_DRIVER.read_text(encoding="utf-8")
        integration_workflow = INTEGRATION_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn('"${SWIFT_EXECUTABLE}" package resolve', driver)
        self.assertIn("prepare_azookey_dependency.cmake", driver)
        self.assertLess(
            driver.index("prepare_azookey_dependency.cmake"),
            driver.index("COMMAND ${SWIFT_COMMAND}"),
        )
        self.assertIn(
            "hazkey-server/prepare_azookey_dependency.cmake",
            integration_workflow,
        )
        self.assertIn(
            "hazkey-server/patches/AzooKeyKanaKanjiConverter/*.patch",
            integration_workflow,
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            scratch = Path(temporary_directory) / "swift-build"
            checkout = scratch / "checkouts/AzooKeyKanaKanjiConverter"
            context_file = (
                checkout
                / "Sources/KanaKanjiConverterModule/ConversionAlgorithms"
                / "Zenzai/Zenz/ZenzContext.swift"
            )
            mock_file = (
                checkout
                / "Sources/KanaKanjiConverterModule/ConversionAlgorithms"
                / "Zenzai/Zenz/llama-mock.swift"
            )
            context_file.parent.mkdir(parents=True)
            context_file.write_text(
                """final class ZenzContext {
    func previousMethod() {
    }

    func reset_context() throws {
        llama_free(self.context)
        let params = Self.ctx_params(deviceConfig: self.currentDeviceConfig)
        let context = llama_init_from_model(self.model, params)
        guard let context else {
            debug("Could not load context!")
            throw ZenzError.couldNotLoadContext
        }
        self.context = context
        self.prevInput = []
        self.prevPrompt = []
    }

    private func get_logits(tokens: [llama_token], logits_start_index: Int = 0) -> UnsafeMutablePointer<Float>? {
        nil
    }
}
""",
                encoding="utf-8",
            )
            mock_file.parent.mkdir(parents=True, exist_ok=True)
            mock_file.write_text(
                """#if !Zenzai
package typealias llama_vocab = OpaquePointer

package func llama_model_free(_: llama_model) {}

package func ggml_backend_load_all() {}
package func ggml_backend_dev_count() {}

package func llama_backend_init() {}
package func llama_backend_free() {}

package typealias llama_context = OpaquePointer
package typealias llama_seq_id = Int32
package typealias llama_pos = Int32
package func llama_model_load_from_file(_: String, _: llama_model_params) -> llama_model? { unimplemented() }

package func llama_kv_cache_seq_rm(_: llama_context, _: llama_seq_id, _: llama_pos, _: llama_pos) {}
package func llama_kv_cache_seq_pos_max(_: llama_context, _: llama_seq_id) -> Int { unimplemented() }

package struct llama_batch {
    package var token: [llama_token]
}

package func ggml_backend_load_all() {}
package func ggml_backend_load_all_from_path(_: String) {}
package func ggml_backend_dev_count() -> Int { 0 }
#endif
""",
                encoding="utf-8",
            )

            subprocess.run(["git", "init", "--quiet", checkout], check=True)
            subprocess.run(
                ["git", "-C", checkout, "config", "user.name", "Grimodex CI"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", checkout, "config", "user.email", "ci@example.invalid"],
                check=True,
            )
            subprocess.run(["git", "-C", checkout, "add", "."], check=True)
            subprocess.run(
                ["git", "-C", checkout, "commit", "--quiet", "-m", "fixture"],
                check=True,
            )
            revision = subprocess.run(
                ["git", "-C", checkout, "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            command = [
                "cmake",
                f"-DSWIFT_SCRATCH_PATH={scratch}",
                f"-DAZOOKEY_EXPECTED_REVISION={revision}",
                "-P",
                str(AZOOKEY_PREPARER),
            ]
            for _ in range(2):
                completed = subprocess.run(
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(
                    completed.returncode,
                    0,
                    completed.stdout + completed.stderr,
                )

            prepared = mock_file.read_text(encoding="utf-8")
            self.assertEqual(prepared.count("ggml_backend_load_all()"), 1)
            self.assertEqual(prepared.count("ggml_backend_dev_count()"), 1)
            self.assertEqual(prepared.count("llama_kv_cache_clear(_: llama_context)"), 1)
            context = context_file.read_text(encoding="utf-8")
            self.assertIn("llama_kv_cache_clear(self.context)", context)
            self.assertNotIn("llama_free(self.context)", context)

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
