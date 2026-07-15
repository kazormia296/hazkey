from __future__ import annotations

from contextlib import contextmanager, ExitStack
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import unittest
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT))

from tools.dictionary import authorize_mozc_v2_b1 as authorizer  # noqa: E402
from tools.dictionary import evaluate_mozc_adoption_v2_gate as gate  # noqa: E402
from tools.dictionary import run_mozc_b0_measurement as v1  # noqa: E402
from tools.dictionary import run_mozc_v2_b1_objective as b1  # noqa: E402
from tools.dictionary import run_mozc_v2_objective as v2  # noqa: E402
from tools.dictionary.tests.test_run_mozc_v2_objective import SyntheticFixture  # noqa: E402


POLICY_FIXTURE = (
    REPOSITORY_ROOT
    / "hazkey-server/Tests/grimodex-spike/Fixtures/mozc-adoption-v2/"
    "formal-gate-policy.json"
)


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class B1ContinuationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.fixture = SyntheticFixture(self.root)
        self._create_b1_and_reseal()
        self.prior = self._acquire_prior()
        self.policy = self._synthetic_policy(self.prior)
        with self.trusted_contract():
            authorization = authorizer.evaluate_early_rejection(
                POLICY_FIXTURE, self.prior
            )
        self.authorization_value = authorization
        self.authorization = self.root / "authorization.json"
        self.authorization.write_bytes(authorizer.encode_authorization(authorization))
        self.authorization.chmod(0o444)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _create_b1_and_reseal(self) -> None:
        generation = "sha256-" + "c" * 64
        self.b1_bundle = self.root / generation
        shutil.copytree(self.fixture.b0, self.b1_bundle)
        self.b1_bundle.chmod(0o755)
        core = {
            name: (self.b1_bundle / name).read_bytes()
            for name in (
                "fcitx5-grimodex-mozc-helper",
                "mozc.data",
                "manifest.json",
            )
        }
        freeze = v2.CandidateFreeze(
            generation=generation,
            helper_size_bytes=len(core["fcitx5-grimodex-mozc-helper"]),
            helper_sha256=digest(core["fcitx5-grimodex-mozc-helper"]),
            data_size_bytes=len(core["mozc.data"]),
            data_sha256=digest(core["mozc.data"]),
            manifest_sha256=digest(core["manifest.json"]),
            resource_fingerprint=v2._directory_fingerprint(
                self.b1_bundle, domain="hazkey.mozc-runtime-fingerprint.v1"
            ),
        )
        policy_path = self.fixture.sealed / v2.SEALED_POLICY_NAME
        manifest_path = self.fixture.sealed / v2.SEALED_MANIFEST_NAME
        self.fixture.sealed.chmod(0o755)
        policy_path.chmod(0o644)
        policy = json.loads(policy_path.read_text())
        policy["artifact_freezes"]["candidates"]["B1"] = v2._freeze_object(freeze)
        policy_bytes = (json.dumps(policy, ensure_ascii=False, indent=2) + "\n").encode()
        policy_path.write_bytes(policy_bytes)
        policy_path.chmod(0o444)
        manifest_path.chmod(0o644)
        manifest = json.loads(manifest_path.read_text())
        manifest["policy"]["sha256"] = digest(policy_bytes)
        manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
        manifest_path.write_bytes(manifest_bytes)
        manifest_path.chmod(0o444)
        self.fixture.sealed.chmod(0o555)
        self.fixture.contract = replace(
            self.fixture.contract,
            policy_sha256=digest(policy_bytes),
            manifest_sha256=digest(manifest_bytes),
            b1=freeze,
        )

    def _raw(
        self,
        backend: str,
        converter: str,
        resource: Path,
        *,
        reverse: bool = False,
        wrong_resource: bool = False,
        fail_protected: bool = False,
    ) -> bytes:
        raw = self.fixture.raw_bytes(
            backend_name=backend,
            converter_backend=converter,
            resource_path=(self.root / "wrong-resource" if wrong_resource else resource),
            ordered_rows=list(reversed(self.fixture.rows)) if reverse else self.fixture.rows,
        )
        if not fail_protected:
            return raw
        changed: list[bytes] = []
        for line in raw.splitlines():
            value = json.loads(line)
            if value["category"] == "protected":
                value["candidates"] = ["incorrect-protected-surface"]
            changed.append(json.dumps(value, ensure_ascii=False, sort_keys=True).encode())
        return b"\n".join(changed) + b"\n"

    def _acquire_prior(self) -> Path:
        output = self.root / "prior-evidence"

        def probe(argv, raw_handle, stderr_handle, run_id, environment, cwd):
            del environment
            converter = argv[argv.index("--converter-backend") + 1]
            backend = argv[argv.index("--backend-name") + 1]
            option = "--dictionary" if converter == "hazkey" else "--mozc-bundle"
            resource = cwd / argv[argv.index(option) + 1]
            raw_handle.write(
                self._raw(
                    backend,
                    converter,
                    resource,
                    fail_protected=converter == "mozc",
                )
            )
            stderr_handle.write(f"stderr:{run_id}".encode())
            return 0

        with mock.patch.object(v2, "_run_probe", side_effect=probe), mock.patch.object(
            v1, "_host_contract", return_value=self._host()
        ):
            v2.acquire(
                executable=self.fixture.executable,
                runtime_library_directory=self.fixture.runtime.resolve(),
                sealed_generation=self.fixture.sealed.resolve(),
                hazkey_dictionary=self.fixture.dictionary.resolve(),
                b0_bundle=self.fixture.b0.resolve(),
                output_directory=output,
                contract=self.fixture.contract,
            )
        return output

    def _synthetic_policy(self, evidence: Path):
        real = gate.load_policy(POLICY_FIXTURE)
        manifest = json.loads((evidence / v2.ACQUISITION_MANIFEST_NAME).read_text())
        sources = {item["id"]: item["sha256"] for item in manifest["python_sources"]["files"]}
        tree = authorizer._capture_evidence(evidence)
        return replace(
            real,
            policy_sha256=digest(POLICY_FIXTURE.read_bytes()),
            manifest_sha256=self.fixture.contract.manifest_sha256,
            corpus_sha256=self.fixture.contract.corpus_sha256,
            source_policy_sha256=self.fixture.contract.policy_sha256,
            candidate_resource_fingerprints={
                "B0": self.fixture.contract.b0.resource_fingerprint,
                "B1": self.fixture.contract.b1.resource_fingerprint,
            },
            hazkey_dictionary_fingerprint=self.fixture.contract.hazkey_dictionary_fingerprint,
            trusted_b0_producer={
                "path": v2.PYTHON_SOURCE_BINDINGS["producer"][1],
                "sha256": sources["producer"],
            },
            trusted_b0_python_source_sha256=sources,
            trusted_b0_acquisition_manifest_sha256=digest(
                (evidence / v2.ACQUISITION_MANIFEST_NAME).read_bytes()
            ),
            trusted_b0_acquisition_manifest_integrity=manifest["integrity"],
            trusted_b0_acquisition_tree_digest=tree.tree_digest,
            trusted_b0_raw_run_sha256={
                run_id: digest((evidence / f"{run_id}.jsonl").read_bytes())
                for run_id in ("H0", "B0")
            },
            b1_raw_resource_suffixes={
                "H0": "inputs/Dictionary",
                "B0": f"inputs/B0/{self.fixture.contract.b0.generation}",
            },
        )

    def _host(self) -> dict[str, object]:
        return {
            "fingerprint": "sha256:" + "9" * 64,
            "effective_cpu_affinity": sorted(os.sched_getaffinity(0)),
        }

    @contextmanager
    def trusted_contract(self):
        contract = self.fixture.contract
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(gate, "load_policy", return_value=self.policy))
            stack.enter_context(mock.patch.object(gate, "GENERATION", contract.sealed_generation))
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

    def _b1_probe(self, *, reverse=False, wrong_resource=False, mutate=None, return_code=0):
        def probe(argv, raw_handle, stderr_handle, run_id, environment, cwd):
            self.assertEqual(run_id, "B1")
            self.assertEqual(environment, b1.CHILD_ENVIRONMENT)
            resource = cwd / argv[argv.index("--mozc-bundle") + 1]
            raw_handle.write(
                self._raw(
                    "B1",
                    "mozc",
                    resource,
                    reverse=reverse,
                    wrong_resource=wrong_resource,
                )
            )
            stderr_handle.write(b"stderr:B1")
            if mutate is not None:
                mutate()
            return return_code

        return probe

    def _acquire(self, output: Path, probe=None):
        with self.trusted_contract(), mock.patch.object(
            v2,
            "_run_probe",
            side_effect=probe or self._b1_probe(),
        ), mock.patch.object(v1, "_host_contract", return_value=self._host()):
            return b1.acquire(
                policy_path=POLICY_FIXTURE,
                prior_b0_root=self.prior,
                authorization_path=self.authorization,
                b1_bundle=self.b1_bundle,
                output_directory=output,
                contract=self.fixture.contract,
            )

    def _retained_temporaries(self, output: Path) -> list[Path]:
        return sorted(output.parent.glob(f".{output.name}.tmp-*"))

    def _remove_retained(self, path: Path) -> None:
        v2._make_tree_removable(path)
        shutil.rmtree(path)

    def test_runs_only_b1_reuses_exact_h0_and_publishes_fail_closed_contract(self) -> None:
        output = self.root / "b1-evidence"
        calls: list[str] = []
        probe = self._b1_probe()

        def recording(*args, **kwargs):
            calls.append(args[3])
            return probe(*args, **kwargs)

        manifest = self._acquire(output, recording)
        self.assertEqual(calls, ["B1"])
        self.assertEqual((output / "H0.jsonl").read_bytes(), (self.prior / "H0.jsonl").read_bytes())
        self.assertFalse(manifest["formal_adoption_allowed"])
        self.assertFalse(manifest["b2_evaluation_authorized"])
        self.assertEqual(manifest["measurement"]["execution_order"], ["B1"])
        sources = manifest["python_sources"]
        self.assertEqual(
            {item["id"] for item in sources["files"]},
            {
                "producer",
                "authorizer",
                "formal_gate",
                "v2_acquisition",
                "v1_acquisition",
                "probe_summarizer",
                "quality_evaluator",
            },
        )
        source_base = {key: value for key, value in sources.items() if key != "integrity"}
        self.assertEqual(sources["integrity"], digest(b1._canonical_json(source_base)))
        path_contract = manifest["measurement"]["raw_resource_path_contract"]
        self.assertFalse(path_contract["resolve_or_open_after_publication"])
        self.assertEqual(
            path_contract["exact_suffix"],
            f"inputs/B1/{self.fixture.contract.b1.generation}",
        )
        objective = json.loads((output / b1.OBJECTIVE_NAME).read_text())
        self.assertTrue(objective["passed"])
        self.assertEqual(objective["next_step"], "continue-b1-human-performance-stability")
        authority = objective["authority"]
        self.assertEqual(authority["policy_sha256"], self.policy.policy_sha256)
        self.assertEqual(
            authority["authorization_integrity"], self.authorization_value["integrity"]
        )
        self.assertEqual(
            authority["prior_tree_digest"],
            self.authorization_value["acquisition"]["tree_digest"],
        )
        self.assertEqual(
            authority["accepted_prior_raw_sha256"],
            self.authorization_value["acquisition"]["raw_runs"],
        )
        self.assertEqual(
            authority["b0_early_rejection_failed_check_ids"],
            self.authorization_value["recomputed"]["failed_check_ids"],
        )
        generation = output / "inputs/B1" / self.fixture.contract.b1.generation
        self.assertEqual(stat.S_IMODE(generation.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((generation / "fcitx5-grimodex-mozc-helper").stat().st_mode), 0o555)
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o555)

    def test_authorization_tamper_and_cross_root_are_rejected_before_run(self) -> None:
        value = json.loads(self.authorization.read_text())
        value["scope"] = "product-adoption"
        self.authorization.chmod(0o644)
        self.authorization.write_bytes(authorizer.encode_authorization(value))
        self.authorization.chmod(0o444)
        with self.assertRaisesRegex(ValueError, "authorization"):
            self._acquire(self.root / "tampered-auth")

        self.authorization.unlink()
        self.authorization.write_bytes(authorizer.encode_authorization(self.authorization_value))
        self.authorization.chmod(0o444)
        copied = self.root / "other-prior"
        shutil.copytree(self.prior, copied)
        original = self.prior
        self.prior = copied
        try:
            with self.assertRaisesRegex(ValueError, "authorization|acquisition root"):
                self._acquire(self.root / "cross-root")
        finally:
            self.prior = original

    def test_b1_artifact_tamper_mode_and_hardlink_are_rejected(self) -> None:
        helper = self.b1_bundle / "fcitx5-grimodex-mozc-helper"
        helper.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "mode|hardlink"):
            self._acquire(self.root / "mode")
        helper.chmod(0o555)
        helper.unlink()
        os.link(self.fixture.b0 / "fcitx5-grimodex-mozc-helper", helper)
        with self.assertRaisesRegex(ValueError, "mode|hardlink"):
            self._acquire(self.root / "hardlink")

    def test_raw_order_and_resource_path_are_fail_closed(self) -> None:
        for name, probe in (
            ("order", self._b1_probe(reverse=True)),
            ("resource", self._b1_probe(wrong_resource=True)),
        ):
            output = self.root / name
            with self.assertRaisesRegex(
                b1.EvidencePublicationError, "case IDs|resource path"
            ):
                self._acquire(output, probe)
            self.assertFalse(output.exists())

    def test_stored_aggregate_tamper_is_rejected_by_raw_authorization(self) -> None:
        quality = self.prior / "H0.quality.json"
        quality.chmod(0o644)
        value = json.loads(quality.read_text())
        value["top1_hits"] -= 1
        quality.write_text(json.dumps(value, sort_keys=True) + "\n")
        quality.chmod(0o444)
        with self.assertRaises((ValueError, OSError)):
            self._acquire(self.root / "aggregate-tamper")

    def test_no_replace_and_runner_failures_retain_only_hidden_temporary(self) -> None:
        collision = self.root / "collision"
        collision.mkdir()
        marker = collision / "marker"
        marker.write_text("owned")
        with self.assertRaisesRegex(ValueError, "overwrite"):
            self._acquire(collision)
        self.assertEqual(marker.read_text(), "owned")
        for name, probe in (
            ("failure", self._b1_probe(return_code=7)),
            ("timeout", mock.Mock(side_effect=ValueError("run B1 exceeded 900 seconds"))),
        ):
            output = self.root / name
            with self.assertRaisesRegex(
                b1.EvidencePublicationError,
                r"owned B1 temporary evidence retained.*dev=.*ino=.*acquisition failed",
            ):
                self._acquire(output, probe)
            self.assertFalse(output.exists())
            retained = self._retained_temporaries(output)
            self.assertEqual(len(retained), 1)
            self.assertTrue(retained[0].is_dir())
            self._remove_retained(retained[0])

    def test_candidate_toctou_and_prepublication_auth_recheck_fail_closed(self) -> None:
        helper = self.b1_bundle / "fcitx5-grimodex-mozc-helper"

        def mutate_candidate():
            helper.chmod(0o644)
            helper.write_bytes(b"changed after run\n")
            helper.chmod(0o555)

        output = self.root / "candidate-toctou"
        with self.assertRaisesRegex(b1.EvidencePublicationError, "B1"):
            self._acquire(output, self._b1_probe(mutate=mutate_candidate))
        self.assertFalse(output.exists())

    def test_authorization_toctou_after_run_is_rejected(self) -> None:
        def mutate_authorization():
            self.authorization.chmod(0o644)
            value = json.loads(self.authorization.read_text())
            value["scope"] = "tampered-after-run"
            self.authorization.write_bytes(authorizer.encode_authorization(value))
            self.authorization.chmod(0o444)

        output = self.root / "authorization-toctou"
        with self.assertRaisesRegex(b1.EvidencePublicationError, "authorization"):
            self._acquire(output, self._b1_probe(mutate=mutate_authorization))
        self.assertFalse(output.exists())

    def test_generated_snapshot_mutation_before_freeze_is_rejected(self) -> None:
        original = b1._freeze_tree_b1

        def tamper(root, generation, expected):
            path = root / "H0.jsonl"
            path.write_bytes(b"tampered generated evidence\n")
            return original(root, generation, expected)

        output = self.root / "generated-tamper"
        with mock.patch.object(b1, "_freeze_tree_b1", side_effect=tamper):
            with self.assertRaisesRegex(b1.EvidencePublicationError, "tree mismatch"):
                self._acquire(output)
        self.assertFalse(output.exists())

    def test_replaced_temporary_tree_is_preserved_on_failure(self) -> None:
        locations: dict[str, Path] = {}

        def replace_temporary(root, generation, expected):
            del generation, expected
            actual = root.resolve(strict=True)
            owned = actual.with_name(actual.name + ".owned-original")
            actual.rename(owned)
            actual.mkdir()
            (actual / "foreign-marker").write_text("foreign")
            locations.update(foreign=actual, owned=owned)
            raise ValueError("synthetic failure after temporary replacement")

        output = self.root / "temporary-replaced"
        with mock.patch.object(b1, "_freeze_tree_b1", side_effect=replace_temporary):
            with self.assertRaisesRegex(
                b1.EvidencePublicationError,
                "foreign entry retained.*synthetic failure",
            ):
                self._acquire(output)
        self.assertFalse(output.exists())
        self.assertEqual((locations["foreign"] / "foreign-marker").read_text(), "foreign")
        shutil.rmtree(locations["foreign"])
        v2._make_tree_removable(locations["owned"])
        shutil.rmtree(locations["owned"])

    def test_post_publication_failure_retains_committed_evidence(self) -> None:
        original = v2._verify_tree

        def verify(root, expected, context):
            if context == "post-publication":
                raise ValueError("synthetic post-publication assurance failure")
            return original(root, expected, context)

        output = self.root / "committed-evidence"
        with mock.patch.object(v2, "_verify_tree", side_effect=verify):
            with self.assertRaises(b1.EvidencePublicationError):
                self._acquire(output)
        self.assertTrue(output.is_dir())
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o555)
        v2._make_tree_removable(output)
        shutil.rmtree(output)

    def test_bundle_license_payload_is_not_part_of_runtime_core_fingerprint(self) -> None:
        licenses = self.b1_bundle / "licenses"
        licenses.mkdir()
        notice = licenses / "NOTICE"
        notice.write_text("redistributable metadata\n")
        notice.chmod(0o444)
        licenses.chmod(0o555)
        output = self.root / "licenses-accepted"
        manifest = self._acquire(output)
        self.assertEqual(
            manifest["candidate"]["resource_fingerprint"],
            self.fixture.contract.b1.resource_fingerprint,
        )
        self.assertFalse((output / "inputs/B1" / self.fixture.contract.b1.generation / "licenses").exists())


if __name__ == "__main__":
    unittest.main()
