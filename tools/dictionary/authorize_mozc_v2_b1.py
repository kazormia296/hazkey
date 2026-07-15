#!/usr/bin/env python3
"""Authorize B1 evaluation from authenticated B0 v2 raw early-rejection evidence.

This consumer deliberately has a narrow result: it may authorize evaluating
the already-frozen B1 artifact, and can never authorize product adoption.  The
checked-in formal gate policy is the trust anchor.  Aggregate objective and
quality JSON files are verified for consistency, but raw H0/B0 JSONL is always
revalidated and rescored before an authorization is issued.
"""

from __future__ import annotations

import argparse
from collections import Counter
import csv
import ctypes
from dataclasses import dataclass
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
from typing import Any, Iterable

if __package__:
    from . import evaluate_mozc_adoption_v2_gate as formal_gate
    from . import summarize_ab_probe
else:
    import evaluate_mozc_adoption_v2_gate as formal_gate  # type: ignore[no-redef]
    import summarize_ab_probe  # type: ignore[no-redef]


AUTHORIZATION_SCHEMA = "hazkey.mozc-v2-b1-authorization.v1"
AUTHORIZATION_SCOPE = "B1-evaluation-only"
ACQUISITION_MANIFEST = "acquisition-manifest.json"
OBJECTIVE_REPORT = "objective-quality.json"
RAW_SCHEMA = "hazkey.ab-probe-result.v3"
QUALITY_SCHEMA = "hazkey.conversion-quality-report.v1"
OBJECTIVE_SCHEMA = "hazkey.mozc-v2-objective-quality.v1"
PYTHON_SNAPSHOT_SCHEMA = "hazkey.mozc-v2-python-sources.v1"
RUNTIME_SNAPSHOT_SCHEMA = "hazkey.mozc-b0-runtime-dependencies.v1"
TOP_K = 10
WARMUPS = 0
ITERATIONS = 1
TOTAL_CASES = 1_360
QUALITY_CASES = 1_260
PRODUCT_SOURCE_REF = "7373b1a59b2c94a9fada5650984c28ed352c3be1"
RUNNER_SHA256 = (
    "sha256:249c43c8eb02651b685291ad47fd6bd85efac3438abd0a4d284dd1caec11f30a"
)
RUNNER_SIZE = 106_269_232
RUNTIME_INTEGRITY = (
    "sha256:5d847919dbfb4b866546104cfbc73f5ffa9ff45ee9d8bc85889bf1de6c299f2d"
)
RUNTIME_FILES = (
    "libggml-base.so",
    "libggml-cpu-alderlake.so",
    "libggml-cpu-haswell.so",
    "libggml-cpu-icelake.so",
    "libggml-cpu-sandybridge.so",
    "libggml-cpu-sapphirerapids.so",
    "libggml-cpu-skylakex.so",
    "libggml-vulkan.so",
    "libggml.so",
    "libllama.so",
    "vulkan-shaders-gen",
)
SOURCE_LAYOUT = {
    "producer": (
        "tools/dictionary/run_mozc_v2_objective.py",
        "python-sources/run_mozc_v2_objective.py",
    ),
    "v1_acquisition": (
        "tools/dictionary/run_mozc_b0_measurement.py",
        "python-sources/run_mozc_b0_measurement.py",
    ),
    "quality_evaluator": (
        "tools/dictionary/evaluate_conversion_quality.py",
        "python-sources/evaluate_conversion_quality.py",
    ),
    "probe_summarizer": (
        "tools/dictionary/summarize_ab_probe.py",
        "python-sources/summarize_ab_probe.py",
    ),
}
TOP_LEVEL_FILES = {
    ACQUISITION_MANIFEST,
    OBJECTIVE_REPORT,
    "H0.jsonl",
    "H0.stderr",
    "H0.quality.json",
    "B0.jsonl",
    "B0.stderr",
    "B0.quality.json",
}
TOP_LEVEL_DIRECTORIES = {"runtime", "inputs", "python-sources"}
RENAME_NOREPLACE = 1


@dataclass(frozen=True)
class TreeEntry:
    kind: str
    mode: int
    size_bytes: int | None = None
    sha256: str | None = None


@dataclass(frozen=True)
class EvidenceSnapshot:
    root_leaf: str
    entries: dict[str, TreeEntry]
    blobs: dict[str, bytes]
    tree_digest: str


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _pretty_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _json(data: bytes, context: str) -> dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_no_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{context} is not valid UTF-8 JSON: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{context} must contain an object")
    return value


def _exact_keys(value: Any, expected: set[str], context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be an object")
    actual = set(value)
    if actual != expected:
        raise ValueError(
            f"{context} fields differ; missing={sorted(expected - actual)!r}, "
            f"unknown={sorted(actual - expected)!r}"
        )
    return value


def _require_sha(value: Any, context: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
        raise ValueError(f"{context} must be a canonical SHA-256 identity")
    return value


def _blob_required(path: str) -> bool:
    return (
        path in TOP_LEVEL_FILES
        or path.startswith("python-sources/")
        or path.startswith("inputs/sealed/")
        or (path.startswith("inputs/B0/") and path.endswith("/manifest.json"))
    )


def _read_open_file(descriptor: int, before: os.stat_result, context: str) -> tuple[bytes | None, str]:
    keep = _blob_required(context)
    chunks: list[bytes] = []
    hasher = hashlib.sha256()
    total = 0
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        hasher.update(chunk)
        total += len(chunk)
        if keep:
            chunks.append(chunk)
    after = os.fstat(descriptor)
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_mode", "st_nlink")
    if (
        any(getattr(before, field) != getattr(after, field) for field in fields)
        or total != before.st_size
        or before.st_nlink != 1
    ):
        raise ValueError(f"evidence file changed while read: {context}")
    return (b"".join(chunks) if keep else None), "sha256:" + hasher.hexdigest()


def _capture_evidence(root: Path) -> EvidenceSnapshot:
    if not root.is_absolute():
        raise ValueError("acquisition root must be an absolute path")
    leaf = root.name
    if not leaf or leaf in {".", ".."} or "/" in leaf or "\x00" in leaf:
        raise ValueError("acquisition root has an invalid leaf name")
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        path_before = root.lstat()
        if stat.S_ISLNK(path_before.st_mode) or not stat.S_ISDIR(path_before.st_mode):
            raise ValueError("acquisition root must be a non-symlink directory")
        root_fd = os.open(root, flags)
    except OSError as error:
        raise ValueError("acquisition root must be a non-symlink directory") from error
    entries: dict[str, TreeEntry] = {}
    blobs: dict[str, bytes] = {}

    def walk(directory_fd: int, prefix: str) -> None:
        initial = os.fstat(directory_fd)
        names = sorted(os.listdir(directory_fd), key=lambda item: os.fsencode(item))
        for name in names:
            if name in {".", ".."} or "/" in name or "\x00" in name:
                raise ValueError("evidence tree contains an invalid entry name")
            relative = f"{prefix}/{name}" if prefix else name
            before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISDIR(before.st_mode):
                child_fd = os.open(name, flags, dir_fd=directory_fd)
                try:
                    opened = os.fstat(child_fd)
                    if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                        raise ValueError(f"evidence directory changed before open: {relative}")
                    entries[relative] = TreeEntry("directory", stat.S_IMODE(opened.st_mode))
                    walk(child_fd, relative)
                    final = os.fstat(child_fd)
                    if (opened.st_dev, opened.st_ino) != (final.st_dev, final.st_ino):
                        raise ValueError(f"evidence directory changed: {relative}")
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(before.st_mode):
                file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                if hasattr(os, "O_NOFOLLOW"):
                    file_flags |= os.O_NOFOLLOW
                file_fd = os.open(name, file_flags, dir_fd=directory_fd)
                try:
                    opened = os.fstat(file_fd)
                    if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                        raise ValueError(f"evidence file changed before open: {relative}")
                    data, digest = _read_open_file(file_fd, opened, relative)
                finally:
                    os.close(file_fd)
                final = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                identity = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_mode", "st_nlink")
                if any(getattr(opened, field) != getattr(final, field) for field in identity):
                    raise ValueError(f"evidence file changed after read: {relative}")
                entries[relative] = TreeEntry(
                    "file", stat.S_IMODE(opened.st_mode), opened.st_size, digest
                )
                if data is not None:
                    blobs[relative] = data
            else:
                raise ValueError(f"evidence tree contains a symlink or special entry: {relative}")
        final_names = sorted(os.listdir(directory_fd), key=lambda item: os.fsencode(item))
        final = os.fstat(directory_fd)
        if (
            final_names != names
            or (initial.st_dev, initial.st_ino) != (final.st_dev, final.st_ino)
            or initial.st_mtime_ns != final.st_mtime_ns
            or initial.st_ctime_ns != final.st_ctime_ns
        ):
            raise ValueError("evidence directory changed during traversal")

    try:
        opened_root = os.fstat(root_fd)
        if (opened_root.st_dev, opened_root.st_ino) != (
            path_before.st_dev,
            path_before.st_ino,
        ):
            raise ValueError("acquisition root changed while it was opened")
        if stat.S_IMODE(opened_root.st_mode) != 0o555:
            raise ValueError("acquisition root mode must be 0555")
        walk(root_fd, "")
        final_root = os.fstat(root_fd)
        if (opened_root.st_dev, opened_root.st_ino) != (final_root.st_dev, final_root.st_ino):
            raise ValueError("acquisition root identity changed")
        try:
            path_after = root.lstat()
        except OSError as error:
            raise ValueError("acquisition root pathname disappeared during validation") from error
        if (
            stat.S_ISLNK(path_after.st_mode)
            or not stat.S_ISDIR(path_after.st_mode)
            or (path_after.st_dev, path_after.st_ino)
            != (opened_root.st_dev, opened_root.st_ino)
        ):
            raise ValueError("acquisition root pathname identity changed")
    finally:
        os.close(root_fd)
    tree_payload = {
        "schema": "hazkey.mozc-v2-acquisition-tree.v1",
        "entries": [
            {
                "path": path,
                "kind": entry.kind,
                "mode": f"{entry.mode:04o}",
                **(
                    {"size_bytes": entry.size_bytes, "sha256": entry.sha256}
                    if entry.kind == "file"
                    else {}
                ),
            }
            for path, entry in sorted(entries.items(), key=lambda item: item[0].encode())
        ],
    }
    return EvidenceSnapshot(leaf, entries, blobs, _sha256(_canonical_json(tree_payload)))


def _fingerprint(entries: list[tuple[bytes, bytes]], domain: str) -> str:
    hasher = hashlib.sha256()
    hasher.update(domain.encode("utf-8") + b"\0")
    for path, digest in sorted(entries, key=lambda item: item[0]):
        hasher.update(b"\x01")
        hasher.update(len(path).to_bytes(8, "big"))
        hasher.update(path)
        hasher.update(digest)
    return "sha256:" + hasher.hexdigest()


def _validate_tree(snapshot: EvidenceSnapshot, manifest: dict[str, Any], policy: formal_gate.ParsedPolicy) -> None:
    entries = snapshot.entries
    top_actual = {path for path in entries if "/" not in path}
    expected_top = TOP_LEVEL_FILES | TOP_LEVEL_DIRECTORIES
    if top_actual != expected_top:
        raise ValueError("acquisition top-level file set changed")

    b0 = _exact_keys(manifest["candidates"]["B0"], {
        "id", "status", "source_path", "snapshot_path", "generation",
        "helper_size_bytes", "helper_sha256", "data_size_bytes", "data_sha256",
        "manifest_sha256", "resource_fingerprint",
    }, "manifest.candidates.B0")
    generation = b0["generation"]
    if not isinstance(generation, str) or re.fullmatch(r"sha256-[0-9a-f]{64}", generation) is None:
        raise ValueError("manifest B0 generation is invalid")
    b0_root = f"inputs/B0/{generation}"
    fixed_directories = {
        "runtime", "runtime/lib", "inputs", "inputs/sealed", "inputs/Dictionary",
        "inputs/B0", b0_root, "python-sources",
    }
    fixed_files = (
        TOP_LEVEL_FILES
        | {"runtime/hazkey-server"}
        | {f"runtime/lib/{name}" for name in RUNTIME_FILES}
        | {item[1] for item in SOURCE_LAYOUT.values()}
        | {
            "inputs/sealed/corpus-policy.json",
            "inputs/sealed/manifest.json",
            "inputs/sealed/formal-corpus.tsv",
            f"{b0_root}/fcitx5-grimodex-mozc-helper",
            f"{b0_root}/mozc.data",
            f"{b0_root}/manifest.json",
        }
    )
    dictionary_prefix = "inputs/Dictionary/"
    for path, entry in entries.items():
        if entry.kind == "directory":
            if path not in fixed_directories and not path.startswith(dictionary_prefix):
                raise ValueError(f"unexpected evidence directory {path}")
            expected_mode = 0o755 if path == b0_root else 0o555
        else:
            if path not in fixed_files and not path.startswith(dictionary_prefix):
                raise ValueError(f"unexpected evidence file {path}")
            expected_mode = (
                0o555
                if path == "runtime/hazkey-server"
                or path.startswith("runtime/lib/")
                or path == f"{b0_root}/fcitx5-grimodex-mozc-helper"
                else 0o444
            )
        if entry.mode != expected_mode:
            raise ValueError(f"evidence mode mismatch for {path}: {entry.mode:04o}")
    for path in fixed_directories | fixed_files:
        if path not in entries:
            raise ValueError(f"missing evidence entry {path}")

    dictionary_files = [
        (path[len(dictionary_prefix):].encode("utf-8"), bytes.fromhex(entry.sha256[7:]))
        for path, entry in entries.items()
        if entry.kind == "file" and path.startswith(dictionary_prefix)
    ]
    dictionary_size = sum(
        entry.size_bytes or 0
        for path, entry in entries.items()
        if entry.kind == "file" and path.startswith(dictionary_prefix)
    )
    dictionary_directories = [
        path
        for path, entry in entries.items()
        if entry.kind == "directory" and path.startswith(dictionary_prefix)
    ]
    for directory in dictionary_directories:
        if not any(
            path.startswith(directory + "/")
            for path, entry in entries.items()
            if entry.kind == "file"
        ):
            raise ValueError(f"Hazkey dictionary contains an empty directory: {directory}")
    dictionary = _exact_keys(
        manifest["hazkey_dictionary"],
        {"source_path", "snapshot_path", "files", "size_bytes", "fingerprint"},
        "manifest.hazkey_dictionary",
    )
    if (
        len(dictionary_files) != dictionary.get("files")
        or dictionary_size != dictionary.get("size_bytes")
        or dictionary.get("snapshot_path") != "inputs/Dictionary"
        or _fingerprint(dictionary_files, "hazkey.dictionary-fingerprint.v1")
        != policy.hazkey_dictionary_fingerprint
        or dictionary.get("fingerprint") != policy.hazkey_dictionary_fingerprint
    ):
        raise ValueError("Hazkey dictionary snapshot identity mismatch")

    b0_files = [
        (path[len(b0_root) + 1:].encode("utf-8"), bytes.fromhex(entry.sha256[7:]))
        for path, entry in entries.items()
        if entry.kind == "file" and path.startswith(b0_root + "/")
    ]
    if _fingerprint(b0_files, "hazkey.mozc-runtime-fingerprint.v1") != policy.candidate_resource_fingerprints["B0"]:
        raise ValueError("B0 snapshot resource fingerprint mismatch")
    if b0.get("resource_fingerprint") != policy.candidate_resource_fingerprints["B0"]:
        raise ValueError("manifest B0 resource fingerprint mismatch")


def _load_corpus(data: bytes) -> list[dict[str, str]]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("sealed corpus is not UTF-8") from error
    rows = list(csv.DictReader(io.StringIO(text, newline=""), delimiter="\t"))
    if not rows or set(rows[0]) != {"id", "reading", "expected", "category"}:
        raise ValueError("sealed corpus column set changed")
    ids: set[str] = set()
    counts: Counter[str] = Counter()
    for index, row in enumerate(rows, 2):
        if any(not row[field] for field in ("id", "reading", "expected", "category")):
            raise ValueError(f"sealed corpus row {index} has an empty required field")
        if row["id"] in ids:
            raise ValueError(f"sealed corpus duplicate id {row['id']!r}")
        if any(not item for item in row["expected"].split("|")):
            raise ValueError(f"sealed corpus row {index} has an empty expected alternative")
        ids.add(row["id"])
        counts[row["category"]] += 1
    if len(rows) != TOTAL_CASES or dict(counts) != formal_gate.ALL_CATEGORIES:
        raise ValueError("sealed corpus category population changed")
    return rows


def _quality_report(rows: list[dict[str, str]], loaded: dict[str, Any]) -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    by_category: dict[str, Counter[str]] = {}
    for row in rows:
        candidates = loaded["cases"][row["id"]]["candidates"]
        expected = row["expected"].split("|")
        top1 = bool(candidates) and candidates[0] in expected
        top10 = any(item in expected for item in candidates[:TOP_K])
        counter = by_category.setdefault(row["category"], Counter())
        counter["total"] += 1
        counter["top1"] += int(top1)
        counter["top10"] += int(top10)
        cases.append({
            "id": row["id"], "category": row["category"], "reading": row["reading"],
            "expected": expected, "top1": top1, "observed": candidates[:TOP_K], "top10": top10,
        })
    category_reports: dict[str, dict[str, Any]] = {}
    for category, counter in sorted(by_category.items()):
        category_reports[category] = dict(counter) | {
            "top1_rate": counter["top1"] / counter["total"],
            "top10_rate": counter["top10"] / counter["total"],
        }
    top1_hits = sum(case["top1"] for case in cases)
    top10_hits = sum(case["top10"] for case in cases)
    return {
        "schema": QUALITY_SCHEMA,
        "top_k": TOP_K,
        "corpus_cases": len(rows),
        "evaluated_cases": len(cases),
        "missing_results": [],
        "top1_hits": top1_hits,
        "top1_rate": top1_hits / len(cases),
        "by_category": category_reports,
        "cases": cases,
        "top10_hits": top10_hits,
        "top10_rate": top10_hits / len(cases),
    }


def _backend_metrics(report: dict[str, Any]) -> dict[str, Any]:
    categories = {
        category: {
            "cases": cases,
            "top1_hits": report["by_category"][category]["top1"],
            "top10_hits": report["by_category"][category]["top10"],
        }
        for category, cases in formal_gate.QUALITY_CATEGORIES.items()
    }
    protected = report["by_category"][formal_gate.PROTECTED_CATEGORY]
    return {
        "quality_cases": QUALITY_CASES,
        "top1_hits": sum(item["top1_hits"] for item in categories.values()),
        "top10_hits": sum(item["top10_hits"] for item in categories.values()),
        "categories": categories,
        "protected": {
            "cases": formal_gate.PROTECTED_CASES,
            "top1_hits": protected["top1"],
            "top10_hits": protected["top10"],
        },
    }


def _objective(
    h0_report: dict[str, Any], b0_report: dict[str, Any], raw_hashes: dict[str, str], policy: formal_gate.ParsedPolicy
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    hazkey = _backend_metrics(h0_report)
    b0 = _backend_metrics(b0_report)
    checks: list[dict[str, Any]] = []
    top1_delta = b0["top1_hits"] - hazkey["top1_hits"]
    top10_delta = b0["top10_hits"] - hazkey["top10_hits"]
    checks.extend([
        {"id": "quality-top1-delta", "actual_delta_hits": top1_delta, "minimum_delta_hits": policy.gate.minimum_top1_delta_hits, "passed": top1_delta >= policy.gate.minimum_top1_delta_hits},
        {"id": "quality-top10-delta", "actual_delta_hits": top10_delta, "minimum_delta_hits": policy.gate.minimum_top10_delta_hits, "passed": top10_delta >= policy.gate.minimum_top10_delta_hits},
    ])
    category_deltas: dict[str, int] = {}
    for category, minimum in policy.gate.minimum_category_delta_hits.items():
        delta = b0["categories"][category]["top1_hits"] - hazkey["categories"][category]["top1_hits"]
        category_deltas[category] = delta
        checks.append({"id": f"category-top1-delta:{category}", "actual_delta_hits": delta, "minimum_delta_hits": minimum, "passed": delta >= minimum})
    protected_hits = b0["protected"]["top1_hits"]
    checks.append({"id": "protected-top1", "actual_hits": protected_hits, "required_hits": policy.gate.protected_required, "passed": protected_hits == policy.gate.protected_required})
    passed = all(item["passed"] for item in checks)
    base = {
        "schema": OBJECTIVE_SCHEMA,
        "corpus": {"cases": TOTAL_CASES, "quality_cases": QUALITY_CASES, "protected_cases": formal_gate.PROTECTED_CASES, "sha256": policy.corpus_sha256},
        "baseline": {"id": "Hazkey", "resource_fingerprint": policy.hazkey_dictionary_fingerprint},
        "candidate": {"id": "B0", "resource_fingerprint": policy.candidate_resource_fingerprints["B0"]},
        "raw_runs": raw_hashes,
        "backends": {"Hazkey": hazkey, "B0": b0},
        "delta_direction": "B0-minus-Hazkey",
        "deltas": {"top1_hits": top1_delta, "top10_hits": top10_delta, "category_top1_hits": category_deltas},
        "gates": checks,
        "passed": passed,
        "next_step": "continue-b0-human-performance-stability" if passed else "complete-b0-formal-evidence-before-b1",
        "not_evaluated": ["human_preference", "both_bad", "warm_latency_p95", "pss", "stability"],
    }
    return base | {"integrity": _sha256(_canonical_json(base))}, checks


def _lexical_historical_root(raw: str, suffix: tuple[str, ...], current_leaf: str, context: str) -> tuple[str, ...]:
    if not isinstance(raw, str) or not raw.startswith("/") or raw.endswith("/") or "//" in raw or "\x00" in raw:
        raise ValueError(f"{context} must be an absolute normalized lexical path")
    parts = tuple(raw.split("/")[1:])
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"{context} contains an unsafe lexical component")
    if len(parts) <= len(suffix) or parts[-len(suffix):] != suffix:
        raise ValueError(f"{context} does not have the exact snapshot suffix")
    root = parts[:-len(suffix)]
    expected_leaf = re.compile(r"^\." + re.escape(current_leaf) + r"\.tmp-[0-9a-f]{16}$")
    if not root or expected_leaf.fullmatch(root[-1]) is None:
        raise ValueError(f"{context} temporary basename is not tied to the acquisition root")
    return root


def _validate_manifest(snapshot: EvidenceSnapshot, policy: formal_gate.ParsedPolicy) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, str]]]:
    manifest_bytes = snapshot.blobs[ACQUISITION_MANIFEST]
    manifest = _json(manifest_bytes, "acquisition manifest")
    _exact_keys(manifest, {
        "schema", "producer", "python_sources", "sealed_corpus", "evaluation_runner",
        "runtime_dependencies", "hazkey_dictionary", "candidates", "environment", "host",
        "measurement", "entries", "objective_quality", "integrity",
    }, "acquisition manifest")
    if manifest["schema"] != policy.trusted_b0_acquisition_schema:
        raise ValueError("acquisition schema is not trusted by policy")
    integrity = _require_sha(manifest["integrity"], "acquisition manifest.integrity")
    base = {key: value for key, value in manifest.items() if key != "integrity"}
    if integrity != _sha256(_canonical_json(base)):
        raise ValueError("acquisition manifest integrity mismatch")
    if integrity != policy.trusted_b0_acquisition_manifest_integrity:
        raise ValueError("acquisition manifest integrity is not the accepted evidence")

    producer = _exact_keys(manifest["producer"], {"path", "snapshot_path", "size_bytes", "sha256"}, "manifest.producer")
    if producer["path"] != policy.trusted_b0_producer["path"] or producer["sha256"] != policy.trusted_b0_producer["sha256"] or producer["snapshot_path"] != SOURCE_LAYOUT["producer"][1]:
        raise ValueError("acquisition producer is not trusted by policy")
    producer_entry = snapshot.entries[producer["snapshot_path"]]
    if producer_entry.sha256 != producer["sha256"] or producer_entry.size_bytes != producer["size_bytes"]:
        raise ValueError("producer snapshot identity mismatch")

    sources = _exact_keys(manifest["python_sources"], {"schema", "files", "integrity"}, "manifest.python_sources")
    if sources["schema"] != PYTHON_SNAPSHOT_SCHEMA or not isinstance(sources["files"], list) or len(sources["files"]) != 4:
        raise ValueError("Python source snapshot contract mismatch")
    source_base = {"schema": sources["schema"], "files": sources["files"]}
    if sources["integrity"] != _sha256(_canonical_json(source_base)):
        raise ValueError("Python source snapshot integrity mismatch")
    for expected_id, item in zip(SOURCE_LAYOUT, sources["files"], strict=True):
        item = _exact_keys(item, {"id", "path", "source_path", "snapshot_path", "size_bytes", "sha256"}, f"python source {expected_id}")
        repository_path, snapshot_path = SOURCE_LAYOUT[expected_id]
        if item["id"] != expected_id or item["path"] != repository_path or item["snapshot_path"] != snapshot_path or item["sha256"] != policy.trusted_b0_python_source_sha256[expected_id]:
            raise ValueError(f"Python source {expected_id} is not policy-trusted")
        entry = snapshot.entries[snapshot_path]
        if entry.sha256 != item["sha256"] or entry.size_bytes != item["size_bytes"]:
            raise ValueError(f"Python source snapshot changed: {expected_id}")
    if producer != {key: sources["files"][0][key] for key in ("path", "snapshot_path", "size_bytes", "sha256")}:
        raise ValueError("producer and Python source contracts disagree")

    sealed = _exact_keys(
        manifest["sealed_corpus"],
        {
            "source_path",
            "snapshot_path",
            "generation",
            "policy",
            "manifest",
            "corpus",
        },
        "manifest.sealed_corpus",
    )
    if sealed.get("generation") != formal_gate.GENERATION or sealed.get("snapshot_path") != "inputs/sealed":
        raise ValueError("sealed corpus generation mismatch")
    sealed_contract = {
        "policy": ("inputs/sealed/corpus-policy.json", policy.source_policy_sha256),
        "manifest": ("inputs/sealed/manifest.json", policy.manifest_sha256),
        "corpus": ("inputs/sealed/formal-corpus.tsv", policy.corpus_sha256),
    }
    for key, (path, digest) in sealed_contract.items():
        item_fields = {"path", "sha256"}
        if key == "corpus":
            item_fields |= {"size_bytes", "cases"}
        item = _exact_keys(sealed[key], item_fields, f"manifest.sealed_corpus.{key}")
        if item.get("sha256") != digest or snapshot.entries[path].sha256 != digest:
            raise ValueError(f"sealed {key} identity mismatch")
    if (
        sealed["policy"]["path"] != "corpus-policy.json"
        or sealed["manifest"]["path"] != "manifest.json"
        or sealed["corpus"]["path"] != "formal-corpus.tsv"
        or sealed["corpus"]["cases"] != TOTAL_CASES
        or sealed["corpus"]["size_bytes"]
        != snapshot.entries["inputs/sealed/formal-corpus.tsv"].size_bytes
    ):
        raise ValueError("sealed corpus manifest layout mismatch")
    rows = _load_corpus(snapshot.blobs["inputs/sealed/formal-corpus.tsv"])

    sealed_policy = _json(snapshot.blobs["inputs/sealed/corpus-policy.json"], "sealed source policy")
    freezes = sealed_policy.get("artifact_freezes")
    if not isinstance(freezes, dict):
        raise ValueError("sealed policy lacks artifact freezes")
    runner_freeze = freezes.get("evaluation_runner")
    if runner_freeze != {"product_source_revision": PRODUCT_SOURCE_REF, "size_bytes": RUNNER_SIZE, "sha256": RUNNER_SHA256, "runtime_dependencies_integrity": RUNTIME_INTEGRITY}:
        raise ValueError("sealed runner freeze mismatch")
    runner = manifest["evaluation_runner"]
    if runner.get("snapshot_path") != "runtime/hazkey-server" or runner.get("product_source_ref") != PRODUCT_SOURCE_REF or runner.get("size_bytes") != RUNNER_SIZE or runner.get("sha256") != RUNNER_SHA256:
        raise ValueError("evaluation runner manifest mismatch")
    runner_entry = snapshot.entries["runtime/hazkey-server"]
    if runner_entry.sha256 != RUNNER_SHA256 or runner_entry.size_bytes != RUNNER_SIZE:
        raise ValueError("evaluation runner snapshot mismatch")

    runtime = _exact_keys(manifest["runtime_dependencies"], {"schema", "source_path", "snapshot_path", "files", "integrity"}, "runtime dependencies")
    if runtime["schema"] != RUNTIME_SNAPSHOT_SCHEMA or runtime["snapshot_path"] != "runtime/lib" or not isinstance(runtime["files"], list):
        raise ValueError("runtime dependency contract mismatch")
    expected_runtime_files = []
    for name in RUNTIME_FILES:
        entry = snapshot.entries[f"runtime/lib/{name}"]
        expected_runtime_files.append({"path": name, "size_bytes": entry.size_bytes, "sha256": entry.sha256})
    runtime_base = {"schema": RUNTIME_SNAPSHOT_SCHEMA, "files": expected_runtime_files}
    if runtime["files"] != expected_runtime_files or runtime["integrity"] != _sha256(_canonical_json(runtime_base)) or runtime["integrity"] != RUNTIME_INTEGRITY:
        raise ValueError("runtime dependency identity mismatch")

    candidates = _exact_keys(manifest["candidates"], {"B0", "B1"}, "manifest.candidates")
    _exact_keys(
        candidates["B1"],
        {
            "id",
            "status",
            "generation",
            "helper_size_bytes",
            "helper_sha256",
            "data_size_bytes",
            "data_sha256",
            "manifest_sha256",
            "resource_fingerprint",
        },
        "manifest.candidates.B1",
    )
    sealed_candidates = freezes.get("candidates")
    if not isinstance(sealed_candidates, dict):
        raise ValueError("sealed candidate freezes missing")
    for candidate_id in ("B0", "B1"):
        item = candidates[candidate_id]
        freeze = sealed_candidates[candidate_id]
        for field in ("generation", "helper_size_bytes", "helper_sha256", "data_size_bytes", "data_sha256", "manifest_sha256"):
            if item.get(field) != freeze.get(field):
                raise ValueError(f"candidate {candidate_id} freeze mismatch")
        if item.get("resource_fingerprint") != policy.candidate_resource_fingerprints[candidate_id]:
            raise ValueError(f"candidate {candidate_id} resource identity mismatch")
    if candidates["B0"].get("id") != "B0" or candidates["B0"].get("status") != "evaluated" or candidates["B1"].get("id") != "B1" or candidates["B1"].get("status") != "frozen_not_evaluated":
        raise ValueError("candidate evaluation status mismatch")
    if candidates["B0"].get("snapshot_path") != (
        f"inputs/B0/{candidates['B0']['generation']}"
    ):
        raise ValueError("candidate B0 snapshot path mismatch")

    expected_environment = {
        "policy": "private-runtime-snapshot-v2-objective", "cwd": "acquisition-root", "ambient_inheritance": False,
        "values": {"GGML_BACKEND_DIR": "./runtime/lib", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "LD_LIBRARY_PATH": "./runtime/lib", "PATH": os.defpath, "TZ": "UTC"},
    }
    if manifest["environment"] != expected_environment:
        raise ValueError("acquisition environment contract mismatch")
    measurement = manifest["measurement"]
    expected_measurement = {
        "purpose": "objective-quality-only", "execution_order": ["H0", "B0"], "warmups_per_case": 0,
        "iterations_per_case": 1, "top_k": 10, "cases": TOTAL_CASES, "quality_cases": QUALITY_CASES,
        "raw_schema": RAW_SCHEMA, "per_run_timeout_seconds": 900, "latency_and_pss_are_formal_gate_evidence": False,
    }
    if measurement != expected_measurement:
        raise ValueError("objective measurement contract mismatch")
    return manifest, sealed_policy, rows


def _validate_raw(
    snapshot: EvidenceSnapshot, manifest: dict[str, Any], rows: list[dict[str, str]], policy: formal_gate.ParsedPolicy
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    if not isinstance(manifest["entries"], list) or len(manifest["entries"]) != 2:
        raise ValueError("acquisition entries must be exactly H0 then B0")
    loaded: dict[str, dict[str, Any]] = {}
    raw_hashes: dict[str, str] = {}
    historical_roots: list[tuple[str, ...]] = []
    previous_ended: int | None = None
    b0_generation = manifest["candidates"]["B0"]["generation"]
    if policy.b1_raw_run_ids != ("H0", "B0"):
        raise ValueError("policy raw run sequence is unsupported")
    expected = (
        (
            "H0",
            "Hazkey",
            "hazkey",
            "hazkey_dictionary",
            tuple(policy.b1_raw_resource_suffixes["H0"].split("/")),
        ),
        (
            "B0",
            "B0",
            "mozc",
            "mozc_runtime_inputs",
            tuple(policy.b1_raw_resource_suffixes["B0"].split("/")),
        ),
    )
    if expected[1][4][-1] != b0_generation:
        raise ValueError("policy B0 raw resource suffix and manifest generation disagree")
    expected_ids = [row["id"] for row in rows]
    host = _exact_keys(
        manifest["host"],
        {"fingerprint", "effective_cpu_affinity"},
        "manifest.host",
    )
    _require_sha(host["fingerprint"], "manifest.host.fingerprint")
    affinity = host["effective_cpu_affinity"]
    if (
        not isinstance(affinity, list)
        or not affinity
        or any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in affinity)
        or affinity != sorted(set(affinity))
    ):
        raise ValueError("manifest host CPU affinity is invalid")
    for sequence, (entry, contract) in enumerate(zip(manifest["entries"], expected, strict=True), 1):
        run_id, backend, converter, resource_kind, resource_suffix = contract
        entry = _exact_keys(
            entry,
            {
                "id",
                "sequence",
                "backend_name",
                "converter_backend",
                "argv",
                "effective_cpu_affinity",
                "started_monotonic_ns",
                "ended_monotonic_ns",
                "exit_code",
                "host_fingerprint",
                "raw",
                "stderr",
                "quality_report",
            },
            f"manifest.entries[{sequence - 1}]",
        )
        if entry.get("id") != run_id or entry.get("sequence") != sequence or entry.get("backend_name") != backend or entry.get("converter_backend") != converter or entry.get("exit_code") != 0:
            raise ValueError(f"manifest run entry mismatch: {run_id}")
        started = entry["started_monotonic_ns"]
        ended = entry["ended_monotonic_ns"]
        if (
            isinstance(started, bool)
            or not isinstance(started, int)
            or isinstance(ended, bool)
            or not isinstance(ended, int)
            or started < 0
            or ended < started
            or entry["effective_cpu_affinity"] != affinity
            or entry["host_fingerprint"] != host["fingerprint"]
        ):
            raise ValueError(f"manifest run timing or host binding mismatch: {run_id}")
        if previous_ended is not None and started < previous_ended:
            raise ValueError("manifest H0 and B0 execution intervals overlap")
        previous_ended = ended
        expected_argv = [
            "./runtime/hazkey-server",
            "--ab-probe",
            "--corpus",
            "./inputs/sealed/formal-corpus.tsv",
            "--source-ref",
            PRODUCT_SOURCE_REF,
            "--warmups",
            "0",
            "--iterations",
            "1",
            "--top-k",
            "10",
            "--backend-name",
            backend,
            "--converter-backend",
            converter,
            "--dictionary" if run_id == "H0" else "--mozc-bundle",
            (
                "./inputs/Dictionary"
                if run_id == "H0"
                else f"./inputs/B0/{b0_generation}"
            ),
        ]
        if entry["argv"] != expected_argv:
            raise ValueError(f"manifest run argv mismatch: {run_id}")
        for kind, expected_path in (("raw", f"{run_id}.jsonl"), ("stderr", f"{run_id}.stderr"), ("quality_report", f"{run_id}.quality.json")):
            reference = entry.get(kind)
            if reference != {"path": expected_path, "sha256": snapshot.entries[expected_path].sha256}:
                raise ValueError(f"manifest {run_id} {kind} binding mismatch")
        raw = snapshot.blobs[f"{run_id}.jsonl"]
        raw_hashes[run_id] = _sha256(raw)
        if raw_hashes[run_id] != policy.trusted_b0_raw_run_sha256[run_id]:
            raise ValueError(f"raw {run_id} SHA-256 is not the accepted evidence")
        run = summarize_ab_probe.load_run_bytes(raw, f"{run_id}.jsonl")
        loaded[run_id] = run
        expectations = {
            "schema": RAW_SCHEMA, "backend": backend, "converter_backend": converter,
            "source_ref": PRODUCT_SOURCE_REF, "top_k": TOP_K, "warmups": WARMUPS,
            "iterations": ITERATIONS, "corpus": {"sha256": policy.corpus_sha256, "cases": TOTAL_CASES},
        }
        for field, value in expectations.items():
            if run[field] != value:
                raise ValueError(f"raw {run_id} {field} mismatch")
        resource = run["resource"]
        if resource["kind"] != resource_kind or resource["fingerprint"] != (policy.hazkey_dictionary_fingerprint if run_id == "H0" else policy.candidate_resource_fingerprints["B0"]):
            raise ValueError(f"raw {run_id} resource identity mismatch")
        historical_roots.append(_lexical_historical_root(resource["path"], resource_suffix, snapshot.root_leaf, f"raw {run_id} resource.path"))
        if list(run["cases"]) != expected_ids:
            raise ValueError(f"raw {run_id} case IDs or order differ from sealed corpus")
        for row in rows:
            case = run["cases"][row["id"]]
            if case["reading"] != row["reading"] or case["category"] != row["category"]:
                raise ValueError(f"raw {run_id} case {row['id']} differs from sealed corpus")
    if historical_roots[0] != historical_roots[1]:
        raise ValueError("raw resource paths do not share one historical acquisition root")
    return loaded["H0"], loaded["B0"], raw_hashes


def _evaluate(policy_path: Path, acquisition_root: Path) -> dict[str, Any]:
    policy = formal_gate.load_policy(policy_path)
    if policy.formal_evidence_status != "not_ready" or policy.formal_adoption_allowed:
        raise ValueError("early-rejection authorizer requires adoption to remain fail-closed")
    if policy.b1_authorization_schema != AUTHORIZATION_SCHEMA or policy.b1_authorization_scope != AUTHORIZATION_SCOPE:
        raise ValueError("policy does not bind this authorization schema and scope")
    snapshot = _capture_evidence(acquisition_root)
    if snapshot.tree_digest != policy.trusted_b0_acquisition_tree_digest:
        raise ValueError("acquisition tree is not the policy-accepted evidence")
    manifest_sha256 = _sha256(snapshot.blobs[ACQUISITION_MANIFEST])
    if manifest_sha256 != policy.trusted_b0_acquisition_manifest_sha256:
        raise ValueError("acquisition manifest SHA-256 is not the accepted evidence")
    manifest = _json(snapshot.blobs[ACQUISITION_MANIFEST], "acquisition manifest")
    _validate_tree(snapshot, manifest, policy)
    manifest, _sealed_policy, rows = _validate_manifest(snapshot, policy)
    h0, b0, raw_hashes = _validate_raw(snapshot, manifest, rows, policy)
    reports = {"H0": _quality_report(rows, h0), "B0": _quality_report(rows, b0)}
    for run_id in ("H0", "B0"):
        stored = _json(snapshot.blobs[f"{run_id}.quality.json"], f"stored {run_id} quality")
        if stored != reports[run_id]:
            raise ValueError(f"stored {run_id} quality report differs from raw recomputation")
    objective, checks = _objective(reports["H0"], reports["B0"], raw_hashes, policy)
    stored_objective = _json(snapshot.blobs[OBJECTIVE_REPORT], "stored objective report")
    if stored_objective != objective:
        raise ValueError("stored objective report differs from raw recomputation")
    objective_ref = manifest["objective_quality"]
    if objective_ref != {
        "path": OBJECTIVE_REPORT, "sha256": snapshot.entries[OBJECTIVE_REPORT].sha256,
        "passed": objective["passed"], "next_step": objective["next_step"],
    }:
        raise ValueError("manifest objective binding mismatch")
    check_ids = tuple(item["id"] for item in checks)
    if check_ids != policy.b1_mandatory_objective_check_ids:
        raise ValueError("recomputed objective checks do not match the policy rule")
    if policy.b1_authorization_decision != "any-mandatory-objective-check-false":
        raise ValueError("unsupported policy early-rejection decision")
    failed_ids = [item["id"] for item in checks if item["passed"] is False]
    authorized = bool(failed_ids)
    manifest_bytes = snapshot.blobs[ACQUISITION_MANIFEST]
    base = {
        "schema": AUTHORIZATION_SCHEMA,
        "scope": AUTHORIZATION_SCOPE,
        "authorized": authorized,
        "formal_evidence_status": "not_ready",
        "formal_adoption_allowed": False,
        "policy": {"id": policy.policy_id, "sha256": policy.policy_sha256},
        "corpus": {
            "generation": formal_gate.GENERATION, "manifest_sha256": policy.manifest_sha256,
            "sha256": policy.corpus_sha256, "total_cases": TOTAL_CASES, "quality_cases": QUALITY_CASES,
        },
        "acquisition": {
            "root_leaf": snapshot.root_leaf,
            "schema": manifest["schema"],
            "manifest_sha256": _sha256(manifest_bytes),
            "manifest_integrity": manifest["integrity"],
            "tree_digest": snapshot.tree_digest,
            "raw_runs": raw_hashes,
        },
        "resources": {
            "evaluation_runner_sha256": RUNNER_SHA256,
            "runtime_dependencies_integrity": RUNTIME_INTEGRITY,
            "hazkey_dictionary_fingerprint": policy.hazkey_dictionary_fingerprint,
            "B0_resource_fingerprint": policy.candidate_resource_fingerprints["B0"],
            "B1_resource_fingerprint": policy.candidate_resource_fingerprints["B1"],
        },
        "recomputed": {
            "objective_integrity": objective["integrity"],
            "checks": checks,
            "failed_check_ids": failed_ids,
        },
    }
    return base | {"integrity": _sha256(_canonical_json(base))}


def evaluate_early_rejection(policy_path: Path, acquisition_root: Path) -> dict[str, Any]:
    """Return canonical B1-evaluation authorization state from raw B0 evidence."""

    return _evaluate(Path(policy_path), Path(acquisition_root))


def encode_authorization(value: dict[str, Any]) -> bytes:
    return _canonical_json(value) + b"\n"


def verify_b1_authorization(policy_path: Path, acquisition_root: Path, authorization_bytes: bytes) -> str:
    """Re-run evidence validation and exact-compare an authorization document."""

    supplied = _json(authorization_bytes, "B1 authorization")
    if authorization_bytes != encode_authorization(supplied):
        raise ValueError("B1 authorization must use canonical JSON encoding")
    expected = _evaluate(Path(policy_path), Path(acquisition_root))
    if supplied != expected:
        raise ValueError("B1 authorization does not match recomputed raw evidence")
    if not supplied["authorized"]:
        raise ValueError("B1 evaluation is not authorized by this evidence")
    return supplied["integrity"]


def _rename_noreplace_at(
    parent_fd: int, source_name: str, destination_name: str
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOSYS, "Linux renameat2 is required")
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    if renameat2(
        parent_fd,
        os.fsencode(source_name),
        parent_fd,
        os.fsencode(destination_name),
        RENAME_NOREPLACE,
    ) == 0:
        return
    number = ctypes.get_errno()
    if number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise ValueError(f"refusing to overwrite output {destination_name}")
    raise OSError(number, os.strerror(number), destination_name)


def _write_noreplace(path: Path, data: bytes) -> None:
    if not path.is_absolute():
        raise ValueError("authorization output must be an absolute path")
    parent = path.parent
    if path.name in {"", ".", ".."} or "/" in path.name or "\x00" in path.name:
        raise ValueError("authorization output has an invalid leaf name")
    parent_before = parent.lstat()
    if stat.S_ISLNK(parent_before.st_mode) or not stat.S_ISDIR(parent_before.st_mode):
        raise ValueError("authorization output parent must be a non-symlink directory")
    parent_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        parent_flags |= os.O_NOFOLLOW
    parent_fd = os.open(parent, parent_flags)
    opened_parent = os.fstat(parent_fd)
    if (opened_parent.st_dev, opened_parent.st_ino) != (
        parent_before.st_dev,
        parent_before.st_ino,
    ):
        os.close(parent_fd)
        raise ValueError("authorization output parent changed while opening")
    temporary_name = f".{path.name}.tmp-{secrets.token_hex(8)}"
    descriptor = -1
    temporary_identity: tuple[int, int] | None = None
    committed = False
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary_name, flags, 0o600, dir_fd=parent_fd)
        created = os.fstat(descriptor)
        temporary_identity = (created.st_dev, created.st_ino)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fchmod(handle.fileno(), 0o444)
            os.fsync(handle.fileno())
            metadata = os.fstat(handle.fileno())
            if (metadata.st_dev, metadata.st_ino) != temporary_identity:
                raise OSError("authorization temporary output identity changed")
        _rename_noreplace_at(parent_fd, temporary_name, path.name)
        committed = True
        os.fsync(parent_fd)
        published = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            temporary_identity is None
            or (published.st_dev, published.st_ino) != temporary_identity
            or not stat.S_ISREG(published.st_mode)
            or stat.S_IMODE(published.st_mode) != 0o444
            or published.st_nlink != 1
            or published.st_size != len(data)
        ):
            raise OSError("authorization output committed but identity assurance failed")
        read_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            read_flags |= os.O_NOFOLLOW
        published_fd = os.open(path.name, read_flags, dir_fd=parent_fd)
        try:
            opened = os.fstat(published_fd)
            if temporary_identity is None or (opened.st_dev, opened.st_ino) != temporary_identity:
                raise OSError("authorization output committed but changed before readback")
            _unused, published_sha256 = _read_open_file(
                published_fd, opened, f"published authorization {path.name}"
            )
        finally:
            os.close(published_fd)
        if published_sha256 != _sha256(data):
            raise OSError("authorization output committed but bytes changed")
        after_read = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if temporary_identity is None or (after_read.st_dev, after_read.st_ino) != temporary_identity:
            raise OSError("authorization output committed but path changed after readback")
        parent_after = parent.lstat()
        if (
            stat.S_ISLNK(parent_after.st_mode)
            or not stat.S_ISDIR(parent_after.st_mode)
            or (parent_after.st_dev, parent_after.st_ino)
            != (opened_parent.st_dev, opened_parent.st_ino)
        ):
            raise OSError("authorization output committed but parent identity changed")
    except BaseException as error:
        if committed:
            try:
                remaining = os.stat(
                    path.name, dir_fd=parent_fd, follow_symlinks=False
                )
            except FileNotFoundError:
                remaining = None
            if remaining is not None and temporary_identity is not None and (
                remaining.st_dev,
                remaining.st_ino,
            ) == temporary_identity:
                try:
                    os.unlink(path.name, dir_fd=parent_fd)
                    os.fsync(parent_fd)
                    committed = False
                except OSError as rollback_error:
                    raise OSError(
                        "authorization output committed; owned rollback failed"
                    ) from rollback_error
            elif remaining is not None:
                raise OSError(
                    "authorization output committed with uncertain identity; "
                    "refusing destructive rollback"
                ) from error
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if not committed and temporary_identity is not None:
            try:
                remaining = os.stat(
                    temporary_name, dir_fd=parent_fd, follow_symlinks=False
                )
            except FileNotFoundError:
                remaining = None
            if remaining is not None and (remaining.st_dev, remaining.st_ino) == temporary_identity:
                os.unlink(temporary_name, dir_fd=parent_fd)
        os.close(parent_fd)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--acquisition-root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        result = evaluate_early_rejection(args.policy, args.acquisition_root)
        encoded = encode_authorization(result)
        if args.output is None:
            sys.stdout.buffer.write(encoded)
        else:
            _write_noreplace(args.output, encoded)
        return 0 if result["authorized"] else 1
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
