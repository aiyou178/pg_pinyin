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
  WITH input AS (
    SELECT translate(value, U&'\0027\2018\2019', '   ') AS normalized
  )
  SELECT CASE
    WHEN input.normalized IS NULL THEN NULL
    WHEN input.normalized !~ '^[A-Za-z[:space:]]+$' THEN ARRAY[]::text[]
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
        FROM public.pinyin_tokenize(input.normalized, true) AS token
      ) AS tokenized
    ), ARRAY[]::text[])
    END
  FROM input
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

-- Separate mode APIs avoid changing resolution of legacy calls with NULL.
CREATE OR REPLACE FUNCTION public.sql_pinyin_regex_phrase_patterns_mode(
  value text,
  mode text,
  generated_pinyin boolean DEFAULT false
)
RETURNS text[]
LANGUAGE plpgsql
STABLE
PARALLEL SAFE
AS $$
DECLARE
  normalized text;
  ch text;
  previous_letter text := '';
  raw_letter text := '';
  diaeresis_base boolean := false;
  marked_base boolean := false;
  syllabic_tone boolean := false;
  current_chunk text := '';
  chunks text[] := ARRAY[]::text[];
  numeric_tones boolean[] := ARRAY[]::boolean[];
  chunk_has_tones boolean[] := ARRAY[]::boolean[];
  current_has_tones boolean := false;
  tone_chunks integer[] := ARRAY[]::integer[];
  tone_positions integer[] := ARRAY[]::integer[];
  inventory text[];
  tokens text[] := ARRAY[]::text[];
  patterns text[] := ARRAY[]::text[];
  remaining text;
  token text;
  matched text;
  consumed integer;
  position integer;
  chunk_index integer;
  tone_index integer;
  tone_count integer;
  is_prefix boolean;
  prefix_exists boolean;
BEGIN
  IF value IS NULL OR mode IS NULL OR generated_pinyin IS NULL THEN
    RETURN NULL;
  END IF;
  IF mode = 'compat' THEN
    RETURN public.sql_pinyin_regex_phrase_patterns(value, generated_pinyin);
  END IF;
  IF mode NOT IN ('full', 'initials', 'autocomplete') THEN
    RAISE EXCEPTION 'unknown pinyin matching mode: %', mode USING ERRCODE = '22023';
  END IF;
  IF char_length(value) > 256 THEN
    RETURN ARRAY[]::text[];
  END IF;

  normalized := normalize(lower(value), NFD);
  -- Record tone positions before discarding marks, so a numeric tone cannot
  -- silently duplicate an accent in the last segmented syllable.
  FOR position IN 1..char_length(normalized) + 1 LOOP
    ch := CASE WHEN position > char_length(normalized) THEN ' '
               ELSE substr(normalized, position, 1) END;
    IF ch ~ '^[a-z]$' THEN
      current_chunk := current_chunk || ch;
      previous_letter := ch;
      raw_letter := ch;
      diaeresis_base := false;
      marked_base := false;
    ELSIF ch = U&'\0308' THEN
      IF raw_letter <> 'u' OR diaeresis_base THEN
        RETURN ARRAY[]::text[];
      END IF;
      current_chunk := left(current_chunk, -1) || 'v';
      previous_letter := 'v';
      diaeresis_base := true;
    ELSIF ch = ':' THEN
      IF raw_letter <> 'u' OR diaeresis_base OR marked_base THEN
        RETURN ARRAY[]::text[];
      END IF;
      current_chunk := left(current_chunk, -1) || 'v';
      previous_letter := 'v';
      diaeresis_base := true;
    ELSIF ch IN (U&'\0300', U&'\0301', U&'\0304', U&'\030C') THEN
      IF previous_letter NOT IN ('a', 'e', 'i', 'o', 'u', 'v', 'm', 'n')
         OR marked_base OR current_chunk = '' THEN
        RETURN ARRAY[]::text[];
      END IF;
      IF previous_letter IN ('m', 'n') THEN
        syllabic_tone := true;
      END IF;
      marked_base := true;
      current_has_tones := true;
      tone_chunks := array_append(tone_chunks, cardinality(chunks) + 1);
      tone_positions := array_append(tone_positions, char_length(current_chunk));
    ELSIF ch ~ '^[0-5]$'
          OR ch IN ('''', U&'\2018', U&'\2019')
          OR strpos(U&'\0009\000A\000B\000C\000D\0020\0085\00A0\1680\2000\2001\2002\2003\2004\2005\2006\2007\2008\2009\200A\2028\2029\202F\205F\3000', ch) > 0 THEN
      IF ch ~ '^[0-5]$' AND current_chunk = '' THEN
        RETURN ARRAY[]::text[];
      END IF;
      IF syllabic_tone AND current_chunk NOT IN ('m', 'n') THEN
        RETURN ARRAY[]::text[];
      END IF;
      IF current_chunk <> '' THEN
        chunks := array_append(chunks, current_chunk);
        numeric_tones := array_append(numeric_tones, ch ~ '^[0-5]$');
        chunk_has_tones := array_append(chunk_has_tones, current_has_tones);
      END IF;
      current_chunk := '';
      previous_letter := '';
      raw_letter := '';
      diaeresis_base := false;
      marked_base := false;
      syllabic_tone := false;
      current_has_tones := false;
    ELSE
      RETURN ARRAY[]::text[];
    END IF;
  END LOOP;

  IF cardinality(chunks) = 0 THEN
    RETURN ARRAY[]::text[];
  END IF;
  SELECT array_agg(t.character ORDER BY char_length(t.character) DESC, t.character COLLATE "C")
  INTO inventory
  FROM (
    -- Ignore malformed live rows rather than accepting empty/non-ASCII tokens.
    SELECT character FROM pinyin.pinyin_token
    WHERE category IN (1, 3) AND character ~ '^[a-z]+$'
    UNION SELECT 'm'
    UNION SELECT 'n'
  ) AS t;

  FOR chunk_index IN 1..cardinality(chunks) LOOP
    -- Unmarked initials are deliberately not syllabified. Tone-bearing input
    -- still has to describe full syllables before becoming initial patterns.
    IF mode <> 'initials' OR numeric_tones[chunk_index] OR chunk_has_tones[chunk_index] THEN
      remaining := chunks[chunk_index];
      consumed := 0;
      WHILE remaining <> '' LOOP
        matched := NULL;
        -- A valid trailing prefix stays in one position: yon must not become yo+n.
        IF mode = 'autocomplete' AND chunk_index = cardinality(chunks)
           AND NOT numeric_tones[chunk_index] THEN
          FOREACH token IN ARRAY inventory LOOP
            IF left(token, char_length(remaining)) = remaining THEN
              matched := remaining;
              EXIT;
            END IF;
          END LOOP;
        END IF;
        IF matched IS NULL THEN
          FOREACH token IN ARRAY inventory LOOP
            IF left(remaining, char_length(token)) = token THEN
              matched := token;
              EXIT;
            END IF;
          END LOOP;
        END IF;
        IF matched IS NULL THEN
          RETURN ARRAY[]::text[];
        END IF;
        remaining := substr(remaining, char_length(matched) + 1);
        tone_count := 0;
        FOR tone_index IN 1..cardinality(tone_positions) LOOP
          IF tone_chunks[tone_index] = chunk_index
             AND tone_positions[tone_index] > consumed
             AND tone_positions[tone_index] <= consumed + char_length(matched) THEN
            tone_count := tone_count + 1;
          END IF;
        END LOOP;
        IF tone_count > 1 OR (remaining = '' AND numeric_tones[chunk_index] AND tone_count > 0) THEN
          RETURN ARRAY[]::text[];
        END IF;
        consumed := consumed + char_length(matched);
        IF mode <> 'initials' THEN
          tokens := array_append(tokens, matched);
          IF cardinality(tokens) > 64 THEN
            RETURN ARRAY[]::text[];
          END IF;
        END IF;
      END LOOP;
    END IF;

    IF mode = 'initials' THEN
      FOR position IN 1..CASE WHEN chunks[chunk_index] IN ('zh', 'ch', 'sh') THEN 1
                             ELSE char_length(chunks[chunk_index]) END LOOP
        token := CASE WHEN chunks[chunk_index] IN ('zh', 'ch', 'sh') THEN chunks[chunk_index]
                      ELSE substr(chunks[chunk_index], position, 1) END;
        prefix_exists := false;
        FOREACH matched IN ARRAY inventory LOOP
          IF left(matched, char_length(token)) = token THEN
            prefix_exists := true;
            EXIT;
          END IF;
        END LOOP;
        IF NOT prefix_exists THEN
          RETURN ARRAY[]::text[];
        END IF;
        tokens := array_append(tokens, token);
        IF cardinality(tokens) > 64 THEN
          RETURN ARRAY[]::text[];
        END IF;
      END LOOP;
    END IF;
  END LOOP;

  FOR position IN 1..cardinality(tokens) LOOP
    token := tokens[position];
    is_prefix := mode = 'initials' OR (mode = 'autocomplete' AND position = cardinality(tokens));
    IF generated_pinyin THEN
      patterns := array_append(patterns,
        CASE WHEN is_prefix THEN '(' || token || '.*|.*\|' || token || '[^|]*\|.*)'
             ELSE '(' || token || '|.*\|' || token || '\|.*)' END);
    ELSE
      patterns := array_append(patterns, token || CASE WHEN is_prefix THEN '.*' ELSE '' END);
    END IF;
  END LOOP;
  RETURN patterns;
END;
$$;

CREATE OR REPLACE FUNCTION public.sql_pinyin_regex_phrase_mode(
  value text,
  mode text,
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
    public.sql_pinyin_regex_phrase_patterns_mode(value, mode, generated_pinyin),
    slope,
    max_expansions
  );
$$;
