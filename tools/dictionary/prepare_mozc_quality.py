#!/usr/bin/env python3
"""Convert the Hazkey quality corpus to Mozc quality_regression_main input."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

if __package__:
    from .evaluate_conversion_quality import load_corpus
else:
    from evaluate_conversion_quality import load_corpus


HEADER = "# label\tkey\tvalue\tcommand"
COMMAND = "Conversion Expected"


def positive_integer(value: str) -> int:
    """Parse a strictly positive integer for argparse."""
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _validate_tsv_field(value: str, description: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{description} must be a string")
    if any(character in value for character in ("\t", "\r", "\n")):
        raise ValueError(f"{description} must not contain tabs or newlines")


def render_quality_regression(
    corpus: Sequence[Mapping[str, str]], repeat: int = 1
) -> str:
    """Render corpus rows in the format consumed by quality_regression_main."""
    if isinstance(repeat, bool) or not isinstance(repeat, int) or repeat < 1:
        raise ValueError("repeat must be a positive integer")

    lines = [HEADER]
    for run in range(1, repeat + 1):
        for row in corpus:
            case_id = row["id"]
            reading = row["reading"]
            expected = row["expected"].split("|", 1)[0]
            label = f"ab-{run}-{case_id}"
            for description, value in (
                ("label", label),
                ("reading", reading),
                ("expected", expected),
            ):
                _validate_tsv_field(value, description)
            lines.append("\t".join((label, reading, expected, COMMAND)))
    return "\n".join(lines) + "\n"


def prepare(corpus_path: Path, output_path: Path, repeat: int = 1) -> None:
    rendered = render_quality_regression(load_corpus(corpus_path), repeat)
    with output_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(rendered)


def parse_arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeat", type=positive_integer, default=1)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_arguments(argv)
    try:
        prepare(args.corpus, args.output, args.repeat)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
