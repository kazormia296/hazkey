#!/usr/bin/env python3
"""Run the built addon and server inside Fcitx's display-free test frontend."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time


MAXIMUM_RESTART_CYCLES = 1_000
MAXIMUM_SOAK_ITERATIONS = 1_000_000
MAXIMUM_TIMEOUT_SECONDS = 86_400
MAXIMUM_LAUNCH_AUDIT_BYTES = 65_536
MAXIMUM_PRODUCT_SERVER_BYTES = 1 << 40
RESULT_SCHEMA = "hazkey.fcitx-full-stack-result.v1"
RESULT_VERSION = 1
SNAPSHOT_SCHEMA = "hazkey.fcitx-full-stack-input-snapshot.v1"
SNAPSHOT_FINGERPRINT_DOMAIN = "hazkey.fcitx-input-snapshot.v1"
MOZC_RUNTIME_FINGERPRINT_DOMAIN = "hazkey.mozc-runtime-fingerprint.v1"
MOZC_RUNTIME_FILES = (
    "fcitx5-grimodex-mozc-helper",
    "manifest.json",
    "mozc.data",
)
MOZC_BUNDLE_LICENSE_FILES = (
    "ABSEIL-LICENSE",
    "DICTIONARY-OSS-NOTICE.txt",
    "FCITX-MOZKEY-THIRD-PARTY-NOTICES.md",
    "JAPANESE-USAGE-DICTIONARY-LICENSE",
    "MOZC-LICENSE",
    "PROTOBUF-LICENSE",
    "UTF8-RANGE-LICENSE",
)
ACTIVE_TEST_SESSIONS: set[int] = set()
ACTIVE_PROCESS_AUDITS: dict[Path, Path] = {}


def bounded_positive_integer(value: str, maximum: int) -> int:
    if re.fullmatch(r"[1-9][0-9]*", value) is None:
        raise argparse.ArgumentTypeError("must be a positive decimal integer")
    parsed = int(value)
    if parsed > maximum:
        raise argparse.ArgumentTypeError(f"must not exceed {maximum}")
    return parsed


def nonnegative_integer(value: str) -> int:
    if re.fullmatch(r"0|[1-9][0-9]*", value) is None:
        raise argparse.ArgumentTypeError(
            "must be a nonnegative decimal integer"
        )
    parsed = int(value)
    if parsed > MAXIMUM_SOAK_ITERATIONS:
        raise argparse.ArgumentTypeError(
            f"must not exceed {MAXIMUM_SOAK_ITERATIONS}"
        )
    return parsed


def restart_cycles(value: str) -> int:
    return bounded_positive_integer(value, MAXIMUM_RESTART_CYCLES)


def timeout_seconds(value: str) -> int:
    return bounded_positive_integer(value, MAXIMUM_TIMEOUT_SECONDS)


def product_source_ref(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise argparse.ArgumentTypeError("must be a lowercase 40-hex commit")
    return value


def lowercase_sha256(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise argparse.ArgumentTypeError("must be a lowercase 64-hex SHA-256")
    return value


def product_server_size(value: str) -> int:
    return bounded_positive_integer(value, MAXIMUM_PRODUCT_SERVER_BYTES)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--harness", type=Path, required=True)
    parser.add_argument("--addon", type=Path, required=True)
    parser.add_argument("--server", type=Path, required=True)
    parser.add_argument("--dictionary", type=Path, required=True)
    parser.add_argument("--addon-config", type=Path, required=True)
    parser.add_argument("--input-method-config", type=Path, required=True)
    parser.add_argument("--system-test-addon-dir", type=Path, required=True)
    parser.add_argument("--llama-lib-dir", type=Path, required=True)
    parser.add_argument(
        "--converter-backend",
        choices=("hazkey", "mozc"),
        default="hazkey",
    )
    parser.add_argument("--mozc-verifier", type=Path)
    parser.add_argument("--mozc-generation", type=Path)
    parser.add_argument("--cycles", type=restart_cycles, default=1)
    parser.add_argument(
        "--soak-iterations",
        type=nonnegative_integer,
        default=0,
    )
    parser.add_argument("--timeout", type=timeout_seconds, default=45)
    parser.add_argument(
        "--result-output",
        type=Path,
        help=(
            "publish native structured evidence after successful final cleanup; "
            "the destination must not already exist"
        ),
    )
    parser.add_argument("--product-source-ref", type=product_source_ref)
    parser.add_argument("--product-server-sha256", type=lowercase_sha256)
    parser.add_argument("--product-server-size", type=product_server_size)
    args = parser.parse_args()

    mozc_arguments = (args.mozc_verifier, args.mozc_generation)
    if args.converter_backend == "hazkey" and any(mozc_arguments):
        parser.error("Mozc arguments require --converter-backend=mozc")
    if args.converter_backend == "mozc" and not all(mozc_arguments):
        parser.error(
            "--converter-backend=mozc requires --mozc-verifier and "
            "--mozc-generation"
        )
    if args.result_output is not None:
        required_evidence_arguments = {
            "--product-source-ref": args.product_source_ref,
            "--product-server-sha256": args.product_server_sha256,
            "--product-server-size": args.product_server_size,
        }
        missing = [
            name for name, value in required_evidence_arguments.items()
            if value is None
        ]
        if missing:
            parser.error(
                "--result-output requires " + ", ".join(missing)
            )
    return args


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    executable: str
    process_group: int
    session_id: int
    start_time: str


@dataclass(frozen=True)
class ProcessLaunch:
    pid: int
    start_time: str


@dataclass(frozen=True)
class CycleObservation:
    cycle: int
    conversions: int
    server_launches: tuple[ProcessLaunch, ...]
    server_identities: tuple[ProcessIdentity, ...]
    helper_launches: tuple[ProcessLaunch, ...]
    helper_identities: tuple[ProcessIdentity, ...]
    lock_owner_observed: bool
    max_concurrent_helpers: int
    server_cleanup_ok: bool
    helper_cleanup_ok: bool
    process_group_cleanup_ok: bool


@dataclass(frozen=True)
class SnapshotEntry:
    input_id: str
    source_path: str
    relative_path: str
    size: int
    sha256: str
    mode: int


@dataclass(frozen=True)
class InputSnapshot:
    root: Path
    entries: tuple[SnapshotEntry, ...]
    directories: tuple[str, ...]
    fingerprint: str


@dataclass
class ResultOutput:
    parent_path: Path
    filename: str
    parent_fd: int

    def close(self) -> None:
        if self.parent_fd >= 0:
            os.close(self.parent_fd)
            self.parent_fd = -1


class ProcessInspectionError(RuntimeError):
    pass


class ResultEvidenceError(RuntimeError):
    pass


def process_owner(pid: int) -> int | None:
    try:
        return os.stat(f"/proc/{pid}").st_uid
    except FileNotFoundError:
        return None
    except OSError as error:
        if error.errno in (errno.ENOENT, errno.ESRCH):
            return None
        raise ProcessInspectionError(
            f"cannot inspect ownership of process {pid}: {error}"
        ) from error


def process_inspection_failed(pid: int, error: OSError) -> None:
    if error.errno in (errno.ENOENT, errno.ESRCH):
        return
    owner = process_owner(pid)
    if owner is None or owner != os.getuid():
        return
    raise ProcessInspectionError(
        f"cannot inspect process {pid}: {error}"
    ) from error


def read_process_identity(
    pid: int,
    expected_process_group: int | None = None,
    expected_session: int | None = None,
) -> ProcessIdentity | None:
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as error:
        process_inspection_failed(pid, error)
        return None
    closing_parenthesis = stat_text.rfind(")")
    if closing_parenthesis < 0:
        return None
    fields_after_name = stat_text[closing_parenthesis + 2 :].split()
    # /proc/<pid>/stat field 22 is process start time. The split suffix starts
    # at field 3 because field 2 is the parenthesized executable name.
    if len(fields_after_name) <= 19:
        return None
    process_group = int(fields_after_name[2])
    session_id = int(fields_after_name[3])
    if (
        expected_process_group is not None
        and process_group != expected_process_group
    ):
        return None
    if expected_session is not None and session_id != expected_session:
        return None
    try:
        executable = os.readlink(f"/proc/{pid}/exe")
    except FileNotFoundError:
        return None
    except OSError as error:
        process_inspection_failed(pid, error)
        return None
    return ProcessIdentity(
        pid=pid,
        executable=executable,
        process_group=process_group,
        session_id=session_id,
        start_time=fields_after_name[19],
    )


def process_still_matches(identity: ProcessIdentity) -> bool:
    return read_process_identity(identity.pid) == identity


def exact_process_identities(
    executable: Path,
    process_group: int | None = None,
) -> set[ProcessIdentity]:
    expected = str(executable.resolve())
    identities: set[ProcessIdentity] = set()
    try:
        entries = list(Path("/proc").iterdir())
    except OSError as error:
        raise ProcessInspectionError(f"cannot enumerate /proc: {error}") from error
    for entry in entries:
        if not entry.name.isdigit():
            continue
        identity = read_process_identity(
            int(entry.name),
            expected_process_group=process_group,
        )
        if (
            identity is not None
            and identity.executable == expected
            and (
                process_group is None
                or identity.process_group == process_group
            )
        ):
            identities.add(identity)
    return identities


def session_process_identities(session_leader: int) -> set[ProcessIdentity]:
    identities: set[ProcessIdentity] = set()
    try:
        entries = list(Path("/proc").iterdir())
    except OSError as error:
        raise ProcessInspectionError(f"cannot enumerate /proc: {error}") from error
    for entry in entries:
        if not entry.name.isdigit():
            continue
        identity = read_process_identity(
            int(entry.name),
            expected_session=session_leader,
        )
        if (
            identity is not None
            and identity.session_id == session_leader
        ):
            identities.add(identity)
    return identities


def create_launch_wrapper(
    wrapper: Path,
    target: Path,
    audit: Path,
    expected_binding: dict[str, object] | None = None,
) -> None:
    target = target.resolve(strict=True)
    audit = audit.absolute()
    expected_size: int | None = None
    expected_sha256: str | None = None
    if expected_binding is not None:
        expected_size = expected_binding.get("size")  # type: ignore[assignment]
        expected_sha256 = expected_binding.get("sha256")  # type: ignore[assignment]
        if (
            not isinstance(expected_size, int)
            or expected_size <= 0
            or not isinstance(expected_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
        ):
            raise ResultEvidenceError("launch binding requires exact size/SHA-256")
    descriptor = os.open(
        audit,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | os.O_CLOEXEC
        | os.O_NOFOLLOW,
        0o600,
    )
    os.close(descriptor)
    wrapper.write_text(
        f"""#!{sys.executable}
import hashlib
import os
import stat
import sys

TARGET = {str(target)!r}
AUDIT = {str(audit)!r}
EXPECTED_SIZE = {expected_size!r}
EXPECTED_SHA256 = {expected_sha256!r}

target_descriptor = -1
if EXPECTED_SIZE is not None:
    if os.execve not in os.supports_fd:
        raise RuntimeError("fd-based exec is unavailable")
    target_descriptor = os.open(
        TARGET,
        os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
    )
    target_before = os.fstat(target_descriptor)
    digest = hashlib.sha256()
    while True:
        chunk = os.read(target_descriptor, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    target_after = os.fstat(target_descriptor)
    if (
        not stat.S_ISREG(target_before.st_mode)
        or target_before.st_dev != target_after.st_dev
        or target_before.st_ino != target_after.st_ino
        or target_before.st_size != target_after.st_size
        or target_before.st_mtime_ns != target_after.st_mtime_ns
        or target_before.st_ctime_ns != target_after.st_ctime_ns
        or target_after.st_size != EXPECTED_SIZE
        or digest.hexdigest() != EXPECTED_SHA256
    ):
        raise RuntimeError("launch target does not match its exact binding")

with open("/proc/self/stat", "rb", buffering=0) as handle:
    process_stat = handle.read()
closing_parenthesis = process_stat.rfind(b")")
fields_after_name = process_stat[closing_parenthesis + 2:].split()
if closing_parenthesis < 0 or len(fields_after_name) <= 19:
    raise RuntimeError("cannot read launch process start time")
record = str(os.getpid()).encode("ascii") + b" " + fields_after_name[19] + b"\\n"

descriptor = os.open(
    AUDIT,
    os.O_WRONLY | os.O_APPEND | os.O_CLOEXEC | os.O_NOFOLLOW,
)
try:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
    ):
        raise RuntimeError("launch audit has unsafe metadata")
    if os.write(descriptor, record) != len(record):
        raise RuntimeError("launch audit write was incomplete")
    os.fsync(descriptor)
finally:
    os.close(descriptor)

if target_descriptor >= 0:
    os.execve(target_descriptor, [TARGET, *sys.argv[1:]], dict(os.environ))
os.execv(TARGET, [TARGET, *sys.argv[1:]])
""",
        encoding="utf-8",
    )
    wrapper.chmod(0o700)


def load_process_launches(audit: Path) -> list[ProcessLaunch]:
    try:
        descriptor = os.open(
            audit,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
    except OSError as error:
        raise ProcessInspectionError(
            f"cannot open private launch audit {audit}: {error}"
        ) from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
        ):
            raise ProcessInspectionError(
                f"private launch audit has unsafe metadata: {audit}"
            )
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(
                descriptor,
                min(4096, MAXIMUM_LAUNCH_AUDIT_BYTES + 1 - size),
            )
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAXIMUM_LAUNCH_AUDIT_BYTES:
                raise ProcessInspectionError(
                    f"private launch audit is too large: {audit}"
                )
        path_metadata = os.stat(audit, follow_symlinks=False)
        if (
            path_metadata.st_dev != metadata.st_dev
            or path_metadata.st_ino != metadata.st_ino
        ):
            raise ProcessInspectionError(
                f"private launch audit changed during inspection: {audit}"
            )
    except OSError as error:
        raise ProcessInspectionError(
            f"cannot read private launch audit {audit}: {error}"
        ) from error
    finally:
        os.close(descriptor)

    raw = b"".join(chunks)
    if not raw:
        return []
    if not raw.endswith(b"\n"):
        raise ProcessInspectionError(
            f"private launch audit has an incomplete record: {audit}"
        )
    launches: list[ProcessLaunch] = []
    for line in raw.splitlines():
        if re.fullmatch(rb"[1-9][0-9]* [1-9][0-9]*", line) is None:
            raise ProcessInspectionError(
                f"private launch audit has an invalid record: {audit}"
            )
        pid, start_time = line.decode("ascii").split(" ")
        launches.append(ProcessLaunch(int(pid), start_time))
    return launches


def identity_for_launch(
    launch: ProcessLaunch,
    expected_executable: Path | None = None,
) -> ProcessIdentity | None:
    identity = read_process_identity(launch.pid)
    if identity is None or identity.start_time != launch.start_time:
        return None
    if (
        expected_executable is not None
        and identity.executable != str(expected_executable.resolve())
    ):
        return None
    return identity


def identities_for_launches(
    launches: list[ProcessLaunch],
    expected_executable: Path | None = None,
) -> set[ProcessIdentity]:
    identities: set[ProcessIdentity] = set()
    for launch in launches:
        identity = identity_for_launch(launch, expected_executable)
        if identity is not None:
            identities.add(identity)
    return identities


def identity_from_lock(
    lock_file: Path,
    expected_executable: Path,
    launches: list[ProcessLaunch],
) -> ProcessIdentity | None:
    if not lock_file.exists():
        return None
    try:
        pid = int(lock_file.read_text(encoding="utf-8").splitlines()[0])
    except FileNotFoundError:
        return None
    except (ValueError, IndexError):
        return None
    except OSError as error:
        raise ProcessInspectionError(
            f"cannot inspect temporary server lock {lock_file}: {error}"
        ) from error
    identity = read_process_identity(pid)
    if (
        identity is None
        or identity.executable != str(expected_executable.resolve())
        or not any(
            launch.pid == identity.pid
            and launch.start_time == identity.start_time
            for launch in launches
        )
    ):
        return None
    return identity


def signal_exact_process(
    identity: ProcessIdentity,
    value: signal.Signals,
) -> bool:
    try:
        descriptor = os.pidfd_open(identity.pid)
    except ProcessLookupError:
        return True
    except OSError as error:
        raise ProcessInspectionError(
            f"cannot open pidfd for process {identity.pid}: {error}"
        ) from error
    try:
        if not process_still_matches(identity):
            return True
        try:
            signal.pidfd_send_signal(descriptor, value)
        except ProcessLookupError:
            return True
        except OSError as error:
            raise ProcessInspectionError(
                f"cannot signal process {identity.pid}: {error}"
            ) from error
    finally:
        os.close(descriptor)
    return True


def wait_for_identities_to_exit(
    identities: set[ProcessIdentity],
    timeout: float,
) -> set[ProcessIdentity]:
    deadline = time.monotonic() + timeout
    remaining = {identity for identity in identities if process_still_matches(identity)}
    while remaining and time.monotonic() < deadline:
        time.sleep(0.025)
        remaining = {
            identity for identity in remaining if process_still_matches(identity)
        }
    return remaining


def stop_exact_processes(identities: set[ProcessIdentity]) -> bool:
    remaining = {identity for identity in identities if process_still_matches(identity)}
    for identity in remaining:
        signal_exact_process(identity, signal.SIGTERM)
    remaining = wait_for_identities_to_exit(remaining, 3.0)
    for identity in remaining:
        signal_exact_process(identity, signal.SIGKILL)
    return not wait_for_identities_to_exit(remaining, 1.0)


def stop_process_launches(launches: list[ProcessLaunch]) -> bool:
    identities = identities_for_launches(launches)
    return stop_exact_processes(identities)


def drain_process_session(session_leader: int) -> bool:
    term_deadline = time.monotonic() + 1.0
    while True:
        identities = session_process_identities(session_leader)
        if not identities:
            return True
        for identity in identities:
            signal_exact_process(identity, signal.SIGTERM)
        if time.monotonic() >= term_deadline:
            break
        time.sleep(0.025)

    kill_deadline = time.monotonic() + 1.0
    while True:
        identities = session_process_identities(session_leader)
        if not identities:
            return True
        for identity in identities:
            signal_exact_process(identity, signal.SIGKILL)
        if time.monotonic() >= kill_deadline:
            return False
        time.sleep(0.025)


def terminate_process_group(process: subprocess.Popen[object]) -> bool:
    drained = drain_process_session(process.pid)
    if process.poll() is None:
        try:
            process.wait(timeout=0.25)
        except subprocess.TimeoutExpired:
            return False
    return drained


def completed_conversions(stderr_file: Path) -> int:
    try:
        stderr = stderr_file.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeError) as error:
        raise ResultEvidenceError(
            f"cannot read native Fcitx harness result {stderr_file}: {error}"
        ) from error
    matches = re.findall(
        r"^grimodex-fcitx-full-stack: same-session conversion soak passed: "
        r"(0|[1-9][0-9]*) iterations$",
        stderr,
        flags=re.MULTILINE,
    )
    if len(matches) != 1:
        raise ResultEvidenceError(
            "native Fcitx harness did not emit exactly one conversion-count "
            "milestone"
        )
    conversions = int(matches[0])
    if conversions > MAXIMUM_SOAK_ITERATIONS:
        raise ResultEvidenceError(
            "native Fcitx harness reported an out-of-range conversion count"
        )
    return conversions


def sha256_regular_file_bytes(path: Path) -> bytes:
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ResultEvidenceError(
            f"cannot resolve evidence input {path}: {error}"
        ) from error
    try:
        descriptor = os.open(
            resolved,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
    except OSError as error:
        raise ResultEvidenceError(
            f"cannot open evidence input {resolved}: {error}"
        ) from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ResultEvidenceError(
                f"evidence input is not a regular file: {resolved}"
            )
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        path_metadata = os.stat(resolved, follow_symlinks=False)
        if (
            path_metadata.st_dev != metadata.st_dev
            or path_metadata.st_ino != metadata.st_ino
            or path_metadata.st_size != metadata.st_size
            or path_metadata.st_mtime_ns != metadata.st_mtime_ns
        ):
            raise ResultEvidenceError(
                f"evidence input changed while it was hashed: {resolved}"
            )
    except OSError as error:
        raise ResultEvidenceError(
            f"cannot hash evidence input {resolved}: {error}"
        ) from error
    finally:
        os.close(descriptor)
    return digest.digest()


def sha256_regular_file(path: Path) -> str:
    return sha256_regular_file_bytes(path).hex()


def file_binding(path: Path) -> dict[str, object]:
    resolved = path.resolve(strict=True)
    metadata = resolved.stat(follow_symlinks=False)
    return {
        "path": str(resolved),
        "size": metadata.st_size,
        "sha256": sha256_regular_file(resolved),
    }


def readonly_runtime_binding(path: Path) -> dict[str, object]:
    binding = file_binding(path)
    binding["mode"] = stat.S_IMODE(
        path.resolve(strict=True).stat(follow_symlinks=False).st_mode
    )
    return binding


def copy_snapshot_file(
    source: Path,
    destination: Path,
    *,
    input_id: str,
    snapshot_root: Path,
) -> SnapshotEntry:
    try:
        source_metadata_before = source.stat(follow_symlinks=False)
        if source.is_symlink() or not stat.S_ISREG(source_metadata_before.st_mode):
            raise ResultEvidenceError(
                f"snapshot input must be a regular non-symlink file: {source}"
            )
        resolved = source.resolve(strict=True)
        source_descriptor = os.open(
            resolved,
            os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
    except ResultEvidenceError:
        raise
    except OSError as error:
        raise ResultEvidenceError(
            f"cannot open snapshot input {source}: {error}"
        ) from error
    destination_descriptor = -1
    try:
        source_metadata = os.fstat(source_descriptor)
        if (
            not stat.S_ISREG(source_metadata.st_mode)
            or source_metadata.st_dev != source_metadata_before.st_dev
            or source_metadata.st_ino != source_metadata_before.st_ino
        ):
            raise ResultEvidenceError(
                f"snapshot input changed before it was opened: {resolved}"
            )
        destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        destination_descriptor = os.open(
            destination,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_CLOEXEC
            | os.O_NOFOLLOW,
            0o600,
        )
        digest = hashlib.sha256()
        copied = 0
        while True:
            chunk = os.read(source_descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            copied += len(chunk)
            remaining = memoryview(chunk)
            while remaining:
                written = os.write(destination_descriptor, remaining)
                if written <= 0:
                    raise ResultEvidenceError(
                        f"snapshot write made no progress: {destination}"
                    )
                remaining = remaining[written:]
        source_after = os.stat(resolved, follow_symlinks=False)
        if (
            source_after.st_dev != source_metadata.st_dev
            or source_after.st_ino != source_metadata.st_ino
            or source_after.st_size != source_metadata.st_size
            or source_after.st_mtime_ns != source_metadata.st_mtime_ns
            or source_after.st_ctime_ns != source_metadata.st_ctime_ns
            or copied != source_metadata.st_size
        ):
            raise ResultEvidenceError(
                f"snapshot input changed while copied: {resolved}"
            )
        mode = 0o555 if source_metadata.st_mode & 0o111 else 0o444
        os.fchmod(destination_descriptor, mode)
        os.fsync(destination_descriptor)
    except OSError as error:
        raise ResultEvidenceError(
            f"cannot snapshot input {resolved}: {error}"
        ) from error
    finally:
        os.close(source_descriptor)
        if destination_descriptor >= 0:
            os.close(destination_descriptor)
    return SnapshotEntry(
        input_id=input_id,
        source_path=str(resolved),
        relative_path=destination.relative_to(snapshot_root).as_posix(),
        size=copied,
        sha256=digest.hexdigest(),
        mode=mode,
    )


def copy_snapshot_tree(
    source: Path,
    destination: Path,
    *,
    input_id: str,
    snapshot_root: Path,
) -> list[SnapshotEntry]:
    if source.is_symlink():
        raise ResultEvidenceError(
            f"snapshot directory must not be a symlink: {source}"
        )
    try:
        resolved = source.resolve(strict=True)
        metadata = resolved.stat(follow_symlinks=False)
    except OSError as error:
        raise ResultEvidenceError(
            f"cannot inspect snapshot directory {source}: {error}"
        ) from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise ResultEvidenceError(
            f"snapshot input is not a directory: {resolved}"
        )
    def inspect_tree() -> tuple[
        dict[str, tuple[int, int, int, int, int]],
        dict[str, tuple[int, int, int, int, int, int]],
    ]:
        directories: dict[str, tuple[int, int, int, int, int]] = {}
        files: dict[str, tuple[int, int, int, int, int, int]] = {}
        try:
            for current, directory_names, file_names in os.walk(
                resolved,
                topdown=True,
                followlinks=False,
            ):
                directory_names.sort()
                file_names.sort()
                current_path = Path(current)
                current_metadata = current_path.stat(follow_symlinks=False)
                if current_path.is_symlink() or not stat.S_ISDIR(
                    current_metadata.st_mode
                ):
                    raise ResultEvidenceError(
                        f"snapshot tree contains an unsafe directory: {current_path}"
                    )
                relative_directory = current_path.relative_to(resolved).as_posix()
                directories[relative_directory or "."] = (
                    current_metadata.st_dev,
                    current_metadata.st_ino,
                    stat.S_IMODE(current_metadata.st_mode),
                    current_metadata.st_mtime_ns,
                    current_metadata.st_ctime_ns,
                )
                for name in directory_names:
                    path = current_path / name
                    entry_metadata = path.stat(follow_symlinks=False)
                    if path.is_symlink() or not stat.S_ISDIR(entry_metadata.st_mode):
                        raise ResultEvidenceError(
                            "snapshot tree contains a non-directory or symlink: "
                            f"{path}"
                        )
                for name in file_names:
                    path = current_path / name
                    entry_metadata = path.stat(follow_symlinks=False)
                    if path.is_symlink() or not stat.S_ISREG(entry_metadata.st_mode):
                        raise ResultEvidenceError(
                            "snapshot tree contains a non-regular file or symlink: "
                            f"{path}"
                        )
                    relative = path.relative_to(resolved).as_posix()
                    files[relative] = (
                        entry_metadata.st_dev,
                        entry_metadata.st_ino,
                        stat.S_IMODE(entry_metadata.st_mode),
                        entry_metadata.st_size,
                        entry_metadata.st_mtime_ns,
                        entry_metadata.st_ctime_ns,
                    )
        except OSError as error:
            raise ResultEvidenceError(
                f"cannot inspect snapshot tree {resolved}: {error}"
            ) from error
        return directories, files

    directories_before, files_before = inspect_tree()
    destination.mkdir(parents=True, mode=0o700)
    for relative in sorted(
        (path for path in directories_before if path != "."),
        key=lambda value: (value.count("/"), value.encode("utf-8")),
    ):
        (destination / relative).mkdir(mode=0o700)
    entries = [
        copy_snapshot_file(
            resolved / relative,
            destination / relative,
            input_id=input_id,
            snapshot_root=snapshot_root,
        )
        for relative in sorted(files_before, key=lambda value: value.encode("utf-8"))
    ]
    directories_after, files_after = inspect_tree()
    if directories_after != directories_before or files_after != files_before:
        raise ResultEvidenceError(
            f"snapshot input tree changed while copied: {resolved}"
        )
    return entries


def snapshot_fingerprint(
    entries: tuple[SnapshotEntry, ...],
    directories: tuple[str, ...],
) -> str:
    hasher = hashlib.sha256()
    hasher.update(SNAPSHOT_FINGERPRINT_DOMAIN.encode("utf-8") + b"\0")
    for directory in directories:
        path_bytes = directory.encode("utf-8")
        hasher.update(b"\x02")
        hasher.update(len(path_bytes).to_bytes(8, "big"))
        hasher.update(path_bytes)
    for entry in entries:
        path_bytes = entry.relative_path.encode("utf-8")
        input_id_bytes = entry.input_id.encode("utf-8")
        hasher.update(b"\x01")
        hasher.update(len(path_bytes).to_bytes(8, "big"))
        hasher.update(path_bytes)
        hasher.update(len(input_id_bytes).to_bytes(8, "big"))
        hasher.update(input_id_bytes)
        hasher.update(entry.size.to_bytes(8, "big"))
        hasher.update(entry.mode.to_bytes(4, "big"))
        hasher.update(bytes.fromhex(entry.sha256))
    return "sha256:" + hasher.hexdigest()


def snapshot_directories(root: Path) -> tuple[str, ...]:
    directories = ["."]
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ResultEvidenceError(
                f"snapshot contains a symlink: {path}"
            )
        if path.is_dir():
            directories.append(path.relative_to(root).as_posix())
        elif not path.is_file():
            raise ResultEvidenceError(
                f"snapshot contains a non-regular entry: {path}"
            )
    return tuple(sorted(directories, key=lambda value: value.encode("utf-8")))


def create_input_snapshot(
    args: argparse.Namespace,
    private_root: Path,
) -> tuple[argparse.Namespace, InputSnapshot]:
    snapshot_root = private_root / "evidence-inputs"
    snapshot_root.mkdir(mode=0o700)
    snapshot_args = argparse.Namespace(**vars(args))
    entries: list[SnapshotEntry] = []

    single_files = (
        ("harness", args.harness, snapshot_root / "harness"),
        ("addon", args.addon, snapshot_root / "addon.so"),
        ("product_server", args.server, snapshot_root / "server"),
        (
            "addon_config",
            args.addon_config,
            snapshot_root / "config/addon.conf",
        ),
        (
            "input_method_config",
            args.input_method_config,
            snapshot_root / "config/input-method.conf",
        ),
    )
    for input_id, source, destination in single_files:
        entries.append(
            copy_snapshot_file(
                source,
                destination,
                input_id=input_id,
                snapshot_root=snapshot_root,
            )
        )

    snapshot_args.harness = snapshot_root / "harness"
    snapshot_args.addon = snapshot_root / "addon.so"
    snapshot_args.server = snapshot_root / "server"
    snapshot_args.addon_config = snapshot_root / "config/addon.conf"
    snapshot_args.input_method_config = snapshot_root / "config/input-method.conf"

    snapshot_args.dictionary = snapshot_root / "dictionary"
    entries.extend(
        copy_snapshot_tree(
            args.dictionary,
            snapshot_args.dictionary,
            input_id="dictionary",
            snapshot_root=snapshot_root,
        )
    )
    snapshot_args.llama_lib_dir = snapshot_root / "llama-lib"
    entries.extend(
        copy_snapshot_tree(
            args.llama_lib_dir,
            snapshot_args.llama_lib_dir,
            input_id="llama_lib",
            snapshot_root=snapshot_root,
        )
    )

    snapshot_args.system_test_addon_dir = snapshot_root / "system-test-addon"
    for name in ("testfrontend.conf", "testui.conf", "testim.conf"):
        entries.append(
            copy_snapshot_file(
                args.system_test_addon_dir / name,
                snapshot_args.system_test_addon_dir / name,
                input_id=f"system_test_addon:{name}",
                snapshot_root=snapshot_root,
            )
        )

    if args.converter_backend == "mozc":
        assert args.mozc_verifier is not None
        assert args.mozc_generation is not None
        snapshot_args.mozc_verifier = snapshot_root / "mozc/verifier.py"
        entries.append(
            copy_snapshot_file(
                args.mozc_verifier,
                snapshot_args.mozc_verifier,
                input_id="mozc_verifier",
                snapshot_root=snapshot_root,
            )
        )
        snapshot_args.mozc_generation = snapshot_root / "mozc/generation"
        entries.extend(
            copy_snapshot_tree(
                args.mozc_generation,
                snapshot_args.mozc_generation,
                input_id="mozc_generation",
                snapshot_root=snapshot_root,
            )
        )
        mozc_directories = {"."}
        mozc_files: set[str] = set()
        for path in snapshot_args.mozc_generation.rglob("*"):
            relative = path.relative_to(snapshot_args.mozc_generation).as_posix()
            if path.is_symlink():
                raise ResultEvidenceError(
                    f"Mozc snapshot bundle contains a symlink: {relative}"
                )
            if path.is_dir():
                mozc_directories.add(relative)
            elif path.is_file():
                mozc_files.add(relative)
            else:
                raise ResultEvidenceError(
                    f"Mozc snapshot bundle contains a special entry: {relative}"
                )
        expected_mozc_files = {
            *MOZC_RUNTIME_FILES,
            *(f"licenses/{name}" for name in MOZC_BUNDLE_LICENSE_FILES),
        }
        if mozc_directories != {".", "licenses"} or mozc_files != expected_mozc_files:
            raise ResultEvidenceError(
                "Mozc snapshot bundle does not match the fixed artifact set"
            )

    entries_tuple = tuple(
        sorted(entries, key=lambda entry: entry.relative_path.encode("utf-8"))
    )
    server_entries = [
        entry for entry in entries_tuple if entry.input_id == "product_server"
    ]
    if len(server_entries) != 1:
        raise ResultEvidenceError("snapshot did not capture exactly one product server")
    server_entry = server_entries[0]
    if (
        server_entry.sha256 != args.product_server_sha256
        or server_entry.size != args.product_server_size
    ):
        raise ResultEvidenceError(
            "snapshot product server does not match the explicit SHA-256/size binding"
        )

    directories = snapshot_directories(snapshot_root)
    for relative in sorted(
        directories,
        key=lambda value: (value.count("/"), value),
        reverse=True,
    ):
        path = snapshot_root if relative == "." else snapshot_root / relative
        path.chmod(0o555)
    snapshot = InputSnapshot(
        root=snapshot_root,
        entries=entries_tuple,
        directories=directories,
        fingerprint=snapshot_fingerprint(entries_tuple, directories),
    )
    verify_input_snapshot(snapshot)
    return snapshot_args, snapshot


def snapshot_payload(
    snapshot: InputSnapshot,
    *,
    post_run_verified: bool,
) -> dict[str, object]:
    return {
        "schema": SNAPSHOT_SCHEMA,
        "root": str(snapshot.root),
        "fingerprint": snapshot.fingerprint,
        "directories": list(snapshot.directories),
        "entries": [
            {
                "input_id": entry.input_id,
                "source_path": entry.source_path,
                "relative_path": entry.relative_path,
                "size": entry.size,
                "sha256": entry.sha256,
                "mode": entry.mode,
            }
            for entry in snapshot.entries
        ],
        "integrity": {
            "post_run_verified": post_run_verified,
            "entry_count": len(snapshot.entries),
        },
    }


def verify_input_snapshot(snapshot: InputSnapshot) -> None:
    root = snapshot.root
    if root.is_symlink() or not root.is_dir():
        raise ResultEvidenceError("input snapshot root is unavailable or unsafe")
    directories = snapshot_directories(root)
    if directories != snapshot.directories:
        raise ResultEvidenceError("input snapshot directory set changed")
    for relative in directories:
        path = root if relative == "." else root / relative
        metadata = path.stat(follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o555:
            raise ResultEvidenceError(
                f"input snapshot directory metadata changed: {relative}"
            )

    actual_files = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    expected_files = {entry.relative_path for entry in snapshot.entries}
    if actual_files != expected_files:
        raise ResultEvidenceError("input snapshot file set changed")
    for entry in snapshot.entries:
        path = root / entry.relative_path
        if path.is_symlink():
            raise ResultEvidenceError(
                f"input snapshot file became a symlink: {entry.relative_path}"
            )
        metadata = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != entry.mode
            or metadata.st_size != entry.size
            or sha256_regular_file(path) != entry.sha256
        ):
            raise ResultEvidenceError(
                f"input snapshot integrity mismatch: {entry.relative_path}"
            )
    if snapshot_fingerprint(snapshot.entries, directories) != snapshot.fingerprint:
        raise ResultEvidenceError("input snapshot manifest fingerprint changed")


def source_binding() -> dict[str, object]:
    repository_root = Path(__file__).resolve().parents[2]
    head: str | None = None
    worktree_clean: bool | None = None
    try:
        head_result = subprocess.run(
            ["git", "-C", str(repository_root), "rev-parse", "--verify", "HEAD"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            check=False,
        )
        status_result = subprocess.run(
            [
                "git",
                "-C",
                str(repository_root),
                "status",
                "--porcelain=v1",
                "--untracked-files=normal",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
    else:
        candidate = head_result.stdout.strip()
        if head_result.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", candidate):
            head = candidate
        if status_result.returncode == 0:
            worktree_clean = not bool(status_result.stdout)
    return {
        "repository_root": str(repository_root),
        "git_head": head,
        "worktree_clean": worktree_clean,
    }


def mozc_runtime_fingerprint(paths: dict[str, Path]) -> str:
    expected = set(MOZC_RUNTIME_FILES)
    if set(paths) != expected:
        missing = sorted(expected - set(paths))
        unexpected = sorted(set(paths) - expected)
        raise ResultEvidenceError(
            "Mozc runtime fingerprint requires exactly helper/data/manifest; "
            f"missing={missing}, unexpected={unexpected}"
        )
    directory_hasher = hashlib.sha256()
    directory_hasher.update(
        MOZC_RUNTIME_FINGERPRINT_DOMAIN.encode("utf-8") + b"\0"
    )
    for relative_path in sorted(paths, key=lambda value: value.encode("utf-8")):
        path_bytes = relative_path.encode("utf-8")
        directory_hasher.update(b"\x01")
        directory_hasher.update(len(path_bytes).to_bytes(8, byteorder="big"))
        directory_hasher.update(path_bytes)
        directory_hasher.update(sha256_regular_file_bytes(paths[relative_path]))
    return "sha256:" + directory_hasher.hexdigest()


def capture_evidence_bindings(
    args: argparse.Namespace,
    private_generation: Path | None,
    snapshot: InputSnapshot,
) -> dict[str, object]:
    artifacts: dict[str, object] = {
        "harness": file_binding(args.harness),
        "addon": file_binding(args.addon),
        "server": file_binding(args.server),
        "dictionary": {"path": str(args.dictionary.resolve(strict=True))},
        "llama_library_directory": {
            "path": str(args.llama_lib_dir.resolve(strict=True))
        },
    }
    if args.converter_backend == "mozc":
        assert args.mozc_verifier is not None
        assert args.mozc_generation is not None
        assert private_generation is not None
        source_generation = args.mozc_generation.resolve(strict=True)
        generation = private_generation.resolve(strict=True)
        source_manifest_entries = [
            entry
            for entry in snapshot.entries
            if entry.relative_path == "mozc/generation/manifest.json"
        ]
        if len(source_manifest_entries) != 1:
            raise ResultEvidenceError(
                "Mozc snapshot must contain exactly one source manifest"
            )
        generation_name = Path(source_manifest_entries[0].source_path).parent.name
        content_address = (
            generation_name
            if re.fullmatch(r"sha256-[0-9a-f]{64}", generation_name)
            else None
        )
        prepared_content_address = (
            generation.name
            if re.fullmatch(r"sha256-[0-9a-f]{64}", generation.name)
            else None
        )
        manifest = source_generation / "manifest.json"
        fingerprint_inputs = {
            "fcitx5-grimodex-mozc-helper": (
                generation / "fcitx5-grimodex-mozc-helper"
            ),
            "manifest.json": manifest,
            "mozc.data": generation / "mozc.data",
        }
        artifact_fingerprint = mozc_runtime_fingerprint(fingerprint_inputs)
        artifacts.update(
            {
                "mozc_verifier": file_binding(args.mozc_verifier),
                "mozc_helper": readonly_runtime_binding(
                    generation / "fcitx5-grimodex-mozc-helper"
                ),
                "mozc_data": readonly_runtime_binding(generation / "mozc.data"),
                "mozc_generation": {
                    "path": str(generation),
                    "source_path": str(source_generation),
                    "content_address": content_address,
                    "prepared_content_address": prepared_content_address,
                    "artifact_fingerprint": artifact_fingerprint,
                },
            }
        )
        artifacts["mozc_manifest"] = file_binding(manifest)
    return {
        "producer": file_binding(Path(__file__)),
        "source": source_binding(),
        "artifacts": artifacts,
        "input_snapshot": snapshot_payload(
            snapshot,
            post_run_verified=False,
        ),
        "runtime_integrity": {
            "post_run_verified": False,
            "verified_artifacts": [],
        },
    }


def verify_bound_artifact(name: str, binding: object) -> None:
    if not isinstance(binding, dict):
        raise ResultEvidenceError(f"missing exact binding for {name}")
    path_value = binding.get("path")
    size = binding.get("size")
    digest = binding.get("sha256")
    mode = binding.get("mode")
    if (
        not isinstance(path_value, str)
        or not isinstance(size, int)
        or size < 0
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or not isinstance(mode, int)
        or mode < 0
        or mode > 0o7777
    ):
        raise ResultEvidenceError(f"invalid exact binding for {name}")
    path = Path(path_value)
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as error:
        raise ResultEvidenceError(
            f"cannot reverify bound artifact {name}: {error}"
        ) from error
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != mode
        or metadata.st_size != size
        or sha256_regular_file(path) != digest
    ):
        raise ResultEvidenceError(
            f"post-run integrity mismatch for bound artifact {name}"
        )


def verify_post_run_evidence(
    snapshot: InputSnapshot,
    bindings: dict[str, object],
) -> None:
    verify_input_snapshot(snapshot)
    artifacts = bindings.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ResultEvidenceError("evidence artifacts are unavailable")
    verified_artifacts: list[str] = []
    generation = artifacts.get("mozc_generation")
    if generation is not None:
        if not isinstance(generation, dict):
            raise ResultEvidenceError("Mozc runtime generation binding is invalid")
        generation_path_value = generation.get("path")
        prepared_content_address = generation.get("prepared_content_address")
        if (
            not isinstance(generation_path_value, str)
            or not isinstance(prepared_content_address, str)
            or re.fullmatch(
                r"sha256-[0-9a-f]{64}",
                prepared_content_address,
            )
            is None
        ):
            raise ResultEvidenceError("Mozc runtime generation identity is invalid")
        generation_path = Path(generation_path_value)
        try:
            generation_metadata = generation_path.stat(follow_symlinks=False)
            runtime_contents = {entry.name for entry in generation_path.iterdir()}
        except OSError as error:
            raise ResultEvidenceError(
                f"cannot reverify Mozc runtime generation: {error}"
            ) from error
        if (
            generation_path.is_symlink()
            or not stat.S_ISDIR(generation_metadata.st_mode)
            or stat.S_IMODE(generation_metadata.st_mode) != 0o555
            or generation_path.name != prepared_content_address
            or runtime_contents
            != {"fcitx5-grimodex-mozc-helper", "mozc.data"}
        ):
            raise ResultEvidenceError(
                "post-run Mozc runtime generation integrity mismatch"
            )
    for name in ("mozc_helper", "mozc_data"):
        if name in artifacts:
            verify_bound_artifact(name, artifacts[name])
            verified_artifacts.append(name)
    bindings["input_snapshot"] = snapshot_payload(
        snapshot,
        post_run_verified=True,
    )
    bindings["runtime_integrity"] = {
        "post_run_verified": True,
        "verified_artifacts": verified_artifacts,
    }


def launch_payload(launch: ProcessLaunch) -> dict[str, object]:
    return {"pid": launch.pid, "start_time": launch.start_time}


def identity_payload(identity: ProcessIdentity) -> dict[str, object]:
    return {
        "pid": identity.pid,
        "start_time": identity.start_time,
        "executable": identity.executable,
        "process_group": identity.process_group,
        "session_id": identity.session_id,
    }


def process_observation_payload(
    launches: tuple[ProcessLaunch, ...],
    identities: tuple[ProcessIdentity, ...],
    cleanup_ok: bool,
) -> dict[str, object]:
    return {
        "launch_count": len(launches),
        "recovery_count": max(0, len(launches) - 1),
        "launches": [launch_payload(launch) for launch in launches],
        "observed_identities": [
            identity_payload(identity) for identity in identities
        ],
        "cleanup_ok": cleanup_ok,
    }


def cycle_observation_payload(observation: CycleObservation) -> dict[str, object]:
    return {
        "cycle": observation.cycle,
        "conversions": observation.conversions,
        "lock_owner_observed": observation.lock_owner_observed,
        "max_concurrent_helpers": observation.max_concurrent_helpers,
        "process_group_cleanup_ok": observation.process_group_cleanup_ok,
        "server": process_observation_payload(
            observation.server_launches,
            observation.server_identities,
            observation.server_cleanup_ok,
        ),
        "helper": process_observation_payload(
            observation.helper_launches,
            observation.helper_identities,
            observation.helper_cleanup_ok,
        ),
    }


def build_structured_result(
    args: argparse.Namespace,
    observations: list[CycleObservation],
    bindings: dict[str, object],
    command: list[str],
    residue_count: int,
) -> dict[str, object]:
    source_ref = getattr(args, "product_source_ref", None)
    if not isinstance(source_ref, str) or re.fullmatch(
        r"[0-9a-f]{40}", source_ref
    ) is None:
        raise ResultEvidenceError(
            "structured result requires an explicit product source ref"
        )
    server_sha256 = getattr(args, "product_server_sha256", None)
    server_size = getattr(args, "product_server_size", None)
    if (
        not isinstance(server_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", server_sha256) is None
        or not isinstance(server_size, int)
        or server_size <= 0
    ):
        raise ResultEvidenceError(
            "structured result requires an explicit product server SHA-256/size"
        )
    source = bindings["source"]
    artifacts = bindings["artifacts"]
    input_snapshot = bindings.get("input_snapshot")
    runtime_integrity = bindings.get("runtime_integrity")
    assert isinstance(source, dict)
    assert isinstance(artifacts, dict)
    if not isinstance(input_snapshot, dict):
        raise ResultEvidenceError("structured result requires an input snapshot")
    snapshot_integrity = input_snapshot.get("integrity")
    snapshot_entries = input_snapshot.get("entries")
    if (
        not isinstance(snapshot_integrity, dict)
        or snapshot_integrity.get("post_run_verified") is not True
        or not isinstance(snapshot_entries, list)
    ):
        raise ResultEvidenceError(
            "structured result requires post-run snapshot integrity"
        )
    server_entries = [
        entry
        for entry in snapshot_entries
        if isinstance(entry, dict) and entry.get("input_id") == "product_server"
    ]
    if (
        len(server_entries) != 1
        or server_entries[0].get("sha256") != server_sha256
        or server_entries[0].get("size") != server_size
    ):
        raise ResultEvidenceError(
            "structured result product server does not match the snapshot"
        )
    if (
        not isinstance(runtime_integrity, dict)
        or runtime_integrity.get("post_run_verified") is not True
    ):
        raise ResultEvidenceError(
            "structured result requires post-run runtime integrity"
        )
    generation = artifacts.get("mozc_generation")
    artifact_fingerprint = (
        generation.get("artifact_fingerprint")
        if isinstance(generation, dict)
        else None
    )
    if args.converter_backend == "mozc" and (
        not isinstance(artifact_fingerprint, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", artifact_fingerprint) is None
    ):
        raise ResultEvidenceError(
            "Mozc structured result requires an exact runtime fingerprint"
        )
    return {
        "schema": RESULT_SCHEMA,
        "version": RESULT_VERSION,
        "exit_code": 0,
        "producer": bindings["producer"],
        "source": source,
        "product_source_ref": source_ref,
        "product_server": {
            "sha256": server_sha256,
            "size": server_size,
        },
        "artifact_fingerprint": artifact_fingerprint,
        "command": command,
        "artifacts": artifacts,
        "input_snapshot": input_snapshot,
        "runtime_integrity": runtime_integrity,
        "configuration": {
            "converter_backend": args.converter_backend,
            "iterations": args.soak_iterations,
            "cycles": args.cycles,
            "timeout_seconds": args.timeout,
        },
        "conversions": sum(item.conversions for item in observations),
        "cycles": len(observations),
        "helper_launches": sum(len(item.helper_launches) for item in observations),
        "server_launches": sum(len(item.server_launches) for item in observations),
        "helper_recoveries": sum(
            max(0, len(item.helper_launches) - 1) for item in observations
        ),
        "server_recoveries": sum(
            max(0, len(item.server_launches) - 1) for item in observations
        ),
        "residue_count": residue_count,
        "cycle_results": [
            cycle_observation_payload(item) for item in observations
        ],
    }


def open_result_output(path: Path) -> ResultOutput:
    if path.name in ("", ".", ".."):
        raise ResultEvidenceError("result output must name a JSON file")
    try:
        parent = path.parent.resolve(strict=True)
        descriptor = os.open(
            parent,
            os.O_RDONLY
            | os.O_CLOEXEC
            | os.O_DIRECTORY
            | os.O_NOFOLLOW,
        )
    except OSError as error:
        raise ResultEvidenceError(
            f"cannot bind result output parent {path.parent}: {error}"
        ) from error
    try:
        descriptor_metadata = os.fstat(descriptor)
        path_metadata = os.stat(parent, follow_symlinks=False)
        if (
            not stat.S_ISDIR(descriptor_metadata.st_mode)
            or descriptor_metadata.st_dev != path_metadata.st_dev
            or descriptor_metadata.st_ino != path_metadata.st_ino
            or descriptor_metadata.st_uid != os.getuid()
            or stat.S_IMODE(descriptor_metadata.st_mode) & 0o022
        ):
            raise ResultEvidenceError(
                "result output parent is not a stable user-owned directory"
            )
        try:
            os.stat(path.name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError(
                f"result output already exists: {parent / path.name}"
            )
    except BaseException:
        os.close(descriptor)
        raise
    return ResultOutput(parent, path.name, descriptor)


def atomic_publish_json(
    destination: ResultOutput,
    payload: dict[str, object],
) -> None:
    if destination.parent_fd < 0:
        raise ResultEvidenceError("result output parent descriptor is closed")
    temporary_name: str | None = None
    temporary_descriptor = -1
    temporary_metadata: os.stat_result | None = None
    destination_linked = False
    for _ in range(100):
        candidate = f".{destination.filename}.{secrets.token_hex(16)}.tmp"
        try:
            temporary_descriptor = os.open(
                candidate,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_CLOEXEC
                | os.O_NOFOLLOW,
                0o600,
                dir_fd=destination.parent_fd,
            )
        except FileExistsError:
            continue
        temporary_name = candidate
        break
    if temporary_name is None or temporary_descriptor < 0:
        raise ResultEvidenceError("could not allocate a private result temp file")
    try:
        temporary_metadata = os.fstat(temporary_descriptor)
        if (
            not stat.S_ISREG(temporary_metadata.st_mode)
            or temporary_metadata.st_uid != os.getuid()
            or stat.S_IMODE(temporary_metadata.st_mode) != 0o600
            or temporary_metadata.st_nlink != 1
        ):
            raise ResultEvidenceError("result temp file has unsafe metadata")
        with os.fdopen(
            temporary_descriptor,
            "w",
            encoding="utf-8",
            newline="\n",
        ) as output:
            temporary_descriptor = -1
            json.dump(
                payload,
                output,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        named_temp_metadata = os.stat(
            temporary_name,
            dir_fd=destination.parent_fd,
            follow_symlinks=False,
        )
        if (
            named_temp_metadata.st_dev != temporary_metadata.st_dev
            or named_temp_metadata.st_ino != temporary_metadata.st_ino
            or not stat.S_ISREG(named_temp_metadata.st_mode)
        ):
            raise ResultEvidenceError("result temp file changed before publication")
        try:
            os.link(
                temporary_name,
                destination.filename,
                src_dir_fd=destination.parent_fd,
                dst_dir_fd=destination.parent_fd,
                follow_symlinks=False,
            )
            destination_linked = True
        except FileExistsError as error:
            raise FileExistsError(
                "result output already exists: "
                f"{destination.parent_path / destination.filename}"
            ) from error
        published_metadata = os.stat(
            destination.filename,
            dir_fd=destination.parent_fd,
            follow_symlinks=False,
        )
        if (
            published_metadata.st_dev != temporary_metadata.st_dev
            or published_metadata.st_ino != temporary_metadata.st_ino
            or not stat.S_ISREG(published_metadata.st_mode)
        ):
            raise ResultEvidenceError("published result inode does not match temp file")
        os.fsync(destination.parent_fd)
        os.unlink(temporary_name, dir_fd=destination.parent_fd)
        temporary_name = None
        os.fsync(destination.parent_fd)
    except BaseException as error:
        cleanup_errors: list[str] = []
        if destination_linked and temporary_metadata is not None:
            try:
                published_metadata = os.stat(
                    destination.filename,
                    dir_fd=destination.parent_fd,
                    follow_symlinks=False,
                )
                if (
                    published_metadata.st_dev != temporary_metadata.st_dev
                    or published_metadata.st_ino != temporary_metadata.st_ino
                ):
                    cleanup_errors.append("destination inode changed")
                else:
                    os.unlink(
                        destination.filename,
                        dir_fd=destination.parent_fd,
                    )
            except FileNotFoundError:
                pass
            except OSError as cleanup_error:
                cleanup_errors.append(str(cleanup_error))
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=destination.parent_fd)
                temporary_name = None
            except FileNotFoundError:
                temporary_name = None
            except OSError as cleanup_error:
                cleanup_errors.append(str(cleanup_error))
        try:
            os.fsync(destination.parent_fd)
        except OSError as cleanup_error:
            cleanup_errors.append(str(cleanup_error))
        if cleanup_errors:
            raise ResultEvidenceError(
                "result publication rollback was incomplete: "
                + "; ".join(cleanup_errors)
            ) from error
        raise
    finally:
        if temporary_descriptor >= 0:
            os.close(temporary_descriptor)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=destination.parent_fd)
            except FileNotFoundError:
                pass


def observed_residue_count(
    root: Path,
    observations: list[CycleObservation],
) -> int:
    residues: set[tuple[object, ...]] = set()
    for observation in observations:
        for launch in (*observation.server_launches, *observation.helper_launches):
            identity = identity_for_launch(launch)
            if identity is not None:
                residues.add(("process", identity.pid, identity.start_time))
        for identity in (
            *observation.server_identities,
            *observation.helper_identities,
        ):
            if process_still_matches(identity):
                residues.add(("process", identity.pid, identity.start_time))
    for session_leader in ACTIVE_TEST_SESSIONS:
        residues.add(("active-session", session_leader))
        for identity in session_process_identities(session_leader):
            residues.add(("process", identity.pid, identity.start_time))
    for audit in ACTIVE_PROCESS_AUDITS:
        residues.add(("active-audit", str(audit)))
    if os.path.lexists(root):
        residues.add(("private-root", str(root)))
    return len(residues)


def publish_success_result(
    args: argparse.Namespace,
    root: Path,
    observations: list[CycleObservation],
    bindings: dict[str, object],
    command: list[str],
    destination: ResultOutput,
) -> None:
    assert args.result_output is not None
    residue_count = observed_residue_count(root, observations)
    if residue_count != 0:
        raise ResultEvidenceError(
            "refusing to publish Fcitx evidence with "
            f"{residue_count} final residue(s)"
        )
    if len(observations) != args.cycles:
        raise ResultEvidenceError(
            "refusing to publish incomplete Fcitx cycle evidence"
        )
    atomic_publish_json(
        destination,
        build_structured_result(
            args,
            observations,
            bindings,
            command,
            residue_count,
        ),
    )


def verifier_environment(root: Path) -> dict[str, str]:
    home = root / "verifier-home"
    temporary = root / "verifier-tmp"
    home.mkdir(mode=0o700)
    temporary.mkdir(mode=0o700)
    return {
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "TMPDIR": str(temporary),
    }


def run_verifier(
    command: list[str],
    environment: dict[str, str],
) -> tuple[int, str, str]:
    process = subprocess.Popen(
        command,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    ACTIVE_TEST_SESSIONS.add(process.pid)
    try:
        stdout, stderr = process.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        terminated = terminate_process_group(process)
        try:
            stdout, stderr = process.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            stdout = ""
            stderr = "Mozc artifact verifier pipes did not close\n"
            terminated = False
        if not terminated:
            stderr += "Mozc artifact verifier process group did not exit\n"
        else:
            ACTIVE_TEST_SESSIONS.discard(process.pid)
        return 124, stdout, stderr + "Mozc artifact verifier timed out\n"
    if not drain_process_session(process.pid):
        return 1, stdout, stderr + "Mozc artifact verifier left a child process\n"
    ACTIVE_TEST_SESSIONS.discard(process.pid)
    return process.returncode, stdout, stderr


def prepare_mozc_runtime(args: argparse.Namespace, root: Path) -> Path:
    assert args.mozc_verifier is not None
    assert args.mozc_generation is not None
    if args.mozc_generation.is_symlink():
        raise RuntimeError("Mozc source generation must not be a symlink")
    verifier = args.mozc_verifier.resolve(strict=True)
    source_generation = args.mozc_generation.resolve(strict=True)
    environment = verifier_environment(root)

    helper = source_generation / "fcitx5-grimodex-mozc-helper"
    data = source_generation / "mozc.data"
    if args.result_output is not None:
        # The evidence snapshot deliberately removes owner-write bits from all
        # directories. Staged-generation verification requires its original
        # content-addressed basename and a mode-0755 license directory, so use
        # the installed-runtime action here. It re-copies and verifies the
        # fixed helper/data bytes, ABI, and PING from the readonly snapshot.
        verification_command = [
            sys.executable,
            str(verifier),
            "--verify-installed-runtime",
            "--helper",
            str(helper),
            "--data",
            str(data),
        ]
    else:
        verification_command = [
            sys.executable,
            str(verifier),
            "--verify-host-runtime",
            str(source_generation),
        ]
    result, _, stderr = run_verifier(verification_command, environment)
    if result != 0:
        raise RuntimeError(
            "Mozc source generation failed host-runtime verification:\n" + stderr
        )

    runtime_root = (root / "mozc-runtime").absolute()
    result, stdout, stderr = run_verifier(
        [
            sys.executable,
            str(verifier),
            "--prepare-installed-runtime",
            "--helper",
            str(helper),
            "--data",
            str(data),
            "--runtime-root",
            str(runtime_root),
        ],
        environment,
    )
    if result != 0:
        raise RuntimeError("Mozc private runtime preparation failed:\n" + stderr)

    output_lines = stdout.splitlines()
    if len(output_lines) != 1 or not output_lines[0]:
        raise RuntimeError("Mozc verifier returned an invalid generation path")
    returned = Path(output_lines[0])
    if not returned.is_absolute():
        raise RuntimeError("Mozc verifier returned a non-absolute generation path")
    try:
        runtime_root_resolved = runtime_root.resolve(strict=True)
        returned_resolved = returned.resolve(strict=True)
    except OSError as error:
        raise RuntimeError("Mozc private runtime path cannot be resolved") from error
    if returned != returned_resolved or returned.parent != runtime_root_resolved:
        raise RuntimeError(
            "Mozc verifier returned a generation outside the private runtime root"
        )
    if stat.S_IMODE(runtime_root_resolved.stat().st_mode) != 0o700:
        raise RuntimeError("Mozc private runtime root is not mode 0700")

    private_helper = returned_resolved / "fcitx5-grimodex-mozc-helper"
    private_data = returned_resolved / "mozc.data"
    if (
        returned_resolved.is_symlink()
        or private_helper.is_symlink()
        or private_data.is_symlink()
        or stat.S_IMODE(returned_resolved.stat().st_mode) != 0o555
        or stat.S_IMODE(private_helper.stat().st_mode) != 0o555
        or stat.S_IMODE(private_data.stat().st_mode) != 0o444
    ):
        raise RuntimeError("Mozc private runtime is not the readonly generation")
    return returned_resolved


def make_runtime_writable(runtime_root: Path) -> None:
    if not runtime_root.exists() or runtime_root.is_symlink():
        return
    for current_root, directories, files in os.walk(
        runtime_root,
        topdown=False,
        followlinks=False,
    ):
        current = Path(current_root)
        for filename in files:
            path = current / filename
            if not path.is_symlink():
                path.chmod(0o600)
        for directory in directories:
            path = current / directory
            if not path.is_symlink():
                path.chmod(0o700)
        current.chmod(0o700)


def configure_cycle(
    args: argparse.Namespace,
    root: Path,
    private_generation: Path | None,
    launch_bindings: dict[str, dict[str, object]] | None = None,
) -> tuple[dict[str, str], Path, Path, Path | None]:
    addon_dir = root / "share/fcitx5/addon"
    input_method_dir = root / "share/fcitx5/inputmethod"
    config_dir = root / "config/fcitx5"
    server_config_dir = root / "config/fcitx5-grimodex"
    runtime_dir = root / "runtime"
    home_dir = root / "home"
    bin_dir = root / "bin"
    temporary_dir = root / "tmp"
    for directory in (
        addon_dir,
        input_method_dir,
        config_dir,
        server_config_dir,
        runtime_dir,
        home_dir,
        bin_dir,
        temporary_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    runtime_dir.chmod(0o700)
    bin_dir.chmod(0o700)
    temporary_dir.chmod(0o700)

    addon_path = args.addon.resolve()
    addon_text = args.addon_config.read_text(encoding="utf-8")
    addon_stem = str(addon_path)
    if addon_stem.endswith(".so"):
        addon_stem = addon_stem[:-3]
    addon_text, count = re.subn(
        r"^Library=.*$",
        f"Library={addon_stem}",
        addon_text,
        count=1,
        flags=re.MULTILINE,
    )
    if count != 1:
        raise RuntimeError("generated Grimodex addon config has no Library field")
    (addon_dir / "grimodex.conf").write_text(addon_text, encoding="utf-8")
    shutil.copy2(args.input_method_config, input_method_dir / "grimodex.conf")
    for name in ("testfrontend.conf", "testui.conf", "testim.conf"):
        shutil.copy2(args.system_test_addon_dir / name, addon_dir / name)

    (config_dir / "profile").write_text(
        """[Groups/0]
Name=Integration
Default Layout=us
DefaultIM=grimodex

[Groups/0/Items/0]
Name=testim
Layout=

[Groups/0/Items/1]
Name=grimodex
Layout=

[GroupOrder]
0=Integration
""",
        encoding="utf-8",
    )
    # Keep conversion deterministic and display-free. A developer's local
    # Zenzai model must not turn this timer test into GPU/model-loading I/O.
    (server_config_dir / "config.json").write_text(
        json.dumps(
            [
                {
                    "profileName": "Fcitx full-stack test",
                    "autoConvertMode": 3,
                    "liveConversionDelayMsec": 228,
                    "suggestionListMode": 3,
                    "numSuggestions": 3,
                    "numCandidatesPerPage": 9,
                    "useInputHistory": False,
                    "zenzaiEnable": False,
                    "enabledTables": [
                        {
                            "name": "Romaji",
                            "isBuiltIn": True,
                            "filename": "Romaji",
                        }
                    ],
                }
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    server_audit = root / "server-launches.log"
    create_launch_wrapper(
        bin_dir / "fcitx5-grimodex-server",
        args.server,
        server_audit,
        launch_bindings.get("server") if launch_bindings is not None else None,
    )
    helper_audit: Path | None = None
    helper_wrapper: Path | None = None
    if private_generation is not None:
        helper_audit = root / "helper-launches.log"
        helper_wrapper = bin_dir / "fcitx5-grimodex-mozc-helper"
        create_launch_wrapper(
            helper_wrapper,
            private_generation / "fcitx5-grimodex-mozc-helper",
            helper_audit,
            launch_bindings.get("helper") if launch_bindings is not None else None,
        )

    llama_lib_dir = args.llama_lib_dir.resolve()
    environment = {
        "HOME": str(home_dir),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "TMPDIR": str(temporary_dir),
        "XDG_CACHE_HOME": str(root / "cache"),
        "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_DATA_HOME": str(root / "data"),
        "XDG_DATA_DIRS": f"{root / 'share'}:/usr/local/share:/usr/share",
        "XDG_RUNTIME_DIR": str(runtime_dir),
        "XDG_STATE_HOME": str(root / "state"),
        "LD_LIBRARY_PATH": os.pathsep.join(
            (str(addon_path.parent), str(llama_lib_dir))
        ),
        "GGML_BACKEND_DIR": str(llama_lib_dir),
        "FCITX5_GRIMODEX_DICTIONARY": str(args.dictionary.resolve()),
        "GRIMODEX_FCITX_SOAK_ITERATIONS": str(args.soak_iterations),
    }
    if args.converter_backend == "mozc":
        assert private_generation is not None
        assert helper_wrapper is not None
        environment.update(
            {
                "FCITX5_GRIMODEX_CONVERTER": "mozc",
                "FCITX5_GRIMODEX_MOZC_HELPER": str(helper_wrapper),
                "FCITX5_GRIMODEX_MOZC_DATA": str(
                    private_generation / "mozc.data"
                ),
            }
        )
    return (
        environment,
        runtime_dir / "fcitx5-grimodex/server.lock",
        server_audit,
        helper_audit,
    )


def print_cycle_logs(root: Path, cycle: int) -> None:
    print(f"--- Fcitx full-stack cycle {cycle} stdout ---")
    print((root / "harness.stdout").read_text(encoding="utf-8", errors="replace"))
    print(f"--- Fcitx full-stack cycle {cycle} stderr ---")
    print((root / "harness.stderr").read_text(encoding="utf-8", errors="replace"))


def run_cycle(
    args: argparse.Namespace,
    root: Path,
    cycle: int,
    private_generation: Path | None,
    launch_bindings: dict[str, dict[str, object]] | None = None,
) -> tuple[int, set[ProcessIdentity], CycleObservation | None]:
    if ACTIVE_TEST_SESSIONS or ACTIVE_PROCESS_AUDITS:
        print("A process or launch audit leaked from an earlier cycle")
        return 1, set(), None

    environment, lock_file, server_audit, helper_audit = configure_cycle(
        args,
        root,
        private_generation,
        launch_bindings,
    )
    expected_server = args.server.resolve()
    private_helper = (
        private_generation / "fcitx5-grimodex-mozc-helper"
        if private_generation is not None
        else None
    )
    ACTIVE_PROCESS_AUDITS[server_audit] = expected_server
    if helper_audit is not None:
        assert private_helper is not None
        ACTIVE_PROCESS_AUDITS[helper_audit] = private_helper

    stdout_file = root / "harness.stdout"
    stderr_file = root / "harness.stderr"
    server_launches: list[ProcessLaunch] = []
    helper_launches: list[ProcessLaunch] = []
    observed_helpers: set[ProcessIdentity] = set()
    max_concurrent_helpers = 0
    server_identities: set[ProcessIdentity] = set()
    lock_owner_observed = False
    timed_out = False
    native_conversions: int | None = None
    with stdout_file.open("w", encoding="utf-8") as stdout_handle, stderr_file.open(
        "w", encoding="utf-8"
    ) as stderr_handle:
        process = subprocess.Popen(
            [str(args.harness.resolve())],
            env=environment,
            stdout=stdout_handle,
            stderr=stderr_handle,
            text=True,
            start_new_session=True,
        )
        ACTIVE_TEST_SESSIONS.add(process.pid)
        deadline = time.monotonic() + args.timeout
        while True:
            server_launches = load_process_launches(server_audit)
            current_servers = identities_for_launches(
                server_launches,
                expected_server,
            )
            server_identities.update(current_servers)
            server_identity = identity_from_lock(
                lock_file,
                expected_server,
                server_launches,
            )
            if server_identity is not None:
                server_identities.add(server_identity)
                lock_owner_observed = True
            if private_helper is not None and helper_audit is not None:
                helper_launches = load_process_launches(helper_audit)
                helper_identities = identities_for_launches(
                    helper_launches,
                    private_helper,
                )
                observed_helpers.update(helper_identities)
                max_concurrent_helpers = max(
                    max_concurrent_helpers,
                    len(helper_identities),
                )
            return_code = process.poll()
            if return_code is not None:
                break
            if time.monotonic() >= deadline:
                timed_out = True
                terminate_process_group(process)
                return_code = 124
                break
            time.sleep(0.01)

    server_launches = load_process_launches(server_audit)
    server_identities.update(
        identities_for_launches(server_launches, expected_server)
    )
    server_identity = identity_from_lock(
        lock_file,
        expected_server,
        server_launches,
    )
    if server_identity is not None:
        server_identities.add(server_identity)
        lock_owner_observed = True
    if private_helper is not None and helper_audit is not None:
        helper_launches = load_process_launches(helper_audit)
        helper_identities = identities_for_launches(
            helper_launches,
            private_helper,
        )
        observed_helpers.update(helper_identities)
        max_concurrent_helpers = max(
            max_concurrent_helpers,
            len(helper_identities),
        )

    process_group_cleanup_ok = drain_process_session(process.pid)
    if process_group_cleanup_ok:
        ACTIVE_TEST_SESSIONS.discard(process.pid)

    # Once the harness session is gone it cannot request another detached
    # server/helper launch. Reload the append-only audits before evaluating the
    # authoritative launch count and before cleanup.
    server_launches = load_process_launches(server_audit)
    server_identities.update(
        identities_for_launches(server_launches, expected_server)
    )
    if private_helper is not None and helper_audit is not None:
        helper_launches = load_process_launches(helper_audit)
        helper_identities = identities_for_launches(
            helper_launches,
            private_helper,
        )
        observed_helpers.update(helper_identities)
        max_concurrent_helpers = max(
            max_concurrent_helpers,
            len(helper_identities),
        )

    harness_ok = return_code == 0
    observation_ok = True
    if harness_ok and args.result_output is not None:
        try:
            native_conversions = completed_conversions(stderr_file)
        except ResultEvidenceError as error:
            print(error)
            observation_ok = False
        else:
            if native_conversions != args.soak_iterations:
                print(
                    f"Cycle {cycle} reported {native_conversions} completed "
                    f"conversions; configured {args.soak_iterations}"
                )
                observation_ok = False
    elif harness_ok:
        # Keep the pre-evidence CTest path behaviorally unchanged. The configured
        # value is used only in an observation that will never be published.
        native_conversions = args.soak_iterations
    if harness_ok and not lock_owner_observed:
        print(f"Cycle {cycle} did not observe the exact temporary server lock owner")
        observation_ok = False
    if harness_ok and len(server_launches) != 1:
        print(
            f"Cycle {cycle} recorded {len(server_launches)} temporary server "
            "launches; expected exactly one"
        )
        observation_ok = False
    if harness_ok and len(server_identities) != 1:
        print(
            f"Cycle {cycle} observed {len(server_identities)} exact temporary "
            "server identities; expected exactly one"
        )
        observation_ok = False
    if harness_ok and private_helper is not None and helper_audit is not None:
        if len(helper_launches) != 1:
            print(
                f"Cycle {cycle} recorded {len(helper_launches)} private Mozc "
                "helper launches; expected exactly one"
            )
            observation_ok = False
        if len(observed_helpers) != 1:
            print(
                f"Cycle {cycle} observed {len(observed_helpers)} exact Mozc helper "
                "identities; expected exactly one"
            )
            observation_ok = False
        if max_concurrent_helpers != 1:
            print(
                f"Cycle {cycle} reached {max_concurrent_helpers} concurrent exact "
                "Mozc helpers; expected exactly one"
            )
            observation_ok = False

    # Stop the server first so it cannot respawn the helper, then reload the
    # helper audit and stop every exact PID/start-time launch, including a
    # wrapper that had not reached exec yet.
    server_cleanup_ok = stop_process_launches(server_launches)
    final_server_launches = load_process_launches(server_audit)
    server_cleanup_ok = (
        stop_process_launches(final_server_launches)
        and server_cleanup_ok
        and not identities_for_launches(final_server_launches)
    )
    helper_cleanup_ok = True
    final_helper_launches: list[ProcessLaunch] = []
    if helper_audit is not None:
        final_helper_launches = load_process_launches(helper_audit)
        helper_cleanup_ok = stop_process_launches(final_helper_launches)
        final_helper_launches = load_process_launches(helper_audit)
        helper_cleanup_ok = (
            stop_process_launches(final_helper_launches)
            and helper_cleanup_ok
            and not identities_for_launches(final_helper_launches)
        )
    cleanup_ok = (
        server_cleanup_ok
        and helper_cleanup_ok
        and process_group_cleanup_ok
    )
    if cleanup_ok:
        ACTIVE_PROCESS_AUDITS.pop(server_audit, None)
        if helper_audit is not None:
            ACTIVE_PROCESS_AUDITS.pop(helper_audit, None)
    if not server_cleanup_ok:
        print(f"Cycle {cycle} could not terminate its exact temporary server")
    if not helper_cleanup_ok:
        print(f"Cycle {cycle} left an exact private Mozc helper running")
    if not process_group_cleanup_ok:
        print(f"Cycle {cycle} left a process in its private harness session")

    cycle_observation = CycleObservation(
        cycle=cycle,
        conversions=native_conversions if native_conversions is not None else 0,
        server_launches=tuple(final_server_launches),
        server_identities=tuple(
            sorted(
                server_identities,
                key=lambda identity: (
                    identity.pid,
                    identity.start_time,
                    identity.executable,
                ),
            )
        ),
        helper_launches=tuple(final_helper_launches),
        helper_identities=tuple(
            sorted(
                observed_helpers,
                key=lambda identity: (
                    identity.pid,
                    identity.start_time,
                    identity.executable,
                ),
            )
        ),
        lock_owner_observed=lock_owner_observed,
        max_concurrent_helpers=max_concurrent_helpers,
        server_cleanup_ok=server_cleanup_ok,
        helper_cleanup_ok=helper_cleanup_ok,
        process_group_cleanup_ok=process_group_cleanup_ok,
    )

    if timed_out:
        print_cycle_logs(root, cycle)
        print(f"Grimodex Fcitx full-stack cycle {cycle} timed out")
        return 124, server_identities, cycle_observation
    if not harness_ok or not observation_ok or not cleanup_ok:
        print_cycle_logs(root, cycle)
    if not harness_ok:
        return return_code, server_identities, cycle_observation
    if not observation_ok or not cleanup_ok:
        return 1, server_identities, cycle_observation
    return 0, server_identities, cycle_observation


def main() -> int:
    args = parse_args()
    required = [
        args.harness,
        args.addon,
        args.server,
        args.dictionary,
        args.addon_config,
        args.input_method_config,
        args.llama_lib_dir,
    ]
    test_addons = [
        args.system_test_addon_dir / "testfrontend.conf",
        args.system_test_addon_dir / "testui.conf",
        args.system_test_addon_dir / "testim.conf",
    ]
    if args.converter_backend == "mozc":
        assert args.mozc_verifier is not None
        assert args.mozc_generation is not None
        required.extend(
            [
                args.mozc_verifier,
                args.mozc_generation,
                args.mozc_generation / "fcitx5-grimodex-mozc-helper",
                args.mozc_generation / "manifest.json",
                args.mozc_generation / "mozc.data",
            ]
        )
    missing = [path for path in required + test_addons if not path.exists()]
    if missing:
        if args.converter_backend == "mozc":
            print(
                "ERROR: required Mozc Fcitx integration artifact is "
                f"unavailable: {missing[0]}"
            )
            return 1
        print("SKIP: Fcitx testfrontend or Grimodex build artifact is unavailable")
        return 77

    evidence_bindings: dict[str, object] | None = None
    evidence_command: list[str] | None = None
    result_output: ResultOutput | None = None
    if args.result_output is not None:
        try:
            result_output = open_result_output(args.result_output)
        except (FileExistsError, OSError, ResultEvidenceError) as error:
            print(f"ERROR: cannot prepare Fcitx result evidence: {error}")
            return 1
        evidence_command = [
            sys.executable,
            str(Path(__file__).resolve()),
            *sys.argv[1:],
        ]
    try:
        root = Path(tempfile.mkdtemp(prefix="grimodex-fcitx-full-stack-"))
        execution_args = args
        input_snapshot: InputSnapshot | None = None
        private_generation: Path | None = None
        launch_bindings: dict[str, dict[str, object]] | None = None
        all_server_identities: set[ProcessIdentity] = set()
        cycle_observations: list[CycleObservation] = []
        result = 1
        try:
            if args.result_output is not None:
                try:
                    execution_args, input_snapshot = create_input_snapshot(
                        args,
                        root,
                    )
                except (OSError, ResultEvidenceError) as error:
                    print(f"ERROR: cannot snapshot Fcitx evidence inputs: {error}")

            if input_snapshot is not None or args.result_output is None:
                if execution_args.converter_backend == "mozc":
                    try:
                        private_generation = prepare_mozc_runtime(
                            execution_args,
                            root,
                        )
                    except (OSError, RuntimeError) as error:
                        print(error)
                    else:
                        result = 0
                else:
                    result = 0

            if result == 0 and execution_args.result_output is not None:
                assert input_snapshot is not None
                try:
                    evidence_bindings = capture_evidence_bindings(
                        execution_args,
                        private_generation,
                        input_snapshot,
                    )
                except (OSError, ResultEvidenceError) as error:
                    print(f"ERROR: cannot bind Fcitx result evidence: {error}")
                    result = 1
                else:
                    artifacts = evidence_bindings["artifacts"]
                    assert isinstance(artifacts, dict)
                    server_binding = artifacts.get("server")
                    if not isinstance(server_binding, dict):
                        print("ERROR: exact server launch binding is unavailable")
                        result = 1
                    else:
                        launch_bindings = {"server": server_binding}
                        helper_binding = artifacts.get("mozc_helper")
                        if helper_binding is not None:
                            if not isinstance(helper_binding, dict):
                                print(
                                    "ERROR: exact helper launch binding is unavailable"
                                )
                                result = 1
                            else:
                                launch_bindings["helper"] = helper_binding

            if result == 0:
                for cycle in range(1, execution_args.cycles + 1):
                    cycle_root = root / f"cycle-{cycle:03d}"
                    cycle_root.mkdir(mode=0o700)
                    cycle_result, server_identities, cycle_observation = run_cycle(
                        execution_args,
                        cycle_root,
                        cycle,
                        private_generation,
                        launch_bindings,
                    )
                    all_server_identities.update(server_identities)
                    if cycle_observation is not None:
                        cycle_observations.append(cycle_observation)
                    if cycle_result != 0:
                        result = cycle_result
                        break
        finally:
            sessions_gone = True
            for session_leader in tuple(ACTIVE_TEST_SESSIONS):
                if drain_process_session(session_leader):
                    ACTIVE_TEST_SESSIONS.discard(session_leader)
                else:
                    sessions_gone = False
            servers_gone = stop_exact_processes(all_server_identities)
            audits_gone = True
            for audit in tuple(ACTIVE_PROCESS_AUDITS):
                try:
                    launches = load_process_launches(audit)
                    stopped = stop_process_launches(launches)
                    launches = load_process_launches(audit)
                    stopped = (
                        stop_process_launches(launches)
                        and stopped
                        and not identities_for_launches(launches)
                    )
                except ProcessInspectionError as error:
                    print(error)
                    stopped = False
                if stopped:
                    ACTIVE_PROCESS_AUDITS.pop(audit, None)
                else:
                    audits_gone = False
            # The per-cycle result still reports an initial cleanup failure, but a
            # final exact-identity retry may now make removal of the private tree
            # safe. Base this decision on the final observed process state.
            processes_gone = (
                servers_gone
                and sessions_gone
                and audits_gone
                and not ACTIVE_TEST_SESSIONS
                and not ACTIVE_PROCESS_AUDITS
            )
            if processes_gone:
                if input_snapshot is not None:
                    try:
                        if evidence_bindings is None:
                            verify_input_snapshot(input_snapshot)
                        else:
                            verify_post_run_evidence(
                                input_snapshot,
                                evidence_bindings,
                            )
                    except (OSError, ResultEvidenceError) as error:
                        print(
                            "ERROR: post-run Fcitx evidence integrity failed: "
                            f"{error}"
                        )
                        result = 1
                try:
                    make_runtime_writable(root)
                    shutil.rmtree(root)
                except OSError as error:
                    print(f"ERROR: cannot remove private Fcitx test root: {error}")
                    result = 1
            else:
                print(
                    "Preserving private test root because an exact test process "
                    f"could not be stopped: {root}"
                )
                if result == 0:
                    result = 1

        if result == 0 and execution_args.result_output is not None:
            assert evidence_bindings is not None
            assert evidence_command is not None
            assert result_output is not None
            try:
                publish_success_result(
                    execution_args,
                    root,
                    cycle_observations,
                    evidence_bindings,
                    evidence_command,
                    result_output,
                )
            except (
                FileExistsError,
                OSError,
                ProcessInspectionError,
                ResultEvidenceError,
            ) as error:
                print(f"ERROR: could not publish Fcitx result evidence: {error}")
                result = 1
        return result
    finally:
        if result_output is not None:
            result_output.close()


if __name__ == "__main__":
    raise SystemExit(main())
