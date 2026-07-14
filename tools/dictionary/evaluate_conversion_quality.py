#!/usr/bin/env python3
"""Evaluate a versioned conversion corpus against JSONL candidate output.

Each result line must contain an ``id`` and either ``candidates`` (a list of
strings or objects with a ``text`` field) or ``candidate_window.items``. The
script is dependency-free so it can run in CI and on a release machine.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


def load_corpus(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    required = {"id", "reading", "expected", "category"}
    if not rows or not required.issubset(rows[0]):
        raise ValueError(f"{path}: required columns are {sorted(required)}")
    ids = [row["id"] for row in rows]
    if any(not value for value in ids) or len(ids) != len(set(ids)):
        raise ValueError(f"{path}: ids must be non-empty and unique")
    for line_number, row in enumerate(rows, 2):
        if not row["reading"]:
            raise ValueError(f"{path}:{line_number}: reading must not be empty")
        if not row["category"]:
            raise ValueError(f"{path}:{line_number}: category must not be empty")
        expected = row["expected"].split("|")
        if not expected or any(not alternative for alternative in expected):
            raise ValueError(
                f"{path}:{line_number}: expected alternatives must not be empty"
            )
    return rows


def candidate_texts(payload: dict[str, Any]) -> list[str]:
    if "candidates" in payload:
        raw = payload["candidates"]
    else:
        candidate_window = payload.get("candidate_window")
        if not isinstance(candidate_window, dict):
            raise ValueError(
                "result requires a candidates array or candidate_window object"
            )
        if "items" not in candidate_window:
            raise ValueError("candidate_window requires an items array")
        raw = candidate_window["items"]
    if not isinstance(raw, list):
        raise ValueError("result candidates must be a list")
    texts: list[str] = []
    for item in raw:
        if isinstance(item, str):
            texts.append(item)
        elif isinstance(item, dict) and isinstance(item.get("text"), str):
            texts.append(item["text"])
        else:
            raise ValueError(f"unsupported candidate item: {item!r}")
    return texts


def load_results(path: Path) -> dict[str, list[str]]:
    results: dict[str, list[str]] = {}
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
            results[result_id] = candidate_texts(payload)
    return results


def evaluate(corpus: list[dict[str, str]], results: dict[str, list[str]], top_k: int) -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    by_category: dict[str, Counter[str]] = {}
    missing: list[str] = []
    top_k_metric = f"top{top_k}"
    for row in corpus:
        result = results.get(row["id"])
        if result is None:
            missing.append(row["id"])
            continue
        expected = [value for value in row["expected"].split("|") if value]
        top1 = bool(result) and result[0] in expected
        top_k_hit = any(value in expected for value in result[:top_k])
        counters = by_category.setdefault(row["category"], Counter())
        counters["total"] += 1
        counters["top1"] += int(top1)
        if top_k_metric != "top1":
            counters[top_k_metric] += int(top_k_hit)
        case = {
            "id": row["id"],
            "category": row["category"],
            "reading": row["reading"],
            "expected": expected,
            "top1": top1,
            "observed": result[:top_k],
        }
        if top_k_metric != "top1":
            case[top_k_metric] = top_k_hit
        cases.append(case)

    evaluated = len(cases)
    top1_hits = sum(case["top1"] for case in cases)
    top_k_hits = (
        top1_hits
        if top_k_metric == "top1"
        else sum(case[top_k_metric] for case in cases)
    )
    category_reports: dict[str, dict[str, Any]] = {}
    for category, counter in sorted(by_category.items()):
        category_report: dict[str, Any] = dict(counter) | {
            "top1_rate": counter["top1"] / counter["total"],
        }
        if top_k_metric != "top1":
            category_report[f"{top_k_metric}_rate"] = (
                counter[top_k_metric] / counter["total"]
            )
        category_reports[category] = category_report

    report: dict[str, Any] = {
        "schema": "hazkey.conversion-quality-report.v1",
        "top_k": top_k,
        "corpus_cases": len(corpus),
        "evaluated_cases": evaluated,
        "missing_results": missing,
        "top1_hits": top1_hits,
        "top1_rate": top1_hits / evaluated if evaluated else 0.0,
        "by_category": category_reports,
        "cases": cases,
    }
    if top_k_metric != "top1":
        report[f"{top_k_metric}_hits"] = top_k_hits
        report[f"{top_k_metric}_rate"] = top_k_hits / evaluated if evaluated else 0.0
    return report


def self_test(corpus_path: Path) -> None:
    corpus = load_corpus(corpus_path)
    for row in corpus:
        expected = [value for value in row["expected"].split("|") if value]
        if row["category"] == "protected" and row["reading"] not in expected:
            raise AssertionError(
                f"protected case {row['id']} must accept its exact input surface"
            )
    synthetic = {
        row["id"]: {"id": row["id"], "candidates": [row["expected"].split("|")[0]]}
        for row in corpus
    }
    results = {result_id: candidate_texts(payload) for result_id, payload in synthetic.items()}
    report = evaluate(corpus, results, top_k=10)
    if report["top1_rate"] != 1.0 or report["missing_results"]:
        raise AssertionError("synthetic corpus evaluation did not reach 100%")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--results", type=Path)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.top_k < 1:
        parser.error("--top-k must be positive")
    try:
        if args.self_test:
            self_test(args.corpus)
            print(f"self-test ok: {args.corpus}")
            return 0
        if args.results is None:
            parser.error("--results is required unless --self-test is used")
        report = evaluate(load_corpus(args.corpus), load_results(args.results), args.top_k)
        encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.write_text(encoded, encoding="utf-8")
        else:
            sys.stdout.write(encoded)
        return 0 if not report["missing_results"] else 2
    except (OSError, ValueError, json.JSONDecodeError, AssertionError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
