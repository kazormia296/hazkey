#!/usr/bin/env python3
"""Evaluate sealed reviewed first-segment targets with the frozen H0/H1/H2 policies."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any, Iterable

try:
    from . import build_mozc_hybrid_segment_holdout_v1 as holdout
    from . import evaluate_mozc_hybrid_spike as hybrid
    from .summarize_ab_probe import INPUT_SCHEMA_V5, load_run_bytes
except ImportError:  # Direct execution from tools/dictionary.
    import build_mozc_hybrid_segment_holdout_v1 as holdout  # type: ignore[no-redef]
    import evaluate_mozc_hybrid_spike as hybrid  # type: ignore[no-redef]
    from summarize_ab_probe import INPUT_SCHEMA_V5, load_run_bytes


OUTPUT_SCHEMA = "hazkey.mozc-hybrid-segment-holdout-evaluation.v1"
QUALITY_CATEGORY_POLICY_ID = (
    "mozc-hybrid-reviewed-segment-holdout-quality-categories-v1"
)
EXPECTED_GENERATION_FILES = {
    holdout.SOURCE_CASES_NAME,
    holdout.APPROVAL_NAME,
    holdout.PROBE_INPUT_NAME,
    holdout.SEGMENT_LABELS_NAME,
    holdout.MANIFEST_NAME,
}
ABPROBE_V5_ROOT_FIELDS = {
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
ABPROBE_V5_RSS_FIELDS = {
    "before_kib",
    "after_kib",
    "before_pss_kib",
    "after_pss_kib",
    "backend_before_kib",
    "backend_after_kib",
    "backend_before_pss_kib",
    "backend_after_pss_kib",
}
ABPROBE_V5_BACKEND_DIAGNOSTIC_FIELDS = {
    "process_launch_count",
    "cleanup_failure_count",
}


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _json(data: bytes, context: str) -> dict[str, Any]:
    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_without_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{context} is not valid UTF-8 JSON: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{context} must contain an object")
    return value


def _jsonl(data: bytes, context: str) -> list[dict[str, Any]]:
    if data.startswith(b"\xef\xbb\xbf") or b"\r" in data:
        raise ValueError(f"{context} must be BOM-free UTF-8 JSONL with LF endings")
    if not data.endswith(b"\n"):
        raise ValueError(f"{context} must end with one LF")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"{context} is not valid UTF-8") from error
    lines = text[:-1].split("\n")
    if not lines or any(not line for line in lines):
        raise ValueError(f"{context} must contain non-empty JSON lines")
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, 1):
        try:
            value = json.loads(line, object_pairs_hook=_without_duplicate_keys)
        except (json.JSONDecodeError, ValueError) as error:
            raise ValueError(
                f"{context}:{line_number} is not valid JSON: {error}"
            ) from error
        if not isinstance(value, dict):
            raise ValueError(f"{context}:{line_number} must contain an object")
        records.append(value)
    return records


def _exact_keys(value: Any, expected: Iterable[str], context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be an object")
    expected_set = set(expected)
    actual = set(value)
    if actual != expected_set:
        raise ValueError(
            f"{context} fields differ; missing={sorted(expected_set - actual)!r}, "
            f"unexpected={sorted(actual - expected_set)!r}"
        )
    return value


def _allowed_keys(
    value: Any,
    allowed: Iterable[str],
    required: Iterable[str],
    context: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be an object")
    allowed_set = set(allowed)
    required_set = set(required)
    actual = set(value)
    missing = required_set - actual
    unexpected = actual - allowed_set
    if missing or unexpected:
        raise ValueError(
            f"{context} fields differ; missing={sorted(missing)!r}, "
            f"unexpected={sorted(unexpected)!r}"
        )
    return value


def _validate_abprobe_v5_contract_bytes(data: bytes, context: str) -> None:
    """Reject schema drift at every object boundary before summary loading."""

    records = _jsonl(data, context)
    for line_number, record in enumerate(records, 1):
        record_context = f"{context}:{line_number}"
        root = _exact_keys(record, ABPROBE_V5_ROOT_FIELDS, record_context)
        if root["schema"] != INPUT_SCHEMA_V5:
            raise ValueError(f"{record_context}.schema must be {INPUT_SCHEMA_V5}")
        _exact_keys(
            root["resource"],
            {"kind", "path", "fingerprint"},
            f"{record_context}.resource",
        )
        _exact_keys(
            root["corpus"],
            {"sha256", "cases"},
            f"{record_context}.corpus",
        )
        candidates = root["candidates"]
        if not isinstance(candidates, list):
            raise ValueError(f"{record_context}.candidates must be an array")
        for candidate_index, candidate in enumerate(candidates, 1):
            _exact_keys(
                candidate,
                {"text", "rank", "consuming_count"},
                f"{record_context}.candidates[{candidate_index}]",
            )
        _exact_keys(
            root["composition_span"],
            {"start", "count", "unit"},
            f"{record_context}.composition_span",
        )
        measurement = _exact_keys(
            root["measurement"],
            {"warmups", "iterations", "latency_ms", "rss", "backend_diagnostics"},
            f"{record_context}.measurement",
        )
        _exact_keys(
            measurement["latency_ms"],
            {"median", "p95", "minimum", "maximum", "samples"},
            f"{record_context}.measurement.latency_ms",
        )
        _allowed_keys(
            measurement["rss"],
            ABPROBE_V5_RSS_FIELDS,
            {"before_kib", "after_kib"},
            f"{record_context}.measurement.rss",
        )
        _allowed_keys(
            measurement["backend_diagnostics"],
            ABPROBE_V5_BACKEND_DIAGNOSTIC_FIELDS,
            set(),
            f"{record_context}.measurement.backend_diagnostics",
        )


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


def _read_open_regular(
    directory_fd: int,
    name: str,
    *,
    required_mode: int | None,
) -> bytes:
    descriptor = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=directory_fd,
    )
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError(f"generation file {name} must be a non-hardlinked regular file")
        if required_mode is not None and stat.S_IMODE(before.st_mode) != required_mode:
            raise ValueError(f"generation file {name} must have mode 0444")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if _stat_identity(before) != _stat_identity(after) or _stat_identity(
            before
        ) != _stat_identity(current):
            raise ValueError(f"generation file {name} changed while read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _capture_generation(generation: Path) -> dict[str, bytes]:
    before = os.stat(generation, follow_symlinks=False)
    if not stat.S_ISDIR(before.st_mode):
        raise ValueError("generation must be a non-symlink directory")
    descriptor = os.open(
        generation,
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(descriptor)
        if _stat_identity(opened) != _stat_identity(before):
            raise ValueError("generation changed while opened")
        if stat.S_IMODE(opened.st_mode) != 0o555:
            raise ValueError("generation directory must have mode 0555")
        names = set(os.listdir(descriptor))
        if names != EXPECTED_GENERATION_FILES:
            raise ValueError(
                "generation file set differs; "
                f"missing={sorted(EXPECTED_GENERATION_FILES - names)!r}, "
                f"unexpected={sorted(names - EXPECTED_GENERATION_FILES)!r}"
            )
        blobs = {
            name: _read_open_regular(descriptor, name, required_mode=0o444)
            for name in sorted(names)
        }
        after_fd = os.fstat(descriptor)
        after_path = os.stat(generation, follow_symlinks=False)
        if _stat_identity(opened) != _stat_identity(after_fd) or _stat_identity(
            opened
        ) != _stat_identity(after_path):
            raise ValueError("generation directory changed while read")
    finally:
        os.close(descriptor)
    expected_name = holdout.sealed_directory_name(blobs)
    if generation.name != expected_name:
        raise ValueError(
            f"generation name must bind exact content as {expected_name!r}"
        )
    return blobs


def _read_regular_path(path: Path, context: str) -> bytes:
    before = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValueError(f"{context} must be a non-hardlinked regular file")
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(descriptor)
        if _stat_identity(opened) != _stat_identity(before):
            raise ValueError(f"{context} changed while opened")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        current = os.stat(path, follow_symlinks=False)
        if _stat_identity(opened) != _stat_identity(after) or _stat_identity(
            opened
        ) != _stat_identity(current):
            raise ValueError(f"{context} changed while read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _validate_derived_records(
    cases: list[dict[str, Any]],
    probe_bytes: bytes,
    label_bytes: bytes,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    probes = _jsonl(probe_bytes, holdout.PROBE_INPUT_NAME)
    labels = _jsonl(label_bytes, holdout.SEGMENT_LABELS_NAME)
    if len(probes) != len(cases) or len(labels) != len(cases):
        raise ValueError("derived probe/label case counts do not match source cases")
    for index, (case, probe, label) in enumerate(
        zip(cases, probes, labels, strict=True), 1
    ):
        expected_probe = {
            "schema": holdout.PROBE_INPUT_SCHEMA,
            "id": case["id"],
            "category": case["category"],
            "elements": [dict(element) for element in case["elements"]],
        }
        expected_label = {
            "schema": holdout.SEGMENT_LABEL_SCHEMA,
            "id": case["id"],
            "family_id": case["family_id"],
            "target": {
                "span": dict(case["target"]["span"]),
                "surfaces": list(case["target"]["surfaces"]),
            },
        }
        if probe != expected_probe:
            raise ValueError(f"probe-input.jsonl:{index} is not derived from source cases")
        if label != expected_label:
            raise ValueError(f"segment-labels.jsonl:{index} is not derived from source cases")
    return probes, labels


def _validate_binding(
    blobs: dict[str, bytes],
    cases: list[dict[str, Any]],
    approval: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    manifest = _exact_keys(
        _json(blobs[holdout.MANIFEST_NAME], holdout.MANIFEST_NAME),
        {
            "schema",
            "holdout_id",
            "formal_authorized",
            "human_collection_attested",
            "bindings",
            "category_counts",
            "evaluation_contract",
            "policy_freeze",
            "outstanding_requirements",
        },
        "manifest",
    )
    if manifest["schema"] != holdout.MANIFEST_SCHEMA:
        raise ValueError(f"manifest.schema must be {holdout.MANIFEST_SCHEMA}")
    if manifest["holdout_id"] != approval["holdout_id"]:
        raise ValueError("manifest holdout_id does not match approval")
    if manifest["formal_authorized"] is not False:
        raise ValueError("segment holdout manifest must not claim formal authorization")
    if manifest["human_collection_attested"] is not True:
        raise ValueError("segment holdout manifest must attest human collection")

    bindings = _exact_keys(
        manifest["bindings"],
        {"source_cases", "review_approval", "probe_input", "segment_labels"},
        "manifest.bindings",
    )
    binding_contracts = {
        "source_cases": (
            holdout.SOURCE_CASES_NAME,
            holdout.CASE_SCHEMA,
            len(cases),
        ),
        "probe_input": (
            holdout.PROBE_INPUT_NAME,
            holdout.PROBE_INPUT_SCHEMA,
            len(cases),
        ),
        "segment_labels": (
            holdout.SEGMENT_LABELS_NAME,
            holdout.SEGMENT_LABEL_SCHEMA,
            len(cases),
        ),
    }
    for field, (name, schema, count) in binding_contracts.items():
        binding = _exact_keys(
            bindings[field], {"path", "schema", "sha256", "cases"}, f"bindings.{field}"
        )
        if binding != {
            "path": name,
            "schema": schema,
            "sha256": _sha256(blobs[name]),
            "cases": count,
        }:
            raise ValueError(f"manifest binding for {field} does not match exact bytes")
    approval_binding = _exact_keys(
        bindings["review_approval"],
        {"path", "schema", "status", "sha256"},
        "bindings.review_approval",
    )
    if approval_binding != {
        "path": holdout.APPROVAL_NAME,
        "schema": holdout.APPROVAL_SCHEMA,
        "status": "approved",
        "sha256": _sha256(blobs[holdout.APPROVAL_NAME]),
    }:
        raise ValueError("manifest review approval binding does not match exact bytes")

    category_counts = dict(Counter(case["category"] for case in cases))
    if manifest["category_counts"] != category_counts:
        raise ValueError("manifest category_counts do not match source cases")
    quality_count_mismatches = {
        category: {"expected": expected, "actual": category_counts.get(category, 0)}
        for category, expected in approval["quality_categories"].items()
        if category_counts.get(category, 0) != expected
    }
    if quality_count_mismatches:
        raise ValueError(
            "approval quality category counts do not match source cases: "
            f"{quality_count_mismatches!r}"
        )
    evaluation_contract = _exact_keys(
        manifest["evaluation_contract"],
        {
            "quality_categories",
            "minimum_h2_promotion_opportunities",
            "target_match",
        },
        "manifest.evaluation_contract",
    )
    if evaluation_contract != {
        "quality_categories": sorted(approval["quality_categories"]),
        "minimum_h2_promotion_opportunities": approval[
            "minimum_h2_promotion_opportunities"
        ],
        "target_match": (
            "raw-exact-NFC-label-surface-and-composition-element-count.v1"
        ),
    }:
        raise ValueError("manifest evaluation_contract does not match approval")
    policy_binding = _exact_keys(
        manifest["policy_freeze"], {"sha256", "value"}, "manifest.policy_freeze"
    )
    if policy_binding["value"] != approval["policy_freeze"] or policy_binding[
        "sha256"
    ] != _sha256(_canonical_json(policy_binding["value"])):
        raise ValueError("manifest policy_freeze binding is invalid")
    outstanding = _exact_keys(
        manifest["outstanding_requirements"],
        {
            "existing_v2_and_auxiliary_duplicate_screen",
            "backend_label_isolation",
            "evaluator_loaded_code_identity",
            "formal_authorization_blocked",
        },
        "manifest.outstanding_requirements",
    )
    if outstanding != {
        "existing_v2_and_auxiliary_duplicate_screen": "not_implemented",
        "backend_label_isolation": "not_implemented",
        "evaluator_loaded_code_identity": "not_attested",
        "formal_authorization_blocked": True,
    }:
        raise ValueError("manifest outstanding requirements changed")
    return manifest, _sha256(_canonical_json(bindings))


def _validate_run_order(run: dict[str, Any], expected_ids: list[str], context: str) -> None:
    actual_ids = list(run["cases"])
    if actual_ids != expected_ids:
        raise ValueError(
            f"{context} result order does not match probe input; "
            f"expected={expected_ids!r}, actual={actual_ids!r}"
        )


def _validate_policy_freeze(
    freeze: dict[str, Any],
    hazkey_run: dict[str, Any],
    mozc_run: dict[str, Any],
) -> dict[str, Any]:
    expected_policy_ids = {
        "h0_policy_id": "mozc-first-preserve-top1-h0",
        "h1_policy_id": hybrid.POLICY_ID,
        "h2_policy_id": hybrid.WIDTH_GUARDED_POLICY_ID,
    }
    for field, expected in expected_policy_ids.items():
        if freeze[field] != expected:
            raise ValueError(f"policy freeze {field} does not match evaluator")
    evaluator_sha256 = _sha256(
        _read_regular_path(Path(__file__), "segment holdout evaluator")
    )
    if freeze["evaluator_sha256"] != evaluator_sha256:
        raise ValueError("policy freeze evaluator_sha256 does not match this evaluator")
    hybrid_evaluator_sha256 = _sha256(
        _read_regular_path(
            Path(hybrid.__file__), "shared Mozc hybrid policy evaluator"
        )
    )
    if freeze["hybrid_evaluator_sha256"] != hybrid_evaluator_sha256:
        raise ValueError(
            "policy freeze hybrid_evaluator_sha256 does not match shared evaluator"
        )
    for run_name, run in (("Hazkey", hazkey_run), ("Mozc", mozc_run)):
        if run["schema"] != INPUT_SCHEMA_V5:
            raise ValueError(f"{run_name} segment holdout results must use ABProbe v5")
        if run["source_ref"] != freeze["product_source_revision"]:
            raise ValueError(f"{run_name} source_ref does not match policy freeze")
        for field in ("top_k", "warmups", "iterations"):
            if run[field] != freeze[field]:
                raise ValueError(f"{run_name} {field} does not match policy freeze")
    if hazkey_run["resource"]["fingerprint"] != freeze[
        "hazkey_resource_fingerprint"
    ]:
        raise ValueError("Hazkey resource fingerprint does not match policy freeze")
    if mozc_run["resource"]["fingerprint"] != freeze[
        "mozc_resource_fingerprint"
    ]:
        raise ValueError("Mozc resource fingerprint does not match policy freeze")
    if Path(mozc_run["resource"]["path"]).name != freeze[
        "mozc_bundle_generation"
    ]:
        raise ValueError("Mozc bundle generation does not match policy freeze")
    return {
        "evaluator": {
            "expected_sha256": freeze["evaluator_sha256"],
            "observed_sha256": evaluator_sha256,
            "status": "source_file_hash_match",
            "loaded_code_identity": "not_attested",
        },
        "hybrid_evaluator": {
            "expected_sha256": freeze["hybrid_evaluator_sha256"],
            "observed_sha256": hybrid_evaluator_sha256,
            "status": "source_file_hash_match",
            "loaded_code_identity": "not_attested",
        },
        "abprobe_executable": {
            "expected_sha256": freeze["abprobe_executable_sha256"],
            "observed_sha256": None,
            "status": "not_bound_by_probe_result",
        },
        "hazkey_resource": {
            "expected_fingerprint": freeze["hazkey_resource_fingerprint"],
            "observed_fingerprint": hazkey_run["resource"]["fingerprint"],
            "kind": hazkey_run["resource"]["kind"],
            "path": hazkey_run["resource"]["path"],
            "status": "probe_result_match",
        },
        "mozc_resource": {
            "expected_fingerprint": freeze["mozc_resource_fingerprint"],
            "observed_fingerprint": mozc_run["resource"]["fingerprint"],
            "expected_bundle_generation": freeze["mozc_bundle_generation"],
            "observed_bundle_generation": Path(
                mozc_run["resource"]["path"]
            ).name,
            "kind": mozc_run["resource"]["kind"],
            "path": mozc_run["resource"]["path"],
            "status": "probe_result_match",
        },
    }


def evaluate_generation(
    generation: Path,
    hazkey_results: Path,
    mozc_results: Path,
) -> dict[str, Any]:
    blobs = _capture_generation(generation)
    cases = holdout.load_cases_bytes(blobs[holdout.SOURCE_CASES_NAME])
    approval = holdout.load_approval_bytes(blobs[holdout.APPROVAL_NAME])
    if approval["source_cases_sha256"] != _sha256(
        blobs[holdout.SOURCE_CASES_NAME]
    ):
        raise ValueError("approval source case identity does not match generation")
    probes, labels = _validate_derived_records(
        cases,
        blobs[holdout.PROBE_INPUT_NAME],
        blobs[holdout.SEGMENT_LABELS_NAME],
    )
    manifest, bindings_sha256 = _validate_binding(blobs, cases, approval)

    hazkey_bytes = _read_regular_path(hazkey_results, "Hazkey results")
    mozc_bytes = _read_regular_path(mozc_results, "Mozc results")
    _validate_abprobe_v5_contract_bytes(hazkey_bytes, str(hazkey_results))
    _validate_abprobe_v5_contract_bytes(mozc_bytes, str(mozc_results))
    hazkey_run = load_run_bytes(hazkey_bytes, hazkey_results)
    mozc_run = load_run_bytes(mozc_bytes, mozc_results)
    expected_ids = [probe["id"] for probe in probes]
    _validate_run_order(hazkey_run, expected_ids, "Hazkey")
    _validate_run_order(mozc_run, expected_ids, "Mozc")
    freeze = manifest["policy_freeze"]["value"]
    artifact_identity = _validate_policy_freeze(
        freeze, hazkey_run, mozc_run
    )

    corpus = [
        {
            "id": probe["id"],
            "reading": "".join(element["text"] for element in probe["elements"]),
            "expected": "|".join(label["target"]["surfaces"]),
            "category": probe["category"],
        }
        for probe, label in zip(probes, labels, strict=True)
    ]
    targets = {label["id"]: label["target"] for label in labels}
    generation_digest = generation.name.removeprefix(
        holdout.SEALED_DIRECTORY_PREFIX
    )
    additional_inputs = {
        "generation": {
            "name": generation.name,
            "content_sha256": "sha256:" + generation_digest,
            "files": len(blobs),
        },
        "manifest": {
            "schema": holdout.MANIFEST_SCHEMA,
            "sha256": _sha256(blobs[holdout.MANIFEST_NAME]),
            "holdout_id": manifest["holdout_id"],
        },
        "binding": {
            "authority": "manifest.json.bindings",
            "sha256": bindings_sha256,
        },
        "probe_input": {
            "schema": holdout.PROBE_INPUT_SCHEMA,
            "sha256": _sha256(blobs[holdout.PROBE_INPUT_NAME]),
            "cases": len(probes),
        },
        "segment_labels": {
            "schema": holdout.SEGMENT_LABEL_SCHEMA,
            "sha256": _sha256(blobs[holdout.SEGMENT_LABELS_NAME]),
            "cases": len(labels),
        },
        "policy_freeze": {
            "sha256": manifest["policy_freeze"]["sha256"],
            "value": freeze,
        },
    }
    formal_categories = list(
        manifest["evaluation_contract"]["quality_categories"]
    )
    report = hybrid.evaluate_runs(
        corpus,
        hazkey_run,
        mozc_run,
        corpus_sha256=_sha256(blobs[holdout.PROBE_INPUT_NAME]),
        corpus_bytes=blobs[holdout.PROBE_INPUT_NAME],
        hazkey_bytes=hazkey_bytes,
        mozc_bytes=mozc_bytes,
        hazkey_context=str(hazkey_results),
        mozc_context=str(mozc_results),
        reviewed_first_segment_targets=targets,
        formal_quality_categories=formal_categories,
        formal_quality_category_policy_id=QUALITY_CATEGORY_POLICY_ID,
        reviewed_target_metadata={
            "label_schema": holdout.SEGMENT_LABEL_SCHEMA,
            "labels_sha256": _sha256(blobs[holdout.SEGMENT_LABELS_NAME]),
            "bindings_sha256": bindings_sha256,
            "match": manifest["evaluation_contract"]["target_match"],
        },
        additional_input_metadata=additional_inputs,
        report_schema=OUTPUT_SCHEMA,
        new_holdout_required=False,
    )
    if report["target_comparability"]["comparable_count"] != len(cases):
        raise AssertionError("reviewed segment labels did not make every case comparable")
    if report["promotion_opportunities"]["outcome_incomparable_count"] != 0:
        raise AssertionError("H1 promotion outcomes must all be label-comparable")
    if report["width_guarded_promotion_opportunities"][
        "outcome_incomparable_count"
    ] != 0:
        raise AssertionError("H2 promotion outcomes must all be label-comparable")
    minimum_h2 = manifest["evaluation_contract"][
        "minimum_h2_promotion_opportunities"
    ]
    quality_categories = set(
        manifest["evaluation_contract"]["quality_categories"]
    )
    eligible_h2_cases = [
        case
        for case in report["cases"]
        if case["category"] in quality_categories and case["target_comparable"]
    ]
    observed_h2 = sum(
        case["width_guarded_policy_decision"] == hybrid.PROMOTION_DECISION
        for case in eligible_h2_cases
    )
    opportunity_minimum_met = observed_h2 >= minimum_h2
    blocking_reasons = [
        "abprobe_executable_not_bound_by_probe_result",
        "existing_v2_and_auxiliary_duplicate_screen_not_implemented",
        "backend_label_isolation_not_implemented",
        "evaluator_loaded_code_identity_not_attested",
    ]
    if not opportunity_minimum_met:
        blocking_reasons.append("h2_promotion_opportunity_minimum_not_met")
    report["artifact_identity"] = artifact_identity
    report["decision"] = {
        "status": "inconclusive",
        "formal_authorized": False,
        "blocking_reasons": blocking_reasons,
        "h2_promotion_opportunity_gate": {
            "scope": "manifest_quality_categories_and_target_comparable_cases",
            "quality_categories": sorted(quality_categories),
            "eligible_cases": len(eligible_h2_cases),
            "observed": observed_h2,
            "required": minimum_h2,
            "met": opportunity_minimum_met,
        },
        "production_policy": {
            "id": "mozc-first-preserve-top1-h0",
            "retained": True,
        },
    }
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate a sealed reviewed Mozc hybrid first-segment holdout."
    )
    parser.add_argument("--generation", type=Path, required=True)
    parser.add_argument("--hazkey-results", type=Path, required=True)
    parser.add_argument("--mozc-results", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = evaluate_generation(
            args.generation,
            args.hazkey_results,
            args.mozc_results,
        )
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return 0
    except (OSError, ValueError, AssertionError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
