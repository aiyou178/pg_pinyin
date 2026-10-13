CREATE EXTENSION IF NOT EXISTS pgtap;
CREATE EXTENSION IF NOT EXISTS pg_pinyin;

DROP SCHEMA IF EXISTS pinyin_array_batch_test CASCADE;
DROP TABLE IF EXISTS pinyin.pinyin_mapping_pinyin_array_batch_test;
DROP TABLE IF EXISTS pinyin.pinyin_words_pinyin_array_batch_test;
CREATE SCHEMA pinyin_array_batch_test;
CREATE TABLE pinyin_array_batch_test.probe AS
SELECT '吕梁'::text AS value FROM generate_series(1, 100000);
ALTER TABLE pinyin_array_batch_test.probe SET (parallel_workers = 2);
ANALYZE pinyin_array_batch_test.probe;
CREATE TABLE pinyin.pinyin_mapping_pinyin_array_batch_test (
  character text PRIMARY KEY, pinyin text NOT NULL
);
CREATE TABLE pinyin.pinyin_words_pinyin_array_batch_test (
  word text PRIMARY KEY, pinyin text NOT NULL
);
INSERT INTO pinyin.pinyin_words_pinyin_array_batch_test VALUES ('吕梁', 'lva liangx');

CREATE FUNCTION pinyin_array_batch_test.run_parallel_probe()
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE
  case_row record;
  query text;
  explain_text text;
  nodes jsonb;
  matches bigint;
  no_xid_before boolean;
  results jsonb := '[]'::jsonb;
BEGIN
  FOR case_row IN SELECT * FROM (VALUES
    ('native tokens', 'public.pinyin_word_romanize(ARRAY[value, ''ABC''])',
      '''lv liang abc''::text', 'pinyin_word_romanize'),
    ('native suffix tokens', 'public.pinyin_word_romanize(ARRAY[value, ''ABC''], ''_pinyin_array_batch_test'')',
      '''lva liangx abc''::text', 'pinyin_word_romanize'),
    ('batch documents', 'public.pinyin_word_romanize_batch(ARRAY[value, NULL, ''''])',
      'ARRAY[''lv liang'', NULL, '''']::text[]', 'pinyin_word_romanize_batch'),
    ('batch suffix documents', 'public.pinyin_word_romanize_batch(ARRAY[value, NULL, ''''], ''_pinyin_array_batch_test'')',
      'ARRAY[''lva liangx'', NULL, '''']::text[]', 'pinyin_word_romanize_batch')
  ) AS cases(label, expression, expected, function_name)
  LOOP
    query := format('SELECT count(*) FROM pinyin_array_batch_test.probe WHERE %s = %s',
                    case_row.expression, case_row.expected);
    no_xid_before := pg_current_xact_id_if_assigned() IS NULL;
    EXECUTE 'EXPLAIN (ANALYZE, FORMAT JSON) ' || query INTO explain_text;
    WITH RECURSIVE plan_nodes(node) AS (
      SELECT explain_text::jsonb->0->'Plan'
      UNION ALL
      SELECT child.value FROM plan_nodes CROSS JOIN LATERAL
        jsonb_array_elements(COALESCE(node->'Plans', '[]'::jsonb)) AS child(value)
    ) SELECT jsonb_agg(node) INTO nodes FROM plan_nodes;
    EXECUTE query INTO matches;
    results := results || jsonb_build_array(jsonb_build_object(
      'case', case_row.label,
      'no_xid_before', no_xid_before,
      'no_xid_after', pg_current_xact_id_if_assigned() IS NULL,
      'gather', EXISTS (SELECT 1 FROM jsonb_array_elements(nodes) AS node
                       WHERE node->>'Node Type' IN ('Gather', 'Gather Merge')),
      'workers', (SELECT COALESCE(sum((node->>'Workers Launched')::integer), 0)
                  FROM jsonb_array_elements(nodes) AS node),
      'worker_filter', EXISTS (SELECT 1 FROM jsonb_array_elements(nodes) AS node
                              WHERE (node->>'Parallel Aware')::boolean
                              AND position(case_row.function_name IN COALESCE(node->>'Filter', '')) > 0),
      'matches', matches
    ));
  END LOOP;
  RETURN results;
END;
$$;

-- Capture execution before TAP writes can assign an XID in this transaction.
BEGIN READ ONLY;
SET LOCAL max_parallel_workers_per_gather = 2;
SET LOCAL min_parallel_table_scan_size = 0;
SET LOCAL parallel_setup_cost = 0;
SET LOCAL parallel_tuple_cost = 0;
SET LOCAL jit = off;
SELECT pinyin_array_batch_test.run_parallel_probe() AS parallel_results \gset
COMMIT;

BEGIN;
SELECT no_plan();
SELECT is(jsonb_array_length(:'parallel_results'::jsonb), 4, 'all native/batch parallel cases ran');
SELECT ok((entry->>'no_xid_before')::boolean, entry->>'case' || ': begins without XID')
FROM jsonb_array_elements(:'parallel_results'::jsonb) AS entry;
SELECT ok((entry->>'gather')::boolean, entry->>'case' || ': actual plan contains Gather')
FROM jsonb_array_elements(:'parallel_results'::jsonb) AS entry;
SELECT ok((entry->>'workers')::integer > 0, entry->>'case' || ': actual workers launch')
FROM jsonb_array_elements(:'parallel_results'::jsonb) AS entry;
SELECT ok((entry->>'worker_filter')::boolean, entry->>'case' || ': function executes below Gather')
FROM jsonb_array_elements(:'parallel_results'::jsonb) AS entry;
SELECT is((entry->>'matches')::bigint, 100000::bigint, entry->>'case' || ': all rows match')
FROM jsonb_array_elements(:'parallel_results'::jsonb) AS entry;
SELECT ok((entry->>'no_xid_after')::boolean, entry->>'case' || ': ends without XID')
FROM jsonb_array_elements(:'parallel_results'::jsonb) AS entry;

CREATE TEMP TABLE batch_gold(origin text, expected text);
INSERT INTO batch_gold VALUES
  ('吕梁', 'lv liang'), ('女儿', 'nv er'), ('旅行', 'lv xing'),
  ('戰略', 'zhan lve'), ('阿爾及利亞', 'a er ji li ya'),
  ('丙酸氟替卡鬆', 'bing suan fu ti ka song'), ('遵时養晦', 'zun shi yang hui'),
  ('ABC123', 'abc123'), ('', ''), (NULL, NULL);
SELECT is(public.pinyin_word_romanize_batch(ARRAY[origin]), ARRAY[expected],
          'batch retains scalar hand gold: ' || COALESCE(origin, 'NULL'))
FROM batch_gold;
SELECT is(public.pinyin_word_romanize(ARRAY[origin]), expected,
          'native text[] retains restored phrase/umlaut: ' || origin)
FROM batch_gold WHERE origin IS NOT NULL;
SELECT is(public.pinyin_word_romanize_batch(ARRAY[origin]), ARRAY[public.pinyin_word_romanize(origin)],
          'batch and scalar parity: ' || COALESCE(origin, 'NULL'))
FROM batch_gold;

SELECT is(public.pinyin_word_romanize_batch(NULL), NULL::text[], 'untyped NULL batch is unambiguous/strict');
SELECT is(public.pinyin_word_romanize(NULL), NULL::text, 'legacy untyped NULL stays unambiguous/strict');
SELECT is(public.pinyin_word_romanize(NULL, NULL), NULL::text, 'legacy two untyped NULLs stay unambiguous');
SELECT is(public.pinyin_word_romanize(NULL::text[]), NULL::text, 'native outer NULL array stays strict');
SELECT is(public.pinyin_word_romanize_batch(ARRAY['ABC'], NULL), NULL::text[], 'NULL suffix is strict');
SELECT is(public.pinyin_word_romanize_batch(ARRAY[]::text[]), ARRAY[]::text[], 'empty batch stays empty');
SELECT is(public.pinyin_word_romanize_batch(ARRAY[NULL, NULL]::text[]), ARRAY[NULL, NULL]::text[],
          'all-NULL batch preserves positions');
SELECT is(public.pinyin_word_romanize_batch(ARRAY['ABC', NULL, '', '吕梁']),
          ARRAY['abc', NULL, '', 'lv liang'], 'mixed batch preserves NULL and empty positions');
SELECT is(public.pinyin_word_romanize_batch(ARRAY[['ABC', NULL], ['', '吕梁']]),
          ARRAY['abc', NULL, '', 'lv liang'], 'multidimensional batch flattens row-major');
SELECT is(public.pinyin_word_romanize_batch('[0:2]={ABC,NULL,吕梁}'::text[]),
          ARRAY['abc', NULL, 'lv liang'], 'batch resets non-1 lower bounds');
SELECT is(array_lower(public.pinyin_word_romanize_batch('[0:1]={ABC,吕梁}'::text[]), 1),
          1, 'output batch lower bound is 1');
SELECT is(array_ndims(public.pinyin_word_romanize_batch(ARRAY[['ABC'], ['吕梁']])),
          1, 'output batch has one dimension');
SELECT is(public.pinyin_word_romanize_batch(ARRAY['吕', '梁']),
          ARRAY[public.pinyin_word_romanize('吕'), public.pinyin_word_romanize('梁')],
          'batch elements are independent documents, never joined into phrase tokens');

SAVEPOINT no_dictionary_lookup;
ALTER TABLE pinyin.pinyin_dictionary_meta RENAME TO batch_hidden_meta;
ALTER TABLE pinyin.pinyin_mapping RENAME TO batch_hidden_mapping;
ALTER TABLE pinyin.pinyin_words RENAME TO batch_hidden_words;
SELECT public.pinyin_word_romanize_batch(ARRAY[]::text[]) = ARRAY[]::text[] AS hidden_empty_ok,
       public.pinyin_word_romanize_batch(ARRAY[NULL, NULL]::text[]) = ARRAY[NULL, NULL]::text[] AS hidden_null_ok,
       public.pinyin_word_romanize_batch(ARRAY[]::text[], '_pinyin_array_batch_test') = ARRAY[]::text[] AS hidden_suffix_empty_ok,
       public.pinyin_word_romanize_batch(ARRAY[NULL]::text[], '_pinyin_array_batch_test') = ARRAY[NULL]::text[] AS hidden_suffix_null_ok
\gset
ROLLBACK TO SAVEPOINT no_dictionary_lookup;
SELECT ok(:'hidden_empty_ok'::boolean, 'empty batch needs no base dictionary tables');
SELECT ok(:'hidden_null_ok'::boolean, 'all-NULL batch needs no base dictionary tables');
SELECT ok(:'hidden_suffix_empty_ok'::boolean, 'empty suffix batch needs no dictionary lookup');
SELECT ok(:'hidden_suffix_null_ok'::boolean, 'all-NULL suffix batch needs no dictionary lookup');

CREATE DOMAIN pinyin_array_batch_test.token_domain AS text[];
CREATE TEMP TABLE token_gold(tokens text[], expected text);
INSERT INTO token_gold VALUES
  (ARRAY[' 吕梁 ', '', ' ', 'ABC123'], 'lv liang abc123'),
  (ARRAY['吕', '梁', 'ABC'], 'lv liang abc'),
  (ARRAY[]::text[], ''),
  (ARRAY['ABC-DEF', 'ABC_DEF', 'A.B', 'Ü', '123'], 'ABC-DEF ABC_DEF A.B Ü 123'),
  (ARRAY[U&'\3000ABC\00a0', '', U&'\3000'], 'abc'),
  (ARRAY[E'\tABC\n', ' ', E'\t'], 'abc');
SELECT is(public.pinyin_word_romanize(tokens), expected, 'native token normalization: ' || tokens::text)
FROM token_gold;
SELECT is(public.pinyin_word_romanize(tokens::varchar[]), expected, 'varchar[] SPI fallback: ' || tokens::text)
FROM token_gold;
SELECT is(public.pinyin_word_romanize(tokens::pinyin_array_batch_test.token_domain), expected,
          'domain SPI fallback: ' || tokens::text)
FROM token_gold;
SELECT is(public.pinyin_word_romanize(ARRAY[['吕', '梁'], ['ABC', '123']]),
          'lv liang abc 123', 'native token multidimensional flattening is row-major');
SELECT is(public.pinyin_word_romanize('[0:2]={吕,梁,ABC}'::text[]),
          'lv liang abc', 'native token lower bounds do not change storage order');
SELECT throws_ok($$SELECT public.pinyin_word_romanize(123)$$,
                 '42846', NULL, 'unsupported non-array retains legacy cast error');
SELECT is(public.pinyin_word_romanize(ARRAY[123, 456]::integer[]), '123 456',
          'integer[] castable-to-text[] fallback retains behavior');
SELECT is(public.pinyin_word_romanize_batch(ARRAY['ABC-DEF']), ARRAY[public.pinyin_word_romanize('ABC-DEF')],
          'batch punctuation follows scalar document semantics, not token semantics');

-- EXTERNAL disables compression; sufficiently large input arrays must be toasted.
CREATE TABLE pinyin_array_batch_test.toasted_probe(tokens text[], origins text[]);
ALTER TABLE pinyin_array_batch_test.toasted_probe ALTER COLUMN tokens SET STORAGE EXTERNAL;
ALTER TABLE pinyin_array_batch_test.toasted_probe ALTER COLUMN origins SET STORAGE EXTERNAL;
INSERT INTO pinyin_array_batch_test.toasted_probe
SELECT (SELECT array_agg(repeat('ABCD', 32) || n::text ORDER BY n) FROM generate_series(1, 96) AS n),
       (SELECT array_agg(CASE WHEN n % 17 = 0 THEN NULL WHEN n % 19 = 0 THEN ''
                             ELSE '吕梁ABC' || lpad(n::text, 3, '0') END ORDER BY n)
        FROM generate_series(1, 900) AS n);
CREATE FUNCTION pinyin_array_batch_test.toast_chunk_count(relation regclass)
RETURNS bigint LANGUAGE plpgsql AS $$
DECLARE toast_relation regclass; chunks bigint;
BEGIN
  SELECT reltoastrelid::regclass INTO toast_relation FROM pg_class WHERE oid = relation;
  EXECUTE format('SELECT count(*) FROM %s', toast_relation) INTO chunks;
  RETURN chunks;
END;
$$;
SELECT ok(pg_column_size(tokens) > 8192 AND pg_column_size(origins) > 8192,
          'native/batch input arrays exceed a heap page without compression')
FROM pinyin_array_batch_test.toasted_probe;
SELECT ok(pinyin_array_batch_test.toast_chunk_count('pinyin_array_batch_test.toasted_probe') > 0,
          'fixture has actual external TOAST chunks');
SELECT is(public.pinyin_word_romanize(tokens),
          (SELECT string_agg(repeat('abcd', 32) || n::text, ' ' ORDER BY n) FROM generate_series(1, 96) AS n),
          'native token reader detoasts EXTERNAL arrays without changing normalization')
FROM pinyin_array_batch_test.toasted_probe;
SELECT is(public.pinyin_word_romanize(tokens), public.pinyin_word_romanize(tokens::varchar[]),
          'native toasted tokens match legacy SPI fallback')
FROM pinyin_array_batch_test.toasted_probe;
SELECT is(public.pinyin_word_romanize_batch(origins),
          (SELECT array_agg(CASE WHEN n % 17 = 0 THEN NULL WHEN n % 19 = 0 THEN ''
                                ELSE 'lv liang abc' || lpad(n::text, 3, '0') END ORDER BY n)
           FROM generate_series(1, 900) AS n),
          'batch reader detoasts EXTERNAL document arrays and preserves NULL/empty positions')
FROM pinyin_array_batch_test.toasted_probe;
SELECT is(cardinality(public.pinyin_word_romanize_batch(origins)), 900,
          'toasted batch preserves document count')
FROM pinyin_array_batch_test.toasted_probe;
SELECT is(public.pinyin_word_romanize_batch(origins, '_pinyin_array_batch_test'),
          (SELECT array_agg(CASE WHEN n % 17 = 0 THEN NULL WHEN n % 19 = 0 THEN ''
                                ELSE 'lva liangx abc' || lpad(n::text, 3, '0') END ORDER BY n)
           FROM generate_series(1, 900) AS n),
          'toasted suffix batches preserve independent document overrides')
FROM pinyin_array_batch_test.toasted_probe;

SELECT throws_ok($$SELECT public.pinyin_word_romanize(ARRAY['ABC', NULL]::text[])$$,
                 'XX000', NULL, 'native NULL token remains an error');
SELECT throws_ok($$SELECT public.pinyin_word_romanize(ARRAY['ABC', NULL]::text[], '_pinyin_array_batch_test')$$,
                 'XX000', NULL, 'native suffix NULL token remains an error');
SELECT throws_ok($$SELECT public.pinyin_word_romanize(ARRAY['ABC', NULL]::varchar[])$$,
                 'XX000', NULL, 'fallback NULL token remains an error');
SELECT throws_ok($$SELECT public.pinyin_word_romanize(ARRAY['ABC', NULL]::pinyin_array_batch_test.token_domain)$$,
                 'XX000', NULL, 'domain fallback NULL token remains an error');

SELECT ok(to_regprocedure('public.pinyin_word_romanize(text[])') IS NULL,
          'no text[] overload was added to legacy anyelement API');
SELECT ok(to_regprocedure('public.pinyin_word_romanize(text[],text)') IS NULL,
          'no text[] suffix overload was added to legacy API');
SELECT is(p.proparallel::text, 's', 'batch is parallel safe: ' || p.oid::regprocedure::text)
FROM pg_proc AS p WHERE p.oid IN (
  'public.pinyin_word_romanize_batch(text[])'::regprocedure,
  'public.pinyin_word_romanize_batch(text[],text)'::regprocedure
);
SELECT is(p.provolatile::text, 's', 'batch is stable: ' || p.oid::regprocedure::text)
FROM pg_proc AS p WHERE p.oid IN (
  'public.pinyin_word_romanize_batch(text[])'::regprocedure,
  'public.pinyin_word_romanize_batch(text[],text)'::regprocedure
);
SELECT ok(p.proisstrict, 'batch is strict: ' || p.oid::regprocedure::text)
FROM pg_proc AS p WHERE p.oid IN (
  'public.pinyin_word_romanize_batch(text[])'::regprocedure,
  'public.pinyin_word_romanize_batch(text[],text)'::regprocedure
);
SELECT is(l.lanname::text, 'c', 'batch is directly Rust-backed: ' || p.oid::regprocedure::text)
FROM pg_proc AS p JOIN pg_language AS l ON l.oid = p.prolang WHERE p.oid IN (
  'public.pinyin_word_romanize_batch(text[])'::regprocedure,
  'public.pinyin_word_romanize_batch(text[],text)'::regprocedure
);
SELECT is(pg_get_function_result(signature::regprocedure), 'text[]', 'batch return type: ' || signature)
FROM (VALUES ('public.pinyin_word_romanize_batch(text[])'),
             ('public.pinyin_word_romanize_batch(text[],text)')) AS signatures(signature);
SELECT ok(EXISTS (
  SELECT 1 FROM pg_depend AS d JOIN pg_extension AS e ON e.oid = d.refobjid
  WHERE d.classid = 'pg_proc'::regclass AND d.objid = signature::regprocedure
    AND d.refclassid = 'pg_extension'::regclass AND d.deptype = 'e' AND e.extname = 'pg_pinyin'
), 'extension owns batch API: ' || signature)
FROM (VALUES ('public.pinyin_word_romanize_batch(text[])'),
             ('public.pinyin_word_romanize_batch(text[],text)')) AS signatures(signature);

SELECT is(public.pinyin_word_romanize_batch(ARRAY['吕梁'], '_pinyin_array_batch_test'),
          ARRAY['lva liangx'], 'suffix batch applies override');
SELECT is(public.pinyin_word_romanize_batch(ARRAY['吕梁'], '_missing_batch_suffix'),
          ARRAY['lv liang'], 'missing suffix batch falls back to base');
SELECT is(public.pinyin_word_romanize_batch(ARRAY['吕梁'], ''),
          ARRAY['lv liang'], 'empty suffix batch falls back to base');
UPDATE pinyin.pinyin_words_pinyin_array_batch_test SET pinyin = 'lvb liangy' WHERE word = '吕梁';
SELECT is(public.pinyin_word_romanize_batch(ARRAY['吕梁'], '_pinyin_array_batch_test'),
          ARRAY['lva liangx'], 'suffix cache remains old until explicitly cleared');
SELECT ok(public.pinyin_clear_suffix_cache('_pinyin_array_batch_test'), 'clear batch suffix cache');
SELECT is(public.pinyin_word_romanize_batch(ARRAY['吕梁'], '_pinyin_array_batch_test'),
          ARRAY['lvb liangy'], 'batch suffix override refreshes after clearing');

SAVEPOINT base_dictionary_mutation;
UPDATE pinyin.pinyin_words SET pinyin = 'lvc liangz' WHERE word = '吕梁';
SELECT public.pinyin_word_romanize_batch(ARRAY['吕梁']) = ARRAY['lvc liangz'] AS base_mutation_ok,
       public.pinyin_word_romanize(ARRAY['吕梁']) = 'lvc liangz' AS native_mutation_ok
\gset
UPDATE pinyin.pinyin_words_pinyin_array_batch_test SET pinyin = 'lvd liangw' WHERE word = '吕梁';
UPDATE pinyin.pinyin_words SET pinyin = 'lve liangv' WHERE word = '吕梁';
SELECT public.pinyin_word_romanize_batch(ARRAY['吕梁'], '_pinyin_array_batch_test') = ARRAY['lvd liangw'] AS suffix_mutation_ok
\gset
ROLLBACK TO SAVEPOINT base_dictionary_mutation;
SELECT ok(:'base_mutation_ok'::boolean, 'base version mutation invalidates batch cache between calls');
SELECT ok(:'native_mutation_ok'::boolean, 'native token calls retain per-call base invalidation');
SELECT ok(:'suffix_mutation_ok'::boolean, 'base version mutation also invalidates suffix batch cache');
SELECT is(public.pinyin_word_romanize_batch(ARRAY['吕梁']), ARRAY['lv liang'],
          'batch cache refreshes to base dictionary after rollback');
SELECT is(public.pinyin_word_romanize_batch(ARRAY['吕梁'], '_pinyin_array_batch_test'), ARRAY['lvb liangy'],
          'suffix batch cache refreshes after base-version rollback');

CREATE TEMP TABLE version_reuse_input(phrase text, character text);
INSERT INTO version_reuse_input VALUES ('吕梁', '吕');
SAVEPOINT reused_dictionary_version;
UPDATE pinyin.pinyin_mapping SET pinyin = 'lva' WHERE character = '吕';
UPDATE pinyin.pinyin_words SET pinyin = 'worda phasea' WHERE word = '吕梁';
UPDATE pinyin.pinyin_words_pinyin_array_batch_test SET pinyin = 'suffixa phasea' WHERE word = '吕梁';
SELECT version AS reused_version_a, xmin::text AS reused_xmin_a
FROM pinyin.pinyin_dictionary_meta WHERE singleton
\gset
SELECT jsonb_build_object(
  'native', public.pinyin_word_romanize(ARRAY[phrase]),
  'word', public.pinyin_word_romanize(phrase),
  'batch', public.pinyin_word_romanize_batch(ARRAY[phrase]),
  'suffix_native', public.pinyin_word_romanize(ARRAY[phrase], '_pinyin_array_batch_test'),
  'suffix_word', public.pinyin_word_romanize(phrase, '_pinyin_array_batch_test'),
  'suffix_batch', public.pinyin_word_romanize_batch(ARRAY[phrase], '_pinyin_array_batch_test'),
  'char', public.pinyin_char_romanize(character),
  'suffix_char', public.pinyin_char_romanize(character, '_pinyin_array_batch_test')
) AS reused_results_a FROM version_reuse_input
\gset
ROLLBACK TO SAVEPOINT reused_dictionary_version;
-- Do not romanize between rollback and the new mutation: the counter is reused.
UPDATE pinyin.pinyin_mapping SET pinyin = 'lvb' WHERE character = '吕';
UPDATE pinyin.pinyin_words SET pinyin = 'wordb phaseb' WHERE word = '吕梁';
UPDATE pinyin.pinyin_words_pinyin_array_batch_test SET pinyin = 'suffixb phaseb' WHERE word = '吕梁';
SELECT version AS reused_version_b, xmin::text AS reused_xmin_b
FROM pinyin.pinyin_dictionary_meta WHERE singleton
\gset
SELECT jsonb_build_object(
  'native', public.pinyin_word_romanize(ARRAY[phrase]),
  'word', public.pinyin_word_romanize(phrase),
  'batch', public.pinyin_word_romanize_batch(ARRAY[phrase]),
  'suffix_native', public.pinyin_word_romanize(ARRAY[phrase], '_pinyin_array_batch_test'),
  'suffix_word', public.pinyin_word_romanize(phrase, '_pinyin_array_batch_test'),
  'suffix_batch', public.pinyin_word_romanize_batch(ARRAY[phrase], '_pinyin_array_batch_test'),
  'char', public.pinyin_char_romanize(character),
  'suffix_char', public.pinyin_char_romanize(character, '_pinyin_array_batch_test')
) AS reused_results_b FROM version_reuse_input
\gset
ROLLBACK TO SAVEPOINT reused_dictionary_version;
SELECT is(:'reused_version_b'::bigint, :'reused_version_a'::bigint,
          'rollback followed by equal mutations reuses dictionary counter');
SELECT isnt(:'reused_xmin_b'::text, :'reused_xmin_a'::text, 'reused counter has a distinct metadata tuple xmin');
SELECT is(:'reused_results_a'::jsonb, jsonb_build_object(
  'native', 'worda phasea', 'word', 'worda phasea', 'batch', ARRAY['worda phasea'],
  'suffix_native', 'suffixa phasea', 'suffix_word', 'suffixa phasea', 'suffix_batch', ARRAY['suffixa phasea'],
  'char', 'lva', 'suffix_char', 'lva'
), 'variable-input calls warmed native/base/batch and suffix/char caches with version A');
SELECT is(:'reused_results_b'::jsonb, jsonb_build_object(
  'native', 'wordb phaseb', 'word', 'wordb phaseb', 'batch', ARRAY['wordb phaseb'],
  'suffix_native', 'suffixb phaseb', 'suffix_word', 'suffixb phaseb', 'suffix_batch', ARRAY['suffixb phaseb'],
  'char', 'lvb', 'suffix_char', 'lvb'
), 'same counter and new xmin refresh every native/base/batch/suffix/char cache to version B');
SELECT is(public.pinyin_word_romanize_batch(ARRAY[phrase]), ARRAY['lv liang'],
          'batch refreshes to restored base after the reused-counter rollback') FROM version_reuse_input;
SELECT is(public.pinyin_word_romanize_batch(ARRAY[phrase], '_pinyin_array_batch_test'), ARRAY['lvb liangy'],
          'suffix batch refreshes to restored override after reused-counter rollback') FROM version_reuse_input;
SELECT is(public.pinyin_char_romanize(character), 'lv',
          'char cache refreshes after the reused-counter rollback') FROM version_reuse_input;

SELECT * FROM finish();
ROLLBACK;
DROP SCHEMA pinyin_array_batch_test CASCADE;
DROP TABLE pinyin.pinyin_mapping_pinyin_array_batch_test;
DROP TABLE pinyin.pinyin_words_pinyin_array_batch_test;
