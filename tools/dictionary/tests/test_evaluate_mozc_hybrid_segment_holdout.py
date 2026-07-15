from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from tools.dictionary import build_mozc_hybrid_segment_holdout_v1 as holdout
from tools.dictionary import evaluate_mozc_hybrid_segment_holdout as evaluator


def render_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def render_jsonl(values: list[dict[str, object]]) -> bytes:
    return b"".join(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
        for value in values
    )


def elements(reading: str) -> list[dict[str, str]]:
    return [{"text": character, "input_style": "direct"} for character in reading]


def case(
    case_id: str,
    reading: str,
    category: str,
    surface: str,
    count: int,
) -> dict[str, object]:
    return {
        "schema": holdout.CASE_SCHEMA,
        "id": case_id,
        "category": category,
        "family_id": "family-" + case_id,
        "elements": elements(reading),
        "target": {
            "span": {
                "start": 0,
                "count": count,
                "unit": holdout.COMPOSITION_ELEMENT_UNIT,
            },
            "surfaces": [surface],
        },
    }


CASES = [
    case("prefix-rescue", "よみあ", "homophone-context", "正解R", 2),
    case("boundary-error", "よみい", "homophone-context", "正解B", 3),
    case("same-surface-wrong-count", "よみう", "proper-noun", "同じ", 3),
    case("width-regression", "しがつ", "width-orthography", "４月", 3),
    case("mozc-empty", "よみ", "proper-noun", "正解E", 2),
    case("raw-exact", "ええ", "proper-noun", "é", 2),
    case("unchanged-correct", "ただ", "proper-noun", "正解U", 2),
    case("protected-diagnostic", "ほご", "protected", "保護", 2),
]


CANDIDATES = {
    "prefix-rescue": {
        "hazkey": [("正解R", 2), ("H-R", 2)],
        "mozc": [("誤りR", 2), ("正解R", 2)],
    },
    "boundary-error": {
        "hazkey": [("正解B", 3)],
        "mozc": [("誤りB", 2)],
    },
    "same-surface-wrong-count": {
        "hazkey": [("同じ", 2)],
        "mozc": [("同じ", 2)],
    },
    "width-regression": {
        "hazkey": [("4月", 3)],
        "mozc": [("４月", 3), ("4月", 3)],
    },
    "mozc-empty": {
        "hazkey": [("正解E", 2)],
        "mozc": [],
    },
    "raw-exact": {
        "hazkey": [("別", 2)],
        "mozc": [("e\u0301", 2)],
    },
    "unchanged-correct": {
        "hazkey": [("別U", 2)],
        "mozc": [("正解U", 2)],
    },
    "protected-diagnostic": {
        "hazkey": [("別P", 2)],
        "mozc": [("保護", 2)],
    },
}


def policy_freeze() -> dict[str, object]:
    return {
        "h0_policy_id": holdout.H0_POLICY_ID,
        "h1_policy_id": holdout.H1_POLICY_ID,
        "h2_policy_id": holdout.H2_POLICY_ID,
        "product_source_revision": "a" * 40,
        "evaluator_sha256": (
            "sha256:"
            + hashlib.sha256(Path(evaluator.__file__).read_bytes()).hexdigest()
        ),
        "hybrid_evaluator_sha256": (
            "sha256:"
            + hashlib.sha256(
                Path(evaluator.hybrid.__file__).read_bytes()
            ).hexdigest()
        ),
        "abprobe_executable_sha256": "sha256:" + "c" * 64,
        "hazkey_resource_fingerprint": "sha256:" + "d" * 64,
        "mozc_resource_fingerprint": "sha256:" + "f" * 64,
        "mozc_bundle_generation": "sha256-" + "e" * 64,
        "top_k": 10,
        "warmups": 0,
        "iterations": 1,
        "learning_enabled": False,
    }


def result(
    source_case: dict[str, object],
    backend: str,
    candidates: list[tuple[str, int]],
    probe_sha256: str,
    total: int,
    *,
    top_k: int = 10,
) -> dict[str, object]:
    reading = "".join(element["text"] for element in source_case["elements"])
    is_hazkey = backend == "hazkey"
    return {
        "schema": "hazkey.ab-probe-result.v5",
        "conversion_path": "segment_candidates",
        "id": source_case["id"],
        "reading": reading,
        "category": source_case["category"],
        "backend": "Hazkey" if is_hazkey else "Mozc",
        "backend_version": "segment-holdout-test-v1",
        "converter_backend": backend,
        "source_ref": "a" * 40,
        "resource": {
            "kind": "hazkey_dictionary" if is_hazkey else "mozc_runtime_inputs",
            "path": (
                "/fixture/hazkey-dictionary"
                if is_hazkey
                else "/fixture/" + "sha256-" + "e" * 64
            ),
            "fingerprint": (
                "sha256:" + "d" * 64 if is_hazkey else "sha256:" + "f" * 64
            ),
        },
        "top_k": top_k,
        "corpus": {"sha256": probe_sha256, "cases": total},
        "candidates": [
            {"text": text, "rank": rank, "consuming_count": count}
            for rank, (text, count) in enumerate(candidates, 1)
        ],
        "composition_span": {
            "start": 0,
            "count": len(source_case["elements"]),
            "unit": "composition_element",
        },
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


class SegmentEvaluationFixture:
    def __init__(
        self,
        root: Path,
        *,
        result_top_k: int = 10,
        minimum_h2_promotion_opportunities: int = 1,
    ) -> None:
        self.root = root
        self.cases = copy.deepcopy(CASES)
        cases_bytes = render_jsonl(self.cases)
        approval = {
            "schema": holdout.APPROVAL_SCHEMA,
            "status": "approved",
            "holdout_id": "segment-holdout-evaluator-test",
            "source_cases_sha256": holdout.sha256_bytes(cases_bytes),
            "author_id": "segment-author",
            "reviewer_id": "independent-reviewer",
            "quality_categories": {
                "homophone-context": 2,
                "proper-noun": 4,
                "width-orthography": 1,
            },
            "minimum_h2_promotion_opportunities": (
                minimum_h2_promotion_opportunities
            ),
            "attestation": dict(holdout.ATTESTATION_CONTRACT),
            "policy_freeze": policy_freeze(),
        }
        cases_path = root / "reviewed-cases.jsonl"
        approval_path = root / "review-approval.json"
        cases_path.write_bytes(cases_bytes)
        approval_path.write_bytes(render_json(approval))
        _, self.generation = holdout.seal(
            cases_path=cases_path,
            approval_path=approval_path,
            output_root=root,
        )
        probe_bytes = (self.generation / holdout.PROBE_INPUT_NAME).read_bytes()
        probe_sha256 = holdout.sha256_bytes(probe_bytes)
        self.hazkey_records = [
            result(
                source_case,
                "hazkey",
                CANDIDATES[source_case["id"]]["hazkey"],
                probe_sha256,
                len(self.cases),
                top_k=result_top_k,
            )
            for source_case in self.cases
        ]
        self.mozc_records = [
            result(
                source_case,
                "mozc",
                CANDIDATES[source_case["id"]]["mozc"],
                probe_sha256,
                len(self.cases),
                top_k=result_top_k,
            )
            for source_case in self.cases
        ]
        self.hazkey_path = root / "hazkey.jsonl"
        self.mozc_path = root / "mozc.jsonl"
        self.write_results()

    def write_results(self) -> None:
        self.hazkey_path.write_bytes(render_jsonl(self.hazkey_records))
        self.mozc_path.write_bytes(render_jsonl(self.mozc_records))


class MozcHybridSegmentHoldoutEvaluationTests(unittest.TestCase):
    def test_jsonl_uses_only_lf_as_a_physical_record_separator(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture = SegmentEvaluationFixture(Path(temporary_directory))
            candidate_text = "line\u2028separator\u2029paragraph"
            fixture.hazkey_records[0]["candidates"][1]["text"] = candidate_text
            fixture.write_results()
            self.assertIn(candidate_text.encode("utf-8"), fixture.hazkey_path.read_bytes())

            report = evaluator.evaluate_generation(
                fixture.generation,
                fixture.hazkey_path,
                fixture.mozc_path,
            )

        self.assertEqual(len(report["cases"]), len(CASES))

    def test_scores_reviewed_prefix_targets_for_h0_h1_h2(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture = SegmentEvaluationFixture(Path(temporary_directory))
            report = evaluator.evaluate_generation(
                fixture.generation,
                fixture.hazkey_path,
                fixture.mozc_path,
            )

        self.assertEqual(report["schema"], evaluator.OUTPUT_SCHEMA)
        self.assertFalse(report["formal_authorized"])
        self.assertFalse(report["new_holdout_required"])
        self.assertFalse(report["policy"]["uses_expected_labels"])
        self.assertEqual(
            report["target_comparability"],
            {
                "quality_target": "first_reviewed_segment",
                "observed_candidate_scope": "first_clause",
                "established": True,
                "comparable_count": 8,
                "incomparable_count": 0,
                "comparison_basis": "reviewed_segment_label",
                "partial_parity_established": True,
                "selection_basis": "corpus_label_not_backend_output",
                "selection_biased": False,
                "reviewed_target_metadata": {
                    "label_schema": holdout.SEGMENT_LABEL_SCHEMA,
                    "labels_sha256": report["inputs"]["segment_labels"]["sha256"],
                    "bindings_sha256": report["inputs"]["binding"]["sha256"],
                    "match": (
                        "raw-exact-NFC-label-surface-and-"
                        "composition-element-count.v1"
                    ),
                },
                "required_evidence": None,
            },
        )
        self.assertEqual(
            report["promotion_opportunities"]["outcome_incomparable_count"], 0
        )
        self.assertEqual(
            report["width_guarded_promotion_opportunities"][
                "outcome_incomparable_count"
            ],
            0,
        )
        cases = {case["id"]: case for case in report["cases"]}
        self.assertEqual(cases["prefix-rescue"]["top1_outcome"], "rescued")
        self.assertEqual(
            cases["prefix-rescue"]["width_guarded_top1_outcome"], "rescued"
        )
        boundary = cases["boundary-error"]
        self.assertTrue(boundary["target_comparable"])
        self.assertFalse(
            boundary["boundary_evidence"][
                "reviewed_target_boundary_matches_mozc"
            ]
        )
        self.assertIsNone(boundary["expected_rank"]["mozc"])
        self.assertEqual(
            cases["same-surface-wrong-count"]["mozc_top1_miss_classification"],
            "both_absent",
        )
        width = cases["width-regression"]
        self.assertEqual(width["top1_outcome"], "regressed")
        self.assertEqual(
            width["width_guarded_top1_outcome"], "unchanged_correct"
        )
        self.assertEqual(cases["mozc-empty"]["expected_rank"]["runtime_h0"], 1)
        self.assertIsNone(cases["raw-exact"]["expected_rank"]["mozc"])

        self.assertEqual(report["diagnostic_target_comparable"]["top1"]["cases"], 8)
        self.assertEqual(report["formal_quality"]["top1"]["cases"], 7)
        self.assertEqual(
            report["formal_quality"]["category_policy"]["included_categories"],
            ["homophone-context", "proper-noun", "width-orthography"],
        )
        self.assertEqual(
            report["formal_quality"]["category_policy"][
                "excluded_categories_observed"
            ],
            ["protected"],
        )
        diagnostic_decomposition = report["diagnostic_target_comparable"][
            "quality_decomposition"
        ]
        formal_decomposition = report["formal_quality"][
            "quality_decomposition"
        ]
        self.assertEqual(
            diagnostic_decomposition["metric_contract"][
                "primary_product_metric"
            ],
            "end_to_end_top1",
        )
        self.assertEqual(
            set(diagnostic_decomposition["systems"]),
            {
                "hazkey",
                "mozc",
                "runtime_h0",
                "h1_hybrid",
                "h2_width_guarded",
            },
        )
        rank_keys = {
            "hazkey": "hazkey",
            "mozc": "mozc",
            "runtime_h0": "runtime_h0",
            "h1_hybrid": "hybrid",
            "h2_width_guarded": "width_guarded_hybrid",
        }
        formal_categories = {
            "homophone-context",
            "proper-noun",
            "width-orthography",
        }
        for view, scoped_cases in (
            (diagnostic_decomposition, list(cases.values())),
            (
                formal_decomposition,
                [
                    case
                    for case in cases.values()
                    if case["category"] in formal_categories
                ],
            ),
        ):
            with self.subTest(view_cases=view["cases"]):
                groups = view["hazkey_mozc_top1_boundary_comparison"][
                    "groups"
                ]
                self.assertEqual(sum(groups.values()), view["cases"])
                for system, rank_key in rank_keys.items():
                    system_metrics = view["systems"][system]["top1"]
                    expected_hits = sum(
                        case["expected_rank"][rank_key] == 1
                        for case in scoped_cases
                    )
                    self.assertEqual(
                        system_metrics["end_to_end"]["hits"], expected_hits
                    )
                    self.assertEqual(
                        system_metrics[
                            "raw_exact_surface_given_boundary_correct"
                        ]["hits"],
                        system_metrics["end_to_end"]["hits"],
                    )
                invariant = view["boundary_preservation_invariant"]
                self.assertEqual(
                    invariant["observed_changed_count"],
                    {
                        "runtime_h0": 0,
                        "h1_hybrid": 0,
                        "h2_width_guarded": 0,
                    },
                )
                for policy in ("h1_hybrid", "h2_width_guarded"):
                    self.assertEqual(
                        view["policy_delta_vs_runtime_h0"][policy][
                            "top1_boundary_changes"
                        ]["count"],
                        0,
                    )

        mozc_boundary = diagnostic_decomposition["systems"]["mozc"][
            "top1"
        ]["boundary"]
        self.assertEqual(mozc_boundary["missing_candidate"], 1)
        self.assertEqual(
            mozc_boundary["ends_before_reviewed_boundary"]["segmentation"],
            "over_segmentation",
        )
        self.assertEqual(
            mozc_boundary["ends_before_reviewed_boundary"]["element_delta"],
            {
                "definition": (
                    "predicted_consuming_count - reviewed_consuming_count"
                ),
                "sum": -2,
                "absolute_sum": 2,
                "mean_absolute": 1.0,
                "minimum": -1,
                "maximum": -1,
            },
        )
        same_surface_quality = cases["same-surface-wrong-count"][
            "top1_quality"
        ]["systems"]["mozc"]["top1"]
        self.assertTrue(same_surface_quality["raw_exact_surface_correct"])
        self.assertFalse(same_surface_quality["end_to_end_correct"])
        self.assertEqual(
            same_surface_quality["boundary"]["classification"],
            "ends_before_reviewed_boundary",
        )
        boundary_comparison = diagnostic_decomposition[
            "hazkey_mozc_top1_boundary_comparison"
        ]
        self.assertEqual(
            boundary_comparison["groups"],
            {
                "both_correct": 5,
                "mozc_only": 0,
                "hazkey_only": 2,
                "neither": 1,
            },
        )
        self.assertEqual(
            boundary_comparison["boundary_switch_opportunity"]["count"], 1
        )
        self.assertEqual(
            boundary_comparison["actual_top1_rescueable"]["count"], 1
        )
        self.assertEqual(
            boundary_comparison[
                "mozc_top1_missing_hazkey_boundary_correct"
            ]["count"],
            1,
        )
        self.assertTrue(
            diagnostic_decomposition["systems"]["mozc"]["top_k"][
                "end_to_end"
            ]["secondary_candidate_coverage_metric"]
        )
        self.assertEqual(
            diagnostic_decomposition["policy_delta_vs_runtime_h0"][
                "h1_hybrid"
            ]["rescues"],
            {
                "total": 1,
                "boundary_caused": 0,
                "surface_within_same_boundary": 1,
            },
        )
        self.assertEqual(
            diagnostic_decomposition["policy_delta_vs_runtime_h0"][
                "h1_hybrid"
            ]["regressions"],
            {
                "total": 1,
                "boundary_caused": 0,
                "surface_within_same_boundary": 1,
            },
        )
        self.assertEqual(
            report["inputs"]["binding"]["authority"], "manifest.json.bindings"
        )
        self.assertEqual(
            report["inputs"]["probe_input"]["sha256"],
            report["inputs"]["corpus"]["sha256"],
        )
        self.assertEqual(
            report["artifact_identity"]["abprobe_executable"]["status"],
            "not_bound_by_probe_result",
        )
        self.assertEqual(
            report["artifact_identity"]["hybrid_evaluator"]["status"],
            "source_file_hash_match",
        )
        self.assertEqual(
            report["artifact_identity"]["hybrid_evaluator"][
                "loaded_code_identity"
            ],
            "not_attested",
        )
        self.assertEqual(
            report["artifact_identity"]["hazkey_resource"][
                "observed_fingerprint"
            ],
            "sha256:" + "d" * 64,
        )
        self.assertEqual(
            report["artifact_identity"]["mozc_resource"][
                "observed_fingerprint"
            ],
            "sha256:" + "f" * 64,
        )
        self.assertEqual(report["decision"]["status"], "inconclusive")
        self.assertEqual(
            report["decision"]["h2_promotion_opportunity_gate"],
            {
                "scope": (
                    "manifest_quality_categories_and_target_comparable_cases"
                ),
                "quality_categories": [
                    "homophone-context",
                    "proper-noun",
                    "width-orthography",
                ],
                "eligible_cases": 7,
                "observed": 1,
                "required": 1,
                "met": True,
            },
        )
        self.assertTrue(
            report["decision"]["production_policy"]["retained"]
        )
        self.assertNotIn(
            "human_collection_required", report["decision"]["blocking_reasons"]
        )
        self.assertIn(
            "backend_label_isolation_not_implemented",
            report["decision"]["blocking_reasons"],
        )
        self.assertIn(
            "evaluator_loaded_code_identity_not_attested",
            report["decision"]["blocking_reasons"],
        )

    def test_returns_inconclusive_report_when_h2_opportunity_minimum_is_not_met(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture = SegmentEvaluationFixture(
                Path(temporary_directory),
                minimum_h2_promotion_opportunities=2,
            )
            report = evaluator.evaluate_generation(
                fixture.generation,
                fixture.hazkey_path,
                fixture.mozc_path,
            )

        self.assertEqual(report["decision"]["status"], "inconclusive")
        self.assertEqual(
            report["decision"]["h2_promotion_opportunity_gate"],
            {
                "scope": (
                    "manifest_quality_categories_and_target_comparable_cases"
                ),
                "quality_categories": [
                    "homophone-context",
                    "proper-noun",
                    "width-orthography",
                ],
                "eligible_cases": 7,
                "observed": 1,
                "required": 2,
                "met": False,
            },
        )
        self.assertIn(
            "h2_promotion_opportunity_minimum_not_met",
            report["decision"]["blocking_reasons"],
        )
        self.assertFalse(report["formal_authorized"])
        self.assertEqual(
            report["runtime_policy"]["id"], "mozc-first-preserve-top1-h0"
        )

    def test_h2_gate_excludes_protected_only_promotion_opportunity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture = SegmentEvaluationFixture(Path(temporary_directory))
            hazkey_by_id = {
                record["id"]: record for record in fixture.hazkey_records
            }
            mozc_by_id = {record["id"]: record for record in fixture.mozc_records}
            mozc_by_id["prefix-rescue"]["candidates"] = [
                {"text": "誤りR", "rank": 1, "consuming_count": 2}
            ]
            hazkey_by_id["protected-diagnostic"]["candidates"] = [
                {"text": "保護", "rank": 1, "consuming_count": 2}
            ]
            mozc_by_id["protected-diagnostic"]["candidates"] = [
                {"text": "誤P", "rank": 1, "consuming_count": 2},
                {"text": "保護", "rank": 2, "consuming_count": 2},
            ]
            fixture.write_results()
            report = evaluator.evaluate_generation(
                fixture.generation,
                fixture.hazkey_path,
                fixture.mozc_path,
            )

        self.assertEqual(
            report["width_guarded_promotion_opportunities"]["count"], 1
        )
        gate = report["decision"]["h2_promotion_opportunity_gate"]
        self.assertEqual(gate["eligible_cases"], 7)
        self.assertEqual(gate["observed"], 0)
        self.assertFalse(gate["met"])
        self.assertIn(
            "h2_promotion_opportunity_minimum_not_met",
            report["decision"]["blocking_reasons"],
        )

    def test_rejects_mozc_resource_fingerprint_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture = SegmentEvaluationFixture(Path(temporary_directory))
            for record in fixture.mozc_records:
                record["resource"]["fingerprint"] = "sha256:" + "0" * 64
            fixture.write_results()
            with self.assertRaisesRegex(ValueError, "Mozc resource fingerprint"):
                evaluator.evaluate_generation(
                    fixture.generation,
                    fixture.hazkey_path,
                    fixture.mozc_path,
                )

    def test_rejects_unknown_abprobe_v5_fields_at_every_object_level(self) -> None:
        mutations = (
            ("root", lambda record: record.__setitem__("unexpected", True)),
            (
                "resource",
                lambda record: record["resource"].__setitem__(
                    "unexpected", True
                ),
            ),
            (
                "corpus",
                lambda record: record["corpus"].__setitem__("unexpected", True),
            ),
            (
                "candidate",
                lambda record: record["candidates"][0].__setitem__(
                    "unexpected", True
                ),
            ),
            (
                "composition span",
                lambda record: record["composition_span"].__setitem__(
                    "unexpected", True
                ),
            ),
            (
                "measurement",
                lambda record: record["measurement"].__setitem__(
                    "unexpected", True
                ),
            ),
            (
                "latency",
                lambda record: record["measurement"]["latency_ms"].__setitem__(
                    "unexpected", True
                ),
            ),
            (
                "rss",
                lambda record: record["measurement"]["rss"].__setitem__(
                    "unexpected", True
                ),
            ),
            (
                "backend diagnostics",
                lambda record: record["measurement"][
                    "backend_diagnostics"
                ].__setitem__("unexpected", True),
            ),
        )
        for label, mutate in mutations:
            with self.subTest(level=label), tempfile.TemporaryDirectory() as directory:
                fixture = SegmentEvaluationFixture(Path(directory))
                mutate(fixture.hazkey_records[0])
                fixture.write_results()
                with self.assertRaisesRegex(
                    ValueError, "fields differ.*unexpected"
                ):
                    evaluator.evaluate_generation(
                        fixture.generation,
                        fixture.hazkey_path,
                        fixture.mozc_path,
                    )

    def test_rejects_duplicate_abprobe_v5_json_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture = SegmentEvaluationFixture(Path(temporary_directory))
            lines = fixture.hazkey_path.read_text(encoding="utf-8").splitlines()
            lines[0] = (
                lines[0][:-1]
                + ',"schema":"hazkey.ab-probe-result.v5"}'
            )
            fixture.hazkey_path.write_text(
                "\n".join(lines) + "\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "duplicate JSON key 'schema'"):
                evaluator.evaluate_generation(
                    fixture.generation,
                    fixture.hazkey_path,
                    fixture.mozc_path,
                )

    def test_cli_writes_the_evaluation_only_after_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture = SegmentEvaluationFixture(Path(temporary_directory))
            output = fixture.root / "evaluation.json"
            status = evaluator.main(
                [
                    "--generation",
                    str(fixture.generation),
                    "--hazkey-results",
                    str(fixture.hazkey_path),
                    "--mozc-results",
                    str(fixture.mozc_path),
                    "--output",
                    str(output),
                ]
            )
            report = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(status, 0)
        self.assertEqual(report["schema"], evaluator.OUTPUT_SCHEMA)

    def test_rejects_result_order_policy_and_manifest_binding_mismatches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture = SegmentEvaluationFixture(Path(temporary_directory))
            fixture.mozc_records.reverse()
            fixture.write_results()
            with self.assertRaisesRegex(ValueError, "result order"):
                evaluator.evaluate_generation(
                    fixture.generation,
                    fixture.hazkey_path,
                    fixture.mozc_path,
                )

        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture = SegmentEvaluationFixture(
                Path(temporary_directory), result_top_k=9
            )
            with self.assertRaisesRegex(ValueError, "top_k does not match policy freeze"):
                evaluator.evaluate_generation(
                    fixture.generation,
                    fixture.hazkey_path,
                    fixture.mozc_path,
                )

        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture = SegmentEvaluationFixture(Path(temporary_directory))
            blobs = {
                name: (fixture.generation / name).read_bytes()
                for name in evaluator.EXPECTED_GENERATION_FILES
            }
            manifest = json.loads(blobs[holdout.MANIFEST_NAME])
            manifest["bindings"]["segment_labels"]["sha256"] = (
                "sha256:" + "0" * 64
            )
            blobs[holdout.MANIFEST_NAME] = render_json(manifest)
            tampered = fixture.root / holdout.sealed_directory_name(blobs)
            tampered.mkdir(mode=0o700)
            for name, data in blobs.items():
                path = tampered / name
                path.write_bytes(data)
                path.chmod(0o444)
            tampered.chmod(0o555)
            with self.assertRaisesRegex(ValueError, "binding for segment_labels"):
                evaluator.evaluate_generation(
                    tampered,
                    fixture.hazkey_path,
                    fixture.mozc_path,
                )


if __name__ == "__main__":
    unittest.main()
