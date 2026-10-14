# Default Regex vs Exact Pinyin Phrase Tradeoff

- Runtime base HEAD: `2224b7928bd48a68e3ba30323fa1a4d15d2c047e`; working tree dirty: `True`. Exact source, fixture, corpus, script and installed-library hashes are retained in JSON.
- PostgreSQL: `PostgreSQL 19beta4 (Debian 19~beta4-1.pgdg13+1) on aarch64-unknown-linux-gnu, compiled by gcc (Debian 14.2.0-19) 14.2.0, 64-bit`; extensions: `{"pg_pinyin": "0.0.9", "pg_search": "0.26.1", "plpgsql": "1.0", "vector": "0.8.4"}`.
- Fresh UUID database; 20,000 reproducible dictionary-derived rows; 2,000 identical complete queries (2-10 syllables); 6 rotated repeats; top-k 10.
- Six independent public APIs: Rust and SQL default prefix regex, full regex control, and native exact term/phrase. Generated/pipe fields are not part of this plain-whitespace-index comparison.
- Default means the original `pinyin_regex_phrase(value,slope,NULL,false)`, not mode `full`. All strategies use the same installed extension library/dictionary and one shared index. Full and native share the full parser; default retains its legacy compatibility grammar.
- Full-client latency includes query construction, BM25 matching/scoring, round trips and ranked top-k transfer; connection startup, setup, complete-hitset checks, gold evaluation and EXPLAIN are not timed. Every measured method warms its entire workload first.
- All six recorded performance plans must use real `TopKScanExecState` on the shared BM25 index. Timed ranked IDs and scores must exactly match untimed verification for that method.
- Constructor timing materializes actual `pdb.query::text` values for every corpus row. Cold is a fresh PostgreSQL backend, not cold OS storage or shared buffers.
- Every sampled query transfers all matching IDs during preflight. Differences below are computed from actual ID intersections, not just top-k, counts or hashes. Rust/SQL IDs, ranking and scores must agree within each strategy; full-regex/native complete hitset equality is required.
- Hand-labeled gold is separate from dictionary-derived performance data. Each case exhaustively labels every gold document for its stated intent; corpus scope differences are not automatically classified as errors or measured recall.
- Run condition: PG19 beta4 / latest published pg_search 0.26.1 (release API checked 2026-10-14), source-built with repository compatibility patch in cached SDK image cb67405b38115285c20566b9eadede61b1521e5cc8f793bd35daf6e7dcdb2d77; experimental, not an official upstream PG19 package.
- Run condition: Shared macOS host; Docker VM has 8 CPUs and approximately 16 GB RAM. Host vm.swapusage was 27860.56M before validation. Rotated six-repeat medians are exploratory, not a statistical or universal speedup claim.
- Run condition: Same release-mode pg_pinyin library as the preceding native/full comparison, SHA256 998a52aad9db1f37d091773ae855fbfdc0e93aa3fae6dc61b34faa8acfbd9bad. No product function or version changes in this follow-up.
- Run condition: Timed queries contain 2-10 complete syllables. Partial-query accuracy is evaluated separately with hand-labeled gold; partial queries are not timed as successful native searches.

## Complete-Query Hit Scope

Of 2,000 complete inputs, 355 changed full hitsets and 1,645 did not.

| Query/document pairs | Count |
| --- | ---: |
| Default regex matches | 5,366 |
| Native exact matches | 3,477 |
| Intersection | 3,477 |
| Default only | 1,889 |
| Native only | 0 |

Native retained 64.80% of default matches in this sampled workload. This is scope retention, not relevance recall: additional prefix matches require an intent-specific oracle before they can be called true or false positives.
Default/native changed ranking for 355 queries, scores for 2,000, and top-k IDs for 354.
The full-regex control and native path have identical full hitsets; 0 queries changed ranking, 0 changed scores, and 0 changed top-k IDs.
Singleton term scoring need not equal constant-score regex scoring; neither hitset parity nor this sampled multi-syllable workload proves universal ranking equivalence.

### Representative Scope Differences

| Input | Default/native hits | Default-only documents (ID: name; plain tokens) |
| --- | ---: | --- |
| `gu ji` | 94/4 | 125: 太平廣記; `tai ping guang ji`<br>145: 伦敦国际金融期货交易所; `lun dun guo ji jin rong qi huo jiao yi suo`<br>644: 顾家; `gu jia`<br>786: 血管痉挛; `xue guan jing luan`<br>930: 国家所有制; `guo jia suo you zhi`<br>1041: 国家标准码; `guo jia biao zhun ma`<br>1398: 髋关节; `kuan guan jie`<br>2020: 國家主義; `guo jia zhu yi` |
| `shi ji` | 74/16 | 53: 柳條湖事件; `liu tiao hu shi jian`<br>170: 今日事今日畢; `jin ri shi jin ri bi`<br>307: 黄金时间; `huang jin shi jian`<br>712: 使盡; `shi jin`<br>758: 霎时间; `sha shi jian`<br>879: 分布式结构; `fen bu shi jie gou`<br>1074: 试讲; `shi jiang`<br>1143: 美丽岛事件; `mei li dao shi jian` |
| `ji gu` | 61/3 | 180: 節骨眼; `jie gu yan`<br>1037: 沉靜寡言; `chen jing gua yan`<br>1232: 必然结果; `bi ran jie guo`<br>1824: 结棍; `jie gun`<br>2284: 坚固; `jian gu`<br>2349: 嬌貴; `jiao gui`<br>3173: 金龟; `jin gui`<br>3533: 公私兼顧; `gong si jian gu` |
| `ji shi` | 70/15 | 307: 黄金时间; `huang jin shi jian`<br>420: 靓饰; `jing shi`<br>472: 不经世故; `bu jing shi gu`<br>704: 建始縣; `jian shi xian`<br>1247: 救世济民; `jiu shi ji min`<br>1264: 一般见识; `yi ban jian shi`<br>1500: 九十; `jiu shi`<br>1888: 金獅獎; `jin shi jiang` |
| `jin xi` | 41/3 | 751: 紧行无好步; `jin xing wu hao bu`<br>2369: 盡孝; `jin xiao`<br>2975: 液晶显示器; `ye jing xian shi qi`<br>5392: 金小蜂; `jin xiao feng`<br>5626: 驚現; `jing xian`<br>6162: 金县; `jin xian`<br>6216: 费尽心机; `fei jin xin ji`<br>6769: 井渫莫食; `jing xie mo shi` |
| `xi xing` | 32/3 | 1082: 显性; `xian xing`<br>2725: 惺惺相惜; `xing xing xiang xi`<br>4331: 行刑隊; `xing xing dui`<br>5109: 方向性; `fang xiang xing`<br>5891: 线性; `xian xing`<br>7173: 新兴产业; `xin xing chan ye`<br>7193: 官场现形记; `guan chang xian xing ji`<br>8083: 象形文字; `xiang xing wen zi` |
| `xia yi` | 29/4 | 151: 孝衣; `xiao yi`<br>1213: 誤入歧途效應; `wu ru qi tu xiao ying`<br>1238: 祸福相倚; `huo fu xiang yi`<br>2767: 一项一项地; `yi xiang yi xiang de`<br>3125: 湘阴; `xiang yin`<br>3470: 小營盤鎮; `xiao ying pan zhen`<br>3818: 溫室效應; `wen shi xiao ying`<br>6837: 小义大利; `xiao yi da li` |
| `xi wei` | 23/1 | 521: 原核細胞型微生物; `yuan he xi bao xing wei sheng wu`<br>3103: 衔尾相随; `xian wei xiang sui`<br>3371: 醋酸纤维; `cu suan xian wei`<br>5420: 独在异乡为异客; `du zai yi xiang wei yi ke`<br>9187: 官官相卫; `guan guan xiang wei`<br>10723: 顯微學; `xian wei xue`<br>11068: 玻璃纤维; `bo li xian wei`<br>11649: 首尾相卫; `shou wei xiang wei` |

## Intent-Labeled Accuracy

Micro-averages sum query/document TP, FP, FN and TN within each intent, not top-k. `FDR = FP/(TP+FP)` is the false-match fraction of returned results; `FPR = FP/(FP+TN)` is the false-positive fraction of labeled negatives. Undefined denominators are `n/a`, not zero.
Full and normalization intents are kept separate from initials/autocomplete. Native exact is not an initials/completion feature; empty native responses to partial input are counted as false negatives for those intents and are not included in complete-query speedup timing.
Rust/SQL gold outputs, IDs, rankings and scores agree within each strategy. The following shows the Rust rows once; JSON retains all six methods and every returned ID.

| Intent | Strategy | Cases | TP | FP | FN | TN | Precision | Recall | FDR | FPR |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| full | default | 20 | 70 | 20 | 0 | 910 | 77.78% | 100.00% | 22.22% | 2.15% |
| full | full | 20 | 70 | 0 | 0 | 930 | 100.00% | 100.00% | 0.00% | 0.00% |
| full | native | 20 | 70 | 0 | 0 | 930 | 100.00% | 100.00% | 0.00% | 0.00% |
| initials | default | 6 | 30 | 3 | 0 | 267 | 90.91% | 100.00% | 9.09% | 1.11% |
| initials | full | 6 | 0 | 0 | 30 | 270 | n/a | 0.00% | n/a | 0.00% |
| initials | native | 6 | 0 | 0 | 30 | 270 | n/a | 0.00% | n/a | 0.00% |
| autocomplete | default | 6 | 41 | 4 | 0 | 255 | 91.11% | 100.00% | 8.89% | 1.54% |
| autocomplete | full | 6 | 16 | 0 | 25 | 259 | 100.00% | 39.02% | 0.00% | 0.00% |
| autocomplete | native | 6 | 16 | 0 | 25 | 259 | 100.00% | 39.02% | 0.00% | 0.00% |
| normalization | default | 8 | 12 | 8 | 22 | 358 | 60.00% | 35.29% | 40.00% | 2.19% |
| normalization | full | 8 | 34 | 0 | 0 | 366 | 100.00% | 100.00% | 0.00% | 0.00% |
| normalization | native | 8 | 34 | 0 | 0 | 366 | 100.00% | 100.00% | 0.00% | 0.00% |

### All Gold Returned IDs

Every gold case uses independently stated positive/negative labels; the full list is retained in JSON and the fixture.

| Intent/input | Relevant IDs | Default returned | Full returned | Native returned |
| --- | --- | --- | --- | --- |
| full: `xi'an` | 1,2,3,8,9,10 | 1,2,3,4,5,6,7,8,9,10 | 1,2,3,8,9,10 | 1,2,3,8,9,10 |
| full: `xi an` | 1,2,3,8,9,10 | 1,2,3,4,5,6,7,8,9,10 | 1,2,3,8,9,10 | 1,2,3,8,9,10 |
| full: `xian` | 13,14,15 | 13,14,15 | 13,14,15 | 13,14,15 |
| full: `an` | 1,2,3,5,6,7,8,9,10,11,12,16,18,20,21,22 | 1,2,3,4,5,6,7,8,9,10,11,12,16,17,18,19,20,21,22 | 1,2,3,5,6,7,8,9,10,11,12,16,18,20,21,22 | 1,2,3,5,6,7,8,9,10,11,12,16,18,20,21,22 |
| full: `ang` | 4,17,19,21,22 | 4,17,19,21,22 | 4,17,19,21,22 | 4,17,19,21,22 |
| full: `xi ang` | 4 | 4 | 4 | 4 |
| full: `an xi` | 11 | 8,11 | 11 | 11 |
| full: `xi an` | 1,2,3,8,9,10,12 | 1,2,3,4,5,6,7,8,9,10,12 | 1,2,3,8,9,10,12 | 1,2,3,8,9,10,12 |
| full: `an an` | 9,20 | 9,20,21,22 | 9,20 | 9,20 |
| full: `lv liang` | 23,24,29 | 23,24,25,29 | 23,24,29 | 23,24,29 |
| full: `lve liang` | 25 | 25 | 25 | 25 |
| full: `lu liang` | 27 | 27 | 27 | 27 |
| full: `shang hai` | 37,38,41 | 37,38,41 | 37,38,41 | 37,38,41 |
| full: `shang hao` | 39 | 39 | 39 | 39 |
| full: `shang hui` | 40 | 40 | 40 | 40 |
| full: `zhong guo` | 43,44,46 | 43,44,46 | 43,44,46 | 43,44,46 |
| full: `yin hang` | 44,48,49,50 | 44,48,49,50 | 44,48,49,50 | 44,48,49,50 |
| full: `wu han` | 30,31,32,35 | 30,31,32,35 | 30,31,32,35 | 30,31,32,35 |
| full: `wu han hu` | 30 | 30,32 | 30 | 30 |
| full: `wu han han` | 31 | 31 | 31 | 31 |
| initials: `whh` | 30,31,32,33,34,36 | 30,31,32,33,34,36 | (none) | (none) |
| initials: `w h h` | 30,31,32,33,34,36 | 30,31,32,33,34,36 | (none) | (none) |
| initials: `sh` | 37,38,39,40,41 | 24,35,37,38,39,40,41,42 | (none) | (none) |
| initials: `s h` | 37,38,39,40,41 | 37,38,39,40,41 | (none) | (none) |
| initials: `zg` | 43,44,45,46 | 43,44,45,46 | (none) | (none) |
| initials: `yh` | 44,48,49,50 | 44,48,49,50 | (none) | (none) |
| autocomplete: `xi a` | 1,2,3,4,8,9,10 | 1,2,3,4,5,6,7,8,9,10 | (none) | (none) |
| autocomplete: `lv lia` | 23,24,29 | 23,24,25,29 | (none) | (none) |
| autocomplete: `shang ha` | 37,38,39,41 | 37,38,39,41 | (none) | (none) |
| autocomplete: `zhong gu` | 43,44,45,46 | 43,44,45,46 | (none) | (none) |
| autocomplete: `yin h` | 44,48,49,50 | 44,48,49,50 | (none) | (none) |
| autocomplete: `an` | 1,2,3,4,5,6,7,8,9,10,11,12,16,17,18,19,20,21,22 | 1,2,3,4,5,6,7,8,9,10,11,12,16,17,18,19,20,21,22 | 1,2,3,5,6,7,8,9,10,11,12,16,18,20,21,22 | 1,2,3,5,6,7,8,9,10,11,12,16,18,20,21,22 |
| normalization: `xī'ān` | 1,2,3,8,9,10 | (none) | 1,2,3,8,9,10 | 1,2,3,8,9,10 |
| normalization: `xi1 an1` | 1,2,3,8,9,10 | (none) | 1,2,3,8,9,10 | 1,2,3,8,9,10 |
| normalization: `lü liang` | 23,24,29 | (none) | 23,24,29 | 23,24,29 |
| normalization: `lǚ liáng` | 23,24,29 | (none) | 23,24,29 | 23,24,29 |
| normalization: `lu: liang` | 23,24,29 | (none) | 23,24,29 | 23,24,29 |
| normalization: `lu4 liang2` | 27 | (none) | 27 | 27 |
| normalization: `XI'AN` | 1,2,3,8,9,10 | 1,2,3,4,5,6,7,8,9,10 | 1,2,3,8,9,10 | 1,2,3,8,9,10 |
| normalization: `xi’an` | 1,2,3,8,9,10 | 1,2,3,4,5,6,7,8,9,10 | 1,2,3,8,9,10 | 1,2,3,8,9,10 |

## End-to-End Warm Indexed Performance

Only identical complete query inputs are timed; each source phrase must occur in every strategy's actual full hitset.

| Method | Batch ms | Query p50 ms | Query p95 ms |
| --- | ---: | ---: | ---: |
| rust_default | 1,292.054 | 0.610 | 0.962 |
| rust_full | 380.906 | 0.186 | 0.232 |
| rust_native | 286.320 | 0.139 | 0.174 |
| sql_default | 1,464.892 | 0.698 | 1.056 |
| sql_full | 981.670 | 0.479 | 0.605 |
| sql_native | 871.142 | 0.428 | 0.523 |

| Backend | Native elapsed reduction vs default | Native elapsed reduction vs full control |
| --- | ---: | ---: |
| rust | 77.84% | 24.83% |
| sql | 40.53% | 11.26% |

Positive reductions mean lower native elapsed time; negative reductions mean slower. The full control isolates native construction/retrieval changes without narrowing complete hitsets. The default comparison combines that change with prefix-expansion and result-scope differences; it cannot attribute all speedup to an implementation improvement.

### Unchanged vs Changed Hitsets

These are post-stratified sums of measured per-query latency, not separate timed batches or causal estimates. The same queries appear in each strategy for a subgroup.

| Hitset subgroup | Method | Queries | Latency sum median ms | Per-query median ms |
| --- | --- | ---: | ---: | ---: |
| unchanged_hitset | rust_default | 1,645 | 1,105.739 | 0.636 |
| unchanged_hitset | rust_full | 1,645 | 316.431 | 0.189 |
| unchanged_hitset | rust_native | 1,645 | 233.678 | 0.139 |
| unchanged_hitset | sql_default | 1,645 | 1,249.113 | 0.729 |
| unchanged_hitset | sql_full | 1,645 | 817.185 | 0.490 |
| unchanged_hitset | sql_native | 1,645 | 721.603 | 0.433 |
| changed_hitset | rust_default | 355 | 185.632 | 0.507 |
| changed_hitset | rust_full | 355 | 63.711 | 0.175 |
| changed_hitset | rust_native | 355 | 52.072 | 0.143 |
| changed_hitset | sql_default | 355 | 215.148 | 0.593 |
| changed_hitset | sql_full | 355 | 163.317 | 0.452 |
| changed_hitset | sql_native | 355 | 148.944 | 0.416 |

## Actual Query Construction

Milliseconds for the complete corpus, median over fresh-backend samples; not BM25 search.

| Method | Cold ms | Warm ms | Materialized bytes |
| --- | ---: | ---: | ---: |
| rust_default | 54.352 | 46.682 | 1,751,866 |
| rust_full | 52.603 | 49.651 | 1,635,168 |
| rust_native | 48.892 | 46.309 | 1,175,168 |
| sql_default | 1,800.084 | 1,820.366 | 1,751,866 |
| sql_full | 5,468.546 | 5,517.044 | 1,635,168 |
| sql_native | 5,412.358 | 5,530.232 | 1,175,168 |

## Shared Index

The plain whitespace BM25 index is shared by all methods: 3,350,528 bytes; 58,349 tokens. Median fresh rebuild: 38.585 ms. Database total includes dictionaries and the separate gold index.
No index-size improvement is expected from substituting a query constructor; this run does not change indexed values.

## Interpretation Limits

- Gold is a small, deliberately contrastive fixture, not representative production prevalence or a broad pronunciation/relevance benchmark.
- Performance inputs are deterministic dictionary readings with known source matches. Extra prefix matches are scope differences; only independent gold defines mis-matches.
- Native exact preserves complete-syllable intent while removing prefix leakage, but is not a drop-in replacement for initials or completion. Input normalization can also add native matches that the ASCII-only default rejects.
- Latency includes loopback client overhead and varies with the shared host/cache environment. Repeated rotated samples expose variance but do not establish statistical significance or a general speedup guarantee.
- JSON retains full ID sets, rank/score fingerprints, top-k results, per-query/run timings, gold labels, installed function definitions, real Top-K plans and runtime/source hashes.
