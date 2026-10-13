CREATE EXTENSION IF NOT EXISTS pgtap;
SELECT CASE WHEN EXISTS (
  SELECT 1 FROM pg_available_extensions WHERE name = 'pg_search'
) AND position('pg_search' in current_setting('shared_preload_libraries', true)) > 0
THEN 1 ELSE 0 END AS has_pg_search
\gset
\if :has_pg_search
CREATE EXTENSION IF NOT EXISTS pg_search CASCADE;
CREATE EXTENSION IF NOT EXISTS pg_pinyin;
\ir ../../sql/pinyin.sql
\ir ../../sql/word.sql

DROP SCHEMA IF EXISTS pinyin_phrase_test CASCADE;
CREATE SCHEMA pinyin_phrase_test;
CREATE TABLE pinyin_phrase_test.parallel_probe AS
SELECT CASE n % 4 WHEN 0 THEN 'an' WHEN 1 THEN 'xi''an'
       WHEN 2 THEN 'LǛ LIÁNG' ELSE 'invalid!' END AS value
FROM generate_series(1,100000) AS n;
ALTER TABLE pinyin_phrase_test.parallel_probe SET (parallel_workers = 2);
ANALYZE pinyin_phrase_test.parallel_probe;

CREATE FUNCTION pinyin_phrase_test.run_parallel_probe()
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE
  case_row record;
  query text;
  explain_text text;
  nodes jsonb;
  matched bigint;
  no_xid_before boolean;
  results jsonb := '[]'::jsonb;
BEGIN
  FOR case_row IN SELECT * FROM (VALUES
    ('Rust exact', 'pinyin_phrase', 'NULL'),
    ('Rust slop', 'pinyin_phrase', '1'),
    ('SQL exact', 'sql_pinyin_phrase', '0'),
    ('SQL slop', 'sql_pinyin_phrase', '2')
  ) AS cases(label, function_name, slope)
  LOOP
    query := format('SELECT count(*) FROM pinyin_phrase_test.parallel_probe '
                    'WHERE public.%I(value,%s)::text <> pdb.empty()::text',
                    case_row.function_name, case_row.slope);
    no_xid_before := pg_current_xact_id_if_assigned() IS NULL;
    EXECUTE 'EXPLAIN (ANALYZE, FORMAT JSON) ' || query INTO explain_text;
    WITH RECURSIVE plan_nodes(node) AS (
      SELECT explain_text::jsonb->0->'Plan'
      UNION ALL
      SELECT child.value FROM plan_nodes CROSS JOIN LATERAL
        jsonb_array_elements(COALESCE(node->'Plans', '[]'::jsonb)) AS child(value)
    ) SELECT jsonb_agg(node) INTO nodes FROM plan_nodes;
    EXECUTE query INTO matched;
    results := results || jsonb_build_array(jsonb_build_object(
      'case', case_row.label,
      'no_xid_before', no_xid_before,
      'no_xid_after', pg_current_xact_id_if_assigned() IS NULL,
      'gather', EXISTS (SELECT 1 FROM jsonb_array_elements(nodes) AS node
                       WHERE node->>'Node Type' IN ('Gather','Gather Merge')),
      'workers', (SELECT COALESCE(sum((node->>'Workers Launched')::integer),0)
                  FROM jsonb_array_elements(nodes) AS node),
      'worker_filter', EXISTS (SELECT 1 FROM jsonb_array_elements(nodes) AS node
                              WHERE (node->>'Parallel Aware')::boolean
                              AND position(case_row.function_name IN COALESCE(node->>'Filter','')) > 0),
      'matched', matched
    ));
  END LOOP;
  RETURN results;
END;
$$;

-- Read-only capture precedes TAP temporary-table writes and any assigned XID.
BEGIN READ ONLY;
SET LOCAL max_parallel_workers_per_gather = 2;
SET LOCAL min_parallel_table_scan_size = 0;
SET LOCAL parallel_setup_cost = 0;
SET LOCAL parallel_tuple_cost = 0;
SET LOCAL jit = off;
SELECT pinyin_phrase_test.run_parallel_probe() AS parallel_results \gset
COMMIT;

BEGIN;
SELECT no_plan();
SELECT is(jsonb_array_length(:'parallel_results'::jsonb),4,'all native constructor parallel cases ran');
SELECT ok((entry->>'no_xid_before')::boolean,entry->>'case' || ': begins without XID')
FROM jsonb_array_elements(:'parallel_results'::jsonb) AS entry;
SELECT ok((entry->>'gather')::boolean,entry->>'case' || ': Gather plan exists')
FROM jsonb_array_elements(:'parallel_results'::jsonb) AS entry;
SELECT ok((entry->>'workers')::integer > 0,entry->>'case' || ': actual workers launched')
FROM jsonb_array_elements(:'parallel_results'::jsonb) AS entry;
SELECT ok((entry->>'worker_filter')::boolean,entry->>'case' || ': query constructor executes below Gather')
FROM jsonb_array_elements(:'parallel_results'::jsonb) AS entry;
SELECT is((entry->>'matched')::bigint,75000::bigint,entry->>'case' || ': exact hand-labeled count')
FROM jsonb_array_elements(:'parallel_results'::jsonb) AS entry;
SELECT ok((entry->>'no_xid_after')::boolean,entry->>'case' || ': ends without XID')
FROM jsonb_array_elements(:'parallel_results'::jsonb) AS entry;

SELECT is(p.proparallel::text,'s','native helper is catalogued parallel safe: ' || p.oid::regprocedure::text)
FROM pg_proc AS p WHERE p.oid IN (
  'public.pinyin_phrase(text,integer)'::regprocedure,
  'public.sql_pinyin_phrase(text,integer)'::regprocedure
);
SELECT is(p.provolatile::text,'s','native helper remains stable: ' || p.oid::regprocedure::text)
FROM pg_proc AS p WHERE p.oid IN (
  'public.pinyin_phrase(text,integer)'::regprocedure,
  'public.sql_pinyin_phrase(text,integer)'::regprocedure
);
SELECT ok(NOT p.proisstrict AND p.pronargdefaults = 1,
          'native helper permits nullable default slope: ' || p.oid::regprocedure::text)
FROM pg_proc AS p WHERE p.oid IN (
  'public.pinyin_phrase(text,integer)'::regprocedure,
  'public.sql_pinyin_phrase(text,integer)'::regprocedure
);
SELECT ok(EXISTS (
  SELECT 1 FROM pg_depend d JOIN pg_extension e ON e.oid=d.refobjid
  WHERE d.classid='pg_proc'::regclass AND d.objid='public.pinyin_phrase(text,integer)'::regprocedure
    AND d.deptype='e' AND e.extname='pg_pinyin'
),'Rust-backed native helper is extension-owned');
SELECT ok(NOT EXISTS (
  SELECT 1 FROM pg_depend d JOIN pg_extension e ON e.oid=d.refobjid
  WHERE d.classid='pg_proc'::regclass AND d.objid='public.sql_pinyin_phrase(text,integer)'::regprocedure
    AND d.deptype='e' AND e.extname='pg_pinyin'
),'SQL native helper stays independent of the Rust extension');

SELECT is(public.pinyin_phrase('an')::text,pdb.term('an')::text,'single full syllable is an exact native term');
SELECT is(public.sql_pinyin_phrase('an')::text,pdb.term('an')::text,'SQL single syllable is an exact native term');
SELECT is(public.pinyin_phrase('xi''an')::text,pdb.phrase_array(ARRAY['xi','an'])::text,
          'Rust hard boundary uses a pretokenized phrase array');
SELECT is(public.sql_pinyin_phrase('xi’an')::text,pdb.phrase_array(ARRAY['xi','an'])::text,
          'SQL curly boundary uses a pretokenized phrase array');
SELECT is(public.pinyin_phrase('xi an',2)::text,
          paradedb.slop_to_query(paradedb.query_to_slop(pdb.phrase_array(ARRAY['xi','an']),2,true))::text,
          'positive slope is native phrase slop');
SELECT is(public.sql_pinyin_phrase('xi an',2)::text,public.pinyin_phrase('xi an',2)::text,
          'SQL and Rust phrase slop have identical query structure');
SELECT is(public.pinyin_phrase('xi an',0)::text,public.pinyin_phrase('xi an',NULL)::text,
          'zero and NULL slope are the same native phrase');
SELECT is(public.pinyin_phrase('an',2)::text,pdb.term('an')::text,'slop does not change a single term');
SELECT ok(public.pinyin_phrase(NULL) IS NULL AND public.sql_pinyin_phrase(NULL) IS NULL,
          'native helpers preserve SQL NULL');
SELECT ok(public.pinyin_phrase(NULL,-1) IS NULL AND public.sql_pinyin_phrase(NULL,-1) IS NULL,
          'NULL value is returned before slope validation');
SELECT throws_ok($$SELECT public.pinyin_phrase('xi an',-1)$$,'22023',
                 'pinyin phrase slope must be non-negative','Rust rejects negative native slope');
SELECT throws_ok($$SELECT public.sql_pinyin_phrase('invalid!',-1)$$,'22023',
                 'pinyin phrase slope must be non-negative','SQL rejects negative slope before parsing');

CREATE TABLE pinyin_phrase_test.gold(id bigint PRIMARY KEY,plain text NOT NULL,mixed text NOT NULL);
INSERT INTO pinyin_phrase_test.gold VALUES
  (1,'an','|an|'), (2,'ang','|ang|'), (3,'an an','|an| |an|'),
  (4,'xian','|xian|'), (5,'xi an','|xi| |an|'), (6,'xi ang','|xi| |ang|'),
  (7,'xi hao an','|xi| |hao| |an|'), (8,'xi hao hao an','|xi| |hao| |hao| |an|'),
  (9,'an xi','|an| |xi|'), (10,'xi xi an','|xi| |xi| |an|'),
  (11,'xi an xi an','|xi| |an| |xi| |an|'), (12,'lv liang','|lv| |liang|'),
  (13,'lve liang','|lve| |liang|'), (14,'an an an','|an| |an| |an|'),
  (15,'|xi| |an|','|xi| |an|'), (16,'wu han han','|wu| |han| |han|'),
  (17,'wu han hang','|wu| |han| |hang|');
CREATE INDEX pinyin_phrase_gold_bm25 ON pinyin_phrase_test.gold USING bm25
(id,(plain::pdb.whitespace),(mixed::pdb.whitespace)) WITH (key_field='id');

CREATE TEMP TABLE native_gold(label text,value text,slope integer,expected bigint[]);
INSERT INTO native_gold VALUES
  ('an not ang','an',NULL,ARRAY[1,3,5,7,8,9,10,11,14]),
  ('an positive slop is still a term','an',2,ARRAY[1,3,5,7,8,9,10,11,14]),
  ('one joined syllable','xian',NULL,ARRAY[4]),
  ('straight boundary','xi''an',NULL,ARRAY[5,10,11]),
  ('right curly boundary','xi’an',0,ARRAY[5,10,11]),
  ('left curly boundary','xi‘an',NULL,ARRAY[5,10,11]),
  ('gap one','xi an',1,ARRAY[5,7,10,11]),
  ('gap two also permits transposition','xi an',2,ARRAY[5,7,8,9,10,11]),
  ('exact reverse phrase','an xi',0,ARRAY[9,11]),
  ('repeat adjacent','an an',0,ARRAY[3,14]),
  ('umlaut with tones','LǛ LIÁNG',NULL,ARRAY[12]),
  ('umlaut numeric colon','lu:4liang2',NULL,ARRAY[12]),
  ('three syllables','wu han han',NULL,ARRAY[16]),
  ('initials are not full syllables','whh',NULL,ARRAY[]::bigint[]),
  ('separator only','''‘’',NULL,ARRAY[]::bigint[]),
  ('empty','',NULL,ARRAY[]::bigint[]),
  ('invalid punctuation','xi-an',NULL,ARRAY[]::bigint[]),
  ('invalid trailing punctuation','xi''an!',NULL,ARRAY[]::bigint[]),
  ('invalid Chinese','中文',NULL,ARRAY[]::bigint[]),
  ('over input limit',repeat('a',257),NULL,ARRAY[]::bigint[]),
  ('over token limit',repeat('a ',65),NULL,ARRAY[]::bigint[]),
  ('outer NULL',NULL,NULL,ARRAY[]::bigint[]);

CREATE FUNCTION pinyin_phrase_test.ids(value text,slope integer,backend text)
RETURNS bigint[] LANGUAGE plpgsql AS $$
DECLARE ids bigint[];
BEGIN
  EXECUTE format('SELECT coalesce(array_agg(id ORDER BY id),ARRAY[]::bigint[]) '
                 'FROM pinyin_phrase_test.gold WHERE plain @@@ public.%I($1,$2)',
                 CASE backend WHEN 'rust' THEN 'pinyin_phrase' ELSE 'sql_pinyin_phrase' END)
  INTO ids USING value,slope;
  RETURN ids;
END;
$$;
SELECT is(pinyin_phrase_test.ids(value,slope,backend),expected,
          backend || ' native hand-labeled hit set: ' || label)
FROM native_gold CROSS JOIN (VALUES ('rust'),('sql')) AS backends(backend);
SELECT results_eq(
  $$SELECT id FROM pinyin_phrase_test.gold WHERE plain @@@ public.pinyin_phrase('xi an',2) ORDER BY id$$,
  $$VALUES (5::bigint),(7::bigint),(8::bigint),(9::bigint),(10::bigint),(11::bigint)$$,
  'native slop two also admits the two-position transposition an xi');
SELECT results_eq(
  $$SELECT id FROM pinyin_phrase_test.gold WHERE mixed @@@ public.pinyin_phrase('xi an') ORDER BY id$$,
  $$SELECT 1::bigint WHERE false$$,'native helper does not silently expand pipe alternatives');

SELECT results_eq(
  format('SELECT id FROM pinyin_phrase_test.gold WHERE plain @@@ public.pinyin_phrase(%L) ORDER BY id',value),
  format('SELECT id FROM pinyin_phrase_test.gold WHERE plain @@@ public.pinyin_regex_phrase_mode(%L,''full'') ORDER BY id',value),
  'zero-slop native and unchanged full regex hit parity: ' || label)
FROM native_gold WHERE COALESCE(slope,0)=0 AND value IS NOT NULL;
SELECT results_eq(
  format('SELECT id FROM pinyin_phrase_test.gold WHERE plain @@@ public.pinyin_phrase(%L,%s) ORDER BY id',value,slope),
  format('SELECT id FROM pinyin_phrase_test.gold WHERE plain @@@ public.pinyin_regex_phrase_mode(%L,''full'',%s) ORDER BY id',value,slope),
  format('native/regex slop hit parity includes reverse and repeated terms: %s / slope=%s',value,slope))
FROM (VALUES ('xi an'),('an an')) AS queries(value)
CROSS JOIN (VALUES (0),(1),(2)) AS slopes(slope);
-- This is an upstream scorer limitation, not a linguistic retrieval gold case:
-- positive slop may reuse one occurrence for a repeated query term. Preserve
-- the exact zero-slop repeated-token gold above and expose this behavior here.
SELECT results_eq(
  $$SELECT id FROM pinyin_phrase_test.gold WHERE plain @@@ public.pinyin_phrase('an an',1) ORDER BY id$$,
  $$VALUES (1::bigint),(3::bigint),(5::bigint),(7::bigint),(8::bigint),(9::bigint),(10::bigint),(11::bigint),(14::bigint)$$,
  'known upstream limitation: native positive slop reuses a single an occurrence');
SELECT results_eq(
  $$SELECT id FROM pinyin_phrase_test.gold WHERE plain @@@ public.pinyin_regex_phrase_mode('an an','full',1) ORDER BY id$$,
  $$VALUES (1::bigint),(3::bigint),(5::bigint),(7::bigint),(8::bigint),(9::bigint),(10::bigint),(11::bigint),(14::bigint)$$,
  'known upstream limitation also affects unchanged regex positive slop');
SELECT results_eq(
  $$SELECT id FROM pinyin_phrase_test.gold WHERE id=4 OR plain @@@ public.pinyin_phrase('invalid!') ORDER BY id$$,
  $$VALUES (4::bigint)$$,'empty native query is a vanilla false OR part');
SELECT results_eq(
  $$SELECT id FROM pinyin_phrase_test.gold WHERE id=4 AND plain @@@ public.sql_pinyin_phrase('invalid!') ORDER BY id$$,
  $$SELECT 1::bigint WHERE false$$,'empty SQL native query is false in an AND branch');

CREATE FUNCTION pinyin_phrase_test.ranked(value text,backend text,mixed_or boolean)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE ranked jsonb;
BEGIN
  EXECUTE format('SELECT coalesce(jsonb_agg(jsonb_build_array(id,score) ORDER BY score DESC,id),''[]''::jsonb) '
                 'FROM (SELECT id,pdb.score(id) AS score FROM pinyin_phrase_test.gold '
                 'WHERE plain @@@ public.%I($1)%s) AS found',
                 CASE backend WHEN 'rust' THEN 'pinyin_phrase' ELSE 'sql_pinyin_phrase' END,
                 CASE WHEN mixed_or THEN ' OR plain @@@ pdb.term(''liang'')' ELSE '' END)
  INTO ranked USING value;
  RETURN ranked;
END;
$$;
SELECT is(pinyin_phrase_test.ranked(value,'rust',mixed_or),pinyin_phrase_test.ranked(value,'sql',mixed_or),
          format('native exact scores and ranked IDs agree across backends: %s / OR=%s',value,mixed_or))
FROM (VALUES ('an'),('xi an'),('lv liang')) AS queries(value)
CROSS JOIN (VALUES (false),(true)) AS clauses(mixed_or);
SELECT ok((SELECT count(DISTINCT score)>1 FROM
           (SELECT pdb.score(id) AS score FROM pinyin_phrase_test.gold WHERE plain @@@ public.pinyin_phrase('an')) AS found),
          'native single-term BM25 varies with frequency and length');
SELECT is((SELECT count(DISTINCT score) FROM
           (SELECT pdb.score(id) AS score FROM pinyin_phrase_test.gold
            WHERE plain @@@ public.pinyin_regex_phrase_mode('an','full')) AS found),
          1::bigint,'unchanged single regex remains constant-score, not native BM25');
SELECT ok((SELECT jsonb_agg(jsonb_build_array(id,score) ORDER BY score DESC,id) FROM
           (SELECT id,pdb.score(id) AS score FROM pinyin_phrase_test.gold
            WHERE plain @@@ public.pinyin_regex_phrase_mode('an','full') OR plain @@@ pdb.term('liang')) AS found)
          IS DISTINCT FROM pinyin_phrase_test.ranked('an','rust',true),
          'native scores differ in mixed OR too: new helper is opt-in, not a silent regex replacement');

SELECT is(public.pinyin_regex_phrase_patterns_mode('xi''an','full'),ARRAY['xi','an'],
          'existing full patterns remain exact regex literals');
SELECT is(public.pinyin_regex_phrase_patterns_mode('whh','initials'),ARRAY['w.*','h.*','h.*'],
          'existing initials still split w h h');
SELECT is(public.pinyin_regex_phrase_patterns_mode('xi''an','autocomplete'),ARRAY['xi','an.*'],
          'existing autocomplete keeps final prefix');
SELECT is(public.pinyin_regex_phrase_patterns_mode('xi''an','full',true),
          ARRAY['(xi|.*\|xi\|.*)','(an|.*\|an\|.*)'],'pipe-aware full mode remains regex-based');
SELECT is(public.pinyin_regex_phrase_mode('xi an','full',NULL,5)::text,
          pdb.regex_phrase(ARRAY['xi','an'],0,5)::text,'existing max_expansions is untouched');

-- A tokenizer may split a full syllable. Native tokens must not be re-analyzed.
CREATE TABLE pinyin_phrase_test.custom_analyzer(id bigint PRIMARY KEY,pinyin text NOT NULL);
INSERT INTO pinyin_phrase_test.custom_analyzer VALUES (1,'xian an');
CREATE INDEX pinyin_phrase_custom_bm25 ON pinyin_phrase_test.custom_analyzer USING bm25
(id,(pinyin::pdb.regex_pattern('xi|an'))) WITH (key_field='id');
SELECT is('xian an'::pdb.regex_pattern('xi|an')::text[],ARRAY['xi','an','an'],
          'custom analyzer deliberately splits xian into xi/an');
SELECT results_eq(
  $$SELECT id FROM pinyin_phrase_test.custom_analyzer WHERE pinyin @@@ public.pinyin_phrase('xian an')$$,
  $$SELECT 1::bigint WHERE false$$,'Rust native phrase does not re-analyze exact full tokens');
SELECT results_eq(
  $$SELECT id FROM pinyin_phrase_test.custom_analyzer WHERE pinyin @@@ public.sql_pinyin_phrase('xian an')$$,
  $$SELECT 1::bigint WHERE false$$,'SQL native phrase does not re-analyze exact full tokens');
SELECT results_eq(
  $$SELECT id FROM pinyin_phrase_test.custom_analyzer WHERE pinyin @@@ public.pinyin_regex_phrase_mode('xian an','full')$$,
  $$SELECT 1::bigint WHERE false$$,'native custom-analyzer hit semantics agree with exact full regex');
SELECT results_eq(
  $$SELECT id FROM pinyin_phrase_test.custom_analyzer WHERE pinyin @@@ pdb.phrase(ARRAY['xian','an'])$$,
  $$VALUES (1::bigint)$$,'pdb.phrase(text[]) is a different re-tokenizing path and must not replace phrase_array');

SAVEPOINT live_sql_dictionary;
INSERT INTO pinyin.pinyin_token(character,category) VALUES ('zz',1)
ON CONFLICT (character) DO UPDATE SET category=EXCLUDED.category;
SELECT public.pinyin_phrase('zz')::text = pdb.empty()::text AS rust_static_dictionary,
       public.sql_pinyin_phrase('zz')::text = pdb.term('zz')::text AS sql_live_dictionary
\gset
ROLLBACK TO live_sql_dictionary;
SELECT ok(:'rust_static_dictionary'::boolean,'Rust native query still uses the embedded immutable token inventory');
SELECT ok(:'sql_live_dictionary'::boolean,'separate SQL native query retains live token table behavior');

SELECT * FROM finish();
ROLLBACK;
DROP SCHEMA pinyin_phrase_test CASCADE;
\else
SELECT plan(1);
SELECT skip('native phrase checks require preloaded pg_search',1);
SELECT * FROM finish();
\endif
