#!/usr/bin/env python3
"""Acquire the authorized Mozc v2 B1 objective-quality continuation.

The accepted B0 acquisition remains immutable and is never re-run.  This
producer authenticates that acquisition from raw evidence, copies its exact H0
raw baseline plus the fixed runner/runtime/corpus into a private snapshot, runs
only B1, independently re-scores H0/B1, and publishes a no-replace read-only
evidence directory.  Its result can only advance B1 to the remaining human,
performance, and stability gates; it can never authorize product adoption or
B2 evaluation.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
from typing import Any, Iterable

if __package__:
    from . import authorize_mozc_v2_b1 as authorizer
    from . import evaluate_mozc_adoption_v2_gate as formal_gate
    from . import run_mozc_b0_measurement as v1_acquisition
    from . import run_mozc_v2_objective as v2_acquisition
    from . import summarize_ab_probe
else:
    import authorize_mozc_v2_b1 as authorizer  # type: ignore[no-redef]
    import evaluate_mozc_adoption_v2_gate as formal_gate  # type: ignore[no-redef]
    import run_mozc_b0_measurement as v1_acquisition  # type: ignore[no-redef]
    import run_mozc_v2_objective as v2_acquisition  # type: ignore[no-redef]
    import summarize_ab_probe  # type: ignore[no-redef]


ACQUISITION_SCHEMA = "hazkey.mozc-v2-b1-continuation-acquisition.v1"
OBJECTIVE_SCHEMA = "hazkey.mozc-v2-b1-continuation-objective-quality.v1"
AUTHORIZATION_SNAPSHOT = "b1-authorization.json"
MANIFEST_NAME = "acquisition-manifest.json"
OBJECTIVE_NAME = "objective-quality.json"
PRIOR_MANIFEST_NAME = "prior-b0-acquisition-manifest.json"
PYTHON_SOURCE_DIRECTORY = "python-sources"
RUNNER_ARGUMENT = "./runtime/hazkey-server"
LIBRARY_ARGUMENT = "./runtime/lib"
CORPUS_ARGUMENT = "./inputs/sealed/formal-corpus.tsv"
TOTAL_CASES = 1_360
QUALITY_CASES = 1_260
TOP_K = 10
WARMUPS = 0
ITERATIONS = 1
PER_RUN_TIMEOUT_SECONDS = 900
RENAME_NOREPLACE = 1
CHILD_ENVIRONMENT = {
    "GGML_BACKEND_DIR": LIBRARY_ARGUMENT,
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "LD_LIBRARY_PATH": LIBRARY_ARGUMENT,
    "PATH": os.defpath,
    "TZ": "UTC",
}


@dataclass(frozen=True)
class PriorSnapshot:
    root_identity: tuple[int, int]
    root_leaf: str
    manifest: dict[str, Any]
    manifest_bytes: bytes
    runner: bytes
    runtime_files: dict[str, bytes]
    sealed_files: dict[str, bytes]
    h0_raw: bytes
    h0_quality: bytes


@dataclass(frozen=True)
class SourceSnapshot:
    source_id: str
    repository_path: str
    snapshot_name: str
    path: Path
    data: bytes
    identity: tuple[int, int]


class EvidencePublicationError(OSError):
    """Evidence was retained because safe automatic removal was not assured."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _load_json(data: bytes, context: str) -> dict[str, Any]:
    return v2_acquisition._load_json_bytes(data, context)


def _canonical_input(path: Path, context: str, *, directory: bool) -> Path:
    return v2_acquisition._canonical_input_root(path, context, directory=directory)


def _read_stable_input(path: Path, context: str) -> tuple[bytes, tuple[int, int]]:
    canonical = _canonical_input(path, context, directory=False)
    data, metadata = v2_acquisition._read_stable_regular(canonical, context)
    return data, (metadata.st_dev, metadata.st_ino)


def _verify_stable_input(
    path: Path, context: str, expected_bytes: bytes, identity: tuple[int, int]
) -> None:
    data, current = _read_stable_input(path, context)
    if current != identity or data != expected_bytes:
        raise ValueError(f"{context} changed during acquisition")


def _read_regular_at(root_fd: int, relative: str, context: str) -> tuple[bytes, int]:
    parts = Path(relative).parts
    if (
        not parts
        or Path(relative).is_absolute()
        or any(part in {"", ".", ".."} or "/" in part for part in parts)
    ):
        raise ValueError(f"{context} has an unsafe relative path")
    directory_fd = os.dup(root_fd)
    directory_flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        for component in parts[:-1]:
            before = os.stat(component, dir_fd=directory_fd, follow_symlinks=False)
            if not stat.S_ISDIR(before.st_mode):
                raise ValueError(f"{context} parent is not a directory")
            child = os.open(component, directory_flags, dir_fd=directory_fd)
            opened = os.fstat(child)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                os.close(child)
                raise ValueError(f"{context} parent changed while opening")
            os.close(directory_fd)
            directory_fd = child
        name = parts[-1]
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError(f"{context} must be a non-hardlinked regular file")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(name, flags, dir_fd=directory_fd)
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise ValueError(f"{context} changed before open")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        final = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_mode", "st_nlink")
        if any(getattr(opened, key) != getattr(after, key) for key in fields) or any(
            getattr(after, key) != getattr(final, key) for key in fields
        ):
            raise ValueError(f"{context} changed while read")
        data = b"".join(chunks)
        if len(data) != final.st_size:
            raise ValueError(f"{context} size changed while read")
        return data, stat.S_IMODE(final.st_mode)
    finally:
        os.close(directory_fd)


def _capture_prior(root: Path, authorization: dict[str, Any]) -> PriorSnapshot:
    root = _canonical_input(root, "prior B0 acquisition", directory=True)
    root_before = root.lstat()
    flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    root_fd = os.open(root, flags)
    try:
        opened = os.fstat(root_fd)
        root_identity = (opened.st_dev, opened.st_ino)
        if root_identity != (root_before.st_dev, root_before.st_ino):
            raise ValueError("prior B0 acquisition changed while opening")
        manifest_bytes, manifest_mode = _read_regular_at(
            root_fd, "acquisition-manifest.json", "prior acquisition manifest"
        )
        if manifest_mode != 0o444:
            raise ValueError("prior acquisition manifest mode mismatch")
        manifest = _load_json(manifest_bytes, "prior acquisition manifest")
        accepted = authorization["acquisition"]
        if (
            _sha256(manifest_bytes) != accepted["manifest_sha256"]
            or manifest.get("integrity") != accepted["manifest_integrity"]
            or root.name != accepted["root_leaf"]
        ):
            raise ValueError("prior acquisition manifest is not authorization-bound")

        runner_ref = manifest["evaluation_runner"]
        runner, runner_mode = _read_regular_at(
            root_fd, runner_ref["snapshot_path"], "accepted evaluation runner"
        )
        if (
            runner_mode != 0o555
            or len(runner) != runner_ref["size_bytes"]
            or _sha256(runner) != runner_ref["sha256"]
            or runner_ref["sha256"] != authorization["resources"]["evaluation_runner_sha256"]
        ):
            raise ValueError("accepted evaluation runner identity mismatch")

        runtime_ref = manifest["runtime_dependencies"]
        runtime_files: dict[str, bytes] = {}
        runtime_entries: list[dict[str, Any]] = []
        for item in runtime_ref["files"]:
            path = f"{runtime_ref['snapshot_path']}/{item['path']}"
            data, mode = _read_regular_at(root_fd, path, f"accepted runtime {item['path']}")
            if mode != 0o555 or len(data) != item["size_bytes"] or _sha256(data) != item["sha256"]:
                raise ValueError(f"accepted runtime dependency mismatch: {item['path']}")
            runtime_files[item["path"]] = data
            runtime_entries.append(dict(item))
        runtime_base = {"schema": runtime_ref["schema"], "files": runtime_entries}
        if (
            runtime_ref["integrity"] != _sha256(_canonical_json(runtime_base))
            or runtime_ref["integrity"] != authorization["resources"]["runtime_dependencies_integrity"]
        ):
            raise ValueError("accepted runtime dependency integrity mismatch")

        sealed_ref = manifest["sealed_corpus"]
        sealed_files: dict[str, bytes] = {}
        expected_sealed = {
            "corpus-policy.json": sealed_ref["policy"]["sha256"],
            "manifest.json": sealed_ref["manifest"]["sha256"],
            "formal-corpus.tsv": sealed_ref["corpus"]["sha256"],
        }
        for name, expected in expected_sealed.items():
            data, mode = _read_regular_at(
                root_fd, f"{sealed_ref['snapshot_path']}/{name}", f"accepted sealed {name}"
            )
            if mode != 0o444 or _sha256(data) != expected:
                raise ValueError(f"accepted sealed input mismatch: {name}")
            sealed_files[name] = data
        h0_raw, h0_raw_mode = _read_regular_at(root_fd, "H0.jsonl", "accepted H0 raw")
        h0_quality, h0_quality_mode = _read_regular_at(
            root_fd, "H0.quality.json", "accepted H0 quality"
        )
        if h0_raw_mode != 0o444 or h0_quality_mode != 0o444:
            raise ValueError("accepted H0 evidence mode mismatch")
        if _sha256(h0_raw) != accepted["raw_runs"]["H0"]:
            raise ValueError("accepted H0 raw hash mismatch")
        root_after = root.lstat()
        if (root_after.st_dev, root_after.st_ino) != root_identity:
            raise ValueError("prior B0 acquisition root changed while captured")
        return PriorSnapshot(
            root_identity=root_identity,
            root_leaf=root.name,
            manifest=manifest,
            manifest_bytes=manifest_bytes,
            runner=runner,
            runtime_files=runtime_files,
            sealed_files=sealed_files,
            h0_raw=h0_raw,
            h0_quality=h0_quality,
        )
    finally:
        os.close(root_fd)


def _verify_prior_root(root: Path, expected: PriorSnapshot) -> None:
    metadata = root.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode) or (
        metadata.st_dev,
        metadata.st_ino,
    ) != expected.root_identity:
        raise ValueError("prior B0 acquisition root identity changed")


def _candidate_core(source: Path, freeze: v2_acquisition.CandidateFreeze) -> dict[str, bytes]:
    source = _canonical_input(source, "B1 bundle", directory=True)
    metadata = source.lstat()
    if stat.S_IMODE(metadata.st_mode) != 0o755 or source.name != freeze.generation:
        raise ValueError("B1 bundle generation identity or mode mismatch")
    expected = {
        "fcitx5-grimodex-mozc-helper": (freeze.helper_size_bytes, freeze.helper_sha256, 0o555),
        "mozc.data": (freeze.data_size_bytes, freeze.data_sha256, 0o444),
        "manifest.json": (None, freeze.manifest_sha256, 0o444),
    }
    result: dict[str, bytes] = {}
    for name, (size, digest, mode) in expected.items():
        data, info = v2_acquisition._read_stable_regular(source / name, f"B1 {name}")
        if info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != mode:
            raise ValueError(f"B1 {name} mode or hardlink mismatch")
        if (size is not None and len(data) != size) or _sha256(data) != digest:
            raise ValueError(f"B1 {name} does not match frozen identity")
        result[name] = data
    manifest = _load_json(result["manifest.json"], "B1 manifest")
    artifacts = manifest.get("artifacts")
    if manifest.get("schema") != "grimodex.mozc-artifact-bundle.v1" or not isinstance(
        artifacts, dict
    ) or set(artifacts) != {"fcitx5-grimodex-mozc-helper", "mozc.data"}:
        raise ValueError("B1 artifact manifest contract mismatch")
    for name, key in (("fcitx5-grimodex-mozc-helper", "helper"), ("mozc.data", "data")):
        item = artifacts[name]
        if item != {
            "size": len(result[name]),
            "sha256": _sha256(result[name]).removeprefix("sha256:"),
        }:
            raise ValueError(f"B1 manifest does not bind {key} artifact")
    fingerprint = v2_acquisition._fingerprint_entries(
        [
            (name.encode("utf-8"), hashlib.sha256(data).digest())
            for name, data in result.items()
        ],
        domain="hazkey.mozc-runtime-fingerprint.v1",
    )
    if fingerprint != freeze.resource_fingerprint:
        raise ValueError("B1 bundle resource fingerprint mismatch")
    return result


def _verify_candidate_source(
    source: Path,
    identity: tuple[int, int],
    freeze: v2_acquisition.CandidateFreeze,
    expected: dict[str, bytes],
) -> None:
    metadata = source.lstat()
    if (metadata.st_dev, metadata.st_ino) != identity or _candidate_core(source, freeze) != expected:
        raise ValueError("B1 bundle changed during acquisition")


def _command(generation: str, source_ref: str) -> list[str]:
    return [
        RUNNER_ARGUMENT,
        "--ab-probe",
        "--corpus",
        CORPUS_ARGUMENT,
        "--source-ref",
        source_ref,
        "--warmups",
        str(WARMUPS),
        "--iterations",
        str(ITERATIONS),
        "--top-k",
        str(TOP_K),
        "--backend-name",
        "B1",
        "--converter-backend",
        "mozc",
        "--mozc-bundle",
        f"./inputs/B1/{generation}",
    ]


def _validate_raw_common(
    data: bytes,
    context: str,
    *,
    rows: list[dict[str, str]],
    backend: str,
    converter: str,
    resource_fingerprint: str,
    source_ref: str,
    corpus_sha256: str,
) -> dict[str, Any]:
    loaded = summarize_ab_probe.load_run_bytes(data, context)
    expected = {
        "schema": authorizer.RAW_SCHEMA,
        "backend": backend,
        "source_ref": source_ref,
        "converter_backend": converter,
        "top_k": TOP_K,
        "warmups": WARMUPS,
        "iterations": ITERATIONS,
        "corpus": {"sha256": corpus_sha256, "cases": TOTAL_CASES},
    }
    for key, value in expected.items():
        if loaded[key] != value:
            raise ValueError(f"{context}: {key} mismatch")
    resource = loaded["resource"]
    expected_kind = "hazkey_dictionary" if converter == "hazkey" else "mozc_runtime_inputs"
    if resource["kind"] != expected_kind or resource["fingerprint"] != resource_fingerprint:
        raise ValueError(f"{context}: resource provenance mismatch")
    if list(loaded["cases"]) != [row["id"] for row in rows]:
        raise ValueError(f"{context}: case IDs or order differ from sealed corpus")
    for row in rows:
        case = loaded["cases"][row["id"]]
        if case["reading"] != row["reading"] or case["category"] != row["category"]:
            raise ValueError(f"{context}: case {row['id']!r} differs from sealed corpus")
    return loaded


def _validate_h0_raw(
    data: bytes,
    rows: list[dict[str, str]],
    policy: formal_gate.ParsedPolicy,
    prior_leaf: str,
    source_ref: str,
) -> dict[str, Any]:
    loaded = _validate_raw_common(
        data,
        "accepted H0 raw",
        rows=rows,
        backend="Hazkey",
        converter="hazkey",
        resource_fingerprint=policy.hazkey_dictionary_fingerprint,
        source_ref=source_ref,
        corpus_sha256=policy.corpus_sha256,
    )
    authorizer._lexical_historical_root(
        loaded["resource"]["path"],
        tuple(Path(policy.b1_raw_resource_suffixes["H0"]).parts),
        prior_leaf,
        "accepted H0 resource path",
    )
    return loaded


def _validate_b1_raw(
    data: bytes,
    rows: list[dict[str, str]],
    policy: formal_gate.ParsedPolicy,
    resource_path: Path,
    source_ref: str,
    output_leaf: str,
) -> dict[str, Any]:
    loaded = _validate_raw_common(
        data,
        "B1 raw",
        rows=rows,
        backend="B1",
        converter="mozc",
        resource_fingerprint=policy.candidate_resource_fingerprints["B1"],
        source_ref=source_ref,
        corpus_sha256=policy.corpus_sha256,
    )
    if Path(loaded["resource"]["path"]).resolve() != resource_path.resolve():
        raise ValueError("B1 raw resource path does not bind the private snapshot")
    raw_path = Path(loaded["resource"]["path"])
    # B1 is not part of the B0 authorization's raw suffix table, so bind its
    # exact frozen suffix here and treat the post-rename path as lexical-only.
    suffix = ("inputs", "B1", resource_path.name)
    parts = raw_path.parts
    if (
        not raw_path.is_absolute()
        or len(parts) <= len(suffix)
        or tuple(parts[-len(suffix) :]) != suffix
        or re.fullmatch(
            rf"\.{re.escape(output_leaf)}\.tmp-[0-9a-f]{{16}}",
            parts[-len(suffix) - 1],
        )
        is None
    ):
        raise ValueError("B1 raw resource path is not tied to the private output generation")
    return loaded


def _objective_report(
    h0_report: dict[str, Any],
    b1_report: dict[str, Any],
    raw_hashes: dict[str, str],
    policy: formal_gate.ParsedPolicy,
) -> dict[str, Any]:
    if set(raw_hashes) != {"H0", "B1"}:
        raise ValueError("B1 objective must bind exactly H0 and B1 raw runs")
    baseline = authorizer._backend_metrics(h0_report)
    candidate = authorizer._backend_metrics(b1_report)
    top1_delta = candidate["top1_hits"] - baseline["top1_hits"]
    top10_delta = candidate["top10_hits"] - baseline["top10_hits"]
    checks: list[dict[str, Any]] = [
        {
            "id": "quality-top1-delta",
            "actual_delta_hits": top1_delta,
            "minimum_delta_hits": policy.gate.minimum_top1_delta_hits,
            "passed": top1_delta >= policy.gate.minimum_top1_delta_hits,
        },
        {
            "id": "quality-top10-delta",
            "actual_delta_hits": top10_delta,
            "minimum_delta_hits": policy.gate.minimum_top10_delta_hits,
            "passed": top10_delta >= policy.gate.minimum_top10_delta_hits,
        },
    ]
    category_deltas: dict[str, int] = {}
    for category, minimum in policy.gate.minimum_category_delta_hits.items():
        delta = (
            candidate["categories"][category]["top1_hits"]
            - baseline["categories"][category]["top1_hits"]
        )
        category_deltas[category] = delta
        checks.append(
            {
                "id": f"category-top1-delta:{category}",
                "actual_delta_hits": delta,
                "minimum_delta_hits": minimum,
                "passed": delta >= minimum,
            }
        )
    protected_hits = candidate["protected"]["top1_hits"]
    checks.append(
        {
            "id": "protected-top1",
            "actual_hits": protected_hits,
            "required_hits": policy.gate.protected_required,
            "passed": protected_hits == policy.gate.protected_required,
        }
    )
    if tuple(item["id"] for item in checks) != policy.b1_mandatory_objective_check_ids:
        raise ValueError("B1 objective check sequence differs from policy")
    passed = all(item["passed"] for item in checks)
    base = {
        "schema": OBJECTIVE_SCHEMA,
        "scope": "B1-objective-quality-continuation",
        "formal_evidence_status": "not_ready",
        "formal_adoption_allowed": False,
        "b2_evaluation_authorized": False,
        "corpus": {
            "cases": TOTAL_CASES,
            "quality_cases": QUALITY_CASES,
            "protected_cases": formal_gate.PROTECTED_CASES,
            "sha256": policy.corpus_sha256,
        },
        "baseline": {
            "id": "Hazkey",
            "resource_fingerprint": policy.hazkey_dictionary_fingerprint,
            "source": "accepted-prior-B0-acquisition",
        },
        "candidate": {
            "id": "B1",
            "resource_fingerprint": policy.candidate_resource_fingerprints["B1"],
        },
        "raw_runs": raw_hashes,
        "backends": {"Hazkey": baseline, "B1": candidate},
        "delta_direction": "B1-minus-Hazkey",
        "deltas": {
            "top1_hits": top1_delta,
            "top10_hits": top10_delta,
            "category_top1_hits": category_deltas,
        },
        "gates": checks,
        "passed": passed,
        "next_step": (
            "continue-b1-human-performance-stability"
            if passed
            else "b1-objective-failed-b2-not-authorized"
        ),
        "not_evaluated": [
            "human_preference",
            "both_bad",
            "warm_latency_p95",
            "pss",
            "stability",
        ],
    }
    return base | {"integrity": _sha256(_canonical_json(base))}


def _write_file(path: Path, data: bytes, mode: int) -> None:
    v2_acquisition._write_snapshot_file(path, data, mode)


def _snapshot_prior(temporary: Path, prior: PriorSnapshot) -> tuple[Path, Path]:
    runtime = temporary / "runtime"
    runtime.mkdir(mode=0o700)
    runner = runtime / "hazkey-server"
    _write_file(runner, prior.runner, 0o500)
    libraries = runtime / "lib"
    libraries.mkdir(mode=0o700)
    for name, data in prior.runtime_files.items():
        _write_file(libraries / name, data, 0o500)
    libraries.chmod(0o555)
    runtime.chmod(0o555)

    inputs = temporary / "inputs"
    inputs.mkdir(mode=0o700)
    sealed = inputs / "sealed"
    sealed.mkdir(mode=0o700)
    for name, data in prior.sealed_files.items():
        _write_file(sealed / name, data, 0o444)
    sealed.chmod(0o555)
    return runner, inputs


def _snapshot_b1(
    inputs: Path,
    source: Path,
    core: dict[str, bytes],
    freeze: v2_acquisition.CandidateFreeze,
) -> tuple[Path, dict[str, Any]]:
    parent = inputs / "B1"
    parent.mkdir(mode=0o700)
    generation = parent / freeze.generation
    generation.mkdir(mode=0o700)
    for name, mode in {
        "fcitx5-grimodex-mozc-helper": 0o555,
        "mozc.data": 0o444,
        "manifest.json": 0o444,
    }.items():
        _write_file(generation / name, core[name], mode)
    generation.chmod(0o755)
    parent.chmod(0o555)
    inputs.chmod(0o555)
    fingerprint = v2_acquisition._directory_fingerprint(
        generation, domain="hazkey.mozc-runtime-fingerprint.v1"
    )
    if fingerprint != freeze.resource_fingerprint:
        raise ValueError("private B1 snapshot fingerprint mismatch")
    return generation, {
        "id": "B1",
        "status": "evaluated",
        "source_path": str(source),
        "snapshot_path": f"inputs/B1/{freeze.generation}",
        **v2_acquisition._freeze_object(freeze),
        "resource_fingerprint": fingerprint,
    }


def _freeze_tree_b1(
    root: Path,
    generation: str,
    expected_before: dict[str, v2_acquisition.TreeEntry],
) -> dict[str, v2_acquisition.TreeEntry]:
    v2_acquisition._verify_tree(root, expected_before, "generated pre-freeze")
    flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    root_fd = os.open(root, flags)
    generation_relative = f"inputs/B1/{generation}"

    def freeze(directory_fd: int, prefix: str) -> None:
        names = sorted(os.listdir(directory_fd), key=lambda item: os.fsencode(item))
        for name in names:
            before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            relative = f"{prefix}/{name}" if prefix else name
            if stat.S_ISDIR(before.st_mode):
                child = os.open(name, flags, dir_fd=directory_fd)
                try:
                    opened = os.fstat(child)
                    if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                        raise ValueError(f"evidence directory changed before freeze: {relative}")
                    freeze(child, relative)
                finally:
                    os.close(child)
            elif stat.S_ISREG(before.st_mode) and before.st_nlink == 1:
                file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(name, file_flags, dir_fd=directory_fd)
                try:
                    opened = os.fstat(descriptor)
                    if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                        raise ValueError(f"evidence file changed before freeze: {relative}")
                    os.fchmod(descriptor, 0o555 if opened.st_mode & 0o111 else 0o444)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            else:
                raise ValueError(f"evidence contains symlink, hardlink, or special entry: {relative}")
        os.fchmod(directory_fd, 0o755 if prefix == generation_relative else 0o555)
        os.fsync(directory_fd)

    try:
        freeze(root_fd, "")
    finally:
        os.close(root_fd)
    frozen: dict[str, v2_acquisition.TreeEntry] = {}
    for relative, entry in expected_before.items():
        if entry.kind == "directory":
            expected_mode = 0o755 if relative == generation_relative else 0o555
            frozen[relative] = v2_acquisition.TreeEntry("directory", expected_mode)
        else:
            expected_mode = 0o555 if entry.mode & 0o111 else 0o444
            frozen[relative] = v2_acquisition.TreeEntry(
                "file", expected_mode, entry.size_bytes, entry.sha256
            )
    v2_acquisition._verify_tree(root, frozen, "generated post-freeze")
    return frozen


def _capture_sources() -> dict[str, SourceSnapshot]:
    repository = Path(__file__).resolve().parents[2]
    modules = {
        "producer": (sys.modules[__name__], "run_mozc_v2_b1_objective.py"),
        "authorizer": (authorizer, "authorize_mozc_v2_b1.py"),
        "formal_gate": (formal_gate, "evaluate_mozc_adoption_v2_gate.py"),
        "v2_acquisition": (v2_acquisition, "run_mozc_v2_objective.py"),
        "v1_acquisition": (v1_acquisition, "run_mozc_b0_measurement.py"),
        "probe_summarizer": (summarize_ab_probe, "summarize_ab_probe.py"),
        "quality_evaluator": (
            v2_acquisition.evaluate_conversion_quality,
            "evaluate_conversion_quality.py",
        ),
    }
    snapshots: dict[str, SourceSnapshot] = {}
    for source_id, (module, name) in modules.items():
        expected = repository / "tools/dictionary" / name
        actual = Path(module.__file__).resolve(strict=True)
        if actual != expected:
            raise ValueError(f"executed Python source path mismatch: {source_id}")
        data, metadata = v2_acquisition._read_stable_regular(
            actual, f"executed Python source {source_id}"
        )
        snapshots[source_id] = SourceSnapshot(
            source_id,
            f"tools/dictionary/{name}",
            name,
            actual,
            data,
            (metadata.st_dev, metadata.st_ino),
        )
    return snapshots


def _verify_sources(sources: dict[str, SourceSnapshot]) -> None:
    for source in sources.values():
        _verify_stable_input(
            source.path,
            f"executed Python source {source.source_id}",
            source.data,
            source.identity,
        )


def _copy_sources(temporary: Path, sources: dict[str, SourceSnapshot]) -> dict[str, Any]:
    directory = temporary / PYTHON_SOURCE_DIRECTORY
    directory.mkdir(mode=0o700)
    files: list[dict[str, Any]] = []
    for source in sources.values():
        target = directory / source.snapshot_name
        _write_file(target, source.data, 0o444)
        files.append(
            {
                "id": source.source_id,
                "path": source.repository_path,
                "snapshot_path": f"{PYTHON_SOURCE_DIRECTORY}/{source.snapshot_name}",
                "size_bytes": len(source.data),
                "sha256": _sha256(source.data),
            }
        )
    directory.chmod(0o555)
    base = {"schema": "hazkey.mozc-v2-b1-python-sources.v1", "files": files}
    return base | {"integrity": _sha256(_canonical_json(base))}


def _expected_tree(
    *,
    prior: PriorSnapshot,
    candidate: dict[str, bytes],
    contract: v2_acquisition.FrozenContract,
    sources: dict[str, SourceSnapshot],
    generated: dict[str, bytes],
) -> dict[str, v2_acquisition.TreeEntry]:
    directories = {
        ".": 0o700,
        "runtime": 0o555,
        "runtime/lib": 0o555,
        "inputs": 0o555,
        "inputs/sealed": 0o555,
        "inputs/B1": 0o555,
        f"inputs/B1/{contract.b1.generation}": 0o755,
        PYTHON_SOURCE_DIRECTORY: 0o555,
    }
    files: dict[str, tuple[bytes, int]] = {
        "runtime/hazkey-server": (prior.runner, 0o500),
        **{
            f"runtime/lib/{name}": (data, 0o500)
            for name, data in prior.runtime_files.items()
        },
        **{
            f"inputs/sealed/{name}": (data, 0o444)
            for name, data in prior.sealed_files.items()
        },
        **{
            f"inputs/B1/{contract.b1.generation}/{name}": (
                data,
                0o555 if name == "fcitx5-grimodex-mozc-helper" else 0o444,
            )
            for name, data in candidate.items()
        },
        **{
            f"{PYTHON_SOURCE_DIRECTORY}/{source.snapshot_name}": (source.data, 0o444)
            for source in sources.values()
        },
        **{name: (data, 0o600) for name, data in generated.items()},
    }
    return {
        **{
            path: v2_acquisition.TreeEntry("directory", mode)
            for path, mode in directories.items()
        },
        **{
            path: v2_acquisition.TreeEntry("file", mode, len(data), _sha256(data))
            for path, (data, mode) in files.items()
        },
    }


def _verify_fixed_contract(
    prior: PriorSnapshot,
    authorization: dict[str, Any],
    contract: v2_acquisition.FrozenContract,
) -> None:
    manifest = prior.manifest
    runner = manifest["evaluation_runner"]
    if runner["product_source_ref"] != contract.product_source_ref or runner["size_bytes"] != contract.executable_size_bytes or runner["sha256"] != contract.executable_sha256:
        raise ValueError("accepted prior runner differs from the fixed v2 contract")
    runtime = manifest["runtime_dependencies"]
    if runtime["integrity"] != contract.runtime_dependencies_integrity:
        raise ValueError("accepted prior runtime differs from the fixed v2 contract")
    sealed = manifest["sealed_corpus"]
    if (
        sealed["generation"] != contract.sealed_generation
        or sealed["policy"]["sha256"] != contract.policy_sha256
        or sealed["manifest"]["sha256"] != contract.manifest_sha256
        or sealed["corpus"]["sha256"] != contract.corpus_sha256
    ):
        raise ValueError("accepted prior sealed corpus differs from the fixed v2 contract")
    expected_b1 = {
        "id": "B1",
        "status": "frozen_not_evaluated",
        **v2_acquisition._freeze_object(contract.b1),
        "resource_fingerprint": contract.b1.resource_fingerprint,
    }
    actual_b1 = {
        key: manifest["candidates"]["B1"][key]
        for key in expected_b1
    }
    if actual_b1 != expected_b1:
        raise ValueError("accepted prior B1 freeze differs from the fixed contract")
    if authorization["resources"]["B1_resource_fingerprint"] != contract.b1.resource_fingerprint:
        raise ValueError("authorization does not bind the fixed B1 resource")


def acquire(
    *,
    policy_path: Path,
    prior_b0_root: Path,
    authorization_path: Path,
    b1_bundle: Path,
    output_directory: Path,
    contract: v2_acquisition.FrozenContract = v2_acquisition.FIXED_CONTRACT,
) -> dict[str, Any]:
    policy_path = _canonical_input(policy_path, "formal gate policy", directory=False)
    prior_b0_root = _canonical_input(prior_b0_root, "prior B0 acquisition", directory=True)
    authorization_path = _canonical_input(authorization_path, "B1 authorization", directory=False)
    b1_bundle = _canonical_input(b1_bundle, "B1 bundle", directory=True)

    policy_bytes, policy_identity = _read_stable_input(policy_path, "formal gate policy")
    authorization_bytes, authorization_identity = _read_stable_input(
        authorization_path, "B1 authorization"
    )
    policy = formal_gate.load_policy(policy_path)
    if _sha256(policy_bytes) != policy.policy_sha256:
        raise ValueError("formal gate policy bytes changed while parsed")
    authorization_integrity = authorizer.verify_b1_authorization(
        policy_path, prior_b0_root, authorization_bytes
    )
    authorization = _load_json(authorization_bytes, "B1 authorization")
    if (
        authorization["schema"] != authorizer.AUTHORIZATION_SCHEMA
        or authorization["scope"] != authorizer.AUTHORIZATION_SCOPE
        or authorization["formal_adoption_allowed"] is not False
        or authorization["formal_evidence_status"] != "not_ready"
    ):
        raise ValueError("authorization scope cannot start a B1 continuation")
    prior = _capture_prior(prior_b0_root, authorization)
    _verify_fixed_contract(prior, authorization, contract)

    candidate_identity = v2_acquisition._root_identity(b1_bundle)
    candidate_core = _candidate_core(b1_bundle, contract.b1)
    sources = _capture_sources()

    rows = authorizer._load_corpus(prior.sealed_files["formal-corpus.tsv"])
    if _sha256(prior.sealed_files["formal-corpus.tsv"]) != policy.corpus_sha256:
        raise ValueError("accepted sealed corpus does not match formal policy")
    h0_loaded = _validate_h0_raw(
        prior.h0_raw, rows, policy, prior.root_leaf, contract.product_source_ref
    )
    h0_quality = authorizer._quality_report(rows, h0_loaded)
    if _load_json(prior.h0_quality, "accepted H0 quality") != h0_quality:
        raise ValueError("accepted H0 aggregate differs from raw recomputation")

    parent, output_name, parent_fd, parent_identity = v2_acquisition._open_output_parent(
        output_directory
    )
    canonical_output = parent / output_name
    temporary_name = ""
    temporary = Path()
    temporary_identity: tuple[int, int] | None = None
    committed = False
    result: dict[str, Any] | None = None
    error: BaseException | None = None
    try:
        v2_acquisition._assert_parent_identity(parent, parent_fd, parent_identity)
        if v2_acquisition._entry_exists_at(parent_fd, output_name):
            raise ValueError(f"refusing to overwrite output {canonical_output}")
        temporary_name, temporary = v2_acquisition._create_temporary_at(parent_fd, output_name)
        temporary_metadata = temporary.lstat()
        temporary_identity = (temporary_metadata.st_dev, temporary_metadata.st_ino)

        runner_snapshot, inputs = _snapshot_prior(temporary, prior)
        b1_snapshot, b1_contract = _snapshot_b1(
            inputs, b1_bundle, candidate_core, contract.b1
        )
        python_sources = _copy_sources(temporary, sources)
        _write_file(temporary / "H0.jsonl", prior.h0_raw, 0o600)
        _write_file(temporary / "H0.quality.json", prior.h0_quality, 0o600)
        _write_file(temporary / PRIOR_MANIFEST_NAME, prior.manifest_bytes, 0o600)
        _write_file(temporary / AUTHORIZATION_SNAPSHOT, authorization_bytes, 0o600)
        _write_file(temporary / "formal-gate-policy.json", policy_bytes, 0o600)

        host = v1_acquisition._host_contract()
        if sorted(os.sched_getaffinity(0)) != host["effective_cpu_affinity"]:
            raise ValueError("orchestrator CPU affinity changed before B1 run")
        argv = _command(contract.b1.generation, contract.product_source_ref)
        raw_path = temporary / "B1.jsonl"
        stderr_path = temporary / "B1.stderr"
        raw_fd = os.open(raw_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        stderr_fd = os.open(stderr_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        started = time.monotonic_ns()
        try:
            with os.fdopen(raw_fd, "wb") as raw_handle, os.fdopen(stderr_fd, "wb") as stderr_handle:
                raw_fd = -1
                stderr_fd = -1
                return_code = v2_acquisition._run_probe(
                    argv, raw_handle, stderr_handle, "B1", CHILD_ENVIRONMENT, temporary
                )
                raw_handle.flush()
                stderr_handle.flush()
                os.fsync(raw_handle.fileno())
                os.fsync(stderr_handle.fileno())
        finally:
            if raw_fd >= 0:
                os.close(raw_fd)
            if stderr_fd >= 0:
                os.close(stderr_fd)
        ended = time.monotonic_ns()
        if ended < started:
            raise ValueError("B1 run timestamps are non-monotonic")
        if return_code != 0:
            raise ValueError(f"run B1 exited with {return_code}")
        b1_raw, _ = v2_acquisition._read_stable_regular(raw_path, "B1 raw")
        b1_stderr, _ = v2_acquisition._read_stable_regular(stderr_path, "B1 stderr")
        b1_loaded = _validate_b1_raw(
            b1_raw,
            rows,
            policy,
            b1_snapshot,
            contract.product_source_ref,
            output_name,
        )
        b1_quality = authorizer._quality_report(rows, b1_loaded)
        b1_quality_bytes = _json_bytes(b1_quality)
        _write_file(temporary / "B1.quality.json", b1_quality_bytes, 0o600)
        objective = _objective_report(
            h0_quality,
            b1_quality,
            {"H0": _sha256(prior.h0_raw), "B1": _sha256(b1_raw)},
            policy,
        )
        objective["authority"] = {
            "policy_sha256": policy.policy_sha256,
            "authorization_schema": authorization["schema"],
            "authorization_scope": authorization["scope"],
            "authorization_integrity": authorization_integrity,
            "prior_manifest_sha256": authorization["acquisition"]["manifest_sha256"],
            "prior_manifest_integrity": authorization["acquisition"]["manifest_integrity"],
            "prior_tree_digest": authorization["acquisition"]["tree_digest"],
            "accepted_prior_raw_sha256": authorization["acquisition"]["raw_runs"],
            "b0_early_rejection_failed_check_ids": authorization["recomputed"][
                "failed_check_ids"
            ],
        }
        objective_base = {key: value for key, value in objective.items() if key != "integrity"}
        objective["integrity"] = _sha256(_canonical_json(objective_base))
        objective_bytes = _json_bytes(objective)
        _write_file(temporary / OBJECTIVE_NAME, objective_bytes, 0o600)

        prior_binding = {
            "root_leaf": authorization["acquisition"]["root_leaf"],
            "schema": authorization["acquisition"]["schema"],
            "manifest_path": PRIOR_MANIFEST_NAME,
            "manifest_sha256": authorization["acquisition"]["manifest_sha256"],
            "manifest_integrity": authorization["acquisition"]["manifest_integrity"],
            "tree_digest": authorization["acquisition"]["tree_digest"],
            "raw_runs": authorization["acquisition"]["raw_runs"],
            "reused_H0": {
                "raw_path": "H0.jsonl",
                "raw_sha256": _sha256(prior.h0_raw),
                "quality_path": "H0.quality.json",
                "quality_sha256": _sha256(prior.h0_quality),
            },
        }
        entry = {
            "sequence": 1,
            "id": "B1",
            "backend_name": "B1",
            "converter_backend": "mozc",
            "argv": argv,
            "raw": {"path": "B1.jsonl", "sha256": _sha256(b1_raw)},
            "stderr": {"path": "B1.stderr", "sha256": _sha256(b1_stderr)},
            "quality_report": {"path": "B1.quality.json", "sha256": _sha256(b1_quality_bytes)},
            "exit_code": return_code,
            "started_monotonic_ns": started,
            "ended_monotonic_ns": ended,
            "host_fingerprint": host["fingerprint"],
            "effective_cpu_affinity": host["effective_cpu_affinity"],
        }
        runtime_ref = prior.manifest["runtime_dependencies"]
        sealed_ref = prior.manifest["sealed_corpus"]
        manifest_base = {
            "schema": ACQUISITION_SCHEMA,
            "scope": "B1-objective-quality-continuation",
            "formal_evidence_status": "not_ready",
            "formal_adoption_allowed": False,
            "b2_evaluation_authorized": False,
            "python_sources": python_sources,
            "policy": {
                "id": policy.policy_id,
                "path": "formal-gate-policy.json",
                "sha256": policy.policy_sha256,
            },
            "authorization": {
                "path": AUTHORIZATION_SNAPSHOT,
                "sha256": _sha256(authorization_bytes),
                "integrity": authorization_integrity,
                "schema": authorization["schema"],
                "scope": authorization["scope"],
            },
            "prior_b0_acquisition": prior_binding,
            "sealed_corpus": {
                "generation": sealed_ref["generation"],
                "snapshot_path": "inputs/sealed",
                "policy_sha256": sealed_ref["policy"]["sha256"],
                "manifest_sha256": sealed_ref["manifest"]["sha256"],
                "corpus_sha256": sealed_ref["corpus"]["sha256"],
                "cases": TOTAL_CASES,
                "quality_cases": QUALITY_CASES,
            },
            "evaluation_runner": {
                "snapshot_path": "runtime/hazkey-server",
                "product_source_ref": contract.product_source_ref,
                "size_bytes": len(prior.runner),
                "sha256": _sha256(prior.runner),
            },
            "runtime_dependencies": {
                "snapshot_path": "runtime/lib",
                "schema": runtime_ref["schema"],
                "files": runtime_ref["files"],
                "integrity": runtime_ref["integrity"],
            },
            "candidate": b1_contract,
            "environment": {
                "policy": "private-prior-runtime-snapshot-v2-b1-continuation",
                "cwd": "acquisition-root",
                "ambient_inheritance": False,
                "values": CHILD_ENVIRONMENT,
            },
            "host": host,
            "measurement": {
                "purpose": "objective-quality-only",
                "execution_order": ["B1"],
                "baseline_reused": "H0",
                "warmups_per_case": WARMUPS,
                "iterations_per_case": ITERATIONS,
                "top_k": TOP_K,
                "cases": TOTAL_CASES,
                "quality_cases": QUALITY_CASES,
                "raw_schema": authorizer.RAW_SCHEMA,
                "per_run_timeout_seconds": PER_RUN_TIMEOUT_SECONDS,
                "latency_and_pss_are_formal_gate_evidence": False,
                "raw_resource_path_contract": {
                    "run_id": "B1",
                    "resolve_or_open_after_publication": False,
                    "temporary_basename_pattern": (
                        f".{output_name}.tmp-<16-lowercase-hex>"
                    ),
                    "exact_suffix": f"inputs/B1/{contract.b1.generation}",
                },
            },
            "entries": [entry],
            "objective_quality": {
                "path": OBJECTIVE_NAME,
                "sha256": _sha256(objective_bytes),
                "passed": objective["passed"],
                "next_step": objective["next_step"],
            },
        }
        manifest = manifest_base | {"integrity": _sha256(_canonical_json(manifest_base))}
        manifest_bytes = _json_bytes(manifest)
        _write_file(temporary / MANIFEST_NAME, manifest_bytes, 0o600)
        v1_acquisition._fsync_directory(temporary)

        # Re-authenticate the complete accepted raw tree immediately before
        # publication.  This is deliberately after B1 execution.
        _verify_stable_input(policy_path, "formal gate policy", policy_bytes, policy_identity)
        _verify_stable_input(
            authorization_path,
            "B1 authorization",
            authorization_bytes,
            authorization_identity,
        )
        if authorizer.verify_b1_authorization(
            policy_path, prior_b0_root, authorization_bytes
        ) != authorization_integrity:
            raise ValueError("B1 authorization integrity changed before publication")
        _verify_prior_root(prior_b0_root, prior)
        _verify_candidate_source(
            b1_bundle, candidate_identity, contract.b1, candidate_core
        )
        _verify_sources(sources)
        if sorted(os.sched_getaffinity(0)) != host["effective_cpu_affinity"]:
            raise ValueError("orchestrator CPU affinity changed before publication")

        generated = {
            "H0.jsonl": prior.h0_raw,
            "H0.quality.json": prior.h0_quality,
            PRIOR_MANIFEST_NAME: prior.manifest_bytes,
            AUTHORIZATION_SNAPSHOT: authorization_bytes,
            "formal-gate-policy.json": policy_bytes,
            "B1.jsonl": b1_raw,
            "B1.stderr": b1_stderr,
            "B1.quality.json": b1_quality_bytes,
            OBJECTIVE_NAME: objective_bytes,
            MANIFEST_NAME: manifest_bytes,
        }
        expected_before = _expected_tree(
            prior=prior,
            candidate=candidate_core,
            contract=contract,
            sources=sources,
            generated=generated,
        )
        frozen_tree = _freeze_tree_b1(
            temporary, contract.b1.generation, expected_before
        )
        v2_acquisition._verify_tree(temporary, frozen_tree, "pre-publication")
        _verify_stable_input(policy_path, "formal gate policy", policy_bytes, policy_identity)
        _verify_stable_input(
            authorization_path,
            "B1 authorization",
            authorization_bytes,
            authorization_identity,
        )
        if authorizer.verify_b1_authorization(
            policy_path, prior_b0_root, authorization_bytes
        ) != authorization_integrity:
            raise ValueError("B1 authorization integrity changed at publication boundary")
        _verify_prior_root(prior_b0_root, prior)
        _verify_candidate_source(
            b1_bundle, candidate_identity, contract.b1, candidate_core
        )
        _verify_sources(sources)
        v2_acquisition._verify_tree(
            temporary, frozen_tree, "publication-boundary"
        )
        v2_acquisition._assert_parent_identity(parent, parent_fd, parent_identity)
        temporary_at = os.stat(
            temporary_name, dir_fd=parent_fd, follow_symlinks=False
        )
        if temporary_identity is None or not stat.S_ISDIR(temporary_at.st_mode) or (
            temporary_at.st_dev,
            temporary_at.st_ino,
        ) != temporary_identity:
            raise ValueError("B1 temporary evidence identity changed before publication")
        v2_acquisition._rename_noreplace_at(parent_fd, temporary_name, output_name)
        committed = True
        published = os.stat(output_name, dir_fd=parent_fd, follow_symlinks=False)
        if temporary_identity is None or not stat.S_ISDIR(published.st_mode) or (
            published.st_dev,
            published.st_ino,
        ) != temporary_identity:
            raise OSError("published B1 evidence identity changed")
        published_path = Path(f"/proc/self/fd/{parent_fd}") / output_name
        v2_acquisition._verify_tree(published_path, frozen_tree, "post-publication")
        os.fsync(parent_fd)
        v2_acquisition._assert_parent_identity(parent, parent_fd, parent_identity)
        final_published = os.stat(
            output_name, dir_fd=parent_fd, follow_symlinks=False
        )
        if temporary_identity is None or not stat.S_ISDIR(final_published.st_mode) or (
            final_published.st_dev,
            final_published.st_ino,
        ) != temporary_identity:
            raise OSError("published B1 evidence path changed after assurance")
        result = manifest
    except BaseException as caught:
        error = caught
    finally:
        if error is not None and committed:
            error = EvidencePublicationError(
                f"B1 evidence committed at {canonical_output}; post-publication assurance failed: {error}"
            )
        # There is no Linux compare-and-rmtree primitive.  Even a dirfd-pinned
        # recursive walk has a final same-UID leaf replacement race.  Preserve
        # every failed pre-publication tree and report its observed identity;
        # a human may remove it after inspecting the failure.
        if (
            error is not None
            and not committed
            and temporary_name
        ):
            retained_path = parent / temporary_name
            try:
                current_temporary = os.stat(
                    temporary_name, dir_fd=parent_fd, follow_symlinks=False
                )
            except FileNotFoundError:
                detail = (
                    f"B1 temporary pathname disappeared; automatic cleanup skipped "
                    f"for {retained_path} (expected dev={temporary_identity[0] if temporary_identity else 'unknown'}, "
                    f"ino={temporary_identity[1] if temporary_identity else 'unknown'})"
                )
            else:
                if temporary_identity is not None and stat.S_ISDIR(
                    current_temporary.st_mode
                ) and (
                    current_temporary.st_dev,
                    current_temporary.st_ino,
                ) == temporary_identity:
                    detail = (
                        f"owned B1 temporary evidence retained at {retained_path} "
                        f"(dev={temporary_identity[0]}, ino={temporary_identity[1]})"
                    )
                else:
                    detail = (
                        f"B1 temporary pathname was replaced; foreign entry retained at "
                        f"{retained_path} (dev={current_temporary.st_dev}, "
                        f"ino={current_temporary.st_ino})"
                    )
            if isinstance(error, Exception):
                error = EvidencePublicationError(f"{detail}; acquisition failed: {error}")
        try:
            os.close(parent_fd)
        except OSError as close_error:
            if error is None:
                error = close_error
    if error is not None:
        raise error.with_traceback(error.__traceback__)
    if result is None:
        raise RuntimeError("B1 continuation ended without a result")
    return result


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--prior-b0-root", type=Path, required=True)
    parser.add_argument("--authorization", type=Path, required=True)
    parser.add_argument("--b1-bundle", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        manifest = acquire(
            policy_path=args.policy,
            prior_b0_root=args.prior_b0_root,
            authorization_path=args.authorization,
            b1_bundle=args.b1_bundle,
            output_directory=args.output_dir,
        )
        print(f"{manifest['integrity']} {args.output_dir}")
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
