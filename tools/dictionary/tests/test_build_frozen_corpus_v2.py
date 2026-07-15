from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT))

from tools.dictionary import build_frozen_corpus  # noqa: E402
from tools.dictionary import build_frozen_corpus_v2  # noqa: E402


V2_FIXTURE = (
    REPOSITORY_ROOT
    / "hazkey-server/Tests/grimodex-spike/Fixtures/mozc-adoption-v2"
)
POLICY_PATH = V2_FIXTURE / "corpus-policy.json"
PILOT_V1_MANIFEST = (
    REPOSITORY_ROOT
    / "hazkey-server/Tests/grimodex-spike/Fixtures/mozc-adoption-v1/manifest.json"
)


def render_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def render_jsonl(values: list[dict[str, object]]) -> bytes:
    return (
        "".join(
            json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
            for value in values
        )
    ).encode("utf-8")


def unique_reading(seed: str) -> str:
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return "".join(
        chr(0x3400 + (int(digest[index : index + 4], 16) % 0x19B5))
        for index in range(0, 64, 4)
    )


def provenance_for(row: dict[str, str], component: str, index: int) -> dict[str, object]:
    family_id = f"family-{component}-{index + 1:04d}"
    return {
        "schema": build_frozen_corpus_v2.PROVENANCE_SCHEMA,
        "case_id": row["id"],
        "family_id": family_id,
        "source": {
            "kind": "project-authored",
            "source_id": f"source-{component}-{index:04d}",
            "locator_sha256": build_frozen_corpus_v2.case_locator_sha256(
                row, family_id
            ),
            "license": "MIT",
            "new_holdout": True,
        },
        "rights": {
            "redistribution_approved": True,
            "privacy_reviewed": True,
            "reviewer_id": "test-rights-reviewer",
        },
        "exposure": {
            "status": "sealed-for-b0-b1",
            "eligible_candidate_ids": ["B0", "B1"],
            "disclosed_before_candidate_freezes": False,
        },
        "contamination": {
            "status": "no-known-overlap",
            "screened_against": list(
                build_frozen_corpus_v2.REQUIRED_CONTAMINATION_SCREENS
            ),
        },
    }


class SyntheticV2Fixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
        self.policy["collection"] = {
            "status": "ready",
            "manifest_path": "manifest.json",
        }
        self.policy_path = root / "corpus-policy.json"
        self.policy_path.write_bytes(render_json(self.policy))

        self.rows: dict[str, list[dict[str, str]]] = {}
        self.provenance: dict[str, list[dict[str, object]]] = {}
        self.components: list[dict[str, object]] = []
        all_rows: list[dict[str, str]] = []
        for contract in build_frozen_corpus_v2.COMPONENT_CONTRACTS:
            component_id = str(contract["id"])
            rows: list[dict[str, str]] = []
            records: list[dict[str, object]] = []
            for index in range(int(contract["cases"])):
                reading = unique_reading(f"{component_id}:{index}")
                row = {
                    "id": f"{contract['id_prefix']}{index + 1:04d}",
                    "reading": reading,
                    "expected": f"期待{reading}",
                    "category": str(contract["category"]),
                }
                rows.append(row)
                records.append(provenance_for(row, component_id, index))
            self.rows[component_id] = rows
            self.provenance[component_id] = records
            all_rows.extend(rows)
            self._write_component(contract, rows, records, append=True)

        pilot_bytes = build_frozen_corpus.build_aggregate(PILOT_V1_MANIFEST)
        self.pilot_rows = build_frozen_corpus_v2._parse_tsv(
            pilot_bytes, "test pilot v1"
        )
        self.review_path = root / "near-duplicate-review.json"
        self._write_review(all_rows)
        self.manifest: dict[str, object] = {
            "schema": build_frozen_corpus_v2.MANIFEST_SCHEMA,
            "policy": {
                "path": self.policy_path.name,
                "sha256": build_frozen_corpus_v2.sha256_bytes(
                    self.policy_path.read_bytes()
                ),
            },
            "components": self.components,
            "near_duplicate_review": {
                "path": self.review_path.name,
                "sha256": build_frozen_corpus_v2.sha256_bytes(
                    self.review_path.read_bytes()
                ),
                "status": "closed",
            },
            "pilot_v1": {
                key: value
                for key, value in self.policy["exclusions"]["pilot_v1"].items()
                if key != "counted"
            },
            "aggregate": self._aggregate_object(all_rows),
        }
        self.manifest_path = root / "manifest.json"
        self._write_manifest()

    def _write_component(
        self,
        contract: dict[str, object],
        rows: list[dict[str, str]],
        records: list[dict[str, object]],
        *,
        append: bool,
    ) -> None:
        tsv_data = build_frozen_corpus_v2._encode_rows(rows)
        provenance_data = render_jsonl(records)
        (self.root / str(contract["tsv_path"])).write_bytes(tsv_data)
        (self.root / str(contract["provenance_path"])).write_bytes(provenance_data)
        entry: dict[str, object] = {
            "id": contract["id"],
            "tsv": {
                "path": contract["tsv_path"],
                "sha256": build_frozen_corpus_v2.sha256_bytes(tsv_data),
                "cases": contract["cases"],
            },
            "provenance": {
                "path": contract["provenance_path"],
                "sha256": build_frozen_corpus_v2.sha256_bytes(provenance_data),
                "records": contract["cases"],
            },
        }
        if append:
            self.components.append(entry)
        else:
            index = next(
                index
                for index, value in enumerate(self.components)
                if value["id"] == contract["id"]
            )
            self.components[index] = entry

    def _all_rows(self) -> list[dict[str, str]]:
        return [
            row
            for contract in build_frozen_corpus_v2.COMPONENT_CONTRACTS
            for row in self.rows[str(contract["id"])]
        ]

    def _write_review(self, all_rows: list[dict[str, str]]) -> None:
        pairs = build_frozen_corpus_v2.find_near_duplicate_pairs(
            all_rows, self.pilot_rows
        )
        review = {
            "schema": build_frozen_corpus_v2.NEAR_REVIEW_SCHEMA,
            "status": "closed",
            "algorithm": {
                "normalization": build_frozen_corpus_v2.NEAR_NORMALIZATION,
                "match": "either",
                "algorithms": build_frozen_corpus_v2.NEAR_ALGORITHMS,
            },
            "pairs": [
                pair
                | {
                    "disposition": "distinct-reviewed",
                    "reviewer_id": "test-near-reviewer",
                    "rationale": "synthetic cases have independent sources",
                }
                for pair in pairs
            ],
        }
        self.review_path.write_bytes(render_json(review))

    @staticmethod
    def _aggregate_object(rows: list[dict[str, str]]) -> dict[str, object]:
        aggregate = build_frozen_corpus_v2._encode_rows(rows)
        return {
            "cases": build_frozen_corpus_v2.TOTAL_CASES,
            "quality_cases": build_frozen_corpus_v2.QUALITY_CASES,
            "sha256": build_frozen_corpus_v2.sha256_bytes(aggregate),
            "categories": build_frozen_corpus_v2.ALL_CATEGORIES,
            "protected_included_in_overall_quality_rates": False,
            "exact_pilot_overlap_cases": 0,
        }

    def rewrite_component(self, component_id: str) -> None:
        contract = next(
            contract
            for contract in build_frozen_corpus_v2.COMPONENT_CONTRACTS
            if contract["id"] == component_id
        )
        self._write_component(
            contract,
            self.rows[component_id],
            self.provenance[component_id],
            append=False,
        )
        all_rows = self._all_rows()
        self.manifest["components"] = self.components
        self.manifest["aggregate"] = self._aggregate_object(all_rows)
        self._write_manifest()

    def refresh_locator(self, component_id: str, index: int) -> None:
        row = self.rows[component_id][index]
        record = self.provenance[component_id][index]
        record["source"]["locator_sha256"] = (
            build_frozen_corpus_v2.case_locator_sha256(
                row, str(record["family_id"])
            )
        )

    def close_near_review(self) -> None:
        all_rows = self._all_rows()
        self._write_review(all_rows)
        self.manifest["near_duplicate_review"] = {
            "path": self.review_path.name,
            "sha256": build_frozen_corpus_v2.sha256_bytes(
                self.review_path.read_bytes()
            ),
            "status": "closed",
        }
        self._write_manifest()

    def _write_manifest(self) -> None:
        self.manifest_path.write_bytes(render_json(self.manifest))


class FrozenCorpusV2BuilderTests(unittest.TestCase):
    def test_checked_in_policy_is_valid_pending_and_freezes_scope(self) -> None:
        policy, _ = build_frozen_corpus_v2.validate_policy(
            POLICY_PATH, require_ready=False
        )
        self.assertEqual(policy["collection"]["status"], "pending_collection")
        self.assertEqual(policy["formal_suite"]["total_cases"], 1360)
        self.assertEqual(policy["formal_suite"]["quality_cases"], 1260)
        self.assertEqual(
            policy["formal_suite"]["quality_metrics"],
            ["top1", "top10", "human_preference", "both_bad"],
        )
        self.assertEqual(policy["formal_suite"]["protected"]["required_passes"], 100)
        self.assertEqual(
            policy["formal_suite"]["protected"]["metric"], "top1_exact"
        )
        self.assertFalse(
            policy["formal_suite"]["protected"][
                "included_in_overall_quality_rates"
            ]
        )
        self.assertEqual(
            policy["artifact_freezes"]["eligible_candidate_ids"], ["B0", "B1"]
        )
        self.assertFalse(
            policy["artifact_freezes"]["one_shot_exposure"]["B2_eligible"]
        )
        with self.assertRaisesRegex(ValueError, "pending_collection"):
            build_frozen_corpus_v2.validate_policy(POLICY_PATH, require_ready=True)

    def test_builds_exact_deterministic_1360_case_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture = SyntheticV2Fixture(Path(temporary_directory))
            first = build_frozen_corpus_v2.build_aggregate(
                policy_path=fixture.policy_path,
                manifest_path=fixture.manifest_path,
                pilot_v1_manifest_path=PILOT_V1_MANIFEST,
            )
            second = build_frozen_corpus_v2.build_aggregate(
                policy_path=fixture.policy_path,
                manifest_path=fixture.manifest_path,
                pilot_v1_manifest_path=PILOT_V1_MANIFEST,
            )
        rows = build_frozen_corpus_v2._parse_tsv(first, "synthetic aggregate")
        self.assertEqual(first, second)
        self.assertEqual(len(rows), 1360)
        self.assertEqual(
            sum(row["category"] != "protected" for row in rows), 1260
        )
        self.assertEqual(sum(row["category"] == "protected" for row in rows), 100)

    def test_provenance_rejects_rights_exposure_contamination_and_source(self) -> None:
        row = {
            "id": "v2-technical-0001",
            "reading": "てすと",
            "expected": "試験",
            "category": "technical-mixed",
        }
        valid = provenance_for(row, "technical-mixed", 0)
        mutations = {
            "redistribution approval": lambda value: value["rights"].update(
                redistribution_approved=False
            ),
            "candidate exposure": lambda value: value["exposure"].update(
                disclosed_before_candidate_freezes=True
            ),
            "training overlap": lambda value: value["contamination"].update(
                status="known-overlap"
            ),
            "locator forgery": lambda value: value["source"].update(
                locator_sha256="sha256:" + "0" * 64
            ),
            "excluded source": lambda value: value["source"].update(
                source_id="zenz-training-row"
            ),
            "unverified product source": lambda value: value["source"].update(
                kind="rights-cleared-product"
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                changed = copy.deepcopy(valid)
                mutate(changed)
                with self.assertRaises(ValueError):
                    build_frozen_corpus_v2._validate_provenance_record(
                        changed, row, name
                    )

    def test_rejects_duplicate_family_and_exact_pilot_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture = SyntheticV2Fixture(Path(temporary_directory))
            records = fixture.provenance["technical-mixed"]
            records[1]["family_id"] = records[0]["family_id"]
            fixture.refresh_locator("technical-mixed", 1)
            fixture.rewrite_component("technical-mixed")
            with self.assertRaisesRegex(ValueError, "duplicate formal family_id"):
                build_frozen_corpus_v2.build_aggregate(
                    policy_path=fixture.policy_path,
                    manifest_path=fixture.manifest_path,
                    pilot_v1_manifest_path=PILOT_V1_MANIFEST,
                )

        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture = SyntheticV2Fixture(Path(temporary_directory))
            pilot = fixture.pilot_rows[0]
            fixture.rows["technical-mixed"][0]["reading"] = pilot["reading"]
            fixture.rows["technical-mixed"][0]["expected"] = pilot["expected"]
            fixture.refresh_locator("technical-mixed", 0)
            fixture.rewrite_component("technical-mixed")
            with self.assertRaisesRegex(ValueError, "exact overlaps with pilot v1"):
                build_frozen_corpus_v2.build_aggregate(
                    policy_path=fixture.policy_path,
                    manifest_path=fixture.manifest_path,
                    pilot_v1_manifest_path=PILOT_V1_MANIFEST,
                )

    def test_rejects_component_id_gap_or_reordering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture = SyntheticV2Fixture(Path(temporary_directory))
            rows = fixture.rows["proper-noun"]
            records = fixture.provenance["proper-noun"]
            rows[0], rows[1] = rows[1], rows[0]
            records[0], records[1] = records[1], records[0]
            fixture.rewrite_component("proper-noun")
            with self.assertRaisesRegex(ValueError, "case ID sequence mismatch"):
                build_frozen_corpus_v2.build_aggregate(
                    policy_path=fixture.policy_path,
                    manifest_path=fixture.manifest_path,
                    pilot_v1_manifest_path=PILOT_V1_MANIFEST,
                )

        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture = SyntheticV2Fixture(Path(temporary_directory))
            row = fixture.rows["proper-noun"][1]
            record = fixture.provenance["proper-noun"][1]
            row["id"] = "v2-proper-0003"
            record["case_id"] = row["id"]
            fixture.refresh_locator("proper-noun", 1)
            fixture.rewrite_component("proper-noun")
            with self.assertRaisesRegex(ValueError, "case ID sequence mismatch"):
                build_frozen_corpus_v2.build_aggregate(
                    policy_path=fixture.policy_path,
                    manifest_path=fixture.manifest_path,
                    pilot_v1_manifest_path=PILOT_V1_MANIFEST,
                )

    def test_near_review_requires_every_jaccard_or_levenshtein_pair(self) -> None:
        left = {
            "id": "a",
            "reading": "これはながいちかいよみです",
            "expected": "甲",
            "category": "technical-mixed",
        }
        right = {
            "id": "b",
            "reading": "これはながいちかいよみてす",
            "expected": "乙",
            "category": "technical-mixed",
        }
        pairs = build_frozen_corpus_v2.find_near_duplicate_pairs([left, right], [])
        self.assertEqual(len(pairs), 1)
        self.assertGreaterEqual(pairs[0]["levenshtein_basis_points"], 9000)

        with tempfile.TemporaryDirectory() as temporary_directory:
            fixture = SyntheticV2Fixture(Path(temporary_directory))
            rows = fixture.rows["technical-mixed"]
            rows[1]["reading"] = rows[0]["reading"][:-1] + "亜"
            fixture.refresh_locator("technical-mixed", 1)
            fixture.rewrite_component("technical-mixed")
            with self.assertRaisesRegex(ValueError, "near review is not closed"):
                build_frozen_corpus_v2.build_aggregate(
                    policy_path=fixture.policy_path,
                    manifest_path=fixture.manifest_path,
                    pilot_v1_manifest_path=PILOT_V1_MANIFEST,
                )
            fixture.close_near_review()
            aggregate = build_frozen_corpus_v2.build_aggregate(
                policy_path=fixture.policy_path,
                manifest_path=fixture.manifest_path,
                pilot_v1_manifest_path=PILOT_V1_MANIFEST,
            )
            self.assertEqual(
                len(build_frozen_corpus_v2._parse_tsv(aggregate, "reviewed")), 1360
            )


if __name__ == "__main__":
    unittest.main()
