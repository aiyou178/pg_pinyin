# P2 Native Array and Document Batch Benchmark

- Started UTC: `2026-10-13T08:28:14.597751+00:00`; runtime HEAD: `32e3ae591650a6919289da01cab3801d4f72a74a`.
- Working-tree source hashes are recorded separately. Runtime HEAD is not a release/tag claim.
- PostgreSQL: `PostgreSQL 19beta4 (Debian 19~beta4-1.pgdg13+1) on aarch64-unknown-linux-gnu, compiled by gcc (Debian 14.2.0-19) 14.2.0, 64-bit`; extensions: `{"pg_pinyin": "0.0.9", "pg_search": "0.26.1", "plpgsql": "1.0", "vector": "0.8.4"}`.
- Real baseline: pre-optimization P1 snapshot (unreleased development 0.0.9), commit `f1094beeb69c093085403f5b02d616a4de7adabe`, DLL SHA256 `548c58b9ccd54c033187c4e3037292c9388f3a994181d97e0162cecd48b0071d`.
- Both baseline and current use development version 0.0.9. This is a source/artifact comparison, not a comparison between two released extension versions; latest release remains 0.0.8.
- Baseline/current Cargo build profiles: release/release; recorded compiler: `rustc 1.98.1 (48a229cea 2026-09-01)`. This build-profile label does not claim a published release. Current installed DLL must match the declared release-profile build artifact byte-for-byte and differ from the baseline DLL.
- Baseline aliases bind the separately rebuilt old C module and old Rust symbols; no SQL wrapper calls the current core to imitate old behavior.
- Fresh isolated database; 20,000 normalized Han-only phrases; 3 rotated repeats; batch sizes [1, 32, 128, 512, 2048].
- Dictionary phrases are hash selected, not supplemented with low-ID gold. Every stored document and character-token array is identical across methods.
- Character-token arrays and bounded input batches are precomputed outside timing. This excludes tokenization and array aggregation/transfer costs from native-vs-legacy and bounded-batch conversion timings.
- Cold means a new PostgreSQL backend (and its dictionary cache), not cold OS storage/shared buffers. Warm samples follow a complete untimed conversion.
- Generation forces materialized output, count, byte sum and ordered ID-output MD5. Full outputs are checked against dictionary readings and independent handwritten gold outside timing.
- Untimed non-executing plans, exact SQL and parameters are recorded. Batch and rowwise inputs have different physical row layouts; timings include their scans and aggregation, not just Rust instructions.
- INSERT measures server-side INSERT SELECT and includes heap/primary-key writes; each phase recreates the table. No BM25 index is maintained and no client COPY/upload cost is included.
- Batch elements are independent documents, NOT tokens within one document. NULL positions, flattened dimensions and bank/character document boundaries have separate handwritten gold.
- Run condition: Shared macOS arm64 host; pre-timing vm.swapusage used=31946.69M; Docker VM 8 CPUs and 15.6 GiB RAM. No compilation or database test suite runs during this benchmark.
- Run condition: PG19 beta 4 uses source-built pg_search 0.26.1 with the repository compatibility patch, not an official upstream PG19 binary. This local run uses a cached SDK overlay; P1 clean GitHub CI has passed PG18 and PG19 recipe builds.

## Generation Only

Whole-corpus milliseconds, median of repeated samples.

| Method | Cold ms | Warm ms | Warm documents/s |
| --- | ---: | ---: | ---: |
| legacy_scalar | 412.497 | 300.489 | 66,558 |
| current_scalar | 379.271 | 258.099 | 77,490 |
| legacy_tokens | 751.030 | 623.978 | 32,052 |
| current_tokens | 345.144 | 261.685 | 76,428 |
| current_batch_1 | 421.421 | 271.756 | 73,595 |
| current_batch_32 | 129.444 | 32.393 | 617,416 |
| current_batch_128 | 118.944 | 25.482 | 784,877 |
| current_batch_512 | 112.779 | 23.418 | 854,044 |
| current_batch_2048 | 110.768 | 21.265 | 940,500 |

## Fresh Unindexed INSERT

Scalar methods use stored generated columns. Token and batch methods insert explicitly romanized output, with identical IDs, source documents and output verified after every timed INSERT.

| Method | Cold ms | Warm ms | Warm inserted rows/s |
| --- | ---: | ---: | ---: |
| legacy_scalar | 408.709 | 321.993 | 62,113 |
| current_scalar | 391.727 | 296.844 | 67,375 |
| legacy_tokens | 804.224 | 649.027 | 30,815 |
| current_tokens | 404.257 | 277.513 | 72,069 |
| current_batch_1 | 409.147 | 331.318 | 60,365 |
| current_batch_32 | 153.352 | 71.299 | 280,511 |
| current_batch_128 | 141.936 | 41.774 | 478,762 |
| current_batch_512 | 126.567 | 41.592 | 480,856 |
| current_batch_2048 | 127.606 | 61.087 | 327,400 |

## Separate ICU Tokenizer

- Cold median: 54.846 ms; warm median: 48.015 ms.
- This measures `document::pdb.icu::text[]` only. It is not added to precomputed character-array timings; ICU arrays can have different token grouping from one-character arrays.

## Validation and Limits

- Verified corpus rows: 20,000; ID-output SHA256: `cbc0ef46884eee68448c4730e51af7a41fe1f20b591f75b41263c49b1b141596`.
- Independent document gold cases: 12; includes restored coverage, umlaut, polyphony, ASCII, empty and NULL documents.
- No document is omitted, joined to an adjacent document, or accepted merely because both implementations share empty output.
- Raw timing samples, checksum signatures, exact baseline manifest, aliases, corpus/dictionary/source hashes and effective database settings are retained in JSON.
- Installed current DLL/installation SQL hashes and C-function catalog bindings are recorded separately from current source hashes to expose stale same-version installations.
- Fresh table relation sizes are recorded as validation context only. No BM25 index build or index-reduction claim is made; dictionary and output semantics are unchanged.
- Times include one client/server round trip per aggregate or INSERT but not transfer of all generated outputs. They are not per-document Python/database round-trip benchmarks.
- Batch sizes are bounded; this does not benchmark an unbounded array or establish a universal optimal batch size. Live dictionary version is checked per explicit batch, not cached for an entire transaction.
- Shared-host load, cache state and timing variance limit speedup claims. This protocol differs from P0/P1 and their absolute timings are not directly comparable.
