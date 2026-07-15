from __future__ import annotations

import copy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from tools.dictionary import build_mozc_hybrid_segment_holdout_v1 as holdout


def render_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def render_jsonl(values: list[dict[str, object]]) -> bytes:
    return b"".join(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
        for value in values
    )


def sample_cases() -> list[dict[str, object]]:
    return [
        {
            "schema": holdout.CASE_SCHEMA,
            "id": "hs1-homophone-0001",
            "category": "homophone-context",
            "family_id": "family-hs1-homophone-0001",
            "elements": [
                {"text": "き", "input_style": "direct"},
                {"text": "ょ", "input_style": "direct"},
                {"text": "う", "input_style": "direct"},
                {"text": "は", "input_style": "direct"},
            ],
            "target": {
                "span": {
                    "start": 0,
                    "count": 3,
                    "unit": holdout.COMPOSITION_ELEMENT_UNIT,
                },
                "surfaces": ["今日"],
            },
        },
        {
            "schema": holdout.CASE_SCHEMA,
            "id": "hs1-width-0001",
            "category": "width-orthography",
            "family_id": "family-hs1-width-0001",
            "elements": [
                {"text": "よ", "input_style": "direct"},
                {"text": "ん", "input_style": "direct"},
                {"text": "が", "input_style": "direct"},
                {"text": "つ", "input_style": "direct"},
            ],
            "target": {
                "span": {
                    "start": 0,
                    "count": 4,
                    "unit": holdout.COMPOSITION_ELEMENT_UNIT,
                },
                "surfaces": ["4月"],
            },
        },
    ]


def sample_approval(cases_data: bytes) -> dict[str, object]:
    return {
        "schema": holdout.APPROVAL_SCHEMA,
        "status": "approved",
        "holdout_id": "mozc-hybrid-segment-holdout-v1-test",
        "source_cases_sha256": holdout.sha256_bytes(cases_data),
        "author_id": "test-holdout-author",
        "reviewer_id": "independent-holdout-reviewer",
        "quality_categories": {
            "homophone-context": 1,
            "width-orthography": 1,
        },
        "minimum_h2_promotion_opportunities": 1,
        "attestation": dict(holdout.ATTESTATION_CONTRACT),
        "policy_freeze": {
            "h0_policy_id": holdout.H0_POLICY_ID,
            "h1_policy_id": holdout.H1_POLICY_ID,
            "h2_policy_id": holdout.H2_POLICY_ID,
            "product_source_revision": "a" * 40,
            "evaluator_sha256": "sha256:" + "b" * 64,
            "hybrid_evaluator_sha256": "sha256:" + "f" * 64,
            "abprobe_executable_sha256": "sha256:" + "c" * 64,
            "hazkey_resource_fingerprint": "sha256:" + "d" * 64,
            "mozc_resource_fingerprint": "sha256:" + "9" * 64,
            "mozc_bundle_generation": "sha256-" + "e" * 64,
            "top_k": 10,
            "warmups": 0,
            "iterations": 1,
            "learning_enabled": False,
        },
    }


class SegmentHoldoutFixture:
    def __init__(
        self,
        root: Path,
        *,
        cases: list[dict[str, object]] | None = None,
        approval: dict[str, object] | None = None,
    ) -> None:
        self.root = root
        self.cases = copy.deepcopy(cases if cases is not None else sample_cases())
        self.cases_data = render_jsonl(self.cases)
        self.approval = copy.deepcopy(
            approval if approval is not None else sample_approval(self.cases_data)
        )
        self.cases_path = root / "reviewed-cases.jsonl"
        self.approval_path = root / "review-approval.json"
        self.cases_path.write_bytes(self.cases_data)
        self.approval_path.write_bytes(render_json(self.approval))


class MozcHybridSegmentHoldoutV1Tests(unittest.TestCase):
    def test_prepares_deterministic_label_free_and_label_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture = SegmentHoldoutFixture(Path(temporary_directory))
            generated = holdout.prepare_outputs(
                cases_path=fixture.cases_path,
                approval_path=fixture.approval_path,
            )
            repeated = holdout.prepare_outputs(
                cases_path=fixture.cases_path,
                approval_path=fixture.approval_path,
            )

        self.assertEqual(generated, repeated)
        self.assertEqual(
            set(generated),
            {
                holdout.SOURCE_CASES_NAME,
                holdout.APPROVAL_NAME,
                holdout.PROBE_INPUT_NAME,
                holdout.SEGMENT_LABELS_NAME,
                holdout.MANIFEST_NAME,
            },
        )
        probe_records = [
            json.loads(line)
            for line in generated[holdout.PROBE_INPUT_NAME].splitlines()
        ]
        label_records = [
            json.loads(line)
            for line in generated[holdout.SEGMENT_LABELS_NAME].splitlines()
        ]
        self.assertEqual(len(probe_records), 2)
        self.assertEqual(len(label_records), 2)
        self.assertEqual(
            set(probe_records[0]), {"schema", "id", "category", "elements"}
        )
        self.assertNotIn("target", probe_records[0])
        self.assertNotIn("family_id", probe_records[0])
        self.assertEqual(
            set(label_records[0]), {"schema", "id", "family_id", "target"}
        )
        self.assertNotIn("category", label_records[0])
        self.assertNotIn("elements", label_records[0])

        manifest = json.loads(generated[holdout.MANIFEST_NAME])
        self.assertEqual(manifest["schema"], holdout.MANIFEST_SCHEMA)
        self.assertFalse(manifest["formal_authorized"])
        self.assertTrue(manifest["human_collection_attested"])
        self.assertEqual(
            manifest["category_counts"],
            {"homophone-context": 1, "width-orthography": 1},
        )
        self.assertEqual(
            manifest["evaluation_contract"]["quality_categories"],
            ["homophone-context", "width-orthography"],
        )
        self.assertEqual(
            manifest["evaluation_contract"]["target_match"],
            "raw-exact-NFC-label-surface-and-composition-element-count.v1",
        )
        self.assertEqual(
            manifest["bindings"]["probe_input"]["sha256"],
            holdout.sha256_bytes(generated[holdout.PROBE_INPUT_NAME]),
        )
        self.assertEqual(
            manifest["bindings"]["segment_labels"]["sha256"],
            holdout.sha256_bytes(generated[holdout.SEGMENT_LABELS_NAME]),
        )
        self.assertEqual(
            manifest["bindings"]["review_approval"]["sha256"],
            holdout.sha256_bytes(generated[holdout.APPROVAL_NAME]),
        )
        self.assertEqual(
            manifest["policy_freeze"]["value"]["h2_policy_id"],
            holdout.H2_POLICY_ID,
        )
        self.assertEqual(
            manifest["policy_freeze"]["value"]["hybrid_evaluator_sha256"],
            "sha256:" + "f" * 64,
        )
        self.assertEqual(
            manifest["outstanding_requirements"],
            {
                "existing_v2_and_auxiliary_duplicate_screen": "not_implemented",
                "backend_label_isolation": "not_implemented",
                "evaluator_loaded_code_identity": "not_attested",
                "formal_authorization_blocked": True,
            },
        )
        self.assertEqual(
            holdout.sealed_directory_name(generated),
            holdout.sealed_directory_name(repeated),
        )

    def test_rejects_unknown_case_or_approval_fields(self) -> None:
        cases = sample_cases()
        cases[0]["unexpected"] = True
        with self.assertRaisesRegex(ValueError, "unknown=.*unexpected"):
            holdout.load_cases_bytes(render_jsonl(cases))

        cases_data = render_jsonl(sample_cases())
        approval = sample_approval(cases_data)
        approval["unexpected"] = True
        with self.assertRaisesRegex(ValueError, "unknown=.*unexpected"):
            holdout.load_approval_bytes(render_json(approval))

        nested = sample_cases()
        nested[0]["target"]["span"]["unexpected"] = 1
        with self.assertRaisesRegex(ValueError, "unknown=.*unexpected"):
            holdout.load_cases_bytes(render_jsonl(nested))

    def test_rejects_duplicate_ids_families_surfaces_and_json_keys(self) -> None:
        for label, mutate, message in (
            (
                "id",
                lambda values: values[1].__setitem__("id", values[0]["id"]),
                "duplicate case id",
            ),
            (
                "family",
                lambda values: values[1].__setitem__(
                    "family_id", values[0]["family_id"]
                ),
                "duplicate family_id",
            ),
            (
                "surface",
                lambda values: values[0]["target"].__setitem__(
                    "surfaces", ["今日", "今日"]
                ),
                "surfaces must be unique",
            ),
        ):
            with self.subTest(label=label):
                cases = sample_cases()
                mutate(cases)
                with self.assertRaisesRegex(ValueError, message):
                    holdout.load_cases_bytes(render_jsonl(cases))

        duplicate_key = (
            b'{"schema":"hazkey.mozc-hybrid-segment-case.v1",'
            b'"id":"one","id":"two","category":"c","family_id":"f",'
            b'"elements":[{"text":"a","input_style":"direct"}],'
            b'"target":{"span":{"start":0,"count":1,'
            b'"unit":"composition_element"},"surfaces":["A"]}}\n'
        )
        with self.assertRaisesRegex(ValueError, "duplicate JSON key 'id'"):
            holdout.load_cases_bytes(duplicate_key)

    def test_rejects_non_nfc_empty_and_control_text(self) -> None:
        mutations = (
            (
                "element NFC",
                lambda case: case["elements"][0].__setitem__("text", "は\u3099"),
                "NFC",
            ),
            (
                "surface NFC",
                lambda case: case["target"].__setitem__("surfaces", ["A\u030a"]),
                "NFC",
            ),
            (
                "empty element",
                lambda case: case["elements"][0].__setitem__("text", ""),
                "non-empty",
            ),
            (
                "control",
                lambda case: case.__setitem__("id", "bad\tid"),
                "control",
            ),
            (
                "embedded BOM",
                lambda case: case.__setitem__("id", "bad\ufeffid"),
                "control",
            ),
        )
        for label, mutate, message in mutations:
            with self.subTest(label=label):
                cases = sample_cases()
                mutate(cases[0])
                with self.assertRaisesRegex(ValueError, message):
                    holdout.load_cases_bytes(render_jsonl(cases))

    def test_rejects_invalid_input_style_and_span(self) -> None:
        mutations = (
            (
                "mapped",
                lambda case: case["elements"][0].__setitem__(
                    "input_style", "mapped"
                ),
                "must be direct",
            ),
            (
                "start",
                lambda case: case["target"]["span"].__setitem__("start", 1),
                "start must be 0",
            ),
            (
                "start bool",
                lambda case: case["target"]["span"].__setitem__("start", False),
                "non-negative integer",
            ),
            (
                "count bool",
                lambda case: case["target"]["span"].__setitem__("count", True),
                "positive integer",
            ),
            (
                "count zero",
                lambda case: case["target"]["span"].__setitem__("count", 0),
                "positive integer",
            ),
            (
                "count large",
                lambda case: case["target"]["span"].__setitem__("count", 99),
                "must not exceed",
            ),
            (
                "unit",
                lambda case: case["target"]["span"].__setitem__(
                    "unit", "codepoint"
                ),
                "composition_element",
            ),
            (
                "surfaces",
                lambda case: case["target"].__setitem__("surfaces", []),
                "must not be empty",
            ),
        )
        for label, mutate, message in mutations:
            with self.subTest(label=label):
                cases = sample_cases()
                mutate(cases[0])
                with self.assertRaisesRegex(ValueError, message):
                    holdout.load_cases_bytes(render_jsonl(cases))

    def test_rejects_hash_category_identity_and_minimum_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fixture = SegmentHoldoutFixture(root)

            approval = copy.deepcopy(fixture.approval)
            approval["source_cases_sha256"] = "sha256:" + "0" * 64
            fixture.approval_path.write_bytes(render_json(approval))
            with self.assertRaisesRegex(ValueError, "does not match exact cases bytes"):
                holdout.prepare_outputs(
                    cases_path=fixture.cases_path,
                    approval_path=fixture.approval_path,
                )

            approval = copy.deepcopy(fixture.approval)
            approval["quality_categories"] = {"homophone-context": 2}
            fixture.approval_path.write_bytes(render_json(approval))
            with self.assertRaisesRegex(ValueError, "category counts"):
                holdout.prepare_outputs(
                    cases_path=fixture.cases_path,
                    approval_path=fixture.approval_path,
                )

            approval = copy.deepcopy(fixture.approval)
            approval["minimum_h2_promotion_opportunities"] = 3
            fixture.approval_path.write_bytes(render_json(approval))
            with self.assertRaisesRegex(ValueError, "quality case count"):
                holdout.prepare_outputs(
                    cases_path=fixture.cases_path,
                    approval_path=fixture.approval_path,
                )

        cases_data = render_jsonl(sample_cases())
        approval = sample_approval(cases_data)
        approval["reviewer_id"] = approval["author_id"].upper()
        with self.assertRaisesRegex(ValueError, "must be independent"):
            holdout.load_approval_bytes(render_json(approval))

    def test_rejects_boolean_numeric_contracts(self) -> None:
        cases_data = render_jsonl(sample_cases())
        for field, value, message in (
            ("minimum_h2_promotion_opportunities", True, "positive integer"),
            ("quality_categories", {"homophone-context": True}, "positive integer"),
        ):
            with self.subTest(field=field):
                approval = sample_approval(cases_data)
                approval[field] = value
                with self.assertRaisesRegex(ValueError, message):
                    holdout.load_approval_bytes(render_json(approval))

        for field, value, message in (
            ("top_k", True, "positive integer"),
            ("top_k", 11, "must not exceed 10"),
            ("warmups", True, "non-negative integer"),
            ("iterations", False, "positive integer"),
        ):
            with self.subTest(field=field):
                approval = sample_approval(cases_data)
                approval["policy_freeze"][field] = value
                with self.assertRaisesRegex(ValueError, message):
                    holdout.load_approval_bytes(render_json(approval))

    def test_rejects_failed_attestation(self) -> None:
        cases_data = render_jsonl(sample_cases())
        for field, expected in holdout.ATTESTATION_CONTRACT.items():
            with self.subTest(field=field):
                approval = sample_approval(cases_data)
                approval["attestation"][field] = not expected
                with self.assertRaisesRegex(ValueError, "blind holdout contract"):
                    holdout.load_approval_bytes(render_json(approval))

                approval = sample_approval(cases_data)
                approval["attestation"][field] = int(expected)
                with self.assertRaisesRegex(ValueError, "blind holdout contract"):
                    holdout.load_approval_bytes(render_json(approval))

    def test_non_quality_category_is_bound_but_excluded_from_formal_allowlist(self) -> None:
        cases = sample_cases()
        protected = copy.deepcopy(cases[0])
        protected["id"] = "hs1-protected-0001"
        protected["category"] = "protected"
        protected["family_id"] = "family-hs1-protected-0001"
        cases.append(protected)
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            cases_data = render_jsonl(cases)
            fixture = SegmentHoldoutFixture(
                root,
                cases=cases,
                approval=sample_approval(cases_data),
            )
            generated = holdout.prepare_outputs(
                cases_path=fixture.cases_path,
                approval_path=fixture.approval_path,
            )
        manifest = json.loads(generated[holdout.MANIFEST_NAME])
        self.assertEqual(manifest["category_counts"]["protected"], 1)
        self.assertNotIn(
            "protected", manifest["evaluation_contract"]["quality_categories"]
        )

    def test_rejects_h2_minimum_that_only_non_quality_cases_could_satisfy(self) -> None:
        cases = sample_cases()
        protected = copy.deepcopy(cases[0])
        protected["id"] = "hs1-protected-0001"
        protected["category"] = "protected"
        protected["family_id"] = "family-hs1-protected-0001"
        cases.append(protected)
        cases_data = render_jsonl(cases)
        approval = sample_approval(cases_data)
        approval["minimum_h2_promotion_opportunities"] = 3
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture = SegmentHoldoutFixture(
                Path(temporary_directory),
                cases=cases,
                approval=approval,
            )
            with self.assertRaisesRegex(ValueError, "quality case count"):
                holdout.prepare_outputs(
                    cases_path=fixture.cases_path,
                    approval_path=fixture.approval_path,
                )

    def test_rejects_invalid_policy_freeze_identity(self) -> None:
        cases_data = render_jsonl(sample_cases())
        mutations = (
            ("h2", "h2_policy_id", "other", "h2_policy_id"),
            ("revision", "product_source_revision", "A" * 40, "40 lowercase hex"),
            ("evaluator", "evaluator_sha256", "sha256:" + "G" * 64, "sha256"),
            (
                "hybrid evaluator",
                "hybrid_evaluator_sha256",
                "sha256:bad",
                "sha256",
            ),
            ("executable", "abprobe_executable_sha256", "sha256:bad", "sha256"),
            ("resource", "hazkey_resource_fingerprint", "sha256-" + "d" * 64, "sha256"),
            (
                "mozc resource",
                "mozc_resource_fingerprint",
                "sha256-" + "9" * 64,
                "sha256",
            ),
            (
                "zero mozc resource",
                "mozc_resource_fingerprint",
                "sha256:" + "0" * 64,
                "all-zero",
            ),
            ("mozc", "mozc_bundle_generation", "sha256:" + "e" * 64, "sha256-"),
            ("learning", "learning_enabled", True, "must be false"),
        )
        for label, field, value, message in mutations:
            with self.subTest(label=label):
                approval = sample_approval(cases_data)
                approval["policy_freeze"][field] = value
                with self.assertRaisesRegex(ValueError, message):
                    holdout.load_approval_bytes(render_json(approval))

    def test_seal_is_read_only_content_addressed_and_no_replace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fixture = SegmentHoldoutFixture(root)
            generated, generation = holdout.seal(
                cases_path=fixture.cases_path,
                approval_path=fixture.approval_path,
                output_root=root,
            )
            self.assertEqual(generation.name, holdout.sealed_directory_name(generated))
            self.assertEqual(stat.S_IMODE(generation.stat().st_mode), 0o555)
            self.assertEqual({path.name for path in generation.iterdir()}, set(generated))
            for path in generation.iterdir():
                metadata = path.stat(follow_symlinks=False)
                self.assertTrue(stat.S_ISREG(metadata.st_mode))
                self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o444)
                self.assertEqual(metadata.st_nlink, 1)
                self.assertEqual(path.read_bytes(), generated[path.name])

            with self.assertRaises(OSError):
                holdout.seal(
                    cases_path=fixture.cases_path,
                    approval_path=fixture.approval_path,
                    output_root=root,
                )
            self.assertTrue(generation.is_dir())
            self.assertFalse(
                any(
                    entry.name.startswith(holdout.STAGING_DIRECTORY_PREFIX)
                    for entry in root.iterdir()
                )
            )

    def test_post_rename_verification_failure_removes_final_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fixture = SegmentHoldoutFixture(root)
            generated = holdout.prepare_outputs(
                cases_path=fixture.cases_path,
                approval_path=fixture.approval_path,
            )
            generation = root / holdout.sealed_directory_name(generated)
            with mock.patch.object(
                holdout,
                "_verify_generation_at",
                side_effect=ValueError("synthetic verification failure"),
            ):
                with self.assertRaisesRegex(ValueError, "synthetic verification"):
                    holdout.seal(
                        cases_path=fixture.cases_path,
                        approval_path=fixture.approval_path,
                        output_root=root,
                    )
            self.assertFalse(generation.exists())
            self.assertFalse(
                any(
                    entry.name.startswith(
                        (
                            holdout.STAGING_DIRECTORY_PREFIX,
                            holdout.REJECTED_DIRECTORY_PREFIX,
                        )
                    )
                    for entry in root.iterdir()
                )
            )

    def test_post_rename_cleanup_removes_unexpected_nested_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fixture = SegmentHoldoutFixture(root)
            generated = holdout.prepare_outputs(
                cases_path=fixture.cases_path,
                approval_path=fixture.approval_path,
            )
            generation = root / holdout.sealed_directory_name(generated)

            def contaminate_and_fail(*_: object) -> None:
                generation.chmod(0o755)
                unexpected = generation / "unexpected"
                unexpected.mkdir()
                (unexpected / "entry").write_text("untrusted", encoding="utf-8")
                raise ValueError("synthetic contaminated generation")

            with mock.patch.object(
                holdout,
                "_verify_generation_at",
                side_effect=contaminate_and_fail,
            ):
                with self.assertRaisesRegex(ValueError, "synthetic contaminated"):
                    holdout.seal(
                        cases_path=fixture.cases_path,
                        approval_path=fixture.approval_path,
                        output_root=root,
                    )
            self.assertFalse(generation.exists())
            self.assertFalse(
                any(
                    entry.name.startswith(
                        (
                            holdout.STAGING_DIRECTORY_PREFIX,
                            holdout.REJECTED_DIRECTORY_PREFIX,
                        )
                    )
                    for entry in root.iterdir()
                )
            )

    def test_post_rename_cleanup_preserves_replaced_directory_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fixture = SegmentHoldoutFixture(root)
            generated = holdout.prepare_outputs(
                cases_path=fixture.cases_path,
                approval_path=fixture.approval_path,
            )
            generation = root / holdout.sealed_directory_name(generated)
            displaced = root / "displaced-original-generation"

            def replace_and_fail(*_: object) -> None:
                generation.rename(displaced)
                generation.mkdir()
                (generation / "must-survive").write_text(
                    "unknown tree", encoding="utf-8"
                )
                raise ValueError("synthetic replaced generation")

            with mock.patch.object(
                holdout,
                "_verify_generation_at",
                side_effect=replace_and_fail,
            ):
                with self.assertRaisesRegex(ValueError, "synthetic replaced"):
                    holdout.seal(
                        cases_path=fixture.cases_path,
                        approval_path=fixture.approval_path,
                        output_root=root,
                    )

            self.assertFalse(generation.exists())
            rejected = [
                entry
                for entry in root.iterdir()
                if entry.name.startswith(holdout.REJECTED_DIRECTORY_PREFIX)
            ]
            self.assertEqual(len(rejected), 1)
            self.assertEqual(
                (rejected[0] / "must-survive").read_text(encoding="utf-8"),
                "unknown tree",
            )
            self.assertTrue(displaced.is_dir())

    def test_cli_publishes_and_refuses_second_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fixture = SegmentHoldoutFixture(root)
            arguments = [
                "--cases",
                os.fspath(fixture.cases_path),
                "--approval",
                os.fspath(fixture.approval_path),
                "--output-root",
                os.fspath(root),
            ]
            standard_output = io.StringIO()
            standard_error = io.StringIO()
            with redirect_stdout(standard_output), redirect_stderr(standard_error):
                self.assertEqual(holdout.main(arguments), 0)
                self.assertEqual(holdout.main(arguments), 2)
            self.assertIn(holdout.SEALED_DIRECTORY_PREFIX, standard_output.getvalue())
            self.assertIn("File exists", standard_error.getvalue())


if __name__ == "__main__":
    unittest.main()
