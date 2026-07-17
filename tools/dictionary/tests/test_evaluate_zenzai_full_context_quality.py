from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools.dictionary import evaluate_zenzai_full_context_quality as evaluator
from tools.dictionary import import_ajimee_contextual_blind_silver as importer
from tools.dictionary import prepare_ajimee_contextual_full_evaluation as preparer
from tools.dictionary.tests.test_evaluate_zenzai_left_context_quality import (
    candidate_records,
    jsonl_bytes,
    measurement,
    quality_policy,
    sha256,
    span,
    v7_context,
    zenzai_execution,
)
from tools.dictionary.tests.test_import_ajimee_contextual_blind_silver import (
    render_raw,
    upstream_rows,
)


class Fixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.raw = render_raw(upstream_rows())
        self.raw_path = root / "raw.json"
        self.raw_path.write_bytes(self.raw)
        self.imported = importer._build_generation_for_contract(
            self.raw,
            expected_raw_sha256=sha256(self.raw),
            expected_total_rows=4,
            expected_contextual_rows=2,
            expected_empty_rows=2,
        )
        self.generated = preparer._build_generation_for_contract(
            self.raw,
            self.imported,
            expected_raw_sha256=sha256(self.raw),
            expected_total_rows=4,
            expected_contextual_rows=2,
            expected_empty_rows=2,
        )
        self.generation_dir = root / "generation"
        self.generation_dir.mkdir()
        for name, data in self.generated.items():
            (self.generation_dir / name).write_bytes(data)
        self.manifest_path = self.generation_dir / preparer.MANIFEST_NAME
        self.empty_path = root / "empty-v7.jsonl"
        self.natural_path = root / "natural-v7.jsonl"
        self.targets = self.records(self.generation_dir / preparer.TARGETS_NAME)
        self.empty_context = self.records(
            self.generation_dir / preparer.EMPTY_CONTEXT_NAME
        )
        self.natural_context = self.records(
            self.generation_dir / preparer.CONTEXT_NAME
        )
        self.write_runs()

    @staticmethod
    def records(path: Path) -> list[dict[str, object]]:
        return [json.loads(line) for line in path.read_text().splitlines()]

    @staticmethod
    def replace(path: Path, records: list[dict[str, object]]) -> None:
        path.write_bytes(jsonl_bytes(records))

    def record(
        self,
        target: dict[str, object],
        context: dict[str, object],
        context_source: dict[str, object],
        candidates: list[tuple[str, int]],
        *,
        latency: float,
        score: float,
    ) -> dict[str, object]:
        reading = str(target["reading"])
        return {
            "schema": "hazkey.ab-probe-result.v7",
            "conversion_path": evaluator.CONVERSION_PATH,
            "id": target["id"],
            "reading": reading,
            "category": target["category"],
            "backend": "Hazkey+Zenzai",
            "backend_version": "full-context-test-v1",
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
            "quality_policy": quality_policy("left_context_sidecar"),
            "top_k": 4,
            "corpus": {
                "sha256": sha256(self.generated[preparer.PROBE_INPUT_NAME]),
                "cases": len(self.targets),
            },
            "candidates": candidate_records(candidates, score),
            "composition_span": span(len(reading)),
            "measurement": measurement(latency),
            "context": v7_context(context, context_source),
            "boundary_policy": dict(evaluator.BOUNDARY_POLICY),
            "fixed_boundary": None,
            "zenzai_execution": zenzai_execution(True),
        }

    def write_run(
        self,
        path: Path,
        contexts: list[dict[str, object]],
        candidates: dict[str, list[str]],
        *,
        latency: float,
        score: float,
    ) -> None:
        context_data = (
            self.generated[preparer.EMPTY_CONTEXT_NAME]
            if contexts is self.empty_context
            else self.generated[preparer.CONTEXT_NAME]
        )
        context_source = {
            "schema": "hazkey.blind-silver-left-context.v1",
            "sha256": sha256(context_data),
            "cases": len(self.targets),
        }
        records = []
        for target, context in zip(self.targets, contexts, strict=True):
            count = len(str(target["reading"]))
            records.append(
                self.record(
                    target,
                    context,
                    context_source,
                    [(text, count) for text in candidates[str(target["id"])]],
                    latency=latency,
                    score=score,
                )
            )
        path.write_bytes(jsonl_bytes(records))

    def write_runs(self) -> None:
        first, second = self.targets
        first_ref = str(first["surface_references"][0])
        second_ref = str(second["surface_references"][0])
        self.write_run(
            self.empty_path,
            self.empty_context,
            {
                str(first["id"]): ["誤答", first_ref],
                str(second["id"]): [second_ref],
            },
            latency=1.0,
            score=-2.0,
        )
        self.write_run(
            self.natural_path,
            self.natural_context,
            {
                str(first["id"]): [first_ref],
                str(second["id"]): ["誤答", str(second["surface_references"][1])],
            },
            latency=2.0,
            score=-1.0,
        )

    def evaluate(self) -> dict[str, object]:
        def rederive(raw: bytes, actual: dict[str, bytes]) -> dict[str, bytes]:
            return preparer._rederive_generation_for_contract(
                raw,
                actual,
                expected_raw_sha256=sha256(self.raw),
                expected_total_rows=4,
                expected_contextual_rows=2,
                expected_empty_rows=2,
            )

        with mock.patch.object(preparer, "rederive_generation", side_effect=rederive):
            return evaluator.evaluate(
                self.raw_path,
                self.manifest_path,
                self.empty_path,
                self.natural_path,
            )


class ZenzaiFullContextQualityTests(unittest.TestCase):
    def test_reports_exact_reference_quality_pairing_execution_latency_and_strata(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            report = Fixture(Path(temporary)).evaluate()

        self.assertEqual(report["schema"], evaluator.OUTPUT_SCHEMA)
        self.assertTrue(report["diagnostic_only"])
        self.assertFalse(report["formal_authorized"])
        self.assertFalse(report["evaluation_scope"]["boundary_metric_reported"])
        summary = report["all_cases"]
        self.assertEqual(
            summary["systems"][evaluator.EMPTY_SYSTEM]["accuracy_at1"],
            {"hits": 1, "cases": 2, "accuracy": 0.5},
        )
        self.assertEqual(
            summary["systems"][evaluator.NATURAL_SYSTEM]["accuracy_at_k"],
            {"hits": 2, "cases": 2, "accuracy": 1.0},
        )
        self.assertEqual(
            summary["systems"][evaluator.EMPTY_SYSTEM]["mrr_at_k"],
            {"k": 4, "cases": 2, "value": 0.75},
        )
        at1 = summary["paired_natural_vs_empty"]["accuracy_at1"]
        self.assertEqual((at1["rescued"], at1["regressed"]), (1, 1))
        self.assertEqual(
            summary["zenzai_execution"][evaluator.EMPTY_SYSTEM]["request_count"], 2
        )
        self.assertEqual(
            summary["latency_ms"]["natural_minus_empty"]["median"], 1.0
        )
        self.assertEqual(
            summary["memory_kib"]["systems"][evaluator.NATURAL_SYSTEM][
                "process_pss_kib"
            ]["after"]["median"],
            85.0,
        )
        self.assertEqual(
            sum(value["cases"] for value in report["by_context_code_point_length"].values()),
            2,
        )
        self.assertEqual(
            sum(value["cases"] for value in report["by_input_code_point_length"].values()),
            2,
        )
        rendered = json.dumps(report, ensure_ascii=False)
        self.assertNotIn('"left_context":', rendered)
        self.assertNotIn("first_segment_boundary", rendered)

    def test_rejects_partial_consumption_and_wrong_span(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            records = fixture.records(fixture.natural_path)
            records[0]["candidates"][0]["consuming_count"] -= 1
            fixture.replace(fixture.natural_path, records)
            with self.assertRaisesRegex(ValueError, "consume the complete composition"):
                fixture.evaluate()

        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            records = fixture.records(fixture.natural_path)
            records[0]["composition_span"]["count"] -= 1
            fixture.replace(fixture.natural_path, records)
            with self.assertRaisesRegex(ValueError, "(?:complete|whole) reading"):
                fixture.evaluate()

    def test_path_policy_fixed_boundary_and_request_count_are_strict(self) -> None:
        mutations = (
            (
                "conversion_path",
                "conversion_path must be",
                lambda record: record.update({"conversion_path": "segment_candidates"}),
            ),
            (
                "boundary_policy",
                "must describe full composition",
                lambda record: record["boundary_policy"].update({"source": "wrong"}),
            ),
            (
                "boundary_policy_boolean",
                "boolean fields must be JSON booleans",
                lambda record: record["boundary_policy"].update(
                    {"surface_zenzai_enabled": 1}
                ),
            ),
            (
                "fixed_boundary",
                "fixed_boundary must be null",
                lambda record: record.update({"fixed_boundary": {}}),
            ),
            (
                "request_count",
                "request_count must be 1",
                lambda record: record.update(
                    {
                        "zenzai_execution": {
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
                    }
                ),
            ),
        )
        for name, message, mutate in mutations:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                fixture = Fixture(Path(temporary))
                records = fixture.records(fixture.natural_path)
                mutate(records[0])
                fixture.replace(fixture.natural_path, records)
                with self.assertRaisesRegex(ValueError, message):
                    fixture.evaluate()

    def test_common_producer_resource_model_top_k_and_corpus_are_strict(self) -> None:
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
            (
                "corpus",
                lambda record: record["corpus"].update(
                    {"sha256": "sha256:" + "f" * 64}
                ),
            ),
        )
        for field, mutate in mutations:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                fixture = Fixture(Path(temporary))
                records = fixture.records(fixture.natural_path)
                for record in records:
                    mutate(record)
                fixture.replace(fixture.natural_path, records)
                message = (
                    "corpus identity does not match probe input"
                    if field == "corpus"
                    else f"paired full-composition acquisition {field} differs"
                )
                with self.assertRaisesRegex(ValueError, message):
                    fixture.evaluate()

    def test_context_attestation_and_natural_empty_roles_are_strict(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            records = fixture.records(fixture.natural_path)
            for record in records:
                record["context"]["source"]["sha256"] = "sha256:" + "f" * 64
            fixture.replace(fixture.natural_path, records)
            with self.assertRaisesRegex(ValueError, "context.source does not match"):
                fixture.evaluate()

        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            records = fixture.records(fixture.natural_path)
            empty_source = {
                "schema": "hazkey.blind-silver-left-context.v1",
                "sha256": sha256(fixture.generated[preparer.EMPTY_CONTEXT_NAME]),
                "cases": len(records),
            }
            for record, context in zip(records, fixture.empty_context, strict=True):
                record["context"] = v7_context(context, empty_source)
            fixture.replace(fixture.natural_path, records)
            with self.assertRaisesRegex(ValueError, "context.source does not match"):
                fixture.evaluate()

    def test_generation_and_raw_snapshot_are_exactly_rederived(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            target_path = fixture.generation_dir / preparer.TARGETS_NAME
            target_path.write_bytes(target_path.read_bytes() + b" ")
            with self.assertRaisesRegex(ValueError, "not exactly rederived"):
                fixture.evaluate()

        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            fixture.raw_path.write_bytes(fixture.raw + b" ")
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                fixture.evaluate()

    def test_terminal_failures_are_counted_and_formal_blockers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            records = fixture.records(fixture.natural_path)
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
            fixture.replace(fixture.natural_path, records)
            report = fixture.evaluate()

        execution = report["all_cases"]["zenzai_execution"][evaluator.NATURAL_SYSTEM]
        self.assertEqual(execution["terminal_outcomes"]["no_candidate"], 1)
        self.assertTrue(
            any("natural_v7" in value and "no_candidate" in value for value in report["decision"]["formal_blockers"])
        )


if __name__ == "__main__":
    unittest.main()
