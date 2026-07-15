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

| Prefetch allowance | Mozc first-display median | Space median | Candidate augmentation | Top-1 changes | Candidate jumps |
|---:|---:|---:|---:|---:|---:|
| 0 ms | 8.79 ms | 5.38 ms | 0 / 12 | 0 | 0 |
| 25 ms | 6.05 ms | 5.71 ms | 1 / 12 | 0 | 0 |
| 100 ms | 7.00 ms | 5.89 ms | 3 / 12 | 0 | 0 |

Measured counters: `prefetch_started=36`, `prefetch_ready=7`,
`formal_ready_consumed=7`, `formal_deadline_miss=29`,
`stale_discarded=29`, `late_completion_discarded=29`,
`hazkey_requests=36`, `merged_requests=4`, `boundary_mismatch=3`, and
`hazkey_failure=0`. Hazkey totaled 5.4380 s, about 151.1 ms/request.

### Zenzai enabled

This run enabled the local 69 MiB model and configured GGML backend.

| Prefetch allowance | Mozc first-display median | Space median | Candidate augmentation | Top-1 changes | Candidate jumps |
|---:|---:|---:|---:|---:|---:|
| 0 ms | 7.88 ms | 4.74 ms | 0 / 12 | 0 | 0 |
| 25 ms | 8.10 ms | 4.63 ms | 0 / 12 | 0 | 0 |
| 100 ms | 7.73 ms | 5.43 ms | 1 / 12 | 0 | 0 |

Measured counters: `prefetch_started=36`, `prefetch_ready=2`,
`formal_ready_consumed=2`, `formal_deadline_miss=34`,
`stale_discarded=34`, `late_completion_discarded=34`,
`hazkey_requests=36`, `merged_requests=1`, `boundary_mismatch=1`, and
`hazkey_failure=0`. Hazkey totaled 9.2260 s, about 256.3 ms/request.

### Endpoint memory

These are endpoint snapshots, not peak or simultaneous samples.

| Mode / snapshot | Server RSS / PSS | Helper RSS / PSS | Total PSS |
|---|---:|---:|---:|
| Zenzai off / before | 178,012 / 61,284 KiB | 20,308 / 16,404 KiB | 77,688 KiB |
| Zenzai off / after | 183,068 / 66,180 KiB | 23,236 / 19,332 KiB | 85,512 KiB |
| Zenzai on / before | 300,380 / 159,909 KiB | 20,376 / 16,524 KiB | 176,433 KiB |
| Zenzai on / after | 317,624 / 177,088 KiB | 23,308 / 19,456 KiB | 196,544 KiB |

The local reports are `build-grimodex/hybrid-runtime-spike.json` (Zenzai on)
and `build-grimodex/hybrid-runtime-spike-no-zenzai.json` (Zenzai off).

## Interpretation

Within this single-session probe, formal conversion never joined Hazkey work,
no candidate jump was observed, and H0 never changed Mozc Top-1. First-step,
ready-only publication also prevents a later clause request from blocking
learning for a candidate just published by the same session.

Readiness remains the bottleneck: at 100 ms only 3/12 windows were augmented
without Zenzai and 1/12 with Zenzai. Merely adding later candidates still does
not improve one-key Top-1, while the tested H1 promotion rule regresses net
accuracy.

One concurrency limitation remains outside this probe: all sessions share the
Hazkey execution gate. Learning a selected Hazkey candidate can wait behind a
different session's speculative request, and a subsequent request can therefore
lose the strict Mozc-immediate property. Before production enablement, add a
two-session contention probe and move learning to a deferred/priority path (or
isolate speculative Hazkey execution). Any Top-1 promotion also needs a new,
reviewed holdout.
