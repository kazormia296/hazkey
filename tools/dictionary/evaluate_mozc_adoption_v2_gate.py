#!/usr/bin/env python3
"""Evaluate normalized metrics against the sealed Mozc adoption v2 gate.

This module is deliberately API-only.  It validates a strict normalized metric
object and provides exact integer gate arithmetic, but normalized metrics are
not formal evidence.  A future evidence wrapper must re-hash the raw probe,
blind-review, artifact, and native stability inputs before adoption can be
authorized.  Until then every result says ``formal_adoption_allowed=false``.

The frozen 256-case B0 evaluator remains a separate pilot implementation.
Nothing in this module accepts or upgrades its policy or result schemas.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any, Iterable


POLICY_SCHEMA = "hazkey.mozc-adoption-formal-gate-policy.v2"
POLICY_ID = "mozc-adoption-v2-formal-gate"
METRICS_SCHEMA = "hazkey.mozc-adoption-formal-gate-metrics.v2"
RESULT_SCHEMA = "hazkey.mozc-adoption-formal-gate-result.v2"
CORPUS_MANIFEST_SCHEMA = "hazkey.frozen-conversion-corpus-manifest.v2"
DECISION_TIER = "formal"
FORMAL_EVIDENCE_STATUS = "not_ready"
FORMAL_ADOPTION_ALLOWED = False
FORMAL_RESULTS = frozenset({"formal_pass", "formal_fail", "inconclusive"})

GENERATION = (
    "sealed-v2-sha256-"
    "b4c1351b1b0ef7797349ebf26858db4d0dd69ce1c8bcbfaee88e0f0b644225ed"
)
MANIFEST_SHA256 = (
    "sha256:3ccefa5552d1c0d851b07cc1ed8f65983dd7db019d9250509f2467af7bfd1c02"
)
CORPUS_SHA256 = (
    "sha256:cdb2a017b4548f6f77ec3d466f84ec09268a74adb5e876e224e01069f128c8ae"
)
SOURCE_POLICY_SHA256 = (
    "sha256:7b0a8e8ddcc9f8d2bfffd7dac6f365d7d5b1cf4ff42b92ba9fc4c99fce7f9220"
)
TOTAL_CASES = 1_360
QUALITY_CASES = 1_260
QUALITY_CATEGORIES = {
    "technical-mixed": 240,
    "proper-noun": 200,
    "colloquial": 200,
    "homophone-context": 200,
    "long-structural": 200,
    "grimodex-regression": 220,
}
PROTECTED_CATEGORY = "protected"
PROTECTED_CASES = 100
ALL_CATEGORIES = QUALITY_CATEGORIES | {PROTECTED_CATEGORY: PROTECTED_CASES}
CATEGORY_MINIMUM_DELTA_HITS = {
    "technical-mixed": -24,
    "proper-noun": -20,
    "colloquial": -20,
    "homophone-context": -20,
    "long-structural": -20,
    "grimodex-regression": -22,
}
REQUIRED_STABILITY_IDS = (
    "adapter-soak-150k",
    "protocol-v2-steady-1500",
    "protocol-v2-recovery",
    "fcitx-long-soak-150k",
    "fcitx-lifecycle-3x100",
)
RUNTIME_RESOURCE_FINGERPRINTS = {
    "B0": "sha256:2ba2cccb3c7489def988b63b0f0fd2cd96469521569c4807b63c80d2b50d3063",
    "B1": "sha256:65f3f341f491c1deec1182743c4923db3c7ad6f2609cb50cfde9c0a6b8e3adaa",
}
BLOCKING_ITEMS = (
    "candidate-aware raw acquisition validation",
    "candidate-aware native stability evidence validation",
)


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


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


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


def _sha256(value: Any, context: str) -> str:
    result = _string(value, context)
    if re.fullmatch(r"sha256:[0-9a-f]{64}", result) is None:
        raise ValueError(f"{context} must be a lowercase SHA-256 digest")
    return result


def _exact(payload: dict[str, Any], expected: set[str], context: str) -> None:
    actual = set(payload)
    if actual != expected:
        raise ValueError(
            f"{context} fields do not match schema; "
            f"missing={sorted(expected - actual)!r}, "
            f"unknown={sorted(actual - expected)!r}"
        )


def _expect(value: Any, expected: Any, context: str) -> None:
    if value != expected:
        raise ValueError(f"{context} must be {expected!r}, got {value!r}")


def _ceil_fraction(numerator: int, denominator: int) -> int:
    if denominator <= 0:
        raise ValueError("ceiling denominator must be positive")
    return -((-numerator) // denominator)


def _read_regular(path: Path, context: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
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
    finally:
        os.close(descriptor)
    data = b"".join(chunks)
    identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
    if identity(before) != identity(after) or len(data) != before.st_size:
        raise ValueError(f"{context} changed while it was read")
    return data


@dataclass(frozen=True)
class GatePolicy:
    total_cases: int
    quality_cases: int
    quality_categories: dict[str, int]
    all_categories: dict[str, int]
    minimum_human_basis_points: int
    minimum_human_net_cases: int
    minimum_top1_basis_points: int
    minimum_top1_delta_hits: int
    minimum_top10_basis_points: int
    minimum_top10_delta_hits: int
    minimum_category_basis_points: int
    minimum_category_delta_hits: dict[str, int]
    protected_required: int
    maximum_both_bad: int
    maximum_latency_ratio_basis_points: int
    maximum_pss_ratio_basis_points: int
    required_stability_ids: tuple[str, ...]


@dataclass(frozen=True)
class ParsedPolicy:
    gate: GatePolicy
    policy_id: str
    policy_sha256: str
    manifest_path: str
    manifest_sha256: str
    corpus_path: str
    corpus_sha256: str
    source_policy_path: str
    source_policy_sha256: str
    candidate_resource_fingerprints: dict[str, str]
    formal_evidence_status: str
    formal_adoption_allowed: bool
    blocking_items: tuple[str, ...]


def parse_policy(data: bytes, context: str = "formal gate policy") -> ParsedPolicy:
    root = _object(_load_json_bytes(data, context), context)
    _exact(
        root,
        {
            "schema",
            "policy_id",
            "decision_tier",
            "corpus_binding",
            "candidate_artifacts",
            "candidate_sequence",
            "gates",
            "api_contract",
        },
        context,
    )
    _expect(root["schema"], POLICY_SCHEMA, f"{context}.schema")
    _expect(root["policy_id"], POLICY_ID, f"{context}.policy_id")
    _expect(root["decision_tier"], DECISION_TIER, f"{context}.decision_tier")

    corpus = _object(root["corpus_binding"], f"{context}.corpus_binding")
    _exact(
        corpus,
        {
            "generation",
            "manifest",
            "aggregate",
            "source_policy",
            "quality_categories",
            "protected",
            "pilot_v1_counted",
        },
        f"{context}.corpus_binding",
    )
    _expect(corpus["generation"], GENERATION, f"{context}.corpus_binding.generation")

    manifest = _object(corpus["manifest"], f"{context}.corpus_binding.manifest")
    _exact(manifest, {"schema", "path", "sha256"}, f"{context}.corpus_binding.manifest")
    _expect(manifest["schema"], CORPUS_MANIFEST_SCHEMA, f"{context}.corpus_binding.manifest.schema")
    expected_manifest_path = f"{GENERATION}/manifest.json"
    _expect(manifest["path"], expected_manifest_path, f"{context}.corpus_binding.manifest.path")
    _expect(_sha256(manifest["sha256"], f"{context}.corpus_binding.manifest.sha256"), MANIFEST_SHA256, f"{context}.corpus_binding.manifest.sha256")

    aggregate = _object(corpus["aggregate"], f"{context}.corpus_binding.aggregate")
    _exact(aggregate, {"path", "sha256", "total_cases", "quality_cases"}, f"{context}.corpus_binding.aggregate")
    expected_corpus_path = f"{GENERATION}/formal-corpus.tsv"
    _expect(aggregate["path"], expected_corpus_path, f"{context}.corpus_binding.aggregate.path")
    _expect(_sha256(aggregate["sha256"], f"{context}.corpus_binding.aggregate.sha256"), CORPUS_SHA256, f"{context}.corpus_binding.aggregate.sha256")
    _expect(_positive_int(aggregate["total_cases"], f"{context}.corpus_binding.aggregate.total_cases"), TOTAL_CASES, f"{context}.corpus_binding.aggregate.total_cases")
    _expect(_positive_int(aggregate["quality_cases"], f"{context}.corpus_binding.aggregate.quality_cases"), QUALITY_CASES, f"{context}.corpus_binding.aggregate.quality_cases")

    source_policy = _object(corpus["source_policy"], f"{context}.corpus_binding.source_policy")
    _exact(source_policy, {"path", "sha256"}, f"{context}.corpus_binding.source_policy")
    expected_source_policy_path = f"{GENERATION}/corpus-policy.json"
    _expect(source_policy["path"], expected_source_policy_path, f"{context}.corpus_binding.source_policy.path")
    _expect(_sha256(source_policy["sha256"], f"{context}.corpus_binding.source_policy.sha256"), SOURCE_POLICY_SHA256, f"{context}.corpus_binding.source_policy.sha256")
    _expect(corpus["quality_categories"], QUALITY_CATEGORIES, f"{context}.corpus_binding.quality_categories")
    protected = _object(corpus["protected"], f"{context}.corpus_binding.protected")
    _exact(protected, {"category", "cases", "included_in_quality"}, f"{context}.corpus_binding.protected")
    normalized_protected = {
        "category": _string(protected["category"], f"{context}.corpus_binding.protected.category"),
        "cases": _positive_int(protected["cases"], f"{context}.corpus_binding.protected.cases"),
        "included_in_quality": _boolean(protected["included_in_quality"], f"{context}.corpus_binding.protected.included_in_quality"),
    }
    _expect(normalized_protected, {"category": PROTECTED_CATEGORY, "cases": PROTECTED_CASES, "included_in_quality": False}, f"{context}.corpus_binding.protected")
    _expect(_boolean(corpus["pilot_v1_counted"], f"{context}.corpus_binding.pilot_v1_counted"), False, f"{context}.corpus_binding.pilot_v1_counted")

    artifacts = _object(root["candidate_artifacts"], f"{context}.candidate_artifacts")
    _exact(artifacts, {"source", "eligible_candidate_ids", "runtime_resource_fingerprints"}, f"{context}.candidate_artifacts")
    _expect(artifacts["source"], "corpus_binding.source_policy.artifact_freezes", f"{context}.candidate_artifacts.source")
    _expect(artifacts["eligible_candidate_ids"], ["B0", "B1"], f"{context}.candidate_artifacts.eligible_candidate_ids")
    raw_fingerprints = _object(artifacts["runtime_resource_fingerprints"], f"{context}.candidate_artifacts.runtime_resource_fingerprints")
    _exact(raw_fingerprints, {"B0", "B1"}, f"{context}.candidate_artifacts.runtime_resource_fingerprints")
    fingerprints = {
        candidate: _sha256(raw_fingerprints[candidate], f"{context}.candidate_artifacts.runtime_resource_fingerprints.{candidate}")
        for candidate in ("B0", "B1")
    }
    _expect(fingerprints, RUNTIME_RESOURCE_FINGERPRINTS, f"{context}.candidate_artifacts.runtime_resource_fingerprints")

    sequence = _object(root["candidate_sequence"], f"{context}.candidate_sequence")
    _exact(sequence, {"evaluate_first", "evaluate_B1_only_if_B0_result", "B1_prior_result_binding", "B2_eligible", "post_disclosure_new_candidate_policy"}, f"{context}.candidate_sequence")
    _expect(sequence["evaluate_first"], "B0", f"{context}.candidate_sequence.evaluate_first")
    _expect(sequence["evaluate_B1_only_if_B0_result"], "formal_fail", f"{context}.candidate_sequence.evaluate_B1_only_if_B0_result")
    _expect(sequence["B1_prior_result_binding"], ["policy_sha256", "corpus_manifest_sha256", "corpus_sha256", "result_integrity"], f"{context}.candidate_sequence.B1_prior_result_binding")
    _expect(_boolean(sequence["B2_eligible"], f"{context}.candidate_sequence.B2_eligible"), False, f"{context}.candidate_sequence.B2_eligible")
    _expect(sequence["post_disclosure_new_candidate_policy"], "new_holdout_required", f"{context}.candidate_sequence.post_disclosure_new_candidate_policy")

    gates = _object(root["gates"], f"{context}.gates")
    _exact(gates, {"human_net_preference", "top1", "top10", "per_category_top1", "protected", "both_bad", "warm_latency_p95", "pss", "long_running_stability"}, f"{context}.gates")

    human = _object(gates["human_net_preference"], f"{context}.gates.human_net_preference")
    _exact(human, {"scope", "comparison_backend", "denominator_cases", "minimum_basis_points", "minimum_net_cases"}, f"{context}.gates.human_net_preference")
    _expect(human["scope"], "quality_categories", f"{context}.gates.human_net_preference.scope")
    _expect(human["comparison_backend"], "hazkey", f"{context}.gates.human_net_preference.comparison_backend")
    _expect(human["denominator_cases"], QUALITY_CASES, f"{context}.gates.human_net_preference.denominator_cases")
    _expect(human["minimum_basis_points"], -300, f"{context}.gates.human_net_preference.minimum_basis_points")
    _expect(human["minimum_net_cases"], -37, f"{context}.gates.human_net_preference.minimum_net_cases")
    _expect(_ceil_fraction(human["minimum_basis_points"] * QUALITY_CASES, 10_000), human["minimum_net_cases"], f"{context}.gates.human_net_preference integer boundary")

    def delta_gate(name: str, basis_points: int, minimum_hits: int) -> None:
        payload = _object(gates[name], f"{context}.gates.{name}")
        _exact(payload, {"scope", "comparison_backend", "minimum_delta_basis_points", "minimum_delta_hits"}, f"{context}.gates.{name}")
        _expect(payload["scope"], "quality_categories", f"{context}.gates.{name}.scope")
        _expect(payload["comparison_backend"], "hazkey", f"{context}.gates.{name}.comparison_backend")
        _expect(payload["minimum_delta_basis_points"], basis_points, f"{context}.gates.{name}.minimum_delta_basis_points")
        _expect(payload["minimum_delta_hits"], minimum_hits, f"{context}.gates.{name}.minimum_delta_hits")
        _expect(_ceil_fraction(basis_points * QUALITY_CASES, 10_000), minimum_hits, f"{context}.gates.{name} integer boundary")

    delta_gate("top1", -800, -100)
    delta_gate("top10", -1200, -151)

    category_gate = _object(gates["per_category_top1"], f"{context}.gates.per_category_top1")
    _exact(category_gate, {"scope", "comparison_backend", "minimum_delta_basis_points", "minimum_delta_hits"}, f"{context}.gates.per_category_top1")
    _expect(category_gate["scope"], "quality_categories", f"{context}.gates.per_category_top1.scope")
    _expect(category_gate["comparison_backend"], "hazkey", f"{context}.gates.per_category_top1.comparison_backend")
    _expect(category_gate["minimum_delta_basis_points"], -1000, f"{context}.gates.per_category_top1.minimum_delta_basis_points")
    _expect(category_gate["minimum_delta_hits"], CATEGORY_MINIMUM_DELTA_HITS, f"{context}.gates.per_category_top1.minimum_delta_hits")
    for category, cases in QUALITY_CATEGORIES.items():
        _expect(_ceil_fraction(-1000 * cases, 10_000), CATEGORY_MINIMUM_DELTA_HITS[category], f"{context}.gates.per_category_top1 integer boundary {category}")

    protected_gate = _object(gates["protected"], f"{context}.gates.protected")
    _exact(protected_gate, {"category", "total_cases", "required_top1_hits"}, f"{context}.gates.protected")
    _expect(protected_gate, {"category": PROTECTED_CATEGORY, "total_cases": PROTECTED_CASES, "required_top1_hits": PROTECTED_CASES}, f"{context}.gates.protected")
    both_bad = _object(gates["both_bad"], f"{context}.gates.both_bad")
    _exact(both_bad, {"scope", "maximum_cases"}, f"{context}.gates.both_bad")
    _expect(both_bad, {"scope": "quality_categories", "maximum_cases": 59}, f"{context}.gates.both_bad")

    def ratio_gate(name: str, expected: int) -> None:
        payload = _object(gates[name], f"{context}.gates.{name}")
        _exact(payload, {"comparison_backend", "maximum_ratio_basis_points"}, f"{context}.gates.{name}")
        _expect(payload["comparison_backend"], "hazkey", f"{context}.gates.{name}.comparison_backend")
        _expect(payload["maximum_ratio_basis_points"], expected, f"{context}.gates.{name}.maximum_ratio_basis_points")

    ratio_gate("warm_latency_p95", 5000)
    ratio_gate("pss", 12500)

    stability = _object(gates["long_running_stability"], f"{context}.gates.long_running_stability")
    _exact(stability, {"required_result", "required_check_ids"}, f"{context}.gates.long_running_stability")
    _expect(stability["required_result"], "all_pass", f"{context}.gates.long_running_stability.required_result")
    _expect(stability["required_check_ids"], list(REQUIRED_STABILITY_IDS), f"{context}.gates.long_running_stability.required_check_ids")

    api = _object(root["api_contract"], f"{context}.api_contract")
    _exact(api, {"normalized_metrics_schema", "result_schema", "formal_evidence_status", "precomputed_metrics_are_formal_evidence", "formal_adoption_allowed", "blocking_items"}, f"{context}.api_contract")
    _expect(api["normalized_metrics_schema"], METRICS_SCHEMA, f"{context}.api_contract.normalized_metrics_schema")
    _expect(api["result_schema"], RESULT_SCHEMA, f"{context}.api_contract.result_schema")
    _expect(api["formal_evidence_status"], FORMAL_EVIDENCE_STATUS, f"{context}.api_contract.formal_evidence_status")
    _expect(_boolean(api["precomputed_metrics_are_formal_evidence"], f"{context}.api_contract.precomputed_metrics_are_formal_evidence"), False, f"{context}.api_contract.precomputed_metrics_are_formal_evidence")
    _expect(_boolean(api["formal_adoption_allowed"], f"{context}.api_contract.formal_adoption_allowed"), FORMAL_ADOPTION_ALLOWED, f"{context}.api_contract.formal_adoption_allowed")
    _expect(api["blocking_items"], list(BLOCKING_ITEMS), f"{context}.api_contract.blocking_items")

    gate = GatePolicy(
        total_cases=TOTAL_CASES,
        quality_cases=QUALITY_CASES,
        quality_categories=dict(QUALITY_CATEGORIES),
        all_categories=dict(ALL_CATEGORIES),
        minimum_human_basis_points=-300,
        minimum_human_net_cases=-37,
        minimum_top1_basis_points=-800,
        minimum_top1_delta_hits=-100,
        minimum_top10_basis_points=-1200,
        minimum_top10_delta_hits=-151,
        minimum_category_basis_points=-1000,
        minimum_category_delta_hits=dict(CATEGORY_MINIMUM_DELTA_HITS),
        protected_required=PROTECTED_CASES,
        maximum_both_bad=59,
        maximum_latency_ratio_basis_points=5000,
        maximum_pss_ratio_basis_points=12500,
        required_stability_ids=REQUIRED_STABILITY_IDS,
    )
    return ParsedPolicy(
        gate=gate,
        policy_id=POLICY_ID,
        policy_sha256=_sha256_bytes(data),
        manifest_path=expected_manifest_path,
        manifest_sha256=MANIFEST_SHA256,
        corpus_path=expected_corpus_path,
        corpus_sha256=CORPUS_SHA256,
        source_policy_path=expected_source_policy_path,
        source_policy_sha256=SOURCE_POLICY_SHA256,
        candidate_resource_fingerprints=fingerprints,
        formal_evidence_status=FORMAL_EVIDENCE_STATUS,
        formal_adoption_allowed=FORMAL_ADOPTION_ALLOWED,
        blocking_items=BLOCKING_ITEMS,
    )


def _safe_bound_path(root: Path, raw: str, context: str) -> Path:
    path = Path(raw)
    if path.is_absolute() or ".." in path.parts or path.name in {"", ".", ".."}:
        raise ValueError(f"{context} must be a self-contained relative path")
    return root / path


def load_policy(path: Path, *, verify_bound_files: bool = True) -> ParsedPolicy:
    data = _read_regular(path, "formal gate policy")
    policy = parse_policy(data, str(path))
    if verify_bound_files:
        for label, raw_path, digest in (
            ("sealed corpus manifest", policy.manifest_path, policy.manifest_sha256),
            ("sealed formal corpus", policy.corpus_path, policy.corpus_sha256),
            ("sealed corpus policy", policy.source_policy_path, policy.source_policy_sha256),
        ):
            bound = _safe_bound_path(path.parent, raw_path, label)
            actual = _sha256_bytes(_read_regular(bound, label))
            if actual != digest:
                raise ValueError(f"{label} hash mismatch: expected {digest}, got {actual}")
    return policy


def _normalize_metrics(policy: ParsedPolicy, metrics: dict[str, Any]) -> dict[str, Any]:
    _exact(metrics, {"schema", "candidate_id", "corpus", "quality", "human", "warm_latency_p95_ms", "total_pss_kib", "stability"}, "metrics")
    _expect(metrics["schema"], METRICS_SCHEMA, "metrics.schema")
    candidate_id = _string(metrics["candidate_id"], "metrics.candidate_id")
    if candidate_id not in policy.candidate_resource_fingerprints:
        raise ValueError("metrics.candidate_id must be B0 or B1")

    corpus = _object(metrics["corpus"], "metrics.corpus")
    _exact(corpus, {"sha256", "total_cases", "quality_cases"}, "metrics.corpus")
    normalized_corpus = {
        "sha256": _sha256(corpus["sha256"], "metrics.corpus.sha256"),
        "total_cases": _positive_int(corpus["total_cases"], "metrics.corpus.total_cases"),
        "quality_cases": _positive_int(corpus["quality_cases"], "metrics.corpus.quality_cases"),
    }
    _expect(normalized_corpus, {"sha256": policy.corpus_sha256, "total_cases": policy.gate.total_cases, "quality_cases": policy.gate.quality_cases}, "metrics.corpus")

    quality = _object(metrics["quality"], "metrics.quality")
    _exact(quality, {"hazkey", "candidate"}, "metrics.quality")
    normalized_quality: dict[str, Any] = {}
    for backend in ("hazkey", "candidate"):
        backend_context = f"metrics.quality.{backend}"
        payload = _object(quality[backend], backend_context)
        _exact(payload, {"categories"}, backend_context)
        categories = _object(payload["categories"], f"{backend_context}.categories")
        if set(categories) != set(policy.gate.all_categories):
            raise ValueError(f"{backend_context}.categories do not exactly match policy")
        normalized_categories: dict[str, dict[str, int]] = {}
        for category, expected_cases in policy.gate.all_categories.items():
            category_context = f"{backend_context}.categories.{category}"
            item = _object(categories[category], category_context)
            _exact(item, {"cases", "top1_hits", "top10_hits"}, category_context)
            cases = _positive_int(item["cases"], f"{category_context}.cases")
            top1 = _nonnegative_int(item["top1_hits"], f"{category_context}.top1_hits")
            top10 = _nonnegative_int(item["top10_hits"], f"{category_context}.top10_hits")
            if cases != expected_cases or top1 > top10 or top10 > cases:
                raise ValueError(f"{category_context} totals are inconsistent with policy")
            normalized_categories[category] = {"cases": cases, "top1_hits": top1, "top10_hits": top10}
        if sum(item["cases"] for item in normalized_categories.values()) != policy.gate.total_cases:
            raise ValueError(f"{backend_context}.categories do not add up to total_cases")
        normalized_quality[backend] = {"categories": normalized_categories}

    human_value = metrics["human"]
    normalized_human: dict[str, Any] | None
    if human_value is None:
        normalized_human = None
    else:
        human = _object(human_value, "metrics.human")
        _exact(human, {"by_category"}, "metrics.human")
        by_category = _object(human["by_category"], "metrics.human.by_category")
        if set(by_category) != set(policy.gate.quality_categories):
            raise ValueError("metrics.human.by_category must contain only quality categories")
        normalized_human_categories: dict[str, dict[str, int]] = {}
        for category, expected_cases in policy.gate.quality_categories.items():
            item_context = f"metrics.human.by_category.{category}"
            item = _object(by_category[category], item_context)
            _exact(item, {"wins", "losses", "ties", "both_bad"}, item_context)
            counts = {
                field: _nonnegative_int(item[field], f"{item_context}.{field}")
                for field in ("wins", "losses", "ties", "both_bad")
            }
            if sum(counts.values()) != expected_cases:
                raise ValueError(f"{item_context} counts do not add up to category cases")
            normalized_human_categories[category] = counts
        normalized_human = {"by_category": normalized_human_categories}

    latency_value = metrics["warm_latency_p95_ms"]
    normalized_latency: dict[str, str] | None
    if latency_value is None:
        normalized_latency = None
    else:
        latency = _object(latency_value, "metrics.warm_latency_p95_ms")
        _exact(latency, {"hazkey", "candidate"}, "metrics.warm_latency_p95_ms")
        normalized_latency = {}
        for backend in ("hazkey", "candidate"):
            raw = _string(latency[backend], f"metrics.warm_latency_p95_ms.{backend}")
            if len(raw) > 256 or re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", raw) is None:
                raise ValueError(
                    "metrics warm latency values must be finite positive decimals"
                )
            try:
                value = Decimal(raw)
            except InvalidOperation as error:
                raise ValueError("metrics warm latency values must be finite positive decimals") from error
            if not value.is_finite() or value <= 0:
                raise ValueError("metrics warm latency values must be finite positive decimals")
            normalized_latency[backend] = raw

    pss_value = metrics["total_pss_kib"]
    normalized_pss: dict[str, int] | None
    if pss_value is None:
        normalized_pss = None
    else:
        pss = _object(pss_value, "metrics.total_pss_kib")
        _exact(pss, {"hazkey", "candidate"}, "metrics.total_pss_kib")
        normalized_pss = {
            backend: _positive_int(pss[backend], f"metrics.total_pss_kib.{backend}")
            for backend in ("hazkey", "candidate")
        }

    stability_value = metrics["stability"]
    normalized_stability: dict[str, bool] | None
    if stability_value is None:
        normalized_stability = None
    else:
        stability = _object(stability_value, "metrics.stability")
        if set(stability) != set(policy.gate.required_stability_ids):
            raise ValueError("metrics.stability IDs do not exactly match policy")
        normalized_stability = {
            check_id: _boolean(stability[check_id], f"metrics.stability.{check_id}")
            for check_id in policy.gate.required_stability_ids
        }

    return {
        "schema": METRICS_SCHEMA,
        "candidate_id": candidate_id,
        "corpus": normalized_corpus,
        "quality": normalized_quality,
        "human": normalized_human,
        "warm_latency_p95_ms": normalized_latency,
        "total_pss_kib": normalized_pss,
        "stability": normalized_stability,
    }


def _check(check_id: str, passed: bool | None, actual: Any, comparison: Any, limit: Any) -> dict[str, Any]:
    return {"id": check_id, "passed": passed, "actual": actual, "comparison": comparison, "limit": limit}


def _delta_check(check_id: str, baseline: int, candidate: int, cases: int, minimum_basis_points: int, minimum_hits: int) -> dict[str, Any]:
    delta = candidate - baseline
    left = delta * 10_000
    right = minimum_basis_points * cases
    return _check(
        check_id,
        left >= right and delta >= minimum_hits,
        {"hazkey_hits": baseline, "candidate_hits": candidate, "delta_hits": delta, "cases": cases},
        {"left": left, "operator": ">=", "right": right},
        {"minimum_basis_points": minimum_basis_points, "minimum_delta_hits": minimum_hits},
    )


def _not_run(check_id: str, limit: Any) -> dict[str, Any]:
    return _check(check_id, None, None, {"status": "not_run"}, limit)


def _evaluate_checks(gate: GatePolicy, metrics: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    human = metrics["human"]
    if human is None:
        checks.append(_not_run("human-net-preference", {"minimum_basis_points": gate.minimum_human_basis_points, "minimum_net_cases": gate.minimum_human_net_cases}))
    else:
        categories = human["by_category"]
        wins = sum(item["wins"] for item in categories.values())
        losses = sum(item["losses"] for item in categories.values())
        net = wins - losses
        left = net * 10_000
        right = gate.minimum_human_basis_points * gate.quality_cases
        checks.append(_check("human-net-preference", left >= right and net >= gate.minimum_human_net_cases, {"wins": wins, "losses": losses, "net_cases": net, "cases": gate.quality_cases}, {"left": left, "operator": ">=", "right": right}, {"minimum_basis_points": gate.minimum_human_basis_points, "minimum_net_cases": gate.minimum_human_net_cases}))

    quality = metrics["quality"]
    totals: dict[str, dict[str, int]] = {}
    for backend in ("hazkey", "candidate"):
        categories = quality[backend]["categories"]
        totals[backend] = {
            "top1_hits": sum(categories[category]["top1_hits"] for category in gate.quality_categories),
            "top10_hits": sum(categories[category]["top10_hits"] for category in gate.quality_categories),
        }
    checks.append(_delta_check("top1-delta", totals["hazkey"]["top1_hits"], totals["candidate"]["top1_hits"], gate.quality_cases, gate.minimum_top1_basis_points, gate.minimum_top1_delta_hits))
    checks.append(_delta_check("top10-delta", totals["hazkey"]["top10_hits"], totals["candidate"]["top10_hits"], gate.quality_cases, gate.minimum_top10_basis_points, gate.minimum_top10_delta_hits))
    for category, cases in gate.quality_categories.items():
        checks.append(_delta_check(f"category-top1-delta:{category}", quality["hazkey"]["categories"][category]["top1_hits"], quality["candidate"]["categories"][category]["top1_hits"], cases, gate.minimum_category_basis_points, gate.minimum_category_delta_hits[category]))

    protected_hits = quality["candidate"]["categories"][PROTECTED_CATEGORY]["top1_hits"]
    checks.append(_check("protected-cases", protected_hits == gate.protected_required, protected_hits, {"operator": "=="}, gate.protected_required))

    if human is None:
        checks.append(_not_run("both-bad", gate.maximum_both_bad))
    else:
        both_bad = sum(item["both_bad"] for item in human["by_category"].values())
        checks.append(_check("both-bad", both_bad <= gate.maximum_both_bad, both_bad, {"operator": "<="}, gate.maximum_both_bad))

    latency = metrics["warm_latency_p95_ms"]
    if latency is None:
        checks.append(_not_run("warm-latency-p95-ratio", gate.maximum_latency_ratio_basis_points))
    else:
        baseline = Decimal(latency["hazkey"])
        candidate = Decimal(latency["candidate"])
        candidate_numerator, candidate_denominator = candidate.as_integer_ratio()
        baseline_numerator, baseline_denominator = baseline.as_integer_ratio()
        left = candidate_numerator * 10_000 * baseline_denominator
        right = (
            baseline_numerator
            * gate.maximum_latency_ratio_basis_points
            * candidate_denominator
        )
        checks.append(_check("warm-latency-p95-ratio", left <= right, {"hazkey": latency["hazkey"], "candidate": latency["candidate"]}, {"left": left, "operator": "<=", "right": right}, gate.maximum_latency_ratio_basis_points))

    pss = metrics["total_pss_kib"]
    if pss is None:
        checks.append(_not_run("total-pss-ratio", gate.maximum_pss_ratio_basis_points))
    else:
        left = pss["candidate"] * 10_000
        right = pss["hazkey"] * gate.maximum_pss_ratio_basis_points
        checks.append(_check("total-pss-ratio", left <= right, {"hazkey": pss["hazkey"], "candidate": pss["candidate"]}, {"left": left, "operator": "<=", "right": right}, gate.maximum_pss_ratio_basis_points))

    stability = metrics["stability"]
    for check_id in gate.required_stability_ids:
        if stability is None:
            checks.append(_not_run(f"stability:{check_id}", True))
        else:
            passed = stability[check_id]
            checks.append(_check(f"stability:{check_id}", passed, passed, {"operator": "=="}, True))
    return checks


def derive_formal_result(checks: Iterable[dict[str, Any]]) -> str:
    states: list[bool | None] = []
    for index, check in enumerate(checks):
        state = check.get("passed")
        if state is not True and state is not False and state is not None:
            raise ValueError(f"checks[{index}].passed must be boolean or null")
        states.append(state)
    if not states:
        raise ValueError("formal gate requires at least one check")
    if any(state is False for state in states):
        return "formal_fail"
    if all(state is True for state in states):
        return "formal_pass"
    return "inconclusive"


def encode_result(result: dict[str, Any]) -> bytes:
    return _canonical_json(result) + b"\n"


def _result_base(policy: ParsedPolicy, metrics: dict[str, Any], checks: list[dict[str, Any]], prior_sha256: str | None) -> dict[str, Any]:
    candidate_id = metrics["candidate_id"]
    return {
        "schema": RESULT_SCHEMA,
        "decision_tier": DECISION_TIER,
        "formal_evidence_status": policy.formal_evidence_status,
        "formal_adoption_allowed": policy.formal_adoption_allowed,
        "gate_result": derive_formal_result(checks),
        "policy_id": policy.policy_id,
        "policy_sha256": policy.policy_sha256,
        "candidate_id": candidate_id,
        "candidate_resource_fingerprint": policy.candidate_resource_fingerprints[candidate_id],
        "corpus": {
            "manifest_sha256": policy.manifest_sha256,
            "sha256": policy.corpus_sha256,
            "total_cases": policy.gate.total_cases,
            "quality_cases": policy.gate.quality_cases,
        },
        "prior_b0_result_sha256": prior_sha256,
        "metrics": metrics,
        "checks": checks,
    }


def _validate_prior_b0_result(policy: ParsedPolicy, data: bytes) -> str:
    payload = _object(_load_json_bytes(data, "prior B0 result"), "prior B0 result")
    if data != encode_result(payload):
        raise ValueError("prior B0 result must use canonical JSON encoding")
    _exact(payload, {"schema", "decision_tier", "formal_evidence_status", "formal_adoption_allowed", "gate_result", "policy_id", "policy_sha256", "candidate_id", "candidate_resource_fingerprint", "corpus", "prior_b0_result_sha256", "metrics", "checks", "integrity"}, "prior B0 result")
    integrity = _sha256(payload["integrity"], "prior B0 result.integrity")
    base = {key: value for key, value in payload.items() if key != "integrity"}
    _expect(integrity, _sha256_bytes(_canonical_json(base)), "prior B0 result.integrity")
    _expect(payload["schema"], RESULT_SCHEMA, "prior B0 result.schema")
    _expect(payload["decision_tier"], DECISION_TIER, "prior B0 result.decision_tier")
    _expect(payload["formal_evidence_status"], policy.formal_evidence_status, "prior B0 result.formal_evidence_status")
    _expect(payload["formal_adoption_allowed"], policy.formal_adoption_allowed, "prior B0 result.formal_adoption_allowed")
    _expect(payload["policy_id"], policy.policy_id, "prior B0 result.policy_id")
    _expect(payload["policy_sha256"], policy.policy_sha256, "prior B0 result.policy_sha256")
    _expect(payload["candidate_id"], "B0", "prior B0 result.candidate_id")
    _expect(payload["candidate_resource_fingerprint"], policy.candidate_resource_fingerprints["B0"], "prior B0 result.candidate_resource_fingerprint")
    _expect(payload["prior_b0_result_sha256"], None, "prior B0 result.prior_b0_result_sha256")
    expected_corpus = {"manifest_sha256": policy.manifest_sha256, "sha256": policy.corpus_sha256, "total_cases": policy.gate.total_cases, "quality_cases": policy.gate.quality_cases}
    _expect(payload["corpus"], expected_corpus, "prior B0 result.corpus")
    metrics = _normalize_metrics(policy, _object(payload["metrics"], "prior B0 result.metrics"))
    _expect(metrics["candidate_id"], "B0", "prior B0 result.metrics.candidate_id")
    expected_checks = _evaluate_checks(policy.gate, metrics)
    _expect(payload["checks"], expected_checks, "prior B0 result.checks")
    expected_result = derive_formal_result(expected_checks)
    _expect(payload["gate_result"], expected_result, "prior B0 result.gate_result")
    _expect(expected_result, "formal_fail", "prior B0 result.gate_result")
    return _sha256_bytes(data)


def evaluate_metrics(policy: ParsedPolicy, metrics: dict[str, Any], *, prior_b0_result: bytes | None = None) -> dict[str, Any]:
    """Evaluate normalized metrics without treating them as formal evidence."""

    normalized = _normalize_metrics(policy, metrics)
    candidate_id = normalized["candidate_id"]
    if candidate_id == "B0":
        if prior_b0_result is not None:
            raise ValueError("B0 evaluation must not supply a prior B0 result")
        prior_sha256 = None
    elif candidate_id == "B1":
        if policy.formal_evidence_status != "ready":
            raise ValueError(
                "B1 evaluation is blocked until a raw-evidence wrapper can "
                "verify a B0 formal_fail result"
            )
        if prior_b0_result is None:
            raise ValueError("B1 evaluation requires an integrity-bound B0 formal_fail result")
        prior_sha256 = _validate_prior_b0_result(policy, prior_b0_result)
    else:  # Kept explicit even though normalization already rejects it.
        raise ValueError("only B0 and B1 are eligible for the v2 holdout")

    checks = _evaluate_checks(policy.gate, normalized)
    base = _result_base(policy, normalized, checks, prior_sha256)
    return base | {"integrity": _sha256_bytes(_canonical_json(base))}


def main(argv: Iterable[str] | None = None) -> int:
    del argv
    print(
        "error: normalized metrics API is not formal evidence; "
        "candidate-aware raw acquisition and stability validation are not ready",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
