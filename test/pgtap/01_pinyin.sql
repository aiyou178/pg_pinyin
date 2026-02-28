BEGIN;

CREATE EXTENSION IF NOT EXISTS pgtap;
\ir ../../sql/pinyin.sql

TRUNCATE TABLE public.pinyin_mapping;
TRUNCATE TABLE public.pinyin_token;
TRUNCATE TABLE public.pinyin_words;

INSERT INTO public.pinyin_mapping (character, pinyin) VALUES
  (' ', ' '),
  ('我', '|wo|'),
  ('们', '|men|'),
  ('重', '|tong|zhong|chong|'),
  ('起', '|qi|'),
  ('郑', '|zheng|'),
  ('爽', '|shuang|');

INSERT INTO public.pinyin_token (character, category) VALUES
  ('wang', 1), ('chong', 1), ('yang', 1),
  ('zh', 2), ('ch', 2), ('sh', 2),
  ('w', 2), ('c', 2), ('h', 2), ('y', 2),
  ('xi', 1), ('an', 1);

INSERT INTO public.pinyin_words (word, pinyin)
VALUES ('郑爽', '|zheng| |shuang|');

SELECT plan(15);

SELECT is(public.normalize2text('我们'), '我们', 'normalize2text keeps mapped Han chars');
SELECT is(public.normalize2text('我ABC们123'), '我ABC们123', 'normalize2text keeps ASCII runs');
SELECT is(public.normalize2text('我!!!们'), '我 们', 'normalize2text collapses unknown chars to one space');

SELECT is_deeply(
  public.normalize2array('我ABC们123'),
  ARRAY['我', 'ABC', '们', '123']::text[],
  'normalize2array splits into Han chars and ASCII runs'
);

SELECT is(public.characters2pinyin('我ABC们123'), '|wo| |ABC| |men| |123|', 'characters2pinyin maps Han chars and keeps ASCII');
SELECT is(public.characters2pinyin('重起'), '|tong|zhong|chong| |qi|', 'characters2pinyin keeps polyphone mapping string');
SELECT is(public.characters2pinyin('郑爽'), '|zheng| |shuang|', 'characters2pinyin applies full-word override from pinyin_words');

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

SELECT ok(public.normalize2text(NULL) IS NULL, 'normalize2text(NULL) returns NULL');
SELECT ok(public.normalize2array(NULL) IS NULL, 'normalize2array(NULL) returns NULL');
SELECT ok(public.characters2pinyin(NULL) IS NULL, 'characters2pinyin(NULL) returns NULL');

SELECT * FROM finish();

ROLLBACK;
