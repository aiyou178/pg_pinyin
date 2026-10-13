# Dictionary Data Attribution

The extension code is MIT licensed. This does not replace the licenses of
third-party dictionary data embedded in a binary or copied into PostgreSQL.

Character readings come from [mozillazg/pinyin-data](https://github.com/mozillazg/pinyin-data).
Phrase readings and manual corrections come from
[mozillazg/phrase-pinyin-data](https://github.com/mozillazg/phrase-pinyin-data).
Their repository MIT notices are retained in the pinned submodules. Their READMEs
also identify original data contributors and upstream sources; the repository
license is not a blanket clearance for every upstream dataset.

The imported `cc_cedict.txt` is a syllable-preserving extract of **CC-CEDICT**,
created by CC-CEDICT contributors, published by MDBG, and extracted by
phrase-pinyin-data. CC-CEDICT is distributed under
[Creative Commons Attribution-ShareAlike 4.0 International](https://creativecommons.org/licenses/by-sa/4.0/),
as stated on the [publisher's data page](https://www.mdbg.net/chinese/dictionary?page=cc-cedict).
Coverage restoration also uses original CC-CEDICT V1 records, with both
traditional and simplified headwords, from these retained snapshots:

- MDBG, 2026-10-12T08:13:53Z, CC-BY-SA-4.0 (official download).
- MDBG, 2019-05-27T20:06:35Z, CC-BY-SA-4.0 (raw mirror in yishn/chinese-tokenizer).
- MDBG, 2013-08-28T08:05:53Z, **CC-BY-SA-3.0 Unported** (raw mirror in regisb/CC-CEDICT).

The vendored gzip files in `third_party/cc-cedict` retain their original copyright
headers and licenses. `CC-BY-SA-3.0.txt` and `CC-BY-SA-4.0.txt` accompany releases.
The 3.0-origin adaptation is distributed under the later 4.0 license with the
same license elements, as permitted by [3.0 Section 4(b)](https://creativecommons.org/licenses/by-sa/3.0/legalcode.en).
This does not relabel the original 2013 snapshot as 4.0.

The generated phrase dictionary includes a modified CC-CEDICT extract:
tones are removed, umlauts become `v`, readings are reconciled by source priority,
and unsupported/non-aligned entries are excluded. Distribute this combined
`pinyin_words.csv` and its adaptations under CC-BY-SA-4.0, retaining this
attribution and identifying further modifications. This data notice does not
change the license of independently written Rust/Python/SQL code. Explicit
`r5` aligned with `儿/兒` is serialized as `er` for existing-index compatibility;
raw phonetic evidence and this modification remain in provenance. The released
mixed-script key `遵时養晦` is retained as one line-bound alias of the explicitly
paired source forms `遵時養晦`/`遵时养晦`. Historical readings preserve released
coverage, not a claim that every old dictionary entry is current or independently
verified.

Exact revisions, source SHA-256 hashes and reconciliation priorities are recorded
in `scripts/data/sources.lock.json`; generated hashes/counts are in `manifest.json`.
No `large_pinyin.txt`, scraped HanDian tables, private sibling repository or
network download is implicitly merged by the generator. Licensing and coverage
must be reviewed when adding a new source.
