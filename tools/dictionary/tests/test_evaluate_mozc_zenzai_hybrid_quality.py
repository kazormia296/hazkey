from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools.dictionary import compile_mozc_acceptable_path_evaluation as compiler
from tools.dictionary import evaluate_mozc_zenzai_hybrid_quality as evaluator


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
        "surface_boundary": 1,
    },
    {
        "id": "surface-displacement",
        "category": "long-structural",
        "reading": "かきく",
        "surface": "正二末",
        "surface_boundary": 2,
    },
    {
        "id": "boundary-rescue",
        "category": "proper-noun",
        "reading": "さしす",
        "surface": "境末",
        "surface_boundary": None,
    },
]


MOZC_CANDIDATES = {
    "surface-rescue": [
        ("誤一", 2),
        ("誤二", 2),
        ("誤三", 2),
        ("Mozc四", 2),
    ],
    "surface-displacement": [
        ("別一", 2),
        ("別二", 2),
        ("別三", 2),
        ("正二", 2),
    ],
    "boundary-rescue": [("先", 3)],
}


HAZKEY_CANDIDATES = {
    "surface-rescue": [
        ("正", 2, -1.0),
        ("異", 2, -3.0),
    ],
    "surface-displacement": [("誤", 2, None)],
    "boundary-rescue": [("境", 2, -2.0)],
}


def compiler_source_record(case: dict[str, object]) -> dict[str, object]:
    case_id = str(case["id"])
    reading = str(case["reading"])
    surface = str(case["surface"])
    surface_boundary = case["surface_boundary"]
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
            "surface_references": [{"id": "surface-0", "text": surface}],
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
                "surface_boundaries": (
                    None if surface_boundary is None else [surface_boundary]
                ),
                "alignment_status": (
                    "reading_only" if surface_boundary is None else "aligned"
                ),
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


def quality_policy(enabled: bool) -> dict[str, object]:
    nullable = {
        "model_path": None,
        "model_size_bytes": None,
        "model_sha256": None,
        "inference_limit": None,
        "resolved_device": None,
    }
    if enabled:
        nullable = {
            "model_path": "/fixture/zenzai.gguf",
            "model_size_bytes": 2048,
            "model_sha256": "sha256:" + "d" * 64,
            "inference_limit": 10,
            "resolved_device": "Vulkan0",
        }
    return {
        "learning": False,
        "context": "empty",
        "zenzai": {"enabled": enabled, **nullable},
    }


def measurement() -> dict[str, object]:
    return {
        "warmups": 0,
        "iterations": 1,
        "latency_ms": {
            "median": 1.0,
            "p95": 1.0,
            "minimum": 1.0,
            "maximum": 1.0,
            "samples": [1.0],
        },
        "rss": {"before_kib": 100, "after_kib": 100},
        "backend_diagnostics": {},
    }


def abprobe_result(
    target: dict[str, object],
    converter: str,
    probe_hash: str,
    total: int,
) -> dict[str, object]:
    is_hazkey = converter == "hazkey"
    if is_hazkey:
        candidates = [
            {
                "text": text,
                "rank": rank,
                "consuming_count": count,
                "provenance": "standard",
                "ranking_influence": "zenzai",
                "zenzai_score": score,
                "zenzai_score_token_count": 2 if score is not None else None,
                "zenzai_score_scope": (
                    ("full_candidate" if rank == 1 else "constraint_suffix")
                    if score is not None
                    else None
                ),
            }
            for rank, (text, count, score) in enumerate(
                HAZKEY_CANDIDATES[str(target["id"])], 1
            )
        ]
    else:
        candidates = [
            {
                "text": text,
                "rank": rank,
                "consuming_count": count,
                "provenance": "standard",
                "ranking_influence": "standard",
                "zenzai_score": None,
                "zenzai_score_token_count": None,
                "zenzai_score_scope": None,
            }
            for rank, (text, count) in enumerate(
                MOZC_CANDIDATES[str(target["id"])], 1
            )
        ]
    return {
        "schema": evaluator.INPUT_SCHEMA,
        "conversion_path": evaluator.CONVERSION_PATH,
        "id": target["id"],
        "reading": target["reading"],
        "category": target["category"],
        "backend": "Hazkey+Zenzai" if is_hazkey else "Mozc",
        "backend_version": "quality-test-v1",
        "converter_backend": converter,
        "source_ref": "a" * 40,
        "resource": {
            "kind": "hazkey_dictionary" if is_hazkey else "mozc_runtime_inputs",
            "path": "/fixture/hazkey" if is_hazkey else "/fixture/mozc",
            "fingerprint": "sha256:" + ("b" if is_hazkey else "c") * 64,
        },
        "producer": {
            "path": "/fixture/ab-probe",
            "size_bytes": 1234,
            "sha256": "sha256:" + "e" * 64,
        },
        "quality_policy": quality_policy(is_hazkey),
        "top_k": 4,
        "corpus": {"sha256": probe_hash, "cases": total},
        "candidates": candidates,
        "composition_span": span(len(str(target["reading"]))),
        "measurement": measurement(),
    }


class Fixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.generation = root / "generation"
        self.reviewed_path = self.generation / compiler.SOURCE_REVIEWED_PATHS_NAME
        self.annotation_path = (
            self.generation / compiler.SOURCE_ANNOTATION_MANIFEST_NAME
        )
        self.probe_path = self.generation / compiler.PROBE_INPUT_NAME
        self.targets_path = self.generation / compiler.TARGETS_NAME
        self.manifest_path = self.generation / compiler.MANIFEST_NAME
        self.mozc_path = root / "mozc-v6.jsonl"
        self.hazkey_path = root / "hazkey-zenzai-v6.jsonl"
        self.write_generation()
        self.write_runs()

    def write_generation(self) -> None:
        reviewed_bytes = jsonl_bytes(
            [compiler_source_record(case) for case in CASES]
        )
        annotation = {
            "schema": compiler.ANNOTATION_MANIFEST_SCHEMA,
            "queue_sha256": "sha256:" + "1" * 64,
            "workbook_sha256": "sha256:" + "4" * 64,
            "reviewed_paths_sha256": sha256(reviewed_bytes),
            "cases": len(CASES),
            "path_set_statuses": {"closed": len(CASES)},
            "complete": True,
            "formal_authorized": False,
            "diagnostic_only": True,
        }
        generated = compiler.prepare_outputs_bytes(
            reviewed_paths_data=reviewed_bytes,
            annotation_manifest_data=json_bytes(annotation),
        )
        self.generation.mkdir()
        for name, data in generated.items():
            (self.generation / name).write_bytes(data)
        self.targets = [
            json.loads(line) for line in self.targets_path.read_text().splitlines()
        ]

    def write_runs(self) -> None:
        probe_hash = sha256(self.probe_path.read_bytes())
        self.mozc_path.write_bytes(
            jsonl_bytes(
                [
                    abprobe_result(target, "mozc", probe_hash, len(self.targets))
                    for target in self.targets
                ]
            )
        )
        self.hazkey_path.write_bytes(
            jsonl_bytes(
                [
                    abprobe_result(
                        target, "hazkey", probe_hash, len(self.targets)
                    )
                    for target in self.targets
                ]
            )
        )

    def records(self, path: Path) -> list[dict[str, object]]:
        return [json.loads(line) for line in path.read_text().splitlines()]

    def replace_records(
        self, path: Path, records: list[dict[str, object]]
    ) -> None:
        path.write_bytes(jsonl_bytes(records))

    def evaluate(self) -> dict[str, object]:
        return evaluator.evaluate(
            self.manifest_path,
            self.targets_path,
            self.mozc_path,
            self.hazkey_path,
        )


class MozcZenzaiHybridQualityEvaluationTests(unittest.TestCase):
    def test_same_denominator_metrics_h0_coverage_and_invariants(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = Fixture(Path(temporary)).evaluate()

        systems = report["all_cases"]["systems"]
        mozc = systems[evaluator.MOZC_SYSTEM]
        hazkey = systems[evaluator.HAZKEY_ZENZAI_SYSTEM]
        h0 = systems[evaluator.H0_SYSTEM]
        self.assertEqual(mozc["first_segment_boundary"]["at1"]["hits"], 2)
        self.assertEqual(hazkey["first_segment_boundary"]["at1"]["hits"], 3)
        self.assertEqual(
            h0["first_segment_boundary"]["at1"],
            mozc["first_segment_boundary"]["at1"],
        )
        self.assertEqual(h0["end_to_end"]["at1"], mozc["end_to_end"]["at1"])
        self.assertEqual(mozc["end_to_end"]["at_k"]["hits"], 1)
        self.assertEqual(hazkey["end_to_end"]["at_k"]["hits"], 1)
        self.assertEqual(h0["end_to_end"]["at_k"]["hits"], 1)
        delta = report["all_cases"]["pairwise_rescue_regression"][
            f"{evaluator.H0_SYSTEM}_vs_{evaluator.MOZC_SYSTEM}"
        ]["end_to_end"]["at_k"]
        self.assertEqual((delta["rescued"], delta["regressed"], delta["net"]), (1, 1, 0))
        coverage = report["all_cases"]["h0_additional_coverage"]
        self.assertEqual(coverage["windows_with_new_hazkey_zenzai_candidate"], 2)
        self.assertEqual(coverage["new_hazkey_zenzai_candidates"], 2)
        self.assertEqual(
            coverage["fully_aligned_windows_with_gold_hit_from_added_candidate"],
            1,
        )
        score_evidence = report["all_cases"]["zenzai_score_evidence"]
        self.assertEqual(score_evidence["scored_candidates"], 3)
        self.assertEqual(score_evidence["cases_with_score"], 2)
        self.assertEqual(score_evidence["top1_scored_cases"], 2)
        self.assertEqual(
            score_evidence["score_scope_counts"],
            {"full_candidate": 2, "constraint_suffix": 1},
        )
        self.assertFalse(score_evidence["candidate_score_margin_available"])

    def test_score_normalization_and_override_scopes_do_not_leak_gold(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = Fixture(Path(temporary)).evaluate()

        cases = {case["id"]: case for case in report["cases"]}
        rescue = cases["surface-rescue"]
        evidence = rescue["runtime_features"]["hazkey_zenzai_evidence"]
        self.assertEqual(evidence["scored_candidate_count"], 2)
        self.assertEqual(
            evidence["top_ranked_scored_candidate"]["zenzai_score_per_token"],
            -0.5,
        )
        self.assertIsNone(evidence["candidate_score_margin"])
        self.assertEqual(
            evidence["score_scope_counts"],
            {"full_candidate": 1, "constraint_suffix": 1},
        )
        self.assertIn(
            "first pass", evidence["candidate_score_margin_unavailable_reason"]
        )
        observed = rescue["runtime_features"]["observed_candidates"][
            evaluator.HAZKEY_ZENZAI_SYSTEM
        ][0]
        self.assertEqual(observed["zenzai_score_per_token"], -0.5)
        self.assertEqual(observed["zenzai_score_scope"], "full_candidate")
        self.assertNotIn("gold_category", rescue["runtime_features"])
        self.assertTrue(
            rescue["runtime_features"]["override_trigger_scope"][
                "surface_override"
            ]["eligible"]
        )
        self.assertEqual(
            rescue["runtime_features"]["override_trigger_scope"][
                "surface_override"
            ]["candidate_surface_source"],
            evaluator.HAZKEY_ZENZAI_SURFACE_SOURCE,
        )
        self.assertEqual(
            rescue["gold_outcomes"]["override_outcomes"]["surface_override"][
                "end_to_end_at1"
            ],
            "rescued",
        )
        boundary = cases["boundary-rescue"]
        self.assertTrue(
            boundary["runtime_features"]["override_trigger_scope"][
                "boundary_override"
            ]["eligible"]
        )
        self.assertEqual(
            boundary["runtime_features"]["override_trigger_scope"][
                "boundary_override"
            ]["candidate_boundary_source"],
            evaluator.HAZKEY_BOUNDARY_SOURCE,
        )
        self.assertEqual(
            boundary["gold_outcomes"]["override_outcomes"]["boundary_override"][
                "first_segment_boundary_at1"
            ],
            "rescued",
        )
        self.assertIsNone(
            boundary["runtime_features"]["hazkey_zenzai_evidence"][
                "candidate_score_margin"
            ]
        )

    def test_v6_contract_rejects_unknown_candidate_field(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            records = fixture.records(fixture.mozc_path)
            records[0]["candidates"][0]["unexpected"] = True
            fixture.replace_records(fixture.mozc_path, records)
            with self.assertRaisesRegex(ValueError, "fields differ"):
                fixture.evaluate()

    def test_score_requires_zenzai_ranking_influence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            records = fixture.records(fixture.hazkey_path)
            records[0]["candidates"][0]["ranking_influence"] = "standard"
            fixture.replace_records(fixture.hazkey_path, records)
            with self.assertRaisesRegex(ValueError, "requires zenzai"):
                fixture.evaluate()

    def test_score_requires_complete_scope_and_token_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            records = fixture.records(fixture.hazkey_path)
            records[0]["candidates"][0]["zenzai_score_scope"] = None
            fixture.replace_records(fixture.hazkey_path, records)
            with self.assertRaisesRegex(ValueError, "all null or all present"):
                fixture.evaluate()

    def test_enabled_zenzai_run_requires_an_observed_pass_score(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            records = fixture.records(fixture.hazkey_path)
            for record in records:
                for candidate in record["candidates"]:
                    candidate["zenzai_score"] = None
                    candidate["zenzai_score_token_count"] = None
                    candidate["zenzai_score_scope"] = None
            fixture.replace_records(fixture.hazkey_path, records)
            with self.assertRaisesRegex(ValueError, "no observed candidate pass score"):
                fixture.evaluate()

    def test_enabled_zenzai_run_requires_single_iteration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            for path in (fixture.mozc_path, fixture.hazkey_path):
                records = fixture.records(path)
                for record in records:
                    record["measurement"]["iterations"] = 2
                    record["measurement"]["latency_ms"] = {
                        "median": 1.5,
                        "p95": 2.0,
                        "minimum": 1.0,
                        "maximum": 2.0,
                        "samples": [1.0, 2.0],
                    }
                fixture.replace_records(path, records)
            with self.assertRaisesRegex(
                ValueError, "iterations must be 1 when Zenzai is enabled"
            ):
                fixture.evaluate()

    def test_paired_producer_drift_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            records = fixture.records(fixture.hazkey_path)
            records[0]["producer"]["sha256"] = "sha256:" + "f" * 64
            records[1]["producer"]["sha256"] = "sha256:" + "f" * 64
            records[2]["producer"]["sha256"] = "sha256:" + "f" * 64
            fixture.replace_records(fixture.hazkey_path, records)
            with self.assertRaisesRegex(ValueError, "producer differs"):
                fixture.evaluate()

    def test_h0_asserts_when_shared_merge_changes_mozc_top1(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            with mock.patch.object(
                evaluator.hybrid,
                "_merge_boundary_aware_candidate_records",
                side_effect=lambda hazkey, mozc, *_args, **_kwargs: (
                    hazkey or mozc,
                    {"reason": "corrupt-test-merge"},
                ),
            ):
                with self.assertRaisesRegex(AssertionError, "Top-1"):
                    fixture.evaluate()

    def test_h0_can_fall_back_to_hazkey_when_mozc_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            records = fixture.records(fixture.mozc_path)
            records[2]["candidates"] = []
            fixture.replace_records(fixture.mozc_path, records)
            report = fixture.evaluate()

        case = next(
            value for value in report["cases"] if value["id"] == "boundary-rescue"
        )
        systems = case["gold_outcomes"]["systems"]
        self.assertFalse(
            systems[evaluator.MOZC_SYSTEM]["first_segment_boundary"]["at1"]
        )
        self.assertTrue(
            systems[evaluator.H0_SYSTEM]["first_segment_boundary"]["at1"]
        )


if __name__ == "__main__":
    unittest.main()
