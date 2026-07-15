# Mozc B0/B1 v1 pilot findings

実施日: 2026-07-15

## 位置づけ

この256件評価は`decision_tier=pilot`であり、B0/B1の正式な採用・棄却を
決定しない。`formal_adoption_allowed=false`を固定し、正式採用についての結論は
`inconclusive`とする。以下は再現可能なpilot findingである。

- B0はpilot基準上、速度・PSS・protected 16/16・全体Top-1を満たしたが、
  Top-10がHazkey比`-18.359375pt`となり、pilot許容値`-12pt`を超えた。
- B1はB0の候補列をprefixとして保持したまま自然分割候補を追加し、Top-10を
  `171/256`まで回復してHazkeyと同点にした。ただしTop-1は`124/256`のままで、
  `long-structural=-25pt`と`proper-noun=-16.666667pt`がpilotカテゴリ許容値
  `-10pt`を超えた。
- pilot合格条件は全項目のANDである。上記の品質条件だけでpilot failureが確定するため、
  人手blind preference、`both_bad`集計、長時間stability実走はearly stopした。
  未実施項目をpassとして扱っていない。
- 現在の運用状態として、既存のopt-in Mozc経路とB0 artifact defaultを維持する。
  B1は`--profile b1`を明示した評価専用profileとして凍結する。これは採用・棄却の
  判断ではない。B2はこのpilotでは作らない。
- 通常runner `scripts/grimodex-ime.sh`とMozc専用runner
  `scripts/grimodex-ime_mozc.sh`の分離は維持する。

機械可読なpilot findingとevidence identityは
[`decision.json`](./decision.json)に固定した。

## 評価対象

v1 pilot corpusは256件で、aggregate SHA-256は
`123f47cb6f747135451e5969b32d9868ec61d9574fa6eb4b0001e5409287c807`である。

| source | cases |
|---|---:|
| AJIMEE-Bench unconditional | 100 |
| product curated | 140 |
| protected | 16 |

corpus本体、上流revision、license、変換手順は
[`mozc-adoption-v1`](../../../hazkey-server/Tests/grimodex-spike/Fixtures/mozc-adoption-v1/README.md)
に固定した。AJIMEE由来データとproduct由来データは分離したまま連結し、文脈ありAJIMEE、
15件sentinel、stress dataはpilot 256件へ混ぜていない。

## 品質結果

以下の`pass`/`fail`はv1 pilot内の閾値判定であり、正式採用・棄却を表さない。

| metric | Hazkey | B0 | B1 | threshold | B0 | B1 |
|---|---:|---:|---:|---:|---|---|
| Top-1 | 127/256 | 124/256 | 124/256 | Hazkey比`-8pt`以内 | pass | pass |
| Top-10 | 171/256 | 124/256 | 171/256 | Hazkey比`-12pt`以内 | **fail** | pass |
| protected | 16/16 | 16/16 | 16/16 | 16/16 | pass | pass |
| long-structural Top-1 | 17/20 | 12/20 | 12/20 | Hazkey比`-10pt`以内 | **fail** | **fail** |
| proper-noun Top-1 | 16/24 | 12/24 | 12/24 | Hazkey比`-10pt`以内 | **fail** | **fail** |

全カテゴリTop-1 deltaは次のとおり。B0とB1はcandidate zeroを同一に保つため、
Top-1値も同一である。

| category | Hazkey | B0/B1 | delta |
|---|---:|---:|---:|
| AJIMEE unconditional | 47/100 | 50/100 | +3.000pt |
| colloquial | 19/24 | 20/24 | +4.166667pt |
| Grimodex regression | 3/20 | 5/20 | +10.000pt |
| homophone/context | 6/20 | 7/20 | +5.000pt |
| long/structural | 17/20 | 12/20 | **-25.000pt** |
| proper noun | 16/24 | 12/24 | **-16.666667pt** |
| protected | 16/16 | 16/16 | 0.000pt |
| technical/mixed | 3/32 | 2/32 | -3.125pt |

## B1の境界

B1は学習とZenzaiを引き続き無効にし、private sidecar protocolとB0 datasetを変更しない。
full-reading変換でだけ、Mozcの自然分節候補をbounded beamで組み合わせる。

最終B0/B1 quick comparisonでは次を確認した。

- B0で候補があった253件のcandidate zero変更: 0件
- B0候補の欠落または順序変更: 0件
- B1で追加候補が得られたcase: 233件
- B1で候補が空のcase: 0件

B1はTop-10を47件回復した一方、candidate zeroを再順位付けしない設計なので、
Top-1のカテゴリ退行を直せない。次候補を検討する場合はB2として、context注入または
deterministic rerankingを別途設計し、B1を黙って差し替えない。

## 性能結果

B0のpaired pilot値は同一host、各backend 4 run、各case warmup 5 /
iteration 20、交互順序で取得した。

| metric | Hazkey | B0 | ratio | threshold | result |
|---|---:|---:|---:|---:|---|
| warm p95 | 26.203913 ms | 7.056145 ms | 26.927829% | <= 50% | pass |
| total PSS | 48,730 KiB | 45,514 KiB | 93.400369% | <= 125% | pass |

B1の同じcase/warmup/iterationによる単一runはp95 `7.134653 ms`、total PSS
`47,010 KiB`だった。これはB1に明確な速度退行がないことの確認値であり、B0の8-run
acquisitionと同格のpaired性能判定には使わない。B1はpilot品質基準だけで
`pilot_fail`が確定している。

## Early-stopしたgate

次は`not_run_early_stop`であり、passではない。

- 256件のblind human net preference
- `both_bad`件数
- B0/B1の長時間stability実走

pilot quality gateの失敗後にこれらを実行しても、このv1 pilotのAND結果は
`pilot_pass`へ変わらない。ただし未実施項目があるため、このearly stopを正式な
採用・棄却の根拠にはしない。将来の評価を安全に実行できるよう、native recovery、
Protocol steady、Fcitx lifecycle/soakのevidence validatorとproducer contractは
repoに残す。

## Artifact identity

B0:

- helper: `8676275bb47aefe963c8b82047cc66fb7a5140caec72d1ebbfa17556b281577d`
- data: `b9884362e37772f772a0d28d1e12622455c14353497b3435deed60aa7e592c5e`
- runtime generation: `sha256-ad277af2ad5a634f23c7b84b7f346b02f341905f10fcfa6eb9912db78a0866cb`

B1:

- overlay: `974003704cacdc9b272fe22c3675222889c1bee2c75b81619317b2431318f55d`
- helper: `728d9a79c0f540a832d3f404a2603f49080e1f9e7ee1d24df1a0a69f5a4a75e8`
- data: B0と同一
- runtime generation: `sha256-046bcfa093aac43ad6ee64afd4b3a3e8325bab0f3d20b8cb083c447ba8c91a2f`

B1 bundleは固定upstream revision/tree、Bazel 9.0.2、固定overlayから隔離buildし、
default B0 verifierで拒否、explicit B1 verifierでaccept、host ABI/PING成功まで確認した。
