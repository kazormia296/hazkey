# AJIMEE-Bench attribution and license notice

`external-ajimee-unconditional.tsv` is derived from
[azooKey/AJIMEE-Bench](https://github.com/azooKey/AJIMEE-Bench), revision
`401666cd56d1a570c2021798b64b6da4396bfd45`, file
`JWTD_v2/v1/evaluation_items.json` (SHA-256
`e9eb668fd6aa14b1e26436f429b5550108af0a1dfd443b8cea0bcb3ab3028fca`).

The upstream README states that this dataset follows the source dataset's
[Creative Commons Attribution-ShareAlike 3.0 license](https://creativecommons.org/licenses/by-sa/3.0/).
The upstream evaluation utilities are separately described as CC0; they are
not included in the derived TSV.

The derived TSV is distributed under CC BY-SA 3.0. It selects the 100 records
whose `context_text` is empty, converts the katakana input to hiragana with the
versioned repository transform, preserves distinct accepted outputs in their
upstream order, removes only exact duplicate outputs, and sorts by the numeric
upstream index. The derived file SHA-256 is
`91068dd92eddc70865c1b998843f38fd21d47458d1adf21799f9ad645e265fba`.
