BEGIN;

CREATE EXTENSION IF NOT EXISTS pgtap;
SELECT CASE WHEN EXISTS (
  SELECT 1 FROM pg_available_extensions WHERE name = 'pg_search'
) AND position('pg_search' in current_setting('shared_preload_libraries', true)) > 0
THEN 1 ELSE 0 END AS has_pg_search
\gset
\if :has_pg_search
DROP EXTENSION IF EXISTS pg_pinyin CASCADE;
CREATE EXTENSION IF NOT EXISTS pg_search CASCADE;
CREATE EXTENSION pg_pinyin;
\ir ../../sql/pinyin.sql
\ir ../../sql/word.sql
SELECT no_plan();

CREATE TEMP TABLE mode_pattern_gold (
  label text PRIMARY KEY, value text, mode text, generated boolean, patterns text[]
);
INSERT INTO mode_pattern_gold VALUES
  ('compat prefix', 'An', 'compat', false, ARRAY['an.*']),
  ('compat initials', 'whh', 'compat', false, ARRAY['w.*', 'h.*', 'h.*']),
  ('compat pipe', 'an', 'compat', true, ARRAY['.*\|an.*']),
  ('compat invalid apostrophe', 'xi''an', 'compat', false, ARRAY[]::text[]),
  ('full an', 'an', 'full', false, ARRAY['an']),
  ('full ang', 'ang', 'full', false, ARRAY['ang']),
  ('full joined xian', 'xian', 'full', false, ARRAY['xian']),
  ('full spaced xi an', 'xi an', 'full', false, ARRAY['xi', 'an']),
  ('full straight apostrophe', 'xi''an', 'full', false, ARRAY['xi', 'an']),
  ('full curly apostrophe', 'xi’an', 'full', false, ARRAY['xi', 'an']),
  ('full umlaut tone', 'LǙ LIÁNG', 'full', false, ARRAY['lv', 'liang']),
  ('full decomposed umlaut', U&'lu\0308\0300 liang', 'full', false, ARRAY['lv', 'liang']),
  ('full numeric tones', 'nu:3 er2', 'full', false, ARRAY['nv', 'er']),
  ('full neutral zero', 'ma0', 'full', false, ARRAY['ma']),
  ('full neutral five', 'ma5', 'full', false, ARRAY['ma']),
  ('full syllabic nasals', 'm n', 'full', false, ARRAY['m', 'n']),
  ('full rejects incomplete', 'whh', 'full', false, ARRAY[]::text[]),
  ('full rejects punctuation', 'xi-an', 'full', false, ARRAY[]::text[]),
  ('full rejects tone six', 'ma6', 'full', false, ARRAY[]::text[]),
  ('full mixed exact', 'an', 'full', true, ARRAY['(an|.*\|an\|.*)']),
  ('initials compact', 'whh', 'initials', false, ARRAY['w.*', 'h.*', 'h.*']),
  ('initials compact zhh', 'zhh', 'initials', false, ARRAY['z.*', 'h.*', 'h.*']),
  ('initials explicit digraph', 'zh', 'initials', false, ARRAY['zh.*']),
  ('initials spaced digraphs', 'zh ch sh', 'initials', false, ARRAY['zh.*', 'ch.*', 'sh.*']),
  ('initials apostrophe', 'zh''h', 'initials', false, ARRAY['zh.*', 'h.*']),
  ('initials normalized v has no syllable prefix', 'ü', 'initials', false, ARRAY[]::text[]),
  ('initials mixed prefixes', 'whh', 'initials', true,
    ARRAY['(w.*|.*\|w[^|]*\|.*)', '(h.*|.*\|h[^|]*\|.*)', '(h.*|.*\|h[^|]*\|.*)']),
  ('autocomplete final prefix', 'zhan a', 'autocomplete', false, ARRAY['zhan', 'a.*']),
  ('autocomplete incomplete last', 'zheng sh', 'autocomplete', false, ARRAY['zheng', 'sh.*']),
  ('autocomplete invalid earlier', 'w han', 'autocomplete', false, ARRAY[]::text[]),
  ('autocomplete mixed', 'an g', 'autocomplete', true,
    ARRAY['(an|.*\|an\|.*)', '(g.*|.*\|g[^|]*\|.*)']),
  ('empty input', '', 'full', false, ARRAY[]::text[]),
  ('whitespace input', E' \t ', 'initials', false, ARRAY[]::text[]),
  ('SQL null', NULL, 'full', false, NULL),
  ('compat malformed multiple tones', 'hǎó', 'compat', false, ARRAY[]::text[]),
  ('full malformed multiple tones', 'hǎó', 'full', false, ARRAY[]::text[]),
  ('initials malformed multiple tones', 'hǎó', 'initials', false, ARRAY[]::text[]),
  ('autocomplete malformed multiple tones', 'hǎó', 'autocomplete', false, ARRAY[]::text[]),
  ('full malformed different marked vowels', 'āí', 'full', false, ARRAY[]::text[]),
  ('full joined individually toned syllables', 'nǐhǎo', 'full', false, ARRAY['ni', 'hao']),
  ('autocomplete yon unfinished whole', 'yon', 'autocomplete', false, ARRAY['yon.*']),
  ('autocomplete lon unfinished whole', 'lon', 'autocomplete', false, ARRAY['lon.*']),
  ('autocomplete spaced ni yon', 'ni yon', 'autocomplete', false, ARRAY['ni', 'yon.*']),
  ('autocomplete joined niyon', 'niyon', 'autocomplete', false, ARRAY['ni', 'yon.*']),
  ('autocomplete spaced ni lon', 'ni lon', 'autocomplete', false, ARRAY['ni', 'lon.*']),
  ('autocomplete joined nilon', 'nilon', 'autocomplete', false, ARRAY['ni', 'lon.*']),
  ('autocomplete explicit ni yo n', 'ni yo n', 'autocomplete', false, ARRAY['ni', 'yo', 'n.*']),
  ('autocomplete explicit ni lo n', 'ni lo n', 'autocomplete', false, ARRAY['ni', 'lo', 'n.*']),
  ('autocomplete pipe yon unfinished whole', 'yon', 'autocomplete', true,
    ARRAY['(yon.*|.*\|yon[^|]*\|.*)']),
  ('autocomplete pipe ni yon positions', 'ni yon', 'autocomplete', true,
    ARRAY['(ni|.*\|ni\|.*)', '(yon.*|.*\|yon[^|]*\|.*)']);

SELECT is(public.pinyin_regex_phrase_patterns_mode(value, mode, generated), patterns,
          'Rust hand-curated patterns: ' || label)
FROM mode_pattern_gold ORDER BY label;
SELECT is(public.sql_pinyin_regex_phrase_patterns_mode(value, mode, generated), patterns,
          'SQL hand-curated patterns: ' || label)
FROM mode_pattern_gold ORDER BY label;
SELECT is(public.sql_pinyin_regex_phrase_patterns_mode(value, mode, generated),
          public.pinyin_regex_phrase_patterns_mode(value, mode, generated),
          'separate backend parity: ' || label)
FROM mode_pattern_gold ORDER BY label;

SELECT is(public.pinyin_regex_phrase_patterns_mode(value, 'compat', generated),
          public.pinyin_regex_phrase_patterns(value, generated), 'Rust compatibility contract: ' || label)
FROM mode_pattern_gold ORDER BY label;
SELECT is(public.sql_pinyin_regex_phrase_patterns_mode(value, 'compat', generated),
          public.sql_pinyin_regex_phrase_patterns(value, generated), 'SQL compatibility contract: ' || label)
FROM mode_pattern_gold ORDER BY label;

SELECT lives_ok($$SELECT public.pinyin_regex_phrase_patterns('an', NULL)$$,
                'legacy Rust untyped NULL remains unambiguous');
SELECT lives_ok($$SELECT public.sql_pinyin_regex_phrase_patterns('an', NULL)$$,
                'legacy SQL untyped NULL remains unambiguous');
SELECT lives_ok($$SELECT public.pinyin_regex_phrase('an', NULL)$$,
                'legacy Rust query untyped NULL remains unambiguous');
SELECT lives_ok($$SELECT public.sql_pinyin_regex_phrase('an', NULL)$$,
                'legacy SQL query untyped NULL remains unambiguous');
SELECT throws_ok($$SELECT public.pinyin_regex_phrase_patterns_mode('an', 'unknown')$$,
                 '22023', NULL, 'unknown Rust mode is a parameter error');
SELECT throws_ok($$SELECT public.sql_pinyin_regex_phrase_patterns_mode('an', 'unknown')$$,
                 '22023', NULL, 'unknown SQL mode is a parameter error');
SELECT throws_ok($$SELECT public.pinyin_regex_phrase_mode('an', 'unknown')$$,
                 '22023', NULL, 'unknown Rust query mode is a parameter error');
SELECT throws_ok($$SELECT public.sql_pinyin_regex_phrase_mode('an', 'unknown')$$,
                 '22023', NULL, 'unknown SQL query mode is a parameter error');
SELECT is(public.pinyin_regex_phrase_patterns_mode(repeat('a', 257), 'initials'), ARRAY[]::text[],
          'Rust limits input to 256 Unicode characters');
SELECT is(public.sql_pinyin_regex_phrase_patterns_mode(repeat('a', 257), 'initials'), ARRAY[]::text[],
          'SQL limits input to 256 Unicode characters');
SELECT is(public.pinyin_regex_phrase_patterns_mode(repeat('a ', 65), 'full'), ARRAY[]::text[],
          'Rust rejects more than 64 tokens');
SELECT is(public.sql_pinyin_regex_phrase_patterns_mode(repeat('a ', 65), 'full'), ARRAY[]::text[],
          'SQL rejects more than 64 tokens');
SELECT is(cardinality(public.pinyin_regex_phrase_patterns_mode(repeat('a ', 64), 'full')), 64,
          'Rust accepts the 64-token boundary');
SELECT is(cardinality(public.sql_pinyin_regex_phrase_patterns_mode(repeat('a ', 64), 'full')), 64,
          'SQL accepts the 64-token boundary');

SELECT is(p.proparallel::text, 's', 'new Rust API is parallel safe: ' || p.oid::regprocedure::text)
FROM pg_proc AS p WHERE p.oid IN (
  'public.pinyin_regex_phrase_patterns_mode(text,text,boolean)'::regprocedure,
  'public.pinyin_regex_phrase_mode(text,text,integer,integer,boolean)'::regprocedure
);
SELECT ok(EXISTS (
  SELECT 1 FROM pg_depend AS d JOIN pg_extension AS e ON e.oid = d.refobjid
  WHERE d.classid = 'pg_proc'::regclass AND d.objid = signature::regprocedure
    AND d.deptype = 'e' AND e.extname = 'pg_pinyin'
), 'Rust API is extension-owned, not word.sql: ' || signature)
FROM (VALUES
  ('public.pinyin_regex_phrase_patterns_mode(text,text,boolean)'),
  ('public.pinyin_regex_phrase_mode(text,text,integer,integer,boolean)')
) AS functions(signature);

CREATE TABLE public.pinyin_mode_gold (id bigint PRIMARY KEY, name text, plain text, mixed text);
INSERT INTO public.pinyin_mode_gold VALUES
  (101, '安', 'an', '|an|'), (102, '昂', 'ang', '|ang|'),
  (103, '绿', 'lv', '|lv|'), (104, '略', 'lve', '|lve|'),
  (105, '西安', 'xi an', '|xi| |an|'), (106, '仙', 'xian', '|xian|'),
  (107, '武汉汉', 'wu han han', '|wu| |han| |han|'),
  (108, '武汉行', 'wu han hang', '|wu| |han| |hang|'),
  (109, '上海', 'shang hai', '|shang| |hai|'),
  (110, '山海', 'shan hai', '|shan| |hai|'),
  (111, '战安', 'zhan an', '|zhan| |an|'),
  (112, '张安', 'zhang an', '|zhang| |an|'),
  (113, '银行', 'yin hang', '|yin| |xing|hang|'),
  (114, '行行', 'xing hang', '|xing| |hang|'),
  (115, '吕梁', 'lv liang', '|lv| |liang|'),
  (116, '略良', 'lve liang', '|lve| |liang|'),
  (117, '安国', 'an guo', '|an| |guo|'),
  (118, '昂国', 'ang guo', '|ang| |guo|'),
  (119, '吴洪汉', 'wu hong han', '|wu| |hong| |han|'),
  (120, '吴很汉', 'wu hen han', '|wu| |hen| |han|'),
  (121, '重庆', 'chong qing', 'chong |qing|'),
  (122, '钟庆', 'zhong qing', '|zhong| qing'),
  (123, '勇', 'yong', '|yong|'), (124, '龙', 'long', '|long|'),
  (125, '分裂对照 yo n', 'yo n', '|yo| |n|'),
  (126, '分裂对照 lo n', 'lo n', '|lo| |n|'),
  (127, '倪勇', 'ni yong', 'ni |yong|'),
  (128, '尼龙', 'ni long', '|ni| long'),
  (129, '分裂对照 ni yo n', 'ni yo n', '|ni| |yo| |n|'),
  (130, '分裂对照 ni lo n', 'ni lo n', '|ni| |lo| |n|');

-- The pinned pg_search 0.26.1 whitespace tokenizer preserves pipe alternatives.
-- https://github.com/paradedb/paradedb/blob/529c77e9dffe4bd86b6b7971c019bb26b8eadbfa/docs/reference/tokenizers/available-tokenizers/whitespace.mdx
CREATE INDEX pinyin_mode_gold_bm25 ON public.pinyin_mode_gold USING bm25
(id, (plain::pdb.whitespace), (mixed::pdb.whitespace)) WITH (key_field = 'id');
SELECT is('|xing|hang|'::pdb.whitespace::text[], ARRAY['|xing|hang|'],
          'index tokenizer retains alternatives in one positional token');

CREATE TEMP TABLE mode_retrieval_gold (label text, query text, mode text, positive bigint[], negative bigint[]);
INSERT INTO mode_retrieval_gold VALUES
  ('legacy prefix contrast', 'an', 'compat', ARRAY[101,102,105,111,112,117,118], ARRAY[103,104,106]),
  ('an not ang', 'an', 'full', ARRAY[101,105,111,112,117], ARRAY[102,106,118]),
  ('lv not lve', 'lü4', 'full', ARRAY[103,115], ARRAY[104,116]),
  ('one syllable xian', 'xian', 'full', ARRAY[106], ARRAY[105]),
  ('explicit xi an', 'xi''an', 'full', ARRAY[105], ARRAY[106]),
  ('earlier zhan not zhang', 'zhan an', 'full', ARRAY[111], ARRAY[112]),
  ('autocomplete earlier exact', 'zhan a', 'autocomplete', ARRAY[111], ARRAY[112]),
  ('autocomplete earlier lv exact', 'lv l', 'autocomplete', ARRAY[115], ARRAY[116]),
  ('autocomplete earlier an exact', 'an g', 'autocomplete', ARRAY[117], ARRAY[118]),
  ('compact whh initials', 'whh', 'initials', ARRAY[107,108,119,120], ARRAY[109,110]),
  ('full final han not hang', 'wu han han', 'full', ARRAY[107], ARRAY[108,119,120]),
  ('autocomplete final han prefix', 'wu han ha', 'autocomplete', ARRAY[107,108], ARRAY[119,120]),
  ('pipe alternate reading', 'yin hang', 'full', ARRAY[113], ARRAY[114]),
  ('mixed plain first token', 'chong qing', 'full', ARRAY[121], ARRAY[122]),
  ('mixed plain last token', 'zhong qing', 'full', ARRAY[122], ARRAY[121]),
  ('yon not split yo n', 'yon', 'autocomplete', ARRAY[123,127], ARRAY[125,129]),
  ('lon not split lo n', 'lon', 'autocomplete', ARRAY[124,128], ARRAY[126,130]),
  ('ni yon not split ni yo n', 'ni yon', 'autocomplete', ARRAY[127], ARRAY[129,128]),
  ('niyon not split ni yo n', 'niyon', 'autocomplete', ARRAY[127], ARRAY[129,128]),
  ('ni lon not split ni lo n', 'ni lon', 'autocomplete', ARRAY[128], ARRAY[130,127]),
  ('nilon not split ni lo n', 'nilon', 'autocomplete', ARRAY[128], ARRAY[130,127]),
  ('explicit ni yo n stays split', 'ni yo n', 'autocomplete', ARRAY[129], ARRAY[127]),
  ('explicit ni lo n stays split', 'ni lo n', 'autocomplete', ARRAY[130], ARRAY[128]);

CREATE FUNCTION public.pinyin_mode_ranked_ids(value text, mode text, backend text, storage text)
RETURNS bigint[] LANGUAGE plpgsql AS $$
DECLARE ids bigint[];
BEGIN
  EXECUTE format(
    'SELECT coalesce(array_agg(id ORDER BY score DESC, id), ARRAY[]::bigint[]) FROM '
    '(SELECT id, pdb.score(id) AS score FROM public.pinyin_mode_gold WHERE %I @@@ public.%I($1,$2,NULL,NULL,$3) '
    'ORDER BY pdb.score(id) DESC,id LIMIT 100) AS ranked',
    storage, CASE backend WHEN 'rust' THEN 'pinyin_regex_phrase_mode' ELSE 'sql_pinyin_regex_phrase_mode' END
  ) INTO ids USING value, mode, storage = 'mixed';
  RETURN ids;
END;
$$;
SELECT ok(g.positive <@ public.pinyin_mode_ranked_ids(g.query,g.mode,b.backend,s.storage),
          format('%s/%s retrieves independently labeled positives: %s', b.backend,s.storage,g.label))
FROM mode_retrieval_gold AS g CROSS JOIN (VALUES ('rust'),('sql')) AS b(backend)
CROSS JOIN (VALUES ('plain'),('mixed')) AS s(storage);
SELECT ok(NOT g.negative && public.pinyin_mode_ranked_ids(g.query,g.mode,b.backend,s.storage),
          format('%s/%s excludes independently labeled negatives: %s', b.backend,s.storage,g.label))
FROM mode_retrieval_gold AS g CROSS JOIN (VALUES ('rust'),('sql')) AS b(backend)
CROSS JOIN (VALUES ('plain'),('mixed')) AS s(storage);
SELECT is(public.pinyin_mode_ranked_ids(g.query,g.mode,'rust',s.storage),
          public.pinyin_mode_ranked_ids(g.query,g.mode,'sql',s.storage),
          format('ranked backend parity %s: %s',s.storage,g.label))
FROM mode_retrieval_gold AS g CROSS JOIN (VALUES ('plain'),('mixed')) AS s(storage);
SELECT results_eq(
  $$SELECT id FROM public.pinyin_mode_gold WHERE plain @@@ public.pinyin_regex_phrase_mode('invalid!', 'full') OR id = 105$$,
  $$VALUES (105::bigint)$$, 'Rust empty mode query does not affect an OR branch');
SELECT results_eq(
  $$SELECT id FROM public.pinyin_mode_gold WHERE mixed @@@ public.sql_pinyin_regex_phrase_mode('invalid!', 'full') OR id = 105$$,
  $$VALUES (105::bigint)$$, 'SQL empty mode query does not affect an OR branch');

CREATE TABLE public.pinyin_mode_parallel_probe AS
SELECT i AS id, CASE i % 3 WHEN 0 THEN 'an' WHEN 1 THEN 'lv liang' ELSE 'xi an' END AS value
FROM generate_series(1,20000) AS i;
ALTER TABLE public.pinyin_mode_parallel_probe SET (parallel_workers = 2);
ANALYZE public.pinyin_mode_parallel_probe;
SET LOCAL max_parallel_workers_per_gather = 2;
SET LOCAL min_parallel_table_scan_size = 0;
SET LOCAL parallel_setup_cost = 0;
SET LOCAL parallel_tuple_cost = 0;
CREATE FUNCTION public.pinyin_mode_parallel_plan() RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE result jsonb;
BEGIN
  EXECUTE 'EXPLAIN (ANALYZE, FORMAT JSON) SELECT count(*) FROM public.pinyin_mode_parallel_probe '
          'WHERE cardinality(public.pinyin_regex_phrase_patterns_mode(value, ''full'')) > 0 '
          'AND public.pinyin_regex_phrase_mode(value, ''full'') IS NOT NULL' INTO result;
  RETURN result;
END;
$$;
CREATE TEMP TABLE mode_parallel_result AS SELECT public.pinyin_mode_parallel_plan() AS plan;
SELECT matches((SELECT plan::text FROM mode_parallel_result), 'Gather',
               'new mode patterns and public query helper get a forced Gather plan');
SELECT ok((SELECT jsonb_path_exists(plan, '$.**."Workers Launched" ? (@ > 0)') FROM mode_parallel_result),
          'new mode patterns and public query helper execute in actual parallel workers');
SELECT lives_ok($$SELECT count(*) FROM public.pinyin_mode_parallel_probe
                  WHERE cardinality(public.pinyin_regex_phrase_patterns_mode(value, 'full')) > 0
                    AND public.pinyin_regex_phrase_mode(value, 'full') IS NOT NULL$$,
                'new Rust patterns and query helper do not crash in the parallel scan');
SELECT is((SELECT count(*) FROM public.pinyin_mode_parallel_probe
           WHERE cardinality(public.pinyin_regex_phrase_patterns_mode(value, 'full')) > 0
             AND public.pinyin_regex_phrase_mode(value, 'full') IS NOT NULL),
          20000::bigint, 'parallel mode scan preserves all expected rows');

SELECT * FROM finish();
\else
SELECT plan(1);
SELECT skip('query mode BM25 checks require preloaded pg_search', 1);
SELECT * FROM finish();
\endif
ROLLBACK;
