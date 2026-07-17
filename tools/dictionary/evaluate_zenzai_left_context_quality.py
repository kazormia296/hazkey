#!/usr/bin/env python3
"""Compare Hazkey+Zenzai with empty and natural left context.

The baseline and candidate are both strict ABProbe v7 runs whose left contexts
are supplied by separately bound blind-Silver context sidecars.  The baseline
sidecar must be empty for every case; the candidate sidecar must contain at
least one natural-left case.  Both runs must otherwise be the same acquisition:
corpus, model, producer, resource, options, IDs, and order.

This is a diagnostic comparison.  The existing acceptable-path generation was
not authored as a context-conditioned gold set, so the report cannot authorize
a production override policy.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import math
from pathlib import Path
import statistics
import sys
from typing import Any, Iterable, Sequence

try:
    from . import evaluate_mozc_acceptable_path_boundaries as acceptable
    from . import evaluate_mozc_zenzai_hybrid_quality as quality
    from . import prepare_blind_silver_annotations as blind
    from . import prepare_mozc_fixed_boundary_sidecar as fixed_prepare
except ImportError:  # Direct execution from tools/dictionary.
    import evaluate_mozc_acceptable_path_boundaries as acceptable
    import evaluate_mozc_zenzai_hybrid_quality as quality
    import prepare_blind_silver_annotations as blind
    import prepare_mozc_fixed_boundary_sidecar as fixed_prepare


INPUT_SCHEMA_V7 = "hazkey.ab-probe-result.v7"
OUTPUT_SCHEMA = "hazkey.zenzai-left-context-quality-evaluation.v1"
CONTEXT_POLICY = "left_context_sidecar"
CONTEXT_MODES = frozenset({"empty", "natural_left"})
V7_ROOT_FIELDS = quality.ROOT_FIELDS | {
    "context",
    "boundary_policy",
    "fixed_boundary",
    "zenzai_execution",
}
V7_CONTEXT_FIELDS = {
    "mode",
    "left_context_sha256",
    "left_context_code_point_count",
    "left_context_utf8_byte_count",
    "source_content_sha256",
    "source",
}
V7_CONTEXT_SOURCE_FIELDS = {"schema", "sha256", "cases"}
V7_FIXED_BOUNDARY_FIELDS = {
    "reading_sha256",
    "consuming_count",
    "source",
}
V7_FIXED_BOUNDARY_SOURCE_FIELDS = {"schema", "sha256", "cases"}
V7_BOUNDARY_POLICY_FIELDS = {
    "mode",
    "boundary_zenzai_enabled",
    "surface_zenzai_enabled",
    "source",
}
V7_ZENZAI_EXECUTION_FIELDS = {
    "request_count",
    "evaluation_attempt_count",
    "attempt_outcomes",
    "terminal_outcomes",
}
V7_ZENZAI_ATTEMPT_OUTCOME_ORDER = (
    "pass",
    "fix_required",
    "whole_result",
    "error",
)
V7_ZENZAI_ATTEMPT_OUTCOME_FIELDS = set(V7_ZENZAI_ATTEMPT_OUTCOME_ORDER)
V7_ZENZAI_TERMINAL_OUTCOME_ORDER = V7_ZENZAI_ATTEMPT_OUTCOME_ORDER + (
    "inference_limit",
    "no_candidate",
)
V7_ZENZAI_TERMINAL_OUTCOME_FIELDS = set(V7_ZENZAI_TERMINAL_OUTCOME_ORDER)
COMMON_HAZKEY_V7_ACQUISITION_FIELDS = (
    "schema",
    "backend",
    "backend_version",
    "converter_backend",
    "source_ref",
    "resource",
    "producer",
    "quality_policy",
    "top_k",
    "corpus",
    "warmups",
    "iterations",
)
V7_BOUNDARY_POLICIES = {
    "isolated_dictionary": {
        "mode": "isolated_dictionary",
        "boundary_zenzai_enabled": False,
        "surface_zenzai_enabled": True,
        "source": "separate_converter",
        "conversion_path": quality.CONVERSION_PATH,
    },
    "native_zenzai_first_clause": {
        "mode": "native_zenzai_first_clause",
        "boundary_zenzai_enabled": True,
        "surface_zenzai_enabled": True,
        "source": "primary_converter_first_clause_results",
        "conversion_path": "native_segment_candidates",
    },
    "mozc_fixed": {
        "mode": "mozc_fixed",
        "boundary_zenzai_enabled": False,
        "surface_zenzai_enabled": True,
        "source": "mozc_top1_fixed_boundary_sidecar",
        "conversion_path": "mozc_fixed_segment_candidates",
    },
}
V7_BOUNDARY_REQUEST_COUNTS = {
    "isolated_dictionary": 2,
    "native_zenzai_first_clause": 1,
    "mozc_fixed": 1,
}
EMPTY_SHA256 = acceptable._sha256(b"")
ISOLATED_EMPTY_SYSTEM = "isolated_empty_context_v7"
ISOLATED_LEFT_CONTEXT_SYSTEM = "isolated_left_context_v7"
ISOLATED_SYSTEMS = (ISOLATED_EMPTY_SYSTEM, ISOLATED_LEFT_CONTEXT_SYSTEM)
NATIVE_EMPTY_SYSTEM = "native_empty_context_v7"
NATIVE_LEFT_CONTEXT_SYSTEM = "native_left_context_v7"
NATIVE_SYSTEMS = (NATIVE_EMPTY_SYSTEM, NATIVE_LEFT_CONTEXT_SYSTEM)
FIXED_EMPTY_SYSTEM = "mozc_fixed_empty_context_v7"
FIXED_LEFT_CONTEXT_SYSTEM = "mozc_fixed_left_context_v7"
FIXED_SYSTEMS = (FIXED_EMPTY_SYSTEM, FIXED_LEFT_CONTEXT_SYSTEM)
MOZC_AT_FIXED_BOUNDARY_SYSTEM = "mozc_at_fixed_boundary"
LENGTH_BUCKETS = (
    ("0", 0, 0),
    ("1-8", 1, 8),
    ("9-16", 9, 16),
    ("17-32", 17, 32),
    ("33-64", 33, 64),
    ("65-128", 65, 128),
    ("129+", 129, None),
)
MEMORY_MEASURES = {
    "process_rss_kib": ("before_kib", "after_kib"),
    "process_pss_kib": ("before_pss_kib", "after_pss_kib"),
    "backend_rss_kib": ("backend_before_kib", "backend_after_kib"),
    "backend_pss_kib": (
        "backend_before_pss_kib",
        "backend_after_pss_kib",
    ),
}


def _distribution(values: Sequence[float]) -> dict[str, Any]:
    if not values:
        return {
            "count": 0,
            "minimum": None,
            "median": None,
            "mean": None,
            "p95": None,
            "maximum": None,
        }
    normalized = [float(value) for value in values]
    return {
        "count": len(normalized),
        "minimum": min(normalized),
        "median": float(statistics.median(normalized)),
        "mean": float(statistics.fmean(normalized)),
        "p95": quality._nearest_rank_p95(normalized),
        "maximum": max(normalized),
    }


def _execution_summary(
    cases: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    observed = list(cases)
    attempts = {
        field: sum(
            case["zenzai_execution"]["attempt_outcomes"][field]
            for case in observed
        )
        for field in V7_ZENZAI_ATTEMPT_OUTCOME_ORDER
    }
    terminals = {
        field: sum(
            case["zenzai_execution"]["terminal_outcomes"][field]
            for case in observed
        )
        for field in V7_ZENZAI_TERMINAL_OUTCOME_ORDER
    }
    terminal_case_counts = {
        field: sum(
            case["zenzai_execution"]["terminal_outcomes"][field] > 0
            for case in observed
        )
        for field in V7_ZENZAI_TERMINAL_OUTCOME_ORDER
    }
    request_count = sum(
        case["zenzai_execution"]["request_count"] for case in observed
    )
    successful_request_count = terminals["pass"]
    return {
        "cases": len(observed),
        "request_count": request_count,
        "evaluation_attempt_count": sum(
            case["zenzai_execution"]["evaluation_attempt_count"]
            for case in observed
        ),
        "attempt_outcomes": attempts,
        "terminal_outcomes": terminals,
        "cases_with_terminal_outcome": terminal_case_counts,
        "successful_request_count": successful_request_count,
        "terminal_failure_request_count": request_count
        - successful_request_count,
        "terminal_pass_rate": (
            successful_request_count / request_count if request_count else None
        ),
        "candidate_score_required": False,
    }


def _comparison_execution_summary(
    cases: list[dict[str, Any]], systems: tuple[str, str]
) -> dict[str, Any]:
    return {
        system: _execution_summary(
            [
                {"zenzai_execution": case["zenzai_execution"][system]}
                for case in cases
            ]
        )
        for system in systems
    }


def _execution_failure_blockers(
    runs: Sequence[tuple[str, dict[str, Any]]],
) -> list[str]:
    blockers: list[str] = []
    for label, run in runs:
        terminals = run["zenzai_execution"]["terminal_outcomes"]
        for outcome in (
            "fix_required",
            "whole_result",
            "error",
            "inference_limit",
            "no_candidate",
        ):
            count = terminals[outcome]
            if count:
                blockers.append(
                    f"{label} contains {count} terminal Zenzai {outcome} outcome(s)"
                )
    return blockers


def _validate_context_source(value: Any, where: str) -> dict[str, Any]:
    source = acceptable._exact_object(value, V7_CONTEXT_SOURCE_FIELDS, where)
    if source["schema"] != blind.CONTEXT_SCHEMA:
        raise ValueError(
            f"{where}.schema must be {blind.CONTEXT_SCHEMA!r}"
        )
    return {
        "schema": blind.CONTEXT_SCHEMA,
        "sha256": acceptable._hash(source["sha256"], f"{where}.sha256"),
        "cases": acceptable._positive_int(source["cases"], f"{where}.cases"),
    }


def _validate_v7_context(value: Any, where: str) -> dict[str, Any]:
    context = acceptable._exact_object(value, V7_CONTEXT_FIELDS, where)
    mode = context["mode"]
    if mode not in CONTEXT_MODES:
        raise ValueError(f"{where}.mode must be 'empty' or 'natural_left'")
    code_points = acceptable._nonnegative_int(
        context["left_context_code_point_count"],
        f"{where}.left_context_code_point_count",
    )
    byte_count = acceptable._nonnegative_int(
        context["left_context_utf8_byte_count"],
        f"{where}.left_context_utf8_byte_count",
    )
    digest = acceptable._hash(
        context["left_context_sha256"], f"{where}.left_context_sha256"
    )
    is_empty = code_points == 0
    if (byte_count == 0) != is_empty:
        raise ValueError(f"{where} code-point and UTF-8 byte counts conflict")
    if not is_empty and not code_points <= byte_count <= code_points * 4:
        raise ValueError(f"{where} UTF-8 byte count is impossible")
    if (mode == "empty") != is_empty:
        raise ValueError(f"{where}.mode conflicts with its context length")
    if is_empty and digest != EMPTY_SHA256:
        raise ValueError(f"{where}.left_context_sha256 is not the empty hash")
    if not is_empty and digest == EMPTY_SHA256:
        raise ValueError(f"{where}.left_context_sha256 is the empty hash")
    return {
        "mode": mode,
        "left_context_sha256": digest,
        "left_context_code_point_count": code_points,
        "left_context_utf8_byte_count": byte_count,
        "source_content_sha256": acceptable._hash(
            context["source_content_sha256"],
            f"{where}.source_content_sha256",
        ),
        "source": _validate_context_source(context["source"], f"{where}.source"),
    }


def _validate_boundary_policy(
    value: Any, conversion_path: Any, where: str
) -> dict[str, Any]:
    policy = acceptable._exact_object(value, V7_BOUNDARY_POLICY_FIELDS, where)
    mode = policy["mode"]
    if mode not in V7_BOUNDARY_POLICIES:
        raise ValueError(f"{where}.mode is not supported")
    expected = V7_BOUNDARY_POLICIES[mode]
    normalized = {field: policy[field] for field in V7_BOUNDARY_POLICY_FIELDS}
    if normalized != {
        field: expected[field] for field in V7_BOUNDARY_POLICY_FIELDS
    }:
        raise ValueError(f"{where} fields conflict with mode {mode!r}")
    if conversion_path != expected["conversion_path"]:
        raise ValueError(f"{where} conflicts with conversion_path")
    return normalized


def _validate_fixed_boundary(value: Any, where: str) -> dict[str, Any]:
    boundary = acceptable._exact_object(value, V7_FIXED_BOUNDARY_FIELDS, where)
    source = acceptable._exact_object(
        boundary["source"], V7_FIXED_BOUNDARY_SOURCE_FIELDS, f"{where}.source"
    )
    if source["schema"] != fixed_prepare.SIDECAR_SCHEMA:
        raise ValueError(
            f"{where}.source.schema must be {fixed_prepare.SIDECAR_SCHEMA!r}"
        )
    return {
        "reading_sha256": acceptable._hash(
            boundary["reading_sha256"], f"{where}.reading_sha256"
        ),
        "consuming_count": acceptable._positive_int(
            boundary["consuming_count"], f"{where}.consuming_count"
        ),
        "source": {
            "schema": fixed_prepare.SIDECAR_SCHEMA,
            "sha256": acceptable._hash(
                source["sha256"], f"{where}.source.sha256"
            ),
            "cases": acceptable._positive_int(
                source["cases"], f"{where}.source.cases"
            ),
        },
    }


def _validate_zenzai_execution(value: Any, where: str) -> dict[str, Any]:
    execution = acceptable._exact_object(
        value, V7_ZENZAI_EXECUTION_FIELDS, where
    )
    attempts = acceptable._exact_object(
        execution["attempt_outcomes"],
        V7_ZENZAI_ATTEMPT_OUTCOME_FIELDS,
        f"{where}.attempt_outcomes",
    )
    terminals = acceptable._exact_object(
        execution["terminal_outcomes"],
        V7_ZENZAI_TERMINAL_OUTCOME_FIELDS,
        f"{where}.terminal_outcomes",
    )
    normalized_attempts = {
        field: acceptable._nonnegative_int(
            attempts[field], f"{where}.attempt_outcomes.{field}"
        )
        for field in V7_ZENZAI_ATTEMPT_OUTCOME_ORDER
    }
    normalized_terminals = {
        field: acceptable._nonnegative_int(
            terminals[field], f"{where}.terminal_outcomes.{field}"
        )
        for field in V7_ZENZAI_TERMINAL_OUTCOME_ORDER
    }
    request_count = acceptable._positive_int(
        execution["request_count"], f"{where}.request_count"
    )
    evaluation_attempt_count = acceptable._nonnegative_int(
        execution["evaluation_attempt_count"],
        f"{where}.evaluation_attempt_count",
    )
    if sum(normalized_attempts.values()) != evaluation_attempt_count:
        raise ValueError(
            f"{where}.attempt_outcomes total must equal evaluation_attempt_count"
        )
    if sum(normalized_terminals.values()) != request_count:
        raise ValueError(
            f"{where}.terminal_outcomes total must equal request_count"
        )
    for outcome in V7_ZENZAI_ATTEMPT_OUTCOME_ORDER:
        if normalized_terminals[outcome] > normalized_attempts[outcome]:
            raise ValueError(
                f"{where}.terminal_outcomes.{outcome} cannot exceed "
                f"attempt_outcomes.{outcome}"
            )
    return {
        "request_count": request_count,
        "evaluation_attempt_count": evaluation_attempt_count,
        "attempt_outcomes": normalized_attempts,
        "terminal_outcomes": normalized_terminals,
    }


def _validate_v7_record(value: Any, where: str) -> dict[str, Any]:
    root = acceptable._exact_object(value, V7_ROOT_FIELDS, where)
    if root["schema"] != INPUT_SCHEMA_V7:
        raise ValueError(f"{where}.schema must be {INPUT_SCHEMA_V7!r}")
    policy = acceptable._exact_object(
        root["quality_policy"], quality.QUALITY_POLICY_FIELDS, f"{where}.quality_policy"
    )
    if policy["context"] != CONTEXT_POLICY:
        raise ValueError(
            f"{where}.quality_policy.context must be {CONTEXT_POLICY!r}"
        )
    # The v7 conversion/candidate/measurement contract is deliberately v6 plus
    # one context object and one changed policy literal.  Downgrading a copy lets
    # the established strict validator remain the single source of truth.
    boundary_policy = _validate_boundary_policy(
        root["boundary_policy"], root["conversion_path"], f"{where}.boundary_policy"
    )
    if boundary_policy["mode"] == "mozc_fixed":
        if root["fixed_boundary"] is None:
            raise ValueError(
                f"{where}.fixed_boundary is required for mozc_fixed mode"
            )
        fixed_boundary = _validate_fixed_boundary(
            root["fixed_boundary"], f"{where}.fixed_boundary"
        )
    else:
        if root["fixed_boundary"] is not None:
            raise ValueError(
                f"{where}.fixed_boundary must be null for isolated/native modes"
            )
        fixed_boundary = None
    v6_compatible = dict(root)
    v6_compatible.pop("context")
    v6_compatible.pop("boundary_policy")
    v6_compatible.pop("fixed_boundary")
    v6_compatible.pop("zenzai_execution")
    v6_compatible["schema"] = quality.INPUT_SCHEMA
    v6_compatible["conversion_path"] = quality.CONVERSION_PATH
    v6_policy = dict(policy)
    v6_policy["context"] = "empty"
    v6_compatible["quality_policy"] = v6_policy
    normalized = quality._validate_v6_record(v6_compatible, where)
    if normalized["converter_backend"] != "hazkey":
        raise ValueError(f"{where}: ABProbe v7 is supported only for Hazkey")
    if not normalized["quality_policy"]["zenzai"]["enabled"]:
        raise ValueError(f"{where}: ABProbe v7 must enable Zenzai")
    if normalized["measurement"]["iterations"] != 1:
        raise ValueError(
            f"{where}.measurement.iterations must be 1 when Zenzai is enabled"
        )
    normalized["schema"] = INPUT_SCHEMA_V7
    normalized["conversion_path"] = root["conversion_path"]
    normalized["quality_policy"] = {
        **normalized["quality_policy"],
        "context": CONTEXT_POLICY,
    }
    normalized["context"] = _validate_v7_context(root["context"], f"{where}.context")
    normalized["boundary_policy"] = boundary_policy
    if fixed_boundary is not None:
        expected_reading_sha256 = acceptable._sha256(
            normalized["reading"].encode("utf-8")
        )
        if fixed_boundary["reading_sha256"] != expected_reading_sha256:
            raise ValueError(
                f"{where}.fixed_boundary.reading_sha256 does not match reading"
            )
        if fixed_boundary["consuming_count"] > normalized["composition_span"]["count"]:
            raise ValueError(
                f"{where}.fixed_boundary.consuming_count exceeds composition_span"
            )
        if any(
            candidate["consuming_count"] != fixed_boundary["consuming_count"]
            for candidate in normalized["candidates"]
        ):
            raise ValueError(
                f"{where}.candidates escape the fixed boundary"
            )
    normalized["fixed_boundary"] = fixed_boundary
    normalized["zenzai_execution"] = _validate_zenzai_execution(
        root["zenzai_execution"], f"{where}.zenzai_execution"
    )
    expected_request_count = V7_BOUNDARY_REQUEST_COUNTS[boundary_policy["mode"]]
    if normalized["zenzai_execution"]["request_count"] != expected_request_count:
        raise ValueError(
            f"{where}.zenzai_execution.request_count must be "
            f"{expected_request_count} for boundary mode {boundary_policy['mode']!r}"
        )
    inference_limit = normalized["quality_policy"]["zenzai"]["inference_limit"]
    maximum_attempts = inference_limit * expected_request_count
    if normalized["zenzai_execution"]["evaluation_attempt_count"] > maximum_attempts:
        raise ValueError(
            f"{where}.zenzai_execution.evaluation_attempt_count exceeds "
            "inference_limit * request_count"
        )
    return normalized


def _load_v7_run(data: bytes, path: Path) -> dict[str, Any]:
    records = acceptable._jsonl(data, str(path))
    cases: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(records, 1):
        case = _validate_v7_record(raw, f"{path}:{index}")
        if case["id"] in cases:
            raise ValueError(f"{path}: duplicate id {case['id']!r}")
        cases[case["id"]] = case
    first = next(iter(cases.values()))
    consistency_fields = (
        "backend",
        "backend_version",
        "converter_backend",
        "source_ref",
        "resource",
        "producer",
        "quality_policy",
        "top_k",
        "corpus",
        "conversion_path",
        "boundary_policy",
    )
    for case in cases.values():
        for field in consistency_fields:
            if case[field] != first[field]:
                raise ValueError(f"{path}: inconsistent {field} within run")
        if case["measurement"]["warmups"] != first["measurement"]["warmups"]:
            raise ValueError(f"{path}: inconsistent warmups within run")
        if case["measurement"]["iterations"] != first["measurement"]["iterations"]:
            raise ValueError(f"{path}: inconsistent iterations within run")
        if case["context"]["source"] != first["context"]["source"]:
            raise ValueError(f"{path}: inconsistent context.source within run")
        if (case["fixed_boundary"] is None) != (first["fixed_boundary"] is None):
            raise ValueError(f"{path}: inconsistent fixed_boundary presence within run")
        if (
            case["fixed_boundary"] is not None
            and case["fixed_boundary"]["source"]
            != first["fixed_boundary"]["source"]
        ):
            raise ValueError(f"{path}: inconsistent fixed_boundary.source within run")
    if first["corpus"]["cases"] != len(cases):
        raise ValueError(f"{path}: corpus.cases does not match result count")
    if first["context"]["source"]["cases"] != len(cases):
        raise ValueError(f"{path}: context.source.cases does not match result count")
    if (
        first["fixed_boundary"] is not None
        and first["fixed_boundary"]["source"]["cases"] != len(cases)
    ):
        raise ValueError(
            f"{path}: fixed_boundary.source.cases does not match result count"
        )
    evaluation_attempt_count = sum(
        case["zenzai_execution"]["evaluation_attempt_count"]
        for case in cases.values()
    )
    if evaluation_attempt_count == 0:
        raise ValueError(
            f"{path}: enabled Zenzai run has no model evaluation attempt"
        )
    return {
        "path": path,
        "schema": INPUT_SCHEMA_V7,
        "backend": first["backend"],
        "backend_version": first["backend_version"],
        "converter_backend": first["converter_backend"],
        "source_ref": first["source_ref"],
        "resource": first["resource"],
        "producer": first["producer"],
        "quality_policy": first["quality_policy"],
        "conversion_path": first["conversion_path"],
        "boundary_policy": first["boundary_policy"],
        "top_k": first["top_k"],
        "corpus": first["corpus"],
        "warmups": first["measurement"]["warmups"],
        "iterations": first["measurement"]["iterations"],
        "context_source": first["context"]["source"],
        "fixed_boundary_source": (
            None
            if first["fixed_boundary"] is None
            else first["fixed_boundary"]["source"]
        ),
        "zenzai_execution": _execution_summary(cases.values()),
        "cases": cases,
    }


def _load_context_sidecar(
    data: bytes,
    path: Path,
    *,
    targets: list[dict[str, Any]],
    reviewed_row_hashes: dict[str, str],
) -> dict[str, Any]:
    raw_records = acceptable._jsonl(data, str(path))
    expected_ids = [target["id"] for target in targets]
    records: dict[str, dict[str, Any]] = {}
    observed_ids: list[str] = []
    fields = {
        "schema",
        "id",
        "source_content_sha256",
        "left_context",
        "left_context_sha256",
    }
    for index, raw in enumerate(raw_records, 1):
        where = f"{path}:{index}"
        record = acceptable._exact_object(raw, fields, where)
        if record["schema"] != blind.CONTEXT_SCHEMA:
            raise ValueError(f"{where}.schema must be {blind.CONTEXT_SCHEMA!r}")
        case_id = blind._identifier(record["id"], f"{where}.id")
        if case_id in records:
            raise ValueError(f"{path}: duplicate id {case_id!r}")
        left_context = blind._text(
            record["left_context"],
            f"{where}.left_context",
            maximum_code_points=blind.MAX_LEFT_CONTEXT_CODE_POINTS,
            allow_empty=True,
        )
        digest = acceptable._hash(
            record["left_context_sha256"], f"{where}.left_context_sha256"
        )
        if digest != acceptable._sha256(left_context.encode("utf-8")):
            raise ValueError(f"{where}.left_context_sha256 does not match exact UTF-8")
        source_digest = acceptable._hash(
            record["source_content_sha256"], f"{where}.source_content_sha256"
        )
        expected_source = reviewed_row_hashes.get(case_id)
        if expected_source is None:
            raise ValueError(f"{where}.id is absent from acceptable generation")
        if source_digest != expected_source:
            raise ValueError(
                f"{where}.source_content_sha256 does not match reviewed source.row_sha256"
            )
        observed_ids.append(case_id)
        records[case_id] = {
            "schema": blind.CONTEXT_SCHEMA,
            "id": case_id,
            "source_content_sha256": source_digest,
            "left_context": left_context,
            "left_context_sha256": digest,
        }
    if observed_ids != expected_ids:
        raise ValueError("context sidecar IDs/order do not match acceptable targets")
    return {
        "identity": {
            "schema": blind.CONTEXT_SCHEMA,
            "sha256": acceptable._sha256(data),
            "cases": len(records),
        },
        "records": records,
    }


def _load_fixed_boundary_sidecar(
    data: bytes,
    path: Path,
    *,
    raw_mozc: bytes,
    raw_mozc_path: Path,
    targets: list[dict[str, Any]],
) -> dict[str, Any]:
    rederived = fixed_prepare.prepare_sidecar_bytes(raw_mozc, str(raw_mozc_path))
    if data != rederived:
        raise ValueError(
            "Mozc fixed-boundary sidecar is not the canonical derivation of "
            "the exact raw Mozc bytes"
        )
    raw_records = acceptable._jsonl(data, str(path))
    expected_ids = [target["id"] for target in targets]
    observed_ids = [record["id"] for record in raw_records]
    if observed_ids != expected_ids:
        raise ValueError("Mozc fixed-boundary sidecar IDs/order do not match targets")
    records: dict[str, dict[str, Any]] = {}
    for index, (record, target) in enumerate(zip(raw_records, targets), 1):
        where = f"{path}:{index}"
        normalized = acceptable._exact_object(
            record, fixed_prepare.SIDECAR_FIELDS, where
        )
        origin = acceptable._exact_object(
            normalized["origin"], fixed_prepare.ORIGIN_FIELDS, f"{where}.origin"
        )
        if normalized["reading"] != target["reading"]:
            raise ValueError(f"{where}.reading does not match acceptable target")
        records[str(normalized["id"])] = {
            "schema": fixed_prepare.SIDECAR_SCHEMA,
            "id": str(normalized["id"]),
            "reading": str(normalized["reading"]),
            "reading_sha256": acceptable._hash(
                normalized["reading_sha256"], f"{where}.reading_sha256"
            ),
            "consuming_count": acceptable._positive_int(
                normalized["consuming_count"], f"{where}.consuming_count"
            ),
            "origin": dict(origin),
        }
    first_origin = records[expected_ids[0]]["origin"]
    return {
        "identity": {
            "schema": fixed_prepare.SIDECAR_SCHEMA,
            "sha256": acceptable._sha256(data),
            "cases": len(records),
        },
        "origin": first_origin,
        "records": records,
    }


def _reviewed_row_hashes(
    reviewed_data: bytes, targets: list[dict[str, Any]]
) -> dict[str, str]:
    records = acceptable._jsonl(reviewed_data, "bound reviewed paths")
    expected_ids = [target["id"] for target in targets]
    observed_ids = [record.get("id") for record in records]
    if observed_ids != expected_ids:
        raise ValueError("reviewed-path IDs/order do not match acceptable targets")
    return {
        str(record["id"]): acceptable._hash(
            acceptable._exact_object(
                record["source"],
                {
                    "queue_sha256",
                    "corpus_sha256",
                    "row_sha256",
                    "reading",
                    "annotation_reading",
                    "reading_unit",
                    "annotation_reading_unit",
                    "surface_unit",
                    "surface_references",
                },
                f"bound reviewed paths:{index}.source",
            )["row_sha256"],
            f"bound reviewed paths:{index}.source.row_sha256",
        )
        for index, record in enumerate(records, 1)
    }


def _validate_pair(
    targets: list[dict[str, Any]],
    manifest: dict[str, Any],
    empty: dict[str, Any],
    contextual: dict[str, Any],
    empty_sidecar: dict[str, Any],
    contextual_sidecar: dict[str, Any],
) -> None:
    expected_ids = [target["id"] for target in targets]
    expected_corpus = {
        "sha256": manifest["bindings"]["probe_input"]["sha256"],
        "cases": manifest["bindings"]["probe_input"]["cases"],
    }
    expected_boundary_policy = {
        field: V7_BOUNDARY_POLICIES["isolated_dictionary"][field]
        for field in V7_BOUNDARY_POLICY_FIELDS
    }
    for label, run, sidecar in (
        ("isolated empty v7", empty, empty_sidecar),
        ("isolated left-context v7", contextual, contextual_sidecar),
    ):
        if list(run["cases"]) != expected_ids:
            raise ValueError(f"{label} result IDs/order do not match targets")
        if run["corpus"] != expected_corpus:
            raise ValueError(f"{label} corpus identity does not match probe input")
        if run["converter_backend"] != "hazkey":
            raise ValueError(f"{label} must use converter_backend='hazkey'")
        if run["quality_policy"]["learning"] is not False:
            raise ValueError(f"{label} learning policy must be false")
        if run["quality_policy"]["context"] != CONTEXT_POLICY:
            raise ValueError(f"{label} context policy is invalid")
        if run["boundary_policy"] != expected_boundary_policy:
            raise ValueError(f"{label} must use isolated_dictionary")
        if run["context_source"] != sidecar["identity"]:
            raise ValueError(f"{label} context.source does not match its sidecar")
    strict_fields = (
        "schema",
        "backend",
        "backend_version",
        "converter_backend",
        "source_ref",
        "resource",
        "producer",
        "quality_policy",
        "conversion_path",
        "boundary_policy",
        "top_k",
        "corpus",
        "warmups",
        "iterations",
    )
    for field in strict_fields:
        if empty[field] != contextual[field]:
            if field == "quality_policy":
                raise ValueError(
                    "paired isolated run Zenzai model/runtime policy differs"
                )
            raise ValueError(f"paired isolated run metadata {field} differs")

    natural_count = 0
    for target in targets:
        case_id = target["id"]
        empty_case = empty["cases"][case_id]
        contextual_case = contextual["cases"][case_id]
        empty_sidecar_case = empty_sidecar["records"][case_id]
        contextual_sidecar_case = contextual_sidecar["records"][case_id]
        if (
            empty_sidecar_case["source_content_sha256"]
            != contextual_sidecar_case["source_content_sha256"]
        ):
            raise ValueError(
                f"isolated sidecars case {case_id!r} source_content_sha256 differs"
            )
        expected_span = {
            "start": 0,
            "count": len(target["reading"]),
            "unit": quality.COMPOSITION_ELEMENT_UNIT,
        }
        for label, case, sidecar_case, source_identity in (
            (
                "isolated empty v7",
                empty_case,
                empty_sidecar_case,
                empty_sidecar["identity"],
            ),
            (
                "isolated left-context v7",
                contextual_case,
                contextual_sidecar_case,
                contextual_sidecar["identity"],
            ),
        ):
            if case["reading"] != target["reading"]:
                raise ValueError(f"{label} case {case_id!r} reading mismatch")
            if case["category"] != target["category"]:
                raise ValueError(f"{label} case {case_id!r} category mismatch")
            if case["composition_span"] != expected_span:
                raise ValueError(f"{label} case {case_id!r} composition_span mismatch")
            raw_left = sidecar_case["left_context"]
            expected_mode = "empty" if not raw_left else "natural_left"
            expected_context = {
                "mode": expected_mode,
                "left_context_sha256": sidecar_case["left_context_sha256"],
                "left_context_code_point_count": len(raw_left),
                "left_context_utf8_byte_count": len(raw_left.encode("utf-8")),
                "source_content_sha256": sidecar_case["source_content_sha256"],
                "source": source_identity,
            }
            if case["context"] != expected_context:
                raise ValueError(
                    f"{label} case {case_id!r} context does not match sidecar"
                )
        if empty_case["context"]["mode"] != "empty":
            raise ValueError(
                f"isolated empty v7 case {case_id!r} must have empty context"
            )
        natural_count += contextual_case["context"]["mode"] == "natural_left"
    if natural_count == 0:
        raise ValueError("isolated left-context v7 must contain a natural_left case")


def _validate_common_hazkey_v7_acquisition(
    runs: Sequence[tuple[str, dict[str, Any]]],
) -> None:
    baseline_label, baseline = runs[0]
    for label, run in runs[1:]:
        for field in COMMON_HAZKEY_V7_ACQUISITION_FIELDS:
            if run[field] != baseline[field]:
                raise ValueError(
                    "Hazkey v7 common acquisition "
                    f"{field} differs between {baseline_label} and {label}"
                )


def _validate_common_context_source(
    runs: Sequence[tuple[str, dict[str, Any]]],
    expected: dict[str, Any],
    role: str,
) -> None:
    for label, run in runs:
        if run["context_source"] != expected:
            raise ValueError(
                f"Hazkey v7 {role} context_source differs for {label}"
            )


def _validate_native_pair(
    targets: list[dict[str, Any]],
    manifest: dict[str, Any],
    empty: dict[str, Any],
    contextual: dict[str, Any],
    empty_sidecar: dict[str, Any],
    contextual_sidecar: dict[str, Any],
) -> None:
    expected_ids = [target["id"] for target in targets]
    expected_corpus = {
        "sha256": manifest["bindings"]["probe_input"]["sha256"],
        "cases": manifest["bindings"]["probe_input"]["cases"],
    }
    expected_boundary_policy = {
        field: V7_BOUNDARY_POLICIES["native_zenzai_first_clause"][field]
        for field in V7_BOUNDARY_POLICY_FIELDS
    }
    for label, run, sidecar in (
        ("native empty v7", empty, empty_sidecar),
        ("native left-context v7", contextual, contextual_sidecar),
    ):
        if list(run["cases"]) != expected_ids:
            raise ValueError(f"{label} result IDs/order do not match targets")
        if run["corpus"] != expected_corpus:
            raise ValueError(f"{label} corpus identity does not match probe input")
        if run["converter_backend"] != "hazkey":
            raise ValueError(f"{label} must use converter_backend='hazkey'")
        if run["quality_policy"]["context"] != CONTEXT_POLICY:
            raise ValueError(f"{label} context policy is invalid")
        if run["boundary_policy"] != expected_boundary_policy:
            raise ValueError(f"{label} must use native_zenzai_first_clause")
        if run["context_source"] != sidecar["identity"]:
            raise ValueError(f"{label} context.source does not match its sidecar")
    strict_fields = (
        "schema",
        "backend",
        "backend_version",
        "converter_backend",
        "source_ref",
        "resource",
        "producer",
        "quality_policy",
        "conversion_path",
        "boundary_policy",
        "top_k",
        "corpus",
        "warmups",
        "iterations",
    )
    for field in strict_fields:
        if empty[field] != contextual[field]:
            raise ValueError(f"paired native run metadata {field} differs")

    natural_count = 0
    for target in targets:
        case_id = target["id"]
        empty_case = empty["cases"][case_id]
        contextual_case = contextual["cases"][case_id]
        empty_sidecar_case = empty_sidecar["records"][case_id]
        contextual_sidecar_case = contextual_sidecar["records"][case_id]
        if (
            empty_sidecar_case["source_content_sha256"]
            != contextual_sidecar_case["source_content_sha256"]
        ):
            raise ValueError(
                f"native sidecars case {case_id!r} source_content_sha256 differs"
            )
        expected_span = {
            "start": 0,
            "count": len(target["reading"]),
            "unit": quality.COMPOSITION_ELEMENT_UNIT,
        }
        for label, case, sidecar_case, source_identity in (
            (
                "native empty v7",
                empty_case,
                empty_sidecar_case,
                empty_sidecar["identity"],
            ),
            (
                "native left-context v7",
                contextual_case,
                contextual_sidecar_case,
                contextual_sidecar["identity"],
            ),
        ):
            if case["reading"] != target["reading"]:
                raise ValueError(f"{label} case {case_id!r} reading mismatch")
            if case["category"] != target["category"]:
                raise ValueError(f"{label} case {case_id!r} category mismatch")
            if case["composition_span"] != expected_span:
                raise ValueError(f"{label} case {case_id!r} composition_span mismatch")
            raw_left = sidecar_case["left_context"]
            expected_mode = "empty" if not raw_left else "natural_left"
            expected_context = {
                "mode": expected_mode,
                "left_context_sha256": sidecar_case["left_context_sha256"],
                "left_context_code_point_count": len(raw_left),
                "left_context_utf8_byte_count": len(raw_left.encode("utf-8")),
                "source_content_sha256": sidecar_case["source_content_sha256"],
                "source": source_identity,
            }
            if case["context"] != expected_context:
                raise ValueError(f"{label} case {case_id!r} context does not match sidecar")
        if empty_case["context"]["mode"] != "empty":
            raise ValueError(f"native empty v7 case {case_id!r} must have empty context")
        natural_count += contextual_case["context"]["mode"] == "natural_left"
    if natural_count == 0:
        raise ValueError("native left-context v7 must contain a natural_left case")


def _validate_fixed_pair(
    targets: list[dict[str, Any]],
    manifest: dict[str, Any],
    raw_mozc: dict[str, Any],
    fixed_sidecar: dict[str, Any],
    empty: dict[str, Any],
    contextual: dict[str, Any],
    empty_context_sidecar: dict[str, Any],
    contextual_sidecar: dict[str, Any],
) -> None:
    expected_ids = [target["id"] for target in targets]
    expected_corpus = {
        "sha256": manifest["bindings"]["probe_input"]["sha256"],
        "cases": manifest["bindings"]["probe_input"]["cases"],
    }
    expected_policy = {
        field: V7_BOUNDARY_POLICIES["mozc_fixed"][field]
        for field in V7_BOUNDARY_POLICY_FIELDS
    }
    if list(raw_mozc["cases"]) != expected_ids:
        raise ValueError("raw Mozc result IDs/order do not match targets")
    if raw_mozc["corpus"] != expected_corpus:
        raise ValueError("raw Mozc corpus identity does not match probe input")
    if fixed_sidecar["origin"] != {
        "schema": fixed_prepare.INPUT_SCHEMA_V6,
        "sha256": fixed_sidecar["origin"]["sha256"],
        "cases": len(targets),
        "converter_backend": "mozc",
        "conversion_path": quality.CONVERSION_PATH,
    }:
        raise ValueError("fixed-boundary sidecar Mozc origin is invalid")

    for label, run, context_sidecar in (
        ("Mozc-fixed empty v7", empty, empty_context_sidecar),
        ("Mozc-fixed left-context v7", contextual, contextual_sidecar),
    ):
        if list(run["cases"]) != expected_ids:
            raise ValueError(f"{label} result IDs/order do not match targets")
        if run["corpus"] != expected_corpus:
            raise ValueError(f"{label} corpus identity does not match probe input")
        if run["converter_backend"] != "hazkey":
            raise ValueError(f"{label} must use converter_backend='hazkey'")
        if run["quality_policy"]["context"] != CONTEXT_POLICY:
            raise ValueError(f"{label} context policy is invalid")
        if run["boundary_policy"] != expected_policy:
            raise ValueError(f"{label} must use mozc_fixed")
        if run["context_source"] != context_sidecar["identity"]:
            raise ValueError(f"{label} context.source does not match its sidecar")
        if run["fixed_boundary_source"] != fixed_sidecar["identity"]:
            raise ValueError(
                f"{label} fixed_boundary.source does not match exact sidecar bytes"
            )

    paired_fields = (
        "schema",
        "backend",
        "backend_version",
        "converter_backend",
        "source_ref",
        "resource",
        "producer",
        "quality_policy",
        "conversion_path",
        "boundary_policy",
        "fixed_boundary_source",
        "top_k",
        "corpus",
        "warmups",
        "iterations",
    )
    for field in paired_fields:
        if empty[field] != contextual[field]:
            raise ValueError(f"paired Mozc-fixed run metadata {field} differs")
    for field in (
        "backend_version",
        "source_ref",
        "producer",
        "top_k",
        "corpus",
        "warmups",
        "iterations",
    ):
        if raw_mozc[field] != empty[field]:
            raise ValueError(f"raw Mozc/fixed acquisition metadata {field} differs")

    natural_count = 0
    for target in targets:
        case_id = target["id"]
        raw_case = raw_mozc["cases"][case_id]
        fixed_record = fixed_sidecar["records"][case_id]
        if not raw_case["candidates"]:
            raise ValueError(f"raw Mozc case {case_id!r} has no Top-1 boundary")
        if raw_case["candidates"][0]["consuming_count"] != fixed_record[
            "consuming_count"
        ]:
            raise ValueError(f"case {case_id!r} sidecar count differs from raw Mozc")
        expected_span = {
            "start": 0,
            "count": len(target["reading"]),
            "unit": quality.COMPOSITION_ELEMENT_UNIT,
        }
        if raw_case["reading"] != target["reading"]:
            raise ValueError(f"raw Mozc case {case_id!r} reading mismatch")
        if raw_case["category"] != target["category"]:
            raise ValueError(f"raw Mozc case {case_id!r} category mismatch")
        if raw_case["composition_span"] != expected_span:
            raise ValueError(f"raw Mozc case {case_id!r} composition_span mismatch")

        expected_fixed = {
            "reading_sha256": fixed_record["reading_sha256"],
            "consuming_count": fixed_record["consuming_count"],
            "source": fixed_sidecar["identity"],
        }
        for label, run, sidecar, require_empty in (
            ("Mozc-fixed empty v7", empty, empty_context_sidecar, True),
            (
                "Mozc-fixed left-context v7",
                contextual,
                contextual_sidecar,
                False,
            ),
        ):
            case = run["cases"][case_id]
            sidecar_case = sidecar["records"][case_id]
            if case["reading"] != target["reading"]:
                raise ValueError(f"{label} case {case_id!r} reading mismatch")
            if case["category"] != target["category"]:
                raise ValueError(f"{label} case {case_id!r} category mismatch")
            if case["composition_span"] != expected_span:
                raise ValueError(f"{label} case {case_id!r} composition_span mismatch")
            if case["fixed_boundary"] != expected_fixed:
                raise ValueError(
                    f"{label} case {case_id!r} fixed_boundary does not match sidecar"
                )
            raw_left = sidecar_case["left_context"]
            expected_mode = "empty" if not raw_left else "natural_left"
            expected_context = {
                "mode": expected_mode,
                "left_context_sha256": sidecar_case["left_context_sha256"],
                "left_context_code_point_count": len(raw_left),
                "left_context_utf8_byte_count": len(raw_left.encode("utf-8")),
                "source_content_sha256": sidecar_case["source_content_sha256"],
                "source": sidecar["identity"],
            }
            if case["context"] != expected_context:
                raise ValueError(
                    f"{label} case {case_id!r} context does not match sidecar"
                )
            if require_empty and case["context"]["mode"] != "empty":
                raise ValueError(
                    f"Mozc-fixed empty v7 case {case_id!r} must have empty context"
                )
        natural_count += contextual["cases"][case_id]["context"]["mode"] == "natural_left"
    if natural_count == 0:
        raise ValueError("Mozc-fixed left-context v7 must contain a natural_left case")


def _case_samples(case: dict[str, Any]) -> list[float]:
    if "measurement" in case:
        return list(case["measurement"]["latency_ms"]["samples"])
    return list(case["samples"])


def _score(candidate: dict[str, Any] | None) -> dict[str, Any]:
    if candidate is None or candidate["zenzai_score"] is None:
        return {
            "available": False,
            "raw": None,
            "token_count": None,
            "scope": None,
            "per_token": None,
        }
    raw = float(candidate["zenzai_score"])
    tokens = int(candidate["zenzai_score_token_count"])
    return {
        "available": True,
        "raw": raw,
        "token_count": tokens,
        "scope": candidate["zenzai_score_scope"],
        "per_token": raw / tokens,
    }


def _memory_snapshot(case: dict[str, Any]) -> dict[str, dict[str, int | None]]:
    rss = case["measurement"]["rss"]
    result: dict[str, dict[str, int | None]] = {}
    for name, (before_key, after_key) in MEMORY_MEASURES.items():
        before = rss.get(before_key)
        after = rss.get(after_key)
        result[name] = {
            "after": after,
            "delta_after_minus_before": (
                after - before if before is not None and after is not None else None
            ),
        }
    return result


def _boundary_diagnostic(
    candidates: list[dict[str, Any]],
    target: dict[str, Any],
    *,
    explicit_count: int | None = None,
) -> dict[str, Any]:
    acceptable_counts = sorted(
        {span["count"] for span in target["acceptable_first_spans"]}
    )
    predicted_count = (
        explicit_count
        if explicit_count is not None
        else candidates[0]["consuming_count"] if candidates else None
    )
    if predicted_count is None:
        return {
            "top1_consuming_count": None,
            "acceptable_consuming_counts": acceptable_counts,
            "nearest_acceptable_consuming_count": None,
            "nearest_acceptable_signed_delta": None,
            "minimum_absolute_delta": None,
            "classification": "missing",
            "count_source": "explicit_fixed_boundary" if explicit_count is not None else "candidate_top1",
        }
    nearest = min(
        acceptable_counts,
        key=lambda accepted: (abs(predicted_count - accepted), accepted),
    )
    signed_delta = predicted_count - nearest
    return {
        "top1_consuming_count": predicted_count,
        "acceptable_consuming_counts": acceptable_counts,
        "nearest_acceptable_consuming_count": nearest,
        "nearest_acceptable_signed_delta": signed_delta,
        "minimum_absolute_delta": abs(signed_delta),
        "classification": (
            "match"
            if signed_delta == 0
            else "too_long" if signed_delta > 0 else "too_short"
        ),
        "count_source": (
            "explicit_fixed_boundary"
            if explicit_count is not None
            else "candidate_top1"
        ),
    }


def _build_case(
    target: dict[str, Any],
    empty_case: dict[str, Any],
    contextual_case: dict[str, Any],
    *,
    systems: tuple[str, str] = ISOLATED_SYSTEMS,
) -> dict[str, Any]:
    baseline_system, candidate_system = systems
    empty_candidates = empty_case["candidates"]
    contextual_candidates = contextual_case["candidates"]
    empty_score = _score(empty_candidates[0] if empty_candidates else None)
    contextual_score = _score(contextual_candidates[0] if contextual_candidates else None)
    per_token_score_comparable = (
        empty_score["available"]
        and contextual_score["available"]
        and empty_score["scope"] == contextual_score["scope"]
    )
    raw_score_comparable = (
        per_token_score_comparable
        and empty_score["token_count"] == contextual_score["token_count"]
    )
    empty_latency = float(statistics.median(_case_samples(empty_case)))
    contextual_latency = float(statistics.median(_case_samples(contextual_case)))
    return {
        "id": target["id"],
        "category": target["category"],
        "reading": target["reading"],
        "context": dict(contextual_case["context"]),
        "gold_outcomes": {
            "systems": {
                baseline_system: quality._gold_outcome(empty_candidates, target),
                candidate_system: quality._gold_outcome(
                    contextual_candidates, target
                ),
            }
        },
        "boundary_diagnostics": {
            "systems": {
                baseline_system: _boundary_diagnostic(empty_candidates, target),
                candidate_system: _boundary_diagnostic(
                    contextual_candidates, target
                ),
            }
        },
        "top1_score": {
            baseline_system: empty_score,
            candidate_system: contextual_score,
            "raw_delta_comparable_same_scope_and_token_count": (
                raw_score_comparable
            ),
            "per_token_delta_comparable_same_scope": per_token_score_comparable,
            "left_context_minus_empty_raw": (
                contextual_score["raw"] - empty_score["raw"]
                if raw_score_comparable
                else None
            ),
            "left_context_minus_empty_per_token": (
                contextual_score["per_token"] - empty_score["per_token"]
                if per_token_score_comparable
                else None
            ),
        },
        "zenzai_execution": {
            baseline_system: empty_case.get("zenzai_execution"),
            candidate_system: contextual_case.get("zenzai_execution"),
        },
        "latency_ms": {
            "per_case_statistic": "median-of-recorded-iterations",
            baseline_system: empty_latency,
            candidate_system: contextual_latency,
            "left_context_minus_empty": contextual_latency - empty_latency,
            "left_context_over_empty_ratio": (
                contextual_latency / empty_latency if empty_latency > 0 else None
            ),
        },
        "memory_kib": {
            baseline_system: _memory_snapshot(empty_case),
            candidate_system: _memory_snapshot(contextual_case),
        },
    }


def _fixed_gold_outcome(
    case: dict[str, Any], target: dict[str, Any]
) -> dict[str, Any]:
    outcome = quality._gold_outcome(case["candidates"], target)
    fixed_count = case["fixed_boundary"]["consuming_count"]
    boundary_hit = any(
        span["count"] == fixed_count for span in target["acceptable_first_spans"]
    )
    outcome["first_segment_boundary"] = {
        "at1": boundary_hit,
        "at_k": boundary_hit,
        "first_hit_rank": 1 if boundary_hit else None,
        "reciprocal_rank": 1.0 if boundary_hit else 0.0,
    }
    fully_aligned = target["surface_evaluation_status"] == "fully_aligned"
    e2e = outcome["end_to_end"]
    outcome["conditional_surface_given_acceptable_first_segment_boundary"] = {
        "eligible": fully_aligned,
        "at1_comparable": fully_aligned and boundary_hit,
        "at1_hit": bool(e2e["at1"]) if fully_aligned and boundary_hit else None,
        "at_k_comparable": fully_aligned and boundary_hit,
        "at_k_hit": bool(e2e["at_k"]) if fully_aligned and boundary_hit else None,
    }
    return outcome


def _build_fixed_case(
    target: dict[str, Any],
    empty_case: dict[str, Any],
    contextual_case: dict[str, Any],
    raw_mozc_case: dict[str, Any],
    fixed_count: int,
) -> dict[str, Any]:
    result = _build_case(
        target,
        empty_case,
        contextual_case,
        systems=FIXED_SYSTEMS,
    )
    result["gold_outcomes"]["systems"] = {
        FIXED_EMPTY_SYSTEM: _fixed_gold_outcome(empty_case, target),
        FIXED_LEFT_CONTEXT_SYSTEM: _fixed_gold_outcome(contextual_case, target),
    }
    raw_fixed_candidates = [
        candidate
        for candidate in raw_mozc_case["candidates"]
        if candidate["consuming_count"] == fixed_count
    ]
    result["gold_outcomes"]["systems"][MOZC_AT_FIXED_BOUNDARY_SYSTEM] = (
        _fixed_gold_outcome(
            {
                "candidates": raw_fixed_candidates,
                "fixed_boundary": {"consuming_count": fixed_count},
            },
            target,
        )
    )
    for system in FIXED_SYSTEMS + (MOZC_AT_FIXED_BOUNDARY_SYSTEM,):
        result["boundary_diagnostics"]["systems"][system] = (
            _boundary_diagnostic([], target, explicit_count=fixed_count)
        )
    result["mozc_at_fixed_boundary_candidate_count"] = len(raw_fixed_candidates)
    return result


def _transition(values: Iterable[tuple[bool, bool]]) -> dict[str, int]:
    pairs = list(values)
    rescued = sum(not baseline and candidate for baseline, candidate in pairs)
    regressed = sum(baseline and not candidate for baseline, candidate in pairs)
    unchanged_hit = sum(baseline and candidate for baseline, candidate in pairs)
    unchanged_miss = sum(not baseline and not candidate for baseline, candidate in pairs)
    return {
        "comparable_cases": len(pairs),
        "rescued": rescued,
        "regressed": regressed,
        "net": rescued - regressed,
        "unchanged": unchanged_hit + unchanged_miss,
        "unchanged_hit": unchanged_hit,
        "unchanged_miss": unchanged_miss,
        "miss": unchanged_miss,
    }


def _pairwise(
    cases: list[dict[str, Any]], systems: tuple[str, str]
) -> dict[str, Any]:
    baseline_system, candidate_system = systems

    def outcomes(case: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        observed = case["gold_outcomes"]["systems"]
        return observed[baseline_system], observed[candidate_system]

    boundary: dict[str, Any] = {}
    e2e: dict[str, Any] = {}
    conditional: dict[str, Any] = {}
    comparable_keys = {
        "at1": ("at1_comparable", "at1_hit"),
        "at_k": ("at_k_comparable", "at_k_hit"),
    }
    for rank in ("at1", "at_k"):
        boundary[rank] = _transition(
            (
                bool(left["first_segment_boundary"][rank]),
                bool(right["first_segment_boundary"][rank]),
            )
            for left, right in map(outcomes, cases)
        )
        e2e[rank] = _transition(
            (
                bool(left["end_to_end"][rank]),
                bool(right["end_to_end"][rank]),
            )
            for left, right in map(outcomes, cases)
            if left["end_to_end"][rank] is not None
            and right["end_to_end"][rank] is not None
        )
        comparable_key, hit_key = comparable_keys[rank]
        conditional[rank] = _transition(
            (
                bool(
                    left[
                        "conditional_surface_given_acceptable_first_segment_boundary"
                    ][hit_key]
                ),
                bool(
                    right[
                        "conditional_surface_given_acceptable_first_segment_boundary"
                    ][hit_key]
                ),
            )
            for left, right in map(outcomes, cases)
            if left[
                "conditional_surface_given_acceptable_first_segment_boundary"
            ][comparable_key]
            and right[
                "conditional_surface_given_acceptable_first_segment_boundary"
            ][comparable_key]
        )
    return {
        "baseline": baseline_system,
        "candidate": candidate_system,
        "first_segment_boundary": boundary,
        "conditional_surface_on_mutually_acceptable_boundaries": conditional,
        "end_to_end": e2e,
    }


def _pairwise_matrix(
    cases: list[dict[str, Any]], systems: Sequence[str]
) -> dict[str, dict[str, Any]]:
    return {
        baseline: {
            candidate: _pairwise(cases, (baseline, candidate))
            for candidate in systems
            if candidate != baseline
        }
        for baseline in systems
    }


def _combine_outcomes_by_case(
    case_groups: Sequence[tuple[str, list[dict[str, Any]]]],
) -> list[dict[str, Any]]:
    expected_ids = [case["id"] for case in case_groups[0][1]]
    combined = [
        {
            "id": source["id"],
            "category": source["category"],
            "gold_outcomes": {"systems": {}},
            "boundary_diagnostics": {"systems": {}},
        }
        for source in case_groups[0][1]
    ]
    for system, cases in case_groups:
        if [case["id"] for case in cases] != expected_ids:
            raise AssertionError("cross-policy case IDs/order differ")
        for destination, source in zip(combined, cases, strict=True):
            if destination["category"] != source["category"]:
                raise AssertionError("cross-policy case categories differ")
            destination["gold_outcomes"]["systems"][system] = source[
                "gold_outcomes"
            ]["systems"][system]
            destination["boundary_diagnostics"]["systems"][system] = source[
                "boundary_diagnostics"
            ]["systems"][system]
    return combined


def _score_summary(
    cases: list[dict[str, Any]], systems: tuple[str, str]
) -> dict[str, Any]:
    raw_comparable = [
        case
        for case in cases
        if case["top1_score"][
            "raw_delta_comparable_same_scope_and_token_count"
        ]
    ]
    per_token_comparable = [
        case
        for case in cases
        if case["top1_score"]["per_token_delta_comparable_same_scope"]
    ]
    return {
        "comparison_unit": "observed Top-1 Zenzai score",
        "cases": len(cases),
        "delta_comparability": {
            "raw": {
                "rule": "identical score scope and scored token count",
                "comparable_cases": len(raw_comparable),
                "noncomparable_cases": len(cases) - len(raw_comparable),
            },
            "per_token": {
                "rule": "identical score scope",
                "comparable_cases": len(per_token_comparable),
                "noncomparable_cases": len(cases) - len(per_token_comparable),
            },
        },
        "scope_counts": {
            system: dict(
                sorted(
                    Counter(
                        case["top1_score"][system]["scope"]
                        for case in cases
                        if case["top1_score"][system]["available"]
                    ).items()
                )
            )
            for system in systems
        },
        "raw": {
            system: _distribution(
                [
                    case["top1_score"][system]["raw"]
                    for case in cases
                    if case["top1_score"][system]["available"]
                ]
            )
            for system in systems
        },
        "per_token": {
            system: _distribution(
                [
                    case["top1_score"][system]["per_token"]
                    for case in cases
                    if case["top1_score"][system]["available"]
                ]
            )
            for system in systems
        },
        "left_context_minus_empty_raw": _distribution(
            [
                case["top1_score"]["left_context_minus_empty_raw"]
                for case in raw_comparable
            ]
        ),
        "left_context_minus_empty_per_token": _distribution(
            [
                case["top1_score"]["left_context_minus_empty_per_token"]
                for case in per_token_comparable
            ]
        ),
    }


def _latency_summary(
    cases: list[dict[str, Any]], systems: tuple[str, str]
) -> dict[str, Any]:
    baseline_system, candidate_system = systems
    ratios = [
        case["latency_ms"]["left_context_over_empty_ratio"]
        for case in cases
        if case["latency_ms"]["left_context_over_empty_ratio"] is not None
    ]
    return {
        "per_case_statistic": "median-of-recorded-iterations",
        baseline_system: _distribution(
            [case["latency_ms"][baseline_system] for case in cases]
        ),
        candidate_system: _distribution(
            [case["latency_ms"][candidate_system] for case in cases]
        ),
        "left_context_minus_empty_ms": _distribution(
            [case["latency_ms"]["left_context_minus_empty"] for case in cases]
        ),
        "left_context_over_empty_ratio": _distribution(ratios),
    }


def _memory_summary(
    cases: list[dict[str, Any]], systems: tuple[str, str]
) -> dict[str, Any]:
    return {
        "unit": "KiB",
        "acquisition_semantics": (
            "sequential separate ABProbe processes; after and in-process delta "
            "distributions are diagnostic and are not randomized paired memory effects"
        ),
        "systems": {
            system: {
                measure: {
                    statistic: _distribution(
                        [
                            case["memory_kib"][system][measure][statistic]
                            for case in cases
                            if case["memory_kib"][system][measure][statistic]
                            is not None
                        ]
                    )
                    for statistic in ("after", "delta_after_minus_before")
                }
                for measure in MEMORY_MEASURES
            }
            for system in systems
        },
    }


def _boundary_diagnostic_summary(
    cases: list[dict[str, Any]], systems: Sequence[str]
) -> dict[str, Any]:
    return {
        "delta_unit": quality.COMPOSITION_ELEMENT_UNIT,
        "signed_delta_definition": (
            "top1 consuming_count minus the nearest acceptable count; nearest-count "
            "ties choose the smaller acceptable count"
        ),
        "systems": {
            system: {
                "cases": len(cases),
                "classification_counts": dict(
                    sorted(
                        Counter(
                            case["boundary_diagnostics"]["systems"][system][
                                "classification"
                            ]
                            for case in cases
                        ).items()
                    )
                ),
                "top1_consuming_count": _distribution(
                    [
                        case["boundary_diagnostics"]["systems"][system][
                            "top1_consuming_count"
                        ]
                        for case in cases
                        if case["boundary_diagnostics"]["systems"][system][
                            "top1_consuming_count"
                        ]
                        is not None
                    ]
                ),
                "nearest_acceptable_signed_delta": _distribution(
                    [
                        case["boundary_diagnostics"]["systems"][system][
                            "nearest_acceptable_signed_delta"
                        ]
                        for case in cases
                        if case["boundary_diagnostics"]["systems"][system][
                            "nearest_acceptable_signed_delta"
                        ]
                        is not None
                    ]
                ),
                "minimum_absolute_delta": _distribution(
                    [
                        case["boundary_diagnostics"]["systems"][system][
                            "minimum_absolute_delta"
                        ]
                        for case in cases
                        if case["boundary_diagnostics"]["systems"][system][
                            "minimum_absolute_delta"
                        ]
                        is not None
                    ]
                ),
            }
            for system in systems
        },
    }


def _summary(
    cases: list[dict[str, Any]], systems: tuple[str, str]
) -> dict[str, Any]:
    return {
        "cases": len(cases),
        "systems": {
            system: quality._system_metrics(cases, system) for system in systems
        },
        "pairwise_rescue_regression": _pairwise(cases, systems),
        "top1_score": _score_summary(cases, systems),
        "zenzai_execution": _comparison_execution_summary(cases, systems),
        "latency_ms": _latency_summary(cases, systems),
        "memory_kib": _memory_summary(cases, systems),
        "boundary_diagnostics": _boundary_diagnostic_summary(cases, systems),
    }


def _fixed_summary(cases: list[dict[str, Any]]) -> dict[str, Any]:
    result = _summary(cases, FIXED_SYSTEMS)
    result["systems"][MOZC_AT_FIXED_BOUNDARY_SYSTEM] = quality._system_metrics(
        cases, MOZC_AT_FIXED_BOUNDARY_SYSTEM
    )
    result["boundary_diagnostics"]["systems"][
        MOZC_AT_FIXED_BOUNDARY_SYSTEM
    ] = _boundary_diagnostic_summary(
        cases, (MOZC_AT_FIXED_BOUNDARY_SYSTEM,)
    )["systems"][MOZC_AT_FIXED_BOUNDARY_SYSTEM]
    return result


def _cases_by_category(
    cases: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for case in cases:
        grouped.setdefault(case["category"], []).append(case)
    return dict(sorted(grouped.items()))


def _cross_policy_context_summary(
    cases: list[dict[str, Any]], systems: Sequence[str]
) -> dict[str, Any]:
    return {
        "cases": len(cases),
        "systems": list(systems),
        "pairwise_matrix": _pairwise_matrix(cases, systems),
        "boundary_diagnostics": _boundary_diagnostic_summary(cases, systems),
        "by_category": {
            category: {
                "cases": len(category_cases),
                "pairwise_matrix": _pairwise_matrix(category_cases, systems),
                "boundary_diagnostics": _boundary_diagnostic_summary(
                    category_cases, systems
                ),
            }
            for category, category_cases in _cases_by_category(cases).items()
        },
    }


def _length_bucket(length: int) -> str:
    for name, minimum, maximum in LENGTH_BUCKETS:
        if length >= minimum and (maximum is None or length <= maximum):
            return name
    raise AssertionError(f"unbucketed context length {length}")


def evaluate(
    generation_manifest: Path,
    targets_path: Path,
    isolated_empty_context_sidecar_path: Path,
    isolated_empty_v7_path: Path,
    isolated_left_context_sidecar_path: Path,
    isolated_left_v7_path: Path,
    *,
    native_empty_v7_path: Path | None = None,
    native_empty_context_sidecar_path: Path | None = None,
    native_left_v7_path: Path | None = None,
    native_left_context_sidecar_path: Path | None = None,
    fixed_raw_mozc_v6_path: Path | None = None,
    fixed_boundary_sidecar_path: Path | None = None,
    fixed_empty_v7_path: Path | None = None,
    fixed_empty_context_sidecar_path: Path | None = None,
    fixed_left_v7_path: Path | None = None,
    fixed_left_context_sidecar_path: Path | None = None,
) -> dict[str, Any]:
    blobs = acceptable._capture_generation(generation_manifest)
    manifest_bytes = blobs[acceptable.compiler.MANIFEST_NAME]
    manifest, bound = acceptable._validate_manifest(blobs)
    targets_bytes = acceptable._read_regular(targets_path, "acceptable targets")
    target_binding = manifest["bindings"]["targets"]
    if (
        acceptable._sha256(targets_bytes) != target_binding["sha256"]
        or targets_bytes != bound["targets"]
    ):
        raise ValueError("supplied targets do not match the generation binding")
    targets = acceptable._validate_targets(targets_bytes, str(targets_path))
    if len(targets) != target_binding["cases"]:
        raise ValueError("target case count does not match generation binding")
    acceptable._validate_manifest_aggregates(manifest, targets)
    acceptable._validate_probe_binding(bound["probe_input"], targets)

    row_hashes = _reviewed_row_hashes(bound["reviewed_paths"], targets)
    isolated_empty_context_bytes = acceptable._read_regular(
        isolated_empty_context_sidecar_path, "isolated empty context sidecar"
    )
    isolated_left_context_bytes = acceptable._read_regular(
        isolated_left_context_sidecar_path, "isolated left context sidecar"
    )
    isolated_empty_sidecar = _load_context_sidecar(
        isolated_empty_context_bytes,
        isolated_empty_context_sidecar_path,
        targets=targets,
        reviewed_row_hashes=row_hashes,
    )
    isolated_left_sidecar = _load_context_sidecar(
        isolated_left_context_bytes,
        isolated_left_context_sidecar_path,
        targets=targets,
        reviewed_row_hashes=row_hashes,
    )
    empty_bytes = acceptable._read_regular(
        isolated_empty_v7_path, "isolated empty-context ABProbe v7"
    )
    contextual_bytes = acceptable._read_regular(
        isolated_left_v7_path, "isolated left-context ABProbe v7"
    )
    empty_run = _load_v7_run(empty_bytes, isolated_empty_v7_path)
    contextual_run = _load_v7_run(contextual_bytes, isolated_left_v7_path)
    _validate_pair(
        targets,
        manifest,
        empty_run,
        contextual_run,
        isolated_empty_sidecar,
        isolated_left_sidecar,
    )

    cases = [
        _build_case(
            target,
            empty_run["cases"][target["id"]],
            contextual_run["cases"][target["id"]],
        )
        for target in targets
    ]
    by_mode = {
        mode: [case for case in cases if case["context"]["mode"] == mode]
        for mode in ("natural_left", "empty")
    }
    by_bucket = {
        name: [
            case
            for case in cases
            if _length_bucket(case["context"]["left_context_code_point_count"])
            == name
        ]
        for name, _minimum, _maximum in LENGTH_BUCKETS
    }
    native_paths = (
        native_empty_v7_path,
        native_empty_context_sidecar_path,
        native_left_v7_path,
        native_left_context_sidecar_path,
    )
    if any(path is not None for path in native_paths) and not all(
        path is not None for path in native_paths
    ):
        raise ValueError(
            "native comparison requires all four native v7/context-sidecar inputs"
        )
    if all(path is not None for path in native_paths):
        assert native_empty_v7_path is not None
        assert native_empty_context_sidecar_path is not None
        assert native_left_v7_path is not None
        assert native_left_context_sidecar_path is not None
        native_empty_context_bytes = acceptable._read_regular(
            native_empty_context_sidecar_path, "native empty context sidecar"
        )
        native_left_context_bytes = acceptable._read_regular(
            native_left_context_sidecar_path, "native left context sidecar"
        )
        native_empty_sidecar = _load_context_sidecar(
            native_empty_context_bytes,
            native_empty_context_sidecar_path,
            targets=targets,
            reviewed_row_hashes=row_hashes,
        )
        native_left_sidecar = _load_context_sidecar(
            native_left_context_bytes,
            native_left_context_sidecar_path,
            targets=targets,
            reviewed_row_hashes=row_hashes,
        )
        native_empty_bytes = acceptable._read_regular(
            native_empty_v7_path, "native empty-context ABProbe v7"
        )
        native_left_bytes = acceptable._read_regular(
            native_left_v7_path, "native left-context ABProbe v7"
        )
        native_empty_run = _load_v7_run(native_empty_bytes, native_empty_v7_path)
        native_left_run = _load_v7_run(native_left_bytes, native_left_v7_path)
        _validate_native_pair(
            targets,
            manifest,
            native_empty_run,
            native_left_run,
            native_empty_sidecar,
            native_left_sidecar,
        )
        native_cases = [
            _build_case(
                target,
                native_empty_run["cases"][target["id"]],
                native_left_run["cases"][target["id"]],
                systems=NATIVE_SYSTEMS,
            )
            for target in targets
        ]
        native_by_mode = {
            mode: [case for case in native_cases if case["context"]["mode"] == mode]
            for mode in ("natural_left", "empty")
        }
        native_by_bucket = {
            name: [
                case
                for case in native_cases
                if _length_bucket(
                    case["context"]["left_context_code_point_count"]
                )
                == name
            ]
            for name, _minimum, _maximum in LENGTH_BUCKETS
        }
        native_comparison: dict[str, Any] = {
            "available": True,
            "comparison_axis": "native-zenzai-boundary-empty-vs-natural-left",
            "boundary_policy": native_empty_run["boundary_policy"],
            "inputs": {
                "native_empty_context_sidecar": {
                    "path": str(native_empty_context_sidecar_path),
                    **native_empty_sidecar["identity"],
                },
                "native_left_context_sidecar": {
                    "path": str(native_left_context_sidecar_path),
                    **native_left_sidecar["identity"],
                },
                "native_empty_v7": {
                    "path": str(native_empty_v7_path),
                    "sha256": acceptable._sha256(native_empty_bytes),
                },
                "native_left_v7": {
                    "path": str(native_left_v7_path),
                    "sha256": acceptable._sha256(native_left_bytes),
                },
            },
            "all_cases": _summary(native_cases, NATIVE_SYSTEMS),
            "by_context_mode": {
                "natural_left": {
                    "role": "primary_boundary_effect",
                    **_summary(native_by_mode["natural_left"], NATIVE_SYSTEMS),
                },
                "empty": {
                    "role": "negative_control",
                    **_summary(native_by_mode["empty"], NATIVE_SYSTEMS),
                },
            },
            "by_context_code_point_length": {
                name: _summary(bucket_cases, NATIVE_SYSTEMS)
                for name, bucket_cases in native_by_bucket.items()
            },
            "by_category": {
                category: _summary(category_cases, NATIVE_SYSTEMS)
                for category, category_cases in _cases_by_category(
                    native_cases
                ).items()
            },
            "cases": native_cases,
        }
    else:
        native_comparison = {
            "available": False,
            "reason": "native v7 pair and its two context sidecars were not supplied",
        }

    fixed_paths = (
        fixed_raw_mozc_v6_path,
        fixed_boundary_sidecar_path,
        fixed_empty_v7_path,
        fixed_empty_context_sidecar_path,
        fixed_left_v7_path,
        fixed_left_context_sidecar_path,
    )
    if any(path is not None for path in fixed_paths) and not all(
        path is not None for path in fixed_paths
    ):
        raise ValueError(
            "Mozc-fixed comparison requires raw Mozc v6, fixed-boundary "
            "sidecar, both fixed v7 runs, and both context sidecars"
        )
    if all(path is not None for path in fixed_paths):
        assert fixed_raw_mozc_v6_path is not None
        assert fixed_boundary_sidecar_path is not None
        assert fixed_empty_v7_path is not None
        assert fixed_empty_context_sidecar_path is not None
        assert fixed_left_v7_path is not None
        assert fixed_left_context_sidecar_path is not None
        fixed_raw_mozc_bytes = acceptable._read_regular(
            fixed_raw_mozc_v6_path, "raw Mozc ABProbe v6"
        )
        fixed_boundary_bytes = acceptable._read_regular(
            fixed_boundary_sidecar_path, "Mozc fixed-boundary sidecar"
        )
        fixed_sidecar = _load_fixed_boundary_sidecar(
            fixed_boundary_bytes,
            fixed_boundary_sidecar_path,
            raw_mozc=fixed_raw_mozc_bytes,
            raw_mozc_path=fixed_raw_mozc_v6_path,
            targets=targets,
        )
        fixed_raw_mozc_run = quality._load_v6_run(
            fixed_raw_mozc_bytes, fixed_raw_mozc_v6_path, "mozc"
        )
        fixed_empty_context_bytes = acceptable._read_regular(
            fixed_empty_context_sidecar_path,
            "Mozc-fixed empty context sidecar",
        )
        fixed_left_context_bytes = acceptable._read_regular(
            fixed_left_context_sidecar_path,
            "Mozc-fixed left context sidecar",
        )
        fixed_empty_context_sidecar = _load_context_sidecar(
            fixed_empty_context_bytes,
            fixed_empty_context_sidecar_path,
            targets=targets,
            reviewed_row_hashes=row_hashes,
        )
        fixed_left_context_sidecar = _load_context_sidecar(
            fixed_left_context_bytes,
            fixed_left_context_sidecar_path,
            targets=targets,
            reviewed_row_hashes=row_hashes,
        )
        fixed_empty_bytes = acceptable._read_regular(
            fixed_empty_v7_path, "Mozc-fixed empty-context ABProbe v7"
        )
        fixed_left_bytes = acceptable._read_regular(
            fixed_left_v7_path, "Mozc-fixed left-context ABProbe v7"
        )
        fixed_empty_run = _load_v7_run(fixed_empty_bytes, fixed_empty_v7_path)
        fixed_left_run = _load_v7_run(fixed_left_bytes, fixed_left_v7_path)
        _validate_fixed_pair(
            targets,
            manifest,
            fixed_raw_mozc_run,
            fixed_sidecar,
            fixed_empty_run,
            fixed_left_run,
            fixed_empty_context_sidecar,
            fixed_left_context_sidecar,
        )
        fixed_cases = [
            _build_fixed_case(
                target,
                fixed_empty_run["cases"][target["id"]],
                fixed_left_run["cases"][target["id"]],
                fixed_raw_mozc_run["cases"][target["id"]],
                fixed_sidecar["records"][target["id"]]["consuming_count"],
            )
            for target in targets
        ]
        fixed_by_mode = {
            mode: [case for case in fixed_cases if case["context"]["mode"] == mode]
            for mode in ("natural_left", "empty")
        }
        fixed_by_bucket = {
            name: [
                case
                for case in fixed_cases
                if _length_bucket(
                    case["context"]["left_context_code_point_count"]
                )
                == name
            ]
            for name, _minimum, _maximum in LENGTH_BUCKETS
        }
        fixed_comparison: dict[str, Any] = {
            "available": True,
            "comparison_axis": "mozc-fixed-surface-empty-vs-natural-left",
            "boundary_policy": fixed_empty_run["boundary_policy"],
            "provenance_chain_verified": {
                "result_to_fixed_sidecar_exact_sha256": True,
                "fixed_sidecar_to_raw_mozc_exact_sha256": True,
                "ids_order_readings_and_counts_rederived": True,
            },
            "inputs": {
                "raw_mozc_v6": {
                    "path": str(fixed_raw_mozc_v6_path),
                    "sha256": acceptable._sha256(fixed_raw_mozc_bytes),
                },
                "fixed_boundary_sidecar": {
                    "path": str(fixed_boundary_sidecar_path),
                    **fixed_sidecar["identity"],
                    "origin": fixed_sidecar["origin"],
                },
                "fixed_empty_context_sidecar": {
                    "path": str(fixed_empty_context_sidecar_path),
                    **fixed_empty_context_sidecar["identity"],
                },
                "fixed_left_context_sidecar": {
                    "path": str(fixed_left_context_sidecar_path),
                    **fixed_left_context_sidecar["identity"],
                },
                "fixed_empty_v7": {
                    "path": str(fixed_empty_v7_path),
                    "sha256": acceptable._sha256(fixed_empty_bytes),
                    "zenzai_execution": fixed_empty_run["zenzai_execution"],
                },
                "fixed_left_v7": {
                    "path": str(fixed_left_v7_path),
                    "sha256": acceptable._sha256(fixed_left_bytes),
                    "zenzai_execution": fixed_left_run["zenzai_execution"],
                },
            },
            "all_cases": _fixed_summary(fixed_cases),
            "mozc_to_hazkey_at_fixed_boundary": {
                "candidate_filter": (
                    "raw Mozc candidates with consuming_count exactly equal to "
                    "the attested fixed-boundary sidecar count, retaining relative rank"
                ),
                "empty": _pairwise(
                    fixed_cases,
                    (MOZC_AT_FIXED_BOUNDARY_SYSTEM, FIXED_EMPTY_SYSTEM),
                ),
                "natural_left": _pairwise(
                    fixed_cases,
                    (MOZC_AT_FIXED_BOUNDARY_SYSTEM, FIXED_LEFT_CONTEXT_SYSTEM),
                ),
                "by_category": {
                    category: {
                        "empty": _pairwise(
                            category_cases,
                            (
                                MOZC_AT_FIXED_BOUNDARY_SYSTEM,
                                FIXED_EMPTY_SYSTEM,
                            ),
                        ),
                        "natural_left": _pairwise(
                            category_cases,
                            (
                                MOZC_AT_FIXED_BOUNDARY_SYSTEM,
                                FIXED_LEFT_CONTEXT_SYSTEM,
                            ),
                        ),
                    }
                    for category, category_cases in _cases_by_category(
                        fixed_cases
                    ).items()
                },
            },
            "by_context_mode": {
                "natural_left": {
                    "role": "primary_surface_effect_at_mozc_fixed_boundary",
                    **_fixed_summary(fixed_by_mode["natural_left"]),
                },
                "empty": {
                    "role": "negative_control",
                    **_fixed_summary(fixed_by_mode["empty"]),
                },
            },
            "by_context_code_point_length": {
                name: _fixed_summary(bucket_cases)
                for name, bucket_cases in fixed_by_bucket.items()
            },
            "by_category": {
                category: _fixed_summary(category_cases)
                for category, category_cases in _cases_by_category(
                    fixed_cases
                ).items()
            },
            "cases": fixed_cases,
        }
    else:
        fixed_comparison = {
            "available": False,
            "reason": (
                "raw Mozc v6, fixed-boundary sidecar, fixed v7 pair, and "
                "their two context sidecars were not supplied"
            ),
        }
    execution_runs = [
        ("isolated_empty_v7", empty_run),
        ("isolated_left_v7", contextual_run),
    ]
    empty_context_runs = [("isolated_empty_v7", empty_run)]
    left_context_runs = [("isolated_left_v7", contextual_run)]
    if native_comparison["available"]:
        execution_runs.extend(
            (
                ("native_empty_v7", native_empty_run),
                ("native_left_v7", native_left_run),
            )
        )
        empty_context_runs.append(("native_empty_v7", native_empty_run))
        left_context_runs.append(("native_left_v7", native_left_run))
    if fixed_comparison["available"]:
        execution_runs.extend(
            (
                ("fixed_empty_v7", fixed_empty_run),
                ("fixed_left_v7", fixed_left_run),
            )
        )
        empty_context_runs.append(("fixed_empty_v7", fixed_empty_run))
        left_context_runs.append(("fixed_left_v7", fixed_left_run))
    _validate_common_hazkey_v7_acquisition(execution_runs)
    _validate_common_context_source(
        empty_context_runs,
        isolated_empty_sidecar["identity"],
        "empty",
    )
    _validate_common_context_source(
        left_context_runs,
        isolated_left_sidecar["identity"],
        "natural-left",
    )
    if native_comparison["available"] and empty_run["top_k"] > 5:
        raise ValueError(
            "Hazkey v7 common top_k must be <= 5 when the native "
            "firstClauseResults path is compared"
        )
    if native_comparison["available"] and fixed_comparison["available"]:
        empty_policy_systems = (
            ISOLATED_EMPTY_SYSTEM,
            NATIVE_EMPTY_SYSTEM,
            FIXED_EMPTY_SYSTEM,
        )
        left_policy_systems = (
            ISOLATED_LEFT_CONTEXT_SYSTEM,
            NATIVE_LEFT_CONTEXT_SYSTEM,
            FIXED_LEFT_CONTEXT_SYSTEM,
        )
        empty_policy_cases = _combine_outcomes_by_case(
            (
                (ISOLATED_EMPTY_SYSTEM, cases),
                (NATIVE_EMPTY_SYSTEM, native_cases),
                (FIXED_EMPTY_SYSTEM, fixed_cases),
            )
        )
        left_policy_cases = _combine_outcomes_by_case(
            (
                (ISOLATED_LEFT_CONTEXT_SYSTEM, cases),
                (NATIVE_LEFT_CONTEXT_SYSTEM, native_cases),
                (FIXED_LEFT_CONTEXT_SYSTEM, fixed_cases),
            )
        )
        cross_policy_comparison: dict[str, Any] = {
            "available": True,
            "context_source_identity_shared_within_each_context_role": True,
            "empty": _cross_policy_context_summary(
                empty_policy_cases, empty_policy_systems
            ),
            "natural_left": _cross_policy_context_summary(
                left_policy_cases, left_policy_systems
            ),
        }
    else:
        cross_policy_comparison = {
            "available": False,
            "reason": (
                "both native and Mozc-fixed v7 pairs are required for the "
                "three-boundary-policy within-context matrix"
            ),
        }
    execution_failure_blockers = _execution_failure_blockers(execution_runs)
    return {
        "schema": OUTPUT_SCHEMA,
        "diagnostic_only": True,
        "formal_authorized": False,
        "evaluation_scope": {
            "comparison": "hazkey-zenzai-three-boundary-policies-by-v7-context-mode",
            "primary_effect_subset": "context.mode=natural_left",
            "negative_control_subset": "context.mode=empty",
            "first_segment_only": True,
            "surface_scope": "fully-aligned-first-segment-pairs-only",
            "raw_left_context_emitted": False,
            "context_sidecar_exact_bytes_verified": True,
            "context_source_bound_to_reviewed_row_sha256": True,
            "gold_category_usage": "stratification-only",
            "runtime_gate_uses_gold_category": False,
            "all_hazkey_v7_common_acquisition_fields_verified": True,
            "all_hazkey_v7_context_sources_match_by_context_role": True,
            "requested_top_k": empty_run["top_k"],
            "effective_comparable_top_k": empty_run["top_k"],
            "native_first_clause_results_top_k_limit": (
                5 if native_comparison["available"] else None
            ),
            "memory_measurement_semantics": (
                "sequential separate ABProbe processes; RSS/PSS after and "
                "within-process deltas are diagnostic, not randomized paired effects"
            ),
            "candidate_score_required_for_v7": False,
            "boundary_zenzai_enabled_semantics": (
                "requested configuration policy, not successful-execution evidence"
            ),
        },
        "inputs": {
            "generation_manifest": {
                "path": str(generation_manifest),
                "sha256": acceptable._sha256(manifest_bytes),
                "schema": acceptable.MANIFEST_SCHEMA,
            },
            "targets": {
                "path": str(targets_path),
                "sha256": acceptable._sha256(targets_bytes),
                "schema": acceptable.TARGET_SCHEMA,
                "cases": len(targets),
            },
            "probe_input": {
                "path": manifest["bindings"]["probe_input"]["path"],
                "sha256": acceptable._sha256(bound["probe_input"]),
                "schema": acceptable.PROBE_INPUT_SCHEMA,
            },
            "isolated_empty_context_sidecar": {
                "path": str(isolated_empty_context_sidecar_path),
                **isolated_empty_sidecar["identity"],
            },
            "isolated_left_context_sidecar": {
                "path": str(isolated_left_context_sidecar_path),
                **isolated_left_sidecar["identity"],
            },
            "isolated_empty_v7": {
                "path": str(isolated_empty_v7_path),
                "sha256": acceptable._sha256(empty_bytes),
                "producer": empty_run["producer"],
                "resource": empty_run["resource"],
                "quality_policy": empty_run["quality_policy"],
                "context_source": empty_run["context_source"],
                "zenzai_execution": empty_run["zenzai_execution"],
            },
            "isolated_left_v7": {
                "path": str(isolated_left_v7_path),
                "sha256": acceptable._sha256(contextual_bytes),
                "producer": contextual_run["producer"],
                "resource": contextual_run["resource"],
                "quality_policy": contextual_run["quality_policy"],
                "context_source": contextual_run["context_source"],
                "zenzai_execution": contextual_run["zenzai_execution"],
            },
            "paired_acquisition": {
                "backend": empty_run["backend"],
                "backend_version": empty_run["backend_version"],
                "source_ref": empty_run["source_ref"],
                "top_k": empty_run["top_k"],
                "warmups": empty_run["warmups"],
                "iterations": empty_run["iterations"],
                "verified_hazkey_v7_runs": [label for label, _run in execution_runs],
                "common_fields": list(COMMON_HAZKEY_V7_ACQUISITION_FIELDS),
            },
        },
        "isolated_surface_comparison": {
            "boundary_policy": contextual_run["boundary_policy"],
            "all_cases": _summary(cases, ISOLATED_SYSTEMS),
            "by_context_mode": {
                "natural_left": {
                    "role": "primary_effect",
                    **_summary(by_mode["natural_left"], ISOLATED_SYSTEMS),
                },
                "empty": {
                    "role": "negative_control",
                    **_summary(by_mode["empty"], ISOLATED_SYSTEMS),
                },
            },
            "by_context_code_point_length": {
                name: _summary(bucket_cases, ISOLATED_SYSTEMS)
                for name, bucket_cases in by_bucket.items()
            },
            "by_category": {
                category: _summary(category_cases, ISOLATED_SYSTEMS)
                for category, category_cases in _cases_by_category(cases).items()
            },
            "cases": cases,
        },
        "native_boundary_comparison": native_comparison,
        "mozc_fixed_boundary_comparison": fixed_comparison,
        "within_context_boundary_policy_pairwise": cross_policy_comparison,
        "zenzai_execution": {
            label: run["zenzai_execution"] for label, run in execution_runs
        },
        # Stable aliases for the original isolated-only report consumers.
        "all_cases": _summary(cases, ISOLATED_SYSTEMS),
        "by_context_mode": {
            "natural_left": {
                "role": "primary_effect",
                **_summary(by_mode["natural_left"], ISOLATED_SYSTEMS),
            },
            "empty": {
                "role": "negative_control",
                **_summary(by_mode["empty"], ISOLATED_SYSTEMS),
            },
        },
        "by_context_code_point_length": {
            name: _summary(bucket_cases, ISOLATED_SYSTEMS)
            for name, bucket_cases in by_bucket.items()
        },
        "by_category": {
            category: _summary(category_cases, ISOLATED_SYSTEMS)
            for category, category_cases in _cases_by_category(cases).items()
        },
        "cases": cases,
        "decision": {
            "status": "inconclusive",
            "formal_authorized": False,
            "reason": (
                "diagnostic paired context comparison; the acceptable-path gold "
                "set is not context-conditioned policy-authorization evidence"
            ),
            "formal_blockers": [
                "acceptable-path generation is diagnostic-only",
                "acceptable paths were not adjudicated with natural left context as a gold input",
                "dynamic llama/GGML runtime dependency identities are not bound",
                "separate-run latency deltas are not randomized carry-over-controlled measurements",
                "this comparison does not authorize a Top-1 override rule",
            ]
            + execution_failure_blockers,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Compare paired Hazkey+Zenzai ABProbe v7 empty-context and natural "
            "left-context runs against one acceptable-path generation."
        )
    )
    parser.add_argument("--generation-manifest", type=Path, required=True)
    parser.add_argument("--targets", type=Path, required=True)
    parser.add_argument("--isolated-empty-context-sidecar", type=Path, required=True)
    parser.add_argument("--isolated-empty-v7", type=Path, required=True)
    parser.add_argument("--isolated-left-context-sidecar", type=Path, required=True)
    parser.add_argument("--isolated-left-v7", type=Path, required=True)
    parser.add_argument("--native-empty-v7", type=Path)
    parser.add_argument("--native-empty-context-sidecar", type=Path)
    parser.add_argument("--native-left-v7", type=Path)
    parser.add_argument("--native-left-context-sidecar", type=Path)
    parser.add_argument("--fixed-raw-mozc-v6", type=Path)
    parser.add_argument("--fixed-boundary-sidecar", type=Path)
    parser.add_argument("--fixed-empty-v7", type=Path)
    parser.add_argument("--fixed-empty-context-sidecar", type=Path)
    parser.add_argument("--fixed-left-v7", type=Path)
    parser.add_argument("--fixed-left-context-sidecar", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = evaluate(
            args.generation_manifest,
            args.targets,
            args.isolated_empty_context_sidecar,
            args.isolated_empty_v7,
            args.isolated_left_context_sidecar,
            args.isolated_left_v7,
            native_empty_v7_path=args.native_empty_v7,
            native_empty_context_sidecar_path=args.native_empty_context_sidecar,
            native_left_v7_path=args.native_left_v7,
            native_left_context_sidecar_path=args.native_left_context_sidecar,
            fixed_raw_mozc_v6_path=args.fixed_raw_mozc_v6,
            fixed_boundary_sidecar_path=args.fixed_boundary_sidecar,
            fixed_empty_v7_path=args.fixed_empty_v7,
            fixed_empty_context_sidecar_path=args.fixed_empty_context_sidecar,
            fixed_left_v7_path=args.fixed_left_v7,
            fixed_left_context_sidecar_path=args.fixed_left_context_sidecar,
        )
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return 0
    except (OSError, ValueError, AssertionError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
