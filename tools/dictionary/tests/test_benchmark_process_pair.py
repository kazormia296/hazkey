from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT))

from tools.dictionary import benchmark_process_pair  # noqa: E402


SCRIPT = REPOSITORY_ROOT / "tools/dictionary/benchmark_process_pair.py"


def backend_spec(
    name: str,
    argv: list[str],
    *,
    cwd: str = ".",
    environment: dict[str, str] | None = None,
    expected_exit_code: int = 0,
    timeout_seconds: float | None = None,
) -> dict[str, object]:
    return {
        "backend_name": name,
        "argv": argv,
        "cwd": cwd,
        "environment_overrides": environment or {},
        "expected_exit_code": expected_exit_code,
        "timeout_seconds": timeout_seconds,
    }


def write_manifest(
    path: Path,
    a: dict[str, object],
    b: dict[str, object],
) -> None:
    path.write_text(
        json.dumps(
            {
                "schema": "hazkey.process-backend-pair-manifest.v1",
                "a": a,
                "b": b,
            }
        ),
        encoding="utf-8",
    )


def fake_result(
    label: str,
    wall_time_ms: float,
    maximum_rss_kib: int,
    sequence: int,
) -> dict[str, object]:
    fingerprint_character = "a" if label == "a" else "b"
    return {
        "schema": "hazkey.process-backend-benchmark.v1",
        "backend": f"backend-{label}",
        "command_fingerprint": "sha256:" + fingerprint_character * 64,
        "execution_fingerprint": "sha256:" + fingerprint_character.upper() * 64,
        "provenance": {"argv": [label], "rss_unit": "KiB"},
        "expected_exit_code": 0,
        "timeout_seconds": None,
        "run_count": 1,
        "raw_runs": [
            {
                "run": 1,
                "started_monotonic_ns": sequence * 1_000_000,
                "ended_monotonic_ns": int(
                    (sequence * 1_000_000) + wall_time_ms * 1_000_000
                ),
                "wall_time_ms": wall_time_ms,
                "maximum_rss_kib": maximum_rss_kib,
                "exit_code": 0,
            }
        ],
    }


@unittest.skipUnless(sys.platform == "linux", "pair runner requires Linux wait4")
class ProcessBackendPairTests(unittest.TestCase):
    def test_alternates_ab_ba_and_summarizes_paired_measurements(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            manifest = directory / "pair.json"
            write_manifest(
                manifest,
                backend_spec("backend-a", ["a"]),
                backend_spec("backend-b", ["b"]),
            )
            wall_times = {"a": iter([10.0, 30.0, 50.0]), "b": iter([20.0, 40.0, 25.0])}
            rss_values = {"a": iter([100, 300, 200]), "b": iter([400, 250, 350])}
            call_order: list[str] = []

            def measure(argv, **kwargs):
                label = argv[0]
                call_order.append(label)
                return fake_result(
                    label,
                    next(wall_times[label]),
                    next(rss_values[label]),
                    len(call_order),
                )

            with mock.patch.object(
                benchmark_process_pair.process_backend,
                "benchmark_process_backend",
                side_effect=measure,
            ):
                result = benchmark_process_pair.benchmark_process_pair(
                    manifest, cycles=3
                )

        self.assertEqual(call_order, ["a", "b", "b", "a", "a", "b"])
        self.assertEqual(
            [item["backend"] for item in result["raw_execution_order"]],
            call_order,
        )
        self.assertEqual(
            [item["cycle"] for item in result["raw_execution_order"]],
            [1, 1, 2, 2, 3, 3],
        )
        self.assertEqual(result["backends"]["a"]["wall_time_ms"]["mean"], 30.0)
        self.assertEqual(result["backends"]["a"]["wall_time_ms"]["median"], 30.0)
        self.assertEqual(result["backends"]["a"]["wall_time_ms"]["p95"], 50.0)
        self.assertEqual(result["backends"]["a"]["maximum_rss_kib"], 300)
        self.assertEqual(
            result["backends"]["b"]["wall_time_ms"]["mean"], 85 / 3
        )
        self.assertEqual(result["backends"]["b"]["wall_time_ms"]["median"], 25.0)
        self.assertEqual(result["backends"]["b"]["wall_time_ms"]["p95"], 40.0)
        self.assertEqual(result["backends"]["b"]["maximum_rss_kib"], 400)
        paired = result["paired_wall_ratio"]
        self.assertEqual(
            [sample["a_over_b"] for sample in paired["samples"]],
            [0.5, 0.75, 2.0],
        )
        self.assertEqual(paired["mean"], 3.25 / 3)
        self.assertEqual(paired["median"], 0.75)
        self.assertEqual(paired["p95"], 2.0)
        self.assertEqual(paired["ratio_of_means"], 18 / 17)
        self.assertRegex(
            result["manifest"]["fingerprint"], r"^sha256:[0-9a-f]{64}$"
        )
        self.assertEqual(
            result["backends"]["a"]["command_fingerprint"],
            "sha256:" + "a" * 64,
        )

    def test_cli_runs_real_children_with_cwd_env_and_expected_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            manifest = directory / "pair.json"
            output = directory / "result.json"
            a_code = (
                "import os; assert os.getcwd() == os.environ['EXPECTED_CWD']; "
                "assert os.environ['SIDE'] == 'a'; print('discarded-a')"
            )
            b_code = (
                "import os,sys; assert os.getcwd() == os.environ['EXPECTED_CWD']; "
                "assert os.environ['SIDE'] == 'b'; "
                "print('discarded-b', file=sys.stderr); "
                "raise SystemExit(7)"
            )
            write_manifest(
                manifest,
                backend_spec(
                    "real-a",
                    [sys.executable, "-c", a_code],
                    environment={"SIDE": "a", "EXPECTED_CWD": str(directory)},
                ),
                backend_spec(
                    "real-b",
                    [sys.executable, "-c", b_code],
                    environment={"SIDE": "b", "EXPECTED_CWD": str(directory)},
                    expected_exit_code=7,
                ),
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--manifest",
                    str(manifest),
                    "--cycles",
                    "2",
                    "--output",
                    str(output),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            result = json.loads(output.read_text(encoding="utf-8"))
            temporary_files = list(directory.glob(".result.json.*.tmp"))

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(completed.stderr, "")
        self.assertEqual(result["schema"], "hazkey.process-backend-pair-benchmark.v1")
        self.assertEqual(result["cycles"], 2)
        self.assertEqual(
            [item["backend"] for item in result["raw_execution_order"]],
            ["a", "b", "b", "a"],
        )
        self.assertEqual(
            [item["exit_code"] for item in result["raw_execution_order"]],
            [0, 7, 7, 0],
        )
        self.assertEqual(
            result["backends"]["a"]["process"]["stdout"], {"mode": "discard"}
        )
        self.assertEqual(
            result["backends"]["b"]["process"]["stderr"], {"mode": "discard"}
        )
        self.assertEqual(temporary_files, [])

    def test_unexpected_child_exit_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            manifest = directory / "pair.json"
            output = directory / "must-not-exist.json"
            write_manifest(
                manifest,
                backend_spec("success", [sys.executable, "-c", "pass"]),
                backend_spec(
                    "failure",
                    [sys.executable, "-c", "raise SystemExit(9)"],
                ),
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--manifest",
                    str(manifest),
                    "--cycles",
                    "2",
                    "--output",
                    str(output),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 2)
            self.assertIn("run 1 exited with 9; expected 0", completed.stderr)
            self.assertNotIn("Traceback", completed.stderr)
            self.assertFalse(output.exists())

    def test_backend_timeout_kills_child_and_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            manifest = directory / "pair.json"
            output = directory / "must-not-exist.json"
            write_manifest(
                manifest,
                backend_spec("success", [sys.executable, "-c", "pass"]),
                backend_spec(
                    "hang",
                    [sys.executable, "-c", "import time; time.sleep(10)"],
                    timeout_seconds=0.1,
                ),
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--manifest",
                    str(manifest),
                    "--cycles",
                    "2",
                    "--output",
                    str(output),
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=3,
            )

            self.assertEqual(completed.returncode, 2)
            self.assertIn("timed out after 0.1 seconds", completed.stderr)
            self.assertIn("killed and reaped", completed.stderr)
            self.assertNotIn("Traceback", completed.stderr)
            self.assertFalse(output.exists())

    def test_manifest_rejects_duplicate_keys_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            valid_backend = backend_spec("fixture", [sys.executable, "-c", "pass"])
            invalid_manifests = {
                "wrong-schema": {
                    "schema": "wrong",
                    "a": valid_backend,
                    "b": valid_backend,
                },
                "unexpected-field": {
                    "schema": "hazkey.process-backend-pair-manifest.v1",
                    "a": {**valid_backend, "typo": True},
                    "b": valid_backend,
                },
                "boolean-exit": {
                    "schema": "hazkey.process-backend-pair-manifest.v1",
                    "a": {**valid_backend, "expected_exit_code": True},
                    "b": valid_backend,
                },
                "invalid-timeout": {
                    "schema": "hazkey.process-backend-pair-manifest.v1",
                    "a": {**valid_backend, "timeout_seconds": -1},
                    "b": valid_backend,
                },
                "boolean-timeout": {
                    "schema": "hazkey.process-backend-pair-manifest.v1",
                    "a": {**valid_backend, "timeout_seconds": True},
                    "b": valid_backend,
                },
                "missing-timeout": {
                    "schema": "hazkey.process-backend-pair-manifest.v1",
                    "a": {
                        key: value
                        for key, value in valid_backend.items()
                        if key != "timeout_seconds"
                    },
                    "b": valid_backend,
                },
            }
            for name, payload in invalid_manifests.items():
                with self.subTest(name=name):
                    path = directory / f"{name}.json"
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    with mock.patch.object(
                        benchmark_process_pair.process_backend,
                        "benchmark_process_backend",
                    ) as runner:
                        with self.assertRaises(ValueError):
                            benchmark_process_pair.benchmark_process_pair(
                                path, cycles=1
                            )
                        runner.assert_not_called()

            duplicate = directory / "duplicate.json"
            duplicate.write_text(
                '{"schema":"hazkey.process-backend-pair-manifest.v1",'
                '"a":{},"a":{},"b":{}}',
                encoding="utf-8",
            )
            with mock.patch.object(
                benchmark_process_pair.process_backend,
                "benchmark_process_backend",
            ) as runner:
                with self.assertRaisesRegex(ValueError, "duplicate JSON key 'a'"):
                    benchmark_process_pair.benchmark_process_pair(duplicate, cycles=1)
                runner.assert_not_called()

    def test_provenance_drift_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            manifest = directory / "pair.json"
            write_manifest(
                manifest,
                backend_spec("backend-a", ["a"]),
                backend_spec("backend-b", ["b"]),
            )
            calls = {"a": 0, "b": 0}

            def changing_measurement(argv, **kwargs):
                label = argv[0]
                calls[label] += 1
                result = fake_result(label, 1.0, 10, calls[label])
                if label == "a" and calls[label] == 2:
                    result["execution_fingerprint"] = "sha256:" + "c" * 64
                return result

            with mock.patch.object(
                benchmark_process_pair.process_backend,
                "benchmark_process_backend",
                side_effect=changing_measurement,
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "backend a provenance changed"
                ):
                    benchmark_process_pair.benchmark_process_pair(
                        manifest, cycles=2
                    )


if __name__ == "__main__":
    unittest.main()
