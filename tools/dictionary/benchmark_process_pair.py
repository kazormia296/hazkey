#!/usr/bin/env python3
"""Run two process backends in alternating AB/BA cycles.

The manifest is intentionally strict and contains exactly two backend specs::

    {
      "schema": "hazkey.process-backend-pair-manifest.v1",
      "a": {
        "backend_name": "hazkey",
        "argv": ["/path/to/backend", "--flag"],
        "cwd": ".",
        "environment_overrides": {"KEY": "VALUE"},
        "expected_exit_code": 0,
        "timeout_seconds": 60
      },
      "b": { ... }
    }

Relative working directories are resolved against the manifest directory.
Each child measurement is delegated to ``benchmark_process_backend``, so it
uses the same Linux ``wait4(pid)`` RSS and monotonic wall-clock boundaries.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys
from typing import Any, Mapping, Sequence


if __package__:
    from . import benchmark_process_backend as process_backend
else:
    import benchmark_process_backend as process_backend


MANIFEST_SCHEMA = "hazkey.process-backend-pair-manifest.v1"
OUTPUT_SCHEMA = "hazkey.process-backend-pair-benchmark.v1"
_MANIFEST_FIELDS = {"schema", "a", "b"}
_BACKEND_FIELDS = {
    "backend_name",
    "argv",
    "cwd",
    "environment_overrides",
    "expected_exit_code",
    "timeout_seconds",
}


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _fingerprint(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value)).hexdigest()


def _require_exact_fields(
    value: Mapping[str, Any], expected: set[str], context: str
) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        raise ValueError(
            f"{context} fields do not match schema; "
            f"missing={missing!r}, unexpected={unexpected!r}"
        )


def _validate_backend(
    value: Any,
    *,
    label: str,
    manifest_directory: Path,
) -> dict[str, Any]:
    context = f"manifest.{label}"
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be an object")
    _require_exact_fields(value, _BACKEND_FIELDS, context)

    backend_name = value["backend_name"]
    if not isinstance(backend_name, str) or not backend_name:
        raise ValueError(f"{context}.backend_name must be a non-empty string")

    argv = value["argv"]
    if not isinstance(argv, list) or not argv:
        raise ValueError(f"{context}.argv must be a non-empty array")
    for index, argument in enumerate(argv):
        if not isinstance(argument, str):
            raise ValueError(f"{context}.argv[{index}] must be a string")
        if "\0" in argument:
            raise ValueError(f"{context}.argv[{index}] must not contain NUL")
    if not argv[0]:
        raise ValueError(f"{context}.argv[0] must be non-empty")

    cwd_value = value["cwd"]
    if not isinstance(cwd_value, str) or not cwd_value:
        raise ValueError(f"{context}.cwd must be a non-empty string")
    cwd = Path(cwd_value)
    if not cwd.is_absolute():
        cwd = manifest_directory / cwd
    try:
        cwd = cwd.resolve(strict=True)
    except OSError as error:
        raise ValueError(f"{context}.cwd cannot be resolved: {error}") from error
    if not cwd.is_dir():
        raise ValueError(f"{context}.cwd is not a directory: {cwd}")

    environment = value["environment_overrides"]
    if not isinstance(environment, dict):
        raise ValueError(f"{context}.environment_overrides must be an object")
    normalized_environment: dict[str, str] = {}
    for name, environment_value in environment.items():
        if not isinstance(name, str) or not name or "=" in name or "\0" in name:
            raise ValueError(
                f"{context}.environment_overrides has invalid name {name!r}"
            )
        if not isinstance(environment_value, str) or "\0" in environment_value:
            raise ValueError(
                f"{context}.environment_overrides[{name!r}] must be a "
                "NUL-free string"
            )
        normalized_environment[name] = environment_value

    expected_exit_code = value["expected_exit_code"]
    if (
        isinstance(expected_exit_code, bool)
        or not isinstance(expected_exit_code, int)
        or not 0 <= expected_exit_code <= 255
    ):
        raise ValueError(
            f"{context}.expected_exit_code must be an integer from 0 through 255"
        )

    timeout_seconds = value["timeout_seconds"]
    if timeout_seconds is not None and (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(float(timeout_seconds))
        or timeout_seconds <= 0
    ):
        raise ValueError(
            f"{context}.timeout_seconds must be a finite positive number or null"
        )

    return {
        "backend_name": backend_name,
        "argv": list(argv),
        "cwd": cwd,
        "environment_overrides": normalized_environment,
        "expected_exit_code": expected_exit_code,
        "timeout_seconds": (
            None if timeout_seconds is None else float(timeout_seconds)
        ),
    }


def load_manifest(path: Path | str) -> dict[str, Any]:
    manifest_path = Path(path).resolve(strict=True)
    if not manifest_path.is_file():
        raise ValueError(f"manifest is not a file: {manifest_path}")
    try:
        payload = json.loads(
            manifest_path.read_text(encoding="utf-8"),
            object_pairs_hook=_object_without_duplicate_keys,
        )
    except json.JSONDecodeError as error:
        raise ValueError(
            f"{manifest_path}: invalid JSON at line {error.lineno}: {error.msg}"
        ) from error
    if not isinstance(payload, dict):
        raise ValueError("manifest must be an object")
    _require_exact_fields(payload, _MANIFEST_FIELDS, "manifest")
    if payload["schema"] != MANIFEST_SCHEMA:
        raise ValueError(f"manifest.schema must be {MANIFEST_SCHEMA}")

    return {
        "path": manifest_path,
        "fingerprint": _fingerprint(payload),
        "a": _validate_backend(
            payload["a"], label="a", manifest_directory=manifest_path.parent
        ),
        "b": _validate_backend(
            payload["b"], label="b", manifest_directory=manifest_path.parent
        ),
    }


def _nearest_rank_p95(samples: Sequence[float]) -> float:
    if not samples:
        raise ValueError("at least one sample is required")
    ordered = sorted(samples)
    index = min(len(ordered) - 1, math.ceil(len(ordered) * 0.95) - 1)
    return ordered[index]


def _wall_summary(samples: Sequence[float]) -> dict[str, float]:
    if not samples or any(not math.isfinite(value) or value < 0 for value in samples):
        raise ValueError("wall samples must be finite non-negative values")
    return {
        "mean": statistics.fmean(samples),
        "median": float(statistics.median(samples)),
        "p95": _nearest_rank_p95(samples),
        "minimum": min(samples),
        "maximum": max(samples),
    }


def _measure_backend(specification: Mapping[str, Any]) -> dict[str, Any]:
    result = process_backend.benchmark_process_backend(
        specification["argv"],
        runs=1,
        backend_name=specification["backend_name"],
        cwd=specification["cwd"],
        environment_overrides=specification["environment_overrides"],
        expected_exit_code=specification["expected_exit_code"],
        timeout_seconds=specification["timeout_seconds"],
    )
    if result.get("schema") != process_backend.OUTPUT_SCHEMA:
        raise RuntimeError("process backend returned an unexpected schema")
    if result.get("backend") != specification["backend_name"]:
        raise RuntimeError("process backend returned the wrong backend_name")
    if result.get("expected_exit_code") != specification["expected_exit_code"]:
        raise RuntimeError("process backend returned the wrong expected_exit_code")
    if result.get("timeout_seconds") != specification["timeout_seconds"]:
        raise RuntimeError("process backend returned the wrong timeout_seconds")
    if result.get("run_count") != 1 or len(result.get("raw_runs", [])) != 1:
        raise RuntimeError("process backend returned an invalid single-run result")
    if (
        result["raw_runs"][0].get("exit_code")
        != specification["expected_exit_code"]
    ):
        raise RuntimeError("process backend returned the wrong child exit_code")
    return result


def benchmark_process_pair(
    manifest_path: Path | str,
    *,
    cycles: int,
) -> dict[str, Any]:
    if isinstance(cycles, bool) or not isinstance(cycles, int) or cycles <= 0:
        raise ValueError("cycles must be a positive integer")
    manifest = load_manifest(manifest_path)
    specifications = {"a": manifest["a"], "b": manifest["b"]}
    raw_executions: list[dict[str, Any]] = []
    measurements: dict[str, list[dict[str, Any]]] = {"a": [], "b": []}
    provenance: dict[str, dict[str, Any]] = {}

    for cycle in range(1, cycles + 1):
        order = ("a", "b") if cycle % 2 == 1 else ("b", "a")
        for position, label in enumerate(order, 1):
            result = _measure_backend(specifications[label])
            command_fingerprint = result.get("command_fingerprint")
            execution_fingerprint = result.get("execution_fingerprint")
            if not isinstance(command_fingerprint, str) or not command_fingerprint:
                raise RuntimeError(f"backend {label} omitted command_fingerprint")
            if not isinstance(execution_fingerprint, str) or not execution_fingerprint:
                raise RuntimeError(f"backend {label} omitted execution_fingerprint")
            current_provenance = {
                "backend_name": result.get("backend"),
                "command_fingerprint": command_fingerprint,
                "execution_fingerprint": execution_fingerprint,
                "process": result.get("provenance"),
                "expected_exit_code": result.get("expected_exit_code"),
                "timeout_seconds": result.get("timeout_seconds"),
            }
            if label in provenance and provenance[label] != current_provenance:
                raise RuntimeError(
                    f"backend {label} provenance changed during paired measurement"
                )
            provenance.setdefault(label, current_provenance)

            measured = result["raw_runs"][0]
            execution = {
                "sequence": len(raw_executions) + 1,
                "cycle": cycle,
                "position": position,
                "backend": label,
                "backend_name": specifications[label]["backend_name"],
                **measured,
            }
            execution.pop("run", None)
            raw_executions.append(execution)
            measurements[label].append(execution)

    backend_summaries: dict[str, dict[str, Any]] = {}
    for label in ("a", "b"):
        wall_times = [
            float(measurement["wall_time_ms"])
            for measurement in measurements[label]
        ]
        rss_values = [
            int(measurement["maximum_rss_kib"])
            for measurement in measurements[label]
        ]
        backend_summaries[label] = {
            **provenance[label],
            "runs": cycles,
            "wall_time_ms": _wall_summary(wall_times),
            "maximum_rss_kib": max(rss_values),
        }

    paired_samples: list[dict[str, float | int]] = []
    ratios: list[float] = []
    for cycle in range(1, cycles + 1):
        a_wall = float(measurements["a"][cycle - 1]["wall_time_ms"])
        b_wall = float(measurements["b"][cycle - 1]["wall_time_ms"])
        if a_wall <= 0 or b_wall <= 0:
            raise RuntimeError(
                f"cycle {cycle} has non-positive wall time; paired ratio is undefined"
            )
        ratio = a_wall / b_wall
        ratios.append(ratio)
        paired_samples.append(
            {
                "cycle": cycle,
                "a_wall_time_ms": a_wall,
                "b_wall_time_ms": b_wall,
                "a_over_b": ratio,
            }
        )

    ratio_summary = _wall_summary(ratios)
    ratio_summary["ratio_of_means"] = (
        backend_summaries["a"]["wall_time_ms"]["mean"]
        / backend_summaries["b"]["wall_time_ms"]["mean"]
    )
    return {
        "schema": OUTPUT_SCHEMA,
        "manifest": {
            "schema": MANIFEST_SCHEMA,
            "path": str(manifest["path"]),
            "fingerprint": manifest["fingerprint"],
        },
        "cycles": cycles,
        "ordering": "odd cycles AB; even cycles BA",
        "raw_execution_order": raw_executions,
        "backends": backend_summaries,
        "paired_wall_ratio": {
            "definition": "a.wall_time_ms / b.wall_time_ms within each cycle",
            "samples": paired_samples,
            **ratio_summary,
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure two process backends in alternating AB/BA cycles."
    )
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--cycles", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        manifest_path = args.manifest.resolve(strict=True)
        output_path = args.output.resolve()
        if output_path == manifest_path:
            raise ValueError("--output must differ from --manifest")
        output_parent = output_path.parent.resolve(strict=True)
        if not output_parent.is_dir():
            raise ValueError(f"output parent is not a directory: {output_parent}")

        result = benchmark_process_pair(manifest_path, cycles=args.cycles)
        encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        process_backend.atomic_write_text(output_path, encoded)
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
