# Mozc adoption formal corpus v2

This directory contains the reviewed source inputs and immutable generation for
a new 1,360-case holdout. The seven category TSVs are complete, their exact
bytes and reviewed family assignments are approved in `review-approvals.json`,
and `corpus-policy.json` is `ready`. The sealer validates all source inputs,
generates provenance and the combined corpus, then publishes the whole result
as one content-addressed `sealed-v2-sha256-*` directory without replacing an
existing generation.

Sealed identity:

- generation: `sealed-v2-sha256-b4c1351b1b0ef7797349ebf26858db4d0dd69ce1c8bcbfaee88e0f0b644225ed`
- aggregate SHA-256: `sha256:cdb2a017b4548f6f77ec3d466f84ec09268a74adb5e876e224e01069f128c8ae`
- manifest SHA-256: `sha256:3ccefa5552d1c0d851b07cc1ed8f65983dd7db019d9250509f2467af7bfd1c02`
- sealed files: 19

The existing 256-case v1 corpus is a pilot and development replay. None of its
cases count toward v2. AJIMEE unconditional, the 15-case sentinel, Mozc stress
data, and every contextual suite are also auxiliary and excluded from the
formal 1,360-case aggregate.

## Counted components

Each category has its own source TSV. The source TSVs plus
`review-approvals.json` are the reviewed inputs; the matching provenance JSONL,
manifest, near-duplicate review, and combined TSV are deterministic sealed
outputs. This keeps ownership, review, and diffs category-local while making
the evaluation generation self-contained.

| Category | Cases | Quality score |
|---|---:|:---:|
| `technical-mixed` | 240 | yes |
| `proper-noun` | 200 | yes |
| `colloquial` | 200 | yes |
| `homophone-context` | 200 | yes |
| `long-structural` | 200 | yes |
| `grimodex-regression` | 220 | yes |
| `protected` | 100 | no; legacy probe is not formal interaction evidence |

Overall Top-1, Top-10, human preference, and `both_bad` rates use only the six
quality categories: 1,260 cases. The 100 protected cases cannot improve or
dilute those rates. Their old 100/100 requirement is superseded by the product
interaction suites below.

## Mixed-input protocol correction

The sealed rows and hashes above remain unchanged, but their interaction model
is not sufficient for a formal decision. `ABProbe` sends every character in a
row as `.direct`, with empty left and right context, in one composition. For a
mixed row such as `RUST_LOG=debugでくわしいろぐをだす`, that is closer to
whole-selection reconversion than ordinary PC romaji input.

Ordinary input normally commits half-width ASCII in alphanumeric mode, returns
to Japanese mode, and sends only the kana composition to the converter. The
committed ASCII is left context. Whole mixed strings may instead reach the
product through selected-text reconversion. F9 and F10 are separate full-width
and half-width alphabet transforms; they are not candidate-ranking cases.

The formal interaction contract therefore requires three product-path suites:

- `normal-input-context`: committed half-width ASCII as left context and only
  the kana portion as the active composition.
- `reconversion`: the selected mixed string plus explicit left/right context
  and replacement range through Protocol v2.
- `alphabet-width-f9-f10`: separate F9/F10 transform and commit checks through
  the Fcitx key path.

Case counts for those suites are `pending_review`; assigning the existing 100
rows among them without reviewed action metadata would be arbitrary. The old
protected Top-1 observation is `invalid_for_formal_gate`, not a pass or a fail.

The mismatch is broader than the protected category. A deterministic scan of
the sealed readings finds ASCII in 565/1,360 rows: technical 239, proper noun
4, colloquial 2, homophone 0, long 0, Grimodex regression 220, and protected
100. Of those, 431 have one mechanically separable left literal plus terminal
kana; the remaining 134 require multiple literal/convert actions or explicit
reconversion. All 565 still need reviewed scenario kind, action trace,
left/right context, composition reading, input style, requested transform, and
expected target before they can count as normal-input evidence.

An ASCII-free diagnostic slice has 795 quality rows. On that slice H0/B0
Top-1 is 441/441 and Top-10 is 601/451 (delta -150; the recalculated -12-point
boundary is -95), while B1 is 444/607 (deltas +3/+6). These figures are
diagnostic only: technical has one ASCII-free row and Grimodex regression has
none, so the per-category gates cannot be evaluated. B0/B1 remain
`not_ready:interaction-model-mismatch`; neither the old authorization nor its
B1 continuation is formal decision evidence. A future authorization requires
a new policy revision with reviewed interaction check IDs; changing a status
field to `ready` is insufficient.

The read-only classifier reproduces the scope and migration split:

```bash
python3 tools/dictionary/audit_mozc_v2_interaction_model.py \
  --corpus hazkey-server/Tests/grimodex-spike/Fixtures/mozc-adoption-v2/\
sealed-v2-sha256-b4c1351b1b0ef7797349ebf26858db4d0dd69ce1c8bcbfaee88e0f0b644225ed/\
formal-corpus.tsv
```

Its output is a classification report only and never authorizes formal
scoring, B1 evaluation, or adoption.

The draft sidecar preparer expands that classification into review input:

```bash
python3 tools/dictionary/prepare_mozc_v2_interaction_sidecar.py \
  --corpus hazkey-server/Tests/grimodex-spike/Fixtures/mozc-adoption-v2/\
sealed-v2-sha256-b4c1351b1b0ef7797349ebf26858db4d0dd69ce1c8bcbfaee88e0f0b644225ed/\
formal-corpus.tsv
```

It proposes `update_context` plus a kana-only `conversion_boundary` for the 431
mechanically separable rows and leaves the other 134 rows explicitly
unclassified. `update_context` seeds text that is already committed at the
conversion boundary; it does not claim to reconstruct how that prefix was
typed or converted. The draft deliberately records the physical key trace as
missing and the input style as `unknown_pending_review`; it cannot be consumed
by a formal product-path runner. The canonical output remains `not_ready` and
`formal_authorized=false` until a separately reviewed sidecar and product-path
runner are frozen.

`homophone-context` may contain context rendered inside the reading itself. A
case that requires an external left context is not made unconditional by
deleting that context; it belongs in the separate Protocol v2/product-path
contextual suite.

## One-shot holdout and artifact freeze

The holdout is eligible only for the exact pre-disclosure evaluation runner,
B0 and B1 generations, helper/data bytes, and artifact manifests pinned in the
policy. Every case provenance record must say that it remained sealed until
both eligible artifact identities were frozen. After the v2 corpus is published
or disclosed, a newly developed B2 is ineligible for this holdout and requires
a new holdout revision. Changing an artifact hash or relabelling a B2 artifact
as B0/B1 does not restore eligibility.

The sealed manifest binds the exact ready policy and review-approval bytes.
Publication is transactional: all files first enter a private staging
directory, are fsynced and made read-only, and are then renamed as one
content-addressed generation with no-replace semantics. A failed write or
destination conflict leaves no partial generation, and rerunning the same seal
is rejected.

To reproduce the seal from the reviewed source inputs:

```bash
python3 tools/dictionary/seal_frozen_corpus_v2.py \
  --policy hazkey-server/Tests/grimodex-spike/Fixtures/mozc-adoption-v2/corpus-policy.json \
  --approvals hazkey-server/Tests/grimodex-spike/Fixtures/mozc-adoption-v2/review-approvals.json \
  --pilot-v1-manifest hazkey-server/Tests/grimodex-spike/Fixtures/mozc-adoption-v1/manifest.json
```

The command refuses to replace the already sealed generation. Reproduction
therefore uses an empty copy of the source-input directory and must produce the
same generation name and bytes.

## Provenance and contamination contract

Each provenance JSONL record has this exact shape:

```json
{
  "schema": "hazkey.frozen-conversion-case-provenance.v2",
  "case_id": "v2-technical-...",
  "family_id": "family-...",
  "source": {
    "kind": "project-authored",
    "source_id": "rights-reviewed-source-id",
    "author_id": "canonical-author-id",
    "locator_sha256": "sha256:...",
    "license": "MIT",
    "new_holdout": true
  },
  "rights": {
    "redistribution_approved": true,
    "privacy_reviewed": true,
    "reviewer_id": "reviewer-id"
  },
  "exposure": {
    "status": "sealed-for-b0-b1",
    "eligible_candidate_ids": ["B0", "B1"],
    "disclosed_before_candidate_freezes": false
  },
  "contamination": {
    "status": "no-known-overlap",
    "screened_against": [
      "pilot-v1",
      "ajimee-bench",
      "sentinel-v1",
      "mozc-stress",
      "microsoft-ime-corpus",
      "zenz-v2.5-dataset"
    ]
  }
}
```

`locator_sha256` is not a caller-selected source label. It is SHA-256 over the
builder's canonical sorted-key JSON object containing the exact TSV `id`,
`reading`, `expected`, and `category` plus `family_id`, under contract
`canonical-case-and-family-json.v1`. Each component's IDs are exactly its
policy prefix followed by four decimal digits from `0001` through the component
count, in that order. Gaps, reordering, or a locator copied from another case
fail closed.

AJIMEE remains separated under CC BY-SA 3.0 in the v1 pilot. Microsoft Research
IME Corpus remains uncollected because its license is non-commercial and
non-redistributable. The zenz-v2.5 dataset may inform vocabulary distribution
but contributes no formal row. Stress data is robustness-only. Project data
must have affirmative redistribution and privacy review; an unknown license,
unknown origin, known training overlap, or pre-freeze disclosure fails closed.

## Duplicate and derived-case controls

- Case IDs follow the exact per-component sequence; normalized readings and
  `family_id` values are globally unique.
- A quality-scored case may not list its unchanged reading as an accepted
  surface. This prevents conversion-free rows from inflating Top-1 or Top-10.
- Multiple accepted surfaces are alternatives in one TSV row, never extra
  cases.
- Kana, punctuation, whitespace, numeric, inflection, paraphrase, or template
  variants share one family; at most one member may remain in the aggregate.
- The builder reconstructs the pinned v1 pilot and rejects any exact normalized
  reading or case fingerprint overlap.
- It deterministically compares normalized readings by character 3-gram
  Jaccard similarity and normalized Levenshtein similarity. Every pair with
  Jaccard at or above 8,000 basis points **or** Levenshtein similarity at or
  above 9,000 basis points, including v1-to-v2 pairs, must have exactly one
  closed review record with a reviewer and rationale. Missing,
  stale, duplicated, or invented review pairs are rejected.

The manifest schema is `hazkey.frozen-conversion-corpus-manifest.v2`. A
generated combined TSV is an output only; the seven category TSVs and their
exact review approvals remain the authoritative reviewed inputs.

The normal runner `scripts/grimodex-ime.sh` and the Mozc-only runner
`scripts/grimodex-ime_mozc.sh` remain separate.
