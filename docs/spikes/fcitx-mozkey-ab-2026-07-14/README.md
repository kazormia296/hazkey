# fcitx-mozkey base A/B spike (2026-07-14)

## 結論

`fcitx-mozkey` の変換コアは、今回の小規模コーパスでは Hazkey より
Top-1 が 2 件多かった。一方で、Grimodex の安全・復旧契約は固定コミットの
ままでは満たせず、作者が想定する `daily` 辞書も clean checkout から
再現できない。性能値は測定範囲が異なる参考値であり、優劣の根拠にはしない。

したがって現時点の判断は次の通り。

- Hazkey 全体を `fcitx-mozkey` ベースへ載せ替えない。
- Mozc の変換コアだけを Protocol v2 の後ろへ差し替える追加スパイクは有望。
- `daily` 辞書の固定 snapshot/checksum と 100〜500 件の実利用コーパスが
  揃うまでは、変換品質の最終判断をしない。
- 性能は同じ adapter と測定境界を使うベンチマークを作ってから再評価する。

## 比較条件

- A: Hazkey `0.2.1`, conversion base
  `8e8ce85cca10ee6a02008322628f3f8a965a62db` + このスパイクの
  `ABProbe.swift` harness（変換ロジック自体は未変更）
- B: `Masterisk-F/fcitx-mozkey`, source
  `462cbbf04886e32096bc318833e974ccc43d9fc8`
- 学習、プロジェクト辞書、Zenzai は無効
- 品質は同じ15ケースのTop-1 exact matchで比較
- 性能は同じ入力を使ったが、Hazkeyはconverter call、BはCLI process全体を測定
- Fcitx、IPC、候補UI、描画はいずれの性能値にも含めない

## 実測結果

| 品質指標 | Hazkey | fcitx-mozkey core | 差 |
|---|---:|---:|---:|
| Top-1 exact match | 10/15 (66.7%) | 12/15 (80.0%) | B +2件 / +13.3pt |

Hazkey は `山梨県立美術館` を2位、`mainにマージしました` を2位に出した。
Bはこの2件をTop-1にしたため、Bの2勝・13引き分け・Aの0勝となった。
両者ともTop-1を外したのは次の3件。

- `はだみはなさず → 肌身離さず`
- `おちゃはいりません → お茶はいりません`
- `ねおちつうわ → 寝落ち通話`

Hazkeyでは15件中14件がTop-10に入ったが、BのTop-10列は今回の upstream
評価CLIでは採取していないため、Top-10同士の比較はしていない。

性能は各15ケースを100回、プロセス単位で5回測った。ただし測定範囲が異なる
ため、次の値はbackend間で直接比較できない参考値である。

| backend | latencyの測定範囲 | 記録値 |
|---|---|---:|
| Hazkey | ABProbe内のconverter call sample | mean 4.245 ms/変換、median 3.495 ms、p95 10.355 ms |
| fcitx-mozkey core | `/usr/bin/time`によるCLI process wall clock。起動、data load、test harness処理を含む | mean 782.2 ms/1,500件（単純除算 0.522 ms/件） |

RSSも測定方法が異なるため、差や比率を算出しない。

| backend | RSSの測定方法 | 参考値 |
|---|---|---:|
| Hazkey | ABProbeが取得したbefore/after RSS snapshotの最大値 | 52,312 KiB |
| fcitx-mozkey core | `/usr/bin/time -f %M`のCLI process maximum RSS | 29.6 MiB |

性能を移行判断へ使うには、両backendを同じ長寿命adapter processから呼び、
同じwarmup、計時境界、RSS samplerで再ベンチする必要がある。

詳細な機械可読値は `benchmark.json` に保存した。

## 辞書の再現性ブロッカー

固定コミットの `src/data/dictionary_oss/BUILD.bazel` は
`//data/dictionary_koyasi:mozcdic_ut_daily_local` を入力に含める。しかし、その
target は `generated/profiled/*.txt` を `allow_empty = True` で読む一方、対象
ファイルはgit管理されていない。clean checkout のB実測は、作者のリリース相当
`daily` 辞書ではなく、標準Mozc OSS辞書による値である。

作者想定の比較には、少なくとも次を固定する必要がある。

1. 生成済みdaily辞書 artifact
2. artifact のSHA-256
3. 生成元とライセンス一覧
4. 生成コマンドと入力source revision

## 機能契約

固定コミットを source inspection した予測では、既存の9シナリオは
1 pass / 3 partial / 5 fail。次は移行のhard gateを満たさない。

- secure input: Fcitxからfield typeが伝播せず、project/Zenz/context/learning/
  recovery の即時purge契約がない
- stale candidate: candidate generation、expected revision、無変更拒否がない
- server failure: request ID、checkpoint、effect dedup、exact-once commitがない
- Unicode caret: extended grapheme cluster境界の契約がない
- project integration: snapshot validation、generation pin、session別project辞書、
  consumer handshake/heartbeatがない

これは「Mozc coreに実装不能」という意味ではなく、現在のFcitx/Mozc protocolを
そのままGrimodexのProtocol v2として採用できないという意味である。詳細は
`capabilities.json` に保存した。実行結果ではなくsource予測なので、adapterを
作った後には9シナリオ/26ステップをunitとFcitx full-stackの両方で再実行する。

## Hazkey側の再実行

```bash
set -euo pipefail

cmake --build build-grimodex --target build_hazkey_server

export ROOT="$PWD"
export LD_LIBRARY_PATH="$ROOT/build-grimodex/bin"
export GGML_BACKEND_DIR="$ROOT/build-grimodex/bin"
SERVER="$ROOT/build-grimodex/hazkey-server/swift-build/release/hazkey-server"
CORPUS="$ROOT/hazkey-server/Tests/grimodex-spike/Fixtures/ime-base-ab-v1/conversion-quality-v1.tsv"
DICTIONARY="$ROOT/hazkey-server/azooKey_dictionary_storage/Dictionary"
SOURCE_REF=8e8ce85cca10ee6a02008322628f3f8a965a62db

for run in 1 2 3 4 5; do
  "$SERVER" --ab-probe --corpus "$CORPUS" \
    --dictionary "$DICTIONARY" --source-ref "$SOURCE_REF" \
    --warmups 5 --iterations 100 --top-k 10 \
    > "/tmp/hazkey-ime-base-ab-run-$run.jsonl"
done

python3 tools/dictionary/evaluate_conversion_quality.py \
  --corpus "$CORPUS" \
  --results /tmp/hazkey-ime-base-ab-run-1.jsonl \
  --top-k 1 \
  --output /tmp/hazkey-ime-base-top1.json

python3 tools/dictionary/summarize_ab_probe.py \
  /tmp/hazkey-ime-base-ab-run-*.jsonl \
  --output /tmp/hazkey-ime-base-performance.json
```

## fcitx-mozkey側の再実行

第三者リポジトリのBazel buildはそのリポジトリのbuild logicを実行するため、
隔離環境で内容を確認してから実行する。

```bash
set -euo pipefail

ROOT="$PWD"
CORPUS="$ROOT/hazkey-server/Tests/grimodex-spike/Fixtures/ime-base-ab-v1/conversion-quality-v1.tsv"
python3 tools/dictionary/prepare_mozc_quality.py \
  --corpus "$CORPUS" \
  --output /tmp/fcitx-mozkey-quality.tsv

SPIKE_TMP="$(mktemp -d /tmp/fcitx-mozkey-ab.XXXXXX)"
MOZKEY="$SPIKE_TMP/fcitx-mozkey"
MOZKEY_HOME="$SPIKE_TMP/home"
BAZEL_ROOT="$SPIKE_TMP/bazel"

git clone https://github.com/Masterisk-F/fcitx-mozkey.git "$MOZKEY"
git -C "$MOZKEY" checkout --detach \
  462cbbf04886e32096bc318833e974ccc43d9fc8

test "$(git -C "$MOZKEY" rev-parse HEAD)" = \
  462cbbf04886e32096bc318833e974ccc43d9fc8
test -z "$(git -C "$MOZKEY" status --porcelain)"
test ! -d "$MOZKEY/src/data/dictionary_koyasi/generated/profiled"

cd "$MOZKEY/src"
bazelisk --output_user_root="$BAZEL_ROOT" build \
  //converter:quality_regression_main \
  //session:session_handler_main \
  --config=release_build \
  --config=oss_linux

HOME="$MOZKEY_HOME" \
./bazel-bin/converter/quality_regression_main \
  --data_file=./bazel-bin/data_manager/oss/mozc.data \
  --data_type=oss \
  --test_files=/tmp/fcitx-mozkey-quality.tsv
```

性能測定用の1,500変換入力は同じadapterで生成できる。

```bash
cd "$ROOT"
python3 tools/dictionary/prepare_mozc_quality.py \
  --corpus "$CORPUS" \
  --output /tmp/fcitx-mozkey-quality-100.tsv \
  --repeat 100

cd "$MOZKEY/src"
for run in 1 2 3 4 5; do
  HOME="$MOZKEY_HOME" \
  /usr/bin/time -f "run=$run elapsed_sec=%e max_rss_kib=%M" \
  ./bazel-bin/converter/quality_regression_main \
    --data_file=./bazel-bin/data_manager/oss/mozc.data \
    --data_type=oss \
    --test_files=/tmp/fcitx-mozkey-quality-100.tsv
done
```

固定refのquality regression CLIは、caseごとに `FAILED` を出しても通常は終了コード0で
終了する。caseの成否は終了コードではなくログで確認する。性能値は
`/usr/bin/time` の出力から集計する。

記録したBのTop-1は `fcitx-mozkey-top1.jsonl`。共通評価と差分は次で再生成する。

```bash
python3 tools/dictionary/evaluate_conversion_quality.py \
  --corpus "$CORPUS" \
  --results "$ROOT/docs/spikes/fcitx-mozkey-ab-2026-07-14/fcitx-mozkey-top1.jsonl" \
  --top-k 1 \
  --output /tmp/fcitx-mozkey-ime-base-top1.json

python3 tools/dictionary/compare_conversion_quality.py \
  --a-report /tmp/hazkey-ime-base-top1.json \
  --b-report /tmp/fcitx-mozkey-ime-base-top1.json \
  --a-name Hazkey \
  --b-name fcitx-mozkey-core \
  --output /tmp/ime-base-ab-quality.json
```

## 次の判定ゲート

全面移行を再検討するのは、次をすべて満たした後とする。

1. daily辞書をchecksum付きで再現できる
2. 100〜500件のblind実利用corpusで品質を再測定する
3. 9/9 scenarios、26/26 stepsをunit/full-stackの双方で通す
4. secure traceに機密状態の残留がない
5. kill/response-loss 100回でcomposition lossと二重commitがない
6. 2 session以上、project switch 1,000回でcross-project leakageがない
7. warm p95が現行の1.10倍以内、steady RSSが1.30倍以内

現段階では小規模コーパスの品質はBに追試価値があるが、安全契約と辞書再現性を
満たさないためAを維持する。性能は現測定から優劣を判断せず、共通benchmarkで
再測定する。
