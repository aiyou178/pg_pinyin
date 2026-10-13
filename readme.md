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
- `pinyin_word_romanize_batch(origins text[])` (independent plain-text documents, returns `text[]`)
- `pinyin_word_romanize_batch(origins text[], suffix text)` (document batch with user-table suffix)
- `pinyin_regex_phrase(text, slope integer DEFAULT NULL, max_expansions integer DEFAULT NULL, generated_pinyin boolean DEFAULT false)` (`pg_search` query helper; installed by `CREATE EXTENSION pg_pinyin` when `pg_search` is already enabled in the database, returns `pdb.query`)
- `pinyin_regex_phrase_mode(text, mode text, slope integer DEFAULT NULL, max_expansions integer DEFAULT NULL, generated_pinyin boolean DEFAULT false)` (opt-in explicit query modes; same installation requirement)

`pinyin_regex_phrase` is a Rust-backend public API, but its return type is `pdb.query`, so `pg_search` must be enabled in the database before `CREATE EXTENSION pg_pinyin`. PostgreSQL extension scripts cannot reliably enable another extension while they are being installed. If `pg_pinyin` is installed before `pg_search`, the romanization APIs are still installed and `pinyin_regex_phrase` is installed as an error stub with a clear exception.

## Core Internal API

`CREATE EXTENSION pg_pinyin` also installs `pinyin_regex_phrase_patterns(text, generated_pinyin boolean DEFAULT false)`. It is a Rust-backed internal helper for `pinyin_regex_phrase`; application SQL should normally call `pinyin_regex_phrase(...)` instead.

`pinyin_regex_phrase_patterns` returns an empty `text[]` when the input is empty, whitespace-only, or cannot be parsed as pinyin tokens. SQL NULL input still returns SQL NULL because the function is strict.

`pinyin_regex_phrase_patterns_mode(text, mode text, generated_pinyin boolean DEFAULT false)`
is the mode-aware internal helper. Its Rust tokenizer uses embedded data without
runtime SPI, retaining `PARALLEL SAFE`. The original helpers and their signatures
remain unchanged, including untyped NULL arguments.

## Optional pg_search SQL Helpers

`sql/word.sql` is not installed automatically by `CREATE EXTENSION pg_pinyin`. It is the SQL-backend companion to `sql/pinyin.sql`; load `sql/pinyin.sql` first and use it only in databases where `pg_search` is available. `pg_search` must be preloaded before `CREATE EXTENSION pg_search`, for example by starting PostgreSQL with `-c shared_preload_libraries=pg_search`.

- `sql_pinyin_regex_phrase(text, slope integer DEFAULT NULL, max_expansions integer DEFAULT NULL, generated_pinyin boolean DEFAULT false)` (SQL tokenization, returns `pdb.query`)
- `sql_pinyin_regex_phrase_patterns(text, generated_pinyin boolean DEFAULT false)` (SQL helper returning regex phrase tokens as `text[]`)
- `sql_pinyin_regex_phrase_mode(text, mode text, slope integer DEFAULT NULL, max_expansions integer DEFAULT NULL, generated_pinyin boolean DEFAULT false)` (independent SQL mode-aware query builder)
- `sql_pinyin_regex_phrase_patterns_mode(text, mode text, generated_pinyin boolean DEFAULT false)` (independent SQL mode-aware pattern helper)
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

### Explicit Query Modes

Development version 0.0.9 adds separate mode-aware interfaces, not ambiguous
overloads of legacy functions. Use a mode matching the user's query intent:

| Mode | Input | Plain term patterns |
| --- | --- | --- |
| `full` | `xi an` or `xi'an` | `xi`, `an` |
| `initials` | `whh` | `w.*`, `h.*`, `h.*` |
| `initials` | `zh ch sh` | `zh.*`, `ch.*`, `sh.*` |
| `autocomplete` | `xi a` | `xi`, `a.*` |
| `compat` | `xi an` | Original `xi.*`, `an.*` |

```sql
SELECT * FROM voice
WHERE pinyin @@@ public.pinyin_regex_phrase_mode('xi an', 'full');
SELECT * FROM voice
WHERE pinyin @@@ public.pinyin_regex_phrase_mode('whh', 'initials');
SELECT * FROM voice
WHERE pinyin @@@ public.sql_pinyin_regex_phrase_mode('xi a', 'autocomplete');
```

New modes normalize `ü`, decomposed umlauts, `u:` and `v` to `v`, remove tone
marks, and treat numeric tones 0-5/apostrophes as syllable boundaries (`lv4liang2`
becomes `lv liang`). `compat` preserves the original ASCII-only behavior. Joined
input still uses longest-first segmentation, not contextual disambiguation;
use explicit boundaries for `xin gan` versus `xing an`. Compact initials split
one letter at a time; only a complete explicit `zh`/`ch`/`sh` chunk is a digraph.
Autocomplete always prefix-expands its final token, even after trailing whitespace.
A valid final syllable prefix stays whole before greedy splitting: `yon` becomes
`yon.*`, not `yo`, `n.*`; `niyon` becomes `ni`, `yon.*`.

`full` matches exact indexed syllable terms, not the entire document: `an` no
longer expands to `ang`. New `generated_pinyin=true` patterns accept both plain
word-hit terms and pipe-delimited alternatives, bounding both pipes so `an`
does not match `|ang|`. Such mixed fields require a whitespace tokenizer that
preserves pipes, for example an index on `(pinyin::pdb.whitespace)`. Structured
queries use `WHERE pinyin @@@ ...`, without the tokenizer cast on the left.
This flag is not needed for Rust's plain word output.
No index contents or dictionary coverage are reduced by selecting a query mode.

New modes reject unsupported punctuation/malformed tones and inputs over 256
Unicode characters or 64 tokens with an empty pattern array / `pdb.empty()`;
they never search a truncated prefix. Unknown modes raise SQLSTATE `22023`, and
SQL NULL inputs remain NULL. Existing installations use
`ALTER EXTENSION pg_pinyin UPDATE TO '0.0.9'` after installing the new binary and
migration; live dictionaries and stored generated values are not overwritten.
Run the upgrade from a new database connection after replacing the Rust library,
so an existing connection does not retain the previously loaded binary.

## Token Arrays and Document Batches

The unreleased development version 0.0.9 reads real `text[]` tokenizer input directly in Rust,
without the previous SPI/JSON serialization. Existing `anyelement` signatures,
function OIDs and generated-column bindings are unchanged; castable non-`text[]`
inputs retain the legacy fallback. Tokens are trimmed, empty tokens dropped,
and wholly ASCII-alphanumeric tokens lowercased. NULL token elements still raise
an error. Array dimensions/lower bounds do not affect row-major token order.

The new `pinyin_word_romanize_batch` API has different semantics: each element
is a complete, independent plain-text document, not a token to concatenate.
It reuses one live dictionary-version check and dictionary snapshot per explicit
batch. Ordinary calls still check the version on every invocation. Cache identity
also includes the metadata row's `xmin`, preventing reused version counters
after savepoint rollback from serving an earlier uncommitted dictionary. Suffix
overrides follow the existing cache-clear and base-version invalidation rules.
The batch functions are `STABLE`, `STRICT`, `PARALLEL SAFE` Rust functions; use
the existing scalar `IMMUTABLE` API for stored generated columns.

```sql
-- Tokens of ONE document; existing calls also receive the fast path.
SELECT public.pinyin_word_romanize(ARRAY['吕', '梁', 'ABC']);
-- lv liang abc

-- THREE independent documents; NULL positions are retained.
SELECT public.pinyin_word_romanize_batch(ARRAY['吕梁', NULL, '银行']);
-- {"lv liang",NULL,"yin hang"}

-- Pair batch output with source IDs for bounded, explicit INSERT batches.
INSERT INTO imported_voice (id, description, pinyin)
SELECT ids[i], documents[i], outputs[i]
FROM (SELECT ARRAY[1, 2]::bigint[] AS ids,
             ARRAY['吕梁', '银行']::text[] AS documents) AS batch
CROSS JOIN LATERAL public.pinyin_word_romanize_batch(documents) AS converted(outputs)
CROSS JOIN LATERAL generate_subscripts(documents, 1) AS item(i);
```

Here `imported_voice.pinyin` is an ordinary text column, not a generated column.
Use bounded batches rather than aggregating an entire import into one array.
The result is a one-dimensional array with lower bound 1; multidimensional input
is flattened in storage order. SQL NULL input returns NULL, an empty array stays
empty, and NULL document elements are preserved. Empty/all-NULL batches do not
load dictionaries. No dictionary entries, output representation or index content
are removed by these optimizations. The independent SQL backend is unchanged.
Install the new library/migration and upgrade from a new connection with
`ALTER EXTENSION pg_pinyin UPDATE TO '0.0.9'`. Version 0.0.8 remains the latest
published release; these changes extend the existing 0.0.8-to-0.0.9 migration.

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

The project pins its character and syllable-preserving phrase sources as submodules:

- `third_party/pinyin-data`
- `third_party/phrase-pinyin-data`

Initialize submodule:

```bash
git submodule update --init third_party/pinyin-data third_party/phrase-pinyin-data
```

Generate all extension data in one command:

```bash
./scripts/generate_data.sh
```

Notes:

- Source SHA-256 hashes, revisions and priorities are locked in `scripts/data/sources.lock.json`.
- Character/token data preserves umlauts as ASCII `v`; phrase data retains the source's explicit syllables (`西安 -> xi an`, not `xian`).
- Manual phrase corrections take priority over base phrases, then CC-CEDICT. Alternative readings and rejected records are reported, not silently discarded or greedily resegmented.
- Missing or changed sources fail before output replacement. No private sibling repository or implicit network fetch is used.
- Raw CC-CEDICT keeps both traditional and simplified headwords; lower-priority restoration sources preserve all released v0.0.8 keys. A hash-locked baseline checks key coverage, not just word count.
- `coverage_audit.json` traces all 64,091 keys missing from the initial P0 selection to explicit source syllables. The old CSV is only a comparison baseline, never a pronunciation source.

Generated outputs:

- `sql/data/pinyin_mapping.csv`
- `sql/data/pinyin_token.csv`
- `sql/data/pinyin_words.csv`
- `sql/data/manifest.json` and `sql/data/reports/*.json`

See [the data pipeline](scripts/data/README.md), [data attribution](sql/data/NOTICE.md),
and [the P0/P1/P2 specification](spec/pinyin-data-and-retrieval.md).
Code is MIT; CC-CEDICT-derived phrase data carries separate CC-BY-SA attribution/share-alike terms.

Run the targeted accuracy tests after generation:

```bash
python3 -m unittest discover -s scripts/data/tests
python3 test/test_dictionary_accuracy.py
```

In the shared-path test container, also run the SQL-only loader/rollback guard:
`PGURL="$PGURL" PG_LOADER_TESTS=1 python3 -m unittest discover -s test`.

Existing installations keep their live dictionary tables. Back up custom base
entries before explicitly reloading these CSVs (or invoking the internal
`pinyin__seed_embedded_data()` with a newly built binary). Stored generated values
must be refreshed, for example `UPDATE voice SET description = description`,
and expression indexes rebuilt. Updating dictionary tables alone does not
rewrite existing stored values. Suffix dictionaries remain independent.

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

PostgreSQL 19 beta 4 is supported through `pgrx` 0.19.3:

```bash
cargo pgrx init --pg19=/usr/lib/postgresql/19/bin/pg_config
cargo pgrx install --features pg19 --no-default-features --pg-config /usr/lib/postgresql/19/bin/pg_config
```

Full Python integration and genuine upgrade tests require psycopg 3, an admin
`PGURL` able to create databases, and preloaded pg_search 0.26.1. After initializing
pgrx for the server, install the old catalog in its isolated Cargo target, then
restore the current library before testing (PG19 example):

```bash
export PG_CONFIG=/usr/lib/postgresql/19/bin/pg_config
bash test/prepare_upgrade.sh v0.0.8
cargo pgrx install --features pg19 --no-default-features --pg-config "$PG_CONFIG"
cp pg_pinyin--*--*.sql "$("$PG_CONFIG" --sharedir)/extension/"
PGURL="$PGURL" PG_LOADER_TESTS=1 TEST_UPGRADE_DSN="$PGURL" python3 -m unittest discover -s test
python3 -m unittest discover -s scripts/data/tests
cargo test --lib --no-default-features --locked
```

The database tests create and clean up their own temporary databases. pgTAP also
checks real parallel workers executing both mode-aware public and pattern APIs.

## Docker (General Upstream)

Dockerfiles:

- `docker/Dockerfile.test-trixie`
- `docker/Dockerfile.test-pg19beta4-trixie`
- `docker/Dockerfile.release-trixie`

Defaults now use upstream addresses (no mirror rewrite):

- base image: `postgres:18.3-trixie` for PG18 tests, `postgres:19beta4-trixie` for PG19 beta tests
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
# docker build --build-arg PG_SEARCH_VERSION=0.26.1 -f docker/Dockerfile.test-trixie -t pg_pinyin/test:trixie .
```

Build PostgreSQL 19 beta test image:

```bash
docker build -f docker/Dockerfile.test-pg19beta4-trixie -t pg_pinyin/test:pg19beta4 .
```

Test images now pin [pg_search 0.26.1](https://github.com/paradedb/paradedb/releases/tag/v0.26.1),
the latest stable release verified on 2026-10-13. ParadeDB does not publish a
PostgreSQL 19 package for this release.
The PG19 beta 4 Dockerfile builds the pinned `v0.26.1` source commit
`529c77e9dffe4bd86b6b7971c019bb26b8eadbfa` with its existing pgrx 0.19.3 and the
scoped `docker/patches/paradedb-0.26.1-pg19beta4.patch` compatibility patch. This is
an experimental compatibility build used by this project's integration and
performance tests, not an upstream ParadeDB binary.

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

### Native Arrays and Document Batches

The separate `scripts/benchmark_array_batch.py` harness compares the real
pre-optimization P1 commit `f1094be` and the current working Rust library on the
same 20,000 phrases. Both are unreleased 0.0.9 development snapshots, not two
published releases. Rebuild the P1 snapshot before installing the current library,
using the same Rust toolchain
and release profile for both. Run inside the test container so `pg_config` and
library paths refer to the server's filesystem:

```bash
export PG_CONFIG=/usr/lib/postgresql/19/bin/pg_config
PG_PINYIN_BUILD_RELEASE=1 bash test/prepare_upgrade.sh f1094beeb69c093085403f5b02d616a4de7adabe
cargo pgrx install --release --features pg19 --no-default-features --pg-config "$PG_CONFIG"
cp pg_pinyin--*--*.sql "$("$PG_CONFIG" --sharedir)/extension/"
python3 scripts/benchmark_array_batch.py --pgurl "$PGADMINURL" --rows 20000 --runs 3
```

Conversion and fresh heap/primary-key INSERT are separate measurements. Token
arrays/batches are precomputed outside timing; ICU tokenization is measured
separately. Cold means a new backend cache, not cold OS buffers. The harness
rejects debug baselines or mismatched current release artifacts and verifies
every output against the unchanged dictionary. It does not measure BM25 index
maintenance or claim index reduction.

The 2026-10-13 [report](benchmarks/p2_array_batch.md) and
[raw JSON](benchmarks/p2_array_batch.json) use PostgreSQL 19 beta 4 / pg_search
0.26.1, 20,000 phrases and three rotated repeats. Warm medians in milliseconds:

| Method | Conversion | Fresh INSERT |
| --- | ---: | ---: |
| P1 snapshot scalar | 300.489 | 321.993 |
| Current scalar | 258.099 | 296.844 |
| P1 snapshot token array | 623.978 | 649.027 |
| Native token array | 261.685 | 277.513 |
| Batch 128 documents | 25.482 | 41.774 |
| Batch 512 documents | 23.418 | 41.592 |

Native arrays observed 2.38x faster conversion and 2.34x faster INSERT than the
old array path. Batch 512 observed 12.8x faster conversion and 7.74x faster
INSERT than the old scalar path, with identical verified outputs. Batch 128
and 512 INSERT medians were close; larger batches were not uniformly faster.
These are shared-host observations, not universal speedup/optimal-size claims.
The host used `31946.69M` swap before timing. Input preparation/client transfer,
BM25 maintenance and production ingestion throughput are outside this result.

Local validation passed 88 Python tests, 875 pgTAP assertions and 16 Rust tests,
including genuine released 0.0.8-to-development-0.0.9 upgrades, live cache
mutation/rollback, TOAST and actual read-only parallel workers. Dictionary
regeneration was deterministic and `sql/data` remained unchanged.

### P1 Query Modes (pg_search 0.26.1)

The separate [P1 report](benchmarks/p1_query_modes.md) and
[raw JSON](benchmarks/p1_query_modes.json) record the 2026-10-13 PG19 beta 4
experimental build: 20,000 phrases, 2,000 queries per method, three rotated
repeats, top-k 10, and a fresh UUID database that is removed after the run.
Reproduce with psycopg 3 and an administrative DSN:

```bash
python3 scripts/benchmark_pinyin_query_modes.py --pgurl "$PGADMINURL" \
  --rows 20000 --query-limit 2000 --runs 3 --pg-search-version 0.26.1
```

Plain-field medians in milliseconds; constructor timing evaluates all 20,000
inputs, while full queries include construction, BM25, round trips and results:

| Mode | Rust warm constructor | SQL warm constructor | Rust full query batch | SQL full query batch |
| --- | ---: | ---: | ---: | ---: |
| `compat` | 12.152 | 1,878.998 | 1,378.673 | 1,576.698 |
| `full` | 13.902 | 5,729.506 | 400.313 | 1,167.757 |
| `initials` | 12.593 | 4,251.153 | 2,059.790 | 2,457.391 |
| `autocomplete` | 14.346 | 5,373.237 | 788.271 | 1,440.392 |

All pattern arrays and ranked IDs agree between backends within each mode and
storage format. Every autocomplete corpus output is also checked against source
syllable positions, independently of either backend. In three same-input,
same-label exact-boundary contrasts, compat has four visible labeled false
positives versus zero for full, with all seven positive labels retained. The
57 pattern cases and 30 adversarial retrieval cases are targeted contract checks,
not evidence of general retrieval accuracy or the absence of all false positives.

Full and compat have different matching semantics, so their timing difference
is not a parser algorithm speedup. Pipe-wrapped single-reading fields are much
slower here: Rust full/initials/autocomplete batches are 4,867.482 / 15,883.333 /
9,140.413 ms. These fields are not a realistic polyphonic performance corpus.
The same dual-field index is 3,514,368 bytes across all three fresh builds; query
modes do not reduce indexed content. The host had about 33.3 GB swap in use and
other workloads, so these timings do not establish stable performance gains.
The JSON retains raw repeats, IDs, plans and source hashes. Historical results
below use older pg_search versions and different protocols, and are not relabeled
or directly compared with this run.

### P0 Dictionary Coverage

P0 dictionary correction comparison (20,000 stratified phrases, isolated databases,
old committed CSVs versus candidate CSVs under the same runtime):

```bash
python3 scripts/benchmark_dictionary_p0.py --pgurl "$PGADMINURL" --baseline-ref v0.0.8 --rows 20000 --runs 3
```

The dedicated [P0 report](benchmarks/p0_dictionary.md) separates targeted exact
accuracy, warm latency, generated bytes and actual BM25/GIN sizes. Synthetic
queries are not real user traffic. The general benchmark below remains a
different workload and must not be compared directly with P0 timings.

The coverage-preserving run includes retained, restored and new word pools in
the same input corpus for both variants. Curated accuracy is separate from
synthetic retrieval consistency, and fixture IDs no longer always win score ties.
See the report for current measurements; the previous common-only run did not
validate the 64,091 missing words and is superseded. The standalone script requires psycopg 3
(included in the test image) and an administrative DSN able to create databases.

Coverage-preserving run on PG19 beta4 / pg_search 0.25.1, 2026-10-13
(20,000 phrases, 2,000 queries, three rotated repeats):

| Metric | v0.0.8 data | Corrected data |
| --- | ---: | ---: |
| Phrase dictionary keys | 157,702 | 196,188; no old keys missing |
| Curated exact readings | 33/55 | 55/55 |
| Curated Rust Recall@10 / MRR | 0.6000 / 0.4606 | 1.0000 / 0.8273 |
| Rust text romanization, warm median | 333.270 ms | 292.435 ms (-12.25%) |
| Rust full-query batch, median | 1,841.729 ms | 1,867.646 ms (+1.41%) |
| BM25 index bytes | 3,309,568 | 3,309,568 |

These are targeted regressions, not general pronunciation accuracy. Full-query
latency is effectively unchanged and no index reduction is established; generated
word-field bytes increase by 0.19%. SQL exact-array warm conversion is 22.97%
slower. The legacy SQL phrase-or-character path is much faster on this corpus,
consistent with avoiding expensive character fallback for newly covered phrases,
not a parser algorithm change. Host swapping and large run-to-run variation mean
the three-repeat timings do not establish a stable speedup.

Tokenization, query-builder, and full-search benchmark scripts:

- `scripts/benchmark_pg18.sh`
- `scripts/benchmark_pg19beta4.sh` (PG19 beta 4 report-name wrapper)

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

The general shell benchmark uses `EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)`.
Dedicated P0/P1 scripts record client timings and untimed execution-plan probes
separately; their absolute times are not directly comparable with this protocol.

By default, `scripts/benchmark_pg18.sh` drops and recreates a dedicated benchmark database (`BENCHMARK_DATABASE=pg_pinyin_benchmark`) before each run, then refreshes runtime state before each benchmark task. This avoids collisions with previously loaded extensions, SQL helper definitions, BM25 indexes, bench tables, and Rust dictionary caches. Set `BENCHMARK_FRESH_DATABASE=0` only when you intentionally want to reuse the database named by `PGURL`.

Run:

```bash
ROWS=2000 REGEX_BENCH_ROWS=20000 USER_TABLE_SUFFIX=_bench PGURL=postgres://localhost/postgres ./scripts/benchmark_pg18.sh

# In the PostgreSQL 19 beta 4 + ParadeDB 0.25.1 container
ROWS=20000 REGEX_BENCH_ROWS=20000 USER_TABLE_SUFFIX=_bench PGURL=postgres://localhost/postgres ./scripts/benchmark_pg19beta4.sh
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

Latest run (PG18, `pg_search=0.24.0`, `pg_pinyin=0.0.4`, fresh benchmark database, `ROWS=2000`, `REGEX_BENCH_ROWS=20000`, 2026-06-08):

Character mode:

| Scenario                                                                                          |       Cold |       Warm | Speedup vs SQL (`cold` / `warm`) |
| ------------------------------------------------------------------------------------------------- | ---------: | ---------: | --------------------------------: |
| SQL baseline (`characters2romanize`)                                                              |  `8291.975` | `8072.434` |                       `1.0x / 1.0x` |
| Rust (`pinyin_char_romanize`)                                                                     |    `90.025` |   `32.087` |                      `92.1x / 251.6x` |
| Rust + suffix (`pinyin_char_romanize(name, '_bench')`)                                            |   `176.412` |   `33.863` |                      `47.0x / 238.4x` |

Word mode (`pg_search` tokenizer input):

| Scenario                                                                                          |       Cold |       Warm | Speedup vs SQL (`cold` / `warm`) |
| ------------------------------------------------------------------------------------------------- | ---------: | ---------: | --------------------------------: |
| SQL baseline (`icu_romanize(name::pdb.icu::text[])`)                                              |  `255.649` |  `249.832` |                       `1.0x / 1.0x` |
| Rust (`pinyin_word_romanize(name::pdb.icu::text[])`)                                              |  `332.474` |   `71.259` |                       `0.8x / 3.5x` |
| Rust + suffix (`pinyin_word_romanize(name::pdb.icu::text[], '_bench')`)                          |  `788.152` |   `69.230` |                       `0.3x / 3.6x` |
| Rust plain text (`pinyin_word_romanize(name)`)                                                    |  `334.510` |   `36.570` |                       `0.8x / 6.8x` |

Query-token builder (`pinyin_regex_phrase_patterns`, 20,000 rows):

| Scenario                                                                                          | Cold-ish / Best | Warm / Best | Notes |
| ------------------------------------------------------------------------------------------------- | --------------: | ----------: | ----- |
| SQL backend tokens (`sql_pinyin_regex_phrase_patterns`)                                           |    `1555.603` ms | `1572.450` ms | PostgreSQL SQL helper from `sql/word.sql` |
| Rust backend tokens (`pinyin_regex_phrase_patterns`)                                              |      `41.784` ms |   `43.334` ms | PostgreSQL UDF path, includes `text[]` return overhead |
| Rust backend generated-pinyin tokens (`pinyin_regex_phrase_patterns(query, true)`)                |              - |   `44.439` ms | PostgreSQL UDF path |
| Rust backend `pg_search` query (`pinyin_regex_phrase`)                                            |              - |   `60.588` ms | Public helper exported by `CREATE EXTENSION pg_pinyin` |
| Rust backend slope/max (`pinyin_regex_phrase(query, 2, 4096)`)                                    |              - |   `57.838` ms | Public helper exported by `CREATE EXTENSION pg_pinyin` |
| SQL backend + `pg_search` query (`sql_pinyin_regex_phrase`)                                       |              - | `1611.476` ms | Builds `pdb.query` |
| SQL backend + slope/max (`sql_pinyin_regex_phrase(query, 2, 4096)`)                               |              - | `1607.444` ms | Builds `pdb.query` |
| SQL backend generated-pinyin query (`sql_pinyin_regex_phrase(query, NULL, NULL, true)`)           |              - | `1568.413` ms | Builds `pdb.query` |

Standalone query-token builder (`tokens` mode, 20,000 rows, no PostgreSQL executor or SQL array overhead):

| Scenario | Best | Median | Best per row | Checksum |
| -------- | ---: | -----: | -----------: | -------: |
| Rust standalone | `3.538` ms | `3.581` ms | `0.177` us | `219996` |
| Python standalone | `33.353` ms | `33.631` ms | `1.668` us | `219996` |

Server-side full `pg_search` query benchmark (20,000 rows from `bench_pinyin_regex_queries`, no client round trip per query):

| Scenario | Execution Time | Notes |
| -------- | -------------: | ----- |
| Rust parser in PostgreSQL + `pg_search`, memoize enabled | `1027.056` ms | Query mix has 9 distinct queries; PostgreSQL memoizes repeated LATERAL results |
| Rust parser in PostgreSQL + `pg_search`, memoize disabled and JIT off | `5739.186` ms | Executes 20,000 BM25 lookups |

Full `pg_search` query benchmark (20,000 client queries against a 2,000-row BM25 table, same result checksum):

| Scenario | Best | Median | Best per query | Checksum |
| -------- | ---: | -----: | -------------: | -------: |
| Python client parse + `text[]` patterns + `pg_search` | `7439.585` ms | `7503.080` ms | `371.979` us | `1337644` |
| Rust in-Postgres parse + `pg_search` | `7529.758` ms | `7530.354` ms | `376.488` us | `1337644` |

The full client-query benchmark shows that once each query executes a real `pg_search` index lookup and a client/server round trip, parser cost is not the dominant factor. Keeping parsing in PostgreSQL is still useful when callers want a single parameterized SQL API, server-side batch queries, no client-side token dictionary, and no dynamic SQL construction.

Times above are `Execution Time` in milliseconds from `EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)`.
`cold` runs for Rust base paths force a dictionary version bump before execution to simulate first-use cache load.
Suffix dictionaries are cached on first use and reused across statements. If suffix tables are updated, clear cache with `public.pinyin_clear_suffix_cache('_suffix')` (or `public.pinyin_clear_suffix_cache()` for all).
The standalone Rust/Python query-token numbers intentionally exclude PostgreSQL executor, UDF, and SQL array materialization overhead; they compare only the tokenization and pattern construction path.

### Benchmark Session (PG19 Beta 4)

Run on 2026-09-28 with PostgreSQL 19 beta 4, `pg_search=0.25.1`, the published `pg_pinyin=0.0.7` arm64 DEB (pgrx `0.19.3`), a fresh benchmark database, `ROWS=20000`, and `REGEX_BENCH_ROWS=20000`. Times are in milliseconds; lower is better.

PostgreSQL functions (20,000 rows):

| Scenario | Cold / Cold-ish | Warm | Notes |
| --- | ---: | ---: | --- |
| SQL char tokenizer | `80726.375` | `2943.466` | Cold result is an outlier under host memory pressure |
| Rust char tokenizer | `346.601` | `296.710` | `9.9x` faster than SQL when warm |
| SQL word tokenizer (`pdb.icu` input) | `3373.882` | `3340.056` | Same tokenizer input as Rust below |
| Rust word tokenizer (`pdb.icu` input) | `773.751` | `635.176` | `5.3x` faster than SQL when warm |
| Rust word tokenizer (plain text) | `402.054` | `241.737` | Input mode without `pdb.icu` conversion |
| SQL regex token UDF | `1896.137` | `1815.796` | 20,000 queries |
| Rust regex token UDF | `6.552` | `6.323` | `287x` faster than SQL when warm |
| SQL `pdb.query` builder | - | `1929.006` | `sql_pinyin_regex_phrase(query)` |
| Rust `pdb.query` builder | - | `22.151` | `pinyin_regex_phrase(query)` |

Standalone token construction (20,000 queries, five timed runs; excludes PostgreSQL executor and SQL array overhead):

| Scenario | Best | Median | Checksum |
| --- | ---: | ---: | ---: |
| Rust | `3.489` | `3.533` | `219996` |
| Python | `34.677` | `35.724` | `219996` |

Full `pg_search` client queries against a 20,000-row BM25 table (20,000 queries, three timed runs per path):

| Scenario | Best | Median | Best per query | Result checksum |
| --- | ---: | ---: | ---: | ---: |
| Python client parse + `text[]` patterns | `11420.440` | `12274.454` | `571.022` us | `13336444` |
| Rust in-Postgres parse | `11549.828` | `12339.904` | `577.491` us | `13336444` |

The full-query medians differ by only `0.5%`; this run does not establish an end-to-end advantage for either parser. The three Python runs were `11420.440`, `12650.059`, and `12274.454` ms; the Rust runs were `11549.828`, `12339.904`, and `13478.456` ms. The host was using about `27.8 GB` of swap, so the cold SQL char result should not be treated as representative. The complete raw report is generated as `benchmark_pg19beta4_report.txt` and remains untracked.

## Roadmap

1. P0 reproducible, syllable-preserving data pipeline implemented; follow [the retrieval/data spec](spec/pinyin-data-and-retrieval.md) for proposed P1/P2 work.
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

- Character readings: [mozillazg/pinyin-data](https://github.com/mozillazg/pinyin-data).
- Boundary-preserving phrases: [mozillazg/phrase-pinyin-data](https://github.com/mozillazg/phrase-pinyin-data) and [CC-CEDICT contributors / MDBG](https://www.mdbg.net/chinese/dictionary?page=cc-cedict). See [data notices](sql/data/NOTICE.md).
- Historical pre-P0 joined word data came from [tsroten/dragonmapper](https://github.com/tsroten/dragonmapper/blob/main/src/dragonmapper/data/hanzi_pinyin_words.tsv); it is only a coverage baseline. Restoration uses pinned raw CC-CEDICT syllables, not guessed segmentation of this joined data.
