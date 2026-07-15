# Mozc adoption pilot contract v1

This directory locks the corpus shape and pass/fail thresholds for the first
pilot evaluation of the fixed B0 Mozc artifact. The pinned AJIMEE component is
kept separate from the reviewed product-owned 140 cases and protected 16 cases.
Their deterministic aggregate manifest and the ABProbe measurement contract are
frozen. The five long-running stability contracts and their native schemas are
also frozen, including the committed Fcitx native producer identity. This v1
contract is permanently `decision_tier=pilot` and
`formal_adoption_allowed=false`. Pilot-evaluation readiness is enabled at the
contract level; this does not assert that the five required native results
exist or pass. Evidence results do not need to exist for the contract itself to
be ready, but a complete `pilot_pass` requires all five. The legacy policy key
`formal_suite` names this frozen v1 shape only and does not authorize a formal
adoption decision.

## Immutable historical pilot evidence

v1 is an immutable historical pilot contract. Its policy and retained evidence
remain pinned to the runner, Swift package, and orchestration code used for the
original acquisition. A newer repository HEAD does not invalidate that
historical evidence, but it also must not be presented as having produced it.

Do not rewrite `b0-policy.json`, the published `decision.json`, or any retained
v1 evidence to match current source files. Acquiring evidence after a
policy-bound runner, package, or orchestrator changes requires a new policy
ID/revision with identities derived from that implementation, followed by a
fresh acquisition of every required result.

All policy rates use integer basis points. A score delta of `-800` means minus
8 percentage points; a ratio of `5000` means 50% of the Hazkey value. The human
net preference is `(B0 wins - B0 losses) / 256`: it must be at least -300 basis
points and at least -7 cases.

## Pilot 256-case suite

The suite is assembled deterministically from three separately licensed files.

| File | Cases | Contents |
|---|---:|---|
| `external-ajimee-unconditional.tsv` | 100 | AJIMEE-Bench cases whose original left context is empty |
| `product-curated.tsv` | 140 | Original Grimodex product cases in the counted categories below |
| `protected.tsv` | 16 | Must-pass protected and known-regression cases |

`product-curated.tsv` has exactly these counted categories:

| Category ID | Cases | Meaning |
|---|---:|---|
| `technical-mixed` | 32 | technical terms and mixed Japanese/alphanumeric input |
| `proper-noun` | 24 | proper nouns |
| `colloquial` | 24 | colloquial and internet usage |
| `homophone-context` | 20 | homophones and readings whose intended surface depends on context |
| `long-structural` | 20 | long or syntactically difficult input |
| `grimodex-regression` | 20 | Grimodex-specific terms and known regressions |

The aggregate manifest also uses `ajimee-unconditional` and `protected` as
source-boundary category IDs. The per-category Top-1 gate applies to all eight
aggregate IDs. The protected file additionally has an exact 16/16 gate.
Overall Top-1 and Top-10 use all 256 cases.

## AJIMEE provenance and transformation

AJIMEE-derived data stays in `external-ajimee-unconditional.tsv`; do not copy it
into either project-owned file. The pinned upstream input, observed on
2026-07-15, is:

- repository: <https://github.com/azooKey/AJIMEE-Bench>
- revision: `401666cd56d1a570c2021798b64b6da4396bfd45`
- raw path: `JWTD_v2/v1/evaluation_items.json`
- raw SHA-256: `e9eb668fd6aa14b1e26436f429b5550108af0a1dfd443b8cea0bcb3ab3028fca`
- raw shape: 200 cases, split into 100 empty-context and 100 contextual cases;
  83 raw cases have multiple accepted answers
- dataset license: CC BY-SA 3.0 (the upstream README separately labels its
  utilities and test utilities CC0)

The checked-in derived file was produced by
`tools/dictionary/build_frozen_corpus.py derive-ajimee`. Transform
`ajimee-unconditional-to-tsv.v1` selects only the 100 cases with an empty
original context, applies NFC and `katakana-to-hiragana.v1` to the input,
retains every distinct accepted answer in upstream order, and stable-dedupes
only exact duplicate answers. It sorts rows by the numeric upstream index and
uses that index in the stable case ID. Its exact output SHA-256 is
`91068dd92eddc70865c1b998843f38fd21d47458d1adf21799f9ad645e265fba`.
Attribution and license details are in [`LICENSES/AJIMEE-Bench-NOTICE.md`](LICENSES/AJIMEE-Bench-NOTICE.md).
Any redistributed aggregate contains this CC BY-SA 3.0 material and must retain
that notice and comply with the license for the covered material.

The contextual AJIMEE cases form a separate 100-case suite. They must not be
made context-free by deleting their context. Evaluate them only after Protocol
v2 or the product path can inject the original left context, and keep that
suite outside the v1 pilot 256-case score.

## Excluded and auxiliary data

- The existing 15-case
  `../ime-base-ab-v1/conversion-quality-v1.tsv` remains a fast sentinel
  regression suite. It is not part of the pilot score and must not be repeated
  or padded into the 256 cases.
- Mozc stress-test data is for parser robustness and soak testing only. It does
  not contribute to conversion-quality scores.
- [Microsoft Research IME Corpus](https://www.microsoft.com/en-us/research/publication/microsoft-research-ime-corpus/)
  may inform category design and annotation practice only. Its
  [non-commercial research license](https://download.microsoft.com/download/B/8/8/B88DDDC1-F316-412A-94B3-025788436054/LICENSE.pdf)
  forbids repository redistribution, so no examples or derived corpus rows may
  be committed here.
- [`Miwa-Keita/zenz-v2.5-dataset`](https://huggingface.co/datasets/Miwa-Keita/zenz-v2.5-dataset)
  may inform vocabulary distribution only. Its possible overlap with training
  data excludes its examples from the v1 pilot corpus.

## Frozen manifest and aggregate

`manifest.json` uses schema `hazkey.frozen-conversion-corpus-manifest.v1`. It
was frozen after all three real TSVs existed and independent validation
confirmed:

1. exact file byte hashes and counts of 100, 140, and 16;
2. the six product category counts, all eight aggregate category counts in
   `b0-policy.json`, and a total of 256;
3. unique, stable case IDs and retained multiple accepted answers;
4. the pinned AJIMEE revision, raw hash, license, attribution, and deterministic
   transform identity described above;
5. an explicit deterministic merge order and the SHA-256 of the exact generated
   256-case byte snapshot consumed by all eight ABProbe v3 runs; and
6. no placeholder, contextual AJIMEE, sentinel, stress, Microsoft, or zenz-v2.5
   rows in the pilot snapshot.

The component SHA-256 values are `91068dd9…e265fba`,
`32a72027…8a0e4`, and `18c384f5…3d768` in manifest order. The exact generated
256-case snapshot is
`sha256:123f47cb6f747135451e5969b32d9868ec61d9574fa6eb4b0001e5409287c807`;
the manifest file itself is
`sha256:b1319e356ba025e1e06221330479d48b12cc44ebb502ddda970ea5fa583336e3`.

```bash
python3 tools/dictionary/build_frozen_corpus.py build \
  --manifest hazkey-server/Tests/grimodex-spike/Fixtures/mozc-adoption-v1/manifest.json \
  --output /private/path/to/pilot-256.tsv
```

The frozen pilot evidence must bind that manifest hash, the hashed acquisition
manifest for all eight raw ABProbe v3 runs, the blind-review packet, and the
exact product executable, its 11 runtime dependencies, and the B0 helper/data
identities in `b0-policy.json`.
It must also use the frozen product implementation revision
`6e0354f2514edf1fe8219657ed23e7a02c8a7f7a`; a caller-selected lookalike source
revision is rejected.
Do not reuse results from B0 lookalikes or locally rebuilt artifacts. B1 is made
only if this B0 fails the pilot thresholds; B2 remains a future option.

## Locked pilot gates and measurement contracts

The fixed gates are: human net preference at least -3% (net loss no worse than
7 cases), overall Top-1 no more than 8 points below Hazkey, overall Top-10 no
more than 12 points below Hazkey, every counted category Top-1 no more than 10
points below Hazkey, protected 16/16, at most 12 `both_bad` judgments, warm
latency p95 no more than 50% of Hazkey, PSS no more than 125% of Hazkey, and all
required long-running stability checks passing.

The pilot performance comparison uses ABProbe v3 with exactly four runs per
backend in execution order `H1,M1,M2,H2,H3,M3,M4,H4`, 5 warmups and 20 measured
iterations per case, Top-10 output, and all 256 cases. Warm latency is the
nearest-rank p95 across every measured sample. PSS is the maximum simultaneous
parent-plus-backend before/after observation. CPU scheduling is explicitly
`unrestricted-same-host`; this contract makes no CPU-affinity or isolation
claim. Each run has a 900-second timeout; a timeout terminates the probe's
process group. The producer fingerprints the current boot, records the inherited
effective CPU affinity, and runs every probe sequentially from one parent
process. It does not inherit `LD_LIBRARY_PATH`, `GGML_BACKEND_DIR`,
`FCITX5_GRIMODEX_*`, `HOME`, or any other ambient value. The child environment
is exactly the fixed locale, timezone, default system PATH, and
`LD_LIBRARY_PATH=GGML_BACKEND_DIR=./runtime/lib`; the child working directory is
the private acquisition root. The fixed pilot CLI accepts one explicit absolute
`--runtime-lib-dir` and rejects anything other than the policy-pinned set of 11
regular files. Their ordered path/size/SHA-256 manifest has integrity
`sha256:5d847919dbfb4b866546104cfbc73f5ffa9ff45ee9d8bc85889bf1de6c299f2d`.

Before any run, the producer copies the exact 106248768-byte product executable
(SHA-256 `a476e8fa96855158f881cecbac75b3cce8fbd57b0c5dd338065e8a89a7eeee11`)
and all 11 dependency byte snapshots into a private, read-only `runtime/`
subtree. Every child executes `./runtime/hazkey-server`; it never reopens the
caller-supplied executable path. The source path is retained only as audit
metadata. The complete snapshot is verified before the first run and after the
last run, retained with the acquisition evidence, and re-hashed by the gate.

Acquire the exact eight-run sequence with the policy-pinned producer. It writes
an owner-only directory without overwriting an existing result and publishes
`acquisition-manifest.json` only after every raw and stderr file is durable.
Final publication uses Linux `renameat2(RENAME_NOREPLACE)`, so even a destination
created after the preflight check cannot be replaced:

```bash
python3 tools/dictionary/run_mozc_b0_measurement.py \
  --executable /absolute/path/to/hazkey-server \
  --runtime-lib-dir /absolute/path/to/build-grimodex/bin \
  --corpus /private/path/to/pilot-256.tsv \
  --source-ref 6e0354f2514edf1fe8219657ed23e7a02c8a7f7a \
  --hazkey-dictionary /absolute/path/to/hazkey-dictionary \
  --mozc-bundle /absolute/path/to/mozc-runtime-generation \
  --output-dir /private/path/to/b0-acquisition
```

The pilot evidence uses `acquisition_manifest: {path, sha256}` instead of a
caller-asserted run-order list. The gate re-hashes the manifest, private
executable snapshot, all 11 policy-pinned runtime dependency snapshots, all raw
outputs, and stderr logs. It checks exact snapshot modes and file set,
chronology, backend order, host/affinity identity, producer SHA, argv, fixed
cwd/environment semantics, and the one-to-one raw run mapping before scoring.
Raw and stderr files must use the producer's exact self-contained names. The
gate also reloads every raw run and rejects any drift in the measurement values.
Hazkey PSS observations must have parent readings only; Mozc observations must
have complete parent and helper readings.

The stability contract consists of exactly these five suite IDs:

| Suite | Native schema and frozen minimum |
|---|---|
| `adapter-soak-150k` | `hazkey.mozc-b0-adapter-soak-result.v1` wrapping exact B0 ABProbe v3, 150,000 conversions, one audited server/helper launch, no recovery or residue |
| `protocol-v2-steady-1500` | `hazkey.mozc-b0-protocol-v2-steady-result.v3` wrapping `hazkey.protocol-v2-backend-benchmark.v1`, exact baseline dictionary and private Swift-package identities, 1,500 conversions per backend, two audited server launches (Hazkey and Mozc), one helper, no recovery or residue |
| `protocol-v2-recovery` | `hazkey.mozc-b0-protocol-v2-recovery-result.v5`; exact fault-fixture source, private Swift package, fresh scratch, and private runtime-library identities plus four fixture-backed checks for EOF, partial-frame EOF, timeout, and external SIGKILL; all named checks and cleanup must pass |
| `fcitx-long-soak-150k` | `hazkey.fcitx-full-stack-result.v1`, one continuous 150,000-conversion cycle, one server/helper launch, no recovery or residue |
| `fcitx-lifecycle-3x100` | the same native Fcitx schema, three fresh 100-conversion cycles, exactly three server/helper launches, no recovery or residue |

The recovery suite is deliberately identified as `fault-fixture`; it must not
claim the B0 artifact fingerprint. The other four suites bind the exact B0
fingerprint and product source revision. Every command in the policy is a
deterministic repo-relative token list with explicit placeholders for private
runtime paths.

`tools/dictionary/run_mozc_b0_stability.py` emits schema
`hazkey.mozc-b0-stability-record.v2`. A record has no `passed` field and no
generic aggregate observations. It binds one native result by path and
SHA-256. The pilot gate re-hashes that file and re-derives counts from its
suite-specific native schema. It rejects schema substitution, aggregate-count
forgery, producer drift, missing or renamed recovery subchecks, and B0 versus
fault-fixture identity substitution.

The adapter and Protocol v2 steady runners execute in a new Linux session and
retain a native `process_audit`; server and helper counts are not filled in from
generic caller assertions. The observer follows the complete session, including
Swift children that create a separate process group. Every observed process is
identified by PID plus Linux start-time ticks. Server classification requires
the running `/proc/<pid>/exe` inode to be the policy-pinned private server
snapshot. Helper classification reads the running executable through
`/proc/<pid>/exe` and
requires the B0 helper's exact size and SHA-256. Each process identity records
that executable identity. The gate cross-checks the Protocol report's server
and helper PIDs against the audit, cross-checks ABProbe's native helper launch
diagnostics, and requires that no process remained for process-group cleanup.
Server and helper PID/start-time identities must be disjoint; a process cannot
change roles merely by claiming a different executable hash.
It separately requires clean termination of the original process group and the
complete same-session process set. PID reuse, basename-only classification, a
same-session child in another process group, and an inferred zero residue
therefore cannot satisfy either wrapper schema. This scope relies on the fixed
Swift runner, product server, and B0 helper not calling `setsid(2)`; it does not
claim containment of an adversarial child that creates a new session.

The Protocol steady runner verifies the baseline dictionary fingerprint before
and after execution, records both observations, and requires the native report
to name the same dictionary and fingerprint. Its validator also fixes five
warmups, 100 measured iterations, the ordered 15-case sentinel corpus, all
latency samples and derived summaries, endpoint RSS/PSS snapshots, execution
metadata, and derived comparison values for both backends. Every case must
carry the exact ID/category and at least one concrete candidate, so null metric
objects or an array of empty candidates cannot manufacture acceptance.

The Protocol steady and recovery runners copy the policy-pinned Swift package
to a read-only private snapshot and execute the copied
`scripts/swift-test.sh`. The package fingerprint covers path, content, and
mode for `Package.swift`, `Package.resolved`, the generated
`Sources/hazkey-server/constants.swift`, all selected Sources and Tests,
`prepare_azookey_dependency.cmake`, the AzooKey patches, and the runner. The
self-referential `Fixtures/mozc-adoption-v1` and `Fixtures/mozc-adoption-v2`
policy, documentation, and evaluation-corpus directories, which the selected
Swift filters do not consume, are excluded by exact path prefix. Files added
under either evaluation-only prefix therefore do not change the executable
Swift-package identity. Both runners create a
new private `swift-scratch` below the new output directory and reject an
existing output directory; caller-selected incremental build products are
never used. The native validator re-walks the read-only snapshot with
no-follow descriptors and compares its file count, total size, and fingerprint
to policy. Cleanup runs
from a `finally` boundary even if process communication raises an unexpected
exception. Evidence and native-result bindings are opened component by
component with no-follow directory file descriptors; an ancestor swap is not
reopened through a pathname after validation.

The acquisition host is part of the trusted computing base: this contract does
not attest the kernel, root, or the system Bash, Swift, CMake, and Git binaries
selected by the fixed system `PATH`. SwiftPM dependencies are resolved only in
the fresh scratch from the snapshotted `Package.resolved`; the repository CMake
preparation additionally checks the AzooKey revision before applying the
snapshotted patches. The contract binds the inputs and outputs used by these
suites, but does not claim a reproducible proof of the system toolchain or
remote package-host infrastructure.

The Fcitx validators rederive their command, complete input-snapshot
fingerprint, artifact/runtime bindings, and PID/start-time/executable
identities. For frozen pilot output, the producer atomically reserves a
content-addressed evidence root beside the native JSON before execution, runs
the server/helper from that root, removes all ephemeral cycle/verifier state,
and retains only the exact input snapshot and prepared Mozc runtime. The root
is owner-only and read-only when the JSON is published. Re-evaluation opens it
component by component without following symlinks, requires the exact
top-level/directory/file sets and modes, and re-hashes every retained server,
helper, data, runtime-library, configuration, and bundle byte. Missing, extra,
modified, or symlink-substituted retained evidence therefore fails closed.
Both Fcitx suites additionally freeze the same complete 3,428-entry,
11-directory input closure in policy as
`sha256:bb4f63a09a16fd0cb00bc41ee6091dca7e3fa85c118ebae688cd7ada6bd99573`.
The gate compares this policy value to the native manifest before accepting
the manifest's independently rederived fingerprint, so replacing an otherwise
unlisted harness, addon, configuration, test addon, or verifier and merely
rewriting the manifest cannot satisfy the frozen pilot contract.
Historical re-evaluation requires the reported producer path to be the exact
absolute lexical child `source.repository_root/<fixed producer path>`, and
checks its positive reported size and policy-pinned SHA-256, but never reopens
the reported acquisition checkout. The repository root is used only for that
lexical relation; `source.git_head` and `source.worktree_clean` remain typed
audit metadata. The retained evidence root is therefore sufficient even when
the acquisition checkout and its Python interpreter no longer exist. Protocol
v2 likewise
re-hashes its sentinel corpus from the retained Swift-package snapshot rather
than from the evaluator's current tree. Process and session identities remain
unique across cycles.

New Fcitx collection has a separate fail-closed preflight. It selects the
collector's trusted current repository root, never a path supplied by evidence,
and verifies the current producer size/SHA-256, exact reported producer path,
reported Git HEAD against the actual trusted-checkout HEAD, and both reported
and actual clean-worktree state before publishing a record. The Fcitx tooling
HEAD is distinct from `product_source_ref`: the latter identifies the frozen
product server and continues to be checked independently in the result and
command provenance. Keep native and retained output outside the trusted
checkout so producing that evidence does not itself dirty the checkout.

Acquire the adapter soak from policy-pinned private snapshots:

```bash
python3 tools/dictionary/run_mozc_b0_stability.py run-adapter \
  --server /absolute/path/to/hazkey-server \
  --runtime-lib-dir /absolute/path/to/build-grimodex/bin \
  --mozc-generation /absolute/path/to/sha256-b0-generation \
  --output-directory /private/path/to/adapter-soak-150k
```

Acquire the Protocol v2 steady suite; the runner creates its own fresh private
Swift scratch directory:

```bash
python3 tools/dictionary/run_mozc_b0_stability.py run-protocol-steady \
  --server /absolute/path/to/hazkey-server \
  --runtime-lib-dir /absolute/path/to/build-grimodex/bin \
  --mozc-generation /absolute/path/to/sha256-b0-generation \
  --dictionary /absolute/path/to/hazkey-dictionary \
  --output-directory /private/path/to/protocol-v2-steady-1500
```

After a native result is produced, place the record beside it and collect it
with the frozen policy:

The frozen Fcitx closure intentionally uses the B0 verifier from commit
`2e326f0`, not a potentially newer working-tree verifier. Materialize it from a
clean detached worktree and verify its exact identity before acquisition:

```bash
git worktree add --detach /private/path/to/hazkey-b0-verifier 2e326f0
B0_VERIFIER=/private/path/to/hazkey-b0-verifier/packaging/scripts/verify_mozc_artifact_bundle.py
test "$(stat -c %s "$B0_VERIFIER")" = 49759
test "$(sha256sum "$B0_VERIFIER" | cut -d' ' -f1)" = \
  7b517e294ed306eafc84cc48290dc1e7eea7cb5c37d9b0fa46004570f9657850
```

Pass that absolute `B0_VERIFIER` path to both native Fcitx runs. The product
server must likewise be the policy-pinned 106,248,768-byte executable with
SHA-256
`a476e8fa96855158f881cecbac75b3cce8fbd57b0c5dd338065e8a89a7eeee11`.
Use `--cycles 1 --soak-iterations 150000` for `fcitx-long-soak-150k`, and
`--cycles 3 --soak-iterations 100` for `fcitx-lifecycle-3x100`. The published
native JSON must report the frozen `bb4f...` input-snapshot fingerprint; a
different value is an input-closure mismatch, not a value to copy into policy.

```bash
python3 tools/dictionary/run_mozc_b0_stability.py collect \
  --suite-id fcitx-lifecycle-3x100 \
  --native-result /private/path/to/stability/fcitx-lifecycle.json \
  --output /private/path/to/stability/fcitx-lifecycle-record.json \
  --policy hazkey-server/Tests/grimodex-spike/Fixtures/mozc-adoption-v1/b0-policy.json
```

The four recovery checks can be acquired without claiming a B0 helper:

```bash
python3 tools/dictionary/run_mozc_b0_stability.py run-recovery \
  --server /absolute/path/to/hazkey-server \
  --runtime-lib-dir /absolute/path/to/build-grimodex/bin \
  --output-directory /private/path/to/protocol-v2-recovery
```

The recovery runner first checks the product server, fault-fixture source,
Swift test runner, and exact runtime-library file set against the selected B0
policy. It snapshots the verified server and runtime libraries, points both the
dynamic loader and GGML backend discovery only at that private snapshot, and
executes the four exact filters separately. It retains hashed
stdout/stderr, rejects skipped or unnamed tests, audits and cleans both each
private process group and its full same-session process set even on
timeout/error, re-hashes every runtime and Swift-package snapshot file, and
re-derives an exact named pass from each bound log before validating the native
v5 result and publishing a record or
returning success. A generic zero-count JSON cannot substitute for any suite.

Once all native stability evidence exists, invoke the pilot evaluator with:

```bash
python3 tools/dictionary/evaluate_mozc_b0_gate.py \
  --policy hazkey-server/Tests/grimodex-spike/Fixtures/mozc-adoption-v1/b0-policy.json \
  --evidence /private/path/to/evidence.json \
  --output /private/path/to/b0-gate-result.json
```

The result fixes `decision_tier` to `pilot` and
`formal_adoption_allowed` to `false`. Its only aggregate classification is
`pilot_result`, whose values are `pilot_pass`, `pilot_fail`, or
`inconclusive`. A failed required check produces `pilot_fail`; complete success
produces `pilot_pass`; an evaluation without either conclusion is
`inconclusive`. None of these values is an adoption authorization or rejection,
and the result deliberately has no top-level generic `passed` or `adopt` flag.

Evaluation tooling must preserve the separation between
`scripts/grimodex-ime.sh` and `scripts/grimodex-ime_mozc.sh`.
