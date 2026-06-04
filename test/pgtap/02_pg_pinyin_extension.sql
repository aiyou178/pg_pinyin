BEGIN;

CREATE EXTENSION IF NOT EXISTS pgtap;
CREATE EXTENSION IF NOT EXISTS pg_pinyin;

TRUNCATE TABLE pinyin.pinyin_mapping;
TRUNCATE TABLE pinyin.pinyin_words;
TRUNCATE TABLE pinyin.pinyin_model_meta, pinyin.pinyin_model_registry;

INSERT INTO pinyin.pinyin_mapping (character, pinyin) VALUES
  (' ', ' '),
  ('郑', '|zheng|'),
  ('爽', '|shuang|');

INSERT INTO pinyin.pinyin_words (word, pinyin)
VALUES ('郑爽', '|zheng| |shuang|')
ON CONFLICT (word) DO UPDATE SET pinyin = EXCLUDED.pinyin;

CREATE TABLE IF NOT EXISTS pinyin.pinyin_mapping_suffix1 (
  character text PRIMARY KEY,
  pinyin text NOT NULL
);

CREATE TABLE IF NOT EXISTS pinyin.pinyin_words_suffix1 (
  word text PRIMARY KEY,
  pinyin text NOT NULL
);

TRUNCATE TABLE pinyin.pinyin_mapping_suffix1;
TRUNCATE TABLE pinyin.pinyin_words_suffix1;

INSERT INTO pinyin.pinyin_mapping_suffix1 (character, pinyin) VALUES
  ('郑', '|zhengx|');

INSERT INTO pinyin.pinyin_words_suffix1 (word, pinyin) VALUES
  ('郑爽', '|zhengx| |shuangx|');

INSERT INTO pinyin.pinyin_model_registry (
  model_name,
  kind,
  model_path,
  tokenizer_path,
  labels_path,
  config,
  enabled
) VALUES (
  'mock_pgtap',
  'g2pw_onnx',
  '/tmp/mock.onnx',
  NULL,
  NULL,
  '{"mock_char_decisions":{"重":{"chosen":"zhong","confidence":0.96,"margin":0.40}}}'::jsonb,
  true
);

SELECT plan(19);

SELECT is(
  public.pinyin_char_romanize('郑爽ABC'),
  'zheng shuang abc',
  'base char romanize uses base tables'
);

SELECT is(
  public.pinyin_char_romanize('郑爽ABC', '_suffix1'),
  'zhengx shuang abc',
  'suffix char romanize gives suffix table priority'
);

SELECT is(
  public.pinyin_word_romanize('郑爽ABC', '_suffix1'::text),
  'zhengx shuangx abc',
  'suffix word romanize gives suffix word table priority'
);

SELECT is(
  public.pinyin_word_romanize(ARRAY['郑爽', 'ABC']::text[], '_suffix1'::text),
  'zhengx shuangx abc',
  'suffix tokenizer overload works'
);

SELECT is(
  public.pinyin_word_romanize('郑爽ABC', '_nosuch'::text),
  public.pinyin_word_romanize('郑爽ABC'),
  'missing suffix tables fall back to base dictionary'
);

SELECT is(
  public.pinyin_word_romanize('郑爽ABC', model => 'g2pw'),
  public.pinyin_word_romanize('郑爽ABC'),
  'word romanize with missing model config reports dictionary-equivalent output in debug-free path only when no polyphone fallback is needed'
);

SELECT is(
  public.pinyin_word_romanize(ARRAY['郑爽', 'ABC']::text[], model => 'g2pw'),
  'zheng shuang abc',
  'word tokenizer model overload works when no model fallback is needed'
);

SELECT ok(
  public.pinyin_word_romanize_debug('郑爽ABC')::jsonb ? 'tokens',
  'debug function returns jsonb payload with tokens'
);

SELECT ok(
  public.pinyin_model_romanize_debug('郑爽', 'g2pw')::jsonb ? 'model_error',
  'model-only debug function returns jsonb payload'
);

UPDATE pinyin.pinyin_mapping_suffix1
SET pinyin = '|zhengy|'
WHERE character = '郑';

SELECT is(
  public.pinyin_char_romanize('郑爽ABC', '_suffix1'),
  'zhengx shuang abc',
  'suffix cache remains unchanged before explicit clear'
);

SELECT ok(
  public.pinyin_clear_suffix_cache('_suffix1'),
  'clear suffix cache by suffix'
);

SELECT is(
  public.pinyin_char_romanize('郑爽ABC', '_suffix1'),
  'zhengy shuang abc',
  'suffix cache refreshes after explicit clear'
);

SELECT is(
  (
    SELECT p.provolatile::text
    FROM pg_proc AS p
    WHERE p.oid = 'public.pinyin_char_romanize(text,text)'::regprocedure
  ),
  'i',
  'pinyin_char_romanize(text,text) is immutable'
);

SELECT is(
  (
    SELECT p.provolatile::text
    FROM pg_proc AS p
    WHERE p.oid = 'public.pinyin_word_romanize(text,text)'::regprocedure
  ),
  'i',
  'pinyin_word_romanize(text,text) is immutable'
);

SELECT is(
  (
    SELECT p.provolatile::text
    FROM pg_proc AS p
    WHERE p.oid = 'public.pinyin_word_romanize(anyelement,text)'::regprocedure
  ),
  'i',
  'pinyin_word_romanize(anyelement,text) is immutable'
);

SELECT is(
  (
    SELECT p.provolatile::text
    FROM pg_proc AS p
    WHERE p.oid = 'public.pinyin_word_romanize(text,pinyin.model_identifier)'::regprocedure
  ),
  's',
  'pinyin_word_romanize(text,pinyin.model_identifier) is stable'
);

SELECT is(
  (
    SELECT p.provolatile::text
    FROM pg_proc AS p
    WHERE p.oid = 'public.pinyin_model_romanize(text,text)'::regprocedure
  ),
  's',
  'pinyin_model_romanize(text,text) is stable'
);

SELECT is(
  (
    SELECT p.provolatile::text
    FROM pg_proc AS p
    WHERE p.oid = 'public.pinyin_word_romanize_debug(text,text,text)'::regprocedure
  ),
  's',
  'pinyin_word_romanize_debug(text,text,text) is stable'
);

SELECT is(
  to_regprocedure('public.pinyin_word_romanize_hybrid(text)')::text,
  NULL,
  'legacy public hybrid function has been removed'
);

SELECT * FROM finish();

ROLLBACK;
