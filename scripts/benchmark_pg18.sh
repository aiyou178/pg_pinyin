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
BENCH_DATASET="${BENCH_DATASET:-synthetic}"
CPP_DATA_DIR="${CPP_DATA_DIR:-$ROOT_DIR/benchmark/testdata/cpp}"
BENCH_SENT_FILE="${BENCH_SENT_FILE:-}"
BENCH_LABEL_FILE="${BENCH_LABEL_FILE:-}"
CPP_ACCURACY_ROWS="${CPP_ACCURACY_ROWS:-$ROWS}"
G2PW_MODEL_PATH="${G2PW_MODEL_PATH:-${HYBRID_MODEL_PATH:-}}"
G2PW_TOKENIZER_PATH="${G2PW_TOKENIZER_PATH:-${HYBRID_TOKENIZER_PATH:-}}"
G2PW_LABELS_PATH="${G2PW_LABELS_PATH:-${HYBRID_LABELS_PATH:-}}"
G2PW_MODEL_CONFIG="${G2PW_MODEL_CONFIG:-${HYBRID_MODEL_CONFIG:-{\"min_confidence\":0.80,\"min_margin\":0.05,\"disable_on_error\":true,\"window_size\":32,\"intra_op_num_threads\":1}}}"
G2PM_MODEL_PATH="${G2PM_MODEL_PATH:-}"
G2PM_MODEL_CONFIG="${G2PM_MODEL_CONFIG:-{\"min_confidence\":0.80,\"min_margin\":0.05,\"disable_on_error\":true}}"
PG_PINYIN_G2PW_WINDOW_SIZE="${PG_PINYIN_G2PW_WINDOW_SIZE:-32}"
PG_PINYIN_G2PW_INTRA_OP_NUM_THREADS="${PG_PINYIN_G2PW_INTRA_OP_NUM_THREADS:-2}"
PG_PINYIN_MODEL_MIN_CONFIDENCE="${PG_PINYIN_MODEL_MIN_CONFIDENCE:-0}"
PG_PINYIN_MODEL_MIN_MARGIN="${PG_PINYIN_MODEL_MIN_MARGIN:-0}"

for numeric_var in ROWS CPP_ACCURACY_ROWS; do
  value="${!numeric_var}"
  if ! [[ "$value" =~ ^[0-9]+$ ]]; then
    echo "$numeric_var must be an integer, got: $value" >&2
    exit 2
  fi
done

if (( CPP_ACCURACY_ROWS < ROWS )); then
  echo "CPP_ACCURACY_ROWS must be >= ROWS for consistent benchmark notes" >&2
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

for cmd in psql python3; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "$cmd not found in PATH" >&2
    exit 2
  fi
done

for file in "$MAPPING_FILE" "$TOKEN_FILE" "$WORDS_FILE"; do
  if [[ ! -f "$file" ]]; then
    echo "missing benchmark data file: $file" >&2
    echo "run ./scripts/generate_data.sh first" >&2
    exit 2
  fi
done

mkdir -p "$(dirname "$OUT_FILE")"

TIMESTAMP_UTC="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
ARCH="$(uname -m)"
EXECUTION_CONTEXT="host"
if [[ -f "/.dockerenv" ]]; then
  EXECUTION_CONTEXT="docker"
fi
BATCH_SCOPE_PUBLIC="per_sql_row_per_sentence"
BATCH_SCOPE_INTERNAL="single_sql_call_grouped_by_sentence"

ROOT_SQL=${ROOT_DIR//\'/\'\'}
MAPPING_SQL=${MAPPING_FILE//\'/\'\'}
TOKEN_SQL=${TOKEN_FILE//\'/\'\'}
WORDS_SQL=${WORDS_FILE//\'/\'\'}
SUFFIX_SQL=${USER_SUFFIX_NORMALIZED//\'/\'\'}
G2PW_MODEL_PATH_SQL=${G2PW_MODEL_PATH//\'/\'\'}
G2PW_TOKENIZER_PATH_SQL=${G2PW_TOKENIZER_PATH//\'/\'\'}
G2PW_LABELS_PATH_SQL=${G2PW_LABELS_PATH//\'/\'\'}
G2PW_MODEL_CONFIG_SQL=${G2PW_MODEL_CONFIG//\'/\'\'}
G2PM_MODEL_PATH_SQL=${G2PM_MODEL_PATH//\'/\'\'}
G2PM_MODEL_CONFIG_SQL=${G2PM_MODEL_CONFIG//\'/\'\'}
PG_PINYIN_G2PW_WINDOW_SIZE_SQL=${PG_PINYIN_G2PW_WINDOW_SIZE//\'/\'\'}
PG_PINYIN_G2PW_INTRA_THREADS_SQL=${PG_PINYIN_G2PW_INTRA_OP_NUM_THREADS//\'/\'\'}
PG_PINYIN_MODEL_MIN_CONFIDENCE_SQL=${PG_PINYIN_MODEL_MIN_CONFIDENCE//\'/\'\'}
PG_PINYIN_MODEL_MIN_MARGIN_SQL=${PG_PINYIN_MODEL_MIN_MARGIN//\'/\'\'}
TMP_SQL="$(mktemp)"
DATASET_TSV="$(mktemp)"
ACCURACY_TSV="$(mktemp)"
trap 'rm -f "$TMP_SQL" "$DATASET_TSV" "$ACCURACY_TSV"' EXIT

HAS_REAL_MODEL=0
if [[ -n "$G2PW_MODEL_PATH" ]]; then
  HAS_REAL_MODEL=1
fi
HAS_G2PW_MODEL=0
if [[ -n "$G2PW_MODEL_PATH" ]]; then
  HAS_G2PW_MODEL=1
fi
HAS_G2PM_MODEL=0
if [[ -n "$G2PM_MODEL_PATH" ]]; then
  HAS_G2PM_MODEL=1
  HAS_REAL_MODEL=1
fi

HAS_CPP_ACCURACY=0

DATASET_LABEL="synthetic"
case "$BENCH_DATASET" in
  synthetic)
    DATASET_SQL=$(cat <<'SQL'
\echo '[setup] generating benchmark dataset (synthetic)'
DROP TABLE IF EXISTS public.bench_names;
CREATE TABLE public.bench_names (
  id bigint PRIMARY KEY,
  name text NOT NULL,
  query_pos integer,
  gold_label text
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
FROM generate_series(1, :rows) AS g
CROSS JOIN dict;

UPDATE public.bench_names
SET query_pos = NULL,
    gold_label = NULL;

ANALYZE public.bench_names;
SQL
)
    ;;
  cpp)
    DATASET_LABEL="cpp"
    HAS_CPP_ACCURACY=1
    : "${BENCH_SENT_FILE:=$CPP_DATA_DIR/test.sent}"
    : "${BENCH_LABEL_FILE:=$CPP_DATA_DIR/test.lb}"
    ;;
  *)
    echo "unsupported BENCH_DATASET: $BENCH_DATASET" >&2
    echo "expected one of: synthetic, cpp" >&2
    exit 2
    ;;
esac

if [[ "$BENCH_DATASET" != "synthetic" ]]; then
  if [[ ! -f "$BENCH_SENT_FILE" ]]; then
    echo "missing BENCH_SENT_FILE: $BENCH_SENT_FILE" >&2
    exit 2
  fi
  if [[ ! -f "$BENCH_LABEL_FILE" ]]; then
    echo "missing BENCH_LABEL_FILE: $BENCH_LABEL_FILE" >&2
    exit 2
  fi

  ROWS="$ROWS" python3 - <<'PY' "$BENCH_SENT_FILE" "$BENCH_LABEL_FILE" > "$DATASET_TSV"
from pathlib import Path
import os
import sys

sent_file = Path(sys.argv[1])
label_file = Path(sys.argv[2])
limit = int(os.environ["ROWS"])

count = 0
with sent_file.open("r", encoding="utf-8") as sfh, label_file.open("r", encoding="utf-8") as lfh:
    for raw, label in zip(sfh, lfh):
        raw = raw.rstrip("\r\n")
        label = label.rstrip("\r\n")
        if not raw:
            continue
        query_pos = raw.find("▁")
        if query_pos < 0:
            continue
        sentence = raw.replace("▁", "")
        count += 1
        print(f"{count}\t{sentence}\t{query_pos}\t{label}")
        if count >= limit:
            break
PY

  ROWS="$CPP_ACCURACY_ROWS" python3 - <<'PY' "$BENCH_SENT_FILE" "$BENCH_LABEL_FILE" > "$ACCURACY_TSV"
from pathlib import Path
import os
import sys

sent_file = Path(sys.argv[1])
label_file = Path(sys.argv[2])
limit = int(os.environ["ROWS"])

count = 0
with sent_file.open("r", encoding="utf-8") as sfh, label_file.open("r", encoding="utf-8") as lfh:
    for raw, label in zip(sfh, lfh):
        raw = raw.rstrip("\r\n")
        label = label.rstrip("\r\n")
        if not raw:
            continue
        query_pos = raw.find("▁")
        if query_pos < 0:
            continue
        sentence = raw.replace("▁", "")
        count += 1
        print(f"{count}\t{sentence}\t{query_pos}\t{label}")
        if count >= limit:
            break
PY

  DATASET_TSV_SQL=${DATASET_TSV//\'/\'\'}
  ACCURACY_TSV_SQL=${ACCURACY_TSV//\'/\'\'}
  DATASET_SQL=$(cat <<SQL
\echo '[setup] loading benchmark dataset from $DATASET_LABEL'
DROP TABLE IF EXISTS public.bench_names;
DROP TABLE IF EXISTS public.bench_names_accuracy;
CREATE TABLE public.bench_names (
  id bigint PRIMARY KEY,
  name text NOT NULL,
  query_pos integer,
  gold_label text
);
CREATE TABLE public.bench_names_accuracy (
  id bigint PRIMARY KEY,
  name text NOT NULL,
  query_pos integer,
  gold_label text
);

\\copy public.bench_names (id, name, query_pos, gold_label) FROM '$DATASET_TSV_SQL' WITH (FORMAT text, NULL '\\N')
\\copy public.bench_names_accuracy (id, name, query_pos, gold_label) FROM '$ACCURACY_TSV_SQL' WITH (FORMAT text, NULL '\\N')
ANALYZE public.bench_names;
ANALYZE public.bench_names_accuracy;
SQL
)
fi

cat > "$TMP_SQL" <<SQL
\\set ON_ERROR_STOP on
\\timing on
\\set rows $ROWS
\\set has_real_model $HAS_REAL_MODEL
\\set has_g2pw_model $HAS_G2PW_MODEL
\\set has_g2pm_model $HAS_G2PM_MODEL
\\set has_cpp_accuracy $HAS_CPP_ACCURACY

\\echo '[setup] ensuring extensions and SQL baseline are loaded'
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS pg_pinyin;
\\i '$ROOT_SQL/sql/pinyin.sql'

\\echo '[setup] applying pg_pinyin model GUCs for this benchmark session'
SET pg_pinyin.g2pw_window_size = '$PG_PINYIN_G2PW_WINDOW_SIZE_SQL';
SET pg_pinyin.g2pw_intra_op_num_threads = '$PG_PINYIN_G2PW_INTRA_THREADS_SQL';
SET pg_pinyin.model_min_confidence = '$PG_PINYIN_MODEL_MIN_CONFIDENCE_SQL';
SET pg_pinyin.model_min_margin = '$PG_PINYIN_MODEL_MIN_MARGIN_SQL';
SHOW pg_pinyin.g2pw_window_size;
SHOW pg_pinyin.g2pw_intra_op_num_threads;
SHOW pg_pinyin.model_min_confidence;
SHOW pg_pinyin.model_min_margin;

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

$DATASET_SQL

\\echo '[setup] resetting model registry'
TRUNCATE TABLE pinyin.pinyin_model_meta, pinyin.pinyin_model_registry;
INSERT INTO pinyin.pinyin_model_meta (singleton, active_model, version)
VALUES (true, NULL, 1)
ON CONFLICT (singleton) DO NOTHING;
UPDATE pinyin.pinyin_model_meta SET active_model = NULL WHERE singleton;

\\if :has_real_model
  \\if :has_g2pw_model
  \\echo '[setup] registering explicit g2pW benchmark model'
  INSERT INTO pinyin.pinyin_model_registry (
    model_name, kind, model_path, tokenizer_path, labels_path, config, enabled
  ) VALUES (
    'bench_g2pw',
    'g2pw_onnx',
    '$G2PW_MODEL_PATH_SQL',
    NULLIF('$G2PW_TOKENIZER_PATH_SQL', ''),
    NULLIF('$G2PW_LABELS_PATH_SQL', ''),
    '$G2PW_MODEL_CONFIG_SQL'::jsonb,
    true
  );
  \\endif
  \\if :has_g2pm_model
  \\echo '[setup] registering explicit g2pM benchmark model'
  INSERT INTO pinyin.pinyin_model_registry (
    model_name, kind, model_path, tokenizer_path, labels_path, config, enabled
  ) VALUES (
    'bench_g2pm',
    'g2pm_numpy',
    '$G2PM_MODEL_PATH_SQL',
    NULL,
    NULL,
    '$G2PM_MODEL_CONFIG_SQL'::jsonb,
    true
  );
  \\endif
\\else
  \\echo '[setup] no model paths provided; model-enabled rows will be skipped'
\\endif

\\set has_pg_search 0
SELECT CASE
  WHEN EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pg_search') THEN 1
  ELSE 0
END AS has_pg_search
\\gset

\\echo ''
\\echo '=== Character Mode Timing ==='
\\echo '[benchmark][cold] SQL baseline: characters2romanize(name)'
EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
SELECT sum(length(public.characters2romanize(name)))
FROM public.bench_names;

\\echo '[benchmark][warm] SQL baseline: characters2romanize(name)'
EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
SELECT sum(length(public.characters2romanize(name)))
FROM public.bench_names;

\\echo '[benchmark][cold] Rust char: pinyin_char_romanize(name)'
UPDATE pinyin.pinyin_mapping SET pinyin = pinyin WHERE character = ' ';
EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
SELECT sum(length(public.pinyin_char_romanize(name)))
FROM public.bench_names;

\\echo '[benchmark][warm] Rust char: pinyin_char_romanize(name)'
EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
SELECT sum(length(public.pinyin_char_romanize(name)))
FROM public.bench_names;

\\echo '[benchmark][cold] Rust char suffix: pinyin_char_romanize(name, ''_$SUFFIX_SQL'')'
EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
SELECT sum(length(public.pinyin_char_romanize(name, '_$SUFFIX_SQL')))
FROM public.bench_names;

\\echo '[benchmark][warm] Rust char suffix: pinyin_char_romanize(name, ''_$SUFFIX_SQL'')'
EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
SELECT sum(length(public.pinyin_char_romanize(name, '_$SUFFIX_SQL')))
FROM public.bench_names;

\\echo ''
\\echo '=== Word Mode Timing ==='
\\echo '[benchmark][cold] Rust word: pinyin_word_romanize(name)'
UPDATE pinyin.pinyin_mapping SET pinyin = pinyin WHERE character = ' ';
EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
SELECT sum(length(public.pinyin_word_romanize(name)))
FROM public.bench_names;

\\echo '[benchmark][warm] Rust word: pinyin_word_romanize(name)'
EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
SELECT sum(length(public.pinyin_word_romanize(name)))
FROM public.bench_names;

\\echo '[benchmark][cold] Rust word suffix: pinyin_word_romanize(name, ''_$SUFFIX_SQL'')'
EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
SELECT sum(length(public.pinyin_word_romanize(name, '_$SUFFIX_SQL'::text)))
FROM public.bench_names;

\\echo '[benchmark][warm] Rust word suffix: pinyin_word_romanize(name, ''_$SUFFIX_SQL'')'
EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
SELECT sum(length(public.pinyin_word_romanize(name, '_$SUFFIX_SQL'::text)))
FROM public.bench_names;

\\if :has_pg_search
  \\echo '[setup] pg_search available; loading SQL word baseline (icu_romanize)'
  CREATE EXTENSION IF NOT EXISTS pg_search;
  \\i '$ROOT_SQL/sql/word.sql'

  \\echo '[benchmark][cold] SQL word baseline: icu_romanize(name::pdb.icu::text[])'
  EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
  SELECT sum(length(public.icu_romanize(name::pdb.icu::text[])))
  FROM public.bench_names;

  \\echo '[benchmark][warm] SQL word baseline: icu_romanize(name::pdb.icu::text[])'
  EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
  SELECT sum(length(public.icu_romanize(name::pdb.icu::text[])))
  FROM public.bench_names;

  \\echo '[benchmark][cold] Rust word tokenizer: pinyin_word_romanize(name::pdb.icu::text[])'
  EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
  SELECT sum(length(public.pinyin_word_romanize(name::pdb.icu::text[])))
  FROM public.bench_names;

  \\echo '[benchmark][warm] Rust word tokenizer: pinyin_word_romanize(name::pdb.icu::text[])'
  EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
  SELECT sum(length(public.pinyin_word_romanize(name::pdb.icu::text[])))
  FROM public.bench_names;

  \\echo '[benchmark][cold] Rust word tokenizer suffix: pinyin_word_romanize(name::pdb.icu::text[], ''_$SUFFIX_SQL'')'
  EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
  SELECT sum(length(public.pinyin_word_romanize(name::pdb.icu::text[], '_$SUFFIX_SQL'::text)))
  FROM public.bench_names;

  \\echo '[benchmark][warm] Rust word tokenizer suffix: pinyin_word_romanize(name::pdb.icu::text[], ''_$SUFFIX_SQL'')'
  EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
  SELECT sum(length(public.pinyin_word_romanize(name::pdb.icu::text[], '_$SUFFIX_SQL'::text)))
  FROM public.bench_names;
\\else
  \\echo '[skip] pg_search is not available; tokenizer-input timing rows skipped.'
\\endif

\\if :has_g2pw_model
  \\echo '[benchmark][cold] Rust word + model: pinyin_word_romanize(name, model => ''g2pw'')'
  UPDATE pinyin.pinyin_model_meta SET version = version + 1 WHERE singleton;
  EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
  SELECT sum(length(public.pinyin_word_romanize(name, model => 'g2pw')))
  FROM public.bench_names;

  \\echo '[benchmark][warm] Rust word + model: pinyin_word_romanize(name, model => ''g2pw'')'
  EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
  SELECT sum(length(public.pinyin_word_romanize(name, model => 'g2pw')))
  FROM public.bench_names;

  \\echo '[benchmark][cold] Rust model-only: pinyin_model_romanize(name, ''g2pw'')'
  UPDATE pinyin.pinyin_model_meta SET version = version + 1 WHERE singleton;
  EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
  SELECT sum(length(public.pinyin_model_romanize(name, 'g2pw')))
  FROM public.bench_names;

  \\echo '[benchmark][warm] Rust model-only: pinyin_model_romanize(name, ''g2pw'')'
  EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
  SELECT sum(length(public.pinyin_model_romanize(name, 'g2pw')))
  FROM public.bench_names;

  \\if :has_pg_search
    \\echo '[benchmark][cold] Rust word tokenizer + model: pinyin_word_romanize(name::pdb.icu::text[], model => ''g2pw'')'
    EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
    SELECT sum(length(public.pinyin_word_romanize(name::pdb.icu::text[], model => 'g2pw')))
    FROM public.bench_names;

    \\echo '[benchmark][warm] Rust word tokenizer + model: pinyin_word_romanize(name::pdb.icu::text[], model => ''g2pw'')'
    EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
    SELECT sum(length(public.pinyin_word_romanize(name::pdb.icu::text[], model => 'g2pw')))
    FROM public.bench_names;
  \\endif

  \\if :has_cpp_accuracy
    \\echo '[benchmark][cold] Internal g2pW helper: pinyin__benchmark_model_target_batch_length(payload, ''g2pw'')'
    UPDATE pinyin.pinyin_model_meta SET version = version + 1 WHERE singleton;
    EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
    WITH payload AS (
      SELECT jsonb_agg(
        jsonb_build_object(
          'sentence', name,
          'query_char_offset', query_pos
        )
        ORDER BY id
      ) AS batch_payload
      FROM public.bench_names
      WHERE query_pos IS NOT NULL
    )
    SELECT public.pinyin__benchmark_model_target_batch_length(batch_payload, 'g2pw')
    FROM payload;

    \\echo '[benchmark][warm] Internal g2pW helper: pinyin__benchmark_model_target_batch_length(payload, ''g2pw'')'
    EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
    WITH payload AS (
      SELECT jsonb_agg(
        jsonb_build_object(
          'sentence', name,
          'query_char_offset', query_pos
        )
        ORDER BY id
      ) AS batch_payload
      FROM public.bench_names
      WHERE query_pos IS NOT NULL
    )
    SELECT public.pinyin__benchmark_model_target_batch_length(batch_payload, 'g2pw')
    FROM payload;
  \\endif
\\endif

\\if :has_g2pm_model
  \\echo '[benchmark][cold] Rust word + model: pinyin_word_romanize(name, model => ''g2pm'')'
  UPDATE pinyin.pinyin_model_meta SET version = version + 1 WHERE singleton;
  EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
  SELECT sum(length(public.pinyin_word_romanize(name, model => 'g2pm')))
  FROM public.bench_names;

  \\echo '[benchmark][warm] Rust word + model: pinyin_word_romanize(name, model => ''g2pm'')'
  EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
  SELECT sum(length(public.pinyin_word_romanize(name, model => 'g2pm')))
  FROM public.bench_names;

  \\echo '[benchmark][cold] Rust model-only: pinyin_model_romanize(name, ''g2pm'')'
  UPDATE pinyin.pinyin_model_meta SET version = version + 1 WHERE singleton;
  EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
  SELECT sum(length(public.pinyin_model_romanize(name, 'g2pm')))
  FROM public.bench_names;

  \\echo '[benchmark][warm] Rust model-only: pinyin_model_romanize(name, ''g2pm'')'
  EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
  SELECT sum(length(public.pinyin_model_romanize(name, 'g2pm')))
  FROM public.bench_names;

  \\if :has_pg_search
    \\echo '[benchmark][cold] Rust word tokenizer + model: pinyin_word_romanize(name::pdb.icu::text[], model => ''g2pm'')'
    EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
    SELECT sum(length(public.pinyin_word_romanize(name::pdb.icu::text[], model => 'g2pm')))
    FROM public.bench_names;

    \\echo '[benchmark][warm] Rust word tokenizer + model: pinyin_word_romanize(name::pdb.icu::text[], model => ''g2pm'')'
    EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)
    SELECT sum(length(public.pinyin_word_romanize(name::pdb.icu::text[], model => 'g2pm')))
    FROM public.bench_names;
  \\endif
\\endif

\\if :has_real_model
\\else
  \\echo '[skip] model-enabled timing rows skipped because no model paths were provided.'
\\endif

\\if :has_cpp_accuracy
\\echo ''
\\echo '=== CPP Accuracy (Toneless) ==='

\\echo '[accuracy] Rust char dictionary target accuracy'
SELECT round(
  100.0 * avg(
    (
      public.pinyin__char_target_romanize(name, query_pos)
      = regexp_replace(gold_label, '[1-5]$', '')
    )::int
  ),
  4
) AS toneless_accuracy_pct
FROM public.bench_names_accuracy
WHERE query_pos IS NOT NULL
  AND gold_label IS NOT NULL;

\\echo '[accuracy] Rust word dictionary target accuracy'
SELECT round(
  100.0 * avg(
    (
      public.pinyin__word_target_romanize(name, query_pos)
      = regexp_replace(gold_label, '[1-5]$', '')
    )::int
  ),
  4
) AS toneless_accuracy_pct
FROM public.bench_names_accuracy
WHERE query_pos IS NOT NULL
  AND gold_label IS NOT NULL;

\\if :has_g2pw_model
  \\echo '[accuracy] Rust word + model target accuracy'
  SELECT round(
    100.0 * avg(
      (
        public.pinyin__word_target_romanize(name, query_pos, '', 'g2pw')
        = regexp_replace(gold_label, '[1-5]$', '')
      )::int
    ),
    4
  ) AS toneless_accuracy_pct
  FROM public.bench_names_accuracy
  WHERE query_pos IS NOT NULL
    AND gold_label IS NOT NULL;

  \\echo '[accuracy] Rust model-only target accuracy'
  SELECT round(
    100.0 * avg(
      (
        public.pinyin__model_target_romanize(name, query_pos, 'g2pw')
        = regexp_replace(gold_label, '[1-5]$', '')
      )::int
    ),
    4
  ) AS toneless_accuracy_pct
  FROM public.bench_names_accuracy
  WHERE query_pos IS NOT NULL
    AND gold_label IS NOT NULL;
\\endif

\\if :has_g2pm_model
  \\echo '[accuracy] Rust word + model target accuracy (g2pm)'
  SELECT round(
    100.0 * avg(
      (
        public.pinyin__word_target_romanize(name, query_pos, '', 'g2pm')
        = regexp_replace(gold_label, '[1-5]$', '')
      )::int
    ),
    4
  ) AS toneless_accuracy_pct
  FROM public.bench_names_accuracy
  WHERE query_pos IS NOT NULL
    AND gold_label IS NOT NULL;

  \\echo '[accuracy] Rust model-only target accuracy (g2pm)'
  SELECT round(
    100.0 * avg(
      (
        public.pinyin__model_target_romanize(name, query_pos, 'g2pm')
        = regexp_replace(gold_label, '[1-5]$', '')
      )::int
    ),
    4
  ) AS toneless_accuracy_pct
  FROM public.bench_names_accuracy
  WHERE query_pos IS NOT NULL
    AND gold_label IS NOT NULL;
\\endif

\\if :has_real_model
\\else
  \\echo '[skip] model-enabled CPP accuracy rows skipped because no model paths were provided.'
\\endif
\\endif
SQL

echo "[benchmark] running with PGURL=$PGURL, ROWS=$ROWS, USER_TABLE_SUFFIX=_$USER_SUFFIX_NORMALIZED, BENCH_DATASET=$DATASET_LABEL"
if [[ "$BENCH_DATASET" != "synthetic" ]]; then
  echo "[benchmark] sentence file: $BENCH_SENT_FILE"
  echo "[benchmark] label file: $BENCH_LABEL_FILE"
  echo "[benchmark] cpp timing rows: $ROWS"
  echo "[benchmark] cpp accuracy rows: $CPP_ACCURACY_ROWS"
fi
if [[ "$HAS_G2PW_MODEL" == "1" ]]; then
  echo "[benchmark] model path: $G2PW_MODEL_PATH"
fi
if [[ "$HAS_G2PM_MODEL" == "1" ]]; then
  echo "[benchmark] g2pm manifest path: $G2PM_MODEL_PATH"
fi

{
  echo "[metadata] timestamp_utc=$TIMESTAMP_UTC"
  echo "[metadata] execution_context=$EXECUTION_CONTEXT"
  echo "[metadata] arch=$ARCH"
  echo "[metadata] bench_dataset=$DATASET_LABEL"
  echo "[metadata] rows=$ROWS"
  echo "[metadata] cpp_accuracy_rows=$CPP_ACCURACY_ROWS"
  echo "[metadata] has_g2pw_model=$HAS_G2PW_MODEL"
  echo "[metadata] has_g2pm_model=$HAS_G2PM_MODEL"
  echo "[metadata] g2pw_window_size=$PG_PINYIN_G2PW_WINDOW_SIZE"
  echo "[metadata] g2pw_intra_op_num_threads=$PG_PINYIN_G2PW_INTRA_OP_NUM_THREADS"
  echo "[metadata] model_min_confidence=$PG_PINYIN_MODEL_MIN_CONFIDENCE"
  echo "[metadata] model_min_margin=$PG_PINYIN_MODEL_MIN_MARGIN"
  echo "[metadata] public_api_batch_scope=$BATCH_SCOPE_PUBLIC"
  echo "[metadata] internal_helper_batch_scope=$BATCH_SCOPE_INTERNAL"
  if [[ "$BENCH_DATASET" != "synthetic" ]]; then
    echo "[metadata] bench_sent_file=$BENCH_SENT_FILE"
    echo "[metadata] bench_label_file=$BENCH_LABEL_FILE"
  fi
  if [[ "$HAS_G2PW_MODEL" == "1" ]]; then
    echo "[metadata] g2pw_model_path=$G2PW_MODEL_PATH"
  fi
  if [[ "$HAS_G2PM_MODEL" == "1" ]]; then
    echo "[metadata] g2pm_model_path=$G2PM_MODEL_PATH"
  fi
  echo
} | tee "$OUT_FILE"

psql "$PGURL" -v BENCH_DATASET="$BENCH_DATASET" -f "$TMP_SQL" | tee -a "$OUT_FILE"

echo "[benchmark] report written to: $OUT_FILE"
