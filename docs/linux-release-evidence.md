# Linux IME Phase 0–11 implementation and release evidence

Date: 2026-07-13

This document records the implementation status of
`grimodex-fcitx5-ime-architecture-implementation-plan.md`. The Linux product
code and every local automated gate are complete. Interactive typing in named
GUI applications remains a release-machine sign-off because it requires a
logged-in desktop and human keyboard input; hosted package/release workflows
also remain pending until the changes are published. Neither is counted as a
pass below.

## Phase status

| Phase | Status | Evidence |
|---|---|---|
| 0. Shared contract and baseline | implemented | `composition-behavior-v1` is versioned in Grimodex, SHA-256 locked in all three platform repositories, and all nine scenarios run through the Linux reducer adapter. The ADR separates the shared semantic contract, Grimodex Snapshot Protocol v1, and Linux-private wire protocol. |
| 1. Swift domain extraction | implemented | `CompositionSession`, `ImePhase`, `ImeAction`, `ImeReducer`, `SessionSnapshot`, and `KanaKanjiConverterPort` own semantic state and are tested with fake converters. |
| 2. Protocol v2 and idempotency | implemented | semantic actions, snapshots, capability negotiation, request IDs, revisions, candidate generations, checkpoints, and Effect IDs are generated for Swift and C++. Duplicate/stale/fault tests and real-process round trips pass. The temporary v1/v2 coexistence condition was intentionally superseded by Phase 10. |
| 3. Snapshot renderer | implemented | one renderer handles client and input-panel preedit, span styles, aux text, and UTF-8 caret offsets. Unicode and both rendering paths are tested. |
| 4. Composing editor | implemented | cursor movement, edge movement, insertion, deletion, staged cancel, converted-display commit, checkpoint, journal, and acknowledgement paths are reducer/client owned. |
| 5. Candidate UX | implemented | candidate/page movement, stable edge behavior, global page indices, number selection, mouse selection, and generation-aware stale rejection are tested. |
| 6. Segment editing and partial commit | implemented | boundary resize, candidate regeneration, partial commit, remainder reconversion, left-context update, active-segment transforms, and one-time learning are reducer owned. C++ never reconstructs commit text. |
| 7. Japanese keyboard and modes | implemented | Henkan, Muhenkan, Kana, Eisu, Hankaku/Zenkaku, F6–F10, JIS/US printable input, and inverse-width Shift+Space routing have exhaustive mapper tests. |
| 8. Lifecycle, recovery, and secure input | implemented | deactivate/focus/capability transitions, retry, session-not-found recovery, process-local table rebinding, response-loss deduplication, converter failure, zero candidates, secure boundaries, capacity bounds, and multi-session isolation are tested. |
| 9. P0 release gate | local gate passed; external sign-off pending | Fcitx full-stack, Zenzai on/off, CPU/no-Zenzai fallback, 32 live compositions, package staging, offline/network audits, minimum Fcitx 5.0.4, and package contracts pass. The interactive application matrix and hosted package/release workflows still require sign-off. |
| 10. Legacy removal | implemented | v1 handlers and fields, `state.swift`, old preedit reconstruction, candidate-focus semantics, and distributed phase flags are removed. Protocol v2 is the only product path and all P0 tests pass on it. |
| 11. P1 | implemented | persistent user-dictionary CRUD/import/export and settings UI, candidate forgetting, surrounding-text reconversion, Unicode input, shortcuts, right context, and prediction acceptance are on the same action/snapshot model and covered by unit/process tests. |

## Automated evidence

The following gates passed against the final source tree:

| Gate | Result |
|---|---|
| Zenzai-enabled Swift suite | 141 tests, 4 environment-gated skips, 0 failures |
| Zenzai-disabled Release Swift suite | 141 tests, 4 environment-gated skips, 0 failures |
| Real-server process E2E, Zenzai enabled | 4/4 |
| Real-server process E2E, Zenzai disabled | 4/4 |
| Top-level CTest, Zenzai enabled | 12/12 |
| Top-level CTest, Zenzai disabled | 12/12 |
| Real Fcitx addon/server full stack | pass in both builds; client/panel preedit, Romaji commit, F7, JIS mode key, deactivate commit, direct Kana editing, and multiple InputContexts |
| Deterministic reducer invariant run | 1,000 semantic actions; caret, phase, generation, secure recovery, no-mutation-on-failure, and unique Effect-ID invariants pass |
| Live-session stress | 32 simultaneous compositions across 8 sockets; cancel/survivor isolation passes |
| Fresh staged package contract | 48/48 in both builds |
| Grimodex central contract | 10/10 Vitest |
| macOS reference Core | 69 tests; shared fixture lock/action audit passes |
| Windows gap baseline | Rust lock/gap test passes; all nine gaps remain explicit |

Canonical commands:

```sh
ctest --test-dir /tmp/hazkey-full-build --output-on-failure
ctest --test-dir /tmp/hazkey-nozenzai-build-20260713 --output-on-failure
GRIMODEX_STAGED_ROOT=/tmp/hazkey-stage-final-20260713 \
  python3 packaging/tests/package_contract_test.py
GRIMODEX_STAGED_ROOT=/tmp/hazkey-stage-nozenzai-final-20260713 \
  python3 packaging/tests/package_contract_test.py
```

The Fcitx full-stack CTest builds an embedded Fcitx instance with its official
display-free test frontend, loads the just-built Grimodex addon by absolute
path, starts the just-built Swift server in isolated XDG directories, and
asserts committed strings. This caught and now guards three real integration
defects: first-run double server startup, process-local input-table IDs in
recovery checkpoints, and virtual `InputContext` access during base
construction.

## P0 Definition of Done mapping

- Editing, candidate navigation, page/global selection, mouse selection,
  segment resize, partial commit, transforms, Japanese mode keys, and width
  inversion are covered by reducer, mapper, renderer, and Fcitx full-stack
  tests.
- Unicode corpus coverage includes Japanese, supplementary-plane scalars,
  combining marks, emoji sequences, variation selectors, mixed ASCII, and
  halfwidth Kana. Carets are bounded UTF-8 byte offsets at the Fcitx boundary.
- Input preservation and exactly-once effects are covered for converter
  failures, empty candidates, stale revisions/candidates, response loss,
  timeout/reconnect, and server replacement.
- Secure input excludes surrounding context, Zenzai, learning, persistence,
  checkpoints, and recovery journals. Crossing a secure boundary clears data
  from the other security domain.
- Each `InputContext` has an independent server session. Capacity, ownership,
  idle eviction, 32-session interleaving, and context-construction lifetime are
  tested.
- Linux consumes the shared nine-scenario contract. macOS records its reference
  adapter and Cocoa range exception; Windows records every non-conforming
  scenario with a migration slice. Neither blocks the Linux gate.

## Interactive release-machine sign-off

These checks are deliberately marked `MANUAL-PENDING`; automated Fcitx
coverage above is not represented as human application testing.

| ID | Environment | Required observation | Status |
|---|---|---|---|
| IME-MANUAL-GTK-WL | GTK text and password fields on Wayland | P0 typing/edit/convert works; password field publishes no candidates, context, or learning | MANUAL-PENDING |
| IME-MANUAL-QT-WL | Qt text field on Wayland | client preedit, caret, candidates, click selection, and focus switching match GTK | MANUAL-PENDING |
| IME-MANUAL-ELECTRON-WL | Grimodex/Electron editor on Wayland | composing, partial commit, reconversion, and project dictionary scope work | MANUAL-PENDING |
| IME-MANUAL-TERM-WL | terminal on Wayland | panel-preedit fallback, cursor editing, commit, and cancel work | MANUAL-PENDING |
| IME-MANUAL-X11 | the same representative clients under X11/XWayland | no input loss, duplicate commit, freeze, or caret-unit regression | MANUAL-PENDING |
| IME-MANUAL-JIS-US | physical JIS and US layouts | mode keys, punctuation, Henkan/Muhenkan, and Space/Shift+Space match the mapper fixtures | MANUAL-PENDING |
| IME-MANUAL-MULTI | two applications and multiple fields | compositions, candidates, policy, and learning never leak between fields | MANUAL-PENDING |

For each row, record distro, Fcitx version, display protocol, application
version, keyboard layout, Zenzai setting, result, and tester. A failed row is a
release blocker; it does not invalidate the completed architecture phases but
does prevent declaring a release candidate fully certified.

Package-manager installation/removal transactions remain enforced by the
Debian and Arch release workflows. Local validation used fresh CMake DESTDIR
trees and the same immutable 48-test package validator; no files were installed
into the host `/usr` tree. The hosted workflows have not run for this uncommitted
working tree and must pass after publication before declaring a release
candidate fully certified.
