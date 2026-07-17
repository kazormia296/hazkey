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


def make_v2_result(
    case_id: str,
    samples: list[float | int],
    *,
    converter_backend: str = "mozc",
    resource_kind: str = "mozc_runtime_inputs",
    resource_path: str = "/fixtures/mozc.data",
    resource_fingerprint: str = "sha256:123456",
    **kwargs: object,
) -> dict[str, object]:
    result = make_result(case_id, samples, **kwargs)
    result["schema"] = "hazkey.ab-probe-result.v2"
    del result["dictionary_path"]
    del result["dictionary_fingerprint"]
    result["converter_backend"] = converter_backend
    result["resource"] = {
        "kind": resource_kind,
        "path": resource_path,
        "fingerprint": resource_fingerprint,
    }
    return result


def make_v3_result(
    case_id: str,
    samples: list[float | int],
    *,
    reading: str = "よみ",
    top_k: int = 5,
    corpus_sha256: str = "sha256:" + "a" * 64,
    corpus_cases: int = 1,
    **kwargs: object,
) -> dict[str, object]:
    result = make_v2_result(case_id, samples, **kwargs)
    result["schema"] = "hazkey.ab-probe-result.v3"
    result["reading"] = reading
    result["top_k"] = top_k
    result["corpus"] = {
        "sha256": corpus_sha256,
        "cases": corpus_cases,
    }
    return result


def make_v4_result(
    case_id: str,
    samples: list[float | int],
    *,
    candidates: list[tuple[str, int]] | None = None,
    conversion_path: str = "segment_candidates",
    **kwargs: object,
) -> dict[str, object]:
    result = make_v3_result(case_id, samples, **kwargs)
    result["schema"] = "hazkey.ab-probe-result.v4"
    result["conversion_path"] = conversion_path
    candidate_values = candidates or [(f"candidate-{case_id}", 1)]
    result["candidates"] = [
        {
            "text": text,
            "rank": index,
            "consuming_count": consuming_count,
        }
        for index, (text, consuming_count) in enumerate(
            candidate_values, start=1
        )
    ]
    return result


def make_v5_result(
    case_id: str,
    samples: list[float | int],
    *,
    composition_count: int = 1,
    composition_start: int = 0,
    composition_unit: str = "composition_element",
    **kwargs: object,
) -> dict[str, object]:
    result = make_v4_result(case_id, samples, **kwargs)
    result["schema"] = "hazkey.ab-probe-result.v5"
    result["composition_span"] = {
        "start": composition_start,
        "count": composition_count,
        "unit": composition_unit,
    }
    return result


def make_v6_result(
    case_id: str,
    samples: list[float | int],
    *,
    zenzai_enabled: bool = True,
    zenzai_score: float | None = -1.25,
    zenzai_score_token_count: int = 2,
    zenzai_score_scope: str = "full_candidate",
    ranking_influence: str | None = None,
    **kwargs: object,
) -> dict[str, object]:
    kwargs.setdefault("converter_backend", "hazkey")
    kwargs.setdefault("resource_kind", "hazkey_dictionary")
    kwargs.setdefault("resource_path", "/fixtures/dictionary")
    result = make_v5_result(case_id, samples, **kwargs)
    result["schema"] = "hazkey.ab-probe-result.v6"
    influence = ranking_influence or (
        "zenzai" if zenzai_enabled else "standard"
    )
    for candidate in result["candidates"]:
        candidate.update(
            {
                "provenance": "standard",
                "ranking_influence": influence,
                "zenzai_score": zenzai_score if zenzai_enabled else None,
                "zenzai_score_token_count": (
                    zenzai_score_token_count
                    if zenzai_enabled and zenzai_score is not None
                    else None
                ),
                "zenzai_score_scope": (
                    zenzai_score_scope
                    if zenzai_enabled and zenzai_score is not None
                    else None
                ),
            }
        )
    result["producer"] = {
        "path": "/fixtures/hazkey-server",
        "size_bytes": 123456,
        "sha256": "sha256:" + "b" * 64,
    }
    result["quality_policy"] = {
        "learning": False,
        "context": "empty",
        "zenzai": {
            "enabled": zenzai_enabled,
            "model_path": "/fixtures/zenzai.gguf" if zenzai_enabled else None,
            "model_size_bytes": 654321 if zenzai_enabled else None,
            "model_sha256": (
                "sha256:" + "c" * 64 if zenzai_enabled else None
            ),
            "inference_limit": 10 if zenzai_enabled else None,
            "resolved_device": "CPU" if zenzai_enabled else None,
        },
    }
    return result


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
        self.assertEqual(summary["max_observed_total_rss_kib"], 200)
        self.assertIsNone(summary["max_observed_parent_pss_kib"])
        self.assertIsNone(summary["max_observed_backend_rss_kib"])
        self.assertIsNone(summary["max_observed_backend_pss_kib"])
        self.assertIsNone(summary["max_observed_total_pss_kib"])
        self.assertIsNone(summary["max_backend_process_launch_count"])
        self.assertIsNone(summary["max_backend_cleanup_failure_count"])
        self.assertNotIn("converter_backend", summary)
        self.assertNotIn("peak_rss_kib", summary)

    def test_summarize_aggregates_optional_memory_and_backend_diagnostics(self) -> None:
        first_result = make_result("one", [1, 2], rss_before=90, rss_after=100)
        first_result["measurement"]["rss"].update(
            {
                "before_pss_kib": 80,
                "after_pss_kib": 90,
                "backend_before_kib": 100,
                "backend_after_kib": 150,
                "backend_before_pss_kib": 30,
                "backend_after_pss_kib": 40,
            }
        )
        first_result["measurement"]["backend_diagnostics"] = {
            "process_launch_count": 1,
            "cleanup_failure_count": 0,
        }
        second_result = make_result("two", [3, 4], rss_before=400, rss_after=160)
        second_result["measurement"]["rss"].update(
            {
                "before_pss_kib": 400,
                "after_pss_kib": 160,
                "backend_before_kib": None,
                "backend_after_kib": 300,
                "backend_before_pss_kib": None,
                "backend_after_pss_kib": 120,
            }
        )
        second_result["measurement"]["backend_diagnostics"] = {
            "process_launch_count": 4,
            "cleanup_failure_count": None,
        }
        incomplete_result = make_result(
            "three", [5, 6], rss_before=900, rss_after=1_000
        )
        incomplete_result["measurement"]["rss"].update(
            {"before_pss_kib": 900, "after_pss_kib": 1_000}
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            run_path = Path(temporary_directory) / "run.jsonl"
            write_run(run_path, [first_result, second_result, incomplete_result])
            summary = summarize_ab_probe.summarize([run_path])

        self.assertEqual(summary["max_observed_rss_kib"], 1_000)
        self.assertEqual(summary["max_observed_total_rss_kib"], 460)
        self.assertEqual(summary["max_observed_parent_pss_kib"], 1_000)
        self.assertEqual(summary["max_observed_backend_rss_kib"], 300)
        self.assertEqual(summary["max_observed_backend_pss_kib"], 120)
        # Incomplete snapshots are omitted, including cases with no backend fields.
        self.assertEqual(summary["max_observed_total_pss_kib"], 280)
        self.assertEqual(summary["max_backend_process_launch_count"], 4)
        self.assertEqual(summary["max_backend_cleanup_failure_count"], 0)

    def test_summarize_supports_v2_resource_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_path = Path(temporary_directory) / "run.jsonl"
            write_run(run_path, [make_v2_result("case", [1, 2])])
            summary = summarize_ab_probe.summarize([run_path])

        self.assertEqual(summary["schema"], "hazkey.ab-probe-summary.v2")
        self.assertEqual(summary["converter_backend"], "mozc")
        self.assertIsNone(summary["max_observed_total_rss_kib"])
        self.assertIsNone(summary["max_observed_total_pss_kib"])
        self.assertEqual(
            summary["provenance"],
            {
                "source_ref": "0123456789abcdef",
                "resource": {
                    "kind": "mozc_runtime_inputs",
                    "path": "/fixtures/mozc.data",
                    "fingerprint": "sha256:123456",
                },
            },
        )

    def test_summarize_supports_v3_corpus_provenance_and_top_k(self) -> None:
        first = make_v3_result(
            "one", [1, 2], reading="よみいち", corpus_cases=2
        )
        second = make_v3_result(
            "two", [3, 4], reading="よみに", corpus_cases=2
        )
        first["candidates"] = ["候補一", "候補二"]
        second["candidates"] = ["候補三"]
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            first_run = directory / "first.jsonl"
            second_run = directory / "second.jsonl"
            write_run(first_run, [first, second])
            write_run(
                second_run,
                [
                    make_v3_result(
                        "two", [5, 6], reading="よみに", corpus_cases=2
                    ),
                    make_v3_result(
                        "one", [7, 8], reading="よみいち", corpus_cases=2
                    ),
                ],
            )
            # v3 keeps the v2 candidate-repeatability contract.
            second_run_payloads = [
                make_v3_result(
                    "two", [5, 6], reading="よみに", corpus_cases=2
                ),
                make_v3_result(
                    "one", [7, 8], reading="よみいち", corpus_cases=2
                ),
            ]
            second_run_payloads[0]["candidates"] = ["候補三"]
            second_run_payloads[1]["candidates"] = ["候補一", "候補二"]
            write_run(second_run, second_run_payloads)
            summary = summarize_ab_probe.summarize([first_run, second_run])

        self.assertEqual(summary["schema"], "hazkey.ab-probe-summary.v3")
        self.assertEqual(summary["converter_backend"], "mozc")
        self.assertEqual(summary["top_k"], 5)
        self.assertEqual(
            summary["provenance"],
            {
                "source_ref": "0123456789abcdef",
                "resource": {
                    "kind": "mozc_runtime_inputs",
                    "path": "/fixtures/mozc.data",
                    "fingerprint": "sha256:123456",
                },
                "corpus": {
                    "sha256": "sha256:" + "a" * 64,
                    "cases": 2,
                },
            },
        )

    def test_summarize_supports_v4_ranked_boundary_candidates(self) -> None:
        first = make_v4_result(
            "one",
            [1, 2],
            reading="よみいち",
            corpus_cases=2,
            candidates=[("候補一", 3), ("候補二", 2)],
        )
        second = make_v4_result(
            "two",
            [3, 4],
            reading="よみに",
            corpus_cases=2,
            candidates=[("候補三", 4)],
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            first_run = directory / "first.jsonl"
            second_run = directory / "second.jsonl"
            write_run(first_run, [first, second])
            write_run(
                second_run,
                [
                    make_v4_result(
                        "two",
                        [5, 6],
                        reading="よみに",
                        corpus_cases=2,
                        candidates=[("候補三", 4)],
                    ),
                    make_v4_result(
                        "one",
                        [7, 8],
                        reading="よみいち",
                        corpus_cases=2,
                        candidates=[("候補一", 3), ("候補二", 2)],
                    ),
                ],
            )
            summary = summarize_ab_probe.summarize([first_run, second_run])

        self.assertEqual(summary["schema"], "hazkey.ab-probe-summary.v4")
        self.assertEqual(summary["top_k"], 5)
        self.assertEqual(summary["conversion_path"], "segment_candidates")
        self.assertEqual(
            summary["provenance"]["corpus"],
            {"sha256": "sha256:" + "a" * 64, "cases": 2},
        )

    def test_v4_requires_segment_candidates_conversion_path(self) -> None:
        missing = make_v4_result("case", [1, 2])
        del missing["conversion_path"]
        wrong = make_v4_result(
            "case", [1, 2], conversion_path="request_candidates"
        )

        self.assert_v4_scenarios_rejected(
            {"missing-path": missing, "wrong-path": wrong}
        )

    def test_v4_rejects_malformed_ranked_boundary_candidates(self) -> None:
        scenarios: dict[str, dict[str, object]] = {}

        non_object = make_v4_result("case", [1, 2])
        non_object["candidates"] = ["candidate"]
        scenarios["non-object"] = non_object

        for field in ("text", "rank", "consuming_count"):
            missing = make_v4_result("case", [1, 2])
            del missing["candidates"][0][field]
            scenarios[f"missing-{field}"] = missing

        extra = make_v4_result("case", [1, 2])
        extra["candidates"][0]["annotation"] = None
        scenarios["extra-field"] = extra

        empty_text = make_v4_result("case", [1, 2])
        empty_text["candidates"][0]["text"] = ""
        scenarios["empty-text"] = empty_text

        for label, rank in (("zero-rank", 0), ("bool-rank", True), ("gap-rank", 2)):
            invalid_rank = make_v4_result("case", [1, 2])
            invalid_rank["candidates"][0]["rank"] = rank
            scenarios[label] = invalid_rank

        for label, consuming_count in (
            ("zero-consuming-count", 0),
            ("bool-consuming-count", True),
        ):
            invalid_count = make_v4_result("case", [1, 2])
            invalid_count["candidates"][0]["consuming_count"] = consuming_count
            scenarios[label] = invalid_count

        overflow = make_v4_result(
            "case",
            [1, 2],
            top_k=1,
            candidates=[("one", 1), ("two", 1)],
        )
        scenarios["candidate-overflow"] = overflow

        self.assert_v4_scenarios_rejected(scenarios)

    def test_v4_rejects_candidate_boundary_drift_between_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            first = directory / "first.jsonl"
            second = directory / "second.jsonl"
            write_run(first, [make_v4_result("case", [1, 2])])
            changed = make_v4_result("case", [3, 4])
            changed["candidates"][0]["consuming_count"] = 2
            write_run(second, [changed])

            with self.assertRaisesRegex(ValueError, "candidates.*do not match"):
                summarize_ab_probe.summarize([first, second])

    def test_summarize_supports_v5_composition_span_evidence(self) -> None:
        first_cases = [
            make_v5_result(
                "one",
                [1, 2],
                reading="よみいち",
                corpus_cases=2,
                composition_count=3,
                candidates=[("全文一", 3), ("部分一", 2)],
            ),
            make_v5_result(
                "two",
                [3, 4],
                reading="よみに",
                corpus_cases=2,
                composition_count=4,
                candidates=[("部分二", 2), ("全文二", 4)],
            ),
        ]
        second_cases = [
            make_v5_result(
                "two",
                [5, 6],
                reading="よみに",
                corpus_cases=2,
                composition_count=4,
                candidates=[("部分二", 2), ("全文二", 4)],
            ),
            make_v5_result(
                "one",
                [7, 8],
                reading="よみいち",
                corpus_cases=2,
                composition_count=3,
                candidates=[("全文一", 3), ("部分一", 2)],
            ),
        ]
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            first_run = directory / "first.jsonl"
            second_run = directory / "second.jsonl"
            write_run(first_run, first_cases)
            write_run(second_run, second_cases)

            loaded = summarize_ab_probe.load_run(first_run)
            summary = summarize_ab_probe.summarize([first_run, second_run])

        self.assertTrue(loaded["composition_span_available"])
        self.assertEqual(
            loaded["cases"]["one"]["composition_span"],
            {"start": 0, "count": 3, "unit": "composition_element"},
        )
        self.assertEqual(
            loaded["cases"]["one"]["whole_span_candidate_count"], 1
        )
        self.assertEqual(
            loaded["cases"]["two"]["whole_span_candidate_count"], 1
        )
        self.assertEqual(summary["schema"], "hazkey.ab-probe-summary.v5")
        self.assertEqual(summary["conversion_path"], "segment_candidates")
        self.assertEqual(
            summary["composition_span_evidence"],
            {
                "available": True,
                "unit": "composition_element",
                "start": 0,
                "min_count": 3,
                "max_count": 4,
                "cases_with_top1_consuming_full_span": 1,
                "rate_with_top1_consuming_full_span": 0.5,
            },
        )

    def test_v5_requires_strict_composition_span(self) -> None:
        scenarios: dict[str, dict[str, object]] = {}

        missing_span = make_v5_result("case", [1, 2])
        del missing_span["composition_span"]
        scenarios["missing-span"] = missing_span

        non_object = make_v5_result("case", [1, 2])
        non_object["composition_span"] = []
        scenarios["non-object"] = non_object

        for field in ("start", "count", "unit"):
            missing = make_v5_result("case", [1, 2])
            del missing["composition_span"][field]
            scenarios[f"missing-{field}"] = missing

        extra = make_v5_result("case", [1, 2])
        extra["composition_span"]["end"] = 1
        scenarios["extra-field"] = extra

        for label, start in (("nonzero-start", 1), ("bool-start", False)):
            invalid = make_v5_result("case", [1, 2])
            invalid["composition_span"]["start"] = start
            scenarios[label] = invalid

        for label, count in (
            ("zero-count", 0),
            ("bool-count", True),
            ("string-count", "1"),
        ):
            invalid = make_v5_result("case", [1, 2])
            invalid["composition_span"]["count"] = count
            scenarios[label] = invalid

        for label, unit in (
            ("wrong-unit", "character"),
            ("empty-unit", ""),
        ):
            invalid = make_v5_result("case", [1, 2])
            invalid["composition_span"]["unit"] = unit
            scenarios[label] = invalid

        overflowing_candidate = make_v5_result(
            "case",
            [1, 2],
            composition_count=1,
            candidates=[("too-long", 2)],
        )
        scenarios["candidate-exceeds-span"] = overflowing_candidate

        self.assert_v5_scenarios_rejected(scenarios)

    def test_v5_requires_segment_candidates_conversion_path(self) -> None:
        missing = make_v5_result("case", [1, 2])
        del missing["conversion_path"]
        wrong = make_v5_result(
            "case", [1, 2], conversion_path="request_candidates"
        )

        self.assert_v5_scenarios_rejected(
            {"missing-path": missing, "wrong-path": wrong}
        )

    def test_v5_rejects_composition_span_drift_between_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            first = directory / "first.jsonl"
            second = directory / "second.jsonl"
            write_run(
                first,
                [make_v5_result("case", [1, 2], composition_count=2)],
            )
            write_run(
                second,
                [make_v5_result("case", [3, 4], composition_count=3)],
            )

            with self.assertRaisesRegex(
                ValueError, "composition_span.*does not match"
            ):
                summarize_ab_probe.summarize([first, second])

    def test_summarize_supports_v6_zenzai_evidence(self) -> None:
        cases = [
            make_v6_result(
                "one",
                [1],
                reading="よみいち",
                corpus_cases=2,
                composition_count=4,
                candidates=[("候補一", 2), ("候補二", 2)],
                zenzai_score=-3.5,
            ),
            make_v6_result(
                "two",
                [3],
                reading="よみに",
                corpus_cases=2,
                composition_count=3,
                candidates=[("候補三", 3)],
                zenzai_score=None,
            ),
        ]
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "v6.jsonl"
            write_run(path, cases)

            loaded = summarize_ab_probe.load_run(path)
            summary = summarize_ab_probe.summarize([path])

        self.assertEqual(loaded["schema"], "hazkey.ab-probe-result.v6")
        self.assertTrue(loaded["composition_span_available"])
        self.assertTrue(loaded["quality_policy"]["zenzai"]["enabled"])
        self.assertEqual(summary["schema"], "hazkey.ab-probe-summary.v6")
        self.assertEqual(summary["zenzai_evidence"]["candidates"], 3)
        self.assertEqual(
            summary["zenzai_evidence"]["ranking_influenced_candidates"], 3
        )
        self.assertEqual(summary["zenzai_evidence"]["scored_candidates"], 2)
        self.assertEqual(
            summary["zenzai_evidence"][
                "score_coverage_of_influenced_candidates"
            ],
            2 / 3,
        )
        self.assertEqual(summary["zenzai_evidence"]["minimum_score"], -3.5)
        self.assertEqual(
            summary["zenzai_evidence"]["minimum_score_per_token"], -1.75
        )
        self.assertEqual(
            summary["zenzai_evidence"]["score_scope_counts"],
            {"full_candidate": 2, "constraint_suffix": 0},
        )

    def test_v6_rejects_forged_zenzai_policy_and_candidate_evidence(self) -> None:
        scenarios: dict[str, dict[str, object]] = {}

        bad_score = make_v6_result("case", [1])
        bad_score["candidates"][0]["zenzai_score"] = "not-a-score"
        scenarios["bad-score"] = bad_score

        partial_score = make_v6_result("case", [1])
        partial_score["candidates"][0]["zenzai_score_scope"] = None
        scenarios["partial-score"] = partial_score

        bad_scope = make_v6_result(
            "case", [1], zenzai_score_scope="unknown"
        )
        scenarios["bad-score-scope"] = bad_scope

        standard_score = make_v6_result(
            "case", [1], ranking_influence="standard"
        )
        scenarios["standard-score"] = standard_score

        disabled_evidence = make_v6_result("case", [1, 2], zenzai_enabled=False)
        disabled_evidence["candidates"][0]["ranking_influence"] = "zenzai"
        scenarios["disabled-evidence"] = disabled_evidence

        bad_model_hash = make_v6_result("case", [1])
        bad_model_hash["quality_policy"]["zenzai"]["model_sha256"] = "bad"
        scenarios["bad-model-hash"] = bad_model_hash

        mozc_zenzai = make_v6_result(
            "case",
            [1],
            converter_backend="mozc",
            resource_kind="mozc_runtime_inputs",
        )
        scenarios["mozc-zenzai"] = mozc_zenzai

        multi_iteration = make_v6_result("case", [1, 2])
        scenarios["zenzai-multi-iteration"] = multi_iteration

        no_observed_score = make_v6_result(
            "case", [1], zenzai_score=None
        )
        scenarios["zenzai-no-observed-score"] = no_observed_score

        self.assert_v6_scenarios_rejected(scenarios)

    def test_load_run_bytes_parses_an_immutable_snapshot(self) -> None:
        payload = make_v3_result("case", [1, 2])
        data = (json.dumps(payload) + "\n").encode("utf-8")

        run = summarize_ab_probe.load_run_bytes(data, "snapshot.jsonl")

        self.assertEqual(run["path"], "snapshot.jsonl")
        self.assertEqual(run["schema"], "hazkey.ab-probe-result.v3")
        self.assertEqual(run["cases"]["case"]["reading"], "よみ")

    def test_load_run_reads_the_file_once_as_bytes(self) -> None:
        payload = make_result("case", [1, 2])
        data = (json.dumps(payload) + "\n").encode("utf-8")

        class ReadOncePath:
            reads = 0

            def read_bytes(self) -> bytes:
                self.reads += 1
                if self.reads > 1:
                    raise AssertionError("path was read more than once")
                return data

            def __str__(self) -> str:
                return "read-once.jsonl"

        path = ReadOncePath()
        run = summarize_ab_probe.load_run(path)  # type: ignore[arg-type]

        self.assertEqual(path.reads, 1)
        self.assertEqual(set(run["cases"]), {"case"})

    def test_v3_rejects_invalid_reading(self) -> None:
        scenarios: dict[str, dict[str, object]] = {}
        for label, value in (("empty", ""), ("non-string", 123)):
            result = make_v3_result("case", [1, 2])
            result["reading"] = value
            scenarios[label] = result
        missing = make_v3_result("case", [1, 2])
        del missing["reading"]
        scenarios["missing"] = missing

        self.assert_v3_scenarios_rejected(scenarios)

    def test_v3_rejects_invalid_top_k(self) -> None:
        scenarios: dict[str, dict[str, object]] = {}
        for label, value in (
            ("zero", 0),
            ("too-large", 11),
            ("boolean", True),
            ("non-integer", "5"),
        ):
            result = make_v3_result("case", [1, 2])
            result["top_k"] = value
            scenarios[label] = result
        missing = make_v3_result("case", [1, 2])
        del missing["top_k"]
        scenarios["missing"] = missing

        self.assert_v3_scenarios_rejected(scenarios)

    def test_v3_rejects_invalid_corpus(self) -> None:
        scenarios: dict[str, dict[str, object]] = {}
        for label, sha256 in (
            ("short-hash", "sha256:abc"),
            ("uppercase-hash", "sha256:" + "A" * 64),
            ("wrong-prefix", "sha512:" + "a" * 64),
        ):
            result = make_v3_result("case", [1, 2])
            result["corpus"]["sha256"] = sha256
            scenarios[label] = result
        for label, cases in (
            ("zero-cases", 0),
            ("boolean-cases", True),
            ("non-integer-cases", "2"),
        ):
            result = make_v3_result("case", [1, 2])
            result["corpus"]["cases"] = cases
            scenarios[label] = result
        missing = make_v3_result("case", [1, 2])
        del missing["corpus"]
        scenarios["missing-corpus"] = missing
        extra = make_v3_result("case", [1, 2])
        extra["corpus"]["path"] = "/fixtures/corpus.tsv"
        scenarios["extra-field"] = extra

        self.assert_v3_scenarios_rejected(scenarios)

    def test_v3_rejects_more_candidates_than_top_k(self) -> None:
        result = make_v3_result("case", [1, 2], top_k=1)
        result["candidates"] = ["one", "two"]

        self.assert_v3_scenarios_rejected({"candidate-overflow": result})

    def test_v3_rejects_corpus_case_count_mismatch(self) -> None:
        result = make_v3_result("case", [1, 2], corpus_cases=2)

        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "run.jsonl"
            write_run(path, [result])
            with self.assertRaisesRegex(
                ValueError, "corpus.cases does not match result count"
            ):
                summarize_ab_probe.load_run(path)

    def test_v3_rejects_top_k_corpus_and_reading_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            within = directory / "within.jsonl"
            first_run = directory / "first.jsonl"
            second_run = directory / "second.jsonl"

            for field in ("top_k", "corpus"):
                with self.subTest(field=field, scope="within-run"):
                    first_case = make_v3_result(
                        "one", [1, 2], corpus_cases=2
                    )
                    second_case = make_v3_result(
                        "two", [3, 4], corpus_cases=2
                    )
                    if field == "top_k":
                        second_case[field] = 4
                    else:
                        second_case[field] = {
                            "sha256": "sha256:" + "b" * 64,
                            "cases": 2,
                        }
                    write_run(within, [first_case, second_case])
                    with self.assertRaisesRegex(
                        ValueError, rf"inconsistent {field} within run"
                    ):
                        summarize_ab_probe.summarize([within])

                with self.subTest(field=field, scope="across-runs"):
                    write_run(first_run, [make_v3_result("case", [1, 2])])
                    changed = make_v3_result("case", [3, 4])
                    if field == "top_k":
                        changed[field] = 4
                    else:
                        changed[field] = {
                            "sha256": "sha256:" + "b" * 64,
                            "cases": 1,
                        }
                    write_run(second_run, [changed])
                    with self.assertRaisesRegex(
                        ValueError, rf"{field} does not match the first run"
                    ):
                        summarize_ab_probe.summarize([first_run, second_run])

            write_run(
                first_run,
                [make_v3_result("case", [1, 2], reading="same-reading")],
            )
            write_run(
                second_run,
                [make_v3_result("case", [3, 4], reading="changed-reading")],
            )
            with self.assertRaisesRegex(
                ValueError, "reading for case 'case' does not match"
            ):
                summarize_ab_probe.summarize([first_run, second_run])

    def test_summarize_rejects_mixed_v1_and_v2_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            first = directory / "v1.jsonl"
            second = directory / "v2.jsonl"
            write_run(first, [make_result("case", [1, 2])])
            write_run(second, [make_v2_result("case", [3, 4])])

            with self.assertRaisesRegex(ValueError, "cannot mix"):
                summarize_ab_probe.summarize([first, second])

    def test_summarize_rejects_candidate_drift_between_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            first = directory / "first.jsonl"
            second = directory / "second.jsonl"
            write_run(first, [make_v2_result("case", [1, 2])])
            changed = make_v2_result("case", [3, 4])
            changed["candidates"] = ["different-candidate"]
            write_run(second, [changed])

            with self.assertRaisesRegex(ValueError, "candidates.*do not match"):
                summarize_ab_probe.summarize([first, second])

    def test_v1_preserves_candidate_drift_compatibility(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            first = directory / "first.jsonl"
            second = directory / "second.jsonl"
            write_run(first, [make_result("case", [1, 2])])
            changed = make_result("case", [3, 4])
            changed["candidates"] = ["different-candidate"]
            write_run(second, [changed])

            summary = summarize_ab_probe.summarize([first, second])

        self.assertEqual(summary["schema"], "hazkey.ab-probe-summary.v1")
        self.assertEqual(summary["runs"], 2)

    def test_optional_measurements_reject_invalid_values(self) -> None:
        scenarios: dict[str, dict[str, object]] = {}

        negative_pss = make_result("case", [1, 2])
        negative_pss["measurement"]["rss"]["before_pss_kib"] = -1
        scenarios["negative pss"] = negative_pss

        bool_backend_rss = make_result("case", [1, 2])
        bool_backend_rss["measurement"]["rss"]["backend_after_kib"] = True
        scenarios["bool backend rss"] = bool_backend_rss

        invalid_diagnostics = make_result("case", [1, 2])
        invalid_diagnostics["measurement"]["backend_diagnostics"] = []
        scenarios["non-object diagnostics"] = invalid_diagnostics

        negative_launch_count = make_result("case", [1, 2])
        negative_launch_count["measurement"]["backend_diagnostics"] = {
            "process_launch_count": -1,
            "cleanup_failure_count": None,
        }
        scenarios["negative launch count"] = negative_launch_count

        bool_cleanup_count = make_result("case", [1, 2])
        bool_cleanup_count["measurement"]["backend_diagnostics"] = {
            "process_launch_count": None,
            "cleanup_failure_count": False,
        }
        scenarios["bool cleanup count"] = bool_cleanup_count

        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            for name, payload in scenarios.items():
                with self.subTest(name=name):
                    path = directory / f"{name}.jsonl"
                    write_run(path, [payload])
                    with self.assertRaises(ValueError):
                        summarize_ab_probe.summarize([path])

    def test_backend_diagnostics_allows_omitted_or_null_counts(self) -> None:
        result = make_v2_result(
            "case",
            [1, 2],
            converter_backend="hazkey",
            resource_kind="hazkey_dictionary",
        )
        result["measurement"]["rss"].update(
            {"before_pss_kib": 80, "after_pss_kib": 90}
        )
        result["measurement"]["backend_diagnostics"] = {
            "cleanup_failure_count": None,
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "run.jsonl"
            write_run(path, [result])
            summary = summarize_ab_probe.summarize([path])

        self.assertIsNone(summary["max_backend_process_launch_count"])
        self.assertIsNone(summary["max_backend_cleanup_failure_count"])
        self.assertEqual(summary["converter_backend"], "hazkey")
        self.assertEqual(summary["max_observed_total_rss_kib"], 120)
        self.assertEqual(summary["max_observed_total_pss_kib"], 90)

    def test_v2_fails_closed_for_invalid_resource_and_converter(self) -> None:
        scenarios: dict[str, dict[str, object]] = {}
        for field in ("kind", "path", "fingerprint"):
            missing = make_v2_result("case", [1, 2])
            del missing["resource"][field]
            scenarios[f"missing resource {field}"] = missing

            empty = make_v2_result("case", [1, 2])
            empty["resource"][field] = ""
            scenarios[f"empty resource {field}"] = empty

        missing_converter = make_v2_result("case", [1, 2])
        del missing_converter["converter_backend"]
        scenarios["missing converter"] = missing_converter

        empty_converter = make_v2_result("case", [1, 2])
        empty_converter["converter_backend"] = ""
        scenarios["empty converter"] = empty_converter

        unknown_converter = make_v2_result("case", [1, 2])
        unknown_converter["converter_backend"] = "unknown"
        scenarios["unknown converter"] = unknown_converter

        mismatched_kind = make_v2_result("case", [1, 2])
        mismatched_kind["resource"]["kind"] = "hazkey_dictionary"
        scenarios["mismatched converter and resource"] = mismatched_kind

        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            for name, payload in scenarios.items():
                with self.subTest(name=name):
                    path = directory / f"{name}.jsonl"
                    write_run(path, [payload])
                    with self.assertRaises(ValueError):
                        summarize_ab_probe.summarize([path])

    def test_v2_rejects_inconsistent_resource_or_converter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            first = directory / "first.jsonl"
            second = directory / "second.jsonl"
            write_run(first, [make_v2_result("case", [1, 2])])

            for field, changed in (
                ("resource", make_v2_result("case", [3, 4], resource_path="/other")),
                (
                    "converter_backend",
                    make_v2_result(
                        "case",
                        [3, 4],
                        converter_backend="hazkey",
                        resource_kind="hazkey_dictionary",
                    ),
                ),
            ):
                with self.subTest(field=field):
                    write_run(second, [changed])
                    with self.assertRaisesRegex(
                        ValueError, rf"{field} does not match the first run"
                    ):
                        summarize_ab_probe.summarize([first, second])

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

    def assert_v3_scenarios_rejected(
        self, scenarios: dict[str, dict[str, object]]
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            for name, payload in scenarios.items():
                with self.subTest(name=name):
                    path = directory / f"{name}.jsonl"
                    write_run(path, [payload])
                    with self.assertRaises(ValueError):
                        summarize_ab_probe.summarize([path])

    def assert_v4_scenarios_rejected(
        self, scenarios: dict[str, dict[str, object]]
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            for name, payload in scenarios.items():
                with self.subTest(name=name):
                    path = directory / f"{name}.jsonl"
                    write_run(path, [payload])
                    with self.assertRaises(ValueError):
                        summarize_ab_probe.summarize([path])

    def assert_v5_scenarios_rejected(
        self, scenarios: dict[str, dict[str, object]]
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            for name, payload in scenarios.items():
                with self.subTest(name=name):
                    path = directory / f"{name}.jsonl"
                    write_run(path, [payload])
                    with self.assertRaises(ValueError):
                        summarize_ab_probe.summarize([path])

    def assert_v6_scenarios_rejected(
        self, scenarios: dict[str, dict[str, object]]
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            for name, payload in scenarios.items():
                with self.subTest(name=name):
                    path = directory / f"{name}.jsonl"
                    write_run(path, [payload])
                    with self.assertRaises(ValueError):
                        summarize_ab_probe.summarize([path])


if __name__ == "__main__":
    unittest.main()
