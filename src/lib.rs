#[cfg(feature = "extension")]
pgrx::pg_module_magic!();

pub mod query_modes;
pub mod regex_phrase;
pub mod romanization;

#[cfg(feature = "extension")]
mod extension {
    use crate::query_modes::{self, PinyinQueryDictionary};
    use crate::regex_phrase::{self, RegexTokenDictionary};

    use crate::romanization::{self, WordIndex};
    use std::cell::Cell;
    use std::collections::HashMap;
    use std::fs;
    use std::process;
    use std::sync::{Arc, OnceLock, RwLock};
    use std::time::{SystemTime, UNIX_EPOCH};

    use pgrx::datum::AnyElement;
    use pgrx::prelude::*;

    const DICTIONARY_SCHEMA: &str = "pinyin";
    const EMBEDDED_MAPPING_CSV: &str = include_str!("../sql/data/pinyin_mapping.csv");
    const EMBEDDED_TOKEN_CSV: &str = include_str!("../sql/data/pinyin_token.csv");
    const EMBEDDED_WORDS_CSV: &str = include_str!("../sql/data/pinyin_words.csv");

    #[derive(Clone, Copy, Default, PartialEq, Eq)]
    struct DictionaryVersion {
        counter: i64,
        metadata_xmin: i64,
    }

    #[derive(Clone, Copy, PartialEq, Eq)]
    struct SnapshotKey {
        address: usize,
        xmin: pg_sys::TransactionId,
        xmax: pg_sys::TransactionId,
        completions: u64,
        command: pg_sys::CommandId,
        current_command: pg_sys::CommandId,
        subtransaction: pg_sys::SubTransactionId,
    }

    thread_local! {
        static VERSION_SNAPSHOT: Cell<Option<(SnapshotKey, DictionaryVersion)>> = const { Cell::new(None) };
    }
    static CACHE_CALLBACKS: OnceLock<()> = OnceLock::new();

    #[pg_guard]
    unsafe extern "C-unwind" fn reset_version_snapshot(
        _event: pg_sys::XactEvent::Type,
        _arg: *mut std::ffi::c_void,
    ) {
        VERSION_SNAPSHOT.with(|cache| cache.set(None));
    }

    #[pg_guard]
    unsafe extern "C-unwind" fn reset_subtransaction_snapshot(
        _event: pg_sys::SubXactEvent::Type,
        _subtransaction: pg_sys::SubTransactionId,
        _parent: pg_sys::SubTransactionId,
        _arg: *mut std::ffi::c_void,
    ) {
        VERSION_SNAPSHOT.with(|cache| cache.set(None));
    }

    fn snapshot_key() -> Option<SnapshotKey> {
        CACHE_CALLBACKS.get_or_init(|| unsafe {
            pg_sys::RegisterXactCallback(Some(reset_version_snapshot), std::ptr::null_mut());
            pg_sys::RegisterSubXactCallback(
                Some(reset_subtransaction_snapshot),
                std::ptr::null_mut(),
            );
        });
        // Read-only SPI uses the caller's active MVCC snapshot. Do not allocate
        // an XID; workers and read-only transactions may not have one.
        unsafe {
            if !pg_sys::ActiveSnapshotSet() {
                return None;
            }
            let snapshot = pg_sys::GetActiveSnapshot();
            if (*snapshot).snapshot_type != pg_sys::SnapshotType::SNAPSHOT_MVCC
                || (*snapshot).takenDuringRecovery
            {
                return None;
            }
            Some(SnapshotKey {
                address: snapshot as usize,
                xmin: (*snapshot).xmin,
                xmax: (*snapshot).xmax,
                completions: (*snapshot).snapXactCompletionCount,
                command: (*snapshot).curcid,
                current_command: pg_sys::GetCurrentCommandId(false),
                subtransaction: pg_sys::GetCurrentSubTransactionId(),
            })
        }
    }

    #[derive(Default)]
    struct CharDictionaryCache {
        version: DictionaryVersion,
        loaded: bool,
        char_map: Arc<HashMap<String, String>>,
    }

    #[derive(Default)]
    struct DictionaryCache {
        version: DictionaryVersion,
        loaded: bool,
        char_map: Arc<HashMap<String, String>>,
        word_map: Arc<HashMap<String, String>>,
        word_index: Arc<WordIndex>,
    }

    #[derive(Default)]
    struct SuffixDictionaryCacheEntry {
        base_version: DictionaryVersion,
        relations: (u32, u32),
        char_map: Arc<HashMap<String, String>>,
        words_loaded: bool,
        word_map: Arc<HashMap<String, String>>,
        word_index: Arc<WordIndex>,
    }

    static CHAR_DICTIONARY_CACHE: OnceLock<RwLock<CharDictionaryCache>> = OnceLock::new();
    static DICTIONARY_CACHE: OnceLock<RwLock<DictionaryCache>> = OnceLock::new();
    static REGEX_TOKEN_DICTIONARY: OnceLock<RegexTokenDictionary> = OnceLock::new();
    static QUERY_TOKEN_DICTIONARY: OnceLock<PinyinQueryDictionary> = OnceLock::new();
    static SUFFIX_DICTIONARY_CACHE: OnceLock<RwLock<HashMap<String, SuffixDictionaryCacheEntry>>> =
        OnceLock::new();

    fn char_dictionary_cache() -> &'static RwLock<CharDictionaryCache> {
        CHAR_DICTIONARY_CACHE.get_or_init(|| RwLock::new(CharDictionaryCache::default()))
    }

    fn dictionary_cache() -> &'static RwLock<DictionaryCache> {
        DICTIONARY_CACHE.get_or_init(|| RwLock::new(DictionaryCache::default()))
    }

    fn regex_token_dictionary() -> &'static RegexTokenDictionary {
        REGEX_TOKEN_DICTIONARY.get_or_init(|| {
            RegexTokenDictionary::from_tokens(regex_phrase::tokens_from_pinyin_token_csv(
                EMBEDDED_TOKEN_CSV,
            ))
        })
    }

    fn query_token_dictionary() -> &'static PinyinQueryDictionary {
        QUERY_TOKEN_DICTIONARY
            .get_or_init(|| PinyinQueryDictionary::from_token_csv(EMBEDDED_TOKEN_CSV))
    }

    fn suffix_dictionary_cache() -> &'static RwLock<HashMap<String, SuffixDictionaryCacheEntry>> {
        SUFFIX_DICTIONARY_CACHE.get_or_init(|| RwLock::new(HashMap::new()))
    }

    fn sql_literal(value: &str) -> String {
        format!("'{}'", value.replace('\'', "''"))
    }

    fn fetch_string_map_from_table(
        table: &str,
        key_col: &str,
        value_col: &str,
    ) -> HashMap<String, String> {
        let query = format!(
            "SELECT {key_col}, {value_col} FROM {schema}.{table}",
            key_col = key_col,
            value_col = value_col,
            schema = DICTIONARY_SCHEMA,
            table = table,
        );

        Spi::connect(|client| {
            let rows = match client.select(&query, None, &[]) {
                Ok(rows) => rows,
                Err(err) => error!("SPI query failed: {err}. query={query}"),
            };

            let mut out = HashMap::with_capacity(rows.len());
            for row in rows {
                let key = match row[key_col].value::<String>() {
                    Ok(Some(v)) => v,
                    Ok(None) => continue,
                    Err(err) => error!("SPI row parse failed for {table}.{key_col}: {err}"),
                };
                let value = match row[value_col].value::<String>() {
                    Ok(Some(v)) => v,
                    Ok(None) => continue,
                    Err(err) => error!("SPI row parse failed for {table}.{value_col}: {err}"),
                };
                out.insert(key, value);
            }

            out
        })
    }

    fn fetch_overlayed_string_map(
        base_table: &str,
        key_col: &str,
        value_col: &str,
        overlay_table: Option<&str>,
    ) -> HashMap<String, String> {
        let mut out = fetch_string_map_from_table(base_table, key_col, value_col);
        if let Some(overlay) = overlay_table {
            let overlay_map = fetch_string_map_from_table(overlay, key_col, value_col);
            out.extend(overlay_map);
        }
        out
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

    fn write_temp_csv(prefix: &str, csv_text: &str) -> Result<String, String> {
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        let path = format!("/tmp/pg_pinyin_{prefix}_{}_{}.csv", process::id(), now);
        fs::write(&path, csv_text).map_err(|err| format!("failed writing {path}: {err}"))?;
        Ok(path)
    }

    fn try_copy_csv_to_table(
        table: &str,
        columns: &str,
        prefix: &str,
        csv_text: &str,
    ) -> Result<(), String> {
        let path = write_temp_csv(prefix, csv_text)?;
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

    fn fetch_dictionary_version() -> DictionaryVersion {
        let key = snapshot_key();
        if let Some(key) = key {
            if let Some((_, version)) = VERSION_SNAPSHOT
                .with(Cell::get)
                .filter(|(cached, _)| *cached == key)
            {
                return version;
            }
        }
        let version = read_dictionary_version();
        // Nested mutable SPI may advance the command counter even while the
        // caller keeps the same active snapshot. Cache the post-read state.
        if let Some(key) = snapshot_key() {
            VERSION_SNAPSHOT.with(|cache| cache.set(Some((key, version))));
        }
        version
    }

    fn read_dictionary_version() -> DictionaryVersion {
        let sql = format!(
            "SELECT version, xmin::text::bigint FROM {s}.pinyin_dictionary_meta WHERE singleton",
            s = DICTIONARY_SCHEMA
        );
        // Spi::get_one uses a mutable SPI call even for SELECT and assigns an
        // XID, which is forbidden in parallel workers. Keep these reads read-only.
        // A rolled-back subtransaction can reuse its counter, but not its row xmin.
        match Spi::connect(|client| {
            client
                .select(&sql, Some(1), &[])?
                .first()
                .get_two::<i64, i64>()
        }) {
            Ok((Some(counter), Some(metadata_xmin))) => DictionaryVersion {
                counter,
                metadata_xmin,
            },
            _ => DictionaryVersion::default(),
        }
    }

    fn load_char_dictionary_snapshot(version: DictionaryVersion) -> CharDictionaryCache {
        let char_map = Arc::new(fetch_string_map_from_table(
            "pinyin_mapping",
            "character",
            "pinyin",
        ));
        CharDictionaryCache {
            version,
            loaded: true,
            char_map,
        }
    }

    fn with_char_dictionary_cache<R>(f: impl FnOnce(&Arc<HashMap<String, String>>) -> R) -> R {
        let version = fetch_dictionary_version();
        let lock = char_dictionary_cache();

        {
            let cache = lock
                .read()
                .expect("char dictionary cache read lock poisoned");
            if cache.loaded && cache.version == version {
                return f(&cache.char_map);
            }
        }

        let snapshot = load_char_dictionary_snapshot(version);

        {
            let mut cache = lock
                .write()
                .expect("char dictionary cache write lock poisoned");
            if !cache.loaded || cache.version != version {
                *cache = snapshot;
            }
            f(&cache.char_map)
        }
    }

    fn load_dictionary_snapshot(version: DictionaryVersion) -> DictionaryCache {
        let char_map = with_char_dictionary_cache(Arc::clone);
        let word_map = Arc::new(fetch_string_map_from_table(
            "pinyin_words",
            "word",
            "pinyin",
        ));

        let word_index = Arc::new(WordIndex::new(&word_map));

        DictionaryCache {
            version,
            loaded: true,
            char_map,
            word_map,
            word_index,
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

    fn canonicalize_table_suffix(suffix: &str) -> Option<String> {
        let trimmed = suffix.trim();
        if trimmed.is_empty() {
            return None;
        }

        let normalized = trimmed.trim_start_matches('_');
        if normalized.is_empty() {
            error!("dictionary table suffix cannot be empty");
        }

        if !normalized
            .chars()
            .all(|ch| ch.is_ascii_alphanumeric() || ch == '_')
        {
            error!("dictionary table suffix must contain only [A-Za-z0-9_]");
        }

        if normalized.len() > 40 {
            error!("dictionary table suffix must not exceed 40 ASCII characters");
        }
        Some(format!("_{}", normalized.to_ascii_lowercase()))
    }

    fn table_exists(schema: &str, table: &str) -> bool {
        let query = format!(
            "SELECT EXISTS (
               SELECT 1
               FROM pg_catalog.pg_class AS c
               JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
               WHERE n.nspname = {schema}
                 AND c.relname = {table}
                 AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
             )",
            schema = sql_literal(schema),
            table = sql_literal(table),
        );
        match Spi::connect(|client| {
            client
                .select(&query, Some(1), &[])?
                .first()
                .get_one::<bool>()
        }) {
            Ok(Some(v)) => v,
            _ => false,
        }
    }

    fn overlay_table_name(base_name: &str, canonical_suffix: Option<&str>) -> Option<String> {
        canonical_suffix
            .map(|s| format!("{base_name}{s}"))
            .filter(|table| table_exists(DICTIONARY_SCHEMA, table))
    }

    fn load_char_map_from_canonical_suffix(
        canonical_suffix: Option<&str>,
    ) -> HashMap<String, String> {
        let overlay_mapping = overlay_table_name("pinyin_mapping", canonical_suffix);
        fetch_overlayed_string_map(
            "pinyin_mapping",
            "character",
            "pinyin",
            overlay_mapping.as_deref(),
        )
    }

    fn load_word_map_from_canonical_suffix(
        canonical_suffix: Option<&str>,
    ) -> (HashMap<String, String>, usize) {
        let overlay_words = overlay_table_name("pinyin_words", canonical_suffix);
        let word_map =
            fetch_overlayed_string_map("pinyin_words", "word", "pinyin", overlay_words.as_deref());
        let max_word_len = word_map
            .keys()
            .map(|word| word.chars().count())
            .max()
            .unwrap_or(0);
        (word_map, max_word_len)
    }

    fn clear_all_suffix_cache_impl() -> i64 {
        let lock = suffix_dictionary_cache();
        let mut cache = lock
            .write()
            .expect("suffix dictionary cache write lock poisoned");
        let cleared = cache.len() as i64;
        cache.clear();
        cleared
    }

    fn clear_suffix_cache_impl(suffix: &str) -> bool {
        let canonical_suffix = match canonicalize_table_suffix(suffix) {
            Some(value) => value,
            None => return false,
        };

        let lock = suffix_dictionary_cache();
        let mut cache = lock
            .write()
            .expect("suffix dictionary cache write lock poisoned");
        cache.remove(&canonical_suffix).is_some()
    }

    fn suffix_relations(suffix: &str) -> (u32, u32) {
        let query = format!(
            "SELECT COALESCE(to_regclass({}), 0)::oid::bigint, COALESCE(to_regclass({}), 0)::oid::bigint",
            sql_literal(&format!("pinyin.pinyin_mapping{suffix}")),
            sql_literal(&format!("pinyin.pinyin_words{suffix}"))
        );
        Spi::connect(|client| {
            client
                .select(&query, Some(1), &[])?
                .first()
                .get_two::<i64, i64>()
        })
        .map(|(a, b)| (a.unwrap_or(0) as u32, b.unwrap_or(0) as u32))
        .unwrap_or_else(|err| error!("suffix relation lookup failed: {err}"))
    }

    fn with_suffix_char_cache<R>(
        suffix: &str,
        f: impl FnOnce(&SuffixDictionaryCacheEntry) -> R,
    ) -> R {
        with_suffix_cache(suffix, false, f)
    }

    fn with_suffix_word_cache<R>(
        suffix: &str,
        f: impl FnOnce(&SuffixDictionaryCacheEntry) -> R,
    ) -> R {
        with_suffix_cache(suffix, true, f)
    }

    fn with_suffix_cache<R>(
        suffix: &str,
        words: bool,
        f: impl FnOnce(&SuffixDictionaryCacheEntry) -> R,
    ) -> R {
        let version = fetch_dictionary_version();
        let relations = suffix_relations(suffix);
        if relations == (0, 0) {
            // Missing overlays must not occupy a backend-lifetime cache entry.
            suffix_dictionary_cache()
                .write()
                .expect("suffix cache poisoned")
                .remove(suffix);
            if !words {
                return with_char_dictionary_cache(|chars| {
                    f(&SuffixDictionaryCacheEntry {
                        base_version: version,
                        char_map: Arc::clone(chars),
                        ..Default::default()
                    })
                });
            }
            return with_dictionary_cache(|base| {
                f(&SuffixDictionaryCacheEntry {
                    base_version: version,
                    relations,
                    char_map: Arc::clone(&base.char_map),
                    words_loaded: true,
                    word_map: Arc::clone(&base.word_map),
                    word_index: Arc::clone(&base.word_index),
                })
            });
        }
        let lock = suffix_dictionary_cache();
        {
            let cache = lock.read().expect("suffix cache poisoned");
            if let Some(entry) = cache.get(suffix) {
                if entry.base_version == version
                    && entry.relations == relations
                    && (!words || entry.words_loaded)
                {
                    return f(entry);
                }
            }
        }
        let previous_chars = {
            let cache = lock.read().expect("suffix cache poisoned");
            cache
                .get(suffix)
                .filter(|entry| entry.base_version == version && entry.relations == relations)
                .map(|entry| Arc::clone(&entry.char_map))
        };
        let char_map = previous_chars.unwrap_or_else(|| {
            if relations.0 == 0 {
                with_char_dictionary_cache(Arc::clone)
            } else {
                Arc::new(load_char_map_from_canonical_suffix(Some(suffix)))
            }
        });
        let (word_map, _) = if words {
            load_word_map_from_canonical_suffix(Some(suffix))
        } else {
            (HashMap::new(), 0)
        };
        let entry = SuffixDictionaryCacheEntry {
            base_version: version,
            relations,
            char_map,
            words_loaded: words,
            word_index: Arc::new(WordIndex::new(&word_map)),
            word_map: Arc::new(word_map),
        };
        let mut cache = lock.write().expect("suffix cache poisoned");
        cache.insert(suffix.to_string(), entry);
        f(cache.get(suffix).expect("inserted suffix"))
    }

    fn pinyin_regex_phrase_patterns_impl(
        value: &str,
        generated_pinyin: bool,
    ) -> Option<Vec<String>> {
        regex_phrase::pinyin_regex_phrase_patterns(
            value,
            generated_pinyin,
            regex_token_dictionary(),
        )
    }

    fn normalize_token(token: &str) -> Option<String> {
        let token = token.trim();
        if token.is_empty() {
            None
        } else if token.chars().all(|ch| ch.is_ascii_alphanumeric()) {
            Some(token.to_ascii_lowercase())
        } else {
            Some(token.to_string())
        }
    }

    fn normalized_array(tokens: Array<&str>) -> Vec<String> {
        tokens
            .iter()
            .filter_map(|token| {
                normalize_token(token.unwrap_or_else(|| {
                    error!("tokenizer text[] input must not contain NULL elements")
                }))
            })
            .collect()
    }

    unsafe fn call_tokenizer_cast(
        function: pg_sys::Oid,
        input: AnyElement,
        caller: pg_sys::FunctionCallInfo,
    ) -> Option<pg_sys::Datum> {
        unsafe {
            #[cfg(feature = "pg15")]
            let permission = pg_sys::pg_proc_aclcheck(
                function,
                pg_sys::GetUserId(),
                pg_sys::ACL_EXECUTE as pg_sys::AclMode,
            );
            #[cfg(not(feature = "pg15"))]
            let permission = pg_sys::object_aclcheck(
                pg_sys::ProcedureRelationId,
                function,
                pg_sys::GetUserId(),
                pg_sys::ACL_EXECUTE as pg_sys::AclMode,
            );
            if permission != pg_sys::AclResult::ACLCHECK_OK {
                pg_sys::aclcheck_error(
                    permission,
                    pg_sys::ObjectType::OBJECT_FUNCTION,
                    pg_sys::get_func_name(function),
                );
            }
            let expression = (*(*caller).flinfo).fn_expr;
            let argument = if !expression.is_null()
                && (*expression).type_ == pg_sys::NodeTag::T_FuncExpr
            {
                pgrx::PgList::<pg_sys::Node>::from_pg((*expression.cast::<pg_sys::FuncExpr>()).args)
                    .head()
            } else {
                None
            }
            .unwrap_or_else(|| pg_sys::makeNullConst(input.oid(), -1, pg_sys::InvalidOid).cast());
            let mut args = pgrx::PgList::<pg_sys::Node>::new();
            args.push(argument);
            let cast_expression = pg_sys::makeFuncExpr(
                function,
                pg_sys::TEXTARRAYOID,
                args.into_pg(),
                (*caller).fncollation,
                pg_sys::exprCollation(argument),
                pg_sys::CoercionForm::COERCE_EXPLICIT_CAST,
            );
            let mut info = pg_sys::FmgrInfo::default();
            pg_sys::fmgr_info(function, &mut info);
            // ParadeDB tokenizers read fn_expr to obtain the input typmod.
            // OidFunctionCall has no expression and would crash in that wrapper.
            info.fn_expr = cast_expression.cast();
            let call = pg_sys::palloc0(pg_sys::SizeForFunctionCallInfo!(1).get() as usize)
                .cast::<pg_sys::FunctionCallInfoBaseData>();
            pg_sys::InitFunctionCallInfoData!(
                *call,
                std::ptr::addr_of_mut!(info),
                1,
                (*caller).fncollation,
                std::ptr::null_mut::<pg_sys::Node>(),
                std::ptr::null_mut::<pg_sys::Node>()
            );
            std::ptr::addr_of_mut!((*call).args)
                .cast::<pg_sys::NullableDatum>()
                .write(pg_sys::NullableDatum {
                    value: input.datum(),
                    isnull: false,
                });
            let result = pg_sys::ffi::pg_guard_ffi_boundary(|| info.fn_addr.unwrap()(call));
            let result = (!(*call).isnull).then_some(result);
            pg_sys::pfree(call.cast());
            result
        }
    }

    fn fetch_tokenizer_input_tokens(
        input: AnyElement,
        fcinfo: pg_sys::FunctionCallInfo,
    ) -> Option<Vec<String>> {
        let source = unsafe { pg_sys::getBaseType(input.oid()) };
        if source == pg_sys::TEXTARRAYOID {
            return Some(normalized_array(
                input
                    .value::<Array<&str>>()
                    .unwrap_or_else(|err| error!("invalid tokenizer array: {err}"))?,
            ));
        }
        let mut function = pg_sys::InvalidOid;
        // Resolve each call so ALTER/DROP CAST cannot leave a stale function OID.
        let path = unsafe {
            pg_sys::find_coercion_pathway(
                pg_sys::TEXTARRAYOID,
                source,
                pg_sys::CoercionContext::COERCION_EXPLICIT,
                &mut function,
            )
        };
        let datum = if path == pg_sys::CoercionPathType::COERCION_PATH_RELABELTYPE {
            Some(input.datum())
        } else if path == pg_sys::CoercionPathType::COERCION_PATH_FUNC
            && unsafe { pg_sys::get_func_nargs(function) } == 1
            && unsafe { pg_sys::func_volatile(function) } as u8 == b'i'
            && unsafe { pg_sys::func_parallel(function) } as u8 == b's'
        {
            unsafe { call_tokenizer_cast(function, input, fcinfo) }
        } else {
            None
        };
        if let Some(datum) = datum {
            // The catalog coercion established TEXTARRAYOID. Decode in this call's memory context.
            return unsafe { Array::<&str>::from_datum(datum, false) }.map(normalized_array);
        }
        let args = [unsafe { pgrx::datum::DatumWithOid::new(input, input.oid()) }];
        let tokens = Spi::connect(|client| {
            client
                .select("SELECT $1::text[]", Some(1), &args)?
                .first()
                .get_one::<Vec<Option<String>>>()
        })
        .ok()
        .flatten()?;
        Some(
            tokens
                .into_iter()
                .filter_map(|token| {
                    normalize_token(&token.unwrap_or_else(|| {
                        error!("tokenizer text[] input must not contain NULL elements")
                    }))
                })
                .collect(),
        )
    }

    fn anyelement_to_text(tokenizer_input: AnyElement) -> Option<String> {
        let args =
            [unsafe { pgrx::datum::DatumWithOid::new(tokenizer_input, tokenizer_input.oid()) }];
        Spi::connect(|client| {
            client
                .select("SELECT $1::text", Some(1), &args)?
                .first()
                .get_one::<String>()
        })
        .ok()
        .flatten()
    }

    fn pinyin_char_romanize_with_char_map(
        origin: &str,
        char_map: &HashMap<String, String>,
    ) -> String {
        romanization::romanize_chars(origin, char_map)
    }

    fn pinyin_char_romanize_impl(origin: &str) -> String {
        with_char_dictionary_cache(|chars| pinyin_char_romanize_with_char_map(origin, chars))
    }

    fn pinyin_char_romanize_with_suffix_impl(origin: &str, suffix: &str) -> String {
        match canonicalize_table_suffix(suffix) {
            Some(suffix) => with_suffix_char_cache(&suffix, |entry| {
                pinyin_char_romanize_with_char_map(origin, &entry.char_map)
            }),
            None => pinyin_char_romanize_impl(origin),
        }
    }

    fn romanize_word_tokens_with_maps(
        tokens: Vec<String>,
        char_map: &HashMap<String, String>,
        word_map: &HashMap<String, String>,
        word_index: &WordIndex,
    ) -> String {
        romanization::romanize(
            tokens.iter().map(String::as_str),
            char_map,
            word_map,
            word_index,
        )
    }

    fn romanize_word_tokens(tokens: Vec<String>) -> String {
        with_dictionary_cache(|cache| {
            romanize_word_tokens_with_maps(
                tokens,
                &cache.char_map,
                &cache.word_map,
                &cache.word_index,
            )
        })
    }

    fn pinyin_word_romanize_with_maps(
        origin: &str,
        char_map: &HashMap<String, String>,
        word_map: &HashMap<String, String>,
        word_index: &WordIndex,
    ) -> String {
        romanization::romanize([origin], char_map, word_map, word_index)
    }

    fn pinyin_word_romanize_impl(origin: &str) -> String {
        with_dictionary_cache(|cache| {
            pinyin_word_romanize_with_maps(
                origin,
                &cache.char_map,
                &cache.word_map,
                &cache.word_index,
            )
        })
    }

    fn pinyin_word_romanize_with_suffix_impl(origin: &str, suffix: &str) -> String {
        match canonicalize_table_suffix(suffix) {
            Some(canonical_suffix) => with_suffix_word_cache(&canonical_suffix, |entry| {
                pinyin_word_romanize_with_maps(
                    origin,
                    &entry.char_map,
                    &entry.word_map,
                    &entry.word_index,
                )
            }),
            None => with_dictionary_cache(|cache| {
                pinyin_word_romanize_with_maps(
                    origin,
                    &cache.char_map,
                    &cache.word_map,
                    &cache.word_index,
                )
            }),
        }
    }

    fn romanize_document_batch_with_maps(
        origins: &Array<&str>,
        char_map: &HashMap<String, String>,
        word_map: &HashMap<String, String>,
        word_index: &WordIndex,
    ) -> Vec<Option<String>> {
        origins
            .iter()
            .map(|origin| {
                origin.map(|origin| {
                    pinyin_word_romanize_with_maps(origin, char_map, word_map, word_index)
                })
            })
            .collect()
    }

    fn empty_document_batch(origins: &Array<&str>) -> Option<Vec<Option<String>>> {
        if origins.iter().all(|origin| origin.is_none()) {
            Some(vec![None; origins.len()])
        } else {
            None
        }
    }

    #[pg_extern(stable, strict, parallel_safe)]
    fn pinyin_word_romanize_batch(origins: Array<&str>) -> Vec<Option<String>> {
        if let Some(empty) = empty_document_batch(&origins) {
            return empty;
        }
        // One version check/cache snapshot per explicit batch, never per transaction.
        with_dictionary_cache(|cache| {
            romanize_document_batch_with_maps(
                &origins,
                &cache.char_map,
                &cache.word_map,
                &cache.word_index,
            )
        })
    }

    #[pg_extern(stable, strict, parallel_safe, name = "pinyin_word_romanize_batch")]
    fn pinyin_word_romanize_batch_with_suffix(
        origins: Array<&str>,
        suffix: &str,
    ) -> Vec<Option<String>> {
        if let Some(empty) = empty_document_batch(&origins) {
            return empty;
        }
        match canonicalize_table_suffix(suffix) {
            Some(canonical_suffix) => with_suffix_word_cache(&canonical_suffix, |entry| {
                romanize_document_batch_with_maps(
                    &origins,
                    &entry.char_map,
                    &entry.word_map,
                    &entry.word_index,
                )
            }),
            None => with_dictionary_cache(|cache| {
                romanize_document_batch_with_maps(
                    &origins,
                    &cache.char_map,
                    &cache.word_map,
                    &cache.word_index,
                )
            }),
        }
    }

    fn pinyin_word_romanize_tokenizer_impl(
        tokenizer_input: AnyElement,
        fcinfo: pg_sys::FunctionCallInfo,
    ) -> String {
        if let Some(tokens) = fetch_tokenizer_input_tokens(tokenizer_input, fcinfo) {
            return romanize_word_tokens(tokens);
        }

        match anyelement_to_text(tokenizer_input) {
            Some(text) => pinyin_word_romanize_impl(&text),
            None => error!("tokenizer input must be castable to text[] or text"),
        }
    }

    fn pinyin_word_romanize_tokenizer_with_suffix_impl(
        tokenizer_input: AnyElement,
        suffix: &str,
        fcinfo: pg_sys::FunctionCallInfo,
    ) -> String {
        let canonical_suffix = canonicalize_table_suffix(suffix);

        if let Some(tokens) = fetch_tokenizer_input_tokens(tokenizer_input, fcinfo) {
            return match canonical_suffix.as_deref() {
                Some(canonical_suffix) => with_suffix_word_cache(canonical_suffix, |entry| {
                    romanize_word_tokens_with_maps(
                        tokens,
                        &entry.char_map,
                        &entry.word_map,
                        &entry.word_index,
                    )
                }),
                None => with_dictionary_cache(|cache| {
                    romanize_word_tokens_with_maps(
                        tokens,
                        &cache.char_map,
                        &cache.word_map,
                        &cache.word_index,
                    )
                }),
            };
        }

        match anyelement_to_text(tokenizer_input) {
            Some(text) => match canonical_suffix.as_deref() {
                Some(canonical_suffix) => with_suffix_word_cache(canonical_suffix, |entry| {
                    pinyin_word_romanize_with_maps(
                        &text,
                        &entry.char_map,
                        &entry.word_map,
                        &entry.word_index,
                    )
                }),
                None => with_dictionary_cache(|cache| {
                    pinyin_word_romanize_with_maps(
                        &text,
                        &cache.char_map,
                        &cache.word_map,
                        &cache.word_index,
                    )
                }),
            },
            None => error!("tokenizer input must be castable to text[] or text"),
        }
    }

    #[pg_extern(immutable, strict, parallel_safe)]
    fn pinyin_char_romanize(origin: &str) -> String {
        pinyin_char_romanize_impl(origin)
    }

    #[pg_extern(immutable, strict, parallel_safe, name = "pinyin_char_romanize")]
    fn pinyin_char_romanize_with_suffix(origin: &str, suffix: &str) -> String {
        pinyin_char_romanize_with_suffix_impl(origin, suffix)
    }

    #[pg_extern(immutable, strict, parallel_safe)]
    fn pinyin_word_romanize(origin: &str) -> String {
        pinyin_word_romanize_impl(origin)
    }

    #[pg_extern(immutable, strict, parallel_safe, name = "pinyin_word_romanize")]
    fn pinyin_word_romanize_with_suffix(origin: &str, suffix: &str) -> String {
        pinyin_word_romanize_with_suffix_impl(origin, suffix)
    }

    #[pg_extern(immutable, strict, parallel_safe, name = "pinyin_word_romanize")]
    fn pinyin_word_romanize_with_tokenizer(
        tokenizer_input: AnyElement,
        fcinfo: pg_sys::FunctionCallInfo,
    ) -> String {
        pinyin_word_romanize_tokenizer_impl(tokenizer_input, fcinfo)
    }

    #[pg_extern(immutable, strict, parallel_safe, name = "pinyin_word_romanize")]
    fn pinyin_word_romanize_with_tokenizer_and_suffix(
        tokenizer_input: AnyElement,
        suffix: &str,
        fcinfo: pg_sys::FunctionCallInfo,
    ) -> String {
        pinyin_word_romanize_tokenizer_with_suffix_impl(tokenizer_input, suffix, fcinfo)
    }

    #[pg_extern(
        immutable,
        strict,
        parallel_safe,
        name = "pinyin_regex_phrase_patterns"
    )]
    fn pinyin_regex_phrase_patterns_default(value: &str) -> Option<Vec<String>> {
        pinyin_regex_phrase_patterns_impl(value, false)
    }

    #[pg_extern(
        immutable,
        strict,
        parallel_safe,
        name = "pinyin_regex_phrase_patterns"
    )]
    fn pinyin_regex_phrase_patterns_with_generated(
        value: &str,
        generated_pinyin: bool,
    ) -> Option<Vec<String>> {
        pinyin_regex_phrase_patterns_impl(value, generated_pinyin)
    }

    #[pg_extern(
        immutable,
        strict,
        parallel_safe,
        name = "pinyin_regex_phrase_patterns_mode"
    )]
    fn pinyin_regex_phrase_patterns_with_mode(
        value: &str,
        mode: &str,
        generated_pinyin: default!(bool, false),
    ) -> Vec<String> {
        if mode == "compat" {
            return pinyin_regex_phrase_patterns_impl(value, generated_pinyin).unwrap_or_default();
        }
        let mode = match query_modes::parse_mode(mode) {
            Ok(mode) => mode,
            Err(message) => {
                pgrx::ereport!(
                    ERROR,
                    pgrx::PgSqlErrorCode::ERRCODE_INVALID_PARAMETER_VALUE,
                    message
                );
            }
        };
        query_modes::pinyin_regex_phrase_patterns(
            value,
            mode,
            generated_pinyin,
            query_token_dictionary(),
        )
    }

    #[pg_extern(volatile, parallel_unsafe, name = "pinyin_clear_suffix_cache")]
    fn pinyin_clear_suffix_cache_all() -> i64 {
        clear_all_suffix_cache_impl()
    }

    #[pg_extern(volatile, strict, parallel_unsafe, name = "pinyin_clear_suffix_cache")]
    fn pinyin_clear_suffix_cache_for_suffix(suffix: &str) -> bool {
        clear_suffix_cache_impl(suffix)
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

    extension_sql!(
        r#"
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
        "#,
        name = "pinyin_regex_phrase_pg_search_helper",
        requires = [pinyin_regex_phrase_patterns_with_generated]
    );

    extension_sql!(
        r#"
DO $pinyin_query_modes$
DECLARE definition text;
BEGIN
  IF to_regtype('pdb.query') IS NOT NULL THEN
    definition := $create_function$
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
    EXECUTE definition;
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
        "#,
        name = "pinyin_regex_phrase_query_modes",
        requires = [pinyin_regex_phrase_patterns_with_mode]
    );

    extension_sql!(
        r#"
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
        "#,
        name = "pinyin_native_phrase_pg_search_helper",
        requires = [pinyin_regex_phrase_patterns_with_mode]
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

        fn seed_suffix_tables(suffix: &str) {
            let mapping_table = format!("pinyin.pinyin_mapping{}", suffix);
            let words_table = format!("pinyin.pinyin_words{}", suffix);

            Spi::run(&format!(
                "CREATE TABLE IF NOT EXISTS {mapping_table} (
                   character text PRIMARY KEY,
                   pinyin text NOT NULL
                 )"
            ))
            .expect("failed to create suffix mapping table");

            Spi::run(&format!(
                "CREATE TABLE IF NOT EXISTS {words_table} (
                   word text PRIMARY KEY,
                   pinyin text NOT NULL
                 )"
            ))
            .expect("failed to create suffix words table");

            Spi::run(&format!(
                "TRUNCATE TABLE {mapping_table}; TRUNCATE TABLE {words_table};"
            ))
            .expect("failed to truncate suffix tables");

            Spi::run(&format!(
                "INSERT INTO {mapping_table} (character, pinyin) VALUES
                   ('郑', '|zhengx|')
                 ON CONFLICT (character) DO UPDATE SET pinyin = EXCLUDED.pinyin"
            ))
            .expect("failed to seed suffix mapping");

            Spi::run(&format!(
                "INSERT INTO {words_table} (word, pinyin) VALUES
                   ('郑爽', '|zhengx| |shuangx|')
                 ON CONFLICT (word) DO UPDATE SET pinyin = EXCLUDED.pinyin"
            ))
            .expect("failed to seed suffix words");
        }

        #[pg_test]
        fn test_pinyin_char_romanize() {
            seed_minimal_data();

            let converted =
                Spi::get_one::<String>("SELECT public.pinyin_char_romanize('我ABC们123')")
                    .expect("SPI failed")
                    .expect("no row returned");
            assert_eq!(converted, "wo abc men 123");
        }

        #[pg_test]
        fn test_pinyin_word_romanize() {
            seed_minimal_data();

            let converted = Spi::get_one::<String>("SELECT public.pinyin_word_romanize('郑爽ABC')")
                .expect("SPI failed")
                .expect("no row returned");
            assert_eq!(converted, "zheng shuang abc");
        }

        #[pg_test]
        fn test_pinyin_word_romanize_with_tokenizer_passthrough() {
            seed_minimal_data();

            let converted = Spi::get_one::<String>(
                "SELECT public.pinyin_word_romanize(ARRAY['郑爽', 'ABC']::text[])",
            )
            .expect("SPI failed")
            .expect("no row returned");
            assert_eq!(converted, "zheng shuang abc");
        }

        #[pg_test]
        fn test_dictionary_update_reflected() {
            seed_minimal_data();

            let before = Spi::get_one::<String>("SELECT public.pinyin_char_romanize('我')")
                .expect("SPI failed")
                .expect("no row returned");
            assert_eq!(before, "wo");

            Spi::run("UPDATE pinyin.pinyin_mapping SET pinyin='|wo2|' WHERE character='我'")
                .expect("failed to update mapping");

            let after = Spi::get_one::<String>("SELECT public.pinyin_char_romanize('我')")
                .expect("SPI failed")
                .expect("no row returned");
            assert_eq!(after, "wo2");
        }

        #[pg_test]
        fn test_polyphone_first_reading() {
            seed_minimal_data();

            let converted = Spi::get_one::<String>("SELECT public.pinyin_char_romanize('重起')")
                .expect("SPI failed")
                .expect("no row returned");
            assert_eq!(converted, "tong qi");
        }

        #[pg_test]
        fn test_romanize_functions_are_immutable() {
            let char_volatile = Spi::get_one::<String>(
                "SELECT p.provolatile::text
                 FROM pg_proc AS p
                 WHERE p.oid = 'public.pinyin_char_romanize(text)'::regprocedure",
            )
            .expect("SPI failed")
            .expect("no row returned");
            assert_eq!(char_volatile, "i");

            let char_suffix_volatile = Spi::get_one::<String>(
                "SELECT p.provolatile::text
                 FROM pg_proc AS p
                 WHERE p.oid = 'public.pinyin_char_romanize(text,text)'::regprocedure",
            )
            .expect("SPI failed")
            .expect("no row returned");
            assert_eq!(char_suffix_volatile, "i");

            let word_volatile = Spi::get_one::<String>(
                "SELECT p.provolatile::text
                 FROM pg_proc AS p
                 WHERE p.oid = 'public.pinyin_word_romanize(text)'::regprocedure",
            )
            .expect("SPI failed")
            .expect("no row returned");
            assert_eq!(word_volatile, "i");

            let word_suffix_volatile = Spi::get_one::<String>(
                "SELECT p.provolatile::text
                 FROM pg_proc AS p
                 WHERE p.oid = 'public.pinyin_word_romanize(text,text)'::regprocedure",
            )
            .expect("SPI failed")
            .expect("no row returned");
            assert_eq!(word_suffix_volatile, "i");

            let word_tokenizer_volatile = Spi::get_one::<String>(
                "SELECT p.provolatile::text
                 FROM pg_proc AS p
                 WHERE p.oid = 'public.pinyin_word_romanize(anyelement)'::regprocedure",
            )
            .expect("SPI failed")
            .expect("no row returned");
            assert_eq!(word_tokenizer_volatile, "i");

            let word_tokenizer_suffix_volatile = Spi::get_one::<String>(
                "SELECT p.provolatile::text
                 FROM pg_proc AS p
                 WHERE p.oid = 'public.pinyin_word_romanize(anyelement,text)'::regprocedure",
            )
            .expect("SPI failed")
            .expect("no row returned");
            assert_eq!(word_tokenizer_suffix_volatile, "i");
        }

        #[pg_test]
        fn test_generated_column_usage_raw_sql() {
            seed_minimal_data();

            Spi::run(
                "CREATE TEMP TABLE pinyin_generated_demo (
                   id bigserial PRIMARY KEY,
                   description text NOT NULL,
                   pinyin text GENERATED ALWAYS AS (public.pinyin_char_romanize(description)) STORED
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

        #[pg_test]
        fn test_suffix_overlay_priority() {
            seed_minimal_data();
            seed_suffix_tables("_suffix1");

            let base_char = Spi::get_one::<String>("SELECT public.pinyin_char_romanize('郑爽ABC')")
                .expect("SPI failed")
                .expect("no row returned");
            assert_eq!(base_char, "zheng shuang abc");

            let suffix_char =
                Spi::get_one::<String>("SELECT public.pinyin_char_romanize('郑爽ABC', '_suffix1')")
                    .expect("SPI failed")
                    .expect("no row returned");
            assert_eq!(suffix_char, "zhengx shuang abc");

            let suffix_word =
                Spi::get_one::<String>("SELECT public.pinyin_word_romanize('郑爽ABC', '_suffix1')")
                    .expect("SPI failed")
                    .expect("no row returned");
            assert_eq!(suffix_word, "zhengx shuangx abc");
        }

        #[pg_test]
        fn test_suffix_overlay_update_reflected_after_cache_clear() {
            seed_minimal_data();
            seed_suffix_tables("_suffix1");

            let before =
                Spi::get_one::<String>("SELECT public.pinyin_char_romanize('郑爽ABC', '_suffix1')")
                    .expect("SPI failed")
                    .expect("no row returned");
            assert_eq!(before, "zhengx shuang abc");

            Spi::run(
                "UPDATE pinyin.pinyin_mapping_suffix1
                 SET pinyin = '|zhengy|'
                 WHERE character = '郑'",
            )
            .expect("failed to update suffix mapping");

            let still_cached =
                Spi::get_one::<String>("SELECT public.pinyin_char_romanize('郑爽ABC', '_suffix1')")
                    .expect("SPI failed")
                    .expect("no row returned");
            assert_eq!(still_cached, "zhengx shuang abc");

            let cleared =
                Spi::get_one::<bool>("SELECT public.pinyin_clear_suffix_cache('_suffix1')")
                    .expect("SPI failed")
                    .expect("no row returned");
            assert!(cleared);

            let after =
                Spi::get_one::<String>("SELECT public.pinyin_char_romanize('郑爽ABC', '_suffix1')")
                    .expect("SPI failed")
                    .expect("no row returned");
            assert_eq!(after, "zhengy shuang abc");
        }

        #[pg_test]
        fn test_suffix_overlay_fallback_to_base_when_missing() {
            seed_minimal_data();

            let base_word = Spi::get_one::<String>("SELECT public.pinyin_word_romanize('郑爽ABC')")
                .expect("SPI failed")
                .expect("no row returned");

            let missing_suffix_word =
                Spi::get_one::<String>("SELECT public.pinyin_word_romanize('郑爽ABC', '_nosuch')")
                    .expect("SPI failed")
                    .expect("no row returned");

            assert_eq!(missing_suffix_word, base_word);
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
