#!/usr/bin/env python3
"""Fresh-database comparison: consumed conversion, COPY and real Top-K.

Variant manifests identify separately rebuilt libraries installed on the local
server. Alternatively, --server compares stock extension installs on disposable
servers. Report overwrite is refused.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import statistics
import time
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("upgrade_query_helpers", ROOT / "scripts/benchmark_pinyin_query_modes.py")
COMMON = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(COMMON)
PREFIX = "pinyin_pgrx_bench_"


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def connect(dsn, database):
    conn = psycopg.connect(make_conninfo(dsn, dbname=database), autocommit=True, prepare_threshold=None)
    conn.execute("SET jit=off; SET max_parallel_workers_per_gather=0", prepare=False)
    return conn


def consumed(expression, source="bench_docs"):
    return ("WITH outputs AS MATERIALIZED (SELECT id," + expression + " AS output FROM " + source + ") "
            "SELECT count(*),sum(octet_length(output)),"
            "encode(sha256(convert_to(string_agg(output,E'\\n' ORDER BY id),'UTF8')),'hex') FROM outputs")


def nodes(plan):
    yield plan
    for child in plan.get("Plans", []):
        yield from nodes(child)


def setup(dsn, database, variant, corpus, *, search=True):
    with connect(dsn, database) as conn:
        server_version = conn.execute("SHOW server_version").fetchone()[0].split()[0]
        if "server_version" in variant and server_version != variant["server_version"]:
            raise RuntimeError(f"Expected PostgreSQL {variant['server_version']}; got {server_version}")
        if search:
            conn.execute("CREATE EXTENSION pg_search CASCADE")
        extensions = dict(conn.execute("SELECT extname,extversion FROM pg_extension"))
        if "pg_search" in variant and extensions.get("pg_search") != variant["pg_search"]:
            raise RuntimeError(f"Unexpected pg_search version: {extensions.get('pg_search')}")
        conn.execute("CREATE EXTENSION pg_pinyin")
        library = variant.get("library", "pg_pinyin")
        if "library" in variant:
            functions = conn.execute(
                "SELECT p.oid::regprocedure::text,pg_get_functiondef(p.oid) FROM pg_proc p "
                "JOIN pg_language l ON l.oid=p.prolang WHERE l.lanname='c' AND p.probin='$libdir/pg_pinyin'"
            ).fetchall()
            if not functions:
                raise RuntimeError("Missing extension C bindings")
            for signature, definition in functions:
                conn.execute(definition.replace("'$libdir/pg_pinyin'", "'$libdir/" + library + "'"))
                if "pinyin_regex_phrase_patterns" in signature:
                    conn.execute("ALTER FUNCTION " + signature + " " + variant["pattern_volatility"])
        installed_path = variant.get("installed_path")
        if installed_path is None:
            pkglibdir = conn.execute("SELECT setting FROM pg_config WHERE name='PKGLIBDIR'").fetchone()[0]
            installed_path = f"{pkglibdir}/{library}.so"
        installed = conn.execute(
            "SELECT encode(sha256(pg_read_binary_file(%s)),'hex')", (installed_path,)
        ).fetchone()[0]
        if "library_sha256" in variant and installed != variant["library_sha256"]:
            raise RuntimeError("Loaded library does not match rebuilt artifact manifest")
        conn.execute("CREATE TABLE bench_docs(id bigint PRIMARY KEY,name text,plain text,tokens text[])")
        with conn.cursor().copy("COPY bench_docs(id,name,plain,tokens) FROM STDIN") as copy:
            for row in corpus:
                copy.write_row((row["id"], row["name"], row["plain"], list(row["name"])))
        if search:
            conn.execute("CREATE INDEX bench_bm25 ON bench_docs USING bm25(id,(plain::pdb.whitespace))")
        conn.execute("ANALYZE bench_docs")
        gold = conn.execute(
            "SELECT public.pinyin_word_romanize(value) FROM unnest(%s::text[]) WITH ORDINALITY AS g(value,n) ORDER BY n",
            (["吕梁", "重庆", "西安", "ABC", "", None],),
        ).fetchall()
        if gold != [(x,) for x in ["lv liang", "chong qing", "xi an", "abc", "", None]]:
            raise RuntimeError(f"Independent pronunciation gold failed: {gold}")
        if conn.execute("SELECT public.pinyin_regex_phrase_patterns('whh')").fetchone()[0] != ["w.*", "h.*", "h.*"]:
            raise RuntimeError("Initials gold failed")
        return {"server": conn.execute("SELECT version()").fetchone()[0],
                "pg_search": extensions.get("pg_search"),
                "pg_pinyin": conn.execute("SELECT extversion FROM pg_extension WHERE extname='pg_pinyin'").fetchone()[0],
                "library_sha256": installed,
                "settings": dict(conn.execute("SELECT name,setting FROM pg_settings WHERE name IN "
                    "('jit','max_parallel_workers_per_gather','fsync','synchronous_commit','full_page_writes',"
                    "'shared_buffers','work_mem')").fetchall()),
                "bm25_bytes": conn.execute("SELECT pg_relation_size('bench_bm25')").fetchone()[0] if search else None,
                "bindings": conn.execute("SELECT proname,probin,provolatile FROM pg_proc WHERE probin=%s ORDER BY proname,oid",
                                         ("$libdir/" + library,)).fetchall()}


def aggregate_run(dsn, database, statement, expected=None):
    with connect(dsn, database) as conn:
        start = time.perf_counter()
        first = conn.execute(statement).fetchone()
        first_ms = (time.perf_counter() - start) * 1000
        conn.execute(statement).fetchone()  # Complete workload warmup.
        start = time.perf_counter()
        result = conn.execute(statement).fetchone()
        elapsed = (time.perf_counter() - start) * 1000
        if result != first or (expected is not None and result != expected):
            raise RuntimeError("Consumed output checksum differs")
        plan = conn.execute("EXPLAIN (ANALYZE,FORMAT JSON) " + statement).fetchone()[0]
        return {"first_backend_aggregate_ms": first_ms, "warm_ms": elapsed, "result": result, "plan": plan}


def search_run(dsn, database, queries, expected):
    statement = ("SELECT id,pdb.score(id)::double precision FROM bench_docs "
                 "WHERE plain @@@ public.pinyin_regex_phrase(%s) ORDER BY pdb.score(id) DESC,id LIMIT 10")
    with connect(dsn, database) as conn:
        start = time.perf_counter()
        conn.execute(statement, (queries[0],)).fetchall()
        first_ms = (time.perf_counter() - start) * 1000
        full_hits = []
        for query in queries:
            hits = conn.execute(statement.replace(" LIMIT 10", ""), (query,)).fetchall()
            full_hits.append(hits)
        if expected is not None and full_hits != expected:
            raise RuntimeError("Complete scored-hit or ranked-ID parity failed")
        for query in queries:
            conn.execute(statement, (query,)).fetchall()
        outputs, times = [], []
        start = time.perf_counter()
        for query in queries:
            one = time.perf_counter()
            outputs.append(conn.execute(statement, (query,)).fetchall())
            times.append((time.perf_counter() - one) * 1000)
        elapsed = (time.perf_counter() - start) * 1000
        if outputs != [hits[:10] for hits in full_hits]:
            raise RuntimeError("Timed Top-K differs from complete scored hits")
        plan = conn.execute("EXPLAIN (ANALYZE,FORMAT JSON) " + statement, (queries[0],)).fetchone()[0]
        if not any(node.get("Node Type") == "Custom Scan" and
                   "ParadeDB" in str(node.get("Custom Plan Provider", "")) and
                   "TopK" in str(node.get("Exec Method", ""))
                   for node in nodes(plan[0]["Plan"])):
            raise RuntimeError("Missing real ParadeDB Top-K plan")
        return {"first_backend_query_ms": first_ms, "warm_ms": elapsed, "per_query_ms": times,
                "p95_ms": COMMON.percentile(times, .95), "output_sha256": digest(outputs),
                "full_scored_hits": full_hits, "plan": plan}


def copy_run(dsn, database, corpus, *, search=True):
    table = sql.Identifier("bench_copy" if search else "bench_core_copy")
    with connect(dsn, database) as conn:
        conn.execute(sql.SQL("CREATE TABLE {}(id bigint PRIMARY KEY,name text,"
                             "plain text GENERATED ALWAYS AS (public.pinyin_word_romanize(name)) STORED)").format(table))
        if search:
            conn.execute("CREATE INDEX bench_copy_bm25 ON bench_copy USING bm25(id,(plain::pdb.whitespace))")
        start = time.perf_counter()
        with conn.transaction():
            with conn.cursor().copy(sql.SQL("COPY {}(id,name) FROM STDIN").format(table)) as copy:
                for row in corpus:
                    copy.write_row((row["id"], row["name"]))
        elapsed = (time.perf_counter() - start) * 1000
        actual = conn.execute(sql.SQL("SELECT id,name,plain FROM {} ORDER BY id").format(table)).fetchall()
        if actual != [(r["id"], r["name"], r["plain"]) for r in corpus]:
            raise RuntimeError("COPY stored values differ from independent source dictionary")
        return {"warm_ms": elapsed, "output_sha256": digest(actual),
                "bm25_bytes": conn.execute("SELECT pg_relation_size('bench_copy_bm25')").fetchone()[0] if search else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--manifest", type=Path)
    target.add_argument("--server", action="append", nargs=4, metavar=("LABEL", "DSN", "VERSION", "PG_SEARCH"),
                        help="Repeat per disposable server; use '-' for core-only without pg_search.")
    parser.add_argument("--rows", type=int, default=20000)
    parser.add_argument("--queries", type=int, default=2000)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--output", type=Path, default=ROOT / "benchmarks/pgrx_0200")
    args = parser.parse_args()
    if not 1 <= args.queries <= args.rows or args.rounds < 3:
        raise ValueError("Require positive queries <= rows and at least three rounds")
    raw_path, md_path = args.output.with_suffix(".json.gz"), args.output.with_suffix(".md")
    if raw_path.exists() or md_path.exists():
        raise ValueError("Refusing report overwrite")
    if args.manifest:
        if not args.dsn:
            parser.error("--manifest requires --dsn")
        variants = json.loads(args.manifest.read_text())
        dsns = dict.fromkeys((v["label"] for v in variants), args.dsn)
        for variant in variants:
            if not re.fullmatch(r"pg_pinyin_[a-z0-9_]+", variant["library"]) or variant["pattern_volatility"] not in ("STABLE", "IMMUTABLE"):
                raise ValueError("Invalid variant library or volatility")
    else:
        if args.dsn:
            parser.error("Provide each DSN with --server")
        variants = [{"label": label, "server_version": version,
                     "pg_search": None if pg_search == "-" else pg_search}
                    for label, _, version, pg_search in args.server]
        dsns = {label: dsn for label, dsn, _, _ in args.server}
    if len(variants) < 2 or len(dsns) != len(variants):
        raise ValueError("Require at least two uniquely labeled targets")
    words = COMMON.read_words((ROOT / "sql/data/pinyin_words.csv").read_bytes())
    words = {w: p for w, p in words.items() if all("\u3400" <= c <= "\u9fff" for c in w)}
    corpus = COMMON.make_corpus(words, args.rows)
    queries = [q["inputs"]["compat"] for q in COMMON.make_queries(corpus, args.queries)]
    methods = {
        "char_text": consumed("public.pinyin_char_romanize(name)"),
        "word_text": consumed("public.pinyin_word_romanize(name)"),
        "word_tokens": consumed("public.pinyin_word_romanize(tokens)"),
        "batch_512": consumed("b.output", "(SELECT (row_number() OVER (ORDER BY batch,n))::bigint id,output "
            "FROM (SELECT (id-1)/512 batch,array_agg(name ORDER BY id) names FROM bench_docs GROUP BY 1) g "
            "CROSS JOIN LATERAL unnest(public.pinyin_word_romanize_batch(names)) WITH ORDINALITY b(output,n)) b"),
        "patterns_dynamic": consumed("public.pinyin_regex_phrase_patterns(replace(plain,' ',''))::text"),
        "patterns_constant": consumed("public.pinyin_regex_phrase_patterns('whh')::text"),
    }
    expected_word = (args.rows, sum(len(r["plain"].encode()) for r in corpus),
                     hashlib.sha256("\n".join(r["plain"] for r in corpus).encode()).hexdigest())
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "variants": variants,
              "configuration": {"rows": args.rows, "queries": args.queries, "rounds": args.rounds,
                                "manifest": str(args.manifest) if args.manifest else None, "output": str(args.output)},
              "corpus_sha256": digest(corpus), "queries_sha256": digest(queries), "queries": queries,
              "source_sha256": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in [ROOT / "Cargo.toml", ROOT / "Cargo.lock", ROOT / "src/lib.rs", Path(__file__),
                            ROOT / "sql/data/pinyin_words.csv", ROOT / "sql/data/pinyin_mapping.csv",
                            ROOT / "sql/data/pinyin_token.csv"]},
              "aggregate_statements": methods,
              "host_before": {kind: Path("/proc/pressure", kind).read_text() for kind in ("memory", "cpu", "io")},
              "samples": []}
    search_refs, aggregate_refs = {}, {}
    workload = [*methods, *([] if args.manifest else ["core_copy"]), "indexed_copy", "ranked_search"]
    for round_index in range(args.rounds):
        shift = round_index % len(variants)
        ordered = variants[shift:] + variants[:shift]
        for variant in ordered:
            dsn = dsns[variant["label"]]
            has_search = bool(variant.get("pg_search", args.manifest))
            database = PREFIX + uuid.uuid4().hex[:16]
            with psycopg.connect(dsn, autocommit=True) as admin:
                admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(database)))
            print(f"[round {round_index + 1}] {variant['label']} {database}", flush=True)
            try:
                cell = {"round": round_index, "variant": variant["label"], "database": database,
                        "setup": setup(dsn, database, variant, corpus, search=has_search), "methods": {}}
                ordered_methods = list(methods.items())
                shift = round_index % len(ordered_methods)
                ordered_methods = ordered_methods[shift:] + ordered_methods[:shift]
                for method, statement in ordered_methods:
                    expected = expected_word if method in ("word_text", "word_tokens", "batch_512") else aggregate_refs.get(method)
                    result = aggregate_run(dsn, database, statement, expected)
                    aggregate_refs.setdefault(method, result["result"])
                    cell["methods"][method] = result
                    print(f"  {method}: {result['warm_ms']:.3f} ms", flush=True)
                if not args.manifest:
                    cell["methods"]["core_copy"] = copy_run(dsn, database, corpus, search=False)
                if has_search:
                    cell["methods"]["indexed_copy"] = copy_run(dsn, database, corpus)
                    # Require repeatability per search release; cross-release scores may legitimately change.
                    search_release = cell["setup"]["pg_search"]
                    search_key = "manifest" if args.manifest else search_release
                    result = search_run(dsn, database, queries, search_refs.get(search_key))
                    search_refs.setdefault(search_key, result["full_scored_hits"])
                    cell["methods"]["ranked_search"] = result
                    print(f"  ranked_search: {result['warm_ms']:.3f} ms", flush=True)
                report["samples"].append(cell)
            finally:
                with psycopg.connect(dsn, autocommit=True) as admin:
                    admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))
    report["host_after"] = {kind: Path("/proc/pressure", kind).read_text() for kind in ("memory", "cpu", "io")}
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode()
    with raw_path.open("xb") as stream:
        stream.write(gzip.compress(raw))
    lines = ["# " + ("pgrx Upgrade Benchmark" if args.manifest else "PostgreSQL Platform Benchmark"),
             "", f"Started: {report['started_at']}.", "",
             f"{args.rows:,} documents; {args.queries:,} queries; {args.rounds} fresh rounds per variant.", "",
             ("All variants share one PostgreSQL server." if args.manifest else
              "The same corpus and queries are used on each server. Core-only targets cannot run BM25."),
             "Native library hashes, full scored hits, plans, warm/first-backend aggregates and pressure are retained in JSON.", "",
             "| Method | " + " | ".join(v["label"] for v in variants) + " |",
             "| --- | " + " | ".join("---:" for _ in variants) + " |"]
    for method in workload:
        columns = []
        for variant in variants:
            times = [c["methods"][method]["warm_ms"] for c in report["samples"]
                     if c["variant"] == variant["label"] and method in c["methods"]]
            columns.append(f"{statistics.median(times):.3f}" if times else "n/a")
        lines.append("| " + method + " | " + " | ".join(columns) + " |")
    lines += ["", "Times are client wall milliseconds per complete workload, not isolated Rust CPU.",
              "First-backend aggregate includes initialization; it is not a cold OS-cache measurement.",
              "COPY includes first-use conversion, writes, upload and commit; it is not warm conversion CPU.",
              "Constant parsing intentionally measures planner constant-folding separately from dynamic inputs.",
              "Fresh databases prevent collisions, not shared OS caches or external host load.",
              "No business-ranking or index-reduction improvement is inferred from output parity.",
              "", f"Raw uncompressed SHA256: `{hashlib.sha256(raw).hexdigest()}`."]
    with md_path.open("x") as stream:
        stream.write("\n".join(lines) + "\n")
    print(f"Reports: {md_path}, {raw_path}", flush=True)


if __name__ == "__main__":
    main()
