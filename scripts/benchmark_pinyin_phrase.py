#!/usr/bin/env python3
"""Compare opt-in exact term/phrase queries with the unchanged full regex path.

Each invocation creates and removes its own database. This is a query-constructor
comparison, not a different parser or an index-compression benchmark.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import importlib.util
import json
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
COMMON_SPEC = importlib.util.spec_from_file_location(
    "phrase_benchmark_common", ROOT / "scripts/benchmark_pinyin_query_modes.py"
)
COMMON = importlib.util.module_from_spec(COMMON_SPEC)
COMMON_SPEC.loader.exec_module(COMMON)
METHODS = ("rust_regex", "rust_native", "sql_regex", "sql_native")
NAMESPACE = "pg_pinyin_native_"
BASELINE_COMMIT = "c33bb89"
GOLD_ROWS = [
    (101, "an"), (102, "ang"), (103, "chang an"), (104, "xi an"),
    (105, "xian"), (106, "xi ang"), (107, "xi hao an"), (108, "hao xi an"),
    (109, "xi an xi an"), (110, "lv liang"), (111, "lve liang"),
    (112, "xin gan"), (113, "xing an"), (114, "wu han han"),
    (115, "xi an hao"), (116, "hao hao xi an"), (117, "an an"),
]
GOLD_CASES = [
    {"label": "single exact term excludes ang", "input": "an", "slope": None,
     "positive_ids": [101, 103, 104, 107, 108, 109, 113, 115, 116, 117],
     "negative_ids": [102, 105, 106, 110, 111, 112, 114]},
    {"label": "apostrophe is a hard syllable boundary", "input": "xi'an", "slope": None,
     "positive_ids": [104, 108, 109, 115, 116],
     "negative_ids": [105, 106, 107]},
    {"label": "curly apostrophe preserves exact positions", "input": "xi\u2019an", "slope": 0,
     "positive_ids": [104, 108, 109, 115, 116],
     "negative_ids": [105, 106, 107]},
    {"label": "joined xian remains one syllable", "input": "xian", "slope": None,
     "positive_ids": [105], "negative_ids": [104, 106, 107, 108, 109, 115, 116]},
    {"label": "positive slop permits one intervening token", "input": "xi an", "slope": 1,
     "positive_ids": [104, 107, 108, 109, 115, 116], "negative_ids": [105, 106]},
    {"label": "umlaut spelling normalizes without prefix leakage", "input": "l\u00fc liang", "slope": None,
     "positive_ids": [110], "negative_ids": [111]},
    {"label": "explicit xin gan is not xing an", "input": "xin'gan", "slope": None,
     "positive_ids": [112], "negative_ids": [113]},
    {"label": "empty input yields empty search part", "input": "", "slope": None,
     "positive_ids": [], "negative_ids": [ident for ident, _ in GOLD_ROWS]},
    {"label": "separators only yield empty search part", "input": " ' \u2019 ", "slope": None,
     "positive_ids": [], "negative_ids": [ident for ident, _ in GOLD_ROWS]},
    {"label": "invalid character yields empty search part", "input": "xi-an", "slope": None,
     "positive_ids": [], "negative_ids": [ident for ident, _ in GOLD_ROWS]},
]


def validate_configuration(rows: int, queries: int, repeats: int, top_k: int) -> None:
    if min(rows, queries, top_k) < 1 or queries > rows or repeats < 3:
        raise ValueError("rows/queries/top-k must be positive, queries <= rows, and repeats >= 3")


def validate_gold() -> None:
    ids = {ident for ident, _ in GOLD_ROWS}
    if len(ids) != len(GOLD_ROWS):
        raise ValueError("Gold IDs must be unique")
    for case in GOLD_CASES:
        positive, negative = set(case["positive_ids"]), set(case["negative_ids"])
        if not negative or positive & negative or not (positive | negative) <= ids:
            raise ValueError("Independent gold labels must be disjoint and refer to fixture rows")


def rotated_methods(round_index: int) -> list[str]:
    shift = round_index % len(METHODS)
    methods = [*METHODS[shift:], *METHODS[:shift]]
    return methods if round_index % 2 == 0 else list(reversed(methods))


def query_expression(method: str, value: str = "%s", slope: str = "%s") -> str:
    if method not in METHODS or value not in ("%s", "plain") or slope not in ("%s", "NULL"):
        raise ValueError("Unknown query method or expression")
    prefix = "sql_" if method.startswith("sql_") else ""
    if method.endswith("native"):
        return f"public.{prefix}pinyin_phrase({value},{slope})"
    return f"public.{prefix}pinyin_regex_phrase_mode({value},'full',{slope},NULL,false)"


def search_statement(method: str, table: str = "bench_index", mixed_or: bool = False) -> str:
    if table not in ("bench_index", "bench_gold"):
        raise ValueError("Unknown benchmark relation")
    predicate = f"plain @@@ {query_expression(method)}"
    if mixed_or:
        predicate = f"({predicate} OR plain @@@ pdb.term('hao'))"
    return (f"SELECT id,pdb.score(id)::double precision AS score FROM {table} "
            f"WHERE {predicate} ORDER BY pdb.score(id) DESC,id LIMIT %s")


def verification_statement(method: str) -> str:
    # Aggregate every match, not just top-k, without transferring large hit lists.
    return (
        "WITH hits AS MATERIALIZED (SELECT id,pdb.score(id)::double precision AS score "
        f"FROM bench_index WHERE plain @@@ {query_expression(method)}) "
        "SELECT count(*),md5(coalesce(string_agg(id::text,',' ORDER BY id),'')),"
        "md5(coalesce(string_agg(id::text,',' ORDER BY score DESC,id),'')),"
        "md5(coalesce(string_agg(id::text || ':' || score::text,',' ORDER BY id),'')),"
        "coalesce(bool_or(id=%s),false),"
        "(SELECT coalesce(json_agg(r ORDER BY r.score DESC,r.id),'[]'::json) FROM "
        "(SELECT id,score FROM hits ORDER BY score DESC,id LIMIT %s) r) FROM hits"
    )


def connection(dsn: str, database: str):
    return psycopg.connect(make_conninfo(dsn, dbname=database), autocommit=True)


def new_database_name() -> str:
    return NAMESPACE + uuid.uuid4().hex[:16]


def drop_database(admin, database: str) -> None:
    if not re.fullmatch(NAMESPACE + r"[a-f0-9]{16}", database):
        raise ValueError("Refusing to drop a database outside this benchmark's namespace")
    admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))


@contextmanager
def temporary_database(dsn: str):
    database = new_database_name()
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(database)))
    print(f"[setup] fresh database {database}", flush=True)
    try:
        yield database
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            drop_database(admin, database)
        print(f"[cleanup] removed {database}", flush=True)


def runtime_provenance(cursor) -> dict[str, Any]:
    cursor.execute("SELECT version(),current_setting('server_version_num')")
    version, number = cursor.fetchone()
    cursor.execute("SELECT extname,extversion FROM pg_extension ORDER BY extname")
    extensions = dict(cursor.fetchall())
    cursor.execute("SELECT name,setting,unit FROM pg_settings WHERE name IN "
                   "('shared_buffers','work_mem','maintenance_work_mem','jit',"
                   "'max_parallel_workers_per_gather') ORDER BY name")
    settings = {name: {"value": value, "unit": unit} for name, value, unit in cursor.fetchall()}
    signatures = {
        "rust_regex": "public.pinyin_regex_phrase_mode(text,text,integer,integer,boolean)",
        "sql_regex": "public.sql_pinyin_regex_phrase_mode(text,text,integer,integer,boolean)",
        "rust_native": "public.pinyin_phrase(text,integer)",
        "sql_native": "public.sql_pinyin_phrase(text,integer)",
    }
    definitions = {}
    for method, signature in signatures.items():
        cursor.execute("SELECT pg_get_functiondef(%s::regprocedure)", (signature,))
        definitions[method] = COMMON.digest(cursor.fetchone()[0].encode())
    library = {}
    try:
        cursor.execute("SELECT setting FROM pg_config WHERE name='PKGLIBDIR'")
        libdir = cursor.fetchone()[0]
        cursor.execute("SELECT probin FROM pg_proc WHERE oid="
                       "'public.pinyin_regex_phrase_patterns_mode(text,text,boolean)'::regprocedure")
        probin = cursor.fetchone()[0]
        path = probin.replace("$libdir", libdir)
        if not path.endswith(".so"):
            path += ".so"
        cursor.execute("SELECT encode(sha256(pg_read_binary_file(%s)),'hex')", (path,))
        library = {"path": path, "sha256": cursor.fetchone()[0]}
    except psycopg.Error as error:
        library = {"sha256": None, "limitation": str(error).splitlines()[0]}
    return {"postgresql": version, "server_version_num": number, "extensions": extensions,
            "settings": settings, "installed_query_definition_sha256": definitions,
            "loaded_rust_library": library}


def setup(dsn: str, database: str, corpus: list[dict[str, Any]], data: Path) -> dict[str, Any]:
    with connection(dsn, database) as conn, conn.cursor() as cursor:
        COMMON.configure(cursor)
        cursor.execute("CREATE EXTENSION pg_search CASCADE")
        cursor.execute("CREATE EXTENSION pg_pinyin")
        for path in (ROOT / "sql/pinyin.sql", ROOT / "sql/word.sql"):
            cursor.execute(path.read_text(encoding="utf-8"), prepare=False)
        cursor.execute("TRUNCATE pinyin.pinyin_mapping,pinyin.pinyin_token,pinyin.pinyin_words")
        for filename, table, columns in (
            ("pinyin_mapping.csv", "pinyin_mapping", "character,pinyin"),
            ("pinyin_token.csv", "pinyin_token", "character,category"),
            ("pinyin_words.csv", "pinyin_words", "word,pinyin"),
        ):
            with cursor.copy(f"COPY pinyin.{table} ({columns}) FROM STDIN WITH (FORMAT CSV)") as copy:
                copy.write((data / filename).read_bytes())
        cursor.execute("UPDATE pinyin.pinyin_dictionary_meta SET version=version+1 WHERE singleton")
        for table in ("pinyin_mapping", "pinyin_token", "pinyin_words"):
            cursor.execute(sql.SQL("ANALYZE pinyin.{}").format(sql.Identifier(table)))
        cursor.execute("CREATE TABLE bench_corpus (id bigint PRIMARY KEY,name text NOT NULL,plain text NOT NULL)")
        with cursor.copy("COPY bench_corpus FROM STDIN") as copy:
            for row in corpus:
                copy.write_row((row["id"], row["name"], row["plain"]))
        cursor.execute("ANALYZE bench_corpus")
        cursor.execute("CREATE TABLE bench_gold (id bigint PRIMARY KEY,plain text NOT NULL)")
        with cursor.copy("COPY bench_gold FROM STDIN") as copy:
            for row in GOLD_ROWS:
                copy.write_row(row)
        cursor.execute("CREATE INDEX bench_gold_bm25 ON bench_gold USING bm25 "
                       "(id,(plain::pdb.whitespace)) WITH (key_field='id')")
        cursor.execute("ANALYZE bench_gold")
        return runtime_provenance(cursor)


def check_gold(dsn: str, database: str) -> dict[str, Any]:
    result = {}
    with connection(dsn, database) as conn, conn.cursor() as cursor:
        COMMON.configure(cursor)
        for method in METHODS:
            outputs = []
            for case in GOLD_CASES:
                cursor.execute(search_statement(method, "bench_gold"),
                               (case["input"], case["slope"], len(GOLD_ROWS)))
                rows = [[ident, score] for ident, score in cursor.fetchall()]
                ids = {ident for ident, _ in rows}
                if not set(case["positive_ids"]) <= ids or set(case["negative_ids"]) & ids:
                    raise RuntimeError(f"Independent full-match gold failed: {method}/{case['label']}: {rows}")
                outputs.append({**case, "returned": rows})
            cursor.execute(search_statement(method, "bench_gold", mixed_or=True),
                           ("an", None, len(GOLD_ROWS)))
            mixed_or = [[ident, score] for ident, score in cursor.fetchall()]
            result[method] = {"cases": outputs, "mixed_or_single_term": mixed_or,
                              "sha256": COMMON.serialized_digest([outputs, mixed_or])}
    for kind in ("regex", "native"):
        if result[f"rust_{kind}"] != result[f"sql_{kind}"]:
            raise RuntimeError(f"Independent Rust/SQL gold ranking or score parity failed: {kind}")
    return result


def constructor_statement(method: str) -> str:
    expression = query_expression(method, "plain", "NULL")
    return ("WITH constructed AS MATERIALIZED (SELECT id," + expression + "::text AS query "
            "FROM bench_corpus) SELECT count(*),sum(octet_length(query)),"
            "md5(string_agg(query,E'\\n' ORDER BY id)),"
            "count(*) FILTER (WHERE query IS NULL OR query='') FROM constructed")


def measure_constructor(dsn: str, database: str, method: str) -> dict[str, Any]:
    statement = constructor_statement(method)
    with connection(dsn, database) as conn, conn.cursor() as cursor:
        COMMON.configure(cursor)
        start = time.perf_counter()
        cursor.execute(statement)
        cold_output = list(cursor.fetchone())
        cold_ms = (time.perf_counter() - start) * 1000
        cursor.execute(statement)
        warmup_output = list(cursor.fetchone())
        start = time.perf_counter()
        cursor.execute(statement)
        warm_output = list(cursor.fetchone())
        warm_ms = (time.perf_counter() - start) * 1000
    if not cold_output[0] or cold_output[3] or not cold_output[1]:
        raise RuntimeError(f"Empty actual pdb.query output invalidates comparison: {method}")
    if cold_output != warmup_output or warmup_output != warm_output:
        raise RuntimeError(f"Actual pdb.query constructor output drift: {method}")
    return {"cold_ms": cold_ms, "warm_ms": warm_ms, "rows": warm_output[0],
            "output_bytes": warm_output[1], "output_md5": warm_output[2], "empty_outputs": warm_output[3]}


def build_index(dsn: str, database: str) -> dict[str, Any]:
    with connection(dsn, database) as conn, conn.cursor() as cursor:
        COMMON.configure(cursor)
        cursor.execute("DROP TABLE IF EXISTS bench_index")
        cursor.execute("CREATE TABLE bench_index (id bigint PRIMARY KEY,name text NOT NULL,plain text NOT NULL)")
        cursor.execute("INSERT INTO bench_index SELECT * FROM bench_corpus ORDER BY id")
        start = time.perf_counter()
        cursor.execute("CREATE INDEX bench_phrase_bm25 ON bench_index USING bm25 "
                       "(id,(plain::pdb.whitespace)) WITH (key_field='id')")
        build_ms = (time.perf_counter() - start) * 1000
        cursor.execute("ANALYZE bench_index")
        cursor.execute("SELECT count(*),sum(octet_length(plain)),"
                       "sum(cardinality(string_to_array(plain,' '))),"
                       "md5(string_agg(id::text || ':' || plain,E'\\n' ORDER BY id)) FROM bench_index")
        rows, field_bytes, tokens, field_md5 = cursor.fetchone()
        cursor.execute("SELECT pg_relation_size('bench_index'),pg_total_relation_size('bench_index'),"
                       "pg_relation_size('bench_phrase_bm25'),pg_total_relation_size('bench_phrase_bm25'),"
                       "pg_database_size(current_database())")
        heap, total, index, index_total, database_bytes = cursor.fetchone()
    return {"build_ms": build_ms, "rows": rows, "plain_field_bytes": field_bytes,
            "position_tokens": tokens, "field_md5": field_md5, "table_relation_bytes": heap,
            "table_total_bytes": total, "index_relation_bytes": index,
            "index_total_bytes": index_total, "database_bytes": database_bytes}


def verify_top_k_plan(plan: list[dict[str, Any]]) -> None:
    def nodes(node):
        yield node
        for child in node.get("Plans", []):
            yield from nodes(child)

    if not any(node.get("Exec Method") == "TopKScanExecState"
               and node.get("Index") == "bench_phrase_bm25"
               for node in nodes(plan[0]["Plan"])):
        raise RuntimeError("Indexed query must use BM25 score Top-K, not a normal scan plus sort")


def check_performance_results(dsn: str, database: str, queries: list[dict[str, Any]],
                              method: str, top_k: int) -> dict[str, Any]:
    outputs = []
    statement = verification_statement(method)
    with connection(dsn, database) as conn, conn.cursor() as cursor:
        COMMON.configure(cursor)
        for query in queries:
            cursor.execute(statement, (query["inputs"]["full"], None, query["source_id"], top_k))
            count, ids_md5, rank_md5, score_md5, source_hit, top = cursor.fetchone()
            if count < 1 or not source_hit:
                raise RuntimeError(f"Source phrase missing from full hitset: {method}/{query['source_id']}")
            outputs.append({"source_id": query["source_id"], "count": count, "ids_md5": ids_md5,
                            "rank_md5": rank_md5, "score_md5": score_md5,
                            "top": [[row["id"], row["score"]] for row in top]})
        cursor.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + search_statement(method),
                       (queries[0]["inputs"]["full"], None, top_k))
        plan = cursor.fetchone()[0]
        verify_top_k_plan(plan)
    return {"queries": outputs, "all_results_sha256": COMMON.serialized_digest(outputs),
            "top_results_sha256": COMMON.serialized_digest([row["top"] for row in outputs]),
            "example_plan": plan}


def compare_results(results: dict[str, Any]) -> dict[str, Any]:
    for kind in ("regex", "native"):
        left, right = (results[f"{backend}_{kind}"]["queries"] for backend in ("rust", "sql"))
        if left != right:
            raise RuntimeError(f"Untimed full-hitset/ranking/score Rust/SQL parity failed: {kind}")
    regex, native = results["rust_regex"]["queries"], results["rust_native"]["queries"]
    if not regex or any(row["count"] < 1 or not row["top"] for row in [*regex, *native]):
        raise RuntimeError("Empty performance hitsets invalidate the comparison")
    if len(regex) != len(native):
        raise RuntimeError("Regex/native comparison has different query counts")
    differences = {"ranked_queries": 0, "scored_queries": 0, "top_k_queries": 0, "examples": []}
    for old, new in zip(regex, native):
        if (old["source_id"], old["count"], old["ids_md5"]) != (
                new["source_id"], new["count"], new["ids_md5"]):
            raise RuntimeError(f"Regex/native full hitset differs: source id {old['source_id']}")
        ranking = old["rank_md5"] != new["rank_md5"]
        scoring = old["score_md5"] != new["score_md5"]
        top = [row[0] for row in old["top"]] != [row[0] for row in new["top"]]
        differences["ranked_queries"] += ranking
        differences["scored_queries"] += scoring
        differences["top_k_queries"] += top
        if (ranking or scoring) and len(differences["examples"]) < 5:
            differences["examples"].append({"source_id": old["source_id"],
                                             "regex_top": old["top"], "native_top": new["top"]})
    return {"verified_queries": len(regex), "all_hitsets_equal": True,
            "rust_sql_rank_and_score_equal_within_each_strategy": True, **differences}


def measure_search(dsn: str, database: str, queries: list[dict[str, Any]], method: str,
                   top_k: int, expected: list[dict[str, Any]]) -> dict[str, Any]:
    statement = search_statement(method)
    latencies, outputs = [], []
    with connection(dsn, database) as conn, conn.cursor() as cursor:
        COMMON.configure(cursor)
        for query in queries:
            cursor.execute(statement, (query["inputs"]["full"], None, top_k))
            cursor.fetchall()
        batch_start = time.perf_counter()
        for query in queries:
            start = time.perf_counter()
            cursor.execute(statement, (query["inputs"]["full"], None, top_k))
            rows = [[ident, score] for ident, score in cursor.fetchall()]
            latencies.append((time.perf_counter() - start) * 1000)
            outputs.append(rows)
        batch_ms = (time.perf_counter() - batch_start) * 1000
    if outputs != [row["top"] for row in expected]:
        raise RuntimeError(f"Timed ranked IDs/scores differ from untimed verification: {method}")
    return {"batch_ms": batch_ms, "per_query": COMMON.timings(latencies),
            "top_results_sha256": COMMON.serialized_digest(outputs)}


def summarize(report: dict[str, Any]) -> None:
    report["comparison"] = compare_results(report["performance_results"])
    report["constructor"], report["search_timing"] = {}, {}
    for method in METHODS:
        constructors = report["constructor_samples"][method]
        if len({(sample["rows"], sample["output_bytes"], sample["output_md5"])
                for sample in constructors}) != 1:
            raise RuntimeError(f"Actual query constructor drift across repeats: {method}")
        report["constructor"][method] = {
            "cold": COMMON.timings([sample["cold_ms"] for sample in constructors]),
            "warm": COMMON.timings([sample["warm_ms"] for sample in constructors]),
            "output_bytes": constructors[0]["output_bytes"], "output_md5": constructors[0]["output_md5"],
        }
        searches = report["search_samples"][method]
        expected_hash = report["performance_results"][method]["top_results_sha256"]
        if any(sample["top_results_sha256"] != expected_hash for sample in searches):
            raise RuntimeError(f"Timed ranked IDs/score checksum drift: {method}")
        report["search_timing"][method] = {
            "batch": COMMON.timings([sample["batch_ms"] for sample in searches]),
            "query_p50_ms": statistics.median(sample["per_query"]["median_ms"] for sample in searches),
            "query_p95_ms": statistics.median(sample["per_query"]["p95_ms"] for sample in searches),
            "top_results_sha256": expected_hash,
        }
    stable = ("rows", "plain_field_bytes", "position_tokens", "field_md5")
    for key in stable:
        if len({sample[key] for sample in report["index_samples"]}) != 1:
            raise RuntimeError(f"Fresh index inputs differ across builds: {key}")
    report["index"] = {key: report["index_samples"][0][key] for key in stable}
    for key in ("table_relation_bytes", "table_total_bytes", "index_relation_bytes",
                "index_total_bytes", "database_bytes"):
        report["index"][key] = int(statistics.median(sample[key] for sample in report["index_samples"]))
    report["index"]["build"] = COMMON.timings([sample["build_ms"] for sample in report["index_samples"]])


def render_markdown(report: dict[str, Any]) -> str:
    config, environment, comparison = report["configuration"], report["environment"], report["comparison"]
    lines = ["# Exact Pinyin Phrase Benchmark", "",
             f"- Runtime base HEAD: `{report['runtime_git_sha']}`; working tree dirty: `{report['runtime_git_worktree_dirty']}`; regex reference base: `{report['regex_reference_commit']}`. Exact candidate source and installed-library hashes are retained in JSON.",
             f"- PostgreSQL: `{environment['postgresql']}`; extensions: `{json.dumps(environment['extensions'], sort_keys=True)}`.",
             f"- Fresh UUID database; {config['rows']:,} deterministic dictionary phrases; {config['queries']:,} identical full queries; {config['repeats']} rotated repeats; top-k {config['top_k']}.",
             "- This compares query-constructor paths in the same installed binary/parser, not a parser rewrite or two separately compiled baselines. The existing full regex API is unchanged; native term/phrase is opt-in.",
             "- Actual `pdb.query` values are materialized and cast to text once per row for constructor timing. Cold means the first batch in a new PostgreSQL backend, not cold OS storage or shared buffers; one whole batch warms each backend before the warm sample.",
             "- Full-client timing includes query construction, BM25 matching/scoring, round trips and ranked top-k transfer. Each measured run has a complete untimed workload warm-up; setup, gold, full-hitset verification and EXPLAIN are outside timing.",
             "- Score ordering uses `ORDER BY pdb.score(id) DESC,id` without a cast in the sort expression. Every method's recorded plan must use `TopKScanExecState` on the shared BM25 index, not just any ParadeDB scan followed by a PostgreSQL sort.",
             "- Every performance query is checked against all matching IDs (not just top-k), plus ranking and scores. SQL/Rust parity is required within a strategy. Timed ranked IDs and scores must exactly match that method's preflight.",
             "- The corpus/query set is dictionary-derived and measures performance, not general pronunciation accuracy. Separate hand-written positive/negative gold covers exact an/ang, xi'an/xian, umlaut, boundaries, positive slop and empty/invalid input. A mixed-OR single-term gold query makes native BM25 score changes visible even if the sampled corpus contains only multi-syllable phrases.",
             "- All methods share one plain-field whitespace-tokenized index. No index-reduction claim is made; pipe/polyphonic fields are deliberately outside this opt-in path.",
             *[f"- Run condition: {note}" for note in report.get("environment_notes", [])],
             "", "## Semantic Comparison", "",
             f"Full hitsets agree for all {comparison['verified_queries']:,} sampled inputs. Rust/SQL ranking and scores agree within each strategy.",
             f"Regex versus native: {comparison['ranked_queries']:,} queries changed full ranking; {comparison['scored_queries']:,} changed scores; {comparison['top_k_queries']:,} changed top-k IDs.",
             "These counts describe the sampled multi-syllable workload, not universal score equivalence. Separate single-term and mixed-OR gold makes the constant-regex versus native-BM25 difference visible; the new helper is not a transparent ranking replacement.", "",
             "## Actual Query Construction", "",
             "Elapsed milliseconds for the entire corpus, median over fresh-backend samples.", "",
             "| Method | Cold ms | Warm ms | Materialized bytes |", "| --- | ---: | ---: | ---: |"]
    for method in METHODS:
        item = report["constructor"][method]
        lines.append(f"| {method} | {item['cold']['median_ms']:,.3f} | {item['warm']['median_ms']:,.3f} | {item['output_bytes']:,} |")
    lines += ["", "## Full Warm Indexed Queries", "",
              "| Method | Batch ms | Query p50 ms | Query p95 ms |", "| --- | ---: | ---: | ---: |"]
    for method in METHODS:
        item = report["search_timing"][method]
        lines.append(f"| {method} | {item['batch']['median_ms']:,.3f} | {item['query_p50_ms']:.3f} | {item['query_p95_ms']:.3f} |")
    lines += ["", "## Single Shared Index", "",
              "The same plain-field index is rebuilt for each size/build sample, not once per strategy. Database size includes the dictionaries and separate gold table as well as the performance index.",
              "", "| Metric | Value |", "| --- | ---: |"]
    for key, value in report["index"].items():
        if isinstance(value, int):
            lines.append(f"| {key} | {value:,} |")
    lines += [f"| Index build median ms | {report['index']['build']['median_ms']:,.3f} |", "",
              "JSON retains all run samples, compact full-hitset/ranking/score fingerprints for every query, selected ranked results, real BM25 plans and runtime/source/corpus hashes. Absolute times are not directly comparable to the previous multi-mode dual-field protocol. Three repeats expose sample variability but do not establish a statistical speedup.", ""]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", required=True, help="Administrative DSN able to create an isolated database")
    parser.add_argument("--data", type=Path, default=ROOT / "sql/data")
    parser.add_argument("--rows", type=int, default=20000)
    parser.add_argument("--queries", type=int, default=2000)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--pg-search-version", default="0.26.1")
    parser.add_argument("--environment-note", action="append", default=[])
    parser.add_argument("--json-output", type=Path, default=ROOT / "benchmarks/native_phrase.json")
    parser.add_argument("--markdown-output", type=Path, default=ROOT / "benchmarks/native_phrase.md")
    args = parser.parse_args()
    try:
        validate_configuration(args.rows, args.queries, args.repeats, args.top_k)
        validate_gold()
    except ValueError as error:
        parser.error(str(error))
    word_bytes = (args.data / "pinyin_words.csv").read_bytes()
    corpus = COMMON.make_corpus(COMMON.read_words(word_bytes), args.rows)
    queries = COMMON.make_queries(corpus, args.queries)
    report: dict[str, Any] = {
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "runtime_git_sha": subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip(),
        "runtime_git_worktree_dirty": bool(subprocess.check_output(
            ["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=no",
             "--ignore-submodules=all"], text=True).strip()),
        "regex_reference_commit": BASELINE_COMMIT,
        "benchmark_script_sha256": COMMON.digest(Path(__file__).read_bytes()),
        "shared_benchmark_utilities_sha256": COMMON.digest((ROOT / "scripts/benchmark_pinyin_query_modes.py").read_bytes()),
        "gold_sha256": COMMON.serialized_digest([GOLD_ROWS, GOLD_CASES]),
        "corpus_sha256": COMMON.serialized_digest(corpus), "queries_sha256": COMMON.serialized_digest(queries),
        "dictionary_sha256": {path.name: COMMON.digest(path.read_bytes()) for path in sorted(args.data.glob("*.csv"))},
        "runtime_source_sha256": {str(path.relative_to(ROOT)): COMMON.digest(path.read_bytes()) for path in
                                  [*sorted((ROOT / "src").glob("*.rs")), ROOT / "Cargo.lock", ROOT / "sql/word.sql",
                                   ROOT / "sql/pinyin.sql", ROOT / "pg_pinyin--0.0.8--0.0.9.sql",
                                   ROOT / "docker/Dockerfile.test-pg19beta4-trixie",
                                   ROOT / "docker/patches/paradedb-0.26.1-pg19beta4.patch"]},
        "client_environment": {"python": platform.python_version(), "psycopg": psycopg.__version__,
                               "architecture": platform.machine(), "available_cpu_count": os.cpu_count()},
        "configuration": {"rows": args.rows, "queries": len(queries), "repeats": args.repeats,
                          "top_k": args.top_k, "methods": list(METHODS), "storage": "plain"},
        "environment_notes": args.environment_note, "performance_queries": queries,
        "constructor_samples": {method: [] for method in METHODS},
        "search_samples": {method: [] for method in METHODS}, "index_samples": [],
        "performance_results": {},
    }
    with temporary_database(args.dsn) as database:
        report["database"] = database
        report["environment"] = setup(args.dsn, database, corpus, args.data)
        if report["environment"]["extensions"].get("pg_search") != args.pg_search_version:
            raise RuntimeError(f"Expected pg_search {args.pg_search_version}; got {report['environment']['extensions']}")
        print("[gold] independent exact-match, boundary, slop and mixed-OR cases", flush=True)
        report["gold"] = check_gold(args.dsn, database)
        for repeat in range(args.repeats):
            for method in rotated_methods(repeat):
                result = measure_constructor(args.dsn, database, method)
                report["constructor_samples"][method].append(result)
                print(f"[constructor {repeat + 1}/{args.repeats}] {method} cold={result['cold_ms']:.3f} warm={result['warm_ms']:.3f} ms", flush=True)
            result = build_index(args.dsn, database)
            report["index_samples"].append(result)
            print(f"[fresh index {repeat + 1}/{args.repeats}] {result['build_ms']:.3f} ms, {result['index_relation_bytes']:,} bytes", flush=True)
        for method in METHODS:
            print(f"[all-hitset preflight] {method}, {len(queries):,} queries", flush=True)
            report["performance_results"][method] = check_performance_results(args.dsn, database, queries, method, args.top_k)
        report["comparison"] = compare_results(report["performance_results"])
        for repeat in range(args.repeats):
            for method in rotated_methods(repeat):
                result = measure_search(args.dsn, database, queries, method, args.top_k,
                                        report["performance_results"][method]["queries"])
                report["search_samples"][method].append(result)
                print(f"[indexed {repeat + 1}/{args.repeats}] {method} batch={result['batch_ms']:.3f} ms p50/p95={result['per_query']['median_ms']:.3f}/{result['per_query']['p95_ms']:.3f} ms", flush=True)
        summarize(report)
        report["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
        for path in (args.json_output, args.markdown_output):
            path.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        args.markdown_output.write_text(render_markdown(report), encoding="utf-8")
        print(f"[reports] {args.markdown_output} and {args.json_output}", flush=True)


if __name__ == "__main__":
    main()
