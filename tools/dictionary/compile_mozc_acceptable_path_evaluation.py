#!/usr/bin/env python3
"""Compile reviewed IME paths into a label-free diagnostic probe generation.

The annotation export is candidate-blind, but its offsets are expressed in
annotation-reading code points.  This compiler deliberately reacquires every
case with one direct ``CompositionElement`` per effective-reading code point,
and keeps the human targets in a separate hash-bound JSONL.  Multiple
acceptable paths are preserved as multiple acceptable first-segment targets.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
from typing import Any, Iterable
import unicodedata
import uuid


ANNOTATION_EXPORT_SCHEMA = "hazkey.mozc-hybrid-acceptable-paths.v3"
ANNOTATION_MANIFEST_SCHEMA = (
    "hazkey.mozc-boundary-annotation-export-manifest.v1"
)
PROBE_INPUT_SCHEMA = "hazkey.mozc-hybrid-segment-probe-input.v1"
TARGET_SCHEMA = "hazkey.mozc-acceptable-first-segment-target.v1"
GENERATION_MANIFEST_SCHEMA = (
    "hazkey.mozc-acceptable-path-evaluation-generation-manifest.v1"
)

SOURCE_READING_UNIT = "source_reading_code_point"
ANNOTATION_READING_UNIT = "annotation_reading_code_point"
SURFACE_UNIT = "surface_reference_code_point"
COMPOSITION_ELEMENT_UNIT = "composition_element"

SOURCE_REVIEWED_PATHS_NAME = "reviewed-paths.jsonl"
SOURCE_ANNOTATION_MANIFEST_NAME = "annotation-manifest.json"
PROBE_INPUT_NAME = "probe-input.jsonl"
TARGETS_NAME = "targets.jsonl"
MANIFEST_NAME = "manifest.json"

SHA256_URI = re.compile(r"sha256:[0-9a-f]{64}")
PATH_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
SURFACE_EVALUATION_STATUSES = frozenset(
    {"fully_aligned", "partially_aligned", "not_aligned"}
)
ANNOTATION_AUDIT_KEYS = frozenset(
    {
        "routing_batch_id",
        "annotation_tier",
        "llm_unmodified",
        "human_reviewed",
    }
)


def _object_without_duplicate_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _require_exact_keys(
    value: dict[str, Any], expected: Iterable[str], context: str
) -> None:
    expected_set = set(expected)
    actual_set = set(value)
    if actual_set != expected_set:
        raise ValueError(
            f"{context} fields do not match schema; "
            f"missing={sorted(expected_set - actual_set)!r}, "
            f"unknown={sorted(actual_set - expected_set)!r}"
        )


def _require_allowed_keys(
    value: dict[str, Any], required: Iterable[str], allowed: Iterable[str], context: str
) -> None:
    required_set = set(required)
    allowed_set = set(allowed)
    actual_set = set(value)
    if not required_set.issubset(actual_set) or not actual_set.issubset(allowed_set):
        raise ValueError(
            f"{context} fields do not match schema; "
            f"missing={sorted(required_set - actual_set)!r}, "
            f"unknown={sorted(actual_set - allowed_set)!r}"
        )


def _object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be an object")
    return value


def _array(value: Any, context: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{context} must be an array")
    return value


def _text(value: Any, context: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        qualifier = "a string" if allow_empty else "a non-empty string"
        raise ValueError(f"{context} must be {qualifier}")
    if value != unicodedata.normalize("NFC", value):
        raise ValueError(f"{context} must be NFC-normalized")
    if any(
        unicodedata.category(character) == "Cc" or character == "\ufeff"
        for character in value
    ):
        raise ValueError(
            f"{context} must not contain control characters or U+FEFF"
        )
    return value


def _note(value: Any, context: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{context} must be a string")
    if value != unicodedata.normalize("NFC", value):
        raise ValueError(f"{context} must be NFC-normalized")
    if any(
        unicodedata.category(character) == "Cc" and character not in {"\n", "\t"}
        for character in value
    ):
        raise ValueError(f"{context} contains an unsupported control character")
    return value


def _positive_int(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{context} must be a positive integer")
    return value


def _nonnegative_int(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{context} must be a non-negative integer")
    return value


def _sha256(value: Any, context: str) -> str:
    if not isinstance(value, str) or SHA256_URI.fullmatch(value) is None:
        raise ValueError(f"{context} must be sha256:<64 lowercase hex>")
    return value


def sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def _render_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _render_jsonl(values: Iterable[dict[str, Any]]) -> bytes:
    return b"".join(_canonical_json(value) + b"\n" for value in values)


def _file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_regular(path: Path, context: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValueError(f"{context} must be a regular non-symlink file") from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"{context} must be a regular non-symlink file")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        current = os.stat(path, follow_symlinks=False)
        if _file_identity(before) != _file_identity(after) or _file_identity(
            before
        ) != _file_identity(current):
            raise ValueError(f"{context} changed during the exact-byte read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _load_json_bytes(data: bytes, context: str) -> Any:
    if data.startswith(b"\xef\xbb\xbf"):
        raise ValueError(f"{context} must not contain a UTF-8 BOM")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"{context} is not valid UTF-8") from error
    try:
        return json.loads(text, object_pairs_hook=_object_without_duplicate_keys)
    except json.JSONDecodeError as error:
        raise ValueError(f"{context} is invalid JSON: {error.msg}") from error


def _load_jsonl(data: bytes, context: str) -> list[dict[str, Any]]:
    if data.startswith(b"\xef\xbb\xbf") or b"\r" in data or not data.endswith(b"\n"):
        raise ValueError(f"{context} must be BOM-free UTF-8 JSONL with LF endings")
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(data[:-1].split(b"\n"), 1):
        if not line:
            raise ValueError(f"{context}:{line_number} must not be blank")
        records.append(
            _object(
                _load_json_bytes(line, f"{context}:{line_number}"),
                f"{context}:{line_number}",
            )
        )
    if not records:
        raise ValueError(f"{context} must contain at least one record")
    return records


def _validate_boundaries(value: Any, length: int, context: str) -> list[int]:
    boundaries = _array(value, context)
    if any(isinstance(item, bool) or not isinstance(item, int) for item in boundaries):
        raise ValueError(f"{context} must contain integers")
    if boundaries != sorted(set(boundaries)):
        raise ValueError(f"{context} must be strictly increasing and unique")
    if any(boundary <= 0 or boundary >= length for boundary in boundaries):
        raise ValueError(f"{context} must contain internal boundaries only")
    return boundaries


def _validate_annotation_audit(
    value: dict[str, Any], context: str
) -> dict[str, Any] | None:
    present = ANNOTATION_AUDIT_KEYS & set(value)
    if not present:
        return None
    if present != ANNOTATION_AUDIT_KEYS:
        raise ValueError(
            f"{context} annotation audit must define all four fields"
        )
    routing_batch_id = value["routing_batch_id"]
    if routing_batch_id is not None:
        routing_batch_id = _text(
            routing_batch_id, f"{context}.routing_batch_id"
        )
        try:
            parsed = uuid.UUID(routing_batch_id)
        except ValueError as error:
            raise ValueError(
                f"{context}.routing_batch_id must be a canonical UUID"
            ) from error
        if str(parsed) != routing_batch_id:
            raise ValueError(
                f"{context}.routing_batch_id must be a canonical UUID"
            )
    if value["annotation_tier"] != "gold":
        raise ValueError(
            f"{context}.annotation_tier must be gold for reviewed evaluation data"
        )
    if value["llm_unmodified"] is not False:
        raise ValueError(f"{context}.llm_unmodified must be false")
    if value["human_reviewed"] is not True:
        raise ValueError(f"{context}.human_reviewed must be true")
    return {
        "routing_batch_id": routing_batch_id,
        "annotation_tier": "gold",
        "llm_unmodified": False,
        "human_reviewed": True,
    }


def _validate_provenance(
    value: Any, context: str
) -> dict[str, Any] | None:
    provenance = _object(value, context)
    optional = {
        "proposal_id",
        "workbook_sha256",
        "legacy_status",
        "source_path_id",
        "row_number",
    }
    _require_allowed_keys(
        provenance,
        {"kind"},
        {"kind", *optional, *ANNOTATION_AUDIT_KEYS},
        context,
    )
    kind = provenance["kind"]
    if kind not in {"human", "xlsx", "lindera", "llm"}:
        raise ValueError(f"{context}.kind is invalid")
    for key in optional - {"row_number"}:
        if key in provenance:
            _text(provenance[key], f"{context}.{key}")
    if "row_number" in provenance and _positive_int(
        provenance["row_number"], f"{context}.row_number"
    ) < 2:
        raise ValueError(f"{context}.row_number must be at least 2")
    return _validate_annotation_audit(provenance, context)


def _validate_imported(value: Any, context: str) -> None:
    imported = _object(value, context)
    allowed = {
        "kind",
        "workbook_sha256",
        "row_number",
        "legacy_status",
        "legacy_validation",
        "legacy_marked_reading",
    }
    _require_allowed_keys(imported, set(), allowed, context)
    if not imported:
        return
    required = {"kind", "workbook_sha256", "row_number", "legacy_status"}
    if not required.issubset(imported):
        raise ValueError(f"{context} is missing imported workbook provenance")
    if imported["kind"] != "xlsx":
        raise ValueError(f"{context}.kind must be xlsx")
    _sha256(imported["workbook_sha256"], f"{context}.workbook_sha256")
    if _positive_int(imported["row_number"], f"{context}.row_number") < 2:
        raise ValueError(f"{context}.row_number must be at least 2")
    _text(imported["legacy_status"], f"{context}.legacy_status")
    for key in ("legacy_validation", "legacy_marked_reading"):
        item = imported.get(key)
        if item is not None:
            _text(item, f"{context}.{key}", allow_empty=True)


def _validate_path(
    value: Any,
    *,
    context: str,
    reading: str,
    surfaces: dict[str, str],
    expected_status: str,
) -> dict[str, Any]:
    path = _object(value, context)
    _require_exact_keys(
        path,
        {
            "path_id",
            "status",
            "surface_reference_id",
            "reading_boundaries",
            "surface_boundaries",
            "alignment_status",
            "provenance",
        },
        context,
    )
    path_id = _text(path["path_id"], f"{context}.path_id")
    if PATH_ID.fullmatch(path_id) is None:
        raise ValueError(f"{context}.path_id is invalid")
    if path["status"] != expected_status:
        raise ValueError(f"{context}.status must be {expected_status}")
    reference_id = _text(
        path["surface_reference_id"], f"{context}.surface_reference_id"
    )
    if reference_id not in surfaces:
        raise ValueError(f"{context}.surface_reference_id is unknown")
    reading_boundaries = _validate_boundaries(
        path["reading_boundaries"], len(reading), f"{context}.reading_boundaries"
    )
    alignment_status = path["alignment_status"]
    if alignment_status not in {"reading_only", "aligned"}:
        raise ValueError(f"{context}.alignment_status is invalid")
    if alignment_status == "reading_only":
        if path["surface_boundaries"] is not None:
            raise ValueError(f"{context}.surface_boundaries must be null")
        surface_boundaries = None
    else:
        surface_boundaries = _validate_boundaries(
            path["surface_boundaries"],
            len(surfaces[reference_id]),
            f"{context}.surface_boundaries",
        )
        if len(surface_boundaries) != len(reading_boundaries):
            raise ValueError(
                f"{context} aligned reading and surface chunk counts differ"
            )
    annotation_audit = _validate_provenance(
        path["provenance"], f"{context}.provenance"
    )
    return {
        "path_id": path_id,
        "surface_reference_id": reference_id,
        "reading_boundaries": reading_boundaries,
        "surface_boundaries": surface_boundaries,
        "alignment_status": alignment_status,
        "annotation_audit": annotation_audit,
    }


def _validate_export_record(record: dict[str, Any], context: str) -> dict[str, Any]:
    _require_exact_keys(
        record,
        {
            "schema",
            "id",
            "category",
            "source",
            "path_set_status",
            "needs_adjudication",
            "path_units",
            "acceptable_paths",
            "draft_paths",
            "review",
        },
        context,
    )
    if record["schema"] != ANNOTATION_EXPORT_SCHEMA:
        raise ValueError(f"{context}.schema must be {ANNOTATION_EXPORT_SCHEMA}")
    case_id = _text(record["id"], f"{context}.id")
    category = _text(record["category"], f"{context}.category")
    if record["path_set_status"] != "closed":
        raise ValueError(f"{context}.path_set_status must be closed")
    if record["needs_adjudication"] is not False:
        raise ValueError(f"{context}.needs_adjudication must be false")

    path_units = _object(record["path_units"], f"{context}.path_units")
    _require_exact_keys(
        path_units,
        {"reading_boundaries", "surface_boundaries"},
        f"{context}.path_units",
    )
    if path_units["reading_boundaries"] != ANNOTATION_READING_UNIT:
        raise ValueError(f"{context}.path_units.reading_boundaries is invalid")
    if path_units["surface_boundaries"] != SURFACE_UNIT:
        raise ValueError(f"{context}.path_units.surface_boundaries is invalid")

    source = _object(record["source"], f"{context}.source")
    _require_exact_keys(
        source,
        {
            "queue_sha256",
            "corpus_sha256",
            "row_sha256",
            "reading",
            "annotation_reading",
            "reading_unit",
            "annotation_reading_unit",
            "surface_unit",
            "surface_references",
        },
        f"{context}.source",
    )
    queue_sha256 = _sha256(source["queue_sha256"], f"{context}.source.queue_sha256")
    corpus_sha256 = _sha256(
        source["corpus_sha256"], f"{context}.source.corpus_sha256"
    )
    row_sha256 = _sha256(source["row_sha256"], f"{context}.source.row_sha256")
    reading = _text(source["reading"], f"{context}.source.reading")
    annotation_reading = _text(
        source["annotation_reading"], f"{context}.source.annotation_reading"
    )
    if "|" in reading or "|" in annotation_reading:
        raise ValueError(
            f"{context}.source readings contain reserved boundary marker '|'"
        )
    if source["reading_unit"] != SOURCE_READING_UNIT:
        raise ValueError(f"{context}.source.reading_unit is invalid")
    if source["annotation_reading_unit"] != ANNOTATION_READING_UNIT:
        raise ValueError(f"{context}.source.annotation_reading_unit is invalid")
    if source["surface_unit"] != SURFACE_UNIT:
        raise ValueError(f"{context}.source.surface_unit is invalid")

    surface_records = _array(
        source["surface_references"], f"{context}.source.surface_references"
    )
    if not surface_records:
        raise ValueError(f"{context}.source.surface_references must not be empty")
    surfaces: dict[str, str] = {}
    for index, raw_surface in enumerate(surface_records):
        surface_context = f"{context}.source.surface_references[{index}]"
        surface = _object(raw_surface, surface_context)
        _require_exact_keys(surface, {"id", "text"}, surface_context)
        expected_id = f"surface-{index}"
        if surface["id"] != expected_id:
            raise ValueError(f"{surface_context}.id must be {expected_id}")
        text = _text(surface["text"], f"{surface_context}.text")
        if text in surfaces.values():
            raise ValueError(f"{context}.source.surface_references duplicate text")
        surfaces[expected_id] = text

    draft_raw = _array(record["draft_paths"], f"{context}.draft_paths")
    drafts = [
        _validate_path(
            path,
            context=f"{context}.draft_paths[{index}]",
            reading=annotation_reading,
            surfaces=surfaces,
            expected_status="draft",
        )
        for index, path in enumerate(draft_raw)
    ]
    acceptable_raw = _array(
        record["acceptable_paths"], f"{context}.acceptable_paths"
    )
    if not acceptable_raw:
        raise ValueError(f"{context}.acceptable_paths must not be empty")
    acceptable = [
        _validate_path(
            path,
            context=f"{context}.acceptable_paths[{index}]",
            reading=annotation_reading,
            surfaces=surfaces,
            expected_status="acceptable",
        )
        for index, path in enumerate(acceptable_raw)
    ]
    path_ids = [path["path_id"] for path in [*acceptable, *drafts]]
    if len(path_ids) != len(set(path_ids)):
        raise ValueError(f"{context} contains duplicate path_id")
    semantic_paths = [
        (
            path["surface_reference_id"],
            tuple(path["reading_boundaries"]),
            None
            if path["surface_boundaries"] is None
            else tuple(path["surface_boundaries"]),
        )
        for path in acceptable
    ]
    if len(semantic_paths) != len(set(semantic_paths)):
        raise ValueError(f"{context}.acceptable_paths duplicate semantic path")

    review = _object(record["review"], f"{context}.review")
    required_review_keys = {
        "revision",
        "corrected_reading",
        "annotator_id",
        "reviewed_once",
        "updated_at",
        "notes",
        "imported",
    }
    _require_allowed_keys(
        review,
        required_review_keys,
        {*required_review_keys, *ANNOTATION_AUDIT_KEYS},
        f"{context}.review",
    )
    _nonnegative_int(review["revision"], f"{context}.review.revision")
    corrected_reading = review["corrected_reading"]
    if corrected_reading is None:
        if annotation_reading != reading:
            raise ValueError(
                f"{context}.source.annotation_reading differs without a correction"
            )
    else:
        corrected = _text(corrected_reading, f"{context}.review.corrected_reading")
        if corrected == reading or corrected != annotation_reading:
            raise ValueError(f"{context}.review.corrected_reading is inconsistent")
    _text(review["annotator_id"], f"{context}.review.annotator_id")
    if review["reviewed_once"] is not True:
        raise ValueError(f"{context}.review.reviewed_once must be true")
    if review["updated_at"] is not None:
        _text(review["updated_at"], f"{context}.review.updated_at")
    if review["notes"] is not None:
        _note(review["notes"], f"{context}.review.notes")
    _validate_imported(review["imported"], f"{context}.review.imported")
    review_audit = _validate_annotation_audit(
        review, f"{context}.review"
    )
    for path_kind, paths in (("acceptable", acceptable), ("draft", drafts)):
        for index, path in enumerate(paths):
            path_audit = path["annotation_audit"]
            if path_audit != review_audit:
                raise ValueError(
                    f"{context}.{path_kind}_paths[{index}] annotation audit "
                    "does not match review"
                )

    return {
        "id": case_id,
        "category": category,
        "queue_sha256": queue_sha256,
        "corpus_sha256": corpus_sha256,
        "row_sha256": row_sha256,
        "reading": annotation_reading,
        "reading_corrected": corrected_reading is not None,
        "surfaces": surfaces,
        "acceptable_paths": acceptable,
        "annotation_audit": review_audit,
    }


def _validate_annotation_manifest(
    value: Any, *, reviewed_paths_data: bytes, case_count: int
) -> dict[str, Any]:
    manifest = _object(value, "annotation manifest")
    _require_exact_keys(
        manifest,
        {
            "schema",
            "queue_sha256",
            "workbook_sha256",
            "reviewed_paths_sha256",
            "cases",
            "path_set_statuses",
            "complete",
            "formal_authorized",
            "diagnostic_only",
        },
        "annotation manifest",
    )
    if manifest["schema"] != ANNOTATION_MANIFEST_SCHEMA:
        raise ValueError(
            f"annotation manifest.schema must be {ANNOTATION_MANIFEST_SCHEMA}"
        )
    queue_sha256 = _sha256(manifest["queue_sha256"], "annotation manifest.queue_sha256")
    workbook_sha256 = manifest["workbook_sha256"]
    if workbook_sha256 is not None:
        _sha256(workbook_sha256, "annotation manifest.workbook_sha256")
    expected_review_sha = sha256_bytes(reviewed_paths_data)
    if _sha256(
        manifest["reviewed_paths_sha256"],
        "annotation manifest.reviewed_paths_sha256",
    ) != expected_review_sha:
        raise ValueError(
            "annotation manifest.reviewed_paths_sha256 does not match exact "
            "reviewed paths bytes"
        )
    cases = _positive_int(manifest["cases"], "annotation manifest.cases")
    if cases != case_count:
        raise ValueError(
            "annotation manifest.cases does not match reviewed path coverage"
        )
    statuses = _object(
        manifest["path_set_statuses"], "annotation manifest.path_set_statuses"
    )
    if statuses != {"closed": cases}:
        raise ValueError(
            "annotation manifest.path_set_statuses must contain only all closed cases"
        )
    if manifest["complete"] is not True:
        raise ValueError("annotation manifest.complete must be true")
    if manifest["formal_authorized"] is not False:
        raise ValueError("annotation manifest.formal_authorized must be false")
    if manifest["diagnostic_only"] is not True:
        raise ValueError("annotation manifest.diagnostic_only must be true")
    return {"queue_sha256": queue_sha256, "cases": cases}


def _span(count: int) -> dict[str, Any]:
    return {"start": 0, "count": count, "unit": COMPOSITION_ELEMENT_UNIT}


def _compile_record(
    record: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], dict[str, int]]:
    reading = record["reading"]
    probe = {
        "schema": PROBE_INPUT_SCHEMA,
        "id": record["id"],
        "category": record["category"],
        "elements": [
            {"text": character, "input_style": "direct"} for character in reading
        ],
    }

    span_counts: set[int] = set()
    aligned_chunks: set[tuple[int, str]] = set()
    aligned_count = 0
    reading_only_count = 0
    for path in record["acceptable_paths"]:
        reading_boundaries = path["reading_boundaries"]
        count = reading_boundaries[0] if reading_boundaries else len(reading)
        span_counts.add(count)
        if path["alignment_status"] == "aligned":
            aligned_count += 1
            surface = record["surfaces"][path["surface_reference_id"]]
            surface_boundaries = path["surface_boundaries"]
            surface_end = surface_boundaries[0] if surface_boundaries else len(surface)
            aligned_chunks.add((count, surface[:surface_end]))
        else:
            reading_only_count += 1

    acceptable_count = len(record["acceptable_paths"])
    if aligned_count == acceptable_count:
        surface_status = "fully_aligned"
    elif aligned_count:
        surface_status = "partially_aligned"
    else:
        surface_status = "not_aligned"
    if surface_status not in SURFACE_EVALUATION_STATUSES:
        raise AssertionError("unexpected surface evaluation status")

    target = {
        "schema": TARGET_SCHEMA,
        "id": record["id"],
        "category": record["category"],
        "reading": reading,
        "acceptable_first_spans": [_span(count) for count in sorted(span_counts)],
        "surface_evaluation_status": surface_status,
        "acceptable_first_chunks": [
            {"span": _span(count), "surface": surface}
            for count, surface in sorted(aligned_chunks)
        ],
        "path_counts": {
            "acceptable": acceptable_count,
            "aligned": aligned_count,
            "reading_only": reading_only_count,
        },
    }
    counts = {
        "paths": acceptable_count,
        "spans": len(span_counts),
        "chunks": len(aligned_chunks),
    }
    return probe, target, counts


def prepare_outputs_bytes(
    *, reviewed_paths_data: bytes, annotation_manifest_data: bytes
) -> dict[str, bytes]:
    """Derive the complete generation from already pinned exact input bytes."""

    if not isinstance(reviewed_paths_data, bytes):
        raise ValueError("reviewed_paths_data must be bytes")
    if not isinstance(annotation_manifest_data, bytes):
        raise ValueError("annotation_manifest_data must be bytes")
    reviewed_data = reviewed_paths_data
    manifest_data = annotation_manifest_data
    raw_records = _load_jsonl(reviewed_data, "reviewed paths")
    records = [
        _validate_export_record(record, f"reviewed paths:{index}")
        for index, record in enumerate(raw_records, 1)
    ]
    ids = [record["id"] for record in records]
    if len(ids) != len(set(ids)):
        raise ValueError("reviewed paths contain duplicate case ids")
    source_queue_hashes = {record["queue_sha256"] for record in records}
    if len(source_queue_hashes) != 1:
        raise ValueError("reviewed paths change source.queue_sha256")
    source_corpus_hashes = {record["corpus_sha256"] for record in records}
    if len(source_corpus_hashes) != 1:
        raise ValueError("reviewed paths change source.corpus_sha256")
    row_hashes = [record["row_sha256"] for record in records]
    if len(row_hashes) != len(set(row_hashes)):
        raise ValueError("reviewed paths contain duplicate source.row_sha256")
    raw_manifest = _load_json_bytes(manifest_data, "annotation manifest")
    validated_manifest = _validate_annotation_manifest(
        raw_manifest, reviewed_paths_data=reviewed_data, case_count=len(records)
    )
    if source_queue_hashes != {validated_manifest["queue_sha256"]}:
        raise ValueError(
            "reviewed path source.queue_sha256 does not match annotation manifest"
        )

    probe_records: list[dict[str, Any]] = []
    target_records: list[dict[str, Any]] = []
    path_total = 0
    span_total = 0
    chunk_total = 0
    for record in records:
        probe, target, counts = _compile_record(record)
        probe_records.append(probe)
        target_records.append(target)
        path_total += counts["paths"]
        span_total += counts["spans"]
        chunk_total += counts["chunks"]

    probe_data = _render_jsonl(probe_records)
    targets_data = _render_jsonl(target_records)
    category_counts = dict(
        sorted(Counter(record["category"] for record in records).items())
    )
    surface_counts = dict(
        sorted(
            Counter(
                target["surface_evaluation_status"] for target in target_records
            ).items()
        )
    )
    generation_manifest = {
        "schema": GENERATION_MANIFEST_SCHEMA,
        "diagnostic_only": True,
        "formal_authorized": False,
        "bindings": {
            "reviewed_paths": {
                "path": SOURCE_REVIEWED_PATHS_NAME,
                "schema": ANNOTATION_EXPORT_SCHEMA,
                "sha256": sha256_bytes(reviewed_data),
                "cases": len(records),
            },
            "annotation_manifest": {
                "path": SOURCE_ANNOTATION_MANIFEST_NAME,
                "schema": ANNOTATION_MANIFEST_SCHEMA,
                "sha256": sha256_bytes(manifest_data),
                "cases": len(records),
                "complete": True,
            },
            "probe_input": {
                "path": PROBE_INPUT_NAME,
                "schema": PROBE_INPUT_SCHEMA,
                "sha256": sha256_bytes(probe_data),
                "cases": len(records),
            },
            "targets": {
                "path": TARGETS_NAME,
                "schema": TARGET_SCHEMA,
                "sha256": sha256_bytes(targets_data),
                "cases": len(records),
            },
        },
        "category_counts": category_counts,
        "surface_evaluation_status_counts": surface_counts,
        "corrected_reading_cases": sum(
            1 for record in records if record["reading_corrected"]
        ),
        "total_acceptable_paths": path_total,
        "total_acceptable_first_spans": span_total,
        "total_acceptable_first_chunks": chunk_total,
        "contracts": {
            "annotation_reading_source": "source.annotation_reading",
            "composition_element_mapping": (
                "one-NFC-code-point-per-direct-composition-element.v1"
            ),
            "first_segment_target": "first-reading-boundary-or-full-reading.v1",
            "multiple_acceptable_paths": (
                "preserved-as-deduplicated-first-segment-targets.v1"
            ),
            "surface_evaluation": "fully-aligned-cases-only.v1",
        },
    }
    return {
        SOURCE_REVIEWED_PATHS_NAME: reviewed_data,
        SOURCE_ANNOTATION_MANIFEST_NAME: manifest_data,
        PROBE_INPUT_NAME: probe_data,
        TARGETS_NAME: targets_data,
        MANIFEST_NAME: _render_json(generation_manifest),
    }


def prepare_outputs(
    *, reviewed_paths_path: Path, annotation_manifest_path: Path
) -> dict[str, bytes]:
    reviewed_data = _read_regular(reviewed_paths_path, "reviewed paths")
    manifest_data = _read_regular(annotation_manifest_path, "annotation manifest")
    return prepare_outputs_bytes(
        reviewed_paths_data=reviewed_data,
        annotation_manifest_data=manifest_data,
    )


def write_outputs(*, generated: dict[str, bytes], output_dir: Path) -> None:
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    if output_dir.exists() or output_dir.is_symlink():
        raise ValueError("output directory already exists")
    staging = Path(
        tempfile.mkdtemp(prefix=f".{output_dir.name}.staging-", dir=output_dir.parent)
    )
    try:
        for name, data in generated.items():
            path = staging / name
            path.write_bytes(data)
        staging.rename(output_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reviewed-paths", type=Path, required=True)
    parser.add_argument("--annotation-manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        generated = prepare_outputs(
            reviewed_paths_path=args.reviewed_paths,
            annotation_manifest_path=args.annotation_manifest,
        )
        write_outputs(generated=generated, output_dir=args.output_dir)
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(f"{sha256_bytes(generated[MANIFEST_NAME])} {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
