from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from tools.dictionary import compile_mozc_acceptable_path_evaluation as compiler
from tools.dictionary import evaluate_mozc_acceptable_path_boundaries as evaluator


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def pretty_json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()


def jsonl_bytes(values: list[dict[str, object]]) -> bytes:
    return b"".join(json_bytes(value) for value in values)


def sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def span(count: int) -> dict[str, object]:
    return {"start": 0, "count": count, "unit": "composition_element"}


TARGETS: list[dict[str, object]] = [
    {
        "schema": evaluator.TARGET_SCHEMA,
        "id": "both",
        "category": "proper-noun",
        "reading": "あいう",
        "acceptable_first_spans": [span(2), span(3)],
        "surface_evaluation_status": "fully_aligned",
        "acceptable_first_chunks": [
            {"span": span(2), "surface": "愛"},
            {"span": span(3), "surface": "愛雨"},
        ],
        "path_counts": {"acceptable": 2, "aligned": 2, "reading_only": 0},
    },
    {
        "schema": evaluator.TARGET_SCHEMA,
        "id": "mozc-only",
        "category": "homophone-context",
        "reading": "かきく",
        "acceptable_first_spans": [span(2)],
        "surface_evaluation_status": "partially_aligned",
        "acceptable_first_chunks": [{"span": span(2), "surface": "柿"}],
        "path_counts": {"acceptable": 2, "aligned": 1, "reading_only": 1},
    },
    {
        "schema": evaluator.TARGET_SCHEMA,
        "id": "hazkey-only",
        "category": "long-structural",
        "reading": "さしす",
        "acceptable_first_spans": [span(2)],
        "surface_evaluation_status": "not_aligned",
        "acceptable_first_chunks": [],
        "path_counts": {"acceptable": 1, "aligned": 0, "reading_only": 1},
    },
    {
        "schema": evaluator.TARGET_SCHEMA,
        "id": "between",
        "category": "colloquial",
        "reading": "たちつてと",
        "acceptable_first_spans": [span(2), span(4)],
        "surface_evaluation_status": "fully_aligned",
        "acceptable_first_chunks": [
            {"span": span(2), "surface": "立"},
            {"span": span(4), "surface": "立つ手"},
        ],
        "path_counts": {"acceptable": 2, "aligned": 2, "reading_only": 0},
    },
    {
        "schema": evaluator.TARGET_SCHEMA,
        "id": "missing",
        "category": "protected",
        "reading": "なにぬ",
        "acceptable_first_spans": [span(2)],
        "surface_evaluation_status": "fully_aligned",
        "acceptable_first_chunks": [{"span": span(2), "surface": "何"}],
        "path_counts": {"acceptable": 1, "aligned": 1, "reading_only": 0},
    },
]


CANDIDATES = {
    "both": {
        "hazkey": [("愛", 2), ("別", 1)],
        "mozc": [("誤", 3), ("愛雨", 3)],
    },
    "mozc-only": {
        "hazkey": [("下", 1)],
        "mozc": [("柿", 2)],
    },
    "hazkey-only": {
        "hazkey": [("差", 2)],
        "mozc": [("先", 3)],
    },
    "between": {
        "hazkey": [("中", 3), ("立つ手", 4)],
        "mozc": [("後", 5)],
    },
    "missing": {
        "hazkey": [],
        "mozc": [("前", 1)],
    },
}


def abprobe_result(
    target: dict[str, object],
    converter: str,
    candidates: list[tuple[str, int]],
    probe_hash: str,
    total: int,
) -> dict[str, object]:
    is_hazkey = converter == "hazkey"
    return {
        "schema": "hazkey.ab-probe-result.v5",
        "conversion_path": "segment_candidates",
        "id": target["id"],
        "reading": target["reading"],
        "category": target["category"],
        "backend": "Hazkey" if is_hazkey else "Mozc",
        "backend_version": "acceptable-path-test-v1",
        "converter_backend": converter,
        "source_ref": "a" * 40,
        "resource": {
            "kind": "hazkey_dictionary" if is_hazkey else "mozc_runtime_inputs",
            "path": "/fixture/hazkey" if is_hazkey else "/fixture/mozc",
            "fingerprint": "sha256:" + ("b" if is_hazkey else "c") * 64,
        },
        "top_k": 10,
        "corpus": {"sha256": probe_hash, "cases": total},
        "candidates": [
            {"text": text, "rank": rank, "consuming_count": count}
            for rank, (text, count) in enumerate(candidates, 1)
        ],
        "composition_span": span(len(str(target["reading"]))),
        "measurement": {
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
        },
    }


SOURCE_PATH_SPECS: dict[
    str, tuple[str, list[tuple[int, int | None]]]
] = {
    "both": ("愛雨", [(2, 1), (3, 2)]),
    "mozc-only": ("柿木", [(2, 1), (2, None)]),
    "hazkey-only": ("差", [(2, None)]),
    "between": ("立つ手と", [(2, 1), (4, 3)]),
    "missing": ("何か", [(2, 1)]),
}


def compiler_source_record(target: dict[str, object]) -> dict[str, object]:
    case_id = str(target["id"])
    reading = str(target["reading"])
    surface, path_specs = SOURCE_PATH_SPECS[case_id]
    acceptable_paths = []
    for index, (reading_end, surface_end) in enumerate(path_specs, 1):
        acceptable_paths.append(
            {
                "path_id": f"path-{index}",
                "status": "acceptable",
                "surface_reference_id": "surface-0",
                "reading_boundaries": (
                    [] if reading_end == len(reading) else [reading_end]
                ),
                "surface_boundaries": (
                    None
                    if surface_end is None
                    else ([] if surface_end == len(surface) else [surface_end])
                ),
                "alignment_status": (
                    "reading_only" if surface_end is None else "aligned"
                ),
                "provenance": {"kind": "human"},
            }
        )
    return {
        "schema": compiler.ANNOTATION_EXPORT_SCHEMA,
        "id": case_id,
        "category": target["category"],
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
        "acceptable_paths": acceptable_paths,
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


def replace_case_candidates(
    path: Path, case_id: str, candidates: list[tuple[str, int]]
) -> None:
    records = [json.loads(line) for line in path.read_text().splitlines()]
    record = next(record for record in records if record["id"] == case_id)
    record["candidates"] = [
        {"text": text, "rank": rank, "consuming_count": count}
        for rank, (text, count) in enumerate(candidates, 1)
    ]
    path.write_bytes(jsonl_bytes(records))


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
        self.hazkey_path = root / "hazkey-v5.jsonl"
        self.mozc_path = root / "mozc-v5.jsonl"
        self.write_generation()
        self.write_runs()

    def write_generation(self) -> None:
        reviewed = [compiler_source_record(target) for target in TARGETS]
        reviewed_bytes = jsonl_bytes(reviewed)
        annotation = {
            "schema": compiler.ANNOTATION_MANIFEST_SCHEMA,
            "queue_sha256": "sha256:" + "1" * 64,
            "workbook_sha256": "sha256:" + "4" * 64,
            "reviewed_paths_sha256": sha256(reviewed_bytes),
            "cases": len(TARGETS),
            "path_set_statuses": {"closed": len(TARGETS)},
            "complete": True,
            "formal_authorized": False,
            "diagnostic_only": True,
        }
        annotation_bytes = json_bytes(annotation)
        generated = compiler.prepare_outputs_bytes(
            reviewed_paths_data=reviewed_bytes,
            annotation_manifest_data=annotation_bytes,
        )
        self.generation.mkdir()
        for name, data in generated.items():
            (self.generation / name).write_bytes(data)
        self.targets = [
            json.loads(line)
            for line in self.targets_path.read_text().splitlines()
        ]
        if self.targets != TARGETS:
            raise AssertionError("test source records do not compile to TARGETS")

    def result(self, target: dict[str, object], converter: str, probe_hash: str) -> dict[str, object]:
        candidates = CANDIDATES[str(target["id"])][converter]
        return abprobe_result(
            target, converter, candidates, probe_hash, len(self.targets)
        )

    def write_runs(self) -> None:
        probe_hash = sha256(self.probe_path.read_bytes())
        self.hazkey_path.write_bytes(jsonl_bytes([self.result(target, "hazkey", probe_hash) for target in self.targets]))
        self.mozc_path.write_bytes(jsonl_bytes([self.result(target, "mozc", probe_hash) for target in self.targets]))

    def evaluate(self) -> dict[str, object]:
        return evaluator.evaluate(
            self.manifest_path,
            self.targets_path,
            self.hazkey_path,
            self.mozc_path,
        )


class AcceptablePathBoundaryEvaluationTests(unittest.TestCase):
    def test_consumes_exact_compiler_generation(self) -> None:
        source_record = {
            "schema": compiler.ANNOTATION_EXPORT_SCHEMA,
            "id": "both",
            "category": "proper-noun",
            "source": {
                "queue_sha256": "sha256:" + "1" * 64,
                "corpus_sha256": "sha256:" + "2" * 64,
                "row_sha256": "sha256:" + "3" * 64,
                "reading": "あいう",
                "annotation_reading": "あいう",
                "reading_unit": compiler.SOURCE_READING_UNIT,
                "annotation_reading_unit": compiler.ANNOTATION_READING_UNIT,
                "surface_unit": compiler.SURFACE_UNIT,
                "surface_references": [{"id": "surface-0", "text": "愛雨"}],
            },
            "path_set_status": "closed",
            "needs_adjudication": False,
            "path_units": {
                "reading_boundaries": compiler.ANNOTATION_READING_UNIT,
                "surface_boundaries": compiler.SURFACE_UNIT,
            },
            "acceptable_paths": [
                {
                    "path_id": "short",
                    "status": "acceptable",
                    "surface_reference_id": "surface-0",
                    "reading_boundaries": [2],
                    "surface_boundaries": [1],
                    "alignment_status": "aligned",
                    "provenance": {"kind": "human"},
                },
                {
                    "path_id": "full",
                    "status": "acceptable",
                    "surface_reference_id": "surface-0",
                    "reading_boundaries": [],
                    "surface_boundaries": [],
                    "alignment_status": "aligned",
                    "provenance": {"kind": "human"},
                },
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
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reviewed_path = root / "source-reviewed.jsonl"
            annotation_path = root / "source-manifest.json"
            reviewed_bytes = jsonl_bytes([source_record])
            reviewed_path.write_bytes(reviewed_bytes)
            annotation_path.write_bytes(
                json_bytes(
                    {
                        "schema": compiler.ANNOTATION_MANIFEST_SCHEMA,
                        "queue_sha256": "sha256:" + "1" * 64,
                        "workbook_sha256": "sha256:" + "4" * 64,
                        "reviewed_paths_sha256": sha256(reviewed_bytes),
                        "cases": 1,
                        "path_set_statuses": {"closed": 1},
                        "complete": True,
                        "formal_authorized": False,
                        "diagnostic_only": True,
                    }
                )
            )
            generated = compiler.prepare_outputs(
                reviewed_paths_path=reviewed_path,
                annotation_manifest_path=annotation_path,
            )
            generation = root / "generation"
            generation.mkdir()
            for name, data in generated.items():
                (generation / name).write_bytes(data)
            target = json.loads(
                generated[compiler.TARGETS_NAME].decode().splitlines()[0]
            )
            probe_hash = sha256(generated[compiler.PROBE_INPUT_NAME])
            hazkey_path = root / "hazkey.jsonl"
            mozc_path = root / "mozc.jsonl"
            hazkey_path.write_bytes(
                jsonl_bytes(
                    [abprobe_result(target, "hazkey", [("愛", 2)], probe_hash, 1)]
                )
            )
            mozc_path.write_bytes(
                jsonl_bytes(
                    [abprobe_result(target, "mozc", [("愛雨", 3)], probe_hash, 1)]
                )
            )
            report = evaluator.evaluate(
                generation / compiler.MANIFEST_NAME,
                generation / compiler.TARGETS_NAME,
                hazkey_path,
                mozc_path,
            )

        self.assertEqual(
            report["all_cases"]["top1_first_segment_boundary_groups"]["both"],
            1,
        )
        self.assertEqual(
            [value["count"] for value in report["cases"][0]["acceptable_first_spans"]],
            [2, 3],
        )

    def test_reports_set_boundary_and_surface_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            report = fixture.evaluate()

        self.assertTrue(report["diagnostic_only"])
        self.assertFalse(report["formal_authorized"])
        self.assertEqual(
            report["evaluation_scope"],
            {
                "first_segment_boundary_scope": "first-segment-only",
                "full_segmentation_path_sequence_evaluated": False,
                "surface_scope": "fully-aligned-first-segment-pairs-only",
            },
        )
        all_cases = report["all_cases"]
        self.assertEqual(
            all_cases["top1_first_segment_boundary_groups"],
            {"both": 1, "mozc_only": 1, "hazkey_only": 1, "neither": 2},
        )
        self.assertEqual(
            all_cases["first_segment_boundary"]["hazkey"]["top1"][
                "first_segment_boundary_hits"
            ],
            2,
        )
        self.assertEqual(
            all_cases["first_segment_boundary"]["mozc"]["top1"][
                "first_segment_boundary_hits"
            ],
            2,
        )
        self.assertEqual(
            all_cases["first_segment_boundary"]["hazkey"]["top_k"][
                "first_segment_boundary_hits"
            ],
            3,
        )
        self.assertEqual(
            all_cases["first_segment_boundary"]["hazkey"]["top1"][
                "first_segment_boundary_error_positions"
            ],
            {
                "hit": 2,
                "missing": 1,
                "before_all": 1,
                "after_all": 0,
                "between_alternatives": 1,
                "mixed_sides": 0,
            },
        )
        self.assertEqual(
            all_cases["surface_evaluation_coverage"],
            {"fully_aligned": 3, "partially_aligned_excluded": 1, "not_aligned_excluded": 1},
        )
        hazkey_surface = all_cases["surface"]["hazkey"]
        self.assertEqual(
            hazkey_surface["top1"][
                "conditional_surface_given_acceptable_first_segment_boundary"
            ]["cases"],
            1,
        )
        self.assertEqual(hazkey_surface["top1"]["end_to_end"], {"hits": 1, "cases": 3, "accuracy": 1 / 3})
        self.assertEqual(hazkey_surface["top_k"]["end_to_end"], {"hits": 2, "cases": 3, "accuracy": 2 / 3})
        self.assertEqual(report["formal_quality"]["cases"], 4)

    def test_multiple_acceptable_spans_are_not_collapsed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            report = fixture.evaluate()
        both = next(case for case in report["cases"] if case["id"] == "both")
        self.assertEqual([value["count"] for value in both["acceptable_first_spans"]], [2, 3])
        self.assertTrue(
            both["backends"]["hazkey"]["top1"][
                "first_segment_boundary_hit"
            ]
        )
        self.assertTrue(
            both["backends"]["mozc"]["top1"][
                "first_segment_boundary_hit"
            ]
        )
        between = next(case for case in report["cases"] if case["id"] == "between")
        self.assertEqual(
            between["backends"]["hazkey"]["top1"][
                "first_segment_boundary_error_position"
            ],
            "between_alternatives",
        )
        self.assertEqual(
            between["backends"]["hazkey"]["top1"][
                "minimum_absolute_first_segment_boundary_element_distance"
            ],
            1,
        )

    def test_rejects_target_binding_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            fixture.targets_path.write_bytes(fixture.targets_path.read_bytes() + b"\n")
            with self.assertRaisesRegex(ValueError, "bound targets SHA-256 mismatch"):
                fixture.evaluate()

    def test_rejects_paired_composition_span_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            records = [json.loads(line) for line in fixture.mozc_path.read_text().splitlines()]
            records[0]["composition_span"]["count"] = 4
            fixture.mozc_path.write_bytes(jsonl_bytes(records))
            with self.assertRaisesRegex(ValueError, "composition_span mismatch"):
                fixture.evaluate()

    def test_rejects_partially_aligned_target_marked_fully_aligned(self) -> None:
        tampered = json.loads(json.dumps(TARGETS[1], ensure_ascii=False))
        tampered["surface_evaluation_status"] = "fully_aligned"
        with self.assertRaisesRegex(
            ValueError, "surface_evaluation_status conflicts"
        ):
            evaluator._validate_targets(jsonl_bytes([tampered]), "tampered")

    def test_rejects_unexpected_abprobe_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            records = [json.loads(line) for line in fixture.hazkey_path.read_text().splitlines()]
            records[0]["unexpected"] = True
            fixture.hazkey_path.write_bytes(jsonl_bytes(records))
            with self.assertRaisesRegex(ValueError, "fields differ"):
                fixture.evaluate()

    def test_rejects_paired_metadata_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            records = [
                json.loads(line) for line in fixture.mozc_path.read_text().splitlines()
            ]
            for record in records:
                record["source_ref"] = "d" * 40
            fixture.mozc_path.write_bytes(jsonl_bytes(records))
            with self.assertRaisesRegex(ValueError, "metadata source_ref differs"):
                fixture.evaluate()

    def test_accepts_experiment_backend_display_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            for path, display_name in (
                (fixture.hazkey_path, "hazkey"),
                (fixture.mozc_path, "B0"),
            ):
                records = [
                    json.loads(line) for line in path.read_text().splitlines()
                ]
                for record in records:
                    record["backend"] = display_name
                path.write_bytes(jsonl_bytes(records))
            report = fixture.evaluate()

        self.assertEqual(report["all_cases"]["cases"], len(TARGETS))

    def test_rejects_candidate_count_past_composition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            records = [
                json.loads(line)
                for line in fixture.hazkey_path.read_text().splitlines()
            ]
            records[0]["candidates"][0]["consuming_count"] = 4
            fixture.hazkey_path.write_bytes(jsonl_bytes(records))
            with self.assertRaisesRegex(ValueError, "must not exceed composition_span.count"):
                fixture.evaluate()

    def test_rederives_targets_from_reviewed_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            records = [
                json.loads(line)
                for line in fixture.targets_path.read_text().splitlines()
            ]
            records[0]["acceptable_first_chunks"][0]["surface"] = "偽"
            tampered_targets = jsonl_bytes(records)
            fixture.targets_path.write_bytes(tampered_targets)
            manifest = json.loads(fixture.manifest_path.read_text())
            manifest["bindings"]["targets"]["sha256"] = sha256(tampered_targets)
            fixture.manifest_path.write_bytes(pretty_json_bytes(manifest))
            with self.assertRaisesRegex(
                ValueError, "targets.jsonl is not exactly derived"
            ):
                fixture.evaluate()

    def test_rejects_nonfixed_binding_paths(self) -> None:
        for bad_path in ("/tmp/targets.jsonl", "../targets.jsonl"):
            with self.subTest(path=bad_path), tempfile.TemporaryDirectory() as directory:
                fixture = Fixture(Path(directory))
                manifest = json.loads(fixture.manifest_path.read_text())
                manifest["bindings"]["targets"]["path"] = bad_path
                fixture.manifest_path.write_bytes(pretty_json_bytes(manifest))
                with self.assertRaisesRegex(
                    ValueError, "must be the fixed generation basename"
                ):
                    fixture.evaluate()

    def test_rejects_generation_file_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            external = fixture.root / "external-targets.jsonl"
            external.write_bytes(fixture.targets_path.read_bytes())
            fixture.targets_path.unlink()
            fixture.targets_path.symlink_to(external)
            with self.assertRaisesRegex(ValueError, "regular non-symlink file"):
                fixture.evaluate()

    def test_rejects_generation_ancestor_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real = root / "real"
            real.mkdir()
            fixture = Fixture(real)
            alias = root / "alias"
            alias.symlink_to(real, target_is_directory=True)
            with self.assertRaisesRegex(
                ValueError, "all ancestors must be non-symlink directories"
            ):
                evaluator.evaluate(
                    alias / "generation" / compiler.MANIFEST_NAME,
                    alias / "generation" / compiler.TARGETS_NAME,
                    fixture.hazkey_path,
                    fixture.mozc_path,
                )

    def test_rejects_supplied_run_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            external = fixture.root / "hazkey-real.jsonl"
            fixture.hazkey_path.rename(external)
            fixture.hazkey_path.symlink_to(external)
            with self.assertRaisesRegex(ValueError, "regular non-symlink file"):
                fixture.evaluate()

    def test_top_k_straddling_singleton_is_mixed_sides(self) -> None:
        self.assertEqual(evaluator._error_position([1, 3], {2}), "mixed_sides")
        self.assertEqual(
            evaluator._error_position([3], {2, 4}), "between_alternatives"
        )

    def test_structured_h1_promotes_and_reports_rescue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            replace_case_candidates(fixture.hazkey_path, "both", [("愛", 2)])
            replace_case_candidates(
                fixture.mozc_path, "both", [("誤", 2), ("愛", 2)]
            )
            report = fixture.evaluate()

        case = next(case for case in report["cases"] if case["id"] == "both")
        self.assertEqual(
            case["policies"]["runtime_h0"]["top1_candidate"]["text"], "誤"
        )
        self.assertEqual(
            case["policies"]["diagnostic_h1"]["decision"],
            evaluator.hybrid.PROMOTION_DECISION,
        )
        self.assertEqual(
            case["policies"]["diagnostic_h1"]["top1_candidate"]["text"],
            "愛",
        )
        policies = report["all_cases"]["structured_merge_policies"]
        self.assertEqual(
            policies["diagnostic_h1"]["deltas_vs_runtime_h0"]["surface"][
                "top1"
            ]["end_to_end"]["rescued"],
            1,
        )
        self.assertEqual(
            policies["diagnostic_h1"]["deltas_vs_runtime_h0"][
                "first_segment_boundary"
            ]["top1"]["net"],
            0,
        )

    def test_structured_h2_suppresses_width_only_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            replace_case_candidates(
                fixture.hazkey_path, "mozc-only", [("4月", 2)]
            )
            replace_case_candidates(
                fixture.mozc_path,
                "mozc-only",
                [("４月", 2), ("4月", 2)],
            )
            report = fixture.evaluate()

        case = next(
            case for case in report["cases"] if case["id"] == "mozc-only"
        )
        self.assertEqual(
            case["policies"]["diagnostic_h1"]["decision"],
            evaluator.hybrid.PROMOTION_DECISION,
        )
        self.assertEqual(
            case["policies"]["diagnostic_h1"]["top1_candidate"]["text"],
            "4月",
        )
        self.assertEqual(
            case["policies"]["diagnostic_h2"]["decision"],
            evaluator.hybrid.WIDTH_EQUIVALENT_DECISION,
        )
        self.assertEqual(
            case["policies"]["diagnostic_h2"]["top1_candidate"]["text"],
            "４月",
        )
        self.assertEqual(
            case["policies"]["diagnostic_h2"]["top1_candidate"][
                "consuming_count"
            ],
            case["policies"]["runtime_h0"]["top1_candidate"][
                "consuming_count"
            ],
        )


if __name__ == "__main__":
    unittest.main()
