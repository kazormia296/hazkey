#!/usr/bin/env python3
"""Prepare the diagnostic Mozc v2 normal-input-context probe contract.

This preparer consumes the exact sealed v2 corpus and the exact canonical
interaction-sidecar draft derived from it.  It emits only the 431 mechanically
separable ``normal_input_context_candidate`` cases: already-committed text is
left context, and only the terminal non-ASCII reading is the conversion target.

The output is review material, not converter output or formal gate evidence.
It remains ``not_ready`` and ``formal_authorized=false`` until the interaction
model and a product-path runner have been reviewed separately.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))

from tools.dictionary import audit_mozc_v2_interaction_model as interaction_audit
from tools.dictionary import prepare_mozc_v2_interaction_sidecar as interaction_sidecar


SCHEMA = "hazkey.mozc-v2-normal-input-context-probe-draft.v1"
SEALED_GENERATION = interaction_sidecar.SEALED_GENERATION
SEALED_GENERATION_SHA256 = interaction_sidecar.SEALED_GENERATION_SHA256
SEALED_CORPUS_SHA256 = interaction_sidecar.SEALED_CORPUS_SHA256
CURRENT_INTERACTION_SIDECAR_SHA256 = (
    "sha256:8108171333d653034262b00695e03e29daf97311088ec2c956a0bed0bd87cfe4"
)
CORPUS_NAME = interaction_sidecar.CORPUS_NAME
EXPECTED_NORMAL_INPUT_CASES = 431
EXPECTED_SIDECAR_CASES = 565
EXPECTED_REVIEW_REQUIRED_CASES = 134


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _read_bound_input(path: Path) -> bytes:
    """Read one non-hardlinked regular-file leaf through a pinned descriptor."""

    return interaction_sidecar._read_bound_input(path)


def _reject_constant(value: str) -> None:
    raise ValueError(f"interaction sidecar contains non-JSON constant {value!r}")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"interaction sidecar contains duplicate key {key!r}")
        result[key] = value
    return result


def _load_sidecar(data: bytes) -> dict[str, Any]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("interaction sidecar is not valid UTF-8") from error
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as error:
        raise ValueError("interaction sidecar is not valid JSON") from error
    if not isinstance(value, dict):
        raise ValueError("interaction sidecar root must be an object")
    return value


def _validate_current_sidecar(
    corpus_data: bytes,
    sidecar_data: bytes,
    *,
    generation_name: str,
) -> dict[str, Any]:
    digest = _sha256(sidecar_data)
    if digest != CURRENT_INTERACTION_SIDECAR_SHA256:
        raise ValueError(
            "interaction sidecar sha256 mismatch: expected "
            f"{CURRENT_INTERACTION_SIDECAR_SHA256}, got {digest}"
        )

    expected_report = interaction_sidecar.prepare_bytes(
        corpus_data,
        generation_name=generation_name,
    )
    expected_bytes = interaction_sidecar.canonical_json_bytes(expected_report)
    if sidecar_data != expected_bytes:
        raise ValueError(
            "interaction sidecar is not the canonical current draft for the "
            "sealed corpus"
        )

    parsed = _load_sidecar(sidecar_data)
    if parsed != expected_report:
        raise ValueError(
            "interaction sidecar JSON does not match the regenerated current draft"
        )
    return parsed


def _prepare_case(
    row: dict[str, str],
    sidecar_case: dict[str, Any],
) -> dict[str, Any]:
    case_id = row["id"]
    if sidecar_case.get("case_id") != case_id:
        raise ValueError(f"{case_id}: sidecar source id mismatch")
    if sidecar_case.get("category") != row["category"]:
        raise ValueError(f"{case_id}: sidecar category mismatch")
    if sidecar_case.get("reading") != row["reading"]:
        raise ValueError(f"{case_id}: sidecar reading mismatch")
    if sidecar_case.get("expected") != row["expected"]:
        raise ValueError(f"{case_id}: sidecar expected alternatives mismatch")
    if sidecar_case.get("review_status") != "pending_review":
        raise ValueError(f"{case_id}: normal-input candidate is not pending review")

    proposed = sidecar_case.get("proposed")
    if not isinstance(proposed, dict):
        raise ValueError(f"{case_id}: normal-input candidate proposal is missing")
    if proposed.get("scenario_kind") != "normal_input_context_candidate":
        raise ValueError(f"{case_id}: unexpected sidecar scenario kind")
    if proposed.get("formal_product_path_eligible") is not False:
        raise ValueError(f"{case_id}: draft must not be product-path eligible")
    if proposed.get("input_style") != "unknown_pending_review":
        raise ValueError(f"{case_id}: input style must remain pending review")
    if proposed.get("physical_key_trace") is not None:
        raise ValueError(f"{case_id}: draft must not infer a physical key trace")
    if proposed.get("requested_transform") is not None:
        raise ValueError(f"{case_id}: normal-input draft must not request F9/F10")

    left_context = proposed.get("committed_left_context")
    conversion_target = proposed.get("composition_reading")
    expected_candidates = proposed.get("expected_target")
    if not isinstance(left_context, str) or not left_context:
        raise ValueError(f"{case_id}: committed left context must be non-empty")
    if not any(ord(character) <= 0x7F for character in left_context):
        raise ValueError(f"{case_id}: committed left context must contain ASCII")
    if ord(left_context[-1]) > 0x7F:
        raise ValueError(f"{case_id}: committed left context must end with ASCII")
    if not isinstance(conversion_target, str) or not conversion_target:
        raise ValueError(f"{case_id}: conversion target must be non-empty")
    if any(ord(character) <= 0x7F for character in conversion_target):
        raise ValueError(f"{case_id}: conversion target must not contain ASCII")
    if not any(
        interaction_audit._is_kana_scalar(character)
        for character in conversion_target
    ):
        raise ValueError(f"{case_id}: conversion target must contain kana")
    if left_context + conversion_target != row["reading"]:
        raise ValueError(
            f"{case_id}: context and conversion target do not reconstruct reading"
        )

    full_alternatives = row["expected"].split("|")
    derived_candidates: list[str] = []
    for alternative in full_alternatives:
        if not alternative.startswith(left_context):
            raise ValueError(
                f"{case_id}: expected alternative does not preserve context"
            )
        candidate = alternative[len(left_context) :]
        if not candidate:
            raise ValueError(f"{case_id}: expected candidate must be non-empty")
        derived_candidates.append(candidate)
    if expected_candidates != derived_candidates:
        raise ValueError(f"{case_id}: expected candidate alternatives mismatch")

    expected_trace = [
        {
            "action": "update_context",
            "left_context": left_context,
            "right_context": "",
        },
        {
            "action": "conversion_boundary",
            "composition_reading": conversion_target,
        },
    ]
    if proposed.get("action_trace") != expected_trace:
        raise ValueError(f"{case_id}: sidecar action trace mismatch")
    if proposed.get("right_context") != "":
        raise ValueError(f"{case_id}: normal-input draft right context must be empty")

    return {
        "case_id": case_id,
        "category": row["category"],
        "conversion_target": conversion_target,
        "expected_candidates": derived_candidates,
        "left_context": left_context,
        "right_context": "",
        "source_ids": {
            "corpus_case_id": case_id,
            "interaction_sidecar_case_id": case_id,
        },
    }


def prepare_bytes(
    corpus_data: bytes,
    sidecar_data: bytes,
    *,
    generation_name: str,
) -> dict[str, Any]:
    corpus_digest = _sha256(corpus_data)
    if generation_name != SEALED_GENERATION:
        raise ValueError(
            f"sealed generation mismatch: expected {SEALED_GENERATION}, "
            f"got {generation_name}"
        )
    if corpus_digest != SEALED_CORPUS_SHA256:
        raise ValueError(
            f"sealed corpus sha256 mismatch: expected {SEALED_CORPUS_SHA256}, "
            f"got {corpus_digest}"
        )

    sidecar_report = _validate_current_sidecar(
        corpus_data,
        sidecar_data,
        generation_name=generation_name,
    )
    rows = interaction_audit._load_rows(corpus_data, CORPUS_NAME)
    row_by_id = {row["id"]: row for row in rows}
    if len(row_by_id) != len(rows):
        raise ValueError("sealed corpus case ids are not unique")

    sidecar_cases = sidecar_report.get("cases")
    if not isinstance(sidecar_cases, list):
        raise ValueError("interaction sidecar cases must be an array")
    if len(sidecar_cases) != EXPECTED_SIDECAR_CASES:
        raise ValueError(
            "interaction sidecar case count mismatch: expected "
            f"{EXPECTED_SIDECAR_CASES}, got {len(sidecar_cases)}"
        )

    selected: list[dict[str, Any]] = []
    selected_ids: list[str] = []
    review_required = 0
    for raw_case in sidecar_cases:
        if not isinstance(raw_case, dict):
            raise ValueError("interaction sidecar case must be an object")
        case_id = raw_case.get("case_id")
        if not isinstance(case_id, str) or case_id not in row_by_id:
            raise ValueError("interaction sidecar contains an unknown source id")
        if raw_case.get("review_status") == "action_trace_review_required":
            review_required += 1
            continue
        selected.append(_prepare_case(row_by_id[case_id], raw_case))
        selected_ids.append(case_id)

    if review_required != EXPECTED_REVIEW_REQUIRED_CASES:
        raise ValueError(
            "action-trace review case count mismatch: expected "
            f"{EXPECTED_REVIEW_REQUIRED_CASES}, got {review_required}"
        )
    if len(selected) != EXPECTED_NORMAL_INPUT_CASES:
        raise ValueError(
            "normal-input candidate count mismatch: expected "
            f"{EXPECTED_NORMAL_INPUT_CASES}, got {len(selected)}"
        )
    if len(selected_ids) != len(set(selected_ids)):
        raise ValueError("normal-input probe contains duplicate source ids")
    if selected_ids != sorted(selected_ids):
        raise ValueError("normal-input probe source ids must be sorted")

    return {
        "cases": selected,
        "contract": {
            "conversion_target": (
                "terminal non-ASCII reading only; committed prefix is excluded"
            ),
            "evidence_use": "diagnostic_only",
            "formal_product_path_eligible": False,
            "left_context": (
                "exact already-committed prefix proposed by the current draft sidecar"
            ),
            "runner": "not_executed_by_preparer",
        },
        "counts": {
            "excluded_action_trace_review_required": review_required,
            "normal_input_context_cases": len(selected),
            "sealed_corpus_cases": len(rows),
            "source_sidecar_cases": len(sidecar_cases),
        },
        "formal_authorized": False,
        "inputs": {
            "corpus": {
                "generation": generation_name,
                "generation_sha256": SEALED_GENERATION_SHA256,
                "name": CORPUS_NAME,
                "sha256": corpus_digest,
                "size_bytes": len(corpus_data),
            },
            "interaction_sidecar": {
                "schema": interaction_sidecar.SCHEMA,
                "sha256": _sha256(sidecar_data),
                "size_bytes": len(sidecar_data),
            },
        },
        "not_ready_reason": "interaction-review-and-product-path-runner-pending",
        "schema": SCHEMA,
        "status": "not_ready",
    }


def prepare_paths(corpus_path: Path, sidecar_path: Path) -> dict[str, Any]:
    if corpus_path.name != CORPUS_NAME:
        raise ValueError(f"corpus input filename must be {CORPUS_NAME}")
    corpus_data = _read_bound_input(corpus_path)
    sidecar_data = _read_bound_input(sidecar_path)
    return prepare_bytes(
        corpus_data,
        sidecar_data,
        generation_name=corpus_path.parent.name,
    )


def canonical_json_bytes(report: dict[str, Any]) -> bytes:
    text = json.dumps(
        report,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"
    return text.encode("utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--interaction-sidecar", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        report = prepare_paths(args.corpus, args.interaction_sidecar)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    sys.stdout.buffer.write(canonical_json_bytes(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
