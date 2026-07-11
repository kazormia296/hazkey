# Linux packaging contract

The Linux fork is published as the independent product `fcitx5-grimodex`.
Package managers must own only the paths in
`manifests/fcitx5-grimodex.install-paths`, and uninstall must remove the same
set. `manifests/fcitx5-hazkey.reference-paths` documents representative paths
owned by the upstream product. The two sets must remain disjoint.

Neither Debian nor Arch metadata declares `Conflicts`, `Replaces`, or
`Provides` against `fcitx5-hazkey`. The AUR binary package conflicts only with
the source form of the same Grimodex package and provides that same Grimodex
identity. Installing or removing Grimodex must not modify Hazkey files or user
data.

## Debian

`debian/` contains source-package metadata. It does not make the current tree
offline-buildable: SwiftPM dependencies and Git submodules are pinned but not
vendored. See `debian/README.source` for the precise constraint. Package CI may
create and inspect a `.dsc`, but that is not evidence of a policy-compliant
offline binary build.

## AUR binary release

`aur/fcitx5-grimodex-bin` consumes one release archive per architecture. Each
archive is a DESTDIR-style tree rooted at `usr/` and must ship the root
`LICENSE`, `NOTICE.md`, and all third-party notices required by its linked or
bundled components. A same-release `.sha256` sidecar is mandatory and is
checked before extraction. This is a staging definition until matching release
assets exist. Before publishing an AUR revision, the release maintainer must
replace `SKIP` integrity entries with immutable archive and sidecar hashes once
the final assets exist, then regenerate `.SRCINFO` with `makepkg --printsrcinfo`.

## Validation

Run the stdlib-only contract suite:

```sh
python3 packaging/tests/package_contract_test.py
```

To inspect a real staged install, set `GRIMODEX_STAGED_ROOT` to its DESTDIR.
To inspect individual unpacked executables or libraries, set
`GRIMODEX_PRODUCT_ARTIFACTS` to an `os.pathsep`-separated list. Staged files and
provided product artifacts are rejected if they expose old Hazkey paths or
embed Qt Network/Hugging Face download-client markers. These checks complement,
rather than replace, package manager install/uninstall tests on Debian and Arch
runners.
