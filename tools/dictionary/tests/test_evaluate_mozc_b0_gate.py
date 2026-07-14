from __future__ import annotations

from collections.abc import Callable
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT))

from tools.dictionary import blind_conversion_ab  # noqa: E402
from tools.dictionary import build_frozen_corpus  # noqa: E402
from tools.dictionary import evaluate_mozc_b0_gate as gate  # noqa: E402
from tools.dictionary import run_mozc_b0_measurement as acquisition  # noqa: E402


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

    def _build(self, root: Path) -> tuple[Path, Path]:
        source_ref = "d" * 40
        baseline_fingerprint = "sha256:" + "b" * 64
        candidate_fingerprint = "sha256:" + "c" * 64
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
        stability_contracts = [
            {
                "id": "soak",
                "protocol": "hazkey.mozc-soak.v1",
                "command": ["./scripts/grimodex-ime_mozc.sh", "soak"],
                "minimum_conversions": 150_000,
                "minimum_cycles": 1,
                "expected_counts": {
                    "helper_launches": 1,
                    "server_launches": 1,
                    "helper_recoveries": 0,
                    "server_recoveries": 0,
                    "residue_count": 0,
                },
            },
            {
                "id": "restart",
                "protocol": "hazkey.mozc-restart-recovery.v1",
                "command": ["./scripts/grimodex-ime_mozc.sh", "restart-soak"],
                "minimum_conversions": 300,
                "minimum_cycles": 3,
                "expected_counts": {
                    "helper_launches": 4,
                    "server_launches": 1,
                    "helper_recoveries": 3,
                    "server_recoveries": 0,
                    "residue_count": 0,
                },
            },
        ]
        policy["gates"]["long_running_stability"] = {
            "required_result": "all_pass",
            "check_contracts_frozen": True,
            "checks": stability_contracts,
        }
        policy["measurement_contracts"]["long_running_stability"]["status"] = "ready"
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

        stability: list[dict[str, str]] = []
        for contract in stability_contracts:
            check_id = contract["id"]
            raw_result_path = root / f"stability-{check_id}-raw.json"
            raw_result_path.write_text(
                json.dumps(
                    {
                        "schema": gate.STABILITY_RAW_RESULT_SCHEMA,
                        "id": check_id,
                        "protocol": contract["protocol"],
                        "command": contract["command"],
                        "product_source_ref": source_ref,
                        "artifact_fingerprint": candidate_fingerprint,
                        "observations": {
                            "exit_code": 0,
                            "conversions": contract["minimum_conversions"],
                            "cycles": contract["minimum_cycles"],
                            **contract["expected_counts"],
                        },
                    }
                ),
                encoding="utf-8",
            )
            path = root / f"stability-{check_id}.json"
            path.write_text(
                json.dumps(
                    {
                        "schema": gate.STABILITY_SCHEMA,
                        "id": check_id,
                        "protocol": contract["protocol"],
                        "command": contract["command"],
                        "product_source_ref": source_ref,
                        "artifact_fingerprint": candidate_fingerprint,
                        "raw_result": {
                            "path": raw_result_path.name,
                            "sha256": digest(raw_result_path.read_bytes()),
                        },
                    }
                ),
                encoding="utf-8",
            )
            stability.append({"id": check_id, "path": path.name, "sha256": digest(path.read_bytes())})

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
            "stability": stability,
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
        raw_path = record_path.parent / record["raw_result"]["path"]
        raw_result = json.loads(raw_path.read_text(encoding="utf-8"))
        mutate(raw_result)
        raw_path.write_text(json.dumps(raw_result), encoding="utf-8")
        record["raw_result"]["sha256"] = digest(raw_path.read_bytes())
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

    def test_stability_pass_is_derived_from_raw_observations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy, evidence = self._build(root)
            self._rewrite_stability_raw(
                root,
                evidence,
                "soak",
                lambda raw: raw["observations"].__setitem__("conversions", 0),
            )
            result = gate.evaluate(policy, evidence)
            self.assertFalse(result["passed"])
            self.assertFalse(result["metrics"]["stability"]["soak"])
            self.assertFalse(check(result["checks"], "stability:soak")["passed"])

    def test_stability_raw_result_rejects_a_trusted_passed_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy, evidence = self._build(root)
            self._rewrite_stability_raw(
                root,
                evidence,
                "soak",
                lambda raw: raw.__setitem__("passed", True),
            )
            with self.assertRaisesRegex(ValueError, "unknown=.*passed"):
                gate.evaluate(policy, evidence)

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
        with self.assertRaisesRegex(ValueError, "formal decision is not ready"):
            gate.parse_policy(json.dumps(policy).encode())
        policy["gates"]["long_running_stability"] = {
            "required_result": "all_pass",
            "check_contracts_frozen": True,
            "checks": [],
        }
        with self.assertRaisesRegex(ValueError, "non-empty"):
            gate.parse_policy(json.dumps(policy).encode())

    def test_frozen_stability_requirements_may_be_zero_but_not_all_zero(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            policy_path, _ = self._build(root)
            policy = json.loads(policy_path.read_text(encoding="utf-8"))
            first = policy["gates"]["long_running_stability"]["checks"][0]
            first["minimum_conversions"] = 0
            first["minimum_cycles"] = 0
            first["expected_counts"] = {
                field: 0 for field in first["expected_counts"]
            }
            with self.assertRaisesRegex(ValueError, "non-zero requirement"):
                gate.parse_policy(json.dumps(policy).encode())
            first["expected_counts"]["helper_launches"] = 1
            parsed = gate.parse_policy(json.dumps(policy).encode())
            self.assertEqual(parsed.stability_checks["soak"].minimum_conversions, 0)

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
