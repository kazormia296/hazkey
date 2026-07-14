#!/usr/bin/env python3
"""Run the built addon and server inside Fcitx's display-free test frontend."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import errno
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


MAXIMUM_RESTART_CYCLES = 1_000
MAXIMUM_SOAK_ITERATIONS = 1_000_000
MAXIMUM_TIMEOUT_SECONDS = 86_400
MAXIMUM_LAUNCH_AUDIT_BYTES = 65_536
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
    args = parser.parse_args()

    mozc_arguments = (args.mozc_verifier, args.mozc_generation)
    if args.converter_backend == "hazkey" and any(mozc_arguments):
        parser.error("Mozc arguments require --converter-backend=mozc")
    if args.converter_backend == "mozc" and not all(mozc_arguments):
        parser.error(
            "--converter-backend=mozc requires --mozc-verifier and "
            "--mozc-generation"
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


class ProcessInspectionError(RuntimeError):
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


def create_launch_wrapper(wrapper: Path, target: Path, audit: Path) -> None:
    target = target.resolve(strict=True)
    audit = audit.absolute()
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
import os
import stat
import sys

TARGET = {str(target)!r}
AUDIT = {str(audit)!r}

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

    result, _, stderr = run_verifier(
        [
            sys.executable,
            str(verifier),
            "--verify-host-runtime",
            str(source_generation),
        ],
        environment,
    )
    if result != 0:
        raise RuntimeError(
            "Mozc source generation failed host-runtime verification:\n" + stderr
        )

    runtime_root = (root / "mozc-runtime").absolute()
    helper = source_generation / "fcitx5-grimodex-mozc-helper"
    data = source_generation / "mozc.data"
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
) -> tuple[int, set[ProcessIdentity], bool]:
    if ACTIVE_TEST_SESSIONS or ACTIVE_PROCESS_AUDITS:
        print("A process or launch audit leaked from an earlier cycle")
        return 1, set(), False

    environment, lock_file, server_audit, helper_audit = configure_cycle(
        args,
        root,
        private_generation,
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

    if timed_out:
        print_cycle_logs(root, cycle)
        print(f"Grimodex Fcitx full-stack cycle {cycle} timed out")
        return 124, server_identities, cleanup_ok
    if not harness_ok or not observation_ok or not cleanup_ok:
        print_cycle_logs(root, cycle)
    if not harness_ok:
        return return_code, server_identities, cleanup_ok
    if not observation_ok or not cleanup_ok:
        return 1, server_identities, cleanup_ok
    return 0, server_identities, True


def main() -> int:
    args = parse_args()
    required = [
        args.harness,
        args.addon,
        args.server,
        args.dictionary,
        args.addon_config,
        args.input_method_config,
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

    root = Path(tempfile.mkdtemp(prefix="grimodex-fcitx-full-stack-"))
    private_generation: Path | None = None
    all_server_identities: set[ProcessIdentity] = set()
    result = 1
    try:
        if args.converter_backend == "mozc":
            try:
                private_generation = prepare_mozc_runtime(args, root)
            except RuntimeError as error:
                print(error)
            else:
                result = 0
        else:
            result = 0

        if result == 0:
            for cycle in range(1, args.cycles + 1):
                cycle_root = root / f"cycle-{cycle:03d}"
                cycle_root.mkdir(mode=0o700)
                cycle_result, server_identities, _ = run_cycle(
                    args,
                    cycle_root,
                    cycle,
                    private_generation,
                )
                all_server_identities.update(server_identities)
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
            make_runtime_writable(root / "mozc-runtime")
            shutil.rmtree(root)
        else:
            print(
                "Preserving private test root because an exact test process "
                f"could not be stopped: {root}"
            )
            if result == 0:
                result = 1
    return result


if __name__ == "__main__":
    raise SystemExit(main())
