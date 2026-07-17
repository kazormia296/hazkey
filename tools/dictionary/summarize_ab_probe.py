#!/usr/bin/env python3
"""Validate and summarize one or more Hazkey A/B probe JSONL runs.

Each input file is one run. Latency statistics are recomputed from every raw
sample, P95 uses the nearest-rank definition, and memory maxima use the largest
available before/after snapshot across all cases and runs. Source and resource
provenance must be identical in every result and run.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import math
from pathlib import Path
import re
import statistics
import sys
from typing import Any


INPUT_SCHEMA_V1 = "hazkey.ab-probe-result.v1"
INPUT_SCHEMA_V2 = "hazkey.ab-probe-result.v2"
INPUT_SCHEMA_V3 = "hazkey.ab-probe-result.v3"
INPUT_SCHEMA_V4 = "hazkey.ab-probe-result.v4"
INPUT_SCHEMA_V5 = "hazkey.ab-probe-result.v5"
INPUT_SCHEMA_V6 = "hazkey.ab-probe-result.v6"
OUTPUT_SCHEMA_V1 = "hazkey.ab-probe-summary.v1"
OUTPUT_SCHEMA_V2 = "hazkey.ab-probe-summary.v2"
OUTPUT_SCHEMA_V3 = "hazkey.ab-probe-summary.v3"
OUTPUT_SCHEMA_V4 = "hazkey.ab-probe-summary.v4"
OUTPUT_SCHEMA_V5 = "hazkey.ab-probe-summary.v5"
OUTPUT_SCHEMA_V6 = "hazkey.ab-probe-summary.v6"
SEGMENT_CANDIDATES_PATH = "segment_candidates"
COMPOSITION_ELEMENT_UNIT = "composition_element"
V2_RESOURCE_KIND_BY_CONVERTER = {
    "hazkey": "hazkey_dictionary",
    "mozc": "mozc_runtime_inputs",
}
# Preserve the original module constants for existing importers.
INPUT_SCHEMA = INPUT_SCHEMA_V1
OUTPUT_SCHEMA = OUTPUT_SCHEMA_V1


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _required(payload: dict[str, Any], field: str, context: str) -> Any:
    if field not in payload:
        raise ValueError(f"{context}.{field} is required")
    return payload[field]


def _object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be an object")
    return value


def _array(value: Any, context: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{context} must be an array")
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


def _sha256(value: Any, context: str) -> str:
    result = _string(value, context)
    if re.fullmatch(r"sha256:[0-9a-f]{64}", result) is None:
        raise ValueError(
            f"{context} must be sha256: followed by 64 lowercase hex digits"
        )
    return result


def _nonnegative_number(value: Any, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{context} must be a finite non-negative number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{context} must be a finite non-negative number")
    return result


def _finite_number(value: Any, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{context} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{context} must be a finite number")
    return result


def _optional_rss(value: Any, context: str) -> int | None:
    if value is None:
        return None
    return _nonnegative_int(value, context)


def _optional_int_field(
    payload: dict[str, Any], field: str, context: str
) -> int | None:
    if field not in payload:
        return None
    return _optional_rss(payload[field], f"{context}.{field}")


def _total_memory_snapshots(
    parent: list[int | None],
    backend: list[int | None],
    *,
    requires_backend: bool,
) -> list[int | None]:
    totals: list[int | None] = []
    for parent_value, backend_value in zip(parent, backend, strict=True):
        if parent_value is None or (requires_backend and backend_value is None):
            totals.append(None)
        else:
            totals.append(parent_value + (backend_value or 0))
    return totals


def _nearest_rank_p95(samples: list[float]) -> float:
    ordered = sorted(samples)
    index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * 0.95) - 1))
    return ordered[index]


def _require_number(actual: Any, expected: float, context: str) -> None:
    value = _nonnegative_number(actual, context)
    if not math.isclose(value, expected, rel_tol=0.0, abs_tol=1e-9):
        raise ValueError(f"{context} is inconsistent: expected {expected!r}, got {value!r}")


def validate_result(payload: Any, context: str) -> dict[str, Any]:
    result = _object(payload, context)
    schema = result.get("schema")
    if schema not in (
        INPUT_SCHEMA_V1,
        INPUT_SCHEMA_V2,
        INPUT_SCHEMA_V3,
        INPUT_SCHEMA_V4,
        INPUT_SCHEMA_V5,
        INPUT_SCHEMA_V6,
    ):
        raise ValueError(
            f"{context}.schema must be {INPUT_SCHEMA_V1}, {INPUT_SCHEMA_V2}, "
            f"{INPUT_SCHEMA_V3}, {INPUT_SCHEMA_V4}, {INPUT_SCHEMA_V5}, or "
            f"{INPUT_SCHEMA_V6}"
        )

    case_id = _string(_required(result, "id", context), f"{context}.id")
    category = _string(
        _required(result, "category", context), f"{context}.category"
    )
    backend = _string(
        _required(result, "backend", context), f"{context}.backend"
    )
    backend_version = _string(
        _required(result, "backend_version", context),
        f"{context}.backend_version",
    )
    source_ref = _string(
        _required(result, "source_ref", context), f"{context}.source_ref"
    )
    if schema == INPUT_SCHEMA_V1:
        resource = {
            "kind": "hazkey_dictionary",
            "path": _string(
                _required(result, "dictionary_path", context),
                f"{context}.dictionary_path",
            ),
            "fingerprint": _string(
                _required(result, "dictionary_fingerprint", context),
                f"{context}.dictionary_fingerprint",
            ),
        }
        converter_backend = None
    else:
        raw_resource = _object(
            _required(result, "resource", context), f"{context}.resource"
        )
        resource = {
            field: _string(
                _required(raw_resource, field, f"{context}.resource"),
                f"{context}.resource.{field}",
            )
            for field in ("kind", "path", "fingerprint")
        }
        converter_backend = _string(
            _required(result, "converter_backend", context),
            f"{context}.converter_backend",
        )
        expected_resource_kind = V2_RESOURCE_KIND_BY_CONVERTER.get(
            converter_backend
        )
        if expected_resource_kind is None:
            raise ValueError(
                f"{context}.converter_backend must be hazkey or mozc"
            )
        if resource["kind"] != expected_resource_kind:
            raise ValueError(
                f"{context}.resource.kind must be {expected_resource_kind!r} "
                f"for converter_backend {converter_backend!r}"
            )
    if schema in (
        INPUT_SCHEMA_V3,
        INPUT_SCHEMA_V4,
        INPUT_SCHEMA_V5,
        INPUT_SCHEMA_V6,
    ):
        reading = _string(
            _required(result, "reading", context), f"{context}.reading"
        )
        top_k = _positive_int(
            _required(result, "top_k", context), f"{context}.top_k"
        )
        if top_k > 10:
            raise ValueError(f"{context}.top_k must be between 1 and 10")
        raw_corpus = _object(
            _required(result, "corpus", context), f"{context}.corpus"
        )
        expected_corpus_fields = {"sha256", "cases"}
        actual_corpus_fields = set(raw_corpus)
        if actual_corpus_fields != expected_corpus_fields:
            missing = sorted(expected_corpus_fields - actual_corpus_fields)
            unexpected = sorted(actual_corpus_fields - expected_corpus_fields)
            raise ValueError(
                f"{context}.corpus must contain exactly sha256 and cases; "
                f"missing={missing!r}, unexpected={unexpected!r}"
            )
        corpus = {
            "sha256": _sha256(
                raw_corpus["sha256"], f"{context}.corpus.sha256"
            ),
            "cases": _positive_int(
                raw_corpus["cases"], f"{context}.corpus.cases"
            ),
        }
    else:
        reading = None
        top_k = None
        corpus = None

    if schema in (INPUT_SCHEMA_V4, INPUT_SCHEMA_V5, INPUT_SCHEMA_V6):
        conversion_path = _string(
            _required(result, "conversion_path", context),
            f"{context}.conversion_path",
        )
        if conversion_path != SEGMENT_CANDIDATES_PATH:
            raise ValueError(
                f"{context}.conversion_path must be "
                f"{SEGMENT_CANDIDATES_PATH!r}"
            )
    else:
        conversion_path = None

    raw_candidates = _array(
        _required(result, "candidates", context), f"{context}.candidates"
    )
    candidates: list[str] | list[dict[str, Any]]
    if schema in (INPUT_SCHEMA_V4, INPUT_SCHEMA_V5, INPUT_SCHEMA_V6):
        candidates = []
        expected_candidate_fields = {"text", "rank", "consuming_count"}
        if schema == INPUT_SCHEMA_V6:
            expected_candidate_fields |= {
                "provenance",
                "ranking_influence",
                "zenzai_score",
                "zenzai_score_token_count",
                "zenzai_score_scope",
            }
        for index, raw_candidate in enumerate(raw_candidates):
            candidate_context = f"{context}.candidates[{index}]"
            candidate = _object(raw_candidate, candidate_context)
            actual_candidate_fields = set(candidate)
            if actual_candidate_fields != expected_candidate_fields:
                missing = sorted(
                    expected_candidate_fields - actual_candidate_fields
                )
                unexpected = sorted(
                    actual_candidate_fields - expected_candidate_fields
                )
                raise ValueError(
                    f"{candidate_context} must contain exactly text, rank, and "
                    f"consuming_count; missing={missing!r}, "
                    f"unexpected={unexpected!r}"
                )
            rank = _positive_int(candidate["rank"], f"{candidate_context}.rank")
            expected_rank = index + 1
            if rank != expected_rank:
                raise ValueError(
                    f"{candidate_context}.rank must be {expected_rank}, got {rank}"
                )
            parsed_candidate = {
                "text": _string(
                    candidate["text"], f"{candidate_context}.text"
                ),
                "rank": rank,
                "consuming_count": _positive_int(
                    candidate["consuming_count"],
                    f"{candidate_context}.consuming_count",
                ),
            }
            if schema == INPUT_SCHEMA_V6:
                provenance = _string(
                    candidate["provenance"],
                    f"{candidate_context}.provenance",
                )
                if provenance not in {
                    "standard",
                    "personalDictionary",
                    "projectDictionary",
                    "temporaryDictionary",
                    "zenzai",
                    "builtInGuard",
                    "unknown",
                }:
                    raise ValueError(
                        f"{candidate_context}.provenance is invalid"
                    )
                ranking_influence = _string(
                    candidate["ranking_influence"],
                    f"{candidate_context}.ranking_influence",
                )
                if ranking_influence not in {"standard", "zenzai"}:
                    raise ValueError(
                        f"{candidate_context}.ranking_influence is invalid"
                    )
                raw_score = candidate["zenzai_score"]
                raw_score_token_count = candidate["zenzai_score_token_count"]
                raw_score_scope = candidate["zenzai_score_scope"]
                score_metadata_presence = (
                    raw_score is not None,
                    raw_score_token_count is not None,
                    raw_score_scope is not None,
                )
                if len(set(score_metadata_presence)) != 1:
                    raise ValueError(
                        f"{candidate_context}.zenzai_score, "
                        "zenzai_score_token_count, and zenzai_score_scope "
                        "must be all null or all present"
                    )
                zenzai_score = (
                    None
                    if raw_score is None
                    else _finite_number(
                        raw_score, f"{candidate_context}.zenzai_score"
                    )
                )
                if zenzai_score is not None and ranking_influence != "zenzai":
                    raise ValueError(
                        f"{candidate_context}.zenzai_score requires "
                        "ranking_influence='zenzai'"
                    )
                zenzai_score_token_count = (
                    None
                    if raw_score_token_count is None
                    else _positive_int(
                        raw_score_token_count,
                        f"{candidate_context}.zenzai_score_token_count",
                    )
                )
                zenzai_score_scope = (
                    None
                    if raw_score_scope is None
                    else _string(
                        raw_score_scope,
                        f"{candidate_context}.zenzai_score_scope",
                    )
                )
                if zenzai_score_scope not in {
                    None,
                    "full_candidate",
                    "constraint_suffix",
                }:
                    raise ValueError(
                        f"{candidate_context}.zenzai_score_scope is invalid"
                    )
                parsed_candidate.update(
                    {
                        "provenance": provenance,
                        "ranking_influence": ranking_influence,
                        "zenzai_score": zenzai_score,
                        "zenzai_score_token_count": zenzai_score_token_count,
                        "zenzai_score_scope": zenzai_score_scope,
                    }
                )
            candidates.append(parsed_candidate)
    else:
        candidates = []
        for index, candidate in enumerate(raw_candidates):
            candidates.append(
                _string(candidate, f"{context}.candidates[{index}]")
            )
    if top_k is not None and len(candidates) > top_k:
        raise ValueError(
            f"{context}.candidates has {len(candidates)} values; top_k is {top_k}"
        )

    if schema in (INPUT_SCHEMA_V5, INPUT_SCHEMA_V6):
        span_context = f"{context}.composition_span"
        raw_span = _object(
            _required(result, "composition_span", context), span_context
        )
        expected_span_fields = {"start", "count", "unit"}
        actual_span_fields = set(raw_span)
        if actual_span_fields != expected_span_fields:
            missing = sorted(expected_span_fields - actual_span_fields)
            unexpected = sorted(actual_span_fields - expected_span_fields)
            raise ValueError(
                f"{span_context} must contain exactly start, count, and unit; "
                f"missing={missing!r}, unexpected={unexpected!r}"
            )
        span_start = _nonnegative_int(raw_span["start"], f"{span_context}.start")
        if span_start != 0:
            raise ValueError(f"{span_context}.start must be 0")
        span_count = _positive_int(raw_span["count"], f"{span_context}.count")
        span_unit = _string(raw_span["unit"], f"{span_context}.unit")
        if span_unit != COMPOSITION_ELEMENT_UNIT:
            raise ValueError(
                f"{span_context}.unit must be {COMPOSITION_ELEMENT_UNIT!r}"
            )
        composition_span = {
            "start": span_start,
            "count": span_count,
            "unit": span_unit,
        }
        if any(
            candidate["consuming_count"] > span_count
            for candidate in candidates
        ):
            raise ValueError(
                f"{context}.candidates consuming_count must not exceed "
                "composition_span.count"
            )
        whole_span_candidate_count = sum(
            candidate["consuming_count"] == span_count
            for candidate in candidates
        )
    else:
        composition_span = None
        whole_span_candidate_count = None

    if schema == INPUT_SCHEMA_V6:
        producer_context = f"{context}.producer"
        raw_producer = _object(
            _required(result, "producer", context), producer_context
        )
        expected_producer_fields = {"path", "size_bytes", "sha256"}
        if set(raw_producer) != expected_producer_fields:
            raise ValueError(
                f"{producer_context} must contain exactly path, size_bytes, "
                "and sha256"
            )
        producer = {
            "path": _string(raw_producer["path"], f"{producer_context}.path"),
            "size_bytes": _positive_int(
                raw_producer["size_bytes"], f"{producer_context}.size_bytes"
            ),
            "sha256": _sha256(
                raw_producer["sha256"], f"{producer_context}.sha256"
            ),
        }

        policy_context = f"{context}.quality_policy"
        raw_policy = _object(
            _required(result, "quality_policy", context), policy_context
        )
        if set(raw_policy) != {"learning", "context", "zenzai"}:
            raise ValueError(
                f"{policy_context} must contain exactly learning, context, "
                "and zenzai"
            )
        if raw_policy["learning"] is not False:
            raise ValueError(f"{policy_context}.learning must be false")
        if raw_policy["context"] != "empty":
            raise ValueError(f"{policy_context}.context must be empty")
        zenzai_context = f"{policy_context}.zenzai"
        raw_zenzai = _object(raw_policy["zenzai"], zenzai_context)
        expected_zenzai_fields = {
            "enabled",
            "model_path",
            "model_size_bytes",
            "model_sha256",
            "inference_limit",
            "resolved_device",
        }
        if set(raw_zenzai) != expected_zenzai_fields:
            raise ValueError(
                f"{zenzai_context} fields do not match the v6 contract"
            )
        enabled = raw_zenzai["enabled"]
        if not isinstance(enabled, bool):
            raise ValueError(f"{zenzai_context}.enabled must be a boolean")
        nullable_fields = (
            "model_path",
            "model_size_bytes",
            "model_sha256",
            "inference_limit",
            "resolved_device",
        )
        if enabled:
            zenzai = {
                "enabled": True,
                "model_path": _string(
                    raw_zenzai["model_path"], f"{zenzai_context}.model_path"
                ),
                "model_size_bytes": _positive_int(
                    raw_zenzai["model_size_bytes"],
                    f"{zenzai_context}.model_size_bytes",
                ),
                "model_sha256": _sha256(
                    raw_zenzai["model_sha256"],
                    f"{zenzai_context}.model_sha256",
                ),
                "inference_limit": _positive_int(
                    raw_zenzai["inference_limit"],
                    f"{zenzai_context}.inference_limit",
                ),
                "resolved_device": _string(
                    raw_zenzai["resolved_device"],
                    f"{zenzai_context}.resolved_device",
                ),
            }
            if converter_backend != "hazkey":
                raise ValueError(
                    f"{zenzai_context}.enabled requires converter_backend='hazkey'"
                )
        else:
            if any(raw_zenzai[field] is not None for field in nullable_fields):
                raise ValueError(
                    f"{zenzai_context} disabled metadata must be null"
                )
            zenzai = {"enabled": False, **{field: None for field in nullable_fields}}
        quality_policy = {
            "learning": False,
            "context": "empty",
            "zenzai": zenzai,
        }
        if not enabled and any(
            candidate["ranking_influence"] == "zenzai"
            or candidate["zenzai_score"] is not None
            or candidate["zenzai_score_token_count"] is not None
            or candidate["zenzai_score_scope"] is not None
            for candidate in candidates
        ):
            raise ValueError(
                f"{context}.candidates contain Zenzai evidence while Zenzai is disabled"
            )
    else:
        producer = None
        quality_policy = None

    measurement = _object(
        _required(result, "measurement", context), f"{context}.measurement"
    )
    warmups = _nonnegative_int(
        _required(measurement, "warmups", f"{context}.measurement"),
        f"{context}.measurement.warmups",
    )
    iterations = _positive_int(
        _required(measurement, "iterations", f"{context}.measurement"),
        f"{context}.measurement.iterations",
    )
    if (
        schema == INPUT_SCHEMA_V6
        and quality_policy["zenzai"]["enabled"]
        and iterations != 1
    ):
        raise ValueError(
            f"{context}.measurement.iterations must be 1 when Zenzai is enabled"
        )

    latency = _object(
        _required(measurement, "latency_ms", f"{context}.measurement"),
        f"{context}.measurement.latency_ms",
    )
    raw_samples = _array(
        _required(latency, "samples", f"{context}.measurement.latency_ms"),
        f"{context}.measurement.latency_ms.samples",
    )
    samples = [
        _nonnegative_number(
            sample, f"{context}.measurement.latency_ms.samples[{index}]"
        )
        for index, sample in enumerate(raw_samples)
    ]
    if len(samples) != iterations:
        raise ValueError(
            f"{context}.measurement.latency_ms.samples has {len(samples)} values; "
            f"iterations is {iterations}"
        )
    _require_number(
        _required(latency, "median", f"{context}.measurement.latency_ms"),
        float(statistics.median(samples)),
        f"{context}.measurement.latency_ms.median",
    )
    _require_number(
        _required(latency, "p95", f"{context}.measurement.latency_ms"),
        _nearest_rank_p95(samples),
        f"{context}.measurement.latency_ms.p95",
    )
    _require_number(
        _required(latency, "minimum", f"{context}.measurement.latency_ms"),
        min(samples),
        f"{context}.measurement.latency_ms.minimum",
    )
    _require_number(
        _required(latency, "maximum", f"{context}.measurement.latency_ms"),
        max(samples),
        f"{context}.measurement.latency_ms.maximum",
    )

    rss = _object(
        _required(measurement, "rss", f"{context}.measurement"),
        f"{context}.measurement.rss",
    )
    rss_before = _optional_rss(
        _required(rss, "before_kib", f"{context}.measurement.rss"),
        f"{context}.measurement.rss.before_kib",
    )
    rss_after = _optional_rss(
        _required(rss, "after_kib", f"{context}.measurement.rss"),
        f"{context}.measurement.rss.after_kib",
    )
    rss_context = f"{context}.measurement.rss"
    before_pss = _optional_int_field(rss, "before_pss_kib", rss_context)
    after_pss = _optional_int_field(rss, "after_pss_kib", rss_context)
    backend_before = _optional_int_field(rss, "backend_before_kib", rss_context)
    backend_after = _optional_int_field(rss, "backend_after_kib", rss_context)
    backend_before_pss = _optional_int_field(
        rss, "backend_before_pss_kib", rss_context
    )
    backend_after_pss = _optional_int_field(
        rss, "backend_after_pss_kib", rss_context
    )

    if "backend_diagnostics" not in measurement:
        process_launch_count = None
        cleanup_failure_count = None
    else:
        diagnostics = _object(
            measurement["backend_diagnostics"],
            f"{context}.measurement.backend_diagnostics",
        )
        diagnostics_context = f"{context}.measurement.backend_diagnostics"
        process_launch_count = _optional_int_field(
            diagnostics, "process_launch_count", diagnostics_context
        )
        cleanup_failure_count = _optional_int_field(
            diagnostics, "cleanup_failure_count", diagnostics_context
        )

    parent_rss = [rss_before, rss_after]
    parent_pss = [before_pss, after_pss]
    backend_rss = [backend_before, backend_after]
    backend_pss = [backend_before_pss, backend_after_pss]
    backend_diagnostic_values = [
        process_launch_count,
        cleanup_failure_count,
    ]
    has_backend_evidence = (
        any(value is not None for value in backend_rss)
        or any(value is not None for value in backend_pss)
        or any(value is not None for value in backend_diagnostic_values)
    )

    return {
        "schema": schema,
        "id": case_id,
        "reading": reading,
        "category": category,
        "backend": backend,
        "backend_version": backend_version,
        "source_ref": source_ref,
        "resource": resource,
        "dictionary_path": (
            resource["path"] if schema == INPUT_SCHEMA_V1 else None
        ),
        "dictionary_fingerprint": (
            resource["fingerprint"] if schema == INPUT_SCHEMA_V1 else None
        ),
        "converter_backend": converter_backend,
        "producer": producer,
        "quality_policy": quality_policy,
        "conversion_path": conversion_path,
        "composition_span": composition_span,
        "whole_span_candidate_count": whole_span_candidate_count,
        "top_k": top_k,
        "corpus": corpus,
        "candidates": candidates,
        "warmups": warmups,
        "iterations": iterations,
        "samples": samples,
        "rss": parent_rss,
        "parent_pss": parent_pss,
        "backend_rss": backend_rss,
        "backend_pss": backend_pss,
        "has_backend_evidence": has_backend_evidence,
        "backend_diagnostics": backend_diagnostic_values,
    }


def load_run_bytes(data: bytes, path: Path | str) -> dict[str, Any]:
    """Parse one immutable JSONL byte snapshot using ``path`` for context."""

    try:
        contents = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"{path}: invalid UTF-8: {error.reason}") from error

    cases: dict[str, dict[str, Any]] = {}
    for line_number, line in enumerate(contents.split("\n"), 1):
        if not line.strip():
            continue
        context = f"{path}:{line_number}"
        try:
            payload = json.loads(
                line,
                object_pairs_hook=_object_without_duplicate_keys,
            )
        except json.JSONDecodeError as error:
            raise ValueError(f"{context}: invalid JSON: {error.msg}") from error
        except ValueError as error:
            raise ValueError(f"{context}: {error}") from error
        result = validate_result(payload, context)
        case_id = result["id"]
        if case_id in cases:
            raise ValueError(f"{path}: duplicate id {case_id!r}")
        cases[case_id] = result

    if not cases:
        raise ValueError(f"{path}: probe run has no results")

    first = next(iter(cases.values()))
    consistency_fields = (
        "schema",
        "backend",
        "backend_version",
        "source_ref",
        "converter_backend",
        "warmups",
        "iterations",
    )
    if first["schema"] in (
        INPUT_SCHEMA_V3,
        INPUT_SCHEMA_V4,
        INPUT_SCHEMA_V5,
        INPUT_SCHEMA_V6,
    ):
        consistency_fields += ("top_k", "corpus")
    if first["schema"] in (INPUT_SCHEMA_V4, INPUT_SCHEMA_V5, INPUT_SCHEMA_V6):
        consistency_fields += ("conversion_path",)
    if first["schema"] == INPUT_SCHEMA_V6:
        consistency_fields += ("producer", "quality_policy")
    consistency_fields += (
        ("dictionary_path", "dictionary_fingerprint")
        if first["schema"] == INPUT_SCHEMA_V1
        else ("resource",)
    )
    for case in cases.values():
        for field in consistency_fields:
            if case[field] != first[field]:
                raise ValueError(f"{path}: inconsistent {field} within run")
    if (
        first["schema"] in (
            INPUT_SCHEMA_V3,
            INPUT_SCHEMA_V4,
            INPUT_SCHEMA_V5,
            INPUT_SCHEMA_V6,
        )
        and first["corpus"]["cases"] != len(cases)
    ):
        raise ValueError(f"{path}: corpus.cases does not match result count")
    if (
        first["schema"] == INPUT_SCHEMA_V6
        and first["quality_policy"]["zenzai"]["enabled"]
        and not any(
            candidate["zenzai_score"] is not None
            for case in cases.values()
            for candidate in case["candidates"]
        )
    ):
        raise ValueError(
            f"{path}: enabled Zenzai run has no observed candidate pass score"
        )
    return {
        "path": path,
        "schema": first["schema"],
        "backend": first["backend"],
        "backend_version": first["backend_version"],
        "source_ref": first["source_ref"],
        "resource": first["resource"],
        "dictionary_path": first["dictionary_path"],
        "dictionary_fingerprint": first["dictionary_fingerprint"],
        "converter_backend": first["converter_backend"],
        "producer": first["producer"],
        "quality_policy": first["quality_policy"],
        "conversion_path": first["conversion_path"],
        "composition_span_available": (
            first["schema"] in (INPUT_SCHEMA_V5, INPUT_SCHEMA_V6)
        ),
        "top_k": first["top_k"],
        "corpus": first["corpus"],
        "warmups": first["warmups"],
        "iterations": first["iterations"],
        "cases": cases,
    }


def load_run(path: Path) -> dict[str, Any]:
    return load_run_bytes(path.read_bytes(), path)


def summarize(paths: list[Path]) -> dict[str, Any]:
    if not paths:
        raise ValueError("at least one probe JSONL run is required")
    runs = [load_run(path) for path in paths]
    first = runs[0]
    expected_ids = set(first["cases"])
    expected_categories = {
        case_id: case["category"] for case_id, case in first["cases"].items()
    }
    expected_readings = {
        case_id: case["reading"] for case_id, case in first["cases"].items()
    }
    expected_composition_spans = {
        case_id: case["composition_span"]
        for case_id, case in first["cases"].items()
    }
    for run in runs[1:]:
        if run["schema"] != first["schema"]:
            raise ValueError(
                f"{run['path']}: cannot mix {run['schema']} with {first['schema']}"
            )
        consistency_fields = (
            "backend",
            "backend_version",
            "source_ref",
            "converter_backend",
            "warmups",
            "iterations",
        )
        if first["schema"] in (
            INPUT_SCHEMA_V3,
            INPUT_SCHEMA_V4,
            INPUT_SCHEMA_V5,
            INPUT_SCHEMA_V6,
        ):
            consistency_fields += ("top_k", "corpus")
        if first["schema"] in (
            INPUT_SCHEMA_V4,
            INPUT_SCHEMA_V5,
            INPUT_SCHEMA_V6,
        ):
            consistency_fields += ("conversion_path",)
        if first["schema"] == INPUT_SCHEMA_V6:
            consistency_fields += ("producer", "quality_policy")
        consistency_fields += (
            ("dictionary_path", "dictionary_fingerprint")
            if first["schema"] == INPUT_SCHEMA_V1
            else ("resource",)
        )
        for field in consistency_fields:
            if run[field] != first[field]:
                raise ValueError(
                    f"{run['path']}: {field} does not match the first run"
                )
        actual_ids = set(run["cases"])
        if actual_ids != expected_ids:
            missing = sorted(expected_ids - actual_ids)
            unexpected = sorted(actual_ids - expected_ids)
            raise ValueError(
                f"{run['path']}: case set does not match the first run; "
                f"missing={missing!r}, unexpected={unexpected!r}"
            )
        for case_id, category in expected_categories.items():
            if run["cases"][case_id]["category"] != category:
                raise ValueError(
                    f"{run['path']}: category for case {case_id!r} does not "
                    "match the first run"
                )
            if first["schema"] in (
                INPUT_SCHEMA_V3,
                INPUT_SCHEMA_V4,
                INPUT_SCHEMA_V5,
                INPUT_SCHEMA_V6,
            ) and (
                run["cases"][case_id]["reading"]
                != expected_readings[case_id]
            ):
                raise ValueError(
                    f"{run['path']}: reading for case {case_id!r} does not "
                    "match the first run"
                )
            if first["schema"] in (INPUT_SCHEMA_V5, INPUT_SCHEMA_V6) and (
                run["cases"][case_id]["composition_span"]
                != expected_composition_spans[case_id]
            ):
                raise ValueError(
                    f"{run['path']}: composition_span for case {case_id!r} "
                    "does not match the first run"
                )
            if first["schema"] in (
                INPUT_SCHEMA_V2,
                INPUT_SCHEMA_V3,
                INPUT_SCHEMA_V4,
                INPUT_SCHEMA_V5,
                INPUT_SCHEMA_V6,
            ) and (
                run["cases"][case_id]["candidates"]
                != first["cases"][case_id]["candidates"]
            ):
                raise ValueError(
                    f"{run['path']}: candidates for case {case_id!r} do not "
                    "match the first run"
                )

    all_samples: list[float] = []
    run_totals: list[float] = []
    rss_values: list[int] = []
    parent_pss_values: list[int] = []
    backend_rss_values: list[int] = []
    backend_pss_values: list[int] = []
    total_rss_values: list[int] = []
    total_pss_values: list[int] = []
    process_launch_counts: list[int] = []
    cleanup_failure_counts: list[int] = []
    requires_backend_memory = (
        first["converter_backend"] not in (None, "hazkey")
        or any(
            case["has_backend_evidence"]
            for run in runs
            for case in run["cases"].values()
        )
    )
    for run in runs:
        run_samples = [
            sample
            for case in run["cases"].values()
            for sample in case["samples"]
        ]
        all_samples.extend(run_samples)
        run_totals.append(sum(run_samples))
        rss_values.extend(
            value
            for case in run["cases"].values()
            for value in case["rss"]
            if value is not None
        )
        for case in run["cases"].values():
            parent_pss_values.extend(
                value for value in case["parent_pss"] if value is not None
            )
            backend_rss_values.extend(
                value for value in case["backend_rss"] if value is not None
            )
            backend_pss_values.extend(
                value for value in case["backend_pss"] if value is not None
            )
            total_rss_values.extend(
                value
                for value in _total_memory_snapshots(
                    case["rss"],
                    case["backend_rss"],
                    requires_backend=requires_backend_memory,
                )
                if value is not None
            )
            total_pss_values.extend(
                value
                for value in _total_memory_snapshots(
                    case["parent_pss"],
                    case["backend_pss"],
                    requires_backend=requires_backend_memory,
                )
                if value is not None
            )
            process_launch_count, cleanup_failure_count = case[
                "backend_diagnostics"
            ]
            if process_launch_count is not None:
                process_launch_counts.append(process_launch_count)
            if cleanup_failure_count is not None:
                cleanup_failure_counts.append(cleanup_failure_count)

    expected_measurements = (
        len(runs) * len(expected_ids) * first["iterations"]
    )
    if len(all_samples) != expected_measurements:
        raise ValueError(
            "measured conversion count is inconsistent with runs, cases, and iterations"
        )
    if first["schema"] == INPUT_SCHEMA_V1:
        provenance = {
            "source_ref": first["source_ref"],
            "dictionary_path": first["resource"]["path"],
            "dictionary_fingerprint": first["resource"]["fingerprint"],
        }
        converter_summary = {}
    else:
        provenance = {
            "source_ref": first["source_ref"],
            "resource": first["resource"],
        }
        if first["schema"] in (
            INPUT_SCHEMA_V3,
            INPUT_SCHEMA_V4,
            INPUT_SCHEMA_V5,
            INPUT_SCHEMA_V6,
        ):
            provenance["corpus"] = first["corpus"]
        if first["schema"] == INPUT_SCHEMA_V6:
            provenance["producer"] = first["producer"]
        converter_summary = {"converter_backend": first["converter_backend"]}
    corpus_summary = (
        {"top_k": first["top_k"]}
        if first["schema"] in (
            INPUT_SCHEMA_V3,
            INPUT_SCHEMA_V4,
            INPUT_SCHEMA_V5,
            INPUT_SCHEMA_V6,
        )
        else {}
    )
    segment_summary = (
        {"conversion_path": first["conversion_path"]}
        if first["schema"] in (
            INPUT_SCHEMA_V4,
            INPUT_SCHEMA_V5,
            INPUT_SCHEMA_V6,
        )
        else {}
    )
    if first["schema"] in (INPUT_SCHEMA_V5, INPUT_SCHEMA_V6):
        span_counts = [
            case["composition_span"]["count"]
            for case in first["cases"].values()
        ]
        cases_with_top1_consuming_full_span = sum(
            bool(case["candidates"])
            and case["candidates"][0]["consuming_count"]
            == case["composition_span"]["count"]
            for case in first["cases"].values()
        )
        composition_span_summary = {
            "composition_span_evidence": {
                "available": True,
                "unit": COMPOSITION_ELEMENT_UNIT,
                "start": 0,
                "min_count": min(span_counts),
                "max_count": max(span_counts),
                "cases_with_top1_consuming_full_span": (
                    cases_with_top1_consuming_full_span
                ),
                "rate_with_top1_consuming_full_span": (
                    cases_with_top1_consuming_full_span / len(expected_ids)
                ),
            }
        }
    else:
        composition_span_summary = {}
    if first["schema"] == INPUT_SCHEMA_V6:
        candidates = [
            candidate
            for case in first["cases"].values()
            for candidate in case["candidates"]
        ]
        influenced = [
            candidate
            for candidate in candidates
            if candidate["ranking_influence"] == "zenzai"
        ]
        scored = [
            candidate
            for candidate in candidates
            if candidate["zenzai_score"] is not None
        ]
        scores = [candidate["zenzai_score"] for candidate in scored]
        scores_per_token = [
            candidate["zenzai_score"] / candidate["zenzai_score_token_count"]
            for candidate in scored
        ]
        score_scopes = Counter(
            candidate["zenzai_score_scope"] for candidate in scored
        )
        quality_policy_summary = {
            "quality_policy": first["quality_policy"],
            "zenzai_evidence": {
                "candidates": len(candidates),
                "ranking_influenced_candidates": len(influenced),
                "scored_candidates": len(scored),
                "score_coverage_of_influenced_candidates": (
                    len(scored) / len(influenced) if influenced else None
                ),
                "minimum_score": min(scores) if scores else None,
                "maximum_score": max(scores) if scores else None,
                "minimum_score_per_token": (
                    min(scores_per_token) if scores_per_token else None
                ),
                "maximum_score_per_token": (
                    max(scores_per_token) if scores_per_token else None
                ),
                "score_scope_counts": {
                    scope: score_scopes.get(scope, 0)
                    for scope in ("full_candidate", "constraint_suffix")
                },
            },
        }
    else:
        quality_policy_summary = {}
    summary = {
        "schema": (
            OUTPUT_SCHEMA_V1
            if first["schema"] == INPUT_SCHEMA_V1
            else (
                OUTPUT_SCHEMA_V2
                if first["schema"] == INPUT_SCHEMA_V2
                else (
                    OUTPUT_SCHEMA_V3
                    if first["schema"] == INPUT_SCHEMA_V3
                    else (
                        OUTPUT_SCHEMA_V4
                        if first["schema"] == INPUT_SCHEMA_V4
                        else (
                            OUTPUT_SCHEMA_V5
                            if first["schema"] == INPUT_SCHEMA_V5
                            else OUTPUT_SCHEMA_V6
                        )
                    )
                )
            )
        ),
        "backend": first["backend"],
        "backend_version": first["backend_version"],
        **converter_summary,
        **corpus_summary,
        **segment_summary,
        **composition_span_summary,
        **quality_policy_summary,
        "provenance": provenance,
        "runs": len(runs),
        "cases_per_run": len(expected_ids),
        "iterations": first["iterations"],
        "measured_conversions": len(all_samples),
        "mean_latency_ms": statistics.fmean(all_samples),
        "median_latency_ms": float(statistics.median(all_samples)),
        "p95_latency_ms": _nearest_rank_p95(all_samples),
        "min_latency_ms": min(all_samples),
        "max_latency_ms": max(all_samples),
        "mean_total_ms_per_run": statistics.fmean(run_totals),
        "max_observed_rss_kib": max(rss_values) if rss_values else None,
        "max_observed_total_rss_kib": (
            max(total_rss_values) if total_rss_values else None
        ),
        "max_observed_parent_pss_kib": (
            max(parent_pss_values) if parent_pss_values else None
        ),
        "max_observed_backend_rss_kib": (
            max(backend_rss_values) if backend_rss_values else None
        ),
        "max_observed_backend_pss_kib": (
            max(backend_pss_values) if backend_pss_values else None
        ),
        "max_observed_total_pss_kib": (
            max(total_pss_values) if total_pss_values else None
        ),
        "max_backend_process_launch_count": (
            max(process_launch_counts) if process_launch_counts else None
        ),
        "max_backend_cleanup_failure_count": (
            max(cleanup_failure_counts) if cleanup_failure_counts else None
        ),
    }
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("runs", nargs="+", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        encoded = json.dumps(
            summarize(args.runs), ensure_ascii=False, indent=2
        ) + "\n"
        if args.output:
            args.output.write_text(encoded, encoding="utf-8")
        else:
            sys.stdout.write(encoded)
        return 0
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
