# pg_pinyin

[中文说明](README.zh-CN.md)

`pg_pinyin` includes:

1. SQL baseline (`sql/pinyin.sql`)
2. Rust extension (`src/lib.rs`)

## Extension API (Reduced)

Only two APIs are exposed for romanization:

- `pinyin_char_romanize(text)`
- `pinyin_char_romanize(text, suffix text)`
- `pinyin_word_romanize(text)`
- `pinyin_word_romanize(text, suffix text)`
- `pinyin_word_romanize(tokenizer_input anyelement)` (overload; use `pdb` tokenizer input such as `name::pdb.icu::text[]`)
- `pinyin_word_romanize(tokenizer_input anyelement, suffix text)` (overload with user-table suffix)

Recommended usage:

1. char romanization + `pg_trgm`
2. word romanization + `pg_search`

## Generated Column Example (Raw SQL)

```sql
CREATE EXTENSION IF NOT EXISTS pg_pinyin;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE voice (
  id bigserial PRIMARY KEY,
  description text NOT NULL,
  pinyin text GENERATED ALWAYS AS (public.pinyin_char_romanize(description)) STORED
);

CREATE INDEX voice_pinyin_trgm_idx ON voice USING gin (pinyin gin_trgm_ops);

INSERT INTO voice (description) VALUES ('郑爽ABC');
SELECT id, description, pinyin FROM voice;
```

## User Dictionary Suffix Tables

You can provide custom dictionary tables in schema `pinyin` by suffix:

- `pinyin.pinyin_mapping_suffix1`
- `pinyin.pinyin_words_suffix1`

When calling `...(..., '_suffix1')`, romanization uses a merged dictionary:

1. base tables (`pinyin_mapping` / `pinyin_words`)
2. suffix tables (`pinyin_mapping_suffix1` / `pinyin_words_suffix1`) with higher priority

Example:

```sql
CREATE TABLE IF NOT EXISTS pinyin.pinyin_mapping_suffix1 (
  character text PRIMARY KEY,
  pinyin text NOT NULL
);

CREATE TABLE IF NOT EXISTS pinyin.pinyin_words_suffix1 (
  word text PRIMARY KEY,
  pinyin text NOT NULL
);

INSERT INTO pinyin.pinyin_mapping_suffix1 (character, pinyin)
VALUES ('郑', '|zhengx|')
ON CONFLICT (character) DO UPDATE SET pinyin = EXCLUDED.pinyin;

INSERT INTO pinyin.pinyin_words_suffix1 (word, pinyin)
VALUES ('郑爽', '|zhengx| |shuangx|')
ON CONFLICT (word) DO UPDATE SET pinyin = EXCLUDED.pinyin;

SELECT public.pinyin_char_romanize('郑爽ABC', '_suffix1');
SELECT public.pinyin_word_romanize('郑爽ABC'::pdb.icu::text[], '_suffix1');
```

## Extension-Bundled Dictionary Data

The Rust extension now embeds these dictionaries at build time:

- `sql/data/pinyin_mapping.csv`
- `sql/data/pinyin_token.csv`
- `sql/data/pinyin_words.csv`

During `CREATE EXTENSION pg_pinyin`, it seeds dictionary tables under schema `pinyin`
using PostgreSQL `COPY` from embedded CSV payloads (with SQL `INSERT` fallback).
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

The Dockerfiles use BuildKit cache mounts for Rust download/index caches.
If needed, ensure BuildKit is enabled:

```bash
DOCKER_BUILDKIT=1 docker build -f docker/Dockerfile.test-trixie -t pg_pinyin/test:trixie .
```

## Benchmark

Tokenization-only benchmark script:

- `scripts/benchmark_pg18.sh`

It measures:

- SQL char tokenizer: `characters2romanize(name)` (`cold` + `warm`)
- Rust char tokenizer: `pinyin_char_romanize(name)` (`cold` + `warm`)
- Rust char tokenizer (user suffix overlay): `pinyin_char_romanize(name, '_<suffix>')` (`cold` + `warm`)
- SQL word tokenizer: `icu_romanize(name::pdb.icu::text[])` (`cold` + `warm`, if `pg_search` exists)
- Rust word tokenizer with tokenizer input: `pinyin_word_romanize(name::pdb.icu::text[])` (`cold` + `warm`)
- Rust word tokenizer with suffix overlay: `pinyin_word_romanize(name::pdb.icu::text[], '_<suffix>')` (`cold` + `warm`)
- Rust word tokenizer with plain text input: `pinyin_word_romanize(name)` (`cold` + `warm`)

All benchmark queries use `EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)`.

Run:

```bash
ROWS=2000 USER_TABLE_SUFFIX=_bench PGURL=postgres://localhost/postgres ./scripts/benchmark_pg18.sh
```

### Benchmark Session (PG18)

Session command:

```bash
ROWS=2000 USER_TABLE_SUFFIX=_bench PGURL=postgres://postgres@localhost:5432/postgres ./scripts/benchmark_pg18.sh
```

Latest run (PG18, `ROWS=2000`, 2026-03-01):

Character mode:

| Scenario                                                                                          |       Cold |       Warm | Speedup vs SQL (`cold` / `warm`) |
| ------------------------------------------------------------------------------------------------- | ---------: | ---------: | --------------------------------: |
| SQL baseline (`characters2romanize`)                                                              | `9360.465` | `9669.367` |                       `1.0x / 1.0x` |
| Rust (`pinyin_char_romanize`)                                                                     |   `79.272` |   `27.318` |                     `118.1x / 353.9x` |
| Rust + suffix (`pinyin_char_romanize(name, '_bench')`)                                            |  `138.693` |   `11.000` |                      `67.5x / 879.0x` |

Word mode (`pg_search` tokenizer input):

| Scenario                                                                                          |       Cold |       Warm | Speedup vs SQL (`cold` / `warm`) |
| ------------------------------------------------------------------------------------------------- | ---------: | ---------: | --------------------------------: |
| SQL baseline (`icu_romanize(name::pdb.icu::text[])`)                                              |  `242.998` |  `233.098` |                       `1.0x / 1.0x` |
| Rust (`pinyin_word_romanize(name::pdb.icu::text[])`)                                              |  `336.220` |   `68.312` |                       `0.7x / 3.4x` |
| Rust + suffix (`pinyin_word_romanize(name::pdb.icu::text[], '_bench')`)                          |  `723.940` |   `47.809` |                       `0.3x / 4.9x` |
| Rust plain text (`pinyin_word_romanize(name)`)                                                    |  `351.177` |   `36.041` |                       `0.7x / 6.5x` |

Times above are `Execution Time` in milliseconds from `EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)`.
`cold` runs for Rust base paths force a dictionary version bump before execution to simulate first-use cache load.
Suffix warm performance requires suffix registration via `public.pinyin_register_suffix('_suffix')`, which installs version-bump triggers for user suffix tables.

## Roadmap

1. Tidy up the data generation pipeline and expand the word dictionary coverage.
2. ~~Support user-provided dictionaries and allow romanization against a specific dictionary set.~~
3. Provide a smooth upgrade path for extension dictionaries and user dictionaries.
4. Improve English handling (including stemming).
5. Provide better examples without `pg_search`.
6. Improve performance and memory balance (for example, evaluate frozen hash structures vs table lookups).

## User-Updatable Tables

All dictionaries remain runtime-editable:

- `pinyin.pinyin_mapping`
- `pinyin.pinyin_words`
- `pinyin.pinyin_token`

No extension rebuild is required after table updates.

## SQL Baseline Patent Citation

If you use the SQL-based romanization method (`sql/pinyin.sql`), cite:

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
