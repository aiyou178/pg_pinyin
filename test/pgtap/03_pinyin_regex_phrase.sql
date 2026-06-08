BEGIN;

CREATE EXTENSION IF NOT EXISTS pgtap;

\set has_pg_search 0
SELECT CASE
  WHEN EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pg_search')
    AND position('pg_search' in current_setting('shared_preload_libraries', true)) > 0 THEN 1
  ELSE 0
END AS has_pg_search
\gset

\if :has_pg_search
DROP EXTENSION IF EXISTS pg_pinyin CASCADE;
CREATE EXTENSION IF NOT EXISTS pg_search;
CREATE EXTENSION pg_pinyin;
\ir ../../sql/pinyin.sql

SELECT plan(38);

SELECT ok(
  to_regprocedure('public.pinyin_regex_phrase(text,integer,integer,boolean)') IS NOT NULL,
  'Rust extension exports pinyin_regex_phrase when pg_search is available before CREATE EXTENSION pg_pinyin'
);

SELECT is(
  (
    SELECT proparallel::text
    FROM pg_proc
    WHERE oid = 'public.pinyin_regex_phrase_patterns(text)'::regprocedure
  ),
  's',
  'pinyin_regex_phrase_patterns(text) is parallel safe'
);

SELECT is(
  (
    SELECT proparallel::text
    FROM pg_proc
    WHERE oid = 'public.pinyin_regex_phrase_patterns(text,boolean)'::regprocedure
  ),
  's',
  'pinyin_regex_phrase_patterns(text, boolean) is parallel safe'
);

SELECT is(
  (
    SELECT proparallel::text
    FROM pg_proc
    WHERE oid = 'public.pinyin_regex_phrase(text,integer,integer,boolean)'::regprocedure
  ),
  's',
  'pinyin_regex_phrase(text, integer, integer, boolean) is parallel safe'
);

DROP TABLE IF EXISTS public.pinyin_regex_parallel_probe;
CREATE TABLE public.pinyin_regex_parallel_probe (
  id integer NOT NULL,
  query text NOT NULL
);
INSERT INTO public.pinyin_regex_parallel_probe (id, query)
SELECT
  gs,
  CASE gs % 4
    WHEN 0 THEN 'zhengshuang'
    WHEN 1 THEN 'whh'
    WHEN 2 THEN 'lunlun'
    ELSE 'changsha'
  END
FROM generate_series(1, 20000) AS gs;
ALTER TABLE public.pinyin_regex_parallel_probe SET (parallel_workers = 4);
ANALYZE public.pinyin_regex_parallel_probe;

SET LOCAL max_parallel_workers_per_gather = 4;
SET LOCAL min_parallel_table_scan_size = 0;
SET LOCAL min_parallel_index_scan_size = 0;
SET LOCAL parallel_setup_cost = 0;
SET LOCAL parallel_tuple_cost = 0;

CREATE OR REPLACE FUNCTION public.pinyin_regex_parallel_plan_text()
RETURNS text
LANGUAGE plpgsql
AS $$
DECLARE
  line text;
  plan_text text := '';
BEGIN
  FOR line IN EXECUTE $explain$
    EXPLAIN (COSTS OFF)
    SELECT count(*)
    FROM public.pinyin_regex_parallel_probe
    WHERE cardinality(public.pinyin_regex_phrase_patterns(query)) > 0
  $explain$
  LOOP
    plan_text := plan_text || line || E'\n';
  END LOOP;

  RETURN plan_text;
END;
$$;

SELECT matches(
  public.pinyin_regex_parallel_plan_text(),
  'Gather',
  'forced regex pattern probe uses a parallel Gather plan'
);

SELECT lives_ok(
  $$SELECT count(*)
    FROM public.pinyin_regex_parallel_probe
    WHERE cardinality(public.pinyin_regex_phrase_patterns(query)) > 0$$,
  'pinyin_regex_phrase_patterns runs safely inside a parallel scan'
);

\ir ../../sql/word.sql

SELECT is(
  public.sql_pinyin_regex_phrase_patterns('lun')::text,
  ARRAY['lun.*']::text[]::text,
  'SQL patterns: single full pinyin token generates one prefix regex'
);

SELECT is(
  public.pinyin_regex_phrase_patterns('lun')::text,
  ARRAY['lun.*']::text[]::text,
  'Rust patterns: single full pinyin token generates one prefix regex'
);

SELECT is(
  public.sql_pinyin_regex_phrase_patterns('lunlun')::text,
  ARRAY['lun.*', 'lun.*']::text[]::text,
  'SQL patterns: repeated full pinyin tokenizes as phrase patterns'
);

SELECT is(
  public.pinyin_regex_phrase_patterns('lunlun')::text,
  ARRAY['lun.*', 'lun.*']::text[]::text,
  'Rust patterns: repeated full pinyin tokenizes as phrase patterns'
);

SELECT is(
  public.sql_pinyin_regex_phrase_patterns('zhengshuang')::text,
  ARRAY['zheng.*', 'shuang.*']::text[]::text,
  'SQL patterns: longest pinyin syllables are preferred'
);

SELECT is(
  public.pinyin_regex_phrase_patterns('zhengshuang')::text,
  ARRAY['zheng.*', 'shuang.*']::text[]::text,
  'Rust patterns: longest pinyin syllables are preferred'
);

SELECT is(
  public.sql_pinyin_regex_phrase_patterns('whh')::text,
  ARRAY['w.*', 'h.*', 'h.*']::text[]::text,
  'SQL patterns: initials are split into individual phrase tokens'
);

SELECT is(
  public.pinyin_regex_phrase_patterns('whh')::text,
  ARRAY['w.*', 'h.*', 'h.*']::text[]::text,
  'Rust patterns: initials are split into individual phrase tokens'
);

SELECT is(
  public.sql_pinyin_regex_phrase_patterns('lun', true)::text,
  ARRAY['.*\|lun.*']::text[]::text,
  'SQL patterns: generated pinyin mode prefixes pipe-aware wildcard'
);

SELECT is(
  public.pinyin_regex_phrase_patterns('lun', true)::text,
  ARRAY['.*\|lun.*']::text[]::text,
  'Rust patterns: generated pinyin mode prefixes pipe-aware wildcard'
);

SELECT ok(
  public.sql_pinyin_regex_phrase('lun') IS NOT NULL,
  'SQL query: single token creates pdb.regex query'
);

SELECT ok(
  public.sql_pinyin_regex_phrase('whh') IS NOT NULL,
  'SQL query: multiple tokens create pdb.regex_phrase query'
);

SELECT ok(
  public.sql_pinyin_regex_phrase('lunlun', 2) IS NOT NULL,
  'multiple tokens accept explicit slope'
);

SELECT ok(
  public.sql_pinyin_regex_phrase('lunlun', NULL, 4) IS NOT NULL,
  'max_expansions defaults slope to zero'
);

SELECT ok(
  public.sql_pinyin_regex_phrase('lun', NULL, NULL, true) IS NOT NULL,
  'generated pinyin mode creates a query'
);

SELECT ok(
  public.pinyin_regex_phrase('lun') IS NOT NULL,
  'Rust query: single token creates pdb.regex query'
);

SELECT ok(
  public.pinyin_regex_phrase('whh') IS NOT NULL,
  'Rust query: multiple tokens create pdb.regex_phrase query'
);

SELECT ok(
  public.pinyin_regex_phrase('lunlun', 2) IS NOT NULL,
  'Rust query: multiple tokens accept explicit slope'
);

SELECT ok(
  public.pinyin_regex_phrase('lunlun', NULL, 4) IS NOT NULL,
  'Rust query: max_expansions defaults slope to zero'
);

SELECT ok(
  public.pinyin_regex_phrase('lun', NULL, NULL, true) IS NOT NULL,
  'Rust query: generated pinyin mode creates a query'
);

SELECT is(
  public.sql_pinyin_regex_phrase_patterns('中文')::text,
  ARRAY[]::text[]::text,
  'SQL patterns: non-English input returns empty token array'
);

SELECT is(
  public.pinyin_regex_phrase_patterns('中文')::text,
  ARRAY[]::text[]::text,
  'Rust patterns: non-English input returns empty token array'
);

SELECT is(
  public.sql_pinyin_regex_phrase_patterns('')::text,
  ARRAY[]::text[]::text,
  'SQL patterns: empty input returns empty token array'
);

SELECT is(
  public.pinyin_regex_phrase_patterns('')::text,
  ARRAY[]::text[]::text,
  'Rust patterns: empty input returns empty token array'
);

SELECT is(
  public.sql_pinyin_regex_phrase_patterns('   ')::text,
  ARRAY[]::text[]::text,
  'SQL patterns: whitespace-only input returns empty token array'
);

SELECT is(
  public.pinyin_regex_phrase_patterns('   ')::text,
  ARRAY[]::text[]::text,
  'Rust patterns: whitespace-only input returns empty token array'
);

SELECT ok(
  public.sql_pinyin_regex_phrase('中文') IS NOT NULL,
  'non-English input creates pdb.empty query'
);

SELECT ok(
  public.sql_pinyin_regex_phrase('') IS NOT NULL,
  'empty input creates pdb.empty query'
);

SELECT ok(
  public.sql_pinyin_regex_phrase('   ') IS NOT NULL,
  'whitespace-only input creates pdb.empty query'
);

SELECT ok(
  public.pinyin_regex_phrase('   ') IS NOT NULL,
  'Rust query: whitespace-only input creates pdb.empty query'
);

DROP TABLE IF EXISTS public.pinyin_regex_null_probe;
CREATE TABLE public.pinyin_regex_null_probe (
  id bigint PRIMARY KEY,
  pinyin text NOT NULL
);
INSERT INTO public.pinyin_regex_null_probe (id, pinyin)
VALUES (1, 'zheng shuang');
CREATE INDEX pinyin_regex_null_probe_idx
ON public.pinyin_regex_null_probe
USING bm25 (id, pinyin)
WITH (key_field='id');

SELECT lives_ok(
  $$SELECT count(*) FROM public.pinyin_regex_null_probe WHERE pinyin @@@ NULL::pdb.query$$,
  '@@@ NULL::pdb.query does not crash pg_search'
);

SELECT lives_ok(
  $$SELECT count(*) FROM public.pinyin_regex_null_probe WHERE pinyin @@@ public.pinyin_regex_phrase('   ')$$,
  'Rust pinyin_regex_phrase empty query is safe in @@@'
);
\else
SELECT plan(1);
SELECT skip('pg_search is not available; pinyin_regex_phrase tests skipped', 1);
\endif

SELECT * FROM finish();

ROLLBACK;
