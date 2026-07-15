#!/usr/bin/env python3
"""Validate and bind native results for the frozen Mozc B0 stability suites.

The output record intentionally contains no trusted ``passed`` flag and no
caller-supplied aggregate counters.  It binds one native result by SHA-256;
the B0 gate imports this module and derives all observations again from the
native schema.  This keeps the collector useful without turning it into a
generic "counts say green" escape hatch.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import threading
import time
from typing import Any, Mapping

if __package__:
    from . import run_mozc_b0_measurement, summarize_ab_probe
else:
    import run_mozc_b0_measurement  # type: ignore[no-redef]
    import summarize_ab_probe  # type: ignore[no-redef]


RECORD_SCHEMA = "hazkey.mozc-b0-stability-record.v2"
ADAPTER_SOAK_SCHEMA = "hazkey.mozc-b0-adapter-soak-result.v1"
PROTOCOL_STEADY_SCHEMA = "hazkey.mozc-b0-protocol-v2-steady-result.v3"
RECOVERY_SCHEMA = "hazkey.mozc-b0-protocol-v2-recovery-result.v5"
FCITX_SCHEMA = "hazkey.fcitx-full-stack-result.v1"
FCITX_SNAPSHOT_SCHEMA = "hazkey.fcitx-full-stack-input-snapshot.v1"
FCITX_SNAPSHOT_FINGERPRINT_DOMAIN = "hazkey.fcitx-input-snapshot.v1"
FCITX_RETAINED_EVIDENCE_ROOT_PREFIX = "fcitx-evidence"
PROTOCOL_V2_BENCHMARK_SCHEMA = "hazkey.protocol-v2-backend-benchmark.v1"
ORCHESTRATOR_PATH = "tools/dictionary/run_mozc_b0_stability.py"
DEFAULT_POLICY_PATH = (
    "hazkey-server/Tests/grimodex-spike/Fixtures/mozc-adoption-v1/"
    "b0-policy.json"
)
FCITX_PRODUCER_PATH = "fcitx5-hazkey/tests/run_fcitx_full_stack_test.py"
PROTOCOL_BENCHMARK_SOURCE_PATH = (
    "hazkey-server/Tests/grimodex-spike/"
    "grimodexProcessBackendBenchmarkTests.swift"
)
SWIFT_TEST_RUNNER_PATH = "hazkey-server/scripts/swift-test.sh"
SWIFT_PACKAGE_ROOT = "hazkey-server"
SWIFT_PACKAGE_SNAPSHOT_PATH = "swift-package"
SWIFT_PACKAGE_FINGERPRINT_DOMAIN = "hazkey.swift-package-snapshot.v1"
SWIFT_PACKAGE_EXCLUDED_PREFIXES = (
    "Tests/grimodex-spike/Fixtures/mozc-adoption-v1",
    "Tests/grimodex-spike/Fixtures/mozc-adoption-v2",
)
SWIFT_PACKAGE_EXPLICIT_FILES = (
    "Package.swift",
    "Package.resolved",
    "prepare_azookey_dependency.cmake",
    "scripts/swift-test.sh",
)
SWIFT_PACKAGE_RECURSIVE_ROOTS = (
    "Sources",
    "Tests/grimodex-spike",
    "patches/AzooKeyKanaKanjiConverter",
)
RECOVERY_SOURCE_PATH = (
    "hazkey-server/Tests/grimodex-spike/grimodexMozcSidecarTests.swift"
)
ADAPTER_CORPUS_PATH = (
    "hazkey-server/Tests/grimodex-spike/Fixtures/ime-base-ab-v1/"
    "conversion-quality-v1.tsv"
)
ADAPTER_CORPUS_SHA256 = (
    "sha256:e5c61cc92042c24ff334f702c7bd3e01473e37002c9d64d6c652462721520e9e"
)
PRODUCT_SOURCE_REF = "6e0354f2514edf1fe8219657ed23e7a02c8a7f7a"
B0_RESOURCE_FINGERPRINT = (
    "sha256:2ba2cccb3c7489def988b63b0f0fd2cd96469521569c4807b63c80d2b50d3063"
)
B0_MANIFEST_SHA256 = (
    "sha256:ebdc1bff4da9fbafe3971de7e5f095c90ad78e00e3f40b10fa5a7249d78a7c16"
)
B0_GENERATION_NAME = B0_RESOURCE_FINGERPRINT.replace("sha256:", "sha256-")
ADAPTER_RAW_NAME = "adapter-soak-abprobe.jsonl"
PROTOCOL_RAW_NAME = "protocol-v2-benchmark.json"
PROTOCOL_CASES = (
    ("sentence-weather", "general"),
    ("term-japanese-input", "general"),
    ("proper-yamanashi-museum", "proper-noun"),
    ("suffix-shiga-direction", "proper-noun"),
    ("verb-append", "general"),
    ("mixed-main-merge", "mixed-ascii"),
    ("mixed-github-particle", "mixed-ascii"),
    ("syntax-particle", "syntax"),
    ("syntax-colloquial", "syntax"),
    ("idiom-close-at-hand", "syntax"),
    ("particle-tea", "syntax"),
    ("compound-interpersonal", "compound"),
    ("compound-sleep-call", "compound"),
    ("expressive-very", "expressive"),
    ("ambiguity-rainbow", "ambiguity"),
)
PROTOCOL_STEADY_TEST = (
    "GrimodexProcessBackendBenchmarkTests/"
    "testProtocolV2BackendComparisonKeepsLongLivedProcessesStable"
)

ADAPTER_SOAK_ID = "adapter-soak-150k"
PROTOCOL_STEADY_ID = "protocol-v2-steady-1500"
PROTOCOL_RECOVERY_ID = "protocol-v2-recovery"
FCITX_LONG_SOAK_ID = "fcitx-long-soak-150k"
FCITX_LIFECYCLE_ID = "fcitx-lifecycle-3x100"
SUITE_IDS = (
    ADAPTER_SOAK_ID,
    PROTOCOL_STEADY_ID,
    PROTOCOL_RECOVERY_ID,
    FCITX_LONG_SOAK_ID,
    FCITX_LIFECYCLE_ID,
)
B0_SUITE_IDS = frozenset(
    {
        ADAPTER_SOAK_ID,
        PROTOCOL_STEADY_ID,
        FCITX_LONG_SOAK_ID,
        FCITX_LIFECYCLE_ID,
    }
)

RECOVERY_SUBCHECKS = (
    (
        "eof",
        "GrimodexMozcSidecarTests/"
        "testProtocolV2RealServerDoesNotReplayEOFAndRecoversFreshRequest",
        "eof_after_convert_once",
    ),
    (
        "partial-frame-eof",
        "GrimodexMozcSidecarTests/"
        "testProtocolV2RealServerDoesNotReplayPartialFrameEOFAndRecoversFreshRequest",
        "partial_body_eof_after_convert_once",
    ),
    (
        "timeout",
        "GrimodexMozcSidecarTests/"
        "testProtocolV2RealServerDoesNotReplayTimeoutAndRecoversFreshRequest",
        "timeout_after_convert_once",
    ),
    (
        "external-sigkill",
        "GrimodexMozcSidecarTests/"
        "testProtocolV2RealServerLazilyRespawnsExternallyKilledHelper",
        "ok",
    ),
)

CANONICAL_COMMANDS: dict[str, tuple[str, ...]] = {
    ADAPTER_SOAK_ID: (
        "python3",
        ORCHESTRATOR_PATH,
        "run-adapter",
        "--server",
        "<product-server>",
        "--runtime-lib-dir",
        "<runtime-lib-dir>",
        "--mozc-generation",
        "<b0-generation>",
        "--output-directory",
        "<output-directory>",
    ),
    PROTOCOL_STEADY_ID: (
        "python3",
        ORCHESTRATOR_PATH,
        "run-protocol-steady",
        "--server",
        "<product-server>",
        "--runtime-lib-dir",
        "<runtime-lib-dir>",
        "--mozc-generation",
        "<b0-generation>",
        "--dictionary",
        "<hazkey-dictionary>",
        "--output-directory",
        "<output-directory>",
    ),
    PROTOCOL_RECOVERY_ID: (
        "python3",
        ORCHESTRATOR_PATH,
        "run-recovery",
        "--server",
        "<product-server>",
        "--runtime-lib-dir",
        "<runtime-lib-dir>",
        "--output-directory",
        "<output-directory>",
    ),
    FCITX_LONG_SOAK_ID: (
        "python3",
        FCITX_PRODUCER_PATH,
        "--converter-backend",
        "mozc",
        "--cycles",
        "1",
        "--soak-iterations",
        "150000",
        "--result-output",
        "<native-result>",
    ),
    FCITX_LIFECYCLE_ID: (
        "python3",
        FCITX_PRODUCER_PATH,
        "--converter-backend",
        "mozc",
        "--cycles",
        "3",
        "--soak-iterations",
        "100",
        "--result-output",
        "<native-result>",
    ),
}

ADAPTER_PROBE_COMMAND = (
    "./runtime/hazkey-server",
    "--ab-probe",
    "--converter-backend",
    "mozc",
    "--corpus",
    "./inputs/conversion-quality-v1.tsv",
    "--source-ref",
    PRODUCT_SOURCE_REF,
    "--mozc-bundle",
    f"./inputs/{B0_GENERATION_NAME}",
    "--warmups",
    "5",
    "--iterations",
    "10000",
    "--top-k",
    "10",
    "--backend-name",
    "mozc-b0-adapter-soak",
)

PROTOCOL_TEST_COMMAND = (
    "hazkey-server/scripts/swift-test.sh",
    "--filter",
    PROTOCOL_STEADY_TEST,
)

SUITE_REQUIREMENTS: dict[str, dict[str, Any]] = {
    ADAPTER_SOAK_ID: {
        "minimum_conversions": 150_000,
        "minimum_cycles": 1,
        "expected_counts": {
            "helper_launches": 1,
            "server_launches": 1,
            "helper_recoveries": 0,
            "server_recoveries": 0,
            "residue_count": 0,
        },
        "native_producer_path": "<product-executable>",
        "execution_runner_path": None,
        "execution_package_path": None,
    },
    PROTOCOL_STEADY_ID: {
        "minimum_conversions": 1_500,
        "minimum_cycles": 1,
        "expected_counts": {
            "helper_launches": 1,
            "server_launches": 2,
            "helper_recoveries": 0,
            "server_recoveries": 0,
            "residue_count": 0,
        },
        "native_producer_path": PROTOCOL_BENCHMARK_SOURCE_PATH,
        "execution_runner_path": SWIFT_TEST_RUNNER_PATH,
        "execution_package_path": SWIFT_PACKAGE_ROOT,
    },
    PROTOCOL_RECOVERY_ID: {
        "minimum_conversions": 0,
        "minimum_cycles": 4,
        "expected_counts": {
            "helper_launches": None,
            "server_launches": None,
            "helper_recoveries": None,
            "server_recoveries": None,
            "residue_count": 0,
        },
        "native_producer_path": RECOVERY_SOURCE_PATH,
        "execution_runner_path": SWIFT_TEST_RUNNER_PATH,
        "execution_package_path": SWIFT_PACKAGE_ROOT,
    },
    FCITX_LONG_SOAK_ID: {
        "minimum_conversions": 150_000,
        "minimum_cycles": 1,
        "expected_counts": {
            "helper_launches": 1,
            "server_launches": 1,
            "helper_recoveries": 0,
            "server_recoveries": 0,
            "residue_count": 0,
        },
        "native_producer_path": FCITX_PRODUCER_PATH,
        "execution_runner_path": None,
        "execution_package_path": None,
    },
    FCITX_LIFECYCLE_ID: {
        "minimum_conversions": 300,
        "minimum_cycles": 3,
        "expected_counts": {
            "helper_launches": 3,
            "server_launches": 3,
            "helper_recoveries": 0,
            "server_recoveries": 0,
            "residue_count": 0,
        },
        "native_producer_path": FCITX_PRODUCER_PATH,
        "execution_runner_path": None,
        "execution_package_path": None,
    },
}


@dataclass(frozen=True)
class NativeExpectations:
    product_source_ref: str
    artifact_fingerprint: str
    product_server_size: int
    product_server_sha256: str
    artifacts: Mapping[str, tuple[int, str]]
    native_producer_sha256: str | None
    recovery_fixture_identity: str | None
    input_snapshot_fingerprint: str | None = None
    execution_runner_path: str | None = None
    execution_runner_sha256: str | None = None
    swift_package_file_count: int | None = None
    swift_package_size_bytes: int | None = None
    swift_package_fingerprint: str | None = None
    runtime_dependencies: Mapping[str, tuple[int, str]] = field(
        default_factory=dict
    )
    baseline_resource_fingerprint: str | None = None


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def load_json_bytes(data: bytes, context: str) -> Any:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"{context}: invalid UTF-8") from error
    try:
        return json.loads(text, object_pairs_hook=_without_duplicate_keys)
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


def _integer(value: Any, context: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{context} must be an integer >= {minimum}")
    return value


def _boolean(value: Any, context: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{context} must be a boolean")
    return value


def _finite_number(
    value: Any,
    context: str,
    *,
    minimum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{context} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{context} must be a finite number")
    if minimum is not None and number < minimum:
        raise ValueError(f"{context} must be >= {minimum}")
    return number


def _expect_number(
    value: float,
    expected: float,
    context: str,
) -> None:
    if not math.isclose(value, expected, rel_tol=1e-12, abs_tol=1e-12):
        raise ValueError(f"{context} must be {expected!r}, got {value!r}")


def _exact(value: dict[str, Any], fields: set[str], context: str) -> None:
    actual = set(value)
    if actual != fields:
        raise ValueError(
            f"{context} fields do not match schema; "
            f"missing={sorted(fields - actual)!r}, "
            f"unknown={sorted(actual - fields)!r}"
        )


def _expect(value: Any, expected: Any, context: str) -> None:
    if value != expected:
        raise ValueError(f"{context} must be {expected!r}, got {value!r}")


def _sha256(value: Any, context: str) -> str:
    text = _string(value, context)
    if re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", text) is None:
        raise ValueError(f"{context} must be a lowercase SHA-256 digest")
    return text if text.startswith("sha256:") else "sha256:" + text


def _read_regular(path: Path, context: str) -> bytes:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"{context} must be a non-symlink regular file")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if (before.st_dev, before.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise ValueError(f"{context} changed before it was read")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
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
    ):
        raise ValueError(f"{context} changed while it was read")
    data = b"".join(chunks)
    if len(data) != before.st_size:
        raise ValueError(f"{context} was not read completely")
    return data


def _artifact_identity(
    value: Any,
    context: str,
    expected: tuple[int, str],
) -> None:
    payload = _object(value, context)
    _exact(payload, {"path", "sha256", "size_bytes"}, context)
    _string(payload["path"], f"{context}.path")
    _expect(
        _integer(payload["size_bytes"], f"{context}.size_bytes", minimum=1),
        expected[0],
        f"{context}.size_bytes",
    )
    _expect(_sha256(payload["sha256"], f"{context}.sha256"), expected[1], f"{context}.sha256")


def recovery_fixture_identity(source_sha256: str) -> str:
    normalized = _sha256(source_sha256, "recovery source SHA-256")
    return sha256_bytes(
        canonical_json(
            {
                "domain": "hazkey.mozc-b0-recovery-fixture.v1",
                "source_path": RECOVERY_SOURCE_PATH,
                "source_sha256": normalized,
                "subchecks": [
                    {"id": item[0], "test_name": item[1], "fixture_mode": item[2]}
                    for item in RECOVERY_SUBCHECKS
                ],
            }
        )
    )


def _base_observations(
    *,
    conversions: int,
    cycles: int,
    helper_launches: int | None,
    server_launches: int | None,
    helper_recoveries: int | None,
    server_recoveries: int | None,
    residue_count: int,
) -> dict[str, int | None]:
    return {
        "exit_code": 0,
        "conversions": conversions,
        "cycles": cycles,
        "helper_launches": helper_launches,
        "server_launches": server_launches,
        "helper_recoveries": helper_recoveries,
        "server_recoveries": server_recoveries,
        "residue_count": residue_count,
    }


def _validate_adapter_abprobe(
    data: bytes,
    context: str,
    expected: NativeExpectations,
) -> dict[str, int | None]:
    run = summarize_ab_probe.load_run_bytes(data, context)
    _expect(run["schema"], summarize_ab_probe.INPUT_SCHEMA_V3, f"{context}.schema")
    _expect(run["converter_backend"], "mozc", f"{context}.converter_backend")
    _expect(run["source_ref"], expected.product_source_ref, f"{context}.source_ref")
    _expect(run["resource"]["kind"], "mozc_runtime_inputs", f"{context}.resource.kind")
    _expect(
        run["resource"]["fingerprint"],
        expected.artifact_fingerprint,
        f"{context}.resource.fingerprint",
    )
    _expect(run["corpus"], {"sha256": ADAPTER_CORPUS_SHA256, "cases": 15}, f"{context}.corpus")
    _expect(run["warmups"], 5, f"{context}.warmups")
    _expect(run["iterations"], 10_000, f"{context}.iterations")
    _expect(run["top_k"], 10, f"{context}.top_k")
    for case_id, result in run["cases"].items():
        _expect(
            result["backend_diagnostics"],
            [1, 0],
            f"{context}.{case_id}.backend_diagnostics",
        )
    conversions = len(run["cases"]) * run["iterations"]
    _expect(conversions, 150_000, f"{context}.conversions")
    return _base_observations(
        conversions=conversions,
        cycles=1,
        helper_launches=1,
        server_launches=1,
        helper_recoveries=0,
        server_recoveries=0,
        residue_count=0,
    )


def _validate_protocol_execution(value: Any, context: str) -> None:
    execution = _object(value, context)
    _exact(
        execution,
        {
            "generated_at",
            "measurement_order",
            "build_configuration",
            "toolchain",
            "operating_system",
            "kernel_release",
            "cpu_model",
            "processor_count",
            "active_processor_count",
            "physical_memory_bytes",
            "cpu_affinity_list",
            "memory_sampling",
        },
        context,
    )
    generated_at = _string(execution["generated_at"], f"{context}.generated_at")
    if re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
        r"(?:\.[0-9]{1,9})?Z",
        generated_at,
    ) is None:
        raise ValueError(f"{context}.generated_at must be an RFC 3339 UTC timestamp")
    _expect(
        execution["measurement_order"],
        ["hazkey", "mozc"],
        f"{context}.measurement_order",
    )
    _expect(
        execution["build_configuration"],
        "formal-stability",
        f"{context}.build_configuration",
    )
    _expect(execution["toolchain"], "swift-test.sh", f"{context}.toolchain")
    for field in (
        "operating_system",
        "kernel_release",
        "cpu_model",
        "cpu_affinity_list",
    ):
        _string(execution[field], f"{context}.{field}")
    processor_count = _integer(
        execution["processor_count"], f"{context}.processor_count", minimum=1
    )
    active_processor_count = _integer(
        execution["active_processor_count"],
        f"{context}.active_processor_count",
        minimum=1,
    )
    if active_processor_count > processor_count:
        raise ValueError(
            f"{context}.active_processor_count must not exceed processor_count"
        )
    _integer(
        execution["physical_memory_bytes"],
        f"{context}.physical_memory_bytes",
        minimum=1,
    )
    _expect(
        execution["memory_sampling"],
        "sequential_server_then_helper_after_warmup_and_after_measurement",
        f"{context}.memory_sampling",
    )


def _validate_latency_summary(
    value: Any,
    context: str,
    conversions: int,
) -> dict[str, float]:
    latency = _object(value, context)
    fields = {"mean", "median", "p95", "minimum", "maximum", "samples"}
    _exact(latency, fields, context)
    samples = [
        _finite_number(sample, f"{context}.samples[{index}]", minimum=0.0)
        for index, sample in enumerate(
            _array(latency["samples"], f"{context}.samples")
        )
    ]
    _expect(len(samples), conversions, f"{context}.samples length")
    if not samples or any(sample <= 0.0 for sample in samples):
        raise ValueError(f"{context}.samples must contain positive timings")
    reported = {
        field: _finite_number(latency[field], f"{context}.{field}", minimum=0.0)
        for field in fields - {"samples"}
    }
    ordered = sorted(samples)
    middle = len(ordered) // 2
    median = (
        (ordered[middle - 1] + ordered[middle]) / 2.0
        if len(ordered) % 2 == 0
        else ordered[middle]
    )
    expected = {
        "mean": sum(samples) / len(samples),
        "median": median,
        "p95": ordered[min(len(ordered) - 1, max(0, math.ceil(len(ordered) * 0.95) - 1))],
        "minimum": ordered[0],
        "maximum": ordered[-1],
    }
    for field, expected_value in expected.items():
        _expect_number(reported[field], expected_value, f"{context}.{field}")
    return reported


def _validate_memory_snapshot(value: Any, context: str) -> tuple[int, int]:
    snapshot = _object(value, context)
    _exact(snapshot, {"rss_kib", "pss_kib"}, context)
    return (
        _integer(snapshot["rss_kib"], f"{context}.rss_kib", minimum=1),
        _integer(snapshot["pss_kib"], f"{context}.pss_kib", minimum=1),
    )


def _validate_backend_memory(
    value: Any,
    context: str,
    backend: str,
) -> int:
    memory = _object(value, context)
    fields = {"server_before", "server_after", "max_observed_endpoint_total_pss_kib"}
    if backend == "mozc":
        fields.update({"helper_before", "helper_after"})
    _exact(memory, fields, context)
    _, server_before_pss = _validate_memory_snapshot(
        memory["server_before"], f"{context}.server_before"
    )
    _, server_after_pss = _validate_memory_snapshot(
        memory["server_after"], f"{context}.server_after"
    )
    helper_before_pss = helper_after_pss = 0
    if backend == "mozc":
        _, helper_before_pss = _validate_memory_snapshot(
            memory["helper_before"], f"{context}.helper_before"
        )
        _, helper_after_pss = _validate_memory_snapshot(
            memory["helper_after"], f"{context}.helper_after"
        )
    reported = _integer(
        memory["max_observed_endpoint_total_pss_kib"],
        f"{context}.max_observed_endpoint_total_pss_kib",
        minimum=1,
    )
    derived = max(
        server_before_pss + helper_before_pss,
        server_after_pss + helper_after_pss,
    )
    _expect(
        reported,
        derived,
        f"{context}.max_observed_endpoint_total_pss_kib",
    )
    return reported


def _validate_protocol_benchmark(
    data: bytes,
    context: str,
    expected: NativeExpectations,
    expected_dictionary_path: str,
) -> tuple[dict[str, int | None], set[int], set[int]]:
    payload = _object(load_json_bytes(data, context), context)
    _exact(
        payload,
        {
            "schema",
            "source_ref",
            "timing_boundary",
            "policy",
            "execution",
            "server",
            "corpus",
            "dictionary",
            "mozc_helper",
            "mozc_data",
            "backends",
            "comparison",
        },
        context,
    )
    _expect(payload["schema"], PROTOCOL_V2_BENCHMARK_SCHEMA, f"{context}.schema")
    _expect(payload["source_ref"], expected.product_source_ref, f"{context}.source_ref")
    _expect(
        payload["timing_boundary"],
        "start_conversion_one_protocol_v2_round_trip",
        f"{context}.timing_boundary",
    )
    policy = _object(payload["policy"], f"{context}.policy")
    _exact(policy, {"auto_conversion", "learning", "zenzai"}, f"{context}.policy")
    _expect(policy, {"auto_conversion": False, "learning": False, "zenzai": False}, f"{context}.policy")
    _validate_protocol_execution(payload["execution"], f"{context}.execution")
    corpus_data = _read_regular(
        Path(__file__).resolve().parents[2] / ADAPTER_CORPUS_PATH,
        "Protocol v2 benchmark corpus",
    )
    _artifact_identity(
        payload["corpus"],
        f"{context}.corpus",
        (len(corpus_data), ADAPTER_CORPUS_SHA256),
    )
    _artifact_identity(
        payload["server"],
        f"{context}.server",
        (expected.product_server_size, expected.product_server_sha256),
    )
    dictionary = _object(payload["dictionary"], f"{context}.dictionary")
    _exact(dictionary, {"path", "fingerprint"}, f"{context}.dictionary")
    _expect(
        _string(dictionary["path"], f"{context}.dictionary.path"),
        expected_dictionary_path,
        f"{context}.dictionary.path",
    )
    if expected.baseline_resource_fingerprint is None:
        raise ValueError(f"{context}: baseline dictionary fingerprint is not frozen")
    _expect(
        _sha256(dictionary["fingerprint"], f"{context}.dictionary.fingerprint"),
        expected.baseline_resource_fingerprint,
        f"{context}.dictionary.fingerprint",
    )
    for field, artifact_id in (
        ("mozc_helper", "fcitx5-grimodex-mozc-helper"),
        ("mozc_data", "mozc.data"),
    ):
        artifact = expected.artifacts.get(artifact_id)
        if artifact is None:
            raise ValueError(f"{context}: missing policy artifact {artifact_id!r}")
        _artifact_identity(payload[field], f"{context}.{field}", artifact)

    backends = _array(payload["backends"], f"{context}.backends")
    if len(backends) != 2:
        raise ValueError(f"{context}.backends must contain exactly hazkey and mozc")
    by_name: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(backends):
        backend_context = f"{context}.backends[{index}]"
        backend = _object(raw, backend_context)
        name = _string(backend.get("backend"), f"{backend_context}.backend")
        if name in by_name:
            raise ValueError(f"{context}.backends has duplicate backend {name!r}")
        by_name[name] = backend
    _expect(set(by_name), {"hazkey", "mozc"}, f"{context}.backends names")
    server_pids: set[int] = set()
    helper_pids: set[int] = set()
    mozc_conversions = 0
    summaries: dict[str, dict[str, float]] = {}
    maximum_pss: dict[str, int] = {}
    for name in ("hazkey", "mozc"):
        backend = by_name[name]
        backend_context = f"{context}.backends.{name}"
        required = {
            "backend",
            "protocol_version",
            "warmups_per_case",
            "iterations_per_case",
            "conversion_count",
            "latency_ms",
            "memory",
            "process_stability",
            "candidates",
        }
        _exact(backend, required, backend_context)
        _expect(backend["protocol_version"], 2, f"{backend_context}.protocol_version")
        _expect(
            _integer(
                backend["warmups_per_case"],
                f"{backend_context}.warmups_per_case",
                minimum=0,
            ),
            5,
            f"{backend_context}.warmups_per_case",
        )
        iterations = _integer(
            backend["iterations_per_case"],
            f"{backend_context}.iterations_per_case",
            minimum=1,
        )
        _expect(iterations, 100, f"{backend_context}.iterations_per_case")
        candidates = _array(backend["candidates"], f"{backend_context}.candidates")
        _expect(
            len(candidates),
            len(PROTOCOL_CASES),
            f"{backend_context}.candidates length",
        )
        for case_index, (raw_case, (expected_id, expected_category)) in enumerate(
            zip(candidates, PROTOCOL_CASES, strict=True)
        ):
            case_context = f"{backend_context}.candidates[{case_index}]"
            case = _object(raw_case, case_context)
            _exact(case, {"id", "category", "candidates"}, case_context)
            _expect(case["id"], expected_id, f"{case_context}.id")
            _expect(case["category"], expected_category, f"{case_context}.category")
            surfaces = _array(case["candidates"], f"{case_context}.candidates")
            if not surfaces:
                raise ValueError(f"{case_context}.candidates must not be empty")
            normalized_surfaces = [
                _string(surface, f"{case_context}.candidates[{surface_index}]")
                for surface_index, surface in enumerate(surfaces)
            ]
            if len(set(normalized_surfaces)) != len(normalized_surfaces):
                raise ValueError(f"{case_context}.candidates contains a duplicate")
        conversions = _integer(
            backend["conversion_count"], f"{backend_context}.conversion_count", minimum=1
        )
        _expect(conversions, len(candidates) * iterations, f"{backend_context}.conversion_count")
        _expect(conversions, 1_500, f"{backend_context}.conversion_count")
        summaries[name] = _validate_latency_summary(
            backend["latency_ms"], f"{backend_context}.latency_ms", conversions
        )
        maximum_pss[name] = _validate_backend_memory(
            backend["memory"], f"{backend_context}.memory", name
        )
        stability = _object(backend["process_stability"], f"{backend_context}.process_stability")
        server_pid = _integer(stability.get("server_pid"), f"{backend_context}.process_stability.server_pid", minimum=1)
        if server_pid in server_pids:
            raise ValueError(f"{context}: backend servers must be distinct launches")
        server_pids.add(server_pid)
        before = _array(stability.get("child_pids_before"), f"{backend_context}.process_stability.child_pids_before")
        after = _array(stability.get("child_pids_after"), f"{backend_context}.process_stability.child_pids_after")
        if name == "hazkey":
            _expect(stability, {"server_pid": server_pid, "child_pids_before": [], "child_pids_after": []}, f"{backend_context}.process_stability")
        else:
            expected_fields = {
                "server_pid",
                "child_pids_before",
                "child_pids_after",
                "helper_executable_path_before",
                "helper_executable_path_after",
                "helper_exited_after_server_stop",
            }
            _exact(stability, expected_fields, f"{backend_context}.process_stability")
            if len(before) != 1 or before != after:
                raise ValueError(f"{backend_context} must retain exactly one helper identity")
            _integer(before[0], f"{backend_context}.process_stability.child_pids_before[0]", minimum=1)
            helper_pids.add(before[0])
            _expect(
                _string(stability["helper_executable_path_before"], f"{backend_context}.helper_before"),
                _string(stability["helper_executable_path_after"], f"{backend_context}.helper_after"),
                f"{backend_context}.helper path",
            )
            _expect(
                _boolean(stability["helper_exited_after_server_stop"], f"{backend_context}.helper_exited"),
                True,
                f"{backend_context}.helper_exited_after_server_stop",
            )
            mozc_conversions = conversions
    if mozc_conversions < 1_500:
        raise ValueError(f"{context}.backends.mozc.conversion_count must be >= 1500")
    overlap = server_pids & helper_pids
    if overlap:
        raise ValueError(
            f"{context} server/helper process identifiers must be disjoint: "
            f"{sorted(overlap)!r}"
        )
    comparison = _object(payload["comparison"], f"{context}.comparison")
    comparison_fields = {
        "hazkey_over_mozc_mean_latency",
        "hazkey_over_mozc_median_latency",
        "hazkey_over_mozc_p95_latency",
        "mozc_pss_delta_percent",
    }
    _exact(comparison, comparison_fields, f"{context}.comparison")
    expected_comparison = {
        "hazkey_over_mozc_mean_latency": summaries["hazkey"]["mean"]
        / summaries["mozc"]["mean"],
        "hazkey_over_mozc_median_latency": summaries["hazkey"]["median"]
        / summaries["mozc"]["median"],
        "hazkey_over_mozc_p95_latency": summaries["hazkey"]["p95"]
        / summaries["mozc"]["p95"],
        "mozc_pss_delta_percent": (
            (maximum_pss["mozc"] - maximum_pss["hazkey"])
            / maximum_pss["hazkey"]
        )
        * 100.0,
    }
    for field, expected_value in expected_comparison.items():
        reported = _finite_number(comparison[field], f"{context}.comparison.{field}")
        _expect_number(reported, expected_value, f"{context}.comparison.{field}")
    return (
        _base_observations(
            conversions=mozc_conversions,
            cycles=1,
            helper_launches=1,
            server_launches=2,
            helper_recoveries=0,
            server_recoveries=0,
            residue_count=0,
        ),
        server_pids,
        helper_pids,
    )


def _binding_bytes(value: Any, root: Path, context: str) -> bytes:
    binding = _object(value, context)
    _exact(binding, {"path", "sha256"}, context)
    raw_path = Path(_string(binding["path"], f"{context}.path"))
    if (
        raw_path.is_absolute()
        or ".." in raw_path.parts
        or raw_path.name in {"", ".", ".."}
    ):
        raise ValueError(f"{context}.path must be a self-contained relative path")
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
        for component in raw_path.parts[:-1]:
            descriptor = os.open(component, directory_flags, dir_fd=descriptor)
            descriptors.append(descriptor)
        final_name = raw_path.parts[-1]
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
            f"{context}.path must not contain a symlink or non-directory ancestor"
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
    _expect(sha256_bytes(data), _sha256(binding["sha256"], f"{context}.sha256"), f"{context}.sha256")
    return data


def _process_identities(
    value: Any,
    context: str,
    expected_executable: tuple[int, str],
) -> list[tuple[int, int]]:
    identities: list[tuple[int, int]] = []
    for index, raw in enumerate(_array(value, context)):
        identity_context = f"{context}[{index}]"
        identity = _object(raw, identity_context)
        _exact(
            identity,
            {"pid", "start_time_ticks", "executable"},
            identity_context,
        )
        observed = (
            _integer(identity["pid"], f"{identity_context}.pid", minimum=1),
            _integer(
                identity["start_time_ticks"],
                f"{identity_context}.start_time_ticks",
                minimum=1,
            ),
        )
        if observed in identities:
            raise ValueError(f"{context} contains a duplicate process identity")
        executable = _object(
            identity["executable"], f"{identity_context}.executable"
        )
        _exact(
            executable,
            {"size_bytes", "sha256"},
            f"{identity_context}.executable",
        )
        _expect(
            executable["size_bytes"],
            expected_executable[0],
            f"{identity_context}.executable.size_bytes",
        )
        _expect(
            _sha256(
                executable["sha256"], f"{identity_context}.executable.sha256"
            ),
            expected_executable[1],
            f"{identity_context}.executable.sha256",
        )
        identities.append(observed)
    return identities


def _process_audit(
    value: Any,
    context: str,
    expected_server: tuple[int, str],
    expected_helper: tuple[int, str],
) -> tuple[tuple[int, int], list[tuple[int, int]], list[tuple[int, int]]]:
    audit = _object(value, context)
    _exact(
        audit,
        {
            "runner",
            "servers",
            "helpers",
            "process_group_cleanup",
            "session_cleanup",
            "residue_count",
        },
        context,
    )
    runner = _object(audit["runner"], f"{context}.runner")
    _exact(runner, {"pid", "start_time_ticks"}, f"{context}.runner")
    runner_identity = (
        _integer(runner["pid"], f"{context}.runner.pid", minimum=1),
        _integer(
            runner["start_time_ticks"],
            f"{context}.runner.start_time_ticks",
            minimum=1,
        ),
    )
    servers = _process_identities(
        audit["servers"], f"{context}.servers", expected_server
    )
    helpers = _process_identities(
        audit["helpers"], f"{context}.helpers", expected_helper
    )
    overlap = set(servers) & set(helpers)
    if overlap:
        raise ValueError(
            f"{context} server/helper process identities must be disjoint: "
            f"{sorted(overlap)!r}"
        )
    _expect(
        _boolean(audit["process_group_cleanup"], f"{context}.process_group_cleanup"),
        True,
        f"{context}.process_group_cleanup",
    )
    _expect(
        _boolean(audit["session_cleanup"], f"{context}.session_cleanup"),
        True,
        f"{context}.session_cleanup",
    )
    _expect(
        _integer(audit["residue_count"], f"{context}.residue_count"),
        0,
        f"{context}.residue_count",
    )
    return runner_identity, servers, helpers


def _validate_wrapper_producer(value: Any, context: str) -> None:
    producer = _object(value, context)
    _exact(producer, {"path", "sha256"}, context)
    _expect(producer["path"], ORCHESTRATOR_PATH, f"{context}.path")
    _expect(
        _sha256(producer["sha256"], f"{context}.sha256"),
        sha256_bytes(_read_regular(Path(__file__).resolve(), "stability orchestrator")),
        f"{context}.sha256",
    )


def _validate_wrapper_server(
    value: Any,
    context: str,
    expected: NativeExpectations,
) -> None:
    server = _object(value, context)
    _exact(server, {"size_bytes", "sha256"}, context)
    _expect(server["size_bytes"], expected.product_server_size, f"{context}.size_bytes")
    _expect(
        _sha256(server["sha256"], f"{context}.sha256"),
        expected.product_server_sha256,
        f"{context}.sha256",
    )


def _validate_wrapper_artifact(
    value: Any,
    context: str,
    expected: NativeExpectations,
) -> None:
    artifact = _object(value, context)
    _exact(artifact, {"kind", "fingerprint"}, context)
    _expect(artifact["kind"], "b0", f"{context}.kind")
    _expect(
        _sha256(artifact["fingerprint"], f"{context}.fingerprint"),
        expected.artifact_fingerprint,
        f"{context}.fingerprint",
    )


def _validate_execution_runner(
    value: Any,
    context: str,
    expected: NativeExpectations,
    native_path: Path,
) -> None:
    runner = _object(value, context)
    _exact(runner, {"path", "snapshot_path", "size_bytes", "sha256"}, context)
    if (
        expected.execution_runner_path is None
        or expected.execution_runner_sha256 is None
    ):
        raise ValueError(f"{context}: execution runner is not frozen by policy")
    _expect(runner["path"], expected.execution_runner_path, f"{context}.path")
    snapshot_path = (
        f"{SWIFT_PACKAGE_SNAPSHOT_PATH}/"
        f"{Path(expected.execution_runner_path).relative_to(SWIFT_PACKAGE_ROOT).as_posix()}"
    )
    _expect(runner["snapshot_path"], snapshot_path, f"{context}.snapshot_path")
    runner_sha = _sha256(runner["sha256"], f"{context}.sha256")
    runner_data = _binding_bytes(
        {"path": snapshot_path, "sha256": runner_sha},
        native_path.parent,
        f"{context}.snapshot",
    )
    _expect(
        _integer(runner["size_bytes"], f"{context}.size_bytes", minimum=1),
        len(runner_data),
        f"{context}.size_bytes",
    )
    _expect(
        runner_sha,
        expected.execution_runner_sha256,
        f"{context}.sha256",
    )
    _expect(
        sha256_bytes(runner_data),
        expected.execution_runner_sha256,
        f"{context}.snapshot SHA-256",
    )


def _validate_swift_package(
    value: Any,
    context: str,
    expected: NativeExpectations,
    native_path: Path,
) -> None:
    package = _object(value, context)
    _exact(
        package,
        {"path", "file_count", "size_bytes", "fingerprint", "post_run_verified"},
        context,
    )
    _expect(package["path"], SWIFT_PACKAGE_SNAPSHOT_PATH, f"{context}.path")
    _expect(
        _boolean(package["post_run_verified"], f"{context}.post_run_verified"),
        True,
        f"{context}.post_run_verified",
    )
    reported = (
        _integer(package["file_count"], f"{context}.file_count", minimum=1),
        _integer(package["size_bytes"], f"{context}.size_bytes", minimum=1),
        _sha256(package["fingerprint"], f"{context}.fingerprint"),
    )
    _expect_swift_package_identity(reported, expected, context)
    observed = _swift_package_snapshot_identity(
        native_path.parent / SWIFT_PACKAGE_SNAPSHOT_PATH,
        f"{context}.snapshot",
    )
    _expect(observed, reported, f"{context}.snapshot identity")


def _validate_adapter_soak(
    data: bytes,
    context: str,
    expected: NativeExpectations,
    native_path: Path | None,
) -> dict[str, int | None]:
    payload = _object(load_json_bytes(data, context), context)
    _exact(
        payload,
        {
            "schema",
            "producer",
            "product_source_ref",
            "product_server",
            "artifact",
            "execution",
            "raw_abprobe",
            "stderr",
        },
        context,
    )
    _expect(payload["schema"], ADAPTER_SOAK_SCHEMA, f"{context}.schema")
    _validate_wrapper_producer(payload["producer"], f"{context}.producer")
    _expect(
        payload["product_source_ref"],
        expected.product_source_ref,
        f"{context}.product_source_ref",
    )
    _validate_wrapper_server(payload["product_server"], f"{context}.product_server", expected)
    _validate_wrapper_artifact(payload["artifact"], f"{context}.artifact", expected)
    execution = _object(payload["execution"], f"{context}.execution")
    _exact(execution, {"command", "exit_code", "process_audit"}, f"{context}.execution")
    _expect(execution["command"], list(ADAPTER_PROBE_COMMAND), f"{context}.execution.command")
    _expect(execution["exit_code"], 0, f"{context}.execution.exit_code")
    runner, servers, helpers = _process_audit(
        execution["process_audit"],
        f"{context}.execution.process_audit",
        (expected.product_server_size, expected.product_server_sha256),
        expected.artifacts["fcitx5-grimodex-mozc-helper"],
    )
    _expect(len(servers), 1, f"{context}.execution.process_audit.servers length")
    _expect(len(helpers), 1, f"{context}.execution.process_audit.helpers length")
    _expect(runner, servers[0], f"{context}.execution runner/server identity")
    if native_path is None:
        raise ValueError(f"{context}: adapter validation requires the native result path")
    raw = _binding_bytes(payload["raw_abprobe"], native_path.parent, f"{context}.raw_abprobe")
    _binding_bytes(payload["stderr"], native_path.parent, f"{context}.stderr")
    observations = _validate_adapter_abprobe(raw, f"{context}.raw_abprobe", expected)
    _expect(observations["helper_launches"], len(helpers), f"{context}.helper launches")
    observations["server_launches"] = len(servers)
    observations["server_recoveries"] = 0
    observations["residue_count"] = 0
    return observations


def _validate_protocol_v2_steady(
    data: bytes,
    context: str,
    expected: NativeExpectations,
    native_path: Path | None,
) -> dict[str, int | None]:
    payload = _object(load_json_bytes(data, context), context)
    _exact(
        payload,
        {
            "schema",
            "producer",
            "product_source_ref",
            "product_server",
            "artifact",
            "benchmark_source",
            "test_runner",
            "swift_package",
            "dictionary",
            "execution",
            "benchmark",
            "stdout",
            "stderr",
        },
        context,
    )
    _expect(payload["schema"], PROTOCOL_STEADY_SCHEMA, f"{context}.schema")
    _validate_wrapper_producer(payload["producer"], f"{context}.producer")
    _expect(
        payload["product_source_ref"],
        expected.product_source_ref,
        f"{context}.product_source_ref",
    )
    _validate_wrapper_server(payload["product_server"], f"{context}.product_server", expected)
    _validate_wrapper_artifact(payload["artifact"], f"{context}.artifact", expected)
    if native_path is None:
        raise ValueError(f"{context}: protocol validation requires the native result path")
    _validate_execution_runner(
        payload["test_runner"], f"{context}.test_runner", expected, native_path
    )
    _validate_swift_package(
        payload["swift_package"], f"{context}.swift_package", expected, native_path
    )
    dictionary = _object(payload["dictionary"], f"{context}.dictionary")
    _exact(
        dictionary,
        {"path", "fingerprint_before", "fingerprint_after"},
        f"{context}.dictionary",
    )
    dictionary_path = _string(dictionary["path"], f"{context}.dictionary.path")
    if expected.baseline_resource_fingerprint is None:
        raise ValueError(f"{context}: baseline dictionary fingerprint is not frozen")
    for field in ("fingerprint_before", "fingerprint_after"):
        _expect(
            _sha256(dictionary[field], f"{context}.dictionary.{field}"),
            expected.baseline_resource_fingerprint,
            f"{context}.dictionary.{field}",
        )
    source = _object(payload["benchmark_source"], f"{context}.benchmark_source")
    _exact(
        source,
        {"path", "snapshot_path", "size_bytes", "sha256"},
        f"{context}.benchmark_source",
    )
    _expect(source["path"], PROTOCOL_BENCHMARK_SOURCE_PATH, f"{context}.benchmark_source.path")
    source_snapshot_path = (
        f"{SWIFT_PACKAGE_SNAPSHOT_PATH}/"
        f"{Path(PROTOCOL_BENCHMARK_SOURCE_PATH).relative_to(SWIFT_PACKAGE_ROOT).as_posix()}"
    )
    _expect(
        source["snapshot_path"],
        source_snapshot_path,
        f"{context}.benchmark_source.snapshot_path",
    )
    if expected.native_producer_sha256 is None:
        raise ValueError(f"{context}: Protocol v2 benchmark producer SHA-256 is not frozen")
    _expect(
        _sha256(source["sha256"], f"{context}.benchmark_source.sha256"),
        expected.native_producer_sha256,
        f"{context}.benchmark_source.sha256",
    )
    source_data = _binding_bytes(
        {
            "path": source_snapshot_path,
            "sha256": expected.native_producer_sha256,
        },
        native_path.parent,
        f"{context}.benchmark_source.snapshot",
    )
    _expect(
        _integer(
            source["size_bytes"],
            f"{context}.benchmark_source.size_bytes",
            minimum=1,
        ),
        len(source_data),
        f"{context}.benchmark_source.size_bytes",
    )
    _expect(
        sha256_bytes(source_data),
        expected.native_producer_sha256,
        f"{context}.benchmark_source.path SHA-256",
    )
    execution = _object(payload["execution"], f"{context}.execution")
    _exact(
        execution,
        {"command", "scratch_path", "exit_code", "skipped", "process_audit"},
        f"{context}.execution",
    )
    _expect(execution["command"], list(PROTOCOL_TEST_COMMAND), f"{context}.execution.command")
    _expect(execution["scratch_path"], "swift-scratch", f"{context}.execution.scratch_path")
    _expect(execution["exit_code"], 0, f"{context}.execution.exit_code")
    _expect(execution["skipped"], False, f"{context}.execution.skipped")
    _, servers, helpers = _process_audit(
        execution["process_audit"],
        f"{context}.execution.process_audit",
        (expected.product_server_size, expected.product_server_sha256),
        expected.artifacts["fcitx5-grimodex-mozc-helper"],
    )
    _expect(len(servers), 2, f"{context}.execution.process_audit.servers length")
    _expect(len(helpers), 1, f"{context}.execution.process_audit.helpers length")
    benchmark = _binding_bytes(payload["benchmark"], native_path.parent, f"{context}.benchmark")
    stdout = _binding_bytes(payload["stdout"], native_path.parent, f"{context}.stdout")
    stderr = _binding_bytes(payload["stderr"], native_path.parent, f"{context}.stderr")
    combined = (stdout + b"\n" + stderr).decode("utf-8", errors="replace")
    test_method = PROTOCOL_STEADY_TEST.rsplit("/", 1)[-1]
    named_lines = [line for line in combined.splitlines() if test_method in line]
    if not any(re.search(r"\bpassed\b", line) for line in named_lines) or any(
        re.search(r"\bskipped\b", line) for line in named_lines
    ):
        raise ValueError(
            f"{context}: bound logs must name the exact Protocol steady test as "
            "passed and not skipped"
        )
    observations, reported_servers, reported_helpers = _validate_protocol_benchmark(
        benchmark,
        f"{context}.benchmark",
        expected,
        dictionary_path,
    )
    _expect({item[0] for item in servers}, reported_servers, f"{context}.server process identities")
    _expect({item[0] for item in helpers}, reported_helpers, f"{context}.helper process identities")
    observations["server_launches"] = len(servers)
    observations["helper_launches"] = len(helpers)
    observations["server_recoveries"] = 0
    observations["helper_recoveries"] = 0
    observations["residue_count"] = 0
    return observations


def _validate_recovery(
    data: bytes,
    context: str,
    expected: NativeExpectations,
    native_path: Path | None,
) -> dict[str, int | None]:
    payload = _object(load_json_bytes(data, context), context)
    _exact(
        payload,
        {
            "schema",
            "producer",
            "product_source_ref",
            "product_server",
            "artifact",
            "fixture_source",
            "test_runner",
            "swift_package",
            "runtime_dependencies",
            "scratch_path",
            "subchecks",
            "residue_count",
        },
        context,
    )
    _expect(payload["schema"], RECOVERY_SCHEMA, f"{context}.schema")
    _expect(payload["product_source_ref"], expected.product_source_ref, f"{context}.product_source_ref")
    producer = _object(payload["producer"], f"{context}.producer")
    _exact(producer, {"path", "sha256"}, f"{context}.producer")
    _expect(producer["path"], ORCHESTRATOR_PATH, f"{context}.producer.path")
    _expect(
        _sha256(producer["sha256"], f"{context}.producer.sha256"),
        sha256_bytes(_read_regular(Path(__file__).resolve(), "stability orchestrator")),
        f"{context}.producer.sha256",
    )
    server = _object(payload["product_server"], f"{context}.product_server")
    _exact(server, {"size_bytes", "sha256"}, f"{context}.product_server")
    _expect(
        _integer(
            server["size_bytes"], f"{context}.product_server.size_bytes", minimum=1
        ),
        expected.product_server_size,
        f"{context}.product_server.size_bytes",
    )
    _expect(_sha256(server["sha256"], f"{context}.product_server.sha256"), expected.product_server_sha256, f"{context}.product_server.sha256")
    if native_path is None:
        raise ValueError(f"{context}: recovery validation requires the native result path")
    _validate_execution_runner(
        payload["test_runner"], f"{context}.test_runner", expected, native_path
    )
    _validate_swift_package(
        payload["swift_package"], f"{context}.swift_package", expected, native_path
    )
    _expect(payload["scratch_path"], "swift-scratch", f"{context}.scratch_path")
    runtime_context = f"{context}.runtime_dependencies"
    runtime = _object(payload["runtime_dependencies"], runtime_context)
    _exact(runtime, {"path", "files", "post_run_verified"}, runtime_context)
    _expect(runtime["path"], "runtime-lib", f"{runtime_context}.path")
    _expect(
        _boolean(
            runtime["post_run_verified"],
            f"{runtime_context}.post_run_verified",
        ),
        True,
        f"{runtime_context}.post_run_verified",
    )
    runtime_files = _array(runtime["files"], f"{runtime_context}.files")
    _expect(
        len(runtime_files),
        len(run_mozc_b0_measurement.RUNTIME_DEPENDENCY_FILENAMES),
        f"{runtime_context}.files length",
    )
    for index, (raw, name) in enumerate(
        zip(
            runtime_files,
            run_mozc_b0_measurement.RUNTIME_DEPENDENCY_FILENAMES,
            strict=True,
        )
    ):
        file_context = f"{runtime_context}.files[{index}]"
        item = _object(raw, file_context)
        _exact(item, {"path", "size_bytes", "sha256"}, file_context)
        _expect(item["path"], name, f"{file_context}.path")
        identity = expected.runtime_dependencies.get(name)
        if identity is None:
            raise ValueError(f"{file_context}: runtime dependency is not frozen")
        _expect(
            _integer(item["size_bytes"], f"{file_context}.size_bytes", minimum=1),
            identity[0],
            f"{file_context}.size_bytes",
        )
        digest = _sha256(item["sha256"], f"{file_context}.sha256")
        _expect(digest, identity[1], f"{file_context}.sha256")
        data = _binding_bytes(
            {"path": f"runtime-lib/{name}", "sha256": digest},
            native_path.parent,
            file_context,
        )
        _expect(len(data), identity[0], f"{file_context} bound size")
    artifact = _object(payload["artifact"], f"{context}.artifact")
    _exact(artifact, {"kind", "fixture_identity"}, f"{context}.artifact")
    _expect(artifact["kind"], "fault-fixture", f"{context}.artifact.kind")
    _expect(
        _sha256(artifact["fixture_identity"], f"{context}.artifact.fixture_identity"),
        expected.recovery_fixture_identity,
        f"{context}.artifact.fixture_identity",
    )
    source = _object(payload["fixture_source"], f"{context}.fixture_source")
    _exact(
        source,
        {"path", "snapshot_path", "size_bytes", "sha256"},
        f"{context}.fixture_source",
    )
    _expect(source["path"], RECOVERY_SOURCE_PATH, f"{context}.fixture_source.path")
    source_snapshot_path = (
        f"{SWIFT_PACKAGE_SNAPSHOT_PATH}/"
        f"{Path(RECOVERY_SOURCE_PATH).relative_to(SWIFT_PACKAGE_ROOT).as_posix()}"
    )
    _expect(
        source["snapshot_path"],
        source_snapshot_path,
        f"{context}.fixture_source.snapshot_path",
    )
    source_sha = _sha256(source["sha256"], f"{context}.fixture_source.sha256")
    if expected.native_producer_sha256 is None:
        raise ValueError(f"{context}: recovery fixture source SHA-256 is not frozen")
    _expect(
        source_sha,
        expected.native_producer_sha256,
        f"{context}.fixture_source.sha256",
    )
    source_data = _binding_bytes(
        {"path": source_snapshot_path, "sha256": source_sha},
        native_path.parent,
        f"{context}.fixture_source.snapshot",
    )
    _expect(sha256_bytes(source_data), source_sha, f"{context}.fixture_source.sha256")
    _expect(
        _integer(
            source["size_bytes"], f"{context}.fixture_source.size_bytes", minimum=1
        ),
        len(source_data),
        f"{context}.fixture_source.size_bytes",
    )
    _expect(recovery_fixture_identity(source_sha), expected.recovery_fixture_identity, f"{context}.fixture_source identity")
    _expect(
        _integer(payload["residue_count"], f"{context}.residue_count"),
        0,
        f"{context}.residue_count",
    )
    subchecks = _array(payload["subchecks"], f"{context}.subchecks")
    _expect(len(subchecks), len(RECOVERY_SUBCHECKS), f"{context}.subchecks length")
    root = native_path.parent if native_path is not None else None
    for index, (raw, specification) in enumerate(zip(subchecks, RECOVERY_SUBCHECKS, strict=True)):
        check_context = f"{context}.subchecks[{index}]"
        check = _object(raw, check_context)
        _exact(
            check,
            {
                "id",
                "test_name",
                "fixture_mode",
                "command",
                "exit_code",
                "skipped",
                "cleanup",
                "stdout",
                "stderr",
            },
            check_context,
        )
        check_id, test_name, fixture_mode = specification
        _expect(check["id"], check_id, f"{check_context}.id")
        _expect(check["test_name"], test_name, f"{check_context}.test_name")
        _expect(check["fixture_mode"], fixture_mode, f"{check_context}.fixture_mode")
        _expect(
            check["command"],
            ["hazkey-server/scripts/swift-test.sh", "--filter", test_name],
            f"{check_context}.command",
        )
        _expect(
            _integer(check["exit_code"], f"{check_context}.exit_code"),
            0,
            f"{check_context}.exit_code",
        )
        _expect(
            _boolean(check["skipped"], f"{check_context}.skipped"),
            False,
            f"{check_context}.skipped",
        )
        cleanup = _object(check["cleanup"], f"{check_context}.cleanup")
        _exact(
            cleanup,
            {"process_group", "session", "residue_count"},
            f"{check_context}.cleanup",
        )
        _expect(
            _boolean(
                cleanup["process_group"], f"{check_context}.cleanup.process_group"
            ),
            True,
            f"{check_context}.cleanup.process_group",
        )
        _expect(
            _boolean(cleanup["session"], f"{check_context}.cleanup.session"),
            True,
            f"{check_context}.cleanup.session",
        )
        _expect(
            _integer(cleanup["residue_count"], f"{check_context}.cleanup.residue_count"),
            0,
            f"{check_context}.cleanup.residue_count",
        )
        if root is None:
            raise ValueError(f"{context}: recovery validation requires the native result path")
        stdout = _binding_bytes(check["stdout"], root, f"{check_context}.stdout")
        stderr = _binding_bytes(check["stderr"], root, f"{check_context}.stderr")
        combined = (stdout + b"\n" + stderr).decode("utf-8", errors="replace")
        test_method = test_name.rsplit("/", 1)[-1]
        named_lines = [
            line for line in combined.splitlines() if test_method in line
        ]
        named_pass = any(
            re.search(r"\bpassed\b", line) for line in named_lines
        )
        named_skipped = any(
            re.search(r"\bskipped\b", line) for line in named_lines
        )
        if not named_pass or named_skipped:
            raise ValueError(
                f"{check_context}: bound logs must name the exact test as passed "
                "and not skipped"
            )
    return _base_observations(
        conversions=0,
        cycles=4,
        helper_launches=None,
        server_launches=None,
        helper_recoveries=None,
        server_recoveries=None,
        residue_count=0,
    )


def _fcitx_file_binding(
    value: Any,
    context: str,
    *,
    require_mode: bool = False,
) -> dict[str, Any]:
    binding = _object(value, context)
    fields = {"path", "size", "sha256"}
    if require_mode:
        fields.add("mode")
    _exact(binding, fields, context)
    normalized: dict[str, Any] = {
        "path": _string(binding["path"], f"{context}.path"),
        "size": _integer(binding["size"], f"{context}.size"),
        "sha256": _sha256(binding["sha256"], f"{context}.sha256"),
    }
    if require_mode:
        mode = _integer(binding["mode"], f"{context}.mode")
        if mode > 0o7777:
            raise ValueError(f"{context}.mode must be <= 0o7777")
        normalized["mode"] = mode
    return normalized


def _fcitx_snapshot_fingerprint(
    directories: list[str],
    entries: list[dict[str, Any]],
) -> str:
    digest = hashlib.sha256()
    digest.update(FCITX_SNAPSHOT_FINGERPRINT_DOMAIN.encode("utf-8") + b"\0")
    for directory in directories:
        encoded = directory.encode("utf-8")
        digest.update(b"\x02")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    for entry in entries:
        relative = entry["relative_path"].encode("utf-8")
        input_id = entry["input_id"].encode("utf-8")
        digest.update(b"\x01")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(input_id).to_bytes(8, "big"))
        digest.update(input_id)
        digest.update(entry["size"].to_bytes(8, "big"))
        digest.update(entry["mode"].to_bytes(4, "big"))
        digest.update(bytes.fromhex(entry["sha256"].removeprefix("sha256:")))
    return "sha256:" + digest.hexdigest()


def _fcitx_retained_evidence_root_name(
    native_filename: str, fingerprint: str
) -> str:
    normalized = _sha256(fingerprint, "Fcitx retained snapshot fingerprint")
    filename_identity = hashlib.sha256(native_filename.encode("utf-8")).hexdigest()[:16]
    return (
        f"{FCITX_RETAINED_EVIDENCE_ROOT_PREFIX}-{filename_identity}-"
        f"sha256-{normalized.removeprefix('sha256:')}"
    )


def _read_fcitx_snapshot_directory(
    descriptor: int,
    prefix: tuple[str, ...],
    context: str,
) -> tuple[set[str], dict[str, dict[str, Any]]]:
    before_directory = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(before_directory.st_mode)
        or stat.S_IMODE(before_directory.st_mode) != 0o555
    ):
        raise ValueError(f"{context} must be a mode-0555 directory")
    try:
        names = os.listdir(descriptor)
    except OSError as error:
        raise ValueError(f"{context} could not be enumerated") from error
    if len(names) != len(set(names)):
        raise ValueError(f"{context} contains duplicate entries")
    directories = {"/".join(prefix) if prefix else "."}
    files: dict[str, dict[str, Any]] = {}
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
    for name in sorted(names, key=os.fsencode):
        if name in {"", ".", ".."} or "/" in name:
            raise ValueError(f"{context} contains an invalid entry name")
        relative = "/".join((*prefix, name))
        entry_context = f"{context}/{name}"
        try:
            metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except OSError as error:
            raise ValueError(f"{entry_context} could not be inspected") from error
        if stat.S_ISDIR(metadata.st_mode):
            try:
                child = os.open(name, directory_flags, dir_fd=descriptor)
            except OSError as error:
                raise ValueError(
                    f"{entry_context} must be a non-symlink directory"
                ) from error
            try:
                child_directories, child_files = _read_fcitx_snapshot_directory(
                    child, (*prefix, name), entry_context
                )
            finally:
                os.close(child)
            directories.update(child_directories)
            files.update(child_files)
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"{entry_context} must be a non-symlink regular file")
        try:
            file_descriptor = os.open(name, file_flags, dir_fd=descriptor)
        except OSError as error:
            raise ValueError(
                f"{entry_context} must be a non-symlink regular file"
            ) from error
        try:
            before = os.fstat(file_descriptor)
            digest = hashlib.sha256()
            size = 0
            while True:
                chunk = os.read(file_descriptor, 1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                digest.update(chunk)
            after = os.fstat(file_descriptor)
            final = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        finally:
            os.close(file_descriptor)
        if (
            _stable_metadata(before) != _stable_metadata(after)
            or _stable_metadata(final) != _stable_metadata(after)
            or size != before.st_size
            or before.st_nlink != 1
        ):
            raise ValueError(f"{entry_context} changed while it was read")
        files[relative] = {
            "size": size,
            "sha256": "sha256:" + digest.hexdigest(),
            "mode": stat.S_IMODE(before.st_mode),
        }
    after_directory = os.fstat(descriptor)
    try:
        final_names = os.listdir(descriptor)
    except OSError as error:
        raise ValueError(f"{context} could not be re-enumerated") from error
    if (
        _stable_metadata(before_directory) != _stable_metadata(after_directory)
        or sorted(names, key=os.fsencode) != sorted(final_names, key=os.fsencode)
    ):
        raise ValueError(f"{context} changed while it was read")
    return directories, files


def _runtime_fingerprint_from_hashes(files: Mapping[str, str]) -> str:
    digest = hashlib.sha256()
    digest.update(b"hazkey.mozc-runtime-fingerprint.v1\0")
    for name in sorted(files, key=lambda value: value.encode("utf-8")):
        encoded = name.encode("utf-8")
        digest.update(b"\x01")
        digest.update(len(encoded).to_bytes(8, byteorder="big"))
        digest.update(encoded)
        digest.update(bytes.fromhex(files[name].removeprefix("sha256:")))
    return "sha256:" + digest.hexdigest()


def _dictionary_fingerprint_from_snapshot(
    entries: Mapping[str, dict[str, Any]],
) -> str:
    files = {
        relative.removeprefix("dictionary/"): entry["sha256"]
        for relative, entry in entries.items()
        if relative.startswith("dictionary/") and entry["input_id"] == "dictionary"
    }
    if not files:
        raise ValueError("Fcitx input snapshot must contain dictionary files")
    digest = hashlib.sha256()
    digest.update(b"hazkey.dictionary-fingerprint.v1\0")
    for relative in sorted(files, key=lambda item: item.encode("utf-8")):
        encoded = relative.encode("utf-8")
        digest.update(b"\x01")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(bytes.fromhex(files[relative].removeprefix("sha256:")))
    return "sha256:" + digest.hexdigest()


def _open_absolute_directory_no_symlinks(path: Path, context: str) -> int:
    if not path.is_absolute():
        raise ValueError(f"{context} must be absolute")
    flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = -1
    try:
        descriptor = os.open(path.anchor, flags)
        for component in path.parts[1:]:
            if component in {"", ".", ".."}:
                raise ValueError(f"{context} contains an unsafe component")
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except ValueError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
        raise ValueError(
            f"{context} must not contain a symlink or non-directory component"
        ) from error


def _stable_metadata(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _dictionary_entries_from_descriptor(
    directory_descriptor: int,
    prefix: tuple[str, ...],
    context: str,
) -> list[tuple[bytes, bytes]]:
    before_directory = os.fstat(directory_descriptor)
    if not stat.S_ISDIR(before_directory.st_mode):
        raise ValueError(f"{context} must be a directory")
    try:
        names = os.listdir(directory_descriptor)
    except OSError as error:
        raise ValueError(f"{context} could not be enumerated") from error
    if len(names) != len(set(names)):
        raise ValueError(f"{context} contains duplicate directory entries")
    entries: list[tuple[bytes, bytes]] = []
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
    for name in sorted(names, key=lambda item: os.fsencode(item)):
        if name in {"", ".", ".."} or "/" in name:
            raise ValueError(f"{context} contains an invalid entry name")
        try:
            name.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError(f"{context} entry names must be valid UTF-8") from error
        entry_context = f"{context}/{name}"
        try:
            metadata = os.stat(
                name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
        except OSError as error:
            raise ValueError(f"{entry_context} could not be inspected") from error
        if stat.S_ISDIR(metadata.st_mode):
            try:
                child_descriptor = os.open(
                    name,
                    directory_flags,
                    dir_fd=directory_descriptor,
                )
            except OSError as error:
                raise ValueError(
                    f"{entry_context} must be a non-symlink directory"
                ) from error
            try:
                entries.extend(
                    _dictionary_entries_from_descriptor(
                        child_descriptor,
                        (*prefix, name),
                        entry_context,
                    )
                )
            finally:
                os.close(child_descriptor)
            final = os.stat(
                name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISDIR(final.st_mode)
                or _stable_metadata(final) != _stable_metadata(metadata)
            ):
                raise ValueError(f"{entry_context} changed while it was fingerprinted")
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(
                f"{entry_context} must be a non-symlink regular file or directory"
            )
        try:
            file_descriptor = os.open(
                name,
                file_flags,
                dir_fd=directory_descriptor,
            )
        except OSError as error:
            raise ValueError(
                f"{entry_context} must be a non-symlink regular file"
            ) from error
        try:
            before = os.fstat(file_descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or (before.st_dev, before.st_ino) != (metadata.st_dev, metadata.st_ino)
            ):
                raise ValueError(f"{entry_context} changed before it was read")
            file_digest = hashlib.sha256()
            size = 0
            while True:
                chunk = os.read(file_descriptor, 1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                file_digest.update(chunk)
            after = os.fstat(file_descriptor)
        finally:
            os.close(file_descriptor)
        final = os.stat(
            name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if (
            _stable_metadata(before) != _stable_metadata(after)
            or _stable_metadata(final) != _stable_metadata(after)
            or not stat.S_ISREG(final.st_mode)
            or size != before.st_size
        ):
            raise ValueError(f"{entry_context} changed while it was fingerprinted")
        relative = "/".join((*prefix, name)).encode("utf-8")
        entries.append((relative, file_digest.digest()))
    after_directory = os.fstat(directory_descriptor)
    try:
        final_names = os.listdir(directory_descriptor)
    except OSError as error:
        raise ValueError(f"{context} could not be re-enumerated") from error
    if (
        _stable_metadata(before_directory) != _stable_metadata(after_directory)
        or sorted(names, key=os.fsencode) != sorted(final_names, key=os.fsencode)
    ):
        raise ValueError(f"{context} changed while it was fingerprinted")
    return entries


def _dictionary_fingerprint(directory: Path, context: str) -> str:
    flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(directory, flags)
    except OSError as error:
        raise ValueError(f"{context} must be a non-symlink directory") from error
    try:
        entries = _dictionary_entries_from_descriptor(descriptor, (), context)
    finally:
        os.close(descriptor)
    digest = hashlib.sha256()
    digest.update(b"hazkey.dictionary-fingerprint.v1\0")
    for relative, file_digest in sorted(entries, key=lambda item: item[0]):
        digest.update(b"\x01")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(file_digest)
    return "sha256:" + digest.hexdigest()


def _validate_fcitx_snapshot(
    value: Any,
    context: str,
    native_path: Path,
    expected_fingerprint: str,
) -> tuple[Path, dict[str, dict[str, Any]], list[str]]:
    snapshot = _object(value, context)
    _exact(
        snapshot,
        {"schema", "root", "fingerprint", "directories", "entries", "integrity"},
        context,
    )
    _expect(snapshot["schema"], FCITX_SNAPSHOT_SCHEMA, f"{context}.schema")
    root = Path(_string(snapshot["root"], f"{context}.root"))
    if not root.is_absolute():
        raise ValueError(f"{context}.root must be absolute")
    fingerprint = _sha256(snapshot["fingerprint"], f"{context}.fingerprint")
    _expect(fingerprint, expected_fingerprint, f"{context}.fingerprint")
    retained_root = native_path.parent / _fcitx_retained_evidence_root_name(
        native_path.name, fingerprint
    )
    _expect(root, retained_root / "evidence-inputs", f"{context}.root")
    directories = [
        _string(item, f"{context}.directories[{index}]")
        for index, item in enumerate(_array(snapshot["directories"], f"{context}.directories"))
    ]
    if directories != sorted(set(directories), key=lambda item: item.encode("utf-8")):
        raise ValueError(f"{context}.directories must be unique and bytewise sorted")
    if not directories or directories[0] != ".":
        raise ValueError(f"{context}.directories must begin with '.'")
    for index, directory in enumerate(directories):
        if directory == ".":
            continue
        path = Path(directory)
        if path.is_absolute() or ".." in path.parts or path.name in {"", ".", ".."}:
            raise ValueError(f"{context}.directories[{index}] is not self-contained")

    entries: list[dict[str, Any]] = []
    by_path: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(_array(snapshot["entries"], f"{context}.entries")):
        entry_context = f"{context}.entries[{index}]"
        entry = _object(raw, entry_context)
        _exact(
            entry,
            {"input_id", "source_path", "relative_path", "size", "sha256", "mode"},
            entry_context,
        )
        relative = _string(entry["relative_path"], f"{entry_context}.relative_path")
        relative_path = Path(relative)
        if (
            relative_path.is_absolute()
            or ".." in relative_path.parts
            or relative_path.name in {"", ".", ".."}
        ):
            raise ValueError(f"{entry_context}.relative_path is not self-contained")
        if relative in by_path:
            raise ValueError(f"{context}.entries has duplicate path {relative!r}")
        source_path = Path(_string(entry["source_path"], f"{entry_context}.source_path"))
        if not source_path.is_absolute():
            raise ValueError(f"{entry_context}.source_path must be absolute")
        mode = _integer(entry["mode"], f"{entry_context}.mode")
        if mode > 0o7777:
            raise ValueError(f"{entry_context}.mode must be <= 0o7777")
        normalized = {
            "input_id": _string(entry["input_id"], f"{entry_context}.input_id"),
            "source_path": str(source_path),
            "relative_path": relative,
            "size": _integer(entry["size"], f"{entry_context}.size"),
            "sha256": _sha256(entry["sha256"], f"{entry_context}.sha256"),
            "mode": mode,
        }
        parent = relative_path.parent.as_posix()
        if parent not in directories:
            raise ValueError(f"{entry_context} has an undeclared parent directory")
        by_path[relative] = normalized
        entries.append(normalized)
    if [entry["relative_path"] for entry in entries] != sorted(
        by_path, key=lambda item: item.encode("utf-8")
    ):
        raise ValueError(f"{context}.entries must be bytewise sorted")

    integrity = _object(snapshot["integrity"], f"{context}.integrity")
    _exact(integrity, {"post_run_verified", "entry_count"}, f"{context}.integrity")
    _expect(
        _boolean(integrity["post_run_verified"], f"{context}.integrity.post_run_verified"),
        True,
        f"{context}.integrity.post_run_verified",
    )
    _expect(
        _integer(integrity["entry_count"], f"{context}.integrity.entry_count"),
        len(entries),
        f"{context}.integrity.entry_count",
    )
    _expect(
        fingerprint,
        _fcitx_snapshot_fingerprint(directories, entries),
        f"{context}.fingerprint",
    )
    directory_flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    retained_descriptor = _open_absolute_directory_no_symlinks(
        retained_root,
        f"{context} retained evidence root",
    )
    try:
        retained_before = os.fstat(retained_descriptor)
        if (
            not stat.S_ISDIR(retained_before.st_mode)
            or retained_before.st_uid != os.getuid()
            or stat.S_IMODE(retained_before.st_mode) != 0o500
        ):
            raise ValueError(f"{context} retained evidence root must be mode-0500")
        retained_names = os.listdir(retained_descriptor)
        _expect(
            set(retained_names),
            {"evidence-inputs", "mozc-runtime"},
            f"{context} retained evidence file set",
        )
        try:
            descriptor = os.open(
                "evidence-inputs",
                directory_flags,
                dir_fd=retained_descriptor,
            )
        except OSError as error:
            raise ValueError(
                f"{context}.root must be a non-symlink directory"
            ) from error
        try:
            actual_directories, actual_files = _read_fcitx_snapshot_directory(
                descriptor, (), f"{context}.root"
            )
        finally:
            os.close(descriptor)
        retained_after = os.fstat(retained_descriptor)
        final_retained_names = os.listdir(retained_descriptor)
        if (
            _stable_metadata(retained_before)
            != _stable_metadata(retained_after)
            or sorted(retained_names, key=os.fsencode)
            != sorted(final_retained_names, key=os.fsencode)
        ):
            raise ValueError(f"{context} retained evidence root changed while read")
    finally:
        os.close(retained_descriptor)
    _expect(
        sorted(actual_directories, key=lambda item: item.encode("utf-8")),
        directories,
        f"{context} retained directory set",
    )
    _expect(set(actual_files), set(by_path), f"{context} retained file set")
    for relative, entry in by_path.items():
        _expect(
            actual_files[relative],
            {
                "size": entry["size"],
                "sha256": entry["sha256"],
                "mode": entry["mode"],
            },
            f"{context} retained file {relative}",
        )
    return root, by_path, directories


def _validate_fcitx_retained_runtime(
    retained_root: Path,
    prepared_content_address: str,
    runtime_artifacts: Mapping[str, dict[str, Any]],
    snapshot_directories: list[str],
    snapshot_entries: Mapping[str, dict[str, Any]],
    context: str,
) -> None:
    directory_flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    root_descriptor = _open_absolute_directory_no_symlinks(
        retained_root,
        f"{context} retained root",
    )
    try:
        retained_before = os.fstat(root_descriptor)
        if (
            not stat.S_ISDIR(retained_before.st_mode)
            or retained_before.st_uid != os.getuid()
            or stat.S_IMODE(retained_before.st_mode) != 0o500
        ):
            raise ValueError(f"{context} retained root must be mode-0500")
        retained_names = os.listdir(root_descriptor)
        _expect(
            set(retained_names),
            {"evidence-inputs", "mozc-runtime"},
            f"{context} retained root file set",
        )
        try:
            snapshot_descriptor = os.open(
                "evidence-inputs", directory_flags, dir_fd=root_descriptor
            )
        except OSError as error:
            raise ValueError(
                f"{context} retained snapshot must be a non-symlink directory"
            ) from error
        try:
            rebound_directories, rebound_files = _read_fcitx_snapshot_directory(
                snapshot_descriptor,
                (),
                f"{context}.evidence-inputs",
            )
        finally:
            os.close(snapshot_descriptor)
        try:
            runtime_descriptor = os.open(
                "mozc-runtime", directory_flags, dir_fd=root_descriptor
            )
        except OSError as error:
            raise ValueError(
                f"{context} retained runtime must be a non-symlink directory"
            ) from error
        try:
            directories, files = _read_fcitx_snapshot_directory(
                runtime_descriptor, (), f"{context}.mozc-runtime"
            )
        finally:
            os.close(runtime_descriptor)
        retained_after = os.fstat(root_descriptor)
        final_retained_names = os.listdir(root_descriptor)
        if (
            _stable_metadata(retained_before)
            != _stable_metadata(retained_after)
            or sorted(retained_names, key=os.fsencode)
            != sorted(final_retained_names, key=os.fsencode)
        ):
            raise ValueError(f"{context} retained root changed while read")
    finally:
        os.close(root_descriptor)
    _expect(
        rebound_directories,
        set(snapshot_directories),
        f"{context} rebound snapshot directories",
    )
    _expect(
        rebound_files,
        {
            relative: {
                "size": entry["size"],
                "sha256": entry["sha256"],
                "mode": entry["mode"],
            }
            for relative, entry in snapshot_entries.items()
        },
        f"{context} rebound snapshot files",
    )
    _expect(
        directories,
        {".", prepared_content_address},
        f"{context} retained runtime directories",
    )
    expected_files = {
        f"{prepared_content_address}/fcitx5-grimodex-mozc-helper": {
            "size": runtime_artifacts["mozc_helper"]["size"],
            "sha256": runtime_artifacts["mozc_helper"]["sha256"],
            "mode": 0o555,
        },
        f"{prepared_content_address}/mozc.data": {
            "size": runtime_artifacts["mozc_data"]["size"],
            "sha256": runtime_artifacts["mozc_data"]["sha256"],
            "mode": 0o444,
        },
    }
    _expect(files, expected_files, f"{context} retained runtime files")


def _validate_process_payload(
    value: Any,
    context: str,
    *,
    expected_executable: str,
) -> tuple[int, int, tuple[tuple[int, str], ...], int]:
    payload = _object(value, context)
    _exact(payload, {"launch_count", "recovery_count", "launches", "observed_identities", "cleanup_ok"}, context)
    launch_count = _integer(payload["launch_count"], f"{context}.launch_count")
    recovery_count = _integer(payload["recovery_count"], f"{context}.recovery_count")
    launches: list[tuple[int, str]] = []
    for index, raw in enumerate(_array(payload["launches"], f"{context}.launches")):
        launch_context = f"{context}.launches[{index}]"
        launch = _object(raw, launch_context)
        _exact(launch, {"pid", "start_time"}, launch_context)
        start_time = _string(launch["start_time"], f"{launch_context}.start_time")
        if re.fullmatch(r"[1-9][0-9]*", start_time) is None:
            raise ValueError(f"{launch_context}.start_time must be positive decimal ticks")
        identity = (
            _integer(launch["pid"], f"{launch_context}.pid", minimum=1),
            start_time,
        )
        if identity in launches:
            raise ValueError(f"{context}.launches contains a duplicate identity")
        launches.append(identity)
    _expect(len(launches), launch_count, f"{context}.launches length")

    identities: list[tuple[int, str]] = []
    session_ids: set[int] = set()
    for index, raw in enumerate(
        _array(payload["observed_identities"], f"{context}.observed_identities")
    ):
        identity_context = f"{context}.observed_identities[{index}]"
        observed = _object(raw, identity_context)
        _exact(
            observed,
            {"pid", "start_time", "executable", "process_group", "session_id"},
            identity_context,
        )
        start_time = _string(observed["start_time"], f"{identity_context}.start_time")
        if re.fullmatch(r"[1-9][0-9]*", start_time) is None:
            raise ValueError(f"{identity_context}.start_time must be positive decimal ticks")
        identity = (
            _integer(observed["pid"], f"{identity_context}.pid", minimum=1),
            start_time,
        )
        if identity in identities:
            raise ValueError(f"{context}.observed_identities contains a duplicate")
        identities.append(identity)
        _expect(
            _string(observed["executable"], f"{identity_context}.executable"),
            expected_executable,
            f"{identity_context}.executable",
        )
        _integer(observed["process_group"], f"{identity_context}.process_group", minimum=1)
        session_ids.add(
            _integer(observed["session_id"], f"{identity_context}.session_id", minimum=1)
        )
    _expect(len(identities), launch_count, f"{context}.observed_identities length")
    _expect(set(identities), set(launches), f"{context} launch/identity correspondence")
    _expect(
        _boolean(payload["cleanup_ok"], f"{context}.cleanup_ok"),
        True,
        f"{context}.cleanup_ok",
    )
    if launch_count and len(session_ids) != 1:
        raise ValueError(f"{context} identities must belong to one session")
    session_id = next(iter(session_ids)) if session_ids else 0
    return launch_count, recovery_count, tuple(identities), session_id


def _validate_fcitx(
    suite_id: str,
    data: bytes,
    context: str,
    expected: NativeExpectations,
    native_path: Path | None,
) -> dict[str, int | None]:
    if native_path is None:
        raise ValueError(f"{context}: Fcitx validation requires the native result path")
    payload = _object(load_json_bytes(data, context), context)
    _exact(
        payload,
        {
            "schema",
            "version",
            "exit_code",
            "producer",
            "source",
            "product_source_ref",
            "product_server",
            "artifact_fingerprint",
            "command",
            "artifacts",
            "input_snapshot",
            "runtime_integrity",
            "configuration",
            "conversions",
            "cycles",
            "helper_launches",
            "server_launches",
            "helper_recoveries",
            "server_recoveries",
            "residue_count",
            "cycle_results",
        },
        context,
    )
    _expect(payload["schema"], FCITX_SCHEMA, f"{context}.schema")
    _expect(_integer(payload["version"], f"{context}.version"), 1, f"{context}.version")
    _expect(_integer(payload["exit_code"], f"{context}.exit_code"), 0, f"{context}.exit_code")
    _expect(payload["product_source_ref"], expected.product_source_ref, f"{context}.product_source_ref")
    _expect(_sha256(payload["artifact_fingerprint"], f"{context}.artifact_fingerprint"), expected.artifact_fingerprint, f"{context}.artifact_fingerprint")
    server = _object(payload["product_server"], f"{context}.product_server")
    _exact(server, {"sha256", "size"}, f"{context}.product_server")
    _expect(_sha256(server["sha256"], f"{context}.product_server.sha256"), expected.product_server_sha256, f"{context}.product_server.sha256")
    _expect(
        _integer(server["size"], f"{context}.product_server.size", minimum=1),
        expected.product_server_size,
        f"{context}.product_server.size",
    )
    producer = _object(payload["producer"], f"{context}.producer")
    _exact(producer, {"path", "size", "sha256"}, f"{context}.producer")
    if expected.native_producer_sha256 is None:
        raise ValueError(f"{context}: Fcitx native producer SHA-256 is not frozen")
    _expect(_sha256(producer["sha256"], f"{context}.producer.sha256"), expected.native_producer_sha256, f"{context}.producer.sha256")
    producer_path = _string(producer["path"], f"{context}.producer.path")
    if not producer_path.endswith("/" + FCITX_PRODUCER_PATH) and producer_path != FCITX_PRODUCER_PATH:
        raise ValueError(f"{context}.producer.path does not identify {FCITX_PRODUCER_PATH}")
    producer_bytes = _read_regular(
        Path(__file__).resolve().parents[2] / FCITX_PRODUCER_PATH,
        "Fcitx stability producer",
    )
    _expect(
        sha256_bytes(producer_bytes),
        expected.native_producer_sha256,
        f"{context}.producer.sha256",
    )
    _expect(
        _integer(producer["size"], f"{context}.producer.size", minimum=1),
        len(producer_bytes),
        f"{context}.producer.size",
    )

    source = _object(payload["source"], f"{context}.source")
    _exact(source, {"repository_root", "git_head", "worktree_clean"}, f"{context}.source")
    repository_root = Path(
        _string(source["repository_root"], f"{context}.source.repository_root")
    )
    if not repository_root.is_absolute():
        raise ValueError(f"{context}.source.repository_root must be absolute")
    git_head = _string(source["git_head"], f"{context}.source.git_head")
    if re.fullmatch(r"[0-9a-f]{40}", git_head) is None:
        raise ValueError(f"{context}.source.git_head must be a full lowercase commit")
    _boolean(source["worktree_clean"], f"{context}.source.worktree_clean")
    expected_producer_path = repository_root / FCITX_PRODUCER_PATH
    _expect(Path(producer_path), expected_producer_path, f"{context}.producer.path")
    reported_producer_bytes = _binding_bytes(
        {
            "path": FCITX_PRODUCER_PATH,
            "sha256": expected.native_producer_sha256,
        },
        repository_root,
        f"{context}.producer reported repository blob",
    )
    _expect(
        len(reported_producer_bytes),
        _integer(producer["size"], f"{context}.producer.size", minimum=1),
        f"{context}.producer reported repository size",
    )

    if expected.input_snapshot_fingerprint is None:
        raise ValueError(f"{context}: Fcitx input snapshot fingerprint is not frozen")
    snapshot_root, snapshot_entries, snapshot_directories = (
        _validate_fcitx_snapshot(
            payload["input_snapshot"],
            f"{context}.input_snapshot",
            native_path,
            expected.input_snapshot_fingerprint,
        )
    )
    required_singletons = {
        "harness": ("harness", "harness"),
        "addon.so": ("addon", "addon"),
        "server": ("product_server", "product_server"),
        "config/addon.conf": ("addon_config", "addon_config"),
        "config/input-method.conf": ("input_method_config", "input_method_config"),
        "mozc/verifier.py": ("mozc_verifier", "mozc_verifier"),
        "system-test-addon/testfrontend.conf": (
            "system_test_addon:testfrontend.conf",
            "system_test_addon:testfrontend.conf",
        ),
        "system-test-addon/testim.conf": (
            "system_test_addon:testim.conf",
            "system_test_addon:testim.conf",
        ),
        "system-test-addon/testui.conf": (
            "system_test_addon:testui.conf",
            "system_test_addon:testui.conf",
        ),
    }
    for relative, (input_id, label) in required_singletons.items():
        entry = snapshot_entries.get(relative)
        if entry is None:
            raise ValueError(f"{context}.input_snapshot is missing {label}")
        _expect(entry["input_id"], input_id, f"{context}.input_snapshot.{label}.input_id")
    for relative, entry in snapshot_entries.items():
        if (relative.startswith("dictionary/")) != (entry["input_id"] == "dictionary"):
            raise ValueError(f"{context}.input_snapshot has a misplaced dictionary entry")
        if (relative.startswith("mozc/generation/")) != (
            entry["input_id"] == "mozc_generation"
        ):
            raise ValueError(f"{context}.input_snapshot has a misplaced Mozc bundle entry")
        if (relative.startswith("llama-lib/")) != (entry["input_id"] == "llama_lib"):
            raise ValueError(f"{context}.input_snapshot has a misplaced runtime dependency")
    mozc_bundle_files = {
        relative.removeprefix("mozc/generation/")
        for relative, entry in snapshot_entries.items()
        if relative.startswith("mozc/generation/")
        and entry["input_id"] == "mozc_generation"
    }
    _expect(
        mozc_bundle_files,
        {
            "fcitx5-grimodex-mozc-helper",
            "manifest.json",
            "mozc.data",
            "licenses/ABSEIL-LICENSE",
            "licenses/DICTIONARY-OSS-NOTICE.txt",
            "licenses/FCITX-MOZKEY-THIRD-PARTY-NOTICES.md",
            "licenses/JAPANESE-USAGE-DICTIONARY-LICENSE",
            "licenses/MOZC-LICENSE",
            "licenses/PROTOBUF-LICENSE",
            "licenses/UTF8-RANGE-LICENSE",
        },
        f"{context}.input_snapshot Mozc bundle files",
    )
    if expected.baseline_resource_fingerprint is None:
        raise ValueError(f"{context}: baseline dictionary fingerprint is not frozen")
    _expect(
        _dictionary_fingerprint_from_snapshot(snapshot_entries),
        expected.baseline_resource_fingerprint,
        f"{context}.input_snapshot dictionary fingerprint",
    )
    artifacts = _object(payload["artifacts"], f"{context}.artifacts")
    _exact(
        artifacts,
        {
            "harness",
            "addon",
            "server",
            "dictionary",
            "llama_library_directory",
            "mozc_verifier",
            "mozc_helper",
            "mozc_data",
            "mozc_generation",
            "mozc_manifest",
        },
        f"{context}.artifacts",
    )

    direct_artifacts: dict[str, tuple[dict[str, Any], str, str]] = {}
    for name, relative, input_id in (
        ("harness", "harness", "harness"),
        ("addon", "addon.so", "addon"),
        ("server", "server", "product_server"),
        ("mozc_verifier", "mozc/verifier.py", "mozc_verifier"),
        ("mozc_manifest", "mozc/generation/manifest.json", "mozc_generation"),
    ):
        binding = _fcitx_file_binding(
            artifacts[name], f"{context}.artifacts.{name}"
        )
        entry = snapshot_entries.get(relative)
        if entry is None:
            raise ValueError(f"{context}.input_snapshot is missing {relative!r}")
        _expect(entry["input_id"], input_id, f"{context}.input_snapshot.{relative}.input_id")
        _expect(
            (entry["size"], entry["sha256"]),
            (binding["size"], binding["sha256"]),
            f"{context}.artifacts.{name} snapshot identity",
        )
        _expect(
            Path(binding["path"]),
            snapshot_root / relative,
            f"{context}.artifacts.{name}.path",
        )
        direct_artifacts[name] = (binding, relative, input_id)
    _expect(
        (direct_artifacts["server"][0]["size"], direct_artifacts["server"][0]["sha256"]),
        (expected.product_server_size, expected.product_server_sha256),
        f"{context}.artifacts.server identity",
    )
    _expect(
        direct_artifacts["mozc_manifest"][0]["sha256"],
        B0_MANIFEST_SHA256,
        f"{context}.artifacts.mozc_manifest.sha256",
    )

    runtime_artifacts: dict[str, dict[str, Any]] = {}
    for name, artifact_id, filename in (
        ("mozc_helper", "fcitx5-grimodex-mozc-helper", "fcitx5-grimodex-mozc-helper"),
        ("mozc_data", "mozc.data", "mozc.data"),
    ):
        binding = _fcitx_file_binding(
            artifacts[name], f"{context}.artifacts.{name}", require_mode=True
        )
        expected_identity = expected.artifacts.get(artifact_id)
        if expected_identity is None:
            raise ValueError(f"{context}: policy is missing artifact {artifact_id!r}")
        _expect(
            (binding["size"], binding["sha256"]),
            expected_identity,
            f"{context}.artifacts.{name} identity",
        )
        _expect(
            binding["mode"],
            0o555 if name == "mozc_helper" else 0o444,
            f"{context}.artifacts.{name}.mode",
        )
        entry = snapshot_entries.get(f"mozc/generation/{filename}")
        if entry is None:
            raise ValueError(f"{context}.input_snapshot is missing Mozc {filename!r}")
        _expect(
            (entry["size"], entry["sha256"], entry["mode"]),
            (binding["size"], binding["sha256"], binding["mode"]),
            f"{context}.artifacts.{name} snapshot identity",
        )
        runtime_artifacts[name] = binding

    for field, relative in (
        ("dictionary", "dictionary"),
        ("llama_library_directory", "llama-lib"),
    ):
        directory = _object(artifacts[field], f"{context}.artifacts.{field}")
        _exact(directory, {"path"}, f"{context}.artifacts.{field}")
        _expect(
            Path(_string(directory["path"], f"{context}.artifacts.{field}.path")),
            snapshot_root / relative,
            f"{context}.artifacts.{field}.path",
        )

    generation = _object(
        artifacts["mozc_generation"], f"{context}.artifacts.mozc_generation"
    )
    _exact(
        generation,
        {
            "path",
            "source_path",
            "content_address",
            "prepared_content_address",
            "artifact_fingerprint",
        },
        f"{context}.artifacts.mozc_generation",
    )
    generation_path = Path(
        _string(generation["path"], f"{context}.artifacts.mozc_generation.path")
    )
    _expect(
        Path(_string(generation["source_path"], f"{context}.artifacts.mozc_generation.source_path")),
        snapshot_root / "mozc/generation",
        f"{context}.artifacts.mozc_generation.source_path",
    )
    content_address = _string(
        generation["content_address"],
        f"{context}.artifacts.mozc_generation.content_address",
    )
    prepared_content_address = _string(
        generation["prepared_content_address"],
        f"{context}.artifacts.mozc_generation.prepared_content_address",
    )
    for field, value in (
        ("content_address", content_address),
        ("prepared_content_address", prepared_content_address),
    ):
        if re.fullmatch(r"sha256-[0-9a-f]{64}", value) is None:
            raise ValueError(f"{context}.artifacts.mozc_generation.{field} is invalid")
    _expect(generation_path.name, prepared_content_address, f"{context}.artifacts.mozc_generation.path")
    _expect(
        generation_path,
        snapshot_root.parent / "mozc-runtime" / prepared_content_address,
        f"{context}.artifacts.mozc_generation.path",
    )
    for name, filename in (
        ("mozc_helper", "fcitx5-grimodex-mozc-helper"),
        ("mozc_data", "mozc.data"),
    ):
        _expect(
            Path(runtime_artifacts[name]["path"]),
            generation_path / filename,
            f"{context}.artifacts.{name}.path",
        )
    runtime_fingerprint = _runtime_fingerprint_from_hashes(
        {
            "fcitx5-grimodex-mozc-helper": runtime_artifacts["mozc_helper"]["sha256"],
            "manifest.json": direct_artifacts["mozc_manifest"][0]["sha256"],
            "mozc.data": runtime_artifacts["mozc_data"]["sha256"],
        }
    )
    _expect(runtime_fingerprint, expected.artifact_fingerprint, f"{context}.artifacts runtime fingerprint")
    _expect(
        _sha256(
            generation["artifact_fingerprint"],
            f"{context}.artifacts.mozc_generation.artifact_fingerprint",
        ),
        runtime_fingerprint,
        f"{context}.artifacts.mozc_generation.artifact_fingerprint",
    )
    _validate_fcitx_retained_runtime(
        snapshot_root.parent,
        prepared_content_address,
        runtime_artifacts,
        snapshot_directories,
        snapshot_entries,
        f"{context}.retained_evidence",
    )

    runtime_relative_entries = {
        relative: entry
        for relative, entry in snapshot_entries.items()
        if entry["input_id"] == "llama_lib"
    }
    _expect(
        set(runtime_relative_entries),
        {f"llama-lib/{name}" for name in expected.runtime_dependencies},
        f"{context}.input_snapshot runtime dependencies",
    )
    for name, expected_identity in expected.runtime_dependencies.items():
        entry = runtime_relative_entries[f"llama-lib/{name}"]
        _expect(
            (entry["size"], entry["sha256"]),
            expected_identity,
            f"{context}.input_snapshot runtime dependency {name}",
        )

    runtime = _object(payload["runtime_integrity"], f"{context}.runtime_integrity")
    _exact(runtime, {"post_run_verified", "verified_artifacts"}, f"{context}.runtime_integrity")
    _expect(
        _boolean(runtime["post_run_verified"], f"{context}.runtime_integrity.post_run_verified"),
        True,
        f"{context}.runtime_integrity.post_run_verified",
    )
    _expect(
        _array(runtime["verified_artifacts"], f"{context}.runtime_integrity.verified_artifacts"),
        ["mozc_helper", "mozc_data"],
        f"{context}.runtime_integrity.verified_artifacts",
    )

    command = [
        _string(item, f"{context}.command[{index}]")
        for index, item in enumerate(_array(payload["command"], f"{context}.command"))
    ]
    command_fields = {
        "--harness",
        "--addon",
        "--server",
        "--dictionary",
        "--addon-config",
        "--input-method-config",
        "--system-test-addon-dir",
        "--llama-lib-dir",
        "--converter-backend",
        "--mozc-generation",
        "--mozc-verifier",
        "--cycles",
        "--soak-iterations",
        "--timeout",
        "--product-source-ref",
        "--product-server-sha256",
        "--product-server-size",
        "--result-output",
    }
    if len(command) != 2 + (2 * len(command_fields)):
        raise ValueError(f"{context}.command has the wrong number of arguments")
    if not Path(command[0]).is_absolute():
        raise ValueError(f"{context}.command[0] must be an absolute Python executable")
    if re.fullmatch(r"python(?:[0-9]+(?:\.[0-9]+)*)?", Path(command[0]).name) is None:
        raise ValueError(f"{context}.command[0] must identify Python")
    try:
        command_python = Path(command[0]).resolve(strict=True)
        trusted_python = Path(sys.executable).resolve(strict=True)
    except OSError as error:
        raise ValueError(f"{context}.command[0] Python executable is unavailable") from error
    _expect(command_python, trusted_python, f"{context}.command[0] trusted Python")
    _read_regular(trusted_python, f"{context}.command[0] trusted Python")
    _expect(Path(command[1]), expected_producer_path, f"{context}.command producer")
    command_options: dict[str, str] = {}
    for offset in range(2, len(command), 2):
        flag, argument = command[offset : offset + 2]
        if flag not in command_fields:
            raise ValueError(f"{context}.command has unknown option {flag!r}")
        if flag in command_options:
            raise ValueError(f"{context}.command has duplicate option {flag!r}")
        command_options[flag] = argument
    _expect(set(command_options), command_fields, f"{context}.command options")

    command_path_bindings = {
        "--harness": "harness",
        "--addon": "addon.so",
        "--server": "server",
        "--addon-config": "config/addon.conf",
        "--input-method-config": "config/input-method.conf",
        "--mozc-verifier": "mozc/verifier.py",
    }
    for flag, relative in command_path_bindings.items():
        _expect(
            Path(command_options[flag]).resolve(strict=True),
            Path(snapshot_entries[relative]["source_path"]),
            f"{context}.command {flag}",
        )
    for flag, input_id in (
        ("--dictionary", "dictionary"),
        ("--llama-lib-dir", "llama_lib"),
        ("--system-test-addon-dir", "system_test_addon"),
    ):
        source_root = Path(command_options[flag]).resolve(strict=True)
        if not source_root.is_absolute():
            raise ValueError(f"{context}.command {flag} must be absolute")
        matching_entries = [
            entry
            for entry in snapshot_entries.values()
            if entry["input_id"] == input_id
            or (input_id == "system_test_addon" and entry["input_id"].startswith(input_id + ":"))
        ]
        if not matching_entries:
            raise ValueError(f"{context}.command {flag} has no snapshot inputs")
        for entry in matching_entries:
            try:
                Path(entry["source_path"]).relative_to(source_root)
            except ValueError as error:
                raise ValueError(
                    f"{context}.command {flag} does not contain all snapshot sources"
                ) from error
    mozc_source_root = Path(command_options["--mozc-generation"]).resolve(strict=True)
    if not mozc_source_root.is_absolute():
        raise ValueError(f"{context}.command --mozc-generation must be absolute")
    _expect(mozc_source_root.name, content_address, f"{context}.command --mozc-generation")
    for relative, entry in snapshot_entries.items():
        if entry["input_id"] != "mozc_generation":
            continue
        expected_source = mozc_source_root / relative.removeprefix("mozc/generation/")
        _expect(
            Path(entry["source_path"]),
            expected_source,
            f"{context}.input_snapshot source for {relative}",
        )
    _expect(
        Path(command_options["--result-output"]),
        native_path,
        f"{context}.command --result-output",
    )

    configuration = _object(payload["configuration"], f"{context}.configuration")
    _exact(configuration, {"converter_backend", "iterations", "cycles", "timeout_seconds"}, f"{context}.configuration")
    _expect(configuration["converter_backend"], "mozc", f"{context}.configuration.converter_backend")
    expected_iterations, expected_cycles = (
        (150_000, 1) if suite_id == FCITX_LONG_SOAK_ID else (100, 3)
    )
    _expect(
        _integer(configuration["iterations"], f"{context}.configuration.iterations"),
        expected_iterations,
        f"{context}.configuration.iterations",
    )
    _expect(
        _integer(configuration["cycles"], f"{context}.configuration.cycles", minimum=1),
        expected_cycles,
        f"{context}.configuration.cycles",
    )
    timeout_seconds = _integer(configuration["timeout_seconds"], f"{context}.configuration.timeout_seconds", minimum=1)
    _expect(command_options["--converter-backend"], "mozc", f"{context}.command --converter-backend")
    _expect(command_options["--cycles"], str(expected_cycles), f"{context}.command --cycles")
    _expect(command_options["--soak-iterations"], str(expected_iterations), f"{context}.command --soak-iterations")
    _expect(command_options["--timeout"], str(timeout_seconds), f"{context}.command --timeout")
    _expect(command_options["--product-source-ref"], expected.product_source_ref, f"{context}.command --product-source-ref")
    _expect(
        _sha256(command_options["--product-server-sha256"], f"{context}.command --product-server-sha256"),
        expected.product_server_sha256,
        f"{context}.command --product-server-sha256",
    )
    _expect(command_options["--product-server-size"], str(expected.product_server_size), f"{context}.command --product-server-size")
    for field, expected_value in (
        ("conversions", expected_iterations * expected_cycles),
        ("cycles", expected_cycles),
        ("helper_launches", expected_cycles),
        ("server_launches", expected_cycles),
        ("helper_recoveries", 0),
        ("server_recoveries", 0),
        ("residue_count", 0),
    ):
        _expect(
            _integer(payload[field], f"{context}.{field}"),
            expected_value,
            f"{context}.{field}",
        )
    cycles = _array(payload["cycle_results"], f"{context}.cycle_results")
    _expect(len(cycles), expected_cycles, f"{context}.cycle_results length")
    helper_launches = server_launches = helper_recoveries = server_recoveries = 0
    conversions = 0
    process_identities: set[tuple[int, str]] = set()
    cycle_sessions: set[int] = set()
    for index, raw in enumerate(cycles, 1):
        cycle_context = f"{context}.cycle_results[{index - 1}]"
        cycle = _object(raw, cycle_context)
        _exact(cycle, {"cycle", "conversions", "lock_owner_observed", "max_concurrent_helpers", "process_group_cleanup_ok", "server", "helper"}, cycle_context)
        _expect(
            _integer(cycle["cycle"], f"{cycle_context}.cycle", minimum=1),
            index,
            f"{cycle_context}.cycle",
        )
        _expect(
            _integer(cycle["conversions"], f"{cycle_context}.conversions"),
            expected_iterations,
            f"{cycle_context}.conversions",
        )
        _expect(_boolean(cycle["lock_owner_observed"], f"{cycle_context}.lock_owner_observed"), True, f"{cycle_context}.lock_owner_observed")
        _expect(
            _integer(
                cycle["max_concurrent_helpers"],
                f"{cycle_context}.max_concurrent_helpers",
            ),
            1,
            f"{cycle_context}.max_concurrent_helpers",
        )
        _expect(_boolean(cycle["process_group_cleanup_ok"], f"{cycle_context}.process_group_cleanup_ok"), True, f"{cycle_context}.process_group_cleanup_ok")
        server_count, server_recovery, server_identities, server_session = _validate_process_payload(
            cycle["server"],
            f"{cycle_context}.server",
            expected_executable=direct_artifacts["server"][0]["path"],
        )
        helper_count, helper_recovery, helper_identities, helper_session = _validate_process_payload(
            cycle["helper"],
            f"{cycle_context}.helper",
            expected_executable=runtime_artifacts["mozc_helper"]["path"],
        )
        _expect((server_count, server_recovery), (1, 0), f"{cycle_context}.server counts")
        _expect((helper_count, helper_recovery), (1, 0), f"{cycle_context}.helper counts")
        _expect(server_session, helper_session, f"{cycle_context} process session")
        if server_session in cycle_sessions:
            raise ValueError(f"{cycle_context} reuses an earlier process session")
        cycle_sessions.add(server_session)
        for identity in (*server_identities, *helper_identities):
            if identity in process_identities:
                raise ValueError(f"{cycle_context} reuses a process identity")
            process_identities.add(identity)
        conversions += cycle["conversions"]
        server_launches += server_count
        helper_launches += helper_count
        server_recoveries += server_recovery
        helper_recoveries += helper_recovery
    _expect(conversions, payload["conversions"], f"{context}.cycle conversion aggregate")
    _expect(server_launches, payload["server_launches"], f"{context}.cycle server aggregate")
    _expect(helper_launches, payload["helper_launches"], f"{context}.cycle helper aggregate")
    return _base_observations(
        conversions=conversions,
        cycles=expected_cycles,
        helper_launches=helper_launches,
        server_launches=server_launches,
        helper_recoveries=helper_recoveries,
        server_recoveries=server_recoveries,
        residue_count=0,
    )


def validate_native_result(
    suite_id: str,
    data: bytes,
    context: str,
    expected: NativeExpectations,
    *,
    native_path: Path | None = None,
) -> dict[str, int | None]:
    """Parse a native suite result and derive its non-trusted observations."""

    if suite_id == ADAPTER_SOAK_ID:
        return _validate_adapter_soak(data, context, expected, native_path)
    if suite_id == PROTOCOL_STEADY_ID:
        return _validate_protocol_v2_steady(data, context, expected, native_path)
    if suite_id == PROTOCOL_RECOVERY_ID:
        return _validate_recovery(data, context, expected, native_path)
    if suite_id in {FCITX_LONG_SOAK_ID, FCITX_LIFECYCLE_ID}:
        return _validate_fcitx(suite_id, data, context, expected, native_path)
    raise ValueError(f"unknown Mozc B0 stability suite {suite_id!r}")


def native_schema(suite_id: str) -> str:
    if suite_id == ADAPTER_SOAK_ID:
        return ADAPTER_SOAK_SCHEMA
    if suite_id == PROTOCOL_STEADY_ID:
        return PROTOCOL_STEADY_SCHEMA
    if suite_id == PROTOCOL_RECOVERY_ID:
        return RECOVERY_SCHEMA
    if suite_id in {FCITX_LONG_SOAK_ID, FCITX_LIFECYCLE_ID}:
        return FCITX_SCHEMA
    raise ValueError(f"unknown Mozc B0 stability suite {suite_id!r}")


def build_record(
    suite_id: str,
    native_result_path: Path,
    native_result_bytes: bytes,
    *,
    artifact_fingerprint: str,
    recovery_fixture_identity_value: str | None,
) -> dict[str, Any]:
    if suite_id not in SUITE_IDS:
        raise ValueError(f"unknown Mozc B0 stability suite {suite_id!r}")
    artifact: dict[str, str]
    if suite_id in B0_SUITE_IDS:
        artifact = {
            "kind": "b0",
            "fingerprint": _sha256(
                artifact_fingerprint, "artifact fingerprint"
            ),
        }
    else:
        if recovery_fixture_identity_value is None:
            raise ValueError("protocol-v2-recovery requires a fixture identity")
        artifact = {
            "kind": "fault-fixture",
            "fixture_identity": _sha256(
                recovery_fixture_identity_value, "recovery fixture identity"
            ),
        }
    return {
        "schema": RECORD_SCHEMA,
        "id": suite_id,
        "orchestrator": {
            "path": ORCHESTRATOR_PATH,
            "sha256": sha256_bytes(
                _read_regular(Path(__file__).resolve(), "stability orchestrator")
            ),
        },
        "command": list(CANONICAL_COMMANDS[suite_id]),
        "product_source_ref": PRODUCT_SOURCE_REF,
        "artifact": artifact,
        "native_result": {
            "schema": native_schema(suite_id),
            "path": native_result_path.name,
            "sha256": sha256_bytes(native_result_bytes),
        },
    }


def atomic_publish(path: Path, data: bytes) -> None:
    parent = path.parent.resolve(strict=True)
    if path.name in {"", ".", ".."}:
        raise ValueError("output must name a file")
    descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0))
    temporary_name = f".{path.name}.{secrets.token_hex(16)}.tmp"
    temporary_descriptor = -1
    try:
        try:
            os.stat(path.name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError(f"output already exists: {parent / path.name}")
        temporary_descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=descriptor,
        )
        offset = 0
        while offset < len(data):
            offset += os.write(temporary_descriptor, data[offset:])
        os.fsync(temporary_descriptor)
        os.close(temporary_descriptor)
        temporary_descriptor = -1
        os.link(
            temporary_name,
            path.name,
            src_dir_fd=descriptor,
            dst_dir_fd=descriptor,
            follow_symlinks=False,
        )
        os.fsync(descriptor)
    finally:
        if temporary_descriptor >= 0:
            os.close(temporary_descriptor)
        try:
            os.unlink(temporary_name, dir_fd=descriptor)
        except FileNotFoundError:
            pass
        os.close(descriptor)


def expectations_from_policy(
    policy_path: Path,
    suite_id: str,
) -> NativeExpectations:
    policy = _object(
        load_json_bytes(_read_regular(policy_path, "B0 policy"), str(policy_path)),
        str(policy_path),
    )
    candidate = _object(policy.get("candidate"), f"{policy_path}.candidate")
    baseline = _object(policy.get("baseline"), f"{policy_path}.baseline")
    _expect(baseline.get("id"), "hazkey", f"{policy_path}.baseline.id")
    baseline_fingerprint = _sha256(
        baseline.get("resource_fingerprint"),
        f"{policy_path}.baseline.resource_fingerprint",
    )
    executable = _object(
        candidate.get("product_executable"),
        f"{policy_path}.candidate.product_executable",
    )
    artifacts: dict[str, tuple[int, str]] = {}
    for index, raw in enumerate(
        _array(candidate.get("artifacts"), f"{policy_path}.candidate.artifacts")
    ):
        item = _object(raw, f"{policy_path}.candidate.artifacts[{index}]")
        artifact_id = _string(
            item.get("id"), f"{policy_path}.candidate.artifacts[{index}].id"
        )
        artifacts[artifact_id] = (
            _integer(
                item.get("size_bytes"),
                f"{policy_path}.candidate.artifacts[{index}].size_bytes",
                minimum=1,
            ),
            _sha256(
                item.get("sha256"),
                f"{policy_path}.candidate.artifacts[{index}].sha256",
            ),
        )
    runtime = _object(
        candidate.get("runtime_dependencies"),
        f"{policy_path}.candidate.runtime_dependencies",
    )
    runtime_dependencies: dict[str, tuple[int, str]] = {}
    for index, raw in enumerate(
        _array(
            runtime.get("files"),
            f"{policy_path}.candidate.runtime_dependencies.files",
        )
    ):
        item_context = f"{policy_path}.candidate.runtime_dependencies.files[{index}]"
        item = _object(raw, item_context)
        name = _string(item.get("path"), f"{item_context}.path")
        runtime_dependencies[name] = (
            _integer(item.get("size_bytes"), f"{item_context}.size_bytes", minimum=1),
            _sha256(item.get("sha256"), f"{item_context}.sha256"),
        )
    _expect(
        set(runtime_dependencies),
        set(run_mozc_b0_measurement.RUNTIME_DEPENDENCY_FILENAMES),
        f"{policy_path}.candidate.runtime_dependencies file names",
    )
    gates = _object(policy.get("gates"), f"{policy_path}.gates")
    stability_gate = _object(
        gates.get("long_running_stability"),
        f"{policy_path}.gates.long_running_stability",
    )
    matching = [
        _object(raw, f"{policy_path}.stability check")
        for raw in _array(stability_gate.get("checks"), f"{policy_path}.stability checks")
        if isinstance(raw, dict) and raw.get("id") == suite_id
    ]
    if len(matching) != 1:
        raise ValueError(f"{policy_path}: expected one stability contract for {suite_id}")
    contract = matching[0]
    producer = _object(
        contract.get("native_producer"), f"{policy_path}.{suite_id}.native_producer"
    )
    producer_sha = producer.get("sha256")
    if producer_sha is not None:
        producer_sha = _sha256(
            producer_sha, f"{policy_path}.{suite_id}.native_producer.sha256"
        )
    runner_value = contract.get("execution_runner")
    if runner_value is None:
        runner_path = None
        runner_sha = None
    else:
        runner_context = f"{policy_path}.{suite_id}.execution_runner"
        runner = _object(runner_value, runner_context)
        _exact(runner, {"path", "sha256"}, runner_context)
        runner_path = _string(runner["path"], f"{runner_context}.path")
        if runner_path.startswith("/") or ".." in Path(runner_path).parts:
            raise ValueError(f"{runner_context}.path must be repo-relative")
        runner_sha = _sha256(runner["sha256"], f"{runner_context}.sha256")
    package_value = contract.get("execution_package")
    if package_value is None:
        package_file_count = None
        package_size = None
        package_fingerprint = None
    else:
        package_context = f"{policy_path}.{suite_id}.execution_package"
        package = _object(package_value, package_context)
        _exact(
            package,
            {"path", "file_count", "size_bytes", "fingerprint"},
            package_context,
        )
        _expect(package["path"], SWIFT_PACKAGE_ROOT, f"{package_context}.path")
        package_file_count = _integer(
            package["file_count"], f"{package_context}.file_count", minimum=1
        )
        package_size = _integer(
            package["size_bytes"], f"{package_context}.size_bytes", minimum=1
        )
        package_fingerprint = _sha256(
            package["fingerprint"], f"{package_context}.fingerprint"
        )
    fixture_identity = contract.get("recovery_fixture_identity")
    if fixture_identity is not None:
        fixture_identity = _sha256(
            fixture_identity, f"{policy_path}.{suite_id}.recovery_fixture_identity"
        )
    if "input_snapshot_fingerprint" not in contract:
        raise ValueError(
            f"{policy_path}.{suite_id}.input_snapshot_fingerprint is missing"
        )
    snapshot_fingerprint_value = contract["input_snapshot_fingerprint"]
    if suite_id in {FCITX_LONG_SOAK_ID, FCITX_LIFECYCLE_ID}:
        if snapshot_fingerprint_value is None:
            raise ValueError(
                f"{policy_path}.{suite_id}.input_snapshot_fingerprint must be frozen"
            )
        snapshot_fingerprint = _sha256(
            snapshot_fingerprint_value,
            f"{policy_path}.{suite_id}.input_snapshot_fingerprint",
        )
    else:
        if snapshot_fingerprint_value is not None:
            raise ValueError(
                f"{policy_path}.{suite_id}.input_snapshot_fingerprint must be null"
            )
        snapshot_fingerprint = None
    return NativeExpectations(
        product_source_ref=_string(
            candidate.get("product_source_revision"),
            f"{policy_path}.candidate.product_source_revision",
        ),
        artifact_fingerprint=_sha256(
            candidate.get("resource_fingerprint"),
            f"{policy_path}.candidate.resource_fingerprint",
        ),
        product_server_size=_integer(
            executable.get("size_bytes"),
            f"{policy_path}.candidate.product_executable.size_bytes",
            minimum=1,
        ),
        product_server_sha256=_sha256(
            executable.get("sha256"),
            f"{policy_path}.candidate.product_executable.sha256",
        ),
        artifacts=artifacts,
        native_producer_sha256=producer_sha,
        recovery_fixture_identity=fixture_identity,
        input_snapshot_fingerprint=snapshot_fingerprint,
        execution_runner_path=runner_path,
        execution_runner_sha256=runner_sha,
        swift_package_file_count=package_file_count,
        swift_package_size_bytes=package_size,
        swift_package_fingerprint=package_fingerprint,
        runtime_dependencies=runtime_dependencies,
        baseline_resource_fingerprint=baseline_fingerprint,
    )


def _write_private(path: Path, data: bytes, mode: int = 0o600) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        mode,
    )
    try:
        offset = 0
        while offset < len(data):
            offset += os.write(descriptor, data[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _process_group_members(process_group: int) -> list[int]:
    members: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "stat").read_text(encoding="utf-8")
            suffix = raw[raw.rindex(")") + 2 :].split()
            observed_group = int(suffix[2])
        except (OSError, ValueError, IndexError):
            continue
        if observed_group == process_group:
            members.append(int(entry.name))
    return sorted(members)


def _session_members(session_identifier: int) -> list[int]:
    members: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "stat").read_text(encoding="utf-8")
            suffix = raw[raw.rindex(")") + 2 :].split()
            observed_session = int(suffix[3])
        except (OSError, ValueError, IndexError):
            continue
        if observed_session == session_identifier:
            members.append(int(entry.name))
    return sorted(members)


def _stop_process_group(process_group: int) -> list[int]:
    members = _process_group_members(process_group)
    if not members:
        return []
    for signal_number in (15, 9):
        try:
            os.killpg(process_group, signal_number)
        except ProcessLookupError:
            break
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if not _process_group_members(process_group):
                return members
            time.sleep(0.02)
    return members


def _stop_session(session_identifier: int) -> list[int]:
    members = _session_members(session_identifier)
    if not members:
        return []
    for signal_number in (15, 9):
        for process_identifier in _session_members(session_identifier):
            try:
                os.kill(process_identifier, signal_number)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if not _session_members(session_identifier):
                return members
            time.sleep(0.02)
    return members


def _observe_session_members(
    session_identifier: int,
    observed: set[int],
    stop: threading.Event,
) -> None:
    while True:
        observed.update(_session_members(session_identifier))
        if stop.wait(0.005):
            return


def _process_start_time_ticks(process_identifier: int) -> int:
    raw = (Path("/proc") / str(process_identifier) / "stat").read_text(
        encoding="utf-8"
    )
    suffix = raw[raw.rindex(")") + 2 :].split()
    return int(suffix[19])


def _process_identity(process_identifier: int) -> tuple[int, int]:
    return process_identifier, _process_start_time_ticks(process_identifier)


def _identity_payload(
    identity: tuple[int, int],
    executable: tuple[int, str] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "pid": identity[0],
        "start_time_ticks": identity[1],
    }
    if executable is not None:
        payload["executable"] = {
            "size_bytes": executable[0],
            "sha256": executable[1],
        }
    return payload


def _runtime_fingerprint(files: Mapping[str, bytes]) -> str:
    digest = hashlib.sha256()
    digest.update(b"hazkey.mozc-runtime-fingerprint.v1\0")
    for name in sorted(files, key=lambda value: value.encode("utf-8")):
        encoded = name.encode("utf-8")
        digest.update(b"\x01")
        digest.update(len(encoded).to_bytes(8, byteorder="big"))
        digest.update(encoded)
        digest.update(hashlib.sha256(files[name]).digest())
    return "sha256:" + digest.hexdigest()


def _swift_package_directories(files: Mapping[str, bytes]) -> set[str]:
    directories = {"."}
    for relative in files:
        path = Path(relative)
        for parent in path.parents:
            if str(parent) == ".":
                break
            directories.add(parent.as_posix())
    return directories


def _swift_package_file_mode(relative: str) -> int:
    return 0o555 if relative == "scripts/swift-test.sh" else 0o444


def _swift_package_identity_from_files(
    files: Mapping[str, bytes],
) -> tuple[int, int, str]:
    entries: list[dict[str, Any]] = [
        {"kind": "directory", "path": path, "mode": 0o555}
        for path in sorted(
            _swift_package_directories(files), key=lambda value: value.encode("utf-8")
        )
    ]
    for relative in sorted(files, key=lambda value: value.encode("utf-8")):
        data = files[relative]
        entries.append(
            {
                "kind": "file",
                "path": relative,
                "mode": _swift_package_file_mode(relative),
                "size_bytes": len(data),
                "sha256": sha256_bytes(data),
            }
        )
    fingerprint = sha256_bytes(
        canonical_json(
            {
                "domain": SWIFT_PACKAGE_FINGERPRINT_DOMAIN,
                "entries": entries,
            }
        )
    )
    return len(files), sum(len(data) for data in files.values()), fingerprint


def _read_swift_package_inputs(repository_root: Path) -> dict[str, bytes]:
    package_root = repository_root / SWIFT_PACKAGE_ROOT
    metadata = package_root.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("Swift package root must be a non-symlink directory")
    candidates: set[Path] = set()
    for relative in SWIFT_PACKAGE_EXPLICIT_FILES:
        candidates.add(package_root / relative)
    excluded_prefixes = tuple(
        Path(prefix) for prefix in SWIFT_PACKAGE_EXCLUDED_PREFIXES
    )
    for relative_root in SWIFT_PACKAGE_RECURSIVE_ROOTS:
        source_root = package_root / relative_root
        root_metadata = source_root.lstat()
        if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
            raise ValueError(
                f"Swift package input root {relative_root} must be a non-symlink directory"
            )
        for path in source_root.rglob("*"):
            relative = path.relative_to(package_root)
            if any(
                relative == excluded or excluded in relative.parents
                for excluded in excluded_prefixes
            ):
                continue
            entry_metadata = path.lstat()
            if stat.S_ISLNK(entry_metadata.st_mode):
                raise ValueError(f"Swift package input {relative} must not be a symlink")
            if stat.S_ISDIR(entry_metadata.st_mode):
                continue
            if not stat.S_ISREG(entry_metadata.st_mode):
                raise ValueError(
                    f"Swift package input {relative} must be a regular file"
                )
            candidates.add(path)
    files: dict[str, bytes] = {}
    for path in sorted(candidates, key=lambda item: os.fsencode(item.as_posix())):
        relative = path.relative_to(package_root).as_posix()
        files[relative] = _read_regular(path, f"Swift package input {relative}")
    if "Sources/hazkey-server/constants.swift" not in files:
        raise ValueError(
            "Swift package input is missing generated Sources/hazkey-server/constants.swift"
        )
    return files


def _read_swift_snapshot_directory(
    descriptor: int,
    prefix: tuple[str, ...],
    context: str,
) -> tuple[dict[str, bytes], set[str]]:
    before_directory = os.fstat(descriptor)
    if not stat.S_ISDIR(before_directory.st_mode):
        raise ValueError(f"{context} must be a directory")
    if stat.S_IMODE(before_directory.st_mode) != 0o555:
        raise ValueError(f"{context} directory mode must be 0555")
    names = os.listdir(descriptor)
    if len(names) != len(set(names)):
        raise ValueError(f"{context} contains duplicate entries")
    files: dict[str, bytes] = {}
    directories = {"/".join(prefix) if prefix else "."}
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
    for name in sorted(names, key=os.fsencode):
        if name in {"", ".", ".."} or "/" in name:
            raise ValueError(f"{context} contains an invalid entry name")
        relative = "/".join((*prefix, name))
        entry_context = f"{context}/{name}"
        metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            try:
                child = os.open(name, directory_flags, dir_fd=descriptor)
            except OSError as error:
                raise ValueError(
                    f"{entry_context} must be a non-symlink directory"
                ) from error
            try:
                child_files, child_directories = _read_swift_snapshot_directory(
                    child, (*prefix, name), entry_context
                )
            finally:
                os.close(child)
            files.update(child_files)
            directories.update(child_directories)
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"{entry_context} must be a regular file")
        try:
            file_descriptor = os.open(name, file_flags, dir_fd=descriptor)
        except OSError as error:
            raise ValueError(
                f"{entry_context} must be a non-symlink regular file"
            ) from error
        try:
            before = os.fstat(file_descriptor)
            chunks: list[bytes] = []
            while True:
                chunk = os.read(file_descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            after = os.fstat(file_descriptor)
            final = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        finally:
            os.close(file_descriptor)
        data = b"".join(chunks)
        if (
            _stable_metadata(before) != _stable_metadata(after)
            or _stable_metadata(final) != _stable_metadata(after)
            or len(data) != before.st_size
        ):
            raise ValueError(f"{entry_context} changed while it was read")
        expected_mode = _swift_package_file_mode(relative)
        if stat.S_IMODE(before.st_mode) != expected_mode:
            raise ValueError(
                f"{entry_context} mode must be {expected_mode:04o}"
            )
        files[relative] = data
    after_directory = os.fstat(descriptor)
    final_names = os.listdir(descriptor)
    if (
        _stable_metadata(before_directory) != _stable_metadata(after_directory)
        or sorted(names, key=os.fsencode) != sorted(final_names, key=os.fsencode)
    ):
        raise ValueError(f"{context} changed while it was read")
    return files, directories


def _swift_package_snapshot_identity(
    snapshot: Path, context: str
) -> tuple[int, int, str]:
    flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(snapshot, flags)
    except OSError as error:
        raise ValueError(f"{context} must be a non-symlink directory") from error
    try:
        files, directories = _read_swift_snapshot_directory(descriptor, (), context)
    finally:
        os.close(descriptor)
    _expect(
        directories,
        _swift_package_directories(files),
        f"{context} directory set",
    )
    return _swift_package_identity_from_files(files)


def _expect_swift_package_identity(
    identity: tuple[int, int, str],
    expected: NativeExpectations,
    context: str,
) -> None:
    if (
        expected.swift_package_file_count is None
        or expected.swift_package_size_bytes is None
        or expected.swift_package_fingerprint is None
    ):
        raise ValueError(f"{context}: Swift package identity is not frozen by policy")
    _expect(identity[0], expected.swift_package_file_count, f"{context} file count")
    _expect(identity[1], expected.swift_package_size_bytes, f"{context} size")
    _expect(identity[2], expected.swift_package_fingerprint, f"{context} fingerprint")


def _materialize_swift_package_snapshot(
    output_directory: Path,
    files: Mapping[str, bytes],
) -> tuple[Path, Path, dict[Path, bytes], tuple[int, int, str]]:
    target = output_directory / SWIFT_PACKAGE_SNAPSHOT_PATH
    target.mkdir(mode=0o700)
    directories = _swift_package_directories(files) - {"."}
    for relative in sorted(
        directories,
        key=lambda value: (len(Path(value).parts), value.encode("utf-8")),
    ):
        (target / relative).mkdir(mode=0o700)
    retained: dict[Path, bytes] = {}
    for relative in sorted(files, key=lambda value: value.encode("utf-8")):
        destination = target / relative
        _write_private(destination, files[relative], _swift_package_file_mode(relative))
        retained[destination] = files[relative]
    for relative in sorted(
        directories,
        key=lambda value: (-len(Path(value).parts), value.encode("utf-8")),
    ):
        (target / relative).chmod(0o555)
    target.chmod(0o555)
    identity = _swift_package_snapshot_identity(target, "Swift package snapshot")
    return target, target / "scripts/swift-test.sh", retained, identity


def _read_runtime_dependencies(
    runtime_lib_dir: Path,
    expected: NativeExpectations,
    context: str,
) -> dict[str, bytes]:
    source = runtime_lib_dir.resolve(strict=True)
    metadata = source.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"{context} must be a non-symlink directory")
    expected_names = set(run_mozc_b0_measurement.RUNTIME_DEPENDENCY_FILENAMES)
    _expect(
        {item.name for item in source.iterdir()},
        expected_names,
        f"{context} file set",
    )
    files: dict[str, bytes] = {}
    for name in run_mozc_b0_measurement.RUNTIME_DEPENDENCY_FILENAMES:
        data = _read_regular(source / name, f"{context} {name}")
        identity = expected.runtime_dependencies.get(name)
        if identity is None:
            raise ValueError(f"policy does not pin runtime dependency {name}")
        _expect(len(data), identity[0], f"{context} {name} size")
        _expect(sha256_bytes(data), identity[1], f"{context} {name} SHA-256")
        files[name] = data
    return files


def _prepare_b0_snapshot(
    *,
    server: Path,
    runtime_lib_dir: Path,
    mozc_generation: Path,
    output_directory: Path,
    expected: NativeExpectations,
    include_adapter_corpus: bool,
) -> tuple[Path, Path, Path, dict[Path, bytes], bytes]:
    repository_root = Path(__file__).resolve().parents[2]
    server_data = _read_regular(server.resolve(strict=True), "product server")
    _expect(len(server_data), expected.product_server_size, "product server size")
    _expect(sha256_bytes(server_data), expected.product_server_sha256, "product server SHA-256")

    runtime_source = runtime_lib_dir.resolve(strict=True)
    runtime_metadata = runtime_source.lstat()
    if stat.S_ISLNK(runtime_metadata.st_mode) or not stat.S_ISDIR(runtime_metadata.st_mode):
        raise ValueError("runtime-lib-dir must be a non-symlink directory")
    expected_runtime_names = set(run_mozc_b0_measurement.RUNTIME_DEPENDENCY_FILENAMES)
    _expect({item.name for item in runtime_source.iterdir()}, expected_runtime_names, "runtime dependency file set")
    runtime_bytes: dict[str, bytes] = {}
    for name in run_mozc_b0_measurement.RUNTIME_DEPENDENCY_FILENAMES:
        data = _read_regular(runtime_source / name, f"runtime dependency {name}")
        identity = expected.runtime_dependencies.get(name)
        if identity is None:
            raise ValueError(f"policy does not pin runtime dependency {name}")
        _expect(len(data), identity[0], f"runtime dependency {name} size")
        _expect(sha256_bytes(data), identity[1], f"runtime dependency {name} SHA-256")
        runtime_bytes[name] = data

    generation_source = mozc_generation.resolve(strict=True)
    generation_metadata = generation_source.lstat()
    if stat.S_ISLNK(generation_metadata.st_mode) or not stat.S_ISDIR(generation_metadata.st_mode):
        raise ValueError("mozc-generation must be a non-symlink directory")
    if re.fullmatch(r"sha256-[0-9a-f]{64}", generation_source.name) is None:
        raise ValueError("mozc-generation must use a content-addressed sha256-<digest> name")
    generation_files: dict[str, bytes] = {}
    for name, mode in (
        ("fcitx5-grimodex-mozc-helper", 0o555),
        ("mozc.data", 0o444),
        ("manifest.json", 0o444),
    ):
        source_path = generation_source / name
        data = _read_regular(source_path, f"Mozc generation {name}")
        _expect(stat.S_IMODE(source_path.lstat().st_mode), mode, f"Mozc generation {name} mode")
        generation_files[name] = data
    for artifact_id in ("fcitx5-grimodex-mozc-helper", "mozc.data"):
        identity = expected.artifacts.get(artifact_id)
        if identity is None:
            raise ValueError(f"policy does not pin Mozc artifact {artifact_id}")
        _expect(len(generation_files[artifact_id]), identity[0], f"Mozc artifact {artifact_id} size")
        _expect(sha256_bytes(generation_files[artifact_id]), identity[1], f"Mozc artifact {artifact_id} SHA-256")
    _expect(_runtime_fingerprint(generation_files), expected.artifact_fingerprint, "Mozc generation fingerprint")

    corpus_data = b""
    if include_adapter_corpus:
        corpus_data = _read_regular(repository_root / ADAPTER_CORPUS_PATH, "adapter sentinel corpus")
        _expect(sha256_bytes(corpus_data), ADAPTER_CORPUS_SHA256, "adapter sentinel corpus SHA-256")

    output_directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    runtime_target = output_directory / "runtime"
    runtime_target.mkdir(mode=0o700)
    server_target = runtime_target / "hazkey-server"
    _write_private(server_target, server_data, 0o555)
    library_target = runtime_target / "lib"
    library_target.mkdir(mode=0o700)
    retained: dict[Path, bytes] = {server_target: server_data}
    for name in run_mozc_b0_measurement.RUNTIME_DEPENDENCY_FILENAMES:
        target = library_target / name
        _write_private(target, runtime_bytes[name], 0o555)
        retained[target] = runtime_bytes[name]
    library_target.chmod(0o555)
    runtime_target.chmod(0o555)

    inputs_target = output_directory / "inputs"
    inputs_target.mkdir(mode=0o700)
    generation_target = inputs_target / B0_GENERATION_NAME
    generation_target.mkdir(mode=0o755)
    for name, mode in (
        ("fcitx5-grimodex-mozc-helper", 0o555),
        ("mozc.data", 0o444),
        ("manifest.json", 0o444),
    ):
        target = generation_target / name
        _write_private(target, generation_files[name], mode)
        retained[target] = generation_files[name]
    if include_adapter_corpus:
        corpus_target = inputs_target / "conversion-quality-v1.tsv"
        _write_private(corpus_target, corpus_data, 0o444)
        retained[corpus_target] = corpus_data
    inputs_target.chmod(0o555)
    return server_target, library_target, generation_target, retained, server_data


def _verify_retained_snapshot(retained: Mapping[Path, bytes]) -> None:
    for path, expected in retained.items():
        _expect(_read_regular(path, f"retained snapshot {path.name}"), expected, f"retained snapshot {path.name}")


def _classify_process(
    process_identifier: int,
    server_path: Path,
    server_identity: tuple[int, str],
    helper_identity: tuple[int, str],
) -> tuple[str, tuple[int, int], tuple[int, str]] | None:
    try:
        first = _process_identity(process_identifier)
        proc_executable = Path("/proc") / str(process_identifier) / "exe"
        executable = Path(os.readlink(proc_executable))
        if executable.name not in {
            server_path.name,
            "fcitx5-grimodex-mozc-helper",
        }:
            return None
        descriptor = os.open(
            proc_executable,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            process_metadata = os.fstat(descriptor)
            server_metadata = server_path.stat()
            if (
                process_metadata.st_dev,
                process_metadata.st_ino,
            ) == (
                server_metadata.st_dev,
                server_metadata.st_ino,
            ):
                classified = ("servers", first, server_identity)
            elif (
                executable.name == "fcitx5-grimodex-mozc-helper"
                and process_metadata.st_size == helper_identity[0]
            ):
                chunks: list[bytes] = []
                while True:
                    chunk = os.read(descriptor, 1024 * 1024)
                    if not chunk:
                        break
                    chunks.append(chunk)
                if sha256_bytes(b"".join(chunks)) != helper_identity[1]:
                    return None
                classified = ("helpers", first, helper_identity)
            else:
                return None
        finally:
            os.close(descriptor)
        second = _process_identity(process_identifier)
    except (FileNotFoundError, OSError, ValueError, IndexError):
        return None
    if first != second:
        return None
    return classified


def _observe_process_session(
    session_identifier: int,
    server_path: Path,
    server_identity: tuple[int, str],
    helper_identity: tuple[int, str],
    observed: dict[str, dict[tuple[int, int], tuple[int, str]]],
    stop: threading.Event,
) -> None:
    while True:
        for process_identifier in _session_members(session_identifier):
            classified = _classify_process(
                process_identifier,
                server_path,
                server_identity,
                helper_identity,
            )
            if classified is not None:
                kind, identity, executable_identity = classified
                observed[kind][identity] = executable_identity
        if stop.wait(0.005):
            return


def _run_with_process_audit(
    *,
    command: list[str],
    cwd: Path,
    environment: Mapping[str, str],
    server_path: Path,
    server_identity: tuple[int, str],
    helper_identity: tuple[int, str],
    timeout_seconds: int,
    runner_is_server: bool,
) -> tuple[bytes, bytes, int, dict[str, Any]]:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=dict(environment),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    observed: dict[str, dict[tuple[int, int], tuple[int, str]]] = {
        "servers": {},
        "helpers": {},
    }
    stop = threading.Event()
    observer: threading.Thread | None = None
    timed_out = False
    forced_members: list[int] = []
    group_residues: list[int] = []
    session_residues: list[int] = []
    try:
        runner = _process_identity(process.pid)
        if runner_is_server:
            classified = _classify_process(
                process.pid, server_path, server_identity, helper_identity
            )
            if classified is not None and classified[0] == "servers":
                observed["servers"][classified[1]] = classified[2]
        observer = threading.Thread(
            target=_observe_process_session,
            args=(
                process.pid,
                server_path,
                server_identity,
                helper_identity,
                observed,
                stop,
            ),
            daemon=True,
        )
        observer.start()
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            forced_members = _stop_process_group(process.pid)
            forced_members.extend(_stop_session(process.pid))
            stdout, stderr = process.communicate()
    finally:
        stop.set()
        try:
            if observer is not None and observer.ident is not None:
                observer.join(timeout=2)
        finally:
            try:
                group_residues = _stop_process_group(process.pid)
            finally:
                session_residues = _stop_session(process.pid)
    if observer is not None and observer.is_alive():
        raise RuntimeError("process-session observer did not stop")
    residues = sorted(set(forced_members + group_residues + session_residues))
    audit = {
        "runner": _identity_payload(runner),
        "servers": [
            _identity_payload(identity, observed["servers"][identity])
            for identity in sorted(observed["servers"])
        ],
        "helpers": [
            _identity_payload(identity, observed["helpers"][identity])
            for identity in sorted(observed["helpers"])
        ],
        "process_group_cleanup": not group_residues,
        "session_cleanup": not session_residues,
        "residue_count": len(residues),
    }
    return stdout, stderr, 124 if timed_out else process.returncode, audit


def _controlled_probe_environment() -> dict[str, str]:
    return {
        "GGML_BACKEND_DIR": "./runtime/lib",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "LD_LIBRARY_PATH": "./runtime/lib",
        "PATH": os.defpath,
        "TZ": "UTC",
    }


def _controlled_swift_environment() -> dict[str, str]:
    environment = {
        name: value
        for name, value in os.environ.items()
        if name in {"HOME", "LANG", "LC_ALL", "TMPDIR"}
    }
    environment["PATH"] = os.defpath
    return environment


def _write_native_and_record(
    *,
    suite_id: str,
    output_directory: Path,
    native_name: str,
    record_name: str,
    native: Mapping[str, Any],
    expected: NativeExpectations,
) -> tuple[Path, Path, bool]:
    native_path = output_directory / native_name
    native_bytes = (
        json.dumps(native, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    _write_private(native_path, native_bytes)
    record_path = output_directory / record_name
    record = build_record(
        suite_id,
        native_path,
        native_bytes,
        artifact_fingerprint=expected.artifact_fingerprint,
        recovery_fixture_identity_value=expected.recovery_fixture_identity,
    )
    _write_private(
        record_path,
        (json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"),
    )
    try:
        validate_native_result(
            suite_id,
            native_bytes,
            str(native_path),
            expected,
            native_path=native_path,
        )
    except ValueError:
        passed = False
    else:
        passed = True
    descriptor = os.open(output_directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return native_path, record_path, passed


def run_adapter(
    *,
    server: Path,
    runtime_lib_dir: Path,
    mozc_generation: Path,
    output_directory: Path,
    policy_path: Path,
    timeout_seconds: int,
) -> tuple[Path, Path, bool]:
    expected = expectations_from_policy(policy_path.resolve(strict=True), ADAPTER_SOAK_ID)
    orchestrator_before = _read_regular(Path(__file__).resolve(), "stability orchestrator")
    server_target, _, _, retained, server_data = _prepare_b0_snapshot(
        server=server,
        runtime_lib_dir=runtime_lib_dir,
        mozc_generation=mozc_generation,
        output_directory=output_directory,
        expected=expected,
        include_adapter_corpus=True,
    )
    helper_identity = expected.artifacts["fcitx5-grimodex-mozc-helper"]
    raw, stderr, exit_code, audit = _run_with_process_audit(
        command=list(ADAPTER_PROBE_COMMAND),
        cwd=output_directory,
        environment=_controlled_probe_environment(),
        server_path=server_target,
        server_identity=(expected.product_server_size, expected.product_server_sha256),
        helper_identity=helper_identity,
        timeout_seconds=timeout_seconds,
        runner_is_server=True,
    )
    raw_path = output_directory / ADAPTER_RAW_NAME
    stderr_path = output_directory / "adapter-soak.stderr"
    _write_private(raw_path, raw)
    _write_private(stderr_path, stderr)
    _verify_retained_snapshot(retained)
    _expect(_read_regular(Path(__file__).resolve(), "stability orchestrator after run"), orchestrator_before, "stability orchestrator after run")
    native = {
        "schema": ADAPTER_SOAK_SCHEMA,
        "producer": {"path": ORCHESTRATOR_PATH, "sha256": sha256_bytes(orchestrator_before)},
        "product_source_ref": expected.product_source_ref,
        "product_server": {"size_bytes": len(server_data), "sha256": sha256_bytes(server_data)},
        "artifact": {"kind": "b0", "fingerprint": expected.artifact_fingerprint},
        "execution": {
            "command": list(ADAPTER_PROBE_COMMAND),
            "exit_code": exit_code,
            "process_audit": audit,
        },
        "raw_abprobe": {"path": raw_path.name, "sha256": sha256_bytes(raw)},
        "stderr": {"path": stderr_path.name, "sha256": sha256_bytes(stderr)},
    }
    return _write_native_and_record(
        suite_id=ADAPTER_SOAK_ID,
        output_directory=output_directory,
        native_name="adapter-soak-result.json",
        record_name="adapter-soak-150k.json",
        native=native,
        expected=expected,
    )


def run_protocol_steady(
    *,
    server: Path,
    runtime_lib_dir: Path,
    mozc_generation: Path,
    dictionary: Path,
    output_directory: Path,
    policy_path: Path,
    timeout_seconds: int,
) -> tuple[Path, Path, bool]:
    repository_root = Path(__file__).resolve().parents[2]
    expected = expectations_from_policy(policy_path.resolve(strict=True), PROTOCOL_STEADY_ID)
    orchestrator_before = _read_regular(Path(__file__).resolve(), "stability orchestrator")
    package_files = _read_swift_package_inputs(repository_root)
    package_identity = _swift_package_identity_from_files(package_files)
    _expect_swift_package_identity(
        package_identity, expected, "Protocol v2 Swift package"
    )
    source_relative = Path(PROTOCOL_BENCHMARK_SOURCE_PATH).relative_to(
        SWIFT_PACKAGE_ROOT
    ).as_posix()
    source_before = package_files[source_relative]
    if expected.native_producer_sha256 is None:
        raise ValueError("Protocol v2 benchmark source hash is not frozen")
    _expect(sha256_bytes(source_before), expected.native_producer_sha256, "Protocol v2 benchmark source SHA-256")
    runner_relative = Path(SWIFT_TEST_RUNNER_PATH).relative_to(
        SWIFT_PACKAGE_ROOT
    ).as_posix()
    runner_before = package_files[runner_relative]
    _expect(
        sha256_bytes(runner_before),
        expected.execution_runner_sha256,
        "Protocol v2 Swift test runner SHA-256",
    )
    dictionary_path = dictionary.resolve(strict=True)
    if not dictionary_path.is_dir():
        raise ValueError("dictionary must be a directory")
    dictionary_before = _dictionary_fingerprint(
        dictionary_path, "Protocol v2 benchmark dictionary"
    )
    if expected.baseline_resource_fingerprint is None:
        raise ValueError("policy does not freeze the baseline dictionary fingerprint")
    _expect(
        dictionary_before,
        expected.baseline_resource_fingerprint,
        "Protocol v2 benchmark dictionary fingerprint before run",
    )
    server_target, library_target, generation_target, retained, server_data = _prepare_b0_snapshot(
        server=server,
        runtime_lib_dir=runtime_lib_dir,
        mozc_generation=mozc_generation,
        output_directory=output_directory,
        expected=expected,
        include_adapter_corpus=False,
    )
    package_target, runner_path, package_retained, snapshot_identity = (
        _materialize_swift_package_snapshot(output_directory, package_files)
    )
    _expect(snapshot_identity, package_identity, "Protocol v2 Swift package snapshot")
    retained.update(package_retained)
    scratch = output_directory / "swift-scratch"
    scratch.mkdir(mode=0o700)
    benchmark_path = output_directory / PROTOCOL_RAW_NAME
    environment = _controlled_swift_environment()
    environment.update(
        {
            "LD_LIBRARY_PATH": str(library_target.resolve()),
            "GGML_BACKEND_DIR": str(library_target.resolve()),
            "FCITX5_GRIMODEX_DICTIONARY": str(dictionary_path),
            "GRIMODEX_PROCESS_E2E_SERVER": str(server_target.resolve()),
            "GRIMODEX_PROCESS_E2E_MOZC_HELPER": str((generation_target / "fcitx5-grimodex-mozc-helper").resolve()),
            "GRIMODEX_PROCESS_E2E_MOZC_DATA": str((generation_target / "mozc.data").resolve()),
            "GRIMODEX_PROCESS_E2E_AB_WARMUPS": "5",
            "GRIMODEX_PROCESS_E2E_AB_ITERATIONS": "100",
            "GRIMODEX_PROCESS_E2E_AB_SOURCE_REF": expected.product_source_ref,
            "GRIMODEX_PROCESS_E2E_AB_OUTPUT": str(benchmark_path.resolve()),
            "GRIMODEX_PROCESS_E2E_AB_BUILD_CONFIGURATION": "formal-stability",
            "GRIMODEX_PROCESS_E2E_AB_TOOLCHAIN": "swift-test.sh",
            "SWIFT_SCRATCH_PATH": str(scratch),
        }
    )
    command = [
        str(runner_path),
        "--filter",
        PROTOCOL_STEADY_TEST,
    ]
    stdout, stderr, exit_code, audit = _run_with_process_audit(
        command=command,
        cwd=package_target,
        environment=environment,
        server_path=server_target,
        server_identity=(expected.product_server_size, expected.product_server_sha256),
        helper_identity=expected.artifacts["fcitx5-grimodex-mozc-helper"],
        timeout_seconds=timeout_seconds,
        runner_is_server=False,
    )
    stdout_path = output_directory / "protocol-v2-steady.stdout"
    stderr_path = output_directory / "protocol-v2-steady.stderr"
    _write_private(stdout_path, stdout)
    _write_private(stderr_path, stderr)
    if benchmark_path.exists():
        benchmark = _read_regular(benchmark_path, "Protocol v2 benchmark output")
        benchmark_path.chmod(0o600)
    else:
        benchmark = b""
        _write_private(benchmark_path, benchmark)
    combined = (stdout + b"\n" + stderr).decode("utf-8", errors="replace")
    test_method = PROTOCOL_STEADY_TEST.rsplit("/", 1)[-1]
    named_lines = [line for line in combined.splitlines() if test_method in line]
    skipped = not any(re.search(r"\bpassed\b", line) for line in named_lines) or any(
        re.search(r"\bskipped\b", line) for line in named_lines
    )
    _verify_retained_snapshot(retained)
    _expect(
        _swift_package_snapshot_identity(
            package_target, "Protocol v2 Swift package after run"
        ),
        package_identity,
        "Protocol v2 Swift package after run",
    )
    dictionary_after = _dictionary_fingerprint(
        dictionary_path, "Protocol v2 benchmark dictionary after run"
    )
    _expect(
        dictionary_after,
        expected.baseline_resource_fingerprint,
        "Protocol v2 benchmark dictionary fingerprint after run",
    )
    _expect(_read_regular(Path(__file__).resolve(), "stability orchestrator after run"), orchestrator_before, "stability orchestrator after run")
    native = {
        "schema": PROTOCOL_STEADY_SCHEMA,
        "producer": {"path": ORCHESTRATOR_PATH, "sha256": sha256_bytes(orchestrator_before)},
        "product_source_ref": expected.product_source_ref,
        "product_server": {"size_bytes": len(server_data), "sha256": sha256_bytes(server_data)},
        "artifact": {"kind": "b0", "fingerprint": expected.artifact_fingerprint},
        "benchmark_source": {
            "path": PROTOCOL_BENCHMARK_SOURCE_PATH,
            "snapshot_path": f"{SWIFT_PACKAGE_SNAPSHOT_PATH}/{source_relative}",
            "size_bytes": len(source_before),
            "sha256": sha256_bytes(source_before),
        },
        "test_runner": {
            "path": expected.execution_runner_path,
            "snapshot_path": f"{SWIFT_PACKAGE_SNAPSHOT_PATH}/{runner_relative}",
            "size_bytes": len(runner_before),
            "sha256": sha256_bytes(runner_before),
        },
        "swift_package": {
            "path": SWIFT_PACKAGE_SNAPSHOT_PATH,
            "file_count": package_identity[0],
            "size_bytes": package_identity[1],
            "fingerprint": package_identity[2],
            "post_run_verified": True,
        },
        "dictionary": {
            "path": str(dictionary_path),
            "fingerprint_before": dictionary_before,
            "fingerprint_after": dictionary_after,
        },
        "execution": {
            "command": list(PROTOCOL_TEST_COMMAND),
            "scratch_path": "swift-scratch",
            "exit_code": exit_code,
            "skipped": skipped,
            "process_audit": audit,
        },
        "benchmark": {"path": benchmark_path.name, "sha256": sha256_bytes(benchmark)},
        "stdout": {"path": stdout_path.name, "sha256": sha256_bytes(stdout)},
        "stderr": {"path": stderr_path.name, "sha256": sha256_bytes(stderr)},
    }
    return _write_native_and_record(
        suite_id=PROTOCOL_STEADY_ID,
        output_directory=output_directory,
        native_name="protocol-v2-steady-result.json",
        record_name="protocol-v2-steady-1500.json",
        native=native,
        expected=expected,
    )


def run_recovery(
    *,
    server: Path,
    output_directory: Path,
    runtime_lib_dir: Path,
    policy_path: Path,
    timeout_seconds: int,
) -> tuple[Path, Path, bool]:
    repository_root = Path(__file__).resolve().parents[2]
    expected = expectations_from_policy(
        policy_path.resolve(strict=True), PROTOCOL_RECOVERY_ID
    )
    _expect(expected.product_source_ref, PRODUCT_SOURCE_REF, "recovery policy product source")
    server_data = _read_regular(server.resolve(strict=True), "product server")
    server_sha = sha256_bytes(server_data)
    _expect(len(server_data), expected.product_server_size, "product server size")
    _expect(server_sha, expected.product_server_sha256, "product server SHA-256")
    package_files = _read_swift_package_inputs(repository_root)
    package_identity = _swift_package_identity_from_files(package_files)
    _expect_swift_package_identity(package_identity, expected, "recovery Swift package")
    source_relative = Path(RECOVERY_SOURCE_PATH).relative_to(
        SWIFT_PACKAGE_ROOT
    ).as_posix()
    source_before = package_files[source_relative]
    source_sha = sha256_bytes(source_before)
    if expected.native_producer_sha256 is None:
        raise ValueError("recovery policy does not freeze the fixture source SHA-256")
    _expect(source_sha, expected.native_producer_sha256, "recovery fixture source SHA-256")
    if expected.recovery_fixture_identity is None:
        raise ValueError("recovery policy does not freeze the fixture identity")
    _expect(
        recovery_fixture_identity(source_sha),
        expected.recovery_fixture_identity,
        "recovery fixture identity",
    )
    runtime_files = _read_runtime_dependencies(
        runtime_lib_dir,
        expected,
        "recovery runtime-lib-dir",
    )
    runner_relative = Path(SWIFT_TEST_RUNNER_PATH).relative_to(
        SWIFT_PACKAGE_ROOT
    ).as_posix()
    runner_before = package_files[runner_relative]
    _expect(
        sha256_bytes(runner_before),
        expected.execution_runner_sha256,
        "recovery Swift test runner SHA-256",
    )
    orchestrator_before = _read_regular(Path(__file__).resolve(), "stability orchestrator")
    output_directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    package_target, runner_path, package_retained, snapshot_identity = (
        _materialize_swift_package_snapshot(output_directory, package_files)
    )
    _expect(snapshot_identity, package_identity, "recovery Swift package snapshot")
    scratch = output_directory / "swift-scratch"
    scratch.mkdir(mode=0o700)
    snapshot = output_directory / "product-server"
    _write_private(snapshot, server_data, 0o500)
    runtime_snapshot = output_directory / "runtime-lib"
    runtime_snapshot.mkdir(mode=0o700)
    retained_runtime: dict[Path, bytes] = {}
    for name in run_mozc_b0_measurement.RUNTIME_DEPENDENCY_FILENAMES:
        target = runtime_snapshot / name
        _write_private(target, runtime_files[name], 0o555)
        retained_runtime[target] = runtime_files[name]
    runtime_snapshot.chmod(0o555)

    environment = _controlled_swift_environment()
    environment["GRIMODEX_PROCESS_E2E_SERVER"] = str(snapshot.resolve())
    environment["LD_LIBRARY_PATH"] = str(runtime_snapshot.resolve())
    environment["GGML_BACKEND_DIR"] = str(runtime_snapshot.resolve())
    environment["SWIFT_SCRATCH_PATH"] = str(scratch.resolve())

    subchecks: list[dict[str, Any]] = []
    all_passed = True
    for check_id, test_name, fixture_mode in RECOVERY_SUBCHECKS:
        command = [
            str(runner_path),
            "--filter",
            test_name,
        ]
        process = subprocess.Popen(
            command,
            cwd=package_target,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        observed_session_members: set[int] = set()
        observer_stop = threading.Event()
        observer = threading.Thread(
            target=_observe_session_members,
            args=(process.pid, observed_session_members, observer_stop),
            daemon=True,
        )
        timed_out = False
        forced_group: list[int] = []
        forced_session: list[int] = []
        group_residues: list[int] = []
        session_residues: list[int] = []
        try:
            observer.start()
            try:
                stdout, stderr = process.communicate(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                forced_group = _stop_process_group(process.pid)
                forced_session = _stop_session(process.pid)
                stdout, stderr = process.communicate()
        finally:
            observer_stop.set()
            try:
                if observer.ident is not None:
                    observer.join(timeout=2)
            finally:
                try:
                    group_residues = _stop_process_group(process.pid)
                finally:
                    session_residues = _stop_session(process.pid)
        if observer.ident is not None and observer.is_alive():
            raise RuntimeError("recovery process-session observer did not stop")
        residues = sorted(
            set(forced_group + forced_session + group_residues + session_residues)
        )
        stdout_path = output_directory / f"{check_id}.stdout"
        stderr_path = output_directory / f"{check_id}.stderr"
        _write_private(stdout_path, stdout)
        _write_private(stderr_path, stderr)
        combined = (stdout + b"\n" + stderr).decode("utf-8", errors="replace")
        test_method = test_name.rsplit("/", 1)[-1]
        named_lines = [
            line for line in combined.splitlines() if test_method in line
        ]
        skipped = any(re.search(r"\bskipped\b", line) for line in named_lines)
        named_pass = any(
            re.search(r"\bpassed\b", line) for line in named_lines
        )
        exit_code = 124 if timed_out else process.returncode
        process_group_clean = not forced_group and not group_residues
        session_clean = not forced_session and not session_residues
        passed = (
            exit_code == 0
            and named_pass
            and not skipped
            and process_group_clean
            and session_clean
            and not residues
        )
        all_passed = all_passed and passed
        subchecks.append(
            {
                "id": check_id,
                "test_name": test_name,
                "fixture_mode": fixture_mode,
                "command": [
                    "hazkey-server/scripts/swift-test.sh",
                    "--filter",
                    test_name,
                ],
                "exit_code": exit_code,
                "skipped": skipped or not named_pass,
                "cleanup": {
                    "process_group": process_group_clean,
                    "session": session_clean,
                    "residue_count": len(residues),
                },
                "stdout": {
                    "path": stdout_path.name,
                    "sha256": sha256_bytes(stdout),
                },
                "stderr": {
                    "path": stderr_path.name,
                    "sha256": sha256_bytes(stderr),
                },
            }
        )

    _verify_retained_snapshot(package_retained)
    _verify_retained_snapshot(retained_runtime)
    _expect(
        _swift_package_snapshot_identity(
            package_target, "recovery Swift package after run"
        ),
        package_identity,
        "recovery Swift package after run",
    )
    if _read_regular(Path(__file__).resolve(), "stability orchestrator after run") != orchestrator_before:
        raise ValueError("stability orchestrator changed during the run")
    native_path = output_directory / "recovery-result.json"
    native = {
        "schema": RECOVERY_SCHEMA,
        "producer": {
            "path": ORCHESTRATOR_PATH,
            "sha256": sha256_bytes(orchestrator_before),
        },
        "product_source_ref": PRODUCT_SOURCE_REF,
        "product_server": {
            "size_bytes": len(server_data),
            "sha256": server_sha,
        },
        "artifact": {
            "kind": "fault-fixture",
            "fixture_identity": recovery_fixture_identity(source_sha),
        },
        "fixture_source": {
            "path": RECOVERY_SOURCE_PATH,
            "snapshot_path": f"{SWIFT_PACKAGE_SNAPSHOT_PATH}/{source_relative}",
            "size_bytes": len(source_before),
            "sha256": source_sha,
        },
        "test_runner": {
            "path": expected.execution_runner_path,
            "snapshot_path": f"{SWIFT_PACKAGE_SNAPSHOT_PATH}/{runner_relative}",
            "size_bytes": len(runner_before),
            "sha256": sha256_bytes(runner_before),
        },
        "swift_package": {
            "path": SWIFT_PACKAGE_SNAPSHOT_PATH,
            "file_count": package_identity[0],
            "size_bytes": package_identity[1],
            "fingerprint": package_identity[2],
            "post_run_verified": True,
        },
        "scratch_path": "swift-scratch",
        "runtime_dependencies": {
            "path": "runtime-lib",
            "files": [
                {
                    "path": name,
                    "size_bytes": len(runtime_files[name]),
                    "sha256": sha256_bytes(runtime_files[name]),
                }
                for name in run_mozc_b0_measurement.RUNTIME_DEPENDENCY_FILENAMES
            ],
            "post_run_verified": True,
        },
        "subchecks": subchecks,
        "residue_count": sum(
            item["cleanup"]["residue_count"] for item in subchecks
        ),
    }
    native_bytes = (
        json.dumps(native, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    _write_private(native_path, native_bytes)
    validate_native_result(
        PROTOCOL_RECOVERY_ID,
        native_bytes,
        str(native_path),
        expected,
        native_path=native_path,
    )
    record_path = output_directory / "protocol-v2-recovery.json"
    record = build_record(
        PROTOCOL_RECOVERY_ID,
        native_path,
        native_bytes,
        artifact_fingerprint=expected.artifact_fingerprint,
        recovery_fixture_identity_value=expected.recovery_fixture_identity,
    )
    _write_private(
        record_path,
        (json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"),
    )
    directory_descriptor = os.open(output_directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
    return native_path, record_path, all_passed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bind one native Mozc B0 stability result without trusting aggregate counts."
    )
    subparsers = parser.add_subparsers(dest="operation", required=True)
    collect = subparsers.add_parser("collect")
    collect.add_argument("--suite-id", choices=SUITE_IDS, required=True)
    collect.add_argument("--native-result", type=Path, required=True)
    collect.add_argument("--output", type=Path, required=True)
    collect.add_argument(
        "--policy",
        type=Path,
        default=Path(__file__).resolve().parents[2] / DEFAULT_POLICY_PATH,
    )
    adapter = subparsers.add_parser("run-adapter")
    adapter.add_argument("--server", type=Path, required=True)
    adapter.add_argument("--runtime-lib-dir", type=Path, required=True)
    adapter.add_argument("--mozc-generation", type=Path, required=True)
    adapter.add_argument("--output-directory", type=Path, required=True)
    adapter.add_argument(
        "--policy",
        type=Path,
        default=Path(__file__).resolve().parents[2] / DEFAULT_POLICY_PATH,
    )
    adapter.add_argument("--timeout-seconds", type=int, default=900)
    protocol = subparsers.add_parser("run-protocol-steady")
    protocol.add_argument("--server", type=Path, required=True)
    protocol.add_argument("--runtime-lib-dir", type=Path, required=True)
    protocol.add_argument("--mozc-generation", type=Path, required=True)
    protocol.add_argument("--dictionary", type=Path, required=True)
    protocol.add_argument("--output-directory", type=Path, required=True)
    protocol.add_argument(
        "--policy",
        type=Path,
        default=Path(__file__).resolve().parents[2] / DEFAULT_POLICY_PATH,
    )
    protocol.add_argument("--timeout-seconds", type=int, default=900)
    recovery = subparsers.add_parser("run-recovery")
    recovery.add_argument("--server", type=Path, required=True)
    recovery.add_argument("--output-directory", type=Path, required=True)
    recovery.add_argument("--runtime-lib-dir", type=Path, required=True)
    recovery.add_argument(
        "--policy",
        type=Path,
        default=Path(__file__).resolve().parents[2] / DEFAULT_POLICY_PATH,
    )
    recovery.add_argument("--timeout-seconds", type=int, default=900)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.operation in {"run-adapter", "run-protocol-steady"}:
            if args.timeout_seconds < 1 or args.timeout_seconds > 86_400:
                raise ValueError("--timeout-seconds must be between 1 and 86400")
            if args.operation == "run-adapter":
                _, _, passed = run_adapter(
                    server=args.server,
                    runtime_lib_dir=args.runtime_lib_dir,
                    mozc_generation=args.mozc_generation,
                    output_directory=args.output_directory,
                    policy_path=args.policy,
                    timeout_seconds=args.timeout_seconds,
                )
            else:
                _, _, passed = run_protocol_steady(
                    server=args.server,
                    runtime_lib_dir=args.runtime_lib_dir,
                    mozc_generation=args.mozc_generation,
                    dictionary=args.dictionary,
                    output_directory=args.output_directory,
                    policy_path=args.policy,
                    timeout_seconds=args.timeout_seconds,
                )
            return 0 if passed else 1
        if args.operation == "run-recovery":
            if args.timeout_seconds < 1 or args.timeout_seconds > 86_400:
                raise ValueError("--timeout-seconds must be between 1 and 86400")
            _, _, passed = run_recovery(
                server=args.server,
                output_directory=args.output_directory,
                runtime_lib_dir=args.runtime_lib_dir,
                policy_path=args.policy,
                timeout_seconds=args.timeout_seconds,
            )
            return 0 if passed else 1
        native_path = args.native_result.resolve(strict=True)
        output_path = args.output.resolve()
        if native_path.parent != output_path.parent:
            raise ValueError("native result and record output must share one directory")
        native_bytes = _read_regular(native_path, "native result")
        expectations = expectations_from_policy(
            args.policy.resolve(strict=True), args.suite_id
        )
        validate_native_result(
            args.suite_id,
            native_bytes,
            str(native_path),
            expectations,
            native_path=native_path,
        )
        record = build_record(
            args.suite_id,
            native_path,
            native_bytes,
            artifact_fingerprint=expectations.artifact_fingerprint,
            recovery_fixture_identity_value=expectations.recovery_fixture_identity,
        )
        rendered = json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2)
        atomic_publish(output_path, (rendered + "\n").encode("utf-8"))
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
