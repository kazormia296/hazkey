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

The release workflow also creates an ephemeral payload `.deb` from the
already-validated x86_64 release tree. It relocates the Fcitx addon from the
cross-distribution `/usr/lib/fcitx5` archive path to Ubuntu's multiarch addon
directory, then performs a real `dpkg` install, verify, and remove transaction
on Ubuntu 22.04. This exercises package ownership and removal without claiming
that the source snapshot is an offline-buildable Debian package. The payload
`.deb` is a CI fixture and is not a release asset.

## AUR binary release

`aur/fcitx5-grimodex-bin` consumes one release archive per architecture. Each
archive is a DESTDIR-style tree rooted at `usr/` and must ship the root
`LICENSE`, `NOTICE.md`, and all third-party notices required by its linked or
bundled components. A same-release `.sha256` sidecar is mandatory and is
checked before extraction. The committed PKGBUILD is an intentionally
unpublishable template: its four `RELEASE_*` values must be replaced with the
archive and sidecar SHA-256 values by `scripts/render_aur_release.py`.
`noextract` keeps makepkg from unpacking either archive before verification.

The release workflow renders this fixed-hash PKGBUILD from both architecture
artifacts, regenerates `.SRCINFO` with `makepkg --printsrcinfo`, builds the
x86_64 package from the local artifact cache, and performs a real `pacman`
install, verify, upgrade, and remove transaction. The rendered PKGBUILD and
`.SRCINFO` are preserved with the release inputs for the later AUR publication
step; the template itself must never be pushed to AUR. Both native release
runners validate the canonical tree and every ELF dependency before archiving,
including aarch64. The full `makepkg`/`pacman` transaction is x86_64-only until
a maintained native Arch Linux ARM runner image is available.

## Validation

Run the stdlib-only contract suite:

```sh
python3 packaging/tests/package_contract_test.py
```

To inspect a real staged install, set `GRIMODEX_STAGED_ROOT` to its DESTDIR.
To inspect individual unpacked executables or libraries, set
`GRIMODEX_PRODUCT_ARTIFACTS` to an `os.pathsep`-separated list. Staged files and
provided product artifacts are rejected if they expose old Hazkey paths or
link concrete Qt, Foundation, Swift NIO, or libcurl network-client libraries
or symbols. The release build inspects full ELF symbol tables before stripping;
the package and AUR checks repeat the dependency and dynamic-symbol inspection
without treating arbitrary license or resource text as executable capability.
The same release gate rejects Internet socket families, dynamic loading, and
download clients in production source while allowing the required AF_UNIX IPC.
A separate fail-closed source audit verifies that the exact pinned
`swift-tokenizers` checkout still uses its historical Hub module only for local
JSON configuration; provider-domain metadata by itself is not treated as a
network capability. These checks complement, rather than replace, package
manager install/uninstall tests on Debian and Arch runners.

The server package also owns the SwiftPM resource bundles located beside the
real server executable. In particular, the EfficientNGram tokenizer data and
the tokenizer fallback configuration must survive staging and uninstall as a
single package-owned tree; the executable's build-machine fallback paths are
not usable on an installed system.

The Zenzai GGUF is intentionally not part of the package archive or the IME
application releases. The dedicated `kazormia296/grimodex-models` repository
publishes the pinned model as the immutable
`zenzai-v3-small-q5km-v1` Release asset and includes its SHA-256 catalog. The
package contains only the Qt-based `fcitx5-grimodex-model` helper, which
downloads that fixed asset into the invoking user's XDG data directory with an
atomic replace and checksum verification. It retains a fallback to the pinned
upstream source so an updated helper can recover installations whose app
release predates the model release. Debian's
`postinst` and the AUR install hook make a best-effort download for the user who
invoked `sudo`; when no invoking user is available, the settings application
exposes the same download action. The helper is the sole intentionally
network-capable product artifact and is explicitly allowlisted by the packaging
audit.

The integration workflow installs the already-built Fcitx addon, Qt settings
application, and Swift server with CMake into two DESTDIR trees, merges those
trees, and runs this validator with `GRIMODEX_STAGED_ROOT`. The validator checks
both install and uninstall ownership manifests against every real staged file
and simulates removal of those owned files. This reuses the functional-test
builds and does not perform a second Swift build.

The release workflow adds the package-manager layer. It normalizes archive
ordering, timestamp, and ownership metadata, then passes the same immutable
artifact through the `dpkg` payload transaction and the fixed-hash AUR package
through the `pacman` transaction. Both jobs preserve Hazkey system and user
paths, the four Grimodex IME XDG roots, and the Grimodex protocol snapshot root
as sentinels.
`publish-release` depends on both transactions, so a tag cannot publish assets
that failed either package gate.
A fully offline Debian source build remains unavailable for the reasons above.
