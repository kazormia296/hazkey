#!/usr/bin/env python3
"""Prepare a provenance-closed AJIMEE contextual full-conversion generation."""

from __future__ import annotations

import argparse
from collections import Counter
import errno
import json
import os
from pathlib import Path
import secrets
import stat
import sys
from typing import Any, Iterable

try:
    from . import compile_mozc_acceptable_path_evaluation as compiler
    from . import import_ajimee_contextual_blind_silver as importer
    from . import prepare_blind_silver_annotations as blind
except ImportError:  # Direct execution from tools/dictionary.
    import compile_mozc_acceptable_path_evaluation as compiler
    import import_ajimee_contextual_blind_silver as importer
    import prepare_blind_silver_annotations as blind


MANIFEST_SCHEMA = "hazkey.ajimee-contextual-full-evaluation-generation.v1"
TARGET_SCHEMA = "hazkey.ajimee-contextual-full-conversion-target.v1"
TRANSFORM_ID = "ajimee-contextual-full-evaluation.v1"
CATEGORY = "ajimee-jwtd-v2-contextual"

IMPORT_CASES_NAME = "import-cases.jsonl"
IMPORT_MANIFEST_NAME = "import-manifest.json"
PROBE_INPUT_NAME = "probe-input.jsonl"
TARGETS_NAME = "targets.jsonl"
CONTEXT_NAME = "context.jsonl"
EMPTY_CONTEXT_NAME = "context-empty.jsonl"
MANIFEST_NAME = "manifest.json"
OUTPUT_NAMES = frozenset(
    {
        IMPORT_CASES_NAME,
        IMPORT_MANIFEST_NAME,
        PROBE_INPUT_NAME,
        TARGETS_NAME,
        CONTEXT_NAME,
        EMPTY_CONTEXT_NAME,
        MANIFEST_NAME,
    }
)


def _render_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _render_jsonl(values: Iterable[dict[str, Any]]) -> bytes:
    return b"".join(importer._canonical_json(value) + b"\n" for value in values)


def _decode_json(data: bytes, context: str) -> dict[str, Any]:
    if data.startswith(b"\xef\xbb\xbf"):
        raise ValueError(f"{context} must not contain a UTF-8 BOM")
    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=importer._object_without_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{context} is not valid duplicate-free UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be an object")
    return value


def _decode_jsonl(data: bytes, context: str) -> list[dict[str, Any]]:
    return blind._load_jsonl(data, context)


def _probe_record(case: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": compiler.PROBE_INPUT_SCHEMA,
        "id": case["id"],
        "category": CATEGORY,
        "elements": [
            {"text": character, "input_style": "direct"}
            for character in case["reading"]
        ],
    }


def _target_record(
    case: dict[str, Any], source_content_sha256: str
) -> dict[str, Any]:
    return {
        "schema": TARGET_SCHEMA,
        "id": case["id"],
        "category": CATEGORY,
        "reading": case["reading"],
        "surface_references": list(case["surface_references"]),
        "source_content_sha256": source_content_sha256,
    }


def _validate_imported(
    imported: dict[str, bytes], expected: dict[str, bytes]
) -> None:
    expected_names = {importer.CASES_NAME, importer.MANIFEST_NAME}
    if set(imported) != expected_names:
        raise ValueError(
            "AJIMEE import generation file set differs; "
            f"expected={sorted(expected_names)!r}, actual={sorted(imported)!r}"
        )
    for name in sorted(expected_names):
        if imported[name] != expected[name]:
            raise ValueError(
                f"AJIMEE import generation {name} is not the exact raw-snapshot derivation"
            )


def _build_generation_for_contract(
    raw: bytes,
    imported: dict[str, bytes],
    *,
    expected_raw_sha256: str,
    expected_total_rows: int,
    expected_contextual_rows: int,
    expected_empty_rows: int,
) -> dict[str, bytes]:
    expected_import = importer._build_generation_for_contract(
        raw,
        expected_raw_sha256=expected_raw_sha256,
        expected_total_rows=expected_total_rows,
        expected_contextual_rows=expected_contextual_rows,
        expected_empty_rows=expected_empty_rows,
    )
    _validate_imported(imported, expected_import)
    cases_data = imported[importer.CASES_NAME]
    import_manifest_data = imported[importer.MANIFEST_NAME]
    cases = blind.load_cases_bytes(cases_data, "rederived AJIMEE import cases")
    import_manifest = _decode_json(
        import_manifest_data, "rederived AJIMEE import manifest"
    )
    prepared = blind.prepare_outputs_bytes(cases_data)
    context_data = prepared[blind.CONTEXT_NAME]
    empty_context_data = prepared[blind.EMPTY_CONTEXT_NAME]
    contexts = _decode_jsonl(context_data, "derived contextual sidecar")
    empty_contexts = _decode_jsonl(
        empty_context_data, "derived empty-context sidecar"
    )
    if len(contexts) != len(cases) or len(empty_contexts) != len(cases):
        raise ValueError("derived context sidecars do not cover every import case")

    source_hashes: dict[str, str] = {}
    for case, contextual, empty in zip(
        cases, contexts, empty_contexts, strict=True
    ):
        case_id = case["id"]
        if contextual["id"] != case_id or empty["id"] != case_id:
            raise ValueError("derived context sidecar order differs from import cases")
        if not contextual["left_context"]:
            raise ValueError(f"AJIMEE contextual case {case_id!r} has empty context")
        if empty["left_context"] != "":
            raise ValueError(f"AJIMEE empty-context case {case_id!r} is nonempty")
        if (
            contextual["source_content_sha256"]
            != empty["source_content_sha256"]
        ):
            raise ValueError(f"AJIMEE case {case_id!r} source binding differs")
        source_hashes[case_id] = contextual["source_content_sha256"]

    probe_data = _render_jsonl(_probe_record(case) for case in cases)
    targets_data = _render_jsonl(
        _target_record(case, source_hashes[case["id"]]) for case in cases
    )
    raw_binding = import_manifest["bindings"]["raw_snapshot"]
    bindings = {
        "raw_snapshot": dict(raw_binding),
        "import_cases": {
            "path": IMPORT_CASES_NAME,
            "schema": blind.CASE_SCHEMA,
            "sha256": importer._sha256(cases_data),
            "bytes": len(cases_data),
            "cases": len(cases),
        },
        "import_manifest": {
            "path": IMPORT_MANIFEST_NAME,
            "schema": importer.MANIFEST_SCHEMA,
            "sha256": importer._sha256(import_manifest_data),
            "bytes": len(import_manifest_data),
            "cases": len(cases),
        },
        "probe_input": {
            "path": PROBE_INPUT_NAME,
            "schema": compiler.PROBE_INPUT_SCHEMA,
            "sha256": importer._sha256(probe_data),
            "bytes": len(probe_data),
            "cases": len(cases),
        },
        "targets": {
            "path": TARGETS_NAME,
            "schema": TARGET_SCHEMA,
            "sha256": importer._sha256(targets_data),
            "bytes": len(targets_data),
            "cases": len(cases),
        },
        "context": {
            "path": CONTEXT_NAME,
            "schema": blind.CONTEXT_SCHEMA,
            "sha256": importer._sha256(context_data),
            "bytes": len(context_data),
            "cases": len(cases),
        },
        "empty_context": {
            "path": EMPTY_CONTEXT_NAME,
            "schema": blind.CONTEXT_SCHEMA,
            "sha256": importer._sha256(empty_context_data),
            "bytes": len(empty_context_data),
            "cases": len(cases),
        },
    }
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "diagnostic_only": True,
        "formal_authorized": False,
        "bindings": bindings,
        "counts": {
            "cases": len(cases),
            "families": len({case["family_id"] for case in cases}),
            "surface_references": sum(
                len(case["surface_references"]) for case in cases
            ),
            "cases_with_multiple_surface_references": sum(
                len(case["surface_references"]) > 1 for case in cases
            ),
            "dataset_roles": dict(
                sorted(Counter(case["dataset_role"] for case in cases).items())
            ),
            "folds": dict(
                sorted(Counter(case["fold"] for case in cases).items())
            ),
        },
        "rights": dict(import_manifest["rights"]),
        "source_import": {
            "schema": importer.MANIFEST_SCHEMA,
            "transform": dict(import_manifest["transform"]),
            "candidate_blind_source": dict(
                import_manifest["candidate_blind_source"]
            ),
        },
        "transform": {
            "id": TRANSFORM_ID,
            "ordering": "numeric-upstream-index-ascending",
            "probe_elements": "one-NFC-code-point-per-direct-composition-element.v1",
            "target": "full-composition-multiple-exact-surface-references.v1",
            "context_source_content_sha256": (
                "blind-source-canonical-json-payload-sha256.v1"
            ),
        },
        "contracts": {
            "raw_snapshot_exactly_pinned": True,
            "import_generation_exactly_rederived": True,
            "import_cases_and_provenance_copied_exactly": True,
            "all_contexts_nonempty": True,
            "empty_context_baseline_all_empty": True,
            "context_pair_source_content_hashes_equal": True,
            "surface_references_exact_and_multiple_allowed": True,
            "engine_candidates_or_scores_consulted": False,
            "diagnostic_only": True,
            "formal_authorized": False,
        },
    }
    return {
        IMPORT_CASES_NAME: cases_data,
        IMPORT_MANIFEST_NAME: import_manifest_data,
        PROBE_INPUT_NAME: probe_data,
        TARGETS_NAME: targets_data,
        CONTEXT_NAME: context_data,
        EMPTY_CONTEXT_NAME: empty_context_data,
        MANIFEST_NAME: _render_json(manifest),
    }


def build_generation(raw: bytes, imported: dict[str, bytes]) -> dict[str, bytes]:
    return _build_generation_for_contract(
        raw,
        imported,
        expected_raw_sha256=importer.frozen.AJIMEE_RAW_SHA256,
        expected_total_rows=200,
        expected_contextual_rows=100,
        expected_empty_rows=100,
    )


def _read_directory_files(
    directory: Path, expected_names: frozenset[str], context: str
) -> dict[str, bytes]:
    flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        directory_fd = os.open(directory, flags)
    except OSError as error:
        raise ValueError(f"{context} must be a non-symlink directory") from error
    try:
        directory_identity = importer._directory_identity(os.fstat(directory_fd))
        current = os.stat(directory, follow_symlinks=False)
        if (
            not stat.S_ISDIR(current.st_mode)
            or importer._directory_identity(current) != directory_identity
        ):
            raise ValueError(f"{context} directory identity changed while opening")
        names = set(os.listdir(directory_fd))
        if names != set(expected_names):
            raise ValueError(
                f"{context} file set differs; expected={sorted(expected_names)!r}, "
                f"actual={sorted(names)!r}"
            )
        result: dict[str, bytes] = {}
        for name in sorted(expected_names):
            data, metadata = importer._read_regular_at(
                directory_fd, name, f"{context} {name}"
            )
            if metadata.st_nlink != 1:
                raise ValueError(f"{context} {name} must have exactly one hard link")
            result[name] = data
        current = os.stat(directory, follow_symlinks=False)
        if (
            not stat.S_ISDIR(current.st_mode)
            or importer._directory_identity(current) != directory_identity
        ):
            raise ValueError(f"{context} directory identity changed during read")
        return result
    finally:
        os.close(directory_fd)


def read_import_generation(directory: Path) -> dict[str, bytes]:
    return _read_directory_files(
        directory,
        frozenset({importer.CASES_NAME, importer.MANIFEST_NAME}),
        "AJIMEE import generation",
    )


def capture_generation(manifest_path: Path) -> dict[str, bytes]:
    if manifest_path.name != MANIFEST_NAME:
        raise ValueError(f"generation manifest must be named {MANIFEST_NAME!r}")
    return _read_directory_files(
        manifest_path.parent, OUTPUT_NAMES, "AJIMEE full-evaluation generation"
    )


def _rederive_generation_for_contract(
    raw: bytes,
    actual: dict[str, bytes],
    *,
    expected_raw_sha256: str,
    expected_total_rows: int,
    expected_contextual_rows: int,
    expected_empty_rows: int,
) -> dict[str, bytes]:
    if set(actual) != set(OUTPUT_NAMES):
        raise ValueError("AJIMEE full-evaluation generation file set differs")
    imported = {
        importer.CASES_NAME: actual[IMPORT_CASES_NAME],
        importer.MANIFEST_NAME: actual[IMPORT_MANIFEST_NAME],
    }
    expected = _build_generation_for_contract(
        raw,
        imported,
        expected_raw_sha256=expected_raw_sha256,
        expected_total_rows=expected_total_rows,
        expected_contextual_rows=expected_contextual_rows,
        expected_empty_rows=expected_empty_rows,
    )
    for name in sorted(OUTPUT_NAMES):
        if actual[name] != expected[name]:
            raise ValueError(
                f"AJIMEE full-evaluation generation {name} is not exactly rederived"
            )
    return expected


def rederive_generation(raw: bytes, actual: dict[str, bytes]) -> dict[str, bytes]:
    return _rederive_generation_for_contract(
        raw,
        actual,
        expected_raw_sha256=importer.frozen.AJIMEE_RAW_SHA256,
        expected_total_rows=200,
        expected_contextual_rows=100,
        expected_empty_rows=100,
    )


def publish_generation(generated: dict[str, bytes], output_dir: Path) -> None:
    if set(generated) != set(OUTPUT_NAMES):
        raise ValueError("generated output set does not match the full-evaluation contract")
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
    staging_fd: int | None = None
    staging_identity: tuple[int, int] | None = None
    staging_created = False
    renamed = False
    published = False
    try:
        os.mkdir(staging_name, 0o700, dir_fd=parent_fd)
        staging_created = True
        staging_identity = importer._directory_identity(
            os.stat(staging_name, dir_fd=parent_fd, follow_symlinks=False)
        )
        staging_fd = os.open(
            staging_name,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        if importer._directory_identity(os.fstat(staging_fd)) != staging_identity:
            raise ValueError("staging directory changed while it was opened")
        for name, data in generated.items():
            descriptor = os.open(
                name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
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
        importer._verify_generation_fd(staging_fd, generated, "staging generation")
        importer._assert_directory_identity_at(
            parent_fd, staging_name, staging_identity, "staging"
        )
        try:
            importer._rename_noreplace(parent_fd, staging_name, output_dir.name)
        except OSError as error:
            if error.errno == errno.EEXIST:
                raise ValueError("refusing to overwrite existing output directory") from error
            raise
        renamed = True
        os.fsync(parent_fd)
        importer._assert_directory_identity_at(
            parent_fd, output_dir.name, staging_identity, "published generation"
        )
        importer._verify_generation_fd(staging_fd, generated, "published generation")
        importer._assert_directory_identity_at(
            parent_fd, output_dir.name, staging_identity, "published generation"
        )
        published = True
    finally:
        if staging_fd is not None:
            os.close(staging_fd)
        if staging_created and not renamed and staging_identity is not None:
            importer._remove_generation_at(
                parent_fd, staging_name, expected_identity=staging_identity
            )
        elif renamed and not published and staging_identity is not None:
            importer._remove_generation_at(
                parent_fd, output_dir.name, expected_identity=staging_identity
            )
        os.close(parent_fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare an exact AJIMEE contextual full-composition evaluation generation."
        )
    )
    parser.add_argument("--raw-snapshot", type=Path, required=True)
    parser.add_argument("--import-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        raw = importer._read_regular(args.raw_snapshot)
        imported = read_import_generation(args.import_dir)
        generated = build_generation(raw, imported)
        publish_generation(generated, args.output_dir)
    except (OSError, ValueError, AssertionError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(f"{importer._sha256(generated[MANIFEST_NAME])} {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
