# Standalone SQL Query Parser Performance

## Scope

Implement roadmap candidate 3 independently of the COPY batch PR. The baseline
is `cbfcf07c468e046a4068fb46460e0b64699b6827`, the `main` revision containing
the existing explicit modes, apostrophe boundaries, native phrase and Auto APIs.
Only the optional `sql/word.sql` query parser changes. No Rust algorithm,
extension version, migration, dictionary CSV, indexed document or default query
mode changes. `sql/pinyin.sql` and the shared `pinyin_tokenize` remain unchanged.

SQL-only installation requires pg_search plus the standalone SQL scripts/data,
not `CREATE EXTENSION pg_pinyin`. Existing SQL-only installations adopt the fix
by reloading `sql/word.sql`; they do not rebuild Rust or regenerate an index.
Existing `sql_` signatures and defaults remain unchanged.

## Implementation

- Compatibility patterns use a static parameterized `regexp_matches` aggregate
  directly over the live category-1 dictionary. Remove dynamic SQL planning,
  SETOF intermediate materialization and the synthetic row-number window from
  this query path, without changing the shared tokenizer.
- Explicit modes aggregate their sanitized live inventory once per invocation,
  retaining category 1/3, `m/n`, deduplication and length-descending/C ordering.
  An anchored POSIX regex replaces repeated PL/pgSQL inventory loops. PostgreSQL
  selects the longest match, preserving the old longest-first segmentation.
  See [PostgreSQL matching rules](https://www.postgresql.org/docs/18/functions-matching.html#POSIX-MATCHING-RULES).
- A delimiter-framed inventory string supports prefix checks with `strpos`.
  Length-descending order makes the first matching entry the longest, so its
  trailing character distinguishes an exact terminal token from a proper
  prefix. It must not match in the middle of a token. Serialize the inventory
  only once and omit the unused regex string for the initials-only path.
- Auto fallback aggregates its live compatibility regex only when needed.
  The tone-normalization loops, bounds, native-route selection, slop/caps and
  query constructors remain unchanged.

There is no application-maintained cache, cache table, trigger, materialized
view or parser-time write. PostgreSQL may reuse compiled regexes in a backend;
the exact pattern is regenerated from the live table on each call, so dictionary
changes, deletes, TRUNCATE and rollback cannot leave a stale parser dictionary.
Per-call aggregation/sorting and character/tone processing remain real costs.

## Compatibility

- Preserve SQL NULL separately from empty arrays and `pdb.empty()`; unsupported
  or empty user input must not turn into a truncated query.
- Preserve `whh -> w.* h.* h.*`, `xi'an` boundaries, umlaut normalization,
  numeric/diacritic validation, 256-character and 64-token limits, generated
  pipe-field patterns and final prefix handling (`yon` remains one token).
- Preserve Auto's tone validation before fallback, unmarked leading `m/n`,
  all-single-letter fallback and terminal ambiguity such as `xi a` / `xi an`.
- Compatibility mode retains raw category-1 regex rows and the empty-dictionary
  early return; explicit modes continue to ignore malformed/non-ASCII rows.
  Parameterized patterns no longer interpolate a live row's SQL quote into a
  statement. That quoting error is fixed, not deliberately reproduced.
- All affected helpers remain `STABLE` and `PARALLEL SAFE`. Only read-only
  dictionary access is allowed during parsing. No Rust dependency is introduced.

## Validation Protocol

Finish product code, tests, benchmark harness and protocol documentation before
starting tests/debugging. Use the prepared local PG19 beta 4 image with the
latest verified pg_search 0.26.1 (2026-10-14). Its PG19 compatibility patches are
experimental, not proof of an official upstream PG19 binary.

Tests install the SQL-only backend in disposable UUID databases and cover the
existing independently specified fixtures, explicit boundary/tone/null cases,
live mutation and rollback, empty/malformed inventories and SQL quoting.
Force a parallel scan, verify `Gather` and actual worker launch with parser
evaluation below it, execute in a read-only transaction and verify no transaction
ID assignment. Keep the existing two-backend/upgrade suite as additional
regression coverage, not the SQL-only deployment proof.

## Benchmark Protocol

Compare old versus new SQL implementations of the same API and input, never
infer a speedup by comparing different semantic modes. Use 20,000 indexed plain
syllable documents, 2,000 deterministic inputs for each of compatibility, full,
initials, autocomplete and Auto, with three rotated repeats. Each round owns
fresh baseline/current databases; stages use fresh connections and warm the
entire measured workload. Capture a first-use call separately, without calling
it a cold OS/storage benchmark.

Measure separately:

1. Server-side pattern parsing with all output values consumed.
2. Server-side construction and serialization of real `pdb.query` objects.
3. Full client queries including construction, BM25 scoring/matching, round
   trips and ranked Top-K result transfer. Disable psycopg auto-preparation so
   timed statements and EXPLAIN use the same policy; per-query planning is
   included. This does not measure prepared/generic-plan performance.

Setup, dictionary load, index build, preflight correctness and EXPLAIN verification
are outside query latency. Record setup/index costs and actual index bytes
separately. Every old/new pattern and full hit set must match exactly for its
mode/input; Top-K IDs and scores must also match. Handwritten gold and a real
BM25 Top-K plan guard against benchmarking empty responses or a sequential scan.
Parity proves no observed semantic regression, not new business ranking accuracy.

Record baseline/current source and installed-definition hashes, dictionary/input
hashes, PostgreSQL/pg_search versions, per-round samples, first-use versus warm
boundaries and host pressure. Keep lossless raw JSON and a readable report. Refuse
to overwrite existing results, clean up only owned UUID databases, and do not run
other owned heavy jobs while measuring. No remote DSN may pretend to prove a
local installed Rust build; this benchmark installs no Rust extension at all.

## Outcome

Validated locally on 2026-10-14 in the cached experimental PG19 beta4 image with
pg_search 0.26.1. The publication revision passed 169 repository Python tests
(including 12 SQL-only integration tests and 18 benchmark protocol tests),
22 dictionary tests and 1,585 pgTAP assertions. The unchanged Rust code passed
35 tests and lib/benchmark checks with and without `pg19`; `cargo fmt --check`
passed on the host. No release metadata, migration or Rust source was changed.

The publication test audit replaced source-text/call-shape checks with a real
benchmark CLI integration covering fresh SQL-only installs, consumed outputs,
ranked plans, report hashes, NULL-query handling and database cleanup. The full
Python and pgTAP suites were rerun after that audit. Original benchmark notes
retain the earlier 172-test count; production SQL and measured harness hashes
are unchanged. A direct baseline positive control also confirmed the quoted-live-
row regression: `whh` succeeds before inserting `qq'rr`, then the old parser
fails with SQLSTATE `42601`; the new parser still returns `w.* h.* h.*`.

### CI Checkout Ownership

After rebasing PR #7 onto `46d3bbc`, the PG18 and PG19 CI failures were traced
to Git rejecting `/work`: the host checkout belongs to the runner, but the
test command runs as container root. In both original CI runs, the real
benchmark CLI integration was the only failure among 169 Python tests; pgTAP
was not reached. The parser and benchmark results were not implicated.

Both CI jobs now explicitly trust only the mounted `/work` checkout in the
disposable container's Git configuration. No host configuration or wildcard
trust is added, and the benchmark retains Git's normal ownership protection.
The existing CLI regression remains the primary guard because it reads the
baseline Git object and executes the complete benchmark through a subprocess.
Validation must include a foreign-owned checkout negative control before
applying the same exact-path trust, followed by that CLI test and the complete
suite. The local 2026-10-15 negative control reproduced Git exit 128 against a
checkout owned by PostgreSQL's UID 999. Applying only `/work` trust made all
12 SQL-only integration tests pass as root, without changing the CLI test.
Original benchmark data and recorded source/harness hashes stay intact.

### Benchmark Evidence

The [final benchmark](../benchmarks/sql_parser.md) retains
[lossless raw evidence](../benchmarks/sql_parser.json.gz): 30 stage-complete
implementation/mode samples from six fresh databases. Independent audit
recomputed the summary, checked source/installed-function hashes and all actual
Top-K plans, and verified parity receipts. Every input's patterns, serialized
query, full hit IDs/exact scores and ranked Top-K matched the old implementation.
Handwritten gold and sampled token-window oracles also passed. Each index was
3,350,528 bytes; query semantics, dictionary coverage and indexed values are
unchanged, with no index-size saving.

| Mode | Patterns old/new ms | Constructor old/new ms | Client search old/new ms |
| --- | ---: | ---: | ---: |
| compat | 175.337 / 149.774 | 177.899 / 151.004 | 1610.185 / 1540.101 |
| full | 544.235 / 450.228 | 551.146 / 459.823 | 1183.819 / 1049.690 |
| initials | 421.398 / 412.790 | 429.992 / 416.954 | 2444.626 / 2494.793 |
| autocomplete | 513.727 / 437.326 | 517.405 / 443.137 | 1639.190 / 1340.019 |
| auto | 715.597 / 555.779 | 727.720 / 570.212 | 1825.031 / 1639.135 |

Totals consume 2,000 queries per mode; values are three-round medians. Auto's
patterns time fell 22.5%, 22.7% and 21.2% in paired rounds. Full and autocomplete
parser/query-construction times also improved in every round. Initials parser
time was only slightly better overall and slightly worse in round three; its
search time was worse in all rounds (2.1%, 9.5%, 13.1%). Compatibility search
varied from 27.2% slower to 21.2% faster. Full search improved 7.4%-17.6%,
autocomplete 11.7%-25.9%, and Auto 0.6%-11.6%. These are local observations,
not proof of universal improvements or causal attribution of every search delta
to parser CPU.

The [first pass](../benchmarks/sql_parser_first_pass.md) and its
[raw data](../benchmarks/sql_parser_first_pass.json.gz) are retained, not replaced.
It exposed a consistent 7.1% median initials-parser regression. Removing duplicate
inventory serialization and the unused initials regex construction preceded the
complete final matrix; no stage/mode was selectively omitted from the rerun.

The shared macOS host remained under substantial memory pressure: immediately
before/after the final run, swap used 39,293.12 / 39,091.19 MiB (total 39,936 MiB).
Only three rounds, dictionary-derived short words and unprepared single-client
queries were measured. Prepared/generic plans, concurrency, realistic long
business text and OS-cold behavior remain unvalidated. The experimental image
is not evidence of an official upstream PG19 pg_search binary.

Reproduce with a PostgreSQL administrator DSN (CREATE/DROP DATABASE permissions),
the installed pg_search version and baseline Git object available. Fresh output
paths avoid overwriting evidence:

```bash
report_dir=$(mktemp -d)
python3 scripts/benchmark_sql_parser.py --dsn "$PGURL" \
  --rows 20000 --queries 2000 --repeats 3 --top-k 10 \
  --json-output "$report_dir/sql_parser.json.gz" \
  --markdown-output "$report_dir/sql_parser.md"
```
