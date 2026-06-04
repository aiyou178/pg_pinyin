# pg_pinyin

[中文说明](README.zh-CN.md)

`pg_pinyin` includes:

1. SQL baseline (`sql/pinyin.sql`)
2. Rust extension (`src/lib.rs`)

## Core Extension Public API

`CREATE EXTENSION pg_pinyin` installs the Rust-backed core API:

- `pinyin_char_romanize(text)`
- `pinyin_char_romanize(text, suffix text)`
- `pinyin_word_romanize(text)`
- `pinyin_word_romanize(text, suffix text)`
- `pinyin_word_romanize(tokenizer_input anyelement)` (overload; use `pdb` tokenizer input such as `name::pdb.icu::text[]`)
- `pinyin_word_romanize(tokenizer_input anyelement, suffix text)` (overload with user-table suffix)
- `pinyin_regex_phrase(text, slope integer DEFAULT NULL, max_expansions integer DEFAULT NULL, generated_pinyin boolean DEFAULT false)` (`pg_search` query helper; installed by `CREATE EXTENSION pg_pinyin` when `pg_search` is already enabled in the database, returns `pdb.query`)

`pinyin_regex_phrase` is a Rust-backend public API, but its return type is `pdb.query`, so `pg_search` must be enabled in the database before `CREATE EXTENSION pg_pinyin`. PostgreSQL extension scripts cannot reliably enable another extension while they are being installed. If `pg_pinyin` is installed before `pg_search`, the romanization APIs are still installed and `pinyin_regex_phrase` is installed as an error stub with a clear exception.

## Core Internal API

`CREATE EXTENSION pg_pinyin` also installs `pinyin_regex_phrase_patterns(text, generated_pinyin boolean DEFAULT false)`. It is a Rust-backed internal helper for `pinyin_regex_phrase`; application SQL should normally call `pinyin_regex_phrase(...)` instead.

`pinyin_regex_phrase_patterns` returns an empty `text[]` when the input is empty, whitespace-only, or cannot be parsed as pinyin tokens. SQL NULL input still returns SQL NULL because the function is strict.

## Optional pg_search SQL Helpers

`sql/word.sql` is not installed automatically by `CREATE EXTENSION pg_pinyin`. It is the SQL-backend companion to `sql/pinyin.sql`; load `sql/pinyin.sql` first and use it only in databases where `pg_search` is available. `pg_search` 0.24.0 must be preloaded before `CREATE EXTENSION pg_search`, for example by starting PostgreSQL with `-c shared_preload_libraries=pg_search`.

- `sql_pinyin_regex_phrase(text, slope integer DEFAULT NULL, max_expansions integer DEFAULT NULL, generated_pinyin boolean DEFAULT false)` (SQL tokenization, returns `pdb.query`)
- `sql_pinyin_regex_phrase_patterns(text, generated_pinyin boolean DEFAULT false)` (SQL helper returning regex phrase tokens as `text[]`)
`sql_pinyin_regex_phrase_patterns` follows the same empty-array behavior. `sql_pinyin_regex_phrase` and the Rust-backed `pinyin_regex_phrase` map an empty pattern array to `pdb.empty()` so they are safe to use with `@@@`; callers that want an empty user query to leave other filters unaffected should omit the `@@@` predicate or use `pdb.all()` explicitly.

Recommended usage:

1. char romanization + `pg_trgm`
2. word romanization + `pg_search`

Optional `pg_search` query helper:

```sql
CREATE EXTENSION IF NOT EXISTS pg_search;
CREATE EXTENSION IF NOT EXISTS pg_pinyin;
\i sql/pinyin.sql
\i sql/word.sql

CREATE TABLE voice (
  id bigserial PRIMARY KEY,
  description text NOT NULL,
  pinyin text GENERATED ALWAYS AS (public.pinyin_word_romanize(description)) STORED
);

CREATE INDEX voice_pinyin_bm25_idx
ON voice
USING bm25 (id, pinyin)
WITH (key_field='id');

-- Rust backend: CREATE EXTENSION pg_pinyin exports pinyin_regex_phrase()
-- when pg_search is already enabled in the database.
SELECT *
FROM voice
WHERE pinyin @@@ public.pinyin_regex_phrase('zhengshuang');

-- SQL backend fallback: use this when pg_pinyin is not installed.
SELECT *
FROM voice
WHERE pinyin @@@ public.sql_pinyin_regex_phrase('zhengshuang');
```

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
docker run -d --name pg_pinyin_test \
  -e POSTGRES_HOST_AUTH_METHOD=trust \
  pg_pinyin/test:trixie \
  postgres -c shared_preload_libraries=pg_search

# optional: use local mirrors for one-off local builds
docker build -f docker/Dockerfile.test-trixie -t pg_pinyin/test:trixie \
  --build-arg DEBIAN_MIRROR=http://mirrors.tuna.tsinghua.edu.cn/debian \
  --build-arg DEBIAN_SECURITY_MIRROR=http://mirrors.tuna.tsinghua.edu.cn/debian-security \
  --build-arg RUSTUP_DIST_SERVER=https://mirrors.tuna.tsinghua.edu.cn/rustup \
  --build-arg RUSTUP_UPDATE_ROOT=https://mirrors.tuna.tsinghua.edu.cn/rustup/rustup \
  --build-arg CARGO_REGISTRIES_CRATES_IO_INDEX=sparse+https://mirrors.tuna.tsinghua.edu.cn/crates.io-index/ \
  .

# optional: pin pg_search version at build time
# docker build --build-arg PG_SEARCH_VERSION=0.24.0 -f docker/Dockerfile.test-trixie -t pg_pinyin/test:trixie .
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
- SQL backend query builder: `sql_pinyin_regex_phrase_patterns(query)` plus `sql_pinyin_regex_phrase(query)` for `pdb.query` construction (`warm`, from `sql/word.sql`)
- Rust backend query-token builder: `pinyin_regex_phrase_patterns(query)` (`warm`, from `CREATE EXTENSION pg_pinyin`)
- Rust backend `pg_search` query builder: `pinyin_regex_phrase(query)` (`warm`, exported by `CREATE EXTENSION pg_pinyin` when `pg_search` is already enabled in the database)
- Standalone Rust query-token builder: `cargo run --release --bin benchmark_pinyin_regex_phrase -- --mode tokens`
- Standalone Python query-token builder: `scripts/benchmark_pinyin_regex_phrase.py --mode tokens` using the same tokenization output

All benchmark queries use `EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)`.

By default, `scripts/benchmark_pg18.sh` drops and recreates a dedicated benchmark database (`BENCHMARK_DATABASE=pg_pinyin_benchmark`) before each run, then refreshes runtime state before each benchmark task. This avoids collisions with previously loaded extensions, SQL helper definitions, BM25 indexes, bench tables, and Rust dictionary caches. Set `BENCHMARK_FRESH_DATABASE=0` only when you intentionally want to reuse the database named by `PGURL`.

Run:

```bash
ROWS=2000 REGEX_BENCH_ROWS=20000 USER_TABLE_SUFFIX=_bench PGURL=postgres://localhost/postgres ./scripts/benchmark_pg18.sh
```

Standalone helper benchmarks:

```bash
cargo run --release --bin benchmark_pinyin_regex_phrase -- --rows 20000 --runs 5 --mode tokens
python3 scripts/benchmark_pinyin_regex_phrase.py --rows 20000 --runs 5
```

### Benchmark Session (PG18)

Session command:

```bash
ROWS=2000 REGEX_BENCH_ROWS=20000 USER_TABLE_SUFFIX=_bench PGURL=postgres://postgres@localhost:5432/postgres ./scripts/benchmark_pg18.sh
```

Latest run (PG18, `pg_search=0.24.0`, fresh benchmark database, `ROWS=2000`, `REGEX_BENCH_ROWS=20000`, 2026-06-04):

Character mode:

| Scenario                                                                                          |       Cold |       Warm | Speedup vs SQL (`cold` / `warm`) |
| ------------------------------------------------------------------------------------------------- | ---------: | ---------: | --------------------------------: |
| SQL baseline (`characters2romanize`)                                                              | `10868.511` | `9967.466` |                       `1.0x / 1.0x` |
| Rust (`pinyin_char_romanize`)                                                                     |    `97.526` |   `34.443` |                     `111.4x / 289.4x` |
| Rust + suffix (`pinyin_char_romanize(name, '_bench')`)                                            |   `186.929` |   `37.632` |                      `58.1x / 264.9x` |

Word mode (`pg_search` tokenizer input):

| Scenario                                                                                          |       Cold |       Warm | Speedup vs SQL (`cold` / `warm`) |
| ------------------------------------------------------------------------------------------------- | ---------: | ---------: | --------------------------------: |
| SQL baseline (`icu_romanize(name::pdb.icu::text[])`)                                              |  `257.257` |  `254.385` |                       `1.0x / 1.0x` |
| Rust (`pinyin_word_romanize(name::pdb.icu::text[])`)                                              |  `367.215` |   `68.252` |                       `0.7x / 3.7x` |
| Rust + suffix (`pinyin_word_romanize(name::pdb.icu::text[], '_bench')`)                          |  `888.435` |   `78.558` |                       `0.3x / 3.2x` |
| Rust plain text (`pinyin_word_romanize(name)`)                                                    |  `367.883` |   `37.461` |                       `0.7x / 6.8x` |

Query-token builder (`pinyin_regex_phrase_patterns`, 20,000 rows):

| Scenario                                                                                          | Cold-ish / Best | Warm / Best | Notes |
| ------------------------------------------------------------------------------------------------- | --------------: | ----------: | ----- |
| SQL backend tokens (`sql_pinyin_regex_phrase_patterns`)                                           |    `1808.172` ms | `1772.008` ms | PostgreSQL SQL helper from `sql/word.sql` |
| Rust backend tokens (`pinyin_regex_phrase_patterns`)                                              |     `301.043` ms |  `304.826` ms | PostgreSQL UDF path, includes `text[]` return overhead |
| Rust backend generated-pinyin tokens (`pinyin_regex_phrase_patterns(query, true)`)                |              - |  `295.323` ms | PostgreSQL UDF path |
| Rust backend `pg_search` query (`pinyin_regex_phrase`)                                            |              - |  `328.285` ms | Public helper exported by `CREATE EXTENSION pg_pinyin` |
| Rust backend slope/max (`pinyin_regex_phrase(query, 2, 4096)`)                                    |              - |  `322.512` ms | Public helper exported by `CREATE EXTENSION pg_pinyin` |
| SQL backend + `pg_search` query (`sql_pinyin_regex_phrase`)                                       |              - | `1830.929` ms | Builds `pdb.query` |
| SQL backend + slope/max (`sql_pinyin_regex_phrase(query, 2, 4096)`)                               |              - | `1829.272` ms | Builds `pdb.query` |
| SQL backend generated-pinyin query (`sql_pinyin_regex_phrase(query, NULL, NULL, true)`)           |              - | `1833.291` ms | Builds `pdb.query` |

Standalone query-token builder (`tokens` mode, 20,000 rows, no PostgreSQL executor or SQL array overhead):

| Scenario | Best | Median | Best per row | Checksum |
| -------- | ---: | -----: | -----------: | -------: |
| Rust standalone | `3.642` ms | `3.656` ms | `0.182` us | `219996` |
| Python standalone | `34.272` ms | `34.550` ms | `1.714` us | `219996` |

Server-side full `pg_search` query benchmark (20,000 rows from `bench_pinyin_regex_queries`, no client round trip per query):

| Scenario | Execution Time | Notes |
| -------- | -------------: | ----- |
| Rust parser in PostgreSQL + `pg_search`, memoize enabled | `1542.793` ms | Query mix has 9 distinct queries; PostgreSQL memoizes repeated LATERAL results |
| Rust parser in PostgreSQL + `pg_search`, memoize disabled and JIT off | `7021.452` ms | Executes 20,000 BM25 lookups |

Full `pg_search` query benchmark (20,000 client queries against a 2,000-row BM25 table, same result checksum):

| Scenario | Best | Median | Best per query | Checksum |
| -------- | ---: | -----: | -------------: | -------: |
| Python client parse + `text[]` patterns + `pg_search` | `8554.249` ms | `8564.311` ms | `427.712` us | `1337644` |
| Rust in-Postgres parse + `pg_search` | `8873.015` ms | `8978.465` ms | `443.651` us | `1337644` |

The full client-query benchmark shows that once each query executes a real `pg_search` index lookup and a client/server round trip, parser cost is not the dominant factor. Keeping parsing in PostgreSQL is still useful when callers want a single parameterized SQL API, server-side batch queries, no client-side token dictionary, and no dynamic SQL construction.

Times above are `Execution Time` in milliseconds from `EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)`.
`cold` runs for Rust base paths force a dictionary version bump before execution to simulate first-use cache load.
Suffix dictionaries are cached on first use and reused across statements. If suffix tables are updated, clear cache with `public.pinyin_clear_suffix_cache('_suffix')` (or `public.pinyin_clear_suffix_cache()` for all).
The standalone Rust/Python query-token numbers intentionally exclude PostgreSQL executor, UDF, and SQL array materialization overhead; they compare only the tokenization and pattern construction path.

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
