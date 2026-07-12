#!/usr/bin/env python3
"""Render immutable AUR metadata from verified Grimodex release assets."""

from __future__ import annotations

import argparse
import hashlib
import hmac
from pathlib import Path
import re
import sys


ARCHITECTURES = ("x86_64", "aarch64")
VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
SIDECAR_PATTERN = re.compile(
    r"^([0-9a-fA-F]{64})[ \t]+\*?([^\r\n]+)$"
)
PLACEHOLDERS = {
    "x86_64": (
        "RELEASE_X86_64_ARCHIVE_SHA256",
        "RELEASE_X86_64_SIDECAR_SHA256",
    ),
    "aarch64": (
        "RELEASE_AARCH64_ARCHIVE_SHA256",
        "RELEASE_AARCH64_SIDECAR_SHA256",
    ),
}


class ReleaseRenderError(Exception):
    """Raised when release inputs cannot produce trustworthy AUR metadata."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_regular_file(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise ReleaseRenderError(f"{label} is not a regular file: {path}")


def sidecar_archive_hash(sidecar: Path, archive_name: str) -> str:
    try:
        content = sidecar.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise ReleaseRenderError(
            f"checksum sidecar is not UTF-8: {sidecar}"
        ) from error

    lines = content.splitlines()
    if len(lines) != 1:
        raise ReleaseRenderError(
            f"checksum sidecar must contain exactly one line: {sidecar}"
        )
    match = SIDECAR_PATTERN.fullmatch(lines[0])
    if match is None or match.group(2) != archive_name:
        raise ReleaseRenderError(
            f"checksum sidecar must name {archive_name}: {sidecar}"
        )
    return match.group(1).lower()


def verified_hashes(asset_dir: Path, version: str) -> dict[str, tuple[str, str]]:
    hashes: dict[str, tuple[str, str]] = {}
    for architecture in ARCHITECTURES:
        archive_name = f"fcitx5-grimodex-{version}-{architecture}.tar.zst"
        archive = asset_dir / archive_name
        sidecar = asset_dir / f"{archive_name}.sha256"
        require_regular_file(archive, f"{architecture} release archive")
        require_regular_file(sidecar, f"{architecture} checksum sidecar")

        archive_hash = sha256_file(archive)
        expected_hash = sidecar_archive_hash(sidecar, archive_name)
        if not hmac.compare_digest(archive_hash, expected_hash):
            raise ReleaseRenderError(
                f"checksum mismatch for release archive: {archive}"
            )
        hashes[architecture] = (archive_hash, sha256_file(sidecar))
    return hashes


def render_pkgbuild(template: str, version: str, hashes: dict[str, tuple[str, str]]) -> str:
    version_match = re.search(
        r"^pkgver=([0-9]+\.[0-9]+\.[0-9]+)$",
        template,
        re.MULTILINE,
    )
    if version_match is None:
        raise ReleaseRenderError("PKGBUILD template has no semantic pkgver")
    if version_match.group(1) != version:
        raise ReleaseRenderError(
            "PKGBUILD template version does not match requested release "
            f"({version_match.group(1)} != {version})"
        )

    rendered = template
    for architecture in ARCHITECTURES:
        for placeholder, digest in zip(
            PLACEHOLDERS[architecture], hashes[architecture], strict=True
        ):
            if rendered.count(placeholder) != 1:
                raise ReleaseRenderError(
                    f"PKGBUILD template must contain {placeholder} exactly once"
                )
            rendered = rendered.replace(placeholder, digest)

    if "RELEASE_" in rendered:
        raise ReleaseRenderError("unresolved RELEASE_ placeholder in PKGBUILD")
    return rendered


def render_release(
    template_dir: Path,
    asset_dir: Path,
    output_dir: Path,
    version: str,
) -> None:
    if not VERSION_PATTERN.fullmatch(version):
        raise ReleaseRenderError(f"invalid release version: {version}")
    if output_dir.exists():
        raise ReleaseRenderError(f"output directory already exists: {output_dir}")

    pkgbuild = template_dir / "PKGBUILD"
    install_script = template_dir / "fcitx5-grimodex.install"
    require_regular_file(pkgbuild, "PKGBUILD template")
    require_regular_file(install_script, "install script template")

    template = pkgbuild.read_text(encoding="utf-8")
    install_content = install_script.read_bytes()
    hashes = verified_hashes(asset_dir, version)
    rendered = render_pkgbuild(template, version, hashes)

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir()
    (output_dir / "PKGBUILD").write_text(rendered, encoding="utf-8")
    (output_dir / "fcitx5-grimodex.install").write_bytes(install_content)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render hash-pinned AUR metadata from release assets."
    )
    parser.add_argument("--template-dir", required=True, type=Path)
    parser.add_argument("--asset-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--version", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        render_release(
            template_dir=args.template_dir,
            asset_dir=args.asset_dir,
            output_dir=args.output_dir,
            version=args.version,
        )
    except (OSError, ReleaseRenderError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
