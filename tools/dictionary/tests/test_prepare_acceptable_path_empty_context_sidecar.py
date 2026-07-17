from __future__ import annotations

from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest

from tools.dictionary import compile_mozc_acceptable_path_evaluation as compiler
from tools.dictionary import prepare_acceptable_path_empty_context_sidecar as prepare
from tools.dictionary.tests.test_compile_mozc_acceptable_path_evaluation import (
    Fixture,
    record,
)


def parse_jsonl(data: bytes) -> list[dict[str, object]]:
    return [json.loads(line) for line in data.decode("utf-8").splitlines()]


class PrepareAcceptablePathEmptyContextSidecarTests(unittest.TestCase):
    def make_generation(self, root: Path) -> Path:
        source = root / "source"
        source.mkdir()
        first = record("case-1")
        second = record(
            "case-2", reading="はしをわたる", surfaces=["橋を渡る"]
        )
        second["source"]["row_sha256"] = "sha256:" + "4" * 64
        fixture = Fixture(source, [first, second])
        generated = fixture.prepare()
        generation = root / "generation"
        compiler.write_outputs(generated=generated, output_dir=generation)
        return generation / compiler.MANIFEST_NAME

    def test_binds_empty_context_to_reviewed_rows_in_target_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manifest = self.make_generation(Path(temporary_directory))
            rendered = prepare.prepare_sidecar_bytes(manifest)

        records = parse_jsonl(rendered)
        self.assertEqual([item["id"] for item in records], ["case-1", "case-2"])
        self.assertTrue(all(item["left_context"] == "" for item in records))
        self.assertTrue(
            all(
                item["left_context_sha256"]
                == "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
                for item in records
            )
        )
        self.assertEqual(
            [item["source_content_sha256"] for item in records],
            ["sha256:" + "3" * 64, "sha256:" + "4" * 64],
        )

    def test_rejects_generation_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manifest = self.make_generation(root)
            targets = manifest.parent / compiler.TARGETS_NAME
            targets.write_bytes(targets.read_bytes() + b"\n")
            with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                prepare.prepare_sidecar_bytes(manifest)

    def test_cli_refuses_to_replace_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manifest = self.make_generation(root)
            output = root / "context-empty.jsonl"
            self.assertEqual(
                prepare.main(
                    [
                        "--generation-manifest",
                        str(manifest),
                        "--output",
                        str(output),
                    ]
                ),
                0,
            )
            original = output.read_bytes()
            errors = io.StringIO()
            with redirect_stderr(errors):
                result = prepare.main(
                    [
                        "--generation-manifest",
                        str(manifest),
                        "--output",
                        str(output),
                    ]
                )
            self.assertEqual(result, 2)
            self.assertIn("output already exists", errors.getvalue())
            self.assertEqual(output.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
