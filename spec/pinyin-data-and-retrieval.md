# Pinyin Data Integrity and Retrieval Optimization

Status: P0, opt-in P1 query modes, native arrays/document batches and opt-in native exact queries implemented and locally validated in unreleased development version 0.0.9; remaining P1/P2 proposed. Date: 2026-10-13.

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
Explicit boundary normalization and the native exact-query measurement have been
completed in the increments below; they are no longer remaining proposals.

1. Evaluate the opt-in modes on independent business query labels before
   changing defaults. Keep compatibility and Chinese/unfiltered fallback
   available; curated adversarial checks are not general retrieval accuracy.
2. Optional conservative function-word filtering: tokenize/POS/entity-protect
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
2026-10-13 passed 26 Rust unit tests, 53 Python integration/protocol tests,
22 data-pipeline tests and
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

## Opt-In Native Exact Queries

PR #5 is rebased onto main after the array/batch PR merged. This increment adds
`public.pinyin_phrase(value text, slope integer DEFAULT NULL)` and independent
`public.sql_pinyin_phrase(value text, slope integer DEFAULT NULL)`. Do not replace
the existing `full` regex route: upstream term/phrase scoring is not equivalent.
Keep existing signatures, OIDs, mode grammar and pipe/initials/autocomplete/cap
behavior intact; do not revive automatic segmentation mutations.

Both helpers use their own existing `full`, plain pattern parser. NULL input
returns NULL before argument validation; negative slope on non-NULL input fails
with `22023`; empty patterns become `pdb.empty()`. One syllable becomes exact
`pdb.term`, ignoring nonnegative slop. Several syllables use `pdb.phrase_array`,
which does not invoke the field search tokenizer; positive slop is attached with
`paradedb.query_to_slop` / `paradedb.slop_to_query`. NULL/0 slop means adjacency.
The wrapper's final boolean is `_is_explicit`, not an ordering flag; slop follows
the shared phrase scorer (a gap costs 1, a transposition costs 2).
Fresh validation also exposes repeated-term position reuse in pg_search 0.26.1:
`an an` with slop 1 matches a single `an` position in both native and existing
regex paths. Keep zero-slop repetition in the independent exact gold; separately
guard and disclose this upstream positive-slop limitation, not lower the gold's
occurrence requirement or claim linguistic correctness from backend agreement.
There is no regex cap argument or generated-pinyin storage option.

The Rust extension owns its final public helper and uses embedded SPI-free
tokens. `sql/word.sql` remains optional and independently live-table-backed.
Fresh installs and the existing unreleased 0.0.8-to-0.0.9 upgrade add exactly one
extension function with `STABLE`, `PARALLEL SAFE`, not `STRICT`. No version bump
or dictionary/generated-column mutation is included. Without enabled pg_search,
install a text-returning error stub as with existing query helpers; if pdb.query
exists but native constructors/slop wrappers are missing, retain its return type
and raise an actionable upgrade error. Do not silently substitute regex scores.

Acceptance covers independently labeled exact hit sets, repeated/gapped/reversed
syllables, slop 0/1/2, apostrophe/umlaut boundaries, NULL/invalid inputs, SQL-only
installation, fresh/upgrade catalog parity, retained regex expansion behavior
and actual read-only parallel-worker execution. Verify native Rust/SQL scores
and ranked IDs agree. Compare native versus regex full hit sets separately from
scores, standalone rankings and mixed-OR rankings; score differences are expected
and must remain visible rather than relabeled as equivalent results.

Use a dedicated fresh UUID database and report, a single plain BM25 index,
20,000 deterministic phrases and 2,000 queries, at least three rotated repeats.
Compare current unchanged Rust/SQL regex-full constructors with new native
helpers under the same compiled library/parser/index. This isolates construction
and execution paths, not a new parsing algorithm. Measure actual pdb.query
materialization, fresh-backend first-call cost, warm construction, and warm client
indexed queries separately. Record raw repeats, median/p95, output hashes,
untimed complete hit-set and score/rank checks, plans, source/library hashes and
actual index bytes. A fresh backend is not a flushed OS cache; the identical
index cannot establish an index-size reduction. Do not overwrite P0/P1/P2 reports.

Upstream source audit is pinned to pg_search 0.26.1 and its Tantivy revision:
single regex uses constant scoring while native term uses BM25. General regex
phrase statistics refer to pattern literals; full/plain uses bare syllable
literals, so its phrase statistics may coincide with native terms. Do not claim
that every multi-syllable full query changes scores. The single-term difference
can affect combined-query rankings. Native zero-slop phrase also
has a different pruning path; measured workload results, not this observation,
determine whether the new route improves performance.

The dedicated 2026-10-13 [report](../benchmarks/native_phrase.md) and
[JSON](../benchmarks/native_phrase.json) record 20,000 phrases, 2,000 queries and
three rotated repeats on PG19 beta4 / pg_search 0.26.1. The sort expression uses
direct `pdb.score(id)` rather than a float cast; every recorded plan is verified
as `TopKScanExecState`, not just any ParadeDB scan. The earlier cast-sort trial
was discarded and the complete benchmark rerun in a new UUID database.
Warm full-client batch medians: Rust regex 366.692 ms versus native 282.677 ms
(22.9% lower elapsed time); SQL regex 1,037.258 ms versus native 869.343 ms
(16.2% lower). Actual query-constructor medians on all 20,000 inputs are
49.321 / 45.005 ms (Rust) and 5,560.614 / 5,530.890 ms (SQL). Parsing is unchanged;
this is an execution/construction-path experiment, not a parser-only speedup.
All 2,000 timed inputs are multi-syllable (2-10 syllables): full hit sets, scores,
ranked IDs and top-k agree across all methods. Independent single-term/mixed-OR
gold confirms score and ranking differences, so the new API remains opt-in.
The shared index is 3,350,528 bytes, unchanged by query strategy. The host's
28,247.94M swap usage and shared workloads prevent universal/statistical speedup
claims. This uses the cached experimental PG19 pg_search source compatibility
image, not an upstream official PG19 package or a new full Docker recipe build.

Final local validation passed 26 Rust unit tests, 90 Python integration/protocol
tests, 22 data-pipeline tests and 1,356 pgTAP assertions across eight files.
`cargo fmt --check` and library/benchmark-binary checks pass without PostgreSQL
features and with PG19. Genuine released 0.0.8 upgrades match fresh installs,
including the exact native function definition; legacy OIDs/catalog metadata,
live dictionaries and stored values remain intact. Missing pg_search and missing
native API stubs, standalone SQL loading, query construction below Gather in
actual read-only workers without an assigned XID, and the custom analyzer
pretokenization counterexample pass. Generated dictionary files are byte-identical.
No version metadata, package release or existing historical benchmark is changed.

### Default Regex Tradeoff Follow-Up

Add a separate reproducible benchmark of the unchanged default
`pinyin_regex_phrase(value, slope, NULL, false)` and native `pinyin_phrase`, with
the explicit `full` regex path as a control. Run both independent backends on
the same 20,000 dictionary phrases, 2,000 complete queries and shared plain-field
index in a fresh UUID database. Rotate six methods over six repeats. Preserve
the previous full-regex report and record all matching IDs, not only top-k.

Count actual set intersections, default-only and native-only hits; show example
documents for scope differences. Require Rust/SQL agreement within each strategy
and full-regex/native hit-set agreement on the complete-query workload. Report
timings for changed and unchanged hit sets separately where available; never
attribute all default-to-native speedup to query construction alone.

Use independent exhaustive positive/negative labels for complete syllables,
initials, final completion and normalized spelling. Do not use native output as
the relevance oracle. Report micro-aggregated TP/FP/FN/TN per intent, precision,
recall, false discovery rate and false-positive rate with explicit denominators;
undefined rates remain null, not zero. Dictionary-derived scope differences
measure retrieval semantics, not general user relevance or pronunciation
accuracy. A prefix-only hit may be incorrect for a complete input but relevant
for completion. Native empty results for unsupported partial inputs must not
be counted as a faster successful search.

End-to-end warm timing includes helper construction, actual BM25 score Top-K,
client round trips and returned IDs/scores, excludes setup and full-hitset
validation, and verifies measured output against each strategy's own preflight.
Separate fresh-backend constructor costs from warm constructor costs; do not
describe backend-first-call costs as flushed disk/OS-cache measurements. Record
the actual pg_search version, library/source/fixture hashes and raw repeats.
No product behavior, version, migration or index data changes are needed.

The completed [2026-10-14 report](../benchmarks/native_phrase_tradeoff.md) and
[JSON (gzip)](../benchmarks/native_phrase_tradeoff.json.gz) use the same release-mode
extension library on PG19 beta4 / pg_search 0.26.1, with all six methods passing
actual Top-K, complete-set, backend parity and timed-output checks. Of 2,000
inputs, 355 change hitsets and 1,645 do not. Default has 5,366 query/document
matches, native 3,477, intersection 3,477, default-only 1,889 and native-only zero.
This 35.20% reduction measures scope, not relevant recall. Scores differ for all
2,000 default/native queries; 354 change top-k IDs. Full/native sets, scores and
rankings agree for every sampled query.

Six-repeat warm full-client medians in ms (default / full control / native):
Rust 1,292.054 / 380.906 / 286.320; SQL 1,464.892 / 981.670 / 871.142.
Native elapsed reductions versus default are 77.84% / 40.53% (Rust / SQL), and
versus unchanged-scope full regex 24.83% / 11.26%. Unchanged-hitset query-latency
sums also fall (Rust 1,105.739 to 233.678 ms; SQL 1,249.113 to 721.603 ms),
so fewer returned matches alone do not explain the gain. These are post-strata,
not a causal allocation of saved time. Constructor warm medians on 20,000
inputs are Rust 46.682 / 49.651 / 46.309 ms and SQL
1,820.366 / 5,517.044 / 5,530.232 ms; SQL normalization/parser overhead remains
substantial even though the overall native query is faster than default here.

Independent gold has 50 documents and 40 exhaustive queries. Complete-syllable
intent: default TP/FP/FN/TN = 70/20/0/910, native = 70/0/0/930; FDR falls from
22.22% to zero, FPR from 2.15% to zero, and relevant recall stays 100%.
Initials recall falls from 100% to zero; final-completion recall from 100% to
39.02%. Normalized-input recall rises from 35.29% to 100%, a grammar-acceptance
change rather than a narrowing benefit. Do not automatically replace partial
queries because a spelling happens to parse as a valid full syllable: the same
`an` input has different positive labels for full versus completion intent.
The shared index remains 3,350,528 bytes. The new 20 protocol tests and the 15
existing phrase benchmark tests pass. No product or version changes were made;
existing historical reports and the original checkout's untracked files remain
untouched. Shared-host swap usage (~27.9 GB) limits performance generalization.

### Opt-In Auto Routing

Extend the existing unreleased 0.0.9 mode interfaces with `auto`; do not bump the
version, change default function bodies/OIDs or overwrite earlier benchmarks.
Keep Rust embedded/SPI-free tokenization and independent SQL live-table parsing.

The shared parser emits canonical exact literals or terminal `.*` prefixes:

- Parse complete leading syllables with the existing normalization and greedy
  boundaries. Preserve a valid final suffix whole before greedy splitting.
- An unmarked terminal token is a prefix when incomplete or a proper prefix of
  another syllable. Thus `xi a` and `xi an` retain completion, `xi ang` is exact.
  A numeric tone or a tone mark on the terminal token signals exact completion;
  tones elsewhere do not signal that the terminal token is complete.
- Reject malformed normalization and input/token limit violations. Otherwise,
  failed full-leading/terminal-prefix parsing falls back to normalized compatible
  tokenization (`whh -> w.* h.* h.*`). Never drop unmatched initial letters.
- Unmarked leading syllabic `m`/`n` are not confident full tokens: `nh`, `n h`
  and `m n` fall back to compatible initials. Their own tone or numeric boundary
  permits exact completion; a numeric boundary only marks its chunk's last token.
- Multi-token input made entirely of single letters and without any marked or
  numeric tone is also low-confidence and uses compatible regex. `a h`, `e o`
  and `o y` must not narrow vowel initials to literal leading vowels. Explicit
  completion (`a1 h`, a marked leading vowel) still permits native routing.
- Generated-pinyin auto patterns retain normalized compatible pipe prefixes.
  Native constructors cannot represent arbitrary pipe alternatives.

With no generated flag or explicit expansion cap, all-literal patterns route to
native term/phrase_array (including native slop). Several tokens with exactly one
terminal prefix and zero/NULL slop route to phrase_prefix. Single-prefix,
multi-prefix, positive-prefix-slop, explicit-cap and unavailable-native-API cases
use the existing regex constructor. Explicit caps operate on selected auto
patterns. Preserve NULL, empty and default error behavior.

Use a finite prefix ceiling of 16,384: the pg_search 0.26.1 default of 50 can
silently lose matches, while its Tantivy scorer preallocates a vector of the
requested capacity, making an integer-max ceiling unsafe. The bundled syllable
inventory is much smaller; fields containing arbitrary non-pinyin terms beyond
the ceiling remain a documented limitation, not a claim of unlimited recall.
Test more than 50 expansions and fallback after a native API is removed.

This is a lexical heuristic, not probabilistic intent detection. Valid short
syllables can be initials, and a valid full syllable can be a completion prefix.
Retain explicit modes for known intent. Require regression coverage of both
backends, normalized/boundary input, initials, final completion, slop/cap/pipe
fallbacks, fresh/upgrade parity and actual read-only parallel execution.

Benchmark default regex, auto and native phrase on the same rebuilt plain index
in a fresh UUID database, without modifying historical evidence. Use at least
20,000 corpus phrases and 2,000 queries per complete/initials/completion workload.
Record exhaustive independently labeled fixture metrics by intent, actual full
hit sets, per-method source-hit fractions and native-route distributions. Do not
discard ambiguous misses or time native empty partial results as successful
searches. Separate warm client/BM25/Top-K timing and constructor-first-call costs;
record the installed library/functions and all inputs. No index-data changes.

The completed 2026-10-14 [Auto report](../benchmarks/pinyin_auto.md) and
[JSON (gzip)](../benchmarks/pinyin_auto.json.gz) use release-mode PG19 beta 4 / pg_search
0.26.1, 20,000 rows, 2,000 inputs per workload and six rotated fresh-index rounds.
The prepared image is an experimental source-compatibility build, not an official
PG19 upstream binary package. Earlier benchmark artifacts remain untouched.

- Complete-input hit pairs: default 5,366, Auto 4,148, exact phrase 3,477. Auto
  removes 1,218 default-only pairs (22.70%), changes 234 query hitsets and retains
  every known source. Removed pairs are not assumed irrelevant without labels.
- Initials: Auto preserves all 122,290 default hit pairs and 2,000 source hits;
  every input routes to regex. Exact phrase has no matches. The single-letter
  guard was required: an initial probe missed 15 sources when legal vowel
  initials were mistaken for complete leading syllables. That run was discarded;
  final regressions and the complete fresh run use the corrected policy.
- Completion: default 10,873 pairs, Auto 7,731; 3,534 pairs disappear and 392 are
  added, including 219 recovered sources (1,781 to 2,000). Auto uses phrase_prefix
  throughout. Native exact phrase matches only four known sources, so its
  partial-input latency is omitted rather than presented as a speed benefit.
- Independently labeled gold, complete intent: Auto TP/FP/FN = 70/9/0 versus
  default 70/20/0 and exact phrase 70/0/0. FDR drops 22.22% to 11.39%; terminal
  prefix ambiguity remains. Initials retain 30/3/0; completion improves 41/4/0
  to 41/0/0; normalization improves 12/8/22 to 34/2/0. This small contrastive
  fixture is not a production error-rate or pronunciation-accuracy estimate.
- Score compatibility is not an acceptance claim. Among queries with unchanged
  complete ID sets, 128 complete and 334 completion inputs change Top-K ID order
  under Auto; these all use phrase_prefix in this corpus. Source-hit coverage
  checks any matching position and must not be interpreted as Top-1 accuracy.
- Rust warm complete batches: default/Auto/native = 1,238.865/320.533/278.477 ms;
  completion default/Auto = 1,286.653/467.457 ms. SQL complete =
  1,393.908/984.145/858.965 ms; completion = 1,483.978/1,040.014 ms.
  Initials add classification overhead: Rust 1,509.097 to 1,554.047 ms, SQL
  2,105.065 to 2,710.942 ms. Shared-host outliers are retained in all six rounds;
  these exploratory median comparisons do not prove a universal speedup or a
  statistically significant small initials slowdown.
- Materialized warm construction for 20,000 rows costs Rust default/Auto
  46.727/81.579 ms and SQL 1,731.520/5,716.056 ms. The gain, where observed,
  comes from native index queries and narrower scope, not cheaper parsing.
- The same index has 3,350,528 bytes and 58,349 positions. No new field, token
  mutation, dictionary change or index-size reduction is introduced.

Validation passes 35 Rust unit tests, 139 Python tests, 22 data-pipeline tests and
1,585 pgTAP assertions across nine files, plus formatting and library/binary
checks with no PostgreSQL features and PG19. Actual isolated SQL-only operation,
genuine v0.0.8 upgrade/fresh-function parity and read-only parallel workers pass.
The benchmark additionally guards numeric fingerprint equivalence for PostgreSQL
JSON score `0` and direct-cursor `0.0`, without score tolerances. A failed report
attempt was fully discarded and rerun in a fresh UUID database after that fix.
Development version remains 0.0.9; no release version bump or extra migration.

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
- [Exact term/phrase constructors (pg_search 0.26.1)](https://github.com/paradedb/paradedb/blob/v0.26.1/pg_search/src/api/builder_fns/pdb.rs)
- [Native/retokenized phrase execution (pg_search 0.26.1)](https://github.com/paradedb/paradedb/blob/v0.26.1/pg_search/src/query/pdb_query.rs)
- [Native slop wrappers (pg_search 0.26.1)](https://github.com/paradedb/paradedb/blob/v0.26.1/pg_search/src/api/operator/slop.rs)
- [Regex phrase scoring/expansion implementation (pinned Tantivy)](https://github.com/paradedb/tantivy/tree/63e0c241cfd330381aebd5893f188fa858acbf45/src/query/phrase_query)
- [Repeated-term position reuse with slop (pinned Tantivy scorer)](https://github.com/paradedb/tantivy/blob/63e0c241cfd330381aebd5893f188fa858acbf45/src/query/phrase_query/phrase_scorer.rs#L150-L166)

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
