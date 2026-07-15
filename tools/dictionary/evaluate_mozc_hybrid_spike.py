#!/usr/bin/env python3
"""Evaluate a diagnostic Mozc-first hybrid policy against paired ABProbe runs.

This evaluator is deliberately offline and diagnostic-only.  It reports both
the runtime H0 policy (Mozc Top-1 is never promoted away) and the diagnostic H1
one-sided-consensus policy. It consumes one
corpus snapshot and complete paired Hazkey/Mozc ABProbe v3, v4, or v5 runs,
checks that all three inputs describe the same cases, then applies a policy
that does not consult the corpus expectations:

* Keep Mozc Top-1 unless one-sided cross-backend consensus favors Hazkey.
* One-sided consensus means Hazkey Top-1 occurs below Top-1 in Mozc while
  Mozc Top-1 does not occur in Hazkey.
* If Mozc is empty, fall back to Hazkey.
* Without promotion, keep Mozc Top-3 stable, append unique Hazkey candidates,
  then append the remaining Mozc candidates.
* With promotion, put Hazkey Top-1 first, retain Mozc order, then append the
  remaining Hazkey candidates.

Candidate deduplication uses Unicode NFC surface equality.  Quality matching
uses the exact-surface semantics of ``evaluate_conversion_quality.py``. For
v5, the exact candidate ``consuming_count`` must also match the explicit whole
composition span, and cases where Mozc Top-1 consumes only a prefix remain
incomparable. A diagnostic width guard suppresses H1 promotions caused only by
full-width ASCII forms; it does not use general NFKC folding.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
import unicodedata
from typing import Any, Iterable

try:
    from .compare_conversion_quality import expected_rank
    from .evaluate_conversion_quality import load_corpus_bytes
    from .summarize_ab_probe import (
        INPUT_SCHEMA_V3,
        INPUT_SCHEMA_V4,
        INPUT_SCHEMA_V5,
        SEGMENT_CANDIDATES_PATH,
        load_run_bytes,
    )
except ImportError:  # Direct execution from tools/dictionary.
    from compare_conversion_quality import expected_rank
    from evaluate_conversion_quality import load_corpus_bytes
    from summarize_ab_probe import (
        INPUT_SCHEMA_V3,
        INPUT_SCHEMA_V4,
        INPUT_SCHEMA_V5,
        SEGMENT_CANDIDATES_PATH,
        load_run_bytes,
    )


OUTPUT_SCHEMA = "hazkey.mozc-hybrid-spike-evaluation.v3"
POLICY_ID = "mozc-first-one-sided-consensus-v1"
WIDTH_GUARDED_POLICY_ID = "mozc-first-one-sided-consensus-width-guard-v1"
MOZC_STABLE_PREFIX = 3

PROMOTION_DECISION = "promote_hazkey_one_sided_consensus"
BOUNDARY_REJECTED_DECISION = "keep_mozc_hazkey_top1_boundary_mismatch"
WIDTH_EQUIVALENT_DECISION = "keep_mozc_width_equivalent_top1"
FORMAL_V2_QUALITY_CATEGORY_POLICY_ID = (
    "mozc-adoption-v2-quality-categories-v1"
)
FORMAL_V2_QUALITY_CATEGORIES = (
    "technical-mixed",
    "proper-noun",
    "colloquial",
    "homophone-context",
    "long-structural",
    "grimodex-regression",
)
PROMOTION_OUTCOMES = (
    "rescued",
    "regressed",
    "unchanged_correct",
    "unchanged_incorrect",
)

HAZKEY_TOP1_RESCUE = "hazkey_top1_rescue"
BELOW_TOP1_BOTH = "below_top1_both"
BELOW_TOP1_HAZKEY_ONLY = "below_top1_hazkey_only"
BELOW_TOP1_MOZC_ONLY = "below_top1_mozc_only"
BOTH_ABSENT = "both_absent"
MISS_CLASSES = (
    HAZKEY_TOP1_RESCUE,
    BELOW_TOP1_BOTH,
    BELOW_TOP1_HAZKEY_ONLY,
    BELOW_TOP1_MOZC_ONLY,
    BOTH_ABSENT,
)


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def normalized_surface(value: str) -> str:
    """Return the product-compatible canonical surface used for deduping."""

    return unicodedata.normalize("NFC", value)


def width_folded_surface(value: str) -> str:
    """Fold only full-width ASCII forms after the product NFC normalization."""

    folded: list[str] = []
    for character in normalized_surface(value):
        codepoint = ord(character)
        if 0xFF01 <= codepoint <= 0xFF5E:
            folded.append(chr(codepoint - 0xFEE0))
        elif codepoint == 0x3000:
            folded.append(" ")
        else:
            folded.append(character)
    return "".join(folded)


def _candidate_texts(candidates: list[Any], schema: str) -> list[str]:
    if schema in (INPUT_SCHEMA_V4, INPUT_SCHEMA_V5):
        return [candidate["text"] for candidate in candidates]
    return candidates


def _boundary_eligible_hazkey_candidates(
    hazkey: list[dict[str, Any]], mozc: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    if not mozc:
        return hazkey
    boundary = mozc[0]["consuming_count"]
    return [
        candidate
        for candidate in hazkey
        if candidate["consuming_count"] == boundary
    ]


def _unique_boundary_candidate_records(
    groups: Iterable[Iterable[dict[str, Any]]], suggestion_limit: int
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()
    for group in groups:
        for candidate in group:
            key = (
                candidate["consuming_count"],
                normalized_surface(candidate["text"]),
            )
            if key in seen:
                continue
            seen.add(key)
            candidates.append(candidate)
            if len(candidates) == suggestion_limit:
                return candidates
    return candidates


def _unique_boundary_candidates(
    groups: Iterable[Iterable[dict[str, Any]]], suggestion_limit: int
) -> list[str]:
    return [
        candidate["text"]
        for candidate in _unique_boundary_candidate_records(
            groups, suggestion_limit
        )
    ]


def _boundary_promotion_decision(
    hazkey: list[dict[str, Any]], mozc: list[dict[str, Any]]
) -> str:
    """Mirror the runtime H1 decision over validated ABProbe v4 candidates."""

    if not mozc:
        return "hazkey_fallback_mozc_empty" if hazkey else "no_candidates"
    if not hazkey:
        return "keep_mozc_hazkey_empty"

    boundary = mozc[0]["consuming_count"]
    hazkey_top = hazkey[0]
    if hazkey_top["consuming_count"] != boundary:
        return BOUNDARY_REJECTED_DECISION

    hazkey_top_surface = normalized_surface(hazkey_top["text"])
    hazkey_top_below_mozc = any(
        candidate["consuming_count"] == boundary
        and normalized_surface(candidate["text"]) == hazkey_top_surface
        for candidate in mozc[1:]
    )
    mozc_top_surface = normalized_surface(mozc[0]["text"])
    mozc_top_in_hazkey = any(
        candidate["consuming_count"] == boundary
        and normalized_surface(candidate["text"]) == mozc_top_surface
        for candidate in hazkey
    )
    return (
        PROMOTION_DECISION
        if hazkey_top_below_mozc and not mozc_top_in_hazkey
        else "keep_mozc_top1"
    )


def _width_guarded_boundary_promotion_decision(
    hazkey: list[dict[str, Any]], mozc: list[dict[str, Any]]
) -> str:
    """Suppress H1 when the two Top-1 values differ only by ASCII width."""

    decision = _boundary_promotion_decision(hazkey, mozc)
    if (
        decision == PROMOTION_DECISION
        and width_folded_surface(hazkey[0]["text"])
        == width_folded_surface(mozc[0]["text"])
    ):
        return WIDTH_EQUIVALENT_DECISION
    return decision


def _merge_boundary_aware_candidate_records(
    hazkey: list[dict[str, Any]],
    mozc: list[dict[str, Any]],
    suggestion_limit: int,
    *,
    allow_promotion: bool,
    width_guard: bool = False,
) -> tuple[list[dict[str, Any]], str]:
    """Apply runtime boundary filtering and optionally the diagnostic H1."""

    if not mozc:
        return (
            hazkey[:suggestion_limit],
            "hazkey_fallback_mozc_empty" if hazkey else "no_candidates",
        )
    if not hazkey:
        return (
            mozc[:suggestion_limit],
            "keep_mozc_hazkey_empty",
        )

    eligible_hazkey = _boundary_eligible_hazkey_candidates(hazkey, mozc)
    decision = (
        _width_guarded_boundary_promotion_decision(hazkey, mozc)
        if width_guard
        else _boundary_promotion_decision(hazkey, mozc)
    )
    if allow_promotion and decision == PROMOTION_DECISION:
        return (
            _unique_boundary_candidate_records(
                ((eligible_hazkey[0],), mozc, eligible_hazkey[1:]),
                suggestion_limit,
            ),
            decision,
        )
    merged = _unique_boundary_candidate_records(
        (
            mozc[:MOZC_STABLE_PREFIX],
            eligible_hazkey,
            mozc[MOZC_STABLE_PREFIX:],
        ),
        suggestion_limit,
    )
    h0_decision = "keep_mozc_top1"
    if allow_promotion and decision in (
        BOUNDARY_REJECTED_DECISION,
        WIDTH_EQUIVALENT_DECISION,
    ):
        return merged, decision
    return merged, h0_decision


def _merge_boundary_aware_candidates(
    hazkey: list[dict[str, Any]],
    mozc: list[dict[str, Any]],
    suggestion_limit: int,
    *,
    allow_promotion: bool,
    width_guard: bool = False,
) -> tuple[list[str], str]:
    records, decision = _merge_boundary_aware_candidate_records(
        hazkey,
        mozc,
        suggestion_limit,
        allow_promotion=allow_promotion,
        width_guard=width_guard,
    )
    return [candidate["text"] for candidate in records], decision


def _unique_surfaces(
    groups: Iterable[Iterable[str]], suggestion_limit: int
) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for candidate in group:
            normalized = normalized_surface(candidate)
            if normalized in seen:
                continue
            seen.add(normalized)
            candidates.append(candidate)
            if len(candidates) == suggestion_limit:
                return candidates
    return candidates


def merge_candidates(
    hazkey: list[str], mozc: list[str], suggestion_limit: int
) -> tuple[list[str], str]:
    """Apply the expectation-blind conservative Mozc-first merge policy."""

    if suggestion_limit < 1:
        raise ValueError("suggestion_limit must be positive")
    if not mozc:
        if not hazkey:
            return [], "no_candidates"
        return (
            _unique_surfaces((hazkey,), suggestion_limit),
            "hazkey_fallback_mozc_empty",
        )
    if not hazkey:
        return (
            _unique_surfaces((mozc,), suggestion_limit),
            "keep_mozc_hazkey_empty",
        )

    hazkey_top = normalized_surface(hazkey[0])
    mozc_top = normalized_surface(mozc[0])
    hazkey_top_below_mozc = any(
        normalized_surface(candidate) == hazkey_top for candidate in mozc[1:]
    )
    mozc_top_in_hazkey = any(
        normalized_surface(candidate) == mozc_top for candidate in hazkey
    )
    if hazkey_top_below_mozc and not mozc_top_in_hazkey:
        return (
            _unique_surfaces(
                ((hazkey[0],), mozc, hazkey[1:]), suggestion_limit
            ),
            "promote_hazkey_one_sided_consensus",
        )

    return (
        _unique_surfaces(
            (
                mozc[:MOZC_STABLE_PREFIX],
                hazkey,
                mozc[MOZC_STABLE_PREFIX:],
            ),
            suggestion_limit,
        ),
        "keep_mozc_top1",
    )


def merge_candidates_preserve_mozc_top1(
    hazkey: list[str], mozc: list[str], suggestion_limit: int
) -> tuple[list[str], str]:
    """Apply the deployed H0 order without any Hazkey Top-1 promotion."""

    if suggestion_limit < 1:
        raise ValueError("suggestion_limit must be positive")
    if not mozc:
        if not hazkey:
            return [], "no_candidates"
        return (
            _unique_surfaces((hazkey,), suggestion_limit),
            "hazkey_fallback_mozc_empty",
        )
    if not hazkey:
        return (
            _unique_surfaces((mozc,), suggestion_limit),
            "keep_mozc_hazkey_empty",
        )
    return (
        _unique_surfaces(
            (
                mozc[:MOZC_STABLE_PREFIX],
                hazkey,
                mozc[MOZC_STABLE_PREFIX:],
            ),
            suggestion_limit,
        ),
        "keep_mozc_top1",
    )


def _structured_expected_rank(
    expected: list[str],
    expected_consuming_count: int,
    candidates: list[dict[str, Any]],
) -> int | None:
    """Match both the exact surface and its composition-element span."""

    for rank, candidate in enumerate(candidates, 1):
        if (
            candidate["consuming_count"] == expected_consuming_count
            and candidate["text"] in expected
        ):
            return rank
    return None


def _validate_reviewed_first_segment_targets(
    targets: dict[str, dict[str, Any]],
    corpus: list[dict[str, str]],
    mozc_run: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Validate expectation-only first-segment labels against ABProbe v5 spans."""

    corpus_ids = {row["id"] for row in corpus}
    target_ids = set(targets)
    if target_ids != corpus_ids:
        raise ValueError(
            "reviewed first-segment target set does not match the corpus; "
            f"missing={sorted(corpus_ids - target_ids)!r}, "
            f"unexpected={sorted(target_ids - corpus_ids)!r}"
        )

    validated: dict[str, dict[str, Any]] = {}
    for row in corpus:
        case_id = row["id"]
        context = f"reviewed first-segment target for {case_id!r}"
        target = targets[case_id]
        if not isinstance(target, dict):
            raise ValueError(f"{context} must be an object")
        expected_target_fields = {"span", "surfaces"}
        actual_target_fields = set(target)
        if actual_target_fields != expected_target_fields:
            raise ValueError(
                f"{context} must contain exactly span and surfaces; "
                f"missing={sorted(expected_target_fields - actual_target_fields)!r}, "
                f"unexpected={sorted(actual_target_fields - expected_target_fields)!r}"
            )

        span = target["span"]
        if not isinstance(span, dict):
            raise ValueError(f"{context}.span must be an object")
        expected_span_fields = {"start", "count", "unit"}
        actual_span_fields = set(span)
        if actual_span_fields != expected_span_fields:
            raise ValueError(
                f"{context}.span must contain exactly start, count, and unit; "
                f"missing={sorted(expected_span_fields - actual_span_fields)!r}, "
                f"unexpected={sorted(actual_span_fields - expected_span_fields)!r}"
            )
        start = span["start"]
        count = span["count"]
        unit = span["unit"]
        if isinstance(start, bool) or not isinstance(start, int) or start < 0:
            raise ValueError(f"{context}.span.start must be a non-negative integer")
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError(f"{context}.span.count must be a positive integer")
        if unit != "composition_element":
            raise ValueError(
                f"{context}.span.unit must be 'composition_element'"
            )

        composition_span = mozc_run["cases"][case_id]["composition_span"]
        if start != composition_span["start"]:
            raise ValueError(
                f"{context}.span.start must equal composition_span.start"
            )
        if start + count > composition_span["start"] + composition_span["count"]:
            raise ValueError(f"{context}.span exceeds composition_span")

        surfaces = target["surfaces"]
        if (
            not isinstance(surfaces, list)
            or not surfaces
            or any(not isinstance(surface, str) or not surface for surface in surfaces)
        ):
            raise ValueError(
                f"{context}.surfaces must be a non-empty array of non-empty strings"
            )
        if len(surfaces) != len(set(surfaces)):
            raise ValueError(f"{context}.surfaces must not contain duplicates")
        for surface in surfaces:
            if surface != normalized_surface(surface):
                raise ValueError(f"{context}.surfaces must be NFC-normalized")
            if any(
                unicodedata.category(character) == "Cc"
                or character == "\ufeff"
                for character in surface
            ):
                raise ValueError(
                    f"{context}.surfaces must not contain control characters"
                )
        validated[case_id] = {
            "span": {"start": start, "count": count, "unit": unit},
            "surfaces": list(surfaces),
        }
    return validated


def _whole_span_target_count(
    input_schema: str,
    mozc_records: list[dict[str, Any]],
    composition_span: dict[str, Any] | None,
) -> int | None:
    """Return the explicit whole-input span only when Mozc targets all of it."""

    if (
        input_schema != INPUT_SCHEMA_V5
        or composition_span is None
        or not mozc_records
    ):
        return None
    count = composition_span["count"]
    return count if mozc_records[0]["consuming_count"] == count else None


def classify_mozc_top1_miss(
    hazkey_rank: int | None, mozc_rank: int | None
) -> str:
    """Return one disjoint, exhaustive class for a Mozc Top-1 miss."""

    if mozc_rank == 1:
        raise ValueError("classification requires a Mozc Top-1 miss")
    if hazkey_rank == 1:
        return HAZKEY_TOP1_RESCUE
    if hazkey_rank is not None and mozc_rank is not None:
        return BELOW_TOP1_BOTH
    if hazkey_rank is not None:
        return BELOW_TOP1_HAZKEY_ONLY
    if mozc_rank is not None:
        return BELOW_TOP1_MOZC_ONLY
    return BOTH_ABSENT


QUALITY_DECOMPOSITION_SYSTEMS = (
    "hazkey",
    "mozc",
    "runtime_h0",
    "h1_hybrid",
    "h2_width_guarded",
)


def _structured_candidate_quality(
    candidates: list[dict[str, Any]],
    expected_surfaces: list[str],
    expected_consuming_count: int,
) -> dict[str, Any]:
    """Describe raw-exact Top-1 and Top-K quality against a reviewed segment."""

    top1 = candidates[0] if candidates else None
    predicted_count = top1["consuming_count"] if top1 is not None else None
    if predicted_count is None:
        boundary_classification = "missing_candidate"
        element_delta = None
    else:
        element_delta = predicted_count - expected_consuming_count
        if element_delta == 0:
            boundary_classification = "matches_reviewed_boundary"
        elif element_delta < 0:
            boundary_classification = "ends_before_reviewed_boundary"
        else:
            boundary_classification = "ends_after_reviewed_boundary"
    boundary_correct = element_delta == 0
    surface_correct = bool(
        top1 is not None and top1["text"] in expected_surfaces
    )
    end_to_end_correct = boundary_correct and surface_correct
    top_k_boundary_correct = any(
        candidate["consuming_count"] == expected_consuming_count
        for candidate in candidates
    )
    top_k_end_to_end_correct = any(
        candidate["consuming_count"] == expected_consuming_count
        and candidate["text"] in expected_surfaces
        for candidate in candidates
    )
    return {
        "top1": {
            "candidate_present": top1 is not None,
            "surface": top1["text"] if top1 is not None else None,
            "predicted_consuming_count": predicted_count,
            "boundary": {
                "correct": boundary_correct,
                "classification": boundary_classification,
                "element_delta": element_delta,
            },
            "raw_exact_surface_correct": surface_correct,
            "end_to_end_correct": end_to_end_correct,
        },
        "top_k": {
            "candidate_count": len(candidates),
            "boundary_correct": top_k_boundary_correct,
            "raw_exact_surface_with_correct_boundary": (
                top_k_end_to_end_correct
            ),
            "end_to_end_correct": top_k_end_to_end_correct,
        },
    }


def _element_delta_summary(values: list[int]) -> dict[str, Any]:
    absolute_values = [abs(value) for value in values]
    return {
        "definition": (
            "predicted_consuming_count - reviewed_consuming_count"
        ),
        "sum": sum(values),
        "absolute_sum": sum(absolute_values),
        "mean_absolute": (
            sum(absolute_values) / len(absolute_values)
            if absolute_values
            else None
        ),
        "minimum": min(values) if values else None,
        "maximum": max(values) if values else None,
    }


def _quality_decomposition_system_view(
    comparable_cases: list[dict[str, Any]], system: str
) -> dict[str, Any]:
    evidence = [case["top1_quality"]["systems"][system] for case in comparable_cases]
    total = len(evidence)
    top1 = [item["top1"] for item in evidence]
    boundary_hits = sum(item["boundary"]["correct"] for item in top1)
    end_to_end_hits = sum(item["end_to_end_correct"] for item in top1)
    missing = sum(not item["candidate_present"] for item in top1)
    before_deltas = [
        item["boundary"]["element_delta"]
        for item in top1
        if item["boundary"]["classification"]
        == "ends_before_reviewed_boundary"
    ]
    after_deltas = [
        item["boundary"]["element_delta"]
        for item in top1
        if item["boundary"]["classification"]
        == "ends_after_reviewed_boundary"
    ]
    if boundary_hits + missing + len(before_deltas) + len(after_deltas) != total:
        raise AssertionError("Top-1 boundary decomposition is not exhaustive")

    top_k = [item["top_k"] for item in evidence]
    top_k_boundary_hits = sum(item["boundary_correct"] for item in top_k)
    top_k_end_to_end_hits = sum(item["end_to_end_correct"] for item in top_k)
    return {
        "top1": {
            "boundary": {
                "cases": total,
                "hits": boundary_hits,
                "misses": total - boundary_hits,
                "accuracy": _rate(boundary_hits, total),
                "missing_candidate": missing,
                "ends_before_reviewed_boundary": {
                    "segmentation": "over_segmentation",
                    "count": len(before_deltas),
                    "element_delta": _element_delta_summary(before_deltas),
                },
                "ends_after_reviewed_boundary": {
                    "segmentation": "under_segmentation",
                    "count": len(after_deltas),
                    "element_delta": _element_delta_summary(after_deltas),
                },
            },
            "raw_exact_surface_given_boundary_correct": {
                "diagnostic_conditioned_metric": True,
                "cases": boundary_hits,
                "hits": end_to_end_hits,
                "accuracy": (
                    _rate(end_to_end_hits, boundary_hits)
                    if boundary_hits
                    else None
                ),
            },
            "end_to_end": {
                "primary_product_metric": True,
                "cases": total,
                "hits": end_to_end_hits,
                "accuracy": _rate(end_to_end_hits, total),
            },
        },
        "top_k": {
            "match_semantics": "any_candidate_in_observed_top_k",
            "boundary": {
                "cases": total,
                "hits": top_k_boundary_hits,
                "misses": total - top_k_boundary_hits,
                "accuracy": _rate(top_k_boundary_hits, total),
                "missing_candidate_list": sum(
                    item["candidate_count"] == 0 for item in top_k
                ),
            },
            "raw_exact_surface_given_boundary_correct": {
                "diagnostic_conditioned_metric": True,
                "cases": top_k_boundary_hits,
                "hits": top_k_end_to_end_hits,
                "accuracy": (
                    _rate(top_k_end_to_end_hits, top_k_boundary_hits)
                    if top_k_boundary_hits
                    else None
                ),
            },
            "end_to_end": {
                "secondary_candidate_coverage_metric": True,
                "cases": total,
                "hits": top_k_end_to_end_hits,
                "accuracy": _rate(top_k_end_to_end_hits, total),
            },
        },
    }


def _policy_delta_from_h0(
    comparable_cases: list[dict[str, Any]], policy_system: str
) -> dict[str, Any]:
    rescued = {"boundary_caused": 0, "surface_within_same_boundary": 0}
    regressed = {"boundary_caused": 0, "surface_within_same_boundary": 0}
    boundary_changes = 0
    for case in comparable_cases:
        systems = case["top1_quality"]["systems"]
        h0 = systems["runtime_h0"]["top1"]
        policy = systems[policy_system]["top1"]
        if (
            h0["predicted_consuming_count"]
            != policy["predicted_consuming_count"]
        ):
            boundary_changes += 1
        if h0["end_to_end_correct"] == policy["end_to_end_correct"]:
            continue
        outcome = rescued if policy["end_to_end_correct"] else regressed
        if h0["boundary"]["correct"] != policy["boundary"]["correct"]:
            outcome["boundary_caused"] += 1
        elif h0["boundary"]["correct"] and policy["boundary"]["correct"]:
            outcome["surface_within_same_boundary"] += 1
        else:
            raise AssertionError(
                "E2E policy delta cannot be classified as boundary or surface"
            )
    if boundary_changes != 0:
        raise AssertionError(
            f"{policy_system} changed Top-1 consuming_count relative to H0"
        )
    return {
        "comparison": "end_to_end_top1_vs_runtime_h0",
        "rescues": {"total": sum(rescued.values()), **rescued},
        "regressions": {"total": sum(regressed.values()), **regressed},
        "top1_boundary_changes": {
            "count": boundary_changes,
            "verified_zero": True,
            "invariant": (
                "When Mozc is nonempty, H0/H1/H2 admit Hazkey candidates only "
                "at Mozc Top-1 consuming_count; Mozc-empty fallback is identical."
            ),
        },
    }


def _build_quality_decomposition(
    comparable_cases: list[dict[str, Any]],
) -> dict[str, Any]:
    systems = {
        system: _quality_decomposition_system_view(comparable_cases, system)
        for system in QUALITY_DECOMPOSITION_SYSTEMS
    }
    boundary_groups: Counter[str] = Counter(
        {name: 0 for name in ("both_correct", "mozc_only", "hazkey_only", "neither")}
    )
    boundary_switch_opportunities = 0
    actual_rescueable = 0
    mozc_missing_hazkey_boundary_correct = 0
    mozc_present_cases = 0
    boundary_changes_from_mozc = {
        "runtime_h0": 0,
        "h1_hybrid": 0,
        "h2_width_guarded": 0,
    }
    for case in comparable_cases:
        system_evidence = case["top1_quality"]["systems"]
        hazkey_boundary = system_evidence["hazkey"]["top1"]["boundary"]["correct"]
        mozc_boundary = system_evidence["mozc"]["top1"]["boundary"]["correct"]
        if hazkey_boundary and mozc_boundary:
            group = "both_correct"
        elif mozc_boundary:
            group = "mozc_only"
        elif hazkey_boundary:
            group = "hazkey_only"
        else:
            group = "neither"
        boundary_groups[group] += 1
        if group == "hazkey_only":
            if system_evidence["mozc"]["top1"]["candidate_present"]:
                boundary_switch_opportunities += 1
                if system_evidence["hazkey"]["top1"][
                    "end_to_end_correct"
                ]:
                    actual_rescueable += 1
            else:
                mozc_missing_hazkey_boundary_correct += 1
        mozc_top1 = system_evidence["mozc"]["top1"]
        if mozc_top1["candidate_present"]:
            mozc_present_cases += 1
            mozc_count = mozc_top1["predicted_consuming_count"]
            for system in boundary_changes_from_mozc:
                if system_evidence[system]["top1"][
                    "predicted_consuming_count"
                ] != mozc_count:
                    boundary_changes_from_mozc[system] += 1
    if sum(boundary_groups.values()) != len(comparable_cases):
        raise AssertionError("Hazkey/Mozc boundary groups are not exhaustive")
    if any(boundary_changes_from_mozc.values()):
        raise AssertionError(
            "H0/H1/H2 changed Top-1 consuming_count while Mozc was nonempty"
        )

    for case in comparable_cases:
        ranks = case["expected_rank"]
        expected_system_ranks = {
            "hazkey": ranks["hazkey"],
            "mozc": ranks["mozc"],
            "runtime_h0": ranks["runtime_h0"],
            "h1_hybrid": ranks["hybrid"],
            "h2_width_guarded": ranks["width_guarded_hybrid"],
        }
        for system, rank in expected_system_ranks.items():
            observed = case["top1_quality"]["systems"][system]["top1"][
                "end_to_end_correct"
            ]
            if observed != (rank == 1):
                raise AssertionError(
                    f"{system} E2E Top-1 disagrees with existing expected_rank"
                )

    return {
        "metric_contract": {
            "primary_product_metric": "end_to_end_top1",
            "surface_metric_role": "diagnostic_conditioned_on_boundary_correct",
            "surface_match": "raw_exact",
            "boundary_match": "reviewed_composition_element_count",
            "missing_candidate_is_boundary_miss": True,
            "element_delta": (
                "predicted_consuming_count - reviewed_consuming_count"
            ),
        },
        "cases": len(comparable_cases),
        "systems": systems,
        "hazkey_mozc_top1_boundary_comparison": {
            "exhaustive": True,
            "disjoint": True,
            "groups": dict(boundary_groups),
            "boundary_switch_opportunity": {
                "definition": (
                    "Mozc Top-1 is present with a wrong boundary and Hazkey "
                    "Top-1 has the reviewed boundary"
                ),
                "count": boundary_switch_opportunities,
            },
            "actual_top1_rescueable": {
                "definition": (
                    "Mozc Top-1 is present with a wrong boundary while Hazkey "
                    "boundary and raw-exact surface are both correct"
                ),
                "count": actual_rescueable,
            },
            "mozc_top1_missing_hazkey_boundary_correct": {
                "definition": (
                    "Mozc has no Top-1 candidate and Hazkey Top-1 has the "
                    "reviewed boundary; H0 already uses Hazkey fallback"
                ),
                "count": mozc_missing_hazkey_boundary_correct,
            },
        },
        "policy_delta_vs_runtime_h0": {
            "h1_hybrid": _policy_delta_from_h0(
                comparable_cases, "h1_hybrid"
            ),
            "h2_width_guarded": _policy_delta_from_h0(
                comparable_cases, "h2_width_guarded"
            ),
        },
        "boundary_preservation_invariant": {
            "scope": "cases_with_mozc_top1",
            "eligible_cases": mozc_present_cases,
            "expected_changed_count": 0,
            "observed_changed_count": boundary_changes_from_mozc,
            "established": True,
            "reason": (
                "H0/H1/H2 admit Hazkey candidates only at Mozc Top-1 "
                "consuming_count when Mozc is nonempty."
            ),
        },
    }


def _build_v5_quality_view(
    comparable_cases: list[dict[str, Any]],
    *,
    aggregation_scope: str,
    formal_quality_categories_only: bool,
    excluded_target_incomparable_cases: int,
    target_scope: str = "explicit_whole_composition_span",
    include_quality_decomposition: bool = False,
) -> dict[str, Any]:
    """Aggregate an explicitly labeled v5 diagnostic or formal-category view."""

    total = len(comparable_cases)
    ranks = [case["expected_rank"] for case in comparable_cases]
    hazkey_hits = sum(rank["hazkey"] == 1 for rank in ranks)
    mozc_hits = sum(rank["mozc"] == 1 for rank in ranks)
    hybrid_hits = sum(rank["hybrid"] == 1 for rank in ranks)
    runtime_h0_hits = sum(rank["runtime_h0"] == 1 for rank in ranks)
    guarded_hits = sum(rank["width_guarded_hybrid"] == 1 for rank in ranks)

    rescued = sum(case["top1_outcome"] == "rescued" for case in comparable_cases)
    regressed = sum(
        case["top1_outcome"] == "regressed" for case in comparable_cases
    )
    runtime_h0_rescued = sum(
        rank["mozc"] != 1 and rank["runtime_h0"] == 1 for rank in ranks
    )
    runtime_h0_regressed = sum(
        rank["mozc"] == 1 and rank["runtime_h0"] != 1 for rank in ranks
    )
    guarded_rescued = sum(
        case["width_guarded_top1_outcome"] == "rescued"
        for case in comparable_cases
    )
    guarded_regressed = sum(
        case["width_guarded_top1_outcome"] == "regressed"
        for case in comparable_cases
    )
    backend_top1_oracle_hits = sum(
        rank["hazkey"] == 1 or rank["mozc"] == 1 for rank in ranks
    )
    candidate_union_oracle_hits = sum(
        rank["hazkey"] is not None or rank["mozc"] is not None for rank in ranks
    )

    miss_counts: Counter[str] = Counter({name: 0 for name in MISS_CLASSES})
    for case in comparable_cases:
        classification = case["mozc_top1_miss_classification"]
        if classification is not None:
            miss_counts[classification] += 1
    mozc_misses = total - mozc_hits
    if sum(miss_counts.values()) != mozc_misses:
        raise AssertionError("v5 quality-view miss classification is inconsistent")

    metadata = {
        "diagnostic_only": True,
        "formal_authorized": False,
        "aggregation_scope": aggregation_scope,
        "formal_quality_categories_only": formal_quality_categories_only,
        "target_comparable": total > 0,
        "scope": target_scope,
        "excluded_target_incomparable_cases": excluded_target_incomparable_cases,
    }
    below_both = miss_counts[BELOW_TOP1_BOTH]
    below_hazkey_only = miss_counts[BELOW_TOP1_HAZKEY_ONLY]
    below_mozc_only = miss_counts[BELOW_TOP1_MOZC_ONLY]
    below_total = below_both + below_hazkey_only + below_mozc_only
    result = {
        "top1": {
            **metadata,
            "cases": total,
            "hazkey": {"hits": hazkey_hits, "rate": _rate(hazkey_hits, total)},
            "mozc": {"hits": mozc_hits, "rate": _rate(mozc_hits, total)},
            "hybrid": {"hits": hybrid_hits, "rate": _rate(hybrid_hits, total)},
            "rescued": rescued,
            "regressed": regressed,
            "net_hits": rescued - regressed,
            "net_rate": _rate(rescued - regressed, total),
        },
        "runtime_h0_top1": {
            **metadata,
            "cases": total,
            "hits": runtime_h0_hits,
            "rate": _rate(runtime_h0_hits, total),
            "rescued": runtime_h0_rescued,
            "regressed": runtime_h0_regressed,
            "net_hits": runtime_h0_rescued - runtime_h0_regressed,
            "net_rate": _rate(runtime_h0_rescued - runtime_h0_regressed, total),
        },
        "width_guarded_top1": {
            **metadata,
            "cases": total,
            "hits": guarded_hits,
            "rate": _rate(guarded_hits, total),
            "rescued": guarded_rescued,
            "regressed": guarded_regressed,
            "net_hits": guarded_rescued - guarded_regressed,
            "net_rate": _rate(guarded_rescued - guarded_regressed, total),
        },
        "oracle_ceiling": {
            **metadata,
            "backend_top1_union": {
                "definition": "expected is Top-1 in Hazkey or Mozc",
                "hits": backend_top1_oracle_hits,
                "rate": _rate(backend_top1_oracle_hits, total),
                "incremental_hits_over_mozc": backend_top1_oracle_hits - mozc_hits,
            },
            "candidate_union": {
                "definition": (
                    "expected occurs anywhere in either observed candidate window"
                ),
                "hits": candidate_union_oracle_hits,
                "rate": _rate(candidate_union_oracle_hits, total),
                "incremental_hits_over_mozc": candidate_union_oracle_hits - mozc_hits,
            },
        },
        "mozc_top1_miss_classification": {
            **metadata,
            "scope": (
                "exact expected surface and reviewed first-segment span within "
                "observed top_k"
                if target_scope == "reviewed_first_segment"
                else "exact expected surface and composition span within observed top_k"
            ),
            "exhaustive": True,
            "disjoint": True,
            "total": mozc_misses,
            HAZKEY_TOP1_RESCUE: {
                "count": miss_counts[HAZKEY_TOP1_RESCUE],
                "rate_of_mozc_top1_misses": _rate(
                    miss_counts[HAZKEY_TOP1_RESCUE], mozc_misses
                ),
            },
            "below_top1_presence": {
                "count": below_total,
                "rate_of_mozc_top1_misses": _rate(below_total, mozc_misses),
                "both": below_both,
                "hazkey_only": below_hazkey_only,
                "mozc_only": below_mozc_only,
            },
            BOTH_ABSENT: {
                "count": miss_counts[BOTH_ABSENT],
                "rate_of_mozc_top1_misses": _rate(
                    miss_counts[BOTH_ABSENT], mozc_misses
                ),
            },
        },
    }
    if include_quality_decomposition:
        result["quality_decomposition"] = _build_quality_decomposition(
            comparable_cases
        )
    return result


def _validate_inputs(
    corpus: list[dict[str, str]],
    corpus_sha256: str,
    hazkey_run: dict[str, Any],
    mozc_run: dict[str, Any],
    *,
    hazkey_context: str,
    mozc_context: str,
) -> None:
    runs = ((hazkey_context, hazkey_run), (mozc_context, mozc_run))
    for context, run in runs:
        if run["schema"] not in (
            INPUT_SCHEMA_V3,
            INPUT_SCHEMA_V4,
            INPUT_SCHEMA_V5,
        ):
            raise ValueError(
                f"{context}: hybrid spike requires {INPUT_SCHEMA_V3} or "
                f"{INPUT_SCHEMA_V4} or {INPUT_SCHEMA_V5}"
            )
    if hazkey_run["schema"] != mozc_run["schema"]:
        raise ValueError("probe runs must have an identical schema")
    if hazkey_run["schema"] in (INPUT_SCHEMA_V4, INPUT_SCHEMA_V5):
        for context, run in runs:
            if run.get("conversion_path") != SEGMENT_CANDIDATES_PATH:
                raise ValueError(
                    f"{context}: conversion_path must be "
                    f"{SEGMENT_CANDIDATES_PATH!r}"
                )

    if hazkey_run["converter_backend"] != "hazkey":
        raise ValueError(
            f"{hazkey_context}: converter_backend must be 'hazkey', got "
            f"{hazkey_run['converter_backend']!r}"
        )
    if mozc_run["converter_backend"] != "mozc":
        raise ValueError(
            f"{mozc_context}: converter_backend must be 'mozc', got "
            f"{mozc_run['converter_backend']!r}"
        )

    if hazkey_run["source_ref"] != mozc_run["source_ref"]:
        raise ValueError("probe runs must have an identical source_ref")
    consistency_fields = (
        "backend_version",
        "warmups",
        "iterations",
        "top_k",
        "corpus",
    )
    if hazkey_run["schema"] in (INPUT_SCHEMA_V4, INPUT_SCHEMA_V5):
        consistency_fields += ("conversion_path",)
    for field in consistency_fields:
        if hazkey_run[field] != mozc_run[field]:
            raise ValueError(f"probe runs must have an identical {field}")

    expected_corpus = {"sha256": corpus_sha256, "cases": len(corpus)}
    if hazkey_run["corpus"] != expected_corpus:
        raise ValueError("probe corpus provenance does not match the supplied corpus")

    corpus_by_id = {row["id"]: row for row in corpus}
    corpus_ids = set(corpus_by_id)
    for context, run in runs:
        run_ids = set(run["cases"])
        if run_ids != corpus_ids:
            raise ValueError(
                f"{context}: case set does not match corpus; "
                f"missing={sorted(corpus_ids - run_ids)!r}, "
                f"unexpected={sorted(run_ids - corpus_ids)!r}"
            )
        for case_id, row in corpus_by_id.items():
            case = run["cases"][case_id]
            if case["reading"] != row["reading"]:
                raise ValueError(
                    f"{context}: reading for {case_id!r} does not match corpus"
                )
            if case["category"] != row["category"]:
                raise ValueError(
                    f"{context}: category for {case_id!r} does not match corpus"
                )
    if hazkey_run["schema"] == INPUT_SCHEMA_V5:
        for case_id in corpus_by_id:
            hazkey_span = hazkey_run["cases"][case_id]["composition_span"]
            mozc_span = mozc_run["cases"][case_id]["composition_span"]
            if hazkey_span != mozc_span:
                raise ValueError(
                    f"composition_span for {case_id!r} differs between probe runs"
                )


def _input_metadata(data: bytes, run: dict[str, Any]) -> dict[str, Any]:
    metadata = {
        "sha256": _sha256_bytes(data),
        "schema": run["schema"],
        "backend": run["backend"],
        "backend_version": run["backend_version"],
        "converter_backend": run["converter_backend"],
        "source_ref": run["source_ref"],
        "resource": run["resource"],
    }
    if run["schema"] in (INPUT_SCHEMA_V4, INPUT_SCHEMA_V5):
        metadata["conversion_path"] = run["conversion_path"]
    return metadata


def evaluate_runs(
    corpus: list[dict[str, str]],
    hazkey_run: dict[str, Any],
    mozc_run: dict[str, Any],
    *,
    corpus_sha256: str,
    corpus_bytes: bytes,
    hazkey_bytes: bytes,
    mozc_bytes: bytes,
    hazkey_context: str = "hazkey_results",
    mozc_context: str = "mozc_results",
    reviewed_first_segment_targets: dict[str, dict[str, Any]] | None = None,
    formal_quality_categories: Iterable[str] | None = None,
    formal_quality_category_policy_id: str | None = None,
    reviewed_target_metadata: dict[str, Any] | None = None,
    additional_input_metadata: dict[str, Any] | None = None,
    report_schema: str = OUTPUT_SCHEMA,
    new_holdout_required: bool = True,
) -> dict[str, Any]:
    actual_corpus_sha256 = _sha256_bytes(corpus_bytes)
    if corpus_sha256 != actual_corpus_sha256:
        raise ValueError("corpus_sha256 does not match corpus_bytes")
    _validate_inputs(
        corpus,
        actual_corpus_sha256,
        hazkey_run,
        mozc_run,
        hazkey_context=hazkey_context,
        mozc_context=mozc_context,
    )
    top_k = hazkey_run["top_k"]
    input_schema = hazkey_run["schema"]
    boundary_aware = input_schema in (INPUT_SCHEMA_V4, INPUT_SCHEMA_V5)
    reviewed_target_mode = reviewed_first_segment_targets is not None
    if reviewed_target_mode and input_schema != INPUT_SCHEMA_V5:
        raise ValueError("reviewed first-segment targets require ABProbe v5 inputs")
    reviewed_targets = (
        _validate_reviewed_first_segment_targets(
            reviewed_first_segment_targets,
            corpus,
            mozc_run,
        )
        if reviewed_first_segment_targets is not None
        else None
    )
    if formal_quality_categories is None:
        selected_formal_quality_categories = FORMAL_V2_QUALITY_CATEGORIES
    else:
        selected_formal_quality_categories = tuple(formal_quality_categories)
        if (
            not selected_formal_quality_categories
            or any(
                not isinstance(category, str) or not category
                for category in selected_formal_quality_categories
            )
            or len(selected_formal_quality_categories)
            != len(set(selected_formal_quality_categories))
        ):
            raise ValueError(
                "formal_quality_categories must contain unique non-empty strings"
            )
    selected_formal_quality_category_policy_id = (
        FORMAL_V2_QUALITY_CATEGORY_POLICY_ID
        if formal_quality_category_policy_id is None
        else formal_quality_category_policy_id
    )
    if (
        not isinstance(selected_formal_quality_category_policy_id, str)
        or not selected_formal_quality_category_policy_id
    ):
        raise ValueError("formal_quality_category_policy_id must not be empty")
    if additional_input_metadata is not None:
        if not isinstance(additional_input_metadata, dict):
            raise ValueError("additional_input_metadata must be an object")
        reserved_input_fields = {"corpus", "hazkey", "mozc"}
        overlap = reserved_input_fields & set(additional_input_metadata)
        if overlap:
            raise ValueError(
                "additional_input_metadata uses reserved fields: "
                f"{sorted(overlap)!r}"
            )
    if reviewed_target_metadata is not None and not isinstance(
        reviewed_target_metadata, dict
    ):
        raise ValueError("reviewed_target_metadata must be an object")
    cases: list[dict[str, Any]] = []
    miss_counts: Counter[str] = Counter({name: 0 for name in MISS_CLASSES})
    hazkey_hits = 0
    mozc_hits = 0
    hybrid_hits = 0
    runtime_h0_hits = 0
    guarded_hybrid_hits = 0
    backend_top1_oracle_hits = 0
    candidate_union_oracle_hits = 0
    rescued = 0
    regressed = 0
    runtime_h0_rescued = 0
    runtime_h0_regressed = 0
    guarded_rescued = 0
    guarded_regressed = 0
    policy_decision_counts: Counter[str] = Counter()
    promotion_outcome_counts: Counter[str] = Counter(
        {name: 0 for name in PROMOTION_OUTCOMES}
    )
    guarded_policy_decision_counts: Counter[str] = Counter()
    guarded_promotion_outcome_counts: Counter[str] = Counter(
        {name: 0 for name in PROMOTION_OUTCOMES}
    )
    guarded_suppressed_h1_outcome_counts: Counter[str] = Counter(
        {name: 0 for name in PROMOTION_OUTCOMES}
    )
    guarded_suppressed_h2_outcome_counts: Counter[str] = Counter(
        {name: 0 for name in PROMOTION_OUTCOMES}
    )
    surface_policy_decision_counts: Counter[str] = Counter()
    surface_promotion_outcome_counts: Counter[str] = Counter(
        {name: 0 for name in PROMOTION_OUTCOMES}
    )
    surface_promotion_opportunities = 0
    surface_opportunities_boundary_rejected = 0
    boundary_only_opportunities = 0
    actual_hazkey_top1_boundary_compared = 0
    actual_hazkey_top1_boundary_mismatch = 0
    quality_comparable_cases = 0
    promotion_outcome_incomparable = 0
    guarded_promotion_outcome_incomparable = 0
    guarded_suppressed_h1_outcome_incomparable = 0
    surface_promotion_outcome_incomparable = 0

    for row in corpus:
        case_id = row["id"]
        reviewed_target = (
            reviewed_targets[case_id] if reviewed_targets is not None else None
        )
        expected = (
            reviewed_target["surfaces"]
            if reviewed_target is not None
            else row["expected"].split("|")
        )
        hazkey_records = hazkey_run["cases"][case_id]["candidates"][:top_k]
        mozc_records = mozc_run["cases"][case_id]["candidates"][:top_k]
        hazkey = _candidate_texts(hazkey_records, input_schema)
        mozc = _candidate_texts(mozc_records, input_schema)
        surface_hybrid, surface_decision = merge_candidates(
            hazkey, mozc, top_k
        )
        if boundary_aware:
            hybrid_records, decision = _merge_boundary_aware_candidate_records(
                hazkey_records,
                mozc_records,
                top_k,
                allow_promotion=True,
            )
            runtime_h0_records, runtime_h0_decision = (
                _merge_boundary_aware_candidate_records(
                    hazkey_records, mozc_records, top_k, allow_promotion=False
                )
            )
            guarded_records, guarded_decision = (
                _merge_boundary_aware_candidate_records(
                    hazkey_records,
                    mozc_records,
                    top_k,
                    allow_promotion=True,
                    width_guard=True,
                )
            )
            hybrid = _candidate_texts(hybrid_records, input_schema)
            runtime_h0 = _candidate_texts(runtime_h0_records, input_schema)
            guarded_hybrid = _candidate_texts(guarded_records, input_schema)
            eligible_hazkey_records = _boundary_eligible_hazkey_candidates(
                hazkey_records, mozc_records
            )
            eligible_hazkey = _candidate_texts(
                eligible_hazkey_records, input_schema
            )
            composition_span = (
                mozc_run["cases"][case_id]["composition_span"]
                if input_schema == INPUT_SCHEMA_V5
                else None
            )
            expected_consuming_count = _whole_span_target_count(
                input_schema, mozc_records, composition_span
            )
            if reviewed_target is not None:
                expected_consuming_count = reviewed_target["span"]["count"]
            if hazkey_records and mozc_records:
                actual_hazkey_top1_boundary_compared += 1
                boundary_mismatch = (
                    hazkey_records[0]["consuming_count"]
                    != mozc_records[0]["consuming_count"]
                )
                actual_hazkey_top1_boundary_mismatch += int(
                    boundary_mismatch
                )
            else:
                boundary_mismatch = None
        else:
            hybrid = surface_hybrid
            decision = surface_decision
            runtime_h0, runtime_h0_decision = (
                merge_candidates_preserve_mozc_top1(hazkey, mozc, top_k)
            )
            eligible_hazkey = hazkey
            boundary_mismatch = None
            guarded_hybrid = None
            guarded_decision = None
            guarded_records = []
            composition_span = None
            expected_consuming_count = None
        target_comparable = not boundary_aware or expected_consuming_count is not None
        if target_comparable:
            quality_comparable_cases += 1
            if boundary_aware:
                if expected_consuming_count is None:
                    raise AssertionError("comparable v5 case is missing its span")
                hazkey_rank = _structured_expected_rank(
                    expected, expected_consuming_count, hazkey_records
                )
                mozc_rank = _structured_expected_rank(
                    expected, expected_consuming_count, mozc_records
                )
                hybrid_rank = _structured_expected_rank(
                    expected, expected_consuming_count, hybrid_records
                )
                runtime_h0_rank = _structured_expected_rank(
                    expected, expected_consuming_count, runtime_h0_records
                )
                guarded_hybrid_rank = _structured_expected_rank(
                    expected, expected_consuming_count, guarded_records
                )
                surface_hybrid_rank = None
            else:
                hazkey_rank = expected_rank(expected, hazkey)
                mozc_rank = expected_rank(expected, mozc)
                hybrid_rank = expected_rank(expected, hybrid)
                runtime_h0_rank = expected_rank(expected, runtime_h0)
                guarded_hybrid_rank = None
                surface_hybrid_rank = expected_rank(expected, surface_hybrid)
            hazkey_top1 = hazkey_rank == 1
            mozc_top1 = mozc_rank == 1
            hybrid_top1 = hybrid_rank == 1
            runtime_h0_top1 = runtime_h0_rank == 1
            guarded_hybrid_top1 = guarded_hybrid_rank == 1
            classification = None
            if not mozc_top1:
                classification = classify_mozc_top1_miss(
                    hazkey_rank, mozc_rank
                )
                miss_counts[classification] += 1

            if not mozc_top1 and hybrid_top1:
                outcome = "rescued"
                rescued += 1
            elif mozc_top1 and not hybrid_top1:
                outcome = "regressed"
                regressed += 1
            elif mozc_top1:
                outcome = "unchanged_correct"
            else:
                outcome = "unchanged_incorrect"

            if boundary_aware:
                surface_outcome = None
            else:
                surface_hybrid_top1 = surface_hybrid_rank == 1
                if not mozc_top1 and surface_hybrid_top1:
                    surface_outcome = "rescued"
                elif mozc_top1 and not surface_hybrid_top1:
                    surface_outcome = "regressed"
                elif mozc_top1:
                    surface_outcome = "unchanged_correct"
                else:
                    surface_outcome = "unchanged_incorrect"

            if not mozc_top1 and guarded_hybrid_top1:
                guarded_outcome = "rescued"
                guarded_rescued += 1
            elif mozc_top1 and not guarded_hybrid_top1:
                guarded_outcome = "regressed"
                guarded_regressed += 1
            elif mozc_top1:
                guarded_outcome = "unchanged_correct"
            else:
                guarded_outcome = "unchanged_incorrect"

            hazkey_hits += int(hazkey_top1)
            mozc_hits += int(mozc_top1)
            hybrid_hits += int(hybrid_top1)
            runtime_h0_hits += int(runtime_h0_top1)
            guarded_hybrid_hits += int(guarded_hybrid_top1)
            runtime_h0_rescued += int(not mozc_top1 and runtime_h0_top1)
            runtime_h0_regressed += int(mozc_top1 and not runtime_h0_top1)
            backend_top1_oracle_hits += int(hazkey_top1 or mozc_top1)
            candidate_union_oracle_hits += int(
                hazkey_rank is not None or mozc_rank is not None
            )
        else:
            hazkey_rank = None
            mozc_rank = None
            hybrid_rank = None
            runtime_h0_rank = None
            guarded_hybrid_rank = None
            classification = None
            outcome = None
            surface_outcome = None
            guarded_outcome = None

        policy_decision_counts[decision] += 1
        if decision == PROMOTION_DECISION:
            if outcome is None:
                promotion_outcome_incomparable += 1
            else:
                promotion_outcome_counts[outcome] += 1
        if boundary_aware:
            if guarded_decision is None:
                raise AssertionError("boundary-aware case is missing guarded decision")
            guarded_policy_decision_counts[guarded_decision] += 1
            if guarded_decision == PROMOTION_DECISION:
                if guarded_outcome is None:
                    guarded_promotion_outcome_incomparable += 1
                else:
                    guarded_promotion_outcome_counts[guarded_outcome] += 1
            elif guarded_decision == WIDTH_EQUIVALENT_DECISION:
                if outcome is None:
                    guarded_suppressed_h1_outcome_incomparable += 1
                else:
                    if guarded_outcome is None:
                        raise AssertionError(
                            "comparable width suppression is missing its H2 outcome"
                        )
                    guarded_suppressed_h1_outcome_counts[outcome] += 1
                    guarded_suppressed_h2_outcome_counts[guarded_outcome] += 1
        surface_policy_decision_counts[surface_decision] += 1
        if surface_decision == PROMOTION_DECISION:
            surface_promotion_opportunities += 1
            if surface_outcome is None:
                surface_promotion_outcome_incomparable += 1
            else:
                surface_promotion_outcome_counts[surface_outcome] += 1
            if boundary_aware and decision != PROMOTION_DECISION:
                surface_opportunities_boundary_rejected += 1
        if (
            boundary_aware
            and decision == PROMOTION_DECISION
            and surface_decision != PROMOTION_DECISION
        ):
            boundary_only_opportunities += 1

        top1_quality = None
        if reviewed_target is not None:
            if expected_consuming_count is None:
                raise AssertionError(
                    "reviewed target is missing its consuming-count contract"
                )
            system_records = {
                "hazkey": hazkey_records,
                "mozc": mozc_records,
                "runtime_h0": runtime_h0_records,
                "h1_hybrid": hybrid_records,
                "h2_width_guarded": guarded_records,
            }
            top1_quality = {
                "reviewed": {
                    "surfaces": list(expected),
                    "consuming_count": expected_consuming_count,
                    "boundary_unit": "composition_element",
                    "surface_match": "raw_exact",
                },
                "systems": {
                    system: _structured_candidate_quality(
                        records, expected, expected_consuming_count
                    )
                    for system, records in system_records.items()
                },
            }

        cases.append(
            {
                "id": case_id,
                "reading": row["reading"],
                "category": row["category"],
                "expected": expected,
                **(
                    {"reviewed_first_segment_target": reviewed_target}
                    if reviewed_target is not None
                    else {}
                ),
                **(
                    {"top1_quality": top1_quality}
                    if top1_quality is not None
                    else {}
                ),
                "expected_rank": {
                    "hazkey": hazkey_rank,
                    "mozc": mozc_rank,
                    "hybrid": hybrid_rank,
                    "runtime_h0": runtime_h0_rank,
                    **(
                        {"width_guarded_hybrid": guarded_hybrid_rank}
                        if boundary_aware
                        else {}
                    ),
                },
                "mozc_top1_miss_classification": classification,
                "target_comparable": target_comparable,
                "policy_decision": decision,
                **(
                    {"surface_policy_decision": surface_decision}
                    if boundary_aware
                    else {}
                ),
                "runtime_h0_policy_decision": runtime_h0_decision,
                **(
                    {"width_guarded_policy_decision": guarded_decision}
                    if boundary_aware
                    else {}
                ),
                "top1_outcome": outcome,
                **(
                    {"width_guarded_top1_outcome": guarded_outcome}
                    if boundary_aware
                    else {}
                ),
                **(
                    {
                        "quality_limitation": (
                            "the observed first clause does not explicitly span "
                            "the whole-composition target"
                            if input_schema == INPUT_SCHEMA_V5
                            else (
                                "segment_candidates observes first-clause surfaces, "
                                "while corpus expected values are whole-composition "
                                "targets"
                            )
                        )
                    }
                    if not target_comparable
                    else {}
                ),
                "candidates": {
                    "hazkey": hazkey,
                    "mozc": mozc,
                    "hybrid": hybrid,
                    "runtime_h0": runtime_h0,
                    **(
                        {"width_guarded_hybrid": guarded_hybrid}
                        if boundary_aware
                        else {}
                    ),
                },
                **(
                    {
                        "boundary_evidence": {
                            "mozc_top1_consuming_count": (
                                mozc_records[0]["consuming_count"]
                                if mozc_records
                                else None
                            ),
                            "actual_hazkey_top1_consuming_count": (
                                hazkey_records[0]["consuming_count"]
                                if hazkey_records
                                else None
                            ),
                            "actual_hazkey_top1_mismatch": boundary_mismatch,
                            "eligible_hazkey_candidates": eligible_hazkey,
                            **(
                                {
                                    "composition_span": composition_span,
                                    **(
                                        {
                                            "reviewed_first_segment_span": (
                                                reviewed_target["span"]
                                            ),
                                            "reviewed_target_boundary_matches_mozc": (
                                                bool(mozc_records)
                                                and mozc_records[0]["consuming_count"]
                                                == reviewed_target["span"]["count"]
                                            ),
                                        }
                                        if reviewed_target is not None
                                        else {
                                            "whole_span_target_parity": (
                                                target_comparable
                                            )
                                        }
                                    ),
                                }
                                if input_schema == INPUT_SCHEMA_V5
                                else {}
                            ),
                        }
                    }
                    if boundary_aware
                    else {}
                ),
            }
        )

    total = len(cases)
    target_incomparable_cases = total - quality_comparable_cases
    mozc_misses = quality_comparable_cases - mozc_hits
    classified = sum(miss_counts.values())
    if classified != mozc_misses:
        raise AssertionError(
            "Mozc Top-1 miss classification is not exhaustive and disjoint"
        )
    if hybrid_hits - mozc_hits != rescued - regressed:
        raise AssertionError("Top-1 rescue/regression accounting is inconsistent")
    if (
        boundary_aware
        and guarded_hybrid_hits - mozc_hits
        != guarded_rescued - guarded_regressed
    ):
        raise AssertionError(
            "width-guarded Top-1 rescue/regression accounting is inconsistent"
        )
    if (
        boundary_aware
        and policy_decision_counts[PROMOTION_DECISION]
        != guarded_policy_decision_counts[PROMOTION_DECISION]
        + guarded_policy_decision_counts[WIDTH_EQUIVALENT_DECISION]
    ):
        raise AssertionError("width-guard promotion decomposition is inconsistent")
    if boundary_aware and guarded_policy_decision_counts[
        WIDTH_EQUIVALENT_DECISION
    ] != (
        sum(guarded_suppressed_h1_outcome_counts.values())
        + guarded_suppressed_h1_outcome_incomparable
    ):
        raise AssertionError("width-guard suppression accounting is inconsistent")
    if sum(guarded_suppressed_h1_outcome_counts.values()) != sum(
        guarded_suppressed_h2_outcome_counts.values()
    ):
        raise AssertionError("width-guard suppression outcomes are inconsistent")
    if boundary_aware and any(
        promotion_outcome_counts[name]
        != guarded_promotion_outcome_counts[name]
        + guarded_suppressed_h1_outcome_counts[name]
        for name in PROMOTION_OUTCOMES
    ):
        raise AssertionError("width-guard comparable outcome decomposition is inconsistent")
    if (
        boundary_aware
        and promotion_outcome_incomparable
        != guarded_promotion_outcome_incomparable
        + guarded_suppressed_h1_outcome_incomparable
    ):
        raise AssertionError("width-guard incomparable outcome decomposition is inconsistent")

    below_both = miss_counts[BELOW_TOP1_BOTH]
    below_hazkey_only = miss_counts[BELOW_TOP1_HAZKEY_ONLY]
    below_mozc_only = miss_counts[BELOW_TOP1_MOZC_ONLY]
    below_total = below_both + below_hazkey_only + below_mozc_only

    if boundary_aware:
        promotion_outcome_comparable = sum(promotion_outcome_counts.values())
        guarded_promotion_outcome_comparable = sum(
            guarded_promotion_outcome_counts.values()
        )
        candidate_evidence = {
            "input_schema": input_schema,
            "observed_fields": [
                "text",
                "rank",
                "consuming_count",
            ],
            **(
                {"case_observed_fields": ["composition_span"]}
                if input_schema == INPUT_SCHEMA_V5
                else {}
            ),
            "conversion_path": SEGMENT_CANDIDATES_PATH,
            "consuming_count_available": True,
            "boundary_evidence_available": True,
            "runtime_boundary_parity_established": True,
            "whole_target_quality_comparable": (
                False
                if reviewed_target_mode
                else quality_comparable_cases == total
            ),
            **(
                {
                    **(
                        {
                            "reviewed_first_segment_target_count": (
                                quality_comparable_cases
                            )
                        }
                        if reviewed_target_mode
                        else {
                            "whole_target_quality_comparable_count": (
                                quality_comparable_cases
                            )
                        }
                    )
                }
                if input_schema == INPUT_SCHEMA_V5
                else {}
            ),
            "limitation": (
                "Reviewed labels establish exact first-segment targets, but ABProbe "
                "v5 does not observe the labeled tail segments."
                if reviewed_target_mode
                else "Only cases whose explicit composition span is fully consumed by "
                "Mozc Top-1 are comparable to whole-composition targets; all other "
                "cases remain excluded."
                if input_schema == INPUT_SCHEMA_V5
                else (
                    "segment_candidates observes first-clause surfaces, while the "
                    "corpus expected values are whole-composition targets. Boundary "
                    "evidence is valid, but whole-target quality is not comparable."
                )
            ),
        }
        promotion_opportunities = {
            "decision": PROMOTION_DECISION,
            "scope": "boundary_aware",
            "count": policy_decision_counts[PROMOTION_DECISION],
            "rate": _rate(policy_decision_counts[PROMOTION_DECISION], total),
            "surface_opportunity_count": surface_promotion_opportunities,
            "surface_opportunity_rate": _rate(
                surface_promotion_opportunities, total
            ),
            "boundary_eligible_count": (
                surface_promotion_opportunities
                - surface_opportunities_boundary_rejected
            ),
            "boundary_rejected_count": (
                surface_opportunities_boundary_rejected
            ),
            "boundary_only_opportunity_count": boundary_only_opportunities,
            "outcomes": (
                {
                    name: promotion_outcome_counts[name]
                    for name in PROMOTION_OUTCOMES
                }
                if promotion_outcome_comparable
                else None
            ),
            "outcome_comparable_count": promotion_outcome_comparable,
            "outcome_incomparable_count": promotion_outcome_incomparable,
            "surface_outcomes": None,
            "surface_outcome_comparable_count": 0,
            "surface_outcome_incomparable_count": (
                surface_promotion_outcome_incomparable
            ),
            "all_policy_decisions": dict(
                sorted(policy_decision_counts.items())
            ),
            "all_surface_policy_decisions": dict(
                sorted(surface_policy_decision_counts.items())
            ),
        }
        guarded_promotion_opportunities = {
            "policy_id": WIDTH_GUARDED_POLICY_ID,
            "decision": PROMOTION_DECISION,
            "scope": "boundary_aware",
            "count": guarded_policy_decision_counts[PROMOTION_DECISION],
            "rate": _rate(
                guarded_policy_decision_counts[PROMOTION_DECISION], total
            ),
            "suppressed_width_equivalent_count": guarded_policy_decision_counts[
                WIDTH_EQUIVALENT_DECISION
            ],
            "suppressed_width_equivalent": {
                "count": guarded_policy_decision_counts[
                    WIDTH_EQUIVALENT_DECISION
                ],
                "counterfactual_h1_outcomes": {
                    name: guarded_suppressed_h1_outcome_counts[name]
                    for name in PROMOTION_OUTCOMES
                },
                "h2_outcomes": {
                    name: guarded_suppressed_h2_outcome_counts[name]
                    for name in PROMOTION_OUTCOMES
                },
                "outcome_comparable_count": sum(
                    guarded_suppressed_h1_outcome_counts.values()
                ),
                "outcome_incomparable_count": (
                    guarded_suppressed_h1_outcome_incomparable
                ),
            },
            "outcomes": (
                {
                    name: guarded_promotion_outcome_counts[name]
                    for name in PROMOTION_OUTCOMES
                }
                if guarded_promotion_outcome_comparable
                else None
            ),
            "outcome_comparable_count": guarded_promotion_outcome_comparable,
            "outcome_incomparable_count": (
                guarded_promotion_outcome_incomparable
            ),
            "all_policy_decisions": dict(
                sorted(guarded_policy_decision_counts.items())
            ),
        }
        boundary_evidence = {
            "conversion_path": SEGMENT_CANDIDATES_PATH,
            "actual_hazkey_top1": {
                "compared_count": actual_hazkey_top1_boundary_compared,
                "matching_count": (
                    actual_hazkey_top1_boundary_compared
                    - actual_hazkey_top1_boundary_mismatch
                ),
                "mismatch_count": actual_hazkey_top1_boundary_mismatch,
                "mismatch_rate": _rate(
                    actual_hazkey_top1_boundary_mismatch,
                    actual_hazkey_top1_boundary_compared,
                ),
            },
            **(
                {
                    "explicit_composition_span": {
                        "unit": "composition_element",
                        **(
                            {
                                "reviewed_first_segment_comparable_count": (
                                    quality_comparable_cases
                                ),
                                "reviewed_first_segment_comparable_rate": _rate(
                                    quality_comparable_cases, total
                                ),
                            }
                            if reviewed_target_mode
                            else {
                                "whole_span_comparable_count": (
                                    quality_comparable_cases
                                ),
                                "whole_span_comparable_rate": _rate(
                                    quality_comparable_cases, total
                                ),
                            }
                        ),
                    }
                }
                if input_schema == INPUT_SCHEMA_V5
                else {}
            ),
        }
        target_comparability = {
            "quality_target": (
                "first_reviewed_segment"
                if reviewed_target_mode
                else "whole_composition"
            ),
            "observed_candidate_scope": "first_clause",
            "established": quality_comparable_cases == total,
            "comparable_count": quality_comparable_cases,
            "incomparable_count": target_incomparable_cases,
            **(
                {
                    **(
                        {
                            "comparison_basis": "reviewed_segment_label",
                            "partial_parity_established": (
                                quality_comparable_cases > 0
                            ),
                            "selection_basis": "corpus_label_not_backend_output",
                            "selection_biased": False,
                            **(
                                {"reviewed_target_metadata": reviewed_target_metadata}
                                if reviewed_target_metadata is not None
                                else {}
                            ),
                        }
                        if reviewed_target_mode
                        else {
                            "comparison_basis": "explicit_whole_composition_span",
                            "partial_parity_established": (
                                quality_comparable_cases > 0
                            ),
                            "selection_basis": (
                                "mozc_top1_consumes_explicit_whole_span"
                            ),
                            "selection_biased": True,
                            "absolute_backend_accuracy_generalizable": False,
                        }
                    ),
                }
                if input_schema == INPUT_SCHEMA_V5
                else {}
            ),
            "required_evidence": (
                None
                if reviewed_target_mode
                else (
                    None
                    if target_incomparable_cases == 0
                    else (
                        "For the remaining incomparable rows, acquire "
                        "segment-labeled targets or reviewed evidence that the "
                        "observed candidate span covers the whole target."
                    )
                )
                if input_schema == INPUT_SCHEMA_V5
                else (
                    "a segment-labeled holdout, or an explicit composition-span "
                    "field with a reviewed target-parity inference"
                )
            ),
        }
        evaluation_scope = (
            "boundary_aware_reviewed_first_segment"
            if reviewed_target_mode
            else "boundary_aware_explicit_span"
            if input_schema == INPUT_SCHEMA_V5
            else "boundary_aware"
        )
    else:
        candidate_evidence = {
            "input_schema": INPUT_SCHEMA_V3,
            "observed_fields": ["surface"],
            "conversion_path": None,
            "consuming_count_available": False,
            "boundary_evidence_available": False,
            "runtime_boundary_parity_established": False,
            "whole_target_quality_comparable": True,
            "limitation": (
                "ABProbe v3 records candidate surfaces but not consuming counts; "
                "this report cannot authorize a boundary-sensitive runtime reorder."
            ),
        }
        promotion_opportunities = {
            "decision": PROMOTION_DECISION,
            "scope": "surface_only",
            "count": policy_decision_counts[PROMOTION_DECISION],
            "rate": _rate(policy_decision_counts[PROMOTION_DECISION], total),
            "boundary_eligible_count": None,
            "outcomes": {
                name: promotion_outcome_counts[name]
                for name in PROMOTION_OUTCOMES
            },
            "all_policy_decisions": dict(
                sorted(policy_decision_counts.items())
            ),
        }
        boundary_evidence = None
        target_comparability = {
            "quality_target": "whole_composition",
            "observed_candidate_scope": "whole_composition",
            "established": True,
            "comparable_count": quality_comparable_cases,
            "incomparable_count": target_incomparable_cases,
            "required_evidence": None,
        }
        guarded_promotion_opportunities = None
        evaluation_scope = "surface_only"

    if boundary_aware and quality_comparable_cases == 0:
        top1_report = {
            "quality_comparable": False,
            "scope": "whole_target_comparable_only",
            "cases": 0,
            "excluded_incomparable_cases": target_incomparable_cases,
            "hazkey": {"hits": None, "rate": None},
            "mozc": {"hits": None, "rate": None},
            "hybrid": {"hits": None, "rate": None},
            "rescued": None,
            "regressed": None,
            "net_hits": None,
            "net_rate": None,
        }
        runtime_h0_top1_report = {
            "quality_comparable": False,
            "scope": "whole_target_comparable_only",
            "cases": 0,
            "excluded_incomparable_cases": target_incomparable_cases,
            "hits": None,
            "rate": None,
            "rescued": None,
            "regressed": None,
            "net_hits": None,
            "net_rate": None,
        }
        guarded_top1_report = {
            "quality_comparable": False,
            "scope": "whole_target_comparable_only",
            "cases": 0,
            "excluded_incomparable_cases": target_incomparable_cases,
            "hits": None,
            "rate": None,
            "rescued": None,
            "regressed": None,
            "net_hits": None,
            "net_rate": None,
        }
        oracle_ceiling_report = {
            "quality_comparable": False,
            "excluded_incomparable_cases": target_incomparable_cases,
            "backend_top1_union": {
                "definition": "not evaluated without segment target parity",
                "hits": None,
                "rate": None,
                "incremental_hits_over_mozc": None,
            },
            "candidate_union": {
                "definition": "not evaluated without segment target parity",
                "hits": None,
                "rate": None,
                "incremental_hits_over_mozc": None,
            },
        }
        miss_classification_report = {
            "scope": "not evaluated without segment target parity",
            "quality_comparable": False,
            "excluded_incomparable_cases": target_incomparable_cases,
            "exhaustive": None,
            "disjoint": None,
            "total": None,
            HAZKEY_TOP1_RESCUE: {
                "count": None,
                "rate_of_mozc_top1_misses": None,
            },
            "below_top1_presence": {
                "count": None,
                "rate_of_mozc_top1_misses": None,
                "both": None,
                "hazkey_only": None,
                "mozc_only": None,
            },
            BOTH_ABSENT: {
                "count": None,
                "rate_of_mozc_top1_misses": None,
            },
        }
    else:
        quality_metadata = (
            {
                "quality_comparable": True,
                "scope": "explicit_whole_composition_span",
                "excluded_incomparable_cases": target_incomparable_cases,
            }
            if boundary_aware
            else {}
        )
        top1_report = {
            **quality_metadata,
            "cases": quality_comparable_cases,
            "hazkey": {
                "hits": hazkey_hits,
                "rate": _rate(hazkey_hits, quality_comparable_cases),
            },
            "mozc": {
                "hits": mozc_hits,
                "rate": _rate(mozc_hits, quality_comparable_cases),
            },
            "hybrid": {
                "hits": hybrid_hits,
                "rate": _rate(hybrid_hits, quality_comparable_cases),
            },
            "rescued": rescued,
            "regressed": regressed,
            "net_hits": rescued - regressed,
            "net_rate": _rate(
                rescued - regressed, quality_comparable_cases
            ),
        }
        runtime_h0_top1_report = {
            **quality_metadata,
            "cases": quality_comparable_cases,
            "hits": runtime_h0_hits,
            "rate": _rate(runtime_h0_hits, quality_comparable_cases),
            "rescued": runtime_h0_rescued,
            "regressed": runtime_h0_regressed,
            "net_hits": runtime_h0_rescued - runtime_h0_regressed,
            "net_rate": _rate(
                runtime_h0_rescued - runtime_h0_regressed,
                quality_comparable_cases,
            ),
        }
        guarded_top1_report = (
            {
                **quality_metadata,
                "cases": quality_comparable_cases,
                "hits": guarded_hybrid_hits,
                "rate": _rate(
                    guarded_hybrid_hits, quality_comparable_cases
                ),
                "rescued": guarded_rescued,
                "regressed": guarded_regressed,
                "net_hits": guarded_rescued - guarded_regressed,
                "net_rate": _rate(
                    guarded_rescued - guarded_regressed,
                    quality_comparable_cases,
                ),
            }
            if boundary_aware
            else None
        )
        oracle_ceiling_report = {
            **quality_metadata,
            "backend_top1_union": {
                "definition": "expected is Top-1 in Hazkey or Mozc",
                "hits": backend_top1_oracle_hits,
                "rate": _rate(
                    backend_top1_oracle_hits, quality_comparable_cases
                ),
                "incremental_hits_over_mozc": backend_top1_oracle_hits - mozc_hits,
            },
            "candidate_union": {
                "definition": (
                    "expected occurs anywhere in either observed candidate window"
                ),
                "hits": candidate_union_oracle_hits,
                "rate": _rate(
                    candidate_union_oracle_hits, quality_comparable_cases
                ),
                "incremental_hits_over_mozc": candidate_union_oracle_hits - mozc_hits,
            },
        }
        miss_classification_report = {
            "scope": (
                "exact expected surface and composition span within observed top_k"
                if boundary_aware
                else "exact expected surface within observed top_k"
            ),
            **(
                {
                    "quality_comparable": True,
                    "excluded_incomparable_cases": target_incomparable_cases,
                }
                if boundary_aware
                else {}
            ),
            "exhaustive": True,
            "disjoint": True,
            "total": mozc_misses,
            HAZKEY_TOP1_RESCUE: {
                "count": miss_counts[HAZKEY_TOP1_RESCUE],
                "rate_of_mozc_top1_misses": _rate(
                    miss_counts[HAZKEY_TOP1_RESCUE], mozc_misses
                ),
            },
            "below_top1_presence": {
                "count": below_total,
                "rate_of_mozc_top1_misses": _rate(below_total, mozc_misses),
                "both": below_both,
                "hazkey_only": below_hazkey_only,
                "mozc_only": below_mozc_only,
            },
            BOTH_ABSENT: {
                "count": miss_counts[BOTH_ABSENT],
                "rate_of_mozc_top1_misses": _rate(
                    miss_counts[BOTH_ABSENT], mozc_misses
                ),
            },
        }

    if input_schema == INPUT_SCHEMA_V5:
        diagnostic_cases = [case for case in cases if case["target_comparable"]]
        formal_category_set = set(selected_formal_quality_categories)
        formal_corpus_cases = [
            case for case in cases if case["category"] in formal_category_set
        ]
        formal_comparable_cases = [
            case for case in formal_corpus_cases if case["target_comparable"]
        ]
        diagnostic_target_comparable = {
            "diagnostic_only": True,
            "formal_authorized": False,
            "category_scope": {
                "policy": "all_categories",
                "cases": len(diagnostic_cases),
                "by_category": dict(
                    sorted(
                        Counter(
                            case["category"] for case in diagnostic_cases
                        ).items()
                    )
                ),
                "includes_formal_non_quality_categories": any(
                    case["category"] not in formal_category_set
                    for case in diagnostic_cases
                ),
            },
            **_build_v5_quality_view(
                diagnostic_cases,
                aggregation_scope="all_target_comparable_categories",
                formal_quality_categories_only=False,
                excluded_target_incomparable_cases=target_incomparable_cases,
                target_scope=(
                    "reviewed_first_segment"
                    if reviewed_target_mode
                    else "explicit_whole_composition_span"
                ),
                include_quality_decomposition=reviewed_target_mode,
            ),
        }
        formal_quality = {
            "diagnostic_only": True,
            "formal_authorized": False,
            "category_policy": {
                "id": selected_formal_quality_category_policy_id,
                "included_categories": list(
                    selected_formal_quality_categories
                ),
                "excluded_categories_observed": sorted(
                    {
                        case["category"]
                        for case in cases
                        if case["category"] not in formal_category_set
                    }
                ),
            },
            "case_scope": {
                "corpus_cases": total,
                "eligible_category_cases": len(formal_corpus_cases),
                "comparable_cases": len(formal_comparable_cases),
                "incomparable_cases": (
                    len(formal_corpus_cases) - len(formal_comparable_cases)
                ),
                "excluded_non_quality_cases": total - len(formal_corpus_cases),
                "excluded_non_quality_comparable_cases": (
                    len(diagnostic_cases) - len(formal_comparable_cases)
                ),
            },
            **_build_v5_quality_view(
                formal_comparable_cases,
                aggregation_scope=(
                    "reviewed_holdout_quality_categories"
                    if reviewed_target_mode
                    else "formal_v2_quality_categories"
                ),
                formal_quality_categories_only=True,
                excluded_target_incomparable_cases=(
                    len(formal_corpus_cases) - len(formal_comparable_cases)
                ),
                target_scope=(
                    "reviewed_first_segment"
                    if reviewed_target_mode
                    else "explicit_whole_composition_span"
                ),
                include_quality_decomposition=reviewed_target_mode,
            ),
        }
    else:
        diagnostic_target_comparable = None
        formal_quality = None

    return {
        "schema": report_schema,
        "diagnostic_only": True,
        "formal_authorized": False,
        "new_holdout_required": new_holdout_required,
        "rank_scope": "observed_top_k",
        "top_k": top_k,
        "candidate_evidence": candidate_evidence,
        "target_comparability": target_comparability,
        **(
            {"boundary_evidence": boundary_evidence}
            if boundary_evidence is not None
            else {}
        ),
        "policy": {
            "id": POLICY_ID,
            "uses_expected_labels": False,
            "evaluation_scope": evaluation_scope,
            "quality_evaluation_scope": (
                "reviewed_first_segment"
                if reviewed_target_mode
                else "explicit_whole_composition_span_subset"
                if input_schema == INPUT_SCHEMA_V5
                else (
                    "not_comparable_without_segment_target_parity"
                    if boundary_aware
                    else "whole_composition"
                )
            ),
            "runtime_apply_eligible": False,
            "normalized_surface": "Unicode NFC",
            "mozc_stable_prefix": MOZC_STABLE_PREFIX,
            "required_before_runtime_apply": [
                "Freeze the policy and implementation identities before disclosure.",
                "Acquire paired candidates with consuming counts on a new reviewed holdout.",
                "Verify runtime boundary, provenance, learning, and candidate-order parity.",
                (
                    "Retain the reviewed first-segment labels and their sealed "
                    "identity."
                    if reviewed_target_mode
                    else "Acquire segment-labeled targets or an explicit "
                    "composition-span field with reviewed target-parity inference."
                    if boundary_aware
                    else "Retain whole-composition target parity."
                ),
            ],
            "rules": [
                "Use Hazkey when Mozc returns no candidates.",
                (
                    "Otherwise keep Mozc Top-1 unless Hazkey Top-1 occurs below "
                    "Mozc Top-1 and Mozc Top-1 is absent from Hazkey."
                ),
                (
                    "On one-sided consensus, promote Hazkey Top-1, then retain "
                    "Mozc order, then append remaining unique Hazkey candidates."
                ),
                (
                    "Without promotion, retain Mozc Top-3, append unique Hazkey "
                    "candidates, then append remaining Mozc candidates."
                ),
                (
                    "For boundary-aware ABProbe results, retain only Hazkey "
                    "candidates matching the "
                    "Mozc Top-1 consuming_count."
                    if boundary_aware
                    else "ABProbe v3 cannot evaluate segment-boundary eligibility."
                ),
                (
                    "Deduplicate boundary-aware candidates by consuming_count and "
                    "Unicode NFC surface; v3 candidates by Unicode NFC surface; "
                    "stop at top_k."
                ),
            ],
        },
        **(
            {
                "width_guarded_policy": {
                    "id": WIDTH_GUARDED_POLICY_ID,
                    "diagnostic_only": True,
                    "runtime_apply_eligible": False,
                    "base_policy_id": POLICY_ID,
                    "rules": [
                        "Apply the H1 boundary-aware one-sided-consensus rule.",
                        "Suppress promotion when Hazkey and Mozc Top-1 differ only "
                        "by full-width ASCII or ideographic-space forms.",
                        "Do not use general Unicode NFKC compatibility folding.",
                    ],
                }
            }
            if boundary_aware
            else {}
        ),
        "runtime_policy": {
            "id": "mozc-first-preserve-top1-h0",
            "deployed_by_default": True,
            "uses_expected_labels": False,
            "rules": [
                "Use Hazkey only when Mozc returns no candidates.",
                "Otherwise preserve Mozc Top-1 and stable Top-3 order.",
                "Append unique Hazkey candidates, then remaining Mozc candidates.",
                *(
                    [
                        "For boundary-aware results, admit only Hazkey candidates "
                        "matching the Mozc "
                        "Top-1 consuming_count."
                    ]
                    if boundary_aware
                    else []
                ),
            ],
        },
        "promotion_opportunities": promotion_opportunities,
        **(
            {"width_guarded_promotion_opportunities": guarded_promotion_opportunities}
            if guarded_promotion_opportunities is not None
            else {}
        ),
        "inputs": {
            "corpus": {
                "sha256": actual_corpus_sha256,
                "cases": len(corpus),
            },
            "hazkey": _input_metadata(hazkey_bytes, hazkey_run),
            "mozc": _input_metadata(mozc_bytes, mozc_run),
            **(additional_input_metadata or {}),
        },
        **(
            {
                "diagnostic_target_comparable": diagnostic_target_comparable,
                "formal_quality": formal_quality,
            }
            if input_schema == INPUT_SCHEMA_V5
            else {
                "top1": top1_report,
                "runtime_h0_top1": runtime_h0_top1_report,
                **(
                    {"width_guarded_top1": guarded_top1_report}
                    if guarded_top1_report is not None
                    else {}
                ),
                "oracle_ceiling": oracle_ceiling_report,
                "mozc_top1_miss_classification": miss_classification_report,
            }
        ),
        "cases": cases,
    }


def evaluate_paths(
    corpus_path: Path, hazkey_path: Path, mozc_path: Path
) -> dict[str, Any]:
    """Read each input once, validate its identity, and return the report."""

    corpus_bytes = corpus_path.read_bytes()
    hazkey_bytes = hazkey_path.read_bytes()
    mozc_bytes = mozc_path.read_bytes()
    corpus = load_corpus_bytes(corpus_bytes, str(corpus_path))
    hazkey_run = load_run_bytes(hazkey_bytes, hazkey_path)
    mozc_run = load_run_bytes(mozc_bytes, mozc_path)
    return evaluate_runs(
        corpus,
        hazkey_run,
        mozc_run,
        corpus_sha256=_sha256_bytes(corpus_bytes),
        corpus_bytes=corpus_bytes,
        hazkey_bytes=hazkey_bytes,
        mozc_bytes=mozc_bytes,
        hazkey_context=str(hazkey_path),
        mozc_context=str(mozc_path),
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate the diagnostic Mozc-first hybrid spike policy."
    )
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--hazkey-results", type=Path, required=True)
    parser.add_argument("--mozc-results", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        report = evaluate_paths(
            args.corpus, args.hazkey_results, args.mozc_results
        )
        encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.output is None:
            sys.stdout.write(encoded)
        else:
            args.output.write_text(encoded, encoding="utf-8")
        return 0
    except (OSError, ValueError, AssertionError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
