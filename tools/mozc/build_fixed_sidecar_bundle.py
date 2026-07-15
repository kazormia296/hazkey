#!/usr/bin/env python3
"""Build an importable fixed-source Mozc sidecar bundle.

This script has no third-party Python dependencies.  It requires a clean
checkout of the pinned fcitx-mozkey revision, its already-resolved
MODULE.bazel.lock, and an explicit Bazel/Bazelisk executable.  Bazel is always
run in batch mode as a non-root user and with lockfile updates disabled.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
from typing import Mapping, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
B0_OVERLAY_DIRECTORY = (
    REPOSITORY_ROOT
    / "third_party/fcitx-mozkey/overlay/grimodex_mozc_sidecar"
)
B1_OVERLAY_DIRECTORY = (
    REPOSITORY_ROOT
    / "third_party/fcitx-mozkey/overlay/grimodex_mozc_sidecar_b1"
)
OVERLAY_DIRECTORY = B0_OVERLAY_DIRECTORY

SCHEMA = "grimodex.mozc-artifact-bundle.v1"
SOURCE_REPOSITORY = "https://github.com/Masterisk-F/fcitx-mozkey"
SOURCE_REVISION = "462cbbf04886e32096bc318833e974ccc43d9fc8"
SOURCE_TREE = "95365a39134949f5d68f565e1ce451085b5965a8"
BAZEL_VERSION = "9.0.2"
BAZELISKRC_SHA256 = "59acd943a0d15254345f3e176f42786af2b4fba83b1657341cf56e017a7db19a"
MODULE_LOCK_SHA256 = "ab6b647b1c12072eee26ec2370fa928b2ac7c3146e72daf232010dfe254ed972"
OVERLAY_SHA256 = "26cf5430b39dcdc04c1f91a6ce473554c3f1ba3f04c2defdcf146f859b6776d6"
B1_OVERLAY_SHA256 = "974003704cacdc9b272fe22c3675222889c1bee2c75b81619317b2431318f55d"

TARGET_CONTRACT = {
    "system": "linux",
    "architecture": "x86_64",
    "elf": {
        "class": 64,
        "endianness": "little",
        "machine": "EM_X86_64",
    },
    "runtime": {
        "interpreter": "/lib64/ld-linux-x86-64.so.2",
        "required_symbol_versions": {
            "glibc": "GLIBC_2.38",
            "glibcxx": "GLIBCXX_3.4.32",
            "cxxabi": "CXXABI_1.3.15",
        },
    },
}
FIXED_HELPER_SIZE = 5_695_048
FIXED_HELPER_SHA256 = "8676275bb47aefe963c8b82047cc66fb7a5140caec72d1ebbfa17556b281577d"
B1_FIXED_HELPER_SIZE = 5_746_568
B1_FIXED_HELPER_SHA256 = "728d9a79c0f540a832d3f404a2603f49080e1f9e7ee1d24df1a0a69f5a4a75e8"
FIXED_DATA_SIZE = 18_887_468
FIXED_DATA_SHA256 = "b9884362e37772f772a0d28d1e12622455c14353497b3435deed60aa7e592c5e"
LICENSE_HASHES = {
    "MOZC-LICENSE": "44cdd923b91ea9199293abecc2762c70c87dbf1e581c027a94c416368d1a648c",
    "FCITX-MOZKEY-THIRD-PARTY-NOTICES.md": "e1bc0a70491f19f5acc7edcae23a1aa5c3b317009246837abd04e0f436c87c46",
    "DICTIONARY-OSS-NOTICE.txt": "6b1de66bc6fa30e0b45dc45b8c8f6c57bd78aff923c261a3efffac8eb86f7bac",
    "ABSEIL-LICENSE": "c79a7fea0e3cac04cd43f20e7b648e5a0ff8fa5344e644b0ee09ca1162b62747",
    "PROTOBUF-LICENSE": "6e5e117324afd944dcf67f36cf329843bc1a92229a8cd9bb573d7a83130fea7d",
    "UTF8-RANGE-LICENSE": "02de69b64fc36d9e938f418e52723e42f0b2b226d58a9cb3c8dcbdf7059f5074",
    "JAPANESE-USAGE-DICTIONARY-LICENSE": "91e74c9b189a60a3f5ba13b4aa28f87f25ee9252a64d547784e72752d089631a",
}

OVERLAY_FILES = frozenset(
    {
        "BUILD.bazel",
        "mozc_sidecar.proto",
        "mozc_sidecar_helper.cc",
        "sha256.h",
        "sha256_test.cc",
    }
)
B1_OVERLAY_FILES = frozenset(
    {
        "BUILD.bazel",
        "mozc_sidecar.proto",
        "mozc_sidecar_helper.cc",
        "sentence_alternatives.h",
        "sentence_alternatives_test.cc",
        "sha256.h",
        "sha256_test.cc",
    }
)
HELPER_OUTPUT = Path(
    "bazel-bin/grimodex_mozc_sidecar/fcitx5-grimodex-mozc-helper"
)
DATA_OUTPUT = Path("bazel-bin/data_manager/oss/mozc.data")
BUILD_TARGETS = (
    "//grimodex_mozc_sidecar:fcitx5-grimodex-mozc-helper",
    "//data_manager/oss:mozc_dataset_for_oss",
)
TEST_TARGETS = ("//grimodex_mozc_sidecar:sha256_test",)
B1_HELPER_OUTPUT = Path(
    "bazel-bin/grimodex_mozc_sidecar_b1/fcitx5-grimodex-mozc-helper"
)
B1_BUILD_TARGETS = (
    "//grimodex_mozc_sidecar_b1:fcitx5-grimodex-mozc-helper",
    "//data_manager/oss:mozc_dataset_for_oss",
)
B1_TEST_TARGETS = (
    "//grimodex_mozc_sidecar_b1:sentence_alternatives_test",
    "//grimodex_mozc_sidecar_b1:sha256_test",
)

PROFILE_NAMES = ("b0", "b1")


def activate_profile(name: str) -> None:
    """Select one immutable build contract; B0 remains the process default."""
    global OVERLAY_DIRECTORY
    global OVERLAY_SHA256
    global OVERLAY_FILES
    global HELPER_OUTPUT
    global BUILD_TARGETS
    global TEST_TARGETS
    global FIXED_HELPER_SIZE
    global FIXED_HELPER_SHA256

    if name == "b0":
        OVERLAY_DIRECTORY = B0_OVERLAY_DIRECTORY
        OVERLAY_SHA256 = "26cf5430b39dcdc04c1f91a6ce473554c3f1ba3f04c2defdcf146f859b6776d6"
        OVERLAY_FILES = frozenset(
            {
                "BUILD.bazel",
                "mozc_sidecar.proto",
                "mozc_sidecar_helper.cc",
                "sha256.h",
                "sha256_test.cc",
            }
        )
        HELPER_OUTPUT = Path(
            "bazel-bin/grimodex_mozc_sidecar/fcitx5-grimodex-mozc-helper"
        )
        BUILD_TARGETS = (
            "//grimodex_mozc_sidecar:fcitx5-grimodex-mozc-helper",
            "//data_manager/oss:mozc_dataset_for_oss",
        )
        TEST_TARGETS = ("//grimodex_mozc_sidecar:sha256_test",)
        FIXED_HELPER_SIZE = 5_695_048
        FIXED_HELPER_SHA256 = (
            "8676275bb47aefe963c8b82047cc66fb7a5140caec72d1ebbfa17556b281577d"
        )
        return
    if name == "b1":
        OVERLAY_DIRECTORY = B1_OVERLAY_DIRECTORY
        OVERLAY_SHA256 = B1_OVERLAY_SHA256
        OVERLAY_FILES = B1_OVERLAY_FILES
        HELPER_OUTPUT = B1_HELPER_OUTPUT
        BUILD_TARGETS = B1_BUILD_TARGETS
        TEST_TARGETS = B1_TEST_TARGETS
        FIXED_HELPER_SIZE = B1_FIXED_HELPER_SIZE
        FIXED_HELPER_SHA256 = B1_FIXED_HELPER_SHA256
        return
    raise BuildError(f"unknown Mozc sidecar profile: {name}")
LICENSE_LOCATIONS = {
    "MOZC-LICENSE": ("source", Path("LICENSE")),
    "FCITX-MOZKEY-THIRD-PARTY-NOTICES.md": (
        "source",
        Path("THIRD_PARTY_NOTICES.md"),
    ),
    "DICTIONARY-OSS-NOTICE.txt": (
        "source",
        Path("src/data/dictionary_oss/README.txt"),
    ),
    "ABSEIL-LICENSE": ("output_base", Path("external/abseil-cpp+/LICENSE")),
    "PROTOBUF-LICENSE": ("output_base", Path("external/protobuf+/LICENSE")),
    "UTF8-RANGE-LICENSE": (
        "output_base",
        Path("external/protobuf+/third_party/utf8_range/LICENSE"),
    ),
    "JAPANESE-USAGE-DICTIONARY-LICENSE": (
        "output_base",
        Path("external/+http_archive+ja_usage_dict/LICENSE"),
    ),
}
COPY_CHUNK_BYTES = 1024 * 1024
MAX_HELPER_BYTES = 64 * 1024 * 1024
MAX_LICENSE_BYTES = 1024 * 1024
ELFCLASS64 = 2
ELFDATA2LSB = 1
EV_CURRENT = 1
EM_X86_64 = 62
ELF64_HEADER_SIZE = 64
ELF64_PROGRAM_HEADER_SIZE = 56
ELF64_SECTION_HEADER_SIZE = 64
PT_INTERP = 3
SHT_STRTAB = 3
SHT_GNU_VERNEED = 0x6FFFFFFE
ELF_VERSION_REQUIREMENT = 1


class BuildError(RuntimeError):
    """The fixed-input bundle could not be built safely."""


def _run_checked(
    argv: Sequence[str | Path],
    *,
    cwd: Path,
    capture_output: bool = True,
) -> str:
    command = [str(argument) for argument in argv]
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            check=False,
            text=True,
            stdout=subprocess.PIPE if capture_output else None,
            stderr=subprocess.PIPE if capture_output else None,
        )
    except OSError as error:
        raise BuildError(f"could not execute {command[0]}: {error}") from error
    if result.returncode != 0:
        details = ""
        if capture_output:
            details = (result.stderr or result.stdout or "").strip()
        suffix = f": {details}" if details else ""
        raise BuildError(
            f"command failed with exit code {result.returncode}: "
            f"{' '.join(command)}{suffix}"
        )
    return result.stdout.strip() if result.stdout is not None else ""


def _sha256_regular_file(path: Path) -> tuple[int, str]:
    if path.is_symlink():
        raise BuildError(f"input must not be a symlink: {path}")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise BuildError(f"cannot open required input {path}: {error}") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise BuildError(f"input must be a regular file: {path}")
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(descriptor, COPY_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        return size, digest.hexdigest()
    finally:
        os.close(descriptor)


def _require_identity(
    path: Path,
    *,
    expected_sha256: str,
    expected_size: int | None = None,
) -> tuple[int, str]:
    size, digest = _sha256_regular_file(path)
    if expected_size is not None and size != expected_size:
        raise BuildError(
            f"size mismatch for {path}: expected {expected_size}, got {size}"
        )
    if digest != expected_sha256:
        raise BuildError(
            f"SHA-256 mismatch for {path}: expected {expected_sha256}, got {digest}"
        )
    return size, digest


def _slice_elf(data: bytes, offset: int, size: int, description: str) -> bytes:
    if offset < 0 or size < 0 or offset > len(data) or size > len(data) - offset:
        raise BuildError(f"Bazel helper has an invalid {description} range")
    return data[offset : offset + size]


def _elf_string(table: bytes, offset: int, description: str) -> str:
    if offset < 0 or offset >= len(table):
        raise BuildError(f"Bazel helper has an invalid {description} string offset")
    end = table.find(b"\0", offset)
    if end < 0:
        raise BuildError(f"Bazel helper has an unterminated {description} string")
    try:
        return table[offset:end].decode("ascii")
    except UnicodeDecodeError as error:
        raise BuildError(f"Bazel helper has a non-ASCII {description} string") from error


def _symbol_version_key(value: str, prefix: str) -> tuple[int, ...] | None:
    match = re.fullmatch(re.escape(prefix) + r"([0-9]+(?:\.[0-9]+)*)", value)
    if match is None:
        return None
    return tuple(int(component) for component in match.group(1).split("."))


def _inspect_elf_runtime_contract(data: bytes) -> dict[str, object]:
    if len(data) < ELF64_HEADER_SIZE or data[:4] != b"\x7fELF":
        raise BuildError("Bazel helper output is not an ELF executable")
    if data[4] != ELFCLASS64:
        raise BuildError("Bazel helper output must be ELF64")
    if data[5] != ELFDATA2LSB:
        raise BuildError("Bazel helper output must be little-endian ELF")
    if data[6] != EV_CURRENT:
        raise BuildError("Bazel helper output has an unsupported ELF version")
    if struct.unpack_from("<H", data, 18)[0] != EM_X86_64:
        raise BuildError("Bazel helper output must target EM_X86_64")

    program_offset = struct.unpack_from("<Q", data, 32)[0]
    section_offset = struct.unpack_from("<Q", data, 40)[0]
    program_entry_size = struct.unpack_from("<H", data, 54)[0]
    program_count = struct.unpack_from("<H", data, 56)[0]
    section_entry_size = struct.unpack_from("<H", data, 58)[0]
    section_count = struct.unpack_from("<H", data, 60)[0]
    if (
        program_entry_size < ELF64_PROGRAM_HEADER_SIZE
        or program_count == 0
        or section_entry_size < ELF64_SECTION_HEADER_SIZE
        or section_count == 0
    ):
        raise BuildError("Bazel helper is missing required ELF tables")

    interpreters: list[str] = []
    for index in range(program_count):
        entry = _slice_elf(
            data,
            program_offset + index * program_entry_size,
            ELF64_PROGRAM_HEADER_SIZE,
            "program header",
        )
        if struct.unpack_from("<I", entry, 0)[0] != PT_INTERP:
            continue
        payload = _slice_elf(
            data,
            struct.unpack_from("<Q", entry, 8)[0],
            struct.unpack_from("<Q", entry, 32)[0],
            "interpreter",
        )
        if not payload.endswith(b"\0") or payload.count(b"\0") != 1:
            raise BuildError("Bazel helper has an invalid ELF interpreter")
        try:
            interpreters.append(payload[:-1].decode("ascii"))
        except UnicodeDecodeError as error:
            raise BuildError("Bazel helper has a non-ASCII ELF interpreter") from error
    if len(interpreters) != 1:
        raise BuildError("Bazel helper must declare exactly one ELF interpreter")

    sections: list[tuple[int, int, int, int, int]] = []
    for index in range(section_count):
        entry = _slice_elf(
            data,
            section_offset + index * section_entry_size,
            ELF64_SECTION_HEADER_SIZE,
            "section header",
        )
        sections.append(
            (
                struct.unpack_from("<I", entry, 4)[0],
                struct.unpack_from("<Q", entry, 24)[0],
                struct.unpack_from("<Q", entry, 32)[0],
                struct.unpack_from("<I", entry, 40)[0],
                struct.unpack_from("<I", entry, 44)[0],
            )
        )
    version_sections = [entry for entry in sections if entry[0] == SHT_GNU_VERNEED]
    if len(version_sections) != 1:
        raise BuildError("Bazel helper must contain one GNU version requirement table")
    (
        _,
        requirement_offset,
        requirement_size,
        string_table_index,
        requirement_count,
    ) = version_sections[0]
    if string_table_index >= len(sections):
        raise BuildError("Bazel helper has an invalid GNU version string table")
    string_type, string_offset, string_size, _, _ = sections[string_table_index]
    if string_type != SHT_STRTAB:
        raise BuildError("Bazel helper GNU version names must use a string table")
    strings = _slice_elf(data, string_offset, string_size, "dynamic string table")
    requirements = _slice_elf(
        data,
        requirement_offset,
        requirement_size,
        "GNU version requirement table",
    )
    structure_size = 16
    work_budget = len(requirements) // structure_size
    if requirement_count == 0 or requirement_count > work_budget:
        raise BuildError("Bazel helper GNU version chain exceeds its work budget")

    symbol_versions: set[str] = set()
    requirement_cursor = 0
    visited_structures: set[int] = set()
    remaining_work = work_budget
    for requirement_index in range(requirement_count):
        if requirement_cursor in visited_structures or remaining_work == 0:
            raise BuildError("Bazel helper has a non-monotonic GNU requirement chain")
        visited_structures.add(requirement_cursor)
        remaining_work -= 1
        entry = _slice_elf(
            requirements,
            requirement_cursor,
            structure_size,
            "GNU version requirement",
        )
        version, auxiliary_count, _, auxiliary_offset, next_offset = struct.unpack_from(
            "<HHIII", entry, 0
        )
        if version != ELF_VERSION_REQUIREMENT or auxiliary_count == 0:
            raise BuildError("Bazel helper has an invalid GNU version requirement")
        if auxiliary_offset < structure_size:
            raise BuildError("Bazel helper has an invalid GNU version chain")
        if auxiliary_count > remaining_work:
            raise BuildError("Bazel helper GNU version chain exceeds its work budget")
        auxiliary_cursor = requirement_cursor + auxiliary_offset
        for auxiliary_index in range(auxiliary_count):
            if auxiliary_cursor in visited_structures or remaining_work == 0:
                raise BuildError("Bazel helper has a non-monotonic GNU version chain")
            visited_structures.add(auxiliary_cursor)
            remaining_work -= 1
            auxiliary = _slice_elf(
                requirements,
                auxiliary_cursor,
                structure_size,
                "GNU version auxiliary entry",
            )
            _, _, _, name_offset, auxiliary_next = struct.unpack_from(
                "<IHHII", auxiliary, 0
            )
            symbol_versions.add(
                _elf_string(strings, name_offset, "GNU version requirement")
            )
            if auxiliary_index + 1 < auxiliary_count:
                if auxiliary_next < structure_size:
                    raise BuildError("Bazel helper has an invalid GNU version chain")
                auxiliary_cursor += auxiliary_next
            elif auxiliary_next != 0:
                raise BuildError("Bazel helper has an unterminated GNU version chain")
        if requirement_index + 1 < requirement_count:
            if next_offset < structure_size:
                raise BuildError("Bazel helper has an invalid GNU requirement chain")
            requirement_cursor += next_offset
            if requirement_cursor >= len(requirements):
                raise BuildError("Bazel helper GNU requirement chain is out of range")
        elif next_offset != 0:
            raise BuildError("Bazel helper has an unterminated GNU requirement chain")

    actual_versions: dict[str, str] = {}
    for key, prefix in (
        ("glibc", "GLIBC_"),
        ("glibcxx", "GLIBCXX_"),
        ("cxxabi", "CXXABI_"),
    ):
        matching = [
            value
            for value in symbol_versions
            if _symbol_version_key(value, prefix) is not None
        ]
        if not matching:
            raise BuildError(f"Bazel helper has no {prefix} runtime requirement")
        actual_versions[key] = max(
            matching,
            key=lambda value: _symbol_version_key(value, prefix),
        )

    runtime = {
        "interpreter": interpreters[0],
        "required_symbol_versions": actual_versions,
    }
    if runtime != TARGET_CONTRACT["runtime"]:
        raise BuildError(
            "Bazel helper runtime ABI does not match the fixed contract: "
            f"expected {TARGET_CONTRACT['runtime']}, got {runtime}"
        )
    return runtime


def _require_linux_x86_64_elf(path: Path) -> None:
    """Require the fixed helper target and runtime ABI before publishing it."""
    if path.is_symlink():
        raise BuildError(f"helper must not be a symlink: {path}")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise BuildError(f"cannot open helper {path}: {error}") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise BuildError(f"helper must be a regular file: {path}")
        if metadata.st_size > MAX_HELPER_BYTES:
            raise BuildError("Bazel helper output exceeds the fixed size limit")
        data = bytearray()
        while True:
            chunk = os.read(descriptor, COPY_CHUNK_BYTES)
            if not chunk:
                break
            data.extend(chunk)
    finally:
        os.close(descriptor)
    _inspect_elf_runtime_contract(bytes(data))


def _git(checkout: Path, *arguments: str) -> str:
    absolute_checkout = checkout.resolve()
    return _run_checked(
        ("git", "-C", absolute_checkout, *arguments),
        cwd=absolute_checkout,
    )


def verify_checkout(checkout: Path) -> None:
    if checkout.is_symlink() or not checkout.is_dir():
        raise BuildError("checkout must be a regular, non-symlink directory")
    revision = _git(checkout, "rev-parse", "--verify", "HEAD")
    tree = _git(checkout, "rev-parse", "HEAD^{tree}")
    if revision != SOURCE_REVISION:
        raise BuildError(
            f"checkout revision mismatch: expected {SOURCE_REVISION}, got {revision}"
        )
    if tree != SOURCE_TREE:
        raise BuildError(f"checkout tree mismatch: expected {SOURCE_TREE}, got {tree}")
    status = _git(
        checkout,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--ignored=no",
    )
    if status:
        raise BuildError("checkout contains tracked or untracked changes")

    _require_identity(
        checkout / "src/.bazeliskrc",
        expected_sha256=BAZELISKRC_SHA256,
    )
    bazelisk_settings = {}
    for raw_line in (checkout / "src/.bazeliskrc").read_text(
        encoding="utf-8"
    ).splitlines():
        if not raw_line or raw_line.startswith("#"):
            continue
        key, separator, value = raw_line.partition("=")
        if not separator or key in bazelisk_settings:
            raise BuildError("src/.bazeliskrc is malformed")
        bazelisk_settings[key] = value
    if bazelisk_settings.get("USE_BAZEL_VERSION") != BAZEL_VERSION:
        raise BuildError("src/.bazeliskrc does not pin the required Bazel version")

    _require_identity(
        checkout / "src/MODULE.bazel.lock",
        expected_sha256=MODULE_LOCK_SHA256,
    )


def verify_overlay(overlay: Path | None = None) -> None:
    if overlay is None:
        overlay = OVERLAY_DIRECTORY
    if overlay.is_symlink() or not overlay.is_dir():
        raise BuildError("Mozc sidecar overlay must be a regular directory")
    actual = {entry.name for entry in overlay.iterdir()}
    if actual != OVERLAY_FILES:
        raise BuildError(
            "Mozc sidecar overlay file set mismatch: "
            f"expected {sorted(OVERLAY_FILES)}, got {sorted(actual)}"
        )
    actual_digest = compute_overlay_sha256(overlay)
    if actual_digest != OVERLAY_SHA256:
        raise BuildError(
            "Mozc sidecar overlay identity mismatch: "
            f"expected {OVERLAY_SHA256}, got {actual_digest}"
        )


def compute_overlay_sha256(overlay: Path) -> str:
    """Hash the exact overlay as length-prefixed sorted names and file bytes."""
    digest = hashlib.sha256()
    for name in sorted(OVERLAY_FILES):
        path = overlay / name
        if path.is_symlink():
            raise BuildError(f"overlay input must not be a symlink: {path}")
        flags = os.O_RDONLY
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags)
        except OSError as error:
            raise BuildError(f"cannot open overlay input {path}: {error}") from error
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise BuildError(f"overlay input must be a regular file: {path}")
            name_bytes = name.encode("utf-8")
            digest.update(len(name_bytes).to_bytes(4, byteorder="big"))
            digest.update(name_bytes)
            digest.update(metadata.st_size.to_bytes(8, byteorder="big"))
            while True:
                chunk = os.read(descriptor, COPY_CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
        finally:
            os.close(descriptor)
    return digest.hexdigest()


def ensure_non_root() -> None:
    if not hasattr(os, "geteuid"):
        raise BuildError("the fixed Mozc sidecar builder is supported only on POSIX")
    if os.geteuid() == 0:
        raise BuildError("refusing to run Bazel as root")


def verify_bazel_executable(bazel: Path) -> Path:
    absolute = bazel.expanduser().resolve()
    if not absolute.is_file() or not os.access(absolute, os.X_OK):
        raise BuildError(f"Bazel executable is missing or not executable: {bazel}")
    return absolute


def bazel_argv(
    bazel: Path,
    output_user_root: Path | None,
    command: str,
    arguments: Sequence[str] = (),
) -> list[str]:
    argv = [str(bazel), "--batch"]
    if output_user_root is not None:
        argv.append(f"--output_user_root={output_user_root}")
    # Bazel 9's network profiler crashes when the build itself runs in a
    # network namespace with no loopback interface.  Keep the fixed build
    # compatible with a no-network sandbox, and avoid collecting host network
    # metadata that is irrelevant to the artifact.
    argv.extend(
        (
            command,
            "--noexperimental_collect_system_network_usage",
            *arguments,
        )
    )
    return argv


def _validate_archive_member(member: tarfile.TarInfo) -> None:
    path = PurePosixPath(member.name)
    if path.is_absolute() or ".." in path.parts:
        raise BuildError(f"unsafe path in fixed source archive: {member.name}")
    if not member.isdir() and not member.isfile():
        raise BuildError(f"unsupported entry in fixed source archive: {member.name}")


def _make_isolated_source(checkout: Path, work_directory: Path) -> Path:
    checkout = checkout.resolve()
    archive = work_directory / "source.tar"
    source = work_directory / "source"
    source.mkdir()
    _run_checked(
        (
            "git",
            "-C",
            checkout,
            "archive",
            "--format=tar",
            f"--output={archive}",
            SOURCE_REVISION,
        ),
        cwd=checkout,
    )
    try:
        with tarfile.open(archive, mode="r:") as source_archive:
            members = source_archive.getmembers()
            for member in members:
                _validate_archive_member(member)
            source_archive.extractall(source, members=members)
    except (OSError, tarfile.TarError) as error:
        raise BuildError(f"could not extract fixed source archive: {error}") from error

    module_lock = source / "src/MODULE.bazel.lock"
    shutil.copyfile(checkout / "src/MODULE.bazel.lock", module_lock)
    module_lock.chmod(0o644)
    _require_identity(module_lock, expected_sha256=MODULE_LOCK_SHA256)
    return source


def _apply_overlay(source: Path, overlay: Path | None = None) -> None:
    if overlay is None:
        overlay = OVERLAY_DIRECTORY
    verify_overlay(overlay)
    destination = source / f"src/{OVERLAY_DIRECTORY.name}"
    if destination.exists() or destination.is_symlink():
        raise BuildError("fixed source unexpectedly already contains the overlay path")
    destination.mkdir()
    for name in sorted(OVERLAY_FILES):
        shutil.copyfile(overlay / name, destination / name)
        (destination / name).chmod(0o644)
    copied_digest = compute_overlay_sha256(destination)
    if copied_digest != OVERLAY_SHA256:
        raise BuildError(
            "copied Mozc sidecar overlay identity mismatch: "
            f"expected {OVERLAY_SHA256}, got {copied_digest}"
        )


def _verify_bazel_runtime(
    bazel: Path,
    output_user_root: Path | None,
    source_directory: Path,
) -> None:
    output = _run_checked(
        bazel_argv(
            bazel,
            output_user_root,
            "version",
            ("--gnu_format", "--noenable_platform_specific_config"),
        ),
        cwd=source_directory,
    )
    versions = [
        line.removeprefix("bazel ")
        for line in output.splitlines()
        if line.startswith("bazel ")
    ]
    if versions != [BAZEL_VERSION]:
        raise BuildError(
            f"Bazel runtime mismatch: expected {BAZEL_VERSION}, got {output!r}"
        )


def _build_with_bazel(
    bazel: Path,
    output_user_root: Path | None,
    source_directory: Path,
) -> Path:
    _verify_bazel_runtime(bazel, output_user_root, source_directory)
    _run_checked(
        bazel_argv(
            bazel,
            output_user_root,
            "build",
            (
                "--config=release_build",
                "--config=oss_linux",
                "--stamp=no",
                "--lockfile_mode=error",
                *BUILD_TARGETS,
            ),
        ),
        cwd=source_directory,
        capture_output=False,
    )
    _run_checked(
        bazel_argv(
            bazel,
            output_user_root,
            "test",
            (
                "--config=release_build",
                "--config=oss_linux",
                "--stamp=no",
                "--lockfile_mode=error",
                "--test_output=errors",
                *TEST_TARGETS,
            ),
        ),
        cwd=source_directory,
        capture_output=False,
    )
    output_base_value = _run_checked(
        bazel_argv(
            bazel,
            output_user_root,
            "info",
            (
                "output_base",
                "--noenable_platform_specific_config",
                "--lockfile_mode=error",
            ),
        ),
        cwd=source_directory,
    )
    output_base = Path(output_base_value)
    if not output_base.is_absolute() or not output_base.is_dir():
        raise BuildError(f"Bazel returned an invalid output_base: {output_base_value!r}")
    return output_base


def locate_license_sources(source: Path, output_base: Path) -> dict[str, Path]:
    roots = {"source": source, "output_base": output_base}
    return {
        name: roots[root_name] / relative_path
        for name, (root_name, relative_path) in LICENSE_LOCATIONS.items()
    }


def _copy_and_hash(
    source: Path,
    destination: Path,
    *,
    maximum_size: int,
) -> tuple[int, str]:
    if source.is_symlink():
        raise BuildError(f"input must not be a symlink: {source}")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(source, flags)
    except OSError as error:
        raise BuildError(f"cannot open required input {source}: {error}") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise BuildError(f"input must be a regular file: {source}")
        if metadata.st_size > maximum_size:
            raise BuildError(
                f"input exceeds the {maximum_size}-byte copy limit: {source}"
            )
        digest = hashlib.sha256()
        size = 0
        with destination.open("xb") as output:
            while True:
                chunk = os.read(descriptor, COPY_CHUNK_BYTES)
                if not chunk:
                    break
                if size + len(chunk) > maximum_size:
                    raise BuildError(
                        f"input exceeds the {maximum_size}-byte copy limit: {source}"
                    )
                output.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            output.flush()
            os.fsync(output.fileno())
        return size, digest.hexdigest()
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def emit_bundle(
    output: Path,
    *,
    helper: Path,
    data: Path,
    licenses: Mapping[str, Path],
    expected_helper_size: int | None = None,
    expected_helper_sha256: str | None = None,
    expected_data_size: int | None = None,
    expected_data_sha256: str | None = None,
    license_hashes: Mapping[str, str] = LICENSE_HASHES,
) -> dict[str, object]:
    if expected_helper_size is None:
        expected_helper_size = FIXED_HELPER_SIZE
    if expected_helper_sha256 is None:
        expected_helper_sha256 = FIXED_HELPER_SHA256
    if expected_data_size is None:
        expected_data_size = FIXED_DATA_SIZE
    if expected_data_sha256 is None:
        expected_data_sha256 = FIXED_DATA_SHA256
    if output.exists() or output.is_symlink():
        raise BuildError(f"refusing to replace existing bundle output: {output}")
    if set(licenses) != set(license_hashes):
        raise BuildError("license source set does not match the fixed bundle contract")
    helper_metadata = helper.stat(follow_symlinks=False)
    if not stat.S_ISREG(helper_metadata.st_mode) or not (helper_metadata.st_mode & 0o111):
        raise BuildError("Bazel helper output must be a regular executable file")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{output.name}-", dir=output.parent)
    )
    try:
        helper_size, helper_hash = _copy_and_hash(
            helper,
            temporary / "fcitx5-grimodex-mozc-helper",
            maximum_size=MAX_HELPER_BYTES,
        )
        staged_helper = temporary / "fcitx5-grimodex-mozc-helper"
        if (
            helper_size != expected_helper_size
            or helper_hash != expected_helper_sha256
        ):
            raise BuildError(
                "built helper is not the fixed linux-x86_64 artifact: "
                f"size={helper_size} sha256={helper_hash}"
            )
        _require_linux_x86_64_elf(staged_helper)
        staged_helper.chmod(0o755)
        data_size, data_hash = _copy_and_hash(
            data,
            temporary / "mozc.data",
            maximum_size=expected_data_size,
        )
        if data_size != expected_data_size or data_hash != expected_data_sha256:
            raise BuildError(
                "built mozc.data is not the fixed B0 dataset: "
                f"size={data_size} sha256={data_hash}"
            )
        (temporary / "mozc.data").chmod(0o644)

        license_directory = temporary / "licenses"
        license_directory.mkdir()
        license_manifest: dict[str, dict[str, object]] = {}
        for name in sorted(license_hashes):
            size, digest = _copy_and_hash(
                licenses[name],
                license_directory / name,
                maximum_size=MAX_LICENSE_BYTES,
            )
            if digest != license_hashes[name]:
                raise BuildError(
                    f"license identity mismatch for {name}: "
                    f"expected {license_hashes[name]}, got {digest}"
                )
            (license_directory / name).chmod(0o644)
            license_manifest[name] = {"sha256": digest, "size": size}

        manifest: dict[str, object] = {
            "schema": SCHEMA,
            "target": TARGET_CONTRACT,
            "source": {
                "repository": SOURCE_REPOSITORY,
                "revision": SOURCE_REVISION,
                "tree": SOURCE_TREE,
                "bazel_version": BAZEL_VERSION,
                "bazeliskrc_sha256": BAZELISKRC_SHA256,
                "module_lock_sha256": MODULE_LOCK_SHA256,
                "overlay_sha256": OVERLAY_SHA256,
            },
            "artifacts": {
                "fcitx5-grimodex-mozc-helper": {
                    "sha256": helper_hash,
                    "size": helper_size,
                },
                "mozc.data": {
                    "sha256": data_hash,
                    "size": data_size,
                },
            },
            "licenses": license_manifest,
        }
        manifest_path = temporary / "manifest.json"
        with manifest_path.open("x", encoding="utf-8", newline="\n") as manifest_file:
            json.dump(manifest, manifest_file, indent=2, sort_keys=True)
            manifest_file.write("\n")
            manifest_file.flush()
            os.fsync(manifest_file.fileno())

        _fsync_directory(license_directory)
        _fsync_directory(temporary)
        if output.exists() or output.is_symlink():
            raise BuildError(f"refusing to replace existing bundle output: {output}")
        os.replace(temporary, output)
        _fsync_directory(output.parent)
        return manifest
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)


def build_fixed_sidecar_bundle(
    *,
    checkout: Path,
    bazel: Path,
    output: Path,
    output_user_root: Path | None,
    profile: str = "b0",
) -> dict[str, object]:
    activate_profile(profile)
    ensure_non_root()
    verify_checkout(checkout)
    checkout = checkout.resolve()
    verify_overlay()
    bazel = verify_bazel_executable(bazel)
    output_resolved = output.expanduser().resolve(strict=False)
    if output_resolved == checkout or checkout in output_resolved.parents:
        raise BuildError("bundle output must not be placed inside the source checkout")
    if output.exists() or output.is_symlink():
        raise BuildError(f"refusing to replace existing bundle output: {output}")
    if output_user_root is not None:
        output_user_root = output_user_root.expanduser().resolve()
        if output_user_root == checkout or checkout in output_user_root.parents:
            raise BuildError("Bazel output_user_root must not be inside the source checkout")
        output_user_root.mkdir(parents=True, exist_ok=True)

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".mozc-sidecar-build-", dir=output.parent
    ) as temporary_directory:
        work = Path(temporary_directory)
        source = _make_isolated_source(checkout, work)
        _apply_overlay(source)
        source_directory = source / "src"
        output_base = _build_with_bazel(
            bazel,
            output_user_root,
            source_directory,
        )
        return emit_bundle(
            output,
            helper=source_directory / HELPER_OUTPUT,
            data=source_directory / DATA_OUTPUT,
            licenses=locate_license_sources(source, output_base),
        )


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--bazel", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--profile",
        choices=PROFILE_NAMES,
        default="b0",
        help="Immutable sidecar profile to build (default: b0)",
    )
    parser.add_argument(
        "--output-user-root",
        type=Path,
        help="Optional persistent Bazel output_user_root for dependency reuse",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        manifest = build_fixed_sidecar_bundle(
            checkout=args.checkout,
            bazel=args.bazel,
            output=args.output,
            output_user_root=args.output_user_root,
            profile=args.profile,
        )
    except (BuildError, OSError) as error:
        print(f"Mozc sidecar bundle build failed: {error}", file=sys.stderr)
        return 1
    helper = manifest["artifacts"]["fcitx5-grimodex-mozc-helper"]
    print(
        f"Wrote verified Mozc sidecar bundle to {args.output} "
        f"(helper sha256={helper['sha256']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
