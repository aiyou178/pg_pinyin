BEGIN;
CREATE EXTENSION IF NOT EXISTS pgtap;
CREATE EXTENSION IF NOT EXISTS pg_pinyin;
SELECT no_plan();

TRUNCATE pinyin.pinyin_mapping, pinyin.pinyin_words;
INSERT INTO pinyin.pinyin_mapping VALUES ('葛','|ge|'),('优','|you|'),('甲','|jia|');
INSERT INTO pinyin.pinyin_words VALUES
  ('葛','ge'),('葛优','ge you'),('葛优甲','ge you yi'),('𰻞甲','bian jia');
SELECT is(public.pinyin_word_romanize('葛优甲'), 'ge you yi', 'longest phrase wins over singleton');
UPDATE pinyin.pinyin_words SET pinyin='custom surname' WHERE word='葛优';
SELECT is(public.pinyin_word_romanize('葛优'), 'custom surname', 'multi-character override is not shadowed');
SELECT is(public.pinyin_word_romanize('𰻞甲'), 'bian jia', 'phrase lookup retains unmapped rare Han');
SELECT is(public.pinyin_word_romanize(ARRAY['𰻞','甲']), 'bian jia', 'array phrase retains rare Han');
SELECT is(public.pinyin_word_romanize('葛优ABC！'), 'custom surname abc', 'plain mixed input normalized');
SELECT is(public.pinyin_word_romanize(ARRAY['葛','优ABC','！']), 'custom surname abc', 'multi-character tokenizer chunks normalized');
SELECT is(public.pinyin_word_romanize(ARRAY[]::text[]), '', 'empty tokenizer array is empty text');
SELECT throws_ok($$SELECT public.pinyin_word_romanize(ARRAY['葛',NULL])$$,
  'XX000','tokenizer text[] input must not contain NULL elements','NULL tokenizer elements remain rejected');

SELECT is(public.pinyin_word_romanize('葛优','newcase'), 'custom surname', 'missing suffix uses base');
CREATE TABLE pinyin.pinyin_words_newcase (LIKE pinyin.pinyin_words INCLUDING ALL);
INSERT INTO pinyin.pinyin_words_newcase VALUES ('葛优','overlay surname');
SELECT is(public.pinyin_word_romanize('葛优','NewCase'), 'overlay surname', 'suffix creation invalidates missing overlay and folds case');
DROP TABLE pinyin.pinyin_words_newcase;
SELECT is(public.pinyin_word_romanize('葛优','newcase'), 'custom surname', 'dropped overlay no longer shadows base');
CREATE TABLE pinyin.pinyin_words_newcase (LIKE pinyin.pinyin_words INCLUDING ALL);
INSERT INTO pinyin.pinyin_words_newcase VALUES ('葛优','new oid');
SELECT is(public.pinyin_word_romanize('葛优','newcase'), 'new oid', 'recreated overlay reloads by relation OID');
SELECT throws_ok($$SELECT public.pinyin_word_romanize('葛优', repeat('a',41))$$,
  'XX000','dictionary table suffix must not exceed 40 ASCII characters','suffix fits PostgreSQL identifier limit');
SELECT throws_ok($$SELECT public.pinyin_word_romanize_batch(ARRAY[]::text[], repeat('a',41))$$,
  'XX000','dictionary table suffix must not exceed 40 ASCII characters','empty batch validates suffix length');
SELECT throws_ok($$SELECT public.pinyin_word_romanize_batch(ARRAY[NULL::text], 'bad!')$$,
  'XX000','dictionary table suffix must contain only [A-Za-z0-9_]','all-NULL batch validates suffix characters');

CREATE SEQUENCE pinyin.pinyin_words_seqprobe;
SELECT is(public.pinyin_word_romanize('葛优', 'seqprobe'), 'custom surname',
  'non-table relation with suffix name is ignored safely');
DROP SEQUENCE pinyin.pinyin_words_seqprobe;

CREATE TABLE pinyin.pinyin_words_delta (LIKE pinyin.pinyin_words INCLUDING ALL);
INSERT INTO pinyin.pinyin_words_delta VALUES ('葛优', 'delta two'), ('甲葛', '   ');
SELECT is(public.pinyin_word_romanize('葛优甲', 'delta'), 'ge you yi',
  'longer base phrase wins over shorter delta overlay phrase');
SELECT is(public.pinyin_word_romanize('葛优', 'delta'), 'delta two',
  'equal-length delta overlay phrase overrides base');
SELECT is(public.pinyin_word_romanize('甲葛', 'delta'), '',
  'empty delta overlay word overrides base without character fallback');
DROP TABLE pinyin.pinyin_words_delta;

SAVEPOINT char_only_suffix;
ALTER TABLE pinyin.pinyin_words RENAME TO remediation_hidden_words;
INSERT INTO pinyin.pinyin_mapping VALUES ('乙','|yi|');
SELECT results_eq($$SELECT public.pinyin_char_romanize('乙','absent')$$,
  $$VALUES ('yi'::text)$$,'missing suffix char-only conversion does not load the word dictionary');
ROLLBACK TO char_only_suffix;

SELECT is(public.pinyin_regex_phrase_patterns_mode(value,'auto'),expected,'auto toned boundary: ' || value)
FROM (VALUES
  ('wúh', ARRAY['wu','h.*']),
  ('ni3hao', ARRAY['ni','hao']),
  ('wu2h', ARRAY['wu','h.*']),
  ('nǐyón', ARRAY['ni','yon.*']),
  ('ni3 whh', ARRAY['ni','w.*','h.*','h.*']),
  ('hǎó', ARRAY[]::text[]),
  ('wúh whh', ARRAY[]::text[]),
  ('wúh hǎo', ARRAY[]::text[]),
  ('whh wúh', ARRAY['w.*','h.*','h.*','wu','h.*']),
  ('mni1 whh', ARRAY['m.*','ni.*','w.*','h.*','h.*'])
) AS gold(value,expected);
SELECT is(
  public.pinyin_regex_phrase_patterns_mode('mni1', 'auto', true),
  ARRAY['.*\|m.*', '.*\|ni.*'],
  'auto generated fallback keeps pipe prefix on toned compatibility chunks'
);
SELECT is(public.pinyin_regex_phrase_patterns_mode(value,'full'),ARRAY['ng'],'syllabic nasal: ' || value)
FROM (VALUES ('ńg'),('ňg'),('ǹg'),('ng2')) AS gold(value);
SELECT is(public.pinyin_regex_phrase_patterns('whh'),ARRAY['w.*','h.*','h.*'],'legacy initials unchanged');

SELECT * FROM finish();
ROLLBACK;
