#[cfg(feature = "extension")]
pgrx::pg_module_magic!();

#[cfg(feature = "extension")]
mod extension {
    use std::collections::HashMap;
    use std::fs;
    use std::mem;
    use std::process;
    use std::sync::{OnceLock, RwLock};
    use std::time::{SystemTime, UNIX_EPOCH};

    use pgrx::datum::AnyElement;
    use pgrx::prelude::*;

    const DICTIONARY_SCHEMA: &str = "pinyin";
    const EMBEDDED_MAPPING_CSV: &str = include_str!("../sql/data/pinyin_mapping.csv");
    const EMBEDDED_TOKEN_CSV: &str = include_str!("../sql/data/pinyin_token.csv");
    const EMBEDDED_WORDS_CSV: &str = include_str!("../sql/data/pinyin_words.csv");

    fn spi_json_text(query: &str) -> String {
        match Spi::get_one::<String>(query) {
            Ok(Some(value)) => value,
            Ok(None) => "{}".to_string(),
            Err(err) => error!("SPI query failed: {err}. query={query}"),
        }
    }

    fn fetch_string_map_from_query(query: &str) -> HashMap<String, String> {
        let json_text = spi_json_text(query);
        serde_json::from_str::<HashMap<String, String>>(&json_text).unwrap_or_default()
    }

    #[derive(Default)]
    struct DictionaryCache {
        version: i64,
        loaded: bool,
        char_map: HashMap<String, String>,
        word_map: HashMap<String, String>,
        max_word_len: usize,
    }

    static DICTIONARY_CACHE: OnceLock<RwLock<DictionaryCache>> = OnceLock::new();

    fn dictionary_cache() -> &'static RwLock<DictionaryCache> {
        DICTIONARY_CACHE.get_or_init(|| RwLock::new(DictionaryCache::default()))
    }

    fn sql_literal(value: &str) -> String {
        format!("'{}'", value.replace('\'', "''"))
    }

    fn parse_embedded_string_rows(csv_text: &str, csv_name: &str) -> Vec<(String, String)> {
        let mut rows = Vec::new();
        for (idx, line) in csv_text.lines().enumerate() {
            let row = line.trim_end_matches('\r');
            if row.is_empty() {
                continue;
            }
            let Some((first, second)) = row.split_once(',') else {
                error!(
                    "failed parsing embedded {csv_name} CSV at line {}: invalid two-column format",
                    idx + 1
                );
            };
            rows.push((first.to_string(), second.to_string()));
        }
        rows
    }

    fn parse_embedded_token_rows(csv_text: &str) -> Vec<(String, i16)> {
        let mut rows = Vec::new();
        for (idx, line) in csv_text.lines().enumerate() {
            let row = line.trim_end_matches('\r');
            if row.is_empty() {
                continue;
            }
            let Some((token, category_text)) = row.split_once(',') else {
                error!(
                    "failed parsing embedded token CSV at line {}: invalid two-column format",
                    idx + 1
                );
            };
            let category: i16 = match category_text.parse() {
                Ok(v) => v,
                Err(_) => error!("invalid token category in embedded CSV: {category_text}"),
            };
            rows.push((token.to_string(), category));
        }
        rows
    }

    fn bulk_insert_string_rows(
        table: &str,
        key_col: &str,
        value_col: &str,
        rows: &[(String, String)],
    ) {
        for chunk in rows.chunks(400) {
            let mut values = String::new();
            for (idx, (key, value)) in chunk.iter().enumerate() {
                if idx > 0 {
                    values.push(',');
                }
                values.push_str(&format!("({}, {})", sql_literal(key), sql_literal(value)));
            }
            let sql = format!(
                "INSERT INTO {table} ({key_col}, {value_col}) VALUES {values} \
                 ON CONFLICT ({key_col}) DO UPDATE SET {value_col} = EXCLUDED.{value_col}"
            );
            if let Err(err) = Spi::run(&sql) {
                error!("failed bulk insert into {table}: {err}");
            }
        }
    }

    fn bulk_insert_token_rows(table: &str, rows: &[(String, i16)]) {
        for chunk in rows.chunks(400) {
            let mut values = String::new();
            for (idx, (token, category)) in chunk.iter().enumerate() {
                if idx > 0 {
                    values.push(',');
                }
                values.push_str(&format!("({}, {})", sql_literal(token), category));
            }
            let sql = format!(
                "INSERT INTO {table} (character, category) VALUES {values} \
                 ON CONFLICT (character) DO UPDATE SET category = EXCLUDED.category"
            );
            if let Err(err) = Spi::run(&sql) {
                error!("failed bulk insert into {table}: {err}");
            }
        }
    }

    fn write_temp_csv(prefix: &str, csv_text: &str) -> String {
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        let path = format!("/tmp/pg_pinyin_{prefix}_{}_{}.csv", process::id(), now);
        if let Err(err) = fs::write(&path, csv_text) {
            error!("failed writing temp CSV file {path}: {err}");
        }
        path
    }

    fn try_copy_csv_to_table(
        table: &str,
        columns: &str,
        prefix: &str,
        csv_text: &str,
    ) -> Result<(), String> {
        let path = write_temp_csv(prefix, csv_text);
        let sql = format!(
            "COPY {table} ({columns}) FROM {} WITH (FORMAT csv, HEADER false)",
            sql_literal(&path)
        );
        let result = Spi::run(&sql).map_err(|err| err.to_string());
        let _ = fs::remove_file(&path);
        result
    }

    fn seed_embedded_dictionary_data() {
        let truncate_sql = format!(
            "TRUNCATE TABLE {s}.pinyin_mapping; \
             TRUNCATE TABLE {s}.pinyin_token; \
             TRUNCATE TABLE {s}.pinyin_words;",
            s = DICTIONARY_SCHEMA
        );
        if let Err(err) = Spi::run(&truncate_sql) {
            error!("failed truncating dictionary tables before seed: {err}");
        }

        let copy_attempt = try_copy_csv_to_table(
            &format!("{DICTIONARY_SCHEMA}.pinyin_mapping"),
            "character, pinyin",
            "mapping",
            EMBEDDED_MAPPING_CSV,
        )
        .and_then(|_| {
            try_copy_csv_to_table(
                &format!("{DICTIONARY_SCHEMA}.pinyin_token"),
                "character, category",
                "token",
                EMBEDDED_TOKEN_CSV,
            )
        })
        .and_then(|_| {
            try_copy_csv_to_table(
                &format!("{DICTIONARY_SCHEMA}.pinyin_words"),
                "word, pinyin",
                "words",
                EMBEDDED_WORDS_CSV,
            )
        });

        if let Err(copy_err) = copy_attempt {
            let mapping_rows = parse_embedded_string_rows(EMBEDDED_MAPPING_CSV, "mapping");
            let token_rows = parse_embedded_token_rows(EMBEDDED_TOKEN_CSV);
            let word_rows = parse_embedded_string_rows(EMBEDDED_WORDS_CSV, "word");

            if let Err(err) = Spi::run(&truncate_sql) {
                error!("failed truncating dictionary tables for fallback insert: {err}");
            }

            bulk_insert_string_rows(
                &format!("{DICTIONARY_SCHEMA}.pinyin_mapping"),
                "character",
                "pinyin",
                &mapping_rows,
            );
            bulk_insert_token_rows(&format!("{DICTIONARY_SCHEMA}.pinyin_token"), &token_rows);
            bulk_insert_string_rows(
                &format!("{DICTIONARY_SCHEMA}.pinyin_words"),
                "word",
                "pinyin",
                &word_rows,
            );

            warning!("COPY-based dictionary seed failed, fallback to INSERT: {copy_err}");
        }

        let space_sql = format!(
            "INSERT INTO {s}.pinyin_mapping (character, pinyin) VALUES (' ', ' ') \
             ON CONFLICT (character) DO NOTHING",
            s = DICTIONARY_SCHEMA
        );
        if let Err(err) = Spi::run(&space_sql) {
            error!("failed ensuring space mapping row: {err}");
        }
    }

    fn fetch_dictionary_version() -> i64 {
        let sql = format!(
            "SELECT COALESCE((SELECT version FROM {s}.pinyin_dictionary_meta WHERE singleton), 0)",
            s = DICTIONARY_SCHEMA
        );
        match Spi::get_one::<i64>(&sql) {
            Ok(Some(version)) => version,
            Ok(None) => 0,
            Err(_) => 0,
        }
    }

    fn load_dictionary_snapshot(version: i64) -> DictionaryCache {
        let char_map = fetch_string_map_from_query(&format!(
            "SELECT coalesce(jsonb_object_agg(character, pinyin), '{{}}'::jsonb)::text \
             FROM {s}.pinyin_mapping",
            s = DICTIONARY_SCHEMA
        ));

        let word_map = fetch_string_map_from_query(&format!(
            "SELECT coalesce(jsonb_object_agg(word, pinyin), '{{}}'::jsonb)::text \
             FROM {s}.pinyin_words",
            s = DICTIONARY_SCHEMA
        ));

        let max_word_len = word_map
            .keys()
            .map(|word| word.chars().count())
            .max()
            .unwrap_or(0);

        DictionaryCache {
            version,
            loaded: true,
            char_map,
            word_map,
            max_word_len,
        }
    }

    fn with_dictionary_cache<R>(f: impl FnOnce(&DictionaryCache) -> R) -> R {
        let version = fetch_dictionary_version();
        let lock = dictionary_cache();

        {
            let cache = lock.read().expect("dictionary cache read lock poisoned");
            if cache.loaded && cache.version == version {
                return f(&cache);
            }
        }

        let snapshot = load_dictionary_snapshot(version);

        {
            let mut cache = lock.write().expect("dictionary cache write lock poisoned");
            if !cache.loaded || cache.version != version {
                *cache = snapshot;
            }
            f(&cache)
        }
    }

    #[derive(Clone, Copy, Debug, Eq, PartialEq)]
    enum PieceKind {
        AsciiRun,
        Space,
        Other,
    }

    #[derive(Clone, Debug)]
    struct Piece {
        value: String,
        kind: PieceKind,
    }

    fn split_input(input: &str) -> Vec<Piece> {
        let mut pieces = Vec::new();
        let mut ascii_run = String::new();

        for ch in input.chars() {
            if ch.is_ascii_alphanumeric() {
                ascii_run.push(ch);
                continue;
            }

            if !ascii_run.is_empty() {
                pieces.push(Piece {
                    value: mem::take(&mut ascii_run),
                    kind: PieceKind::AsciiRun,
                });
            }

            pieces.push(Piece {
                value: ch.to_string(),
                kind: if ch.is_whitespace() {
                    PieceKind::Space
                } else {
                    PieceKind::Other
                },
            });
        }

        if !ascii_run.is_empty() {
            pieces.push(Piece {
                value: ascii_run,
                kind: PieceKind::AsciiRun,
            });
        }

        pieces
    }

    fn normalize_first_pinyin(raw: &str) -> String {
        let mut first = String::new();

        for part in raw.split('|') {
            if !part.is_empty() {
                first = part.to_ascii_lowercase();
                break;
            }
        }

        if first.is_empty() {
            raw.to_ascii_lowercase()
        } else {
            first
        }
    }

    fn normalize_pinyin_phrase(raw: &str) -> String {
        let mut out = Vec::new();
        for part in raw.split_whitespace() {
            let token = normalize_first_pinyin(part);
            if !token.is_empty() {
                out.push(token);
            }
        }

        if out.is_empty() {
            normalize_first_pinyin(raw)
        } else {
            out.join(" ")
        }
    }

    fn is_han_token(token: &str) -> bool {
        if token.is_empty() {
            return false;
        }

        let mut chars = token.chars();
        match (chars.next(), chars.next()) {
            (Some(ch), None) => !ch.is_whitespace() && !ch.is_ascii_alphanumeric(),
            _ => false,
        }
    }

    fn is_han_char(ch: char) -> bool {
        !ch.is_whitespace() && !ch.is_ascii_alphanumeric()
    }

    fn is_han_phrase(token: &str) -> bool {
        !token.is_empty() && token.chars().all(is_han_char)
    }

    fn normalize_plain_text(origin: &str) -> String {
        let pieces = split_input(origin);
        with_dictionary_cache(|cache| {
            let mut out = String::new();
            let mut last_is_space = true;

            for piece in pieces {
                match piece.kind {
                    PieceKind::AsciiRun => {
                        out.push_str(&piece.value.to_ascii_lowercase());
                        last_is_space = false;
                    }
                    PieceKind::Space => {
                        if !last_is_space {
                            out.push(' ');
                            last_is_space = true;
                        }
                    }
                    PieceKind::Other => {
                        if cache.char_map.contains_key(&piece.value) {
                            out.push_str(&piece.value);
                            last_is_space = false;
                        } else if !last_is_space {
                            out.push(' ');
                            last_is_space = true;
                        }
                    }
                }
            }

            out.trim().to_string()
        })
    }

    fn tokenize_plain(normalized: &str) -> Vec<String> {
        if normalized.is_empty() {
            return Vec::new();
        }

        let mut tokens = Vec::new();
        let mut ascii_run = String::new();

        for ch in normalized.chars() {
            if ch.is_ascii_alphanumeric() {
                ascii_run.push(ch.to_ascii_lowercase());
                continue;
            }

            if !ascii_run.is_empty() {
                tokens.push(mem::take(&mut ascii_run));
            }

            if ch.is_whitespace() {
                continue;
            }

            tokens.push(ch.to_string());
        }

        if !ascii_run.is_empty() {
            tokens.push(ascii_run);
        }

        tokens
    }

    fn normalize_token_list(json_text: String) -> Vec<String> {
        match serde_json::from_str::<Vec<String>>(&json_text) {
            Ok(tokens) => tokens
                .into_iter()
                .map(|token| token.trim().to_string())
                .filter(|token| !token.is_empty())
                .map(|token| {
                    if token.chars().all(|ch| ch.is_ascii_alphanumeric()) {
                        token.to_ascii_lowercase()
                    } else {
                        token
                    }
                })
                .collect(),
            Err(err) => error!("failed to parse tokenizer result as JSON array: {err}"),
        }
    }

    fn fetch_tokenizer_input_tokens(tokenizer_input: AnyElement) -> Option<Vec<String>> {
        let args =
            [unsafe { pgrx::datum::DatumWithOid::new(tokenizer_input, tokenizer_input.oid()) }];
        let json_text = match Spi::get_one_with_args::<String>(
            "SELECT COALESCE(jsonb_agg(token ORDER BY ord), '[]'::jsonb)::text \
             FROM unnest($1::text[]) WITH ORDINALITY AS t(token, ord)",
            &args,
        ) {
            Ok(Some(value)) => value,
            Ok(None) => "[]".to_string(),
            Err(_) => return None,
        };

        Some(normalize_token_list(json_text))
    }

    fn anyelement_to_text(tokenizer_input: AnyElement) -> Option<String> {
        let args =
            [unsafe { pgrx::datum::DatumWithOid::new(tokenizer_input, tokenizer_input.oid()) }];
        Spi::get_one_with_args::<String>("SELECT $1::text", &args)
            .ok()
            .flatten()
    }

    fn map_token(token: &str, char_map: &HashMap<String, String>) -> String {
        if token.chars().all(|ch| ch.is_ascii_alphanumeric()) {
            return token.to_ascii_lowercase();
        }

        if let Some(mapped) = char_map.get(token) {
            normalize_pinyin_phrase(mapped)
        } else {
            token.to_string()
        }
    }

    fn pinyin_char_normalize_impl(origin: &str) -> String {
        let normalized = normalize_plain_text(origin);
        let tokens = tokenize_plain(&normalized);

        if tokens.is_empty() {
            return String::new();
        }

        with_dictionary_cache(|cache| {
            let mut out = Vec::with_capacity(tokens.len());
            for token in tokens {
                out.push(map_token(&token, &cache.char_map));
            }
            out.join(" ")
        })
    }

    fn map_word_fallback(token: &str, char_map: &HashMap<String, String>) -> String {
        if token.chars().all(|ch| ch.is_ascii_alphanumeric()) {
            return token.to_ascii_lowercase();
        }

        if token.chars().count() == 1 {
            return map_token(token, char_map);
        }

        if !is_han_phrase(token) {
            return token.to_string();
        }

        let mut parts = Vec::new();
        for ch in token.chars() {
            parts.push(map_token(&ch.to_string(), char_map));
        }
        parts.join(" ")
    }

    fn normalize_word_tokens(mut tokens: Vec<String>) -> String {
        tokens.retain(|token| !token.is_empty());
        if tokens.is_empty() {
            return String::new();
        }

        with_dictionary_cache(|cache| {
            let mut out = Vec::with_capacity(tokens.len());
            let mut idx = 0usize;

            while idx < tokens.len() {
                if let Some(mapped) = cache.word_map.get(&tokens[idx]) {
                    out.push(normalize_pinyin_phrase(mapped));
                    idx += 1;
                    continue;
                }

                if cache.max_word_len >= 2 && is_han_token(&tokens[idx]) {
                    let mut candidate = String::new();
                    let max_end = usize::min(tokens.len(), idx + cache.max_word_len);
                    let mut best: Option<(usize, String)> = None;

                    for end in idx..max_end {
                        if !is_han_token(&tokens[end]) {
                            break;
                        }

                        candidate.push_str(&tokens[end]);
                        let span = end - idx + 1;

                        if span >= 2 {
                            if let Some(mapped) = cache.word_map.get(&candidate) {
                                best = Some((span, normalize_pinyin_phrase(mapped)));
                            }
                        }
                    }

                    if let Some((span, mapped)) = best {
                        out.push(mapped);
                        idx += span;
                        continue;
                    }
                }

                out.push(map_word_fallback(&tokens[idx], &cache.char_map));
                idx += 1;
            }

            out.join(" ")
        })
    }

    fn pinyin_word_normalize_impl(origin: &str) -> String {
        let normalized = normalize_plain_text(origin);
        let tokens = tokenize_plain(&normalized);
        normalize_word_tokens(tokens)
    }

    fn pinyin_word_normalize_tokenizer_impl(tokenizer_input: AnyElement) -> String {
        if let Some(tokens) = fetch_tokenizer_input_tokens(tokenizer_input) {
            return normalize_word_tokens(tokens);
        }

        match anyelement_to_text(tokenizer_input) {
            Some(text) => pinyin_word_normalize_impl(&text),
            None => error!("tokenizer input must be castable to text[] or text"),
        }
    }

    #[pg_extern(immutable, strict, parallel_safe)]
    fn pinyin_char_normalize(origin: &str) -> String {
        pinyin_char_normalize_impl(origin)
    }

    #[pg_extern(immutable, strict, parallel_safe)]
    fn pinyin_word_normalize(origin: &str) -> String {
        pinyin_word_normalize_impl(origin)
    }

    #[pg_extern(immutable, strict, parallel_safe, name = "pinyin_word_normalize")]
    fn pinyin_word_normalize_with_tokenizer(tokenizer_input: AnyElement) -> String {
        pinyin_word_normalize_tokenizer_impl(tokenizer_input)
    }

    #[pg_extern(volatile, parallel_unsafe, name = "pinyin__seed_embedded_data")]
    fn pinyin_seed_embedded_data_internal() -> bool {
        seed_embedded_dictionary_data();
        true
    }

    extension_sql!(
        r#"
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
        "#,
        name = "pinyin_dictionary_tables",
        requires = [pinyin_seed_embedded_data_internal],
        bootstrap
    );

    extension_sql!(
        r#"
        SELECT public.pinyin__seed_embedded_data();
        "#,
        name = "pinyin_dictionary_seed",
        requires = [pinyin_seed_embedded_data_internal]
    );

    #[cfg(any(test, feature = "pg_test"))]
    #[pg_schema]
    mod tests {
        use pgrx::prelude::*;

        fn seed_minimal_data() {
            Spi::run(
                "TRUNCATE TABLE pinyin.pinyin_mapping; \
                 TRUNCATE TABLE pinyin.pinyin_token; \
                 TRUNCATE TABLE pinyin.pinyin_words;",
            )
            .expect("failed to truncate dictionary tables");

            Spi::run(
                "INSERT INTO pinyin.pinyin_mapping (character, pinyin) VALUES
                   (' ', ' '),
                   ('我', '|wo|'),
                   ('们', '|men|'),
                   ('重', '|tong|zhong|chong|'),
                   ('起', '|qi|'),
                   ('郑', '|zheng|'),
                   ('爽', '|shuang|');",
            )
            .expect("failed to seed pinyin_mapping");

            Spi::run(
                "INSERT INTO pinyin.pinyin_words (word, pinyin) VALUES
                   ('郑爽', '|zheng| |shuang|')
                 ON CONFLICT (word) DO UPDATE SET pinyin = EXCLUDED.pinyin;",
            )
            .expect("failed to seed pinyin_words");
        }

        #[pg_test]
        fn test_pinyin_char_normalize() {
            seed_minimal_data();

            let converted =
                Spi::get_one::<String>("SELECT public.pinyin_char_normalize('我ABC们123')")
                    .expect("SPI failed")
                    .expect("no row returned");
            assert_eq!(converted, "wo abc men 123");
        }

        #[pg_test]
        fn test_pinyin_word_normalize() {
            seed_minimal_data();

            let converted =
                Spi::get_one::<String>("SELECT public.pinyin_word_normalize('郑爽ABC')")
                    .expect("SPI failed")
                    .expect("no row returned");
            assert_eq!(converted, "zheng shuang abc");
        }

        #[pg_test]
        fn test_pinyin_word_normalize_with_tokenizer_passthrough() {
            seed_minimal_data();

            let converted = Spi::get_one::<String>(
                "SELECT public.pinyin_word_normalize(ARRAY['郑爽', 'ABC']::text[])",
            )
            .expect("SPI failed")
            .expect("no row returned");
            assert_eq!(converted, "zheng shuang abc");
        }

        #[pg_test]
        fn test_dictionary_update_reflected() {
            seed_minimal_data();

            let before = Spi::get_one::<String>("SELECT public.pinyin_char_normalize('我')")
                .expect("SPI failed")
                .expect("no row returned");
            assert_eq!(before, "wo");

            Spi::run("UPDATE pinyin.pinyin_mapping SET pinyin='|wo2|' WHERE character='我'")
                .expect("failed to update mapping");

            let after = Spi::get_one::<String>("SELECT public.pinyin_char_normalize('我')")
                .expect("SPI failed")
                .expect("no row returned");
            assert_eq!(after, "wo2");
        }

        #[pg_test]
        fn test_polyphone_first_reading() {
            seed_minimal_data();

            let converted = Spi::get_one::<String>("SELECT public.pinyin_char_normalize('重起')")
                .expect("SPI failed")
                .expect("no row returned");
            assert_eq!(converted, "tong qi");
        }

        #[pg_test]
        fn test_normalize_functions_are_immutable() {
            let char_volatile = Spi::get_one::<String>(
                "SELECT p.provolatile::text
                 FROM pg_proc AS p
                 WHERE p.oid = 'public.pinyin_char_normalize(text)'::regprocedure",
            )
            .expect("SPI failed")
            .expect("no row returned");
            assert_eq!(char_volatile, "i");

            let word_volatile = Spi::get_one::<String>(
                "SELECT p.provolatile::text
                 FROM pg_proc AS p
                 WHERE p.oid = 'public.pinyin_word_normalize(text)'::regprocedure",
            )
            .expect("SPI failed")
            .expect("no row returned");
            assert_eq!(word_volatile, "i");

            let word_tokenizer_volatile = Spi::get_one::<String>(
                "SELECT p.provolatile::text
                 FROM pg_proc AS p
                 WHERE p.oid = 'public.pinyin_word_normalize(anyelement)'::regprocedure",
            )
            .expect("SPI failed")
            .expect("no row returned");
            assert_eq!(word_tokenizer_volatile, "i");
        }

        #[pg_test]
        fn test_generated_column_usage_raw_sql() {
            seed_minimal_data();

            Spi::run(
                "CREATE TEMP TABLE pinyin_generated_demo (
                   id bigserial PRIMARY KEY,
                   description text NOT NULL,
                   pinyin text GENERATED ALWAYS AS (public.pinyin_char_normalize(description)) STORED
                 )",
            )
            .expect("failed to create generated column demo table");

            Spi::run("INSERT INTO pinyin_generated_demo (description) VALUES ('郑爽ABC')")
                .expect("failed to insert demo row");

            let pinyin = Spi::get_one::<String>(
                "SELECT pinyin FROM pinyin_generated_demo WHERE description = '郑爽ABC'",
            )
            .expect("SPI failed")
            .expect("no row returned");
            assert_eq!(pinyin, "zheng shuang abc");
        }
    }
}

#[cfg(all(feature = "extension", any(test, feature = "pg_test")))]
pub mod pg_test {
    pub fn setup(_options: Vec<&str>) {}

    pub fn postgresql_conf_options() -> Vec<&'static str> {
        vec![]
    }
}
