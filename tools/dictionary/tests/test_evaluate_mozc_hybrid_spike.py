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


def make_v4_result(
    case_id: str,
    reading: str,
    candidates: list[tuple[str, int]],
    converter_backend: str,
    *,
    corpus_sha256: str,
    corpus_cases: int,
    conversion_path: str = "segment_candidates",
    **kwargs: object,
) -> dict[str, object]:
    result = make_result(
        case_id,
        reading,
        [text for text, _ in candidates],
        converter_backend,
        corpus_sha256=corpus_sha256,
        corpus_cases=corpus_cases,
        **kwargs,
    )
    result["schema"] = "hazkey.ab-probe-result.v4"
    result["conversion_path"] = conversion_path
    result["candidates"] = [
        {
            "text": text,
            "rank": rank,
            "consuming_count": consuming_count,
        }
        for rank, (text, consuming_count) in enumerate(candidates, start=1)
    ]
    return result


def make_v5_result(
    case_id: str,
    reading: str,
    candidates: list[tuple[str, int]],
    converter_backend: str,
    *,
    corpus_sha256: str,
    corpus_cases: int,
    composition_count: int,
    composition_start: int = 0,
    composition_unit: str = "composition_element",
    **kwargs: object,
) -> dict[str, object]:
    result = make_v4_result(
        case_id,
        reading,
        candidates,
        converter_backend,
        corpus_sha256=corpus_sha256,
        corpus_cases=corpus_cases,
        **kwargs,
    )
    result["schema"] = "hazkey.ab-probe-result.v5"
    result["composition_span"] = {
        "start": composition_start,
        "count": composition_count,
        "unit": composition_unit,
    }
    return result


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


def make_v4_inputs(
    directory: Path,
) -> tuple[Path, Path, Path, list[dict[str, object]], list[dict[str, object]]]:
    cases = (
        (
            "boundary-eligible",
            "よみ1",
            "H0",
            [("H0", 2), ("HX", 2)],
            [("M0", 2), ("H0", 2), ("同", 1), ("同", 2)],
        ),
        (
            "boundary-rejected",
            "よみ2",
            "M1",
            [("H1", 1)],
            [("M1", 2), ("H1", 2)],
        ),
        (
            "top1-mismatch-only",
            "よみ3",
            "M2",
            [("X", 1), ("eligible", 2)],
            [("M2", 2)],
        ),
    )
    corpus = directory / "corpus-v4.tsv"
    corpus.write_text(
        "id\treading\texpected\tcategory\n"
        + "".join(
            f"{case_id}\t{reading}\t{expected}\tsample\n"
            for case_id, reading, expected, _, _ in cases
        ),
        encoding="utf-8",
    )
    corpus_sha256 = "sha256:" + hashlib.sha256(corpus.read_bytes()).hexdigest()
    hazkey_records = [
        make_v4_result(
            case_id,
            reading,
            hazkey,
            "hazkey",
            corpus_sha256=corpus_sha256,
            corpus_cases=len(cases),
        )
        for case_id, reading, _, hazkey, _ in cases
    ]
    mozc_records = [
        make_v4_result(
            case_id,
            reading,
            mozc,
            "mozc",
            corpus_sha256=corpus_sha256,
            corpus_cases=len(cases),
        )
        for case_id, reading, _, _, mozc in reversed(cases)
    ]
    hazkey_path = directory / "hazkey-v4.jsonl"
    mozc_path = directory / "mozc-v4.jsonl"
    write_jsonl(hazkey_path, hazkey_records)
    write_jsonl(mozc_path, mozc_records)
    return corpus, hazkey_path, mozc_path, hazkey_records, mozc_records


def make_v5_inputs(
    directory: Path,
) -> tuple[Path, Path, Path, list[dict[str, object]], list[dict[str, object]]]:
    cases = (
        (
            "whole-span-rescue",
            "よみ",
            "正解R",
            "proper-noun",
            2,
            [("正解R", 2), ("候補R", 2)],
            [("誤りR", 2), ("正解R", 2)],
        ),
        (
            "whole-span-regression",
            "かな",
            "正解G",
            "proper-noun",
            2,
            [("誤りG", 2)],
            [("正解G", 2), ("誤りG", 2)],
        ),
        (
            "same-surface-wrong-count",
            "てすと",
            "正解C",
            "proper-noun",
            3,
            [("候補C", 3), ("正解C", 2)],
            [("誤りC", 3), ("正解C", 2)],
        ),
        (
            "partial-span-excluded",
            "ぜんぶ",
            "全文正解",
            "proper-noun",
            3,
            [("部分H", 2)],
            [("部分M", 2)],
        ),
        (
            "width-guarded-regression",
            "しがつ",
            "４月",
            "proper-noun",
            3,
            [("4月", 3)],
            [("４月", 3), ("4月", 3)],
        ),
        (
            "protected-quality-excluded",
            "ほご",
            "保護正解",
            "protected",
            2,
            [("保護誤りH", 2)],
            [("保護誤りM", 2)],
        ),
    )
    corpus = directory / "corpus-v5.tsv"
    corpus.write_text(
        "id\treading\texpected\tcategory\n"
        + "".join(
            f"{case_id}\t{reading}\t{expected}\t{category}\n"
            for case_id, reading, expected, category, _, _, _ in cases
        ),
        encoding="utf-8",
    )
    corpus_sha256 = "sha256:" + hashlib.sha256(corpus.read_bytes()).hexdigest()
    hazkey_records = [
        make_v5_result(
            case_id,
            reading,
            hazkey,
            "hazkey",
            corpus_sha256=corpus_sha256,
            corpus_cases=len(cases),
            composition_count=composition_count,
            category=category,
        )
        for case_id, reading, _, category, composition_count, hazkey, _ in cases
    ]
    mozc_records = [
        make_v5_result(
            case_id,
            reading,
            mozc,
            "mozc",
            corpus_sha256=corpus_sha256,
            corpus_cases=len(cases),
            composition_count=composition_count,
            category=category,
        )
        for case_id, reading, _, category, composition_count, _, mozc in reversed(cases)
    ]
    hazkey_path = directory / "hazkey-v5.jsonl"
    mozc_path = directory / "mozc-v5.jsonl"
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
            report["schema"], "hazkey.mozc-hybrid-spike-evaluation.v3"
        )
        self.assertTrue(report["diagnostic_only"])
        self.assertFalse(report["formal_authorized"])
        self.assertTrue(report["new_holdout_required"])
        self.assertFalse(report["policy"]["uses_expected_labels"])
        self.assertFalse(report["policy"]["runtime_apply_eligible"])
        self.assertEqual(report["policy"]["evaluation_scope"], "surface_only")
        self.assertFalse(
            report["candidate_evidence"]["consuming_count_available"]
        )
        self.assertFalse(
            report["candidate_evidence"]["boundary_evidence_available"]
        )
        self.assertIsNone(report["candidate_evidence"]["conversion_path"])
        self.assertFalse(
            report["candidate_evidence"][
                "runtime_boundary_parity_established"
            ]
        )
        self.assertEqual(
            report["runtime_policy"]["id"],
            "mozc-first-preserve-top1-h0",
        )
        self.assertEqual(
            report["policy"]["id"], "mozc-first-one-sided-consensus-v1"
        )
        self.assertEqual(
            report["promotion_opportunities"],
            {
                "decision": "promote_hazkey_one_sided_consensus",
                "scope": "surface_only",
                "count": 2,
                "rate": 0.25,
                "boundary_eligible_count": None,
                "outcomes": {
                    "rescued": 1,
                    "regressed": 1,
                    "unchanged_correct": 0,
                    "unchanged_incorrect": 0,
                },
                "all_policy_decisions": {
                    "hazkey_fallback_mozc_empty": 1,
                    "keep_mozc_top1": 5,
                    "promote_hazkey_one_sided_consensus": 2,
                },
            },
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

    def test_v4_filters_by_mozc_top1_boundary_and_reports_opportunities(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            corpus, hazkey, mozc, _, _ = make_v4_inputs(directory)
            report = evaluate_mozc_hybrid_spike.evaluate_paths(
                corpus, hazkey, mozc
            )

        self.assertEqual(
            report["schema"], "hazkey.mozc-hybrid-spike-evaluation.v3"
        )
        self.assertFalse(report["formal_authorized"])
        self.assertTrue(report["new_holdout_required"])
        self.assertEqual(
            report["candidate_evidence"],
            {
                "input_schema": "hazkey.ab-probe-result.v4",
                "observed_fields": ["text", "rank", "consuming_count"],
                "conversion_path": "segment_candidates",
                "consuming_count_available": True,
                "boundary_evidence_available": True,
                "runtime_boundary_parity_established": True,
                "whole_target_quality_comparable": False,
                "limitation": (
                    "segment_candidates observes first-clause surfaces, while "
                    "the corpus expected values are whole-composition targets. "
                    "Boundary evidence is valid, but whole-target quality is "
                    "not comparable."
                ),
            },
        )
        self.assertEqual(
            report["target_comparability"],
            {
                "quality_target": "whole_composition",
                "observed_candidate_scope": "first_clause",
                "established": False,
                "comparable_count": 0,
                "incomparable_count": 3,
                "required_evidence": (
                    "a segment-labeled holdout, or an explicit composition-span "
                    "field with a reviewed target-parity inference"
                ),
            },
        )
        self.assertFalse(report["policy"]["runtime_apply_eligible"])
        self.assertEqual(report["policy"]["evaluation_scope"], "boundary_aware")
        self.assertEqual(
            report["policy"]["quality_evaluation_scope"],
            "not_comparable_without_segment_target_parity",
        )
        self.assertEqual(
            report["promotion_opportunities"],
            {
                "decision": "promote_hazkey_one_sided_consensus",
                "scope": "boundary_aware",
                "count": 1,
                "rate": 1 / 3,
                "surface_opportunity_count": 2,
                "surface_opportunity_rate": 2 / 3,
                "boundary_eligible_count": 1,
                "boundary_rejected_count": 1,
                "boundary_only_opportunity_count": 0,
                "outcomes": None,
                "outcome_comparable_count": 0,
                "outcome_incomparable_count": 1,
                "surface_outcomes": None,
                "surface_outcome_comparable_count": 0,
                "surface_outcome_incomparable_count": 2,
                "all_policy_decisions": {
                    "keep_mozc_hazkey_top1_boundary_mismatch": 2,
                    "promote_hazkey_one_sided_consensus": 1,
                },
                "all_surface_policy_decisions": {
                    "keep_mozc_top1": 1,
                    "promote_hazkey_one_sided_consensus": 2,
                },
            },
        )
        self.assertEqual(
            report["top1"],
            {
                "quality_comparable": False,
                "scope": "whole_target_comparable_only",
                "cases": 0,
                "excluded_incomparable_cases": 3,
                "hazkey": {"hits": None, "rate": None},
                "mozc": {"hits": None, "rate": None},
                "hybrid": {"hits": None, "rate": None},
                "rescued": None,
                "regressed": None,
                "net_hits": None,
                "net_rate": None,
            },
        )
        self.assertIsNone(
            report["oracle_ceiling"]["candidate_union"]["hits"]
        )
        self.assertIsNone(report["mozc_top1_miss_classification"]["total"])
        self.assertEqual(
            report["boundary_evidence"],
            {
                "conversion_path": "segment_candidates",
                "actual_hazkey_top1": {
                    "compared_count": 3,
                    "matching_count": 1,
                    "mismatch_count": 2,
                    "mismatch_rate": 2 / 3,
                },
            },
        )

        cases = {case["id"]: case for case in report["cases"]}
        eligible = cases["boundary-eligible"]
        self.assertFalse(eligible["target_comparable"])
        self.assertIsNone(eligible["top1_outcome"])
        self.assertEqual(
            eligible["expected_rank"],
            {
                "hazkey": None,
                "mozc": None,
                "hybrid": None,
                "runtime_h0": None,
                "width_guarded_hybrid": None,
            },
        )
        self.assertEqual(
            eligible["candidates"]["hybrid"],
            ["H0", "M0", "同", "同", "HX"],
        )
        self.assertEqual(
            eligible["boundary_evidence"]["eligible_hazkey_candidates"],
            ["H0", "HX"],
        )
        rejected = cases["boundary-rejected"]
        self.assertEqual(
            rejected["policy_decision"],
            "keep_mozc_hazkey_top1_boundary_mismatch",
        )
        self.assertEqual(rejected["candidates"]["hybrid"], ["M1", "H1"])
        self.assertEqual(
            rejected["boundary_evidence"]["eligible_hazkey_candidates"], []
        )
        mismatch_only = cases["top1-mismatch-only"]
        self.assertEqual(
            mismatch_only["boundary_evidence"]["eligible_hazkey_candidates"],
            ["eligible"],
        )
        self.assertEqual(
            mismatch_only["candidates"]["runtime_h0"], ["M2", "eligible"]
        )

    def test_v5_scores_only_explicit_whole_span_and_guards_width_promotions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            corpus, hazkey, mozc, _, _ = make_v5_inputs(directory)
            report = evaluate_mozc_hybrid_spike.evaluate_paths(
                corpus, hazkey, mozc
            )

        self.assertEqual(
            report["candidate_evidence"]["input_schema"],
            "hazkey.ab-probe-result.v5",
        )
        self.assertEqual(
            report["candidate_evidence"]["observed_fields"],
            ["text", "rank", "consuming_count"],
        )
        self.assertEqual(
            report["candidate_evidence"]["case_observed_fields"],
            ["composition_span"],
        )
        self.assertFalse(
            report["candidate_evidence"]["whole_target_quality_comparable"]
        )
        self.assertEqual(
            report["candidate_evidence"][
                "whole_target_quality_comparable_count"
            ],
            5,
        )
        self.assertEqual(
            report["target_comparability"],
            {
                "quality_target": "whole_composition",
                "observed_candidate_scope": "first_clause",
                "established": False,
                "comparable_count": 5,
                "incomparable_count": 1,
                "comparison_basis": "explicit_whole_composition_span",
                "partial_parity_established": True,
                "selection_basis": (
                    "mozc_top1_consumes_explicit_whole_span"
                ),
                "selection_biased": True,
                "absolute_backend_accuracy_generalizable": False,
                "required_evidence": (
                    "For the remaining incomparable rows, acquire "
                    "segment-labeled targets or reviewed evidence that the "
                    "observed candidate span covers the whole target."
                ),
            },
        )
        self.assertEqual(
            report["boundary_evidence"]["explicit_composition_span"],
            {
                "unit": "composition_element",
                "whole_span_comparable_count": 5,
                "whole_span_comparable_rate": 5 / 6,
            },
        )
        self.assertNotIn("top1", report)
        diagnostic = report["diagnostic_target_comparable"]
        self.assertNotIn("quality_decomposition", diagnostic)
        self.assertEqual(
            diagnostic["category_scope"],
            {
                "policy": "all_categories",
                "cases": 5,
                "by_category": {"proper-noun": 4, "protected": 1},
                "includes_formal_non_quality_categories": True,
            },
        )
        self.assertEqual(diagnostic["top1"]["cases"], 5)
        self.assertEqual(
            diagnostic["top1"]["mozc"], {"hits": 2, "rate": 0.4}
        )
        self.assertFalse(
            diagnostic["top1"]["formal_quality_categories_only"]
        )

        formal = report["formal_quality"]
        self.assertNotIn("quality_decomposition", formal)
        self.assertFalse(formal["formal_authorized"])
        self.assertEqual(
            formal["case_scope"],
            {
                "corpus_cases": 6,
                "eligible_category_cases": 5,
                "comparable_cases": 4,
                "incomparable_cases": 1,
                "excluded_non_quality_cases": 1,
                "excluded_non_quality_comparable_cases": 1,
            },
        )
        self.assertEqual(
            formal["category_policy"]["excluded_categories_observed"],
            ["protected"],
        )
        self.assertEqual(
            formal["top1"]["mozc"], {"hits": 2, "rate": 0.5}
        )
        self.assertEqual(formal["top1"]["cases"], 4)
        self.assertTrue(formal["top1"]["formal_quality_categories_only"])
        self.assertEqual(formal["width_guarded_top1"]["regressed"], 1)
        self.assertEqual(
            report["promotion_opportunities"]["outcomes"],
            {
                "rescued": 1,
                "regressed": 2,
                "unchanged_correct": 0,
                "unchanged_incorrect": 0,
            },
        )
        self.assertEqual(
            report["promotion_opportunities"]["outcome_comparable_count"],
            3,
        )
        self.assertEqual(
            report["width_guarded_promotion_opportunities"],
            {
                "policy_id": (
                    "mozc-first-one-sided-consensus-width-guard-v1"
                ),
                "decision": "promote_hazkey_one_sided_consensus",
                "scope": "boundary_aware",
                "count": 2,
                "rate": 1 / 3,
                "suppressed_width_equivalent_count": 1,
                "suppressed_width_equivalent": {
                    "count": 1,
                    "counterfactual_h1_outcomes": {
                        "rescued": 0,
                        "regressed": 1,
                        "unchanged_correct": 0,
                        "unchanged_incorrect": 0,
                    },
                    "h2_outcomes": {
                        "rescued": 0,
                        "regressed": 0,
                        "unchanged_correct": 1,
                        "unchanged_incorrect": 0,
                    },
                    "outcome_comparable_count": 1,
                    "outcome_incomparable_count": 0,
                },
                "outcomes": {
                    "rescued": 1,
                    "regressed": 1,
                    "unchanged_correct": 0,
                    "unchanged_incorrect": 0,
                },
                "outcome_comparable_count": 2,
                "outcome_incomparable_count": 0,
                "all_policy_decisions": {
                    "keep_mozc_top1": 3,
                    "keep_mozc_width_equivalent_top1": 1,
                    "promote_hazkey_one_sided_consensus": 2,
                },
            },
        )

        classification = formal["mozc_top1_miss_classification"]
        self.assertEqual(classification["total"], 2)
        self.assertEqual(classification["hazkey_top1_rescue"]["count"], 1)
        self.assertEqual(classification["both_absent"]["count"], 1)
        self.assertEqual(classification["excluded_target_incomparable_cases"], 1)

        cases = {case["id"]: case for case in report["cases"]}
        rescue = cases["whole-span-rescue"]
        self.assertTrue(rescue["target_comparable"])
        self.assertEqual(rescue["top1_outcome"], "rescued")
        self.assertEqual(rescue["expected_rank"]["hybrid"], 1)
        self.assertTrue(
            rescue["boundary_evidence"]["whole_span_target_parity"]
        )

        regression = cases["whole-span-regression"]
        self.assertEqual(regression["top1_outcome"], "regressed")
        self.assertEqual(regression["expected_rank"]["mozc"], 1)
        self.assertEqual(regression["expected_rank"]["hybrid"], 2)

        wrong_count = cases["same-surface-wrong-count"]
        self.assertTrue(wrong_count["target_comparable"])
        self.assertIn("正解C", wrong_count["candidates"]["hybrid"])
        self.assertEqual(
            wrong_count["expected_rank"],
            {
                "hazkey": None,
                "mozc": None,
                "hybrid": None,
                "runtime_h0": None,
                "width_guarded_hybrid": None,
            },
        )
        self.assertEqual(
            wrong_count["mozc_top1_miss_classification"], "both_absent"
        )

        partial = cases["partial-span-excluded"]
        self.assertFalse(partial["target_comparable"])
        self.assertIsNone(partial["top1_outcome"])
        self.assertFalse(
            partial["boundary_evidence"]["whole_span_target_parity"]
        )
        self.assertEqual(
            partial["quality_limitation"],
            "the observed first clause does not explicitly span the "
            "whole-composition target",
        )

        width_guarded = cases["width-guarded-regression"]
        self.assertEqual(width_guarded["top1_outcome"], "regressed")
        self.assertEqual(
            width_guarded["width_guarded_policy_decision"],
            "keep_mozc_width_equivalent_top1",
        )
        self.assertEqual(
            width_guarded["width_guarded_top1_outcome"],
            "unchanged_correct",
        )
        self.assertEqual(
            width_guarded["candidates"]["width_guarded_hybrid"],
            ["４月", "4月"],
        )

    def test_v5_requires_matching_composition_spans_between_backends(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            corpus, hazkey, mozc, _, mozc_records = make_v5_inputs(directory)
            rescue = next(
                record
                for record in mozc_records
                if record["id"] == "whole-span-rescue"
            )
            rescue["composition_span"]["count"] = 3
            write_jsonl(mozc, mozc_records)

            with self.assertRaisesRegex(
                ValueError,
                "composition_span for 'whole-span-rescue' differs between "
                "probe runs",
            ):
                evaluate_mozc_hybrid_spike.evaluate_paths(corpus, hazkey, mozc)

    def test_v5_reviewed_first_segment_targets_score_prefixes_without_output_selection(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            corpus_path, hazkey_path, mozc_path, _, _ = make_v5_inputs(directory)
            corpus_bytes = corpus_path.read_bytes()
            hazkey_bytes = hazkey_path.read_bytes()
            mozc_bytes = mozc_path.read_bytes()
            corpus = evaluate_mozc_hybrid_spike.load_corpus_bytes(
                corpus_bytes, str(corpus_path)
            )
            hazkey_run = evaluate_mozc_hybrid_spike.load_run_bytes(
                hazkey_bytes, hazkey_path
            )
            mozc_run = evaluate_mozc_hybrid_spike.load_run_bytes(
                mozc_bytes, mozc_path
            )
            targets = {
                row["id"]: {
                    "span": {
                        "start": 0,
                        "count": mozc_run["cases"][row["id"]][
                            "composition_span"
                        ]["count"],
                        "unit": "composition_element",
                    },
                    "surfaces": row["expected"].split("|"),
                }
                for row in corpus
            }
            report = evaluate_mozc_hybrid_spike.evaluate_runs(
                corpus,
                hazkey_run,
                mozc_run,
                corpus_sha256=(
                    "sha256:" + hashlib.sha256(corpus_bytes).hexdigest()
                ),
                corpus_bytes=corpus_bytes,
                hazkey_bytes=hazkey_bytes,
                mozc_bytes=mozc_bytes,
                reviewed_first_segment_targets=targets,
                formal_quality_categories=["proper-noun"],
                formal_quality_category_policy_id="reviewed-test-categories-v1",
            )
            directional_targets = deepcopy(targets)
            directional_targets["whole-span-regression"]["span"]["count"] = 1
            directional_report = evaluate_mozc_hybrid_spike.evaluate_runs(
                corpus,
                hazkey_run,
                mozc_run,
                corpus_sha256=(
                    "sha256:" + hashlib.sha256(corpus_bytes).hexdigest()
                ),
                corpus_bytes=corpus_bytes,
                hazkey_bytes=hazkey_bytes,
                mozc_bytes=mozc_bytes,
                reviewed_first_segment_targets=directional_targets,
                formal_quality_categories=["proper-noun"],
            )
            first_id = corpus[0]["id"]
            for invalid_surface, message in (
                ("e\u0301", "NFC-normalized"),
                ("bad\ufeffsurface", "control characters"),
            ):
                with self.subTest(invalid_surface=invalid_surface):
                    invalid_targets = deepcopy(targets)
                    invalid_targets[first_id]["surfaces"] = [invalid_surface]
                    with self.assertRaisesRegex(ValueError, message):
                        evaluate_mozc_hybrid_spike.evaluate_runs(
                            corpus,
                            hazkey_run,
                            mozc_run,
                            corpus_sha256=(
                                "sha256:"
                                + hashlib.sha256(corpus_bytes).hexdigest()
                            ),
                            corpus_bytes=corpus_bytes,
                            hazkey_bytes=hazkey_bytes,
                            mozc_bytes=mozc_bytes,
                            reviewed_first_segment_targets=invalid_targets,
                            formal_quality_categories=["proper-noun"],
                        )

        self.assertEqual(
            report["target_comparability"]["quality_target"],
            "first_reviewed_segment",
        )
        self.assertEqual(report["target_comparability"]["comparable_count"], 6)
        self.assertEqual(report["target_comparability"]["incomparable_count"], 0)
        self.assertEqual(
            report["target_comparability"]["selection_basis"],
            "corpus_label_not_backend_output",
        )
        self.assertFalse(report["target_comparability"]["selection_biased"])
        self.assertEqual(
            report["promotion_opportunities"]["outcome_incomparable_count"], 0
        )
        cases = {case["id"]: case for case in report["cases"]}
        partial = cases["partial-span-excluded"]
        self.assertTrue(partial["target_comparable"])
        self.assertFalse(
            partial["boundary_evidence"][
                "reviewed_target_boundary_matches_mozc"
            ]
        )
        self.assertEqual(
            partial["expected_rank"],
            {
                "hazkey": None,
                "mozc": None,
                "hybrid": None,
                "runtime_h0": None,
                "width_guarded_hybrid": None,
            },
        )
        self.assertEqual(report["formal_quality"]["top1"]["cases"], 5)
        self.assertIn(
            "quality_decomposition", report["diagnostic_target_comparable"]
        )
        self.assertIn("quality_decomposition", report["formal_quality"])
        directional_case = {
            case["id"]: case for case in directional_report["cases"]
        }["whole-span-regression"]
        self.assertEqual(
            directional_case["top1_quality"]["systems"]["mozc"]["top1"][
                "boundary"
            ]["classification"],
            "ends_after_reviewed_boundary",
        )
        under_segmentation = directional_report[
            "diagnostic_target_comparable"
        ]["quality_decomposition"]["systems"]["mozc"]["top1"]["boundary"][
            "ends_after_reviewed_boundary"
        ]
        self.assertEqual(under_segmentation["segmentation"], "under_segmentation")
        self.assertGreaterEqual(under_segmentation["count"], 1)
        self.assertGreater(under_segmentation["element_delta"]["absolute_sum"], 0)
        self.assertEqual(
            report["formal_quality"]["category_policy"]["included_categories"],
            ["proper-noun"],
        )

    def test_conditioned_surface_accuracy_is_undefined_without_boundary_hits(
        self,
    ) -> None:
        evidence = evaluate_mozc_hybrid_spike._structured_candidate_quality(
            [{"text": "wrong", "rank": 1, "consuming_count": 1}],
            ["expected"],
            2,
        )
        view = evaluate_mozc_hybrid_spike._quality_decomposition_system_view(
            [{"top1_quality": {"systems": {"mozc": evidence}}}],
            "mozc",
        )

        self.assertEqual(view["top1"]["boundary"]["hits"], 0)
        self.assertIsNone(
            view["top1"]["raw_exact_surface_given_boundary_correct"][
                "accuracy"
            ]
        )
        self.assertIsNone(
            view["top_k"]["raw_exact_surface_given_boundary_correct"][
                "accuracy"
            ]
        )

    def test_v4_requires_segment_candidates_conversion_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            corpus, hazkey, mozc, _, mozc_records = make_v4_inputs(directory)
            for record in mozc_records:
                record["conversion_path"] = "request_candidates"
            write_jsonl(mozc, mozc_records)

            with self.assertRaisesRegex(
                ValueError, "conversion_path must be 'segment_candidates'"
            ):
                evaluate_mozc_hybrid_spike.evaluate_paths(corpus, hazkey, mozc)

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

    def test_width_guard_folds_only_full_width_ascii_forms(self) -> None:
        self.assertEqual(
            evaluate_mozc_hybrid_spike.width_folded_surface("Ａ１！　"),
            "A1! ",
        )
        self.assertEqual(
            evaluate_mozc_hybrid_spike.width_folded_surface("①㍑"),
            "①㍑",
        )

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
