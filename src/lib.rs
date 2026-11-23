#[cfg(feature = "extension")]
pgrx::pg_module_magic!();

pub mod query_modes;
pub mod regex_phrase;
pub mod romanization;

#[cfg(feature = "extension")]
mod extension {
    use crate::query_modes::{self, PinyinQueryDictionary};
    use crate::regex_phrase::{self, RegexTokenDictionary};
    use crate::romanization::{
        self, CharDictionary, CharDictionaryBuilder, WordTrie, WordTrieBuilder,
    };
    use std::cell::Cell;
    use std::collections::HashMap;
    use std::ffi::{c_char, c_int};
    use std::fs;
    use std::mem::{ManuallyDrop, MaybeUninit};
    use std::process;
    use std::sync::atomic::{AtomicU64, Ordering};
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
    static CAST_CACHE_GENERATION: AtomicU64 = AtomicU64::new(1);

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

    #[pg_guard]
    unsafe extern "C-unwind" fn bump_cast_cache_generation(
        _arg: pg_sys::Datum,
        _cacheid: c_int,
        _hashvalue: u32,
    ) {
        CAST_CACHE_GENERATION.fetch_add(1, Ordering::Relaxed);
    }

    fn register_cache_callbacks() {
        CACHE_CALLBACKS.get_or_init(|| unsafe {
            pg_sys::RegisterXactCallback(Some(reset_version_snapshot), std::ptr::null_mut());
            pg_sys::RegisterSubXactCallback(
                Some(reset_subtransaction_snapshot),
                std::ptr::null_mut(),
            );
            pg_sys::CacheRegisterSyscacheCallback(
                pg_sys::SysCacheIdentifier::CASTSOURCETARGET as c_int,
                Some(bump_cast_cache_generation),
                pg_sys::Datum::null(),
            );
            pg_sys::CacheRegisterSyscacheCallback(
                pg_sys::SysCacheIdentifier::PROCOID as c_int,
                Some(bump_cast_cache_generation),
                pg_sys::Datum::null(),
            );
            pg_sys::CacheRegisterSyscacheCallback(
                pg_sys::SysCacheIdentifier::TYPEOID as c_int,
                Some(bump_cast_cache_generation),
                pg_sys::Datum::null(),
            );
            pg_sys::CacheRegisterSyscacheCallback(
                pg_sys::SysCacheIdentifier::AUTHMEMMEMROLE as c_int,
                Some(bump_cast_cache_generation),
                pg_sys::Datum::null(),
            );
            pg_sys::CacheRegisterSyscacheCallback(
                pg_sys::SysCacheIdentifier::AUTHOID as c_int,
                Some(bump_cast_cache_generation),
                pg_sys::Datum::null(),
            );
        });
    }

    fn snapshot_key() -> Option<SnapshotKey> {
        register_cache_callbacks();
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
        char_dict: Arc<CharDictionary>,
    }

    #[derive(Default)]
    struct DictionaryCache {
        version: DictionaryVersion,
        loaded: bool,
        char_dict: Arc<CharDictionary>,
        word_trie: Arc<WordTrie>,
    }

    #[derive(Default)]
    struct SuffixDictionaryCacheEntry {
        base_version: DictionaryVersion,
        relations: (u32, u32),
        char_overlay: Option<Arc<CharDictionary>>,
        words_loaded: bool,
        word_overlay: Option<Arc<WordTrie>>,
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

    fn fetch_char_dictionary_from_table(table: &str) -> CharDictionary {
        let query = format!(
            "SELECT character, pinyin FROM {schema}.{table}",
            schema = DICTIONARY_SCHEMA,
            table = table,
        );

        Spi::connect(|client| {
            let rows = match client.select(&query, None, &[]) {
                Ok(rows) => rows,
                Err(err) => error!("SPI query failed: {err}. query={query}"),
            };

            let mut builder = CharDictionaryBuilder::with_capacity(rows.len());
            for row in rows {
                let key = match row.get::<&str>(1) {
                    Ok(Some(v)) => v,
                    Ok(None) => continue,
                    Err(err) => error!("SPI row parse failed for {table}.character: {err}"),
                };
                let value = match row.get::<&str>(2) {
                    Ok(Some(v)) => v,
                    Ok(None) => continue,
                    Err(err) => error!("SPI row parse failed for {table}.pinyin: {err}"),
                };
                builder.insert(key, value);
            }

            builder.build()
        })
    }

    fn fetch_word_trie_from_table(table: &str) -> WordTrie {
        let query = format!(
            "SELECT word, pinyin FROM {schema}.{table}",
            schema = DICTIONARY_SCHEMA,
            table = table,
        );

        Spi::connect(|client| {
            let rows = match client.select(&query, None, &[]) {
                Ok(rows) => rows,
                Err(err) => error!("SPI query failed: {err}. query={query}"),
            };

            let mut builder = WordTrieBuilder::with_capacity(rows.len());
            for row in rows {
                let key = match row.get::<&str>(1) {
                    Ok(Some(v)) => v,
                    Ok(None) => continue,
                    Err(err) => error!("SPI row parse failed for {table}.word: {err}"),
                };
                let value = match row.get::<&str>(2) {
                    Ok(Some(v)) => v,
                    Ok(None) => continue,
                    Err(err) => error!("SPI row parse failed for {table}.pinyin: {err}"),
                };
                builder.insert(key, value);
            }

            builder.build()
        })
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
        let char_dict = Arc::new(fetch_char_dictionary_from_table("pinyin_mapping"));
        CharDictionaryCache {
            version,
            loaded: true,
            char_dict,
        }
    }

    fn with_char_dictionary_cache<R>(f: impl FnOnce(&Arc<CharDictionary>) -> R) -> R {
        let version = fetch_dictionary_version();
        let lock = char_dictionary_cache();

        {
            let cache = lock
                .read()
                .expect("char dictionary cache read lock poisoned");
            if cache.loaded && cache.version == version {
                return f(&cache.char_dict);
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
            f(&cache.char_dict)
        }
    }

    fn load_dictionary_snapshot(version: DictionaryVersion) -> DictionaryCache {
        let char_dict = with_char_dictionary_cache(Arc::clone);
        let word_trie = Arc::new(fetch_word_trie_from_table("pinyin_words"));

        DictionaryCache {
            version,
            loaded: true,
            char_dict,
            word_trie,
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

    fn lookup_table_relid(namespace_oid: pg_sys::Oid, prefix: &str, suffix: &str) -> u32 {
        let mut name_buf = [0u8; 64];
        let total = prefix.len() + suffix.len();
        assert!(
            total < name_buf.len(),
            "suffix relation name exceeded buffer"
        );
        name_buf[..prefix.len()].copy_from_slice(prefix.as_bytes());
        name_buf[prefix.len()..total].copy_from_slice(suffix.as_bytes());
        name_buf[total] = 0;

        unsafe {
            let relid =
                pg_sys::get_relname_relid(name_buf.as_ptr().cast::<c_char>(), namespace_oid);
            if relid == pg_sys::InvalidOid {
                return 0;
            }
            let relkind = pg_sys::get_rel_relkind(relid) as u8;
            if matches!(relkind, b'r' | b'p' | b'v' | b'm' | b'f') {
                relid.to_u32()
            } else {
                0
            }
        }
    }

    fn suffix_relations(suffix: &str) -> (u32, u32) {
        let namespace_oid = unsafe { pg_sys::get_namespace_oid(c"pinyin".as_ptr(), true) };
        if namespace_oid == pg_sys::InvalidOid {
            return (0, 0);
        }
        (
            lookup_table_relid(namespace_oid, "pinyin_mapping", suffix),
            lookup_table_relid(namespace_oid, "pinyin_words", suffix),
        )
    }

    fn get_suffix_overlays(
        suffix: &str,
        words: bool,
    ) -> (Option<Arc<CharDictionary>>, Option<Arc<WordTrie>>) {
        let version = fetch_dictionary_version();
        let relations = suffix_relations(suffix);
        let lock = suffix_dictionary_cache();

        if relations == (0, 0) {
            // Missing overlays must not occupy a backend-lifetime cache entry,
            // and avoid taking a write lock on the common absent path.
            let has_stale = lock
                .read()
                .expect("suffix cache poisoned")
                .contains_key(suffix);
            if has_stale {
                lock.write().expect("suffix cache poisoned").remove(suffix);
            }
            return (None, None);
        }

        let previous_chars = {
            let cache = lock.read().expect("suffix cache poisoned");
            if let Some(entry) = cache.get(suffix) {
                if entry.base_version == version && entry.relations == relations {
                    if !words || entry.words_loaded {
                        return (
                            entry.char_overlay.clone(),
                            if words {
                                entry.word_overlay.clone()
                            } else {
                                None
                            },
                        );
                    }
                    Some(entry.char_overlay.clone())
                } else {
                    None
                }
            } else {
                None
            }
        };

        let char_overlay = previous_chars.unwrap_or_else(|| {
            if relations.0 == 0 {
                None
            } else {
                Some(Arc::new(fetch_char_dictionary_from_table(&format!(
                    "pinyin_mapping{suffix}"
                ))))
            }
        });
        let word_overlay = if words && relations.1 != 0 {
            Some(Arc::new(fetch_word_trie_from_table(&format!(
                "pinyin_words{suffix}"
            ))))
        } else {
            None
        };
        let entry = SuffixDictionaryCacheEntry {
            base_version: version,
            relations,
            char_overlay: char_overlay.clone(),
            words_loaded: words,
            word_overlay: word_overlay.clone(),
        };
        let mut cache = lock.write().expect("suffix cache poisoned");
        cache.insert(suffix.to_string(), entry);
        (char_overlay, word_overlay)
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

    enum DirectCastResult {
        Datum(pg_sys::Datum),
        Null,
        Unsupported,
    }

    #[repr(u8)]
    #[derive(Clone, Copy, Eq, PartialEq)]
    enum TokenizerCastKind {
        Unresolved = 0,
        TextArray = 1,
        Relabel = 2,
        DirectFunc = 3,
        SpiFallback = 4,
    }

    #[repr(C)]
    struct TokenizerCastCache {
        generation: u64,
        input_oid: pg_sys::Oid,
        user_id: pg_sys::Oid,
        collation: pg_sys::Oid,
        kind: TokenizerCastKind,
        fmgr_info: pg_sys::FmgrInfo,
    }

    struct MemoryContextGuard(pg_sys::MemoryContext);

    impl Drop for MemoryContextGuard {
        fn drop(&mut self) {
            unsafe {
                pg_sys::MemoryContextSwitchTo(self.0);
            }
        }
    }

    const FCINFO1_SLOTS: usize = 1 + std::mem::size_of::<pg_sys::FunctionCallInfoBaseData>()
        .div_ceil(std::mem::size_of::<pg_sys::NullableDatum>());

    #[repr(C)]
    union LocalFcInfo1 {
        fcinfo: ManuallyDrop<pg_sys::FunctionCallInfoBaseData>,
        _storage: [pg_sys::NullableDatum; FCINFO1_SLOTS],
    }

    unsafe fn check_func_execute_acl(function: pg_sys::Oid, user_id: pg_sys::Oid) {
        unsafe {
            #[cfg(feature = "pg15")]
            let permission =
                pg_sys::pg_proc_aclcheck(function, user_id, pg_sys::ACL_EXECUTE as pg_sys::AclMode);
            #[cfg(not(feature = "pg15"))]
            let permission = pg_sys::object_aclcheck(
                pg_sys::ProcedureRelationId,
                function,
                user_id,
                pg_sys::ACL_EXECUTE as pg_sys::AclMode,
            );
            if permission != pg_sys::AclResult::ACLCHECK_OK {
                pg_sys::aclcheck_error(
                    permission,
                    pg_sys::ObjectType::OBJECT_FUNCTION,
                    pg_sys::get_func_name(function),
                );
            }
        }
    }

    unsafe fn build_cast_func_expr(
        function: pg_sys::Oid,
        input_oid: pg_sys::Oid,
        caller: pg_sys::FunctionCallInfo,
    ) -> *mut pg_sys::FuncExpr {
        unsafe {
            let expression = (*(*caller).flinfo).fn_expr;
            let argument = if !expression.is_null()
                && (*expression).type_ == pg_sys::NodeTag::T_FuncExpr
            {
                pgrx::PgList::<pg_sys::Node>::from_pg((*expression.cast::<pg_sys::FuncExpr>()).args)
                    .head()
            } else {
                None
            }
            .unwrap_or_else(|| pg_sys::makeNullConst(input_oid, -1, pg_sys::InvalidOid).cast());
            let mut args = pgrx::PgList::<pg_sys::Node>::new();
            args.push(argument);
            pg_sys::makeFuncExpr(
                function,
                pg_sys::TEXTARRAYOID,
                args.into_pg(),
                (*caller).fncollation,
                pg_sys::exprCollation(argument),
                pg_sys::CoercionForm::COERCE_EXPLICIT_CAST,
            )
        }
    }

    unsafe fn invoke_direct_cast(
        info: *mut pg_sys::FmgrInfo,
        input: AnyElement,
        collation: pg_sys::Oid,
    ) -> DirectCastResult {
        unsafe {
            debug_assert!(
                std::mem::size_of::<LocalFcInfo1>()
                    >= pg_sys::SizeForFunctionCallInfo!(1).get() as usize
            );
            let mut local = MaybeUninit::<LocalFcInfo1>::zeroed().assume_init();
            let call: *mut pg_sys::FunctionCallInfoBaseData =
                std::ptr::addr_of_mut!(local.fcinfo).cast();
            pg_sys::InitFunctionCallInfoData!(
                *call,
                info,
                1,
                collation,
                std::ptr::null_mut::<pg_sys::Node>(),
                std::ptr::null_mut::<pg_sys::Node>()
            );
            std::ptr::addr_of_mut!((*call).args)
                .cast::<pg_sys::NullableDatum>()
                .write(pg_sys::NullableDatum {
                    value: input.datum(),
                    isnull: false,
                });
            let result = pg_sys::ffi::pg_guard_ffi_boundary(|| (*info).fn_addr.unwrap()(call));
            if (*call).isnull {
                DirectCastResult::Null
            } else {
                DirectCastResult::Datum(result)
            }
        }
    }

    unsafe fn resolve_direct_tokenizer_cast(
        input: AnyElement,
        caller: pg_sys::FunctionCallInfo,
    ) -> DirectCastResult {
        register_cache_callbacks();
        unsafe {
            let input_oid = input.oid();
            let user_id = pg_sys::GetUserId();

            if !caller.is_null()
                && !(*caller).flinfo.is_null()
                && !(*(*caller).flinfo).fn_mcxt.is_null()
            {
                let flinfo = (*caller).flinfo;
                let fn_mcxt = (*flinfo).fn_mcxt;
                let collation = (*caller).fncollation;
                let generation = CAST_CACHE_GENERATION.load(Ordering::Relaxed);

                let cache = if (*flinfo).fn_extra.is_null() {
                    let ptr = pg_sys::MemoryContextAllocZero(
                        fn_mcxt,
                        std::mem::size_of::<TokenizerCastCache>(),
                    )
                    .cast::<TokenizerCastCache>();
                    (*flinfo).fn_extra = ptr.cast();
                    ptr
                } else {
                    (*flinfo).fn_extra.cast::<TokenizerCastCache>()
                };

                if (*cache).kind == TokenizerCastKind::Unresolved
                    || (*cache).generation != generation
                    || (*cache).input_oid != input_oid
                    || (*cache).user_id != user_id
                    || (*cache).collation != collation
                {
                    // Reset before ACL/catalog work so an error never leaves stale cache state.
                    (*cache).kind = TokenizerCastKind::Unresolved;
                    let source = pg_sys::getBaseType(input_oid);
                    if source == pg_sys::TEXTARRAYOID {
                        (*cache).kind = TokenizerCastKind::TextArray;
                    } else {
                        let mut function = pg_sys::InvalidOid;
                        let path = pg_sys::find_coercion_pathway(
                            pg_sys::TEXTARRAYOID,
                            source,
                            pg_sys::CoercionContext::COERCION_EXPLICIT,
                            &mut function,
                        );
                        if path == pg_sys::CoercionPathType::COERCION_PATH_RELABELTYPE {
                            (*cache).kind = TokenizerCastKind::Relabel;
                        } else if path == pg_sys::CoercionPathType::COERCION_PATH_FUNC
                            && pg_sys::get_func_nargs(function) == 1
                            && pg_sys::func_volatile(function) as u8 == b'i'
                            && pg_sys::func_parallel(function) as u8 == b's'
                        {
                            check_func_execute_acl(function, user_id);
                            pg_sys::fmgr_info_cxt(
                                function,
                                std::ptr::addr_of_mut!((*cache).fmgr_info),
                                fn_mcxt,
                            );
                            // ParadeDB tokenizers read fn_expr to obtain the input typmod.
                            // Allocate it in fn_mcxt so it outlives per-tuple memory resets.
                            let cast_expression = {
                                let _guard =
                                    MemoryContextGuard(pg_sys::MemoryContextSwitchTo(fn_mcxt));
                                build_cast_func_expr(function, input_oid, caller)
                            };
                            (*cache).fmgr_info.fn_expr = cast_expression.cast();
                            (*cache).kind = TokenizerCastKind::DirectFunc;
                        } else {
                            (*cache).kind = TokenizerCastKind::SpiFallback;
                        }
                    }
                    (*cache).generation = generation;
                    (*cache).input_oid = input_oid;
                    (*cache).user_id = user_id;
                    (*cache).collation = collation;
                }

                return match (*cache).kind {
                    TokenizerCastKind::TextArray | TokenizerCastKind::Relabel => {
                        DirectCastResult::Datum(input.datum())
                    }
                    TokenizerCastKind::DirectFunc => invoke_direct_cast(
                        std::ptr::addr_of_mut!((*cache).fmgr_info),
                        input,
                        collation,
                    ),
                    TokenizerCastKind::SpiFallback | TokenizerCastKind::Unresolved => {
                        DirectCastResult::Unsupported
                    }
                };
            }

            let source = pg_sys::getBaseType(input_oid);
            if source == pg_sys::TEXTARRAYOID {
                return DirectCastResult::Datum(input.datum());
            }
            let mut function = pg_sys::InvalidOid;
            let path = pg_sys::find_coercion_pathway(
                pg_sys::TEXTARRAYOID,
                source,
                pg_sys::CoercionContext::COERCION_EXPLICIT,
                &mut function,
            );
            if path == pg_sys::CoercionPathType::COERCION_PATH_RELABELTYPE {
                return DirectCastResult::Datum(input.datum());
            }
            if path == pg_sys::CoercionPathType::COERCION_PATH_FUNC
                && pg_sys::get_func_nargs(function) == 1
                && pg_sys::func_volatile(function) as u8 == b'i'
                && pg_sys::func_parallel(function) as u8 == b's'
            {
                check_func_execute_acl(function, user_id);
                let mut info = pg_sys::FmgrInfo::default();
                pg_sys::fmgr_info(function, &mut info);
                let collation = if caller.is_null() {
                    pg_sys::InvalidOid
                } else {
                    info.fn_expr = build_cast_func_expr(function, input_oid, caller).cast();
                    (*caller).fncollation
                };
                return invoke_direct_cast(&mut info, input, collation);
            }
            DirectCastResult::Unsupported
        }
    }

    fn with_tokenizer_input_tokens<R>(
        input: AnyElement,
        fcinfo: pg_sys::FunctionCallInfo,
        f: impl FnOnce(&[&str]) -> R,
    ) -> Option<R> {
        match unsafe { resolve_direct_tokenizer_cast(input, fcinfo) } {
            DirectCastResult::Datum(datum) => {
                // The catalog coercion established TEXTARRAYOID. Decode in this call's memory context.
                let array = unsafe { Array::<&str>::from_datum(datum, false) }?;
                let mut slices = Vec::with_capacity(array.len());
                for token in array.iter() {
                    slices.push(token.unwrap_or_else(|| {
                        error!("tokenizer text[] input must not contain NULL elements")
                    }));
                }
                return Some(f(&slices));
            }
            DirectCastResult::Null => return None,
            DirectCastResult::Unsupported => {}
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
        let owned: Vec<String> = tokens
            .into_iter()
            .map(|token| {
                token.unwrap_or_else(|| {
                    error!("tokenizer text[] input must not contain NULL elements")
                })
            })
            .collect();
        let slices: Vec<&str> = owned.iter().map(String::as_str).collect();
        Some(f(&slices))
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

    fn pinyin_char_romanize_impl(origin: &str) -> String {
        with_char_dictionary_cache(|chars| romanization::romanize_chars(origin, chars, None))
    }

    fn pinyin_char_romanize_with_suffix_impl(origin: &str, suffix: &str) -> String {
        match canonicalize_table_suffix(suffix) {
            Some(suffix) => {
                let (char_overlay, _) = get_suffix_overlays(&suffix, false);
                with_char_dictionary_cache(|chars| {
                    romanization::romanize_chars(origin, chars, char_overlay.as_deref())
                })
            }
            None => pinyin_char_romanize_impl(origin),
        }
    }

    fn pinyin_word_romanize_impl(origin: &str) -> String {
        with_dictionary_cache(|cache| {
            romanization::romanize([origin], &cache.char_dict, None, &cache.word_trie, None)
        })
    }

    fn pinyin_word_romanize_with_suffix_impl(origin: &str, suffix: &str) -> String {
        match canonicalize_table_suffix(suffix) {
            Some(canonical_suffix) => {
                let (char_overlay, word_overlay) = get_suffix_overlays(&canonical_suffix, true);
                with_dictionary_cache(|cache| {
                    romanization::romanize(
                        [origin],
                        &cache.char_dict,
                        char_overlay.as_deref(),
                        &cache.word_trie,
                        word_overlay.as_deref(),
                    )
                })
            }
            None => pinyin_word_romanize_impl(origin),
        }
    }

    fn romanize_document_batch_with_dicts(
        origins: &Array<&str>,
        base_chars: &CharDictionary,
        overlay_chars: Option<&CharDictionary>,
        base_words: &WordTrie,
        overlay_words: Option<&WordTrie>,
    ) -> Vec<Option<String>> {
        let mut scratch = Vec::new();
        origins
            .iter()
            .map(|origin| {
                origin.map(|origin| {
                    romanization::romanize_with_scratch(
                        [origin],
                        base_chars,
                        overlay_chars,
                        base_words,
                        overlay_words,
                        &mut scratch,
                    )
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
            romanize_document_batch_with_dicts(
                &origins,
                &cache.char_dict,
                None,
                &cache.word_trie,
                None,
            )
        })
    }

    #[pg_extern(stable, strict, parallel_safe, name = "pinyin_word_romanize_batch")]
    fn pinyin_word_romanize_batch_with_suffix(
        origins: Array<&str>,
        suffix: &str,
    ) -> Vec<Option<String>> {
        let canonical_suffix = canonicalize_table_suffix(suffix);
        if let Some(empty) = empty_document_batch(&origins) {
            return empty;
        }
        match canonical_suffix {
            Some(canonical_suffix) => {
                let (char_overlay, word_overlay) = get_suffix_overlays(&canonical_suffix, true);
                with_dictionary_cache(|cache| {
                    romanize_document_batch_with_dicts(
                        &origins,
                        &cache.char_dict,
                        char_overlay.as_deref(),
                        &cache.word_trie,
                        word_overlay.as_deref(),
                    )
                })
            }
            None => with_dictionary_cache(|cache| {
                romanize_document_batch_with_dicts(
                    &origins,
                    &cache.char_dict,
                    None,
                    &cache.word_trie,
                    None,
                )
            }),
        }
    }

    fn pinyin_word_romanize_tokenizer_impl(
        tokenizer_input: AnyElement,
        fcinfo: pg_sys::FunctionCallInfo,
    ) -> String {
        if let Some(result) = with_tokenizer_input_tokens(tokenizer_input, fcinfo, |tokens| {
            with_dictionary_cache(|cache| {
                romanization::romanize(
                    tokens.iter().copied(),
                    &cache.char_dict,
                    None,
                    &cache.word_trie,
                    None,
                )
            })
        }) {
            return result;
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

        if let Some(result) = with_tokenizer_input_tokens(tokenizer_input, fcinfo, |tokens| {
            match canonical_suffix.as_deref() {
                Some(canonical_suffix) => {
                    let (char_overlay, word_overlay) = get_suffix_overlays(canonical_suffix, true);
                    with_dictionary_cache(|cache| {
                        romanization::romanize(
                            tokens.iter().copied(),
                            &cache.char_dict,
                            char_overlay.as_deref(),
                            &cache.word_trie,
                            word_overlay.as_deref(),
                        )
                    })
                }
                None => with_dictionary_cache(|cache| {
                    romanization::romanize(
                        tokens.iter().copied(),
                        &cache.char_dict,
                        None,
                        &cache.word_trie,
                        None,
                    )
                }),
            }
        }) {
            return result;
        }

        match anyelement_to_text(tokenizer_input) {
            Some(text) => match canonical_suffix.as_deref() {
                Some(canonical_suffix) => {
                    let (char_overlay, word_overlay) = get_suffix_overlays(canonical_suffix, true);
                    with_dictionary_cache(|cache| {
                        romanization::romanize(
                            [text.as_str()],
                            &cache.char_dict,
                            char_overlay.as_deref(),
                            &cache.word_trie,
                            word_overlay.as_deref(),
                        )
                    })
                }
                None => pinyin_word_romanize_impl(&text),
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
