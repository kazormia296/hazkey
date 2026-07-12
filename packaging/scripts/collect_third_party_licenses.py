#!/usr/bin/env python3
"""Collect every license required by the canonical Grimodex Linux archive."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile


LICENSE_NAME = re.compile(r"^(?:licen[cs]e|copying|notice)(?:[._-].*)?$", re.I)


def require_file(path: Path, description: str) -> Path:
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError(f"missing required {description}: {path}")
    return path


def license_documents(directory: Path, description: str) -> list[Path]:
    if not directory.is_dir():
        raise RuntimeError(f"missing required {description}: {directory}")
    documents = sorted(
        path for path in directory.iterdir()
        if path.is_file() and LICENSE_NAME.match(path.name)
    )
    if not any(path.name.lower().startswith(("license", "licence", "copying")) for path in documents):
        raise RuntimeError(f"no license document found for {description}: {directory}")
    return documents


def copy_documents(documents: list[Path], destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for document in documents:
        shutil.copy2(document, destination / document.name)


def resolved_identities(package_resolved: Path) -> list[str]:
    document = json.loads(require_file(package_resolved, "Package.resolved").read_text())
    pins = document.get("pins")
    if not isinstance(pins, list) or not pins:
        raise RuntimeError(f"Package.resolved contains no pins: {package_resolved}")
    identities: list[str] = []
    for pin in pins:
        identity = pin.get("identity") if isinstance(pin, dict) else None
        if not isinstance(identity, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", identity):
            raise RuntimeError(f"invalid Package.resolved identity: {identity!r}")
        if identity in identities:
            raise RuntimeError(f"duplicate Package.resolved identity: {identity}")
        identities.append(identity)
    return identities


def find_checkout(checkouts: Path, identity: str) -> Path:
    matches = [path for path in checkouts.iterdir() if path.is_dir() and path.name.casefold() == identity.casefold()]
    if len(matches) != 1:
        raise RuntimeError(f"expected one checkout for {identity}, found {len(matches)} in {checkouts}")
    return matches[0]


def collect(args: argparse.Namespace) -> None:
    repository = args.repository_root.resolve()
    server = repository / "hazkey-server"
    identities = resolved_identities(server / "Package.resolved")
    converter_identity = "azookeykanakanjiconverter"
    checkouts = args.swift_checkouts.resolve()
    if not checkouts.is_dir():
        raise RuntimeError(f"missing SwiftPM checkouts: {checkouts}")

    destination_parent = (
        args.destination_root.resolve()
        / "usr/share/licenses/fcitx5-grimodex"
    )
    destination_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination_parent) as temporary_directory:
        output = Path(temporary_directory) / "third-party"
        copy_documents(
            [require_file(server / "azooKey_dictionary_storage/LICENSE", "dictionary license")],
            output / "azookey-dictionary",
        )
        emoji = output / "azookey-emoji"
        emoji.mkdir(parents=True)
        shutil.copy2(
            require_file(server / "azooKey_emoji_dictionary_storage/data/README.md", "emoji source notice"),
            emoji / "SOURCE-NOTICE.md",
        )
        shutil.copy2(require_file(args.emoji_mozc_license, "Mozc emoji license"), emoji / "MOZC-LICENSE")
        shutil.copy2(require_file(args.emoji_unicode_license, "Unicode emoji license"), emoji / "UNICODE-LICENSE")
        copy_documents(license_documents(server / "llama.cpp", "llama.cpp"), output / "llama.cpp")
        (output / "protobuf").mkdir(parents=True)
        shutil.copy2(require_file(args.protobuf_license, "protobuf license"), output / "protobuf/LICENSE")
        (output / "swift-runtime").mkdir(parents=True)
        shutil.copy2(require_file(args.swift_runtime_license, "Swift runtime license"), output / "swift-runtime/LICENSE.txt")

        for identity in identities:
            checkout = find_checkout(checkouts, identity)
            documents = license_documents(checkout, f"Swift package {identity}")
            copy_documents(
                documents,
                output / "swift-packages" / identity,
            )
            if identity == converter_identity:
                converter_license = next(
                    document for document in documents
                    if document.name.lower().startswith(("license", "licence", "copying"))
                )
                shutil.copy2(converter_license, emoji / "AZOOKEY-LICENSE")

        final = destination_parent / "third-party"
        if final.exists():
            shutil.rmtree(final)
        shutil.move(str(output), final)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--swift-checkouts", type=Path, required=True)
    parser.add_argument("--swift-runtime-license", type=Path, required=True)
    parser.add_argument("--protobuf-license", type=Path, required=True)
    parser.add_argument("--emoji-mozc-license", type=Path, required=True)
    parser.add_argument("--emoji-unicode-license", type=Path, required=True)
    parser.add_argument("--destination-root", type=Path, required=True)
    return parser.parse_args()


if __name__ == "__main__":
    try:
        collect(parse_args())
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(f"license collection failed: {error}", file=sys.stderr)
        raise SystemExit(1)
