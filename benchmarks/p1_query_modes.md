# P1 Pinyin Query Modes Benchmark

- Runtime HEAD: `f863c42a60f76b3b14ad77e8564bc5d40e928e23`; PostgreSQL: `PostgreSQL 19beta4 (Debian 19~beta4-1.pgdg13+1) on aarch64-unknown-linux-gnu, compiled by gcc (Debian 14.2.0-19) 14.2.0, 64-bit`.
- The JSON records working-tree source hashes separately; the HEAD above is not a claim that the tested implementation is a released tag.
- Extensions: `{"pg_pinyin": "0.0.9", "pg_search": "0.26.1", "plpgsql": "1.0", "vector": "0.8.4"}`.
- Fresh isolated database, 20,000 dictionary-derived phrases, 2,000 queries per method/run, 3 rotated-order repeats, top-k 10.
- Corpus IDs and query selection are hash ordered. Curated gold is in a separate table, never forced into low-ID performance rows.
- Cold constructor means a new PostgreSQL backend, not cold storage, OS caches or shared buffers. Output byte-length sums force evaluation; actual arrays are checked outside timing.
- Every autocomplete constructor output is checked against explicit source syllable positions outside timing: completed syllables remain exact and the last shortened syllable stays one prefix position, for both plain and pipe-aware patterns.
- Full-query timing includes SQL construction, BM25 matching/scoring, client round trips and top-k result transfer. It is not a parser-only comparison.
- Compat and full use identical query strings. Initials and autocomplete use their own deterministic input forms, so timings across those semantic modes are not same-input parser comparisons.
- All modes use the same field bytes, positional tokens and whitespace-tokenized index. No stopword filtering or index-reduction claim is made.
- Performance `mixed` is pipe-wrapped single-reading storage, not a measured polyphonic/mixed-realistic workload. Genuine mixed plain/pipe tokens and polyphonic alternatives are covered by the separate adversarial gold.
- [Pinned pg_search tokenizer source](https://github.com/paradedb/paradedb/blob/529c77e9dffe4bd86b6b7971c019bb26b8eadbfa/docs/reference/tokenizers/available-tokenizers/whitespace.mdx); pipe alternatives remain one positional token.
- Run condition: Shared macOS host at start: total = 34816.00M  used = 33297.00M  free = 1519.00M  (encrypted). Docker VM has 8 CPUs and 15.6 GiB; unrelated workloads remain running. Timings are exploratory, not isolated-machine performance claims.
- Run condition: Experimental PG19 compatibility: pg_search 0.26.1 pinned source and repository patch built in cached SDK overlay image cb67405b38115285c20566b9eadede61b1521e5cc8f793bd35daf6e7dcdb2d77; not an official PG19 package or a clean end-to-end build of the complete Docker recipe.

## Independent Adversarial Gold

Hand-labeled positives AND negatives test an/ang, lv/lve, xi an/xian, earlier-token prefix leakage, initials and mixed pipe/plain storage. This is a targeted contract suite, not general pronunciation accuracy.
Precision is calculated only over explicitly labeled returned positives/negatives; unlabeled returns are retained in JSON, not silently treated as correct.

| Method | Cases | Labeled precision | False positives | Recall@10 | MRR |
| --- | ---: | ---: | ---: | ---: | ---: |
| rust/compat/plain | 3 | 0.6364 | 4 | 1.0000 | 1.0000 |
| sql/compat/plain | 3 | 0.6364 | 4 | 1.0000 | 1.0000 |
| rust/compat/mixed | 3 | 0.6364 | 4 | 1.0000 | 1.0000 |
| sql/compat/mixed | 3 | 0.6364 | 4 | 1.0000 | 1.0000 |
| rust/full/plain | 11 | 1.0000 | 0 | 1.0000 | 1.0000 |
| sql/full/plain | 11 | 1.0000 | 0 | 1.0000 | 1.0000 |
| rust/full/mixed | 11 | 1.0000 | 0 | 1.0000 | 1.0000 |
| sql/full/mixed | 11 | 1.0000 | 0 | 1.0000 | 1.0000 |
| rust/initials/plain | 2 | 1.0000 | 0 | 1.0000 | 1.0000 |
| sql/initials/plain | 2 | 1.0000 | 0 | 1.0000 | 1.0000 |
| rust/initials/mixed | 2 | 1.0000 | 0 | 1.0000 | 1.0000 |
| sql/initials/mixed | 2 | 1.0000 | 0 | 1.0000 | 1.0000 |
| rust/autocomplete/plain | 14 | 1.0000 | 0 | 1.0000 | 1.0000 |
| sql/autocomplete/plain | 14 | 1.0000 | 0 | 1.0000 | 1.0000 |
| rust/autocomplete/mixed | 14 | 1.0000 | 0 | 1.0000 | 1.0000 |
| sql/autocomplete/mixed | 14 | 1.0000 | 0 | 1.0000 | 1.0000 |

Compat and full overall totals have different case counts and must not be compared as an aggregate accuracy delta. The matched contrast below uses identical an, lv liang and zhan an inputs and the same independently labeled positives/negatives.
False-positive counts/rates are visible within top-k only; a truncated result set cannot establish the absence of all false positives.

| Matched group/mode | Cases | Labeled precision | Visible false positives | Visible labeled-negative rate |
| --- | ---: | ---: | ---: | ---: |
| rust/plain/compat | 3 | 0.6364 | 4 | 0.4444 |
| rust/plain/full | 3 | 1.0000 | 0 | 0.0000 |
| rust/mixed/compat | 3 | 0.6364 | 4 | 0.4444 |
| rust/mixed/full | 3 | 1.0000 | 0 | 0.0000 |
| sql/plain/compat | 3 | 0.6364 | 4 | 0.4444 |
| sql/plain/full | 3 | 1.0000 | 0 | 0.0000 |
| sql/mixed/compat | 3 | 0.6364 | 4 | 0.4444 |
| sql/mixed/full | 3 | 1.0000 | 0 | 0.0000 |

## Constructor-Only

Whole-corpus elapsed milliseconds, median of fresh-backend cold and warmed samples.

| Method | Cold ms | Warm ms |
| --- | ---: | ---: |
| rust/compat/plain | 17.093 | 12.152 |
| sql/compat/plain | 1,816.345 | 1,878.998 |
| rust/compat/mixed | 16.326 | 12.935 |
| sql/compat/mixed | 1,808.049 | 1,816.723 |
| rust/full/plain | 18.109 | 13.902 |
| sql/full/plain | 5,599.274 | 5,729.506 |
| rust/full/mixed | 23.487 | 15.359 |
| sql/full/mixed | 5,826.695 | 5,581.008 |
| rust/initials/plain | 18.140 | 12.593 |
| sql/initials/plain | 4,296.003 | 4,251.153 |
| rust/initials/mixed | 17.497 | 12.692 |
| sql/initials/mixed | 4,270.687 | 4,242.261 |
| rust/autocomplete/plain | 19.123 | 14.346 |
| sql/autocomplete/plain | 5,187.375 | 5,373.237 |
| rust/autocomplete/mixed | 21.785 | 14.803 |
| sql/autocomplete/mixed | 5,125.799 | 5,249.934 |

## Full Warm Indexed Queries

Synthetic dictionary-derived inputs measure performance and backend consistency, not independent retrieval accuracy. Returned IDs are verified before timing and again for every timed run; SQL/Rust parity is checked within each mode/storage only.

| Method | Batch ms | Query median ms | Query p95 ms |
| --- | ---: | ---: | ---: | ---: |
| rust/compat/plain | 1,378.673 | 0.634 | 1.050 |
| sql/compat/plain | 1,576.698 | 0.740 | 1.136 |
| rust/compat/mixed | 4,121.764 | 1.878 | 3.203 |
| sql/compat/mixed | 4,216.310 | 1.919 | 3.406 |
| rust/full/plain | 400.313 | 0.192 | 0.256 |
| sql/full/plain | 1,167.757 | 0.553 | 0.804 |
| rust/full/mixed | 4,867.482 | 2.216 | 3.886 |
| sql/full/mixed | 6,328.502 | 2.806 | 4.778 |
| rust/initials/plain | 2,059.790 | 0.952 | 1.532 |
| sql/initials/plain | 2,457.391 | 1.157 | 1.717 |
| rust/initials/mixed | 15,883.333 | 7.253 | 14.025 |
| sql/initials/mixed | 19,375.062 | 9.300 | 15.348 |
| rust/autocomplete/plain | 788.271 | 0.373 | 0.490 |
| sql/autocomplete/plain | 1,440.392 | 0.683 | 0.917 |
| rust/autocomplete/mixed | 9,140.413 | 4.309 | 6.208 |
| sql/autocomplete/mixed | 11,201.156 | 5.292 | 7.422 |

## Actual Index Footprint

The table and index are recreated for each build. Sizes are median actual relation bytes, not dictionary size estimates.

| Metric | Value |
| --- | ---: |
| rows | 20,000 |
| plain_field_bytes | 218,470 |
| mixed_field_bytes | 335,168 |
| position_tokens | 58,349 |
| table_relation_bytes | 1,597,440 |
| table_total_bytes | 5,619,712 |
| index_relation_bytes | 3,514,368 |
| index_total_bytes | 3,514,368 |
| Index build median ms | 78.520 |

This is one shared dual-field index containing both plain and pipe-wrapped fields. Its bytes cannot be attributed to a single mode or compared as an index-compression improvement.

Mode semantics intentionally differ; full and autocomplete are not required to reproduce compat result sets. Raw samples, ranked IDs, plans, source/corpus/fixture hashes and environment are retained in the JSON report.
Timing variability and page allocation granularity do not establish statistical speedups. This report uses a different protocol from P0; do not compare its absolute times with the prior dictionary report.
