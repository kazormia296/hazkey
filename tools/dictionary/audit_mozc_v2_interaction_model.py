#!/usr/bin/env python3
"""Audit whether sealed Mozc v2 cases have an explicit interaction model.

The v2 corpus stores complete visible readings, but a reading containing ASCII
does not say whether the ASCII was already committed, selected for
reconversion, or produced by an F9/F10 transform.  This read-only audit keeps
those rows out of formal scoring until that interaction metadata is supplied.

For migration planning, an ASCII-containing row is a deterministic
``single-context-target`` candidate when splitting immediately after its final
ASCII scalar leaves a non-empty suffix containing kana and every accepted
surface preserves the derived context byte-for-byte.  All other ASCII rows
require an explicit action trace review.  This classification does not itself
authorize either class for formal scoring.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any


OUTPUT_SCHEMA = "hazkey.mozc-v2-interaction-model-audit.v1"
NOT_READY_REASON = "interaction-model-metadata-missing"
GENERATION_PATTERN = re.compile(r"sealed-v2-sha256-([0-9a-f]{64})")
REQUIRED_COLUMNS = ("id", "reading", "expected", "category")


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _is_ascii_scalar(character: str) -> bool:
    return ord(character) <= 0x7F


def _is_kana_scalar(character: str) -> bool:
    value = ord(character)
    return (
        0x3040 <= value <= 0x309F
        or 0x30A0 <= value <= 0x30FF
        or 0xFF61 <= value <= 0xFF9F
    )


def _load_rows(data: bytes, context: str) -> list[dict[str, str]]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"{context}: input is not valid UTF-8") from error

    with io.StringIO(text, newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if reader.fieldnames != list(REQUIRED_COLUMNS):
            raise ValueError(
                f"{context}: header must be exactly {list(REQUIRED_COLUMNS)!r}"
            )
        rows = list(reader)

    if not rows:
        raise ValueError(f"{context}: corpus must contain at least one case")

    seen_ids: set[str] = set()
    for line_number, row in enumerate(rows, 2):
        if set(row) != set(REQUIRED_COLUMNS):
            raise ValueError(f"{context}:{line_number}: unexpected TSV columns")
        for field in REQUIRED_COLUMNS:
            if not isinstance(row[field], str) or not row[field]:
                raise ValueError(
                    f"{context}:{line_number}: {field} must be non-empty"
                )
        if row["id"] in seen_ids:
            raise ValueError(
                f"{context}:{line_number}: duplicate case id {row['id']!r}"
            )
        seen_ids.add(row["id"])
        alternatives = row["expected"].split("|")
        if any(not alternative for alternative in alternatives):
            raise ValueError(
                f"{context}:{line_number}: expected alternatives must be non-empty"
            )
    return rows


def _single_context_target_candidate(row: dict[str, str]) -> bool:
    ascii_offsets = [
        index
        for index, character in enumerate(row["reading"])
        if _is_ascii_scalar(character)
    ]
    if not ascii_offsets:
        return False

    boundary = ascii_offsets[-1] + 1
    context = row["reading"][:boundary]
    target = row["reading"][boundary:]
    if not target or not any(_is_kana_scalar(character) for character in target):
        return False

    alternatives = row["expected"].split("|")
    return all(
        alternative.startswith(context) and len(alternative) > len(context)
        for alternative in alternatives
    )


def audit_bytes(
    data: bytes,
    *,
    generation_name: str,
    input_name: str = "formal-corpus.tsv",
) -> dict[str, Any]:
    """Return the canonical audit model for one immutable input byte string."""

    generation_match = GENERATION_PATTERN.fullmatch(generation_name)
    if generation_match is None:
        raise ValueError(
            "generation name must be sealed-v2-sha256- followed by 64 lowercase hex digits"
        )
    if not input_name or Path(input_name).name != input_name:
        raise ValueError("input name must be one non-empty path component")

    rows = _load_rows(data, input_name)
    category_rows: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        category_rows.setdefault(row["category"], []).append(row)

    ascii_ids: list[str] = []
    ascii_free_ids: list[str] = []
    single_ids: list[str] = []
    review_ids: list[str] = []
    categories: dict[str, dict[str, int]] = {}

    for category in sorted(category_rows):
        current = category_rows[category]
        category_ascii: list[str] = []
        category_ascii_free: list[str] = []
        category_single: list[str] = []
        category_review: list[str] = []
        for row in current:
            case_id = row["id"]
            if not any(_is_ascii_scalar(character) for character in row["reading"]):
                category_ascii_free.append(case_id)
            elif _single_context_target_candidate(row):
                category_ascii.append(case_id)
                category_single.append(case_id)
            else:
                category_ascii.append(case_id)
                category_review.append(case_id)

        ascii_ids.extend(category_ascii)
        ascii_free_ids.extend(category_ascii_free)
        single_ids.extend(category_single)
        review_ids.extend(category_review)
        categories[category] = {
            "action_trace_review_required": len(category_review),
            "ascii_containing": len(category_ascii),
            "ascii_free": len(category_ascii_free),
            "cases": len(current),
            "single_context_target_candidates": len(category_single),
        }

    for values in (ascii_ids, ascii_free_ids, single_ids, review_ids):
        values.sort()

    input_sha256 = _sha256(data)
    return {
        "blocking_not_ready_reason": {
            "action_trace_review_required_cases": len(review_ids),
            "affected_ascii_cases": len(ascii_ids),
            "id": NOT_READY_REASON,
            "message": (
                "ASCII-containing readings require explicit normal-composition, "
                "reconversion, F9, or F10 interaction metadata before formal scoring"
            ),
        },
        "case_ids": {
            "action_trace_review_required": review_ids,
            "ascii_containing": ascii_ids,
            "ascii_free": ascii_free_ids,
            "single_context_target_candidates": single_ids,
        },
        "categories": categories,
        "classification_contract": {
            "ascii_scalar_range": "U+0000..U+007F",
            "formal_scoring_authorized": False,
            "single_context_target": (
                "split-after-final-ascii-scalar; non-empty suffix containing kana; "
                "all expected alternatives preserve the derived context exactly"
            ),
        },
        "corpus": {
            "cases": len(rows),
            "sha256": input_sha256,
        },
        "counts": {
            "action_trace_review_required": len(review_ids),
            "ascii_containing": len(ascii_ids),
            "ascii_free": len(ascii_free_ids),
            "single_context_target_candidates": len(single_ids),
            "total": len(rows),
        },
        "formal_evidence_status": "not_ready",
        "generation": {
            "name": generation_name,
            "sha256": "sha256:" + generation_match.group(1),
        },
        "input": {
            "name": input_name,
            "sha256": input_sha256,
            "size_bytes": len(data),
        },
        "schema": OUTPUT_SCHEMA,
    }


def audit_path(path: Path) -> dict[str, Any]:
    """Read only from one pinned regular-file descriptor, failing closed."""

    if not hasattr(os, "O_CLOEXEC") or not hasattr(os, "O_NOFOLLOW"):
        raise ValueError("descriptor-safe audit requires O_CLOEXEC and O_NOFOLLOW")

    path_before = path.lstat()
    if stat.S_ISLNK(path_before.st_mode):
        raise ValueError(f"{path}: input must not be a symlink")
    path_identity_before = _regular_file_identity(path_before, str(path))

    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        descriptor_before = os.fstat(fd)
        descriptor_identity_before = _regular_file_identity(
            descriptor_before, f"{path}: opened descriptor"
        )
        if path_identity_before != descriptor_identity_before:
            raise ValueError(
                f"{path}: pre-open path identity does not match opened descriptor"
            )

        path_after_open = path.lstat()
        path_identity_after_open = _regular_file_identity(
            path_after_open, f"{path}: post-open path"
        )
        if path_identity_after_open != descriptor_identity_before:
            raise ValueError(
                f"{path}: path identity changed at the descriptor-open boundary"
            )

        data = _read_exact_descriptor(fd, descriptor_before.st_size, str(path))
        descriptor_after = os.fstat(fd)
        descriptor_identity_after = _regular_file_identity(
            descriptor_after, f"{path}: descriptor after read"
        )
        if descriptor_identity_after != descriptor_identity_before:
            raise ValueError(f"{path}: opened descriptor changed while being read")

        path_after_read = path.lstat()
        path_identity_after_read = _regular_file_identity(
            path_after_read, f"{path}: path after read"
        )
        if path_identity_after_read != descriptor_identity_after:
            raise ValueError(f"{path}: path identity changed while being read")
    finally:
        os.close(fd)

    return audit_bytes(
        data,
        generation_name=path.parent.name,
        input_name=path.name,
    )


def stable_json(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _regular_file_identity(metadata: os.stat_result, context: str) -> tuple[int, ...]:
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"{context}: input must be a regular file")
    if metadata.st_nlink != 1:
        raise ValueError(f"{context}: input must have exactly one hard link")
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_exact_descriptor(fd: int, size: int, context: str) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = os.read(fd, min(1024 * 1024, remaining))
        if not chunk:
            raise ValueError(f"{context}: input ended before its recorded size")
        chunks.append(chunk)
        remaining -= len(chunk)
    if os.read(fd, 1):
        raise ValueError(f"{context}: input grew while being read")
    return b"".join(chunks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        report = audit_path(args.corpus)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    sys.stdout.write(stable_json(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
