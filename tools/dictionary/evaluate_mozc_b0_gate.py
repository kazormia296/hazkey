#!/usr/bin/env python3
"""Evaluate the frozen Mozc B0 adoption gate from immutable raw evidence.

The gate deliberately does not accept precomputed rates.  It re-hashes every
input named by an evidence manifest, re-runs the blind quality scorer and the
AB-probe summarizer, and then evaluates the frozen policy with integer cross
multiplication (or ``Decimal`` for measured latency values).
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from typing import Any, Iterable

if __package__:
    from . import (
        blind_conversion_ab,
        build_frozen_corpus,
        run_mozc_b0_measurement,
        run_mozc_b0_stability,
        summarize_ab_probe,
    )
    from .evaluate_conversion_quality import load_corpus_bytes
else:
    import blind_conversion_ab  # type: ignore[no-redef]
    import build_frozen_corpus  # type: ignore[no-redef]
    import run_mozc_b0_measurement  # type: ignore[no-redef]
    import run_mozc_b0_stability  # type: ignore[no-redef]
    import summarize_ab_probe  # type: ignore[no-redef]
    from evaluate_conversion_quality import load_corpus_bytes


POLICY_SCHEMA = "hazkey.mozc-adoption-b0-policy.v1"
EVIDENCE_SCHEMA = "hazkey.mozc-b0-gate-evidence.v1"
CORPUS_MANIFEST_SCHEMA = "hazkey.frozen-conversion-corpus-manifest.v1"
STABILITY_SCHEMA = run_mozc_b0_stability.RECORD_SCHEMA
OUTPUT_SCHEMA = "hazkey.mozc-b0-gate-result.v1"

EXPECTED_TOTAL_CASES = 256
EXPECTED_CATEGORIES = {
    "ajimee-unconditional": 100,
    "technical-mixed": 32,
    "proper-noun": 24,
    "colloquial": 24,
    "homophone-context": 20,
    "long-structural": 20,
    "grimodex-regression": 20,
    "protected": 16,
}
EXPECTED_CURATED_CATEGORIES = {
    key: value
    for key, value in EXPECTED_CATEGORIES.items()
    if key not in {"ajimee-unconditional", "protected"}
}
EXPECTED_COMPONENTS = (
    ("ajimee-unconditional", 100, "ajimee-jwtd-v2-"),
    ("product-curated", 140, "product-"),
    ("protected", 16, "protected-"),
)
EXPECTED_RUN_SEQUENCE = ("H1", "M1", "M2", "H2", "H3", "M3", "M4", "H4")
EXPECTED_RUN_IDS = {
    "hazkey": ("H1", "H2", "H3", "H4"),
    "mozc": ("M1", "M2", "M3", "M4"),
}
TSV_HEADER = b"id\treading\texpected\tcategory\n"
AJIMEE_PROVENANCE = {
    "kind": "external",
    "repository": "https://github.com/azooKey/AJIMEE-Bench",
    "revision": "401666cd56d1a570c2021798b64b6da4396bfd45",
    "raw_path": "JWTD_v2/v1/evaluation_items.json",
    "raw_sha256": "sha256:e9eb668fd6aa14b1e26436f429b5550108af0a1dfd443b8cea0bcb3ab3028fca",
    "license": "CC-BY-SA-3.0",
    "transform": "ajimee-unconditional-to-tsv.v1",
}


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _load_json_bytes(data: bytes, context: str) -> Any:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"{context}: invalid UTF-8") from error
    try:
        return json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except json.JSONDecodeError as error:
        raise ValueError(f"{context}: invalid JSON: {error.msg}") from error


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


def _boolean(value: Any, context: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{context} must be a boolean")
    return value


def _integer(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{context} must be an integer")
    return value


def _nonnegative_int(value: Any, context: str) -> int:
    result = _integer(value, context)
    if result < 0:
        raise ValueError(f"{context} must be non-negative")
    return result


def _positive_int(value: Any, context: str) -> int:
    result = _integer(value, context)
    if result < 1:
        raise ValueError(f"{context} must be positive")
    return result


def _exact(payload: dict[str, Any], expected: set[str], context: str) -> None:
    actual = set(payload)
    if actual != expected:
        raise ValueError(
            f"{context} fields do not match schema; "
            f"missing={sorted(expected - actual)!r}, "
            f"unknown={sorted(actual - expected)!r}"
        )


def _sha256(value: Any, context: str, *, allow_bare: bool = False) -> str:
    text = _string(value, context)
    if allow_bare and re.fullmatch(r"[0-9a-f]{64}", text):
        return "sha256:" + text
    if re.fullmatch(r"sha256:[0-9a-f]{64}", text) is None:
        raise ValueError(f"{context} must be a lowercase SHA-256 digest")
    return text


def _sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _path(value: Any, root: Path, context: str) -> Path:
    raw = _string(value, context)
    if "\0" in raw:
        raise ValueError(f"{context} contains NUL")
    path = Path(raw)
    return path if path.is_absolute() else root / path


def _self_contained_path(
    value: Any,
    root: Path,
    context: str,
) -> tuple[Path, bytes]:
    raw = _string(value, context)
    if "\0" in raw:
        raise ValueError(f"{context} contains NUL")
    path = Path(raw)
    if path.is_absolute() or ".." in path.parts or path.name in {"", ".", ".."}:
        raise ValueError(f"{context} must be a self-contained relative path")
    directory_flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    file_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptors: list[int] = []
    try:
        descriptor = os.open(root, directory_flags)
        descriptors.append(descriptor)
        for component in path.parts[:-1]:
            descriptor = os.open(
                component,
                directory_flags,
                dir_fd=descriptor,
            )
            descriptors.append(descriptor)
        final_name = path.parts[-1]
        file_descriptor = os.open(final_name, file_flags, dir_fd=descriptor)
        descriptors.append(file_descriptor)
        before = os.fstat(file_descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"{context} must be a non-symlink regular file")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(file_descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(file_descriptor)
        final = os.stat(final_name, dir_fd=descriptor, follow_symlinks=False)
    except OSError as error:
        raise ValueError(
            f"{context} must not contain a symlink ancestor or non-directory ancestor"
        ) from error
    finally:
        for open_descriptor in reversed(descriptors):
            os.close(open_descriptor)
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ) or (
        final.st_dev,
        final.st_ino,
        final.st_size,
        final.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ) or not stat.S_ISREG(final.st_mode):
        raise ValueError(f"{context} changed while it was read")
    data = b"".join(chunks)
    if len(data) != before.st_size:
        raise ValueError(f"{context} was not read completely")
    return root / path, data


def _read_regular(path: Path, context: str) -> bytes:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"{context} must be a non-symlink regular file")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or (before.st_dev, before.st_ino) != (metadata.st_dev, metadata.st_ino)
        ):
            raise ValueError(f"{context} changed before it was read")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        data = b"".join(chunks)
        after = os.fstat(descriptor)
        final = path.lstat()
    finally:
        os.close(descriptor)
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ) or (
        final.st_dev,
        final.st_ino,
        final.st_size,
        final.st_mtime_ns,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ) or not stat.S_ISREG(final.st_mode) or len(data) != before.st_size:
        raise ValueError(f"{context} changed while it was read")
    return data


def _verified_bytes(path: Path, expected: str, context: str) -> bytes:
    data = _read_regular(path, context)
    return _verified_data(data, expected, context)


def _verified_data(data: bytes, expected: str, context: str) -> bytes:
    actual = _sha256_bytes(data)
    if actual != expected:
        raise ValueError(
            f"{context} hash mismatch: expected {expected}, got {actual}"
        )
    return data


@dataclass(frozen=True)
class GatePolicy:
    total_cases: int
    categories: dict[str, int]
    minimum_net_basis_points: int
    minimum_net_cases: int
    minimum_top1_delta_basis_points: int
    minimum_top10_delta_basis_points: int
    minimum_category_top1_delta_basis_points: int
    protected_required: int
    maximum_both_bad: int
    maximum_warm_p95_ratio_basis_points: int
    maximum_pss_ratio_basis_points: int
    required_stability_ids: tuple[str, ...]


@dataclass(frozen=True)
class MeasurementContract:
    producer_sha256: str
    runs_per_backend: int
    execution_order: tuple[str, ...]
    warmups_per_case: int
    iterations_per_case: int
    top_k: int
    cases: int
    latency_statistic: str
    pss_statistic: str
    cpu_policy: str
    per_run_timeout_seconds: int


@dataclass(frozen=True)
class StabilityCheck:
    check_id: str
    native_schema: str
    artifact_kind: str
    command: tuple[str, ...]
    minimum_conversions: int
    minimum_cycles: int
    helper_launches: int | None
    server_launches: int | None
    helper_recoveries: int | None
    server_recoveries: int | None
    residue_count: int | None
    native_producer_path: str
    native_producer_sha256: str | None
    execution_runner_path: str | None
    execution_runner_sha256: str | None
    execution_package_path: str | None
    execution_package_file_count: int | None
    execution_package_size_bytes: int | None
    execution_package_fingerprint: str | None
    recovery_fixture_identity: str | None
    input_snapshot_fingerprint: str | None


@dataclass(frozen=True)
class ParsedPolicy:
    gate: GatePolicy
    policy_id: str
    candidate_id: str
    product_source_revision: str
    artifact_source_revision: str
    candidate_resource_fingerprint: str
    baseline_resource_fingerprint: str
    artifacts: dict[str, tuple[int, str]]
    product_executable: tuple[int, str]
    runtime_dependencies: dict[str, tuple[int, str]]
    runtime_dependency_integrity: str
    ajimee_derived_sha256: str
    manifest_path: str
    manifest_sha256: str
    policy_sha256: str
    measurement: MeasurementContract
    stability_orchestrator_sha256: str
    stability_checks: dict[str, StabilityCheck]


def _expect(value: Any, expected: Any, context: str) -> None:
    if value != expected:
        raise ValueError(f"{context} must be {expected!r}, got {value!r}")


def parse_policy(data: bytes, context: str = "policy") -> ParsedPolicy:
    root = _object(_load_json_bytes(data, context), context)
    _exact(
        root,
        {
            "schema",
            "policy_id",
            "candidate",
            "baseline",
            "candidate_sequence",
            "formal_suite",
            "external_sources",
            "gates",
            "measurement_contracts",
            "manifest_binding",
            "readiness",
        },
        context,
    )
    _expect(root["schema"], POLICY_SCHEMA, f"{context}.schema")
    policy_id = _string(root["policy_id"], f"{context}.policy_id")

    candidate = _object(root["candidate"], f"{context}.candidate")
    _exact(
        candidate,
        {
            "id",
            "product_source_revision",
            "artifact_source_revision",
            "resource_fingerprint",
            "product_executable",
            "runtime_dependencies",
            "artifacts",
        },
        f"{context}.candidate",
    )
    _expect(candidate["id"], "B0", f"{context}.candidate.id")
    product_source_revision = _string(
        candidate["product_source_revision"],
        f"{context}.candidate.product_source_revision",
    )
    if re.fullmatch(r"[0-9a-f]{40}", product_source_revision) is None:
        raise ValueError(
            f"{context}.candidate.product_source_revision must be a 40-hex commit"
        )
    _expect(
        product_source_revision,
        run_mozc_b0_stability.PRODUCT_SOURCE_REF,
        f"{context}.candidate.product_source_revision",
    )
    artifact_source_revision = _string(
        candidate["artifact_source_revision"],
        f"{context}.candidate.artifact_source_revision",
    )
    if re.fullmatch(r"[0-9a-f]{40}", artifact_source_revision) is None:
        raise ValueError(
            f"{context}.candidate.artifact_source_revision must be a 40-hex commit"
        )
    candidate_fingerprint = _sha256(
        candidate["resource_fingerprint"],
        f"{context}.candidate.resource_fingerprint",
    )
    _expect(
        candidate_fingerprint,
        run_mozc_b0_stability.B0_RESOURCE_FINGERPRINT,
        f"{context}.candidate.resource_fingerprint",
    )
    executable_context = f"{context}.candidate.product_executable"
    executable = _object(candidate["product_executable"], executable_context)
    _exact(executable, {"size_bytes", "sha256"}, executable_context)
    product_executable = (
        _positive_int(executable["size_bytes"], f"{executable_context}.size_bytes"),
        _sha256(
            executable["sha256"], f"{executable_context}.sha256", allow_bare=True
        ),
    )
    runtime_context = f"{context}.candidate.runtime_dependencies"
    runtime = _object(candidate["runtime_dependencies"], runtime_context)
    _exact(runtime, {"schema", "files", "integrity"}, runtime_context)
    _expect(
        runtime["schema"],
        run_mozc_b0_measurement.RUNTIME_DEPENDENCY_SCHEMA,
        f"{runtime_context}.schema",
    )
    runtime_files = _array(runtime["files"], f"{runtime_context}.files")
    _expect(
        len(runtime_files),
        len(run_mozc_b0_measurement.RUNTIME_DEPENDENCY_FILENAMES),
        f"{runtime_context}.files length",
    )
    runtime_dependencies: dict[str, tuple[int, str]] = {}
    normalized_runtime_files: list[dict[str, Any]] = []
    for index, (raw_file, expected_name) in enumerate(
        zip(
            runtime_files,
            run_mozc_b0_measurement.RUNTIME_DEPENDENCY_FILENAMES,
            strict=True,
        )
    ):
        file_context = f"{runtime_context}.files[{index}]"
        runtime_file = _object(raw_file, file_context)
        _exact(runtime_file, {"path", "size_bytes", "sha256"}, file_context)
        _expect(runtime_file["path"], expected_name, f"{file_context}.path")
        size = _positive_int(runtime_file["size_bytes"], f"{file_context}.size_bytes")
        digest = _sha256(runtime_file["sha256"], f"{file_context}.sha256")
        runtime_dependencies[expected_name] = (size, digest)
        normalized_runtime_files.append(
            {"path": expected_name, "size_bytes": size, "sha256": digest}
        )
    runtime_integrity = _sha256(runtime["integrity"], f"{runtime_context}.integrity")
    expected_runtime_integrity = _sha256_bytes(
        run_mozc_b0_measurement.canonical_json(
            {
                "schema": run_mozc_b0_measurement.RUNTIME_DEPENDENCY_SCHEMA,
                "files": normalized_runtime_files,
            }
        )
    )
    _expect(runtime_integrity, expected_runtime_integrity, f"{runtime_context}.integrity")
    artifacts: dict[str, tuple[int, str]] = {}
    for index, raw in enumerate(
        _array(candidate["artifacts"], f"{context}.candidate.artifacts")
    ):
        item_context = f"{context}.candidate.artifacts[{index}]"
        artifact = _object(raw, item_context)
        _exact(artifact, {"id", "size_bytes", "sha256"}, item_context)
        artifact_id = _string(artifact["id"], f"{item_context}.id")
        if artifact_id in artifacts:
            raise ValueError(f"{context}.candidate.artifacts has duplicate id {artifact_id!r}")
        artifacts[artifact_id] = (
            _positive_int(artifact["size_bytes"], f"{item_context}.size_bytes"),
            _sha256(artifact["sha256"], f"{item_context}.sha256", allow_bare=True),
        )
    if not artifacts:
        raise ValueError(f"{context}.candidate.artifacts must not be empty")

    baseline = _object(root["baseline"], f"{context}.baseline")
    _exact(baseline, {"id", "resource_fingerprint"}, f"{context}.baseline")
    _expect(baseline["id"], "hazkey", f"{context}.baseline.id")
    baseline_fingerprint = _sha256(
        baseline["resource_fingerprint"], f"{context}.baseline.resource_fingerprint"
    )

    sequence = _object(root["candidate_sequence"], f"{context}.candidate_sequence")
    _exact(
        sequence,
        {"evaluate_first", "build_B1_only_if_B0_fails", "B2_status"},
        f"{context}.candidate_sequence",
    )
    _expect(sequence["evaluate_first"], "B0", f"{context}.candidate_sequence.evaluate_first")
    _expect(
        _boolean(
            sequence["build_B1_only_if_B0_fails"],
            f"{context}.candidate_sequence.build_B1_only_if_B0_fails",
        ),
        True,
        f"{context}.candidate_sequence.build_B1_only_if_B0_fails",
    )
    _expect(sequence["B2_status"], "future_consideration", f"{context}.candidate_sequence.B2_status")

    suite = _object(root["formal_suite"], f"{context}.formal_suite")
    _exact(
        suite,
        {"total_cases", "components", "curated_categories", "categories"},
        f"{context}.formal_suite",
    )
    _expect(
        _positive_int(suite["total_cases"], f"{context}.formal_suite.total_cases"),
        EXPECTED_TOTAL_CASES,
        f"{context}.formal_suite.total_cases",
    )
    components = _object(suite["components"], f"{context}.formal_suite.components")
    expected_component_policy = {
        "ajimee_unconditional": ("external-ajimee-unconditional.tsv", 100),
        "product_curated": ("product-curated.tsv", 140),
        "protected": ("protected.tsv", 16),
    }
    _exact(components, set(expected_component_policy), f"{context}.formal_suite.components")
    for name, (filename, cases) in expected_component_policy.items():
        component_context = f"{context}.formal_suite.components.{name}"
        component = _object(components[name], component_context)
        component_fields = {"file", "cases"}
        if name == "ajimee_unconditional":
            component_fields.add("sha256")
        _exact(component, component_fields, component_context)
        _expect(component["file"], filename, f"{component_context}.file")
        _expect(component["cases"], cases, f"{component_context}.cases")
        if name == "ajimee_unconditional":
            _sha256(component["sha256"], f"{component_context}.sha256")
    _expect(
        suite["curated_categories"],
        EXPECTED_CURATED_CATEGORIES,
        f"{context}.formal_suite.curated_categories",
    )
    _expect(suite["categories"], EXPECTED_CATEGORIES, f"{context}.formal_suite.categories")

    external = _object(root["external_sources"], f"{context}.external_sources")
    _exact(external, {"ajimee_bench"}, f"{context}.external_sources")
    ajimee = _object(external["ajimee_bench"], f"{context}.external_sources.ajimee_bench")
    _exact(
        ajimee,
        {
            "repository",
            "revision",
            "raw_path",
            "raw_sha256",
            "raw_cases",
            "unconditional_cases",
            "contextual_cases",
            "license",
            "derived_path",
            "derived_sha256",
            "transform",
            "reading_normalization",
        },
        f"{context}.external_sources.ajimee_bench",
    )
    for field, expected in (
        ("repository", AJIMEE_PROVENANCE["repository"]),
        ("raw_path", AJIMEE_PROVENANCE["raw_path"]),
        ("license", AJIMEE_PROVENANCE["license"]),
        ("derived_path", "external-ajimee-unconditional.tsv"),
        ("transform", AJIMEE_PROVENANCE["transform"]),
        ("reading_normalization", "katakana-to-hiragana.v1"),
    ):
        _expect(
            ajimee[field],
            expected,
            f"{context}.external_sources.ajimee_bench.{field}",
        )
    revision = _string(ajimee["revision"], f"{context}.external_sources.ajimee_bench.revision")
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise ValueError(f"{context}.external_sources.ajimee_bench.revision must be 40-hex")
    _expect(revision, AJIMEE_PROVENANCE["revision"], f"{context}.external_sources.ajimee_bench.revision")
    _expect(
        _sha256(
            ajimee["raw_sha256"],
            f"{context}.external_sources.ajimee_bench.raw_sha256",
            allow_bare=True,
        ),
        AJIMEE_PROVENANCE["raw_sha256"],
        f"{context}.external_sources.ajimee_bench.raw_sha256",
    )
    derived_sha = _sha256(
        ajimee["derived_sha256"],
        f"{context}.external_sources.ajimee_bench.derived_sha256",
    )
    _expect(
        derived_sha,
        _sha256(
            components["ajimee_unconditional"]["sha256"],
            f"{context}.formal_suite.components.ajimee_unconditional.sha256",
        ),
        f"{context}.external_sources.ajimee_bench.derived_sha256",
    )
    for field, expected in (("raw_cases", 200), ("unconditional_cases", 100), ("contextual_cases", 100)):
        _expect(ajimee[field], expected, f"{context}.external_sources.ajimee_bench.{field}")

    gates = _object(root["gates"], f"{context}.gates")
    expected_gate_names = {
        "human_net_preference",
        "top1",
        "top10",
        "per_category_top1",
        "protected",
        "both_bad",
        "warm_latency_p95",
        "pss",
        "long_running_stability",
    }
    _exact(gates, expected_gate_names, f"{context}.gates")

    human = _object(gates["human_net_preference"], f"{context}.gates.human_net_preference")
    _exact(human, {"comparison_backend", "minimum_basis_points", "minimum_net_cases"}, f"{context}.gates.human_net_preference")
    _expect(human["comparison_backend"], "hazkey", f"{context}.gates.human_net_preference.comparison_backend")
    _expect(human["minimum_basis_points"], -300, f"{context}.gates.human_net_preference.minimum_basis_points")
    _expect(human["minimum_net_cases"], -7, f"{context}.gates.human_net_preference.minimum_net_cases")

    def delta_gate(name: str, expected_bp: int, *, scope: bool = False) -> int:
        gate_context = f"{context}.gates.{name}"
        payload = _object(gates[name], gate_context)
        fields = {"comparison_backend", "minimum_delta_basis_points"}
        if scope:
            fields.add("scope")
        _exact(payload, fields, gate_context)
        _expect(payload["comparison_backend"], "hazkey", f"{gate_context}.comparison_backend")
        if scope:
            _expect(payload["scope"], "formal_suite.categories", f"{gate_context}.scope")
        _expect(payload["minimum_delta_basis_points"], expected_bp, f"{gate_context}.minimum_delta_basis_points")
        return expected_bp

    delta_gate("top1", -800)
    delta_gate("top10", -1200)
    delta_gate("per_category_top1", -1000, scope=True)

    protected = _object(gates["protected"], f"{context}.gates.protected")
    _exact(protected, {"required_passes", "total_cases"}, f"{context}.gates.protected")
    _expect(protected["required_passes"], 16, f"{context}.gates.protected.required_passes")
    _expect(protected["total_cases"], 16, f"{context}.gates.protected.total_cases")
    both_bad = _object(gates["both_bad"], f"{context}.gates.both_bad")
    _exact(both_bad, {"maximum_cases"}, f"{context}.gates.both_bad")
    _expect(both_bad["maximum_cases"], 12, f"{context}.gates.both_bad.maximum_cases")

    def ratio_gate(name: str, expected_bp: int) -> None:
        gate_context = f"{context}.gates.{name}"
        payload = _object(gates[name], gate_context)
        _exact(payload, {"comparison_backend", "maximum_ratio_basis_points"}, gate_context)
        _expect(payload["comparison_backend"], "hazkey", f"{gate_context}.comparison_backend")
        _expect(payload["maximum_ratio_basis_points"], expected_bp, f"{gate_context}.maximum_ratio_basis_points")

    ratio_gate("warm_latency_p95", 5000)
    ratio_gate("pss", 12500)

    stability = _object(
        gates["long_running_stability"],
        f"{context}.gates.long_running_stability",
    )
    _exact(
        stability,
        {"required_result", "check_contracts_frozen", "checks"},
        f"{context}.gates.long_running_stability",
    )
    _expect(
        stability["required_result"],
        "all_pass",
        f"{context}.gates.long_running_stability.required_result",
    )
    frozen = _boolean(
        stability["check_contracts_frozen"],
        f"{context}.gates.long_running_stability.check_contracts_frozen",
    )
    stability_checks: dict[str, StabilityCheck] = {}
    if frozen:
        raw_checks = _array(
            stability["checks"],
            f"{context}.gates.long_running_stability.checks",
        )
        if not raw_checks:
            raise ValueError(
                f"{context}.gates.long_running_stability.checks must be non-empty"
            )
        for index, raw_check in enumerate(raw_checks):
            check_context = (
                f"{context}.gates.long_running_stability.checks[{index}]"
            )
            check = _object(raw_check, check_context)
            _exact(
                check,
                {
                    "id",
                    "native_schema",
                    "artifact_kind",
                    "command",
                    "minimum_conversions",
                    "minimum_cycles",
                    "expected_counts",
                    "native_producer",
                    "execution_runner",
                    "execution_package",
                    "recovery_fixture_identity",
                    "input_snapshot_fingerprint",
                },
                check_context,
            )
            check_id = _string(check["id"], f"{check_context}.id")
            if check_id in stability_checks:
                raise ValueError(
                    f"{context}.gates.long_running_stability.checks has "
                    f"duplicate id {check_id!r}"
                )
            command = tuple(
                _string(value, f"{check_context}.command[{item_index}]")
                for item_index, value in enumerate(
                    _array(check["command"], f"{check_context}.command")
                )
            )
            if not command:
                raise ValueError(f"{check_context}.command must not be empty")
            counts_context = f"{check_context}.expected_counts"
            counts = _object(check["expected_counts"], counts_context)
            count_fields = {
                "helper_launches",
                "server_launches",
                "helper_recoveries",
                "server_recoveries",
                "residue_count",
            }
            _exact(counts, count_fields, counts_context)
            minimum_conversions = _nonnegative_int(
                check["minimum_conversions"],
                f"{check_context}.minimum_conversions",
            )
            minimum_cycles = _nonnegative_int(
                check["minimum_cycles"], f"{check_context}.minimum_cycles"
            )
            parsed_counts: dict[str, int | None] = {}
            for field in count_fields:
                value = counts[field]
                parsed_counts[field] = (
                    None
                    if value is None
                    else _nonnegative_int(value, f"{counts_context}.{field}")
                )
            if not any(
                value
                for value in (
                    minimum_conversions,
                    minimum_cycles,
                    *(value for value in parsed_counts.values() if value is not None),
                )
            ):
                raise ValueError(
                    f"{check_context} must contain at least one non-zero requirement"
                )
            producer_context = f"{check_context}.native_producer"
            producer = _object(check["native_producer"], producer_context)
            _exact(
                producer,
                {"path", "status", "sha256"},
                producer_context,
            )
            producer_path = _string(producer["path"], f"{producer_context}.path")
            if producer_path.startswith("/") or ".." in Path(producer_path).parts:
                raise ValueError(f"{producer_context}.path must be repo-relative")
            producer_status = _string(
                producer["status"], f"{producer_context}.status"
            )
            if producer_status not in {"pending", "ready"}:
                raise ValueError(f"{producer_context}.status must be pending or ready")
            if producer_status == "ready":
                producer_sha = _sha256(
                    producer["sha256"], f"{producer_context}.sha256"
                )
                if producer_path == "<product-executable>":
                    expected_producer_sha = product_executable[1]
                else:
                    repository_root = Path(__file__).resolve().parents[2]
                    expected_producer_sha = _sha256_bytes(
                        _read_regular(
                            repository_root / producer_path,
                            f"{producer_context}.path",
                        )
                    )
                _expect(
                    producer_sha,
                    expected_producer_sha,
                    f"{producer_context}.sha256",
                )
            else:
                if producer["sha256"] is not None:
                    raise ValueError(
                        f"{producer_context}.sha256 must be null while pending"
                    )
                producer_sha = None
            runner_value = check["execution_runner"]
            if runner_value is None:
                runner_path = None
                runner_sha = None
            else:
                runner_context = f"{check_context}.execution_runner"
                runner = _object(runner_value, runner_context)
                _exact(runner, {"path", "sha256"}, runner_context)
                runner_path = _string(runner["path"], f"{runner_context}.path")
                if runner_path.startswith("/") or ".." in Path(runner_path).parts:
                    raise ValueError(f"{runner_context}.path must be repo-relative")
                runner_sha = _sha256(runner["sha256"], f"{runner_context}.sha256")
                repository_root = Path(__file__).resolve().parents[2]
                _expect(
                    runner_sha,
                    _sha256_bytes(
                        _read_regular(
                            repository_root / runner_path,
                            f"{runner_context}.path",
                        )
                    ),
                    f"{runner_context}.sha256",
                )
            package_value = check["execution_package"]
            if package_value is None:
                package_path = None
                package_file_count = None
                package_size = None
                package_fingerprint = None
            else:
                package_context = f"{check_context}.execution_package"
                package = _object(package_value, package_context)
                _exact(
                    package,
                    {"path", "file_count", "size_bytes", "fingerprint"},
                    package_context,
                )
                package_path = _string(
                    package["path"], f"{package_context}.path"
                )
                _expect(
                    package_path,
                    run_mozc_b0_stability.SWIFT_PACKAGE_ROOT,
                    f"{package_context}.path",
                )
                package_file_count = _nonnegative_int(
                    package["file_count"], f"{package_context}.file_count"
                )
                package_size = _nonnegative_int(
                    package["size_bytes"], f"{package_context}.size_bytes"
                )
                if package_file_count < 1 or package_size < 1:
                    raise ValueError(
                        f"{package_context} file_count and size_bytes must be positive"
                    )
                package_fingerprint = _sha256(
                    package["fingerprint"], f"{package_context}.fingerprint"
                )
                actual_package_identity = (
                    run_mozc_b0_stability._swift_package_identity_from_files(
                        run_mozc_b0_stability._read_swift_package_inputs(
                            Path(__file__).resolve().parents[2]
                        )
                    )
                )
                _expect(
                    (package_file_count, package_size, package_fingerprint),
                    actual_package_identity,
                    package_context,
                )
            fixture_identity = check["recovery_fixture_identity"]
            if fixture_identity is not None:
                fixture_identity = _sha256(
                    fixture_identity,
                    f"{check_context}.recovery_fixture_identity",
                )
            snapshot_fingerprint_value = check["input_snapshot_fingerprint"]
            snapshot_fingerprint = (
                None
                if snapshot_fingerprint_value is None
                else _sha256(
                    snapshot_fingerprint_value,
                    f"{check_context}.input_snapshot_fingerprint",
                )
            )
            stability_checks[check_id] = StabilityCheck(
                check_id=check_id,
                native_schema=_string(
                    check["native_schema"], f"{check_context}.native_schema"
                ),
                artifact_kind=_string(
                    check["artifact_kind"], f"{check_context}.artifact_kind"
                ),
                command=command,
                minimum_conversions=minimum_conversions,
                minimum_cycles=minimum_cycles,
                helper_launches=parsed_counts["helper_launches"],
                server_launches=parsed_counts["server_launches"],
                helper_recoveries=parsed_counts["helper_recoveries"],
                server_recoveries=parsed_counts["server_recoveries"],
                residue_count=parsed_counts["residue_count"],
                native_producer_path=producer_path,
                native_producer_sha256=producer_sha,
                execution_runner_path=runner_path,
                execution_runner_sha256=runner_sha,
                execution_package_path=package_path,
                execution_package_file_count=package_file_count,
                execution_package_size_bytes=package_size,
                execution_package_fingerprint=package_fingerprint,
                recovery_fixture_identity=fixture_identity,
                input_snapshot_fingerprint=snapshot_fingerprint,
            )
    elif stability["checks"] is not None:
        raise ValueError(
            f"{context}.gates.long_running_stability.checks must be null "
            "until frozen"
        )
    required_ids = tuple(stability_checks)
    if frozen:
        _expect(
            required_ids,
            run_mozc_b0_stability.SUITE_IDS,
            f"{context}.gates.long_running_stability check IDs",
        )
        for check_id, check in stability_checks.items():
            requirements = run_mozc_b0_stability.SUITE_REQUIREMENTS[check_id]
            _expect(
                check.native_schema,
                run_mozc_b0_stability.native_schema(check_id),
                f"{context}.stability.{check_id}.native_schema",
            )
            _expect(
                check.command,
                run_mozc_b0_stability.CANONICAL_COMMANDS[check_id],
                f"{context}.stability.{check_id}.command",
            )
            expected_kind = (
                "b0"
                if check_id in run_mozc_b0_stability.B0_SUITE_IDS
                else "fault-fixture"
            )
            _expect(
                check.artifact_kind,
                expected_kind,
                f"{context}.stability.{check_id}.artifact_kind",
            )
            _expect(
                check.minimum_conversions,
                requirements["minimum_conversions"],
                f"{context}.stability.{check_id}.minimum_conversions",
            )
            _expect(
                check.minimum_cycles,
                requirements["minimum_cycles"],
                f"{context}.stability.{check_id}.minimum_cycles",
            )
            for field, expected_count in requirements["expected_counts"].items():
                _expect(
                    getattr(check, field),
                    expected_count,
                    f"{context}.stability.{check_id}.expected_counts.{field}",
                )
            _expect(
                check.native_producer_path,
                requirements["native_producer_path"],
                f"{context}.stability.{check_id}.native_producer.path",
            )
            _expect(
                check.execution_runner_path,
                requirements["execution_runner_path"],
                f"{context}.stability.{check_id}.execution_runner.path",
            )
            _expect(
                check.execution_package_path,
                requirements["execution_package_path"],
                f"{context}.stability.{check_id}.execution_package.path",
            )
            if check_id == run_mozc_b0_stability.PROTOCOL_RECOVERY_ID:
                if check.recovery_fixture_identity is None:
                    raise ValueError(
                        f"{context}.stability.{check_id} requires fixture identity"
                    )
            elif check.recovery_fixture_identity is not None:
                raise ValueError(
                    f"{context}.stability.{check_id} must not claim a fixture identity"
                )
            if check_id in {
                run_mozc_b0_stability.FCITX_LONG_SOAK_ID,
                run_mozc_b0_stability.FCITX_LIFECYCLE_ID,
            }:
                if check.input_snapshot_fingerprint is None:
                    raise ValueError(
                        f"{context}.stability.{check_id} requires a frozen "
                        "input snapshot fingerprint"
                    )
            elif check.input_snapshot_fingerprint is not None:
                raise ValueError(
                    f"{context}.stability.{check_id} must not claim an input "
                    "snapshot fingerprint"
                )
        fcitx_snapshot_fingerprints = {
            check.input_snapshot_fingerprint
            for check_id, check in stability_checks.items()
            if check_id
            in {
                run_mozc_b0_stability.FCITX_LONG_SOAK_ID,
                run_mozc_b0_stability.FCITX_LIFECYCLE_ID,
            }
        }
        if len(fcitx_snapshot_fingerprints) != 1:
            raise ValueError(
                f"{context}.gates.long_running_stability Fcitx suites must "
                "freeze the same input snapshot fingerprint"
            )

    contracts = _object(
        root["measurement_contracts"], f"{context}.measurement_contracts"
    )
    _exact(
        contracts,
        {"formal_abprobe_v3", "long_running_stability"},
        f"{context}.measurement_contracts",
    )
    measurement_context = f"{context}.measurement_contracts.formal_abprobe_v3"
    raw_measurement = _object(contracts["formal_abprobe_v3"], measurement_context)
    _exact(
        raw_measurement,
        {
            "status",
            "schema",
            "producer_sha256",
            "runs_per_backend",
            "execution_order",
            "warmups_per_case",
            "iterations_per_case",
            "top_k",
            "cases",
            "latency_statistic",
            "pss_statistic",
            "cpu_policy",
            "per_run_timeout_seconds",
        },
        measurement_context,
    )
    measurement_status = _string(
        raw_measurement["status"], f"{measurement_context}.status"
    )
    if measurement_status not in {"pending", "ready"}:
        raise ValueError(f"{measurement_context}.status must be pending or ready")
    expected_measurement = {
        "schema": summarize_ab_probe.INPUT_SCHEMA_V3,
        "producer_sha256": _sha256_bytes(
            _read_regular(
                Path(run_mozc_b0_measurement.__file__).resolve(),
                "measurement producer",
            )
        ),
        "runs_per_backend": 4,
        "execution_order": list(EXPECTED_RUN_SEQUENCE),
        "warmups_per_case": 5,
        "iterations_per_case": 20,
        "top_k": 10,
        "cases": EXPECTED_TOTAL_CASES,
        "latency_statistic": "nearest-rank-p95-across-all-samples",
        "pss_statistic": "max-parent-plus-backend-before-after",
        "cpu_policy": "unrestricted-same-host",
        "per_run_timeout_seconds": run_mozc_b0_measurement.PER_RUN_TIMEOUT_SECONDS,
    }
    for field, expected in expected_measurement.items():
        _expect(raw_measurement[field], expected, f"{measurement_context}.{field}")
    measurement = MeasurementContract(
        producer_sha256=expected_measurement["producer_sha256"],
        runs_per_backend=4,
        execution_order=EXPECTED_RUN_SEQUENCE,
        warmups_per_case=5,
        iterations_per_case=20,
        top_k=10,
        cases=EXPECTED_TOTAL_CASES,
        latency_statistic="nearest-rank-p95-across-all-samples",
        pss_statistic="max-parent-plus-backend-before-after",
        cpu_policy="unrestricted-same-host",
        per_run_timeout_seconds=run_mozc_b0_measurement.PER_RUN_TIMEOUT_SECONDS,
    )
    stability_contract_context = (
        f"{context}.measurement_contracts.long_running_stability"
    )
    stability_contract = _object(
        contracts["long_running_stability"], stability_contract_context
    )
    _exact(
        stability_contract,
        {"status", "orchestrator"},
        stability_contract_context,
    )
    stability_status = _string(
        stability_contract["status"], f"{stability_contract_context}.status"
    )
    if stability_status not in {"pending", "ready"}:
        raise ValueError(
            f"{stability_contract_context}.status must be pending or ready"
        )
    orchestrator_context = f"{stability_contract_context}.orchestrator"
    orchestrator = _object(
        stability_contract["orchestrator"], orchestrator_context
    )
    _exact(
        orchestrator,
        {"schema", "path", "sha256"},
        orchestrator_context,
    )
    _expect(
        orchestrator["schema"],
        run_mozc_b0_stability.RECORD_SCHEMA,
        f"{orchestrator_context}.schema",
    )
    _expect(
        orchestrator["path"],
        run_mozc_b0_stability.ORCHESTRATOR_PATH,
        f"{orchestrator_context}.path",
    )
    orchestrator_sha = _sha256(
        orchestrator["sha256"], f"{orchestrator_context}.sha256"
    )
    _expect(
        orchestrator_sha,
        _sha256_bytes(
            _read_regular(
                Path(run_mozc_b0_stability.__file__).resolve(),
                "stability orchestrator",
            )
        ),
        f"{orchestrator_context}.sha256",
    )
    producer_contracts_ready = all(
        check.native_producer_sha256 is not None
        for check in stability_checks.values()
    )
    contracts_ready = (
        measurement_status == "ready"
        and stability_status == "ready"
        and producer_contracts_ready
    )

    binding = _object(root["manifest_binding"], f"{context}.manifest_binding")
    _exact(binding, {"required_for_formal_decision", "expected_schema", "status", "path", "sha256"}, f"{context}.manifest_binding")
    _expect(binding["required_for_formal_decision"], True, f"{context}.manifest_binding.required_for_formal_decision")
    _expect(binding["expected_schema"], CORPUS_MANIFEST_SCHEMA, f"{context}.manifest_binding.expected_schema")
    binding_status = _string(binding["status"], f"{context}.manifest_binding.status")
    if binding_status == "ready":
        manifest_path = _string(binding["path"], f"{context}.manifest_binding.path")
        manifest_sha256 = _sha256(binding["sha256"], f"{context}.manifest_binding.sha256", allow_bare=True)
    else:
        if binding["path"] is not None or binding["sha256"] is not None:
            raise ValueError(f"{context}.manifest_binding path/hash must be null unless ready")
        manifest_path = ""
        manifest_sha256 = ""

    readiness = _object(root["readiness"], f"{context}.readiness")
    _exact(readiness, {"formal_decision_enabled", "blocking_items"}, f"{context}.readiness")
    enabled = _boolean(readiness["formal_decision_enabled"], f"{context}.readiness.formal_decision_enabled")
    blockers = tuple(
        _string(value, f"{context}.readiness.blocking_items[{index}]")
        for index, value in enumerate(_array(readiness["blocking_items"], f"{context}.readiness.blocking_items"))
    )
    if not (
        frozen
        and required_ids
        and contracts_ready
        and binding_status == "ready"
        and enabled
        and not blockers
    ):
        raise ValueError(
            f"{context}: formal decision is not ready; stability IDs, measurement "
            "contracts, corpus binding, and readiness must all be frozen/ready"
        )

    gate = GatePolicy(
        total_cases=EXPECTED_TOTAL_CASES,
        categories=dict(EXPECTED_CATEGORIES),
        minimum_net_basis_points=-300,
        minimum_net_cases=-7,
        minimum_top1_delta_basis_points=-800,
        minimum_top10_delta_basis_points=-1200,
        minimum_category_top1_delta_basis_points=-1000,
        protected_required=16,
        maximum_both_bad=12,
        maximum_warm_p95_ratio_basis_points=5000,
        maximum_pss_ratio_basis_points=12500,
        required_stability_ids=required_ids,
    )
    return ParsedPolicy(
        gate=gate,
        policy_id=policy_id,
        candidate_id="B0",
        product_source_revision=product_source_revision,
        artifact_source_revision=artifact_source_revision,
        candidate_resource_fingerprint=candidate_fingerprint,
        baseline_resource_fingerprint=baseline_fingerprint,
        artifacts=artifacts,
        product_executable=product_executable,
        runtime_dependencies=runtime_dependencies,
        runtime_dependency_integrity=runtime_integrity,
        ajimee_derived_sha256=derived_sha,
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha256,
        policy_sha256=_sha256_bytes(data),
        measurement=measurement,
        stability_orchestrator_sha256=orchestrator_sha,
        stability_checks=stability_checks,
    )


def _rational_check(
    check_id: str,
    *,
    numerator: int,
    denominator: int,
    minimum_basis_points: int,
) -> dict[str, Any]:
    if denominator <= 0:
        raise ValueError(f"{check_id}: denominator must be positive")
    left = numerator * 10_000
    right = minimum_basis_points * denominator
    return {
        "id": check_id,
        "passed": left >= right,
        "actual": {"numerator": numerator, "denominator": denominator},
        "comparison": {"left": left, "operator": ">=", "right": right},
        "limit_basis_points": minimum_basis_points,
    }


def _ratio_check(
    check_id: str,
    *,
    candidate: int | Decimal,
    baseline: int | Decimal,
    maximum_basis_points: int,
) -> dict[str, Any]:
    if candidate < 0 or baseline <= 0:
        raise ValueError(f"{check_id}: candidate must be non-negative and baseline positive")
    left = candidate * 10_000
    right = baseline * maximum_basis_points
    passed = left <= right
    render = lambda value: str(value) if isinstance(value, Decimal) else value
    return {
        "id": check_id,
        "passed": passed,
        "actual": {"candidate": render(candidate), "baseline": render(baseline)},
        "comparison": {"left": render(left), "operator": "<=", "right": render(right)},
        "limit_basis_points": maximum_basis_points,
    }


def evaluate_metrics(policy: GatePolicy, metrics: dict[str, Any]) -> list[dict[str, Any]]:
    """Evaluate already-derived counts without any floating-point division."""

    _exact(
        metrics,
        {"cases", "human", "quality", "warm_latency_p95_ms", "total_pss_kib", "stability"},
        "metrics",
    )
    cases = _positive_int(metrics["cases"], "metrics.cases")
    human = _object(metrics["human"], "metrics.human")
    _exact(human, {"wins", "losses", "ties", "both_bad"}, "metrics.human")
    wins = _nonnegative_int(human["wins"], "metrics.human.wins")
    losses = _nonnegative_int(human["losses"], "metrics.human.losses")
    ties = _nonnegative_int(human["ties"], "metrics.human.ties")
    both_bad = _nonnegative_int(human["both_bad"], "metrics.human.both_bad")
    if wins + losses + ties + both_bad != cases:
        raise ValueError("metrics.human counts do not add up to cases")

    quality = _object(metrics["quality"], "metrics.quality")
    _exact(quality, {"hazkey", "mozc"}, "metrics.quality")
    normalized: dict[str, dict[str, Any]] = {}
    for backend in ("hazkey", "mozc"):
        item_context = f"metrics.quality.{backend}"
        item = _object(quality[backend], item_context)
        _exact(item, {"cases", "top1_hits", "top10_hits", "categories"}, item_context)
        backend_cases = _positive_int(item["cases"], f"{item_context}.cases")
        top1_hits = _nonnegative_int(item["top1_hits"], f"{item_context}.top1_hits")
        top10_hits = _nonnegative_int(item["top10_hits"], f"{item_context}.top10_hits")
        if backend_cases != cases or top1_hits > top10_hits or top10_hits > backend_cases:
            raise ValueError(f"{item_context} totals are inconsistent")
        categories = _object(item["categories"], f"{item_context}.categories")
        if set(categories) != set(policy.categories):
            raise ValueError(f"{item_context}.categories do not match policy")
        normalized_categories: dict[str, dict[str, int]] = {}
        for category, expected_total in policy.categories.items():
            category_context = f"{item_context}.categories.{category}"
            category_metrics = _object(categories[category], category_context)
            _exact(category_metrics, {"cases", "top1_hits"}, category_context)
            total = _positive_int(category_metrics["cases"], f"{category_context}.cases")
            hits = _nonnegative_int(category_metrics["top1_hits"], f"{category_context}.top1_hits")
            if total != expected_total or hits > total:
                raise ValueError(f"{category_context} totals are inconsistent with policy")
            normalized_categories[category] = {"cases": total, "top1_hits": hits}
        if sum(item["cases"] for item in normalized_categories.values()) != backend_cases:
            raise ValueError(f"{item_context}.categories do not add up to cases")
        if sum(item["top1_hits"] for item in normalized_categories.values()) != top1_hits:
            raise ValueError(f"{item_context}.categories do not add up to top1_hits")
        normalized[backend] = {
            "cases": backend_cases,
            "top1_hits": top1_hits,
            "top10_hits": top10_hits,
            "categories": normalized_categories,
        }

    checks: list[dict[str, Any]] = [
        {
            "id": "formal-case-count",
            "passed": cases == policy.total_cases,
            "actual": cases,
            "operator": "==",
            "limit": policy.total_cases,
        }
    ]
    net = wins - losses
    checks.append(
        _rational_check(
            "human-net-preference-basis-points",
            numerator=net,
            denominator=cases,
            minimum_basis_points=policy.minimum_net_basis_points,
        )
    )
    checks.append(
        {
            "id": "human-net-preference-cases",
            "passed": net >= policy.minimum_net_cases,
            "actual": net,
            "operator": ">=",
            "limit": policy.minimum_net_cases,
        }
    )

    for check_id, field, limit in (
        ("top1-delta", "top1_hits", policy.minimum_top1_delta_basis_points),
        ("top10-delta", "top10_hits", policy.minimum_top10_delta_basis_points),
    ):
        hazkey = normalized["hazkey"]
        mozc = normalized["mozc"]
        numerator = mozc[field] * hazkey["cases"] - hazkey[field] * mozc["cases"]
        denominator = mozc["cases"] * hazkey["cases"]
        checks.append(
            _rational_check(
                check_id,
                numerator=numerator,
                denominator=denominator,
                minimum_basis_points=limit,
            )
        )

    for category in sorted(policy.categories):
        hazkey_category = normalized["hazkey"]["categories"][category]
        mozc_category = normalized["mozc"]["categories"][category]
        numerator = (
            mozc_category["top1_hits"] * hazkey_category["cases"]
            - hazkey_category["top1_hits"] * mozc_category["cases"]
        )
        denominator = mozc_category["cases"] * hazkey_category["cases"]
        checks.append(
            _rational_check(
                f"category-top1-delta:{category}",
                numerator=numerator,
                denominator=denominator,
                minimum_basis_points=policy.minimum_category_top1_delta_basis_points,
            )
        )

    protected_passes = normalized["mozc"]["categories"]["protected"]["top1_hits"]
    checks.extend(
        [
            {
                "id": "protected-cases",
                "passed": protected_passes == policy.protected_required,
                "actual": protected_passes,
                "operator": "==",
                "limit": policy.protected_required,
            },
            {
                "id": "both-bad",
                "passed": both_bad <= policy.maximum_both_bad,
                "actual": both_bad,
                "operator": "<=",
                "limit": policy.maximum_both_bad,
            },
        ]
    )

    latency = _object(metrics["warm_latency_p95_ms"], "metrics.warm_latency_p95_ms")
    _exact(latency, {"hazkey", "mozc"}, "metrics.warm_latency_p95_ms")
    try:
        hazkey_latency = Decimal(str(latency["hazkey"]))
        mozc_latency = Decimal(str(latency["mozc"]))
    except (InvalidOperation, ValueError) as error:
        raise ValueError("metrics warm latency values must be finite decimals") from error
    if not hazkey_latency.is_finite() or not mozc_latency.is_finite():
        raise ValueError("metrics warm latency values must be finite decimals")
    checks.append(
        _ratio_check(
            "warm-latency-p95-ratio",
            candidate=mozc_latency,
            baseline=hazkey_latency,
            maximum_basis_points=policy.maximum_warm_p95_ratio_basis_points,
        )
    )

    pss = _object(metrics["total_pss_kib"], "metrics.total_pss_kib")
    _exact(pss, {"hazkey", "mozc"}, "metrics.total_pss_kib")
    checks.append(
        _ratio_check(
            "total-pss-ratio",
            candidate=_nonnegative_int(pss["mozc"], "metrics.total_pss_kib.mozc"),
            baseline=_positive_int(pss["hazkey"], "metrics.total_pss_kib.hazkey"),
            maximum_basis_points=policy.maximum_pss_ratio_basis_points,
        )
    )

    stability_metrics = _object(metrics["stability"], "metrics.stability")
    if set(stability_metrics) != set(policy.required_stability_ids):
        raise ValueError("metrics.stability IDs do not exactly match the frozen policy")
    for check_id in policy.required_stability_ids:
        passed = _boolean(stability_metrics[check_id], f"metrics.stability.{check_id}")
        checks.append(
            {
                "id": f"stability:{check_id}",
                "passed": passed,
                "actual": passed,
                "operator": "==",
                "limit": True,
            }
        )
    return checks


def _parse_corpus_manifest(
    data: bytes, path: Path, expected_sha256: str
) -> tuple[bytes, str, dict[str, int], dict[str, str]]:
    # Keep the corpus builder authoritative for filenames, provenance, exact
    # four-column TSV syntax, NFC/hiragana normalization, IDs, and merge order.
    authoritative_aggregate = build_frozen_corpus.build_aggregate(path)
    payload = _object(_load_json_bytes(data, str(path)), str(path))
    _exact(payload, {"schema", "normalization", "components", "aggregate"}, str(path))
    _expect(payload["schema"], CORPUS_MANIFEST_SCHEMA, f"{path}.schema")
    _expect(
        payload["normalization"],
        {
            "unicode": "NFC",
            "line_endings": "LF",
            "reading_transform": "katakana-to-hiragana.v1",
        },
        f"{path}.normalization",
    )
    components = _array(payload["components"], f"{path}.components")
    if len(components) != len(EXPECTED_COMPONENTS):
        raise ValueError(f"{path}.components must contain exactly three entries")
    all_rows: list[dict[str, str]] = []
    component_bodies: list[bytes] = []
    component_hashes: dict[str, str] = {}
    seen_ids: set[str] = set()
    for index, (raw, expected) in enumerate(zip(components, EXPECTED_COMPONENTS, strict=True)):
        context = f"{path}.components[{index}]"
        component = _object(raw, context)
        _exact(
            component,
            {
                "id",
                "path",
                "sha256",
                "cases",
                "id_prefix",
                "categories",
                "provenance",
            },
            context,
        )
        expected_name, expected_cases, expected_prefix = expected
        _expect(component["id"], expected_name, f"{context}.id")
        _expect(component["cases"], expected_cases, f"{context}.cases")
        _expect(component["id_prefix"], expected_prefix, f"{context}.id_prefix")
        component_categories = _object(component["categories"], f"{context}.categories")
        component_path = _path(component["path"], path.parent, f"{context}.path")
        component_sha = _sha256(component["sha256"], f"{context}.sha256")
        component_hashes[expected_name] = component_sha
        component_bytes = _verified_bytes(component_path, component_sha, context)
        if (
            b"\r" in component_bytes
            or not component_bytes.endswith(b"\n")
            or not component_bytes.startswith(TSV_HEADER)
        ):
            raise ValueError(f"{context}: component must use the exact v1 TSV format")
        rows = load_corpus_bytes(component_bytes, str(component_path))
        if len(rows) != expected_cases:
            raise ValueError(f"{context}: actual case count does not match manifest")
        counts: dict[str, int] = {}
        for row in rows:
            if not row["id"].startswith(expected_prefix):
                raise ValueError(f"{context}: case id does not use frozen prefix")
            if row["id"] in seen_ids:
                raise ValueError(f"{context}: duplicate aggregate case id {row['id']!r}")
            seen_ids.add(row["id"])
            counts[row["category"]] = counts.get(row["category"], 0) + 1
        if counts != component_categories:
            raise ValueError(f"{context}: actual categories do not match manifest")
        expected_provenance = (
            AJIMEE_PROVENANCE
            if expected_name == "ajimee-unconditional"
            else {
                "kind": "project",
                "license": "MIT",
                "source": (
                    "grimodex-curated-v1"
                    if expected_name == "product-curated"
                    else "grimodex-protected-v1"
                ),
            }
        )
        _expect(component["provenance"], expected_provenance, f"{context}.provenance")
        all_rows.extend(rows)
        component_bodies.append(component_bytes[len(TSV_HEADER) :])

    aggregate = _object(payload["aggregate"], f"{path}.aggregate")
    _exact(aggregate, {"sha256", "cases", "categories"}, f"{path}.aggregate")
    _expect(aggregate["cases"], EXPECTED_TOTAL_CASES, f"{path}.aggregate.cases")
    _expect(aggregate["categories"], EXPECTED_CATEGORIES, f"{path}.aggregate.categories")
    aggregate_sha = _sha256(aggregate["sha256"], f"{path}.aggregate.sha256")
    aggregate_bytes = TSV_HEADER + b"".join(component_bodies)
    if aggregate_bytes != authoritative_aggregate:
        raise ValueError(f"{path}.aggregate differs from the authoritative builder")
    if _sha256_bytes(aggregate_bytes) != aggregate_sha:
        raise ValueError(f"{path}.aggregate hash does not match generated bytes")
    aggregate_rows = load_corpus_bytes(aggregate_bytes, f"{path}.aggregate")
    if aggregate_rows != all_rows:
        raise ValueError(f"{path}.aggregate reconstruction is inconsistent")
    if _sha256_bytes(data) != expected_sha256:
        raise ValueError(f"{path}: manifest hash changed after verification")
    return aggregate_bytes, aggregate_sha, dict(EXPECTED_CATEGORIES), component_hashes


def _copy_snapshot(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.write_bytes(data)
    path.chmod(mode)


def _parse_file_contract(raw: Any, root: Path, context: str) -> tuple[Path, str]:
    payload = _object(raw, context)
    _exact(payload, {"path", "sha256"}, context)
    return (
        _path(payload["path"], root, f"{context}.path"),
        _sha256(payload["sha256"], f"{context}.sha256"),
    )


def _parse_acquisition_manifest(
    raw: Any,
    root: Path,
    context: str,
    *,
    policy: ParsedPolicy,
    product_source_ref: str,
    corpus_path: Path,
    corpus_sha256: str,
    raw_bindings: dict[str, dict[str, tuple[Path, str]]],
) -> dict[str, Any]:
    manifest_path, manifest_sha256 = _parse_file_contract(raw, root, context)
    acquisition_root_metadata = manifest_path.parent.lstat()
    if (
        stat.S_ISLNK(acquisition_root_metadata.st_mode)
        or not stat.S_ISDIR(acquisition_root_metadata.st_mode)
        or acquisition_root_metadata.st_uid != os.getuid()
        or stat.S_IMODE(acquisition_root_metadata.st_mode) != 0o700
    ):
        raise ValueError(
            "acquisition root must be an owner-only mode-0700 non-symlink directory"
        )
    manifest_bytes = _verified_bytes(
        manifest_path, manifest_sha256, "acquisition manifest"
    )
    payload = _object(
        _load_json_bytes(manifest_bytes, str(manifest_path)), str(manifest_path)
    )
    _exact(
        payload,
        {
            "schema",
            "producer",
            "executable",
            "runtime_dependencies",
            "environment",
            "product_source_ref",
            "corpus",
            "host",
            "measurement",
            "entries",
            "integrity",
        },
        str(manifest_path),
    )
    _expect(
        payload["schema"], run_mozc_b0_measurement.SCHEMA, f"{manifest_path}.schema"
    )
    manifest_base = {key: value for key, value in payload.items() if key != "integrity"}
    expected_integrity = _sha256_bytes(
        json.dumps(
            manifest_base,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    _expect(
        _sha256(payload["integrity"], f"{manifest_path}.integrity"),
        expected_integrity,
        f"{manifest_path}.integrity",
    )

    producer_context = f"{manifest_path}.producer"
    producer = _object(payload["producer"], producer_context)
    _exact(producer, {"path", "sha256"}, producer_context)
    _expect(
        producer["path"],
        "tools/dictionary/run_mozc_b0_measurement.py",
        f"{producer_context}.path",
    )
    producer_sha256 = _sha256(producer["sha256"], f"{producer_context}.sha256")
    _expect(
        producer_sha256,
        policy.measurement.producer_sha256,
        f"{producer_context}.sha256",
    )

    executable_context = f"{manifest_path}.executable"
    executable = _object(payload["executable"], executable_context)
    _exact(
        executable,
        {"source_path", "snapshot_path", "size_bytes", "sha256"},
        executable_context,
    )
    executable_source_path = Path(
        _string(executable["source_path"], f"{executable_context}.source_path")
    )
    if not executable_source_path.is_absolute():
        raise ValueError(f"{executable_context}.source_path must be absolute")
    _expect(
        executable["snapshot_path"],
        f"{run_mozc_b0_measurement.SNAPSHOT_ROOT_NAME}/"
        f"{run_mozc_b0_measurement.SNAPSHOT_EXECUTABLE_NAME}",
        f"{executable_context}.snapshot_path",
    )
    executable_path = manifest_path.parent / executable["snapshot_path"]
    snapshot_root = manifest_path.parent / run_mozc_b0_measurement.SNAPSHOT_ROOT_NAME
    snapshot_root_metadata = snapshot_root.lstat()
    if (
        stat.S_ISLNK(snapshot_root_metadata.st_mode)
        or not stat.S_ISDIR(snapshot_root_metadata.st_mode)
        or stat.S_IMODE(snapshot_root_metadata.st_mode) != 0o555
    ):
        raise ValueError("acquisition runtime snapshot root must have mode 0555")
    executable_size = _positive_int(
        executable["size_bytes"], f"{executable_context}.size_bytes"
    )
    executable_sha256 = _sha256(
        executable["sha256"], f"{executable_context}.sha256"
    )
    executable_bytes = _verified_bytes(
        executable_path, executable_sha256, "acquisition executable"
    )
    _expect(
        (executable_size, executable_sha256),
        policy.product_executable,
        executable_context,
    )
    _expect(len(executable_bytes), executable_size, f"{executable_context}.size_bytes")
    if not os.access(executable_path, os.X_OK):
        raise ValueError(f"{executable_context}.snapshot_path is not executable")
    if stat.S_IMODE(executable_path.lstat().st_mode) != 0o555:
        raise ValueError(f"{executable_context}.snapshot_path must have mode 0555")

    runtime_context = f"{manifest_path}.runtime_dependencies"
    runtime = _object(payload["runtime_dependencies"], runtime_context)
    _exact(
        runtime,
        {"schema", "source_path", "snapshot_path", "files", "integrity"},
        runtime_context,
    )
    _expect(
        runtime["schema"],
        run_mozc_b0_measurement.RUNTIME_DEPENDENCY_SCHEMA,
        f"{runtime_context}.schema",
    )
    runtime_source = Path(
        _string(runtime["source_path"], f"{runtime_context}.source_path")
    )
    if not runtime_source.is_absolute():
        raise ValueError(f"{runtime_context}.source_path must be absolute")
    expected_snapshot_path = (
        f"{run_mozc_b0_measurement.SNAPSHOT_ROOT_NAME}/"
        f"{run_mozc_b0_measurement.SNAPSHOT_LIBRARY_DIRECTORY_NAME}"
    )
    _expect(
        runtime["snapshot_path"],
        expected_snapshot_path,
        f"{runtime_context}.snapshot_path",
    )
    runtime_snapshot = manifest_path.parent / runtime["snapshot_path"]
    runtime_metadata = runtime_snapshot.lstat()
    if (
        stat.S_ISLNK(runtime_metadata.st_mode)
        or not stat.S_ISDIR(runtime_metadata.st_mode)
        or stat.S_IMODE(runtime_metadata.st_mode) != 0o555
    ):
        raise ValueError(f"{runtime_context}.snapshot_path must be a mode-0555 directory")
    runtime_files = _array(runtime["files"], f"{runtime_context}.files")
    _expect(
        len(runtime_files),
        len(policy.runtime_dependencies),
        f"{runtime_context}.files length",
    )
    if {entry.name for entry in runtime_snapshot.iterdir()} != set(
        policy.runtime_dependencies
    ):
        raise ValueError(f"{runtime_context}.snapshot_path file set does not match policy")
    normalized_runtime_files: list[dict[str, Any]] = []
    for index, (raw_file, expected_name) in enumerate(
        zip(runtime_files, policy.runtime_dependencies, strict=True)
    ):
        file_context = f"{runtime_context}.files[{index}]"
        runtime_file = _object(raw_file, file_context)
        _exact(runtime_file, {"path", "size_bytes", "sha256"}, file_context)
        _expect(runtime_file["path"], expected_name, f"{file_context}.path")
        size = _positive_int(runtime_file["size_bytes"], f"{file_context}.size_bytes")
        digest = _sha256(runtime_file["sha256"], f"{file_context}.sha256")
        _expect((size, digest), policy.runtime_dependencies[expected_name], file_context)
        dependency_path = runtime_snapshot / expected_name
        dependency_data = _verified_bytes(
            dependency_path, digest, f"runtime dependency {expected_name}"
        )
        _expect(len(dependency_data), size, f"{file_context}.size_bytes")
        if stat.S_IMODE(dependency_path.lstat().st_mode) != 0o555:
            raise ValueError(f"{file_context} snapshot must have mode 0555")
        normalized_runtime_files.append(
            {"path": expected_name, "size_bytes": size, "sha256": digest}
        )
    runtime_integrity = _sha256(runtime["integrity"], f"{runtime_context}.integrity")
    _expect(
        runtime_integrity,
        policy.runtime_dependency_integrity,
        f"{runtime_context}.integrity",
    )
    _expect(
        runtime_integrity,
        _sha256_bytes(
            run_mozc_b0_measurement.canonical_json(
                {
                    "schema": run_mozc_b0_measurement.RUNTIME_DEPENDENCY_SCHEMA,
                    "files": normalized_runtime_files,
                }
            )
        ),
        f"{runtime_context}.integrity",
    )

    environment_context = f"{manifest_path}.environment"
    environment = _object(payload["environment"], environment_context)
    _exact(
        environment,
        {"policy", "cwd", "ambient_inheritance", "values"},
        environment_context,
    )
    _expect(
        environment["policy"],
        "private-runtime-snapshot-v1",
        f"{environment_context}.policy",
    )
    _expect(environment["cwd"], "acquisition-root", f"{environment_context}.cwd")
    _expect(
        environment["ambient_inheritance"],
        False,
        f"{environment_context}.ambient_inheritance",
    )
    environment_values_context = f"{environment_context}.values"
    environment_values = _object(
        environment["values"], environment_values_context
    )
    _exact(
        environment_values,
        {"GGML_BACKEND_DIR", "LANG", "LC_ALL", "LD_LIBRARY_PATH", "PATH", "TZ"},
        environment_values_context,
    )
    for field, expected in run_mozc_b0_measurement.CHILD_ENVIRONMENT.items():
        _expect(
            environment_values[field],
            expected,
            f"{environment_values_context}.{field}",
        )

    _expect(
        payload["product_source_ref"],
        product_source_ref,
        f"{manifest_path}.product_source_ref",
    )
    corpus_context = f"{manifest_path}.corpus"
    corpus = _object(payload["corpus"], corpus_context)
    _exact(corpus, {"path", "sha256", "cases"}, corpus_context)
    manifest_corpus_path = Path(_string(corpus["path"], f"{corpus_context}.path"))
    if not manifest_corpus_path.is_absolute():
        raise ValueError(f"{corpus_context}.path must be absolute")
    _expect(
        manifest_corpus_path.resolve(strict=True),
        corpus_path.resolve(strict=True),
        f"{corpus_context}.path",
    )
    _expect(
        _sha256(corpus["sha256"], f"{corpus_context}.sha256"),
        corpus_sha256,
        f"{corpus_context}.sha256",
    )
    _expect(corpus["cases"], policy.measurement.cases, f"{corpus_context}.cases")

    host_context = f"{manifest_path}.host"
    host = _object(payload["host"], host_context)
    _exact(host, {"fingerprint", "effective_cpu_affinity"}, host_context)
    host_fingerprint = _sha256(host["fingerprint"], f"{host_context}.fingerprint")
    affinity = tuple(
        _nonnegative_int(value, f"{host_context}.effective_cpu_affinity[{index}]")
        for index, value in enumerate(
            _array(
                host["effective_cpu_affinity"],
                f"{host_context}.effective_cpu_affinity",
            )
        )
    )
    if not affinity or tuple(sorted(set(affinity))) != affinity:
        raise ValueError(
            f"{host_context}.effective_cpu_affinity must be sorted and unique"
        )

    measurement_context = f"{manifest_path}.measurement"
    measurement = _object(payload["measurement"], measurement_context)
    _exact(
        measurement,
        {
            "runs_per_backend",
            "execution_order",
            "warmups_per_case",
            "iterations_per_case",
            "top_k",
            "latency_statistic",
            "pss_statistic",
            "cpu_policy",
            "per_run_timeout_seconds",
        },
        measurement_context,
    )
    expected_measurement = {
        "runs_per_backend": policy.measurement.runs_per_backend,
        "execution_order": list(policy.measurement.execution_order),
        "warmups_per_case": policy.measurement.warmups_per_case,
        "iterations_per_case": policy.measurement.iterations_per_case,
        "top_k": policy.measurement.top_k,
        "latency_statistic": policy.measurement.latency_statistic,
        "pss_statistic": policy.measurement.pss_statistic,
        "cpu_policy": policy.measurement.cpu_policy,
        "per_run_timeout_seconds": policy.measurement.per_run_timeout_seconds,
    }
    _expect(measurement, expected_measurement, measurement_context)

    entries = _array(payload["entries"], f"{manifest_path}.entries")
    _expect(len(entries), len(run_mozc_b0_measurement.SEQUENCE), f"{manifest_path}.entries length")
    previous_end = 0
    resources: dict[str, str] = {}
    observed_paths: set[Path] = set()
    parsed_entries: list[tuple[str, str, list[str]]] = []
    entry_bindings: dict[str, dict[str, str]] = {}
    for index, (raw_entry, expected_run) in enumerate(
        zip(entries, run_mozc_b0_measurement.SEQUENCE, strict=True), 1
    ):
        entry_context = f"{manifest_path}.entries[{index - 1}]"
        entry = _object(raw_entry, entry_context)
        _exact(
            entry,
            {
                "sequence",
                "id",
                "backend",
                "argv",
                "raw",
                "stderr",
                "exit_code",
                "started_monotonic_ns",
                "ended_monotonic_ns",
                "host_fingerprint",
                "effective_cpu_affinity",
            },
            entry_context,
        )
        run_id, backend = expected_run
        _expect(
            _integer(entry["sequence"], f"{entry_context}.sequence"),
            index,
            f"{entry_context}.sequence",
        )
        _expect(entry["id"], run_id, f"{entry_context}.id")
        _expect(entry["backend"], backend, f"{entry_context}.backend")
        _expect(
            _integer(entry["exit_code"], f"{entry_context}.exit_code"),
            0,
            f"{entry_context}.exit_code",
        )
        started = _nonnegative_int(
            entry["started_monotonic_ns"], f"{entry_context}.started_monotonic_ns"
        )
        ended = _nonnegative_int(
            entry["ended_monotonic_ns"], f"{entry_context}.ended_monotonic_ns"
        )
        if started < previous_end or ended < started:
            raise ValueError(f"{entry_context} timestamps overlap or go backwards")
        previous_end = ended
        _expect(
            _sha256(entry["host_fingerprint"], f"{entry_context}.host_fingerprint"),
            host_fingerprint,
            f"{entry_context}.host_fingerprint",
        )
        entry_affinity = tuple(
            _nonnegative_int(value, f"{entry_context}.effective_cpu_affinity[{item}]")
            for item, value in enumerate(
                _array(
                    entry["effective_cpu_affinity"],
                    f"{entry_context}.effective_cpu_affinity",
                )
            )
        )
        _expect(entry_affinity, affinity, f"{entry_context}.effective_cpu_affinity")
        argv = [
            _string(value, f"{entry_context}.argv[{item}]")
            for item, value in enumerate(
                _array(entry["argv"], f"{entry_context}.argv")
            )
        ]
        if len(argv) != 16:
            raise ValueError(f"{entry_context}.argv does not match the producer command")
        _expect(
            argv[0],
            run_mozc_b0_measurement.SNAPSHOT_EXECUTABLE_ARG,
            f"{entry_context}.argv executable",
        )
        resources.setdefault(backend, argv[15])
        _expect(argv[15], resources[backend], f"{entry_context}.argv resource")
        if not Path(argv[15]).is_absolute():
            raise ValueError(f"{entry_context}.argv resource must be absolute")
        parsed_entries.append((run_id, backend, argv))

        hashed: dict[str, str] = {}
        for field in ("raw", "stderr"):
            file_context = f"{entry_context}.{field}"
            expected_file_name = (
                f"{run_id}.jsonl" if field == "raw" else f"{run_id}.stderr"
            )
            file_payload = _object(entry[field], file_context)
            _expect(
                file_payload.get("path"),
                expected_file_name,
                f"{file_context}.path",
            )
            file_path, file_sha = _parse_file_contract(
                file_payload, manifest_path.parent, file_context
            )
            resolved_file = file_path.resolve(strict=True)
            if resolved_file in observed_paths:
                raise ValueError(f"{file_context}.path is duplicated")
            observed_paths.add(resolved_file)
            _verified_bytes(file_path, file_sha, file_context)
            hashed[f"{field}_sha256"] = file_sha
            if field == "raw":
                expected_path, expected_sha = raw_bindings[backend][run_id]
                _expect(resolved_file, expected_path.resolve(strict=True), f"{file_context}.path")
                _expect(file_sha, expected_sha, f"{file_context}.sha256")
        entry_bindings[run_id] = hashed

    for run_id, backend, argv in parsed_entries:
        expected_argv = run_mozc_b0_measurement._command(
            run_mozc_b0_measurement.SNAPSHOT_EXECUTABLE_ARG,
            manifest_corpus_path,
            product_source_ref,
            backend,
            Path(resources["hazkey"]),
            Path(resources["mozc"]),
        )
        _expect(argv, expected_argv, f"acquisition argv {run_id}")

    return {
        "manifest_sha256": manifest_sha256,
        "producer_sha256": producer_sha256,
        "executable": {
            "source_path": str(executable_source_path),
            "snapshot_path": executable["snapshot_path"],
            "size_bytes": executable_size,
            "sha256": executable_sha256,
        },
        "runtime_dependencies": {
            "source_path": str(runtime_source),
            "snapshot_path": runtime["snapshot_path"],
            "integrity": runtime_integrity,
            "files": normalized_runtime_files,
        },
        "environment": environment,
        "host": {
            "fingerprint": host_fingerprint,
            "effective_cpu_affinity": list(affinity),
        },
        "entries": entry_bindings,
    }


def evaluate(policy_path: Path, evidence_path: Path) -> dict[str, Any]:
    policy_bytes = _read_regular(policy_path, "policy")
    policy = parse_policy(policy_bytes, str(policy_path))
    evidence_bytes = _read_regular(evidence_path, "evidence")
    evidence = _object(_load_json_bytes(evidence_bytes, str(evidence_path)), str(evidence_path))
    _exact(
        evidence,
        {
            "schema",
            "policy",
            "product_source_ref",
            "corpus_manifest",
            "corpus",
            "packet",
            "judgments",
            "artifacts",
            "raw_runs",
            "acquisition_manifest",
            "stability",
        },
        str(evidence_path),
    )
    _expect(evidence["schema"], EVIDENCE_SCHEMA, f"{evidence_path}.schema")
    root = evidence_path.parent
    policy_contract = _object(evidence["policy"], f"{evidence_path}.policy")
    _exact(policy_contract, {"sha256"}, f"{evidence_path}.policy")
    expected_policy_sha = _sha256(policy_contract["sha256"], f"{evidence_path}.policy.sha256")
    _expect(expected_policy_sha, policy.policy_sha256, f"{evidence_path}.policy.sha256")
    product_source_ref = _string(
        evidence["product_source_ref"], f"{evidence_path}.product_source_ref"
    )
    if re.fullmatch(r"[0-9a-f]{40}", product_source_ref) is None:
        raise ValueError(
            f"{evidence_path}.product_source_ref must be a 40-hex commit"
        )
    _expect(
        product_source_ref,
        policy.product_source_revision,
        f"{evidence_path}.product_source_ref",
    )
    corpus_path, corpus_manifest_sha = _parse_file_contract(
        evidence["corpus_manifest"], root, f"{evidence_path}.corpus_manifest"
    )
    _expect(corpus_manifest_sha, policy.manifest_sha256, f"{evidence_path}.corpus_manifest.sha256")
    if Path(policy.manifest_path).name != corpus_path.name:
        raise ValueError(f"{evidence_path}.corpus_manifest.path does not match policy")
    corpus_manifest_bytes = _verified_bytes(corpus_path, corpus_manifest_sha, "corpus manifest")
    generated_corpus, aggregate_sha, categories, component_hashes = _parse_corpus_manifest(
        corpus_manifest_bytes, corpus_path, corpus_manifest_sha
    )
    _expect(
        component_hashes["ajimee-unconditional"],
        policy.ajimee_derived_sha256,
        "AJIMEE derived corpus hash",
    )
    aggregate_path, aggregate_contract_sha = _parse_file_contract(
        evidence["corpus"], root, f"{evidence_path}.corpus"
    )
    _expect(aggregate_contract_sha, aggregate_sha, f"{evidence_path}.corpus.sha256")
    actual_corpus = _verified_bytes(aggregate_path, aggregate_sha, "formal corpus")
    if actual_corpus != generated_corpus:
        raise ValueError("formal corpus is not the exact manifest-derived aggregate")
    _expect(categories, policy.gate.categories, "corpus categories")

    artifact_entries = _array(evidence["artifacts"], f"{evidence_path}.artifacts")
    observed_artifacts: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(artifact_entries):
        context = f"{evidence_path}.artifacts[{index}]"
        item = _object(raw, context)
        _exact(item, {"id", "path", "sha256"}, context)
        artifact_id = _string(item["id"], f"{context}.id")
        if artifact_id in observed_artifacts:
            raise ValueError(f"{evidence_path}.artifacts contains duplicate id {artifact_id!r}")
        path = _path(item["path"], root, f"{context}.path")
        expected_sha = _sha256(item["sha256"], f"{context}.sha256")
        data = _verified_bytes(path, expected_sha, context)
        observed_artifacts[artifact_id] = {"sha256": expected_sha, "size_bytes": len(data)}
    if set(observed_artifacts) != set(policy.artifacts):
        raise ValueError("artifact IDs do not exactly match policy")
    for artifact_id, (size, digest) in policy.artifacts.items():
        _expect(observed_artifacts[artifact_id], {"size_bytes": size, "sha256": digest}, f"artifact {artifact_id}")

    packet = _object(evidence["packet"], f"{evidence_path}.packet")
    _exact(packet, {"path", "manifest_sha256", "key_sha256", "review_sha256"}, f"{evidence_path}.packet")
    packet_path = _path(packet["path"], root, f"{evidence_path}.packet.path")
    metadata = packet_path.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise ValueError("packet path must be an owner-only non-symlink directory")
    expected_packet_names = {
        blind_conversion_ab.MANIFEST_NAME,
        blind_conversion_ab.KEY_NAME,
        blind_conversion_ab.REVIEW_NAME,
    }
    actual_packet_names = {entry.name for entry in packet_path.iterdir()}
    if actual_packet_names != expected_packet_names:
        raise ValueError("packet directory contents do not exactly match the schema")
    packet_contract = {
        blind_conversion_ab.MANIFEST_NAME: _sha256(packet["manifest_sha256"], f"{evidence_path}.packet.manifest_sha256"),
        blind_conversion_ab.KEY_NAME: _sha256(packet["key_sha256"], f"{evidence_path}.packet.key_sha256"),
        blind_conversion_ab.REVIEW_NAME: _sha256(packet["review_sha256"], f"{evidence_path}.packet.review_sha256"),
    }
    packet_bytes: dict[str, bytes] = {}
    for name, digest in packet_contract.items():
        item_path = packet_path / name
        item_metadata = item_path.lstat()
        if (
            item_metadata.st_uid != os.getuid()
            or stat.S_IMODE(item_metadata.st_mode) & 0o077
        ):
            raise ValueError(f"packet {name} must be owner-only")
        packet_bytes[name] = _verified_bytes(item_path, digest, f"packet {name}")

    judgments_path, judgments_sha = _parse_file_contract(
        evidence["judgments"], root, f"{evidence_path}.judgments"
    )
    judgments_bytes = _verified_bytes(judgments_path, judgments_sha, "judgments")

    raw_runs = _object(evidence["raw_runs"], f"{evidence_path}.raw_runs")
    _exact(raw_runs, {"hazkey", "mozc"}, f"{evidence_path}.raw_runs")
    raw_snapshots: dict[str, list[tuple[str, bytes]]] = {"hazkey": [], "mozc": []}
    raw_hashes: dict[str, dict[str, str]] = {"hazkey": {}, "mozc": {}}
    raw_bindings: dict[str, dict[str, tuple[Path, str]]] = {
        "hazkey": {},
        "mozc": {},
    }
    expected_fingerprints = {
        "hazkey": policy.baseline_resource_fingerprint,
        "mozc": policy.candidate_resource_fingerprint,
    }
    for backend in ("hazkey", "mozc"):
        entries = _array(raw_runs[backend], f"{evidence_path}.raw_runs.{backend}")
        seen_paths: set[Path] = set()
        seen_hashes: set[str] = set()
        for index, raw in enumerate(entries):
            context = f"{evidence_path}.raw_runs.{backend}[{index}]"
            item = _object(raw, context)
            _exact(item, {"id", "path", "sha256"}, context)
            run_id = _string(item["id"], f"{context}.id")
            if run_id in raw_hashes[backend]:
                raise ValueError(f"{context}: duplicate run id {run_id!r}")
            path = _path(item["path"], root, f"{context}.path")
            digest = _sha256(item["sha256"], f"{context}.sha256")
            normalized_path = path.absolute()
            if normalized_path in seen_paths or digest in seen_hashes:
                raise ValueError(f"{context}: duplicate raw run path or SHA-256")
            seen_paths.add(normalized_path)
            seen_hashes.add(digest)
            data = _verified_bytes(path, digest, context)
            loaded = summarize_ab_probe.load_run_bytes(data, str(path))
            _expect(
                loaded["schema"],
                summarize_ab_probe.INPUT_SCHEMA_V3,
                f"{context} schema",
            )
            _expect(loaded["converter_backend"], backend, f"{context} backend")
            _expect(
                loaded["warmups"],
                policy.measurement.warmups_per_case,
                f"{context} warmups",
            )
            _expect(
                loaded["iterations"],
                policy.measurement.iterations_per_case,
                f"{context} iterations",
            )
            _expect(loaded["top_k"], policy.measurement.top_k, f"{context} top_k")
            _expect(
                loaded["corpus"],
                {"sha256": aggregate_sha, "cases": policy.measurement.cases},
                f"{context} corpus",
            )
            _expect(
                len(loaded["cases"]),
                policy.measurement.cases,
                f"{context} case count",
            )
            _expect(
                loaded["source_ref"], product_source_ref, f"{context} source_ref"
            )
            _expect(
                loaded["resource"]["fingerprint"],
                expected_fingerprints[backend],
                f"{context} resource fingerprint",
            )
            for case_id, case in loaded["cases"].items():
                if any(value is None for value in case["parent_pss"]):
                    raise ValueError(f"{context} case {case_id!r} lacks parent PSS")
                if backend == "mozc" and any(
                    value is None for value in case["backend_pss"]
                ):
                    raise ValueError(f"{context} case {case_id!r} lacks helper PSS")
                if backend == "hazkey" and any(
                    value is not None for value in case["backend_pss"]
                ):
                    raise ValueError(
                        f"{context} case {case_id!r} has unexpected backend PSS"
                    )
            raw_hashes[backend][run_id] = digest
            raw_bindings[backend][run_id] = (path, digest)
            raw_snapshots[backend].append((run_id, data))
        _expect(
            tuple(raw_hashes[backend]),
            EXPECTED_RUN_IDS[backend],
            f"{evidence_path}.raw_runs.{backend} IDs",
        )

    acquisition_binding = _parse_acquisition_manifest(
        evidence["acquisition_manifest"],
        root,
        f"{evidence_path}.acquisition_manifest",
        policy=policy,
        product_source_ref=product_source_ref,
        corpus_path=aggregate_path,
        corpus_sha256=aggregate_sha,
        raw_bindings=raw_bindings,
    )

    stability_entries = _array(evidence["stability"], f"{evidence_path}.stability")
    stability_values: dict[str, bool] = {}
    stability_hashes: dict[str, dict[str, str]] = {}
    stability_observations: dict[str, dict[str, int | None]] = {}
    for index, raw in enumerate(stability_entries):
        context = f"{evidence_path}.stability[{index}]"
        item = _object(raw, context)
        _exact(item, {"id", "path", "sha256"}, context)
        check_id = _string(item["id"], f"{context}.id")
        if check_id in stability_values:
            raise ValueError(f"{evidence_path}.stability contains duplicate id {check_id!r}")
        digest = _sha256(item["sha256"], f"{context}.sha256")
        path, data = _self_contained_path(
            item["path"], root, f"{context}.path"
        )
        _verified_data(data, digest, context)
        record = _object(_load_json_bytes(data, str(path)), str(path))
        _exact(
            record,
            {
                "schema",
                "id",
                "orchestrator",
                "command",
                "product_source_ref",
                "artifact",
                "native_result",
            },
            str(path),
        )
        _expect(record["schema"], STABILITY_SCHEMA, f"{path}.schema")
        _expect(record["id"], check_id, f"{path}.id")
        contract = policy.stability_checks.get(check_id)
        if contract is None:
            raise ValueError(f"{path}.id is not a frozen stability check")
        orchestrator_context = f"{path}.orchestrator"
        orchestrator = _object(record["orchestrator"], orchestrator_context)
        _exact(orchestrator, {"path", "sha256"}, orchestrator_context)
        _expect(
            orchestrator["path"],
            run_mozc_b0_stability.ORCHESTRATOR_PATH,
            f"{orchestrator_context}.path",
        )
        _expect(
            _sha256(orchestrator["sha256"], f"{orchestrator_context}.sha256"),
            policy.stability_orchestrator_sha256,
            f"{orchestrator_context}.sha256",
        )
        command = tuple(
            _string(value, f"{path}.command[{item_index}]")
            for item_index, value in enumerate(
                _array(record["command"], f"{path}.command")
            )
        )
        _expect(command, contract.command, f"{path}.command")
        _expect(
            record["product_source_ref"],
            product_source_ref,
            f"{path}.product_source_ref",
        )
        artifact_context = f"{path}.artifact"
        artifact = _object(record["artifact"], artifact_context)
        if contract.artifact_kind == "b0":
            _exact(artifact, {"kind", "fingerprint"}, artifact_context)
            _expect(artifact["kind"], "b0", f"{artifact_context}.kind")
            _expect(
                _sha256(artifact["fingerprint"], f"{artifact_context}.fingerprint"),
                policy.candidate_resource_fingerprint,
                f"{artifact_context}.fingerprint",
            )
        else:
            _exact(artifact, {"kind", "fixture_identity"}, artifact_context)
            _expect(
                artifact["kind"], "fault-fixture", f"{artifact_context}.kind"
            )
            _expect(
                _sha256(
                    artifact["fixture_identity"],
                    f"{artifact_context}.fixture_identity",
                ),
                contract.recovery_fixture_identity,
                f"{artifact_context}.fixture_identity",
            )
        native_context = f"{path}.native_result"
        native = _object(record["native_result"], native_context)
        _exact(native, {"schema", "path", "sha256"}, native_context)
        _expect(native["schema"], contract.native_schema, f"{native_context}.schema")
        native_sha = _sha256(native["sha256"], f"{native_context}.sha256")
        native_path, native_bytes = _self_contained_path(
            native["path"], path.parent, f"{native_context}.path"
        )
        _verified_data(native_bytes, native_sha, native_context)
        observations = run_mozc_b0_stability.validate_native_result(
            check_id,
            native_bytes,
            str(native_path),
            run_mozc_b0_stability.NativeExpectations(
                product_source_ref=product_source_ref,
                artifact_fingerprint=policy.candidate_resource_fingerprint,
                product_server_size=policy.product_executable[0],
                product_server_sha256=policy.product_executable[1],
                artifacts=policy.artifacts,
                native_producer_sha256=contract.native_producer_sha256,
                recovery_fixture_identity=contract.recovery_fixture_identity,
                input_snapshot_fingerprint=contract.input_snapshot_fingerprint,
                execution_runner_path=contract.execution_runner_path,
                execution_runner_sha256=contract.execution_runner_sha256,
                swift_package_file_count=contract.execution_package_file_count,
                swift_package_size_bytes=contract.execution_package_size_bytes,
                swift_package_fingerprint=contract.execution_package_fingerprint,
                runtime_dependencies=policy.runtime_dependencies,
                baseline_resource_fingerprint=policy.baseline_resource_fingerprint,
            ),
            native_path=native_path,
        )
        expected_counts: dict[str, int | None] = {
            "helper_launches": contract.helper_launches,
            "server_launches": contract.server_launches,
            "helper_recoveries": contract.helper_recoveries,
            "server_recoveries": contract.server_recoveries,
            "residue_count": contract.residue_count,
        }
        exit_code = observations["exit_code"]
        conversions = observations["conversions"]
        cycles = observations["cycles"]
        if exit_code is None or conversions is None or cycles is None:
            raise ValueError(f"{native_path}: core stability observations are missing")
        stability_values[check_id] = (
            exit_code == 0
            and conversions >= contract.minimum_conversions
            and cycles >= contract.minimum_cycles
            and all(
                observations[field] == expected
                for field, expected in expected_counts.items()
                if expected is not None
            )
        )
        stability_observations[check_id] = observations
        stability_hashes[check_id] = {
            "record_sha256": digest,
            "native_result_sha256": native_sha,
        }
    if set(stability_values) != set(policy.gate.required_stability_ids):
        raise ValueError("stability evidence IDs do not exactly match frozen policy")

    with tempfile.TemporaryDirectory(prefix="mozc-b0-gate-") as temporary:
        snapshot_root = Path(temporary)
        snapshot_root.chmod(0o700)
        packet_snapshot = snapshot_root / "packet"
        packet_snapshot.mkdir(mode=0o700)
        for name, data in packet_bytes.items():
            _copy_snapshot(packet_snapshot / name, data)
        judgments_snapshot = snapshot_root / "judgments.jsonl"
        _copy_snapshot(judgments_snapshot, judgments_bytes)
        blind_report = blind_conversion_ab.score(packet_snapshot, judgments_snapshot)

        summaries: dict[str, dict[str, Any]] = {}
        for backend in ("hazkey", "mozc"):
            paths: list[Path] = []
            for index, (run_id, data) in enumerate(raw_snapshots[backend]):
                path = snapshot_root / f"{backend}-{index}-{run_id}.jsonl"
                _copy_snapshot(path, data)
                paths.append(path)
            summaries[backend] = summarize_ab_probe.summarize(paths)

    _expect(
        blind_report["source_ref"], product_source_ref, "blind report source_ref"
    )
    _expect(blind_report["cases"], policy.measurement.cases, "blind report cases")
    _expect(blind_report["top_k"], policy.measurement.top_k, "blind report top_k")
    _expect(blind_report["corpus"]["sha256"], aggregate_sha, "blind report corpus hash")
    _expect(blind_report["corpus"]["cases"], EXPECTED_TOTAL_CASES, "blind report corpus cases")
    _expect(blind_report["review_sha256"], packet_contract[blind_conversion_ab.REVIEW_NAME], "blind report review hash")
    _expect(blind_report["judgments_sha256"], judgments_sha, "blind report judgments hash")

    blind_backends = {item["converter_backend"]: item for item in blind_report["backends"]}
    if set(blind_backends) != {"hazkey", "mozc"}:
        raise ValueError("blind report backend set is not hazkey/mozc")
    expected_fingerprints = {
        "hazkey": policy.baseline_resource_fingerprint,
        "mozc": policy.candidate_resource_fingerprint,
    }
    for backend in ("hazkey", "mozc"):
        _expect(
            blind_backends[backend]["measurement"],
            {
                "warmups": policy.measurement.warmups_per_case,
                "iterations": policy.measurement.iterations_per_case,
            },
            f"{backend} blind measurement",
        )
        summary = summaries[backend]
        _expect(summary["schema"], summarize_ab_probe.OUTPUT_SCHEMA_V3, f"{backend} summary schema")
        _expect(summary["converter_backend"], backend, f"{backend} summary converter")
        _expect(summary["top_k"], policy.measurement.top_k, f"{backend} summary top_k")
        _expect(
            summary["runs"],
            policy.measurement.runs_per_backend,
            f"{backend} summary runs",
        )
        _expect(
            summary["iterations"],
            policy.measurement.iterations_per_case,
            f"{backend} summary iterations",
        )
        _expect(
            summary["cases_per_run"],
            policy.measurement.cases,
            f"{backend} summary cases",
        )
        _expect(
            summary["measured_conversions"],
            policy.measurement.runs_per_backend
            * policy.measurement.cases
            * policy.measurement.iterations_per_case,
            f"{backend} measured conversions",
        )
        _expect(
            summary["provenance"]["source_ref"],
            product_source_ref,
            f"{backend} summary source",
        )
        _expect(summary["provenance"]["corpus"], {"sha256": aggregate_sha, "cases": EXPECTED_TOTAL_CASES}, f"{backend} summary corpus")
        _expect(summary["provenance"]["resource"]["fingerprint"], expected_fingerprints[backend], f"{backend} summary resource fingerprint")
        _expect(blind_backends[backend]["resource"], summary["provenance"]["resource"], f"{backend} blind/summary resource")
        if blind_backends[backend]["run_sha256"] not in set(raw_hashes[backend].values()):
            raise ValueError(f"{backend} blind run hash is not present in raw evidence")

    objective = blind_report["objective_quality"]["by_backend"]
    quality_metrics: dict[str, Any] = {}
    for backend in ("hazkey", "mozc"):
        report = objective[backend]
        category_metrics = {
            category: {
                "cases": report["by_category"][category]["total"],
                "top1_hits": report["by_category"][category]["top1"],
            }
            for category in report["by_category"]
        }
        quality_metrics[backend] = {
            "cases": report["evaluated_cases"],
            "top1_hits": report["top1_hits"],
            "top10_hits": report["top10_hits"],
            "categories": category_metrics,
        }

    human = blind_report["human_preference"]
    mozc_human = human["by_backend"]["mozc"]
    metrics = {
        "cases": EXPECTED_TOTAL_CASES,
        "human": {
            "wins": mozc_human["wins"],
            "losses": mozc_human["losses"],
            "ties": mozc_human["ties"],
            "both_bad": human["judgment_counts"]["both_bad"],
        },
        "quality": quality_metrics,
        "warm_latency_p95_ms": {
            "hazkey": str(summaries["hazkey"]["p95_latency_ms"]),
            "mozc": str(summaries["mozc"]["p95_latency_ms"]),
        },
        "total_pss_kib": {
            "hazkey": summaries["hazkey"]["max_observed_total_pss_kib"],
            "mozc": summaries["mozc"]["max_observed_total_pss_kib"],
        },
        "stability": stability_values,
    }
    if metrics["total_pss_kib"]["hazkey"] is None or metrics["total_pss_kib"]["mozc"] is None:
        raise ValueError("both backends require measured total PSS")
    checks = evaluate_metrics(policy.gate, metrics)
    output_base = {
        "schema": OUTPUT_SCHEMA,
        "passed": all(check["passed"] for check in checks),
        "policy_id": policy.policy_id,
        "candidate": policy.candidate_id,
        "product_source_ref": product_source_ref,
        "artifact_source_revision": policy.artifact_source_revision,
        "measurement_contract": {
            "schema": summarize_ab_probe.INPUT_SCHEMA_V3,
            "producer_sha256": policy.measurement.producer_sha256,
            "runs_per_backend": policy.measurement.runs_per_backend,
            "execution_order": list(policy.measurement.execution_order),
            "warmups_per_case": policy.measurement.warmups_per_case,
            "iterations_per_case": policy.measurement.iterations_per_case,
            "top_k": policy.measurement.top_k,
            "cases": policy.measurement.cases,
            "latency_statistic": policy.measurement.latency_statistic,
            "pss_statistic": policy.measurement.pss_statistic,
            "cpu_policy": policy.measurement.cpu_policy,
            "per_run_timeout_seconds": policy.measurement.per_run_timeout_seconds,
        },
        "stability_observations": stability_observations,
        "bindings": {
            "policy_sha256": policy.policy_sha256,
            "corpus_manifest_sha256": corpus_manifest_sha,
            "corpus_sha256": aggregate_sha,
            "artifacts": observed_artifacts,
            "packet": packet_contract,
            "judgments_sha256": judgments_sha,
            "raw_runs": raw_hashes,
            "acquisition": acquisition_binding,
            "stability": stability_hashes,
        },
        "metrics": metrics,
        "checks": checks,
    }
    canonical = json.dumps(output_base, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return output_base | {"integrity": _sha256_bytes(canonical)}


def _write_atomic(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_descriptor = os.open(
            path.parent,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        result = evaluate(args.policy, args.evidence)
        rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        if args.output is None:
            sys.stdout.write(rendered)
        else:
            _write_atomic(args.output, rendered)
        return 0 if result["passed"] else 1
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
