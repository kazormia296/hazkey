from __future__ import annotations

from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT))

from tools.dictionary import blind_conversion_ab  # noqa: E402


SCRIPT = REPOSITORY_ROOT / "tools/dictionary/blind_conversion_ab.py"


def make_result(
    case_id: str,
    reading: str,
    category: str,
    converter_backend: str,
    candidates: list[str],
    *,
    corpus_sha256: str,
    corpus_cases: int,
    top_k: int = 2,
    source_ref: str = "0123456789abcdef",
    backend_version: str = "0.2.1",
    warmups: int = 1,
) -> dict[str, object]:
    samples = [1.0, 2.0]
    resource_kind = (
        "hazkey_dictionary"
        if converter_backend == "hazkey"
        else "mozc_runtime_inputs"
    )
    return {
        "schema": "hazkey.ab-probe-result.v3",
        "id": case_id,
        "reading": reading,
        "category": category,
        "backend": "hazkey-server",
        "backend_version": backend_version,
        "source_ref": source_ref,
        "converter_backend": converter_backend,
        "resource": {
            "kind": resource_kind,
            "path": f"/fixtures/{converter_backend}",
            "fingerprint": f"sha256:{converter_backend}-fixture",
        },
        "top_k": top_k,
        "corpus": {"sha256": corpus_sha256, "cases": corpus_cases},
        "candidates": candidates,
        "measurement": {
            "warmups": warmups,
            "iterations": len(samples),
            "latency_ms": {
                "median": statistics.median(samples),
                "p95": samples[math.ceil(len(samples) * 0.95) - 1],
                "minimum": min(samples),
                "maximum": max(samples),
                "samples": samples,
            },
            "rss": {"before_kib": 100, "after_kib": 120},
        },
    }


def write_jsonl(path: Path, values: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(value, ensure_ascii=False) + "\n" for value in values),
        encoding="utf-8",
    )


class BlindConversionABTests(unittest.TestCase):
    def make_inputs(self, directory: Path, count: int = 5) -> tuple[Path, Path, Path]:
        corpus = directory / "corpus.tsv"
        rows = [
            (
                f"original-{index}",
                f"よみ{index}",
                f"期待{index}|別解{index}",
                "sentence" if index % 2 == 0 else "proper-noun",
            )
            for index in range(count)
        ]
        corpus.write_text(
            "id\treading\texpected\tcategory\tnotes\n"
            + "".join(
                f"{case_id}\t{reading}\t{expected}\t{category}\tfixture\n"
                for case_id, reading, expected, category in rows
            ),
            encoding="utf-8",
        )
        corpus_sha256 = "sha256:" + hashlib.sha256(corpus.read_bytes()).hexdigest()
        hazkey = directory / "hazkey.jsonl"
        mozc = directory / "mozc.jsonl"
        write_jsonl(
            hazkey,
            [
                make_result(
                    case_id,
                    reading,
                    category,
                    "hazkey",
                    [expected.split("|")[0], f"H候補{index}"],
                    corpus_sha256=corpus_sha256,
                    corpus_cases=len(rows),
                )
                for index, (case_id, reading, expected, category) in enumerate(rows)
            ],
        )
        # Reversed order proves matching is by case ID, not line position.
        write_jsonl(
            mozc,
            [
                make_result(
                    case_id,
                    reading,
                    category,
                    "mozc",
                    [f"M候補{index}", expected.split("|")[0]],
                    corpus_sha256=corpus_sha256,
                    corpus_cases=len(rows),
                )
                for index, (case_id, reading, expected, category) in reversed(
                    list(enumerate(rows))
                )
            ],
        )
        return corpus, hazkey, mozc

    def prepare_files(
        self, directory: Path, *, seed: str = "11" * 32, count: int = 5
    ) -> tuple[Path, Path, Path, Path, Path]:
        corpus, hazkey, mozc = self.make_inputs(directory, count=count)
        packet = directory / "packet"
        blind_conversion_ab.prepare(corpus, hazkey, mozc, seed, packet)
        return corpus, hazkey, mozc, packet, packet / blind_conversion_ab.KEY_NAME

    def test_prepare_is_reproducible_balanced_and_review_does_not_leak(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            corpus, hazkey, mozc = self.make_inputs(directory)
            review_bytes, key = blind_conversion_ab.build_review_and_key(
                corpus, hazkey, mozc, "11" * 32
            )
            repeated_bytes, repeated_key = blind_conversion_ab.build_review_and_key(
                corpus, hazkey, mozc, "11" * 32
            )
            changed_bytes, _ = blind_conversion_ab.build_review_and_key(
                corpus, hazkey, mozc, "22" * 32
            )

        self.assertEqual(review_bytes, repeated_bytes)
        self.assertEqual(key, repeated_key)
        self.assertNotEqual(review_bytes, changed_bytes)
        records = [json.loads(line) for line in review_bytes.splitlines()]
        self.assertEqual(len(records), 5)
        for record in records:
            self.assertEqual(
                set(record),
                {"schema", "case", "reading", "category", "x", "y", "integrity"},
            )
            self.assertTrue(record["case"].startswith("blind-"))
            self.assertNotIn(
                record["case"],
                {f"original-{index}" for index in range(5)},
            )
            self.assertNotIn("expected", record)
            self.assertNotIn("backend", record)
            self.assertNotIn("source_ref", record)
        self.assertEqual(
            sum(key["placement"]["x"].values()),
            5,
        )
        x_counts = list(key["placement"]["x"].values())
        self.assertLessEqual(abs(x_counts[0] - x_counts[1]), 1)
        self.assertEqual(
            key["placement"]["x"]["hazkey"],
            key["placement"]["y"]["mozc"],
        )
        self.assertEqual(
            key["placement"]["x"]["mozc"],
            key["placement"]["y"]["hazkey"],
        )
        for category in {case["category"] for case in key["cases"]}:
            category_cases = [
                case for case in key["cases"] if case["category"] == category
            ]
            category_x = {
                backend: sum(
                    case["x_backend"] == backend for case in category_cases
                )
                for backend in ("hazkey", "mozc")
            }
            self.assertLessEqual(
                abs(category_x["hazkey"] - category_x["mozc"]), 1
            )

    def test_score_unblinds_all_judgment_kinds_and_categories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            _, _, _, packet, key_path = self.prepare_files(directory, count=4)
            key = json.loads(key_path.read_text(encoding="utf-8"))
            choices = ["x", "y", "tie", "both_bad"]
            judgments = directory / "judgments.jsonl"
            write_jsonl(
                judgments,
                [
                    {
                        "schema": blind_conversion_ab.JUDGMENT_SCHEMA,
                        "case": case["case"],
                        "judgment": choices[index],
                    }
                    for index, case in enumerate(key["cases"])
                ],
            )
            os.chmod(judgments, 0o600)

            report = blind_conversion_ab.score(packet, judgments)

        expected_wins = {"hazkey": 0, "mozc": 0}
        expected_losses = {"hazkey": 0, "mozc": 0}
        for case, judgment in zip(key["cases"], choices, strict=True):
            if judgment in {"x", "y"}:
                winner_side = f"{judgment}_backend"
                loser_side = "y_backend" if judgment == "x" else "x_backend"
                expected_wins[case[winner_side]] += 1
                expected_losses[case[loser_side]] += 1
        self.assertEqual(report["schema"], blind_conversion_ab.REPORT_SCHEMA)
        self.assertEqual(report["source_ref"], "0123456789abcdef")
        self.assertEqual(report["cases"], 4)
        self.assertEqual(
            {backend["converter_backend"] for backend in report["backends"]},
            {"hazkey", "mozc"},
        )
        for backend in report["backends"]:
            self.assertEqual(
                backend["measurement"], {"warmups": 1, "iterations": 2}
            )
            self.assertIn("fingerprint", backend["resource"])
            self.assertTrue(backend["run_sha256"].startswith("sha256:"))
        self.assertTrue(report["judgments_sha256"].startswith("sha256:"))
        self.assertTrue(report["unblind_key_integrity"].startswith("sha256:"))
        self.assertEqual(
            report["human_preference"]["judgment_counts"],
            {"both_bad": 1, "tie": 1, "x": 1, "y": 1},
        )
        for backend in ("hazkey", "mozc"):
            self.assertEqual(
                report["human_preference"]["by_backend"][backend]["wins"],
                expected_wins[backend],
            )
            self.assertEqual(
                report["human_preference"]["by_backend"][backend]["losses"],
                expected_losses[backend],
            )
            outcomes = report["human_preference"]["by_backend"][backend]
            self.assertEqual(outcomes["ties"], 1)
            self.assertEqual(outcomes["both_bad"], 1)
            self.assertEqual(outcomes["all_cases"], 4)
            self.assertIn("decisive_win_rate_ci95", outcomes)
            self.assertIn("net_preference_rate_all_cases", outcomes)
        self.assertEqual(len(report["unblinded_cases"]), 4)
        self.assertEqual(
            {case["original_id"] for case in report["unblinded_cases"]},
            {f"original-{index}" for index in range(4)},
        )
        self.assertEqual(
            set(report["human_preference"]["by_category"]),
            {"proper-noun", "sentence"},
        )
        objective = report["objective_quality"]
        self.assertEqual(objective["top_k"], 2)
        self.assertEqual(objective["by_backend"]["hazkey"]["top1_hits"], 4)
        self.assertEqual(objective["by_backend"]["hazkey"]["top2_hits"], 4)
        self.assertEqual(objective["by_backend"]["mozc"]["top1_hits"], 0)
        self.assertEqual(objective["by_backend"]["mozc"]["top2_hits"], 4)
        comparison = objective["paired_comparison"]
        self.assertEqual(comparison["backends"], {"a": "hazkey", "b": "mozc"})
        self.assertEqual(comparison["wins"], {"a": 4, "b": 0, "ties": 0})

    def test_prepare_rejects_case_provenance_mismatch(self) -> None:
        mutations = {
            "schema": lambda payload: payload.__setitem__(
                "schema", "hazkey.ab-probe-result.v2"
            ),
            "case": lambda payload: payload.__setitem__("id", "unknown-case"),
            "reading": lambda payload: payload.__setitem__("reading", "べつ"),
            "category": lambda payload: payload.__setitem__("category", "wrong"),
            "source_ref": lambda payload: payload.__setitem__(
                "source_ref", "different-source"
            ),
        }
        for name, mutate in mutations.items():
            with (
                self.subTest(name=name),
                tempfile.TemporaryDirectory() as temporary_directory,
            ):
                directory = Path(temporary_directory)
                corpus, hazkey, mozc = self.make_inputs(directory)
                lines = [json.loads(line) for line in mozc.read_text().splitlines()]
                mutate(lines[0])
                if name == "schema":
                    # Keep v2 valid so the explicit v3-only gate is reached.
                    del lines[0]["reading"]
                    del lines[0]["top_k"]
                    del lines[0]["corpus"]
                write_jsonl(mozc, lines)
                with self.assertRaises(ValueError):
                    blind_conversion_ab.build_review_and_key(
                        corpus, hazkey, mozc, "11" * 32
                    )

    def test_prepare_rejects_corpus_and_top_k_mismatch(self) -> None:
        for field in ("corpus", "top_k"):
            with (
                self.subTest(field=field),
                tempfile.TemporaryDirectory() as temporary_directory,
            ):
                directory = Path(temporary_directory)
                corpus, hazkey, mozc = self.make_inputs(directory)
                lines = [json.loads(line) for line in mozc.read_text().splitlines()]
                for payload in lines:
                    if field == "corpus":
                        payload["corpus"]["sha256"] = "sha256:" + "0" * 64
                    else:
                        payload["top_k"] = 3
                write_jsonl(mozc, lines)
                with self.assertRaises(ValueError):
                    blind_conversion_ab.build_review_and_key(
                        corpus, hazkey, mozc, "11" * 32
                    )

    def test_prepare_rejects_mismatched_measurement_contract(self) -> None:
        mutations = {
            "backend_version": lambda payload: payload.__setitem__(
                "backend_version", "different"
            ),
            "warmups": lambda payload: payload["measurement"].__setitem__(
                "warmups", 2
            ),
            "iterations": lambda payload: payload["measurement"].__setitem__(
                "iterations", 3
            ),
        }
        for field, mutate in mutations.items():
            with (
                self.subTest(field=field),
                tempfile.TemporaryDirectory() as temporary_directory,
            ):
                directory = Path(temporary_directory)
                corpus, hazkey, mozc = self.make_inputs(directory)
                lines = [json.loads(line) for line in mozc.read_text().splitlines()]
                for payload in lines:
                    mutate(payload)
                    if field == "iterations":
                        latency = payload["measurement"]["latency_ms"]
                        latency["samples"] = [1.0, 2.0, 3.0]
                        latency["median"] = 2.0
                        latency["p95"] = 3.0
                        latency["maximum"] = 3.0
                write_jsonl(mozc, lines)
                with self.assertRaises(ValueError):
                    blind_conversion_ab.build_review_and_key(
                        corpus, hazkey, mozc, "11" * 32
                    )

    def test_prepare_atomically_publishes_private_packet(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            corpus, hazkey, mozc = self.make_inputs(directory)
            packet = directory / "packet"
            packet.mkdir()
            marker = packet / "do-not-replace"
            marker.write_text("present\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
                blind_conversion_ab.prepare(
                    corpus, hazkey, mozc, "11" * 32, packet
                )
            self.assertEqual(marker.read_text(encoding="utf-8"), "present\n")
            marker.unlink()
            packet.rmdir()

            original_write = blind_conversion_ab._write_private_file
            calls = 0

            def fail_second_write(path: Path, data: bytes) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected write failure")
                original_write(path, data)

            with (
                mock.patch.object(
                    blind_conversion_ab,
                    "_write_private_file",
                    side_effect=fail_second_write,
                ),
                self.assertRaisesRegex(OSError, "injected write failure"),
            ):
                blind_conversion_ab.prepare(
                    corpus, hazkey, mozc, "11" * 32, packet
                )
            self.assertFalse(packet.exists())
            self.assertFalse(any(directory.glob(".packet.tmp-*")))
            self.assertFalse((directory / ".packet.lock").exists())

            blind_conversion_ab.prepare(
                corpus, hazkey, mozc, "11" * 32, packet
            )
            self.assertEqual(stat.S_IMODE(packet.stat().st_mode), 0o700)
            self.assertEqual(
                {path.name for path in packet.iterdir()},
                {
                    blind_conversion_ab.REVIEW_NAME,
                    blind_conversion_ab.KEY_NAME,
                    blind_conversion_ab.MANIFEST_NAME,
                },
            )
            for path in packet.iterdir():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_seed_file_requires_private_256_bit_hex_value(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            seed = directory / "seed"
            seed.write_text("11" * 32 + "\n", encoding="ascii")
            os.chmod(seed, 0o600)
            self.assertEqual(blind_conversion_ab._load_seed_file(seed), "11" * 32)

            os.chmod(seed, 0o644)
            with self.assertRaisesRegex(ValueError, "owner-only"):
                blind_conversion_ab._load_seed_file(seed)

            os.chmod(seed, 0o600)
            seed.write_text("short\n", encoding="ascii")
            with self.assertRaisesRegex(ValueError, "64 lowercase hex"):
                blind_conversion_ab._load_seed_file(seed)

            seed.unlink()
            target = directory / "target"
            target.write_text("11" * 32 + "\n", encoding="ascii")
            os.chmod(target, 0o600)
            seed.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "non-symlink"):
                blind_conversion_ab._load_seed_file(seed)

    def test_human_metrics_keep_tie_heavy_result_in_context(self) -> None:
        outcomes = {
            "hazkey": Counter({"wins": 1, "losses": 0, "ties": 499}),
            "mozc": Counter({"wins": 0, "losses": 1, "ties": 499}),
        }

        rendered = blind_conversion_ab._render_outcomes(outcomes, 500)

        self.assertEqual(rendered["hazkey"]["decisive_win_rate"], 1.0)
        self.assertEqual(rendered["hazkey"]["decisive_case_rate"], 0.002)
        self.assertEqual(
            rendered["hazkey"]["net_preference_rate_all_cases"], 0.002
        )
        self.assertEqual(rendered["hazkey"]["two_sided_sign_test_p_value"], 1.0)

    def test_report_output_is_private_atomic_and_no_replace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "report.json"
            blind_conversion_ab._write_json_or_stdout({"result": "ok"}, output)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8")),
                {"result": "ok"},
            )
            with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
                blind_conversion_ab._write_json_or_stdout(
                    {"result": "replacement"}, output
                )

    def test_score_rejects_review_and_key_tampering(self) -> None:
        for target in ("review", "key"):
            with (
                self.subTest(target=target),
                tempfile.TemporaryDirectory() as temporary_directory,
            ):
                directory = Path(temporary_directory)
                _, _, _, packet, key_path = self.prepare_files(directory, count=2)
                review = packet / blind_conversion_ab.REVIEW_NAME
                key = json.loads(key_path.read_text(encoding="utf-8"))
                judgments = directory / "judgments.jsonl"
                write_jsonl(
                    judgments,
                    [
                        {
                            "schema": blind_conversion_ab.JUDGMENT_SCHEMA,
                            "case": case["case"],
                            "judgment": "tie",
                        }
                        for case in key["cases"]
                    ],
                )
                os.chmod(judgments, 0o600)
                if target == "review":
                    records = [
                        json.loads(line)
                        for line in review.read_text().splitlines()
                    ]
                    records[0]["category"] = "tampered"
                    write_jsonl(review, records)
                else:
                    key["source_ref"] = "tampered"
                    key_path.write_text(json.dumps(key), encoding="utf-8")
                with self.assertRaises(ValueError):
                    blind_conversion_ab.score(packet, judgments)

    def test_score_rejects_invalid_judgment_sets(self) -> None:
        modes = ("missing", "duplicate", "unknown", "invalid")
        for mode in modes:
            with (
                self.subTest(mode=mode),
                tempfile.TemporaryDirectory() as temporary_directory,
            ):
                directory = Path(temporary_directory)
                _, _, _, packet, key_path = self.prepare_files(directory, count=2)
                key = json.loads(key_path.read_text(encoding="utf-8"))
                values: list[dict[str, object]] = [
                    {
                        "schema": blind_conversion_ab.JUDGMENT_SCHEMA,
                        "case": case["case"],
                        "judgment": "tie",
                    }
                    for case in key["cases"]
                ]
                if mode == "missing":
                    values.pop()
                elif mode == "duplicate":
                    values.append(dict(values[0]))
                elif mode == "unknown":
                    values[-1]["case"] = "blind-unknown"
                else:
                    values[-1]["judgment"] = "maybe"
                judgments = directory / "judgments.jsonl"
                write_jsonl(judgments, values)
                os.chmod(judgments, 0o600)
                with self.assertRaises(ValueError):
                    blind_conversion_ab.score(packet, judgments)

    def test_cli_returns_two_without_traceback_for_invalid_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            corpus, hazkey, mozc = self.make_inputs(directory)
            seed = directory / "seed"
            seed.write_text("short\n", encoding="ascii")
            os.chmod(seed, 0o600)
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "prepare",
                    "--corpus",
                    str(corpus),
                    "--run-a",
                    str(hazkey),
                    "--run-b",
                    str(mozc),
                    "--seed-file",
                    str(seed),
                    "--output-directory",
                    str(directory / "packet"),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("seed must be exactly 64 lowercase hex digits", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
