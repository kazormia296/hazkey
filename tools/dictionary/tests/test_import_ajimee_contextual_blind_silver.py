from __future__ import annotations

from contextlib import redirect_stderr
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import unittest
from unittest import mock

from tools.dictionary import build_frozen_corpus as frozen
from tools.dictionary import import_ajimee_contextual_blind_silver as importer
from tools.dictionary import prepare_blind_silver_annotations as blind


def sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def upstream_rows() -> list[dict[str, object]]:
    return [
        {
            "index": "13",
            "context_text": "前の発話です。",
            "input": "ハシヲワタル",
            "expected_output": ["橋を渡る", "箸を渡る", "橋を渡る"],
            "original_text": "前の発話です。橋を渡る",
            "splitted_input_for_limited_input_length": [],
        },
        {
            "index": "2",
            "context_text": "",
            "input": "アメデス",
            "expected_output": ["雨です"],
            "original_text": "雨です",
            "splitted_input_for_limited_input_length": [],
        },
        {
            "index": "7",
            "context_text": "札幌へ来ました。",
            "input": "オオドオリコウエンヘイク",
            "expected_output": ["大通公園へ行く"],
            "original_text": "札幌へ来ました。大通公園へ行く",
            "splitted_input_for_limited_input_length": ["オオドオリ", "コウエンヘイク"],
        },
        {
            "index": "1",
            "context_text": "",
            "input": "ハレデス",
            "expected_output": ["晴れです"],
            "original_text": "晴れです",
            "splitted_input_for_limited_input_length": [],
        },
    ]


def render_raw(rows: list[dict[str, object]]) -> bytes:
    return (json.dumps(rows, ensure_ascii=False) + "\n").encode("utf-8")


def build_synthetic(raw: bytes) -> dict[str, bytes]:
    return importer._build_generation_for_contract(
        raw,
        expected_raw_sha256=sha256(raw),
        expected_total_rows=4,
        expected_contextual_rows=2,
        expected_empty_rows=2,
    )


def decode_jsonl(data: bytes) -> list[dict[str, object]]:
    return [json.loads(line) for line in data.decode("utf-8").splitlines()]


class AJIMEEContextualBlindSilverImporterTests(unittest.TestCase):
    def test_builds_deterministic_downstream_compatible_contextual_cases(self) -> None:
        raw = render_raw(upstream_rows())

        first = build_synthetic(raw)
        second = build_synthetic(raw)

        self.assertEqual(first, second)
        self.assertEqual(set(first), {importer.CASES_NAME, importer.MANIFEST_NAME})
        cases = decode_jsonl(first[importer.CASES_NAME])
        manifest = json.loads(first[importer.MANIFEST_NAME])

        self.assertEqual(
            [case["id"] for case in cases],
            [
                "ajimee-jwtd-v2-contextual-000007",
                "ajimee-jwtd-v2-contextual-000013",
            ],
        )
        self.assertEqual(
            [case["family_id"] for case in cases],
            ["ajimee-jwtd-v2-index-000007", "ajimee-jwtd-v2-index-000013"],
        )
        self.assertEqual(cases[0]["reading"], "おおどおりこうえんへいく")
        self.assertEqual(cases[1]["reading"], "はしをわたる")
        self.assertEqual(cases[1]["surface_references"], ["橋を渡る", "箸を渡る"])
        self.assertTrue(all(case["left_context"] for case in cases))
        self.assertTrue(all(case["dataset_role"] == "representative" for case in cases))
        self.assertTrue(all(case["fold"] == "exploration" for case in cases))
        self.assertTrue(all(case["schema"] == blind.CASE_SCHEMA for case in cases))
        self.assertTrue(
            all(
                re.fullmatch(
                    re.escape(
                        f"ajimee-bench@{frozen.AJIMEE_REVISION}:"
                        f"{frozen.AJIMEE_RAW_PATH}:row-sha256:"
                    )
                    + r"[0-9a-f]{64}",
                    case["source_revision"],
                )
                for case in cases
            )
        )

        # The exact emitted bytes are accepted by the next compiler without a
        # compatibility adapter.
        prepared = blind.prepare_outputs_bytes(first[importer.CASES_NAME])
        prepared_manifest = json.loads(prepared[blind.MANIFEST_NAME])
        self.assertEqual(prepared_manifest["counts"]["cases"], 2)
        self.assertEqual(prepared_manifest["counts"]["nonempty_left_context_cases"], 2)

        self.assertEqual(manifest["schema"], importer.MANIFEST_SCHEMA)
        self.assertTrue(manifest["diagnostic_only"])
        self.assertFalse(manifest["formal_authorized"])
        self.assertEqual(manifest["annotation_tier"], "silver_source")
        self.assertEqual(
            manifest["bindings"]["raw_snapshot"],
            {
                "repository": frozen.AJIMEE_REPOSITORY,
                "revision": frozen.AJIMEE_REVISION,
                "path": frozen.AJIMEE_RAW_PATH,
                "sha256": sha256(raw),
                "bytes": len(raw),
                "rows": 4,
            },
        )
        self.assertEqual(
            manifest["bindings"]["cases"]["sha256"],
            sha256(first[importer.CASES_NAME]),
        )
        self.assertEqual(manifest["bindings"]["cases"]["cases"], 2)
        self.assertTrue(
            manifest["bindings"]["nonempty_context_projection"]["all_nonempty"]
        )
        self.assertEqual(
            manifest["counts"],
            {
                "upstream_rows": 4,
                "upstream_contextual_rows": 2,
                "upstream_empty_context_rows": 2,
                "emitted_cases": 2,
                "emitted_families": 2,
                "dataset_roles": {"representative": 2},
                "folds": {"exploration": 2},
            },
        )
        self.assertEqual(manifest["rights"]["license"], frozen.AJIMEE_LICENSE)
        self.assertEqual(manifest["transform"]["reading"], frozen.NORMALIZATION_ID)
        self.assertEqual(
            manifest["transform"]["expected_outputs"],
            "stable-exact-deduplicate-preserve-first",
        )
        blindness = manifest["candidate_blind_source"]
        self.assertEqual(blindness["claim_scope"], "this-importer-selection-and-transform-only")
        self.assertEqual(blindness["selection_fields_consulted"], ["context_text"])
        self.assertFalse(blindness["engine_candidates_or_scores_consulted"])
        self.assertFalse(blindness["upstream_dataset_creation_blindness_claimed"])
        self.assertFalse(
            manifest["contracts"]["raw_snapshot_bytes_included_in_generation"]
        )

    def test_source_revision_row_digest_covers_every_upstream_field(self) -> None:
        rows = upstream_rows()
        raw = render_raw(rows)
        cases = decode_jsonl(build_synthetic(raw)[importer.CASES_NAME])
        case_by_id = {case["id"]: case for case in cases}

        upstream = rows[2]
        expected_row_sha = sha256(importer._canonical_json(upstream))
        self.assertTrue(
            case_by_id["ajimee-jwtd-v2-contextual-000007"]["source_revision"].endswith(
                "row-" + expected_row_sha
            )
        )

        changed = copy.deepcopy(rows)
        changed[2]["original_text"] = "別の原文です。"
        changed_raw = render_raw(changed)
        changed_cases = decode_jsonl(build_synthetic(changed_raw)[importer.CASES_NAME])
        changed_by_id = {case["id"]: case for case in changed_cases}
        self.assertNotEqual(
            case_by_id["ajimee-jwtd-v2-contextual-000007"]["source_revision"],
            changed_by_id["ajimee-jwtd-v2-contextual-000007"]["source_revision"],
        )

    def test_rejects_wrong_hash_count_partition_and_index_contracts(self) -> None:
        rows = upstream_rows()
        raw = render_raw(rows)
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            importer._build_generation_for_contract(
                raw,
                expected_raw_sha256="sha256:" + "0" * 64,
                expected_total_rows=4,
                expected_contextual_rows=2,
                expected_empty_rows=2,
            )
        with self.assertRaisesRegex(ValueError, "exactly 5"):
            importer._build_generation_for_contract(
                raw,
                expected_raw_sha256=sha256(raw),
                expected_total_rows=5,
                expected_contextual_rows=2,
                expected_empty_rows=3,
            )

        changed = copy.deepcopy(rows)
        changed[1]["context_text"] = "文脈が増えた"
        changed_raw = render_raw(changed)
        with self.assertRaisesRegex(ValueError, "context partition"):
            build_synthetic(changed_raw)

        changed = copy.deepcopy(rows)
        changed[1]["index"] = changed[0]["index"]
        changed_raw = render_raw(changed)
        with self.assertRaisesRegex(ValueError, "duplicate canonical indices"):
            build_synthetic(changed_raw)

        changed = copy.deepcopy(rows)
        changed[0]["index"] = "013"
        changed_raw = render_raw(changed)
        with self.assertRaisesRegex(ValueError, "canonical decimal"):
            build_synthetic(changed_raw)

    def test_rejects_schema_and_invalid_text_in_every_upstream_half(self) -> None:
        mutations: list[tuple[str, list[dict[str, object]]]] = []
        missing = copy.deepcopy(upstream_rows())
        del missing[0]["original_text"]
        mutations.append(("fields do not match", missing))
        unknown = copy.deepcopy(upstream_rows())
        unknown[0]["engine_score"] = 1
        mutations.append(("fields do not match", unknown))
        decomposed = copy.deepcopy(upstream_rows())
        decomposed[1]["original_text"] = "ハ\u3099"
        mutations.append(("NFC-normalized", decomposed))
        controlled = copy.deepcopy(upstream_rows())
        controlled[0]["context_text"] = "前\n後"
        mutations.append(("control", controlled))
        bad_expected = copy.deepcopy(upstream_rows())
        bad_expected[0]["expected_output"] = []
        mutations.append(("non-empty array", bad_expected))
        bad_split = copy.deepcopy(upstream_rows())
        bad_split[3]["splitted_input_for_limited_input_length"] = [1]
        mutations.append(("must be a string", bad_split))

        for message, rows in mutations:
            with self.subTest(message=message):
                raw = render_raw(rows)
                with self.assertRaisesRegex(ValueError, message):
                    build_synthetic(raw)

    def test_duplicate_json_keys_and_non_array_root_fail_closed(self) -> None:
        duplicate = b'[{"index":"1","index":"2"}]\n'
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            importer._build_generation_for_contract(
                duplicate,
                expected_raw_sha256=sha256(duplicate),
                expected_total_rows=1,
                expected_contextual_rows=0,
                expected_empty_rows=1,
            )
        root = b'{}\n'
        with self.assertRaisesRegex(ValueError, "must be an array"):
            importer._build_generation_for_contract(
                root,
                expected_raw_sha256=sha256(root),
                expected_total_rows=0,
                expected_contextual_rows=0,
                expected_empty_rows=0,
            )

    def test_cli_has_no_contract_override_and_rejects_synthetic_raw(self) -> None:
        raw = render_raw(upstream_rows())
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "raw.json"
            output = root / "generation"
            source.write_bytes(raw)
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                result = importer.main(
                    ["--input", str(source), "--output-dir", str(output)]
                )
            self.assertFalse(output.exists())
        self.assertEqual(result, 2)
        self.assertIn("pinned snapshot", stderr.getvalue())

        with mock.patch.object(
            importer, "_build_generation_for_contract", return_value={}
        ) as implementation:
            self.assertEqual(importer.build_generation(b"raw"), {})
        implementation.assert_called_once_with(
            b"raw",
            expected_raw_sha256=frozen.AJIMEE_RAW_SHA256,
            expected_total_rows=200,
            expected_contextual_rows=100,
            expected_empty_rows=100,
        )

    def test_publication_is_atomic_read_only_and_never_overwrites(self) -> None:
        generated = build_synthetic(render_raw(upstream_rows()))
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            output = root / "generation"
            importer.publish_generation(generated, output)
            self.assertTrue(output.is_dir())
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o555)
            for name, expected in generated.items():
                self.assertEqual((output / name).read_bytes(), expected)
                self.assertEqual(stat.S_IMODE((output / name).stat().st_mode), 0o444)

            with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
                importer.publish_generation(generated, output)
            self.assertEqual((output / importer.CASES_NAME).read_bytes(), generated[importer.CASES_NAME])
            self.assertFalse(any(path.name.startswith(".generation.staging-") for path in root.iterdir()))

            target = root / "target"
            target.mkdir()
            symlink_output = root / "symlink-generation"
            symlink_output.symlink_to(target, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
                importer.publish_generation(generated, symlink_output)
            self.assertEqual(list(target.iterdir()), [])
            self.assertFalse(
                any(path.name.startswith(".symlink-generation.staging-") for path in root.iterdir())
            )

    def test_publication_detects_post_verification_tampering_and_removes_own_inode(self) -> None:
        generated = build_synthetic(render_raw(upstream_rows()))
        real_rename = importer._rename_noreplace

        def tamper_then_rename(parent_fd: int, source: str, destination: str) -> None:
            directory_fd = os.open(
                source,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
            try:
                os.fchmod(directory_fd, 0o700)
                os.unlink(importer.CASES_NAME, dir_fd=directory_fd)
                descriptor = os.open(
                    importer.CASES_NAME,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o444,
                    dir_fd=directory_fd,
                )
                with os.fdopen(descriptor, "wb") as output:
                    output.write(b"tampered\n")
                    output.flush()
                    os.fsync(output.fileno())
                os.fchmod(directory_fd, 0o555)
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            real_rename(parent_fd, source, destination)

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            output = root / "generation"
            with mock.patch.object(
                importer,
                "_rename_noreplace",
                side_effect=tamper_then_rename,
            ):
                with self.assertRaisesRegex(ValueError, "published generation output changed"):
                    importer.publish_generation(generated, output)
            self.assertFalse(output.exists())
            self.assertFalse(
                any(path.name.startswith(".generation.staging-") for path in root.iterdir())
            )

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "requires O_NOFOLLOW")
    def test_exact_input_reader_rejects_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "raw.json"
            source.write_bytes(render_raw(upstream_rows()))
            link = root / "link.json"
            link.symlink_to(source)
            with self.assertRaisesRegex(ValueError, "non-symlink"):
                importer._read_regular(link)


if __name__ == "__main__":
    unittest.main()
