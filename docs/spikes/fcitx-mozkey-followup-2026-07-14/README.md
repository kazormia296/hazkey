# fcitx-mozkey A/B follow-up spike

実施日: 2026-07-14

## 結論

初回spikeの結論を次のように更新する。

- **Hazkeyをdefaultのまま維持する。** full rebaseはまだ行わない。
- **Mozc core sidecarは実装候補として採用できる。** core APIの5操作は実動した。
  `SessionHandler`ではなく
  `ConverterInterface`を既存Protocol v2 / `ImeReducer`の背後に置く最小sliceを、
  feature flag付きで進める価値がある。ただし9 scenario全体は未実行である。
- clean Mozc OSSの小規模品質とprocess性能はBが優位だった。ただしdaily辞書、
  product dictionary overlay、secure/failure契約をまだ満たしていないため、default切替の
  根拠にはしない。
- fcitx-mozkeyのfull daily辞書は固定source SHAだけでは再現できない。B1のoffline
  syntax guardは再現できたが、生成辞書を組み込んだ品質測定は未実施である。

初回結果は[前段spike](../fcitx-mozkey-ab-2026-07-14/README.md)を参照する。

## 追加結果の要約

| 観点 | A: Hazkey | B: fixed-SHA Mozc OSS | 判定 |
|---|---:|---:|---|
| Top-1 exact match、15件 | 10/15 (66.7%) | 12/15 (80.0%) | Bが2件優位。corpusが小さいため仮説扱い |
| fresh process wall、1,500変換、5回mean | 5,236.6 ms | 830.4 ms | 同一process境界ではratio-of-means=6.31 |
| 同条件median | 5,228.2 ms | 838.5 ms | 交互実行5 cycleで傾向は安定 |
| 同条件max (n=5) | 5,292.6 ms | 858.6 ms | Bが優位。tail推定値ではない |
| child process max RSS | 51,352 KiB | 30,448 KiB | A/B=1.69 |
| 既存Protocol v2との接続 | native | core smoke成功 | 9 scenarioは責務mapping上の仮説、未検証 |
| 辞書再現性 | repo固定 | clean OSSのみ固定 | full dailyはlock前提 |

## 1. 同一process境界の交互benchmark

### 条件

- AMD Ryzen 5 3600、Linux 7.1.3、x86_64
- 15 readingsを100回ずつ処理し、各backend 1 processあたり1,500変換
- Aはcommit `fc11156bfac57850c7edc7e5b197b50f4cd9970d` のAB probe、
  `--warmups 0 --iterations 100 --top-k 1`
- Bはfixed SHA `462cbbf04886e32096bc318833e974ccc43d9fc8` の
  `quality_regression_main`とclean checkoutのMozc OSS `mozc.data`
- 両者を`taskset -c 8-11`で同じCPU集合へ固定
- odd cycleはA→B、even cycleはB→Aとして5 cycle実行
- 両者ともstdin/stdout/stderrをdiscardし、fresh child processの開始から終了までを
  `time.monotonic_ns`で測定
- RSSは累積`RUSAGE_CHILDREN`ではなく、各PIDを`os.wait4(pid, WNOHANG)`で監視した
  `ru_maxrss`を使用
- 各childに30秒timeoutを設定し、超過時はkill/reapしてsummaryを出さない

主要入力のSHA-256は次の通り。

- A executable: `e07d297c17561c0dd4b5c767042c5d99328084591691e2f328adf484c7ceb819`
- corpus: `e5c61cc92042c24ff334f702c7bd3e01473e37002c9d64d6c652462721520e9e`
- B executable: `b8ea5fdeb5a566ad0c55d037a2d2257af6943cef3abb0d2b64407dd02e75d36c`
- B `mozc.data`: `b9884362e37772f772a0d28d1e12622455c14353497b3435deed60aa7e592c5e`
- B repeated input: `0060f50bca6cfffd909ae1a74804d0c0a45f9e24b6b4b004efc13030ceacb149`

### raw値

| cycle | 実行順 | A wall (ms) | B wall (ms) | A/B |
|---:|:---:|---:|---:|---:|
| 1 | AB | 5,199.5 | 838.5 | 6.201 |
| 2 | BA | 5,275.1 | 798.1 | 6.609 |
| 3 | AB | 5,292.6 | 858.6 | 6.165 |
| 4 | BA | 5,187.7 | 817.5 | 6.346 |
| 5 | AB | 5,228.2 | 839.4 | 6.229 |

A/Bのpaired meanは6.310、medianは6.229、ratio-of-meansは6.306だった。n=5の
nearest-rank p95は必ずmaximumになるためtail指標としては採用せず、表ではmaxとした。
個別実行では
system loadの影響を受けたため、判定にはこの交互実行値だけを使う。

この結果は**同じtop-level process測定境界**での比較であり、内部harnessの仕事まで
同一ではない。起動、辞書load、入力parse、結果検証/serializationを含む一方、Fcitx、
外部Protocol v2 IPC、rendering、Zenzaiは含まない。また、長寿命Mozc sidecarのwarm
request latencyでもない。したがって「現在のテストworkloadではB processが速い」と
解釈し、実製品のkey latencyが6.31倍になるとは解釈しない。

generic runnerは
[`benchmark_process_backend.py`](../../../tools/dictionary/benchmark_process_backend.py)と
[`benchmark_process_pair.py`](../../../tools/dictionary/benchmark_process_pair.py)に追加した。
strict JSON manifest、argv実行、environment/cwd provenance、raw順序、command/execution
fingerprint、30秒timeout、atomic output、child失敗時fail-closedを備える。実測に使った
[`manifest`](./process-benchmark.manifest.json)と完全な
実測出力は
[`process-benchmark.raw.json`](./process-benchmark.raw.json)に保存した。

## 2. daily辞書の再現性

fixed SHAのclean checkoutには生成済みdaily辞書がない。さらにBazel側はdaily入力を
`glob(..., allow_empty = True)`としているため、clean buildは失敗せずMozc OSS辞書に
退化する。初回B測定は作者のdaily buildではなく、B0のclean Mozc OSSだった。

再現できない外部入力は次の3系統である。

- `merge-ut-dictionaries`: shallow clone / branch pullでcommit未固定。推移的入力lockもない
- nico/pixiv差分: raw GitHub `master` URL、checksumなし
- personal names: raw GitHub `main` URL、checksumなし

これに対し、fixed checkout内だけで作るsyntax guardはofflineで2回生成してbyte一致した。

- 537 entries
- output SHA-256:
  `1d29b99c8b4bff45239e6ae2c5b97e43356fe0740a96c758f1020a32a28007fd`
- debug TSV SHA-256:
  `4b1dcfb111404e71fac322e6f0183dd2e49b868e7b7b413666faf238a6f8fa8b`
- 初回corpusでB0が落とした`はだみはなさ → 肌身離さ`と
  `はだみはなさず → 肌身離さず`の明示entryを含む

ただし、このB1 guardを`mozc.data`へ組み込むfresh third-party Bazel buildは、restricted
sandbox内ではBazel runtimeが必要とするloopbackを利用できず、sandbox外実行は第三者build
logicに広いhost accessを与えるため承認されなかった。このためB1の品質改善は**推測せず
unmeasured**とする。

今後は次の3群を混ぜずに扱う。

- B0: clean Mozc OSS。今回測定済み
- B1: Mozc OSS + offline syntax guard。guard生成済み、組込み後品質は未測定
- B2: full daily。immutable input、checksum、toolchain、output、最終`mozc.data`、noticeを
  lockしたsnapshot入手後に測定

監査の機械可読結果と最小lock要件は
[`daily-dictionary-audit.json`](./daily-dictionary-audit.json)に記録した。B2は再現性だけでなく、
mixed/derived dataのredistribution reviewとpackageへの辞書notice同梱もhard gateとする。

## 3. Mozc core adapter smoke

fixed SHAの一時worktreeで、20 LOCのBazel targetと81 LOCのC++ smokeを作り、
`CreateEvalEngine → StartConversion → ResizeSegment → CommitSegments →
CancelConversion`を実行した。workspaceは変更していない。

```text
segments=3 first_key=きょうは first_value=今日は
resize_plus_one=1
learning=off partial_commit_api=ok cancel=ok
```

`mozc.data`は18,887,468 bytes、SHA-256は
`b9884362e37772f772a0d28d1e12622455c14353497b3435deed60aa7e592c5e`。
smoke helperは5,658,528 bytesだった。binary sizeは参考値であり、runtime RSS比較には使わない。

一時smokeの完全なsourceは
[`mozc-core-adapter-smoke`](./mozc-core-adapter-smoke/)に保存した。fixed SHA checkoutへ
このdirectoryを`src/grimodex_adapter_spike`としてcopyし、次で再実行できる。

```bash
cd "$MOZC_CHECKOUT/src"
"$BAZELISK" --output_user_root="$BAZEL_OUTPUT_ROOT" build \
  //grimodex_adapter_spike:converter_adapter_smoke \
  --config=release_build \
  --config=oss_linux

./bazel-bin/grimodex_adapter_spike/converter_adapter_smoke \
  --data_file=./bazel-bin/data_manager/oss/mozc.data
```

fixed SHAの主要根拠は次の通り。

- `src/converter/converter_interface.h:47-68,123-175`: conversion、partial commit、
  segment resizeの直接API
- `src/engine/eval_engine_factory.cc:43-52`と`src/engine/engine.h:64-84`:
  file-backed engine初期化とconverter取得
- `src/request/options.h:96-111`: conversion history無効化とincognito option
- `src/converter/segments.h:55-112,335-419`、`candidate.h:79-95`:
  request-local segmentsとHazkey candidateへ写像するkey/value/consumed key/description
- `src/session/session_handler.h:57-154`: session LRU、watchdog、engine、keymap、history等の
  state ownership。既存reducerと責務が重なるため採用しない

source/APIの責務mappingでは、外部Protocol v2と`ImeReducer`を唯一の
semantic/session ownerとして維持し、Mozcは
`ConverterInterface`直結のprivate sidecarに限定する。private Swift↔helper wireは
length-prefixed protobufとし、公開Protocol v2は変更しない案が最小である。9 scenarioの
責務仮説は次の通り。

- composing / escape / backspace / partial commit / failure / stale candidateは既存reducer
- cursor / Unicode caret / surface index写像はSwift EGC mapper
- segment editingはMozc native segmentsと`ResizeSegment`
- secure inputはsidecar no-call + purge。readingやcontextをhelperへ送らない
- S0はuser historyをhard-offし、partial commitのvisible exactly-once semanticsだけを
  reducerで保証する

実動したのは上記5 core操作だけである。reducer、EGC mapper、secure no-call、helper
failure、stale candidateとの統合はまだ実行していないため、このmappingを「9 scenario
回復済み」とは扱わない。

ここでいうS0の9 scenario通過は、現行fixtureをそのまま満たすという意味ではない。現行
`partial-commit.json`はlearningを`completed=1, updated=1, committed=1, forgotten=0`と
期待するが、S0 profileは4値すべて0とする。この差は意図的な初期sliceのproduct-contract
gapであり、visible commit semanticsが同じでもfull parityとは数えない。

詳細なboundary、scenario割当、見積りは
[`mozc-core-adapter.json`](./mozc-core-adapter.json)に記録した。最短sliceは8〜10 files、
手書き約750〜1,050 LOCである。

## 更新した判断gate

full rebaseではなく、まず`FCITX5_GRIMODEX_CONVERTER=mozc`配下のsidecar sliceを作る。
default切替を検討するのは次を通過した後とする。

1. dedicated learning-off profileでProtocol v2の9 scenario / 26 visible/session stepsを
   fake coreで通す。ただし、これはfull parityとは数えない
2. actual coreでsegment/resize/qualityを通す
3. secure時helper requestが0であり、purge後にreading/contextが残らない
4. helper kill / EOF後もcompositionを保持し、commit effectを重複させない
5. 現行partial-commit learning期待値`1/1/1/0`を復元するか、廃止する明示的なproduct判断を得る
6. project/personal dictionaryをgeneric candidate overlayとして復元する
7. B1またはlocked B2でblind実利用corpusを100〜500件へ拡大する
8. 同じ長寿命process / IPC境界でwarm latency、steady RSS、failure recoveryを再測定する

ここまで満たす前はHazkeyをdefaultとし、Mozc coreはopt-in比較実装に留める。
