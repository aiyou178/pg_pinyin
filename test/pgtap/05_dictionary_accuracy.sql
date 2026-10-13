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

-- Test installed embedded data, not a replacement miniature dictionary.
SELECT public.pinyin__seed_embedded_data();
SELECT no_plan();

CREATE TEMP TABLE pronunciation_gold (
  id bigint PRIMARY KEY,
  name text NOT NULL,
  expected text NOT NULL
);
INSERT INTO pronunciation_gold VALUES
  (1, '吕梁', 'lv liang'),
  (2, '女儿', 'nv er'),
  (3, '旅行', 'lv xing'),
  (4, '绿地', 'lv di'),
  (5, '虐待', 'nve dai'),
  (6, '战略', 'zhan lve'),
  (7, '西安', 'xi an'),
  (8, '善感', 'shan gan'),
  (9, '阿奶', 'a nai'),
  (10, '天安门', 'tian an men'),
  (11, '银行', 'yin hang'),
  (12, '行长', 'hang zhang'),
  (13, '音乐', 'yin yue'),
  (14, '长大', 'zhang da'),
  (15, '重庆', 'chong qing'),
  (16, '便宜', 'pian yi'),
  (17, '仙居', 'xian ju'),
  (18, '女兒', 'nv er'),
  (19, '銀行', 'yin hang'),
  (20, '重慶', 'chong qing'),
  (21, '長安', 'chang an'),
  (22, '呂梁', 'lv liang'),
  (23, '阿爾及利亞', 'a er ji li ya'),
  (24, '劉禹錫', 'liu yu xi'),
  (25, '戰略', 'zhan lve'),
  (26, '銀行家', 'yin hang jia'),
  (27, '奧地利', 'ao di li'),
  (28, '臺灣', 'tai wan'),
  (29, '音樂', 'yin yue'),
  (30, '單反機', 'dan fan ji'),
  (31, '丙酸氟替卡鬆', 'bing suan fu ti ka song'),
  (32, '遵时養晦', 'zun shi yang hui');

SELECT is(
  (SELECT count(*) FROM pinyin.pinyin_words AS w
   JOIN pronunciation_gold AS g ON w.word = g.name
   WHERE g.id >= 18 AND w.pinyin = g.expected),
  15::bigint, 'all targeted formerly lost words are present with source syllables'
);
SELECT is((SELECT pinyin FROM pinyin.pinyin_words WHERE word = '葛'),
          'ge', 'historical singleton dictionary key is not silently discarded');

SELECT is(public.pinyin_word_romanize(name), expected,
          'Rust text phrase preserves pronunciation: ' || name)
FROM pronunciation_gold ORDER BY id;
SELECT is(public.pinyin_word_romanize(ARRAY[name]::text[]), expected,
          'Rust tokenizer phrase preserves boundaries: ' || name)
FROM pronunciation_gold ORDER BY id;
SELECT is(public.icu_romanize_tokens(ARRAY[name]::text[]), expected,
          'independent SQL phrase backend: ' || name)
FROM pronunciation_gold ORDER BY id;

SELECT is(public.pinyin_char_romanize('吕女旅绿虐略'),
          'lv nv lv lv nve lve', 'character fallback retains umlaut as v');
SELECT is(public.pinyin_char_romanize('居区需'),
          'ju qu xu', 'ordinary ju/qu/xu orthography remains unchanged');
SELECT is(
  (SELECT count(*) FROM pinyin.pinyin_words
   WHERE char_length(word) <> cardinality(string_to_array(pinyin, ' '))),
  0::bigint, 'every imported phrase has one retained syllable per character'
);
SELECT is(public.pinyin_regex_phrase_patterns('xi an'),
          ARRAY['xi.*', 'an.*'], 'explicit xi an boundaries survive query parsing');
SELECT is(public.pinyin_regex_phrase_patterns('shan gan'),
          ARRAY['shan.*', 'gan.*'], 'explicit shan gan is not shang an');
SELECT is(public.pinyin_regex_phrase_patterns('a nai'),
          ARRAY['a.*', 'nai.*'], 'explicit a nai is not an ai');
SELECT is(public.pinyin_regex_phrase_patterns('nv er'),
          ARRAY['nv.*', 'er.*'], 'canonical umlaut query produces exact patterns');
SELECT is(public.pinyin_regex_phrase_patterns('whh'),
          ARRAY['w.*', 'h.*', 'h.*'], 'initial splitting stays compatible');
SELECT is(public.sql_pinyin_regex_phrase_patterns('lv liang'),
          public.pinyin_regex_phrase_patterns('lv liang'), 'SQL and Rust umlaut pattern arrays agree');

CREATE TABLE public.pinyin_dictionary_accuracy_probe AS
SELECT id, name, public.pinyin_word_romanize(ARRAY[name]::text[]) AS pinyin
FROM pronunciation_gold;
CREATE INDEX pinyin_dictionary_accuracy_bm25 ON public.pinyin_dictionary_accuracy_probe
USING bm25 (id, pinyin) WITH (key_field = 'id');

SELECT results_eq(
  format('SELECT id FROM public.pinyin_dictionary_accuracy_probe WHERE id = %s AND pinyin @@@ public.pinyin_regex_phrase(%L)',
         g.id, g.expected),
  format('VALUES (%s::bigint)', g.id),
  'BM25 full syllables retrieve target: ' || g.name
)
FROM pronunciation_gold AS g ORDER BY id;
SELECT is(
  (SELECT array_agg(id ORDER BY id) FROM public.pinyin_dictionary_accuracy_probe
   WHERE pinyin @@@ public.pinyin_regex_phrase('xi an')),
  ARRAY[7]::bigint[], 'xi an does not confuse one-syllable xian in 仙居'
);

SELECT * FROM finish();
\else
SELECT plan(1);
SELECT skip('dictionary BM25 accuracy checks require preloaded pg_search', 1);
SELECT * FROM finish();
\endif
ROLLBACK;
