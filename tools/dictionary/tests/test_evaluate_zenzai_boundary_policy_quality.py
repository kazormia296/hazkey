from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from tools.dictionary import evaluate_zenzai_boundary_policy_quality as evaluator
from tools.dictionary.tests.test_evaluate_zenzai_left_context_quality import (
    CASES,
    Fixture,
    jsonl_bytes,
    sha256,
)


class ZenzaiBoundaryPolicyQualityTests(unittest.TestCase):
    @staticmethod
    def evaluate(fixture: Fixture) -> dict[str, object]:
        return evaluator.evaluate(
            fixture.manifest_path,
            fixture.targets_path,
            fixture.isolated_empty_context_path,
            fixture.isolated_empty_path,
            fixture.native_empty_path,
            fixture.fixed_empty_path,
            fixture.fixed_raw_mozc_path,
            fixture.fixed_boundary_path,
        )

    def test_reports_absolute_quality_pairwise_category_delta_execution_and_memory(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = self.evaluate(Fixture(Path(temporary)))

        self.assertEqual(report["schema"], evaluator.OUTPUT_SCHEMA)
        self.assertTrue(report["diagnostic_only"])
        self.assertFalse(report["formal_authorized"])
        summary = report["all_cases"]
        systems = summary["systems"]
        self.assertEqual(set(systems), set(evaluator.SYSTEMS))
        self.assertEqual(
            systems[evaluator.ISOLATED_SYSTEM]["first_segment_boundary"]["at1"],
            {"hits": 4, "cases": 4, "accuracy": 1.0},
        )
        self.assertEqual(
            systems[evaluator.NATIVE_SYSTEM]["first_segment_boundary"]["at1"],
            {"hits": 2, "cases": 4, "accuracy": 0.5},
        )
        self.assertEqual(
            systems[evaluator.NATIVE_SYSTEM][
                "conditional_surface_given_acceptable_first_segment_boundary"
            ]["at1"],
            {"hits": 2, "cases": 2, "accuracy": 1.0},
        )
        self.assertEqual(
            systems[evaluator.FIXED_SYSTEM]["end_to_end"]["at1"],
            {"hits": 2, "cases": 4, "accuracy": 0.5},
        )

        isolated_to_native = summary["pairwise_matrix"][
            evaluator.ISOLATED_SYSTEM
        ][evaluator.NATIVE_SYSTEM]
        self.assertEqual(
            isolated_to_native["first_segment_boundary"]["at1"]["regressed"],
            2,
        )
        self.assertEqual(
            isolated_to_native[
                "conditional_surface_on_mutually_acceptable_boundaries"
            ]["at1"]["comparable_cases"],
            2,
        )
        proper_noun = report["by_category"]["proper-noun"]
        self.assertEqual(proper_noun["cases"], 1)
        native_delta = proper_noun["boundary_diagnostics"]["systems"][
            evaluator.NATIVE_SYSTEM
        ]
        self.assertEqual(native_delta["classification_counts"], {"too_long": 1})
        fixed_delta = proper_noun["boundary_diagnostics"]["systems"][
            evaluator.FIXED_SYSTEM
        ]
        self.assertEqual(fixed_delta["classification_counts"], {"match": 1})

        shapes = report["by_input_shape"]
        self.assertEqual(set(shapes), set(evaluator.INPUT_SHAPES))
        self.assertEqual(shapes["contains_ascii"]["cases"], 0)
        self.assertEqual(shapes["no_ascii"]["cases"], 4)
        self.assertEqual(
            shapes["no_ascii"]["systems"][evaluator.NATIVE_SYSTEM][
                "first_segment_boundary"
            ]["at1"]["cases"],
            4,
        )
        self.assertEqual(
            shapes["no_ascii"]["pairwise_matrix"][evaluator.ISOLATED_SYSTEM][
                evaluator.NATIVE_SYSTEM
            ]["first_segment_boundary"]["at1"]["regressed"],
            2,
        )
        self.assertEqual(
            shapes["no_ascii"]["zenzai_execution"][evaluator.NATIVE_SYSTEM][
                "request_count"
            ],
            4,
        )

        execution = summary["zenzai_execution"]
        self.assertEqual(execution[evaluator.ISOLATED_SYSTEM]["request_count"], 8)
        self.assertEqual(execution[evaluator.NATIVE_SYSTEM]["request_count"], 4)
        self.assertEqual(execution[evaluator.FIXED_SYSTEM]["request_count"], 4)
        self.assertEqual(
            summary["latency_ms"]["systems"][evaluator.NATIVE_SYSTEM]["median"],
            1.5,
        )
        self.assertEqual(
            summary["memory_kib"]["systems"][evaluator.FIXED_SYSTEM][
                "process_rss_kib"
            ]["after"]["median"],
            110.0,
        )
        self.assertTrue(
            all(report["inputs"]["fixed_boundary_provenance_chain_verified"].values())
        )
        rendered = json.dumps(report, ensure_ascii=False)
        self.assertNotIn('"left_context":', rendered)
        self.assertTrue(report["evaluation_scope"]["raw_left_context_emitted"] is False)

    def test_empty_fixed_boundary_is_scored_from_attestation_not_candidates(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = self.evaluate(Fixture(Path(temporary)))

        case = next(case for case in report["cases"] if case["id"] == "second-rescue")
        self.assertEqual(case["mozc_fixed_candidate_count"], 0)
        fixed = case["gold_outcomes"]["systems"][evaluator.FIXED_SYSTEM]
        self.assertTrue(fixed["first_segment_boundary"]["at1"])
        self.assertTrue(
            fixed[
                "conditional_surface_given_acceptable_first_segment_boundary"
            ]["at1_comparable"]
        )
        self.assertFalse(
            fixed[
                "conditional_surface_given_acceptable_first_segment_boundary"
            ]["at1_hit"]
        )
        self.assertFalse(fixed["end_to_end"]["at1"])
        diagnostic = case["boundary_diagnostics"]["systems"][evaluator.FIXED_SYSTEM]
        self.assertEqual(diagnostic["count_source"], "explicit_fixed_boundary")
        self.assertEqual(diagnostic["classification"], "match")

    def test_input_shape_partition_is_runtime_observable_and_exhaustive(self) -> None:
        cases = [
            {"reading": "かな"},
            {"reading": "abc"},
            {"reading": "かな1"},
            {"reading": "ＡＢＣ"},
        ]
        grouped = evaluator._cases_by_input_shape(cases)
        self.assertEqual(
            [case["reading"] for case in grouped["contains_ascii"]],
            ["abc", "かな1"],
        )
        self.assertEqual(
            [case["reading"] for case in grouped["no_ascii"]],
            ["かな", "ＡＢＣ"],
        )
        self.assertEqual(sum(map(len, grouped.values())), len(cases))

    def test_sidecar_must_be_empty_and_bound_to_reviewed_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            records = fixture.records(fixture.isolated_empty_context_path)
            records[0]["left_context"] = "非空"
            records[0]["left_context_sha256"] = sha256("非空".encode())
            fixture.replace(fixture.isolated_empty_context_path, records)
            with self.assertRaisesRegex(ValueError, "contains nonempty left context"):
                self.evaluate(fixture)

        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            records = fixture.records(fixture.isolated_empty_context_path)
            records[0]["source_content_sha256"] = "sha256:" + "f" * 64
            fixture.replace(fixture.isolated_empty_context_path, records)
            with self.assertRaisesRegex(ValueError, "reviewed source.row_sha256"):
                self.evaluate(fixture)

    def test_every_run_must_bind_the_one_exact_context_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            records = fixture.records(fixture.native_empty_path)
            for record in records:
                record["context"]["source"]["sha256"] = "sha256:" + "f" * 64
            fixture.replace(fixture.native_empty_path, records)
            with self.assertRaisesRegex(ValueError, "exact empty sidecar"):
                self.evaluate(fixture)

    def test_common_producer_resource_model_and_top_k_are_strict(self) -> None:
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
            ("top_k", lambda record: record.update({"top_k": 5})),
        )
        for field, mutate in mutations:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                fixture = Fixture(Path(temporary))
                records = fixture.records(fixture.native_empty_path)
                for record in records:
                    mutate(record)
                fixture.replace(fixture.native_empty_path, records)
                with self.assertRaisesRegex(
                    ValueError, f"common acquisition {field} differs"
                ):
                    self.evaluate(fixture)

        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            for path in (
                fixture.isolated_empty_path,
                fixture.native_empty_path,
                fixture.fixed_empty_path,
            ):
                records = fixture.records(path)
                for record in records:
                    record["top_k"] = 6
                fixture.replace(path, records)
            with self.assertRaisesRegex(ValueError, "common top_k must be <= 5"):
                self.evaluate(fixture)

    def test_boundary_policy_and_execution_request_count_are_mode_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            records = fixture.records(fixture.native_empty_path)
            records[0]["boundary_policy"] = {
                "mode": "isolated_dictionary",
                "boundary_zenzai_enabled": False,
                "surface_zenzai_enabled": True,
                "source": "separate_converter",
            }
            fixture.replace(fixture.native_empty_path, records)
            with self.assertRaisesRegex(ValueError, "conflicts with conversion_path"):
                self.evaluate(fixture)

        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            records = fixture.records(fixture.native_empty_path)
            records[0]["zenzai_execution"] = {
                "request_count": 2,
                "evaluation_attempt_count": 2,
                "attempt_outcomes": {
                    "pass": 2,
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
            fixture.replace(fixture.native_empty_path, records)
            with self.assertRaisesRegex(ValueError, "request_count must be 1"):
                self.evaluate(fixture)

    def test_empty_probe_run_is_rejected_as_a_validation_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            fixture.native_empty_path.write_bytes(b"")
            with self.assertRaisesRegex(ValueError, "contains no records"):
                self.evaluate(fixture)

    def test_fixed_sidecar_is_canonically_rederived_and_explicitly_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            records = fixture.records(fixture.fixed_boundary_path)
            records[0]["consuming_count"] += 1
            fixture.replace(fixture.fixed_boundary_path, records)
            with self.assertRaisesRegex(ValueError, "canonical derivation"):
                self.evaluate(fixture)

        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            records = fixture.records(fixture.fixed_empty_path)
            target = next(record for record in records if record["id"] == "second-rescue")
            self.assertEqual(target["candidates"], [])
            target["fixed_boundary"]["consuming_count"] = 3
            fixture.replace(fixture.fixed_empty_path, records)
            with self.assertRaisesRegex(ValueError, "fixed_boundary does not match sidecar"):
                self.evaluate(fixture)

    def test_raw_mozc_and_fixed_run_must_share_acquisition_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            raw_records = fixture.records(fixture.fixed_raw_mozc_path)
            for record in raw_records:
                record["backend_version"] = "different-version"
            raw_data = jsonl_bytes(raw_records)
            fixture.fixed_raw_mozc_path.write_bytes(raw_data)

            from tools.dictionary import prepare_mozc_fixed_boundary_sidecar as prepare

            fixed_data = prepare.prepare_sidecar_bytes(raw_data)
            fixture.fixed_boundary_path.write_bytes(fixed_data)
            fixed_source = {
                "schema": prepare.SIDECAR_SCHEMA,
                "sha256": sha256(fixed_data),
                "cases": len(CASES),
            }
            fixed_records = fixture.records(fixture.fixed_empty_path)
            for record in fixed_records:
                record["fixed_boundary"]["source"] = fixed_source
            fixture.replace(fixture.fixed_empty_path, fixed_records)
            with self.assertRaisesRegex(
                ValueError, "acquisition metadata backend_version differs"
            ):
                self.evaluate(fixture)

    def test_terminal_failures_are_reported_as_diagnostic_blockers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            records = fixture.records(fixture.native_empty_path)
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
                    "pass": 0,
                    "fix_required": 0,
                    "whole_result": 0,
                    "error": 0,
                    "inference_limit": 0,
                    "no_candidate": 1,
                },
            }
            fixture.replace(fixture.native_empty_path, records)
            report = self.evaluate(fixture)

        execution = report["all_cases"]["zenzai_execution"][
            evaluator.NATIVE_SYSTEM
        ]
        self.assertEqual(execution["terminal_outcomes"]["no_candidate"], 1)
        self.assertTrue(
            any(
                evaluator.NATIVE_SYSTEM in blocker and "no_candidate" in blocker
                for blocker in report["decision"]["formal_blockers"]
            )
        )

    def test_cli_writes_a_json_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            output = Path(temporary) / "report.json"
            status = evaluator.main(
                [
                    "--generation-manifest",
                    str(fixture.manifest_path),
                    "--targets",
                    str(fixture.targets_path),
                    "--empty-context-sidecar",
                    str(fixture.isolated_empty_context_path),
                    "--isolated-v7",
                    str(fixture.isolated_empty_path),
                    "--native-v7",
                    str(fixture.native_empty_path),
                    "--fixed-v7",
                    str(fixture.fixed_empty_path),
                    "--raw-mozc-v6",
                    str(fixture.fixed_raw_mozc_path),
                    "--fixed-boundary-sidecar",
                    str(fixture.fixed_boundary_path),
                    "--output",
                    str(output),
                ]
            )
            report = json.loads(output.read_text())

        self.assertEqual(status, 0)
        self.assertEqual(report["schema"], evaluator.OUTPUT_SCHEMA)


if __name__ == "__main__":
    unittest.main()
