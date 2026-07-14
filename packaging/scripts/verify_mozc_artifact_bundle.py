#!/usr/bin/env python3
"""Verify and stage the fixed-source Mozc sidecar artifact bundle.

The importer is intentionally strict: the bundle must contain only the
manifest, the two artifacts, and the fixed seven-file license set consumed by
the optional CMake install path. Successful verification stages the exact
bytes that were hashed so CMake does not later install mutable source files
from the input bundle.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
from typing import BinaryIO


SCHEMA = "grimodex.mozc-artifact-bundle.v1"
SOURCE_REPOSITORY = "https://github.com/Masterisk-F/fcitx-mozkey"
SOURCE_REVISION = "462cbbf04886e32096bc318833e974ccc43d9fc8"
SOURCE_TREE = "95365a39134949f5d68f565e1ce451085b5965a8"
BAZEL_VERSION = "9.0.2"
BAZELISKRC_SHA256 = "59acd943a0d15254345f3e176f42786af2b4fba83b1657341cf56e017a7db19a"
MODULE_LOCK_SHA256 = "ab6b647b1c12072eee26ec2370fa928b2ac7c3146e72daf232010dfe254ed972"
OVERLAY_SHA256 = "26cf5430b39dcdc04c1f91a6ce473554c3f1ba3f04c2defdcf146f859b6776d6"
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
MANIFEST_NAME = "manifest.json"
ARTIFACT_NAMES = (
    "fcitx5-grimodex-mozc-helper",
    "mozc.data",
)
FIXED_DATA_SIZE = 18_887_468
FIXED_DATA_SHA256 = "b9884362e37772f772a0d28d1e12622455c14353497b3435deed60aa7e592c5e"
FIXED_HELPER_SIZE = 5_695_048
FIXED_HELPER_SHA256 = "8676275bb47aefe963c8b82047cc66fb7a5140caec72d1ebbfa17556b281577d"
LICENSE_HASHES = {
    "MOZC-LICENSE": "44cdd923b91ea9199293abecc2762c70c87dbf1e581c027a94c416368d1a648c",
    "FCITX-MOZKEY-THIRD-PARTY-NOTICES.md": "e1bc0a70491f19f5acc7edcae23a1aa5c3b317009246837abd04e0f436c87c46",
    "DICTIONARY-OSS-NOTICE.txt": "6b1de66bc6fa30e0b45dc45b8c8f6c57bd78aff923c261a3efffac8eb86f7bac",
    "ABSEIL-LICENSE": "c79a7fea0e3cac04cd43f20e7b648e5a0ff8fa5344e644b0ee09ca1162b62747",
    "PROTOBUF-LICENSE": "6e5e117324afd944dcf67f36cf329843bc1a92229a8cd9bb573d7a83130fea7d",
    "UTF8-RANGE-LICENSE": "02de69b64fc36d9e938f418e52723e42f0b2b226d58a9cb3c8dcbdf7059f5074",
    "JAPANESE-USAGE-DICTIONARY-LICENSE": "91e74c9b189a60a3f5ba13b4aa28f87f25ee9252a64d547784e72752d089631a",
}
MAX_MANIFEST_BYTES = 64 * 1024
MAX_HELPER_BYTES = 64 * 1024 * 1024
MAX_LICENSE_BYTES = 1024 * 1024
COPY_CHUNK_BYTES = 1024 * 1024
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
HOST_RUNTIME_TIMEOUT_SECONDS = 5
STAGED_HELPER_MODE = 0o555
STAGED_FILE_MODE = 0o444
STAGED_DIRECTORY_MODE = 0o755
PREPARED_RUNTIME_DIRECTORY_MODE = 0o555


class BundleVerificationError(RuntimeError):
    """The artifact bundle did not satisfy the import contract."""


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise BundleVerificationError(f"manifest contains duplicate key: {key}")
        result[key] = value
    return result


def _require_exact_keys(
    value: object,
    expected: set[str],
    location: str,
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise BundleVerificationError(f"{location} must be an object")
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        details = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if unknown:
            details.append("unknown=" + ",".join(unknown))
        raise BundleVerificationError(
            f"{location} has invalid keys ({'; '.join(details)})"
        )
    return value


def _read_manifest(bundle_dir: Path) -> tuple[dict[str, object], bytes]:
    manifest_path = bundle_dir / MANIFEST_NAME
    descriptor, _ = _open_regular_file(manifest_path)
    with os.fdopen(descriptor, "rb") as manifest_file:
        manifest_bytes = manifest_file.read(MAX_MANIFEST_BYTES + 1)
    if len(manifest_bytes) > MAX_MANIFEST_BYTES:
        raise BundleVerificationError(
            f"{MANIFEST_NAME} exceeds {MAX_MANIFEST_BYTES} bytes"
        )
    try:
        manifest = json.loads(
            manifest_bytes,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except BundleVerificationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BundleVerificationError(f"invalid JSON in {MANIFEST_NAME}: {error}") from error
    return _require_exact_keys(
        manifest,
        {"schema", "target", "source", "artifacts", "licenses"},
        "manifest",
    ), manifest_bytes


def _validate_entries(
    entries_value: object,
    names: set[str],
    location: str,
) -> dict[str, dict[str, object]]:
    entries = _require_exact_keys(entries_value, names, location)
    validated: dict[str, dict[str, object]] = {}
    for name in sorted(names):
        entry = _require_exact_keys(
            entries[name],
            {"sha256", "size"},
            f"{location}.{name}",
        )
        expected_hash = entry["sha256"]
        expected_size = entry["size"]
        if (
            not isinstance(expected_hash, str)
            or len(expected_hash) != 64
            or any(character not in "0123456789abcdef" for character in expected_hash)
        ):
            raise BundleVerificationError(
                f"{location}.{name}.sha256 must be 64 lowercase hex characters"
            )
        if (
            not isinstance(expected_size, int)
            or isinstance(expected_size, bool)
            or expected_size <= 0
        ):
            raise BundleVerificationError(
                f"{location}.{name}.size must be a positive integer"
            )
        validated[name] = entry
    return validated


def _validate_manifest(
    manifest: dict[str, object],
) -> tuple[dict[str, dict[str, object]], dict[str, dict[str, object]]]:
    if manifest["schema"] != SCHEMA:
        raise BundleVerificationError(
            f"unsupported schema: expected {SCHEMA!r}, got {manifest['schema']!r}"
        )

    target = _require_exact_keys(
        manifest["target"],
        {"system", "architecture", "elf", "runtime"},
        "manifest.target",
    )
    elf = _require_exact_keys(
        target["elf"],
        {"class", "endianness", "machine"},
        "manifest.target.elf",
    )
    runtime = _require_exact_keys(
        target["runtime"],
        {"interpreter", "required_symbol_versions"},
        "manifest.target.runtime",
    )
    required_symbol_versions = _require_exact_keys(
        runtime["required_symbol_versions"],
        {"glibc", "glibcxx", "cxxabi"},
        "manifest.target.runtime.required_symbol_versions",
    )
    if {
        "system": target["system"],
        "architecture": target["architecture"],
        "elf": elf,
        "runtime": {
            "interpreter": runtime["interpreter"],
            "required_symbol_versions": required_symbol_versions,
        },
    } != TARGET_CONTRACT:
        raise BundleVerificationError(
            "manifest target is not the fixed linux-x86_64 runtime ABI"
        )

    source = _require_exact_keys(
        manifest["source"],
        {
            "repository",
            "revision",
            "tree",
            "bazel_version",
            "bazeliskrc_sha256",
            "module_lock_sha256",
            "overlay_sha256",
        },
        "manifest.source",
    )
    if source["repository"] != SOURCE_REPOSITORY:
        raise BundleVerificationError(
            f"unexpected source repository: {source['repository']!r}"
        )
    if source["revision"] != SOURCE_REVISION:
        raise BundleVerificationError(
            f"unexpected source revision: {source['revision']!r}"
        )
    for key, expected in (
        ("tree", SOURCE_TREE),
        ("bazel_version", BAZEL_VERSION),
        ("bazeliskrc_sha256", BAZELISKRC_SHA256),
        ("module_lock_sha256", MODULE_LOCK_SHA256),
        ("overlay_sha256", OVERLAY_SHA256),
    ):
        if source[key] != expected:
            raise BundleVerificationError(
                f"unexpected source {key}: {source[key]!r}"
            )

    artifacts = _validate_entries(
        manifest["artifacts"], set(ARTIFACT_NAMES), "manifest.artifacts"
    )
    helper = artifacts["fcitx5-grimodex-mozc-helper"]
    if (
        helper["sha256"] != FIXED_HELPER_SHA256
        or helper["size"] != FIXED_HELPER_SIZE
    ):
        raise BundleVerificationError(
            "helper identity is not the fixed linux-x86_64 artifact"
        )
    if (
        artifacts["mozc.data"]["sha256"] != FIXED_DATA_SHA256
        or artifacts["mozc.data"]["size"] != FIXED_DATA_SIZE
    ):
        raise BundleVerificationError("mozc.data identity is not the fixed B0 dataset")
    licenses = _validate_entries(
        manifest["licenses"], set(LICENSE_HASHES), "manifest.licenses"
    )
    for name, expected_hash in LICENSE_HASHES.items():
        if licenses[name]["sha256"] != expected_hash:
            raise BundleVerificationError(f"unexpected license identity: {name}")
    return artifacts, licenses


def _open_regular_file(path: Path) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise BundleVerificationError(f"cannot open artifact {path.name}: {error}") from error
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        os.close(descriptor)
        raise BundleVerificationError(f"artifact {path.name} must be a regular file")
    return descriptor, metadata


def _hash_stream(
    source: BinaryIO,
    *,
    maximum_size: int,
    destination: BinaryIO | None = None,
) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    while True:
        chunk = source.read(COPY_CHUNK_BYTES)
        if not chunk:
            break
        if size + len(chunk) > maximum_size:
            raise BundleVerificationError(
                f"input exceeds the {maximum_size}-byte copy limit"
            )
        if destination is not None:
            destination.write(chunk)
        digest.update(chunk)
        size += len(chunk)
    if destination is not None:
        destination.flush()
        os.fsync(destination.fileno())
    return size, digest.hexdigest()


def _validate_bundle_contents(bundle_dir: Path) -> None:
    allowed = {MANIFEST_NAME, *ARTIFACT_NAMES, "licenses"}
    try:
        actual = {entry.name for entry in bundle_dir.iterdir()}
    except OSError as error:
        raise BundleVerificationError(f"cannot list artifact bundle: {error}") from error
    if actual != allowed:
        missing = sorted(allowed - actual)
        unknown = sorted(actual - allowed)
        details = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if unknown:
            details.append("unknown=" + ",".join(unknown))
        raise BundleVerificationError(
            "artifact bundle has invalid contents (" + "; ".join(details) + ")"
        )


def _canonical_manifest_bytes(manifest: dict[str, object]) -> bytes:
    return (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _slice_elf(
    data: bytes,
    offset: int,
    size: int,
    name: str,
    description: str,
) -> bytes:
    if offset < 0 or size < 0 or offset > len(data) or size > len(data) - offset:
        raise BundleVerificationError(f"{name} has an invalid {description} range")
    return data[offset : offset + size]


def _elf_string(table: bytes, offset: int, name: str, description: str) -> str:
    if offset < 0 or offset >= len(table):
        raise BundleVerificationError(
            f"{name} has an invalid {description} string offset"
        )
    end = table.find(b"\0", offset)
    if end < 0:
        raise BundleVerificationError(
            f"{name} has an unterminated {description} string"
        )
    try:
        return table[offset:end].decode("ascii")
    except UnicodeDecodeError as error:
        raise BundleVerificationError(
            f"{name} has a non-ASCII {description} string"
        ) from error


def _symbol_version_key(value: str, prefix: str) -> tuple[int, ...] | None:
    match = re.fullmatch(re.escape(prefix) + r"([0-9]+(?:\.[0-9]+)*)", value)
    if match is None:
        return None
    return tuple(int(component) for component in match.group(1).split("."))


def _read_descriptor(descriptor: int, maximum_size: int, name: str) -> bytes:
    metadata = os.fstat(descriptor)
    if metadata.st_size > maximum_size:
        raise BundleVerificationError(
            f"{name} exceeds the {maximum_size}-byte inspection limit"
        )
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks = []
    remaining = metadata.st_size
    while remaining > 0:
        chunk = os.read(descriptor, min(COPY_CHUNK_BYTES, remaining))
        if not chunk:
            raise BundleVerificationError(f"{name} changed while being inspected")
        chunks.append(chunk)
        remaining -= len(chunk)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return b"".join(chunks)


def _inspect_elf_runtime_contract(data: bytes, name: str) -> dict[str, object]:
    if len(data) < ELF64_HEADER_SIZE or data[:4] != b"\x7fELF":
        raise BundleVerificationError(f"{name} must be an ELF executable")
    if data[4] != ELFCLASS64:
        raise BundleVerificationError(f"{name} must be ELF64")
    if data[5] != ELFDATA2LSB:
        raise BundleVerificationError(f"{name} must be little-endian ELF")
    if data[6] != EV_CURRENT:
        raise BundleVerificationError(f"{name} has an unsupported ELF version")
    if struct.unpack_from("<H", data, 18)[0] != EM_X86_64:
        raise BundleVerificationError(f"{name} must target EM_X86_64")

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
        raise BundleVerificationError(f"{name} is missing required ELF tables")

    interpreters: list[str] = []
    for index in range(program_count):
        entry = _slice_elf(
            data,
            program_offset + index * program_entry_size,
            ELF64_PROGRAM_HEADER_SIZE,
            name,
            "program header",
        )
        if struct.unpack_from("<I", entry, 0)[0] != PT_INTERP:
            continue
        payload = _slice_elf(
            data,
            struct.unpack_from("<Q", entry, 8)[0],
            struct.unpack_from("<Q", entry, 32)[0],
            name,
            "interpreter",
        )
        if not payload.endswith(b"\0") or payload.count(b"\0") != 1:
            raise BundleVerificationError(f"{name} has an invalid ELF interpreter")
        try:
            interpreters.append(payload[:-1].decode("ascii"))
        except UnicodeDecodeError as error:
            raise BundleVerificationError(
                f"{name} has a non-ASCII ELF interpreter"
            ) from error
    if len(interpreters) != 1:
        raise BundleVerificationError(
            f"{name} must declare exactly one ELF interpreter"
        )

    sections: list[tuple[int, int, int, int, int]] = []
    for index in range(section_count):
        entry = _slice_elf(
            data,
            section_offset + index * section_entry_size,
            ELF64_SECTION_HEADER_SIZE,
            name,
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
        raise BundleVerificationError(
            f"{name} must contain one GNU version requirement table"
        )
    (
        _,
        requirement_offset,
        requirement_size,
        string_table_index,
        requirement_count,
    ) = version_sections[0]
    if string_table_index >= len(sections):
        raise BundleVerificationError(
            f"{name} has an invalid GNU version string table"
        )
    string_type, string_offset, string_size, _, _ = sections[string_table_index]
    if string_type != SHT_STRTAB:
        raise BundleVerificationError(
            f"{name} GNU version names must use a string table"
        )
    strings = _slice_elf(
        data, string_offset, string_size, name, "dynamic string table"
    )
    requirements = _slice_elf(
        data,
        requirement_offset,
        requirement_size,
        name,
        "GNU version requirement table",
    )
    structure_size = 16
    work_budget = len(requirements) // structure_size
    if requirement_count == 0 or requirement_count > work_budget:
        raise BundleVerificationError(
            f"{name} GNU version chain exceeds its work budget"
        )

    symbol_versions: set[str] = set()
    requirement_cursor = 0
    visited_structures: set[int] = set()
    remaining_work = work_budget
    for requirement_index in range(requirement_count):
        if requirement_cursor in visited_structures or remaining_work == 0:
            raise BundleVerificationError(
                f"{name} has a non-monotonic GNU requirement chain"
            )
        visited_structures.add(requirement_cursor)
        remaining_work -= 1
        entry = _slice_elf(
            requirements,
            requirement_cursor,
            structure_size,
            name,
            "GNU version requirement",
        )
        version, auxiliary_count, _, auxiliary_offset, next_offset = struct.unpack_from(
            "<HHIII", entry, 0
        )
        if version != ELF_VERSION_REQUIREMENT or auxiliary_count == 0:
            raise BundleVerificationError(
                f"{name} has an invalid GNU version requirement"
            )
        if auxiliary_offset < structure_size:
            raise BundleVerificationError(
                f"{name} has an invalid GNU version chain"
            )
        if auxiliary_count > remaining_work:
            raise BundleVerificationError(
                f"{name} GNU version chain exceeds its work budget"
            )
        auxiliary_cursor = requirement_cursor + auxiliary_offset
        for auxiliary_index in range(auxiliary_count):
            if auxiliary_cursor in visited_structures or remaining_work == 0:
                raise BundleVerificationError(
                    f"{name} has a non-monotonic GNU version chain"
                )
            visited_structures.add(auxiliary_cursor)
            remaining_work -= 1
            auxiliary = _slice_elf(
                requirements,
                auxiliary_cursor,
                structure_size,
                name,
                "GNU version auxiliary entry",
            )
            _, _, _, name_offset, auxiliary_next = struct.unpack_from(
                "<IHHII", auxiliary, 0
            )
            symbol_versions.add(
                _elf_string(
                    strings,
                    name_offset,
                    name,
                    "GNU version requirement",
                )
            )
            if auxiliary_index + 1 < auxiliary_count:
                if auxiliary_next < structure_size:
                    raise BundleVerificationError(
                        f"{name} has an invalid GNU version chain"
                    )
                auxiliary_cursor += auxiliary_next
            elif auxiliary_next != 0:
                raise BundleVerificationError(
                    f"{name} has an unterminated GNU version chain"
                )
        if requirement_index + 1 < requirement_count:
            if next_offset < structure_size:
                raise BundleVerificationError(
                    f"{name} has an invalid GNU requirement chain"
                )
            requirement_cursor += next_offset
            if requirement_cursor >= len(requirements):
                raise BundleVerificationError(
                    f"{name} GNU requirement chain is out of range"
                )
        elif next_offset != 0:
            raise BundleVerificationError(
                f"{name} has an unterminated GNU requirement chain"
            )

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
            raise BundleVerificationError(
                f"{name} has no {prefix} runtime requirement"
            )
        actual_versions[key] = max(
            matching,
            key=lambda value: _symbol_version_key(value, prefix),
        )
    runtime = {
        "interpreter": interpreters[0],
        "required_symbol_versions": actual_versions,
    }
    if runtime != TARGET_CONTRACT["runtime"]:
        raise BundleVerificationError(
            f"{name} runtime ABI does not match the fixed contract: "
            f"expected {TARGET_CONTRACT['runtime']}, got {runtime}"
        )
    return runtime


def _require_linux_x86_64_elf(descriptor: int, name: str) -> None:
    _inspect_elf_runtime_contract(
        _read_descriptor(descriptor, MAX_HELPER_BYTES, name),
        name,
    )


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validate_license_directory(licenses_dir: Path, *, location: str) -> None:
    if licenses_dir.is_symlink() or not licenses_dir.is_dir():
        raise BundleVerificationError(
            f"{location} must be a regular, non-symlink directory"
        )
    try:
        license_names = {entry.name for entry in licenses_dir.iterdir()}
    except OSError as error:
        raise BundleVerificationError(f"cannot list {location}: {error}") from error
    if license_names != set(LICENSE_HASHES):
        raise BundleVerificationError(f"{location} does not match the required set")


def _stage_tree_record(
    relative_path: str,
    metadata: os.stat_result,
    size: int,
    digest: str,
) -> dict[str, object]:
    return {
        "path": relative_path,
        "mode": stat.S_IMODE(metadata.st_mode),
        "size": size,
        "sha256": digest,
    }


def _content_identifier(records: list[dict[str, object]]) -> str:
    payload = json.dumps(
        sorted(records, key=lambda entry: str(entry["path"])),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256-" + hashlib.sha256(payload).hexdigest()


def verify_staged_generation(
    generation_dir: Path,
    *,
    require_generation_name: bool = True,
) -> str:
    """Revalidate an immutable staged generation without repairing it."""
    if generation_dir.is_symlink() or not generation_dir.is_dir():
        raise BundleVerificationError(
            "staged generation must be a regular, non-symlink directory"
        )
    _validate_bundle_contents(generation_dir)
    manifest, manifest_bytes = _read_manifest(generation_dir)
    artifact_entries, license_entries = _validate_manifest(manifest)
    canonical_manifest = _canonical_manifest_bytes(manifest)
    if manifest_bytes != canonical_manifest:
        raise BundleVerificationError("staged manifest is not canonical")
    licenses_dir = generation_dir / "licenses"
    _validate_license_directory(licenses_dir, location="staged licenses")
    if stat.S_IMODE(licenses_dir.stat(follow_symlinks=False).st_mode) != STAGED_DIRECTORY_MODE:
        raise BundleVerificationError("staged licenses directory has an invalid mode")

    records: list[dict[str, object]] = []
    for artifact_name in ARTIFACT_NAMES:
        path = generation_dir / artifact_name
        descriptor, metadata = _open_regular_file(path)
        try:
            if metadata.st_nlink != 1:
                raise BundleVerificationError(
                    f"staged artifact {artifact_name} must not be hard-linked"
                )
            expected_mode = (
                STAGED_HELPER_MODE
                if artifact_name == "fcitx5-grimodex-mozc-helper"
                else STAGED_FILE_MODE
            )
            if stat.S_IMODE(metadata.st_mode) != expected_mode:
                raise BundleVerificationError(
                    f"staged artifact {artifact_name} has an invalid mode"
                )
            with os.fdopen(os.dup(descriptor), "rb") as source:
                size, digest = _hash_stream(
                    source,
                    maximum_size=(
                        MAX_HELPER_BYTES
                        if artifact_name == "fcitx5-grimodex-mozc-helper"
                        else FIXED_DATA_SIZE
                    ),
                )
            expected = artifact_entries[artifact_name]
            if size != expected["size"] or digest != expected["sha256"]:
                raise BundleVerificationError(
                    f"staged artifact identity mismatch: {artifact_name}"
                )
            if artifact_name == "fcitx5-grimodex-mozc-helper":
                _require_linux_x86_64_elf(descriptor, artifact_name)
        finally:
            os.close(descriptor)
        records.append(_stage_tree_record(artifact_name, metadata, size, digest))

    manifest_metadata = (generation_dir / MANIFEST_NAME).stat(follow_symlinks=False)
    if manifest_metadata.st_nlink != 1:
        raise BundleVerificationError("staged manifest must not be hard-linked")
    if stat.S_IMODE(manifest_metadata.st_mode) != STAGED_FILE_MODE:
        raise BundleVerificationError("staged manifest has an invalid mode")
    records.append(
        _stage_tree_record(
            MANIFEST_NAME,
            manifest_metadata,
            len(manifest_bytes),
            hashlib.sha256(manifest_bytes).hexdigest(),
        )
    )

    for license_name in sorted(LICENSE_HASHES):
        path = licenses_dir / license_name
        descriptor, metadata = _open_regular_file(path)
        try:
            if metadata.st_nlink != 1:
                raise BundleVerificationError(
                    f"staged license {license_name} must not be hard-linked"
                )
            if stat.S_IMODE(metadata.st_mode) != STAGED_FILE_MODE:
                raise BundleVerificationError(
                    f"staged license {license_name} has an invalid mode"
                )
            with os.fdopen(os.dup(descriptor), "rb") as source:
                size, digest = _hash_stream(
                    source,
                    maximum_size=MAX_LICENSE_BYTES,
                )
        finally:
            os.close(descriptor)
        expected = license_entries[license_name]
        if size != expected["size"] or digest != expected["sha256"]:
            raise BundleVerificationError(
                f"staged license identity mismatch: {license_name}"
            )
        records.append(
            _stage_tree_record(f"licenses/{license_name}", metadata, size, digest)
        )

    records.append(
        {
            "path": "licenses/",
            "mode": STAGED_DIRECTORY_MODE,
            "size": 0,
            "sha256": "",
        }
    )
    identifier = _content_identifier(records)
    if require_generation_name and generation_dir.name != identifier:
        raise BundleVerificationError(
            f"staged generation name mismatch: expected {identifier}"
        )
    return identifier


def _run_fixed_helper_ping(helper: Path, data: Path) -> None:
    request_payload = b"\x08\x01\x10\x01\x18\x02"
    request_frame = struct.pack(">I", len(request_payload)) + request_payload
    response_payload = (
        b"\x08\x01\x10\x01\x18\x01\x3a\x40"
        + FIXED_DATA_SHA256.encode("ascii")
    )
    expected_response = struct.pack(">I", len(response_payload)) + response_payload
    try:
        with tempfile.TemporaryDirectory(prefix="grimodex-mozc-runtime-") as runtime:
            result = subprocess.run(
                [
                    str(helper),
                    f"--data_file={data}",
                    f"--dataset_sha256={FIXED_DATA_SHA256}",
                ],
                input=request_frame,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=HOST_RUNTIME_TIMEOUT_SECONDS,
                env={
                    "HOME": runtime,
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                    "PATH": "/usr/bin:/bin",
                    "TMPDIR": runtime,
                },
            )
    except subprocess.TimeoutExpired as error:
        raise BundleVerificationError(
            "fixed helper runtime PING timed out"
        ) from error
    except OSError as error:
        raise BundleVerificationError(
            f"fixed helper cannot start on this host: {error}"
        ) from error
    if result.returncode != 0:
        raise BundleVerificationError(
            "fixed helper cannot load or initialize on this host "
            f"(exit {result.returncode}); required runtime is "
            "GLIBC_2.38, GLIBCXX_3.4.32, CXXABI_1.3.15 with "
            "/lib64/ld-linux-x86-64.so.2"
        )
    if result.stdout != expected_response:
        raise BundleVerificationError(
            "fixed helper runtime PING returned an invalid response"
        )


def verify_host_runtime(generation_dir: Path) -> str:
    """Prove that the fixed staged helper can complete its private PING."""
    identifier = verify_staged_generation(generation_dir)
    _run_fixed_helper_ping(
        generation_dir / "fcitx5-grimodex-mozc-helper",
        generation_dir / "mozc.data",
    )
    return identifier


def _copy_fixed_runtime_artifact(
    source_path: Path,
    destination_path: Path,
    *,
    expected_size: int,
    expected_sha256: str,
    maximum_size: int,
    executable: bool,
) -> None:
    descriptor, metadata = _open_regular_file(source_path)
    try:
        if metadata.st_size != expected_size:
            raise BundleVerificationError(
                f"installed artifact identity mismatch: {source_path.name}"
            )
        if executable and not (metadata.st_mode & 0o111):
            raise BundleVerificationError(
                f"installed helper is not executable: {source_path}"
            )
        with os.fdopen(os.dup(descriptor), "rb") as source, destination_path.open(
            "xb"
        ) as destination:
            actual_size, actual_sha256 = _hash_stream(
                source,
                maximum_size=maximum_size,
                destination=destination,
            )
    finally:
        os.close(descriptor)
    if actual_size != expected_size or actual_sha256 != expected_sha256:
        raise BundleVerificationError(
            f"installed artifact identity mismatch: {source_path.name}"
        )
    destination_path.chmod(0o500 if executable else 0o400)


def verify_installed_runtime(helper_path: Path, data_path: Path) -> str:
    """Copy, identify, ABI-check, and PING installed fixed artifacts."""
    with tempfile.TemporaryDirectory(prefix="grimodex-mozc-installed-") as temporary:
        runtime_dir = Path(temporary)
        helper = runtime_dir / "fcitx5-grimodex-mozc-helper"
        data = runtime_dir / "mozc.data"
        _copy_fixed_runtime_artifact(
            helper_path,
            helper,
            expected_size=FIXED_HELPER_SIZE,
            expected_sha256=FIXED_HELPER_SHA256,
            maximum_size=MAX_HELPER_BYTES,
            executable=True,
        )
        _copy_fixed_runtime_artifact(
            data_path,
            data,
            expected_size=FIXED_DATA_SIZE,
            expected_sha256=FIXED_DATA_SHA256,
            maximum_size=FIXED_DATA_SIZE,
            executable=False,
        )
        descriptor, _ = _open_regular_file(helper)
        try:
            _require_linux_x86_64_elf(descriptor, helper.name)
        finally:
            os.close(descriptor)
        _run_fixed_helper_ping(helper, data)
    return FIXED_DATA_SHA256


def _prepared_runtime_identifier() -> str:
    return _content_identifier(
        [
            {
                "path": "fcitx5-grimodex-mozc-helper",
                "mode": STAGED_HELPER_MODE,
                "size": FIXED_HELPER_SIZE,
                "sha256": FIXED_HELPER_SHA256,
            },
            {
                "path": "mozc.data",
                "mode": STAGED_FILE_MODE,
                "size": FIXED_DATA_SIZE,
                "sha256": FIXED_DATA_SHA256,
            },
        ]
    )


def verify_prepared_runtime(
    generation_dir: Path,
    *,
    require_generation_name: bool = True,
) -> str:
    if generation_dir.is_symlink() or not generation_dir.is_dir():
        raise BundleVerificationError(
            "prepared runtime must be a regular, non-symlink directory"
        )
    if stat.S_IMODE(generation_dir.stat(follow_symlinks=False).st_mode) != (
        PREPARED_RUNTIME_DIRECTORY_MODE
    ):
        raise BundleVerificationError("prepared runtime has an invalid mode")
    try:
        contents = {entry.name for entry in generation_dir.iterdir()}
    except OSError as error:
        raise BundleVerificationError(
            f"cannot list prepared runtime: {error}"
        ) from error
    if contents != set(ARTIFACT_NAMES):
        raise BundleVerificationError("prepared runtime has invalid contents")

    expected_artifacts = {
        "fcitx5-grimodex-mozc-helper": (
            STAGED_HELPER_MODE,
            FIXED_HELPER_SIZE,
            FIXED_HELPER_SHA256,
            MAX_HELPER_BYTES,
        ),
        "mozc.data": (
            STAGED_FILE_MODE,
            FIXED_DATA_SIZE,
            FIXED_DATA_SHA256,
            FIXED_DATA_SIZE,
        ),
    }
    for name, (expected_mode, expected_size, expected_hash, maximum_size) in (
        expected_artifacts.items()
    ):
        descriptor, metadata = _open_regular_file(generation_dir / name)
        try:
            if metadata.st_nlink != 1:
                raise BundleVerificationError(
                    f"prepared runtime artifact must not be hard-linked: {name}"
                )
            if stat.S_IMODE(metadata.st_mode) != expected_mode:
                raise BundleVerificationError(
                    f"prepared runtime artifact has an invalid mode: {name}"
                )
            if metadata.st_size != expected_size:
                raise BundleVerificationError(
                    f"prepared runtime artifact identity mismatch: {name}"
                )
            with os.fdopen(os.dup(descriptor), "rb") as source:
                actual_size, actual_hash = _hash_stream(
                    source,
                    maximum_size=maximum_size,
                )
        finally:
            os.close(descriptor)
        if actual_size != expected_size or actual_hash != expected_hash:
            raise BundleVerificationError(
                f"prepared runtime artifact identity mismatch: {name}"
            )

    helper = generation_dir / "fcitx5-grimodex-mozc-helper"
    data = generation_dir / "mozc.data"
    descriptor, _ = _open_regular_file(helper)
    try:
        _require_linux_x86_64_elf(descriptor, helper.name)
    finally:
        os.close(descriptor)
    identifier = _prepared_runtime_identifier()
    if require_generation_name and generation_dir.name != identifier:
        raise BundleVerificationError(
            f"prepared runtime name mismatch: expected {identifier}"
        )
    _run_fixed_helper_ping(helper, data)
    return identifier


def _prepare_runtime_root(runtime_root: Path) -> Path:
    if runtime_root.is_symlink():
        raise BundleVerificationError("runtime root must not be a symlink")
    root = runtime_root.expanduser().resolve(strict=False)
    created = not root.exists()
    try:
        root.mkdir(parents=True, mode=0o700, exist_ok=True)
    except OSError as error:
        raise BundleVerificationError(f"cannot create runtime root: {error}") from error
    if created:
        root.chmod(0o700)
    metadata = root.stat(follow_symlinks=False)
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise BundleVerificationError("runtime root must be a user-owned directory")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise BundleVerificationError("runtime root must have mode 0700")
    return root


def prepare_installed_runtime(
    helper_path: Path,
    data_path: Path,
    runtime_root: Path,
) -> Path:
    root = _prepare_runtime_root(runtime_root)
    identifier = _prepared_runtime_identifier()
    generation = root / identifier
    if generation.exists() or generation.is_symlink():
        verify_prepared_runtime(generation)
        return generation

    temporary_dir = Path(
        tempfile.mkdtemp(prefix=".mozc-runtime-", dir=root)
    )
    try:
        _copy_fixed_runtime_artifact(
            helper_path,
            temporary_dir / "fcitx5-grimodex-mozc-helper",
            expected_size=FIXED_HELPER_SIZE,
            expected_sha256=FIXED_HELPER_SHA256,
            maximum_size=MAX_HELPER_BYTES,
            executable=True,
        )
        _copy_fixed_runtime_artifact(
            data_path,
            temporary_dir / "mozc.data",
            expected_size=FIXED_DATA_SIZE,
            expected_sha256=FIXED_DATA_SHA256,
            maximum_size=FIXED_DATA_SIZE,
            executable=False,
        )
        (temporary_dir / "fcitx5-grimodex-mozc-helper").chmod(
            STAGED_HELPER_MODE
        )
        (temporary_dir / "mozc.data").chmod(STAGED_FILE_MODE)
        temporary_dir.chmod(PREPARED_RUNTIME_DIRECTORY_MODE)
        verify_prepared_runtime(temporary_dir, require_generation_name=False)
        try:
            temporary_dir.rename(generation)
        except OSError as error:
            if error.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                raise
            verify_prepared_runtime(generation)
            return generation
        _fsync_directory(root)
        verify_prepared_runtime(generation)
        return generation
    finally:
        if temporary_dir.exists():
            _make_cleanup_writable(temporary_dir)
            shutil.rmtree(temporary_dir, ignore_errors=True)


def _make_cleanup_writable(path: Path) -> None:
    if not path.exists() or path.is_symlink():
        return
    licenses = path / "licenses"
    if licenses.is_dir() and not licenses.is_symlink():
        licenses.chmod(0o700)
    path.chmod(0o700)


def verify_and_stage(bundle_dir: Path, stage_root: Path) -> Path:
    if bundle_dir.is_symlink() or not bundle_dir.is_dir():
        raise BundleVerificationError(
            "artifact bundle must be a regular, non-symlink directory"
        )
    _validate_bundle_contents(bundle_dir)
    manifest, _ = _read_manifest(bundle_dir)
    artifact_entries, license_entries = _validate_manifest(manifest)

    bundle_resolved = bundle_dir.resolve()
    if stage_root.is_symlink():
        raise BundleVerificationError("staging root must not be a symlink")
    stage_resolved = stage_root.expanduser().resolve(strict=False)
    if (
        stage_resolved == bundle_resolved
        or bundle_resolved in stage_resolved.parents
        or stage_resolved in bundle_resolved.parents
    ):
        raise BundleVerificationError(
            "staging directory and bundle directory must not overlap"
        )

    licenses_dir = bundle_dir / "licenses"
    _validate_license_directory(licenses_dir, location="bundle licenses")

    stage_resolved.parent.mkdir(parents=True, exist_ok=True)
    if stage_resolved.exists() and not stage_resolved.is_dir():
        raise BundleVerificationError(
            "staging root must be a regular directory"
        )
    stage_resolved.mkdir(parents=True, exist_ok=True)

    temporary_dir = Path(
        tempfile.mkdtemp(prefix=".mozc-artifacts-", dir=stage_resolved)
    )
    try:
        for artifact_name in ARTIFACT_NAMES:
            source_path = bundle_dir / artifact_name
            if source_path.is_symlink():
                raise BundleVerificationError(
                    f"artifact {artifact_name} must not be a symlink"
                )
            descriptor, metadata = _open_regular_file(source_path)
            if artifact_name == "fcitx5-grimodex-mozc-helper" and not (
                metadata.st_mode & 0o111
            ):
                os.close(descriptor)
                raise BundleVerificationError(
                    "fcitx5-grimodex-mozc-helper must have an executable bit"
                )
            staged_path = temporary_dir / artifact_name
            with os.fdopen(descriptor, "rb") as source, staged_path.open("wb") as output:
                actual_size, actual_hash = _hash_stream(
                    source,
                    maximum_size=(
                        MAX_HELPER_BYTES
                        if artifact_name == "fcitx5-grimodex-mozc-helper"
                        else FIXED_DATA_SIZE
                    ),
                    destination=output,
                )
            expected = artifact_entries[artifact_name]
            if actual_size != expected["size"]:
                raise BundleVerificationError(
                    f"size mismatch for {artifact_name}: "
                    f"expected {expected['size']}, got {actual_size}"
                )
            if actual_hash != expected["sha256"]:
                raise BundleVerificationError(
                    f"SHA-256 mismatch for {artifact_name}: "
                    f"expected {expected['sha256']}, got {actual_hash}"
                )
            if artifact_name == "fcitx5-grimodex-mozc-helper":
                staged_descriptor, _ = _open_regular_file(staged_path)
                try:
                    _require_linux_x86_64_elf(staged_descriptor, artifact_name)
                finally:
                    os.close(staged_descriptor)
            staged_path.chmod(
                STAGED_HELPER_MODE
                if artifact_name == "fcitx5-grimodex-mozc-helper"
                else STAGED_FILE_MODE
            )
            _fsync_file(staged_path)

        staged_licenses = temporary_dir / "licenses"
        staged_licenses.mkdir()
        for license_name in sorted(LICENSE_HASHES):
            source_path = licenses_dir / license_name
            if source_path.is_symlink():
                raise BundleVerificationError(
                    f"license {license_name} must not be a symlink"
                )
            descriptor, _ = _open_regular_file(source_path)
            staged_path = staged_licenses / license_name
            with os.fdopen(descriptor, "rb") as source, staged_path.open("wb") as output:
                actual_size, actual_hash = _hash_stream(
                    source,
                    maximum_size=MAX_LICENSE_BYTES,
                    destination=output,
                )
            expected = license_entries[license_name]
            if actual_size != expected["size"] or actual_hash != expected["sha256"]:
                raise BundleVerificationError(f"identity mismatch for license {license_name}")
            staged_path.chmod(STAGED_FILE_MODE)
            _fsync_file(staged_path)

        manifest_path = temporary_dir / MANIFEST_NAME
        with manifest_path.open("xb") as manifest_file:
            manifest_file.write(_canonical_manifest_bytes(manifest))
            manifest_file.flush()
            os.fsync(manifest_file.fileno())
        manifest_path.chmod(STAGED_FILE_MODE)
        _fsync_file(manifest_path)
        staged_licenses.chmod(STAGED_DIRECTORY_MODE)
        _fsync_directory(staged_licenses)
        _fsync_directory(temporary_dir)

        identifier = verify_staged_generation(
            temporary_dir,
            require_generation_name=False,
        )
        generation_dir = stage_resolved / identifier
        if generation_dir.exists() or generation_dir.is_symlink():
            verify_staged_generation(generation_dir)
            return generation_dir

        temporary_dir.chmod(STAGED_DIRECTORY_MODE)
        _fsync_directory(temporary_dir)
        try:
            os.rename(temporary_dir, generation_dir)
        except OSError as error:
            if error.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                raise
            verify_staged_generation(generation_dir)
            return generation_dir
        _fsync_directory(stage_resolved)
        verify_staged_generation(generation_dir)
        return generation_dir
    finally:
        if temporary_dir.exists():
            _make_cleanup_writable(temporary_dir)
            shutil.rmtree(temporary_dir, ignore_errors=True)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--bundle", type=Path)
    action.add_argument("--verify-only", type=Path)
    action.add_argument("--verify-host-runtime", type=Path)
    action.add_argument("--verify-installed-runtime", action="store_true")
    action.add_argument("--prepare-installed-runtime", action="store_true")
    parser.add_argument("--stage-root", type=Path)
    parser.add_argument("--runtime-root", type=Path)
    parser.add_argument("--helper", type=Path)
    parser.add_argument("--data", type=Path)
    args = parser.parse_args(argv)
    if args.bundle is not None and args.stage_root is None:
        parser.error("--bundle requires --stage-root")
    if args.verify_only is not None and args.stage_root is not None:
        parser.error("--verify-only does not accept --stage-root")
    if args.verify_host_runtime is not None and args.stage_root is not None:
        parser.error("--verify-host-runtime does not accept --stage-root")
    if args.verify_installed_runtime:
        if args.stage_root is not None:
            parser.error("--verify-installed-runtime does not accept --stage-root")
        if args.helper is None or args.data is None:
            parser.error("--verify-installed-runtime requires --helper and --data")
        if args.runtime_root is not None:
            parser.error("--verify-installed-runtime does not accept --runtime-root")
    elif args.prepare_installed_runtime:
        if args.stage_root is not None:
            parser.error("--prepare-installed-runtime does not accept --stage-root")
        if args.helper is None or args.data is None or args.runtime_root is None:
            parser.error(
                "--prepare-installed-runtime requires --helper, --data, and --runtime-root"
            )
    elif args.helper is not None or args.data is not None:
        parser.error(
            "--helper and --data require an installed-runtime action"
        )
    elif args.runtime_root is not None:
        parser.error("--runtime-root requires --prepare-installed-runtime")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.verify_only is not None:
            verify_staged_generation(args.verify_only)
            generation = args.verify_only.resolve()
        elif args.verify_host_runtime is not None:
            verify_host_runtime(args.verify_host_runtime)
            generation = args.verify_host_runtime.resolve()
        elif args.verify_installed_runtime:
            verify_installed_runtime(args.helper, args.data)
            generation = args.helper.resolve()
        elif args.prepare_installed_runtime:
            generation = prepare_installed_runtime(
                args.helper,
                args.data,
                args.runtime_root,
            )
        else:
            generation = verify_and_stage(args.bundle, args.stage_root)
    except (BundleVerificationError, OSError) as error:
        print(f"Mozc artifact verification failed: {error}", file=sys.stderr)
        return 1
    print(generation)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
