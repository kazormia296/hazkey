#!/usr/bin/env python3
"""Evaluate paired ABProbe v5 runs against acceptable first-segment paths.

This evaluator is intentionally diagnostic-only.  It preserves every accepted
first boundary as a set instead of selecting one annotation path as canonical.
Surface metrics are reported only when every acceptable path for a case has a
reading/surface alignment.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any, Iterable
import unicodedata

try:
    from . import compile_mozc_acceptable_path_evaluation as compiler
    from . import evaluate_mozc_hybrid_spike as hybrid
    from .summarize_ab_probe import INPUT_SCHEMA_V5, load_run_bytes
except ImportError:  # Direct execution from tools/dictionary.
    import compile_mozc_acceptable_path_evaluation as compiler
    import evaluate_mozc_hybrid_spike as hybrid
    from summarize_ab_probe import INPUT_SCHEMA_V5, load_run_bytes


MANIFEST_SCHEMA = (
    "hazkey.mozc-acceptable-path-evaluation-generation-manifest.v1"
)
TARGET_SCHEMA = "hazkey.mozc-acceptable-first-segment-target.v1"
REVIEWED_PATHS_SCHEMA = "hazkey.mozc-hybrid-acceptable-paths.v3"
ANNOTATION_MANIFEST_SCHEMA = (
    "hazkey.mozc-boundary-annotation-export-manifest.v1"
)
PROBE_INPUT_SCHEMA = "hazkey.mozc-hybrid-segment-probe-input.v1"
OUTPUT_SCHEMA = (
    "hazkey.mozc-acceptable-path-first-segment-boundary-evaluation.v1"
)
COMPOSITION_ELEMENT_UNIT = "composition_element"
SURFACE_STATUSES = {"fully_aligned", "partially_aligned", "not_aligned"}
POLICY_CONFIGS = {
    "runtime_h0": {
        "id": "mozc-first-preserve-top1-h0",
        "allow_promotion": False,
        "width_guard": False,
    },
    "diagnostic_h1": {
        "id": hybrid.POLICY_ID,
        "allow_promotion": True,
        "width_guard": False,
    },
    "diagnostic_h2": {
        "id": hybrid.WIDTH_GUARDED_POLICY_ID,
        "allow_promotion": True,
        "width_guard": True,
    },
}
EXPECTED_CONTRACTS = {
    "annotation_reading_source": "source.annotation_reading",
    "composition_element_mapping": (
        "one-NFC-code-point-per-direct-composition-element.v1"
    ),
    "first_segment_target": "first-reading-boundary-or-full-reading.v1",
    "multiple_acceptable_paths": (
        "preserved-as-deduplicated-first-segment-targets.v1"
    ),
    "surface_evaluation": "fully-aligned-cases-only.v1",
}
EXPECTED_BINDING_SCHEMAS = {
    "reviewed_paths": REVIEWED_PATHS_SCHEMA,
    "annotation_manifest": ANNOTATION_MANIFEST_SCHEMA,
    "probe_input": PROBE_INPUT_SCHEMA,
    "targets": TARGET_SCHEMA,
}
GENERATION_FILE_BY_BINDING = {
    "reviewed_paths": compiler.SOURCE_REVIEWED_PATHS_NAME,
    "annotation_manifest": compiler.SOURCE_ANNOTATION_MANIFEST_NAME,
    "probe_input": compiler.PROBE_INPUT_NAME,
    "targets": compiler.TARGETS_NAME,
}
EXPECTED_GENERATION_FILES = {
    *GENERATION_FILE_BY_BINDING.values(),
    compiler.MANIFEST_NAME,
}
ROOT_MANIFEST_FIELDS = {
    "schema",
    "diagnostic_only",
    "formal_authorized",
    "bindings",
    "category_counts",
    "surface_evaluation_status_counts",
    "corrected_reading_cases",
    "total_acceptable_paths",
    "total_acceptable_first_spans",
    "total_acceptable_first_chunks",
    "contracts",
}
TARGET_FIELDS = {
    "schema",
    "id",
    "category",
    "reading",
    "acceptable_first_spans",
    "surface_evaluation_status",
    "acceptable_first_chunks",
    "path_counts",
}
ABPROBE_ROOT_FIELDS = {
    "schema",
    "conversion_path",
    "id",
    "reading",
    "category",
    "backend",
    "backend_version",
    "converter_backend",
    "source_ref",
    "resource",
    "top_k",
    "corpus",
    "candidates",
    "composition_span",
    "measurement",
}
RSS_FIELDS = {
    "before_kib",
    "after_kib",
    "before_pss_kib",
    "after_pss_kib",
    "backend_before_kib",
    "backend_after_kib",
    "backend_before_pss_kib",
    "backend_after_pss_kib",
}


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _exact_object(value: Any, fields: Iterable[str], context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be an object")
    expected = set(fields)
    actual = set(value)
    if actual != expected:
        raise ValueError(
            f"{context} fields differ; missing={sorted(expected - actual)!r}, "
            f"unexpected={sorted(actual - expected)!r}"
        )
    return value


def _string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{context} must be a non-empty string")
    return value


def _nonnegative_int(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{context} must be a non-negative integer")
    return value


def _positive_int(value: Any, context: str) -> int:
    result = _nonnegative_int(value, context)
    if result == 0:
        raise ValueError(f"{context} must be a positive integer")
    return result


def _hash(value: Any, context: str) -> str:
    result = _string(value, context)
    if re.fullmatch(r"sha256:[0-9a-f]{64}", result) is None:
        raise ValueError(f"{context} must be a canonical SHA-256 value")
    return result


def _json(data: bytes, context: str) -> dict[str, Any]:
    try:
        value = json.loads(
            data.decode("utf-8"), object_pairs_hook=_no_duplicate_keys
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"{context} is not valid UTF-8 JSON: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{context} must contain one JSON object")
    return value


def _jsonl(data: bytes, context: str) -> list[dict[str, Any]]:
    if data.startswith(b"\xef\xbb\xbf") or b"\r" in data:
        raise ValueError(f"{context} must be BOM-free UTF-8 JSONL with LF endings")
    if not data.endswith(b"\n"):
        raise ValueError(f"{context} must end with exactly an LF-delimited record")
    try:
        lines = data.decode("utf-8")[:-1].split("\n")
    except UnicodeDecodeError as error:
        raise ValueError(f"{context} is not valid UTF-8") from error
    if not lines or any(not line for line in lines):
        raise ValueError(f"{context} contains an empty JSONL record")
    result: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, 1):
        try:
            value = json.loads(line, object_pairs_hook=_no_duplicate_keys)
        except (json.JSONDecodeError, ValueError) as error:
            raise ValueError(f"{context}:{line_number}: {error}") from error
        if not isinstance(value, dict):
            raise ValueError(f"{context}:{line_number} must be an object")
        result.append(value)
    return result


def _stat_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _open_directory_no_symlinks(path: Path, context: str) -> int:
    if ".." in path.parts:
        raise ValueError(f"{context} path must not contain traversal")
    absolute = Path(os.path.abspath(path))
    descriptor = os.open(
        "/", os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        for component in absolute.parts[1:]:
            next_descriptor = os.open(
                component,
                os.O_RDONLY
                | os.O_DIRECTORY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except OSError as error:
        os.close(descriptor)
        raise ValueError(
            f"{context} and all ancestors must be non-symlink directories"
        ) from error


def _read_open_regular(directory_fd: int, name: str, context: str) -> bytes:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
    except OSError as error:
        raise ValueError(
            f"{context} must be a non-hardlinked regular non-symlink file"
        ) from error
    try:
        before = os.fstat(descriptor)
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or _stat_identity(before) != _stat_identity(current)
        ):
            raise ValueError(
                f"{context} must be a non-hardlinked regular non-symlink file"
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            _stat_identity(before) != _stat_identity(after)
            or _stat_identity(before) != _stat_identity(current)
        ):
            raise ValueError(f"{context} changed while read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _read_regular(path: Path, context: str) -> bytes:
    if not path.name:
        raise ValueError(f"{context} path must name a file")
    directory_fd = _open_directory_no_symlinks(path.parent, f"{context} parent")
    try:
        return _read_open_regular(directory_fd, path.name, context)
    finally:
        os.close(directory_fd)


def _capture_generation(manifest_path: Path) -> dict[str, bytes]:
    if manifest_path.name != compiler.MANIFEST_NAME:
        raise ValueError(
            f"generation manifest filename must be {compiler.MANIFEST_NAME!r}"
        )
    directory_fd = _open_directory_no_symlinks(
        manifest_path.parent, "generation directory"
    )
    try:
        before = os.fstat(directory_fd)
        names = set(os.listdir(directory_fd))
        if names != EXPECTED_GENERATION_FILES:
            raise ValueError(
                "generation file set differs; "
                f"missing={sorted(EXPECTED_GENERATION_FILES - names)!r}, "
                f"unexpected={sorted(names - EXPECTED_GENERATION_FILES)!r}"
            )
        blobs = {
            name: _read_open_regular(
                directory_fd, name, f"generation file {name}"
            )
            for name in sorted(names)
        }
        after = os.fstat(directory_fd)
        if _stat_identity(before) != _stat_identity(after):
            raise ValueError("generation directory changed while read")
        if set(os.listdir(directory_fd)) != names:
            raise ValueError("generation file set changed while read")
        return blobs
    finally:
        os.close(directory_fd)


def _validate_binding_file(
    name: str,
    binding: dict[str, Any],
    data: bytes,
) -> None:
    fields = {"path", "schema", "sha256", "cases"}
    if name == "annotation_manifest":
        fields.add("complete")
    _exact_object(binding, fields, f"manifest.bindings.{name}")
    path_text = _string(binding["path"], f"manifest.bindings.{name}.path")
    if path_text != GENERATION_FILE_BY_BINDING[name]:
        raise ValueError(
            f"manifest.bindings.{name}.path must be the fixed generation basename "
            f"{GENERATION_FILE_BY_BINDING[name]!r}"
        )
    if binding["schema"] != EXPECTED_BINDING_SCHEMAS[name]:
        raise ValueError(f"manifest.bindings.{name}.schema is not supported")
    expected_hash = _hash(
        binding["sha256"], f"manifest.bindings.{name}.sha256"
    )
    expected_cases = _positive_int(
        binding["cases"], f"manifest.bindings.{name}.cases"
    )
    if name == "annotation_manifest" and binding["complete"] is not True:
        raise ValueError("annotation manifest binding must be complete")
    if _sha256(data) != expected_hash:
        raise ValueError(f"bound {name} SHA-256 mismatch")
    if name == "annotation_manifest":
        payload = _json(data, f"bound {name}")
        if payload.get("schema") != binding["schema"]:
            raise ValueError(f"bound {name} schema mismatch")
        if payload.get("cases") != expected_cases or payload.get("complete") is not True:
            raise ValueError(f"bound {name} completion/count mismatch")
    else:
        records = _jsonl(data, f"bound {name}")
        if len(records) != expected_cases:
            raise ValueError(f"bound {name} case count mismatch")
        if any(record.get("schema") != binding["schema"] for record in records):
            raise ValueError(f"bound {name} schema mismatch")
    return None


def _validate_manifest(blobs: dict[str, bytes]) -> tuple[dict[str, Any], dict[str, bytes]]:
    data = blobs[compiler.MANIFEST_NAME]
    manifest = _exact_object(
        _json(data, compiler.MANIFEST_NAME), ROOT_MANIFEST_FIELDS, "manifest"
    )
    if manifest["schema"] != MANIFEST_SCHEMA:
        raise ValueError(f"manifest.schema must be {MANIFEST_SCHEMA!r}")
    if manifest["diagnostic_only"] is not True or manifest["formal_authorized"] is not False:
        raise ValueError("acceptable-path generation must remain diagnostic-only")
    if manifest["contracts"] != EXPECTED_CONTRACTS:
        raise ValueError("manifest contracts do not match this evaluator")
    bindings = _exact_object(
        manifest["bindings"], EXPECTED_BINDING_SCHEMAS, "manifest.bindings"
    )
    bound = {
        name: blobs[GENERATION_FILE_BY_BINDING[name]]
        for name in EXPECTED_BINDING_SCHEMAS
    }
    for name in EXPECTED_BINDING_SCHEMAS:
        _validate_binding_file(name, bindings[name], bound[name])
    binding_case_counts = {binding["cases"] for binding in bindings.values()}
    if len(binding_case_counts) != 1:
        raise ValueError("manifest bindings do not describe one common case set")
    for field in (
        "corrected_reading_cases",
        "total_acceptable_paths",
        "total_acceptable_first_spans",
        "total_acceptable_first_chunks",
    ):
        _nonnegative_int(manifest[field], f"manifest.{field}")
    case_count = bindings["targets"]["cases"]
    if manifest["corrected_reading_cases"] > case_count:
        raise ValueError("manifest.corrected_reading_cases exceeds case count")
    for field in ("category_counts", "surface_evaluation_status_counts"):
        counts = manifest[field]
        if not isinstance(counts, dict) or any(
            not isinstance(key, str) or not key or _nonnegative_int(value, f"manifest.{field}.{key}") < 0
            for key, value in counts.items()
        ):
            raise ValueError(f"manifest.{field} must be a string-to-count object")

    rederived = compiler.prepare_outputs_bytes(
        reviewed_paths_data=bound["reviewed_paths"],
        annotation_manifest_data=bound["annotation_manifest"],
    )
    if set(rederived) != EXPECTED_GENERATION_FILES:
        raise AssertionError("compiler generation file contract changed")
    derivation_order = (
        compiler.SOURCE_REVIEWED_PATHS_NAME,
        compiler.SOURCE_ANNOTATION_MANIFEST_NAME,
        compiler.PROBE_INPUT_NAME,
        compiler.TARGETS_NAME,
        compiler.MANIFEST_NAME,
    )
    for name in derivation_order:
        if blobs[name] != rederived[name]:
            raise ValueError(
                f"generation file {name} is not exactly derived from reviewed paths"
            )
    return manifest, bound


def _validate_span(value: Any, context: str, reading_count: int) -> dict[str, Any]:
    span = _exact_object(value, {"start", "count", "unit"}, context)
    if span["start"] != 0:
        raise ValueError(f"{context}.start must be 0")
    count = _positive_int(span["count"], f"{context}.count")
    if count > reading_count:
        raise ValueError(f"{context}.count exceeds the reading element count")
    if span["unit"] != COMPOSITION_ELEMENT_UNIT:
        raise ValueError(f"{context}.unit must be {COMPOSITION_ELEMENT_UNIT!r}")
    return {"start": 0, "count": count, "unit": COMPOSITION_ELEMENT_UNIT}


def _validate_targets(data: bytes, context: str) -> list[dict[str, Any]]:
    records = _jsonl(data, context)
    seen_ids: set[str] = set()
    targets: list[dict[str, Any]] = []
    for index, raw in enumerate(records, 1):
        where = f"{context}:{index}"
        target = _exact_object(raw, TARGET_FIELDS, where)
        if target["schema"] != TARGET_SCHEMA:
            raise ValueError(f"{where}.schema must be {TARGET_SCHEMA!r}")
        case_id = _string(target["id"], f"{where}.id")
        if case_id in seen_ids:
            raise ValueError(f"{where}.id duplicates {case_id!r}")
        seen_ids.add(case_id)
        category = _string(target["category"], f"{where}.category")
        reading = _string(target["reading"], f"{where}.reading")
        if unicodedata.normalize("NFC", reading) != reading:
            raise ValueError(f"{where}.reading must be NFC")
        reading_count = len(reading)
        raw_spans = target["acceptable_first_spans"]
        if not isinstance(raw_spans, list) or not raw_spans:
            raise ValueError(f"{where}.acceptable_first_spans must be non-empty")
        spans = [
            _validate_span(span, f"{where}.acceptable_first_spans[{i}]", reading_count)
            for i, span in enumerate(raw_spans)
        ]
        span_counts = [span["count"] for span in spans]
        if span_counts != sorted(set(span_counts)):
            raise ValueError(f"{where}.acceptable_first_spans must be sorted and unique")
        status = target["surface_evaluation_status"]
        if status not in SURFACE_STATUSES:
            raise ValueError(f"{where}.surface_evaluation_status is invalid")
        path_counts = _exact_object(
            target["path_counts"], {"acceptable", "aligned", "reading_only"}, f"{where}.path_counts"
        )
        acceptable = _positive_int(path_counts["acceptable"], f"{where}.path_counts.acceptable")
        aligned = _nonnegative_int(path_counts["aligned"], f"{where}.path_counts.aligned")
        reading_only = _nonnegative_int(path_counts["reading_only"], f"{where}.path_counts.reading_only")
        if aligned + reading_only != acceptable:
            raise ValueError(f"{where}.path_counts do not partition acceptable paths")
        chunks_raw = target["acceptable_first_chunks"]
        if not isinstance(chunks_raw, list):
            raise ValueError(f"{where}.acceptable_first_chunks must be an array")
        chunks: list[dict[str, Any]] = []
        chunk_keys: list[tuple[int, str]] = []
        for chunk_index, raw_chunk in enumerate(chunks_raw):
            chunk_where = f"{where}.acceptable_first_chunks[{chunk_index}]"
            chunk = _exact_object(raw_chunk, {"span", "surface"}, chunk_where)
            span = _validate_span(chunk["span"], f"{chunk_where}.span", reading_count)
            if span["count"] not in span_counts:
                raise ValueError(f"{chunk_where}.span is not an acceptable first span")
            surface = _string(chunk["surface"], f"{chunk_where}.surface")
            if unicodedata.normalize("NFC", surface) != surface:
                raise ValueError(f"{chunk_where}.surface must be NFC")
            chunks.append({"span": span, "surface": surface})
            chunk_keys.append((span["count"], surface))
        if chunk_keys != sorted(set(chunk_keys)):
            raise ValueError(f"{where}.acceptable_first_chunks must be sorted and unique")
        expected_status = (
            "fully_aligned" if reading_only == 0 else
            "not_aligned" if aligned == 0 else
            "partially_aligned"
        )
        if status != expected_status:
            raise ValueError(f"{where}.surface_evaluation_status conflicts with path_counts")
        if (aligned == 0) != (len(chunks) == 0):
            raise ValueError(f"{where}.acceptable_first_chunks conflict with aligned path count")
        if len(spans) > acceptable or len(chunks) > aligned:
            raise ValueError(f"{where} deduplicated targets exceed source path counts")
        if status == "fully_aligned" and {
            count for count, _surface in chunk_keys
        } != set(span_counts):
            raise ValueError(
                f"{where}.acceptable_first_chunks do not cover every fully aligned span"
            )
        targets.append({
            "schema": TARGET_SCHEMA,
            "id": case_id,
            "category": category,
            "reading": reading,
            "acceptable_first_spans": spans,
            "surface_evaluation_status": status,
            "acceptable_first_chunks": chunks,
            "path_counts": {
                "acceptable": acceptable,
                "aligned": aligned,
                "reading_only": reading_only,
            },
        })
    return targets


def _validate_manifest_aggregates(manifest: dict[str, Any], targets: list[dict[str, Any]]) -> None:
    category_counts = Counter(target["category"] for target in targets)
    status_counts = Counter(target["surface_evaluation_status"] for target in targets)
    if manifest["category_counts"] != dict(sorted(category_counts.items())):
        raise ValueError("manifest.category_counts do not match targets")
    if manifest["surface_evaluation_status_counts"] != dict(sorted(status_counts.items())):
        raise ValueError("manifest.surface_evaluation_status_counts do not match targets")
    expected_totals = {
        "total_acceptable_paths": sum(t["path_counts"]["acceptable"] for t in targets),
        "total_acceptable_first_spans": sum(len(t["acceptable_first_spans"]) for t in targets),
        "total_acceptable_first_chunks": sum(len(t["acceptable_first_chunks"]) for t in targets),
    }
    for field, expected in expected_totals.items():
        if manifest[field] != expected:
            raise ValueError(f"manifest.{field} does not match targets")


def _validate_probe_binding(data: bytes, targets: list[dict[str, Any]]) -> None:
    records = _jsonl(data, "bound probe_input")
    if len(records) != len(targets):
        raise ValueError("bound probe input does not cover all targets")
    for index, (record, target) in enumerate(zip(records, targets, strict=True), 1):
        where = f"bound probe_input:{index}"
        probe = _exact_object(record, {"schema", "id", "category", "elements"}, where)
        if probe["schema"] != PROBE_INPUT_SCHEMA:
            raise ValueError(f"{where}.schema mismatch")
        if probe["id"] != target["id"] or probe["category"] != target["category"]:
            raise ValueError(f"{where} identity/category mismatch")
        elements = probe["elements"]
        if not isinstance(elements, list) or len(elements) != len(target["reading"]):
            raise ValueError(f"{where}.elements do not map one-to-one to reading")
        texts: list[str] = []
        for element_index, raw_element in enumerate(elements):
            element = _exact_object(raw_element, {"text", "input_style"}, f"{where}.elements[{element_index}]")
            text = _string(element["text"], f"{where}.elements[{element_index}].text")
            if len(text) != 1 or element["input_style"] != "direct":
                raise ValueError(f"{where}.elements[{element_index}] violates direct code-point mapping")
            texts.append(text)
        if "".join(texts) != target["reading"]:
            raise ValueError(f"{where}.elements do not reconstruct target reading")


def _validate_abprobe_contract(data: bytes, context: str) -> None:
    for line_number, record in enumerate(_jsonl(data, context), 1):
        where = f"{context}:{line_number}"
        root = _exact_object(record, ABPROBE_ROOT_FIELDS, where)
        if root["schema"] != INPUT_SCHEMA_V5:
            raise ValueError(f"{where}.schema must be ABProbe v5")
        if root["conversion_path"] != "segment_candidates":
            raise ValueError(f"{where}.conversion_path must be 'segment_candidates'")
        _exact_object(root["resource"], {"kind", "path", "fingerprint"}, f"{where}.resource")
        _exact_object(root["corpus"], {"sha256", "cases"}, f"{where}.corpus")
        _exact_object(root["composition_span"], {"start", "count", "unit"}, f"{where}.composition_span")
        if not isinstance(root["candidates"], list):
            raise ValueError(f"{where}.candidates must be an array")
        for index, candidate in enumerate(root["candidates"]):
            _exact_object(candidate, {"text", "rank", "consuming_count"}, f"{where}.candidates[{index}]")
        measurement = _exact_object(
            root["measurement"], {"warmups", "iterations", "latency_ms", "rss", "backend_diagnostics"}, f"{where}.measurement"
        )
        _exact_object(measurement["latency_ms"], {"median", "p95", "minimum", "maximum", "samples"}, f"{where}.measurement.latency_ms")
        rss = measurement["rss"]
        if not isinstance(rss, dict) or not {"before_kib", "after_kib"}.issubset(rss) or not set(rss).issubset(RSS_FIELDS):
            raise ValueError(f"{where}.measurement.rss fields differ")
        diagnostics = measurement["backend_diagnostics"]
        if not isinstance(diagnostics, dict) or not set(diagnostics).issubset({"process_launch_count", "cleanup_failure_count"}):
            raise ValueError(f"{where}.measurement.backend_diagnostics fields differ")


def _load_run(data: bytes, path: Path, converter: str) -> dict[str, Any]:
    _validate_abprobe_contract(data, str(path))
    run = load_run_bytes(data, path)
    if run["schema"] != INPUT_SCHEMA_V5 or run["conversion_path"] != "segment_candidates":
        raise ValueError(
            f"{converter} run must use ABProbe v5 segment_candidates"
        )
    # ``backend`` is a human-facing experiment label (for example ``B0``),
    # whereas ``converter_backend`` is the machine identity that selects the
    # contract and resource kind.  load_run_bytes already requires the display
    # label to be non-empty and consistent within the run.
    if run["converter_backend"] != converter:
        raise ValueError(f"{converter} run converter_backend identity mismatch")
    return run


def _validate_runs(
    targets: list[dict[str, Any]],
    manifest: dict[str, Any],
    hazkey_run: dict[str, Any],
    mozc_run: dict[str, Any],
) -> None:
    expected_ids = [target["id"] for target in targets]
    for name, run in (("Hazkey", hazkey_run), ("Mozc", mozc_run)):
        if list(run["cases"]) != expected_ids:
            raise ValueError(f"{name} result IDs/order do not match targets")
        binding = manifest["bindings"]["probe_input"]
        if run["corpus"] != {"sha256": binding["sha256"], "cases": binding["cases"]}:
            raise ValueError(f"{name} corpus identity does not match bound probe input")
    for field in ("source_ref", "top_k", "warmups", "iterations", "corpus"):
        if hazkey_run[field] != mozc_run[field]:
            raise ValueError(f"paired run metadata {field} differs")
    for target in targets:
        case_id = target["id"]
        hazkey = hazkey_run["cases"][case_id]
        mozc = mozc_run["cases"][case_id]
        expected_span = {"start": 0, "count": len(target["reading"]), "unit": COMPOSITION_ELEMENT_UNIT}
        for name, case in (("Hazkey", hazkey), ("Mozc", mozc)):
            if case["reading"] != target["reading"] or case["category"] != target["category"]:
                raise ValueError(f"{name} case {case_id!r} reading/category mismatch")
            if case["composition_span"] != expected_span:
                raise ValueError(f"{name} case {case_id!r} composition_span mismatch")
        if hazkey["composition_span"] != mozc["composition_span"]:
            raise ValueError(f"paired case {case_id!r} composition_span differs")


def _error_position(counts: list[int], accepted: set[int]) -> str:
    if not counts:
        return "missing"
    if any(count in accepted for count in counts):
        return "hit"
    if max(counts) < min(accepted):
        return "before_all"
    if min(counts) > max(accepted):
        return "after_all"
    if any(count < min(accepted) for count in counts) and any(
        count > max(accepted) for count in counts
    ):
        return "mixed_sides"
    if len(accepted) > 1 and any(
        min(accepted) < count < max(accepted) for count in counts
    ):
        return "between_alternatives"
    return "mixed_sides"


def _prediction(candidates: list[dict[str, Any]], target: dict[str, Any]) -> dict[str, Any]:
    accepted = {span["count"] for span in target["acceptable_first_spans"]}
    pairs = {(chunk["span"]["count"], chunk["surface"]) for chunk in target["acceptable_first_chunks"]}
    counts = [candidate["consuming_count"] for candidate in candidates]
    top1 = candidates[:1]
    fully_aligned = target["surface_evaluation_status"] == "fully_aligned"

    def result(values: list[dict[str, Any]]) -> dict[str, Any]:
        value_counts = [value["consuming_count"] for value in values]
        boundary_hit = any(count in accepted for count in value_counts)
        distance = (
            min(abs(count - accepted_count) for count in value_counts for accepted_count in accepted)
            if value_counts else None
        )
        return {
            "first_segment_boundary_hit": boundary_hit,
            "first_segment_boundary_error_position": _error_position(
                value_counts, accepted
            ),
            "minimum_absolute_first_segment_boundary_element_distance": distance,
            "surface_comparable_given_acceptable_first_segment_boundary": (
                fully_aligned and boundary_hit
            ),
            "surface_hit_given_acceptable_first_segment_boundary": (
                any((value["consuming_count"], value["text"]) in pairs for value in values)
                if fully_aligned and boundary_hit else None
            ),
            "end_to_end_hit": (
                any((value["consuming_count"], value["text"]) in pairs for value in values)
                if fully_aligned else None
            ),
        }

    return {
        "top1_candidate": top1[0] if top1 else None,
        "top1": result(top1),
        "top_k": result(candidates),
    }


def _structured_policy_predictions(
    hazkey: list[dict[str, Any]],
    mozc: list[dict[str, Any]],
    target: dict[str, Any],
    suggestion_limit: int,
) -> dict[str, Any]:
    policies: dict[str, Any] = {}
    for policy, config in POLICY_CONFIGS.items():
        candidates, decision = hybrid._merge_boundary_aware_candidate_records(
            hazkey,
            mozc,
            suggestion_limit,
            allow_promotion=config["allow_promotion"],
            width_guard=config["width_guard"],
        )
        policies[policy] = {
            "decision": decision,
            "candidates": candidates,
            **_prediction(candidates, target),
        }
    h0_candidate = policies["runtime_h0"]["top1_candidate"]
    if mozc and (
        h0_candidate is None
        or h0_candidate["consuming_count"] != mozc[0]["consuming_count"]
    ):
        raise AssertionError("runtime H0 changed Mozc first-segment boundary")
    h0_count = None if h0_candidate is None else h0_candidate["consuming_count"]
    for policy in ("diagnostic_h1", "diagnostic_h2"):
        candidate = policies[policy]["top1_candidate"]
        count = None if candidate is None else candidate["consuming_count"]
        if count != h0_count:
            raise AssertionError(
                f"{policy} changed runtime H0 first-segment boundary"
            )
    return policies


def _ratio(hits: int, cases: int) -> float | None:
    return hits / cases if cases else None


def _metric(
    cases: list[dict[str, Any]], group: str, system: str, rank: str
) -> dict[str, Any]:
    predictions = [case[group][system][rank] for case in cases]
    hits = sum(
        prediction["first_segment_boundary_hit"] for prediction in predictions
    )
    positions = Counter(
        prediction["first_segment_boundary_error_position"]
        for prediction in predictions
    )
    distances = [
        prediction[
            "minimum_absolute_first_segment_boundary_element_distance"
        ]
        for prediction in predictions
        if prediction[
            "minimum_absolute_first_segment_boundary_element_distance"
        ]
        is not None
    ]
    return {
        "first_segment_boundary_hits": hits,
        "cases": len(predictions),
        "first_segment_boundary_accuracy": _ratio(hits, len(predictions)),
        "first_segment_boundary_error_positions": {
            key: positions.get(key, 0)
            for key in (
                "hit",
                "missing",
                "before_all",
                "after_all",
                "between_alternatives",
                "mixed_sides",
            )
        },
        "minimum_absolute_first_segment_boundary_element_distance": {
            "comparable_cases": len(distances),
            "missing_cases": len(predictions) - len(distances),
            "sum": sum(distances),
            "mean": (sum(distances) / len(distances) if distances else None),
            "maximum": max(distances) if distances else None,
        },
    }


def _surface_metrics(
    cases: list[dict[str, Any]], group: str, system: str
) -> dict[str, Any]:
    fully = [case for case in cases if case["surface_evaluation_status"] == "fully_aligned"]
    output: dict[str, Any] = {}
    for rank in ("top1", "top_k"):
        predictions = [case[group][system][rank] for case in fully]
        conditional = [
            prediction
            for prediction in predictions
            if prediction[
                "surface_comparable_given_acceptable_first_segment_boundary"
            ]
        ]
        conditional_hits = sum(
            prediction[
                "surface_hit_given_acceptable_first_segment_boundary"
            ]
            is True
            for prediction in conditional
        )
        e2e_hits = sum(prediction["end_to_end_hit"] is True for prediction in predictions)
        output[rank] = {
            "conditional_surface_given_acceptable_first_segment_boundary": {
                "hits": conditional_hits,
                "cases": len(conditional),
                "accuracy": _ratio(conditional_hits, len(conditional)),
            },
            "end_to_end": {
                "hits": e2e_hits,
                "cases": len(predictions),
                "accuracy": _ratio(e2e_hits, len(predictions)),
            },
        }
    return output


def _delta(pairs: Iterable[tuple[bool, bool]]) -> dict[str, int]:
    values = list(pairs)
    rescued = sum(not baseline and candidate for baseline, candidate in values)
    regressed = sum(baseline and not candidate for baseline, candidate in values)
    return {
        "comparable_cases": len(values),
        "rescued": rescued,
        "regressed": regressed,
        "net": rescued - regressed,
    }


def _policy_deltas(
    cases: list[dict[str, Any]], policy: str
) -> dict[str, Any]:
    baseline = "runtime_h0"
    boundary: dict[str, Any] = {}
    surface: dict[str, Any] = {}
    for rank in ("top1", "top_k"):
        boundary[rank] = _delta(
            (
                bool(
                    case["policies"][baseline][rank][
                        "first_segment_boundary_hit"
                    ]
                ),
                bool(
                    case["policies"][policy][rank][
                        "first_segment_boundary_hit"
                    ]
                ),
            )
            for case in cases
        )
        fully = [
            case
            for case in cases
            if case["surface_evaluation_status"] == "fully_aligned"
        ]
        for case in fully:
            baseline_comparable = case["policies"][baseline][rank][
                "surface_comparable_given_acceptable_first_segment_boundary"
            ]
            policy_comparable = case["policies"][policy][rank][
                "surface_comparable_given_acceptable_first_segment_boundary"
            ]
            if baseline_comparable != policy_comparable:
                raise AssertionError(
                    "H0/H1/H2 changed first-segment boundary comparability"
                )
        conditional = [
            case
            for case in fully
            if case["policies"][baseline][rank][
                "surface_comparable_given_acceptable_first_segment_boundary"
            ]
        ]
        surface[rank] = {
            "conditional_surface_given_acceptable_first_segment_boundary": _delta(
                (
                    bool(
                        case["policies"][baseline][rank][
                            "surface_hit_given_acceptable_first_segment_boundary"
                        ]
                    ),
                    bool(
                        case["policies"][policy][rank][
                            "surface_hit_given_acceptable_first_segment_boundary"
                        ]
                    ),
                )
                for case in conditional
            ),
            "end_to_end": _delta(
                (
                    bool(case["policies"][baseline][rank]["end_to_end_hit"]),
                    bool(case["policies"][policy][rank]["end_to_end_hit"]),
                )
                for case in fully
            ),
        }
    return {
        "first_segment_boundary": boundary,
        "surface": surface,
    }


def _policy_metrics(cases: list[dict[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for policy in POLICY_CONFIGS:
        decisions = Counter(case["policies"][policy]["decision"] for case in cases)
        output[policy] = {
            "policy": dict(POLICY_CONFIGS[policy]),
            "decision_counts": dict(sorted(decisions.items())),
            "first_segment_boundary": {
                rank: _metric(cases, "policies", policy, rank)
                for rank in ("top1", "top_k")
            },
            "surface": _surface_metrics(cases, "policies", policy),
            "deltas_vs_runtime_h0": _policy_deltas(cases, policy),
        }
    return output


def _slice(cases: list[dict[str, Any]]) -> dict[str, Any]:
    groups = Counter()
    for case in cases:
        hazkey = case["backends"]["hazkey"]["top1"][
            "first_segment_boundary_hit"
        ]
        mozc = case["backends"]["mozc"]["top1"][
            "first_segment_boundary_hit"
        ]
        groups["both" if hazkey and mozc else "hazkey_only" if hazkey else "mozc_only" if mozc else "neither"] += 1
    coverage = Counter(case["surface_evaluation_status"] for case in cases)
    return {
        "cases": len(cases),
        "first_segment_boundary": {
            backend: {
                rank: _metric(cases, "backends", backend, rank)
                for rank in ("top1", "top_k")
            }
            for backend in ("hazkey", "mozc")
        },
        "top1_first_segment_boundary_groups": {
            key: groups.get(key, 0)
            for key in ("both", "mozc_only", "hazkey_only", "neither")
        },
        "surface_evaluation_coverage": {
            "fully_aligned": coverage.get("fully_aligned", 0),
            "partially_aligned_excluded": coverage.get("partially_aligned", 0),
            "not_aligned_excluded": coverage.get("not_aligned", 0),
        },
        "surface": {
            backend: _surface_metrics(cases, "backends", backend)
            for backend in ("hazkey", "mozc")
        },
        "structured_merge_policies": _policy_metrics(cases),
    }


def evaluate(
    generation_manifest: Path,
    targets_path: Path,
    hazkey_results: Path,
    mozc_results: Path,
) -> dict[str, Any]:
    blobs = _capture_generation(generation_manifest)
    manifest_bytes = blobs[compiler.MANIFEST_NAME]
    manifest, bound = _validate_manifest(blobs)
    targets_bytes = _read_regular(targets_path, "acceptable targets")
    target_binding = manifest["bindings"]["targets"]
    if _sha256(targets_bytes) != target_binding["sha256"] or targets_bytes != bound["targets"]:
        raise ValueError("supplied targets do not match the generation binding")
    targets = _validate_targets(targets_bytes, str(targets_path))
    if len(targets) != target_binding["cases"]:
        raise ValueError("target case count does not match the generation binding")
    _validate_manifest_aggregates(manifest, targets)
    _validate_probe_binding(bound["probe_input"], targets)

    hazkey_bytes = _read_regular(hazkey_results, "Hazkey ABProbe v5 results")
    mozc_bytes = _read_regular(mozc_results, "Mozc ABProbe v5 results")
    hazkey_run = _load_run(hazkey_bytes, hazkey_results, "hazkey")
    mozc_run = _load_run(mozc_bytes, mozc_results, "mozc")
    _validate_runs(targets, manifest, hazkey_run, mozc_run)

    cases: list[dict[str, Any]] = []
    for target in targets:
        case_id = target["id"]
        hazkey_candidates = hazkey_run["cases"][case_id]["candidates"]
        mozc_candidates = mozc_run["cases"][case_id]["candidates"]
        cases.append({
            "id": case_id,
            "category": target["category"],
            "reading": target["reading"],
            "acceptable_first_spans": target["acceptable_first_spans"],
            "surface_evaluation_status": target["surface_evaluation_status"],
            "acceptable_first_chunks": target["acceptable_first_chunks"],
            "backends": {
                "hazkey": _prediction(hazkey_candidates, target),
                "mozc": _prediction(mozc_candidates, target),
            },
            "policies": _structured_policy_predictions(
                hazkey_candidates,
                mozc_candidates,
                target,
                mozc_run["top_k"],
            ),
        })
    formal_categories = set(hybrid.FORMAL_V2_QUALITY_CATEGORIES)
    formal_cases = [case for case in cases if case["category"] in formal_categories]
    return {
        "schema": OUTPUT_SCHEMA,
        "diagnostic_only": True,
        "formal_authorized": False,
        "evaluation_scope": {
            "first_segment_boundary_scope": "first-segment-only",
            "full_segmentation_path_sequence_evaluated": False,
            "surface_scope": "fully-aligned-first-segment-pairs-only",
        },
        "inputs": {
            "generation_manifest": {"path": str(generation_manifest), "sha256": _sha256(manifest_bytes), "schema": MANIFEST_SCHEMA},
            "targets": {"path": str(targets_path), "sha256": _sha256(targets_bytes), "schema": TARGET_SCHEMA, "cases": len(targets)},
            "probe_input": {"path": manifest["bindings"]["probe_input"]["path"], "sha256": _sha256(bound["probe_input"]), "schema": PROBE_INPUT_SCHEMA},
            "hazkey_v5": {"path": str(hazkey_results), "sha256": _sha256(hazkey_bytes), "source_ref": hazkey_run["source_ref"], "resource": hazkey_run["resource"]},
            "mozc_v5": {"path": str(mozc_results), "sha256": _sha256(mozc_bytes), "source_ref": mozc_run["source_ref"], "resource": mozc_run["resource"]},
        },
        "category_policy": {
            "id": "mozc-adoption-v2-quality-categories-v1",
            "included_categories": list(hybrid.FORMAL_V2_QUALITY_CATEGORIES),
            "excluded_case_count": len(cases) - len(formal_cases),
        },
        "all_cases": _slice(cases),
        "formal_quality": _slice(formal_cases),
        "cases": cases,
        "decision": {
            "status": "inconclusive",
            "formal_authorized": False,
            "production_policy_retained": "mozc-first-preserve-top1-h0",
            "reason": "known diagnostic corpus; acceptable paths do not authorize policy adoption",
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate acceptable first-segment paths against paired ABProbe v5 results.")
    parser.add_argument("--generation-manifest", type=Path, required=True)
    parser.add_argument("--targets", type=Path, required=True)
    parser.add_argument("--hazkey-v5", type=Path, required=True)
    parser.add_argument("--mozc-v5", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = evaluate(args.generation_manifest, args.targets, args.hazkey_v5, args.mozc_v5)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 0
    except (OSError, ValueError, AssertionError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
