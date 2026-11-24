-- Optional pg_search helpers.
-- This file is intentionally separate from CREATE EXTENSION pg_pinyin because
-- the query builders return pdb.query and therefore require pg_search.

CREATE OR REPLACE FUNCTION public.icu_romanize_tokens(tokens text[])
RETURNS text
LANGUAGE plpgsql
IMMUTABLE
STRICT
PARALLEL SAFE
AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM unnest(tokens) AS t(token) WHERE token IS NULL) THEN
    RAISE EXCEPTION 'tokenizer text[] input must not contain NULL elements';
  END IF;
  RETURN (WITH input_tokens AS (
    SELECT t.token,t.position FROM unnest(tokens) WITH ORDINALITY AS t(token,position)
  ), normalized AS (
    SELECT t.position, public.characters2romanize(t.token) AS pinyin FROM input_tokens t
  )
  SELECT COALESCE(string_agg(pinyin,' ' ORDER BY position) FILTER (WHERE pinyin <> ''),'')
  FROM normalized);
END;
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
LANGUAGE plpgsql
STABLE
PARALLEL SAFE
AS $$
DECLARE
  normalized text := translate(value, U&'\0027\2018\2019', '   ');
  pattern text;
BEGIN
  IF normalized IS NULL THEN
    RETURN NULL;
  END IF;
  IF normalized !~ '^[A-Za-z[:space:]]+$' THEN
    RETURN ARRAY[]::text[];
  END IF;

  SELECT string_agg(character, '|' ORDER BY char_length(character) DESC, character)
  INTO pattern
  FROM pinyin.pinyin_token
  WHERE category = 1;
  IF pattern IS NULL THEN
    RETURN ARRAY[]::text[];
  END IF;

  -- Keep live legacy regex rows and empty-dictionary behavior, without dynamic
  -- SQL planning or materializing the shared tokenizer's SETOF result first.
  RETURN (
    SELECT COALESCE(array_agg(
      CASE WHEN generated_pinyin THEN '.*\|' || matched[1] || '.*'
           ELSE matched[1] || '.*' END ORDER BY ord
    ), ARRAY[]::text[])
    FROM regexp_matches(lower(normalized), '(' || pattern || '|zh|ch|sh|[a-z])', 'g')
      WITH ORDINALITY AS tokenized(matched, ord)
  );
END;
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
  inventory_pattern text;
  inventory_text text;
  compat_pattern text;
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
  auto_prefix boolean := false;
  auto_fallback boolean := false;
  pattern_chunks integer[] := ARRAY[]::integer[];
  resolved_patterns text[];
  validation_remaining text;
  validation_consumed integer;
BEGIN
  IF value IS NULL OR mode IS NULL OR generated_pinyin IS NULL THEN
    RETURN NULL;
  END IF;
  IF mode = 'compat' THEN
    RETURN public.sql_pinyin_regex_phrase_patterns(value, generated_pinyin);
  END IF;
  IF mode NOT IN ('full', 'initials', 'autocomplete', 'auto') THEN
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
      IF syllabic_tone AND current_chunk NOT IN ('m', 'n', 'ng') THEN
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
  -- The regex engine selects the longest anchored match. Delimited, length-
  -- sorted entries let prefix tests use C string operations instead of loops.
  inventory_text := array_to_string(inventory, '|');
  IF mode <> 'initials' THEN
    inventory_pattern := '^(?:' || inventory_text || ')';
  END IF;
  inventory_text := '|' || inventory_text || '|';

  IF mode = 'auto' THEN
    SELECT '^(?:' || string_agg(t.character, '|' ORDER BY char_length(t.character) DESC, t.character COLLATE "C") || ')'
    INTO compat_pattern FROM (
      SELECT character FROM pinyin.pinyin_token WHERE category = 1 AND character ~ '^[a-z]+$'
      UNION SELECT 'zh' UNION SELECT 'ch' UNION SELECT 'sh'
    ) AS t;

    FOR chunk_index IN 1..cardinality(chunks) LOOP
      remaining := chunks[chunk_index];
      consumed := 0;
      WHILE remaining <> '' LOOP
        matched := NULL;
        is_prefix := false;
        SELECT count(*) INTO tone_count FROM generate_subscripts(tone_positions,1) AS j
        WHERE tone_chunks[j] = chunk_index AND tone_positions[j] > consumed;
        IF NOT generated_pinyin AND chunk_index = cardinality(chunks)
           AND NOT numeric_tones[chunk_index] AND tone_count <= 1
           AND strpos(inventory_text, '|' || remaining) > 0 THEN
          matched := remaining;
          is_prefix := NOT (matched = ANY(inventory)) OR
            (tone_count = 0 AND substr(inventory_text,
             strpos(inventory_text, '|' || matched) + char_length(matched) + 1,1) <> '|');
        END IF;
        IF matched IS NULL THEN matched := substring(remaining FROM inventory_pattern); END IF;
        IF matched IS NOT NULL THEN
          SELECT count(*) INTO tone_count FROM generate_subscripts(tone_positions,1) AS j
          WHERE tone_chunks[j] = chunk_index AND tone_positions[j] > consumed
            AND tone_positions[j] <= consumed + char_length(matched);
          IF tone_count > 1 OR (remaining = matched AND numeric_tones[chunk_index] AND tone_count > 0) THEN
            RETURN ARRAY[]::text[];
          END IF;
          IF matched IN ('m','n') AND (remaining <> matched OR chunk_index < cardinality(chunks))
             AND tone_count = 0 AND NOT (numeric_tones[chunk_index] AND remaining = matched) THEN
            matched := NULL;
          END IF;
        END IF;
        IF matched IS NULL THEN
          auto_fallback := true;
          IF chunk_has_tones[chunk_index] OR numeric_tones[chunk_index] THEN
            validation_remaining := remaining;
            validation_consumed := consumed;
            WHILE validation_remaining <> '' LOOP
              matched := substring(validation_remaining FROM inventory_pattern);
              IF matched IS NULL THEN RETURN ARRAY[]::text[]; END IF;
              SELECT count(*) INTO tone_count FROM generate_subscripts(tone_positions,1) AS j
              WHERE tone_chunks[j] = chunk_index AND tone_positions[j] > validation_consumed
                AND tone_positions[j] <= validation_consumed + char_length(matched);
              IF tone_count > 1 OR (validation_remaining = matched AND numeric_tones[chunk_index] AND tone_count > 0) THEN
                RETURN ARRAY[]::text[];
              END IF;
              validation_remaining := substr(validation_remaining,char_length(matched)+1);
              validation_consumed := validation_consumed + char_length(matched);
            END LOOP;
          END IF;
          WHILE remaining <> '' LOOP
            matched := COALESCE(substring(remaining FROM compat_pattern),left(remaining,1));
            patterns := array_append(patterns,
              CASE WHEN generated_pinyin THEN '.*\|' ELSE '' END || matched || '.*');
            pattern_chunks := array_append(pattern_chunks,chunk_index);
            remaining := substr(remaining,char_length(matched)+1);
          END LOOP;
        ELSE
          patterns := array_append(patterns,
            CASE WHEN generated_pinyin THEN '.*\|' || matched || '.*'
                 ELSE matched || CASE WHEN is_prefix THEN '.*' ELSE '' END END);
          pattern_chunks := array_append(pattern_chunks,chunk_index);
          consumed := consumed + char_length(matched);
          remaining := substr(remaining,char_length(matched)+1);
        END IF;
        IF cardinality(patterns) > 64 THEN RETURN ARRAY[]::text[]; END IF;
      END LOOP;
    END LOOP;
    IF auto_fallback THEN
      resolved_patterns := patterns;
      patterns := ARRAY[]::text[];
      FOR chunk_index IN 1..cardinality(chunks) LOOP
        IF numeric_tones[chunk_index] OR chunk_has_tones[chunk_index] THEN
          FOR position IN 1..cardinality(resolved_patterns) LOOP
            IF pattern_chunks[position] = chunk_index THEN
              patterns := array_append(patterns,resolved_patterns[position]);
            END IF;
          END LOOP;
        ELSE
          remaining := chunks[chunk_index];
          WHILE remaining <> '' LOOP
            matched := COALESCE(substring(remaining FROM compat_pattern),left(remaining,1));
            patterns := array_append(patterns,
              CASE WHEN generated_pinyin THEN '.*\|' ELSE '' END || matched || '.*');
            remaining := substr(remaining,char_length(matched)+1);
          END LOOP;
        END IF;
      END LOOP;
    END IF;
    IF cardinality(patterns) > 64 THEN RETURN ARRAY[]::text[]; END IF;
    -- Unmarked single-letter sequences remain initials, including bare vowels.
    IF cardinality(patterns) > 1 AND NOT (true = ANY(numeric_tones))
       AND NOT (true = ANY(chunk_has_tones)) AND NOT EXISTS (
         SELECT 1 FROM unnest(patterns) AS p WHERE p !~ '^[a-z]([.][*])?$'
       ) THEN
      SELECT array_agg(left(p,1) || '.*' ORDER BY n) INTO patterns
      FROM unnest(patterns) WITH ORDINALITY AS t(p,n);
    END IF;
    RETURN patterns;
  END IF;

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
          IF strpos(inventory_text, '|' || remaining) > 0 THEN
            matched := remaining;
          END IF;
        END IF;
        IF matched IS NULL THEN
          matched := substring(remaining FROM inventory_pattern);
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
        prefix_exists := strpos(inventory_text, '|' || token) > 0;
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

DO $sql_pinyin_auto_capabilities$
DECLARE definition text;
BEGIN
  definition := $create_function$
CREATE OR REPLACE FUNCTION public.sql_pinyin_regex_phrase_mode(
  value text,
  mode text,
  slope integer DEFAULT NULL,
  max_expansions integer DEFAULT NULL,
  generated_pinyin boolean DEFAULT false
)
RETURNS pdb.query
LANGUAGE plpgsql
STABLE
PARALLEL SAFE
AS $$
DECLARE
  patterns text[];
  tokens text[];
  all_full boolean := true;
  terminal_prefix boolean := false;
  token_index integer;
BEGIN
  patterns := public.sql_pinyin_regex_phrase_patterns_mode(value, mode, generated_pinyin);
  IF mode <> 'auto' OR generated_pinyin OR max_expansions IS NOT NULL THEN
    RETURN public.sql_pinyin__regex_phrase_query_from_patterns(patterns, slope, max_expansions);
  END IF;
  IF patterns IS NULL THEN
    RETURN NULL;
  END IF;
  IF cardinality(patterns) = 0 THEN
    RETURN pdb.empty();
  END IF;

  -- These shapes come only from the parser above, never from caller regexes.
  tokens := patterns;
  FOR token_index IN 1..cardinality(patterns) LOOP
    IF patterns[token_index] ~ '^[a-z]+$' THEN
      CONTINUE;
    END IF;
    all_full := false;
    IF token_index = cardinality(patterns) AND patterns[token_index] ~ '^[a-z]+[.][*]$' THEN
      terminal_prefix := true;
      tokens[token_index] := left(patterns[token_index], -2);
    ELSE
      EXIT;
    END IF;
  END LOOP;
  IF all_full THEN
    IF cardinality(patterns) = 1 AND to_regprocedure('pdb.term(text)') IS NOT NULL THEN
      RETURN pdb.term(patterns[1]);
    END IF;
    IF cardinality(patterns) > 1 AND to_regprocedure('pdb.phrase_array(text[])') IS NOT NULL THEN
      IF slope IS NULL OR slope = 0 THEN
        RETURN pdb.phrase_array(patterns);
      END IF;
      IF slope > 0 AND to_regtype('pdb.slop') IS NOT NULL THEN
        IF to_regprocedure('paradedb.query_to_slop(pdb.query,integer,boolean)') IS NOT NULL
           AND to_regprocedure('paradedb.slop_to_query(pdb.slop)') IS NOT NULL THEN
          RETURN paradedb.slop_to_query(paradedb.query_to_slop(pdb.phrase_array(patterns), slope, true));
        END IF;
      END IF;
    END IF;
  ELSIF terminal_prefix AND cardinality(patterns) > 1 AND (slope IS NULL OR slope = 0)
        AND to_regprocedure('pdb.phrase_prefix(text[],integer)') IS NOT NULL THEN
    -- Keep the prefix ceiling aligned with regex phrase, not the default 50.
    -- An unbounded value would cause a huge upfront allocation upstream.
    RETURN pdb.phrase_prefix(tokens, 16384);
  END IF;
  RETURN public.sql_pinyin__regex_phrase_query_from_patterns(patterns, slope, max_expansions);
END;
$$;
$create_function$;
EXECUTE definition;
END;
$sql_pinyin_auto_capabilities$;

DO $sql_pinyin_native_phrase$
DECLARE
  native_phrase_available boolean := false;
BEGIN
  IF to_regtype('pdb.slop') IS NOT NULL THEN
    native_phrase_available :=
      to_regprocedure('pdb.empty()') IS NOT NULL
      AND to_regprocedure('pdb.term(text)') IS NOT NULL
      AND to_regprocedure('pdb.phrase_array(text[])') IS NOT NULL
      AND to_regprocedure('paradedb.query_to_slop(pdb.query,integer,boolean)') IS NOT NULL
      AND to_regprocedure('paradedb.slop_to_query(pdb.slop)') IS NOT NULL;
  END IF;

  IF native_phrase_available THEN
    EXECUTE $create_function$
      CREATE OR REPLACE FUNCTION public.sql_pinyin_phrase(
        value text,
        slope integer DEFAULT NULL
      )
      RETURNS pdb.query
      LANGUAGE plpgsql
      STABLE
      PARALLEL SAFE
      AS $function$
      DECLARE
        patterns text[];
      BEGIN
        IF value IS NULL THEN
          RETURN NULL;
        END IF;
        IF slope < 0 THEN
          RAISE EXCEPTION 'pinyin phrase slope must be non-negative'
            USING ERRCODE = '22023';
        END IF;

        patterns := public.sql_pinyin_regex_phrase_patterns_mode(value, 'full', false);
        IF cardinality(patterns) = 0 THEN
          RETURN pdb.empty();
        END IF;
        IF cardinality(patterns) = 1 THEN
          RETURN pdb.term(patterns[1]);
        END IF;
        IF slope IS NULL OR slope = 0 THEN
          RETURN pdb.phrase_array(patterns);
        END IF;
        RETURN paradedb.slop_to_query(
          paradedb.query_to_slop(pdb.phrase_array(patterns), slope, true)
        );
      END;
      $function$;
    $create_function$;
  ELSE
    EXECUTE $create_function$
      CREATE OR REPLACE FUNCTION public.sql_pinyin_phrase(
        value text,
        slope integer DEFAULT NULL
      )
      RETURNS pdb.query
      LANGUAGE plpgsql
      STABLE
      PARALLEL SAFE
      AS $function$
      BEGIN
        RAISE EXCEPTION
          'public.sql_pinyin_phrase requires native phrase APIs; upgrade pg_search and reload sql/word.sql'
          USING ERRCODE = '0A000';
      END;
      $function$;
    $create_function$;
  END IF;
END;
$sql_pinyin_native_phrase$;
