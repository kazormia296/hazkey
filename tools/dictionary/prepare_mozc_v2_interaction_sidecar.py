#!/usr/bin/env python3
"""Prepare a non-authoritative interaction-model sidecar for sealed Mozc v2.

The sidecar contains only the sealed corpus's ASCII-containing cases.  Cases
that can be split mechanically after their final ASCII scalar receive a draft
normal-input-context candidate; every other case remains unclassified pending
an explicit action-trace review.  Output is canonical UTF-8 JSON on stdout.
This tool never makes the draft formally authoritative and never writes a
generated file.  A proposal seeds the prefix as context at the conversion
boundary; it does not infer physical keys, input mapping style, or earlier
conversions that created that already-committed context.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))

from tools.dictionary import audit_mozc_v2_interaction_model as interaction_audit


SCHEMA = "hazkey.mozc-v2-interaction-sidecar-draft.v1"
SEALED_GENERATION = (
    "sealed-v2-sha256-"
    "b4c1351b1b0ef7797349ebf26858db4d0dd69ce1c8bcbfaee88e0f0b644225ed"
)
SEALED_GENERATION_SHA256 = (
    "sha256:b4c1351b1b0ef7797349ebf26858db4d0dd69ce1c8bcbfaee88e0f0b644225ed"
)
SEALED_CORPUS_SHA256 = (
    "sha256:cdb2a017b4548f6f77ec3d466f84ec09268a74adb5e876e224e01069f128c8ae"
)
CORPUS_NAME = "formal-corpus.tsv"


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _read_bound_input(path: Path) -> bytes:
    """Use the audit module's descriptor primitives to read one pinned leaf."""

    required_flags = ("O_CLOEXEC", "O_NOFOLLOW", "O_NONBLOCK")
    if any(not hasattr(os, name) for name in required_flags):
        raise ValueError(
            "descriptor-safe preparation requires O_CLOEXEC, O_NOFOLLOW, "
            "and O_NONBLOCK"
        )

    path_before = path.lstat()
    if stat.S_ISLNK(path_before.st_mode):
        raise ValueError(f"{path}: input must not be a symlink")
    path_identity_before = interaction_audit._regular_file_identity(
        path_before, str(path)
    )

    fd = os.open(
        path,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
    )
    try:
        descriptor_before = os.fstat(fd)
        descriptor_identity_before = interaction_audit._regular_file_identity(
            descriptor_before, f"{path}: opened descriptor"
        )
        if path_identity_before != descriptor_identity_before:
            raise ValueError(
                f"{path}: pre-open path identity does not match opened descriptor"
            )
        path_after_open = interaction_audit._regular_file_identity(
            path.lstat(), f"{path}: post-open path"
        )
        if path_after_open != descriptor_identity_before:
            raise ValueError(
                f"{path}: path identity changed at the descriptor-open boundary"
            )

        data = interaction_audit._read_exact_descriptor(
            fd, descriptor_before.st_size, str(path)
        )
        descriptor_after = interaction_audit._regular_file_identity(
            os.fstat(fd), f"{path}: descriptor after read"
        )
        if descriptor_after != descriptor_identity_before:
            raise ValueError(f"{path}: opened descriptor changed while being read")
        path_after_read = interaction_audit._regular_file_identity(
            path.lstat(), f"{path}: path after read"
        )
        if path_after_read != descriptor_after:
            raise ValueError(f"{path}: path identity changed while being read")
    finally:
        os.close(fd)
    return data


def _split_after_final_ascii(reading: str) -> tuple[str, str]:
    offsets = [
        index for index, character in enumerate(reading) if ord(character) <= 0x7F
    ]
    if not offsets:
        raise ValueError("normal-composition proposal requires an ASCII-containing reading")
    boundary = offsets[-1] + 1
    context = reading[:boundary]
    composition = reading[boundary:]
    if not composition:
        raise ValueError("normal-composition proposal requires a non-empty composition")
    return context, composition


def _normal_case(row: dict[str, str]) -> dict[str, Any]:
    context, composition = _split_after_final_ascii(row["reading"])
    expected_target: list[str] = []
    for alternative in row["expected"].split("|"):
        if not alternative.startswith(context):
            raise ValueError(
                f"{row['id']}: expected alternative does not preserve committed context"
            )
        target = alternative[len(context) :]
        if not target:
            raise ValueError(f"{row['id']}: expected target must not be empty")
        expected_target.append(target)

    return {
        "case_id": row["id"],
        "category": row["category"],
        "expected": row["expected"],
        "proposed": {
            "action_trace": [
                {
                    "action": "update_context",
                    "left_context": context,
                    "right_context": "",
                },
                {
                    "action": "conversion_boundary",
                    "composition_reading": composition,
                },
            ],
            "committed_left_context": context,
            "composition_reading": composition,
            "expected_target": expected_target,
            "formal_product_path_eligible": False,
            "input_style": "unknown_pending_review",
            "physical_key_trace": None,
            "requested_transform": None,
            "right_context": "",
            "scenario_kind": "normal_input_context_candidate",
        },
        "reading": row["reading"],
        "review_status": "pending_review",
    }


def _review_case(row: dict[str, str]) -> dict[str, str]:
    return {
        "case_id": row["id"],
        "category": row["category"],
        "expected": row["expected"],
        "reading": row["reading"],
        "review_status": "action_trace_review_required",
    }


def prepare_bytes(data: bytes, *, generation_name: str) -> dict[str, Any]:
    digest = _sha256(data)
    if generation_name != SEALED_GENERATION:
        raise ValueError(
            f"sealed generation mismatch: expected {SEALED_GENERATION}, got {generation_name}"
        )
    if digest != SEALED_CORPUS_SHA256:
        raise ValueError(
            f"sealed corpus sha256 mismatch: expected {SEALED_CORPUS_SHA256}, got {digest}"
        )

    audit_report = interaction_audit.audit_bytes(
        data,
        generation_name=generation_name,
        input_name=CORPUS_NAME,
    )
    rows = interaction_audit._load_rows(data, CORPUS_NAME)
    row_by_id = {row["id"]: row for row in rows}
    if len(row_by_id) != len(rows):
        raise ValueError("sealed corpus case ids are not unique")

    ascii_ids = set(audit_report["case_ids"]["ascii_containing"])
    simple_ids = set(
        audit_report["case_ids"]["single_context_target_candidates"]
    )
    review_ids = set(audit_report["case_ids"]["action_trace_review_required"])
    if simple_ids & review_ids or simple_ids | review_ids != ascii_ids:
        raise ValueError("audit classifications do not exactly partition ASCII cases")
    if not ascii_ids.issubset(row_by_id):
        raise ValueError("audit classifications contain unknown case ids")

    cases: list[dict[str, Any]] = []
    for case_id in sorted(ascii_ids):
        row = row_by_id[case_id]
        cases.append(
            _normal_case(row) if case_id in simple_ids else _review_case(row)
        )

    emitted_ids = [case["case_id"] for case in cases]
    emitted_set = set(emitted_ids)
    if len(emitted_ids) != len(emitted_set):
        raise ValueError("draft sidecar contains duplicate case ids")
    if emitted_set != ascii_ids:
        raise ValueError("draft sidecar does not exactly cover ASCII corpus cases")

    return {
        "cases": cases,
        "classification_contract": {
            "action_trace_review_required": (
                "no scenario is inferred when the final-ASCII split cannot derive "
                "a non-empty expected target with exact context preservation"
            ),
            "audit_schema": audit_report["schema"],
            "context_action_semantics": (
                "update_context seeds left_context and right_context at the "
                "conversion boundary; it does not reproduce the prefix's key-input "
                "or prior-conversion history"
            ),
            "formal_runner_eligibility": (
                "draft candidates are not eligible for a formal product-path runner "
                "until physical input style and action traces are reviewed"
            ),
            "input_style_semantics": (
                "unknown_pending_review means direct versus mapped input is not "
                "inferred from the rendered composition reading"
            ),
            "single_context_target": audit_report["classification_contract"][
                "single_context_target"
            ],
        },
        "corpus": {
            "cases": audit_report["corpus"]["cases"],
            "input_name": CORPUS_NAME,
            "sha256": digest,
        },
        "counts": {
            "action_trace_review_required": len(review_ids),
            "ascii_cases": len(ascii_ids),
            "proposed_normal_input_context_candidates": len(simple_ids),
        },
        "coverage": {
            "complete": emitted_set == ascii_ids,
            "duplicate_case_ids": [],
            "emitted_ascii_cases": len(emitted_ids),
            "missing_ascii_case_ids": sorted(ascii_ids - emitted_set),
            "source_ascii_cases": len(ascii_ids),
            "source_total_cases": len(rows),
            "unexpected_case_ids": sorted(emitted_set - ascii_ids),
            "unique": len(emitted_ids) == len(emitted_set),
        },
        "formal_authorized": False,
        "generation": {
            "name": generation_name,
            "sha256": SEALED_GENERATION_SHA256,
        },
        "schema": SCHEMA,
        "status": "not_ready",
    }


def prepare_path(path: Path) -> dict[str, Any]:
    if path.name != CORPUS_NAME:
        raise ValueError(f"input filename must be {CORPUS_NAME}")
    data = _read_bound_input(path)
    return prepare_bytes(data, generation_name=path.parent.name)


def canonical_json_bytes(report: dict[str, Any]) -> bytes:
    text = json.dumps(
        report, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ) + "\n"
    return text.encode("utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        report = prepare_path(args.corpus)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    sys.stdout.buffer.write(canonical_json_bytes(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
