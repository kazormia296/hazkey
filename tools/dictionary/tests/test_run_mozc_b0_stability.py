from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT))

from tools.dictionary import run_mozc_b0_stability as stability  # noqa: E402
from tools.dictionary import evaluate_mozc_b0_gate as gate  # noqa: E402


POLICY = (
    REPOSITORY_ROOT
    / "hazkey-server/Tests/grimodex-spike/Fixtures/mozc-adoption-v1/b0-policy.json"
)
PROTOCOL_FIXTURE = (
    REPOSITORY_ROOT
    / "docs/spikes/fcitx-mozkey-followup-2026-07-14/"
    "protocol-v2-backend-benchmark.json"
)


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def load_fcitx_runner():
    path = REPOSITORY_ROOT / stability.FCITX_PRODUCER_PATH
    name = "test_run_mozc_b0_stability_fcitx_runner"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class MozcB0StabilityContractTests(unittest.TestCase):
    def _prepare_recovery_inputs(self, root: Path) -> tuple[Path, Path, Path]:
        server = root / "hazkey-server"
        server.write_bytes(b"recovery server\n")
        server.chmod(0o700)
        runtime = root / "runtime-lib"
        runtime.mkdir()
        source = REPOSITORY_ROOT / stability.RECOVERY_SOURCE_PATH
        source_sha = digest(source.read_bytes())
        policy = json.loads(POLICY.read_text(encoding="utf-8"))
        policy["candidate"]["product_executable"] = {
            "size_bytes": server.stat().st_size,
            "sha256": digest(server.read_bytes()),
        }
        for index, item in enumerate(
            policy["candidate"]["runtime_dependencies"]["files"]
        ):
            name = item["path"]
            data = f"recovery runtime {index}: {name}\n".encode()
            (runtime / name).write_bytes(data)
            item["size_bytes"] = len(data)
            item["sha256"] = digest(data)
        contract = next(
            item
            for item in policy["gates"]["long_running_stability"]["checks"]
            if item["id"] == stability.PROTOCOL_RECOVERY_ID
        )
        contract["native_schema"] = stability.RECOVERY_SCHEMA
        contract["native_producer"]["sha256"] = source_sha
        contract["recovery_fixture_identity"] = stability.recovery_fixture_identity(
            source_sha
        )
        policy_path = root / "policy.json"
        policy_path.write_text(json.dumps(policy), encoding="utf-8")
        return server, runtime, policy_path

    def test_checked_policy_freezes_five_native_commands_and_orchestrator(self) -> None:
        policy = json.loads(POLICY.read_text(encoding="utf-8"))
        checks = policy["gates"]["long_running_stability"]["checks"]
        self.assertEqual(tuple(item["id"] for item in checks), stability.SUITE_IDS)
        for check in checks:
            self.assertEqual(
                tuple(check["command"]),
                stability.CANONICAL_COMMANDS[check["id"]],
            )
            self.assertEqual(
                check["native_schema"], stability.native_schema(check["id"])
            )
            expected_runner = stability.SUITE_REQUIREMENTS[check["id"]][
                "execution_runner_path"
            ]
            if expected_runner is None:
                self.assertIsNone(check["execution_runner"])
            else:
                self.assertEqual(check["execution_runner"]["path"], expected_runner)
                self.assertEqual(
                    check["execution_runner"]["sha256"],
                    digest((REPOSITORY_ROOT / expected_runner).read_bytes()),
                )
        orchestrator = policy["measurement_contracts"]["long_running_stability"][
            "orchestrator"
        ]
        self.assertEqual(orchestrator["path"], stability.ORCHESTRATOR_PATH)
        self.assertEqual(
            orchestrator["sha256"],
            digest((REPOSITORY_ROOT / stability.ORCHESTRATOR_PATH).read_bytes()),
        )
        self.assertEqual(
            policy["readiness"]["blocking_items"],
            [],
        )
        self.assertTrue(policy["readiness"]["formal_decision_enabled"])
        frozen_snapshot = (
            "sha256:bb4f63a09a16fd0cb00bc41ee6091dca"
            "7e3fa85c118ebae688cd7ada6bd99573"
        )
        for suite_id in stability.SUITE_IDS:
            expectations = stability.expectations_from_policy(POLICY, suite_id)
            if suite_id in {
                stability.FCITX_LONG_SOAK_ID,
                stability.FCITX_LIFECYCLE_ID,
            }:
                self.assertEqual(
                    expectations.input_snapshot_fingerprint,
                    frozen_snapshot,
                )
            else:
                self.assertIsNone(expectations.input_snapshot_fingerprint)

    def test_records_bind_native_bytes_without_generic_counts_or_passed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            native = root / "native.json"
            native.write_text("{}\n", encoding="utf-8")
            b0 = stability.build_record(
                stability.FCITX_LONG_SOAK_ID,
                native,
                native.read_bytes(),
                artifact_fingerprint=stability.B0_RESOURCE_FINGERPRINT,
                recovery_fixture_identity_value=None,
            )
            recovery = stability.build_record(
                stability.PROTOCOL_RECOVERY_ID,
                native,
                native.read_bytes(),
                artifact_fingerprint=stability.B0_RESOURCE_FINGERPRINT,
                recovery_fixture_identity_value="sha256:" + "1" * 64,
            )
        self.assertEqual(b0["artifact"]["kind"], "b0")
        self.assertEqual(recovery["artifact"]["kind"], "fault-fixture")
        for record in (b0, recovery):
            self.assertNotIn("passed", record)
            self.assertNotIn("observations", record)
            self.assertNotIn("counts", record)

    def test_protocol_native_counts_are_rederived_and_forgery_is_rejected(self) -> None:
        payload = json.loads(PROTOCOL_FIXTURE.read_text(encoding="utf-8"))
        payload["execution"]["build_configuration"] = "formal-stability"
        payload["execution"]["toolchain"] = "swift-test.sh"
        benchmark_source = REPOSITORY_ROOT / stability.PROTOCOL_BENCHMARK_SOURCE_PATH
        test_runner = REPOSITORY_ROOT / stability.SWIFT_TEST_RUNNER_PATH
        package_files = stability._read_swift_package_inputs(REPOSITORY_ROOT)
        package_identity = stability._swift_package_identity_from_files(package_files)
        expected = stability.NativeExpectations(
            product_source_ref=payload["source_ref"],
            artifact_fingerprint=stability.B0_RESOURCE_FINGERPRINT,
            product_server_size=payload["server"]["size_bytes"],
            product_server_sha256="sha256:" + payload["server"]["sha256"],
            artifacts={
                "fcitx5-grimodex-mozc-helper": (
                    payload["mozc_helper"]["size_bytes"],
                    "sha256:" + payload["mozc_helper"]["sha256"],
                ),
                "mozc.data": (
                    payload["mozc_data"]["size_bytes"],
                    "sha256:" + payload["mozc_data"]["sha256"],
                ),
            },
            native_producer_sha256=digest(benchmark_source.read_bytes()),
            recovery_fixture_identity=None,
            execution_runner_path=stability.SWIFT_TEST_RUNNER_PATH,
            execution_runner_sha256=digest(test_runner.read_bytes()),
            swift_package_file_count=package_identity[0],
            swift_package_size_bytes=package_identity[1],
            swift_package_fingerprint=package_identity[2],
            baseline_resource_fingerprint=payload["dictionary"]["fingerprint"],
        )
        server_pids = [
            backend["process_stability"]["server_pid"]
            for backend in payload["backends"]
        ]
        helper_pid = payload["backends"][1]["process_stability"][
            "child_pids_before"
        ][0]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw = root / "benchmark.json"
            stdout = root / "stdout"
            stderr = root / "stderr"
            native = root / "native.json"
            stability._materialize_swift_package_snapshot(root, package_files)
            stdout.write_bytes(
                b"testProtocolV2BackendComparisonKeepsLongLivedProcessesStable passed\n"
            )
            stderr.write_bytes(b"")

            def write_native(benchmark: dict[str, object]) -> bytes:
                raw.write_text(json.dumps(benchmark), encoding="utf-8")
                wrapper = {
                    "schema": stability.PROTOCOL_STEADY_SCHEMA,
                    "producer": {
                        "path": stability.ORCHESTRATOR_PATH,
                        "sha256": digest(
                            (REPOSITORY_ROOT / stability.ORCHESTRATOR_PATH).read_bytes()
                        ),
                    },
                    "product_source_ref": expected.product_source_ref,
                    "product_server": {
                        "size_bytes": expected.product_server_size,
                        "sha256": expected.product_server_sha256,
                    },
                    "artifact": {
                        "kind": "b0",
                        "fingerprint": expected.artifact_fingerprint,
                    },
                    "benchmark_source": {
                        "path": stability.PROTOCOL_BENCHMARK_SOURCE_PATH,
                        "snapshot_path": (
                            f"{stability.SWIFT_PACKAGE_SNAPSHOT_PATH}/"
                            f"{Path(stability.PROTOCOL_BENCHMARK_SOURCE_PATH).relative_to(stability.SWIFT_PACKAGE_ROOT).as_posix()}"
                        ),
                        "size_bytes": benchmark_source.stat().st_size,
                        "sha256": digest(benchmark_source.read_bytes()),
                    },
                    "test_runner": {
                        "path": stability.SWIFT_TEST_RUNNER_PATH,
                        "snapshot_path": (
                            f"{stability.SWIFT_PACKAGE_SNAPSHOT_PATH}/scripts/swift-test.sh"
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
                        "path": payload["dictionary"]["path"],
                        "fingerprint_before": expected.baseline_resource_fingerprint,
                        "fingerprint_after": expected.baseline_resource_fingerprint,
                    },
                    "execution": {
                        "command": list(stability.PROTOCOL_TEST_COMMAND),
                        "scratch_path": "swift-scratch",
                        "exit_code": 0,
                        "skipped": False,
                        "process_audit": {
                            "runner": {"pid": 1, "start_time_ticks": 1},
                            "servers": [
                                {
                                    "pid": pid,
                                    "start_time_ticks": index + 10,
                                    "executable": {
                                        "size_bytes": expected.product_server_size,
                                        "sha256": expected.product_server_sha256,
                                    },
                                }
                                for index, pid in enumerate(server_pids)
                            ],
                            "helpers": [
                                {
                                    "pid": helper_pid,
                                    "start_time_ticks": 20,
                                    "executable": {
                                        "size_bytes": expected.artifacts[
                                            "fcitx5-grimodex-mozc-helper"
                                        ][0],
                                        "sha256": expected.artifacts[
                                            "fcitx5-grimodex-mozc-helper"
                                        ][1],
                                    },
                                }
                            ],
                            "process_group_cleanup": True,
                            "session_cleanup": True,
                            "residue_count": 0,
                        },
                    },
                    "benchmark": {"path": raw.name, "sha256": digest(raw.read_bytes())},
                    "stdout": {"path": stdout.name, "sha256": digest(stdout.read_bytes())},
                    "stderr": {"path": stderr.name, "sha256": digest(stderr.read_bytes())},
                }
                rendered = json.dumps(wrapper).encode()
                native.write_bytes(rendered)
                return rendered

            observations = stability.validate_native_result(
                stability.PROTOCOL_STEADY_ID,
                write_native(payload),
                "protocol.json",
                expected,
                native_path=native,
            )
            self.assertEqual(observations["conversions"], 1500)
            self.assertEqual(observations["server_launches"], 2)
            self.assertEqual(observations["helper_launches"], 1)

            wrapper = json.loads(write_native(payload))
            wrapper["dictionary"]["fingerprint_after"] = "sha256:" + "0" * 64
            with self.assertRaisesRegex(ValueError, "fingerprint_after"):
                stability.validate_native_result(
                    stability.PROTOCOL_STEADY_ID,
                    json.dumps(wrapper).encode(),
                    "dictionary-wrapper-forgery.json",
                    expected,
                    native_path=native,
                )
            wrapper = json.loads(write_native(payload))
            wrapper["test_runner"]["sha256"] = "sha256:" + "0" * 64
            with self.assertRaisesRegex(ValueError, "test_runner.*sha256"):
                stability.validate_native_result(
                    stability.PROTOCOL_STEADY_ID,
                    json.dumps(wrapper).encode(),
                    "runner-wrapper-forgery.json",
                    expected,
                    native_path=native,
                )

            scenarios = {
                "schema": lambda value: value.__setitem__(
                    "schema", "hazkey.generic-counts.v1"
                ),
                "count": lambda value: value["backends"][1].__setitem__(
                    "conversion_count", 999999
                ),
                "warmups": lambda value: value["backends"][1].__setitem__(
                    "warmups_per_case", 4
                ),
                "iterations": lambda value: value["backends"][1].__setitem__(
                    "iterations_per_case", 99
                ),
                "empty-candidate-objects": lambda value: value["backends"][
                    1
                ].__setitem__("candidates", [{} for _ in range(15)]),
                "wrong-case-id": lambda value: value["backends"][1][
                    "candidates"
                ][0].__setitem__("id", "forged"),
                "wrong-category": lambda value: value["backends"][1][
                    "candidates"
                ][0].__setitem__("category", "forged"),
                "empty-surfaces": lambda value: value["backends"][1][
                    "candidates"
                ][0].__setitem__("candidates", []),
                "non-string-surface": lambda value: value["backends"][1][
                    "candidates"
                ][0].__setitem__("candidates", [None]),
                "dictionary": lambda value: value["dictionary"].__setitem__(
                    "fingerprint", "sha256:" + "0" * 64
                ),
                "execution": lambda value: value.__setitem__("execution", None),
                "latency": lambda value: value["backends"][1].__setitem__(
                    "latency_ms", None
                ),
                "memory": lambda value: value["backends"][1].__setitem__(
                    "memory", None
                ),
                "comparison": lambda value: value.__setitem__("comparison", None),
            }
            for name, mutate in scenarios.items():
                forged = copy.deepcopy(payload)
                mutate(forged)
                with self.subTest(name=name), self.assertRaises(ValueError):
                    stability.validate_native_result(
                        stability.PROTOCOL_STEADY_ID,
                        write_native(forged),
                        "forged.json",
                        expected,
                        native_path=native,
                    )

            wrapper_role_forgery = copy.deepcopy(payload)
            rendered = json.loads(write_native(wrapper_role_forgery))
            rendered["execution"]["process_audit"]["helpers"][0]["pid"] = rendered[
                "execution"
            ]["process_audit"]["servers"][0]["pid"]
            rendered["execution"]["process_audit"]["helpers"][0][
                "start_time_ticks"
            ] = rendered["execution"]["process_audit"]["servers"][0][
                "start_time_ticks"
            ]
            with self.assertRaisesRegex(ValueError, "must be disjoint"):
                stability.validate_native_result(
                    stability.PROTOCOL_STEADY_ID,
                    json.dumps(rendered).encode(),
                    "wrapper-role-forgery.json",
                    expected,
                    native_path=native,
                )

            raw_role_forgery = copy.deepcopy(payload)
            raw_role_forgery["backends"][1]["process_stability"][
                "child_pids_before"
            ] = [server_pids[0]]
            raw_role_forgery["backends"][1]["process_stability"][
                "child_pids_after"
            ] = [server_pids[0]]
            rendered = json.loads(write_native(raw_role_forgery))
            rendered["execution"]["process_audit"]["helpers"][0]["pid"] = server_pids[0]
            with self.assertRaisesRegex(ValueError, "must be disjoint"):
                stability.validate_native_result(
                    stability.PROTOCOL_STEADY_ID,
                    json.dumps(rendered).encode(),
                    "raw-role-forgery.json",
                    expected,
                    native_path=native,
                )

    def test_recovery_fixture_identity_binds_exact_source_and_subchecks(self) -> None:
        first = stability.recovery_fixture_identity("sha256:" + "1" * 64)
        second = stability.recovery_fixture_identity("sha256:" + "2" * 64)
        self.assertNotEqual(first, second)
        self.assertRegex(first, r"^sha256:[0-9a-f]{64}$")

    def test_binding_rejects_a_symlink_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "outside"
            outside.mkdir()
            target = outside / "raw.json"
            target.write_bytes(b"{}\n")
            evidence_root = root / "evidence"
            evidence_root.mkdir()
            (evidence_root / "escape").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink|ancestor"):
                stability._binding_bytes(
                    {"path": "escape/raw.json", "sha256": digest(target.read_bytes())},
                    evidence_root,
                    "binding",
                )

    def test_fcitx_retained_root_is_no_replace_and_rolls_back_failure(self) -> None:
        runner = load_fcitx_runner()

        def fixture(root: Path):
            private = root / "private"
            snapshot_root = private / "evidence-inputs"
            snapshot_root.mkdir(parents=True, mode=0o700)
            private.chmod(0o700)
            snapshot_root.chmod(0o555)
            snapshot = runner.InputSnapshot(
                root=snapshot_root,
                entries=(),
                directories=(".",),
                fingerprint=runner.snapshot_fingerprint((), (".",)),
            )
            return private, argparse.Namespace(), snapshot

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "result.json"
            private, args, snapshot = fixture(root)
            destination = runner.open_result_output(output)
            final = root / runner.retained_evidence_root_name(
                output.name, snapshot.fingerprint
            )
            final.mkdir()
            marker = final / "marker"
            marker.write_bytes(b"do not replace\n")
            try:
                with self.assertRaisesRegex(FileExistsError, "already exists"):
                    runner.bind_content_addressed_evidence_root(
                        private, args, snapshot, destination
                    )
            finally:
                destination.close()
            self.assertEqual(marker.read_bytes(), b"do not replace\n")
            self.assertTrue(private.is_dir())

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "result.json"
            private, args, snapshot = fixture(root)
            destination = runner.open_result_output(output)
            final = root / runner.retained_evidence_root_name(
                output.name, snapshot.fingerprint
            )
            try:
                with (
                    mock.patch.object(
                        runner,
                        "verify_input_snapshot",
                        side_effect=runner.ResultEvidenceError("injected failure"),
                    ),
                    self.assertRaisesRegex(
                        runner.ResultEvidenceError, "injected failure"
                    ),
                ):
                    runner.bind_content_addressed_evidence_root(
                        private, args, snapshot, destination
                    )
            finally:
                destination.close()
            self.assertFalse(final.exists())
            self.assertTrue(private.is_dir())

    def test_run_recovery_preflights_policy_and_validates_before_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            server, runtime, policy_path = self._prepare_recovery_inputs(root)

            next_pid = 900_000

            class FakeProcess:
                def __init__(self, command: list[str]):
                    nonlocal next_pid
                    next_pid += 1
                    self.pid = next_pid
                    self.returncode = 0
                    self.test_name = command[-1].rsplit("/", 1)[-1]

                def communicate(self, timeout: int | None = None) -> tuple[bytes, bytes]:
                    del timeout
                    return f"{self.test_name} passed\n".encode(), b""

            with mock.patch.object(
                stability.subprocess,
                "Popen",
                side_effect=lambda command, **_: FakeProcess(command),
            ) as popen:
                native_path, record_path, passed = stability.run_recovery(
                    server=server,
                    output_directory=root / "recovery-output",
                    runtime_lib_dir=runtime,
                    policy_path=policy_path,
                    timeout_seconds=30,
                )
            self.assertTrue(passed)
            self.assertEqual(popen.call_count, len(stability.RECOVERY_SUBCHECKS))
            self.assertTrue(
                all(
                    call.kwargs["env"]["PATH"] == os.defpath
                    for call in popen.call_args_list
                )
            )
            private_scratch = root / "recovery-output/swift-scratch"
            private_runner = (
                root
                / "recovery-output/swift-package/scripts/swift-test.sh"
            )
            self.assertTrue(private_scratch.is_dir())
            self.assertTrue(private_runner.is_file())
            self.assertTrue(
                all(
                    call.kwargs["env"]["SWIFT_SCRATCH_PATH"]
                    == str(private_scratch.resolve())
                    and call.args[0][0] == str(private_runner)
                    for call in popen.call_args_list
                )
            )
            self.assertTrue(native_path.is_file())
            self.assertTrue(record_path.is_file())
            native = json.loads(native_path.read_text(encoding="utf-8"))
            self.assertTrue(
                all(
                    item["cleanup"]
                    == {"process_group": True, "session": True, "residue_count": 0}
                    for item in native["subchecks"]
                )
            )

    def test_run_recovery_cleans_group_and_session_when_communicate_raises(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            server, runtime, policy_path = self._prepare_recovery_inputs(root)

            process = mock.Mock(pid=910_000)
            process.communicate.side_effect = RuntimeError("communicate failed")
            with (
                mock.patch.object(stability.subprocess, "Popen", return_value=process),
                mock.patch.object(stability, "_observe_session_members"),
                mock.patch.object(stability, "_stop_process_group", return_value=[]) as group,
                mock.patch.object(stability, "_stop_session", return_value=[]) as session,
                self.assertRaisesRegex(RuntimeError, "communicate failed"),
            ):
                stability.run_recovery(
                    server=server,
                    output_directory=root / "recovery-output",
                    runtime_lib_dir=runtime,
                    policy_path=policy_path,
                    timeout_seconds=30,
                )
            group.assert_called_with(process.pid)
            session.assert_called_with(process.pid)

    def test_run_recovery_cleans_session_when_observer_start_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            server, runtime, policy_path = self._prepare_recovery_inputs(root)

            process = mock.Mock(pid=940_000)
            with (
                mock.patch.object(stability.subprocess, "Popen", return_value=process),
                mock.patch.object(
                    stability.threading.Thread,
                    "start",
                    side_effect=RuntimeError("observer start failed"),
                ),
                mock.patch.object(
                    stability, "_stop_process_group", return_value=[]
                ) as group,
                mock.patch.object(stability, "_stop_session", return_value=[]) as session,
                self.assertRaisesRegex(RuntimeError, "observer start failed"),
            ):
                stability.run_recovery(
                    server=server,
                    output_directory=root / "recovery-output",
                    runtime_lib_dir=runtime,
                    policy_path=policy_path,
                    timeout_seconds=30,
                )
            group.assert_called_once_with(process.pid)
            session.assert_called_once_with(process.pid)

    def test_process_audit_cleans_group_and_session_on_general_runner_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            process = mock.Mock(pid=920_000, returncode=1)
            process.communicate.side_effect = RuntimeError("communicate failed")
            with (
                mock.patch.object(stability.subprocess, "Popen", return_value=process),
                mock.patch.object(
                    stability, "_process_identity", return_value=(process.pid, 42)
                ),
                mock.patch.object(stability, "_observe_process_session"),
                mock.patch.object(
                    stability, "_stop_process_group", return_value=[]
                ) as group,
                mock.patch.object(stability, "_stop_session", return_value=[]) as session,
                self.assertRaisesRegex(RuntimeError, "communicate failed"),
            ):
                stability._run_with_process_audit(
                    command=["runner"],
                    cwd=root,
                    environment={},
                    server_path=root / "server",
                    server_identity=(1, "sha256:" + "1" * 64),
                    helper_identity=(1, "sha256:" + "2" * 64),
                    timeout_seconds=1,
                    runner_is_server=False,
                )
            group.assert_called_once_with(process.pid)
            session.assert_called_once_with(process.pid)

    def test_process_audit_cleans_session_when_observer_start_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            process = mock.Mock(pid=930_000, returncode=1)
            with (
                mock.patch.object(stability.subprocess, "Popen", return_value=process),
                mock.patch.object(
                    stability, "_process_identity", return_value=(process.pid, 43)
                ),
                mock.patch.object(
                    stability.threading.Thread,
                    "start",
                    side_effect=RuntimeError("observer start failed"),
                ),
                mock.patch.object(
                    stability, "_stop_process_group", return_value=[]
                ) as group,
                mock.patch.object(stability, "_stop_session", return_value=[]) as session,
                self.assertRaisesRegex(RuntimeError, "observer start failed"),
            ):
                stability._run_with_process_audit(
                    command=["runner"],
                    cwd=root,
                    environment={},
                    server_path=root / "server",
                    server_identity=(1, "sha256:" + "1" * 64),
                    helper_identity=(1, "sha256:" + "2" * 64),
                    timeout_seconds=1,
                    runner_is_server=False,
                )
            group.assert_called_once_with(process.pid)
            session.assert_called_once_with(process.pid)

    def test_pending_fcitx_producer_cannot_be_hidden_by_readiness_flags(self) -> None:
        policy = json.loads(POLICY.read_text(encoding="utf-8"))
        fcitx = next(
            item
            for item in policy["gates"]["long_running_stability"]["checks"]
            if item["id"] == stability.FCITX_LONG_SOAK_ID
        )
        fcitx["native_producer"]["status"] = "pending"
        fcitx["native_producer"]["sha256"] = None
        policy["readiness"] = {
            "formal_decision_enabled": True,
            "blocking_items": [],
        }
        with self.assertRaisesRegex(ValueError, "formal decision is not ready"):
            gate.parse_policy(json.dumps(policy).encode())

    def test_policy_rejects_forged_swift_test_runner_hash(self) -> None:
        policy = json.loads(POLICY.read_text(encoding="utf-8"))
        steady = next(
            item
            for item in policy["gates"]["long_running_stability"]["checks"]
            if item["id"] == stability.PROTOCOL_STEADY_ID
        )
        steady["execution_runner"]["sha256"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(ValueError, "execution_runner.sha256"):
            gate.parse_policy(json.dumps(policy).encode())


if __name__ == "__main__":
    unittest.main()
