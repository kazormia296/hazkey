#!/usr/bin/env python3
"""Derive an auditable fixed-boundary sidecar from raw Mozc ABProbe output.

The sidecar carries only the Mozc Top-1 span needed by the probe-only
``mozc_fixed`` path.  Every row repeats the immutable identity of the exact raw
Mozc JSONL so a later evaluator can verify the complete result -> sidecar ->
Mozc chain instead of trusting a copied ``consuming_count``.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Iterable
import unicodedata

try:
    from . import evaluate_mozc_acceptable_path_boundaries as acceptable
    from . import evaluate_mozc_zenzai_hybrid_quality as quality
except ImportError:  # Direct execution from tools/dictionary.
    import evaluate_mozc_acceptable_path_boundaries as acceptable
    import evaluate_mozc_zenzai_hybrid_quality as quality


SIDECAR_SCHEMA = "hazkey.mozc-fixed-boundary.v1"
INPUT_SCHEMA_V6 = quality.INPUT_SCHEMA
CONVERSION_PATH = quality.CONVERSION_PATH
SIDECAR_FIELDS = {
    "schema",
    "id",
    "reading",
    "reading_sha256",
    "consuming_count",
    "origin",
}
ORIGIN_FIELDS = {
    "schema",
    "sha256",
    "cases",
    "converter_backend",
    "conversion_path",
}


def _canonical_jsonl(records: Iterable[dict[str, Any]]) -> bytes:
    return b"".join(
        json.dumps(
            record,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
        for record in records
    )


def _validate_source_record(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be an object")
    schema = value.get("schema")
    if schema != INPUT_SCHEMA_V6:
        raise ValueError(f"{where}.schema must be {INPUT_SCHEMA_V6!r}")
    normalized = quality._validate_v6_record(value, where)
    if normalized["converter_backend"] != "mozc":
        raise ValueError(f"{where}.converter_backend must be 'mozc'")
    if normalized["conversion_path"] != CONVERSION_PATH:
        raise ValueError(
            f"{where}.conversion_path must be {CONVERSION_PATH!r}"
        )
    reading = normalized["reading"]
    if reading != unicodedata.normalize("NFC", reading):
        raise ValueError(f"{where}.reading must be NFC-normalized")
    if not normalized["candidates"]:
        raise ValueError(
            f"{where}.candidates must contain a Mozc Top-1 boundary"
        )
    return normalized


def prepare_sidecar_bytes(raw_mozc: bytes, source: str = "Mozc ABProbe JSONL") -> bytes:
    records = acceptable._jsonl(raw_mozc, source)
    normalized: list[dict[str, Any]] = []
    observed_ids: set[str] = set()
    for index, record in enumerate(records, 1):
        case = _validate_source_record(record, f"{source}:{index}")
        if case["id"] in observed_ids:
            raise ValueError(f"{source}: duplicate id {case['id']!r}")
        observed_ids.add(case["id"])
        normalized.append(case)

    first = normalized[0]
    if first["corpus"]["cases"] != len(normalized):
        raise ValueError(f"{source}: corpus.cases does not match result count")
    consistency_fields = (
        "schema",
        "conversion_path",
        "backend",
        "backend_version",
        "converter_backend",
        "source_ref",
        "resource",
        "producer",
        "quality_policy",
        "top_k",
        "corpus",
    )
    for case in normalized[1:]:
        for field in consistency_fields:
            if case[field] != first[field]:
                raise ValueError(f"{source}: inconsistent {field} within run")
        if case["measurement"]["warmups"] != first["measurement"]["warmups"]:
            raise ValueError(f"{source}: inconsistent warmups within run")
        if case["measurement"]["iterations"] != first["measurement"]["iterations"]:
            raise ValueError(f"{source}: inconsistent iterations within run")
    origin = {
        "schema": INPUT_SCHEMA_V6,
        "sha256": acceptable._sha256(raw_mozc),
        "cases": len(normalized),
        "converter_backend": "mozc",
        "conversion_path": CONVERSION_PATH,
    }
    sidecar = []
    for case in normalized:
        count = case["candidates"][0]["consuming_count"]
        if count > len(case["reading"]):
            # The shared validator already enforces this; keep the derivation
            # invariant local and explicit for future contract changes.
            raise ValueError(
                f"{source}: case {case['id']!r} Top-1 consuming_count is out of range"
            )
        sidecar.append(
            {
                "schema": SIDECAR_SCHEMA,
                "id": case["id"],
                "reading": case["reading"],
                "reading_sha256": acceptable._sha256(
                    case["reading"].encode("utf-8")
                ),
                "consuming_count": count,
                "origin": origin,
            }
        )
    return _canonical_jsonl(sidecar)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary_path, path, follow_symlinks=False)
        except FileExistsError as error:
            raise FileExistsError(f"output already exists: {path}") from error
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mozc-results",
        required=True,
        type=Path,
        help="raw Mozc ABProbe v6 JSONL",
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="fixed-boundary sidecar JSONL to write",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        raw = acceptable._read_regular(
            arguments.mozc_results, "Mozc ABProbe results"
        )
        rendered = prepare_sidecar_bytes(raw, str(arguments.mozc_results))
        _atomic_write(arguments.output, rendered)
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
