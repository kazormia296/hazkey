from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT))

from tools.dictionary import compare_conversion_quality  # noqa: E402


SCRIPT = REPOSITORY_ROOT / "tools/dictionary/compare_conversion_quality.py"


def make_case(
    case_id: str,
    observed: list[str],
    *,
    expected: list[str] | None = None,
    top_k: int = 3,
    category: str = "sample",
    reading: str | None = None,
) -> dict[str, object]:
    expected = expected or [f"expected-{case_id}"]
    rank = compare_conversion_quality.expected_rank(expected, observed)
    case: dict[str, object] = {
        "id": case_id,
        "category": category,
        "reading": reading or f"reading-{case_id}",
        "expected": expected,
        "top1": rank == 1,
        "observed": observed,
    }
    if top_k != 1:
        case[f"top{top_k}"] = rank is not None
    return case


def make_report(cases: list[dict[str, object]], top_k: int = 3) -> dict[str, object]:
    top1_hits = sum(int(case["top1"]) for case in cases)
    top_k_key = f"top{top_k}"
    top_k_hits = (
        top1_hits if top_k == 1 else sum(int(case[top_k_key]) for case in cases)
    )
    by_category: dict[str, Counter[str]] = {}
    for case in cases:
        category = str(case["category"])
        counters = by_category.setdefault(category, Counter())
        counters["total"] += 1
        counters["top1"] += int(case["top1"])
        counters["top_k"] += int(case["top1"] if top_k == 1 else case[top_k_key])

    categories: dict[str, dict[str, object]] = {}
    for category, counters in by_category.items():
        values: dict[str, object] = {
            "total": counters["total"],
            "top1": counters["top1"],
            "top1_rate": counters["top1"] / counters["total"],
        }
        if top_k != 1:
            values[top_k_key] = counters["top_k"]
            values[f"{top_k_key}_rate"] = counters["top_k"] / counters["total"]
        categories[category] = values

    report: dict[str, object] = {
        "schema": "hazkey.conversion-quality-report.v1",
        "top_k": top_k,
        "corpus_cases": len(cases),
        "evaluated_cases": len(cases),
        "missing_results": [],
        "top1_hits": top1_hits,
        "top1_rate": top1_hits / len(cases),
        "by_category": categories,
        "cases": cases,
    }
    if top_k != 1:
        report[f"{top_k_key}_hits"] = top_k_hits
        report[f"{top_k_key}_rate"] = top_k_hits / len(cases)
    return report


class ConversionQualityABComparisonTests(unittest.TestCase):
    def test_compare_reports_emits_ranks_metrics_deltas_and_winners(self) -> None:
        expected = ["expected"]
        a_report = make_report(
            [
                make_case("a-win", ["expected", "other"], expected=expected),
                make_case("b-win", ["other"], expected=expected),
                make_case("tie", ["other"], expected=expected),
            ]
        )
        b_report = make_report(
            [
                make_case("a-win", ["other", "expected"], expected=expected),
                make_case("b-win", ["expected"], expected=expected),
                make_case("tie", ["other"], expected=expected),
            ]
        )

        report = compare_conversion_quality.compare_reports(
            a_report,
            b_report,
            a_name="Hazkey",
            b_name="Mozkey",
        )

        self.assertEqual(report["schema"], "hazkey.conversion-quality-ab-report.v1")
        self.assertEqual(report["backends"], {"a": "Hazkey", "b": "Mozkey"})
        self.assertEqual(report["delta_direction"], "b_minus_a")
        self.assertEqual(report["wins"], {"a": 1, "b": 1, "ties": 1})
        self.assertEqual(
            report["metrics"]["a"],
            {
                "top1_hits": 1,
                "top1_rate": 1 / 3,
                "top_k_hits": 1,
                "top_k_rate": 1 / 3,
            },
        )
        self.assertEqual(
            report["metrics"]["b"],
            {
                "top1_hits": 1,
                "top1_rate": 1 / 3,
                "top_k_hits": 2,
                "top_k_rate": 2 / 3,
            },
        )
        self.assertEqual(
            report["metrics"]["delta"],
            {
                "top1_hits": 0,
                "top1_rate": 0.0,
                "top_k_hits": 1,
                "top_k_rate": 1 / 3,
            },
        )
        cases = {case["id"]: case for case in report["cases"]}
        self.assertEqual(cases["a-win"]["a"]["expected_rank"], 1)
        self.assertEqual(cases["a-win"]["b"]["expected_rank"], 2)
        self.assertEqual(cases["a-win"]["winner"], "a")
        self.assertEqual(cases["a-win"]["rank_delta"], 1)
        self.assertIsNone(cases["b-win"]["a"]["expected_rank"])
        self.assertEqual(cases["b-win"]["winner"], "b")
        self.assertIsNone(cases["b-win"]["rank_delta"])
        self.assertEqual(cases["tie"]["winner"], "tie")
        self.assertIsNone(cases["tie"]["rank_delta"])

    def test_cli_writes_a_valid_report(self) -> None:
        expected = ["expected"]
        a_report = make_report([make_case("case", ["expected"], expected=expected)])
        b_report = make_report(
            [make_case("case", ["other", "expected"], expected=expected)]
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            a_path = directory / "a.json"
            b_path = directory / "b.json"
            output_path = directory / "comparison.json"
            a_path.write_text(json.dumps(a_report), encoding="utf-8")
            b_path.write_text(json.dumps(b_report), encoding="utf-8")
            result = self.run_cli(a_path, b_path, output_path)
            output = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output["schema"], "hazkey.conversion-quality-ab-report.v1")
        self.assertEqual(output["cases"][0]["rank_delta"], 1)

    def test_cli_rejects_missing_duplicate_and_malformed_inputs_without_traceback(
        self,
    ) -> None:
        expected = ["expected"]
        valid_cases = [
            make_case("first", ["expected"], expected=expected),
            make_case("second", ["other"], expected=expected),
        ]
        valid = make_report(valid_cases)

        missing = make_report(valid_cases[:1])

        duplicate = make_report(valid_cases)
        duplicate["cases"] = list(duplicate["cases"]) + [dict(valid_cases[0])]
        duplicate["corpus_cases"] = 3
        duplicate["evaluated_cases"] = 3

        malformed = make_report(valid_cases)
        malformed["top1_hits"] = 0

        top_k_mismatch = make_report(
            [
                make_case("first", ["expected"], expected=expected, top_k=2),
                make_case("second", ["other"], expected=expected, top_k=2),
            ],
            top_k=2,
        )

        scenarios = {
            "missing case": (valid, missing),
            "duplicate case id": (duplicate, valid),
            "malformed aggregate": (malformed, valid),
            "top-k mismatch": (valid, top_k_mismatch),
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            for name, (a_report, b_report) in scenarios.items():
                with self.subTest(name=name):
                    a_path = directory / f"{name}-a.json"
                    b_path = directory / f"{name}-b.json"
                    output_path = directory / f"{name}-output.json"
                    a_path.write_text(json.dumps(a_report), encoding="utf-8")
                    b_path.write_text(json.dumps(b_report), encoding="utf-8")
                    result = self.run_cli(a_path, b_path, output_path)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn("error:", result.stderr)
                    self.assertNotIn("Traceback", result.stderr)

    def test_cli_rejects_duplicate_json_keys_without_traceback(self) -> None:
        valid = make_report([make_case("case", ["expected-case"])])
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            a_path = directory / "a.json"
            b_path = directory / "b.json"
            output_path = directory / "output.json"
            a_path.write_text(
                '{"schema":"hazkey.conversion-quality-report.v1",'
                '"schema":"hazkey.conversion-quality-report.v1"}',
                encoding="utf-8",
            )
            b_path.write_text(json.dumps(valid), encoding="utf-8")
            result = self.run_cli(a_path, b_path, output_path)

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("duplicate JSON key", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def run_cli(
        self,
        a_path: Path,
        b_path: Path,
        output_path: Path,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--a-report",
                str(a_path),
                "--b-report",
                str(b_path),
                "--a-name",
                "Hazkey",
                "--b-name",
                "Mozkey",
                "--output",
                str(output_path),
            ],
            check=False,
            capture_output=True,
            text=True,
        )


if __name__ == "__main__":
    unittest.main()
