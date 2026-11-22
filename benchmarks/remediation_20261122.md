# Runtime Remediation Benchmark

Date: 2026-11-22. Development changes, no version bump.

Baseline: `2dadce0` (`main`, pg_pinyin 0.0.9). Before and after libraries were
built separately with Rust 1.99.1, pgrx 0.20.0 and matching server headers.
The final after libraries were extracted from the audited DEB packages, not
test-feature builds. Earlier exploratory measurements are not included below.
Character and phrase CSV bytes are identical between variants.

## Protocol

- Linux ARM64; PostgreSQL 18.7 with pg_search 0.27.0, and PostgreSQL 19.0 core-only.
- 20,000 deterministic dictionary documents; 2,000 default-regex queries on PG18.
- Three new databases per variant per platform; variant and aggregate order rotate.
- Separate, hash-verified native libraries; both pattern APIs remain immutable.
- Every conversion is consumed through count, byte length and SHA256. Word text,
  native tokens and batch output must match the source-dictionary gold exactly.
- Each aggregate uses a new backend, one complete warmup, then a measured warm
  pass. First-backend measurements include loading; they are not cold OS-cache tests.
- COPY includes upload, generated-column conversion, writes and commit, including
  first-use dictionary initialization. It is not an isolated conversion kernel.
- Search validates complete scored hits, then times ranked Top-10 results and
  requires a real ParadeDB TopK plan. CPU-heavy test/build jobs finish first.

Corpus SHA256: `f45d55432fd47d0298994a71965c1f54f19de4c897641b95c873b0c3ed203001`.
Query SHA256: `b25336472e7a712e386ae8cafb9faa9cd5d79700c8362dbdf1f4c01d582251dd`.

Raw results, plans, full hits, hashes, RSS and pressure samples are kept outside
the repository. Compressed report SHA256:

- PG18: `d5de4e9d63559ad65b4a91228dfa01cd5c5668f43eb7b826d4681c37dda8f916`.
- PG19: `8e418f73b054187c9af614313a44eb8e9d403036fae5db02ac127ece028b90e0`.

## PostgreSQL 18.7

Median client wall milliseconds per complete workload. Negative change means
less time; percentages are not isolated Rust CPU measurements.

| Workload | Before | After | Time Change |
| --- | ---: | ---: | ---: |
| Character text, 20k rows | 223.089 | 11.181 | -95.0% |
| Word text, 20k rows | 239.993 | 12.135 | -94.9% |
| Word native text[] tokens, 20k rows | 222.162 | 16.807 | -92.4% |
| Word batch, 512 documents per call | 25.201 | 21.084 | -16.3% |
| Real pdb.icu input, 20k rows* | 642.467 | 57.494 | -91.1% |
| Dynamic legacy regex patterns | 15.916 | 15.968 | +0.3% |
| Constant legacy regex patterns | 5.224 | 5.336 | +2.1% |
| COPY with generated pinyin, no BM25 | 335.981 | 129.628 | -61.4% |
| COPY with generated pinyin and BM25 | 364.523 | 160.579 | -55.9% |
| Ranked Top-10, 2k default-regex queries | 1402.255 | 1414.842 | +0.9% |

*ICU is a corrected pipeline, not an equivalent-output microbenchmark: 120 of
20,000 old ICU outputs differed from scalar dictionary gold; all new outputs
match it in every round. This is dictionary consistency, not a claim of 100%
linguistic accuracy on unseen text. Examples repaired include `不看僧面看佛面`
(`fo`, not `fu`) and `一语中的` (`di`, not `de`).

The same 5,367 full scored hits and ranked outputs were preserved. Top-10 ranges
overlap: before 1381.436-1402.964 ms, after 1391.529-1433.446 ms. Per-query p95
was 1.002 vs 1.000 ms. This does not establish a search speedup.

Index sizes were identical in all six databases: 3,383,296 bytes for the
precomputed field and 3,424,256 bytes for generated-column COPY. The unchanged
scalar output does not reduce BM25 index size. Standalone SQL-generated piped
fields are not the index-size workload measured here.

### Loading and Memory Tradeoffs

| First-Backend Aggregate | Before (ms) | After (ms) |
| --- | ---: | ---: |
| Character text | 225.346 | 26.179 |
| Word text | 320.828 | 110.961 |
| Native token arrays | 329.484 | 126.682 |
| Batch 512 | 112.796 | 118.206 |
| Real ICU input* | 729.320 | 155.659 |

The first batch is **4.8% slower**, despite its warm improvement. Building the
prefix index increases initialization work; batch already avoided most per-row
version-query overhead before this change.

Median backend RSS growth during the word-text workload was 66.602 vs 76.480 MiB;
for batches, 68.664 vs 79.035 MiB. Expect roughly **10 MiB more per initialized
word backend** in this workload. The prefix HashSet costs memory; sharing character
maps with Arc does not eliminate that cost. RSS includes mapped pages, query
memory and allocator retention: it is neither dictionary-only allocation nor a
sampled peak. Character-only RSS growth was essentially unchanged.

## PostgreSQL 19.0

Same corpus and three fresh rotated rounds; no pg_search/ICU or BM25 claim.

| Workload | Before (ms) | After (ms) | Time Change |
| --- | ---: | ---: | ---: |
| Character text | 139.495 | 10.504 | -92.5% |
| Word text | 144.873 | 11.613 | -92.0% |
| Native text[] tokens | 141.707 | 16.067 | -88.7% |
| Batch 512 | 19.775 | 16.095 | -18.6% |
| Dynamic legacy patterns | 15.274 | 15.389 | +0.8% |
| Constant legacy patterns | 4.566 | 4.519 | -1.0% |
| COPY, generated column, no BM25 | 247.634 | 125.707 | -49.2% |

First-backend batch: 100.963 vs 111.988 ms (+10.9%). Word-text RSS growth:
61.109 vs 70.090 MiB. Small pattern timing changes are not the target of this fix.

## Attribution and Limits

The large scalar improvement is consistent with removing the per-row SPI version
query. Prefix-pruned lookup, borrowed input slices, deferred reading conversion
and direct typed casts also change the pipeline. These variants combine changes;
there is no isolated per-change ablation, so do not attribute every percentage
to a single mechanism. Warm batch improvement is much smaller because its old
implementation already amortized version checks.

Auto-query capability probes intentionally remain dynamic: replacing them with
installation-time booleans broke the existing rename/missing-native-API fallback
contract. Delta-only suffix overlays are also deferred. Suffix table DML still
requires explicit cache clearing; relation creation/drop/recreation is detected.

Fresh databases prevent object collisions, not shared OS caches or external
host load. Three local rounds do not establish fleet-wide latency guarantees.

## Regression and Upgrade Validation

- PG19: 46 native/Rust tests passed. PG15.20, PG16.16, PG17.12 and PG19.0:
  83 core pgTAP assertions each; PG18.7 + pg_search: 1,630 assertions passed.
- Full PG18 Python integration suite: 212 tests passed. Snapshot visibility,
  savepoint rollback, repeatable-read and transactional dictionary-sync tests
  also passed on PG19. The new core regression has 16 intended failures on the
  rebuilt baseline and passes after repair. Real role tests also prove that
  direct cast calls preserve EXECUTE grants/revocations on every PG15-19 major.
- Exact released 0.0.5-0.0.8 installation SQL plus each tag's dictionary data was
  replayed on PG18 using the current ABI-compatible library. ALTER EXTENSION
  preserved all rows; explicit sync added 38,486 words and four tokens per old
  dictionary, preserved overrides, and was idempotent. This is catalog/data
  replay, not execution of old pre-CVE binaries on patched server headers.
- All nine generated data/report artifacts reproduced byte-for-byte. Character
  and phrase CSVs are unchanged; 196,188 word keys and all 64,091 restorations
  remain. Conflicts fell from 576 to 397 by removing redundant restoration
  candidates, not by silently changing selected readings.
- All ten local ARM64 PG15-19 DEB/RPM packages built. Migration bytes, controls,
  architecture, notices, dependencies and corresponding payloads were checked;
  old installation SQL did not leak from live server directories. Mixed-arch
  conversion ignored the opposite architecture. RPMs require glibc 2.41+, not an
  older RHEL ABI. AMD64 and hosted CI were not executed in this local run.
- A real unwritable-/tmp probe reached INSERT seeding fallback and installed all
  196,188 phrases. Permissions were restored after the test.

## Replay

Build the baseline archive and current checkout with separate Cargo targets and
the same compiler/PG_CONFIG. Save the libraries as `pg_pinyin_before.so` and
`pg_pinyin_after.so` in the matching server's pkglibdir; never mix PG majors.
Install the current SQL. Create a two-entry JSON manifest with `label`,
`server_version`, `pg_search`, `library`, `library_sha256` and
`pattern_volatility: "IMMUTABLE"`; set `require_icu_gold: true` for the repaired
ICU variant. Hashes must come from those actual binaries.

```bash
python3 scripts/benchmark_pgrx_upgrade.py \
  --dsn "$PGURL" --manifest /tmp/pinyin-variants.json --icu \
  --rows 20000 --queries 2000 --rounds 3 --output /tmp/pinyin-remediation
```

Use a disposable Linux server with psycopg 3 and superuser access for native
file/hash/RSS verification. PG19 uses `pg_search: null` and omits `--icu`.
Existing report paths are not overwritten. Raw outputs and machine details
should remain local.
