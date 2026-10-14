# Standalone SQL Parser Benchmark: First Pass

Measured 2026-10-14T04:25:47.593708+00:00.

Baseline `cbfcf07c468e046a4068fb46460e0b64699b6827` vs the recorded current SQL snapshot; no pg_pinyin extension or Rust backend is installed.

Historical implementation trial, not the final implementation result. This
snapshot serialized the inventory twice and built an unused regex string for
initials. Its three paired patterns samples regressed by 9.82%, 2.41% and 9.31%;
the subsequent implementation removes this redundant work and is measured
separately in [the final report](sql_parser.md). The raw first-pass evidence is
retained in [sql_parser_first_pass.json.gz](sql_parser_first_pass.json.gz).

20,000 documents, 2,000 queries per mode, 3 rotated repeats. Fresh database pair per round; fresh backend per measured stage and complete workload warmup.

| Mode | Pattern Server ms old/new | Constructor Server ms old/new | Client Search ms old/new | Search Speedup |
| --- | ---: | ---: | ---: | ---: |
| compat | 181.557 / 153.077 | 189.225 / 153.153 | 1840.555 / 1768.241 | 1.041x |
| full | 546.630 / 467.660 | 539.528 / 490.753 | 1219.334 / 1086.942 | 1.122x |
| initials | 422.630 / 452.434 | 438.405 / 448.641 | 2677.887 / 2223.817 | 1.204x |
| autocomplete | 541.835 / 489.170 | 537.387 / 496.512 | 1658.305 / 1525.611 | 1.087x |
| auto | 774.654 / 573.010 | 778.668 / 590.992 | 1960.717 / 1676.972 | 1.169x |

## Interpretation

Pattern/constructor totals are server Execution Time for a MATERIALIZED aggregate that consumes every output, including serialization and aggregate overhead. Client search totals include construction, BM25 retrieval, roundtrip, result transfer, and identical client-side parity checks. This is not isolated PL/pgSQL CPU time.

First fresh-backend calls are recorded separately in raw data; they are not cold OS or cold shared-buffer measurements. Each stage warms its complete input workload. Baseline and current are compared only within the same mode, not across incompatible matching semantics.

Automatic prepared statements are disabled. Client query time includes per-query planning; prepared or generic plan performance is not benchmarked. Captured EXPLAIN plans therefore use the same unprepared execution policy as the timed queries.

Every workload pattern and serialized query is exactly equal. Full hit IDs and exact float scores, counts, top-K IDs, and top-K scores are compared in memory before timing; cryptographic digests are retained instead of giant hit lists. Handwritten pattern gold and sampled independent token-position oracles guard against equal-but-wrong outputs. Real ParadeDB score Top-K Custom Scan plans are required.

No accuracy improvement or index reduction is claimed. Corpus and indexed values are unchanged dictionary-derived short words, not representative business text. Index sizes are immediate physical bytes of fresh builds, not steady-state compaction measurements. Setup, dictionary loading, index building, and validation are outside query timings.

## Evidence

Raw uncompressed JSON SHA-256: `e53fc12297a8b5a8887ae418a2428fe1bd67fc96f4708cbb286b57e2db3f282d`.

| Round | Implementation | BM25 bytes |
| --- | --- | ---: |
| 1 | baseline | 3,350,528 |
| 1 | current | 3,350,528 |
| 2 | current | 3,350,528 |
| 2 | baseline | 3,350,528 |
| 3 | baseline | 3,350,528 |
| 3 | current | 3,350,528 |

## Environment Notes

- Local cached experimental PG19 beta4/pg_search 0.26.1 image sha256:cb67405b38115285c20566b9eadede61b1521e5cc8f793bd35daf6e7dcdb2d77; not an official upstream PG19 binary. Docker Linux aarch64 VM: 8 CPUs, 16746086400 memory bytes.
- Shared macOS host; immediately before run vm.swapusage total=36864.00M used=36397.31M free=466.69M (encrypted). High host memory pressure limits generalization; no other owned build/test jobs ran during timing.
- Validation before timing: 172 repository Python tests + 22 dictionary tests, 1585 pgTAP assertions, 35 Rust tests, lib/benchmark cargo checks with and without pg19, cargo fmt --check. Benchmarked databases themselves install pg_search only and standalone SQL, not pg_pinyin.
- Immediately after the first pass, macOS swap total=38912.00M used=38274.06M free=637.94M (encrypted). Per-round autocomplete and search timings varied substantially; the medians are observations, not stable production speedup estimates.
