from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest

from tools.dictionary import prepare_blind_silver_annotations as prepare
from tools.dictionary import serve_mozc_boundary_annotations as server


def render_jsonl(records: list[dict[str, object]]) -> bytes:
    return b"".join(
        json.dumps(
            record,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
        for record in records
    )


def sample_cases() -> list[dict[str, object]]:
    return [
        {
            "schema": prepare.CASE_SCHEMA,
            "id": "silver-rep-0001",
            "family_id": "family-weather-0001",
            "source_revision": "source-corpus@sha256:1111",
            "dataset_role": "representative",
            "fold": "exploration",
            "reading": "きょうはあめです",
            "surface_references": ["今日は雨です"],
            "left_context": "",
        },
        {
            "schema": prepare.CASE_SCHEMA,
            "id": "silver-rep-0002",
            "family_id": "family-weather-0001",
            "source_revision": "source-corpus@sha256:1111",
            "dataset_role": "representative",
            "fold": "exploration",
            "reading": "あしたははれです",
            "surface_references": ["明日は晴れです"],
            "left_context": "天気予報では、",
        },
        {
            "schema": prepare.CASE_SCHEMA,
            "id": "silver-disagree-0001",
            "family_id": "family-proper-noun-0001",
            "source_revision": "source-corpus@sha256:2222",
            "dataset_role": "disagreement_enriched",
            "fold": "final_locked",
            "reading": "おおどおりこうえんへいく",
            "surface_references": ["大通公園へ行く"],
            "left_context": "札幌の観光で",
        },
    ]


def sample_selection() -> list[dict[str, object]]:
    return [
        {
            "schema": prepare.SELECTION_INPUT_SCHEMA,
            "id": "silver-disagree-0001",
            "source_revision": "source-corpus@sha256:2222",
            "selection_policy_revision": "selector-v1",
            "selection_reasons": [
                "top1-surface-difference",
                "score-band-priority",
            ],
            "runtime_features": {
                "top1_surface_differs": True,
                "top1_boundary_differs": False,
                "mozc_top1_consuming_count": 6,
                "hazkey_zenzai_top1_consuming_count": 6,
                "zenzai_score": -12.5,
                "zenzai_score_token_count": 5,
                "zenzai_score_scope": "full_candidate",
                "zenzai_score_band_id": "raw-neg20-neg10",
                "normalized_candidate_overlap_count": 2,
                "mozc_only_candidate_count": 4,
                "hazkey_zenzai_only_candidate_count": 3,
            },
        }
    ]


def decode_jsonl(data: bytes) -> list[dict[str, object]]:
    return [json.loads(line) for line in data.decode("utf-8").splitlines()]


class BlindSilverAnnotationPreparationTests(unittest.TestCase):
    def test_builds_deterministic_separated_server_compatible_generation(self) -> None:
        cases_data = render_jsonl(sample_cases())
        selection_data = render_jsonl(sample_selection())

        first = prepare.prepare_outputs_bytes(cases_data, selection_data)
        second = prepare.prepare_outputs_bytes(cases_data, selection_data)

        self.assertEqual(first, second)
        self.assertEqual(
            set(first),
            {
                prepare.SOURCE_NAME,
                prepare.ASSIGNMENT_NAME,
                prepare.CONTEXT_NAME,
                prepare.EMPTY_CONTEXT_NAME,
                prepare.QUEUE_SEED_NAME,
                prepare.SELECTION_NAME,
                prepare.MANIFEST_NAME,
            },
        )
        sources = decode_jsonl(first[prepare.SOURCE_NAME])
        assignments = decode_jsonl(first[prepare.ASSIGNMENT_NAME])
        contexts = decode_jsonl(first[prepare.CONTEXT_NAME])
        empty_contexts = decode_jsonl(first[prepare.EMPTY_CONTEXT_NAME])
        queue = decode_jsonl(first[prepare.QUEUE_SEED_NAME])
        selections = decode_jsonl(first[prepare.SELECTION_NAME])
        manifest = json.loads(first[prepare.MANIFEST_NAME])

        self.assertEqual([item["id"] for item in sources], [
            "silver-rep-0001",
            "silver-rep-0002",
            "silver-disagree-0001",
        ])
        self.assertEqual(
            set(sources[0]),
            {
                "schema",
                "id",
                "source_revision",
                "reading",
                "surface_references",
                "content_sha256",
            },
        )
        self.assertNotIn("family_id", sources[0])
        self.assertNotIn("dataset_role", sources[0])
        self.assertNotIn("fold", sources[0])
        self.assertEqual(
            set(assignments[0]),
            {
                "schema",
                "id",
                "family_id",
                "dataset_role",
                "fold",
                "source_content_sha256",
                "input_case_sha256",
            },
        )
        self.assertNotIn("reading", assignments[0])
        self.assertNotIn("surface_references", assignments[0])
        self.assertEqual([item["left_context"] for item in contexts], [
            "",
            "天気予報では、",
            "札幌の観光で",
        ])
        self.assertEqual(
            contexts[0]["left_context_sha256"], prepare._sha256(b"")
        )
        self.assertTrue(all(item["left_context"] == "" for item in empty_contexts))
        self.assertEqual(
            [item["source_content_sha256"] for item in empty_contexts],
            [item["source_content_sha256"] for item in contexts],
        )
        self.assertEqual(len(selections), 1)
        self.assertEqual(selections[0]["id"], "silver-disagree-0001")
        self.assertEqual(
            selections[0]["runtime_features"]["zenzai_score_per_token"],
            -2.5,
        )
        self.assertEqual(
            sum(selections[0]["runtime_features"]["input_shape"].values()),
            len("おおどおりこうえんへいく"),
        )
        self.assertNotIn("gold", first[prepare.SELECTION_NAME].decode("utf-8"))
        self.assertNotIn("left_context", first[prepare.SELECTION_NAME].decode("utf-8"))
        context_text = first[prepare.CONTEXT_NAME].decode("utf-8")
        self.assertNotIn("surface_references", context_text)
        self.assertNotIn("zenzai_score", context_text)

        self.assertTrue(all(item["category"] == "blind-silver" for item in queue))
        self.assertTrue(
            all(item["candidate_outputs_consulted"] is False for item in queue)
        )
        self.assertEqual(
            set(queue[0]["source"]),
            {"corpus_sha256", "row_sha256", "reading", "expected_surfaces"},
        )
        queue_text = first[prepare.QUEUE_SEED_NAME].decode("utf-8")
        for assignment_field in (
            '"family_id"',
            '"source_revision"',
            '"dataset_role"',
            '"fold"',
            '"left_context"',
            '"zenzai_score"',
            '"mozc_candidates"',
        ):
            self.assertNotIn(assignment_field, queue_text)

        self.assertEqual(manifest["schema"], prepare.MANIFEST_SCHEMA)
        self.assertEqual(manifest["annotation_tier"], "silver_seed")
        self.assertFalse(manifest["formal_authorized"])
        self.assertEqual(manifest["counts"]["cases"], 3)
        self.assertEqual(manifest["counts"]["families"], 2)
        self.assertEqual(manifest["counts"]["multi_case_families"], 1)
        self.assertEqual(manifest["counts"]["final_locked_families"], 1)
        self.assertEqual(
            manifest["counts"]["dataset_roles"],
            {"disagreement_enriched": 1, "representative": 2},
        )
        for name, binding_name in (
            (prepare.SOURCE_NAME, "source"),
            (prepare.ASSIGNMENT_NAME, "assignment"),
            (prepare.CONTEXT_NAME, "context"),
            (prepare.EMPTY_CONTEXT_NAME, "empty_context"),
            (prepare.QUEUE_SEED_NAME, "queue_seed"),
            (prepare.SELECTION_NAME, "selection"),
        ):
            self.assertEqual(
                manifest["bindings"][binding_name]["sha256"],
                prepare._sha256(first[name]),
            )
        self.assertEqual(
            manifest["bindings"]["input_cases"]["sha256"],
            prepare._sha256(cases_data),
        )
        self.assertTrue(manifest["contracts"]["family_fold_disjoint"])
        self.assertEqual(
            manifest["contracts"]["selection_assignment_metadata"],
            "assignment-sidecar-only",
        )
        self.assertEqual(
            manifest["contracts"]["selection_runtime_metadata"],
            "selection-sidecar-only",
        )
        self.assertFalse(
            manifest["contracts"][
                "llm_annotation_payload_engine_candidates_or_scores"
            ]
        )
        self.assertFalse(
            manifest["contracts"]["left_context_llm_annotation_payload_included"]
        )
        self.assertFalse(manifest["contracts"]["left_context_engine_or_score_derived"])
        self.assertFalse(manifest["contracts"]["left_context_gold_label_derived"])
        self.assertTrue(
            manifest["contracts"]["empty_context_baseline_all_cases_explicit"]
        )
        self.assertTrue(
            manifest["contracts"]["empty_context_baseline_source_bindings_equal"]
        )
        self.assertEqual(manifest["counts"]["empty_left_context_cases"], 1)
        self.assertEqual(manifest["counts"]["nonempty_left_context_cases"], 2)
        self.assertEqual(manifest["counts"]["selection_metadata_records"], 1)

        with tempfile.TemporaryDirectory() as temporary_directory:
            queue_path = Path(temporary_directory) / prepare.QUEUE_SEED_NAME
            queue_path.write_bytes(first[prepare.QUEUE_SEED_NAME])
            loaded = server.load_queue(queue_path)
        self.assertEqual(len(loaded.records), 3)
        self.assertEqual(
            loaded.records[2]["source"]["expected_surfaces"],
            ["大通公園へ行く"],
        )

    def test_rejects_family_crossing_fold_or_dataset_role(self) -> None:
        cross_fold = sample_cases()[:2]
        cross_fold[1]["fold"] = "final_locked"
        with self.assertRaisesRegex(ValueError, "crosses folds"):
            prepare.load_cases_bytes(render_jsonl(cross_fold))

        cross_role = sample_cases()[:2]
        cross_role[1]["dataset_role"] = "disagreement_enriched"
        with self.assertRaisesRegex(ValueError, "changes dataset_role"):
            prepare.load_cases_bytes(render_jsonl(cross_role))

    def test_rejects_duplicate_ids_sources_and_surface_references(self) -> None:
        duplicate_id = sample_cases()[:2]
        duplicate_id[1]["id"] = duplicate_id[0]["id"]
        with self.assertRaisesRegex(ValueError, "duplicate case id"):
            prepare.load_cases_bytes(render_jsonl(duplicate_id))

        duplicate_source = sample_cases()[:2]
        duplicate_source[1]["reading"] = duplicate_source[0]["reading"]
        duplicate_source[1]["surface_references"] = duplicate_source[0][
            "surface_references"
        ]
        duplicate_source[1]["left_context"] = duplicate_source[0]["left_context"]
        with self.assertRaisesRegex(ValueError, "duplicate annotation source"):
            prepare.load_cases_bytes(render_jsonl(duplicate_source))

        contextual_variants = copy.deepcopy(duplicate_source)
        contextual_variants[1]["left_context"] = "別の左文脈"
        self.assertEqual(
            len(prepare.load_cases_bytes(render_jsonl(contextual_variants))), 2
        )

        reordered_references = sample_cases()[:2]
        reordered_references[1]["reading"] = reordered_references[0]["reading"]
        reordered_references[0]["surface_references"] = ["今日は雨です", "きょうは雨です"]
        reordered_references[1]["surface_references"] = ["きょうは雨です", "今日は雨です"]
        reordered_references[1]["left_context"] = reordered_references[0]["left_context"]
        with self.assertRaisesRegex(ValueError, "duplicate annotation source"):
            prepare.load_cases_bytes(render_jsonl(reordered_references))

        duplicate_surfaces = sample_cases()[:1]
        duplicate_surfaces[0]["surface_references"] = ["今日は雨です", "今日は雨です"]
        with self.assertRaisesRegex(ValueError, "contains duplicates"):
            prepare.load_cases_bytes(render_jsonl(duplicate_surfaces))

    def test_rejects_unknown_or_engine_fields_in_authoritative_cases(self) -> None:
        for field, value in (
            ("zenzai_score", -1.5),
            ("mozc_candidates", ["候補"]),
            ("hazkey_top1", "候補"),
        ):
            with self.subTest(field=field):
                cases = sample_cases()[:1]
                cases[0][field] = value
                with self.assertRaisesRegex(ValueError, "unknown"):
                    prepare.load_cases_bytes(render_jsonl(cases))

        nested_candidate = sample_cases()[:1]
        nested_candidate[0]["surface_references"] = [
            {"text": "今日は雨です", "score": 1.0}
        ]
        with self.assertRaisesRegex(ValueError, "must be a non-empty string"):
            prepare.load_cases_bytes(render_jsonl(nested_candidate))

        duplicate_key = (
            '{"schema":"%s","id":"case-1","id":"case-2",'
            '"family_id":"family-1","source_revision":"rev-1",'
            '"dataset_role":"representative","fold":"exploration",'
            '"reading":"よみ","surface_references":["読み"]}\n'
            % prepare.CASE_SCHEMA
        ).encode("utf-8")
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            prepare.load_cases_bytes(duplicate_key)

    def test_selection_sidecar_is_runtime_only_strict_and_complete(self) -> None:
        cases = prepare.load_cases_bytes(render_jsonl(sample_cases()))
        sources = [prepare._source_record(case) for case in cases]
        records = prepare.load_selection_bytes(
            render_jsonl(sample_selection()),
            cases=cases,
            source_records=sources,
        )
        self.assertEqual([record["id"] for record in records], [
            "silver-disagree-0001"
        ])

        missing = sample_selection()
        missing.clear()
        with self.assertRaisesRegex(ValueError, "BOM-free"):
            prepare.load_selection_bytes(
                b"",
                cases=cases,
                source_records=sources,
            )

        representative = sample_selection()
        representative[0]["id"] = "silver-rep-0001"
        representative[0]["source_revision"] = "source-corpus@sha256:1111"
        with self.assertRaisesRegex(ValueError, "disagreement_enriched"):
            prepare.load_selection_bytes(
                render_jsonl(representative),
                cases=cases,
                source_records=sources,
            )

        unknown_gold = sample_selection()
        unknown_gold[0]["runtime_features"]["gold_hit"] = True
        with self.assertRaisesRegex(ValueError, "unknown"):
            prepare.load_selection_bytes(
                render_jsonl(unknown_gold),
                cases=cases,
                source_records=sources,
            )

        gold_reason = sample_selection()
        gold_reason[0]["selection_reasons"] = ["gold-rescue"]
        with self.assertRaisesRegex(ValueError, "gold-derived"):
            prepare.load_selection_bytes(
                render_jsonl(gold_reason),
                cases=cases,
                source_records=sources,
            )

        boundary_mismatch = sample_selection()
        boundary_mismatch[0]["runtime_features"]["top1_boundary_differs"] = True
        with self.assertRaisesRegex(ValueError, "disagrees with counts"):
            prepare.load_selection_bytes(
                render_jsonl(boundary_mismatch),
                cases=cases,
                source_records=sources,
            )

        partial_score = sample_selection()
        partial_score[0]["runtime_features"]["zenzai_score_scope"] = None
        with self.assertRaisesRegex(ValueError, "all null or present"):
            prepare.load_selection_bytes(
                render_jsonl(partial_score),
                cases=cases,
                source_records=sources,
            )

        nonfinite_score = sample_selection()
        nonfinite_score[0]["runtime_features"]["zenzai_score"] = float("inf")
        with self.assertRaisesRegex(ValueError, "finite number"):
            prepare.load_selection_bytes(
                render_jsonl(nonfinite_score),
                cases=cases,
                source_records=sources,
            )

    def test_selection_sidecar_is_optional_and_never_changes_blind_outputs(self) -> None:
        cases_data = render_jsonl(sample_cases())
        without_selection = prepare.prepare_outputs_bytes(cases_data)
        with_selection = prepare.prepare_outputs_bytes(
            cases_data, render_jsonl(sample_selection())
        )
        for name in (
            prepare.SOURCE_NAME,
            prepare.ASSIGNMENT_NAME,
            prepare.CONTEXT_NAME,
            prepare.EMPTY_CONTEXT_NAME,
            prepare.QUEUE_SEED_NAME,
        ):
            self.assertEqual(without_selection[name], with_selection[name])
        self.assertNotIn(prepare.SELECTION_NAME, without_selection)
        self.assertIn(prepare.SELECTION_NAME, with_selection)
        manifest = json.loads(without_selection[prepare.MANIFEST_NAME])
        self.assertIsNone(manifest["bindings"]["selection_input"])
        self.assertIsNone(manifest["bindings"]["selection"])
        self.assertFalse(
            manifest["contracts"]["selection_runtime_metadata_complete"]
        )
        with_manifest = json.loads(with_selection[prepare.MANIFEST_NAME])
        self.assertTrue(
            with_manifest["contracts"]["selection_runtime_metadata_complete"]
        )

    def test_rejects_invalid_unicode_and_wire_format(self) -> None:
        invalid_values = (
            ("reading", "e\u0301", "NFC-normalized"),
            ("source_revision", "revision\u0001", "control"),
            ("left_context", "context\u0001", "control"),
            ("reading", "bad\ud800", "surrogate"),
            ("surface_references", ["bad\ufdd0"], "noncharacter"),
        )
        for field, value, message in invalid_values:
            with self.subTest(field=field, value=repr(value)):
                cases = sample_cases()[:1]
                cases[0][field] = value
                data = render_jsonl(cases)
                with self.assertRaisesRegex(ValueError, message):
                    prepare.load_cases_bytes(data)

        valid = render_jsonl(sample_cases()[:1])
        for data in (b"\xef\xbb\xbf" + valid, valid.replace(b"\n", b"\r\n"), valid[:-1]):
            with self.subTest(data=data[:8]):
                with self.assertRaisesRegex(ValueError, "BOM-free|end with one LF"):
                    prepare.load_cases_bytes(data)

    def test_rejects_invalid_enums_ids_and_reserved_boundary_marker(self) -> None:
        cases = sample_cases()[:1]
        cases[0]["dataset_role"] = "gold"
        with self.assertRaisesRegex(ValueError, "dataset_role"):
            prepare.load_cases_bytes(render_jsonl(cases))

        cases = sample_cases()[:1]
        cases[0]["fold"] = "test"
        with self.assertRaisesRegex(ValueError, "fold"):
            prepare.load_cases_bytes(render_jsonl(cases))

        cases = sample_cases()[:1]
        cases[0]["family_id"] = "family with spaces"
        with self.assertRaisesRegex(ValueError, "server-safe IDs"):
            prepare.load_cases_bytes(render_jsonl(cases))

        cases = sample_cases()[:1]
        cases[0]["reading"] = "きょう|は"
        with self.assertRaisesRegex(ValueError, "reserved boundary marker"):
            prepare.load_cases_bytes(render_jsonl(cases))

    def test_write_and_cli_are_no_replace(self) -> None:
        cases_data = render_jsonl(sample_cases())
        selection_data = render_jsonl(sample_selection())
        generated = prepare.prepare_outputs_bytes(cases_data, selection_data)
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            cases_path = root / "cases.jsonl"
            cases_path.write_bytes(cases_data)
            selection_path = root / "selection-input.jsonl"
            selection_path.write_bytes(selection_data)
            output_dir = root / "generation"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                result = prepare.main(
                    [
                        "--cases",
                        str(cases_path),
                        "--selection-metadata",
                        str(selection_path),
                        "--output-dir",
                        str(output_dir),
                    ]
                )
            self.assertEqual(result, 0, stderr.getvalue())
            self.assertEqual(
                {path.name for path in output_dir.iterdir()}, set(generated)
            )
            self.assertEqual(
                (output_dir / prepare.MANIFEST_NAME).read_bytes(),
                generated[prepare.MANIFEST_NAME],
            )

            stderr = io.StringIO()
            with redirect_stderr(stderr):
                repeated = prepare.main(
                    [
                        "--cases",
                        str(cases_path),
                        "--selection-metadata",
                        str(selection_path),
                        "--output-dir",
                        str(output_dir),
                    ]
                )
            self.assertEqual(repeated, 2)
            self.assertIn("output directory already exists", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
