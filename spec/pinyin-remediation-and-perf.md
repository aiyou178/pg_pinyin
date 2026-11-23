# Correctness, Performance, and Packaging Remediation Specification

Date: 2026-11-22
Status: Implemented and locally validated, with explicit deferrals below; no version bump
Baseline: `main` (`2dadce0`, development version `0.0.9`)

## Baseline Audit and Implementation Decisions

The numbered findings below are retained as the original audit input, not as
verified descriptions of every current function. Inspection of `2dadce0`
corrected these assumptions:

- There is no `scripts/prepare_upgrade.sh`; the baseline installer is
  `test/prepare_upgrade.sh`. The released upgrade intentionally preserves live
  data. No published migration is edited here. Explicit additions are available
  through `python3 scripts/sync_dictionary.py --dsn "$PGURL"`: one transaction,
  `ON CONFLICT DO NOTHING`, no deletion or override replacement.
- Suffix case folding already works. This change adds a 40-character limit,
  relation-OID invalidation and avoids caching nonexistent overlays. DML in
  custom suffix tables still requires `pinyin_clear_suffix_cache`, as before.
- Native tokenizer arrays already reject NULL elements. Preserve that contract
  and apply it consistently to SQL arrays rather than silently dropping data.
- `pinyin_isearch` already uses `string_agg` with separators; no vowel-reset bug
  exists in this baseline. `pinyin_tokenize` already has an ASCII letter fallback.
  Reject non-ASCII/punctuated input rather than silently returning a partial query.
  `characters2pinyin`, `characters2header`, and the named core-only query-mode
  test file do not exist; extend the actual APIs and add `10_remediation_core.sql`.
- Modern generated-mode patterns already match both piped and plain tokens;
  legacy generated patterns require pipes. Normalize SQL column generators to
  `|syllable| |syllable|`, including ASCII runs and phrase hits. Applications
  must regenerate stored SQL-backend columns and rebuild affected indexes;
  no upgrade performs that rewrite automatically.
- Retain all 196,188 word keys, including historical singleton `葛`, but make
  longest phrase lookup take precedence. This uses the allowed bypass option
  without reducing the previously audited coverage floor.

Implemented performance work:

- Version checks are cached only for an active MVCC snapshot key, including
  command ID, completion counter and subtransaction ID; transaction and
  subtransaction callbacks reset the cached version. Recovery snapshots do not
  use this fast path. No transaction ID is assigned.
- Character maps are shared by `Arc`; char-to-word suffix promotion reuses its
  character map. Overlay maps still merge the word dictionary, not delta-only
  tries. A delta-only overlay representation is deferred.
- A prefix index bounds greedy lookup; mapped readings are converted only after
  the longest match is selected. Plain input uses borrowed UTF-8 slices directly.
- Single-argument catalog casts to text[] bypass SPI and JSON; other coercion
  pathways use typed read-only SPI, without JSON serialization. Cast OIDs are
  resolved on each call to respect ALTER/DROP CAST rather than cached unsafely.
  Direct calls carry a real `FuncExpr` and the original input expression/typmod;
  `OidFunctionCall1Coll` alone is not sufficient for
  [ParadeDB's tokenizer wrappers](https://github.com/paradedb/paradedb/blob/v0.27.0/pg_search/src/api/tokenizers/definitions.rs#L144-L156).
  They also perform the same EXECUTE ACL check as SQL evaluation, including
  revocations and grants after SET ROLE. Missing-suffix character calls do not
  load or require the word dictionary.
- Keep runtime auto-query capability probes for now. Existing regressions require
  graceful fallback when native APIs are renamed after helper creation. Folding
  these probes at installation failed that contract; a future cache needs catalog
  invalidation rather than a permanently captured boolean.

Packaging uses isolated pgrx package directories, validates both control files
and minimum server/header versions, scopes RPM conversion by architecture, and
maps RPM files to PGDG paths. RPMs converted from Debian Trixie binaries require
glibc 2.41 or later; this is not a claim of compatibility with older RHEL ABIs.

### Validation Record

Results and replay protocol: [runtime remediation benchmark](../benchmarks/remediation_20261122.md).

- PG19 native/Rust: 46 passed. Core pgTAP: 83 assertions each on PG15.20,
  PG16.16, PG17.12 and PG19.0. PG18.7 + pg_search 0.27.0: all 1,630 passed.
  The full PG18 Python suite passed 212 tests. Real snapshot/savepoint/sync tests
  also passed on PG19; no transaction-wide dictionary-version cache is used.
- New core regression: 16 intended failures on the rebuilt baseline, all 25
  assertions passing after the fix. Real ICU casting is tested, not only text[].
  Direct-cast EXECUTE ACLs are tested with a real role on every major.
- Released 0.0.5-0.0.8 catalogs and tag data replayed with the current library:
  ALTER EXTENSION preserves rows; explicit sync adds 38,486 words and four tokens
  per old dictionary without replacing overrides. A second sync adds nothing.
  This is not an old-binary compatibility claim. Fresh token categories and
  legacy upgraded categories can differ because explicit sync is additive.
- All nine generated artifacts reproduced byte-for-byte; mapping and phrase CSV
  bytes did not change. Coverage remains 44,435 characters / 196,188 phrases /
  447 fresh tokens, including all 64,091 restored old words. The database seed
  also retains its separate space mapping row.
- Ten ARM64 DEB/RPM packages built and audited across PG15-19. Corresponding
  payloads and every migration match; dependencies, PGDG paths and architecture
  filtering passed. AMD64 and hosted CI were not run locally.
- An unwritable-/tmp installation successfully reached the INSERT fallback.
  cargo fmt/check, generator tests, dictionary accuracy tests and shell syntax
  checks passed. Existing unrelated untracked files were left untouched.

At 20k documents, PG18 warm word text fell from 239.993 to 12.135 ms; indexed
COPY from 364.523 to 160.579 ms. PG19 warm word text fell from 144.873 to
11.613 ms. This is a combined-change comparison, not isolated attribution.
Default-regex search was effectively unchanged (PG18 median +0.9%, overlapping
ranges), with identical hits and index bytes. Word-backend RSS grew about 10 MiB;
first-backend batch time regressed by 4.8% on PG18 and 10.9% on PG19.

Deferred deliberately: delta-only suffix dictionaries, and cached native-query
capabilities with proper catalog invalidation. Installation-time booleans were
rejected after they broke runtime API-removal fallback coverage. The numbered
original audit below remains as input; the corrected contracts above govern the
implementation, including retaining NULL-element rejection and singleton coverage.

## Scope and Non-Goals

This specification covers correctness bugs, data-pipeline anomalies, migration
and packaging defects, and runtime performance bottlenecks identified across the
Rust extension (`src/`), standalone SQL runtime (`sql/`), dictionary generator
(`scripts/data/`), release packaging (`scripts/`, `docker/`, `.github/`), and
test suites (`test/`).

Non-goals:
- Do not break existing public SQL function signatures or volatility contracts
  required by stored generated columns (`IMMUTABLE` scalar romanization and
  pattern helpers; see [pgrx 0.20.0 Upgrade](pgrx-0.20-upgrade.md#volatility-and-compatibility)).
- Do not reintroduce multi-segmentation token expansion into the default BM25
  field or perform destructive table rewrites during `ALTER EXTENSION pg_pinyin UPDATE`.
- Do not reduce the 196,188-key phrase coverage floor in `sql/data/pinyin_words.csv`
  (except removing or bypassing the redundant 1-character `葛` entry in
  `pinyin_words.csv` whose reading `ge` is already primary in `pinyin_mapping.csv`).

---

## 1. Romanization and Tokenizer Correctness

### 1.1 Greedy Longest-Match Precedence vs. 1-Character `pinyin_words` Keys

- **Defect**: `romanize_word_tokens_with_maps` (`src/lib.rs:937`) checks
  `word_map.get(&tokens[idx])` on the single-character token before running the
  `2..=max_word_len` multi-character lookahead loop (`src/lib.rs:948-964`).
  Because `sql/data/pinyin_words.csv:151630` contains the single-character row
  `葛,ge` (`len == 1`), any input starting with `葛` immediately matches at length
  `1`, shadowing all 23 multi-character `葛*` phrases in `pinyin_words.csv`
  (`葛优`, `葛洲坝`, `葛兰素史克`, `葛屦履霜`, etc.) as well as any user-inserted
  polyphonic `葛*` overrides (e.g., surname `葛` -> `ge3` vs. `ge2`).
- **Contract**:
  1. Multi-character greedy lookup (`2..=max_word_len`) MUST execute before any
     single-token `word_map` fallback when `tokens[idx]` is a single Han character.
  2. `scripts/data/generate_extension_data.py` MUST filter or reject 1-character
     entries in `pinyin_words.csv` (`len(word) < 2`), keeping single-character
     readings exclusively in `pinyin_mapping.csv`.

### 1.2 Word vs. Character Dictionary Independence

- **Defect**: `tokenize_plain` (`src/lib.rs:761-764`) filters input characters
  through `char_map.contains_key(&ch)` before `romanize_word_tokens_with_maps`
  consults `word_map`. Any phrase inserted into `pinyin.pinyin_words` (or suffix
  `pinyin.pinyin_words_*`) containing a rare CJK character outside
  `pinyin_mapping` has that character stripped prior to word lookup.
- **Contract**:
  - `tokenize_plain` MUST preserve contiguous Han characters (or characters
    present in either `char_map` or `word_map`) so `pinyin_words` lookup operates
    on the unstripped token sequence, dropping or passing through unmapped
    characters only after phrase matching fails.

### 1.3 Tokenizer (`text[]` / `anyelement` / `pdb.icu`) Normalization Parity

- **Defects**:
  1. **Mixed Han+ASCII and punctuation tokens**: In `romanize_word_tokens_with_maps`
     (`src/lib.rs:971-973`), any token that is not strictly single-Han and not an
     exact `word_map` key is emitted verbatim (`output.push(token)`), bypassing
     `split_input` normalization. Thus `pinyin_word_romanize(ARRAY['郑爽ABC', '！'])`
     returns `'zheng shuang 郑爽ABC ！'`, whereas `pinyin_word_romanize('郑爽ABC！')`
     returns `'zheng shuang abc'`.
  2. **Multi-character Han sub-tokens**: The greedy loop (`src/lib.rs:943-956`)
     requires `tokens[idx + offset].chars().count() == 1` and steps by array
     element rather than character count, failing to match dictionary phrases or
     fall back to `char_map` when an upstream tokenizer emits a multi-character
     Han token outside `pinyin_words`.
  3. **`icu_romanize_tokens(text[])` ASCII splitting and casing**:
     `src/lib.rs:907-911` emits non-Han characters per codepoint without ASCII
     lowercasing or run coalescing (`ARRAY['郑爽ABC']` -> `'zheng shuang A B C'`).
  4. **Inconsistent `NULL` and empty-array semantics**:
     - `pinyin_word_romanize(ARRAY[]::text[])` returns `''` (`src/lib.rs:1098`),
       whereas `icu_romanize_tokens(ARRAY[]::text[])` returns `NULL`
       (`src/lib.rs:920-924`).
     - `pinyin_word_romanize(ARRAY['郑爽', NULL]::text[])` ignores `NULL` on the
       native `TEXTARRAYOID` path (`src/lib.rs:791`), whereas the SPI fallback
       for non-`text[]` array-like types rejects `NULL` elements with
       `ERRCODE_NULL_VALUE_NOT_ALLOWED` (`src/lib.rs:820-835`).
- **Contract**:
  - Normalize non-dictionary tokens in `pinyin_word_romanize(anyelement)` and
    `icu_romanize_tokens(text[])` through the same character/ASCII segmentation
    rules as plain-text romanization.
  - Standardize `NULL` element handling (skip `NULL` elements consistently across
    native `text[]` and custom tokenizer casts) and empty-array return values.

---

## 2. Query Mode, Orthography, and SQL Runtime Correctness

### 2.1 Toned Chunks with Trailing Prefixes in `auto` Mode

- **Defect**: `parse_auto_chunk` (`src/query_modes.rs:429-468` and
  `sql/word.sql:504-547`) runs `normalize_explicit_chunk` on the entire
  whitespace-delimited chunk and returns `AutoChunk::Invalid` if the normalized
  chunk is not in `FULL_TOKEN_SET` (`src/query_modes.rs:442`). Because
  `normalize_explicit_chunk` rejects any chunk with a tone mark inside the chunk
  rather than at the end (`src/query_modes.rs:347-349`), valid multi-syllable
  inputs combining a toned syllable with a trailing prefix or second syllable
  fail in `auto` mode:
  - Diacritic syllable + prefix (`'wúh'`, `'nǐyón'`) fails `FULL_TOKEN_SET` and
    triggers `return Ok(Vec::new())` (`src/query_modes.rs:455`), collapsing the
    entire query to `EMPTY`.
  - Numeric-toned syllable + untoned syllable/prefix without space (`'ni3hao'`,
    `'wu2h'`) is rejected before compatibility fallback (`src/query_modes.rs:347`).
  - When a later chunk triggers compatibility fallback (e.g., `'ni3 whh'`),
    `auto_patterns_from_input` (`src/query_modes.rs:479-493`) discards the
    already-resolved exact token `'ni'` from chunk 1 and re-parses the stripped
    stream via regex compatibility mode, turning `'ni3'` into `'ni.*'`.
- **Contract**:
  - Segment chunks at syllable/tone boundaries before checking whether the
    terminal segment of the final chunk is an unfinished prefix.
  - Preserve resolved exact chunks when a subsequent chunk falls back to
    compatibility tokenization.

### 2.2 Syllabic Nasal Diacritics (`ńg`, `ňg`, `ǹg`) and `'m'` / `'n'` Category Parity

- **Defects**:
  1. `normalize_diacritic_char` (`src/query_modes.rs:285-305`) and
     `sql_pinyin_normalize_explicit_chunk` (`sql/word.sql:198-216`) map `'ń'`,
     `'ň'`, `'ǹ'` (`U+0144`, `U+0148`, `U+01F9`), which `normalize_explicit_chunk`
     only accepts when `ch == 'n' && normalized.len() == 1`
     (`src/query_modes.rs:341-342`, `sql/word.sql:237-238`). Consequently,
     syllabic `'ńg'`, `'ňg'`, `'ǹg'` (`嗯` / `唔` / `噥`, where `ng` is a valid
     category-1 syllable in `sql/data/pinyin_token.csv:225` and `'ng2'` works)
     are rejected as `EMPTY`.
  2. `build_token_rows` (`scripts/data/generate_extension_data.py:337-344`)
     excludes `INITIALS` (category `2`) from category `1`, so standalone `'m'`
     and `'n'` (used for `呣`, `嗯`, `唔` in `pinyin_mapping.csv`) exist in
     `pinyin_token.csv` only as category `2`. As a result, `public.pinyin_isearch`
     (`sql/pinyin.sql:235-237`) treats `'m'` and `'n'` as prefix patterns (`m%`,
     `n%`) while treating category-3 vowel syllables (`'a'`, `'e'`, `'o'`) as
     exact matches, requiring hardcoded `'m'`/`'n'` workarounds in
     `src/query_modes.rs:144` and `sql/word.sql:296-300`.
- **Contract**:
  - Allow diacritic tone marks on `'n'` in syllabic `'ng'` (`normalized == "ng"`).
  - Unify `'m'` and `'n'` syllabic token representation across `pinyin_token.csv`,
    `pinyin_isearch`, `query_modes.rs`, and `word.sql`.

### 2.3 `generated_pinyin = true` Compatibility with `icu_romanize` / `characters2romanize`

- **Defect**: `icu_romanize` (`sql/word.sql:33-34`) and `characters2romanize`
  (`sql/pinyin.sql:151-152`) emit un-piped syllables for `pinyin_words` phrase
  matches (`w.pinyin`) and only wrap per-character fallback tokens in `|...|`
  from `pinyin_mapping.pinyin` (`'郑爽'` -> `'zheng shuang'`). However,
  `generated_pinyin = true` in `src/query_modes.rs:170-189` and
  `sql/word.sql:271-277` wraps every query pattern in `\|...\|` (e.g.,
  `'\|zheng\|'`), which fails to match any phrase-matched word indexed via
  `icu_romanize` or `characters2romanize`.
- **Contract**:
  - Either format `generated_pinyin = true` patterns to match both piped and
    un-piped tokens (or normalize `icu_romanize` / `characters2romanize` output
    format consistently with `generated_pinyin = true`) and document the exact
    column generator pairing.

### 2.4 `pinyin_isearch` Vowel Separator Bug and Unanchored Fallback Regex

- **Defects**:
  1. `public.pinyin_isearch` (`sql/pinyin.sql:228-237`) resets `sep := ''` after
     emitting a category-3 single-vowel token (`'a'`, `'e'`, `'o'`). For input
     `'xian'`, `'xi'` (category 1) + `'a'` (category 3) + `'n'` (category 2)
     concatenates `'a'` and `'n%'` without a space, yielding `'xi an%'` instead
     of `'xi a n%'` (or greedy `'xian'`).
  2. `pinyin_tokenize` (`sql/pinyin.sql:70`) uses an unanchored alternation regex
     against `pinyin.pinyin_token`, allowing `regexp_matches` to skip unmapped
     consonants (`'xzh'` -> `'zh'`) and match inside words instead of enforcing
     contiguous prefix segmentation.
- **Contract**:
  - Set `sep := ' '` after category-3 tokens in `public.pinyin_isearch` and add
    pgTAP coverage for `characters2pinyin`, `characters2header`, and
    `pinyin_isearch`.

### 2.5 Custom Suffix Overlay Caching and Validation Bugs

- **Defects**:
  1. **Unvalidated suffix identifier and cache pollution**: `validate_suffix`
     (`src/lib.rs:206-214`) checks `[A-Za-z0-9_]` without normalizing case or
     bounding identifier length (`NAMEDATALEN = 64`), and inserts a cache entry
     into `SUFFIX_DICTIONARY_CACHE` (`src/lib.rs:536-561`, `:573-601`) even when
     neither `pinyin.pinyin_mapping_<suffix>` nor `pinyin.pinyin_words_<suffix>`
     exists.
  2. **Silent base fallback on uppercase suffix**: Unquoted PostgreSQL table names
     fold to lowercase, so `pinyin_word_romanize('...', 'Test')` checks
     `to_regclass('pinyin.pinyin_mapping_Test')`, finds `None`, and silently
     returns base dictionary results while caching `'Test'`.
  3. **Cache invalidation blindness when overlay table is created or dropped**:
     `load_suffix_dictionary_version` (`src/lib.rs:344-354`) only queries the
     base `pinyin.dictionary_meta` version when `suffix_meta_exists` is false.
     If `pinyin.pinyin_words_<suffix>` is created and populated without a
     `pinyin.dictionary_meta_<suffix>` table (or after a non-existent suffix was
     cached), the backend continues serving cached base data until `dictionary_meta`
     changes or the session restarts.
- **Contract**:
  - Normalize or restrict `suffix` to lowercase `[a-z0-9_]{1,40}` so
    `pinyin.pinyin_mapping_<suffix>` always fits within PostgreSQL's 63-byte
    identifier limit.
  - Include overlay relation OIDs (`to_regclass`) in the suffix cache state key
    and do not allocate new cache entries when no overlay tables exist for `suffix`.

---

## 3. Upgrade Migration, Packaging, CI, and Data Pipeline Remediation

### 3.1 Upgrade Migration (`scripts/prepare_upgrade.sh`) Does Not Refresh Dictionary Data

- **Defect**: `scripts/prepare_upgrade.sh:104-137` removes all `INSERT INTO
  pinyin.pinyin_mapping`, `pinyin.pinyin_words`, and `pinyin.pinyin_token`
  statements from generated `pg_pinyin--*--*.sql` upgrade scripts, and the
  Rust bootstrap `seed_embedded_dictionary_data()` (`src/lib.rs:241-291`) only
  seeds tables when `SELECT EXISTS (SELECT 1 FROM pinyin.<table> LIMIT 1)` is
  false. As a result, `ALTER EXTENSION pg_pinyin UPDATE` from `0.0.5`–`0.0.8`
  to `0.0.9` leaves pre-existing dictionary tables at their older row counts
  unless manually re-seeded.
- **Contract**:
  - Provide an explicit, non-destructive dictionary sync routine (or idempotent
    `INSERT ... ON CONFLICT DO NOTHING` / versioned refresh helper) during
    extension upgrade so new dictionary entries ship to upgraded databases
    without overwriting user customizations.

### 3.2 Broken RPM FHS Paths, Missing RPM/DEB Dependencies, and Multi-Arch Glob Collision

- **Defects**:
  1. `scripts/package_rpms.sh:45-62` unpacks the Debian `.deb` and repackages
     `/usr` via `fpm -s dir -t rpm`, placing artifacts in Debian paths
     (`/usr/share/postgresql/${pg}/extension/` and `/usr/lib/postgresql/${pg}/lib/`)
     instead of PGDG RPM paths (`/usr/pgsql-${pg}/share/extension/` and
     `/usr/pgsql-${pg}/lib/`), without `--depends` or `--license`.
  2. `scripts/package_rpms.sh:25` globs
     `"$OUT_DIR"/postgresql-*-pg-pinyin_"${EXT_VERSION}"_trixie_*.deb` across
     all architectures instead of filtering by `DPKG_ARCH`, colliding in
     `$tmp_dir/root-$pg` when multi-arch `.deb` artifacts share `$OUT_DIR`.
  3. `docker/Dockerfile.release-trixie:62-77` and `scripts/package_debs.sh:87-95`
     do not assert the CVE-2026-6460 minimum minor versions (`15.20`, `16.16`,
     `17.12`, `18.7`, `19.0`) documented in `spec/pgrx-0.20-upgrade.md` and
     `readme.md:16-29`.
  4. `scripts/package_debs.sh:60,76` globs `"$ext_dir"/pg_pinyin--*.sql` from the
     live `/usr/share/postgresql/$pg/extension` directory without cleaning stale
     `pg_pinyin--*.sql` files first.
  5. Both `pg_pinyin.control` and `src/pg_pinyin.control` exist, while
     `.github/workflows/release-packages.yml:30-41` only validates top-level
     `pg_pinyin.control`.
- **Contract**:
  - Scope `package_rpms.sh` to `_${DPKG_ARCH}.deb`, map or build RPMs for PGDG
    paths with proper `--depends` and `--license`, clean `$ext_dir/pg_pinyin--*.sql`
    before `cargo pgrx package`, enforce the CVE-2026-6460 version floor in
    `package_debs.sh`, and eliminate or cross-check `src/pg_pinyin.control`.

### 3.3 CI Matrix Coverage and `test_query_modes_upgrade.py` Core-Only Gate

- **Defects**:
  1. `.github/workflows/ci.yml:146-161` only runs `prepare_upgrade.sh`, Python
     `unittest`, and full `./test/pgtap/run.sh` on PG18 (`matrix.pg_major == '18'`).
     On PG15, PG16, PG17, and PG19, CI skips `test/pgtap/01_pinyin.sql`,
     `test/pgtap/05_query_modes_core.sql`, and all non-`pg_search` Python tests.
  2. `test/test_query_modes_upgrade.py:58-61` aborts `setUpClass` when `pg_search`
     is not available on the server, preventing
     `test_available_but_uninstalled_pg_search_uses_stub_on_install_and_upgrade`
     (`test/test_query_modes_upgrade.py:540`) from running on core-only PG builds.
- **Contract**:
  - Run `test/pgtap/01_pinyin.sql` and `test/pgtap/05_query_modes_core.sql` across
    all PG15–19 matrix legs, and gate `pg_search`-dependent tests in
    `test/test_query_modes_upgrade.py` via `cls.has_pg_search` (matching
    `test/test_array_batch_upgrade.py:369-370`).

### 3.4 Data Generator (`scripts/data/generate_extension_data.py`) Report Pollution

- **Defect**: For `restore_baseline_only` sources (`cc-cedict-official-2026`,
  `cc-cedict-historical-2019`, `cc-cedict-historical-2013`),
  `parse_phrase_sources` (`scripts/data/generate_extension_data.py:253-257`)
  checks `if word not in baseline: continue` instead of restricting to words
  missing from `initial_p0` (`baseline - initial_p0`). All 157,702 baseline keys
  are re-ingested across three CC-CEDICT snapshots (618,507 `accepted_phrase_rows`
  in `sql/data/manifest.json:103`), polluting `sql/data/reports/conflicts.json`
  (344 of 576 conflicts come solely from unselected `restore_baseline_only`
  sources) and `sql/data/reports/char_reading_mismatches.json` (28 of 160
  mismatches are unselected historical readings). Additionally, line `371`
  re-parses `initial_sources` from scratch after line `368`.
- **Contract**:
  - Restrict `restore_baseline_only` sources to `baseline - initial_p0_keys` (or
    filter unselected `restore_baseline_only` alternatives from `conflicts.json`
    and `char_reading_mismatches.json`) and derive `initial_phrases` in a single
    pass.

---

## 4. Performance Bottlenecks and Optimization Plan

### 4.1 Per-Call SPI Dictionary Version Query on Every Scalar Call

- **Bottleneck**: Every scalar call to `pinyin_word_romanize`, `icu_romanize`,
  `icu_romanize_tokens`, or `characters2romanize` connects to SPI and runs
  `SELECT COALESCE(MAX(version), 0) FROM pinyin.dictionary_meta` (`src/lib.rs:321-334`)
  before touching the thread-local cache. On custom-suffix calls where no
  `pinyin.dictionary_meta_<suffix>` exists (`src/lib.rs:344-354`), it executes two
  SPI queries per call (`to_regclass` + `SELECT COALESCE(MAX(version), 0)`).
- **Remediation**:
  - Cache the validated dictionary version per `(TransactionId, CommandId)` (or
    subtransaction/command counter via `GetCurrentTransactionIdIfAny` /
    `GetCurrentCommandId(false)`) so a multi-row `SELECT`, `COPY`, or index build
    within the same command performs at most one SPI version check while still
    observing intra-transaction dictionary writes on the next command.

### 4.2 19-Substring Hash Probes and Eager Allocations per Han Character

- **Bottleneck**: Because `max_word_len` is `19` (`sql/data/pinyin_words.csv:2482`)
  and `word_map` is a flat `HashMap<String, String>`,
  `romanize_word_tokens_with_maps` (`src/lib.rs:948-964`) probes up to 19 heap-
  allocated `String` candidates for every Han character and eagerly computes
  `romanize_pinyin_phrase(mapped)` (`src/lib.rs:958`) on intermediate prefix
  matches that are later overwritten by a longer match.
- **Remediation**:
  - Store a prefix bitmap or compact character-trie / first-character length mask
    alongside `word_map` so lookahead terminates immediately when no dictionary
    phrase extends the current prefix, and defer `romanize_pinyin_phrase(mapped)`
    until the longest matching `(best_span, best_mapped_str)` is finalized.

### 4.3 Three-Pass Per-Character `String` Allocation in Plain-Text Romanization

- **Bottleneck**: `split_input` (`src/lib.rs:615-633`) allocates a `String` per
  character (`ch.to_string()`), `romanize_plain_text_with_char_map`
  (`src/lib.rs:705-714`) joins them into a single `String`, and `tokenize_plain`
  (`src/lib.rs:740-762`) re-splits and allocates another `String` per character.
- **Remediation**:
  - Fuse `split_input` and `tokenize_plain` into a single pass over `&str` slices
    (or `SmallVec`/char indices) that feeds directly into greedy phrase matching
    without intermediate per-character `String` heap allocations.

### 4.4 Duplicate In-Memory `char_map` Copies and Redundant Suffix Reloads

- **Bottleneck**:
  1. `CHAR_DICTIONARY_CACHE` (`src/lib.rs:34`) and `DICTIONARY_CACHE`
     (`src/lib.rs:41`) store separate full copies of the 44,435-entry
     `pinyin_mapping` table per backend.
  2. When a suffix was previously loaded by `with_suffix_char_cache`
     (`src/lib.rs:525-561`, `words_loaded: false`), `with_suffix_word_cache`
     (`src/lib.rs:581`) unconditionally re-queries `load_suffix_char_map(suffix)`
     via SPI even when `base_version` is unchanged.
  3. `write_temp_csv` (`src/lib.rs:229-239`) aborts with `error!()` on `fs::write`
     failure instead of returning `Result`, defeating the `INSERT` fallback in
     `seed_embedded_dictionary_data` (`src/lib.rs:291-315`) if `/tmp` is full or
     read-only.
- **Remediation**:
  - Share the base `Arc<CharDictionary>` across `CHAR_DICTIONARY_CACHE`,
    `DICTIONARY_CACHE`, and suffix overlays (storing only overlay delta maps for
    suffixes), preserve `entry.char_map` in `with_suffix_word_cache` when
    `entry.version == version`, and return `Result` from `write_temp_csv` so the
    `INSERT` fallback is reachable.

### 4.5 Per-Row SPI + `jsonb_agg` + `serde_json` for Non-`text[]` Tokenizer Inputs

- **Bottleneck**: When `pinyin_word_romanize(anyelement)` receives `pdb.icu` or
  any non-`TEXTARRAYOID` type (`src/lib.rs:805-835`), it runs an SPI SQL query
  per row to cast `$1::text[]`, `unnest`, and serialize to a JSONB string before
  parsing it back with `serde_json` (`src/lib.rs:795`).
- **Remediation**:
  - Cast non-`TEXTARRAYOID` datums to `TEXTARRAYOID` via PostgreSQL's catalog cast
    pathway (`OidFunctionCall1` / cached cast `FmgrInfo` or `ArrayType` extraction)
    and decode the resulting `ArrayType` directly via `extract_text_array_datum`.

### 4.6 Per-Call Catalog Probes in `pinyin_regex_phrase_mode` / `sql_pinyin_regex_phrase_mode`

- **Bottleneck**: `pinyin_regex_phrase_mode` (`src/lib.rs:1468-1511`) and
  `sql_pinyin_regex_phrase_mode` (`sql/word.sql:591-634`) run multiple
  `to_regprocedure(...)` and `to_regtype(...)` catalog lookups on every `'auto'`
  call, whereas `pinyin_phrase` (`src/lib.rs:1540-1589`) resolves native
  `pg_search` capabilities at function creation time.
- **Remediation**:
  - Resolve native `pg_search` capabilities in `pinyin_regex_phrase_mode` and
    `sql_pinyin_regex_phrase_mode` using the same creation-time branch generation
    used by `pinyin_phrase` (or cache the resolved OIDs per backend).

---

## 5. Validation Protocol

1. **Unit and Regression Tests**:
   - Add Rust unit tests (`src/query_modes.rs`, `src/lib.rs`) covering:
     - `葛*` multi-character phrases (`葛优`, `葛洲坝`) and custom 1-character vs.
       multi-character `pinyin_words` precedence.
     - `pinyin_words` phrases containing characters absent from `pinyin_mapping`.
     - Mixed Han+ASCII and punctuation tokens across `pinyin_word_romanize(text)`,
       `pinyin_word_romanize(text[])`, and `icu_romanize_tokens(text[])`.
     - `auto` mode toned chunks with trailing prefixes (`'wúh'`, `'nǐyón'`,
       `'ni3hao'`) and partial exact preservation (`'ni3 whh'`).
     - Syllabic nasal diacritics (`'ńg'`, `'ňg'`, `'ǹg'`) and `'m'` / `'n'` parity.
     - Suffix normalization, 63-byte identifier bounds, and overlay table
       creation/drop cache invalidation.
2. **pgTAP and Upgrade Matrix**:
   - Run `test/pgtap/01_pinyin.sql`, `02_pg_pinyin_extension.sql`,
     `03_pg_search_integration.sql`, `04_parallel_read_only.sql`, and
     `05_query_modes_core.sql` across PostgreSQL 15–19.
   - Verify `ALTER EXTENSION pg_pinyin UPDATE` from `0.0.5`–`0.0.8` refreshes
     dictionary row counts to `44,435` chars / `196,187` multi-char phrases /
     `436+` tokens while preserving user overrides.
3. **Benchmarks**:
   - Measure scalar `pinyin_word_romanize(text)`, `pinyin_word_romanize(pdb.icu)`,
     and `pinyin_word_romanize_batch(text[])` throughput and memory before and
     after command-scoped version caching, prefix-pruned phrase lookup, and
     single-pass tokenization.
