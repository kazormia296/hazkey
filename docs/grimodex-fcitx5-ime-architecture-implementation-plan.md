# Grimodex Fcitx5 IME（Linux）アーキテクチャ刷新・基本機能実装計画

- **対象リポジトリ:** `kazormia296/hazkey`
- **対象プラットフォーム:** Linux / Fcitx5
- **基準ブランチ:** `main`
- **基準コミット:** `5e0ecef50c787e84ec76a16d5b1a644b871cc610`
- **文書バージョン:** 0.2
- **文書ステータス:** Draft / 実装着手用
- **最終更新:** 2026-07-12
- **Linux実装対象:** Fcitx5クライアント、Unix socket内部プロトコル、Swift変換サーバー、関連テスト・CI
- **クロスプラットフォーム契約の所有先:** `kazormia296/Grimodex/ime-contract/composition-behavior-v1/`
- **共通wire protocol:** 採用しない。意味論、schema、fixture、適合試験だけを共通化する
- **macOS参照・適合対象:** `kazormia296/azooKey-Desktop`（上流: `azooKey/azooKey-Desktop`）
- **Windows別計画対象:** `kazormia296/azooKey-Windows`
- **参照実装要素:** `InputState`、`SegmentsManager`、`ConverterServer+KeyEvent`、`ConverterServer+Snapshot`
- **前提:** Hazkey/Fcitx5基盤、Grimodexのプロジェクト辞書・セッション分離・secure input・学習制御は維持する

---

## 1. 結論

Hazkeyをベースにした構成は維持する。Fcitx5/Linux統合、Swift変換サーバー、設定・パッケージング、Grimodex連携は再利用価値が高く、azooKey DesktopからLinux IME全体を作り直す必要はない。

一方、現在の入力状態はFcitx側のフラグ、候補リストのフォーカス、Swift側の`ComposingText`と候補一覧に分散している。この状態で機能を継ぎ足すと、文節編集、部分確定、Escape、再接続、学習対象などが相互に不整合を起こしやすい。

本計画では次を基本方針とする。

1. **Swift側のComposition SessionをIME状態の唯一の正とする。**
2. **Fcitx側は生キーの解釈、Snapshotの描画、Client Effectの適用に限定する。**
3. **IPCを細粒度コマンド列から`Action → Snapshot + Effects`へ移行する。**
4. **全面置換ではなく、旧プロトコルを残した段階移行を行う。**
5. **azooKey Desktopは仕様とアルゴリズムの参照元とし、ファイル単位では移植しない。**
6. **まずP0の日本語IME操作契約を完成させ、その後P1機能を追加する。**
7. **本書はLinux/Fcitx5の実装計画に限定し、OS共通のユーザー可視挙動はGrimodex側のComposition Behavior Contractとして分離する。**
8. **macOSへ本オーバーホールをそのまま移植せず、macOS版は参照実装・適合試験の対象とする。Windows版は別の専用計画で状態所有を再設計する。**

---

## 2. 目標

### 2.1 製品目標

- 文節編集、候補選択、部分確定、カーソル編集が一般的な日本語IMEとして自然に動作する。
- サーバー障害、候補0件、古い候補操作、再接続によって入力が消失または二重確定されない。
- 複数InputContext、Grimodexプロジェクト切替、secure input、学習可否がComposition単位で一貫する。
- 状態遷移の大半をFcitxや実辞書なしで高速に単体テストできる。
- Fcitx frontendやアプリ差による表示差をSnapshot Renderer内に閉じ込める。
- 将来のユーザー辞書、候補忘却、再変換を同じ状態モデル上に追加できる。
- Linux、macOS、Windowsで、キーコードや描画APIが異なっても同じ意味的Action・状態遷移・不変条件を検証できる。
- OS固有差分を暗黙の実装差ではなく、共通契約のplatform mappingとして明示できる。

### 2.2 技術目標

- `candidateList->focused()`をIME状態判定に使わない。
- `isCursorMoving_`、`isDirectConversionMode_`、`livePreeditIndex_`のような分散フラグを段階的に廃止する。
- 候補indexは候補世代と組み合わせ、古い候補の確定を拒否する。
- すべての位置単位を明示する。
  - Swift内部: `ComposingText.InputElement` / `ComposingCount`
  - 表示文字列: Swift `String`の書記素クラスタ
  - Fcitx caret: UTF-8 byte offset
  - surrounding text anchor: 現行契約に合わせたUnicode scalar offset
- commit等の副作用を冪等にし、IPC再送で二重適用しない。
- 既存のGrimodexセッション・学習・secure inputテストをすべて維持する。
- Grimodex本体が配布する共通シナリオfixtureをLinuxのSwift/C++テストから実行できるようにする。

### 2.3 非目標

P0完了前には、以下を必須としない。

- azooKey Desktopとの全機能一致
- LLM書き換え
- Tuner連携
- 高度な誤入力訂正
- クラウド同期
- macOS固有UI・キーバインドの完全再現
- converter更新と状態機械刷新の同時実施
- Fcitx transportの全面的な非同期化
- 3 OSの内部wire protocolを単一のprotobuf/XPC/gRPCへ統一すること
- Linux実装の完了をWindows/macOSの同時改修でブロックすること
- 初期段階から共通Swift Packageへコードを抽出すること
- 3 OSで物理キー割り当てを完全一致させること

---

## 3. 適用範囲とクロスプラットフォーム方針

### 3.1 本書の適用範囲

本書で定義するコード変更、Protocol v2、Fcitx renderer、Unix socket復旧、C++/Swift責務分離は、`kazormia296/hazkey` のLinux/Fcitx5実装に固有である。

以下へ本書をそのまま適用しない。

- `kazormia296/azooKey-Desktop`（macOS / InputMethodKit / XPC）
- `kazormia296/azooKey-Windows`（Windows / TSF / named pipe gRPC / Swift FFI）

ただし、本書に含まれるP0操作、状態遷移、不変条件、障害時の期待挙動はOS共通契約へ切り出し、各OS実装が適合試験を行う。

### 3.2 共通化する契約

`kazormia296/Grimodex` に、既存のGrimodex→IME辞書連携Protocol V1とは別の契約を追加する。

```text
Grimodex/
  ime-contract/
    protocol-v1/                    # Grimodex → IMEの辞書・Zenzai条件
    composition-behavior-v1/        # IME内部の入力・変換・候補・確定の意味論
      README.md
      state-machine.md
      actions.schema.json
      snapshots.schema.json
      platform-mapping.md
      platform-exceptions.md
      scenarios/
        composing-basic.json
        cursor-editing.json
        segment-editing.json
        partial-commit.json
        escape-backspace.json
        unicode-cursor.json
        stale-candidate.json
        server-failure.json
        secure-input.json
```

`composition-behavior-v1`で共通化するもの:

- 意味的なIME状態: `idle`、`composing`、`previewing`、`selecting`
- 意味的なAction: insert、delete、move、resize segment、candidate navigation、commit、cancel
- Snapshotの意味: preedit spans、caret、candidate window、selection、effects
- Escape、Backspace、Enter、部分確定後の状態遷移
- 入力消失禁止、二重確定禁止、古い候補確定禁止などの不変条件
- secure input、学習、Grimodex generation pinの共通原則
- Unicode位置変換の境界とテストデータ
- 同じAction列に対する期待Snapshot・Effectのfixture

### 3.3 OS固有のままにするもの

| 項目 | Linux | macOS | Windows |
|---|---|---|---|
| フロントエンド | Fcitx5 | InputMethodKit | TSF |
| 生キー表現 | Fcitx `KeyEvent` | `NSEvent` / keyCode | virtual key / TSF event |
| preedit描画 | Fcitx `Text` | marked text | TSF range/property |
| IPC | Unix socket + protobuf | XPC + Codable JSON | named pipe gRPC + Swift FFI |
| caret単位 | UTF-8 byte | Cocoa range契約 | UTF-16 code unit中心 |
| 候補UI | Fcitx candidate list | AppKit window | Windows候補UI process |
| lifecycle | Fcitx InputContext | IMK session | TSF activation/context |
| package | AUR/deb等 | `.pkg` | Inno Setup / TSF登録 |

物理キーは共通契約へ直接含めず、各OSのAction Mapperが意味的Actionへ変換する。例えば`ResizeSegment(delta: -1)`は共通だが、それを`Shift+Left`へ割り当てるかはOS別アダプタの責務とする。

### 3.4 リポジトリ別の役割

| リポジトリ | 役割 |
|---|---|
| `kazormia296/Grimodex` | 共通Composition Behavior Contract、JSON fixture、platform matrix、契約versionの正本 |
| `kazormia296/hazkey` | 本書に基づくLinux/Fcitx5全面改修。最初の新アーキテクチャ実装対象 |
| `kazormia296/azooKey-Desktop` | 状態機械・文節編集の参照実装。大規模移植はせず、共通fixtureへの適合確認を追加 |
| `kazormia296/azooKey-Windows` | Linux契約安定後に別計画で状態所有を再設計。TSF/Rust/Swift固有の移行を実施 |

### 3.5 横展開の順序

1. Grimodex本体にComposition Behavior Contract v1と最小fixtureを追加する。
2. Linux/Fcitx5を本書どおりに改修し、共通fixtureの最初の完全実装とする。
3. macOSは既存構造を維持し、共通fixtureとplatform差分の適合監査を追加する。
4. Windowsについて専用のアーキテクチャ刷新計画を作成し、同じ意味論へ適合させる。
5. LinuxとWindowsの実装が安定し、重複コードが実証された後だけ共有Swiftコアの抽出を再評価する。

LinuxのP0完了は、Windows改修やmacOS適合試験の完了を待たない。

### 3.6 wire protocolを共通化しない理由

3 OSはネイティブIME API、プロセス寿命、文字位置単位、再入可能性が異なる。共通化対象はAction/Snapshot/Effectの**意味**であり、transportやserializationではない。

```text
Linux:  Fcitx C++  ← Unix socket / protobuf → Swift server
macOS:  IMKit Swift ← XPC / Codable JSON     → Swift server
Windows: TSF Rust   ← named pipe / gRPC      → Rust server ← FFI → Swift
```

各実装は同じfixtureを自分のDTOへdecodeし、同じ意味的結果を検証すればよい。共通wire protocol化は本計画の非目標とする。

---

## 4. 現状の主な問題

### 4.1 状態がC++とSwiftに分散している

Fcitx側は候補リストのフォーカスと複数のboolean/indexで状態を推測し、Swift側は`ComposingText`、候補、学習状態を持つ。どちらにも完全なIME状態がない。

結果として、次の区別が不明瞭になる。

- 入力中
- 最初の変換結果をプレビュー中
- 候補選択中
- 文節範囲編集中
- 部分確定後の残りを変換中
- 直接文字種変換中
- エラー後に復旧待ち

### 4.2 候補UIのフォーカスがキー意味を変えている

候補フォーカス中のLeft/Rightがページ移動になり、文節操作や部分確定と競合する。候補ウィンドウの表示状態はUIの状態であり、IMEドメイン状態として使わない。

### 4.3 プロトコルが操作の意味を表現できない

現行プロトコルは`MoveCursor`、`GetCandidates`、`PrefixComplete`などの個別コマンドを提供するが、次を一つの整合した結果として返せない。

- 現在のIME phase
- アクティブ文節
- preedit span
- caret
- 候補世代
- 選択候補
- commit effect
- 学習対象
- 復旧用状態

### 4.4 表示文字列から確定内容を再構築している

Fcitx側が候補のpreedit表示を用いてcommit文字列を組み立てるため、表示とドメイン状態がずれた場合に誤確定しうる。確定文字列はSwift側がEffectとして返す。

### 4.5 エラー時の入力保持が弱い

候補取得失敗や候補なしでローカルcompositionを破棄する経路がある。変換失敗は「読みを維持して再試行可能」にすべきであり、「入力を消す」にしてはいけない。

---

## 5. 目標アーキテクチャ

```text
Fcitx5 KeyEvent / Focus / Capability Event
                    │
                    ▼
          Fcitx Action Mapper
     （Fcitx固有キー・設定を意味的Intentへ）
                    │
                    ▼
 HandleImeAction(session_id, request_id,
                 expected_revision, action)
                    │
                    ▼
      Swift CompositionSession Reducer
    ├─ ImePhase / composingText / cursor
    ├─ active conversion boundary
    ├─ candidates / candidate generation
    ├─ learning and Grimodex policy
    └─ recovery checkpoint
                    │
                    ▼
             Converter Port
  （AzooKeyKanaKanjiConverterへの唯一の境界）
                    │
                    ▼
      SessionSnapshot + ClientEffects
                    │
          ┌─────────┴─────────┐
          ▼                   ▼
 Fcitx Snapshot Renderer   Effect Applier
 preedit/caret/candidates  commit/switch/delete
```

### 5.1 Fcitx側の責務

- Fcitxの`KeyEvent`、focus、capability changeを意味的Actionへ変換する。
- サーバー応答のpreedit spans、caret、候補、auxを描画する。
- commit等のClient Effectを一度だけアプリへ適用する。
- Fcitx固有のclient preedit / input panel preedit差を吸収する。
- 候補クリックを`candidate_id + generation`付きActionとして送る。
- 最後に確認済みのSnapshotと未確認Action journalを保持し、再接続に利用する。
- transport障害だけを理由にpreeditを消さない。

### 5.2 Swift側の責務

- `ComposingText`と入力カーソルを所有する。
- IME phaseを所有する。
- 変換対象境界と文節編集履歴を所有する。
- 候補生成、選択、部分確定、学習を所有する。
- Grimodex条件とsecure input方針をComposition開始時に固定する。
- 表示用Snapshotと副作用Effectを生成する。
- 古いrevision、重複request、古いcandidate generationを検出する。
- 復旧用checkpointを生成・検証する。

### 5.3 Converter Port

`KanaKanjiConverter`を直接各所から呼ばず、次のような抽象境界を置く。

```swift
protocol KanaKanjiConverting {
    func candidates(
        for composition: CompositionInput,
        options: ConversionOptions
    ) throws -> ConversionOutput

    func setCompletedData(_ candidate: CandidateRef)
    func updateLearningData(_ candidate: CandidateRef)
    func commitLearning()
    func forget(_ candidate: CandidateRef)
    func stopComposition()
}
```

目的は以下。

- fake converterで状態遷移テストを可能にする。
- converter fork更新を別PRに分離する。
- Zenzai有無や障害をテストで注入する。
- 学習呼び出しの回数・対象を検証する。

---

## 6. ドメインモデル

### 6.1 IME phase

P0では次のphaseを実装する。

```swift
enum ImePhase {
    case idle
    case composing
    case previewing
    case selecting
}
```

P1で追加候補となるもの。

```swift
case reconverting
case unicodeInput
```

直接文字種変換は独立phaseにせず、現在phaseと変換対象scopeに対するActionとして扱う。

### 6.2 CompositionSession

概念上、次を所有する。

```swift
struct CompositionSession {
    var phase: ImePhase
    var composingText: ComposingText
    var activeBoundary: ComposingCount?
    var candidates: CandidateSet?
    var selection: CandidateSelection?
    var revision: UInt64
    var lastRequestIDs: RecentRequestCache
    var pendingEffects: [ClientEffect]
    var recoveryCheckpoint: RecoveryCheckpoint
    var context: SessionContext
    var policy: PinnedCompositionPolicy
}
```

注意事項:

- 文節範囲を`String.Index`や整数文字範囲で保存しない。
- ローマ字入力とかな表面長が一致しないため、`ComposingText`の入力要素および`ComposingCount`を正とする。
- `activeBoundary`は「現在候補が消費するprefix」を表す。
- 候補を確定したらprefixを削除し、残りの`ComposingText`に対して新しい候補世代を生成する。

### 6.3 CandidateSet

```swift
struct CandidateSet {
    var generation: UInt64
    var items: [CandidateSnapshot]
    var selectedIndex: Int?
    var pageSize: Int
}
```

各候補はgeneration内だけで有効なopaque IDを持つ。

```swift
struct CandidateSnapshot {
    var id: String
    var text: String
    var annotation: String?
    var consumingCount: ComposingCount
}
```

クライアントは配列indexだけで確定要求を送らない。

### 6.4 Snapshot

```swift
struct SessionSnapshot {
    var revision: UInt64
    var phase: ImePhase
    var preedit: [PreeditSpan]
    var caretUtf8ByteOffset: UInt32?
    var candidateWindow: CandidateWindowSnapshot
    var aux: AuxSnapshot?
    var recovery: RecoveryCheckpoint?
    var effects: [ClientEffect]
}
```

`PreeditSpan`は少なくとも次を表す。

```swift
enum PreeditStyle {
    case plain
    case underline
    case active
}
```

候補選択中は原則として以下を返す。

```text
[選択候補: active] [残りの読み: underline]
                  ↑ caret
```

### 6.5 Client Effect

```swift
enum ClientEffect {
    case commitText(effectID: UInt64, text: String)
    case deleteSurroundingText(effectID: UInt64, before: Int, after: Int)
    case switchInputMode(effectID: UInt64, mode: InputMode)
    case notify(effectID: UInt64, message: String)
}
```

要件:

- `effectID`はセッション内で一意かつ単調増加。
- クライアントは適用済みIDを記録し、同じEffectを二重適用しない。
- 同じ`request_id`の再送には同じ応答を返す。
- commitを適用した応答が途中で失われても、再送時に二重確定しない。

---

## 7. Protocol v2

### 7.1 追加するメッセージ

概念例:

```proto
message HandleImeAction {
  string request_id = 1;
  uint64 expected_revision = 2;

  oneof action {
    InsertText insert_text = 10;
    DeleteBackward delete_backward = 11;
    DeleteForward delete_forward = 12;
    MoveCursor move_cursor = 13;
    MoveCursorToEdge move_cursor_to_edge = 14;
    StartConversion start_conversion = 15;
    NavigateCandidate navigate_candidate = 16;
    NavigateCandidatePage navigate_candidate_page = 17;
    ResizeSegment resize_segment = 18;
    CommitSelected commit_selected = 19;
    CommitAll commit_all = 20;
    Cancel cancel = 21;
    SelectCandidate select_candidate = 22;
    TransformActiveSegment transform_active_segment = 23;
    LifecycleEvent lifecycle_event = 24;
  }
}
```

```proto
message SessionSnapshot {
  uint64 revision = 1;
  ImePhase phase = 2;
  repeated PreeditSpan preedit = 3;
  optional uint32 caret_utf8_byte_offset = 4;
  CandidateWindow candidate_window = 5;
  repeated ClientEffect effects = 6;
  optional RecoveryCheckpoint recovery = 7;
}
```

### 7.2 version negotiation

`OpenSessionResult`に次を追加する。

- protocol version
- feature bitset
- max snapshot version
- recovery support有無
- idempotent request support有無

旧クライアントとの互換性を保つため、既存フィールド番号は変更しない。

### 7.3 revision規則

- 正常Actionごとにsession revisionを進める。
- 読み取り専用Actionを設ける場合はrevisionを進めない。
- `expected_revision`不一致時は状態を変更せず、最新Snapshotを返す。
- candidate選択には`candidate_generation`を必須にする。
- stale candidateは失敗させ、最新候補Snapshotを返す。

### 7.4 エラー規則

エラー応答は少なくとも次を区別する。

- retryable transport/server error
- session not found
- stale revision
- stale candidate generation
- invalid action for phase
- converter unavailable
- malformed request
- secure input policy violation

可能な限り、エラーとともに最後の有効Snapshotを返す。候補生成だけが失敗した場合、読みpreeditとcomposing phaseを維持する。

### 7.5 段階移行

1. サーバーにv2を追加し、v1を維持する。
2. Fcitxクライアントにv2経路を追加し、feature flagで選択する。
3. v2をCIと手動試験で既定化する。
4. rollback要件を満たす間はv1を残す。
5. v1利用が不要になった段階で、旧コマンドとC++側フラグを削除する。

同一sessionでv1とv2を混在させない。

---

## 8. P0 日本語IME操作契約

### 8.1 入力中カーソル編集

必須操作:

- Left / Right: 1入力単位または1書記素単位で移動
- Home / End: composition先頭・末尾
- Backspace: 左側削除
- Delete: 右側削除
- 途中位置への文字挿入
- Enter:現在表示中の内容を正確に確定
- Escape: phaseに応じて一段階戻る

テスト対象:

- ASCII
- ひらがな・カタカナ
- `𠮷`
- 絵文字
- variation selector
- 結合濁点
- ZWJ sequence
- ローマ字未完成状態
- カスタム入力テーブル

### 8.2 phase別キー仕様

#### idle

| 操作 | 結果 |
|---|---|
| 入力可能文字 | composingへ遷移 |
| Space / Shift+Space | 設定に従う半角・全角スペースを直接確定 |
| IME外ショートカット | applicationへfallthrough |

#### composing

| 操作 | 結果 |
|---|---|
| 文字入力 | カーソル位置へ挿入 |
| Left / Right | 入力カーソル移動 |
| Home / End | 先頭・末尾へ移動 |
| Backspace / Delete | 左・右削除 |
| Space / 変換 | previewingまたはselectingへ |
| Down | selectingへ |
| Shift+Left / Shift+Right | 文節境界変更後selectingへ |
| Enter | 表示中preeditを全確定 |
| Escape | compositionを取消 |
| F6–F10 | 全体を対象に文字種変換・確定、または仕様で定めた非確定変換 |

#### previewing

| 操作 | 結果 |
|---|---|
| Space / Down | selectingへ |
| Shift+Left / Shift+Right | 文節境界変更後selectingへ |
| Escape | composingへ戻る |
| Backspace | composingへ戻して削除 |
| Enter | 表示中の第一候補を全確定 |
| 文字入力 | 現在表示を確定して新しいcomposition開始、または明示した継続仕様 |

#### selecting

| 操作 | 結果 |
|---|---|
| Up / Down | 前後候補 |
| Space / Shift+Space | 次候補 / 前候補 |
| PageUp / PageDown | 候補ページ移動 |
| 数字キー | 表示候補の直接選択・確定 |
| 候補クリック | IDとgenerationを検証して確定 |
| Shift+Left / Shift+Right | アクティブ文節の縮小・拡大 |
| Right | 選択文節を部分確定し、残りへ進む |
| Enter | 選択候補を確定。残りがあれば次のphaseへ |
| Escape | previewingまたはcomposingへ一段階戻る |
| Backspace | composingへ戻して1文字削除 |
| F6–F10 | アクティブ文節だけを文字種変換 |
| modifierなしLeft | page移動には使わずconsume、または別途定めた安全な動作 |

### 8.3 文節編集

`ResizeSegment(delta:)`の要件:

- 選択候補がある場合、その`composingCount`にカーソル境界を合わせてから編集する。
- 初回のShift+Rightは末尾から先頭側の最小文節へ移るazooKey相当の挙動を検討する。
- 0文字文節を作らない。
- 境界変更後はcandidate generationを更新する。
- selectionを新候補の先頭へリセットする。
- preeditをactive候補＋残りの読みで返す。
- Escapeで全体変換またはcomposingへ戻れる。
- 文節編集は確定ではないため、学習を更新しない。

### 8.4 部分確定

要件:

- 選択候補が消費するprefixだけを確定する。
- commit文字列はSwift側からEffectで返す。
- `ComposingText.prefixComplete`後の残りを保持する。
- 残りがある場合、カーソルを末尾へ戻し、文節編集状態をリセットする。
- 確定文字列を新しい左文脈へ加え、残り候補を再計算する。
- 学習は実際に確定した候補だけを対象とする。
- secure inputまたはGrimodex policyで学習禁止の場合は更新しない。
- 部分確定済み文字列は後続Escapeで取り消さない。

### 8.5 候補操作

- Left/Rightによるページ移動を廃止する。
- PageUp/PageDownをページ移動へ割り当てる。
- Up/Downは候補単位。
- Space/Shift+Spaceは次・前候補。
- 数字選択は現在表示ページのrowとcandidate generationを検証する。
- クリック選択を実装する。
- 候補端で循環するか停止するかを設定または仕様として固定する。
- 候補再生成後、旧generationの操作を拒否する。

### 8.6 日本語キーボード固有操作

P0で仕様化・実装する。

- 変換
- 無変換
- かな
- 英数
- 半角／全角
- F6–F10
- Shift+Space
- JIS数字・記号入力
- US/JIS双方の物理キー差

無変換キーを未処理のままにしない。候補例:

- composing: ひらがな無変換または文字種循環
- selecting: 読み・カナ候補へ変更
- idle: 入力モード切替またはfallthrough

最終仕様はテストで固定する。

### 8.7 半角・全角スペース

設定値とShiftの排他的論理で決める。

```text
normal = fullwidth なら:
  Space       -> 全角
  Shift+Space -> 半角

normal = halfwidth なら:
  Space       -> 半角
  Shift+Space -> 全角
```

候補選択中のShift+Spaceは前候補なので、phaseに応じて意味を切り替える。

### 8.8 Focus・IME切替・Capability change

イベントごとの扱いを明示する。

| イベント | 原則 |
|---|---|
| IME deactivate | 設定した規則で確定または取消。暗黙動作をテストで固定 |
| 別InputContextへfocus | セッションを混ぜず、各contextの状態を保持または明示的に終了 |
| secure inputへ遷移 | context・Zenzai・学習・復旧永続化を即時無効化 |
| client preedit capability変更 | compositionを失わずrendererだけ切替 |
| アプリ終了 | pending learningをpolicyに従ってflush |
| Fcitx終了 | 二重commitせずsessionを閉じる |

### 8.9 変換失敗・再接続

- 候補0件でも読みを維持する。
- converter例外でもcomposingへ戻し、再試行可能にする。
- socket timeout時にpreeditを消さない。
- `SESSION_NOT_FOUND`時は新sessionを開き、checkpointまたはaction journalから復元する。
- 復元できない場合も、最後に表示していた読みを直接compositionとして再構築し、入力消失を避ける。
- commit response消失後の再送でも二重確定しない。
- サーバー再起動後、古いcandidate IDを確定しない。

### 8.10 マルチセッション・secure input

- InputContextごとに独立session。
- project conditionsはcomposition開始時にpinする。
- composition途中のproject revision更新は次compositionから反映する。
- secure inputではsurrounding textを送らない。
- secure inputではZenzaiを無効化する。
- secure inputでは学習しない。
- secure input用checkpointをディスクへ保存しない。
- context切替時に他sessionの候補・学習・preeditが漏れない。

---

## 9. P1 機能

P0完了後に同じ状態モデルへ追加する。

### 9.1 ユーザー辞書

- 単語・読み・品詞の登録
- 編集・削除
- 重複検出
- import/export
- system dictionary、個人辞書、Grimodex project dictionary、temporary shortcutsのレイヤー分離
- secure input・project policyとの独立性を明示

### 9.2 候補単位の忘却

- 選択候補に対する`ForgetCandidate`
- 既定キー候補: Ctrl+Delete
- 候補IDから正しい学習要素を特定
- 全履歴消去とは別API
- policyで学習禁止のsessionではno-opまたは明示応答

### 9.3 確定済み文字列の再変換

- surrounding text / selected text capability判定
- 再変換対象の削除とpreedit再構築
- 対応しないfrontendではfallthrough
- right-side contextの利用
- 置換Effectの冪等性
- reconverting phaseの追加

### 9.4 追加候補

- Unicode入力
- Home/End以外のEmacs風ショートカット
- 予測候補の受入れ
- 日時・記号・絵文字の追加候補表示
- プロファイル切替
- キーマップの高度なカスタマイズ

---

## 10. 実装フェーズ

大規模な単一PRを作らず、各フェーズを小さなreview可能単位に分ける。各PRは「挙動維持」または「一つの縦切り機能」のどちらかにする。

### Phase 0: 共通契約・仕様固定・基準テスト

#### Track 0A: Grimodex共通Composition Behavior Contract

- [ ] `kazormia296/Grimodex/ime-contract/composition-behavior-v1/`を追加する。
- [ ] 既存の辞書snapshot Protocol V1と、IME内部のComposition Behavior Contractを明確に分離する。
- [ ] `state-machine.md`へP0状態と遷移を定義する。
- [ ] semantic Action、Snapshot、Client EffectのJSON schemaを定義する。
- [ ] OS非依存の不変条件を定義する。
  - [ ] 入力消失禁止
  - [ ] 二重commit禁止
  - [ ] stale candidate確定禁止
  - [ ] secure inputでcontext・Zenzai・学習を停止
  - [ ] session間状態漏洩禁止
- [ ] 最小シナリオfixtureを追加する。
  - [ ] composing basic
  - [ ] cursor editing
  - [ ] segment editing
  - [ ] partial commit
  - [ ] Escape / Backspace
  - [ ] Unicode caret
  - [ ] stale candidate
  - [ ] server failure
  - [ ] secure input
- [ ] `platform-mapping.md`へLinux/macOS/Windowsのキー、caret単位、描画、lifecycle差分を記載する。
- [ ] `platform-exceptions.md`へ、共通契約との差異を登録する条件、理由、代替挙動、対象OS/version、解消予定を定義する。
- [ ] macOSの現行挙動を参照baselineとして記録する。
- [ ] Windowsの既知gapを記録し、Linux完了後の別EPICへ接続する。
- [ ] contract versioningと後方互換規則を定義する。

#### Track 0B: Linux/Fcitx5基準固定

- [ ] 本計画を`hazkey/docs/`へ追加する。
- [ ] Architecture Decision Recordを作る。
  - [ ] Swift側をsingle source of truthとする。
  - [ ] Action/Snapshot protocolへ移行する。
  - [ ] candidate generationとrequest idempotencyを導入する。
  - [ ] 共通契約とLinux wire protocolを分離する。
- [ ] 現行挙動をcharacterization testで固定する。
- [ ] 共通fixtureのLinux test adapterを作る。
- [ ] P0キー操作表をLinux固有key mappingのテストケースへ変換する。
- [ ] fake converterのinterfaceとfixtureを定義する。
- [ ] azooKey Desktopから参照する挙動と参照commitを記録する。
- [ ] ソースを相当量移植する場合のNOTICE方針を定める。

#### 完了条件

- 共通契約とLinux実装計画の境界が文書化されている。
- 最小共通fixtureがGrimodexリポジトリでversion管理されている。
- Linux test adapterがfixtureを読み込める。
- 既存CIが変更前後で同じ結果。
- ユーザー向け挙動変更なし。
- P0受け入れケースが追跡可能な共通テストIDを持つ。
- Windows/macOSの改修をLinux Phase 1の前提にしない。

---

### Phase 1: Swiftドメイン層の抽出

#### 作業

- [ ] `state.swift`から次を分離する。
  - [ ] `CompositionSession.swift`
  - [ ] `ImePhase.swift`
  - [ ] `ImeAction.swift`
  - [ ] `ImeReducer.swift`
  - [ ] `SessionSnapshot.swift`
  - [ ] `KanaKanjiConverterPort.swift`
- [ ] 現行コマンドを新しいdomain methodへ委譲するlegacy adapterを作る。
- [ ] candidate learningとGrimodex policyをdomain boundary越しに注入する。
- [ ] fake converterを用いた純粋状態遷移テストを追加する。

#### 完了条件

- wire protocolとUI挙動は未変更。
- 既存サーバー・Grimodexテストがすべて通る。
- converterなしでidle/composingの遷移をテストできる。

---

### Phase 2: Protocol v2と冪等性

#### 作業

- [ ] `protocol/commands.proto`へAction/Snapshot/Effectを追加する。
- [ ] `protocol/base.proto`へv2 payloadとcapability negotiationを追加する。
- [ ] Swift protobuf生成物を更新する。
- [ ] C++生成・リンク経路を更新する。
- [ ] `request_id`重複検出を実装する。
- [ ] `expected_revision`を検証する。
- [ ] Effect IDとクライアント側重複排除を実装する。
- [ ] candidate generationを導入する。
- [ ] v2のprocess E2E roundtrip testを追加する。
- [ ] hidden feature flagまたはprotocol negotiationでv1/v2を切り替える。

#### 完了条件

- v1とv2が同じサーバーバイナリで利用可能。
- duplicate requestで状態・commitが二重適用されない。
- stale revision / stale candidateのcontract testが通る。

---

### Phase 3: Snapshot Rendererとcaret修正

#### 作業

- [ ] Fcitx側に`HazkeySnapshotRenderer`を追加する。
- [ ] preedit spansをFcitx `TextFormatFlag`へ変換する。
- [ ] `caret_utf8_byte_offset`を正しく設定する。
- [ ] client preedit対応・非対応の両経路を実装する。
- [ ] active segment末尾へcaretを表示する。
- [ ] aux表示をSnapshot由来にする。
- [ ] Unicode offset unit testを追加する。
- [ ] 既存`HazkeyPreedit`をrendererの互換層へ縮小する。

#### 完了条件

- 先頭へ出ていた変換中caretが期待位置へ出る。
- `𠮷`、絵文字、結合文字を含んでもbyte offsetが壊れない。
- client preeditとinput panel preeditで同じ文字列・装飾になる。

---

### Phase 4: Composing Editor

#### 作業

- [ ] Left/Right
- [ ] Home/End
- [ ] Backspace/Delete
- [ ] 途中挿入
- [ ] Enter全確定
- [ ] Escape取消
- [ ] ローマ字未完成時のカーソル・削除仕様
- [ ] 空compositionへの遷移
- [ ] recovery checkpointの最小形式
- [ ] action journalの保持とack処理

#### 完了条件

- P0カーソル編集テストが通る。
- Fcitx側の`isCursorMoving_`を削除できる。
- transport失敗時も最後のpreeditを維持する。

---

### Phase 5: 候補UXの整理

#### 作業

- [ ] Up/Down候補移動
- [ ] Space/Shift+Space候補移動
- [ ] PageUp/PageDownページ移動
- [ ] Left/Rightページ移動を削除
- [ ] 数字選択をgeneration-aware化
- [ ] 候補クリックを実装
- [ ] 候補端挙動を固定
- [ ] candidate window pageとglobal indexのテスト
- [ ] stale click / stale keyの拒否

#### 完了条件

- 候補操作と文節操作のキーが競合しない。
- `HazkeyCandidateWord::select()`が実処理へ接続される。
- 古い候補ウィンドウのクリックで別候補を誤確定しない。

---

### Phase 6: 文節編集・部分確定の縦切り実装

#### 作業

- [ ] `ResizeSegment(delta:)`
- [ ] 選択候補の`composingCount`への境界同期
- [ ] 0文字文節防止
- [ ] 境界変更後の候補再生成
- [ ] active/unfocused preedit spans
- [ ] Shift+Left/Rightルーティング
- [ ] RightまたはEnterによる部分確定
- [ ] 残りcompositionの再変換
- [ ] 左文脈更新
- [ ] 候補単位学習
- [ ] 文節編集中のEscape/Backspace
- [ ] active segmentに限定したF6–F10

#### 完了条件

以下のシナリオが自動E2Eで通る。

```text
きょうはいしゃにいく
→ Space
→ Shift+Left/Rightで文節境界変更
→ 候補変更
→ 先頭文節だけ確定
→ 残りを再変換
→ Backspaceで修正
→ F7で現在文節をカタカナ化
→ Enterで最終確定
```

追加条件:

- 学習は確定候補だけに一度行われる。
- 途中Escapeで確定済みprefixを取り消さない。
- C++側でcommit文字列を再構築しない。

---

### Phase 7: 日本語キーボード・スペース・入力モード

#### 作業

- [ ] 変換
- [ ] 無変換
- [ ] かな
- [ ] 英数
- [ ] 半角／全角
- [ ] F6–F10のphase別scope
- [ ] Shift+Space反転
- [ ] JIS/USキーボード差
- [ ] カスタム入力テーブルとの相互作用
- [ ] keymap設定とsemantic actionの責務整理

#### 完了条件

- 未処理`Muhenkan`経路がない。
- 通常スペース設定に対してShift+Spaceが常に逆幅になる。
- JIS/US双方のkey fixtureが通る。

---

### Phase 8: Lifecycle・障害復旧・secure input

#### 作業

- [ ] activate/deactivate/focus change仕様
- [ ] capability change中のpreedit保持
- [ ] session not found復旧
- [ ] server restart復旧
- [ ] timeout/retry
- [ ] response loss後の冪等commit
- [ ] converter例外時の読み保持
- [ ] 候補0件fallback
- [ ] secure input遷移時のcontext/learning/Zenzai停止
- [ ] 複数InputContextの並行試験
- [ ] fault injection test

#### 完了条件

以下を満たす。

- 入力消失なし。
- 二重commitなし。
- 他sessionへのpreedit・候補・学習漏洩なし。
- secure inputから通常入力へ戻った後もsessionが正常。
- サーバー再起動後に古いcandidate IDを適用しない。

---

### Phase 9: P0互換性・リリースゲート

#### 作業

- [ ] GTK
- [ ] Qt
- [ ] Chromium/Electron
- [ ] terminal
- [ ] Wayland
- [ ] X11/XWayland
- [ ] client preeditあり・なし
- [ ] Fcitx最低対応版と現在対応版
- [ ] Arch/AUR
- [ ] Debian package
- [ ] Zenzai on/off
- [ ] Vulkan/CPU構成
- [ ] multi-session stress
- [ ] package contract test更新
- [ ] network/offline audit維持
- [ ] v2を既定化
- [ ] rollback switchを確認

#### 完了条件

- P0 Definition of Doneを満たす。
- package CIと既存Grimodex release gateが通る。
- blocker級の入力消失・二重確定・freezeがない。

---

### Phase 10: Legacy削除

#### 作業

- [ ] v1コマンド利用箇所を計測・確認する。
- [ ] `candidateList->focused()`依存を削除する。
- [ ] `isDirectConversionMode_`等の残存フラグを削除する。
- [ ] C++側の旧preedit再構築を削除する。
- [ ] 旧protocol handlerを削除する。
- [ ] 互換feature flagを削除または保守モードへ移す。
- [ ] README、設計文書、NOTICEを更新する。

#### 完了条件

- v2のみで全P0テストが通る。
- legacy削除前後でユーザー向け挙動が変わらない。
- サーバー状態の単一所有者がSwift CompositionSessionになっている。

---

### Phase 11: P1機能

独立したissue/PRとして進める。

- [ ] ユーザー辞書
- [ ] 候補単位忘却
- [ ] 再変換
- [ ] Unicode入力
- [ ] 追加ショートカット
- [ ] right-side context
- [ ] 高度な予測候補受入れ

---

## 11. ファイル別変更案

### 11.0 クロスリポジトリ配置

```text
kazormia296/Grimodex/
  ime-contract/
    protocol-v1/                         # 既存: Grimodex → IME snapshot契約
    composition-behavior-v1/             # 新規: OS非依存のIME挙動契約
      README.md
      state-machine.md
      actions.schema.json
      snapshots.schema.json
      platform-mapping.md
      platform-exceptions.md
      scenarios/
  docs/
    IME_PLATFORM_MATRIX.md

kazormia296/hazkey/
  docs/
    fcitx5-ime-architecture-overhaul.md  # 本計画

kazormia296/azooKey-Desktop/
  docs/
    composition-contract-conformance.md  # macOS適合監査

kazormia296/azooKey-Windows/
  docs/
    tsf-ime-architecture-overhaul.md     # Windows専用の別計画
```

各OSリポジトリは、利用するComposition Behavior ContractのversionまたはcommitをCIで固定する。共通fixtureの取得方法は、submodule、release artifact、検証付きcopyなど再現可能な方式を選び、開発者のローカルなGrimodex作業ツリーへ暗黙依存しない。

### 11.1 Protocol

| ファイル | 変更 |
|---|---|
| `protocol/base.proto` | v2 request/response、capability negotiation、status拡張 |
| `protocol/commands.proto` | ImeAction、Snapshot、Effect、candidate generation |
| `hazkey-server/scripts/generate-swift-protobuf.sh` | 生成対象・検証更新 |
| Swift生成済みprotobuf | schema更新に追従 |
| `fcitx5-hazkey/src/CMakeLists.txt` | 新proto・renderer/action mapperを追加 |

### 11.2 Swift server

| 現在/新規ファイル | 変更 |
|---|---|
| `state.swift` | legacy facadeへ縮小 |
| `CompositionSession.swift` | 状態の唯一の所有者 |
| `ImeAction.swift` | semantic action |
| `ImeReducer.swift` | phase別状態遷移 |
| `SessionSnapshot.swift` | preedit/candidate/effect構築 |
| `KanaKanjiConverterPort.swift` | converter抽象化 |
| `CandidateGeneration.swift` | candidate ID・generation |
| `RecoveryCheckpoint.swift` | 再接続復旧 |
| `protocolHandler.swift` | v2 dispatch、request idempotency |
| `hazkeySessionRegistry.swift` | v2 session capability、response cache |
| Grimodex integration各ファイル | pinned policyを新sessionへ注入 |

### 11.3 Fcitx client

| 現在/新規ファイル | 変更 |
|---|---|
| `hazkey_state.cpp/.h` | raw key orchestrationからaction dispatchへ縮小 |
| `hazkey_preedit.cpp/.h` | Snapshot renderer配下へ移行 |
| `hazkey_candidate.cpp/.h` | ID/generation付き候補、クリック実装 |
| `hazkey_action_mapper.cpp/.h` | KeyEvent→semantic action |
| `hazkey_snapshot_renderer.cpp/.h` | preedit/caret/candidate/aux描画 |
| `hazkey_effect_applier.cpp/.h` | idempotent commit等 |
| `hazkey_recovery_journal.cpp/.h` | 未確認Actionとlast snapshot |
| `hazkey_session_client.cpp/.h` | v2 negotiation、revision、retry |

### 11.4 Tests

| 場所 | 追加 |
|---|---|
| Grimodex contract tests | schema、scenario ID、OS非依存性、platform exception、fixture version |
| Swift unit tests | reducer、segment edit、partial commit、learning、共通scenario adapter |
| Swift fake converter tests | deterministic candidates、failure injection |
| protocol tests | version、revision、idempotency、stale generation |
| C++ unit tests | key mapping、UTF-8 caret、renderer、effect dedupe |
| process E2E | Unix socket、server restart、multi-session |
| package tests | 生成物、install path、offline/no-network |
| manual matrix | GTK/Qt/Electron、Wayland/X11、JIS/US |

---

## 12. テスト戦略

### 12.0 共通fixture適合試験

- 共通scenarioはGrimodex本体で、OS非依存のsemantic action列と期待snapshot/effectとして管理する。
- 比較対象はserializationや具体的UI objectではなく、phase、preeditの意味、caret境界、候補選択、Effect、revision、学習呼び出しとする。
- LinuxはscenarioをSwift domain actionへ変換し、Fcitx固有key mapping、renderer、transportは別テストで検証する。
- macOSは同じscenarioを既存`InputState` / `ClientAction` / `SegmentsManager`へ対応付ける。
- Windowsは同じscenarioを将来のserver-side actionへ対応付け、現時点のgapも未対応として可視化する。
- fake converterの固定候補、candidate ID、期待学習呼び出しは三OSで同じfixtureを使う。
- OS固有差分はfixtureを削除またはskipして隠さず、`platform-exceptions.md`へ理由、代替挙動、対象OS/version、解消条件を登録する。
- fixtureのschema versionとscenario IDを安定化し、破壊的変更時はcontract versionを上げる。旧versionは少なくとも一つの製品リリース移行期間を維持する。
- 各OSの結果を`IME_PLATFORM_MATRIX.md`へ反映する。LinuxではP0 release gate、macOSでは参照適合、Windowsでは後続overhaulのbaselineとして使う。

### 12.1 状態遷移テスト

表駆動で次を記述する。

```text
initial phase
initial composition
initial candidates
action
expected phase
expected composition
expected preedit spans
expected candidate generation
expected effects
expected learning calls
```

辞書やZenzaiに依存しない固定候補を返すfake converterを使う。OS非依存部分はGrimodexの`composition-behavior-v1/scenarios/`を読み込み、Linux固有のキー・renderer・transportテストは別fixtureで補う。

### 12.2 不変条件テスト

ランダムAction sequenceでも次を常に満たす。

1. caretはpreeditのUTF-8 byte長以内。
2. candidate selectionは現在generation内。
3. empty compositionでphaseがselectingにならない。
4. commit済みprefixが再度commitされない。
5. 同じrequest IDの再送でrevisionが増えない。
6. stale revisionで状態が変わらない。
7. secure inputで学習呼び出しがない。
8. session AのActionがsession Bを変更しない。
9. converter失敗で入力文字列が短くならない。
10. Effect IDが重複適用されない。

### 12.3 Unicode corpus

最低限:

- `あいう`
- `𠮷野家`
- `ば`（結合文字）
- `👨‍👩‍👧‍👦`
- `✈️`
- ASCII + 日本語混在
- 半角カナ
- supplementary plane文字
- variation selectorを含む固有名詞

### 12.4 Fault injection

- request送信後にsocket切断
- serverが状態更新後、response送信前に切断
- commit responseだけdrop
- session registryからsessionを削除
- converterがthrow
- candidatesが空
- Zenzaiだけ失敗
- malformed protobuf
- stale candidate click
- capability changeと変換要求の競合
- project revision更新とcompositionの競合

### 12.5 実アプリ試験

最低限の代表:

- GTKテキスト欄
- Qt/KDEテキスト欄
- Chromium系ブラウザ
- Electronアプリ
- terminal
- password field
- 複数ウィンドウ・複数入力欄


## 13. P0 Definition of Done

以下をすべて満たした時点で「日本語IMEとしての基本操作が実装済み」とする。

### 13.1 操作

- [ ] Left/Right/Home/End
- [ ] Backspace/Delete
- [ ] 途中挿入
- [ ] Escapeの段階的取消
- [ ] Up/Down/Space/Shift+Space候補移動
- [ ] PageUp/PageDown候補ページ
- [ ] 数字候補選択
- [ ] マウス候補選択
- [ ] Shift+Left/Right文節編集
- [ ] 部分確定
- [ ] 残り文節の再変換
- [ ] F6–F10
- [ ] 変換/無変換/かな/英数/半角全角
- [ ] 半角・全角スペース反転

### 13.2 正しさ

- [ ] active文節末尾にcaret表示
- [ ] Unicode offsetが正しい
- [ ] 表示文字列とcommit文字列が一致
- [ ] stale candidateを確定しない
- [ ] 確定候補だけを学習
- [ ] secure inputでcontext・Zenzai・学習が無効
- [ ] 複数InputContextが独立
- [ ] project policyがcomposition単位で固定

### 13.3 障害耐性

- [ ] 候補0件で入力が消えない
- [ ] converter失敗で入力が消えない
- [ ] socket timeoutで入力が消えない
- [ ] response再送で二重commitしない
- [ ] server restart後に復元または安全な読みfallback
- [ ] deactivate/focus changeで仕様外の自動確定がない
- [ ] Fcitx終了時に二重commitしない

### 13.4 品質

- [ ] Swift unit tests
- [ ] C++ unit tests
- [ ] protocol contract tests
- [ ] process E2E
- [ ] package CI
- [ ] GTK/Qt/Chromium/Electron手動試験
- [ ] Wayland/X11試験
- [ ] JIS/USキーボード試験
- [ ] Zenzai on/off試験
- [ ] Composition Behavior Contract v1の必須P0 scenarioへLinuxが適合
- [ ] Linux固有差分が`platform-mapping.md`へ記録済み
- [ ] macOS/Windowsの未完了がLinux releaseをブロックしない

### 13.5 クロスプラットフォーム契約

- [ ] Composition Behavior Contract V1が`kazormia296/Grimodex`に存在する。
- [ ] Linuxの全P0受け入れテストが共通scenario IDへ対応付いている。
- [ ] Linux固有key mappingとOS非依存state transitionのテストが分離されている。
- [ ] macOSの適合状況と意図的差分がplatform matrix / exceptionへ記録されている。
- [ ] Windows刷新計画の入力となる未適合scenario一覧が存在する。
- [ ] Hazkey Internal Protocol v2がGrimodex IME Snapshot Protocol V1と明確に分離されている。
- [ ] 各OSリポジトリが参照するcontract versionを再現可能に固定している。

---

## 14. 推奨Issue分割

現在のforkで新規に作る場合の例。

| ID | Issue | 依存 |
|---|---|---|
| IME-EPIC | Fcitx5 IME architecture overhaul and P0 parity | - |
| IME-001 | ADR: Swift CompositionSessionをsingle source of truthにする | EPIC |
| IME-002 | Extract converter port and pure reducer | 001 |
| IME-003 | Add Action/Snapshot protocol v2 | 002 |
| IME-004 | Add request idempotency, revision and effect IDs | 003 |
| IME-005 | Implement Fcitx snapshot renderer and UTF-8 caret | 003 |
| IME-006 | Implement composing cursor editor and Home/End | 005 |
| IME-007 | Separate candidate navigation from segment navigation | 005 |
| IME-008 | Implement candidate click with generation validation | 007 |
| IME-009 | Port segment resizing semantics from azooKey Desktop | 006,007 |
| IME-010 | Implement partial commit and remaining-segment conversion | 009 |
| IME-011 | Implement Japanese keyboard keys and space width rules | 006,007 |
| IME-012 | Add lifecycle, reconnect and no-input-loss recovery | 004,006 |
| IME-013 | Add multi-session and secure-input fault tests | 012 |
| IME-014 | Enable protocol v2 by default and complete compatibility matrix | 005-013 |
| IME-015 | Remove legacy command/state path | 014 |
| IME-101 | User dictionary CRUD/import/export | P0 |
| IME-102 | Forget selected candidate learning | P0 |
| IME-103 | Reconversion of committed text | P0 |


### 14.1 クロスリポジトリ作業項目

| ID | リポジトリ | Issue | 依存 |
|---|---|---|---|
| IME-CONTRACT-001 | `Grimodex` | Define Composition Behavior Contract v1 | - |
| IME-CONTRACT-002 | `Grimodex` | Add shared P0 scenario fixtures and platform matrix | 001 |
| IME-LINUX-EPIC | `hazkey` | Implement Fcitx5 architecture overhaul against contract v1 | 001 |
| IME-MAC-CONFORMANCE | `azooKey-Desktop` | Run shared fixtures and document intentional macOS differences | 002 |
| IME-WIN-AUDIT | `azooKey-Windows` | Audit split composition ownership and map gaps to contract v1 | 002 |
| IME-WIN-EPIC | `azooKey-Windows` | Move Windows composition authority server-side using a dedicated plan | WIN-AUDIT, Linux contract stabilization |

クロスリポジトリIssueは実装を同期させるためではなく、契約versionと適合状況を追跡するために使う。LinuxのIssueはWindows/macOSの完了を依存関係に持たせない。

---

## 15. PR運用方針

- 共通契約の変更は`Grimodex`へ先行PRとして追加し、各OSの実装PRは契約versionとテストIDを参照する。
- 1つのatomic mergeで複数リポジトリを同時変更しない。各repoは単独でbuild・test・rollback可能に保つ。
- OS実装が追従するまで、共通fixtureは加算的に変更し互換期間を設ける。
- Linux固有の都合を共通契約へ昇格させる場合、macOS参照挙動とWindowsでの実現可能性を確認する。
- 長期の巨大feature branchを避ける。
- schema変更は削除せず追加から始める。
- 一つのPRでconverter revision更新を行わない。
- 一つのPRでFcitx UI、protocol、Swift reducerを同時に大改造しない。ただし文節編集のように縦切りが必要な機能は、最小限の各層変更を一つのPRにまとめる。
- feature flag中でもCIではv1/v2両方を実行する。
- 各PRに次を記載する。
  - 変更する状態遷移
  - 変更しない状態遷移
  - protocol compatibility
  - rollback方法
  - 追加した受け入れテスト
- azooKey Desktopからコードを相当量コピーした場合は、元ファイル、commit、ライセンス、変更内容をPRとNOTICEへ記録する。

---

## 16. リスクと対策

| リスク | 対策 |
|---|---|
| Unicode単位の混同 | フィールド名に単位を含め、境界変換を一か所へ集約 |
| ローマ字入力と表示長の不一致 | `ComposingText.InputElement` / `ComposingCount`を正にする |
| 古い候補の誤確定 | candidate generation + opaque ID |
| response再送による二重commit | request ID cache + effect ID dedupe |
| サーバー再起動で入力消失 | checkpoint + action journal +読みfallback |
| C++/Swift二重状態 | Swift sessionを唯一の正とし、C++はlast snapshotのみ |
| converter forkとの差分拡大 | Converter Portで隔離し、更新PRを別にする |
| 学習の誤適用 | 確定Action内だけで更新し、fakeでcall count検証 |
| secure input漏洩 | policy invariant test、checkpoint非永続化 |
| Fcitx frontend差 | renderer abstractionとcapability fixture |
| 大規模移行のrollback不能 | v1/v2 dual stack、capability negotiation |
| upstream追従困難 | ported behaviorをテスト仕様として保持し、コピー量を抑える |
| OS間の挙動ドリフト | Grimodex共通fixture、platform matrix、contract versionで検出する |
| 共通化しすぎてOSネイティブAPIを阻害 | 共通化は意味論とfixtureに限定し、wire・描画・lifecycleはOS別に維持 |
| 3リポジトリ同時変更でLinuxが停滞 | Linuxを最初の実装対象とし、macOS/Windowsを非ブロッキングfollow-upにする |
| 未成熟な共有コード抽出 | 2 OS以上で重複と安定性が実証されるまで共有Packageを作らない |
| freeze/timeout | timeout、retryable error、preedit保持、fault injection |

---

## 17. 前提PRと最初のLinux 3 PR

### 前提PR 0: Grimodex Composition Behavior Contract v1

対象: `kazormia296/Grimodex`

- `ime-contract/composition-behavior-v1/`
- P0 state machine
- semantic Action / Snapshot / Effect schema
- 最小scenario fixture
- platform mapping
- 辞書snapshot Protocol V1との境界説明
- Linux、macOS、Windowsの適合状況欄

このPRはLinuxの内部protobufを定義しない。ユーザー可視の意味論とテスト入力・期待結果だけを固定する。

### PR 1: ADR・仕様テスト・Converter Port

- 設計文書
- P0キー仕様
- fake converter
- `KanaKanjiConverting`
- 現行挙動のcharacterization test
- 挙動変更なし

### PR 2: Swift CompositionSessionとSnapshot

- `ImePhase`
- `ImeAction`
- `ImeReducer`
- `SessionSnapshot`
- legacy command adapter
- reducer unit tests
- wire/UI変更なし

### PR 3: Protocol v2最小roundtrip

- `OpenSessionResult` capability
- `HandleImeAction`
- 最小Snapshot
- revision/request ID
- C++ client
- process E2E
- feature flag
- UI挙動変更なし

前提PR 0とLinux側のPR 1〜3を先に完了させると、以後のカーソル、候補、文節編集を新アーキテクチャ上で実装できる。現行`hazkey_state.cpp`へ一時的なフラグやcaseを追加してから後で作り直す経路を避けられる。

---

## 18. 他OSへの横展開計画

### 18.1 macOS (`kazormia296/azooKey-Desktop`)

macOS版はすでに明示的な`InputState`、`SegmentsManager`、server-owned candidate state、Snapshot/Effectに近い構造を持つため、本書のLinuxオーバーホールを移植しない。

実施対象:

- 共通fixtureを既存Core testへ接続する。
- macOS固有の入力規則を`platform-mapping.md`へ記録する。
- shared contractとの差異が意図的か、バグかを分類する。
- candidate generation、composition epoch、非同期応答のstale防止を監査する。
- Grimodex secure transition、generation pin、学習停止の共通不変条件を検証する。

非対象:

- Fcitx Protocol v2の移植
- Fcitx rendererやUnix socket復旧の移植
- macOS XPCをprotobufへ変更
- 上流azooKey Desktopの状態機械をLinux都合で作り替えること

### 18.2 Windows (`kazormia296/azooKey-Windows`)

Windows版はTSF/Rust側の`CompositionState`・preview・suffix・candidate stateと、Swift側の`ComposingText`が分散しているため、Linux契約安定後に別の全面改修計画を作る。

Windows専用計画の目標:

```text
TSF key event
    ↓
Windows Action Mapper
    ↓
Semantic ImeAction
    ↓
Server-owned CompositionSession
    ↓
SessionSnapshot + TSF ClientEffects
    ↓
TSF edit session / candidate UI renderer
```

主な移行項目:

- `AppendText`、`RemoveText`、`ShrinkText`等の手続きRPCを意味的Actionへ寄せる。
- Rustクライアント側のpreview、suffix、candidate選択をSnapshotの描画cacheへ縮小する。
- Swiftまたはserver-side domain層をcompositionの正とする。
- TSFのUTF-16 range、edit session、composition terminationをWindows adapterに閉じ込める。
- candidate generation、revision、Effect IDを導入する。
- 共通fixtureに加えてTSF固有のreentrancy、COM lifecycle、x64/x86試験を行う。

Windows改修はLinuxのコードをコピーする作業ではなく、同じ契約に対する別実装とする。

### 18.3 共有コード抽出の判断基準

`GrimodexIMECore`等の共有Swift Packageは、次をすべて満たすまで作らない。

- LinuxとWindows、またはLinuxとmacOSの2実装で同じdomain logicが実際に重複している。
- converter revision・Swift version・platform build条件を両立できる。
- OS設定、辞書パス、lifecycle、UI型を共有層へ持ち込まずに済む。
- 共通fixtureだけでは保守できない実質的なコード重複削減効果がある。
- 各上流forkへの追従コストが増えないことを確認できる。

順序は常に、**共有テスト → 共通意味論 → 各OS実装 → 必要なら共有コード**とする。

### 18.4 クロスプラットフォーム適合マトリクス

Grimodex本体に次の表を維持する。

| Contract機能 | Linux/Fcitx5 | macOS/IMKit | Windows/TSF |
|---|---|---|---|
| composing cursor | planned/implemented | conforming/difference | gap/planned |
| segment editing | planned/implemented | reference | gap/planned |
| partial commit | planned/implemented | reference | audit |
| stale candidate guard | planned/implemented | audit | planned |
| no-input-loss recovery | planned/implemented | platform test | planned |
| secure input invariants | implemented/tested | implemented/tested | implemented/tested |
| shared fixture version | v1 | v1 | v1 |

表のstatusはリリースを相互ブロックするためではなく、ユーザー可視差分と契約適合度を明示するために使う。

---

## 19. 実装上の最終判断

- Hazkey基盤は維持する。
- azooKey Desktopの**状態遷移と文節アルゴリズム**を参照・移植する。
- azooKey Desktopの全機能を一括移植しない。
- P0は「日常入力の操作契約」と「入力を失わない障害耐性」を同じ優先度で扱う。
- 文節編集を単独機能として先に差し込まず、CompositionSession、Snapshot、candidate generationの上に実装する。
- ユーザー辞書、候補忘却、再変換はP1として同じAction/Snapshotモデルへ追加する。
- legacy削除はv2既定化と全受け入れ試験完了後に行う。
- 本書はLinux/Fcitx5固有の実装計画として維持する。
- OS共通の入力・変換・候補・確定の意味論はGrimodex本体のComposition Behavior Contractへ置く。
- macOSは参照実装・適合試験とし、本オーバーホールをそのまま適用しない。
- WindowsはLinux契約安定後に専用計画で状態所有を再設計する。
- 3 OSのwire protocol、描画、lifecycle、物理キーはOS固有のまま維持する。
- 共通コード抽出より先に、共通fixtureと適合試験を整備する。
