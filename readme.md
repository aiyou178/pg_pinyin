# pg_pinyin

[中文说明](README.zh-CN.md)

`pg_pinyin` includes:

1. SQL baseline (`sql/pinyin.sql`)
2. Rust extension (`src/lib.rs`)

## Extension API

Romanization is split into three families:

- `pinyin_char_romanize(text)`
- `pinyin_char_romanize(text, suffix text)`
- `pinyin_word_romanize(text)`
- `pinyin_word_romanize(text, suffix text)`
- `pinyin_word_romanize(tokenizer_input anyelement)` (overload; use `pdb` tokenizer input such as `name::pdb.icu::text[]`)
- `pinyin_word_romanize(tokenizer_input anyelement, suffix text)` (overload with user-table suffix)
- `pinyin_word_romanize(text, model pinyin.model_identifier)` (call with `model => 'g2pw'`)
- `pinyin_word_romanize(text, suffix text, model pinyin.model_identifier)`
- `pinyin_word_romanize(tokenizer_input anyelement, model pinyin.model_identifier)`
- `pinyin_word_romanize(tokenizer_input anyelement, suffix text, model pinyin.model_identifier)`
- `pinyin_model_romanize(text, model text)`
- `pinyin_model_romanize(tokenizer_input anyelement, model text)`
- `pinyin_word_romanize_debug(text, suffix text default '', model text default '')` returns `jsonb`
- `pinyin_model_romanize_debug(text, model text)` returns `jsonb`

Recommended usage:

1. char romanization + `pg_trgm`
2. word romanization + `pg_search`
3. `pinyin_word_romanize(..., model => 'g2pm')` when you want bundled model help without external ONNX Runtime
4. `pinyin_word_romanize(..., model => 'g2pw')` when you want dictionary word hits first and model fallback only for unresolved polyphones
5. `pinyin_model_romanize(..., 'g2pw')` when you want model-driven polyphone selection without word-dictionary shortcuts

Volatility:

- `pinyin_char_romanize*` and `pinyin_word_romanize*` stay `IMMUTABLE`
- `pinyin_word_romanize(..., model => ...)`, `pinyin_model_romanize*`, and the debug APIs are `STABLE` and `PARALLEL UNSAFE`

## Release Tracks

`pg_pinyin` now has two package/release tracks:

- `v0.1.0` style tags publish `postgresql-<pg>-pg-pinyin`
  - ships char/word APIs and bundled compact `g2pm`
  - does not require ONNX Runtime
- `model-v0.1.0` style tags publish `postgresql-<pg>-pg-pinyin-model`
  - ships bundled compact `g2pm`
  - enables `g2pw_onnx` support via `hybrid_onnx`
  - does not bundle ONNX Runtime or `g2pW` assets

Both tracks still install SQL extension name `pg_pinyin`.

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

Packaged releases seed one enabled bundled row automatically:

- `model_name = 'bundled_g2pm'`
- `kind = 'g2pm_numpy'`
- `model_path = /usr/share/postgresql/<major>/extension/pg_pinyin/g2pm/manifest.json`

So after installing either package line, `model => 'g2pm'` should work without manual registry inserts.

Example for manually registering `g2pW`:

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

## Polyphone Model Registry

Model-assisted APIs keep dictionary behavior unchanged unless you pass `model => ...`.

Schema objects:

- `pinyin.pinyin_model_registry`
- `pinyin.pinyin_model_meta`

Example:

```sql
INSERT INTO pinyin.pinyin_model_registry (
  model_name,
  kind,
  model_path,
  tokenizer_path,
  labels_path,
  config
) VALUES (
  'g2pw_v1',
  'g2pw_onnx',
  '/absolute/path/to/G2PWModel-v2-onnx/g2pW.onnx',
  '/absolute/path/to/tokenizer-assets',
  '/absolute/path/to/labels.txt',
  '{"min_confidence":0.80,"min_margin":0.05,"disable_on_error":true}'::jsonb
);

SELECT public.pinyin_word_romanize('重启', model => 'g2pw');
SELECT public.pinyin_word_romanize('银行行长', model => 'g2pw');
SELECT public.pinyin_model_romanize('银行行长', 'g2pw');
SELECT public.pinyin_word_romanize_debug('银行行长', model => 'g2pw');
SELECT public.pinyin_model_romanize_debug('银行行长', 'g2pw');
```

To prepare custom `g2pM` assets for the Rust backend:

```bash
./scripts/export_g2pm_assets.sh --output-dir /absolute/path/to/g2pm-export
```

Then register them like this:

```sql
INSERT INTO pinyin.pinyin_model_registry (
  model_name,
  kind,
  model_path,
  tokenizer_path,
  labels_path,
  config
) VALUES (
  'g2pm_v1',
  'g2pm_numpy',
  '/absolute/path/to/g2pm-export/manifest.json',
  NULL,
  NULL,
  '{"min_confidence":0.80,"min_margin":0.05,"disable_on_error":true}'::jsonb
);

SELECT public.pinyin_word_romanize('重门', model => 'g2pm');
SELECT public.pinyin_model_romanize('银行行长', 'g2pm');
```

`model => 'g2pw'` is resolved as a kind alias for `g2pw_onnx`, not as `model_name`.
`model => 'g2pm'` is resolved as a kind alias for `g2pm_numpy`.
The public APIs require exactly one enabled registry row for that kind.

Fallback precedence for `pinyin_word_romanize(..., model => 'g2pw')`:

1. suffix word
2. base word
3. suffix char only when it resolves to exactly one candidate
4. model choice from base char candidates
5. base char first candidate
6. original token

`pinyin_model_romanize(..., 'g2pw')` bypasses word-dictionary matching entirely and only uses candidate parsing + model selection + fallback to first candidate.

If the requested model cannot be loaded, or a decision is below threshold, model-assisted paths gracefully fall back to the first base candidate by default.

## Model Cache Semantics

Loaded models are cached as backend-local singletons:

- one cached runtime per PostgreSQL backend process
- one cache entry per normalized model kind
- reload on `pinyin_model_meta.version` change
- reload on relevant model GUC signature change

The cache is intentionally not shared across PostgreSQL backend processes.
Under concurrent SQL load, different PostgreSQL backends can still load their own model runtime.

### Deferred Server-Wide Singleton

`v0.1.0` intentionally stops at backend-local caching.
We are not trying to provide a true PostgreSQL-server singleton model host in this release because that would likely require a background worker plus shared-memory IPC and a `shared_preload_libraries` dependency.

That server-wide design remains a roadmap item rather than something the current package promises.

## Model GUCs

Model-assisted APIs can also be tuned at PostgreSQL level through GUCs:

- `pg_pinyin.g2pw_window_size`
- `pg_pinyin.g2pw_intra_op_num_threads`
- `pg_pinyin.model_min_confidence`
- `pg_pinyin.model_min_margin`

`g2pw_window_size` and `g2pw_intra_op_num_threads` only affect the `g2pw_onnx` backend.
`g2pm_numpy` uses the shared threshold GUCs but has no extra backend-specific GUCs yet.

Recommended CPU defaults for the current Docker benchmark image:

```sql
ALTER SYSTEM SET pg_pinyin.g2pw_window_size = '32';
ALTER SYSTEM SET pg_pinyin.g2pw_intra_op_num_threads = '2';
ALTER SYSTEM SET pg_pinyin.model_min_confidence = '0';
ALTER SYSTEM SET pg_pinyin.model_min_margin = '0';
SELECT pg_reload_conf();
```

Notes:

- `window_size=32` matches the official g2pW CPP config.
- `intra_op_num_threads=2` matches the official converter and is currently the best speed / simplicity tradeoff we found on CPU.
- `model_min_confidence=0` and `model_min_margin=0` disable extra acceptance thresholds during benchmarking so model rows measure actual model behavior instead of conservative fallback.
- These settings improve the current CPU path, and the extension now batches unresolved polyphone decisions per sentence before calling ORT.
- PostgreSQL in-process inference is still slower than the standalone ORT driver because the extension currently batches within one SQL row / sentence, while the standalone benchmark can drive much larger multi-sentence batches.

## `hybrid_onnx` Build Notes

Base PostgreSQL builds now include `g2pm_numpy`.
The Cargo feature `hybrid_onnx` is only needed for `g2pw_onnx`.

```bash
cargo build --features "pg18"
cargo build --features "pg18 hybrid_onnx"
```

Notes:

- `pg18` alone gives you the normal package behavior with bundled `g2pm`.
- `hybrid_onnx` adds `g2pw_onnx` and requires ONNX Runtime to be installed separately on the target system.
- Model assets are not bundled into this repository.
- PostgreSQL databases using this extension should use UTF-8 encoding.

## `g2pW` Setup For `model-v*` Releases

The `_model` package leaves `g2pW` optional. To enable it:

1. Install the `model-v*` package line for your PostgreSQL major version.
2. Install ONNX Runtime so `libonnxruntime.so` is discoverable by PostgreSQL.
3. Put the official `g2pW` files under:
   `/usr/share/postgresql/<major>/extension/pg_pinyin/g2pw/`
4. Register or update a `g2pw_onnx` row in `pinyin.pinyin_model_registry`.
5. Smoke test with `SELECT public.pinyin_word_romanize('银行行长', model => 'g2pw');`

Expected files in that directory:

- `g2pw.onnx`
- `POLYPHONIC_CHARS.txt`
- `vocab.txt` (or a directory containing it)

Troubleshooting:

- If PostgreSQL reports missing `libonnxruntime.so`, install ONNX Runtime or expose it through the system loader path.
- If the model row fails to load, check `model_path`, `tokenizer_path`, and `labels_path`.
- If the extension was built without `hybrid_onnx`, `g2pw` calls will error while `g2pm` remains available.

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

- base image: `postgres:18.4-trixie`
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
- `scripts/docker_pg18_test_and_bench.sh` (builds `docker/Dockerfile.test-trixie`, runs pgTAP, and then runs the benchmark inside Docker)
- `scripts/benchmark_g2pw_cpp.sh` (downloads the official public `g2pW` ONNX model on first run and measures standalone ORT inference on CPP)

It measures:

- SQL char tokenizer: `characters2romanize(name)` (`cold` + `warm`)
- Rust char tokenizer: `pinyin_char_romanize(name)` (`cold` + `warm`)
- Rust char tokenizer (user suffix overlay): `pinyin_char_romanize(name, '_<suffix>')` (`cold` + `warm`)
- SQL word tokenizer: `icu_romanize(name::pdb.icu::text[])` (`cold` + `warm`, if `pg_search` exists)
- Rust word tokenizer with tokenizer input: `pinyin_word_romanize(name::pdb.icu::text[])` (`cold` + `warm`)
- Rust word tokenizer with suffix overlay: `pinyin_word_romanize(name::pdb.icu::text[], '_<suffix>')` (`cold` + `warm`)
- Rust word tokenizer with plain text input: `pinyin_word_romanize(name)` (`cold` + `warm`)
- Rust word tokenizer with model fallback: `pinyin_word_romanize(name, model => 'g2pw')` (`cold` + `warm`)
- Rust word tokenizer with model fallback: `pinyin_word_romanize(name, model => 'g2pm')` (`cold` + `warm`)
- Rust model-only romanization: `pinyin_model_romanize(name, 'g2pw')` (`cold` + `warm`)
- Rust model-only romanization: `pinyin_model_romanize(name, 'g2pm')` (`cold` + `warm`)
- Rust tokenizer-input word romanization with model fallback: `pinyin_word_romanize(name::pdb.icu::text[], model => 'g2pw')` (`cold` + `warm`)
- Rust tokenizer-input word romanization with model fallback: `pinyin_word_romanize(name::pdb.icu::text[], model => 'g2pm')` (`cold` + `warm`)
- Internal `g2pw` batch helper: `pinyin__benchmark_model_target_batch_length(payload, 'g2pw')` (`cold` + `warm`, CPP only)

All benchmark queries use `EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)`.
`scripts/benchmark_pg18.sh` now also emits metadata headers so each report records its execution context, architecture, row count, model/GUC settings, and batching scope.

Run:

```bash
ROWS=2000 USER_TABLE_SUFFIX=_bench PGURL=postgres://localhost/postgres ./scripts/benchmark_pg18.sh
```

Run the same flow in Docker:

```bash
RUN_RUST_TESTS=0 RUN_BENCHMARK=1 BENCH_DATASET=synthetic ROWS=2000 ./scripts/docker_pg18_test_and_bench.sh
```

Benchmark dataset options:

- `BENCH_DATASET=synthetic`
- `BENCH_DATASET=cpp` using the vendored CPP split under `benchmark/testdata/cpp/test.sent` and `benchmark/testdata/cpp/test.lb`, sourced from the public data linked by the Interspeech 2023 paper: [zhang23h_interspeech.pdf](https://www.isca-archive.org/interspeech_2023/zhang23h_interspeech.pdf)

Run the standalone official g2pW ONNX benchmark:

```bash
./scripts/benchmark_g2pw_cpp.sh --report ./benchmark_g2pw_cpp_report.txt
```

### Benchmark Session (PG18)

Latest Docker-backed runs (PG18, refreshed on 2026-04-12 for base + bundled `g2pm`; the `g2pw` in-database rows below are retained from the last full Docker model run on 2026-04-11 because this release split did not change the `g2pw` execution path, model GUCs `window_size=32`, `intra_op_num_threads=2`, `min_confidence=0`, `min_margin=0`):

Synthetic dataset (`ROWS=2000`):

Character mode

| Scenario                                                                                          |        Cold |        Warm | Speedup vs SQL (`cold` / `warm`) |
| ------------------------------------------------------------------------------------------------- | ----------: | ----------: | --------------------------------: |
| SQL baseline (`characters2romanize`)                                                              |  `9937.718` |  `9859.117` |                       `1.0x / 1.0x` |
| Rust (`pinyin_char_romanize`)                                                                     |   `109.390` |    `31.505` |                      `90.8x / 312.9x` |
| Rust + suffix (`pinyin_char_romanize(name, '_bench')`)                                            |   `272.373` |    `33.326` |                      `36.5x / 295.8x` |

Word mode (`pg_search` tokenizer input when available)

| Scenario                                                                                          |        Cold |        Warm | Speedup vs SQL (`cold` / `warm`) |
| ------------------------------------------------------------------------------------------------- | ----------: | ----------: | --------------------------------: |
| SQL baseline (`icu_romanize(name::pdb.icu::text[])`)                                              |   `281.951` |   `250.884` |                         `1.0x / 1.0x` |
| Rust tokenizer (`pinyin_word_romanize(name::pdb.icu::text[])`)                                    |    `77.337` |    `77.464` |                         `3.6x / 3.2x` |
| Rust tokenizer + suffix (`pinyin_word_romanize(name::pdb.icu::text[], '_bench')`)                |    `78.970` |    `77.460` |                         `3.6x / 3.2x` |
| Rust plain text (`pinyin_word_romanize(name)`)                                                    |   `418.301` |    `34.014` |                         `0.7x / 7.4x` |
| Rust plain text + suffix (`pinyin_word_romanize(name, '_bench')`)                                 |  `1511.666` |    `41.509` |                         `0.2x / 6.0x` |
| Rust plain text + model (`pinyin_word_romanize(name, model => 'g2pw')`)                           | `10918.896` |  `3840.944` |                         `0.0x / 0.1x` |
| Rust model-only (`pinyin_model_romanize(name, 'g2pw')`)                                           | `14567.764` |  `7969.875` |                         `0.0x / 0.0x` |
| Rust tokenizer + model (`pinyin_word_romanize(name::pdb.icu::text[], model => 'g2pw')`)          |  `3462.680` |  `3373.820` |                         `0.1x / 0.1x` |
| Rust plain text + model (`pinyin_word_romanize(name, model => 'g2pm')`)                           |  `1869.567` |  `1862.360` |                         `0.2x / 0.1x` |
| Rust model-only (`pinyin_model_romanize(name, 'g2pm')`)                                           |  `2734.517` |  `2658.187` |                         `0.1x / 0.1x` |
| Rust tokenizer + model (`pinyin_word_romanize(name::pdb.icu::text[], model => 'g2pm')`)          |  `1789.931` |  `1811.543` |                         `0.2x / 0.1x` |

CPP timing dataset (`benchmark/testdata/cpp/test.sent`, first `ROWS=500` sentences after stripping sentencepiece markers):

Character mode

| Scenario                                                                                          |        Cold |        Warm | Speedup vs SQL (`cold` / `warm`) |
| ------------------------------------------------------------------------------------------------- | ----------: | ----------: | --------------------------------: |
| SQL baseline (`characters2romanize`)                                                              |  `1256.717` |  `1229.955` |                         `1.0x / 1.0x` |
| Rust (`pinyin_char_romanize`)                                                                     |    `96.955` |    `31.466` |                        `13.0x / 39.1x` |
| Rust + suffix (`pinyin_char_romanize(name, '_bench')`)                                            |   `258.189` |    `33.340` |                         `4.9x / 36.9x` |

Word mode (`pg_search` tokenizer input when available)

| Scenario                                                                                          |        Cold |        Warm | Speedup vs SQL (`cold` / `warm`) |
| ------------------------------------------------------------------------------------------------- | ----------: | ----------: | --------------------------------: |
| SQL baseline (`icu_romanize(name::pdb.icu::text[])`)                                              |   `113.244` |    `81.492` |                         `1.0x / 1.0x` |
| Rust tokenizer (`pinyin_word_romanize(name::pdb.icu::text[])`)                                    |    `50.533` |    `49.279` |                         `2.2x / 1.7x` |
| Rust tokenizer + suffix (`pinyin_word_romanize(name::pdb.icu::text[], '_bench')`)                |    `51.875` |    `51.279` |                         `2.2x / 1.6x` |
| Rust plain text (`pinyin_word_romanize(name)`)                                                    |   `391.888` |    `62.857` |                         `0.3x / 1.3x` |
| Rust plain text + suffix (`pinyin_word_romanize(name, '_bench')`)                                 |  `1456.842` |    `66.582` |                         `0.1x / 1.2x` |
| Rust plain text + model (`pinyin_word_romanize(name, model => 'g2pw')`)                           | `12865.518` | `11889.673` |                         `0.0x / 0.0x` |
| Rust model-only (`pinyin_model_romanize(name, 'g2pw')`)                                           |  `6104.084` |  `4917.590` |                         `0.0x / 0.0x` |
| Rust tokenizer + model (`pinyin_word_romanize(name::pdb.icu::text[], model => 'g2pw')`)          | `11426.707` | `11455.037` |                         `0.0x / 0.0x` |
| Rust plain text + model (`pinyin_word_romanize(name, model => 'g2pm')`)                           |  `5730.846` |  `5511.569` |                         `0.0x / 0.0x` |
| Rust model-only (`pinyin_model_romanize(name, 'g2pm')`)                                           |  `6843.033` |  `6679.726` |                         `0.0x / 0.0x` |
| Rust tokenizer + model (`pinyin_word_romanize(name::pdb.icu::text[], model => 'g2pm')`)          |  `5398.908` |  `5242.447` |                         `0.0x / 0.0x` |

CPP target accuracy (`benchmark/testdata/cpp/test.sent` + `test.lb`, full `CPP_ACCURACY_ROWS=8935`, toneless):

| Method | Toneless accuracy |
| ------ | ----------------: |
| Char dictionary (`pinyin__char_target_debug`) | `88.4051%` |
| Word dictionary (`pinyin__word_target_debug`) | `92.4566%` |
| Word + model (`pinyin__word_target_debug(..., 'g2pw')`) | `93.4303%` |
| Model-only (`pinyin__model_target_debug(..., 'g2pw')`) | `91.4941%` |
| Word + model (`pinyin__word_target_debug(..., 'g2pm')`) | `97.2132%` |
| Model-only (`pinyin__model_target_debug(..., 'g2pm')`) | `98.1309%` |

Times above are `Execution Time` in milliseconds from `EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)`.
`cold` runs for Rust base paths force a dictionary version bump before execution to simulate first-use cache load.
Suffix dictionaries are cached on first use and reused across statements. If suffix tables are updated, clear cache with `public.pinyin_clear_suffix_cache('_suffix')` (or `public.pinyin_clear_suffix_cache()` for all).
For the current CPP notes, timing uses `ROWS=500` while accuracy uses the full vendored split with `CPP_ACCURACY_ROWS=8935`.
The helper `public.pinyin__polyphone_romanize(name, query_pos)` remains useful as a narrower extension-side ORT benchmark, but the tables above now focus on the public API families.

The new internal helper `public.pinyin__benchmark_model_target_batch_length(...)` is benchmark-only and deliberately undocumented for normal SQL use.
It strips most row-by-row SQL call overhead by resolving many target characters inside one SQL call, while still using the same backend-local model runtime and per-sentence model requests as the extension itself.

### Runtime Gap Investigation

Latest aarch64 Docker investigation on 2026-04-12, with the same benchmark GUCs (`window_size=32`, `intra_op_num_threads=2`, `min_confidence=0`, `min_margin=0`):

- standalone raw `g2pw` on a 10-row CPP slice completed with `372.473 ms` warm prediction time, about `37.247 ms/row`
- the in-database public API path `pinyin_word_romanize(name, model => 'g2pw')` did not finish a 10-row CPP slice within 90 seconds
- the internal helper `pinyin__benchmark_model_target_batch_length(..., 'g2pw')`, which removes most row-by-row SQL call overhead, still did not finish a 500-row CPP slice within 8 minutes on the same environment

This points to a problem deeper than just SQL wrapper overhead.
On this Docker/aarch64 CPU path, the gap to standalone `g2pw` appears to be dominated by the extension-side model/runtime execution itself, not only by public SQL entrypoint dispatch.
The raw generated reports for this investigation are not tracked; this section keeps the retained summary.

### Real g2pW ONNX Benchmark

Latest standalone run (2026-04-12) using the official public `G2PWModel-v2-onnx.zip`, vendored `benchmark/testdata/cpp/test.sent` + `test.lb`, full `8935` rows, and `intra_op_threads=2` to match the official converter configuration:

| Metric | Value |
| ------ | ----: |
| Rows | `8935` |
| Target coverage | `onnx=7654, mono=1224, char_default=57, unknown=0` |
| Tone accuracy, all rows | `85.7079%` |
| Toneless accuracy, all rows | `92.5797%` |
| Tone accuracy, ONNX rows only | `86.5691%` |
| Toneless accuracy, ONNX rows only | `92.5398%` |
| Model+tokenizer load | `7692.993 ms` |
| Feature preparation | `1629.357 ms` |
| Predict cold | `256054.396 ms` |
| Predict warm | `252729.680 ms` |
| First-run total | `265376.747 ms` |
| Warm predict per row | `28.285359 ms` |

This standalone benchmark uses the public inference model and its official support assets outside PostgreSQL. Keep it separate from the in-database ORT numbers above: the extension-side path now has its own benchmark and accuracy line, while this standalone run remains useful as a reference for raw model behavior outside PostgreSQL.

### Why This Is Not `99.08%`

The g2pW paper reports `99.08%` on CPP, but there are two important differences between that headline number and the runs above:

- The paper explicitly says the released package ships "model weights trained from the MPB dataset", while the README/PyPI page shows CPP benchmarking via separate `saved_models/CPP_...` checkpoints rather than the public `G2PWModel-v2-onnx.zip`. This strongly suggests the downloadable public ONNX model is not the exact paper checkpoint used for the `99.08%` table. [Paper](https://www.isca-archive.org/interspeech_2022/chen22d_interspeech.pdf), [PyPI](https://pypi.org/project/g2pw/)
- The CVTE-Poly paper later points out that CPP has wrong labels and `1319` test sentences whose target character is not actually polyphonic, then defines a refined CPP test split with `8935` rows. That means "CPP accuracy" depends on whether you measure the original g2pM split (`10254` rows) or the refined split (`8935` rows). [CVTE-Poly](https://www.isca-archive.org/interspeech_2023/zhang23h_interspeech.pdf)

Our current reference measurements of the public ONNX release are:

- vendored refined CPP (`8935` rows): `92.5797%` toneless accuracy
- original g2pM/CPP test split (`10254` rows): `92.5005%` toneless accuracy

So the gap to `99.08%` is not a PostgreSQL-only artifact. A large part of it comes from comparing the public released inference model against a paper result that appears to use a different checkpoint and a different benchmark curation story.

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
