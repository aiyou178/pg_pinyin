-- Add explicit query modes without changing the legacy overloads or live data.
CREATE OR REPLACE FUNCTION public.pinyin_regex_phrase_patterns_mode(
  value text,
  mode text,
  generated_pinyin boolean DEFAULT false
)
RETURNS text[]
AS 'MODULE_PATHNAME', 'pinyin_regex_phrase_patterns_with_mode_wrapper'
LANGUAGE c
STABLE
STRICT
PARALLEL SAFE;

DO $pinyin_query_modes$
BEGIN
  IF to_regtype('pdb.query') IS NOT NULL THEN
    EXECUTE $create_function$
      CREATE OR REPLACE FUNCTION public.pinyin_regex_phrase_mode(
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
      AS $function$
      DECLARE
        patterns text[];
        tokens text[];
        token_index integer;
        all_literals boolean := true;
        terminal_prefix boolean := false;
      BEGIN
        patterns := public.pinyin_regex_phrase_patterns_mode(value, mode, generated_pinyin);
        IF patterns IS NULL THEN
          RETURN NULL;
        END IF;
        IF cardinality(patterns) = 0 THEN
          RETURN pdb.empty();
        END IF;
        IF mode = 'auto' AND NOT generated_pinyin AND max_expansions IS NULL THEN
          -- Only canonical parser literals and a final prefix can use native queries.
          tokens := patterns;
          FOR token_index IN 1..cardinality(patterns) LOOP
            IF patterns[token_index] ~ '^[a-z]+$' THEN
              CONTINUE;
            END IF;
            all_literals := false;
            IF token_index = cardinality(patterns)
               AND patterns[token_index] ~ '^[a-z]+\.\*$' THEN
              terminal_prefix := true;
              tokens[token_index] := left(patterns[token_index], length(patterns[token_index]) - 2);
            ELSE
              EXIT;
            END IF;
          END LOOP;

          IF all_literals THEN
            IF cardinality(tokens) = 1
               AND to_regprocedure('pdb.term(text)') IS NOT NULL THEN
              RETURN pdb.term(tokens[1]);
            END IF;
            IF cardinality(tokens) > 1
               AND to_regprocedure('pdb.phrase_array(text[])') IS NOT NULL THEN
              IF slope IS NULL OR slope = 0 THEN
                RETURN pdb.phrase_array(tokens);
              END IF;
              IF slope > 0 AND to_regtype('pdb.slop') IS NOT NULL THEN
                IF to_regprocedure('paradedb.query_to_slop(pdb.query,integer,boolean)') IS NOT NULL
                   AND to_regprocedure('paradedb.slop_to_query(pdb.slop)') IS NOT NULL THEN
                  RETURN paradedb.slop_to_query(
                    paradedb.query_to_slop(pdb.phrase_array(tokens), slope, true)
                  );
                END IF;
              END IF;
            END IF;
          ELSIF terminal_prefix AND cardinality(tokens) > 1
                AND (slope IS NULL OR slope = 0)
                AND to_regprocedure('pdb.phrase_prefix(text[],integer)') IS NOT NULL THEN
            -- Avoid the native default of 50 without an unbounded upfront allocation.
            RETURN pdb.phrase_prefix(tokens, 16384);
          END IF;
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
      $function$;
    $create_function$;
  ELSE
    EXECUTE $create_function$
      CREATE OR REPLACE FUNCTION public.pinyin_regex_phrase_mode(
        value text,
        mode text,
        slope integer DEFAULT NULL,
        max_expansions integer DEFAULT NULL,
        generated_pinyin boolean DEFAULT false
      )
      RETURNS text
      LANGUAGE plpgsql
      STABLE
      PARALLEL SAFE
      AS $function$
      BEGIN
        RAISE EXCEPTION
          'public.pinyin_regex_phrase_mode requires CREATE EXTENSION pg_search before CREATE EXTENSION pg_pinyin';
      END;
      $function$;
    $create_function$;
  END IF;
END;
$pinyin_query_modes$;

-- The unreleased 0.0.9 includes independent document batches as well.
CREATE FUNCTION public.pinyin_word_romanize_batch(origins text[])
RETURNS text[]
AS 'MODULE_PATHNAME', 'pinyin_word_romanize_batch_wrapper'
LANGUAGE c
STABLE
STRICT
PARALLEL SAFE;

CREATE FUNCTION public.pinyin_word_romanize_batch(origins text[], suffix text)
RETURNS text[]
AS 'MODULE_PATHNAME', 'pinyin_word_romanize_batch_with_suffix_wrapper'
LANGUAGE c
STABLE
STRICT
PARALLEL SAFE;

-- Native full-syllable queries are opt-in because their scores differ from regex.
DO $pinyin_native_phrase$
DECLARE
  native_phrase_available boolean := false;
BEGIN
  IF to_regtype('pdb.query') IS NOT NULL THEN
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
        CREATE OR REPLACE FUNCTION public.pinyin_phrase(
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

          patterns := public.pinyin_regex_phrase_patterns_mode(value, 'full', false);
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
        CREATE OR REPLACE FUNCTION public.pinyin_phrase(
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
            'public.pinyin_phrase requires native phrase APIs; upgrade pg_search and reinstall or upgrade pg_pinyin'
            USING ERRCODE = '0A000';
        END;
        $function$;
      $create_function$;
    END IF;
  ELSE
    EXECUTE $create_function$
      CREATE OR REPLACE FUNCTION public.pinyin_phrase(
        value text,
        slope integer DEFAULT NULL
      )
      RETURNS text
      LANGUAGE plpgsql
      STABLE
      PARALLEL SAFE
      AS $function$
      BEGIN
        RAISE EXCEPTION
          'public.pinyin_phrase requires CREATE EXTENSION pg_search before CREATE EXTENSION pg_pinyin';
      END;
      $function$;
    $create_function$;
  END IF;
END;
$pinyin_native_phrase$;
