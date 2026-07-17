#!/usr/bin/env python3
"""Evaluate three Hazkey+Zenzai boundary policies with empty context.

This evaluator deliberately holds context and acquisition identity constant and
changes only the boundary policy:

* an isolated dictionary boundary converter with Zenzai disabled for boundary
  discovery;
* the native first clause selected by the Zenzai-scored whole-sentence path;
* a Mozc Top-1 boundary fixed by a canonically derived sidecar.

The acceptable-path generation is diagnostic, not a locked unseen holdout.  A
report from this program therefore cannot authorize a production boundary or
Top-1 override policy.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import statistics
import sys
from typing import Any, Sequence

try:
    from . import evaluate_mozc_acceptable_path_boundaries as acceptable
    from . import evaluate_mozc_zenzai_hybrid_quality as quality
    from . import evaluate_zenzai_left_context_quality as shared
except ImportError:  # Direct execution from tools/dictionary.
    import evaluate_mozc_acceptable_path_boundaries as acceptable
    import evaluate_mozc_zenzai_hybrid_quality as quality
    import evaluate_zenzai_left_context_quality as shared


OUTPUT_SCHEMA = "hazkey.zenzai-boundary-policy-quality-evaluation.v1"
ISOLATED_SYSTEM = "isolated_dictionary_empty_context_v7"
NATIVE_SYSTEM = "native_zenzai_first_clause_empty_context_v7"
FIXED_SYSTEM = "mozc_fixed_empty_context_v7"
SYSTEMS = (ISOLATED_SYSTEM, NATIVE_SYSTEM, FIXED_SYSTEM)
INPUT_SHAPES = ("contains_ascii", "no_ascii")
RUNS = (
    (ISOLATED_SYSTEM, "isolated_dictionary"),
    (NATIVE_SYSTEM, "native_zenzai_first_clause"),
    (FIXED_SYSTEM, "mozc_fixed"),
)


def _load_generation(
    generation_manifest: Path, targets_path: Path
) -> tuple[
    bytes,
    dict[str, Any],
    dict[str, bytes],
    bytes,
    list[dict[str, Any]],
]:
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
    return manifest_bytes, manifest, bound, targets_bytes, targets


def _load_empty_context_sidecar(
    data: bytes,
    path: Path,
    *,
    targets: list[dict[str, Any]],
    reviewed_row_hashes: dict[str, str],
) -> dict[str, Any]:
    sidecar = shared._load_context_sidecar(
        data,
        path,
        targets=targets,
        reviewed_row_hashes=reviewed_row_hashes,
    )
    nonempty = [
        case_id
        for case_id, record in sidecar["records"].items()
        if record["left_context"]
    ]
    if nonempty:
        raise ValueError(
            "empty context sidecar contains nonempty left context for "
            f"{len(nonempty)} case(s), including {nonempty[0]!r}"
        )
    return sidecar


def _expected_context(
    sidecar_case: dict[str, Any], sidecar_identity: dict[str, Any]
) -> dict[str, Any]:
    return {
        "mode": "empty",
        "left_context_sha256": sidecar_case["left_context_sha256"],
        "left_context_code_point_count": 0,
        "left_context_utf8_byte_count": 0,
        "source_content_sha256": sidecar_case["source_content_sha256"],
        "source": sidecar_identity,
    }


def _load_v7_run(data: bytes, path: Path) -> dict[str, Any]:
    if not data:
        raise ValueError(f"{path}: ABProbe v7 result contains no records")
    try:
        return shared._load_v7_run(data, path)
    except StopIteration as error:
        raise ValueError(f"{path}: ABProbe v7 result contains no records") from error


def _load_raw_mozc_run(data: bytes, path: Path) -> dict[str, Any]:
    if not data:
        raise ValueError(f"{path}: raw Mozc ABProbe v6 contains no records")
    try:
        return quality._load_v6_run(data, path, "mozc")
    except StopIteration as error:
        raise ValueError(f"{path}: raw Mozc ABProbe v6 contains no records") from error


def _validate_empty_run(
    *,
    label: str,
    mode: str,
    run: dict[str, Any],
    targets: list[dict[str, Any]],
    manifest: dict[str, Any],
    sidecar: dict[str, Any],
) -> None:
    expected_ids = [target["id"] for target in targets]
    expected_corpus = {
        "sha256": manifest["bindings"]["probe_input"]["sha256"],
        "cases": manifest["bindings"]["probe_input"]["cases"],
    }
    expected_policy = {
        field: shared.V7_BOUNDARY_POLICIES[mode][field]
        for field in shared.V7_BOUNDARY_POLICY_FIELDS
    }
    if list(run["cases"]) != expected_ids:
        raise ValueError(f"{label} result IDs/order do not match targets")
    if run["corpus"] != expected_corpus:
        raise ValueError(f"{label} corpus identity does not match probe input")
    if run["converter_backend"] != "hazkey":
        raise ValueError(f"{label} must use converter_backend='hazkey'")
    if run["quality_policy"]["learning"] is not False:
        raise ValueError(f"{label} learning policy must be false")
    if run["quality_policy"]["context"] != shared.CONTEXT_POLICY:
        raise ValueError(f"{label} context policy is invalid")
    if run["boundary_policy"] != expected_policy:
        raise ValueError(f"{label} must use boundary policy {mode!r}")
    if run["context_source"] != sidecar["identity"]:
        raise ValueError(
            f"{label} context.source does not match the exact empty sidecar"
        )

    for target in targets:
        case_id = target["id"]
        case = run["cases"][case_id]
        sidecar_case = sidecar["records"][case_id]
        expected_span = {
            "start": 0,
            "count": len(target["reading"]),
            "unit": quality.COMPOSITION_ELEMENT_UNIT,
        }
        if case["reading"] != target["reading"]:
            raise ValueError(f"{label} case {case_id!r} reading mismatch")
        if case["category"] != target["category"]:
            raise ValueError(f"{label} case {case_id!r} category mismatch")
        if case["composition_span"] != expected_span:
            raise ValueError(f"{label} case {case_id!r} composition_span mismatch")
        if case["context"] != _expected_context(
            sidecar_case, sidecar["identity"]
        ):
            raise ValueError(
                f"{label} case {case_id!r} context does not match empty sidecar"
            )


def _validate_fixed_provenance(
    *,
    targets: list[dict[str, Any]],
    manifest: dict[str, Any],
    raw_mozc: dict[str, Any],
    fixed_sidecar: dict[str, Any],
    fixed_run: dict[str, Any],
) -> None:
    expected_ids = [target["id"] for target in targets]
    expected_corpus = {
        "sha256": manifest["bindings"]["probe_input"]["sha256"],
        "cases": manifest["bindings"]["probe_input"]["cases"],
    }
    if list(raw_mozc["cases"]) != expected_ids:
        raise ValueError("raw Mozc result IDs/order do not match targets")
    if raw_mozc["corpus"] != expected_corpus:
        raise ValueError("raw Mozc corpus identity does not match probe input")
    if fixed_sidecar["origin"] != {
        "schema": shared.fixed_prepare.INPUT_SCHEMA_V6,
        "sha256": fixed_sidecar["origin"]["sha256"],
        "cases": len(targets),
        "converter_backend": "mozc",
        "conversion_path": quality.CONVERSION_PATH,
    }:
        raise ValueError("fixed-boundary sidecar Mozc origin is invalid")
    if fixed_run["fixed_boundary_source"] != fixed_sidecar["identity"]:
        raise ValueError(
            "Mozc-fixed v7 fixed_boundary.source does not match exact sidecar bytes"
        )

    # These fields make the sidecar-producing Mozc observation and the
    # constrained Hazkey observation one acquisition, while allowing their
    # backend-specific resources and quality policies to differ.
    for field in (
        "backend_version",
        "source_ref",
        "producer",
        "top_k",
        "corpus",
        "warmups",
        "iterations",
    ):
        if raw_mozc[field] != fixed_run[field]:
            raise ValueError(f"raw Mozc/fixed acquisition metadata {field} differs")

    for target in targets:
        case_id = target["id"]
        raw_case = raw_mozc["cases"][case_id]
        fixed_case = fixed_run["cases"][case_id]
        fixed_record = fixed_sidecar["records"][case_id]
        expected_span = {
            "start": 0,
            "count": len(target["reading"]),
            "unit": quality.COMPOSITION_ELEMENT_UNIT,
        }
        if not raw_case["candidates"]:
            raise ValueError(f"raw Mozc case {case_id!r} has no Top-1 boundary")
        if raw_case["reading"] != target["reading"]:
            raise ValueError(f"raw Mozc case {case_id!r} reading mismatch")
        if raw_case["category"] != target["category"]:
            raise ValueError(f"raw Mozc case {case_id!r} category mismatch")
        if raw_case["composition_span"] != expected_span:
            raise ValueError(f"raw Mozc case {case_id!r} composition_span mismatch")
        if raw_case["candidates"][0]["consuming_count"] != fixed_record[
            "consuming_count"
        ]:
            raise ValueError(f"case {case_id!r} sidecar count differs from raw Mozc")
        expected_fixed = {
            "reading_sha256": fixed_record["reading_sha256"],
            "consuming_count": fixed_record["consuming_count"],
            "source": fixed_sidecar["identity"],
        }
        if fixed_case["fixed_boundary"] != expected_fixed:
            raise ValueError(
                f"Mozc-fixed v7 case {case_id!r} fixed_boundary does not match sidecar"
            )


def _median_latency(case: dict[str, Any]) -> float:
    return float(statistics.median(shared._case_samples(case)))


def _build_case(
    target: dict[str, Any],
    isolated: dict[str, Any],
    native: dict[str, Any],
    fixed: dict[str, Any],
) -> dict[str, Any]:
    run_cases = {
        ISOLATED_SYSTEM: isolated,
        NATIVE_SYSTEM: native,
        FIXED_SYSTEM: fixed,
    }
    fixed_count = fixed["fixed_boundary"]["consuming_count"]
    outcomes = {
        ISOLATED_SYSTEM: quality._gold_outcome(isolated["candidates"], target),
        NATIVE_SYSTEM: quality._gold_outcome(native["candidates"], target),
        FIXED_SYSTEM: shared._fixed_gold_outcome(fixed, target),
    }
    diagnostics = {
        ISOLATED_SYSTEM: shared._boundary_diagnostic(
            isolated["candidates"], target
        ),
        NATIVE_SYSTEM: shared._boundary_diagnostic(native["candidates"], target),
        FIXED_SYSTEM: shared._boundary_diagnostic(
            [], target, explicit_count=fixed_count
        ),
    }
    return {
        "id": target["id"],
        "category": target["category"],
        "reading": target["reading"],
        "gold_outcomes": {"systems": outcomes},
        "boundary_diagnostics": {"systems": diagnostics},
        "top1_score": {
            system: shared._score(
                run_case["candidates"][0] if run_case["candidates"] else None
            )
            for system, run_case in run_cases.items()
        },
        "zenzai_execution": {
            system: run_case["zenzai_execution"]
            for system, run_case in run_cases.items()
        },
        "latency_ms": {
            "per_case_statistic": "median-of-recorded-iterations",
            **{
                system: _median_latency(run_case)
                for system, run_case in run_cases.items()
            },
        },
        "memory_kib": {
            system: shared._memory_snapshot(run_case)
            for system, run_case in run_cases.items()
        },
        "mozc_fixed_candidate_count": len(fixed["candidates"]),
    }


def _execution_summary(
    cases: list[dict[str, Any]], systems: Sequence[str]
) -> dict[str, Any]:
    return {
        system: shared._execution_summary(
            [
                {"zenzai_execution": case["zenzai_execution"][system]}
                for case in cases
            ]
        )
        for system in systems
    }


def _latency_summary(
    cases: list[dict[str, Any]], systems: Sequence[str]
) -> dict[str, Any]:
    return {
        "unit": "ms",
        "per_case_statistic": "median-of-recorded-iterations",
        "acquisition_semantics": (
            "sequential separate ABProbe processes; absolute distributions are "
            "diagnostic and are not randomized paired latency effects"
        ),
        "systems": {
            system: shared._distribution(
                [case["latency_ms"][system] for case in cases]
            )
            for system in systems
        },
    }


def _score_summary(
    cases: list[dict[str, Any]], systems: Sequence[str]
) -> dict[str, Any]:
    return {
        "comparison_unit": "observed Top-1 Zenzai score",
        "cross_policy_score_deltas_reported": False,
        "cross_policy_score_delta_reason": (
            "boundary policies may expose different score scopes and token counts"
        ),
        "systems": {
            system: {
                "available_cases": sum(
                    case["top1_score"][system]["available"] for case in cases
                ),
                "scope_counts": dict(
                    sorted(
                        Counter(
                            case["top1_score"][system]["scope"]
                            for case in cases
                            if case["top1_score"][system]["available"]
                        ).items()
                    )
                ),
                "raw": shared._distribution(
                    [
                        case["top1_score"][system]["raw"]
                        for case in cases
                        if case["top1_score"][system]["available"]
                    ]
                ),
                "per_token": shared._distribution(
                    [
                        case["top1_score"][system]["per_token"]
                        for case in cases
                        if case["top1_score"][system]["available"]
                    ]
                ),
            }
            for system in systems
        },
    }


def _summary(
    cases: list[dict[str, Any]], systems: Sequence[str] = SYSTEMS
) -> dict[str, Any]:
    return {
        "cases": len(cases),
        "systems": {
            system: quality._system_metrics(cases, system) for system in systems
        },
        "pairwise_matrix": shared._pairwise_matrix(cases, systems),
        "boundary_diagnostics": shared._boundary_diagnostic_summary(
            cases, systems
        ),
        "zenzai_execution": _execution_summary(cases, systems),
        "latency_ms": _latency_summary(cases, systems),
        "memory_kib": shared._memory_summary(cases, tuple(systems)),
        "top1_score": _score_summary(cases, systems),
    }


def _cases_by_category(
    cases: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for case in cases:
        grouped.setdefault(case["category"], []).append(case)
    return dict(sorted(grouped.items()))


def _input_shape(reading: str) -> str:
    return "contains_ascii" if any(character.isascii() for character in reading) else "no_ascii"


def _cases_by_input_shape(
    cases: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    grouped = {shape: [] for shape in INPUT_SHAPES}
    for case in cases:
        grouped[_input_shape(case["reading"])].append(case)
    if sum(len(values) for values in grouped.values()) != len(cases):
        raise AssertionError("input-shape strata do not cover every case exactly once")
    return grouped


def evaluate(
    generation_manifest: Path,
    targets_path: Path,
    empty_context_sidecar_path: Path,
    isolated_v7_path: Path,
    native_v7_path: Path,
    fixed_v7_path: Path,
    raw_mozc_v6_path: Path,
    fixed_boundary_sidecar_path: Path,
) -> dict[str, Any]:
    (
        manifest_bytes,
        manifest,
        bound,
        targets_bytes,
        targets,
    ) = _load_generation(generation_manifest, targets_path)
    row_hashes = shared._reviewed_row_hashes(bound["reviewed_paths"], targets)

    context_bytes = acceptable._read_regular(
        empty_context_sidecar_path, "empty context sidecar"
    )
    context_sidecar = _load_empty_context_sidecar(
        context_bytes,
        empty_context_sidecar_path,
        targets=targets,
        reviewed_row_hashes=row_hashes,
    )

    v7_paths = {
        ISOLATED_SYSTEM: isolated_v7_path,
        NATIVE_SYSTEM: native_v7_path,
        FIXED_SYSTEM: fixed_v7_path,
    }
    v7_bytes = {
        system: acceptable._read_regular(
            v7_paths[system], f"{mode} empty-context ABProbe v7"
        )
        for system, mode in RUNS
    }
    runs = {
        system: _load_v7_run(v7_bytes[system], v7_paths[system])
        for system, _mode in RUNS
    }
    for system, mode in RUNS:
        _validate_empty_run(
            label=system,
            mode=mode,
            run=runs[system],
            targets=targets,
            manifest=manifest,
            sidecar=context_sidecar,
        )

    run_items = [(system, runs[system]) for system, _mode in RUNS]
    shared._validate_common_hazkey_v7_acquisition(run_items)
    shared._validate_common_context_source(
        run_items, context_sidecar["identity"], "empty"
    )
    if runs[ISOLATED_SYSTEM]["top_k"] > 5:
        raise ValueError(
            "Hazkey v7 common top_k must be <= 5 for native firstClauseResults "
            "boundary-policy comparison"
        )

    raw_mozc_bytes = acceptable._read_regular(
        raw_mozc_v6_path, "raw Mozc ABProbe v6"
    )
    fixed_sidecar_bytes = acceptable._read_regular(
        fixed_boundary_sidecar_path, "Mozc fixed-boundary sidecar"
    )
    raw_mozc = _load_raw_mozc_run(raw_mozc_bytes, raw_mozc_v6_path)
    fixed_sidecar = shared._load_fixed_boundary_sidecar(
        fixed_sidecar_bytes,
        fixed_boundary_sidecar_path,
        raw_mozc=raw_mozc_bytes,
        raw_mozc_path=raw_mozc_v6_path,
        targets=targets,
    )
    _validate_fixed_provenance(
        targets=targets,
        manifest=manifest,
        raw_mozc=raw_mozc,
        fixed_sidecar=fixed_sidecar,
        fixed_run=runs[FIXED_SYSTEM],
    )

    cases = [
        _build_case(
            target,
            runs[ISOLATED_SYSTEM]["cases"][target["id"]],
            runs[NATIVE_SYSTEM]["cases"][target["id"]],
            runs[FIXED_SYSTEM]["cases"][target["id"]],
        )
        for target in targets
    ]
    execution_blockers = shared._execution_failure_blockers(run_items)
    return {
        "schema": OUTPUT_SCHEMA,
        "diagnostic_only": True,
        "formal_authorized": False,
        "evaluation_scope": {
            "comparison": "hazkey-zenzai-three-boundary-policies-empty-context",
            "first_segment_only": True,
            "context_mode": "empty",
            "context_source_shared_exactly": True,
            "raw_left_context_emitted": False,
            "surface_scope": "fully-aligned-first-segment-pairs-only",
            "boundary_and_surface_reported_separately": True,
            "fixed_boundary_scored_from_explicit_attestation": True,
            "gold_category_usage": "stratification-only",
            "runtime_gate_uses_gold_category": False,
            "input_shape_usage": "runtime-observable-stratification",
            "input_shape_partition": list(INPUT_SHAPES),
            "input_shape_partition_mutually_exclusive_and_exhaustive": True,
            "all_hazkey_v7_common_acquisition_fields_verified": True,
            "requested_top_k": runs[ISOLATED_SYSTEM]["top_k"],
            "effective_comparable_top_k": runs[ISOLATED_SYSTEM]["top_k"],
            "native_first_clause_results_top_k_limit": 5,
            "candidate_score_required_for_v7": False,
            "memory_measurement_semantics": (
                "sequential separate ABProbe processes; RSS/PSS after and "
                "within-process deltas are diagnostic, not randomized paired effects"
            ),
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
            "empty_context_sidecar": {
                "path": str(empty_context_sidecar_path),
                **context_sidecar["identity"],
                "all_cases_empty": True,
            },
            "hazkey_v7": {
                system: {
                    "path": str(v7_paths[system]),
                    "sha256": acceptable._sha256(v7_bytes[system]),
                    "boundary_policy": runs[system]["boundary_policy"],
                    "producer": runs[system]["producer"],
                    "resource": runs[system]["resource"],
                    "quality_policy": runs[system]["quality_policy"],
                    "context_source": runs[system]["context_source"],
                    "zenzai_execution": runs[system]["zenzai_execution"],
                }
                for system, _mode in RUNS
            },
            "raw_mozc_v6": {
                "path": str(raw_mozc_v6_path),
                "sha256": acceptable._sha256(raw_mozc_bytes),
                "producer": raw_mozc["producer"],
                "resource": raw_mozc["resource"],
                "quality_policy": raw_mozc["quality_policy"],
            },
            "fixed_boundary_sidecar": {
                "path": str(fixed_boundary_sidecar_path),
                **fixed_sidecar["identity"],
                "origin": fixed_sidecar["origin"],
            },
            "paired_acquisition": {
                "backend": runs[ISOLATED_SYSTEM]["backend"],
                "backend_version": runs[ISOLATED_SYSTEM]["backend_version"],
                "source_ref": runs[ISOLATED_SYSTEM]["source_ref"],
                "top_k": runs[ISOLATED_SYSTEM]["top_k"],
                "warmups": runs[ISOLATED_SYSTEM]["warmups"],
                "iterations": runs[ISOLATED_SYSTEM]["iterations"],
                "common_fields": list(shared.COMMON_HAZKEY_V7_ACQUISITION_FIELDS),
            },
            "fixed_boundary_provenance_chain_verified": {
                "result_to_fixed_sidecar_exact_sha256": True,
                "fixed_sidecar_to_raw_mozc_exact_sha256": True,
                "ids_order_readings_and_counts_rederived": True,
            },
        },
        "all_cases": _summary(cases),
        "by_category": {
            category: _summary(category_cases)
            for category, category_cases in _cases_by_category(cases).items()
        },
        "by_input_shape": {
            shape: _summary(shape_cases)
            for shape, shape_cases in _cases_by_input_shape(cases).items()
        },
        "cases": cases,
        "decision": {
            "status": "inconclusive",
            "formal_authorized": False,
            "reason": (
                "diagnostic empty-context boundary-policy comparison; the "
                "acceptable-path generation is not a locked unseen holdout"
            ),
            "formal_blockers": [
                "acceptable-path generation is diagnostic-only",
                "this acquisition has empty left context and cannot measure contextual Zenzai quality",
                "dynamic llama/GGML runtime dependency identities are not bound",
                "separate-run latency and memory differences are not randomized paired effects",
                "this comparison does not authorize a boundary or Top-1 override rule",
            ]
            + execution_blockers,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate isolated, native-Zenzai, and Mozc-fixed Hazkey boundary "
            "policies under one exact empty-context acquisition."
        )
    )
    parser.add_argument("--generation-manifest", type=Path, required=True)
    parser.add_argument("--targets", type=Path, required=True)
    parser.add_argument("--empty-context-sidecar", type=Path, required=True)
    parser.add_argument("--isolated-v7", type=Path, required=True)
    parser.add_argument("--native-v7", type=Path, required=True)
    parser.add_argument("--fixed-v7", type=Path, required=True)
    parser.add_argument("--raw-mozc-v6", type=Path, required=True)
    parser.add_argument("--fixed-boundary-sidecar", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = evaluate(
            args.generation_manifest,
            args.targets,
            args.empty_context_sidecar,
            args.isolated_v7,
            args.native_v7,
            args.fixed_v7,
            args.raw_mozc_v6,
            args.fixed_boundary_sidecar,
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
