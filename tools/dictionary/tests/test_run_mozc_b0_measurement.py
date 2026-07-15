from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT))

from tools.dictionary import run_mozc_b0_measurement as acquisition  # noqa: E402


POLICY_FIXTURE = (
    REPOSITORY_ROOT
    / "hazkey-server/Tests/grimodex-spike/Fixtures/mozc-adoption-v1/b0-policy.json"
)


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class AcquisitionTests(unittest.TestCase):
    def _fixture(self, root: Path) -> dict[str, object]:
        rows = [
            {
                "id": f"case-{index:03d}",
                "reading": f"よみ{index}",
                "expected": f"期待{index}",
                "category": "fixture",
            }
            for index in range(acquisition.CASES)
        ]
        corpus = root / "formal-256.tsv"
        corpus.write_text(
            "id\treading\texpected\tcategory\n"
            + "".join(
                f"{row['id']}\t{row['reading']}\t{row['expected']}\t{row['category']}\n"
                for row in rows
            ),
            encoding="utf-8",
        )
        executable = root / "hazkey-server"
        executable.write_bytes(b"fixture executable\n")
        executable.chmod(0o700)
        dictionary = root / "dictionary"
        bundle = root / "mozc-bundle"
        runtime_library_directory = root / "runtime-libs"
        dictionary.mkdir()
        bundle.mkdir()
        runtime_library_directory.mkdir()
        for index, name in enumerate(acquisition.RUNTIME_DEPENDENCY_FILENAMES):
            path = runtime_library_directory / name
            path.write_bytes(f"runtime dependency {index}: {name}\n".encode())
            path.chmod(0o755)
        policy = json.loads(POLICY_FIXTURE.read_text(encoding="utf-8"))
        source_ref = policy["candidate"]["product_source_revision"]
        corpus_sha = digest(corpus.read_bytes())

        executable_identity = {
            "size_bytes": executable.stat().st_size,
            "sha256": digest(executable.read_bytes()),
        }
        policy["candidate"]["product_executable"] = executable_identity
        for contract in policy["gates"]["long_running_stability"]["checks"]:
            if contract["native_producer"]["path"] == "<product-executable>":
                contract["native_producer"]["sha256"] = executable_identity["sha256"]
        _, runtime_contract = acquisition._runtime_dependency_contract(
            runtime_library_directory
        )
        policy["candidate"]["runtime_dependencies"] = runtime_contract
        policy["measurement_contracts"]["formal_abprobe_v3"][
            "producer_sha256"
        ] = digest(Path(acquisition.__file__).read_bytes())
        policy_path = root / "policy.json"
        policy_path.write_text(json.dumps(policy), encoding="utf-8")

        def run_bytes(backend: str) -> bytes:
            resource = {
                "kind": (
                    "hazkey_dictionary"
                    if backend == "hazkey"
                    else "mozc_runtime_inputs"
                ),
                "path": f"/fixture/{backend}",
                "fingerprint": "sha256:" + ("b" if backend == "hazkey" else "c") * 64,
            }
            records = []
            for row in rows:
                rss = {
                    "before_kib": 100,
                    "after_kib": 100,
                    "before_pss_kib": 1000,
                    "after_pss_kib": 1000,
                }
                if backend == "mozc":
                    rss.update(
                        {
                            "backend_before_kib": 100,
                            "backend_after_kib": 100,
                            "backend_before_pss_kib": 500,
                            "backend_after_pss_kib": 500,
                        }
                    )
                samples = [1.0] * acquisition.ITERATIONS
                records.append(
                    {
                        "schema": "hazkey.ab-probe-result.v3",
                        "id": row["id"],
                        "reading": row["reading"],
                        "category": row["category"],
                        "backend": "hazkey-server",
                        "backend_version": "fixture-v1",
                        "source_ref": source_ref,
                        "converter_backend": backend,
                        "resource": resource,
                        "top_k": acquisition.TOP_K,
                        "corpus": {
                            "sha256": corpus_sha,
                            "cases": acquisition.CASES,
                        },
                        "candidates": [row["expected"]],
                        "measurement": {
                            "warmups": acquisition.WARMUPS,
                            "iterations": acquisition.ITERATIONS,
                            "latency_ms": {
                                "median": 1.0,
                                "p95": 1.0,
                                "minimum": 1.0,
                                "maximum": 1.0,
                                "samples": samples,
                            },
                            "rss": rss,
                        },
                    }
                )
            return b"".join(
                json.dumps(record, ensure_ascii=False).encode("utf-8") + b"\n"
                for record in records
            )

        return {
            "corpus": corpus,
            "executable": executable,
            "dictionary": dictionary,
            "bundle": bundle,
            "runtime_library_directory": runtime_library_directory,
            "source_ref": source_ref,
            "policy": policy_path,
            "runs": {backend: run_bytes(backend) for backend in ("hazkey", "mozc")},
        }

    def test_acquires_exact_sequence_privately_and_writes_manifest_last(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = self._fixture(root)
            output = root / "acquisition"
            calls: list[list[str]] = []
            events: list[str] = []

            class FakeProcess:
                pid = 4242

                def __init__(self, argv: list[str], **kwargs: object) -> None:
                    self.argv = argv
                    self.stdout = kwargs["stdout"]
                    self.stderr = kwargs["stderr"]
                    self.returncode: int | None = None
                    calls.append(argv)
                    events.append(argv[13])
                    self.assertions = kwargs

                def wait(self, timeout: int | None = None) -> int:
                    self.stdout.write(fixture["runs"][self.argv[13]])
                    self.stderr.write(f"stderr:{self.argv[13]}".encode())
                    self.returncode = 0
                    self.timeout = timeout
                    return 0

                def poll(self) -> int | None:
                    return self.returncode

            original_write = acquisition._write_private

            def tracking_write(path: Path, data: bytes, *, mode: int = 0o600) -> None:
                events.append(path.name)
                original_write(path, data, mode=mode)

            with mock.patch.object(
                acquisition.subprocess, "Popen", side_effect=FakeProcess
            ) as popen, mock.patch.object(
                acquisition, "_write_private", side_effect=tracking_write
            ):
                manifest = acquisition.acquire(
                    executable=fixture["executable"],
                    runtime_library_directory=fixture["runtime_library_directory"],
                    corpus=fixture["corpus"],
                    source_ref=fixture["source_ref"],
                    hazkey_dictionary=fixture["dictionary"],
                    mozc_bundle=fixture["bundle"],
                    output_directory=output,
                    policy_path=fixture["policy"],
                )

            self.assertEqual(
                [argv[13] for argv in calls],
                [backend for _, backend in acquisition.SEQUENCE],
            )
            self.assertEqual(
                [entry["id"] for entry in manifest["entries"]],
                [run_id for run_id, _ in acquisition.SEQUENCE],
            )
            self.assertEqual(events[-1], acquisition.MANIFEST_NAME)
            self.assertEqual(popen.call_count, 8)
            for call in popen.call_args_list:
                self.assertFalse(call.kwargs["shell"])
                self.assertTrue(call.kwargs["start_new_session"])
                self.assertEqual(call.args[0][0], acquisition.SNAPSHOT_EXECUTABLE_ARG)
                self.assertEqual(call.kwargs["env"], acquisition.CHILD_ENVIRONMENT)
                self.assertEqual(Path(call.kwargs["cwd"]).parent, output.parent)
                self.assertTrue(Path(call.kwargs["cwd"]).name.startswith(".acquisition.tmp-"))
                self.assertNotIn("HOME", call.kwargs["env"])
                self.assertFalse(
                    any(
                        key.startswith("FCITX5_GRIMODEX_")
                        for key in call.kwargs["env"]
                    )
                )
            self.assertEqual(os.stat(output).st_mode & 0o777, 0o700)
            self.assertEqual((output / acquisition.SNAPSHOT_ROOT_NAME).stat().st_mode & 0o777, 0o555)
            self.assertTrue(
                all(
                    (item.stat().st_mode & 0o777) == 0o600
                    for item in output.iterdir()
                    if item.name != acquisition.SNAPSHOT_ROOT_NAME
                )
            )
            persisted = json.loads(
                (output / acquisition.MANIFEST_NAME).read_text(encoding="utf-8")
            )
            self.assertEqual(persisted, manifest)
            self.assertEqual(
                persisted["executable"],
                {
                    "source_path": str(Path(fixture["executable"]).resolve()),
                    "snapshot_path": "runtime/hazkey-server",
                    "size_bytes": Path(fixture["executable"]).stat().st_size,
                    "sha256": digest(Path(fixture["executable"]).read_bytes()),
                },
            )
            self.assertEqual(
                persisted["runtime_dependencies"]["snapshot_path"], "runtime/lib"
            )
            self.assertEqual(
                [item["path"] for item in persisted["runtime_dependencies"]["files"]],
                list(acquisition.RUNTIME_DEPENDENCY_FILENAMES),
            )
            self.assertEqual(
                (output / "runtime/hazkey-server").read_bytes(),
                Path(fixture["executable"]).read_bytes(),
            )
            self.assertRegex(persisted["host"]["fingerprint"], r"^sha256:[0-9a-f]{64}$")
            self.assertEqual(
                persisted["measurement"]["per_run_timeout_seconds"],
                acquisition.PER_RUN_TIMEOUT_SECONDS,
            )
            with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
                acquisition.acquire(
                    executable=fixture["executable"],
                    runtime_library_directory=fixture["runtime_library_directory"],
                    corpus=fixture["corpus"],
                    source_ref=fixture["source_ref"],
                    hazkey_dictionary=fixture["dictionary"],
                    mozc_bundle=fixture["bundle"],
                    output_directory=output,
                    policy_path=fixture["policy"],
                )

    def test_timeout_terminates_the_probe_process_group(self) -> None:
        process = mock.Mock()
        process.wait.side_effect = subprocess.TimeoutExpired(["probe"], 900)
        with mock.patch.object(acquisition.subprocess, "Popen", return_value=process), mock.patch.object(
            acquisition, "_terminate_process_group"
        ) as terminate, self.assertRaisesRegex(ValueError, "exceeded 900 seconds"):
            acquisition._run_probe(
                ["probe"],
                mock.Mock(),
                mock.Mock(),
                "H1",
                {"PATH": os.defpath},
                Path("/tmp"),
            )
        terminate.assert_called_once_with(process)

    def test_failed_run_leaves_no_partial_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = self._fixture(root)
            output = root / "acquisition"

            class FailedProcess:
                pid = 4242

                def __init__(self, *args: object, **kwargs: object) -> None:
                    self.returncode: int | None = None

                def wait(self, timeout: int | None = None) -> int:
                    self.returncode = 7
                    return 7

                def poll(self) -> int | None:
                    return self.returncode

            with mock.patch.object(
                acquisition.subprocess, "Popen", side_effect=FailedProcess
            ), self.assertRaisesRegex(ValueError, "H1 exited with 7"):
                acquisition.acquire(
                    executable=fixture["executable"],
                    runtime_library_directory=fixture["runtime_library_directory"],
                    corpus=fixture["corpus"],
                    source_ref=fixture["source_ref"],
                    hazkey_dictionary=fixture["dictionary"],
                    mozc_bundle=fixture["bundle"],
                    output_directory=output,
                    policy_path=fixture["policy"],
                )
            self.assertFalse(output.exists())
            self.assertEqual(list(root.glob(".acquisition.tmp-*")), [])
            self.assertFalse((root / ".acquisition.lock").exists())

    def test_policy_drift_fails_before_temp_process_output_or_lock(self) -> None:
        def drift_producer(
            fixture: dict[str, object], kwargs: dict[str, object]
        ) -> None:
            policy_path = Path(fixture["policy"])
            policy = json.loads(policy_path.read_text(encoding="utf-8"))
            policy["measurement_contracts"]["formal_abprobe_v3"][
                "producer_sha256"
            ] = "sha256:" + "0" * 64
            policy_path.write_text(json.dumps(policy), encoding="utf-8")

        def drift_source_ref(
            fixture: dict[str, object], kwargs: dict[str, object]
        ) -> None:
            kwargs["source_ref"] = "e" * 40

        def drift_executable(
            fixture: dict[str, object], kwargs: dict[str, object]
        ) -> None:
            executable = Path(fixture["executable"])
            executable.write_bytes(executable.read_bytes() + b"drift\n")
            executable.chmod(0o700)

        def drift_runtime(
            fixture: dict[str, object], kwargs: dict[str, object]
        ) -> None:
            dependency = (
                Path(fixture["runtime_library_directory"])
                / acquisition.RUNTIME_DEPENDENCY_FILENAMES[0]
            )
            dependency.write_bytes(b"runtime drift\n")
            dependency.chmod(0o755)

        cases = (
            ("producer", drift_producer, "live measurement producer SHA-256"),
            ("source-ref", drift_source_ref, "source_ref does not match"),
            ("executable", drift_executable, "live product executable identity"),
            ("runtime", drift_runtime, "live runtime dependency identities"),
        )
        for name, mutate, expected_error in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                fixture = self._fixture(root)
                output = root / "acquisition"
                kwargs: dict[str, object] = {
                    "executable": fixture["executable"],
                    "runtime_library_directory": fixture[
                        "runtime_library_directory"
                    ],
                    "corpus": fixture["corpus"],
                    "source_ref": fixture["source_ref"],
                    "hazkey_dictionary": fixture["dictionary"],
                    "mozc_bundle": fixture["bundle"],
                    "output_directory": output,
                    "policy_path": fixture["policy"],
                }
                mutate(fixture, kwargs)

                with mock.patch.object(
                    acquisition.subprocess, "Popen"
                ) as popen, mock.patch.object(
                    acquisition.tempfile,
                    "mkdtemp",
                    side_effect=AssertionError("policy preflight reached temp output"),
                ) as make_temp, self.assertRaisesRegex(ValueError, expected_error):
                    acquisition.acquire(**kwargs)

                popen.assert_not_called()
                make_temp.assert_not_called()
                self.assertFalse(output.exists())
                self.assertEqual(list(root.glob(".acquisition.tmp-*")), [])
                self.assertFalse((root / ".acquisition.lock").exists())

    def test_source_replacement_cannot_change_the_private_executable_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = self._fixture(root)
            output = root / "acquisition"
            original_bytes = Path(fixture["executable"]).read_bytes()
            replacement_bytes = b"replacement executable bytes\n"
            launched_bytes: list[bytes] = []

            class ReplacingProcess:
                pid = 4242

                def __init__(self, argv: list[str], **kwargs: object) -> None:
                    source = Path(fixture["executable"])
                    if not launched_bytes:
                        source.write_bytes(replacement_bytes)
                        source.chmod(0o700)
                    cwd = Path(kwargs["cwd"])
                    launched_bytes.append((cwd / argv[0]).read_bytes())
                    self.stdout = kwargs["stdout"]
                    self.stderr = kwargs["stderr"]
                    self.backend = argv[13]
                    self.returncode: int | None = None

                def wait(self, timeout: int | None = None) -> int:
                    self.stdout.write(fixture["runs"][self.backend])
                    self.returncode = 0
                    return 0

                def poll(self) -> int | None:
                    return self.returncode

            with mock.patch.object(
                acquisition.subprocess, "Popen", side_effect=ReplacingProcess
            ):
                manifest = acquisition.acquire(
                    executable=fixture["executable"],
                    runtime_library_directory=fixture["runtime_library_directory"],
                    corpus=fixture["corpus"],
                    source_ref=fixture["source_ref"],
                    hazkey_dictionary=fixture["dictionary"],
                    mozc_bundle=fixture["bundle"],
                    output_directory=output,
                    policy_path=fixture["policy"],
                )

            self.assertEqual(launched_bytes, [original_bytes] * len(acquisition.SEQUENCE))
            self.assertEqual(Path(fixture["executable"]).read_bytes(), replacement_bytes)
            self.assertEqual((output / "runtime/hazkey-server").read_bytes(), original_bytes)
            self.assertEqual(manifest["executable"]["sha256"], digest(original_bytes))

    def test_producer_replacement_before_publication_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = self._fixture(root)
            output = root / "acquisition"
            producer_copy = root / "run_mozc_b0_measurement.py"
            producer_copy.write_bytes(Path(acquisition.__file__).read_bytes())
            replacement_bytes = b"replacement measurement producer\n"
            calls: list[list[str]] = []

            class ReplacingProcess:
                pid = 4242

                def __init__(self, argv: list[str], **kwargs: object) -> None:
                    calls.append(argv)
                    self.stdout = kwargs["stdout"]
                    self.stderr = kwargs["stderr"]
                    self.backend = argv[13]
                    self.returncode: int | None = None

                def wait(self, timeout: int | None = None) -> int:
                    self.stdout.write(fixture["runs"][self.backend])
                    self.stderr.write(f"stderr:{self.backend}".encode())
                    if len(calls) == len(acquisition.SEQUENCE):
                        producer_copy.write_bytes(replacement_bytes)
                    self.returncode = 0
                    return 0

                def poll(self) -> int | None:
                    return self.returncode

            with mock.patch.object(
                acquisition, "__file__", str(producer_copy)
            ), mock.patch.object(
                acquisition.subprocess, "Popen", side_effect=ReplacingProcess
            ) as popen, mock.patch.object(
                acquisition, "_rename_noreplace"
            ) as publish, self.assertRaisesRegex(
                ValueError, "measurement producer changed during acquisition"
            ):
                acquisition.acquire(
                    executable=fixture["executable"],
                    runtime_library_directory=fixture["runtime_library_directory"],
                    corpus=fixture["corpus"],
                    source_ref=fixture["source_ref"],
                    hazkey_dictionary=fixture["dictionary"],
                    mozc_bundle=fixture["bundle"],
                    output_directory=output,
                    policy_path=fixture["policy"],
                )

            self.assertEqual(popen.call_count, len(acquisition.SEQUENCE))
            self.assertEqual(len(calls), len(acquisition.SEQUENCE))
            publish.assert_not_called()
            self.assertEqual(producer_copy.read_bytes(), replacement_bytes)
            self.assertFalse(output.exists())
            self.assertEqual(list(root.glob(".acquisition.tmp-*")), [])
            self.assertFalse((root / ".acquisition.lock").exists())

    def test_publication_collision_is_atomically_no_replace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = self._fixture(root)
            output = root / "acquisition"

            def run_probe(
                argv: list[str],
                raw_handle: object,
                stderr_handle: object,
                run_id: str,
                environment: dict[str, str],
                cwd: Path,
            ) -> int:
                raw_handle.write(fixture["runs"][argv[13]])
                return 0

            original_rename = acquisition._rename_noreplace

            def collide(source: Path, destination: Path) -> None:
                destination.mkdir()
                original_rename(source, destination)

            with mock.patch.object(
                acquisition, "_run_probe", side_effect=run_probe
            ), mock.patch.object(
                acquisition, "_rename_noreplace", side_effect=collide
            ), self.assertRaisesRegex(ValueError, "refusing to overwrite"):
                acquisition.acquire(
                    executable=fixture["executable"],
                    runtime_library_directory=fixture["runtime_library_directory"],
                    corpus=fixture["corpus"],
                    source_ref=fixture["source_ref"],
                    hazkey_dictionary=fixture["dictionary"],
                    mozc_bundle=fixture["bundle"],
                    output_directory=output,
                    policy_path=fixture["policy"],
                )

            self.assertTrue(output.is_dir())
            self.assertEqual(list(output.iterdir()), [])
            self.assertEqual(list(root.glob(".acquisition.tmp-*")), [])
            self.assertFalse((root / ".acquisition.lock").exists())

    def test_runtime_lib_dir_must_be_absolute_and_exact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = self._fixture(root)
            with self.assertRaisesRegex(ValueError, "absolute"):
                acquisition.acquire(
                    executable=fixture["executable"],
                    runtime_library_directory=Path("relative-runtime"),
                    corpus=fixture["corpus"],
                    source_ref=fixture["source_ref"],
                    hazkey_dictionary=fixture["dictionary"],
                    mozc_bundle=fixture["bundle"],
                    output_directory=root / "acquisition",
                    policy_path=fixture["policy"],
                )
            extra = Path(fixture["runtime_library_directory"]) / "unexpected.so"
            extra.write_bytes(b"unexpected")
            with self.assertRaisesRegex(ValueError, "exact formal B0 dependency set"):
                acquisition.acquire(
                    executable=fixture["executable"],
                    runtime_library_directory=fixture["runtime_library_directory"],
                    corpus=fixture["corpus"],
                    source_ref=fixture["source_ref"],
                    hazkey_dictionary=fixture["dictionary"],
                    mozc_bundle=fixture["bundle"],
                    output_directory=root / "acquisition",
                    policy_path=fixture["policy"],
                )


if __name__ == "__main__":
    unittest.main()
