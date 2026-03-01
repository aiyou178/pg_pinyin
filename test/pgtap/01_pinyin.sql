BEGIN;

CREATE EXTENSION IF NOT EXISTS pgtap;
\ir ../../sql/pinyin.sql

TRUNCATE TABLE pinyin.pinyin_mapping;
TRUNCATE TABLE pinyin.pinyin_token;
TRUNCATE TABLE pinyin.pinyin_words;

INSERT INTO pinyin.pinyin_mapping (character, pinyin) VALUES
  (' ', ' '),
  ('我', '|wo|'),
  ('们', '|men|'),
  ('重', '|tong|zhong|chong|'),
  ('起', '|qi|'),
  ('郑', '|zheng|'),
  ('爽', '|shuang|');

INSERT INTO pinyin.pinyin_token (character, category) VALUES
  ('wang', 1), ('chong', 1), ('yang', 1),
  ('zh', 2), ('ch', 2), ('sh', 2),
  ('w', 2), ('c', 2), ('h', 2), ('y', 2),
  ('xi', 1), ('an', 1);

INSERT INTO pinyin.pinyin_words (word, pinyin)
VALUES ('郑爽', '|zheng| |shuang|');

SELECT plan(15);

SELECT is(public.romanize2text('我们'), '我们', 'romanize2text keeps mapped Han chars');
SELECT is(public.romanize2text('我ABC们123'), '我ABC们123', 'romanize2text keeps ASCII runs');
SELECT is(public.romanize2text('我!!!们'), '我 们', 'romanize2text collapses unknown chars to one space');

SELECT is(
  public.romanize2array('我ABC们123')::text,
  ARRAY['我', 'ABC', '们', '123']::text[]::text,
  'romanize2array splits into Han chars and ASCII runs'
);

SELECT is(public.characters2romanize('我ABC们123'), '|wo| |ABC| |men| |123|', 'characters2romanize maps Han chars and keeps ASCII');
SELECT is(public.characters2romanize('重起'), '|tong|zhong|chong| |qi|', 'characters2romanize keeps polyphone mapping string');
SELECT is(public.characters2romanize('郑爽'), '|zheng| |shuang|', 'characters2romanize applies full-word override from pinyin_words');

SELECT is(
  public.pinyin_search('zh sh', false, false),
  E'\\S*\\|zh[^\\|]*\\|\\S* \\S*\\|sh[^\\|]*\\|\\S*',
  'pinyin_search supports initial matching'
);
SELECT is(
  public.pinyin_search('zhang sheng', true, true),
  E'^\\S*\\|zhang\\|\\S* \\S*\\|sheng\\|\\S*',
  'pinyin_search supports full + prefix mode'
);

SELECT bag_eq(
  $$SELECT * FROM public.pinyin_tokenize('wangchongyang', false)$$,
  $$VALUES ('wang'::text), ('chong'::text), ('yang'::text)$$,
  'pinyin_tokenize uses longest syllable match'
);

SELECT is(
  public.pinyin_isearch('wchy', false, false),
  E'\\S*\\|w[^\\|]*\\|\\S* \\S*\\|c[^\\|]*\\|\\S* \\S*\\|h[^\\|]*\\|\\S* \\S*\\|y[^\\|]*\\|\\S*',
  'pinyin_isearch defaults to initials without zh/ch/sh merge'
);

SELECT is(
  public.pinyin_isearch('wchy', false, true),
  E'\\S*\\|w[^\\|]*\\|\\S* \\S*\\|ch[^\\|]*\\|\\S* \\S*\\|y[^\\|]*\\|\\S*',
  'pinyin_isearch merges zh/ch/sh when enabled'
);

SELECT ok(public.romanize2text(NULL) IS NULL, 'romanize2text(NULL) returns NULL');
SELECT ok(public.romanize2array(NULL) IS NULL, 'romanize2array(NULL) returns NULL');
SELECT ok(public.characters2romanize(NULL) IS NULL, 'characters2romanize(NULL) returns NULL');

SELECT * FROM finish();

ROLLBACK;
