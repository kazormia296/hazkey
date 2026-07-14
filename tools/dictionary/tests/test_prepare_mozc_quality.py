from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT))

from tools.dictionary import prepare_mozc_quality  # noqa: E402


SCRIPT = REPOSITORY_ROOT / "tools/dictionary/prepare_mozc_quality.py"


class PrepareMozcQualityTests(unittest.TestCase):
    def test_cli_renders_utf8_lf_and_first_expected_alternative(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            corpus_path = directory / "corpus.tsv"
            output_path = directory / "mozc.tsv"
            corpus_path.write_bytes(
                (
                    "id\treading\texpected\tcategory\r\n"
                    "kana\tかな\t仮名|かな\tcommon\r\n"
                    "emoji\tえもじ\t絵文字|😀\tunicode\r\n"
                ).encode("utf-8")
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--corpus",
                    str(corpus_path),
                    "--output",
                    str(output_path),
                    "--repeat",
                    "2",
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
            self.assertEqual(
                output_path.read_bytes(),
                (
                    "# label\tkey\tvalue\tcommand\n"
                    "ab-1-kana\tかな\t仮名\tConversion Expected\n"
                    "ab-1-emoji\tえもじ\t絵文字\tConversion Expected\n"
                    "ab-2-kana\tかな\t仮名\tConversion Expected\n"
                    "ab-2-emoji\tえもじ\t絵文字\tConversion Expected\n"
                ).encode("utf-8"),
            )

    def test_repeat_defaults_to_one(self) -> None:
        corpus = [
            {
                "id": "default",
                "reading": "よみ",
                "expected": "第一|第二",
                "category": "sample",
            }
        ]

        self.assertEqual(
            prepare_mozc_quality.render_quality_regression(corpus),
            "# label\tkey\tvalue\tcommand\n"
            "ab-1-default\tよみ\t第一\tConversion Expected\n",
        )

    def test_invalid_repeat_exits_two_without_traceback(self) -> None:
        for repeat in ("0", "-1", "not-an-integer"):
            with self.subTest(repeat=repeat):
                result = subprocess.run(
                    [
                        sys.executable,
                        str(SCRIPT),
                        "--corpus",
                        "unused.tsv",
                        "--output",
                        "unused-output.tsv",
                        "--repeat",
                        repeat,
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                )

                self.assertEqual(result.returncode, 2)
                self.assertIn("positive integer", result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_corpus_error_exits_two_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            corpus_path = directory / "invalid.tsv"
            output_path = directory / "mozc.tsv"
            corpus_path.write_text(
                "id\treading\texpected\tcategory\n"
                "duplicate\tよみ\t期待\tsample\n"
                "duplicate\tよみ2\t期待2\tsample\n",
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--corpus",
                    str(corpus_path),
                    "--output",
                    str(output_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("error:", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertFalse(output_path.exists())

    def test_tsv_control_characters_are_rejected(self) -> None:
        corpus = [
            {
                "id": "unsafe",
                "reading": "line\nbreak",
                "expected": "expected",
                "category": "sample",
            }
        ]

        with self.assertRaisesRegex(ValueError, "tabs or newlines"):
            prepare_mozc_quality.render_quality_regression(corpus)


if __name__ == "__main__":
    unittest.main()
