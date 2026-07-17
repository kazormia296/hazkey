# Mozc-first投機ハイブリッド・スパイク — 2026-07-15

この文書は、作業ツリー上で実施した診断スパイクの記録である。リリース判定の証拠ではなく、
診断用H1順位規則の採用を許可するものでもない。

2026-07-17の方針更新では、次の主比較対象をMozc単独、Hazkey+Zenzai単独、
Mozc-firstでHazkey+Zenzai候補を補完するランタイムH0の3系統へ変更した。本文中のH1/H2結果は
過去の診断記録として残すが、H1/H2の合意規則は次の採否対象ではない。最新プロトコルは
「Mozc/Hazkey+Zenzai/H0の品質評価方針」を正とする。

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
キュージョブを`stale`として破棄するが、生成済み提案の適用可否は実効読みSHA-256で判定する。そのため、経路の追加や
状態・注記の保存でreview revisionが進んでも提案は保持し、実効読みを変更した場合だけ一覧から除外する。
review exportはv3へ上げ、元読み、実効読み、人手修正値を
それぞれ`source.reading`、`source.annotation_reading`、`review.corrected_reading`として分離した。
経路の`reading_boundaries`は`annotation_reading_code_point`単位であることを`path_units`へ明示し、
元読み用の`source_reading_code_point`を修正読みの境界へ流用しない。

LLM Top-3は補助提案に限定する。認証済みCodex CLIのApp Serverをユーザー操作時だけ起動し、現在の1件、
または一覧で人が明示的に選択した複数件をオンデマンドで生成する。複数件はサーバープロセス内の有界FIFOへ
積み、単一ワーカーで直列に処理する。取得待ち・生成中でも別ケースの「提案を取得」は押せるため、レビューを
止めずに後続ジョブを追加できる。同じcase、review revision、実効読みSHA-256、LLM settings revisionの
組が待機中または生成中なら重複ジョブを作らない。モデルIDを省略した場合はCodexの既定モデルを使う。
モデルIDとエフォートは
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
応答を検査する。長文の下位案だけで読みや表層の転記を誤った場合は、その候補だけを除外して完全検証済みの
上位案を返す。除外件数と理由はproposal journalへ残し、全候補が不正な場合だけfail-closedで拒否する。
提案は専用journalへ保存するが、自動で許容経路へ昇格せず、人が採否を決める。キューへ積む時点でreview
revision、実効読みSHA-256、LLM settings revisionを固定し、実行開始までに入力や設定が変わったジョブは
LLMを呼ばず`stale`とする。1件の失敗で後続ジョブは止めない。キューと完了・失敗履歴はプロセス内だけに持ち、
サーバー再起動時の未完了ジョブは再開しない。一方、生成が完了してjournalへ書かれた提案は従来どおり永続化する。

経路確認時の単発計測では、通常の`CODEX_HOME`が17,315 input tokenだったのに対し、認証以外の通常設定を
除いた隔離`CODEX_HOME`は7,713 input tokenで、55.5%減少した。隔離経路のturn時間は4.049秒、App Serverの
起動と終了を含むend-to-end時間は4.638秒だった。ただし単発計測なので、隔離によるlatency優位はまだ
断定できない。人が作業対象として選んだ少数件の補助提案には利用できる一方、1,360件全体を自動投入する用途には
起動・推論時間が重く、bulkプリアノテーション経路としては採用しない。今回の一括取得は、一覧で人が選んだ
ケースをレビュー作業中に先読みするための操作であり、corpus全件の無人生成とは分ける。

2026-07-16の最終実機確認では、file-backed ChatGPT認証を`chatgptAuthTokens` RPCで隔離App Serverへ
渡し、既定モデル`gpt-5.6-sol`で「きょうはあめです」から`きょうは｜あめです`を3.779秒で生成した。
隔離homeに`auth.json`は作られず、元の`auth.json`のSHA-256も前後で一致した。さらに1,360件queueの
`v2-technical-0002`を実workspace契約で生成し、読み境界`[6, 14]`、表層境界`[6, 12]`の1経路が
semantic validatorと提案journalを通過した。これは経路の動作確認であり、境界精度の評価値ではない。

起動オプションは`--codex-executable`、`--codex-model`、`--codex-timeout-seconds`、`--codex-effort`、
`--llm-few-shots`とする。`--codex-executable`の既定値は`codex`、`--codex-model`は任意であり、
`--codex-effort`の既定値は`low`とする。CLIのモデル・エフォートは新規workspaceの初期値にだけ使い、
保存済みworkspaceではUI設定を優先する。設定保存とレビュー保存のdirty状態は分離し、未保存設定で
提案を要求した場合、キューが空なら設定を先に保存する。すでにキューが動作中なら未保存設定は保持したまま、
現在の保存済みsettings revisionで後続ジョブを追加する。複数タブの古いsettings revisionは拒否し、利用者の
入力を保持したまま再確認を求める。設定変更はキューが空になってから保存し、実行中の提案へ混ぜない。
提案journalにはApp Serverが返した実モデルに加え、開始時の要求モデルとエフォートを記録する。
Codex App Serverを利用できない場合も通常のレビュー経路は維持する。

ブラウザ検証では、Excel由来の841/1,360件を表示し、長文フィルター233件、要裁定5件、長文ケースの
読み境界修正、複数行注記、autosave、revision付き再読込、review JSONLとmanifestの書き出しを確認した。
複製経路はケースごとの`path_id`で選択を復元する。autosave後も編集ハンドラが参照する経路オブジェクトを
保存応答と同一内容へ更新し、続けて行った分割変更が古いオブジェクトだけに残らないようにした。実ブラウザで
「複製、autosave、分割変更、保存、別ケースへ移動、復帰」を通し、複製経路の選択と両側の境界が保持されることを確認した。
2026-07-17には、一覧の表示中3件を選択して一括取得し、投入直後の`待機 3`、待機中も有効な単体取得ボタン、
`case-1`、`case-2`、`case-3`のFIFO完了、各ケースの提案件数反映を実ブラウザで確認した。LLM設定とモデル一覧の
再取得は実行中の設定混在を避けるためキューが空になるまで固定し、別タブや古い画面からの設定PATCHもサーバーで
拒否する。一方、ケース移動、レビュー編集、後続ジョブの追加は継続できる。旧単体提案POSTも同じキューへの
single-case aliasへ変更し、状態APIに現れない生成がFIFOへ割り込まないようにした。同一queue revisionのpollでは
一覧DOMを再構築せず、チェックボックスやキーボードフォーカスを維持する。選択はフィルターをまたいで保持し、
投入応答を待つ間に追加した別ケースの選択も消さない。バックグラウンド完了時は表示中ケースの提案だけを再取得し、
未保存の経路下書きは置き換えない。
検証用workspaceだけを変更し、元Excelは変更していない。実作業用workspaceは
`build-grimodex/mozc-boundary-annotation-ui`へ841件の初期状態で作成した。

## 許容経路レビュー完了と先頭チャンク評価 — 2026-07-17

人手レビュー後の全1,360件について、`reviewed_once=true`、要裁定0件、許容経路ありを一括検証し、
`open`から`closed`へ一つのbatchとして確定した。確定処理は1,360件分の監査イベントをappendしてから
snapshotとexportを各1回だけ更新し、再実行では変更0件となる。確定exportは次の状態である。

- `path_set_statuses={"closed":1360}`、`complete=true`
- 許容経路1,633本。1経路1,164件、2経路119件、3経路77件
- 修正読み10件、要裁定0件
- 作業履歴として残ったdraft 5本は保持するが、許容targetには含めない
- reviewed paths SHA-256:
  `sha256:573bfd3b3743d4222edf65ae6167ac2e61157003c899015ce52a1dc7993971a0`

単一の先頭spanへ変換すると、先頭境界自体に複数の許容値がある160件を壊す。そのため、確定exportから
修正後の読みを1 code pointずつ明示的なdirect `composition_element`へ変換したlabel-free probeと、
許容先頭span集合・許容先頭表層pairを別ファイルへコンパイルした。生成内訳は、重複除去後の許容先頭span
1,543個、許容先頭表層pair 716個、全経路aligned 533件、部分aligned 3件、reading-only 824件である。
部分aligned 3件はsurface分母から除外する。

generationは人手export、annotation manifest、probe、target、manifestの5ファイル専用とした。評価器は
固定ファイル集合をno-followで取得し、元の人手exportから全生成物を再導出してbyte一致を要求する。
probe SHA-256は
`sha256:993e5b27ece2e2b772fdb5c6c832aed51a3f97db2916ec3add5b1e5d888ca35c`、
generation manifest SHA-256は
`sha256:d08a0d136a0f00e280954fde59cc86020daa0b147c80d3971e4e1be3a812a00f`である。

現checkoutからrelease serverを再buildし、同じprobeをHazkey、Mozcの順に逐次実行した。各1,360件、
ウォームアップ0回、反復1回、Top-10、ABProbe v5 `segment_candidates`である。raw SHA-256は次のとおり。
このHazkey v5取得ではZenzaiを有効化していないため、以下の結果をHazkey+Zenzaiの品質値として扱わない。

- Hazkey: `sha256:5be953f09a62373967af5495c9f4d34b2b161b2b1655856b919e6a544d8fe3d5`
- Mozc B0: `sha256:2efeaab87a7253bc9574bc584035a8dbb779fbe1b0d9812798e2ef5723a54a6e`

### 先頭IMEチャンク境界

ここで測るのは、ABProbe v5候補の`consuming_count`が許容先頭span集合のいずれかへ一致するかである。
全文のチャンク列、Acceptable Path Accuracy、全境界F1は測っていない。

| 対象 | Hazkey Top-1 | Mozc Top-1 | Mozc差 |
|---|---:|---:|---:|
| 全1,360件 | 545 / 1,360（40.07%） | 715 / 1,360（52.57%） | +170件、+12.50pt |
| `protected`除外1,260件 | 545 / 1,260（43.25%） | 713 / 1,260（56.59%） | +168件、+13.33pt |

全件の排他的4群は、両方正解451件、Mozcだけ正解264件、Hazkeyだけ正解94件、両方不正解551件だった。
予測境界から最も近い許容境界までの絶対element差平均はHazkey 7.02、Mozc 4.81である。許容境界より
手前で切る過分割はHazkey 641件、Mozc 603件、許容境界より後ろで切る不足分割はHazkey 155件、
Mozc 41件だった。Top-10内の候補は同じ`consuming_count`を共有したため、この取得では境界Top-kは
Top-1と同値である。

カテゴリ別ではMozcが大半で優位だった一方、proper-nounだけはHazkey 138/200、Mozc 119/200だった。
したがってHazkey境界へ切り替える余地は94件あるが、全体でMozcだけが正しい264件を壊さない保守的な
選択規則が必要である。

### 境界と表層を分けた評価

全許容経路が読み・表層対応済みの533件だけをsurface分母にした。

| 系 | 先頭境界正解 | 境界正解時の表層Top-1 | End-to-End Top-1 | End-to-End Top-k |
|---|---:|---:|---:|---:|
| Hazkey | 158 / 533（29.64%） | 147 / 158（93.04%） | 147 / 533（27.58%） | 158 / 533（29.64%） |
| Mozc | 208 / 533（39.02%） | 199 / 208（95.67%） | 199 / 533（37.34%） | 206 / 533（38.65%） |
| ランタイムH0 | 208 / 533（39.02%） | 199 / 208（95.67%） | 199 / 533（37.34%） | 208 / 533（39.02%） |

Hazkeyでは境界誤り375件に対し、境界が正しいのに表層を外したのは11件だった。Mozcではそれぞれ325件、
9件である。このsliceでは、End-to-End誤りの主因は表層順位より先頭境界であり、「文節精度が変換精度へ
強くつながる」という仮説と整合する。ただしaligned 533件への条件付き結果であり、全1,360件へ外挿しない。

H0はMozc Top-1と境界を維持し、Hazkey候補を後段へ追加するため、Top-1はMozcと同じだった。Top-kでは
Mozc-onlyより2件多い208/533を被覆した。診断H1の昇格は6件、width guard付きH2では2件だったが、
fully-aligned sliceのTop-1/Top-k End-to-EndはどちらもH0から改善0・悪化0だった。したがって今回の値は
H1/H2昇格を有効化する根拠にならず、production H0を維持する。

### 取得時間と制約

逐次取得の候補生成時間はHazkeyが中央値15.00 ms、P95 65.24 ms、Mozcが中央値1.31 ms、P95 8.84 msだった。
観測最大total PSSはHazkey 60,207 KiB、Mozc 50,952 KiBである。ウォームアップなし・1反復のABProbe診断値で、
製品UIの初回表示時間やSpace待ち時間ではない。

最終評価JSONのSHA-256は
`sha256:39a2310cf01bb6279d5a631177362c1b652e5cc31725314580b912b7c539873d`である。
この1,360件は規則策定前から既知のcorpusなので、評価は`diagnostic_only=true`、
`formal_authorized=false`、結論`inconclusive`である。全文許容経路の精度には全segment列を観測するrunner拡張、
H1/H2の採否には事前固定した未見holdoutが必要である。

## Mozc/Hazkey+Zenzai/H0の品質評価方針（2026-07-17更新）

### 主比較対象

次の評価では、同じlabel-free probeを使った以下の3系統だけを主出力とする。

1. `mozc_standalone`: Mozc単独の観測結果
2. `hazkey_zenzai_standalone`: Zenzaiを有効にしたHazkey単独の観測結果
3. `mozc_first_hazkey_zenzai_h0`: 上の2観測結果から導出するランタイムH0 mirror

H0は共有実装`_merge_boundary_aware_candidate_records`を
`allow_promotion=false`、`width_guard=false`で呼び出す。Mozc候補が空でないケースでは、H0が
Mozc Top-1、Boundary@1、End-to-End@1を変えた場合に評価を失敗させる。Mozcが空の場合だけ、
既存ランタイムと同じHazkey fallbackを許す。H1/H2の昇格結果は主レポートへ含めず、過去の表にある
H1/H2値から新しい規則を選ばない。

既知1,360件の許容経路generationは、評価器と指標の動作確認、失敗原因の分解、取得契約の検証には使う。
すでに人間と実装者が内容を見ているため、score threshold、カテゴリ規則、surface override、
boundary overrideの採否には使わない。既知1,360件から得た結果は、改善していても
`diagnostic_only=true`、`formal_authorized=false`、`decision.status=inconclusive`のままとする。

### ABProbe v6の対取得契約

品質比較はABProbe v6 `segment_candidates`のMozc runとHazkey+Zenzai runを対にして行う。
両runでケース順、probe SHA-256、source revision、producer実行ファイルidentity、Top-K、warm-up、
iteration、composition spanを一致させる。学習は無効、contextは空とする。Mozc側はZenzai無効と
全モデル項目の`null`を要求し、Hazkey側はZenzai有効、モデルpath/size/SHA-256、inference limit、
resolved deviceを固定する。resource identityはバックエンドごとに記録する。

各候補は`text`、`rank`、`consuming_count`に加え、`provenance`、`ranking_influence`、
nullableな`zenzai_score`、`zenzai_score_token_count`、`zenzai_score_scope`を持つ。score、採点token数、
scopeは3項目すべてがnullか、すべてが有効値でなければならない。scoreがある候補は
`ranking_influence=zenzai`を必須とするが、
Zenzaiが順位へ影響していてもscoreを取得できない経路はあるため、逆向きは要求しない。未知field、
重複JSON key、不正なrank、composition span外の候補、producer drift、各run内のquality policy driftは
評価前に拒否する。

評価器`tools/dictionary/evaluate_mozc_zenzai_hybrid_quality.py`は、既存の許容経路generationを
no-followで固定取得し、人手exportから派生物を再導出してbyte一致を確認する。品質指標は次を別々に報告する。

- 許容先頭span集合に対するBoundary@1、Boundary@K、Boundary MRR
- 全許容経路が読み・表層対応済みのケースだけを分母にした、境界正解条件付きSurface@1/@K
- 同じfully-aligned分母に対するEnd-to-End@1/@K、End-to-End MRR
- 各系統間の救済、悪化、純増減
- H0がMozc-onlyへ追加した候補数と、その追加候補によるTop-K被覆

実測値は、対になったv6取得と入力identityの検証が完了するまで記載しない。v5以前の数値をv6結果として
流用しない。

### 既知1,360件のv6診断結果

2026-07-17に同一release producerでMozcとHazkey+Zenzaiを逐次取得した。各1,360件、warm-up 0、
1反復、Top-10、空context、学習無効である。ZenzaiはCPU、inference limit 10、modelは
72,298,816 bytes、`sha256:501f605d088f5b988791a00ae19ed46985ed7c48144f364b2f3f1f951c9b2083`を使った。
producerは両runとも
`sha256:80e70345ff5f9aca8a9f9c4b535dc46d2deb4efa3b0c72b1cfef96212337c99c`で一致した。
ローカル成果物は`build-grimodex/mozc-zenzai-hybrid-quality-20260717/`に置いた。

| 系 | 境界Top-1 | 境界正解時の表層Top-1 | End-to-End Top-1 | End-to-End Top-k |
|---|---:|---:|---:|---:|
| Mozc単独 | 715 / 1,360（52.57%） | 199 / 208（95.67%） | 199 / 533（37.34%） | 206 / 533（38.65%） |
| Hazkey+Zenzai単独（分離境界） | 545 / 1,360（40.07%） | 147 / 158（93.04%） | 147 / 533（27.58%） | 156 / 533（29.27%） |
| Mozc-first H0 | 715 / 1,360（52.57%） | 199 / 208（95.67%） | 199 / 533（37.34%） | 208 / 533（39.02%） |

Hazkey+ZenzaiはMozcに対し、境界Top-1を94件救済して264件悪化、End-to-End Top-1を22件救済して
74件悪化した。H0はMozc Top-1を1件も変更せず、279ウィンドウへ重複しない518候補を追加し、
End-to-End Top-kを2件救済、悪化0件だった。

Zenzai有効の順位経路を通った候補は3,523件で、raw scoreを持つ候補は1,344件（38.15%）、scoreを1件以上持つ
ケースは1,344/1,360件、Top-1がscoreを持つケースは1,291件だった。scopeは`full_candidate` 1,281件、
`constraint_suffix` 63件である。scoreがない候補を除外する規則は、順位影響候補の大半を欠測として
落とすため採用しない。

参考として旧Zenzai無効H5との差を見ると、Top-1表層は90件変化したが境界変化は0件だった。ただし両runとも
Zenzaiを無効化した同じ分離境界コンバーターで境界を決めているため、これはNative Zenzai連動境界が
変化しないという証拠ではない。
fully-aligned 533件中、Top-1が変化した24件は7件救済、7件悪化、10件は両方誤りで、純増減は0だった。
Zenzaiが異なる答えを出す価値は実測できた一方、無条件切替の根拠にはならない。`surface_override`候補は
47件あったが、End-to-Endを判定できたのは10件だけで、4件救済、2件悪化、4件両方誤りだった。
この10件では救済4件のTop-1だけにscoreがあり、悪化2件と両方誤り4件にはなかったが、件数が小さく、
既知corpusでもあるため「scoreあり」を昇格規則にしない。不一致強化セットの抽出層としてのみ使う。

候補生成時間はMozcが中央値1.31 ms、P95 8.79 ms、Hazkey+Zenzaiが中央値72.11 ms、P95 247.92 msだった。
最大total PSSはMozc 53,616 KiB、Hazkey+Zenzai 195,252 KiBである。これは逐次ABProbeの診断値であり、
IMEの初回表示時間やSpace待ち時間ではない。raw SHA-256はMozc
`sha256:d70150ad1402a3c6323e4935158b3f2d204590dc6e1ffd3fecb3745cf1b3ab6c`、Hazkey+Zenzai
`sha256:f7aeb0b19d801d9fe6bcd25ae418dc7027070ee380c1aebd2773b72822fd4c03`、評価JSON
`sha256:d69f0e4e1baff53481ea5118ab08bf14f6186f167ac334ccd86ecf33ed9c8c30`である。

この結果は`diagnostic_only=true`、`formal_authorized=false`、`decision.status=inconclusive`である。
既知corpusからscore threshold、入力カテゴリgate、surface/boundary overrideを採用しない。

### 境界方式と左文脈を分離したABProbe v7

上表の「Hazkey+Zenzai単独（分離境界）」という名称は重要である。現行adapterは、Zenzai有効のprimary
converterが表層と順位を作る前に、別のboundary converterを学習なし、履歴なし、Zenzai OFFで実行して
`consuming_count`を固定する。そのため545/1,360という値は正確には「azooKey辞書だけで決めた分離境界
＋その固定span内のZenzai表層」の境界精度であり、azooKey本来のZenzai連動境界の評価値ではない。

ZenzaiはIME境界A/Bを直接scoreするモデルではない。全文候補の評価で別の辞書ラティス経路が選ばれ、
その経路の辞書ノード列から`CandidateData.clauses.first`に相当する先頭clauseが取り出される結果として、
境界へ間接的に作用する。この作用を分離して測るため、境界方式を次の3経路に固定する。

| 境界方式 | 境界の由来 | Zenzaiの役割 |
|---|---|---|
| `isolated_dictionary` | 独立boundary converter | 境界はOFF、固定span内の表層・順位だけON |
| `native_zenzai_first_clause` | primary converterのZenzai選択後`firstClauseResults` | 全文経路と、その副産物である先頭境界にON |
| `mozc_fixed` | 同一入力のMozc Top-1 `consuming_count` | 境界はMozc固定、span内のHazkey表層・順位だけON |

Native経路は製品の`segmentCandidates`を変更せず、probe専用
`nativeZenzaiSegmentCandidatesForProbe`からprimary requestの`firstClauseResults`をそのまま観測する。
ABProbe v7は`boundary_policy`へmode、境界へのZenzai適用有無、表層への適用有無、境界sourceを明記し、
`conversion_path`との矛盾を評価器で拒否する。`mozc_fixed`はMozc観測値を固定span sidecarとして束縛する
probe専用経路として実装した。製品の`segmentCandidates`は変更せず、固定した`targetCount`に対して
Hazkey primary converterの表層候補だけを取得する。このAPIは固定spanより短い部分候補も返し得るため、
probeでは`consuming_count == targetCount`の候補だけを残し、それ以外を境界候補として混入させない。
完全一致候補がなければ空候補として記録する。H0の後段候補追加をこの第3経路の代用にしない。

固定span sidecarはMozc ABProbe v6のexact bytesから決定論的に生成する。各行はID、読み、読みの
UTF-8 SHA-256、Mozc Top-1の`consuming_count`、元Mozc runのschema、exact-byte SHA-256、case数、
backend、conversion pathを持つ。case順、読み、hash、1以上かつ入力範囲内のcount、全行で同一のoriginを
ABProbe起動時に検証する。v7 rootの`fixed_boundary`は全modeで常在し、分離境界とNativeでは`null`、
`mozc_fixed`では読みhash、count、sidecar全体のexact identityを持つ。固定spanで候補が0件でも
`fixed_boundary`は残り、Zenzai実行有無は`zenzai_execution`で別に監査する。

`boundary_zenzai_enabled`は要求した設定方針を示すだけで、モデル評価が成功した証拠ではない。そこでv7は、
warmupを除く各caseの最終計測iterationについて、primary converterへ送った全request数、候補評価attempt数、
attempt outcome（`pass` / `fix_required` / `whole_result` / `error`）、requestごとのterminal outcome
（前記4種に`inference_limit` / `no_candidate`を加えたもの）を`zenzai_execution`へ記録する。
分離境界経路は内部の2 requestを合算し、NativeおよびMozc固定境界経路は1 requestを記録する。候補scoreは
全v7経路でnullableの診断値とし、モデル実行の証明には使わない。評価器はcaseごとの件数整合、run全体で1回以上の
評価attempt、model load済みproducerの取得契約を要求する一方、失敗terminalを含むcase自体は捨てずに分母へ残す。
terminalの成功は`pass`だけであり、`fix_required` / `whole_result` / `error` / `inference_limit` /
`no_candidate`は件数を報告し、採否上のformal blockerとする。
さらに方式別request数を2/1/1で固定し、`pass` / `fix_required` / `whole_result` / `error`のterminal件数が
対応attempt件数を超えないこと、総attempt数が`inference_limit * request_count`以下であることを検証する。
`no_candidate`はモデル評価前に終わるrequestもあるため、0 attemptを許容する。

左文脈の有無はこの境界方式とは別軸である。盲検source compilerは自然な左文脈のsidecarに加え、同じcase、
同じsource hashを持つ全件空文字の対照sidecarを生成する。ABProbe v7はraw左文脈を結果へ出さず、mode、
UTF-8 SHA-256、code point数、byte数、sidecar全体のidentityだけを記録する。caseごとの左文脈は
`CompositionInput`とZenzai requestの両方へ渡し、空文字caseではcontextual modeを明示的に無効化して、
「空文字を文脈あり設定で渡した」差が対照へ混ざらないようにする。

3境界方式の比較では、空文脈3 runが同じ空sidecarのexact identity、自然左文脈3 runが同じ自然sidecarの
exact identityを持つことを必須とする。さらに全Hazkey v7 runでproducer、辞書resource、Zenzai modelを含む
quality policy、source-ref、Top-K、corpus、warm-up、iterationを共通照合し、別binary・別辞書・別GGUFの
混在をfail-closedで拒否する。Nativeの`firstClauseResults`は最大5件なので、Nativeを含む品質比較の共通
Top-Kは5以下とし、reportへ要求Top-Kと実効比較Top-Kを明記する。Top-K 10で取得した既存2ケースsmokeは
配線確認に限り、Top-K品質比較の証拠にはしない。

専用評価器`tools/dictionary/evaluate_zenzai_left_context_quality.py`は、分離境界も空文脈v7と自然左文脈v7を
厳密に対にし、
任意でNative境界およびMozc固定境界の空文脈v7と自然左文脈v7を対にする。Mozc固定比較ではraw Mozc v6から
sidecarを再生成してbyte一致を要求し、`result -> fixed sidecar -> raw Mozc`のSHA-256、ID順、読み、countを
再導出する。corpus、producer、辞書、Zenzai model、Top-K、
source row、context sidecarのexact identityを照合し、Boundary、正しい境界に条件付けたSurface、End-to-Endの
Top-1/Top-Kと救済・悪化を別々に出す。既存の許容経路は左文脈を条件に作られたgoldではないため、
この比較も当面は`diagnostic_only`であり、production overrideを承認しない。

Mozc固定経路の境界成否は候補列ではなく、固定sidecarの`consuming_count`から採点する。固定境界が正しくても
表層候補が0件なら、表層alignment済みケースではBoundaryは成功、条件付きSurfaceは比較可能だが失敗、
End-to-Endも失敗として分離して
数える。また各方式×文脈についてprocess/backendのRSS/PSSの`after`と`after - before`分布を残す。ただし
runは逐次に起動した別ABProbe processなので、これは診断値であり、ランダム化された対メモリ効果ではない。

同一文脈内では、分離境界、Native、Mozc固定の各system間についてBoundary、条件付きSurface、End-to-Endの
救済・悪化matrixをempty/natural別に出す。Mozc固定sectionには、raw Mozc候補をsidecarの
`consuming_count`完全一致だけへfilterし、相対順位を保った`mozc_at_fixed_boundary`も第3 systemとして加える。
別境界のMozc候補はTop-Kへ混ぜず、Mozcから固定境界内Hazkeyの空／自然文脈へのSurface・End-to-End差を
比較する。このMozc systemも、候補0件時を含め境界は明示fixed countから独立に採点する。

各caseにはsystem別Top-1 `consuming_count`、許容count集合、最寄り許容境界へのsigned deltaと最小絶対差、
`match` / `too_long` / `too_short` / `missing`を残し、summaryでも件数と要素差分布を出す。これによりNativeの
長い読み優先が過長境界へ寄るリスクを、表層精度と混ぜずに確認できる。category別集計も全比較と方式間matrixへ
出すが、gold categoryは層別専用でありruntime gateへ入力しない。

Zenzai score差は、raw scoreではscore scopeと採点token数が両方同じ場合だけ比較可能とする。token数で正規化した
per-token score差はscopeが同じなら比較できるため、rawとper-tokenで比較可能分母を分けて報告する。候補score
欠測は引き続き有効な観測であり、実行成否は`zenzai_execution`だけで判断する。

取得順は、同じABProbe executable、corpus、source-ref、Top-K、warm-up、iterationを維持して次のようにする。

```sh
# 1. Mozc v6を取得後、そのexact bytesから固定境界sidecarを一度だけ生成する。
python3 tools/dictionary/prepare_mozc_fixed_boundary_sidecar.py \
  --mozc-results /path/to/mozc-v6.jsonl \
  --output /path/to/mozc-fixed-boundary.jsonl

# 2. 全件空文脈sidecarと自然左文脈sidecarで、同じ固定境界を2回測る。
hazkey-server --ab-probe --corpus /path/to/probe-input.jsonl \
  --dictionary /path/to/dictionary --source-ref SOURCE_REVISION \
  --result-schema v7 --zenzai-model /path/to/zenzai.gguf \
  --iterations 1 --top-k 5 --left-contexts /path/to/context-empty.jsonl \
  --boundary-mode mozc_fixed \
  --mozc-fixed-boundaries /path/to/mozc-fixed-boundary.jsonl \
  > /path/to/fixed-empty-v7.jsonl

hazkey-server --ab-probe --corpus /path/to/probe-input.jsonl \
  --dictionary /path/to/dictionary --source-ref SOURCE_REVISION \
  --result-schema v7 --zenzai-model /path/to/zenzai.gguf \
  --iterations 1 --top-k 5 --left-contexts /path/to/context-natural.jsonl \
  --boundary-mode mozc_fixed \
  --mozc-fixed-boundaries /path/to/mozc-fixed-boundary.jsonl \
  > /path/to/fixed-natural-v7.jsonl
```

評価時の必須入力は、`--isolated-empty-context-sidecar`、`--isolated-empty-v7`、
`--isolated-left-context-sidecar`、`--isolated-left-v7`の4項目である。旧Hazkey空文脈v6を比較baselineには
使わない。v6は`--fixed-raw-mozc-v6`でMozc固定sidecarの起点を証明する用途にだけ残す。Mozc固定比較を
追加する場合はこれに`--fixed-boundary-sidecar`、`--fixed-empty-v7`、
`--fixed-empty-context-sidecar`、`--fixed-left-v7`、`--fixed-left-context-sidecar`を合わせた6項目を
すべて渡す。Nativeも2 runと2 sidecarを全指定し、各optional経路の部分指定は拒否する。

2026-07-17の実モデルによる2ケースsmokeでは、`きょうはいしゃにいく`に対して分離境界が
`consuming_count=4`（`今日は`）、Native境界が`consuming_count=3`（`今日`）となった。これは現行分離境界と
Native Zenzai連動境界が実際に別の結果を返し得るという配線確認であり、2ケースから精度改善を結論しない。

同じ現行producer、同じGGUF、同じ入力、共通`top_k=5`で3境界方式×空／自然左文脈の6 runも再取得した。
全12 case-run、合計16 requestでZenzaiのterminal outcomeは`pass`であり、途中の`fix_required` 7回を含む
23 attemptも最終成功まで監査できた。
結果は次の通りである。

| 入力 | 境界方式 | 空文脈Top-1 | 自然左文脈Top-1 |
|---|---|---|---|
| `きょうはいしゃにいく` | 分離境界 | `4:今日は` | `4:今日は` |
| 同上 | Native | `3:今日` | `3:今日` |
| 同上 | Mozc固定 | `4:今日は` | `4:今日は` |
| `はしをわたる` | 分離境界 | `3:橋を` | `3:橋を` |
| 同上 | Native | `3:橋を` | `3:橋を` |
| 同上 | Mozc固定 | `3:箸を` | `3:橋を` |

2件目の自然左文脈は「川の向こうへ行きたい。」である。Mozc境界を3要素へ固定したまま、空文脈の`箸を`が
自然左文脈では`橋を`へ変化したため、境界差を交ぜずに左文脈ありZenzaiの表層・順位効果を観測できた。
6 runのproducerはすべて
`sha256:a1ec3f9c0f3bbaf90a5687f0406c72d47ee5fe204ad7a2dad2352511a6a358b6`で一致した。
raw Mozc v6は
`sha256:604b1103199b8613920d1da89098678337cc1c1ad720ef66977eb484622272d5`、そこから生成した固定境界sidecarは
`sha256:a2502ecf159db0cf24d0b356c80ca8736e284c31705abe0a50cdeef104354af8`である。v7成果物には生の左文脈を
保存せず、hash、文字数、byte数、sidecar identityだけを記録している。

Nativeも「操作しやすいIMEチャンク」を直接学習しておらず、`firstClauseResults`の長い読み優先を含むため、
長すぎるチャンク問題が改善する保証はない。未知holdoutでは境界方式と左文脈を交差させ、分布と操作コストを
個別に確認する。

### 未知holdoutで分けて探索する二つのoverride

未知holdoutでは、Mozc Top-1とHazkey+Zenzai Top-1の`consuming_count`が同じで、NFC正規化後の
表層だけが違うケースを`surface_override`候補とする。`consuming_count`自体が違うケースは
`boundary_override`候補とする。この二つは、発火対象と正解outcomeをケースごとに別フィールドへ保存し、
一つの「Hazkey優位」指標へ混ぜない。

現行adapterでは、表層と順位はZenzai有効のprimary converterが作る一方、`consuming_count`を決める
boundary converterは学習、履歴、Zenzaiを常に無効化している。したがって`boundary_override`は
「Zenzai境界への切替」ではなく、辞書ベースのHazkey境界への切替として記録・評価する。

探索特徴には、nullableな生のZenzai score、scoreを`zenzai_score_token_count`で割った採点長正規化値、
score availability、`ranking_influence`、候補集合の重複・差分、Top-1表層差、Top-1境界差、
読みのelement数とscript構成を使う。holdoutの入力カテゴリは層別集計には使えるが、正解ラベルから
導出したカテゴリをruntime gateへ使わない。カテゴリを将来gateへ使う場合は、変換前にruntimeで観測可能な
定義と分類器を別途固定し、さらに未使用の評価集合で検証する。

scoreは確率として校正されておらず、長さ正規化しても異なるprompt/request間で比較可能になるとは限らない。
現経路では1 request内で記録される`.pass(score:)`が最大1件であり、複数scoreが一つの候補列に現れた場合は
別request由来である可能性がある。request identityとscoreの対応を保持するまでは、候補間score gapを
昇格条件にしない。scoreなしのケースも分母から黙って除外せず、availabilityを明示して別集計する。

探索で規則を作ったholdout自身は採否分母に再利用しない。surface overrideとboundary overrideは個別に
規則を固定し、その後に作成・封印した未使用の評価集合で、Boundary、条件付きSurface、End-to-Endの
救済・悪化を再測定する。少なくとも悪化上限、必要な発火件数、欠測scoreの扱い、fallbackを事前固定する。

### 次期データセットの二層構成

総件数だけで安全性を判断せず、実際に固定規則が発火する件数を管理する。悪化0件を観測した場合でも、
悪化率の片側95%上限は概ね`3 / 発火件数`である。上限1%には約300発火、0.5%には約600発火、
0.1%には約3,000発火が必要になる。発火率5%なら300発火を得るために全体約6,000件、発火率1%なら
約30,000件が必要であり、全入力を同じ比率で人手注釈する方式は採らない。

- 代表分布holdout: 実入力分布に近い未見2,000〜3,000件を使い、三方式の総合品質を測る。
- 不一致強化セット: Top-1表層差、境界差、score availability/scope/帯、固有名詞、入力shapeを使って
  label-freeで重点抽出し、override規則の探索と必要発火数の確保に使う。

探索集合と最終確認集合は、同一原文や言い換えがまたがらないfamily単位で分離する。規則とthresholdを
固定した後にだけロックされた最終holdoutを開き、override発火ケースは最終採否前に全件人手確認する。

### Lunar Lowを使うblind annotation

5,000〜10,000件規模へ拡張する際は、Lunar Lowを一次アノテータとして使う。読みと参照表層だけを渡し、
Mozc候補、Hazkey/Zenzai候補、Zenzai score、エンジン名、どちらを勝たせたいかという情報は渡さない。
出力は単一分割ではなく、複数を許せる`acceptable_paths`とする。

大規模SilverセットはLunar Lowの提案を自動採用候補とし、読み全体の被覆、表層alignment、経路重複、
空チャンク、source revision、schemaを機械検証する。ロックされたGold holdoutはLunar Lowで初期作成した後、
長文、複数許容経路、読み修正、alignment不成立、異常ケースを人手確認し、残りからもランダム10〜20%を
監査する。Lunarのみの行はSilver、人手レビュー済みの最終subsetだけをGoldとする。model、effort、
prompt version、生成日時を各生成batchへ記録する。

### 現在のblocker

- 同一producerと固定modelの対ABProbe v6は取得済みだが、動的`libllama`/`libggml`、GGML backend、
  driver/hardwareのidentityまでは封印していない。
- 未知holdoutの作成、既存corpusとの重複screen、labelとprobeの物理的隔離が未完了である。
- Zenzai scoreへrequest identityがなく、候補間gapの比較可能性を証明できない。
- ABProbe v7の左文脈経路と空文脈対照は実装済みだが、大規模な対取得は未実施であり、既存goldも
  左文脈条件付きではない。request単位の実行outcomeは公開したが、学習tokenがTop-1 overrideへ寄与した
  候補単位の証拠はscoreと別フィールドで公開していない。
- runtimeで使用可能な入力カテゴリ定義と分類器が固定されていない。
- 現ABProbeは先頭segment候補の評価であり、全文Acceptable Path Accuracyや全境界F1にはrunner拡張が要る。
- 品質用ABProbe時間は製品UIのMozc初回表示時間、Space待ち時間、PSS/RSSの代替にはならないため、
  ランタイム測定を別に継続する必要がある。

以上が解消するまではH0をproduction既定値として維持し、H1/H2も新しいoverride規則も有効化しない。

## 未見・文節ラベル付きholdout経路の実装スパイク（旧H1/H2採否プロトコル）

この節はH1/H2を次の候補としていた時点の実装記録である。2026-07-17以降の主比較対象と採否順序は
前節が置き換える。ここで封印した契約の考え方は再利用するが、H2昇格機会の下限を次の製品gateにはしない。

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
新しいホールドアウトも必要である。次はH2を直接試すのではなく、Mozc単独、Hazkey+Zenzai単独、
Mozc-first H0を同じv6分母で測定する。その後、未知データで同一境界のsurface overrideと
異なる境界のboundary overrideを分離して探索し、規則を固定してから別の未使用評価集合で検証する。
