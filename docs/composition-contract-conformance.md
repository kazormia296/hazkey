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
Rollback means reinstalling the previous package as one coherent client/server
pair; mixed protocol binaries fail capability negotiation instead of falling
back silently.
