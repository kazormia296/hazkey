from __future__ import annotations

from collections.abc import Callable
import copy
import hashlib
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT))

from tools.dictionary import blind_conversion_ab  # noqa: E402
from tools.dictionary import build_frozen_corpus  # noqa: E402
from tools.dictionary import evaluate_mozc_b0_gate as gate  # noqa: E402
from tools.dictionary import run_mozc_b0_measurement as acquisition  # noqa: E402
from tools.dictionary import run_mozc_b0_stability as stability  # noqa: E402


POLICY_FIXTURE = (
    REPOSITORY_ROOT
    / "hazkey-server/Tests/grimodex-spike/Fixtures/mozc-adoption-v1/b0-policy.json"
)


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def check(result: list[dict[str, object]], check_id: str) -> dict[str, object]:
    return next(item for item in result if item["id"] == check_id)


class MetricBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = gate.GatePolicy(
            total_cases=256,
            categories=dict(gate.EXPECTED_CATEGORIES),
            minimum_net_basis_points=-300,
            minimum_net_cases=-7,
            minimum_top1_delta_basis_points=-800,
            minimum_top10_delta_basis_points=-1200,
            minimum_category_top1_delta_basis_points=-1000,
            protected_required=16,
            maximum_both_bad=12,
            maximum_warm_p95_ratio_basis_points=5000,
            maximum_pss_ratio_basis_points=12500,
            required_stability_ids=("soak", "restart"),
        )

    def metrics(self) -> dict[str, object]:
        categories = {
            name: {"cases": total, "top1_hits": total}
            for name, total in gate.EXPECTED_CATEGORIES.items()
        }
        return {
            "cases": 256,
            "human": {"wins": 0, "losses": 0, "ties": 256, "both_bad": 0},
            "quality": {
                backend: {
                    "cases": 256,
                    "top1_hits": 256,
                    "top10_hits": 256,
                    "categories": copy.deepcopy(categories),
                }
                for backend in ("hazkey", "mozc")
            },
            "warm_latency_p95_ms": {"hazkey": "1", "mozc": "0.5"},
            "total_pss_kib": {"hazkey": 100, "mozc": 125},
            "stability": {"soak": True, "restart": True},
        }

    def _set_category_hits(
        self, metrics: dict[str, object], backend: str, category: str, hits: int
    ) -> None:
        quality = metrics["quality"]  # type: ignore[index]
        payload = quality[backend]  # type: ignore[index]
        old = payload["categories"][category]["top1_hits"]  # type: ignore[index]
        payload["categories"][category]["top1_hits"] = hits  # type: ignore[index]
        payload["top1_hits"] += hits - old  # type: ignore[index]

    def test_human_net_boundary_minus_seven_passes_minus_eight_fails(self) -> None:
        passing = self.metrics()
        passing["human"] = {"wins": 0, "losses": 7, "ties": 249, "both_bad": 0}
        failing = self.metrics()
        failing["human"] = {"wins": 0, "losses": 8, "ties": 248, "both_bad": 0}
        for check_id in (
            "human-net-preference-basis-points",
            "human-net-preference-cases",
        ):
            self.assertTrue(check(gate.evaluate_metrics(self.policy, passing), check_id)["passed"])
            self.assertFalse(check(gate.evaluate_metrics(self.policy, failing), check_id)["passed"])

    def test_top1_boundary_minus_twenty_passes_minus_twenty_one_fails(self) -> None:
        passing = self.metrics()
        self._set_category_hits(passing, "mozc", "ajimee-unconditional", 80)
        failing = self.metrics()
        self._set_category_hits(failing, "mozc", "ajimee-unconditional", 79)
        self.assertTrue(check(gate.evaluate_metrics(self.policy, passing), "top1-delta")["passed"])
        self.assertFalse(check(gate.evaluate_metrics(self.policy, failing), "top1-delta")["passed"])

    def test_top10_boundary_minus_thirty_passes_minus_thirty_one_fails(self) -> None:
        passing = self.metrics()
        passing["quality"]["mozc"]["top10_hits"] = 226  # type: ignore[index]
        # Top-1 must remain no larger than Top-10.
        self._set_category_hits(passing, "mozc", "ajimee-unconditional", 70)
        failing = self.metrics()
        failing["quality"]["mozc"]["top10_hits"] = 225  # type: ignore[index]
        self._set_category_hits(failing, "mozc", "ajimee-unconditional", 69)
        self.assertTrue(check(gate.evaluate_metrics(self.policy, passing), "top10-delta")["passed"])
        self.assertFalse(check(gate.evaluate_metrics(self.policy, failing), "top10-delta")["passed"])

    def test_both_bad_twelve_passes_thirteen_fails(self) -> None:
        passing = self.metrics()
        passing["human"] = {"wins": 0, "losses": 0, "ties": 244, "both_bad": 12}
        failing = self.metrics()
        failing["human"] = {"wins": 0, "losses": 0, "ties": 243, "both_bad": 13}
        self.assertTrue(check(gate.evaluate_metrics(self.policy, passing), "both-bad")["passed"])
        self.assertFalse(check(gate.evaluate_metrics(self.policy, failing), "both-bad")["passed"])

    def test_decimal_latency_half_exact_passes_any_amount_over_fails(self) -> None:
        passing = self.metrics()
        failing = self.metrics()
        failing["warm_latency_p95_ms"] = {
            "hazkey": "1",
            "mozc": "0.5000000000000000000000000001",
        }
        self.assertTrue(check(gate.evaluate_metrics(self.policy, passing), "warm-latency-p95-ratio")["passed"])
        self.assertFalse(check(gate.evaluate_metrics(self.policy, failing), "warm-latency-p95-ratio")["passed"])

    def test_pss_one_point_two_five_exact_passes_over_fails(self) -> None:
        passing = self.metrics()
        failing = self.metrics()
        failing["total_pss_kib"] = {"hazkey": 100, "mozc": 126}
        self.assertTrue(check(gate.evaluate_metrics(self.policy, passing), "total-pss-ratio")["passed"])
        self.assertFalse(check(gate.evaluate_metrics(self.policy, failing), "total-pss-ratio")["passed"])

    def test_protected_sixteen_passes_fifteen_fails(self) -> None:
        passing = self.metrics()
        failing = self.metrics()
        self._set_category_hits(failing, "mozc", "protected", 15)
        self.assertTrue(check(gate.evaluate_metrics(self.policy, passing), "protected-cases")["passed"])
        self.assertFalse(check(gate.evaluate_metrics(self.policy, failing), "protected-cases")["passed"])

    def test_every_category_uses_its_own_denominator(self) -> None:
        for category, total in gate.EXPECTED_CATEGORIES.items():
            with self.subTest(category=category, total=total):
                allowed_loss = total // 10
                passing = self.metrics()
                self._set_category_hits(passing, "mozc", category, total - allowed_loss)
                failing = self.metrics()
                self._set_category_hits(failing, "mozc", category, total - allowed_loss - 1)
                check_id = f"category-top1-delta:{category}"
                self.assertTrue(check(gate.evaluate_metrics(self.policy, passing), check_id)["passed"])
                self.assertFalse(check(gate.evaluate_metrics(self.policy, failing), check_id)["passed"])

    def test_stability_missing_unknown_and_failure_fail_closed(self) -> None:
        for stability in (
            {"soak": True},
            {"soak": True, "restart": True, "unknown": True},
        ):
            metrics = self.metrics()
            metrics["stability"] = stability
            with self.assertRaisesRegex(ValueError, "stability IDs"):
                gate.evaluate_metrics(self.policy, metrics)
        metrics = self.metrics()
        metrics["stability"] = {"soak": True, "restart": False}
        self.assertFalse(check(gate.evaluate_metrics(self.policy, metrics), "stability:restart")["passed"])


class RawEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.synthetic_manifest_sha = digest(b"synthetic Mozc manifest\n")
        self.synthetic_candidate_fingerprint = stability._runtime_fingerprint_from_hashes(
            {
                "fcitx5-grimodex-mozc-helper": digest(b"helper"),
                "manifest.json": self.synthetic_manifest_sha,
                "mozc.data": digest(b"data"),
            }
        )
        self._fingerprint_patch = mock.patch.object(
            stability,
            "B0_RESOURCE_FINGERPRINT",
            self.synthetic_candidate_fingerprint,
        )
        self._manifest_patch = mock.patch.object(
            stability,
            "B0_MANIFEST_SHA256",
            self.synthetic_manifest_sha,
        )
        self._fingerprint_patch.start()
        self._manifest_patch.start()
        self.addCleanup(self._manifest_patch.stop)
        self.addCleanup(self._fingerprint_patch.stop)

    maxDiff = None

    def _write_tsv(self, path: Path, rows: list[dict[str, str]]) -> None:
        path.write_text(
            "id\treading\texpected\tcategory\n"
            + "".join(
                f"{row['id']}\t{row['reading']}\t{row['expected']}\t{row['category']}\n"
                for row in rows
            ),
            encoding="utf-8",
        )

    def _ensure_swift_package_snapshot(
        self, root: Path
    ) -> tuple[int, int, str]:
        target = root / stability.SWIFT_PACKAGE_SNAPSHOT_PATH
        if not target.exists():
            files = stability._read_swift_package_inputs(REPOSITORY_ROOT)
            _, _, _, identity = stability._materialize_swift_package_snapshot(
                root, files
            )
            return identity
        return stability._swift_package_snapshot_identity(
            target, "synthetic Swift package snapshot"
        )

    def _result(
        self,
        row: dict[str, str],
        *,
        backend: str,
        corpus_sha: str,
        resource_fingerprint: str,
        source_ref: str,
        sample_value: float,
    ) -> dict[str, object]:
        samples = [sample_value] * 20
        rss: dict[str, int] = {
            "before_kib": 100,
            "after_kib": 100,
        }
        if backend == "hazkey":
            rss.update({"before_pss_kib": 1000, "after_pss_kib": 1000})
        else:
            rss.update(
                {
                    "before_pss_kib": 600,
                    "after_pss_kib": 600,
                    "backend_before_kib": 100,
                    "backend_after_kib": 100,
                    "backend_before_pss_kib": 650,
                    "backend_after_pss_kib": 650,
                }
            )
        return {
            "schema": "hazkey.ab-probe-result.v3",
            "id": row["id"],
            "reading": row["reading"],
            "category": row["category"],
            "backend": "hazkey-server",
            "backend_version": "fixture-v1",
            "source_ref": source_ref,
            "converter_backend": backend,
            "resource": {
                "kind": "hazkey_dictionary" if backend == "hazkey" else "mozc_runtime_inputs",
                "path": f"/fixture/{backend}",
                "fingerprint": resource_fingerprint,
            },
            "top_k": 10,
            "corpus": {"sha256": corpus_sha, "cases": 256},
            "candidates": [row["expected"]],
            "measurement": {
                "warmups": 5,
                "iterations": 20,
                "latency_ms": {
                    "median": samples[0],
                    "p95": samples[0],
                    "minimum": samples[0],
                    "maximum": samples[0],
                    "samples": samples,
                },
                "rss": rss,
            },
        }

    def _adapter_stability_native(
        self,
        root: Path,
        source_ref: str,
        candidate_fingerprint: str,
        executable: Path,
    ) -> Path:
        corpus_path = REPOSITORY_ROOT / stability.ADAPTER_CORPUS_PATH
        rows = gate.load_corpus_bytes(corpus_path.read_bytes(), str(corpus_path))
        raw_path = root / "adapter-soak-native.jsonl"
        results: list[dict[str, object]] = []
        samples = [1.0] * 10_000
        for row in rows:
            result = self._result(
                row,
                backend="mozc",
                corpus_sha=stability.ADAPTER_CORPUS_SHA256,
                resource_fingerprint=candidate_fingerprint,
                source_ref=source_ref,
                sample_value=1.0,
            )
            result["corpus"] = {
                "sha256": stability.ADAPTER_CORPUS_SHA256,
                "cases": 15,
            }
            measurement = result["measurement"]
            measurement["iterations"] = 10_000  # type: ignore[index]
            measurement["latency_ms"] = {  # type: ignore[index]
                "median": 1.0,
                "p95": 1.0,
                "minimum": 1.0,
                "maximum": 1.0,
                "samples": samples,
            }
            measurement["backend_diagnostics"] = {  # type: ignore[index]
                "process_launch_count": 1,
                "cleanup_failure_count": 0,
            }
            results.append(result)
        raw_path.write_text(
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in results),
            encoding="utf-8",
        )
        stderr_path = root / "adapter-soak.stderr"
        stderr_path.write_bytes(b"")
        path = root / "adapter-soak-result.json"
        path.write_text(
            json.dumps(
                {
                    "schema": stability.ADAPTER_SOAK_SCHEMA,
                    "producer": {
                        "path": stability.ORCHESTRATOR_PATH,
                        "sha256": digest(
                            (REPOSITORY_ROOT / stability.ORCHESTRATOR_PATH).read_bytes()
                        ),
                    },
                    "product_source_ref": source_ref,
                    "product_server": {
                        "size_bytes": executable.stat().st_size,
                        "sha256": digest(executable.read_bytes()),
                    },
                    "artifact": {
                        "kind": "b0",
                        "fingerprint": candidate_fingerprint,
                    },
                    "execution": {
                        "command": list(stability.ADAPTER_PROBE_COMMAND),
                        "exit_code": 0,
                        "process_audit": {
                            "runner": {"pid": 1001, "start_time_ticks": 101},
                            "servers": [
                                {
                                    "pid": 1001,
                                    "start_time_ticks": 101,
                                    "executable": {
                                        "size_bytes": executable.stat().st_size,
                                        "sha256": digest(executable.read_bytes()),
                                    },
                                }
                            ],
                            "helpers": [
                                {
                                    "pid": 1002,
                                    "start_time_ticks": 102,
                                    "executable": {
                                        "size_bytes": len(b"helper"),
                                        "sha256": digest(b"helper"),
                                    },
                                }
                            ],
                            "process_group_cleanup": True,
                            "session_cleanup": True,
                            "residue_count": 0,
                        },
                    },
                    "raw_abprobe": {
                        "path": raw_path.name,
                        "sha256": digest(raw_path.read_bytes()),
                    },
                    "stderr": {
                        "path": stderr_path.name,
                        "sha256": digest(stderr_path.read_bytes()),
                    },
                }
            ),
            encoding="utf-8",
        )
        return path

    def _protocol_stability_native(
        self,
        root: Path,
        source_ref: str,
        executable: Path,
        artifacts: list[dict[str, object]],
        baseline_fingerprint: str,
    ) -> Path:
        package_identity = self._ensure_swift_package_snapshot(root)
        raw_path = root / "protocol-steady-benchmark.json"
        cases = [
            {"id": case_id, "category": category, "candidates": [f"候補-{case_id}"]}
            for case_id, category in stability.PROTOCOL_CASES
        ]
        def latency(value: float) -> dict[str, object]:
            return {
                "mean": value,
                "median": value,
                "p95": value,
                "minimum": value,
                "maximum": value,
                "samples": [value] * 1500,
            }

        dictionary_path = str((root / "dictionary-fixture").resolve())
        payload = {
            "schema": stability.PROTOCOL_V2_BENCHMARK_SCHEMA,
            "source_ref": source_ref,
            "timing_boundary": "start_conversion_one_protocol_v2_round_trip",
            "policy": {
                "auto_conversion": False,
                "learning": False,
                "zenzai": False,
            },
            "execution": {
                "generated_at": "2026-07-15T00:00:00.000Z",
                "measurement_order": ["hazkey", "mozc"],
                "build_configuration": "formal-stability",
                "toolchain": "swift-test.sh",
                "operating_system": "test-linux",
                "kernel_release": "test-kernel",
                "cpu_model": "test-cpu",
                "processor_count": 2,
                "active_processor_count": 2,
                "physical_memory_bytes": 1024,
                "cpu_affinity_list": "0-1",
                "memory_sampling": (
                    "sequential_server_then_helper_after_warmup_and_after_measurement"
                ),
            },
            "server": {
                "path": str(executable),
                "size_bytes": executable.stat().st_size,
                "sha256": digest(executable.read_bytes()).removeprefix("sha256:"),
            },
            "corpus": {
                "path": stability.ADAPTER_CORPUS_PATH,
                "size_bytes": (
                    REPOSITORY_ROOT / stability.ADAPTER_CORPUS_PATH
                ).stat().st_size,
                "sha256": stability.ADAPTER_CORPUS_SHA256.removeprefix("sha256:"),
            },
            "dictionary": {
                "path": dictionary_path,
                "fingerprint": baseline_fingerprint,
            },
            "mozc_helper": {
                "path": artifacts[0]["path"],
                "size_bytes": artifacts[0]["size_bytes"],
                "sha256": str(artifacts[0]["sha256"]).removeprefix("sha256:"),
            },
            "mozc_data": {
                "path": artifacts[1]["path"],
                "size_bytes": artifacts[1]["size_bytes"],
                "sha256": str(artifacts[1]["sha256"]).removeprefix("sha256:"),
            },
            "backends": [
                {
                    "backend": "hazkey",
                    "protocol_version": 2,
                    "warmups_per_case": 5,
                    "iterations_per_case": 100,
                    "conversion_count": 1500,
                    "latency_ms": latency(2.0),
                    "memory": {
                        "server_before": {"rss_kib": 120, "pss_kib": 100},
                        "server_after": {"rss_kib": 120, "pss_kib": 100},
                        "max_observed_endpoint_total_pss_kib": 100,
                    },
                    "candidates": cases,
                    "process_stability": {
                        "server_pid": 1001,
                        "child_pids_before": [],
                        "child_pids_after": [],
                    },
                },
                {
                    "backend": "mozc",
                    "protocol_version": 2,
                    "warmups_per_case": 5,
                    "iterations_per_case": 100,
                    "conversion_count": 1500,
                    "latency_ms": latency(1.0),
                    "memory": {
                        "server_before": {"rss_kib": 100, "pss_kib": 80},
                        "server_after": {"rss_kib": 100, "pss_kib": 80},
                        "helper_before": {"rss_kib": 30, "pss_kib": 20},
                        "helper_after": {"rss_kib": 30, "pss_kib": 20},
                        "max_observed_endpoint_total_pss_kib": 100,
                    },
                    "candidates": cases,
                    "process_stability": {
                        "server_pid": 1002,
                        "child_pids_before": [1003],
                        "child_pids_after": [1003],
                        "helper_executable_path_before": str(artifacts[0]["path"]),
                        "helper_executable_path_after": str(artifacts[0]["path"]),
                        "helper_exited_after_server_stop": True,
                    },
                },
            ],
            "comparison": {
                "hazkey_over_mozc_mean_latency": 2.0,
                "hazkey_over_mozc_median_latency": 2.0,
                "hazkey_over_mozc_p95_latency": 2.0,
                "mozc_pss_delta_percent": 0.0,
            },
        }
        raw_path.write_text(json.dumps(payload), encoding="utf-8")
        stdout_path = root / "protocol-steady.stdout"
        stderr_path = root / "protocol-steady.stderr"
        stdout_path.write_text("testProtocolV2BackendComparisonKeepsLongLivedProcessesStable passed\n", encoding="utf-8")
        stderr_path.write_bytes(b"")
        source_path = REPOSITORY_ROOT / stability.PROTOCOL_BENCHMARK_SOURCE_PATH
        test_runner = REPOSITORY_ROOT / stability.SWIFT_TEST_RUNNER_PATH
        path = root / "protocol-steady-result.json"
        path.write_text(
            json.dumps(
                {
                    "schema": stability.PROTOCOL_STEADY_SCHEMA,
                    "producer": {
                        "path": stability.ORCHESTRATOR_PATH,
                        "sha256": digest(
                            (REPOSITORY_ROOT / stability.ORCHESTRATOR_PATH).read_bytes()
                        ),
                    },
                    "product_source_ref": source_ref,
                    "product_server": {
                        "size_bytes": executable.stat().st_size,
                        "sha256": digest(executable.read_bytes()),
                    },
                    "artifact": {
                        "kind": "b0",
                        "fingerprint": stability.B0_RESOURCE_FINGERPRINT,
                    },
                    "benchmark_source": {
                        "path": stability.PROTOCOL_BENCHMARK_SOURCE_PATH,
                        "snapshot_path": (
                            f"{stability.SWIFT_PACKAGE_SNAPSHOT_PATH}/"
                            f"{Path(stability.PROTOCOL_BENCHMARK_SOURCE_PATH).relative_to(stability.SWIFT_PACKAGE_ROOT).as_posix()}"
                        ),
                        "size_bytes": source_path.stat().st_size,
                        "sha256": digest(source_path.read_bytes()),
                    },
                    "test_runner": {
                        "path": stability.SWIFT_TEST_RUNNER_PATH,
                        "snapshot_path": (
                            f"{stability.SWIFT_PACKAGE_SNAPSHOT_PATH}/"
                            "scripts/swift-test.sh"
                        ),
                        "size_bytes": test_runner.stat().st_size,
                        "sha256": digest(test_runner.read_bytes()),
                    },
                    "swift_package": {
                        "path": stability.SWIFT_PACKAGE_SNAPSHOT_PATH,
                        "file_count": package_identity[0],
                        "size_bytes": package_identity[1],
                        "fingerprint": package_identity[2],
                        "post_run_verified": True,
                    },
                    "dictionary": {
                        "path": dictionary_path,
                        "fingerprint_before": baseline_fingerprint,
                        "fingerprint_after": baseline_fingerprint,
                    },
                    "execution": {
                        "command": list(stability.PROTOCOL_TEST_COMMAND),
                        "scratch_path": "swift-scratch",
                        "exit_code": 0,
                        "skipped": False,
                        "process_audit": {
                            "runner": {"pid": 999, "start_time_ticks": 99},
                            "servers": [
                                {
                                    "pid": 1001,
                                    "start_time_ticks": 101,
                                    "executable": {
                                        "size_bytes": executable.stat().st_size,
                                        "sha256": digest(executable.read_bytes()),
                                    },
                                },
                                {
                                    "pid": 1002,
                                    "start_time_ticks": 102,
                                    "executable": {
                                        "size_bytes": executable.stat().st_size,
                                        "sha256": digest(executable.read_bytes()),
                                    },
                                },
                            ],
                            "helpers": [
                                {
                                    "pid": 1003,
                                    "start_time_ticks": 103,
                                    "executable": {
                                        "size_bytes": artifacts[0]["size_bytes"],
                                        "sha256": artifacts[0]["sha256"],
                                    },
                                }
                            ],
                            "process_group_cleanup": True,
                            "session_cleanup": True,
                            "residue_count": 0,
                        },
                    },
                    "benchmark": {
                        "path": raw_path.name,
                        "sha256": digest(raw_path.read_bytes()),
                    },
                    "stdout": {
                        "path": stdout_path.name,
                        "sha256": digest(stdout_path.read_bytes()),
                    },
                    "stderr": {
                        "path": stderr_path.name,
                        "sha256": digest(stderr_path.read_bytes()),
                    },
                }
            ),
            encoding="utf-8",
        )
        return path

    def _recovery_stability_native(
        self,
        root: Path,
        source_ref: str,
        executable: Path,
        runtime_files: list[dict[str, object]],
    ) -> Path:
        package_identity = self._ensure_swift_package_snapshot(root)
        source_path = REPOSITORY_ROOT / stability.RECOVERY_SOURCE_PATH
        test_runner = REPOSITORY_ROOT / stability.SWIFT_TEST_RUNNER_PATH
        source_data = source_path.read_bytes()
        source_sha = digest(source_data)
        runtime_root = root / "runtime-lib"
        runtime_root.mkdir()
        for index, (item, name) in enumerate(
            zip(
                runtime_files,
                stability.run_mozc_b0_measurement.RUNTIME_DEPENDENCY_FILENAMES,
                strict=True,
            )
        ):
            data = f"fixture runtime dependency {index}: {name}\n".encode()
            self.assertEqual(item["path"], name)
            self.assertEqual(item["size_bytes"], len(data))
            self.assertEqual(item["sha256"], digest(data))
            (runtime_root / name).write_bytes(data)
        subchecks: list[dict[str, object]] = []
        for check_id, test_name, fixture_mode in stability.RECOVERY_SUBCHECKS:
            stdout = root / f"recovery-{check_id}.stdout"
            stderr = root / f"recovery-{check_id}.stderr"
            stdout.write_text(
                f"{test_name.rsplit('/', 1)[-1]} passed\n",
                encoding="utf-8",
            )
            stderr.write_bytes(b"")
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
                    "exit_code": 0,
                    "skipped": False,
                    "cleanup": {
                        "process_group": True,
                        "session": True,
                        "residue_count": 0,
                    },
                    "stdout": {"path": stdout.name, "sha256": digest(stdout.read_bytes())},
                    "stderr": {"path": stderr.name, "sha256": digest(stderr.read_bytes())},
                }
            )
        path = root / "protocol-recovery-native.json"
        path.write_text(
            json.dumps(
                {
                    "schema": stability.RECOVERY_SCHEMA,
                    "producer": {
                        "path": stability.ORCHESTRATOR_PATH,
                        "sha256": digest(
                            (REPOSITORY_ROOT / stability.ORCHESTRATOR_PATH).read_bytes()
                        ),
                    },
                    "product_source_ref": source_ref,
                    "product_server": {
                        "size_bytes": executable.stat().st_size,
                        "sha256": digest(executable.read_bytes()),
                    },
                    "artifact": {
                        "kind": "fault-fixture",
                        "fixture_identity": stability.recovery_fixture_identity(source_sha),
                    },
                    "fixture_source": {
                        "path": stability.RECOVERY_SOURCE_PATH,
                        "snapshot_path": (
                            f"{stability.SWIFT_PACKAGE_SNAPSHOT_PATH}/"
                            f"{Path(stability.RECOVERY_SOURCE_PATH).relative_to(stability.SWIFT_PACKAGE_ROOT).as_posix()}"
                        ),
                        "size_bytes": len(source_data),
                        "sha256": source_sha,
                    },
                    "test_runner": {
                        "path": stability.SWIFT_TEST_RUNNER_PATH,
                        "snapshot_path": (
                            f"{stability.SWIFT_PACKAGE_SNAPSHOT_PATH}/"
                            "scripts/swift-test.sh"
                        ),
                        "size_bytes": test_runner.stat().st_size,
                        "sha256": digest(test_runner.read_bytes()),
                    },
                    "swift_package": {
                        "path": stability.SWIFT_PACKAGE_SNAPSHOT_PATH,
                        "file_count": package_identity[0],
                        "size_bytes": package_identity[1],
                        "fingerprint": package_identity[2],
                        "post_run_verified": True,
                    },
                    "scratch_path": "swift-scratch",
                    "runtime_dependencies": {
                        "path": "runtime-lib",
                        "files": runtime_files,
                        "post_run_verified": True,
                    },
                    "subchecks": subchecks,
                    "residue_count": 0,
                }
            ),
            encoding="utf-8",
        )
        return path

    def _fcitx_stability_native(
        self,
        root: Path,
        suite_id: str,
        source_ref: str,
        candidate_fingerprint: str,
        executable: Path,
        artifacts: list[dict[str, object]],
        runtime_files: list[dict[str, object]],
    ) -> Path:
        iterations, cycles = (
            (150_000, 1)
            if suite_id == stability.FCITX_LONG_SOAK_ID
            else (100, 3)
        )
        producer_path = REPOSITORY_ROOT / stability.FCITX_PRODUCER_PATH
        path = root / f"{suite_id}-native.json"
        source_root = root / f"{suite_id}-sources"
        source_root.mkdir()

        def source_file(relative: str, data: bytes) -> Path:
            path = source_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            return path.resolve()

        harness_source = source_file("harness", b"harness\n")
        addon_source = source_file("addon.so", b"addon\n")
        addon_config_source = source_file("config/addon.conf", b"addon config\n")
        input_method_source = source_file(
            "config/input-method.conf", b"input method config\n"
        )
        verifier_source = source_file("mozc/verifier.py", b"verifier\n")
        for name in ("testfrontend.conf", "testim.conf", "testui.conf"):
            source_file(f"system-test-addon/{name}", name.encode())
        dictionary_source = source_file(
            "dictionary/fixture.dict", b"dictionary fixture\n"
        )
        llama_source = source_root / "llama-lib"
        llama_source.mkdir()
        for index, item in enumerate(runtime_files):
            (llama_source / str(item["path"])).write_bytes(
                f"fixture runtime dependency {index}: {item['path']}\n".encode()
            )
        content_address = "sha256-" + "d" * 64
        mozc_source = source_root / content_address
        mozc_source.mkdir()
        helper_source = mozc_source / "fcitx5-grimodex-mozc-helper"
        helper_source.write_bytes(b"helper")
        data_source = mozc_source / "mozc.data"
        data_source.write_bytes(b"data")
        manifest_data = b"synthetic Mozc manifest\n"
        manifest_source = mozc_source / "manifest.json"
        manifest_source.write_bytes(manifest_data)
        license_names = (
            "ABSEIL-LICENSE",
            "DICTIONARY-OSS-NOTICE.txt",
            "FCITX-MOZKEY-THIRD-PARTY-NOTICES.md",
            "JAPANESE-USAGE-DICTIONARY-LICENSE",
            "MOZC-LICENSE",
            "PROTOBUF-LICENSE",
            "UTF8-RANGE-LICENSE",
        )
        for name in license_names:
            source_file_path = mozc_source / "licenses" / name
            source_file_path.parent.mkdir(parents=True, exist_ok=True)
            source_file_path.write_bytes((name + "\n").encode())

        snapshot_root = root / f"{suite_id}-snapshot-placeholder"
        entries: list[dict[str, object]] = []

        def add_entry(
            input_id: str,
            relative: str,
            source: Path,
            *,
            data: bytes | None = None,
            size: int | None = None,
            sha256: str | None = None,
            mode: int = 0o444,
        ) -> None:
            if data is not None:
                size = len(data)
                sha256 = digest(data)
            assert size is not None and sha256 is not None
            entries.append(
                {
                    "input_id": input_id,
                    "source_path": str(source.resolve()),
                    "relative_path": relative,
                    "size": size,
                    "sha256": sha256.removeprefix("sha256:"),
                    "mode": mode,
                }
            )

        add_entry("harness", "harness", harness_source, data=b"harness\n", mode=0o555)
        add_entry("addon", "addon.so", addon_source, data=b"addon\n", mode=0o555)
        add_entry(
            "product_server",
            "server",
            executable,
            data=executable.read_bytes(),
            mode=0o555,
        )
        add_entry(
            "addon_config",
            "config/addon.conf",
            addon_config_source,
            data=b"addon config\n",
        )
        add_entry(
            "input_method_config",
            "config/input-method.conf",
            input_method_source,
            data=b"input method config\n",
        )
        add_entry(
            "dictionary",
            "dictionary/fixture.dict",
            dictionary_source,
            data=b"dictionary fixture\n",
        )
        for item in runtime_files:
            name = str(item["path"])
            add_entry(
                "llama_lib",
                f"llama-lib/{name}",
                llama_source / name,
                size=int(item["size_bytes"]),
                sha256=str(item["sha256"]),
                mode=0o555,
            )
        add_entry(
            "mozc_verifier",
            "mozc/verifier.py",
            verifier_source,
            data=b"verifier\n",
        )
        add_entry(
            "mozc_generation",
            "mozc/generation/fcitx5-grimodex-mozc-helper",
            helper_source,
            size=int(artifacts[0]["size_bytes"]),
            sha256=str(artifacts[0]["sha256"]),
            mode=0o555,
        )
        add_entry(
            "mozc_generation",
            "mozc/generation/manifest.json",
            manifest_source,
            data=manifest_data,
        )
        add_entry(
            "mozc_generation",
            "mozc/generation/mozc.data",
            data_source,
            size=int(artifacts[1]["size_bytes"]),
            sha256=str(artifacts[1]["sha256"]),
        )
        for name in license_names:
            license_data = (name + "\n").encode()
            add_entry(
                "mozc_generation",
                f"mozc/generation/licenses/{name}",
                mozc_source / "licenses" / name,
                data=license_data,
            )
        for name in ("testfrontend.conf", "testim.conf", "testui.conf"):
            data = name.encode()
            add_entry(
                f"system_test_addon:{name}",
                f"system-test-addon/{name}",
                source_root / "system-test-addon" / name,
                data=data,
            )
        entries.sort(key=lambda item: str(item["relative_path"]).encode())
        directories = {"."}
        for entry in entries:
            parent = Path(str(entry["relative_path"])).parent
            while parent.as_posix() != ".":
                directories.add(parent.as_posix())
                parent = parent.parent
        sorted_directories = sorted(directories, key=lambda item: item.encode())
        normalized_entries = [
            {
                **entry,
                "sha256": "sha256:" + str(entry["sha256"]),
            }
            for entry in entries
        ]
        snapshot_fingerprint = stability._fcitx_snapshot_fingerprint(
            sorted_directories, normalized_entries
        )

        prepared_content_address = "sha256-" + "e" * 64
        retained_root = root / stability._fcitx_retained_evidence_root_name(
            path.name, snapshot_fingerprint
        )
        snapshot_root = retained_root / "evidence-inputs"
        runtime_generation = (
            retained_root / "mozc-runtime" / prepared_content_address
        )
        for directory in sorted(
            (item for item in sorted_directories if item != "."),
            key=lambda value: (value.count("/"), value.encode("utf-8")),
        ):
            (snapshot_root / directory).mkdir(parents=True, exist_ok=True)
        snapshot_root.mkdir(parents=True, exist_ok=True)
        for entry in entries:
            destination = snapshot_root / str(entry["relative_path"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            source_data = Path(str(entry["source_path"])).read_bytes()
            self.assertEqual(len(source_data), entry["size"])
            self.assertEqual(
                digest(source_data).removeprefix("sha256:"), entry["sha256"]
            )
            destination.write_bytes(source_data)
            destination.chmod(int(entry["mode"]))
        for directory in sorted(
            sorted_directories,
            key=lambda value: (value.count("/"), value.encode("utf-8")),
            reverse=True,
        ):
            target = snapshot_root if directory == "." else snapshot_root / directory
            target.chmod(0o555)
        runtime_generation.mkdir(parents=True)
        (runtime_generation / "fcitx5-grimodex-mozc-helper").write_bytes(
            helper_source.read_bytes()
        )
        (runtime_generation / "fcitx5-grimodex-mozc-helper").chmod(0o555)
        (runtime_generation / "mozc.data").write_bytes(data_source.read_bytes())
        (runtime_generation / "mozc.data").chmod(0o444)
        runtime_generation.chmod(0o555)
        runtime_generation.parent.chmod(0o555)
        retained_root.chmod(0o500)
        server_runtime_path = snapshot_root / "server"
        helper_runtime_path = runtime_generation / "fcitx5-grimodex-mozc-helper"
        data_runtime_path = runtime_generation / "mozc.data"
        manifest_snapshot_path = snapshot_root / "mozc/generation/manifest.json"
        cycle_results = []
        for cycle in range(1, cycles + 1):
            session_id = 10_000 + cycle
            server_pid = 20_000 + (cycle * 2)
            helper_pid = server_pid + 1

            def process(pid: int, executable_path: Path) -> dict[str, object]:
                start_time = str(30_000 + pid)
                return {
                    "launch_count": 1,
                    "recovery_count": 0,
                    "launches": [{"pid": pid, "start_time": start_time}],
                    "observed_identities": [
                        {
                            "pid": pid,
                            "start_time": start_time,
                            "executable": str(executable_path),
                            "process_group": session_id,
                            "session_id": session_id,
                        }
                    ],
                    "cleanup_ok": True,
                }

            cycle_results.append(
                {
                    "cycle": cycle,
                    "conversions": iterations,
                    "lock_owner_observed": True,
                    "max_concurrent_helpers": 1,
                    "process_group_cleanup_ok": True,
                    "server": process(server_pid, server_runtime_path),
                    "helper": process(helper_pid, helper_runtime_path),
                }
            )
        command = [
            sys.executable,
            str(producer_path),
            "--harness",
            str(harness_source),
            "--addon",
            str(addon_source),
            "--server",
            str(executable.resolve()),
            "--dictionary",
            str((source_root / "dictionary").resolve()),
            "--addon-config",
            str(addon_config_source),
            "--input-method-config",
            str(input_method_source),
            "--system-test-addon-dir",
            str((source_root / "system-test-addon").resolve()),
            "--llama-lib-dir",
            str(llama_source.resolve()),
            "--converter-backend",
            "mozc",
            "--mozc-generation",
            str(mozc_source.resolve()),
            "--mozc-verifier",
            str(verifier_source),
            "--cycles",
            str(cycles),
            "--soak-iterations",
            str(iterations),
            "--timeout",
            "900",
            "--product-source-ref",
            source_ref,
            "--product-server-sha256",
            digest(executable.read_bytes()).removeprefix("sha256:"),
            "--product-server-size",
            str(executable.stat().st_size),
            "--result-output",
            str(path),
        ]
        path.write_text(
            json.dumps(
                {
                    "schema": stability.FCITX_SCHEMA,
                    "version": 1,
                    "exit_code": 0,
                    "producer": {
                        "path": str(producer_path),
                        "size": producer_path.stat().st_size,
                        "sha256": digest(producer_path.read_bytes()).removeprefix("sha256:"),
                    },
                    "source": {
                        "repository_root": str(REPOSITORY_ROOT),
                        "git_head": "1" * 40,
                        "worktree_clean": False,
                    },
                    "product_source_ref": source_ref,
                    "product_server": {
                        "sha256": digest(executable.read_bytes()).removeprefix("sha256:"),
                        "size": executable.stat().st_size,
                    },
                    "artifact_fingerprint": candidate_fingerprint,
                    "command": command,
                    "artifacts": {
                        "harness": {
                            "path": str(snapshot_root / "harness"),
                            "size": len(b"harness\n"),
                            "sha256": digest(b"harness\n").removeprefix("sha256:"),
                        },
                        "addon": {
                            "path": str(snapshot_root / "addon.so"),
                            "size": len(b"addon\n"),
                            "sha256": digest(b"addon\n").removeprefix("sha256:"),
                        },
                        "server": {
                            "path": str(server_runtime_path),
                            "size": executable.stat().st_size,
                            "sha256": digest(executable.read_bytes()).removeprefix("sha256:"),
                        },
                        "dictionary": {"path": str(snapshot_root / "dictionary")},
                        "llama_library_directory": {"path": str(snapshot_root / "llama-lib")},
                        "mozc_verifier": {
                            "path": str(snapshot_root / "mozc/verifier.py"),
                            "size": len(b"verifier\n"),
                            "sha256": digest(b"verifier\n").removeprefix("sha256:"),
                        },
                        "mozc_helper": {
                            "path": str(helper_runtime_path),
                            "size": artifacts[0]["size_bytes"],
                            "sha256": str(artifacts[0]["sha256"]).removeprefix("sha256:"),
                            "mode": 0o555,
                        },
                        "mozc_data": {
                            "path": str(data_runtime_path),
                            "size": artifacts[1]["size_bytes"],
                            "sha256": str(artifacts[1]["sha256"]).removeprefix("sha256:"),
                            "mode": 0o444,
                        },
                        "mozc_generation": {
                            "path": str(runtime_generation),
                            "source_path": str(snapshot_root / "mozc/generation"),
                            "content_address": content_address,
                            "prepared_content_address": prepared_content_address,
                            "artifact_fingerprint": candidate_fingerprint,
                        },
                        "mozc_manifest": {
                            "path": str(manifest_snapshot_path),
                            "size": len(manifest_data),
                            "sha256": self.synthetic_manifest_sha.removeprefix("sha256:"),
                        },
                    },
                    "input_snapshot": {
                        "schema": stability.FCITX_SNAPSHOT_SCHEMA,
                        "root": str(snapshot_root),
                        "fingerprint": snapshot_fingerprint,
                        "directories": sorted_directories,
                        "entries": entries,
                        "integrity": {
                            "post_run_verified": True,
                            "entry_count": len(entries),
                        },
                    },
                    "runtime_integrity": {
                        "post_run_verified": True,
                        "verified_artifacts": ["mozc_helper", "mozc_data"],
                    },
                    "configuration": {
                        "converter_backend": "mozc",
                        "iterations": iterations,
                        "cycles": cycles,
                        "timeout_seconds": 900,
                    },
                    "conversions": iterations * cycles,
                    "cycles": cycles,
                    "helper_launches": cycles,
                    "server_launches": cycles,
                    "helper_recoveries": 0,
                    "server_recoveries": 0,
                    "residue_count": 0,
                    "cycle_results": cycle_results,
                }
            ),
            encoding="utf-8",
        )
        return path

    def _build(self, root: Path) -> tuple[Path, Path]:
        source_ref = stability.PRODUCT_SOURCE_REF
        baseline_fingerprint = stability._dictionary_fingerprint_from_snapshot(
            {
                "dictionary/fixture.dict": {
                    "input_id": "dictionary",
                    "sha256": digest(b"dictionary fixture\n"),
                }
            }
        )
        candidate_fingerprint = stability.B0_RESOURCE_FINGERPRINT
        rows_by_component: list[list[dict[str, str]]] = []
        for name, count, prefix in gate.EXPECTED_COMPONENTS:
            rows: list[dict[str, str]] = []
            if name == "ajimee-unconditional":
                allocation = [("ajimee-unconditional", 100)]
            elif name == "protected":
                allocation = [("protected", 16)]
            else:
                allocation = list(gate.EXPECTED_CURATED_CATEGORIES.items())
            index = 0
            for category, category_count in allocation:
                for _ in range(category_count):
                    rows.append(
                        {
                            "id": f"{prefix}{index:03d}",
                            "reading": f"よみ{prefix}{index}",
                            "expected": f"期待{prefix}{index}",
                            "category": category,
                        }
                    )
                    index += 1
            self.assertEqual(len(rows), count)
            rows_by_component.append(rows)

        component_names = [
            "external-ajimee-unconditional.tsv",
            "product-curated.tsv",
            "protected.tsv",
        ]
        components: list[dict[str, object]] = []
        for (name, count, prefix), filename, rows in zip(
            gate.EXPECTED_COMPONENTS, component_names, rows_by_component, strict=True
        ):
            path = root / filename
            self._write_tsv(path, rows)
            counts: dict[str, int] = {}
            for row in rows:
                counts[row["category"]] = counts.get(row["category"], 0) + 1
            components.append(
                {
                    "id": name,
                    "path": filename,
                    "sha256": digest(path.read_bytes()),
                    "cases": count,
                    "id_prefix": prefix,
                    "categories": counts,
                    "provenance": (
                        gate.AJIMEE_PROVENANCE
                        if name == "ajimee-unconditional"
                        else {
                            "kind": "project",
                            "license": "MIT",
                            "source": (
                                "grimodex-curated-v1"
                                if name == "product-curated"
                                else "grimodex-protected-v1"
                            ),
                        }
                    ),
                }
            )
        aggregate = root / "formal-256.tsv"
        all_rows = [row for rows in rows_by_component for row in rows]
        self._write_tsv(aggregate, all_rows)
        aggregate_sha = digest(aggregate.read_bytes())
        corpus_manifest = root / "corpus-manifest.json"
        corpus_manifest.write_text(
            json.dumps(
                {
                    "schema": gate.CORPUS_MANIFEST_SCHEMA,
                    "normalization": {
                        "unicode": "NFC",
                        "line_endings": "LF",
                        "reading_transform": "katakana-to-hiragana.v1",
                    },
                    "components": components,
                    "aggregate": {
                        "sha256": aggregate_sha,
                        "cases": 256,
                        "categories": gate.EXPECTED_CATEGORIES,
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        manifest_sha = digest(corpus_manifest.read_bytes())

        artifacts: list[dict[str, object]] = []
        for artifact_id, data in (("fcitx5-grimodex-mozc-helper", b"helper"), ("mozc.data", b"data")):
            path = root / artifact_id
            path.write_bytes(data)
            artifacts.append(
                {
                    "id": artifact_id,
                    "path": path.name,
                    "sha256": digest(data),
                    "size_bytes": len(data),
                }
            )

        executable = root / "hazkey-server"
        executable.write_bytes(b"synthetic ABProbe executable\n")
        executable.chmod(0o700)
        runtime_source = root / "runtime-library-source"
        runtime_source.mkdir()
        runtime_snapshot_root = root / acquisition.SNAPSHOT_ROOT_NAME
        runtime_snapshot_root.mkdir()
        executable_snapshot = (
            runtime_snapshot_root / acquisition.SNAPSHOT_EXECUTABLE_NAME
        )
        executable_snapshot.write_bytes(executable.read_bytes())
        executable_snapshot.chmod(0o555)
        runtime_snapshot = (
            runtime_snapshot_root / acquisition.SNAPSHOT_LIBRARY_DIRECTORY_NAME
        )
        runtime_snapshot.mkdir()
        runtime_files: list[dict[str, object]] = []
        for index, name in enumerate(acquisition.RUNTIME_DEPENDENCY_FILENAMES):
            data = f"fixture runtime dependency {index}: {name}\n".encode()
            source_path = runtime_source / name
            source_path.write_bytes(data)
            source_path.chmod(0o755)
            snapshot_path = runtime_snapshot / name
            snapshot_path.write_bytes(data)
            snapshot_path.chmod(0o555)
            runtime_files.append(
                {"path": name, "size_bytes": len(data), "sha256": digest(data)}
            )
        runtime_snapshot.chmod(0o555)
        runtime_snapshot_root.chmod(0o555)
        runtime_base = {
            "schema": acquisition.RUNTIME_DEPENDENCY_SCHEMA,
            "files": runtime_files,
        }
        runtime_contract = runtime_base | {
            "integrity": digest(acquisition.canonical_json(runtime_base))
        }
        hazkey_dictionary = root / "hazkey-dictionary"
        mozc_bundle = root / "mozc-bundle"
        hazkey_dictionary.mkdir()
        mozc_bundle.mkdir()

        policy = json.loads(POLICY_FIXTURE.read_text(encoding="utf-8"))
        policy["formal_suite"]["components"]["ajimee_unconditional"]["sha256"] = components[0]["sha256"]
        policy["external_sources"]["ajimee_bench"]["derived_sha256"] = components[0]["sha256"]
        policy["candidate"]["product_source_revision"] = source_ref
        policy["candidate"]["resource_fingerprint"] = candidate_fingerprint
        policy["candidate"]["product_executable"] = {
            "size_bytes": executable.stat().st_size,
            "sha256": digest(executable.read_bytes()),
        }
        policy["candidate"]["runtime_dependencies"] = runtime_contract
        for item, fixture in zip(policy["candidate"]["artifacts"], artifacts, strict=True):
            item["size_bytes"] = fixture["size_bytes"]
            item["sha256"] = str(fixture["sha256"]).removeprefix("sha256:")
        policy["baseline"]["resource_fingerprint"] = baseline_fingerprint
        stability_contracts = policy["gates"]["long_running_stability"]["checks"]
        fcitx_producer_sha = digest(
            (REPOSITORY_ROOT / stability.FCITX_PRODUCER_PATH).read_bytes()
        )
        for contract in stability_contracts:
            if contract["id"] == stability.ADAPTER_SOAK_ID:
                contract["native_producer"]["sha256"] = digest(
                    executable.read_bytes()
                )
            if contract["id"] in {
                stability.FCITX_LONG_SOAK_ID,
                stability.FCITX_LIFECYCLE_ID,
            }:
                contract["native_producer"] = {
                    "path": stability.FCITX_PRODUCER_PATH,
                    "status": "ready",
                    "sha256": fcitx_producer_sha,
                }
        policy["measurement_contracts"]["long_running_stability"] = {
            "status": "ready",
            "orchestrator": {
                "schema": stability.RECORD_SCHEMA,
                "path": stability.ORCHESTRATOR_PATH,
                "sha256": digest(
                    (REPOSITORY_ROOT / stability.ORCHESTRATOR_PATH).read_bytes()
                ),
            },
        }
        policy["manifest_binding"] = {
            "required_for_formal_decision": True,
            "expected_schema": gate.CORPUS_MANIFEST_SCHEMA,
            "status": "ready",
            "path": corpus_manifest.name,
            "sha256": manifest_sha,
        }
        policy["readiness"] = {"formal_decision_enabled": True, "blocking_items": []}
        policy_path = root / "policy.json"
        policy_path.write_text(json.dumps(policy, ensure_ascii=False), encoding="utf-8")

        run_paths: dict[str, dict[str, Path]] = {"hazkey": {}, "mozc": {}}
        run_hashes: dict[str, dict[str, str]] = {"hazkey": {}, "mozc": {}}
        for backend, fingerprint in (
            ("hazkey", baseline_fingerprint),
            ("mozc", candidate_fingerprint),
        ):
            for run_index, run_id in enumerate(gate.EXPECTED_RUN_IDS[backend]):
                path = root / f"{run_id}.jsonl"
                sample_value = (
                    2.0 + run_index * 0.1
                    if backend == "hazkey"
                    else 1.0 + run_index * 0.01
                )
                payloads = [
                    self._result(
                        row,
                        backend=backend,
                        corpus_sha=aggregate_sha,
                        resource_fingerprint=fingerprint,
                        source_ref=source_ref,
                        sample_value=sample_value,
                    )
                    for row in all_rows
                ]
                path.write_text(
                    "".join(
                        json.dumps(value, ensure_ascii=False) + "\n"
                        for value in payloads
                    ),
                    encoding="utf-8",
                )
                run_paths[backend][run_id] = path
                run_hashes[backend][run_id] = digest(path.read_bytes())

        host_fingerprint = "sha256:" + "1" * 64
        acquisition_entries: list[dict[str, object]] = []
        for sequence, (run_id, backend) in enumerate(acquisition.SEQUENCE, 1):
            stderr_path = root / f"{run_id}.stderr"
            stderr_path.write_bytes(b"")
            acquisition_entries.append(
                {
                    "sequence": sequence,
                    "id": run_id,
                    "backend": backend,
                    "argv": acquisition._command(
                        acquisition.SNAPSHOT_EXECUTABLE_ARG,
                        aggregate.resolve(),
                        source_ref,
                        backend,
                        hazkey_dictionary.resolve(),
                        mozc_bundle.resolve(),
                    ),
                    "raw": {
                        "path": run_paths[backend][run_id].name,
                        "sha256": run_hashes[backend][run_id],
                    },
                    "stderr": {
                        "path": stderr_path.name,
                        "sha256": digest(stderr_path.read_bytes()),
                    },
                    "exit_code": 0,
                    "started_monotonic_ns": sequence * 2 - 1,
                    "ended_monotonic_ns": sequence * 2,
                    "host_fingerprint": host_fingerprint,
                    "effective_cpu_affinity": [0],
                }
            )
        acquisition_base = {
            "schema": acquisition.SCHEMA,
            "producer": {
                "path": "tools/dictionary/run_mozc_b0_measurement.py",
                "sha256": policy["measurement_contracts"]["formal_abprobe_v3"][
                    "producer_sha256"
                ],
            },
            "executable": {
                "source_path": str(executable.resolve()),
                "snapshot_path": "runtime/hazkey-server",
                "size_bytes": executable.stat().st_size,
                "sha256": digest(executable.read_bytes()),
            },
            "runtime_dependencies": {
                "schema": acquisition.RUNTIME_DEPENDENCY_SCHEMA,
                "source_path": str(runtime_source.resolve()),
                "snapshot_path": "runtime/lib",
                "files": runtime_files,
                "integrity": runtime_contract["integrity"],
            },
            "environment": acquisition._child_environment()[1],
            "product_source_ref": source_ref,
            "corpus": {
                "path": str(aggregate.resolve()),
                "sha256": aggregate_sha,
                "cases": 256,
            },
            "host": {
                "fingerprint": host_fingerprint,
                "effective_cpu_affinity": [0],
            },
            "measurement": {
                "runs_per_backend": 4,
                "execution_order": list(gate.EXPECTED_RUN_SEQUENCE),
                "warmups_per_case": 5,
                "iterations_per_case": 20,
                "top_k": 10,
                "latency_statistic": "nearest-rank-p95-across-all-samples",
                "pss_statistic": "max-parent-plus-backend-before-after",
                "cpu_policy": "unrestricted-same-host",
                "per_run_timeout_seconds": acquisition.PER_RUN_TIMEOUT_SECONDS,
            },
            "entries": acquisition_entries,
        }
        acquisition_payload = acquisition_base | {
            "integrity": digest(acquisition.canonical_json(acquisition_base))
        }
        acquisition_manifest = root / acquisition.MANIFEST_NAME
        acquisition_manifest.write_text(
            json.dumps(acquisition_payload, ensure_ascii=False), encoding="utf-8"
        )

        packet = root / "packet"
        blind_conversion_ab.prepare(
            aggregate,
            run_paths["hazkey"]["H1"],
            run_paths["mozc"]["M1"],
            "11" * 32,
            packet,
        )
        review_records = [
            json.loads(line)
            for line in (packet / blind_conversion_ab.REVIEW_NAME).read_text(encoding="utf-8").splitlines()
        ]
        judgments = root / "judgments.jsonl"
        judgments.write_text(
            "".join(
                json.dumps(
                    {
                        "schema": blind_conversion_ab.JUDGMENT_SCHEMA,
                        "case": record["case"],
                        "judgment": "tie",
                    }
                )
                + "\n"
                for record in review_records
            ),
            encoding="utf-8",
        )

        stability_entries: list[dict[str, str]] = []
        native_paths = {
            stability.ADAPTER_SOAK_ID: self._adapter_stability_native(
                root, source_ref, candidate_fingerprint, executable
            ),
            stability.PROTOCOL_STEADY_ID: self._protocol_stability_native(
                root,
                source_ref,
                executable,
                artifacts,
                baseline_fingerprint,
            ),
            stability.PROTOCOL_RECOVERY_ID: self._recovery_stability_native(
                root, source_ref, executable, runtime_files
            ),
            stability.FCITX_LONG_SOAK_ID: self._fcitx_stability_native(
                root,
                stability.FCITX_LONG_SOAK_ID,
                source_ref,
                candidate_fingerprint,
                executable,
                artifacts,
                runtime_files,
            ),
            stability.FCITX_LIFECYCLE_ID: self._fcitx_stability_native(
                root,
                stability.FCITX_LIFECYCLE_ID,
                source_ref,
                candidate_fingerprint,
                executable,
                artifacts,
                runtime_files,
            ),
        }
        synthetic_fcitx_fingerprints = {
            json.loads(native_paths[check_id].read_text(encoding="utf-8"))[
                "input_snapshot"
            ]["fingerprint"]
            for check_id in (
                stability.FCITX_LONG_SOAK_ID,
                stability.FCITX_LIFECYCLE_ID,
            )
        }
        self.assertEqual(len(synthetic_fcitx_fingerprints), 1)
        synthetic_fcitx_fingerprint = synthetic_fcitx_fingerprints.pop()
        for contract in stability_contracts:
            if contract["id"] in {
                stability.FCITX_LONG_SOAK_ID,
                stability.FCITX_LIFECYCLE_ID,
            }:
                contract["input_snapshot_fingerprint"] = (
                    synthetic_fcitx_fingerprint
                )
        policy_path.write_text(
            json.dumps(policy, ensure_ascii=False),
            encoding="utf-8",
        )
        for contract in stability_contracts:
            check_id = contract["id"]
            native_path = native_paths[check_id]
            path = root / f"stability-{check_id}.json"
            path.write_text(
                json.dumps(
                    stability.build_record(
                        check_id,
                        native_path,
                        native_path.read_bytes(),
                        artifact_fingerprint=candidate_fingerprint,
                        recovery_fixture_identity_value=(
                            contract["recovery_fixture_identity"]
                            if check_id == stability.PROTOCOL_RECOVERY_ID
                            else None
                        ),
                    )
                ),
                encoding="utf-8",
            )
            stability_entries.append({"id": check_id, "path": path.name, "sha256": digest(path.read_bytes())})

        evidence = {
            "schema": gate.EVIDENCE_SCHEMA,
            "policy": {"sha256": digest(policy_path.read_bytes())},
            "product_source_ref": source_ref,
            "corpus_manifest": {"path": corpus_manifest.name, "sha256": manifest_sha},
            "corpus": {"path": aggregate.name, "sha256": aggregate_sha},
            "packet": {
                "path": packet.name,
                "manifest_sha256": digest((packet / blind_conversion_ab.MANIFEST_NAME).read_bytes()),
                "key_sha256": digest((packet / blind_conversion_ab.KEY_NAME).read_bytes()),
                "review_sha256": digest((packet / blind_conversion_ab.REVIEW_NAME).read_bytes()),
            },
            "judgments": {"path": judgments.name, "sha256": digest(judgments.read_bytes())},
            "artifacts": [
                {key: value for key, value in item.items() if key != "size_bytes"}
                for item in artifacts
            ],
            "raw_runs": {
                backend: [
                    {
                        "id": run_id,
                        "path": run_paths[backend][run_id].name,
                        "sha256": run_hashes[backend][run_id],
                    }
                    for run_id in gate.EXPECTED_RUN_IDS[backend]
                ]
                for backend in ("hazkey", "mozc")
            },
            "acquisition_manifest": {
                "path": acquisition_manifest.name,
                "sha256": digest(acquisition_manifest.read_bytes()),
            },
            "stability": stability_entries,
        }
        evidence_path = root / "evidence.json"
        evidence_path.write_text(json.dumps(evidence, ensure_ascii=False), encoding="utf-8")
        return policy_path, evidence_path

    def _rewrite_stability_raw(
        self,
        root: Path,
        evidence_path: Path,
        check_id: str,
        mutate: Callable[[dict[str, object]], None],
    ) -> None:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        entry = next(item for item in evidence["stability"] if item["id"] == check_id)
        record_path = root / entry["path"]
        record = json.loads(record_path.read_text(encoding="utf-8"))
        raw_path = record_path.parent / record["native_result"]["path"]
        raw_result = json.loads(raw_path.read_text(encoding="utf-8"))
        mutate(raw_result)
        raw_path.write_text(json.dumps(raw_result), encoding="utf-8")
        record["native_result"]["sha256"] = digest(raw_path.read_bytes())
        record_path.write_text(json.dumps(record), encoding="utf-8")
        entry["sha256"] = digest(record_path.read_bytes())
        evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

    def _rewrite_protocol_benchmark(
        self,
        root: Path,
        evidence_path: Path,
        mutate: Callable[[dict[str, object]], None],
    ) -> None:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        entry = next(
            item
            for item in evidence["stability"]
            if item["id"] == stability.PROTOCOL_STEADY_ID
        )
        record_path = root / entry["path"]
        record = json.loads(record_path.read_text(encoding="utf-8"))
        native_path = record_path.parent / record["native_result"]["path"]
        native = json.loads(native_path.read_text(encoding="utf-8"))
        benchmark_path = native_path.parent / native["benchmark"]["path"]
        benchmark = json.loads(benchmark_path.read_text(encoding="utf-8"))
        mutate(benchmark)
        benchmark_path.write_text(json.dumps(benchmark), encoding="utf-8")
        native["benchmark"]["sha256"] = digest(benchmark_path.read_bytes())
        native_path.write_text(json.dumps(native), encoding="utf-8")
        record["native_result"]["sha256"] = digest(native_path.read_bytes())
        record_path.write_text(json.dumps(record), encoding="utf-8")
        entry["sha256"] = digest(record_path.read_bytes())
        evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

    def _rewrite_stability_record(
        self,
        root: Path,
        evidence_path: Path,
        check_id: str,
        mutate: Callable[[dict[str, object]], None],
    ) -> None:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        entry = next(item for item in evidence["stability"] if item["id"] == check_id)
        record_path = root / entry["path"]
        record = json.loads(record_path.read_text(encoding="utf-8"))
        mutate(record)
        record_path.write_text(json.dumps(record), encoding="utf-8")
        entry["sha256"] = digest(record_path.read_bytes())
        evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

    def _rewrite_acquisition_manifest(
        self,
        root: Path,
        evidence_path: Path,
        mutate: Callable[[dict[str, object]], None],
    ) -> None:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        manifest_path = root / evidence["acquisition_manifest"]["path"]
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        mutate(manifest)
        manifest_base = {
            key: value for key, value in manifest.items() if key != "integrity"
        }
        manifest["integrity"] = digest(acquisition.canonical_json(manifest_base))
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        evidence["acquisition_manifest"]["sha256"] = digest(
            manifest_path.read_bytes()
        )
        evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

    def _sync_run_hash(
        self, root: Path, evidence_path: Path, backend: str, run_id: str
    ) -> None:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        run_path = root / f"{run_id}.jsonl"
        changed_sha = digest(run_path.read_bytes())
        entry = next(
            item for item in evidence["raw_runs"][backend] if item["id"] == run_id
        )
        entry["sha256"] = changed_sha
        manifest_path = root / evidence["acquisition_manifest"]["path"]
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_entry = next(
            item for item in manifest["entries"] if item["id"] == run_id
        )
        manifest_entry["raw"]["sha256"] = changed_sha
        manifest_base = {
            key: value for key, value in manifest.items() if key != "integrity"
        }
        manifest["integrity"] = digest(acquisition.canonical_json(manifest_base))
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        evidence["acquisition_manifest"]["sha256"] = digest(
            manifest_path.read_bytes()
        )
        evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

    def test_rehashes_raw_inputs_recomputes_reports_and_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            policy, evidence = self._build(Path(temporary))
            result = gate.evaluate(policy, evidence)
        self.assertTrue(result["passed"])
        self.assertEqual(result["metrics"]["cases"], 256)
        self.assertEqual(
            result["measurement_contract"]["execution_order"],
            list(gate.EXPECTED_RUN_SEQUENCE),
        )
        self.assertEqual(
            result["measurement_contract"]["cpu_policy"],
            "unrestricted-same-host",
        )
        self.assertRegex(
            result["bindings"]["raw_runs"]["mozc"]["M1"],
            r"^sha256:[0-9a-f]{64}$",
        )
        self.assertRegex(result["integrity"], r"^sha256:[0-9a-f]{64}$")

    def test_actual_raw_byte_tamper_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy, evidence = self._build(root)
            with (root / "M1.jsonl").open("ab") as handle:
                handle.write(b"\n")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                gate.evaluate(policy, evidence)

    def test_policy_requires_the_exact_formal_measurement_contract(self) -> None:
        mutations = {
            "runs": ("runs_per_backend", 3),
            "order": (
                "execution_order",
                ["M1", "H1", "M2", "H2", "H3", "M3", "M4", "H4"],
            ),
            "warmups": ("warmups_per_case", 4),
            "iterations": ("iterations_per_case", 19),
            "top-k": ("top_k", 9),
            "cases": ("cases", 255),
            "latency": ("latency_statistic", "opaque-p95-v1"),
            "pss": ("pss_statistic", "opaque-pss-v1"),
            "cpu": ("cpu_policy", "pinned-affinity"),
            "producer": ("producer_sha256", "sha256:" + "0" * 64),
            "timeout": ("per_run_timeout_seconds", 899),
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy_path, _ = self._build(root)
            base = json.loads(policy_path.read_text(encoding="utf-8"))
            for name, (field, value) in mutations.items():
                policy = copy.deepcopy(base)
                policy["measurement_contracts"]["formal_abprobe_v3"][field] = value
                with self.subTest(name=name), self.assertRaisesRegex(
                    ValueError, "must be"
                ):
                    gate.parse_policy(json.dumps(policy).encode())

            opaque = copy.deepcopy(base)
            opaque["measurement_contracts"] = {
                "warm_latency_p95": {"status": "ready", "protocol_id": "opaque"},
                "pss": {"status": "ready", "protocol_id": "opaque"},
                "long_running_stability": {"status": "ready"},
            }
            with self.assertRaisesRegex(ValueError, "fields do not match schema"):
                gate.parse_policy(json.dumps(opaque).encode())

    def test_acquisition_manifest_run_sequence_is_exact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy, evidence = self._build(root)
            self._rewrite_acquisition_manifest(
                root,
                evidence,
                lambda payload: payload["entries"].__setitem__(
                    0, copy.deepcopy(payload["entries"][1])
                ),
            )
            with self.assertRaisesRegex(ValueError, r"entries\[0\].*(sequence|id)"):
                gate.evaluate(policy, evidence)

    def test_acquisition_environment_is_exact_and_ambient_free(self) -> None:
        mutations = {
            "relative-library-path": lambda payload: payload["environment"][
                "values"
            ].__setitem__("LD_LIBRARY_PATH", "relative/unpinned"),
            "ambient-home": lambda payload: payload["environment"]["values"].__setitem__(
                "HOME", "/tmp/ambient"
            ),
            "ambient-inheritance": lambda payload: payload["environment"].__setitem__(
                "ambient_inheritance", True
            ),
            "cwd": lambda payload: payload["environment"].__setitem__(
                "cwd", "/caller-selected"
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                policy, evidence = self._build(root)
                self._rewrite_acquisition_manifest(root, evidence, mutate)
                with self.assertRaisesRegex(
                    ValueError, "LD_LIBRARY_PATH|unknown=.*HOME|ambient_inheritance|cwd"
                ):
                    gate.evaluate(policy, evidence)

    def test_private_executable_and_runtime_dependency_snapshots_are_rehashed(self) -> None:
        mutations = {
            "executable": lambda root: (
                (root / "runtime/hazkey-server").chmod(0o700),
                (root / "runtime/hazkey-server").write_bytes(b"changed executable"),
            ),
            "dependency": lambda root: (
                (root / "runtime/lib/libggml-base.so").chmod(0o700),
                (root / "runtime/lib/libggml-base.so").write_bytes(
                    b"changed dependency"
                ),
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                policy, evidence = self._build(root)
                mutate(root)
                with self.assertRaisesRegex(ValueError, "hash mismatch"):
                    gate.evaluate(policy, evidence)

    def test_source_executable_is_audit_only_after_snapshot_acquisition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy, evidence = self._build(root)
            (root / "hazkey-server").write_bytes(b"replaced after acquisition")
            result = gate.evaluate(policy, evidence)
            self.assertTrue(result["passed"])
            self.assertEqual(
                result["bindings"]["acquisition"]["executable"]["snapshot_path"],
                "runtime/hazkey-server",
            )

    def test_fcitx_collect_accepts_audit_head_and_dirty_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy, evidence = self._build(root)
            payload = json.loads(evidence.read_text(encoding="utf-8"))
            entry = next(
                item
                for item in payload["stability"]
                if item["id"] == stability.FCITX_LIFECYCLE_ID
            )
            existing_record_path = root / entry["path"]
            existing_record = json.loads(
                existing_record_path.read_text(encoding="utf-8")
            )
            native_path = (
                existing_record_path.parent
                / existing_record["native_result"]["path"]
            )
            native = json.loads(native_path.read_text(encoding="utf-8"))
            self.assertEqual(native["source"]["git_head"], "1" * 40)
            self.assertFalse(native["source"]["worktree_clean"])
            collected = native_path.parent / "collected-fcitx-lifecycle.json"
            with mock.patch.object(
                sys,
                "argv",
                [
                    stability.ORCHESTRATOR_PATH,
                    "collect",
                    "--suite-id",
                    stability.FCITX_LIFECYCLE_ID,
                    "--native-result",
                    str(native_path),
                    "--output",
                    str(collected),
                    "--policy",
                    str(policy),
                ],
            ):
                self.assertEqual(stability.main(), 0)
            collected_record = json.loads(collected.read_text(encoding="utf-8"))
            self.assertEqual(collected_record["id"], stability.FCITX_LIFECYCLE_ID)

            native["producer"]["path"] = str(root / "forged-producer.py")
            native_path.write_text(json.dumps(native), encoding="utf-8")
            rejected = native_path.parent / "rejected-fcitx-lifecycle.json"
            with mock.patch.object(
                sys,
                "argv",
                [
                    stability.ORCHESTRATOR_PATH,
                    "collect",
                    "--suite-id",
                    stability.FCITX_LIFECYCLE_ID,
                    "--native-result",
                    str(native_path),
                    "--output",
                    str(rejected),
                    "--policy",
                    str(policy),
                ],
            ):
                self.assertEqual(stability.main(), 2)
            self.assertFalse(rejected.exists())

    def test_acquisition_raw_files_must_be_self_contained_exact_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy, evidence = self._build(root)
            self._rewrite_acquisition_manifest(
                root,
                evidence,
                lambda payload: payload["entries"][0]["stderr"].__setitem__(
                    "path", str((root / "H1.stderr").resolve())
                ),
            )
            with self.assertRaisesRegex(ValueError, r"entries\[0\]\.stderr\.path"):
                gate.evaluate(policy, evidence)

    def test_evidence_product_source_must_match_frozen_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy, evidence = self._build(root)
            payload = json.loads(evidence.read_text(encoding="utf-8"))
            payload["product_source_ref"] = "e" * 40
            evidence.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "product_source_ref"):
                gate.evaluate(policy, evidence)

    def test_every_raw_run_must_match_warmup_iteration_and_pss_contract(self) -> None:
        mutations = {
            "warmups": lambda result: result["measurement"].__setitem__(
                "warmups", 4
            ),
            "iterations": lambda result: (
                result["measurement"].__setitem__("iterations", 19),
                result["measurement"]["latency_ms"].__setitem__(
                    "samples", result["measurement"]["latency_ms"]["samples"][:19]
                ),
            ),
            "helper-pss": lambda result: result["measurement"]["rss"].pop(
                "backend_after_pss_kib"
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                policy, evidence = self._build(root)
                run_path = root / "M4.jsonl"
                results = [
                    json.loads(line)
                    for line in run_path.read_text(encoding="utf-8").splitlines()
                ]
                for result in results:
                    mutate(result)
                run_path.write_text(
                    "".join(json.dumps(result) + "\n" for result in results),
                    encoding="utf-8",
                )
                self._sync_run_hash(root, evidence, "mozc", "M4")
                with self.assertRaisesRegex(
                    ValueError, "warmups|iterations|helper PSS"
                ):
                    gate.evaluate(policy, evidence)

    def test_hazkey_raw_runs_must_not_claim_a_separate_backend_pss(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy, evidence = self._build(root)
            run_path = root / "H4.jsonl"
            results = [
                json.loads(line)
                for line in run_path.read_text(encoding="utf-8").splitlines()
            ]
            for result in results:
                result["measurement"]["rss"].update(
                    {
                        "backend_before_pss_kib": 1,
                        "backend_after_pss_kib": 1,
                    }
                )
            run_path.write_text(
                "".join(json.dumps(result) + "\n" for result in results),
                encoding="utf-8",
            )
            self._sync_run_hash(root, evidence, "hazkey", "H4")
            with self.assertRaisesRegex(ValueError, "unexpected backend PSS"):
                gate.evaluate(policy, evidence)

    def test_policy_corpus_artifact_and_review_hashes_are_enforced(self) -> None:
        mutations = {
            "policy": lambda root, payload: payload["policy"].__setitem__(
                "sha256", "sha256:" + "0" * 64
            ),
            "corpus": lambda root, payload: (root / "formal-256.tsv").write_bytes(
                (root / "formal-256.tsv").read_bytes() + b"\n"
            ),
            "artifact": lambda root, payload: (root / "mozc.data").write_bytes(b"tampered"),
            "review": lambda root, payload: (
                root / "packet" / blind_conversion_ab.REVIEW_NAME
            ).write_bytes(
                (root / "packet" / blind_conversion_ab.REVIEW_NAME).read_bytes()
                + b"\n"
            ),
            "stability": lambda root, payload: (
                root / payload["stability"][0]["path"]
            ).write_bytes(
                (root / payload["stability"][0]["path"]).read_bytes() + b"\n"
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                policy, evidence = self._build(root)
                payload = json.loads(evidence.read_text(encoding="utf-8"))
                mutate(root, payload)
                evidence.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "hash|policy"):
                    gate.evaluate(policy, evidence)

    def test_wrong_source_is_rejected_after_hash_is_updated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy, evidence = self._build(root)
            payload = json.loads(
                (root / "M1.jsonl").read_text(encoding="utf-8").splitlines()[0]
            )
            payload["source_ref"] = "e" * 40
            lines = (root / "M1.jsonl").read_text(encoding="utf-8").splitlines()
            lines[0] = json.dumps(payload, ensure_ascii=False)
            (root / "M1.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
            self._sync_run_hash(root, evidence, "mozc", "M1")
            with self.assertRaisesRegex(ValueError, "inconsistent source_ref|source"):
                gate.evaluate(policy, evidence)

    def test_duplicate_stability_id_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy, evidence = self._build(root)
            payload = json.loads(evidence.read_text(encoding="utf-8"))
            payload["stability"].append(copy.deepcopy(payload["stability"][0]))
            evidence.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate id"):
                gate.evaluate(policy, evidence)

    def test_stability_counts_are_derived_from_native_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy, evidence = self._build(root)
            self._rewrite_stability_raw(
                root,
                evidence,
                stability.FCITX_LONG_SOAK_ID,
                lambda raw: raw.__setitem__("conversions", 0),
            )
            with self.assertRaisesRegex(ValueError, "conversions"):
                gate.evaluate(policy, evidence)

    def test_stability_raw_result_rejects_a_trusted_passed_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy, evidence = self._build(root)
            self._rewrite_stability_raw(
                root,
                evidence,
                stability.FCITX_LONG_SOAK_ID,
                lambda raw: raw.__setitem__("passed", True),
            )
            with self.assertRaisesRegex(ValueError, "unknown=.*passed"):
                gate.evaluate(policy, evidence)

    def test_stability_rejects_forged_native_schema_and_artifact_kind(self) -> None:
        mutations = {
            "schema": lambda record: record["native_result"].__setitem__(
                "schema", "hazkey.generic-counts.v1"
            ),
            "artifact-kind": lambda record: record["artifact"].__setitem__(
                "kind", "fault-fixture"
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                policy, evidence = self._build(root)
                self._rewrite_stability_record(
                    root,
                    evidence,
                    stability.FCITX_LONG_SOAK_ID,
                    mutate,
                )
                with self.assertRaisesRegex(ValueError, "schema|artifact"):
                    gate.evaluate(policy, evidence)

    def test_stability_rejects_forged_native_producer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy, evidence = self._build(root)
            self._rewrite_stability_raw(
                root,
                evidence,
                stability.FCITX_LONG_SOAK_ID,
                lambda raw: raw["producer"].__setitem__("sha256", "0" * 64),
            )
            with self.assertRaisesRegex(ValueError, "producer.sha256"):
                gate.evaluate(policy, evidence)

    def test_stability_rejects_forged_fcitx_snapshot_policy_pin(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy_path, evidence_path = self._build(root)
            policy = json.loads(policy_path.read_text(encoding="utf-8"))
            forged = "sha256:" + "0" * 64
            for contract in policy["gates"]["long_running_stability"]["checks"]:
                if contract["id"] in {
                    stability.FCITX_LONG_SOAK_ID,
                    stability.FCITX_LIFECYCLE_ID,
                }:
                    contract["input_snapshot_fingerprint"] = forged
            policy_path.write_text(json.dumps(policy), encoding="utf-8")
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            evidence["policy"]["sha256"] = digest(policy_path.read_bytes())
            evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
            with self.assertRaisesRegex(
                ValueError,
                "input_snapshot.fingerprint",
            ):
                gate.evaluate(policy_path, evidence_path)

    def test_adapter_process_audit_rejects_residue_and_identity_forgery(self) -> None:
        mutations = {
            "residue": lambda raw: raw["execution"]["process_audit"].update(
                {"process_group_cleanup": False, "residue_count": 1}
            ),
            "detached-session-residue": lambda raw: raw["execution"][
                "process_audit"
            ].update({"session_cleanup": False, "residue_count": 1}),
            "helper-identity": lambda raw: raw["execution"]["process_audit"][
                "helpers"
            ][0]["executable"].__setitem__("sha256", "sha256:" + "0" * 64),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                policy, evidence = self._build(root)
                self._rewrite_stability_raw(
                    root,
                    evidence,
                    stability.ADAPTER_SOAK_ID,
                    mutate,
                )
                with self.assertRaisesRegex(
                    ValueError,
                    "process_group_cleanup|session_cleanup|executable.sha256",
                ):
                    gate.evaluate(policy, evidence)

    def test_stability_path_rejects_a_symlink_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy, evidence = self._build(root)
            payload = json.loads(evidence.read_text(encoding="utf-8"))
            original = payload["stability"][0]["path"]
            (root / "linked-evidence").symlink_to(root, target_is_directory=True)
            payload["stability"][0]["path"] = f"linked-evidence/{original}"
            evidence.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "symlink ancestor"):
                gate.evaluate(policy, evidence)

    def test_stability_path_read_stays_on_open_dirfd_during_ancestor_swap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence_root = root / "evidence"
            safe = evidence_root / "safe"
            outside = root / "outside"
            safe.mkdir(parents=True)
            outside.mkdir()
            trusted = b'{"trusted":true}\n'
            malicious = b'{"trusted":false}\n'
            (safe / "data.json").write_bytes(trusted)
            (outside / "data.json").write_bytes(malicious)
            original_open = gate.os.open
            swapped = False

            def swapping_open(
                path: object,
                flags: int,
                mode: int = 0o777,
                *,
                dir_fd: int | None = None,
            ) -> int:
                nonlocal swapped
                if dir_fd is None:
                    descriptor = original_open(path, flags, mode)
                else:
                    descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
                if path == "safe" and dir_fd is not None and not swapped:
                    safe.rename(evidence_root / "held")
                    safe.symlink_to(outside, target_is_directory=True)
                    swapped = True
                return descriptor

            with mock.patch.object(gate.os, "open", side_effect=swapping_open):
                logical_path, data = gate._self_contained_path(
                    "safe/data.json", evidence_root, "race evidence"
                )
            self.assertTrue(swapped)
            self.assertEqual(logical_path, evidence_root / "safe/data.json")
            self.assertEqual(data, trusted)
            self.assertNotEqual(data, logical_path.read_bytes())
            self.assertEqual(
                gate._verified_data(data, digest(trusted), "race evidence"),
                trusted,
            )

    def test_protocol_process_audit_must_match_raw_server_pids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy, evidence = self._build(root)
            self._rewrite_stability_raw(
                root,
                evidence,
                stability.PROTOCOL_STEADY_ID,
                lambda raw: raw["execution"]["process_audit"]["servers"][0].__setitem__(
                    "pid", 424242
                ),
            )
            with self.assertRaisesRegex(ValueError, "server process identities"):
                gate.evaluate(policy, evidence)

    def test_protocol_rejects_unparsed_metric_and_provenance_holes(self) -> None:
        mutations: dict[str, Callable[[dict[str, object]], None]] = {
            "dictionary": lambda raw: raw["dictionary"].__setitem__(
                "fingerprint", "sha256:" + "0" * 64
            ),
            "execution": lambda raw: raw.__setitem__("execution", None),
            "latency": lambda raw: raw["backends"][1].__setitem__(
                "latency_ms", None
            ),
            "memory": lambda raw: raw["backends"][1].__setitem__("memory", None),
            "comparison": lambda raw: raw.__setitem__("comparison", None),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                policy, evidence = self._build(root)
                self._rewrite_protocol_benchmark(root, evidence, mutate)
                with self.assertRaises(ValueError):
                    gate.evaluate(policy, evidence)

    def test_protocol_server_and_helper_roles_must_be_disjoint(self) -> None:
        def collide_wrapper_identity(raw: dict[str, object]) -> None:
            audit = raw["execution"]["process_audit"]
            audit["helpers"][0]["pid"] = audit["servers"][0]["pid"]
            audit["helpers"][0]["start_time_ticks"] = audit["servers"][0][
                "start_time_ticks"
            ]

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy, evidence = self._build(root)
            self._rewrite_stability_raw(
                root,
                evidence,
                stability.PROTOCOL_STEADY_ID,
                collide_wrapper_identity,
            )
            with self.assertRaisesRegex(ValueError, "must be disjoint"):
                gate.evaluate(policy, evidence)

    def test_recovery_requires_all_exact_named_subchecks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy, evidence = self._build(root)
            self._rewrite_stability_raw(
                root,
                evidence,
                stability.PROTOCOL_RECOVERY_ID,
                lambda raw: raw["subchecks"].pop(),
            )
            with self.assertRaisesRegex(ValueError, "subchecks"):
                gate.evaluate(policy, evidence)

    def test_recovery_bound_logs_must_name_the_exact_passing_test(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy, evidence = self._build(root)

            def replace_stdout_with_empty_stderr(raw: dict[str, object]) -> None:
                raw["subchecks"][0]["stdout"] = copy.deepcopy(
                    raw["subchecks"][0]["stderr"]
                )

            self._rewrite_stability_raw(
                root,
                evidence,
                stability.PROTOCOL_RECOVERY_ID,
                replace_stdout_with_empty_stderr,
            )
            with self.assertRaisesRegex(ValueError, "exact test as passed"):
                gate.evaluate(policy, evidence)

    def test_swift_package_snapshot_tampering_is_rehashed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy, evidence = self._build(root)
            package_root = root / stability.SWIFT_PACKAGE_SNAPSHOT_PATH
            source = package_root / "Sources/hazkey-server/ImeReducer.swift"
            source.chmod(0o644)
            source.write_bytes(source.read_bytes() + b"\n// tampered\n")
            source.chmod(0o444)
            with self.assertRaisesRegex(ValueError, "swift_package|fingerprint|identity"):
                gate.evaluate(policy, evidence)

    def test_recovery_requires_clean_process_group_and_session(self) -> None:
        mutations = {
            "group": lambda raw: raw["subchecks"][0]["cleanup"].__setitem__(
                "process_group", False
            ),
            "session": lambda raw: raw["subchecks"][0]["cleanup"].__setitem__(
                "session", False
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                policy, evidence = self._build(root)
                self._rewrite_stability_raw(
                    root,
                    evidence,
                    stability.PROTOCOL_RECOVERY_ID,
                    mutate,
                )
                with self.assertRaisesRegex(ValueError, "process_group|session"):
                    gate.evaluate(policy, evidence)

    def test_fcitx_native_rejects_forged_bindings_and_process_identities(self) -> None:
        def duplicate_command_option(raw: dict[str, object]) -> None:
            command = raw["command"]
            assert isinstance(command, list)
            command[command.index("--addon")] = "--harness"

        def reuse_process_identity(raw: dict[str, object]) -> None:
            cycles = raw["cycle_results"]
            assert isinstance(cycles, list)
            first = cycles[0]["server"]
            second = cycles[1]["server"]
            second["launches"] = copy.deepcopy(first["launches"])
            second["observed_identities"][0]["pid"] = first["observed_identities"][0]["pid"]
            second["observed_identities"][0]["start_time"] = first["observed_identities"][0]["start_time"]

        def reuse_session(raw: dict[str, object]) -> None:
            cycles = raw["cycle_results"]
            assert isinstance(cycles, list)
            session_id = cycles[0]["server"]["observed_identities"][0]["session_id"]
            cycles[1]["server"]["observed_identities"][0]["session_id"] = session_id
            cycles[1]["helper"]["observed_identities"][0]["session_id"] = session_id

        mutations: dict[str, Callable[[dict[str, object]], None]] = {
            "source-schema": lambda raw: raw["source"].__setitem__("unknown", True),
            "git-head-format": lambda raw: raw["source"].__setitem__(
                "git_head", "not-a-commit"
            ),
            "worktree-type": lambda raw: raw["source"].__setitem__(
                "worktree_clean", "dirty"
            ),
            "repository-root": lambda raw: raw["source"].__setitem__(
                "repository_root", "/tmp/forged-repository"
            ),
            "producer-path": lambda raw: raw["producer"].__setitem__(
                "path", "/tmp/forged-repository/fcitx5-hazkey/tests/run_fcitx_full_stack_test.py"
            ),
            "python-interpreter": lambda raw: raw["command"].__setitem__(
                0, "/tmp/python3"
            ),
            "duplicate-command-option": duplicate_command_option,
            "missing-artifact": lambda raw: raw["artifacts"].pop("mozc_helper"),
            "snapshot-fingerprint": lambda raw: raw["input_snapshot"].__setitem__(
                "fingerprint", "sha256:" + "0" * 64
            ),
            "runtime-integrity": lambda raw: raw["runtime_integrity"].__setitem__(
                "verified_artifacts", []
            ),
            "launch-identity": lambda raw: raw["cycle_results"][0]["server"][
                "observed_identities"
            ][0].__setitem__("pid", 999_999),
            "executable": lambda raw: raw["cycle_results"][0]["helper"][
                "observed_identities"
            ][0].__setitem__("executable", "/forged/helper"),
            "reused-process": reuse_process_identity,
            "reused-session": reuse_session,
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                policy, evidence = self._build(root)
                self._rewrite_stability_raw(
                    root,
                    evidence,
                    stability.FCITX_LIFECYCLE_ID,
                    mutate,
                )
                with self.assertRaises(ValueError):
                    gate.evaluate(policy, evidence)

    def test_fcitx_retained_snapshot_and_runtime_tampering_is_rejected(self) -> None:
        def remove(path: Path) -> None:
            path.parent.chmod(0o755)
            path.unlink()
            path.parent.chmod(0o555)

        def add(path: Path) -> None:
            path.parent.chmod(0o755)
            path.write_bytes(b"unexpected\n")
            path.chmod(0o444)
            path.parent.chmod(0o555)

        def replace_with_symlink(path: Path) -> None:
            path.parent.chmod(0o755)
            path.unlink()
            path.symlink_to("/dev/null")
            path.parent.chmod(0o555)

        def tamper(path: Path) -> None:
            mode = stat.S_IMODE(path.stat().st_mode)
            path.chmod(0o600)
            path.write_bytes(b"tampered retained evidence\n")
            path.chmod(mode)

        mutations = {
            "snapshot-tamper": lambda snapshot, runtime: tamper(snapshot / "harness"),
            "snapshot-missing": lambda snapshot, runtime: remove(snapshot / "harness"),
            "snapshot-extra": lambda snapshot, runtime: add(snapshot / "unexpected"),
            "snapshot-symlink": lambda snapshot, runtime: replace_with_symlink(
                snapshot / "harness"
            ),
            "runtime-tamper": lambda snapshot, runtime: tamper(
                runtime / "fcitx5-grimodex-mozc-helper"
            ),
            "runtime-missing": lambda snapshot, runtime: remove(runtime / "mozc.data"),
            "runtime-extra": lambda snapshot, runtime: add(runtime / "unexpected"),
            "runtime-symlink": lambda snapshot, runtime: replace_with_symlink(
                runtime / "mozc.data"
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                policy, evidence = self._build(root)
                manifest = json.loads(evidence.read_text(encoding="utf-8"))
                entry = next(
                    item
                    for item in manifest["stability"]
                    if item["id"] == stability.FCITX_LIFECYCLE_ID
                )
                record_path = root / entry["path"]
                record = json.loads(record_path.read_text(encoding="utf-8"))
                native_path = record_path.parent / record["native_result"]["path"]
                native = json.loads(native_path.read_text(encoding="utf-8"))
                snapshot = Path(native["input_snapshot"]["root"])
                runtime = Path(native["artifacts"]["mozc_generation"]["path"])
                mutate(snapshot, runtime)
                with self.assertRaises(ValueError):
                    gate.evaluate(policy, evidence)

    def test_atomic_output_fsyncs_file_and_parent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "result.json"
            with mock.patch.object(gate.os, "fsync", wraps=gate.os.fsync) as fsync:
                gate._write_atomic(output, "{}\n")
            self.assertEqual(output.read_text(encoding="utf-8"), "{}\n")
            self.assertGreaterEqual(fsync.call_count, 2)

    def test_duplicate_raw_run_under_a_new_id_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy, evidence = self._build(root)
            payload = json.loads(evidence.read_text(encoding="utf-8"))
            duplicate = copy.deepcopy(payload["raw_runs"]["mozc"][0])
            duplicate["id"] = "duplicate"
            payload["raw_runs"]["mozc"].append(duplicate)
            evidence.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate raw run"):
                gate.evaluate(policy, evidence)

    def test_policy_with_unfrozen_or_empty_stability_ids_is_rejected(self) -> None:
        policy = json.loads(POLICY_FIXTURE.read_text(encoding="utf-8"))
        policy["candidate"]["resource_fingerprint"] = stability.B0_RESOURCE_FINGERPRINT
        parsed = gate.parse_policy(json.dumps(policy).encode())
        self.assertEqual(parsed.gate.required_stability_ids, stability.SUITE_IDS)
        unfrozen = copy.deepcopy(policy)
        unfrozen["gates"]["long_running_stability"] = {
            "required_result": "all_pass",
            "check_contracts_frozen": False,
            "checks": None,
        }
        unfrozen["measurement_contracts"]["long_running_stability"]["status"] = (
            "pending"
        )
        unfrozen["readiness"] = {
            "formal_decision_enabled": False,
            "blocking_items": ["long_running_stability_check_contracts"],
        }
        with self.assertRaisesRegex(ValueError, "formal decision is not ready"):
            gate.parse_policy(json.dumps(unfrozen).encode())
        policy["gates"]["long_running_stability"] = {
            "required_result": "all_pass",
            "check_contracts_frozen": True,
            "checks": [],
        }
        with self.assertRaisesRegex(ValueError, "non-empty"):
            gate.parse_policy(json.dumps(policy).encode())

    def test_frozen_stability_requirements_are_exact_suite_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy_path, _ = self._build(root)
            policy = json.loads(policy_path.read_text(encoding="utf-8"))
            first = policy["gates"]["long_running_stability"]["checks"][0]
            first["minimum_conversions"] = 149999
            with self.assertRaisesRegex(ValueError, "minimum_conversions"):
                gate.parse_policy(json.dumps(policy).encode())

    def test_fcitx_snapshot_policy_pin_is_exact_and_shared(self) -> None:
        policy = json.loads(POLICY_FIXTURE.read_text(encoding="utf-8"))
        expected = (
            "sha256:bb4f63a09a16fd0cb00bc41ee6091dca"
            "7e3fa85c118ebae688cd7ada6bd99573"
        )
        contracts = {
            contract["id"]: contract
            for contract in policy["gates"]["long_running_stability"]["checks"]
        }
        self.assertEqual(
            {
                contracts[stability.FCITX_LONG_SOAK_ID][
                    "input_snapshot_fingerprint"
                ],
                contracts[stability.FCITX_LIFECYCLE_ID][
                    "input_snapshot_fingerprint"
                ],
            },
            {expected},
        )
        with tempfile.TemporaryDirectory() as temporary:
            synthetic_policy_path, _ = self._build(Path(temporary))
            divergent = json.loads(
                synthetic_policy_path.read_text(encoding="utf-8")
            )
            for contract in divergent["gates"]["long_running_stability"][
                "checks"
            ]:
                if contract["id"] == stability.FCITX_LIFECYCLE_ID:
                    contract["input_snapshot_fingerprint"] = (
                        "sha256:" + "0" * 64
                    )
            with self.assertRaisesRegex(
                ValueError,
                "same input snapshot fingerprint",
            ):
                gate.parse_policy(json.dumps(divergent).encode())

    def test_unknown_or_missing_evidence_fields_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy, evidence = self._build(root)
            original = json.loads(evidence.read_text(encoding="utf-8"))
            for mutation in ("missing", "unknown"):
                payload = copy.deepcopy(original)
                if mutation == "missing":
                    del payload["judgments"]
                else:
                    payload["unexpected"] = True
                evidence.write_text(json.dumps(payload), encoding="utf-8")
                with self.subTest(mutation=mutation), self.assertRaisesRegex(ValueError, "fields do not match schema"):
                    gate.evaluate(policy, evidence)


class CheckedInCorpusLockTests(unittest.TestCase):
    def test_ajimee_tsv_is_exactly_100_rows_and_matches_both_policy_hashes(self) -> None:
        fixture_root = POLICY_FIXTURE.parent
        path = fixture_root / "external-ajimee-unconditional.tsv"
        data = path.read_bytes()
        rows = build_frozen_corpus._parse_tsv(data, str(path))
        self.assertEqual(len(rows), 100)
        actual = digest(data)
        policy = json.loads(POLICY_FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(
            actual,
            policy["formal_suite"]["components"]["ajimee_unconditional"][
                "sha256"
            ],
        )
        self.assertEqual(
            actual,
            policy["external_sources"]["ajimee_bench"]["derived_sha256"],
        )


if __name__ == "__main__":
    unittest.main()
