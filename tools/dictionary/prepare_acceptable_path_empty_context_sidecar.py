#!/usr/bin/env python3
"""Derive an empty left-context sidecar from an acceptable-path generation.

The ABProbe v7 context contract deliberately keeps source text and raw context
outside result JSONL.  This helper binds an all-empty baseline to the reviewed
``source.row_sha256`` of every case in one exact, re-derived acceptable-path
generation.  It does not invent a natural context and cannot be used as one.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Iterable

try:
    from . import evaluate_mozc_acceptable_path_boundaries as acceptable
    from . import evaluate_zenzai_left_context_quality as context_quality
    from . import prepare_blind_silver_annotations as blind
    from . import prepare_mozc_fixed_boundary_sidecar as fixed_prepare
except ImportError:  # Direct execution from tools/dictionary.
    import evaluate_mozc_acceptable_path_boundaries as acceptable
    import evaluate_zenzai_left_context_quality as context_quality
    import prepare_blind_silver_annotations as blind
    import prepare_mozc_fixed_boundary_sidecar as fixed_prepare


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


def prepare_sidecar_bytes(generation_manifest: Path) -> bytes:
    """Validate and bind one all-empty sidecar to an exact generation."""

    blobs = acceptable._capture_generation(generation_manifest)
    manifest, bound = acceptable._validate_manifest(blobs)
    targets = acceptable._validate_targets(
        bound["targets"], "bound acceptable targets"
    )
    acceptable._validate_manifest_aggregates(manifest, targets)
    acceptable._validate_probe_binding(bound["probe_input"], targets)
    row_hashes = context_quality._reviewed_row_hashes(
        bound["reviewed_paths"], targets
    )
    empty_hash = acceptable._sha256(b"")
    return _canonical_jsonl(
        {
            "schema": blind.CONTEXT_SCHEMA,
            "id": target["id"],
            "source_content_sha256": row_hashes[target["id"]],
            "left_context": "",
            "left_context_sha256": empty_hash,
        }
        for target in targets
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--generation-manifest",
        required=True,
        type=Path,
        help="acceptable-path generation manifest.json",
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="empty-context JSONL to create without replacement",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        rendered = prepare_sidecar_bytes(arguments.generation_manifest)
        fixed_prepare._atomic_write(arguments.output, rendered)
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
