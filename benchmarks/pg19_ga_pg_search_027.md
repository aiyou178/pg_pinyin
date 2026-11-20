# PostgreSQL 19 GA and pg_search 0.27.0

Measured on 2026-11-20 (UTC+08:00) in Linux ARM64 containers with
pg_pinyin 0.0.9, pgrx 0.20.0 and Rust 1.99.1. Each of the four configurations
used a fresh database in each of four rotated rounds, with the same 20,000
documents and 2,000 queries. PostgreSQL 18.7 used the identical pg_pinyin
binary with both official ParadeDB packages. PG19 RC2 and 19.0 each used a
release build against their own PGDG headers, without pg_search.

## PG18.7 Search

Client wall times in milliseconds, median of four rounds. The search timed
one request per query, including the Rust query builder, BM25 lookup,
Top-10 ranking and the database round trip. Durability remained enabled.

| Workload | pg_search 0.26.1 | pg_search 0.27.0 | Change |
| --- | ---: | ---: | ---: |
| Ranked Top-10, 2,000 queries | 1,397.200 | 1,562.273 | +11.8% |
| Per-query p95 (median of round p95s) | 0.982 | 1.129 | +14.9% |
| First query in a fresh backend | 2.943 | 3.804 | +29.3% |
| COPY with generated pinyin and BM25, 20,000 rows | 369.409 | 366.473 | -0.8% |
| Search index bytes | 3,350,528 | 3,383,296 | +1.0% |

The Top-10 batch ranges were 1,360 to 1,422 ms and 1,399 to 1,666 ms respectively.
Both releases used real ParadeDB Top-K plans. All 5,367 scored hits across
the 2,000 queries matched exactly, including order and scores. The median
read regression is specific to this workload; the ranges overlap and this
run does not isolate the cause. Indexed COPY was effectively unchanged.

## PG19 Core

Median wall milliseconds per 20,000 rows; conversion and parsing are warm.
COPY includes upload, generated pinyin, primary-key writes and commit.

| Workload | 19 RC2 | 19.0 GA | Change |
| --- | ---: | ---: | ---: |
| Character conversion | 149.822 | 154.900 | +3.4% |
| Word conversion | 167.383 | 162.967 | -2.6% |
| Token-array conversion | 162.525 | 162.989 | +0.3% |
| Word conversion in batches of 512 | 24.770 | 24.668 | -0.4% |
| Dynamic regex patterns | 16.185 | 16.184 | <0.1% |
| Constant regex patterns | 5.023 | 4.709 | -6.2% |
| Core COPY | 269.458 | 260.286 | -3.4% |

All consumed outputs and stored pinyin matched. The COPY ranges were
267 to 282 ms and 253 to 290 ms. Most changes are small relative to the
round-to-round spread. There is no PG19 BM25 measurement:
[ParadeDB has not released PG19 support](https://github.com/paradedb/paradedb/issues/5405).

## Reproduce

Run on Linux with psycopg 3 and four disposable servers with pg_pinyin
installed. Preload the indicated pg_search version only on the PG18 targets.
The DSNs must allow benchmark database creation and deletion.

```bash
python3 scripts/benchmark_pgrx_upgrade.py \
  --server old_search "$PG18_0261_DSN" 18.7 0.26.1 \
  --server new_search "$PG18_0270_DSN" 18.7 0.27.0 \
  --server rc2_core "$PG19_RC2_DSN" 19rc2 - \
  --server ga_core "$PG19_GA_DSN" 19.0 - \
  --rows 20000 --queries 2000 --rounds 4 --output /tmp/pg-pinyin-platform
```

Validation on ARM64: PG18.7 / pg_search 0.27.0 passed 208 Python tests,
including upgrade regressions, and 1,588 pgTAP assertions. PG19.0 passed
45 native tests and 40 core pgTAP assertions. Raw timings, checksums and
plans were retained locally (uncompressed JSON SHA256
`190ea138e417de585496e09c760f10ba4372b59019e5885e83f5c9a594405107`).
