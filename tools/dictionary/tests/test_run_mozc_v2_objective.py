from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT))

from tools.dictionary import run_mozc_b0_measurement as v1  # noqa: E402
from tools.dictionary import run_mozc_v2_objective as acquisition  # noqa: E402


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class SyntheticFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.rows: list[dict[str, str]] = []
        for category, count in acquisition.ALL_CATEGORY_COUNTS.items():
            prefix = category.replace("-", "")
            for index in range(1, count + 1):
                self.rows.append(
                    {
                        "id": f"v2-{prefix}-{index:04d}",
                        "reading": f"{prefix}よみ{index}",
                        "expected": f"{prefix}期待{index}",
                        "category": category,
                    }
                )
        self.corpus_bytes = (
            "id\treading\texpected\tcategory\n"
            + "".join(
                f"{row['id']}\t{row['reading']}\t{row['expected']}\t{row['category']}\n"
                for row in self.rows
            )
        ).encode("utf-8")

        self.executable = root / "hazkey-server"
        self.executable.write_bytes(b"synthetic v2 runner\n")
        self.executable.chmod(0o700)

        self.runtime = root / "runtime-libs"
        self.runtime.mkdir()
        for index, name in enumerate(v1.RUNTIME_DEPENDENCY_FILENAMES):
            path = self.runtime / name
            path.write_bytes(f"runtime {index} {name}\n".encode())
            path.chmod(0o755)
        _, runtime_contract = v1._runtime_dependency_contract(self.runtime)

        self.dictionary = root / "Dictionary-source"
        (self.dictionary / "nested").mkdir(parents=True)
        (self.dictionary / "a.binary").write_bytes(b"dictionary-a\n")
        (self.dictionary / "nested/b.binary").write_bytes(b"dictionary-b\n")
        dictionary_fingerprint = acquisition._directory_fingerprint(
            self.dictionary, domain="hazkey.dictionary-fingerprint.v1"
        )

        self.b0 = root / ("sha256-" + "a" * 64)
        self.b0.mkdir(mode=0o755)
        helper = b"synthetic B0 helper\n"
        data = b"synthetic B0 data\n"
        manifest = {
            "schema": "grimodex.mozc-artifact-bundle.v1",
            "artifacts": {
                "fcitx5-grimodex-mozc-helper": {
                    "size": len(helper),
                    "sha256": hashlib.sha256(helper).hexdigest(),
                },
                "mozc.data": {
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                },
            },
        }
        manifest_bytes = (
            json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n"
        ).encode()
        core = {
            "fcitx5-grimodex-mozc-helper": (helper, 0o555),
            "mozc.data": (data, 0o444),
            "manifest.json": (manifest_bytes, 0o444),
        }
        for name, (contents, mode) in core.items():
            path = self.b0 / name
            path.write_bytes(contents)
            path.chmod(mode)
        b0_fingerprint = acquisition._directory_fingerprint(
            self.b0, domain="hazkey.mozc-runtime-fingerprint.v1"
        )
        b0_freeze = acquisition.CandidateFreeze(
            generation=self.b0.name,
            helper_size_bytes=len(helper),
            helper_sha256=digest(helper),
            data_size_bytes=len(data),
            data_sha256=digest(data),
            manifest_sha256=digest(manifest_bytes),
            resource_fingerprint=b0_fingerprint,
        )
        b1_freeze = acquisition.CandidateFreeze(
            generation="sha256-" + "b" * 64,
            helper_size_bytes=23,
            helper_sha256="sha256:" + "c" * 64,
            data_size_bytes=29,
            data_sha256="sha256:" + "d" * 64,
            manifest_sha256="sha256:" + "e" * 64,
            resource_fingerprint="sha256:" + "f" * 64,
        )
        source_ref = "1" * 40
        executable_bytes = self.executable.read_bytes()
        preliminary = acquisition.FrozenContract(
            sealed_generation="sealed-v2-sha256-" + "2" * 64,
            policy_sha256="",
            manifest_sha256="",
            corpus_sha256=digest(self.corpus_bytes),
            product_source_ref=source_ref,
            executable_size_bytes=len(executable_bytes),
            executable_sha256=digest(executable_bytes),
            runtime_dependencies_integrity=runtime_contract["integrity"],
            hazkey_dictionary_fingerprint=dictionary_fingerprint,
            b0=b0_freeze,
            b1=b1_freeze,
        )
        policy = {
            "schema": acquisition.SEALED_POLICY_SCHEMA,
            "policy_id": "mozc-adoption-v2",
            "decision_tier": "formal",
            "collection": {
                "status": "ready",
                "manifest_path": acquisition.SEALED_MANIFEST_NAME,
            },
            "formal_suite": {
                "total_cases": acquisition.TOTAL_CASES,
                "quality_cases": acquisition.QUALITY_CASES,
                "quality_categories": acquisition.QUALITY_CATEGORY_COUNTS,
                "protected": {
                    "cases": 100,
                    "required_passes": 100,
                    "metric": "top1_exact",
                    "included_in_overall_quality_rates": False,
                },
            },
            "artifact_freezes": {
                "eligible_candidate_ids": ["B0", "B1"],
                "evaluation_runner": {
                    "product_source_revision": source_ref,
                    "size_bytes": len(executable_bytes),
                    "sha256": digest(executable_bytes),
                    "runtime_dependencies_integrity": runtime_contract["integrity"],
                },
                "candidates": {
                    "B0": acquisition._freeze_object(b0_freeze),
                    "B1": acquisition._freeze_object(b1_freeze),
                },
            },
        }
        policy_bytes = (
            json.dumps(policy, ensure_ascii=False, indent=2) + "\n"
        ).encode()
        manifest = {
            "schema": acquisition.SEALED_MANIFEST_SCHEMA,
            "policy": {
                "path": acquisition.SEALED_POLICY_NAME,
                "sha256": digest(policy_bytes),
            },
            "aggregate": {
                "cases": acquisition.TOTAL_CASES,
                "quality_cases": acquisition.QUALITY_CASES,
                "sha256": digest(self.corpus_bytes),
                "categories": acquisition.ALL_CATEGORY_COUNTS,
                "protected_included_in_overall_quality_rates": False,
            },
        }
        manifest_bytes = (
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
        ).encode()
        self.contract = acquisition.FrozenContract(
            **{
                **preliminary.__dict__,
                "policy_sha256": digest(policy_bytes),
                "manifest_sha256": digest(manifest_bytes),
            }
        )
        self.sealed = root / self.contract.sealed_generation
        self.sealed.mkdir(mode=0o700)
        sealed_files = {
            acquisition.SEALED_POLICY_NAME: policy_bytes,
            acquisition.SEALED_MANIFEST_NAME: manifest_bytes,
            acquisition.SEALED_CORPUS_NAME: self.corpus_bytes,
        }
        for name, contents in sealed_files.items():
            path = self.sealed / name
            path.write_bytes(contents)
            path.chmod(0o444)
        self.sealed.chmod(0o555)

    def raw_bytes(
        self,
        *,
        backend_name: str,
        converter_backend: str,
        resource_path: Path,
        ordered_rows: list[dict[str, str]] | None = None,
    ) -> bytes:
        resource = {
            "kind": (
                "hazkey_dictionary"
                if converter_backend == "hazkey"
                else "mozc_runtime_inputs"
            ),
            "path": str(resource_path.resolve()),
            "fingerprint": (
                self.contract.hazkey_dictionary_fingerprint
                if converter_backend == "hazkey"
                else self.contract.b0.resource_fingerprint
            ),
        }
        records = []
        for row in self.rows if ordered_rows is None else ordered_rows:
            rss = {
                "before_kib": 100,
                "after_kib": 101,
                "before_pss_kib": 90,
                "after_pss_kib": 91,
            }
            if converter_backend == "mozc":
                rss |= {
                    "backend_before_kib": 20,
                    "backend_after_kib": 21,
                    "backend_before_pss_kib": 19,
                    "backend_after_pss_kib": 20,
                }
            records.append(
                {
                    "schema": acquisition.RAW_SCHEMA,
                    "id": row["id"],
                    "reading": row["reading"],
                    "category": row["category"],
                    "backend": backend_name,
                    "backend_version": "fixture-v2",
                    "source_ref": self.contract.product_source_ref,
                    "converter_backend": converter_backend,
                    "resource": resource,
                    "top_k": acquisition.TOP_K,
                    "corpus": {
                        "sha256": self.contract.corpus_sha256,
                        "cases": acquisition.TOTAL_CASES,
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
                            "samples": [1.0],
                        },
                        "rss": rss,
                        "backend_diagnostics": {
                            "process_launch_count": (
                                1 if converter_backend == "mozc" else None
                            ),
                            "cleanup_failure_count": (
                                0 if converter_backend == "mozc" else None
                            ),
                        },
                    },
                }
            )
        return b"".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True).encode() + b"\n"
            for record in records
        )


class V2ObjectiveAcquisitionTests(unittest.TestCase):
    def _acquire(
        self, fixture: SyntheticFixture, output: Path
    ) -> dict[str, object]:
        return acquisition.acquire(
            executable=fixture.executable,
            runtime_library_directory=fixture.runtime.resolve(),
            sealed_generation=fixture.sealed.resolve(),
            hazkey_dictionary=fixture.dictionary.resolve(),
            b0_bundle=fixture.b0.resolve(),
            output_directory=output,
            contract=fixture.contract,
        )

    def _redirect_python_binding(
        self,
        root: Path,
        bindings: dict[str, acquisition.SourceBinding],
        key: str,
    ) -> Path:
        original = bindings[key]
        source = root / f"bound-{key}.py"
        source.write_bytes(original.data)
        source.chmod(original.mode)
        metadata = source.lstat()
        bindings[key] = acquisition.SourceBinding(
            key=key,
            source_path=source,
            repository_path=original.repository_path,
            snapshot_name=original.snapshot_name,
            data=original.data,
            mode=stat.S_IMODE(metadata.st_mode),
            device=metadata.st_dev,
            inode=metadata.st_ino,
        )
        return source

    def _run_probe(
        self,
        fixture: SyntheticFixture,
        calls: list[dict[str, object]],
        *,
        reverse_hazkey: bool = False,
    ):
        def run_probe(
            argv: list[str],
            raw_handle: object,
            stderr_handle: object,
            run_id: str,
            environment: dict[str, str],
            cwd: Path,
        ) -> int:
            converter = argv[argv.index("--converter-backend") + 1]
            backend_name = argv[argv.index("--backend-name") + 1]
            resource_option = (
                "--dictionary" if converter == "hazkey" else "--mozc-bundle"
            )
            resource = cwd / argv[argv.index(resource_option) + 1]
            ordered_rows = (
                list(reversed(fixture.rows))
                if reverse_hazkey and converter == "hazkey"
                else fixture.rows
            )
            raw_handle.write(
                fixture.raw_bytes(
                    backend_name=backend_name,
                    converter_backend=converter,
                    resource_path=resource,
                    ordered_rows=ordered_rows,
                )
            )
            stderr_handle.write(f"stderr:{run_id}".encode())
            calls.append(
                {
                    "argv": argv,
                    "run_id": run_id,
                    "environment": environment,
                    "cwd": cwd,
                }
            )
            return 0

        return run_probe

    def test_acquires_hazkey_then_b0_and_publishes_complete_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = SyntheticFixture(root)
            output = root / "objective"
            calls: list[dict[str, object]] = []
            with mock.patch.object(
                acquisition,
                "_run_probe",
                side_effect=self._run_probe(fixture, calls),
            ), mock.patch.object(
                v1,
                "_host_contract",
                return_value={
                    "fingerprint": "sha256:" + "9" * 64,
                    "effective_cpu_affinity": sorted(os.sched_getaffinity(0)),
                },
            ):
                manifest = acquisition.acquire(
                    executable=fixture.executable,
                    runtime_library_directory=fixture.runtime.resolve(),
                    sealed_generation=fixture.sealed.resolve(),
                    hazkey_dictionary=fixture.dictionary.resolve(),
                    b0_bundle=fixture.b0.resolve(),
                    output_directory=output,
                    contract=fixture.contract,
                )

            self.assertEqual([call["run_id"] for call in calls], ["H0", "B0"])
            self.assertEqual(
                [entry["converter_backend"] for entry in manifest["entries"]],
                ["hazkey", "mozc"],
            )
            for call in calls:
                self.assertEqual(call["environment"], acquisition.CHILD_ENVIRONMENT)
                self.assertTrue(Path(call["cwd"]).name.startswith(".objective.tmp-"))
                argv = call["argv"]
                self.assertEqual(argv[0], acquisition.SNAPSHOT_EXECUTABLE_ARG)
                self.assertEqual(argv[argv.index("--warmups") + 1], "0")
                self.assertEqual(argv[argv.index("--iterations") + 1], "1")
                self.assertEqual(argv[argv.index("--top-k") + 1], "10")
            self.assertEqual(manifest["candidates"]["B0"]["status"], "evaluated")
            self.assertEqual(
                manifest["candidates"]["B1"]["status"],
                "frozen_not_evaluated",
            )
            self.assertEqual(
                manifest["candidates"]["B1"]["generation"],
                fixture.contract.b1.generation,
            )
            self.assertTrue(manifest["objective_quality"]["passed"])
            self.assertEqual(
                {item["id"] for item in manifest["python_sources"]["files"]},
                set(acquisition.PYTHON_SOURCE_BINDINGS),
            )
            for item in manifest["python_sources"]["files"]:
                snapshot = output / item["snapshot_path"]
                self.assertEqual(digest(snapshot.read_bytes()), item["sha256"])
                self.assertEqual(snapshot.stat().st_mode & 0o777, 0o444)
            self.assertEqual(
                manifest["measurement"]["per_run_timeout_seconds"],
                acquisition.PER_RUN_TIMEOUT_SECONDS,
            )
            objective = json.loads(
                (output / acquisition.OBJECTIVE_REPORT_NAME).read_text()
            )
            self.assertEqual(objective["backends"]["Hazkey"]["quality_cases"], 1260)
            self.assertEqual(objective["backends"]["B0"]["top1_hits"], 1260)
            self.assertEqual(objective["backends"]["B0"]["protected"]["top1_hits"], 100)
            self.assertEqual(objective["deltas"]["top1_hits"], 0)
            self.assertEqual(objective["deltas"]["top10_hits"], 0)
            self.assertEqual(objective["candidate"]["id"], "B0")
            self.assertEqual(
                objective["corpus"]["sha256"], fixture.contract.corpus_sha256
            )
            self.assertEqual(
                objective["raw_runs"],
                {
                    entry["id"]: entry["raw"]["sha256"]
                    for entry in manifest["entries"]
                },
            )
            self.assertEqual((output / "H0.jsonl").read_bytes().count(b"\n"), 1360)
            self.assertEqual((output / "B0.jsonl").read_bytes().count(b"\n"), 1360)
            self.assertEqual(
                (output / "runtime/hazkey-server").stat().st_mode & 0o777,
                0o555,
            )
            self.assertEqual(
                (output / "inputs/Dictionary").stat().st_mode & 0o777,
                0o555,
            )
            self.assertEqual(output.stat().st_mode & 0o777, 0o555)
            self.assertEqual((output / "H0.jsonl").stat().st_mode & 0o777, 0o444)
            self.assertEqual(
                (
                    output
                    / "inputs/B0"
                    / fixture.contract.b0.generation
                    / "fcitx5-grimodex-mozc-helper"
                ).stat().st_mode
                & 0o777,
                0o555,
            )
            self.assertEqual(
                (
                    output
                    / "inputs/B0"
                    / fixture.contract.b0.generation
                ).stat().st_mode
                & 0o777,
                0o755,
            )
            persisted = json.loads(
                (output / acquisition.ACQUISITION_MANIFEST_NAME).read_text()
            )
            self.assertEqual(persisted, manifest)
            self.assertEqual(
                persisted["integrity"],
                digest(
                    acquisition._canonical_json(
                        {
                            key: value
                            for key, value in persisted.items()
                            if key != "integrity"
                        }
                    )
                ),
            )
            with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
                acquisition.acquire(
                    executable=fixture.executable,
                    runtime_library_directory=fixture.runtime.resolve(),
                    sealed_generation=fixture.sealed.resolve(),
                    hazkey_dictionary=fixture.dictionary.resolve(),
                    b0_bundle=fixture.b0.resolve(),
                    output_directory=output,
                    contract=fixture.contract,
                )

    def test_raw_case_order_mismatch_leaves_no_partial_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = SyntheticFixture(root)
            output = root / "objective"
            calls: list[dict[str, object]] = []
            with mock.patch.object(
                acquisition,
                "_run_probe",
                side_effect=self._run_probe(
                    fixture, calls, reverse_hazkey=True
                ),
            ), self.assertRaisesRegex(ValueError, "case IDs or order"):
                acquisition.acquire(
                    executable=fixture.executable,
                    runtime_library_directory=fixture.runtime.resolve(),
                    sealed_generation=fixture.sealed.resolve(),
                    hazkey_dictionary=fixture.dictionary.resolve(),
                    b0_bundle=fixture.b0.resolve(),
                    output_directory=output,
                    contract=fixture.contract,
                )
            self.assertFalse(output.exists())
            self.assertEqual(list(root.glob(".objective.tmp-*")), [])
            self.assertFalse((root / ".objective.lock").exists())

    def test_all_input_roots_reject_leaf_symlinks_before_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = SyntheticFixture(root)
            real = {
                "executable": fixture.executable,
                "runtime_library_directory": fixture.runtime.resolve(),
                "sealed_generation": fixture.sealed.resolve(),
                "hazkey_dictionary": fixture.dictionary.resolve(),
                "b0_bundle": fixture.b0.resolve(),
            }
            for field, target in real.items():
                with self.subTest(field=field):
                    link = root / f"link-{field}"
                    link.symlink_to(target, target_is_directory=target.is_dir())
                    arguments = dict(real)
                    arguments[field] = link
                    with self.assertRaisesRegex(ValueError, "root must not be a symlink"):
                        acquisition.acquire(
                            **arguments,
                            output_directory=root / f"output-{field}",
                            contract=fixture.contract,
                        )

    def test_relative_input_root_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = SyntheticFixture(root)
            with self.assertRaisesRegex(ValueError, "must be an absolute path"):
                acquisition.acquire(
                    executable=Path(fixture.executable.name),
                    runtime_library_directory=fixture.runtime.resolve(),
                    sealed_generation=fixture.sealed.resolve(),
                    hazkey_dictionary=fixture.dictionary.resolve(),
                    b0_bundle=fixture.b0.resolve(),
                    output_directory=root / "objective",
                    contract=fixture.contract,
                )

    def test_dependency_and_producer_drift_leave_no_evidence(self) -> None:
        for key in ("probe_summarizer", "producer"):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                fixture = SyntheticFixture(root)
                output = root / "objective"
                bindings = acquisition._capture_python_sources()
                redirected = self._redirect_python_binding(root, bindings, key)
                calls: list[dict[str, object]] = []
                base_probe = self._run_probe(fixture, calls)

                def drift(*args: object, **kwargs: object) -> int:
                    result = base_probe(*args, **kwargs)
                    if args[3] == "H0":
                        redirected.write_bytes(redirected.read_bytes() + b"# drift\n")
                    return result

                with mock.patch.object(
                    acquisition, "_capture_python_sources", return_value=bindings
                ), mock.patch.object(
                    acquisition, "_run_probe", side_effect=drift
                ), self.assertRaisesRegex(ValueError, "Python source .* changed"):
                    self._acquire(fixture, output)
                self.assertFalse(output.exists())
                self.assertEqual(list(root.glob(".objective.tmp-*")), [])
                self.assertFalse((root / ".objective.lock").exists())

    def test_input_mutation_leaves_no_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = SyntheticFixture(root)
            output = root / "objective"
            calls: list[dict[str, object]] = []
            base_probe = self._run_probe(fixture, calls)

            def mutate(*args: object, **kwargs: object) -> int:
                result = base_probe(*args, **kwargs)
                if args[3] == "H0":
                    source = fixture.dictionary / "a.binary"
                    source.write_bytes(source.read_bytes() + b"changed\n")
                return result

            with mock.patch.object(
                acquisition, "_run_probe", side_effect=mutate
            ), self.assertRaisesRegex(ValueError, "dictionary source changed"):
                self._acquire(fixture, output)
            self.assertFalse(output.exists())
            self.assertEqual(list(root.glob(".objective.tmp-*")), [])
            self.assertFalse((root / ".objective.lock").exists())

    def test_tree_tamper_before_publication_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = SyntheticFixture(root)
            output = root / "objective"
            calls: list[dict[str, object]] = []
            original_freeze = acquisition._freeze_tree

            def tamper(evidence_root: Path) -> None:
                (evidence_root / "H0.jsonl").write_bytes(b"tampered\n")
                original_freeze(evidence_root)

            with mock.patch.object(
                acquisition,
                "_run_probe",
                side_effect=self._run_probe(fixture, calls),
            ), mock.patch.object(
                acquisition, "_freeze_tree", side_effect=tamper
            ), self.assertRaisesRegex(ValueError, "pre-publication tree mismatch"):
                self._acquire(fixture, output)
            self.assertFalse(output.exists())
            self.assertEqual(list(root.glob(".objective.tmp-*")), [])

    def test_post_publication_verification_failure_reports_committed_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = SyntheticFixture(root)
            output = root / "objective"
            calls: list[dict[str, object]] = []
            original_verify = acquisition._verify_tree

            def fail_after_publish(
                evidence_root: Path,
                expected: dict[str, acquisition.TreeEntry],
                context: str,
            ) -> None:
                if context == "post-publication":
                    raise ValueError("injected post-publication failure")
                original_verify(evidence_root, expected, context)

            with mock.patch.object(
                acquisition,
                "_run_probe",
                side_effect=self._run_probe(fixture, calls),
            ), mock.patch.object(
                acquisition, "_verify_tree", side_effect=fail_after_publish
            ), self.assertRaises(acquisition.EvidenceCommittedError) as raised:
                self._acquire(fixture, output)
            self.assertTrue(raised.exception.committed)
            self.assertIn("evidence committed", str(raised.exception))
            self.assertTrue(output.is_dir())
            self.assertEqual(output.stat().st_mode & 0o777, 0o555)
            self.assertFalse((root / ".objective.lock").exists())
            self.assertEqual(list(root.glob(".objective.tmp-*")), [])

    def test_durability_fsync_failure_reports_committed_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = SyntheticFixture(root)
            output = root / "objective"
            calls: list[dict[str, object]] = []
            original_fsync = acquisition.os.fsync
            parent_identity = root.stat().st_dev, root.stat().st_ino

            def fail_committed_parent(descriptor: int) -> None:
                metadata = os.fstat(descriptor)
                if (metadata.st_dev, metadata.st_ino) == parent_identity and output.exists():
                    raise OSError("injected durability failure")
                original_fsync(descriptor)

            with mock.patch.object(
                acquisition,
                "_run_probe",
                side_effect=self._run_probe(fixture, calls),
            ), mock.patch.object(
                acquisition.os, "fsync", side_effect=fail_committed_parent
            ), self.assertRaises(acquisition.EvidenceCommittedError) as raised:
                self._acquire(fixture, output)
            self.assertIn("durability failure", str(raised.exception))
            self.assertTrue(output.is_dir())
            self.assertFalse((root / ".objective.lock").exists())

    def test_replaced_lock_is_preserved_and_reports_committed_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = SyntheticFixture(root)
            output = root / "objective"
            lock = root / ".objective.lock"
            calls: list[dict[str, object]] = []
            original_verify = acquisition._verify_tree
            foreign_contents = b"foreign lock\n"

            def replace_lock(
                evidence_root: Path,
                expected: dict[str, acquisition.TreeEntry],
                context: str,
            ) -> None:
                original_verify(evidence_root, expected, context)
                if context == "post-publication":
                    lock.unlink()
                    lock.write_bytes(foreign_contents)

            with mock.patch.object(
                acquisition,
                "_run_probe",
                side_effect=self._run_probe(fixture, calls),
            ), mock.patch.object(
                acquisition, "_verify_tree", side_effect=replace_lock
            ), self.assertRaises(acquisition.EvidenceCommittedError) as raised:
                self._acquire(fixture, output)
            self.assertIn("lock identity changed", str(raised.exception))
            self.assertTrue(output.is_dir())
            self.assertEqual(lock.read_bytes(), foreign_contents)

    def test_late_parent_swap_after_final_fsync_reports_committed_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = SyntheticFixture(root)
            publish = root / "publish"
            publish.mkdir()
            moved = root / "publish-moved"
            output = publish / "objective"
            lock = publish / ".objective.lock"
            calls: list[dict[str, object]] = []
            original_fsync = acquisition.os.fsync
            parent_identity = publish.stat().st_dev, publish.stat().st_ino
            swapped = False

            def swap_after_final_fsync(descriptor: int) -> None:
                nonlocal swapped
                metadata = os.fstat(descriptor)
                original_fsync(descriptor)
                if (
                    not swapped
                    and (metadata.st_dev, metadata.st_ino) == parent_identity
                    and output.exists()
                    and not lock.exists()
                ):
                    publish.rename(moved)
                    publish.mkdir()
                    swapped = True

            with mock.patch.object(
                acquisition,
                "_run_probe",
                side_effect=self._run_probe(fixture, calls),
            ), mock.patch.object(
                acquisition.os, "fsync", side_effect=swap_after_final_fsync
            ), self.assertRaises(acquisition.EvidenceCommittedError) as raised:
                self._acquire(fixture, output)
            self.assertIn("parent path identity changed", str(raised.exception))
            self.assertFalse(output.exists())
            self.assertTrue((moved / "objective").is_dir())

    def test_parent_close_failure_reports_committed_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = SyntheticFixture(root)
            output = root / "objective"
            lock = root / ".objective.lock"
            calls: list[dict[str, object]] = []
            original_close = acquisition.os.close
            parent_identity = root.stat().st_dev, root.stat().st_ino
            failed = False

            def fail_parent_close(descriptor: int) -> None:
                nonlocal failed
                metadata = os.fstat(descriptor)
                if (
                    not failed
                    and (metadata.st_dev, metadata.st_ino) == parent_identity
                    and output.exists()
                    and not lock.exists()
                ):
                    failed = True
                    original_close(descriptor)
                    raise OSError("injected parent close failure")
                original_close(descriptor)

            with mock.patch.object(
                acquisition,
                "_run_probe",
                side_effect=self._run_probe(fixture, calls),
            ), mock.patch.object(
                acquisition.os, "close", side_effect=fail_parent_close
            ), self.assertRaises(acquisition.EvidenceCommittedError) as raised:
                self._acquire(fixture, output)
            self.assertIn("parent close failure", str(raised.exception))
            self.assertTrue(output.is_dir())
            self.assertFalse(lock.exists())

    def test_output_parent_identity_swap_is_rejected_via_pinned_dirfd(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = SyntheticFixture(root)
            publish = root / "publish"
            publish.mkdir()
            moved = root / "publish-moved"
            output = publish / "objective"
            calls: list[dict[str, object]] = []
            original_freeze = acquisition._freeze_tree

            def swap_after_freeze(evidence_root: Path) -> None:
                original_freeze(evidence_root)
                publish.rename(moved)
                publish.mkdir()

            with mock.patch.object(
                acquisition,
                "_run_probe",
                side_effect=self._run_probe(fixture, calls),
            ), mock.patch.object(
                acquisition, "_freeze_tree", side_effect=swap_after_freeze
            ), self.assertRaisesRegex(ValueError, "parent path identity changed"):
                self._acquire(fixture, output)
            self.assertFalse(output.exists())
            self.assertEqual(list(moved.iterdir()), [])

    def test_publication_collision_is_no_replace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = SyntheticFixture(root)
            output = root / "objective"
            calls: list[dict[str, object]] = []
            original = acquisition._rename_noreplace_at

            def collide(directory_fd: int, source: str, destination: str) -> None:
                os.mkdir(destination, dir_fd=directory_fd)
                original(directory_fd, source, destination)

            with mock.patch.object(
                acquisition,
                "_run_probe",
                side_effect=self._run_probe(fixture, calls),
            ), mock.patch.object(
                acquisition, "_rename_noreplace_at", side_effect=collide
            ), self.assertRaisesRegex(ValueError, "refusing to overwrite"):
                acquisition.acquire(
                    executable=fixture.executable,
                    runtime_library_directory=fixture.runtime.resolve(),
                    sealed_generation=fixture.sealed.resolve(),
                    hazkey_dictionary=fixture.dictionary.resolve(),
                    b0_bundle=fixture.b0.resolve(),
                    output_directory=output,
                    contract=fixture.contract,
                )
            self.assertTrue(output.is_dir())
            self.assertEqual(list(output.iterdir()), [])
            self.assertEqual(list(root.glob(".objective.tmp-*")), [])
            self.assertFalse((root / ".objective.lock").exists())

    def test_integer_gate_boundaries_and_protected_failure(self) -> None:
        def report(
            *,
            top1_deltas: dict[str, int] | None = None,
            top10_delta: int = 0,
            protected: int = 100,
        ) -> tuple[dict[str, object], dict[str, object]]:
            top1_deltas = top1_deltas or {
                category: 0 for category in acquisition.QUALITY_CATEGORY_COUNTS
            }
            hazkey_categories: dict[str, dict[str, int]] = {}
            b0_categories: dict[str, dict[str, int]] = {}
            remaining_top10 = top10_delta
            for category, count in acquisition.QUALITY_CATEGORY_COUNTS.items():
                baseline_top1 = count - 30
                category_top10_delta = max(-count, remaining_top10)
                remaining_top10 -= category_top10_delta
                hazkey_categories[category] = {
                    "total": count,
                    "top1": baseline_top1,
                    "top10": count,
                }
                b0_categories[category] = {
                    "total": count,
                    "top1": baseline_top1 + top1_deltas[category],
                    "top10": count + category_top10_delta,
                }
            hazkey_categories["protected"] = {
                "total": 100,
                "top1": 100,
                "top10": 100,
            }
            b0_categories["protected"] = {
                "total": 100,
                "top1": protected,
                "top10": protected,
            }
            return (
                {"by_category": hazkey_categories},
                {"by_category": b0_categories},
            )

        deltas = {
            "technical-mixed": -24,
            "proper-noun": -20,
            "colloquial": -20,
            "homophone-context": -20,
            "long-structural": -16,
            "grimodex-regression": 0,
        }
        binding = {
            "corpus_sha256": "sha256:" + "1" * 64,
            "hazkey_resource_fingerprint": "sha256:" + "2" * 64,
            "candidate_id": "B0",
            "candidate_resource_fingerprint": "sha256:" + "3" * 64,
            "raw_run_sha256": {
                "H0": "sha256:" + "4" * 64,
                "B0": "sha256:" + "5" * 64,
            },
        }
        hazkey, b0 = report(top1_deltas=deltas, top10_delta=-151)
        exact = acquisition.build_objective_report(hazkey, b0, **binding)
        self.assertTrue(exact["passed"])
        self.assertEqual(exact["deltas"]["top1_hits"], -100)
        self.assertEqual(exact["deltas"]["top10_hits"], -151)

        below = dict(deltas)
        below["long-structural"] = -17
        hazkey, b0 = report(top1_deltas=below, top10_delta=-151)
        self.assertFalse(
            acquisition.build_objective_report(hazkey, b0, **binding)["passed"]
        )

        category_fail = dict(deltas)
        category_fail["technical-mixed"] = -25
        category_fail["grimodex-regression"] = 1
        hazkey, b0 = report(top1_deltas=category_fail, top10_delta=-151)
        category_result = acquisition.build_objective_report(
            hazkey, b0, **binding
        )
        self.assertEqual(category_result["deltas"]["top1_hits"], -100)
        self.assertFalse(category_result["passed"])

        hazkey, b0 = report(top1_deltas=deltas, top10_delta=-151, protected=99)
        protected_result = acquisition.build_objective_report(
            hazkey, b0, **binding
        )
        self.assertFalse(protected_result["passed"])
        self.assertEqual(
            protected_result["next_step"],
            "complete-b0-formal-evidence-before-b1",
        )


if __name__ == "__main__":
    unittest.main()
