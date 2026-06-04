#!/usr/bin/env python3
"""Benchmark Python pinyin_regex_phrase-compatible query construction."""

from __future__ import annotations

import argparse
import csv
import re
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


DEFAULT_QUERIES = (
    "lun",
    "lunlun",
    "zhengshuang",
    "wangchongyang",
    "xian",
    "wchy",
    "zh sh",
    "   ",
    "abc",
)


@dataclass(frozen=True)
class BenchmarkResult:
    label: str
    mode: str
    rows: int
    nonnull: int
    checksum: int
    best_ms: float
    median_ms: float
    mean_ms: float
    per_row_us: float
    last_result: str | None
    runs_ms: list[float]


def parse_args() -> argparse.Namespace:
    root_dir = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description="Benchmark Python pinyin_regex_phrase-compatible query construction."
    )
    parser.add_argument(
        "--rows",
        type=int,
        default=20000,
        help="number of query strings to build",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=5,
        help="number of timed runs",
    )
    parser.add_argument(
        "--token-file",
        default=str(root_dir / "sql/data/pinyin_token.csv"),
        help="pinyin token CSV used by the Python implementation",
    )
    parser.add_argument(
        "--queries",
        default=",".join(DEFAULT_QUERIES),
        help="comma-separated query mix",
    )
    parser.add_argument(
        "--mode",
        choices=("tokens", "query"),
        default="tokens",
        help="benchmark token pattern construction or pdb.query string construction",
    )
    return parser.parse_args()


def load_python_helper(token_file: str, mode: str) -> Callable[[str], list[str] | str | None]:
    tokens: list[str] = []
    with open(token_file, newline="") as fh:
        for token, category in csv.reader(fh):
            if category == "1" or token in {"zh", "ch", "sh"}:
                tokens.append(token)

    tokens = sorted(set(tokens), key=lambda item: (-len(item), item))
    tokenizer = re.compile("(" + "|".join(re.escape(token) for token in tokens) + "|[a-z])")
    all_english = re.compile(r"[a-zA-Z\s]+$")

    def helper(value: str) -> list[str] | str | None:
        if not all_english.match(value):
            return []
        pinyins = tokenizer.findall(value.lower())
        patterns = [f"{token}.*" for token in pinyins]
        if mode == "tokens":
            return patterns
        if not patterns:
            return "pdb.empty()"
        if len(patterns) < 2:
            return f"pdb.regex({patterns[0]!r})"
        return f"pdb.regex_phrase({patterns!r})"

    return helper


def build_inputs(rows: int, queries_text: str) -> list[str]:
    queries = [query for query in queries_text.split(",")]
    if rows < 0:
        raise ValueError("--rows must be non-negative")
    if not queries:
        raise ValueError("--queries must contain at least one query")
    return [queries[i % len(queries)] for i in range(rows)]


def run_benchmark(
    helper: Callable[[str], list[str] | str | None],
    label: str,
    mode: str,
    inputs: list[str],
    runs: int,
) -> BenchmarkResult:
    if runs <= 0:
        raise ValueError("--runs must be positive")

    for value in inputs[: min(1000, len(inputs))]:
        helper(value)

    durations: list[float] = []
    nonnull = 0
    checksum = 0
    last_result: list[str] | str | None = None

    for _ in range(runs):
        start = time.perf_counter()
        local_nonnull = 0
        local_checksum = 0
        for value in inputs:
            result = helper(value)
            if result is not None:
                local_nonnull += 1
                if isinstance(result, list):
                    local_checksum += len(result)
                    local_checksum += sum(len(item) for item in result)
                else:
                    local_checksum += len(result)
                last_result = result
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        durations.append(elapsed_ms)
        nonnull = local_nonnull
        checksum = local_checksum

    best_ms = min(durations)
    return BenchmarkResult(
        label=label,
        mode=mode,
        rows=len(inputs),
        nonnull=nonnull,
        checksum=checksum,
        best_ms=best_ms,
        median_ms=statistics.median(durations),
        mean_ms=statistics.fmean(durations),
        per_row_us=(best_ms * 1000.0 / len(inputs)) if inputs else 0.0,
        last_result=repr(last_result),
        runs_ms=durations,
    )


def print_result(result: BenchmarkResult) -> None:
    print("")
    print("=== Query Builder Benchmark: Python pinyin_regex_phrase ===")
    print(f"helper: {result.label}")
    print(f"mode: {result.mode}")
    print("")
    print("| metric | value |")
    print("| --- | ---: |")
    print(f"| rows | {result.rows} |")
    print(f"| non-null queries | {result.nonnull} |")
    print(f"| best | {result.best_ms:.3f} ms |")
    print(f"| median | {result.median_ms:.3f} ms |")
    print(f"| mean | {result.mean_ms:.3f} ms |")
    print(f"| best per row | {result.per_row_us:.3f} us |")
    print(f"| checksum | {result.checksum} |")
    print("")
    print(f"runs_ms: {', '.join(f'{item:.3f}' for item in result.runs_ms)}")
    print(f"last_result: {result.last_result}")


def main() -> int:
    args = parse_args()
    inputs = build_inputs(args.rows, args.queries)
    helper = load_python_helper(args.token_file, args.mode)
    label = "pure Python compatible implementation"

    result = run_benchmark(helper, label, args.mode, inputs, args.runs)
    print_result(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
