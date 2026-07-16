# Mozc-first投機ハイブリッド・スパイク — 2026-07-15

この文書は、作業ツリー上で実施した診断スパイクの記録である。リリース判定の証拠ではなく、
診断用H1順位規則の採用を許可するものでもない。

## 実装したランタイム方針

- 編集中の入力と同期的な候補表示はMozcを使う。
- Hazkeyは、入力全体の先頭自然文節だけを、同一composition revisionに対する1回のリクエストとして
  バックグラウンドの直列ワーカーで準備する。
- Space時点で、そのリクエストと共有Hazkey gateの両方が完了している場合だけ結果を使用する。
  未完了の処理は論理的にキャンセルし、SpaceはMozc-onlyのまま待機しない。
- 編集、カーソル移動、文節操作、確定、キャンセル、ライフサイクル、辞書、設定、
  learning revision、secure domainの変更時には古い処理を無効化する。
- 候補移動を開始した後は、公開済みgenerationと候補順を変更しない。
- ランタイムH0はMozc Top-1と安定したTop-3を維持する。Hazkeyはその後ろに重複しない候補だけを
  補完する。学習対象は、実際に選ばれたHazkey由来候補だけとする。
- Hazkeyワーカー、学習、ユーザー辞書、設定、モデル再読込、履歴リセット、終了処理は、
  registry全体で共有する実行fenceを使用する。
- 学習可能なHazkey候補ウィンドウの準備が完了すると、commit/discard/cancelまでregistry全体の
  candidate-learning fenceを保持する。このfenceが止めるのは新しいHazkey投機だけで、
  Mozc表示とMozc-only正式変換は継続できる。

## オフライン品質評価

入力は、次の場所にある1,360件の対になったABProbe v3取得結果である。

`build-grimodex/hazkey-server/mozc-v2-b0-objective-20260715`

| 方針/バックエンド | Top-1正解数 | 正解率 | 改善 | 悪化 | 純増減 |
|---|---:|---:|---:|---:|---:|
| Hazkey | 909 / 1360 | 66.84% | — | — | — |
| Mozc | 809 / 1360 | 59.49% | — | — | — |
| ランタイムH0（`preserveMozcTop1`） | 809 / 1360 | 59.49% | 0 | 0 | 0 |
| 診断H1（`oneSidedConsensus`） | 808 / 1360 | 59.41% | 2 | 3 | -1 |

H1が昇格を検討したのは12/1,360件で、2件が改善、3件が悪化、7件は誤りのままだった。
入力に使ったABProbe v3は候補の表層文字列だけを記録し、`consuming_count`を持たない。
そのため現在の評価スキーマv3でも、このv3入力についてはランタイムの文節境界との同等性が
未確立であり、H1を実ランタイムへ
適用できないことを明示する。製品経路ではH0の出力順と由来別ルーティングを維持したまま、
境界対応のシャドーカウンターだけを収集できる。

MozcがTop-1を外した551件の内訳は次のとおり。

- HazkeyではTop-1に正解があるもの: 234件（42.47%）
- 両候補リストで正解がTop-1より下にあるもの: 0件
- HazkeyだけでTop-1より下に正解があるもの: 139件
- MozcだけでTop-1より下に正解があるもの: 6件
- 両方の観測Top-10に正解がないもの: 172件（31.22%）

理論上の改善余地は大きいが、今回評価した正解ラベル非依存の`oneSidedConsensus`規則は安全ではない。
改善数より悪化数が多いため、ランタイムの既定値はH0のままとする。

## 境界対応v4の結果

明示的に有効化したABProbe v4で再取得し、各アダプターの`segmentCandidates`経路を使って、
全1,360件の`{text, rank, consuming_count}`を記録した。ローカルの診断成果物は次の場所にある。

`build-grimodex/mozc-hybrid-boundary-v4-20260715`

| 文節境界の診断 | 件数 | 割合 |
|---|---:|---:|
| Hazkey Top-1の境界がMozc Top-1と一致 | 555 / 1360 | 40.81% |
| Hazkey Top-1の境界がMozc Top-1と不一致 | 805 / 1360 | 59.19% |
| 境界対応H1の昇格機会 | 6 / 1360 | 0.44% |

表層文字列だけで判定した6件の機会は、すべて境界条件も満たしており、境界判定による除外は
0件だった。内訳は`Docker`、`棚から`対`店から`、半角/全角の`4月`、および同じ先頭文節に対する
半角/全角の`2つの`が3件である。これは昇格機会の証拠であり、品質改善の証拠ではない。

封印済みcorpusの正解ラベルは入力全体を対象とする一方、v4が観測する候補は先頭文節だけである。
このため評価器は品質比較可能な件数を0件とし、全1,360件をTop-1、改善/悪化、理論上限、
誤り分類の集計対象から除外する。H1を有効化する前に、文節単位の正解ラベルを持つホールドアウト、
または明示的なcomposition span（入力対象範囲）とレビュー済みのtarget parity
（正解対象の一致）推論が必要である。

1回の反復・ウォームアップなしのデバッグ取得では、Hazkeyの中央値が194.33 ms、
P95が1029.81 ms、Mozcの中央値が2.74 ms、P95が14.08 msだった。これはアダプター経路の
診断値であり、製品UIの遅延ではない。初回表示とSpaceの時間には、次節のプロセス経路測定を使う。

## composition span付きv5の結果

ABProbe v5ではv4の候補構造を維持したまま、各ケースへ、ABProbeが構築した入力全体の
`composition_span={start,count,unit}`を明示した。paired Hazkey/Mozc結果では同じspanを要求し、
候補の`consuming_count`がspanを超える入力や、両runでspanが異なる入力は評価前に拒否する。
ローカルの診断成果物は次の場所にある。

機械可読レポートでは全比較可能診断行を`diagnostic_target_comparable`、formal v2品質カテゴリだけを
`formal_quality`へ分離し、後者から`protected`を除外する。

`build-grimodex/mozc-hybrid-composition-v5-20260715`

既存1,360件を各1回、ウォームアップなしで再取得した。ローカル実行時にはrelease buildを
使用したが、v5結果自体は実行バイナリのhashやbuild modeを保持しない。Mozc Top-1が入力全体の
spanを消費した120件だけは、先頭文節候補と既存の全文正解が同じ対象を表す。残り1,240件は
引き続き比較不能として除外した。比較時は表層文字列だけでなく`consuming_count`も一致させた。

| 全文span一致の全診断行 | Top-1正解数 | 正解率 | H0からの改善 | H0からの悪化 |
|---|---:|---:|---:|---:|
| Hazkey | 83 / 120 | 69.17% | — | — |
| Mozc | 99 / 120 | 82.50% | — | — |
| ランタイムH0 | 99 / 120 | 82.50% | 0 | 0 |
| 診断H1 | 100 / 120 | 83.33% | 1 | 0 |
| 診断H2（width guard） | 100 / 120 | 83.33% | 1 | 0 |

比較可能sliceは「Mozc Top-1が全文spanを消費すること」で選ばれるためMozc依存の選択バイアスがあり、
proper-noun 113件、colloquial 4件、protected 3件に偏っている。このためHazkeyとMozcの絶対精度差を
一般化しない。formal v2 corpus契約で品質対象外のprotected 3件を除く117件では、Hazkeyが
83/117（70.94%）、Mozc/H0が99/117（84.62%）、H1/H2が100/117（85.47%）だった。

全120診断行におけるMozc Top-1の21件の誤りは、Hazkey Top-1なら救済できるもの9件、
Hazkeyだけの下位候補に正解があるもの1件、Mozcだけの下位候補に正解があるもの5件、
両方の観測Top-10に正解がないもの6件だった。protectedを除く品質117件では誤り18件、
最後の分類が3件になる以外は同じである。バックエンドTop-1 oracleと候補union oracleは、
全120診断行で108/120・114/120、品質117件で108/117・114/117だった。

H1の境界対応昇格機会6件のうち、全文span一致sliceに入ったのは`Docker`の1件だけであり、
`ドッカー`から`Docker`への救済になった。残り5件の品質は、このsliceからは判定しない。

H2 `one-sided-consensus-width-guard`は、H1の条件を満たした後、Hazkey/Mozc Top-1の差が
全角ASCIIと半角ASCII、または全角空白と半角空白だけなら昇格を抑制する。一般のNFKC互換文字は
畳み込まない。全1,360件では、`４月`対`4月`の1件と`２つの`対`2つの`の3件を抑制し、
昇格機会を6件から2件へ減らした。残るのは`Docker`と`棚から`対`店から`である。
全文span一致sliceでは抑制対象がなく、H2の全120診断行と品質117件の結果はH1と同じだった。
抑制4件はすべて全文span比較不能であり、outcome比較可能0件・比較不能4件だった。したがって、
この4件について「悪化を防いだ」または「救済を失わなかった」とは主張しない。残る昇格2件は、
`Docker`の救済1件と、全文span比較不能の1件である。

v5取得のアダプター診断値は、Hazkeyが中央値16.73 ms・P95 73.08 ms、Mozcが中央値
1.45 ms・P95 8.96 msだった。ローカルでrelease buildを使った取得の付随値だが、そのbuild identityは
結果へ束縛されていないため、正式な性能証拠や製品UI遅延ではない。

この結果はtarget parityが成立する診断sliceの証拠にはなるが、H1/H2の採用証拠にはしない。
既公開corpusを規則策定後に再利用しており、比較可能ケースも固有名詞へ強く偏っているためである。
ランタイム既定値はH0のままとし、昇格を有効化する前に、規則を固定してから取得した未公開の
文節ラベル付きholdoutが必要である。

## 既知1,360件の境界アノテーション診断

既知corpusでも、全文の`expected`表層とは別に先頭文節境界を人手レビューすれば、HazkeyとMozcの
境界傾向を診断できる。1,360件を一から手で区切る負担を減らすため、Linderaの組み込みUniDicによる
プリアノテーションを作り、その後に全件を人手で確定する。この経路は候補表層の正誤を評価するものではなく、
未見holdoutを置き換えるものでもない。

プリアノテーションでは、各ケースの`expected`に`|`区切りの許容表層が複数ある場合、一つだけを代表値に
選ばず全てをLinderaで解析する。各tokenの読みをcorpusのreadingへ対応付け、全表層の先頭境界が
`source_reading_code_point`単位で一致する場合だけconsensusを提案する。提案根拠は次の3状態で保持する。

- `exact`: UniDicのtoken読みとcorpusのreadingが完全一致し、先頭境界を一意に対応付けられた。
- `aligned`: token読みとcorpusのreadingを整列することで先頭境界を一意に対応付けられた。
- `ambiguous`: 複数表層の境界が一致しない、または一意に対応付けられないため自動確定できない。

レビュー時にHazkey/Mozcの候補出力を見せると、正解境界が比較対象の出力へ引っ張られるため、
preannotationとreview成果物には両バックエンドの候補を含めない。レビュー担当者は各行を次のいずれかで
確定する。

1. 提案境界を承認する。
2. source readingのcode point単位で境界を修正する。
3. 解釈が一意でない行を曖昧として保留する。
4. corpus入力またはラベルとして成立しない行を無効とする。

準備は次のコマンドで行う。Lindera tokenizerは
`tools/dictionary/lindera_boundary_tokenizer`からbuildした実行ファイルを指定する。

```sh
python3 tools/dictionary/prepare_mozc_hybrid_boundary_annotations.py \
  --corpus /path/to/formal-corpus.tsv \
  --lindera-tokenizer /path/to/lindera-boundary-tokenizer \
  --output /path/to/boundary-review-queue.jsonl \
  --summary-output /path/to/boundary-review-summary.json
```

今回のローカル生成では1,360件を全件処理し、許容表層の全alternativeを含む15,494 tokenを解析した。
45件が複数の許容表層を持ち、そのうち3件は表層間で提案境界が一致しなかった。レビューqueueの
信頼度内訳は`exact` 738件、`aligned` 334件、`ambiguous` 288件で、canonical JSONLのSHA-256は
`sha256:3753789ee6545512a90d78e7f81dde571cf05d25b5d9be9539d0f9141288d594`である。この内訳は
プリアノテーションの機械的な確信度であり、文節境界の正解率ではない。

### 許容経路UIへの移行

単一の「正解分割」では、国語的な文節とIMEで操作しやすいチャンクの両方が自然なケースを表せない。
そのため、新しいレビュー正本は一列の境界ではなく、読み区間と表層区間の組を並べた複数の
`acceptable_paths`として扱う。読み境界だけ確定して表層対応が未確定の経路は`reading_only`、
両側を対応付けた経路は`aligned`として分ける。特定IMEで実際に観測したsegment列や操作履歴は、
人手で許容した経路とは別の`observations`として将来追加し、観測されたことだけで正解にはしない。

2026-07-16時点のExcel中間成果物を読み取り専用で移行した結果は次のとおりである。

| 項目 | 件数 |
|---|---:|
| 全ケース | 1,360 |
| 人手レビュー済み | 841 (61.8%) |
| 承認または修正から復元した作業中経路集合 | 828 |
| 未確認 | 519 |
| 曖昧・要裁定 | 5 |
| 無効入力 | 8 |

`曖昧`5件には既存の「レビュー済み分割」が入っていたため、消去せず`draft`経路として復元した。
Linderaと異なる人手修正もLLM few-shotから除外せず、読み境界の確定例として渡す。一方、表層境界が
未確認なら`aligned_chunks=null`と明示し、LLMへ人手確定済みの表層対応だと誤認させない。複数の
許容経路があるケースは最大3経路を同じfew-shot例に残し、要裁定ケースはgold例から除外する。

ローカルUIはPython標準ライブラリのloopback serverと静的HTML/CSS/JavaScriptで実装した。元Excelと
queueは変更せず、`review.snapshot.json`、`review.events.jsonl`、`proposals.jsonl`をsidecar workspaceへ
保存する。revision競合、同一workspaceの二重起動、queue/ExcelのSHA-256不一致、event revisionの欠落を
fail-closedで拒否する。exportするreview JSONLとmanifestは同じlock世代から生成し、manifestの
`reviewed_paths_sha256`が必ず同じJSONLを指す。

corpus側の読みが誤っている行は、無効入力へ落とすだけでなく、UIから修正読みをsidecar reviewへ保存できる。
このときqueueの`source.reading`とrow hashは監査入力として不変に保ち、境界編集、few-shot、LLM target、
semantic validationは`corrected_reading ?? source.reading`を実効読みとして使う。読み変更時には、文字数が
同じでも境界の意味が変わり得るため既存経路を再利用せず、`pending`かつ空経路のrevisionを一度保存してから
新しい境界を付け直す。Linderaプリアノテーションは元読みにだけ適用可能と明示する。LLM提案は開始時の
review revisionと実効読みSHA-256を記録する。生成中にrevisionが変わった応答はjournalへ書かず
409として破棄するが、生成済み提案の適用可否は実効読みSHA-256で判定する。そのため、経路の追加や
状態・注記の保存でreview revisionが進んでも提案は保持し、実効読みを変更した場合だけ一覧から除外する。
review exportはv3へ上げ、元読み、実効読み、人手修正値を
それぞれ`source.reading`、`source.annotation_reading`、`review.corrected_reading`として分離した。
経路の`reading_boundaries`は`annotation_reading_code_point`単位であることを`path_units`へ明示し、
元読み用の`source_reading_code_point`を修正読みの境界へ流用しない。

LLM Top-3は補助提案に限定する。認証済みCodex CLIのApp Serverをユーザー操作時だけ起動し、1件ずつ
オンデマンドで生成する。モデルIDを省略した場合はCodexの既定モデルを使う。モデルIDとエフォートは
アノテーションUIから変更し、workspace固有の`llm-settings.json`へrevision付きで永続化できる。
UIはApp Serverの`model/list`を最後のページまで取得し、サーバーが返すモデル順と各モデルの
`supportedReasoningEfforts`順をそのまま表示する。エフォートは固定enumにせず、一覧にない保存済み値や
将来の値のためにモデル・エフォートともカスタム入力を残す。一覧取得の成否は設定のdirty状態や
settings revisionを変えず、取得失敗時も前回の一覧と現在の入力値を保持する。
App Serverには一時的な
隔離`CODEX_HOME`を渡し、通常の設定、skills、MCPは読み込ませない。ファイル認証では元の`auth.json`を
コピーせず、access tokenとaccount IDだけを`account/login/start`の`chatgptAuthTokens`として渡し、
refresh tokenの更新権限を通常のCodexに一本化する。
keyring credentialは元の`CODEX_HOME`の識別子へ束縛されており隔離homeから安全に共有できないため、
この経路は`CODEX_ACCESS_TOKEN`または`cli_auth_credentials_store = "file"`を明示したloginを要求する。
`auto`はkeyringかfileか安全に判別できないため、keyringとともにfail-closedで拒否する。各提案は
ephemeral thread、read-only sandbox、tools無効で実行し、`outputSchema`とsemantic validatorの両方で
応答を検査する。提案は専用journalへ保存するが、自動で許容経路へ昇格せず、人が採否を決める。

経路確認時の単発計測では、通常の`CODEX_HOME`が17,315 input tokenだったのに対し、認証以外の通常設定を
除いた隔離`CODEX_HOME`は7,713 input tokenで、55.5%減少した。隔離経路のturn時間は4.049秒、App Serverの
起動と終了を含むend-to-end時間は4.638秒だった。ただし単発計測なので、隔離によるlatency優位はまだ
断定できない。1件ごとの補助提案には利用できる一方、1,360件を一括生成する用途には起動・推論時間が重く、
bulkプリアノテーション経路としては採用しない。

2026-07-16の最終実機確認では、file-backed ChatGPT認証を`chatgptAuthTokens` RPCで隔離App Serverへ
渡し、既定モデル`gpt-5.6-sol`で「きょうはあめです」から`きょうは｜あめです`を3.779秒で生成した。
隔離homeに`auth.json`は作られず、元の`auth.json`のSHA-256も前後で一致した。さらに1,360件queueの
`v2-technical-0002`を実workspace契約で生成し、読み境界`[6, 14]`、表層境界`[6, 12]`の1経路が
semantic validatorと提案journalを通過した。これは経路の動作確認であり、境界精度の評価値ではない。

起動オプションは`--codex-executable`、`--codex-model`、`--codex-timeout-seconds`、`--codex-effort`、
`--llm-few-shots`とする。`--codex-executable`の既定値は`codex`、`--codex-model`は任意であり、
`--codex-effort`の既定値は`low`とする。CLIのモデル・エフォートは新規workspaceの初期値にだけ使い、
保存済みworkspaceではUI設定を優先する。設定保存とレビュー保存のdirty状態は分離し、未保存設定で
提案を要求した場合は設定を先に保存する。複数タブの古いsettings revisionは拒否し、利用者の入力を
保持したまま再確認を求める。生成開始後の設定変更は実行中の提案へ混ぜず、次回生成から反映する。
提案journalにはApp Serverが返した実モデルに加え、開始時の要求モデルとエフォートを記録する。
Codex App Serverを利用できない場合も通常のレビュー経路は維持する。

ブラウザ検証では、Excel由来の841/1,360件を表示し、長文フィルター233件、要裁定5件、長文ケースの
読み境界修正、複数行注記、autosave、revision付き再読込、review JSONLとmanifestの書き出しを確認した。
検証用workspaceだけを変更し、元Excelは変更していない。実作業用workspaceは
`build-grimodex/mozc-boundary-annotation-ui`へ841件の初期状態で作成した。

承認または修正によって全1,360件の境界が確定し、曖昧・無効行が解消されるまでは、境界精度を集計・主張
しない。preannotation queueの単位は`source_reading_code_point`だが、legacy TSVを読むABProbeが作る
境界単位はSwift `Character`ごとの`composition_element`である。複数scalarから成るgraphemeでは両者が
一致しない可能性があるため、単位名だけを置き換えてはならない。各レビュー境界を対応するABProbe v5の
composition element境界と照合した後、確定JSONLを
`{schema,id,span:{start,count,unit:"composition_element"}}`の境界専用schemaで作る。paired結果との評価は
次のとおりである。

```sh
python3 tools/dictionary/evaluate_mozc_hybrid_spike.py \
  --corpus /path/to/formal-corpus.tsv \
  --hazkey-results /path/to/hazkey-ab-probe-v5.jsonl \
  --mozc-results /path/to/mozc-ab-probe-v5.jsonl \
  --reviewed-boundaries /path/to/reviewed-boundaries.jsonl \
  --output /tmp/mozc-hybrid-reviewed-boundaries.json
```

評価器はラベルのexact schema、重複のない全corpus ID coverage、v5のcomposition span内に収まる
`composition_element`境界をfail-closedで要求する。全件とformal-qualityカテゴリを別の分母で集計し、
Hazkey/Mozc Top-1境界を「両方正しい」「Mozcだけ正しい」「Hazkeyだけ正しい」「両方誤り」の4群へ分け、
Mozc対Hazkeyの境界精度差も記録する。ただし表層ラベルを参照しないため、ここから変換surfaceの正解率や
End-to-End品質を推論してはならない。surface評価には、境界と許容表層を独立にレビューしたラベルが必要である。

この1,360件は規則策定前から既知のcorpusなので、レビュー完了後の値もdiagnostic-onlyであり、formal adoption
evidenceにはしない。H1/H2の採用判断には未見holdoutを使い、それまではproduction H0を維持する。

## 未見・文節ラベル付きholdout経路の実装スパイク

既知1,360件から新しい採否スコアを作るのではなく、H0/H1/H2を固定した後に人手で作成・レビューする
未見holdout用の入出力経路を追加した。この段階では実holdoutケースを作成しておらず、品質値も
取得していない。production既定値は引き続きH0である。

正本`cases.jsonl`は、ケースID、品質カテゴリ、重複確認用family ID、明示的なcomposition element列、
レビュー済み先頭文節targetを持つ。各elementは`{text,input_style:"direct"}`で表し、targetは
`composition_element`単位の`{start:0,count}`と、許容するNFC表層の集合を持つ。文字数やSwiftの
`Character`数から入力境界を推定しないため、複数scalarから成る1 graphemeも1個の明示elementとして
ABProbeへ渡せる。

封印ビルダーは、独立したauthor/reviewer、正本のexact-byte SHA-256、blind収集attestation、カテゴリ別
件数、H2昇格機会の事前下限、次の実装・成果物identityを承認ファイルへ要求する。

- H0/H1/H2 policy ID
- product source revision
- holdout専用評価器と共有H0/H1/H2評価器のSHA-256
- ABProbe実行ファイルのSHA-256
- Hazkey/Mozc両resourceのfingerprintとMozc prepared generation
- Top-K、warm-up、iteration、learning無効

承認済み正本から、ラベルを含まない`probe-input.jsonl`と、`segment-labels.jsonl`を決定論的に分離し、
正本・承認・両派生ファイル・manifestを単一のcontent-addressed generationへ封印する。ファイルは0444、
generationは0555とし、公開前後のexact bytes、inode、link数、ファイル集合を検証する。ABProbeは既存TSV
との互換性を維持しつつ、このlabel-free JSONL schemaを自動判別し、明示element列をそのまま
`CompositionInput`へ渡してv5の`composition_span.count`を記録する。

評価器は、Hazkey/Mozc両runのケース順、probe SHA、source revision、取得パラメータ、resource identity、
Mozc generation、2個の評価器source SHAを封印時の値と照合する。holdout専用経路では、ABProbe v5
各行のroot、resource、corpus、候補、composition span、measurementとその子objectについて、未知fieldと
重複JSON keyを拒否する。品質判定では、候補の生の表層文字列と、
レビュー済みNFCラベルを完全一致で比較し、さらに`consuming_count`がレビュー済み文節長と一致することを
要求する。幅正規化はH2の昇格抑制規則だけに使い、正解判定には使わない。Mozcの文節境界が誤っている
ケースも除外せず誤りとして数えるため、全ケースが同じレビュー済み分母に入る。

この完全一致は製品上のEnd-to-End主指標だが、失敗原因を分けるため、同じ分母を次の軸でも集計する。

1. 文節精度: Top-1の`consuming_count`がレビュー済み文節長と一致する割合
2. 条件付き変換精度: 文節が正しいケースに限定した、生のTop-1表層の正解率
3. End-to-End精度: 文節長と表層の両方が正しい割合

Top-Kについても、正しい境界を持つ候補の有無と、その中に正解表層があるEnd-to-End hitを分ける。

境界誤りは、レビュー済み境界より前で切る場合と後まで消費する場合に分け、件数とelement差を記録する。
Hazkey/MozcのTop-1境界は「両方正しい」「Mozcだけ正しい」「Hazkeyだけ正しい」「両方誤り」の4群に
分類する。特に「Mozc境界誤り・Hazkey境界正解」について、境界切替の候補件数と、Hazkey表層まで正しく
実際にEnd-to-End救済できる件数を分ける。

現在のH1/H2は、Mozc候補がある場合、Mozc Top-1と同じ`consuming_count`のHazkey候補だけを昇格対象に
する。このためH1/H2の文節精度は原則H0と同じであり、「Mozcだけ境界を外し、Hazkeyは正しい」ケースを
救えない。H0からの改善・悪化は境界起因と同一境界内の表層起因に分解して記録し、Hazkeyの境界優位が
確認できた場合に限り、H2とは別の保守的な境界切替規則を検討する。

ただし、現在のgeneration内ではlabel-free probeと正解ラベルが論理的に分離されているだけで、別権限・
別mountなどによる物理的なバックエンド隔離はまだない。また、既存v2 corpusおよび補助テスト群との
重複screenも未実装であり、ABProbe v5結果自身は実行ファイルのSHA-256を保持しない。Python評価器の
source file SHAは照合するが、すでにloadされたcode objectがそのsourceから生成されたことも証明できない。
この4点を機械可読blockerとして残す。承認済みケースについて`human_collection_attested=true`、
`new_holdout_required=false`にはなるが、評価結果は常に`formal_authorized=false`かつ
`decision.status=inconclusive`である。事前に固定したH2昇格機会の下限を満たさない場合も結果全体を
破棄せず、追加blockerを記録してH0を維持する。H2機会数は、manifestで固定した品質カテゴリかつ
target比較可能なケースだけで判定し、`protected`など診断専用カテゴリでは下限を満たしたことにしない。

封印は次のように行う。

```sh
python3 tools/dictionary/build_mozc_hybrid_segment_holdout_v1.py \
  --cases /path/to/reviewed/cases.jsonl \
  --approval /path/to/reviewed/approval.json \
  --output-root /path/to/sealed-holdouts
```

出力generationの`probe-input.jsonl`だけを、承認時に固定した同一の`source-ref`、Top-K、warm-up、
iterationでHazkey/MozcそれぞれのABProbe v5へ入力する。取得後の評価は次のとおりである。

```sh
python3 tools/dictionary/evaluate_mozc_hybrid_segment_holdout.py \
  --generation /path/to/sealed-holdouts/sealed-segment-holdout-v1-sha256-... \
  --hazkey-results /path/to/hazkey-ab-probe-v5.jsonl \
  --mozc-results /path/to/mozc-ab-probe-v5.jsonl \
  --output /path/to/segment-holdout-evaluation.json
```

## 製品経路の時間・メモリ測定

明示的に有効化したプロセス測定で、実際のデバッグserver、固定Mozc helper/data、Unix socket、
Protocol v2を起動した。12件を各1回実行し、Mozc表示の応答からSpaceまでの猶予を
0/25/100 msに設定した。以下のカウンターは、ワーカーの静止を待って取得した、36個の測定
ウィンドウの前後差分を合計したものである。ウォームアップとリセット時の後処理は含まない。
両測定とも有効なモデルを参照できる同一の`ZenzaiSupport`バイナリを使い、プロファイルの
Zenzai有効フラグだけを変更した。

### Zenzai無効

| 先読み猶予 | Mozc初回表示の中央値 | Space時の中央値 | Mozc基準候補にない表層文字列を含むウィンドウ | Top-1変更 | 候補ジャンプ |
|---:|---:|---:|---:|---:|---:|
| 0 ms | 7.71 ms | 4.92 ms | 0 / 12 | 0 | 0 |
| 25 ms | 7.45 ms | 5.28 ms | 1 / 12 | 0 | 0 |
| 100 ms | 8.00 ms | 5.89 ms | 3 / 12 | 0 | 0 |

測定カウンターは`prefetch_started=36`、`prefetch_ready=7`、
`formal_ready_consumed=7`、`formal_deadline_miss=29`、
`stale_discarded=29`、`late_completion_discarded=29`、
`hazkey_requests=36`、`merged_requests=4`、`boundary_mismatch=3`、
`hazkey_failure=0`だった。シャドーH1は準備完了結果を7回評価し、昇格機会は0件、
Hazkey Top-1の境界不一致による棄却は3件だった。Hazkeyの合計時間は5.1859秒で、
1リクエストあたり約144.1 msだった。

### Zenzai有効

ローカルの69 MiBモデルを有効にし、GGMLバックエンドを設定して測定した。

| 先読み猶予 | Mozc初回表示の中央値 | Space時の中央値 | Mozc基準候補にない表層文字列を含むウィンドウ | Top-1変更 | 候補ジャンプ |
|---:|---:|---:|---:|---:|---:|
| 0 ms | 6.10 ms | 4.51 ms | 0 / 12 | 0 | 0 |
| 25 ms | 7.53 ms | 5.69 ms | 0 / 12 | 0 | 0 |
| 100 ms | 7.38 ms | 5.19 ms | 1 / 12 | 0 | 0 |

測定カウンターは`prefetch_started=36`、`prefetch_ready=2`、
`formal_ready_consumed=2`、`formal_deadline_miss=34`、
`stale_discarded=34`、`late_completion_discarded=34`、
`hazkey_requests=36`、`merged_requests=1`、`boundary_mismatch=1`、
`hazkey_failure=0`だった。シャドーH1は準備完了結果を2回評価し、昇格機会は0件、
Hazkey Top-1の境界不一致による棄却は1件だった。Hazkeyの合計時間は8.9871秒で、
1リクエストあたり約249.6 msだった。

### エンドポイントメモリ

以下は測定開始前後のエンドポイントスナップショットであり、ピーク値でも、同時点で取得した
スナップショットでもない。

| モード/時点 | Server RSS / PSS | Helper RSS / PSS | 合計PSS |
|---|---:|---:|---:|
| Zenzai無効/開始前 | 177,532 / 61,159 KiB | 20,392 / 16,503 KiB | 77,662 KiB |
| Zenzai無効/終了後 | 182,608 / 66,075 KiB | 23,336 / 19,447 KiB | 85,522 KiB |
| Zenzai有効/開始前 | 300,772 / 160,312 KiB | 20,316 / 16,411 KiB | 176,723 KiB |
| Zenzai有効/終了後 | 316,728 / 176,204 KiB | 23,240 / 19,335 KiB | 195,539 KiB |

ローカルレポートは`build-grimodex/hybrid-runtime-spike.json`（Zenzai有効）と
`build-grimodex/hybrid-runtime-spike-no-zenzai.json`（Zenzai無効）である。

## 2セッション競合の結果

決定論的なバリアテストでは、registryと同じ実行gateと直列executorを共有する2個の
ハイブリッド変換器を使った。セッションAが学習可能な準備完了候補ウィンドウを保持している間、
セッションBはHazkey変換器へ入れない。一方、BのMozc表示、リアルタイム候補、
Mozc-only正式変換のフォールバックは完了できる。Aが学習をcommitすると、Bの待機中の投機処理が
Hazkeyへ入るより先に、基底Hazkeyのcommitが実行されたことを確認した。

主要なfence解放経路として、discard、正式変換時のMozc失敗、文節境界不一致、
複数文節の部分巻き戻し、文節サイズ変更、候補変換、学習不能フォールバック、
learning revision不一致、secure purgeを検証した。registryの保守処理と終了処理は、
無効化と排他的なHazkey変更を囲む明示的なadmission fenceを使用する。

## 解釈

この単一セッション測定では、正式変換がHazkey処理の完了を待つことはなく、候補ジャンプも
発生せず、H0がMozc Top-1を変更することもなかった。先頭文節だけを準備完了時に公開する方式は、
後続文節のリクエストが、同じセッションで直前に公開した候補の学習を妨げることも防ぐ。

ボトルネックは引き続き準備完了率である。100 msの猶予でも、別途取得したMozc基準候補にない
正規化済み表層文字列を含んだのは、Zenzai無効で3/12ウィンドウ、Zenzai有効で1/12ウィンドウ
だけだった。このウィンドウ単位の指標からは、バックエンド由来や個別の追加候補数は分からない。
後から候補を追加するだけでは一発変換のTop-1は改善しない。全文候補を使ったv3評価ではH1が
純減1件、target parityが成立するv5の全120診断行では純増1件、protectedを除くformal-quality
117件でも純増1件だった。採否に使う品質分母は117件であり、120件はprotected 3件を含む
診断値である。この差は候補scopeが異なる評価を混ぜて採否を決められないことを示す。
v5 sliceは既知corpusかつ固有名詞へ偏るため、純増結果からH1/H2を有効化しない。

この指標は、正式変換ウィンドウに、別途取得したMozc基準候補にはないNFC正規化済みの
表層文字列が1個以上含まれる場合だけ加算する。候補の並べ替え、重複、Unicode正規化上
等価な表記だけでは加算しない。

スパイク後の2セッションバリア測定では、共有gateによるhead-of-line blockingの危険を再現し、
candidate-learning fenceを追加した。学習可能なHazkey結果の準備が完了すると、候補ウィンドウと
staged learningの判断が解決するまで、他セッションの待機中の投機処理はHazkeyへ入れない。
その間もMozc表示とMozc-only正式変換は利用でき、保守、セキュア入力、終了処理の各経路は
それぞれadmission fenceを取得する。

これにより、非同期ジャーナルを追加せずに同期的な学習の永続性を維持できる。代償として、
学習可能な候補ウィンドウまたはundo判断が残っている間は、全セッションのHazkey先読みが停止する。
すでに実行中のHazkeyリクエストは途中で中断できない。全セッション停止が問題になる場合は、
プロセス分離が次のスケーラブルな選択肢になる。Top-1昇格を有効にするには、別途レビューした
新しいホールドアウトも必要である。次の候補規則は、全角/半角だけの昇格を除くH2だが、
productionへ入れる前に新規holdoutで救済・悪化を再測定する。
