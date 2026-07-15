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

from tools.dictionary import audit_mozc_v2_interaction_model as interaction_audit  # noqa: E402
from tools.dictionary import prepare_mozc_v2_interaction_sidecar as interaction_sidecar  # noqa: E402
from tools.dictionary import prepare_mozc_v2_normal_input_context_probe as probe  # noqa: E402


SCRIPT = (
    REPOSITORY_ROOT
    / "tools/dictionary/prepare_mozc_v2_normal_input_context_probe.py"
)
CORPUS = (
    REPOSITORY_ROOT
    / "hazkey-server/Tests/grimodex-spike/Fixtures/mozc-adoption-v2"
    / probe.SEALED_GENERATION
    / probe.CORPUS_NAME
)


class MozcV2NormalInputContextProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.corpus_data = CORPUS.read_bytes()
        cls.sidecar_report = interaction_sidecar.prepare_bytes(
            cls.corpus_data,
            generation_name=probe.SEALED_GENERATION,
        )
        cls.sidecar_data = interaction_sidecar.canonical_json_bytes(
            cls.sidecar_report
        )
        cls.report = probe.prepare_bytes(
            cls.corpus_data,
            cls.sidecar_data,
            generation_name=probe.SEALED_GENERATION,
        )
        cls.rows = interaction_audit._load_rows(cls.corpus_data, probe.CORPUS_NAME)
        cls.row_by_id = {row["id"]: row for row in cls.rows}

    def test_current_fixture_emits_exact_diagnostic_contract(self) -> None:
        report = self.report
        self.assertEqual(
            report["schema"],
            "hazkey.mozc-v2-normal-input-context-probe-draft.v1",
        )
        self.assertEqual(report["status"], "not_ready")
        self.assertFalse(report["formal_authorized"])
        self.assertEqual(
            report["not_ready_reason"],
            "interaction-review-and-product-path-runner-pending",
        )
        self.assertEqual(
            report["counts"],
            {
                "excluded_action_trace_review_required": 134,
                "normal_input_context_cases": 431,
                "sealed_corpus_cases": 1360,
                "source_sidecar_cases": 565,
            },
        )
        self.assertEqual(report["contract"]["evidence_use"], "diagnostic_only")
        self.assertFalse(report["contract"]["formal_product_path_eligible"])
        self.assertEqual(report["contract"]["runner"], "not_executed_by_preparer")

    def test_binds_exact_corpus_and_current_sidecar_bytes(self) -> None:
        self.assertEqual(
            self.report["inputs"],
            {
                "corpus": {
                    "generation": probe.SEALED_GENERATION,
                    "generation_sha256": probe.SEALED_GENERATION_SHA256,
                    "name": probe.CORPUS_NAME,
                    "sha256": probe.SEALED_CORPUS_SHA256,
                    "size_bytes": len(self.corpus_data),
                },
                "interaction_sidecar": {
                    "schema": interaction_sidecar.SCHEMA,
                    "sha256": probe.CURRENT_INTERACTION_SIDECAR_SHA256,
                    "size_bytes": len(self.sidecar_data),
                },
            },
        )
        self.assertEqual(
            probe._sha256(self.sidecar_data),
            probe.CURRENT_INTERACTION_SIDECAR_SHA256,
        )

    def test_all_431_cases_are_unique_sorted_and_preserve_source_ids(self) -> None:
        cases = self.report["cases"]
        self.assertEqual(len(cases), 431)
        ids = [case["case_id"] for case in cases]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(ids), len(set(ids)))
        for case in cases:
            self.assertEqual(
                case["source_ids"],
                {
                    "corpus_case_id": case["case_id"],
                    "interaction_sidecar_case_id": case["case_id"],
                },
            )
            self.assertEqual(
                case["category"], self.row_by_id[case["case_id"]]["category"]
            )

    def test_conversion_targets_exclude_ascii_prefix_and_preserve_alternatives(self) -> None:
        for case in self.report["cases"]:
            row = self.row_by_id[case["case_id"]]
            self.assertTrue(case["left_context"])
            self.assertLessEqual(ord(case["left_context"][-1]), 0x7F)
            self.assertTrue(case["conversion_target"])
            self.assertFalse(
                any(ord(character) <= 0x7F for character in case["conversion_target"])
            )
            self.assertEqual(
                case["left_context"] + case["conversion_target"],
                row["reading"],
            )
            expected_candidates = [
                alternative[len(case["left_context"]) :]
                for alternative in row["expected"].split("|")
            ]
            self.assertEqual(case["expected_candidates"], expected_candidates)
            self.assertEqual(case["right_context"], "")

    def test_representative_protected_case_has_committed_ascii_context(self) -> None:
        case = next(
            case
            for case in self.report["cases"]
            if case["case_id"] == "v2-protected-0001"
        )
        self.assertEqual(case["left_context"], "RUST_LOG=debug")
        self.assertEqual(case["conversion_target"], "でくわしいろぐをだす")
        self.assertEqual(case["expected_candidates"], ["で詳しいログを出す"])

    def test_tampered_corpus_sha_is_rejected(self) -> None:
        tampered = self.corpus_data.replace(
            b"v2-protected-0001", b"v2-protected-X001", 1
        )
        self.assertEqual(len(tampered), len(self.corpus_data))
        with self.assertRaisesRegex(ValueError, "sealed corpus sha256 mismatch"):
            probe.prepare_bytes(
                tampered,
                self.sidecar_data,
                generation_name=probe.SEALED_GENERATION,
            )

    def test_tampered_or_noncanonical_sidecar_sha_is_rejected(self) -> None:
        tampered = self.sidecar_data.replace(
            b'"formal_authorized":false',
            b'"formal_authorized":true ',
            1,
        )
        self.assertEqual(len(tampered), len(self.sidecar_data))
        with self.assertRaisesRegex(ValueError, "interaction sidecar sha256 mismatch"):
            probe.prepare_bytes(
                self.corpus_data,
                tampered,
                generation_name=probe.SEALED_GENERATION,
            )

    def test_wrong_generation_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "sealed generation mismatch"):
            probe.prepare_bytes(
                self.corpus_data,
                self.sidecar_data,
                generation_name="sealed-v2-sha256-" + "0" * 64,
            )

    def test_hardlinked_corpus_and_sidecar_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            generation = root / probe.SEALED_GENERATION
            generation.mkdir()
            corpus = generation / probe.CORPUS_NAME
            sidecar_path = root / "interaction-sidecar.json"
            corpus.write_bytes(self.corpus_data)
            sidecar_path.write_bytes(self.sidecar_data)

            corpus_alias = generation / "formal-corpus-alias.tsv"
            os.link(corpus, corpus_alias)
            with self.assertRaisesRegex(ValueError, "exactly one hard link"):
                probe.prepare_paths(corpus, sidecar_path)
            corpus_alias.unlink()

            sidecar_alias = root / "interaction-sidecar-alias.json"
            os.link(sidecar_path, sidecar_alias)
            with self.assertRaisesRegex(ValueError, "exactly one hard link"):
                probe.prepare_paths(corpus, sidecar_path)

    def test_sidecar_leaf_swap_between_lstat_and_open_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            generation = root / probe.SEALED_GENERATION
            generation.mkdir()
            corpus = generation / probe.CORPUS_NAME
            sidecar_path = root / "interaction-sidecar.json"
            replacement = root / "replacement.json"
            backup = root / "sidecar-backup.json"
            corpus.write_bytes(self.corpus_data)
            sidecar_path.write_bytes(self.sidecar_data)
            replacement.write_bytes(self.sidecar_data[:-1] + b" ")

            real_open = os.open
            opened_descriptors: list[int] = []

            def swap_open(path: os.PathLike[str], flags: int) -> int:
                target = Path(path)
                if target != sidecar_path:
                    return real_open(path, flags)
                os.replace(sidecar_path, backup)
                os.replace(replacement, sidecar_path)
                descriptor = real_open(sidecar_path, flags)
                opened_descriptors.append(descriptor)
                os.replace(sidecar_path, replacement)
                os.replace(backup, sidecar_path)
                return descriptor

            with mock.patch.object(
                interaction_sidecar.os,
                "open",
                side_effect=swap_open,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "pre-open path identity does not match opened descriptor",
                ):
                    probe.prepare_paths(corpus, sidecar_path)

            self.assertEqual(sidecar_path.read_bytes(), self.sidecar_data)
            self.assertEqual(len(opened_descriptors), 1)
            with self.assertRaises(OSError):
                os.fstat(opened_descriptors[0])

    def test_cli_stdout_is_canonical_utf8_and_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            sidecar_path = Path(temporary_directory) / "interaction-sidecar.json"
            sidecar_path.write_bytes(self.sidecar_data)
            command = [
                sys.executable,
                str(SCRIPT),
                "--corpus",
                str(CORPUS),
                "--interaction-sidecar",
                str(sidecar_path),
            ]
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
        self.assertEqual(outputs[0], probe.canonical_json_bytes(parsed))


if __name__ == "__main__":
    unittest.main()
