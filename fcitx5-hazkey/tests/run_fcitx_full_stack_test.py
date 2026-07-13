#!/usr/bin/env python3
"""Run the built addon and server inside Fcitx's display-free test frontend."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import time


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
    return parser.parse_args()


def stop_server(lock_file: Path) -> None:
    if not lock_file.exists():
        return
    try:
        pid = int(lock_file.read_text(encoding="utf-8").splitlines()[0])
    except (ValueError, IndexError, OSError):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.025)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


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
    if any(not path.exists() for path in required + test_addons):
        print("SKIP: Fcitx testfrontend or Grimodex build artifact is unavailable")
        return 77

    with tempfile.TemporaryDirectory(prefix="grimodex-fcitx-full-stack-") as raw:
        root = Path(raw)
        addon_dir = root / "share/fcitx5/addon"
        input_method_dir = root / "share/fcitx5/inputmethod"
        config_dir = root / "config/fcitx5"
        runtime_dir = root / "runtime"
        home_dir = root / "home"
        bin_dir = root / "bin"
        for directory in (
            addon_dir,
            input_method_dir,
            config_dir,
            runtime_dir,
            home_dir,
            bin_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        runtime_dir.chmod(0o700)

        addon_text = args.addon_config.read_text(encoding="utf-8")
        addon_stem = str(args.addon)
        if addon_stem.endswith(".so"):
            addon_stem = addon_stem[:-3]
        addon_text, count = re.subn(
            r"^Library=.*$", f"Library={addon_stem}", addon_text,
            count=1, flags=re.MULTILINE,
        )
        if count != 1:
            raise RuntimeError("generated Grimodex addon config has no Library field")
        (addon_dir / "grimodex.conf").write_text(addon_text, encoding="utf-8")
        shutil.copy2(args.input_method_config, input_method_dir / "grimodex.conf")
        for source in test_addons:
            shutil.copy2(source, addon_dir / source.name)

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
        (bin_dir / "fcitx5-grimodex-server").symlink_to(args.server)

        environment = os.environ.copy()
        environment.update({
            "HOME": str(home_dir),
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_DATA_DIRS": f"{root / 'share'}:/usr/local/share:/usr/share",
            "XDG_RUNTIME_DIR": str(runtime_dir),
            "PATH": f"{bin_dir}:{environment.get('PATH', '/usr/bin')}",
            "FCITX5_GRIMODEX_DICTIONARY": str(args.dictionary),
        })
        library_paths = [str(args.addon.parent), str(args.llama_lib_dir)]
        if environment.get("LD_LIBRARY_PATH"):
            library_paths.append(environment["LD_LIBRARY_PATH"])
        environment["LD_LIBRARY_PATH"] = os.pathsep.join(library_paths)

        lock_file = runtime_dir / "fcitx5-grimodex/server.lock"
        stdout_file = root / "harness.stdout"
        stderr_file = root / "harness.stderr"
        try:
            with stdout_file.open("w", encoding="utf-8") as stdout_handle, \
                    stderr_file.open("w", encoding="utf-8") as stderr_handle:
                try:
                    completed = subprocess.run(
                        [str(args.harness)],
                        env=environment,
                        check=False,
                        stdout=stdout_handle,
                        stderr=stderr_handle,
                        text=True,
                        timeout=45,
                    )
                except subprocess.TimeoutExpired:
                    print(stdout_file.read_text(encoding="utf-8", errors="replace"))
                    print(stderr_file.read_text(encoding="utf-8", errors="replace"))
                    print("Grimodex Fcitx full-stack harness timed out")
                    return 124
        finally:
            stop_server(lock_file)
        if completed.returncode != 0:
            print(stdout_file.read_text(encoding="utf-8", errors="replace"))
            print(stderr_file.read_text(encoding="utf-8", errors="replace"))
        return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
