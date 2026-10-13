#!/usr/bin/env python3
"""Compare committed and candidate dictionaries in isolated PostgreSQL databases.

The corpus and compiled extension are identical; only live dictionary CSVs differ.
Accuracy assertions and result checks run outside timed sections. No Dispatcher
repository, client tokenizer package, or external dataset is required.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import platform
import statistics
import subprocess
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo


ROOT = Path(__file__).resolve().parents[1]
DATA_FILES = {
    "mapping": ("pinyin_mapping.csv", "pinyin_mapping", "character, pinyin"),
    "tokens": ("pinyin_token.csv", "pinyin_token", "character, category"),
    "words": ("pinyin_words.csv", "pinyin_words", "word, pinyin"),
}
ROMANIZATION = {
    "rust_char": "public.pinyin_char_romanize(name)",
    "rust_word_text": "public.pinyin_word_romanize(name)",
    "rust_word_exact_array": "public.pinyin_word_romanize(ARRAY[name]::text[])",
    "sql_legacy_phrase_or_char": "public.characters2romanize(name)",
    "sql_word_exact_array": "public.icu_romanize_tokens(ARRAY[name]::text[])",
}
SEARCH = {
    "rust": "public.pinyin_regex_phrase(%s)",
    "sql": "public.sql_pinyin_regex_phrase(%s)",
}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(ROOT), *args])


def csv_rows(data: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(data.decode("utf-8"))))


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    location = (len(ordered) - 1) * fraction
    lower, upper = math.floor(location), math.ceil(location)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (location - lower)


def timings(values: list[float]) -> dict[str, Any]:
    return {
        "samples_ms": values,
        "median_ms": statistics.median(values) if values else 0.0,
        "p95_ms": percentile(values, 0.95),
        "min_ms": min(values) if values else 0.0,
    }


def display_ms(value: float) -> str:
    return f"{value:,.3f}"


def dictionary_metadata(data: dict[str, bytes]) -> dict[str, Any]:
    return {
        key: {
            "sha256": digest(content),
            "bytes": len(content),
            "rows": len(csv_rows(content)),
        }
        for key, content in data.items()
    }


def canonical_tokens(value: str | None) -> list[str]:
    """SQL character fallbacks contain alternatives; compare their first reading."""
    if value is None:
        return []
    return [part.strip("|").split("|")[0] for part in value.split()]


def load_restored_words(path: Path | None) -> set[str]:
    if path is None:
        return set()
    audit = json.loads(path.read_bytes())
    words = audit.get("restored_words") if isinstance(audit, dict) else None
    if not isinstance(words, list) or not all(isinstance(word, str) and word for word in words):
        raise ValueError("Coverage audit must contain a restored_words list of nonempty strings")
    if len(set(words)) != len(words):
        raise ValueError("Coverage audit contains duplicate restored_words")
    return set(words)


def coverage_categories(
    old_words: dict[str, str], new_words: dict[str, str], restored_words: set[str]
) -> dict[str, str]:
    old_keys, new_keys = set(old_words), set(new_words)
    shared_keys = old_keys & new_keys
    if not restored_words <= shared_keys:
        raise ValueError("Every audited restored word must exist in both benchmark dictionaries")
    categories = {}
    for word in old_keys | new_keys:
        if word in restored_words:
            category = "shared_restored"
        elif word in shared_keys:
            category = "shared_retained"
        elif word in old_keys:
            category = "baseline_only"
        else:
            category = "candidate_only"
        categories[word] = category
    return categories


def sample_strata(pools: dict[str, list[str]], count: int) -> list[str]:
    """Proportional deterministic sampling, representing every pool when possible."""
    pools = {category: words for category, words in pools.items() if words}
    available = sum(len(words) for words in pools.values())
    if count < 0 or count > available:
        raise ValueError(f"Cannot sample {count} unique words from {available} available words")
    if not count:
        return []
    allocations = {category: int(count >= len(pools)) for category in pools}
    remaining = count - sum(allocations.values())
    capacities = {category: len(words) - allocations[category] for category, words in pools.items()}
    capacity = sum(capacities.values())
    # Integer remainders avoid platform-specific floating point allocation drift.
    shares = {
        category: divmod(remaining * size, capacity) if remaining else (0, 0)
        for category, size in capacities.items()
    }
    for category, (whole, _) in shares.items():
        allocations[category] += whole
    remainder = count - sum(allocations.values())
    for category in sorted(shares, key=lambda item: (-shares[item][1], item))[:remainder]:
        allocations[category] += 1
    selected = []
    for category in sorted(pools):
        words = sorted(pools[category], key=lambda word: (digest(word.encode("utf-8")), word))
        selected.extend(words[:allocations[category]])
    return selected


def make_corpus(
    baseline: dict[str, bytes],
    candidate: dict[str, bytes],
    fixtures: list[dict[str, Any]],
    rows: int,
    restored_words: set[str] | None = None,
) -> tuple[list[tuple[int, str]], dict[str, str]]:
    old_words = dict(csv_rows(baseline["words"]))
    new_words = dict(csv_rows(candidate["words"]))
    protected = list(dict.fromkeys(fixture["text"] for fixture in fixtures))
    if len(protected) > rows:
        raise ValueError("--rows must be at least the accuracy fixture count")
    protected_set = set(protected)
    categories = coverage_categories(old_words, new_words, restored_words or set())
    pools: dict[str, list[str]] = defaultdict(list)
    for word, category in categories.items():
        if word not in protected_set:
            pools[category].append(word)
    words = protected + sample_strata(pools, rows - len(protected))
    # Fixtures must not always win the secondary ORDER BY id tie breaker.
    words.sort(key=lambda word: (digest(word.encode("utf-8")), word))
    return list(enumerate(words, 1)), new_words


def make_queries(
    corpus: list[tuple[int, str]],
    candidate_words: dict[str, str],
    fixtures: list[dict[str, Any]],
    limit: int,
    categories: dict[str, str] | None = None,
    baseline_words: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    categories = categories or {}
    expected_readings = dict(baseline_words or {})
    expected_readings.update(candidate_words)
    for fixture in fixtures:
        expected_readings[fixture["text"]] = " ".join(fixture["syllables"])
    ids_by_reading: dict[str, set[int]] = defaultdict(set)
    for ident, word in corpus:
        reading = expected_readings.get(word)
        if reading:
            ids_by_reading[reading].add(ident)
    id_by_word = {word: ident for ident, word in corpus}
    queries = [
        {
            "query": " ".join(fixture["syllables"]),
            "text": fixture["text"],
            "kind": "curated",
            "fixture_kind": fixture["kind"],
            "coverage_category": categories.get(fixture["text"], "neither"),
            "relevant_ids": [id_by_word[fixture["text"]]],
        }
        for fixture in fixtures
    ]
    fixture_words = {fixture["text"] for fixture in fixtures}
    pools: dict[str, list[str]] = defaultdict(list)
    for _, word in corpus:
        reading = expected_readings.get(word)
        if word in fixture_words or not reading:
            continue
        if not all(token.isascii() and token.isalpha() for token in reading.split()):
            continue
        pools[categories.get(word, "neither")].append(word)
    count = min(max(limit - len(queries), 0), sum(len(words) for words in pools.values()))
    for word in sorted(sample_strata(pools, count), key=lambda word: (digest(word.encode("utf-8")), word)):
        reading = expected_readings[word]
        queries.append(
            {
                "query": reading,
                "text": word,
                "kind": "synthetic_dictionary_derived",
                "coverage_category": categories.get(word, "neither"),
                "reading_source": "candidate_dictionary" if word in candidate_words else "baseline_dictionary",
                "relevant_ids": sorted(ids_by_reading[reading]),
            }
        )
    return queries[:limit]


def corpus_coverage(
    baseline_words: dict[str, str],
    candidate_words: dict[str, str],
    corpus: list[tuple[int, str]],
    categories: dict[str, str],
    queries: list[dict[str, Any]],
) -> dict[str, Any]:
    old_keys, new_keys = set(baseline_words), set(candidate_words)
    category_names = (
        "shared_retained", "shared_restored", "baseline_only", "candidate_only", "neither"
    )
    strata = {
        category: {
            "dictionary_words": sum(value == category for value in categories.values()),
            "corpus_words": sum(categories.get(word, "neither") == category for _, word in corpus),
            "queries": {
                kind: sum(query["kind"] == kind and query.get("coverage_category", "neither") == category
                          for query in queries)
                for kind in ("curated", "synthetic_dictionary_derived")
            },
        }
        for category in category_names
    }
    return {
        "baseline_words": len(old_keys),
        "candidate_words": len(new_keys),
        "shared_words": len(old_keys & new_keys),
        "baseline_only_words": len(old_keys - new_keys),
        "candidate_only_words": len(new_keys - old_keys),
        "restored_baseline_words": strata["shared_restored"]["dictionary_words"],
        "corpus_shared_words": strata["shared_retained"]["corpus_words"] + strata["shared_restored"]["corpus_words"],
        "corpus_baseline_only_words": strata["baseline_only"]["corpus_words"],
        "corpus_candidate_only_words": strata["candidate_only"]["corpus_words"],
        "corpus_neither_words": strata["neither"]["corpus_words"],
        "strata": strata,
        "sampling": "proportional union-key strata plus curated fixtures; SHA-256 ordered IDs and queries",
    }


def connect_database(pgurl: str, name: str):
    return psycopg.connect(make_conninfo(pgurl, dbname=name), autocommit=True)


def load_variant(
    pgurl: str,
    name: str,
    dictionaries: dict[str, bytes],
    corpus: list[tuple[int, str]],
) -> dict[str, Any]:
    with connect_database(pgurl, name) as connection, connection.cursor() as cursor:
        cursor.execute("CREATE EXTENSION pg_search CASCADE")
        cursor.execute("CREATE EXTENSION pg_pinyin")
        for path in (ROOT / "sql/pinyin.sql", ROOT / "sql/word.sql"):
            cursor.execute(path.read_text(encoding="utf-8"), prepare=False)
        cursor.execute(
            "TRUNCATE pinyin.pinyin_mapping, pinyin.pinyin_token, pinyin.pinyin_words"
        )
        for key, (_, table, columns) in DATA_FILES.items():
            with cursor.copy(f"COPY pinyin.{table} ({columns}) FROM STDIN WITH (FORMAT CSV)") as copy:
                copy.write(dictionaries[key])
        cursor.execute(
            "INSERT INTO pinyin.pinyin_mapping (character, pinyin) VALUES (' ', ' ') "
            "ON CONFLICT (character) DO NOTHING"
        )
        cursor.execute(
            "UPDATE pinyin.pinyin_dictionary_meta SET version = version + 1 WHERE singleton"
        )
        for table in ("pinyin_mapping", "pinyin_token", "pinyin_words"):
            cursor.execute(sql.SQL("ANALYZE pinyin.{}").format(sql.Identifier(table)))
        cursor.execute("CREATE TABLE bench_corpus (id bigint PRIMARY KEY, name text NOT NULL)")
        with cursor.copy("COPY bench_corpus (id, name) FROM STDIN") as copy:
            for row in corpus:
                copy.write_row(row)
        cursor.execute("ANALYZE bench_corpus")
        cursor.execute("SELECT version(), current_setting('server_version_num')")
        pg_version, pg_version_num = cursor.fetchone()
        cursor.execute("SELECT extname, extversion FROM pg_extension ORDER BY extname")
        versions = dict(cursor.fetchall())
        cursor.execute("SELECT pg_database_size(current_database())")
        db_size = cursor.fetchone()[0]
        cursor.execute(
            "SELECT name, setting, unit FROM pg_settings WHERE name IN "
            "('shared_buffers', 'work_mem', 'maintenance_work_mem', "
            "'max_parallel_workers', 'max_parallel_workers_per_gather', 'jit') ORDER BY name"
        )
        settings = {setting: {"value": value, "unit": unit} for setting, value, unit in cursor.fetchall()}
    return {
        "postgresql": pg_version,
        "server_version_num": pg_version_num,
        "extensions": versions,
        "database_bytes_before_index": db_size,
        "postgresql_settings": settings,
    }


def check_accuracy(
    pgurl: str, name: str, fixtures: list[dict[str, Any]]
) -> dict[str, Any]:
    results = []
    with connect_database(pgurl, name) as connection, connection.cursor() as cursor:
        cursor.execute("SET jit = off; SET max_parallel_workers_per_gather = 0", prepare=False)
        for fixture in fixtures:
            cursor.execute(
                "SELECT public.pinyin_char_romanize(%s), "
                "public.pinyin_word_romanize(%s), "
                "public.pinyin_word_romanize(ARRAY[%s]::text[]), "
                "public.characters2romanize(%s), "
                "public.icu_romanize_tokens(ARRAY[%s]::text[])",
                [fixture["text"]] * 5,
            )
            outputs = dict(zip(ROMANIZATION, cursor.fetchone()))
            assertions = {}
            for method, output in outputs.items():
                expected = fixture.get("char_syllables") if method == "rust_char" else fixture["syllables"]
                if expected is not None:
                    assertions[method] = canonical_tokens(output) == expected
            results.append(
                {
                    "text": fixture["text"],
                    "kind": fixture["kind"],
                    "expected_syllables": fixture["syllables"],
                    "expected_char_syllables": fixture.get("char_syllables"),
                    "outputs": outputs,
                    "passes": assertions,
                }
            )
    summary = {}
    for method in ROMANIZATION:
        scored = [row["passes"][method] for row in results if method in row["passes"]]
        summary[method] = {"passed": sum(scored), "total": len(scored)}
    return {"summary": summary, "cases": results}


def measure_romanization(pgurl: str, name: str, expression: str) -> dict[str, Any]:
    statement = f"SELECT sum(octet_length({expression})) FROM bench_corpus"
    with connect_database(pgurl, name) as connection, connection.cursor() as cursor:
        cursor.execute("SET jit = off; SET max_parallel_workers_per_gather = 0", prepare=False)
        start = time.perf_counter()
        cursor.execute(statement)
        cold_result = cursor.fetchone()[0]
        cold_ms = (time.perf_counter() - start) * 1000
        cursor.execute(statement)
        warmup_result = cursor.fetchone()[0]
        start = time.perf_counter()
        cursor.execute(statement)
        warm_result = cursor.fetchone()[0]
        warm_ms = (time.perf_counter() - start) * 1000
    if cold_result != warm_result or warm_result != warmup_result:
        raise RuntimeError(f"Unstable romanization output in {name}: {expression}")
    # Verify actual serialized outputs outside timing, not just aggregate lengths.
    checksum = hashlib.sha256()
    with connect_database(pgurl, name) as connection, connection.cursor() as cursor:
        cursor.execute("SET jit = off; SET max_parallel_workers_per_gather = 0", prepare=False)
        cursor.execute(f"SELECT id, {expression} FROM bench_corpus ORDER BY id")
        for ident, output in cursor:
            checksum.update(json.dumps([ident, output], ensure_ascii=False).encode("utf-8"))
            checksum.update(b"\n")
    return {"cold_ms": cold_ms, "warm_ms": warm_ms,
            "output_bytes_checksum": warm_result, "output_sha256": checksum.hexdigest()}


def build_indexes(pgurl: str, name: str, char_gin: bool) -> dict[str, Any]:
    with connect_database(pgurl, name) as connection, connection.cursor() as cursor:
        cursor.execute("SET jit = off; SET max_parallel_workers_per_gather = 0", prepare=False)
        cursor.execute("DROP TABLE IF EXISTS bench_index")
        cursor.execute(
            "CREATE TABLE bench_index (id bigint PRIMARY KEY, name text NOT NULL, "
            "pinyin text NOT NULL, char_pinyin text NOT NULL)"
        )
        start = time.perf_counter()
        cursor.execute(
            "INSERT INTO bench_index SELECT id, name, "
            "public.pinyin_word_romanize(ARRAY[name]::text[]), "
            "public.pinyin_char_romanize(name) FROM bench_corpus ORDER BY id"
        )
        generation_ms = (time.perf_counter() - start) * 1000
        start = time.perf_counter()
        cursor.execute(
            "CREATE INDEX bench_pinyin_bm25 ON bench_index USING bm25 (id, pinyin) "
            "WITH (key_field='id')"
        )
        bm25_build_ms = (time.perf_counter() - start) * 1000
        gin_build_ms = None
        if char_gin:
            cursor.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
            start = time.perf_counter()
            cursor.execute(
                "CREATE INDEX bench_char_gin ON bench_index USING gin (char_pinyin gin_trgm_ops)"
            )
            gin_build_ms = (time.perf_counter() - start) * 1000
        cursor.execute("ANALYZE bench_index")
        cursor.execute(
            "SELECT count(*), sum(octet_length(pinyin)), "
            "sum(cardinality(string_to_array(pinyin, ' '))), "
            "sum(octet_length(char_pinyin)), "
            "sum(cardinality(string_to_array(char_pinyin, ' '))) FROM bench_index"
        )
        row_count, word_bytes, word_tokens, char_bytes, char_tokens = cursor.fetchone()
        cursor.execute(
            "SELECT indexname, pg_relation_size(indexname::regclass), "
            "pg_total_relation_size(indexname::regclass) "
            "FROM pg_indexes WHERE tablename = 'bench_index' ORDER BY indexname"
        )
        indexes = {
            index: {"relation_bytes": relation, "total_bytes": total}
            for index, relation, total in cursor.fetchall()
        }
        cursor.execute(
            "SELECT pg_relation_size('bench_index'), pg_total_relation_size('bench_index')"
        )
        table_relation, table_total = cursor.fetchone()
        cursor.execute("SELECT id, pinyin, char_pinyin FROM bench_index ORDER BY id")
        rows = cursor.fetchall()
    checksum = hashlib.sha256()
    for ident, pinyin, char_pinyin in rows:
        checksum.update(json.dumps([ident, pinyin, char_pinyin], ensure_ascii=False).encode("utf-8"))
        checksum.update(b"\n")
    return {
        "generation_ms": generation_ms,
        "bm25_build_ms": bm25_build_ms,
        "char_gin_build_ms": gin_build_ms,
        "rows": row_count,
        "word_field_bytes": word_bytes,
        "word_field_tokens": word_tokens,
        "char_field_bytes": char_bytes,
        "char_field_tokens": char_tokens,
        "table_relation_bytes": table_relation,
        "table_total_bytes": table_total,
        "indexes": indexes,
        "field_sha256": checksum.hexdigest(),
    }


def summarize_indexes(samples: list[dict[str, Any]]) -> dict[str, Any]:
    stable = (
        "rows", "word_field_bytes", "word_field_tokens", "char_field_bytes",
        "char_field_tokens", "field_sha256",
    )
    for key in stable:
        if len({sample[key] for sample in samples}) != 1:
            raise RuntimeError(f"Index field drift across fresh rebuilds: {key}")
    summary = {key: samples[0][key] for key in stable}
    for key in ("generation_ms", "bm25_build_ms", "char_gin_build_ms"):
        values = [sample[key] for sample in samples if sample[key] is not None]
        summary[key] = statistics.median(values) if values else None
        summary[key.replace("_ms", "_timing")] = timings(values) if values else None
    for key in ("table_relation_bytes", "table_total_bytes"):
        summary[key] = int(statistics.median(sample[key] for sample in samples))
    summary["indexes"] = {
        index: {
            key: int(statistics.median(sample["indexes"][index][key] for sample in samples))
            for key in ("relation_bytes", "total_bytes")
        }
        for index in samples[0]["indexes"]
    }
    return summary


def search_statement(backend: str) -> str:
    return (
        "SELECT id FROM bench_index WHERE pinyin @@@ "
        + SEARCH[backend]
        + " ORDER BY pdb.score(id) DESC, id LIMIT %s"
    )


def relevance_summary(results: list[dict[str, Any]], top_k: int) -> dict[str, Any]:
    groups = {}
    selections = {
        kind: [result for result in results if kind == "all" or result["kind"] == kind]
        for kind in ("all", "curated", "synthetic_dictionary_derived")
    }
    categories = sorted({result.get("coverage_category", "neither") for result in results})
    for kind in ("curated", "synthetic_dictionary_derived"):
        for category in categories:
            selections[f"{kind}:{category}"] = [
                result for result in selections[kind]
                if result.get("coverage_category", "neither") == category
            ]
    for kind, selected in selections.items():
        recall = []
        hit_rate = []
        reciprocal_rank = []
        for result in selected:
            relevant = set(result["relevant_ids"])
            ids = result["returned_ids"]
            hits = relevant.intersection(ids)
            recall.append(len(hits) / len(relevant))
            hit_rate.append(bool(hits))
            reciprocal_rank.append(next((1 / rank for rank, ident in enumerate(ids, 1) if ident in relevant), 0))
        groups[kind] = {
            "queries": len(selected),
            f"recall_at_{top_k}": statistics.mean(recall) if recall else None,
            f"hit_rate_at_{top_k}": statistics.mean(hit_rate) if hit_rate else None,
            "mrr": statistics.mean(reciprocal_rank) if reciprocal_rank else None,
        }
    return groups


def check_search(
    pgurl: str,
    name: str,
    queries: list[dict[str, Any]],
    backend: str,
    top_k: int,
) -> dict[str, Any]:
    results = []
    statement = search_statement(backend)
    with connect_database(pgurl, name) as connection, connection.cursor() as cursor:
        cursor.execute("SET jit = off; SET max_parallel_workers_per_gather = 0", prepare=False)
        for query in queries:
            cursor.execute(statement, (query["query"], top_k))
            results.append({**query, "returned_ids": [row[0] for row in cursor.fetchall()]})
        cursor.execute("EXPLAIN (FORMAT JSON) " + statement, (queries[0]["query"], top_k))
        plan = cursor.fetchone()[0]
    return {
        "metrics": relevance_summary(results, top_k),
        "results": results,
        "sha256": digest(json.dumps(results, ensure_ascii=False, sort_keys=True).encode("utf-8")),
        "example_plan": plan,
    }


def measure_search(
    pgurl: str, name: str, queries: list[dict[str, Any]], backend: str, top_k: int
) -> dict[str, Any]:
    statement = search_statement(backend)
    latencies = []
    checksum = hashlib.sha256()
    with connect_database(pgurl, name) as connection, connection.cursor() as cursor:
        cursor.execute("SET jit = off; SET max_parallel_workers_per_gather = 0", prepare=False)
        for query in queries:
            cursor.execute(statement, (query["query"], top_k))
            cursor.fetchall()
        batch_start = time.perf_counter()
        for query in queries:
            start = time.perf_counter()
            cursor.execute(statement, (query["query"], top_k))
            ids = [row[0] for row in cursor.fetchall()]
            latencies.append((time.perf_counter() - start) * 1000)
            # Checksumming is outside each per-query latency measurement.
            checksum.update(json.dumps(ids).encode("ascii"))
            checksum.update(b"\n")
        batch_ms = (time.perf_counter() - batch_start) * 1000
    return {
        "batch_ms": batch_ms,
        "per_query": timings(latencies),
        "result_sha256": checksum.hexdigest(),
    }


def percent_change(old: float, new: float) -> str:
    return "n/a" if not old else f"{(new / old - 1) * 100:+.2f}%"


def render_markdown(report: dict[str, Any]) -> str:
    baseline, candidate = report["variants"]["baseline"], report["variants"]["candidate"]
    environment = candidate["environment"]
    lines = [
        "# P0 Dictionary Benchmark",
        "",
        f"- Baseline ref: `{report['baseline_ref']}` (`{report['baseline_git_sha']}`).",
        f"- Runtime/source HEAD: `{report['runtime_git_sha']}`; identical compiled extension for both variants.",
        f"- PostgreSQL: `{environment['postgresql']}`.",
        f"- Extensions: `{json.dumps(environment['extensions'], sort_keys=True)}`.",
        f"- Corpus: {report['configuration']['rows']:,} unique phrases; "
        f"{report['configuration']['queries']:,} full queries per run; "
        f"{report['configuration']['runs']} rotated-order repeats; top-k {report['configuration']['top_k']}.",
        "- Corpus uses deterministic proportional sampling across retained, restored, baseline-only and "
        "candidate-only phrase strata plus curated fixtures; identical inputs for both variants, not production traffic. "
        "All corpus IDs are SHA-256 ordered, so fixtures do not systematically win tied ranks.",
        f"- Dictionary coverage: {report['dictionary_coverage']['baseline_words']:,} baseline phrases, "
        f"{report['dictionary_coverage']['candidate_words']:,} candidate phrases, "
        f"{report['dictionary_coverage']['shared_words']:,} shared; "
        f"{report['dictionary_coverage']['baseline_only_words']:,} baseline-only and "
        f"{report['dictionary_coverage']['candidate_only_words']:,} candidate-only. "
        "Size/timing comparisons use the identical corpus, not total dictionary coverage as an index-reduction metric.",
        "- Cold means fresh PostgreSQL backend/dictionary cache, not cold OS, PostgreSQL shared buffers, or storage.",
        "- All loaded dictionary tables are explicitly ANALYZEd before timing; planner-statistics refresh is not part of cold cache timing.",
        "- Exact-array input is `ARRAY[name]::text[]`, not ICU segmentation. SQL outputs keep reading alternatives; "
        "accuracy compares each serialized token's first reading, not byte equivalence with Rust.",
        "- Rust regex tokens are embedded in the same candidate binary for both variants; SQL regex tokens use "
        "each variant's live table. This is a dictionary/index experiment, not an old/new parser binary comparison.",
        "",
        "## Coverage Strata",
        "",
        "Restored phrases are identified by the reproducible coverage audit, not inferred from new dictionary size.",
        "Candidate-only synthetic queries measure retrieval consistency; they are not independent accuracy gold.",
        "",
        "| Stratum | Dictionary phrases | Corpus phrases | Curated queries | Synthetic queries |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for category, values in report["dictionary_coverage"]["strata"].items():
        lines.append(
            f"| {category} | {values['dictionary_words']:,} | {values['corpus_words']:,} | "
            f"{values['queries']['curated']:,} | {values['queries']['synthetic_dictionary_derived']:,} |"
        )
    lines += [
        "",
        "## Targeted Accuracy",
        "",
        "Fixtures are hand-curated regressions, not an estimate of general Mandarin pronunciation accuracy.",
        "",
        "| Backend | Baseline correct | Candidate correct |",
        "| --- | ---: | ---: |",
    ]
    for method in ROMANIZATION:
        old = baseline["accuracy"]["summary"][method]
        new = candidate["accuracy"]["summary"][method]
        lines.append(f"| {method} | {old['passed']}/{old['total']} | {new['passed']}/{new['total']} |")
    lines += [
        "",
        "## Romanization",
        "",
        "Whole-corpus elapsed times, median across runs. Timed SQL sums output byte lengths; this forces "
        "conversion but avoids downloading every row. Results/checksums are validated outside timing.",
        "",
        "| Backend | Baseline cold ms | Candidate cold ms | Baseline warm ms | Candidate warm ms | Warm change |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for method in ROMANIZATION:
        old, new = baseline["romanization"][method], candidate["romanization"][method]
        old_warm, new_warm = old["warm"]["median_ms"], new["warm"]["median_ms"]
        lines.append(
            f"| {method} | {display_ms(old['cold']['median_ms'])} | {display_ms(new['cold']['median_ms'])} | "
            f"{display_ms(old_warm)} | {display_ms(new_warm)} | {percent_change(old_warm, new_warm)} |"
        )
    lines += ["", "## Fields and Actual Index Size", "", "| Metric | Baseline | Candidate | Change |", "| --- | ---: | ---: | ---: |"]
    metrics = [
        ("Word field UTF-8 bytes", "word_field_bytes"),
        ("Word field whitespace tokens", "word_field_tokens"),
        ("Char field UTF-8 bytes", "char_field_bytes"),
        ("Char field whitespace tokens", "char_field_tokens"),
        ("Table heap bytes", "table_relation_bytes"),
        ("Table + all indexes bytes", "table_total_bytes"),
    ]
    for label, key in metrics:
        old, new = baseline["index"][key], candidate["index"][key]
        lines.append(f"| {label} | {old:,} | {new:,} | {percent_change(old, new)} |")
    for index in sorted(baseline["index"]["indexes"]):
        old = baseline["index"]["indexes"][index]["total_bytes"]
        new = candidate["index"]["indexes"][index]["total_bytes"]
        lines.append(f"| {index} total bytes | {old:,} | {new:,} | {percent_change(old, new)} |")
    lines += [
        "", "Build times and index sizes are medians of rotated-order runs, each recreating the table "
        "and indexes in a fresh backend. Raw per-build measurements are retained in JSON.", "",
        "| Build stage | Baseline median ms | Candidate median ms |", "| --- | ---: | ---: |",
    ]
    for label, key in (("Both Rust fields generation", "generation_ms"), ("BM25 index", "bm25_build_ms"), ("Char GIN index", "char_gin_build_ms")):
        old, new = baseline["index"][key], candidate["index"][key]
        if old is not None and new is not None:
            lines.append(f"| {label} | {display_ms(old)} | {display_ms(new)} |")
    lines += [
        "",
        "## Full Indexed Queries",
        "",
        "Warm client SQL round trips with query parsing, BM25 lookup, top-k scoring and result transfer. "
        "Median batch elapsed and median per-run latency quantiles; batch includes untimed per-query checksum work.",
        "",
        "| Parser backend | Baseline batch ms | Candidate batch ms | Baseline query median/p95 ms | Candidate query median/p95 ms |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for backend in SEARCH:
        old, new = baseline["search_timing"][backend], candidate["search_timing"][backend]
        lines.append(
            f"| {backend} | {display_ms(old['batch']['median_ms'])} | {display_ms(new['batch']['median_ms'])} | "
            f"{display_ms(old['query_median_ms'])}/{display_ms(old['query_p95_ms'])} | "
            f"{display_ms(new['query_median_ms'])}/{display_ms(new['query_p95_ms'])} |"
        )
    top_k = report["configuration"]["top_k"]
    lines += ["", f"| Group/backend | Baseline Recall@{top_k} / MRR | Candidate Recall@{top_k} / MRR |", "| --- | ---: | ---: |"]
    groups = ["curated", "synthetic_dictionary_derived"] + [
        group for group in baseline["search_accuracy"]["rust"]["metrics"] if ":" in group
    ]
    for kind in groups:
        for backend in SEARCH:
            old = baseline["search_accuracy"][backend]["metrics"][kind]
            new = candidate["search_accuracy"][backend]["metrics"][kind]
            if old["queries"]:
                lines.append(
                    f"| {kind}/{backend} ({old['queries']} queries) | {old[f'recall_at_{top_k}']:.4f}/{old['mrr']:.4f} | "
                    f"{new[f'recall_at_{top_k}']:.4f}/{new['mrr']:.4f} |"
                )
    lines += [
        "",
        "Curated relevance means the intended named phrase; synthetic relevance groups all corpus phrases "
        "with the reference dictionary's same complete syllable sequence (candidate when present, baseline "
        "fallback for baseline-only keys). Synthetic labels are not independent gold. "
        "Regex phrase remains prefix-expanding, so candidate ambiguity is still possible; results are not required "
        "to be byte-identical between corrected and legacy data. All result IDs, fixture outputs, hashes, raw "
        "timings and plans are retained in the JSON report.",
        "",
        "Build-time variability and PostgreSQL page allocation granularity should not be interpreted as "
        "statistically established improvements. No stopword filtering or index reduction policy is applied in P0.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pgurl", required=True, help="Administrative DSN able to create temporary databases")
    parser.add_argument("--baseline-ref", default="v0.0.8", help="Pinned pre-P0 Git ref (default: v0.0.8)")
    parser.add_argument("--candidate-data", type=Path, default=ROOT / "sql/data")
    parser.add_argument("--coverage-audit", type=Path,
                        help="Coverage audit with restored_words (default: candidate data reports/coverage_audit.json if present)")
    parser.add_argument("--fixtures", type=Path, default=ROOT / "test/fixtures/pinyin_accuracy.json")
    parser.add_argument("--rows", type=int, default=20000)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--query-limit", type=int, default=2000)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--output-prefix", type=Path, default=ROOT / "benchmarks/p0_dictionary")
    parser.add_argument("--no-char-gin", action="store_true")
    parser.add_argument("--keep-databases", action="store_true")
    args = parser.parse_args()
    if args.rows < 1 or args.runs < 3 or args.query_limit < 1 or args.top_k < 1:
        parser.error("rows/query-limit/top-k must be positive, and --runs must be at least 3")
    fixture_bytes = args.fixtures.read_bytes()
    fixtures = json.loads(fixture_bytes)
    if not isinstance(fixtures, list) or not fixtures:
        parser.error("accuracy fixtures must be a nonempty JSON list")
    baseline = {
        key: git("show", f"{args.baseline_ref}:sql/data/{filename}")
        for key, (filename, _, _) in DATA_FILES.items()
    }
    candidate = {
        key: (args.candidate_data / filename).read_bytes()
        for key, (filename, _, _) in DATA_FILES.items()
    }
    audit_path = args.coverage_audit or args.candidate_data / "reports/coverage_audit.json"
    if not audit_path.is_file():
        if args.coverage_audit:
            parser.error(f"Coverage audit does not exist: {audit_path}")
        audit_path = None
    restored_words = load_restored_words(audit_path)
    corpus, candidate_words = make_corpus(baseline, candidate, fixtures, args.rows, restored_words)
    baseline_words = dict(csv_rows(baseline["words"]))
    categories = coverage_categories(baseline_words, candidate_words, restored_words)
    queries = make_queries(corpus, candidate_words, fixtures, args.query_limit, categories, baseline_words)
    if len(queries) < len(fixtures):
        parser.error("--query-limit must be at least the fixture count")
    names = {variant: f"pg_pinyin_p0_{uuid.uuid4().hex[:12]}_{variant}" for variant in ("baseline", "candidate")}
    report: dict[str, Any] = {
        "baseline_ref": args.baseline_ref,
        "baseline_git_sha": git("rev-parse", f"{args.baseline_ref}^{{commit}}").decode().strip(),
        "baseline_ref_object_sha": git("rev-parse", args.baseline_ref).decode().strip(),
        "runtime_git_sha": git("rev-parse", "HEAD").decode().strip(),
        # Nested submodule gitdir pointers may be host-absolute in Docker mounts.
        # Input CSV hashes, not nested working-tree status, identify benchmark data.
        "working_tree_status": git("status", "--porcelain", "--ignore-submodules=dirty").decode(),
        "fixture_sha256": digest(fixture_bytes),
        "benchmark_script_sha256": digest(Path(__file__).read_bytes()),
        "corpus_sha256": digest(json.dumps(corpus, ensure_ascii=False).encode("utf-8")),
        "coverage_audit_sha256": digest(audit_path.read_bytes()) if audit_path else None,
        "dictionary_coverage": corpus_coverage(baseline_words, candidate_words, corpus, categories, queries),
        "runtime_source_sha256": {
            str(path.relative_to(ROOT)): digest(path.read_bytes())
            for path in (ROOT / "src/lib.rs", ROOT / "src/regex_phrase.rs", ROOT / "Cargo.lock")
        },
        "client_environment": {
            "architecture": platform.machine(),
            "python": platform.python_version(),
            "psycopg": psycopg.__version__,
            "available_cpu_count": os.cpu_count(),
        },
        "configuration": {
            "rows": args.rows,
            "queries": len(queries),
            "runs": args.runs,
            "top_k": args.top_k,
            "char_gin": not args.no_char_gin,
        },
        "variants": {},
    }
    created: list[str] = []
    try:
        with psycopg.connect(args.pgurl, autocommit=True) as admin:
            for variant, data in (("baseline", baseline), ("candidate", candidate)):
                name = names[variant]
                admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
                created.append(name)
                print(f"[setup] {variant}: fresh database {name}", flush=True)
                environment = load_variant(args.pgurl, name, data, corpus)
                report["variants"][variant] = {
                    "database": name,
                    "environment": environment,
                    "dictionary": dictionary_metadata(data),
                    "accuracy": check_accuracy(args.pgurl, name, fixtures),
                    "romanization_samples": {method: [] for method in ROMANIZATION},
                    "search_samples": {backend: [] for backend in SEARCH},
                    "index_samples": [],
                }
        for round_index in range(args.runs):
            order = ("baseline", "candidate") if round_index % 2 == 0 else ("candidate", "baseline")
            print(f"[romanization] round {round_index + 1}/{args.runs}: {' -> '.join(order)}", flush=True)
            for method, expression in ROMANIZATION.items():
                for variant in order:
                    result = measure_romanization(args.pgurl, names[variant], expression)
                    report["variants"][variant]["romanization_samples"][method].append(result)
                    print(f"  {variant:9s} {method:27s} cold {result['cold_ms']:10.3f} ms  warm {result['warm_ms']:10.3f} ms", flush=True)
        for round_index in range(args.runs):
            order = ("baseline", "candidate") if round_index % 2 == 0 else ("candidate", "baseline")
            print(f"[index] round {round_index + 1}/{args.runs}: {' -> '.join(order)}", flush=True)
            for variant in order:
                result = build_indexes(args.pgurl, names[variant], not args.no_char_gin)
                report["variants"][variant]["index_samples"].append(result)
                print(f"  {variant:9s} fields {result['generation_ms']:10.3f} ms  BM25 {result['bm25_build_ms']:10.3f} ms", flush=True)
        for variant in ("baseline", "candidate"):
            report["variants"][variant]["index"] = summarize_indexes(report["variants"][variant]["index_samples"])
            report["variants"][variant]["search_accuracy"] = {}
            for backend in SEARCH:
                print(f"[accuracy] {variant}/{backend}: checking {len(queries):,} full-query result sets outside timing", flush=True)
                report["variants"][variant]["search_accuracy"][backend] = check_search(args.pgurl, names[variant], queries, backend, args.top_k)
        for round_index in range(args.runs):
            order = ("baseline", "candidate") if round_index % 2 == 0 else ("candidate", "baseline")
            backends = list(SEARCH) if round_index % 2 == 0 else list(reversed(SEARCH))
            print(f"[search] round {round_index + 1}/{args.runs}: {' -> '.join(order)}", flush=True)
            for backend in backends:
                for variant in order:
                    result = measure_search(args.pgurl, names[variant], queries, backend, args.top_k)
                    report["variants"][variant]["search_samples"][backend].append(result)
                    print(f"  {variant:9s} {backend:5s} batch {result['batch_ms']:10.3f} ms  median/p95 {result['per_query']['median_ms']:.3f}/{result['per_query']['p95_ms']:.3f} ms", flush=True)
        for variant in report["variants"].values():
            variant["romanization"] = {}
            for method, samples in variant["romanization_samples"].items():
                checksums = {sample["output_bytes_checksum"] for sample in samples}
                if len(checksums) != 1:
                    raise RuntimeError(f"Romanization checksum drift across repeats: {method}")
                if len({sample["output_sha256"] for sample in samples}) != 1:
                    raise RuntimeError(f"Exact romanization output drift across repeats: {method}")
                variant["romanization"][method] = {
                    "cold": timings([sample["cold_ms"] for sample in samples]),
                    "warm": timings([sample["warm_ms"] for sample in samples]),
                    "output_bytes_checksum": samples[0]["output_bytes_checksum"],
                    "output_sha256": samples[0]["output_sha256"],
                }
            variant["search_timing"] = {}
            for backend, samples in variant["search_samples"].items():
                checksums = {sample["result_sha256"] for sample in samples}
                if len(checksums) != 1:
                    raise RuntimeError(f"Search result ordering drift across repeats: {backend}")
                expected_results = variant["search_accuracy"][backend]["results"]
                checksum = hashlib.sha256()
                for result in expected_results:
                    checksum.update(json.dumps(result["returned_ids"]).encode("ascii"))
                    checksum.update(b"\n")
                if checksum.hexdigest() != samples[0]["result_sha256"]:
                    raise RuntimeError(f"Timed search differs from untimed verification: {backend}")
                variant["search_timing"][backend] = {
                    "batch": timings([sample["batch_ms"] for sample in samples]),
                    "query_median_ms": statistics.median(sample["per_query"]["median_ms"] for sample in samples),
                    "query_p95_ms": statistics.median(sample["per_query"]["p95_ms"] for sample in samples),
                    "result_sha256": samples[0]["result_sha256"],
                }
        output_json = args.output_prefix.with_suffix(".json")
        output_md = args.output_prefix.with_suffix(".md")
        output_json.parent.mkdir(parents=True, exist_ok=True)
        output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        output_md.write_text(render_markdown(report), encoding="utf-8")
        print(f"\n[report] {output_md}\n[raw data] {output_json}", flush=True)
    finally:
        if args.keep_databases:
            print(f"[retained databases] {', '.join(created)}", flush=True)
        elif created:
            with psycopg.connect(args.pgurl, autocommit=True) as admin:
                for name in created:
                    if not name.startswith("pg_pinyin_p0_"):
                        raise RuntimeError("Refusing to remove a database outside the benchmark namespace")
                    admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
                    print(f"[cleanup] removed {name}", flush=True)


if __name__ == "__main__":
    main()
