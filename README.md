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
artifact pathを再利用できます。Mozc modeではlearningとZenzaiは無効です。

主要なローカル検証:

```sh
ctest --test-dir build --output-on-failure
hazkey-server/scripts/swift-test.sh --traits ZenzaiSupport
python3 packaging/tests/package_contract_test.py
```

Swiftテスト用ラッパーは、固定済みのAzooKey依存を解決し、リポジトリで
管理する互換性・性能パッチをコンパイル前に適用します。CMakeとSwiftPMの
作業ディレクトリを共有する場合は`SWIFT_SCRATCH_PATH`を指定してください。

## ライセンス

[MIT License](./LICENSE)。上流Hazkeyの著作権表示と派生元は
[NOTICE](./NOTICE.md)にも記載しています。
