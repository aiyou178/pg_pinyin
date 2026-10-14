# Pinyin Auto vs Default Regex vs Exact Phrase

- Runtime base HEAD: `2224b7928bd48a68e3ba30323fa1a4d15d2c047e`; dirty source: `True`. Source, dictionary, fixture, scripts and installed-library hashes are retained in JSON.
- PostgreSQL: `PostgreSQL 19beta4 (Debian 19~beta4-1.pgdg13+1) on aarch64-unknown-linux-gnu, compiled by gcc (Debian 14.2.0-19) 14.2.0, 64-bit`; extensions: `{"pg_pinyin": "0.0.9", "pg_search": "0.26.1", "plpgsql": "1.0", "vector": "0.8.4"}`.
- Fresh UUID database; 20,000 dictionary-derived rows, 2,000 inputs per full/initials/autocomplete workload, 6 rotated timing rounds; top-k 10.
- Original default APIs are called directly. Auto is explicitly requested through mode `auto`. Native means `pinyin_phrase`, not full regex. All use the same plain whitespace BM25 index; generated/pipe fields are out of scope.
- Every timing round recreates the shared index from the same corpus. Each method warms its entire workload before timing; index construction, setup, connection startup, complete-hitset preflight, gold checks and EXPLAIN are excluded from search latency.
- Search latency includes PostgreSQL construction, BM25 matching/scoring, loopback round trips and ranked top-k transfer. Actual top-k IDs/scores must equal preflight and every timed method requires a real `TopKScanExecState` plan.
- Ranked-score fingerprints use finite float scores on both PostgreSQL JSON and direct-cursor boundaries (including canonical zero); exact numeric score and ordered ID equality remains mandatory, without an approximate tolerance.
- All methods transfer their full hitsets during untimed verification. Rust/SQL IDs, scores, rankings and Auto query serialization must match. Source-hit coverage is reported without filtering any method's failed partial queries; it is a known-source probe, not a broad relevance recall estimate.
- Initial inputs use each source syllable's first letter separated by spaces. Completion inputs remove the final letter of the last source syllable when possible. Valid-looking initials and completions remain inherently ambiguous; workloads are not cherry-picked to make Auto succeed.
- Exact phrase is evaluated for all intents, including its false negatives on partial input, but native initials/completion latency is deliberately omitted: empty or exact-only responses are not the same successful service.
- Auto routing counts are read from the installed `pdb.query` JSON, not guessed from input or regex patterns. Constructor cold timing means a new backend, not cold storage or shared buffers.
- Native phrase-prefix expansion is explicitly limited to 16,384 terms, not the upstream default of 50. A finite ceiling still may truncate broader external vocabularies; this plain pinyin corpus does not prove unbounded prefix equivalence.
- Run condition: Cached experimental PG19 beta4 pg_search 0.26.1 source compatibility image, not an official upstream PG19 binary package or a fresh full Docker recipe build.
- Run condition: Release-mode pg_pinyin, CARGO_BUILD_JOBS=2; all validation/build jobs completed before measurements.
- Run condition: Shared Docker VM: 8 CPUs, about 16 GB RAM; host swap in use immediately before run was 31,071.25 MiB. Exploratory same-run comparison, not a universal speedup claim.

## Actual Hit Scope and Source Coverage

Scope is measured over every matching query/document pair, not only top-k. Removed default matches are not automatically errors; relevance requires the separate intent oracle.

| Workload | Strategy | Hit pairs | Default intersection | Default only | Strategy only | Changed queries | Source hits | Empty responses |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| full | default | 5,366 | 5,366 | 0 | 0 | 0 | 2,000/2,000 | 0 |
| full | auto | 4,148 | 4,148 | 1,218 | 0 | 234 | 2,000/2,000 | 0 |
| full | native | 3,477 | 3,477 | 1,889 | 0 | 355 | 2,000/2,000 | 0 |
| initials | default | 122,290 | 122,290 | 0 | 0 | 0 | 2,000/2,000 | 0 |
| initials | auto | 122,290 | 122,290 | 0 | 0 | 0 | 2,000/2,000 | 0 |
| initials | native | 0 | 0 | 122,290 | 0 | 2,000 | 0/2,000 | 2,000 |
| autocomplete | default | 10,873 | 10,873 | 0 | 0 | 0 | 1,781/2,000 | 215 |
| autocomplete | auto | 7,731 | 7,339 | 3,534 | 392 | 508 | 2,000/2,000 | 0 |
| autocomplete | native | 464 | 464 | 10,409 | 0 | 1,781 | 4/2,000 | 1,824 |

### Installed Auto Routes

| Workload | Native term | Native phrase | Native phrase prefix | Regex | Empty |
| --- | ---: | ---: | ---: | ---: | ---: |
| full | 0 | 1082 | 918 | 0 | 0 |
| initials | 0 | 0 | 0 | 2000 | 0 |
| autocomplete | 0 | 0 | 2000 | 0 | 0 |

## Intent-Labeled Accuracy

The reused independent contrastive gold fixture is unchanged from the previous regex/phrase study. Every case labels every document. Micro-averages pool TP/FP/FN/TN; `FDR=FP/(TP+FP)` is the fraction of returned matches that are false, while `FPR=FP/(FP+TN)` is the fraction of labeled negatives returned. Undefined denominators are `n/a`.
Auto cannot infer whether a valid final `an` is complete or a prefix from spelling alone. Its conservative prefix policy may preserve completion while adding false matches to complete intent. Native full remains the exact-intent reference, not the gold-label generator.

| Intent | Strategy | Cases | TP | FP | FN | TN | Precision | Recall | FDR | FPR |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| full | default | 20 | 70 | 20 | 0 | 910 | 77.78% | 100.00% | 22.22% | 2.15% |
| full | auto | 20 | 70 | 9 | 0 | 921 | 88.61% | 100.00% | 11.39% | 0.97% |
| full | native | 20 | 70 | 0 | 0 | 930 | 100.00% | 100.00% | 0.00% | 0.00% |
| initials | default | 6 | 30 | 3 | 0 | 267 | 90.91% | 100.00% | 9.09% | 1.11% |
| initials | auto | 6 | 30 | 3 | 0 | 267 | 90.91% | 100.00% | 9.09% | 1.11% |
| initials | native | 6 | 0 | 0 | 30 | 270 | n/a | 0.00% | n/a | 0.00% |
| autocomplete | default | 6 | 41 | 4 | 0 | 255 | 91.11% | 100.00% | 8.89% | 1.54% |
| autocomplete | auto | 6 | 41 | 0 | 0 | 259 | 100.00% | 100.00% | 0.00% | 0.00% |
| autocomplete | native | 6 | 16 | 0 | 25 | 259 | 100.00% | 39.02% | 0.00% | 0.00% |
| normalization | default | 8 | 12 | 8 | 22 | 358 | 60.00% | 35.29% | 40.00% | 2.19% |
| normalization | auto | 8 | 34 | 2 | 0 | 364 | 94.44% | 100.00% | 5.56% | 0.55% |
| normalization | native | 8 | 34 | 0 | 0 | 366 | 100.00% | 100.00% | 0.00% | 0.00% |

### Gold Returned IDs

| Intent/input | Relevant IDs | Default | Auto | Native | Auto route |
| --- | --- | --- | --- | --- | --- |
| full: `xi'an` | 1,2,3,8,9,10 | 1,2,3,4,5,6,7,8,9,10 | 1,2,3,4,8,9,10 | 1,2,3,8,9,10 | phrase_prefix |
| full: `xi an` | 1,2,3,8,9,10 | 1,2,3,4,5,6,7,8,9,10 | 1,2,3,4,8,9,10 | 1,2,3,8,9,10 | phrase_prefix |
| full: `xian` | 13,14,15 | 13,14,15 | 13,14,15 | 13,14,15 | regex |
| full: `an` | 1,2,3,5,6,7,8,9,10,11,12,16,18,20,21,22 | 1,2,3,4,5,6,7,8,9,10,11,12,16,17,18,19,20,21,22 | 1,2,3,4,5,6,7,8,9,10,11,12,16,17,18,19,20,21,22 | 1,2,3,5,6,7,8,9,10,11,12,16,18,20,21,22 | regex |
| full: `ang` | 4,17,19,21,22 | 4,17,19,21,22 | 4,17,19,21,22 | 4,17,19,21,22 | term |
| full: `xi ang` | 4 | 4 | 4 | 4 | phrase |
| full: `an xi` | 11 | 8,11 | 8,11 | 11 | phrase_prefix |
| full: `xi an` | 1,2,3,8,9,10,12 | 1,2,3,4,5,6,7,8,9,10,12 | 1,2,3,4,8,9,10,12 | 1,2,3,8,9,10,12 | regex |
| full: `an an` | 9,20 | 9,20,21,22 | 9,20,21 | 9,20 | phrase_prefix |
| full: `lv liang` | 23,24,29 | 23,24,25,29 | 23,24,29 | 23,24,29 | phrase |
| full: `lve liang` | 25 | 25 | 25 | 25 | phrase |
| full: `lu liang` | 27 | 27 | 27 | 27 | phrase |
| full: `shang hai` | 37,38,41 | 37,38,41 | 37,38,41 | 37,38,41 | phrase |
| full: `shang hao` | 39 | 39 | 39 | 39 | phrase |
| full: `shang hui` | 40 | 40 | 40 | 40 | phrase |
| full: `zhong guo` | 43,44,46 | 43,44,46 | 43,44,46 | 43,44,46 | phrase |
| full: `yin hang` | 44,48,49,50 | 44,48,49,50 | 44,48,49,50 | 44,48,49,50 | phrase |
| full: `wu han` | 30,31,32,35 | 30,31,32,35 | 30,31,32,35 | 30,31,32,35 | phrase_prefix |
| full: `wu han hu` | 30 | 30,32 | 30,32 | 30 | phrase_prefix |
| full: `wu han han` | 31 | 31 | 31 | 31 | phrase_prefix |
| initials: `whh` | 30,31,32,33,34,36 | 30,31,32,33,34,36 | 30,31,32,33,34,36 | (none) | regex |
| initials: `w h h` | 30,31,32,33,34,36 | 30,31,32,33,34,36 | 30,31,32,33,34,36 | (none) | regex |
| initials: `sh` | 37,38,39,40,41 | 24,35,37,38,39,40,41,42 | 24,35,37,38,39,40,41,42 | (none) | regex |
| initials: `s h` | 37,38,39,40,41 | 37,38,39,40,41 | 37,38,39,40,41 | (none) | regex |
| initials: `zg` | 43,44,45,46 | 43,44,45,46 | 43,44,45,46 | (none) | regex |
| initials: `yh` | 44,48,49,50 | 44,48,49,50 | 44,48,49,50 | (none) | regex |
| autocomplete: `xi a` | 1,2,3,4,8,9,10 | 1,2,3,4,5,6,7,8,9,10 | 1,2,3,4,8,9,10 | (none) | phrase_prefix |
| autocomplete: `lv lia` | 23,24,29 | 23,24,25,29 | 23,24,29 | (none) | phrase_prefix |
| autocomplete: `shang ha` | 37,38,39,41 | 37,38,39,41 | 37,38,39,41 | (none) | phrase_prefix |
| autocomplete: `zhong gu` | 43,44,45,46 | 43,44,45,46 | 43,44,45,46 | (none) | phrase_prefix |
| autocomplete: `yin h` | 44,48,49,50 | 44,48,49,50 | 44,48,49,50 | (none) | phrase_prefix |
| autocomplete: `an` | 1,2,3,4,5,6,7,8,9,10,11,12,16,17,18,19,20,21,22 | 1,2,3,4,5,6,7,8,9,10,11,12,16,17,18,19,20,21,22 | 1,2,3,4,5,6,7,8,9,10,11,12,16,17,18,19,20,21,22 | 1,2,3,5,6,7,8,9,10,11,12,16,18,20,21,22 | regex |
| normalization: `xī'ān` | 1,2,3,8,9,10 | (none) | 1,2,3,8,9,10 | 1,2,3,8,9,10 | phrase |
| normalization: `xi1 an1` | 1,2,3,8,9,10 | (none) | 1,2,3,8,9,10 | 1,2,3,8,9,10 | phrase |
| normalization: `lü liang` | 23,24,29 | (none) | 23,24,29 | 23,24,29 | phrase |
| normalization: `lǚ liáng` | 23,24,29 | (none) | 23,24,29 | 23,24,29 | phrase |
| normalization: `lu: liang` | 23,24,29 | (none) | 23,24,29 | 23,24,29 | phrase |
| normalization: `lu4 liang2` | 27 | (none) | 27 | 27 | phrase |
| normalization: `XI'AN` | 1,2,3,8,9,10 | 1,2,3,4,5,6,7,8,9,10 | 1,2,3,4,8,9,10 | 1,2,3,8,9,10 | phrase_prefix |
| normalization: `xi’an` | 1,2,3,8,9,10 | 1,2,3,4,5,6,7,8,9,10 | 1,2,3,4,8,9,10 | 1,2,3,8,9,10 | phrase_prefix |

## End-to-End Warm Indexed Performance

Medians over rotated rounds. Inputs are identical across methods within each workload; all queries, including default or Auto misses, remain timed. Coverage compares the actual source-hit mask, not merely counts or a presumed perfect default. Improved coverage means gains without losses; reduced coverage means losses without gains; mixed means both. Lower time with lost source hits is a quality tradeoff, not a like-for-like successful-search speedup.

| Workload | Method | Batch ms | Query p50 ms | Query p95 ms | Source hits | Coverage vs default (gained/lost) |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| full | rust_default | 1,238.865 | 0.590 | 0.926 | 2,000/2,000 | same (+0/-0) |
| full | rust_auto | 320.533 | 0.153 | 0.217 | 2,000/2,000 | same (+0/-0) |
| full | rust_native | 278.477 | 0.138 | 0.171 | 2,000/2,000 | same (+0/-0) |
| full | sql_default | 1,393.908 | 0.666 | 1.001 | 2,000/2,000 | same (+0/-0) |
| full | sql_auto | 984.145 | 0.485 | 0.592 | 2,000/2,000 | same (+0/-0) |
| full | sql_native | 858.965 | 0.422 | 0.515 | 2,000/2,000 | same (+0/-0) |
| initials | rust_default | 1,509.097 | 0.719 | 1.036 | 2,000/2,000 | same (+0/-0) |
| initials | rust_auto | 1,554.047 | 0.739 | 1.068 | 2,000/2,000 | same (+0/-0) |
| initials | sql_default | 2,105.065 | 0.952 | 1.691 | 2,000/2,000 | same (+0/-0) |
| initials | sql_auto | 2,710.942 | 1.310 | 1.733 | 2,000/2,000 | same (+0/-0) |
| autocomplete | rust_default | 1,286.653 | 0.604 | 0.943 | 1,781/2,000 | same (+0/-0) |
| autocomplete | rust_auto | 467.457 | 0.211 | 0.365 | 2,000/2,000 | improved (+219/-0) |
| autocomplete | sql_default | 1,483.978 | 0.701 | 1.048 | 1,781/2,000 | same (+0/-0) |
| autocomplete | sql_auto | 1,040.014 | 0.510 | 0.654 | 2,000/2,000 | improved (+219/-0) |

| Workload | Backend | Auto elapsed reduction vs default | Coverage vs default (gained/lost) |
| --- | --- | ---: | --- |
| full | rust | 74.13% | same (+0/-0) |
| full | sql | 29.40% | same (+0/-0) |
| initials | rust | -2.98% | same (+0/-0) |
| initials | sql | -28.78% | same (+0/-0) |
| autocomplete | rust | 63.67% | improved (+219/-0) |
| autocomplete | sql | 29.92% | improved (+219/-0) |

Positive reductions mean lower elapsed time, not a claim that semantics are identical. Scope changes and constructor choices both influence latency. Native partial timing is omitted intentionally; JSON retains native partial match coverage.

## Materialized Construction and Shared Index

Construction materializes actual query strings for all corpus rows, without BM25 execution.

| Method | Fresh-backend cold ms | Warm ms | Query bytes |
| --- | ---: | ---: | ---: |
| rust_default | 53.644 | 46.727 | 1,751,866 |
| rust_auto | 85.994 | 81.579 | 1,288,451 |
| rust_native | 49.588 | 45.777 | 1,175,168 |
| sql_default | 1,750.796 | 1,731.520 | 1,751,866 |
| sql_auto | 5,744.633 | 5,716.056 | 1,288,451 |
| sql_native | 5,299.836 | 5,312.928 | 1,175,168 |

Shared index: 3,350,528 bytes, 58,349 position tokens; median fresh rebuild 43.112 ms. Auto changes query construction, not stored fields, so no index-size reduction is expected.

## Limits

- Gold is small and deliberately contrastive, not representative production prevalence or a pronunciation benchmark.
- Dictionary-derived source-hit coverage checks a known relevant source but does not label every extra performance-corpus match.
- BM25 scores and rankings may change when Auto selects native term/phrase/prefix instead of regex; Rust/SQL parity is required only within the same strategy.
- Even with identical complete hitsets, Auto changes Top-K ID order for 128 full and 334 completion queries; all these changes use phrase_prefix in this corpus. Known-source coverage means a source occurs anywhere in the full hitset, not that it wins Top-1.
- The plain whitespace field is intentionally shared. This does not evaluate generated/pipe indexes, custom analyzers or a lower index footprint.
- Cold constructor means a new PostgreSQL connection. Indexed timing is warm and includes loopback client overhead; shared host load and caches limit generalization.
- JSON retains every actual ID set, source coverage, scores/ranks, constructed Auto query, gold labels, Top-K plan, individual timing sample and source/runtime hash.

## Validation Record

- Final implementation: 35 Rust unit tests, 139 Python tests, 22 data-pipeline tests and 1,585 pgTAP assertions across nine files passed.
- `cargo fmt --check` and library/benchmark-binary checks passed with no PostgreSQL features and with PG19. Release-mode installation preceded timing; no build/test jobs ran alongside measurements.
- Independent SQL-only databases, genuine v0.0.8 upgrade/fresh-function parity, missing-pg_search stubs and actual read-only parallel workers passed.
- The final benchmark completed all six rounds and removed its fresh UUID database. Earlier incomplete benchmark attempts were discarded, not spliced into this report.
- Shared-host outliers remain in all samples: SQL default initials batches range from 1,661.766 to 7,021.467 ms; Auto ranges from 2,587.008 to 9,476.711 ms. This is an exploratory comparison, not a universal speedup or evidence that a small Rust initials difference is significant.
- Version metadata and bundled dictionary data were not changed; existing development version is 0.0.9 and the latest published release remains v0.0.8.
- The committed `pinyin_auto.json.gz` is lossless; decompressed bytes match the original full JSON SHA-256 `6d3d52bb7dd544c898a6ffc41ae1023f9ce345e3c27f18f6ca8d18ae8942ce55`. No samples or hitsets were removed for packaging.
