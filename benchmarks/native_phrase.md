# Exact Pinyin Phrase Benchmark

- Runtime base HEAD: `c33bb898cda2c1ce57b8b448e4182a943c046855`; working tree dirty: `True`; regex reference base: `c33bb89`. Exact candidate source and installed-library hashes are retained in JSON.
- PostgreSQL: `PostgreSQL 19beta4 (Debian 19~beta4-1.pgdg13+1) on aarch64-unknown-linux-gnu, compiled by gcc (Debian 14.2.0-19) 14.2.0, 64-bit`; extensions: `{"pg_pinyin": "0.0.9", "pg_search": "0.26.1", "plpgsql": "1.0", "vector": "0.8.4"}`.
- Fresh UUID database; 20,000 deterministic dictionary phrases; 2,000 identical full queries; 3 rotated repeats; top-k 10.
- This compares query-constructor paths in the same installed binary/parser, not a parser rewrite or two separately compiled baselines. The existing full regex API is unchanged; native term/phrase is opt-in.
- Actual `pdb.query` values are materialized and cast to text once per row for constructor timing. Cold means the first batch in a new PostgreSQL backend, not cold OS storage or shared buffers; one whole batch warms each backend before the warm sample.
- Full-client timing includes query construction, BM25 matching/scoring, round trips and ranked top-k transfer. Each measured run has a complete untimed workload warm-up; setup, gold, full-hitset verification and EXPLAIN are outside timing.
- Score ordering uses `ORDER BY pdb.score(id) DESC,id` without a cast in the sort expression. Every method's recorded plan must use `TopKScanExecState` on the shared BM25 index, not just any ParadeDB scan followed by a PostgreSQL sort.
- Every performance query is checked against all matching IDs (not just top-k), plus ranking and scores. SQL/Rust parity is required within a strategy. Timed ranked IDs and scores must exactly match that method's preflight.
- The corpus/query set is dictionary-derived and measures performance, not general pronunciation accuracy. Separate hand-written positive/negative gold covers exact an/ang, xi'an/xian, umlaut, boundaries, positive slop and empty/invalid input. A mixed-OR single-term gold query makes native BM25 score changes visible even if the sampled corpus contains only multi-syllable phrases.
- All methods share one plain-field whitespace-tokenized index. No index-reduction claim is made; pipe/polyphonic fields are deliberately outside this opt-in path.
- Run condition: PostgreSQL 19 beta4 / pg_search 0.26.1 source-built with repository compatibility patch in a cached SDK image (cb67405b38115285c20566b9eadede61b1521e5cc8f793bd35daf6e7dcdb2d77); experimental, not an official upstream PG19 package or a new full Docker build.
- Run condition: Shared macOS host with other workloads; vm.swapusage used 28247.94M before validation. Absolute timings and observed speedups are exploratory.
- Run condition: pg_pinyin candidate installed with cargo pgrx install --release; current full regex bodies are unchanged from rebased reference c33bb89. Earlier P0/P1/P2 reports use different protocols and are not directly comparable.
- Run condition: The 2000 timed dictionary-derived queries contain multiple syllables; single-syllable score/rank differences are covered separately by handwritten gold, not represented in timed performance averages.

## Semantic Comparison

Full hitsets agree for all 2,000 sampled inputs. Rust/SQL ranking and scores agree within each strategy.
Regex versus native: 0 queries changed full ranking; 0 changed scores; 0 changed top-k IDs.
These counts describe the sampled multi-syllable workload, not universal score equivalence. Separate single-term and mixed-OR gold makes the constant-regex versus native-BM25 difference visible; the new helper is not a transparent ranking replacement.

## Actual Query Construction

Elapsed milliseconds for the entire corpus, median over fresh-backend samples.

| Method | Cold ms | Warm ms | Materialized bytes |
| --- | ---: | ---: | ---: |
| rust_regex | 59.272 | 49.321 | 1,635,168 |
| rust_native | 51.468 | 45.005 | 1,175,168 |
| sql_regex | 5,523.572 | 5,560.614 | 1,635,168 |
| sql_native | 5,529.630 | 5,530.890 | 1,175,168 |

## Full Warm Indexed Queries

| Method | Batch ms | Query p50 ms | Query p95 ms |
| --- | ---: | ---: | ---: |
| rust_regex | 366.692 | 0.180 | 0.222 |
| rust_native | 282.677 | 0.138 | 0.181 |
| sql_regex | 1,037.258 | 0.495 | 0.651 |
| sql_native | 869.343 | 0.426 | 0.523 |

## Single Shared Index

The same plain-field index is rebuilt for each size/build sample, not once per strategy. Database size includes the dictionaries and separate gold table as well as the performance index.

| Metric | Value |
| --- | ---: |
| rows | 20,000 |
| plain_field_bytes | 218,470 |
| position_tokens | 58,349 |
| table_relation_bytes | 1,220,608 |
| table_total_bytes | 5,079,040 |
| index_relation_bytes | 3,350,528 |
| index_total_bytes | 3,350,528 |
| database_bytes | 38,451,743 |
| Index build median ms | 39.637 |

JSON retains all run samples, compact full-hitset/ranking/score fingerprints for every query, selected ranked results, real BM25 plans and runtime/source/corpus hashes. Absolute times are not directly comparable to the previous multi-mode dual-field protocol. Three repeats expose sample variability but do not establish a statistical speedup.
