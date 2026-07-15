# Mozc-first speculative hybrid spike — 2026-07-15

This note records a diagnostic working-tree spike. It is not release evidence
and does not authorize the diagnostic H1 ranking policy.

## Implemented runtime policy

- Editable input and synchronous candidate display stay on Mozc.
- Hazkey prepares only the first natural segment for the complete input, using
  one request for the same composition revision on a background serial worker.
- Space consumes the result only after that request and the shared Hazkey gate
  have completed. Pending work is cancelled logically and Space stays Mozc-only.
- Edit, cursor, segment, commit, cancel, lifecycle, dictionary, configuration,
  learning-revision, and secure-domain changes invalidate old work.
- Candidate navigation never changes the published generation or item order.
- Runtime H0 preserves Mozc Top-1 and its stable Top-3. Hazkey only fills unique
  candidates below that prefix. Only selected Hazkey-origin candidates learn.
- Hazkey workers, learning, user dictionaries, configuration, model reload,
  history reset, and teardown share one registry-wide execution fence.
- A learnable ready Hazkey window owns a registry-wide candidate-learning
  fence through commit/discard/cancel. It blocks new Hazkey speculation, not
  Mozc display or Mozc-only formal conversion.

## Offline quality result

Input: the paired 1,360-case ABProbe v3 acquisition under
`build-grimodex/hazkey-server/mozc-v2-b0-objective-20260715`.

| Policy/backend | Top-1 hits | Rate | Rescued | Regressed | Net |
|---|---:|---:|---:|---:|---:|
| Hazkey | 909 / 1360 | 66.84% | — | — | — |
| Mozc | 809 / 1360 | 59.49% | — | — | — |
| Runtime H0 (`preserveMozcTop1`) | 809 / 1360 | 59.49% | 0 | 0 | 0 |
| Diagnostic H1 (`oneSidedConsensus`) | 808 / 1360 | 59.41% | 2 | 3 | -1 |

For the 551 Mozc Top-1 misses:

- Hazkey is Top-1 in 234 cases (42.47%).
- The expected surface is below Top-1 in both candidate lists in 0 cases.
- It is below Top-1 only in Hazkey in 139 cases and only in Mozc in 6 cases.
- It is absent from both observed Top-10 lists in 172 cases (31.22%).

The theoretical rescue pool is substantial, but the tested expectation-blind
one-sided-consensus rule is not safe: it regresses more cases than it rescues.
H0 therefore remains the runtime default.

## Product-path timing and memory samples

The opt-in process probe launched the real debug server, fixed Mozc helper/data,
Unix socket, and Protocol v2. It used 12 cases, one iteration per case, and a
0/25/100 ms wait between the Mozc display response and Space. Counters below
are sums of quiesced before/after deltas for the 36 measured windows; warm-up
and reset cleanup are excluded. Both runs use the same `ZenzaiSupport` binary
with a valid model available; only the profile's Zenzai enable flag differs.

### Zenzai disabled

| Prefetch allowance | Mozc first-display median | Space median | Windows with a surface absent from Mozc baseline | Top-1 changes | Candidate jumps |
|---:|---:|---:|---:|---:|---:|
| 0 ms | 6.76 ms | 4.47 ms | 0 / 12 | 0 | 0 |
| 25 ms | 7.03 ms | 5.12 ms | 1 / 12 | 0 | 0 |
| 100 ms | 6.30 ms | 5.69 ms | 3 / 12 | 0 | 0 |

Measured counters: `prefetch_started=36`, `prefetch_ready=7`,
`formal_ready_consumed=7`, `formal_deadline_miss=29`,
`stale_discarded=29`, `late_completion_discarded=29`,
`hazkey_requests=36`, `merged_requests=4`, `boundary_mismatch=3`, and
`hazkey_failure=0`. Hazkey totaled 5.4274 s, about 150.8 ms/request.

### Zenzai enabled

This run enabled the local 69 MiB model and configured GGML backend.

| Prefetch allowance | Mozc first-display median | Space median | Windows with a surface absent from Mozc baseline | Top-1 changes | Candidate jumps |
|---:|---:|---:|---:|---:|---:|
| 0 ms | 8.52 ms | 5.15 ms | 0 / 12 | 0 | 0 |
| 25 ms | 8.36 ms | 5.47 ms | 0 / 12 | 0 | 0 |
| 100 ms | 5.90 ms | 4.08 ms | 1 / 12 | 0 | 0 |

Measured counters: `prefetch_started=36`, `prefetch_ready=2`,
`formal_ready_consumed=2`, `formal_deadline_miss=34`,
`stale_discarded=34`, `late_completion_discarded=34`,
`hazkey_requests=36`, `merged_requests=1`, `boundary_mismatch=1`, and
`hazkey_failure=0`. Hazkey totaled 9.4141 s, about 261.5 ms/request.

### Endpoint memory

These are endpoint snapshots, not peak or simultaneous samples.

| Mode / snapshot | Server RSS / PSS | Helper RSS / PSS | Total PSS |
|---|---:|---:|---:|
| Zenzai off / before | 177,420 / 61,075 KiB | 20,400 / 16,500 KiB | 77,575 KiB |
| Zenzai off / after | 182,488 / 65,984 KiB | 23,328 / 19,428 KiB | 85,412 KiB |
| Zenzai on / before | 300,648 / 160,159 KiB | 20,280 / 16,471 KiB | 176,630 KiB |
| Zenzai on / after | 310,112 / 169,559 KiB | 23,184 / 19,375 KiB | 188,934 KiB |

The local reports are `build-grimodex/hybrid-runtime-spike.json` (Zenzai on)
and `build-grimodex/hybrid-runtime-spike-no-zenzai.json` (Zenzai off).

## Two-session contention result

A deterministic barrier test uses two hybrid converters with the same
registry-style execution gate and serial executor. While session A owns a
learnable ready window, session B cannot enter its Hazkey converter, but its
Mozc display, realtime candidates, and Mozc-only formal fallback complete.
When A commits learning, the underlying Hazkey commit is observed before B's
queued speculative call enters Hazkey. Discard, formal Mozc failure, boundary
mismatch, partial multi-segment rollback, segment resize, candidate transform,
unlearnable fallback, learning-revision mismatch, and secure purge cover the
principal release paths; registry maintenance and teardown use an explicit
admission fence around invalidation and exclusive Hazkey mutation.

## Interpretation

Within this single-session probe, formal conversion never joined Hazkey work,
no candidate jump was observed, and H0 never changed Mozc Top-1. First-step,
ready-only publication also prevents a later clause request from blocking
learning for a candidate just published by the same session.

Readiness remains the bottleneck: at 100 ms only 3/12 windows without Zenzai
and 1/12 with Zenzai contained a normalized surface absent from the separately
captured Mozc baseline. This window-level metric does not identify backend
provenance or count individual additions. Merely adding later candidates still
does not improve one-key Top-1, while the tested H1 promotion rule regresses net
accuracy.

The post-spike two-session barrier probe reproduced the shared-gate head-of-line
risk and added a candidate-learning fence. Once a learnable Hazkey result is
ready, queued speculation from other sessions cannot enter Hazkey until the
candidate window and any staged-learning decision are resolved. Mozc display
and Mozc-only formal conversion remain available while that fence is held, and
maintenance/secure/teardown paths take their own admission fence.

This preserves synchronous learning durability without an asynchronous journal.
The tradeoff is conservative: all sessions pause Hazkey prefetch while a
learnable candidate window or undo decision remains open. An already-active
Hazkey request is still not preemptible. Process isolation remains the scalable
next step if that global pause is too costly. Any Top-1 promotion also needs a
new, reviewed holdout.
