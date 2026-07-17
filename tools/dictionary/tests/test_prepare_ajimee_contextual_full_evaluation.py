from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
import unittest

from tools.dictionary import import_ajimee_contextual_blind_silver as importer
from tools.dictionary import prepare_ajimee_contextual_full_evaluation as preparer
from tools.dictionary.tests.test_import_ajimee_contextual_blind_silver import (
    render_raw,
    sha256,
    upstream_rows,
)


def synthetic_inputs() -> tuple[bytes, dict[str, bytes], dict[str, bytes]]:
    raw = render_raw(upstream_rows())
    imported = importer._build_generation_for_contract(
        raw,
        expected_raw_sha256=sha256(raw),
        expected_total_rows=4,
        expected_contextual_rows=2,
        expected_empty_rows=2,
    )
    generated = preparer._build_generation_for_contract(
        raw,
        imported,
        expected_raw_sha256=sha256(raw),
        expected_total_rows=4,
        expected_contextual_rows=2,
        expected_empty_rows=2,
    )
    return raw, imported, generated


def decode_jsonl(data: bytes) -> list[dict[str, object]]:
    return [json.loads(line) for line in data.decode().splitlines()]


class AJIMEEContextualFullEvaluationPreparationTests(unittest.TestCase):
    def test_builds_deterministic_full_targets_probe_and_bound_context_pair(self) -> None:
        raw, imported, first = synthetic_inputs()
        second = preparer._build_generation_for_contract(
            raw,
            imported,
            expected_raw_sha256=sha256(raw),
            expected_total_rows=4,
            expected_contextual_rows=2,
            expected_empty_rows=2,
        )
        self.assertEqual(first, second)
        self.assertEqual(set(first), set(preparer.OUTPUT_NAMES))
        self.assertEqual(
            first[preparer.IMPORT_CASES_NAME], imported[importer.CASES_NAME]
        )
        self.assertEqual(
            first[preparer.IMPORT_MANIFEST_NAME], imported[importer.MANIFEST_NAME]
        )

        targets = decode_jsonl(first[preparer.TARGETS_NAME])
        probes = decode_jsonl(first[preparer.PROBE_INPUT_NAME])
        contexts = decode_jsonl(first[preparer.CONTEXT_NAME])
        empty = decode_jsonl(first[preparer.EMPTY_CONTEXT_NAME])
        manifest = json.loads(first[preparer.MANIFEST_NAME])
        self.assertEqual([target["id"] for target in targets], [probe["id"] for probe in probes])
        self.assertEqual(targets[1]["surface_references"], ["橋を渡る", "箸を渡る"])
        for target, probe, context, control in zip(
            targets, probes, contexts, empty, strict=True
        ):
            self.assertEqual(target["schema"], preparer.TARGET_SCHEMA)
            self.assertEqual(target["category"], preparer.CATEGORY)
            self.assertEqual(
                "".join(element["text"] for element in probe["elements"]),
                target["reading"],
            )
            self.assertTrue(all(element["input_style"] == "direct" for element in probe["elements"]))
            self.assertTrue(context["left_context"])
            self.assertEqual(control["left_context"], "")
            self.assertEqual(
                context["source_content_sha256"], target["source_content_sha256"]
            )
            self.assertEqual(
                control["source_content_sha256"], target["source_content_sha256"]
            )
        self.assertEqual(manifest["schema"], preparer.MANIFEST_SCHEMA)
        self.assertTrue(manifest["diagnostic_only"])
        self.assertFalse(manifest["formal_authorized"])
        self.assertEqual(manifest["counts"]["cases"], 2)
        self.assertEqual(manifest["counts"]["surface_references"], 3)
        self.assertEqual(
            manifest["bindings"]["raw_snapshot"]["sha256"], sha256(raw)
        )
        self.assertTrue(manifest["contracts"]["import_generation_exactly_rederived"])
        self.assertFalse(
            manifest["source_import"]["candidate_blind_source"][
                "engine_candidates_or_scores_consulted"
            ]
        )

    def test_import_cases_and_manifest_must_be_exact_raw_derivations(self) -> None:
        raw, imported, _generated = synthetic_inputs()
        for name in (importer.CASES_NAME, importer.MANIFEST_NAME):
            with self.subTest(name=name):
                changed = dict(imported)
                changed[name] += b" "
                with self.assertRaisesRegex(ValueError, "not the exact raw-snapshot derivation"):
                    preparer._build_generation_for_contract(
                        raw,
                        changed,
                        expected_raw_sha256=sha256(raw),
                        expected_total_rows=4,
                        expected_contextual_rows=2,
                        expected_empty_rows=2,
                    )
        changed = dict(imported)
        changed["extra"] = b""
        with self.assertRaisesRegex(ValueError, "file set differs"):
            preparer._build_generation_for_contract(
                raw,
                changed,
                expected_raw_sha256=sha256(raw),
                expected_total_rows=4,
                expected_contextual_rows=2,
                expected_empty_rows=2,
            )

    def test_complete_generation_is_rederived_and_any_file_tamper_fails(self) -> None:
        raw, _imported, generated = synthetic_inputs()
        def rederive(values: dict[str, bytes]) -> dict[str, bytes]:
            return preparer._rederive_generation_for_contract(
                raw,
                values,
                expected_raw_sha256=sha256(raw),
                expected_total_rows=4,
                expected_contextual_rows=2,
                expected_empty_rows=2,
            )

        self.assertEqual(rederive(generated), generated)
        for name in sorted(preparer.OUTPUT_NAMES):
            with self.subTest(name=name):
                changed = dict(generated)
                changed[name] += b" "
                with self.assertRaisesRegex(
                    ValueError,
                    "not (?:the exact raw-snapshot derivation|exactly rederived)",
                ):
                    rederive(changed)

    def test_directory_readers_reject_extra_files_and_symlink_directories(self) -> None:
        _raw, imported, generated = synthetic_inputs()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            import_dir = root / "import"
            import_dir.mkdir()
            for name, data in imported.items():
                (import_dir / name).write_bytes(data)
            self.assertEqual(preparer.read_import_generation(import_dir), imported)
            (import_dir / "extra").write_text("x")
            with self.assertRaisesRegex(ValueError, "file set differs"):
                preparer.read_import_generation(import_dir)

            generation_dir = root / "generation"
            generation_dir.mkdir()
            for name, data in generated.items():
                (generation_dir / name).write_bytes(data)
            link = root / "generation-link"
            link.symlink_to(generation_dir, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "non-symlink directory"):
                preparer.capture_generation(link / preparer.MANIFEST_NAME)

    def test_atomic_publication_is_read_only_and_never_overwrites(self) -> None:
        _raw, _imported, generated = synthetic_inputs()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "generation"
            preparer.publish_generation(generated, output)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o555)
            for name, expected in generated.items():
                path = output / name
                self.assertEqual(path.read_bytes(), expected)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o444)
                self.assertEqual(path.stat().st_nlink, 1)
            self.assertEqual(
                preparer.capture_generation(output / preparer.MANIFEST_NAME),
                generated,
            )
            with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
                preparer.publish_generation(generated, output)
            self.assertFalse(
                any(path.name.startswith(".generation.staging-") for path in root.iterdir())
            )

            target = root / "target"
            target.mkdir()
            link = root / "link"
            link.symlink_to(target, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
                preparer.publish_generation(generated, link)
            self.assertEqual(list(target.iterdir()), [])

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "requires O_NOFOLLOW")
    def test_import_reader_rejects_symlinked_files(self) -> None:
        _raw, imported, _generated = synthetic_inputs()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            import_dir = root / "import"
            import_dir.mkdir()
            cases = root / "cases"
            cases.write_bytes(imported[importer.CASES_NAME])
            (import_dir / importer.CASES_NAME).symlink_to(cases)
            (import_dir / importer.MANIFEST_NAME).write_bytes(
                imported[importer.MANIFEST_NAME]
            )
            with self.assertRaises(OSError):
                preparer.read_import_generation(import_dir)


if __name__ == "__main__":
    unittest.main()
