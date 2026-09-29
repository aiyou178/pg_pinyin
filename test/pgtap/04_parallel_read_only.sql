CREATE EXTENSION IF NOT EXISTS pgtap;
CREATE EXTENSION IF NOT EXISTS pg_pinyin;

DROP SCHEMA IF EXISTS pinyin_parallel_read_only_test CASCADE;
DROP TABLE IF EXISTS pinyin.pinyin_mapping_pinyin_parallel_read_only_test;
DROP TABLE IF EXISTS pinyin.pinyin_words_pinyin_parallel_read_only_test;

CREATE SCHEMA pinyin_parallel_read_only_test;
CREATE TABLE pinyin_parallel_read_only_test.probe AS
SELECT '郑爽'::text AS name FROM generate_series(1, 2000);
ALTER TABLE pinyin_parallel_read_only_test.probe SET (parallel_workers = 2);
ANALYZE pinyin_parallel_read_only_test.probe;

CREATE TABLE pinyin.pinyin_mapping_pinyin_parallel_read_only_test (
  character text PRIMARY KEY,
  pinyin text
);
INSERT INTO pinyin.pinyin_mapping_pinyin_parallel_read_only_test
VALUES ('郑', '|zhengx|');

CREATE TABLE pinyin.pinyin_words_pinyin_parallel_read_only_test (
  word text PRIMARY KEY,
  pinyin text
);
INSERT INTO pinyin.pinyin_words_pinyin_parallel_read_only_test
VALUES ('郑爽', '|zhengx| |shuangx|');

CREATE FUNCTION pinyin_parallel_read_only_test.run_probe()
RETURNS jsonb
LANGUAGE plpgsql
AS $$
DECLARE
  case_row record;
  query text;
  explain_text text;
  workers integer;
  matches bigint;
  no_xid_before boolean;
  no_xid_after boolean;
  results jsonb := '[]'::jsonb;
BEGIN
  FOR case_row IN
    SELECT * FROM (VALUES
      ('char text', 'pinyin_char_romanize(name || ''ABC'')', 'zheng shuang abc'),
      ('word text', 'pinyin_word_romanize(name || ''ABC'')', 'zheng shuang abc'),
      ('word tokens', 'pinyin_word_romanize(ARRAY[name, ''ABC''])', 'zheng shuang abc'),
      ('char suffix', 'pinyin_char_romanize(name || ''ABC'', ''_pinyin_parallel_read_only_test'')', 'zhengx shuang abc'),
      ('word suffix', 'pinyin_word_romanize(name || ''ABC'', ''_pinyin_parallel_read_only_test'')', 'zhengx shuangx abc'),
      ('word tokens suffix', 'pinyin_word_romanize(ARRAY[name, ''ABC''], ''_pinyin_parallel_read_only_test'')', 'zhengx shuangx abc'),
      ('missing suffix', 'pinyin_word_romanize(name || ''ABC'', ''_missing_parallel_suffix'')', 'zheng shuang abc')
    ) AS cases(label, expression, expected)
  LOOP
    query := format(
      'SELECT count(*) FROM pinyin_parallel_read_only_test.probe WHERE public.%s = %L',
      case_row.expression,
      case_row.expected
    );
    no_xid_before := pg_current_xact_id_if_assigned() IS NULL;
    EXECUTE 'EXPLAIN (ANALYZE, FORMAT JSON) ' || query INTO explain_text;

    WITH RECURSIVE plan_nodes(node) AS (
      SELECT explain_text::jsonb->0->'Plan'
      UNION ALL
      SELECT child.value
      FROM plan_nodes
      CROSS JOIN LATERAL jsonb_array_elements(
        COALESCE(plan_nodes.node->'Plans', '[]'::jsonb)
      ) AS child(value)
    )
    SELECT COALESCE(sum((node->>'Workers Launched')::integer), 0)
    INTO workers
    FROM plan_nodes;

    EXECUTE query INTO matches;
    no_xid_after := pg_current_xact_id_if_assigned() IS NULL;
    results := results || jsonb_build_array(jsonb_build_object(
      'case', case_row.label,
      'no_xid_before', no_xid_before,
      'workers_launched', workers,
      'matches', matches,
      'no_xid_after', no_xid_after
    ));
  END LOOP;
  RETURN results;
END;
$$;

BEGIN READ ONLY;
SET LOCAL max_parallel_workers_per_gather = 2;
SET LOCAL min_parallel_table_scan_size = 0;
SET LOCAL parallel_setup_cost = 0;
SET LOCAL parallel_tuple_cost = 0;
SELECT pinyin_parallel_read_only_test.run_probe() AS results \gset
COMMIT;

SELECT plan(29);
SELECT is(jsonb_array_length(:'results'::jsonb), 7, 'all parallel cases ran');
SELECT ok(
  (entry->>'no_xid_before')::boolean,
  format('%s starts without an assigned XID', entry->>'case')
) FROM jsonb_array_elements(:'results'::jsonb) AS entry;
SELECT ok(
  (entry->>'workers_launched')::integer > 0,
  format('%s launches parallel workers', entry->>'case')
) FROM jsonb_array_elements(:'results'::jsonb) AS entry;
SELECT is(
  (entry->>'matches')::bigint,
  2000::bigint,
  format('%s returns every matching row', entry->>'case')
) FROM jsonb_array_elements(:'results'::jsonb) AS entry;
SELECT ok(
  (entry->>'no_xid_after')::boolean,
  format('%s ends without an assigned XID', entry->>'case')
) FROM jsonb_array_elements(:'results'::jsonb) AS entry;
SELECT * FROM finish();

DROP SCHEMA pinyin_parallel_read_only_test CASCADE;
DROP TABLE pinyin.pinyin_mapping_pinyin_parallel_read_only_test;
DROP TABLE pinyin.pinyin_words_pinyin_parallel_read_only_test;
