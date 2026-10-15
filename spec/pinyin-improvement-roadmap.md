# Remaining Pinyin Improvements

Recorded on 2026-10-14. This roadmap does not change the extension version or
authorize dictionary mutation, a release, or implementation of every candidate.
The current development version is 0.0.9; the latest published release is v0.0.8.

## Baseline and Status

The coverage-preserving dictionary import, explicit query modes and native
document-array/batch conversion are on `main`. Native exact queries, explicit
apostrophe boundaries and conservative Auto dispatch reached `main` in
[PR #5](https://github.com/aiyou178/pg_pinyin/pull/5), merged on 2026-10-14. Its
[recorded Auto experiment](https://github.com/aiyou178/pg_pinyin/blob/e345d8a6e1adac9648707e298006a1105ea079c0/benchmarks/pinyin_auto.md)
is exploratory evidence, not a business relevance benchmark. In particular,
retaining the source row does not establish correct Top-1 ranking, and Auto does
not accelerate initials when it preserves the default regex hit set.

The earlier [array/batch benchmark](../benchmarks/p2_array_batch.md) measured
conversion and unindexed server-side INSERT. Its batch speedup does not measure
client upload, transaction commits, or BM25 index maintenance. Candidate 6 below
closes that measurement gap independently of PR #5.

## Candidates

| Priority | Candidate | Status | Acceptance and constraints |
| --- | --- | --- | --- |
| 1 | Business Top-1 and ranking evaluation | Proposed | Collect provenance-bearing held-out real queries and independent relevance labels; compare Recall@1/10, MRR/nDCG, zero-hit rate and latency across default regex, explicit phrase and Auto. Dictionary-derived source retention is not relevance gold. |
| 2 | Optional positional initials side field/index | Proposed | Keep the current API/default index unchanged; compare initials recall, false positives, scoring, build time and actual combined index bytes. An additional index must justify its storage/write cost. |
| 3 | Standalone SQL query-parser cost | Proposed | Profile parsing and query construction separately from index lookup, preserve SQL-only deployment, token boundaries and empty-query semantics; check real end-to-end performance and worker safety. Do not require the Rust extension. |
| 4 | Polyphone and domain names | Proposed | Reconcile traceable syllable-preserving person/place/domain sources, licenses and conflicts; evaluate held-out readings and retrieval without reducing existing coverage or using the same imported data as accuracy gold. |
| 5 | Entity-protected keyword/POS pinyin field | Proposed | Make filtering opt-in and protect names, entities, phrase positions and short queries. Measure recall/ranking losses together with token/index bytes and write cost; never silently remove function words from the default field. |
| 6 | Real BM25 bulk import | Complete | Compare raw-document INSERT/COPY and server-side scalar/batch conversion, existing-index versus post-load build, transaction sizes and sampled memory. Include client transfer, commit and readiness costs; verify every stored value and real BM25 hits. |

The previously declined token-mutation/multiple-segmentation approach is not
reintroduced here. Candidates 1-5 remain proposals; this implementation selects
candidate 6 only.

## Candidate 6: Bulk Import Protocol

### Implementation Boundary

Add a standalone Python/psycopg 3 harness, focused protocol/integration tests and
a read-only local memory sampler. Reuse the existing public
`pinyin_word_romanize(text)` and `pinyin_word_romanize_batch(text[])` APIs, without
changing Rust functions, dictionaries, extension migrations or versions. Run on
the latest verified pg_search release (0.26.1) with the prepared experimental
PostgreSQL 19 beta 4 image. Record exact server/build artifacts rather than
claiming official upstream PG19 support for that cached image.

Compare these methods:

| Method | Input and conversion | Interpretation |
| --- | --- | --- |
| `insert_generated` | One parameterized INSERT per raw document; stored generated pinyin | Row-at-a-time client baseline |
| `copy_generated` | COPY raw documents into a table with stored generated pinyin | Native COPY with per-row server conversion |
| `copy_stage_scalar` | COPY raw documents to a temporary staging table, then scalar INSERT SELECT | COPY staging baseline |
| `copy_stage_batch` | COPY raw documents to staging, build bounded document arrays in SQL, batch-convert and insert using ordinality | Existing batch API applied to a complete upload pipeline |
| `copy_precomputed` | COPY documents with already-known pinyin | Lower-bound control only; conversion is excluded |

Every target has a primary key and a real BM25 index over the document ICU field
and whitespace pinyin field. Test both an empty index created before ingestion
(`online`) and an index built after committed ingestion (`after_load`). The latter
is suitable for a new/rebuilt table, not uninterrupted search on an existing
production table.

Default matrix: 20,000 deterministic documents, three rotated repeats,
transaction sizes 512/4,096/20,000, and arrays of at most 512 independent
documents. Transaction size and conversion-array size are distinct controls;
arrays are bounded to at most 8,192 documents. No concatenation across document
boundaries is permitted. These defaults are not an asserted universal optimum.

### Timing and Isolation

- Use a disposable UUID database and fresh table/index/staging state plus a new
  backend for each matrix cell. Refuse to delete non-benchmark databases.
- Load the current dictionary outside import timing. Raw documents must travel
  from the client during timing; no preloaded source table or precomputed batch
  array may hide upload/construction costs.
- Include source encoding, client/server upload/execution, all transaction
  commits, any post-load BM25 build and final ANALYZE in total ready time.
  Report phase timing without presenting folded execution as isolated Rust CPU.
- Record source/data/script hashes, Git state, installed release DLL equality,
  extension versions, effective durability/planner/memory settings and host
  observations. Verify a co-located Linux server before fingerprinting its files,
  reject a debug or mismatched declared release artifact, and explicitly rebuild
  it from the recorded source before running. Identical stale installed/build
  files cannot establish source-to-binary linkage from hashes alone.
- Measure WAL LSN growth as a cluster-wide observation, not exclusive attribution
  to one import. Sample client/backend RSS and container cgroup memory at 10 ms
  where available. Missing/remote observations are unavailable, not zero;
  process-lifetime VmHWM and observed sample peaks are labeled separately.

### Correctness and Evidence

Before timing, exercise hand-written cases for umlaut/traditional readings,
polyphones, independent document boundaries, mixed Han/ASCII, empty and NULL
documents through every path and index strategy. After every timed case, use a
separate post-commit reader to compare all ordered IDs, original documents and
pinyin values against the deterministic corpus. Check BM25 positional phrase
hits against an independent token-window oracle, source retention and Top-K
execution plans; do not compare only counts or a few sampled output strings.

Test duplicate/invalid COPY failure, rollback and cleanup, then a successful fresh
case. Guard batch/transaction bounds, output overwrite, report completeness,
memory-unavailable behavior and safe database/table identifiers offline. Preserve
raw per-case/repeat JSON (optionally losslessly gzipped), with a readable report
covering medians, ranges, readiness phases, index bytes, sampled memory and
quality checks. Index sizes/segment layouts may differ across transaction
strategies even with identical documents and search hits.

Implementation must be complete before beginning tests or debugging. Start the
performance matrix only after the focused integration and regression checks,
and do not run other owned heavy benchmark/build jobs concurrently. Record
shared-host pressure and sampling overhead as limitations. Update this status
and README with actual measured recommendations after the run; do not promise a
speedup, smaller index, or a universal transaction size in advance.

## Candidate 6: Measured Outcome

The [2026-10-14 report](../benchmarks/bm25_import.md) and adjacent
[lossless JSON](../benchmarks/bm25_import.json.gz) record all 90 samples,
30 case configurations and three distinct rounds. The measurement used
Rust/dictionary source at `b29763b`, before PR #5 merged; the measured Python
harness is recorded by file hashes in the dirty development snapshot. The installed release DLL matches
its explicitly rebuilt artifact, and the executing server was verified local.
The publication branch is based on the later `main`; the raw report retains the
original source and artifact hashes rather than claiming a post-rebase timing run.

The fair 20,000-row transaction comparison is generated-column COPY versus
batch staging, both starting with a new backend and raw client input. Existing
index readiness changes from 510.890 to 307.486 ms (-39.81%); deferred index
readiness changes from 506.850 to 308.075 ms (-39.22%). The corresponding batch
times with 4,096-row transactions are 322.070 / 339.823 ms. With 512-row
transactions they are 383.679 / 350.117 ms. Larger transactions were helpful in
this small-phrase workload; rollback/visibility/locking and large-document memory
costs are not measured as production guarantees.

The same raw COPY staging shape provides phase evidence: at 20,000-row
transactions, scalar versus batch conversion+INSERT medians are 448.199 versus
210.508 ms online, and 367.103 versus 136.390 ms before deferred indexing. These
phases include first-use dictionary initialization and heap/index work, not
isolated Rust CPU. The existing batch API amortizes version checks/cache
snapshots across documents; SQL execution shapes also differ. No separate
profiling experiment attributes the whole improvement to one internal operation.

Independent raw-data audit verified 1.8 million recorded row checks, all 5,760
complete positional source probes (63 distinct token windows per sample), all
medians/ranges and the unchanged source/data hashes. Top-K IDs and exact recorded
scores are identical for every probe across all 90 cases. This is retrieval
consistency, not independent business ranking accuracy. The decompressed JSON
SHA256 is `94fa9a2af3ead3efe7bdad790b02eed5b7e2f32356d0afc3c93fae5b90179da1`
(4,007,480 raw bytes, 502,567 gzip bytes).

All methods produced identical verified document/pinyin rows and complete
positional hits. Hand-written gold includes NULL, empty input, traditional forms,
umlaut normalization, polyphones and independent document boundaries. Validation
passed 127 Python tests (including four genuine v0.0.8 upgrade tests), 875 pgTAP
assertions, 16 Rust unit tests, formatting and lib/benchmark checks with and
without pg19. The first pgTAP launch collided with asynchronous initial extension
creation; after setup completed, the whole serial suite passed. This was local
test orchestration, not a product change or a hidden weakened assertion.

No pinyin token/index-content reduction is implemented. At the 20,000-row
transaction setting, generated COPY and batch staging both have approximately
3.46 MiB online BM25 relations, and all deferred builds have approximately
3.44 MiB. Small online batch transactions observe 5.58-5.59 MiB. These are
immediate catalog sizes with possible segment/merge differences, not post-merge
steady-state compression. Maximum sampled online backend RSS is 120.8 MiB for
generated COPY and 125.9 MiB for batch staging, so no memory saving is claimed.
Client RSS and shared whole-container memory are recorded separately in JSON.

The shared host reported `32486.81M` swap before and `32240.75M` after timing.
The cached PG19 beta 4 / pg_search 0.26.1 image contains experimental compatibility
patches; it is not official upstream PG19 binary support. Three rotated repeats
support local workflow recommendations only, not stable universal speedups.
No extension/public API, default behavior, version, migration or dictionary data
was changed for this increment.

## Reference Staging SQL

Keep ordinary generated-column COPY when automatic per-row updates are needed.
For a new/rebuilt table with a plain pinyin column, the tested batch path can be
adapted as follows. The destination must already exist, for example
`documents(id bigint PRIMARY KEY, document text, pinyin text)`; do not drop a
production generated column or replace a table automatically.

Create the session-local stage once, outside the per-chunk transaction:

```sql
CREATE TEMP TABLE import_stage (
  id bigint PRIMARY KEY,
  document text
) ON COMMIT DELETE ROWS;
```

For each client transaction, COPY that chunk's raw `(id, document)` rows into
`import_stage` using the driver's COPY API. Then execute the following and commit
in the same transaction. The value 512 bounds independent documents per array;
it is separate from the transaction's row count. NULL and empty documents keep
their positions and the IDs align by ordinality.

```sql
WITH numbered AS MATERIALIZED (
  SELECT id, document,
         (row_number() OVER (ORDER BY id) - 1) / 512 AS batch
  FROM import_stage
), groups AS MATERIALIZED (
  SELECT batch,
         array_agg(id ORDER BY id) AS ids,
         array_agg(document ORDER BY id) AS documents
  FROM numbered
  GROUP BY batch
)
INSERT INTO documents (id, document, pinyin)
SELECT g.ids[o.ordinality::integer],
       g.documents[o.ordinality::integer],
       o.output
FROM groups g
CROSS JOIN LATERAL
  unnest(public.pinyin_word_romanize_batch(g.documents))
  WITH ORDINALITY AS o(output, ordinality)
ORDER BY g.batch, o.ordinality;
```

After an offline/new-table load, build the same BM25 fields and ANALYZE before
publishing the table as ready:

```sql
CREATE INDEX documents_bm25 ON documents USING bm25
  (id, (document::pdb.icu), (pinyin::pdb.whitespace));
ANALYZE documents;
```

An existing index is instead maintained by the normal INSERT commits. Commit
each chunk only when partial progress is acceptable; a single transaction keeps
atomic visibility but increases rollback/locking risk. Temporary stage rows are
cleared on commit. A failure rolls back the current transaction, not previously
committed chunks. The plain pinyin column must be recomputed by future write
paths when `document` changes; the batch loader is not a trigger or mutation API.
Dictionary reloads still require explicit stored-value refresh/index rebuild.
No automatic target migration, overwrite, conflict upsert or table swap is added.

The three SQL blocks above were also executed verbatim against a fresh disposable
PG19 beta 4 / pg_search 0.26.1 database: all 12 hand-written rows, 10 complete
source search probes and commit-cleared staging rows passed. The omitted
deprecated `key_field` option correctly derives the destination's primary key.
