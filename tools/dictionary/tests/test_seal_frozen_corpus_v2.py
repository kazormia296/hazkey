from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools.dictionary import build_frozen_corpus_v2
from tools.dictionary import seal_frozen_corpus_v2
from tools.dictionary.tests.test_build_frozen_corpus_v2 import (
    PILOT_V1_MANIFEST,
    SyntheticV2Fixture,
)


def render_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def approvals_for(root: Path) -> dict[str, object]:
    components: list[dict[str, object]] = []
    for contract in build_frozen_corpus_v2.COMPONENT_CONTRACTS:
        component_id = str(contract["id"])
        data = (root / str(contract["tsv_path"])).read_bytes()
        rows = build_frozen_corpus_v2._parse_tsv(data, component_id)
        family_ids = [
            f"family-{component_id}-{position:04d}"
            for position in range(1, len(rows) + 1)
        ]
        components.append(
            {
                "id": component_id,
                "status": "approved",
                "tsv_sha256": build_frozen_corpus_v2.sha256_bytes(data),
                "source_id": f"project-curation-v2-{component_id}",
                "author_id": f"project-author-v2-{component_id}",
                "reviewer_id": f"independent-review-v2-{component_id}",
                "redistribution_approved": True,
                "privacy_reviewed": True,
                "family_assignment": {
                    "contract": build_frozen_corpus_v2.FAMILY_ASSIGNMENT_CONTRACT,
                    "sha256": build_frozen_corpus_v2.family_assignment_sha256(
                        rows, family_ids
                    ),
                },
            }
        )
    return {
        "schema": seal_frozen_corpus_v2.APPROVALS_SCHEMA,
        "status": "approved",
        "components": components,
        "near_duplicate_review": {
            "status": "closed",
            "computed_pairs": 0,
            "reviewer_id": "independent-review-v2-near",
            "algorithm": {
                "normalization": build_frozen_corpus_v2.NEAR_NORMALIZATION,
                "match": "either",
                "algorithms": build_frozen_corpus_v2.NEAR_ALGORITHMS,
            },
        },
    }


class FrozenCorpusV2SealerTests(unittest.TestCase):
    def test_prepares_exact_outputs_from_reviewed_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fixture = SyntheticV2Fixture(root)
            approvals_path = root / seal_frozen_corpus_v2.APPROVALS_NAME
            approvals_path.write_bytes(render_json(approvals_for(root)))
            generated = seal_frozen_corpus_v2._prepare_outputs(
                policy_path=fixture.policy_path,
                approvals_path=approvals_path,
                pilot_v1_manifest_path=PILOT_V1_MANIFEST,
            )
            repeated = seal_frozen_corpus_v2._prepare_outputs(
                policy_path=fixture.policy_path,
                approvals_path=approvals_path,
                pilot_v1_manifest_path=PILOT_V1_MANIFEST,
            )
        aggregate = generated[seal_frozen_corpus_v2.AGGREGATE_NAME]
        rows = build_frozen_corpus_v2._parse_tsv(aggregate, "sealed test aggregate")
        self.assertEqual(len(rows), 1360)
        self.assertIn(seal_frozen_corpus_v2.MANIFEST_NAME, generated)
        self.assertIn(seal_frozen_corpus_v2.NEAR_REVIEW_NAME, generated)
        self.assertEqual(
            sum(name.endswith(".provenance.jsonl") for name in generated), 7
        )
        self.assertEqual(generated, repeated)
        self.assertEqual(
            seal_frozen_corpus_v2.sealed_directory_name(generated),
            seal_frozen_corpus_v2.sealed_directory_name(repeated),
        )

    def test_rejects_tsv_changed_after_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fixture = SyntheticV2Fixture(root)
            approvals = approvals_for(root)
            approvals["components"][0]["tsv_sha256"] = "sha256:" + "0" * 64
            approvals_path = root / seal_frozen_corpus_v2.APPROVALS_NAME
            approvals_path.write_bytes(render_json(approvals))
            with self.assertRaisesRegex(ValueError, "reviewed hash changed"):
                seal_frozen_corpus_v2._prepare_outputs(
                    policy_path=fixture.policy_path,
                    approvals_path=approvals_path,
                    pilot_v1_manifest_path=PILOT_V1_MANIFEST,
                )

    def test_seal_publishes_one_immutable_generation_and_refuses_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fixture = SyntheticV2Fixture(root)
            approvals_path = root / seal_frozen_corpus_v2.APPROVALS_NAME
            approvals_path.write_bytes(render_json(approvals_for(root)))

            generated, generation = seal_frozen_corpus_v2.seal(
                policy_path=fixture.policy_path,
                approvals_path=approvals_path,
                pilot_v1_manifest_path=PILOT_V1_MANIFEST,
            )

            self.assertEqual(
                generation.name,
                seal_frozen_corpus_v2.sealed_directory_name(generated),
            )
            self.assertEqual(
                {path.name for path in generation.iterdir()}, set(generated)
            )
            self.assertEqual(generation.stat().st_mode & 0o777, 0o555)
            self.assertTrue(
                all(
                    (path.stat().st_mode & 0o777) == 0o444
                    for path in generation.iterdir()
                )
            )
            rebuilt = build_frozen_corpus_v2.build_aggregate(
                policy_path=generation / fixture.policy_path.name,
                manifest_path=generation / seal_frozen_corpus_v2.MANIFEST_NAME,
                pilot_v1_manifest_path=PILOT_V1_MANIFEST,
            )
            self.assertEqual(
                rebuilt, generated[seal_frozen_corpus_v2.AGGREGATE_NAME]
            )

            with self.assertRaises(OSError):
                seal_frozen_corpus_v2.seal(
                    policy_path=fixture.policy_path,
                    approvals_path=approvals_path,
                    pilot_v1_manifest_path=PILOT_V1_MANIFEST,
                )
            self.assertEqual(list(root.glob(".sealed-v2-staging-*")), [])
            self.assertEqual(
                [
                    path
                    for path in root.iterdir()
                    if path.name.startswith(
                        seal_frozen_corpus_v2.SEALED_DIRECTORY_PREFIX
                    )
                ],
                [generation],
            )

    def test_destination_symlink_conflict_leaves_no_partial_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fixture = SyntheticV2Fixture(root)
            approvals_path = root / seal_frozen_corpus_v2.APPROVALS_NAME
            approvals_path.write_bytes(render_json(approvals_for(root)))
            generated = seal_frozen_corpus_v2._prepare_outputs(
                policy_path=fixture.policy_path,
                approvals_path=approvals_path,
                pilot_v1_manifest_path=PILOT_V1_MANIFEST,
            )
            conflict = root / seal_frozen_corpus_v2.sealed_directory_name(generated)
            conflict.symlink_to("missing-generation")

            with self.assertRaises(OSError):
                seal_frozen_corpus_v2.seal(
                    policy_path=fixture.policy_path,
                    approvals_path=approvals_path,
                    pilot_v1_manifest_path=PILOT_V1_MANIFEST,
                )

            self.assertTrue(conflict.is_symlink())
            self.assertEqual(list(root.glob(".sealed-v2-staging-*")), [])

    def test_partial_staging_write_is_rolled_back(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fixture = SyntheticV2Fixture(root)
            approvals_path = root / seal_frozen_corpus_v2.APPROVALS_NAME
            approvals_path.write_bytes(render_json(approvals_for(root)))
            original_write = seal_frozen_corpus_v2._write_all_at

            def fail_after_two_files(
                directory_fd: int, generated: dict[str, bytes]
            ) -> None:
                original_write(directory_fd, dict(list(generated.items())[:2]))
                raise OSError("injected publication failure")

            with mock.patch.object(
                seal_frozen_corpus_v2,
                "_write_all_at",
                side_effect=fail_after_two_files,
            ), self.assertRaisesRegex(OSError, "injected publication failure"):
                seal_frozen_corpus_v2.seal(
                    policy_path=fixture.policy_path,
                    approvals_path=approvals_path,
                    pilot_v1_manifest_path=PILOT_V1_MANIFEST,
                )

            self.assertEqual(list(root.glob(".sealed-v2-staging-*")), [])
            self.assertEqual(
                [
                    path
                    for path in root.iterdir()
                    if path.name.startswith(
                        seal_frozen_corpus_v2.SEALED_DIRECTORY_PREFIX
                    )
                ],
                [],
            )

    def test_root_path_swap_after_publish_fails_closed_and_removes_generation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            root = base / "corpus"
            root.mkdir()
            moved = base / "moved-corpus"
            fixture = SyntheticV2Fixture(root)
            approvals_path = root / seal_frozen_corpus_v2.APPROVALS_NAME
            approvals_path.write_bytes(render_json(approvals_for(root)))
            original_rename = seal_frozen_corpus_v2._rename_noreplace

            def publish_then_swap(
                root_fd: int,
                source_name: str,
                destination_name: str,
            ) -> None:
                original_rename(root_fd, source_name, destination_name)
                root.rename(moved)
                root.mkdir()
                fake = root / destination_name
                fake.mkdir()
                (fake / "marker").write_text("fake", encoding="utf-8")

            with mock.patch.object(
                seal_frozen_corpus_v2,
                "_rename_noreplace",
                side_effect=publish_then_swap,
            ), self.assertRaisesRegex(ValueError, "corpus root path changed"):
                seal_frozen_corpus_v2.seal(
                    policy_path=fixture.policy_path,
                    approvals_path=approvals_path,
                    pilot_v1_manifest_path=PILOT_V1_MANIFEST,
                )

            self.assertEqual(
                [
                    path
                    for path in moved.iterdir()
                    if path.name.startswith(
                        seal_frozen_corpus_v2.SEALED_DIRECTORY_PREFIX
                    )
                ],
                [],
            )
            fake_generations = [
                path
                for path in root.iterdir()
                if path.name.startswith(
                    seal_frozen_corpus_v2.SEALED_DIRECTORY_PREFIX
                )
            ]
            self.assertEqual(len(fake_generations), 1)
            self.assertEqual(
                (fake_generations[0] / "marker").read_text(encoding="utf-8"),
                "fake",
            )
            self.assertEqual(list(moved.glob(".sealed-v2-staging-*")), [])

    def test_post_publish_content_mutation_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fixture = SyntheticV2Fixture(root)
            approvals_path = root / seal_frozen_corpus_v2.APPROVALS_NAME
            approvals_path.write_bytes(render_json(approvals_for(root)))
            original_rename = seal_frozen_corpus_v2._rename_noreplace

            def publish_then_mutate(
                root_fd: int,
                source_name: str,
                destination_name: str,
            ) -> None:
                original_rename(root_fd, source_name, destination_name)
                generation = root / destination_name
                generation.chmod(0o700)
                policy = generation / fixture.policy_path.name
                policy.chmod(0o600)
                policy.write_bytes(b"tampered\n")
                policy.chmod(0o444)
                generation.chmod(0o555)

            with mock.patch.object(
                seal_frozen_corpus_v2,
                "_rename_noreplace",
                side_effect=publish_then_mutate,
            ), self.assertRaisesRegex(ValueError, "sealed generation output changed"):
                seal_frozen_corpus_v2.seal(
                    policy_path=fixture.policy_path,
                    approvals_path=approvals_path,
                    pilot_v1_manifest_path=PILOT_V1_MANIFEST,
                )

            self.assertEqual(
                [
                    path
                    for path in root.iterdir()
                    if path.name.startswith(
                        seal_frozen_corpus_v2.SEALED_DIRECTORY_PREFIX
                    )
                ],
                [],
            )

    def test_post_publish_symlink_replacement_is_rolled_back(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fixture = SyntheticV2Fixture(root)
            approvals_path = root / seal_frozen_corpus_v2.APPROVALS_NAME
            approvals_path.write_bytes(render_json(approvals_for(root)))
            original_rename = seal_frozen_corpus_v2._rename_noreplace

            def publish_then_replace_with_symlink(
                root_fd: int,
                source_name: str,
                destination_name: str,
            ) -> None:
                original_rename(root_fd, source_name, destination_name)
                generation = root / destination_name
                generation.chmod(0o700)
                policy = generation / fixture.policy_path.name
                policy.unlink()
                policy.symlink_to("missing-policy")
                generation.chmod(0o555)

            with mock.patch.object(
                seal_frozen_corpus_v2,
                "_rename_noreplace",
                side_effect=publish_then_replace_with_symlink,
            ), self.assertRaises(OSError):
                seal_frozen_corpus_v2.seal(
                    policy_path=fixture.policy_path,
                    approvals_path=approvals_path,
                    pilot_v1_manifest_path=PILOT_V1_MANIFEST,
                )

            self.assertEqual(
                [
                    path
                    for path in root.iterdir()
                    if path.name.startswith(
                        seal_frozen_corpus_v2.SEALED_DIRECTORY_PREFIX
                    )
                ],
                [],
            )


if __name__ == "__main__":
    unittest.main()
