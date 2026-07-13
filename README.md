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
文節操作中は、選択文節と残りの読みの間を表示専用の `│` で示します。

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

### ソースビルド・インストール手順

ninjaを利用します。

```sh
git clone --recursive https://github.com/kazormia296/hazkey.git
cd hazkey
mkdir build && cd build
cmake -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr -DGGML_VULKAN=OFF -G Ninja ..
ninja
sudo ninja install
```

主要なローカル検証:

```sh
ctest --test-dir build --output-on-failure
swift test --package-path hazkey-server --traits ZenzaiSupport
python3 packaging/tests/package_contract_test.py
```

## ライセンス

[MIT License](./LICENSE)。上流Hazkeyの著作権表示と派生元は
[NOTICE](./NOTICE.md)にも記載しています。
