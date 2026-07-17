from __future__ import annotations

import copy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from tools.dictionary import compile_mozc_acceptable_path_evaluation as compiler


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


def path(
    path_id: str,
    *,
    reading_boundaries: list[int],
    surface_boundaries: list[int] | None,
    surface_reference_id: str = "surface-0",
    kind: str = "human",
) -> dict[str, object]:
    return {
        "path_id": path_id,
        "status": "acceptable",
        "surface_reference_id": surface_reference_id,
        "reading_boundaries": reading_boundaries,
        "surface_boundaries": surface_boundaries,
        "alignment_status": (
            "aligned" if surface_boundaries is not None else "reading_only"
        ),
        "provenance": {"kind": kind},
    }


def record(
    case_id: str,
    *,
    reading: str = "きょうはあめ",
    annotation_reading: str | None = None,
    surfaces: list[str] | None = None,
    paths: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    effective_reading = annotation_reading or reading
    expected_surfaces = surfaces or ["今日は雨"]
    acceptable_paths = paths or [
        path(
            "human-1",
            reading_boundaries=[3, 4],
            surface_boundaries=[2, 3],
        )
    ]
    return {
        "schema": compiler.ANNOTATION_EXPORT_SCHEMA,
        "id": case_id,
        "category": "sentence",
        "source": {
            "queue_sha256": "sha256:" + "1" * 64,
            "corpus_sha256": "sha256:" + "2" * 64,
            "row_sha256": "sha256:" + "3" * 64,
            "reading": reading,
            "annotation_reading": effective_reading,
            "reading_unit": compiler.SOURCE_READING_UNIT,
            "annotation_reading_unit": compiler.ANNOTATION_READING_UNIT,
            "surface_unit": compiler.SURFACE_UNIT,
            "surface_references": [
                {"id": f"surface-{index}", "text": surface}
                for index, surface in enumerate(expected_surfaces)
            ],
        },
        "path_set_status": "closed",
        "needs_adjudication": False,
        "path_units": {
            "reading_boundaries": compiler.ANNOTATION_READING_UNIT,
            "surface_boundaries": compiler.SURFACE_UNIT,
        },
        "acceptable_paths": acceptable_paths,
        "draft_paths": [],
        "review": {
            "revision": 7,
            "corrected_reading": (
                effective_reading if effective_reading != reading else None
            ),
            "annotator_id": "human-reviewer",
            "reviewed_once": True,
            "updated_at": "2026-07-17T00:00:00Z",
            "notes": None,
            "imported": {},
        },
    }


def annotation_manifest(records_data: bytes, cases: int) -> dict[str, object]:
    return {
        "schema": compiler.ANNOTATION_MANIFEST_SCHEMA,
        "queue_sha256": "sha256:" + "1" * 64,
        "workbook_sha256": "sha256:" + "4" * 64,
        "reviewed_paths_sha256": compiler.sha256_bytes(records_data),
        "cases": cases,
        "path_set_statuses": {"closed": cases},
        "complete": True,
        "formal_authorized": False,
        "diagnostic_only": True,
    }


class Fixture:
    def __init__(self, root: Path, records: list[dict[str, object]]) -> None:
        self.root = root
        self.records = copy.deepcopy(records)
        self.reviewed_path = root / "reviewed-paths.jsonl"
        self.manifest_path = root / "annotation-manifest.json"
        self.manifest: dict[str, object] = {}
        self.write()

    def write(self) -> None:
        records_data = render_jsonl(self.records)
        self.manifest = annotation_manifest(records_data, len(self.records))
        self.reviewed_path.write_bytes(records_data)
        self.manifest_path.write_bytes(render_json(self.manifest))

    def write_manifest(self) -> None:
        self.manifest_path.write_bytes(render_json(self.manifest))

    def prepare(self) -> dict[str, bytes]:
        return compiler.prepare_outputs(
            reviewed_paths_path=self.reviewed_path,
            annotation_manifest_path=self.manifest_path,
        )


def parse_jsonl(data: bytes) -> list[dict[str, object]]:
    return [json.loads(line) for line in data.decode("utf-8").splitlines()]


class CompileMozcAcceptablePathEvaluationTests(unittest.TestCase):
    def test_preserves_multiple_acceptable_first_boundaries_and_pairs(self) -> None:
        first_paths = [
            path(
                "aligned-short",
                reading_boundaries=[3, 4],
                surface_boundaries=[2, 3],
            ),
            path(
                "aligned-long",
                reading_boundaries=[4],
                surface_boundaries=[3],
            ),
            path(
                "aligned-short-duplicate-target",
                reading_boundaries=[3],
                surface_boundaries=[2],
                surface_reference_id="surface-1",
            ),
        ]
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture = Fixture(
                Path(temporary_directory),
                [
                    record(
                        "case-1",
                        surfaces=["今日は雨", "今日なら"],
                        paths=first_paths,
                    )
                ],
            )
            generated = fixture.prepare()

        probe = parse_jsonl(generated[compiler.PROBE_INPUT_NAME])[0]
        target = parse_jsonl(generated[compiler.TARGETS_NAME])[0]
        manifest = json.loads(generated[compiler.MANIFEST_NAME])
        self.assertEqual(
            "".join(element["text"] for element in probe["elements"]),
            "きょうはあめ",
        )
        self.assertEqual(
            [span["count"] for span in target["acceptable_first_spans"]],
            [3, 4],
        )
        self.assertEqual(
            target["acceptable_first_chunks"],
            [
                {
                    "span": {"count": 3, "start": 0, "unit": "composition_element"},
                    "surface": "今日",
                },
                {
                    "span": {"count": 4, "start": 0, "unit": "composition_element"},
                    "surface": "今日は",
                },
            ],
        )
        self.assertEqual(target["surface_evaluation_status"], "fully_aligned")
        self.assertEqual(target["path_counts"]["acceptable"], 3)
        self.assertEqual(manifest["total_acceptable_paths"], 3)
        self.assertEqual(manifest["total_acceptable_first_spans"], 2)
        self.assertEqual(manifest["total_acceptable_first_chunks"], 2)

    def test_corrected_reading_is_reacquired_as_explicit_elements(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture = Fixture(
                Path(temporary_directory),
                [
                    record(
                        "corrected",
                        reading="こんにちわ",
                        annotation_reading="こんにちは",
                        surfaces=["こんにちは"],
                        paths=[
                            path(
                                "corrected-full",
                                reading_boundaries=[],
                                surface_boundaries=[],
                            )
                        ],
                    )
                ],
            )
            generated = fixture.prepare()

        probe = parse_jsonl(generated[compiler.PROBE_INPUT_NAME])[0]
        target = parse_jsonl(generated[compiler.TARGETS_NAME])[0]
        manifest = json.loads(generated[compiler.MANIFEST_NAME])
        self.assertEqual(
            [element["text"] for element in probe["elements"]],
            list("こんにちは"),
        )
        self.assertNotIn("reading", probe)
        self.assertEqual(target["reading"], "こんにちは")
        self.assertEqual(target["acceptable_first_spans"][0]["count"], 5)
        self.assertEqual(manifest["corrected_reading_cases"], 1)

    def test_bytes_derivation_is_identical_to_single_read_path_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture = Fixture(Path(temporary_directory), [record("case-1")])
            from_paths = fixture.prepare()
            from_bytes = compiler.prepare_outputs_bytes(
                reviewed_paths_data=fixture.reviewed_path.read_bytes(),
                annotation_manifest_data=fixture.manifest_path.read_bytes(),
            )

        self.assertEqual(from_bytes, from_paths)
        self.assertEqual(
            set(from_bytes),
            {
                compiler.SOURCE_REVIEWED_PATHS_NAME,
                compiler.SOURCE_ANNOTATION_MANIFEST_NAME,
                compiler.PROBE_INPUT_NAME,
                compiler.TARGETS_NAME,
                compiler.MANIFEST_NAME,
            },
        )

    def test_accepts_bound_gold_audit_and_rejects_audit_drift(self) -> None:
        audit = {
            "routing_batch_id": "00000000-0000-4000-8000-000000000001",
            "annotation_tier": "gold",
            "llm_unmodified": False,
            "human_reviewed": True,
        }
        candidate = record("audited")
        candidate["review"].update(audit)  # type: ignore[union-attr]
        candidate["acceptable_paths"][0]["provenance"].update(audit)  # type: ignore[index,union-attr]
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture = Fixture(Path(temporary_directory), [candidate])
            generated = fixture.prepare()
        self.assertIn(compiler.PROBE_INPUT_NAME, generated)

        drifted = copy.deepcopy(candidate)
        drifted["acceptable_paths"][0]["provenance"][  # type: ignore[index]
            "human_reviewed"
        ] = False
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture = Fixture(Path(temporary_directory), [drifted])
            with self.assertRaisesRegex(ValueError, "human_reviewed must be true"):
                fixture.prepare()

        silver = copy.deepcopy(candidate)
        silver["review"]["annotation_tier"] = "silver"  # type: ignore[index]
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture = Fixture(Path(temporary_directory), [silver])
            with self.assertRaisesRegex(ValueError, "must be gold"):
                fixture.prepare()

    def test_partial_alignment_is_not_marked_fully_evaluable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture = Fixture(
                Path(temporary_directory),
                [
                    record(
                        "partial",
                        paths=[
                            path(
                                "aligned",
                                reading_boundaries=[3],
                                surface_boundaries=[2],
                            ),
                            path(
                                "reading-only",
                                reading_boundaries=[4],
                                surface_boundaries=None,
                            ),
                        ],
                    )
                ],
            )
            target = parse_jsonl(fixture.prepare()[compiler.TARGETS_NAME])[0]

        self.assertEqual(target["surface_evaluation_status"], "partially_aligned")
        self.assertEqual(
            target["path_counts"],
            {"acceptable": 2, "aligned": 1, "reading_only": 1},
        )
        self.assertEqual(len(target["acceptable_first_chunks"]), 1)

    def test_reading_only_case_has_no_surface_targets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture = Fixture(
                Path(temporary_directory),
                [
                    record(
                        "reading-only",
                        paths=[
                            path(
                                "reading-only",
                                reading_boundaries=[3],
                                surface_boundaries=None,
                            )
                        ],
                    )
                ],
            )
            target = parse_jsonl(fixture.prepare()[compiler.TARGETS_NAME])[0]

        self.assertEqual(target["surface_evaluation_status"], "not_aligned")
        self.assertEqual(target["acceptable_first_chunks"], [])

    def test_rejects_incomplete_annotation_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture = Fixture(Path(temporary_directory), [record("case-1")])
            fixture.manifest["complete"] = False
            fixture.write_manifest()
            with self.assertRaisesRegex(ValueError, "complete must be true"):
                fixture.prepare()

    def test_rejects_reviewed_paths_sha_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture = Fixture(Path(temporary_directory), [record("case-1")])
            fixture.manifest["reviewed_paths_sha256"] = "sha256:" + "9" * 64
            fixture.write_manifest()
            with self.assertRaisesRegex(ValueError, "does not match exact"):
                fixture.prepare()

    def test_rejects_duplicate_case_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture = Fixture(
                Path(temporary_directory), [record("same"), record("same")]
            )
            with self.assertRaisesRegex(ValueError, "duplicate case ids"):
                fixture.prepare()

    def test_rejects_nonclosed_or_adjudication_case(self) -> None:
        for field, value, message in (
            ("path_set_status", "open", "must be closed"),
            ("needs_adjudication", True, "must be false"),
        ):
            with (
                self.subTest(field=field),
                tempfile.TemporaryDirectory() as temporary_directory,
            ):
                candidate = record("case-1")
                candidate[field] = value
                fixture = Fixture(Path(temporary_directory), [candidate])
                with self.assertRaisesRegex(ValueError, message):
                    fixture.prepare()

    def test_rejects_wrong_units_and_out_of_range_boundaries(self) -> None:
        candidates: list[tuple[dict[str, object], str]] = []
        wrong_unit = record("wrong-unit")
        wrong_unit["path_units"]["reading_boundaries"] = "composition_element"
        candidates.append((wrong_unit, "reading_boundaries is invalid"))
        bad_boundary = record("bad-boundary")
        bad_boundary["acceptable_paths"][0]["reading_boundaries"] = [99]
        candidates.append((bad_boundary, "internal boundaries only"))
        for candidate, message in candidates:
            with (
                self.subTest(message=message),
                tempfile.TemporaryDirectory() as temporary_directory,
            ):
                fixture = Fixture(Path(temporary_directory), [candidate])
                with self.assertRaisesRegex(ValueError, message):
                    fixture.prepare()

    def test_rejects_u_feff_in_annotation_text(self) -> None:
        candidate = record("bom-inside-reading", reading="き\ufeffょうはあめ")
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture = Fixture(Path(temporary_directory), [candidate])
            with self.assertRaisesRegex(ValueError, "U\\+FEFF"):
                fixture.prepare()

    def test_validates_but_does_not_compile_draft_paths(self) -> None:
        draft_record = record("draft")
        draft = copy.deepcopy(draft_record["acceptable_paths"][0])
        draft["path_id"] = "draft-1"
        draft["status"] = "draft"
        draft_record["draft_paths"] = [draft]
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture = Fixture(Path(temporary_directory), [draft_record])
            target = parse_jsonl(fixture.prepare()[compiler.TARGETS_NAME])[0]
        self.assertEqual(target["path_counts"]["acceptable"], 1)

        malformed = copy.deepcopy(draft_record)
        malformed["draft_paths"][0]["status"] = "acceptable"
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture = Fixture(Path(temporary_directory), [malformed])
            with self.assertRaisesRegex(ValueError, "status must be draft"):
                fixture.prepare()

    def test_rejects_unknown_export_fields(self) -> None:
        unknown_record = record("unknown")
        unknown_record["unexpected"] = True
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture = Fixture(Path(temporary_directory), [unknown_record])
            with self.assertRaisesRegex(ValueError, "unknown=\\['unexpected'\\]"):
                fixture.prepare()

    def test_cli_writes_hash_bound_generation_and_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fixture = Fixture(root, [record("case-1")])
            output_dir = root / "generation"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                status = compiler.main(
                    [
                        "--reviewed-paths",
                        str(fixture.reviewed_path),
                        "--annotation-manifest",
                        str(fixture.manifest_path),
                        "--output-dir",
                        str(output_dir),
                    ]
                )
            self.assertEqual(status, 0)
            generated_manifest = json.loads(
                (output_dir / compiler.MANIFEST_NAME).read_text(encoding="utf-8")
            )
            self.assertEqual(
                (output_dir / compiler.SOURCE_REVIEWED_PATHS_NAME).read_bytes(),
                fixture.reviewed_path.read_bytes(),
            )
            self.assertEqual(
                (output_dir / compiler.SOURCE_ANNOTATION_MANIFEST_NAME).read_bytes(),
                fixture.manifest_path.read_bytes(),
            )
            self.assertEqual(
                generated_manifest["bindings"]["probe_input"]["sha256"],
                compiler.sha256_bytes(
                    (output_dir / compiler.PROBE_INPUT_NAME).read_bytes()
                ),
            )
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                second_status = compiler.main(
                    [
                        "--reviewed-paths",
                        str(fixture.reviewed_path),
                        "--annotation-manifest",
                        str(fixture.manifest_path),
                        "--output-dir",
                        str(output_dir),
                    ]
                )
            self.assertEqual(second_status, 2)
            self.assertIn("already exists", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
