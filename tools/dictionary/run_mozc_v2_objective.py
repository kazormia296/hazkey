#!/usr/bin/env python3
"""Acquire the frozen Mozc v2 Hazkey-versus-B0 objective-quality evidence.

This is deliberately separate from ``run_mozc_b0_measurement.py``.  The v1
runner owns the eight-run pilot performance contract; this runner owns one
quality-only Hazkey run followed by one B0 run over the sealed 1,360-case v2
holdout.  It snapshots every runtime input, validates the complete raw result
contract, scores only the 1,260 quality cases for relative metrics, and
publishes one no-replace evidence directory.
"""

from __future__ import annotations

import argparse
import ctypes
from dataclasses import dataclass
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import time
from typing import Any, Iterable

if __package__:
    from . import evaluate_conversion_quality
    from . import run_mozc_b0_measurement as v1_acquisition
    from . import summarize_ab_probe
else:
    import evaluate_conversion_quality  # type: ignore[no-redef]
    import run_mozc_b0_measurement as v1_acquisition  # type: ignore[no-redef]
    import summarize_ab_probe  # type: ignore[no-redef]


ACQUISITION_SCHEMA = "hazkey.mozc-v2-objective-acquisition.v1"
OBJECTIVE_SCHEMA = "hazkey.mozc-v2-objective-quality.v1"
SEALED_POLICY_SCHEMA = "hazkey.mozc-adoption-corpus-policy.v2"
SEALED_MANIFEST_SCHEMA = "hazkey.frozen-conversion-corpus-manifest.v2"
RAW_SCHEMA = summarize_ab_probe.INPUT_SCHEMA_V3
QUALITY_REPORT_SCHEMA = "hazkey.conversion-quality-report.v1"

SEALED_POLICY_NAME = "corpus-policy.json"
SEALED_MANIFEST_NAME = "manifest.json"
SEALED_CORPUS_NAME = "formal-corpus.tsv"
ACQUISITION_MANIFEST_NAME = "acquisition-manifest.json"
OBJECTIVE_REPORT_NAME = "objective-quality.json"
SNAPSHOT_ROOT_NAME = "runtime"
INPUT_ROOT_NAME = "inputs"
SEALED_SNAPSHOT_NAME = "sealed"
DICTIONARY_SNAPSHOT_NAME = "Dictionary"
B0_SNAPSHOT_NAME = "B0"
SNAPSHOT_EXECUTABLE_ARG = "./runtime/hazkey-server"
SNAPSHOT_LIBRARY_ARGUMENT = "./runtime/lib"
SNAPSHOT_CORPUS_ARGUMENT = "./inputs/sealed/formal-corpus.tsv"
SNAPSHOT_DICTIONARY_ARGUMENT = "./inputs/Dictionary"

TOTAL_CASES = 1_360
QUALITY_CASES = 1_260
TOP_K = 10
WARMUPS = 0
ITERATIONS = 1
PER_RUN_TIMEOUT_SECONDS = 900
TERMINATION_GRACE_SECONDS = 5
RUN_SEQUENCE = (("H0", "Hazkey", "hazkey"), ("B0", "B0", "mozc"))
QUALITY_CATEGORY_COUNTS = {
    "technical-mixed": 240,
    "proper-noun": 200,
    "colloquial": 200,
    "homophone-context": 200,
    "long-structural": 200,
    "grimodex-regression": 220,
}
ALL_CATEGORY_COUNTS = QUALITY_CATEGORY_COUNTS | {"protected": 100}
MINIMUM_TOP1_DELTA_HITS = -100
MINIMUM_TOP10_DELTA_HITS = -151
MINIMUM_CATEGORY_TOP1_DELTA_HITS = {
    "technical-mixed": -24,
    "proper-noun": -20,
    "colloquial": -20,
    "homophone-context": -20,
    "long-structural": -20,
    "grimodex-regression": -22,
}
PROTECTED_REQUIRED = 100
AT_FDCWD = -100
RENAME_NOREPLACE = 1

PYTHON_SOURCE_SNAPSHOT_NAME = "python-sources"
PYTHON_SOURCE_BINDINGS = {
    "producer": ("run_mozc_v2_objective.py", "tools/dictionary/run_mozc_v2_objective.py"),
    "v1_acquisition": (
        "run_mozc_b0_measurement.py",
        "tools/dictionary/run_mozc_b0_measurement.py",
    ),
    "quality_evaluator": (
        "evaluate_conversion_quality.py",
        "tools/dictionary/evaluate_conversion_quality.py",
    ),
    "probe_summarizer": (
        "summarize_ab_probe.py",
        "tools/dictionary/summarize_ab_probe.py",
    ),
}

CHILD_ENVIRONMENT = {
    "GGML_BACKEND_DIR": SNAPSHOT_LIBRARY_ARGUMENT,
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "LD_LIBRARY_PATH": SNAPSHOT_LIBRARY_ARGUMENT,
    "PATH": os.defpath,
    "TZ": "UTC",
}


@dataclass(frozen=True)
class CandidateFreeze:
    generation: str
    helper_size_bytes: int
    helper_sha256: str
    data_size_bytes: int
    data_sha256: str
    manifest_sha256: str
    resource_fingerprint: str


@dataclass(frozen=True)
class FrozenContract:
    sealed_generation: str
    policy_sha256: str
    manifest_sha256: str
    corpus_sha256: str
    product_source_ref: str
    executable_size_bytes: int
    executable_sha256: str
    runtime_dependencies_integrity: str
    hazkey_dictionary_fingerprint: str
    b0: CandidateFreeze
    b1: CandidateFreeze


@dataclass(frozen=True)
class SourceBinding:
    key: str
    source_path: Path
    repository_path: str
    snapshot_name: str
    data: bytes
    mode: int
    device: int
    inode: int


@dataclass(frozen=True)
class TreeEntry:
    kind: str
    mode: int
    size_bytes: int | None = None
    sha256: str | None = None


class EvidenceCommittedError(OSError):
    """Publication renamed the evidence tree, but final assurance failed."""

    def __init__(self, output: Path, detail: str) -> None:
        super().__init__(
            f"evidence committed at {output}, but publication assurance failed: {detail}"
        )
        self.output = output
        self.committed = True


FIXED_CONTRACT = FrozenContract(
    sealed_generation=(
        "sealed-v2-sha256-"
        "b4c1351b1b0ef7797349ebf26858db4d0dd69ce1c8bcbfaee88e0f0b644225ed"
    ),
    policy_sha256=(
        "sha256:7b0a8e8ddcc9f8d2bfffd7dac6f365d7d5b1cf4ff42b92ba9fc4c99fce7f9220"
    ),
    manifest_sha256=(
        "sha256:3ccefa5552d1c0d851b07cc1ed8f65983dd7db019d9250509f2467af7bfd1c02"
    ),
    corpus_sha256=(
        "sha256:cdb2a017b4548f6f77ec3d466f84ec09268a74adb5e876e224e01069f128c8ae"
    ),
    product_source_ref="7373b1a59b2c94a9fada5650984c28ed352c3be1",
    executable_size_bytes=106_269_232,
    executable_sha256=(
        "sha256:249c43c8eb02651b685291ad47fd6bd85efac3438abd0a4d284dd1caec11f30a"
    ),
    runtime_dependencies_integrity=(
        "sha256:5d847919dbfb4b866546104cfbc73f5ffa9ff45ee9d8bc85889bf1de6c299f2d"
    ),
    hazkey_dictionary_fingerprint=(
        "sha256:cee9210b8dc92a30e8b4e600c416db70a51fed1199f6b4c3659aba821ef4024c"
    ),
    b0=CandidateFreeze(
        generation=(
            "sha256-"
            "ad277af2ad5a634f23c7b84b7f346b02f341905f10fcfa6eb9912db78a0866cb"
        ),
        helper_size_bytes=5_695_048,
        helper_sha256=(
            "sha256:8676275bb47aefe963c8b82047cc66fb7a5140caec72d1ebbfa17556b281577d"
        ),
        data_size_bytes=18_887_468,
        data_sha256=(
            "sha256:b9884362e37772f772a0d28d1e12622455c14353497b3435deed60aa7e592c5e"
        ),
        manifest_sha256=(
            "sha256:ebdc1bff4da9fbafe3971de7e5f095c90ad78e00e3f40b10fa5a7249d78a7c16"
        ),
        resource_fingerprint=(
            "sha256:2ba2cccb3c7489def988b63b0f0fd2cd96469521569c4807b63c80d2b50d3063"
        ),
    ),
    b1=CandidateFreeze(
        generation=(
            "sha256-"
            "046bcfa093aac43ad6ee64afd4b3a3e8325bab0f3d20b8cb083c447ba8c91a2f"
        ),
        helper_size_bytes=5_746_568,
        helper_sha256=(
            "sha256:728d9a79c0f540a832d3f404a2603f49080e1f9e7ee1d24df1a0a69f5a4a75e8"
        ),
        data_size_bytes=18_887_468,
        data_sha256=(
            "sha256:b9884362e37772f772a0d28d1e12622455c14353497b3435deed60aa7e592c5e"
        ),
        manifest_sha256=(
            "sha256:c06ab9c7374ae1c4d114da6c0cabf2a6ef586e94449cb658a3c7927e4d30cb79"
        ),
        resource_fingerprint=(
            "sha256:65f3f341f491c1deec1182743c4923db3c7ad6f2609cb50cfde9c0a6b8e3adaa"
        ),
    ),
)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _load_json_bytes(data: bytes, context: str) -> dict[str, Any]:
    try:
        value = json.loads(
            data.decode("utf-8"), object_pairs_hook=_no_duplicate_keys
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{context} is not valid UTF-8 JSON: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{context} must contain a JSON object")
    return value


def _read_stable_regular(path: Path, context: str) -> tuple[bytes, os.stat_result]:
    before = path.lstat()
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
    ):
        raise ValueError(f"{context} must be a non-hardlinked regular file")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError(f"{context} changed before it was read")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        final = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    after = path.lstat()
    data = b"".join(chunks)
    identity_fields = (
        "st_dev",
        "st_ino",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
        "st_mode",
        "st_nlink",
    )
    if (
        any(getattr(opened, key) != getattr(final, key) for key in identity_fields)
        or any(getattr(final, key) != getattr(after, key) for key in identity_fields)
        or len(data) != final.st_size
    ):
        raise ValueError(f"{context} changed while it was read")
    return data, final


def _canonical_input_root(path: Path, context: str, *, directory: bool) -> Path:
    if not path.is_absolute():
        raise ValueError(f"{context} must be an absolute path")
    before = path.lstat()
    if stat.S_ISLNK(before.st_mode):
        raise ValueError(f"{context} root must not be a symlink")
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(before.st_mode):
        kind = "directory" if directory else "regular file"
        raise ValueError(f"{context} must be a {kind}")
    resolved = path.resolve(strict=True)
    after = resolved.lstat()
    if (
        stat.S_ISLNK(after.st_mode)
        or (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
    ):
        raise ValueError(f"{context} root identity changed while resolving")
    return resolved


def _root_identity(path: Path) -> tuple[int, int]:
    metadata = path.lstat()
    return metadata.st_dev, metadata.st_ino


def _verify_root_identity(
    path: Path, expected: tuple[int, int], context: str, *, directory: bool
) -> None:
    metadata = path.lstat()
    expected_kind = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not expected_kind(metadata.st_mode)
        or (metadata.st_dev, metadata.st_ino) != expected
    ):
        raise ValueError(f"{context} root identity changed during acquisition")


def _capture_python_sources() -> dict[str, SourceBinding]:
    module_paths = {
        "producer": Path(__file__),
        "v1_acquisition": Path(v1_acquisition.__file__),
        "quality_evaluator": Path(evaluate_conversion_quality.__file__),
        "probe_summarizer": Path(summarize_ab_probe.__file__),
    }
    repository_root = Path(__file__).resolve().parents[2]
    bindings: dict[str, SourceBinding] = {}
    for key, (snapshot_name, repository_path) in PYTHON_SOURCE_BINDINGS.items():
        source = _canonical_input_root(
            module_paths[key].absolute(), f"Python source {key}", directory=False
        )
        expected_source = (repository_root / repository_path).resolve(strict=True)
        if source != expected_source:
            raise ValueError(
                f"Python source {key} was not imported from {repository_path}"
            )
        data, metadata = _read_stable_regular(source, f"Python source {key}")
        bindings[key] = SourceBinding(
            key=key,
            source_path=source,
            repository_path=repository_path,
            snapshot_name=snapshot_name,
            data=data,
            mode=stat.S_IMODE(metadata.st_mode),
            device=metadata.st_dev,
            inode=metadata.st_ino,
        )
    return bindings


def _verify_python_sources(bindings: dict[str, SourceBinding]) -> None:
    if set(bindings) != set(PYTHON_SOURCE_BINDINGS):
        raise ValueError("Python source binding set changed")
    for key, binding in bindings.items():
        data, metadata = _read_stable_regular(
            binding.source_path, f"Python source {key}"
        )
        if (
            data != binding.data
            or stat.S_IMODE(metadata.st_mode) != binding.mode
            or (metadata.st_dev, metadata.st_ino)
            != (binding.device, binding.inode)
        ):
            raise ValueError(f"Python source {key} changed during acquisition")


def _snapshot_python_sources(
    root: Path, bindings: dict[str, SourceBinding]
) -> tuple[Path, dict[str, Any]]:
    destination = root / PYTHON_SOURCE_SNAPSHOT_NAME
    destination.mkdir(mode=0o700)
    files: list[dict[str, Any]] = []
    for key in PYTHON_SOURCE_BINDINGS:
        binding = bindings[key]
        _write_snapshot_file(destination / binding.snapshot_name, binding.data, 0o444)
        files.append(
            {
                "id": key,
                "path": binding.repository_path,
                "source_path": str(binding.source_path),
                "snapshot_path": (
                    f"{PYTHON_SOURCE_SNAPSHOT_NAME}/{binding.snapshot_name}"
                ),
                "size_bytes": len(binding.data),
                "sha256": _sha256(binding.data),
            }
        )
    destination.chmod(0o555)
    v1_acquisition._fsync_directory(destination)
    contract_base = {
        "schema": "hazkey.mozc-v2-python-sources.v1",
        "files": files,
    }
    return destination, contract_base | {
        "integrity": _sha256(_canonical_json(contract_base))
    }


def _verify_python_snapshot(
    destination: Path, bindings: dict[str, SourceBinding]
) -> None:
    metadata = destination.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o555
    ):
        raise ValueError("Python source snapshot directory changed")
    if {item.name for item in destination.iterdir()} != {
        binding.snapshot_name for binding in bindings.values()
    }:
        raise ValueError("Python source snapshot file set changed")
    for key, binding in bindings.items():
        data = _require_regular_mode(
            destination / binding.snapshot_name,
            0o444,
            f"Python source snapshot {key}",
        )
        if data != binding.data:
            raise ValueError(f"Python source snapshot {key} changed")


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=TERMINATION_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def _run_probe(
    argv: list[str],
    raw_handle: Any,
    stderr_handle: Any,
    run_id: str,
    environment: dict[str, str],
    cwd: Path,
) -> int:
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=raw_handle,
        stderr=stderr_handle,
        shell=False,
        start_new_session=True,
        env=environment,
        cwd=cwd,
    )
    try:
        return process.wait(timeout=PER_RUN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as error:
        _terminate_process_group(process)
        raise ValueError(
            f"run {run_id} exceeded {PER_RUN_TIMEOUT_SECONDS} seconds"
        ) from error
    except BaseException:
        _terminate_process_group(process)
        raise


def _require_regular_mode(path: Path, mode: int, context: str) -> bytes:
    metadata = path.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != mode
    ):
        raise ValueError(
            f"{context} must be a non-symlink regular file with mode {mode:04o}"
        )
    return v1_acquisition._read_regular(path, context)


def _freeze_object(freeze: CandidateFreeze) -> dict[str, Any]:
    return {
        "generation": freeze.generation,
        "helper_size_bytes": freeze.helper_size_bytes,
        "helper_sha256": freeze.helper_sha256,
        "data_size_bytes": freeze.data_size_bytes,
        "data_sha256": freeze.data_sha256,
        "manifest_sha256": freeze.manifest_sha256,
    }


def _validate_sealed_inputs(
    sealed_generation: Path,
    contract: FrozenContract,
) -> tuple[dict[str, bytes], list[dict[str, str]], dict[str, Any]]:
    sealed_generation = _canonical_input_root(
        sealed_generation, "sealed-generation", directory=True
    )
    metadata = sealed_generation.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o555
        or sealed_generation.name != contract.sealed_generation
    ):
        raise ValueError("sealed-generation identity or mode does not match v2")

    files = {
        SEALED_POLICY_NAME: _require_regular_mode(
            sealed_generation / SEALED_POLICY_NAME,
            0o444,
            "sealed corpus policy",
        ),
        SEALED_MANIFEST_NAME: _require_regular_mode(
            sealed_generation / SEALED_MANIFEST_NAME,
            0o444,
            "sealed corpus manifest",
        ),
        SEALED_CORPUS_NAME: _require_regular_mode(
            sealed_generation / SEALED_CORPUS_NAME,
            0o444,
            "sealed formal corpus",
        ),
    }
    expected_hashes = {
        SEALED_POLICY_NAME: contract.policy_sha256,
        SEALED_MANIFEST_NAME: contract.manifest_sha256,
        SEALED_CORPUS_NAME: contract.corpus_sha256,
    }
    for name, expected in expected_hashes.items():
        if _sha256(files[name]) != expected:
            raise ValueError(f"sealed {name} does not match its frozen SHA-256")

    policy = _load_json_bytes(files[SEALED_POLICY_NAME], "sealed corpus policy")
    manifest = _load_json_bytes(
        files[SEALED_MANIFEST_NAME], "sealed corpus manifest"
    )
    if (
        policy.get("schema") != SEALED_POLICY_SCHEMA
        or policy.get("policy_id") != "mozc-adoption-v2"
        or policy.get("decision_tier") != "formal"
        or policy.get("collection")
        != {"status": "ready", "manifest_path": SEALED_MANIFEST_NAME}
    ):
        raise ValueError("sealed policy is not the ready formal v2 policy")
    suite = policy.get("formal_suite")
    if not isinstance(suite, dict):
        raise ValueError("sealed policy formal_suite must be an object")
    if (
        suite.get("total_cases") != TOTAL_CASES
        or suite.get("quality_cases") != QUALITY_CASES
        or suite.get("quality_categories") != QUALITY_CATEGORY_COUNTS
        or suite.get("protected")
        != {
            "cases": 100,
            "required_passes": 100,
            "metric": "top1_exact",
            "included_in_overall_quality_rates": False,
        }
    ):
        raise ValueError("sealed policy formal suite does not match objective v2")
    freezes = policy.get("artifact_freezes")
    if not isinstance(freezes, dict):
        raise ValueError("sealed policy artifact_freezes must be an object")
    if freezes.get("eligible_candidate_ids") != ["B0", "B1"]:
        raise ValueError("sealed policy must bind exactly B0 then B1")
    if freezes.get("evaluation_runner") != {
        "product_source_revision": contract.product_source_ref,
        "size_bytes": contract.executable_size_bytes,
        "sha256": contract.executable_sha256,
        "runtime_dependencies_integrity": contract.runtime_dependencies_integrity,
    }:
        raise ValueError("sealed policy evaluation runner freeze changed")
    if freezes.get("candidates") != {
        "B0": _freeze_object(contract.b0),
        "B1": _freeze_object(contract.b1),
    }:
        raise ValueError("sealed policy B0/B1 artifact freezes changed")

    if manifest.get("schema") != SEALED_MANIFEST_SCHEMA:
        raise ValueError("sealed manifest schema mismatch")
    if manifest.get("policy") != {
        "path": SEALED_POLICY_NAME,
        "sha256": contract.policy_sha256,
    }:
        raise ValueError("sealed manifest does not bind the frozen policy")
    aggregate = manifest.get("aggregate")
    if not isinstance(aggregate, dict):
        raise ValueError("sealed manifest aggregate must be an object")
    if (
        aggregate.get("cases") != TOTAL_CASES
        or aggregate.get("quality_cases") != QUALITY_CASES
        or aggregate.get("sha256") != contract.corpus_sha256
        or aggregate.get("categories") != ALL_CATEGORY_COUNTS
        or aggregate.get("protected_included_in_overall_quality_rates") is not False
    ):
        raise ValueError("sealed manifest aggregate contract changed")

    rows = evaluate_conversion_quality.load_corpus_bytes(
        files[SEALED_CORPUS_NAME], "sealed formal corpus"
    )
    categories: dict[str, int] = {}
    for row in rows:
        categories[row["category"]] = categories.get(row["category"], 0) + 1
    if len(rows) != TOTAL_CASES or categories != ALL_CATEGORY_COUNTS:
        raise ValueError("sealed formal corpus rows do not match the v2 counts")
    return files, rows, policy


def _read_regular_at(directory_fd: int, name: str, context: str) -> bytes:
    before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValueError(f"{context} must be a non-hardlinked regular file")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(name, flags, dir_fd=directory_fd)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError(f"{context} changed before it was read")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        final = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    data = b"".join(chunks)
    identity_fields = (
        "st_dev",
        "st_ino",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    if (
        any(
            getattr(opened, field) != getattr(final, field)
            for field in identity_fields
        )
        or any(
            getattr(final, field) != getattr(after, field)
            for field in identity_fields
        )
        or len(data) != final.st_size
        or final.st_nlink != 1
        or not stat.S_ISREG(final.st_mode)
    ):
        raise ValueError(f"{context} changed while it was read")
    return data


def _open_output_parent(
    output_directory: Path,
) -> tuple[Path, str, int, tuple[int, int]]:
    if not output_directory.is_absolute():
        raise ValueError("output-dir must be an absolute path")
    output_name = output_directory.name
    if output_name in {"", ".", ".."} or "/" in output_name or "\x00" in output_name:
        raise ValueError("output-dir must name one child of an existing parent")
    requested_parent = output_directory.parent
    before = requested_parent.lstat()
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
        raise ValueError("output parent must be a non-symlink directory")
    parent = requested_parent.resolve(strict=True)
    resolved = parent.lstat()
    if (resolved.st_dev, resolved.st_ino) != (before.st_dev, before.st_ino):
        raise ValueError("output parent identity changed while resolving")
    flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(parent, flags)
    opened = os.fstat(descriptor)
    if (opened.st_dev, opened.st_ino) != (resolved.st_dev, resolved.st_ino):
        os.close(descriptor)
        raise ValueError("output parent identity changed while opening")
    return parent, output_name, descriptor, (opened.st_dev, opened.st_ino)


def _assert_parent_identity(
    parent: Path, descriptor: int, identity: tuple[int, int]
) -> None:
    opened = os.fstat(descriptor)
    current = parent.lstat()
    if (
        not stat.S_ISDIR(opened.st_mode)
        or stat.S_ISLNK(current.st_mode)
        or not stat.S_ISDIR(current.st_mode)
        or (opened.st_dev, opened.st_ino) != identity
        or (current.st_dev, current.st_ino) != identity
    ):
        raise ValueError("output parent path identity changed during acquisition")


def _entry_exists_at(directory_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _unlink_owned_lock(
    directory_fd: int, name: str, identity: tuple[int, int]
) -> None:
    try:
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError as error:
        raise ValueError("acquisition lock disappeared before removal") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or (metadata.st_dev, metadata.st_ino) != identity
    ):
        raise ValueError(
            "acquisition lock identity changed; preserving the foreign entry"
        )
    os.unlink(name, dir_fd=directory_fd)


def _create_temporary_at(directory_fd: int, output_name: str) -> tuple[str, Path]:
    for _ in range(128):
        name = f".{output_name}.tmp-{secrets.token_hex(8)}"
        try:
            os.mkdir(name, 0o700, dir_fd=directory_fd)
        except FileExistsError:
            continue
        return name, Path(f"/proc/self/fd/{directory_fd}") / name
    raise FileExistsError("could not allocate a unique acquisition directory")


def _rename_noreplace_at(
    directory_fd: int, source_name: str, destination_name: str
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOSYS, "Linux renameat2 is required for formal acquisition")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        directory_fd,
        os.fsencode(source_name),
        directory_fd,
        os.fsencode(destination_name),
        RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise ValueError(f"refusing to overwrite output {destination_name}")
    raise OSError(error_number, os.strerror(error_number), destination_name)


def _capture_tree(root: Path) -> dict[str, TreeEntry]:
    root_metadata = root.lstat()
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        raise ValueError("evidence root must be a non-symlink directory")
    root_flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    root_fd = os.open(root, root_flags)
    entries = {
        ".": TreeEntry(kind="directory", mode=stat.S_IMODE(root_metadata.st_mode))
    }

    def walk(directory_fd: int, prefix: str) -> None:
        initial = os.fstat(directory_fd)
        names = sorted(os.listdir(directory_fd), key=lambda value: value.encode())
        for name in names:
            if name in {".", ".."} or "/" in name or "\x00" in name:
                raise ValueError("evidence tree contains an invalid entry name")
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            relative = f"{prefix}/{name}" if prefix else name
            if stat.S_ISDIR(metadata.st_mode):
                child_fd = os.open(
                    name,
                    root_flags,
                    dir_fd=directory_fd,
                )
                try:
                    opened = os.fstat(child_fd)
                    if (opened.st_dev, opened.st_ino) != (
                        metadata.st_dev,
                        metadata.st_ino,
                    ):
                        raise ValueError(f"evidence directory changed: {relative}")
                    entries[relative] = TreeEntry(
                        kind="directory", mode=stat.S_IMODE(opened.st_mode)
                    )
                    walk(child_fd, relative)
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(metadata.st_mode):
                data = _read_regular_at(
                    directory_fd, name, f"evidence file {relative}"
                )
                entries[relative] = TreeEntry(
                    kind="file",
                    mode=stat.S_IMODE(metadata.st_mode),
                    size_bytes=len(data),
                    sha256=_sha256(data),
                )
            else:
                raise ValueError(f"evidence tree contains a symlink or special entry: {relative}")
        final = os.fstat(directory_fd)
        final_names = sorted(os.listdir(directory_fd), key=lambda value: value.encode())
        if (
            names != final_names
            or (initial.st_dev, initial.st_ino) != (final.st_dev, final.st_ino)
            or initial.st_mtime_ns != final.st_mtime_ns
            or initial.st_ctime_ns != final.st_ctime_ns
        ):
            raise ValueError("evidence directory changed while it was verified")

    try:
        opened_root = os.fstat(root_fd)
        if (opened_root.st_dev, opened_root.st_ino) != (
            root_metadata.st_dev,
            root_metadata.st_ino,
        ):
            raise ValueError("evidence root changed while opening")
        walk(root_fd, "")
        final_root = root.lstat()
        if (final_root.st_dev, final_root.st_ino) != (
            opened_root.st_dev,
            opened_root.st_ino,
        ):
            raise ValueError("evidence root identity changed while it was verified")
    finally:
        os.close(root_fd)
    return entries


def _frozen_tree_contract(entries: dict[str, TreeEntry]) -> dict[str, TreeEntry]:
    return {
        path: (
            TreeEntry(
                kind="directory",
                # The product's Mozc runtime identity check requires the
                # generation directory itself to remain exactly 0755.
                mode=(
                    0o755
                    if path.startswith(f"{INPUT_ROOT_NAME}/{B0_SNAPSHOT_NAME}/sha256-")
                    and path.count("/") == 2
                    else 0o555
                ),
            )
            if entry.kind == "directory"
            else TreeEntry(
                kind="file",
                mode=0o555 if entry.mode & 0o111 else 0o444,
                size_bytes=entry.size_bytes,
                sha256=entry.sha256,
            )
        )
        for path, entry in entries.items()
    }


def _freeze_tree(root: Path) -> None:
    flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    root_fd = os.open(root, flags)

    def freeze(directory_fd: int, prefix: str) -> None:
        for name in sorted(os.listdir(directory_fd), key=lambda value: value.encode()):
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            relative = f"{prefix}/{name}" if prefix else name
            if stat.S_ISDIR(metadata.st_mode):
                child_fd = os.open(name, flags, dir_fd=directory_fd)
                try:
                    freeze(child_fd, relative)
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
                file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                if hasattr(os, "O_NOFOLLOW"):
                    file_flags |= os.O_NOFOLLOW
                file_fd = os.open(name, file_flags, dir_fd=directory_fd)
                try:
                    opened = os.fstat(file_fd)
                    if (opened.st_dev, opened.st_ino) != (
                        metadata.st_dev,
                        metadata.st_ino,
                    ):
                        raise ValueError(f"evidence file changed before freeze: {name}")
                    os.fchmod(file_fd, 0o555 if opened.st_mode & 0o111 else 0o444)
                    os.fsync(file_fd)
                finally:
                    os.close(file_fd)
            else:
                raise ValueError(f"evidence entry cannot be frozen safely: {name}")
        mode = (
            0o755
            if prefix.startswith(f"{INPUT_ROOT_NAME}/{B0_SNAPSHOT_NAME}/sha256-")
            and prefix.count("/") == 2
            else 0o555
        )
        os.fchmod(directory_fd, mode)
        os.fsync(directory_fd)

    try:
        freeze(root_fd, "")
    finally:
        os.close(root_fd)


def _verify_tree(root: Path, expected: dict[str, TreeEntry], context: str) -> None:
    actual = _capture_tree(root)
    if actual != expected:
        missing = sorted(set(expected) - set(actual))
        unknown = sorted(set(actual) - set(expected))
        changed = sorted(
            key for key in set(actual) & set(expected) if actual[key] != expected[key]
        )
        raise ValueError(
            f"{context} tree mismatch; missing={missing!r}, unknown={unknown!r}, "
            f"changed={changed!r}"
        )


def _verify_generated_outputs(root: Path, expected: dict[str, bytes]) -> None:
    expected_top = {
        SNAPSHOT_ROOT_NAME,
        INPUT_ROOT_NAME,
        PYTHON_SOURCE_SNAPSHOT_NAME,
        *expected.keys(),
    }
    actual_top = {item.name for item in root.iterdir()}
    if actual_top != expected_top:
        raise ValueError(
            "evidence top-level set changed; "
            f"missing={sorted(expected_top - actual_top)!r}, "
            f"unknown={sorted(actual_top - expected_top)!r}"
        )
    for name, data in expected.items():
        actual, metadata = _read_stable_regular(root / name, f"output {name}")
        if actual != data or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ValueError(f"output {name} bytes or mode changed")


def _write_snapshot_file(path: Path, data: bytes, mode: int) -> None:
    v1_acquisition._write_private(path, data, mode=mode)


def _snapshot_dictionary(source: Path, destination: Path) -> dict[str, Any]:
    source = _canonical_input_root(
        source, "hazkey-dictionary", directory=True
    )
    metadata = source.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("hazkey-dictionary must be a non-symlink directory")
    source_fd = os.open(
        source,
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    destination.mkdir(mode=0o700)
    entries: list[tuple[bytes, bytes]] = []
    total_size = 0

    def copy_directory(directory_fd: int, target: Path, prefix: str) -> None:
        nonlocal total_size
        initial = os.fstat(directory_fd)
        names = sorted(
            os.listdir(directory_fd), key=lambda value: value.encode("utf-8")
        )
        for name in names:
            if name in {".", ".."} or "/" in name or "\x00" in name:
                raise ValueError("dictionary contains an invalid entry name")
            child = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            relative = f"{prefix}/{name}" if prefix else name
            target_path = target / name
            if stat.S_ISDIR(child.st_mode):
                child_fd = os.open(
                    name,
                    os.O_RDONLY
                    | os.O_DIRECTORY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directory_fd,
                )
                try:
                    opened = os.fstat(child_fd)
                    if (opened.st_dev, opened.st_ino) != (child.st_dev, child.st_ino):
                        raise ValueError(f"dictionary directory changed: {relative}")
                    target_path.mkdir(mode=0o700)
                    copy_directory(child_fd, target_path, relative)
                    target_path.chmod(0o555)
                    v1_acquisition._fsync_directory(target_path)
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(child.st_mode):
                data = _read_regular_at(
                    directory_fd, name, f"dictionary entry {relative}"
                )
                _write_snapshot_file(target_path, data, 0o444)
                path_bytes = relative.encode("utf-8")
                entries.append((path_bytes, hashlib.sha256(data).digest()))
                total_size += len(data)
            else:
                raise ValueError(
                    f"dictionary contains a symlink or special entry: {relative}"
                )
        final_names = sorted(
            os.listdir(directory_fd), key=lambda value: value.encode("utf-8")
        )
        final = os.fstat(directory_fd)
        if (
            final_names != names
            or initial.st_mtime_ns != final.st_mtime_ns
            or initial.st_ctime_ns != final.st_ctime_ns
        ):
            raise ValueError("dictionary directory changed while it was snapshotted")

    try:
        copy_directory(source_fd, destination, "")
    finally:
        os.close(source_fd)
    destination.chmod(0o555)
    v1_acquisition._fsync_directory(destination)
    fingerprint = _fingerprint_entries(
        entries, domain="hazkey.dictionary-fingerprint.v1"
    )
    return {
        "source_path": str(source),
        "snapshot_path": f"{INPUT_ROOT_NAME}/{DICTIONARY_SNAPSHOT_NAME}",
        "files": len(entries),
        "size_bytes": total_size,
        "fingerprint": fingerprint,
    }


def _fingerprint_entries(
    entries: list[tuple[bytes, bytes]], *, domain: str
) -> str:
    hasher = hashlib.sha256()
    hasher.update(domain.encode("utf-8") + b"\0")
    for path_bytes, file_digest in sorted(entries, key=lambda item: item[0]):
        hasher.update(b"\x01")
        hasher.update(len(path_bytes).to_bytes(8, "big"))
        hasher.update(path_bytes)
        hasher.update(file_digest)
    return "sha256:" + hasher.hexdigest()


def _directory_fingerprint(path: Path, *, domain: str) -> str:
    entries: list[tuple[bytes, bytes]] = []
    for item in path.rglob("*"):
        metadata = item.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"snapshot contains a symlink: {item}")
        if stat.S_ISREG(metadata.st_mode):
            data = v1_acquisition._read_regular(item, f"snapshot file {item}")
            entries.append(
                (
                    item.relative_to(path).as_posix().encode("utf-8"),
                    hashlib.sha256(data).digest(),
                )
            )
        elif not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"snapshot contains a special entry: {item}")
    return _fingerprint_entries(entries, domain=domain)


def _read_b0_core(source: Path, freeze: CandidateFreeze) -> dict[str, bytes]:
    source = _canonical_input_root(source, "b0-bundle", directory=True)
    metadata = source.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o755
        or source.name != freeze.generation
    ):
        raise ValueError("B0 bundle generation identity or mode mismatch")
    identities = {
        "fcitx5-grimodex-mozc-helper": (
            freeze.helper_size_bytes,
            freeze.helper_sha256,
            0o555,
        ),
        "mozc.data": (freeze.data_size_bytes, freeze.data_sha256, 0o444),
        "manifest.json": (None, freeze.manifest_sha256, 0o444),
    }
    contents: dict[str, bytes] = {}
    for name, (expected_size, expected_sha, mode) in identities.items():
        path = source / name
        data = _require_regular_mode(path, mode, f"B0 {name}")
        if path.stat().st_nlink != 1:
            raise ValueError(f"B0 {name} must not be hardlinked")
        if (expected_size is not None and len(data) != expected_size) or _sha256(
            data
        ) != expected_sha:
            raise ValueError(f"B0 {name} does not match the frozen identity")
        contents[name] = data
    manifest = _load_json_bytes(contents["manifest.json"], "B0 manifest")
    artifacts = manifest.get("artifacts")
    if (
        manifest.get("schema") != "grimodex.mozc-artifact-bundle.v1"
        or not isinstance(artifacts, dict)
        or set(artifacts) != {"fcitx5-grimodex-mozc-helper", "mozc.data"}
    ):
        raise ValueError("B0 artifact manifest schema or artifact set mismatch")
    return contents


def _snapshot_b0(
    source: Path, destination_parent: Path, freeze: CandidateFreeze
) -> tuple[Path, dict[str, Any]]:
    source = _canonical_input_root(source, "b0-bundle", directory=True)
    contents = _read_b0_core(source, freeze)
    identities = {
        "fcitx5-grimodex-mozc-helper": 0o555,
        "mozc.data": 0o444,
        "manifest.json": 0o444,
    }

    destination_parent.mkdir(mode=0o700)
    generation = destination_parent / freeze.generation
    generation.mkdir(mode=0o700)
    for name, mode in identities.items():
        _write_snapshot_file(generation / name, contents[name], mode)
    generation.chmod(0o755)
    v1_acquisition._fsync_directory(generation)
    destination_parent.chmod(0o555)
    v1_acquisition._fsync_directory(destination_parent)
    fingerprint = _directory_fingerprint(
        generation, domain="hazkey.mozc-runtime-fingerprint.v1"
    )
    if fingerprint != freeze.resource_fingerprint:
        raise ValueError("B0 runtime snapshot fingerprint mismatch")
    return generation, {
        "source_path": str(source),
        "snapshot_path": (
            f"{INPUT_ROOT_NAME}/{B0_SNAPSHOT_NAME}/{freeze.generation}"
        ),
        "generation": freeze.generation,
        "helper_size_bytes": freeze.helper_size_bytes,
        "helper_sha256": freeze.helper_sha256,
        "data_size_bytes": freeze.data_size_bytes,
        "data_sha256": freeze.data_sha256,
        "manifest_sha256": freeze.manifest_sha256,
        "resource_fingerprint": fingerprint,
    }


def _snapshot_sealed_files(
    destination: Path, files: dict[str, bytes], source: Path
) -> dict[str, Any]:
    destination.mkdir(mode=0o700)
    for name, data in files.items():
        _write_snapshot_file(destination / name, data, 0o444)
    destination.chmod(0o555)
    v1_acquisition._fsync_directory(destination)
    return {
        "source_path": str(source),
        "snapshot_path": f"{INPUT_ROOT_NAME}/{SEALED_SNAPSHOT_NAME}",
        "generation": source.name,
        "policy": {
            "path": SEALED_POLICY_NAME,
            "sha256": _sha256(files[SEALED_POLICY_NAME]),
        },
        "manifest": {
            "path": SEALED_MANIFEST_NAME,
            "sha256": _sha256(files[SEALED_MANIFEST_NAME]),
        },
        "corpus": {
            "path": SEALED_CORPUS_NAME,
            "sha256": _sha256(files[SEALED_CORPUS_NAME]),
            "size_bytes": len(files[SEALED_CORPUS_NAME]),
            "cases": TOTAL_CASES,
        },
    }


def _command(backend: str, b0_snapshot: Path, source_ref: str) -> list[str]:
    backend_name = "Hazkey" if backend == "hazkey" else "B0"
    command = [
        SNAPSHOT_EXECUTABLE_ARG,
        "--ab-probe",
        "--corpus",
        SNAPSHOT_CORPUS_ARGUMENT,
        "--source-ref",
        source_ref,
        "--warmups",
        str(WARMUPS),
        "--iterations",
        str(ITERATIONS),
        "--top-k",
        str(TOP_K),
        "--backend-name",
        backend_name,
        "--converter-backend",
        backend,
    ]
    if backend == "hazkey":
        command.extend(("--dictionary", SNAPSHOT_DICTIONARY_ARGUMENT))
    else:
        command.extend(
            (
                "--mozc-bundle",
                f"./{INPUT_ROOT_NAME}/{B0_SNAPSHOT_NAME}/{b0_snapshot.name}",
            )
        )
    return command


def _validate_raw_run(
    data: bytes,
    context: str,
    *,
    rows: list[dict[str, str]],
    backend_name: str,
    converter_backend: str,
    resource_path: Path,
    resource_fingerprint: str,
    contract: FrozenContract,
) -> dict[str, Any]:
    loaded = summarize_ab_probe.load_run_bytes(data, context)
    expectations = {
        "schema": RAW_SCHEMA,
        "backend": backend_name,
        "source_ref": contract.product_source_ref,
        "converter_backend": converter_backend,
        "top_k": TOP_K,
        "warmups": WARMUPS,
        "iterations": ITERATIONS,
        "corpus": {"sha256": contract.corpus_sha256, "cases": TOTAL_CASES},
    }
    for field, expected in expectations.items():
        if loaded[field] != expected:
            raise ValueError(
                f"{context}: {field} mismatch; expected {expected!r}, "
                f"got {loaded[field]!r}"
            )
    expected_kind = (
        "hazkey_dictionary"
        if converter_backend == "hazkey"
        else "mozc_runtime_inputs"
    )
    resource = loaded["resource"]
    if (
        resource["kind"] != expected_kind
        or Path(resource["path"]).resolve() != resource_path.resolve()
        or resource["fingerprint"] != resource_fingerprint
    ):
        raise ValueError(f"{context}: resource provenance mismatch")
    expected_ids = [row["id"] for row in rows]
    if list(loaded["cases"]) != expected_ids:
        raise ValueError(f"{context}: case IDs or order differ from sealed corpus")
    for row in rows:
        case = loaded["cases"][row["id"]]
        if (
            case["reading"] != row["reading"]
            or case["category"] != row["category"]
        ):
            raise ValueError(
                f"{context}: case {row['id']!r} differs from sealed corpus"
            )
    return loaded


def _quality_report(
    rows: list[dict[str, str]], loaded: dict[str, Any]
) -> dict[str, Any]:
    candidates = {
        case_id: case["candidates"] for case_id, case in loaded["cases"].items()
    }
    report = evaluate_conversion_quality.evaluate(rows, candidates, TOP_K)
    if (
        report.get("schema") != QUALITY_REPORT_SCHEMA
        or report.get("corpus_cases") != TOTAL_CASES
        or report.get("evaluated_cases") != TOTAL_CASES
        or report.get("missing_results")
        or {
            category: values["total"]
            for category, values in report["by_category"].items()
        }
        != ALL_CATEGORY_COUNTS
    ):
        raise ValueError("quality report does not cover the exact v2 corpus")
    return report


def _backend_objective_metrics(report: dict[str, Any]) -> dict[str, Any]:
    by_category = report["by_category"]
    categories = {
        category: {
            "cases": expected_count,
            "top1_hits": by_category[category]["top1"],
            "top10_hits": by_category[category]["top10"],
        }
        for category, expected_count in QUALITY_CATEGORY_COUNTS.items()
    }
    return {
        "quality_cases": QUALITY_CASES,
        "top1_hits": sum(item["top1_hits"] for item in categories.values()),
        "top10_hits": sum(item["top10_hits"] for item in categories.values()),
        "categories": categories,
        "protected": {
            "cases": 100,
            "top1_hits": by_category["protected"]["top1"],
            "top10_hits": by_category["protected"]["top10"],
        },
    }


def build_objective_report(
    hazkey_report: dict[str, Any],
    b0_report: dict[str, Any],
    *,
    corpus_sha256: str,
    hazkey_resource_fingerprint: str,
    candidate_id: str,
    candidate_resource_fingerprint: str,
    raw_run_sha256: dict[str, str],
) -> dict[str, Any]:
    if candidate_id != "B0" or set(raw_run_sha256) != {"H0", "B0"}:
        raise ValueError("objective evidence must bind Hazkey H0 and candidate B0")
    for context, value in {
        "corpus_sha256": corpus_sha256,
        "hazkey_resource_fingerprint": hazkey_resource_fingerprint,
        "candidate_resource_fingerprint": candidate_resource_fingerprint,
        **{f"raw_run_sha256.{key}": value for key, value in raw_run_sha256.items()},
    }.items():
        if re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
            raise ValueError(f"{context} must be a canonical SHA-256 identity")
    hazkey = _backend_objective_metrics(hazkey_report)
    b0 = _backend_objective_metrics(b0_report)
    top1_delta = b0["top1_hits"] - hazkey["top1_hits"]
    top10_delta = b0["top10_hits"] - hazkey["top10_hits"]
    gates: list[dict[str, Any]] = [
        {
            "id": "quality-top1-delta",
            "actual_delta_hits": top1_delta,
            "minimum_delta_hits": MINIMUM_TOP1_DELTA_HITS,
            "passed": top1_delta >= MINIMUM_TOP1_DELTA_HITS,
        },
        {
            "id": "quality-top10-delta",
            "actual_delta_hits": top10_delta,
            "minimum_delta_hits": MINIMUM_TOP10_DELTA_HITS,
            "passed": top10_delta >= MINIMUM_TOP10_DELTA_HITS,
        },
    ]
    category_deltas: dict[str, int] = {}
    for category, minimum in MINIMUM_CATEGORY_TOP1_DELTA_HITS.items():
        delta = (
            b0["categories"][category]["top1_hits"]
            - hazkey["categories"][category]["top1_hits"]
        )
        category_deltas[category] = delta
        gates.append(
            {
                "id": f"category-top1-delta:{category}",
                "actual_delta_hits": delta,
                "minimum_delta_hits": minimum,
                "passed": delta >= minimum,
            }
        )
    protected_hits = b0["protected"]["top1_hits"]
    gates.append(
        {
            "id": "protected-top1",
            "actual_hits": protected_hits,
            "required_hits": PROTECTED_REQUIRED,
            "passed": protected_hits == PROTECTED_REQUIRED,
        }
    )
    passed = all(gate["passed"] for gate in gates)
    report_base = {
        "schema": OBJECTIVE_SCHEMA,
        "corpus": {
            "cases": TOTAL_CASES,
            "quality_cases": QUALITY_CASES,
            "protected_cases": 100,
            "sha256": corpus_sha256,
        },
        "baseline": {
            "id": "Hazkey",
            "resource_fingerprint": hazkey_resource_fingerprint,
        },
        "candidate": {
            "id": candidate_id,
            "resource_fingerprint": candidate_resource_fingerprint,
        },
        "raw_runs": dict(raw_run_sha256),
        "backends": {"Hazkey": hazkey, "B0": b0},
        "delta_direction": "B0-minus-Hazkey",
        "deltas": {
            "top1_hits": top1_delta,
            "top10_hits": top10_delta,
            "category_top1_hits": category_deltas,
        },
        "gates": gates,
        "passed": passed,
        "next_step": (
            "continue-b0-human-performance-stability"
            if passed
            else "complete-b0-formal-evidence-before-b1"
        ),
        "not_evaluated": [
            "human_preference",
            "both_bad",
            "warm_latency_p95",
            "pss",
            "stability",
        ],
    }
    return report_base | {"integrity": _sha256(_canonical_json(report_base))}


def _verify_snapshots(
    *,
    executable_snapshot: Path,
    executable_bytes: bytes,
    library_snapshot: Path,
    runtime_dependencies: dict[str, bytes],
    sealed_snapshot: Path,
    sealed_files: dict[str, bytes],
    dictionary_snapshot: Path,
    dictionary_fingerprint: str,
    b0_snapshot: Path,
    b0_fingerprint: str,
    python_snapshot: Path,
    python_sources: dict[str, SourceBinding],
) -> None:
    v1_acquisition._verify_runtime_snapshot(
        executable_snapshot,
        executable_bytes,
        library_snapshot,
        runtime_dependencies,
    )
    sealed_metadata = sealed_snapshot.lstat()
    if (
        stat.S_ISLNK(sealed_metadata.st_mode)
        or not stat.S_ISDIR(sealed_metadata.st_mode)
        or stat.S_IMODE(sealed_metadata.st_mode) != 0o555
        or {item.name for item in sealed_snapshot.iterdir()} != set(sealed_files)
    ):
        raise ValueError("sealed snapshot directory or file set changed")
    for name, expected in sealed_files.items():
        data = _require_regular_mode(
            sealed_snapshot / name, 0o444, f"sealed snapshot {name}"
        )
        if data != expected:
            raise ValueError(f"sealed snapshot changed: {name}")
    dictionary_tree = _capture_tree(dictionary_snapshot)
    if any(
        entry.mode != (0o555 if entry.kind == "directory" else 0o444)
        for entry in dictionary_tree.values()
    ):
        raise ValueError("Hazkey dictionary snapshot mode changed")
    if (
        _directory_fingerprint(
            dictionary_snapshot, domain="hazkey.dictionary-fingerprint.v1"
        )
        != dictionary_fingerprint
    ):
        raise ValueError("Hazkey dictionary snapshot changed")
    if (
        _directory_fingerprint(
            b0_snapshot, domain="hazkey.mozc-runtime-fingerprint.v1"
        )
        != b0_fingerprint
    ):
        raise ValueError("B0 core artifact snapshot changed")
    b0_tree = _capture_tree(b0_snapshot)
    expected_b0_modes = {
        ".": 0o755,
        "fcitx5-grimodex-mozc-helper": 0o555,
        "mozc.data": 0o444,
        "manifest.json": 0o444,
    }
    if set(b0_tree) != set(expected_b0_modes) or any(
        b0_tree[path].mode != mode for path, mode in expected_b0_modes.items()
    ):
        raise ValueError("B0 core artifact snapshot layout or mode changed")
    _verify_python_snapshot(python_snapshot, python_sources)


def _verify_source_inputs(
    *,
    executable: Path,
    executable_identity: tuple[int, int],
    executable_bytes: bytes,
    runtime_library_directory: Path,
    runtime_identity: tuple[int, int],
    runtime_dependencies: dict[str, bytes],
    runtime_contract: dict[str, Any],
    sealed_generation: Path,
    sealed_identity: tuple[int, int],
    sealed_files: dict[str, bytes],
    hazkey_dictionary: Path,
    dictionary_identity: tuple[int, int],
    dictionary_fingerprint: str,
    b0_bundle: Path,
    b0_identity: tuple[int, int],
    b0_freeze: CandidateFreeze,
    contract: FrozenContract,
) -> None:
    _verify_root_identity(
        executable, executable_identity, "executable", directory=False
    )
    current_executable, metadata = _read_stable_regular(executable, "executable")
    if current_executable != executable_bytes or not metadata.st_mode & 0o111:
        raise ValueError("executable changed during acquisition")

    _verify_root_identity(
        runtime_library_directory,
        runtime_identity,
        "runtime-lib-dir",
        directory=True,
    )
    current_runtime, current_runtime_contract = (
        v1_acquisition._runtime_dependency_contract(runtime_library_directory)
    )
    if current_runtime != runtime_dependencies or current_runtime_contract != runtime_contract:
        raise ValueError("runtime dependencies changed during acquisition")

    _verify_root_identity(
        sealed_generation, sealed_identity, "sealed-generation", directory=True
    )
    current_sealed, _, _ = _validate_sealed_inputs(sealed_generation, contract)
    if current_sealed != sealed_files:
        raise ValueError("sealed corpus inputs changed during acquisition")

    _verify_root_identity(
        hazkey_dictionary,
        dictionary_identity,
        "hazkey-dictionary",
        directory=True,
    )
    if (
        _directory_fingerprint(
            hazkey_dictionary, domain="hazkey.dictionary-fingerprint.v1"
        )
        != dictionary_fingerprint
    ):
        raise ValueError("Hazkey dictionary source changed during acquisition")

    _verify_root_identity(b0_bundle, b0_identity, "b0-bundle", directory=True)
    _read_b0_core(b0_bundle, b0_freeze)


def _make_tree_removable(path: Path) -> None:
    if not path.exists() or path.is_symlink():
        return
    for directory, names, _ in os.walk(path, topdown=False):
        for name in names:
            child = Path(directory) / name
            if child.is_dir() and not child.is_symlink():
                child.chmod(0o700)
        Path(directory).chmod(0o700)


def acquire(
    *,
    executable: Path,
    runtime_library_directory: Path,
    sealed_generation: Path,
    hazkey_dictionary: Path,
    b0_bundle: Path,
    output_directory: Path,
    contract: FrozenContract = FIXED_CONTRACT,
) -> dict[str, Any]:
    if re.fullmatch(r"[0-9a-f]{40}", contract.product_source_ref) is None:
        raise ValueError("frozen product source ref is invalid")

    executable = _canonical_input_root(executable, "executable", directory=False)
    runtime_library_directory = _canonical_input_root(
        runtime_library_directory, "runtime-lib-dir", directory=True
    )
    sealed_generation = _canonical_input_root(
        sealed_generation, "sealed-generation", directory=True
    )
    hazkey_dictionary = _canonical_input_root(
        hazkey_dictionary, "hazkey-dictionary", directory=True
    )
    b0_bundle = _canonical_input_root(b0_bundle, "b0-bundle", directory=True)
    source_identities = {
        "executable": _root_identity(executable),
        "runtime": _root_identity(runtime_library_directory),
        "sealed": _root_identity(sealed_generation),
        "dictionary": _root_identity(hazkey_dictionary),
        "b0": _root_identity(b0_bundle),
    }

    sealed_files, rows, _ = _validate_sealed_inputs(sealed_generation, contract)
    executable_bytes, executable_metadata = _read_stable_regular(
        executable, "executable"
    )
    if not executable_metadata.st_mode & 0o111:
        raise ValueError("executable must be an executable regular file")
    if (
        len(executable_bytes) != contract.executable_size_bytes
        or _sha256(executable_bytes) != contract.executable_sha256
    ):
        raise ValueError("executable does not match the frozen v2 runner")

    runtime_dependencies, runtime_contract = (
        v1_acquisition._runtime_dependency_contract(runtime_library_directory)
    )
    if runtime_contract["integrity"] != contract.runtime_dependencies_integrity:
        raise ValueError("runtime dependencies do not match the frozen v2 runner")
    python_sources = _capture_python_sources()

    parent, output_name, parent_fd, parent_identity = _open_output_parent(
        output_directory
    )
    canonical_output = parent / output_name
    lock_name = f".{output_name}.lock"
    lock_descriptor = -1
    lock_created = False
    lock_identity: tuple[int, int] | None = None
    temporary_name = ""
    temporary = Path()
    committed = False
    result: dict[str, Any] | None = None
    pending_error: BaseException | None = None
    try:
        _assert_parent_identity(parent, parent_fd, parent_identity)
        if _entry_exists_at(parent_fd, output_name):
            raise ValueError(f"refusing to overwrite output {canonical_output}")
        lock_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            lock_flags |= os.O_NOFOLLOW
        lock_descriptor = os.open(
            lock_name, lock_flags, 0o600, dir_fd=parent_fd
        )
        lock_created = True
        lock_metadata = os.fstat(lock_descriptor)
        if not stat.S_ISREG(lock_metadata.st_mode):
            raise ValueError("new acquisition lock is not a regular file")
        lock_identity = (lock_metadata.st_dev, lock_metadata.st_ino)
        os.fsync(lock_descriptor)
        os.close(lock_descriptor)
        lock_descriptor = -1
        os.fsync(parent_fd)
        temporary_name, temporary = _create_temporary_at(parent_fd, output_name)

        executable_snapshot, library_snapshot = (
            v1_acquisition._create_runtime_snapshot(
                temporary, executable_bytes, runtime_dependencies
            )
        )
        python_snapshot, python_contract = _snapshot_python_sources(
            temporary, python_sources
        )
        input_root = temporary / INPUT_ROOT_NAME
        input_root.mkdir(mode=0o700)
        sealed_snapshot = input_root / SEALED_SNAPSHOT_NAME
        sealed_contract = _snapshot_sealed_files(
            sealed_snapshot, sealed_files, sealed_generation
        )
        dictionary_snapshot = input_root / DICTIONARY_SNAPSHOT_NAME
        dictionary_contract = _snapshot_dictionary(
            hazkey_dictionary, dictionary_snapshot
        )
        if (
            dictionary_contract["fingerprint"]
            != contract.hazkey_dictionary_fingerprint
        ):
            raise ValueError("Hazkey dictionary does not match the frozen baseline")
        b0_parent = input_root / B0_SNAPSHOT_NAME
        b0_snapshot, b0_contract = _snapshot_b0(
            b0_bundle, b0_parent, contract.b0
        )
        input_root.chmod(0o555)
        v1_acquisition._fsync_directory(input_root)

        _verify_snapshots(
            executable_snapshot=executable_snapshot,
            executable_bytes=executable_bytes,
            library_snapshot=library_snapshot,
            runtime_dependencies=runtime_dependencies,
            sealed_snapshot=sealed_snapshot,
            sealed_files=sealed_files,
            dictionary_snapshot=dictionary_snapshot,
            dictionary_fingerprint=contract.hazkey_dictionary_fingerprint,
            b0_snapshot=b0_snapshot,
            b0_fingerprint=contract.b0.resource_fingerprint,
            python_snapshot=python_snapshot,
            python_sources=python_sources,
        )
        _verify_source_inputs(
            executable=executable,
            executable_identity=source_identities["executable"],
            executable_bytes=executable_bytes,
            runtime_library_directory=runtime_library_directory,
            runtime_identity=source_identities["runtime"],
            runtime_dependencies=runtime_dependencies,
            runtime_contract=runtime_contract,
            sealed_generation=sealed_generation,
            sealed_identity=source_identities["sealed"],
            sealed_files=sealed_files,
            hazkey_dictionary=hazkey_dictionary,
            dictionary_identity=source_identities["dictionary"],
            dictionary_fingerprint=contract.hazkey_dictionary_fingerprint,
            b0_bundle=b0_bundle,
            b0_identity=source_identities["b0"],
            b0_freeze=contract.b0,
            contract=contract,
        )
        _verify_python_sources(python_sources)

        host = v1_acquisition._host_contract()
        entries: list[dict[str, Any]] = []
        quality_reports: dict[str, dict[str, Any]] = {}
        generated_outputs: dict[str, bytes] = {}
        previous_end = 0
        for sequence, (run_id, backend_name, converter_backend) in enumerate(
            RUN_SEQUENCE, 1
        ):
            if sorted(os.sched_getaffinity(0)) != host["effective_cpu_affinity"]:
                raise ValueError("orchestrator CPU affinity changed during acquisition")
            raw_path = temporary / f"{run_id}.jsonl"
            stderr_path = temporary / f"{run_id}.stderr"
            raw_descriptor = os.open(
                raw_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            stderr_descriptor = os.open(
                stderr_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            argv = _command(
                converter_backend, b0_snapshot, contract.product_source_ref
            )
            started = time.monotonic_ns()
            try:
                with os.fdopen(raw_descriptor, "wb") as raw_handle, os.fdopen(
                    stderr_descriptor, "wb"
                ) as stderr_handle:
                    raw_descriptor = -1
                    stderr_descriptor = -1
                    return_code = _run_probe(
                        argv,
                        raw_handle,
                        stderr_handle,
                        run_id,
                        CHILD_ENVIRONMENT,
                        temporary,
                    )
                    raw_handle.flush()
                    stderr_handle.flush()
                    os.fsync(raw_handle.fileno())
                    os.fsync(stderr_handle.fileno())
            finally:
                if raw_descriptor >= 0:
                    os.close(raw_descriptor)
                if stderr_descriptor >= 0:
                    os.close(stderr_descriptor)
            ended = time.monotonic_ns()
            if started < previous_end or ended < started:
                raise ValueError("non-monotonic or overlapping run timestamps")
            previous_end = ended
            if return_code != 0:
                raise ValueError(f"run {run_id} exited with {return_code}")
            raw_bytes = v1_acquisition._read_regular(raw_path, f"run {run_id}")
            stderr_bytes = v1_acquisition._read_regular(
                stderr_path, f"run {run_id} stderr"
            )
            generated_outputs[raw_path.name] = raw_bytes
            generated_outputs[stderr_path.name] = stderr_bytes
            resource_path = (
                dictionary_snapshot if converter_backend == "hazkey" else b0_snapshot
            )
            resource_fingerprint = (
                contract.hazkey_dictionary_fingerprint
                if converter_backend == "hazkey"
                else contract.b0.resource_fingerprint
            )
            loaded = _validate_raw_run(
                raw_bytes,
                str(raw_path),
                rows=rows,
                backend_name=backend_name,
                converter_backend=converter_backend,
                resource_path=resource_path,
                resource_fingerprint=resource_fingerprint,
                contract=contract,
            )
            quality = _quality_report(rows, loaded)
            quality_name = f"{run_id}.quality.json"
            quality_bytes = _json_bytes(quality)
            _write_snapshot_file(temporary / quality_name, quality_bytes, 0o600)
            generated_outputs[quality_name] = quality_bytes
            quality_reports[run_id] = quality
            entries.append(
                {
                    "sequence": sequence,
                    "id": run_id,
                    "backend_name": backend_name,
                    "converter_backend": converter_backend,
                    "argv": argv,
                    "raw": {"path": raw_path.name, "sha256": _sha256(raw_bytes)},
                    "stderr": {
                        "path": stderr_path.name,
                        "sha256": _sha256(stderr_bytes),
                    },
                    "quality_report": {
                        "path": quality_name,
                        "sha256": _sha256(quality_bytes),
                    },
                    "exit_code": return_code,
                    "started_monotonic_ns": started,
                    "ended_monotonic_ns": ended,
                    "host_fingerprint": host["fingerprint"],
                    "effective_cpu_affinity": host["effective_cpu_affinity"],
                }
            )

        objective = build_objective_report(
            quality_reports["H0"],
            quality_reports["B0"],
            corpus_sha256=contract.corpus_sha256,
            hazkey_resource_fingerprint=contract.hazkey_dictionary_fingerprint,
            candidate_id="B0",
            candidate_resource_fingerprint=contract.b0.resource_fingerprint,
            raw_run_sha256={
                entry["id"]: entry["raw"]["sha256"] for entry in entries
            },
        )
        objective_bytes = _json_bytes(objective)
        _write_snapshot_file(
            temporary / OBJECTIVE_REPORT_NAME, objective_bytes, 0o600
        )
        generated_outputs[OBJECTIVE_REPORT_NAME] = objective_bytes
        _verify_snapshots(
            executable_snapshot=executable_snapshot,
            executable_bytes=executable_bytes,
            library_snapshot=library_snapshot,
            runtime_dependencies=runtime_dependencies,
            sealed_snapshot=sealed_snapshot,
            sealed_files=sealed_files,
            dictionary_snapshot=dictionary_snapshot,
            dictionary_fingerprint=contract.hazkey_dictionary_fingerprint,
            b0_snapshot=b0_snapshot,
            b0_fingerprint=contract.b0.resource_fingerprint,
            python_snapshot=python_snapshot,
            python_sources=python_sources,
        )
        _verify_source_inputs(
            executable=executable,
            executable_identity=source_identities["executable"],
            executable_bytes=executable_bytes,
            runtime_library_directory=runtime_library_directory,
            runtime_identity=source_identities["runtime"],
            runtime_dependencies=runtime_dependencies,
            runtime_contract=runtime_contract,
            sealed_generation=sealed_generation,
            sealed_identity=source_identities["sealed"],
            sealed_files=sealed_files,
            hazkey_dictionary=hazkey_dictionary,
            dictionary_identity=source_identities["dictionary"],
            dictionary_fingerprint=contract.hazkey_dictionary_fingerprint,
            b0_bundle=b0_bundle,
            b0_identity=source_identities["b0"],
            b0_freeze=contract.b0,
            contract=contract,
        )
        _verify_python_sources(python_sources)
        producer = python_sources["producer"]
        manifest_base = {
            "schema": ACQUISITION_SCHEMA,
            "producer": {
                "path": producer.repository_path,
                "snapshot_path": (
                    f"{PYTHON_SOURCE_SNAPSHOT_NAME}/{producer.snapshot_name}"
                ),
                "size_bytes": len(producer.data),
                "sha256": _sha256(producer.data),
            },
            "python_sources": python_contract,
            "sealed_corpus": sealed_contract,
            "evaluation_runner": {
                "source_path": str(executable),
                "snapshot_path": "runtime/hazkey-server",
                "product_source_ref": contract.product_source_ref,
                "size_bytes": len(executable_bytes),
                "sha256": _sha256(executable_bytes),
            },
            "runtime_dependencies": {
                "source_path": str(runtime_library_directory),
                "snapshot_path": "runtime/lib",
                **runtime_contract,
            },
            "hazkey_dictionary": dictionary_contract,
            "candidates": {
                "B0": {"id": "B0", "status": "evaluated", **b0_contract},
                "B1": {
                    "id": "B1",
                    "status": "frozen_not_evaluated",
                    **_freeze_object(contract.b1),
                    "resource_fingerprint": contract.b1.resource_fingerprint,
                },
            },
            "environment": {
                "policy": "private-runtime-snapshot-v2-objective",
                "cwd": "acquisition-root",
                "ambient_inheritance": False,
                "values": CHILD_ENVIRONMENT,
            },
            "host": host,
            "measurement": {
                "purpose": "objective-quality-only",
                "execution_order": [item[0] for item in RUN_SEQUENCE],
                "warmups_per_case": WARMUPS,
                "iterations_per_case": ITERATIONS,
                "top_k": TOP_K,
                "cases": TOTAL_CASES,
                "quality_cases": QUALITY_CASES,
                "raw_schema": RAW_SCHEMA,
                "per_run_timeout_seconds": PER_RUN_TIMEOUT_SECONDS,
                "latency_and_pss_are_formal_gate_evidence": False,
            },
            "entries": entries,
            "objective_quality": {
                "path": OBJECTIVE_REPORT_NAME,
                "sha256": _sha256(objective_bytes),
                "passed": objective["passed"],
                "next_step": objective["next_step"],
            },
        }
        manifest = manifest_base | {
            "integrity": _sha256(_canonical_json(manifest_base))
        }
        manifest_bytes = _json_bytes(manifest)
        _write_snapshot_file(
            temporary / ACQUISITION_MANIFEST_NAME,
            manifest_bytes,
            0o600,
        )
        generated_outputs[ACQUISITION_MANIFEST_NAME] = manifest_bytes
        v1_acquisition._fsync_directory(temporary)

        _verify_snapshots(
            executable_snapshot=executable_snapshot,
            executable_bytes=executable_bytes,
            library_snapshot=library_snapshot,
            runtime_dependencies=runtime_dependencies,
            sealed_snapshot=sealed_snapshot,
            sealed_files=sealed_files,
            dictionary_snapshot=dictionary_snapshot,
            dictionary_fingerprint=contract.hazkey_dictionary_fingerprint,
            b0_snapshot=b0_snapshot,
            b0_fingerprint=contract.b0.resource_fingerprint,
            python_snapshot=python_snapshot,
            python_sources=python_sources,
        )
        _verify_generated_outputs(temporary, generated_outputs)
        _verify_python_sources(python_sources)
        _verify_source_inputs(
            executable=executable,
            executable_identity=source_identities["executable"],
            executable_bytes=executable_bytes,
            runtime_library_directory=runtime_library_directory,
            runtime_identity=source_identities["runtime"],
            runtime_dependencies=runtime_dependencies,
            runtime_contract=runtime_contract,
            sealed_generation=sealed_generation,
            sealed_identity=source_identities["sealed"],
            sealed_files=sealed_files,
            hazkey_dictionary=hazkey_dictionary,
            dictionary_identity=source_identities["dictionary"],
            dictionary_fingerprint=contract.hazkey_dictionary_fingerprint,
            b0_bundle=b0_bundle,
            b0_identity=source_identities["b0"],
            b0_freeze=contract.b0,
            contract=contract,
        )
        pre_freeze_tree = _capture_tree(temporary)
        frozen_tree = _frozen_tree_contract(pre_freeze_tree)
        _freeze_tree(temporary)
        _verify_tree(temporary, frozen_tree, "pre-publication")
        _verify_python_sources(python_sources)
        _assert_parent_identity(parent, parent_fd, parent_identity)

        temporary_metadata = temporary.lstat()
        try:
            _rename_noreplace_at(parent_fd, temporary_name, output_name)
        except BaseException:
            try:
                destination_metadata = os.stat(
                    output_name, dir_fd=parent_fd, follow_symlinks=False
                )
            except FileNotFoundError:
                pass
            else:
                committed = (
                    stat.S_ISDIR(destination_metadata.st_mode)
                    and (destination_metadata.st_dev, destination_metadata.st_ino)
                    == (temporary_metadata.st_dev, temporary_metadata.st_ino)
                )
            raise
        committed = True
        destination_metadata = os.stat(
            output_name, dir_fd=parent_fd, follow_symlinks=False
        )
        if (
            not stat.S_ISDIR(destination_metadata.st_mode)
            or (destination_metadata.st_dev, destination_metadata.st_ino)
            != (temporary_metadata.st_dev, temporary_metadata.st_ino)
        ):
            raise ValueError("published evidence root identity changed")
        published_path = Path(f"/proc/self/fd/{parent_fd}") / output_name
        _verify_tree(published_path, frozen_tree, "post-publication")
        _assert_parent_identity(parent, parent_fd, parent_identity)
        if lock_identity is None:
            raise ValueError("acquisition lock identity is unavailable")
        _unlink_owned_lock(parent_fd, lock_name, lock_identity)
        lock_created = False
        lock_identity = None
        os.fsync(parent_fd)
        _assert_parent_identity(parent, parent_fd, parent_identity)
        result = manifest
    except BaseException as error:
        pending_error = error
    finally:
        if lock_descriptor >= 0:
            try:
                os.close(lock_descriptor)
            except OSError as error:
                if pending_error is None:
                    pending_error = error
        if lock_created:
            try:
                if lock_identity is None:
                    raise ValueError("acquisition lock identity is unavailable")
                _unlink_owned_lock(parent_fd, lock_name, lock_identity)
                lock_created = False
                lock_identity = None
            except (OSError, ValueError) as error:
                if pending_error is None:
                    pending_error = error
        if not committed and temporary_name and _entry_exists_at(
            parent_fd, temporary_name
        ):
            try:
                _make_tree_removable(temporary)
                shutil.rmtree(temporary)
            except OSError as error:
                if pending_error is None:
                    pending_error = error
        try:
            os.fsync(parent_fd)
        except OSError as error:
            if pending_error is None:
                pending_error = error
        try:
            _assert_parent_identity(parent, parent_fd, parent_identity)
        except (OSError, ValueError) as error:
            if pending_error is None:
                pending_error = error
        try:
            os.close(parent_fd)
        except OSError as error:
            if pending_error is None:
                pending_error = error

    if pending_error is not None:
        if committed:
            raise EvidenceCommittedError(
                canonical_output, str(pending_error)
            ) from pending_error
        raise pending_error.with_traceback(pending_error.__traceback__)
    if result is None:
        raise RuntimeError("objective acquisition ended without a result")
    return result


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--runtime-lib-dir", type=Path, required=True)
    parser.add_argument("--sealed-generation", type=Path, required=True)
    parser.add_argument("--hazkey-dictionary", type=Path, required=True)
    parser.add_argument("--b0-bundle", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        manifest = acquire(
            executable=args.executable,
            runtime_library_directory=args.runtime_lib_dir,
            sealed_generation=args.sealed_generation,
            hazkey_dictionary=args.hazkey_dictionary,
            b0_bundle=args.b0_bundle,
            output_directory=args.output_dir,
        )
        print(f"{manifest['integrity']} {args.output_dir}")
        return 0
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
