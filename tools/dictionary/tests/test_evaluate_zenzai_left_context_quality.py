from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from tools.dictionary import compile_mozc_acceptable_path_evaluation as compiler
from tools.dictionary import evaluate_zenzai_left_context_quality as evaluator
from tools.dictionary import prepare_blind_silver_annotations as blind
from tools.dictionary import prepare_mozc_fixed_boundary_sidecar as fixed_prepare


def json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def jsonl_bytes(values: list[dict[str, object]]) -> bytes:
    return b"".join(json_bytes(value) for value in values)


def sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def span(count: int) -> dict[str, object]:
    return {"start": 0, "count": count, "unit": "composition_element"}


CASES = [
    {
        "id": "surface-rescue",
        "category": "homophone-context",
        "reading": "あいう",
        "surface": "正末",
        "context": "前文脈。",
    },
    {
        "id": "surface-regression",
        "category": "long-structural",
        "reading": "かきく",
        "surface": "善末",
        "context": "これは十二文字の左文脈です。",
    },
    {
        "id": "second-rescue",
        "category": "proper-noun",
        "reading": "さしす",
        "surface": "境末",
        "context": "長い左文脈を用意して三十三文字以上の長さ区分を確実に検査するための文章です。追加分です。",
    },
    {
        "id": "empty-control",
        "category": "sample",
        "reading": "たちつ",
        "surface": "保末",
        "context": "",
    },
]


ISOLATED_EMPTY = {
    "surface-rescue": [("誤", 2), ("正", 2)],
    "surface-regression": [("善", 2)],
    "second-rescue": [("誤", 2), ("境", 2)],
    "empty-control": [("保", 2)],
}
ISOLATED_LEFT = {
    "surface-rescue": [("正", 2)],
    "surface-regression": [("誤", 2), ("善", 2)],
    "second-rescue": [("境", 2)],
    "empty-control": [("保", 2)],
}
NATIVE_EMPTY = {
    "surface-rescue": [("誤", 3)],
    "surface-regression": [("善", 2)],
    "second-rescue": [("誤", 3)],
    "empty-control": [("保", 2)],
}
NATIVE_LEFT = {
    "surface-rescue": [("正", 2)],
    "surface-regression": [("誤", 3)],
    "second-rescue": [("誤", 3)],
    "empty-control": [("保", 2)],
}
MOZC_RAW = {case["id"]: [("Mozc", 2)] for case in CASES}
MOZC_RAW["surface-rescue"] = [("Mozc", 2), ("別境界", 3)]
FIXED_EMPTY = {
    "surface-rescue": [("誤", 2)],
    "surface-regression": [("善", 2)],
    "second-rescue": [],
    "empty-control": [("保", 2)],
}
FIXED_LEFT = {
    "surface-rescue": [("正", 2)],
    "surface-regression": [("誤", 2)],
    "second-rescue": [],
    "empty-control": [("保", 2)],
}


def compiler_source_record(case: dict[str, str]) -> dict[str, object]:
    case_id = case["id"]
    reading = case["reading"]
    return {
        "schema": compiler.ANNOTATION_EXPORT_SCHEMA,
        "id": case_id,
        "category": case["category"],
        "source": {
            "queue_sha256": "sha256:" + "1" * 64,
            "corpus_sha256": "sha256:" + "2" * 64,
            "row_sha256": sha256(case_id.encode()),
            "reading": reading,
            "annotation_reading": reading,
            "reading_unit": compiler.SOURCE_READING_UNIT,
            "annotation_reading_unit": compiler.ANNOTATION_READING_UNIT,
            "surface_unit": compiler.SURFACE_UNIT,
            "surface_references": [
                {"id": "surface-0", "text": case["surface"]}
            ],
        },
        "path_set_status": "closed",
        "needs_adjudication": False,
        "path_units": {
            "reading_boundaries": compiler.ANNOTATION_READING_UNIT,
            "surface_boundaries": compiler.SURFACE_UNIT,
        },
        "acceptable_paths": [
            {
                "path_id": "path-1",
                "status": "acceptable",
                "surface_reference_id": "surface-0",
                "reading_boundaries": [2],
                "surface_boundaries": [1],
                "alignment_status": "aligned",
                "provenance": {"kind": "human"},
            }
        ],
        "draft_paths": [],
        "review": {
            "revision": 1,
            "corrected_reading": None,
            "annotator_id": "reviewer",
            "reviewed_once": True,
            "updated_at": "2026-07-17T00:00:00Z",
            "notes": None,
            "imported": {},
        },
    }


def quality_policy(context: str) -> dict[str, object]:
    return {
        "learning": False,
        "context": context,
        "zenzai": {
            "enabled": True,
            "model_path": "/fixture/zenzai.gguf",
            "model_size_bytes": 2048,
            "model_sha256": "sha256:" + "d" * 64,
            "inference_limit": 10,
            "resolved_device": "Vulkan0",
        },
    }


def mozc_quality_policy() -> dict[str, object]:
    return {
        "learning": False,
        "context": "empty",
        "zenzai": {
            "enabled": False,
            "model_path": None,
            "model_size_bytes": None,
            "model_sha256": None,
            "inference_limit": None,
            "resolved_device": None,
        },
    }


def measurement(latency: float) -> dict[str, object]:
    return {
        "warmups": 0,
        "iterations": 1,
        "latency_ms": {
            "median": latency,
            "p95": latency,
            "minimum": latency,
            "maximum": latency,
            "samples": [latency],
        },
        "rss": {
            "before_kib": 100,
            "after_kib": 110,
            "before_pss_kib": 80,
            "after_pss_kib": 85,
        },
        "backend_diagnostics": {},
    }


def candidate_records(
    values: list[tuple[str, int]], score: float | None
) -> list[dict[str, object]]:
    return [
        {
            "text": text,
            "rank": rank,
            "consuming_count": count,
            "provenance": "standard",
            "ranking_influence": "zenzai",
            "zenzai_score": score - rank + 1 if score is not None else None,
            "zenzai_score_token_count": 2 if score is not None else None,
            "zenzai_score_scope": "full_candidate" if score is not None else None,
        }
        for rank, (text, count) in enumerate(values, 1)
    ]


def zenzai_execution(native: bool) -> dict[str, object]:
    request_count = 1 if native else 2
    return {
        "request_count": request_count,
        "evaluation_attempt_count": request_count,
        "attempt_outcomes": {
            "pass": request_count,
            "fix_required": 0,
            "whole_result": 0,
            "error": 0,
        },
        "terminal_outcomes": {
            "pass": request_count,
            "fix_required": 0,
            "whole_result": 0,
            "error": 0,
            "inference_limit": 0,
            "no_candidate": 0,
        },
    }


def context_records(contexts: dict[str, str]) -> list[dict[str, object]]:
    return [
        {
            "schema": blind.CONTEXT_SCHEMA,
            "id": case["id"],
            "source_content_sha256": sha256(case["id"].encode()),
            "left_context": contexts[case["id"]],
            "left_context_sha256": sha256(contexts[case["id"]].encode()),
        }
        for case in CASES
    ]


def v7_context(
    sidecar_record: dict[str, object], source: dict[str, object]
) -> dict[str, object]:
    left_context = str(sidecar_record["left_context"])
    return {
        "mode": "empty" if not left_context else "natural_left",
        "left_context_sha256": sidecar_record["left_context_sha256"],
        "left_context_code_point_count": len(left_context),
        "left_context_utf8_byte_count": len(left_context.encode()),
        "source_content_sha256": sidecar_record["source_content_sha256"],
        "source": source,
    }


def boundary_policy(native: bool, fixed: bool = False) -> dict[str, object]:
    if fixed:
        return {
            "mode": "mozc_fixed",
            "boundary_zenzai_enabled": False,
            "surface_zenzai_enabled": True,
            "source": "mozc_top1_fixed_boundary_sidecar",
        }
    if native:
        return {
            "mode": "native_zenzai_first_clause",
            "boundary_zenzai_enabled": True,
            "surface_zenzai_enabled": True,
            "source": "primary_converter_first_clause_results",
        }
    return {
        "mode": "isolated_dictionary",
        "boundary_zenzai_enabled": False,
        "surface_zenzai_enabled": True,
        "source": "separate_converter",
    }


def abprobe_result(
    target: dict[str, object],
    candidates: list[tuple[str, int]],
    probe_hash: str,
    total: int,
    *,
    schema: str,
    latency: float,
    score: float | None,
    sidecar_record: dict[str, object] | None = None,
    sidecar_source: dict[str, object] | None = None,
    native: bool = False,
    fixed_boundary_record: dict[str, object] | None = None,
    fixed_boundary_source: dict[str, object] | None = None,
) -> dict[str, object]:
    fixed = fixed_boundary_record is not None
    record: dict[str, object] = {
        "schema": schema,
        "conversion_path": (
            "native_segment_candidates"
            if native
            else "mozc_fixed_segment_candidates"
            if fixed
            else "segment_candidates"
        ),
        "id": target["id"],
        "reading": target["reading"],
        "category": target["category"],
        "backend": "Hazkey+Zenzai",
        "backend_version": "context-quality-test-v1",
        "converter_backend": "hazkey",
        "source_ref": "a" * 40,
        "resource": {
            "kind": "hazkey_dictionary",
            "path": "/fixture/hazkey",
            "fingerprint": "sha256:" + "b" * 64,
        },
        "producer": {
            "path": "/fixture/ab-probe",
            "size_bytes": 1234,
            "sha256": "sha256:" + "e" * 64,
        },
        "quality_policy": quality_policy(
            "empty" if schema == evaluator.quality.INPUT_SCHEMA else evaluator.CONTEXT_POLICY
        ),
        "top_k": 4,
        "corpus": {"sha256": probe_hash, "cases": total},
        "candidates": candidate_records(candidates, score),
        "composition_span": span(len(str(target["reading"]))),
        "measurement": measurement(latency),
    }
    if schema == evaluator.INPUT_SCHEMA_V7:
        assert sidecar_record is not None
        assert sidecar_source is not None
        record["boundary_policy"] = boundary_policy(native, fixed)
        record["context"] = v7_context(sidecar_record, sidecar_source)
        if fixed:
            assert fixed_boundary_source is not None
            record["fixed_boundary"] = {
                "reading_sha256": fixed_boundary_record["reading_sha256"],
                "consuming_count": fixed_boundary_record["consuming_count"],
                "source": fixed_boundary_source,
            }
        else:
            record["fixed_boundary"] = None
        record["zenzai_execution"] = zenzai_execution(native or fixed)
    return record


def mozc_result(
    target: dict[str, object],
    candidates: list[tuple[str, int]],
    probe_hash: str,
    total: int,
) -> dict[str, object]:
    record = abprobe_result(
        target,
        candidates,
        probe_hash,
        total,
        schema=evaluator.quality.INPUT_SCHEMA,
        latency=1.0,
        score=None,
    )
    record["backend"] = "Mozc"
    record["converter_backend"] = "mozc"
    record["resource"] = {
        "kind": "mozc_runtime_inputs",
        "path": "/fixture/mozc",
        "fingerprint": "sha256:" + "c" * 64,
    }
    record["quality_policy"] = mozc_quality_policy()
    for candidate in record["candidates"]:
        candidate["ranking_influence"] = "standard"
    return record


class Fixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.generation = root / "generation"
        self.targets_path = self.generation / compiler.TARGETS_NAME
        self.manifest_path = self.generation / compiler.MANIFEST_NAME
        self.probe_path = self.generation / compiler.PROBE_INPUT_NAME
        self.isolated_empty_context_path = root / "isolated-empty-context.jsonl"
        self.isolated_left_context_path = root / "isolated-left-context.jsonl"
        self.isolated_empty_path = root / "isolated-empty-v7.jsonl"
        self.isolated_left_path = root / "isolated-left-v7.jsonl"
        self.native_empty_context_path = root / "native-empty-context.jsonl"
        self.native_left_context_path = root / "native-left-context.jsonl"
        self.native_empty_path = root / "native-empty-v7.jsonl"
        self.native_left_path = root / "native-left-v7.jsonl"
        self.fixed_raw_mozc_path = root / "fixed-raw-mozc-v6.jsonl"
        self.fixed_boundary_path = root / "fixed-boundary.jsonl"
        self.fixed_empty_context_path = root / "fixed-empty-context.jsonl"
        self.fixed_left_context_path = root / "fixed-left-context.jsonl"
        self.fixed_empty_path = root / "fixed-empty-v7.jsonl"
        self.fixed_left_path = root / "fixed-left-v7.jsonl"
        self.write_generation()
        self.write_all_runs()

    def write_generation(self) -> None:
        reviewed = jsonl_bytes([compiler_source_record(case) for case in CASES])
        annotation = {
            "schema": compiler.ANNOTATION_MANIFEST_SCHEMA,
            "queue_sha256": "sha256:" + "1" * 64,
            "workbook_sha256": "sha256:" + "4" * 64,
            "reviewed_paths_sha256": sha256(reviewed),
            "cases": len(CASES),
            "path_set_statuses": {"closed": len(CASES)},
            "complete": True,
            "formal_authorized": False,
            "diagnostic_only": True,
        }
        generated = compiler.prepare_outputs_bytes(
            reviewed_paths_data=reviewed,
            annotation_manifest_data=json_bytes(annotation),
        )
        self.generation.mkdir()
        for name, data in generated.items():
            (self.generation / name).write_bytes(data)
        self.targets = [
            json.loads(line) for line in self.targets_path.read_text().splitlines()
        ]

    def write_pair(
        self,
        *,
        contexts: dict[str, str],
        sidecar_path: Path,
        run_path: Path,
        candidates: dict[str, list[tuple[str, int]]],
        native: bool,
        latency: float,
        score: float | None,
    ) -> None:
        sidecar_records = context_records(contexts)
        sidecar_data = jsonl_bytes(sidecar_records)
        sidecar_path.write_bytes(sidecar_data)
        source = {
            "schema": blind.CONTEXT_SCHEMA,
            "sha256": sha256(sidecar_data),
            "cases": len(CASES),
        }
        probe_hash = sha256(self.probe_path.read_bytes())
        run_path.write_bytes(
            jsonl_bytes(
                [
                    abprobe_result(
                        target,
                        candidates[str(target["id"])],
                        probe_hash,
                        len(CASES),
                        schema=evaluator.INPUT_SCHEMA_V7,
                        latency=latency,
                        score=score,
                        sidecar_record=sidecar_record,
                        sidecar_source=source,
                        native=native,
                    )
                    for target, sidecar_record in zip(
                        self.targets, sidecar_records, strict=True
                    )
                ]
            )
        )

    def write_fixed_run(
        self,
        *,
        contexts: dict[str, str],
        context_path: Path,
        run_path: Path,
        candidates: dict[str, list[tuple[str, int]]],
        latency: float,
    ) -> None:
        context_values = context_records(contexts)
        context_data = jsonl_bytes(context_values)
        context_path.write_bytes(context_data)
        context_source = {
            "schema": blind.CONTEXT_SCHEMA,
            "sha256": sha256(context_data),
            "cases": len(CASES),
        }
        fixed_data = self.fixed_boundary_path.read_bytes()
        fixed_values = [json.loads(line) for line in fixed_data.splitlines()]
        fixed_source = {
            "schema": fixed_prepare.SIDECAR_SCHEMA,
            "sha256": sha256(fixed_data),
            "cases": len(CASES),
        }
        probe_hash = sha256(self.probe_path.read_bytes())
        run_path.write_bytes(
            jsonl_bytes(
                [
                    abprobe_result(
                        target,
                        candidates[str(target["id"])],
                        probe_hash,
                        len(CASES),
                        schema=evaluator.INPUT_SCHEMA_V7,
                        latency=latency,
                        score=-1.0,
                        sidecar_record=context_record,
                        sidecar_source=context_source,
                        fixed_boundary_record=fixed_record,
                        fixed_boundary_source=fixed_source,
                    )
                    for target, context_record, fixed_record in zip(
                        self.targets,
                        context_values,
                        fixed_values,
                        strict=True,
                    )
                ]
            )
        )

    def write_all_runs(self) -> None:
        contexts = {case["id"]: case["context"] for case in CASES}
        empty_contexts = {case["id"]: "" for case in CASES}
        self.write_pair(
            contexts=contexts,
            sidecar_path=self.isolated_left_context_path,
            run_path=self.isolated_left_path,
            candidates=ISOLATED_LEFT,
            native=False,
            latency=2.0,
            score=-1.0,
        )
        probe_hash = sha256(self.probe_path.read_bytes())
        raw_mozc = jsonl_bytes(
            [
                mozc_result(
                    target,
                    MOZC_RAW[str(target["id"])],
                    probe_hash,
                    len(CASES),
                )
                for target in self.targets
            ]
        )
        self.fixed_raw_mozc_path.write_bytes(raw_mozc)
        self.fixed_boundary_path.write_bytes(
            fixed_prepare.prepare_sidecar_bytes(raw_mozc)
        )
        self.write_fixed_run(
            contexts=empty_contexts,
            context_path=self.fixed_empty_context_path,
            run_path=self.fixed_empty_path,
            candidates=FIXED_EMPTY,
            latency=2.0,
        )
        self.write_fixed_run(
            contexts=contexts,
            context_path=self.fixed_left_context_path,
            run_path=self.fixed_left_path,
            candidates=FIXED_LEFT,
            latency=3.0,
        )
        self.write_pair(
            contexts=empty_contexts,
            sidecar_path=self.isolated_empty_context_path,
            run_path=self.isolated_empty_path,
            candidates=ISOLATED_EMPTY,
            native=False,
            latency=1.0,
            score=-2.0,
        )
        self.write_pair(
            contexts=empty_contexts,
            sidecar_path=self.native_empty_context_path,
            run_path=self.native_empty_path,
            candidates=NATIVE_EMPTY,
            native=True,
            latency=1.5,
            score=-2.0,
        )
        self.write_pair(
            contexts=contexts,
            sidecar_path=self.native_left_context_path,
            run_path=self.native_left_path,
            candidates=NATIVE_LEFT,
            native=True,
            latency=2.5,
            score=-1.0,
        )

    @staticmethod
    def records(path: Path) -> list[dict[str, object]]:
        return [json.loads(line) for line in path.read_text().splitlines()]

    @staticmethod
    def replace(path: Path, records: list[dict[str, object]]) -> None:
        path.write_bytes(jsonl_bytes(records))

    def evaluate(self, *, native: bool = False, fixed: bool = False) -> dict[str, object]:
        kwargs: dict[str, Path] = {}
        if native:
            kwargs.update({
                "native_empty_v7_path": self.native_empty_path,
                "native_empty_context_sidecar_path": self.native_empty_context_path,
                "native_left_v7_path": self.native_left_path,
                "native_left_context_sidecar_path": self.native_left_context_path,
            })
        if fixed:
            kwargs.update({
                "fixed_raw_mozc_v6_path": self.fixed_raw_mozc_path,
                "fixed_boundary_sidecar_path": self.fixed_boundary_path,
                "fixed_empty_v7_path": self.fixed_empty_path,
                "fixed_empty_context_sidecar_path": self.fixed_empty_context_path,
                "fixed_left_v7_path": self.fixed_left_path,
                "fixed_left_context_sidecar_path": self.fixed_left_context_path,
            })
        return evaluator.evaluate(
            self.manifest_path,
            self.targets_path,
            self.isolated_empty_context_path,
            self.isolated_empty_path,
            self.isolated_left_context_path,
            self.isolated_left_path,
            **kwargs,
        )


class ZenzaiLeftContextQualityTests(unittest.TestCase):
    def test_isolated_primary_control_metrics_score_latency_and_no_raw_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = Fixture(Path(temporary)).evaluate()

        comparison = report["isolated_surface_comparison"]
        natural = comparison["by_context_mode"]["natural_left"]
        control = comparison["by_context_mode"]["empty"]
        self.assertEqual(natural["cases"], 3)
        self.assertEqual(control["cases"], 1)
        boundary = natural["pairwise_rescue_regression"][
            "first_segment_boundary"
        ]["at1"]
        self.assertEqual((boundary["rescued"], boundary["regressed"]), (0, 0))
        e2e = natural["pairwise_rescue_regression"]["end_to_end"]["at1"]
        self.assertEqual((e2e["rescued"], e2e["regressed"]), (2, 1))
        self.assertEqual(e2e["miss"], 0)
        control_e2e = control["pairwise_rescue_regression"]["end_to_end"]["at1"]
        self.assertEqual(control_e2e["unchanged_hit"], 1)
        self.assertEqual(
            natural["top1_score"]["delta_comparability"]["raw"][
                "comparable_cases"
            ],
            3,
        )
        self.assertEqual(
            natural["top1_score"]["left_context_minus_empty_raw"]["median"],
            1.0,
        )
        self.assertEqual(
            natural["latency_ms"]["left_context_minus_empty_ms"]["median"],
            1.0,
        )
        self.assertEqual(
            natural["memory_kib"]["systems"][
                evaluator.ISOLATED_EMPTY_SYSTEM
            ]["process_rss_kib"]["after"]["median"],
            110.0,
        )
        self.assertEqual(
            natural["memory_kib"]["systems"][
                evaluator.ISOLATED_LEFT_CONTEXT_SYSTEM
            ]["process_pss_kib"]["delta_after_minus_before"]["median"],
            5.0,
        )
        self.assertIn(
            "sequential separate ABProbe processes",
            report["evaluation_scope"]["memory_measurement_semantics"],
        )
        self.assertEqual(
            set(natural["systems"]),
            {
                evaluator.ISOLATED_EMPTY_SYSTEM,
                evaluator.ISOLATED_LEFT_CONTEXT_SYSTEM,
            },
        )
        self.assertEqual(
            set(report["zenzai_execution"]),
            {"isolated_empty_v7", "isolated_left_v7"},
        )
        buckets = comparison["by_context_code_point_length"]
        self.assertEqual(buckets["0"]["cases"], 1)
        self.assertEqual(buckets["1-8"]["cases"], 1)
        self.assertEqual(buckets["9-16"]["cases"], 1)
        self.assertEqual(buckets["33-64"]["cases"], 1)
        rendered = json.dumps(report, ensure_ascii=False)
        for case in CASES:
            if case["context"]:
                self.assertNotIn(case["context"], rendered)
        self.assertFalse(report["native_boundary_comparison"]["available"])

    def test_native_pair_reports_boundary_rescue_regression_and_miss(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = Fixture(Path(temporary)).evaluate(native=True)

        native = report["native_boundary_comparison"]
        self.assertTrue(native["available"])
        transition = native["all_cases"]["pairwise_rescue_regression"][
            "first_segment_boundary"
        ]["at1"]
        self.assertEqual(transition["rescued"], 1)
        self.assertEqual(transition["regressed"], 1)
        self.assertEqual(transition["unchanged_hit"], 1)
        self.assertEqual(transition["unchanged_miss"], 1)
        self.assertEqual(transition["miss"], 1)
        self.assertEqual(
            native["by_context_mode"]["natural_left"]["cases"], 3
        )
        self.assertEqual(native["by_context_mode"]["empty"]["cases"], 1)
        self.assertEqual(report["evaluation_scope"]["requested_top_k"], 4)
        self.assertEqual(report["evaluation_scope"]["effective_comparable_top_k"], 4)
        self.assertEqual(
            report["evaluation_scope"]["native_first_clause_results_top_k_limit"],
            5,
        )

    def test_native_pair_allows_unexposed_candidate_scores(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            for path in (fixture.native_empty_path, fixture.native_left_path):
                records = fixture.records(path)
                for record in records:
                    for candidate in record["candidates"]:
                        candidate["zenzai_score"] = None
                        candidate["zenzai_score_token_count"] = None
                        candidate["zenzai_score_scope"] = None
                fixture.replace(path, records)
            report = fixture.evaluate(native=True)

        score = report["native_boundary_comparison"]["all_cases"]["top1_score"]
        self.assertEqual(
            score["delta_comparability"]["raw"]["comparable_cases"], 0
        )
        self.assertEqual(
            score["delta_comparability"]["per_token"]["noncomparable_cases"],
            len(CASES),
        )

    def test_raw_score_delta_requires_equal_token_count(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            records = fixture.records(fixture.isolated_left_path)
            for record in records:
                for candidate in record["candidates"]:
                    candidate["zenzai_score_token_count"] = 3
            fixture.replace(fixture.isolated_left_path, records)
            report = fixture.evaluate()

        score = report["isolated_surface_comparison"]["all_cases"]["top1_score"]
        self.assertEqual(
            score["delta_comparability"]["raw"]["comparable_cases"], 0
        )
        self.assertEqual(
            score["delta_comparability"]["per_token"]["comparable_cases"],
            len(CASES),
        )
        self.assertEqual(score["left_context_minus_empty_raw"]["count"], 0)
        self.assertEqual(
            score["left_context_minus_empty_per_token"]["count"], len(CASES)
        )

    def test_context_sidecar_hash_and_reviewed_source_binding_are_strict(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            records = fixture.records(fixture.isolated_left_context_path)
            records[0]["left_context"] = "改竄"
            fixture.replace(fixture.isolated_left_context_path, records)
            with self.assertRaisesRegex(ValueError, "does not match exact UTF-8"):
                fixture.evaluate()

        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            records = fixture.records(fixture.isolated_left_context_path)
            records[0]["source_content_sha256"] = "sha256:" + "f" * 64
            fixture.replace(fixture.isolated_left_context_path, records)
            with self.assertRaisesRegex(ValueError, "reviewed source.row_sha256"):
                fixture.evaluate()

    def test_v7_context_attestation_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            records = fixture.records(fixture.isolated_left_path)
            records[0]["context"]["left_context_utf8_byte_count"] += 1
            fixture.replace(fixture.isolated_left_path, records)
            with self.assertRaisesRegex(ValueError, "does not match sidecar"):
                fixture.evaluate()

    def test_resource_model_producer_and_top_k_drift_are_rejected(self) -> None:
        mutations = (
            (
                "resource",
                lambda record: record["resource"].update(
                    {"fingerprint": "sha256:" + "f" * 64}
                ),
            ),
            (
                "Zenzai",
                lambda record: record["quality_policy"]["zenzai"].update(
                    {"model_sha256": "sha256:" + "f" * 64}
                ),
            ),
            (
                "producer",
                lambda record: record["producer"].update(
                    {"sha256": "sha256:" + "f" * 64}
                ),
            ),
            ("top_k", lambda record: record.update({"top_k": 5})),
        )
        for expected, mutate in mutations:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as temporary:
                fixture = Fixture(Path(temporary))
                records = fixture.records(fixture.isolated_left_path)
                for record in records:
                    mutate(record)
                fixture.replace(fixture.isolated_left_path, records)
                with self.assertRaisesRegex(ValueError, expected):
                    fixture.evaluate()

    def test_boundary_policy_runs_must_share_one_hazkey_v7_acquisition(self) -> None:
        mutations = (
            (
                "producer",
                lambda record: record["producer"].update(
                    {"sha256": "sha256:" + "f" * 64}
                ),
            ),
            (
                "resource",
                lambda record: record["resource"].update(
                    {"fingerprint": "sha256:" + "f" * 64}
                ),
            ),
            (
                "quality_policy",
                lambda record: record["quality_policy"]["zenzai"].update(
                    {"model_sha256": "sha256:" + "f" * 64}
                ),
            ),
            ("source_ref", lambda record: record.update({"source_ref": "f" * 40})),
            ("top_k", lambda record: record.update({"top_k": 5})),
        )
        for expected, mutate in mutations:
            with self.subTest(field=expected), tempfile.TemporaryDirectory() as temporary:
                fixture = Fixture(Path(temporary))
                for path in (fixture.native_empty_path, fixture.native_left_path):
                    records = fixture.records(path)
                    for record in records:
                        mutate(record)
                    fixture.replace(path, records)
                with self.assertRaisesRegex(
                    ValueError, f"common acquisition {expected} differs"
                ):
                    fixture.evaluate(native=True)

    def test_boundary_policy_runs_must_share_exact_context_sidecars(self) -> None:
        roles = (
            (
                "empty",
                "native_empty_v7",
                "native_empty_context_path",
                "native_empty_path",
            ),
            (
                "natural-left",
                "native_left_v7",
                "native_left_context_path",
                "native_left_path",
            ),
        )
        for role, label, sidecar_attribute, run_attribute in roles:
            with self.subTest(role=role), tempfile.TemporaryDirectory() as temporary:
                fixture = Fixture(Path(temporary))
                sidecar_path = getattr(fixture, sidecar_attribute)
                run_path = getattr(fixture, run_attribute)
                sidecar_records = fixture.records(sidecar_path)
                rewritten = b"".join(
                    (json.dumps(record, ensure_ascii=False) + "\n").encode()
                    for record in sidecar_records
                )
                sidecar_path.write_bytes(rewritten)
                records = fixture.records(run_path)
                for record in records:
                    record["context"]["source"]["sha256"] = sha256(rewritten)
                fixture.replace(run_path, records)
                with self.assertRaisesRegex(
                    ValueError, f"{role} context_source differs for {label}"
                ):
                    fixture.evaluate(native=True)

    def test_native_comparison_caps_common_top_k_at_five(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            for path in (
                fixture.isolated_empty_path,
                fixture.isolated_left_path,
                fixture.native_empty_path,
                fixture.native_left_path,
            ):
                records = fixture.records(path)
                for record in records:
                    record["top_k"] = 6
                fixture.replace(path, records)
            with self.assertRaisesRegex(ValueError, "common top_k must be <= 5"):
                fixture.evaluate(native=True)

    def test_native_arguments_are_all_or_none(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            with self.assertRaisesRegex(ValueError, "all four"):
                evaluator.evaluate(
                    fixture.manifest_path,
                    fixture.targets_path,
                    fixture.isolated_empty_context_path,
                    fixture.isolated_empty_path,
                    fixture.isolated_left_context_path,
                    fixture.isolated_left_path,
                    native_empty_v7_path=fixture.native_empty_path,
                )

    def test_native_empty_and_left_context_roles_are_enforced(self) -> None:
        empty_contexts = {case["id"]: "" for case in CASES}
        natural_contexts = {case["id"]: case["context"] for case in CASES}
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            fixture.write_pair(
                contexts=empty_contexts,
                sidecar_path=fixture.native_left_context_path,
                run_path=fixture.native_left_path,
                candidates=NATIVE_LEFT,
                native=True,
                latency=2.5,
                score=-1.0,
            )
            with self.assertRaisesRegex(ValueError, "natural_left case"):
                fixture.evaluate(native=True)

        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            fixture.write_pair(
                contexts=natural_contexts,
                sidecar_path=fixture.native_empty_context_path,
                run_path=fixture.native_empty_path,
                candidates=NATIVE_EMPTY,
                native=True,
                latency=1.5,
                score=-2.0,
            )
            with self.assertRaisesRegex(ValueError, "must have empty context"):
                fixture.evaluate(native=True)

    def test_isolated_empty_and_left_context_roles_are_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            empty_contexts = {case["id"]: "" for case in CASES}
            fixture.write_pair(
                contexts=empty_contexts,
                sidecar_path=fixture.isolated_left_context_path,
                run_path=fixture.isolated_left_path,
                candidates=ISOLATED_LEFT,
                native=False,
                latency=2.0,
                score=-1.0,
            )
            with self.assertRaisesRegex(ValueError, "natural_left case"):
                fixture.evaluate()

        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            natural_contexts = {case["id"]: case["context"] for case in CASES}
            fixture.write_pair(
                contexts=natural_contexts,
                sidecar_path=fixture.isolated_empty_context_path,
                run_path=fixture.isolated_empty_path,
                candidates=ISOLATED_EMPTY,
                native=False,
                latency=1.0,
                score=-2.0,
            )
            with self.assertRaisesRegex(ValueError, "must have empty context"):
                fixture.evaluate()

    def test_boundary_policy_and_conversion_path_must_agree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            records = fixture.records(fixture.isolated_left_path)
            records[0]["boundary_policy"] = boundary_policy(True)
            fixture.replace(fixture.isolated_left_path, records)
            with self.assertRaisesRegex(ValueError, "conflicts with conversion_path"):
                fixture.evaluate()

    def test_isolated_and_native_runs_require_null_fixed_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            records = fixture.records(fixture.isolated_left_path)
            records[0]["fixed_boundary"] = {
                "reading_sha256": "sha256:" + "1" * 64,
                "consuming_count": 1,
                "source": {
                    "schema": "hazkey.mozc-fixed-boundary.v1",
                    "sha256": "sha256:" + "2" * 64,
                    "cases": len(CASES),
                },
            }
            fixture.replace(fixture.isolated_left_path, records)
            with self.assertRaisesRegex(ValueError, "fixed_boundary must be null"):
                fixture.evaluate()

    def test_all_v7_modes_allow_candidate_scores_to_be_null(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            for path in (
                fixture.isolated_empty_path,
                fixture.isolated_left_path,
                fixture.native_empty_path,
                fixture.native_left_path,
            ):
                records = fixture.records(path)
                for record in records:
                    for candidate in record["candidates"]:
                        candidate["zenzai_score"] = None
                        candidate["zenzai_score_token_count"] = None
                        candidate["zenzai_score_scope"] = None
                fixture.replace(path, records)
            report = fixture.evaluate(native=True)

        isolated_scores = report["isolated_surface_comparison"]["all_cases"][
            "top1_score"
        ]
        self.assertEqual(
            isolated_scores["delta_comparability"]["raw"]["comparable_cases"],
            0,
        )
        native_scores = report["native_boundary_comparison"]["all_cases"][
            "top1_score"
        ]
        self.assertEqual(
            native_scores["delta_comparability"]["raw"]["comparable_cases"],
            0,
        )
        self.assertFalse(
            report["evaluation_scope"]["candidate_score_required_for_v7"]
        )

    def test_v7_execution_evidence_is_required_and_totals_are_strict(self) -> None:
        mutations = (
            (
                "fields differ",
                lambda record: record.pop("zenzai_execution"),
            ),
            (
                "attempt_outcomes total",
                lambda record: record["zenzai_execution"]["attempt_outcomes"].update(
                    {"pass": 99}
                ),
            ),
            (
                "terminal_outcomes total",
                lambda record: record["zenzai_execution"]["terminal_outcomes"].update(
                    {"pass": 0}
                ),
            ),
        )
        for expected, mutate in mutations:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as temporary:
                fixture = Fixture(Path(temporary))
                records = fixture.records(fixture.isolated_left_path)
                mutate(records[0])
                fixture.replace(fixture.isolated_left_path, records)
                with self.assertRaisesRegex(ValueError, expected):
                    fixture.evaluate()

    def test_v7_run_requires_at_least_one_model_evaluation_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            records = fixture.records(fixture.isolated_left_path)
            for record in records:
                evidence = record["zenzai_execution"]
                evidence["evaluation_attempt_count"] = 0
                evidence["attempt_outcomes"] = {
                    "pass": 0,
                    "fix_required": 0,
                    "whole_result": 0,
                    "error": 0,
                }
                request_count = evidence["request_count"]
                evidence["terminal_outcomes"] = {
                    "pass": 0,
                    "fix_required": 0,
                    "whole_result": 0,
                    "error": 0,
                    "inference_limit": 0,
                    "no_candidate": request_count,
                }
            fixture.replace(fixture.isolated_left_path, records)
            with self.assertRaisesRegex(ValueError, "no model evaluation attempt"):
                fixture.evaluate()

    def test_terminal_pass_requires_a_corresponding_evaluation_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            records = fixture.records(fixture.isolated_left_path)
            records[0]["zenzai_execution"] = {
                "request_count": 1,
                "evaluation_attempt_count": 0,
                "attempt_outcomes": {
                    "pass": 0,
                    "fix_required": 0,
                    "whole_result": 0,
                    "error": 0,
                },
                "terminal_outcomes": {
                    "pass": 1,
                    "fix_required": 0,
                    "whole_result": 0,
                    "error": 0,
                    "inference_limit": 0,
                    "no_candidate": 0,
                },
            }
            fixture.replace(fixture.isolated_left_path, records)
            with self.assertRaisesRegex(
                ValueError, "terminal_outcomes.pass cannot exceed"
            ):
                fixture.evaluate()

    def test_execution_request_count_and_inference_limit_are_mode_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            records = fixture.records(fixture.isolated_left_path)
            records[0]["zenzai_execution"] = {
                "request_count": 1,
                "evaluation_attempt_count": 1,
                "attempt_outcomes": {
                    "pass": 1,
                    "fix_required": 0,
                    "whole_result": 0,
                    "error": 0,
                },
                "terminal_outcomes": {
                    "pass": 1,
                    "fix_required": 0,
                    "whole_result": 0,
                    "error": 0,
                    "inference_limit": 0,
                    "no_candidate": 0,
                },
            }
            fixture.replace(fixture.isolated_left_path, records)
            with self.assertRaisesRegex(
                ValueError, "request_count must be 2 for boundary mode"
            ):
                fixture.evaluate()

        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            records = fixture.records(fixture.isolated_left_path)
            records[0]["zenzai_execution"] = {
                "request_count": 2,
                "evaluation_attempt_count": 21,
                "attempt_outcomes": {
                    "pass": 21,
                    "fix_required": 0,
                    "whole_result": 0,
                    "error": 0,
                },
                "terminal_outcomes": {
                    "pass": 2,
                    "fix_required": 0,
                    "whole_result": 0,
                    "error": 0,
                    "inference_limit": 0,
                    "no_candidate": 0,
                },
            }
            fixture.replace(fixture.isolated_left_path, records)
            with self.assertRaisesRegex(
                ValueError, r"exceeds inference_limit \* request_count"
            ):
                fixture.evaluate()

    def test_no_candidate_terminal_allows_zero_case_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            records = fixture.records(fixture.isolated_left_path)
            records[0]["zenzai_execution"] = {
                "request_count": 2,
                "evaluation_attempt_count": 0,
                "attempt_outcomes": {
                    "pass": 0,
                    "fix_required": 0,
                    "whole_result": 0,
                    "error": 0,
                },
                "terminal_outcomes": {
                    "pass": 0,
                    "fix_required": 0,
                    "whole_result": 0,
                    "error": 0,
                    "inference_limit": 0,
                    "no_candidate": 2,
                },
            }
            fixture.replace(fixture.isolated_left_path, records)
            report = fixture.evaluate()

        execution = report["zenzai_execution"]["isolated_left_v7"]
        self.assertEqual(execution["terminal_outcomes"]["no_candidate"], 2)

    def test_terminal_failures_remain_in_counts_and_become_formal_blockers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            records = fixture.records(fixture.isolated_left_path)
            records[0]["zenzai_execution"] = {
                "request_count": 2,
                "evaluation_attempt_count": 2,
                "attempt_outcomes": {
                    "pass": 0,
                    "fix_required": 1,
                    "whole_result": 1,
                    "error": 0,
                },
                "terminal_outcomes": {
                    "pass": 0,
                    "fix_required": 1,
                    "whole_result": 1,
                    "error": 0,
                    "inference_limit": 0,
                    "no_candidate": 0,
                },
            }
            records[1]["zenzai_execution"] = {
                "request_count": 2,
                "evaluation_attempt_count": 1,
                "attempt_outcomes": {
                    "pass": 0,
                    "fix_required": 0,
                    "whole_result": 0,
                    "error": 1,
                },
                "terminal_outcomes": {
                    "pass": 0,
                    "fix_required": 0,
                    "whole_result": 0,
                    "error": 1,
                    "inference_limit": 1,
                    "no_candidate": 0,
                },
            }
            records[2]["zenzai_execution"] = {
                "request_count": 2,
                "evaluation_attempt_count": 1,
                "attempt_outcomes": {
                    "pass": 1,
                    "fix_required": 0,
                    "whole_result": 0,
                    "error": 0,
                },
                "terminal_outcomes": {
                    "pass": 1,
                    "fix_required": 0,
                    "whole_result": 0,
                    "error": 0,
                    "inference_limit": 0,
                    "no_candidate": 1,
                },
            }
            fixture.replace(fixture.isolated_left_path, records)
            report = fixture.evaluate()

        execution = report["zenzai_execution"]["isolated_left_v7"]
        for outcome in (
            "fix_required",
            "whole_result",
            "error",
            "inference_limit",
            "no_candidate",
        ):
            self.assertEqual(execution["terminal_outcomes"][outcome], 1)
            self.assertTrue(
                any(outcome in blocker for blocker in report["decision"]["formal_blockers"])
            )

    def test_mozc_fixed_pair_verifies_chain_and_allows_zero_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = Fixture(Path(temporary)).evaluate(fixed=True)

        fixed = report["mozc_fixed_boundary_comparison"]
        self.assertTrue(fixed["available"])
        self.assertEqual(fixed["boundary_policy"]["mode"], "mozc_fixed")
        self.assertTrue(all(fixed["provenance_chain_verified"].values()))
        transition = fixed["all_cases"]["pairwise_rescue_regression"][
            "first_segment_boundary"
        ]["at1"]
        self.assertEqual(transition["rescued"], 0)
        self.assertEqual(transition["regressed"], 0)
        zero_case = next(
            case for case in fixed["cases"] if case["id"] == "second-rescue"
        )
        self.assertFalse(
            zero_case["top1_score"][evaluator.FIXED_EMPTY_SYSTEM]["available"]
        )
        for system in evaluator.FIXED_SYSTEMS:
            outcome = zero_case["gold_outcomes"]["systems"][system]
            self.assertTrue(outcome["first_segment_boundary"]["at1"])
            conditional = outcome[
                "conditional_surface_given_acceptable_first_segment_boundary"
            ]
            self.assertTrue(conditional["at1_comparable"])
            self.assertFalse(conditional["at1_hit"])
            self.assertFalse(outcome["end_to_end"]["at1"])
        self.assertEqual(
            fixed["inputs"]["fixed_boundary_sidecar"]["origin"]["sha256"],
            fixed["inputs"]["raw_mozc_v6"]["sha256"],
        )

    def test_three_policy_matrix_fixed_mozc_control_category_and_boundary_deltas(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            report = fixture.evaluate(native=True, fixed=True)

        cross = report["within_context_boundary_policy_pairwise"]
        self.assertTrue(cross["available"])
        natural_matrix = cross["natural_left"]["pairwise_matrix"]
        isolated_to_native = natural_matrix[
            evaluator.ISOLATED_LEFT_CONTEXT_SYSTEM
        ][evaluator.NATIVE_LEFT_CONTEXT_SYSTEM]
        boundary = isolated_to_native["first_segment_boundary"]["at1"]
        self.assertEqual((boundary["rescued"], boundary["regressed"]), (0, 2))
        self.assertIn("proper-noun", cross["natural_left"]["by_category"])
        self.assertEqual(
            cross["natural_left"]["by_category"]["proper-noun"]["cases"], 1
        )

        native_diagnostics = report["native_boundary_comparison"]["all_cases"][
            "boundary_diagnostics"
        ]["systems"][evaluator.NATIVE_LEFT_CONTEXT_SYSTEM]
        self.assertEqual(
            native_diagnostics["classification_counts"],
            {"match": 2, "too_long": 2},
        )
        native_case = next(
            case
            for case in report["native_boundary_comparison"]["cases"]
            if case["id"] == "surface-regression"
        )
        case_diagnostic = native_case["boundary_diagnostics"]["systems"][
            evaluator.NATIVE_LEFT_CONTEXT_SYSTEM
        ]
        self.assertEqual(case_diagnostic["acceptable_consuming_counts"], [2])
        self.assertEqual(case_diagnostic["nearest_acceptable_signed_delta"], 1)
        self.assertEqual(case_diagnostic["minimum_absolute_delta"], 1)

        fixed = report["mozc_fixed_boundary_comparison"]
        self.assertIn(
            evaluator.MOZC_AT_FIXED_BOUNDARY_SYSTEM,
            fixed["all_cases"]["systems"],
        )
        controlled = fixed["mozc_to_hazkey_at_fixed_boundary"]["natural_left"]
        self.assertEqual(
            controlled["baseline"], evaluator.MOZC_AT_FIXED_BOUNDARY_SYSTEM
        )
        surface_rescue = next(
            case for case in fixed["cases"] if case["id"] == "surface-rescue"
        )
        self.assertEqual(surface_rescue["mozc_at_fixed_boundary_candidate_count"], 1)
        fixed_diagnostic = surface_rescue["boundary_diagnostics"]["systems"][
            evaluator.MOZC_AT_FIXED_BOUNDARY_SYSTEM
        ]
        self.assertEqual(fixed_diagnostic["count_source"], "explicit_fixed_boundary")
        self.assertEqual(fixed_diagnostic["classification"], "match")
        self.assertIn("proper-noun", fixed["by_category"])

        too_short = evaluator._boundary_diagnostic(
            [{"consuming_count": 1}],
            {
                "acceptable_first_spans": [{"count": 2}],
            },
        )
        self.assertEqual(too_short["classification"], "too_short")
        self.assertEqual(too_short["nearest_acceptable_signed_delta"], -1)

    def test_mozc_fixed_raw_and_sidecar_tamper_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            records = fixture.records(fixture.fixed_raw_mozc_path)
            records[0]["candidates"][0]["text"] = "改竄"
            fixture.replace(fixture.fixed_raw_mozc_path, records)
            with self.assertRaisesRegex(ValueError, "canonical derivation"):
                fixture.evaluate(fixed=True)

        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            records = fixture.records(fixture.fixed_boundary_path)
            records[0]["consuming_count"] = 1
            fixture.replace(fixture.fixed_boundary_path, records)
            with self.assertRaisesRegex(ValueError, "canonical derivation"):
                fixture.evaluate(fixed=True)

        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            records = fixture.records(fixture.fixed_boundary_path)
            records[0], records[1] = records[1], records[0]
            fixture.replace(fixture.fixed_boundary_path, records)
            with self.assertRaisesRegex(ValueError, "canonical derivation"):
                fixture.evaluate(fixed=True)

    def test_mozc_fixed_result_must_bind_sidecar_and_keep_candidate_span(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            records = fixture.records(fixture.fixed_left_path)
            for record in records:
                record["fixed_boundary"]["source"]["sha256"] = (
                    "sha256:" + "f" * 64
                )
            fixture.replace(fixture.fixed_left_path, records)
            with self.assertRaisesRegex(ValueError, "exact sidecar bytes"):
                fixture.evaluate(fixed=True)

        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            records = fixture.records(fixture.fixed_left_path)
            records[0]["candidates"][0]["consuming_count"] = 1
            fixture.replace(fixture.fixed_left_path, records)
            with self.assertRaisesRegex(ValueError, "escape the fixed boundary"):
                fixture.evaluate(fixed=True)

    def test_mozc_fixed_arguments_are_all_or_none(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            with self.assertRaisesRegex(ValueError, "requires raw Mozc v6"):
                evaluator.evaluate(
                    fixture.manifest_path,
                    fixture.targets_path,
                    fixture.isolated_empty_context_path,
                    fixture.isolated_empty_path,
                    fixture.isolated_left_context_path,
                    fixture.isolated_left_path,
                    fixed_raw_mozc_v6_path=fixture.fixed_raw_mozc_path,
                )


if __name__ == "__main__":
    unittest.main()
