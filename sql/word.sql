CREATE OR REPLACE FUNCTION public.icu_romanize_tokens(tokens text[])
RETURNS text
LANGUAGE sql
IMMUTABLE
STRICT
PARALLEL SAFE
AS $$
  WITH input_tokens AS (
    SELECT
      t.token,
      t.position
    FROM unnest(tokens) WITH ORDINALITY AS t(token, position)
  ),
  word_hits AS (
    SELECT
      input_tokens.position,
      input_tokens.token,
      pinyin_words.pinyin
    FROM input_tokens
    LEFT JOIN public.pinyin_words
      ON input_tokens.token = pinyin_words.word
  ),
  char_candidates AS (
    SELECT
      word_hits.position,
      chars.ch,
      chars.ord
    FROM word_hits
    JOIN LATERAL (
      SELECT
        x.ch,
        x.ord
      FROM unnest(string_to_array(word_hits.token, NULL)) WITH ORDINALITY AS x(ch, ord)
    ) AS chars ON word_hits.pinyin IS NULL
  ),
  char_hits AS (
    SELECT
      char_candidates.position,
      char_candidates.ord,
      COALESCE(pinyin_mapping.pinyin, char_candidates.ch) AS pinyin
    FROM char_candidates
    LEFT JOIN public.pinyin_mapping
      ON char_candidates.ch = pinyin_mapping.character
  ),
  char_agg AS (
    SELECT
      char_hits.position,
      string_agg(char_hits.pinyin, ' ' ORDER BY char_hits.ord) AS pinyin
    FROM char_hits
    GROUP BY char_hits.position
  ),
  combined AS (
    SELECT
      word_hits.position,
      COALESCE(word_hits.pinyin, char_agg.pinyin, word_hits.token) AS pinyin
    FROM word_hits
    LEFT JOIN char_agg
      ON word_hits.position = char_agg.position
  )
  SELECT string_agg(combined.pinyin, ' ' ORDER BY combined.position)
  FROM combined;
$$;

CREATE OR REPLACE FUNCTION public.icu_romanize(tokenizer_input pdb.icu)
RETURNS text
LANGUAGE sql
IMMUTABLE
STRICT
PARALLEL SAFE
AS $$
  SELECT public.icu_romanize_tokens(tokenizer_input::text[]);
$$;

CREATE OR REPLACE FUNCTION public.icu_romanize(tokenizer_input anycompatible)
RETURNS text
LANGUAGE sql
IMMUTABLE
STRICT
PARALLEL SAFE
AS $$
  SELECT public.icu_romanize_tokens(tokenizer_input::text[]);
$$;
