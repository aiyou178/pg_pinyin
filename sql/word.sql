-- Optional pg_search helpers.
-- This file is intentionally separate from CREATE EXTENSION pg_pinyin because
-- the query builders return pdb.query and therefore require pg_search.

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
    LEFT JOIN pinyin.pinyin_words
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
    LEFT JOIN pinyin.pinyin_mapping
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

CREATE OR REPLACE FUNCTION public.sql_pinyin_regex_phrase_patterns(
  value text,
  generated_pinyin boolean DEFAULT false
)
RETURNS text[]
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
  SELECT CASE
    WHEN value IS NULL THEN NULL
    WHEN value !~ '^[A-Za-z[:space:]]+$' THEN ARRAY[]::text[]
    ELSE COALESCE((
      SELECT array_agg(
        CASE
          WHEN generated_pinyin THEN '.*\|' || token || '.*'
          ELSE token || '.*'
        END
        ORDER BY ord
      )
      FROM (
        SELECT token, row_number() OVER () AS ord
        FROM public.pinyin_tokenize(value, true) AS token
      ) AS tokenized
    ), ARRAY[]::text[])
    END
$$;

CREATE OR REPLACE FUNCTION public.sql_pinyin__regex_phrase_query_from_patterns(
  patterns text[],
  slope integer DEFAULT NULL,
  max_expansions integer DEFAULT NULL
)
RETURNS pdb.query
LANGUAGE plpgsql
STABLE
PARALLEL SAFE
AS $$
BEGIN
  IF patterns IS NULL THEN
    RETURN NULL;
  END IF;

  IF cardinality(patterns) = 0 THEN
    RETURN pdb.empty();
  END IF;

  IF cardinality(patterns) = 1 THEN
    RETURN pdb.regex(patterns[1]);
  END IF;

  IF max_expansions IS NOT NULL THEN
    RETURN pdb.regex_phrase(patterns, COALESCE(slope, 0), max_expansions);
  END IF;

  IF slope IS NOT NULL THEN
    RETURN pdb.regex_phrase(patterns, slope);
  END IF;

  RETURN pdb.regex_phrase(patterns);
END;
$$;

CREATE OR REPLACE FUNCTION public.sql_pinyin_regex_phrase(
  value text,
  slope integer DEFAULT NULL,
  max_expansions integer DEFAULT NULL,
  generated_pinyin boolean DEFAULT false
)
RETURNS pdb.query
LANGUAGE sql
STABLE
PARALLEL SAFE
AS $$
  SELECT public.sql_pinyin__regex_phrase_query_from_patterns(
    public.sql_pinyin_regex_phrase_patterns(value, generated_pinyin),
    slope,
    max_expansions
  );
$$;
