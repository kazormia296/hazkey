#!/usr/bin/env python3
"""Atomically seal independently reviewed Mozc adoption corpus v2 inputs."""

from __future__ import annotations

import argparse
from collections import Counter
import ctypes
import errno
import json
import os
from pathlib import Path
import secrets
import stat
import sys
import tempfile
from typing import Any

try:
    from . import build_frozen_corpus_v2 as corpus
except ImportError:  # Direct execution from tools/dictionary.
    import build_frozen_corpus_v2 as corpus  # type: ignore[no-redef]


APPROVALS_SCHEMA = corpus.REVIEW_APPROVALS_SCHEMA
APPROVALS_NAME = corpus.REVIEW_APPROVALS_NAME
MANIFEST_NAME = "manifest.json"
NEAR_REVIEW_NAME = "near-duplicate-review.json"
AGGREGATE_NAME = "formal-corpus.tsv"
SEALED_DIRECTORY_PREFIX = "sealed-v2-sha256-"
RENAME_NOREPLACE = 1


def _render_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _render_jsonl(values: list[dict[str, object]]) -> bytes:
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


def _root_identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_nlink


def _open_pinned_root(root: Path) -> int:
    before = os.stat(root, follow_symlinks=False)
    if not stat.S_ISDIR(before.st_mode):
        raise ValueError("corpus root must be a real directory")
    descriptor = os.open(
        root,
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    opened = os.fstat(descriptor)
    if _root_identity(opened) != _root_identity(before):
        os.close(descriptor)
        raise ValueError("corpus root changed while it was opened")
    return descriptor


def _assert_pinned_root_path(root: Path, root_fd: int) -> None:
    """Fail if the caller-visible root path no longer names the pinned inode."""

    try:
        before = os.stat(root, follow_symlinks=False)
        pinned = os.fstat(root_fd)
        after = os.stat(root, follow_symlinks=False)
    except OSError as error:
        raise ValueError("corpus root path changed while sealing") from error
    if (
        not stat.S_ISDIR(before.st_mode)
        or _root_identity(before) != _root_identity(pinned)
        or _root_identity(after) != _root_identity(pinned)
    ):
        raise ValueError("corpus root path changed while sealing")


def _read_regular_at(root_fd: int, name: str, context: str) -> bytes:
    path = Path(name)
    if path.is_absolute() or len(path.parts) != 1 or path.name in {"", ".", ".."}:
        raise ValueError(f"{context} has an unsafe filename")
    descriptor = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=root_fd,
    )
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"{context} must be a regular non-symlink file")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        current = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mode,
            before.st_nlink,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        if identity != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mode,
            after.st_nlink,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) or identity != (
            current.st_dev,
            current.st_ino,
            current.st_size,
            current.st_mode,
            current.st_nlink,
            current.st_mtime_ns,
            current.st_ctime_ns,
        ):
            raise ValueError(f"{context} changed during the pinned read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _snapshot_inputs(
    root_fd: int,
    *,
    policy_name: str,
    approvals_name: str,
) -> dict[str, bytes]:
    names = [policy_name, approvals_name]
    names.extend(str(contract["tsv_path"]) for contract in corpus.COMPONENT_CONTRACTS)
    if len(names) != len(set(names)):
        raise ValueError("sealed input filenames must be unique")
    return {
        name: _read_regular_at(root_fd, name, f"sealed input {name}")
        for name in names
    }


def _provenance_record(
    row: dict[str, str],
    family_id: str,
    approval: dict[str, Any],
) -> dict[str, object]:
    return {
        "schema": corpus.PROVENANCE_SCHEMA,
        "case_id": row["id"],
        "family_id": family_id,
        "source": {
            "kind": "project-authored",
            "source_id": approval["source_id"],
            "author_id": approval["author_id"],
            "locator_sha256": corpus.case_locator_sha256(row, family_id),
            "license": "MIT",
            "new_holdout": True,
        },
        "rights": {
            "redistribution_approved": approval["redistribution_approved"],
            "privacy_reviewed": approval["privacy_reviewed"],
            "reviewer_id": approval["reviewer_id"],
        },
        "exposure": {
            "status": "sealed-for-b0-b1",
            "eligible_candidate_ids": list(corpus.ELIGIBLE_CANDIDATE_IDS),
            "disclosed_before_candidate_freezes": False,
        },
        "contamination": {
            "status": "no-known-overlap",
            "screened_against": list(corpus.REQUIRED_CONTAMINATION_SCREENS),
        },
    }


def _prepare_snapshot(
    *,
    snapshots: dict[str, bytes],
    policy_name: str,
    approvals_name: str,
    pilot_v1_manifest_path: Path,
) -> dict[str, bytes]:
    with tempfile.TemporaryDirectory(prefix="mozc-adoption-v2-verify-") as temporary:
        staging = Path(temporary)
        for name, data in snapshots.items():
            (staging / name).write_bytes(data)
        policy_path = staging / policy_name
        approvals_path = staging / approvals_name
        policy, policy_bytes = corpus.validate_policy(
            policy_path,
            require_ready=True,
            expected_manifest_name=MANIFEST_NAME,
        )
        approvals, near_approval, approval_bytes = corpus.load_review_approvals(
            approvals_path
        )
        pilot_rows, _ = corpus._validate_pilot_v1(
            pilot_v1_manifest_path,
            policy["exclusions"]["pilot_v1"],
        )

        all_rows: list[dict[str, str]] = []
        component_entries: list[dict[str, object]] = []
        generated = dict(snapshots)
        seen_ids: set[str] = set()
        seen_readings: set[str] = set()

        for contract in corpus.COMPONENT_CONTRACTS:
            component_id = str(contract["id"])
            approval = approvals[component_id]
            tsv_name = str(contract["tsv_path"])
            tsv_data = snapshots[tsv_name]
            tsv_sha256 = corpus.sha256_bytes(tsv_data)
            if tsv_sha256 != approval["tsv_sha256"]:
                raise ValueError(f"reviewed hash changed for {component_id}")
            rows = corpus._parse_tsv(tsv_data, f"reviewed {component_id} TSV")
            if len(rows) != contract["cases"] or Counter(
                row["category"] for row in rows
            ) != Counter({str(contract["category"]): int(contract["cases"])}):
                raise ValueError(f"reviewed {component_id} count/category mismatch")

            family_ids = [
                f"family-{component_id}-{position:04d}"
                for position in range(1, len(rows) + 1)
            ]
            if corpus.family_assignment_sha256(rows, family_ids) != approval[
                "family_assignment"
            ]["sha256"]:
                raise ValueError(f"reviewed family assignment changed for {component_id}")

            records: list[dict[str, object]] = []
            for position, (row, family_id) in enumerate(
                zip(rows, family_ids, strict=True), 1
            ):
                expected_id = f"{contract['id_prefix']}{position:04d}"
                if row["id"] != expected_id:
                    raise ValueError(f"reviewed {component_id} ID sequence mismatch")
                reading = corpus._canonical_reading(row["reading"])
                if row["id"] in seen_ids or reading in seen_readings:
                    raise ValueError(f"duplicate reviewed case at {row['id']}")
                if (
                    row["category"] != "protected"
                    and row["reading"] in row["expected"].split("|")
                ):
                    raise ValueError(
                        f"quality case permits unchanged reading for {row['id']!r}"
                    )
                seen_ids.add(row["id"])
                seen_readings.add(reading)
                records.append(_provenance_record(row, family_id, approval))
            provenance_data = _render_jsonl(records)
            generated[str(contract["provenance_path"])] = provenance_data
            component_entries.append(
                {
                    "id": component_id,
                    "tsv": {
                        "path": tsv_name,
                        "sha256": tsv_sha256,
                        "cases": contract["cases"],
                    },
                    "provenance": {
                        "path": contract["provenance_path"],
                        "sha256": corpus.sha256_bytes(provenance_data),
                        "records": contract["cases"],
                    },
                }
            )
            all_rows.extend(rows)

        if len(all_rows) != corpus.TOTAL_CASES or Counter(
            row["category"] for row in all_rows
        ) != Counter(corpus.ALL_CATEGORIES):
            raise ValueError("reviewed corpus does not match the formal count contract")
        pilot_readings = {
            corpus._canonical_reading(row["reading"]) for row in pilot_rows
        }
        pilot_fingerprints = {corpus._case_fingerprint(row) for row in pilot_rows}
        if any(
            corpus._canonical_reading(row["reading"]) in pilot_readings
            or corpus._case_fingerprint(row) in pilot_fingerprints
            for row in all_rows
        ):
            raise ValueError("reviewed corpus overlaps the v1 pilot")

        near_pairs = corpus.find_near_duplicate_pairs(all_rows, pilot_rows)
        if len(near_pairs) != near_approval["computed_pairs"] or near_pairs:
            raise ValueError(
                "computed near-duplicate pairs do not match the closed zero-pair review"
            )
        near_review = {
            "schema": corpus.NEAR_REVIEW_SCHEMA,
            "status": "closed",
            "reviewer_id": near_approval["reviewer_id"],
            "algorithm": near_approval["algorithm"],
            "pairs": [],
        }
        near_bytes = _render_json(near_review)
        generated[NEAR_REVIEW_NAME] = near_bytes

        aggregate_bytes = corpus._encode_rows(all_rows)
        manifest = {
            "schema": corpus.MANIFEST_SCHEMA,
            "policy": {
                "path": policy_name,
                "sha256": corpus.sha256_bytes(policy_bytes),
            },
            "review_approvals": {
                "path": approvals_name,
                "sha256": corpus.sha256_bytes(approval_bytes),
                "schema": corpus.REVIEW_APPROVALS_SCHEMA,
                "status": "approved",
            },
            "components": component_entries,
            "near_duplicate_review": {
                "path": NEAR_REVIEW_NAME,
                "sha256": corpus.sha256_bytes(near_bytes),
                "status": "closed",
            },
            "pilot_v1": {
                key: value
                for key, value in policy["exclusions"]["pilot_v1"].items()
                if key != "counted"
            },
            "aggregate": {
                "cases": corpus.TOTAL_CASES,
                "quality_cases": corpus.QUALITY_CASES,
                "sha256": corpus.sha256_bytes(aggregate_bytes),
                "categories": corpus.ALL_CATEGORIES,
                "protected_included_in_overall_quality_rates": False,
                "exact_pilot_overlap_cases": 0,
            },
        }
        generated[MANIFEST_NAME] = _render_json(manifest)
        generated[AGGREGATE_NAME] = aggregate_bytes

        for name, data in generated.items():
            (staging / name).write_bytes(data)
        rebuilt = corpus.build_aggregate(
            policy_path=policy_path,
            manifest_path=staging / MANIFEST_NAME,
            pilot_v1_manifest_path=pilot_v1_manifest_path,
        )
        if rebuilt != aggregate_bytes:
            raise ValueError("staged formal aggregate is not deterministic")
        return generated


def _prepare_outputs(
    *,
    policy_path: Path,
    approvals_path: Path,
    pilot_v1_manifest_path: Path,
) -> dict[str, bytes]:
    if (
        policy_path.parent != approvals_path.parent
        or approvals_path.name != APPROVALS_NAME
    ):
        raise ValueError("policy and review-approvals.json must share the corpus directory")
    root_fd = _open_pinned_root(policy_path.parent)
    try:
        snapshots = _snapshot_inputs(
            root_fd,
            policy_name=policy_path.name,
            approvals_name=approvals_path.name,
        )
    finally:
        os.close(root_fd)
    return _prepare_snapshot(
        snapshots=snapshots,
        policy_name=policy_path.name,
        approvals_name=approvals_path.name,
        pilot_v1_manifest_path=pilot_v1_manifest_path,
    )


def sealed_directory_name(generated: dict[str, bytes]) -> str:
    fingerprint = {
        name: corpus.sha256_bytes(data) for name, data in generated.items()
    }
    digest = corpus.sha256_bytes(corpus._canonical_json(fingerprint)).removeprefix(
        "sha256:"
    )
    return SEALED_DIRECTORY_PREFIX + digest


def _write_all_at(directory_fd: int, generated: dict[str, bytes]) -> None:
    for name, data in generated.items():
        path = Path(name)
        if path.is_absolute() or len(path.parts) != 1 or path.name in {"", ".", ".."}:
            raise ValueError(f"generated output has an unsafe filename: {name!r}")
        descriptor = os.open(
            name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        try:
            view = memoryview(data)
            while view:
                count = os.write(descriptor, view)
                if count <= 0:
                    raise OSError("short write while publishing sealed corpus")
                view = view[count:]
            os.fsync(descriptor)
            os.fchmod(descriptor, 0o444)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _verify_generation_at(
    root_fd: int,
    directory_name: str,
    generated: dict[str, bytes],
) -> None:
    directory_fd = os.open(
        directory_name,
        os.O_RDONLY
        | os.O_DIRECTORY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=root_fd,
    )
    try:
        metadata = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o555
        ):
            raise ValueError("sealed generation directory mode changed")
        if set(os.listdir(directory_fd)) != set(generated):
            raise ValueError("sealed generation file set changed")
        for name, expected in generated.items():
            actual = _read_regular_at(
                directory_fd,
                name,
                f"sealed generation output {name}",
            )
            file_metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_IMODE(file_metadata.st_mode) != 0o444 or actual != expected:
                raise ValueError(f"sealed generation output changed: {name}")
    finally:
        os.close(directory_fd)


def _rename_noreplace(
    root_fd: int,
    source_name: str,
    destination_name: str,
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOSYS, "renameat2 is required for no-replace publication")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    if (
        renameat2(
            root_fd,
            os.fsencode(source_name),
            root_fd,
            os.fsencode(destination_name),
            RENAME_NOREPLACE,
        )
        != 0
    ):
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), destination_name)


def _remove_staging(root_fd: int, name: str, generated: dict[str, bytes]) -> None:
    try:
        directory_fd = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=root_fd,
        )
    except FileNotFoundError:
        return
    try:
        os.fchmod(directory_fd, 0o700)
        for filename in generated:
            try:
                os.unlink(filename, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    os.rmdir(name, dir_fd=root_fd)
    os.fsync(root_fd)


def seal(
    *,
    policy_path: Path,
    approvals_path: Path,
    pilot_v1_manifest_path: Path,
) -> tuple[dict[str, bytes], Path]:
    if (
        policy_path.parent != approvals_path.parent
        or approvals_path.name != APPROVALS_NAME
    ):
        raise ValueError("policy and review-approvals.json must share the corpus directory")
    root = policy_path.parent
    root_fd = _open_pinned_root(root)
    staging_name = ".sealed-v2-staging-" + secrets.token_hex(12)
    generated: dict[str, bytes] = {}
    staging_created = False
    published = False
    try:
        snapshots = _snapshot_inputs(
            root_fd,
            policy_name=policy_path.name,
            approvals_name=approvals_path.name,
        )
        generated = _prepare_snapshot(
            snapshots=snapshots,
            policy_name=policy_path.name,
            approvals_name=approvals_path.name,
            pilot_v1_manifest_path=pilot_v1_manifest_path,
        )
        final_name = sealed_directory_name(generated)
        _assert_pinned_root_path(root, root_fd)
        os.mkdir(staging_name, 0o700, dir_fd=root_fd)
        staging_created = True
        staging_fd = os.open(
            staging_name,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=root_fd,
        )
        try:
            _write_all_at(staging_fd, generated)
            os.fsync(staging_fd)
            os.fchmod(staging_fd, 0o555)
            os.fsync(staging_fd)
        finally:
            os.close(staging_fd)
        _assert_pinned_root_path(root, root_fd)
        _rename_noreplace(root_fd, staging_name, final_name)
        os.fsync(root_fd)
        try:
            _assert_pinned_root_path(root, root_fd)
            _verify_generation_at(root_fd, final_name, generated)
            _assert_pinned_root_path(root, root_fd)
        except (OSError, ValueError):
            _remove_staging(root_fd, final_name, generated)
            raise
        published = True
        return generated, root / final_name
    finally:
        if not published and staging_created:
            _remove_staging(root_fd, staging_name, generated)
        elif staging_created:
            try:
                os.rmdir(staging_name, dir_fd=root_fd)
            except FileNotFoundError:
                pass
        os.close(root_fd)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--approvals", type=Path, required=True)
    parser.add_argument("--pilot-v1-manifest", type=Path, required=True)
    args = parser.parse_args()
    try:
        generated, generation = seal(
            policy_path=args.policy,
            approvals_path=args.approvals,
            pilot_v1_manifest_path=args.pilot_v1_manifest,
        )
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(
        f"{corpus.sha256_bytes(generated[AGGREGATE_NAME])} "
        f"{generation / AGGREGATE_NAME}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
