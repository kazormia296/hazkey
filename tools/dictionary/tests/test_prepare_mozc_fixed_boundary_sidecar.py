from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from tools.dictionary import prepare_mozc_fixed_boundary_sidecar as prepare


def sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def render(records: list[dict[str, object]]) -> bytes:
    return b"".join(
        json.dumps(
            record,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
        for record in records
    )


def measurement() -> dict[str, object]:
    return {
        "warmups": 0,
        "iterations": 1,
        "latency_ms": {
            "median": 1.0,
            "p95": 1.0,
            "minimum": 1.0,
            "maximum": 1.0,
            "samples": [1.0],
        },
        "rss": {"before_kib": 100, "after_kib": 100},
        "backend_diagnostics": {
            "process_launch_count": 1,
            "cleanup_failure_count": 0,
        },
    }


def policy(*, context: str = "empty") -> dict[str, object]:
    return {
        "learning": False,
        "context": context,
        "zenzai": {
            "enabled": False,
            "model_path": None,
            "model_size_bytes": None,
            "model_sha256": None,
            "inference_limit": None,
            "resolved_device": None,
        },
    }


def result(case_id: str, reading: str, count: int) -> dict[str, object]:
    return {
        "schema": prepare.INPUT_SCHEMA_V6,
        "conversion_path": prepare.CONVERSION_PATH,
        "id": case_id,
        "reading": reading,
        "category": "fixture",
        "backend": "Mozc",
        "backend_version": "fixture-v1",
        "converter_backend": "mozc",
        "source_ref": "a" * 40,
        "resource": {
            "kind": "mozc_runtime_inputs",
            "path": "/fixture/mozc",
            "fingerprint": "sha256:" + "b" * 64,
        },
        "producer": {
            "path": "/fixture/ab-probe",
            "size_bytes": 123,
            "sha256": "sha256:" + "c" * 64,
        },
        "quality_policy": policy(),
        "top_k": 2,
        "corpus": {"sha256": "sha256:" + "d" * 64, "cases": 2},
        "candidates": [
            {
                "text": "候補",
                "rank": 1,
                "consuming_count": count,
                "provenance": "standard",
                "ranking_influence": "standard",
                "zenzai_score": None,
                "zenzai_score_token_count": None,
                "zenzai_score_scope": None,
            }
        ],
        "composition_span": {
            "start": 0,
            "count": len(reading),
            "unit": "composition_element",
        },
        "measurement": measurement(),
    }


def records() -> list[dict[str, object]]:
    return [result("case-b", "きょうは", 3), result("case-a", "あめ", 2)]


class PrepareMozcFixedBoundarySidecarTests(unittest.TestCase):
    def test_derives_top1_counts_preserves_order_and_binds_exact_bytes(self) -> None:
        raw = render(records())
        output = prepare.prepare_sidecar_bytes(raw)
        sidecar = [json.loads(line) for line in output.decode().splitlines()]

        self.assertEqual([case["id"] for case in sidecar], ["case-b", "case-a"])
        self.assertEqual([case["consuming_count"] for case in sidecar], [3, 2])
        self.assertTrue(all(set(case) == prepare.SIDECAR_FIELDS for case in sidecar))
        self.assertTrue(
            all(set(case["origin"]) == prepare.ORIGIN_FIELDS for case in sidecar)
        )
        self.assertEqual(sidecar[0]["reading_sha256"], sha256("きょうは".encode()))
        self.assertEqual(
            sidecar[0]["origin"],
            {
                "schema": prepare.INPUT_SCHEMA_V6,
                "sha256": sha256(raw),
                "cases": 2,
                "converter_backend": "mozc",
                "conversion_path": "segment_candidates",
            },
        )
        self.assertEqual(sidecar[0]["origin"], sidecar[1]["origin"])

    def test_rejects_empty_candidates(self) -> None:
        values = records()
        values[0]["candidates"] = []
        with self.assertRaisesRegex(ValueError, "contain a Mozc Top-1 boundary"):
            prepare.prepare_sidecar_bytes(render(values))

    def test_rejects_out_of_range_top1_count(self) -> None:
        values = records()
        values[0]["candidates"][0]["consuming_count"] = 99
        with self.assertRaisesRegex(ValueError, "must not exceed composition_span"):
            prepare.prepare_sidecar_bytes(render(values))

    def test_rejects_duplicate_ids(self) -> None:
        values = records()
        values[1]["id"] = values[0]["id"]
        with self.assertRaisesRegex(ValueError, "duplicate id"):
            prepare.prepare_sidecar_bytes(render(values))

    def test_rejects_non_v6_schema(self) -> None:
        values = records()
        values[0]["schema"] = "hazkey.ab-probe-result.v7"
        with self.assertRaisesRegex(ValueError, "schema must be .*v6"):
            prepare.prepare_sidecar_bytes(render(values))

    def test_cli_writes_same_bytes(self) -> None:
        raw = render(records())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "mozc.jsonl"
            output = root / "fixed.jsonl"
            source.write_bytes(raw)
            self.assertEqual(
                prepare.main(
                    ["--mozc-results", str(source), "--output", str(output)]
                ),
                0,
            )
            self.assertEqual(output.read_bytes(), prepare.prepare_sidecar_bytes(raw))

    def test_cli_refuses_to_replace_existing_output_or_symlink(self) -> None:
        raw = render(records())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "mozc.jsonl"
            source.write_bytes(raw)
            for output in (root / "existing.jsonl", root / "link.jsonl"):
                target = root / "target.jsonl"
                target.write_bytes(b"sealed\n")
                if output.name == "existing.jsonl":
                    output.write_bytes(b"existing\n")
                else:
                    output.symlink_to(target)
                self.assertEqual(
                    prepare.main(
                        ["--mozc-results", str(source), "--output", str(output)]
                    ),
                    2,
                )
                self.assertEqual(target.read_bytes(), b"sealed\n")


if __name__ == "__main__":
    unittest.main()
