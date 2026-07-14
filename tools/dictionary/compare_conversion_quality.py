#!/usr/bin/env python3
"""Compare two validated conversion-quality reports.

Ranks are one-based positions inside each report's observed top-k candidates.
All deltas use ``B - A``. A rank delta is null when either backend did not
produce an expected candidate inside the observed top-k window.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from pathlib import Path
from typing import Any


INPUT_SCHEMA = "hazkey.conversion-quality-report.v1"
OUTPUT_SCHEMA = "hazkey.conversion-quality-ab-report.v1"


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def load_json(path: Path) -> Any:
    return json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_object_without_duplicate_keys,
    )


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
    if result < 1:
        raise ValueError(f"{context} must be a positive integer")
    return result


def _boolean(value: Any, context: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{context} must be a boolean")
    return value


def _rate(value: Any, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{context} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or not 0.0 <= result <= 1.0:
        raise ValueError(f"{context} must be between zero and one")
    return result


def _require_equal(actual: Any, expected: Any, context: str) -> None:
    if actual != expected:
        raise ValueError(f"{context} is inconsistent: expected {expected!r}, got {actual!r}")


def _require_rate(actual: Any, expected: float, context: str) -> None:
    value = _rate(actual, context)
    if not math.isclose(value, expected, rel_tol=0.0, abs_tol=1e-12):
        raise ValueError(f"{context} is inconsistent: expected {expected!r}, got {value!r}")


def expected_rank(expected: list[str], observed: list[str]) -> int | None:
    expected_values = set(expected)
    return next(
        (index for index, candidate in enumerate(observed, 1) if candidate in expected_values),
        None,
    )


def validate_report(report: Any, label: str) -> dict[str, Any]:
    payload = _object(report, label)
    if payload.get("schema") != INPUT_SCHEMA:
        raise ValueError(f"{label}.schema must be {INPUT_SCHEMA}")

    top_k = _positive_int(payload.get("top_k"), f"{label}.top_k")
    corpus_cases = _nonnegative_int(
        payload.get("corpus_cases"), f"{label}.corpus_cases"
    )
    evaluated_cases = _nonnegative_int(
        payload.get("evaluated_cases"), f"{label}.evaluated_cases"
    )
    missing_results = _array(
        payload.get("missing_results"), f"{label}.missing_results"
    )
    if missing_results:
        raise ValueError(f"{label} is incomplete: missing_results must be empty")

    raw_cases = _array(payload.get("cases"), f"{label}.cases")
    if not raw_cases:
        raise ValueError(f"{label}.cases must not be empty")
    _require_equal(corpus_cases, len(raw_cases), f"{label}.corpus_cases")
    _require_equal(evaluated_cases, len(raw_cases), f"{label}.evaluated_cases")

    cases: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    category_counts: dict[str, Counter[str]] = {}
    for index, raw_case in enumerate(raw_cases):
        context = f"{label}.cases[{index}]"
        case = _object(raw_case, context)
        case_id = _string(case.get("id"), f"{context}.id")
        if case_id in seen_ids:
            raise ValueError(f"{label}.cases contains duplicate id {case_id!r}")
        seen_ids.add(case_id)

        category = _string(case.get("category"), f"{context}.category")
        reading = _string(case.get("reading"), f"{context}.reading")
        expected = [
            _string(value, f"{context}.expected[{item_index}]")
            for item_index, value in enumerate(
                _array(case.get("expected"), f"{context}.expected")
            )
        ]
        if not expected:
            raise ValueError(f"{context}.expected must not be empty")
        if len(expected) != len(set(expected)):
            raise ValueError(f"{context}.expected contains duplicate alternatives")

        observed = [
            _string(value, f"{context}.observed[{item_index}]")
            for item_index, value in enumerate(
                _array(case.get("observed"), f"{context}.observed")
            )
        ]
        if len(observed) > top_k:
            raise ValueError(f"{context}.observed exceeds top_k {top_k}")

        rank = expected_rank(expected, observed)
        top1_hit = rank == 1
        top_k_hit = rank is not None
        _require_equal(
            _boolean(case.get("top1"), f"{context}.top1"),
            top1_hit,
            f"{context}.top1",
        )
        if top_k != 1:
            metric = f"top{top_k}"
            _require_equal(
                _boolean(case.get(metric), f"{context}.{metric}"),
                top_k_hit,
                f"{context}.{metric}",
            )

        counters = category_counts.setdefault(category, Counter())
        counters["total"] += 1
        counters["top1"] += int(top1_hit)
        counters["top_k"] += int(top_k_hit)
        cases.append(
            {
                "id": case_id,
                "category": category,
                "reading": reading,
                "expected": expected,
                "observed": observed,
                "expected_rank": rank,
                "top1_hit": top1_hit,
                "top_k_hit": top_k_hit,
            }
        )

    total = len(cases)
    top1_hits = sum(int(case["top1_hit"]) for case in cases)
    top_k_hits = sum(int(case["top_k_hit"]) for case in cases)
    _require_equal(
        _nonnegative_int(payload.get("top1_hits"), f"{label}.top1_hits"),
        top1_hits,
        f"{label}.top1_hits",
    )
    _require_rate(payload.get("top1_rate"), top1_hits / total, f"{label}.top1_rate")
    if top_k != 1:
        metric = f"top{top_k}"
        _require_equal(
            _nonnegative_int(payload.get(f"{metric}_hits"), f"{label}.{metric}_hits"),
            top_k_hits,
            f"{label}.{metric}_hits",
        )
        _require_rate(
            payload.get(f"{metric}_rate"),
            top_k_hits / total,
            f"{label}.{metric}_rate",
        )

    raw_categories = _object(payload.get("by_category"), f"{label}.by_category")
    _require_equal(
        set(raw_categories), set(category_counts), f"{label}.by_category categories"
    )
    for category, counters in category_counts.items():
        context = f"{label}.by_category[{category!r}]"
        values = _object(raw_categories[category], context)
        _require_equal(
            _nonnegative_int(values.get("total"), f"{context}.total"),
            counters["total"],
            f"{context}.total",
        )
        _require_equal(
            _nonnegative_int(values.get("top1"), f"{context}.top1"),
            counters["top1"],
            f"{context}.top1",
        )
        _require_rate(
            values.get("top1_rate"),
            counters["top1"] / counters["total"],
            f"{context}.top1_rate",
        )
        if top_k != 1:
            metric = f"top{top_k}"
            _require_equal(
                _nonnegative_int(values.get(metric), f"{context}.{metric}"),
                counters["top_k"],
                f"{context}.{metric}",
            )
            _require_rate(
                values.get(f"{metric}_rate"),
                counters["top_k"] / counters["total"],
                f"{context}.{metric}_rate",
            )

    return {"top_k": top_k, "cases": cases}


def _backend_metrics(cases: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(cases)
    top1_hits = sum(int(case["top1_hit"]) for case in cases)
    top_k_hits = sum(int(case["top_k_hit"]) for case in cases)
    return {
        "top1_hits": top1_hits,
        "top1_rate": top1_hits / total,
        "top_k_hits": top_k_hits,
        "top_k_rate": top_k_hits / total,
    }


def _winner(a_rank: int | None, b_rank: int | None) -> str:
    if a_rank is None and b_rank is None:
        return "tie"
    if a_rank is None:
        return "b"
    if b_rank is None:
        return "a"
    if a_rank < b_rank:
        return "a"
    if b_rank < a_rank:
        return "b"
    return "tie"


def compare_reports(
    a_report: Any,
    b_report: Any,
    *,
    a_name: str,
    b_name: str,
) -> dict[str, Any]:
    a_name = _string(a_name.strip(), "a_name")
    b_name = _string(b_name.strip(), "b_name")
    if a_name == b_name:
        raise ValueError("a_name and b_name must be distinct")

    a = validate_report(a_report, "a_report")
    b = validate_report(b_report, "b_report")
    _require_equal(b["top_k"], a["top_k"], "report top_k")

    a_ids = [case["id"] for case in a["cases"]]
    b_ids = [case["id"] for case in b["cases"]]
    _require_equal(b_ids, a_ids, "report case ids")

    cases: list[dict[str, Any]] = []
    wins = Counter({"a": 0, "b": 0, "ties": 0})
    for index, (a_case, b_case) in enumerate(zip(a["cases"], b["cases"], strict=True)):
        context = f"case {a_case['id']!r}"
        for field in ("category", "reading", "expected"):
            _require_equal(b_case[field], a_case[field], f"{context}.{field}")

        winner = _winner(a_case["expected_rank"], b_case["expected_rank"])
        wins["ties" if winner == "tie" else winner] += 1
        rank_delta = (
            b_case["expected_rank"] - a_case["expected_rank"]
            if a_case["expected_rank"] is not None
            and b_case["expected_rank"] is not None
            else None
        )
        cases.append(
            {
                "id": a_case["id"],
                "category": a_case["category"],
                "reading": a_case["reading"],
                "expected": a_case["expected"],
                "a": {
                    "observed": a_case["observed"],
                    "expected_rank": a_case["expected_rank"],
                    "top1_hit": a_case["top1_hit"],
                    "top_k_hit": a_case["top_k_hit"],
                },
                "b": {
                    "observed": b_case["observed"],
                    "expected_rank": b_case["expected_rank"],
                    "top1_hit": b_case["top1_hit"],
                    "top_k_hit": b_case["top_k_hit"],
                },
                "winner": winner,
                "rank_delta": rank_delta,
            }
        )

    a_metrics = _backend_metrics(a["cases"])
    b_metrics = _backend_metrics(b["cases"])
    delta = {
        key: b_metrics[key] - a_metrics[key]
        for key in ("top1_hits", "top1_rate", "top_k_hits", "top_k_rate")
    }
    return {
        "schema": OUTPUT_SCHEMA,
        "top_k": a["top_k"],
        "rank_scope": "observed_top_k",
        "delta_direction": "b_minus_a",
        "backends": {"a": a_name, "b": b_name},
        "metrics": {"a": a_metrics, "b": b_metrics, "delta": delta},
        "wins": {"a": wins["a"], "b": wins["b"], "ties": wins["ties"]},
        "cases": cases,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--a-report", type=Path, required=True)
    parser.add_argument("--b-report", type=Path, required=True)
    parser.add_argument("--a-name", required=True)
    parser.add_argument("--b-name", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = compare_reports(
            load_json(args.a_report),
            load_json(args.b_report),
            a_name=args.a_name,
            b_name=args.b_name,
        )
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return 0
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
