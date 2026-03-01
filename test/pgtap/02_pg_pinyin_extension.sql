BEGIN;

CREATE EXTENSION IF NOT EXISTS pgtap;
CREATE EXTENSION IF NOT EXISTS pg_pinyin;

TRUNCATE TABLE pinyin.pinyin_mapping;
TRUNCATE TABLE pinyin.pinyin_words;

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

SELECT plan(10);

SELECT ok(
  public.pinyin_register_suffix('_suffix1'),
  'register suffix dictionary for trigger-based versioning'
);

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
  public.pinyin_word_romanize('郑爽ABC', '_suffix1'),
  'zhengx shuangx abc',
  'suffix word romanize gives suffix word table priority'
);

SELECT is(
  public.pinyin_word_romanize(ARRAY['郑爽', 'ABC']::text[], '_suffix1'),
  'zhengx shuangx abc',
  'suffix tokenizer overload works'
);

SELECT is(
  public.pinyin_word_romanize('郑爽ABC', '_nosuch'),
  public.pinyin_word_romanize('郑爽ABC'),
  'missing suffix tables fall back to base dictionary'
);

UPDATE pinyin.pinyin_mapping_suffix1
SET pinyin = '|zhengy|'
WHERE character = '郑';

SELECT is(
  public.pinyin_char_romanize('郑爽ABC', '_suffix1'),
  'zhengy shuang abc',
  'suffix cache refreshes on next statement after user-table update'
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

SELECT * FROM finish();

ROLLBACK;
