# pg_pinyin

`pg_pinyin` includes:

1. SQL baseline (`sql/pinyin.sql`)
2. Rust extension (`src/lib.rs`)

## Extension API (Reduced)

Only two functions are exposed:

- `pinyin_char_normalize(text)`
- `pinyin_word_normalize(text)`
- `pinyin_word_normalize(tokenizer_input anyelement)` (overload; use `pdb` tokenizer input such as `name::pdb.icu`)

Recommended usage:

1. char normalization + `pg_trgm`
2. word normalization + `pg_search`

## Data Prep (Moved + One-Shot)

Data prep logic is now in this repo:

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

Compatibility copies are also written to:

- `sql_patent/pinyin_mapping.csv`
- `sql_patent/pinyin_token.csv`

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

## Docker (Mirror + pgsty PGDG)

Dockerfiles:

- `docker/Dockerfile.test-trixie`
- `docker/Dockerfile.release-trixie`

Configured defaults:

- base image: `docker.m.daocloud.io/postgres:18.3-trixie`
- Debian apt mirror: Tsinghua (`mirrors.tuna.tsinghua.edu.cn`)
- PGDG packages: pgsty mirror (`repo.pigsty.cc/apt/pgdg`)
- rustup/cargo mirror: Tsinghua

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

- `public.pinyin_mapping`
- `public.pinyin_words`
- `public.pinyin_token`

No extension rebuild is required after table updates.

## Acknowledgements

- Hanzi word-to-pinyin TSV source: [tsroten/dragonmapper `hanzi_pinyin_words.tsv`](https://github.com/tsroten/dragonmapper/blob/main/src/dragonmapper/data/hanzi_pinyin_words.tsv)
