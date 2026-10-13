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
      BEGIN
        patterns := public.pinyin_regex_phrase_patterns_mode(value, mode, generated_pinyin);
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
