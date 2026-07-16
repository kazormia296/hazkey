#!/usr/bin/env python3
"""Preannotate first-segment boundaries with Lindera/UniDic.

This tool deliberately consumes only the frozen corpus and morphological
analysis.  It never reads Hazkey or Mozc candidates.  Its output is a review
queue, not a reviewed label set and not formal adoption evidence.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import unicodedata
from typing import Any, Iterable


SCHEMA = "hazkey.mozc-hybrid-boundary-preannotation.v1"
ELEMENT_UNIT = "source_reading_code_point"
CORPUS_HEADER = ["id", "reading", "expected", "category"]
HELPER_TOKEN_FIELDS = (
    "kind",
    "helper_id",
    "token_index",
    "byte_start",
    "byte_end",
    "surface",
    "pos_major",
    "pos_sub1",
    "pos_sub2",
    "lexical_reading",
    "orth_surface",
    "pronunciation",
)
CANONICAL_INTEGER = re.compile(r"0|[1-9][0-9]*")


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical_jsonl(records: Iterable[dict[str, Any]]) -> bytes:
    return b"".join(
        json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
        for record in records
    )


def _validate_text(value: str, context: str) -> str:
    if not value:
        raise ValueError(f"{context} must be non-empty")
    if value != unicodedata.normalize("NFC", value):
        raise ValueError(f"{context} must be NFC-normalized")
    if any(
        unicodedata.category(character) == "Cc" or character == "\ufeff"
        for character in value
    ):
        raise ValueError(f"{context} must not contain control characters")
    return value


@dataclass(frozen=True)
class CorpusRow:
    case_id: str
    reading: str
    expected_surfaces: tuple[str, ...]
    category: str
    row_sha256: str


def load_corpus_bytes(data: bytes, context: str) -> list[CorpusRow]:
    if data.startswith(b"\xef\xbb\xbf") or b"\r" in data:
        raise ValueError(f"{context} must be BOM-free UTF-8 TSV with LF endings")
    if not data.endswith(b"\n"):
        raise ValueError(f"{context} must end with one LF")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"{context} is not valid UTF-8") from error
    physical_lines = text[:-1].split("\n")
    if not physical_lines or any(not line for line in physical_lines):
        raise ValueError(f"{context} must contain non-empty TSV lines")
    parsed = list(csv.reader(io.StringIO(text), delimiter="\t", strict=True))
    if not parsed or parsed[0] != CORPUS_HEADER:
        raise ValueError(f"{context} header must be {CORPUS_HEADER!r}")
    if len(parsed) != len(physical_lines):
        raise ValueError(f"{context} must not contain embedded newlines")

    result: list[CorpusRow] = []
    seen_ids: set[str] = set()
    for line_number, (fields, raw_line) in enumerate(
        zip(parsed[1:], physical_lines[1:], strict=True), 2
    ):
        if len(fields) != 4:
            raise ValueError(f"{context}:{line_number} must have four columns")
        case_id, reading, expected, category = fields
        _validate_text(case_id, f"{context}:{line_number}.id")
        _validate_text(reading, f"{context}:{line_number}.reading")
        _validate_text(expected, f"{context}:{line_number}.expected")
        _validate_text(category, f"{context}:{line_number}.category")
        if case_id in seen_ids:
            raise ValueError(f"{context}:{line_number} duplicates id {case_id!r}")
        seen_ids.add(case_id)
        alternatives = tuple(expected.split("|"))
        if any(not alternative for alternative in alternatives):
            raise ValueError(
                f"{context}:{line_number}.expected contains an empty alternative"
            )
        if len(alternatives) != len(set(alternatives)):
            raise ValueError(
                f"{context}:{line_number}.expected contains duplicate alternatives"
            )
        for alternative_index, alternative in enumerate(alternatives):
            _validate_text(
                alternative,
                f"{context}:{line_number}.expected[{alternative_index}]",
            )
        result.append(
            CorpusRow(
                case_id=case_id,
                reading=reading,
                expected_surfaces=alternatives,
                category=category,
                row_sha256=sha256_bytes((raw_line + "\n").encode("utf-8")),
            )
        )
    if not result:
        raise ValueError(f"{context} must contain at least one case")
    return result


def _canonical_nonnegative_integer(value: str, context: str) -> int:
    if CANONICAL_INTEGER.fullmatch(value) is None:
        raise ValueError(f"{context} must be a canonical non-negative integer")
    return int(value)


def parse_helper_output(
    data: bytes,
    requests: list[tuple[str, str]],
    context: str = "Lindera helper stdout",
) -> dict[str, list[dict[str, Any]]]:
    if data.startswith(b"\xef\xbb\xbf") or b"\r" in data:
        raise ValueError(f"{context} must be BOM-free UTF-8 TSV with LF endings")
    if not data.endswith(b"\n"):
        raise ValueError(f"{context} must end with one LF")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"{context} is not valid UTF-8") from error
    request_index = 0
    tokens: list[dict[str, Any]] = []
    result: dict[str, list[dict[str, Any]]] = {}
    next_byte_start = 0
    for line_number, line in enumerate(text[:-1].split("\n"), 1):
        if not line:
            raise ValueError(f"{context}:{line_number} must not be blank")
        fields = line.split("\t")
        if request_index >= len(requests):
            raise ValueError(f"{context}:{line_number} is unexpected trailing output")
        expected_id, expected_text = requests[request_index]
        if fields[0] == "T":
            if len(fields) != len(HELPER_TOKEN_FIELDS):
                raise ValueError(
                    f"{context}:{line_number} token line must have "
                    f"{len(HELPER_TOKEN_FIELDS)} columns"
                )
            if fields[1] != expected_id:
                raise ValueError(
                    f"{context}:{line_number} expected helper id {expected_id!r}"
                )
            token_index = _canonical_nonnegative_integer(
                fields[2], f"{context}:{line_number}.token_index"
            )
            byte_start = _canonical_nonnegative_integer(
                fields[3], f"{context}:{line_number}.byte_start"
            )
            byte_end = _canonical_nonnegative_integer(
                fields[4], f"{context}:{line_number}.byte_end"
            )
            if token_index != len(tokens):
                raise ValueError(
                    f"{context}:{line_number}.token_index must be contiguous"
                )
            if byte_start < next_byte_start or byte_end <= byte_start:
                raise ValueError(
                    f"{context}:{line_number} token byte spans must be ordered"
                )
            source_bytes = expected_text.encode("utf-8")
            if byte_end > len(source_bytes):
                raise ValueError(f"{context}:{line_number} token exceeds source text")
            try:
                skipped = source_bytes[next_byte_start:byte_start].decode("utf-8")
            except UnicodeDecodeError as error:
                raise ValueError(
                    f"{context}:{line_number} skipped span splits a UTF-8 scalar"
                ) from error
            if skipped and not skipped.isspace():
                raise ValueError(
                    f"{context}:{line_number} helper skipped non-whitespace text"
                )
            surface = fields[5]
            try:
                sliced_surface = source_bytes[byte_start:byte_end].decode("utf-8")
            except UnicodeDecodeError as error:
                raise ValueError(
                    f"{context}:{line_number} token splits a UTF-8 scalar"
                ) from error
            if surface != sliced_surface:
                raise ValueError(
                    f"{context}:{line_number}.surface does not match its byte span"
                )
            if any(not field for field in fields[5:]):
                raise ValueError(f"{context}:{line_number} contains an empty field")
            tokens.append(
                {
                    "token_index": token_index,
                    "byte_start": byte_start,
                    "byte_end": byte_end,
                    "surface": surface,
                    "pos_major": fields[6],
                    "pos_sub1": fields[7],
                    "pos_sub2": fields[8],
                    "lexical_reading": fields[9],
                    "orth_surface": fields[10],
                    "pronunciation": fields[11],
                    "gap_before": skipped,
                }
            )
            next_byte_start = byte_end
        elif fields[0] == "E":
            if len(fields) != 2 or fields[1] != expected_id:
                raise ValueError(
                    f"{context}:{line_number} malformed end line for {expected_id!r}"
                )
            if not tokens:
                raise ValueError(f"{context}:{line_number} request has no tokens")
            trailing = expected_text.encode("utf-8")[next_byte_start:]
            try:
                trailing_text = trailing.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ValueError(
                    f"{context}:{line_number} trailing span splits a UTF-8 scalar"
                ) from error
            if trailing_text and not trailing_text.isspace():
                raise ValueError(
                    f"{context}:{line_number} helper skipped non-whitespace trailing text"
                )
            result[expected_id] = tokens
            request_index += 1
            tokens = []
            next_byte_start = 0
        else:
            raise ValueError(f"{context}:{line_number} has unknown record kind")
    if request_index != len(requests):
        raise ValueError(
            f"{context} ended after {request_index} of {len(requests)} requests"
        )
    return result


def run_lindera_helper(
    tokenizer_path: Path, requests: list[tuple[str, str]]
) -> dict[str, list[dict[str, Any]]]:
    request_bytes = "".join(
        f"{helper_id}\t{text}\n" for helper_id, text in requests
    ).encode("utf-8")
    try:
        completed = subprocess.run(
            [str(tokenizer_path)],
            input=request_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as error:
        raise ValueError(f"could not execute Lindera tokenizer: {error}") from error
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(
            f"Lindera tokenizer exited with {completed.returncode}: {stderr}"
        )
    if completed.stderr:
        raise ValueError("Lindera tokenizer must not write to stderr")
    return parse_helper_output(completed.stdout, requests)


def normalize_reading(value: str) -> str:
    result: list[str] = []
    for character in unicodedata.normalize("NFC", value):
        codepoint = ord(character)
        if 0x30A1 <= codepoint <= 0x30F6:
            result.append(chr(codepoint - 0x60))
        elif codepoint == 0x30FD:
            result.append("ゝ")
        elif codepoint == 0x30FE:
            result.append("ゞ")
        else:
            result.append(character)
    return "".join(result)


def _token_reading(token: dict[str, Any]) -> tuple[str, str]:
    gap_before = token.get("gap_before", "")
    if token["surface"].isascii() and any(
        character.isalnum() for character in token["surface"]
    ):
        return normalize_reading(gap_before + token["surface"]), "ascii_surface"
    lexical = token["lexical_reading"]
    if lexical != "*":
        return normalize_reading(gap_before + lexical), "lexical_reading"
    orthographic = token["orth_surface"]
    if orthographic != "*":
        return normalize_reading(gap_before + orthographic), "orth_surface_fallback"
    return normalize_reading(gap_before + token["surface"]), "surface_fallback"


def _is_ascii_fragment(token: dict[str, Any]) -> bool:
    return bool(token["surface"]) and token["surface"].isascii()


def _is_punctuation(token: dict[str, Any]) -> bool:
    return token["pos_major"] == "補助記号" or all(
        not character.isalnum()
        and not ("ぁ" <= character <= "ん")
        and not ("ァ" <= character <= "ヶ")
        and not ("一" <= character <= "龯")
        for character in token["surface"]
    )


def group_bunsetsu_tokens(
    tokens: list[dict[str, Any]],
) -> tuple[list[list[dict[str, Any]]], list[str]]:
    groups: list[list[dict[str, Any]]] = []
    ambiguity: list[str] = []
    for token in tokens:
        major = token["pos_major"]
        sub1 = token["pos_sub1"]
        dependent = major in {"助詞", "助動詞", "接尾辞"} or _is_punctuation(
            token
        )
        non_independent_predicate = (
            major in {"動詞", "形容詞"} and "非自立" in sub1
        )
        if not groups:
            groups.append([token])
            if dependent or non_independent_predicate:
                ambiguity.append("leading_dependent_token")
            continue
        previous = groups[-1][-1]
        if dependent or previous["pos_major"] == "接頭辞":
            groups[-1].append(token)
        elif non_independent_predicate and (
            groups[-1][0]["pos_major"] == "動詞"
            or not any(item["pos_major"] == "助詞" for item in groups[-1])
        ):
            groups[-1].append(token)
        elif _is_ascii_fragment(previous) and _is_ascii_fragment(token):
            groups[-1].append(token)
        elif major == "接頭辞":
            groups.append([token])
        elif major == "名詞" and previous["pos_major"] == "名詞":
            groups[-1].append(token)
        else:
            if major == "*":
                ambiguity.append("unknown_part_of_speech")
            if major == "動詞" and previous["pos_major"] == "動詞":
                ambiguity.append("consecutive_independent_verbs")
            groups.append([token])
    if groups and groups[-1][-1]["pos_major"] == "接頭辞":
        ambiguity.append("trailing_prefix")
    return groups, sorted(set(ambiguity))


def _levenshtein_tables(
    predicted: str, source: str
) -> tuple[list[list[int]], list[list[int]]]:
    m = len(predicted)
    n = len(source)
    forward = [[0] * (n + 1) for _ in range(m + 1)]
    for index in range(m + 1):
        forward[index][0] = index
    for index in range(n + 1):
        forward[0][index] = index
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            forward[i][j] = min(
                forward[i - 1][j] + 1,
                forward[i][j - 1] + 1,
                forward[i - 1][j - 1]
                + (predicted[i - 1] != source[j - 1]),
            )
    backward = [[0] * (n + 1) for _ in range(m + 1)]
    for index in range(m + 1):
        backward[index][n] = m - index
    for index in range(n + 1):
        backward[m][index] = n - index
    for i in range(m - 1, -1, -1):
        for j in range(n - 1, -1, -1):
            backward[i][j] = min(
                backward[i + 1][j] + 1,
                backward[i][j + 1] + 1,
                backward[i + 1][j + 1] + (predicted[i] != source[j]),
            )
    return forward, backward


def _align_boundaries(
    predicted: str, source: str, predicted_boundaries: list[int]
) -> tuple[list[int], int, int, list[str]]:
    forward, backward = _levenshtein_tables(predicted, source)
    distance = forward[len(predicted)][len(source)]
    denominator = max(len(predicted), len(source), 1)
    rate_basis_points = max(
        0, round((1.0 - distance / denominator) * 10_000)
    )
    selected: list[int] = []
    ambiguity: list[str] = []
    previous = 0
    for boundary_index, predicted_offset in enumerate(predicted_boundaries):
        candidates = [
            source_offset
            for source_offset in range(previous + 1, len(source))
            if forward[predicted_offset][source_offset]
            + backward[predicted_offset][source_offset]
            == distance
        ]
        if not candidates:
            proportional = round(
                predicted_offset * len(source) / max(len(predicted), 1)
            )
            chosen = min(max(proportional, previous + 1), len(source) - 1)
            ambiguity.append(f"boundary_{boundary_index}_has_no_optimal_cut")
        else:
            proportional = round(
                predicted_offset * len(source) / max(len(predicted), 1)
            )
            chosen = min(candidates, key=lambda value: (abs(value - proportional), value))
            if len(candidates) != 1:
                ambiguity.append(
                    f"boundary_{boundary_index}_has_{len(candidates)}_optimal_cuts"
                )
        selected.append(chosen)
        previous = chosen
    return selected, distance, rate_basis_points, ambiguity


def annotate_alternative(
    *,
    alternative_index: int,
    surface: str,
    source_reading: str,
    helper_id: str,
    tokens: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    audited_tokens: list[dict[str, Any]] = []
    token_readings: dict[int, str] = {}
    fallback_ambiguity: list[str] = []
    for token in tokens:
        normalized, reading_source = _token_reading(token)
        token_readings[token["token_index"]] = normalized
        if reading_source not in {"lexical_reading", "ascii_surface"} and any(
            character.isalnum()
            or "ぁ" <= character <= "ん"
            or "ァ" <= character <= "ヶ"
            or "一" <= character <= "龯"
            for character in token["surface"]
        ):
            fallback_ambiguity.append(
                f"token_{token['token_index']}_{reading_source}"
            )
        audited_tokens.append(
            {
                **token,
                "reading_source": reading_source,
                "normalized_reading": normalized,
            }
        )

    groups, grouping_ambiguity = group_bunsetsu_tokens(tokens)
    group_readings = [
        "".join(token_readings[token["token_index"]] for token in group)
        for group in groups
    ]
    predicted = "".join(group_readings)
    predicted_boundaries: list[int] = []
    offset = 0
    for reading in group_readings[:-1]:
        offset += len(reading)
        predicted_boundaries.append(offset)
    normalized_source = normalize_reading(source_reading)
    boundaries, distance, rate_basis_points, alignment_ambiguity = _align_boundaries(
        predicted, normalized_source, predicted_boundaries
    )
    ambiguity = sorted(
        set(grouping_ambiguity + fallback_ambiguity + alignment_ambiguity)
    )
    unique_alignment = not alignment_ambiguity
    if (
        distance == 0
        and unique_alignment
        and not grouping_ambiguity
        and not fallback_ambiguity
    ):
        confidence = "exact"
    elif rate_basis_points >= 9000 and unique_alignment and not ambiguity:
        confidence = "aligned"
    else:
        confidence = "ambiguous"

    source_boundaries = [0, *boundaries, len(source_reading)]
    segments: list[dict[str, Any]] = []
    for group_index, group in enumerate(groups):
        start = source_boundaries[group_index]
        end = source_boundaries[group_index + 1]
        segments.append(
            {
                "index": group_index,
                "surface": "".join(
                    token.get("gap_before", "") + token["surface"]
                    for token in group
                ),
                "reading": source_reading[start:end],
                "token_indices": [token["token_index"] for token in group],
                "start": start,
                "count": end - start,
                "unit": ELEMENT_UNIT,
            }
        )
    marked_reading = "|".join(segment["reading"] for segment in segments)
    annotation = {
        "index": alternative_index,
        "surface": surface,
        "marked_reading": marked_reading,
        "segments": segments,
        "boundaries_after": boundaries,
        "first_segment_count": segments[0]["count"],
        "confidence": confidence,
        "alignment_distance": distance,
        "alignment_rate_basis_points": rate_basis_points,
        "ambiguity": ambiguity,
    }
    audit = {
        "index": alternative_index,
        "helper_id": helper_id,
        "tokens": audited_tokens,
    }
    return annotation, audit


def prepare_records(
    corpus_bytes: bytes,
    tokenizer_path: Path,
    *,
    corpus_context: str = "corpus",
) -> list[dict[str, Any]]:
    rows = load_corpus_bytes(corpus_bytes, corpus_context)
    corpus_sha256 = sha256_bytes(corpus_bytes)
    requests = [
        (f"{row.case_id}::alt-{index}", surface)
        for row in rows
        for index, surface in enumerate(row.expected_surfaces)
    ]
    tokenized = run_lindera_helper(tokenizer_path, requests)
    records: list[dict[str, Any]] = []
    for row in rows:
        alternatives: list[dict[str, Any]] = []
        audits: list[dict[str, Any]] = []
        for index, surface in enumerate(row.expected_surfaces):
            helper_id = f"{row.case_id}::alt-{index}"
            annotation, audit = annotate_alternative(
                alternative_index=index,
                surface=surface,
                source_reading=row.reading,
                helper_id=helper_id,
                tokens=tokenized[helper_id],
            )
            alternatives.append(annotation)
            audits.append(audit)
        consensus = len(
            {tuple(alternative["boundaries_after"]) for alternative in alternatives}
        ) == 1
        selected = alternatives[0]
        combined_ambiguity = list(selected["ambiguity"])
        if not consensus:
            combined_ambiguity.append("expected_surface_boundary_disagreement")
        confidence = selected["confidence"] if consensus else "ambiguous"
        records.append(
            {
                "schema": SCHEMA,
                "id": row.case_id,
                "category": row.category,
                "source": {
                    "corpus_sha256": corpus_sha256,
                    "row_sha256": row.row_sha256,
                    "reading": row.reading,
                    "expected_surfaces": list(row.expected_surfaces),
                },
                "elements": {
                    "unit": ELEMENT_UNIT,
                    "values": [
                        {"index": index, "text": character}
                        for index, character in enumerate(row.reading)
                    ],
                },
                "known_source_reused": True,
                "diagnostic_only": True,
                "formal_authorized": False,
                "candidate_outputs_consulted": False,
                "preannotation": {
                    "selected_alternative_index": 0,
                    "marked_reading": selected["marked_reading"],
                    "segments": selected["segments"],
                    "boundaries_after": selected["boundaries_after"],
                    "first_segment_count": selected["first_segment_count"],
                    "confidence": confidence,
                    "alignment_distance": selected["alignment_distance"],
                    "alignment_rate_basis_points": selected[
                        "alignment_rate_basis_points"
                    ],
                    "ambiguity": sorted(set(combined_ambiguity)),
                    "alternative_boundary_disagreement": not consensus,
                    "alternatives": alternatives,
                },
                "token_audit": {"alternatives": audits},
                "review": {
                    "status": "pending",
                    "annotator_id": None,
                    "marked_reading": None,
                    "first_segment_count": None,
                    "surfaces": [],
                    "notes": None,
                },
            }
        )
    return records


def build_summary(records: list[dict[str, Any]], output_bytes: bytes) -> dict[str, Any]:
    confidence = {"exact": 0, "aligned": 0, "ambiguous": 0}
    disagreements = 0
    tokens = 0
    for record in records:
        confidence[record["preannotation"]["confidence"]] += 1
        disagreements += int(
            record["preannotation"]["alternative_boundary_disagreement"]
        )
        tokens += sum(
            len(alternative["tokens"])
            for alternative in record["token_audit"]["alternatives"]
        )
    return {
        "schema": SCHEMA,
        "cases": len(records),
        "tokens": tokens,
        "confidence": confidence,
        "alternative_boundary_disagreements": disagreements,
        "candidate_outputs_consulted": False,
        "diagnostic_only": True,
        "formal_authorized": False,
        "output_sha256": sha256_bytes(output_bytes),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a Lindera/UniDic first-segment boundary review queue."
    )
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--lindera-tokenizer", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary-output", type=Path)
    args = parser.parse_args()
    try:
        corpus_bytes = args.corpus.read_bytes()
        records = prepare_records(
            corpus_bytes,
            args.lindera_tokenizer,
            corpus_context=str(args.corpus),
        )
        output_bytes = canonical_jsonl(records)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(output_bytes)
        summary = build_summary(records, output_bytes)
        encoded_summary = (
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        )
        if args.summary_output is not None:
            args.summary_output.parent.mkdir(parents=True, exist_ok=True)
            args.summary_output.write_text(encoded_summary, encoding="utf-8")
        sys.stdout.write(encoded_summary)
        return 0
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
