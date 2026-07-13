#!/usr/bin/env python3
"""Summarize one or more conversion-quality reports."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


def summarize(paths: list[Path]) -> dict[str, Any]:
    reports = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    if not reports:
        raise ValueError("at least one report is required")
    top_k = reports[0]["top_k"]
    if any(report.get("top_k") != top_k for report in reports):
        raise ValueError("all reports must use the same top_k")
    totals = defaultdict(int)
    for report in reports:
        totals["corpus_cases"] += report.get("corpus_cases", 0)
        totals["evaluated_cases"] += report.get("evaluated_cases", 0)
        totals["top1_hits"] += report.get("top1_hits", 0)
        totals[f"top{top_k}_hits"] += report.get(f"top{top_k}_hits", 0)
    evaluated = totals["evaluated_cases"]
    categories: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for report in reports:
        for category, values in report.get("by_category", {}).items():
            for key in ("total", "top1", f"top{top_k}"):
                categories[category][key] += values.get(key, 0)
    return {
        "schema": "hazkey.conversion-quality-summary.v1",
        "reports": [str(path) for path in paths],
        "top_k": top_k,
        **dict(totals),
        "top1_rate": totals["top1_hits"] / evaluated if evaluated else 0.0,
        f"top{top_k}_rate": totals[f"top{top_k}_hits"] / evaluated if evaluated else 0.0,
        "by_category": {
            category: {
                **dict(values),
                "top1_rate": values["top1"] / values["total"] if values["total"] else 0.0,
                f"top{top_k}_rate": values[f"top{top_k}"] / values["total"]
                if values["total"]
                else 0.0,
            }
            for category, values in sorted(categories.items())
        },
    }


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
