-- Plain SQL version of the pinyin helpers.
-- SQL baseline method citation: CN115905297A (https://patents.google.com/patent/CN115905297A/zh).
-- This keeps dictionary tables user-editable and avoids the huge hardcoded Han regex.

CREATE SCHEMA IF NOT EXISTS pinyin;

CREATE TABLE IF NOT EXISTS pinyin.pinyin_mapping (
  character text PRIMARY KEY,
  pinyin text NOT NULL
);

CREATE TABLE IF NOT EXISTS pinyin.pinyin_token (
  character text PRIMARY KEY,
  category smallint NOT NULL CHECK (category IN (1, 2, 3))
);

CREATE TABLE IF NOT EXISTS pinyin.pinyin_words (
  word text PRIMARY KEY,
  pinyin text NOT NULL
);

CREATE TABLE IF NOT EXISTS pinyin.pinyin_dictionary_meta (
  singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
  version bigint NOT NULL DEFAULT 1
);

INSERT INTO pinyin.pinyin_dictionary_meta (singleton, version)
VALUES (true, 1)
ON CONFLICT (singleton) DO NOTHING;

CREATE OR REPLACE FUNCTION pinyin.pinyin_dictionary_bump_version()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  UPDATE pinyin.pinyin_dictionary_meta
  SET version = version + 1
  WHERE singleton;
  RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS pinyin_mapping_bump_version ON pinyin.pinyin_mapping;
CREATE TRIGGER pinyin_mapping_bump_version
AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON pinyin.pinyin_mapping
FOR EACH STATEMENT
EXECUTE FUNCTION pinyin.pinyin_dictionary_bump_version();

DROP TRIGGER IF EXISTS pinyin_words_bump_version ON pinyin.pinyin_words;
CREATE TRIGGER pinyin_words_bump_version
AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON pinyin.pinyin_words
FOR EACH STATEMENT
EXECUTE FUNCTION pinyin.pinyin_dictionary_bump_version();

DROP TRIGGER IF EXISTS pinyin_token_bump_version ON pinyin.pinyin_token;
CREATE TRIGGER pinyin_token_bump_version
AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON pinyin.pinyin_token
FOR EACH STATEMENT
EXECUTE FUNCTION pinyin.pinyin_dictionary_bump_version();

INSERT INTO pinyin.pinyin_mapping (character, pinyin)
VALUES (' ', ' ')
ON CONFLICT (character) DO NOTHING;

CREATE OR REPLACE FUNCTION public.pinyin__split_tokens(input text)
RETURNS TABLE (token text, ord bigint)
LANGUAGE sql
IMMUTABLE
STRICT
PARALLEL SAFE
AS $$
  SELECT m[1], ord
  FROM regexp_matches(input, '([0-9A-Za-z]+|.)', 'g') WITH ORDINALITY AS t(m, ord);
$$;

CREATE OR REPLACE FUNCTION public.romanize2text(characters text)
RETURNS text
LANGUAGE sql
STABLE
STRICT
PARALLEL SAFE
AS $$
  WITH pieces AS (
    SELECT
      s.ord,
      CASE
        WHEN s.token ~ '^[0-9A-Za-z]+$' THEN s.token
        WHEN s.token ~ '^\\s$' THEN ' '
        WHEN m.character IS NOT NULL THEN s.token
        ELSE ' '
      END AS romanized_token
    FROM public.pinyin__split_tokens(characters) AS s
    LEFT JOIN pinyin.pinyin_mapping AS m
      ON m.character = s.token
  ),
  collapsed AS (
    SELECT
      ord,
      CASE
        WHEN romanized_token = ' ' AND lag(romanized_token) OVER (ORDER BY ord) = ' ' THEN NULL
        ELSE romanized_token
      END AS romanized_token
    FROM pieces
  )
  SELECT COALESCE(string_agg(romanized_token, '' ORDER BY ord), '')
  FROM collapsed
  WHERE romanized_token IS NOT NULL;
$$;

CREATE OR REPLACE FUNCTION public.romanize2array(characters text)
RETURNS text[]
LANGUAGE sql
STABLE
STRICT
PARALLEL SAFE
AS $$
  WITH romanized_text AS (
    SELECT public.romanize2text(characters) AS value
  ),
  pieces AS (
    SELECT
      (regexp_matches(value, '([0-9A-Za-z]+|.)', 'g'))[1] AS token,
      row_number() OVER () AS ord,
      value
    FROM romanized_text
  )
  SELECT CASE
    WHEN (SELECT value FROM romanized_text) = '' THEN ARRAY['']::text[]
    ELSE COALESCE(array_agg(token ORDER BY ord), ARRAY['']::text[])
  END
  FROM pieces;
$$;

CREATE OR REPLACE FUNCTION public.characters2romanize(characters text)
RETURNS text
LANGUAGE sql
STABLE
STRICT
PARALLEL SAFE
AS $$
  WITH word_hit AS (
    SELECT pinyin
    FROM pinyin.pinyin_words
    WHERE word = characters
    LIMIT 1
  ),
  char_fallback AS (
    SELECT string_agg(COALESCE(m.pinyin, CONCAT('|', t.token, '|')), ' ' ORDER BY t.ord) AS value
    FROM unnest(public.romanize2array(characters)) WITH ORDINALITY AS t(token, ord)
    LEFT JOIN pinyin.pinyin_mapping AS m
      ON m.character = t.token
  )
  SELECT COALESCE((SELECT pinyin FROM word_hit), (SELECT value FROM char_fallback));
$$;

CREATE OR REPLACE FUNCTION public.pinyin_tokenize(
  characters text,
  include_zhchsh boolean DEFAULT false
)
RETURNS SETOF text
LANGUAGE plpgsql
STABLE
STRICT
PARALLEL SAFE
AS $$
DECLARE
  pattern text;
BEGIN
  SELECT string_agg(character, '|' ORDER BY char_length(character) DESC, character)
  INTO pattern
  FROM pinyin.pinyin_token
  WHERE category = 1;

  IF pattern IS NULL THEN
    RETURN;
  END IF;

  IF include_zhchsh THEN
    pattern := pattern || '|zh|ch|sh';
  END IF;

  RETURN QUERY EXECUTE format(
    $re$SELECT (regexp_matches(lower($1), '(%s|[a-z])', 'g'))[1]$re$,
    pattern
  ) USING characters;
END;
$$;

CREATE OR REPLACE FUNCTION public.pinyin_search(
  _input text,
  is_full boolean DEFAULT false,
  prefix boolean DEFAULT false
)
RETURNS text
LANGUAGE plpgsql
IMMUTABLE
STRICT
PARALLEL SAFE
AS $$
BEGIN
  IF is_full AND prefix THEN
    RETURN '^' || regexp_replace(_input, '([[:alnum:]_]+)', '\S*\|\1\|\S*', 'g');
  ELSIF NOT is_full AND prefix THEN
    RETURN '^' || regexp_replace(_input, '([[:alnum:]_]+)', '\S*\|\1[^\|]*\|\S*', 'g');
  ELSIF is_full AND NOT prefix THEN
    RETURN regexp_replace(_input, '([[:alnum:]_]+)', '\S*\|\1\|\S*', 'g');
  ELSE
    RETURN regexp_replace(_input, '([[:alnum:]_]+)', '\S*\|\1[^\|]*\|\S*', 'g');
  END IF;
END;
$$;

CREATE OR REPLACE FUNCTION public.pinyin_isearch(
  characters text,
  prefix boolean DEFAULT false,
  include_zhchsh boolean DEFAULT false
)
RETURNS text
LANGUAGE sql
STABLE
STRICT
PARALLEL SAFE
AS $$
  WITH tokenized AS (
    SELECT
      row_number() OVER () AS ord,
      token
    FROM public.pinyin_tokenize(characters, include_zhchsh) AS token
  )
  SELECT CASE
    WHEN count(*) = 0 THEN NULL
    ELSE
      CASE WHEN prefix THEN '^' ELSE '' END ||
      string_agg(
        CASE p.category
          WHEN 2 THEN '\S*\|' || t.token || '[^\|]*\|\S*'
          ELSE '\S*\|' || t.token || '\|\S*'
        END,
        ' ' ORDER BY t.ord
      )
  END
  FROM tokenized AS t
  LEFT JOIN pinyin.pinyin_token AS p
    ON p.character = t.token;
$$;
