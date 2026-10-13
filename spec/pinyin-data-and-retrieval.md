# Pinyin Data Integrity and Retrieval Optimization

Status: P0, opt-in P1 query modes and native arrays/document batches implemented and locally validated in unreleased development version 0.0.9; remaining P1/P2 proposed. Date: 2026-10-13.

## Scope

Keep the Rust extension and optional SQL backend independent. Do not change the
existing public API, runtime parallel-safety contract, editable dictionary tables, or
suffix override precedence. No generic stopword list is applied in P0. The
pre-existing untracked `doc/` directory is not part of this work.

P0 repairs dictionary normalization and imported syllable boundaries. It does
**not** promise contextual pronunciation disambiguation or unambiguous parsing
of a joined Latin query. Applications can pass explicit spaces or apostrophes:
`xi an` / `xi'an` versus `xian`. The existing regex helper accepts ASCII letters,
whitespace and three explicit apostrophe forms, splits initials such as `whh`,
and prefix-expands each syllable with `.*`.

## P0 Data Contract

Canonical storage is lower-case, tone-free ASCII. `ü`, its four tone forms,
decomposed `u` plus diaeresis, `u:` and `v` normalize to `v`. Remove tone accents
only after preserving the diaeresis. Accept numeric tone suffixes 0-5 in source
syllables. Do not turn ordinary `ju`, `qu`, `xu` into `jv`, `qv`, `xv`; do not
silently discard punctuation or concatenate syllables.

The character source stays at the existing pinned pinyin-data revision.
Introduce a pinned phrase-pinyin-data submodule and consume these selected files
plus the raw CC-CEDICT snapshots retained for released-key coverage:

| Source | Priority | Role |
| --- | ---: | --- |
| `overwrite.txt` | 300 | Upstream manual corrections |
| `pinyin.txt` | 200 | Base boundary-preserving phrase readings |
| `cc_cedict.txt` | 100 | Additional phrase/entity coverage and alternatives |
| Raw CC-CEDICT 2026-10-12 | 50 | Current explicit syllables and both written forms; baseline keys only |
| Raw CC-CEDICT 2019-05-27 | 40 | Historical coverage absent from the current snapshot |
| Raw CC-CEDICT 2013-08-28 | 30 | Older released coverage; original 3.0 notice retained |

The source format supplies one space-separated syllable per character. Preserve
that sequence without joining and guessing boundaries. Fold upstream `_N`
alternative keys into the same word, retaining the original key and line in
provenance. One complete canonical sequence is selected by priority, source ID,
then source line. Never create per-character Cartesian combinations from
alternative phrase readings. The runtime table still contains one reading per
word; every alternative is retained in the conflict report for review.

Reject/report unsupported keys, malformed pronunciations, unknown syllables,
non-phrase records and character/syllable count mismatches. Contracted erhua
needs an explicit alignment model in future; P0 reports rather than guesses it.
Readings absent from the corresponding character's reading list are diagnostics,
not automatically errors: context, neutral tones and incomplete character data
can be legitimate. This count is not a measured error rate.

### Coverage Preservation

The first source replacement lost 64,091 of the 157,702 released keys because
phrase-pinyin-data's CC-CEDICT converter exports only the simplified headword.
Most missing keys are traditional forms; the valid singleton `葛` was also
excluded by the new phrase-only gate. This is a source-selection regression,
not evidence those words are invalid. More unrelated new words do not offset it.

Pin the released CSV as a comparison-only baseline. Consume raw, hash-locked
CC-CEDICT V1 data with both headword columns and explicit syllables to restore
the old key set. Restoration has lower precedence than P0 manual/base readings,
is scoped to baseline keys, and must never import the old joined pronunciation.
Retain raw numbered syllables and source line references. Historical sources
are explicitly labeled historical, not represented as current confirmations.
For explicit erhua `r5` aligned to `儿/兒`, serialize `er` to match the existing
index convention and record this compatibility policy; it is not a linguistic
claim that rhotacization is an independent pronounced `er` syllable.

Generate `coverage_audit.json` covering all original missing keys and changed
retained readings. Require every released key to survive and every restored or
changed sequence to have selected source provenance. Fail before replacing
outputs on any unresolved deletion; same-size or larger replacement dictionaries
must not bypass this guard. Add exact traditional/umlaut/boundary regressions,
independent of dictionary-generated expectations.

## Reproducible Import

`scripts/data/sources.lock.json` declares source revisions, paths, SHA-256,
licenses and priorities. Submodule gitlinks pin bytes; the generator verifies
all selected source hashes before writing. No implicit network download,
machine-local sibling repository, optional `ok.json` normalization or missing
phrase fallback is allowed. Any update requires an explicit lock change.

Generate CSVs in stable ordering with LF endings, plus deterministic manifest,
conflict, rejection and character-reading diagnostics. Include input/output
hashes, policy and counts; exclude wall-clock timestamps and absolute paths.
Validate/build the complete result before replacing outputs. Missing/corrupt
sources must fail without replacing a valid dictionary with empty content.
Two clean rebuilds from the same lock must have byte-identical outputs/reports.

Retain source notices; repository MIT licenses do not erase upstream rights.
CC-CEDICT-derived phrase data is attributed and distributed under CC-BY-SA-4.0.
The 2013 CC-CEDICT snapshot retains its original CC-BY-SA-3.0 notice and license;
the derived extract uses the permitted later-version adaptation license, not
a claim that the original data was published under 4.0.
Do not import merged `large_pinyin.txt` or extra scraped sources by default.
See `sql/data/NOTICE.md`. The old opaque joined-CSV input is no longer a default;
an explicitly pinned CSV must already preserve syllable boundaries.
DEB/RPM packages must include the data notice, upstream MIT notices, CC-BY-SA
3.0/4.0 license texts, sources lock and generated manifest alongside the code license.
Packaging refuses absent attribution files.

## P0 Validation

- Unit tests cover composed/decomposed umlauts, tone numbers, invalid input,
  source priority, duplicate/alternative reconciliation and no-overwrite errors.
- Exact gold fixtures cover umlauts, 西安/善感/阿奶 boundaries, polyphones and
  unaffected controls. These are curated regression cases, not a representative
  estimate of Mandarin pronunciation accuracy.
- Audit every output row for alignment, canonical alphabet and duplicate keys.
- pgTAP tests inspect the installed embedded dictionary, both backends, exact
  generated pattern arrays, `whh -> w h h`, real BM25 retrieval, and existing
  parallel read-only regressions.
- Fresh installation embeds corrected data. Existing tables/generated columns
  are not silently overwritten: reseeding is explicit, affects custom base
  dictionaries, and requires generated-value refresh/index rebuild. No release
  version bump or automatic destructive data migration is included here.

## P0 Benchmark Protocol

Use a fresh unique database for each variant, and a fresh backend plus rebuilt
tables/indexes for every repeated build task. Compare committed pre-P0
CSV data and the candidate CSVs under **the same compiled runtime**, same rows,
same PostgreSQL/pg_search/configuration. At least 20,000 deterministic phrases
stratified across retained, restored and new key pools, plus curated gold cases;
record pool/sample hashes and fixture composition. Both variants use identical
rows even when a word has no phrase match in one variant. Hash-order IDs so gold
fixtures do not systematically win score ties by receiving all the smallest IDs.
This controls code changes but does not represent an application traffic sample.

Separate fresh-backend cache load, repeated warm romanization, field generation,
index build and warm full-query latency. Rotate variant order over >=3 runs.
Report medians and p95 where applicable, data hashes, exact output checksums,
actual BM25 relation sizes and generated byte/token counts. Do not claim field
byte counts are index sizes. PostgreSQL "cold" here means fresh backend/cache,
not flushed OS/page cache. Embedded Rust regex tokens are candidate tokens in
both variants; state this limitation explicitly.

Measure targeted fixture exact readings and real target retrieval separately
from synthetic corpus Recall@k/MRR (candidate-derived homophone labels).
Synthetic query labels cannot independently prove linguistic accuracy. Preserve
ranking/IDs in correctness checks outside timed loops; do not reduce verification
to aggregate `count(*)`. SQL pipe-delimited multi-readings and Rust first-reading
outputs are intentionally distinct semantics, not interchangeable checksums.

The acceptance gate is exact P0 regressions fixed, deterministic imports, no
parallel-safety regressions and measured performance/size changes, not an assumed
speedup or index reduction. Correctness may increase token count/index size.

## P1: Retrieval Semantics and Index Reduction

The first increment adds opt-in query semantics, without changing defaults:

- `public.pinyin_regex_phrase_mode(value, mode, slope, max_expansions, generated_pinyin)`
  is an extension-owned Rust-backend query helper. Its pattern core is
  `pinyin_regex_phrase_patterns_mode(value, mode, generated_pinyin)`.
- Optional `sql/word.sql` exports independent `sql_`-prefixed equivalents using
  live token tables. No Rust API is required for the SQL implementation.
- Separate names avoid ambiguity in existing untyped-NULL calls; do not add a
  second-text overload to the legacy functions.
- `full` uses exact syllable term patterns, `initials` splits compact single
  letters (explicit `zh`/`ch`/`sh` chunks are digraphs), `autocomplete` expands
  only the final token, and `compat` delegates the original behavior.
- In the final non-numeric-tone autocomplete chunk, each remaining suffix is
  first checked as a whole valid syllable prefix before greedy splitting.
  Thus `yon`/`lon` remain one token, and `niyon` becomes `ni`, `yon.*`.
- The new normalizer uses NFD, preserves umlauts as `v`, accepts `u:`/`v`, removes
  valid tone marks, and preserves numeric tones/apostrophes as boundaries.
  Malformed/unsupported inputs produce empty arrays, unknown modes fail with
  `22023`, SQL NULL remains NULL. Limits are 256 original Unicode scalars and
  64 output tokens, with no truncated searches.
- Complete syllables use token categories 1/3 plus syllabic `m`/`n`. Joined
  chunks still use a single longest-first path; explicit spaces/apostrophes are
  authoritative. Automatic ambiguous-path expansion is not part of this work.
- New generated-pinyin mode accepts both plain word-hit terms and pipe-bearing
  alternatives, with closed syllable boundaries. Whitespace index expressions
  retain pipe characters; structured queries use the original field as their
  left operand. Simple tokenization is not an equivalent storage contract.
- Rust parsing remains SPI-free and parallel safe. Development version 0.0.9
  includes a catalog-only 0.0.8 upgrade; dictionaries/generated values are not
  silently replaced. SQL backend installation remains separate.

Independent adversarial fixtures must measure exact pattern arrays, positive and
negative real BM25 results, precision/false positives and recall/MRR by intent.
SQL and Rust results must agree within a mode; different modes need not agree.
Use a fresh independent P1 benchmark/database/report, not overwrite P0 evidence.
Keep constructor-only and full indexed timing separate, use rotated >=3 repeats
on 20,000 rows / 2,000 queries, preserve actual IDs/hashes/plans and index bytes.
Identical index content across modes is not an index-size optimization.

Local validation on 2026-10-13 used PostgreSQL 19 beta 4 and pg_search 0.26.1:
69 Python tests, 738 pgTAP assertions and 16 Rust unit tests passed. The genuine
0.0.8 catalog is generated in an isolated baseline Cargo target, with a guard
against accidentally reusing the newer shared-library/schema artifact. Upgrade
tests retain old function OIDs, live dictionaries and generated values. The
forced-worker pgTAP probe executes both the public query constructor and its
pattern core. Additional isolated checks cover sequential operator fallback,
private exact bitmap intersection and commit/rollback/VACUUM visibility.

pg_search 0.26.1 has no official PG19 package. Local testing built its pinned
source with the repository's PG19 compatibility patch in a cached SDK overlay;
this is experimental compatibility, not full upstream support or a clean
end-to-end build of the complete Docker recipe. Shared/lossy bitmap paths are
not covered by these smoke tests. CI also targets PG18, but this local run is
PG19 only. The P1 benchmark records the same extension version and the shared
host conditions; its report is separate from historical P0/general results.

The completed P1 run ([report](../benchmarks/p1_query_modes.md),
[raw JSON](../benchmarks/p1_query_modes.json)) used 20,000 phrases, 2,000 queries
per method and three rotated repeats. All 57 independent pattern cases passed;
30 adversarial retrieval cases were recorded, with the new modes satisfying
their positive/negative contracts and legacy false positives kept visible.
Ranked backend parity and independently expected autocomplete arrays passed for
every corpus input.
In the three matched exact-intent contrasts, four visible compat false positives
became zero in full, retaining all seven labeled positives. These are small
targeted/top-k checks, not general accuracy or complete false-positive bounds.
Plain full-query medians were 400.313 ms (Rust) and 1,167.757 ms (SQL). Rust full
was faster than compat's 1,378.673 ms on those inputs, but the matching semantics
differ, so this is not a parser-only speedup. The dual-field index remained
3,514,368 bytes in all three builds. Pipe-wrapped fields were substantially slower;
shared-host load and roughly 33.3 GB swap rule out stable speedup claims.

Remaining proposals require independent labeled evaluation before defaults change:

1. Evaluate the opt-in modes on independent business query labels before
   changing defaults. Keep compatibility and Chinese/unfiltered fallback
   available; curated adversarial checks are not general retrieval accuracy.
2. Normalize explicit boundaries consistently across the existing interfaces.
   Keep single-path joined parsing; automatic candidate expansion was rejected
   on 2026-10-13 in favor of supporting explicitly separated user input.
3. Validate the native `text[]`/document-batch increment below, preserving cache
   invalidation and parallel safety before accepting performance claims.

4. Optional conservative function-word filtering: tokenize/POS/entity-protect
   before romanization, keep original Chinese and full-pinyin fallback. Protect
   names, titles, fixed phrases, quoted text, negation, scope and direction.
   Never delete homophonic `de`/`he` after conversion or union public stoplists.
   Query/index policies must match; preserve positions where phrase semantics
   require them. Compare full, filtered and entity/keyword fields separately.

## Default/Compat Apostrophe Boundaries

On 2026-10-13 the uncommitted multi-candidate increment was discarded, including
its APIs, tests, scripts and reports. No candidate functions or Boolean query
expansion are shipped. Native-array/document-batch work was merged separately
before this branch was rebased onto main.

Default/compat Rust and independent SQL helpers treat U+0027, U+2018 and U+2019
as hard whitespace boundaries. Never remove them and concatenate chunks.
`xi'an` yields `xi.*`, `an.*`; `xian` remains `xian.*`. `xin'gan` yields
`xin.*`, `gan.*` while `xingan` retains the old longest-first result. Repeated
and leading/trailing separators behave like whitespace; separators alone yield
empty arrays / pdb.empty(), while SQL NULL keeps the existing behavior. Other
punctuation still invalidates the entire input. No default tone/umlaut grammar,
token limits, prefix patterns, generated patterns or initials are changed.
Existing full/autocomplete boundary parsing and exact generated patterns remain
unchanged. Rust uses the same embedded SPI-free dictionary and a common lightweight
normalizer for patterns/token counts; SQL continues to use its editable token
table. Standalone Python benchmark helpers accept the added explicit boundaries
without importing Dispatcher or enumerating alternate paths.

Acceptance includes exact default/compat/generated arrays, old ASCII output
parity, pattern/count consistency, smart/ASCII quote and malformed-input cases,
real BM25 plain/pipe positives and one-token-xian negatives, standalone SQL loading,
parallel-worker execution and genuine fresh/upgrade catalogs. Existing signatures
and OIDs remain unchanged; no extra version bump or migration functions are needed.
Use fresh local databases for validation and do not reuse the discarded P3 results.

Before the rebase, local validation of the independent main-based branch on
2026-10-13 passed 26
Rust unit tests, 53 Python integration/protocol tests, 22 data-pipeline tests and
1,079 pgTAP assertions across six files on PostgreSQL 19 beta 4 / pg_search 0.26.1.
Both no-feature and PG19 library/benchmark-binary checks, `cargo fmt --check` and
data regeneration passed; generated dictionary files were byte-identical.
Genuine 0.0.8-to-0.0.9 upgrade and fresh-install checks preserved legacy function
OIDs/catalog metadata and exercised apostrophe normalization below Gather in
actual read-only workers. SQL-only loading and BM25 plain/pipe boundary negatives
passed without the Rust extension. Suite counts exclude the separate array/batch
PR; the earlier combined checkout had additional tests. The PG19 source-build
compatibility limitations noted above still apply. Historical P1 benchmark
reports remain unchanged; no new timing or speedup claim is made for this grammar
fix. The expanded current fixture is newer than the P1 report.

## Native Arrays and Explicit Document Batches

The next increment extends unreleased development version 0.0.9 and its existing
0.0.8 upgrade. The latest published release remains 0.0.8; no extra version bump
or separate migration is introduced.
It does not change query modes, dictionaries or the independent SQL backend.

- Existing `pinyin_word_romanize(anyelement[, suffix])` recognizes exactly the
  PostgreSQL `text[]` OID and decodes it directly with pgrx `Array<&str>`.
  Other castable types retain their SPI/JSON fallback. No extra legacy overload
  is introduced: old function OIDs, untyped calls and generated-column bindings
  remain intact. Trim/drop-empty/ASCII-case behavior is preserved; NULL tokens
  remain errors rather than silently disappearing.
- `public.pinyin_word_romanize_batch(text[][, suffix]) RETURNS text[]` treats
  elements as independent plain-text documents. NULL/empty positions are kept;
  multidimensional/non-1-based arrays flatten in row-major order to a 1-based
  one-dimensional result. Empty/all-NULL batches need no dictionary lookup.
- One explicit batch uses one version check/live dictionary snapshot. Regular
  calls still check every invocation. Cache keys include the version counter
  and metadata-row xmin: a rollback followed by another mutation can reuse the
  counter without an intervening read, but must never reuse the stale snapshot.
  Suffix clear-cache and base invalidation remain intact. New functions are directly exported
  Rust `STABLE STRICT PARALLEL SAFE`, not SQL wrappers or global transaction
  snapshots. Existing scalar volatility is outside this increment's scope.
- The migration adds only the new APIs; it never reseeds live dictionaries or
  rewrites stored generated values. Applications must use ordinary output
  columns for batch INSERT, not the STABLE batch function in generated columns.

Acceptance requires native/fallback parity, independent pronunciation gold,
NULL/TOAST/dimension tests, suffix/base mutation and rollback checks, genuine
0.0.8-to-0.0.9 upgrade preserving old OIDs/generated expressions, and actual launched
parallel workers below Gather in a read-only transaction without assigned XIDs.

`scripts/benchmark_array_batch.py` compares an independently rebuilt real P1
pre-optimization commit `f1094be` DLL with the working DLL in a fresh UUID
database. Both are unreleased 0.0.9 development snapshots. The old DLL has a
separate filename and C aliases, guarded by pinned commit and library/SQL
SHA-256 manifests; a simulated JSON wrapper is not an acceptable baseline.
Require both DLLs to be release builds with matching compiler versions and
verify the installed current DLL against its Cargo release build artifact.
Select 20,000 normalized Han-only phrases by deterministic hash, verify every
output against source readings, and keep independent restored/umlaut/mixed/NULL
gold outside the timing corpus. Time old/new scalar and precomputed character
token paths, bounded document batches of 1/32/128/512/2048, and fresh unindexed
INSERTs separately. ICU-only timing is not charged to precomputed arrays.
Use at least three rotated repeats, JIT off/parallel off, new-backend cold and
warm phases, forced count/bytes/ordered-ID-output hashes and full untimed parity.
Report raw samples, medians, source/runtime/settings/host metadata and cleanup.
This measures conversion/heap+PK INSERT, not BM25 index maintenance or index
compression: output bytes must remain identical. Results belong in a separate
`benchmarks/p2_array_batch.md` / `.json`, not historical P0/P1 reports.

Local validation on 2026-10-13 used PostgreSQL 19 beta 4 / pg_search 0.26.1:
88 Python tests, 875 pgTAP assertions and 16 Rust unit tests passed. Genuine
0.0.8-to-0.0.9 upgrades retain all old function OIDs, stored generated expressions
and values, live dictionaries and token rows. Fresh/upgrade catalogs agree
with and without pg_search. Forced parallel probes launch workers and execute
native/batch/suffix calls below Gather without assigning an XID in read-only
transactions. Real external TOAST arrays and all eight base/suffix/char/native/
batch cache paths pass the reused-counter, distinct-xmin rollback regression.
Dictionary regeneration is deterministic and sql/data is unchanged.

The [completed report](../benchmarks/p2_array_batch.md) and
[raw JSON](../benchmarks/p2_array_batch.json) verify all 20,000 source/ID/output
rows for every method and INSERT phase, plus 12 separate handwritten document
gold cases. Both release-profile DLLs use rustc 1.98.1; current installed DLL
matches its build artifact, and old/current artifacts are distinct despite
sharing the unreleased 0.0.9 version. Generation warm medians are 623.978 ms
for the P1 token-array snapshot and 261.685 ms for native arrays (observed 2.38x).
INSERT medians are 649.027 and 277.513 ms respectively (2.34x). Batch 512 gives
23.418 ms conversion / 41.592 ms INSERT versus old scalar 300.489 / 321.993 ms
(12.8x / 7.74x). Batch 128 INSERT is close at 41.774 ms; 2048 is slower at
61.087 ms, so no universal optimal batch size is established. The independent
ICU-only warm median is 48.015 ms and is not charged to precomputed arrays.
The shared host used 31946.69M swap before timing; these are exploratory
server-side conversion/unindexed INSERT observations, not client upload or
BM25-index-maintenance throughput and not an index-size optimization.


## P2: Sources and Evaluation

Candidate sources are not automatically ingested or treated as independent gold:

| Source | Purpose | Limits |
| --- | --- | --- |
| Unicode Unihan newer release | Character-reading delta audit | Default readings are not context gold |
| CC-CEDICT official snapshots | Readings, variants, traditional/simplified coverage | Attribution/share-alike; multiple senses |
| CPP / g2pM / g2pW | Polyphone held-out evaluation or offline ingest models | Split leakage, model/domain bias, source text rights |
| UD Chinese GSD | POS/function-word policy evaluation | Not a pronunciation gold corpus |
| Wikidata | Typed entity/name/alias protection | Not pinyin or real query gold |
| OSM | POI/road/station names and aliases | ODbL; domain coverage and attribution |
| T2Ranking / MIRACL zh | Relevance/latency evaluation | Generated pinyin queries are synthetic |

Keep source versions, licenses, split IDs and reconciliation evidence. Collect
domain-specific held-out real queries including names, verbs, long sentences,
mixed script, homophones, initials, typos, tone input and ü. Measure Recall@1/10,
MRR/nDCG, zero-hit/false-positive rates, p50/p95, build time, token count and
actual index bytes. Audit worst regressions; do not judge by dictionary coverage
or a toy benchmark alone.

## Primary References

- [Phrase source format and provenance](https://github.com/mozillazg/phrase-pinyin-data)
- [CC-CEDICT data and license](https://www.mdbg.net/chinese/dictionary?page=cc-cedict)
- [Unihan properties](https://www.unicode.org/reports/tr38/)
- [g2pM and CPP](https://github.com/kakaobrain/g2pM)
- [g2pW](https://arxiv.org/abs/2203.10430)
- [UD Chinese GSD](https://github.com/UniversalDependencies/UD_Chinese-GSD)
- [Wikidata dumps](https://www.wikidata.org/wiki/Wikidata:Database_download)
- [OSM name schema](https://wiki.openstreetmap.org/wiki/Names)
- [T2Ranking](https://github.com/THUIR/T2Ranking)
- [MIRACL](https://github.com/project-miracl/miracl)
- [Bounded/error-tolerant pinyin input research](https://www.ijcai.org/Proceedings/11/Papers/424.pdf)
- [Name matching representations](https://aclanthology.org/P15-2062/)
- [Chinese abbreviation ambiguity](https://aclanthology.org/2022.acl-long.133/)
- [Token positions and stopwords](https://www.paradedb.com/blog/when-tokenization-becomes-token)

Benchmark results are recorded separately in `benchmarks/p0_dictionary.md` with
machine-readable details in the adjacent JSON. They are measurements of the
tested setup, not an upstream pg_search compatibility guarantee.

## P0 Evidence

The initial P0-only selection had 132,097 keys (93,611 shared with v0.0.8,
38,486 new, 64,091 missing). Its common-only experiment did not validate lost-key
coverage and is superseded by the coverage-preserving audit and stratified run.
Current evidence and exact counts are recorded in the generated coverage report
and `benchmarks/p0_dictionary.md`; no stable speedup or reduced index size is
an acceptance assumption. The SQL loader and benchmark setup explicitly ANALYZE
all dictionaries. Existing databases still need an explicit dictionary reload,
generated-value refresh and index rebuild; no destructive automatic migration
is introduced.

The final import has 196,188 word keys: all 157,702 released keys plus 38,486
new P0 keys. All 64,091 initial omissions are restored, including 63,038
traditional-only selected forms, 823 simplified-only forms, 229 forms occurring
in both source columns and one explicit mixed-form alias. Of these restorations,
61,868 normalized sequences are unchanged and 2,223 differ from the legacy CSV;
2,998 shared old-key sequences also change. These are reconciliation counts,
not independently verified pronunciation error rates.

The selected restoration evidence comes from the current official snapshot for
58,956 keys (including the alias), 2019 for 3,518 and 2013 for 1,617. The generated
[coverage audit](../sql/data/reports/coverage_audit.md) links the full per-word
proof. All source-derived output phrases are aligned; 576 conflict groups and
160 character-reading diagnostics remain visible, with zero rejected baseline
records. Two complete rebuilds are byte-identical. Python tests pass 43 cases,
and the PG19 beta4 fresh-install/pgTAP suite passes 233 assertions, including
parallel read-only regressions. Rust library/benchmark checks and formatting
pass. These results do not imply validation on every supported PostgreSQL major.

The fresh PG19 beta4 / pg_search 0.25.1 run uses 20,000 phrases (9,555 retained,
6,531 restored, 3,914 new) and 2,000 queries over three rotated repeats. Curated
exact word readings improve from 33/55 to 55/55; the 15 restored-word gold cases
are included in this total. Curated Rust Recall@10 / MRR changes from
0.6000 / 0.4606 to 1.0000 / 0.8273. The other 1,945 queries are dictionary-derived
retrieval-consistency checks, not independent pronunciation gold.

Rust text warm conversion medians are 333.270 / 292.435 ms (-12.25%), while full
query batch medians are 1,841.729 / 1,867.646 ms (+1.41%). SQL exact-array warm
conversion changes from 3,015.929 to 3,708.735 ms (+22.97%). The legacy SQL
phrase-or-character path falls from 97,718.343 to 2,561.951 ms on the identical
union-key corpus. Avoiding character fallback on newly covered phrases is a
plausible explanation, not an isolated profile of the fallback cost or a parser
algorithm improvement. Median field-generation and BM25 build times do not
improve: 1,322.311 / 1,395.744 ms and 60.390 / 83.210 ms, respectively.

Word-field bytes increase 0.19% and whitespace tokens 0.64%; measured BM25 bytes
remain 3,309,568 for both variants (all table/index bytes 5,824,512). This is no
index reduction. Host swapping and large timing variation limit performance
inferences from three repetitions; preserve raw runs rather than interpreting
these medians as a statistically established speedup.
