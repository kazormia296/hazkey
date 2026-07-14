from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT))

from tools.dictionary import evaluate_context_boundaries  # noqa: E402
from tools.dictionary import evaluate_conversion_quality  # noqa: E402
from tools.dictionary import summarize_conversion_quality  # noqa: E402


QUALITY_FIXTURES = (
    REPOSITORY_ROOT
    / "hazkey-server/Tests/grimodex-spike/Fixtures/quality-v1"
)


class ConversionQualityTests(unittest.TestCase):
    def test_corpus_requires_nonempty_semantic_fields(self) -> None:
        invalid_rows = (
            ("", "expected", "sample"),
            ("reading", "", "sample"),
            ("reading", "|", "sample"),
            ("reading", "expected|", "sample"),
            ("reading", "|expected", "sample"),
            ("reading", "first||second", "sample"),
            ("reading", "expected", ""),
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            corpus_path = Path(temporary_directory) / "corpus.tsv"
            for reading, expected, category in invalid_rows:
                with self.subTest(
                    reading=reading, expected=expected, category=category
                ):
                    corpus_path.write_text(
                        "id\treading\texpected\tcategory\n"
                        f"case\t{reading}\t{expected}\t{category}\n",
                        encoding="utf-8",
                    )
                    with self.assertRaises(ValueError):
                        evaluate_conversion_quality.load_corpus(corpus_path)

        corpus = evaluate_conversion_quality.load_corpus(
            QUALITY_FIXTURES / "conversion-quality-v1.tsv"
        )
        self.assertTrue(corpus)
        for row in corpus:
            self.assertTrue(row["reading"])
            self.assertTrue(row["category"])
            self.assertTrue(all(row["expected"].split("|")))

    def test_results_require_nonempty_id_and_candidates(self) -> None:
        invalid_payloads = (
            {"id": "", "candidates": []},
            {"id": "case"},
            {"id": "case", "candidate_window": {}},
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            result_path = Path(temporary_directory) / "results.jsonl"
            for payload in invalid_payloads:
                with self.subTest(payload=payload):
                    result_path.write_text(
                        json.dumps(payload, ensure_ascii=False) + "\n",
                        encoding="utf-8",
                    )
                    with self.assertRaises(ValueError):
                        evaluate_conversion_quality.load_results(result_path)

    def test_candidate_window_requires_an_object_with_items(self) -> None:
        for payload in (
            {"id": "case"},
            {"id": "case", "candidate_window": None},
            {"id": "case", "candidate_window": []},
            {"id": "case", "candidate_window": {}},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    evaluate_conversion_quality.candidate_texts(payload)

    def test_malformed_candidate_window_exits_with_a_validation_error(self) -> None:
        script = REPOSITORY_ROOT / "tools/dictionary/evaluate_conversion_quality.py"
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            corpus_path = directory / "corpus.tsv"
            corpus_path.write_text(
                "id\treading\texpected\tcategory\n"
                "case\treading\texpected\tsample\n",
                encoding="utf-8",
            )
            results_path = directory / "results.jsonl"
            results_path.write_text(
                json.dumps({"id": "case", "candidate_window": None}) + "\n",
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--corpus",
                    str(corpus_path),
                    "--results",
                    str(results_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 2)
        self.assertIn("error:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_protected_fixture_accepts_the_exact_input_surface(self) -> None:
        corpus = evaluate_conversion_quality.load_corpus(
            QUALITY_FIXTURES / "conversion-quality-v1.tsv"
        )

        protected = [row for row in corpus if row["category"] == "protected"]
        self.assertTrue(protected)
        for row in protected:
            with self.subTest(case=row["id"]):
                self.assertIn(row["reading"], row["expected"].split("|"))

    def test_top_k_one_does_not_double_count_top1(self) -> None:
        corpus = [
            {
                "id": "hit",
                "reading": "hit",
                "expected": "hit",
                "category": "sample",
            },
            {
                "id": "miss",
                "reading": "miss",
                "expected": "expected",
                "category": "sample",
            },
        ]
        report = evaluate_conversion_quality.evaluate(
            corpus,
            {"hit": ["hit"], "miss": ["observed"]},
            top_k=1,
        )

        self.assertEqual(report["top1_hits"], 1)
        self.assertEqual(report["top1_rate"], 0.5)
        self.assertEqual(report["by_category"]["sample"]["top1"], 1)
        self.assertEqual(report["by_category"]["sample"]["top1_rate"], 0.5)

        with tempfile.TemporaryDirectory() as temporary_directory:
            report_path = Path(temporary_directory) / "report.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            summary = summarize_conversion_quality.summarize([report_path])

        self.assertEqual(summary["top1_hits"], 1)
        self.assertEqual(summary["top1_rate"], 0.5)
        self.assertEqual(summary["by_category"]["sample"]["top1"], 1)
        self.assertEqual(summary["by_category"]["sample"]["top1_rate"], 0.5)


class ConversionQualitySummaryValidationTests(unittest.TestCase):
    def test_malformed_reports_raise_value_error_and_cli_exits_two(self) -> None:
        script = REPOSITORY_ROOT / "tools/dictionary/summarize_conversion_quality.py"
        malformed_reports = (
            {
                "schema": "hazkey.context-boundary-report.v1",
                "top_k": 1,
                "corpus_cases": 1,
                "evaluated_cases": 1,
                "top1_hits": 1,
                "by_category": {"sample": {"total": 1, "top1": 1}},
            },
            {
                "schema": "hazkey.conversion-quality-report.v1",
                "top_k": True,
                "corpus_cases": 1,
                "evaluated_cases": 1,
                "top1_hits": 1,
                "by_category": {"sample": {"total": 1, "top1": 1}},
            },
            {
                "schema": "hazkey.conversion-quality-report.v1",
                "top_k": 1,
                "corpus_cases": 1,
                "evaluated_cases": 1,
                "top1_hits": True,
                "by_category": {"sample": {"total": 1, "top1": 1}},
            },
            {
                "schema": "hazkey.conversion-quality-report.v1",
                "top_k": 5,
                "corpus_cases": 2,
                "evaluated_cases": 2,
                "top1_hits": 1,
                "top5_hits": 1,
                "by_category": {
                    "sample": {"total": 1, "top1": 1, "top5": 1}
                },
            },
            {
                "schema": "hazkey.conversion-quality-report.v1",
                "top_k": 5,
                "corpus_cases": 2,
                "evaluated_cases": 2,
                "top1_hits": 1,
                "top5_hits": 2,
                "by_category": {
                    "sample": {"total": 2, "top1": 0, "top5": 2}
                },
            },
            {
                "schema": "hazkey.conversion-quality-report.v1",
                "top_k": 5,
                "corpus_cases": 2,
                "evaluated_cases": 2,
                "top1_hits": 1,
                "top5_hits": 2,
                "by_category": {
                    "sample": {"total": 2, "top1": 1, "top5": 1}
                },
            },
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            report_path = Path(temporary_directory) / "report.json"
            for report in malformed_reports:
                with self.subTest(report=report):
                    report_path.write_text(json.dumps(report), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        summarize_conversion_quality.summarize([report_path])

                    result = subprocess.run(
                        [sys.executable, str(script), str(report_path)],
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode, 2)
                    self.assertIn("error:", result.stderr)
                    self.assertNotIn("Traceback", result.stderr)

    def test_top_k_hits_cannot_be_less_than_top1_hits(self) -> None:
        script = REPOSITORY_ROOT / "tools/dictionary/summarize_conversion_quality.py"
        impossible_reports = (
            (
                {
                    "schema": "hazkey.conversion-quality-report.v1",
                    "top_k": 5,
                    "corpus_cases": 2,
                    "evaluated_cases": 2,
                    "top1_hits": 2,
                    "top5_hits": 1,
                    "by_category": {
                        "sample": {"total": 2, "top1": 2, "top5": 1}
                    },
                },
                "top5_hits is less than top1_hits",
            ),
            (
                {
                    "schema": "hazkey.conversion-quality-report.v1",
                    "top_k": 5,
                    "corpus_cases": 2,
                    "evaluated_cases": 2,
                    "top1_hits": 1,
                    "top5_hits": 1,
                    "by_category": {
                        "impossible": {"total": 1, "top1": 1, "top5": 0},
                        "offset": {"total": 1, "top1": 0, "top5": 1},
                    },
                },
                "impossible.top5 is less than impossible.top1",
            ),
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            report_path = Path(temporary_directory) / "report.json"
            for report, expected_error in impossible_reports:
                with self.subTest(expected_error=expected_error):
                    report_path.write_text(json.dumps(report), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, expected_error):
                        summarize_conversion_quality.summarize([report_path])

                    result = subprocess.run(
                        [sys.executable, str(script), str(report_path)],
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode, 2)
                    self.assertIn(expected_error, result.stderr)
                    self.assertNotIn("Traceback", result.stderr)


class ContextBoundaryTests(unittest.TestCase):
    def test_fixture_requires_an_interior_ascii_decimal_split(self) -> None:
        invalid_rows = (
            ("", "1", "sample"),
            ("abc", "", "sample"),
            ("abc", " 1", "sample"),
            ("abc", "1 ", "sample"),
            ("abc", "+1", "sample"),
            ("abc", "-1", "sample"),
            ("abc", "1.0", "sample"),
            ("abc", "１", "sample"),
            ("abc", "0", "sample"),
            ("abc", "01", "sample"),
            ("abc", "3", "sample"),
            ("abc", "1", ""),
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture_path = Path(temporary_directory) / "fixture.tsv"
            for reading, split_at, category in invalid_rows:
                with self.subTest(
                    reading=reading, split_at=split_at, category=category
                ):
                    fixture_path.write_text(
                        "id\treading\tsplit_at\tcategory\n"
                        f"case\t{reading}\t{split_at}\t{category}\n",
                        encoding="utf-8",
                    )
                    with self.assertRaises(ValueError):
                        evaluate_context_boundaries.load_fixture(fixture_path)

        fixture = evaluate_context_boundaries.load_fixture(
            QUALITY_FIXTURES / "context-boundary-v1.tsv"
        )
        self.assertTrue(fixture)
        for row in fixture:
            split_index = int(row["split_at"])
            self.assertGreater(split_index, 0)
            self.assertLess(split_index, len(row["reading"]))

    def test_results_require_both_candidate_arrays(self) -> None:
        invalid_payloads = (
            {"id": "", "whole_candidates": [], "split_candidates": []},
            {"id": "case", "split_candidates": ["same"]},
            {"id": "case", "whole_candidates": ["same"]},
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            result_path = Path(temporary_directory) / "results.jsonl"
            for payload in invalid_payloads:
                with self.subTest(payload=payload):
                    result_path.write_text(
                        json.dumps(payload, ensure_ascii=False) + "\n",
                        encoding="utf-8",
                    )
                    with self.assertRaises(ValueError):
                        evaluate_context_boundaries.load_results(result_path)

    def test_evaluate_reports_noncomparable_direct_results(self) -> None:
        fixture = [
            {
                "id": "case",
                "reading": "same",
                "split_at": "1",
                "category": "sample",
            }
        ]

        report = evaluate_context_boundaries.evaluate(
            fixture, {"case": {"whole": [], "split": ["same"]}}
        )
        self.assertEqual(report["evaluated_cases"], 1)
        self.assertEqual(report["comparable_cases"], 0)
        self.assertEqual(report["top1_drift_cases"], 0)
        self.assertFalse(report["cases"][0]["comparable"])
        self.assertIsNone(report["cases"][0]["whole_top1"])

    def test_fail_on_drift_rejects_invalid_and_reports_real_drift(self) -> None:
        script = REPOSITORY_ROOT / "tools/dictionary/evaluate_context_boundaries.py"
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            fixture_path = directory / "fixture.tsv"
            fixture_path.write_text(
                "id\treading\tsplit_at\tcategory\n"
                "case\tsame\t1\tsample\n",
                encoding="utf-8",
            )
            results_path = directory / "results.jsonl"

            results_path.write_text(
                json.dumps(
                    {
                        "id": "case",
                        "whole_candidates": [],
                        "split_candidates": [],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            invalid = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--fixture",
                    str(fixture_path),
                    "--results",
                    str(results_path),
                    "--fail-on-drift",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(invalid.returncode, 2, invalid.stderr)

            report_only = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--fixture",
                    str(fixture_path),
                    "--results",
                    str(results_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(report_only.returncode, 0, report_only.stderr)

            results_path.write_text(
                json.dumps(
                    {
                        "id": "case",
                        "whole_candidates": ["whole"],
                        "split_candidates": ["split"],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            drift = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--fixture",
                    str(fixture_path),
                    "--results",
                    str(results_path),
                    "--fail-on-drift",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(drift.returncode, 1, drift.stderr)


if __name__ == "__main__":
    unittest.main()
