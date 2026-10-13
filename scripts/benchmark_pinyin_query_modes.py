#!/usr/bin/env python3
"""Benchmark explicit query modes on identical indexes in a fresh database.

Hand-labeled adversarial retrieval checks are separate from dictionary-derived
performance inputs. This script has no Dispatcher repository dependency.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import platform
import re
import statistics
import subprocess
import time
from typing import Any
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo


ROOT = Path(__file__).resolve().parents[1]
MODES = ("compat", "full", "initials", "autocomplete")
BACKENDS = ("rust", "sql")
STORAGES = ("plain", "mixed")
QUERY_COLUMNS = {
    "compat": "query_full", "full": "query_full",
    "initials": "query_initials", "autocomplete": "query_autocomplete",
}
TOKENIZER_SOURCE = (
    "https://github.com/paradedb/paradedb/blob/"
    "529c77e9dffe4bd86b6b7971c019bb26b8eadbfa/"
    "docs/reference/tokenizers/available-tokenizers/whitespace.mdx"
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def serialized_digest(rows: Any) -> str:
    return digest(json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def timings(values: list[float]) -> dict[str, Any]:
    return {"samples_ms": values, "median_ms": statistics.median(values) if values else 0.0,
            "p95_ms": percentile(values, 0.95), "min_ms": min(values) if values else 0.0}


def validate_fixtures(fixtures: dict[str, Any]) -> None:
    if not isinstance(fixtures, dict) or not fixtures.get("pattern_cases"):
        raise ValueError("Fixtures must include independent pattern cases")
    rows = fixtures.get("retrieval_rows", [])
    ids = {row["id"] for row in rows}
    if not ids or len(ids) != len(rows):
        raise ValueError("Retrieval fixture IDs must be nonempty and unique")
    queries = fixtures.get("retrieval_queries", [])
    if not queries:
        raise ValueError("Fixtures must include independently labeled retrieval queries")
    for case in fixtures["pattern_cases"]:
        if case["mode"] not in MODES or not isinstance(case["generated"], bool):
            raise ValueError("Invalid pattern mode or storage selector")
    for query in queries:
        positive, negative = set(query["positive_ids"]), set(query["negative_ids"])
        if query["mode"] not in MODES or not positive or not negative:
            raise ValueError("Every retrieval case requires a mode and positive AND negative labels")
        if positive & negative or not (positive | negative) <= ids:
            raise ValueError("Gold positive/negative IDs must be disjoint and refer to fixture rows")
        if not set(query.get("storages", STORAGES)) <= set(STORAGES):
            raise ValueError("Unknown fixture storage")


def read_words(data: bytes) -> dict[str, str]:
    rows = list(csv.reader(io.StringIO(data.decode("utf-8"))))
    words = dict(rows)
    if len(words) != len(rows):
        raise ValueError("Dictionary contains duplicate phrase keys")
    return words


def query_inputs(reading: str) -> dict[str, str]:
    syllables = reading.split()
    if not syllables or len(syllables) > 64 or len(reading) > 256:
        raise ValueError("Dictionary reading exceeds the mode input limits")
    if not all(token.isascii() and token.isalpha() and token.islower() for token in syllables):
        raise ValueError("Benchmark readings must contain normalized ASCII syllables")
    last = syllables[-1][:-1] if len(syllables[-1]) > 1 else syllables[-1]
    return {
        "compat": " ".join(syllables), "full": " ".join(syllables),
        "initials": " ".join(token[0] for token in syllables),
        "autocomplete": " ".join([*syllables[:-1], last]),
    }


def make_corpus(words: dict[str, str], rows: int) -> list[dict[str, Any]]:
    if rows < 1 or rows > len(words):
        raise ValueError("Requested corpus size is outside the dictionary size")
    # Gold rows are deliberately not inserted at low IDs or forced into this corpus.
    selected = sorted(words, key=lambda word: (digest(word.encode()), word))[:rows]
    return [
        {"id": ident, "name": word, "plain": words[word],
         "mixed": " ".join(f"|{syllable}|" for syllable in words[word].split()),
         "queries": query_inputs(words[word])}
        for ident, word in enumerate(selected, 1)
    ]


def make_queries(corpus: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    if count < 1 or count > len(corpus):
        raise ValueError("Query count must be positive and not exceed corpus size")
    selected = sorted(corpus, key=lambda row: (digest(("query:" + row["name"]).encode()), row["id"]))[:count]
    return [{"source_id": row["id"], "kind": "synthetic_dictionary_derived", "inputs": row["queries"]}
            for row in selected]


def method_key(backend: str, mode: str, storage: str) -> str:
    return f"{backend}/{mode}/{storage}"


def rotated_methods(round_index: int) -> list[tuple[str, str, str]]:
    methods = [(backend, mode, storage) for mode in MODES for storage in STORAGES for backend in BACKENDS]
    shift = (round_index * 5) % len(methods)
    methods = methods[shift:] + methods[:shift]
    return methods if round_index % 2 == 0 else list(reversed(methods))


def pattern_function(backend: str) -> str:
    if backend not in BACKENDS:
        raise ValueError("Unknown backend")
    return "public." + ("sql_" if backend == "sql" else "") + "pinyin_regex_phrase_patterns_mode"


def query_function(backend: str) -> str:
    if backend not in BACKENDS:
        raise ValueError("Unknown backend")
    return "public." + ("sql_" if backend == "sql" else "") + "pinyin_regex_phrase_mode"


def search_statement(backend: str, storage: str, table: str = "bench_index") -> str:
    if storage not in STORAGES or table not in ("bench_index", "bench_gold"):
        raise ValueError("Unknown benchmark relation or storage")
    return (
        f"SELECT id FROM {table} WHERE {storage} @@@ "
        f"{query_function(backend)}(%s,%s,NULL,NULL,%s) "
        "ORDER BY pdb.score(id) DESC,id LIMIT %s"
    )


def connection(pgurl: str, database: str):
    return psycopg.connect(make_conninfo(pgurl, dbname=database), autocommit=True)


def configure(cursor) -> None:
    cursor.execute("SET jit = off; SET max_parallel_workers_per_gather = 0", prepare=False)


def new_database_name() -> str:
    return f"pg_pinyin_p1_{uuid.uuid4().hex[:16]}"


def drop_database(admin, name: str) -> None:
    if not re.fullmatch(r"pg_pinyin_p1_[a-f0-9]{16}", name):
        raise ValueError("Refusing to drop a database outside this benchmark's namespace")
    admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@contextmanager
def temporary_database(pgurl: str, keep: bool = False):
    name = new_database_name()
    with psycopg.connect(pgurl, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
    print(f"[setup] fresh database {name}", flush=True)
    try:
        yield name
    finally:
        if keep:
            print(f"[retained database] {name}", flush=True)
        else:
            with psycopg.connect(pgurl, autocommit=True) as admin:
                drop_database(admin, name)
            print(f"[cleanup] removed {name}", flush=True)


def setup(pgurl: str, name: str, corpus: list[dict[str, Any]], fixtures: dict[str, Any], data: Path) -> dict[str, Any]:
    with connection(pgurl, name) as conn, conn.cursor() as cursor:
        configure(cursor)
        cursor.execute("CREATE EXTENSION pg_search CASCADE")
        cursor.execute("CREATE EXTENSION pg_pinyin")
        for path in (ROOT / "sql/pinyin.sql", ROOT / "sql/word.sql"):
            cursor.execute(path.read_text(encoding="utf-8"), prepare=False)
        cursor.execute("TRUNCATE pinyin.pinyin_mapping,pinyin.pinyin_token,pinyin.pinyin_words")
        for filename,table,columns in (
            ("pinyin_mapping.csv","pinyin_mapping","character,pinyin"),
            ("pinyin_token.csv","pinyin_token","character,category"),
            ("pinyin_words.csv","pinyin_words","word,pinyin"),
        ):
            with cursor.copy(f"COPY pinyin.{table} ({columns}) FROM STDIN WITH (FORMAT CSV)") as copy:
                copy.write((data / filename).read_bytes())
        cursor.execute("INSERT INTO pinyin.pinyin_mapping VALUES (' ',' ') ON CONFLICT (character) DO NOTHING")
        cursor.execute("UPDATE pinyin.pinyin_dictionary_meta SET version=version+1 WHERE singleton")
        for table in ("pinyin_mapping", "pinyin_token", "pinyin_words"):
            cursor.execute(sql.SQL("ANALYZE pinyin.{}").format(sql.Identifier(table)))
        cursor.execute(
            "CREATE TABLE bench_corpus (id bigint PRIMARY KEY,name text NOT NULL,plain text NOT NULL,"
            "mixed text NOT NULL,query_full text NOT NULL,query_initials text NOT NULL,query_autocomplete text NOT NULL)"
        )
        with cursor.copy("COPY bench_corpus FROM STDIN") as copy:
            for row in corpus:
                copy.write_row((row["id"],row["name"],row["plain"],row["mixed"],
                                row["queries"]["full"],row["queries"]["initials"],row["queries"]["autocomplete"]))
        cursor.execute("ANALYZE bench_corpus")
        cursor.execute("CREATE TABLE bench_gold (id bigint PRIMARY KEY,name text,plain text,mixed text)")
        with cursor.copy("COPY bench_gold FROM STDIN") as copy:
            for row in fixtures["retrieval_rows"]:
                copy.write_row((row["id"],row["name"],row["plain"],row["mixed"]))
        cursor.execute(
            "CREATE INDEX bench_gold_bm25 ON bench_gold USING bm25 "
            "(id,(plain::pdb.whitespace),(mixed::pdb.whitespace)) WITH (key_field='id')"
        )
        cursor.execute("ANALYZE bench_gold")
        cursor.execute("SELECT '|xing|hang|'::pdb.whitespace::text[]")
        if cursor.fetchone()[0] != ["|xing|hang|"]:
            raise RuntimeError("Whitespace tokenizer does not preserve pipe tokens")
        cursor.execute("SELECT version(),current_setting('server_version_num')")
        pg_version, version_num = cursor.fetchone()
        cursor.execute("SELECT extname,extversion FROM pg_extension ORDER BY extname")
        extensions = dict(cursor.fetchall())
        cursor.execute("SELECT name,setting,unit FROM pg_settings WHERE name IN "
                       "('shared_buffers','work_mem','maintenance_work_mem','jit','max_parallel_workers_per_gather') ORDER BY name")
        settings = {key: {"value": value, "unit": unit} for key,value,unit in cursor.fetchall()}
    return {"postgresql": pg_version, "server_version_num": version_num,
            "extensions": extensions, "settings": settings, "tokenizer_source": TOKENIZER_SOURCE,
            "tokenizer_probe": ["|xing|hang|"]}


def check_patterns(pgurl: str, name: str, fixtures: dict[str, Any]) -> list[dict[str, Any]]:
    results = []
    with connection(pgurl, name) as conn, conn.cursor() as cursor:
        configure(cursor)
        for case in fixtures["pattern_cases"]:
            outputs = {}
            for backend in BACKENDS:
                cursor.execute(f"SELECT {pattern_function(backend)}(%s,%s,%s)",
                               (case["input"],case["mode"],case["generated"]))
                outputs[backend] = cursor.fetchone()[0]
                if outputs[backend] != case["patterns"]:
                    raise RuntimeError(f"Independent pattern gold failed: {backend}/{case['label']}: {outputs[backend]!r}")
            results.append({**case, "outputs": outputs})
    return results


def labeled_metrics(results: list[dict[str, Any]], top_k: int) -> dict[str, Any]:
    positive_hits = false_positives = positive_count = negative_count = unlabeled = 0
    recalls, reciprocal_ranks = [], []
    for result in results:
        positive, negative = set(result["positive_ids"]), set(result["negative_ids"])
        returned = result["returned_ids"]
        positive_hits += len(positive.intersection(returned))
        false_positives += len(negative.intersection(returned))
        positive_count += len(positive)
        negative_count += len(negative)
        unlabeled += len(set(returned) - positive - negative)
        recalls.append(len(positive.intersection(returned)) / len(positive))
        reciprocal_ranks.append(next((1 / rank for rank, ident in enumerate(returned, 1) if ident in positive),0))
    labeled_returned = positive_hits + false_positives
    return {"queries": len(results), "positive_labels": positive_count, "negative_labels": negative_count,
            "positive_hits": positive_hits, "false_positive_hits": false_positives,
            "precision_on_labeled": positive_hits / labeled_returned if labeled_returned else None,
            "false_positive_rate_on_labeled_negatives": false_positives / negative_count if negative_count else None,
            f"recall_at_{top_k}": statistics.mean(recalls) if recalls else None,
            "mrr": statistics.mean(reciprocal_ranks) if reciprocal_ranks else None,
            "unlabeled_returned": unlabeled}


def matched_contrast_metrics(gold: dict[str, Any], top_k: int) -> dict[str, Any]:
    """Compare exact-intent compat/full cases with identical input and labels."""
    groups = {}
    for backend in BACKENDS:
        for storage in STORAGES:
            by_mode = {
                mode: {case["query"]: case for case in gold[method_key(backend,mode,storage)]["results"]}
                for mode in ("compat","full")
            }
            matched = {mode: [] for mode in by_mode}
            for query in sorted(set(by_mode["compat"]) & set(by_mode["full"])):
                old,new = by_mode["compat"][query],by_mode["full"][query]
                if set(old["positive_ids"]) != set(new["positive_ids"]):
                    raise ValueError("Matched exact-intent gold must have identical positive labels")
                positive = sorted(set(old["positive_ids"]))
                negative = sorted(set(old["negative_ids"]) | set(new["negative_ids"]))
                if set(positive) & set(negative):
                    raise ValueError("Matched exact-intent gold has conflicting positive/negative labels")
                for mode in by_mode:
                    matched[mode].append({"query": query, "positive_ids": positive,
                                          "negative_ids": negative,
                                          "returned_ids": by_mode[mode][query]["returned_ids"]})
            groups[f"{backend}/{storage}"] = {
                "cases": matched,
                "metrics": {mode: labeled_metrics(cases,top_k) for mode,cases in matched.items()},
            }
    return groups


def require_nonempty_outputs(outputs: list[Any], label: str) -> None:
    if not outputs or any(not output for output in outputs):
        raise RuntimeError(f"Empty performance output invalidates comparison: {label}")


def expected_autocomplete_patterns(reading: str, storage: str) -> list[str]:
    """Reference explicit source positions, independently of either tokenizer."""
    if storage not in STORAGES:
        raise ValueError("Unknown benchmark storage")
    syllables = reading.split()
    # The performance query removes exactly one final character (except nasals).
    last = syllables[-1][:-1] if len(syllables[-1]) > 1 else syllables[-1]
    if storage == "plain":
        return [*syllables[:-1],last + ".*"]
    return [*(f"({token}|.*\\|{token}\\|.*)" for token in syllables[:-1]),
            f"({last}.*|.*\\|{last}[^|]*\\|.*)"]


def validate_autocomplete_outputs(rows: list[tuple[int,str,list[str]]], storage: str) -> dict[str, Any]:
    expected = []
    actual = []
    for ident,reading,patterns in rows:
        reference = expected_autocomplete_patterns(reading,storage)
        if patterns != reference:
            raise RuntimeError(
                f"Autocomplete changed source syllable positions: {storage}/id={ident}/"
                f"reading={reading!r}: expected {reference!r}, actual {patterns!r}"
            )
        expected.append((ident,reference))
        actual.append((ident,patterns))
    return {"verified_rows": len(rows), "expected_sha256": serialized_digest(expected),
            "actual_sha256": serialized_digest(actual),
            "reference": "explicit source syllables: exact prior positions and one final shortened prefix"}


def check_gold(pgurl: str, name: str, fixtures: dict[str, Any], top_k: int) -> dict[str, Any]:
    result = {}
    for backend, mode, storage in rotated_methods(0):
        cases = [case for case in fixtures["retrieval_queries"]
                 if case["mode"] == mode and storage in case.get("storages", STORAGES)]
        outputs = []
        with connection(pgurl, name) as conn, conn.cursor() as cursor:
            configure(cursor)
            statement = search_statement(backend,storage,"bench_gold")
            for case in cases:
                cursor.execute(statement,(case["query"],mode,storage == "mixed",top_k))
                ids = [row[0] for row in cursor.fetchall()]
                # Compat intentionally records old false positives rather than hiding
                # them in labels; new modes must satisfy their explicit gold contract.
                if mode != "compat" and (not set(case["positive_ids"]) <= set(ids)
                                         or set(case["negative_ids"]) & set(ids)):
                    raise RuntimeError(f"Independent retrieval gold failed: {backend}/{mode}/{storage}/{case['label']}: {ids}")
                outputs.append({**case, "returned_ids": ids})
        result[method_key(backend,mode,storage)] = {
            "metrics": labeled_metrics(outputs,top_k), "results": outputs,
            "result_sha256": serialized_digest(outputs),
        }
    for mode in MODES:
        for storage in STORAGES:
            old, new = (result[method_key(backend,mode,storage)]["results"] for backend in BACKENDS)
            if old != new:
                raise RuntimeError(f"Ranked Rust/SQL gold parity failed in {mode}/{storage}")
    return result


def measure_constructor(pgurl: str, name: str, backend: str, mode: str, storage: str) -> dict[str, Any]:
    column = QUERY_COLUMNS[mode]
    expression = f"{pattern_function(backend)}({column},%s,%s)"
    statement = f"SELECT sum(octet_length(coalesce(array_to_string({expression},' '),''))) FROM bench_corpus"
    params = (mode,storage == "mixed")
    with connection(pgurl, name) as conn, conn.cursor() as cursor:
        configure(cursor)
        start = time.perf_counter()
        cursor.execute(statement,params)
        cold_output = cursor.fetchone()[0]
        cold_ms = (time.perf_counter() - start) * 1000
        cursor.execute(statement,params)
        warmup_output = cursor.fetchone()[0]
        start = time.perf_counter()
        cursor.execute(statement,params)
        warm_output = cursor.fetchone()[0]
        warm_ms = (time.perf_counter() - start) * 1000
        # Read actual arrays only after the timed output-length forcing queries.
        cursor.execute(f"SELECT id,plain,{expression} FROM bench_corpus ORDER BY id",params)
        source_outputs = cursor.fetchall()
    outputs = [(ident,patterns) for ident,_,patterns in source_outputs]
    require_nonempty_outputs([patterns for _,patterns in outputs],f"constructor/{backend}/{mode}/{storage}")
    autocomplete_reference = validate_autocomplete_outputs(source_outputs,storage) if mode == "autocomplete" else None
    if cold_output != warmup_output or warmup_output != warm_output:
        raise RuntimeError("Constructor output length drift between cold and warm calls")
    return {"cold_ms": cold_ms, "warm_ms": warm_ms, "output_bytes": warm_output,
            "output_sha256": serialized_digest(outputs), "autocomplete_reference": autocomplete_reference}


def build_index(pgurl: str, name: str) -> dict[str, Any]:
    with connection(pgurl, name) as conn, conn.cursor() as cursor:
        configure(cursor)
        cursor.execute("DROP TABLE IF EXISTS bench_index")
        cursor.execute("CREATE TABLE bench_index (id bigint PRIMARY KEY,name text,plain text,mixed text)")
        cursor.execute("INSERT INTO bench_index SELECT id,name,plain,mixed FROM bench_corpus ORDER BY id")
        start = time.perf_counter()
        cursor.execute("CREATE INDEX bench_modes_bm25 ON bench_index USING bm25 "
                       "(id,(plain::pdb.whitespace),(mixed::pdb.whitespace)) WITH (key_field='id')")
        build_ms = (time.perf_counter() - start) * 1000
        cursor.execute("ANALYZE bench_index")
        cursor.execute("SELECT count(*),sum(octet_length(plain)),sum(octet_length(mixed)),"
                       "sum(cardinality(string_to_array(plain,' '))) FROM bench_index")
        rows,plain_bytes,mixed_bytes,tokens = cursor.fetchone()
        cursor.execute("SELECT pg_relation_size('bench_index'),pg_total_relation_size('bench_index'),"
                       "pg_relation_size('bench_modes_bm25'),pg_total_relation_size('bench_modes_bm25')")
        heap,total,index,index_total = cursor.fetchone()
        cursor.execute("SELECT id,plain,mixed FROM bench_index ORDER BY id")
        fields = cursor.fetchall()
    return {"build_ms": build_ms, "rows": rows, "plain_field_bytes": plain_bytes,
            "mixed_field_bytes": mixed_bytes, "position_tokens": tokens,
            "table_relation_bytes": heap, "table_total_bytes": total,
            "index_relation_bytes": index, "index_total_bytes": index_total,
            "field_sha256": serialized_digest(fields)}


def check_performance_results(pgurl: str, name: str, queries: list[dict[str, Any]],
                              backend: str, mode: str, storage: str, top_k: int) -> dict[str, Any]:
    ids = []
    statement = search_statement(backend,storage)
    with connection(pgurl, name) as conn, conn.cursor() as cursor:
        configure(cursor)
        for query in queries:
            cursor.execute(statement,(query["inputs"][mode],mode,storage == "mixed",top_k))
            ids.append([row[0] for row in cursor.fetchall()])
        cursor.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + statement,
                       (queries[0]["inputs"][mode],mode,storage == "mixed",top_k))
        plan = cursor.fetchone()[0]
        if "ParadeDB" not in json.dumps(plan):
            raise RuntimeError(f"Real-query plan does not use the BM25 index: {backend}/{mode}/{storage}")
    require_nonempty_outputs(ids,f"search/{backend}/{mode}/{storage}")
    return {"returned_ids": ids, "result_sha256": serialized_digest(ids), "example_plan": plan}


def measure_search(pgurl: str, name: str, queries: list[dict[str, Any]], backend: str,
                   mode: str, storage: str, top_k: int, expected_ids: list[list[int]]) -> dict[str, Any]:
    statement = search_statement(backend,storage)
    latencies, ids = [], []
    with connection(pgurl, name) as conn, conn.cursor() as cursor:
        configure(cursor)
        for query in queries:
            cursor.execute(statement,(query["inputs"][mode],mode,storage == "mixed",top_k))
            cursor.fetchall()
        batch_start = time.perf_counter()
        for query in queries:
            start = time.perf_counter()
            cursor.execute(statement,(query["inputs"][mode],mode,storage == "mixed",top_k))
            returned = [row[0] for row in cursor.fetchall()]
            latencies.append((time.perf_counter() - start) * 1000)
            ids.append(returned)
        batch_ms = (time.perf_counter() - batch_start) * 1000
    if ids != expected_ids:
        raise RuntimeError(f"Timed result IDs differ from untimed verification: {backend}/{mode}/{storage}")
    return {"batch_ms": batch_ms, "per_query": timings(latencies), "result_sha256": serialized_digest(ids)}


def summarize(report: dict[str, Any]) -> None:
    for mode in MODES:
        for storage in STORAGES:
            keys = [method_key(backend,mode,storage) for backend in BACKENDS]
            expected = [report["performance_results"][key]["returned_ids"] for key in keys]
            if expected[0] != expected[1]:
                raise RuntimeError(f"Untimed ranked Rust/SQL parity failed: {mode}/{storage}")
            hashes = set()
            for key in keys:
                constructor = report["constructor_samples"][key]
                if len({(sample["output_bytes"],sample["output_sha256"]) for sample in constructor}) != 1:
                    raise RuntimeError(f"Actual constructor arrays drift across repeats: {key}")
                hashes.add(constructor[0]["output_sha256"])
                searches = report["search_samples"][key]
                expected_hash = report["performance_results"][key]["result_sha256"]
                if any(sample["result_sha256"] != expected_hash for sample in searches):
                    raise RuntimeError(f"Timed result checksum drift: {key}")
                report["constructor"][key] = {
                    "cold": timings([sample["cold_ms"] for sample in constructor]),
                    "warm": timings([sample["warm_ms"] for sample in constructor]),
                    "output_bytes": constructor[0]["output_bytes"], "output_sha256": constructor[0]["output_sha256"],
                    "autocomplete_reference": constructor[0]["autocomplete_reference"],
                }
                report["search_timing"][key] = {
                    "batch": timings([sample["batch_ms"] for sample in searches]),
                    "query_median_ms": statistics.median(sample["per_query"]["median_ms"] for sample in searches),
                    "query_p95_ms": statistics.median(sample["per_query"]["p95_ms"] for sample in searches),
                    "result_sha256": expected_hash,
                }
            if len(hashes) != 1:
                raise RuntimeError(f"Actual SQL/Rust pattern arrays differ: {mode}/{storage}")
    stable = ("rows","plain_field_bytes","mixed_field_bytes","position_tokens","field_sha256")
    for key in stable:
        if len({sample[key] for sample in report["index_samples"]}) != 1:
            raise RuntimeError(f"Index inputs differ across fresh builds: {key}")
    report["index"] = {key: report["index_samples"][0][key] for key in stable}
    for key in ("table_relation_bytes","table_total_bytes","index_relation_bytes","index_total_bytes"):
        report["index"][key] = int(statistics.median(sample[key] for sample in report["index_samples"]))
    report["index"]["build"] = timings([sample["build_ms"] for sample in report["index_samples"]])


def render_markdown(report: dict[str, Any]) -> str:
    config, environment = report["configuration"], report["environment"]
    lines = ["# P1 Pinyin Query Modes Benchmark", "",
             f"- Runtime HEAD: `{report['runtime_git_sha']}`; PostgreSQL: `{environment['postgresql']}`.",
             "- The JSON records working-tree source hashes separately; the HEAD above is not a claim that the tested implementation is a released tag.",
             f"- Extensions: `{json.dumps(environment['extensions'],sort_keys=True)}`.",
             f"- Fresh isolated database, {config['rows']:,} dictionary-derived phrases, {config['queries']:,} queries per method/run, {config['runs']} rotated-order repeats, top-k {config['top_k']}.",
             "- Corpus IDs and query selection are hash ordered. Curated gold is in a separate table, never forced into low-ID performance rows.",
             "- Cold constructor means a new PostgreSQL backend, not cold storage, OS caches or shared buffers. Output byte-length sums force evaluation; actual arrays are checked outside timing.",
             "- Every autocomplete constructor output is checked against explicit source syllable positions outside timing: completed syllables remain exact and the last shortened syllable stays one prefix position, for both plain and pipe-aware patterns.",
             "- Full-query timing includes SQL construction, BM25 matching/scoring, client round trips and top-k result transfer. It is not a parser-only comparison.",
             "- Compat and full use identical query strings. Initials and autocomplete use their own deterministic input forms, so timings across those semantic modes are not same-input parser comparisons.",
             "- All modes use the same field bytes, positional tokens and whitespace-tokenized index. No stopword filtering or index-reduction claim is made.",
             "- Performance `mixed` is pipe-wrapped single-reading storage, not a measured polyphonic/mixed-realistic workload. Genuine mixed plain/pipe tokens and polyphonic alternatives are covered by the separate adversarial gold.",
             f"- [Pinned pg_search tokenizer source]({TOKENIZER_SOURCE}); pipe alternatives remain one positional token.",
             *[f"- Run condition: {note}" for note in report.get("environment_notes", [])],
             "", "## Independent Adversarial Gold", "",
             "Hand-labeled positives AND negatives test an/ang, lv/lve, xi an/xian, earlier-token prefix leakage, initials and mixed pipe/plain storage. This is a targeted contract suite, not general pronunciation accuracy.",
             "Precision is calculated only over explicitly labeled returned positives/negatives; unlabeled returns are retained in JSON, not silently treated as correct.", "",
             f"| Method | Cases | Labeled precision | False positives | Recall@{config['top_k']} | MRR |", "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for key,gold in report["gold"].items():
        metric = gold["metrics"]
        if metric["queries"]:
            precision = metric["precision_on_labeled"]
            recall = metric[f"recall_at_{config['top_k']}"]
            lines.append(f"| {key} | {metric['queries']} | {precision:.4f} | {metric['false_positive_hits']} | {recall:.4f} | {metric['mrr']:.4f} |")
    lines += ["", "Compat and full overall totals have different case counts and must not be compared as an aggregate accuracy delta. The matched contrast below uses identical an, lv liang and zhan an inputs and the same independently labeled positives/negatives.",
              "False-positive counts/rates are visible within top-k only; a truncated result set cannot establish the absence of all false positives.", "",
              "| Matched group/mode | Cases | Labeled precision | Visible false positives | Visible labeled-negative rate |", "| --- | ---: | ---: | ---: | ---: |"]
    for group,contrast in report["matched_contrasts"].items():
        for mode,metric in contrast["metrics"].items():
            lines.append(f"| {group}/{mode} | {metric['queries']} | {metric['precision_on_labeled']:.4f} | {metric['false_positive_hits']} | {metric['false_positive_rate_on_labeled_negatives']:.4f} |")
    lines += ["", "## Constructor-Only", "", "Whole-corpus elapsed milliseconds, median of fresh-backend cold and warmed samples.", "",
              "| Method | Cold ms | Warm ms |", "| --- | ---: | ---: |"]
    for key,result in report["constructor"].items():
        lines.append(f"| {key} | {result['cold']['median_ms']:,.3f} | {result['warm']['median_ms']:,.3f} |")
    lines += ["", "## Full Warm Indexed Queries", "",
              "Synthetic dictionary-derived inputs measure performance and backend consistency, not independent retrieval accuracy. Returned IDs are verified before timing and again for every timed run; SQL/Rust parity is checked within each mode/storage only.", "",
              "| Method | Batch ms | Query median ms | Query p95 ms |", "| --- | ---: | ---: | ---: | ---: |"]
    for key,result in report["search_timing"].items():
        lines.append(f"| {key} | {result['batch']['median_ms']:,.3f} | {result['query_median_ms']:.3f} | {result['query_p95_ms']:.3f} |")
    lines += ["", "## Actual Index Footprint", "",
              "The table and index are recreated for each build. Sizes are median actual relation bytes, not dictionary size estimates.", "",
              "| Metric | Value |", "| --- | ---: |"]
    for key,value in report["index"].items():
        if isinstance(value,int):
            lines.append(f"| {key} | {value:,} |")
    lines += [f"| Index build median ms | {report['index']['build']['median_ms']:,.3f} |", "",
              "This is one shared dual-field index containing both plain and pipe-wrapped fields. Its bytes cannot be attributed to a single mode or compared as an index-compression improvement.", "",
              "Mode semantics intentionally differ; full and autocomplete are not required to reproduce compat result sets. Raw samples, ranked IDs, plans, source/corpus/fixture hashes and environment are retained in the JSON report.",
              "Timing variability and page allocation granularity do not establish statistical speedups. This report uses a different protocol from P0; do not compare its absolute times with the prior dictionary report.", ""]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pgurl",required=True,help="Administrative DSN able to create a temporary database")
    parser.add_argument("--data",type=Path,default=ROOT / "sql/data")
    parser.add_argument("--fixtures",type=Path,default=ROOT / "test/fixtures/pinyin_query_modes.json")
    parser.add_argument("--rows",type=int,default=20000)
    parser.add_argument("--query-limit",type=int,default=2000)
    parser.add_argument("--runs",type=int,default=3)
    parser.add_argument("--top-k",type=int,default=10)
    parser.add_argument("--pg-search-version", default="0.26.1")
    parser.add_argument("--environment-note", action="append", default=[],
                        help="Record external host/load conditions alongside measured settings")
    parser.add_argument("--output-prefix",type=Path,default=ROOT / "benchmarks/p1_query_modes")
    parser.add_argument("--keep-database",action="store_true")
    args = parser.parse_args()
    if args.runs < 3 or min(args.rows,args.query_limit,args.top_k) < 1:
        parser.error("rows/query-limit/top-k must be positive and --runs must be at least 3")
    fixture_bytes = args.fixtures.read_bytes()
    fixtures = json.loads(fixture_bytes)
    validate_fixtures(fixtures)
    word_bytes = (args.data / "pinyin_words.csv").read_bytes()
    corpus = make_corpus(read_words(word_bytes),args.rows)
    queries = make_queries(corpus,args.query_limit)
    report: dict[str,Any] = {
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "environment_notes": args.environment_note,
        "runtime_git_sha": subprocess.check_output(["git","-C",str(ROOT),"rev-parse","HEAD"],text=True).strip(),
        "benchmark_script_sha256": digest(Path(__file__).read_bytes()), "fixture_sha256": digest(fixture_bytes),
        "corpus_sha256": serialized_digest(corpus), "queries_sha256": serialized_digest(queries),
        "dictionary_sha256": {path.name: digest(path.read_bytes()) for path in sorted(args.data.glob("*.csv"))},
        "runtime_source_sha256": {str(path.relative_to(ROOT)): digest(path.read_bytes()) for path in
                                  [*sorted((ROOT / "src").glob("*.rs")),ROOT / "Cargo.lock",ROOT / "sql/word.sql",ROOT / "sql/pinyin.sql",
                                   ROOT / "docker/Dockerfile.test-pg19beta4-trixie",
                                   ROOT / "docker/patches/paradedb-0.26.1-pg19beta4.patch"]},
        "client_environment": {"python": platform.python_version(), "psycopg": psycopg.__version__,
                               "architecture": platform.machine(), "available_cpu_count": os.cpu_count()},
        "configuration": {"rows": args.rows,"queries": len(queries),"runs": args.runs,"top_k": args.top_k,
                          "modes": list(MODES),"backends": list(BACKENDS),"storages": list(STORAGES)},
        "gold": {}, "constructor": {}, "search_timing": {}, "index_samples": [],
        "constructor_samples": {method_key(*method): [] for method in rotated_methods(0)},
        "search_samples": {method_key(*method): [] for method in rotated_methods(0)},
        "performance_queries": queries, "performance_results": {},
    }
    with temporary_database(args.pgurl,args.keep_database) as database:
        report["database"] = database
        report["environment"] = setup(args.pgurl,database,corpus,fixtures,args.data)
        if report["environment"]["extensions"].get("pg_search") != args.pg_search_version:
            raise RuntimeError(f"Expected pg_search {args.pg_search_version}; actual extensions: "
                               f"{report['environment']['extensions']}")
        print("[gold] independent pattern arrays and adversarial ranked retrieval",flush=True)
        report["pattern_gold"] = check_patterns(args.pgurl,database,fixtures)
        report["gold"] = check_gold(args.pgurl,database,fixtures,args.top_k)
        report["matched_contrasts"] = matched_contrast_metrics(report["gold"],args.top_k)
        for round_index in range(args.runs):
            print(f"[constructors] round {round_index + 1}/{args.runs}",flush=True)
            for backend,mode,storage in rotated_methods(round_index):
                key = method_key(backend,mode,storage)
                result = measure_constructor(args.pgurl,database,backend,mode,storage)
                report["constructor_samples"][key].append(result)
                print(f"  {key:25s} cold {result['cold_ms']:10.3f} ms warm {result['warm_ms']:10.3f} ms",flush=True)
        for round_index in range(args.runs):
            result = build_index(args.pgurl,database)
            report["index_samples"].append(result)
            print(f"[index] fresh build {round_index + 1}: {result['build_ms']:.3f} ms, {result['index_relation_bytes']:,} bytes",flush=True)
        for backend,mode,storage in rotated_methods(0):
            key = method_key(backend,mode,storage)
            print(f"[untimed verification] {key}",flush=True)
            report["performance_results"][key] = check_performance_results(args.pgurl,database,queries,backend,mode,storage,args.top_k)
        for round_index in range(args.runs):
            print(f"[full queries] round {round_index + 1}/{args.runs}",flush=True)
            for backend,mode,storage in rotated_methods(round_index):
                key = method_key(backend,mode,storage)
                result = measure_search(args.pgurl,database,queries,backend,mode,storage,args.top_k,
                                        report["performance_results"][key]["returned_ids"])
                report["search_samples"][key].append(result)
                print(f"  {key:25s} batch {result['batch_ms']:10.3f} ms median/p95 {result['per_query']['median_ms']:.3f}/{result['per_query']['p95_ms']:.3f} ms",flush=True)
        summarize(report)
        args.output_prefix.parent.mkdir(parents=True,exist_ok=True)
        args.output_prefix.with_suffix(".json").write_text(json.dumps(report,ensure_ascii=False,indent=2) + "\n",encoding="utf-8")
        args.output_prefix.with_suffix(".md").write_text(render_markdown(report),encoding="utf-8")
        print(f"[reports] {args.output_prefix.with_suffix('.md')} and .json",flush=True)


if __name__ == "__main__":
    main()
