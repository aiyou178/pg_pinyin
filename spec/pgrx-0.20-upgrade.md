# pgrx 0.20.0 Upgrade

Date: 2026-11-13. Development version remains 0.0.9; no release bump.
Implementation and local validation complete. Results:
[benchmark report](../benchmarks/pgrx_0200.md).
Raw measurements and build receipts are preserved locally, outside the PR.

## Upstream Review

Reviewed the tagged [release](https://github.com/pgcentralfoundation/pgrx/releases/tag/v0.20.0)
and [migration guide](https://github.com/pgcentralfoundation/pgrx/blob/v0.20.0/v0.20.0-MIGRATION.md).

- Require Rust 1.99.1 (1.99.0 has an upstream bool ABI miscompilation).
- Drop PostgreSQL 14; package PG15-19. PG19 targets rc2, not beta4.
- Install matching clang, libclang and LLVM tools for generated C macros/inlines.
- Build against patched PostgreSQL headers for CVE-2026-6460. DEB dependencies
  include the header package version used to build, not only the server major.
- Replace the guarded but unchecked AnyElement text-array conversion with the new
  checked value API. Existing array/domain/toasted/NULL/parallel tests own this contract.
- The removed handwritten macros, PgBox/PgVarlena, tuple-descriptor borrows,
  fn_call and catch_unwind changes have no call sites in this extension.
- New tsvector/tsquery APIs and migration-fragment/package opt-ins are unnecessary
  for this change. Keep existing public query types and release packaging.
- Correct pg_test feature forwarding: use the selected PG major instead of
  unconditionally activating pgrx-tests/pg18.

## Volatility and Compatibility

Rust regex pattern parsers use embedded immutable dictionaries without SPI.
Declare all three pattern signatures IMMUTABLE, STRICT and PARALLEL SAFE; test
them in real stored generated columns. Apply the same catalog correction in the
unreleased 0.0.8 -> 0.0.9 migration. Released scripts remain untouched. Existing
development 0.0.9 installations need reinstall/catalog refresh; the extension
version is intentionally not bumped. SQL backend parsers read live tables and
remain separate; no volatility promotion is applied to them.

Live romanization scalar APIs currently declare IMMUTABLE while consulting
editable tables. This is an existing compatibility constraint for stored
generated columns, not a claim that table-backed output is eternally constant.
Do not silently change these APIs to STABLE (which prevents creating those
columns) or freeze their dictionaries (which loses live-table behavior). A
future API revision should separate frozen/versioned generation from live
STABLE lookup. After dictionary changes, refresh stored values and rebuild
function-expression indexes explicitly. Batch APIs already declare STABLE.

## Performance Candidates

- Generated pgrx inlines may reduce native-call guard overhead. Measure rather
  than attribute all timing changes to them.
- Immutable constant pattern calls can fold during planning. Measure constant
  and row-dependent parsing separately; do not call folding a faster tokenizer.
- Scalar romanization still performs a dictionary-version SPI read per call;
  document batches amortize that cost. Avoid transaction-wide caches that hide
  legitimate dictionary edits.
- The exact text-array path avoids JSON/SPI fallback; preserve its type/NULL
  checks. Existing text-domain/custom-tokenizer casts remain compatible.
- Phrase lookup, Unicode normalization/allocation and same-query planning are
  candidates for profiling, not automatically authorized semantic changes.

## Validation Protocol

Finish implementation before testing/debugging. Build old/new libraries in
separate Cargo target directories with the same Rust compiler and release flags.
Record source snapshots and SHA256 for each loaded DLL, including a dependency-
only 0.20.0 control when possible. Use PG18.7 and pg_search 0.26.1 for comparable
full integration and search. PG19rc2 core tests run without pg_search; the old
beta4/ParadeDB compatibility image remains historical and is not a new rc2
integration claim. pg_search itself is not upgraded in this experiment.

Run existing Rust checks, real install/upgrade tests and pgTAP (including actual
parallel read-only workers). The new generated-column regression must fail on
the old STABLE parser declarations. Do not add implementation-string tests.

Benchmark 20,000 deterministic documents and 2,000 complete ranked searches per
variant/round, with three rotated fresh database rounds. Consume every output,
compare against source readings and independent hand gold, compare complete
scored hits/Top-K across variants, retain first-backend versus warmed timings,
actual Top-K plans, relation bytes, source/compiler/library hashes and memory
pressure. Disable automatic prepared statements and JIT. Do not run owned heavy
builds during measurement. All UUID databases are cleaned; raw reports are
losslessly compressed and output overwrite is refused.

## Verified Results

- Rust 1.99.1: formatting, library/benchmark checks, 35 unit tests and strict
  Clippy for the non-extension library pass. Two new compiler lint findings
  were fixed without changing parser behavior.
- PG18.7 / pg_search 0.26.1: 208 Python tests, including genuine 0.0.8 -> 0.0.9
  upgrades and the independent SQL backend, and 1,588 pgTAP assertions pass.
- PG19rc2: fresh core installation, `pg19,pg_test` feature check and 40 pgTAP
  assertions pass, including launched workers in read-only transactions.
  The matching PG18 test-feature check also passes.
- 22 data-import tests and 9 dictionary-accuracy tests pass. All three rebuilt
  variants have identical CSV hashes; baseline/control Rust source hashes match.
- The generated-column negative control rejects the genuine 0.0.8 STABLE
  catalog with SQLSTATE 42P17, then stores `whh` correctly after migration.
- Nine fresh benchmark databases verify 20,000 outputs and 2,000 queries each.
  All complete scored hits and Top-K results match. Both index-size measures
  are identical across variants; memory pressure does not increase.
- Constant parsing median: 11.305 -> 5.343 ms (-52.74%), with a literal output
  confirmed by `EXPLAIN (VERBOSE)`. Dependency-only control: 11.625 ms, so this
  gain comes from the IMMUTABLE correction.
- Dynamic parsing: 16.479 -> 16.759 ms (+1.70%); complete ranked queries:
  1345.264 -> 1393.960 ms (+3.62%). No full-query speedup is claimed.

Local Docker builds reused a cached image base to work around upstream HTTP
403s, then installed the actual patched server/header packages via the official
HTTPS PGDG repository. Clean builds from the default DockerHub bases, x86_64,
PG15-17, release-package production and PG19rc2 with pg_search were not validated
locally. The old PG19 beta4 compatibility image is historical only.

## PR And Release Preparation

Following the coordinated extension upgrade workflow, CI now defines all ten
PG15-19 / amd64-arm64 combinations, plus Rust checks on both architectures and
the deterministic dictionary check. Each PostgreSQL job runs native tests,
release installation, core read-only parallel tests and consumed SQL benchmark
outputs. PG18 additionally runs the complete pg_search suite and genuine
0.0.8 -> 0.0.9 upgrade checks. Remote results will be recorded after completion.

Docker builds bootstrap CA certificates from Debian before contacting PGDG via
HTTPS, and fail on package-index errors. Target PG major and runtime paths are
explicit so PG19 packages can be installed on a stable base without starting
the base image's older server. Both READMEs state minimum runtime and header
versions. The original patent attribution is retained.

Only the final pgrx benchmark summary is included in this PR. Raw JSON, plans,
logs and manifests are preserved outside the repository. Existing historical
reports already on main are unchanged. Release notes cover all unreleased
0.0.9 changes since v0.0.8. No extra version bump, merge, tag or release is
performed before the user reviews the release note.

## Next Performance Priorities

1. Keep using document batches/COPY for import workloads. The existing batch
   implementation consumes the same word output 8.59x faster than scalar calls
   in this run, including batch grouping/array overhead. It predates this upgrade.
2. Profile the per-scalar SPI version/xmin check. Consider an explicitly frozen
   dictionary API for generated columns, distinct from live STABLE lookup;
   preserve visibility of real dictionary updates. This also resolves the
   existing scalar volatility contract without breaking generated columns.
3. Measure repeated query planning/prepared statements with actual Top-K plans
   and exact hit/score parity. Dynamic parsing is already under 1 microsecond
   per input in the aggregate benchmark; do not infer large full-query gains
   from further parser-only tuning.
