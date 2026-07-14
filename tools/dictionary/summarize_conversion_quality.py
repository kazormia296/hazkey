#!/usr/bin/env python3
"""Summarize one or more conversion-quality reports."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


REPORT_SCHEMA = "hazkey.conversion-quality-report.v1"


def nonnegative_int(value: Any, field: str) -> int:
    # bool is an int subclass in Python, but it is never a valid aggregate.
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def validate_report(report: Any, path: Path) -> dict[str, Any]:
    if not isinstance(report, dict):
        raise ValueError(f"{path}: report must be an object")
    if report.get("schema") != REPORT_SCHEMA:
        raise ValueError(f"{path}: schema must be {REPORT_SCHEMA}")

    top_k = report.get("top_k")
    if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k < 1:
        raise ValueError(f"{path}: top_k must be a positive integer")

    corpus_cases = nonnegative_int(
        report.get("corpus_cases"), f"{path}: corpus_cases"
    )
    evaluated_cases = nonnegative_int(
        report.get("evaluated_cases"), f"{path}: evaluated_cases"
    )
    top1_hits = nonnegative_int(report.get("top1_hits"), f"{path}: top1_hits")
    if evaluated_cases > corpus_cases:
        raise ValueError(f"{path}: evaluated_cases exceeds corpus_cases")
    if top1_hits > evaluated_cases:
        raise ValueError(f"{path}: top1_hits exceeds evaluated_cases")

    top_k_metric = f"top{top_k}"
    category_expectations = {
        "total": (evaluated_cases, "evaluated_cases"),
        "top1": (top1_hits, "top1_hits"),
    }
    if top_k_metric != "top1":
        top_k_hits = nonnegative_int(
            report.get(f"{top_k_metric}_hits"),
            f"{path}: {top_k_metric}_hits",
        )
        if top_k_hits > evaluated_cases:
            raise ValueError(
                f"{path}: {top_k_metric}_hits exceeds evaluated_cases"
            )
        if top_k_hits < top1_hits:
            raise ValueError(
                f"{path}: {top_k_metric}_hits is less than top1_hits"
            )
        category_expectations[top_k_metric] = (
            top_k_hits,
            f"{top_k_metric}_hits",
        )

    categories = report.get("by_category")
    if not isinstance(categories, dict):
        raise ValueError(f"{path}: by_category must be an object")
    category_totals = {metric: 0 for metric in category_expectations}
    for category, values in categories.items():
        if not isinstance(category, str) or not category:
            raise ValueError(f"{path}: category names must be non-empty strings")
        if not isinstance(values, dict):
            raise ValueError(f"{path}: category {category} must be an object")
        total = nonnegative_int(values.get("total"), f"{path}: {category}.total")
        category_top1 = nonnegative_int(
            values.get("top1"), f"{path}: {category}.top1"
        )
        if category_top1 > total:
            raise ValueError(f"{path}: {category}.top1 exceeds total")
        category_totals["total"] += total
        category_totals["top1"] += category_top1
        if top_k_metric != "top1":
            category_top_k = nonnegative_int(
                values.get(top_k_metric), f"{path}: {category}.{top_k_metric}"
            )
            if category_top_k > total:
                raise ValueError(
                    f"{path}: {category}.{top_k_metric} exceeds total"
                )
            if category_top_k < category_top1:
                raise ValueError(
                    f"{path}: {category}.{top_k_metric} is less than "
                    f"{category}.top1"
                )
            category_totals[top_k_metric] += category_top_k

    for metric, (expected, report_field) in category_expectations.items():
        actual = category_totals[metric]
        if actual != expected:
            raise ValueError(
                f"{path}: by_category {metric} total {actual} "
                f"does not match {report_field} {expected}"
            )
    return report


def summarize(paths: list[Path]) -> dict[str, Any]:
    if not paths:
        raise ValueError("at least one report is required")
    reports = [
        validate_report(json.loads(path.read_text(encoding="utf-8")), path)
        for path in paths
    ]
    top_k = reports[0]["top_k"]
    if any(report.get("top_k") != top_k for report in reports):
        raise ValueError("all reports must use the same top_k")
    top_k_metric = f"top{top_k}"
    totals = defaultdict(int)
    for report in reports:
        totals["corpus_cases"] += report.get("corpus_cases", 0)
        totals["evaluated_cases"] += report.get("evaluated_cases", 0)
        totals["top1_hits"] += report.get("top1_hits", 0)
        if top_k_metric != "top1":
            totals[f"{top_k_metric}_hits"] += report.get(
                f"{top_k_metric}_hits", 0
            )
    evaluated = totals["evaluated_cases"]
    categories: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for report in reports:
        for category, values in report.get("by_category", {}).items():
            metric_keys = ["total", "top1"]
            if top_k_metric != "top1":
                metric_keys.append(top_k_metric)
            for key in metric_keys:
                categories[category][key] += values.get(key, 0)
    category_summaries: dict[str, dict[str, Any]] = {}
    for category, values in sorted(categories.items()):
        category_summary: dict[str, Any] = {
            **dict(values),
            "top1_rate": values["top1"] / values["total"]
            if values["total"]
            else 0.0,
        }
        if top_k_metric != "top1":
            category_summary[f"{top_k_metric}_rate"] = (
                values[top_k_metric] / values["total"] if values["total"] else 0.0
            )
        category_summaries[category] = category_summary

    summary: dict[str, Any] = {
        "schema": "hazkey.conversion-quality-summary.v1",
        "reports": [str(path) for path in paths],
        "top_k": top_k,
        **dict(totals),
        "top1_rate": totals["top1_hits"] / evaluated if evaluated else 0.0,
        "by_category": category_summaries,
    }
    if top_k_metric != "top1":
        summary[f"{top_k_metric}_rate"] = (
            totals[f"{top_k_metric}_hits"] / evaluated if evaluated else 0.0
        )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("reports", nargs="+", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        output = json.dumps(summarize(args.reports), ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.write_text(output, encoding="utf-8")
        else:
            print(output, end="")
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
