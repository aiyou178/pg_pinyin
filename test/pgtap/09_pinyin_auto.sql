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

DROP SCHEMA IF EXISTS pinyin_auto_test CASCADE;
CREATE SCHEMA pinyin_auto_test;
CREATE TABLE pinyin_auto_test.parallel_probe AS
SELECT CASE n % 4 WHEN 0 THEN 'whh' WHEN 1 THEN 'xi a'
       WHEN 2 THEN 'xi ang' ELSE 'invalid!' END AS value
FROM generate_series(1,100000) AS n;
ALTER TABLE pinyin_auto_test.parallel_probe SET (parallel_workers = 2);
ANALYZE pinyin_auto_test.parallel_probe;

CREATE FUNCTION pinyin_auto_test.run_parallel_probe()
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
    ('Rust patterns', 'pinyin_regex_phrase_patterns_mode', true),
    ('SQL patterns', 'sql_pinyin_regex_phrase_patterns_mode', true),
    ('Rust Auto constructor', 'pinyin_regex_phrase_mode', false),
    ('SQL Auto constructor', 'sql_pinyin_regex_phrase_mode', false)
  ) AS cases(label, function_name, patterns)
  LOOP
    query := CASE WHEN case_row.patterns THEN
      format('SELECT count(*) FROM pinyin_auto_test.parallel_probe '
             'WHERE cardinality(public.%I(value,''auto'')) > 0', case_row.function_name)
    ELSE
      format('SELECT count(*) FROM pinyin_auto_test.parallel_probe '
             'WHERE public.%I(value,''auto'')::text <> pdb.empty()::text', case_row.function_name)
    END;
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

-- Capture real worker execution before TAP writes assign this connection an XID.
BEGIN READ ONLY;
SET LOCAL max_parallel_workers_per_gather = 2;
SET LOCAL min_parallel_table_scan_size = 0;
SET LOCAL parallel_setup_cost = 0;
SET LOCAL parallel_tuple_cost = 0;
SET LOCAL jit = off;
SELECT pinyin_auto_test.run_parallel_probe() AS parallel_results \gset
COMMIT;

BEGIN;
SELECT no_plan();
SELECT is(jsonb_array_length(:'parallel_results'::jsonb),4,'all Auto read-only worker cases ran');
SELECT ok((entry->>'no_xid_before')::boolean,entry->>'case' || ': starts without XID')
FROM jsonb_array_elements(:'parallel_results'::jsonb) AS entry;
SELECT ok((entry->>'gather')::boolean,entry->>'case' || ': Gather exists')
FROM jsonb_array_elements(:'parallel_results'::jsonb) AS entry;
SELECT ok((entry->>'workers')::integer > 0,entry->>'case' || ': real parallel workers launched')
FROM jsonb_array_elements(:'parallel_results'::jsonb) AS entry;
SELECT ok((entry->>'worker_filter')::boolean,entry->>'case' || ': Auto executes below Gather')
FROM jsonb_array_elements(:'parallel_results'::jsonb) AS entry;
SELECT is((entry->>'matched')::bigint,75000::bigint,entry->>'case' || ': hand-labeled count')
FROM jsonb_array_elements(:'parallel_results'::jsonb) AS entry;
SELECT ok((entry->>'no_xid_after')::boolean,entry->>'case' || ': remains without XID')
FROM jsonb_array_elements(:'parallel_results'::jsonb) AS entry;

SELECT is(p.proparallel::text,'s','Auto API stays parallel safe: ' || p.oid::regprocedure::text)
FROM pg_proc AS p WHERE p.oid IN (
  'public.pinyin_regex_phrase_patterns_mode(text,text,boolean)'::regprocedure,
  'public.pinyin_regex_phrase_mode(text,text,integer,integer,boolean)'::regprocedure,
  'public.sql_pinyin_regex_phrase_patterns_mode(text,text,boolean)'::regprocedure,
  'public.sql_pinyin_regex_phrase_mode(text,text,integer,integer,boolean)'::regprocedure
);
SELECT ok(EXISTS (
  SELECT 1 FROM pg_depend d JOIN pg_extension e ON e.oid=d.refobjid
  WHERE d.classid='pg_proc'::regclass AND d.objid=signature::regprocedure
    AND d.deptype='e' AND e.extname='pg_pinyin'
),'Rust Auto API remains extension-owned: ' || signature)
FROM (VALUES
  ('public.pinyin_regex_phrase_patterns_mode(text,text,boolean)'),
  ('public.pinyin_regex_phrase_mode(text,text,integer,integer,boolean)')
) AS signatures(signature);
SELECT ok(NOT EXISTS (
  SELECT 1 FROM pg_depend d JOIN pg_extension e ON e.oid=d.refobjid
  WHERE d.classid='pg_proc'::regclass AND d.objid=signature::regprocedure
    AND d.deptype='e' AND e.extname='pg_pinyin'
),'independent SQL Auto API is not owned by pg_pinyin: ' || signature)
FROM (VALUES
  ('public.sql_pinyin_regex_phrase_patterns_mode(text,text,boolean)'),
  ('public.sql_pinyin_regex_phrase_mode(text,text,integer,integer,boolean)')
) AS signatures(signature);

CREATE TEMP TABLE auto_pattern_gold(label text,value text,generated boolean,expected text[]);
INSERT INTO auto_pattern_gold VALUES
  ('complete unambiguous single', 'ang', false, ARRAY['ang']),
  ('ambiguous complete single keeps prefix', 'an', false, ARRAY['an.*']),
  ('unambiguous complete phrase', 'xi ang', false, ARRAY['xi','ang']),
  ('complete ambiguous last token', 'xi an', false, ARRAY['xi','an.*']),
  ('short valid last syllable is also a prefix', 'xi a', false, ARRAY['xi','a.*']),
  ('straight apostrophe boundary', 'xi''a', false, ARRAY['xi','a.*']),
  ('curly apostrophe boundary', 'xi’a', false, ARRAY['xi','a.*']),
  ('left curly boundary', 'xi‘a', false, ARRAY['xi','a.*']),
  ('trailing whitespace does not promise completion', 'xi a ', false, ARRAY['xi','a.*']),
  ('last syllable can still extend', 'wu han han', false, ARRAY['wu','han','han.*']),
  ('incomplete final consonant cluster', 'zheng sh', false, ARRAY['zheng','sh.*']),
  ('initials stay three positions', 'whh', false, ARRAY['w.*','h.*','h.*']),
  ('spaced initials stay three positions', 'w h h', false, ARRAY['w.*','h.*','h.*']),
  ('unmarked syllabic n is a low-confidence leading initial', 'nh', false, ARRAY['n.*','h.*']),
  ('spaced syllabic n remains a leading initial', 'n h', false, ARRAY['n.*','h.*']),
  ('both syllabic nasals remain initial prefixes', 'm n', false, ARRAY['m.*','n.*']),
  ('numeric tone explicitly completes leading syllabic n', 'n1 h', false, ARRAY['n','h.*']),
  ('vowel-leading initials keep both prefixes', 'a h', false, ARRAY['a.*','h.*']),
  ('two bare full vowels remain ambiguous initials', 'e o', false, ARRAY['e.*','o.*']),
  ('bare vowel and consonant remain ambiguous initials', 'o y', false, ARRAY['o.*','y.*']),
  ('three bare vowels remain ambiguous initials', 'a e o', false, ARRAY['a.*','e.*','o.*']),
  ('numeric tone explicitly completes leading vowel', 'a1 h', false, ARRAY['a','h.*']),
  ('marked tone explicitly completes leading vowel', 'ā h', false, ARRAY['a','h.*']),
  ('mixed full and initials falls back', 'wu hh', false, ARRAY['wu.*','h.*','h.*']),
  ('non-full leading prefix falls back', 'w han', false, ARRAY['w.*','han.*']),
  ('final prefix is not greedily split', 'ni yon', false, ARRAY['ni','yon.*']),
  ('compact incomplete prefix is not split', 'niyon', false, ARRAY['ni','yon.*']),
  ('tone on prior syllable keeps whole terminal prefix', 'nǐyon', false, ARRAY['ni','yon.*']),
  ('numeric tone boundary keeps whole terminal prefix', 'ni3yon', false, ARRAY['ni','yon.*']),
  ('marked complete substring retains greedy grammar', 'nǐyón', false, ARRAY['ni','yo','n.*']),
  ('marked complete last syllable is exact', 'nǐyǒng', false, ARRAY['ni','yong']),
  ('numeric tones explicitly complete', 'xi1 an1', false, ARRAY['xi','an']),
  ('marked terminal syllable explicitly complete', 'xi ān', false, ARRAY['xi','an']),
  ('tone on earlier token does not finish last', 'xī an', false, ARRAY['xi','an.*']),
  ('tone within one greedy full token', 'xīan', false, ARRAY['xian']),
  ('umlaut colon and tones', 'lu:4liang2', false, ARRAY['lv','liang']),
  ('precomposed umlaut and tones', 'LǛ LIÁNG', false, ARRAY['lv','liang']),
  ('decomposed umlaut and tones', U&'lu\0308\0300 lia\0301ng', false, ARRAY['lv','liang']),
  ('numeric repeated full syllables', 'an1 an1', false, ARRAY['an','an']),
  ('generated representation uses compat prefixes', 'xi an', true, ARRAY['.*\|xi.*','.*\|an.*']),
  ('generated normalized tone input', 'xi1 an1', true, ARRAY['.*\|xi.*','.*\|an.*']),
  ('generated normalized umlaut', 'LǛ LIÁNG', true, ARRAY['.*\|lv.*','.*\|liang.*']),
  ('empty', '', false, ARRAY[]::text[]),
  ('separators only', '''‘’ ', false, ARRAY[]::text[]),
  ('invalid punctuation', 'xi-an', false, ARRAY[]::text[]),
  ('invalid partial punctuation', 'xi''an!', false, ARRAY[]::text[]),
  ('invalid Chinese', '中文', false, ARRAY[]::text[]),
  ('invalid tone number', 'xi6 an', false, ARRAY[]::text[]),
  ('malformed multiple tone marks', 'hǎó', false, ARRAY[]::text[]),
  ('input scalar limit', repeat('a',257), false, ARRAY[]::text[]),
  ('token count limit', repeat('a ',65), false, ARRAY[]::text[]),
  ('SQL NULL', NULL, false, NULL);
SELECT is(public.pinyin_regex_phrase_patterns_mode(value,'auto',generated),expected,
          'Rust exact Auto tokens: ' || label) FROM auto_pattern_gold;
SELECT is(public.sql_pinyin_regex_phrase_patterns_mode(value,'auto',generated),expected,
          'SQL exact Auto tokens: ' || label) FROM auto_pattern_gold;

SELECT is(public.pinyin_regex_phrase_mode('ang','auto')::text,pdb.term('ang')::text,
          'confident single syllable uses native term');
SELECT is(public.pinyin_regex_phrase_mode('xi ang','auto')::text,
          pdb.phrase_array(ARRAY['xi','ang'])::text,'confident full phrase uses native phrase_array');
SELECT is(public.pinyin_regex_phrase_mode('xi1 an1','auto',2)::text,
          paradedb.slop_to_query(paradedb.query_to_slop(pdb.phrase_array(ARRAY['xi','an']),2,true))::text,
          'explicit complete tones permit native positive slop');
SELECT is(public.pinyin_regex_phrase_mode('xi a','auto')::text,
          pdb.phrase_prefix(ARRAY['xi','a'],16384)::text,
          'xi a uses phrase prefix without silent default expansion clipping');
SELECT is(public.pinyin_regex_phrase_mode('an','auto')::text,pdb.regex('an.*')::text,
          'ambiguous single syllable remains prefix regex');
SELECT is(public.pinyin_regex_phrase_mode('whh','auto')::text,
          public.pinyin_regex_phrase('whh')::text,'initials retain original positional regex');
SELECT is(public.pinyin_regex_phrase_mode('xi a','auto',1)::text,
          pdb.regex_phrase(ARRAY['xi','a.*'],1)::text,'prefix with positive slop falls back to regex');
SELECT is(public.pinyin_regex_phrase_mode('xi a','auto',NULL,7)::text,
          pdb.regex_phrase(ARRAY['xi','a.*'],0,7)::text,'explicit cap uses selected Auto regex tokens');
SELECT is(public.pinyin_regex_phrase_mode('xi1 an1','auto',NULL,7)::text,
          pdb.regex_phrase(ARRAY['xi','an'],0,7)::text,'explicit cap prevents native exact routing');
SELECT is(public.pinyin_regex_phrase_mode('xi an','auto',NULL,NULL,true)::text,
          public.pinyin_regex_phrase('xi an',NULL,NULL,true)::text,'generated storage retains compat regex');
SELECT ok(public.pinyin_regex_phrase_mode(NULL,'auto') IS NULL
          AND public.sql_pinyin_regex_phrase_mode(NULL,'auto') IS NULL,'Auto preserves SQL NULL');
SELECT is(public.pinyin_regex_phrase_mode('invalid!','auto')::text,pdb.empty()::text,
          'invalid Auto input is a vanilla empty query');
SELECT is(public.sql_pinyin_regex_phrase_mode('','auto')::text,pdb.empty()::text,
          'empty SQL Auto input is a vanilla empty query');
SELECT is(public.sql_pinyin_regex_phrase_mode(value,'auto')::text,
          public.pinyin_regex_phrase_mode(value,'auto')::text,'Auto query shape agrees across backends: ' || value)
FROM (VALUES ('ang'),('an'),('xi ang'),('xi a'),('whh'),('wu hh'),('xi1 an1'),('LǛ LIÁNG')) AS values_to_check(value);

CREATE TABLE pinyin_auto_test.gold(id bigint PRIMARY KEY,plain text NOT NULL,mixed text NOT NULL);
INSERT INTO pinyin_auto_test.gold
SELECT id,plain,(SELECT string_agg('|' || token || '|',' ' ORDER BY position)
                FROM unnest(string_to_array(plain,' ')) WITH ORDINALITY AS tokens(token,position))
FROM (VALUES
  (1,'an'), (2,'ang'), (3,'xi an'), (4,'xi ang'), (5,'xin an'),
  (6,'xian'), (7,'xi a'), (8,'xi hao an'), (9,'xi hao hao an'),
  (10,'an xi'), (11,'xi xi an'), (12,'an an'), (13,'an an an'),
  (14,'wu han han'), (15,'wu han hang'), (16,'wu hong han'), (17,'wu hen han'),
  (18,'lv liang'), (19,'lve liang'), (20,'lv lia'), (21,'zhan an'),
  (22,'zhang an'), (23,'zheng shan'), (24,'zheng shang'), (25,'shang hai'),
  (26,'shan hai'), (27,'ni yong'), (28,'ni yo n'), (29,'ni long'),
  (30,'ni lo n'), (31,'xi zzzz'), (32,'xi an an'), (33,'xi an xi an'), (34,'xi ang xi an'),
  (35,'ni hao'), (36,'man ni'), (37,'n hao'), (38,'ming nan'), (39,'m n'),
  (40,'ao hao'), (41,'a hao'), (42,'e ou'), (43,'e o'), (44,'er ou'),
  (45,'o yang'), (46,'ou yang'), (47,'a e o'), (48,'ao er ou')
) AS rows(id,plain);
CREATE INDEX pinyin_auto_gold_bm25 ON pinyin_auto_test.gold USING bm25
(id,(plain::pdb.whitespace),(mixed::pdb.whitespace)) WITH (key_field='id');

CREATE FUNCTION pinyin_auto_test.ids(value text,slope integer,backend text,generated boolean DEFAULT false)
RETURNS bigint[] LANGUAGE plpgsql AS $$
DECLARE ids bigint[];
BEGIN
  EXECUTE format('SELECT coalesce(array_agg(id ORDER BY id),ARRAY[]::bigint[]) '
                 'FROM pinyin_auto_test.gold WHERE %I @@@ public.%I($1,''auto'',$2,NULL,$3)',
                 CASE WHEN generated THEN 'mixed' ELSE 'plain' END,
                 CASE backend WHEN 'rust' THEN 'pinyin_regex_phrase_mode' ELSE 'sql_pinyin_regex_phrase_mode' END)
  INTO ids USING value,slope,generated;
  RETURN ids;
END;
$$;
CREATE TEMP TABLE auto_retrieval_gold(label text,value text,slope integer,expected bigint[]);
INSERT INTO auto_retrieval_gold VALUES
  ('complete terminal ang is exact','xi ang',NULL,ARRAY[4,34]),
  ('ambiguous terminal an permits ang','xi an',NULL,ARRAY[3,4,11,32,33,34]),
  ('xi a is prefix only at last position','xi a',NULL,ARRAY[3,4,7,11,32,33,34]),
  ('apostrophe final completion','xi''a',0,ARRAY[3,4,7,11,32,33,34]),
  ('curly apostrophe final completion','xi’a',0,ARRAY[3,4,7,11,32,33,34]),
  ('numeric tones explicitly finish an','xi1 an1',NULL,ARRAY[3,11,32,33,34]),
  ('marked terminal tone finishes an','xi ān',NULL,ARRAY[3,11,32,33,34]),
  ('initials remain three ordered positions','whh',NULL,ARRAY[14,15,16,17]),
  ('mixed initials remain compatible','wu hh',NULL,ARRAY[14,15,16,17]),
  ('unmarked leading n keeps ni hao initial recall','nh',NULL,ARRAY[35,37]),
  ('spaced leading n keeps ni hao initial recall','n h',NULL,ARRAY[35,37]),
  ('unmarked m n keeps initial recall','m n',NULL,ARRAY[36,38,39]),
  ('marked leading n is exact rather than initial','n1 h',NULL,ARRAY[37]),
  ('vowel-leading initials keep ao hao recall','a h',NULL,ARRAY[40,41]),
  ('complete single vowels can still be initials','e o',NULL,ARRAY[42,43,44,47,48]),
  ('vowel and consonant initials keep ou yang recall','o y',NULL,ARRAY[45,46]),
  ('all-single-letter initials keep every position prefixed','a e o',NULL,ARRAY[47,48]),
  ('numeric vowel resolves leading token as complete','a1 h',NULL,ARRAY[41]),
  ('marked vowel resolves leading token as complete','ā h',NULL,ARRAY[41]),
  ('complete leading syllables narrow terminal completion','wu han ha',NULL,ARRAY[14,15]),
  ('final full syllable remains extendible','wu han han',NULL,ARRAY[14,15]),
  ('complete tokens plus positive slop','xi1 an1',1,ARRAY[3,8,11,32,33,34]),
  ('prefix plus positive slop','xi a',1,ARRAY[3,4,7,8,11,32,33,34]),
  ('repeated complete tokens need adjacent occurrences','an1 an1',0,ARRAY[12,13,32]),
  ('umlaut normalization preserves exact leading lv','LǛ LIÁNG',NULL,ARRAY[18]),
  ('incomplete whole terminal prefix is not split','ni yon',NULL,ARRAY[27]),
  ('invalid input','invalid!',NULL,ARRAY[]::bigint[]),
  ('NULL input',NULL,NULL,ARRAY[]::bigint[]);
SELECT is(pinyin_auto_test.ids(value,slope,backend),expected,
          backend || ' hand-labeled Auto hit set: ' || label)
FROM auto_retrieval_gold CROSS JOIN (VALUES ('rust'),('sql')) AS backends(backend);
SELECT results_eq(
  $$SELECT id FROM pinyin_auto_test.gold WHERE plain @@@ public.pinyin_regex_phrase('xi a') ORDER BY id$$,
  $$VALUES (3::bigint),(4::bigint),(5::bigint),(7::bigint),(11::bigint),(32::bigint),(33::bigint),(34::bigint)$$,
  'old default keeps both-position prefixes including xin an');
SELECT results_eq(
  $$SELECT id FROM pinyin_auto_test.gold WHERE plain @@@ public.pinyin_phrase('xi a') ORDER BY id$$,
  $$VALUES (7::bigint)$$,'explicit phrase still means exact xi a, not completion');
SELECT is(pinyin_auto_test.ids('xi an',NULL,'rust',true),
          ARRAY[3,4,5,11,32,33,34]::bigint[],'generated Auto fallback retains all-position compat prefixes');
SELECT is(pinyin_auto_test.ids('xi an',NULL,'sql',true),
          pinyin_auto_test.ids('xi an',NULL,'rust',true),'generated fallback hit parity');
SELECT results_eq(
  $$SELECT id FROM pinyin_auto_test.gold WHERE id=6 OR plain @@@ public.pinyin_regex_phrase_mode('invalid!','auto') ORDER BY id$$,
  $$VALUES (6::bigint)$$,'empty Auto query does not affect other OR predicates');
SELECT results_eq(
  $$SELECT id FROM pinyin_auto_test.gold WHERE id=6 AND plain @@@ public.sql_pinyin_regex_phrase_mode('invalid!','auto') ORDER BY id$$,
  $$SELECT 1::bigint WHERE false$$,'empty SQL Auto query is a false AND predicate');

CREATE TABLE pinyin_auto_test.expansions(id bigint PRIMARY KEY,plain text NOT NULL);
INSERT INTO pinyin_auto_test.expansions
SELECT n,'xi a' || lpad(n::text,3,'0') FROM generate_series(1,60) AS n;
CREATE INDEX pinyin_auto_expansions_bm25 ON pinyin_auto_test.expansions USING bm25
(id,(plain::pdb.whitespace)) WITH (key_field='id');
SELECT is((SELECT count(*) FROM pinyin_auto_test.expansions
           WHERE plain @@@ pdb.phrase_prefix(ARRAY['xi','a'])),50::bigint,
          'upstream default phrase_prefix silently caps matching terminal terms at fifty');
SELECT is((SELECT count(*) FROM pinyin_auto_test.expansions
           WHERE plain @@@ public.pinyin_regex_phrase_mode('xi a','auto')),60::bigint,
          'Rust Auto includes all sixty terminal expansions');
SELECT is((SELECT count(*) FROM pinyin_auto_test.expansions
           WHERE plain @@@ public.sql_pinyin_regex_phrase_mode('xi a','auto')),60::bigint,
          'SQL Auto includes all sixty terminal expansions');

-- Native availability is checked at runtime; older pg_search remains usable.
SAVEPOINT missing_prefix_api;
ALTER FUNCTION pdb.phrase_prefix(text[],integer) RENAME TO auto_test_hidden_phrase_prefix;
SELECT public.pinyin_regex_phrase_mode('xi a','auto')::text = pdb.regex_phrase(ARRAY['xi','a.*'])::text AS rust_prefix_fallback,
       public.sql_pinyin_regex_phrase_mode('xi a','auto')::text = pdb.regex_phrase(ARRAY['xi','a.*'])::text AS sql_prefix_fallback
\gset
ROLLBACK TO missing_prefix_api;
SELECT ok(:'rust_prefix_fallback'::boolean,'Rust Auto falls back when native prefix API is missing');
SELECT ok(:'sql_prefix_fallback'::boolean,'SQL Auto falls back when native prefix API is missing');

CREATE TABLE pinyin_auto_test.custom_analyzer(id bigint PRIMARY KEY,plain text NOT NULL);
INSERT INTO pinyin_auto_test.custom_analyzer VALUES (1,'xian an');
CREATE INDEX pinyin_auto_custom_bm25 ON pinyin_auto_test.custom_analyzer USING bm25
(id,(plain::pdb.regex_pattern('xi|an'))) WITH (key_field='id');
SELECT results_eq(
  $$SELECT id FROM pinyin_auto_test.custom_analyzer WHERE plain @@@ public.pinyin_regex_phrase_mode('xian a','auto')$$,
  $$SELECT 1::bigint WHERE false$$,'Rust Auto prefix does not re-analyze exact preceding xian');
SELECT results_eq(
  $$SELECT id FROM pinyin_auto_test.custom_analyzer WHERE plain @@@ public.sql_pinyin_regex_phrase_mode('xian a','auto')$$,
  $$SELECT 1::bigint WHERE false$$,'SQL Auto prefix does not re-analyze exact preceding xian');

SAVEPOINT live_sql_inventory;
INSERT INTO pinyin.pinyin_token(character,category) VALUES ('zz',1)
ON CONFLICT (character) DO UPDATE SET category=EXCLUDED.category;
SELECT public.pinyin_regex_phrase_patterns_mode('zz','auto') = ARRAY['z.*','z.*'] AS rust_static_inventory,
       public.sql_pinyin_regex_phrase_patterns_mode('zz','auto') = ARRAY['zz'] AS sql_live_inventory
\gset
ROLLBACK TO live_sql_inventory;
SELECT ok(:'rust_static_inventory'::boolean,'Rust Auto dictionary remains embedded and SPI-free');
SELECT ok(:'sql_live_inventory'::boolean,'independent SQL Auto keeps its live token inventory');

SELECT * FROM finish();
ROLLBACK;
DROP SCHEMA pinyin_auto_test CASCADE;
\else
SELECT plan(1);
SELECT skip('Auto query checks require preloaded pg_search',1);
SELECT * FROM finish();
\endif
