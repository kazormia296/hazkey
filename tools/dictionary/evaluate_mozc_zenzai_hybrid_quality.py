#!/usr/bin/env python3
"""Evaluate Mozc, Hazkey+Zenzai, and the production H0 merge.

The input is a pair of strict ABProbe v6 runs over one immutable acceptable-
path generation.  Mozc and Hazkey+Zenzai are observed systems.  The hybrid is
derived with the same boundary-aware H0 merge helper as the runtime mirror and
never enables H1/H2 promotion.

The reviewed 1,360-case corpus is already known.  This evaluator is therefore
diagnostic-only and cannot authorize a production ranking policy.
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
import unicodedata

try:
    from . import evaluate_mozc_acceptable_path_boundaries as acceptable
    from . import evaluate_mozc_hybrid_spike as hybrid
    from .summarize_ab_probe import INPUT_SCHEMA_V6, load_run_bytes
except ImportError:  # Direct execution from tools/dictionary.
    import evaluate_mozc_acceptable_path_boundaries as acceptable
    import evaluate_mozc_hybrid_spike as hybrid
    from summarize_ab_probe import INPUT_SCHEMA_V6, load_run_bytes


INPUT_SCHEMA = INPUT_SCHEMA_V6
OUTPUT_SCHEMA = "hazkey.mozc-zenzai-hybrid-quality-evaluation.v1"
CONVERSION_PATH = "segment_candidates"
COMPOSITION_ELEMENT_UNIT = "composition_element"

MOZC_SYSTEM = "mozc_standalone"
HAZKEY_ZENZAI_SYSTEM = "hazkey_zenzai_standalone"
H0_SYSTEM = "mozc_first_hazkey_zenzai_h0"
HAZKEY_ZENZAI_SURFACE_SOURCE = "hazkey_primary_converter_zenzai_enabled"
HAZKEY_BOUNDARY_SOURCE = "hazkey_boundary_converter_zenzai_disabled"
SYSTEMS = (MOZC_SYSTEM, HAZKEY_ZENZAI_SYSTEM, H0_SYSTEM)

H0_POLICY = {
    "id": "mozc-first-preserve-top1-h0",
    "allow_promotion": False,
    "width_guard": False,
    "stable_mozc_prefix": hybrid.MOZC_STABLE_PREFIX,
}

ROOT_FIELDS = acceptable.ABPROBE_ROOT_FIELDS | {"producer", "quality_policy"}
CANDIDATE_FIELDS = {
    "text",
    "rank",
    "consuming_count",
    "provenance",
    "ranking_influence",
    "zenzai_score",
    "zenzai_score_token_count",
    "zenzai_score_scope",
}
PRODUCER_FIELDS = {"path", "size_bytes", "sha256"}
QUALITY_POLICY_FIELDS = {"learning", "context", "zenzai"}
ZENZAI_POLICY_FIELDS = {
    "enabled",
    "model_path",
    "model_size_bytes",
    "model_sha256",
    "inference_limit",
    "resolved_device",
}
MEASUREMENT_FIELDS = {
    "warmups",
    "iterations",
    "latency_ms",
    "rss",
    "backend_diagnostics",
}
LATENCY_FIELDS = {"median", "p95", "minimum", "maximum", "samples"}
PROVENANCE_VALUES = {
    "standard",
    "personalDictionary",
    "projectDictionary",
    "temporaryDictionary",
    "zenzai",
    "builtInGuard",
    "unknown",
}
RANKING_INFLUENCE_VALUES = {"standard", "zenzai"}
RESOURCE_KIND_BY_CONVERTER = {
    "hazkey": "hazkey_dictionary",
    "mozc": "mozc_runtime_inputs",
}


def _finite_number(value: Any, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{context} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{context} must be a finite number")
    return result


def _nonnegative_number(value: Any, context: str) -> float:
    result = _finite_number(value, context)
    if result < 0:
        raise ValueError(f"{context} must be non-negative")
    return result


def _nullable_nonnegative_int(value: Any, context: str) -> int | None:
    if value is None:
        return None
    return acceptable._nonnegative_int(value, context)


def _nearest_rank_p95(samples: Sequence[float]) -> float:
    ordered = sorted(samples)
    index = min(
        len(ordered) - 1,
        max(0, math.ceil(len(ordered) * 0.95) - 1),
    )
    return ordered[index]


def _same_number(actual: Any, expected: float, context: str) -> None:
    value = _nonnegative_number(actual, context)
    if not math.isclose(value, expected, rel_tol=0.0, abs_tol=1e-9):
        raise ValueError(
            f"{context} is inconsistent; expected={expected!r}, actual={value!r}"
        )


def _validate_producer(value: Any, context: str) -> dict[str, Any]:
    producer = acceptable._exact_object(value, PRODUCER_FIELDS, context)
    return {
        "path": acceptable._string(producer["path"], f"{context}.path"),
        "size_bytes": acceptable._positive_int(
            producer["size_bytes"], f"{context}.size_bytes"
        ),
        "sha256": acceptable._hash(producer["sha256"], f"{context}.sha256"),
    }


def _validate_quality_policy(value: Any, context: str) -> dict[str, Any]:
    policy = acceptable._exact_object(value, QUALITY_POLICY_FIELDS, context)
    if policy["learning"] is not False:
        raise ValueError(f"{context}.learning must be false")
    if policy["context"] != "empty":
        raise ValueError(f"{context}.context must be 'empty'")
    raw_zenzai = acceptable._exact_object(
        policy["zenzai"], ZENZAI_POLICY_FIELDS, f"{context}.zenzai"
    )
    if not isinstance(raw_zenzai["enabled"], bool):
        raise ValueError(f"{context}.zenzai.enabled must be a boolean")
    enabled = raw_zenzai["enabled"]
    nullable_fields = (
        "model_path",
        "model_size_bytes",
        "model_sha256",
        "inference_limit",
        "resolved_device",
    )
    if not enabled:
        if any(raw_zenzai[field] is not None for field in nullable_fields):
            raise ValueError(
                f"{context}.zenzai disabled policy must use null model/runtime fields"
            )
        zenzai = {"enabled": False, **{field: None for field in nullable_fields}}
    else:
        if any(raw_zenzai[field] is None for field in nullable_fields):
            raise ValueError(
                f"{context}.zenzai enabled policy requires model/runtime fields"
            )
        zenzai = {
            "enabled": True,
            "model_path": acceptable._string(
                raw_zenzai["model_path"], f"{context}.zenzai.model_path"
            ),
            "model_size_bytes": acceptable._positive_int(
                raw_zenzai["model_size_bytes"],
                f"{context}.zenzai.model_size_bytes",
            ),
            "model_sha256": acceptable._hash(
                raw_zenzai["model_sha256"],
                f"{context}.zenzai.model_sha256",
            ),
            "inference_limit": acceptable._positive_int(
                raw_zenzai["inference_limit"],
                f"{context}.zenzai.inference_limit",
            ),
            "resolved_device": acceptable._string(
                raw_zenzai["resolved_device"],
                f"{context}.zenzai.resolved_device",
            ),
        }
    return {"learning": False, "context": "empty", "zenzai": zenzai}


def _validate_measurement(value: Any, context: str) -> dict[str, Any]:
    measurement = acceptable._exact_object(value, MEASUREMENT_FIELDS, context)
    warmups = acceptable._nonnegative_int(
        measurement["warmups"], f"{context}.warmups"
    )
    iterations = acceptable._positive_int(
        measurement["iterations"], f"{context}.iterations"
    )
    latency = acceptable._exact_object(
        measurement["latency_ms"], LATENCY_FIELDS, f"{context}.latency_ms"
    )
    if not isinstance(latency["samples"], list):
        raise ValueError(f"{context}.latency_ms.samples must be an array")
    samples = [
        _nonnegative_number(sample, f"{context}.latency_ms.samples[{index}]")
        for index, sample in enumerate(latency["samples"])
    ]
    if len(samples) != iterations:
        raise ValueError(
            f"{context}.latency_ms.samples count must equal iterations"
        )
    _same_number(
        latency["median"], statistics.median(samples), f"{context}.latency_ms.median"
    )
    _same_number(
        latency["p95"], _nearest_rank_p95(samples), f"{context}.latency_ms.p95"
    )
    _same_number(
        latency["minimum"], min(samples), f"{context}.latency_ms.minimum"
    )
    _same_number(
        latency["maximum"], max(samples), f"{context}.latency_ms.maximum"
    )

    rss = measurement["rss"]
    if (
        not isinstance(rss, dict)
        or not {"before_kib", "after_kib"}.issubset(rss)
        or not set(rss).issubset(acceptable.RSS_FIELDS)
    ):
        raise ValueError(f"{context}.rss fields differ")
    normalized_rss = {
        field: _nullable_nonnegative_int(raw, f"{context}.rss.{field}")
        for field, raw in rss.items()
    }
    diagnostics = measurement["backend_diagnostics"]
    allowed_diagnostics = {"process_launch_count", "cleanup_failure_count"}
    if not isinstance(diagnostics, dict) or not set(diagnostics).issubset(
        allowed_diagnostics
    ):
        raise ValueError(f"{context}.backend_diagnostics fields differ")
    normalized_diagnostics = {
        field: _nullable_nonnegative_int(
            raw, f"{context}.backend_diagnostics.{field}"
        )
        for field, raw in diagnostics.items()
    }
    return {
        "warmups": warmups,
        "iterations": iterations,
        "latency_ms": {
            "median": float(statistics.median(samples)),
            "p95": _nearest_rank_p95(samples),
            "minimum": min(samples),
            "maximum": max(samples),
            "samples": samples,
        },
        "rss": normalized_rss,
        "backend_diagnostics": normalized_diagnostics,
    }


def _validate_candidate(
    value: Any,
    context: str,
    *,
    expected_rank: int,
    composition_count: int,
) -> dict[str, Any]:
    candidate = acceptable._exact_object(value, CANDIDATE_FIELDS, context)
    rank = acceptable._positive_int(candidate["rank"], f"{context}.rank")
    if rank != expected_rank:
        raise ValueError(f"{context}.rank must be {expected_rank}")
    consuming_count = acceptable._positive_int(
        candidate["consuming_count"], f"{context}.consuming_count"
    )
    if consuming_count > composition_count:
        raise ValueError(
            f"{context}.consuming_count must not exceed composition_span.count"
        )
    provenance = candidate["provenance"]
    if provenance not in PROVENANCE_VALUES:
        raise ValueError(f"{context}.provenance is not supported")
    influence = candidate["ranking_influence"]
    if influence not in RANKING_INFLUENCE_VALUES:
        raise ValueError(f"{context}.ranking_influence is not supported")
    score = candidate["zenzai_score"]
    score_token_count = candidate["zenzai_score_token_count"]
    score_scope = candidate["zenzai_score_scope"]
    score_metadata_presence = (
        score is not None,
        score_token_count is not None,
        score_scope is not None,
    )
    if len(set(score_metadata_presence)) != 1:
        raise ValueError(
            f"{context}.zenzai_score, zenzai_score_token_count, and "
            "zenzai_score_scope must be all null or all present"
        )
    if score is not None:
        score = _finite_number(score, f"{context}.zenzai_score")
        if influence != "zenzai":
            raise ValueError(
                f"{context}.zenzai_score requires zenzai ranking influence"
            )
        score_token_count = acceptable._positive_int(
            score_token_count, f"{context}.zenzai_score_token_count"
        )
        if score_scope not in {"full_candidate", "constraint_suffix"}:
            raise ValueError(f"{context}.zenzai_score_scope is not supported")
    return {
        "text": acceptable._string(candidate["text"], f"{context}.text"),
        "rank": rank,
        "consuming_count": consuming_count,
        "provenance": provenance,
        "ranking_influence": influence,
        "zenzai_score": score,
        "zenzai_score_token_count": score_token_count,
        "zenzai_score_scope": score_scope,
    }


def _validate_v6_record(value: Any, context: str) -> dict[str, Any]:
    root = acceptable._exact_object(value, ROOT_FIELDS, context)
    if root["schema"] != INPUT_SCHEMA:
        raise ValueError(f"{context}.schema must be {INPUT_SCHEMA!r}")
    if root["conversion_path"] != CONVERSION_PATH:
        raise ValueError(
            f"{context}.conversion_path must be {CONVERSION_PATH!r}"
        )
    converter = root["converter_backend"]
    if converter not in RESOURCE_KIND_BY_CONVERTER:
        raise ValueError(f"{context}.converter_backend must be hazkey or mozc")
    resource = acceptable._exact_object(
        root["resource"], {"kind", "path", "fingerprint"}, f"{context}.resource"
    )
    if resource["kind"] != RESOURCE_KIND_BY_CONVERTER[converter]:
        raise ValueError(f"{context}.resource.kind conflicts with converter_backend")
    normalized_resource = {
        "kind": resource["kind"],
        "path": acceptable._string(resource["path"], f"{context}.resource.path"),
        "fingerprint": acceptable._hash(
            resource["fingerprint"], f"{context}.resource.fingerprint"
        ),
    }
    corpus = acceptable._exact_object(
        root["corpus"], {"sha256", "cases"}, f"{context}.corpus"
    )
    normalized_corpus = {
        "sha256": acceptable._hash(corpus["sha256"], f"{context}.corpus.sha256"),
        "cases": acceptable._positive_int(corpus["cases"], f"{context}.corpus.cases"),
    }
    reading = acceptable._string(root["reading"], f"{context}.reading")
    span = acceptable._validate_span(
        root["composition_span"], f"{context}.composition_span", len(reading)
    )
    if span["count"] != len(reading):
        raise ValueError(f"{context}.composition_span must cover the whole reading")
    top_k = acceptable._positive_int(root["top_k"], f"{context}.top_k")
    if top_k > 10:
        raise ValueError(f"{context}.top_k must be between 1 and 10")
    if not isinstance(root["candidates"], list):
        raise ValueError(f"{context}.candidates must be an array")
    if len(root["candidates"]) > top_k:
        raise ValueError(f"{context}.candidates exceeds top_k")
    candidates = [
        _validate_candidate(
            candidate,
            f"{context}.candidates[{index}]",
            expected_rank=index + 1,
            composition_count=span["count"],
        )
        for index, candidate in enumerate(root["candidates"])
    ]
    quality_policy = _validate_quality_policy(
        root["quality_policy"], f"{context}.quality_policy"
    )
    if converter == "mozc":
        if quality_policy["zenzai"]["enabled"]:
            raise ValueError(f"{context}: Mozc run must disable Zenzai")
        if any(
            candidate["ranking_influence"] != "standard"
            or candidate["zenzai_score"] is not None
            or candidate["zenzai_score_token_count"] is not None
            or candidate["zenzai_score_scope"] is not None
            for candidate in candidates
        ):
            raise ValueError(
                f"{context}: Mozc candidates cannot carry Zenzai evidence"
            )
    elif not quality_policy["zenzai"]["enabled"]:
        raise ValueError(f"{context}: Hazkey quality run must enable Zenzai")

    return {
        "schema": INPUT_SCHEMA,
        "conversion_path": CONVERSION_PATH,
        "id": acceptable._string(root["id"], f"{context}.id"),
        "reading": reading,
        "category": acceptable._string(root["category"], f"{context}.category"),
        "backend": acceptable._string(root["backend"], f"{context}.backend"),
        "backend_version": acceptable._string(
            root["backend_version"], f"{context}.backend_version"
        ),
        "converter_backend": converter,
        "source_ref": acceptable._string(
            root["source_ref"], f"{context}.source_ref"
        ),
        "resource": normalized_resource,
        "producer": _validate_producer(root["producer"], f"{context}.producer"),
        "quality_policy": quality_policy,
        "top_k": top_k,
        "corpus": normalized_corpus,
        "candidates": candidates,
        "composition_span": span,
        "measurement": _validate_measurement(
            root["measurement"], f"{context}.measurement"
        ),
    }


def _load_v6_run(data: bytes, path: Path, converter: str) -> dict[str, Any]:
    records = acceptable._jsonl(data, str(path))
    case_ids: list[str] = []
    for index, raw in enumerate(records, 1):
        case = _validate_v6_record(raw, f"{path}:{index}")
        if case["id"] in case_ids:
            raise ValueError(f"{path}: duplicate id {case['id']!r}")
        case_ids.append(case["id"])

    # Reuse the ABProbe module's semantic v6 parser after the stricter exact-
    # field pass above, matching the existing acceptable-path evaluator.
    run = load_run_bytes(data, path)
    if run["schema"] != INPUT_SCHEMA or run["conversion_path"] != CONVERSION_PATH:
        raise ValueError(f"{path}: run must use ABProbe v6 segment_candidates")
    if run["converter_backend"] != converter:
        raise ValueError(f"{path}: converter_backend identity mismatch")
    if list(run["cases"]) != case_ids:
        raise AssertionError("shared ABProbe loader changed result order")
    if converter == "hazkey" and not any(
        candidate["zenzai_score"] is not None
        for case in run["cases"].values()
        for candidate in case["candidates"]
    ):
        raise ValueError(
            f"{path}: enabled Zenzai run has no observed candidate pass score"
        )
    return run


def _validate_runs(
    targets: list[dict[str, Any]],
    manifest: dict[str, Any],
    mozc: dict[str, Any],
    hazkey_zenzai: dict[str, Any],
) -> None:
    expected_ids = [target["id"] for target in targets]
    binding = manifest["bindings"]["probe_input"]
    expected_corpus = {"sha256": binding["sha256"], "cases": binding["cases"]}
    for label, run in (("Mozc", mozc), ("Hazkey+Zenzai", hazkey_zenzai)):
        if list(run["cases"]) != expected_ids:
            raise ValueError(f"{label} result IDs/order do not match targets")
        if run["corpus"] != expected_corpus:
            raise ValueError(f"{label} corpus identity does not match probe input")
    for field in (
        "schema",
        "conversion_path",
        "backend_version",
        "source_ref",
        "producer",
        "top_k",
        "corpus",
        "warmups",
        "iterations",
    ):
        if mozc[field] != hazkey_zenzai[field]:
            raise ValueError(f"paired run metadata {field} differs")
    for target in targets:
        case_id = target["id"]
        expected_span = {
            "start": 0,
            "count": len(target["reading"]),
            "unit": COMPOSITION_ELEMENT_UNIT,
        }
        for label, run in (("Mozc", mozc), ("Hazkey+Zenzai", hazkey_zenzai)):
            case = run["cases"][case_id]
            if case["reading"] != target["reading"]:
                raise ValueError(f"{label} case {case_id!r} reading mismatch")
            if case["category"] != target["category"]:
                raise ValueError(f"{label} case {case_id!r} category mismatch")
            if case["composition_span"] != expected_span:
                raise ValueError(f"{label} case {case_id!r} composition_span mismatch")


def _ratio(hits: int, cases: int) -> float | None:
    return hits / cases if cases else None


def _candidate_gold_key(candidate: dict[str, Any]) -> tuple[int, str]:
    return candidate["consuming_count"], candidate["text"]


def _candidate_runtime_key(candidate: dict[str, Any]) -> tuple[int, str]:
    return (
        candidate["consuming_count"],
        hybrid.normalized_surface(candidate["text"]),
    )


def _first_rank(values: Iterable[bool]) -> int | None:
    return next((index for index, value in enumerate(values, 1) if value), None)


def _gold_outcome(
    candidates: list[dict[str, Any]], target: dict[str, Any]
) -> dict[str, Any]:
    accepted_boundaries = {
        span["count"] for span in target["acceptable_first_spans"]
    }
    acceptable_pairs = {
        (chunk["span"]["count"], chunk["surface"])
        for chunk in target["acceptable_first_chunks"]
    }
    fully_aligned = target["surface_evaluation_status"] == "fully_aligned"
    boundary_matches = [
        candidate["consuming_count"] in accepted_boundaries
        for candidate in candidates
    ]
    boundary_rank = _first_rank(boundary_matches)
    e2e_matches = [
        _candidate_gold_key(candidate) in acceptable_pairs
        for candidate in candidates
    ]
    e2e_rank = _first_rank(e2e_matches) if fully_aligned else None
    top1_boundary = bool(boundary_matches[:1] and boundary_matches[0])
    topk_boundary = any(boundary_matches)
    return {
        "first_segment_boundary": {
            "at1": top1_boundary,
            "at_k": topk_boundary,
            "first_hit_rank": boundary_rank,
            "reciprocal_rank": 0.0 if boundary_rank is None else 1 / boundary_rank,
        },
        "conditional_surface_given_acceptable_first_segment_boundary": {
            "eligible": fully_aligned,
            "at1_comparable": fully_aligned and top1_boundary,
            "at1_hit": (
                bool(e2e_matches[:1] and e2e_matches[0])
                if fully_aligned and top1_boundary
                else None
            ),
            "at_k_comparable": fully_aligned and topk_boundary,
            "at_k_hit": (
                any(e2e_matches)
                if fully_aligned and topk_boundary
                else None
            ),
        },
        "end_to_end": {
            "comparable": fully_aligned,
            "at1": (
                bool(e2e_matches[:1] and e2e_matches[0])
                if fully_aligned
                else None
            ),
            "at_k": any(e2e_matches) if fully_aligned else None,
            "first_hit_rank": e2e_rank,
            "reciprocal_rank": (
                None
                if not fully_aligned
                else 0.0 if e2e_rank is None else 1 / e2e_rank
            ),
        },
    }


def _accuracy(values: list[bool]) -> dict[str, Any]:
    hits = sum(values)
    return {"hits": hits, "cases": len(values), "accuracy": _ratio(hits, len(values))}


def _system_metrics(
    cases: list[dict[str, Any]], system: str
) -> dict[str, Any]:
    outcomes = [case["gold_outcomes"]["systems"][system] for case in cases]
    boundary_at1 = [outcome["first_segment_boundary"]["at1"] for outcome in outcomes]
    boundary_atk = [outcome["first_segment_boundary"]["at_k"] for outcome in outcomes]
    boundary_rr = [
        outcome["first_segment_boundary"]["reciprocal_rank"] for outcome in outcomes
    ]
    conditional = {
        "at1": [
            outcome["conditional_surface_given_acceptable_first_segment_boundary"][
                "at1_hit"
            ]
            for outcome in outcomes
            if outcome[
                "conditional_surface_given_acceptable_first_segment_boundary"
            ]["at1_comparable"]
        ],
        "at_k": [
            outcome["conditional_surface_given_acceptable_first_segment_boundary"][
                "at_k_hit"
            ]
            for outcome in outcomes
            if outcome[
                "conditional_surface_given_acceptable_first_segment_boundary"
            ]["at_k_comparable"]
        ],
    }
    e2e_outcomes = [
        outcome["end_to_end"]
        for outcome in outcomes
        if outcome["end_to_end"]["comparable"]
    ]
    e2e_rr = [outcome["reciprocal_rank"] for outcome in e2e_outcomes]
    return {
        "first_segment_boundary": {
            "at1": _accuracy(boundary_at1),
            "at_k": _accuracy(boundary_atk),
            "mrr": {
                "cases": len(boundary_rr),
                "value": sum(boundary_rr) / len(boundary_rr) if boundary_rr else None,
            },
        },
        "conditional_surface_given_acceptable_first_segment_boundary": {
            rank: _accuracy([bool(value) for value in values])
            for rank, values in conditional.items()
        },
        "end_to_end": {
            "at1": _accuracy([bool(outcome["at1"]) for outcome in e2e_outcomes]),
            "at_k": _accuracy([bool(outcome["at_k"]) for outcome in e2e_outcomes]),
            "mrr": {
                "cases": len(e2e_rr),
                "value": sum(e2e_rr) / len(e2e_rr) if e2e_rr else None,
            },
        },
    }


def _delta(values: Iterable[tuple[bool, bool]]) -> dict[str, int]:
    pairs = list(values)
    rescued = sum(not baseline and candidate for baseline, candidate in pairs)
    regressed = sum(baseline and not candidate for baseline, candidate in pairs)
    return {
        "comparable_cases": len(pairs),
        "rescued": rescued,
        "regressed": regressed,
        "net": rescued - regressed,
        "both_hit": sum(baseline and candidate for baseline, candidate in pairs),
        "both_miss": sum(not baseline and not candidate for baseline, candidate in pairs),
    }


def _pairwise_delta(
    cases: list[dict[str, Any]], baseline: str, candidate: str
) -> dict[str, Any]:
    def outcomes(system: str) -> list[dict[str, Any]]:
        return [case["gold_outcomes"]["systems"][system] for case in cases]

    baseline_values = outcomes(baseline)
    candidate_values = outcomes(candidate)
    boundary = {
        rank: _delta(
            (
                bool(left["first_segment_boundary"][rank]),
                bool(right["first_segment_boundary"][rank]),
            )
            for left, right in zip(baseline_values, candidate_values, strict=True)
        )
        for rank in ("at1", "at_k")
    }
    e2e: dict[str, Any] = {}
    for rank in ("at1", "at_k"):
        pairs: list[tuple[bool, bool]] = []
        for left, right in zip(baseline_values, candidate_values, strict=True):
            if left["end_to_end"][rank] is None or right["end_to_end"][rank] is None:
                continue
            pairs.append((bool(left["end_to_end"][rank]), bool(right["end_to_end"][rank])))
        e2e[rank] = _delta(pairs)
    conditional: dict[str, Any] = {}
    keys = {"at1": ("at1_comparable", "at1_hit"), "at_k": ("at_k_comparable", "at_k_hit")}
    for rank, (comparable_key, hit_key) in keys.items():
        pairs = []
        for left, right in zip(baseline_values, candidate_values, strict=True):
            left_surface = left["conditional_surface_given_acceptable_first_segment_boundary"]
            right_surface = right["conditional_surface_given_acceptable_first_segment_boundary"]
            if not left_surface[comparable_key] or not right_surface[comparable_key]:
                continue
            pairs.append((bool(left_surface[hit_key]), bool(right_surface[hit_key])))
        conditional[rank] = _delta(pairs)
    return {
        "baseline": baseline,
        "candidate": candidate,
        "first_segment_boundary": boundary,
        "conditional_surface_on_mutually_comparable_cases": conditional,
        "end_to_end": e2e,
    }


def _script_features(reading: str) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    for character in reading:
        codepoint = ord(character)
        category = unicodedata.category(character)
        name = unicodedata.name(character, "")
        if 0x3040 <= codepoint <= 0x309F:
            script = "hiragana"
        elif (
            0x30A0 <= codepoint <= 0x30FF
            or 0x31F0 <= codepoint <= 0x31FF
            or 0xFF66 <= codepoint <= 0xFF9D
        ):
            script = "katakana"
        elif (
            0x3400 <= codepoint <= 0x4DBF
            or 0x4E00 <= codepoint <= 0x9FFF
            or 0xF900 <= codepoint <= 0xFAFF
            or 0x20000 <= codepoint <= 0x323AF
        ):
            script = "han"
        elif "LATIN" in name and category.startswith("L"):
            script = "latin"
        elif character.isdigit():
            script = "digit"
        elif character.isspace():
            script = "whitespace"
        elif category[:1] in {"P", "S"}:
            script = "punctuation_or_symbol"
        else:
            script = "other"
        counts[script] += 1
    ordered = (
        "hiragana",
        "katakana",
        "han",
        "latin",
        "digit",
        "whitespace",
        "punctuation_or_symbol",
        "other",
    )
    return {
        "code_point_count": len(reading),
        "utf8_byte_count": len(reading.encode("utf-8")),
        "script_counts": {key: counts.get(key, 0) for key in ordered},
        "scripts_present": [key for key in ordered if counts.get(key, 0)],
        "contains_ascii": any(character.isascii() for character in reading),
    }


def _score_features(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [
        {
            "rank": candidate["rank"],
            "zenzai_score": candidate["zenzai_score"],
            "zenzai_score_token_count": candidate[
                "zenzai_score_token_count"
            ],
            "zenzai_score_scope": candidate["zenzai_score_scope"],
            "zenzai_score_per_token": (
                candidate["zenzai_score"]
                / candidate["zenzai_score_token_count"]
            ),
        }
        for candidate in candidates
        if candidate["zenzai_score"] is not None
    ]
    influence_counts = Counter(
        candidate["ranking_influence"] for candidate in candidates
    )
    scope_counts = Counter(
        candidate["zenzai_score_scope"] for candidate in scored
    )
    return {
        "score_available": bool(scored),
        "scored_candidate_count": len(scored),
        "scored_candidates": scored,
        "top_ranked_scored_candidate": scored[0] if scored else None,
        "score_scope_counts": {
            scope: scope_counts.get(scope, 0)
            for scope in ("full_candidate", "constraint_suffix")
        },
        "candidate_score_margin": None,
        "candidate_score_margin_unavailable_reason": (
            "Zenzai stops at the first pass per request; scores retained in "
            "one segment result can originate from different requests/scopes"
        ),
        "candidate_count": len(candidates),
        "ranking_influence_counts": {
            key: influence_counts.get(key, 0)
            for key in ("standard", "zenzai")
        },
        "top1_ranking_influence": (
            candidates[0]["ranking_influence"] if candidates else None
        ),
    }


def _override_trigger_scope(
    mozc: list[dict[str, Any]], hazkey: list[dict[str, Any]]
) -> dict[str, Any]:
    """Split runtime-only Top-1 override scopes without consulting gold."""

    mozc_top = mozc[0] if mozc else None
    hazkey_top = hazkey[0] if hazkey else None
    both_available = mozc_top is not None and hazkey_top is not None
    same_boundary = (
        mozc_top["consuming_count"] == hazkey_top["consuming_count"]
        if both_available
        else None
    )
    same_surface = (
        hybrid.normalized_surface(mozc_top["text"])
        == hybrid.normalized_surface(hazkey_top["text"])
        if both_available
        else None
    )
    return {
        "surface_override": {
            "eligible": bool(
                both_available and same_boundary is True and same_surface is False
            ),
            "candidate_surface_source": HAZKEY_ZENZAI_SURFACE_SOURCE,
            "requires_same_top1_boundary": True,
            "requires_different_normalized_top1_surface": True,
        },
        "boundary_override": {
            "eligible": bool(both_available and same_boundary is False),
            "candidate_boundary_source": HAZKEY_BOUNDARY_SOURCE,
            "requires_different_top1_boundary": True,
        },
    }


def _transition(baseline: bool, candidate: bool) -> str:
    if not baseline and candidate:
        return "rescued"
    if baseline and not candidate:
        return "regressed"
    return "both_hit" if baseline else "both_miss"


def _override_gold_outcomes(
    trigger_scope: dict[str, Any],
    outcomes: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Attach gold outcomes after runtime trigger scopes have been frozen."""

    mozc = outcomes[MOZC_SYSTEM]
    hazkey = outcomes[HAZKEY_ZENZAI_SYSTEM]
    surface_triggered = trigger_scope["surface_override"]["eligible"]
    boundary_triggered = trigger_scope["boundary_override"]["eligible"]
    surface_e2e_comparable = (
        surface_triggered
        and mozc["end_to_end"]["at1"] is not None
        and hazkey["end_to_end"]["at1"] is not None
    )
    conditional_mozc = mozc[
        "conditional_surface_given_acceptable_first_segment_boundary"
    ]
    conditional_hazkey = hazkey[
        "conditional_surface_given_acceptable_first_segment_boundary"
    ]
    conditional_comparable = (
        surface_triggered
        and conditional_mozc["at1_comparable"]
        and conditional_hazkey["at1_comparable"]
    )
    boundary_e2e_comparable = (
        boundary_triggered
        and mozc["end_to_end"]["at1"] is not None
        and hazkey["end_to_end"]["at1"] is not None
    )
    return {
        "surface_override": {
            "triggered": surface_triggered,
            "end_to_end_at1": (
                _transition(
                    bool(mozc["end_to_end"]["at1"]),
                    bool(hazkey["end_to_end"]["at1"]),
                )
                if surface_e2e_comparable
                else None
            ),
            "conditional_surface_at1": (
                _transition(
                    bool(conditional_mozc["at1_hit"]),
                    bool(conditional_hazkey["at1_hit"]),
                )
                if conditional_comparable
                else None
            ),
        },
        "boundary_override": {
            "triggered": boundary_triggered,
            "first_segment_boundary_at1": (
                _transition(
                    bool(mozc["first_segment_boundary"]["at1"]),
                    bool(hazkey["first_segment_boundary"]["at1"]),
                )
                if boundary_triggered
                else None
            ),
            "end_to_end_at1": (
                _transition(
                    bool(mozc["end_to_end"]["at1"]),
                    bool(hazkey["end_to_end"]["at1"]),
                )
                if boundary_e2e_comparable
                else None
            ),
        },
    }


def _candidate_difference_features(
    mozc: list[dict[str, Any]], hazkey: list[dict[str, Any]]
) -> dict[str, Any]:
    mozc_keys = {_candidate_runtime_key(candidate) for candidate in mozc}
    hazkey_keys = {_candidate_runtime_key(candidate) for candidate in hazkey}
    mozc_top = mozc[0] if mozc else None
    hazkey_top = hazkey[0] if hazkey else None
    return {
        "normalized_candidate_overlap_count": len(mozc_keys & hazkey_keys),
        "mozc_only_normalized_candidate_count": len(mozc_keys - hazkey_keys),
        "hazkey_zenzai_only_normalized_candidate_count": len(hazkey_keys - mozc_keys),
        "top1_boundary_equal": (
            mozc_top["consuming_count"] == hazkey_top["consuming_count"]
            if mozc_top is not None and hazkey_top is not None
            else None
        ),
        "hazkey_minus_mozc_top1_boundary_elements": (
            hazkey_top["consuming_count"] - mozc_top["consuming_count"]
            if mozc_top is not None and hazkey_top is not None
            else None
        ),
        "top1_normalized_surface_equal": (
            hybrid.normalized_surface(mozc_top["text"])
            == hybrid.normalized_surface(hazkey_top["text"])
            if mozc_top is not None and hazkey_top is not None
            else None
        ),
        "mozc_top1_present_in_hazkey_zenzai": (
            _candidate_runtime_key(mozc_top) in hazkey_keys
            if mozc_top is not None
            else None
        ),
        "hazkey_zenzai_top1_present_in_mozc": (
            _candidate_runtime_key(hazkey_top) in mozc_keys
            if hazkey_top is not None
            else None
        ),
    }


def _serialize_candidate(
    candidate: dict[str, Any], *, backend_origin: str | None = None
) -> dict[str, Any]:
    result = dict(candidate)
    if result.get("zenzai_score") is not None:
        result["zenzai_score_per_token"] = (
            result["zenzai_score"] / result["zenzai_score_token_count"]
        )
    if backend_origin is not None:
        result["backend_origin"] = backend_origin
    return result


def _h0_origin(
    candidate: dict[str, Any],
    mozc: list[dict[str, Any]],
    hazkey: list[dict[str, Any]],
) -> str:
    if any(candidate is value for value in mozc):
        return MOZC_SYSTEM
    if any(candidate is value for value in hazkey):
        return HAZKEY_ZENZAI_SYSTEM
    raise AssertionError("shared H0 merge returned an unknown candidate record")


def _build_case(
    target: dict[str, Any],
    mozc_candidates: list[dict[str, Any]],
    hazkey_candidates: list[dict[str, Any]],
    suggestion_limit: int,
) -> dict[str, Any]:
    h0_candidates, decision = hybrid._merge_boundary_aware_candidate_records(
        hazkey_candidates,
        mozc_candidates,
        suggestion_limit,
        allow_promotion=False,
        width_guard=False,
    )
    outcomes = {
        MOZC_SYSTEM: _gold_outcome(mozc_candidates, target),
        HAZKEY_ZENZAI_SYSTEM: _gold_outcome(hazkey_candidates, target),
        H0_SYSTEM: _gold_outcome(h0_candidates, target),
    }
    trigger_scope = _override_trigger_scope(mozc_candidates, hazkey_candidates)
    if mozc_candidates:
        if not h0_candidates:
            raise AssertionError("H0 removed non-empty Mozc candidates")
        if h0_candidates[0] is not mozc_candidates[0]:
            raise AssertionError("H0 changed non-empty Mozc Top-1")
        if (
            outcomes[H0_SYSTEM]["first_segment_boundary"]["at1"]
            != outcomes[MOZC_SYSTEM]["first_segment_boundary"]["at1"]
        ):
            raise AssertionError("H0 changed Mozc Boundary@1")
        if (
            outcomes[H0_SYSTEM]["end_to_end"]["at1"]
            != outcomes[MOZC_SYSTEM]["end_to_end"]["at1"]
        ):
            raise AssertionError("H0 changed Mozc E2E@1")

    mozc_keys = {_candidate_runtime_key(candidate) for candidate in mozc_candidates}
    added = [
        candidate
        for candidate in h0_candidates
        if _h0_origin(candidate, mozc_candidates, hazkey_candidates)
        == HAZKEY_ZENZAI_SYSTEM
        and _candidate_runtime_key(candidate) not in mozc_keys
    ]
    acceptable_pairs = {
        (chunk["span"]["count"], chunk["surface"])
        for chunk in target["acceptable_first_chunks"]
    }
    return {
        "id": target["id"],
        "stratification": {
            "gold_category": target["category"],
            "surface_evaluation_status": target["surface_evaluation_status"],
        },
        "runtime_features": {
            "reading": target["reading"],
            "reading_shape": _script_features(target["reading"]),
            "observed_candidates": {
                MOZC_SYSTEM: [
                    _serialize_candidate(candidate) for candidate in mozc_candidates
                ],
                HAZKEY_ZENZAI_SYSTEM: [
                    _serialize_candidate(candidate) for candidate in hazkey_candidates
                ],
            },
            "hazkey_zenzai_evidence": _score_features(hazkey_candidates),
            "backend_candidate_difference": _candidate_difference_features(
                mozc_candidates, hazkey_candidates
            ),
            "override_trigger_scope": trigger_scope,
            "h0_runtime_mirror": {
                "policy": dict(H0_POLICY),
                "decision": decision,
                "candidates": [
                    _serialize_candidate(
                        candidate,
                        backend_origin=_h0_origin(
                            candidate, mozc_candidates, hazkey_candidates
                        ),
                    )
                    for candidate in h0_candidates
                ],
                "new_hazkey_zenzai_candidate_count": len(added),
            },
        },
        "gold_outcomes": {
            "acceptable_first_spans": target["acceptable_first_spans"],
            "acceptable_first_chunks": target["acceptable_first_chunks"],
            "systems": outcomes,
            "override_outcomes": _override_gold_outcomes(
                trigger_scope, outcomes
            ),
            "h0_added_candidate_end_to_end_hit": (
                any(_candidate_gold_key(candidate) in acceptable_pairs for candidate in added)
                if target["surface_evaluation_status"] == "fully_aligned"
                else None
            ),
        },
    }


def _h0_additional_coverage(cases: list[dict[str, Any]]) -> dict[str, Any]:
    windows = [
        case
        for case in cases
        if case["runtime_features"]["h0_runtime_mirror"][
            "new_hazkey_zenzai_candidate_count"
        ]
        > 0
    ]
    added_count = sum(
        case["runtime_features"]["h0_runtime_mirror"][
            "new_hazkey_zenzai_candidate_count"
        ]
        for case in cases
    )
    gold_added = [
        case
        for case in cases
        if case["gold_outcomes"]["h0_added_candidate_end_to_end_hit"] is True
    ]
    return {
        "cases": len(cases),
        "windows_with_new_hazkey_zenzai_candidate": len(windows),
        "new_hazkey_zenzai_candidates": added_count,
        "fully_aligned_windows_with_gold_hit_from_added_candidate": len(gold_added),
        "end_to_end_at_k_vs_mozc": _pairwise_delta(
            cases, MOZC_SYSTEM, H0_SYSTEM
        )["end_to_end"]["at_k"],
        "first_segment_boundary_at_k_vs_mozc": _pairwise_delta(
            cases, MOZC_SYSTEM, H0_SYSTEM
        )["first_segment_boundary"]["at_k"],
    }


def _override_scope_summary(cases: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    outcome_fields = {
        "surface_override": ("end_to_end_at1", "conditional_surface_at1"),
        "boundary_override": (
            "first_segment_boundary_at1",
            "end_to_end_at1",
        ),
    }
    for scope, fields in outcome_fields.items():
        triggered = [
            case
            for case in cases
            if case["runtime_features"]["override_trigger_scope"][scope]["eligible"]
        ]
        result[scope] = {
            "triggered_cases": len(triggered),
            "outcomes": {
                field: dict(
                    Counter(
                        case["gold_outcomes"]["override_outcomes"][scope][field]
                        for case in triggered
                        if case["gold_outcomes"]["override_outcomes"][scope][field]
                        is not None
                    )
                )
                for field in fields
            },
        }
    return result


def _score_evidence_summary(cases: list[dict[str, Any]]) -> dict[str, Any]:
    candidates = [
        candidate
        for case in cases
        for candidate in case["runtime_features"]["observed_candidates"][
            HAZKEY_ZENZAI_SYSTEM
        ]
    ]
    scored = [
        candidate
        for candidate in candidates
        if candidate["zenzai_score"] is not None
    ]
    scope_counts = Counter(
        candidate["zenzai_score_scope"] for candidate in scored
    )
    raw_scores = [candidate["zenzai_score"] for candidate in scored]
    per_token_scores = [
        candidate["zenzai_score_per_token"] for candidate in scored
    ]

    def distribution(values: list[float]) -> dict[str, Any]:
        return {
            "count": len(values),
            "minimum": min(values) if values else None,
            "median": statistics.median(values) if values else None,
            "maximum": max(values) if values else None,
        }

    return {
        "candidates": len(candidates),
        "ranking_influenced_candidates": sum(
            candidate["ranking_influence"] == "zenzai"
            for candidate in candidates
        ),
        "scored_candidates": len(scored),
        "cases_with_score": sum(
            bool(
                case["runtime_features"]["hazkey_zenzai_evidence"][
                    "score_available"
                ]
            )
            for case in cases
        ),
        "top1_scored_cases": sum(
            bool(
                case["runtime_features"]["observed_candidates"][
                    HAZKEY_ZENZAI_SYSTEM
                ]
            )
            and case["runtime_features"]["observed_candidates"][
                HAZKEY_ZENZAI_SYSTEM
            ][0]["zenzai_score"]
            is not None
            for case in cases
        ),
        "score_scope_counts": {
            scope: scope_counts.get(scope, 0)
            for scope in ("full_candidate", "constraint_suffix")
        },
        "score_distributions_by_scope": {
            scope: {
                "raw": distribution([
                    candidate["zenzai_score"]
                    for candidate in scored
                    if candidate["zenzai_score_scope"] == scope
                ]),
                "per_token": distribution([
                    candidate["zenzai_score_per_token"]
                    for candidate in scored
                    if candidate["zenzai_score_scope"] == scope
                ]),
            }
            for scope in ("full_candidate", "constraint_suffix")
        },
        "full_candidate_standard_provenance_scores": sum(
            candidate["zenzai_score_scope"] == "full_candidate"
            and candidate["provenance"] == "standard"
            for candidate in scored
        ),
        "raw_score_distribution": distribution(raw_scores),
        "score_per_token_distribution": distribution(per_token_scores),
        "candidate_score_margin_available": False,
    }


def _summary(cases: list[dict[str, Any]]) -> dict[str, Any]:
    system_metrics = {system: _system_metrics(cases, system) for system in SYSTEMS}
    pairs = (
        (MOZC_SYSTEM, HAZKEY_ZENZAI_SYSTEM),
        (MOZC_SYSTEM, H0_SYSTEM),
        (HAZKEY_ZENZAI_SYSTEM, H0_SYSTEM),
    )
    return {
        "cases": len(cases),
        "systems": system_metrics,
        "pairwise_rescue_regression": {
            f"{candidate}_vs_{baseline}": _pairwise_delta(
                cases, baseline, candidate
            )
            for baseline, candidate in pairs
        },
        "h0_additional_coverage": _h0_additional_coverage(cases),
        "override_scope_diagnostics": _override_scope_summary(cases),
        "zenzai_score_evidence": _score_evidence_summary(cases),
    }


def evaluate(
    generation_manifest: Path,
    targets_path: Path,
    mozc_v6_path: Path,
    hazkey_zenzai_v6_path: Path,
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

    mozc_bytes = acceptable._read_regular(mozc_v6_path, "Mozc ABProbe v6 results")
    hazkey_bytes = acceptable._read_regular(
        hazkey_zenzai_v6_path, "Hazkey+Zenzai ABProbe v6 results"
    )
    mozc_run = _load_v6_run(mozc_bytes, mozc_v6_path, "mozc")
    hazkey_run = _load_v6_run(
        hazkey_bytes, hazkey_zenzai_v6_path, "hazkey"
    )
    _validate_runs(targets, manifest, mozc_run, hazkey_run)

    cases = [
        _build_case(
            target,
            mozc_run["cases"][target["id"]]["candidates"],
            hazkey_run["cases"][target["id"]]["candidates"],
            mozc_run["top_k"],
        )
        for target in targets
    ]
    category_cases: dict[str, list[dict[str, Any]]] = {}
    for case in cases:
        category_cases.setdefault(
            case["stratification"]["gold_category"], []
        ).append(case)
    return {
        "schema": OUTPUT_SCHEMA,
        "diagnostic_only": True,
        "formal_authorized": False,
        "evaluation_scope": {
            "candidate_scope": "first-segment-abprobe-v6",
            "surface_scope": "fully-aligned-first-segment-pairs-only",
            "boundary_and_end_to_end_share_identical_target_denominators": True,
            "conditional_surface_denominator": (
                "system-specific cases whose predicted boundary is acceptable"
            ),
            "pairwise_conditional_surface_denominator": (
                "mutually-comparable acceptable-boundary cases"
            ),
            "h0_is_derived_runtime_mirror": True,
            "promotion_policies_included": [],
        },
        "feature_contract": {
            "runtime_features_exclude_gold_category": True,
            "gold_category_usage": "stratification-only",
            "zenzai_score_missing_is_valid": True,
            "score_margin_available": False,
            "score_margin_reason": (
                "first-pass early return and cross-request score scopes"
            ),
            "score_length_normalization": "score-divided-by-scored-token-count",
            "score_scope_values": ["full_candidate", "constraint_suffix"],
            "dynamic_runtime_dependency_identity_bound": False,
            "score_analysis_preferred_subset": (
                "full_candidate scope with standard provenance; this is not "
                "a safety guarantee because learned-token override evidence "
                "is not separately exposed"
            ),
            "override_scopes": (
                "runtime-only-trigger-scope-separated-from-gold-outcome"
            ),
            "surface_override_candidate_source": HAZKEY_ZENZAI_SURFACE_SOURCE,
            "boundary_override_candidate_source": HAZKEY_BOUNDARY_SOURCE,
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
            "mozc_v6": {
                "path": str(mozc_v6_path),
                "sha256": acceptable._sha256(mozc_bytes),
                "producer": mozc_run["producer"],
                "resource": mozc_run["resource"],
                "quality_policy": mozc_run["quality_policy"],
            },
            "hazkey_zenzai_v6": {
                "path": str(hazkey_zenzai_v6_path),
                "sha256": acceptable._sha256(hazkey_bytes),
                "producer": hazkey_run["producer"],
                "resource": hazkey_run["resource"],
                "quality_policy": hazkey_run["quality_policy"],
            },
            "paired_acquisition": {
                "source_ref": mozc_run["source_ref"],
                "top_k": mozc_run["top_k"],
                "warmups": mozc_run["warmups"],
                "iterations": mozc_run["iterations"],
            },
        },
        "h0_policy": dict(H0_POLICY),
        "all_cases": _summary(cases),
        "stratification_by_gold_category": {
            category: _summary(values)
            for category, values in sorted(category_cases.items())
        },
        "cases": cases,
        "decision": {
            "status": "inconclusive",
            "formal_authorized": False,
            "production_policy_retained": H0_POLICY["id"],
            "reason": (
                "known diagnostic corpus; this comparison cannot authorize a "
                "score/category gate or Top-1 promotion"
            ),
            "formal_blockers": [
                "known corpus is not an unseen locked holdout",
                "dynamic llama/GGML runtime dependency identities are not bound",
                "ABProbe uses empty left/right context",
                "learned-token score override evidence is not separately exposed",
            ],
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate paired ABProbe v6 Mozc and Hazkey+Zenzai runs plus "
            "the derived production H0 merge."
        )
    )
    parser.add_argument("--generation-manifest", type=Path, required=True)
    parser.add_argument("--targets", type=Path, required=True)
    parser.add_argument("--mozc-v6", type=Path, required=True)
    parser.add_argument("--hazkey-zenzai-v6", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = evaluate(
            args.generation_manifest,
            args.targets,
            args.mozc_v6,
            args.hazkey_zenzai_v6,
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
