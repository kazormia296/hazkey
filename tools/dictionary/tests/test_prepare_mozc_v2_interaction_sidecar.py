from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT))

from tools.dictionary import prepare_mozc_v2_interaction_sidecar as sidecar  # noqa: E402


SCRIPT = REPOSITORY_ROOT / "tools/dictionary/prepare_mozc_v2_interaction_sidecar.py"
CORPUS = (
    REPOSITORY_ROOT
    / "hazkey-server/Tests/grimodex-spike/Fixtures/mozc-adoption-v2"
    / sidecar.SEALED_GENERATION
    / sidecar.CORPUS_NAME
)


class MozcV2InteractionSidecarTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.report = sidecar.prepare_path(CORPUS)

    def test_current_fixture_is_fully_covered_but_not_authorized(self) -> None:
        report = self.report
        self.assertEqual(
            report["schema"], "hazkey.mozc-v2-interaction-sidecar-draft.v1"
        )
        self.assertEqual(report["status"], "not_ready")
        self.assertFalse(report["formal_authorized"])
        self.assertEqual(
            report["counts"],
            {
                "action_trace_review_required": 134,
                "ascii_cases": 565,
                "proposed_normal_input_context_candidates": 431,
            },
        )
        self.assertEqual(
            report["corpus"],
            {
                "cases": 1360,
                "input_name": "formal-corpus.tsv",
                "sha256": sidecar.SEALED_CORPUS_SHA256,
            },
        )
        self.assertEqual(report["generation"]["name"], sidecar.SEALED_GENERATION)
        self.assertEqual(
            report["generation"]["sha256"], sidecar.SEALED_GENERATION_SHA256
        )
        self.assertEqual(
            report["coverage"],
            {
                "complete": True,
                "duplicate_case_ids": [],
                "emitted_ascii_cases": 565,
                "missing_ascii_case_ids": [],
                "source_ascii_cases": 565,
                "source_total_cases": 1360,
                "unexpected_case_ids": [],
                "unique": True,
            },
        )
        ids = [case["case_id"] for case in report["cases"]]
        self.assertEqual(len(ids), 565)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(ids, sorted(ids))

    def test_all_protected_cases_have_pending_normal_input_context_candidates(self) -> None:
        protected = [
            case for case in self.report["cases"] if case["category"] == "protected"
        ]
        self.assertEqual(len(protected), 100)
        self.assertTrue(
            all(case["review_status"] == "pending_review" for case in protected)
        )
        self.assertTrue(
            all(
                case["proposed"]["scenario_kind"]
                == "normal_input_context_candidate"
                for case in protected
            )
        )

        first = next(case for case in protected if case["case_id"] == "v2-protected-0001")
        self.assertEqual(first["proposed"]["committed_left_context"], "RUST_LOG=debug")
        self.assertEqual(first["proposed"]["composition_reading"], "でくわしいろぐをだす")
        self.assertEqual(first["proposed"]["expected_target"], ["で詳しいログを出す"])
        self.assertEqual(
            first["proposed"]["action_trace"],
            [
                {
                    "action": "update_context",
                    "left_context": "RUST_LOG=debug",
                    "right_context": "",
                },
                {
                    "action": "conversion_boundary",
                    "composition_reading": "でくわしいろぐをだす",
                },
            ],
        )
        self.assertEqual(first["proposed"]["right_context"], "")
        self.assertEqual(
            first["proposed"]["input_style"], "unknown_pending_review"
        )
        self.assertIsNone(first["proposed"]["physical_key_trace"])
        self.assertFalse(first["proposed"]["formal_product_path_eligible"])
        self.assertIsNone(first["proposed"]["requested_transform"])
        self.assertIn(
            "does not reproduce the prefix's key-input",
            self.report["classification_contract"]["context_action_semantics"],
        )
        self.assertIn(
            "direct versus mapped input is not inferred",
            self.report["classification_contract"]["input_style_semantics"],
        )
        self.assertIn(
            "not eligible for a formal product-path runner",
            self.report["classification_contract"]["formal_runner_eligibility"],
        )

        mixed_context = next(
            case
            for case in protected
            if case["case_id"] == "v2-protected-0056"
        )
        self.assertEqual(
            mixed_context["proposed"]["action_trace"][0],
            {
                "action": "update_context",
                "left_context": "nullではなくfalse",
                "right_context": "",
            },
        )

    def test_review_cases_do_not_receive_a_scenario(self) -> None:
        review = [
            case
            for case in self.report["cases"]
            if case["review_status"] == "action_trace_review_required"
        ]
        self.assertEqual(len(review), 134)
        self.assertTrue(
            all(
                set(case)
                == {"case_id", "category", "expected", "reading", "review_status"}
                for case in review
            )
        )
        self.assertNotIn("proposed", review[0])

    def test_cli_output_is_canonical_and_deterministic(self) -> None:
        command = [sys.executable, str(SCRIPT), "--corpus", str(CORPUS)]
        outputs: list[bytes] = []
        for encoding in ("utf-8", "utf-16", "ascii"):
            environment = os.environ.copy()
            environment["PYTHONIOENCODING"] = encoding
            result = subprocess.run(
                command,
                check=True,
                capture_output=True,
                env=environment,
            )
            outputs.append(result.stdout)
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[0], outputs[2])
        parsed = json.loads(outputs[0].decode("utf-8"))
        self.assertEqual(outputs[0], sidecar.canonical_json_bytes(parsed))

    def test_tampered_sealed_corpus_is_rejected(self) -> None:
        tampered = CORPUS.read_bytes().replace(
            b"v2-protected-0001", b"v2-protected-X001", 1
        )
        self.assertEqual(len(tampered), CORPUS.stat().st_size)
        with tempfile.TemporaryDirectory() as temporary_directory:
            generation = Path(temporary_directory) / sidecar.SEALED_GENERATION
            generation.mkdir()
            corpus = generation / sidecar.CORPUS_NAME
            corpus.write_bytes(tampered)
            with self.assertRaisesRegex(ValueError, "sealed corpus sha256 mismatch"):
                sidecar.prepare_path(corpus)

    def test_expected_context_mismatch_is_rejected_for_normal_proposal(self) -> None:
        row = {
            "id": "case",
            "reading": "APIをよむ",
            "expected": "SDKを読む",
            "category": "technical",
        }
        with self.assertRaisesRegex(ValueError, "does not preserve committed context"):
            sidecar._normal_case(row)

    def test_fifo_swap_after_lstat_is_nonblocking_and_rejected(self) -> None:
        data = (
            "id\treading\texpected\tcategory\n"
            "case\tAPIをよむ\tAPIを読む\ttechnical\n"
        ).encode()
        with tempfile.TemporaryDirectory() as temporary_directory:
            generation = Path(temporary_directory) / sidecar.SEALED_GENERATION
            generation.mkdir()
            corpus = generation / sidecar.CORPUS_NAME
            backup = generation / "original.tsv"
            corpus.write_bytes(data)
            real_open = os.open
            opened_descriptors: list[int] = []

            def fifo_open(path: os.PathLike[str], flags: int) -> int:
                self.assertEqual(Path(path), corpus)
                self.assertTrue(flags & os.O_NONBLOCK)
                self.assertTrue(flags & os.O_NOFOLLOW)
                self.assertTrue(flags & os.O_CLOEXEC)
                os.replace(corpus, backup)
                os.mkfifo(corpus)
                fd = real_open(corpus, flags)
                opened_descriptors.append(fd)
                return fd

            try:
                with mock.patch.object(sidecar.os, "open", side_effect=fifo_open):
                    with self.assertRaisesRegex(ValueError, "regular file"):
                        sidecar._read_bound_input(corpus)
            finally:
                if corpus.exists():
                    corpus.unlink()
                if backup.exists():
                    os.replace(backup, corpus)

            self.assertEqual(len(opened_descriptors), 1)
            with self.assertRaises(OSError):
                os.fstat(opened_descriptors[0])


if __name__ == "__main__":
    unittest.main()
