#!/usr/bin/env python3
"""Validate, split, and immutably seal a first-segment hybrid holdout.

The reviewed ``cases.jsonl`` is authoritative.  Sealing deterministically
derives a label-free probe input and a separately bound label file, then
publishes all bytes as one content-addressed, read-only generation.  This v1
contract deliberately remains non-formal until the existing-corpus and
auxiliary-suite contamination screens and backend label isolation are
implemented.
"""

from __future__ import annotations

import argparse
from collections import Counter
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import unicodedata
from typing import Any, Iterable


CASE_SCHEMA = "hazkey.mozc-hybrid-segment-case.v1"
APPROVAL_SCHEMA = "hazkey.mozc-hybrid-segment-holdout-approval.v1"
PROBE_INPUT_SCHEMA = "hazkey.mozc-hybrid-segment-probe-input.v1"
SEGMENT_LABEL_SCHEMA = "hazkey.mozc-hybrid-first-segment-label.v1"
MANIFEST_SCHEMA = "hazkey.mozc-hybrid-segment-holdout-manifest.v1"

H0_POLICY_ID = "mozc-first-preserve-top1-h0"
H1_POLICY_ID = "mozc-first-one-sided-consensus-v1"
H2_POLICY_ID = "mozc-first-one-sided-consensus-width-guard-v1"
COMPOSITION_ELEMENT_UNIT = "composition_element"

SOURCE_CASES_NAME = "cases.jsonl"
APPROVAL_NAME = "approval.json"
PROBE_INPUT_NAME = "probe-input.jsonl"
SEGMENT_LABELS_NAME = "segment-labels.jsonl"
MANIFEST_NAME = "manifest.json"
SEALED_DIRECTORY_PREFIX = "sealed-segment-holdout-v1-sha256-"
STAGING_DIRECTORY_PREFIX = ".sealed-segment-holdout-v1-staging-"
REJECTED_DIRECTORY_PREFIX = ".sealed-segment-holdout-v1-rejected-"
RENAME_NOREPLACE = 1

ATTESTATION_CONTRACT = {
    "backend_outputs_consulted": False,
    "known_holdout_rows_reused": False,
    "candidate_based_case_selection": False,
    "labels_authored_independently": True,
    "candidate_artifacts_frozen_before_disclosure": True,
}


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
    actual_set = set(value)
    if actual_set != expected_set:
        raise ValueError(
            f"{context} fields do not match schema; "
            f"missing={sorted(expected_set - actual_set)!r}, "
            f"unknown={sorted(actual_set - expected_set)!r}"
        )


def _object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be an object")
    return value


def _array(value: Any, context: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{context} must be an array")
    return value


def _text(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{context} must be a non-empty string")
    if value != unicodedata.normalize("NFC", value):
        raise ValueError(f"{context} must be NFC-normalized")
    if any(
        unicodedata.category(character) == "Cc" or character == "\ufeff"
        for character in value
    ):
        raise ValueError(f"{context} must not contain control characters")
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
    result = _text(value, context)
    if re.fullmatch(r"sha256:[0-9a-f]{64}", result) is None:
        raise ValueError(f"{context} must be sha256:<64 lowercase hex>")
    return result


def sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


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
    for line_number, raw_line in enumerate(data[:-1].split(b"\n"), 1):
        if not raw_line:
            raise ValueError(f"{context}:{line_number} must not be blank")
        records.append(
            _object(
                _load_json_bytes(raw_line, f"{context}:{line_number}"),
                f"{context}:{line_number}",
            )
        )
    if not records:
        raise ValueError(f"{context} must contain at least one case")
    return records


def _validate_case(record: dict[str, Any], context: str) -> dict[str, Any]:
    _require_exact_keys(
        record,
        {"schema", "id", "category", "family_id", "elements", "target"},
        context,
    )
    if record["schema"] != CASE_SCHEMA:
        raise ValueError(f"{context}.schema must be {CASE_SCHEMA}")
    case_id = _text(record["id"], f"{context}.id")
    category = _text(record["category"], f"{context}.category")
    family_id = _text(record["family_id"], f"{context}.family_id")

    raw_elements = _array(record["elements"], f"{context}.elements")
    if not raw_elements:
        raise ValueError(f"{context}.elements must not be empty")
    elements: list[dict[str, str]] = []
    for index, raw_element in enumerate(raw_elements):
        element_context = f"{context}.elements[{index}]"
        element = _object(raw_element, element_context)
        _require_exact_keys(element, {"text", "input_style"}, element_context)
        text = _text(element["text"], f"{element_context}.text")
        if element["input_style"] != "direct":
            raise ValueError(f"{element_context}.input_style must be direct")
        elements.append({"text": text, "input_style": "direct"})

    target = _object(record["target"], f"{context}.target")
    _require_exact_keys(target, {"span", "surfaces"}, f"{context}.target")
    span = _object(target["span"], f"{context}.target.span")
    _require_exact_keys(
        span, {"start", "count", "unit"}, f"{context}.target.span"
    )
    start = _nonnegative_int(span["start"], f"{context}.target.span.start")
    if start != 0:
        raise ValueError(f"{context}.target.span.start must be 0")
    count = _positive_int(span["count"], f"{context}.target.span.count")
    if count > len(elements):
        raise ValueError(
            f"{context}.target.span.count must not exceed the element count"
        )
    if span["unit"] != COMPOSITION_ELEMENT_UNIT:
        raise ValueError(
            f"{context}.target.span.unit must be {COMPOSITION_ELEMENT_UNIT}"
        )

    raw_surfaces = _array(target["surfaces"], f"{context}.target.surfaces")
    if not raw_surfaces:
        raise ValueError(f"{context}.target.surfaces must not be empty")
    surfaces = [
        _text(surface, f"{context}.target.surfaces[{index}]")
        for index, surface in enumerate(raw_surfaces)
    ]
    if len(surfaces) != len(set(surfaces)):
        raise ValueError(f"{context}.target.surfaces must be unique")

    return {
        "schema": CASE_SCHEMA,
        "id": case_id,
        "category": category,
        "family_id": family_id,
        "elements": elements,
        "target": {
            "span": {
                "start": 0,
                "count": count,
                "unit": COMPOSITION_ELEMENT_UNIT,
            },
            "surfaces": surfaces,
        },
    }


def load_cases_bytes(data: bytes, context: str = "segment holdout cases") -> list[dict[str, Any]]:
    cases = [
        _validate_case(record, f"{context}:{index}")
        for index, record in enumerate(_load_jsonl(data, context), 1)
    ]
    seen_ids: set[str] = set()
    seen_families: set[str] = set()
    for case in cases:
        if case["id"] in seen_ids:
            raise ValueError(f"duplicate case id {case['id']!r}")
        if case["family_id"] in seen_families:
            raise ValueError(f"duplicate family_id {case['family_id']!r}")
        seen_ids.add(case["id"])
        seen_families.add(case["family_id"])
    return cases


def _validate_quality_categories(value: Any) -> dict[str, int]:
    raw = _object(value, "approval.quality_categories")
    if not raw:
        raise ValueError("approval.quality_categories must not be empty")
    result: dict[str, int] = {}
    for category, count in raw.items():
        validated_category = _text(category, "approval.quality_categories key")
        result[validated_category] = _positive_int(
            count, f"approval.quality_categories.{validated_category}"
        )
    return result


def _validate_policy_freeze(value: Any) -> dict[str, Any]:
    freeze = _object(value, "approval.policy_freeze")
    expected_keys = {
        "h0_policy_id",
        "h1_policy_id",
        "h2_policy_id",
        "product_source_revision",
        "evaluator_sha256",
        "hybrid_evaluator_sha256",
        "abprobe_executable_sha256",
        "hazkey_resource_fingerprint",
        "mozc_resource_fingerprint",
        "mozc_bundle_generation",
        "top_k",
        "warmups",
        "iterations",
        "learning_enabled",
    }
    _require_exact_keys(freeze, expected_keys, "approval.policy_freeze")
    expected_ids = {
        "h0_policy_id": H0_POLICY_ID,
        "h1_policy_id": H1_POLICY_ID,
        "h2_policy_id": H2_POLICY_ID,
    }
    for field, expected in expected_ids.items():
        if freeze[field] != expected:
            raise ValueError(f"approval.policy_freeze.{field} must be {expected}")
    revision = _text(
        freeze["product_source_revision"],
        "approval.policy_freeze.product_source_revision",
    )
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise ValueError(
            "approval.policy_freeze.product_source_revision must be 40 lowercase hex"
        )
    evaluator_sha256 = _sha256(
        freeze["evaluator_sha256"], "approval.policy_freeze.evaluator_sha256"
    )
    hybrid_evaluator_sha256 = _sha256(
        freeze["hybrid_evaluator_sha256"],
        "approval.policy_freeze.hybrid_evaluator_sha256",
    )
    executable_sha256 = _sha256(
        freeze["abprobe_executable_sha256"],
        "approval.policy_freeze.abprobe_executable_sha256",
    )
    resource_fingerprint = _sha256(
        freeze["hazkey_resource_fingerprint"],
        "approval.policy_freeze.hazkey_resource_fingerprint",
    )
    if resource_fingerprint == "sha256:" + "0" * 64:
        raise ValueError(
            "approval.policy_freeze.hazkey_resource_fingerprint "
            "must not be the all-zero fingerprint"
        )
    mozc_resource_fingerprint = _sha256(
        freeze["mozc_resource_fingerprint"],
        "approval.policy_freeze.mozc_resource_fingerprint",
    )
    if mozc_resource_fingerprint == "sha256:" + "0" * 64:
        raise ValueError(
            "approval.policy_freeze.mozc_resource_fingerprint "
            "must not be the all-zero fingerprint"
        )
    mozc_generation = _text(
        freeze["mozc_bundle_generation"],
        "approval.policy_freeze.mozc_bundle_generation",
    )
    if re.fullmatch(r"sha256-[0-9a-f]{64}", mozc_generation) is None:
        raise ValueError(
            "approval.policy_freeze.mozc_bundle_generation must be "
            "sha256-<64 lowercase hex>"
        )
    top_k = _positive_int(freeze["top_k"], "approval.policy_freeze.top_k")
    if top_k > 10:
        raise ValueError("approval.policy_freeze.top_k must not exceed 10")
    warmups = _nonnegative_int(
        freeze["warmups"], "approval.policy_freeze.warmups"
    )
    iterations = _positive_int(
        freeze["iterations"], "approval.policy_freeze.iterations"
    )
    if freeze["learning_enabled"] is not False:
        raise ValueError("approval.policy_freeze.learning_enabled must be false")
    return {
        **expected_ids,
        "product_source_revision": revision,
        "evaluator_sha256": evaluator_sha256,
        "hybrid_evaluator_sha256": hybrid_evaluator_sha256,
        "abprobe_executable_sha256": executable_sha256,
        "hazkey_resource_fingerprint": resource_fingerprint,
        "mozc_resource_fingerprint": mozc_resource_fingerprint,
        "mozc_bundle_generation": mozc_generation,
        "top_k": top_k,
        "warmups": warmups,
        "iterations": iterations,
        "learning_enabled": False,
    }


def load_approval_bytes(data: bytes) -> dict[str, Any]:
    approval = _object(
        _load_json_bytes(data, "segment holdout approval"),
        "segment holdout approval",
    )
    _require_exact_keys(
        approval,
        {
            "schema",
            "status",
            "holdout_id",
            "source_cases_sha256",
            "author_id",
            "reviewer_id",
            "quality_categories",
            "minimum_h2_promotion_opportunities",
            "attestation",
            "policy_freeze",
        },
        "segment holdout approval",
    )
    if approval["schema"] != APPROVAL_SCHEMA:
        raise ValueError(f"approval.schema must be {APPROVAL_SCHEMA}")
    if approval["status"] != "approved":
        raise ValueError("approval.status must be approved")
    holdout_id = _text(approval["holdout_id"], "approval.holdout_id")
    source_sha256 = _sha256(
        approval["source_cases_sha256"], "approval.source_cases_sha256"
    )
    author_id = _text(approval["author_id"], "approval.author_id")
    reviewer_id = _text(approval["reviewer_id"], "approval.reviewer_id")
    if author_id.casefold() == reviewer_id.casefold():
        raise ValueError("approval author and reviewer must be independent")
    categories = _validate_quality_categories(approval["quality_categories"])
    minimum_opportunities = _positive_int(
        approval["minimum_h2_promotion_opportunities"],
        "approval.minimum_h2_promotion_opportunities",
    )
    attestation = _object(approval["attestation"], "approval.attestation")
    _require_exact_keys(attestation, ATTESTATION_CONTRACT, "approval.attestation")
    if any(
        attestation[field] is not expected
        for field, expected in ATTESTATION_CONTRACT.items()
    ):
        raise ValueError("approval.attestation does not satisfy the blind holdout contract")
    freeze = _validate_policy_freeze(approval["policy_freeze"])
    return {
        "schema": APPROVAL_SCHEMA,
        "status": "approved",
        "holdout_id": holdout_id,
        "source_cases_sha256": source_sha256,
        "author_id": author_id,
        "reviewer_id": reviewer_id,
        "quality_categories": categories,
        "minimum_h2_promotion_opportunities": minimum_opportunities,
        "attestation": dict(ATTESTATION_CONTRACT),
        "policy_freeze": freeze,
    }


def prepare_outputs(*, cases_path: Path, approval_path: Path) -> dict[str, bytes]:
    cases_data = _read_regular(cases_path, "authoritative segment holdout cases")
    approval_data = _read_regular(approval_path, "segment holdout approval")
    cases = load_cases_bytes(cases_data)
    approval = load_approval_bytes(approval_data)
    actual_source_sha256 = sha256_bytes(cases_data)
    if approval["source_cases_sha256"] != actual_source_sha256:
        raise ValueError("approval source_cases_sha256 does not match exact cases bytes")
    category_counts = dict(Counter(case["category"] for case in cases))
    quality_count_mismatches = {
        category: {
            "expected": expected,
            "actual": category_counts.get(category, 0),
        }
        for category, expected in approval["quality_categories"].items()
        if category_counts.get(category, 0) != expected
    }
    if quality_count_mismatches:
        raise ValueError(
            "case category counts do not match approval.quality_categories: "
            f"{quality_count_mismatches!r}"
        )
    quality_case_count = sum(approval["quality_categories"].values())
    if approval["minimum_h2_promotion_opportunities"] > quality_case_count:
        raise ValueError(
            "minimum H2 promotion opportunities must not exceed quality case count"
        )

    probe_records = [
        {
            "schema": PROBE_INPUT_SCHEMA,
            "id": case["id"],
            "category": case["category"],
            "elements": [dict(element) for element in case["elements"]],
        }
        for case in cases
    ]
    label_records = [
        {
            "schema": SEGMENT_LABEL_SCHEMA,
            "id": case["id"],
            "family_id": case["family_id"],
            "target": {
                "span": dict(case["target"]["span"]),
                "surfaces": list(case["target"]["surfaces"]),
            },
        }
        for case in cases
    ]
    probe_data = _render_jsonl(probe_records)
    labels_data = _render_jsonl(label_records)
    policy_freeze_bytes = _canonical_json(approval["policy_freeze"])
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "holdout_id": approval["holdout_id"],
        "formal_authorized": False,
        "human_collection_attested": True,
        "bindings": {
            "source_cases": {
                "path": SOURCE_CASES_NAME,
                "schema": CASE_SCHEMA,
                "sha256": actual_source_sha256,
                "cases": len(cases),
            },
            "review_approval": {
                "path": APPROVAL_NAME,
                "schema": APPROVAL_SCHEMA,
                "status": "approved",
                "sha256": sha256_bytes(approval_data),
            },
            "probe_input": {
                "path": PROBE_INPUT_NAME,
                "schema": PROBE_INPUT_SCHEMA,
                "sha256": sha256_bytes(probe_data),
                "cases": len(cases),
            },
            "segment_labels": {
                "path": SEGMENT_LABELS_NAME,
                "schema": SEGMENT_LABEL_SCHEMA,
                "sha256": sha256_bytes(labels_data),
                "cases": len(cases),
            },
        },
        "category_counts": category_counts,
        "evaluation_contract": {
            "quality_categories": sorted(approval["quality_categories"]),
            "minimum_h2_promotion_opportunities": approval[
                "minimum_h2_promotion_opportunities"
            ],
            "target_match": (
                "raw-exact-NFC-label-surface-and-composition-element-count.v1"
            ),
        },
        "policy_freeze": {
            "sha256": sha256_bytes(policy_freeze_bytes),
            "value": approval["policy_freeze"],
        },
        "outstanding_requirements": {
            "existing_v2_and_auxiliary_duplicate_screen": "not_implemented",
            "backend_label_isolation": "not_implemented",
            "evaluator_loaded_code_identity": "not_attested",
            "formal_authorization_blocked": True,
        },
    }
    return {
        SOURCE_CASES_NAME: cases_data,
        APPROVAL_NAME: approval_data,
        PROBE_INPUT_NAME: probe_data,
        SEGMENT_LABELS_NAME: labels_data,
        MANIFEST_NAME: _render_json(manifest),
    }


def sealed_directory_name(generated: dict[str, bytes]) -> str:
    fingerprints = {
        name: sha256_bytes(data) for name, data in generated.items()
    }
    digest = sha256_bytes(_canonical_json(fingerprints)).removeprefix("sha256:")
    return SEALED_DIRECTORY_PREFIX + digest


def _root_identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_nlink


def _directory_identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _open_pinned_root(root: Path) -> int:
    before = os.stat(root, follow_symlinks=False)
    if not stat.S_ISDIR(before.st_mode):
        raise ValueError("output root must be a real directory")
    descriptor = os.open(
        root,
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    opened = os.fstat(descriptor)
    if _root_identity(opened) != _root_identity(before):
        os.close(descriptor)
        raise ValueError("output root changed while it was opened")
    return descriptor


def _assert_pinned_root_path(root: Path, root_fd: int) -> None:
    try:
        current = os.stat(root, follow_symlinks=False)
        pinned = os.fstat(root_fd)
    except OSError as error:
        raise ValueError("output root path changed while sealing") from error
    if not stat.S_ISDIR(current.st_mode) or _root_identity(current) != _root_identity(
        pinned
    ):
        raise ValueError("output root path changed while sealing")


def _write_all_at(directory_fd: int, generated: dict[str, bytes]) -> None:
    for name, data in generated.items():
        descriptor = os.open(
            name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        try:
            view = memoryview(data)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short write while publishing segment holdout")
                view = view[written:]
            os.fsync(descriptor)
            os.fchmod(descriptor, 0o444)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _read_regular_at(directory_fd: int, name: str) -> tuple[bytes, os.stat_result]:
    descriptor = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=directory_fd,
    )
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"sealed output {name} must be a regular file")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if _file_identity(before) != _file_identity(after) or _file_identity(
            before
        ) != _file_identity(current):
            raise ValueError(f"sealed output changed during verification: {name}")
        return b"".join(chunks), after
    finally:
        os.close(descriptor)


def _verify_generation_at(
    root_fd: int, directory_name: str, generated: dict[str, bytes]
) -> None:
    directory_fd = os.open(
        directory_name,
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=root_fd,
    )
    try:
        metadata = os.fstat(directory_fd)
        if stat.S_IMODE(metadata.st_mode) != 0o555:
            raise ValueError("sealed generation directory mode changed")
        if set(os.listdir(directory_fd)) != set(generated):
            raise ValueError("sealed generation file set changed")
        for name, expected in generated.items():
            actual, file_metadata = _read_regular_at(directory_fd, name)
            if (
                actual != expected
                or stat.S_IMODE(file_metadata.st_mode) != 0o444
                or file_metadata.st_nlink != 1
            ):
                raise ValueError(f"sealed generation output changed: {name}")
    finally:
        os.close(directory_fd)


def _rename_noreplace(root_fd: int, source: str, destination: str) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOSYS, "renameat2 is required for no-replace publication")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    if (
        renameat2(
            root_fd,
            os.fsencode(source),
            root_fd,
            os.fsencode(destination),
            RENAME_NOREPLACE,
        )
        != 0
    ):
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number), destination)


def _remove_generation_at(
    root_fd: int,
    directory_name: str,
    generated: dict[str, bytes],
    *,
    expected_identity: tuple[int, int],
) -> None:
    try:
        directory_fd = os.open(
            directory_name,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=root_fd,
        )
    except FileNotFoundError:
        return
    try:
        if _directory_identity(os.fstat(directory_fd)) != expected_identity:
            raise ValueError(
                "refusing to remove a generation with an unexpected identity"
            )
        os.fchmod(directory_fd, 0o700)
        for name in os.listdir(directory_fd):
            _remove_entry_at(directory_fd, name)
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    current = os.stat(directory_name, dir_fd=root_fd, follow_symlinks=False)
    if _directory_identity(current) != expected_identity:
        raise ValueError(
            "refusing to remove a generation whose path identity changed"
        )
    os.rmdir(directory_name, dir_fd=root_fd)
    os.fsync(root_fd)


def _remove_entry_at(parent_fd: int, name: str) -> None:
    """Remove an untrusted entry without following symlinks.

    A same-UID process can add an unexpected entry after publication and before
    verification.  Cleanup therefore cannot assume the generated file set; it
    must remove all entries below the directory that this process created.
    """

    try:
        os.unlink(name, dir_fd=parent_fd)
        return
    except FileNotFoundError:
        return
    except IsADirectoryError:
        pass

    directory_fd = os.open(
        name,
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_fd,
    )
    try:
        os.fchmod(directory_fd, 0o700)
        for child in os.listdir(directory_fd):
            _remove_entry_at(directory_fd, child)
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    try:
        os.rmdir(name, dir_fd=parent_fd)
    except FileNotFoundError:
        pass


def seal(
    *, cases_path: Path, approval_path: Path, output_root: Path
) -> tuple[dict[str, bytes], Path]:
    generated = prepare_outputs(cases_path=cases_path, approval_path=approval_path)
    final_name = sealed_directory_name(generated)
    root_fd = _open_pinned_root(output_root)
    staging_name = STAGING_DIRECTORY_PREFIX + secrets.token_hex(12)
    staging_created = False
    staging_identity: tuple[int, int] | None = None
    renamed = False
    try:
        _assert_pinned_root_path(output_root, root_fd)
        os.mkdir(staging_name, 0o700, dir_fd=root_fd)
        staging_created = True
        staging_fd = os.open(
            staging_name,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=root_fd,
        )
        try:
            staging_identity = _directory_identity(os.fstat(staging_fd))
            _write_all_at(staging_fd, generated)
            os.fsync(staging_fd)
            os.fchmod(staging_fd, 0o555)
            os.fsync(staging_fd)
        finally:
            os.close(staging_fd)
        _assert_pinned_root_path(output_root, root_fd)
        _rename_noreplace(root_fd, staging_name, final_name)
        renamed = True
        os.fsync(root_fd)
        _assert_pinned_root_path(output_root, root_fd)
        _verify_generation_at(root_fd, final_name, generated)
        _assert_pinned_root_path(output_root, root_fd)
        return generated, output_root / final_name
    except (OSError, ValueError):
        if renamed:
            rejected_name = REJECTED_DIRECTORY_PREFIX + secrets.token_hex(12)
            try:
                _rename_noreplace(root_fd, final_name, rejected_name)
                os.fsync(root_fd)
                if staging_identity is None:
                    raise AssertionError("staging identity was not captured")
                _remove_generation_at(
                    root_fd,
                    rejected_name,
                    generated,
                    expected_identity=staging_identity,
                )
            except (OSError, ValueError, AssertionError):
                pass
        raise
    finally:
        if not renamed and staging_created:
            try:
                if staging_identity is not None:
                    _remove_generation_at(
                        root_fd,
                        staging_name,
                        generated,
                        expected_identity=staging_identity,
                    )
            except (OSError, ValueError):
                pass
        os.close(root_fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--approval", type=Path, required=True)
    parser.add_argument("--output-root", type=Path)
    args = parser.parse_args(argv)
    output_root = args.output_root or args.cases.parent
    try:
        generated, generation = seal(
            cases_path=args.cases,
            approval_path=args.approval,
            output_root=output_root,
        )
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(f"{sha256_bytes(generated[MANIFEST_NAME])} {generation}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
