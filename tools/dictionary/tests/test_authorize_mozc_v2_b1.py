from __future__ import annotations

from contextlib import contextmanager, ExitStack
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT))

from tools.dictionary import authorize_mozc_v2_b1 as authorizer  # noqa: E402
from tools.dictionary import evaluate_mozc_adoption_v2_gate as gate  # noqa: E402
from tools.dictionary import run_mozc_b0_measurement as v1  # noqa: E402
from tools.dictionary import run_mozc_v2_objective as acquisition  # noqa: E402
from tools.dictionary.tests.test_run_mozc_v2_objective import (  # noqa: E402
    SyntheticFixture,
)


POLICY_FIXTURE = (
    REPOSITORY_ROOT
    / "hazkey-server/Tests/grimodex-spike/Fixtures/mozc-adoption-v2/"
    "formal-gate-policy.json"
)


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def rewrite_json(path: Path, value: dict[str, object]) -> None:
    path.chmod(0o644)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o444)


class B1AuthorizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        cls.fixture = SyntheticFixture(cls.root)
        cls.pass_root = cls._acquire("pass-evidence", fail_protected=False)
        cls.fail_root = cls._acquire("fail-evidence", fail_protected=True)
        cls.other_fail_root = cls._acquire("other-fail-evidence", fail_protected=True)
        cls.real_policy = gate.load_policy(POLICY_FIXTURE)
        cls.pass_policy = cls._policy_for(cls.pass_root)
        cls.fail_policy = cls._policy_for(cls.fail_root)

    @classmethod
    def _policy_for(cls, evidence_root: Path):
        manifest = json.loads(
            (evidence_root / acquisition.ACQUISITION_MANIFEST_NAME).read_text()
        )
        source_hashes = {
            item["id"]: item["sha256"]
            for item in manifest["python_sources"]["files"]
        }
        tree = authorizer._capture_evidence(evidence_root)
        raw_hashes = {
            run_id: digest((evidence_root / f"{run_id}.jsonl").read_bytes())
            for run_id in ("H0", "B0")
        }
        return replace(
            cls.real_policy,
            policy_sha256=digest(
                f"synthetic B1 authorization policy:{evidence_root.name}".encode()
            ),
            manifest_sha256=cls.fixture.contract.manifest_sha256,
            corpus_sha256=cls.fixture.contract.corpus_sha256,
            source_policy_sha256=cls.fixture.contract.policy_sha256,
            candidate_resource_fingerprints={
                "B0": cls.fixture.contract.b0.resource_fingerprint,
                "B1": cls.fixture.contract.b1.resource_fingerprint,
            },
            hazkey_dictionary_fingerprint=(
                cls.fixture.contract.hazkey_dictionary_fingerprint
            ),
            trusted_b0_producer={
                "path": acquisition.PYTHON_SOURCE_BINDINGS["producer"][1],
                "sha256": source_hashes["producer"],
            },
            trusted_b0_python_source_sha256=source_hashes,
            trusted_b0_acquisition_manifest_sha256=digest(
                (evidence_root / acquisition.ACQUISITION_MANIFEST_NAME).read_bytes()
            ),
            trusted_b0_acquisition_manifest_integrity=manifest["integrity"],
            trusted_b0_acquisition_tree_digest=tree.tree_digest,
            trusted_b0_raw_run_sha256=raw_hashes,
            b1_raw_resource_suffixes={
                "H0": "inputs/Dictionary",
                "B0": f"inputs/B0/{cls.fixture.contract.b0.generation}",
            },
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    @classmethod
    def _acquire(cls, name: str, *, fail_protected: bool) -> Path:
        output = cls.root / name

        def run_probe(
            argv: list[str],
            raw_handle: object,
            stderr_handle: object,
            run_id: str,
            environment: dict[str, str],
            cwd: Path,
        ) -> int:
            del environment
            converter = argv[argv.index("--converter-backend") + 1]
            backend = argv[argv.index("--backend-name") + 1]
            option = "--dictionary" if converter == "hazkey" else "--mozc-bundle"
            resource = cwd / argv[argv.index(option) + 1]
            raw = cls.fixture.raw_bytes(
                backend_name=backend,
                converter_backend=converter,
                resource_path=resource,
            )
            if fail_protected and converter == "mozc":
                changed: list[bytes] = []
                for line in raw.splitlines():
                    payload = json.loads(line)
                    if payload["category"] == "protected":
                        payload["candidates"] = ["incorrect-protected-surface"]
                    changed.append(
                        json.dumps(
                            payload, ensure_ascii=False, sort_keys=True
                        ).encode("utf-8")
                    )
                raw = b"\n".join(changed) + b"\n"
            raw_handle.write(raw)
            stderr_handle.write(f"stderr:{run_id}".encode())
            return 0

        with mock.patch.object(
            acquisition, "_run_probe", side_effect=run_probe
        ), mock.patch.object(
            v1,
            "_host_contract",
            return_value={
                "fingerprint": "sha256:" + "9" * 64,
                "effective_cpu_affinity": sorted(os.sched_getaffinity(0)),
            },
        ):
            acquisition.acquire(
                executable=cls.fixture.executable,
                runtime_library_directory=cls.fixture.runtime.resolve(),
                sealed_generation=cls.fixture.sealed.resolve(),
                hazkey_dictionary=cls.fixture.dictionary.resolve(),
                b0_bundle=cls.fixture.b0.resolve(),
                output_directory=output,
                contract=cls.fixture.contract,
            )
        return output

    @contextmanager
    def trusted_contract(self, policy=None):
        contract = self.fixture.contract
        selected_policy = self.fail_policy if policy is None else policy
        with ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(gate, "load_policy", return_value=selected_policy)
            )
            stack.enter_context(
                mock.patch.object(gate, "GENERATION", contract.sealed_generation)
            )
            stack.enter_context(
                mock.patch.multiple(
                    authorizer,
                    PRODUCT_SOURCE_REF=contract.product_source_ref,
                    RUNNER_SHA256=contract.executable_sha256,
                    RUNNER_SIZE=contract.executable_size_bytes,
                    RUNTIME_INTEGRITY=contract.runtime_dependencies_integrity,
                )
            )
            yield

    def copy_evidence(self, source: Path, parent: Path) -> Path:
        destination = parent / source.name
        shutil.copytree(source, destination)
        return destination

    def refresh_raw_chain(self, root: Path, run_id: str) -> None:
        raw_hash = digest((root / f"{run_id}.jsonl").read_bytes())
        objective_path = root / acquisition.OBJECTIVE_REPORT_NAME
        objective = json.loads(objective_path.read_text())
        objective["raw_runs"][run_id] = raw_hash
        objective_base = {
            key: value for key, value in objective.items() if key != "integrity"
        }
        objective["integrity"] = digest(
            acquisition._canonical_json(objective_base)
        )
        rewrite_json(objective_path, objective)
        manifest_path = root / acquisition.ACQUISITION_MANIFEST_NAME
        manifest = json.loads(manifest_path.read_text())
        entry = next(item for item in manifest["entries"] if item["id"] == run_id)
        entry["raw"]["sha256"] = raw_hash
        manifest["objective_quality"]["sha256"] = digest(objective_path.read_bytes())
        manifest_base = {
            key: value for key, value in manifest.items() if key != "integrity"
        }
        manifest["integrity"] = digest(
            acquisition._canonical_json(manifest_base)
        )
        rewrite_json(manifest_path, manifest)

    def recompute_complete_chain(self, root: Path) -> None:
        rows = authorizer._load_corpus(
            (root / "inputs/sealed/formal-corpus.tsv").read_bytes()
        )
        loaded = {
            run_id: authorizer.summarize_ab_probe.load_run_bytes(
                (root / f"{run_id}.jsonl").read_bytes(), f"{run_id}.jsonl"
            )
            for run_id in ("H0", "B0")
        }
        reports = {
            run_id: authorizer._quality_report(rows, loaded[run_id])
            for run_id in ("H0", "B0")
        }
        raw_hashes = {
            run_id: digest((root / f"{run_id}.jsonl").read_bytes())
            for run_id in ("H0", "B0")
        }
        objective, _checks = authorizer._objective(
            reports["H0"], reports["B0"], raw_hashes, self.fail_policy
        )
        for run_id in ("H0", "B0"):
            rewrite_json(root / f"{run_id}.quality.json", reports[run_id])
        objective_path = root / acquisition.OBJECTIVE_REPORT_NAME
        rewrite_json(objective_path, objective)
        manifest_path = root / acquisition.ACQUISITION_MANIFEST_NAME
        manifest = json.loads(manifest_path.read_text())
        for entry in manifest["entries"]:
            run_id = entry["id"]
            entry["raw"]["sha256"] = raw_hashes[run_id]
            entry["quality_report"]["sha256"] = digest(
                (root / f"{run_id}.quality.json").read_bytes()
            )
        manifest["objective_quality"] = {
            "path": acquisition.OBJECTIVE_REPORT_NAME,
            "sha256": digest(objective_path.read_bytes()),
            "passed": objective["passed"],
            "next_step": objective["next_step"],
        }
        base = {
            key: value for key, value in manifest.items() if key != "integrity"
        }
        manifest["integrity"] = digest(acquisition._canonical_json(base))
        rewrite_json(manifest_path, manifest)

    def test_legacy_protected_failure_is_diagnostic_and_cannot_authorize(self) -> None:
        with self.trusted_contract():
            result = authorizer.evaluate_early_rejection(
                POLICY_FIXTURE, self.fail_root
            )
            self.assertFalse(result["authorized"])
            self.assertEqual(result["scope"], "B1-evaluation-only")
            self.assertIs(result["formal_adoption_allowed"], False)
            self.assertEqual(result["recomputed"]["failed_check_ids"], [])
            self.assertEqual(
                result["recomputed"]["diagnostic_failed_check_ids"],
                ["protected-top1"],
            )
            self.assertFalse(result["recomputed"]["policy_revision_ready"])
            encoded = authorizer.encode_authorization(result)
            with self.assertRaisesRegex(ValueError, "not authorized"):
                authorizer.verify_b1_authorization(
                    POLICY_FIXTURE, self.fail_root, encoded
                )

    def test_status_only_ready_cannot_enable_legacy_check_ids(self) -> None:
        status_only = replace(
            self.fail_policy,
            protected_input_protocol_status="ready",
            mixed_input_protocol_status="ready",
            b1_authorization_status="ready",
        )
        with self.trusted_contract(status_only):
            result = authorizer.evaluate_early_rejection(
                POLICY_FIXTURE, self.fail_root
            )
        self.assertFalse(result["authorized"])
        self.assertFalse(result["recomputed"]["policy_revision_ready"])
        self.assertEqual(result["recomputed"]["checks"], [])

    def test_legacy_diagnostic_check_ids_cannot_be_promoted_to_valid(self) -> None:
        for check_id in ("protected-top1", "quality-top10-delta"):
            with self.subTest(check_id=check_id):
                promoted = replace(
                    self.fail_policy,
                    protected_input_protocol_status="ready",
                    mixed_input_protocol_status="ready",
                    b1_authorization_status="ready",
                    b1_authorization_validity_precondition=(
                        "reviewed-interaction-check-set:synthetic-unit-test-v1"
                    ),
                    b1_valid_mandatory_check_ids=(check_id,),
                )
                with self.trusted_contract(promoted), mock.patch.object(
                    authorizer, "_capture_evidence"
                ) as capture_evidence, self.assertRaisesRegex(
                    ValueError, "overlap legacy diagnostic check IDs"
                ):
                    authorizer.evaluate_early_rejection(
                        POLICY_FIXTURE, self.fail_root
                    )
                capture_evidence.assert_not_called()

    def test_all_objective_checks_pass_is_valid_but_not_authorized(self) -> None:
        with self.trusted_contract(self.pass_policy):
            result = authorizer.evaluate_early_rejection(
                POLICY_FIXTURE, self.pass_root
            )
            self.assertFalse(result["authorized"])
            self.assertEqual(result["recomputed"]["failed_check_ids"], [])
            with self.assertRaisesRegex(ValueError, "not authorized"):
                authorizer.verify_b1_authorization(
                    POLICY_FIXTURE,
                    self.pass_root,
                    authorizer.encode_authorization(result),
                )

    def test_objective_and_quality_aggregates_are_not_credentials(self) -> None:
        for filename in ("B0.quality.json", acquisition.OBJECTIVE_REPORT_NAME):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as tmp:
                root = self.copy_evidence(self.pass_root, Path(tmp))
                path = root / filename
                payload = json.loads(path.read_text())
                if filename == "B0.quality.json":
                    payload["top1_hits"] = 0
                else:
                    payload["passed"] = False
                    payload["gates"][0]["passed"] = False
                    base = {
                        key: value
                        for key, value in payload.items()
                        if key != "integrity"
                    }
                    payload["integrity"] = digest(
                        acquisition._canonical_json(base)
                    )
                rewrite_json(path, payload)
                manifest_path = root / acquisition.ACQUISITION_MANIFEST_NAME
                manifest = json.loads(manifest_path.read_text())
                if filename == "B0.quality.json":
                    manifest["entries"][1]["quality_report"]["sha256"] = digest(
                        path.read_bytes()
                    )
                else:
                    manifest["objective_quality"].update(
                        {
                            "sha256": digest(path.read_bytes()),
                            "passed": False,
                        }
                    )
                base = {
                    key: value
                    for key, value in manifest.items()
                    if key != "integrity"
                }
                manifest["integrity"] = digest(acquisition._canonical_json(base))
                rewrite_json(manifest_path, manifest)
                changed_policy = self._policy_for(root)
                with self.trusted_contract(changed_policy), self.assertRaisesRegex(
                    ValueError, "differs from raw recomputation"
                ):
                    authorizer.evaluate_early_rejection(POLICY_FIXTURE, root)

    def test_raw_order_and_stale_path_tampering_are_rejected(self) -> None:
        for mutation in ("order", "path"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                root = self.copy_evidence(self.fail_root, Path(tmp))
                raw_path = root / "H0.jsonl"
                lines = raw_path.read_bytes().splitlines()
                if mutation == "order":
                    lines[0], lines[1] = lines[1], lines[0]
                else:
                    rewritten = []
                    for line in lines:
                        payload = json.loads(line)
                        payload["resource"]["path"] = (
                            "/tmp/.unrelated.tmp-0000000000000000/inputs/Dictionary"
                        )
                        rewritten.append(
                            json.dumps(
                                payload, ensure_ascii=False, sort_keys=True
                            ).encode()
                        )
                    lines = rewritten
                raw_path.chmod(0o644)
                raw_path.write_bytes(b"\n".join(lines) + b"\n")
                raw_path.chmod(0o444)
                self.refresh_raw_chain(root, "H0")
                expected = "case IDs or order|temporary basename"
                changed_policy = self._policy_for(root)
                with self.trusted_contract(changed_policy), self.assertRaisesRegex(
                    ValueError, expected
                ):
                    authorizer.evaluate_early_rejection(POLICY_FIXTURE, root)

    def test_raw_and_snapshot_byte_tampering_are_rejected(self) -> None:
        cases = (
            ("H0.jsonl", b"{}\n", "binding mismatch"),
            (f"runtime/lib/{authorizer.RUNTIME_FILES[0]}", b"tamper", "runtime dependency"),
        )
        for relative, appended, expected in cases:
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as tmp:
                root = self.copy_evidence(self.fail_root, Path(tmp))
                path = root / relative
                path.chmod(0o644)
                path.write_bytes(path.read_bytes() + appended)
                path.chmod(0o444 if relative == "H0.jsonl" else 0o555)
                changed_policy = self._policy_for(root)
                with self.trusted_contract(changed_policy), self.assertRaisesRegex(ValueError, expected):
                    authorizer.evaluate_early_rejection(POLICY_FIXTURE, root)

    def test_candidate_tamper_with_fully_recomputed_chain_is_not_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.copy_evidence(self.fail_root, Path(temporary))
            raw_path = root / "B0.jsonl"
            lines = raw_path.read_bytes().splitlines()
            first = json.loads(lines[0])
            first["candidates"] = ["tampered-but-self-consistent"]
            lines[0] = json.dumps(
                first, ensure_ascii=False, sort_keys=True
            ).encode("utf-8")
            raw_path.chmod(0o644)
            raw_path.write_bytes(b"\n".join(lines) + b"\n")
            raw_path.chmod(0o444)
            self.recompute_complete_chain(root)
            with self.trusted_contract(), self.assertRaisesRegex(
                ValueError, "policy-accepted evidence|accepted evidence"
            ):
                authorizer.evaluate_early_rejection(POLICY_FIXTURE, root)

    def test_authorization_tamper_and_cross_root_are_rejected(self) -> None:
        with self.trusted_contract():
            authorization = authorizer.evaluate_early_rejection(
                POLICY_FIXTURE, self.fail_root
            )
            tampered = dict(authorization)
            tampered["authorized"] = True
            base = {
                key: value for key, value in tampered.items() if key != "integrity"
            }
            tampered["integrity"] = digest(authorizer._canonical_json(base))
            with self.assertRaisesRegex(ValueError, "does not match"):
                authorizer.verify_b1_authorization(
                    POLICY_FIXTURE,
                    self.fail_root,
                    authorizer.encode_authorization(tampered),
                )
            with self.assertRaisesRegex(ValueError, "accepted evidence"):
                authorizer.verify_b1_authorization(
                    POLICY_FIXTURE,
                    self.other_fail_root,
                    authorizer.encode_authorization(authorization),
                )

    def test_cli_exit_codes_and_no_replace_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            authorized_output = parent / "authorized.json"
            with self.trusted_contract():
                self.assertEqual(
                    authorizer.main(
                        [
                            "--policy",
                            str(POLICY_FIXTURE),
                            "--acquisition-root",
                            str(self.fail_root),
                            "--output",
                            str(authorized_output),
                        ]
                    ),
                    1,
                )
            self.assertEqual(authorized_output.stat().st_mode & 0o777, 0o444)
            with self.trusted_contract(), mock.patch(
                "sys.stderr", io.StringIO()
            ) as stderr:
                self.assertEqual(
                    authorizer.main(
                        [
                            "--policy",
                            str(POLICY_FIXTURE),
                            "--acquisition-root",
                            str(self.fail_root),
                            "--output",
                            str(authorized_output),
                        ]
                    ),
                    2,
                )
                self.assertIn("refusing to overwrite", stderr.getvalue())

            not_authorized_output = parent / "not-authorized.json"
            with self.trusted_contract(self.pass_policy):
                self.assertEqual(
                    authorizer.main(
                        [
                            "--policy",
                            str(POLICY_FIXTURE),
                            "--acquisition-root",
                            str(self.pass_root),
                            "--output",
                            str(not_authorized_output),
                        ]
                    ),
                    1,
                )
            with self.trusted_contract(), mock.patch(
                "sys.stderr", io.StringIO()
            ) as stderr:
                self.assertEqual(
                    authorizer.main(
                        [
                            "--policy",
                            str(POLICY_FIXTURE),
                            "--acquisition-root",
                            str(parent / "missing"),
                            "--output",
                            str(parent / "invalid.json"),
                        ]
                    ),
                    2,
                )
                self.assertIn("acquisition root", stderr.getvalue())

    def test_acquisition_and_output_paths_must_be_absolute(self) -> None:
        with self.trusted_contract(), self.assertRaisesRegex(
            ValueError, "acquisition root must be an absolute path"
        ):
            authorizer.evaluate_early_rejection(
                POLICY_FIXTURE, Path("relative-acquisition")
            )
        with self.assertRaisesRegex(ValueError, "output must be an absolute path"):
            authorizer._write_noreplace(Path("relative-authorization.json"), b"{}\n")

    def test_post_publish_assurance_failure_rolls_back_owned_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            output = parent / "authorization.json"
            with mock.patch.object(
                authorizer,
                "_read_open_file",
                return_value=(None, "sha256:" + "0" * 64),
            ), self.assertRaisesRegex(OSError, "bytes changed"):
                authorizer._write_noreplace(output, b"trusted bytes\n")
            self.assertFalse(output.exists())
            self.assertEqual(list(parent.glob(".authorization.json.tmp-*")), [])


if __name__ == "__main__":
    unittest.main()
