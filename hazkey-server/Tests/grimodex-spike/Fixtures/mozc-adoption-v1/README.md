# Mozc adoption evaluation contract v1

This directory locks the corpus shape and pass/fail thresholds for the first
formal evaluation of the fixed B0 Mozc artifact. The pinned AJIMEE component is
kept separate from the reviewed product-owned 140 cases and protected 16 cases.
Their deterministic aggregate manifest and the ABProbe measurement contract are
frozen. The exact long-running stability check contracts and native evidence
producers remain pending. No formal adoption decision may be produced while
`readiness.formal_decision_enabled` is `false`.

All policy rates use integer basis points. A score delta of `-800` means minus
8 percentage points; a ratio of `5000` means 50% of the Hazkey value. The human
net preference is `(B0 wins - B0 losses) / 256`: it must be at least -300 basis
points and at least -7 cases.

## Formal 256-case suite

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
suite outside the formal 256-case score.

## Excluded and auxiliary data

- The existing 15-case
  `../ime-base-ab-v1/conversion-quality-v1.tsv` remains a fast sentinel
  regression suite. It is not part of the formal score and must not be repeated
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
  data excludes its examples from the formal evaluation corpus.

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
   rows in the formal snapshot.

The component SHA-256 values are `91068dd9…e265fba`,
`32a72027…8a0e4`, and `18c384f5…3d768` in manifest order. The exact generated
256-case snapshot is
`sha256:123f47cb6f747135451e5969b32d9868ec61d9574fa6eb4b0001e5409287c807`;
the manifest file itself is
`sha256:b1319e356ba025e1e06221330479d48b12cc44ebb502ddda970ea5fa583336e3`.

```bash
python3 tools/dictionary/build_frozen_corpus.py build \
  --manifest hazkey-server/Tests/grimodex-spike/Fixtures/mozc-adoption-v1/manifest.json \
  --output /private/path/to/formal-256.tsv
```

The formal evidence must bind that manifest hash, the hashed acquisition
manifest for all eight raw ABProbe v3 runs, the blind-review packet, and the
exact product executable, its 11 runtime dependencies, and the B0 helper/data
identities in `b0-policy.json`.
It must also use the frozen product implementation revision
`6e0354f2514edf1fe8219657ed23e7a02c8a7f7a`; a caller-selected lookalike source
revision is rejected.
Do not reuse results from B0 lookalikes or locally rebuilt artifacts. B1 is made
only if this B0 fails; B2 remains a future option.

## Locked gates and measurement contracts

The fixed gates are: human net preference at least -3% (net loss no worse than
7 cases), overall Top-1 no more than 8 points below Hazkey, overall Top-10 no
more than 12 points below Hazkey, every counted category Top-1 no more than 10
points below Hazkey, protected 16/16, at most 12 `both_bad` judgments, warm
latency p95 no more than 50% of Hazkey, PSS no more than 125% of Hazkey, and all
required long-running stability checks passing.

The formal performance comparison uses ABProbe v3 with exactly four runs per
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
the private acquisition root. The formal CLI accepts one explicit absolute
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
  --corpus /private/path/to/formal-256.tsv \
  --source-ref 6e0354f2514edf1fe8219657ed23e7a02c8a7f7a \
  --hazkey-dictionary /absolute/path/to/hazkey-dictionary \
  --mozc-bundle /absolute/path/to/mozc-runtime-generation \
  --output-dir /private/path/to/b0-acquisition
```

The final evidence uses `acquisition_manifest: {path, sha256}` instead of a
caller-asserted run-order list. The gate re-hashes the manifest, private
executable snapshot, all 11 policy-pinned runtime dependency snapshots, all raw
outputs, and stderr logs. It checks exact snapshot modes and file set,
chronology, backend order, host/affinity identity, producer SHA, argv, fixed
cwd/environment semantics, and the one-to-one raw run mapping before scoring.
Raw and stderr files must use the producer's exact self-contained names. The
gate also reloads every raw run and rejects any drift in the measurement values.
Hazkey PSS observations must have parent readings only; Mozc observations must
have complete parent and helper readings.

Long-running stability check contracts are not yet frozen. A ready policy must
carry a non-empty `checks` array. Each check freezes its protocol, exact argv,
minimum conversions and cycles, and exact helper/server launch, recovery, and
residue counts. Evidence records do not contain a trusted `passed` flag: they
bind a separate raw structured result by path and SHA-256, and the gate derives
pass/fail from exit code and the frozen observations. Until those contracts and
their native evidence producers are ready, no gate runner may infer them to
make the policy appear ready.

Once the stability contracts are frozen and their evidence exists, invoke the
final evaluator with:

```bash
python3 tools/dictionary/evaluate_mozc_b0_gate.py \
  --policy hazkey-server/Tests/grimodex-spike/Fixtures/mozc-adoption-v1/b0-policy.json \
  --evidence /private/path/to/evidence.json \
  --output /private/path/to/b0-gate-result.json
```

Evaluation tooling must preserve the separation between
`scripts/grimodex-ime.sh` and `scripts/grimodex-ime_mozc.sh`.
