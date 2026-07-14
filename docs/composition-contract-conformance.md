# Linux Composition Behavior Contract conformance

Hazkey Internal Protocol v2 is intentionally separate from Grimodex's
OS-independent `composition-behavior-v1` and from the project dictionary
Snapshot Protocol v1.

Linux pins all nine common scenarios under
`hazkey-server/Tests/grimodex-spike/Fixtures/composition-behavior-v1`. The
SHA-256 lock is checked by CMake, and the Swift adapter executes every action
with the shared deterministic fake converter. Every step compares status,
revision, phase, all preedit spans, UTF-8 caret, candidate IDs/generation,
effects, and final learning call totals.

That exact assertion is the persistent-learning v1 claim. The experimental
Mozc adapter does not claim v1 conformance: its separately named compatibility
test replays the same visible/session traces with an explicit
`allowsLearning=false` policy, records the real converter callbacks, requires
all steps to publish `pendingLearning=false`, and requires persistent learning
callbacks to remain zero. `setCompletedData` is observed but non-normative
because it is a process-local completion cache.

The proposed profile and migration rules are recorded in
[`adr-002-mozc-conversion-only-learning-profile.md`](./adr-002-mozc-conversion-only-learning-profile.md).
Formal v2 conformance remains blocked until the central Grimodex contract owns
the new schema/profile and Linux vendors its SHA-256-locked copy. The additive
open-session `persistent_learning_available` field exposes current runtime
backend capability without bumping Protocol v2 or snapshot versions; absence
means a legacy server with unknown capability.

Platform-specific layers are tested separately while retaining the shared
scenario IDs:

- `ImeReducer` owns composition, cursor, candidates, policy, checkpoints, and
  monotonic effects.
- `HazkeyActionMapper` maps Fcitx/JIS/US keys without reading candidate-panel
  focus.
- `HazkeySnapshotRenderer` checks Unicode byte carets and both client/panel
  preedit paths.
- `HazkeySessionClient` tests request replay, checkpoint restore, session
  replacement, and effect deduplication.
- process E2E starts the real Unix-socket server and covers project dictionary,
  P1 operations, response duplication, stale candidates, and restart recovery.

Protocol v2 is the only composition protocol. The retired procedural v1 field
numbers remain reserved in `base.proto` and are not accepted or generated.
Rollback across that retired procedural-v1/current-v2 boundary means
reinstalling the previous package as one coherent client/server pair; those
mixed binaries fail capability negotiation instead of falling back silently.
The optional learning-capability field is intentionally different: old clients
ignore it, and updated clients accept an old server's absence as unknown.
