#!/usr/bin/env python3
"""Prepare and score a deterministic, blind conversion-quality A/B review.

``prepare`` consumes two raw ABProbe v2 runs plus the matching corpus.  It
writes a reviewer-facing JSONL file containing only opaque case handles,
readings, categories, and candidates labelled ``x``/``y``.  Backend identity,
the corpus expectation, and the original case ID live only in a separate
unblinding key.

``score`` validates the review, key, and one complete judgment per case before
mapping ``x``/``y`` preferences back to backend names.  All hashes are SHA-256
over deterministic UTF-8 encodings; they are integrity checks, not signatures.
Keep the unblinding key private until review is complete.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
from typing import Any, Iterable

try:
    from .compare_conversion_quality import compare_reports
    from .evaluate_conversion_quality import (
        candidate_texts,
        evaluate,
        load_corpus_bytes,
    )
    from .summarize_ab_probe import INPUT_SCHEMA_V3, load_run_bytes
except ImportError:  # Direct execution from tools/dictionary.
    from compare_conversion_quality import compare_reports
    from evaluate_conversion_quality import candidate_texts, evaluate, load_corpus_bytes
    from summarize_ab_probe import INPUT_SCHEMA_V3, load_run_bytes


REVIEW_SCHEMA = "hazkey.blind-conversion-ab-review.v1"
KEY_SCHEMA = "hazkey.blind-conversion-ab-key.v1"
JUDGMENT_SCHEMA = "hazkey.blind-conversion-ab-judgment.v1"
REPORT_SCHEMA = "hazkey.blind-conversion-ab-report.v1"
PACKET_SCHEMA = "hazkey.blind-conversion-ab-packet.v1"
ALLOWED_JUDGMENTS = frozenset({"x", "y", "tie", "both_bad"})
REVIEW_NAME = "review.jsonl"
KEY_NAME = "unblind-key.json"
MANIFEST_NAME = "manifest.json"


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _validate_sha256(value: Any, context: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 71
        or not value.startswith("sha256:")
    ):
        raise ValueError(f"{context} must be a sha256:<64 lowercase hex> digest")
    digest = value.removeprefix("sha256:")
    if any(character not in "0123456789abcdef" for character in digest):
        raise ValueError(f"{context} must be a sha256:<64 lowercase hex> digest")
    return value


def _string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{context} must be a non-empty string")
    return value


def _nonnegative_int(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{context} must be a non-negative integer")
    return value


def _object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be an object")
    return value


def _array(value: Any, context: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{context} must be an array")
    return value


def _require_exact_keys(
    value: dict[str, Any], expected: Iterable[str], context: str
) -> None:
    expected_set = set(expected)
    actual_set = set(value)
    if actual_set != expected_set:
        missing = sorted(expected_set - actual_set)
        unknown = sorted(actual_set - expected_set)
        raise ValueError(
            f"{context} fields do not match schema; "
            f"missing={missing!r}, unknown={unknown!r}"
        )


def _load_json_bytes(data: bytes, context: str) -> dict[str, Any]:
    try:
        payload = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_object_without_duplicate_keys,
        )
    except UnicodeDecodeError as error:
        raise ValueError(f"{context}: file is not valid UTF-8") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"{context}: invalid JSON: {error.msg}") from error
    return _object(payload, context)


def _load_jsonl_bytes(
    data: bytes, context: str
) -> list[tuple[int, dict[str, Any]]]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"{context}: file is not valid UTF-8") from error
    rows: list[tuple[int, dict[str, Any]]] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            raise ValueError(
                f"{context}:{line_number}: blank lines are not allowed"
            )
        try:
            payload = json.loads(
                line,
                object_pairs_hook=_object_without_duplicate_keys,
            )
        except json.JSONDecodeError as error:
            raise ValueError(
                f"{context}:{line_number}: invalid JSON: {error.msg}"
            ) from error
        rows.append((line_number, _object(payload, f"{context}:{line_number}")))
    if not rows:
        raise ValueError(f"{context}: file has no records")
    return rows


def _load_seed_file(path: Path) -> str:
    data = _read_owner_only_regular(path, "seed file")
    try:
        lines = data.decode("ascii").splitlines()
    except UnicodeDecodeError as error:
        raise ValueError(f"{path}: seed must be ASCII") from error
    if len(lines) != 1 or len(lines[0]) != 64:
        raise ValueError(f"{path}: seed must be exactly 64 lowercase hex digits")
    if any(character not in "0123456789abcdef" for character in lines[0]):
        raise ValueError(f"{path}: seed must be exactly 64 lowercase hex digits")
    return lines[0]


def _read_owner_only_regular(path: Path, label: str) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValueError(
            f"{path}: {label} must be owner-only, regular, and non-symlink"
        ) from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise ValueError(
                f"{path}: {label} must be owner-only, regular, and non-symlink"
            )
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            return handle.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _domain_digest(seed: bytes, domain: str, *parts: str) -> bytes:
    message = _canonical_json([domain, *parts])
    return hmac.new(seed, message, hashlib.sha256).digest()


def _backend_metadata(run: dict[str, Any], data: bytes) -> dict[str, Any]:
    return {
        "converter_backend": run["converter_backend"],
        "probe_backend": run["backend"],
        "backend_version": run["backend_version"],
        "measurement": {
            "warmups": run["warmups"],
            "iterations": run["iterations"],
        },
        "resource": run["resource"],
        "run_sha256": _sha256_bytes(data),
    }


def _case_contract(corpus: list[dict[str, str]]) -> list[dict[str, Any]]:
    return sorted(
        (
            {
                "id": row["id"],
                "reading": row["reading"],
                "expected": row["expected"].split("|"),
                "category": row["category"],
            }
            for row in corpus
        ),
        key=lambda case: case["id"],
    )


def _validate_runs(
    corpus: list[dict[str, str]],
    corpus_sha256: str,
    runs: list[tuple[Path, dict[str, Any]]],
) -> None:
    for path, run in runs:
        if run["schema"] != INPUT_SCHEMA_V3:
            raise ValueError(f"{path}: blind A/B requires {INPUT_SCHEMA_V3}")
    labels = [run["converter_backend"] for _, run in runs]
    if len(set(labels)) != 2:
        raise ValueError("probe runs must use two distinct converter_backend values")
    source_refs = {run["source_ref"] for _, run in runs}
    if len(source_refs) != 1:
        raise ValueError("probe runs must have an identical source_ref")
    for field in ("backend_version", "warmups", "iterations", "top_k", "corpus"):
        values = [run[field] for _, run in runs]
        if any(value != values[0] for value in values[1:]):
            raise ValueError(f"probe runs must have an identical {field}")
    expected_corpus = {"sha256": corpus_sha256, "cases": len(corpus)}
    if runs[0][1]["corpus"] != expected_corpus:
        raise ValueError("probe corpus provenance does not match the supplied corpus")

    corpus_ids = {row["id"] for row in corpus}
    corpus_categories = {row["id"]: row["category"] for row in corpus}
    corpus_readings = {row["id"]: row["reading"] for row in corpus}
    for path, run in runs:
        run_ids = set(run["cases"])
        if run_ids != corpus_ids:
            raise ValueError(
                f"{path}: case set does not match corpus; "
                f"missing={sorted(corpus_ids - run_ids)!r}, "
                f"unexpected={sorted(run_ids - corpus_ids)!r}"
            )
        for case_id, category in corpus_categories.items():
            run_case = run["cases"][case_id]
            if run_case["category"] != category:
                raise ValueError(
                    f"{path}: category for {case_id!r} is "
                    f"{run_case['category']!r}; "
                    f"corpus requires {category!r}"
                )
            expected_reading = corpus_readings[case_id]
            if run_case["reading"] != expected_reading:
                raise ValueError(
                    f"{path}: reading for {case_id!r} does not match corpus"
                )


def build_review_and_key(
    corpus_path: Path,
    first_run_path: Path,
    second_run_path: Path,
    seed: str,
) -> tuple[bytes, dict[str, Any]]:
    """Build reviewer JSONL bytes and its private unblinding key."""

    if len(seed) != 64 or any(
        character not in "0123456789abcdef" for character in seed
    ):
        raise ValueError("seed must be exactly 64 lowercase hex digits")
    corpus_bytes = corpus_path.read_bytes()
    first_run_bytes = first_run_path.read_bytes()
    second_run_bytes = second_run_path.read_bytes()
    corpus = load_corpus_bytes(corpus_bytes, str(corpus_path))
    first_run = load_run_bytes(first_run_bytes, first_run_path)
    second_run = load_run_bytes(second_run_bytes, second_run_path)
    runs = [(first_run_path, first_run), (second_run_path, second_run)]
    corpus_sha256 = _sha256_bytes(corpus_bytes)
    _validate_runs(corpus, corpus_sha256, runs)

    seed_bytes = bytes.fromhex(seed)
    contract = _case_contract(corpus)
    case_contract_sha256 = _sha256_bytes(_canonical_json(contract))
    by_id = {row["id"]: row for row in corpus}
    case_ids = list(by_id)
    ordered_ids = sorted(
        case_ids,
        key=lambda case_id: _domain_digest(
            seed_bytes, "review-order-v1", corpus_sha256, case_id
        ),
    )
    target_first_x_count = len(case_ids) // 2
    if len(case_ids) % 2 and (
        _domain_digest(seed_bytes, "odd-owner-v1", corpus_sha256)[0] & 1
    ):
        target_first_x_count += 1

    # Balance each category as well as the complete corpus.  Every category
    # starts with floor(n/2) first-backend X placements.  Deterministically
    # selected odd-sized categories receive the remaining placements needed to
    # hit the globally balanced target.
    ids_by_category: dict[str, list[str]] = {}
    for row in corpus:
        ids_by_category.setdefault(row["category"], []).append(row["id"])
    first_x_by_category = {
        category: len(ids) // 2 for category, ids in ids_by_category.items()
    }
    extras_needed = target_first_x_count - sum(first_x_by_category.values())
    odd_categories = sorted(
        (
            category
            for category, ids in ids_by_category.items()
            if len(ids) % 2
        ),
        key=lambda category: _domain_digest(
            seed_bytes, "category-extra-v1", corpus_sha256, category
        ),
    )
    for category in odd_categories[:extras_needed]:
        first_x_by_category[category] += 1
    first_on_x: set[str] = set()
    for category, ids in ids_by_category.items():
        ordered_category_ids = sorted(
            ids,
            key=lambda case_id: _domain_digest(
                seed_bytes,
                "placement-v1",
                corpus_sha256,
                category,
                case_id,
            ),
        )
        first_on_x.update(
            ordered_category_ids[: first_x_by_category[category]]
        )

    first_label = first_run["converter_backend"]
    second_label = second_run["converter_backend"]
    opaque_ids: set[str] = set()
    review_records: list[dict[str, Any]] = []
    key_cases: list[dict[str, Any]] = []
    placement: dict[str, Counter[str]] = {
        "x": Counter({first_label: 0, second_label: 0}),
        "y": Counter({first_label: 0, second_label: 0}),
    }
    for original_id in ordered_ids:
        opaque_id = "blind-" + _domain_digest(
            seed_bytes, "opaque-case-v1", corpus_sha256, original_id
        ).hex()[:24]
        if opaque_id in opaque_ids:
            raise ValueError("opaque case handle collision")
        opaque_ids.add(opaque_id)
        if original_id in first_on_x:
            x_run, y_run = first_run, second_run
            x_backend, y_backend = first_label, second_label
        else:
            x_run, y_run = second_run, first_run
            x_backend, y_backend = second_label, first_label
        placement["x"][x_backend] += 1
        placement["y"][y_backend] += 1

        corpus_row = by_id[original_id]
        review_base = {
            "schema": REVIEW_SCHEMA,
            "case": opaque_id,
            "reading": corpus_row["reading"],
            "category": corpus_row["category"],
            "x": candidate_texts(
                {"candidates": x_run["cases"][original_id]["candidates"]}
            ),
            "y": candidate_texts(
                {"candidates": y_run["cases"][original_id]["candidates"]}
            ),
        }
        record_sha256 = _sha256_bytes(_canonical_json(review_base))
        review_records.append(review_base | {"integrity": record_sha256})
        key_cases.append(
            {
                "case": opaque_id,
                "original_id": original_id,
                "reading": corpus_row["reading"],
                "category": corpus_row["category"],
                "expected": corpus_row["expected"].split("|"),
                "x_backend": x_backend,
                "y_backend": y_backend,
                "review_record_sha256": record_sha256,
            }
        )

    review_bytes = b"".join(
        _canonical_json(record) + b"\n" for record in review_records
    )
    backend_metadata = sorted(
        (
            _backend_metadata(first_run, first_run_bytes),
            _backend_metadata(second_run, second_run_bytes),
        ),
        key=lambda metadata: metadata["converter_backend"],
    )
    key_base = {
        "schema": KEY_SCHEMA,
        "review_schema": REVIEW_SCHEMA,
        "seed_sha256": _sha256_bytes(seed_bytes),
        "source_ref": first_run["source_ref"],
        "top_k": first_run["top_k"],
        "corpus": {
            "sha256": corpus_sha256,
            "case_contract_sha256": case_contract_sha256,
            "cases": len(corpus),
        },
        "review_sha256": _sha256_bytes(review_bytes),
        "backends": backend_metadata,
        "placement": {
            side: dict(sorted(counts.items()))
            for side, counts in placement.items()
        },
        "cases": key_cases,
    }
    return review_bytes, key_base | {
        "integrity": _sha256_bytes(_canonical_json(key_base))
    }


def prepare(
    corpus_path: Path,
    first_run_path: Path,
    second_run_path: Path,
    seed: str,
    output_directory: Path,
) -> dict[str, Any]:
    review_bytes, key = build_review_and_key(
        corpus_path, first_run_path, second_run_path, seed
    )
    key_bytes = (
        json.dumps(key, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    manifest_base = {
        "schema": PACKET_SCHEMA,
        "cases": key["corpus"]["cases"],
        "source_ref": key["source_ref"],
        "top_k": key["top_k"],
        "review": {"name": REVIEW_NAME, "sha256": key["review_sha256"]},
        "key": {
            "name": KEY_NAME,
            "sha256": _sha256_bytes(key_bytes),
            "integrity": key["integrity"],
        },
    }
    manifest = manifest_base | {
        "integrity": _sha256_bytes(_canonical_json(manifest_base))
    }
    manifest_bytes = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    _publish_packet(
        output_directory,
        {
            REVIEW_NAME: review_bytes,
            KEY_NAME: key_bytes,
            MANIFEST_NAME: manifest_bytes,
        },
    )
    return manifest


def _validate_key(path: Path, data: bytes | None = None) -> dict[str, Any]:
    if data is None:
        data = path.read_bytes()
    key = _load_json_bytes(data, str(path))
    _require_exact_keys(
        key,
        {
            "schema",
            "review_schema",
            "seed_sha256",
            "source_ref",
            "top_k",
            "corpus",
            "review_sha256",
            "backends",
            "placement",
            "cases",
            "integrity",
        },
        str(path),
    )
    if key["schema"] != KEY_SCHEMA or key["review_schema"] != REVIEW_SCHEMA:
        raise ValueError(f"{path}: unsupported key schema")
    _validate_sha256(key["seed_sha256"], f"{path}.seed_sha256")
    _validate_sha256(key["review_sha256"], f"{path}.review_sha256")
    _string(key["source_ref"], f"{path}.source_ref")
    top_k = _nonnegative_int(key["top_k"], f"{path}.top_k")
    if not 1 <= top_k <= 10:
        raise ValueError(f"{path}.top_k must be between 1 and 10")
    integrity = _validate_sha256(key["integrity"], f"{path}.integrity")
    key_base = {field: value for field, value in key.items() if field != "integrity"}
    if integrity != _sha256_bytes(_canonical_json(key_base)):
        raise ValueError(f"{path}: key integrity mismatch")

    corpus = _object(key["corpus"], f"{path}.corpus")
    _require_exact_keys(
        corpus, {"sha256", "case_contract_sha256", "cases"}, f"{path}.corpus"
    )
    _validate_sha256(corpus["sha256"], f"{path}.corpus.sha256")
    _validate_sha256(
        corpus["case_contract_sha256"], f"{path}.corpus.case_contract_sha256"
    )
    case_count = _nonnegative_int(corpus["cases"], f"{path}.corpus.cases")
    if case_count == 0:
        raise ValueError(f"{path}.corpus.cases must be positive")

    raw_backends = _array(key["backends"], f"{path}.backends")
    if len(raw_backends) != 2:
        raise ValueError(f"{path}.backends must contain exactly two backends")
    backend_labels: list[str] = []
    for index, raw_backend in enumerate(raw_backends):
        context = f"{path}.backends[{index}]"
        backend = _object(raw_backend, context)
        _require_exact_keys(
            backend,
            {
                "converter_backend",
                "probe_backend",
                "backend_version",
                "measurement",
                "resource",
                "run_sha256",
            },
            context,
        )
        label = _string(backend["converter_backend"], f"{context}.converter_backend")
        backend_labels.append(label)
        _string(backend["probe_backend"], f"{context}.probe_backend")
        _string(backend["backend_version"], f"{context}.backend_version")
        _validate_sha256(backend["run_sha256"], f"{context}.run_sha256")
        measurement = _object(backend["measurement"], f"{context}.measurement")
        _require_exact_keys(
            measurement, {"warmups", "iterations"}, f"{context}.measurement"
        )
        _nonnegative_int(
            measurement["warmups"], f"{context}.measurement.warmups"
        )
        if (
            _nonnegative_int(
                measurement["iterations"],
                f"{context}.measurement.iterations",
            )
            == 0
        ):
            raise ValueError(
                f"{context}.measurement.iterations must be positive"
            )
        resource = _object(backend["resource"], f"{context}.resource")
        _require_exact_keys(
            resource,
            {"kind", "path", "fingerprint"},
            f"{context}.resource",
        )
        for field in ("kind", "path", "fingerprint"):
            _string(resource[field], f"{context}.resource.{field}")
        expected_resource_kind = {
            "hazkey": "hazkey_dictionary",
            "mozc": "mozc_runtime_inputs",
        }.get(label)
        if resource["kind"] != expected_resource_kind:
            raise ValueError(
                f"{context}.resource.kind does not match converter_backend"
            )
    if len(set(backend_labels)) != 2:
        raise ValueError(f"{path}.backends converter_backend values must be unique")
    if set(backend_labels) != {"hazkey", "mozc"}:
        raise ValueError(f"{path}.backends must contain hazkey and mozc")
    if backend_labels != sorted(backend_labels):
        raise ValueError(f"{path}.backends must be sorted by converter_backend")
    for field in ("backend_version", "measurement"):
        values = [backend[field] for backend in raw_backends]
        if any(value != values[0] for value in values[1:]):
            raise ValueError(f"{path}.backends must have an identical {field}")

    raw_cases = _array(key["cases"], f"{path}.cases")
    if len(raw_cases) != case_count:
        raise ValueError(f"{path}.cases length does not match corpus.cases")
    opaque_ids: set[str] = set()
    original_ids: set[str] = set()
    placement_counts = {
        "x": Counter({label: 0 for label in backend_labels}),
        "y": Counter({label: 0 for label in backend_labels}),
    }
    category_x_counts: dict[str, Counter[str]] = {}
    contract: list[dict[str, Any]] = []
    for index, raw_case in enumerate(raw_cases):
        context = f"{path}.cases[{index}]"
        case = _object(raw_case, context)
        _require_exact_keys(
            case,
            {
                "case",
                "original_id",
                "reading",
                "category",
                "expected",
                "x_backend",
                "y_backend",
                "review_record_sha256",
            },
            context,
        )
        opaque_id = _string(case["case"], f"{context}.case")
        original_id = _string(case["original_id"], f"{context}.original_id")
        if opaque_id in opaque_ids:
            raise ValueError(f"{path}: duplicate opaque case {opaque_id!r}")
        if original_id in original_ids:
            raise ValueError(f"{path}: duplicate original id {original_id!r}")
        opaque_ids.add(opaque_id)
        original_ids.add(original_id)
        reading = _string(case["reading"], f"{context}.reading")
        category = _string(case["category"], f"{context}.category")
        expected = _array(case["expected"], f"{context}.expected")
        if not expected:
            raise ValueError(f"{context}.expected must not be empty")
        for expected_index, value in enumerate(expected):
            _string(value, f"{context}.expected[{expected_index}]")
        x_backend = _string(case["x_backend"], f"{context}.x_backend")
        y_backend = _string(case["y_backend"], f"{context}.y_backend")
        if {x_backend, y_backend} != set(backend_labels):
            raise ValueError(f"{context}: x/y backends do not match key backends")
        placement_counts["x"][x_backend] += 1
        placement_counts["y"][y_backend] += 1
        category_x_counts.setdefault(
            category, Counter({label: 0 for label in backend_labels})
        )[x_backend] += 1
        _validate_sha256(
            case["review_record_sha256"], f"{context}.review_record_sha256"
        )
        contract.append(
            {
                "id": original_id,
                "reading": reading,
                "expected": expected,
                "category": category,
            }
        )
    if corpus["case_contract_sha256"] != _sha256_bytes(
        _canonical_json(sorted(contract, key=lambda case: case["id"]))
    ):
        raise ValueError(f"{path}: corpus case contract hash mismatch")

    placement = _object(key["placement"], f"{path}.placement")
    _require_exact_keys(placement, {"x", "y"}, f"{path}.placement")
    for side in ("x", "y"):
        raw_counts = _object(placement[side], f"{path}.placement.{side}")
        _require_exact_keys(raw_counts, backend_labels, f"{path}.placement.{side}")
        validated_counts = {
            label: _nonnegative_int(
                raw_counts[label], f"{path}.placement.{side}.{label}"
            )
            for label in backend_labels
        }
        if validated_counts != dict(placement_counts[side]):
            raise ValueError(f"{path}.placement.{side} does not match case mappings")
    x_counts = [placement_counts["x"][label] for label in backend_labels]
    if abs(x_counts[0] - x_counts[1]) > 1:
        raise ValueError(f"{path}: x/y placement is unbalanced")
    for category, counts in category_x_counts.items():
        category_counts = [counts[label] for label in backend_labels]
        if abs(category_counts[0] - category_counts[1]) > 1:
            raise ValueError(
                f"{path}: x/y placement for category {category!r} is unbalanced"
            )
    return key


def _load_review(
    path: Path, key: dict[str, Any], data: bytes | None = None
) -> dict[str, dict[str, Any]]:
    if data is None:
        data = path.read_bytes()
    if _sha256_bytes(data) != key["review_sha256"]:
        raise ValueError(f"{path}: review file hash does not match key")
    key_cases = {case["case"]: case for case in key["cases"]}
    review: dict[str, dict[str, Any]] = {}
    for line_number, record in _load_jsonl_bytes(data, str(path)):
        context = f"{path}:{line_number}"
        _require_exact_keys(
            record,
            {"schema", "case", "reading", "category", "x", "y", "integrity"},
            context,
        )
        if record["schema"] != REVIEW_SCHEMA:
            raise ValueError(f"{context}.schema must be {REVIEW_SCHEMA}")
        opaque_id = _string(record["case"], f"{context}.case")
        if opaque_id in review:
            raise ValueError(f"{context}: duplicate case {opaque_id!r}")
        _string(record["reading"], f"{context}.reading")
        _string(record["category"], f"{context}.category")
        for side in ("x", "y"):
            candidates = candidate_texts({"candidates": record[side]})
            if len(candidates) > key["top_k"]:
                raise ValueError(
                    f"{context}.{side} exceeds top_k {key['top_k']}"
                )
            for index, candidate in enumerate(candidates):
                _string(candidate, f"{context}.{side}[{index}]")
        integrity = _validate_sha256(record["integrity"], f"{context}.integrity")
        base = {field: value for field, value in record.items() if field != "integrity"}
        if integrity != _sha256_bytes(_canonical_json(base)):
            raise ValueError(f"{context}: record integrity mismatch")
        key_case = key_cases.get(opaque_id)
        if key_case is None:
            raise ValueError(f"{context}: unknown case {opaque_id!r}")
        if integrity != key_case["review_record_sha256"]:
            raise ValueError(f"{context}: record hash does not match key")
        for field in ("reading", "category"):
            if record[field] != key_case[field]:
                raise ValueError(f"{context}.{field} does not match key")
        review[opaque_id] = record
    actual = set(review)
    expected = set(key_cases)
    if actual != expected:
        raise ValueError(
            f"{path}: review case set does not match key; "
            f"missing={sorted(expected - actual)!r}, "
            f"unknown={sorted(actual - expected)!r}"
        )
    return review


def _load_judgments(
    path: Path, expected_cases: set[str]
) -> tuple[dict[str, str], str]:
    data = _read_owner_only_regular(path, "judgments")
    judgments: dict[str, str] = {}
    for line_number, record in _load_jsonl_bytes(data, str(path)):
        context = f"{path}:{line_number}"
        _require_exact_keys(record, {"schema", "case", "judgment"}, context)
        if record["schema"] != JUDGMENT_SCHEMA:
            raise ValueError(f"{context}.schema must be {JUDGMENT_SCHEMA}")
        opaque_id = _string(record["case"], f"{context}.case")
        judgment = _string(record["judgment"], f"{context}.judgment")
        if judgment not in ALLOWED_JUDGMENTS:
            raise ValueError(
                f"{context}.judgment must be one of {sorted(ALLOWED_JUDGMENTS)!r}"
            )
        if opaque_id in judgments:
            raise ValueError(f"{context}: duplicate judgment for {opaque_id!r}")
        judgments[opaque_id] = judgment
    actual = set(judgments)
    if actual != expected_cases:
        raise ValueError(
            f"{path}: judgment case set is incomplete; "
            f"missing={sorted(expected_cases - actual)!r}, "
            f"unknown={sorted(actual - expected_cases)!r}"
        )
    return judgments, _sha256_bytes(data)


def _empty_outcomes(labels: list[str]) -> dict[str, Counter[str]]:
    return {label: Counter() for label in labels}


def _record_outcome(
    outcomes: dict[str, Counter[str]],
    x_backend: str,
    y_backend: str,
    judgment: str,
) -> str | None:
    if judgment == "tie":
        outcomes[x_backend]["ties"] += 1
        outcomes[y_backend]["ties"] += 1
        return None
    if judgment == "both_bad":
        outcomes[x_backend]["both_bad"] += 1
        outcomes[y_backend]["both_bad"] += 1
        return None
    winner = x_backend if judgment == "x" else y_backend
    loser = y_backend if judgment == "x" else x_backend
    outcomes[winner]["wins"] += 1
    outcomes[loser]["losses"] += 1
    return winner


def _wilson_interval(wins: int, total: int) -> list[float] | None:
    if total == 0:
        return None
    z = 1.959963984540054
    observed = wins / total
    denominator = 1 + z * z / total
    center = (observed + z * z / (2 * total)) / denominator
    margin = (
        z
        * math.sqrt(
            observed * (1 - observed) / total
            + z * z / (4 * total * total)
        )
        / denominator
    )
    return [max(0.0, center - margin), min(1.0, center + margin)]


def _two_sided_sign_test(wins: int, losses: int) -> float | None:
    decisive = wins + losses
    if decisive == 0:
        return None
    tail = min(wins, losses)
    probability = 2 * sum(
        math.comb(decisive, count) for count in range(tail + 1)
    ) / (2**decisive)
    return min(1.0, probability)


def _render_outcomes(
    outcomes: dict[str, Counter[str]], total_cases: int
) -> dict[str, Any]:
    rendered: dict[str, Any] = {}
    for label, counts in sorted(outcomes.items()):
        wins = counts["wins"]
        losses = counts["losses"]
        decisive = wins + losses
        rendered[label] = {
            "wins": wins,
            "losses": losses,
            "ties": counts["ties"],
            "both_bad": counts["both_bad"],
            "all_cases": total_cases,
            "decisive_cases": decisive,
            "decisive_case_rate": decisive / total_cases,
            "decisive_win_rate": wins / decisive if decisive else None,
            "decisive_win_rate_ci95": _wilson_interval(wins, decisive),
            "preference_rate_all_cases": wins / total_cases,
            "net_preference_rate_all_cases": (wins - losses) / total_cases,
            "two_sided_sign_test_p_value": _two_sided_sign_test(wins, losses),
        }
    return rendered


def _objective_quality(
    key: dict[str, Any], review: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    labels = [backend["converter_backend"] for backend in key["backends"]]
    corpus: list[dict[str, str]] = []
    candidates_by_backend: dict[str, dict[str, list[str]]] = {
        label: {} for label in labels
    }
    for key_case in key["cases"]:
        original_id = key_case["original_id"]
        corpus.append(
            {
                "id": original_id,
                "reading": key_case["reading"],
                "expected": "|".join(key_case["expected"]),
                "category": key_case["category"],
            }
        )
        review_case = review[key_case["case"]]
        candidates_by_backend[key_case["x_backend"]][original_id] = review_case[
            "x"
        ]
        candidates_by_backend[key_case["y_backend"]][original_id] = review_case[
            "y"
        ]

    reports = {
        label: evaluate(corpus, candidates_by_backend[label], key["top_k"])
        for label in labels
    }
    a_name, b_name = labels
    comparison = compare_reports(
        reports[a_name],
        reports[b_name],
        a_name=a_name,
        b_name=b_name,
    )
    summaries = {
        label: {
            field: value
            for field, value in report.items()
            if field != "cases"
        }
        for label, report in reports.items()
    }
    return {
        "top_k": key["top_k"],
        "by_backend": summaries,
        "paired_comparison": comparison,
    }


def _load_packet(
    packet_directory: Path,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    metadata = packet_directory.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or packet_directory.is_symlink()
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise ValueError(
            f"{packet_directory}: packet must be an owner-only non-symlink directory"
        )
    expected_names = {REVIEW_NAME, KEY_NAME, MANIFEST_NAME}
    actual_names = {entry.name for entry in packet_directory.iterdir()}
    if actual_names != expected_names:
        raise ValueError(
            f"{packet_directory}: packet contents do not match schema; "
            f"missing={sorted(expected_names - actual_names)!r}, "
            f"unknown={sorted(actual_names - expected_names)!r}"
        )
    payloads: dict[str, bytes] = {}
    for name in sorted(expected_names):
        path = packet_directory / name
        file_metadata = path.lstat()
        if (
            not stat.S_ISREG(file_metadata.st_mode)
            or path.is_symlink()
            or file_metadata.st_uid != os.getuid()
            or stat.S_IMODE(file_metadata.st_mode) & 0o077
        ):
            raise ValueError(f"{path}: packet file must be owner-only and regular")
        payloads[name] = path.read_bytes()

    manifest_path = packet_directory / MANIFEST_NAME
    manifest = _load_json_bytes(payloads[MANIFEST_NAME], str(manifest_path))
    _require_exact_keys(
        manifest,
        {"schema", "cases", "source_ref", "top_k", "review", "key", "integrity"},
        str(manifest_path),
    )
    if manifest["schema"] != PACKET_SCHEMA:
        raise ValueError(f"{manifest_path}: unsupported packet schema")
    cases = _nonnegative_int(manifest["cases"], f"{manifest_path}.cases")
    if cases == 0:
        raise ValueError(f"{manifest_path}.cases must be positive")
    top_k = _nonnegative_int(manifest["top_k"], f"{manifest_path}.top_k")
    if not 1 <= top_k <= 10:
        raise ValueError(f"{manifest_path}.top_k must be between 1 and 10")
    _string(manifest["source_ref"], f"{manifest_path}.source_ref")
    review_contract = _object(manifest["review"], f"{manifest_path}.review")
    _require_exact_keys(
        review_contract, {"name", "sha256"}, f"{manifest_path}.review"
    )
    key_contract = _object(manifest["key"], f"{manifest_path}.key")
    _require_exact_keys(
        key_contract,
        {"name", "sha256", "integrity"},
        f"{manifest_path}.key",
    )
    if review_contract["name"] != REVIEW_NAME or key_contract["name"] != KEY_NAME:
        raise ValueError(f"{manifest_path}: packet filenames do not match schema")
    for contract, context in (
        (review_contract, f"{manifest_path}.review"),
        (key_contract, f"{manifest_path}.key"),
    ):
        _validate_sha256(contract["sha256"], f"{context}.sha256")
    _validate_sha256(
        key_contract["integrity"], f"{manifest_path}.key.integrity"
    )
    integrity = _validate_sha256(
        manifest["integrity"], f"{manifest_path}.integrity"
    )
    manifest_base = {
        field: value for field, value in manifest.items() if field != "integrity"
    }
    if integrity != _sha256_bytes(_canonical_json(manifest_base)):
        raise ValueError(f"{manifest_path}: packet integrity mismatch")
    if review_contract["sha256"] != _sha256_bytes(payloads[REVIEW_NAME]):
        raise ValueError(f"{manifest_path}: review hash mismatch")
    if key_contract["sha256"] != _sha256_bytes(payloads[KEY_NAME]):
        raise ValueError(f"{manifest_path}: key hash mismatch")

    key_path = packet_directory / KEY_NAME
    review_path = packet_directory / REVIEW_NAME
    key = _validate_key(key_path, payloads[KEY_NAME])
    if (
        key["integrity"] != key_contract["integrity"]
        or key["review_sha256"] != review_contract["sha256"]
        or key["source_ref"] != manifest["source_ref"]
        or key["top_k"] != top_k
        or key["corpus"]["cases"] != cases
    ):
        raise ValueError(f"{manifest_path}: manifest does not match unblind key")
    review = _load_review(review_path, key, payloads[REVIEW_NAME])
    return key, review


def score(packet_directory: Path, judgments_path: Path) -> dict[str, Any]:
    key, review = _load_packet(packet_directory)
    judgments, judgments_sha256 = _load_judgments(judgments_path, set(review))
    labels = [backend["converter_backend"] for backend in key["backends"]]
    outcomes = _empty_outcomes(labels)
    category_outcomes: dict[str, dict[str, Counter[str]]] = {}
    category_counts: Counter[str] = Counter()
    judgment_counts: Counter[str] = Counter()
    unblinded_cases: list[dict[str, Any]] = []
    for key_case in key["cases"]:
        opaque_id = key_case["case"]
        judgment = judgments[opaque_id]
        category = key_case["category"]
        judgment_counts[judgment] += 1
        category_counts[category] += 1
        per_category = category_outcomes.setdefault(
            category, _empty_outcomes(labels)
        )
        winner = _record_outcome(
            outcomes,
            key_case["x_backend"],
            key_case["y_backend"],
            judgment,
        )
        _record_outcome(
            per_category,
            key_case["x_backend"],
            key_case["y_backend"],
            judgment,
        )
        unblinded_cases.append(
            {
                "case": opaque_id,
                "original_id": key_case["original_id"],
                "category": category,
                "reading": key_case["reading"],
                "expected": key_case["expected"],
                "judgment": judgment,
                "x_backend": key_case["x_backend"],
                "y_backend": key_case["y_backend"],
                "winner": winner,
            }
        )
    report_base = {
        "schema": REPORT_SCHEMA,
        "source_ref": key["source_ref"],
        "corpus": key["corpus"],
        "backends": key["backends"],
        "review_sha256": key["review_sha256"],
        "judgments_sha256": judgments_sha256,
        "unblind_key_integrity": key["integrity"],
        "cases": len(key["cases"]),
        "top_k": key["top_k"],
        "placement": key["placement"],
        "human_preference": {
            "judgment_counts": {
                judgment: judgment_counts[judgment]
                for judgment in sorted(ALLOWED_JUDGMENTS)
            },
            "by_backend": _render_outcomes(outcomes, len(key["cases"])),
            "by_category": {
                category: {
                    "cases": category_counts[category],
                    "by_backend": _render_outcomes(
                        category_outcomes[category], category_counts[category]
                    ),
                }
                for category in sorted(category_outcomes)
            }
        },
        "objective_quality": _objective_quality(key, review),
        "unblinded_cases": unblinded_cases,
    }
    return report_base | {
        "integrity": _sha256_bytes(_canonical_json(report_base))
    }


def _write_private_file(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_packet(output_directory: Path, files: dict[str, bytes]) -> None:
    parent = output_directory.parent
    if not parent.is_dir():
        raise ValueError(f"packet parent directory does not exist: {parent}")
    if output_directory.exists() or output_directory.is_symlink():
        raise ValueError(f"refusing to overwrite packet {output_directory}")
    temporary = Path(
        tempfile.mkdtemp(
            prefix=f".{output_directory.name}.tmp-",
            dir=parent,
        )
    )
    os.chmod(temporary, 0o700)
    lock_path = parent / f".{output_directory.name}.lock"
    lock_descriptor = -1
    lock_created = False
    try:
        for name, data in files.items():
            _write_private_file(temporary / name, data)
        _fsync_directory(temporary)
        lock_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            lock_flags |= os.O_NOFOLLOW
        lock_descriptor = os.open(lock_path, lock_flags, 0o600)
        lock_created = True
        os.close(lock_descriptor)
        lock_descriptor = -1
        if output_directory.exists() or output_directory.is_symlink():
            raise ValueError(f"refusing to overwrite packet {output_directory}")
        os.rename(temporary, output_directory)
        temporary = Path()
        _fsync_directory(parent)
    finally:
        if lock_descriptor >= 0:
            os.close(lock_descriptor)
        if lock_created:
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
        if temporary != Path() and temporary.exists():
            shutil.rmtree(temporary)
        _fsync_directory(parent)


def _publish_private_file(path: Path, data: bytes) -> None:
    parent = path.parent
    if not parent.is_dir():
        raise ValueError(f"output parent directory does not exist: {parent}")
    if path.exists() or path.is_symlink():
        raise ValueError(f"refusing to overwrite output {path}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.tmp-", dir=parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path, follow_symlinks=False)
        _fsync_directory(parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _write_json_or_stdout(payload: dict[str, Any], output: Path | None) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if output is None:
        sys.stdout.write(encoded)
    else:
        _publish_private_file(output, encoded.encode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--corpus", type=Path, required=True)
    prepare_parser.add_argument("--run-a", type=Path, required=True)
    prepare_parser.add_argument("--run-b", type=Path, required=True)
    prepare_parser.add_argument("--seed-file", type=Path, required=True)
    prepare_parser.add_argument("--output-directory", type=Path, required=True)

    score_parser = subparsers.add_parser("score")
    score_parser.add_argument("--packet", type=Path, required=True)
    score_parser.add_argument("--judgments", type=Path, required=True)
    score_parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            seed = _load_seed_file(args.seed_file)
            summary = prepare(
                args.corpus,
                args.run_a,
                args.run_b,
                seed,
                args.output_directory,
            )
            _write_json_or_stdout(summary, None)
        else:
            _write_json_or_stdout(
                score(args.packet, args.judgments), args.output
            )
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
