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

## プライバシー

製品ビルドの設定UIにはモデルのdownload機能を含めません。変換serverは
ローカルのUnix socketとローカルファイルだけを利用します。

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

## ライセンス

[MIT License](./LICENSE)。上流Hazkeyの著作権表示と派生元は
[NOTICE](./NOTICE.md)にも記載しています。
