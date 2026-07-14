# IME base A/B fixture

This corpus is a small decision spike for comparing the dictionary-only
conversion core of Hazkey and a Mozc-family backend. It deliberately mixes
ordinary sentences, syntax boundaries, ASCII/Japanese input, proper nouns,
modern compounds, and short ambiguous readings.

It is not a release-quality language benchmark. Learning, project dictionary,
and neural reranking must be disabled, and both backends must report their
exact source revision and dictionary identity. Results produced with a missing
or locally generated dictionary are not interchangeable.
