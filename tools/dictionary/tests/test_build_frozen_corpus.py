from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT))

from tools.dictionary import build_frozen_corpus  # noqa: E402


SCRIPT = REPOSITORY_ROOT / "tools/dictionary/build_frozen_corpus.py"
ADOPTION_FIXTURE = (
    REPOSITORY_ROOT
    / "hazkey-server/Tests/grimodex-spike/Fixtures/mozc-adoption-v1"
)
AJIMEE_DERIVED_SHA256 = (
    "sha256:91068dd92eddc70865c1b998843f38fd21d47458d1adf21799f9ad645e265fba"
)


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def make_ajimee_raw() -> bytes:
    values = []
    for index in reversed(range(100)):
        values.append(
            {
                "index": str(index),
                "context_text": "",
                "input": f"テストヽ{index}",
                "expected_output": [f"試験{index}", f"テスト{index}"],
                "original_text": f"試験{index}です。",
                "splitted_input_for_limited_input_length": [],
            }
        )
    for index in range(100, 200):
        values.append(
            {
                "index": str(index),
                "context_text": f"左文脈{index}",
                "input": f"ブンミャク{index}",
                "expected_output": [f"文脈{index}"],
                "original_text": f"左文脈{index}文脈{index}",
                "splitted_input_for_limited_input_length": [],
            }
        )
    return (json.dumps(values, ensure_ascii=False) + "\n").encode("utf-8")


def rows_for_component(contract: dict[str, object]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    prefix = str(contract["id_prefix"])
    for category, count in dict(contract["categories"]).items():
        for index in range(int(count)):
            if contract["id"] == "ajimee-unconditional":
                case_id = f"{prefix}{index:06d}"
            else:
                case_id = f"{prefix}{category}-{index:03d}"
            rows.append(
                {
                    "id": case_id,
                    "reading": f"よみ{category}{index}",
                    "expected": f"期待{category}{index}|別解{category}{index}",
                    "category": str(category),
                }
            )
    return rows


class FrozenCorpusBuilderTests(unittest.TestCase):
    def make_manifest(
        self, directory: Path
    ) -> tuple[Path, dict[str, object], bytes, dict[str, list[dict[str, str]]]]:
        components = []
        rows_by_component: dict[str, list[dict[str, str]]] = {}
        all_rows: list[dict[str, str]] = []
        for contract in build_frozen_corpus.COMPONENT_CONTRACTS:
            rows = rows_for_component(contract)
            data = build_frozen_corpus._encode_rows(rows)
            (directory / str(contract["path"])).write_bytes(data)
            rows_by_component[str(contract["id"])] = rows
            all_rows.extend(rows)
            components.append(
                {
                    "id": contract["id"],
                    "path": contract["path"],
                    "sha256": digest(data),
                    "cases": contract["cases"],
                    "id_prefix": contract["id_prefix"],
                    "categories": contract["categories"],
                    "provenance": contract["provenance"],
                }
            )
        aggregate = build_frozen_corpus._encode_rows(all_rows)
        manifest: dict[str, object] = {
            "schema": build_frozen_corpus.MANIFEST_SCHEMA,
            "normalization": {
                "unicode": "NFC",
                "line_endings": "LF",
                "reading_transform": build_frozen_corpus.NORMALIZATION_ID,
            },
            "components": components,
            "aggregate": {
                "cases": 256,
                "sha256": digest(aggregate),
                "categories": build_frozen_corpus.AGGREGATE_CATEGORIES,
            },
        }
        path = directory / "manifest.json"
        path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return path, manifest, aggregate, rows_by_component

    @staticmethod
    def rewrite_manifest(path: Path, manifest: dict[str, object]) -> None:
        path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def test_checked_in_ajimee_component_is_bound_to_policy_and_manifest(self) -> None:
        external_path = ADOPTION_FIXTURE / "external-ajimee-unconditional.tsv"
        manifest_path = ADOPTION_FIXTURE / "manifest.json"
        policy_path = ADOPTION_FIXTURE / "b0-policy.json"
        external = external_path.read_bytes()
        self.assertEqual(digest(external), AJIMEE_DERIVED_SHA256)
        rows = build_frozen_corpus._parse_tsv(external, str(external_path))
        self.assertEqual(len(rows), 100)
        self.assertEqual({row["category"] for row in rows}, {"ajimee-unconditional"})

        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        self.assertEqual(
            policy["formal_suite"]["components"]["ajimee_unconditional"]["sha256"],
            AJIMEE_DERIVED_SHA256,
        )
        self.assertEqual(
            policy["external_sources"]["ajimee_bench"]["derived_sha256"],
            AJIMEE_DERIVED_SHA256,
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["components"][0]["sha256"], AJIMEE_DERIVED_SHA256)
        self.assertEqual(policy["manifest_binding"]["path"], "manifest.json")
        self.assertEqual(
            policy["manifest_binding"]["sha256"], digest(manifest_path.read_bytes())
        )
        aggregate = build_frozen_corpus.build_aggregate(manifest_path)
        self.assertEqual(len(build_frozen_corpus._parse_tsv(aggregate, "aggregate")), 256)

    def rewrite_component(
        self,
        directory: Path,
        manifest: dict[str, object],
        component_index: int,
        rows: list[dict[str, str]],
    ) -> None:
        components = list(manifest["components"])
        component = dict(components[component_index])
        data = build_frozen_corpus._encode_rows(rows)
        (directory / component["path"]).write_bytes(data)
        component["sha256"] = digest(data)
        components[component_index] = component
        manifest["components"] = components

    def test_katakana_to_hiragana_v1_is_deterministic(self) -> None:
        self.assertEqual(
            build_frozen_corpus.katakana_to_hiragana("カケヵヶヴヽヾー・ABC"),
            "かけゕゖゔゝゞー・ABC",
        )
        self.assertEqual(
            build_frozen_corpus.katakana_to_hiragana("ハ\u3099"),
            "ば",
        )

    def test_derive_ajimee_selects_exact_unconditional_half(self) -> None:
        values = json.loads(make_ajimee_raw())
        values[99]["expected_output"] = ["試験0", "試験0", "別解0"]
        raw = (json.dumps(values, ensure_ascii=False) + "\n").encode()
        output = build_frozen_corpus.derive_ajimee_bytes(
            raw,
            expected_raw_sha256=digest(raw),
        )
        rows = build_frozen_corpus._parse_tsv(output, "derived")
        self.assertEqual(len(rows), 100)
        self.assertEqual(rows[0]["id"], "ajimee-jwtd-v2-000000")
        self.assertEqual(rows[-1]["id"], "ajimee-jwtd-v2-000099")
        self.assertEqual(rows[0]["reading"], "てすとゝ0")
        self.assertEqual(rows[0]["expected"], "試験0|別解0")
        self.assertTrue(
            all(row["category"] == "ajimee-unconditional" for row in rows)
        )

    def test_derive_ajimee_rejects_wrong_hash_split_and_duplicate_index(self) -> None:
        raw = make_ajimee_raw()
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            build_frozen_corpus.derive_ajimee_bytes(
                raw,
                expected_raw_sha256="sha256:" + "0" * 64,
            )

        values = json.loads(raw)
        values[100]["context_text"] = ""
        changed = (json.dumps(values, ensure_ascii=False) + "\n").encode()
        with self.assertRaisesRegex(ValueError, "100 unconditional"):
            build_frozen_corpus.derive_ajimee_bytes(
                changed,
                expected_raw_sha256=digest(changed),
            )

        values = json.loads(raw)
        values[1]["index"] = values[0]["index"]
        changed = (json.dumps(values, ensure_ascii=False) + "\n").encode()
        with self.assertRaisesRegex(ValueError, "duplicate index"):
            build_frozen_corpus.derive_ajimee_bytes(
                changed,
                expected_raw_sha256=digest(changed),
            )

    def test_builds_exact_deterministic_256_case_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            manifest_path, _, expected, _ = self.make_manifest(directory)
            first = build_frozen_corpus.build_aggregate(manifest_path)
            second = build_frozen_corpus.build_aggregate(manifest_path)
        self.assertEqual(first, expected)
        self.assertEqual(second, expected)
        self.assertEqual(len(build_frozen_corpus._parse_tsv(first, "aggregate")), 256)

    def test_cli_writes_atomically_and_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            manifest_path, _, expected, _ = self.make_manifest(directory)
            output = directory / "formal.tsv"
            first = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "build",
                    "--manifest",
                    str(manifest_path),
                    "--output",
                    str(output),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(output.read_bytes(), expected)
            second = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "build",
                    "--manifest",
                    str(manifest_path),
                    "--output",
                    str(output),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(second.returncode, 2)
            self.assertIn("refusing to overwrite", second.stderr)
            self.assertEqual(output.read_bytes(), expected)
            self.assertFalse(list(directory.glob(".formal.tsv.*")))

    def test_rejects_component_tamper_duplicate_id_and_category_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            manifest_path, manifest, _, rows_by_component = self.make_manifest(directory)
            external = directory / "external-ajimee-unconditional.tsv"
            external.write_bytes(external.read_bytes() + b"tamper")
            with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                build_frozen_corpus.build_aggregate(manifest_path)

            manifest_path, manifest, _, rows_by_component = self.make_manifest(directory)
            product = copy.deepcopy(rows_by_component["product-curated"])
            product[1]["id"] = product[0]["id"]
            self.rewrite_component(directory, manifest, 1, product)
            self.rewrite_manifest(manifest_path, manifest)
            with self.assertRaisesRegex(ValueError, "duplicate case ID"):
                build_frozen_corpus.build_aggregate(manifest_path)

            manifest_path, manifest, _, rows_by_component = self.make_manifest(directory)
            product = copy.deepcopy(rows_by_component["product-curated"])
            product[0]["category"] = "sentinel"
            self.rewrite_component(directory, manifest, 1, product)
            self.rewrite_manifest(manifest_path, manifest)
            with self.assertRaisesRegex(ValueError, "category counts mismatch"):
                build_frozen_corpus.build_aggregate(manifest_path)

    def test_rejects_noncanonical_tsv_and_manifest_contract_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            manifest_path, manifest, _, rows_by_component = self.make_manifest(directory)
            protected = copy.deepcopy(rows_by_component["protected"])
            protected[0]["reading"] = "カタカナ"
            self.rewrite_component(directory, manifest, 2, protected)
            self.rewrite_manifest(manifest_path, manifest)
            with self.assertRaisesRegex(ValueError, "not normalized"):
                build_frozen_corpus.build_aggregate(manifest_path)

            manifest_path, manifest, _, _ = self.make_manifest(directory)
            components = list(manifest["components"])
            changed = dict(components[0])
            changed["path"] = "../sentinel.tsv"
            components[0] = changed
            manifest["components"] = components
            self.rewrite_manifest(manifest_path, manifest)
            with self.assertRaisesRegex(ValueError, "path does not match"):
                build_frozen_corpus.build_aggregate(manifest_path)

    def test_strict_tsv_rejects_extra_blank_duplicate_and_non_nfc_rows(self) -> None:
        invalid = {
            "extra column": (
                b"id\treading\texpected\tcategory\n"
                + "case\tよみ\t期待\tprotected\textra\n".encode()
            ),
            "blank row": (
                b"id\treading\texpected\tcategory\n"
                + "case\tよみ\t期待\tprotected\n\n".encode()
            ),
            "duplicate alternative": (
                b"id\treading\texpected\tcategory\n"
                + "case\tよみ\t期待|期待\tprotected\n".encode()
            ),
            "non-NFC": (
                b"id\treading\texpected\tcategory\n"
                + "case\tよみ\tか\u3099\tprotected\n".encode()
            ),
            "CRLF": b"id\treading\texpected\tcategory\r\n",
        }
        for name, data in invalid.items():
            with self.subTest(name=name), self.assertRaises(ValueError):
                build_frozen_corpus._parse_tsv(data, name)

    def test_rejects_duplicate_json_keys_and_symlink_component(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            manifest_path, _, _, _ = self.make_manifest(directory)
            manifest_path.write_text(
                '{"schema":"x","schema":"y"}\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
                build_frozen_corpus.build_aggregate(manifest_path)

            manifest_path, _, _, _ = self.make_manifest(directory)
            external = directory / "external-ajimee-unconditional.tsv"
            moved = directory / "external.real.tsv"
            external.rename(moved)
            external.symlink_to(moved)
            with self.assertRaisesRegex(ValueError, "regular non-symlink"):
                build_frozen_corpus.build_aggregate(manifest_path)


if __name__ == "__main__":
    unittest.main()
