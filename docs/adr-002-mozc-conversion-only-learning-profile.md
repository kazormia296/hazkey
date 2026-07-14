# ADR-002: Mozc S0 as a conversion-only learning profile

- Status: Proposed; central composition contract v2 is not yet accepted
- Scope: Experimental Mozc backend and default-switch gate

## Context

The authoritative `composition-behavior-v1` contract requires exact final
learning totals. Its `partial-commit` scenario requires completed/updated/
committed/forgotten totals of `1/1/1/0`. The Mozc S0 sidecar deliberately runs
with user history disabled and exposes only conversion and PING operations.
Replacing the v1 totals with zero in a platform test is therefore compatibility
coverage, not v1 conformance.

`setCompletedData` is also not a persistent-history event. The reducer invokes
it as a process-local completion-cache callback even when persistent learning
is disabled. A conversion-only contract must not use that callback as a
normative learning counter.

## Proposed decision

The central Grimodex contract should add a versioned learning profile with
`learning: enabled | disabled` while retaining v1 during migration.

- `enabled` preserves every v1 learning total, including `1/1/1/0` for partial
  commit.
- `disabled` requires persistent stage/update/commit/discard/forget activity to
  remain zero and `pending_learning` to remain false after every action.
- visible status, revision, preedit, candidates, effects, and exactly-once
  commit behavior remain identical across profiles.
- `setCompletedData` remains non-normative because it is process-local.

Reducer permission to persist learning is the conjunction of backend
capability, composition-start policy, and a non-secure input context. Actual
Hazkey persistence is additionally subject to converter profile settings such
as input-history use and storing new history. A checkpoint may retain a
stricter `allowsLearning=false` policy, but it cannot enable learning on a
conversion-only backend.

Mozc S0 is conversion-only. Its helper keeps
`enable_user_history_for_conversion=false` and `incognito_mode=true`; the
private sidecar protocol remains limited to CONVERT and PING. Adding Mozc
learning would be a separate protocol and storage project.

## Runtime compatibility

Hazkey Internal Protocol v2 does not need a version bump. Existing
`pending_learning=false` and successful no-op resolution already represent a
conversion-only session. Current servers additionally set the additive
proto3-optional `OpenSessionResult.persistent_learning_available` field:

- present `true`: the backend can persist learning, subject to policy;
- present `false`: conversion-only backend;
- absent: legacy server, capability unknown.

This is backend capability, not the current secure-input or per-project policy.
Before a default switch, history-dependent settings and candidate-forget UI
must consume this capability and explain that they are unavailable. Clearing
dormant Hazkey history remains a distinct operation and must not silently turn
into a Mozc no-op.

## Current Hazkey preparation

The v1 persistent test remains exact and unchanged. The Mozc adapter test is
named as visible/session compatibility under a draft conversion-only profile,
sets `allowsLearning=false` on the real reducer session, records actual
callbacks, and requires all snapshots to publish no pending learning. It does
not claim v1 or formal v2 conformance.

Formal Gate 5 completion requires the authoritative Grimodex repository to land
the v2 schema/profile and migration tests first, followed by a SHA-256-locked
copy and profile verifier in this repository.

## Rollback

A runtime rollback may stop emitting the optional open-session field while
retaining field number 7 and its name in the schema. Updated clients then treat
absence as unknown, while old clients already ignore the unknown field when it
is present. The number must remain reserved if the field is ever removed from
the schema. Switching the experimental backend back to Hazkey restores the
persistent capability without migrating or deleting existing Hazkey history.
