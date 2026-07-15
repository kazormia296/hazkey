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
