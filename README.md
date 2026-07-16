# fcitx5-grimodex

Grimodex向けのFcitx5日本語入力です。Grimodexが公開するプロジェクト別辞書と
Zenzai条件を、アプリ・入力欄ごとに分離した変換セッションへ安全に反映します。

このリポジトリは
[Hazkey](https://github.com/7ka-Hiira/hazkey)から派生し、
[AzooKeyKanaKanjiConverter](https://github.com/azooKey/AzooKeyKanaKanjiConverter)
を利用しています。元のHazkeyと同時にインストールできる独立製品です。

## 製品ID

- パッケージ: `fcitx5-grimodex`
- Fcitx addon / input method: `grimodex`
- server: `fcitx5-grimodex-server`
- settings: `fcitx5-grimodex-settings`
- user data: `$XDG_*_HOME/fcitx5-grimodex`
- socket: `$XDG_RUNTIME_DIR/fcitx5-grimodex/server.sock`

Grimodexとの共有契約は
`$XDG_DATA_HOME/com.miyakey.grimodex/ime`（または`GRIMODEX_IME_ROOT`）です。

## IMEアーキテクチャ

- Swift `CompositionSession`がpreedit、cursor、候補、文節、revision、
  recovery checkpointを一元管理します。
- Fcitx側はkeyをsemantic actionへ変換し、Protocol v2 snapshotを描画し、
  monotonic effectを一度だけ適用します。
- 旧procedural protocolとC++側の重複composition stateは削除済みです。
- 共通`composition-behavior-v1`の全9シナリオをSHA-256固定し、Linux
  release gateで全step比較します。

基本カーソル編集、候補/文節操作、部分確定、JISキー、F6–F10、再接続、
secure inputに加え、ユーザー辞書CRUD/import/export、候補忘却、再変換、
Unicode入力、right context、予測候補を実装しています。
自動変換中のLeft / Rightは入力文字カーソルを移動し、候補選択中のみ
Left / Rightで未確定の文節を移動します。Shift+Left / Shift+Rightで
選択文節の境界を変更できます。Space / Up / Downは選択文節だけを変換し、
Enterで全文節をまとめて確定します。各文節の間は表示専用の `│` で示します。

## Fcitx5 アドオン設定

Fcitx5の「アドオンを設定」から Grimodex IME を開くと、
`Fix embedded preedit cursor at the beginning of the preedit` を切り替えられます。
既定ではオフで、実際の編集位置にキャレットを表示します。有効にするとキャレットを
preedit先頭へ固定するため、候補ウィンドウを変換中の文字列の左端に安定して表示できます。

Phase 0–11の実装対応表、自動テスト結果、リリース機で行う手動GUI確認は
[Linux IME release evidence](docs/linux-release-evidence.md)に記録しています。

## プライバシー

製品ビルドはZenzaiモデル本体を同梱しません。専用の
`kazormia296/grimodex-models` リポジトリにあるモデルRelease assetを、専用の
モデルヘルパーがSHA-256検証付きでユーザーデータへ取得します。モデルReleaseより
古いインストールを更新した場合は、専用assetが利用できないとき固定した上流モデルへ
フォールバックします。
変換server自体は従来どおりローカルのUnix socketとローカルファイルだけを利用します。

## ビルド

### 依存関係

- Swift >= 6.1
- fcitx5 >= 5.0.4
- Qt >= 6.7 (6.2以降でビルド可能ですが表示が崩れる場合があります)
- CMake >= 3.21 (4.x以降推奨)
- Protobuf >= 3.12
- Ninja
- Gettext
- Vulkan SDK（GPU変換を使う場合。CPU専用ビルドでは不要）

### ソースビルド・インストール手順

ninjaを利用します。

```sh
git clone --recursive https://github.com/kazormia296/hazkey.git
cd hazkey
mkdir build && cd build
cmake -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr -DGGML_VULKAN=ON -G Ninja ..
ninja
sudo ninja install
```

同じ操作を繰り返す場合は、付属スクリプトから呼び出せます。

```sh
./scripts/grimodex-ime.sh build    # configure + build
./scripts/grimodex-ime.sh install  # install the existing build
./scripts/grimodex-ime.sh restart  # replace the old server and restart Fcitx5
./scripts/grimodex-ime.sh all      # build + install + restart
```

`BUILD_DIR`、`INSTALL_PREFIX`、`GGML_VULKAN` などは環境変数で上書きできます。
Vulkan対応ビルドでは、Zenzaiのデバイス既定値は「自動（GPU優先）」です。
GPUが列挙されない場合はCPUへフォールバックします。CPU専用でビルドする場合は
`GGML_VULKAN=OFF ./scripts/grimodex-ime.sh build`を使用してください。

実験的なMozc比較バックエンドを使う場合は、固定sidecar bundleを指定して専用スクリプトを
実行します。通常版とはビルドディレクトリと再起動ログが分離され、再起動したFcitxにも
`FCITX5_GRIMODEX_CONVERTER=mozc`が引き継がれます。再起動前にはinstall済みhelper/dataの
固定ID、ABI、private PINGも検証し、検証済みのread-only runtime generationだけを
サーバーへ渡します。

```sh
MOZC_ARTIFACT_DIR=/path/to/fcitx5-grimodex-mozc-bundle \
  ./scripts/grimodex-ime_mozc.sh all
```

すでにMozc bundle付きでconfigure済みの場合は、`BUILD_DIR`を指定すればCMake cache内の
artifact pathを再利用できます。pure Mozc modeではlearningとZenzaiは無効です。serverは
open-session応答で永続learning capabilityを`false`として明示しますが、履歴依存の設定UIと
候補forgetの無効表示はdefault切替前の残作業です。

Mozc-first投機ハイブリッドの初期スパイクは、同じ専用runnerへ明示的に指定します。
入力中の表示と正式変換はMozcを待ち時間の基準にし、バックグラウンドHazkeyが同じ
composition revisionの全文入力に対する先頭自然文節を1回だけ準備し、そのrequestと共有gateが
Space時点までに完了した場合だけ、同じ文節境界の候補を補完します。
未完了・陳腐化・文節境界不一致は即Mozc-onlyへフォールバックし、選択開始後の候補順は
変更しません。現在のruntime H0は、評価済みholdoutがないためMozc Top-1とTop-3順を
固定します。Hazkey側では通常どおりZenzaiを利用でき、永続学習へ送るのは実際に選択した
Hazkey由来候補だけです。Mozc由来候補はこのmodeでも学習対象にしません。

```sh
MOZC_ARTIFACT_DIR=/path/to/fcitx5-grimodex-mozc-bundle \
GRIMODEX_MOZC_BACKEND=mozc-hybrid \
  ./scripts/grimodex-ime_mozc.sh all
```

paired ABProbe v3/v4/v5の診断評価には
`tools/dictionary/evaluate_mozc_hybrid_spike.py`を使用します。評価出力は
`diagnostic_only=true`かつ`new_holdout_required=true`で、既知corpusを正式な採用判定へ
再利用しないようfail-closedに検証します。`runtime_h0_top1`は実装既定のTop-1固定規則、
`top1.hybrid`は採用されていない診断H1 one-sided-consensus規則です。ABProbe v3は候補surface
だけを記録し文節の`consuming_count`を持たないため、評価schema v3は
`runtime_boundary_parity_established=false`と`runtime_apply_eligible=false`を明示します。
H1はproductionの候補順を変えず、境界込みのshadow counterだけで観測します。
structured diagnosticsの`shadow_promotion_evaluations`、
`shadow_promotion_opportunities`、`shadow_promotion_boundary_rejected`は、H0の候補順・
origin・learning routeを変更せずにH1を観測します。

```sh
python3 tools/dictionary/evaluate_mozc_hybrid_spike.py \
  --corpus /path/to/formal-corpus.tsv \
  --hazkey-results /path/to/hazkey-ab-probe-v3.jsonl \
  --mozc-results /path/to/mozc-ab-probe-v3.jsonl \
  --output /tmp/mozc-hybrid-quality.json
```

文節境界の診断ではABProbeへ`--result-schema v4`を明示します。v4は通常の全文
`candidates`ではなく実runtimeと同じ`segmentCandidates`経路を呼び、各候補へ
`text`、1始まりの`rank`、composition element単位の`consuming_count`を記録します。
paired evaluatorはMozc Top-1と同じ境界のHazkey候補だけをマージします。一方、既存corpusの
正解は全文でv4候補は先頭文節なので、v4のTop-1、rescue/regression、oracleは比較不能として
fail-closedになります。順位変更の判定には文節正解ラベル付きの新規holdoutが必要です。

`--result-schema v5`はv4候補に、ABProbeが実際に構築した入力全体の
`composition_span={start,count,unit}`を追加します。paired evaluatorは、Mozc Top-1の
`consuming_count`がこの明示span全体と一致するケースだけ、既存の全文正解と比較します。
同じ表層でもspanが異なる候補は正解に数えず、残りは比較不能のまま除外します。
v5の機械可読レポートは、全文span一致の全診断行を`diagnostic_target_comparable`、
`protected`を除いたformal-quality行を`formal_quality`として別集計にします。
全診断行の分母は正式品質値として扱いません。
診断H2 `one-sided-consensus-width-guard`はH1を基に、両Top-1の差が全角ASCIIと半角ASCII、
または全角空白と半角空白だけの場合の昇格を抑えます。一般のNFKC互換文字は畳み込みません。
H2の抑制集計は比較可能・比較不能を分け、比較不能な抑制を改善または悪化へ算入しません。
H2も診断専用で、runtimeの候補順は変更しません。

```sh
python3 tools/dictionary/evaluate_mozc_hybrid_spike.py \
  --corpus /path/to/formal-corpus.tsv \
  --hazkey-results /path/to/hazkey-ab-probe-v5.jsonl \
  --mozc-results /path/to/mozc-ab-probe-v5.jsonl \
  --output /tmp/mozc-hybrid-composition-span.json
```

既知1,360件については、変換表層とは独立した先頭文節境界の診断用アノテーションも作成できます。
`prepare_mozc_hybrid_boundary_annotations.py`はLinderaの組み込みUniDicでプリアノテーションし、
`expected`に複数の許容表層がある場合は一つだけを選ばず全て解析して境界consensusを取ります。
判定は`exact`、`aligned`、`ambiguous`に分け、レビューqueueにはHazkey/Mozcの候補出力を
含めません。queueの提案単位は`source_reading_code_point`です。レビュー担当者は各行を承認、
境界修正、曖昧、無効のいずれかに確定します。

```sh
python3 tools/dictionary/prepare_mozc_hybrid_boundary_annotations.py \
  --corpus /path/to/formal-corpus.tsv \
  --lindera-tokenizer /path/to/lindera-boundary-tokenizer \
  --output /path/to/boundary-review-queue.jsonl \
  --summary-output /path/to/boundary-review-summary.json

python3 tools/dictionary/evaluate_mozc_hybrid_spike.py \
  --corpus /path/to/formal-corpus.tsv \
  --hazkey-results /path/to/hazkey-ab-probe-v5.jsonl \
  --mozc-results /path/to/mozc-ab-probe-v5.jsonl \
  --reviewed-boundaries /path/to/reviewed-boundaries.jsonl \
  --output /tmp/mozc-hybrid-reviewed-boundaries.json
```

既存のExcelレビューを引き継いで、複数のIMEチャンク許容経路を編集するローカルUIも使える。
Excelとpreannotation queueは読み取り専用の入力とし、編集内容は`--workspace`以下のsnapshot、
append-only event journal、LLM proposal journalへ保存する。初回起動後は、表示されたtoken付きの
loopback URLをブラウザで開く。

```sh
python3 tools/dictionary/serve_mozc_boundary_annotations.py \
  --queue build-grimodex/mozc-boundary-annotation-v1/preannotations.jsonl \
  --workbook /home/grimodex/Grimodex/temp/mozc-boundary-annotation-1360.xlsx \
  --workspace build-grimodex/mozc-boundary-annotation-ui
```

UIは読み位置だけの暫定経路と、読み区間・表層区間を対応付けた経路を区別する。自然な別解は
一つへ潰さず、複数の許容経路として保存する。`曖昧`だったExcel行は要裁定の下書きとして復元し、
既存の手入力境界を失わない。保存済みworkspaceはqueueとExcelのSHA-256へ束縛され、同じworkspaceの
二重起動、古いrevisionからの上書き、event journalの欠落を拒否する。

queueの読み自体が誤っているケースは、原データを変更せずUIの「読みを修正」から
`review.corrected_reading`として手修正できる。境界座標は修正後の`annotation_reading`を基準にするため、
読みを変更したrevisionでは古い許容経路を全て消去して状態を`pending`へ戻す。Lindera提案も元の読みに
対する参考表示だけとし、修正後には自動転写しない。LLM提案は生成開始時のreview revisionと実効読みの
SHA-256を記録する。生成中にrevisionが進んだ応答は採用せず、生成済み提案は実効読みが同じ間は
経路・状態・注記の保存後も保持する。読み変更前の提案だけを無効化する。export v3は
`source.reading`に不変の元読み、`source.annotation_reading`に境界評価で使う実効読み、
`review.corrected_reading`に人手修正値を分けて保持する。export上の経路境界は
`path_units.reading_boundaries = annotation_reading_code_point`と明示し、元読みの座標と混同しない。

LLM Top-3提案は任意で、認証済みCodex CLIのApp Serverを使い、ユーザーがボタンを押した場合だけ
1件ずつ生成する。UIの「LLM設定」はApp Serverの`model/list`から現在利用できるモデルと、
各モデルが公開するエフォート一覧を動的に取得する。「Codex既定モデル」は保存値`null`のままで、
`isDefault`モデルのエフォート候補を表示する。一覧外や将来の値に備え、モデルIDとエフォートの
カスタム入力も維持する。一覧取得に失敗してもレビューや保存済み設定は失わない。設定は
`--workspace`直下の`llm-settings.json`へ保存され、次回起動後も維持する。
提案は自動で正解にならず、人が下書きまたは許容経路として採用する。

App Serverは一時的な隔離`CODEX_HOME`で起動し、通常の設定、skills、MCPを読み込ませない。
ファイル認証では元の`auth.json`を複製せず、短命なaccess tokenとaccount IDだけを
`account/login/start`の`chatgptAuthTokens`としてApp Serverへ渡すため、OAuthのrefresh tokenは通常の
Codexだけが更新する。Codexのkeyringは元の`CODEX_HOME`へ束縛されるため、隔離経路では
`CODEX_ACCESS_TOKEN`または`cli_auth_credentials_store = "file"`を明示したloginを使う。
`auto`はkeyringかfileか安全に判別できないため、この経路ではkeyringとともにfail-closedで拒否する。各提案は
ephemeral thread、read-only sandbox、tools無効で実行する。応答形式は
`outputSchema`で制約した上で、読み連結、表層連結、境界範囲、Top-3の重複をsemantic validatorでも
検査する。Codexのrefresh token、認証ファイル、access token、未検証の生応答はreview labelへ混ぜない。

```sh
python3 tools/dictionary/serve_mozc_boundary_annotations.py \
  --queue build-grimodex/mozc-boundary-annotation-v1/preannotations.jsonl \
  --workbook /home/grimodex/Grimodex/temp/mozc-boundary-annotation-1360.xlsx \
  --workspace build-grimodex/mozc-boundary-annotation-ui \
  --codex-executable codex \
  --codex-model '<Codex model ID>' \
  --codex-timeout-seconds 120 \
  --codex-effort low \
  --llm-few-shots 10
```

`--codex-executable`の既定値は`codex`で、`--codex-model`は省略できる。`--codex-effort`の既定値は
対話的な補助用途向けの`low`である。これらのモデル・エフォート指定は新規workspaceの初期値であり、
保存済みの`llm-settings.json`がある場合はUIで最後に保存した値を優先する。「設定を保存」だけでも保存でき、
未保存の変更がある状態で「提案を取得」した場合は設定を先に保存する。生成中に別タブで設定が変わっても、
実行中の提案は開始時のモデル・エフォートを使い、変更は次回の提案から適用する。各提案journalには実モデル、
要求モデル、エフォートを記録する。設定更新もrevision付きで、別タブの古い値による上書きは409で拒否する。
`--codex-timeout-seconds`、`--codex-effort`、`--llm-few-shots`は提案経路だけに適用され、通常のレビュー・
保存・exportはCodex App Serverが利用できない場合も動作する。

全1,360件のレビューが完了するまでは境界精度を主張しません。完了後も、これは既知corpusの
診断でありformal adoption evidenceではありません。`--reviewed-boundaries`が測るのは
composition element単位の境界だけで、候補表層の変換品質ではありません。境界精度とsurface精度を
混同せず、レビューqueueのcode-point境界はABProbe v5のcomposition element境界と照合してから
境界専用JSONLへ移します。正式な採用判断には未見の文節ラベル付きholdoutを使います。
production既定値はH0のままです。

未見の文節ラベル付きholdoutは、正本と独立review approvalからlabel-free ABProbe入力を
content-addressed generationへ封印し、取得後に専用評価器でH0/H1/H2を比較します。
ABProbeへ渡すのはgeneration内の`probe-input.jsonl`だけです。現在は重複screen、バックエンドからの
物理的なラベル隔離、v5結果へのABProbe executable identity束縛、Pythonのload済みcode identity証明が
未実装なので、専用評価器は
全件の品質集計を残しつつ常に`inconclusive`とし、production H0を維持します。
品質は文節Top-1精度、文節正解時の条件付き表層Top-1精度、両方を要求するEnd-to-End精度へ分解し、
製品上の主指標にはEnd-to-Endを使います。H1/H2はMozc Top-1と同じ境界のHazkey候補だけを扱うため、
Hazkeyだけが正しい境界を返すケースは別の救済可能量として集計します。

```sh
python3 tools/dictionary/build_mozc_hybrid_segment_holdout_v1.py \
  --cases /path/to/reviewed/cases.jsonl \
  --approval /path/to/reviewed/approval.json \
  --output-root /path/to/sealed-holdouts

python3 tools/dictionary/evaluate_mozc_hybrid_segment_holdout.py \
  --generation /path/to/sealed-holdouts/sealed-segment-holdout-v1-sha256-... \
  --hazkey-results /path/to/hazkey-ab-probe-v5.jsonl \
  --mozc-results /path/to/mozc-ab-probe-v5.jsonl \
  --output /tmp/mozc-hybrid-segment-holdout.json
```

実server経路の初回Mozc表示、Space、stale破棄、PSS/RSS、候補ジャンプはopt-in
`GrimodexHybridProcessSpikeTests`で測定します。待ち時間はカンマ区切りで複数指定し、
単一の固定sleepを一般化しません。メモリ値はbefore/afterのendpoint snapshotであり、
peakまたは同時snapshotではありません。runtime counterは各測定windowの前後でworkerを
quiesceした差分を合算するため、warm-upとcomposition resetは含みません。

```sh
GRIMODEX_HYBRID_SPIKE_SERVER=/path/to/hazkey-server \
GRIMODEX_HYBRID_SPIKE_MOZC_HELPER=/path/to/fcitx5-grimodex-mozc-helper \
GRIMODEX_HYBRID_SPIKE_MOZC_DATA=/path/to/mozc.data \
GRIMODEX_HYBRID_SPIKE_PREFETCH_DELAYS_MS=0,25,100 \
GRIMODEX_HYBRID_SPIKE_ZENZAI_ENABLED=false \
GRIMODEX_HYBRID_SPIKE_OUTPUT=/tmp/mozc-hybrid-runtime.json \
  swift test --package-path hazkey-server \
    --filter GrimodexHybridProcessSpikeTests
```

Zenzai有効条件では`GRIMODEX_HYBRID_SPIKE_ZENZAI_ENABLED=true`、有効な
`FCITX5_GRIMODEX_ZENZAI_MODEL`、`--traits ZenzaiSupport`を指定します。

複数sessionでは、learnableなHazkey候補がreadyになった時点から、その候補windowの
commit/discard/cancelが完了するまでregistry-wideのcandidate-learning fenceを保持します。
fence中もMozc表示とMozc-only正式変換は共有Hazkey gateへ入らず、新しいHazkey投機だけを
待機させます。これにより、別sessionの投機requestが選択候補の同期学習を先取りして
socket処理全体を止める経路を防ぎます。代償として、候補windowまたはundo待ちが開いている間は
全sessionのHazkey先読みが一時停止します。activeなHazkey request自体はpreemptできません。

2026-07-15の診断結果と解釈は
[Mozc-first speculative hybrid spike](docs/spikes/mozc-hybrid-2026-07-15/README.md)
に記録しています。

主要なローカル検証:

```sh
ctest --test-dir build --output-on-failure
hazkey-server/scripts/swift-test.sh --traits ZenzaiSupport
python3 packaging/tests/package_contract_test.py
```

Swiftテスト用ラッパーは、固定済みのAzooKey依存を解決し、リポジトリで
管理する互換性・性能パッチをコンパイル前に適用します。CMakeとSwiftPMの
作業ディレクトリを共有する場合は`SWIFT_SCRATCH_PATH`を指定してください。
実server / Protocol v2境界のHazkey対Mozc比較は、検証済みread-only Mozc runtimeを
明示して実行するopt-in testです。環境変数と実測結果は
[fcitx-mozkey A/B follow-up](docs/spikes/fcitx-mozkey-followup-2026-07-14/README.md#最新-長寿命server--protocol-v2境界)
に固定しています。

## ライセンス

[MIT License](./LICENSE)。上流Hazkeyの著作権表示と派生元は
[NOTICE](./NOTICE.md)にも記載しています。
