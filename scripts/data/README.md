# Dictionary Generation

The generator uses only files pinned by `sources.lock.json`. Character readings
come from `pinyin-data`; phrase readings come from the selected
`phrase-pinyin-data` files. Source licensing and attribution remain separate from
the extension's license. See the lock and third-party notices for exact sources.
Coverage restoration additionally reads pinned raw CC-CEDICT snapshots with
both traditional and simplified headwords. These files are vendored, not fetched
at generation time. The old release CSV is a comparison baseline only, never a
pronunciation source.

```bash
git submodule update --init --recursive
./scripts/generate_data.sh
```

All source files are SHA-256 checked before parsing. Missing, modified, or empty
phrase sources fail without replacing the current dictionaries. No local sibling
checkout, environment-dependent normalizer, or network download is consulted.

## Normalization and Selection

- NFC and NFD tone-marked syllables, `u:`, `v`, and trailing tone numbers `0` to
  `5` are normalized to lowercase toneless ASCII. Umlaut `u` becomes `v` before
  tone marks are removed; official `ju`, `qu`, and `xu` remain unchanged.
- Phrase syllables must already be separated by whitespace and align one-to-one
  with the Han characters. Joined pinyin is never greedily resegmented.
- Raw CC-CEDICT V1 entries retain both written forms and numbered syllables.
  Baseline-restoration sources only supply keys in the locked old release,
  below the priority of existing P0 corrections. The valid legacy singleton
  `葛` is retained; other single-character records remain outside phrase import.
  Explicit `r5` aligned with `儿/兒` becomes `er` for compatibility with the
  existing index format, with raw syllables and this policy retained in reports.
- Upstream `_2`, `_3`, and subsequent variant-key suffixes identify alternative
  readings of the same phrase. The original key remains in report provenance.
- Canonical readings are selected by descending source priority, then source ID,
  then source line number. Repeated equal sequences consolidate their provenance;
  different sequences remain visible in the conflict report. No Cartesian
  product of character readings is generated.
- Invalid syllables, unknown inventory syllables, single-character rows,
  non-Han words, and unaligned/erhua rows are rejected and reported. A reading
  absent from its corresponding character's readings is retained if it exists
  in the overall syllable inventory, and reported as a diagnostic, not an error
  rate.

## Outputs

`sql/data/` contains the three runtime CSV files and `manifest.json`, which
records source metadata, counts, and SHA-256 hashes of generated artifacts.
`sql/data/reports/` contains:

- `conflicts.json`: the selected sequence and every distinct alternative, with
  source IDs, source lines, original keys, and raw syllables.
- `rejected.json`: each skipped invalid row and its reason.
- `char_reading_mismatches.json`: accepted phrase/character reading differences.
- `coverage_audit.json`: every word absent from initial P0, its old/new sequence,
  selected source line and raw syllables; corrected retained words, coverage
  counts and a sorted `restored_words` list for benchmark strata.

The locked `scripts/data/baselines/v0.0.8-pinyin_words.csv` checks **key coverage**,
not just row count. Generation refuses any baseline deletion, even when new
words would make the overall count larger. Restored or changed words must have
selected source provenance. The initial P0 source set is computed separately
before baseline restoration so the original 64,091-word loss stays auditable.

Outputs are deterministically sorted and contain neither timestamps nor local
absolute paths. Two runs with identical locked sources produce identical bytes.
All artifacts are computed and staged before any destination file is replaced.

```bash
./scripts/generate_data.sh --output-dir /tmp/pg-pinyin-data
python3 -m unittest discover -s scripts/data/tests -v
```

## Custom Sources

Create a custom lock with repository-relative paths, exact SHA-256 hashes,
repository/revision/license metadata, and integer phrase priorities. Supported
phrase formats are `phrase` (`word: syllable syllable`), `csv` (exactly two
columns: Han phrase and whitespace-separated syllables), and `cedict` (raw V1
traditional/simplified headwords plus explicit numbered syllables). An unpinned file or
joined-pinyin legacy CSV is not an input override.

```bash
./scripts/generate_data.sh \
  --sources-lock scripts/data/custom.sources.lock.json \
  --output-dir /tmp/pg-pinyin-custom
```

Optional `--mapping-out`, `--token-out`, `--words-out`, and `--report-dir` control
destinations. Legacy `--source-dir` and `--words-source` are integrity checks
against lock-listed paths, not ways to bypass the lock.
