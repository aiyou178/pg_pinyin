#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PGURL="${PGURL:-postgres://localhost/postgres}"
PGADMINURL="${PGADMINURL:-$PGURL}"
ROWS="${ROWS:-2000}"
REGEX_BENCH_ROWS="${REGEX_BENCH_ROWS:-20000}"
OUT_FILE="${1:-$ROOT_DIR/benchmark_pg18_report.txt}"
MAPPING_FILE="${MAPPING_FILE:-$ROOT_DIR/sql/data/pinyin_mapping.csv}"
TOKEN_FILE="${TOKEN_FILE:-$ROOT_DIR/sql/data/pinyin_token.csv}"
WORDS_FILE="${WORDS_FILE:-$ROOT_DIR/sql/data/pinyin_words.csv}"
USER_TABLE_SUFFIX="${USER_TABLE_SUFFIX:-_bench}"
BENCHMARK_FRESH_DATABASE="${BENCHMARK_FRESH_DATABASE:-1}"
BENCHMARK_DATABASE="${BENCHMARK_DATABASE:-pg_pinyin_benchmark}"

if ! [[ "$ROWS" =~ ^[0-9]+$ ]]; then
  echo "ROWS must be an integer, got: $ROWS" >&2
  exit 2
fi
if ! [[ "$REGEX_BENCH_ROWS" =~ ^[0-9]+$ ]]; then
  echo "REGEX_BENCH_ROWS must be an integer, got: $REGEX_BENCH_ROWS" >&2
  exit 2
fi

USER_SUFFIX_NORMALIZED="${USER_TABLE_SUFFIX#_}"
if [[ -z "$USER_SUFFIX_NORMALIZED" ]]; then
  echo "USER_TABLE_SUFFIX cannot be empty" >&2
  exit 2
fi
if ! [[ "$USER_SUFFIX_NORMALIZED" =~ ^[A-Za-z0-9_]+$ ]]; then
  echo "USER_TABLE_SUFFIX must contain only [A-Za-z0-9_], got: $USER_TABLE_SUFFIX" >&2
  exit 2
fi
if ! [[ "$BENCHMARK_FRESH_DATABASE" =~ ^(0|1)$ ]]; then
  echo "BENCHMARK_FRESH_DATABASE must be 0 or 1, got: $BENCHMARK_FRESH_DATABASE" >&2
  exit 2
fi
if ! [[ "$BENCHMARK_DATABASE" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
  echo "BENCHMARK_DATABASE must be a SQL identifier, got: $BENCHMARK_DATABASE" >&2
  exit 2
fi

if ! command -v psql >/dev/null 2>&1; then
  echo "psql not found in PATH" >&2
  exit 2
fi

for file in "$MAPPING_FILE" "$TOKEN_FILE" "$WORDS_FILE"; do
  if [[ ! -f "$file" ]]; then
    echo "missing benchmark data file: $file" >&2
    echo "run ./scripts/generate_data.sh first" >&2
    exit 2
  fi
done

mkdir -p "$(dirname "$OUT_FILE")"

replace_pgurl_database() {
  local url="$1"
  local database="$2"
  local prefix="${url%/*}"
  local tail="${url##*/}"
  local query=""

  if [[ "$tail" == *\?* ]]; then
    query="?${tail#*\?}"
  fi

  printf '%s/%s%s' "$prefix" "$database" "$query"
}

if [[ "$BENCHMARK_FRESH_DATABASE" == "1" ]]; then
  echo "[benchmark] refreshing database: $BENCHMARK_DATABASE"
  psql "$PGADMINURL" -v ON_ERROR_STOP=1 \
    -c "DROP DATABASE IF EXISTS \"$BENCHMARK_DATABASE\" WITH (FORCE)" \
    -c "CREATE DATABASE \"$BENCHMARK_DATABASE\""
  PGURL="$(replace_pgurl_database "$PGADMINURL" "$BENCHMARK_DATABASE")"
fi

ROOT_SQL=${ROOT_DIR//\'/\'\'}
MAPPING_SQL=${MAPPING_FILE//\'/\'\'}
TOKEN_SQL=${TOKEN_FILE//\'/\'\'}
WORDS_SQL=${WORDS_FILE//\'/\'\'}
SUFFIX_SQL=${USER_SUFFIX_NORMALIZED//\'/\'\'}
TMP_SQL="$(mktemp)"
trap 'rm -f "$TMP_SQL"' EXIT

cat > "$TMP_SQL" <<SQL
\\set ON_ERROR_STOP on
\\timing on

\\echo '[setup] ensuring extensions and SQL baseline are loaded'
CREATE EXTENSION IF NOT EXISTS pg_trgm;
\\set has_pg_search 0
SELECT CASE
  WHEN EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pg_search')
    AND position('pg_search' in current_setting('shared_preload_libraries', true)) > 0 THEN 1
  ELSE 0
END AS has_pg_search
\\gset

\\if :has_pg_search
  CREATE EXTENSION IF NOT EXISTS pg_search;
\\endif

CREATE EXTENSION IF NOT EXISTS pg_pinyin;
\\i '$ROOT_SQL/sql/pinyin.sql'

\\echo '[setup] loading mapping/token dictionaries'
TRUNCATE TABLE pinyin.pinyin_mapping;
TRUNCATE TABLE pinyin.pinyin_token;
TRUNCATE TABLE pinyin.pinyin_words;
\\copy pinyin.pinyin_mapping (character, pinyin) FROM '$MAPPING_SQL' WITH (FORMAT csv, HEADER false)
\\copy pinyin.pinyin_token (character, category) FROM '$TOKEN_SQL' WITH (FORMAT csv, HEADER false)
\\copy pinyin.pinyin_words (word, pinyin) FROM '$WORDS_SQL' WITH (FORMAT csv, HEADER false)
INSERT INTO pinyin.pinyin_mapping (character, pinyin)
VALUES (' ', ' ')
ON CONFLICT (character) DO NOTHING;

CREATE OR REPLACE FUNCTION public.bench_pinyin_refresh_runtime_state()
RETURNS void
LANGUAGE plpgsql
AS \$\$
BEGIN
  UPDATE pinyin.pinyin_dictionary_meta
  SET version = version + 1
  WHERE singleton;
  PERFORM public.pinyin_clear_suffix_cache();
END;
\$\$;

\\echo '[setup] preparing suffix user dictionary tables (same size as base)'
SELECT format('pinyin_mapping_%s', '$SUFFIX_SQL') AS mapping_table_name,
       format('pinyin_words_%s', '$SUFFIX_SQL') AS words_table_name
\\gset

SELECT format(
  'CREATE TABLE IF NOT EXISTS pinyin.%I (character text PRIMARY KEY, pinyin text NOT NULL)',
  :'mapping_table_name'
) AS ddl
\\gexec

SELECT format(
  'CREATE TABLE IF NOT EXISTS pinyin.%I (word text PRIMARY KEY, pinyin text NOT NULL)',
  :'words_table_name'
) AS ddl
\\gexec

SELECT format('TRUNCATE TABLE pinyin.%I', :'mapping_table_name') AS ddl
\\gexec
SELECT format('TRUNCATE TABLE pinyin.%I', :'words_table_name') AS ddl
\\gexec

SELECT format(
  'INSERT INTO pinyin.%I (character, pinyin) SELECT character, pinyin FROM pinyin.pinyin_mapping',
  :'mapping_table_name'
) AS ddl
\\gexec

SELECT format(
  'INSERT INTO pinyin.%I (word, pinyin) SELECT word, pinyin FROM pinyin.pinyin_words',
  :'words_table_name'
) AS ddl
\\gexec

\\echo '[setup] generating benchmark dataset'
DROP TABLE IF EXISTS public.bench_names;
CREATE TABLE public.bench_names (
  id bigint PRIMARY KEY,
  name text NOT NULL
);

WITH dict AS (
  SELECT
    ARRAY['张','王','李','赵','郑','重','楼','上','小','龙']::text[] AS surnames,
    ARRAY['爽','世昀','仕英','重阳','先生','小姐','小明','起飞','测试','编号']::text[] AS givens
)
INSERT INTO public.bench_names (id, name)
SELECT
  g,
  dict.surnames[1 + (g % array_length(dict.surnames, 1))]
  || dict.givens[1 + ((g / 3) % array_length(dict.givens, 1))]
  || CASE
       WHEN g % 5 = 0 THEN 'ABC'
       WHEN g % 7 = 0 THEN '123'
       ELSE ''
     END
FROM generate_series(1, $ROWS) AS g
CROSS JOIN dict;

ANALYZE public.bench_names;

DROP TABLE IF EXISTS public.bench_pinyin_regex_queries;
CREATE TABLE public.bench_pinyin_regex_queries (
  id bigint PRIMARY KEY,
  query text NOT NULL
);

WITH queries AS (
  SELECT ARRAY[
    'lun',
    'lunlun',
    'zhengshuang',
    'wangchongyang',
    'xian',
    'wchy',
    'zh sh',
    '   ',
    'abc'
  ]::text[] AS values
)
INSERT INTO public.bench_pinyin_regex_queries (id, query)
SELECT
  g,
  queries.values[1 + (g % array_length(queries.values, 1))]
FROM generate_series(1, $REGEX_BENCH_ROWS) AS g
CROSS JOIN queries;

ANALYZE public.bench_pinyin_regex_queries;

\\echo ''
\\echo '=== Full Tokenization Benchmark: Character Mode ==='
\\echo '[setup] refreshing runtime state for character benchmark'
SELECT public.bench_pinyin_refresh_runtime_state();
DISCARD PLANS;

\\echo '[benchmark][cold] SQL baseline: characters2romanize(name)'
EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
SELECT sum(length(public.characters2romanize(name)))
FROM public.bench_names;

\\echo '[benchmark][warm] SQL baseline: characters2romanize(name)'
EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
SELECT sum(length(public.characters2romanize(name)))
FROM public.bench_names;

\\echo '[benchmark][cold] Rust extension: pinyin_char_romanize(name)'
UPDATE pinyin.pinyin_mapping SET pinyin = pinyin WHERE character = ' ';
EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
SELECT sum(length(public.pinyin_char_romanize(name)))
FROM public.bench_names;

\\echo '[benchmark][warm] Rust extension: pinyin_char_romanize(name)'
EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
SELECT sum(length(public.pinyin_char_romanize(name)))
FROM public.bench_names;

\\echo '[benchmark][cold] Rust extension (suffix): pinyin_char_romanize(name, ''_$SUFFIX_SQL'')'
EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
SELECT sum(length(public.pinyin_char_romanize(name, '_$SUFFIX_SQL')))
FROM public.bench_names;

\\echo '[benchmark][warm] Rust extension (suffix): pinyin_char_romanize(name, ''_$SUFFIX_SQL'')'
EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
SELECT sum(length(public.pinyin_char_romanize(name, '_$SUFFIX_SQL')))
FROM public.bench_names;

\\echo ''
\\echo '=== Full Tokenization Benchmark: Word Mode ==='
\\echo '[setup] refreshing runtime state for word benchmark'
SELECT public.bench_pinyin_refresh_runtime_state();
DISCARD PLANS;

\\if :has_pg_search
  \\echo '[setup] pg_search available; loading SQL word tokenizer baseline (icu_romanize)'
  \\i '$ROOT_SQL/sql/word.sql'

  \\echo '[benchmark][cold] SQL baseline: icu_romanize(name::pdb.icu::text[])'
  EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
  SELECT sum(length(public.icu_romanize(name::pdb.icu::text[])))
  FROM public.bench_names;

  \\echo '[benchmark][warm] SQL baseline: icu_romanize(name::pdb.icu::text[])'
  EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
  SELECT sum(length(public.icu_romanize(name::pdb.icu::text[])))
  FROM public.bench_names;

  \\echo '[benchmark][cold] Rust extension: pinyin_word_romanize(name::pdb.icu::text[])'
  UPDATE pinyin.pinyin_mapping SET pinyin = pinyin WHERE character = ' ';
  EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
  SELECT sum(length(public.pinyin_word_romanize(name::pdb.icu::text[])))
  FROM public.bench_names;

  \\echo '[benchmark][warm] Rust extension: pinyin_word_romanize(name::pdb.icu::text[])'
  EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
  SELECT sum(length(public.pinyin_word_romanize(name::pdb.icu::text[])))
  FROM public.bench_names;

  \\echo '[benchmark][cold] Rust extension (suffix): pinyin_word_romanize(name::pdb.icu::text[], ''_$SUFFIX_SQL'')'
  EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
  SELECT sum(length(public.pinyin_word_romanize(name::pdb.icu::text[], '_$SUFFIX_SQL')))
  FROM public.bench_names;

  \\echo '[benchmark][warm] Rust extension (suffix): pinyin_word_romanize(name::pdb.icu::text[], ''_$SUFFIX_SQL'')'
  EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
  SELECT sum(length(public.pinyin_word_romanize(name::pdb.icu::text[], '_$SUFFIX_SQL')))
  FROM public.bench_names;

  \\echo '[benchmark][cold] Rust extension (plain text): pinyin_word_romanize(name)'
  UPDATE pinyin.pinyin_mapping SET pinyin = pinyin WHERE character = ' ';
  EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
  SELECT sum(length(public.pinyin_word_romanize(name)))
  FROM public.bench_names;

  \\echo '[benchmark][warm] Rust extension (plain text): pinyin_word_romanize(name)'
  EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
  SELECT sum(length(public.pinyin_word_romanize(name)))
  FROM public.bench_names;

  \\echo ''
  \\echo '=== Query Builder Benchmark: pinyin_regex_phrase ==='
  \\echo '[setup] refreshing runtime state for regex phrase benchmark'
  SELECT public.bench_pinyin_refresh_runtime_state();
  DISCARD PLANS;

  \\echo '[warmup] Rust backend token cache: pinyin_regex_phrase_patterns(query)'
  SELECT count(public.pinyin_regex_phrase_patterns(query))
  FROM public.bench_pinyin_regex_queries;

  \\echo '[benchmark][cold-ish] SQL backend tokens: sql_pinyin_regex_phrase_patterns(query)'
  EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
  SELECT sum(cardinality(public.sql_pinyin_regex_phrase_patterns(query)))
  FROM public.bench_pinyin_regex_queries;

  \\echo '[benchmark][warm] SQL backend tokens: sql_pinyin_regex_phrase_patterns(query)'
  EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
  SELECT sum(cardinality(public.sql_pinyin_regex_phrase_patterns(query)))
  FROM public.bench_pinyin_regex_queries;

  \\echo '[benchmark][cold-ish] Rust backend tokens: pinyin_regex_phrase_patterns(query)'
  EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
  SELECT sum(cardinality(public.pinyin_regex_phrase_patterns(query)))
  FROM public.bench_pinyin_regex_queries;

  \\echo '[benchmark][warm] Rust backend tokens: pinyin_regex_phrase_patterns(query)'
  EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
  SELECT sum(cardinality(public.pinyin_regex_phrase_patterns(query)))
  FROM public.bench_pinyin_regex_queries;

  \\echo '[benchmark][warm] Rust backend tokens, generated_pinyin: pinyin_regex_phrase_patterns(query, true)'
  EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
  SELECT sum(cardinality(public.pinyin_regex_phrase_patterns(query, true)))
  FROM public.bench_pinyin_regex_queries;

  \\echo '[benchmark][warm] Rust pg function: pinyin_regex_phrase(query)'
  EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
  SELECT count(public.pinyin_regex_phrase(query))
  FROM public.bench_pinyin_regex_queries;

  \\echo '[benchmark][warm] Rust pg function: pinyin_regex_phrase(query, slope => 2, max_expansions => 4096)'
  EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
  SELECT count(public.pinyin_regex_phrase(query, 2, 4096))
  FROM public.bench_pinyin_regex_queries;

  \\echo '[benchmark][warm] SQL backend + pg_search query: sql_pinyin_regex_phrase(query)'
  EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
  SELECT count(public.sql_pinyin_regex_phrase(query))
  FROM public.bench_pinyin_regex_queries;

  \\echo '[benchmark][warm] SQL pg function: sql_pinyin_regex_phrase(query, slope => 2, max_expansions => 4096)'
  EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
  SELECT count(public.sql_pinyin_regex_phrase(query, 2, 4096))
  FROM public.bench_pinyin_regex_queries;

  \\echo '[benchmark][warm] SQL pg function: sql_pinyin_regex_phrase(query, generated_pinyin => true)'
  EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
  SELECT count(public.sql_pinyin_regex_phrase(query, NULL, NULL, true))
  FROM public.bench_pinyin_regex_queries;

  \\echo ''
  \\echo '=== Full pg_search Benchmark Setup: pinyin_regex_phrase ==='
  \\echo '[setup] refreshing runtime state for full pg_search benchmark'
  SELECT public.bench_pinyin_refresh_runtime_state();
  DISCARD PLANS;

  DROP TABLE IF EXISTS public.bench_search_names;
  CREATE TABLE public.bench_search_names (
    id bigint PRIMARY KEY,
    name text NOT NULL,
    pinyin text NOT NULL
  );

  INSERT INTO public.bench_search_names (id, name, pinyin)
  SELECT id, name, public.pinyin_word_romanize(name)
  FROM public.bench_names;

  CREATE INDEX bench_search_names_pinyin_idx
  ON public.bench_search_names
  USING bm25 (id, pinyin)
  WITH (key_field='id');

  ANALYZE public.bench_search_names;

  \\echo '[benchmark][warm] pg_search full query, Rust parser in PostgreSQL: raw query -> pinyin_regex_phrase -> pdb.query -> @@@'
  EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
  SELECT sum(matches)
  FROM public.bench_pinyin_regex_queries q
  CROSS JOIN LATERAL (
    SELECT count(*) AS matches
    FROM public.bench_search_names s
    WHERE s.pinyin @@@ public.pinyin_regex_phrase(q.query)
  ) AS search;

  \\echo '[benchmark][warm] pg_search full query, Rust parser in PostgreSQL, memoize disabled'
  SET jit = off;
  SET enable_memoize = off;
  EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
  SELECT sum(matches)
  FROM public.bench_pinyin_regex_queries q
  CROSS JOIN LATERAL (
    SELECT count(*) AS matches
    FROM public.bench_search_names s
    WHERE s.pinyin @@@ public.pinyin_regex_phrase(q.query)
  ) AS search;
  RESET enable_memoize;
  RESET jit;
\\else
  \\echo '[skip] pg_search is not available; SQL word tokenizer baseline (icu_romanize) skipped.'
  \\echo '[benchmark][cold] Rust extension: pinyin_word_romanize(name)'
  UPDATE pinyin.pinyin_mapping SET pinyin = pinyin WHERE character = ' ';
  EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
  SELECT sum(length(public.pinyin_word_romanize(name)))
  FROM public.bench_names;

  \\echo '[benchmark][warm] Rust extension: pinyin_word_romanize(name)'
  EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
  SELECT sum(length(public.pinyin_word_romanize(name)))
  FROM public.bench_names;

  \\echo '[benchmark][cold] Rust extension (suffix): pinyin_word_romanize(name, ''_$SUFFIX_SQL'')'
  EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
  SELECT sum(length(public.pinyin_word_romanize(name, '_$SUFFIX_SQL')))
  FROM public.bench_names;

  \\echo '[benchmark][warm] Rust extension (suffix): pinyin_word_romanize(name, ''_$SUFFIX_SQL'')'
  EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
  SELECT sum(length(public.pinyin_word_romanize(name, '_$SUFFIX_SQL')))
  FROM public.bench_names;
\\endif
SQL

echo "[benchmark] running with PGURL=$PGURL, ROWS=$ROWS, USER_TABLE_SUFFIX=_$USER_SUFFIX_NORMALIZED"
psql "$PGURL" -f "$TMP_SQL" | tee "$OUT_FILE"

if command -v cargo >/dev/null 2>&1; then
  cargo run --quiet --release --bin benchmark_pinyin_regex_phrase -- \
    --rows "$REGEX_BENCH_ROWS" \
    --token-file "$TOKEN_FILE" \
    --mode tokens \
    | tee -a "$OUT_FILE"
else
  {
    echo ""
    echo "=== Query Builder Benchmark: Rust standalone pinyin_regex_phrase ==="
    echo "[skip] cargo not found; standalone Rust helper benchmark skipped."
  } | tee -a "$OUT_FILE"
fi

if command -v python3 >/dev/null 2>&1; then
  python3 "$ROOT_DIR/scripts/benchmark_pinyin_regex_phrase.py" \
    --rows "$REGEX_BENCH_ROWS" \
    --token-file "$TOKEN_FILE" \
    --mode tokens \
    | tee -a "$OUT_FILE"

  python3 "$ROOT_DIR/scripts/benchmark_pinyin_regex_search.py" \
    --pgurl "$PGURL" \
    --rows "$REGEX_BENCH_ROWS" \
    --token-file "$TOKEN_FILE" \
    | tee -a "$OUT_FILE"
else
  {
    echo ""
    echo "=== Query Builder Benchmark: Python pinyin_regex_phrase ==="
    echo "[skip] python3 not found; Python helper benchmark skipped."
  } | tee -a "$OUT_FILE"
fi

echo "[benchmark] report written to: $OUT_FILE"
