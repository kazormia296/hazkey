from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools.dictionary import prepare_mozc_hybrid_boundary_annotations as prepare


def token(
    index: int,
    surface: str,
    major: str,
    lexical_reading: str,
    *,
    sub1: str = "*",
    sub2: str = "*",
    gap_before: str = "",
) -> dict[str, object]:
    return {
        "token_index": index,
        "byte_start": 0,
        "byte_end": len(surface.encode("utf-8")),
        "surface": surface,
        "pos_major": major,
        "pos_sub1": sub1,
        "pos_sub2": sub2,
        "lexical_reading": lexical_reading,
        "orth_surface": surface,
        "pronunciation": lexical_reading,
        "gap_before": gap_before,
    }


class PrepareMozcHybridBoundaryAnnotationsTests(unittest.TestCase):
    def test_loads_strict_corpus_and_preserves_expected_alternatives(self) -> None:
        data = (
            "id\treading\texpected\tcategory\n"
            "case-1\tきづく\t気付く|気づく\tcolloquial\n"
        ).encode()

        rows = prepare.load_corpus_bytes(data, "fixture.tsv")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].case_id, "case-1")
        self.assertEqual(rows[0].expected_surfaces, ("気付く", "気づく"))
        self.assertTrue(rows[0].row_sha256.startswith("sha256:"))

    def test_rejects_duplicate_ids_and_non_nfc_input(self) -> None:
        duplicate = (
            "id\treading\texpected\tcategory\n"
            "case-1\ta\ta\ttest\n"
            "case-1\tb\tb\ttest\n"
        ).encode()
        with self.assertRaisesRegex(ValueError, "duplicates id"):
            prepare.load_corpus_bytes(duplicate, "duplicate.tsv")

        non_nfc = (
            "id\treading\texpected\tcategory\n"
            "case-1\te\u0301\té\ttest\n"
        ).encode()
        with self.assertRaisesRegex(ValueError, "NFC-normalized"):
            prepare.load_corpus_bytes(non_nfc, "non-nfc.tsv")

    def test_parses_helper_protocol_and_allows_only_whitespace_gaps(self) -> None:
        output = (
            "T\tcase::alt-0\t0\t0\t1\tA\t名詞\t普通名詞\t一般\t*\t*\t*\n"
            "T\tcase::alt-0\t1\t2\t3\tB\t名詞\t普通名詞\t一般\t*\t*\t*\n"
            "E\tcase::alt-0\n"
        ).encode()

        parsed = prepare.parse_helper_output(
            output, [("case::alt-0", "A B")]
        )

        self.assertEqual(parsed["case::alt-0"][1]["gap_before"], " ")
        self.assertEqual(parsed["case::alt-0"][1]["surface"], "B")

        skipped_non_whitespace = output.replace(b"2\t3\tB", b"2\t3\tB")
        with self.assertRaisesRegex(ValueError, "skipped non-whitespace"):
            prepare.parse_helper_output(
                skipped_non_whitespace, [("case::alt-0", "AxB")]
            )

    def test_rejects_noncanonical_or_out_of_order_helper_records(self) -> None:
        noncanonical = (
            "T\tcase::alt-0\t00\t0\t1\tA\t名詞\t*\t*\t*\t*\t*\n"
            "E\tcase::alt-0\n"
        ).encode()
        with self.assertRaisesRegex(ValueError, "canonical"):
            prepare.parse_helper_output(
                noncanonical, [("case::alt-0", "A")]
            )

        wrong_id = (
            "T\twrong\t0\t0\t1\tA\t名詞\t*\t*\t*\t*\t*\n"
            "E\twrong\n"
        ).encode()
        with self.assertRaisesRegex(ValueError, "expected helper id"):
            prepare.parse_helper_output(wrong_id, [("case::alt-0", "A")])

    def test_uses_literal_ascii_and_groups_dependent_tokens(self) -> None:
        groups, ambiguity = prepare.group_bunsetsu_tokens(
            [
                token(0, "L", "記号", "エル"),
                token(1, "T", "記号", "ティー"),
                token(2, "O", "記号", "オー"),
                token(3, "を", "助詞", "ヲ"),
                token(4, "使う", "動詞", "ツカウ"),
            ]
        )
        self.assertEqual(
            [[item["surface"] for item in group] for group in groups],
            [["L", "T", "O", "を"], ["使う"]],
        )
        self.assertEqual(ambiguity, [])

        annotation, _ = prepare.annotate_alternative(
            alternative_index=0,
            surface="LTOを使う",
            source_reading="LTOをつかう",
            helper_id="case::alt-0",
            tokens=[
                token(0, "L", "記号", "エル"),
                token(1, "T", "記号", "ティー"),
                token(2, "O", "記号", "オー"),
                token(3, "を", "助詞", "ヲ"),
                token(4, "使う", "動詞", "ツカウ"),
            ],
        )
        self.assertEqual(annotation["marked_reading"], "LTOを|つかう")
        self.assertEqual(annotation["confidence"], "exact")

        predicate_groups, _ = prepare.group_bunsetsu_tokens(
            [
                token(0, "野菜", "名詞", "ヤサイ"),
                token(1, "を", "助詞", "ヲ"),
                token(2, "切る", "動詞", "キル", sub1="非自立可能"),
            ]
        )
        self.assertEqual(
            [[item["surface"] for item in group] for group in predicate_groups],
            [["野菜", "を"], ["切る"]],
        )

    def test_prepares_blind_pending_review_records(self) -> None:
        corpus = (
            "id\treading\texpected\tcategory\n"
            "case-1\tわたしはがくせいです\t私は学生です\thomophone-context\n"
        ).encode()
        helper_tokens = {
            "case-1::alt-0": [
                token(0, "私", "代名詞", "ワタシ"),
                token(1, "は", "助詞", "ハ"),
                token(2, "学生", "名詞", "ガクセイ"),
                token(3, "です", "助動詞", "デス"),
            ]
        }
        with mock.patch.object(
            prepare, "run_lindera_helper", return_value=helper_tokens
        ) as helper:
            records = prepare.prepare_records(corpus, Path("/fake/tokenizer"))

        helper.assert_called_once()
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["schema"], prepare.SCHEMA)
        self.assertTrue(record["known_source_reused"])
        self.assertTrue(record["diagnostic_only"])
        self.assertFalse(record["formal_authorized"])
        self.assertFalse(record["candidate_outputs_consulted"])
        self.assertEqual(record["preannotation"]["marked_reading"], "わたしは|がくせいです")
        self.assertEqual(record["preannotation"]["first_segment_count"], 4)
        self.assertEqual(record["preannotation"]["confidence"], "exact")
        self.assertEqual(record["review"]["status"], "pending")
        self.assertIsNone(record["review"]["marked_reading"])
        self.assertNotIn("candidate", record["token_audit"])

        encoded = prepare.canonical_jsonl(records)
        self.assertEqual(json.loads(encoded), record)

    def test_main_writes_deterministic_jsonl_and_summary(self) -> None:
        corpus = (
            "id\treading\texpected\tcategory\n"
            "case-1\tわたしは\t私は\ttest\n"
        ).encode()
        helper_tokens = {
            "case-1::alt-0": [
                token(0, "私", "代名詞", "ワタシ"),
                token(1, "は", "助詞", "ハ"),
            ]
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            corpus_path = root / "corpus.tsv"
            output_path = root / "preannotations.jsonl"
            summary_path = root / "summary.json"
            corpus_path.write_bytes(corpus)
            with mock.patch.object(
                prepare, "run_lindera_helper", return_value=helper_tokens
            ), mock.patch(
                "sys.argv",
                [
                    "prepare",
                    "--corpus",
                    str(corpus_path),
                    "--lindera-tokenizer",
                    str(root / "tokenizer"),
                    "--output",
                    str(output_path),
                    "--summary-output",
                    str(summary_path),
                ],
            ):
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(prepare.main(), 0)

            first = output_path.read_bytes()
            summary = json.loads(summary_path.read_text())

        self.assertTrue(first.endswith(b"\n"))
        self.assertEqual(summary["cases"], 1)
        self.assertEqual(summary["output_sha256"], prepare.sha256_bytes(first))


if __name__ == "__main__":
    unittest.main()
