#!/usr/bin/env python3
"""Prepare a candidate-blind Silver annotation source and server queue seed.

The authoritative input keeps source text and family assignment together for
review.  This compiler deliberately separates them in its outputs:

* ``source.jsonl`` contains only candidate-blind annotation source fields.
* ``assignment.jsonl`` contains family, selection-role, and fold metadata.
* ``context.jsonl`` contains every case's left context, including empty values.
* optional ``selection.jsonl`` contains runtime-only disagreement features.
* ``queue-seed.jsonl`` is compatible with the existing boundary annotation
  server and contains no engine candidates, scores, or assignment metadata.

All outputs are deterministic functions of the exact input bytes and are bound
by ``manifest.json``.  This command prepares Silver annotation work; it does
not authorize a quality holdout or expose final-locked labels to an evaluator.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from typing import Any, Iterable
import unicodedata


CASE_SCHEMA = "hazkey.blind-silver-annotation-case.v1"
SOURCE_SCHEMA = "hazkey.blind-silver-annotation-source.v1"
ASSIGNMENT_SCHEMA = "hazkey.blind-silver-annotation-assignment.v1"
CONTEXT_SCHEMA = "hazkey.blind-silver-left-context.v1"
SELECTION_INPUT_SCHEMA = "hazkey.blind-silver-selection-input.v1"
SELECTION_SCHEMA = "hazkey.blind-silver-selection-metadata.v1"
MANIFEST_SCHEMA = "hazkey.blind-silver-annotation-generation.v1"

# Existing server input contract.  It is repeated instead of importing the
# server so this compiler remains a small, dependency-free CLI.
QUEUE_SCHEMA = "hazkey.mozc-hybrid-boundary-preannotation.v1"
QUEUE_ELEMENT_UNIT = "source_reading_code_point"

SOURCE_NAME = "source.jsonl"
ASSIGNMENT_NAME = "assignment.jsonl"
CONTEXT_NAME = "context.jsonl"
EMPTY_CONTEXT_NAME = "context-empty.jsonl"
QUEUE_SEED_NAME = "queue-seed.jsonl"
SELECTION_NAME = "selection.jsonl"
MANIFEST_NAME = "manifest.json"

DATASET_ROLES = frozenset({"representative", "disagreement_enriched"})
FOLDS = frozenset({"exploration", "final_locked"})
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
MAX_INPUT_BYTES = 128 * 1024 * 1024
MAX_READING_CODE_POINTS = 4096
MAX_SURFACE_CODE_POINTS = 4096
MAX_SURFACE_REFERENCES = 32
MAX_SOURCE_REVISION_CODE_POINTS = 512
MAX_LEFT_CONTEXT_CODE_POINTS = 4096
SCORE_SCOPES = frozenset({"full_candidate", "constraint_suffix"})

CASE_FIELDS = {
    "schema",
    "id",
    "family_id",
    "source_revision",
    "dataset_role",
    "fold",
    "reading",
    "surface_references",
    "left_context",
}
SELECTION_INPUT_FIELDS = {
    "schema",
    "id",
    "source_revision",
    "selection_policy_revision",
    "selection_reasons",
    "runtime_features",
}
SELECTION_RUNTIME_FIELDS = {
    "top1_surface_differs",
    "top1_boundary_differs",
    "mozc_top1_consuming_count",
    "hazkey_zenzai_top1_consuming_count",
    "zenzai_score",
    "zenzai_score_token_count",
    "zenzai_score_scope",
    "zenzai_score_band_id",
    "normalized_candidate_overlap_count",
    "mozc_only_candidate_count",
    "hazkey_zenzai_only_candidate_count",
}
GOLD_DERIVED_TOKENS = frozenset(
    {
        "acceptable",
        "correct",
        "expected",
        "gold",
        "label",
        "reference",
        "target",
        "truth",
    }
)


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
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
    actual = set(value)
    if actual != expected_set:
        raise ValueError(
            f"{context} fields do not match schema; "
            f"missing={sorted(expected_set - actual)!r}, "
            f"unknown={sorted(actual - expected_set)!r}"
        )


def _is_noncharacter(character: str) -> bool:
    value = ord(character)
    return 0xFDD0 <= value <= 0xFDEF or value & 0xFFFF in {0xFFFE, 0xFFFF}


def _text(
    value: Any,
    context: str,
    *,
    maximum_code_points: int,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        qualifier = "a string" if allow_empty else "a non-empty string"
        raise ValueError(f"{context} must be {qualifier}")
    if len(value) > maximum_code_points:
        raise ValueError(
            f"{context} exceeds {maximum_code_points} Unicode code points"
        )
    if value != unicodedata.normalize("NFC", value):
        raise ValueError(f"{context} must be NFC-normalized")
    for character in value:
        category = unicodedata.category(character)
        if category in {"Cc", "Cs"} or character == "\ufeff" or _is_noncharacter(
            character
        ):
            raise ValueError(
                f"{context} contains a control, surrogate, BOM, or noncharacter"
            )
    return value


def _identifier(value: Any, context: str) -> str:
    result = _text(value, context, maximum_code_points=128)
    if IDENTIFIER.fullmatch(result) is None:
        raise ValueError(
            f"{context} must match {IDENTIFIER.pattern!r} for server-safe IDs"
        )
    return result


def _boolean(value: Any, context: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{context} must be boolean")
    return value


def _nonnegative_int(value: Any, context: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{context} must be a non-negative integer")
    return value


def _nullable_positive_int(value: Any, context: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value <= 0:
        raise ValueError(f"{context} must be null or a positive integer")
    return value


def _nullable_finite_number(value: Any, context: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{context} must be null or a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{context} must be null or a finite number")
    return result


def _selection_identifier(value: Any, context: str) -> str:
    result = _identifier(value, context)
    lowered_tokens = {token for token in re.split(r"[._:-]+", result.casefold()) if token}
    forbidden = lowered_tokens & GOLD_DERIVED_TOKENS
    if forbidden:
        raise ValueError(
            f"{context} contains forbidden gold-derived token(s) {sorted(forbidden)!r}"
        )
    return result


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _render_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _render_jsonl(records: Iterable[dict[str, Any]]) -> bytes:
    return b"".join(_canonical_json(record) + b"\n" for record in records)


def _load_jsonl(data: bytes, context: str) -> list[dict[str, Any]]:
    if len(data) > MAX_INPUT_BYTES:
        raise ValueError(f"{context} exceeds the {MAX_INPUT_BYTES}-byte limit")
    if not data or data.startswith(b"\xef\xbb\xbf") or b"\r" in data:
        raise ValueError(f"{context} must be BOM-free UTF-8 JSONL with LF endings")
    if not data.endswith(b"\n"):
        raise ValueError(f"{context} must end with one LF")
    records: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(data[:-1].split(b"\n"), 1):
        if not raw_line:
            raise ValueError(f"{context}:{line_number} must not be blank")
        try:
            value = json.loads(
                raw_line.decode("utf-8"),
                object_pairs_hook=_object_without_duplicate_keys,
            )
        except UnicodeDecodeError as error:
            raise ValueError(f"{context}:{line_number} is not valid UTF-8") from error
        except json.JSONDecodeError as error:
            raise ValueError(
                f"{context}:{line_number} is invalid JSON: {error.msg}"
            ) from error
        if not isinstance(value, dict):
            raise ValueError(f"{context}:{line_number} must contain an object")
        records.append(value)
    if not records:
        raise ValueError(f"{context} must contain at least one case")
    return records


def _validate_case(value: dict[str, Any], context: str) -> dict[str, Any]:
    _require_exact_keys(value, CASE_FIELDS, context)
    if value["schema"] != CASE_SCHEMA:
        raise ValueError(f"{context}.schema must be {CASE_SCHEMA}")
    case_id = _identifier(value["id"], f"{context}.id")
    family_id = _identifier(value["family_id"], f"{context}.family_id")
    source_revision = _text(
        value["source_revision"],
        f"{context}.source_revision",
        maximum_code_points=MAX_SOURCE_REVISION_CODE_POINTS,
    )
    dataset_role = value["dataset_role"]
    if dataset_role not in DATASET_ROLES:
        raise ValueError(
            f"{context}.dataset_role must be one of {sorted(DATASET_ROLES)!r}"
        )
    fold = value["fold"]
    if fold not in FOLDS:
        raise ValueError(f"{context}.fold must be one of {sorted(FOLDS)!r}")
    reading = _text(
        value["reading"],
        f"{context}.reading",
        maximum_code_points=MAX_READING_CODE_POINTS,
    )
    if "|" in reading:
        raise ValueError(f"{context}.reading contains reserved boundary marker '|'")
    left_context = _text(
        value["left_context"],
        f"{context}.left_context",
        maximum_code_points=MAX_LEFT_CONTEXT_CODE_POINTS,
        allow_empty=True,
    )
    raw_surfaces = value["surface_references"]
    if not isinstance(raw_surfaces, list) or not raw_surfaces:
        raise ValueError(f"{context}.surface_references must be a non-empty array")
    if len(raw_surfaces) > MAX_SURFACE_REFERENCES:
        raise ValueError(
            f"{context}.surface_references exceeds {MAX_SURFACE_REFERENCES} entries"
        )
    surfaces = [
        _text(
            surface,
            f"{context}.surface_references[{index}]",
            maximum_code_points=MAX_SURFACE_CODE_POINTS,
        )
        for index, surface in enumerate(raw_surfaces)
    ]
    if len(surfaces) != len(set(surfaces)):
        raise ValueError(f"{context}.surface_references contains duplicates")
    return {
        "schema": CASE_SCHEMA,
        "id": case_id,
        "family_id": family_id,
        "source_revision": source_revision,
        "dataset_role": dataset_role,
        "fold": fold,
        "reading": reading,
        "surface_references": surfaces,
        "left_context": left_context,
    }


def load_cases_bytes(data: bytes, context: str = "blind Silver cases") -> list[dict[str, Any]]:
    cases = [
        _validate_case(record, f"{context}:{index}")
        for index, record in enumerate(_load_jsonl(data, context), 1)
    ]
    seen_ids: set[str] = set()
    seen_semantic_sources: dict[tuple[str, tuple[str, ...], str], str] = {}
    family_assignments: dict[str, tuple[str, str]] = {}
    for case in cases:
        case_id = case["id"]
        if case_id in seen_ids:
            raise ValueError(f"duplicate case id {case_id!r}")
        seen_ids.add(case_id)

        semantic_key = (
            case["reading"],
            tuple(sorted(case["surface_references"])),
            case["left_context"],
        )
        previous_id = seen_semantic_sources.get(semantic_key)
        if previous_id is not None:
            raise ValueError(
                f"duplicate annotation source in {previous_id!r} and {case_id!r}"
            )
        seen_semantic_sources[semantic_key] = case_id

        assignment = (case["dataset_role"], case["fold"])
        previous_assignment = family_assignments.get(case["family_id"])
        if previous_assignment is not None and previous_assignment != assignment:
            previous_role, previous_fold = previous_assignment
            if previous_fold != case["fold"]:
                raise ValueError(
                    f"family {case['family_id']!r} crosses folds "
                    f"{previous_fold!r} and {case['fold']!r}"
                )
            raise ValueError(
                f"family {case['family_id']!r} changes dataset_role from "
                f"{previous_role!r} to {case['dataset_role']!r}"
            )
        family_assignments[case["family_id"]] = assignment
    return cases


def _input_shape(reading: str) -> dict[str, int]:
    counts = {
        "hiragana_count": 0,
        "katakana_count": 0,
        "han_count": 0,
        "latin_count": 0,
        "digit_count": 0,
        "other_count": 0,
    }
    for character in reading:
        value = ord(character)
        name = unicodedata.name(character, "")
        if 0x3040 <= value <= 0x309F:
            key = "hiragana_count"
        elif 0x30A0 <= value <= 0x30FF or 0x31F0 <= value <= 0x31FF:
            key = "katakana_count"
        elif (
            0x3400 <= value <= 0x4DBF
            or 0x4E00 <= value <= 0x9FFF
            or 0xF900 <= value <= 0xFAFF
            or "CJK UNIFIED IDEOGRAPH" in name
        ):
            key = "han_count"
        elif "LATIN" in name:
            key = "latin_count"
        elif unicodedata.category(character) == "Nd":
            key = "digit_count"
        else:
            key = "other_count"
        counts[key] += 1
    return counts


def _validate_selection_record(
    value: dict[str, Any],
    context: str,
    *,
    cases_by_id: dict[str, dict[str, Any]],
    sources_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    _require_exact_keys(value, SELECTION_INPUT_FIELDS, context)
    if value["schema"] != SELECTION_INPUT_SCHEMA:
        raise ValueError(f"{context}.schema must be {SELECTION_INPUT_SCHEMA}")
    case_id = _identifier(value["id"], f"{context}.id")
    case = cases_by_id.get(case_id)
    if case is None:
        raise ValueError(f"{context}.id refers to unknown case {case_id!r}")
    if case["dataset_role"] != "disagreement_enriched":
        raise ValueError(
            f"{context}.id must name a disagreement_enriched case, not "
            f"{case['dataset_role']!r}"
        )
    source_revision = _text(
        value["source_revision"],
        f"{context}.source_revision",
        maximum_code_points=MAX_SOURCE_REVISION_CODE_POINTS,
    )
    if source_revision != case["source_revision"]:
        raise ValueError(f"{context}.source_revision does not match its source case")
    policy_revision = _selection_identifier(
        value["selection_policy_revision"],
        f"{context}.selection_policy_revision",
    )
    raw_reasons = value["selection_reasons"]
    if not isinstance(raw_reasons, list) or not raw_reasons:
        raise ValueError(f"{context}.selection_reasons must be a non-empty array")
    reasons = [
        _selection_identifier(reason, f"{context}.selection_reasons[{index}]")
        for index, reason in enumerate(raw_reasons)
    ]
    if len(reasons) != len(set(reasons)):
        raise ValueError(f"{context}.selection_reasons contains duplicates")

    raw_features = value["runtime_features"]
    if not isinstance(raw_features, dict):
        raise ValueError(f"{context}.runtime_features must be an object")
    _require_exact_keys(
        raw_features, SELECTION_RUNTIME_FIELDS, f"{context}.runtime_features"
    )
    surface_differs = _boolean(
        raw_features["top1_surface_differs"],
        f"{context}.runtime_features.top1_surface_differs",
    )
    boundary_differs = _boolean(
        raw_features["top1_boundary_differs"],
        f"{context}.runtime_features.top1_boundary_differs",
    )
    mozc_count = _nullable_positive_int(
        raw_features["mozc_top1_consuming_count"],
        f"{context}.runtime_features.mozc_top1_consuming_count",
    )
    hazkey_count = _nullable_positive_int(
        raw_features["hazkey_zenzai_top1_consuming_count"],
        f"{context}.runtime_features.hazkey_zenzai_top1_consuming_count",
    )
    if (mozc_count is None) != (hazkey_count is None):
        raise ValueError(f"{context}.runtime_features Top-1 counts must both be null or present")
    if mozc_count is not None and (
        mozc_count > len(case["reading"]) or hazkey_count > len(case["reading"])
    ):
        raise ValueError(
            f"{context}.runtime_features Top-1 count exceeds reading length"
        )
    if mozc_count is not None and boundary_differs != (mozc_count != hazkey_count):
        raise ValueError(
            f"{context}.runtime_features.top1_boundary_differs disagrees with counts"
        )
    if mozc_count is None and boundary_differs:
        raise ValueError(
            f"{context}.runtime_features.top1_boundary_differs cannot be true without counts"
        )

    score = _nullable_finite_number(
        raw_features["zenzai_score"],
        f"{context}.runtime_features.zenzai_score",
    )
    score_tokens = _nullable_positive_int(
        raw_features["zenzai_score_token_count"],
        f"{context}.runtime_features.zenzai_score_token_count",
    )
    score_scope = raw_features["zenzai_score_scope"]
    if score_scope is not None and score_scope not in SCORE_SCOPES:
        raise ValueError(
            f"{context}.runtime_features.zenzai_score_scope must be null or one of "
            f"{sorted(SCORE_SCOPES)!r}"
        )
    if (score is None, score_tokens is None, score_scope is None) not in {
        (True, True, True),
        (False, False, False),
    }:
        raise ValueError(
            f"{context}.runtime_features Zenzai score/count/scope must be all null or present"
        )
    score_band_id = _selection_identifier(
        raw_features["zenzai_score_band_id"],
        f"{context}.runtime_features.zenzai_score_band_id",
    )

    features = {
        "top1_surface_differs": surface_differs,
        "top1_boundary_differs": boundary_differs,
        "mozc_top1_consuming_count": mozc_count,
        "hazkey_zenzai_top1_consuming_count": hazkey_count,
        "zenzai_score": score,
        "zenzai_score_token_count": score_tokens,
        "zenzai_score_scope": score_scope,
        "zenzai_score_per_token": (
            None if score is None else score / score_tokens
        ),
        "zenzai_score_band_id": score_band_id,
        "normalized_candidate_overlap_count": _nonnegative_int(
            raw_features["normalized_candidate_overlap_count"],
            f"{context}.runtime_features.normalized_candidate_overlap_count",
        ),
        "mozc_only_candidate_count": _nonnegative_int(
            raw_features["mozc_only_candidate_count"],
            f"{context}.runtime_features.mozc_only_candidate_count",
        ),
        "hazkey_zenzai_only_candidate_count": _nonnegative_int(
            raw_features["hazkey_zenzai_only_candidate_count"],
            f"{context}.runtime_features.hazkey_zenzai_only_candidate_count",
        ),
        "reading_length": len(case["reading"]),
        "input_shape": _input_shape(case["reading"]),
    }
    return {
        "schema": SELECTION_SCHEMA,
        "id": case_id,
        "source_revision": source_revision,
        "source_content_sha256": sources_by_id[case_id]["content_sha256"],
        "selection_policy_revision": policy_revision,
        "selection_reasons": reasons,
        "runtime_features": features,
    }


def load_selection_bytes(
    data: bytes,
    *,
    cases: list[dict[str, Any]],
    source_records: list[dict[str, Any]],
    context: str = "selection metadata",
) -> list[dict[str, Any]]:
    cases_by_id = {case["id"]: case for case in cases}
    sources_by_id = {record["id"]: record for record in source_records}
    records = [
        _validate_selection_record(
            raw,
            f"{context}:{index}",
            cases_by_id=cases_by_id,
            sources_by_id=sources_by_id,
        )
        for index, raw in enumerate(_load_jsonl(data, context), 1)
    ]
    ids = [record["id"] for record in records]
    if len(ids) != len(set(ids)):
        raise ValueError(f"{context} contains duplicate case ids")
    expected_ids = {
        case["id"]
        for case in cases
        if case["dataset_role"] == "disagreement_enriched"
    }
    if set(ids) != expected_ids:
        raise ValueError(
            f"{context} must cover every disagreement_enriched case exactly; "
            f"missing={sorted(expected_ids - set(ids))!r}, "
            f"unexpected={sorted(set(ids) - expected_ids)!r}"
        )
    policy_revisions = {record["selection_policy_revision"] for record in records}
    if len(policy_revisions) != 1:
        raise ValueError(f"{context} changes selection_policy_revision")
    by_id = {record["id"]: record for record in records}
    return [
        by_id[case["id"]]
        for case in cases
        if case["dataset_role"] == "disagreement_enriched"
    ]


def _source_payload(case: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": SOURCE_SCHEMA,
        "id": case["id"],
        "source_revision": case["source_revision"],
        "reading": case["reading"],
        "surface_references": list(case["surface_references"]),
    }


def _source_record(case: dict[str, Any]) -> dict[str, Any]:
    payload = _source_payload(case)
    return {**payload, "content_sha256": _sha256(_canonical_json(payload))}


def _assignment_record(
    case: dict[str, Any], source_record: dict[str, Any]
) -> dict[str, Any]:
    return {
        "schema": ASSIGNMENT_SCHEMA,
        "id": case["id"],
        "family_id": case["family_id"],
        "dataset_role": case["dataset_role"],
        "fold": case["fold"],
        "source_content_sha256": source_record["content_sha256"],
        "input_case_sha256": _sha256(_canonical_json(case)),
    }


def _context_record(
    case: dict[str, Any],
    source_record: dict[str, Any],
    *,
    empty_baseline: bool = False,
) -> dict[str, Any]:
    left_context = "" if empty_baseline else case["left_context"]
    return {
        "schema": CONTEXT_SCHEMA,
        "id": case["id"],
        "source_content_sha256": source_record["content_sha256"],
        "left_context": left_context,
        "left_context_sha256": _sha256(left_context.encode("utf-8")),
    }


def _queue_record(
    case: dict[str, Any], source_record: dict[str, Any], source_sha256: str
) -> dict[str, Any]:
    reading = case["reading"]
    return {
        "schema": QUEUE_SCHEMA,
        "id": case["id"],
        # A constant category prevents selection role/fold from influencing the
        # existing server's category-biased few-shot retrieval.
        "category": "blind-silver",
        "source": {
            "corpus_sha256": source_sha256,
            "row_sha256": source_record["content_sha256"],
            "reading": reading,
            "expected_surfaces": list(case["surface_references"]),
        },
        "elements": {
            "unit": QUEUE_ELEMENT_UNIT,
            "values": [
                {"index": index, "text": character}
                for index, character in enumerate(reading)
            ],
        },
        "known_source_reused": False,
        "diagnostic_only": True,
        "formal_authorized": False,
        "candidate_outputs_consulted": False,
        # The existing server expects these two fields when constructing an LLM
        # request.  They repeat only the source reading and explicitly state
        # that no machine preannotation exists.
        "preannotation": {
            "selected_alternative_index": 0,
            "marked_reading": reading,
            "segments": [],
            "boundaries_after": [],
            "first_segment_count": len(reading),
            "confidence": "unavailable",
            "alignment_distance": None,
            "alignment_rate_basis_points": None,
            "ambiguity": ["blind_silver_no_machine_preannotation"],
            "alternative_boundary_disagreement": False,
            "alternatives": [],
        },
        "token_audit": {"alternatives": []},
        "review": {
            "status": "pending",
            "annotator_id": None,
            "marked_reading": None,
            "first_segment_count": None,
            "surfaces": [],
            "notes": None,
        },
    }


def prepare_outputs_bytes(
    cases_data: bytes, selection_data: bytes | None = None
) -> dict[str, bytes]:
    cases = load_cases_bytes(cases_data)
    source_records = [_source_record(case) for case in cases]
    source_data = _render_jsonl(source_records)
    source_sha256 = _sha256(source_data)
    assignment_records = [
        _assignment_record(case, source_record)
        for case, source_record in zip(cases, source_records, strict=True)
    ]
    assignment_data = _render_jsonl(assignment_records)
    context_records = [
        _context_record(case, source_record)
        for case, source_record in zip(cases, source_records, strict=True)
    ]
    context_data = _render_jsonl(context_records)
    empty_context_data = _render_jsonl(
        _context_record(case, source_record, empty_baseline=True)
        for case, source_record in zip(cases, source_records, strict=True)
    )
    queue_data = _render_jsonl(
        _queue_record(case, source_record, source_sha256)
        for case, source_record in zip(cases, source_records, strict=True)
    )
    if selection_data is None:
        selection_records: list[dict[str, Any]] = []
        rendered_selection: bytes | None = None
        selection_input_binding: dict[str, Any] | None = None
        selection_binding: dict[str, Any] | None = None
    else:
        selection_records = load_selection_bytes(
            selection_data,
            cases=cases,
            source_records=source_records,
        )
        rendered_selection = _render_jsonl(selection_records)
        selection_input_binding = {
            "schema": SELECTION_INPUT_SCHEMA,
            "sha256": _sha256(selection_data),
            "records": len(selection_records),
        }
        selection_binding = {
            "path": SELECTION_NAME,
            "schema": SELECTION_SCHEMA,
            "sha256": _sha256(rendered_selection),
            "records": len(selection_records),
        }

    role_counts = Counter(case["dataset_role"] for case in cases)
    fold_counts = Counter(case["fold"] for case in cases)
    role_fold_counts = Counter(
        f"{case['dataset_role']}:{case['fold']}" for case in cases
    )
    family_counts = Counter(case["family_id"] for case in cases)
    required_selection_count = sum(
        1 for case in cases if case["dataset_role"] == "disagreement_enriched"
    )
    final_locked_families = {
        case["family_id"] for case in cases if case["fold"] == "final_locked"
    }
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "annotation_tier": "silver_seed",
        "formal_authorized": False,
        "bindings": {
            "input_cases": {
                "schema": CASE_SCHEMA,
                "sha256": _sha256(cases_data),
                "cases": len(cases),
            },
            "source": {
                "path": SOURCE_NAME,
                "schema": SOURCE_SCHEMA,
                "sha256": source_sha256,
                "cases": len(cases),
            },
            "assignment": {
                "path": ASSIGNMENT_NAME,
                "schema": ASSIGNMENT_SCHEMA,
                "sha256": _sha256(assignment_data),
                "cases": len(cases),
            },
            "context": {
                "path": CONTEXT_NAME,
                "schema": CONTEXT_SCHEMA,
                "sha256": _sha256(context_data),
                "cases": len(cases),
            },
            "empty_context": {
                "path": EMPTY_CONTEXT_NAME,
                "schema": CONTEXT_SCHEMA,
                "sha256": _sha256(empty_context_data),
                "cases": len(cases),
            },
            "selection_input": selection_input_binding,
            "selection": selection_binding,
            "queue_seed": {
                "path": QUEUE_SEED_NAME,
                "schema": QUEUE_SCHEMA,
                "sha256": _sha256(queue_data),
                "cases": len(cases),
            },
        },
        "counts": {
            "cases": len(cases),
            "families": len(family_counts),
            "multi_case_families": sum(
                1 for count in family_counts.values() if count > 1
            ),
            "dataset_roles": dict(sorted(role_counts.items())),
            "folds": dict(sorted(fold_counts.items())),
            "dataset_role_by_fold": dict(sorted(role_fold_counts.items())),
            "final_locked_families": len(final_locked_families),
            "nonempty_left_context_cases": sum(
                1 for case in cases if case["left_context"]
            ),
            "empty_left_context_cases": sum(
                1 for case in cases if not case["left_context"]
            ),
            "selection_metadata_records": len(selection_records),
            "selection_metadata_required_cases": required_selection_count,
        },
        "contracts": {
            "source_order": "exact-input-order",
            "family_fold_disjoint": True,
            "family_dataset_role_consistent": True,
            "semantic_source_duplicates_rejected": True,
            "unicode": "NFC-valid-scalars-no-controls-or-noncharacters",
            "selection_assignment_metadata": "assignment-sidecar-only",
            "selection_runtime_metadata": "selection-sidecar-only",
            "selection_runtime_features_are_gold_free": True,
            "selection_runtime_metadata_complete": (
                len(selection_records) == required_selection_count
            ),
            "selection_sidecar_contains_candidate_surfaces": False,
            "queue_assignment_fields": [],
            "queue_selection_fields": [],
            "queue_category": "blind-silver-constant",
            "queue_machine_preannotation": "none",
            "llm_annotation_payload_candidate_outputs_consulted": False,
            "llm_annotation_payload_engine_candidates_or_scores": False,
            "left_context_storage": "context-sidecar-only",
            "left_context_all_cases_explicit": True,
            "empty_context_baseline_all_cases_explicit": True,
            "empty_context_baseline_source_bindings_equal": True,
            "left_context_exact_utf8_sha256_bound": True,
            "left_context_llm_annotation_payload_included": False,
            "left_context_engine_or_score_derived": False,
            "left_context_gold_label_derived": False,
            "final_locked_labels_disclosed": False,
        },
    }
    generated = {
        SOURCE_NAME: source_data,
        ASSIGNMENT_NAME: assignment_data,
        CONTEXT_NAME: context_data,
        EMPTY_CONTEXT_NAME: empty_context_data,
        QUEUE_SEED_NAME: queue_data,
        MANIFEST_NAME: _render_json(manifest),
    }
    if rendered_selection is not None:
        generated[SELECTION_NAME] = rendered_selection
    return generated


def write_outputs(generated: dict[str, bytes], output_dir: Path) -> None:
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    if output_dir.exists() or output_dir.is_symlink():
        raise ValueError("output directory already exists")
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{output_dir.name}.staging-", dir=output_dir.parent
        )
    )
    try:
        for name, data in generated.items():
            path = staging / name
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            try:
                with os.fdopen(descriptor, "wb", closefd=True) as output:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
            except BaseException:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                raise
        staging.rename(output_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare candidate-blind Silver annotation source, assignment "
            "sidecar, and existing-server queue seed."
        )
    )
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--selection-metadata", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        cases_data = args.cases.read_bytes()
        selection_data = (
            None
            if args.selection_metadata is None
            else args.selection_metadata.read_bytes()
        )
        generated = prepare_outputs_bytes(cases_data, selection_data)
        write_outputs(generated, args.output_dir)
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(f"{_sha256(generated[MANIFEST_NAME])} {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
