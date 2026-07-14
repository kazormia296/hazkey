from __future__ import annotations

import json
import math
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT))

from tools.dictionary import summarize_ab_probe  # noqa: E402


SCRIPT = REPOSITORY_ROOT / "tools/dictionary/summarize_ab_probe.py"


def make_result(
    case_id: str,
    samples: list[float | int],
    *,
    backend: str = "hazkey",
    version: str = "0.2.1",
    category: str = "sample",
    source_ref: str = "0123456789abcdef",
    dictionary_path: str = "/fixtures/dictionary",
    dictionary_fingerprint: str = "sha256:abcdef",
    iterations: int | None = None,
    rss_before: int | None = 100,
    rss_after: int | None = 120,
) -> dict[str, object]:
    ordered = sorted(samples)
    p95_index = min(
        len(ordered) - 1,
        max(0, math.ceil(len(ordered) * 0.95) - 1),
    )
    return {
        "schema": "hazkey.ab-probe-result.v1",
        "id": case_id,
        "category": category,
        "backend": backend,
        "backend_version": version,
        "source_ref": source_ref,
        "dictionary_path": dictionary_path,
        "dictionary_fingerprint": dictionary_fingerprint,
        "candidates": [f"candidate-{case_id}"],
        "measurement": {
            "warmups": 2,
            "iterations": iterations if iterations is not None else len(samples),
            "latency_ms": {
                "median": statistics.median(samples),
                "p95": ordered[p95_index],
                "minimum": ordered[0],
                "maximum": ordered[-1],
                "samples": samples,
            },
            "rss": {"before_kib": rss_before, "after_kib": rss_after},
        },
    }


def write_run(path: Path, results: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(result) + "\n" for result in results),
        encoding="utf-8",
    )


class ABProbeSummaryTests(unittest.TestCase):
    def test_summarize_aggregates_raw_samples_runs_and_observed_rss(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            first = directory / "first.jsonl"
            second = directory / "second.jsonl"
            write_run(
                first,
                [
                    make_result("one", [1, 2], rss_before=90, rss_after=110),
                    make_result("two", [3, 4], rss_before=105, rss_after=130),
                ],
            )
            # Reversed order proves that run comparison uses the case set and ID.
            write_run(
                second,
                [
                    make_result("two", [6, 8], rss_before=150, rss_after=200),
                    make_result("one", [2, 4], rss_before=140, rss_after=180),
                ],
            )

            summary = summarize_ab_probe.summarize([first, second])

        self.assertEqual(summary["schema"], "hazkey.ab-probe-summary.v1")
        self.assertEqual(summary["backend"], "hazkey")
        self.assertEqual(summary["backend_version"], "0.2.1")
        self.assertEqual(
            summary["provenance"],
            {
                "source_ref": "0123456789abcdef",
                "dictionary_path": "/fixtures/dictionary",
                "dictionary_fingerprint": "sha256:abcdef",
            },
        )
        self.assertEqual(summary["runs"], 2)
        self.assertEqual(summary["cases_per_run"], 2)
        self.assertEqual(summary["iterations"], 2)
        self.assertEqual(summary["measured_conversions"], 8)
        self.assertEqual(summary["mean_latency_ms"], 3.75)
        self.assertEqual(summary["median_latency_ms"], 3.5)
        self.assertEqual(summary["p95_latency_ms"], 8)
        self.assertEqual(summary["min_latency_ms"], 1)
        self.assertEqual(summary["max_latency_ms"], 8)
        self.assertEqual(summary["mean_total_ms_per_run"], 15)
        self.assertEqual(summary["max_observed_rss_kib"], 200)
        self.assertNotIn("peak_rss_kib", summary)

    def test_cli_supports_output_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            run_path = directory / "run.jsonl"
            output_path = directory / "summary.json"
            write_run(run_path, [make_result("case", [1, 2])])
            result = self.run_cli([run_path], output_path)
            summary = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(summary["runs"], 1)
        self.assertEqual(summary["measured_conversions"], 2)

    def test_cli_rejects_invalid_runs_without_traceback(self) -> None:
        base = [
            make_result("one", [1, 2]),
            make_result("two", [3, 4]),
        ]
        mismatched_cases = [make_result("one", [1, 2])]
        backend_mismatch = [
            make_result("one", [1, 2], backend="other"),
            make_result("two", [3, 4], backend="other"),
        ]
        version_mismatch = [
            make_result("one", [1, 2], version="different"),
            make_result("two", [3, 4], version="different"),
        ]
        sample_count_mismatch = [
            make_result("one", [1, 2], iterations=3),
            make_result("two", [3, 4], iterations=3),
        ]
        bool_sample = [
            make_result("one", [1, 2]),
            make_result("two", [3, 4]),
        ]
        bool_sample[0]["measurement"]["latency_ms"]["samples"][0] = True
        bool_rss = [
            make_result("one", [1, 2]),
            make_result("two", [3, 4]),
        ]
        bool_rss[0]["measurement"]["rss"]["after_kib"] = True
        malformed_summary = [
            make_result("one", [1, 2]),
            make_result("two", [3, 4]),
        ]
        malformed_summary[0]["measurement"]["latency_ms"]["median"] = 99

        scenarios = {
            "case mismatch": (base, mismatched_cases),
            "backend mismatch": (base, backend_mismatch),
            "version mismatch": (base, version_mismatch),
            "sample count mismatch": (sample_count_mismatch,),
            "bool sample": (bool_sample,),
            "bool rss": (bool_rss,),
            "malformed latency summary": (malformed_summary,),
        }

        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            for name, runs in scenarios.items():
                with self.subTest(name=name):
                    paths: list[Path] = []
                    for index, run in enumerate(runs):
                        path = directory / f"{name}-{index}.jsonl"
                        write_run(path, run)
                        paths.append(path)
                    output = directory / f"{name}-summary.json"
                    result = self.run_cli(paths, output)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn("error:", result.stderr)
                    self.assertNotIn("Traceback", result.stderr)

    def test_cli_fails_closed_for_missing_or_invalid_provenance(self) -> None:
        scenarios: dict[str, dict[str, object]] = {}
        for field in (
            "source_ref",
            "dictionary_path",
            "dictionary_fingerprint",
        ):
            missing = make_result("case", [1, 2])
            del missing[field]
            scenarios[f"missing-{field}"] = missing

            empty = make_result("case", [1, 2])
            empty[field] = ""
            scenarios[f"empty-{field}"] = empty

            non_string = make_result("case", [1, 2])
            non_string[field] = 123
            scenarios[f"non-string-{field}"] = non_string

        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            for name, result_payload in scenarios.items():
                with self.subTest(name=name):
                    run_path = directory / f"{name}.jsonl"
                    output_path = directory / f"{name}-summary.json"
                    write_run(run_path, [result_payload])
                    result = self.run_cli([run_path], output_path)

                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn("error:", result.stderr)
                    self.assertNotIn("Traceback", result.stderr)
                    self.assertFalse(output_path.exists())

    def test_rejects_inconsistent_provenance_within_and_across_runs(self) -> None:
        fields = (
            "source_ref",
            "dictionary_path",
            "dictionary_fingerprint",
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            for field in fields:
                with self.subTest(field=field, scope="within-run"):
                    run_path = directory / f"within-{field}.jsonl"
                    first_case = make_result("one", [1, 2])
                    second_case = make_result("two", [3, 4])
                    second_case[field] = f"different-{field}"
                    write_run(run_path, [first_case, second_case])

                    with self.assertRaisesRegex(
                        ValueError, rf"inconsistent {field} within run"
                    ):
                        summarize_ab_probe.summarize([run_path])

                with self.subTest(field=field, scope="across-runs"):
                    first_run = directory / f"first-{field}.jsonl"
                    second_run = directory / f"second-{field}.jsonl"
                    write_run(first_run, [make_result("case", [1, 2])])
                    changed = make_result("case", [3, 4])
                    changed[field] = f"different-{field}"
                    write_run(second_run, [changed])

                    with self.assertRaisesRegex(
                        ValueError, rf"{field} does not match the first run"
                    ):
                        summarize_ab_probe.summarize([first_run, second_run])

    def test_cli_rejects_duplicate_ids_and_malformed_json_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            duplicate = directory / "duplicate.jsonl"
            duplicated_result = make_result("same", [1, 2])
            write_run(duplicate, [duplicated_result, duplicated_result])
            malformed = directory / "malformed.jsonl"
            malformed.write_text("{not-json}\n", encoding="utf-8")

            for name, path in (("duplicate", duplicate), ("malformed", malformed)):
                with self.subTest(name=name):
                    output = directory / f"{name}-summary.json"
                    result = self.run_cli([path], output)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn("error:", result.stderr)
                    self.assertNotIn("Traceback", result.stderr)

    def run_cli(
        self,
        paths: list[Path],
        output: Path,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                *(str(path) for path in paths),
                "--output",
                str(output),
            ],
            check=False,
            capture_output=True,
            text=True,
        )


if __name__ == "__main__":
    unittest.main()
