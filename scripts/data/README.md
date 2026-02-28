# Data Generation

This folder contains the local data-prep pipeline for `pg_pinyin`.

The logic is ported from the `aiyou178/pinyin-data` scripts and optimized for this extension:

- `prepare_tables.py`
- `prepare_words.py`
- `prepare_regex.py`

## Entry Points

- one-shot: `../generate_data.sh`
- direct: `generate_extension_data.py`

## Outputs

- `sql/data/pinyin_mapping.csv`
- `sql/data/pinyin_token.csv`
- `sql/data/pinyin_words.csv`

## Usage

```bash
./scripts/generate_data.sh
```

Advanced:

```bash
python3 scripts/data/generate_extension_data.py \
  --source-dir third_party/pinyin-data \
  --words-source /path/to/hanzi_pinyin_words.csv \
  --mapping-out sql/data/pinyin_mapping.csv \
  --token-out sql/data/pinyin_token.csv \
  --words-out sql/data/pinyin_words.csv
```
