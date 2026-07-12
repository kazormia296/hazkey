#!/usr/bin/env python3
"""Reject concrete network capabilities in packaged Grimodex executables.

Raw byte searches are not capability checks: compiler metadata, tokenizer data,
and license notices may legitimately name an API without making it callable.
This audit instead inspects ELF dependency and symbol tables with ``readelf``.
It runs before release stripping so statically linked symbols remain visible;
the package validator repeats the dynamic metadata check on stripped artifacts.
Executable scripts are checked separately for direct network commands.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import stat
import subprocess
import sys


ELF_MAGIC = b"\x7fELF"
FORBIDDEN_ELF_METADATA_MARKERS = (
    "qt6network",
    "qnetworkaccessmanager",
    "qnetworkrequest",
    "qnetworkreply",
    "foundationnetworking",
    "urlsession",
    "nsurlconnection",
    "asynchttpclient",
    "niohttpclient",
    "niohttp1",
    "curl_easy_",
    "curl_multi_",
    "curl_share_",
    "curl_url_",
    "libcurl.so",
    "getaddrinfo",
    "getnameinfo",
    "gethostbyname",
    "inet_pton",
    "inet_ntop",
)
FORBIDDEN_ELF_RODATA_MARKERS = (
    "libcurl.so",
    "curl_easy_",
    "curl_multi_",
    "curl_share_",
    "curl_url_",
)
FORBIDDEN_SOURCE_PATTERNS = (
    (
        "internet socket",
        re.compile(
            r"\b(?:AF_INET6?|PF_INET6?|sockaddr_in6?|getaddrinfo|getnameinfo|"
            r"gethostbyname2?|inet_(?:addr|aton|ntoa|ntop|pton))\b"
        ),
    ),
    (
        "network client API",
        re.compile(
            r"\b(?:Qt6Network|QNetworkAccessManager|QNetworkRequest|"
            r"QNetworkReply|FoundationNetworking|URLSession[A-Za-z0-9_]*|"
            r"NSURLConnection|AsyncHTTPClient|HTTPClient|NIOHTTPClient|"
            r"NIOHTTP1)\b"
        ),
    ),
    (
        "libcurl",
        re.compile(r"\b(?:libcurl|curl_(?:easy|multi|share|url)_)"),
    ),
    (
        "dynamic loading",
        re.compile(
            r"\b(?:dlopen|dlmopen|dlsym|LoadLibrary[A-Za-z]*|"
            r"GetProcAddress)\b"
        ),
    ),
    (
        "network command",
        re.compile(
            r"(?:/dev/(?:tcp|udp)|\b(?:curl|wget|aria2c|ftp|nc|ncat|"
            r"netcat|socat|urllib\.request|http\.client)\b)"
        ),
    ),
)
SOURCE_SUFFIXES = frozenset(
    {
        ".c",
        ".cc",
        ".cpp",
        ".cxx",
        ".h",
        ".hh",
        ".hpp",
        ".proto",
        ".sh",
        ".swift",
    }
)
IGNORED_SOURCE_PARTS = frozenset(
    {".build", ".git", "build", "fixtures", "test", "tests"}
)
NETWORK_COMMAND_PATTERN = re.compile(
    r"(?:^[ \t]*|[;&|()\n][ \t]*|"
    r"\b(?:exec|env|command|sudo|if|then|do|while|until)\s+)"
    r"(?:[A-Za-z_][A-Za-z0-9_]*=[^\s]+\s+)*"
    r"(?:/[^\s\"']+/)?"
    r"(?:curl|wget|aria2c|ftp|nc|ncat|netcat|socat)\b",
    re.IGNORECASE | re.MULTILINE,
)


class ProductNetworkAuditError(RuntimeError):
    """An artifact cannot be proven free of forbidden network capability."""


def _read_prefix(path: Path, size: int) -> bytes:
    try:
        with path.open("rb") as artifact:
            return artifact.read(size)
    except OSError as error:
        raise ProductNetworkAuditError(f"cannot read product artifact {path}: {error}") from error


def _readelf(path: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["readelf", *arguments, str(path)],
            capture_output=True,
            text=True,
            errors="replace",
        )
    except OSError as error:
        raise ProductNetworkAuditError(
            f"cannot inspect ELF metadata for {path}: {error}"
        ) from error
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "readelf failed"
        raise ProductNetworkAuditError(
            f"cannot inspect ELF metadata for {path}: {detail}"
        )
    return result.stdout


def audit_elf(path: Path) -> None:
    metadata = "\n".join(
        (
            _readelf(path, "-dW"),
            _readelf(path, "-sW"),
        )
    ).casefold()
    present = sorted(
        marker
        for marker in FORBIDDEN_ELF_METADATA_MARKERS
        if marker in metadata
    )
    if present:
        raise ProductNetworkAuditError(
            f"{path} exposes forbidden network capability metadata: "
            + ", ".join(present)
        )
    content = _read_all_bytes(path).lower()
    rodata_markers = sorted(
        marker
        for marker in FORBIDDEN_ELF_RODATA_MARKERS
        if marker.encode("ascii") in content
    )
    if rodata_markers:
        raise ProductNetworkAuditError(
            f"{path} embeds forbidden network capability identifier(s): "
            + ", ".join(rodata_markers)
        )


def _read_all_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as error:
        raise ProductNetworkAuditError(f"cannot read product artifact {path}: {error}") from error


def _is_executable(path: Path) -> bool:
    try:
        mode = path.stat().st_mode
    except OSError as error:
        raise ProductNetworkAuditError(f"cannot stat product artifact {path}: {error}") from error
    return bool(mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))


def audit_script(path: Path) -> None:
    try:
        contents = path.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        raise ProductNetworkAuditError(f"cannot read executable script {path}: {error}") from error
    match = NETWORK_COMMAND_PATTERN.search(contents)
    if match is not None:
        line = contents.count("\n", 0, match.start()) + 1
        raise ProductNetworkAuditError(
            f"{path}:{line} invokes forbidden network command: {match.group(0).strip()}"
        )


def audit_source_file(path: Path) -> None:
    try:
        contents = path.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        raise ProductNetworkAuditError(f"cannot read product source {path}: {error}") from error
    for capability, pattern in FORBIDDEN_SOURCE_PATTERNS:
        match = pattern.search(contents)
        if match is None:
            continue
        line = contents.count("\n", 0, match.start()) + 1
        raise ProductNetworkAuditError(
            f"{path}:{line} contains forbidden source capability "
            f"{capability}: {match.group(0)}"
        )


def _is_product_source(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    if any(part.casefold() in IGNORED_SOURCE_PARTS for part in relative.parts[:-1]):
        return False
    return path.suffix.casefold() in SOURCE_SUFFIXES or path.name.endswith(".sh.in")


def audit_source_tree(root: Path) -> int:
    if not root.is_dir():
        raise ProductNetworkAuditError(f"product source tree is missing: {root}")
    count = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file() or not _is_product_source(path, root):
            continue
        audit_source_file(path)
        count += 1
    if count == 0:
        raise ProductNetworkAuditError(f"product source tree has no auditable sources: {root}")
    return count


def audit_artifact(path: Path) -> str:
    """Audit one regular file and return ``elf``, ``script``, or ``data``."""
    if path.is_symlink():
        raise ProductNetworkAuditError(
            f"symbolic link requires a package-root audit: {path}"
        )
    if not path.is_file():
        raise ProductNetworkAuditError(f"product artifact is not a regular file: {path}")

    prefix = _read_prefix(path, 4)
    if prefix == ELF_MAGIC:
        audit_elf(path)
        return "elf"
    if prefix.startswith(b"#!"):
        if not _is_executable(path):
            return "data"
        audit_script(path)
        return "script"
    if _is_executable(path):
        raise ProductNetworkAuditError(
            f"executable product artifact has unsupported format: {path}"
        )
    return "data"


def resolve_packaged_symlink(path: Path, package_root: Path) -> Path:
    """Resolve one package-internal symlink without following host paths."""
    try:
        raw_target = Path(os.readlink(path))
    except OSError as error:
        raise ProductNetworkAuditError(
            f"cannot read packaged symbolic link {path}: {error}"
        ) from error

    root = Path(os.path.abspath(package_root))
    if raw_target.is_absolute():
        candidate = root / raw_target.relative_to("/")
    else:
        candidate = path.parent / raw_target
    candidate = Path(os.path.abspath(candidate))
    try:
        relative = candidate.relative_to(root)
    except ValueError as error:
        raise ProductNetworkAuditError(
            f"packaged symbolic link escapes product root: {path} -> {raw_target}"
        ) from error

    cursor = root
    for part in relative.parts:
        cursor /= part
        if cursor.is_symlink():
            raise ProductNetworkAuditError(
                f"packaged symbolic link has a chained target: {path} -> {raw_target}"
            )
    if not candidate.is_file():
        raise ProductNetworkAuditError(
            f"packaged symbolic link target is missing or not a file: "
            f"{path} -> {raw_target}"
        )
    return candidate


def audit_tree(root: Path, *, require_elf: bool = True) -> dict[str, int]:
    if not root.is_dir():
        raise ProductNetworkAuditError(f"product tree is missing: {root}")
    counts = {"elf": 0, "script": 0, "data": 0, "symlink": 0}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            target = resolve_packaged_symlink(path, root)
            audit_artifact(target)
            counts["symlink"] += 1
            continue
        if not path.is_file():
            continue
        kind = audit_artifact(path)
        counts[kind] += 1
    if require_elf and counts["elf"] == 0:
        raise ProductNetworkAuditError(f"product tree contains no auditable ELF artifacts: {root}")
    return counts


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", action="append", default=[], type=Path)
    parser.add_argument("--artifact", action="append", default=[], type=Path)
    parser.add_argument("--source-root", action="append", default=[], type=Path)
    parser.add_argument("--source-file", action="append", default=[], type=Path)
    arguments = parser.parse_args()
    if not any(
        (
            arguments.root,
            arguments.artifact,
            arguments.source_root,
            arguments.source_file,
        )
    ):
        parser.error(
            "at least one --root, --artifact, --source-root, or --source-file is required"
        )
    return arguments


def main() -> int:
    arguments = parse_arguments()
    try:
        summaries: list[str] = []
        for root in arguments.root:
            counts = audit_tree(root)
            summaries.append(
                f"{root}: {counts['elf']} ELF, {counts['script']} script, "
                f"{counts['data']} data, {counts['symlink']} symlink"
            )
        for artifact in arguments.artifact:
            summaries.append(f"{artifact}: {audit_artifact(artifact)}")
        for root in arguments.source_root:
            summaries.append(f"{root}: {audit_source_tree(root)} source files")
        for source in arguments.source_file:
            audit_source_file(source)
            summaries.append(f"{source}: source file")
    except ProductNetworkAuditError as error:
        print(f"product network audit failed: {error}", file=sys.stderr)
        return 1
    print("audited product network capabilities: " + "; ".join(summaries))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
