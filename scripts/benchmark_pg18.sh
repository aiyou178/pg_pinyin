#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PGURL="${PGURL:-postgres://localhost/postgres}"
ROWS="${ROWS:-2000}"
OUT_FILE="${1:-$ROOT_DIR/benchmark_pg18_report.txt}"
MAPPING_FILE="${MAPPING_FILE:-$ROOT_DIR/sql/data/pinyin_mapping.csv}"
TOKEN_FILE="${TOKEN_FILE:-$ROOT_DIR/sql/data/pinyin_token.csv}"
WORDS_FILE="${WORDS_FILE:-$ROOT_DIR/sql/data/pinyin_words.csv}"
USER_TABLE_SUFFIX="${USER_TABLE_SUFFIX:-_bench}"

if ! [[ "$ROWS" =~ ^[0-9]+$ ]]; then
  echo "ROWS must be an integer, got: $ROWS" >&2
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

SELECT public.pinyin_register_suffix('_$SUFFIX_SQL');

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

\\echo ''
\\echo '=== Full Tokenization Benchmark: Character Mode ==='
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
\\set has_pg_search 0
SELECT CASE
  WHEN EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pg_search') THEN 1
  ELSE 0
END AS has_pg_search
\\gset

\\if :has_pg_search
  \\echo '[setup] pg_search available; loading SQL word tokenizer baseline (icu_romanize)'
  CREATE EXTENSION IF NOT EXISTS pg_search;
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

echo "[benchmark] report written to: $OUT_FILE"
