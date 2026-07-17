#!/usr/bin/env python3
"""Import the pinned AJIMEE-Bench JWTD v2 contextual rows for blind Silver work.

The command accepts only the exact raw snapshot pinned by
``build_frozen_corpus.py``.  It validates all 200 upstream rows before selecting
the 100 rows whose ``context_text`` is non-empty, renders those rows as
``hazkey.blind-silver-annotation-case.v1``, and publishes a provenance-bound
generation directory without replacing an existing path.

The raw AJIMEE data is intentionally neither downloaded nor copied into this
repository by this module.
"""

from __future__ import annotations

import argparse
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
from typing import Any, Iterable
import unicodedata

try:
    from . import build_frozen_corpus as frozen
    from . import prepare_blind_silver_annotations as blind
except ImportError:  # Direct execution from tools/dictionary.
    import build_frozen_corpus as frozen
    import prepare_blind_silver_annotations as blind


MANIFEST_SCHEMA = "hazkey.ajimee-jwtd-v2-contextual-blind-silver-import.v1"
TRANSFORM_ID = "ajimee-jwtd-v2-contextual-to-blind-silver.v1"
ROW_DIGEST_ALGORITHM = "sha256:canonical-json-utf8-sort-keys-compact.v1"
CONTEXT_BINDING_ALGORITHM = "sha256:canonical-jsonl-utf8-sort-keys-compact.v1"
CASES_NAME = "cases.jsonl"
MANIFEST_NAME = "manifest.json"
RENAME_NOREPLACE = 1
MAX_RAW_BYTES = 128 * 1024 * 1024

UPSTREAM_FIELDS = frozenset(
    {
        "index",
        "context_text",
        "input",
        "expected_output",
        "original_text",
        "splitted_input_for_limited_input_length",
    }
)


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
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


def _render_jsonl(records: Iterable[dict[str, Any]]) -> bytes:
    return b"".join(_canonical_json(record) + b"\n" for record in records)


def _render_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


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


def _stable_unique(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _load_raw_json(raw: bytes) -> list[Any]:
    if not raw or len(raw) > MAX_RAW_BYTES:
        raise ValueError(
            f"AJIMEE raw JSON must contain 1..{MAX_RAW_BYTES} exact bytes"
        )
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ValueError("AJIMEE raw JSON must not contain a UTF-8 BOM")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("AJIMEE raw JSON is not valid UTF-8") from error
    try:
        value = json.loads(text, object_pairs_hook=_object_without_duplicate_keys)
    except json.JSONDecodeError as error:
        raise ValueError(f"AJIMEE raw JSON is invalid JSON: {error.msg}") from error
    if not isinstance(value, list):
        raise ValueError("AJIMEE raw JSON must be an array")
    return value


def _canonical_index(value: Any, context: str) -> tuple[str, int]:
    if not isinstance(value, str) or re.fullmatch(r"0|[1-9][0-9]*", value) is None:
        raise ValueError(f"{context} must be a canonical decimal integer string")
    return value, int(value)


def _validate_upstream_row(raw_value: Any, position: int) -> dict[str, Any]:
    context = f"AJIMEE raw JSON[{position}]"
    if not isinstance(raw_value, dict):
        raise ValueError(f"{context} must be an object")
    actual_fields = set(raw_value)
    if actual_fields != UPSTREAM_FIELDS:
        raise ValueError(
            f"{context} fields do not match the pinned schema; "
            f"missing={sorted(UPSTREAM_FIELDS - actual_fields)!r}, "
            f"unknown={sorted(actual_fields - UPSTREAM_FIELDS)!r}"
        )

    index_text, index = _canonical_index(raw_value["index"], f"{context}.index")
    left_context = _text(
        raw_value["context_text"],
        f"{context}.context_text",
        maximum_code_points=blind.MAX_LEFT_CONTEXT_CODE_POINTS,
        allow_empty=True,
    )
    reading_source = _text(
        raw_value["input"],
        f"{context}.input",
        maximum_code_points=blind.MAX_READING_CODE_POINTS,
    )
    original_text = _text(
        raw_value["original_text"],
        f"{context}.original_text",
        maximum_code_points=blind.MAX_SURFACE_CODE_POINTS * 4,
    )

    raw_expected = raw_value["expected_output"]
    if not isinstance(raw_expected, list) or not raw_expected:
        raise ValueError(f"{context}.expected_output must be a non-empty array")
    expected = _stable_unique(
        _text(
            value,
            f"{context}.expected_output[{output_index}]",
            maximum_code_points=blind.MAX_SURFACE_CODE_POINTS,
        )
        for output_index, value in enumerate(raw_expected)
    )
    if len(expected) > blind.MAX_SURFACE_REFERENCES:
        raise ValueError(
            f"{context}.expected_output has more than "
            f"{blind.MAX_SURFACE_REFERENCES} distinct references"
        )

    raw_split = raw_value["splitted_input_for_limited_input_length"]
    if not isinstance(raw_split, list):
        raise ValueError(
            f"{context}.splitted_input_for_limited_input_length must be an array"
        )
    split_input = [
        _text(
            value,
            f"{context}.splitted_input_for_limited_input_length[{split_index}]",
            maximum_code_points=blind.MAX_READING_CODE_POINTS,
            allow_empty=True,
        )
        for split_index, value in enumerate(raw_split)
    ]

    # Digest the complete validated upstream row before selecting or transforming
    # it.  The whole-file binding in the manifest separately preserves exact-byte
    # identity, including whitespace and object-key order.
    validated_source_row = {
        "index": index_text,
        "context_text": left_context,
        "input": reading_source,
        "expected_output": list(raw_expected),
        "original_text": original_text,
        "splitted_input_for_limited_input_length": split_input,
    }
    row_sha256 = _sha256(_canonical_json(validated_source_row))
    reading = frozen.katakana_to_hiragana(reading_source)
    # Validate the transformed row with the actual downstream case validator;
    # this keeps the importer suitable for prepare_blind_silver_annotations.py.
    case = blind._validate_case(
        {
            "schema": blind.CASE_SCHEMA,
            "id": f"ajimee-jwtd-v2-contextual-{index:06d}",
            "family_id": f"ajimee-jwtd-v2-index-{index:06d}",
            "source_revision": (
                f"ajimee-bench@{frozen.AJIMEE_REVISION}:"
                f"{frozen.AJIMEE_RAW_PATH}:row-{row_sha256}"
            ),
            "dataset_role": "representative",
            "fold": "exploration",
            "reading": reading,
            "surface_references": expected,
            "left_context": left_context,
        },
        f"{context} transformed case",
    )
    return {
        "index": index,
        "row_sha256": row_sha256,
        "source_row": validated_source_row,
        "case": case,
    }


def _build_generation_for_contract(
    raw: bytes,
    *,
    expected_raw_sha256: str,
    expected_total_rows: int,
    expected_contextual_rows: int,
    expected_empty_rows: int,
) -> dict[str, bytes]:
    """Build output bytes against an explicit contract.

    Tests use this internal seam with a synthetic hash and small row counts.
    The CLI never exposes these values and calls ``build_generation`` below,
    which always supplies the repository's pinned AJIMEE contract.
    """

    if re.fullmatch(r"sha256:[0-9a-f]{64}", expected_raw_sha256) is None:
        raise ValueError("expected AJIMEE SHA-256 must be sha256:<64 lowercase hex>")
    if (
        type(expected_total_rows) is not int
        or type(expected_contextual_rows) is not int
        or type(expected_empty_rows) is not int
        or min(
            expected_total_rows,
            expected_contextual_rows,
            expected_empty_rows,
        )
        < 0
        or expected_contextual_rows + expected_empty_rows != expected_total_rows
    ):
        raise ValueError("expected AJIMEE row counts form an invalid partition")

    raw_sha256 = _sha256(raw)
    if raw_sha256 != expected_raw_sha256:
        raise ValueError("AJIMEE raw JSON SHA-256 does not match the pinned snapshot")
    values = _load_raw_json(raw)
    if len(values) != expected_total_rows:
        raise ValueError(
            f"AJIMEE raw JSON must contain exactly {expected_total_rows} cases"
        )

    rows = [_validate_upstream_row(value, position) for position, value in enumerate(values)]
    indices = [row["index"] for row in rows]
    if len(indices) != len(set(indices)):
        duplicates = sorted(index for index in set(indices) if indices.count(index) > 1)
        raise ValueError(f"AJIMEE raw JSON has duplicate canonical indices {duplicates!r}")

    contextual = sorted(
        (row for row in rows if row["case"]["left_context"]),
        key=lambda row: row["index"],
    )
    empty_count = len(rows) - len(contextual)
    if len(contextual) != expected_contextual_rows or empty_count != expected_empty_rows:
        raise ValueError(
            "AJIMEE raw JSON context partition does not match the contract; "
            f"contextual={len(contextual)}, empty={empty_count}"
        )

    cases = [row["case"] for row in contextual]
    cases_data = _render_jsonl(cases)
    # Reparse the exact rendered bytes, catching duplicate semantic sources and
    # any drift from the downstream schema before provenance is emitted.
    downstream_cases = blind.load_cases_bytes(cases_data, "AJIMEE contextual cases")
    if downstream_cases != cases:
        raise ValueError("AJIMEE contextual cases changed during downstream validation")

    selected_rows_data = _render_jsonl(row["source_row"] for row in contextual)
    contexts_data = _render_jsonl(
        {
            "id": case["id"],
            "left_context": case["left_context"],
            "left_context_sha256": _sha256(case["left_context"].encode("utf-8")),
        }
        for case in cases
    )
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "diagnostic_only": True,
        "formal_authorized": False,
        "annotation_tier": "silver_source",
        "bindings": {
            "raw_snapshot": {
                "repository": frozen.AJIMEE_REPOSITORY,
                "revision": frozen.AJIMEE_REVISION,
                "path": frozen.AJIMEE_RAW_PATH,
                "sha256": raw_sha256,
                "bytes": len(raw),
                "rows": len(rows),
            },
            "selected_upstream_rows": {
                "sha256": _sha256(selected_rows_data),
                "rows": len(contextual),
                "ordering": "numeric-upstream-index-ascending",
            },
            "cases": {
                "path": CASES_NAME,
                "schema": blind.CASE_SCHEMA,
                "sha256": _sha256(cases_data),
                "bytes": len(cases_data),
                "cases": len(cases),
            },
            "nonempty_context_projection": {
                "algorithm": CONTEXT_BINDING_ALGORITHM,
                "sha256": _sha256(contexts_data),
                "cases": len(cases),
                "all_nonempty": all(case["left_context"] != "" for case in cases),
            },
        },
        "counts": {
            "upstream_rows": len(rows),
            "upstream_contextual_rows": len(contextual),
            "upstream_empty_context_rows": empty_count,
            "emitted_cases": len(cases),
            "emitted_families": len({case["family_id"] for case in cases}),
            "dataset_roles": {"representative": len(cases)},
            "folds": {"exploration": len(cases)},
        },
        "rights": {
            "license": frozen.AJIMEE_LICENSE,
            "source_repository": frozen.AJIMEE_REPOSITORY,
            "source_revision": frozen.AJIMEE_REVISION,
            "source_path": frozen.AJIMEE_RAW_PATH,
            "attribution_and_share_alike_apply": True,
            "derivative_transform": True,
        },
        "transform": {
            "id": TRANSFORM_ID,
            "exact_upstream_fields": sorted(UPSTREAM_FIELDS),
            "selection": "context_text-is-nonempty",
            "ordering": "numeric-upstream-index-ascending",
            "unicode": "NFC-valid-scalars-no-controls-or-noncharacters",
            "reading": frozen.NORMALIZATION_ID,
            "expected_outputs": "stable-exact-deduplicate-preserve-first",
            "row_digest": ROW_DIGEST_ALGORITHM,
            "source_revision": (
                "ajimee-bench@<pinned-revision>:<pinned-raw-path>:"
                "row-sha256:<canonical-row-sha256>"
            ),
            "family_assignment": "one-family-per-canonical-upstream-index",
        },
        "candidate_blind_source": {
            "claim_scope": "this-importer-selection-and-transform-only",
            "selection_fields_consulted": ["context_text"],
            "selection_predicate": "context_text-is-nonempty",
            "engine_candidates_or_scores_consulted": False,
            "upstream_exact_schema_contains_engine_candidate_or_score_fields": False,
            "upstream_dataset_creation_blindness_claimed": False,
        },
        "contracts": {
            "all_upstream_rows_validated_before_selection": True,
            "all_emitted_contexts_nonempty": True,
            "case_schema_downstream_validated": True,
            "dataset_role": "representative",
            "fold": "exploration",
            "diagnostic_only": True,
            "formal_authorized": False,
            "raw_snapshot_bytes_included_in_generation": False,
        },
    }
    if manifest["bindings"]["nonempty_context_projection"]["all_nonempty"] is not True:
        raise AssertionError("contextual selection emitted an empty context")
    return {CASES_NAME: cases_data, MANIFEST_NAME: _render_json(manifest)}


def build_generation(raw: bytes) -> dict[str, bytes]:
    """Build against the one pinned contract accepted by the CLI."""

    return _build_generation_for_contract(
        raw,
        expected_raw_sha256=frozen.AJIMEE_RAW_SHA256,
        expected_total_rows=200,
        expected_contextual_rows=100,
        expected_empty_rows=100,
    )


def _read_regular(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValueError("AJIMEE raw JSON must be a regular non-symlink file") from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("AJIMEE raw JSON must be a regular non-symlink file")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, MAX_RAW_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_RAW_BYTES:
                raise ValueError(f"AJIMEE raw JSON exceeds {MAX_RAW_BYTES} bytes")
        after = os.fstat(descriptor)
        current = os.stat(path, follow_symlinks=False)
        identity = lambda metadata: (metadata.st_dev, metadata.st_ino, metadata.st_size)
        if identity(before) != identity(after) or identity(before) != identity(current):
            raise ValueError("AJIMEE raw JSON changed during the exact-byte read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _rename_noreplace(parent_fd: int, source: str, destination: str) -> None:
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
            parent_fd,
            os.fsencode(source),
            parent_fd,
            os.fsencode(destination),
            RENAME_NOREPLACE,
        )
        != 0
    ):
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number), destination)


def _directory_identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int]:
    return metadata.st_dev, metadata.st_ino, metadata.st_size


def _assert_directory_identity_at(
    parent_fd: int,
    name: str,
    expected_identity: tuple[int, int],
    context: str,
) -> None:
    metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(metadata.st_mode) or _directory_identity(metadata) != expected_identity:
        raise ValueError(f"{context} directory identity changed")


def _read_regular_at(directory_fd: int, name: str, context: str) -> tuple[bytes, os.stat_result]:
    descriptor = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=directory_fd,
    )
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
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            _file_identity(before) != _file_identity(after)
            or _file_identity(before) != _file_identity(current)
        ):
            raise ValueError(f"{context} identity changed during verification")
        return b"".join(chunks), after
    finally:
        os.close(descriptor)


def _verify_generation_fd(
    directory_fd: int,
    generated: dict[str, bytes],
    context: str,
) -> None:
    metadata = os.fstat(directory_fd)
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o555:
        raise ValueError(f"{context} directory mode changed")
    if set(os.listdir(directory_fd)) != set(generated):
        raise ValueError(f"{context} file set changed")
    for name, expected in generated.items():
        actual, file_metadata = _read_regular_at(directory_fd, name, f"{context} {name}")
        if (
            actual != expected
            or stat.S_IMODE(file_metadata.st_mode) != 0o444
            or file_metadata.st_nlink != 1
        ):
            raise ValueError(f"{context} output changed: {name}")


def _remove_entry_at(parent_fd: int, name: str) -> None:
    """Remove an untrusted child without following a symlink."""

    try:
        os.unlink(name, dir_fd=parent_fd)
        return
    except FileNotFoundError:
        return
    except IsADirectoryError:
        pass
    directory_fd = os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
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


def _remove_generation_at(
    parent_fd: int,
    name: str,
    *,
    expected_identity: tuple[int, int],
) -> None:
    try:
        directory_fd = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
    except FileNotFoundError:
        return
    try:
        if _directory_identity(os.fstat(directory_fd)) != expected_identity:
            raise ValueError("refusing to remove a generation with a changed identity")
        os.fchmod(directory_fd, 0o700)
        for child in os.listdir(directory_fd):
            _remove_entry_at(directory_fd, child)
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    _assert_directory_identity_at(
        parent_fd,
        name,
        expected_identity,
        "generation cleanup",
    )
    os.rmdir(name, dir_fd=parent_fd)
    os.fsync(parent_fd)


def publish_generation(generated: dict[str, bytes], output_dir: Path) -> None:
    if set(generated) != {CASES_NAME, MANIFEST_NAME}:
        raise ValueError("generated output set does not match the import contract")
    if not output_dir.name or output_dir.name in {".", ".."}:
        raise ValueError("output directory must name a new child directory")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    parent_fd = os.open(
        output_dir.parent,
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    staging_name = f".{output_dir.name}.staging-{secrets.token_hex(12)}"
    staging_created = False
    staging_fd: int | None = None
    staging_identity: tuple[int, int] | None = None
    renamed = False
    published = False
    try:
        os.mkdir(staging_name, 0o700, dir_fd=parent_fd)
        staging_created = True
        staging_identity = _directory_identity(
            os.stat(staging_name, dir_fd=parent_fd, follow_symlinks=False)
        )
        staging_fd = os.open(
            staging_name,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        if _directory_identity(os.fstat(staging_fd)) != staging_identity:
            raise ValueError("staging directory changed while it was opened")
        for name, data in generated.items():
            descriptor = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=staging_fd,
            )
            with os.fdopen(descriptor, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
                os.fchmod(output.fileno(), 0o444)
                os.fsync(output.fileno())
        os.fsync(staging_fd)
        os.fchmod(staging_fd, 0o555)
        os.fsync(staging_fd)
        _verify_generation_fd(staging_fd, generated, "staging generation")
        _assert_directory_identity_at(
            parent_fd,
            staging_name,
            staging_identity,
            "staging",
        )
        try:
            _rename_noreplace(parent_fd, staging_name, output_dir.name)
        except OSError as error:
            if error.errno == errno.EEXIST:
                raise ValueError("refusing to overwrite existing output directory") from error
            raise
        renamed = True
        os.fsync(parent_fd)
        _assert_directory_identity_at(
            parent_fd,
            output_dir.name,
            staging_identity,
            "published generation",
        )
        _verify_generation_fd(staging_fd, generated, "published generation")
        _assert_directory_identity_at(
            parent_fd,
            output_dir.name,
            staging_identity,
            "published generation",
        )
        published = True
    finally:
        if staging_fd is not None:
            os.close(staging_fd)
        if staging_created and not published and staging_identity is not None:
            cleanup_name = output_dir.name if renamed else staging_name
            _remove_generation_at(
                parent_fd,
                cleanup_name,
                expected_identity=staging_identity,
            )
        os.close(parent_fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Import exactly the pinned AJIMEE-Bench JWTD v2 contextual half "
            "as candidate-blind Silver annotation cases."
        )
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        raw = _read_regular(args.input)
        generated = build_generation(raw)
        publish_generation(generated, args.output_dir)
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(f"{_sha256(generated[MANIFEST_NAME])} {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
