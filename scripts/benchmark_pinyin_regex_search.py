#!/usr/bin/env python3
"""Benchmark end-to-end pg_search usage for pinyin_regex_phrase."""

from __future__ import annotations

import argparse
import csv
import re
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


DEFAULT_PGURL = "postgres://localhost/postgres"


@dataclass(frozen=True)
class SearchBenchmarkResult:
    label: str
    rows: int
    best_ms: float
    median_ms: float
    mean_ms: float
    per_query_us: float
    checksum: int
    runs_ms: list[float]


def parse_args() -> argparse.Namespace:
    root_dir = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description="Benchmark full pg_search queries with Python-side and Rust-side pinyin regex phrase parsing."
    )
    parser.add_argument("--pgurl", default=DEFAULT_PGURL, help="PostgreSQL connection URL")
    parser.add_argument(
        "--rows",
        type=int,
        default=20000,
        help="number of benchmark queries to read from bench_pinyin_regex_queries",
    )
    parser.add_argument("--runs", type=int, default=3, help="number of timed runs")
    parser.add_argument(
        "--token-file",
        default=str(root_dir / "sql/data/pinyin_token.csv"),
        help="pinyin token CSV used by the Python implementation",
    )
    return parser.parse_args()


def connect(pgurl: str) -> Any:
    try:
        import psycopg

        return psycopg.connect(pgurl)
    except ModuleNotFoundError:
        try:
            import psycopg2

            return psycopg2.connect(pgurl)
        except ModuleNotFoundError as exc:
            raise SystemExit(
                "missing psycopg driver; install python3-psycopg or psycopg2 to run full search benchmark"
            ) from exc


def load_python_patterns(token_file: str) -> Callable[[str], list[str] | None]:
    tokens: list[str] = []
    with open(token_file, newline="") as fh:
        for token, category in csv.reader(fh):
            if category == "1" or token in {"zh", "ch", "sh"}:
                tokens.append(token)

    tokens = sorted(set(tokens), key=lambda item: (-len(item), item))
    tokenizer = re.compile("(" + "|".join(re.escape(token) for token in tokens) + "|[a-z])")
    all_english = re.compile(r"[a-zA-Z\s]+$")

    def helper(value: str) -> list[str] | None:
        if not all_english.match(value):
            return []
        patterns = [f"{token}.*" for token in tokenizer.findall(value.lower())]
        return patterns

    return helper


def fetch_queries(conn: Any, rows: int) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT query
            FROM public.bench_pinyin_regex_queries
            ORDER BY id
            LIMIT %s
            """,
            (rows,),
        )
        return [row[0] for row in cur.fetchall()]


def run_python_client_once(
    conn: Any,
    queries: list[str],
    build_patterns: Callable[[str], list[str] | None],
) -> int:
    checksum = 0
    with conn.cursor() as cur:
        for query in queries:
            patterns = build_patterns(query)
            cur.execute(
                """
                SELECT count(*)
                FROM public.bench_search_names
                WHERE pinyin @@@ public.sql_pinyin__regex_phrase_query_from_patterns(%s::text[])
                """,
                (patterns,),
            )
            checksum += cur.fetchone()[0]
    return checksum


def run_rust_pg_once(conn: Any, queries: list[str]) -> int:
    checksum = 0
    with conn.cursor() as cur:
        for query in queries:
            cur.execute(
                """
                SELECT count(*)
                FROM public.bench_search_names
                WHERE pinyin @@@ public.pinyin_regex_phrase(%s)
                """,
                (query,),
            )
            checksum += cur.fetchone()[0]
    return checksum


def run_benchmark(
    conn: Any,
    label: str,
    queries: list[str],
    runs: int,
    run_once: Callable[[], int],
) -> SearchBenchmarkResult:
    if runs <= 0:
        raise ValueError("--runs must be positive")

    run_once()

    durations: list[float] = []
    checksum = 0
    for _ in range(runs):
        start = time.perf_counter()
        checksum = run_once()
        durations.append((time.perf_counter() - start) * 1000.0)
        conn.rollback()

    best = min(durations)
    return SearchBenchmarkResult(
        label=label,
        rows=len(queries),
        best_ms=best,
        median_ms=statistics.median(durations),
        mean_ms=statistics.fmean(durations),
        per_query_us=(best * 1000.0 / len(queries)) if queries else 0.0,
        checksum=checksum,
        runs_ms=durations,
    )


def print_result(result: SearchBenchmarkResult) -> None:
    print("")
    print("=== Full pg_search Benchmark: pinyin_regex_phrase ===")
    print(f"helper: {result.label}")
    print("")
    print("| metric | value |")
    print("| --- | ---: |")
    print(f"| queries | {result.rows} |")
    print(f"| best | {result.best_ms:.3f} ms |")
    print(f"| median | {result.median_ms:.3f} ms |")
    print(f"| mean | {result.mean_ms:.3f} ms |")
    print(f"| best per query | {result.per_query_us:.3f} us |")
    print(f"| result checksum | {result.checksum} |")
    print("")
    print(f"runs_ms: {', '.join(f'{item:.3f}' for item in result.runs_ms)}")


def main() -> int:
    args = parse_args()
    build_patterns = load_python_patterns(args.token_file)
    conn = connect(args.pgurl)
    try:
        conn.autocommit = True
        queries = fetch_queries(conn, args.rows)
        python_result = run_benchmark(
            conn,
            "Python client parse + text[] patterns + pg_search",
            queries,
            args.runs,
            lambda: run_python_client_once(conn, queries, build_patterns),
        )
        rust_result = run_benchmark(
            conn,
            "Rust in-Postgres parse + pg_search",
            queries,
            args.runs,
            lambda: run_rust_pg_once(conn, queries),
        )
    finally:
        conn.close()

    print_result(python_result)
    print_result(rust_result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
