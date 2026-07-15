#!/usr/bin/env python3
"""Evaluate a diagnostic Mozc-first hybrid policy against paired ABProbe runs.

This evaluator is deliberately offline and diagnostic-only.  It reports both
the runtime H0 policy (Mozc Top-1 is never promoted away) and the diagnostic H1
one-sided-consensus policy. It consumes one
corpus snapshot and complete Hazkey/Mozc ``hazkey.ab-probe-result.v3`` runs,
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
uses the exact-surface semantics of ``evaluate_conversion_quality.py``.
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
    from .summarize_ab_probe import INPUT_SCHEMA_V3, load_run_bytes
except ImportError:  # Direct execution from tools/dictionary.
    from compare_conversion_quality import expected_rank
    from evaluate_conversion_quality import load_corpus_bytes
    from summarize_ab_probe import INPUT_SCHEMA_V3, load_run_bytes


OUTPUT_SCHEMA = "hazkey.mozc-hybrid-spike-evaluation.v1"
POLICY_ID = "mozc-first-one-sided-consensus-v1"
MOZC_STABLE_PREFIX = 3

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
        if run["schema"] != INPUT_SCHEMA_V3:
            raise ValueError(f"{context}: hybrid spike requires {INPUT_SCHEMA_V3}")

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
    for field in ("backend_version", "warmups", "iterations", "top_k", "corpus"):
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


def _input_metadata(data: bytes, run: dict[str, Any]) -> dict[str, Any]:
    return {
        "sha256": _sha256_bytes(data),
        "schema": run["schema"],
        "backend": run["backend"],
        "backend_version": run["backend_version"],
        "converter_backend": run["converter_backend"],
        "source_ref": run["source_ref"],
        "resource": run["resource"],
    }


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
    cases: list[dict[str, Any]] = []
    miss_counts: Counter[str] = Counter({name: 0 for name in MISS_CLASSES})
    hazkey_hits = 0
    mozc_hits = 0
    hybrid_hits = 0
    runtime_h0_hits = 0
    backend_top1_oracle_hits = 0
    candidate_union_oracle_hits = 0
    rescued = 0
    regressed = 0
    runtime_h0_rescued = 0
    runtime_h0_regressed = 0

    for row in corpus:
        case_id = row["id"]
        expected = row["expected"].split("|")
        hazkey = hazkey_run["cases"][case_id]["candidates"][:top_k]
        mozc = mozc_run["cases"][case_id]["candidates"][:top_k]
        hybrid, decision = merge_candidates(hazkey, mozc, top_k)
        runtime_h0, runtime_h0_decision = merge_candidates_preserve_mozc_top1(
            hazkey, mozc, top_k
        )
        hazkey_rank = expected_rank(expected, hazkey)
        mozc_rank = expected_rank(expected, mozc)
        hybrid_rank = expected_rank(expected, hybrid)
        runtime_h0_rank = expected_rank(expected, runtime_h0)
        hazkey_top1 = hazkey_rank == 1
        mozc_top1 = mozc_rank == 1
        hybrid_top1 = hybrid_rank == 1
        runtime_h0_top1 = runtime_h0_rank == 1
        classification = None
        if not mozc_top1:
            classification = classify_mozc_top1_miss(hazkey_rank, mozc_rank)
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

        hazkey_hits += int(hazkey_top1)
        mozc_hits += int(mozc_top1)
        hybrid_hits += int(hybrid_top1)
        runtime_h0_hits += int(runtime_h0_top1)
        runtime_h0_rescued += int(not mozc_top1 and runtime_h0_top1)
        runtime_h0_regressed += int(mozc_top1 and not runtime_h0_top1)
        backend_top1_oracle_hits += int(hazkey_top1 or mozc_top1)
        candidate_union_oracle_hits += int(
            hazkey_rank is not None or mozc_rank is not None
        )
        cases.append(
            {
                "id": case_id,
                "reading": row["reading"],
                "category": row["category"],
                "expected": expected,
                "expected_rank": {
                    "hazkey": hazkey_rank,
                    "mozc": mozc_rank,
                    "hybrid": hybrid_rank,
                    "runtime_h0": runtime_h0_rank,
                },
                "mozc_top1_miss_classification": classification,
                "policy_decision": decision,
                "runtime_h0_policy_decision": runtime_h0_decision,
                "top1_outcome": outcome,
                "candidates": {
                    "hazkey": hazkey,
                    "mozc": mozc,
                    "hybrid": hybrid,
                    "runtime_h0": runtime_h0,
                },
            }
        )

    total = len(cases)
    mozc_misses = total - mozc_hits
    classified = sum(miss_counts.values())
    if classified != mozc_misses:
        raise AssertionError(
            "Mozc Top-1 miss classification is not exhaustive and disjoint"
        )
    if hybrid_hits - mozc_hits != rescued - regressed:
        raise AssertionError("Top-1 rescue/regression accounting is inconsistent")

    below_both = miss_counts[BELOW_TOP1_BOTH]
    below_hazkey_only = miss_counts[BELOW_TOP1_HAZKEY_ONLY]
    below_mozc_only = miss_counts[BELOW_TOP1_MOZC_ONLY]
    below_total = below_both + below_hazkey_only + below_mozc_only

    return {
        "schema": OUTPUT_SCHEMA,
        "diagnostic_only": True,
        "formal_authorized": False,
        "new_holdout_required": True,
        "rank_scope": "observed_top_k",
        "top_k": top_k,
        "policy": {
            "id": POLICY_ID,
            "uses_expected_labels": False,
            "normalized_surface": "Unicode NFC",
            "mozc_stable_prefix": MOZC_STABLE_PREFIX,
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
                "Deduplicate by Unicode NFC surface and stop at top_k.",
            ],
        },
        "runtime_policy": {
            "id": "mozc-first-preserve-top1-h0",
            "deployed_by_default": True,
            "uses_expected_labels": False,
            "rules": [
                "Use Hazkey only when Mozc returns no candidates.",
                "Otherwise preserve Mozc Top-1 and stable Top-3 order.",
                "Append unique Hazkey candidates, then remaining Mozc candidates.",
            ],
        },
        "inputs": {
            "corpus": {
                "sha256": actual_corpus_sha256,
                "cases": len(corpus),
            },
            "hazkey": _input_metadata(hazkey_bytes, hazkey_run),
            "mozc": _input_metadata(mozc_bytes, mozc_run),
        },
        "top1": {
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
            "cases": total,
            "hits": runtime_h0_hits,
            "rate": _rate(runtime_h0_hits, total),
            "rescued": runtime_h0_rescued,
            "regressed": runtime_h0_regressed,
            "net_hits": runtime_h0_rescued - runtime_h0_regressed,
            "net_rate": _rate(
                runtime_h0_rescued - runtime_h0_regressed, total
            ),
        },
        "oracle_ceiling": {
            "backend_top1_union": {
                "definition": "expected is Top-1 in Hazkey or Mozc",
                "hits": backend_top1_oracle_hits,
                "rate": _rate(backend_top1_oracle_hits, total),
                "incremental_hits_over_mozc": backend_top1_oracle_hits - mozc_hits,
            },
            "candidate_union": {
                "definition": "expected occurs anywhere in either observed candidate window",
                "hits": candidate_union_oracle_hits,
                "rate": _rate(candidate_union_oracle_hits, total),
                "incremental_hits_over_mozc": candidate_union_oracle_hits - mozc_hits,
            },
        },
        "mozc_top1_miss_classification": {
            "scope": "exact expected surface within observed top_k",
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
