# Conversion quality fixtures

`conversion-quality-v1.tsv` is a small, reviewable corpus for syntax guards,
protected ASCII surfaces, and clause-boundary baselines. The evaluator accepts
JSONL emitted by a converter probe:

```sh
python3 tools/dictionary/evaluate_conversion_quality.py \
  --corpus hazkey-server/Tests/grimodex-spike/Fixtures/quality-v1/conversion-quality-v1.tsv \
  --results /path/to/candidates.jsonl \
  --output /tmp/conversion-quality.json
```

`context-boundary-v1.tsv` is an evaluation spike rather than a release gate.
It compares one-shot and split-context top-1 results with
`tools/dictionary/evaluate_context_boundaries.py`. Both scripts have a
dependency-free `--self-test` mode for CI and packaging checks.
