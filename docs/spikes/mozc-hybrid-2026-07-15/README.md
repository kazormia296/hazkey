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

H1 considered promotion in 12/1,360 cases: 2 were rescued, 3 regressed,
and 7 remained incorrect. The input ABProbe v3 records candidate surfaces but
not their consuming counts. Evaluation schema v2 therefore marks runtime
boundary parity as unestablished and H1 as ineligible for active runtime use;
the product path may collect boundary-aware shadow counters while retaining
H0 output and origin routing.

For the 551 Mozc Top-1 misses:

- Hazkey is Top-1 in 234 cases (42.47%).
- The expected surface is below Top-1 in both candidate lists in 0 cases.
- It is below Top-1 only in Hazkey in 139 cases and only in Mozc in 6 cases.
- It is absent from both observed Top-10 lists in 172 cases (31.22%).

The theoretical rescue pool is substantial, but the tested expectation-blind
one-sided-consensus rule is not safe: it regresses more cases than it rescues.
H0 therefore remains the runtime default.

## Boundary-aware v4 result

An opt-in ABProbe v4 reacquisition used each adapter's `segmentCandidates`
path and recorded `{text, rank, consuming_count}` for all 1,360 cases. These
artifacts are local diagnostics under
`build-grimodex/mozc-hybrid-boundary-v4-20260715`.

| Boundary diagnostic | Cases | Rate |
|---|---:|---:|
| Hazkey Top-1 boundary matches Mozc Top-1 | 555 / 1360 | 40.81% |
| Hazkey Top-1 boundary differs | 805 / 1360 | 59.19% |
| Boundary-aware H1 promotion opportunity | 6 / 1360 | 0.44% |

All six surface-only opportunities remained boundary-eligible; none was
removed by the boundary check. They comprise `Docker`, `棚から` versus
`店から`, half-width versus full-width `4月`, and three occurrences of the
same half-width versus full-width `2つの` first clause. This is opportunity
evidence only, not a quality result.

The sealed corpus labels whole compositions while v4 observes the first
clause. The evaluator therefore reports zero comparable quality cases and
excludes all 1,360 cases from Top-1, rescue/regression, oracle, and miss-class
claims. A segment-labeled holdout, or an explicit composition-span contract
with reviewed target-parity inference, is required before activating H1.

The one-iteration, zero-warmup debug acquisition measured a 194.33 ms Hazkey
median (1029.81 ms P95) and a 2.74 ms Mozc median (14.08 ms P95). These are
adapter-path diagnostics, not product UI latency; the process-path measurements
below remain the relevant first-display and Space timings.

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
| 0 ms | 7.71 ms | 4.92 ms | 0 / 12 | 0 | 0 |
| 25 ms | 7.45 ms | 5.28 ms | 1 / 12 | 0 | 0 |
| 100 ms | 8.00 ms | 5.89 ms | 3 / 12 | 0 | 0 |

Measured counters: `prefetch_started=36`, `prefetch_ready=7`,
`formal_ready_consumed=7`, `formal_deadline_miss=29`,
`stale_discarded=29`, `late_completion_discarded=29`,
`hazkey_requests=36`, `merged_requests=4`, `boundary_mismatch=3`, and
`hazkey_failure=0`. The shadow H1 evaluated 7 ready results, found 0 promotion
opportunities, and rejected 3 boundary-mismatched Hazkey Top-1 results. Hazkey
totaled 5.1859 s, about 144.1 ms/request.

### Zenzai enabled

This run enabled the local 69 MiB model and configured GGML backend.

| Prefetch allowance | Mozc first-display median | Space median | Windows with a surface absent from Mozc baseline | Top-1 changes | Candidate jumps |
|---:|---:|---:|---:|---:|---:|
| 0 ms | 6.10 ms | 4.51 ms | 0 / 12 | 0 | 0 |
| 25 ms | 7.53 ms | 5.69 ms | 0 / 12 | 0 | 0 |
| 100 ms | 7.38 ms | 5.19 ms | 1 / 12 | 0 | 0 |

Measured counters: `prefetch_started=36`, `prefetch_ready=2`,
`formal_ready_consumed=2`, `formal_deadline_miss=34`,
`stale_discarded=34`, `late_completion_discarded=34`,
`hazkey_requests=36`, `merged_requests=1`, `boundary_mismatch=1`, and
`hazkey_failure=0`. The shadow H1 evaluated 2 ready results, found 0 promotion
opportunities, and rejected 1 boundary-mismatched Hazkey Top-1 result. Hazkey
totaled 8.9871 s, about 249.6 ms/request.

### Endpoint memory

These are endpoint snapshots, not peak or simultaneous samples.

| Mode / snapshot | Server RSS / PSS | Helper RSS / PSS | Total PSS |
|---|---:|---:|---:|
| Zenzai off / before | 177,532 / 61,159 KiB | 20,392 / 16,503 KiB | 77,662 KiB |
| Zenzai off / after | 182,608 / 66,075 KiB | 23,336 / 19,447 KiB | 85,522 KiB |
| Zenzai on / before | 300,772 / 160,312 KiB | 20,316 / 16,411 KiB | 176,723 KiB |
| Zenzai on / after | 316,728 / 176,204 KiB | 23,240 / 19,335 KiB | 195,539 KiB |

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

Concretely, the metric increments only when the formal window contains at least
one NFC-normalized surface absent from the separately captured Mozc baseline.
Candidate reordering, duplicate surfaces, and canonically equivalent spellings
alone do not increment it.

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
