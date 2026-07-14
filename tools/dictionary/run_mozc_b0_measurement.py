#!/usr/bin/env python3
"""Acquire the frozen eight-run Mozc B0 ABProbe comparison."""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterable

if __package__:
    from . import summarize_ab_probe
    from .evaluate_conversion_quality import load_corpus_bytes
else:
    import summarize_ab_probe  # type: ignore[no-redef]
    from evaluate_conversion_quality import load_corpus_bytes


SCHEMA = "hazkey.mozc-b0-acquisition-manifest.v1"
RUNTIME_DEPENDENCY_SCHEMA = "hazkey.mozc-b0-runtime-dependencies.v1"
SEQUENCE = (
    ("H1", "hazkey"),
    ("M1", "mozc"),
    ("M2", "mozc"),
    ("H2", "hazkey"),
    ("H3", "hazkey"),
    ("M3", "mozc"),
    ("M4", "mozc"),
    ("H4", "hazkey"),
)
WARMUPS = 5
ITERATIONS = 20
TOP_K = 10
CASES = 256
LATENCY_STATISTIC = "nearest-rank-p95-across-all-samples"
PSS_STATISTIC = "max-parent-plus-backend-before-after"
CPU_POLICY = "unrestricted-same-host"
PER_RUN_TIMEOUT_SECONDS = 900
TERMINATION_GRACE_SECONDS = 5
MANIFEST_NAME = "acquisition-manifest.json"
SNAPSHOT_ROOT_NAME = "runtime"
SNAPSHOT_EXECUTABLE_NAME = "hazkey-server"
SNAPSHOT_LIBRARY_DIRECTORY_NAME = "lib"
SNAPSHOT_EXECUTABLE_ARG = f"./{SNAPSHOT_ROOT_NAME}/{SNAPSHOT_EXECUTABLE_NAME}"
SNAPSHOT_LIBRARY_ARGUMENT = (
    f"./{SNAPSHOT_ROOT_NAME}/{SNAPSHOT_LIBRARY_DIRECTORY_NAME}"
)
RUNTIME_DEPENDENCY_FILENAMES = (
    "libggml-base.so",
    "libggml-cpu-alderlake.so",
    "libggml-cpu-haswell.so",
    "libggml-cpu-icelake.so",
    "libggml-cpu-sandybridge.so",
    "libggml-cpu-sapphirerapids.so",
    "libggml-cpu-skylakex.so",
    "libggml-vulkan.so",
    "libggml.so",
    "libllama.so",
    "vulkan-shaders-gen",
)
CHILD_ENVIRONMENT = {
    "GGML_BACKEND_DIR": SNAPSHOT_LIBRARY_ARGUMENT,
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "LD_LIBRARY_PATH": SNAPSHOT_LIBRARY_ARGUMENT,
    "PATH": os.defpath,
    "TZ": "UTC",
}
AT_FDCWD = -100
RENAME_NOREPLACE = 1


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _read_regular(path: Path, context: str) -> bytes:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"{context} must be a non-symlink regular file")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise ValueError(f"{context} changed before it was read")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        final = os.fstat(descriptor)
        final_path = path.lstat()
    finally:
        os.close(descriptor)
    if (
        opened.st_size != final.st_size
        or opened.st_mtime_ns != final.st_mtime_ns
        or sum(map(len, chunks)) != opened.st_size
        or (final_path.st_dev, final_path.st_ino) != (final.st_dev, final.st_ino)
        or final_path.st_size != final.st_size
        or final_path.st_mtime_ns != final.st_mtime_ns
        or not stat.S_ISREG(final_path.st_mode)
    ):
        raise ValueError(f"{context} changed while it was read")
    return b"".join(chunks)


def _host_contract() -> dict[str, Any]:
    if not hasattr(os, "sched_getaffinity"):
        raise ValueError("effective CPU affinity is unavailable on this host")
    affinity = sorted(os.sched_getaffinity(0))
    if not affinity:
        raise ValueError("effective CPU affinity must not be empty")
    uname = os.uname()
    boot_path = Path("/proc/sys/kernel/random/boot_id")
    metadata = boot_path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError("kernel boot ID must be a non-symlink regular file")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(boot_path, flags)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise ValueError("kernel boot ID changed before it was read")
        boot_bytes = os.read(descriptor, 128)
        if os.read(descriptor, 1):
            raise ValueError("kernel boot ID is unexpectedly long")
    finally:
        os.close(descriptor)
    boot_id = boot_bytes.decode("ascii").strip()
    if re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        boot_id,
    ) is None:
        raise ValueError("kernel boot ID has an unexpected format")
    identity = {
        "system": uname.sysname,
        "node": uname.nodename,
        "release": uname.release,
        "machine": uname.machine,
        "boot_id": boot_id,
    }
    return {
        "fingerprint": sha256_bytes(canonical_json(identity)),
        "effective_cpu_affinity": affinity,
    }


def _command(
    executable: Path | str,
    corpus: Path,
    source_ref: str,
    backend: str,
    hazkey_dictionary: Path,
    mozc_bundle: Path,
) -> list[str]:
    command = [
        str(executable),
        "--ab-probe",
        "--corpus",
        str(corpus),
        "--source-ref",
        source_ref,
        "--warmups",
        str(WARMUPS),
        "--iterations",
        str(ITERATIONS),
        "--top-k",
        str(TOP_K),
        "--converter-backend",
        backend,
    ]
    if backend == "hazkey":
        command.extend(("--dictionary", str(hazkey_dictionary)))
    else:
        command.extend(("--mozc-bundle", str(mozc_bundle)))
    return command


def _child_environment() -> tuple[dict[str, str], dict[str, Any]]:
    values = dict(CHILD_ENVIRONMENT)
    return values, {
        "policy": "private-runtime-snapshot-v1",
        "cwd": "acquisition-root",
        "ambient_inheritance": False,
        "values": values,
    }


def _runtime_dependency_contract(
    runtime_library_directory: Path,
) -> tuple[dict[str, bytes], dict[str, Any]]:
    metadata = runtime_library_directory.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("runtime-lib-dir must be a non-symlink directory")
    actual_names = {entry.name for entry in runtime_library_directory.iterdir()}
    expected_names = set(RUNTIME_DEPENDENCY_FILENAMES)
    if actual_names != expected_names:
        raise ValueError(
            "runtime-lib-dir does not contain the exact formal B0 dependency set; "
            f"missing={sorted(expected_names - actual_names)!r}, "
            f"unknown={sorted(actual_names - expected_names)!r}"
        )
    contents: dict[str, bytes] = {}
    files: list[dict[str, Any]] = []
    for name in RUNTIME_DEPENDENCY_FILENAMES:
        data = _read_regular(
            runtime_library_directory / name,
            f"runtime dependency {name}",
        )
        if not data:
            raise ValueError(f"runtime dependency {name} must not be empty")
        contents[name] = data
        files.append(
            {
                "path": name,
                "size_bytes": len(data),
                "sha256": sha256_bytes(data),
            }
        )
    contract_base = {"schema": RUNTIME_DEPENDENCY_SCHEMA, "files": files}
    contract = contract_base | {"integrity": sha256_bytes(canonical_json(contract_base))}
    return contents, contract


def _create_runtime_snapshot(
    temporary: Path,
    executable_bytes: bytes,
    runtime_dependencies: dict[str, bytes],
) -> tuple[Path, Path]:
    snapshot_root = temporary / SNAPSHOT_ROOT_NAME
    snapshot_root.mkdir(mode=0o700)
    executable_snapshot = snapshot_root / SNAPSHOT_EXECUTABLE_NAME
    _write_private(executable_snapshot, executable_bytes, mode=0o555)
    library_snapshot = snapshot_root / SNAPSHOT_LIBRARY_DIRECTORY_NAME
    library_snapshot.mkdir(mode=0o700)
    for name in RUNTIME_DEPENDENCY_FILENAMES:
        path = library_snapshot / name
        _write_private(path, runtime_dependencies[name], mode=0o555)
    library_snapshot.chmod(0o555)
    snapshot_root.chmod(0o555)
    return executable_snapshot, library_snapshot


def _verify_runtime_snapshot(
    executable_snapshot: Path,
    executable_bytes: bytes,
    library_snapshot: Path,
    runtime_dependencies: dict[str, bytes],
) -> None:
    if stat.S_IMODE(executable_snapshot.lstat().st_mode) != 0o555:
        raise ValueError("private executable snapshot mode changed during acquisition")
    if _read_regular(executable_snapshot, "private executable snapshot") != executable_bytes:
        raise ValueError("private executable snapshot changed during acquisition")
    metadata = library_snapshot.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o555
    ):
        raise ValueError("private runtime dependency directory changed during acquisition")
    if {entry.name for entry in library_snapshot.iterdir()} != set(
        RUNTIME_DEPENDENCY_FILENAMES
    ):
        raise ValueError("private runtime dependency set changed during acquisition")
    for name in RUNTIME_DEPENDENCY_FILENAMES:
        path = library_snapshot / name
        if stat.S_IMODE(path.lstat().st_mode) != 0o555:
            raise ValueError(f"private runtime dependency {name} mode changed")
        if _read_regular(path, f"private runtime dependency {name}") != runtime_dependencies[name]:
            raise ValueError(f"private runtime dependency {name} changed")


def _make_snapshot_removable(temporary: Path) -> None:
    snapshot_root = temporary / SNAPSHOT_ROOT_NAME
    library_snapshot = snapshot_root / SNAPSHOT_LIBRARY_DIRECTORY_NAME
    if library_snapshot.exists() and not library_snapshot.is_symlink():
        library_snapshot.chmod(0o700)
    if snapshot_root.exists() and not snapshot_root.is_symlink():
        snapshot_root.chmod(0o700)


def _rename_noreplace(source: Path, destination: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOSYS, "Linux renameat2 is required for formal acquisition")
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        AT_FDCWD,
        os.fsencode(source),
        AT_FDCWD,
        os.fsencode(destination),
        RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise ValueError(f"refusing to overwrite output {destination}")
    raise OSError(error_number, os.strerror(error_number), destination)


def _write_private(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=TERMINATION_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def _run_probe(
    argv: list[str],
    raw_handle: Any,
    stderr_handle: Any,
    run_id: str,
    environment: dict[str, str],
    cwd: Path,
) -> int:
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=raw_handle,
        stderr=stderr_handle,
        shell=False,
        start_new_session=True,
        env=environment,
        cwd=cwd,
    )
    try:
        return process.wait(timeout=PER_RUN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as error:
        _terminate_process_group(process)
        raise ValueError(
            f"run {run_id} exceeded {PER_RUN_TIMEOUT_SECONDS} seconds"
        ) from error
    except BaseException:
        _terminate_process_group(process)
        raise


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validate_probe(
    data: bytes,
    path: Path,
    *,
    backend: str,
    source_ref: str,
    corpus_sha256: str,
) -> None:
    run = summarize_ab_probe.load_run_bytes(data, str(path))
    expectations = {
        "schema": summarize_ab_probe.INPUT_SCHEMA_V3,
        "converter_backend": backend,
        "source_ref": source_ref,
        "warmups": WARMUPS,
        "iterations": ITERATIONS,
        "top_k": TOP_K,
        "corpus": {"sha256": corpus_sha256, "cases": CASES},
    }
    for field, expected in expectations.items():
        if run[field] != expected:
            raise ValueError(
                f"{path}: {field} does not match acquisition contract; "
                f"expected {expected!r}, got {run[field]!r}"
            )
    if len(run["cases"]) != CASES:
        raise ValueError(f"{path}: must contain exactly {CASES} cases")


def acquire(
    *,
    executable: Path,
    runtime_library_directory: Path,
    corpus: Path,
    source_ref: str,
    hazkey_dictionary: Path,
    mozc_bundle: Path,
    output_directory: Path,
) -> dict[str, Any]:
    if re.fullmatch(r"[0-9a-f]{40}", source_ref) is None:
        raise ValueError("source_ref must be a 40-hex product commit")
    executable = executable.resolve(strict=True)
    executable_metadata = executable.stat()
    if not stat.S_ISREG(executable_metadata.st_mode) or not os.access(
        executable, os.X_OK
    ):
        raise ValueError("executable must be an executable regular file")
    executable_bytes = _read_regular(executable, "executable")
    if not executable_bytes:
        raise ValueError("executable must not be empty")
    executable_sha256 = sha256_bytes(executable_bytes)
    if not runtime_library_directory.is_absolute():
        raise ValueError("runtime-lib-dir must be an absolute path")
    runtime_library_directory = runtime_library_directory.resolve(strict=True)
    runtime_dependency_bytes, runtime_dependency_contract = (
        _runtime_dependency_contract(runtime_library_directory)
    )
    corpus = corpus.resolve(strict=True)
    hazkey_dictionary = hazkey_dictionary.resolve(strict=True)
    mozc_bundle = mozc_bundle.resolve(strict=True)
    if not hazkey_dictionary.is_dir() or not mozc_bundle.is_dir():
        raise ValueError("dictionary and Mozc bundle must be directories")
    corpus_bytes = _read_regular(corpus, "corpus")
    corpus_rows = load_corpus_bytes(corpus_bytes, str(corpus))
    if len(corpus_rows) != CASES:
        raise ValueError(f"corpus must contain exactly {CASES} cases")
    corpus_sha256 = sha256_bytes(corpus_bytes)
    producer_path = Path(__file__).resolve()
    producer_sha256 = sha256_bytes(_read_regular(producer_path, "producer"))
    host = _host_contract()
    child_environment, environment_contract = _child_environment()

    parent = output_directory.parent
    if not parent.is_dir():
        raise ValueError(f"output parent does not exist: {parent}")
    if output_directory.exists() or output_directory.is_symlink():
        raise ValueError(f"refusing to overwrite output {output_directory}")
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{output_directory.name}.tmp-", dir=parent)
    )
    temporary.chmod(0o700)
    lock_path = parent / f".{output_directory.name}.lock"
    lock_descriptor = -1
    lock_created = False
    entries: list[dict[str, Any]] = []
    try:
        executable_snapshot, library_snapshot = _create_runtime_snapshot(
            temporary,
            executable_bytes,
            runtime_dependency_bytes,
        )
        _verify_runtime_snapshot(
            executable_snapshot,
            executable_bytes,
            library_snapshot,
            runtime_dependency_bytes,
        )
        _fsync_directory(library_snapshot)
        _fsync_directory(executable_snapshot.parent)
        previous_end = 0
        for sequence, (run_id, backend) in enumerate(SEQUENCE, 1):
            if sorted(os.sched_getaffinity(0)) != host["effective_cpu_affinity"]:
                raise ValueError("orchestrator CPU affinity changed during acquisition")
            raw_path = temporary / f"{run_id}.jsonl"
            stderr_path = temporary / f"{run_id}.stderr"
            raw_descriptor = os.open(
                raw_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            stderr_descriptor = os.open(
                stderr_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            argv = _command(
                SNAPSHOT_EXECUTABLE_ARG,
                corpus,
                source_ref,
                backend,
                hazkey_dictionary,
                mozc_bundle,
            )
            started = time.monotonic_ns()
            try:
                with os.fdopen(raw_descriptor, "wb") as raw_handle, os.fdopen(
                    stderr_descriptor, "wb"
                ) as stderr_handle:
                    raw_descriptor = -1
                    stderr_descriptor = -1
                    return_code = _run_probe(
                        argv,
                        raw_handle,
                        stderr_handle,
                        run_id,
                        child_environment,
                        temporary,
                    )
                    raw_handle.flush()
                    stderr_handle.flush()
                    os.fsync(raw_handle.fileno())
                    os.fsync(stderr_handle.fileno())
            finally:
                if raw_descriptor >= 0:
                    os.close(raw_descriptor)
                if stderr_descriptor >= 0:
                    os.close(stderr_descriptor)
            ended = time.monotonic_ns()
            if started < previous_end or ended < started:
                raise ValueError("non-monotonic or overlapping run timestamps")
            previous_end = ended
            if return_code != 0:
                raise ValueError(f"run {run_id} exited with {return_code}")
            raw_bytes = _read_regular(raw_path, f"run {run_id} raw output")
            stderr_bytes = _read_regular(stderr_path, f"run {run_id} stderr")
            _validate_probe(
                raw_bytes,
                raw_path,
                backend=backend,
                source_ref=source_ref,
                corpus_sha256=corpus_sha256,
            )
            entries.append(
                {
                    "sequence": sequence,
                    "id": run_id,
                    "backend": backend,
                    "argv": argv,
                    "raw": {"path": raw_path.name, "sha256": sha256_bytes(raw_bytes)},
                    "stderr": {
                        "path": stderr_path.name,
                        "sha256": sha256_bytes(stderr_bytes),
                    },
                    "exit_code": return_code,
                    "started_monotonic_ns": started,
                    "ended_monotonic_ns": ended,
                    "host_fingerprint": host["fingerprint"],
                    "effective_cpu_affinity": host["effective_cpu_affinity"],
                }
            )

        _verify_runtime_snapshot(
            executable_snapshot,
            executable_bytes,
            library_snapshot,
            runtime_dependency_bytes,
        )
        runtime_manifest = {
            "schema": runtime_dependency_contract["schema"],
            "source_path": str(runtime_library_directory),
            "snapshot_path": f"{SNAPSHOT_ROOT_NAME}/{SNAPSHOT_LIBRARY_DIRECTORY_NAME}",
            "files": runtime_dependency_contract["files"],
            "integrity": runtime_dependency_contract["integrity"],
        }
        manifest_base = {
            "schema": SCHEMA,
            "producer": {
                "path": "tools/dictionary/run_mozc_b0_measurement.py",
                "sha256": producer_sha256,
            },
            "executable": {
                "source_path": str(executable),
                "snapshot_path": f"{SNAPSHOT_ROOT_NAME}/{SNAPSHOT_EXECUTABLE_NAME}",
                "size_bytes": len(executable_bytes),
                "sha256": executable_sha256,
            },
            "runtime_dependencies": runtime_manifest,
            "environment": environment_contract,
            "product_source_ref": source_ref,
            "corpus": {"path": str(corpus), "sha256": corpus_sha256, "cases": CASES},
            "host": host,
            "measurement": {
                "runs_per_backend": 4,
                "execution_order": [run_id for run_id, _ in SEQUENCE],
                "warmups_per_case": WARMUPS,
                "iterations_per_case": ITERATIONS,
                "top_k": TOP_K,
                "latency_statistic": LATENCY_STATISTIC,
                "pss_statistic": PSS_STATISTIC,
                "cpu_policy": CPU_POLICY,
                "per_run_timeout_seconds": PER_RUN_TIMEOUT_SECONDS,
            },
            "entries": entries,
        }
        manifest = manifest_base | {
            "integrity": sha256_bytes(canonical_json(manifest_base))
        }
        _write_private(
            temporary / MANIFEST_NAME,
            (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode(
                "utf-8"
            ),
        )
        _fsync_directory(temporary)
        lock_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            lock_flags |= os.O_NOFOLLOW
        lock_descriptor = os.open(lock_path, lock_flags, 0o600)
        lock_created = True
        os.close(lock_descriptor)
        lock_descriptor = -1
        if output_directory.exists() or output_directory.is_symlink():
            raise ValueError(f"refusing to overwrite output {output_directory}")
        _rename_noreplace(temporary, output_directory)
        temporary = Path()
        _fsync_directory(parent)
        return manifest
    finally:
        if lock_descriptor >= 0:
            os.close(lock_descriptor)
        if lock_created:
            lock_path.unlink(missing_ok=True)
        if temporary != Path() and temporary.exists():
            _make_snapshot_removable(temporary)
            shutil.rmtree(temporary)
        _fsync_directory(parent)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--runtime-lib-dir", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--hazkey-dictionary", type=Path, required=True)
    parser.add_argument("--mozc-bundle", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        manifest = acquire(
            executable=args.executable,
            runtime_library_directory=args.runtime_lib_dir,
            corpus=args.corpus,
            source_ref=args.source_ref,
            hazkey_dictionary=args.hazkey_dictionary,
            mozc_bundle=args.mozc_bundle,
            output_directory=args.output_dir,
        )
        print(f"{manifest['integrity']} {args.output_dir}")
        return 0
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
