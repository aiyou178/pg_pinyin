# P0 Dictionary Benchmark

- Baseline ref: `v0.0.8` (`7398408499cab61c6bcbc047804d72f00fd71f6f`).
- Runtime/source HEAD: `7398408499cab61c6bcbc047804d72f00fd71f6f`; identical compiled extension for both variants.
- PostgreSQL: `PostgreSQL 19beta4 (Debian 19~beta4-1.pgdg13+1) on aarch64-unknown-linux-gnu, compiled by gcc (Debian 14.2.0-19) 14.2.0, 64-bit`.
- Extensions: `{"pg_pinyin": "0.0.8", "pg_search": "0.25.1", "plpgsql": "1.0", "vector": "0.8.4"}`.
- Corpus: 20,000 unique phrases; 2,000 full queries per run; 3 rotated-order repeats; top-k 10.
- Corpus uses deterministic proportional sampling across retained, restored, baseline-only and candidate-only phrase strata plus curated fixtures; identical inputs for both variants, not production traffic. All corpus IDs are SHA-256 ordered, so fixtures do not systematically win tied ranks.
- Dictionary coverage: 157,702 baseline phrases, 196,188 candidate phrases, 157,702 shared; 0 baseline-only and 38,486 candidate-only. Size/timing comparisons use the identical corpus, not total dictionary coverage as an index-reduction metric.
- Cold means fresh PostgreSQL backend/dictionary cache, not cold OS, PostgreSQL shared buffers, or storage.
- All loaded dictionary tables are explicitly ANALYZEd before timing; planner-statistics refresh is not part of cold cache timing.
- Exact-array input is `ARRAY[name]::text[]`, not ICU segmentation. SQL outputs keep reading alternatives; accuracy compares each serialized token's first reading, not byte equivalence with Rust.
- Rust regex tokens are embedded in the same candidate binary for both variants; SQL regex tokens use each variant's live table. This is a dictionary/index experiment, not an old/new parser binary comparison.

## Coverage Strata

Restored phrases are identified by the reproducible coverage audit, not inferred from new dictionary size.
Candidate-only synthetic queries measure retrieval consistency; they are not independent accuracy gold.

| Stratum | Dictionary phrases | Corpus phrases | Curated queries | Synthetic queries |
| --- | ---: | ---: | ---: | ---: |
| shared_retained | 93,611 | 9,555 | 40 | 928 |
| shared_restored | 64,091 | 6,531 | 15 | 635 |
| baseline_only | 0 | 0 | 0 | 0 |
| candidate_only | 38,486 | 3,914 | 0 | 382 |
| neither | 0 | 0 | 0 | 0 |

## Targeted Accuracy

Fixtures are hand-curated regressions, not an estimate of general Mandarin pronunciation accuracy.

| Backend | Baseline correct | Candidate correct |
| --- | ---: | ---: |
| rust_char | 0/7 | 7/7 |
| rust_word_text | 33/55 | 55/55 |
| rust_word_exact_array | 33/55 | 55/55 |
| sql_legacy_phrase_or_char | 33/55 | 55/55 |
| sql_word_exact_array | 33/55 | 55/55 |

## Romanization

Whole-corpus elapsed times, median across runs. Timed SQL sums output byte lengths; this forces conversion but avoids downloading every row. Results/checksums are validated outside timing.

| Backend | Baseline cold ms | Candidate cold ms | Baseline warm ms | Candidate warm ms | Warm change |
| --- | ---: | ---: | ---: | ---: | ---: |
| rust_char | 274.795 | 279.312 | 262.538 | 235.930 | -10.13% |
| rust_word_text | 452.381 | 421.598 | 333.270 | 292.435 | -12.25% |
| rust_word_exact_array | 723.409 | 685.418 | 640.367 | 576.284 | -10.01% |
| sql_legacy_phrase_or_char | 72,386.706 | 3,431.972 | 97,718.343 | 2,561.951 | -97.38% |
| sql_word_exact_array | 2,994.882 | 3,166.762 | 3,015.929 | 3,708.735 | +22.97% |

## Fields and Actual Index Size

| Metric | Baseline | Candidate | Change |
| --- | ---: | ---: | ---: |
| Word field UTF-8 bytes | 217,811 | 218,220 | +0.19% |
| Word field whitespace tokens | 57,922 | 58,291 | +0.64% |
| Char field UTF-8 bytes | 218,127 | 218,127 | +0.00% |
| Char field whitespace tokens | 58,291 | 58,291 | +0.00% |
| Table heap bytes | 1,474,560 | 1,474,560 | +0.00% |
| Table + all indexes bytes | 5,824,512 | 5,824,512 | +0.00% |
| bench_char_gin total bytes | 532,480 | 532,480 | +0.00% |
| bench_index_pkey total bytes | 466,944 | 466,944 | +0.00% |
| bench_pinyin_bm25 total bytes | 3,309,568 | 3,309,568 | +0.00% |

Build times and index sizes are medians of rotated-order runs, each recreating the table and indexes in a fresh backend. Raw per-build measurements are retained in JSON.

| Build stage | Baseline median ms | Candidate median ms |
| --- | ---: | ---: |
| Both Rust fields generation | 1,322.311 | 1,395.744 |
| BM25 index | 60.390 | 83.210 |
| Char GIN index | 42.786 | 42.022 |

## Full Indexed Queries

Warm client SQL round trips with query parsing, BM25 lookup, top-k scoring and result transfer. Median batch elapsed and median per-run latency quantiles; batch includes untimed per-query checksum work.

| Parser backend | Baseline batch ms | Candidate batch ms | Baseline query median/p95 ms | Candidate query median/p95 ms |
| --- | ---: | ---: | ---: | ---: |
| rust | 1,841.729 | 1,867.646 | 0.854/1.506 | 0.858/1.566 |
| sql | 2,196.411 | 2,175.125 | 1.000/1.705 | 0.996/1.694 |

| Group/backend | Baseline Recall@10 / MRR | Candidate Recall@10 / MRR |
| --- | ---: | ---: |
| curated/rust (55 queries) | 0.6000/0.4606 | 1.0000/0.8273 |
| curated/sql (55 queries) | 0.5636/0.4455 | 1.0000/0.8273 |
| synthetic_dictionary_derived/rust (1945 queries) | 0.9545/0.9476 | 0.9995/0.9924 |
| synthetic_dictionary_derived/sql (1945 queries) | 0.9530/0.9461 | 0.9995/0.9924 |
| curated:shared_restored/rust (15 queries) | 0.8667/0.6889 | 1.0000/0.8222 |
| curated:shared_restored/sql (15 queries) | 0.8000/0.6556 | 1.0000/0.8222 |
| curated:shared_retained/rust (40 queries) | 0.5000/0.3750 | 1.0000/0.8292 |
| curated:shared_retained/sql (40 queries) | 0.4750/0.3667 | 1.0000/0.8292 |
| synthetic_dictionary_derived:candidate_only/rust (382 queries) | 0.9254/0.9228 | 1.0000/0.9961 |
| synthetic_dictionary_derived:candidate_only/sql (382 queries) | 0.9254/0.9228 | 1.0000/0.9961 |
| synthetic_dictionary_derived:shared_restored/rust (635 queries) | 0.9591/0.9478 | 0.9984/0.9871 |
| synthetic_dictionary_derived:shared_restored/sql (635 queries) | 0.9591/0.9478 | 0.9984/0.9871 |
| synthetic_dictionary_derived:shared_retained/rust (928 queries) | 0.9634/0.9578 | 1.0000/0.9944 |
| synthetic_dictionary_derived:shared_retained/sql (928 queries) | 0.9601/0.9546 | 1.0000/0.9944 |

Curated relevance means the intended named phrase; synthetic relevance groups all corpus phrases with the reference dictionary's same complete syllable sequence (candidate when present, baseline fallback for baseline-only keys). Synthetic labels are not independent gold. Regex phrase remains prefix-expanding, so candidate ambiguity is still possible; results are not required to be byte-identical between corrected and legacy data. All result IDs, fixture outputs, hashes, raw timings and plans are retained in the JSON report.

Build-time variability and PostgreSQL page allocation granularity should not be interpreted as statistically established improvements. No stopword filtering or index reduction policy is applied in P0.

## Run Observations

Measured locally on 2026-10-13 in an 8-CPU Docker container with approximately
15.6 GiB available memory. Host `sysctl` snapshots reported swap usage of
28,331.12 MB before the run, 30,198.88 MB during round two, and 28,757.44 MB after
completion; load averages were respectively `7.06 6.82 8.56`, `13.69 10.65 9.83`
and `3.04 6.66 8.85`. These are manually recorded host observations, not metrics
collected by the benchmark harness. Large per-run timing variation remains in
the raw JSON; three repetitions under this pressure do not establish a stable
speedup.

The corpus contains 3,914 candidate-only phrases. The unchanged legacy SQL
phrase-or-character implementation first looks up the whole phrase, then falls
back to character conversion on a miss. Its large measured reduction is
consistent with better phrase coverage avoiding that fallback, not a changed
parser algorithm or an isolated estimate of fallback cost. SQL exact-array warm
conversion instead slows by 22.97%. Rust warm text conversion falls by 12.25%,
but the full Rust query batch rises by 1.41%, effectively unchanged at this
run's variability. Neither the actual BM25 size nor total table/index size
decreases.
