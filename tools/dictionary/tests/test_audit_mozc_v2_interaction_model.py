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

from tools.dictionary import audit_mozc_v2_interaction_model as audit  # noqa: E402


SCRIPT = REPOSITORY_ROOT / "tools/dictionary/audit_mozc_v2_interaction_model.py"
GENERATION = (
    REPOSITORY_ROOT
    / "hazkey-server/Tests/grimodex-spike/Fixtures/mozc-adoption-v2/"
    "sealed-v2-sha256-b4c1351b1b0ef7797349ebf26858db4d0dd69ce1c8bcbfaee88e0f0b644225ed"
)
CORPUS = GENERATION / "formal-corpus.tsv"


class MozcV2InteractionModelAuditTests(unittest.TestCase):
    def test_current_sealed_fixture_has_expected_interaction_gap(self) -> None:
        report = audit.audit_path(CORPUS)

        self.assertEqual(
            report["schema"], "hazkey.mozc-v2-interaction-model-audit.v1"
        )
        self.assertEqual(report["formal_evidence_status"], "not_ready")
        self.assertEqual(
            report["blocking_not_ready_reason"]["id"],
            "interaction-model-metadata-missing",
        )
        self.assertEqual(
            report["counts"],
            {
                "action_trace_review_required": 134,
                "ascii_containing": 565,
                "ascii_free": 795,
                "single_context_target_candidates": 431,
                "total": 1360,
            },
        )
        self.assertEqual(
            report["categories"],
            {
                "colloquial": {
                    "action_trace_review_required": 1,
                    "ascii_containing": 2,
                    "ascii_free": 198,
                    "cases": 200,
                    "single_context_target_candidates": 1,
                },
                "grimodex-regression": {
                    "action_trace_review_required": 103,
                    "ascii_containing": 220,
                    "ascii_free": 0,
                    "cases": 220,
                    "single_context_target_candidates": 117,
                },
                "homophone-context": {
                    "action_trace_review_required": 0,
                    "ascii_containing": 0,
                    "ascii_free": 200,
                    "cases": 200,
                    "single_context_target_candidates": 0,
                },
                "long-structural": {
                    "action_trace_review_required": 0,
                    "ascii_containing": 0,
                    "ascii_free": 200,
                    "cases": 200,
                    "single_context_target_candidates": 0,
                },
                "proper-noun": {
                    "action_trace_review_required": 4,
                    "ascii_containing": 4,
                    "ascii_free": 196,
                    "cases": 200,
                    "single_context_target_candidates": 0,
                },
                "protected": {
                    "action_trace_review_required": 0,
                    "ascii_containing": 100,
                    "ascii_free": 0,
                    "cases": 100,
                    "single_context_target_candidates": 100,
                },
                "technical-mixed": {
                    "action_trace_review_required": 26,
                    "ascii_containing": 239,
                    "ascii_free": 1,
                    "cases": 240,
                    "single_context_target_candidates": 213,
                },
            },
        )
        self.assertEqual(
            report["corpus"]["sha256"],
            "sha256:cdb2a017b4548f6f77ec3d466f84ec09268a74adb5e876e224e01069f128c8ae",
        )
        self.assertEqual(
            report["generation"]["sha256"],
            "sha256:b4c1351b1b0ef7797349ebf26858db4d0dd69ce1c8bcbfaee88e0f0b644225ed",
        )
        self.assertEqual(report["input"]["sha256"], report["corpus"]["sha256"])

        ids = report["case_ids"]
        self.assertEqual(len(ids["ascii_containing"]), 565)
        self.assertEqual(len(ids["ascii_free"]), 795)
        self.assertEqual(len(ids["single_context_target_candidates"]), 431)
        self.assertEqual(len(ids["action_trace_review_required"]), 134)
        self.assertEqual(
            set(ids["ascii_containing"]),
            set(ids["single_context_target_candidates"])
            | set(ids["action_trace_review_required"]),
        )
        self.assertFalse(
            set(ids["single_context_target_candidates"])
            & set(ids["action_trace_review_required"])
        )
        self.assertTrue(
            all(
                f"v2-protected-{index:04d}"
                in ids["single_context_target_candidates"]
                for index in range(1, 101)
            )
        )

    def test_classifier_derives_counts_without_fixture_constants(self) -> None:
        data = (
            "id\treading\texpected\tcategory\n"
            "simple\tAPIをよむ\tAPIを読む\ttechnical\n"
            "multi\tかなAPIをよむ\t仮名APIを読む\ttechnical\n"
            "terminal\tかな5\t仮名5\tproper\n"
            "plain\tかなをよむ\t仮名を読む\tplain\n"
        ).encode()
        report = audit.audit_bytes(
            data,
            generation_name="sealed-v2-sha256-" + "a" * 64,
        )

        self.assertEqual(
            report["counts"],
            {
                "action_trace_review_required": 2,
                "ascii_containing": 3,
                "ascii_free": 1,
                "single_context_target_candidates": 1,
                "total": 4,
            },
        )
        self.assertEqual(
            report["case_ids"]["single_context_target_candidates"], ["simple"]
        )
        self.assertEqual(
            report["case_ids"]["action_trace_review_required"],
            ["multi", "terminal"],
        )
        self.assertEqual(report["case_ids"]["ascii_free"], ["plain"])

    def test_cli_output_is_stable_and_does_not_modify_the_input(self) -> None:
        before = CORPUS.stat()
        command = [sys.executable, str(SCRIPT), "--corpus", str(CORPUS)]
        first = subprocess.run(command, check=True, capture_output=True, text=True)
        second = subprocess.run(command, check=True, capture_output=True, text=True)
        after = CORPUS.stat()

        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual(json.loads(first.stdout)["counts"]["ascii_containing"], 565)
        self.assertEqual(
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns),
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns),
        )

    def test_rejects_duplicate_ids(self) -> None:
        data = (
            "id\treading\texpected\tcategory\n"
            "same\tかな\t仮名\tone\n"
            "same\tよみ\t読み\ttwo\n"
        ).encode()
        with self.assertRaisesRegex(ValueError, "duplicate case id"):
            audit.audit_bytes(
                data,
                generation_name="sealed-v2-sha256-" + "b" * 64,
            )

    def test_rejects_leaf_swap_and_restore_between_lstat_and_open(self) -> None:
        original = (
            "id\treading\texpected\tcategory\n"
            "original\tAPIをよむ\tAPIを読む\ttechnical\n"
        ).encode()
        replacement = (
            "id\treading\texpected\tcategory\n"
            "replacement\tAPIをかく\tAPIを書く\ttechnical\n"
        ).encode()
        with tempfile.TemporaryDirectory() as temporary_directory:
            generation = (
                Path(temporary_directory) / ("sealed-v2-sha256-" + "c" * 64)
            )
            generation.mkdir()
            corpus = generation / "formal-corpus.tsv"
            replacement_path = generation / "replacement.tsv"
            backup_path = generation / "original-backup.tsv"
            corpus.write_bytes(original)
            replacement_path.write_bytes(replacement)

            real_open = os.open
            opened_descriptors: list[int] = []

            def swap_open(path: os.PathLike[str], flags: int) -> int:
                self.assertEqual(Path(path), corpus)
                os.replace(corpus, backup_path)
                os.replace(replacement_path, corpus)
                fd = real_open(corpus, flags)
                opened_descriptors.append(fd)
                os.replace(corpus, replacement_path)
                os.replace(backup_path, corpus)
                return fd

            with mock.patch.object(audit.os, "open", side_effect=swap_open):
                with self.assertRaisesRegex(
                    ValueError,
                    "pre-open path identity does not match opened descriptor",
                ):
                    audit.audit_path(corpus)

            self.assertEqual(corpus.read_bytes(), original)
            self.assertEqual(replacement_path.read_bytes(), replacement)
            self.assertEqual(len(opened_descriptors), 1)
            with self.assertRaises(OSError):
                os.fstat(opened_descriptors[0])

    def test_rejects_hardlinked_input(self) -> None:
        data = (
            "id\treading\texpected\tcategory\n"
            "case\tかな\t仮名\tplain\n"
        ).encode()
        with tempfile.TemporaryDirectory() as temporary_directory:
            generation = (
                Path(temporary_directory) / ("sealed-v2-sha256-" + "d" * 64)
            )
            generation.mkdir()
            corpus = generation / "formal-corpus.tsv"
            corpus.write_bytes(data)
            os.link(corpus, generation / "alias.tsv")

            with self.assertRaisesRegex(ValueError, "exactly one hard link"):
                audit.audit_path(corpus)


if __name__ == "__main__":
    unittest.main()
