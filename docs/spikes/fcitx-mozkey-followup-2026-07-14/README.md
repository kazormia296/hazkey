# fcitx-mozkey A/B follow-up spike

実施日: 2026-07-14

## 結論

初回spikeの結論を次のように更新する。

- **Hazkeyをdefaultのまま維持する。** full rebaseはまだ行わない。
- **Mozc core sidecarのopt-in sliceは実装できた。**
  `FCITX5_GRIMODEX_CONVERTER=mozc`との完全一致時だけ、`ConverterInterface`を使う
  private sidecarを既存Protocol v2 / `ImeReducer`の背後に選択する。未指定、未知値、
  大小文字違いはHazkeyのままである。
- real Swift adapter + fake Mozc coreでlocked 9/9 scenario、26/26 stepsの互換経路を
  通過した。これとは別に、本番のsegmented reducer経路で自然分節、resize、partial commitを
  統合検証し、fixed B0 actual helperでも自然変換と文節resizeを通過した。公開Protocol v2は
  変更していない。
- clean Mozc OSSの小規模品質とprocess性能はBが優位だった。ただしdaily辞書、
  product dictionary overlay、learning parity、同一の長寿命server/IPC境界での性能比較が
  未完のため、default切替の根拠にはしない。
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
| 既存Protocol v2との接続 | native | real adapter + fake coreで互換9/9 scenario、26/26 steps。本番segmented reducerとactual helperも別途検証 | 公開Protocol v2変更なし。full parityではない |
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

ただし、このB1 guardを`mozc.data`へ組み込むfresh third-party Bazel buildは未完である。
Bazel 9のnetwork usage計測は
`--noexperimental_collect_system_network_usage`で止め、外側のno-network sandboxを維持した
まま747 processまで実行できたが、未キャッシュの郵便番号辞書ZIPを取得できずfail-closed
した。sandbox外で第三者build logicへ広いhost accessを与える実行は承認されなかった。
したがってclean offline bundle生成の完遂とB1の品質改善は**推測せずunmeasured**とする。

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

### 実装済みopt-in slice

外部Protocol v2と`ImeReducer`を唯一のsemantic/session ownerとして維持し、Mozcは
`ConverterInterface`直結のprivate sidecarに限定した。private Swift↔helper wireは
[`protocol/mozc_sidecar.proto`](../../../protocol/mozc_sidecar.proto)のlength-prefixed
protobufであり、公開Protocol v2は変更していない。

- `FCITX5_GRIMODEX_CONVERTER=mozc`との完全一致時だけMozcを選ぶ
- helper/dataは`FCITX5_GRIMODEX_MOZC_HELPER`と`FCITX5_GRIMODEX_MOZC_DATA`で上書き可能
- helper起動直後にreadingなしの`PING`でprotocol versionと固定B0 dataset SHAを検証し、
  成功するまで最初の`CONVERT`を送らない
- requestはprocess-wideで直列化し、失敗requestを再送しない。次の独立requestで再起動する
- helper responseのsegment、candidate consumed size、forced target、候補数をclient内で
  相互検証し、不一致時は同じhelperを再利用しない
- secure inputはcore no-call、context非送信、purge時はpipeを閉じhelperを`SIGKILL`する
- HazkeyとMozcは同じEGC/surface mapperを使い、Mozc segment boundaryとresizeを
  Hazkey input element境界へ戻す
- Mozc profileは`allowsLearning=false`、`zenzaiEnabled=false`を固定し、helper側も
  `enable_user_history_for_conversion=false`、`incognito_mode=true`とする

9 scenarioの責務は次の分割で実装した。

- composing / escape / backspace / partial commit / failure / stale candidateは既存reducer
- cursor / Unicode caret / surface index写像はSwift EGC mapper
- segment editingはMozc native segmentsと`ResizeSegment`
- secure inputはsidecar no-call + purge。readingやcontextをhelperへ送らない
- S0はuser historyをhard-offし、partial commitのvisible exactly-once semanticsだけを
  reducerで保証する

locked 9/9 scenario、26/26 visible/session stepsは、real adapter + fake coreをversioned v1の
互換presentation経路へ接続して通過した。これだけを本番分節経路の証拠とはせず、別の
reducer integrationで`supportsSegmentEditing=true`の自然分節、resize往復、先頭文節の
exactly-once partial commitを通した。

focused `GrimodexMozcSidecarTests`は22 testsで、通常実行は21 pass + actual bundle smoke
1 optional skip、bundle指定実行では22 passした。coverageはexact opt-in、Unicode boundary、
stable Romaji resize、segment resize、secure no-call、候補一覧OFF時のlive candidateのみ表示、
prediction no-op、purge/respawn、active profile writer停止後のprivate `TMPDIR` cleanup、
failure時のcomposition保持、stale guard、
learning/Zenzai diagnostics hard-off、reading送信前dataset handshake、CONVERT受信後EOFの
no-replay、response cross-field mismatch、zero target、timeout/oversize/request mismatchを含む。

strict verifierを通したfixed bundleは次の通り。

- helper: 5,695,048 bytes、SHA-256
  `8676275bb47aefe963c8b82047cc66fb7a5140caec72d1ebbfa17556b281577d`
- `mozc.data`: 18,887,468 bytes、SHA-256
  `b9884362e37772f772a0d28d1e12622455c14353497b3435deed60aa7e592c5e`
- upstream revision `462cbbf04886e32096bc318833e974ccc43d9fc8`、tree、Bazel 9.0.2、
  module lock、overlay digest、7 noticesをmanifestで固定
- 対象をLinux x86_64、ELF64 little-endian、`EM_X86_64`へ固定し、helper本体から
  interpreter `/lib64/ld-linux-x86-64.so.2`と最大required symbol version
  `GLIBC_2.38` / `GLIBCXX_3.4.32` / `CXXABI_1.3.15`を抽出してmanifestと照合する。
  helperのsize/SHAもverifier側のtrusted constantと一致させ、自己申告manifestだけでは通らない
- import先は実byte、mode、pathから導く`sha256-*` generationで、directory renameにより
  原子的に公開する。同一generationは再検証後にreuseし、tamper時は修復せずfail-closedする
- 今回のverified generationは
  `sha256-ad277af2ad5a634f23c7b84b7f346b02f341905f10fcfa6eb9912db78a0866cb`
- CMake configure時とserver/sidecar destinationのinstall開始前に、同じgenerationの
  再検証に加えてactual helperのdataset-authenticated private PINGを実行する。ABI不足、
  dynamic loader不在、初期化失敗はいずれもinstall前にfail-closedする

原子的なのはbuild-tree内のMozc generation公開であり、project全体の複数install先を
transactional rollbackするものではない。

- `きょうはいしゃにいく`のnatural変換はfirst segment key size 4 / top `今日は`
- `きょうは`をtarget key size 3へresizeするとsegment key size 3 / top `今日`

strict import、通常release build、全Swift回帰257件（6 optional skip、失敗0）、
actual bundle指定のfocused 22件、CTest 17件、実stageを使う59件のpackaging contractは
成功した。一方、このactual
bundleが動くことと、前節のclean/no-network builderをゼロから完遂できることは別の証拠であり、
後者は未証明のままである。

### 再現手順（Linux x86_64、固定runtime ABI）

固定helperはglibc系Linux向けで、`/lib64/ld-linux-x86-64.so.2`、GLIBC 2.38以上、
GLIBCXX 3.4.32以上、CXXABI 1.3.15以上を必要とする。単にx86_64であるだけでは足りず、
CMake importはactual PINGで現在のhostを検証する。

fixed checkout、Bazel/Bazelisk、cacheを明示してbundleを作る。checkoutは上記revision/treeの
clean treeでなければ拒否される。no-network環境では、郵便番号辞書を含む全外部repositoryが
すでに`BAZEL_OUTPUT_ROOT`へ解決済みである必要がある。

```bash
python3 tools/mozc/build_fixed_sidecar_bundle.py \
  --checkout "$MOZC_CHECKOUT" \
  --bazel "$BAZELISK" \
  --output /tmp/fcitx5-grimodex-mozc-bundle \
  --output-user-root "$BAZEL_OUTPUT_ROOT"
```

strict importは内容アドレス付きgenerationの絶対pathを1行だけ出力する。既存generationの
監査だけを行う場合は2つ目のcommandを使う。

```bash
MOZC_GENERATION="$(
  python3 packaging/scripts/verify_mozc_artifact_bundle.py \
    --bundle /tmp/fcitx5-grimodex-mozc-bundle \
    --stage-root /tmp/fcitx5-grimodex-mozc-store
)"
python3 packaging/scripts/verify_mozc_artifact_bundle.py \
  --verify-only "$MOZC_GENERATION"
python3 packaging/scripts/verify_mozc_artifact_bundle.py \
  --verify-host-runtime "$MOZC_GENERATION"
```

CMake import、通常build、actual helper testは次の通り。

```bash
cmake -S . -B build-grimodex \
  -DHAZKEY_SERVER_MOZC_ARTIFACT_DIR=/tmp/fcitx5-grimodex-mozc-bundle
cmake --build build-grimodex --parallel 4

env \
  GRIMODEX_MOZC_TEST_BUNDLE=/tmp/fcitx5-grimodex-mozc-bundle \
  LD_LIBRARY_PATH="$PWD/build-grimodex/bin" \
  hazkey-server/scripts/swift-test.sh --filter GrimodexMozcSidecarTests
```

system installと明示的な比較起動は次の順で行う。未指定ならHazkeyのままである。

```bash
scripts/grimodex-ime.sh install
FCITX5_GRIMODEX_CONVERTER=mozc scripts/grimodex-ime.sh restart
```

ここでいうS0の9 scenario通過は、現行fixtureをそのまま満たすという意味ではない。現行
`partial-commit.json`はlearningを`completed=1, updated=1, committed=1, forgotten=0`と
期待するが、S0 profileは4値すべて0とする。この差は意図的な初期sliceのproduct-contract
gapであり、visible commit semanticsが同じでもfull parityとは数えない。

詳細なboundary、scenario割当、見積りは
[`mozc-core-adapter.json`](./mozc-core-adapter.json)に記録した。

## 更新した判断gate

full rebaseではなく、実装済みのopt-in sliceを比較器として使う。default切替gateの状態は
次の通り。

1. **完了:** dedicated learning-off profileでfake core + real adapterの互換9/9 scenario、
   26/26 stepsに加え、本番segmented reducerのresize/partial commit統合を通した。ただし
   learningを含むfull parityとは数えない
2. **部分完了:** actual fixed B0 helperでnatural/resizeを通した。大規模品質評価は未実施
3. **focused levelで完了:** secure no-call、context非送信、purge/respawn、dataset不一致時の
   reading送信前拒否を通した。process memoryのforensic zeroizationまでは主張しない
4. **完了:** helper EOF時にrequestを再送せずcompositionを保持し、次requestでrespawnする
5. **未完:** 現行partial-commit learning期待値`1/1/1/0`のparityまたは廃止判断
6. **未完:** project/personal dictionaryのgeneric candidate overlay
7. **未完:** B1またはlocked B2で100〜500件のblind実利用corpus
8. **未完:** 同じ長寿命server / IPC境界のwarm latency、steady RSS、failure recovery

現在のMozc session openでもHazkey用`KanaKanjiConverter`を2個生成し、
`HazkeySessionEnvironment`を構築・refreshする。このため現状のserver全体startup/RSSは
Mozc-only対Hazkey-onlyのclean A/Bではない。前段の6.31倍process結果もharness単位の参考値で
あり、実製品server/IPC境界の優位性としては使わない。

ここまで満たす前はHazkeyをdefaultとし、Mozc coreはopt-in比較実装に留める。
