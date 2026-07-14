from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT))

from tools.dictionary import benchmark_process_backend  # noqa: E402


SCRIPT = REPOSITORY_ROOT / "tools/dictionary/benchmark_process_backend.py"


@unittest.skipUnless(sys.platform == "linux", "benchmark runner requires Linux wait4")
class ProcessBackendBenchmarkTests(unittest.TestCase):
    def test_success_uses_per_child_rss_and_computes_aggregates(self) -> None:
        real_wait4 = benchmark_process_backend._wait4
        per_child_rss = iter([101, 202, 50])

        def controlled_wait4(pid: int):
            waited_pid, status, _ = real_wait4(pid)
            return waited_pid, status, SimpleNamespace(ru_maxrss=next(per_child_rss))

        clock_values = [
            1_000_000_000,
            1_001_000_000,
            2_000_000_000,
            2_002_000_000,
            3_000_000_000,
            3_010_000_000,
        ]
        with (
            mock.patch.object(
                benchmark_process_backend,
                "_wait4",
                side_effect=controlled_wait4,
            ),
            mock.patch.object(
                benchmark_process_backend,
                "_monotonic_ns",
                side_effect=clock_values,
            ),
        ):
            result = benchmark_process_backend.benchmark_process_backend(
                [sys.executable, "-c", "pass"],
                runs=3,
                backend_name="fixture",
            )

        self.assertEqual(result["schema"], "hazkey.process-backend-benchmark.v1")
        self.assertEqual(result["backend"], "fixture")
        self.assertEqual(result["run_count"], 3)
        self.assertEqual(
            [run["wall_time_ms"] for run in result["raw_runs"]],
            [1.0, 2.0, 10.0],
        )
        self.assertEqual(
            [run["maximum_rss_kib"] for run in result["raw_runs"]],
            [101, 202, 50],
        )
        self.assertEqual(result["wall_time_ms"]["mean"], 13 / 3)
        self.assertEqual(result["wall_time_ms"]["median"], 2.0)
        self.assertEqual(result["wall_time_ms"]["p95"], 10.0)
        self.assertEqual(result["wall_time_ms"]["minimum"], 1.0)
        self.assertEqual(result["wall_time_ms"]["maximum"], 10.0)
        # The peak is a max over independent wait4 samples, never a cumulative sum.
        self.assertEqual(result["maximum_rss_kib"], 202)
        self.assertEqual(
            result["provenance"]["rss_source"],
            "os.wait4(pid, 0).rusage.ru_maxrss",
        )
        self.assertEqual(result["provenance"]["rss_unit"], "KiB")

    def test_cwd_environment_and_output_destinations_apply_to_every_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            stdout_path = directory / "stdout.log"
            stderr_path = directory / "stderr.log"
            code = (
                "import os,sys; "
                "print(os.getcwd() + '|' + os.environ['AB_SPIKE_VALUE']); "
                "print('error-' + os.environ['AB_SPIKE_VALUE'], file=sys.stderr)"
            )
            result = benchmark_process_backend.benchmark_process_backend(
                [sys.executable, "-c", code],
                runs=2,
                cwd=directory,
                environment_overrides={"AB_SPIKE_VALUE": "from-override"},
                stdout_path=stdout_path,
                stderr_path=stderr_path,
            )

            expected_stdout = f"{directory.resolve()}|from-override\n" * 2
            self.assertEqual(stdout_path.read_text(encoding="utf-8"), expected_stdout)
            self.assertEqual(
                stderr_path.read_text(encoding="utf-8"),
                "error-from-override\n" * 2,
            )

        self.assertEqual(result["provenance"]["cwd"], str(directory.resolve()))
        self.assertEqual(
            result["provenance"]["environment"]["override_keys"],
            ["AB_SPIKE_VALUE"],
        )
        self.assertNotIn("from-override", json.dumps(result))
        self.assertEqual(
            result["provenance"]["stdout"],
            {"mode": "file", "path": str(stdout_path.resolve())},
        )
        self.assertEqual(
            result["provenance"]["stderr"],
            {"mode": "file", "path": str(stderr_path.resolve())},
        )

    def test_expected_nonzero_exit_is_allowed(self) -> None:
        result = benchmark_process_backend.benchmark_process_backend(
            [sys.executable, "-c", "raise SystemExit(7)"],
            runs=1,
            expected_exit_code=7,
        )

        self.assertEqual(result["expected_exit_code"], 7)
        self.assertEqual(result["raw_runs"][0]["exit_code"], 7)

    def test_timeout_kills_reaps_and_fails_closed_without_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_path = directory / "must-not-exist.json"
            pid_path = directory / "child.pid"
            child_code = (
                "import os,time; from pathlib import Path; "
                f"Path({str(pid_path)!r}).write_text(str(os.getpid())); "
                "time.sleep(10)"
            )
            started = time.monotonic()
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--runs",
                    "1",
                    "--timeout-seconds",
                    "0.2",
                    "--output",
                    str(output_path),
                    "--",
                    sys.executable,
                    "-c",
                    child_code,
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=3,
            )
            elapsed = time.monotonic() - started
            child_pid = int(pid_path.read_text(encoding="utf-8"))

            self.assertEqual(completed.returncode, 2)
            self.assertIn("timed out after 0.2 seconds", completed.stderr)
            self.assertIn("killed and reaped", completed.stderr)
            self.assertNotIn("Traceback", completed.stderr)
            self.assertLess(elapsed, 2)
            self.assertFalse(output_path.exists())
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)

    def test_timeout_validation_and_success_provenance(self) -> None:
        for timeout_seconds in (False, 0, -1, float("nan"), float("inf")):
            with self.subTest(timeout_seconds=timeout_seconds):
                with self.assertRaisesRegex(ValueError, "finite positive number"):
                    benchmark_process_backend.benchmark_process_backend(
                        [sys.executable, "-c", "pass"],
                        runs=1,
                        timeout_seconds=timeout_seconds,
                    )

        result = benchmark_process_backend.benchmark_process_backend(
            [sys.executable, "-c", "pass"],
            runs=1,
            timeout_seconds=2,
        )
        self.assertEqual(result["timeout_seconds"], 2.0)
        self.assertEqual(result["provenance"]["timeout_seconds"], 2.0)
        self.assertEqual(
            result["provenance"]["rss_source"],
            "os.wait4(pid, os.WNOHANG).rusage.ru_maxrss",
        )

    def test_unexpected_exit_fails_closed_without_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "must-not-exist.json"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--runs",
                    "2",
                    "--output",
                    str(output_path),
                    "--",
                    sys.executable,
                    "-c",
                    "raise SystemExit(9)",
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 2)
            self.assertIn("run 1 exited with 9; expected 0", completed.stderr)
            self.assertNotIn("Traceback", completed.stderr)
            self.assertEqual(completed.stdout, "")
            self.assertFalse(output_path.exists())

    def test_cli_writes_schema_raw_runs_and_provenance_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            output_path = directory / "result.json"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--runs",
                    "2",
                    "--backend-name",
                    "cli-fixture",
                    "--cwd",
                    str(directory),
                    "--env",
                    "FIXTURE=present",
                    "--output",
                    str(output_path),
                    "--",
                    sys.executable,
                    "-c",
                    "import os; assert os.environ['FIXTURE'] == 'present'",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            payload = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(payload["backend"], "cli-fixture")
        self.assertEqual(len(payload["raw_runs"]), 2)
        self.assertRegex(payload["command_fingerprint"], r"^sha256:[0-9a-f]{64}$")
        self.assertRegex(payload["execution_fingerprint"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(payload["provenance"]["stdin"], {"mode": "discard"})
        self.assertEqual(payload["provenance"]["stdout"], {"mode": "discard"})
        self.assertEqual(payload["provenance"]["stderr"], {"mode": "discard"})

    def test_duplicate_environment_override_and_output_collision_fails(self) -> None:
        scenarios = (
            ["--env", "DUP=one", "--env", "DUP=two"],
            ["--stdout", "result.json"],
            ["--stderr", "result.json"],
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            for extra_arguments in scenarios:
                with self.subTest(arguments=extra_arguments):
                    output_path = directory / "result.json"
                    output_path.unlink(missing_ok=True)
                    completed = subprocess.run(
                        [
                            sys.executable,
                            str(SCRIPT),
                            "--runs",
                            "1",
                            "--cwd",
                            str(directory),
                            "--output",
                            str(output_path),
                            *extra_arguments,
                            "--",
                            sys.executable,
                            "-c",
                            "pass",
                        ],
                        cwd=directory,
                        check=False,
                        capture_output=True,
                        text=True,
                    )

                    self.assertEqual(completed.returncode, 2)
                    self.assertIn("error:", completed.stderr)
                    self.assertNotIn("Traceback", completed.stderr)
                    self.assertFalse(output_path.exists())


if __name__ == "__main__":
    unittest.main()
