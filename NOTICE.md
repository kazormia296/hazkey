# Notices

Grimodex IME for Fcitx5 is derived from
[Hazkey](https://github.com/7ka-Hiira/hazkey), copyright 2024 Nanaka Hiira,
and retains the upstream MIT license notice in `LICENSE`.

The conversion engine is
[AzooKeyKanaKanjiConverter](https://github.com/azooKey/AzooKeyKanaKanjiConverter).
Bundled dictionaries, emoji data, Swift packages, protobuf, and optional local
Zenzai/llama components retain their respective upstream licenses.

The cross-platform behavior fixtures under
`hazkey-server/Tests/grimodex-spike/Fixtures/composition-behavior-v1` are a
verified copy of Grimodex's semantic contract. They contain behavior data, not
macOS or Windows implementation source. No azooKey Desktop or azooKey Windows
source was copied into the Linux runtime; those repositories remain reference
implementations connected only through the shared contract.
