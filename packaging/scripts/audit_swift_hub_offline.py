#!/usr/bin/env python3
"""Fail closed if the pinned Swift tokenizer Hub gains network capability.

The pinned ensan-hcl/swift-tokenizers fork uses its ``Hub`` module only to
decode tokenizer JSON already present on disk.  Its historical API still
contains provider-domain metadata, so domain strings alone are not evidence of
a downloader.  This audit verifies the exact resolved checkout and rejects
concrete Swift, NIO, or libcurl networking APIs in the Hub sources.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys


PACKAGE_IDENTITY = "swift-tokenizers"
REQUIRED_HUB_SOURCES = frozenset({"Downloader.swift", "Hub.swift", "HubApi.swift"})
NETWORK_CAPABILITY_PATTERNS = (
    (
        "Foundation URL loading",
        re.compile(
            r"\b(?:URLSession[A-Za-z0-9_]*|URLRequest|URLProtocol|"
            r"HTTPURLResponse|NSURLConnection)\b"
        ),
    ),
    ("FoundationNetworking", re.compile(r"\bFoundationNetworking\b")),
    ("Network framework", re.compile(r"\b(?:import\s+Network|NWConnection|NWListener|NWBrowser|NWConnectionGroup)\b")),
    (
        "Swift HTTP client",
        re.compile(r"\b(?:AsyncHTTPClient|HTTPClient|NIOHTTPClient|NIOHTTP1)\b"),
    ),
    (
        "libcurl",
        re.compile(r"\bcurl_(?:easy|multi|share|url)_[A-Za-z0-9_]+\b"),
    ),
    (
        "POSIX or platform sockets",
        re.compile(
            r"\bimport\s+(?:Glibc|Musl|Darwin|WinSDK|CWinSock)\b|"
            r"\b(?:AF_INET6?|PF_INET6?|sockaddr_in6?|getaddrinfo|getnameinfo|"
            r"gethostbyname2?|inet_(?:addr|aton|ntoa|ntop|pton)|syscall)\b|"
            r"\b(?:socket|connect|sendto|recvfrom)\s*\("
        ),
    ),
    (
        "external process execution",
        re.compile(
            r"\b(?:Process|NSTask)\s*\(|"
            r"\b(?:posix_spawnp?|popen|system|exec[lv]p?e?)\s*\("
        ),
    ),
)


class OfflineHubAuditError(RuntimeError):
    """The checkout cannot be proven to be the approved local-only Hub."""


def audit_hub_sources(hub_directory: Path) -> tuple[Path, ...]:
    """Audit every Swift source in an exact ``Sources/Hub`` directory."""
    if not hub_directory.is_dir():
        raise OfflineHubAuditError(
            f"expected Hub source directory is missing: {hub_directory}"
        )

    sources = tuple(sorted(hub_directory.rglob("*.swift")))
    source_names = {path.name for path in sources}
    missing = REQUIRED_HUB_SOURCES - source_names
    if missing:
        raise OfflineHubAuditError(
            "expected pinned Hub source file(s) missing: " + ", ".join(sorted(missing))
        )

    for source in sources:
        contents = source.read_text(encoding="utf-8")
        for capability, pattern in NETWORK_CAPABILITY_PATTERNS:
            match = pattern.search(contents)
            if match is not None:
                line = contents.count("\n", 0, match.start()) + 1
                raise OfflineHubAuditError(
                    f"{source}:{line} contains forbidden network capability "
                    f"{capability}: {match.group(0)}"
                )
    return sources


def resolved_revision(package_resolved: Path) -> str:
    if not package_resolved.is_file():
        raise OfflineHubAuditError(
            f"Package.resolved is missing: {package_resolved}"
        )
    try:
        document = json.loads(package_resolved.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        raise OfflineHubAuditError(
            f"cannot read {package_resolved}: {error}"
        ) from error

    pins = [
        pin
        for pin in document.get("pins", [])
        if pin.get("identity") == PACKAGE_IDENTITY
    ]
    if len(pins) != 1:
        raise OfflineHubAuditError(
            f"expected exactly one {PACKAGE_IDENTITY} pin, found {len(pins)}"
        )
    revision = pins[0].get("state", {}).get("revision")
    if not isinstance(revision, str) or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise OfflineHubAuditError(
            f"{PACKAGE_IDENTITY} revision is not an exact 40-character commit"
        )
    return revision


def git_output(checkout: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(checkout), *arguments],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise OfflineHubAuditError(
            f"cannot inspect SwiftPM checkout {checkout}: {detail}"
        )
    return result.stdout.strip()


def audit_checkout(
    package_resolved: Path,
    checkout: Path,
    expected_revision: str,
) -> tuple[Path, ...]:
    if re.fullmatch(r"[0-9a-f]{40}", expected_revision) is None:
        raise OfflineHubAuditError("expected revision must be an exact lowercase commit")
    if checkout.name != PACKAGE_IDENTITY:
        raise OfflineHubAuditError(
            f"expected checkout path ending in {PACKAGE_IDENTITY}, got {checkout}"
        )
    if not checkout.is_dir():
        raise OfflineHubAuditError(f"SwiftPM checkout is missing: {checkout}")

    pinned_revision = resolved_revision(package_resolved)
    if pinned_revision != expected_revision:
        raise OfflineHubAuditError(
            f"resolved revision {pinned_revision} does not match expected revision "
            f"{expected_revision}"
        )

    checkout_revision = git_output(checkout, "rev-parse", "HEAD")
    if checkout_revision != expected_revision:
        raise OfflineHubAuditError(
            f"checkout revision {checkout_revision} does not match expected revision "
            f"{expected_revision}"
        )
    dirty = git_output(checkout, "status", "--porcelain", "--untracked-files=no")
    if dirty:
        raise OfflineHubAuditError(
            f"SwiftPM checkout has modified tracked source and is not auditable: {dirty}"
        )

    return audit_hub_sources(checkout / "Sources/Hub")


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-resolved", required=True, type=Path)
    parser.add_argument("--checkout", required=True, type=Path)
    parser.add_argument("--expected-revision", required=True)
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    try:
        sources = audit_checkout(
            arguments.package_resolved,
            arguments.checkout,
            arguments.expected_revision,
        )
    except OfflineHubAuditError as error:
        print(f"offline Swift Hub audit failed: {error}", file=sys.stderr)
        return 1
    print(
        f"audited {PACKAGE_IDENTITY}@{arguments.expected_revision}: "
        + ", ".join(path.name for path in sources)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
