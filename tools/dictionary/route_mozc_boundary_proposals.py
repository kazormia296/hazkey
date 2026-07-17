#!/usr/bin/env python3
"""Route validated Mozc boundary proposals into Silver or Gold annotation work.

Silver adoption is deliberately conservative.  It never makes an annotation
export formally complete: adopted reviews remain open and explicitly record
that no human reviewed them.  Every decision is bound to the proposal journal
and generator provenance in a durable per-batch manifest.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
from typing import Any, Iterable
import uuid
import zipfile

try:
    from . import serve_mozc_boundary_annotations as serve
except ImportError:  # pragma: no cover - direct script execution
    import serve_mozc_boundary_annotations as serve


BATCH_SCHEMA = "hazkey.mozc-boundary-proposal-routing-batch.v1"
POLICY_VERSION = "mozc-boundary-silver-routing-v1"
BATCH_DIRECTORY = "proposal-routing-batches"


def _append_reason(reasons: list[str], reason: str) -> None:
    if reason not in reasons:
        reasons.append(reason)


def _semantic_path_key(path: dict[str, Any]) -> tuple[Any, ...]:
    surface_boundaries = path.get("surface_boundaries")
    return (
        path.get("surface_reference_id"),
        tuple(path.get("reading_boundaries", [])),
        None
        if surface_boundaries is None
        else tuple(surface_boundaries),
    )


def _chunks_cover(text: str, boundaries: Iterable[int]) -> bool:
    points = [0, *boundaries, len(text)]
    chunks = [
        text[points[index] : points[index + 1]]
        for index in range(len(points) - 1)
    ]
    return bool(chunks) and all(chunks) and "".join(chunks) == text


def _generator_binding(proposal: dict[str, Any] | None) -> dict[str, Any] | None:
    if proposal is None:
        return None
    generator = proposal.get("generator")
    if not isinstance(generator, dict):
        generator = {}
    return {
        "model": generator.get("model"),
        "reasoning_effort": generator.get("reasoning_effort"),
        "prompt_version": generator.get("prompt_version"),
        "prompt_sha256": generator.get("prompt_sha256"),
    }


def _proposal_binding(proposal: dict[str, Any] | None) -> dict[str, Any] | None:
    if proposal is None:
        return None
    generator = _generator_binding(proposal)
    return {
        "proposal_id": proposal.get("proposal_id"),
        "created_at": proposal.get("created_at"),
        "source_revision": proposal.get("review_revision"),
        "effective_reading_sha256": proposal.get(
            "effective_reading_sha256"
        ),
        "generator": generator,
        "record_sha256": serve.sha256_bytes(
            serve.canonical_json_bytes(proposal)
        ),
        "raw_output_sha256": serve.sha256_bytes(
            serve.canonical_json_bytes(proposal.get("raw_output"))
        ),
    }


def _generator_is_bound(proposal: dict[str, Any]) -> bool:
    binding = _generator_binding(proposal)
    if binding is None:
        return False
    if not all(
        isinstance(binding[key], str) and bool(binding[key])
        for key in ("model", "reasoning_effort", "prompt_version")
    ):
        return False
    prompt_sha256 = binding["prompt_sha256"]
    return (
        isinstance(prompt_sha256, str)
        and serve.SHA256_URI.fullmatch(prompt_sha256) is not None
    )


def _classify_proposal(
    workspace: serve.Workspace,
    record: dict[str, Any],
    review: dict[str, Any],
    proposal: dict[str, Any] | None,
) -> tuple[str, list[str], list[dict[str, Any]], bool]:
    """Return tier, Gold reasons, normalized paths, and validation status."""

    reasons: list[str] = []
    reading = serve._effective_reading(record, review)
    is_long = (
        record["category"] == "long-structural"
        or len(reading) >= serve.LONG_READING_THRESHOLD
    )
    if review.get("corrected_reading") is not None:
        _append_reason(reasons, "reading_corrected")
    if is_long:
        _append_reason(reasons, "long_reading")
    if (
        review.get("path_set_status") != "pending"
        or review.get("reviewed_once") is True
        or review.get("needs_adjudication") is True
        or bool(review.get("acceptable_paths"))
    ):
        _append_reason(reasons, "existing_review_state")
    if proposal is None:
        _append_reason(reasons, "missing_proposal")
        return "gold", reasons, [], False

    if proposal.get("schema") != serve.PROPOSAL_SCHEMA:
        _append_reason(reasons, "unsupported_proposal_schema")
    if proposal.get("source_row_sha256") != record["source"]["row_sha256"]:
        _append_reason(reasons, "source_row_mismatch")
    if proposal.get("review_revision") != review.get("revision"):
        _append_reason(reasons, "source_revision_mismatch")
    if proposal.get("effective_reading_sha256") != serve._effective_reading_sha256(
        reading
    ):
        _append_reason(reasons, "effective_reading_mismatch")
    if not isinstance(proposal.get("created_at"), str) or not proposal.get(
        "created_at"
    ):
        _append_reason(reasons, "missing_proposal_created_at")
    if not _generator_is_bound(proposal):
        _append_reason(reasons, "incomplete_generator_provenance")
    if proposal.get("ambiguous") is not False:
        _append_reason(reasons, "ambiguous")
    discarded = proposal.get("discarded_candidates")
    if not isinstance(discarded, list) or discarded:
        _append_reason(reasons, "discarded_candidate")

    raw_paths = proposal.get("paths")
    if not isinstance(raw_paths, list) or not raw_paths:
        _append_reason(reasons, "paths_empty")
        raw_paths = []
    elif len(raw_paths) != 1:
        _append_reason(reasons, "multiple_paths")

    normalized_paths: list[dict[str, Any]] = []
    try:
        normalized_paths = [
            serve.normalize_path(path, record, reading=reading)
            for path in raw_paths
        ]
    except serve.AnnotationError:
        _append_reason(reasons, "path_contract_invalid")

    if normalized_paths:
        keys = [_semantic_path_key(path) for path in normalized_paths]
        if len(keys) != len(set(keys)):
            _append_reason(reasons, "duplicate_paths")
        for path in normalized_paths:
            surface_id = path["surface_reference_id"]
            surface_index = int(surface_id.removeprefix("surface-"))
            surface = record["source"]["expected_surfaces"][surface_index]
            if (
                path["alignment_status"] != "aligned"
                or path["surface_boundaries"] is None
            ):
                _append_reason(reasons, "alignment_not_established")
                continue
            if not _chunks_cover(reading, path["reading_boundaries"]):
                _append_reason(reasons, "reading_not_fully_covered")
            if not _chunks_cover(surface, path["surface_boundaries"]):
                _append_reason(reasons, "surface_not_fully_covered")

    semantic_validation_passed = False
    try:
        (
            validated_ambiguous,
            validated_reasons,
            validated_paths,
            validated_discarded,
        ) = workspace._validate_llm_output(
            record["id"],
            proposal.get("raw_output"),
            proposal.get("proposal_id"),
            reading=reading,
        )
        semantic_validation_passed = (
            validated_ambiguous == proposal.get("ambiguous")
            and validated_reasons == proposal.get("ambiguity_reasons")
            and validated_paths == normalized_paths
            and validated_discarded == proposal.get("discarded_candidates")
        )
    except (serve.AnnotationError, TypeError, ValueError):
        semantic_validation_passed = False
    if not semantic_validation_passed:
        _append_reason(reasons, "semantic_validation_failed")

    return (
        "silver" if not reasons else "gold",
        reasons,
        normalized_paths,
        semantic_validation_passed,
    )


def _case_ids_for_batch(
    workspace: serve.Workspace, requested_case_ids: Iterable[str] | None
) -> list[str]:
    if requested_case_ids is None:
        return [
            record["id"]
            for record in workspace.queue.records
            if workspace._current_proposals_locked(record["id"])
        ]
    result: list[str] = []
    seen: set[str] = set()
    for raw_case_id in requested_case_ids:
        case_id = serve._require_text(raw_case_id, "case_id")
        if case_id in seen:
            raise serve.AnnotationError("case_ids contains duplicates")
        if case_id not in workspace.queue.by_id:
            raise serve.AnnotationError(f"unknown case {case_id!r}")
        seen.add(case_id)
        result.append(case_id)
    return result


def _make_updated_review(
    workspace: serve.Workspace,
    record: dict[str, Any],
    previous: dict[str, Any],
    decision: dict[str, Any],
    *,
    updated_at: str,
) -> dict[str, Any] | None:
    tier = decision["annotation_tier"]
    audit = {
        "routing_batch_id": decision["routing_batch_id"],
        "annotation_tier": tier,
        "llm_unmodified": decision["llm_unmodified"],
        "human_reviewed": decision["human_reviewed"],
    }
    if tier == "silver":
        paths = deepcopy(decision["_normalized_paths"])
        for path in paths:
            path["status"] = "acceptable"
            serve._stamp_path_annotation_audit(path, audit)
        payload = {
            "path_set_status": "open",
            "needs_adjudication": False,
            "acceptable_paths": paths,
            "notes": previous.get("notes"),
            "reviewed_once": False,
            **audit,
        }
    else:
        can_route = (
            previous["path_set_status"] == "pending"
            and previous["reviewed_once"] is False
            and previous["needs_adjudication"] is False
            and not previous["acceptable_paths"]
        )
        if not can_route:
            return None
        payload = {
            "corrected_reading": previous.get("corrected_reading"),
            "path_set_status": "pending",
            "needs_adjudication": True,
            "acceptable_paths": [],
            "notes": previous.get("notes"),
            "reviewed_once": False,
            **audit,
        }
    normalized = serve.normalize_review(
        payload,
        record,
        previous=previous,
        annotator_id=workspace.annotator_id,
    )
    normalized.update(
        {
            "revision": previous["revision"] + 1,
            "reviewed_once": False,
            "updated_at": updated_at,
            "imported": deepcopy(previous.get("imported", {})),
        }
    )
    return normalized


def route_workspace(
    workspace: serve.Workspace,
    *,
    case_ids: Iterable[str] | None = None,
    apply: bool,
    batch_id: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    if type(apply) is not bool:
        raise serve.AnnotationError("apply must be boolean")
    if batch_id is None:
        batch_id = str(uuid.uuid4())
    else:
        batch_id = serve._require_text(batch_id, "batch_id")
        try:
            parsed_batch_id = uuid.UUID(batch_id)
        except ValueError as exc:
            raise serve.AnnotationError(
                "batch_id must be a canonical UUID"
            ) from exc
        if str(parsed_batch_id) != batch_id:
            raise serve.AnnotationError("batch_id must be a canonical UUID")
    if created_at is None:
        created_at = serve.now_iso8601()

    with workspace.lock:
        if workspace._closed:
            raise serve.AnnotationError("annotation workspace is closed")
        selected_case_ids = _case_ids_for_batch(workspace, case_ids)
        if not selected_case_ids:
            raise serve.AnnotationError("no current proposals to route")
        proposal_journal = (
            workspace.proposals_path.read_bytes()
            if workspace.proposals_path.exists()
            else b""
        )
        proposal_journal_sha256 = serve.sha256_bytes(proposal_journal)
        decisions: list[dict[str, Any]] = []
        for case_id in selected_case_ids:
            record = workspace.queue.by_id[case_id]
            review = workspace.reviews[case_id]
            current = workspace._current_proposals_locked(case_id)
            proposal = current[-1] if current else None
            tier, reasons, paths, semantic_valid = _classify_proposal(
                workspace, record, review, proposal
            )
            decisions.append(
                {
                    "case_id": case_id,
                    "routing_batch_id": batch_id,
                    "annotation_tier": tier,
                    "llm_unmodified": tier == "silver",
                    "human_reviewed": bool(review["reviewed_once"]),
                    "gold_reasons": reasons,
                    "semantic_validation_passed": semantic_valid,
                    "source": {
                        "row_sha256": record["source"]["row_sha256"],
                        "workspace_review_revision": review["revision"],
                        "effective_reading_sha256": (
                            serve._effective_reading_sha256(
                                serve._effective_reading(record, review)
                            )
                        ),
                    },
                    "proposal": _proposal_binding(proposal),
                    "event_id": None,
                    "applied_review_revision": review["revision"],
                    "_normalized_paths": paths,
                }
            )

        manifest_cases = []
        for decision in decisions:
            public = {
                key: deepcopy(value)
                for key, value in decision.items()
                if not key.startswith("_")
            }
            manifest_cases.append(public)
        generator_bindings = {
            json.dumps(
                decision["proposal"]["generator"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            for decision in decisions
            if decision["proposal"] is not None
        }
        manifest: dict[str, Any] = {
            "schema": BATCH_SCHEMA,
            "policy_version": POLICY_VERSION,
            "batch_id": batch_id,
            "state": "planned" if not apply else "prepared",
            "created_at": created_at,
            "queue_sha256": workspace.queue.sha256,
            "proposal_journal": {
                "path": workspace.proposals_path.name,
                "sha256": proposal_journal_sha256,
                "bytes": len(proposal_journal),
            },
            "generator_bindings": [
                json.loads(value) for value in sorted(generator_bindings)
            ],
            "policy": {
                "long_reading_threshold": serve.LONG_READING_THRESHOLD,
                "silver_path_count": 1,
                "requires_aligned_full_coverage": True,
                "requires_no_discard": True,
                "requires_non_ambiguous": True,
                "silver_review_status": "open",
                "silver_reviewed_once": False,
            },
            "counts": {
                "cases": len(decisions),
                "silver": sum(
                    item["annotation_tier"] == "silver" for item in decisions
                ),
                "gold": sum(
                    item["annotation_tier"] == "gold" for item in decisions
                ),
            },
            "cases": manifest_cases,
        }
        if not apply:
            return manifest

        batch_directory = workspace.root / BATCH_DIRECTORY / batch_id
        batch_directory.mkdir(parents=True, exist_ok=False)
        manifest_path = batch_directory / "manifest.json"

        updated_reviews: dict[str, dict[str, Any]] = {}
        events: list[dict[str, Any]] = []
        for decision, public in zip(decisions, manifest_cases, strict=True):
            case_id = decision["case_id"]
            record = workspace.queue.by_id[case_id]
            previous = workspace.reviews[case_id]
            updated = _make_updated_review(
                workspace,
                record,
                previous,
                decision,
                updated_at=created_at,
            )
            if updated is None:
                continue
            event_id = str(uuid.uuid4())
            decision["event_id"] = event_id
            decision["applied_review_revision"] = updated["revision"]
            public["event_id"] = event_id
            public["applied_review_revision"] = updated["revision"]
            updated_reviews[case_id] = updated
            events.append(
                {
                    "schema": serve.EVENT_SCHEMA,
                    "event_id": event_id,
                    "created_at": created_at,
                    "queue_sha256": workspace.queue.sha256,
                    "case_id": case_id,
                    "review": updated,
                    "action": {
                        "kind": (
                            "silver_auto_adopt"
                            if decision["annotation_tier"] == "silver"
                            else "gold_review_route"
                        ),
                        "batch_id": batch_id,
                        "routing_batch_id": batch_id,
                        "annotation_tier": decision["annotation_tier"],
                        "llm_unmodified": decision["llm_unmodified"],
                        "human_reviewed": decision["human_reviewed"],
                        "proposal_id": (
                            None
                            if decision["proposal"] is None
                            else decision["proposal"]["proposal_id"]
                        ),
                        "gold_reasons": decision["gold_reasons"],
                    },
                }
            )

        serve._atomic_write(
            manifest_path, serve.canonical_json_bytes(manifest)
        )
        if events:
            with workspace.events_path.open("ab") as output:
                output.write(serve.canonical_jsonl(events))
                output.flush()
                os.fsync(output.fileno())
            workspace.reviews.update(updated_reviews)
            workspace._write_snapshot()
        event_journal = (
            workspace.events_path.read_bytes()
            if workspace.events_path.exists()
            else b""
        )
        manifest.update(
            {
                "state": "applied",
                "applied_at": serve.now_iso8601(),
                "review_event_journal": {
                    "path": workspace.events_path.name,
                    "sha256": serve.sha256_bytes(event_journal),
                    "bytes": len(event_journal),
                    "events_appended": len(events),
                },
                "manifest_path": str(
                    manifest_path.relative_to(workspace.root)
                ),
            }
        )
        serve._atomic_write(
            manifest_path, serve.canonical_json_bytes(manifest)
        )
        return deepcopy(manifest)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Route current Mozc boundary proposals to Silver or Gold."
    )
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--workbook", type=Path)
    parser.add_argument("--annotator-id", default="llm-silver-router")
    parser.add_argument("--case-id", action="append", dest="case_ids")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="append review events and persist an applied batch manifest",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        queue = serve.load_queue(args.queue)
        workspace = serve.Workspace(
            queue,
            args.workspace,
            workbook_path=args.workbook,
            annotator_id=args.annotator_id,
            proposal_backend=None,
            proposal_backend_message="Codex is disabled during proposal routing",
        )
        try:
            manifest = route_workspace(
                workspace,
                case_ids=args.case_ids,
                apply=args.apply,
            )
        finally:
            workspace.close()
    except (serve.AnnotationError, OSError, ValueError, zipfile.BadZipFile) as exc:
        print(f"error: {exc}", file=os.sys.stderr)
        return 2
    print(
        json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
