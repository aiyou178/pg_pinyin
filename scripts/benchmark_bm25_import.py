#!/usr/bin/env python3
"""Measure raw-document import through commit and searchable BM25 readiness.

Fresh destinations/backends prevent cache and previous-table collisions. This
does not compare released versions: the tested extension is development 0.0.9.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import gzip
import importlib.util
import json
import math
import os
from pathlib import Path
import platform
import re
import statistics
import subprocess
import sys
import time
from typing import Any
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo


ROOT = Path(__file__).resolve().parents[1]
METHODS = ("insert_generated", "copy_generated", "copy_stage_scalar",
           "copy_stage_batch", "copy_precomputed")
INDEX_MODES = ("online", "after_load")
TRANSACTION_ROWS = (512, 4096, 20000)
CURRENT_VERSION = "0.0.9"
DATABASE_PATTERN = r"pg_pinyin_import_[a-f0-9]{16}"
TABLE = "bench_import"
INDEX = "bench_import_bm25"
INDEX_NAME = INDEX
STAGE = "bench_stage"
PHASES = ("upload_ms", "conversion_insert_ms", "transaction_commit_ms", "load_total_ms",
          "post_load_index_build_ms", "post_load_index_commit_ms", "analyze_ms",
          "analyze_commit_ms", "total_ready_ms")


def load_sibling(filename: str, name: str):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot import benchmark dependency: {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ARRAY = load_sibling("benchmark_array_batch.py", "bm25_import_array_helpers")
METRICS = load_sibling("bulk_import_metrics.py", "bm25_import_memory_helpers")
GOLD_DOCUMENTS = ARRAY.GOLD_DOCUMENTS
digest = ARRAY.digest
serialized_digest = ARRAY.serialized_digest
make_corpus = ARRAY.make_corpus


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_sizes(value: str | list[int] | tuple[int, ...]) -> tuple[int, ...]:
    try:
        values = tuple(int(part) for part in value.split(",")) if isinstance(value, str) else tuple(value)
    except (TypeError, ValueError) as error:
        raise ValueError("Transaction sizes must be comma-separated positive integers") from error
    if not values or len(set(values)) != len(values) or any(type(size) is not int or size < 1 for size in values):
        raise ValueError("Transaction sizes must be nonempty, unique positive integers")
    return values


def validate_output_paths(json_path: Path, markdown_path: Path) -> None:
    if json_path.resolve() == markdown_path.resolve():
        raise ValueError("JSON and Markdown outputs must resolve to different paths")
    if not (json_path.name.endswith(".json") or json_path.name.endswith(".json.gz")):
        raise ValueError("Raw report output must end in .json or .json.gz")
    for path in (json_path, markdown_path):
        if path.exists():
            raise ValueError(f"Refusing to overwrite report: {path}")


def validate_configuration(args: argparse.Namespace) -> tuple[int, ...]:
    for name in ("rows", "repeats", "search_probes"):
        if type(getattr(args, name)) is not int or getattr(args, name) < 1:
            raise ValueError(f"{name} must be positive")
    if type(args.batch_documents) is not int or not 1 <= args.batch_documents <= 8192:
        raise ValueError("batch_documents must be in 1..8192, independently of transaction size")
    if not math.isfinite(args.memory_interval_ms) or args.memory_interval_ms <= 0:
        raise ValueError("memory_interval_ms must be finite and positive")
    sizes = parse_sizes(args.transaction_rows)
    if not args.pg_search_version or not args.postgresql_version_pattern:
        raise ValueError("Expected pg_search and PostgreSQL versions must be explicit")
    validate_output_paths(args.json_output, args.markdown_output)
    return sizes


def cases(transaction_sizes: tuple[int, ...]) -> list[dict[str, Any]]:
    sizes = parse_sizes(transaction_sizes)
    return [{"method": method, "index_mode": index_mode, "transaction_rows": size}
            for size in sizes for index_mode in INDEX_MODES for method in METHODS]


def case_key(case: dict[str, Any]) -> str:
    if (case["method"] not in METHODS or case["index_mode"] not in INDEX_MODES
            or type(case["transaction_rows"]) is not int or case["transaction_rows"] < 1):
        raise ValueError("Unknown import case")
    return f"{case['index_mode']}/{case['transaction_rows']}/{case['method']}"


def rotated_cases(round_index: int, transaction_sizes: tuple[int, ...]) -> list[dict[str, Any]]:
    result = cases(transaction_sizes)
    offset = (round_index * 7) % len(result)
    result = result[offset:] + result[:offset]
    return result if round_index % 2 == 0 else list(reversed(result))


def transaction_chunks(rows: list[dict[str, Any]], transaction_rows: int):
    if type(transaction_rows) is not int or transaction_rows < 1:
        raise ValueError("transaction_rows must be a positive integer")
    for offset in range(0, len(rows), transaction_rows):
        yield rows[offset:offset + transaction_rows]


def connection(pgurl: str, database: str):
    return psycopg.connect(make_conninfo(pgurl, dbname=database), autocommit=True)


def new_database_name() -> str:
    return "pg_pinyin_import_" + uuid.uuid4().hex[:16]


def drop_database(admin, name: str) -> None:
    if not re.fullmatch(DATABASE_PATTERN, name):
        raise ValueError("Refusing to drop a database outside this benchmark namespace")
    admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@contextmanager
def temporary_database(pgurl: str):
    name = new_database_name()
    with psycopg.connect(pgurl, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
    print(f"[setup] fresh database {name}", flush=True)
    try:
        yield name
    finally:
        with psycopg.connect(pgurl, autocommit=True) as admin:
            drop_database(admin, name)
        print(f"[cleanup] removed {name}", flush=True)


def configure(cursor) -> None:
    cursor.execute("SET jit = off")
    cursor.execute("SET max_parallel_workers_per_gather = 0")


def effective_settings(cursor) -> dict[str, Any]:
    cursor.execute("SELECT name,setting,unit FROM pg_settings WHERE name=ANY(%s) ORDER BY name", ([
        "jit", "max_parallel_workers_per_gather", "shared_buffers", "work_mem", "maintenance_work_mem",
        "synchronous_commit", "fsync", "full_page_writes", "wal_level", "wal_compression",
        "max_wal_size", "checkpoint_timeout", "checkpoint_completion_target",
        "autovacuum", "shared_preload_libraries", "effective_cache_size",
    ],))
    return {name: {"value": value, "unit": unit} for name, value, unit in cursor.fetchall()}


def require_local_server(cursor, database: str) -> dict[str, Any]:
    cursor.execute("SELECT pg_backend_pid(),current_database()")
    backend_pid, actual_database = cursor.fetchone()
    if actual_database != database:
        raise RuntimeError("Performance artifact validation connected to an unexpected database")
    matched, reason = METRICS.backend_identity(backend_pid, database)
    if not matched:
        raise RuntimeError(f"Performance artifact validation requires a verified local Linux server: {reason}")
    return {"backend_pid": backend_pid, "database": database,
            "verified": True, "method": "local /proc title matches PostgreSQL and exact UUID database"}


def install_dictionary(pgurl: str, database: str, data: Path,
                       build_artifact: Path, rust_version: str) -> dict[str, Any]:
    with connection(pgurl, database) as conn, conn.cursor() as cursor:
        configure(cursor)
        locality = require_local_server(cursor, database)
        cursor.execute("CREATE EXTENSION pg_search CASCADE")
        cursor.execute("CREATE EXTENSION pg_pinyin")
        artifacts = ARRAY.current_artifacts(cursor, build_artifact, rust_version)
        cursor.execute("TRUNCATE pinyin.pinyin_mapping,pinyin.pinyin_token,pinyin.pinyin_words")
        for filename, table, columns in (
            ("pinyin_mapping.csv", "pinyin_mapping", ("character", "pinyin")),
            ("pinyin_token.csv", "pinyin_token", ("character", "category")),
            ("pinyin_words.csv", "pinyin_words", ("word", "pinyin")),
        ):
            statement = sql.SQL("COPY {}.{} ({}) FROM STDIN WITH (FORMAT CSV)").format(
                sql.Identifier("pinyin"), sql.Identifier(table),
                sql.SQL(",").join(map(sql.Identifier, columns)))
            with cursor.copy(statement) as copy:
                copy.write((data / filename).read_bytes())
        cursor.execute("INSERT INTO pinyin.pinyin_mapping VALUES (' ',' ') ON CONFLICT (character) DO NOTHING")
        cursor.execute("UPDATE pinyin.pinyin_dictionary_meta SET version=version+1 WHERE singleton")
        cursor.execute("ANALYZE pinyin.pinyin_mapping")
        cursor.execute("ANALYZE pinyin.pinyin_token")
        cursor.execute("ANALYZE pinyin.pinyin_words")
        cursor.execute("SELECT version(),current_setting('server_version_num')")
        postgresql, version_num = cursor.fetchone()
        cursor.execute("SELECT extname,extversion FROM pg_extension ORDER BY extname")
        extensions = dict(cursor.fetchall())
        cursor.execute("SELECT p.oid::regprocedure::text,pg_get_functiondef(p.oid) FROM pg_proc p "
                       "WHERE p.oid=ANY(%s::regprocedure[]) ORDER BY 1", ([
                           "public.pinyin_word_romanize(text)", "public.pinyin_word_romanize(anyelement)",
                           "public.pinyin_word_romanize_batch(text[])",
                       ],))
        definitions = {name: digest(body.encode()) for name, body in cursor.fetchall()}
        settings = effective_settings(cursor)
    return {"postgresql": postgresql, "server_version_num": version_num, "extensions": extensions,
            "settings": settings, "current_installed_artifacts": artifacts,
            "function_definition_sha256": definitions,
            "server_locality": locality,
            "release_source_provenance": "Installed DLL equals declared release artifact; caller must rebuild it from recorded source before running. Hash equality alone cannot prove source-to-binary linkage.",
            "dictionary_setup": "Loaded provided CSV into live tables and bumped dictionary version outside import timers"}


def require_versions(environment: dict[str, Any], pg_search_version: str,
                     postgresql_version_pattern: str) -> None:
    if environment["extensions"].get("pg_search") != pg_search_version:
        raise RuntimeError(f"Expected pg_search {pg_search_version}; got {environment['extensions']}")
    if environment["extensions"].get("pg_pinyin") != CURRENT_VERSION:
        raise RuntimeError(f"Expected pg_pinyin development {CURRENT_VERSION}")
    if postgresql_version_pattern.lower() not in environment["postgresql"].lower():
        raise RuntimeError(f"Expected PostgreSQL {postgresql_version_pattern}; got {environment['postgresql']}")


def create_table(cursor, method: str) -> None:
    if method not in METHODS:
        raise ValueError("Unknown import method")
    cursor.execute(sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(sql.Identifier(TABLE)))
    output = (sql.SQL("pinyin text GENERATED ALWAYS AS (public.pinyin_word_romanize(document)) STORED")
              if method in ("insert_generated", "copy_generated") else sql.SQL("pinyin text"))
    cursor.execute(sql.SQL("CREATE TABLE {} (id bigint PRIMARY KEY,document text,{})").format(
        sql.Identifier(TABLE), output))
    if method in ("copy_stage_scalar", "copy_stage_batch"):
        cursor.execute(sql.SQL("CREATE TEMP TABLE {} (id bigint PRIMARY KEY,document text) ON COMMIT DELETE ROWS").format(
            sql.Identifier(STAGE)))


def create_index(cursor) -> None:
    cursor.execute(sql.SQL("CREATE INDEX {} ON {} USING bm25 "
                           "(id,(document::pdb.icu),(pinyin::pdb.whitespace)) WITH (key_field='id')").format(
        sql.Identifier(INDEX), sql.Identifier(TABLE)))


def copy_rows(cursor, table: str, rows: list[dict[str, Any]], precomputed: bool = False) -> None:
    columns = ("id", "document", "pinyin") if precomputed else ("id", "document")
    statement = sql.SQL("COPY {} ({}) FROM STDIN").format(
        sql.Identifier(table), sql.SQL(",").join(map(sql.Identifier, columns)))
    with cursor.copy(statement) as copy:
        for row in rows:
            values = (row["id"], row["document"], row["expected"]) if precomputed else (row["id"], row["document"])
            copy.write_row(values)


def stage_insert_statement(method: str):
    if method == "copy_stage_scalar":
        return sql.SQL("INSERT INTO {} (id,document,pinyin) SELECT id,document,"
                       "public.pinyin_word_romanize(document) FROM {} ORDER BY id").format(
            sql.Identifier(TABLE), sql.Identifier(STAGE))
    if method != "copy_stage_batch":
        raise ValueError("Unknown stage conversion method")
    # Arrays are aggregated server-side inside the timed transaction. Each array
    # item is an independent document, never a token fragment from its neighbor.
    return sql.SQL("WITH numbered AS MATERIALIZED (SELECT id,document,"
                   "(row_number() OVER (ORDER BY id)-1)/%s AS batch FROM {}),"
                   "groups AS MATERIALIZED (SELECT batch,array_agg(id ORDER BY id) AS ids,"
                   "array_agg(document ORDER BY id) AS documents FROM numbered GROUP BY batch) "
                   "INSERT INTO {} (id,document,pinyin) "
                   "SELECT g.ids[o.ordinality::integer],g.documents[o.ordinality::integer],o.output "
                   "FROM groups g CROSS JOIN LATERAL unnest(public.pinyin_word_romanize_batch(g.documents)) "
                   "WITH ORDINALITY AS o(output,ordinality) ORDER BY g.batch,o.ordinality").format(
        sql.Identifier(STAGE), sql.Identifier(TABLE))


def import_rows(conn, corpus: list[dict[str, Any]], method: str,
                transaction_rows: int, batch_documents: int) -> dict[str, Any]:
    if method not in METHODS:
        raise ValueError("Unknown import method")
    if type(batch_documents) is not int or not 1 <= batch_documents <= 8192:
        raise ValueError("Independent document batch must be within 1..8192")
    result = {"upload_ms": 0.0, "conversion_insert_ms": 0.0, "transaction_commit_ms": 0.0,
              "load_total_ms": 0.0, "committed_transactions": 0, "uploaded_rows": 0}
    conn.autocommit = False
    started_load = time.perf_counter()
    try:
        with conn.cursor() as cursor:
            for chunk in transaction_chunks(corpus, transaction_rows):
                started = time.perf_counter()
                if method == "insert_generated":
                    cursor.executemany(sql.SQL("INSERT INTO {} (id,document) VALUES (%s,%s)").format(
                        sql.Identifier(TABLE)), ((row["id"], row["document"]) for row in chunk))
                else:
                    copy_rows(cursor, STAGE if method.startswith("copy_stage_") else TABLE,
                              chunk, method == "copy_precomputed")
                result["upload_ms"] += (time.perf_counter() - started) * 1000
                result["uploaded_rows"] += len(chunk)
                if method.startswith("copy_stage_"):
                    started = time.perf_counter()
                    cursor.execute(stage_insert_statement(method),
                                   (batch_documents,) if method == "copy_stage_batch" else None)
                    if cursor.rowcount != len(chunk):
                        raise RuntimeError("Stage conversion did not insert exactly one row per source document")
                    result["conversion_insert_ms"] += (time.perf_counter() - started) * 1000
                started = time.perf_counter()
                conn.commit()
                result["transaction_commit_ms"] += (time.perf_counter() - started) * 1000
                result["committed_transactions"] += 1
    except BaseException:
        conn.rollback()
        raise
    result["load_total_ms"] = (time.perf_counter() - started_load) * 1000
    return result


def expected_rows(corpus: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    rows = sorted(((row["id"], row["document"], row["expected"]) for row in corpus), key=lambda row: row[0])
    if not rows or len({row[0] for row in rows}) != len(rows):
        raise ValueError("Corpus must be nonempty and source IDs unique")
    return rows


def verify_rows(cursor, corpus: list[dict[str, Any]], label: str) -> dict[str, Any]:
    reference = expected_rows(corpus)
    cursor.execute(sql.SQL("SELECT id,document,pinyin FROM {} ORDER BY id").format(sql.Identifier(TABLE)))
    actual = cursor.fetchall()
    if actual != reference:
        index = next((index for index, values in enumerate(zip(actual, reference)) if values[0] != values[1]),
                     min(len(actual), len(reference)))
        raise RuntimeError(f"Exact source/document/romanization mismatch: {label}/row={index}")
    cursor.execute(sql.SQL("SELECT count(*),count(DISTINCT id),count(pinyin),"
                           "count(*) FILTER (WHERE pinyin='') FROM {}").format(sql.Identifier(TABLE)))
    count, unique_ids, nonnull, empty = cursor.fetchone()
    if count != len(reference) or unique_ids != count:
        raise RuntimeError("Postcommit reader did not observe every distinct source row")
    nonempty_expected = sum(bool(row[2]) for row in reference)
    if nonnull - empty != nonempty_expected:
        raise RuntimeError("Empty/NULL outputs differ from independently expected corpus")
    return {"verified_rows": count, "distinct_ids": unique_ids, "nonnull_outputs": nonnull,
            "empty_outputs": empty, "nonempty_expected": nonempty_expected,
            "id_document_output_sha256": serialized_digest(actual),
            "reader_visibility": "Exact rows checked from a different backend after all writer commits"}


def contains_window(tokens: list[str], window: list[str]) -> bool:
    return bool(window) and any(tokens[offset:offset + len(window)] == window
                                for offset in range(len(tokens) - len(window) + 1))


def search_probes(corpus: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    if count < 1:
        raise ValueError("Search probe count must be positive")
    tokenized = [(row["id"], (row["expected"] or "").split()) for row in corpus]
    eligible = [(ident, tokens) for ident, tokens in tokenized if tokens]
    if not eligible:
        raise ValueError("No nonempty romanization for BM25 visibility probes")
    selected = sorted(eligible, key=lambda row: (digest(str(row[0]).encode()), row[0]))[:count]
    probes = []
    for ident, tokens in selected:
        # Multi-token contiguous windows verify positions; not unordered term AND.
        window = tokens[:min(3, len(tokens))]
        matches = [other_id for other_id, candidate in tokenized if contains_window(candidate, window)]
        if ident not in matches:
            raise RuntimeError("Independent probe expectation omitted its own source")
        probes.append({"tokens": window, "source_id": ident, "expected_ids": sorted(matches)})
    return probes


def search_statement(probe: dict[str, Any], ranked: bool = False):
    constructor = "pdb.term(%s::text)" if len(probe["tokens"]) == 1 else "pdb.phrase_array(%s::text[])"
    predicate = sql.SQL("pinyin @@@ " + constructor)
    if ranked:
        return sql.SQL("SELECT id,pdb.score(id) FROM {} WHERE {} ORDER BY pdb.score(id) DESC,id LIMIT 10").format(
            sql.Identifier(TABLE), predicate)
    return sql.SQL("SELECT id FROM {} WHERE {} ORDER BY id").format(sql.Identifier(TABLE), predicate)


def has_custom_scan(value: Any) -> bool:
    if isinstance(value, dict):
        if value.get("Node Type") == "Custom Scan" or "Custom Plan Provider" in value:
            return True
        return any(has_custom_scan(item) for item in value.values())
    return isinstance(value, list) and any(has_custom_scan(item) for item in value)


def verify_search(cursor, corpus: list[dict[str, Any]], probes: list[dict[str, Any]]) -> dict[str, Any]:
    if not probes:
        raise ValueError("BM25 validation must contain nonempty expected queries")
    # Key+both tokenized text fields must be present, not a heap-only import.
    cursor.execute("SELECT pg_get_indexdef(indexrelid) FROM pg_index WHERE indexrelid=%s::regclass", (INDEX,))
    definition = cursor.fetchone()[0]
    if not all(marker in definition for marker in ("bm25", "document", "pinyin", "icu", "whitespace")):
        raise RuntimeError("BM25 index does not contain both required tokenized fields")
    actual_probes = []
    plan = None
    for index, probe in enumerate(probes):
        value = probe["tokens"][0] if len(probe["tokens"]) == 1 else probe["tokens"]
        params = (value,)
        cursor.execute(search_statement(probe), params)
        ids = [row[0] for row in cursor.fetchall()]
        if ids != probe["expected_ids"] or probe["source_id"] not in ids:
            raise RuntimeError(f"BM25 complete positional hitset/source mismatch: {probe['tokens']!r}")
        cursor.execute(search_statement(probe, ranked=True), params)
        ranked = cursor.fetchall()
        if not ranked or any(ident not in ids or score is None or not math.isfinite(score) for ident, score in ranked):
            raise RuntimeError("BM25 ranked Top-K must contain real expected hits and finite scores")
        if index == 0:
            cursor.execute(sql.SQL("EXPLAIN (ANALYZE, FORMAT JSON) ") + search_statement(probe, ranked=True), params)
            plan = cursor.fetchone()[0]
            if not has_custom_scan(plan):
                raise RuntimeError("Postcommit visibility query did not execute a BM25 Custom Scan")
        actual_probes.append({**probe, "actual_ids": ids, "ranked_top_k": [[ident, score] for ident, score in ranked]})
    return {"verified_probes": len(actual_probes), "verified_source_hits": len(actual_probes),
            "complete_hitsets_sha256": serialized_digest([probe["actual_ids"] for probe in actual_probes]),
            "probes": actual_probes, "executed_ranked_plan": plan, "index_definition": definition,
            "expectation": "Independent contiguous windows of source dictionary readings, never Top-K as recall proxy"}


def physical_sizes(cursor) -> dict[str, Any]:
    cursor.execute("SELECT pg_relation_size(%s::regclass),pg_total_relation_size(%s::regclass),"
                   "pg_relation_size(%s::regclass),pg_relation_size(%s::regclass)",
                   (TABLE, TABLE, TABLE + "_pkey", INDEX))
    heap, total, primary, bm25 = cursor.fetchone()
    cursor.execute("SELECT pg_database_size(current_database())")
    return {"heap_bytes": heap, "table_total_bytes": total, "primary_key_bytes": primary,
            "bm25_relation_bytes": bm25, "database_bytes": cursor.fetchone()[0],
            "note": "Physical catalog relation sizes after committed ANALYZE; database includes dictionaries"}


def measure_case(pgurl: str, database: str, corpus: list[dict[str, Any]],
                 case: dict[str, Any], batch_documents: int, probes: list[dict[str, Any]],
                 round_index: int, measure: bool = True, memory_interval_ms: float = 10.0) -> dict[str, Any]:
    key = case_key(case)
    expected_rows(corpus)
    # The memory helper returns explicitly unavailable evidence off local Linux.
    with connection(pgurl, database) as conn, conn.cursor() as cursor:
        configure(cursor)
        create_table(cursor, case["method"])
        if case["index_mode"] == "online":
            create_index(cursor)
        cursor.execute("SELECT pg_backend_pid(),pg_current_wal_lsn()::text")
        backend_pid, wal_start = cursor.fetchone()
        settings = effective_settings(cursor)
        started_at = utc_now()
        with METRICS.MemorySampler(backend_pid, database, interval_ms=memory_interval_ms) as sampler:
            started = time.perf_counter()
            timings = import_rows(conn, corpus, case["method"], case["transaction_rows"], batch_documents)
            for phase in PHASES:
                timings.setdefault(phase, 0.0)
            if case["index_mode"] == "after_load":
                phase_started = time.perf_counter()
                create_index(cursor)
                timings["post_load_index_build_ms"] = (time.perf_counter() - phase_started) * 1000
                phase_started = time.perf_counter()
                conn.commit()
                timings["post_load_index_commit_ms"] = (time.perf_counter() - phase_started) * 1000
            phase_started = time.perf_counter()
            cursor.execute(sql.SQL("ANALYZE {}").format(sql.Identifier(TABLE)))
            timings["analyze_ms"] = (time.perf_counter() - phase_started) * 1000
            phase_started = time.perf_counter()
            conn.commit()
            timings["analyze_commit_ms"] = (time.perf_counter() - phase_started) * 1000
            timings["total_ready_ms"] = (time.perf_counter() - started) * 1000
        memory = sampler.summary()
        completed_at = utc_now()
        conn.autocommit = True
        cursor.execute("SELECT pg_current_wal_lsn()::text,"
                       "pg_wal_lsn_diff(pg_current_wal_lsn(),%s::pg_lsn)", (wal_start,))
        wal_end, wal_bytes = cursor.fetchone()
        sizes = physical_sizes(cursor)
        # A new postcommit reader proves visibility independently of writer state.
        with connection(pgurl, database) as reader, reader.cursor() as read_cursor:
            configure(read_cursor)
            read_cursor.execute("SELECT pg_backend_pid()")
            reader_pid = read_cursor.fetchone()[0]
            if reader_pid == backend_pid:
                raise RuntimeError("Postcommit validation must use a different backend")
            verification = verify_rows(read_cursor, corpus, key)
            search = verify_search(read_cursor, corpus, probes)
        return {**case, "case_key": key, "round": round_index, "timed_sample": measure,
                "batch_documents": batch_documents, "backend_pid": backend_pid, "reader_pid": reader_pid,
                "started_at_utc": started_at, "completed_at_utc": completed_at, "rows": len(corpus),
                "timings": timings, "ready_rows_per_second": len(corpus) * 1000 / timings["total_ready_ms"],
                "memory": memory, "verification": verification, "search_verification": search,
                "physical_sizes": sizes, "effective_settings": settings,
                "wal": {"start_lsn": wal_start, "end_lsn": wal_end, "global_lsn_delta_bytes": int(wal_bytes),
                        "attribution": "Cluster-global LSN delta, not exclusive per-table/per-query WAL"},
                "classification": "precomputed lower-bound/control; excludes romanization" if case["method"] == "copy_precomputed"
                else "raw-document to committed/searchable BM25 conversion pipeline"}


def gold_corpus() -> list[dict[str, Any]]:
    return [{"id": index, "document": document, "expected": output, "label": label}
            for index, (label, document, output) in enumerate(GOLD_DOCUMENTS, 1)]


def check_gold(pgurl: str, database: str, batch_documents: int,
               memory_interval_ms: float = 10.0) -> dict[str, Any]:
    corpus = gold_corpus()
    probes = search_probes(corpus, len(corpus))
    results = []
    for index_mode in INDEX_MODES:
        for method in METHODS:
            # Multiple small transactions exercise stage truncation and NULLs.
            case = {"method": method, "index_mode": index_mode, "transaction_rows": 5}
            sample = measure_case(pgurl, database, corpus, case, min(batch_documents, 3), probes,
                                  round_index=-1, measure=False, memory_interval_ms=memory_interval_ms)
            results.append(sample)
    return {"documents": corpus, "id_document_output_sha256": serialized_digest(expected_rows(corpus)),
            "cases": results, "note": "All five methods and both indexed scenarios preserve all 12 handwritten cases; gold timings not performance samples"}


def timing_summary(values: list[float]) -> dict[str, Any]:
    return {"samples_ms": values, "median_ms": statistics.median(values),
            "min_ms": min(values), "max_ms": max(values)}


def summarize(report: dict[str, Any]) -> None:
    reference_hash = report["reference"]["id_document_output_sha256"]
    grouped: dict[str, list[dict[str, Any]]] = {case_key(case): [] for case in cases(tuple(report["configuration"]["transaction_rows"]))}
    for sample in report["samples"]:
        if (sample["verification"]["verified_rows"] != report["configuration"]["rows"]
                or sample["verification"]["id_document_output_sha256"] != reference_hash
                or sample["search_verification"]["verified_source_hits"] != report["configuration"]["actual_search_probes"]):
            raise RuntimeError("Cannot summarize failed full-output/search visibility verification")
        grouped[sample["case_key"]].append(sample)
    result = {}
    for key, samples in grouped.items():
        repeats = report["configuration"]["repeats"]
        if (len(samples) != repeats
                or {sample["round"] for sample in samples} != set(range(repeats))):
            raise RuntimeError(f"Every case must have exactly the requested number of rounds: {key}")
        phases = {phase: timing_summary([sample["timings"][phase] for sample in samples]) for phase in PHASES}
        result[key] = {"method": samples[0]["method"], "index_mode": samples[0]["index_mode"],
                       "transaction_rows": samples[0]["transaction_rows"], "phases": phases,
                       "ready_rows_per_second_at_median": report["configuration"]["rows"] * 1000 / phases["total_ready_ms"]["median_ms"],
                       "physical_sizes_samples": [sample["physical_sizes"] for sample in samples],
                       "wal_global_lsn_delta_bytes_samples": [sample["wal"]["global_lsn_delta_bytes"] for sample in samples],
                       "memory_samples": [sample["memory"] for sample in samples],
                       "classification": samples[0]["classification"]}
    report["summary"] = result


def memory_display(sample: dict[str, Any], component: str) -> str:
    value = sample.get("memory", {}).get(component, {})
    if not value.get("available"):
        return "n/a"
    # Raw evidence remains lossless; accept the helper's explicit RSS naming.
    peak = value.get("observed_peak_rss_bytes")
    if peak is None:
        peak = value.get("observed_peak_bytes")
    return f"{peak / (1024 * 1024):.1f}" if peak is not None else "n/a"


def render_markdown(report: dict[str, Any]) -> str:
    config = report["configuration"]
    environment = report["environment"]
    lines = ["# Committed BM25 Import Benchmark", "",
             f"- Run: `{report['started_at_utc']}` to `{report['completed_at_utc']}`.",
             f"- Source: `{report['git']['commit']}`; working tree dirty: `{report['git']['dirty']}`. No release/version claim.",
             f"- PostgreSQL: `{environment['postgresql']}`; pg_search `{environment['extensions']['pg_search']}`; pg_pinyin development `{CURRENT_VERSION}`.",
             f"- {config['rows']:,} raw documents, {config['repeats']} rotated rounds per case; transaction rows {config['transaction_rows']}; independent-document batch {config['batch_documents']:,}.",
             f"- Release installed DLL SHA256: `{environment['current_installed_artifacts']['library_sha256']}`; matched declared release build artifact.",
             *[f"- Run condition: {note}" for note in report.get("environment_notes", [])], "",
             "## Measurement Boundary", "",
             "Total-ready includes client row/COPY serialization, raw-document upload, server romanization and array aggregation, heap/primary-key writes, BM25 writes or deferred build, all transaction commits, and ANALYZE+commit. A new backend starts each case; its first-use dictionary initialization is included. Repeated samples are fresh-backend runs, not warm-call microbenchmarks.",
             "Source selection, dictionary installation, destination/temp-table DDL and empty online-index creation are outside timing. No raw source corpus or document batch arrays are preloaded into SQL tables. In `after_load`, BM25 build+commit is inside readiness; in `online`, the already-empty index is maintained during every commit.",
             "`insert_generated` executes one SQL INSERT per document through psycopg executemany (batched/pipelined protocol), not one network roundtrip per document. Generated-column methods include server conversion in upload duration; stage methods record COPY upload and conversion+INSERT separately.",
             "`copy_precomputed` uploads independently expected romanization. It excludes conversion and is only a lower-bound/control, not an equivalent end-to-end replacement or an automatic recommendation.", "",
             "The performance CLI verifies a co-located Linux PostgreSQL backend before fingerprinting installed files. Installed DLL equality against the declared release artifact rejects a mismatched installation, not two identically stale files; rebuild that artifact from the recorded source before each run.", "",
             "## Ready-Time Results", "",
             "Medians in milliseconds; load includes import commits. Per-phase raw samples are in JSON.", "",
             "| Index scenario | Tx rows | Method | Load ms | Build+commit ms | ANALYZE+commit ms | Total ready ms | Ready rows/s |",
             "|---|---:|---|---:|---:|---:|---:|---:|"]
    for case in cases(tuple(config["transaction_rows"])):
        summary = report["summary"][case_key(case)]
        phases = summary["phases"]
        build = statistics.median([sample["timings"]["post_load_index_build_ms"] + sample["timings"]["post_load_index_commit_ms"]
                                   for sample in report["samples"] if sample["case_key"] == case_key(case)])
        analyze = statistics.median([sample["timings"]["analyze_ms"] + sample["timings"]["analyze_commit_ms"]
                                     for sample in report["samples"] if sample["case_key"] == case_key(case)])
        lines.append(f"| {case['index_mode']} | {case['transaction_rows']:,} | {case['method']} | "
                     f"{phases['load_total_ms']['median_ms']:.2f} | {build:.2f} | {analyze:.2f} | "
                     f"{phases['total_ready_ms']['median_ms']:.2f} | {summary['ready_rows_per_second_at_median']:,.0f} |")
    lines += ["", "## Quality and Visibility", "",
              f"- All {len(report['gold']['documents'])} handwritten gold documents preserved by all five methods and both index scenarios, including NULL, empty input, umlauts, traditional forms, polyphony and independent 银/行 boundaries.",
              f"- Every timed sample checked all {config['rows']:,} exact `(id, document, pinyin)` rows from another postcommit backend; reference SHA256 `{report['reference']['id_document_output_sha256']}`.",
              f"- Every sample checked {config['actual_search_probes']} nonempty positional pinyin queries against complete independently expected ID sets, including source hit and real ranked Top-K. An executed JSON plan proves BM25 Custom Scan use. Search validation is outside import timing.", "",
              "## Physical and Memory Evidence", "",
              "MiB below are maximum observed samples over rounds, not allocator-instrumented query peaks. Sampling can miss short spikes. A fresh backend's lifetime VmHWM is separately labeled in JSON; container cgroup observations are shared-process evidence, not exclusive query memory.", "",
              "| Index scenario | Tx rows | Method | BM25 MiB min..max | Backend sampled RSS MiB | Client sampled RSS MiB | Cgroup observed MiB | Global WAL MiB min..max |",
              "|---|---:|---|---:|---:|---:|---:|---:|"]
    for case in cases(tuple(config["transaction_rows"])):
        matching = [sample for sample in report["samples"] if sample["case_key"] == case_key(case)]
        bm25 = [sample["physical_sizes"]["bm25_relation_bytes"] / 1048576 for sample in matching]
        wal = [sample["wal"]["global_lsn_delta_bytes"] / 1048576 for sample in matching]
        backend_values = [memory_display(sample, "postgresql_backend") for sample in matching]
        client_values = [memory_display(sample, "client") for sample in matching]
        backend = max((float(value) for value in backend_values if value != "n/a"), default=None)
        client = max((float(value) for value in client_values if value != "n/a"), default=None)
        cgroup = max((sample["memory"].get("container_cgroup", {}).get("observed_peak_memory_bytes", 0)
                      for sample in matching if sample["memory"].get("container_cgroup", {}).get("available")),
                     default=None)
        lines.append(f"| {case['index_mode']} | {case['transaction_rows']:,} | {case['method']} | "
                     f"{min(bm25):.2f}..{max(bm25):.2f} | "
                     f"{f'{backend:.1f}' if backend is not None else 'n/a'} | "
                     f"{f'{client:.1f}' if client is not None else 'n/a'} | "
                     f"{f'{cgroup / 1048576:.1f}' if cgroup is not None else 'n/a'} | "
                     f"{min(wal):.2f}..{max(wal):.2f} |")
    lines += ["", "## Limits", "",
              "- LSN deltas are cluster-global and may include other databases/background activity; they are not attributed exclusive per-table WAL.",
              "- The cgroup contains PostgreSQL, client and any other container processes. Observed RSS includes first-use dictionary caches and index-building state; no true per-query memory peak is claimed.",
              "- This deterministic dictionary-derived corpus measures a specific workload, not production phrase accuracy, sustained ingestion, concurrent-write capacity or a universally optimal batch size.",
              "- Transaction size changes commit count and segment behavior. Independent-document batch size is a separate conversion bound; it does not change phrase boundaries.",
              "- Median phase durations need not sum to median readiness. Range/individual round samples, effective durability settings, physical sizes, complete hit sets and artifacts are retained in JSON.", ""]
    return "\n".join(lines)


def write_report(report: dict[str, Any], json_path: Path, markdown_path: Path) -> None:
    validate_output_paths(json_path, markdown_path)
    raw = (json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
    markdown = render_markdown(report).encode()
    for path in (json_path, markdown_path):
        path.parent.mkdir(parents=True, exist_ok=True)
    if json_path.name.endswith(".gz"):
        raw = gzip.compress(raw, mtime=0)
    # Exclusive creation also prevents a race between initial checks and output.
    with json_path.open("xb") as stream:
        stream.write(raw)
    with markdown_path.open("xb") as stream:
        stream.write(markdown)


def git_metadata() -> dict[str, Any]:
    def command(*parts):
        return subprocess.check_output(["git", "-C", str(ROOT), *parts], text=True).strip()
    try:
        status = command("status", "--porcelain")
        return {"commit": command("rev-parse", "HEAD"), "branch": command("branch", "--show-current"),
                "dirty": bool(status), "status_porcelain": status}
    except (OSError, subprocess.CalledProcessError) as error:
        raise RuntimeError("Git source identity must be accessible for this benchmark") from error


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pgurl", "--dsn", dest="pgurl", default=os.environ.get("PGURL", "postgresql://postgres@localhost/postgres"))
    parser.add_argument("--rows", type=int, default=20000)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--transaction-rows", default="512,4096,20000")
    parser.add_argument("--batch-documents", type=int, default=512)
    parser.add_argument("--search-probes", type=int, default=64)
    parser.add_argument("--memory-interval-ms", type=float, default=10.0)
    parser.add_argument("--pg-search-version", default="0.26.1")
    parser.add_argument("--postgresql-version-pattern", default="19beta4")
    parser.add_argument("--data", type=Path, default=ROOT / "sql/data")
    parser.add_argument("--current-build-artifact", type=Path,
                        default=Path(os.environ.get("CARGO_TARGET_DIR", str(ROOT / "target"))) / "release/libpg_pinyin.so")
    parser.add_argument("--json-output", type=Path, default=ROOT / "benchmarks/bm25_import.json.gz")
    parser.add_argument("--markdown-output", type=Path, default=ROOT / "benchmarks/bm25_import.md")
    parser.add_argument("--environment-note", action="append", default=[])
    return parser


def run(args: argparse.Namespace) -> dict[str, Any]:
    sizes = validate_configuration(args)
    words = ARRAY.read_csv((args.data / "pinyin_words.csv").read_bytes())
    characters = ARRAY.read_csv((args.data / "pinyin_mapping.csv").read_bytes())
    corpus = make_corpus(words, characters, args.rows)
    probes = search_probes(corpus, args.search_probes)
    reference = expected_rows(corpus)
    rust_version = subprocess.check_output(["rustc", "--version"], text=True).strip()
    source_files = [*sorted((ROOT / "src").glob("**/*.rs")), ROOT / "Cargo.toml", ROOT / "Cargo.lock",
                    ROOT / "pg_pinyin.control", Path(__file__), Path(__file__).with_name("benchmark_array_batch.py"),
                    Path(__file__).with_name("bulk_import_metrics.py")]
    report = {"schema": "pg_pinyin.bm25_import.v1", "started_at_utc": utc_now(),
              "environment_notes": args.environment_note, "git": git_metadata(),
              "configuration": {"rows": args.rows, "repeats": args.repeats,
                                "transaction_rows": list(sizes), "batch_documents": args.batch_documents,
                                "search_probes": args.search_probes, "actual_search_probes": len(probes),
                                "memory_interval_ms": args.memory_interval_ms,
                                "methods": list(METHODS), "index_modes": list(INDEX_MODES),
                                "pg_search_version": args.pg_search_version,
                                "postgresql_version_pattern": args.postgresql_version_pattern,
                                "extension_version": CURRENT_VERSION, "build_profile": "release"},
              "client_environment": {"python": platform.python_version(), "psycopg": psycopg.__version__,
                                     "os": platform.platform(), "logical_cpus": os.cpu_count(),
                                     "rust_version": rust_version},
              "runtime_source_sha256": {str(path.relative_to(ROOT)): digest(path.read_bytes()) for path in source_files},
              "dictionary_sha256": {path.name: digest(path.read_bytes()) for path in sorted(args.data.glob("*.csv"))},
              "corpus_sha256": serialized_digest(corpus), "gold_sha256": serialized_digest(GOLD_DOCUMENTS),
              "reference": {"verified_rows": len(reference), "id_document_output_sha256": serialized_digest(reference)},
              "samples": [], "round_case_order": []}
    with temporary_database(args.pgurl) as database:
        report["database"] = database
        report["environment"] = install_dictionary(args.pgurl, database, args.data,
                                                    args.current_build_artifact, rust_version)
        require_versions(report["environment"], args.pg_search_version, args.postgresql_version_pattern)
        print("[gold] validating all methods/index modes before timed performance samples", flush=True)
        report["gold"] = check_gold(args.pgurl, database, args.batch_documents, args.memory_interval_ms)
        for round_index in range(args.repeats):
            order = rotated_cases(round_index, sizes)
            report["round_case_order"].append([case_key(case) for case in order])
            for case in order:
                sample = measure_case(args.pgurl, database, corpus, case, args.batch_documents, probes,
                                      round_index, memory_interval_ms=args.memory_interval_ms)
                report["samples"].append(sample)
                print(f"[round {round_index + 1}/{args.repeats}] {case_key(case)} "
                      f"ready={sample['timings']['total_ready_ms']:.2f} ms "
                      f"verified={sample['verification']['verified_rows']} probes={sample['search_verification']['verified_probes']}", flush=True)
        summarize(report)
    report["completed_at_utc"] = utc_now()
    write_report(report, args.json_output, args.markdown_output)
    print(f"[report] {args.markdown_output}; raw {args.json_output}", flush=True)
    return report


def main() -> None:
    args = argument_parser().parse_args()
    try:
        run(args)
    except (ValueError, RuntimeError) as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
