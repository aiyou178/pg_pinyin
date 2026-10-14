#!/usr/bin/env python3
"""Compare baseline/current standalone SQL parsers on fresh BM25 databases.

No pg_pinyin extension or Rust binary is used. Pattern parsing, serialized query
construction, and client-visible ranked retrieval are measured separately.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import gzip
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import platform
import re
import statistics
import subprocess
import time
from typing import Any, Iterable
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "sql_parser_corpus", ROOT / "scripts/benchmark_pinyin_query_modes.py"
)
COMMON = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(COMMON)
BASELINE = "cbfcf07c468e046a4068fb46460e0b64699b6827"
IMPLEMENTATIONS = ("baseline", "current")
MODES = ("compat", "full", "initials", "autocomplete", "auto")
STAGES = ("patterns", "constructor", "search")
SQL_FILES = ("sql/pinyin.sql", "sql/word.sql")
DATA_FILES = (
    ("pinyin_mapping.csv", "pinyin_mapping", "character,pinyin"),
    ("pinyin_token.csv", "pinyin_token", "character,category"),
    ("pinyin_words.csv", "pinyin_words", "word,pinyin"),
)
NAMESPACE = "pg_pinyin_sql_parser_"
AUTO_GOLD = (
    ("auto exact phrase", "xi ang", ["xi", "ang"]),
    ("auto ambiguous final vowel", "xi a", ["xi", "a.*"]),
    ("auto apostrophe", "xi'an", ["xi", "an.*"]),
    ("auto initials", "whh", ["w.*", "h.*", "h.*"]),
    ("auto leading nasal", "nh", ["n.*", "h.*"]),
    ("auto bare vowels", "a e o", ["a.*", "e.*", "o.*"]),
    ("auto complete numeric tone", "xi1 an1", ["xi", "an"]),
    ("auto tone and trailing prefix", "n\u01d0yon", ["ni", "yon.*"]),
    ("auto whole trailing prefix", "niyon", ["ni", "yon.*"]),
    ("auto umlaut", "L\u01db LI\u00c1NG", ["lv", "liang"]),
    ("auto malformed tone", "h\u01ce\u00f3", []),
    ("auto punctuation", "xi-an", []),
    ("auto empty", "", []),
    ("auto null", None, None),
)


def sha256(value: Any) -> str:
    if not isinstance(value, bytes):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(value).hexdigest()


def git(*arguments: str) -> str:
    return subprocess.check_output(["git", "-C", str(ROOT), *arguments], text=True).strip()


def acquire_sources(baseline: str) -> tuple[dict[str, dict[str, str]], dict[str, Any]]:
    if not re.fullmatch(r"[a-f0-9]{7,40}", baseline):
        raise ValueError("Baseline must be a hexadecimal commit ID, not a moving branch or option")
    commit = git("rev-parse", "--verify", f"{baseline}^{{commit}}")
    if not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("Git did not resolve a full baseline commit")
    old = {path: subprocess.check_output(
        ["git", "-C", str(ROOT), "show", f"{commit}:{path}"], text=True
    ) for path in SQL_FILES}
    current = {path: (ROOT / path).read_text(encoding="utf-8") for path in SQL_FILES}
    sources = {"baseline": old, "current": current}
    return sources, {
        "baseline_commit": commit, "current_head": git("rev-parse", "HEAD"),
        "current_worktree_dirty": bool(git("status", "--porcelain", "--untracked-files=normal")),
        "sql_sources": {
            implementation: {path: {"sha256": sha256(content.encode()), "source": content}
                             for path, content in bundle.items()}
            for implementation, bundle in sources.items()
        },
        "harness_sha256": sha256(Path(__file__).read_bytes()),
        "corpus_helper_sha256": sha256((ROOT / "scripts/benchmark_pinyin_query_modes.py").read_bytes()),
    }


def source_functions(sources: dict[str, str]) -> list[dict[str, str]]:
    functions = []
    for path, source in sources.items():
        for match in re.finditer(
            r"CREATE\s+OR\s+REPLACE\s+FUNCTION\s+([\w.]+)\s*\(.*?"
            r"\bAS\s+\$\$(.*?)\$\$\s*;", source, re.IGNORECASE | re.DOTALL
        ):
            functions.append({"name": match[1], "body": match[2], "path": path,
                              "statement": match[0]})
    if not functions:
        raise ValueError("Standalone sources contain no recognizable dollar-quoted functions")
    return functions


def installed_definitions(cursor, sources: dict[str, str]) -> dict[str, Any]:
    expected = source_functions(sources)
    definitions = {}
    for function in expected:
        schema, name = function["name"].split(".", 1)
        cursor.execute(
            "SELECT p.oid::regprocedure::text,p.prosrc,pg_get_functiondef(p.oid),"
            "l.lanname,p.provolatile,p.proparallel,p.proisstrict FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid=p.pronamespace JOIN pg_language l ON l.oid=p.prolang "
            "WHERE n.nspname=%s AND p.proname=%s AND p.prosrc=%s",
            (schema, name, function["body"]),
        )
        matches = cursor.fetchall()
        if not matches:
            raise RuntimeError(f"Installed SQL function body does not match source: {function['name']}")
        header = function["statement"].split("$$", 1)[0]
        expected_language = re.search(r"\bLANGUAGE\s+(\w+)", header, re.IGNORECASE)[1].lower()
        volatility_match = re.search(r"\b(IMMUTABLE|STABLE|VOLATILE)\b", header, re.IGNORECASE)
        expected_volatility = (volatility_match[1].lower()[0] if volatility_match else "v")
        parallel_match = re.search(r"\bPARALLEL\s+(SAFE|RESTRICTED|UNSAFE)\b", header, re.IGNORECASE)
        expected_parallel = (parallel_match[1].lower()[0] if parallel_match else "u")
        expected_strict = bool(re.search(r"\bSTRICT\b", header, re.IGNORECASE))
        for signature, body, definition, language, volatility, parallel, strict in matches:
            if language not in ("sql", "plpgsql"):
                raise RuntimeError("Standalone parser unexpectedly depends on a native-language function")
            if (language, volatility, parallel, strict) != (expected_language, expected_volatility,
                                                          expected_parallel, expected_strict):
                raise RuntimeError(f"Installed SQL function catalog flags do not match source: {signature}")
            definitions[signature] = {"source_path": function["path"], "body_sha256": sha256(body.encode()),
                                      "definition_sha256": sha256(definition.encode()), "definition": definition,
                                      "language": language, "volatility": volatility, "parallel": parallel,
                                      "strict": strict}
    if len(definitions) != len(expected):
        raise RuntimeError("Installed function count differs from standalone source function count")
    return definitions


def validate_configuration(rows: int, queries: int, repeats: int, top_k: int) -> None:
    if any(type(value) is not int for value in (rows, queries, repeats, top_k)):
        raise ValueError("Benchmark sizes must be integers")
    if min(rows, queries, repeats, top_k) < 1 or queries > rows:
        raise ValueError("Sizes must be positive and queries must not exceed rows")


def rotated_methods(round_index: int) -> list[tuple[str, str]]:
    methods = [(implementation, mode) for mode in MODES for implementation in IMPLEMENTATIONS]
    # Rotate whole old/new pairs so reversing cannot undo the order of a pair
    # split across the beginning and end of the sequence.
    shift = (round_index * 2) % len(methods)
    rotated = methods[shift:] + methods[:shift]
    return rotated if round_index % 2 == 0 else list(reversed(rotated))


def connection(dsn: str, database: str):
    return psycopg.connect(make_conninfo(dsn, dbname=database), autocommit=True, prepare_threshold=None)


def configure(cursor) -> None:
    cursor.execute("SET jit=off; SET max_parallel_workers_per_gather=0", prepare=False)


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
    print(f"[setup] created {database}", flush=True)
    try:
        yield database
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            drop_database(admin, database)
        print(f"[cleanup] removed {database}", flush=True)


def pattern_expression(value: str = "%s", mode: str = "%s") -> str:
    if value not in ("%s", "value") or mode not in ("%s", "mode", *MODES):
        raise ValueError("Unknown parser expression identifier")
    if mode == "compat":
        return f"public.sql_pinyin_regex_phrase_patterns({value},false)"
    if mode in MODES:
        mode = f"'{mode}'"
    return f"public.sql_pinyin_regex_phrase_patterns_mode({value},{mode},false)"


def query_expression(value: str = "%s", mode: str = "%s") -> str:
    if value not in ("%s", "value") or mode not in ("%s", "mode", *MODES):
        raise ValueError("Unknown query expression identifier")
    if mode == "compat":
        return f"public.sql_pinyin_regex_phrase({value},NULL,NULL,false)"
    if mode in MODES:
        mode = f"'{mode}'"
    return f"public.sql_pinyin_regex_phrase_mode({value},{mode},NULL,NULL,false)"


def search_statement(mode: str, table: str = "bench_index", all_hits: bool = False) -> str:
    if table not in ("bench_index", "bench_gold") or mode not in MODES:
        raise ValueError("Unknown benchmark relation")
    statement = (f"SELECT id,pdb.score(id)::double precision FROM {table} "
                 f"WHERE plain @@@ {query_expression(mode=mode)} ")
    return statement + ("ORDER BY id" if all_hits else "ORDER BY pdb.score(id) DESC,id LIMIT %s")


def aggregate_statement(stage: str, mode: str) -> str:
    if stage not in ("patterns", "constructor") or mode not in MODES:
        raise ValueError("Unknown aggregate stage")
    expression = (pattern_expression if stage == "patterns" else query_expression)("value", mode)
    return (
        "WITH consumed AS MATERIALIZED (SELECT id," + expression + "::text AS output FROM bench_inputs "
        "WHERE mode=%s) SELECT count(*),sum(octet_length(output)),"
        "encode(sha256(convert_to(string_agg(output,E'\\n' ORDER BY id),'UTF8')),'hex'),"
        "count(*) FILTER (WHERE output IS NULL) FROM consumed"
    )


def canonical_rows(rows: Iterable[Iterable[Any]]) -> list[list[Any]]:
    result, seen = [], set()
    for ident, score in rows:
        if type(ident) is not int or type(score) not in (int, float) or not math.isfinite(float(score)):
            raise ValueError("Ranked rows require integer IDs and finite scores")
        if ident in seen:
            raise ValueError("Ranked rows cannot contain duplicate IDs")
        seen.add(ident)
        result.append([ident, 0.0 if score == 0 else float(score)])
    return result


def assert_equal(left: Any, right: Any, label: str) -> None:
    if left != right:
        raise RuntimeError(f"Baseline/current parity failed: {label}")


def positional_oracle(documents: list[dict[str, Any]], patterns: list[str] | None) -> list[int]:
    """Independent zero-slop token-window oracle for plain exact/prefix patterns."""
    if not patterns:
        return []
    if any(not re.fullmatch(r"[a-z]+(?:\.\*)?", pattern) for pattern in patterns):
        raise ValueError("Plain token oracle accepts only exact or trailing-prefix patterns")
    compiled = [re.compile(pattern) for pattern in patterns]
    ids = []
    for document in documents:
        tokens = document["plain"].split()
        if any(all(pattern.fullmatch(token) for pattern, token in zip(compiled, tokens[start:]))
               for start in range(len(tokens) - len(compiled) + 1)):
            ids.append(document["id"])
    return sorted(ids)


def verify_top_k_plan(plan: list[dict[str, Any]]) -> None:
    def nodes(node):
        yield node
        for child in node.get("Plans", []):
            yield from nodes(child)
    found = [node for node in nodes(plan[0]["Plan"])
             if node.get("Node Type") == "Custom Scan" and
             "ParadeDB" in str(node.get("Custom Plan Provider", "")) and
             "TopK" in str(node.get("Exec Method", ""))]
    if not found:
        raise RuntimeError("Required BM25 score Top-K Custom Scan is missing")


def live_dictionary(cursor) -> dict[str, Any]:
    result = {}
    for filename, table, columns in DATA_FILES:
        names = columns.split(",")
        cursor.execute(
            sql.SQL("SELECT count(*),encode(sha256(convert_to(string_agg({}::text || ':' || {}::text,"
                    "E'\\n' ORDER BY {} COLLATE \"C\"),'UTF8')),'hex') FROM pinyin.{}").format(
                sql.Identifier(names[0]), sql.Identifier(names[1]), sql.Identifier(names[0]),
                sql.Identifier(table)),
        )
        count, checksum = cursor.fetchone()
        result[filename] = {"rows": count, "ordered_live_sha256": checksum}
    return result


def setup(dsn: str, database: str, sources: dict[str, str], corpus: list[dict[str, Any]],
          queries: list[dict[str, Any]], fixture: dict[str, Any], data: Path,
          pg_search_version: str, postgresql_pattern: str) -> dict[str, Any]:
    with connection(dsn, database) as conn, conn.cursor() as cursor:
        configure(cursor)
        cursor.execute("CREATE EXTENSION pg_search CASCADE")
        cursor.execute("SELECT version(),current_setting('server_version_num')")
        version, version_num = cursor.fetchone()
        if postgresql_pattern not in version:
            raise RuntimeError(f"Unexpected PostgreSQL version: {version}")
        cursor.execute("SELECT extname,extversion FROM pg_extension ORDER BY extname")
        extensions = dict(cursor.fetchall())
        if extensions.get("pg_search") != pg_search_version or "pg_pinyin" in extensions:
            raise RuntimeError("This benchmark requires exact pg_search version and no pg_pinyin extension")
        for path in SQL_FILES:
            cursor.execute(sources[path], prepare=False)
        cursor.execute("TRUNCATE pinyin.pinyin_mapping,pinyin.pinyin_token,pinyin.pinyin_words")
        for filename, table, columns in DATA_FILES:
            with cursor.copy(f"COPY pinyin.{table} ({columns}) FROM STDIN WITH (FORMAT CSV)") as copy:
                copy.write((data / filename).read_bytes())
        cursor.execute("INSERT INTO pinyin.pinyin_mapping VALUES (' ',' ') ON CONFLICT DO NOTHING")
        for _, table, _ in DATA_FILES:
            cursor.execute(sql.SQL("ANALYZE pinyin.{}").format(sql.Identifier(table)))
        definitions = installed_definitions(cursor, sources)
        dictionaries = live_dictionary(cursor)
        for table, rows in (("bench_index", corpus), ("bench_gold", fixture["retrieval_rows"])):
            cursor.execute(sql.SQL("CREATE TABLE {} (id bigint PRIMARY KEY,name text NOT NULL,plain text NOT NULL)")
                           .format(sql.Identifier(table)))
            with cursor.copy(sql.SQL("COPY {} FROM STDIN").format(sql.Identifier(table))) as copy:
                for row in rows:
                    copy.write_row((row["id"], row["name"], row["plain"]))
            cursor.execute(sql.SQL("CREATE INDEX {} ON {} USING bm25 (id,(plain::pdb.whitespace))")
                           .format(sql.Identifier(table + "_bm25"), sql.Identifier(table)))
            cursor.execute(sql.SQL("ANALYZE {}").format(sql.Identifier(table)))
        cursor.execute("CREATE TABLE bench_inputs (id bigint,mode text,value text,PRIMARY KEY(mode,id))")
        with cursor.copy("COPY bench_inputs FROM STDIN") as copy:
            for mode in MODES:
                for ident, query in enumerate(queries, 1):
                    copy.write_row((ident, mode, query["inputs"][mode]))
        cursor.execute("ANALYZE bench_inputs")
        cursor.execute("SELECT pg_relation_size('bench_index_bm25'),pg_total_relation_size('bench_index_bm25'),"
                       "pg_relation_size('bench_index'),pg_database_size(current_database())")
        index, index_total, heap, database_bytes = cursor.fetchone()
        cursor.execute("SELECT name,setting,unit FROM pg_settings WHERE name IN "
                       "('shared_buffers','work_mem','maintenance_work_mem','jit',"
                       "'max_parallel_workers_per_gather','fsync','synchronous_commit','full_page_writes') "
                       "ORDER BY name")
        settings = {name: {"value": setting, "unit": unit} for name, setting, unit in cursor.fetchall()}
    return {"postgresql": version, "server_version_num": version_num, "extensions": extensions,
            "installed_functions": definitions, "live_dictionary": dictionaries, "settings": settings,
            "index_bytes": index, "index_total_bytes": index_total, "heap_bytes": heap,
            "database_bytes": database_bytes, "rows": len(corpus)}


def make_queries(corpus: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    queries = COMMON.make_queries(corpus, count)
    for ident, query in enumerate(queries):
        # Auto exercises complete inputs, initials fallback, and trailing prefixes.
        query["inputs"]["auto"] = query["inputs"][("full", "initials", "autocomplete")[ident % 3]]
    return queries


def gold_cases(fixture: dict[str, Any]) -> list[dict[str, Any]]:
    return [*fixture["pattern_cases"], *(
        {"label": label, "input": value, "mode": "auto", "generated": False, "patterns": patterns}
        for label, value, patterns in AUTO_GOLD
    )]


def verify_parity(dsn: str, databases: dict[str, str], queries: list[dict[str, Any]],
                  corpus: list[dict[str, Any]], fixture: dict[str, Any], top_k: int) -> dict[str, Any]:
    verification = {"gold": [], "workloads": {}, "all_patterns_equal": True,
                    "all_full_hits_scores_and_top_k_equal": True}
    with ExitStack() as stack:
        cursors = {}
        for implementation in IMPLEMENTATIONS:
            conn = stack.enter_context(connection(dsn, databases[implementation]))
            cursors[implementation] = stack.enter_context(conn.cursor())
            configure(cursors[implementation])
        assert_equal(live_dictionary(cursors["baseline"]), live_dictionary(cursors["current"]),
                     "live dictionary contents")
        for case in gold_cases(fixture):
            patterns = []
            for implementation in IMPLEMENTATIONS:
                cursors[implementation].execute(
                    ("SELECT public.sql_pinyin_regex_phrase_patterns(%s,%s)" if case["mode"] == "compat"
                     else "SELECT public.sql_pinyin_regex_phrase_patterns_mode(%s,%s,%s)"),
                    ((case["input"], case["generated"]) if case["mode"] == "compat" else
                     (case["input"], case["mode"], case["generated"])),
                )
                returned = cursors[implementation].fetchone()[0]
                assert_equal(returned, case["patterns"], f"independent patterns/{implementation}/{case['label']}")
                patterns.append(returned)
            assert_equal(*patterns, "gold patterns")
            gold = {**case}
            if not case["generated"]:
                constructed = []
                for implementation in IMPLEMENTATIONS:
                    cursors[implementation].execute("SELECT " + query_expression(mode=case["mode"]) + "::text",
                                                    (case["input"],))
                    constructed.append(cursors[implementation].fetchone()[0])
                assert_equal(*constructed, f"gold serialized query/{case['label']}")
                if patterns[0] is None:
                    assert_equal(constructed[0], None, f"gold NULL constructor/{case['label']}")
                    gold.update(null_constructor_preserved=True, retrieval_skipped="NULL query is not evaluated with @@@")
                    verification["gold"].append(gold)
                    continue
                outputs = []
                for implementation in IMPLEMENTATIONS:
                    cursors[implementation].execute(search_statement(case["mode"], "bench_gold", True),
                                                    (case["input"],))
                    outputs.append(canonical_rows(cursors[implementation].fetchall()))
                assert_equal(*outputs, f"gold hits and scores/{case['label']}")
                expected = positional_oracle(fixture["retrieval_rows"], patterns[0])
                assert_equal([row[0] for row in outputs[0]], expected, f"positional oracle/{case['label']}")
                gold.update(hit_count=len(expected), hit_score_sha256=sha256(outputs[0]))
            verification["gold"].append(gold)
        for mode in MODES:
            checked = []
            for ident, query in enumerate(queries):
                value = query["inputs"][mode]
                patterns, hits, tops, constructed = [], [], [], []
                for implementation in IMPLEMENTATIONS:
                    cursor = cursors[implementation]
                    cursor.execute("SELECT " + pattern_expression(mode=mode) + "," + query_expression(mode=mode) + "::text",
                                   (value, value))
                    pattern, constructor = cursor.fetchone()
                    patterns.append(pattern)
                    constructed.append(constructor)
                    cursor.execute(search_statement(mode, all_hits=True), (value,))
                    hits.append(canonical_rows(cursor.fetchall()))
                    cursor.execute(search_statement(mode), (value, top_k))
                    tops.append(canonical_rows(cursor.fetchall()))
                assert_equal(*patterns, f"patterns/{mode}/{ident}")
                assert_equal(*constructed, f"serialized query/{mode}/{ident}")
                assert_equal(*hits, f"full hit IDs and scores/{mode}/{ident}")
                assert_equal(*tops, f"top-K IDs and scores/{mode}/{ident}")
                expected_top = sorted(hits[0], key=lambda row: (-row[1], row[0]))[:top_k]
                assert_equal(tops[0], expected_top, f"top-K matches complete ranked hitset/{mode}/{ident}")
                if query["source_id"] not in {row[0] for row in hits[0]}:
                    raise RuntimeError(f"Required dictionary source hit is missing: {mode}/{ident}")
                # Checking every synthetic query by scanning 20k Python documents would
                # dominate validation. Gold and 16 sampled windows use this oracle.
                if ident < 16:
                    expected = positional_oracle(corpus, patterns[0])
                    assert_equal([row[0] for row in hits[0]], expected, f"sampled positional oracle/{mode}/{ident}")
                checked.append({"source_id": query["source_id"], "input": value, "patterns": patterns[0],
                                "hit_count": len(hits[0]), "full_hit_score_sha256": sha256(hits[0]),
                                "top_k": tops[0], "constructor_sha256": sha256(constructed[0].encode())})
            verification["workloads"][mode] = {"queries": checked, "sha256": sha256(checked),
                                                "sampled_oracle_queries": min(16, len(queries))}
    return verification


def measure_aggregate(dsn: str, database: str, mode: str, stage: str, inputs: list[str]) -> dict[str, Any]:
    statement = aggregate_statement(stage, mode)
    expression = (pattern_expression if stage == "patterns" else query_expression)(mode=mode)
    with connection(dsn, database) as conn, conn.cursor() as cursor:
        configure(cursor)
        start = time.perf_counter()
        cursor.execute("SELECT " + expression + "::text", (inputs[0],))
        first_output = cursor.fetchone()[0]
        first_ms = (time.perf_counter() - start) * 1000
        cursor.execute(statement, (mode,))
        expected = list(cursor.fetchone())
        start = time.perf_counter()
        cursor.execute("EXPLAIN (ANALYZE, TIMING OFF, FORMAT JSON) " + statement, (mode,))
        plan = cursor.fetchone()[0]
        wall_ms = (time.perf_counter() - start) * 1000
        cursor.execute(statement, (mode,))
        actual = list(cursor.fetchone())
    assert_equal(expected, actual, f"stable consumed {stage}/{mode}")
    if expected[0] != len(inputs) or expected[3] or not expected[1] or not expected[2]:
        raise RuntimeError("Missing or unconsumed aggregate parser outputs")
    return {"first_fresh_backend_call_ms": first_ms, "first_output_sha256": sha256(first_output.encode()),
            "server_execution_ms": plan[0]["Execution Time"], "client_explain_ms": wall_ms,
            "output_rows": expected[0], "output_bytes": expected[1], "output_sha256": expected[2],
            "null_outputs": expected[3]}


def measure_search(dsn: str, database: str, mode: str, queries: list[dict[str, Any]], top_k: int,
                   expected: list[dict[str, Any]]) -> dict[str, Any]:
    statement = search_statement(mode)
    with connection(dsn, database) as conn, conn.cursor() as cursor:
        configure(cursor)
        start = time.perf_counter()
        cursor.execute(statement, (queries[0]["inputs"][mode], top_k))
        first = canonical_rows(cursor.fetchall())
        first_ms = (time.perf_counter() - start) * 1000
        assert_equal(first, expected[0]["top_k"], f"first top-K/{mode}")
        checksums, duration, latencies = [], 0.0, []
        for phase in ("warmup", "timed"):
            outputs, sample_times = [], []
            start = time.perf_counter()
            for ident, query in enumerate(queries):
                item_start = time.perf_counter()
                cursor.execute(statement, (query["inputs"][mode], top_k))
                result = canonical_rows(cursor.fetchall())
                sample_times.append((time.perf_counter() - item_start) * 1000)
                assert_equal(result, expected[ident]["top_k"], f"{phase} top-K/{mode}/{ident}")
                outputs.append(result)
            elapsed = (time.perf_counter() - start) * 1000
            checksums.append(sha256(outputs))
            if phase == "timed":
                duration, latencies = elapsed, sample_times
        assert_equal(*checksums, f"warm/timed ranked outputs/{mode}")
        cursor.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + statement,
                       (queries[0]["inputs"][mode], top_k))
        plan = cursor.fetchone()[0]
        verify_top_k_plan(plan)
    return {"first_fresh_backend_call_ms": first_ms, "elapsed_ms": duration,
            "per_query_ms": latencies, "query_count": len(queries), "top_k": top_k,
            "ranked_output_sha256": checksums[-1], "plan": plan,
            "median_query_ms": statistics.median(latencies), "p95_query_ms": COMMON.percentile(latencies, 0.95)}


def summarize(samples: list[dict[str, Any]], repeats: int) -> dict[str, Any]:
    result = {}
    for mode in MODES:
        result[mode] = {}
        for stage in STAGES:
            result[mode][stage] = {}
            key = "elapsed_ms" if stage == "search" else "server_execution_ms"
            for implementation in IMPLEMENTATIONS:
                cells = [cell for cell in samples if cell["implementation"] == implementation and cell["mode"] == mode]
                if len(cells) != repeats or sorted(cell["round"] for cell in cells) != list(range(repeats)):
                    raise ValueError("Summary requires one complete sample per round/implementation/mode")
                values = [cell[stage][key] for cell in cells]
                first = [cell[stage]["first_fresh_backend_call_ms"] for cell in cells]
                result[mode][stage][implementation] = {
                    "samples_ms": values, "median_ms": statistics.median(values),
                    "first_fresh_backend_calls_ms": first,
                }
            baseline = result[mode][stage]["baseline"]["median_ms"]
            current = result[mode][stage]["current"]["median_ms"]
            result[mode][stage].update(speedup=baseline / current if current else None,
                                      reduction_percent=100 * (baseline - current) / baseline if baseline else None)
    return result


def assert_report_paths_available(json_path: Path, markdown_path: Path) -> None:
    if json_path.resolve() == markdown_path.resolve():
        raise ValueError("Raw and Markdown report paths must differ")
    for path in (json_path, markdown_path):
        if path.exists():
            raise ValueError(f"Refusing to overwrite existing report: {path}")


def write_report(path: Path, content: bytes, compressed: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        if compressed:
            with gzip.GzipFile(fileobj=handle, mode="wb", mtime=0, filename="") as stream:
                stream.write(content)
        else:
            handle.write(content)


def render_markdown(report: dict[str, Any]) -> str:
    config, summary = report["configuration"], report["summary"]
    lines = ["# Standalone SQL Parser Benchmark", "", f"Measured {report['started_at']}.", "",
             f"Baseline `{report['provenance']['baseline_commit']}` vs the recorded current SQL snapshot; "
             "no pg_pinyin extension or Rust backend is installed.", "",
             f"{config['rows']:,} documents, {config['queries']:,} queries per mode, "
             f"{config['repeats']} rotated repeats. Fresh database pair per round; "
             "fresh backend per measured stage and complete workload warmup.", "",
             "| Mode | Pattern Server ms old/new | Constructor Server ms old/new | Client Search ms old/new | Search Speedup |",
             "| --- | ---: | ---: | ---: | ---: |"]
    for mode in MODES:
        cells = []
        for stage in STAGES:
            cell = summary[mode][stage]
            cells.append(f"{cell['baseline']['median_ms']:.3f} / {cell['current']['median_ms']:.3f}")
        lines.append(f"| {mode} | " + " | ".join(cells) + f" | {summary[mode]['search']['speedup']:.3f}x |")
    lines.extend(["", "## Interpretation", "",
                  "Pattern/constructor totals are server Execution Time for a MATERIALIZED aggregate "
                  "that consumes every output, including serialization and aggregate overhead. "
                  "Client search totals include construction, BM25 retrieval, roundtrip, result transfer, "
                  "and identical client-side parity checks. This is not isolated PL/pgSQL CPU time.", "",
                  "First fresh-backend calls are recorded separately in raw data; they are not cold OS "
                  "or cold shared-buffer measurements. Each stage warms its complete input workload. "
                  "Baseline and current are compared only within the same mode, not across incompatible matching semantics.", "",
                  "Automatic prepared statements are disabled. Client query time includes per-query planning; "
                  "prepared or generic plan performance is not benchmarked. Captured EXPLAIN plans therefore "
                  "use the same unprepared execution policy as the timed queries.", "",
                  "Every workload pattern and serialized query is exactly equal. Full hit IDs and exact "
                  "float scores, counts, top-K IDs, and top-K scores are compared in memory before timing; "
                  "cryptographic digests are retained instead of giant hit lists. Handwritten pattern gold "
                  "and sampled independent token-position oracles guard against equal-but-wrong outputs. "
                  "Real ParadeDB score Top-K Custom Scan plans are required.", "",
                  "No accuracy improvement or index reduction is claimed. Corpus and indexed values are "
                  "unchanged dictionary-derived short words, not representative business text. Index sizes "
                  "are immediate physical bytes of fresh builds, not steady-state compaction measurements. "
                  "Setup, dictionary loading, index building, and validation are outside query timings.", "",
                  "## Evidence", "", f"Raw uncompressed JSON SHA-256: `{report['raw_json_sha256']}`.", "",
                  "| Round | Implementation | BM25 bytes |", "| --- | --- | ---: |"])
    for setup_row in report["setups"]:
        lines.append(f"| {setup_row['round'] + 1} | {setup_row['implementation']} | {setup_row['index_bytes']:,} |")
    if report.get("environment_notes"):
        lines.extend(["", "## Environment Notes", "", *[f"- {note}" for note in report["environment_notes"]]])
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=os.environ.get("PGURL", "postgresql://postgres@localhost/postgres"))
    parser.add_argument("--baseline-ref", default=BASELINE)
    parser.add_argument("--rows", type=int, default=20000)
    parser.add_argument("--queries", type=int, default=2000)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "sql/data")
    parser.add_argument("--pg-search-version", default="0.26.1")
    parser.add_argument("--postgresql-version-pattern", default="19beta4")
    parser.add_argument("--json-output", type=Path, default=ROOT / "benchmarks/sql_parser.json.gz")
    parser.add_argument("--markdown-output", type=Path, default=ROOT / "benchmarks/sql_parser.md")
    parser.add_argument("--environment-note", action="append", default=[])
    args = parser.parse_args()
    validate_configuration(args.rows, args.queries, args.repeats, args.top_k)
    assert_report_paths_available(args.json_output, args.markdown_output)
    sources, provenance = acquire_sources(args.baseline_ref)
    fixture_path = ROOT / "test/fixtures/pinyin_query_modes.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    words = COMMON.read_words((args.data_dir / "pinyin_words.csv").read_bytes())
    corpus = COMMON.make_corpus(words, args.rows)
    queries = make_queries(corpus, args.queries)
    started = datetime.now(timezone.utc).isoformat()
    report = {"started_at": started, "configuration": {
        "rows": args.rows, "queries": args.queries, "repeats": args.repeats, "top_k": args.top_k,
        "modes": list(MODES), "pg_search_version": args.pg_search_version,
        "postgresql_version_pattern": args.postgresql_version_pattern,
        "database_isolation": "fresh baseline/current pair per round; fresh backend per stage/mode",
        "warmup": "complete workload before each timed stage; no OS-cold claim",
        "query_planning": "prepare_threshold=None; per-query planning included; no prepared/generic plan comparison",
    }, "provenance": provenance, "environment_notes": args.environment_note,
              "client": {"python": platform.python_version(), "machine": platform.machine(),
                         "psycopg": psycopg.__version__},
              "data": {filename: {"sha256": sha256((args.data_dir / filename).read_bytes())}
                       for filename, _, _ in DATA_FILES},
              "fixture_sha256": sha256(fixture_path.read_bytes()),
              "corpus_sha256": sha256(corpus), "query_inputs_sha256": sha256(queries),
              "setups": [], "verification": [], "samples": []}
    for round_index in range(args.repeats):
        with ExitStack() as stack:
            databases = {implementation: stack.enter_context(temporary_database(args.dsn))
                         for implementation in IMPLEMENTATIONS}
            setup_order = IMPLEMENTATIONS if round_index % 2 == 0 else IMPLEMENTATIONS[::-1]
            for implementation in setup_order:
                print(f"[round {round_index + 1}] install SQL-only {implementation}", flush=True)
                environment = setup(args.dsn, databases[implementation], sources[implementation], corpus,
                                    queries, fixture, args.data_dir, args.pg_search_version,
                                    args.postgresql_version_pattern)
                report["setups"].append({"round": round_index, "implementation": implementation, **environment})
            checked = verify_parity(args.dsn, databases, queries, corpus, fixture, args.top_k)
            report["verification"].append({"round": round_index, **checked})
            for implementation, mode in rotated_methods(round_index):
                print(f"[round {round_index + 1}] measure {implementation}/{mode}", flush=True)
                sample = {"round": round_index, "implementation": implementation, "mode": mode}
                values = [query["inputs"][mode] for query in queries]
                for stage in ("patterns", "constructor"):
                    sample[stage] = measure_aggregate(args.dsn, databases[implementation], mode, stage, values)
                sample["search"] = measure_search(args.dsn, databases[implementation], mode, queries, args.top_k,
                                                   checked["workloads"][mode]["queries"])
                report["samples"].append(sample)
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["summary"] = summarize(report["samples"], args.repeats)
    raw = (json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
    write_report(args.json_output, raw, args.json_output.suffix == ".gz")
    markdown = render_markdown({**report, "raw_json_sha256": sha256(raw)}).encode()
    write_report(args.markdown_output, markdown)
    print(f"[report] {args.markdown_output}; raw {args.json_output}; SHA-256 {sha256(raw)}", flush=True)


if __name__ == "__main__":
    main()
