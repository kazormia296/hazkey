from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT))

from tools.dictionary import evaluate_mozc_hybrid_spike  # noqa: E402


SCRIPT = REPOSITORY_ROOT / "tools/dictionary/evaluate_mozc_hybrid_spike.py"
TOP_K = 6


CASES = (
    ("mozc-correct", "よみ1", "正解1", ["別1", "別2"], ["正解1", "M1", "M2", "M3"]),
    ("policy-rescue", "よみ2", "正解2", ["正解2", "H2"], ["誤り2", "正解2", "M2"]),
    ("policy-regression", "よみ3", "正解3", ["誤りH3", "H3"], ["正解3", "誤りH3", "M3"]),
    ("below-both", "よみ4", "正解4", ["H4", "正解4"], ["M4", "正解4"]),
    ("below-hazkey-only", "よみ5", "正解5", ["H5", "正解5"], ["M5"]),
    ("below-mozc-only", "よみ6", "正解6", ["H6"], ["M6", "正解6"]),
    ("both-absent", "よみ7", "正解7", ["H7"], ["M7"]),
    ("empty-mozc", "よみ8", "正解8", ["正解8", "H8"], []),
)


def make_result(
    case_id: str,
    reading: str,
    candidates: list[str],
    converter_backend: str,
    *,
    corpus_sha256: str,
    corpus_cases: int,
    category: str = "sample",
    source_ref: str = "0123456789abcdef",
    top_k: int = TOP_K,
) -> dict[str, object]:
    resource_kind = (
        "hazkey_dictionary"
        if converter_backend == "hazkey"
        else "mozc_runtime_inputs"
    )
    return {
        "schema": "hazkey.ab-probe-result.v3",
        "id": case_id,
        "reading": reading,
        "category": category,
        "backend": "Hazkey" if converter_backend == "hazkey" else "B0",
        "backend_version": "0.2.1",
        "source_ref": source_ref,
        "converter_backend": converter_backend,
        "resource": {
            "kind": resource_kind,
            "path": f"/fixtures/{converter_backend}",
            "fingerprint": "sha256:" + ("1" if converter_backend == "hazkey" else "2") * 64,
        },
        "top_k": top_k,
        "corpus": {"sha256": corpus_sha256, "cases": corpus_cases},
        "candidates": candidates,
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
            "rss": {"before_kib": 100, "after_kib": 120},
        },
    }


def write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )


def make_inputs(
    directory: Path,
) -> tuple[Path, Path, Path, list[dict[str, object]], list[dict[str, object]]]:
    corpus = directory / "corpus.tsv"
    corpus.write_text(
        "id\treading\texpected\tcategory\n"
        + "".join(
            f"{case_id}\t{reading}\t{expected}\tsample\n"
            for case_id, reading, expected, _, _ in CASES
        ),
        encoding="utf-8",
    )
    corpus_sha256 = "sha256:" + hashlib.sha256(corpus.read_bytes()).hexdigest()
    hazkey_records = [
        make_result(
            case_id,
            reading,
            hazkey,
            "hazkey",
            corpus_sha256=corpus_sha256,
            corpus_cases=len(CASES),
        )
        for case_id, reading, _, hazkey, _ in CASES
    ]
    # Reversed order verifies that identity matching is by case ID.
    mozc_records = [
        make_result(
            case_id,
            reading,
            mozc,
            "mozc",
            corpus_sha256=corpus_sha256,
            corpus_cases=len(CASES),
        )
        for case_id, reading, _, _, mozc in reversed(CASES)
    ]
    hazkey_path = directory / "hazkey.jsonl"
    mozc_path = directory / "mozc.jsonl"
    write_jsonl(hazkey_path, hazkey_records)
    write_jsonl(mozc_path, mozc_records)
    return corpus, hazkey_path, mozc_path, hazkey_records, mozc_records


class MozcHybridSpikeEvaluationTests(unittest.TestCase):
    def test_classifies_all_mozc_misses_and_reports_policy_effect(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            corpus, hazkey, mozc, _, _ = make_inputs(directory)
            report = evaluate_mozc_hybrid_spike.evaluate_paths(corpus, hazkey, mozc)

        self.assertEqual(
            report["schema"], "hazkey.mozc-hybrid-spike-evaluation.v1"
        )
        self.assertTrue(report["diagnostic_only"])
        self.assertFalse(report["formal_authorized"])
        self.assertTrue(report["new_holdout_required"])
        self.assertFalse(report["policy"]["uses_expected_labels"])
        self.assertEqual(
            report["runtime_policy"]["id"],
            "mozc-first-preserve-top1-h0",
        )
        self.assertEqual(
            report["policy"]["id"], "mozc-first-one-sided-consensus-v1"
        )
        self.assertEqual(
            report["top1"],
            {
                "cases": 8,
                "hazkey": {"hits": 2, "rate": 0.25},
                "mozc": {"hits": 2, "rate": 0.25},
                "hybrid": {"hits": 3, "rate": 0.375},
                "rescued": 2,
                "regressed": 1,
                "net_hits": 1,
                "net_rate": 0.125,
            },
        )
        self.assertEqual(
            report["runtime_h0_top1"],
            {
                "cases": 8,
                "hits": 3,
                "rate": 0.375,
                "rescued": 1,
                "regressed": 0,
                "net_hits": 1,
                "net_rate": 0.125,
            },
        )
        self.assertEqual(
            report["oracle_ceiling"]["backend_top1_union"]["hits"], 4
        )
        self.assertEqual(
            report["oracle_ceiling"]["candidate_union"]["hits"], 7
        )

        classification = report["mozc_top1_miss_classification"]
        self.assertTrue(classification["exhaustive"])
        self.assertTrue(classification["disjoint"])
        self.assertEqual(classification["total"], 6)
        self.assertEqual(classification["hazkey_top1_rescue"]["count"], 2)
        self.assertEqual(
            classification["below_top1_presence"],
            {
                "count": 3,
                "rate_of_mozc_top1_misses": 0.5,
                "both": 1,
                "hazkey_only": 1,
                "mozc_only": 1,
            },
        )
        self.assertEqual(classification["both_absent"]["count"], 1)

        cases = {case["id"]: case for case in report["cases"]}
        self.assertEqual(
            cases["policy-rescue"]["mozc_top1_miss_classification"],
            "hazkey_top1_rescue",
        )
        self.assertEqual(cases["policy-rescue"]["top1_outcome"], "rescued")
        self.assertEqual(
            cases["policy-rescue"]["policy_decision"],
            "promote_hazkey_one_sided_consensus",
        )
        self.assertEqual(
            cases["policy-rescue"]["candidates"]["hybrid"],
            ["正解2", "誤り2", "M2", "H2"],
        )
        self.assertEqual(cases["policy-regression"]["top1_outcome"], "regressed")
        self.assertIsNone(
            cases["policy-regression"]["mozc_top1_miss_classification"]
        )
        self.assertEqual(
            cases["mozc-correct"]["candidates"]["hybrid"],
            ["正解1", "M1", "M2", "別1", "別2", "M3"],
        )
        self.assertEqual(
            cases["below-both"]["mozc_top1_miss_classification"],
            "below_top1_both",
        )
        self.assertEqual(
            cases["below-hazkey-only"]["mozc_top1_miss_classification"],
            "below_top1_hazkey_only",
        )
        self.assertEqual(
            cases["below-mozc-only"]["mozc_top1_miss_classification"],
            "below_top1_mozc_only",
        )
        self.assertEqual(
            cases["both-absent"]["mozc_top1_miss_classification"],
            "both_absent",
        )
        self.assertEqual(
            cases["empty-mozc"]["policy_decision"],
            "hazkey_fallback_mozc_empty",
        )

    def test_merge_keeps_mozc_top3_and_deduplicates_unicode_nfc(self) -> None:
        merged, decision = evaluate_mozc_hybrid_spike.merge_candidates(
            ["H0", "e\u0301", "H1"],
            ["M0", "M1", "é", "M3", "M4"],
            6,
        )

        self.assertEqual(decision, "keep_mozc_top1")
        self.assertEqual(merged, ["M0", "M1", "é", "H0", "H1", "M3"])

    def test_merge_requires_one_sided_consensus_for_promotion(self) -> None:
        promoted, promoted_decision = evaluate_mozc_hybrid_spike.merge_candidates(
            ["H0", "H1"], ["M0", "H0", "M1"], 6
        )
        blocked, blocked_decision = evaluate_mozc_hybrid_spike.merge_candidates(
            ["H0", "M0", "H1"], ["M0", "H0", "M1"], 6
        )

        self.assertEqual(promoted_decision, "promote_hazkey_one_sided_consensus")
        self.assertEqual(promoted, ["H0", "M0", "M1", "H1"])
        self.assertEqual(blocked_decision, "keep_mozc_top1")
        self.assertEqual(blocked, ["M0", "H0", "M1", "H1"])

    def test_fails_closed_on_identity_and_case_mismatches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            corpus, hazkey, mozc, hazkey_records, mozc_records = make_inputs(directory)
            scenarios: dict[str, tuple[list[dict[str, object]], list[dict[str, object]]]] = {}

            bad_source = deepcopy(mozc_records)
            for record in bad_source:
                record["source_ref"] = "different-source"
            scenarios["source_ref"] = (deepcopy(hazkey_records), bad_source)

            bad_top_k = deepcopy(mozc_records)
            for record in bad_top_k:
                record["top_k"] = TOP_K - 1
            scenarios["top_k"] = (deepcopy(hazkey_records), bad_top_k)

            bad_hash = deepcopy(mozc_records)
            for record in bad_hash:
                record["corpus"] = {
                    "sha256": "sha256:" + "f" * 64,
                    "cases": len(CASES),
                }
            scenarios["corpus"] = (deepcopy(hazkey_records), bad_hash)

            bad_reading = deepcopy(mozc_records)
            bad_reading[0]["reading"] = "別の読み"
            scenarios["reading"] = (deepcopy(hazkey_records), bad_reading)

            bad_category = deepcopy(hazkey_records)
            bad_category[0]["category"] = "other"
            scenarios["category"] = (bad_category, deepcopy(mozc_records))

            bad_case_id = deepcopy(mozc_records)
            bad_case_id[0]["id"] = "unexpected-case"
            scenarios["case set"] = (deepcopy(hazkey_records), bad_case_id)

            wrong_backend = deepcopy(hazkey_records)
            for record in wrong_backend:
                record["converter_backend"] = "mozc"
                resource = dict(record["resource"])
                resource["kind"] = "mozc_runtime_inputs"
                record["resource"] = resource
            scenarios["backend role"] = (wrong_backend, deepcopy(mozc_records))

            for name, (hazkey_values, mozc_values) in scenarios.items():
                with self.subTest(name=name):
                    write_jsonl(hazkey, hazkey_values)
                    write_jsonl(mozc, mozc_values)
                    with self.assertRaises(ValueError):
                        evaluate_mozc_hybrid_spike.evaluate_paths(
                            corpus, hazkey, mozc
                        )

    def test_cli_writes_json_and_reports_validation_errors_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            corpus, hazkey, mozc, _, mozc_records = make_inputs(directory)
            output = directory / "report.json"
            success = self.run_cli(corpus, hazkey, mozc, output)
            report = json.loads(output.read_text(encoding="utf-8"))

            for record in mozc_records:
                record["source_ref"] = "wrong-source"
            write_jsonl(mozc, mozc_records)
            failure_output = directory / "failure.json"
            failure = self.run_cli(corpus, hazkey, mozc, failure_output)

        self.assertEqual(success.returncode, 0, success.stderr)
        self.assertTrue(report["diagnostic_only"])
        self.assertFalse(report["formal_authorized"])
        self.assertTrue(report["new_holdout_required"])
        self.assertEqual(failure.returncode, 2)
        self.assertIn("error:", failure.stderr)
        self.assertNotIn("Traceback", failure.stderr)
        self.assertFalse(failure_output.exists())

    @staticmethod
    def run_cli(
        corpus: Path, hazkey: Path, mozc: Path, output: Path
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--corpus",
                str(corpus),
                "--hazkey-results",
                str(hazkey),
                "--mozc-results",
                str(mozc),
                "--output",
                str(output),
            ],
            check=False,
            capture_output=True,
            text=True,
        )


if __name__ == "__main__":
    unittest.main()
