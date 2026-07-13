# ADR-001: Swift CompositionSession as the IME source of truth

- Status: Accepted
- Scope: Fcitx5 Grimodex IME architecture refresh

## Decision

The Swift conversion server owns the semantic IME state in one
`CompositionSession`.  Fcitx owns only input-event mapping, rendering of the
latest `SessionSnapshot`, and application of idempotent `ClientEffect` values.

The session stores cursor positions in input-element units.  Display offsets
are derived at the boundary: preedit caret values use UTF-8 byte offsets and
surrounding-text anchors retain the existing Unicode-scalar contract.

Semantic requests use `request_id`, `expected_revision`, and candidate
`generation` values.  A duplicate request returns the cached result, a stale
revision changes no state, and a stale candidate is rejected.  Commit effects
have their own monotonic ID so a response retry cannot commit text twice.

## Consequences

- Candidate-panel focus is no longer an IME phase.
- Converter calls cross `KanaKanjiConverting`, so reducer tests can use a fake
  converter and inject failures without loading a dictionary.
- Grimodex project revision, secure-input status, Zenzai availability, and
  learning policy, input table, and keymap are pinned at composition start.
- `HazkeySessionEnvironment` owns only converter/configuration resources; it
  cannot own preedit, cursor, candidates, or commit state.
- Candidate learning commits and forget operations publish a shared revision,
  allowing other long-lived converter instances to invalidate their persisted
  learning cache at the next conversion boundary.
- Procedural Protocol v1 commands and the duplicate C++/Swift composition state
  are removed. Their field numbers are permanently reserved.

## Rollback

The client requires Protocol v2 capability negotiation. The last confirmed
snapshot plus the in-memory action journal is retained for reconnect. Product
rollback installs the previous package as one atomic client/server/settings
set; a mixed old/new pair is rejected instead of risking duplicated commits.
