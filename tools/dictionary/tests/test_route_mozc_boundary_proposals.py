from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from tools.dictionary import route_mozc_boundary_proposals as route
from tools.dictionary import serve_mozc_boundary_annotations as serve


def sha(value: bytes) -> str:
    return serve.sha256_bytes(value)


def queue_record(
    case_id: str,
    *,
    reading: str = "きょうはあめ",
    surface: str = "今日は雨",
    category: str = "fixture",
) -> dict[str, object]:
    return {
        "schema": serve.QUEUE_SCHEMA,
        "id": case_id,
        "category": category,
        "source": {
            "reading": reading,
            "expected_surfaces": [surface],
            "row_sha256": sha(f"row:{case_id}".encode()),
            "corpus_sha256": sha(b"fixture-corpus"),
        },
        "elements": {
            "unit": serve.ELEMENT_UNIT,
            "values": [
                {"index": index, "text": character}
                for index, character in enumerate(reading)
            ],
        },
        "candidate_outputs_consulted": False,
        "preannotation": {
            "marked_reading": reading,
            "first_segment_count": len(reading),
            "confidence": "exact",
        },
        "token_audit": {"summary": "fixture"},
    }


def queue_data(records: list[dict[str, object]]) -> serve.QueueData:
    data = serve.canonical_jsonl(records)  # type: ignore[arg-type]
    return serve.QueueData(
        tuple(records),  # type: ignore[arg-type]
        {str(record["id"]): record for record in records},  # type: ignore[arg-type]
        sha(data),
    )


class ProposalRoutingTests(unittest.TestCase):
    def make_workspace(
        self, root: Path, records: list[dict[str, object]]
    ) -> serve.Workspace:
        return serve.Workspace(
            queue_data(records),
            root,
            workbook_path=None,
            annotator_id="silver-router-test",
            proposal_backend=None,
            proposal_backend_message="disabled in test",
        )

    def add_proposal(
        self,
        workspace: serve.Workspace,
        case_id: str,
        raw_output: dict[str, object],
    ) -> dict[str, object]:
        record = workspace.queue.by_id[case_id]
        review = workspace.reviews[case_id]
        reading = serve._effective_reading(record, review)
        proposal_id = "proposal-" + case_id
        ambiguous, reasons, paths, discarded = workspace._validate_llm_output(
            case_id,
            raw_output,
            proposal_id,
            reading=reading,
        )
        proposal: dict[str, object] = {
            "schema": serve.PROPOSAL_SCHEMA,
            "proposal_id": proposal_id,
            "case_id": case_id,
            "source_row_sha256": record["source"]["row_sha256"],
            "review_revision": review["revision"],
            "effective_reading_sha256": serve._effective_reading_sha256(
                reading
            ),
            "created_at": "2026-07-17T00:00:00Z",
            "ambiguous": ambiguous,
            "ambiguity_reasons": reasons,
            "paths": paths,
            "discarded_candidates": discarded,
            "generator": {
                "provider": "codex-app-server",
                "model": "gpt-5.2-codex",
                "reasoning_effort": "high",
                "prompt_version": "fixture-prompt-v1",
                "prompt_sha256": sha(b"fixture prompt"),
            },
            "raw_output": raw_output,
        }
        with workspace.proposals_path.open("ab") as output:
            output.write(serve.canonical_json_bytes(proposal))
        workspace.proposals[case_id].append(proposal)  # type: ignore[arg-type]
        return proposal

    @staticmethod
    def single_output(
        *, ambiguous: bool = False
    ) -> dict[str, object]:
        return {
            "ambiguous": ambiguous,
            "ambiguity_reasons": ["別案あり"] if ambiguous else [],
            "candidates": [
                {
                    "surface_reference_index": 0,
                    "chunks": [
                        {"reading": "きょうは", "surface": "今日は"},
                        {"reading": "あめ", "surface": "雨"},
                    ],
                }
            ],
        }

    def test_applies_only_clean_single_proposal_as_open_silver(self) -> None:
        records = [queue_record("silver")]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = self.make_workspace(root, records)
            try:
                proposal = self.add_proposal(
                    workspace, "silver", self.single_output()
                )
                manifest = route.route_workspace(
                    workspace,
                    apply=True,
                    batch_id="00000000-0000-4000-8000-000000000001",
                    created_at="2026-07-17T01:00:00Z",
                )
                review = workspace.reviews["silver"]
                self.assertEqual(review["path_set_status"], "open")
                self.assertFalse(review["reviewed_once"])
                self.assertFalse(review["needs_adjudication"])
                self.assertEqual(
                    review["routing_batch_id"],
                    "00000000-0000-4000-8000-000000000001",
                )
                self.assertEqual(review["annotation_tier"], "silver")
                self.assertTrue(review["llm_unmodified"])
                self.assertFalse(review["human_reviewed"])
                self.assertEqual(
                    [path["status"] for path in review["acceptable_paths"]],
                    ["acceptable"],
                )
                path_provenance = review["acceptable_paths"][0][
                    "provenance"
                ]
                self.assertEqual(
                    path_provenance["routing_batch_id"],
                    "00000000-0000-4000-8000-000000000001",
                )
                self.assertEqual(
                    path_provenance["annotation_tier"], "silver"
                )
                self.assertTrue(path_provenance["llm_unmodified"])
                self.assertFalse(path_provenance["human_reviewed"])
                self.assertEqual(manifest["state"], "applied")
                self.assertEqual(manifest["counts"], {
                    "cases": 1,
                    "silver": 1,
                    "gold": 0,
                })
                routed = manifest["cases"][0]
                self.assertEqual(
                    routed["routing_batch_id"], manifest["batch_id"]
                )
                self.assertEqual(routed["annotation_tier"], "silver")
                self.assertTrue(routed["llm_unmodified"])
                self.assertFalse(routed["human_reviewed"])
                self.assertTrue(routed["semantic_validation_passed"])
                self.assertEqual(
                    routed["proposal"]["source_revision"], 0
                )
                self.assertEqual(
                    routed["proposal"]["generator"],
                    {
                        "model": "gpt-5.2-codex",
                        "reasoning_effort": "high",
                        "prompt_version": "fixture-prompt-v1",
                        "prompt_sha256": sha(b"fixture prompt"),
                    },
                )
                self.assertEqual(
                    manifest["proposal_journal"]["sha256"],
                    sha(workspace.proposals_path.read_bytes()),
                )
                manifest_path = root / manifest["manifest_path"]
                self.assertEqual(
                    json.loads(manifest_path.read_text()), manifest
                )
                event = json.loads(workspace.events_path.read_text())
                self.assertEqual(event["action"]["kind"], "silver_auto_adopt")
                self.assertEqual(
                    event["action"]["proposal_id"], proposal["proposal_id"]
                )
                self.assertFalse(event["action"]["human_reviewed"])
                self.assertEqual(
                    event["action"]["routing_batch_id"], manifest["batch_id"]
                )
                exported = json.loads(workspace.export_bytes())
                self.assertEqual(
                    exported["review"]["routing_batch_id"],
                    manifest["batch_id"],
                )
                self.assertEqual(
                    exported["review"]["annotation_tier"], "silver"
                )
                self.assertEqual(
                    exported["acceptable_paths"][0]["provenance"][
                        "annotation_tier"
                    ],
                    "silver",
                )
            finally:
                workspace.close()

            reopened = self.make_workspace(root, records)
            try:
                self.assertEqual(
                    reopened.reviews["silver"]["path_set_status"], "open"
                )
                self.assertFalse(reopened.reviews["silver"]["reviewed_once"])
                previous = reopened.reviews["silver"]
                saved = reopened.patch_review(
                    "silver",
                    {
                        "base_revision": previous["revision"],
                        "path_set_status": "open",
                        "needs_adjudication": False,
                        "acceptable_paths": deepcopy(
                            previous["acceptable_paths"]
                        ),
                        "notes": "人手確認済み",
                        "reviewed_once": True,
                    },
                )
                self.assertEqual(saved["annotation_tier"], "gold")
                self.assertFalse(saved["llm_unmodified"])
                self.assertTrue(saved["human_reviewed"])
                self.assertEqual(
                    saved["routing_batch_id"], manifest["batch_id"]
                )
                saved_path_audit = saved["acceptable_paths"][0][
                    "provenance"
                ]
                self.assertEqual(saved_path_audit["annotation_tier"], "gold")
                self.assertFalse(saved_path_audit["llm_unmodified"])
                self.assertTrue(saved_path_audit["human_reviewed"])
            finally:
                reopened.close()

            replayed = self.make_workspace(root, records)
            try:
                self.assertEqual(
                    replayed.reviews["silver"]["annotation_tier"], "gold"
                )
                self.assertTrue(
                    replayed.reviews["silver"]["human_reviewed"]
                )
            finally:
                replayed.close()

    def test_routes_policy_exceptions_to_pending_gold_review(self) -> None:
        long_reading = "あ" * serve.LONG_READING_THRESHOLD
        records = [
            queue_record("ambiguous"),
            queue_record("discarded"),
            queue_record("multiple"),
            queue_record("long", reading=long_reading, surface="亜"),
            queue_record("corrected"),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self.make_workspace(Path(temporary), records)
            try:
                self.add_proposal(
                    workspace, "ambiguous", self.single_output(ambiguous=True)
                )
                duplicate_output = self.single_output()
                duplicate_output["candidates"] = [
                    *duplicate_output["candidates"],  # type: ignore[misc]
                    deepcopy(duplicate_output["candidates"][0]),  # type: ignore[index]
                ]
                self.add_proposal(workspace, "discarded", duplicate_output)
                multiple_output = self.single_output()
                multiple_output["candidates"] = [
                    *multiple_output["candidates"],  # type: ignore[misc]
                    {
                        "surface_reference_index": 0,
                        "chunks": [
                            {"reading": "きょう", "surface": "今日"},
                            {"reading": "はあめ", "surface": "は雨"},
                        ],
                    },
                ]
                self.add_proposal(workspace, "multiple", multiple_output)
                self.add_proposal(
                    workspace,
                    "long",
                    {
                        "ambiguous": False,
                        "ambiguity_reasons": [],
                        "candidates": [
                            {
                                "surface_reference_index": 0,
                                "chunks": [
                                    {"reading": long_reading, "surface": "亜"}
                                ],
                            }
                        ],
                    },
                )
                workspace.patch_review(
                    "corrected",
                    {
                        "base_revision": 0,
                        "corrected_reading": "きょうわあめ",
                        "path_set_status": "pending",
                        "needs_adjudication": False,
                        "acceptable_paths": [],
                        "notes": None,
                    },
                )
                self.add_proposal(
                    workspace,
                    "corrected",
                    {
                        "ambiguous": False,
                        "ambiguity_reasons": [],
                        "candidates": [
                            {
                                "surface_reference_index": 0,
                                "chunks": [
                                    {"reading": "きょうわ", "surface": "今日は"},
                                    {"reading": "あめ", "surface": "雨"},
                                ],
                            }
                        ],
                    },
                )

                manifest = route.route_workspace(workspace, apply=True)
                decisions = {
                    item["case_id"]: item for item in manifest["cases"]
                }
                self.assertEqual(manifest["counts"]["silver"], 0)
                self.assertEqual(manifest["counts"]["gold"], 5)
                self.assertIn("ambiguous", decisions["ambiguous"]["gold_reasons"])
                self.assertIn(
                    "discarded_candidate",
                    decisions["discarded"]["gold_reasons"],
                )
                self.assertIn(
                    "multiple_paths", decisions["multiple"]["gold_reasons"]
                )
                self.assertIn("long_reading", decisions["long"]["gold_reasons"])
                self.assertIn(
                    "reading_corrected",
                    decisions["corrected"]["gold_reasons"],
                )
                self.assertTrue(decisions["corrected"]["human_reviewed"])
                for case_id in ("ambiguous", "discarded", "multiple", "long"):
                    review = workspace.reviews[case_id]
                    self.assertEqual(review["path_set_status"], "pending")
                    self.assertTrue(review["needs_adjudication"])
                    self.assertFalse(review["reviewed_once"])
                    self.assertEqual(review["annotation_tier"], "gold")
                    self.assertFalse(review["llm_unmodified"])
                    self.assertFalse(review["human_reviewed"])
                    self.assertEqual(
                        review["routing_batch_id"], manifest["batch_id"]
                    )
                self.assertFalse(
                    workspace.reviews["corrected"]["needs_adjudication"]
                )
                routed_gold = workspace.reviews["ambiguous"]
                human_saved = workspace.patch_review(
                    "ambiguous",
                    {
                        "base_revision": routed_gold["revision"],
                        "path_set_status": "open",
                        "needs_adjudication": False,
                        "acceptable_paths": [
                            {
                                "path_id": "human-gold-path",
                                "status": "acceptable",
                                "surface_reference_id": "surface-0",
                                "reading_boundaries": [4],
                                "surface_boundaries": [3],
                                "alignment_status": "aligned",
                                "provenance": {"kind": "human"},
                            }
                        ],
                        "notes": None,
                        "reviewed_once": True,
                    },
                )
                self.assertEqual(human_saved["annotation_tier"], "gold")
                self.assertTrue(human_saved["human_reviewed"])
                self.assertEqual(
                    human_saved["acceptable_paths"][0]["provenance"][
                        "routing_batch_id"
                    ],
                    manifest["batch_id"],
                )
            finally:
                workspace.close()

    def test_tampered_alignment_cannot_be_silver(self) -> None:
        records = [queue_record("tampered")]
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self.make_workspace(Path(temporary), records)
            try:
                proposal = self.add_proposal(
                    workspace, "tampered", self.single_output()
                )
                tampered_path = deepcopy(proposal["paths"][0])  # type: ignore[index]
                tampered_path["alignment_status"] = "reading_only"
                tampered_path["surface_boundaries"] = None
                proposal["paths"] = [tampered_path]

                manifest = route.route_workspace(workspace, apply=False)
                decision = manifest["cases"][0]
                self.assertEqual(decision["annotation_tier"], "gold")
                self.assertFalse(decision["semantic_validation_passed"])
                self.assertIn(
                    "alignment_not_established", decision["gold_reasons"]
                )
                self.assertIn(
                    "semantic_validation_failed", decision["gold_reasons"]
                )
                self.assertFalse(workspace.events_path.exists())
            finally:
                workspace.close()

    def test_unknown_or_incoherent_audit_input_fails_closed(self) -> None:
        record = queue_record("audit")
        path = {
            "path_id": "audit-path",
            "status": "acceptable",
            "surface_reference_id": "surface-0",
            "reading_boundaries": [4],
            "surface_boundaries": [3],
            "alignment_status": "aligned",
            "provenance": {"kind": "human", "future_field": True},
        }
        with self.assertRaisesRegex(
            serve.AnnotationError, "unsupported fields"
        ):
            serve.normalize_path(path, record)

        path["provenance"] = {
            "kind": "llm",
            "annotation_tier": "silver",
        }
        with self.assertRaisesRegex(
            serve.AnnotationError, "audit must define"
        ):
            serve.normalize_path(path, record)

        audit = {
            "routing_batch_id": "00000000-0000-4000-8000-000000000001",
            "annotation_tier": "silver",
            "llm_unmodified": True,
            "human_reviewed": False,
        }
        path["provenance"] = {"kind": "llm", **audit}
        with self.assertRaisesRegex(
            serve.AnnotationError, "human_reviewed must equal"
        ):
            serve.normalize_review(
                {
                    "path_set_status": "open",
                    "needs_adjudication": False,
                    "acceptable_paths": [path],
                    "reviewed_once": True,
                    **audit,
                },
                record,
                annotator_id="audit-test",
            )

        with self.assertRaisesRegex(
            serve.AnnotationError, "unsupported fields"
        ):
            serve.normalize_review(
                {
                    "path_set_status": "pending",
                    "needs_adjudication": False,
                    "acceptable_paths": [],
                    "unknown_audit": "silver",
                },
                record,
                annotator_id="audit-test",
            )


if __name__ == "__main__":
    unittest.main()
