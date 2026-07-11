# Grimodex IME Phase 3.1 Linux dynamic-dictionary spike

This branch keeps the spike disabled by default. It does not change normal Hazkey conversion until
the developer opts in with an environment variable.

## Fixed vocabulary

Start the server with:

```bash
GRIMODEX_IME_DICTIONARY_SPIKE=1 hazkey-server --replace
```

The server replaces only the AzooKey dynamic-user-dictionary layer with these entries:

| yomi | surface | category | priority | CID | MID | value |
|---|---|---|---:|---:|---:|---:|
| せつな | 刹那 | person | 2 | 1289 (人名一般) | 501 | -5 |
| りゅうせいこう | 龍星港 | place | 1 | 1293 (地名一般) | 501 | -9 |

The normal compiled user dictionary remains on its existing path. This spike calls
`importDynamicUserDictionary` only and never rebuilds or deletes the normal dictionary.

## Benchmark harness

Run each required scale separately so RSS and latency are attributable to one corpus:

```bash
for count in 100 500 2000 5000 10000; do
  GRIMODEX_IME_DICTIONARY_BENCHMARK_COUNT="$count" hazkey-server --replace
done
```

Accepted values are exactly `100`, `500`, `2000`, `5000`, and `10000`; every other value is
fail-closed and leaves the dynamic dictionary unchanged. The log reports:

- `import_ms`: replacement time for the generated dynamic dictionary
- `warm_p95_ms`: P95 of 50 warmed candidate requests for a present entry
- `rss_kib`: Linux `/proc/self/status` resident memory after import

The Phase 3 target is warm P95 below 5 ms excluding Zenzai inference. Cost values in this spike are
the initial baseline from Grimodex's `ime-contract/expected/mapped-entries.json`; they are not final
until the candidate-quality and performance results are recorded.

## Verification

```bash
swift test --package-path hazkey-server --filter GrimodexDictionarySpikeTests
```

The Grimodex contract is pinned to Hazkey upstream `23c78b1a35f828288061145c6dfc73dc53916667`
and `7ka-hiira/AzooKeyKanaKanjiConverter@8b4befc273baafea5964ecf87d3bc36f2bbef68b`.
