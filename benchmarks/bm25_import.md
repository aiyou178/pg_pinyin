# Committed BM25 Import Benchmark

- Run: `2026-10-14T02:38:15.247385+00:00` to `2026-10-14T02:39:33.269899+00:00`.
- Source: `b29763b84155e933ad708de4ac54710bb1ffe4dd`; working tree dirty: `True`. No release/version claim.
- PostgreSQL: `PostgreSQL 19beta4 (Debian 19~beta4-1.pgdg13+1) on aarch64-unknown-linux-gnu, compiled by gcc (Debian 14.2.0-19) 14.2.0, 64-bit`; pg_search `0.26.1`; pg_pinyin development `0.0.9`.
- 20,000 raw documents, 3 rotated rounds per case; transaction rows [512, 4096, 20000]; independent-document batch 512.
- Release installed DLL SHA256: `b7f84954d694c2f4436c6c529a45152dfac3c6b59e0a501ab375f648542ec606`; matched declared release build artifact.
- Run condition: Experimental cached PG19 compatibility-patched pg_search 0.26.1 image; not an official upstream PG19 binary or a newly rebuilt image recipe. Image sha256:cb67405b38115285c20566b9eadede61b1521e5cc8f793bd35daf6e7dcdb2d77.
- Run condition: Docker VM: 8 logical CPUs, 16746086400 memory bytes. Shared macOS host pre-run vm.swapusage: total 33792.00M, used 32486.81M, free 1305.19M. No other owned build/test/benchmark jobs running; unrelated host activity is uncontrolled.
- Run condition: Current main source release library rebuilt and installed immediately before validation; genuine v0.0.8 upgrade baseline prepared separately and current release library restored. 127 Python tests, 875 pgTAP assertions, 16 Rust tests, library/benchmark checks and local cargo fmt passed before timing.

## Measurement Boundary

Total-ready includes client row/COPY serialization, raw-document upload, server romanization and array aggregation, heap/primary-key writes, BM25 writes or deferred build, all transaction commits, and ANALYZE+commit. A new backend starts each case; its first-use dictionary initialization is included. Repeated samples are fresh-backend runs, not warm-call microbenchmarks.
Source selection, dictionary installation, destination/temp-table DDL and empty online-index creation are outside timing. No raw source corpus or document batch arrays are preloaded into SQL tables. In `after_load`, BM25 build+commit is inside readiness; in `online`, the already-empty index is maintained during every commit.
`insert_generated` executes one SQL INSERT per document through psycopg executemany (batched/pipelined protocol), not one network roundtrip per document. Generated-column methods include server conversion in upload duration; stage methods record COPY upload and conversion+INSERT separately.
`copy_precomputed` uploads independently expected romanization. It excludes conversion and is only a lower-bound/control, not an equivalent end-to-end replacement or an automatic recommendation.

The performance CLI verifies a co-located Linux PostgreSQL backend before fingerprinting installed files. Installed DLL equality against the declared release artifact rejects a mismatched installation, not two identically stale files; rebuild that artifact from the recorded source before each run.

## Ready-Time Results

Medians in milliseconds; load includes import commits. Per-phase raw samples are in JSON.

| Index scenario | Tx rows | Method | Load ms | Build+commit ms | ANALYZE+commit ms | Total ready ms | Ready rows/s |
|---|---:|---|---:|---:|---:|---:|---:|
| online | 512 | insert_generated | 640.47 | 0.00 | 67.61 | 708.10 | 28,245 |
| online | 512 | copy_generated | 492.77 | 0.00 | 66.97 | 559.76 | 35,730 |
| online | 512 | copy_stage_scalar | 496.97 | 0.00 | 67.67 | 565.01 | 35,398 |
| online | 512 | copy_stage_batch | 316.34 | 0.00 | 66.93 | 383.68 | 52,127 |
| online | 512 | copy_precomputed | 178.53 | 0.00 | 67.19 | 246.14 | 81,254 |
| after_load | 512 | insert_generated | 510.86 | 79.57 | 67.19 | 658.57 | 30,369 |
| after_load | 512 | copy_generated | 427.60 | 80.79 | 67.71 | 577.49 | 34,633 |
| after_load | 512 | copy_stage_scalar | 476.97 | 73.41 | 67.35 | 625.70 | 31,964 |
| after_load | 512 | copy_stage_batch | 207.74 | 74.72 | 68.65 | 350.12 | 57,124 |
| after_load | 512 | copy_precomputed | 80.43 | 74.72 | 67.44 | 222.61 | 89,843 |
| online | 4,096 | insert_generated | 590.50 | 0.00 | 66.58 | 656.73 | 30,454 |
| online | 4,096 | copy_generated | 437.78 | 0.00 | 66.93 | 504.76 | 39,623 |
| online | 4,096 | copy_stage_scalar | 489.63 | 0.00 | 68.15 | 558.47 | 35,812 |
| online | 4,096 | copy_stage_batch | 255.31 | 0.00 | 66.98 | 322.07 | 62,098 |
| online | 4,096 | copy_precomputed | 96.99 | 0.00 | 67.57 | 164.62 | 121,494 |
| after_load | 4,096 | insert_generated | 452.90 | 74.12 | 66.74 | 599.52 | 33,360 |
| after_load | 4,096 | copy_generated | 376.34 | 75.69 | 67.26 | 520.40 | 38,432 |
| after_load | 4,096 | copy_stage_scalar | 391.66 | 73.23 | 66.75 | 531.16 | 37,653 |
| after_load | 4,096 | copy_stage_batch | 196.23 | 77.77 | 66.70 | 339.82 | 58,854 |
| after_load | 4,096 | copy_precomputed | 38.59 | 80.67 | 67.90 | 187.18 | 106,847 |
| online | 20,000 | insert_generated | 592.74 | 0.00 | 66.78 | 660.91 | 30,261 |
| online | 20,000 | copy_generated | 442.69 | 0.00 | 67.90 | 510.89 | 39,147 |
| online | 20,000 | copy_stage_scalar | 499.35 | 0.00 | 67.07 | 566.58 | 35,299 |
| online | 20,000 | copy_stage_batch | 240.28 | 0.00 | 67.06 | 307.49 | 65,044 |
| online | 20,000 | copy_precomputed | 100.80 | 0.00 | 68.33 | 168.10 | 118,979 |
| after_load | 20,000 | insert_generated | 461.73 | 79.43 | 66.90 | 611.12 | 32,727 |
| after_load | 20,000 | copy_generated | 356.37 | 78.57 | 68.91 | 506.85 | 39,459 |
| after_load | 20,000 | copy_stage_scalar | 422.31 | 78.61 | 68.68 | 573.58 | 34,869 |
| after_load | 20,000 | copy_stage_batch | 167.55 | 73.24 | 67.12 | 308.08 | 64,919 |
| after_load | 20,000 | copy_precomputed | 34.15 | 78.36 | 68.20 | 182.99 | 109,294 |

## Quality and Visibility

- All 12 handwritten gold documents preserved by all five methods and both index scenarios, including NULL, empty input, umlauts, traditional forms, polyphony and independent 银/行 boundaries.
- Every timed sample checked all 20,000 exact `(id, document, pinyin)` rows from another postcommit backend; reference SHA256 `b8e8fec8470398e099b56dd1ea10c3e549b670ecc847c8c3235a5ddb32674dc9`.
- Every sample checked 64 nonempty positional pinyin queries against complete independently expected ID sets, including source hit and real ranked Top-K. An executed JSON plan proves BM25 Custom Scan use. Search validation is outside import timing.

## Physical and Memory Evidence

MiB below are maximum observed samples over rounds, not allocator-instrumented query peaks. Sampling can miss short spikes. A fresh backend's lifetime VmHWM is separately labeled in JSON; container cgroup observations are shared-process evidence, not exclusive query memory.

| Index scenario | Tx rows | Method | BM25 MiB min..max | Backend sampled RSS MiB | Client sampled RSS MiB | Cgroup observed MiB | Global WAL MiB min..max |
|---|---:|---|---:|---:|---:|---:|---:|
| online | 512 | insert_generated | 5.11..5.11 | 115.4 | 113.9 | 2684.0 | 6.95..6.95 |
| online | 512 | copy_generated | 5.16..5.22 | 121.2 | 114.0 | 2689.9 | 3.75..3.80 |
| online | 512 | copy_stage_scalar | 5.20..5.25 | 125.9 | 114.1 | 2694.5 | 4.74..4.77 |
| online | 512 | copy_stage_batch | 5.58..5.59 | 127.6 | 114.1 | 2711.7 | 4.75..4.77 |
| online | 512 | copy_precomputed | 5.58..5.62 | 72.0 | 114.2 | 2674.7 | 3.81..3.83 |
| after_load | 512 | insert_generated | 3.44..3.44 | 120.1 | 114.2 | 2697.6 | 3.53..3.53 |
| after_load | 512 | copy_generated | 3.44..3.44 | 120.1 | 114.2 | 2698.2 | 2.60..2.60 |
| after_load | 512 | copy_stage_scalar | 3.44..3.44 | 125.1 | 114.3 | 2717.6 | 3.54..3.54 |
| after_load | 512 | copy_stage_batch | 3.44..3.44 | 125.9 | 114.4 | 2718.2 | 3.54..3.54 |
| after_load | 512 | copy_precomputed | 3.44..3.44 | 71.2 | 114.4 | 2682.8 | 2.60..2.60 |
| online | 4,096 | insert_generated | 5.15..5.15 | 114.7 | 114.5 | 2719.2 | 6.94..6.94 |
| online | 4,096 | copy_generated | 4.44..4.44 | 118.9 | 114.5 | 2737.5 | 2.80..2.81 |
| online | 4,096 | copy_stage_scalar | 4.47..4.47 | 124.7 | 114.6 | 2741.4 | 3.72..3.92 |
| online | 4,096 | copy_stage_batch | 4.52..4.52 | 125.6 | 114.7 | 2745.5 | 3.72..3.72 |
| online | 4,096 | copy_precomputed | 4.52..4.52 | 68.1 | 113.1 | 2575.3 | 2.80..2.80 |
| after_load | 4,096 | insert_generated | 3.44..3.44 | 119.0 | 113.1 | 2606.7 | 3.52..3.52 |
| after_load | 4,096 | copy_generated | 3.44..3.44 | 118.5 | 113.1 | 2625.2 | 2.61..2.70 |
| after_load | 4,096 | copy_stage_scalar | 3.44..3.44 | 123.9 | 113.2 | 2631.6 | 3.53..3.53 |
| after_load | 4,096 | copy_stage_batch | 3.44..3.44 | 125.5 | 113.2 | 2632.6 | 3.53..3.53 |
| after_load | 4,096 | copy_precomputed | 3.44..3.44 | 68.9 | 113.3 | 2594.4 | 2.61..2.61 |
| online | 20,000 | insert_generated | 5.15..5.52 | 120.1 | 113.4 | 2655.2 | 6.92..6.94 |
| online | 20,000 | copy_generated | 3.46..3.46 | 120.8 | 113.4 | 2650.4 | 2.58..2.58 |
| online | 20,000 | copy_stage_scalar | 3.46..3.46 | 125.1 | 113.5 | 2653.5 | 3.50..3.50 |
| online | 20,000 | copy_stage_batch | 3.46..3.46 | 125.9 | 113.5 | 2655.7 | 3.50..3.50 |
| online | 20,000 | copy_precomputed | 3.46..3.46 | 69.0 | 113.6 | 2629.0 | 2.58..2.58 |
| after_load | 20,000 | insert_generated | 3.44..3.44 | 119.3 | 113.7 | 2664.6 | 3.52..3.53 |
| after_load | 20,000 | copy_generated | 3.44..3.44 | 118.8 | 113.7 | 2663.5 | 2.61..2.61 |
| after_load | 20,000 | copy_stage_scalar | 3.44..3.44 | 122.4 | 113.7 | 2667.4 | 3.52..3.53 |
| after_load | 20,000 | copy_stage_batch | 3.44..3.44 | 123.8 | 113.8 | 2668.5 | 3.52..3.52 |
| after_load | 20,000 | copy_precomputed | 3.44..3.44 | 67.9 | 113.8 | 2645.0 | 2.61..2.61 |

## Limits

- LSN deltas are cluster-global and may include other databases/background activity; they are not attributed exclusive per-table WAL.
- The cgroup contains PostgreSQL, client and any other container processes. Observed RSS includes first-use dictionary caches and index-building state; no true per-query memory peak is claimed.
- This deterministic dictionary-derived corpus measures a specific workload, not production phrase accuracy, sustained ingestion, concurrent-write capacity or a universally optimal batch size.
- Transaction size changes commit count and segment behavior. Independent-document batch size is a separate conversion bound; it does not change phrase boundaries.
- Median phase durations need not sum to median readiness. Range/individual round samples, effective durability settings, physical sizes, complete hit sets and artifacts are retained in JSON.
