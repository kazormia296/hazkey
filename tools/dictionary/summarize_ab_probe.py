#!/usr/bin/env python3
"""Validate and summarize one or more Hazkey A/B probe JSONL runs.

Each input file is one run. Latency statistics are recomputed from every raw
sample, P95 uses the nearest-rank definition, and max observed RSS is the
largest available before/after RSS snapshot across all cases and runs. Source
and dictionary provenance must be identical in every result and run.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import statistics
import sys
from typing import Any


INPUT_SCHEMA = "hazkey.ab-probe-result.v1"
OUTPUT_SCHEMA = "hazkey.ab-probe-summary.v1"


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


def _nonnegative_number(value: Any, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{context} must be a finite non-negative number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{context} must be a finite non-negative number")
    return result


def _optional_rss(value: Any, context: str) -> int | None:
    if value is None:
        return None
    return _nonnegative_int(value, context)


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
    if result.get("schema") != INPUT_SCHEMA:
        raise ValueError(f"{context}.schema must be {INPUT_SCHEMA}")

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
    dictionary_path = _string(
        _required(result, "dictionary_path", context),
        f"{context}.dictionary_path",
    )
    dictionary_fingerprint = _string(
        _required(result, "dictionary_fingerprint", context),
        f"{context}.dictionary_fingerprint",
    )
    candidates = _array(
        _required(result, "candidates", context), f"{context}.candidates"
    )
    for index, candidate in enumerate(candidates):
        _string(candidate, f"{context}.candidates[{index}]")

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

    return {
        "id": case_id,
        "category": category,
        "backend": backend,
        "backend_version": backend_version,
        "source_ref": source_ref,
        "dictionary_path": dictionary_path,
        "dictionary_fingerprint": dictionary_fingerprint,
        "warmups": warmups,
        "iterations": iterations,
        "samples": samples,
        "rss": [rss_before, rss_after],
    }


def load_run(path: Path) -> dict[str, Any]:
    cases: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
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
    for case in cases.values():
        for field in (
            "backend",
            "backend_version",
            "source_ref",
            "dictionary_path",
            "dictionary_fingerprint",
            "warmups",
            "iterations",
        ):
            if case[field] != first[field]:
                raise ValueError(f"{path}: inconsistent {field} within run")
    return {
        "path": path,
        "backend": first["backend"],
        "backend_version": first["backend_version"],
        "source_ref": first["source_ref"],
        "dictionary_path": first["dictionary_path"],
        "dictionary_fingerprint": first["dictionary_fingerprint"],
        "warmups": first["warmups"],
        "iterations": first["iterations"],
        "cases": cases,
    }


def summarize(paths: list[Path]) -> dict[str, Any]:
    if not paths:
        raise ValueError("at least one probe JSONL run is required")
    runs = [load_run(path) for path in paths]
    first = runs[0]
    expected_ids = set(first["cases"])
    expected_categories = {
        case_id: case["category"] for case_id, case in first["cases"].items()
    }
    for run in runs[1:]:
        for field in (
            "backend",
            "backend_version",
            "source_ref",
            "dictionary_path",
            "dictionary_fingerprint",
            "warmups",
            "iterations",
        ):
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

    all_samples: list[float] = []
    run_totals: list[float] = []
    rss_values: list[int] = []
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

    expected_measurements = (
        len(runs) * len(expected_ids) * first["iterations"]
    )
    if len(all_samples) != expected_measurements:
        raise ValueError(
            "measured conversion count is inconsistent with runs, cases, and iterations"
        )
    return {
        "schema": OUTPUT_SCHEMA,
        "backend": first["backend"],
        "backend_version": first["backend_version"],
        "provenance": {
            "source_ref": first["source_ref"],
            "dictionary_path": first["dictionary_path"],
            "dictionary_fingerprint": first["dictionary_fingerprint"],
        },
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
    }


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
