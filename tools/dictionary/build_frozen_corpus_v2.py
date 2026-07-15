#!/usr/bin/env python3
"""Validate and assemble the sealed 1,360-case Mozc adoption holdout.

The seven category TSVs and their exact review approvals are the authoritative
reviewed inputs. A build is allowed only with a ready v2 policy, the pinned v1
pilot manifest, deterministic one-record-per-case provenance, a closed
near-duplicate review, and an exact v2 manifest. The checked-in policy is ready
only because all source rows and approvals are complete and independently
frozen.
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

try:
    from . import build_frozen_corpus
except ImportError:  # Direct execution from tools/dictionary.
    import build_frozen_corpus  # type: ignore[no-redef]


POLICY_SCHEMA = "hazkey.mozc-adoption-corpus-policy.v2"
MANIFEST_SCHEMA = "hazkey.frozen-conversion-corpus-manifest.v2"
PROVENANCE_SCHEMA = "hazkey.frozen-conversion-case-provenance.v2"
NEAR_REVIEW_SCHEMA = "hazkey.frozen-conversion-near-duplicate-review.v2"
REVIEW_APPROVALS_SCHEMA = "hazkey.frozen-conversion-review-approvals.v2"
REVIEW_APPROVALS_NAME = "review-approvals.json"
FAMILY_ASSIGNMENT_CONTRACT = "reviewed-case-to-family-map.v1"
POLICY_ID = "mozc-adoption-v2"
TOTAL_CASES = 1_360
QUALITY_CASES = 1_260
TSV_HEADER = b"id\treading\texpected\tcategory\n"

COMPONENT_CONTRACTS: tuple[dict[str, Any], ...] = (
    {
        "id": "technical-mixed",
        "category": "technical-mixed",
        "cases": 240,
        "tsv_path": "technical-mixed.tsv",
        "provenance_path": "technical-mixed.provenance.jsonl",
        "id_prefix": "v2-technical-",
    },
    {
        "id": "proper-noun",
        "category": "proper-noun",
        "cases": 200,
        "tsv_path": "proper-noun.tsv",
        "provenance_path": "proper-noun.provenance.jsonl",
        "id_prefix": "v2-proper-",
    },
    {
        "id": "colloquial",
        "category": "colloquial",
        "cases": 200,
        "tsv_path": "colloquial.tsv",
        "provenance_path": "colloquial.provenance.jsonl",
        "id_prefix": "v2-colloquial-",
    },
    {
        "id": "homophone-context",
        "category": "homophone-context",
        "cases": 200,
        "tsv_path": "homophone-context.tsv",
        "provenance_path": "homophone-context.provenance.jsonl",
        "id_prefix": "v2-homophone-",
    },
    {
        "id": "long-structural",
        "category": "long-structural",
        "cases": 200,
        "tsv_path": "long-structural.tsv",
        "provenance_path": "long-structural.provenance.jsonl",
        "id_prefix": "v2-long-",
    },
    {
        "id": "grimodex-regression",
        "category": "grimodex-regression",
        "cases": 220,
        "tsv_path": "grimodex-regression.tsv",
        "provenance_path": "grimodex-regression.provenance.jsonl",
        "id_prefix": "v2-grimodex-",
    },
    {
        "id": "protected",
        "category": "protected",
        "cases": 100,
        "tsv_path": "protected.tsv",
        "provenance_path": "protected.provenance.jsonl",
        "id_prefix": "v2-protected-",
    },
)

QUALITY_CATEGORIES = {
    contract["category"]: contract["cases"]
    for contract in COMPONENT_CONTRACTS
    if contract["category"] != "protected"
}
ALL_CATEGORIES = {
    contract["category"]: contract["cases"] for contract in COMPONENT_CONTRACTS
}
ELIGIBLE_CANDIDATE_IDS = ["B0", "B1"]
REQUIRED_CONTAMINATION_SCREENS = [
    "pilot-v1",
    "ajimee-bench",
    "sentinel-v1",
    "mozc-stress",
    "microsoft-ime-corpus",
    "zenz-v2.5-dataset",
]
FORBIDDEN_SOURCE_TOKENS = frozenset(
    {"pilot", "ajimee", "sentinel", "stress", "microsoft", "zenz"}
)
NEAR_NORMALIZATION = (
    "NFKC-casefold-strip-punctuation-and-separators-katakana-to-hiragana.v1"
)
JACCARD_THRESHOLD_BASIS_POINTS = 8_000
LEVENSHTEIN_THRESHOLD_BASIS_POINTS = 9_000
NEAR_ALGORITHMS = [
    {
        "id": "normalized-reading-character-3gram-jaccard.v1",
        "threshold_basis_points": JACCARD_THRESHOLD_BASIS_POINTS,
    },
    {
        "id": "normalized-reading-levenshtein-similarity.v1",
        "threshold_basis_points": LEVENSHTEIN_THRESHOLD_BASIS_POINTS,
    },
]

EXPECTED_ARTIFACT_FREEZES = {
    "eligible_candidate_ids": ELIGIBLE_CANDIDATE_IDS,
    "evaluation_runner": {
        "product_source_revision": "7373b1a59b2c94a9fada5650984c28ed352c3be1",
        "size_bytes": 106_269_232,
        "sha256": "sha256:249c43c8eb02651b685291ad47fd6bd85efac3438abd0a4d284dd1caec11f30a",
        "runtime_dependencies_integrity": "sha256:5d847919dbfb4b866546104cfbc73f5ffa9ff45ee9d8bc85889bf1de6c299f2d",
    },
    "candidates": {
        "B0": {
            "generation": "sha256-ad277af2ad5a634f23c7b84b7f346b02f341905f10fcfa6eb9912db78a0866cb",
            "helper_size_bytes": 5_695_048,
            "helper_sha256": "sha256:8676275bb47aefe963c8b82047cc66fb7a5140caec72d1ebbfa17556b281577d",
            "data_size_bytes": 18_887_468,
            "data_sha256": "sha256:b9884362e37772f772a0d28d1e12622455c14353497b3435deed60aa7e592c5e",
            "manifest_sha256": "sha256:ebdc1bff4da9fbafe3971de7e5f095c90ad78e00e3f40b10fa5a7249d78a7c16",
        },
        "B1": {
            "generation": "sha256-046bcfa093aac43ad6ee64afd4b3a3e8325bab0f3d20b8cb083c447ba8c91a2f",
            "helper_size_bytes": 5_746_568,
            "helper_sha256": "sha256:728d9a79c0f540a832d3f404a2603f49080e1f9e7ee1d24df1a0a69f5a4a75e8",
            "data_size_bytes": 18_887_468,
            "data_sha256": "sha256:b9884362e37772f772a0d28d1e12622455c14353497b3435deed60aa7e592c5e",
            "manifest_sha256": "sha256:c06ab9c7374ae1c4d114da6c0cabf2a6ef586e94449cb658a3c7927e4d30cb79",
        },
    },
    "one_shot_exposure": {
        "publish_only_after_all_eligible_candidates_are_frozen": True,
        "post_publication_candidate_policy": "new_holdout_required",
        "B2_eligible": False,
    },
}

EXPECTED_EXCLUSIONS = {
    "pilot_v1": {
        "counted": False,
        "manifest_sha256": "sha256:b1319e356ba025e1e06221330479d48b12cc44ebb502ddda970ea5fa583336e3",
        "aggregate_sha256": "sha256:123f47cb6f747135451e5969b32d9868ec61d9574fa6eb4b0001e5409287c807",
        "maximum_exact_overlap_cases": 0,
    },
    "auxiliary_suites": [
        "pilot-v1-256",
        "ajimee-unconditional",
        "sentinel-v1-15",
        "mozc-stress",
        "contextual",
    ],
}

EXPECTED_CASE_CONTRACT = {
    "tsv_columns": ["id", "reading", "expected", "category"],
    "provenance_schema": PROVENANCE_SCHEMA,
    "unique_case_ids": True,
    "unique_normalized_readings": True,
    "unique_family_ids": True,
    "quality_cases_require_conversion": True,
    "component_id_sequence": "id_prefix-plus-four-digit-decimal-0001-through-cases.v1",
    "locator_contract": "canonical-case-and-family-json.v1",
    "allowed_source_kinds": ["project-authored"],
    "allowed_licenses": ["MIT"],
    "required_exposure_status": "sealed-for-b0-b1",
    "required_contamination_screens": REQUIRED_CONTAMINATION_SCREENS,
}

EXPECTED_NEAR_REVIEW_CONTRACT = {
    "schema": NEAR_REVIEW_SCHEMA,
    "match": "either",
    "algorithms": NEAR_ALGORITHMS,
    "required_status": "closed",
}

REVIEW_IDENTIFIER_PATTERN = re.compile(r"[a-z0-9][a-z0-9._:-]{2,127}")


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
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ValueError(f"{context} must not contain control characters")
    return value


def _review_identifier(value: Any, context: str) -> str:
    result = _string(value, context)
    if REVIEW_IDENTIFIER_PATTERN.fullmatch(result) is None:
        raise ValueError(
            f"{context} must be a canonical lowercase review identifier"
        )
    return result


def _boolean(value: Any, context: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{context} must be a boolean")
    return value


def _positive_int(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{context} must be a positive integer")
    return value


def _sha256(value: Any, context: str) -> str:
    result = _string(value, context)
    if re.fullmatch(r"sha256:[0-9a-f]{64}", result) is None:
        raise ValueError(f"{context} must be sha256:<64 lowercase hex>")
    return result


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


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
        path_metadata = os.stat(path, follow_symlinks=False)
        identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mode,
            before.st_nlink,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        if identity != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mode,
            after.st_nlink,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) or identity != (
            path_metadata.st_dev,
            path_metadata.st_ino,
            path_metadata.st_size,
            path_metadata.st_mode,
            path_metadata.st_nlink,
            path_metadata.st_mtime_ns,
            path_metadata.st_ctime_ns,
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


def _load_json_file(path: Path, context: str) -> tuple[dict[str, Any], bytes]:
    data = _read_regular(path, context)
    return _object(_load_json_bytes(data, context), context), data


def _resolve_component_path(root: Path, actual: Any, expected: str, context: str) -> Path:
    if _string(actual, context) != expected:
        raise ValueError(f"{context} does not match the v2 policy")
    path = Path(expected)
    if path.is_absolute() or len(path.parts) != 1 or path.name in {"", ".", ".."}:
        raise ValueError(f"{context} is not a safe component filename")
    return root / path


def _parse_tsv(data: bytes, context: str) -> list[dict[str, str]]:
    return build_frozen_corpus._parse_tsv(data, context)


def _encode_rows(rows: list[dict[str, str]]) -> bytes:
    return build_frozen_corpus._encode_rows(rows)


def _load_jsonl(data: bytes, context: str) -> list[dict[str, Any]]:
    if b"\r" in data or not data.endswith(b"\n") or data.startswith(b"\xef\xbb\xbf"):
        raise ValueError(f"{context} must be BOM-free UTF-8 JSONL with LF endings")
    result: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(data[:-1].split(b"\n"), 1):
        if not raw_line:
            raise ValueError(f"{context}:{line_number} must not be blank")
        try:
            line = raw_line.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError(f"{context}:{line_number} is not valid UTF-8") from error
        try:
            value = json.loads(line, object_pairs_hook=_object_without_duplicate_keys)
        except json.JSONDecodeError as error:
            raise ValueError(
                f"{context}:{line_number} is invalid JSON: {error.msg}"
            ) from error
        result.append(_object(value, f"{context}:{line_number}"))
    return result


def _expected_formal_suite() -> dict[str, Any]:
    return {
        "total_cases": TOTAL_CASES,
        "quality_cases": QUALITY_CASES,
        "components": [dict(contract) for contract in COMPONENT_CONTRACTS],
        "quality_categories": QUALITY_CATEGORIES,
        "quality_metrics": ["top1", "top10", "human_preference", "both_bad"],
        "protected": {
            "cases": 100,
            "required_passes": 100,
            "metric": "top1_exact",
            "included_in_overall_quality_rates": False,
        },
    }


def validate_policy(
    policy_path: Path,
    *,
    require_ready: bool,
    expected_manifest_name: str | None = None,
) -> tuple[dict[str, Any], bytes]:
    policy, data = _load_json_file(policy_path, "v2 corpus policy")
    _require_exact_keys(
        policy,
        {
            "schema",
            "policy_id",
            "decision_tier",
            "collection",
            "formal_suite",
            "artifact_freezes",
            "exclusions",
            "case_contract",
            "near_duplicate_review",
        },
        "v2 corpus policy",
    )
    if policy["schema"] != POLICY_SCHEMA or policy["policy_id"] != POLICY_ID:
        raise ValueError("v2 corpus policy schema or policy_id mismatch")
    if policy["decision_tier"] != "formal":
        raise ValueError("v2 corpus policy must remain formal")
    if policy["formal_suite"] != _expected_formal_suite():
        raise ValueError("v2 corpus policy formal_suite does not match the fixed contract")
    if policy["artifact_freezes"] != EXPECTED_ARTIFACT_FREEZES:
        raise ValueError("v2 corpus policy artifact freezes do not match B0/B1")
    if policy["exclusions"] != EXPECTED_EXCLUSIONS:
        raise ValueError("v2 corpus policy exclusions do not match the fixed contract")
    if policy["case_contract"] != EXPECTED_CASE_CONTRACT:
        raise ValueError("v2 corpus policy case contract mismatch")
    if policy["near_duplicate_review"] != EXPECTED_NEAR_REVIEW_CONTRACT:
        raise ValueError("v2 corpus policy near-duplicate contract mismatch")

    collection = _object(policy["collection"], "v2 corpus policy.collection")
    _require_exact_keys(collection, {"status", "manifest_path"}, "collection")
    status = collection["status"]
    if status == "pending_collection":
        if collection["manifest_path"] is not None:
            raise ValueError("pending collection must not claim a manifest path")
        if require_ready:
            raise ValueError("v2 corpus policy is still pending_collection")
    elif status == "ready":
        manifest_name = _string(collection["manifest_path"], "collection.manifest_path")
        if expected_manifest_name is not None and manifest_name != expected_manifest_name:
            raise ValueError("ready policy does not bind the supplied manifest filename")
    else:
        raise ValueError("collection.status must be pending_collection or ready")
    return policy, data


def load_review_approvals(
    path: Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any], bytes]:
    approvals, data = _load_json_file(path, "v2 review approvals")
    _require_exact_keys(
        approvals,
        {"schema", "status", "components", "near_duplicate_review"},
        "v2 review approvals",
    )
    if approvals["schema"] != REVIEW_APPROVALS_SCHEMA:
        raise ValueError("v2 review approvals schema mismatch")
    if approvals["status"] != "approved":
        raise ValueError("v2 review approvals are not approved")

    raw_components = _array(approvals["components"], "review approvals.components")
    if len(raw_components) != len(COMPONENT_CONTRACTS):
        raise ValueError("review approvals must cover all seven components")
    result: dict[str, dict[str, Any]] = {}
    for raw, contract in zip(raw_components, COMPONENT_CONTRACTS, strict=True):
        context = f"review approval {contract['id']}"
        approval = _object(raw, context)
        _require_exact_keys(
            approval,
            {
                "id",
                "status",
                "tsv_sha256",
                "source_id",
                "author_id",
                "reviewer_id",
                "redistribution_approved",
                "privacy_reviewed",
                "family_assignment",
            },
            context,
        )
        if approval["id"] != contract["id"] or approval["status"] != "approved":
            raise ValueError(f"{context} is missing or not approved")
        _sha256(approval["tsv_sha256"], f"{context}.tsv_sha256")
        _review_identifier(approval["source_id"], f"{context}.source_id")
        author_id = _review_identifier(approval["author_id"], f"{context}.author_id")
        reviewer_id = _review_identifier(
            approval["reviewer_id"], f"{context}.reviewer_id"
        )
        if author_id.casefold() == reviewer_id.casefold():
            raise ValueError(f"{context} author and reviewer must be independent")
        if _boolean(
            approval["redistribution_approved"],
            f"{context}.redistribution_approved",
        ) is not True:
            raise ValueError(f"{context} lacks redistribution approval")
        if _boolean(
            approval["privacy_reviewed"], f"{context}.privacy_reviewed"
        ) is not True:
            raise ValueError(f"{context} lacks privacy approval")
        family = _object(approval["family_assignment"], f"{context}.family_assignment")
        _require_exact_keys(
            family,
            {"contract", "sha256"},
            f"{context}.family_assignment",
        )
        if family["contract"] != FAMILY_ASSIGNMENT_CONTRACT:
            raise ValueError(f"{context} family assignment contract mismatch")
        _sha256(family["sha256"], f"{context}.family_assignment.sha256")
        result[str(contract["id"])] = approval

    near = _object(
        approvals["near_duplicate_review"],
        "review approvals.near_duplicate_review",
    )
    _require_exact_keys(
        near,
        {"status", "computed_pairs", "reviewer_id", "algorithm"},
        "review approvals.near_duplicate_review",
    )
    computed_pairs = near["computed_pairs"]
    if (
        near["status"] != "closed"
        or isinstance(computed_pairs, bool)
        or not isinstance(computed_pairs, int)
        or computed_pairs < 0
    ):
        raise ValueError("review approvals do not close the near review")
    _review_identifier(
        near["reviewer_id"], "review approvals.near_duplicate_review.reviewer_id"
    )
    expected_algorithm = {
        "normalization": NEAR_NORMALIZATION,
        "match": "either",
        "algorithms": NEAR_ALGORITHMS,
    }
    if near["algorithm"] != expected_algorithm:
        raise ValueError("review approvals near-duplicate algorithm mismatch")
    return result, near, data


def _validate_provenance_record(
    record: dict[str, Any],
    row: dict[str, str],
    context: str,
    approval: dict[str, Any] | None = None,
) -> str:
    _require_exact_keys(
        record,
        {
            "schema",
            "case_id",
            "family_id",
            "source",
            "rights",
            "exposure",
            "contamination",
        },
        context,
    )
    if record["schema"] != PROVENANCE_SCHEMA:
        raise ValueError(f"{context}.schema mismatch")
    if record["case_id"] != row["id"]:
        raise ValueError(f"{context}.case_id does not match its TSV row")
    family_id = _string(record["family_id"], f"{context}.family_id")

    source = _object(record["source"], f"{context}.source")
    _require_exact_keys(
        source,
        {
            "kind",
            "source_id",
            "author_id",
            "locator_sha256",
            "license",
            "new_holdout",
        },
        f"{context}.source",
    )
    if source["kind"] not in EXPECTED_CASE_CONTRACT["allowed_source_kinds"]:
        raise ValueError(f"{context}.source.kind is not allowed")
    source_id = _review_identifier(
        source["source_id"], f"{context}.source.source_id"
    )
    author_id = _review_identifier(
        source["author_id"], f"{context}.source.author_id"
    )
    lowered_source_id = source_id.casefold()
    if any(token in lowered_source_id for token in FORBIDDEN_SOURCE_TOKENS):
        raise ValueError(f"{context}.source.source_id names an excluded auxiliary source")
    locator = _sha256(source["locator_sha256"], f"{context}.source.locator_sha256")
    if locator != case_locator_sha256(row, family_id):
        raise ValueError(f"{context}.source.locator_sha256 does not match case bytes")
    if source["license"] not in EXPECTED_CASE_CONTRACT["allowed_licenses"]:
        raise ValueError(f"{context}.source.license is not approved")
    if _boolean(source["new_holdout"], f"{context}.source.new_holdout") is not True:
        raise ValueError(f"{context}.source must be a new holdout")

    rights = _object(record["rights"], f"{context}.rights")
    _require_exact_keys(
        rights,
        {"redistribution_approved", "privacy_reviewed", "reviewer_id"},
        f"{context}.rights",
    )
    if _boolean(
        rights["redistribution_approved"],
        f"{context}.rights.redistribution_approved",
    ) is not True:
        raise ValueError(f"{context} lacks redistribution approval")
    if _boolean(rights["privacy_reviewed"], f"{context}.rights.privacy_reviewed") is not True:
        raise ValueError(f"{context} lacks privacy review")
    reviewer_id = _review_identifier(
        rights["reviewer_id"], f"{context}.rights.reviewer_id"
    )
    if author_id.casefold() == reviewer_id.casefold():
        raise ValueError(f"{context} author and reviewer are not independent")
    if approval is not None and (
        source_id != approval["source_id"]
        or author_id != approval["author_id"]
        or reviewer_id != approval["reviewer_id"]
    ):
        raise ValueError(f"{context} identities do not match review approval")

    exposure = _object(record["exposure"], f"{context}.exposure")
    _require_exact_keys(
        exposure,
        {"status", "eligible_candidate_ids", "disclosed_before_candidate_freezes"},
        f"{context}.exposure",
    )
    if exposure["status"] != EXPECTED_CASE_CONTRACT["required_exposure_status"]:
        raise ValueError(f"{context}.exposure.status mismatch")
    if exposure["eligible_candidate_ids"] != ELIGIBLE_CANDIDATE_IDS:
        raise ValueError(f"{context}.exposure must bind exact B0/B1 candidate IDs")
    if _boolean(
        exposure["disclosed_before_candidate_freezes"],
        f"{context}.exposure.disclosed_before_candidate_freezes",
    ) is not False:
        raise ValueError(f"{context} was disclosed before candidate freeze")

    contamination = _object(record["contamination"], f"{context}.contamination")
    _require_exact_keys(contamination, {"status", "screened_against"}, f"{context}.contamination")
    if contamination["status"] != "no-known-overlap":
        raise ValueError(f"{context}.contamination.status is not no-known-overlap")
    if contamination["screened_against"] != REQUIRED_CONTAMINATION_SCREENS:
        raise ValueError(f"{context}.contamination screens are incomplete")
    return family_id


def _canonical_reading(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return build_frozen_corpus.katakana_to_hiragana(normalized).casefold()


def _case_fingerprint(row: dict[str, str]) -> str:
    expected = sorted(
        unicodedata.normalize("NFKC", value).casefold()
        for value in row["expected"].split("|")
    )
    return sha256_bytes(
        _canonical_json({"reading": _canonical_reading(row["reading"]), "expected": expected})
    )


def case_locator_sha256(row: dict[str, str], family_id: str) -> str:
    """Bind provenance to the exact reviewed case bytes and derivation family."""

    return sha256_bytes(
        _canonical_json(
            {
                "category": row["category"],
                "expected": row["expected"],
                "family_id": family_id,
                "id": row["id"],
                "reading": row["reading"],
            }
        )
    )


def family_assignment_sha256(
    rows: list[dict[str, str]], family_ids: list[str]
) -> str:
    if len(rows) != len(family_ids):
        raise ValueError("family assignment count does not match TSV rows")
    return sha256_bytes(
        _canonical_json(
            [
                {"case_id": row["id"], "family_id": family_id}
                for row, family_id in zip(rows, family_ids, strict=True)
            ]
        )
    )


def _near_text(value: str) -> str:
    normalized = _canonical_reading(value)
    return "".join(
        character
        for character in normalized
        if unicodedata.category(character)[0] not in {"P", "Z"}
    )


def _trigrams(value: str) -> frozenset[str]:
    if not value:
        return frozenset()
    if len(value) < 3:
        return frozenset({value})
    return frozenset(value[index : index + 3] for index in range(len(value) - 2))


def _jaccard_basis_points(left: str, right: str) -> int:
    left_grams = _trigrams(left)
    right_grams = _trigrams(right)
    union = left_grams | right_grams
    if not union:
        return 10_000
    return len(left_grams & right_grams) * 10_000 // len(union)


def _levenshtein_distance(left: str, right: str) -> int:
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for left_index, left_character in enumerate(left, 1):
        current = [left_index]
        for right_index, right_character in enumerate(right, 1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1] + (left_character != right_character),
                )
            )
        previous = current
    return previous[-1]


def _levenshtein_similarity_basis_points(left: str, right: str) -> int:
    denominator = max(len(left), len(right))
    if denominator == 0:
        return 10_000
    return (denominator - _levenshtein_distance(left, right)) * 10_000 // denominator


def find_near_duplicate_pairs(
    formal_rows: list[dict[str, str]],
    pilot_rows: list[dict[str, str]],
) -> list[dict[str, Any]]:
    formal = [(row["id"], _near_text(row["reading"])) for row in formal_rows]
    pilot = [("pilot-v1:" + row["id"], _near_text(row["reading"])) for row in pilot_rows]
    if any(not text for _, text in formal):
        raise ValueError("formal reading becomes empty under near-duplicate normalization")

    pairs: list[dict[str, Any]] = []

    # Both locked metrics require at least one shared 3-gram for non-identical
    # strings that can reach their threshold.  Build the complete candidate set
    # from an inverted index instead of doing 1,360 x 1,615 edit-distance runs.
    # Readings shorter than three code points cannot reach 90% after one edit;
    # identical readings have already been rejected by the exact-overlap gate.
    formal_postings: dict[str, list[int]] = {}
    pilot_postings: dict[str, list[int]] = {}
    for index, (_, text) in enumerate(formal):
        for gram in _trigrams(text):
            formal_postings.setdefault(gram, []).append(index)
    for index, (_, text) in enumerate(pilot):
        for gram in _trigrams(text):
            pilot_postings.setdefault(gram, []).append(index)

    formal_candidates: set[tuple[int, int]] = set()
    pilot_candidates: set[tuple[int, int]] = set()
    for gram, formal_indices in formal_postings.items():
        for offset, left_index in enumerate(formal_indices):
            for right_index in formal_indices[offset + 1 :]:
                formal_candidates.add((left_index, right_index))
            for pilot_index in pilot_postings.get(gram, []):
                pilot_candidates.add((left_index, pilot_index))

    def consider(
        left: tuple[str, str],
        right: tuple[str, str],
        scope: str,
    ) -> None:
        jaccard = _jaccard_basis_points(left[1], right[1])
        maximum_length = max(len(left[1]), len(right[1]))
        maximum_possible_levenshtein = (
            10_000
            if maximum_length == 0
            else min(len(left[1]), len(right[1])) * 10_000 // maximum_length
        )
        if (
            jaccard < JACCARD_THRESHOLD_BASIS_POINTS
            and maximum_possible_levenshtein < LEVENSHTEIN_THRESHOLD_BASIS_POINTS
        ):
            return
        levenshtein = _levenshtein_similarity_basis_points(left[1], right[1])
        if (
            jaccard < JACCARD_THRESHOLD_BASIS_POINTS
            and levenshtein < LEVENSHTEIN_THRESHOLD_BASIS_POINTS
        ):
            return
        pairs.append(
            {
                "case_ids": sorted([left[0], right[0]]),
                "scope": scope,
                "jaccard_basis_points": jaccard,
                "levenshtein_basis_points": levenshtein,
            }
        )

    for left_index, right_index in sorted(formal_candidates):
        consider(formal[left_index], formal[right_index], "formal-v2")
    for formal_index, pilot_index in sorted(pilot_candidates):
        consider(
            formal[formal_index],
            pilot[pilot_index],
            "pilot-v1-to-formal-v2",
        )
    return sorted(pairs, key=lambda value: (value["scope"], value["case_ids"]))


def _validate_near_review(
    review: dict[str, Any],
    expected_pairs: list[dict[str, Any]],
    expected_reviewer_id: str | None = None,
) -> None:
    _require_exact_keys(
        review,
        {"schema", "status", "reviewer_id", "algorithm", "pairs"},
        "near review",
    )
    if review["schema"] != NEAR_REVIEW_SCHEMA or review["status"] != "closed":
        raise ValueError("near review schema or status is not closed")
    reviewer_id = _review_identifier(review["reviewer_id"], "near review.reviewer_id")
    if expected_reviewer_id is not None and reviewer_id != expected_reviewer_id:
        raise ValueError("near review reviewer does not match approval")
    expected_algorithm = {
        "normalization": NEAR_NORMALIZATION,
        "match": "either",
        "algorithms": NEAR_ALGORITHMS,
    }
    if review["algorithm"] != expected_algorithm:
        raise ValueError("near review algorithm does not match the v2 contract")

    expected_by_key = {
        (pair["scope"], tuple(pair["case_ids"])): pair for pair in expected_pairs
    }
    reviewed_by_key: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
    for index, raw_record in enumerate(_array(review["pairs"], "near review.pairs")):
        context = f"near review.pairs[{index}]"
        record = _object(raw_record, context)
        _require_exact_keys(
            record,
            {
                "case_ids",
                "scope",
                "jaccard_basis_points",
                "levenshtein_basis_points",
                "disposition",
                "reviewer_id",
                "rationale",
            },
            context,
        )
        case_ids = _array(record["case_ids"], f"{context}.case_ids")
        if len(case_ids) != 2 or any(not isinstance(value, str) or not value for value in case_ids):
            raise ValueError(f"{context}.case_ids must contain two non-empty strings")
        if case_ids != sorted(case_ids) or case_ids[0] == case_ids[1]:
            raise ValueError(f"{context}.case_ids must be distinct and sorted")
        scope = _string(record["scope"], f"{context}.scope")
        key = (scope, tuple(case_ids))
        if key in reviewed_by_key:
            raise ValueError(f"{context} duplicates a reviewed near pair")
        expected = expected_by_key.get(key)
        if expected is None:
            raise ValueError(f"{context} is not a computed near-duplicate pair")
        for metric in ("jaccard_basis_points", "levenshtein_basis_points"):
            if record[metric] != expected[metric]:
                raise ValueError(f"{context}.{metric} is stale or forged")
        if record["disposition"] != "distinct-reviewed":
            raise ValueError(f"{context}.disposition must be distinct-reviewed")
        _string(record["reviewer_id"], f"{context}.reviewer_id")
        _string(record["rationale"], f"{context}.rationale")
        reviewed_by_key[key] = record
    if set(reviewed_by_key) != set(expected_by_key):
        missing = sorted(set(expected_by_key) - set(reviewed_by_key))
        raise ValueError(f"near review is not closed; missing computed pairs: {missing[:5]!r}")


def _validate_pilot_v1(
    pilot_manifest_path: Path,
    pilot_contract: dict[str, Any],
) -> tuple[list[dict[str, str]], bytes]:
    manifest_bytes = _read_regular(pilot_manifest_path, "pinned pilot v1 manifest")
    if sha256_bytes(manifest_bytes) != pilot_contract["manifest_sha256"]:
        raise ValueError("pilot v1 manifest SHA-256 mismatch")
    aggregate = build_frozen_corpus.build_aggregate(pilot_manifest_path)
    if sha256_bytes(aggregate) != pilot_contract["aggregate_sha256"]:
        raise ValueError("pilot v1 aggregate SHA-256 mismatch")
    return _parse_tsv(aggregate, "pilot v1 aggregate"), aggregate


def build_aggregate(
    *,
    policy_path: Path,
    manifest_path: Path,
    pilot_v1_manifest_path: Path,
) -> bytes:
    policy, policy_bytes = validate_policy(
        policy_path,
        require_ready=True,
        expected_manifest_name=manifest_path.name,
    )
    manifest, _ = _load_json_file(manifest_path, "v2 corpus manifest")
    _require_exact_keys(
        manifest,
        {
            "schema",
            "policy",
            "review_approvals",
            "components",
            "near_duplicate_review",
            "pilot_v1",
            "aggregate",
        },
        "v2 corpus manifest",
    )
    if manifest["schema"] != MANIFEST_SCHEMA:
        raise ValueError("v2 corpus manifest schema mismatch")
    manifest_policy = _object(manifest["policy"], "v2 corpus manifest.policy")
    _require_exact_keys(manifest_policy, {"path", "sha256"}, "manifest.policy")
    if manifest_policy["path"] != policy_path.name:
        raise ValueError("manifest.policy.path does not name the supplied policy")
    if _sha256(manifest_policy["sha256"], "manifest.policy.sha256") != sha256_bytes(policy_bytes):
        raise ValueError("manifest policy exact-byte SHA-256 mismatch")

    root = manifest_path.parent
    approval_binding = _object(
        manifest["review_approvals"], "v2 corpus manifest.review_approvals"
    )
    _require_exact_keys(
        approval_binding,
        {"path", "sha256", "schema", "status"},
        "manifest.review_approvals",
    )
    approvals_path = _resolve_component_path(
        root,
        approval_binding["path"],
        REVIEW_APPROVALS_NAME,
        "manifest.review_approvals.path",
    )
    approvals_by_id, near_approval, approval_bytes = load_review_approvals(
        approvals_path
    )
    if approval_binding != {
        "path": REVIEW_APPROVALS_NAME,
        "sha256": sha256_bytes(approval_bytes),
        "schema": REVIEW_APPROVALS_SCHEMA,
        "status": "approved",
    }:
        raise ValueError("manifest review approval binding mismatch")

    pilot_contract = _object(manifest["pilot_v1"], "manifest.pilot_v1")
    _require_exact_keys(
        pilot_contract,
        {"manifest_sha256", "aggregate_sha256", "maximum_exact_overlap_cases"},
        "manifest.pilot_v1",
    )
    # The policy object has one additional explanatory `counted` field.
    expected_pilot = dict(policy["exclusions"]["pilot_v1"])
    expected_pilot.pop("counted")
    if pilot_contract != expected_pilot:
        raise ValueError("manifest pilot v1 binding does not match policy")
    pilot_rows, _ = _validate_pilot_v1(pilot_v1_manifest_path, policy["exclusions"]["pilot_v1"])

    raw_components = _array(manifest["components"], "manifest.components")
    if len(raw_components) != len(COMPONENT_CONTRACTS):
        raise ValueError("manifest must contain exactly seven category components")
    all_rows: list[dict[str, str]] = []
    seen_case_ids: set[str] = set()
    seen_readings: set[str] = set()
    seen_families: set[str] = set()

    for raw_component, contract in zip(raw_components, COMPONENT_CONTRACTS, strict=True):
        context = f"component {contract['id']}"
        approval = approvals_by_id[str(contract["id"])]
        component = _object(raw_component, context)
        _require_exact_keys(component, {"id", "tsv", "provenance"}, context)
        if component["id"] != contract["id"]:
            raise ValueError(f"{context}.id or order does not match policy")

        tsv = _object(component["tsv"], f"{context}.tsv")
        _require_exact_keys(tsv, {"path", "sha256", "cases"}, f"{context}.tsv")
        tsv_path = _resolve_component_path(
            root,
            tsv["path"],
            contract["tsv_path"],
            f"{context}.tsv.path",
        )
        tsv_data = _read_regular(tsv_path, f"{context} TSV")
        if _sha256(tsv["sha256"], f"{context}.tsv.sha256") != sha256_bytes(tsv_data):
            raise ValueError(f"{context} TSV exact-byte SHA-256 mismatch")
        if approval["tsv_sha256"] != sha256_bytes(tsv_data):
            raise ValueError(f"{context} TSV does not match its review approval")
        if _positive_int(tsv["cases"], f"{context}.tsv.cases") != contract["cases"]:
            raise ValueError(f"{context} TSV case contract mismatch")
        rows = _parse_tsv(tsv_data, f"{context} TSV")
        if len(rows) != contract["cases"]:
            raise ValueError(f"{context} TSV row count mismatch")
        if Counter(row["category"] for row in rows) != Counter(
            {contract["category"]: contract["cases"]}
        ):
            raise ValueError(f"{context} category count mismatch")

        provenance = _object(component["provenance"], f"{context}.provenance")
        _require_exact_keys(
            provenance,
            {"path", "sha256", "records"},
            f"{context}.provenance",
        )
        provenance_path = _resolve_component_path(
            root,
            provenance["path"],
            contract["provenance_path"],
            f"{context}.provenance.path",
        )
        provenance_data = _read_regular(provenance_path, f"{context} provenance")
        if _sha256(
            provenance["sha256"], f"{context}.provenance.sha256"
        ) != sha256_bytes(provenance_data):
            raise ValueError(f"{context} provenance exact-byte SHA-256 mismatch")
        if _positive_int(
            provenance["records"], f"{context}.provenance.records"
        ) != contract["cases"]:
            raise ValueError(f"{context} provenance record contract mismatch")
        records = _load_jsonl(provenance_data, f"{context} provenance")
        if len(records) != len(rows):
            raise ValueError(f"{context} TSV/provenance count mismatch")

        component_family_ids: list[str] = []
        for position, (row, record) in enumerate(
            zip(rows, records, strict=True), 1
        ):
            expected_case_id = f"{contract['id_prefix']}{position:04d}"
            if row["id"] != expected_case_id:
                raise ValueError(
                    f"{context} case ID sequence mismatch: expected "
                    f"{expected_case_id!r}, found {row['id']!r}"
                )
            if row["id"] in seen_case_ids:
                raise ValueError(f"duplicate formal case ID {row['id']!r}")
            if (
                row["category"] != "protected"
                and row["reading"] in row["expected"].split("|")
            ):
                raise ValueError(
                    f"quality case permits unchanged reading for {row['id']!r}"
                )
            seen_case_ids.add(row["id"])
            reading_key = _canonical_reading(row["reading"])
            if reading_key in seen_readings:
                raise ValueError(f"duplicate normalized formal reading for {row['id']!r}")
            seen_readings.add(reading_key)
            family_id = _validate_provenance_record(
                record,
                row,
                f"provenance {row['id']}",
                approval,
            )
            if family_id in seen_families:
                raise ValueError(f"duplicate formal family_id {family_id!r}")
            seen_families.add(family_id)
            component_family_ids.append(family_id)
        if family_assignment_sha256(rows, component_family_ids) != approval[
            "family_assignment"
        ]["sha256"]:
            raise ValueError(f"{context} family assignment review hash mismatch")
        all_rows.extend(rows)

    if len(all_rows) != TOTAL_CASES or Counter(
        row["category"] for row in all_rows
    ) != Counter(ALL_CATEGORIES):
        raise ValueError("formal aggregate counts do not match the 1,360-case policy")

    pilot_readings = {_canonical_reading(row["reading"]) for row in pilot_rows}
    pilot_fingerprints = {_case_fingerprint(row) for row in pilot_rows}
    exact_overlap_ids = {
        row["id"]
        for row in all_rows
        if _canonical_reading(row["reading"]) in pilot_readings
        or _case_fingerprint(row) in pilot_fingerprints
    }
    exact_overlap_count = len(exact_overlap_ids)
    if exact_overlap_count != 0:
        raise ValueError(f"formal v2 has {exact_overlap_count} exact overlaps with pilot v1")

    near_binding = _object(manifest["near_duplicate_review"], "manifest.near_duplicate_review")
    _require_exact_keys(
        near_binding,
        {"path", "sha256", "status"},
        "manifest.near_duplicate_review",
    )
    if near_binding["status"] != "closed":
        raise ValueError("manifest near-duplicate review is not closed")
    near_path = _resolve_component_path(
        root,
        near_binding["path"],
        "near-duplicate-review.json",
        "manifest.near_duplicate_review.path",
    )
    near_review, near_bytes = _load_json_file(near_path, "near-duplicate review")
    if _sha256(
        near_binding["sha256"], "manifest.near_duplicate_review.sha256"
    ) != sha256_bytes(near_bytes):
        raise ValueError("near-duplicate review exact-byte SHA-256 mismatch")
    expected_pairs = find_near_duplicate_pairs(all_rows, pilot_rows)
    if len(expected_pairs) != near_approval["computed_pairs"]:
        raise ValueError("near review pair count does not match approval")
    _validate_near_review(
        near_review,
        expected_pairs,
        near_approval["reviewer_id"],
    )

    aggregate_bytes = _encode_rows(all_rows)
    aggregate = _object(manifest["aggregate"], "manifest.aggregate")
    _require_exact_keys(
        aggregate,
        {
            "cases",
            "quality_cases",
            "sha256",
            "categories",
            "protected_included_in_overall_quality_rates",
            "exact_pilot_overlap_cases",
        },
        "manifest.aggregate",
    )
    if aggregate != {
        "cases": TOTAL_CASES,
        "quality_cases": QUALITY_CASES,
        "sha256": sha256_bytes(aggregate_bytes),
        "categories": ALL_CATEGORIES,
        "protected_included_in_overall_quality_rates": False,
        "exact_pilot_overlap_cases": 0,
    }:
        raise ValueError("manifest aggregate does not match the exact v2 output")
    return aggregate_bytes


def write_atomic_new(path: Path, data: bytes) -> None:
    if not path.parent.is_dir():
        raise ValueError(f"output parent does not exist: {path.parent}")
    descriptor, raw_temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw_temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
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
    validate = subparsers.add_parser("validate-policy")
    validate.add_argument("--policy", type=Path, required=True)
    build = subparsers.add_parser("build")
    build.add_argument("--policy", type=Path, required=True)
    build.add_argument("--manifest", type=Path, required=True)
    build.add_argument("--pilot-v1-manifest", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "validate-policy":
            policy, _ = validate_policy(args.policy, require_ready=False)
            print(f"{policy['collection']['status']} {args.policy}")
            return 0
        aggregate = build_aggregate(
            policy_path=args.policy,
            manifest_path=args.manifest,
            pilot_v1_manifest_path=args.pilot_v1_manifest,
        )
        write_atomic_new(args.output, aggregate)
        print(f"{sha256_bytes(aggregate)} {args.output}")
        return 0
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
