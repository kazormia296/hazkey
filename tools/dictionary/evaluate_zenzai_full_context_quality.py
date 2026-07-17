#!/usr/bin/env python3
"""Evaluate paired AJIMEE full-composition Zenzai runs by left context."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import sys
from typing import Any, Iterable, Sequence

try:
    from . import evaluate_mozc_acceptable_path_boundaries as acceptable
    from . import evaluate_mozc_zenzai_hybrid_quality as quality
    from . import evaluate_zenzai_left_context_quality as shared
    from . import import_ajimee_contextual_blind_silver as importer
    from . import prepare_ajimee_contextual_full_evaluation as generation
    from . import prepare_blind_silver_annotations as blind
except ImportError:  # Direct execution from tools/dictionary.
    import evaluate_mozc_acceptable_path_boundaries as acceptable
    import evaluate_mozc_zenzai_hybrid_quality as quality
    import evaluate_zenzai_left_context_quality as shared
    import import_ajimee_contextual_blind_silver as importer
    import prepare_ajimee_contextual_full_evaluation as generation
    import prepare_blind_silver_annotations as blind


OUTPUT_SCHEMA = "hazkey.zenzai-full-context-quality-evaluation.v1"
CONVERSION_PATH = "full_composition_candidates"
BOUNDARY_POLICY = {
    "mode": "full_composition",
    "boundary_zenzai_enabled": False,
    "surface_zenzai_enabled": True,
    "source": "entire_composition",
}
EMPTY_SYSTEM = "full_composition_empty_context_v7"
NATURAL_SYSTEM = "full_composition_natural_context_v7"
SYSTEMS = (EMPTY_SYSTEM, NATURAL_SYSTEM)
LENGTH_BUCKETS = (
    ("0", 0, 0),
    ("1-8", 1, 8),
    ("9-16", 9, 16),
    ("17-32", 17, 32),
    ("33-64", 33, 64),
    ("65-128", 65, 128),
    ("129+", 129, None),
)


def _length_bucket(length: int) -> str:
    for name, minimum, maximum in LENGTH_BUCKETS:
        if length >= minimum and (maximum is None or length <= maximum):
            return name
    raise AssertionError(f"unbucketed length {length}")


def _validate_full_v7_record(value: Any, where: str) -> dict[str, Any]:
    root = acceptable._exact_object(value, shared.V7_ROOT_FIELDS, where)
    if root["schema"] != shared.INPUT_SCHEMA_V7:
        raise ValueError(f"{where}.schema must be {shared.INPUT_SCHEMA_V7!r}")
    if root["conversion_path"] != CONVERSION_PATH:
        raise ValueError(f"{where}.conversion_path must be {CONVERSION_PATH!r}")
    policy = acceptable._exact_object(
        root["quality_policy"], quality.QUALITY_POLICY_FIELDS, f"{where}.quality_policy"
    )
    if policy["context"] != shared.CONTEXT_POLICY:
        raise ValueError(
            f"{where}.quality_policy.context must be {shared.CONTEXT_POLICY!r}"
        )
    boundary_policy = acceptable._exact_object(
        root["boundary_policy"],
        shared.V7_BOUNDARY_POLICY_FIELDS,
        f"{where}.boundary_policy",
    )
    if boundary_policy != BOUNDARY_POLICY:
        raise ValueError(f"{where}.boundary_policy must describe full composition")
    if (
        boundary_policy["boundary_zenzai_enabled"] is not False
        or boundary_policy["surface_zenzai_enabled"] is not True
    ):
        raise ValueError(
            f"{where}.boundary_policy boolean fields must be JSON booleans"
        )
    if root["fixed_boundary"] is not None:
        raise ValueError(f"{where}.fixed_boundary must be null for full composition")

    downgraded = dict(root)
    for field in ("context", "boundary_policy", "fixed_boundary", "zenzai_execution"):
        downgraded.pop(field)
    downgraded["schema"] = quality.INPUT_SCHEMA
    downgraded["conversion_path"] = quality.CONVERSION_PATH
    downgraded_policy = dict(policy)
    downgraded_policy["context"] = "empty"
    downgraded["quality_policy"] = downgraded_policy
    normalized = quality._validate_v6_record(downgraded, where)
    if normalized["converter_backend"] != "hazkey":
        raise ValueError(f"{where}: full-composition v7 requires Hazkey")
    if not normalized["quality_policy"]["zenzai"]["enabled"]:
        raise ValueError(f"{where}: full-composition v7 must enable Zenzai")
    if normalized["measurement"]["iterations"] != 1:
        raise ValueError(
            f"{where}.measurement.iterations must be 1 when Zenzai is enabled"
        )
    span = normalized["composition_span"]
    if (
        span["start"] != 0
        or span["count"] != len(normalized["reading"])
        or span["unit"] != quality.COMPOSITION_ELEMENT_UNIT
    ):
        raise ValueError(f"{where}.composition_span must cover the complete reading")
    if any(
        candidate["consuming_count"] != span["count"]
        for candidate in normalized["candidates"]
    ):
        raise ValueError(f"{where}.candidates must consume the complete composition")

    context = shared._validate_v7_context(root["context"], f"{where}.context")
    execution = shared._validate_zenzai_execution(
        root["zenzai_execution"], f"{where}.zenzai_execution"
    )
    if execution["request_count"] != 1:
        raise ValueError(
            f"{where}.zenzai_execution.request_count must be 1 for full composition"
        )
    inference_limit = normalized["quality_policy"]["zenzai"]["inference_limit"]
    if execution["evaluation_attempt_count"] > inference_limit:
        raise ValueError(
            f"{where}.zenzai_execution.evaluation_attempt_count exceeds inference_limit"
        )
    normalized.update(
        {
            "schema": shared.INPUT_SCHEMA_V7,
            "conversion_path": CONVERSION_PATH,
            "quality_policy": {**normalized["quality_policy"], "context": shared.CONTEXT_POLICY},
            "context": context,
            "boundary_policy": dict(BOUNDARY_POLICY),
            "fixed_boundary": None,
            "zenzai_execution": execution,
        }
    )
    return normalized


def _load_v7_run(data: bytes, path: Path) -> dict[str, Any]:
    records = acceptable._jsonl(data, str(path))
    cases: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(records, 1):
        case = _validate_full_v7_record(raw, f"{path}:{index}")
        if case["id"] in cases:
            raise ValueError(f"{path}: duplicate id {case['id']!r}")
        cases[case["id"]] = case
    if not cases:
        raise ValueError(f"{path}: full-composition v7 result contains no records")
    first = next(iter(cases.values()))
    consistency_fields = (
        "backend",
        "backend_version",
        "converter_backend",
        "source_ref",
        "resource",
        "producer",
        "quality_policy",
        "top_k",
        "corpus",
        "conversion_path",
        "boundary_policy",
    )
    for case in cases.values():
        for field in consistency_fields:
            if case[field] != first[field]:
                raise ValueError(f"{path}: inconsistent {field} within run")
        if case["measurement"]["warmups"] != first["measurement"]["warmups"]:
            raise ValueError(f"{path}: inconsistent warmups within run")
        if case["measurement"]["iterations"] != first["measurement"]["iterations"]:
            raise ValueError(f"{path}: inconsistent iterations within run")
        if case["context"]["source"] != first["context"]["source"]:
            raise ValueError(f"{path}: inconsistent context.source within run")
    if first["corpus"]["cases"] != len(cases):
        raise ValueError(f"{path}: corpus.cases does not match result count")
    if first["context"]["source"]["cases"] != len(cases):
        raise ValueError(f"{path}: context.source.cases does not match result count")
    if sum(
        case["zenzai_execution"]["evaluation_attempt_count"]
        for case in cases.values()
    ) == 0:
        raise ValueError(f"{path}: enabled Zenzai run has no model evaluation attempt")
    return {
        "path": path,
        "schema": shared.INPUT_SCHEMA_V7,
        "backend": first["backend"],
        "backend_version": first["backend_version"],
        "converter_backend": first["converter_backend"],
        "source_ref": first["source_ref"],
        "resource": first["resource"],
        "producer": first["producer"],
        "quality_policy": first["quality_policy"],
        "conversion_path": first["conversion_path"],
        "boundary_policy": first["boundary_policy"],
        "top_k": first["top_k"],
        "corpus": first["corpus"],
        "warmups": first["measurement"]["warmups"],
        "iterations": first["measurement"]["iterations"],
        "context_source": first["context"]["source"],
        "zenzai_execution": shared._execution_summary(cases.values()),
        "cases": cases,
    }


def _load_generation(
    raw_snapshot_path: Path, generation_manifest_path: Path
) -> tuple[bytes, dict[str, bytes], dict[str, Any], list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    raw = importer._read_regular(raw_snapshot_path)
    actual = generation.capture_generation(generation_manifest_path)
    generation.rederive_generation(raw, actual)
    manifest = generation._decode_json(
        actual[generation.MANIFEST_NAME], "full-evaluation manifest"
    )
    cases = blind.load_cases_bytes(
        actual[generation.IMPORT_CASES_NAME], "bound AJIMEE import cases"
    )
    targets = generation._decode_jsonl(
        actual[generation.TARGETS_NAME], "full-conversion targets"
    )
    if len(targets) != len(cases):
        raise ValueError("full-conversion targets do not cover import cases")
    normalized_targets: list[dict[str, Any]] = []
    for index, (target, case) in enumerate(zip(targets, cases, strict=True), 1):
        where = f"full-conversion targets:{index}"
        target = acceptable._exact_object(
            target,
            {
                "schema",
                "id",
                "category",
                "reading",
                "surface_references",
                "source_content_sha256",
            },
            where,
        )
        if target["schema"] != generation.TARGET_SCHEMA:
            raise ValueError(f"{where}.schema mismatch")
        references = target["surface_references"]
        if (
            not isinstance(references, list)
            or not references
            or any(not isinstance(value, str) or not value for value in references)
            or len(references) != len(set(references))
        ):
            raise ValueError(f"{where}.surface_references must be unique nonempty strings")
        if target != generation._target_record(
            case, target["source_content_sha256"]
        ):
            raise ValueError(f"{where} does not match its imported case")
        normalized_targets.append(dict(target))
    reviewed_hashes = {
        target["id"]: target["source_content_sha256"]
        for target in normalized_targets
    }
    contextual = shared._load_context_sidecar(
        actual[generation.CONTEXT_NAME],
        generation_manifest_path.parent / generation.CONTEXT_NAME,
        targets=normalized_targets,
        reviewed_row_hashes=reviewed_hashes,
    )
    empty = shared._load_context_sidecar(
        actual[generation.EMPTY_CONTEXT_NAME],
        generation_manifest_path.parent / generation.EMPTY_CONTEXT_NAME,
        targets=normalized_targets,
        reviewed_row_hashes=reviewed_hashes,
    )
    if any(not record["left_context"] for record in contextual["records"].values()):
        raise ValueError("AJIMEE contextual sidecar must be nonempty for every case")
    if any(record["left_context"] for record in empty["records"].values()):
        raise ValueError("AJIMEE empty-context sidecar must be empty for every case")
    return raw, actual, manifest, normalized_targets, empty, contextual


def _expected_context(
    record: dict[str, Any], identity: dict[str, Any]
) -> dict[str, Any]:
    text = record["left_context"]
    return {
        "mode": "empty" if not text else "natural_left",
        "left_context_sha256": record["left_context_sha256"],
        "left_context_code_point_count": len(text),
        "left_context_utf8_byte_count": len(text.encode("utf-8")),
        "source_content_sha256": record["source_content_sha256"],
        "source": identity,
    }


def _validate_pair(
    targets: list[dict[str, Any]],
    probe_data: bytes,
    empty_sidecar: dict[str, Any],
    natural_sidecar: dict[str, Any],
    empty_run: dict[str, Any],
    natural_run: dict[str, Any],
) -> None:
    expected_ids = [target["id"] for target in targets]
    expected_corpus = {"sha256": acceptable._sha256(probe_data), "cases": len(targets)}
    for label, run, sidecar in (
        ("empty full-composition v7", empty_run, empty_sidecar),
        ("natural full-composition v7", natural_run, natural_sidecar),
    ):
        if list(run["cases"]) != expected_ids:
            raise ValueError(f"{label} result IDs/order do not match targets")
        if run["corpus"] != expected_corpus:
            raise ValueError(f"{label} corpus identity does not match probe input")
        if run["context_source"] != sidecar["identity"]:
            raise ValueError(f"{label} context.source does not match its sidecar")
        if run["quality_policy"]["learning"] is not False:
            raise ValueError(f"{label} learning policy must be false")
    for field in shared.COMMON_HAZKEY_V7_ACQUISITION_FIELDS + (
        "conversion_path",
        "boundary_policy",
    ):
        if empty_run[field] != natural_run[field]:
            raise ValueError(f"paired full-composition acquisition {field} differs")

    for target in targets:
        case_id = target["id"]
        empty_case = empty_run["cases"][case_id]
        natural_case = natural_run["cases"][case_id]
        for label, case, sidecar in (
            ("empty full-composition v7", empty_case, empty_sidecar),
            ("natural full-composition v7", natural_case, natural_sidecar),
        ):
            if case["reading"] != target["reading"]:
                raise ValueError(f"{label} case {case_id!r} reading mismatch")
            if case["category"] != target["category"]:
                raise ValueError(f"{label} case {case_id!r} category mismatch")
            expected = _expected_context(
                sidecar["records"][case_id], sidecar["identity"]
            )
            if case["context"] != expected:
                raise ValueError(f"{label} case {case_id!r} context mismatch")
        if empty_case["context"]["mode"] != "empty":
            raise ValueError(f"empty full-composition case {case_id!r} is not empty")
        if natural_case["context"]["mode"] != "natural_left":
            raise ValueError(f"natural full-composition case {case_id!r} is not natural_left")


def _outcome(candidates: list[dict[str, Any]], references: list[str]) -> dict[str, Any]:
    matches = [candidate["text"] in references for candidate in candidates]
    rank = next((index for index, hit in enumerate(matches, 1) if hit), None)
    return {
        "accuracy_at1": bool(matches[:1] and matches[0]),
        "accuracy_at_k": any(matches),
        "first_hit_rank": rank,
        "reciprocal_rank": 0.0 if rank is None else 1 / rank,
    }


def _build_case(
    target: dict[str, Any], empty: dict[str, Any], natural: dict[str, Any]
) -> dict[str, Any]:
    empty_latency = float(statistics.median(shared._case_samples(empty)))
    natural_latency = float(statistics.median(shared._case_samples(natural)))
    return {
        "id": target["id"],
        "category": target["category"],
        "reading": target["reading"],
        "input_code_point_count": len(target["reading"]),
        "context": dict(natural["context"]),
        "surface_reference_count": len(target["surface_references"]),
        "outcomes": {
            EMPTY_SYSTEM: _outcome(empty["candidates"], target["surface_references"]),
            NATURAL_SYSTEM: _outcome(natural["candidates"], target["surface_references"]),
        },
        "zenzai_execution": {
            EMPTY_SYSTEM: empty["zenzai_execution"],
            NATURAL_SYSTEM: natural["zenzai_execution"],
        },
        "latency_ms": {
            EMPTY_SYSTEM: empty_latency,
            NATURAL_SYSTEM: natural_latency,
            "natural_minus_empty": natural_latency - empty_latency,
            "natural_over_empty_ratio": (
                natural_latency / empty_latency if empty_latency > 0 else None
            ),
        },
        "memory_kib": {
            EMPTY_SYSTEM: shared._memory_snapshot(empty),
            NATURAL_SYSTEM: shared._memory_snapshot(natural),
        },
    }


def _accuracy(values: Iterable[bool]) -> dict[str, Any]:
    observed = list(values)
    hits = sum(observed)
    return {
        "hits": hits,
        "cases": len(observed),
        "accuracy": hits / len(observed) if observed else None,
    }


def _system_metrics(
    cases: list[dict[str, Any]], system: str, top_k: int
) -> dict[str, Any]:
    outcomes = [case["outcomes"][system] for case in cases]
    return {
        "accuracy_at1": _accuracy(outcome["accuracy_at1"] for outcome in outcomes),
        "accuracy_at_k": _accuracy(outcome["accuracy_at_k"] for outcome in outcomes),
        "mrr_at_k": {
            "k": top_k,
            "cases": len(outcomes),
            "value": (
                sum(outcome["reciprocal_rank"] for outcome in outcomes) / len(outcomes)
                if outcomes
                else None
            ),
        },
    }


def _pairwise(cases: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        rank: shared._transition(
            (
                bool(case["outcomes"][EMPTY_SYSTEM][field]),
                bool(case["outcomes"][NATURAL_SYSTEM][field]),
            )
            for case in cases
        )
        for rank, field in (
            ("accuracy_at1", "accuracy_at1"),
            ("accuracy_at_k", "accuracy_at_k"),
        )
    }


def _execution_summary(cases: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        system: shared._execution_summary(
            [{"zenzai_execution": case["zenzai_execution"][system]} for case in cases]
        )
        for system in SYSTEMS
    }


def _latency_summary(cases: list[dict[str, Any]]) -> dict[str, Any]:
    ratios = [
        case["latency_ms"]["natural_over_empty_ratio"]
        for case in cases
        if case["latency_ms"]["natural_over_empty_ratio"] is not None
    ]
    return {
        "unit": "ms",
        "per_case_statistic": "median-of-recorded-iterations",
        EMPTY_SYSTEM: shared._distribution(
            [case["latency_ms"][EMPTY_SYSTEM] for case in cases]
        ),
        NATURAL_SYSTEM: shared._distribution(
            [case["latency_ms"][NATURAL_SYSTEM] for case in cases]
        ),
        "natural_minus_empty": shared._distribution(
            [case["latency_ms"]["natural_minus_empty"] for case in cases]
        ),
        "natural_over_empty_ratio": shared._distribution(ratios),
    }


def _summary(cases: list[dict[str, Any]], top_k: int) -> dict[str, Any]:
    return {
        "cases": len(cases),
        "systems": {
            system: _system_metrics(cases, system, top_k) for system in SYSTEMS
        },
        "paired_natural_vs_empty": _pairwise(cases),
        "zenzai_execution": _execution_summary(cases),
        "latency_ms": _latency_summary(cases),
        "memory_kib": shared._memory_summary(cases, SYSTEMS),
    }


def evaluate(
    raw_snapshot_path: Path,
    generation_manifest_path: Path,
    empty_v7_path: Path,
    natural_v7_path: Path,
) -> dict[str, Any]:
    raw, generated, manifest, targets, empty_sidecar, natural_sidecar = _load_generation(
        raw_snapshot_path, generation_manifest_path
    )
    empty_bytes = acceptable._read_regular(empty_v7_path, "empty full-composition v7")
    natural_bytes = acceptable._read_regular(
        natural_v7_path, "natural full-composition v7"
    )
    empty_run = _load_v7_run(empty_bytes, empty_v7_path)
    natural_run = _load_v7_run(natural_bytes, natural_v7_path)
    _validate_pair(
        targets,
        generated[generation.PROBE_INPUT_NAME],
        empty_sidecar,
        natural_sidecar,
        empty_run,
        natural_run,
    )
    cases = [
        _build_case(
            target,
            empty_run["cases"][target["id"]],
            natural_run["cases"][target["id"]],
        )
        for target in targets
    ]
    by_context_length = {
        name: [
            case
            for case in cases
            if _length_bucket(case["context"]["left_context_code_point_count"]) == name
        ]
        for name, _minimum, _maximum in LENGTH_BUCKETS
    }
    by_input_length = {
        name: [
            case
            for case in cases
            if _length_bucket(case["input_code_point_count"]) == name
        ]
        for name, _minimum, _maximum in LENGTH_BUCKETS
    }
    blockers = shared._execution_failure_blockers(
        (("empty_v7", empty_run), ("natural_v7", natural_run))
    )
    top_k = empty_run["top_k"]
    return {
        "schema": OUTPUT_SCHEMA,
        "diagnostic_only": True,
        "formal_authorized": False,
        "evaluation_scope": {
            "comparison": "ajimee-full-composition-empty-vs-natural-left-context",
            "conversion_path": CONVERSION_PATH,
            "exact_multiple_surface_references": True,
            "rank_metric": (
                "MRR@K over the acquired Top-K candidate list; a reference not "
                "found within K contributes zero"
            ),
            "boundary_metric_reported": False,
            "candidate_full_consuming_count_required": True,
            "raw_left_context_emitted": False,
            "gold_category_usage": "none",
            "raw_snapshot_and_generation_exactly_rederived": True,
            "memory_measurement_semantics": (
                "sequential separate ABProbe processes; RSS/PSS after and within-process "
                "deltas are diagnostic, not randomized paired effects"
            ),
        },
        "inputs": {
            "raw_snapshot": {
                "supplied_path": str(raw_snapshot_path),
                "sha256": acceptable._sha256(raw),
                **manifest["bindings"]["raw_snapshot"],
            },
            "generation_manifest": {
                "path": str(generation_manifest_path),
                "sha256": acceptable._sha256(generated[generation.MANIFEST_NAME]),
                "schema": generation.MANIFEST_SCHEMA,
            },
            "probe_input": manifest["bindings"]["probe_input"],
            "targets": manifest["bindings"]["targets"],
            "empty_context": {
                **manifest["bindings"]["empty_context"],
                "identity": empty_sidecar["identity"],
            },
            "natural_context": {
                **manifest["bindings"]["context"],
                "identity": natural_sidecar["identity"],
            },
            "empty_v7": {
                "path": str(empty_v7_path),
                "sha256": acceptable._sha256(empty_bytes),
                "zenzai_execution": empty_run["zenzai_execution"],
            },
            "natural_v7": {
                "path": str(natural_v7_path),
                "sha256": acceptable._sha256(natural_bytes),
                "zenzai_execution": natural_run["zenzai_execution"],
            },
            "paired_acquisition": {
                "producer": empty_run["producer"],
                "resource": empty_run["resource"],
                "quality_policy": empty_run["quality_policy"],
                "source_ref": empty_run["source_ref"],
                "top_k": empty_run["top_k"],
                "corpus": empty_run["corpus"],
                "warmups": empty_run["warmups"],
                "iterations": empty_run["iterations"],
            },
        },
        "all_cases": _summary(cases, top_k),
        "by_context_code_point_length": {
            name: _summary(values, top_k)
            for name, values in by_context_length.items()
        },
        "by_input_code_point_length": {
            name: _summary(values, top_k)
            for name, values in by_input_length.items()
        },
        "cases": cases,
        "decision": {
            "status": "inconclusive",
            "formal_authorized": False,
            "reason": (
                "AJIMEE contextual Silver diagnostic comparison; this exploration "
                "set cannot authorize a production context policy"
            ),
            "formal_blockers": [
                "AJIMEE labels are imported Silver references, not a locked human Gold holdout",
                "the selected cases and context-conditioned task are known exploration data",
                "dynamic llama/GGML runtime dependency identities are not bound",
                "separate-run latency and memory differences are not randomized paired effects",
                "this comparison does not authorize a Top-1 override rule",
            ]
            + blockers,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate paired AJIMEE full-composition ABProbe v7 runs with empty "
            "and natural left context."
        )
    )
    parser.add_argument("--raw-snapshot", type=Path, required=True)
    parser.add_argument("--generation-manifest", type=Path, required=True)
    parser.add_argument("--empty-v7", type=Path, required=True)
    parser.add_argument("--natural-v7", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = evaluate(
            args.raw_snapshot,
            args.generation_manifest,
            args.empty_v7,
            args.natural_v7,
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
