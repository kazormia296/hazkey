from __future__ import annotations

import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT))

from tools.dictionary import evaluate_mozc_adoption_v2_gate as gate  # noqa: E402


POLICY_FIXTURE = (
    REPOSITORY_ROOT
    / "hazkey-server/Tests/grimodex-spike/Fixtures/mozc-adoption-v2/"
    "formal-gate-policy.json"
)
V1_POLICY_FIXTURE = (
    REPOSITORY_ROOT
    / "hazkey-server/Tests/grimodex-spike/Fixtures/mozc-adoption-v1/"
    "b0-policy.json"
)


def find_check(result: dict[str, object], check_id: str) -> dict[str, object]:
    checks = result["checks"]
    assert isinstance(checks, list)
    return next(item for item in checks if item["id"] == check_id)


class FormalV2GateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = gate.load_policy(POLICY_FIXTURE)

    def metrics(self, candidate_id: str = "B0") -> dict[str, object]:
        quality_categories = {
            category: {
                "cases": cases,
                "top1_hits": cases,
                "top10_hits": cases,
            }
            for category, cases in gate.ALL_CATEGORIES.items()
        }
        human_categories = {
            category: {"wins": 0, "losses": 0, "ties": cases, "both_bad": 0}
            for category, cases in gate.QUALITY_CATEGORIES.items()
        }
        return {
            "schema": gate.METRICS_SCHEMA,
            "candidate_id": candidate_id,
            "corpus": {
                "sha256": gate.CORPUS_SHA256,
                "total_cases": gate.TOTAL_CASES,
                "quality_cases": gate.QUALITY_CASES,
            },
            "quality": {
                "hazkey": {"categories": copy.deepcopy(quality_categories)},
                "candidate": {"categories": copy.deepcopy(quality_categories)},
            },
            "human": {"by_category": human_categories},
            "warm_latency_p95_ms": {"hazkey": "1", "candidate": "0.5"},
            "total_pss_kib": {"hazkey": 100, "candidate": 125},
            "stability": {check_id: True for check_id in gate.REQUIRED_STABILITY_IDS},
        }

    def set_hits(
        self,
        metrics: dict[str, object],
        backend: str,
        category: str,
        *,
        top1: int | None = None,
        top10: int | None = None,
    ) -> None:
        quality = metrics["quality"]
        assert isinstance(quality, dict)
        item = quality[backend]["categories"][category]
        if top1 is not None:
            item["top1_hits"] = top1
        if top10 is not None:
            item["top10_hits"] = top10

    def evaluate(self, metrics: dict[str, object]) -> dict[str, object]:
        return gate.evaluate_metrics(self.policy, metrics)

    def test_checked_in_policy_and_bound_files_are_exact(self) -> None:
        self.assertEqual(self.policy.policy_id, gate.POLICY_ID)
        self.assertEqual(self.policy.corpus_sha256, gate.CORPUS_SHA256)
        self.assertEqual(
            self.policy.candidate_resource_fingerprints,
            gate.RUNTIME_RESOURCE_FINGERPRINTS,
        )
        self.assertEqual(self.policy.formal_evidence_status, "not_ready")
        self.assertIs(self.policy.formal_adoption_allowed, False)
        self.assertEqual(
            self.policy.hazkey_dictionary_fingerprint,
            gate.HAZKEY_DICTIONARY_FINGERPRINT,
        )
        self.assertEqual(
            self.policy.trusted_b0_acquisition_schema,
            gate.TRUSTED_B0_ACQUISITION_SCHEMA,
        )
        self.assertEqual(
            self.policy.trusted_b0_python_source_sha256,
            gate.TRUSTED_B0_PYTHON_SOURCE_SHA256,
        )
        self.assertEqual(
            self.policy.b1_mandatory_objective_check_ids,
            gate.B1_MANDATORY_OBJECTIVE_CHECK_IDS,
        )
        self.assertEqual(self.policy.b1_raw_run_ids, ("H0", "B0"))
        self.assertEqual(
            self.policy.trusted_b0_raw_run_sha256,
            gate.TRUSTED_B0_RAW_RUN_SHA256,
        )

    def test_human_minus_thirty_seven_passes_minus_thirty_eight_fails(self) -> None:
        passing = self.metrics()
        passing["human"]["by_category"]["technical-mixed"] = {  # type: ignore[index]
            "wins": 0,
            "losses": 37,
            "ties": 203,
            "both_bad": 0,
        }
        failing = self.metrics()
        failing["human"]["by_category"]["technical-mixed"] = {  # type: ignore[index]
            "wins": 0,
            "losses": 38,
            "ties": 202,
            "both_bad": 0,
        }
        self.assertTrue(find_check(self.evaluate(passing), "human-net-preference")["passed"])
        self.assertFalse(find_check(self.evaluate(failing), "human-net-preference")["passed"])

    def test_top1_minus_one_hundred_passes_minus_one_hundred_one_fails(self) -> None:
        passing = self.metrics()
        self.set_hits(passing, "candidate", "technical-mixed", top1=140)
        failing = self.metrics()
        self.set_hits(failing, "candidate", "technical-mixed", top1=139)
        self.assertTrue(find_check(self.evaluate(passing), "top1-delta")["passed"])
        self.assertFalse(find_check(self.evaluate(failing), "top1-delta")["passed"])

    def test_top10_minus_one_fifty_one_passes_minus_one_fifty_two_fails(self) -> None:
        passing = self.metrics()
        self.set_hits(passing, "candidate", "technical-mixed", top1=89, top10=89)
        failing = self.metrics()
        self.set_hits(failing, "candidate", "technical-mixed", top1=88, top10=88)
        self.assertTrue(find_check(self.evaluate(passing), "top10-delta")["passed"])
        self.assertFalse(find_check(self.evaluate(failing), "top10-delta")["passed"])

    def test_each_category_uses_its_exact_integer_boundary(self) -> None:
        for category, cases in gate.QUALITY_CATEGORIES.items():
            with self.subTest(category=category):
                allowed_loss = -gate.CATEGORY_MINIMUM_DELTA_HITS[category]
                passing = self.metrics()
                self.set_hits(
                    passing,
                    "candidate",
                    category,
                    top1=cases - allowed_loss,
                )
                failing = self.metrics()
                self.set_hits(
                    failing,
                    "candidate",
                    category,
                    top1=cases - allowed_loss - 1,
                )
                check_id = f"category-top1-delta:{category}"
                self.assertTrue(find_check(self.evaluate(passing), check_id)["passed"])
                self.assertFalse(find_check(self.evaluate(failing), check_id)["passed"])

    def test_protected_one_hundred_passes_ninety_nine_fails(self) -> None:
        passing = self.metrics()
        failing = self.metrics()
        self.set_hits(failing, "candidate", gate.PROTECTED_CATEGORY, top1=99)
        self.assertTrue(find_check(self.evaluate(passing), "protected-cases")["passed"])
        self.assertFalse(find_check(self.evaluate(failing), "protected-cases")["passed"])

    def test_protected_is_excluded_from_overall_quality_delta(self) -> None:
        baseline = self.evaluate(self.metrics())
        changed = self.metrics()
        self.set_hits(changed, "candidate", gate.PROTECTED_CATEGORY, top1=0, top10=0)
        result = self.evaluate(changed)
        for check_id in ("top1-delta", "top10-delta"):
            self.assertEqual(
                find_check(result, check_id)["actual"],
                find_check(baseline, check_id)["actual"],
            )
        self.assertFalse(find_check(result, "protected-cases")["passed"])

    def test_human_schema_structurally_excludes_protected(self) -> None:
        metrics = self.metrics()
        metrics["human"]["by_category"][gate.PROTECTED_CATEGORY] = {  # type: ignore[index]
            "wins": 0,
            "losses": 0,
            "ties": 100,
            "both_bad": 0,
        }
        with self.assertRaisesRegex(ValueError, "only quality categories"):
            self.evaluate(metrics)

    def test_both_bad_fifty_nine_passes_sixty_fails(self) -> None:
        passing = self.metrics()
        passing["human"]["by_category"]["technical-mixed"] = {  # type: ignore[index]
            "wins": 0,
            "losses": 0,
            "ties": 181,
            "both_bad": 59,
        }
        failing = self.metrics()
        failing["human"]["by_category"]["technical-mixed"] = {  # type: ignore[index]
            "wins": 0,
            "losses": 0,
            "ties": 180,
            "both_bad": 60,
        }
        self.assertTrue(find_check(self.evaluate(passing), "both-bad")["passed"])
        self.assertFalse(find_check(self.evaluate(failing), "both-bad")["passed"])

    def test_latency_exact_half_passes_any_decimal_over_fails(self) -> None:
        passing = self.metrics()
        self.assertTrue(find_check(self.evaluate(passing), "warm-latency-p95-ratio")["passed"])
        for candidate in (
            "0.5000000000000000000000000001",
            "0.5" + ("0" * 100) + "1",
        ):
            failing = self.metrics()
            failing["warm_latency_p95_ms"] = {
                "hazkey": "1",
                "candidate": candidate,
            }
            with self.subTest(candidate=candidate):
                self.assertFalse(
                    find_check(
                        self.evaluate(failing), "warm-latency-p95-ratio"
                    )["passed"]
                )

    def test_invalid_latency_values_fail_closed(self) -> None:
        for value in (
            "0",
            "-1",
            "NaN",
            "Infinity",
            "not-a-number",
            "1e1000000",
            " 1",
        ):
            metrics = self.metrics()
            metrics["warm_latency_p95_ms"] = {"hazkey": "1", "candidate": value}
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "finite positive"):
                self.evaluate(metrics)

    def test_pss_one_twenty_five_passes_one_twenty_six_fails(self) -> None:
        passing = self.metrics()
        failing = self.metrics()
        failing["total_pss_kib"] = {"hazkey": 100, "candidate": 126}
        self.assertTrue(find_check(self.evaluate(passing), "total-pss-ratio")["passed"])
        self.assertFalse(find_check(self.evaluate(failing), "total-pss-ratio")["passed"])

    def test_invalid_pss_values_fail_closed(self) -> None:
        for value in (0, -1, True):
            metrics = self.metrics()
            metrics["total_pss_kib"] = {"hazkey": 100, "candidate": value}
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "positive|integer"):
                self.evaluate(metrics)

    def test_stability_requires_the_exact_boolean_id_set(self) -> None:
        for value in (
            {check_id: True for check_id in gate.REQUIRED_STABILITY_IDS[:-1]},
            {**{check_id: True for check_id in gate.REQUIRED_STABILITY_IDS}, "unknown": True},
        ):
            metrics = self.metrics()
            metrics["stability"] = value
            with self.assertRaisesRegex(ValueError, "IDs do not exactly match"):
                self.evaluate(metrics)
        metrics = self.metrics()
        metrics["stability"][gate.REQUIRED_STABILITY_IDS[0]] = "pass"  # type: ignore[index]
        with self.assertRaisesRegex(ValueError, "must be a boolean"):
            self.evaluate(metrics)
        metrics = self.metrics()
        metrics["stability"][gate.REQUIRED_STABILITY_IDS[0]] = False  # type: ignore[index]
        self.assertFalse(
            find_check(
                self.evaluate(metrics),
                f"stability:{gate.REQUIRED_STABILITY_IDS[0]}",
            )["passed"]
        )

    def test_formal_result_is_three_state_and_never_authorizes_adoption(self) -> None:
        passed = self.evaluate(self.metrics())
        self.assertEqual(passed["gate_result"], "formal_pass")
        self.assertIs(passed["formal_adoption_allowed"], False)
        self.assertEqual(passed["formal_evidence_status"], "not_ready")

        incomplete_metrics = self.metrics()
        incomplete_metrics["human"] = None
        incomplete_metrics["warm_latency_p95_ms"] = None
        incomplete_metrics["total_pss_kib"] = None
        incomplete_metrics["stability"] = None
        incomplete = self.evaluate(incomplete_metrics)
        self.assertEqual(incomplete["gate_result"], "inconclusive")

        failed_metrics = copy.deepcopy(incomplete_metrics)
        self.set_hits(failed_metrics, "candidate", gate.PROTECTED_CATEGORY, top1=99)
        failed = self.evaluate(failed_metrics)
        self.assertEqual(failed["gate_result"], "formal_fail")
        self.assertTrue(any(item["passed"] is None for item in failed["checks"]))

    def test_metrics_category_shape_and_counts_fail_closed(self) -> None:
        mutations = []
        missing = self.metrics()
        del missing["quality"]["candidate"]["categories"]["proper-noun"]  # type: ignore[index]
        mutations.append(missing)
        unknown = self.metrics()
        unknown["quality"]["candidate"]["categories"]["unknown"] = {  # type: ignore[index]
            "cases": 1,
            "top1_hits": 1,
            "top10_hits": 1,
        }
        mutations.append(unknown)
        wrong_count = self.metrics()
        wrong_count["quality"]["candidate"]["categories"]["proper-noun"]["cases"] = 199  # type: ignore[index]
        mutations.append(wrong_count)
        inverted = self.metrics()
        self.set_hits(inverted, "candidate", "proper-noun", top1=200, top10=199)
        mutations.append(inverted)
        for metrics in mutations:
            with self.subTest(), self.assertRaises(ValueError):
                self.evaluate(metrics)

    def test_unknown_missing_and_v1_policy_fields_are_rejected(self) -> None:
        raw = json.loads(POLICY_FIXTURE.read_text(encoding="utf-8"))
        unknown = copy.deepcopy(raw)
        unknown["unexpected"] = True
        with self.assertRaisesRegex(ValueError, "fields do not match schema"):
            gate.parse_policy(json.dumps(unknown).encode())
        missing = copy.deepcopy(raw)
        del missing["api_contract"]
        with self.assertRaisesRegex(ValueError, "fields do not match schema"):
            gate.parse_policy(json.dumps(missing).encode())
        with self.assertRaises(ValueError):
            gate.parse_policy(V1_POLICY_FIXTURE.read_bytes())

    def test_policy_rejects_pilot_counting_and_integer_boundary_drift(self) -> None:
        raw = json.loads(POLICY_FIXTURE.read_text(encoding="utf-8"))
        counted = copy.deepcopy(raw)
        counted["corpus_binding"]["pilot_v1_counted"] = True
        with self.assertRaisesRegex(ValueError, "pilot_v1_counted"):
            gate.parse_policy(json.dumps(counted).encode())
        drift = copy.deepcopy(raw)
        drift["gates"]["top1"]["minimum_delta_hits"] = -101
        with self.assertRaisesRegex(ValueError, "minimum_delta_hits"):
            gate.parse_policy(json.dumps(drift).encode())
        false_as_integer = copy.deepcopy(raw)
        false_as_integer["corpus_binding"]["protected"]["included_in_quality"] = 0
        with self.assertRaisesRegex(ValueError, "must be a boolean"):
            gate.parse_policy(json.dumps(false_as_integer).encode())

        source_drift = copy.deepcopy(raw)
        source_drift["b0_early_rejection"]["trusted_acquisition"][
            "python_source_sha256"
        ]["producer"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(ValueError, "python_source_sha256"):
            gate.parse_policy(json.dumps(source_drift).encode())

        rule_drift = copy.deepcopy(raw)
        rule_drift["b0_early_rejection"]["authorization_rule"][
            "mandatory_check_ids"
        ].pop()
        with self.assertRaisesRegex(ValueError, "mandatory_check_ids"):
            gate.parse_policy(json.dumps(rule_drift).encode())

        evidence_drift = copy.deepcopy(raw)
        evidence_drift["b0_early_rejection"]["trusted_acquisition"][
            "accepted_evidence"
        ]["raw_run_sha256"]["B0"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(ValueError, "raw_run_sha256"):
            gate.parse_policy(json.dumps(evidence_drift).encode())

    def test_b1_remains_blocked_with_a_canonical_not_ready_b0_formal_fail(self) -> None:
        b0_metrics = self.metrics("B0")
        self.set_hits(b0_metrics, "candidate", gate.PROTECTED_CATEGORY, top1=99)
        b0_result = self.evaluate(b0_metrics)
        self.assertEqual(b0_result["gate_result"], "formal_fail")
        b0_bytes = gate.encode_result(b0_result)

        b1_metrics = self.metrics("B1")
        with self.assertRaisesRegex(ValueError, "raw-evidence wrapper"):
            gate.evaluate_metrics(
                self.policy,
                b1_metrics,
                prior_b0_result=b0_bytes,
            )

    def test_b1_rejects_every_prior_while_formal_evidence_is_not_ready(self) -> None:
        with self.assertRaisesRegex(ValueError, "raw-evidence wrapper"):
            self.evaluate(self.metrics("B1"))

        passing_bytes = gate.encode_result(self.evaluate(self.metrics("B0")))
        with self.assertRaisesRegex(ValueError, "raw-evidence wrapper"):
            gate.evaluate_metrics(
                self.policy,
                self.metrics("B1"),
                prior_b0_result=passing_bytes,
            )

        incomplete_metrics = self.metrics("B0")
        incomplete_metrics["human"] = None
        incomplete_metrics["warm_latency_p95_ms"] = None
        incomplete_metrics["total_pss_kib"] = None
        incomplete_metrics["stability"] = None
        incomplete_bytes = gate.encode_result(self.evaluate(incomplete_metrics))
        with self.assertRaisesRegex(ValueError, "raw-evidence wrapper"):
            gate.evaluate_metrics(
                self.policy,
                self.metrics("B1"),
                prior_b0_result=incomplete_bytes,
            )

        failing = self.metrics("B0")
        self.set_hits(failing, "candidate", gate.PROTECTED_CATEGORY, top1=99)
        tampered = json.loads(gate.encode_result(self.evaluate(failing)))
        tampered["policy_sha256"] = "sha256:" + "0" * 64
        tampered_bytes = gate._canonical_json(tampered) + b"\n"
        with self.assertRaisesRegex(ValueError, "raw-evidence wrapper"):
            gate.evaluate_metrics(
                self.policy,
                self.metrics("B1"),
                prior_b0_result=tampered_bytes,
            )

        recomputed = json.loads(gate.encode_result(self.evaluate(failing)))
        recomputed["metrics"]["quality"]["candidate"]["categories"][
            gate.PROTECTED_CATEGORY
        ]["top1_hits"] = 100
        base = {key: value for key, value in recomputed.items() if key != "integrity"}
        recomputed["integrity"] = (
            "sha256:" + hashlib.sha256(gate._canonical_json(base)).hexdigest()
        )
        recomputed_bytes = gate._canonical_json(recomputed) + b"\n"
        with self.assertRaisesRegex(ValueError, "raw-evidence wrapper"):
            gate.evaluate_metrics(
                self.policy,
                self.metrics("B1"),
                prior_b0_result=recomputed_bytes,
            )

    def test_b0_rejects_prior_and_b2_is_ineligible(self) -> None:
        b0 = self.evaluate(self.metrics("B0"))
        with self.assertRaisesRegex(ValueError, "must not supply"):
            gate.evaluate_metrics(
                self.policy,
                self.metrics("B0"),
                prior_b0_result=gate.encode_result(b0),
            )
        with self.assertRaisesRegex(ValueError, "B0 or B1"):
            self.evaluate(self.metrics("B2"))

    def test_result_integrity_and_canonical_encoding_are_stable(self) -> None:
        result = self.evaluate(self.metrics())
        base = {key: value for key, value in result.items() if key != "integrity"}
        self.assertEqual(
            result["integrity"],
            "sha256:" + hashlib.sha256(gate._canonical_json(base)).hexdigest(),
        )
        self.assertEqual(gate.encode_result(result), gate.encode_result(result))

    def test_cli_is_explicitly_not_ready(self) -> None:
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            self.assertEqual(gate.main([]), 2)
        self.assertIn("not formal evidence", stderr.getvalue())


class FormalResultClassificationTests(unittest.TestCase):
    def test_result_derivation_is_fail_closed(self) -> None:
        self.assertEqual(gate.derive_formal_result([{"passed": True}]), "formal_pass")
        self.assertEqual(gate.derive_formal_result([{"passed": True}, {"passed": False}]), "formal_fail")
        self.assertEqual(gate.derive_formal_result([{"passed": True}, {"passed": None}]), "inconclusive")
        with self.assertRaisesRegex(ValueError, "at least one"):
            gate.derive_formal_result([])
        with self.assertRaisesRegex(ValueError, "boolean or null"):
            gate.derive_formal_result([{"passed": "pass"}])


if __name__ == "__main__":
    unittest.main()
