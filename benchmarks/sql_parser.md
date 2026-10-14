# Standalone SQL Parser Benchmark

Measured 2026-10-14T04:37:59.563589+00:00.

Baseline `cbfcf07c468e046a4068fb46460e0b64699b6827` vs the recorded current SQL snapshot; no pg_pinyin extension or Rust backend is installed.

20,000 documents, 2,000 queries per mode, 3 rotated repeats. Fresh database pair per round; fresh backend per measured stage and complete workload warmup.

| Mode | Pattern Server ms old/new | Constructor Server ms old/new | Client Search ms old/new | Search Speedup |
| --- | ---: | ---: | ---: | ---: |
| compat | 175.337 / 149.774 | 177.899 / 151.004 | 1610.185 / 1540.101 | 1.046x |
| full | 544.235 / 450.228 | 551.146 / 459.823 | 1183.819 / 1049.690 | 1.128x |
| initials | 421.398 / 412.790 | 429.992 / 416.954 | 2444.626 / 2494.793 | 0.980x |
| autocomplete | 513.727 / 437.326 | 517.405 / 443.137 | 1639.190 / 1340.019 | 1.223x |
| auto | 715.597 / 555.779 | 727.720 / 570.212 | 1825.031 / 1639.135 | 1.113x |

## Interpretation

Pattern/constructor totals are server Execution Time for a MATERIALIZED aggregate that consumes every output, including serialization and aggregate overhead. Client search totals include construction, BM25 retrieval, roundtrip, result transfer, and identical client-side parity checks. This is not isolated PL/pgSQL CPU time.

First fresh-backend calls are recorded separately in raw data; they are not cold OS or cold shared-buffer measurements. Each stage warms its complete input workload. Baseline and current are compared only within the same mode, not across incompatible matching semantics.

Automatic prepared statements are disabled. Client query time includes per-query planning; prepared or generic plan performance is not benchmarked. Captured EXPLAIN plans therefore use the same unprepared execution policy as the timed queries.

Every workload pattern and serialized query is exactly equal. Full hit IDs and exact float scores, counts, top-K IDs, and top-K scores are compared in memory before timing; cryptographic digests are retained instead of giant hit lists. Handwritten pattern gold and sampled independent token-position oracles guard against equal-but-wrong outputs. Real ParadeDB score Top-K Custom Scan plans are required.

No accuracy improvement or index reduction is claimed. Corpus and indexed values are unchanged dictionary-derived short words, not representative business text. Index sizes are immediate physical bytes of fresh builds, not steady-state compaction measurements. Setup, dictionary loading, index building, and validation are outside query timings.

Independent raw-data audit found consistent paired patterns improvements for
full, autocomplete and Auto. Auto patterns fell 22.5%, 22.7% and 21.2% per round.
Initials patterns changed by -2.1%, -5.4% and +0.6% (negative means faster),
so its small median difference is not a stable speedup. Initials search was
slower in all paired rounds: 2.1%, 9.5% and 13.1%. The ratio of medians above
does not capture that paired trend. Compatibility search was also noisy: one
round 27.2% slower, two rounds 2.5% and 21.2% faster. Full search improved
7.4%-17.6%, autocomplete 11.7%-25.9%, and Auto 0.6%-11.6% in this run.
Do not generalize these three-round shared-host results to production workloads.

The [raw JSON](sql_parser.json.gz) and
[first-pass trial](sql_parser_first_pass.md) retain the evidence for both complete
matrices. Their source snapshots differ; only the final matrix represents the
current SQL implementation.

## Evidence

Raw uncompressed JSON SHA-256: `910ea9ef27196b1ee99abd2fafcd419dbd77fb63197822e34f499b398435cc80`.

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
- Shared macOS host; immediately before final run vm.swapusage total=39936.00M used=39293.12M free=642.88M (encrypted). High host memory pressure limits generalization; no other owned build/test jobs ran during timing.
- Validation: final SQL source passed 172 repository Python tests + 22 dictionary tests and 1585 pgTAP assertions; unchanged Rust source passed 35 tests and lib/benchmark cargo checks with and without pg19; cargo fmt --check passed. Benchmarked databases themselves install pg_search only and standalone SQL, not pg_pinyin.
- A complete first-pass matrix is retained separately as sql_parser_first_pass.md/json.gz. It exposed an initials parser regression; this final source serializes the inventory only once and skips the unused initials regex construction. All modes and stages are remeasured, not only the regressed mode.
- Immediately after final timing, macOS swap total=39936.00M used=39091.19M free=844.81M (encrypted).
- Rust checks ran in the container; `cargo fmt --check` passed on the host (rustfmt is not installed in this image).
- Before publication, source-inspection/call-shape tests were replaced with executable CLI coverage. The revised 169 repository Python tests, 22 dictionary tests and 1,585 pgTAP assertions all passed again. Production SQL and benchmark harness hashes are unchanged; raw timing evidence and its original validation notes remain intact.
