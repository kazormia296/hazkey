from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import textwrap
import threading
import time
import unittest
from unittest import mock
from xml.sax.saxutils import escape
import xml.etree.ElementTree as ET
import zipfile

from tools.dictionary import serve_mozc_boundary_annotations as serve


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPOSITORY_ROOT / "tools/dictionary/serve_mozc_boundary_annotations.py"

ANNOTATION_HEADERS = [
    "No.",
    "Batch",
    "ID",
    "カテゴリ",
    "読み",
    "期待表層",
    "Linderaトークン監査",
    "プリアノテーション",
    "信頼度",
    "レビュー状態",
    "レビュー済み分割",
    "注記",
    "検証",
    "境界位置",
    "編集距離",
    "一致率",
    "代替境界差異",
    "曖昧理由",
    "ソース行SHA256",
    "入力JSONL行",
]


def queue_record(
    case_id: str,
    *,
    reading: str = "きょうはあめ",
    expected_surfaces: list[str] | None = None,
    category: str = "fixture",
    marked_reading: str = "きょうは|あめ",
) -> dict[str, object]:
    surfaces = expected_surfaces or ["今日は雨"]
    return {
        "schema": serve.QUEUE_SCHEMA,
        "id": case_id,
        "category": category,
        "source": {
            "reading": reading,
            "expected_surfaces": surfaces,
            "row_sha256": sha256_uri(f"row:{case_id}".encode()),
            "corpus_sha256": sha256_uri(b"fixture-corpus"),
        },
        "elements": {
            "unit": serve.ELEMENT_UNIT,
            "values": [
                {"index": index, "text": character}
                for index, character in enumerate(reading)
            ],
        },
        "candidate_outputs_consulted": False,
        "preannotation": {
            "marked_reading": marked_reading,
            "first_segment_count": len(marked_reading.split("|", 1)[0]),
            "confidence": "exact",
        },
        "token_audit": {"summary": "synthetic fixture"},
        "review": {"status": "pending", "marked_reading": None},
    }


def write_queue(path: Path, records: list[dict[str, object]]) -> None:
    path.write_bytes(canonical_jsonl(records))


def workbook_row(
    record: dict[str, object],
    *,
    review_status: str,
    marked_reading: str = "",
    notes: str = "",
) -> list[str]:
    source = record["source"]
    assert isinstance(source, dict)
    surfaces = source["expected_surfaces"]
    assert isinstance(surfaces, list)
    return [
        "1",
        "1",
        str(record["id"]),
        str(record["category"]),
        str(source["reading"]),
        str(surfaces[0]),
        "今日〈名詞〉 / は〈助詞〉 / 雨〈名詞〉",
        str(record["preannotation"]["marked_reading"]),
        "exact",
        review_status,
        marked_reading,
        notes,
        "OK",
        "4",
        "0",
        "1",
        "なし",
        "なし",
        str(source["row_sha256"]),
        "1",
    ]


def reading_only_path(
    boundaries: list[int], *, path_id: str = "human-path-1"
) -> dict[str, object]:
    return {
        "path_id": path_id,
        "status": "acceptable",
        "surface_reference_id": "surface-0",
        "reading_boundaries": boundaries,
        "surface_boundaries": None,
        "alignment_status": "reading_only",
        "provenance": {"kind": "human"},
    }


def review_payload(
    *,
    base_revision: int = 0,
    path_set_status: str = "closed",
    needs_adjudication: bool = False,
    paths: list[dict[str, object]] | None = None,
    notes: str | None = "fixture review",
) -> dict[str, object]:
    return {
        "base_revision": base_revision,
        "path_set_status": path_set_status,
        "needs_adjudication": needs_adjudication,
        "acceptable_paths": paths if paths is not None else [reading_only_path([4])],
        "notes": notes,
        "action": {"kind": "test"},
    }


def canonical_jsonl(records: list[dict[str, object]]) -> bytes:
    return b"".join(
        (
            json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        for record in records
    )


def sha256_uri(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def column_name(index: int) -> str:
    if index < 1:
        raise ValueError("column index must be positive")
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(ord("A") + remainder) + result
    return result


def inline_string_cell(reference: str, value: str) -> str:
    preserve = ' xml:space="preserve"' if value != value.strip() else ""
    return (
        f'<c r="{reference}" t="inlineStr"><is><t{preserve}>'
        f"{escape(value)}"
        "</t></is></c>"
    )


def write_minimal_xlsx(
    path: Path,
    sheets: list[tuple[str, list[list[str]]]],
) -> None:
    """Write the OOXML subset needed by the annotation workbook importer.

    Inline strings deliberately avoid sharedStrings.xml, so importer tests can
    isolate workbook/sheet relationship handling from Excel implementation
    details.  Entries are written in a stable order with stable timestamps.
    """
    if not sheets:
        raise ValueError("at least one sheet is required")

    content_types = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
        '<Default Extension="xml" ContentType="application/xml"/>',
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
    ]
    for index in range(1, len(sheets) + 1):
        content_types.append(
            '<Override '
            f'PartName="/xl/worksheets/sheet{index}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        )
    content_types.append("</Types>")

    root_relationships = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/>'
        "</Relationships>"
    )

    workbook_sheets = "".join(
        f'<sheet name="{escape(name)}" sheetId="{index}" r:id="rId{index}"/>'
        for index, (name, _) in enumerate(sheets, start=1)
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<sheets>{workbook_sheets}</sheets>"
        "</workbook>"
    )
    workbook_relationships = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(
            '<Relationship '
            f'Id="rId{index}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{index}.xml"/>'
            for index in range(1, len(sheets) + 1)
        )
        + "</Relationships>"
    )

    members: list[tuple[str, str]] = [
        ("[Content_Types].xml", "".join(content_types)),
        ("_rels/.rels", root_relationships),
        ("xl/workbook.xml", workbook),
        ("xl/_rels/workbook.xml.rels", workbook_relationships),
    ]
    for sheet_index, (_, rows) in enumerate(sheets, start=1):
        row_xml = []
        for row_index, row in enumerate(rows, start=1):
            cells = "".join(
                inline_string_cell(
                    f"{column_name(column_index)}{row_index}", value
                )
                for column_index, value in enumerate(row, start=1)
                if value != ""
            )
            row_xml.append(f'<row r="{row_index}">{cells}</row>')
        worksheet = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"<sheetData>{''.join(row_xml)}</sheetData>"
            "</worksheet>"
        )
        members.append((f"xl/worksheets/sheet{sheet_index}.xml", worksheet))

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for member_name, contents in members:
            info = zipfile.ZipInfo(member_name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, contents.encode("utf-8"))


def write_annotation_workbook(
    path: Path,
    *,
    review_status: str,
    marked_reading: str,
    reading: str = "きょうはあめ",
    expected_surface: str = "今日は雨",
    case_id: str = "case-1",
) -> None:
    row = [
        "1",
        "1",
        case_id,
        "fixture",
        reading,
        expected_surface,
        "今日〈名詞〉 / は〈助詞〉 / 雨〈名詞〉",
        "きょうは|あめ",
        "exact",
        review_status,
        marked_reading,
        "fixture note",
        "OK",
        "4",
        "0",
        "1",
        "なし",
        "なし",
        "sha256:" + "a" * 64,
        "1",
    ]
    write_minimal_xlsx(
        path,
        [
            ("使い方", [["fixture"]]),
            ("アノテーション", [ANNOTATION_HEADERS, row]),
        ],
    )


class QueueContractTests(unittest.TestCase):
    def test_load_queue_accepts_canonical_blind_records(self) -> None:
        records = [
            queue_record("case-1"),
            queue_record(
                "case-2",
                reading="あしたははれ",
                expected_surfaces=["明日は晴れ", "あしたは晴れ"],
                marked_reading="あしたは|はれ",
            ),
        ]
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "queue.jsonl"
            write_queue(path, records)

            loaded = serve.load_queue(path)

        self.assertEqual([record["id"] for record in loaded.records], ["case-1", "case-2"])
        self.assertEqual(
            loaded.by_id["case-2"]["source"]["expected_surfaces"],
            ["明日は晴れ", "あしたは晴れ"],
        )
        self.assertEqual(loaded.sha256, sha256_uri(canonical_jsonl(records)))

    def test_load_queue_rejects_non_strict_jsonl_and_changed_contract(self) -> None:
        record = queue_record("case-1")
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            malformed_cases = {
                "missing-final-lf": canonical_jsonl([record]).removesuffix(b"\n"),
                "crlf": canonical_jsonl([record]).replace(b"\n", b"\r\n"),
                "bom": b"\xef\xbb\xbf" + canonical_jsonl([record]),
                "blank-line": canonical_jsonl([record]) + b"\n",
            }
            for name, data in malformed_cases.items():
                with self.subTest(name=name):
                    path = root / f"{name}.jsonl"
                    path.write_bytes(data)
                    with self.assertRaises(serve.AnnotationError):
                        serve.load_queue(path)

            duplicate_path = root / "duplicate.jsonl"
            write_queue(duplicate_path, [record, record])
            with self.assertRaisesRegex(serve.AnnotationError, "duplicates id"):
                serve.load_queue(duplicate_path)

            consulted = deepcopy(record)
            consulted["candidate_outputs_consulted"] = True
            consulted_path = root / "consulted.jsonl"
            write_queue(consulted_path, [consulted])
            with self.assertRaisesRegex(serve.AnnotationError, "candidate-output blind"):
                serve.load_queue(consulted_path)

            reserved = queue_record(
                "case-reserved",
                reading="きょう|は",
                expected_surfaces=["今日は"],
                marked_reading="きょう|は",
            )
            reserved_path = root / "reserved.jsonl"
            write_queue(reserved_path, [reserved])
            with self.assertRaisesRegex(serve.AnnotationError, "reserved boundary marker"):
                serve.load_queue(reserved_path)


class WorkbookImportTests(unittest.TestCase):
    def test_realistic_xlsx_statuses_map_to_v2_review_states(self) -> None:
        records = [
            queue_record("case-open"),
            queue_record("case-pending"),
            queue_record("case-adjudication"),
            queue_record("case-invalid"),
        ]
        rows = [
            workbook_row(
                records[0],
                review_status="承認",
                marked_reading="きょうは|あめ",
                notes="人手で確認済み\n二行目\t補足",
            ),
            workbook_row(records[1], review_status="未確認"),
            workbook_row(
                records[2],
                review_status="曖昧",
                marked_reading="きょうは|あめ",
                notes="別経路あり",
            ),
            workbook_row(
                records[3], review_status="無効入力", notes="読みが不正"
            ),
        ]
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            queue_path = root / "queue.jsonl"
            workbook_path = root / "annotations.xlsx"
            write_queue(queue_path, records)
            write_minimal_xlsx(
                workbook_path,
                [
                    ("使い方", [["fixture"]]),
                    ("アノテーション", [ANNOTATION_HEADERS, *rows]),
                ],
            )
            queue = serve.load_queue(queue_path)
            imported_rows, workbook_sha = serve.read_annotation_workbook(
                workbook_path
            )
            reviews = serve.import_workbook_reviews(
                queue,
                imported_rows,
                workbook_sha256=workbook_sha,
                annotator_id="fixture-reviewer",
            )

        open_review = reviews["case-open"]
        self.assertEqual(open_review["path_set_status"], "open")
        self.assertTrue(open_review["reviewed_once"])
        self.assertEqual(
            open_review["acceptable_paths"][0]["reading_boundaries"], [4]
        )
        self.assertEqual(
            open_review["acceptable_paths"][0]["provenance"]["kind"], "xlsx"
        )
        self.assertEqual(open_review["imported"]["legacy_status"], "承認")
        self.assertEqual(open_review["notes"], "人手で確認済み\n二行目\t補足")

        pending_review = reviews["case-pending"]
        self.assertEqual(pending_review["path_set_status"], "pending")
        self.assertFalse(pending_review["reviewed_once"])
        self.assertFalse(pending_review["needs_adjudication"])

        adjudication_review = reviews["case-adjudication"]
        self.assertEqual(adjudication_review["path_set_status"], "pending")
        self.assertTrue(adjudication_review["reviewed_once"])
        self.assertTrue(adjudication_review["needs_adjudication"])
        self.assertEqual(len(adjudication_review["acceptable_paths"]), 1)
        ambiguous_seed = adjudication_review["acceptable_paths"][0]
        self.assertEqual(ambiguous_seed["path_id"], "xlsx-ambiguous-draft-1")
        self.assertEqual(ambiguous_seed["status"], "draft")
        self.assertEqual(ambiguous_seed["alignment_status"], "reading_only")
        self.assertEqual(ambiguous_seed["reading_boundaries"], [4])
        self.assertIsNone(ambiguous_seed["surface_boundaries"])

        invalid_review = reviews["case-invalid"]
        self.assertEqual(invalid_review["path_set_status"], "invalid")
        self.assertTrue(invalid_review["reviewed_once"])
        self.assertEqual(invalid_review["acceptable_paths"], [])


class XlsxHardeningTests(unittest.TestCase):
    def test_excel_column_limit_accepts_xfd_and_rejects_later_columns(self) -> None:
        self.assertEqual(serve._column_index("XFD1"), 16383)
        for reference in ("XFE1", "ZZZZZZ1"):
            with self.subTest(reference=reference):
                with self.assertRaisesRegex(serve.AnnotationError, "exceeds Excel column XFD"):
                    serve._column_index(reference)

    def test_shared_string_negative_index_is_rejected(self) -> None:
        cell = ET.fromstring(
            f'<c xmlns="{serve.XML_MAIN}" t="s"><v>-1</v></c>'
        )
        with self.assertRaisesRegex(serve.AnnotationError, "shared string index"):
            serve._xlsx_cell_text(cell, ["last string must not be selected"])

    def test_sparse_xfd_header_is_read_without_dense_row_expansion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "sparse-xfd.xlsx"
            write_annotation_workbook(
                path,
                review_status="承認",
                marked_reading="きょうは|あめ",
            )
            with zipfile.ZipFile(path) as archive:
                members = {
                    info.filename: archive.read(info.filename)
                    for info in archive.infolist()
                }
            sheet_name = "xl/worksheets/sheet2.xml"
            sheet = members[sheet_name].decode("utf-8")
            sheet = sheet.replace(
                "</row>", inline_string_cell("XFD1", "疎列") + "</row>", 1
            )
            members[sheet_name] = sheet.encode("utf-8")
            with zipfile.ZipFile(
                path, "w", compression=zipfile.ZIP_DEFLATED
            ) as archive:
                for member_name, contents in members.items():
                    archive.writestr(member_name, contents)
            expected_workbook_sha = sha256_uri(path.read_bytes())

            builtin_range = range

            def reject_dense_range(*args: int) -> range:
                result = builtin_range(*args)
                if len(result) > 1024:
                    raise AssertionError("XLSX parser attempted dense row expansion")
                return result

            with mock.patch.object(
                serve, "range", reject_dense_range, create=True
            ):
                rows, workbook_sha = serve.read_annotation_workbook(path)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ID"], "case-1")
        self.assertEqual(rows[0]["レビュー済み分割"], "きょうは|あめ")
        self.assertEqual(rows[0]["疎列"], "")
        self.assertEqual(workbook_sha, expected_workbook_sha)


class ReviewContractTests(unittest.TestCase):
    def test_marked_reading_boundary_round_trip_and_rejections(self) -> None:
        reading = "きょうはあめ"
        self.assertEqual(
            serve.marked_reading_to_boundaries("きょう|は|あめ", reading),
            [3, 4],
        )
        self.assertEqual(
            serve.boundaries_to_marked_reading(reading, [3, 4]),
            "きょう|は|あめ",
        )
        self.assertEqual(serve.marked_reading_to_boundaries(reading, reading), [])

        for marked in ("|きょうはあめ", "きょうはあめ|", "きょう|はれ"):
            with self.subTest(marked=marked):
                with self.assertRaises(serve.AnnotationError):
                    serve.marked_reading_to_boundaries(marked, reading)

    def test_normalize_path_and_review_preserve_alignment_semantics(self) -> None:
        case = queue_record("case-1")
        aligned_path = {
            "path_id": "aligned-path-1",
            "status": "acceptable",
            "surface_reference_id": "surface-0",
            "reading_boundaries": [4],
            "surface_boundaries": [3],
            "alignment_status": "aligned",
            "provenance": {"kind": "human"},
        }

        normalized_path = serve.normalize_path(aligned_path, case)
        self.assertEqual(normalized_path["reading_boundaries"], [4])
        self.assertEqual(normalized_path["surface_boundaries"], [3])
        self.assertEqual(normalized_path["alignment_status"], "aligned")

        normalized_review = serve.normalize_review(
            {
                "path_set_status": "open",
                "needs_adjudication": True,
                "acceptable_paths": [aligned_path],
                "notes": "複数経路の確認が必要\n二行目\t補足",
            },
            case,
            annotator_id="fixture-reviewer",
        )
        self.assertEqual(normalized_review["path_set_status"], "open")
        self.assertTrue(normalized_review["needs_adjudication"])
        self.assertEqual(normalized_review["acceptable_paths"], [normalized_path])
        self.assertEqual(
            normalized_review["notes"], "複数経路の確認が必要\n二行目\t補足"
        )

        bad_alignment = deepcopy(aligned_path)
        bad_alignment["surface_boundaries"] = []
        with self.assertRaisesRegex(serve.AnnotationError, "same reading and surface"):
            serve.normalize_path(bad_alignment, case)

        closed_adjudication = {
            "path_set_status": "closed",
            "needs_adjudication": True,
            "acceptable_paths": [aligned_path],
        }
        with self.assertRaisesRegex(serve.AnnotationError, "closed review"):
            serve.normalize_review(
                closed_adjudication,
                case,
                annotator_id="fixture-reviewer",
            )

        invalid_with_path = {
            "path_set_status": "invalid",
            "needs_adjudication": False,
            "acceptable_paths": [aligned_path],
        }
        with self.assertRaisesRegex(serve.AnnotationError, "must not contain paths"):
            serve.normalize_review(
                invalid_with_path,
                case,
                annotator_id="fixture-reviewer",
            )

    def test_corrected_reading_is_nfc_and_drives_boundary_validation(self) -> None:
        case = queue_record(
            "case-reading-typo",
            reading="きよはあめ",
            expected_surfaces=["今日は雨"],
            marked_reading="きよは|あめ",
        )
        corrected = "きょうはあめ"
        boundary_only_valid_for_correction = reading_only_path([5])

        correction_only = serve.normalize_review(
            {
                "path_set_status": "pending",
                "needs_adjudication": False,
                "corrected_reading": corrected,
                "acceptable_paths": [],
            },
            case,
            annotator_id="fixture-reviewer",
        )
        normalized = serve.normalize_review(
            {
                "path_set_status": "open",
                "needs_adjudication": False,
                "acceptable_paths": [boundary_only_valid_for_correction],
            },
            case,
            previous=correction_only,
            annotator_id="fixture-reviewer",
        )

        self.assertEqual(normalized["corrected_reading"], corrected)
        self.assertEqual(
            normalized["acceptable_paths"][0]["reading_boundaries"], [5]
        )

        for invalid_reading, expected_message in (
            ("", "non-empty"),
            ("きょう|はあめ", "boundary marker"),
            ("きょ\nうはあめ", "control"),
            ("か\u3099くせい", "NFC"),
            ("あ" * (serve.MAX_CORRECTED_READING_CODE_POINTS + 1), "too long"),
        ):
            with self.subTest(corrected_reading=repr(invalid_reading)):
                with self.assertRaisesRegex(
                    serve.AnnotationError, expected_message
                ):
                    serve.normalize_review(
                        {
                            "path_set_status": "pending",
                            "needs_adjudication": False,
                            "corrected_reading": invalid_reading,
                            "acceptable_paths": [],
                        },
                        case,
                        annotator_id="fixture-reviewer",
                    )

        normalized_without_correction = serve.normalize_review(
            {
                "path_set_status": "pending",
                "needs_adjudication": False,
                "corrected_reading": None,
                "acceptable_paths": [],
            },
            case,
            annotator_id="fixture-reviewer",
        )
        self.assertIsNone(normalized_without_correction["corrected_reading"])

    def test_codex_model_and_effort_validation(self) -> None:
        self.assertIsNone(serve._normalize_codex_model(None, "model"))
        self.assertEqual(
            serve._normalize_codex_model("provider/gpt-5.6-sol", "model"),
            "provider/gpt-5.6-sol",
        )
        self.assertEqual(
            serve._normalize_codex_effort("ultra", "effort"), "ultra"
        )

        for invalid_model in ("", "has space", "e\u0301", "a" * 129):
            with self.subTest(model=repr(invalid_model)):
                with self.assertRaises(serve.AnnotationError):
                    serve._normalize_codex_model(invalid_model, "model")
        for invalid_effort in ("", "very high", "low\n", "a" * 33):
            with self.subTest(effort=repr(invalid_effort)):
                with self.assertRaises(serve.AnnotationError):
                    serve._normalize_codex_effort(invalid_effort, "effort")


class FakeProposalBackend:
    def __init__(
        self,
        output: dict[str, object] | None = None,
        *,
        failure: Exception | None = None,
        catalog: dict[str, object] | None = None,
    ) -> None:
        self.output = output
        self.failure = failure
        self.model = "fixture-requested-model"
        self.effort = "medium"
        self.settings_revision: int | None = None
        self.calls: list[dict[str, object]] = []
        self.catalog_calls = 0
        self.catalog = catalog or {
            "provider": "codex-app-server",
            "fetched_at": "2026-07-16T00:00:00Z",
            "app_server_user_agent": "fixture-app-server/1",
            "models": [
                {
                    "id": "fixture-requested-model",
                    "model": "fixture-requested-model",
                    "display_name": "Fixture Model",
                    "description": "Fixture default model",
                    "is_default": True,
                    "default_reasoning_effort": "medium",
                    "supported_reasoning_efforts": [
                        {
                            "reasoning_effort": "low",
                            "description": "Lower latency",
                        },
                        {
                            "reasoning_effort": "medium",
                            "description": "Balanced",
                        },
                        {
                            "reasoning_effort": "high",
                            "description": "More reasoning",
                        },
                    ],
                }
            ],
        }
        self.closed = False

    def metadata(self) -> dict[str, object]:
        return {
            "enabled": True,
            "configured": True,
            "provider": "codex-app-server",
            "status": "ready",
            "model": self.model,
            "effort": self.effort,
            "message": "fixture Codex App Server",
        }

    def update_configuration(
        self, *, model: object, effort: object, revision: object
    ) -> None:
        self.model = serve._normalize_codex_model(model, "fixture model")
        self.effort = serve._normalize_codex_effort(effort, "fixture effort")
        if type(revision) is not int or revision < 0:
            raise serve.AnnotationError("fixture revision is invalid")
        self.settings_revision = revision

    def generate(
        self,
        *,
        instructions: str,
        input_text: str,
        output_schema: dict[str, object],
        expected_settings_revision: int | None = None,
    ) -> serve.CodexProposalResult:
        if (
            expected_settings_revision is not None
            and self.settings_revision != expected_settings_revision
        ):
            raise serve.CodexAppServerStale("fixture settings revision changed")
        requested_model = self.model
        reasoning_effort = self.effort
        settings_revision = self.settings_revision
        self.calls.append(
            {
                "instructions": instructions,
                "input_text": input_text,
                "output_schema": deepcopy(output_schema),
                "requested_model": requested_model,
                "reasoning_effort": reasoning_effort,
            }
        )
        if self.failure is not None:
            raise self.failure
        assert self.output is not None
        return serve.CodexProposalResult(
            output=deepcopy(self.output),
            model="fixture-actual-model",
            model_provider="fixture-provider",
            requested_model=requested_model,
            reasoning_effort=reasoning_effort,
            settings_revision=settings_revision,
            app_server_user_agent="fixture-app-server/1",
            thread_id="thread-fixture",
            turn_id="turn-fixture",
            message_id="message-fixture",
            duration_ms=42,
        )

    def list_models(self) -> dict[str, object]:
        self.catalog_calls += 1
        if self.failure is not None:
            raise self.failure
        return deepcopy(self.catalog)

    def close(self) -> None:
        self.closed = True


def write_fake_codex_app_server(
    executable: Path,
    transcript_path: Path,
    output: dict[str, object],
    *,
    authenticated: bool = True,
    model_pages: list[dict[str, object]] | None = None,
) -> None:
    output_text = json.dumps(
        output, ensure_ascii=False, separators=(",", ":")
    )
    model_pages_text = json.dumps(
        model_pages
        or [
            {
                "data": [
                    {
                        "id": "fixture-app-server-model",
                        "model": "fixture-app-server-model",
                        "displayName": "Fixture App Server Model",
                        "description": "Fixture model",
                        "hidden": False,
                        "isDefault": True,
                        "defaultReasoningEffort": "medium",
                        "supportedReasoningEfforts": [
                            {
                                "reasoningEffort": "low",
                                "description": "Lower latency",
                            },
                            {
                                "reasoningEffort": "medium",
                                "description": "Balanced",
                            },
                        ],
                    }
                ],
                "nextCursor": None,
            }
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    script = textwrap.dedent(
        """\
        #!/usr/bin/env python3
        import hashlib
        import json
        import os
        import sys

        TRANSCRIPT = @@TRANSCRIPT@@
        OUTPUT = @@OUTPUT@@
        MODEL_PAGES = @@MODEL_PAGES@@
        ACCOUNT_AUTHENTICATED = @@AUTHENTICATED@@
        ENV_ACCESS_TOKEN = os.environ.get("CODEX_ACCESS_TOKEN")
        ENV_ACCESS_TOKEN_SHA256 = (
            hashlib.sha256(ENV_ACCESS_TOKEN.encode("utf-8")).hexdigest()
            if ENV_ACCESS_TOKEN
            else None
        )
        SENSITIVE_KEYS = {
            "accessToken",
            "access_token",
            "apiKey",
            "OPENAI_API_KEY",
            "token",
            "chatgptAccountId",
            "accountId",
            "account_id",
        }

        def redact(value):
            if isinstance(value, dict):
                return {
                    key: "***" if key in SENSITIVE_KEYS else redact(item)
                    for key, item in value.items()
                }
            if isinstance(value, list):
                return [redact(item) for item in value]
            return value

        def credential_metadata(message):
            if message.get("method") != "account/login/start":
                return {"credential_type": None, "credential_sha256": None}
            params = message.get("params", {})
            credential_type = params.get("type")
            if credential_type == "chatgptAuthTokens":
                credential = params.get("accessToken")
            elif credential_type == "apiKey":
                credential = params.get("apiKey")
            else:
                credential = None
            fingerprint = (
                hashlib.sha256(credential.encode("utf-8")).hexdigest()
                if isinstance(credential, str) and credential
                else None
            )
            return {
                "credential_type": credential_type,
                "credential_sha256": fingerprint,
            }

        def record(direction, message):
            entry = {
                "direction": direction,
                "message": redact(message),
                "pid": os.getpid(),
                "has_env_access_token": bool(ENV_ACCESS_TOKEN),
                "env_access_token_sha256": ENV_ACCESS_TOKEN_SHA256,
            }
            if direction == "client":
                entry.update(credential_metadata(message))
            with open(TRANSCRIPT, "a", encoding="utf-8") as transcript:
                transcript.write(json.dumps(
                    entry,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ) + "\\n")

        def send(message):
            record("server", message)
            sys.stdout.write(json.dumps(
                message, ensure_ascii=False, separators=(",", ":")
            ) + "\\n")
            sys.stdout.flush()

        experimental_api = False
        for line in sys.stdin:
            message = json.loads(line)
            record("client", message)
            method = message.get("method")
            if method == "initialize":
                experimental_api = bool(
                    message.get("params", {})
                    .get("capabilities", {})
                    .get("experimentalApi")
                )
                send({
                    "id": message["id"],
                    "result": {"userAgent": "fake-codex-app-server/1"},
                })
            elif method == "initialized":
                send({
                    "id": 9000,
                    "method": "item/commandExecution/requestApproval",
                    "params": {"reason": "fixture unsupported request"},
                })
                if experimental_api:
                    send({
                        "id": 9001,
                        "method": "account/chatgptAuthTokens/refresh",
                        "params": {"reason": "fixture refresh request"},
                    })
            elif method == "account/login/start":
                send({
                    "id": message["id"],
                    "result": {"type": message["params"]["type"]},
                })
            elif method == "account/read":
                send({
                    "id": message["id"],
                    "result": {
                        "requiresOpenaiAuth": True,
                        "account": (
                            {"type": "fixture-authenticated-account"}
                            if ACCOUNT_AUTHENTICATED
                            else None
                        ),
                    },
                })
            elif method == "model/list":
                cursor = message.get("params", {}).get("cursor")
                cursors = [None]
                cursors.extend(
                    page.get("nextCursor") for page in MODEL_PAGES[:-1]
                )
                if cursor not in cursors:
                    send({
                        "id": message["id"],
                        "error": {
                            "code": -32602,
                            "message": "unknown fixture cursor",
                        },
                    })
                else:
                    send({
                        "id": message["id"],
                        "result": MODEL_PAGES[cursors.index(cursor)],
                    })
            elif method == "thread/start":
                send({
                    "id": message["id"],
                    "result": {
                        "thread": {"id": "thread-app-server-fixture"},
                        "model": "fixture-app-server-model",
                        "modelProvider": "fixture-app-server-provider",
                    },
                })
            elif message.get("id") == 9000:
                if message.get("error", {}).get("code") != -32601:
                    raise RuntimeError("server request was not rejected")
            elif message.get("id") == 9001:
                error = message.get("error", {})
                if error.get("code") != -32002:
                    raise RuntimeError("unchanged token refresh was not rejected")
            elif method == "turn/start":
                thread_id = message["params"]["threadId"]
                turn_id = "turn-app-server-fixture"
                item = {
                    "id": "message-app-server-fixture",
                    "type": "agentMessage",
                    "text": OUTPUT,
                    "phase": "final_answer",
                }
                send({
                    "method": "item/started",
                    "params": {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "item": {
                            key: value for key, value in item.items()
                            if key != "text"
                        },
                    },
                })
                send({
                    "method": "item/completed",
                    "params": {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "item": item,
                    },
                })
                send({
                    "method": "turn/completed",
                    "params": {
                        "threadId": thread_id,
                        "turn": {
                            "id": turn_id,
                            "status": "completed",
                            "durationMs": 73,
                        },
                    },
                })
                send({
                    "id": message["id"],
                    "result": {"turn": {"id": turn_id}},
                })
            elif method == "thread/unsubscribe":
                send({"id": message["id"], "result": {}})
        """
    ).replace("@@TRANSCRIPT@@", repr(str(transcript_path))).replace(
        "@@OUTPUT@@", repr(output_text)
    ).replace(
        "@@MODEL_PAGES@@", repr(json.loads(model_pages_text))
    ).replace("@@AUTHENTICATED@@", repr(authenticated))
    executable.write_text(script, encoding="utf-8")
    executable.chmod(0o700)


class WorkspaceTests(unittest.TestCase):
    @staticmethod
    def make_workspace(
        queue_path: Path,
        workspace_root: Path,
        *,
        workbook_path: Path | None = None,
        proposal_backend: object | None = None,
        proposal_backend_message: str | None = None,
    ) -> serve.Workspace:
        return serve.Workspace(
            serve.load_queue(queue_path),
            workspace_root,
            workbook_path=workbook_path,
            annotator_id="fixture-reviewer",
            proposal_backend=proposal_backend,
            proposal_backend_message=proposal_backend_message,
        )

    @staticmethod
    def wait_for_proposal_jobs(
        manager: serve.ProposalJobManager,
        predicate: Callable[[dict[str, object]], bool],
        *,
        timeout: float = 5.0,
    ) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        while True:
            status = manager.status()
            if predicate(status):
                return status
            if time.monotonic() >= deadline:
                raise AssertionError(
                    f"proposal queue did not reach expected state: {status}"
                )
            time.sleep(0.01)

    def test_revision_conflict_reload_and_deterministic_export(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            queue_path = root / "queue.jsonl"
            workspace_root = root / "workspace"
            write_queue(queue_path, [queue_record("case-1")])
            workspace = self.make_workspace(queue_path, workspace_root)
            try:
                with self.assertRaisesRegex(serve.AnnotationError, "already open"):
                    self.make_workspace(queue_path, workspace_root)

                saved = workspace.patch_review(
                    "case-1", review_payload(notes="一行目\n二行目\t補足")
                )
                self.assertEqual(saved["revision"], 1)
                self.assertEqual(saved["path_set_status"], "closed")
                self.assertEqual(saved["notes"], "一行目\n二行目\t補足")
                with self.assertRaises(serve.RevisionConflict):
                    workspace.patch_review("case-1", review_payload(base_revision=0))

                first_export, first_manifest = workspace.export_bundle()
                self.assertEqual(workspace.export_bundle(), (first_export, first_manifest))
                (
                    written_export,
                    written_manifest,
                    review_path,
                    manifest_path,
                ) = workspace.export_and_write()
                self.assertEqual(written_export, first_export)
                self.assertEqual(written_manifest, first_manifest)
                self.assertEqual(review_path.read_bytes(), first_export)
                self.assertEqual(
                    json.loads(manifest_path.read_text(encoding="utf-8")),
                    first_manifest,
                )
                self.assertEqual(
                    first_manifest["reviewed_paths_sha256"],
                    sha256_uri(first_export),
                )
                self.assertTrue(first_manifest["complete"])

                exported = json.loads(first_export.decode("utf-8"))
                self.assertEqual(exported["schema"], serve.EXPORT_SCHEMA)
                self.assertEqual(
                    exported["source"]["surface_unit"], serve.SURFACE_UNIT
                )
                self.assertEqual(
                    exported["acceptable_paths"][0]["reading_boundaries"], [4]
                )
                self.assertEqual(exported["review"]["notes"], "一行目\n二行目\t補足")
            finally:
                workspace.close()

            reloaded = self.make_workspace(queue_path, workspace_root)
            try:
                detail = reloaded.case_detail("case-1")
                self.assertEqual(detail["review"]["revision"], 1)
                self.assertEqual(detail["review"]["notes"], "一行目\n二行目\t補足")
                self.assertEqual(reloaded.export_bundle(), (first_export, first_manifest))
            finally:
                reloaded.close()

    def test_bulk_finalize_preserves_drafts_and_is_idempotent(self) -> None:
        acceptable = reading_only_path([4], path_id="accepted-path")
        draft = deepcopy(acceptable)
        draft.update(
            {
                "path_id": "draft-path",
                "status": "draft",
                "reading_boundaries": [3, 4],
                "provenance": {
                    "kind": "human",
                    "source_path_id": "accepted-path",
                },
            }
        )
        expected_paths = [acceptable, draft]

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            queue_path = root / "queue.jsonl"
            workspace_root = root / "workspace"
            write_queue(
                queue_path,
                [queue_record("case-open"), queue_record("case-closed")],
            )
            workspace = self.make_workspace(queue_path, workspace_root)
            try:
                open_review = workspace.patch_review(
                    "case-open",
                    review_payload(
                        path_set_status="open",
                        paths=expected_paths,
                    ),
                )
                closed_review = workspace.patch_review(
                    "case-closed",
                    review_payload(path_set_status="closed"),
                )
                event_lines_before = workspace.events_path.read_bytes().splitlines()

                with mock.patch.object(
                    workspace,
                    "_write_snapshot",
                    wraps=workspace._write_snapshot,
                ) as snapshot_writer, mock.patch.object(
                    workspace,
                    "_export_bundle_locked",
                    wraps=workspace._export_bundle_locked,
                ) as exporter:
                    result = workspace.finalize_reviewed()
                    snapshot_writer.assert_called_once_with()
                    exporter.assert_called_once_with()

                self.assertEqual(result["finalized_cases"], 1)
                self.assertEqual(result["already_closed_cases"], 1)
                self.assertTrue(result["manifest"]["complete"])
                self.assertEqual(
                    result["manifest"]["path_set_statuses"], {"closed": 2}
                )
                finalized = workspace.case_detail("case-open")["review"]
                self.assertEqual(finalized["path_set_status"], "closed")
                self.assertEqual(finalized["revision"], open_review["revision"] + 1)
                self.assertEqual(finalized["acceptable_paths"], expected_paths)
                unchanged = workspace.case_detail("case-closed")["review"]
                self.assertEqual(unchanged["revision"], closed_review["revision"])

                event_lines_after = workspace.events_path.read_bytes().splitlines()
                self.assertEqual(
                    len(event_lines_after), len(event_lines_before) + 1
                )
                finalization_event = json.loads(event_lines_after[-1])
                self.assertEqual(finalization_event["case_id"], "case-open")
                self.assertEqual(
                    finalization_event["action"]["kind"], "bulk_finalize"
                )
                self.assertEqual(
                    finalization_event["review"]["acceptable_paths"],
                    expected_paths,
                )
                exported_records = [
                    json.loads(line)
                    for line in Path(result["review_path"])
                    .read_text(encoding="utf-8")
                    .splitlines()
                ]
                self.assertEqual(
                    exported_records[0]["acceptable_paths"], [acceptable]
                )
                self.assertEqual(exported_records[0]["draft_paths"], [draft])

                journal_before_retry = workspace.events_path.read_bytes()
                snapshot_before_retry = workspace.snapshot_path.read_bytes()
                review_export_before_retry = Path(result["review_path"]).read_bytes()
                manifest_before_retry = Path(result["manifest_path"]).read_bytes()
                with mock.patch.object(
                    workspace,
                    "_write_snapshot",
                    wraps=workspace._write_snapshot,
                ) as snapshot_writer, mock.patch.object(
                    workspace,
                    "_export_bundle_locked",
                    wraps=workspace._export_bundle_locked,
                ) as exporter:
                    retried = workspace.finalize_reviewed()
                    snapshot_writer.assert_not_called()
                    exporter.assert_called_once_with()
                self.assertIsNone(retried["batch_id"])
                self.assertEqual(retried["finalized_cases"], 0)
                self.assertEqual(retried["already_closed_cases"], 2)
                self.assertEqual(
                    workspace.events_path.read_bytes(), journal_before_retry
                )
                self.assertEqual(
                    workspace.snapshot_path.read_bytes(), snapshot_before_retry
                )
                self.assertEqual(
                    Path(retried["review_path"]).read_bytes(),
                    review_export_before_retry,
                )
                self.assertEqual(
                    Path(retried["manifest_path"]).read_bytes(),
                    manifest_before_retry,
                )
            finally:
                workspace.close()

            reloaded = self.make_workspace(queue_path, workspace_root)
            try:
                reloaded_review = reloaded.case_detail("case-open")["review"]
                self.assertEqual(reloaded_review["path_set_status"], "closed")
                self.assertEqual(
                    reloaded_review["acceptable_paths"], expected_paths
                )
            finally:
                reloaded.close()

    def test_bulk_finalize_refuses_any_incomplete_case_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            queue_path = root / "queue.jsonl"
            workspace_root = root / "workspace"
            write_queue(
                queue_path,
                [
                    queue_record("case-good"),
                    queue_record("case-pending"),
                    queue_record("case-adjudication"),
                    queue_record("case-invalid"),
                ],
            )
            workspace = self.make_workspace(queue_path, workspace_root)
            try:
                workspace.patch_review(
                    "case-good",
                    review_payload(path_set_status="open"),
                )
                workspace.patch_review(
                    "case-adjudication",
                    review_payload(
                        path_set_status="open",
                        needs_adjudication=True,
                    ),
                )
                workspace.patch_review(
                    "case-invalid",
                    review_payload(path_set_status="invalid", paths=[]),
                )
                journal_before = workspace.events_path.read_bytes()
                snapshot_before = workspace.snapshot_path.read_bytes()
                reviews_before = deepcopy(workspace.reviews)

                with self.assertRaisesRegex(
                    serve.AnnotationError,
                    "cannot finalize reviewed annotations",
                ) as raised:
                    workspace.finalize_reviewed()

                message = str(raised.exception)
                self.assertIn("case-pending: reviewed_once is false", message)
                self.assertIn("path_set_status is pending", message)
                self.assertIn("no acceptable path", message)
                self.assertIn(
                    "case-adjudication: needs_adjudication is true", message
                )
                self.assertIn("case-invalid: path_set_status is invalid", message)
                self.assertEqual(workspace.events_path.read_bytes(), journal_before)
                self.assertEqual(workspace.snapshot_path.read_bytes(), snapshot_before)
                self.assertEqual(workspace.reviews, reviews_before)
                self.assertFalse(workspace.exports_dir.exists())
            finally:
                workspace.close()

    def test_finalize_reviewed_cli_writes_exports_without_starting_server(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            queue_path = root / "queue.jsonl"
            workspace_root = root / "workspace"
            write_queue(queue_path, [queue_record("case-1")])
            workspace = self.make_workspace(queue_path, workspace_root)
            workspace.patch_review(
                "case-1", review_payload(path_set_status="open")
            )
            workspace.close()

            stdout = io.StringIO()
            stderr = io.StringIO()
            with mock.patch("sys.stdout", stdout), mock.patch(
                "sys.stderr", stderr
            ), mock.patch.object(serve, "ThreadingHTTPServer") as server:
                exit_code = serve.main(
                    [
                        "--queue",
                        str(queue_path),
                        "--workspace",
                        str(workspace_root),
                        "--annotator-id",
                        "fixture-reviewer",
                        "--finalize-reviewed",
                    ]
                )

            self.assertEqual(exit_code, 0, stderr.getvalue())
            server.assert_not_called()
            result = json.loads(stdout.getvalue())
            self.assertEqual(result["finalized_cases"], 1)
            self.assertTrue(result["manifest"]["complete"])
            self.assertTrue(Path(result["review_path"]).is_file())
            self.assertTrue(Path(result["manifest_path"]).is_file())

            reloaded = self.make_workspace(queue_path, workspace_root)
            try:
                self.assertEqual(
                    reloaded.case_detail("case-1")["review"][
                        "path_set_status"
                    ],
                    "closed",
                )
            finally:
                reloaded.close()

    def test_duplicated_path_edit_persists_through_detail_fetch_and_reload(
        self,
    ) -> None:
        original_path = {
            "path_id": "original-path-1",
            "status": "acceptable",
            "surface_reference_id": "surface-0",
            "reading_boundaries": [4],
            "surface_boundaries": [3],
            "alignment_status": "aligned",
            "provenance": {"kind": "human"},
        }
        duplicated_draft = deepcopy(original_path)
        duplicated_draft.update(
            {
                "path_id": "copy-path-1",
                "status": "draft",
                "provenance": {
                    "kind": "human",
                    "source_path_id": original_path["path_id"],
                },
            }
        )
        edited_copy = deepcopy(duplicated_draft)
        edited_copy.update(
            {
                "reading_boundaries": [3, 4],
                "surface_boundaries": [2, 3],
            }
        )
        expected_paths = [original_path, edited_copy]

        def assert_persisted(paths: object) -> None:
            self.assertIsInstance(paths, list)
            assert isinstance(paths, list)
            self.assertEqual(
                [path["path_id"] for path in paths],
                ["original-path-1", "copy-path-1"],
            )
            self.assertEqual(paths[0], original_path)
            self.assertEqual(paths[1], edited_copy)

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            queue_path = root / "queue.jsonl"
            workspace_root = root / "workspace"
            write_queue(
                queue_path,
                [queue_record("case-1"), queue_record("case-2")],
            )
            workspace = self.make_workspace(queue_path, workspace_root)
            try:
                duplicated = workspace.patch_review(
                    "case-1",
                    review_payload(
                        path_set_status="open",
                        paths=[original_path, duplicated_draft],
                    ),
                )
                self.assertEqual(duplicated["revision"], 1)
                self.assertEqual(
                    duplicated["acceptable_paths"],
                    [original_path, duplicated_draft],
                )

                saved = workspace.patch_review(
                    "case-1",
                    review_payload(
                        base_revision=duplicated["revision"],
                        path_set_status="open",
                        paths=expected_paths,
                    ),
                )
                self.assertEqual(saved["revision"], 2)
                assert_persisted(saved["acceptable_paths"])

                self.assertEqual(
                    workspace.case_detail("case-2")["case"]["id"],
                    "case-2",
                )
                navigated_back = workspace.case_detail("case-1")
                self.assertEqual(navigated_back["review"]["revision"], 2)
                assert_persisted(
                    navigated_back["review"]["acceptable_paths"]
                )
            finally:
                workspace.close()

            reloaded = self.make_workspace(queue_path, workspace_root)
            try:
                reloaded_detail = reloaded.case_detail("case-1")
                self.assertEqual(reloaded_detail["review"]["revision"], 2)
                assert_persisted(
                    reloaded_detail["review"]["acceptable_paths"]
                )
            finally:
                reloaded.close()

    def test_corrected_reading_persists_and_export_keeps_source_immutable(self) -> None:
        original_reading = "きよはあめ"
        corrected_reading = "きょうはあめ"
        record = queue_record(
            "case-1",
            reading=original_reading,
            expected_surfaces=["今日は雨"],
            marked_reading="きよは|あめ",
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            queue_path = root / "queue.jsonl"
            workspace_root = root / "workspace"
            write_queue(queue_path, [record])
            workspace = self.make_workspace(queue_path, workspace_root)
            try:
                correction_payload = review_payload(
                    path_set_status="pending",
                    paths=[],
                )
                correction_payload["corrected_reading"] = corrected_reading
                corrected = workspace.patch_review("case-1", correction_payload)
                saved = workspace.patch_review(
                    "case-1",
                    review_payload(
                        base_revision=corrected["revision"],
                        path_set_status="open",
                        paths=[reading_only_path([5])],
                    ),
                )

                self.assertEqual(saved["corrected_reading"], corrected_reading)
                detail = workspace.case_detail("case-1")
                self.assertEqual(detail["case"]["reading"], original_reading)
                self.assertEqual(
                    detail["case"]["annotation_reading"], corrected_reading
                )
                self.assertNotIn("source_reading", detail["case"])
                self.assertEqual(
                    detail["case"]["reading_length"], len(corrected_reading)
                )
                self.assertEqual(
                    detail["case"]["elements"]["unit"],
                    serve.ANNOTATION_READING_UNIT,
                )
                self.assertEqual(
                    detail["review"]["acceptable_paths"][0]["reading_boundaries"],
                    [5],
                )

                exported = json.loads(workspace.export_bytes())
                self.assertEqual(exported["source"]["reading"], original_reading)
                self.assertEqual(
                    exported["source"]["annotation_reading"], corrected_reading
                )
                self.assertEqual(
                    exported["source"]["reading_unit"], serve.ELEMENT_UNIT
                )
                self.assertEqual(
                    exported["source"]["annotation_reading_unit"],
                    serve.ANNOTATION_READING_UNIT,
                )
                self.assertEqual(
                    exported["path_units"],
                    {
                        "reading_boundaries": serve.ANNOTATION_READING_UNIT,
                        "surface_boundaries": serve.SURFACE_UNIT,
                    },
                )
                self.assertNotIn("effective_reading", exported["source"])
                self.assertEqual(
                    exported["review"]["corrected_reading"], corrected_reading
                )
                summary = workspace.list_cases({})["cases"][0]
                self.assertEqual(summary["reading"], corrected_reading)
                self.assertEqual(summary["source_reading"], original_reading)
                snapshot = json.loads(
                    workspace.snapshot_path.read_text(encoding="utf-8")
                )
                self.assertEqual(
                    snapshot["reviews"]["case-1"]["corrected_reading"],
                    corrected_reading,
                )
                event = json.loads(
                    workspace.events_path.read_text(encoding="utf-8")
                    .splitlines()[-1]
                )
                self.assertEqual(
                    event["review"]["corrected_reading"], corrected_reading
                )
            finally:
                workspace.close()

            reloaded = self.make_workspace(queue_path, workspace_root)
            try:
                detail = reloaded.case_detail("case-1")
                self.assertEqual(
                    detail["case"]["reading"], original_reading
                )
                self.assertEqual(
                    detail["case"]["annotation_reading"], corrected_reading
                )
                self.assertEqual(
                    detail["review"]["corrected_reading"], corrected_reading
                )
            finally:
                reloaded.close()

    def test_changing_reading_requires_paths_to_be_cleared_first(self) -> None:
        original_reading = "きよはあめ"
        corrected_reading = "きょうはあめ"
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            queue_path = root / "queue.jsonl"
            write_queue(
                queue_path,
                [
                    queue_record(
                        "case-1",
                        reading=original_reading,
                        expected_surfaces=["今日は雨"],
                        marked_reading="きよは|あめ",
                    )
                ],
            )
            workspace = self.make_workspace(queue_path, root / "workspace")
            try:
                first = workspace.patch_review(
                    "case-1",
                    review_payload(
                        path_set_status="open",
                        paths=[reading_only_path([3])],
                    ),
                )
                stale_paths = review_payload(
                    base_revision=first["revision"],
                    path_set_status="open",
                    paths=deepcopy(first["acceptable_paths"]),
                )
                stale_paths["corrected_reading"] = corrected_reading
                with self.assertRaises(serve.AnnotationError):
                    workspace.patch_review("case-1", stale_paths)

                clear_paths = review_payload(
                    base_revision=first["revision"],
                    path_set_status="pending",
                    paths=[],
                )
                clear_paths["corrected_reading"] = corrected_reading
                corrected = workspace.patch_review("case-1", clear_paths)
                self.assertEqual(corrected["corrected_reading"], corrected_reading)
                self.assertEqual(corrected["acceptable_paths"], [])

                # A client predating corrected_reading must not silently erase it.
                corrected_path = review_payload(
                    base_revision=corrected["revision"],
                    path_set_status="open",
                    paths=[reading_only_path([5])],
                )
                saved_path = workspace.patch_review("case-1", corrected_path)
                self.assertEqual(
                    saved_path["corrected_reading"], corrected_reading
                )
                self.assertEqual(
                    saved_path["acceptable_paths"][0]["reading_boundaries"], [5]
                )
            finally:
                workspace.close()

    def test_v1_snapshot_and_event_without_corrected_reading_still_reload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            queue_path = root / "queue.jsonl"
            workspace_root = root / "workspace"
            write_queue(queue_path, [queue_record("case-1")])
            workspace = self.make_workspace(queue_path, workspace_root)
            workspace.patch_review("case-1", review_payload())
            workspace.close()

            snapshot = json.loads(
                (workspace_root / "review.snapshot.json").read_text(
                    encoding="utf-8"
                )
            )
            snapshot["reviews"]["case-1"].pop("corrected_reading", None)
            (workspace_root / "review.snapshot.json").write_bytes(
                serve.canonical_json_bytes(snapshot)
            )
            events = [
                json.loads(line)
                for line in (workspace_root / "review.events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            for event in events:
                event["review"].pop("corrected_reading", None)
            (workspace_root / "review.events.jsonl").write_bytes(
                canonical_jsonl(events)
            )

            reloaded = self.make_workspace(queue_path, workspace_root)
            try:
                detail = reloaded.case_detail("case-1")
                self.assertIsNone(detail["review"]["corrected_reading"])
                self.assertEqual(
                    detail["case"]["reading"], "きょうはあめ"
                )
                self.assertEqual(
                    detail["case"]["annotation_reading"], "きょうはあめ"
                )
                self.assertNotIn("source_reading", detail["case"])
                self.assertEqual(detail["review"]["revision"], 1)
                exported = json.loads(reloaded.export_bytes())
                self.assertEqual(
                    exported["source"]["reading"], "きょうはあめ"
                )
                self.assertEqual(
                    exported["source"]["annotation_reading"], "きょうはあめ"
                )
                self.assertNotIn("effective_reading", exported["source"])
            finally:
                reloaded.close()

    def test_snapshot_rejects_changed_queue_and_workbook_bindings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            queue_path = root / "queue.jsonl"
            record = queue_record("case-1")
            write_queue(queue_path, [record])

            workbook_one = root / "annotations-one.xlsx"
            write_minimal_xlsx(
                workbook_one,
                [
                    ("使い方", [["fixture"]]),
                    (
                        "アノテーション",
                        [
                            ANNOTATION_HEADERS,
                            workbook_row(record, review_status="未確認"),
                        ],
                    ),
                ],
            )
            workbook_workspace_root = root / "workbook-workspace"
            workspace = self.make_workspace(
                queue_path,
                workbook_workspace_root,
                workbook_path=workbook_one,
            )
            workspace.close()

            workbook_two = root / "annotations-two.xlsx"
            write_minimal_xlsx(
                workbook_two,
                [
                    ("使い方", [["fixture"]]),
                    (
                        "アノテーション",
                        [
                            ANNOTATION_HEADERS,
                            workbook_row(
                                record,
                                review_status="未確認",
                                notes="workbook hash differs",
                            ),
                        ],
                    ),
                ],
            )
            with self.assertRaisesRegex(
                serve.AnnotationError, "different annotation workbook"
            ):
                self.make_workspace(
                    queue_path,
                    workbook_workspace_root,
                    workbook_path=workbook_two,
                )

            queue_workspace_root = root / "queue-workspace"
            workspace = self.make_workspace(queue_path, queue_workspace_root)
            workspace.close()
            changed_record = queue_record(
                "case-1", expected_surfaces=["今日は雨。"]
            )
            write_queue(queue_path, [changed_record])
            with self.assertRaisesRegex(serve.AnnotationError, "different queue"):
                self.make_workspace(queue_path, queue_workspace_root)

    def test_event_journal_rejects_revision_gaps_and_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            queue_path = root / "queue.jsonl"
            write_queue(queue_path, [queue_record("case-1")])

            for name, corrupt_revision in (("gap", 3), ("duplicate", 1)):
                with self.subTest(name=name):
                    workspace_root = root / f"workspace-{name}"
                    workspace = self.make_workspace(queue_path, workspace_root)
                    workspace.patch_review("case-1", review_payload())
                    workspace.patch_review(
                        "case-1",
                        review_payload(base_revision=1, notes="second revision"),
                    )
                    workspace.close()

                    events = [
                        json.loads(line)
                        for line in (workspace_root / "review.events.jsonl")
                        .read_text(encoding="utf-8")
                        .splitlines()
                    ]
                    self.assertEqual(len(events), 2)
                    events[1]["review"]["revision"] = corrupt_revision
                    (workspace_root / "review.events.jsonl").write_bytes(
                        canonical_jsonl(events)
                    )

                    with self.assertRaisesRegex(
                        serve.AnnotationError, "journal revision sequence"
                    ):
                        self.make_workspace(queue_path, workspace_root)

    def test_few_shots_use_human_reading_path_and_skip_adjudication(self) -> None:
        records = [
            queue_record("case-target"),
            queue_record(
                "case-human",
                reading="きょうははれ",
                expected_surfaces=["今日は晴れ"],
                marked_reading="きょうは|はれ",
            ),
            queue_record(
                "case-adjudication",
                reading="きょうはゆき",
                expected_surfaces=["今日は雪"],
                marked_reading="きょうは|ゆき",
            ),
        ]
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            queue_path = root / "queue.jsonl"
            write_queue(queue_path, records)
            workspace = self.make_workspace(queue_path, root / "workspace")
            try:
                workspace.patch_review(
                    "case-human",
                    review_payload(
                        path_set_status="open",
                        paths=[
                            reading_only_path(
                                [3, 4], path_id="human-corrected-path"
                            ),
                            {
                                "path_id": "human-aligned-path",
                                "status": "acceptable",
                                "surface_reference_id": "surface-0",
                                "reading_boundaries": [4],
                                "surface_boundaries": [3],
                                "alignment_status": "aligned",
                                "provenance": {"kind": "human"},
                            },
                        ],
                    ),
                )
                workspace.patch_review(
                    "case-adjudication",
                    review_payload(
                        path_set_status="open",
                        needs_adjudication=True,
                        paths=[
                            reading_only_path(
                                [4], path_id="unsettled-human-path"
                            )
                        ],
                    ),
                )

                examples = workspace._few_shot_examples("case-target")
                self.assertEqual([example["id"] for example in examples], ["case-human"])
                self.assertEqual(
                    examples[0]["human_accepted_paths"],
                    [
                        {
                            "surface_reference_index": 0,
                            "surface": "今日は晴れ",
                            "reading_chunks": ["きょう", "は", "はれ"],
                            "aligned_chunks": None,
                        },
                        {
                            "surface_reference_index": 0,
                            "surface": "今日は晴れ",
                            "reading_chunks": ["きょうは", "はれ"],
                            "aligned_chunks": [
                                {"reading": "きょうは", "surface": "今日は"},
                                {"reading": "はれ", "surface": "晴れ"},
                            ],
                        },
                    ],
                )
                self.assertNotEqual(
                    "|".join(
                        examples[0]["human_accepted_paths"][0]["reading_chunks"]
                    ),
                    records[1]["preannotation"]["marked_reading"],
                )
            finally:
                workspace.close()

    def test_corrected_reading_flows_into_few_shots_and_llm_binding(self) -> None:
        target_original = "きよはあめ"
        target_corrected = "きょうはあめ"
        example_original = "きよははれ"
        example_corrected = "きょうははれ"
        records = [
            queue_record(
                "case-target",
                reading=target_original,
                expected_surfaces=["今日は雨"],
                marked_reading="きよは|あめ",
            ),
            queue_record(
                "case-example",
                reading=example_original,
                expected_surfaces=["今日は晴れ"],
                marked_reading="きよは|はれ",
            ),
        ]
        parsed = {
            "ambiguous": False,
            "ambiguity_reasons": [],
            "candidates": [
                {
                    "surface_reference_index": 0,
                    "chunks": [
                        {"reading": "きょうは", "surface": "今日は"},
                        {"reading": "あめ", "surface": "雨"},
                    ],
                }
            ],
        }
        backend = FakeProposalBackend(parsed)
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            queue_path = root / "queue.jsonl"
            write_queue(queue_path, records)
            workspace = self.make_workspace(
                queue_path,
                root / "workspace",
                proposal_backend=backend,
            )
            try:
                for case_id, corrected_reading in (
                    ("case-target", target_corrected),
                    ("case-example", example_corrected),
                ):
                    correction_payload = review_payload(
                        path_set_status="pending", paths=[]
                    )
                    correction_payload["corrected_reading"] = corrected_reading
                    workspace.patch_review(case_id, correction_payload)

                workspace.patch_review(
                    "case-example",
                    review_payload(
                        base_revision=1,
                        path_set_status="open",
                        paths=[reading_only_path([4])],
                    ),
                )

                examples = workspace._few_shot_examples("case-target")
                self.assertEqual(len(examples), 1)
                self.assertEqual(examples[0]["id"], "case-example")
                self.assertEqual(examples[0]["reading"], example_corrected)
                self.assertEqual(
                    examples[0]["human_accepted_paths"][0]["reading_chunks"],
                    ["きょうは", "はれ"],
                )

                proposal = workspace.generate_proposals("case-target")
                target = json.loads(str(backend.calls[0]["input_text"]))
                self.assertEqual(target["reading"], target_corrected)
                expected_reading_sha = sha256_uri(
                    target_corrected.encode("utf-8")
                )
                self.assertEqual(
                    proposal["effective_reading_sha256"], expected_reading_sha
                )
                self.assertEqual(proposal["review_revision"], 1)
                self.assertEqual(proposal["paths"][0]["reading_boundaries"], [4])

                stored = json.loads(
                    workspace.proposals_path.read_text(encoding="utf-8")
                )
                self.assertEqual(
                    stored["effective_reading_sha256"], expected_reading_sha
                )
                self.assertEqual(stored["review_revision"], 1)
                self.assertEqual(
                    records[0]["source"]["reading"], target_original
                )
            finally:
                workspace.close()

    def test_proposal_queue_is_fifo_nonblocking_and_deduplicates_active_job(
        self,
    ) -> None:
        class BlockingSerialBackend(FakeProposalBackend):
            def __init__(self) -> None:
                super().__init__({})
                self.first_started = threading.Event()
                self.release_first = threading.Event()
                self.readings: list[str] = []

            def generate(
                self,
                *,
                instructions: str,
                input_text: str,
                output_schema: dict[str, object],
                expected_settings_revision: int | None = None,
            ) -> serve.CodexProposalResult:
                target = json.loads(input_text)
                reading = target["reading"]
                surface = target["surface_references"][0]["text"]
                self.readings.append(reading)
                if len(self.readings) == 1:
                    self.first_started.set()
                    if not self.release_first.wait(timeout=5):
                        raise AssertionError(
                            "test did not release proposal backend"
                        )
                self.output = {
                    "ambiguous": False,
                    "ambiguity_reasons": [],
                    "candidates": [
                        {
                            "surface_reference_index": 0,
                            "chunks": [
                                {"reading": reading, "surface": surface}
                            ],
                        }
                    ],
                }
                return super().generate(
                    instructions=instructions,
                    input_text=input_text,
                    output_schema=output_schema,
                    expected_settings_revision=expected_settings_revision,
                )

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            queue_path = root / "queue.jsonl"
            write_queue(
                queue_path,
                [
                    queue_record(
                        "case-1",
                        reading="いち",
                        expected_surfaces=["一"],
                        marked_reading="いち",
                    ),
                    queue_record(
                        "case-2",
                        reading="に",
                        expected_surfaces=["二"],
                        marked_reading="に",
                    ),
                    queue_record(
                        "case-3",
                        reading="さん",
                        expected_surfaces=["三"],
                        marked_reading="さん",
                    ),
                ],
            )
            backend = BlockingSerialBackend()
            workspace = self.make_workspace(
                queue_path,
                root / "workspace",
                proposal_backend=backend,
            )
            manager = serve.ProposalJobManager(workspace)
            try:
                queued = manager.enqueue(
                    ["case-1", "case-2", "case-3"],
                    expected_llm_settings_revision=0,
                    client_request_id="batch-fifo",
                )
                self.assertEqual(queued["enqueued_count"], 3)
                self.assertTrue(backend.first_started.wait(timeout=2))
                active = manager.status()
                self.assertEqual(active["counts"]["running"], 1)
                self.assertEqual(active["counts"]["queued"], 2)

                duplicate = manager.enqueue(
                    ["case-1"],
                    expected_llm_settings_revision=0,
                    client_request_id="batch-duplicate",
                )
                self.assertEqual(duplicate["enqueued_count"], 0)
                self.assertEqual(duplicate["deduplicated_count"], 1)
                replay = manager.enqueue(
                    ["case-1", "case-2", "case-3"],
                    expected_llm_settings_revision=0,
                    client_request_id="batch-fifo",
                )
                self.assertTrue(replay["idempotent"])

                backend.release_first.set()
                completed = self.wait_for_proposal_jobs(
                    manager,
                    lambda status: status["counts"]["succeeded"] == 3,
                )
                self.assertEqual(completed["pending_count"], 0)
                self.assertEqual(backend.readings, ["いち", "に", "さん"])
                for case_id in ("case-1", "case-2", "case-3"):
                    self.assertEqual(
                        len(workspace.case_detail(case_id)["proposals"]), 1
                    )
            finally:
                backend.release_first.set()
                manager.stop()
                workspace.close()
                manager.join()

    def test_proposal_queue_stales_changed_waiter_without_backend_call(
        self,
    ) -> None:
        class BlockingSerialBackend(FakeProposalBackend):
            def __init__(self) -> None:
                super().__init__({})
                self.first_started = threading.Event()
                self.release_first = threading.Event()
                self.readings: list[str] = []

            def generate(
                self,
                *,
                instructions: str,
                input_text: str,
                output_schema: dict[str, object],
                expected_settings_revision: int | None = None,
            ) -> serve.CodexProposalResult:
                target = json.loads(input_text)
                reading = target["reading"]
                surface = target["surface_references"][0]["text"]
                self.readings.append(reading)
                if len(self.readings) == 1:
                    self.first_started.set()
                    if not self.release_first.wait(timeout=5):
                        raise AssertionError(
                            "test did not release proposal backend"
                        )
                self.output = {
                    "ambiguous": False,
                    "ambiguity_reasons": [],
                    "candidates": [
                        {
                            "surface_reference_index": 0,
                            "chunks": [
                                {"reading": reading, "surface": surface}
                            ],
                        }
                    ],
                }
                return super().generate(
                    instructions=instructions,
                    input_text=input_text,
                    output_schema=output_schema,
                    expected_settings_revision=expected_settings_revision,
                )

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            queue_path = root / "queue.jsonl"
            write_queue(
                queue_path,
                [
                    queue_record(
                        "case-1",
                        reading="いち",
                        expected_surfaces=["一"],
                        marked_reading="いち",
                    ),
                    queue_record(
                        "case-2",
                        reading="に",
                        expected_surfaces=["二"],
                        marked_reading="に",
                    ),
                    queue_record(
                        "case-3",
                        reading="さん",
                        expected_surfaces=["三"],
                        marked_reading="さん",
                    ),
                ],
            )
            backend = BlockingSerialBackend()
            workspace = self.make_workspace(
                queue_path,
                root / "workspace",
                proposal_backend=backend,
            )
            manager = serve.ProposalJobManager(workspace)
            try:
                manager.enqueue(
                    ["case-1", "case-2", "case-3"],
                    expected_llm_settings_revision=0,
                    client_request_id="batch-stale",
                )
                self.assertTrue(backend.first_started.wait(timeout=2))
                workspace.patch_review(
                    "case-2",
                    review_payload(
                        path_set_status="pending",
                        paths=[],
                        notes="changed while queued",
                    ),
                )
                backend.release_first.set()
                completed = self.wait_for_proposal_jobs(
                    manager,
                    lambda status: sum(
                        status["counts"][name]
                        for name in ("succeeded", "stale", "failed")
                    )
                    == 3,
                )
                by_case = {
                    job["case_id"]: job for job in completed["jobs"]
                }
                self.assertEqual(by_case["case-1"]["status"], "succeeded")
                self.assertEqual(by_case["case-2"]["status"], "stale")
                self.assertEqual(by_case["case-3"]["status"], "succeeded")
                self.assertEqual(backend.readings, ["いち", "さん"])
                self.assertEqual(
                    workspace.case_detail("case-2")["proposals"], []
                )
            finally:
                backend.release_first.set()
                manager.stop()
                workspace.close()
                manager.join()

    def test_proposal_queue_continues_after_one_backend_failure(self) -> None:
        class FailOnceBackend(FakeProposalBackend):
            def __init__(self) -> None:
                super().__init__({})
                self.readings: list[str] = []

            def generate(
                self,
                *,
                instructions: str,
                input_text: str,
                output_schema: dict[str, object],
                expected_settings_revision: int | None = None,
            ) -> serve.CodexProposalResult:
                target = json.loads(input_text)
                reading = target["reading"]
                self.readings.append(reading)
                if len(self.readings) == 1:
                    raise serve.CodexAppServerTimeout("fixture timeout")
                surface = target["surface_references"][0]["text"]
                self.output = {
                    "ambiguous": False,
                    "ambiguity_reasons": [],
                    "candidates": [
                        {
                            "surface_reference_index": 0,
                            "chunks": [
                                {"reading": reading, "surface": surface}
                            ],
                        }
                    ],
                }
                return super().generate(
                    instructions=instructions,
                    input_text=input_text,
                    output_schema=output_schema,
                    expected_settings_revision=expected_settings_revision,
                )

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            queue_path = root / "queue.jsonl"
            write_queue(
                queue_path,
                [
                    queue_record(
                        "case-1",
                        reading="いち",
                        expected_surfaces=["一"],
                        marked_reading="いち",
                    ),
                    queue_record(
                        "case-2",
                        reading="に",
                        expected_surfaces=["二"],
                        marked_reading="に",
                    ),
                ],
            )
            backend = FailOnceBackend()
            workspace = self.make_workspace(
                queue_path,
                root / "workspace",
                proposal_backend=backend,
            )
            manager = serve.ProposalJobManager(workspace)
            try:
                manager.enqueue(
                    ["case-1", "case-2"],
                    expected_llm_settings_revision=0,
                    client_request_id="batch-failure",
                )
                completed = self.wait_for_proposal_jobs(
                    manager,
                    lambda status: (
                        status["counts"]["failed"] == 1
                        and status["counts"]["succeeded"] == 1
                    ),
                )
                by_case = {
                    job["case_id"]: job for job in completed["jobs"]
                }
                self.assertEqual(by_case["case-1"]["status"], "failed")
                self.assertEqual(
                    by_case["case-1"]["error"]["code"], "timeout"
                )
                self.assertTrue(
                    by_case["case-1"]["error"]["retryable"]
                )
                self.assertEqual(by_case["case-2"]["status"], "succeeded")
                self.assertEqual(backend.readings, ["いち", "に"])
            finally:
                manager.stop()
                workspace.close()
                manager.join()

    def test_proposal_queue_locks_settings_until_active_jobs_finish(
        self,
    ) -> None:
        class BlockingBackend(FakeProposalBackend):
            def __init__(self) -> None:
                super().__init__({})
                self.started = threading.Event()
                self.release = threading.Event()

            def generate(
                self,
                *,
                instructions: str,
                input_text: str,
                output_schema: dict[str, object],
                expected_settings_revision: int | None = None,
            ) -> serve.CodexProposalResult:
                target = json.loads(input_text)
                self.started.set()
                if not self.release.wait(timeout=5):
                    raise AssertionError("test did not release proposal backend")
                self.output = {
                    "ambiguous": False,
                    "ambiguity_reasons": [],
                    "candidates": [
                        {
                            "surface_reference_index": 0,
                            "chunks": [
                                {
                                    "reading": target["reading"],
                                    "surface": target["surface_references"][0][
                                        "text"
                                    ],
                                }
                            ],
                        }
                    ],
                }
                return super().generate(
                    instructions=instructions,
                    input_text=input_text,
                    output_schema=output_schema,
                    expected_settings_revision=expected_settings_revision,
                )

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            queue_path = root / "queue.jsonl"
            write_queue(queue_path, [queue_record("case-1")])
            backend = BlockingBackend()
            workspace = self.make_workspace(
                queue_path,
                root / "workspace",
                proposal_backend=backend,
            )
            manager = serve.ProposalJobManager(workspace)
            try:
                manager.enqueue(
                    ["case-1"],
                    expected_llm_settings_revision=0,
                    client_request_id="batch-settings-lock",
                )
                self.assertTrue(backend.started.wait(timeout=2))
                settings_payload = {
                    "base_revision": 0,
                    "model": "fixture-next-model",
                    "effort": "high",
                }
                with self.assertRaisesRegex(
                    serve.CodexAppServerBusy, "cannot change"
                ):
                    manager.patch_llm_settings(settings_payload)
                backend.release.set()
                self.wait_for_proposal_jobs(
                    manager,
                    lambda status: status["counts"]["succeeded"] == 1,
                )
                llm = manager.patch_llm_settings(settings_payload)
                self.assertEqual(llm["settings_revision"], 1)
                self.assertEqual(llm["model"], "fixture-next-model")
            finally:
                backend.release.set()
                manager.stop()
                workspace.close()
                manager.join()

    def test_saved_proposal_remains_succeeded_when_queue_stops(
        self,
    ) -> None:
        output = {
            "ambiguous": False,
            "ambiguity_reasons": [],
            "candidates": [
                {
                    "surface_reference_index": 0,
                    "chunks": [
                        {"reading": "きょうは", "surface": "今日は"},
                        {"reading": "あめ", "surface": "雨"},
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            queue_path = root / "queue.jsonl"
            write_queue(queue_path, [queue_record("case-1")])
            workspace = self.make_workspace(
                queue_path,
                root / "workspace",
                proposal_backend=FakeProposalBackend(output),
            )
            manager = serve.ProposalJobManager(workspace)
            proposal_saved = threading.Event()
            allow_worker_return = threading.Event()
            original_generate = workspace.generate_proposals

            def delayed_return(*args: object, **kwargs: object) -> dict[str, object]:
                proposal = original_generate(*args, **kwargs)
                proposal_saved.set()
                if not allow_worker_return.wait(timeout=5):
                    raise AssertionError("test did not release proposal worker")
                return proposal

            try:
                with mock.patch.object(
                    workspace,
                    "generate_proposals",
                    side_effect=delayed_return,
                ):
                    manager.enqueue(
                        ["case-1"],
                        expected_llm_settings_revision=0,
                        client_request_id="batch-stop-after-save",
                    )
                    self.assertTrue(proposal_saved.wait(timeout=2))
                    manager.stop()
                    self.assertFalse(manager.join(timeout=0.01))
                    allow_worker_return.set()
                    completed = self.wait_for_proposal_jobs(
                        manager,
                        lambda status: status["counts"]["succeeded"] == 1,
                    )
                    self.assertEqual(
                        completed["jobs"][0]["status"], "succeeded"
                    )
                    self.assertTrue(manager.join(timeout=1))
                    self.assertEqual(
                        len(workspace.case_detail("case-1")["proposals"]), 1
                    )
            finally:
                allow_worker_return.set()
                manager.stop()
                workspace.close()
                manager.join()

    def test_proposal_survives_review_save_until_effective_reading_changes(
        self,
    ) -> None:
        parsed = {
            "ambiguous": False,
            "ambiguity_reasons": [],
            "candidates": [
                {
                    "surface_reference_index": 0,
                    "chunks": [
                        {"reading": "きょうは", "surface": "今日は"},
                        {"reading": "あめ", "surface": "雨"},
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            queue_path = root / "queue.jsonl"
            workspace_root = root / "workspace"
            write_queue(queue_path, [queue_record("case-1")])

            workspace = self.make_workspace(
                queue_path,
                workspace_root,
                proposal_backend=FakeProposalBackend(parsed),
            )
            try:
                proposal = workspace.generate_proposals("case-1")
                self.assertEqual(proposal["review_revision"], 0)

                accepted_path = deepcopy(proposal["paths"][0])
                accepted_path["status"] = "acceptable"
                saved = workspace.patch_review(
                    "case-1",
                    review_payload(
                        base_revision=0,
                        path_set_status="open",
                        paths=[accepted_path],
                    ),
                )
                self.assertEqual(saved["revision"], 1)
                detail = workspace.case_detail("case-1")
                self.assertEqual(
                    [item["proposal_id"] for item in detail["proposals"]],
                    [proposal["proposal_id"]],
                )
                self.assertEqual(
                    workspace.list_cases({})["cases"][0]["proposal_count"], 1
                )
            finally:
                workspace.close()

            reloaded = self.make_workspace(queue_path, workspace_root)
            try:
                detail = reloaded.case_detail("case-1")
                self.assertEqual(
                    [item["proposal_id"] for item in detail["proposals"]],
                    [proposal["proposal_id"]],
                )
                self.assertEqual(
                    reloaded.list_cases({})["cases"][0]["proposal_count"], 1
                )

                correction_payload = review_payload(
                    base_revision=1,
                    path_set_status="pending",
                    paths=[],
                )
                correction_payload["corrected_reading"] = "きょうはあめです"
                corrected = reloaded.patch_review("case-1", correction_payload)
                self.assertEqual(corrected["revision"], 2)
                self.assertEqual(
                    reloaded.case_detail("case-1")["proposals"], []
                )
                self.assertEqual(
                    reloaded.list_cases({})["cases"][0]["proposal_count"], 0
                )
            finally:
                reloaded.close()

            reloaded_again = self.make_workspace(queue_path, workspace_root)
            try:
                self.assertEqual(
                    reloaded_again.case_detail("case-1")["proposals"], []
                )
                self.assertEqual(
                    reloaded_again.list_cases({})["cases"][0]["proposal_count"],
                    0,
                )
            finally:
                reloaded_again.close()

    def test_correction_while_llm_is_running_discards_stale_proposal(self) -> None:
        parsed = {
            "ambiguous": False,
            "ambiguity_reasons": [],
            "candidates": [
                {
                    "surface_reference_index": 0,
                    "chunks": [
                        {"reading": "きよは", "surface": "今日は"},
                        {"reading": "あめ", "surface": "雨"},
                    ],
                }
            ],
        }

        class BlockingProposalBackend(FakeProposalBackend):
            def __init__(self) -> None:
                super().__init__(parsed)
                self.started = threading.Event()
                self.release = threading.Event()

            def generate(
                self,
                *,
                instructions: str,
                input_text: str,
                output_schema: dict[str, object],
                expected_settings_revision: int | None = None,
            ) -> serve.CodexProposalResult:
                self.started.set()
                if not self.release.wait(timeout=5):
                    raise AssertionError("test did not release proposal backend")
                return super().generate(
                    instructions=instructions,
                    input_text=input_text,
                    output_schema=output_schema,
                    expected_settings_revision=expected_settings_revision,
                )

        backend = BlockingProposalBackend()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            queue_path = root / "queue.jsonl"
            write_queue(
                queue_path,
                [
                    queue_record(
                        "case-1",
                        reading="きよはあめ",
                        expected_surfaces=["今日は雨"],
                        marked_reading="きよは|あめ",
                    )
                ],
            )
            workspace = self.make_workspace(
                queue_path,
                root / "workspace",
                proposal_backend=backend,
            )
            outcomes: list[object] = []

            def generate() -> None:
                try:
                    outcomes.append(workspace.generate_proposals("case-1"))
                except BaseException as exc:  # Test captures the worker outcome.
                    outcomes.append(exc)

            worker = threading.Thread(target=generate, daemon=True)
            try:
                worker.start()
                self.assertTrue(backend.started.wait(timeout=2))
                correction_payload = review_payload(
                    path_set_status="pending", paths=[]
                )
                correction_payload["corrected_reading"] = "きょうはあめ"
                workspace.patch_review("case-1", correction_payload)
                backend.release.set()
                worker.join(timeout=5)

                self.assertFalse(worker.is_alive())
                self.assertEqual(len(outcomes), 1)
                self.assertIsInstance(outcomes[0], serve.CodexAppServerError)
                self.assertEqual(workspace.case_detail("case-1")["proposals"], [])
                self.assertFalse(workspace.proposals_path.exists())
            finally:
                backend.release.set()
                worker.join(timeout=5)
                workspace.close()

    def test_legacy_proposal_binding_is_original_reading_only(self) -> None:
        parsed = {
            "ambiguous": False,
            "ambiguity_reasons": [],
            "candidates": [
                {
                    "surface_reference_index": 0,
                    "chunks": [
                        {"reading": "きよは", "surface": "今日は"},
                        {"reading": "あめ", "surface": "雨"},
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            queue_path = root / "queue.jsonl"
            workspace_root = root / "workspace"
            write_queue(
                queue_path,
                [
                    queue_record(
                        "case-1",
                        reading="きよはあめ",
                        expected_surfaces=["今日は雨"],
                        marked_reading="きよは|あめ",
                    )
                ],
            )
            workspace = self.make_workspace(
                queue_path,
                workspace_root,
                proposal_backend=FakeProposalBackend(parsed),
            )
            workspace.generate_proposals("case-1")
            workspace.close()

            proposals = [
                json.loads(line)
                for line in (workspace_root / "proposals.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            proposals[0].pop("effective_reading_sha256", None)
            proposals[0].pop("review_revision", None)
            proposals[0].pop("discarded_candidates", None)
            (workspace_root / "proposals.jsonl").write_bytes(
                canonical_jsonl(proposals)
            )

            reloaded = self.make_workspace(queue_path, workspace_root)
            try:
                legacy_proposals = reloaded.case_detail("case-1")["proposals"]
                self.assertEqual(len(legacy_proposals), 1)
                self.assertEqual(
                    legacy_proposals[0]["discarded_candidate_count"], 0
                )
                correction_payload = review_payload(
                    path_set_status="pending", paths=[]
                )
                correction_payload["corrected_reading"] = "きょうはあめ"
                reloaded.patch_review("case-1", correction_payload)
                self.assertEqual(
                    reloaded.case_detail("case-1")["proposals"], []
                )
            finally:
                reloaded.close()

            reloaded_again = self.make_workspace(queue_path, workspace_root)
            try:
                self.assertEqual(
                    reloaded_again.case_detail("case-1")["proposals"], []
                )
            finally:
                reloaded_again.close()

    def test_codex_backend_receives_contract_and_journals_validated_result(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            queue_path = root / "queue.jsonl"
            write_queue(queue_path, [queue_record("case-1")])
            parsed = {
                "ambiguous": True,
                "ambiguity_reasons": ["助詞を独立させる経路も自然"],
                "candidates": [
                    {
                        "surface_reference_index": 0,
                        "chunks": [
                            {"reading": "きょうは", "surface": "今日は"},
                            {"reading": "あめ", "surface": "雨"},
                        ],
                    },
                    {
                        "surface_reference_index": 0,
                        "chunks": [
                            {"reading": "きょう", "surface": "今日"},
                            {"reading": "は", "surface": "は"},
                            {"reading": "あめ", "surface": "雨"},
                        ],
                    },
                ],
            }
            backend = FakeProposalBackend(parsed)
            workspace = self.make_workspace(
                queue_path,
                root / "workspace",
                proposal_backend=backend,
            )
            try:
                proposal = workspace.generate_proposals("case-1")
                self.assertEqual(len(backend.calls), 1)
                call = backend.calls[0]
                self.assertIn("日本語IMEのチャンク", call["instructions"])
                target = json.loads(str(call["input_text"]))
                self.assertEqual(target["reading"], "きょうはあめ")
                self.assertEqual(
                    target["surface_references"],
                    [{"index": 0, "text": "今日は雨"}],
                )
                self.assertEqual(target["few_shot_examples"], [])
                expected_schema = workspace._proposal_json_schema(1)
                self.assertEqual(call["output_schema"], expected_schema)
                self.assertEqual(
                    call["output_schema"]["properties"]["candidates"]["items"]
                    ["properties"]["surface_reference_index"]["maximum"],
                    0,
                )

                self.assertTrue(proposal["ambiguous"])
                self.assertEqual(len(proposal["paths"]), 2)
                self.assertEqual(proposal["paths"][0]["reading_boundaries"], [4])
                self.assertEqual(proposal["paths"][0]["surface_boundaries"], [3])
                self.assertEqual(proposal["paths"][0]["status"], "draft")
                self.assertEqual(
                    proposal["paths"][1]["reading_boundaries"], [3, 4]
                )
                self.assertEqual(
                    proposal["paths"][1]["surface_boundaries"], [2, 3]
                )

                journal = [
                    json.loads(line)
                    for line in workspace.proposals_path.read_text(
                        encoding="utf-8"
                    ).splitlines()
                ]
                self.assertEqual(len(journal), 1)
                stored = journal[0]
                self.assertEqual(stored["raw_output"], parsed)
                self.assertEqual(stored["generator"]["provider"], "codex-app-server")
                self.assertEqual(stored["generator"]["model"], "fixture-actual-model")
                self.assertEqual(
                    stored["generator"]["model_provider"], "fixture-provider"
                )
                self.assertEqual(
                    stored["generator"]["requested_model"],
                    "fixture-requested-model",
                )
                self.assertEqual(
                    stored["generator"]["app_server_user_agent"],
                    "fixture-app-server/1",
                )
                self.assertEqual(
                    stored["generator"]["codex_thread_id"], "thread-fixture"
                )
                self.assertEqual(
                    stored["generator"]["codex_turn_id"], "turn-fixture"
                )
                self.assertEqual(
                    stored["generator"]["codex_message_id"], "message-fixture"
                )
                self.assertEqual(stored["generator"]["turn_duration_ms"], 42)
                self.assertEqual(
                    stored["generator"]["reasoning_effort"], "medium"
                )
                self.assertTrue(stored["generator"]["ephemeral"])
                self.assertEqual(stored["generator"]["sandbox"], "read-only")
                self.assertEqual(stored["generator"]["approval_policy"], "never")
            finally:
                workspace.close()
            self.assertTrue(backend.closed)

    def test_codex_failure_or_invalid_output_does_not_append_journal(self) -> None:
        valid_output = {
            "ambiguous": False,
            "ambiguity_reasons": [],
            "candidates": [
                {
                    "surface_reference_index": 0,
                    "chunks": [
                        {"reading": "きょうは", "surface": "今日は"},
                        {"reading": "あめ", "surface": "雨"},
                    ],
                }
            ],
        }
        invalid_output = deepcopy(valid_output)
        invalid_output["candidates"][0]["chunks"][0]["reading"] = "あしたは"

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            queue_path = root / "queue.jsonl"
            write_queue(queue_path, [queue_record("case-1")])
            cases = [
                (
                    "backend-failure",
                    FakeProposalBackend(
                        failure=serve.CodexAppServerError("fixture failure")
                    ),
                    serve.CodexAppServerError,
                    "fixture failure",
                ),
                (
                    "semantic-failure",
                    FakeProposalBackend(invalid_output),
                    serve.CodexAppServerError,
                    "failed semantic validation",
                ),
            ]
            for name, backend, error_type, message in cases:
                with self.subTest(name=name):
                    workspace = self.make_workspace(
                        queue_path,
                        root / name,
                        proposal_backend=backend,
                    )
                    try:
                        with self.assertRaisesRegex(error_type, message) as raised:
                            workspace.generate_proposals("case-1")
                        if name == "semantic-failure":
                            self.assertIs(
                                type(raised.exception),
                                serve.CodexAppServerError,
                            )
                            self.assertIsInstance(
                                raised.exception.__cause__, serve.AnnotationError
                            )
                        self.assertEqual(len(backend.calls), 1)
                        self.assertEqual(workspace.proposals["case-1"], [])
                        self.assertFalse(workspace.proposals_path.exists())
                    finally:
                        workspace.close()

    def test_semantic_validation_retains_valid_candidates_when_later_candidate_is_invalid(
        self,
    ) -> None:
        parsed = {
            "ambiguous": True,
            "ambiguity_reasons": ["助詞を独立させる経路も自然"],
            "candidates": [
                {
                    "surface_reference_index": 0,
                    "chunks": [
                        {"reading": "きょうは", "surface": "今日は"},
                        {"reading": "あめ", "surface": "雨"},
                    ],
                },
                {
                    "surface_reference_index": 0,
                    "chunks": [
                        {"reading": "きょう", "surface": "今日"},
                        {"reading": "は", "surface": "は"},
                        {"reading": "あめ", "surface": "雨"},
                    ],
                },
                {
                    "surface_reference_index": 0,
                    "chunks": [
                        {"reading": "きょうは", "surface": "今日は"},
                        {"reading": "ゆき", "surface": "雨"},
                    ],
                },
            ],
        }
        backend = FakeProposalBackend(parsed)
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            queue_path = root / "queue.jsonl"
            workspace_root = root / "workspace"
            write_queue(queue_path, [queue_record("case-1")])
            workspace = self.make_workspace(
                queue_path,
                workspace_root,
                proposal_backend=backend,
            )
            try:
                proposal = workspace.generate_proposals("case-1")

                self.assertEqual(len(proposal["paths"]), 2)
                self.assertEqual(proposal["discarded_candidate_count"], 1)
                self.assertEqual(
                    [path["reading_boundaries"] for path in proposal["paths"]],
                    [[4], [3, 4]],
                )
                self.assertEqual(
                    [path["surface_boundaries"] for path in proposal["paths"]],
                    [[3], [2, 3]],
                )
                self.assertTrue(
                    proposal["paths"][0]["path_id"].endswith("-path-1")
                )
                self.assertTrue(
                    proposal["paths"][1]["path_id"].endswith("-path-2")
                )
                stored = json.loads(
                    workspace.proposals_path.read_text(encoding="utf-8")
                )
                self.assertEqual(stored["raw_output"], parsed)
                self.assertEqual(len(stored["paths"]), 2)
                self.assertEqual(
                    stored["discarded_candidates"],
                    [
                        {
                            "rank": 3,
                            "reason": (
                                "LLM candidate 3 reading chunks do not cover "
                                "the source"
                            ),
                        }
                    ],
                )
            finally:
                workspace.close()

            reloaded = self.make_workspace(queue_path, workspace_root)
            try:
                proposals = reloaded.case_detail("case-1")["proposals"]
                self.assertEqual(len(proposals), 1)
                self.assertEqual(proposals[0]["discarded_candidate_count"], 1)
            finally:
                reloaded.close()

    def test_proposal_journal_rejects_malformed_discarded_candidates(
        self,
    ) -> None:
        parsed = {
            "ambiguous": False,
            "ambiguity_reasons": [],
            "candidates": [
                {
                    "surface_reference_index": 0,
                    "chunks": [
                        {"reading": "きょうは", "surface": "今日は"},
                        {"reading": "あめ", "surface": "雨"},
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            queue_path = root / "queue.jsonl"
            workspace_root = root / "workspace"
            write_queue(queue_path, [queue_record("case-1")])
            workspace = self.make_workspace(
                queue_path,
                workspace_root,
                proposal_backend=FakeProposalBackend(parsed),
            )
            workspace.generate_proposals("case-1")
            workspace.close()

            proposal = json.loads(
                (workspace_root / "proposals.jsonl").read_text(encoding="utf-8")
            )
            proposal["discarded_candidates"] = None
            (workspace_root / "proposals.jsonl").write_bytes(
                canonical_jsonl([proposal])
            )
            with self.assertRaisesRegex(
                serve.AnnotationError,
                "discarded_candidates must be an array",
            ):
                self.make_workspace(queue_path, workspace_root)

    def test_semantic_validation_rejects_output_when_every_candidate_is_invalid(
        self,
    ) -> None:
        parsed = {
            "ambiguous": True,
            "ambiguity_reasons": ["候補を再検討する必要がある"],
            "candidates": [
                {
                    "surface_reference_index": 0,
                    "chunks": [
                        {"reading": "あしたは", "surface": "今日は"},
                        {"reading": "あめ", "surface": "雨"},
                    ],
                },
                {
                    "surface_reference_index": 0,
                    "chunks": [
                        {"reading": "きょう", "surface": "今日"},
                        {"reading": "の", "surface": "は"},
                        {"reading": "あめ", "surface": "雨"},
                    ],
                },
                {
                    "surface_reference_index": 0,
                    "chunks": [
                        {"reading": "きょうは", "surface": "今日は"},
                        {"reading": "ゆき", "surface": "雨"},
                    ],
                },
            ],
        }
        backend = FakeProposalBackend(parsed)
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            queue_path = root / "queue.jsonl"
            write_queue(queue_path, [queue_record("case-1")])
            workspace = self.make_workspace(
                queue_path,
                root / "workspace",
                proposal_backend=backend,
            )
            try:
                with self.assertRaisesRegex(
                    serve.CodexAppServerError,
                    "failed semantic validation",
                ) as raised:
                    workspace.generate_proposals("case-1")

                self.assertIsInstance(raised.exception.__cause__, serve.AnnotationError)
                self.assertIn(
                    "LLM output contains no valid candidates",
                    str(raised.exception.__cause__),
                )
                self.assertEqual(workspace.proposals["case-1"], [])
                self.assertFalse(workspace.proposals_path.exists())
            finally:
                workspace.close()

    def test_llm_settings_change_applies_to_next_generation_and_audit(self) -> None:
        parsed = {
            "ambiguous": False,
            "ambiguity_reasons": [],
            "candidates": [
                {
                    "surface_reference_index": 0,
                    "chunks": [
                        {"reading": "きょうは", "surface": "今日は"},
                        {"reading": "あめ", "surface": "雨"},
                    ],
                }
            ],
        }

        class SnapshotBlockingBackend(FakeProposalBackend):
            def __init__(self) -> None:
                super().__init__(parsed)
                self.started = threading.Event()
                self.release = threading.Event()

            def generate(
                self,
                *,
                instructions: str,
                input_text: str,
                output_schema: dict[str, object],
                expected_settings_revision: int | None = None,
            ) -> serve.CodexProposalResult:
                if (
                    expected_settings_revision is not None
                    and self.settings_revision != expected_settings_revision
                ):
                    raise serve.CodexAppServerStale(
                        "fixture settings revision changed"
                    )
                requested_model = self.model
                reasoning_effort = self.effort
                settings_revision = self.settings_revision
                self.calls.append(
                    {
                        "instructions": instructions,
                        "input_text": input_text,
                        "output_schema": deepcopy(output_schema),
                        "requested_model": requested_model,
                        "reasoning_effort": reasoning_effort,
                    }
                )
                if len(self.calls) == 1:
                    self.started.set()
                    if not self.release.wait(timeout=5):
                        raise AssertionError("test did not release proposal backend")
                return serve.CodexProposalResult(
                    output=deepcopy(parsed),
                    model="fixture-actual-model",
                    model_provider="fixture-provider",
                    requested_model=requested_model,
                    reasoning_effort=reasoning_effort,
                    settings_revision=settings_revision,
                    app_server_user_agent="fixture-app-server/1",
                    thread_id="thread-fixture",
                    turn_id="turn-fixture",
                    message_id="message-fixture",
                    duration_ms=42,
                )

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            queue_path = root / "queue.jsonl"
            write_queue(queue_path, [queue_record("case-1")])
            backend = SnapshotBlockingBackend()
            workspace = self.make_workspace(
                queue_path,
                root / "workspace",
                proposal_backend=backend,
            )
            outcomes: list[object] = []

            def generate() -> None:
                try:
                    outcomes.append(
                        workspace.generate_proposals(
                            "case-1",
                            expected_llm_settings_revision=0,
                        )
                    )
                except BaseException as exc:
                    outcomes.append(exc)

            worker = threading.Thread(target=generate, daemon=True)
            try:
                worker.start()
                self.assertTrue(backend.started.wait(timeout=2))
                workspace.patch_llm_settings(
                    {
                        "base_revision": 0,
                        "model": "fixture-next-model",
                        "effort": "high",
                    }
                )
                backend.release.set()
                worker.join(timeout=5)
                self.assertFalse(worker.is_alive())
                self.assertEqual(len(outcomes), 1)
                self.assertIsInstance(outcomes[0], dict)

                workspace.generate_proposals(
                    "case-1", expected_llm_settings_revision=1
                )
                journal = [
                    json.loads(line)
                    for line in workspace.proposals_path.read_text(
                        encoding="utf-8"
                    ).splitlines()
                ]
                self.assertEqual(len(journal), 2)
                self.assertEqual(
                    journal[0]["generator"]["requested_model"],
                    "fixture-requested-model",
                )
                self.assertEqual(
                    journal[0]["generator"]["reasoning_effort"], "medium"
                )
                self.assertEqual(
                    journal[0]["generator"]["settings_revision"], 0
                )
                self.assertEqual(
                    journal[1]["generator"]["requested_model"],
                    "fixture-next-model",
                )
                self.assertEqual(
                    journal[1]["generator"]["reasoning_effort"], "high"
                )
                self.assertEqual(
                    journal[1]["generator"]["settings_revision"], 1
                )
            finally:
                backend.release.set()
                worker.join(timeout=5)
                workspace.close()

    def test_proposal_rejects_settings_revision_changed_before_snapshot(
        self,
    ) -> None:
        parsed = {
            "ambiguous": False,
            "ambiguity_reasons": [],
            "candidates": [
                {
                    "surface_reference_index": 0,
                    "chunks": [
                        {"reading": "きょうは", "surface": "今日は"},
                        {"reading": "あめ", "surface": "雨"},
                    ],
                }
            ],
        }

        class BeforeSnapshotBlockingBackend(FakeProposalBackend):
            def __init__(self) -> None:
                super().__init__(parsed)
                self.before_snapshot = threading.Event()
                self.release = threading.Event()

            def generate(
                self,
                *,
                instructions: str,
                input_text: str,
                output_schema: dict[str, object],
                expected_settings_revision: int | None = None,
            ) -> serve.CodexProposalResult:
                self.before_snapshot.set()
                if not self.release.wait(timeout=5):
                    raise AssertionError("test did not release proposal backend")
                return super().generate(
                    instructions=instructions,
                    input_text=input_text,
                    output_schema=output_schema,
                    expected_settings_revision=expected_settings_revision,
                )

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            queue_path = root / "queue.jsonl"
            write_queue(queue_path, [queue_record("case-1")])
            backend = BeforeSnapshotBlockingBackend()
            workspace = self.make_workspace(
                queue_path,
                root / "workspace",
                proposal_backend=backend,
            )
            outcomes: list[object] = []

            def generate() -> None:
                try:
                    outcomes.append(
                        workspace.generate_proposals(
                            "case-1",
                            expected_llm_settings_revision=0,
                        )
                    )
                except BaseException as exc:
                    outcomes.append(exc)

            worker = threading.Thread(target=generate, daemon=True)
            try:
                worker.start()
                self.assertTrue(backend.before_snapshot.wait(timeout=2))
                workspace.patch_llm_settings(
                    {
                        "base_revision": 0,
                        "model": "fixture-next-model",
                        "effort": "high",
                    }
                )
                backend.release.set()
                worker.join(timeout=5)
                self.assertFalse(worker.is_alive())
                self.assertEqual(len(outcomes), 1)
                self.assertIsInstance(
                    outcomes[0], serve.CodexAppServerStale
                )
                self.assertEqual(workspace.proposals["case-1"], [])
                self.assertFalse(workspace.proposals_path.exists())
            finally:
                backend.release.set()
                worker.join(timeout=5)
                workspace.close()

    def test_meta_reports_codex_backend_availability(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            queue_path = root / "queue.jsonl"
            write_queue(queue_path, [queue_record("case-1")])

            disabled = self.make_workspace(
                queue_path,
                root / "disabled",
                proposal_backend_message="fixture Codex unavailable",
            )
            try:
                llm = disabled.meta()["llm"]
                self.assertFalse(llm["enabled"])
                self.assertFalse(llm["configured"])
                self.assertEqual(llm["provider"], "codex-app-server")
                self.assertEqual(llm["status"], "unavailable")
                self.assertEqual(llm["message"], "fixture Codex unavailable")
            finally:
                disabled.close()

            backend = FakeProposalBackend(
                {
                    "ambiguous": False,
                    "ambiguity_reasons": [],
                    "candidates": [],
                }
            )
            enabled = self.make_workspace(
                queue_path,
                root / "enabled",
                proposal_backend=backend,
            )
            try:
                llm = enabled.meta()["llm"]
                self.assertTrue(llm["enabled"])
                self.assertTrue(llm["configured"])
                self.assertEqual(llm["provider"], "codex-app-server")
                self.assertEqual(llm["status"], "ready")
                self.assertEqual(llm["model"], "fixture-requested-model")
                self.assertEqual(llm["effort"], "medium")
                self.assertEqual(llm["settings_revision"], 0)
            finally:
                enabled.close()

    def test_llm_settings_persist_and_reject_stale_or_foreign_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            queue_path = root / "queue.jsonl"
            workspace_root = root / "workspace"
            write_queue(queue_path, [queue_record("case-1")])
            backend = FakeProposalBackend()
            workspace = self.make_workspace(
                queue_path,
                workspace_root,
                proposal_backend=backend,
            )
            try:
                updated = workspace.patch_llm_settings(
                    {
                        "base_revision": 0,
                        "model": None,
                        "effort": "high",
                    }
                )
                self.assertEqual(updated["settings_revision"], 1)
                self.assertIsNone(updated["model"])
                self.assertEqual(updated["effort"], "high")
                self.assertIsNone(backend.model)
                self.assertEqual(backend.effort, "high")
                with self.assertRaises(serve.RevisionConflict):
                    workspace.patch_llm_settings(
                        {
                            "base_revision": 0,
                            "model": "stale-model",
                            "effort": "low",
                        }
                    )
                with self.assertRaises(serve.RevisionConflict):
                    workspace.patch_llm_settings(
                        {
                            "base_revision": 0,
                            "model": None,
                            "effort": "high",
                        }
                    )
            finally:
                workspace.close()

            reloaded_backend = FakeProposalBackend()
            reloaded = self.make_workspace(
                queue_path,
                workspace_root,
                proposal_backend=reloaded_backend,
            )
            try:
                llm = reloaded.meta()["llm"]
                self.assertEqual(llm["settings_revision"], 1)
                self.assertIsNone(llm["model"])
                self.assertEqual(llm["effort"], "high")
                self.assertIsNone(reloaded_backend.model)
                self.assertEqual(reloaded_backend.effort, "high")
                unchanged = reloaded.patch_llm_settings(
                    {
                        "base_revision": 1,
                        "model": None,
                        "effort": "high",
                    }
                )
                self.assertEqual(unchanged["settings_revision"], 1)
            finally:
                reloaded.close()

            settings_path = workspace_root / "llm-settings.json"
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            settings["queue_sha256"] = "sha256:" + "1" * 64
            settings_path.write_bytes(serve.canonical_json_bytes(settings))
            with self.assertRaisesRegex(
                serve.AnnotationError, "different queue"
            ):
                self.make_workspace(queue_path, workspace_root)

    def test_legacy_workspace_wrong_queue_does_not_create_llm_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            queue_path = root / "queue.jsonl"
            wrong_queue_path = root / "wrong-queue.jsonl"
            workspace_root = root / "workspace"
            write_queue(queue_path, [queue_record("case-1")])
            write_queue(wrong_queue_path, [queue_record("case-2")])

            workspace = self.make_workspace(queue_path, workspace_root)
            workspace.close()
            settings_path = workspace_root / "llm-settings.json"
            settings_path.unlink()

            with self.assertRaisesRegex(
                serve.AnnotationError, "snapshot is bound to a different queue"
            ):
                self.make_workspace(wrong_queue_path, workspace_root)
            self.assertFalse(settings_path.exists())

            reopened = self.make_workspace(queue_path, workspace_root)
            try:
                self.assertTrue(settings_path.exists())
                self.assertEqual(
                    reopened.meta()["llm"]["settings_revision"], 0
                )
            finally:
                reopened.close()

    def test_llm_settings_write_failure_never_publishes_uncommitted_value(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            queue_path = root / "queue.jsonl"
            workspace_root = root / "workspace"
            write_queue(queue_path, [queue_record("case-1")])
            backend = FakeProposalBackend()
            workspace = self.make_workspace(
                queue_path,
                workspace_root,
                proposal_backend=backend,
            )
            settings_path = workspace_root / "llm-settings.json"
            original_bytes = settings_path.read_bytes()
            original_atomic_write = serve._atomic_write

            def fail_before_replace(path: Path, data: bytes) -> None:
                if path == settings_path:
                    raise OSError("fixture pre-commit failure")
                original_atomic_write(path, data)

            try:
                with mock.patch.object(
                    serve, "_atomic_write", side_effect=fail_before_replace
                ):
                    with self.assertRaisesRegex(OSError, "pre-commit"):
                        workspace.patch_llm_settings(
                            {
                                "base_revision": 0,
                                "model": "fixture-next-model",
                                "effort": "high",
                            }
                        )
                self.assertEqual(settings_path.read_bytes(), original_bytes)
                self.assertEqual(backend.model, "fixture-requested-model")
                self.assertEqual(backend.effort, "medium")
                self.assertEqual(
                    workspace.meta()["llm"]["settings_revision"], 0
                )

                def fail_after_replace(path: Path, data: bytes) -> None:
                    original_atomic_write(path, data)
                    if path == settings_path:
                        raise OSError("fixture post-commit failure")

                with mock.patch.object(
                    serve, "_atomic_write", side_effect=fail_after_replace
                ):
                    with self.assertRaisesRegex(OSError, "post-commit"):
                        workspace.patch_llm_settings(
                            {
                                "base_revision": 0,
                                "model": "fixture-next-model",
                                "effort": "high",
                            }
                        )
                stored = json.loads(settings_path.read_text(encoding="utf-8"))
                self.assertEqual(stored["revision"], 1)
                self.assertEqual(stored["model"], "fixture-next-model")
                self.assertEqual(stored["effort"], "high")
                self.assertEqual(backend.model, "fixture-next-model")
                self.assertEqual(backend.effort, "high")
                self.assertEqual(
                    workspace.meta()["llm"]["settings_revision"], 1
                )
            finally:
                workspace.close()

    def test_codex_app_server_jsonl_protocol(self) -> None:
        output = {
            "ambiguous": False,
            "ambiguity_reasons": [],
            "candidates": [
                {
                    "surface_reference_index": 0,
                    "chunks": [
                        {"reading": "きょうは", "surface": "今日は"},
                        {"reading": "あめ", "surface": "雨"},
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            transcript_path = root / "protocol.jsonl"
            executable = root / "fake-codex"
            model_pages = [
                {
                    "data": [
                        {
                            "id": "fixture-default",
                            "model": "fixture-default",
                            "displayName": "Fixture Default",
                            "description": "Default fixture model",
                            "hidden": False,
                            "isDefault": True,
                            "defaultReasoningEffort": "medium",
                            "supportedReasoningEfforts": [
                                {
                                    "reasoningEffort": "low",
                                    "description": "Lower latency",
                                },
                                {
                                    "reasoningEffort": "medium",
                                    "description": "Balanced",
                                },
                            ],
                        }
                    ],
                    "nextCursor": "fixture-page-2",
                },
                {
                    "data": [
                        {
                            "id": "fixture-deep",
                            "model": "fixture-deep",
                            "displayName": "Fixture Deep",
                            "description": "Deep fixture model",
                            "hidden": False,
                            "isDefault": False,
                            "defaultReasoningEffort": "xhigh",
                            "supportedReasoningEfforts": [
                                {
                                    "reasoningEffort": "xhigh",
                                    "description": "Extended reasoning",
                                },
                                {
                                    "reasoningEffort": "ultra",
                                    "description": "Maximum reasoning",
                                },
                            ],
                        }
                    ],
                    "nextCursor": None,
                },
            ]
            write_fake_codex_app_server(
                executable,
                transcript_path,
                output,
                model_pages=model_pages,
            )
            source_codex_home = root / "source-codex-home"
            source_codex_home.mkdir()
            source_auth_path = source_codex_home / "auth.json"
            first_access_token = "fixture-access-token-one"
            first_auth = (
                json.dumps(
                    {
                        "auth_mode": "chatgpt",
                        "tokens": {
                            "access_token": first_access_token,
                            "account_id": "fixture-account",
                        },
                    },
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
            source_auth_path.write_bytes(first_auth)
            with mock.patch.dict(
                serve.os.environ, {"CODEX_ACCESS_TOKEN": ""}, clear=False
            ):
                backend = serve.CodexAppServerProposalBackend(
                    executable,
                    model="fixture-requested-model",
                    timeout_seconds=5,
                    effort="low",
                    source_codex_home=source_codex_home,
                )
                self.assertEqual(backend._auth_mode, "source_chatgpt")
                self.assertEqual(backend._source_auth_path, source_auth_path)
                isolated_auth_path = backend._codex_home / "auth.json"
                self.assertFalse(isolated_auth_path.exists())
                self.assertIn(
                    'cli_auth_credentials_store = "ephemeral"',
                    (backend._codex_home / "config.toml").read_text(
                        encoding="utf-8"
                    ),
                )
                self.assertEqual(source_auth_path.read_bytes(), first_auth)
                output_schema = {
                    "type": "object",
                    "properties": {"ambiguous": {"type": "boolean"}},
                    "required": ["ambiguous"],
                    "additionalProperties": False,
                }
                self.assertTrue(backend._generation_lock.acquire(blocking=False))
                try:
                    with self.assertRaises(serve.CodexAppServerBusy):
                        backend.list_models()
                    self.assertIsNone(backend._process)
                finally:
                    backend._generation_lock.release()
                try:
                    catalog = backend.list_models()
                    result = backend.generate(
                        instructions="IMEチャンク候補だけを返す。",
                        input_text='{"reading":"きょうはあめ"}',
                        output_schema=output_schema,
                    )
                    self.assertEqual(source_auth_path.read_bytes(), first_auth)
                    self.assertFalse(isolated_auth_path.exists())

                    second_access_token = "fixture-access-token-two"
                    second_auth = (
                        json.dumps(
                            {
                                "auth_mode": "chatgpt",
                                "tokens": {
                                    "access_token": second_access_token,
                                    "account_id": "fixture-account",
                                },
                            },
                            separators=(",", ":"),
                        )
                        + "\n"
                    ).encode("utf-8")
                    source_auth_path.write_bytes(second_auth)
                    rotated_result = backend.generate(
                        instructions="IMEチャンク候補だけを返す。",
                        input_text='{"reading":"きょうはあめ"}',
                        output_schema=output_schema,
                    )
                    self.assertEqual(source_auth_path.read_bytes(), second_auth)
                    self.assertFalse(isolated_auth_path.exists())
                finally:
                    backend.close()

            self.assertEqual(result.output, output)
            self.assertEqual(rotated_result.output, output)
            self.assertEqual(
                [item["model"] for item in catalog["models"]],
                ["fixture-default", "fixture-deep"],
            )
            self.assertEqual(
                [
                    effort["reasoning_effort"]
                    for effort in catalog["models"][1][
                        "supported_reasoning_efforts"
                    ]
                ],
                ["xhigh", "ultra"],
            )
            self.assertEqual(result.model, "fixture-app-server-model")
            self.assertEqual(result.requested_model, "fixture-requested-model")
            self.assertEqual(result.reasoning_effort, "low")
            self.assertEqual(
                result.model_provider, "fixture-app-server-provider"
            )
            self.assertEqual(result.app_server_user_agent, "fake-codex-app-server/1")
            self.assertEqual(result.thread_id, "thread-app-server-fixture")
            self.assertEqual(result.turn_id, "turn-app-server-fixture")
            self.assertEqual(result.message_id, "message-app-server-fixture")
            self.assertEqual(result.duration_ms, 73)

            transcript_text = transcript_path.read_text(encoding="utf-8")
            self.assertNotIn(first_access_token, transcript_text)
            self.assertNotIn(second_access_token, transcript_text)
            self.assertNotIn("fixture-account", transcript_text)
            self.assertIn('"accessToken":"***"', transcript_text)
            self.assertIn('"chatgptAccountId":"***"', transcript_text)
            transcript = [
                json.loads(line) for line in transcript_text.splitlines()
            ]
            model_list_entries = [
                entry
                for entry in transcript
                if entry["direction"] == "client"
                and entry["message"].get("method") == "model/list"
            ]
            self.assertEqual(len(model_list_entries), 2)
            self.assertEqual(
                model_list_entries[0]["message"]["params"],
                {"limit": 50, "includeHidden": False},
            )
            self.assertEqual(
                model_list_entries[1]["message"]["params"],
                {
                    "limit": 50,
                    "includeHidden": False,
                    "cursor": "fixture-page-2",
                },
            )
            initialize_entries = [
                entry
                for entry in transcript
                if entry["direction"] == "client"
                and entry["message"].get("method") == "initialize"
            ]
            self.assertEqual(len(initialize_entries), 2)
            self.assertEqual(
                len({entry["pid"] for entry in initialize_entries}), 2
            )
            self.assertTrue(
                all(
                    not entry["has_env_access_token"]
                    for entry in initialize_entries
                )
            )
            self.assertTrue(
                all(
                    entry["env_access_token_sha256"] is None
                    for entry in initialize_entries
                )
            )
            login_entries = [
                entry
                for entry in transcript
                if entry["direction"] == "client"
                and entry["message"].get("method") == "account/login/start"
            ]
            self.assertEqual(len(login_entries), 2)
            self.assertEqual(
                {
                    entry["credential_sha256"] for entry in login_entries
                },
                {
                    hashlib.sha256(
                        first_access_token.encode("utf-8")
                    ).hexdigest(),
                    hashlib.sha256(
                        second_access_token.encode("utf-8")
                    ).hexdigest(),
                },
            )
            self.assertEqual(
                {entry["credential_type"] for entry in login_entries},
                {"chatgptAuthTokens"},
            )
            self.assertTrue(
                all(
                    entry["message"]["params"]["accessToken"] == "***"
                    and entry["message"]["params"]["chatgptAccountId"] == "***"
                    for entry in login_entries
                )
            )
            refresh_responses = [
                entry
                for entry in transcript
                if entry["direction"] == "client"
                and entry["message"].get("id") == 9001
            ]
            self.assertEqual(len(refresh_responses), 2)
            self.assertTrue(
                all(
                    entry["message"]["error"]["code"] == -32002
                    and "result" not in entry["message"]
                    for entry in refresh_responses
                )
            )
            account_read_entries = [
                entry
                for entry in transcript
                if entry["direction"] == "client"
                and entry["message"].get("method") == "account/read"
            ]
            self.assertEqual(len(account_read_entries), 2)
            self.assertTrue(
                all(
                    entry["message"]["params"] == {"refreshToken": False}
                    for entry in account_read_entries
                )
            )

            def client_message(method: str) -> dict[str, object]:
                return next(
                    entry["message"]
                    for entry in transcript
                    if entry["direction"] == "client"
                    and entry["message"].get("method") == method
                )

            initialize = client_message("initialize")
            self.assertEqual(
                initialize["params"]["clientInfo"]["name"],
                "hazkey_boundary_annotator",
            )
            self.assertTrue(
                initialize["params"]["capabilities"]["experimentalApi"]
            )

            login = client_message("account/login/start")
            account_read = client_message("account/read")
            first_initialize_index = transcript.index(initialize_entries[0])
            first_login_index = transcript.index(login_entries[0])
            first_account_read_index = transcript.index(account_read_entries[0])
            first_thread_index = next(
                index
                for index, entry in enumerate(transcript)
                if entry["direction"] == "client"
                and entry["message"].get("method") == "thread/start"
            )
            self.assertLess(first_initialize_index, first_login_index)
            self.assertLess(first_login_index, first_account_read_index)
            self.assertLess(first_account_read_index, first_thread_index)
            self.assertEqual(login["params"]["type"], "chatgptAuthTokens")
            self.assertEqual(login["params"]["accessToken"], "***")
            self.assertEqual(login["params"]["chatgptAccountId"], "***")
            self.assertEqual(account_read["params"], {"refreshToken": False})
            thread_start = client_message("thread/start")
            self.assertEqual(thread_start["params"]["sandbox"], "read-only")
            self.assertEqual(thread_start["params"]["approvalPolicy"], "never")
            self.assertTrue(thread_start["params"]["ephemeral"])
            self.assertEqual(
                thread_start["params"]["model"], "fixture-requested-model"
            )
            turn_start = client_message("turn/start")
            self.assertEqual(turn_start["params"]["outputSchema"], output_schema)
            self.assertEqual(
                turn_start["params"]["sandboxPolicy"],
                {"type": "readOnly", "networkAccess": False},
            )
            self.assertEqual(turn_start["params"]["approvalPolicy"], "never")
            self.assertEqual(turn_start["params"]["effort"], "low")
            self.assertTrue(
                turn_start["params"]["input"][0]["text"].endswith(
                    '{"reading":"きょうはあめ"}'
                )
            )
            approval_response = next(
                entry["message"]
                for entry in transcript
                if entry["direction"] == "client"
                and entry["message"].get("id") == 9000
            )
            self.assertEqual(approval_response["error"]["code"], -32601)
            turn_completed_index = next(
                index
                for index, entry in enumerate(transcript)
                if entry["direction"] == "server"
                and entry["message"].get("method") == "turn/completed"
            )
            turn_response_index = next(
                index
                for index, entry in enumerate(transcript)
                if entry["direction"] == "server"
                and entry["message"].get("id") == turn_start["id"]
            )
            self.assertLess(
                turn_completed_index,
                turn_response_index,
            )
            self.assertIn("thread/unsubscribe", [
                entry["message"].get("method")
                for entry in transcript
                if entry["direction"] == "client"
            ])

    def test_codex_model_catalog_rejects_repeated_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            transcript_path = root / "protocol.jsonl"
            executable = root / "fake-codex"

            def model_entry(model: str) -> dict[str, object]:
                return {
                    "id": model,
                    "model": model,
                    "displayName": model,
                    "description": "fixture model",
                    "hidden": False,
                    "isDefault": False,
                    "defaultReasoningEffort": "medium",
                    "supportedReasoningEfforts": [],
                }

            write_fake_codex_app_server(
                executable,
                transcript_path,
                {},
                model_pages=[
                    {
                        "data": [model_entry("fixture-page-one")],
                        "nextCursor": "repeated-cursor",
                    },
                    {
                        "data": [model_entry("fixture-page-two")],
                        "nextCursor": "repeated-cursor",
                    },
                ],
            )
            with mock.patch.dict(
                serve.os.environ,
                {"CODEX_ACCESS_TOKEN": "fixture-access-token"},
                clear=False,
            ):
                backend = serve.CodexAppServerProposalBackend(
                    executable,
                    model=None,
                    timeout_seconds=5,
                    effort="medium",
                    source_codex_home=root / "unused-source-home",
                )
                try:
                    with self.assertRaisesRegex(
                        serve.CodexAppServerUnavailable,
                        "cursor is invalid",
                    ):
                        backend.list_models()
                    self.assertIsNone(backend._process)
                finally:
                    backend.close()

            transcript = [
                json.loads(line)
                for line in transcript_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                sum(
                    entry["direction"] == "client"
                    and entry["message"].get("method") == "model/list"
                    for entry in transcript
                ),
                2,
            )

    def test_codex_model_catalog_counts_hidden_entries_toward_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            transcript_path = root / "protocol.jsonl"
            executable = root / "fake-codex"
            hidden_models = [
                {
                    "id": f"fixture-hidden-{index}",
                    "model": f"fixture-hidden-{index}",
                    "displayName": f"Fixture Hidden {index}",
                    "description": "unexpected hidden fixture model",
                    "hidden": True,
                    "isDefault": False,
                    "defaultReasoningEffort": "medium",
                    "supportedReasoningEfforts": [],
                }
                for index in range(serve.MAX_APP_SERVER_MODELS + 1)
            ]
            write_fake_codex_app_server(
                executable,
                transcript_path,
                {},
                model_pages=[{"data": hidden_models, "nextCursor": None}],
            )
            with mock.patch.dict(
                serve.os.environ,
                {"CODEX_ACCESS_TOKEN": "fixture-access-token"},
                clear=False,
            ):
                backend = serve.CodexAppServerProposalBackend(
                    executable,
                    model=None,
                    timeout_seconds=5,
                    effort="medium",
                    source_codex_home=root / "unused-source-home",
                )
                try:
                    with self.assertRaisesRegex(
                        serve.CodexAppServerUnavailable,
                        "catalog is too large",
                    ):
                        backend.list_models()
                    self.assertIsNone(backend._process)
                finally:
                    backend.close()

    def test_codex_refresh_during_login_preserves_newer_source_auth(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            executable = root / "unused-fake-codex"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o700)
            source_home = root / "source-codex-home"
            source_home.mkdir()
            source_auth_path = source_home / "auth.json"

            def auth_bytes(token: str, account_id: str) -> bytes:
                return (
                    json.dumps(
                        {
                            "auth_mode": "chatgpt",
                            "tokens": {
                                "access_token": token,
                                "account_id": account_id,
                            },
                        },
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode("utf-8")

            first_bytes = auth_bytes("fixture-login-token-a", "fixture-account-a")
            second_bytes = auth_bytes("fixture-login-token-b", "fixture-account-b")
            source_auth_path.write_bytes(first_bytes)
            first_auth = serve._read_codex_file_auth(source_auth_path)

            with mock.patch.dict(
                serve.os.environ, {"CODEX_ACCESS_TOKEN": ""}, clear=False
            ):
                backend = serve.CodexAppServerProposalBackend(
                    executable,
                    model=None,
                    timeout_seconds=5,
                    effort="low",
                    source_codex_home=source_home,
                )
                sent: list[dict[str, object]] = []

                def request(
                    method: str,
                    params: dict[str, object],
                    deadline: float,
                ) -> dict[str, object]:
                    del deadline
                    if method == "account/login/start":
                        self.assertEqual(
                            backend._active_auth_fingerprint,
                            first_auth.fingerprint,
                        )
                        backend._handle_server_request(
                            {
                                "id": 9100,
                                "method": "account/chatgptAuthTokens/refresh",
                                "params": {},
                            }
                        )
                        self.assertEqual(sent[-1]["error"]["code"], -32002)
                        source_auth_path.write_bytes(second_bytes)
                        second_auth = serve._read_codex_file_auth(source_auth_path)
                        backend._handle_server_request(
                            {
                                "id": 9101,
                                "method": "account/chatgptAuthTokens/refresh",
                                "params": {},
                            }
                        )
                        self.assertEqual(
                            sent[-1]["result"],
                            {
                                "accessToken": second_auth.credential,
                                "chatgptAccountId": second_auth.account_id,
                            },
                        )
                        self.assertEqual(
                            backend._active_auth_fingerprint,
                            second_auth.fingerprint,
                        )
                        return {"type": params["type"]}
                    if method == "account/read":
                        return {
                            "requiresOpenaiAuth": True,
                            "account": {"type": "fixture-account"},
                        }
                    self.fail(f"unexpected request: {method}")

                try:
                    with mock.patch.object(
                        backend, "_request", side_effect=request
                    ), mock.patch.object(backend, "_send", side_effect=sent.append):
                        backend._authenticate_isolated_process(float("inf"))
                    second_auth = serve._read_codex_file_auth(source_auth_path)
                    self.assertEqual(
                        backend._active_auth_fingerprint,
                        second_auth.fingerprint,
                    )
                finally:
                    backend.close()

    def test_codex_source_api_key_login_and_account_read_rejection(self) -> None:
        output = {
            "ambiguous": False,
            "ambiguity_reasons": [],
            "candidates": [
                {
                    "surface_reference_index": 0,
                    "chunks": [
                        {"reading": "きょうは", "surface": "今日は"},
                        {"reading": "あめ", "surface": "雨"},
                    ],
                }
            ],
        }
        output_schema = {
            "type": "object",
            "properties": {"ambiguous": {"type": "boolean"}},
            "required": ["ambiguous"],
            "additionalProperties": False,
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            api_key = "fixture-source-api-key"
            source_home = root / "source-home"
            source_home.mkdir()
            source_auth_path = source_home / "auth.json"
            source_auth = (
                json.dumps(
                    {
                        "auth_mode": "apikey",
                        "OPENAI_API_KEY": api_key,
                    },
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
            source_auth_path.write_bytes(source_auth)

            transcript_path = root / "api-key-protocol.jsonl"
            executable = root / "fake-codex-api-key"
            write_fake_codex_app_server(executable, transcript_path, output)
            with mock.patch.dict(
                serve.os.environ, {"CODEX_ACCESS_TOKEN": ""}, clear=False
            ):
                backend = serve.CodexAppServerProposalBackend(
                    executable,
                    model=None,
                    timeout_seconds=5,
                    effort="low",
                    source_codex_home=source_home,
                )
                try:
                    self.assertEqual(backend._auth_mode, "source_api_key")
                    self.assertFalse((backend._codex_home / "auth.json").exists())
                    self.assertIn(
                        'cli_auth_credentials_store = "ephemeral"',
                        (backend._codex_home / "config.toml").read_text(
                            encoding="utf-8"
                        ),
                    )
                    result = backend.generate(
                        instructions="IMEチャンク候補だけを返す。",
                        input_text='{"reading":"きょうはあめ"}',
                        output_schema=output_schema,
                    )
                    self.assertEqual(result.output, output)
                    self.assertEqual(source_auth_path.read_bytes(), source_auth)
                finally:
                    backend.close()

            transcript_text = transcript_path.read_text(encoding="utf-8")
            self.assertNotIn(api_key, transcript_text)
            self.assertIn('"apiKey":"***"', transcript_text)
            transcript = [
                json.loads(line) for line in transcript_text.splitlines()
            ]
            initialize = next(
                entry
                for entry in transcript
                if entry["direction"] == "client"
                and entry["message"].get("method") == "initialize"
            )
            self.assertFalse(
                initialize["message"]["params"]["capabilities"]
                ["experimentalApi"]
            )
            self.assertFalse(initialize["has_env_access_token"])
            login = next(
                entry
                for entry in transcript
                if entry["direction"] == "client"
                and entry["message"].get("method") == "account/login/start"
            )
            self.assertEqual(login["credential_type"], "apiKey")
            self.assertEqual(
                login["credential_sha256"],
                hashlib.sha256(api_key.encode("utf-8")).hexdigest(),
            )
            self.assertEqual(
                login["message"]["params"],
                {"type": "apiKey", "apiKey": "***"},
            )

            rejected_transcript_path = root / "unauthenticated-protocol.jsonl"
            rejected_executable = root / "fake-codex-unauthenticated"
            write_fake_codex_app_server(
                rejected_executable,
                rejected_transcript_path,
                output,
                authenticated=False,
            )
            with mock.patch.dict(
                serve.os.environ, {"CODEX_ACCESS_TOKEN": ""}, clear=False
            ):
                backend = serve.CodexAppServerProposalBackend(
                    rejected_executable,
                    model=None,
                    timeout_seconds=5,
                    effort="low",
                    source_codex_home=source_home,
                )
                try:
                    with self.assertRaisesRegex(
                        serve.CodexAppServerUnavailable, "not authenticated"
                    ):
                        backend.generate(
                            instructions="IMEチャンク候補だけを返す。",
                            input_text='{"reading":"きょうはあめ"}',
                            output_schema=output_schema,
                        )
                finally:
                    backend.close()
            rejected_text = rejected_transcript_path.read_text(encoding="utf-8")
            self.assertNotIn(api_key, rejected_text)
            rejected_transcript = [
                json.loads(line) for line in rejected_text.splitlines()
            ]
            rejected_methods = [
                entry["message"].get("method")
                for entry in rejected_transcript
                if entry["direction"] == "client"
            ]
            self.assertIn("account/login/start", rejected_methods)
            self.assertIn("account/read", rejected_methods)
            self.assertNotIn("thread/start", rejected_methods)
            self.assertEqual(source_auth_path.read_bytes(), source_auth)

    def test_codex_explicit_access_token_uses_environment_only(self) -> None:
        output = {
            "ambiguous": False,
            "ambiguity_reasons": [],
            "candidates": [
                {
                    "surface_reference_index": 0,
                    "chunks": [
                        {"reading": "きょうは", "surface": "今日は"},
                        {"reading": "あめ", "surface": "雨"},
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_home = root / "empty-source-home"
            source_home.mkdir()
            transcript_path = root / "environment-token-protocol.jsonl"
            executable = root / "fake-codex-environment-token"
            write_fake_codex_app_server(executable, transcript_path, output)
            access_token = "fixture-explicit-access-token"
            with mock.patch.dict(
                serve.os.environ,
                {"CODEX_ACCESS_TOKEN": access_token},
                clear=False,
            ):
                backend = serve.CodexAppServerProposalBackend(
                    executable,
                    model=None,
                    timeout_seconds=5,
                    effort="low",
                    source_codex_home=source_home,
                )
                try:
                    self.assertEqual(
                        backend._auth_mode, "environment_access_token"
                    )
                    self.assertFalse((backend._codex_home / "auth.json").exists())
                    result = backend.generate(
                        instructions="IMEチャンク候補だけを返す。",
                        input_text='{"reading":"きょうはあめ"}',
                        output_schema={
                            "type": "object",
                            "properties": {
                                "ambiguous": {"type": "boolean"}
                            },
                            "required": ["ambiguous"],
                            "additionalProperties": False,
                        },
                    )
                    self.assertEqual(result.output, output)
                finally:
                    backend.close()

            transcript_text = transcript_path.read_text(encoding="utf-8")
            self.assertNotIn(access_token, transcript_text)
            transcript = [
                json.loads(line) for line in transcript_text.splitlines()
            ]
            initialize = next(
                entry
                for entry in transcript
                if entry["direction"] == "client"
                and entry["message"].get("method") == "initialize"
            )
            self.assertTrue(initialize["has_env_access_token"])
            self.assertEqual(
                initialize["env_access_token_sha256"],
                hashlib.sha256(access_token.encode("utf-8")).hexdigest(),
            )
            self.assertFalse(
                initialize["message"]["params"]["capabilities"]
                ["experimentalApi"]
            )
            client_methods = [
                entry["message"].get("method")
                for entry in transcript
                if entry["direction"] == "client"
            ]
            self.assertNotIn("account/login/start", client_methods)
            self.assertIn("account/read", client_methods)

    def test_codex_file_auth_validation_and_keyring_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            valid_auth_path = root / "valid-auth.json"
            valid_auth = (
                '{"auth_mode":"chatgpt","tokens":'
                '{"access_token":"fixture-valid-access-token",'
                '"account_id":"fixture-account"}}\n'
            ).encode("utf-8")
            valid_auth_path.write_bytes(valid_auth)
            file_auth = serve._read_codex_file_auth(valid_auth_path)
            self.assertEqual(file_auth.kind, "chatgpt")
            self.assertEqual(file_auth.credential, "fixture-valid-access-token")
            self.assertEqual(file_auth.account_id, "fixture-account")
            self.assertEqual(
                file_auth.fingerprint,
                hashlib.sha256(
                    (
                        file_auth.credential
                        + "\0"
                        + file_auth.account_id
                    ).encode("utf-8")
                ).hexdigest(),
            )
            alternate_account_path = root / "alternate-account-auth.json"
            alternate_account_path.write_text(
                '{"auth_mode":"chatgpt","tokens":'
                '{"access_token":"fixture-valid-access-token",'
                '"account_id":"fixture-other-account"}}\n',
                encoding="utf-8",
            )
            alternate_account_auth = serve._read_codex_file_auth(
                alternate_account_path
            )
            self.assertNotEqual(
                alternate_account_auth.fingerprint, file_auth.fingerprint
            )
            self.assertNotIn(file_auth.credential, repr(file_auth))
            self.assertEqual(valid_auth_path.read_bytes(), valid_auth)

            symlink_auth_path = root / "symlink-auth.json"
            symlink_auth_path.symlink_to(valid_auth_path)
            with self.assertRaisesRegex(
                serve.CodexAppServerUnavailable, "regular file, not a symlink"
            ):
                serve._read_codex_file_auth(symlink_auth_path)

            invalid_documents = {
                "invalid-json": b"{\n",
                "non-object": b"[]\n",
                "missing-token": b'{"tokens":{}}\n',
                "empty-token": b'{"tokens":{"access_token":""}}\n',
                "control-character": (
                    b'{"tokens":{"access_token":"fixture\\u0000token"}}\n'
                ),
            }
            for name, contents in invalid_documents.items():
                with self.subTest(name=name):
                    invalid_auth_path = root / f"{name}.json"
                    invalid_auth_path.write_bytes(contents)
                    with self.assertRaises(serve.CodexAppServerUnavailable):
                        serve._read_codex_file_auth(invalid_auth_path)
                    self.assertEqual(invalid_auth_path.read_bytes(), contents)

            executable = root / "unused-fake-codex"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o700)
            keyring_home = root / "keyring-source-home"
            keyring_home.mkdir()
            (keyring_home / "config.toml").write_text(
                'cli_auth_credentials_store = "keyring"\n', encoding="utf-8"
            )
            with mock.patch.dict(
                serve.os.environ, {"CODEX_ACCESS_TOKEN": ""}, clear=False
            ):
                personal_token = "fixture-file-personal-access-token"
                personal_home = root / "personal-token-source-home"
                personal_home.mkdir()
                personal_auth = (
                    json.dumps(
                        {
                            "auth_mode": "personalAccessToken",
                            "personal_access_token": personal_token,
                        },
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode("utf-8")
                (personal_home / "auth.json").write_bytes(personal_auth)
                personal_backend = serve.CodexAppServerProposalBackend(
                    executable,
                    model=None,
                    timeout_seconds=5,
                    effort="low",
                    source_codex_home=personal_home,
                )
                try:
                    self.assertEqual(
                        personal_backend._auth_mode,
                        "source_personal_access_token",
                    )
                    environment, fingerprint = personal_backend._build_environment()
                    self.assertEqual(
                        environment.pop("CODEX_ACCESS_TOKEN"), personal_token
                    )
                    self.assertEqual(
                        fingerprint,
                        hashlib.sha256(personal_token.encode("utf-8")).hexdigest(),
                    )
                    self.assertFalse(
                        (personal_backend._codex_home / "auth.json").exists()
                    )
                    self.assertEqual(
                        (personal_home / "auth.json").read_bytes(), personal_auth
                    )
                finally:
                    personal_backend.close()

                with self.assertRaises(serve.CodexAppServerUnavailable):
                    serve.CodexAppServerProposalBackend(
                        executable,
                        model=None,
                        timeout_seconds=5,
                        effort="low",
                        source_codex_home=keyring_home,
                    )

                auto_home = root / "auto-source-home"
                auto_home.mkdir()
                (auto_home / "config.toml").write_text(
                    'cli_auth_credentials_store = "auto"\n', encoding="utf-8"
                )
                (auto_home / "auth.json").write_bytes(valid_auth)
                with self.assertRaisesRegex(
                    serve.CodexAppServerUnavailable,
                    "auto authentication",
                ):
                    serve.CodexAppServerProposalBackend(
                        executable,
                        model=None,
                        timeout_seconds=5,
                        effort="low",
                        source_codex_home=auto_home,
                    )


class HttpSmokeTests(unittest.TestCase):
    def test_token_meta_detail_patch_and_export(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            queue_path = root / "queue.jsonl"
            static_dir = root / "static"
            static_dir.mkdir()
            write_queue(queue_path, [queue_record("case-1")])
            for filename in ("index.html", "app.js", "style.css"):
                (static_dir / filename).write_text(
                    f"fixture {filename}\n", encoding="utf-8"
                )
            workspace = WorkspaceTests.make_workspace(
                queue_path,
                root / "workspace",
                proposal_backend=FakeProposalBackend(
                    {
                        "ambiguous": False,
                        "ambiguity_reasons": [],
                        "candidates": [
                            {
                                "surface_reference_index": 0,
                                "chunks": [
                                    {
                                        "reading": "きょうは",
                                        "surface": "今日は",
                                    },
                                    {"reading": "あめ", "surface": "雨"},
                                ],
                            }
                        ],
                    }
                ),
            )
            token = "fixture-token"
            application = serve.AnnotationApplication(workspace, static_dir, token)

            class MemorySocket:
                def __init__(self, request_bytes: bytes) -> None:
                    self.request_bytes = io.BytesIO(request_bytes)
                    self.response_bytes = bytearray()

                def makefile(self, mode: str, buffering: int = -1) -> io.BytesIO:
                    del buffering
                    if mode != "rb":
                        raise AssertionError(f"unexpected makefile mode: {mode}")
                    return self.request_bytes

                def sendall(self, data: bytes) -> None:
                    self.response_bytes.extend(data)

            class MemoryServer:
                server_name = "127.0.0.1"
                server_port = 0

            def request(
                method: str,
                path: str,
                *,
                body: dict[str, object] | None = None,
                authorized: bool = True,
            ) -> tuple[int, dict[str, str], bytes]:
                headers = {"Host": "127.0.0.1"}
                if authorized:
                    headers["X-Annotation-Token"] = token
                encoded_body = b""
                if body is not None:
                    encoded_body = json.dumps(body, ensure_ascii=False).encode("utf-8")
                    headers["Content-Type"] = "application/json"
                    headers["Content-Length"] = str(len(encoded_body))
                request_head = "\r\n".join(
                    [
                        f"{method} {path} HTTP/1.0",
                        *(f"{name}: {value}" for name, value in headers.items()),
                        "",
                        "",
                    ]
                ).encode("ascii")
                connection = MemorySocket(request_head + encoded_body)
                handler = serve.build_handler(application)
                handler(connection, ("127.0.0.1", 12345), MemoryServer())
                response_head, response_body = bytes(connection.response_bytes).split(
                    b"\r\n\r\n", 1
                )
                response_lines = response_head.decode("iso-8859-1").split("\r\n")
                status = int(response_lines[0].split(" ", 2)[1])
                response_headers = dict(
                    line.split(": ", 1) for line in response_lines[1:]
                )
                return status, response_headers, response_body

            status, _, body = request("GET", "/api/meta", authorized=False)
            self.assertEqual(status, 403)
            self.assertEqual(json.loads(body)["status"], 403)
            status, _, body = request(
                "GET", "/api/llm/models", authorized=False
            )
            self.assertEqual(status, 403)
            status, _, body = request(
                "GET", "/api/proposal-jobs", authorized=False
            )
            self.assertEqual(status, 403)
            status, _, body = request(
                "POST",
                "/api/proposal-jobs",
                body={
                    "case_ids": ["case-1"],
                    "llm_settings_revision": 0,
                    "client_request_id": "unauthorized-batch",
                },
                authorized=False,
            )
            self.assertEqual(status, 403)
            status, _, body = request(
                "PATCH",
                "/api/settings/llm",
                body={
                    "base_revision": 0,
                    "model": "fixture-ui-model",
                    "effort": "high",
                },
                authorized=False,
            )
            self.assertEqual(status, 403)

            status, _, body = request("GET", "/", authorized=False)
            self.assertEqual(status, 200)
            self.assertEqual(body, b"fixture index.html\n")
            status, _, body = request("GET", "/app.js", authorized=False)
            self.assertEqual(status, 200)
            self.assertEqual(body, b"fixture app.js\n")
            status, _, body = request(
                "GET", "/?token=fixture-token", authorized=False
            )
            self.assertEqual(status, 200)
            self.assertEqual(body, b"fixture index.html\n")

            status, headers, body = request("GET", "/api/meta")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["total"], 1)
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

            status, _, body = request("GET", "/api/proposal-jobs")
            self.assertEqual(status, 200)
            proposal_queue = json.loads(body)
            self.assertEqual(proposal_queue["schema"], serve.PROPOSAL_QUEUE_SCHEMA)
            self.assertEqual(proposal_queue["jobs"], [])

            status, _, body = request("GET", "/api/llm/models")
            self.assertEqual(status, 200)
            catalog = json.loads(body)
            self.assertEqual(catalog["provider"], "codex-app-server")
            self.assertEqual(
                catalog["models"][0]["model"], "fixture-requested-model"
            )
            self.assertEqual(
                [
                    item["reasoning_effort"]
                    for item in catalog["models"][0][
                        "supported_reasoning_efforts"
                    ]
                ],
                ["low", "medium", "high"],
            )
            catalog_failures = (
                (serve.CodexAppServerBusy("busy"), 409),
                (serve.CodexAppServerTimeout("timeout"), 504),
                (serve.CodexAppServerUnavailable("unavailable"), 503),
                (serve.CodexAppServerError("upstream"), 502),
            )
            for failure, expected_status in catalog_failures:
                with self.subTest(catalog_failure=type(failure).__name__):
                    with mock.patch.object(
                        workspace,
                        "llm_model_catalog",
                        side_effect=failure,
                    ):
                        status, _, body = request(
                            "GET", "/api/llm/models"
                        )
                    self.assertEqual(status, expected_status)
                    self.assertEqual(json.loads(body)["error"], str(failure))

            status, _, body = request(
                "PATCH",
                "/api/settings/llm",
                body={
                    "base_revision": 0,
                    "model": "fixture-ui-model",
                    "effort": "high",
                },
            )
            self.assertEqual(status, 200)
            llm = json.loads(body)["llm"]
            self.assertEqual(llm["settings_revision"], 1)
            self.assertEqual(llm["model"], "fixture-ui-model")
            self.assertEqual(llm["effort"], "high")

            status, _, body = request(
                "PATCH",
                "/api/settings/llm",
                body={
                    "base_revision": 0,
                    "model": None,
                    "effort": "low",
                },
            )
            self.assertEqual(status, 409)
            status, _, body = request(
                "PATCH",
                "/api/settings/llm",
                body={
                    "base_revision": 1,
                    "model": "invalid model",
                    "effort": "low",
                },
            )
            self.assertEqual(status, 400)

            status, _, body = request(
                "POST",
                "/api/proposal-jobs",
                body={
                    "case_ids": ["case-1", "case-1"],
                    "llm_settings_revision": 1,
                    "client_request_id": "duplicate-cases",
                },
            )
            self.assertEqual(status, 400)
            self.assertEqual(application.proposal_jobs.status()["jobs"], [])

            proposal_batch = {
                "case_ids": ["case-1"],
                "llm_settings_revision": 1,
                "client_request_id": "http-batch-1",
            }
            generation_started = threading.Event()
            release_generation = threading.Event()
            original_generate = workspace.generate_proposals

            def blocking_generate(
                *args: object, **kwargs: object
            ) -> dict[str, object]:
                generation_started.set()
                if not release_generation.wait(timeout=5):
                    raise AssertionError("test did not release HTTP proposal")
                return original_generate(*args, **kwargs)

            with mock.patch.object(
                workspace,
                "generate_proposals",
                side_effect=blocking_generate,
            ):
                try:
                    status, _, body = request(
                        "POST", "/api/proposal-jobs", body=proposal_batch
                    )
                    self.assertEqual(status, 202)
                    accepted_batch = json.loads(body)
                    self.assertEqual(accepted_batch["enqueued_count"], 1)
                    self.assertTrue(generation_started.wait(timeout=2))

                    status, _, body = request(
                        "PATCH",
                        "/api/settings/llm",
                        body={
                            "base_revision": 1,
                            "model": "must-wait-for-queue",
                            "effort": "low",
                        },
                    )
                    self.assertEqual(status, 423)
                    self.assertIn("active", json.loads(body)["error"])

                    status, _, body = request(
                        "POST", "/api/proposal-jobs", body=proposal_batch
                    )
                    self.assertEqual(status, 202)
                    self.assertTrue(json.loads(body)["idempotent"])
                    release_generation.set()
                    WorkspaceTests.wait_for_proposal_jobs(
                        application.proposal_jobs,
                        lambda queue_status: (
                            queue_status["counts"]["succeeded"] == 1
                        ),
                    )
                finally:
                    release_generation.set()

            status, _, body = request(
                "POST",
                "/api/cases/case-1/proposals",
                body={"llm_settings_revision": 1},
            )
            self.assertEqual(status, 202)
            legacy_alias = json.loads(body)
            self.assertEqual(legacy_alias["enqueued_count"], 1)
            WorkspaceTests.wait_for_proposal_jobs(
                application.proposal_jobs,
                lambda queue_status: (
                    queue_status["counts"]["succeeded"] == 2
                ),
            )

            status, _, body = request("GET", "/api/cases/case-1")
            self.assertEqual(status, 200)
            detail = json.loads(body)
            self.assertEqual(detail["case"]["reading"], "きょうはあめ")
            self.assertEqual(detail["review"]["revision"], 0)

            status, _, body = request(
                "PATCH", "/api/cases/case-1", body=review_payload()
            )
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["review"]["revision"], 1)

            status, headers, body = request(
                "GET", "/api/export/reviews.jsonl"
            )
            self.assertEqual(status, 200)
            self.assertIn("attachment", headers["Content-Disposition"])
            exported = json.loads(body)
            self.assertEqual(exported["path_set_status"], "closed")

            status, _, body = request("GET", "/api/export/manifest.json")
            self.assertEqual(status, 200)
            manifest = json.loads(body)
            self.assertTrue(manifest["complete"])
            self.assertEqual(
                manifest["reviewed_paths_sha256"],
                sha256_uri(workspace.export_bytes()),
            )

            correction_payload = review_payload(
                base_revision=1,
                path_set_status="pending",
                paths=[],
            )
            correction_payload["corrected_reading"] = "きょうはあめです"
            status, _, body = request(
                "PATCH", "/api/cases/case-1", body=correction_payload
            )
            self.assertEqual(status, 200)
            corrected_review = json.loads(body)["review"]
            self.assertEqual(corrected_review["revision"], 2)
            self.assertEqual(
                corrected_review["corrected_reading"], "きょうはあめです"
            )

            status, _, body = request("GET", "/api/cases/case-1")
            self.assertEqual(status, 200)
            corrected_detail = json.loads(body)
            self.assertEqual(
                corrected_detail["case"]["reading"], "きょうはあめ"
            )
            self.assertEqual(
                corrected_detail["case"]["annotation_reading"],
                "きょうはあめです",
            )
            self.assertNotIn("source_reading", corrected_detail["case"])

            status, _, body = request("GET", "/api/export/reviews.jsonl")
            self.assertEqual(status, 200)
            corrected_export = json.loads(body)
            self.assertEqual(
                corrected_export["source"]["reading"], "きょうはあめ"
            )
            self.assertEqual(
                corrected_export["source"]["annotation_reading"],
                "きょうはあめです",
            )
            self.assertNotIn("effective_reading", corrected_export["source"])

            invalid_correction = review_payload(
                base_revision=2,
                path_set_status="pending",
                paths=[],
            )
            invalid_correction["corrected_reading"] = "か\u3099くせい"
            status, _, body = request(
                "PATCH", "/api/cases/case-1", body=invalid_correction
            )
            self.assertEqual(status, 400)
            self.assertIn("NFC", json.loads(body)["error"])

            status, _, body = request(
                "POST",
                "/api/cases/case-1/proposals",
                body={"llm_settings_revision": 0},
            )
            self.assertEqual(status, 409)
            self.assertIn("revision", json.loads(body)["error"])
            application.close()


class SyntheticFixtureTests(unittest.TestCase):
    def test_minimal_xlsx_builder_is_deterministic(self) -> None:
        sheets = [
            (
                "Annotation",
                [
                    ["id", "reading", "marked_reading"],
                    ["case-1", "きょうはあめ", "きょうは|あめ"],
                ],
            )
        ]
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            first = root / "first.xlsx"
            second = root / "second.xlsx"
            write_minimal_xlsx(first, sheets)
            write_minimal_xlsx(second, sheets)

            self.assertEqual(first.read_bytes(), second.read_bytes())
            with zipfile.ZipFile(first) as archive:
                self.assertIn("xl/workbook.xml", archive.namelist())
                self.assertIn("きょうは|あめ", archive.read("xl/worksheets/sheet1.xml").decode())


if __name__ == "__main__":
    unittest.main()
