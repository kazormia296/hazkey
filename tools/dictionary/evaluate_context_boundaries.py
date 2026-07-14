#!/usr/bin/env python3
"""Measure ranking drift between one-shot and split-context conversion.

This is intentionally a spike, not a pass/fail language-quality oracle. A
result line contains ``id``, ``whole_candidates`` and ``split_candidates``;
the report makes top-1 drift explicit so a later ranking change can be
evaluated against the same fixture.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any


def load_fixture(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    required = {"id", "reading", "split_at", "category"}
    if not rows or not required.issubset(rows[0]):
        raise ValueError(f"{path}: required columns are {sorted(required)}")
    ids = [row["id"] for row in rows]
    if len(ids) != len(set(ids)) or any(not value for value in ids):
        raise ValueError(f"{path}: ids must be non-empty and unique")
    for line_number, row in enumerate(rows, 2):
        reading = row["reading"]
        if not reading:
            raise ValueError(f"{path}:{line_number}: reading must not be empty")
        if not row["category"]:
            raise ValueError(f"{path}:{line_number}: category must not be empty")
        split_at = row["split_at"]
        if re.fullmatch(r"[1-9][0-9]*", split_at) is None:
            raise ValueError(
                f"{path}:{line_number}: split_at must be a canonical ASCII decimal integer"
            )
        split_index = int(split_at)
        if not 1 <= split_index < len(reading):
            raise ValueError(
                f"{path}:{line_number}: split_at must be inside the reading"
            )
    return rows


def candidates(value: Any, field: str = "candidate list") -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be an array")
    result: list[str] = []
    for item in value:
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, dict) and isinstance(item.get("text"), str):
            result.append(item["text"])
        else:
            raise ValueError(f"unsupported candidate item: {item!r}")
    return result


def load_results(path: Path) -> dict[str, dict[str, list[str]]]:
    results: dict[str, dict[str, list[str]]] = {}
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            payload = json.loads(line)
            if not isinstance(payload, dict) or not isinstance(payload.get("id"), str):
                raise ValueError(f"{path}:{line_number}: result requires string id")
            result_id = payload["id"]
            if not result_id:
                raise ValueError(f"{path}:{line_number}: result id must not be empty")
            if result_id in results:
                raise ValueError(f"{path}:{line_number}: duplicate result id {result_id}")
            if "whole_candidates" not in payload or "split_candidates" not in payload:
                raise ValueError(
                    f"{path}:{line_number}: result requires whole_candidates and "
                    "split_candidates"
                )
            results[result_id] = {
                "whole": candidates(
                    payload["whole_candidates"], "whole_candidates"
                ),
                "split": candidates(
                    payload["split_candidates"], "split_candidates"
                ),
            }
    return results


def evaluate(fixture: list[dict[str, str]], results: dict[str, dict[str, list[str]]]) -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    by_category: dict[str, Counter[str]] = {}
    missing: list[str] = []
    for row in fixture:
        result = results.get(row["id"])
        if result is None:
            missing.append(row["id"])
            continue
        if "whole" not in result or "split" not in result:
            raise ValueError(f"result {row['id']} requires whole and split candidates")
        whole = candidates(result["whole"], f"result {row['id']} whole candidates")
        split = candidates(result["split"], f"result {row['id']} split candidates")
        comparable = bool(whole and split)
        drift = comparable and whole[0] != split[0]
        counter = by_category.setdefault(row["category"], Counter())
        counter["total"] += 1
        counter["comparable"] += int(comparable)
        counter["drift"] += int(drift)
        cases.append(
            {
                "id": row["id"],
                "category": row["category"],
                "reading": row["reading"],
                "split_at": int(row["split_at"]),
                "whole_top1": whole[0] if whole else None,
                "split_top1": split[0] if split else None,
                "comparable": comparable,
                "top1_drift": drift,
            }
        )
    comparable_count = sum(case["comparable"] for case in cases)
    drift_count = sum(case["top1_drift"] for case in cases)
    return {
        "schema": "hazkey.context-boundary-report.v1",
        "fixture_cases": len(fixture),
        "evaluated_cases": len(cases),
        "missing_results": missing,
        "comparable_cases": comparable_count,
        "top1_drift_cases": drift_count,
        "top1_drift_rate": drift_count / comparable_count if comparable_count else 0.0,
        "by_category": {category: dict(values) for category, values in sorted(by_category.items())},
        "cases": cases,
    }


def self_test(fixture_path: Path) -> None:
    fixture = load_fixture(fixture_path)
    results = {
        row["id"]: {"whole": [row["reading"]], "split": [row["reading"]]}
        for row in fixture
    }
    report = evaluate(fixture, results)
    if (
        report["missing_results"]
        or report["comparable_cases"] != len(fixture)
        or report["top1_drift_cases"]
    ):
        raise AssertionError("synthetic boundary evaluation drifted")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--results", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--fail-on-drift", action="store_true")
    args = parser.parse_args()
    try:
        if args.self_test:
            self_test(args.fixture)
            print(f"self-test ok: {args.fixture}")
            return 0
        if args.results is None:
            parser.error("--results is required unless --self-test is used")
        report = evaluate(load_fixture(args.fixture), load_results(args.results))
        encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.write_text(encoded, encoding="utf-8")
        else:
            sys.stdout.write(encoded)
        if report["missing_results"]:
            return 2
        if args.fail_on_drift and (
            report["comparable_cases"] == 0
            or report["comparable_cases"] != report["evaluated_cases"]
        ):
            return 2
        return 1 if args.fail_on_drift and report["top1_drift_cases"] else 0
    except (OSError, ValueError, json.JSONDecodeError, AssertionError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
