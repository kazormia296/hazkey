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
  統合検証し、fixed B0 actual helperでも自然変換と文節resizeを通過した。公開Protocol v2の
  version/snapshotは変えず、open-sessionにadditiveな永続learning capabilityだけを追加した。
- fixed Mozc sidecarを同じSwift executable / `candidates()`境界で測ったwarm latencyでも
  Bが明確に優位だった。meanは5.77倍、p95は5.79倍高速だった。実serverの
  Protocol v2 `startConversion`往復でも、単一A→B runではmean 13.90倍、p95 15.12倍の
  差が残った。ただしTop-10は14/15から12/15へ低下し、B1辞書でのblind品質評価、
  中央v2 conversion-only contract、Fcitx込みsoakと交互cycleが未完のため、
  default切替の根拠にはしない。
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
| Protocol v2 `startConversion` mean、各1,500変換 | 16.719 ms | 1.203 ms | 単一A→B runでA/B=13.90。steady-state signal |
| 同p95 | 38.733 ms | 2.561 ms | A/B=15.12 |
| 実server endpoint PSS観測最大 | 62,381 KiB | 70,752 KiB | Bは13.4%増。現行product overhead込み |
| 既存Protocol v2との接続 | native | actual helperで実server 1,500変換を完走 | version変更なし。additive capabilityのみ。full parityではない |
| 辞書再現性 | repo固定 | clean OSSのみ固定 | full dailyはlock前提 |

## 最新: 実adapter境界のwarm A/B

`2364bc1`上の未commit worktreeで、AB probeを実Mozc sidecar対応へ拡張して再測定した。
これは前節のfresh process比較を置き換えるものではなく、起動済みconverter adapterの
同じ`KanaKanjiConverting.candidates()`呼び出し境界を比較する追加証拠である。

### 条件

- 15件、各100 iteration、warmup 5、Top-10、backendごとに5 run（各7,500変換）
- odd cycleはHazkey→Mozc、even cycleはMozc→Hazkey
- learning / Zenzaiは両方off
- Mozc helperは各runで1 processを再利用し、計時区間に起動・PINGを含めない
- RSS/PSSは計時区間外の同じbefore/after phaseでparent→helperの順に逐次取得し、
  対応するpairを合算する。厳密な同時snapshotではない
- Mozc generationは`O_NOFOLLOW`で開き、固定size/SHA-256、manifest、mode、link countを
  fail-closed検証した。同じbyteをprivate read-only snapshotへ固定し、そのpathだけを実行した
- `resource.path`は入力元generationを示し、`resource.fingerprint`はそこからpinして実行した
  helper/data/manifestの名前とbyteだけを対象とする。generation全体や測定後のpath内容のdigestではない
- JSONLは全case終了後もbufferし、helper cleanupとsnapshot削除の成功後だけ公開した

### 結果

| 指標 | Hazkey | Mozc sidecar | Hazkey / Mozc |
|---|---:|---:|---:|
| mean warm latency | 3.858 ms | 0.669 ms | 5.77 |
| median warm latency | 3.210 ms | 0.644 ms | 4.99 |
| p95 warm latency | 9.194 ms | 1.589 ms | 5.79 |
| max total RSS | 37,460 KiB | 48,688 KiB | 0.77 |
| max total PSS | 33,030 KiB | 40,390 KiB | 0.82 |

Mozcのtotal PSSはHazkeyより22.3%多い。RSSは共有pageを二重計上するため、memory判断は
PSSを主指標とする。候補列は両backendとも5 runで完全一致し、Mozcは全runで
helper launch count 1、cleanup failure count 0、一時snapshot残留0だった。

Gate 6のdictionary overlay追加後、最新release binaryのempty-overlay経路を同じ15件、
warmup 5、各100 iterationで単一回帰runした。1,500 sampleのmean 0.661 ms、median
0.632 ms、p95 1.578 ms、helper launch 1、cleanup failure 0で、上記5-run集約の
0.669 / 0.644 / 1.589 msと同等だった。これはhot-path回帰が見えないことの確認であり、
単一runなので5-cycle結果や採用判断の倍率は更新しない。

同じ15件の品質は、Top-1がHazkey 10/15 (66.7%)、Mozc 12/15 (80.0%)、
Top-10がHazkey 14/15 (93.3%)、Mozc 12/15 (80.0%)だった。Top-1の改善だけを見て
defaultを切り替えることはできない。MozcがTop-10で落とした`肌身離さず`、
`お茶はいりません`、`対人スキル`を含め、100〜500件のblind corpusが必要である。

### blind base/core品質評価プロトコル

ABProbe v3のHazkey/Mozc出力から、backend名、expected、元case ID、source refを除いた
review JSONLと、別管理するunblind keyを生成するツールを追加した。case順とX/Y配置は
secret seedから決定し、全体とcategoryごとの配置差を最大1に抑える。corpus、raw run、
各review record、review全体、key、judgmentのSHA-256とresource fingerprint、warmup/
iteration/Top-K条件を最終reportへ残す。v3 raw resultは各caseのreading、同じbyte snapshotから
計算したcorpus SHA-256、case数、Top-Kも保持する。schema不一致、case/reading/category/
corpus/source ref/測定条件の不一致、欠落・重複judgment、改ざん、既存outputの上書きは
fail-closedで拒否する。

raw ABProbe v3を同じcorpus、source ref、warmup、iteration、Top-Kで取得した後、review担当へは
packet内の`review.jsonl`だけを渡す。seedと`unblind-key.json`は採点完了まで分離する。
seedはowner-onlyな256-bit値だけを受理する。packetはprivate temporary directoryで全fileを
fsyncしてからdirectory単位でpublishし、`manifest.json`がreview/key双方の完成状態を固定する。
review、key、manifest、最終reportはmode 0600、packet directoryは0700で生成する。

```bash
umask 077
python3 -c 'import secrets; print(secrets.token_hex(32))' > /tmp/grimodex-blind-seed

python3 tools/dictionary/blind_conversion_ab.py prepare \
  --corpus /path/to/frozen-corpus.tsv \
  --run-a /path/to/hazkey-ab-probe.jsonl \
  --run-b /path/to/mozc-ab-probe.jsonl \
  --seed-file /tmp/grimodex-blind-seed \
  --output-directory /private/path/to/blind-packet
```

reviewerは各opaque caseについて`x`、`y`、`tie`、`both_bad`のいずれかを1件ずつ記録する。

```json
{"schema":"hazkey.blind-conversion-ab-judgment.v1","case":"blind-...","judgment":"x"}
```

全caseのjudgmentが揃った後だけunblindする。

```bash
python3 tools/dictionary/blind_conversion_ab.py score \
  --packet /private/path/to/blind-packet \
  --judgments /path/to/judgments.jsonl \
  --output /path/to/unblinded-report.json
```

reportでは人手選好とobjective exact matchを分離する。前者は全caseに対するnet preference、
decisive率、Wilson 95%区間、paired sign testを含み、後者はbackend/category別Top-1/Top-K、
expected rank、paired win/loss/tie、B−A deltaを含む。統計値だけから採用可否は自動決定せず、
非劣性marginと最小有効件数はcorpusを開封する前に別途固定する。

現行15件はtoolのdress rehearsalには使えるが、複製して100件相当に水増ししない。
正式Gate 7には、backend出力を見る前に凍結した100〜500件の実利用corpusと、B1組込み済み
bundleまたはimmutable B2 snapshotが必要である。現物にはどちらのartifactもないため、
B0結果はbase/core品質の予行測定とだけ表記する。またABProbeはproductの自然文節確定経路では
なく`KanaKanjiConverting.candidates()`境界なので、Fcitx/Protocol v2込み品質とは区別する。

### 15万変換soak

同じMozc adapter/helperを、15件×10,000 iteration（150,000変換）で連続実行した。
mean 0.698 ms、median 0.675 ms、p95 1.636 ms、max 3.908 msだった。helperは
全期間1 process、cleanup failure 0、max total PSS 40,724 KiB、一時snapshot残留0で
完走した。これは無障害時の長時間安定性であり、kill/EOF/timeoutの故障注入や
Fcitx→Protocol v2→server全体のsoakではない。

集約値、resource fingerprint、candidate fingerprint、測定条件は
[`runtime-ab-summary.json`](./runtime-ab-summary.json)に固定した。

## 最新: 長寿命server / Protocol v2境界

direct adapterだけでなく、隔離した実`hazkey-server`、Unix socket framing、Protocol v2
session/revision、response protobuf decodeを通すopt-in benchmarkを追加した。各backendで
server、client、sessionを1つずつ維持し、15件×100 iterationを処理した。

計測区間は、readingをcomposingへ投入した後の`startConversion` request 1往復だけである。
reset、insert、helper初回起動/PING、RSS/PSS取得は計測外とした。warmupは各case 5回、
learning、Zenzai、自動変換はProtocol経由で両backendともoffに固定した。候補列の一致は
反復中のdrift検出にだけ使い、このcandidate windowは選択中segmentを表すため品質スコアには
流用しない。

| 指標 | Hazkey server | Mozc server + helper | Hazkey / Mozc |
|---|---:|---:|---:|
| mean | 16.719 ms | 1.203 ms | 13.90 |
| median | 10.928 ms | 1.076 ms | 10.16 |
| p95 | 38.733 ms | 2.561 ms | 15.12 |
| max | 43.027 ms | 5.044 ms | 8.53 |
| endpoint total PSS観測最大 | 62,381 KiB | 70,752 KiB | 0.88 |

Mozcのendpoint total PSS観測最大は13.4%多かった。PSSはwarmup後と全変換後に
server→helperの順で取得した2 endpoint snapshotの比較であり、実行中peakや厳密な同時値ではない。
Mozc serverはbefore/afterで同じhelper PIDとexpected executable pathを示し、private server
停止後にhelperも終了した。Hazkey serverのbefore/afterにはchild processがなかった。
server executable、corpus、dictionary、helper、dataのidentity、確定済み計測器commit
`be7824b678338f34a19f5700c9fd4798564e3974`、全3,000 latency sampleは
[`protocol-v2-backend-benchmark.json`](./protocol-v2-backend-benchmark.json)に固定した。

これはsteady-stateの実server/IPC境界を埋める結果だが、今回はHazkey→Mozcの単一runであり、
CPU affinityはhostの`0-11`を継承してpinしておらず、交互5 cycleのpaired推定ではない。
また、Fcitx key dispatch、snapshot rendering、GUIは含まない。
現行Mozc sessionはserver内に未使用のHazkey converterも構築するため、PSSはcleanな
Mozc-only coreの値ではなく現在のproduct実装全体の値である。外部SIGKILLの実server故障注入は
後述のとおり完了したが、Fcitxを含むsoakは別gateとして残る。

### Protocol v2経由のEOF/timeout回復

private helperが最初の`CONVERT`を受理した直後にEOFで終了するfixtureを実serverの背後に置き、
Protocol v2の回復経路も通した。最初の`startConversion`は`CONVERTER_UNAVAILABLE`となるが、
compositionの`かな`、進んだrevision、空のeffect/candidate windowを含むauthoritative snapshotを
返し、server/socketは生存する。同じrequest IDの再送はbyte-identicalなcached failureとなり、
helper launchとaccepted conversionは各1回のまま増えない。

返却revisionに対するfresh request IDだけがhelperをlazy respawnし、2回目のaccepted conversionで
`仮名`を返して`previewing`へ進んだ。launchとconversionは最終的に正確に2回で、失敗helperの
private rootは失敗応答を受け取った時点で消え、回復helperのrootもserver停止時に消えた。これは
`exit(0)`由来のEOFを対象にした実server testである。

frame headerで16-byte bodyを宣言して2 bytesだけflushした後に終了するfixtureでも、同じ
no-replay、cached failure、fresh respawn、root cleanup契約を通した。これによりheader受信後の
partial-frame EOFも通常EOFと区別して固定した。

同じ契約を、PING成功後、最初の`CONVERT`受理をmarkerへflushしてから応答しないfixtureにも
適用した。production既定の1,500 msでtimeoutし、hanging helperとprivate rootを回収して
`CONVERTER_UNAVAILABLE`を返す。同じrequest IDはstallを再実行せず、fresh requestだけが
2つ目のhelperで成功した。これによりpost-handshake timeout回復も完了した。

さらに、実server内で長寿命になっているhelperを別sessionで起動し、PIDと`/proc` cmdlineを
fixture helperへ照合してからテストprocessが外部`SIGKILL`した。kill前から別のtarget sessionに
`かな`のcompositionを保持し、旧PIDがserverのchild setから消えるまで待ってから、そのsessionの
`startConversion`を送った。server/socketは落ちず、fresh helperをlazy spawnして`仮名`を返し、
`previewing`へ進んだ。launch/accepted conversionは正確に`1→2`、旧private rootは回復request時、
新rootはserver停止時に消えた。同じrequest IDの再送はbyte-identicalで、helper実行回数も増えない。
これはrequest間の外部kill回復を証明する。in-flight response loss/no-replayは隣接するEOF、
partial-frame EOF、timeout testが別途固定している。

benchmark再実行時は、検証済みread-only runtime generationを明示する。

```bash
RUNTIME=/path/to/mozc-runtime-generation
SOURCE_REF=<full-40-character-commit>
TOOLCHAIN='Swift version 6.3.3 (swift-6.3.3-RELEASE)'

env \
  SWIFT_SCRATCH_PATH="$PWD/build-grimodex/hazkey-server/swift-build" \
  LD_LIBRARY_PATH="$PWD/build-grimodex/bin" \
  GGML_BACKEND_DIR="$PWD/build-grimodex/bin" \
  GRIMODEX_PROCESS_E2E_SERVER="$PWD/build-grimodex/hazkey-server/swift-build/x86_64-unknown-linux-gnu/release/hazkey-server" \
  FCITX5_GRIMODEX_DICTIONARY="$PWD/hazkey-server/azooKey_dictionary_storage/Dictionary" \
  GRIMODEX_PROCESS_E2E_MOZC_HELPER="$RUNTIME/fcitx5-grimodex-mozc-helper" \
  GRIMODEX_PROCESS_E2E_MOZC_DATA="$RUNTIME/mozc.data" \
  GRIMODEX_PROCESS_E2E_AB_WARMUPS=5 \
  GRIMODEX_PROCESS_E2E_AB_ITERATIONS=100 \
  GRIMODEX_PROCESS_E2E_AB_SOURCE_REF="$SOURCE_REF" \
  GRIMODEX_PROCESS_E2E_AB_BUILD_CONFIGURATION=release \
  GRIMODEX_PROCESS_E2E_AB_TOOLCHAIN="$TOOLCHAIN" \
  GRIMODEX_PROCESS_E2E_AB_OUTPUT="$PWD/protocol-v2-backend-benchmark.json" \
  hazkey-server/scripts/swift-test.sh --configuration release \
    --traits ZenzaiSupport \
    -Xlinker -L"$PWD/build-grimodex/bin" \
    --filter GrimodexProcessBackendBenchmarkTests
```

## 実行中Mozc runtimeの現物確認

install/restartは行わず、既存sessionを確認した。`/usr`のhelper/dataはartifact verifierを
通過し、実行中serverとFcitxの両方が次を継承していた。

- `FCITX5_GRIMODEX_CONVERTER=mozc`
- helper/dataは
  `/run/user/1000/fcitx5-grimodex/mozc-runtime/sha256-4e61bd...`の同一generation
- generation/helper/data modeは`0555` / `0555` / `0444`
- helper SHA-256は`8676275b...577d`、data SHA-256は`b9884362...c5e`
- 初回確認時のactive engineは`grimodex`、`fcitx5-remote` stateは`2`だった。測定後の
  最終確認では同じserver/helper processが生存したまま`keyboard-jp` / state `1`へ変わって
  いたため、この値はruntime identityではなく、その時点のfocus/input-context状態として扱う

したがって今回のA/Bは、誤ってHazkey経路を測った結果ではない。ただしA/B probe自体は
実行中Fcitxへ接続せず、別のprivate helperで測定している。

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
protobufである。公開Protocol v2のversion/snapshotは変更せず、open-session応答にproto3 optional
`persistent_learning_available`だけを追加した。present falseはconversion-only、absenceは旧serverの
unknown capabilityであり、現在のsecure/policy状態とは区別する。

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
- project辞書はsession-scopedかつcomposition中にpinされたGrimodex snapshot、personal/temporary辞書は
  thread-safeなimmutable snapshotから取得し、辞書内容をprivate helperへ送らない
- exact readingの候補はproject priority降順、personal/temporaryの安定順、built-in guard、
  Mozcの順で統合し、NFCでfirst-wins重複排除してsuggestion limitを適用する
- natural conversionではstable input boundary上の最長辞書prefixにsegmentを合わせ、
  明示的な文節resizeは常にその境界を優先する
- overlayが空ならprefix探索を省略し、必要時もprefix探索用のrender/mapを1回だけ行う。永続user辞書は
  CRUD/importと同じ検証をload時にも通し、不正・unsupported・重複recordをfirst-valid-winsで除外する
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

focused `GrimodexMozcSidecarTests`は29 testsで、通常実行は28 pass + actual bundle smoke
1 optional skip、bundle指定実行では29 passした。coverageはexact opt-in、Unicode boundary、
stable Romaji resize、segment resize、secure no-call、候補一覧OFF時のlive candidateのみ表示、
prediction no-op、purge/respawn、active profile writer停止後のprivate `TMPDIR` cleanup、
failure時のcomposition保持、stale guard、
learning/Zenzai diagnostics hard-off、reading送信前dataset handshake、CONVERT受信後EOFの
no-replay、real server / Protocol v2での通常/partial-frame EOFとpost-handshake timeoutの
cached failure/fresh respawn、project/personal辞書の優先順位・NFC重複排除・候補上限、
natural最長prefixと明示resize、実serverでのproject scope/pinとlive personal CRUD反映、
empty-overlayの長文fast path、secure時の辞書provider no-call、response cross-field mismatch、zero target、timeout/oversize/
request mismatchを含む。実server辞書E2Eはfake sidecarを用い、候補統合とProtocol v2/session
境界を検証する。actual C++ helperは従来どおりnatural/resize smokeの範囲である。

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

strict import、通常release build、全Swift回帰277件（12 optional skip、失敗0）、
actual bundle指定のfocused 30件、CTest 17件、実stage検証を含む65件のpackaging contractは
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

system installと明示的な比較起動は、通常runnerと混ぜずMozc専用runnerで行う。

```bash
BUILD_DIR="$PWD/build-grimodex" ./scripts/grimodex-ime_mozc.sh install
BUILD_DIR="$PWD/build-grimodex" ./scripts/grimodex-ime_mozc.sh restart
```

ここでいうS0の9 scenario通過は、現行fixtureをそのまま満たすという意味ではない。現行
`partial-commit.json`はlearningを`completed=1, updated=1, committed=1, forgotten=0`と
期待する。S0 draftでは実sessionの`allowsLearning=false`を設定し、全stepの
`pending_learning=false`とpersistent update/commit/forget 0を確認する。`completed`は永続学習ではない
process-local cache callbackなので非規範とし、partial commitでは実際に1回呼ばれる。中央Grimodexに
正式v2 profileがまだないため、visible commit semanticsが同じでもv1/v2 conformanceとは数えない。

詳細なboundary、scenario割当、見積りは
[`mozc-core-adapter.json`](./mozc-core-adapter.json)に記録した。

## 更新した判断gate

full rebaseではなく、実装済みのopt-in sliceを比較器として使う。default切替gateの状態は
次の通り。

1. **互換経路完了:** local draft learning-off profileでfake core + real adapterの互換9/9 scenario、
   26/26 stepsに加え、本番segmented reducerのresize/partial commit統合を通した。ただし
   learningを含むfull parityとは数えない
2. **部分完了:** actual fixed B0 helperでnatural/resizeを通した。大規模品質評価は未実施
3. **focused levelで完了:** secure no-call、context非送信、purge/respawn、dataset不一致時の
   reading送信前拒否を通した。process memoryのforensic zeroizationまでは主張しない
4. **完了:** helperの通常/partial-frame EOFとtimeout時にrequestを再送せずcompositionを保持し、
   同じrequest IDをcached failureとして返し、fresh requestだけでrespawnする経路を
   実server / Protocol v2で通した
5. **hazkey準備完了・中央未完:** v1 `1/1/1/0`は不変のまま、実policyをlearning-offにした
   callback監査、全step pending false、runtime capability、ADR proposalを追加した。正式完了には
   中央Grimodexのversioned v2 `learning: disabled` profileと、そのSHA lock vendorが必要
6. **focused levelで完了:** session-scoped project snapshotとlive personal/temporary snapshotを
   generic candidate overlayへ接続し、priority/stable order、NFC first-wins、候補上限、
   natural最長prefix、明示resize優先、project scope/pin、CRUD後の次composition反映、
   stale revision回復、secure no-call、不正な永続recordのload時除外をunit + 実server /
   Protocol v2で通した
7. **基盤完了・測定未完:** backend-blind review/unblind toolは追加した。B1またはlocked B2と、
   backend出力を見る前に凍結した100〜500件の実利用corpusでの正式測定が必要
8. **部分完了:** 同じadapter境界のwarm latency、PSS、150,000変換soakに加え、
   長寿命server / Protocol v2境界のsteady-state 1,500変換、通常/partial-frame EOF、timeout、
   外部SIGKILL回復は完了。Fcitx込みsoakと交互cycleは未完

現在のMozc session openでもHazkey用`KanaKanjiConverter`を2個生成し、
`HazkeySessionEnvironment`を構築・refreshする。このため現状のserver全体startup/RSSは
Mozc-only対Hazkey-onlyのclean A/Bではない。前段の6.31倍process結果、5.77倍adapter結果、
今回の単一run 13.90倍Protocol v2結果はいずれも明確な速度シグナルだが、最後の値も
交互cycleを経た一般化可能な倍率とは扱わない。

ここまで満たす前はHazkeyをdefaultとし、Mozc coreはopt-in比較実装に留める。
