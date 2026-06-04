-- Upgrade pg_pinyin from 0.0.2 to 0.0.3.

CREATE OR REPLACE FUNCTION public.pinyin_regex_phrase_patterns(
  value text
)
RETURNS text[]
LANGUAGE c
STABLE
STRICT
PARALLEL SAFE
AS 'MODULE_PATHNAME', 'pinyin_regex_phrase_patterns_default_wrapper';

CREATE OR REPLACE FUNCTION public.pinyin_regex_phrase_patterns(
  value text,
  generated_pinyin boolean
)
RETURNS text[]
LANGUAGE c
STABLE
STRICT
PARALLEL SAFE
AS 'MODULE_PATHNAME', 'pinyin_regex_phrase_patterns_with_generated_wrapper';

DROP FUNCTION IF EXISTS public.pinyin_regex_phrase(text);
DROP FUNCTION IF EXISTS public.pinyin_regex_phrase(text, boolean);
DROP FUNCTION IF EXISTS public.pinyin_regex_phrase(text, integer, integer, boolean);

DO $pinyin_regex_phrase$
BEGIN
  IF to_regtype('pdb.query') IS NOT NULL THEN
    EXECUTE $create_function$
      CREATE OR REPLACE FUNCTION public.pinyin_regex_phrase(
        value text,
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
        patterns := public.pinyin_regex_phrase_patterns(value, generated_pinyin);

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
      CREATE OR REPLACE FUNCTION public.pinyin_regex_phrase(
        value text,
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
          'public.pinyin_regex_phrase requires CREATE EXTENSION pg_search before CREATE EXTENSION pg_pinyin';
      END;
      $function$;
    $create_function$;
  END IF;
END;
$pinyin_regex_phrase$;
