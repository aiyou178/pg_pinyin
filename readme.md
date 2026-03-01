# pg_pinyin

[中文说明](README.zh-CN.md)

Maintainer: Liang Zhanzhao

`pg_pinyin` includes:

1. SQL baseline (`sql/pinyin.sql`)
2. Rust extension (`src/lib.rs`)

## Extension API (Reduced)

Only two APIs are exposed for normalization:

- `pinyin_char_normalize(text)`
- `pinyin_word_normalize(text)`
- `pinyin_word_normalize(tokenizer_input anyelement)` (overload; use `pdb` tokenizer input such as `name::pdb.icu`)

Recommended usage:

1. char normalization + `pg_trgm`
2. word normalization + `pg_search`

## Extension-Bundled Dictionary Data

The Rust extension now embeds these dictionaries at build time:

- `sql/data/pinyin_mapping.csv`
- `sql/data/pinyin_token.csv`
- `sql/data/pinyin_words.csv`

On first use, it auto-seeds dictionary tables under schema `pinyin`.
No separate `sql/load_data.sql` step is required for extension usage.

## Data Prep (Moved + One-Shot)

Data prep logic is in this repo:

- `scripts/data/generate_extension_data.py` (optimized pipeline)
- `scripts/generate_data.sh` (one-shot entrypoint)

The project includes `mozillazg/pinyin-data` as submodule at:

- `third_party/pinyin-data`

Initialize submodule:

```bash
git submodule update --init third_party/pinyin-data
```

Generate all extension data in one command:

```bash
./scripts/generate_data.sh
```

Notes:

- char/token data is generated from `third_party/pinyin-data`.
- word data uses `hanzi_pinyin_words.csv` when available; otherwise an empty `pinyin_words.csv` is created.

Generated outputs:

- `sql/data/pinyin_mapping.csv`
- `sql/data/pinyin_token.csv`
- `sql/data/pinyin_words.csv`

If needed, override source repo:

```bash
PINYIN_DATA_DIR=/path/to/pinyin-data ./scripts/generate_data.sh
```

## Load SQL Baseline Data

```bash
psql "$PGURL" -f sql/pinyin.sql

psql "$PGURL" \
  -v mapping_file='/absolute/path/sql/data/pinyin_mapping.csv' \
  -v token_file='/absolute/path/sql/data/pinyin_token.csv' \
  -v words_file='/absolute/path/sql/data/pinyin_words.csv' \
  -f sql/load_data.sql
```

## Tests

pgTAP:

```bash
./test/pgtap/run.sh
```

Rust extension tests:

```bash
cargo pgrx test pg18 --features pg18
```

## Docker (General Upstream)

Dockerfiles:

- `docker/Dockerfile.test-trixie`
- `docker/Dockerfile.release-trixie`

Defaults now use upstream addresses (no mirror rewrite):

- base image: `postgres:18.3-trixie`
- apt source: base image defaults
- rustup/cargo source: upstream defaults

Build test image:

```bash
docker build -f docker/Dockerfile.test-trixie -t pg_pinyin/test:trixie .

# optional: pin pg_search version at build time
# docker build --build-arg PG_SEARCH_VERSION=0.21.10 -f docker/Dockerfile.test-trixie -t pg_pinyin/test:trixie .
```

Build release image:

```bash
docker build -f docker/Dockerfile.release-trixie -t pg_pinyin/release:trixie .
```

## Benchmark

Tokenization-only benchmark script:

- `scripts/benchmark_pg18.sh`

It measures:

- SQL char tokenizer: `characters2pinyin(name)`
- Rust char tokenizer: `pinyin_char_normalize(name)`
- SQL word tokenizer: `icu_romanize(name::pdb.icu)` (if `pg_search` exists)
- Rust word tokenizer: `pinyin_word_normalize(name::pdb.icu)` when `pg_search` exists, else `pinyin_word_normalize(name)`

Run:

```bash
ROWS=2000 PGURL=postgres://localhost/postgres ./scripts/benchmark_pg18.sh
```

## User-Updatable Tables

All dictionaries remain runtime-editable:

- `pinyin.pinyin_mapping`
- `pinyin.pinyin_words`
- `pinyin.pinyin_token`

No extension rebuild is required after table updates.

## SQL Baseline Patent Citation

If you use the SQL-based normalization method (`sql/pinyin.sql`), cite:

- CN115905297A: [一种支持拼音检索和排序的方法及系统](https://patents.google.com/patent/CN115905297A/zh)

BibTeX:

```bibtex
@patent{CN115905297A,
  author  = {Liang Zhanzhao},
  title   = {一种支持拼音检索和排序的方法及系统},
  number  = {CN115905297A},
  country = {CN},
  year    = {2023},
  url     = {https://patents.google.com/patent/CN115905297A/zh}
}
```

## Acknowledgements

- Hanzi word-to-pinyin TSV source: [tsroten/dragonmapper `hanzi_pinyin_words.tsv`](https://github.com/tsroten/dragonmapper/blob/main/src/dragonmapper/data/hanzi_pinyin_words.tsv)
