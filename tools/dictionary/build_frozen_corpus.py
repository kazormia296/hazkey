#!/usr/bin/env python3
"""Derive and assemble the frozen 256-case Mozc adoption corpus.

The formal corpus is deliberately fail-closed.  The AJIMEE importer accepts
only the pinned upstream JSON snapshot, and the aggregate builder accepts only
the three reviewed component files and exact hashes described by the v1
manifest.  Outputs are linked into place atomically and are never overwritten.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import unicodedata
from typing import Any, Iterable


MANIFEST_SCHEMA = "hazkey.frozen-conversion-corpus-manifest.v1"
NORMALIZATION_ID = "katakana-to-hiragana.v1"
AJIMEE_TRANSFORM_ID = "ajimee-unconditional-to-tsv.v1"
AJIMEE_REPOSITORY = "https://github.com/azooKey/AJIMEE-Bench"
AJIMEE_REVISION = "401666cd56d1a570c2021798b64b6da4396bfd45"
AJIMEE_RAW_PATH = "JWTD_v2/v1/evaluation_items.json"
AJIMEE_RAW_SHA256 = (
    "sha256:e9eb668fd6aa14b1e26436f429b5550108af0a1dfd443b8cea0bcb3ab3028fca"
)
AJIMEE_LICENSE = "CC-BY-SA-3.0"
TSV_HEADER = "id\treading\texpected\tcategory\n"

COMPONENT_CONTRACTS = (
    {
        "id": "ajimee-unconditional",
        "path": "external-ajimee-unconditional.tsv",
        "cases": 100,
        "id_prefix": "ajimee-jwtd-v2-",
        "categories": {"ajimee-unconditional": 100},
        "provenance": {
            "kind": "external",
            "repository": AJIMEE_REPOSITORY,
            "revision": AJIMEE_REVISION,
            "raw_path": AJIMEE_RAW_PATH,
            "raw_sha256": AJIMEE_RAW_SHA256,
            "license": AJIMEE_LICENSE,
            "transform": AJIMEE_TRANSFORM_ID,
        },
    },
    {
        "id": "product-curated",
        "path": "product-curated.tsv",
        "cases": 140,
        "id_prefix": "product-",
        "categories": {
            "technical-mixed": 32,
            "proper-noun": 24,
            "colloquial": 24,
            "homophone-context": 20,
            "long-structural": 20,
            "grimodex-regression": 20,
        },
        "provenance": {
            "kind": "project",
            "license": "MIT",
            "source": "grimodex-curated-v1",
        },
    },
    {
        "id": "protected",
        "path": "protected.tsv",
        "cases": 16,
        "id_prefix": "protected-",
        "categories": {"protected": 16},
        "provenance": {
            "kind": "project",
            "license": "MIT",
            "source": "grimodex-protected-v1",
        },
    },
)
AGGREGATE_CATEGORIES = {
    category: count
    for component in COMPONENT_CONTRACTS
    for category, count in component["categories"].items()
}
FORBIDDEN_COMPONENT_TOKENS = frozenset(
    {"contextual", "sentinel", "stress", "microsoft", "zenz"}
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


def _string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{context} must be a non-empty string")
    return value


def _positive_int(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{context} must be a positive integer")
    return value


def _sha256(value: Any, context: str) -> str:
    result = _string(value, context)
    if re.fullmatch(r"sha256:[0-9a-f]{64}", result) is None:
        raise ValueError(f"{context} must be sha256:<64 lowercase hex>")
    return result


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _read_regular(path: Path, context: str) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValueError(f"{context} must be a regular non-symlink file") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"{context} must be a regular non-symlink file")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        path_metadata = os.stat(path, follow_symlinks=False)
        if (
            path_metadata.st_dev != metadata.st_dev
            or path_metadata.st_ino != metadata.st_ino
            or not stat.S_ISREG(path_metadata.st_mode)
        ):
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


def katakana_to_hiragana(value: str) -> str:
    """Apply the versioned v1 code-point mapping and NFC normalization."""

    converted: list[str] = []
    for character in unicodedata.normalize("NFC", value):
        codepoint = ord(character)
        if 0x30A1 <= codepoint <= 0x30F6 or 0x30FD <= codepoint <= 0x30FE:
            converted.append(chr(codepoint - 0x60))
        else:
            converted.append(character)
    return unicodedata.normalize("NFC", "".join(converted))


def _validate_text_field(value: Any, context: str) -> str:
    result = _string(value, context)
    if result != unicodedata.normalize("NFC", result):
        raise ValueError(f"{context} must be NFC-normalized")
    if any(character in result for character in ("\t", "\r", "\n")):
        raise ValueError(f"{context} must not contain TSV control characters")
    if any(unicodedata.category(character) == "Cc" for character in result):
        raise ValueError(f"{context} must not contain control characters")
    return result


def _encode_rows(rows: list[dict[str, str]]) -> bytes:
    return (
        TSV_HEADER
        + "".join(
            f"{row['id']}\t{row['reading']}\t{row['expected']}\t{row['category']}\n"
            for row in rows
        )
    ).encode("utf-8")


def derive_ajimee_bytes(
    raw: bytes,
    *,
    expected_raw_sha256: str = AJIMEE_RAW_SHA256,
) -> bytes:
    if sha256_bytes(raw) != expected_raw_sha256:
        raise ValueError("AJIMEE raw JSON SHA-256 does not match the pinned snapshot")
    values = _array(_load_json_bytes(raw, "AJIMEE raw JSON"), "AJIMEE raw JSON")
    if len(values) != 200:
        raise ValueError("AJIMEE raw JSON must contain exactly 200 cases")

    seen_indices: set[int] = set()
    unconditional: list[tuple[int, dict[str, str]]] = []
    contextual_count = 0
    expected_fields = {
        "index",
        "context_text",
        "input",
        "expected_output",
        "original_text",
        "splitted_input_for_limited_input_length",
    }
    for position, raw_item in enumerate(values):
        context = f"AJIMEE raw JSON[{position}]"
        item = _object(raw_item, context)
        _require_exact_keys(item, expected_fields, context)
        index_text = _string(item["index"], f"{context}.index")
        if re.fullmatch(r"0|[1-9][0-9]*", index_text) is None:
            raise ValueError(f"{context}.index must be a canonical decimal integer")
        index = int(index_text)
        if index in seen_indices:
            raise ValueError(f"AJIMEE raw JSON has duplicate index {index_text!r}")
        seen_indices.add(index)
        left_context = item["context_text"]
        if not isinstance(left_context, str):
            raise ValueError(f"{context}.context_text must be a string")
        _string(item["original_text"], f"{context}.original_text")
        split_input = _array(
            item["splitted_input_for_limited_input_length"],
            f"{context}.splitted_input_for_limited_input_length",
        )
        if any(not isinstance(value, str) for value in split_input):
            raise ValueError(f"{context}.splitted input values must be strings")
        raw_expected = _array(item["expected_output"], f"{context}.expected_output")
        if not raw_expected:
            raise ValueError(f"{context}.expected_output must not be empty")
        expected_values = [
            _validate_text_field(value, f"{context}.expected_output")
            for value in raw_expected
        ]
        # The pinned upstream snapshot contains one row whose distinct accepted
        # answers are repeated.  Preserve every distinct answer and remove only
        # exact duplicates, stably, as part of the versioned transform.
        expected_values = list(dict.fromkeys(expected_values))
        if any("|" in value for value in expected_values):
            raise ValueError(f"{context}.expected_output must not contain '|'")
        if left_context:
            contextual_count += 1
            continue
        reading = katakana_to_hiragana(
            _validate_text_field(item["input"], f"{context}.input")
        )
        unconditional.append(
            (
                index,
                {
                    "id": f"ajimee-jwtd-v2-{index:06d}",
                    "reading": reading,
                    "expected": "|".join(expected_values),
                    "category": "ajimee-unconditional",
                },
            )
        )
    if len(unconditional) != 100 or contextual_count != 100:
        raise ValueError("AJIMEE raw JSON must split into 100 unconditional and 100 contextual cases")
    return _encode_rows([row for _, row in sorted(unconditional)])


def _parse_tsv(data: bytes, context: str) -> list[dict[str, str]]:
    if b"\r" in data:
        raise ValueError(f"{context} must use LF line endings")
    if not data.endswith(b"\n"):
        raise ValueError(f"{context} must end with LF")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"{context} is not valid UTF-8") from error
    lines = text.splitlines()
    if not lines or lines[0] + "\n" != TSV_HEADER:
        raise ValueError(f"{context} must have the exact four-column TSV header")
    rows: list[dict[str, str]] = []
    for line_number, line in enumerate(lines[1:], 2):
        fields = line.split("\t")
        if len(fields) != 4:
            raise ValueError(f"{context}:{line_number} must contain exactly four columns")
        row = dict(zip(("id", "reading", "expected", "category"), fields, strict=True))
        for field, value in row.items():
            row[field] = _validate_text_field(value, f"{context}:{line_number}.{field}")
        alternatives = row["expected"].split("|")
        if any(not value for value in alternatives):
            raise ValueError(f"{context}:{line_number}.expected has an empty alternative")
        if len(alternatives) != len(set(alternatives)):
            raise ValueError(f"{context}:{line_number}.expected has duplicate alternatives")
        if katakana_to_hiragana(row["reading"]) != row["reading"]:
            raise ValueError(f"{context}:{line_number}.reading is not normalized by {NORMALIZATION_ID}")
        rows.append(row)
    return rows


def _validate_provenance(
    actual: Any, expected: dict[str, Any], context: str
) -> dict[str, Any]:
    value = _object(actual, context)
    _require_exact_keys(value, expected, context)
    if value != expected:
        raise ValueError(f"{context} does not match the frozen source contract")
    return value


def _validate_component(
    actual: Any,
    expected: dict[str, Any],
    manifest_directory: Path,
    seen_ids: set[str],
) -> tuple[dict[str, Any], bytes, list[dict[str, str]]]:
    context = f"component {expected['id']}"
    value = _object(actual, context)
    _require_exact_keys(
        value,
        {"id", "path", "sha256", "cases", "id_prefix", "categories", "provenance"},
        context,
    )
    for field in ("id", "path", "cases", "id_prefix"):
        if value[field] != expected[field]:
            raise ValueError(f"{context}.{field} does not match the v1 contract")
    if any(token in value["id"].lower() for token in FORBIDDEN_COMPONENT_TOKENS):
        raise ValueError(f"{context} is an excluded auxiliary suite")
    _sha256(value["sha256"], f"{context}.sha256")
    categories = _object(value["categories"], f"{context}.categories")
    if categories != expected["categories"]:
        raise ValueError(f"{context}.categories does not match the v1 contract")
    _validate_provenance(value["provenance"], expected["provenance"], f"{context}.provenance")

    component_path = manifest_directory / value["path"]
    data = _read_regular(component_path, context)
    if sha256_bytes(data) != value["sha256"]:
        raise ValueError(f"{context} exact-byte SHA-256 mismatch")
    rows = _parse_tsv(data, context)
    if len(rows) != value["cases"]:
        raise ValueError(f"{context} case count mismatch")
    counts = Counter(row["category"] for row in rows)
    if dict(sorted(counts.items())) != dict(sorted(categories.items())):
        raise ValueError(f"{context} category counts mismatch")
    for row in rows:
        if not row["id"].startswith(value["id_prefix"]):
            raise ValueError(f"{context} case {row['id']!r} has the wrong ID prefix")
        if row["id"] in seen_ids:
            raise ValueError(f"aggregate has duplicate case ID {row['id']!r}")
        seen_ids.add(row["id"])
    return value, data, rows


def build_aggregate(manifest_path: Path) -> bytes:
    manifest_data = _read_regular(manifest_path, "corpus manifest")
    manifest = _object(
        _load_json_bytes(manifest_data, str(manifest_path)),
        str(manifest_path),
    )
    _require_exact_keys(manifest, {"schema", "normalization", "components", "aggregate"}, str(manifest_path))
    if manifest["schema"] != MANIFEST_SCHEMA:
        raise ValueError(f"{manifest_path}.schema must be {MANIFEST_SCHEMA}")
    normalization = _object(manifest["normalization"], f"{manifest_path}.normalization")
    expected_normalization = {
        "unicode": "NFC",
        "line_endings": "LF",
        "reading_transform": NORMALIZATION_ID,
    }
    if normalization != expected_normalization:
        raise ValueError(f"{manifest_path}.normalization does not match the v1 contract")
    components = _array(manifest["components"], f"{manifest_path}.components")
    if len(components) != len(COMPONENT_CONTRACTS):
        raise ValueError(f"{manifest_path}.components must contain exactly three entries")

    seen_ids: set[str] = set()
    all_rows: list[dict[str, str]] = []
    for actual, expected in zip(components, COMPONENT_CONTRACTS, strict=True):
        _, _, rows = _validate_component(
            actual,
            expected,
            manifest_path.parent,
            seen_ids,
        )
        all_rows.extend(rows)
    aggregate_bytes = _encode_rows(all_rows)
    aggregate = _object(manifest["aggregate"], f"{manifest_path}.aggregate")
    _require_exact_keys(aggregate, {"cases", "sha256", "categories"}, f"{manifest_path}.aggregate")
    if _positive_int(aggregate["cases"], f"{manifest_path}.aggregate.cases") != 256:
        raise ValueError("aggregate must contain exactly 256 cases")
    categories = _object(aggregate["categories"], f"{manifest_path}.aggregate.categories")
    if categories != AGGREGATE_CATEGORIES:
        raise ValueError("aggregate category counts do not match the v1 contract")
    if Counter(row["category"] for row in all_rows) != Counter(AGGREGATE_CATEGORIES):
        raise ValueError("generated aggregate category counts do not match the manifest")
    expected_sha256 = _sha256(aggregate["sha256"], f"{manifest_path}.aggregate.sha256")
    if sha256_bytes(aggregate_bytes) != expected_sha256:
        raise ValueError("generated aggregate exact-byte SHA-256 mismatch")
    return aggregate_bytes


def write_atomic_new(path: Path, data: bytes) -> None:
    if not path.parent.is_dir():
        raise ValueError(f"output parent does not exist: {path.parent}")
    descriptor, raw_temporary = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temporary = Path(raw_temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o644)
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError as error:
            raise ValueError(f"refusing to overwrite existing output: {path}") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    derive = subparsers.add_parser("derive-ajimee")
    derive.add_argument("--input", type=Path, required=True)
    derive.add_argument("--output", type=Path, required=True)
    build = subparsers.add_parser("build")
    build.add_argument("--manifest", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "derive-ajimee":
            raw = _read_regular(args.input, "AJIMEE raw JSON")
            output = derive_ajimee_bytes(raw)
        else:
            output = build_aggregate(args.manifest)
        write_atomic_new(args.output, output)
        print(f"{sha256_bytes(output)} {args.output}")
        return 0
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
