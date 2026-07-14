#!/usr/bin/env python3
"""Measure an argv-based backend command as a fresh Linux process per run.

Wall time uses ``time.monotonic_ns``.  Peak RSS comes from the ``rusage``
returned by ``os.wait4`` for the exact PID that was launched for that run;
``RUSAGE_CHILDREN`` is deliberately not used because it is cumulative.

Backend stdout and stderr are discarded by default.  A file supplied for an
output stream is truncated once, then receives output from every run.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Any, BinaryIO, Iterator, Mapping, Sequence


OUTPUT_SCHEMA = "hazkey.process-backend-benchmark.v1"


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _fingerprint(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value)).hexdigest()


def _nearest_rank_p95(samples: Sequence[float]) -> float:
    if not samples:
        raise ValueError("at least one sample is required")
    ordered = sorted(samples)
    index = min(len(ordered) - 1, math.ceil(len(ordered) * 0.95) - 1)
    return ordered[index]


def _monotonic_ns() -> int:
    return time.monotonic_ns()


def _wait4(pid: int, options: int = 0) -> tuple[int, int, Any]:
    # wait4(pid), unlike getrusage(RUSAGE_CHILDREN), returns this child's usage.
    return os.wait4(pid, options)


def _validate_timeout(timeout_seconds: int | float | None) -> float | None:
    if timeout_seconds is None:
        return None
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(float(timeout_seconds))
        or timeout_seconds <= 0
    ):
        raise ValueError("timeout_seconds must be a finite positive number or null")
    return float(timeout_seconds)


def _validate_command(argv: Sequence[str]) -> list[str]:
    command = list(argv)
    if not command:
        raise ValueError("backend command argv is required")
    for index, argument in enumerate(command):
        if not isinstance(argument, str):
            raise ValueError(f"argv[{index}] must be a string")
        if "\0" in argument:
            raise ValueError(f"argv[{index}] must not contain NUL")
    if not command[0]:
        raise ValueError("argv[0] must be non-empty")
    return command


def _validate_environment(overrides: Mapping[str, str] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, value in (overrides or {}).items():
        if not isinstance(name, str) or not name or "=" in name or "\0" in name:
            raise ValueError(f"invalid environment variable name {name!r}")
        if not isinstance(value, str) or "\0" in value:
            raise ValueError(
                f"environment value for {name!r} must be a NUL-free string"
            )
        result[name] = value
    return result


def parse_environment_overrides(items: Sequence[str]) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for item in items:
        name, separator, value = item.partition("=")
        if not separator:
            raise ValueError(f"--env value must be NAME=VALUE, got {item!r}")
        if name in overrides:
            raise ValueError(f"duplicate --env variable {name!r}")
        overrides[name] = value
    return _validate_environment(overrides)


def _resolve_cwd(cwd: Path | str | None) -> Path:
    resolved = Path.cwd().resolve() if cwd is None else Path(cwd).resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError(f"cwd is not a directory: {resolved}")
    return resolved


def _resolve_output_path(path: Path | str | None) -> Path | None:
    if path is None:
        return None
    return Path(path).resolve()


def _resolve_executable(
    executable: str,
    cwd: Path,
    environment: Mapping[str, str],
) -> str | None:
    if os.sep in executable:
        candidate = Path(executable)
        if not candidate.is_absolute():
            candidate = cwd / candidate
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
        return None

    for directory in environment.get("PATH", os.defpath).split(os.pathsep):
        base = Path(directory or ".")
        if not base.is_absolute():
            base = cwd / base
        candidate = base / executable
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
    return None


def _output_provenance(path: Path | None) -> dict[str, str]:
    if path is None:
        return {"mode": "discard"}
    return {"mode": "file", "path": str(path)}


@contextmanager
def _open_backend_outputs(
    stdout_path: Path | None,
    stderr_path: Path | None,
) -> Iterator[tuple[int | BinaryIO, int | BinaryIO]]:
    with ExitStack() as stack:
        if stdout_path is not None and stdout_path == stderr_path:
            combined = stack.enter_context(stdout_path.open("wb"))
            yield combined, combined
            return

        stdout: int | BinaryIO = subprocess.DEVNULL
        stderr: int | BinaryIO = subprocess.DEVNULL
        if stdout_path is not None:
            stdout = stack.enter_context(stdout_path.open("wb"))
        if stderr_path is not None:
            stderr = stack.enter_context(stderr_path.open("wb"))
        yield stdout, stderr


def _reap_after_interruption(process: subprocess.Popen[bytes]) -> None:
    if process.returncode is not None:
        return
    try:
        process.kill()
    except ProcessLookupError:
        pass
    try:
        waited_pid, status, _ = os.wait4(process.pid, 0)
        if waited_pid == process.pid:
            process.returncode = os.waitstatus_to_exitcode(status)
    except ChildProcessError:
        process.poll()


def _kill_and_reap(process: subprocess.Popen[bytes]) -> int:
    try:
        process.kill()
    except ProcessLookupError:
        pass
    try:
        waited_pid, status, _ = _wait4(process.pid)
    except ChildProcessError as error:
        returncode = process.poll()
        if returncode is None:
            raise RuntimeError("timed-out child could not be reaped") from error
        return returncode
    if waited_pid != process.pid:
        raise RuntimeError(
            f"wait4 returned PID {waited_pid}, expected launched PID {process.pid}"
        )
    returncode = os.waitstatus_to_exitcode(status)
    process.returncode = returncode
    return returncode


def _wait_for_process(
    process: subprocess.Popen[bytes],
    *,
    started_ns: int,
    timeout_seconds: float | None,
) -> tuple[int, int, Any, int]:
    if timeout_seconds is None:
        waited_pid, status, usage = _wait4(process.pid)
        return waited_pid, status, usage, _monotonic_ns()

    deadline_ns = started_ns + math.ceil(timeout_seconds * 1_000_000_000)
    while True:
        waited_pid, status, usage = _wait4(process.pid, os.WNOHANG)
        observed_ns = _monotonic_ns()
        if waited_pid == process.pid:
            return waited_pid, status, usage, observed_ns
        if waited_pid != 0:
            raise RuntimeError(
                f"wait4 returned PID {waited_pid}, expected 0 or {process.pid}"
            )
        if observed_ns >= deadline_ns:
            returncode = _kill_and_reap(process)
            raise TimeoutError(
                f"process timed out after {timeout_seconds:g} seconds; "
                f"killed and reaped with exit code {returncode}"
            )
        remaining_seconds = (deadline_ns - observed_ns) / 1_000_000_000
        time.sleep(min(0.01, remaining_seconds))


def _run_once(
    argv: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    stdout: int | BinaryIO,
    stderr: int | BinaryIO,
    timeout_seconds: float | None,
) -> dict[str, int | float]:
    started_ns = _monotonic_ns()
    process = subprocess.Popen(
        list(argv),
        cwd=cwd,
        env=dict(environment),
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
        shell=False,
        close_fds=True,
    )
    try:
        waited_pid, status, usage, ended_ns = _wait_for_process(
            process,
            started_ns=started_ns,
            timeout_seconds=timeout_seconds,
        )
    except BaseException:
        _reap_after_interruption(process)
        raise

    exit_code = os.waitstatus_to_exitcode(status)
    process.returncode = exit_code
    if waited_pid != process.pid:
        raise RuntimeError(
            f"wait4 returned PID {waited_pid}, expected launched PID {process.pid}"
        )
    maximum_rss_kib = usage.ru_maxrss
    if (
        isinstance(maximum_rss_kib, bool)
        or not isinstance(maximum_rss_kib, (int, float))
        or not math.isfinite(float(maximum_rss_kib))
        or maximum_rss_kib < 0
    ):
        raise RuntimeError(f"wait4 returned invalid ru_maxrss {maximum_rss_kib!r}")
    if ended_ns < started_ns:
        raise RuntimeError("monotonic clock moved backwards")

    return {
        "started_monotonic_ns": started_ns,
        "ended_monotonic_ns": ended_ns,
        "wall_time_ms": (ended_ns - started_ns) / 1_000_000,
        "maximum_rss_kib": int(maximum_rss_kib),
        "exit_code": exit_code,
    }


def benchmark_process_backend(
    argv: Sequence[str],
    *,
    runs: int,
    backend_name: str = "process-backend",
    cwd: Path | str | None = None,
    environment_overrides: Mapping[str, str] | None = None,
    stdout_path: Path | str | None = None,
    stderr_path: Path | str | None = None,
    expected_exit_code: int = 0,
    timeout_seconds: int | float | None = None,
) -> dict[str, Any]:
    """Run ``argv`` repeatedly and return raw per-child and aggregate metrics."""

    if sys.platform != "linux" or not hasattr(os, "wait4"):
        raise RuntimeError("process backend benchmarking requires Linux os.wait4")
    if isinstance(runs, bool) or not isinstance(runs, int) or runs <= 0:
        raise ValueError("runs must be a positive integer")
    if not isinstance(backend_name, str) or not backend_name:
        raise ValueError("backend_name must be a non-empty string")
    if (
        isinstance(expected_exit_code, bool)
        or not isinstance(expected_exit_code, int)
        or not 0 <= expected_exit_code <= 255
    ):
        raise ValueError("expected_exit_code must be an integer from 0 through 255")
    normalized_timeout = _validate_timeout(timeout_seconds)

    command = _validate_command(argv)
    resolved_cwd = _resolve_cwd(cwd)
    overrides = _validate_environment(environment_overrides)
    environment = os.environ.copy()
    environment.update(overrides)
    resolved_stdout = _resolve_output_path(stdout_path)
    resolved_stderr = _resolve_output_path(stderr_path)
    command_fingerprint = _fingerprint({"argv": command})
    effective_environment_fingerprint = _fingerprint(environment)
    execution_specification = {
        "argv": command,
        "cwd": str(resolved_cwd),
        "effective_environment_fingerprint": effective_environment_fingerprint,
        "expected_exit_code": expected_exit_code,
        "stdout": _output_provenance(resolved_stdout),
        "stderr": _output_provenance(resolved_stderr),
    }
    # Preserve fingerprints from pre-timeout unlimited runs. A finite timeout
    # is execution-relevant and therefore extends the fingerprint material.
    if normalized_timeout is not None:
        execution_specification["timeout_seconds"] = normalized_timeout
    execution_fingerprint = _fingerprint(execution_specification)

    raw_runs: list[dict[str, int | float]] = []
    with _open_backend_outputs(resolved_stdout, resolved_stderr) as outputs:
        for run_number in range(1, runs + 1):
            measured = _run_once(
                command,
                cwd=resolved_cwd,
                environment=environment,
                stdout=outputs[0],
                stderr=outputs[1],
                timeout_seconds=normalized_timeout,
            )
            if measured["exit_code"] != expected_exit_code:
                raise RuntimeError(
                    f"run {run_number} exited with {measured['exit_code']}; "
                    f"expected {expected_exit_code}"
                )
            raw_runs.append({"run": run_number, **measured})

    wall_times = [float(run["wall_time_ms"]) for run in raw_runs]
    maximum_rss_values = [int(run["maximum_rss_kib"]) for run in raw_runs]
    return {
        "schema": OUTPUT_SCHEMA,
        "backend": backend_name,
        "command_fingerprint": command_fingerprint,
        "execution_fingerprint": execution_fingerprint,
        "provenance": {
            "argv": command,
            "resolved_executable": _resolve_executable(
                command[0], resolved_cwd, environment
            ),
            "cwd": str(resolved_cwd),
            "environment": {
                "mode": "inherit-with-overrides",
                "override_keys": sorted(overrides),
                "effective_fingerprint": effective_environment_fingerprint,
            },
            "stdin": {"mode": "discard"},
            "stdout": _output_provenance(resolved_stdout),
            "stderr": _output_provenance(resolved_stderr),
            "clock": "time.monotonic_ns",
            "rss_source": (
                "os.wait4(pid, 0).rusage.ru_maxrss"
                if normalized_timeout is None
                else "os.wait4(pid, os.WNOHANG).rusage.ru_maxrss"
            ),
            "rss_unit": "KiB",
            "platform": sys.platform,
            "timeout_seconds": normalized_timeout,
        },
        "expected_exit_code": expected_exit_code,
        "timeout_seconds": normalized_timeout,
        "run_count": runs,
        "raw_runs": raw_runs,
        "wall_time_ms": {
            "mean": statistics.fmean(wall_times),
            "median": float(statistics.median(wall_times)),
            "p95": _nearest_rank_p95(wall_times),
            "minimum": min(wall_times),
            "maximum": max(wall_times),
        },
        "maximum_rss_kib": max(maximum_rss_values),
    }


def atomic_write_text(path: Path, content: str) -> None:
    """Replace ``path`` atomically after fully writing and syncing ``content``."""

    resolved = path.resolve()
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{resolved.name}.", suffix=".tmp", dir=resolved.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, resolved)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure a backend command as a fresh Linux process per run."
    )
    parser.add_argument("--runs", required=True, type=int)
    parser.add_argument("--backend-name", default="process-backend")
    parser.add_argument("--cwd", type=Path)
    parser.add_argument(
        "--env",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="override one inherited environment variable; repeatable",
    )
    parser.add_argument(
        "--stdout", type=Path, help="backend stdout file (default: discard)"
    )
    parser.add_argument(
        "--stderr", type=Path, help="backend stderr file (default: discard)"
    )
    parser.add_argument("--expected-exit-code", type=int, default=0)
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        help="kill and reap a run after this many seconds (default: unlimited)",
    )
    parser.add_argument("--output", type=Path, help="summary JSON (default: stdout)")
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="backend argv, conventionally following --",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    try:
        overrides = parse_environment_overrides(args.env)
        if args.output is not None:
            output_path = args.output.resolve()
            for stream_name, stream_path in (
                ("stdout", args.stdout),
                ("stderr", args.stderr),
            ):
                if stream_path is not None and stream_path.resolve() == output_path:
                    raise ValueError(
                        f"--output must differ from backend --{stream_name}"
                    )
        result = benchmark_process_backend(
            command,
            runs=args.runs,
            backend_name=args.backend_name,
            cwd=args.cwd,
            environment_overrides=overrides,
            stdout_path=args.stdout,
            stderr_path=args.stderr,
            expected_exit_code=args.expected_exit_code,
            timeout_seconds=args.timeout_seconds,
        )
        encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        if args.output is None:
            sys.stdout.write(encoded)
        else:
            atomic_write_text(args.output, encoded)
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
