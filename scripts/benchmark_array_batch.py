#!/usr/bin/env python3
"""Compare the pre-optimization P1 snapshot with native arrays/document batches.

Every run uses a fresh database. Precomputed character-token conversion, ICU
tokenization, independent-document conversion and unindexed INSERT are separate
measurements. Both snapshots use the unreleased development version 0.0.9;
0.0.8 remains the latest release. This has no Dispatcher repository dependency.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import csv
from datetime import datetime, timezone
import hashlib
import io
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
BASELINE_COMMIT = "f1094beeb69c093085403f5b02d616a4de7adabe"
BASELINE_VERSION = "0.0.9"
CURRENT_VERSION = "0.0.9"
BATCH_SIZES = (1, 32, 128, 512, 2048)
MAX_BENCHMARK_BATCH_SIZE = 8192
ROWWISE_METHODS = ("legacy_scalar", "current_scalar", "legacy_tokens", "current_tokens")
LEGACY_SYMBOLS = {
    "bench_legacy_scalar": ("text", "pinyin_word_romanize_wrapper"),
    "bench_legacy_tokens": ("anyelement", "pinyin_word_romanize_with_tokenizer_wrapper"),
}
GOLD_DOCUMENTS = [
    ("umlaut", "吕梁", "lv liang"),
    ("restored traditional", "呂梁", "lv liang"),
    ("restored polyphony", "銀行", "yin hang"),
    ("restored long phrase", "丙酸氟替卡鬆", "bing suan fu ti ka song"),
    ("phrase polyphony", "银行", "yin hang"),
    ("document boundary left", "银", "yin"),
    ("document boundary right", "行", "xing"),
    ("mixed Han and ASCII", "郑爽ABC", "zheng shuang abc"),
    ("ASCII case", "ABC123", "abc123"),
    ("ASCII words", "Hello World", "hello world"),
    ("empty document", "", ""),
    ("NULL document", None, None),
]


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def serialized_digest(value: Any) -> str:
    return digest(json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode())


def timing_summary(values: list[float]) -> dict[str, Any]:
    return {"samples_ms": values, "median_ms": statistics.median(values),
            "min_ms": min(values), "max_ms": max(values)}


def bounded_chunks(values: list[Any], size: int) -> list[tuple[int, list[Any]]]:
    if size < 1:
        raise ValueError("Batch size must be positive")
    return [(offset + 1, values[offset:offset + size]) for offset in range(0, len(values), size)]


def batch_method(size: int) -> str:
    if size < 1:
        raise ValueError("Batch size must be positive")
    return f"current_batch_{size}"


def all_methods(sizes: tuple[int, ...]) -> list[str]:
    if (not sizes or len(sizes) != len(set(sizes))
            or any(not 1 <= size <= MAX_BENCHMARK_BATCH_SIZE for size in sizes)):
        raise ValueError("Batch sizes must be nonempty, unique and within 1..8192")
    return [*ROWWISE_METHODS, *(batch_method(size) for size in sizes)]


def rotated_methods(round_index: int, sizes: tuple[int, ...]) -> list[str]:
    methods = all_methods(sizes)
    offset = (round_index * 2) % len(methods)
    rotated = methods[offset:] + methods[:offset]
    return rotated if round_index % 2 == 0 else list(reversed(rotated))


def read_csv(data: bytes) -> dict[str, str]:
    rows = list(csv.reader(io.StringIO(data.decode("utf-8"))))
    result = dict(rows)
    if len(rows) != len(result):
        raise ValueError("Duplicate dictionary key")
    return result


def is_han(value: str) -> bool:
    return bool(value) and all(
        0x3400 <= ord(char) <= 0x4DBF or 0x4E00 <= ord(char) <= 0x9FFF
        or 0xF900 <= ord(char) <= 0xFAFF or 0x20000 <= ord(char) <= 0x323AF
        for char in value
    )


def make_corpus(words: dict[str, str], characters: dict[str, str], count: int) -> list[dict[str, Any]]:
    eligible = [word for word, reading in words.items()
                if is_han(word) and all(char in characters for char in word)
                and len(word) == len(reading.split())
                and all(re.fullmatch("[a-z]+", token) for token in reading.split())]
    if not 1 <= count <= len(eligible):
        raise ValueError("Requested corpus exceeds normalized mapped Han-only phrases")
    selected = sorted(eligible, key=lambda word: (digest(word.encode()), word))[:count]
    return [{"id": ident, "document": word, "tokens": list(word), "expected": words[word]}
            for ident, word in enumerate(selected, 1)]


def output_signature(rows: list[tuple[int, str]]) -> tuple[int, int, str]:
    if not rows or any(output is None or not output for _, output in rows):
        raise RuntimeError("Empty performance output invalidates comparison")
    ids = [ident for ident, _ in rows]
    if ids != list(range(1, len(rows) + 1)):
        raise RuntimeError("Output IDs must cover each document once in source order")
    joined = "\n".join(f"{ident}:{output}" for ident, output in rows)
    return len(rows), sum(len(output.encode()) for _, output in rows), hashlib.md5(joined.encode()).hexdigest()


def verify_outputs(actual: list[tuple[int, str]], expected: list[tuple[int, str]], label: str) -> dict[str, Any]:
    signature = output_signature(actual)
    if actual != expected:
        mismatch = next((index for index, pair in enumerate(zip(actual, expected))
                         if pair[0] != pair[1]), min(len(actual), len(expected)))
        raise RuntimeError(f"Actual document output differs from reference: {label}/position={mismatch}")
    return {"signature": list(signature), "id_output_sha256": serialized_digest(actual),
            "verified_rows": len(actual)}


def connection(pgurl: str, database: str):
    return psycopg.connect(make_conninfo(pgurl, dbname=database), autocommit=True)


def configure(cursor) -> None:
    cursor.execute("SET jit = off; SET max_parallel_workers_per_gather = 0", prepare=False)


def new_database_name() -> str:
    return "pg_pinyin_p2_" + uuid.uuid4().hex[:16]


def drop_database(admin, name: str) -> None:
    if not re.fullmatch(r"pg_pinyin_p2_[a-f0-9]{16}", name):
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


def baseline_paths(library: Path | None, installation_sql: Path | None,
                   manifest: Path | None) -> tuple[Path, Path, Path]:
    library_dir = Path(subprocess.check_output(["pg_config", "--pkglibdir"], text=True).strip())
    return (library or library_dir / "pg_pinyin_baseline_0_0_9.so",
            installation_sql or library_dir / "pg_pinyin_baseline_0_0_9.sql",
            manifest or library_dir / "pg_pinyin_baseline_0_0_9.json")


def baseline_provenance(library: Path, installation_sql: Path, manifest: Path) -> dict[str, Any]:
    for path in (library, installation_sql, manifest):
        if not path.is_file():
            raise RuntimeError(f"Real rebuilt pre-optimization P1 snapshot required; missing {path}")
    declaration = json.loads(manifest.read_text(encoding="utf-8"))
    if declaration.get("build_profile") != "release":
        raise RuntimeError("Real baseline must be rebuilt with the release profile")
    rust_version = declaration.get("rust_version")
    if not isinstance(rust_version, str) or not rust_version.startswith("rustc "):
        raise RuntimeError("Baseline manifest must record the actual Rust compiler version")
    observed = {"source_commit": BASELINE_COMMIT, "extension_version": BASELINE_VERSION,
                "library_sha256": digest(library.read_bytes()),
                "installation_sql_sha256": digest(installation_sql.read_bytes())}
    if any(declaration.get(key) != value for key, value in observed.items()):
        raise RuntimeError("Baseline source/version/library/installation SQL manifest mismatch")
    old_sql = installation_sql.read_text(encoding="utf-8")
    if "pinyin_word_romanize_batch" in old_sql or any(symbol not in old_sql for _, symbol in LEGACY_SYMBOLS.values()):
        raise RuntimeError("Baseline snapshot installation SQL is not the pre-batch Rust API")
    return {**observed, "snapshot_kind": "pre-optimization P1, unreleased development",
            "build_profile": "release", "rust_version": rust_version,
            "library_path": str(library.resolve()),
            "installation_sql_path": str(installation_sql.resolve()),
            "manifest_path": str(manifest.resolve()), "manifest_sha256": digest(manifest.read_bytes())}


def create_legacy_aliases(cursor, baseline: dict[str, Any]) -> list[dict[str, str]]:
    for name, (argument, symbol) in LEGACY_SYMBOLS.items():
        cursor.execute(sql.SQL("CREATE FUNCTION public.{}({}) RETURNS text AS {},{} "
                               "LANGUAGE c IMMUTABLE STRICT PARALLEL SAFE").format(
            sql.Identifier(name), sql.SQL(argument), sql.Literal(baseline["library_path"]), sql.Literal(symbol)))
    cursor.execute("SELECT p.proname,p.probin,p.prosrc,l.lanname FROM pg_proc p "
                   "JOIN pg_language l ON l.oid=p.prolang WHERE p.proname=ANY(%s) ORDER BY p.proname",
                   (list(LEGACY_SYMBOLS),))
    aliases = []
    for name, module, symbol, language in cursor.fetchall():
        if module != baseline["library_path"] or symbol != LEGACY_SYMBOLS[name][1] or language != "c":
            raise RuntimeError("Legacy alias does not bind the real old C module/symbol")
        aliases.append({"function": name, "module": module, "symbol": symbol, "language": language})
    if len(aliases) != len(LEGACY_SYMBOLS):
        raise RuntimeError("Missing real baseline aliases")
    return aliases


def require_versions(environment: dict[str, Any], pg_search_version: str) -> None:
    extensions = environment["extensions"]
    if extensions.get("pg_search") != pg_search_version or extensions.get("pg_pinyin") != CURRENT_VERSION:
        raise RuntimeError(f"Expected pg_search {pg_search_version} and pg_pinyin {CURRENT_VERSION}; actual {extensions}")


def current_artifacts(cursor, build_artifact: Path, rust_version: str) -> dict[str, Any]:
    cursor.execute("SELECT p.oid::regprocedure::text,p.probin,p.prosrc,p.provolatile,"
                   "p.proparallel,p.proisstrict FROM pg_proc p WHERE p.oid=ANY(%s::regprocedure[]) "
                   "ORDER BY p.oid::regprocedure::text", ([
                       "public.pinyin_word_romanize(text)", "public.pinyin_word_romanize(anyelement)",
                       "public.pinyin_word_romanize_batch(text[])",
                   ],))
    functions = cursor.fetchall()
    if len(functions) != 3 or len({row[1] for row in functions}) != 1:
        raise RuntimeError("Current scalar, native-array and batch functions must bind one Rust module")
    batch = next((row for row in functions if row[2] == "pinyin_word_romanize_batch_wrapper"), None)
    if batch is None or batch[3:] != ("s", "s", True):
        raise RuntimeError("Current batch C symbol/catalog properties do not match the tested contract")
    library_dir = Path(subprocess.check_output(["pg_config", "--pkglibdir"], text=True).strip())
    shared_dir = Path(subprocess.check_output(["pg_config", "--sharedir"], text=True).strip())
    module = functions[0][1]
    library = Path(module.replace("$libdir", str(library_dir)))
    if not library.is_file():
        library = library.with_name(library.name + ".so")
    installed_sql = shared_dir / f"extension/pg_pinyin--{CURRENT_VERSION}.sql"
    if not library.is_file() or not installed_sql.is_file():
        raise RuntimeError("Current installed Rust module/installation SQL cannot be fingerprinted")
    if not build_artifact.is_file():
        raise RuntimeError(f"Current release build artifact required; missing {build_artifact}")
    if build_artifact.parent.name != "release":
        raise RuntimeError("Current build artifact must come from Cargo's release directory")
    artifact_hash = digest(build_artifact.read_bytes())
    installed_hash = digest(library.read_bytes())
    if artifact_hash != installed_hash:
        raise RuntimeError("Current installed DLL differs from the declared release build artifact")
    if not rust_version.startswith("rustc "):
        raise RuntimeError("Current actual Rust compiler version must be recorded")
    return {"library_path": str(library.resolve()), "library_sha256": installed_hash,
            "build_profile": "release", "build_artifact_path": str(build_artifact.resolve()),
            "build_artifact_sha256": artifact_hash, "rust_version": rust_version,
            "installation_sql_path": str(installed_sql.resolve()),
            "installation_sql_sha256": digest(installed_sql.read_bytes()),
            "catalog": [{"function": name, "module": module, "symbol": symbol,
                         "volatility": volatility, "parallel": parallel, "strict": strict}
                        for name, module, symbol, volatility, parallel, strict in functions]}


def require_comparable_artifacts(current: dict[str, Any], baseline: dict[str, Any]) -> None:
    if current["rust_version"] != baseline["rust_version"]:
        raise RuntimeError("Baseline/current release builds must use the same recorded Rust compiler")
    if current["library_sha256"] == baseline["library_sha256"]:
        raise RuntimeError("Current DLL is identical to the pre-optimization P1 snapshot")


def setup(pgurl: str, name: str, corpus: list[dict[str, Any]], sizes: tuple[int, ...],
          data: Path, baseline: dict[str, Any], current_build_artifact: Path,
          current_rust_version: str) -> dict[str, Any]:
    with connection(pgurl, name) as conn, conn.cursor() as cursor:
        configure(cursor)
        cursor.execute("CREATE EXTENSION pg_search CASCADE")
        cursor.execute("CREATE EXTENSION pg_pinyin")
        installed = current_artifacts(cursor, current_build_artifact, current_rust_version)
        require_comparable_artifacts(installed, baseline)
        aliases = create_legacy_aliases(cursor, baseline)
        cursor.execute("TRUNCATE pinyin.pinyin_mapping,pinyin.pinyin_token,pinyin.pinyin_words")
        for filename, table, columns in (
            ("pinyin_mapping.csv", "pinyin_mapping", "character,pinyin"),
            ("pinyin_token.csv", "pinyin_token", "character,category"),
            ("pinyin_words.csv", "pinyin_words", "word,pinyin"),
        ):
            with cursor.copy(f"COPY pinyin.{table} ({columns}) FROM STDIN WITH (FORMAT CSV)") as copy:
                copy.write((data / filename).read_bytes())
        cursor.execute("INSERT INTO pinyin.pinyin_mapping VALUES (' ',' ') ON CONFLICT (character) DO NOTHING")
        cursor.execute("UPDATE pinyin.pinyin_dictionary_meta SET version=version+1 WHERE singleton")
        cursor.execute("CREATE TABLE bench_corpus (id bigint PRIMARY KEY,document text NOT NULL,"
                       "tokens text[] NOT NULL,expected text NOT NULL)")
        with cursor.copy("COPY bench_corpus FROM STDIN") as copy:
            for row in corpus:
                copy.write_row((row["id"], row["document"], row["tokens"], row["expected"]))
        cursor.execute("CREATE TABLE bench_chunks (batch_size integer NOT NULL,start_id bigint NOT NULL,"
                       "documents text[] NOT NULL,PRIMARY KEY(batch_size,start_id))")
        documents = [row["document"] for row in corpus]
        with cursor.copy("COPY bench_chunks FROM STDIN") as copy:
            for size in sizes:
                for start_id, batch in bounded_chunks(documents, size):
                    copy.write_row((size, start_id, batch))
        cursor.execute("ANALYZE bench_corpus; ANALYZE bench_chunks", prepare=False)
        cursor.execute("SELECT version(),current_setting('server_version_num')")
        pg_version, version_num = cursor.fetchone()
        cursor.execute("SELECT extname,extversion FROM pg_extension ORDER BY extname")
        extensions = dict(cursor.fetchall())
        cursor.execute("SELECT name,setting,unit FROM pg_settings WHERE name IN "
                       "('jit','max_parallel_workers_per_gather','shared_buffers','work_mem',"
                       "'maintenance_work_mem') ORDER BY name")
        settings = {key: {"value": value, "unit": unit} for key, value, unit in cursor.fetchall()}
    return {"postgresql": pg_version, "server_version_num": version_num,
            "extensions": extensions, "settings": settings, "legacy_aliases": aliases,
            "current_installed_artifacts": installed}


def conversion_select(method: str) -> tuple[str, tuple[Any, ...]]:
    expressions = {"legacy_scalar": "public.bench_legacy_scalar(document)",
                   "current_scalar": "public.pinyin_word_romanize(document)",
                   "legacy_tokens": "public.bench_legacy_tokens(tokens)",
                   "current_tokens": "public.pinyin_word_romanize(tokens)"}
    if method in expressions:
        return f"SELECT id,{expressions[method]} AS output FROM bench_corpus", ()
    match = re.fullmatch(r"current_batch_([1-9][0-9]*)", method)
    if not match:
        raise ValueError("Unknown conversion method")
    return ("SELECT c.start_id+o.ordinality-1 AS id,o.output FROM bench_chunks c "
            "CROSS JOIN LATERAL unnest(public.pinyin_word_romanize_batch(c.documents)) "
            "WITH ORDINALITY AS o(output,ordinality) WHERE c.batch_size=%s", (int(match[1]),))


def forced_statement(select: str) -> str:
    return (f"WITH converted AS MATERIALIZED ({select}) SELECT count(*),"
            "sum(octet_length(output)),md5(string_agg(id::text||':'||output,E'\\n' ORDER BY id)) "
            "FROM converted")


def checked_forced_call(cursor, statement: str, params: tuple[Any, ...],
                        expected_signature: tuple[int, int, str]) -> float:
    started = time.perf_counter()
    cursor.execute(statement, params)
    actual = cursor.fetchone()
    elapsed = (time.perf_counter() - started) * 1000
    if tuple(actual) != expected_signature:
        raise RuntimeError(f"Timed count/byte sum/ordered ID-output checksum mismatch: {actual}")
    return elapsed


def measure_generation(pgurl: str, name: str, method: str,
                       expected: list[tuple[int, str]]) -> dict[str, Any]:
    select, params = conversion_select(method)
    statement = forced_statement(select)
    signature = output_signature(expected)
    with connection(pgurl, name) as conn, conn.cursor() as cursor:
        configure(cursor)
        cold = checked_forced_call(cursor, statement, params, signature)
        checked_forced_call(cursor, statement, params, signature)
        warm = checked_forced_call(cursor, statement, params, signature)
        cursor.execute(f"SELECT id,output FROM ({select}) actual ORDER BY id", params)
        verification = verify_outputs(cursor.fetchall(), expected, method)
        cursor.execute("EXPLAIN (VERBOSE, FORMAT JSON) " + statement, params)
        plan = cursor.fetchone()[0]
    return {"cold_ms": cold, "warm_ms": warm, "statement": statement,
            "parameters": list(params), "plan_without_execution": plan, **verification}


def check_gold(pgurl: str, name: str) -> dict[str, Any]:
    documents = [document for _, document, _ in GOLD_DOCUMENTS]
    expected = [output for _, _, output in GOLD_DOCUMENTS]
    with connection(pgurl, name) as conn, conn.cursor() as cursor:
        configure(cursor)
        for function in ("public.bench_legacy_scalar", "public.pinyin_word_romanize"):
            for label, document, output in GOLD_DOCUMENTS:
                cursor.execute(f"SELECT {function}(%s::text)", (document,))
                if cursor.fetchone()[0] != output:
                    raise RuntimeError(f"Independent document gold failed: {function}/{label}")
        cursor.execute("SELECT public.pinyin_word_romanize_batch(%s::text[])", (documents,))
        batch = cursor.fetchone()[0]
        if batch != expected:
            raise RuntimeError(f"Independent batch gold/NULL/document boundaries failed: {batch!r}")
        cursor.execute("SELECT public.pinyin_word_romanize_batch(NULL::text[]),"
                       "public.pinyin_word_romanize_batch(ARRAY[]::text[]),"
                       "public.pinyin_word_romanize_batch(ARRAY[['银',NULL],['行','银行']]::text[])")
        if cursor.fetchone() != (None, [], ["yin", None, "xing", "yin hang"]):
            raise RuntimeError("NULL array, empty array or multidimensional flattening contract failed")
        token_cases = [(["  ABC  ", "", " 银 ", "行"], "abc yin hang"),
                       (["呂梁"], "lv liang"), (["", "  "], "")]
        for tokens, output in token_cases:
            for function in ("public.bench_legacy_tokens", "public.pinyin_word_romanize"):
                cursor.execute(f"SELECT {function}(%s::text[])", (tokens,))
                if cursor.fetchone()[0] != output:
                    raise RuntimeError(f"Independent array token gold failed: {function}/{tokens!r}")
        token_null_errors = {}
        for function in ("public.bench_legacy_tokens", "public.pinyin_word_romanize"):
            try:
                cursor.execute(f"SELECT {function}(ARRAY['银',NULL,'行']::text[])")
            except psycopg.Error as error:
                if error.sqlstate != "XX000":
                    raise RuntimeError(f"Unexpected token-array NULL error state: {function}/{error.sqlstate}") from error
                token_null_errors[function] = error.sqlstate
            else:
                raise RuntimeError(f"Token-array NULL element no longer raises an error: {function}")
    return {"documents": [{"label": label, "input": document, "expected": output}
                          for label, document, output in GOLD_DOCUMENTS],
            "batch_output": batch, "token_cases": token_cases,
            "multidimensional_expected": ["yin", None, "xing", "yin hang"],
            "token_array_null_element_sqlstates": token_null_errors,
            "note": "Handwritten contract gold, separate from dictionary-derived performance corpus"}


def measure_tokenizer(pgurl: str, name: str, expected_count: int) -> dict[str, Any]:
    select = "SELECT id,array_to_string(document::pdb.icu::text[],' ') AS output FROM bench_corpus"
    statement = forced_statement(select)
    with connection(pgurl, name) as conn, conn.cursor() as cursor:
        configure(cursor)
        started = time.perf_counter()
        cursor.execute(statement)
        cold_signature = tuple(cursor.fetchone())
        cold = (time.perf_counter() - started) * 1000
        cursor.execute(statement)
        if tuple(cursor.fetchone()) != cold_signature:
            raise RuntimeError("ICU tokenizer output drift during warmup")
        started = time.perf_counter()
        cursor.execute(statement)
        warm_signature = tuple(cursor.fetchone())
        warm = (time.perf_counter() - started) * 1000
        if warm_signature != cold_signature or warm_signature[0] != expected_count or not warm_signature[1]:
            raise RuntimeError("ICU tokenizer output/count mismatch")
        cursor.execute(f"SELECT id,output FROM ({select}) actual ORDER BY id")
        actual = cursor.fetchall()
        if output_signature(actual) != warm_signature:
            raise RuntimeError("Actual ICU arrays disagree with timed forcing checksum")
        cursor.execute("SELECT id,document::pdb.icu::text[] FROM bench_corpus ORDER BY id")
        arrays = cursor.fetchall()
        if any(not tokens for _, tokens in arrays):
            raise RuntimeError("Empty ICU token array invalidates tokenizer measurement")
    return {"cold_ms": cold, "warm_ms": warm, "signature": list(warm_signature),
            "actual_arrays_sha256": serialized_digest(arrays),
            "note": "ICU tokenizer only; native-vs-legacy inputs are precomputed character arrays, not these ICU arrays"}


def refresh_insert_table(cursor, method: str) -> str:
    cursor.execute("DROP TABLE IF EXISTS bench_insert")
    if method in ("legacy_scalar", "current_scalar"):
        function = "bench_legacy_scalar" if method == "legacy_scalar" else "pinyin_word_romanize"
        cursor.execute(f"CREATE TABLE bench_insert (id bigint PRIMARY KEY,document text NOT NULL,"
                       f"output text GENERATED ALWAYS AS (public.{function}(document)) STORED)")
        return "INSERT INTO bench_insert (id,document) SELECT id,document FROM bench_corpus ORDER BY id"
    cursor.execute("CREATE TABLE bench_insert (id bigint PRIMARY KEY,document text NOT NULL,output text NOT NULL)")
    select, _ = conversion_select(method)
    return (f"WITH converted AS MATERIALIZED ({select}) INSERT INTO bench_insert (id,document,output) "
            "SELECT c.id,c.document,o.output FROM bench_corpus c JOIN converted o ON o.id=c.id ORDER BY c.id")


def measure_insert(pgurl: str, name: str, method: str,
                   expected: list[tuple[int, str]]) -> dict[str, Any]:
    samples = {}
    select, params = conversion_select(method)
    signature = output_signature(expected)
    with connection(pgurl, name) as conn, conn.cursor() as cursor:
        configure(cursor)
        for phase in ("cold", "warm"):
            if phase == "warm":
                checked_forced_call(cursor, forced_statement(select), params, signature)
            statement = refresh_insert_table(cursor, method)
            started = time.perf_counter()
            cursor.execute(statement, params)
            elapsed = (time.perf_counter() - started) * 1000
            inserted = cursor.rowcount
            cursor.execute("SELECT id,output FROM bench_insert ORDER BY id")
            verification = verify_outputs(cursor.fetchall(), expected, f"insert/{method}/{phase}")
            if inserted != len(expected):
                raise RuntimeError("INSERT did not process every source ID exactly once")
            cursor.execute("SELECT pg_relation_size('bench_insert'),pg_total_relation_size('bench_insert')")
            heap_bytes, total_bytes = cursor.fetchone()
            cursor.execute("EXPLAIN (VERBOSE, FORMAT JSON) " + statement, params)
            plan = cursor.fetchone()[0]
            samples[phase] = {"ms": elapsed, "inserted_rows": inserted,
                              "heap_relation_bytes": heap_bytes, "table_total_bytes": total_bytes,
                              "statement": statement, "parameters": list(params),
                              "plan_without_execution": plan,
                              **verification}
        cursor.execute("DROP TABLE bench_insert")
    return samples


def summarize(report: dict[str, Any]) -> None:
    count = report["configuration"]["rows"]
    expected_hash = report["reference"]["id_output_sha256"]
    for phase_name, sample_name in (("generation", "generation_samples"), ("insert", "insert_samples")):
        report[phase_name] = {}
        for method, samples in report[sample_name].items():
            result = {}
            for phase in ("cold", "warm"):
                phase_samples = samples if phase_name == "generation" else [sample[phase] for sample in samples]
                if any(sample["id_output_sha256"] != expected_hash or sample["verified_rows"] != count
                       for sample in phase_samples):
                    raise RuntimeError(f"Actual ID-output checksum drift: {phase_name}/{method}/{phase}")
                values = ([sample[f"{phase}_ms"] for sample in phase_samples] if phase_name == "generation"
                          else [sample["ms"] for sample in phase_samples])
                result[phase] = {**timing_summary(values),
                                 "rows_per_second_at_median": count * 1000 / statistics.median(values)}
            report[phase_name][method] = result
    if len({tuple(sample["signature"]) for sample in report["tokenizer_samples"]}) != 1:
        raise RuntimeError("ICU tokenizer output signature drift across runs")
    report["tokenizer"] = {phase: timing_summary([sample[f"{phase}_ms"]
                                                 for sample in report["tokenizer_samples"]])
                           for phase in ("cold", "warm")}


def render_markdown(report: dict[str, Any]) -> str:
    config, baseline, environment = report["configuration"], report["baseline"], report["environment"]
    lines = ["# P2 Native Array and Document Batch Benchmark", "",
             f"- Started UTC: `{report['started_at_utc']}`; runtime HEAD: `{report['runtime_git_sha']}`.",
             "- Working-tree source hashes are recorded separately. Runtime HEAD is not a release/tag claim.",
             f"- PostgreSQL: `{environment['postgresql']}`; extensions: `{json.dumps(environment['extensions'], sort_keys=True)}`.",
             f"- Real baseline: pre-optimization P1 snapshot (unreleased development {BASELINE_VERSION}), commit `{baseline['source_commit']}`, DLL SHA256 `{baseline['library_sha256']}`.",
             "- Both baseline and current use development version 0.0.9. This is a source/artifact comparison, not a comparison between two released extension versions; latest release remains 0.0.8.",
             f"- Baseline/current Cargo build profiles: release/release; recorded compiler: `{baseline['rust_version']}`. This build-profile label does not claim a published release. Current installed DLL must match the declared release-profile build artifact byte-for-byte and differ from the baseline DLL.",
             "- Baseline aliases bind the separately rebuilt old C module and old Rust symbols; no SQL wrapper calls the current core to imitate old behavior.",
             f"- Fresh isolated database; {config['rows']:,} normalized Han-only phrases; {config['runs']} rotated repeats; batch sizes {config['batch_sizes']}.",
             "- Dictionary phrases are hash selected, not supplemented with low-ID gold. Every stored document and character-token array is identical across methods.",
             "- Character-token arrays and bounded input batches are precomputed outside timing. This excludes tokenization and array aggregation/transfer costs from native-vs-legacy and bounded-batch conversion timings.",
             "- Cold means a new PostgreSQL backend (and its dictionary cache), not cold OS storage/shared buffers. Warm samples follow a complete untimed conversion.",
             "- Generation forces materialized output, count, byte sum and ordered ID-output MD5. Full outputs are checked against dictionary readings and independent handwritten gold outside timing.",
             "- Untimed non-executing plans, exact SQL and parameters are recorded. Batch and rowwise inputs have different physical row layouts; timings include their scans and aggregation, not just Rust instructions.",
             "- INSERT measures server-side INSERT SELECT and includes heap/primary-key writes; each phase recreates the table. No BM25 index is maintained and no client COPY/upload cost is included.",
             "- Batch elements are independent documents, NOT tokens within one document. NULL positions, flattened dimensions and bank/character document boundaries have separate handwritten gold.",
             *[f"- Run condition: {note}" for note in report.get("environment_notes", [])],
             "", "## Generation Only", "", "Whole-corpus milliseconds, median of repeated samples.", "",
             "| Method | Cold ms | Warm ms | Warm documents/s |", "| --- | ---: | ---: | ---: |"]
    for method, sample in report["generation"].items():
        lines.append(f"| {method} | {sample['cold']['median_ms']:,.3f} | {sample['warm']['median_ms']:,.3f} | {sample['warm']['rows_per_second_at_median']:,.0f} |")
    lines += ["", "## Fresh Unindexed INSERT", "",
              "Scalar methods use stored generated columns. Token and batch methods insert explicitly romanized output, with identical IDs, source documents and output verified after every timed INSERT.", "",
              "| Method | Cold ms | Warm ms | Warm inserted rows/s |", "| --- | ---: | ---: | ---: |"]
    for method, sample in report["insert"].items():
        lines.append(f"| {method} | {sample['cold']['median_ms']:,.3f} | {sample['warm']['median_ms']:,.3f} | {sample['warm']['rows_per_second_at_median']:,.0f} |")
    lines += ["", "## Separate ICU Tokenizer", "",
              f"- Cold median: {report['tokenizer']['cold']['median_ms']:,.3f} ms; warm median: {report['tokenizer']['warm']['median_ms']:,.3f} ms.",
              "- This measures `document::pdb.icu::text[]` only. It is not added to precomputed character-array timings; ICU arrays can have different token grouping from one-character arrays.",
              "", "## Validation and Limits", "",
              f"- Verified corpus rows: {report['reference']['verified_rows']:,}; ID-output SHA256: `{report['reference']['id_output_sha256']}`.",
              f"- Independent document gold cases: {len(report['gold']['documents'])}; includes restored coverage, umlaut, polyphony, ASCII, empty and NULL documents.",
              "- No document is omitted, joined to an adjacent document, or accepted merely because both implementations share empty output.",
              "- Raw timing samples, checksum signatures, exact baseline manifest, aliases, corpus/dictionary/source hashes and effective database settings are retained in JSON.",
              "- Installed current DLL/installation SQL hashes and C-function catalog bindings are recorded separately from current source hashes to expose stale same-version installations.",
              "- Fresh table relation sizes are recorded as validation context only. No BM25 index build or index-reduction claim is made; dictionary and output semantics are unchanged.",
              "- Times include one client/server round trip per aggregate or INSERT but not transfer of all generated outputs. They are not per-document Python/database round-trip benchmarks.",
              "- Batch sizes are bounded; this does not benchmark an unbounded array or establish a universal optimal batch size. Live dictionary version is checked per explicit batch, not cached for an entire transaction.",
              "- Shared-host load, cache state and timing variance limit speedup claims. This protocol differs from P0/P1 and their absolute timings are not directly comparable.", ""]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pgurl", required=True, help="Admin DSN able to create an isolated database")
    parser.add_argument("--data", type=Path, default=ROOT / "sql/data")
    parser.add_argument("--rows", type=int, default=20000)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--batch-sizes", type=int, nargs="+", default=list(BATCH_SIZES))
    parser.add_argument("--pg-search-version", default="0.26.1")
    parser.add_argument("--baseline-library", type=Path)
    parser.add_argument("--baseline-sql", type=Path)
    parser.add_argument("--baseline-manifest", type=Path)
    parser.add_argument("--current-build-artifact", type=Path,
                        default=ROOT / "target/release/libpg_pinyin.so",
                        help="Actual current release build library; must match the installed DLL SHA256")
    parser.add_argument("--current-rust-version", help="Compiler from the current release build; defaults to rustc --version")
    parser.add_argument("--environment-note", action="append", default=[])
    parser.add_argument("--output-prefix", type=Path, default=ROOT / "benchmarks/p2_array_batch")
    parser.add_argument("--keep-database", action="store_true")
    args = parser.parse_args()
    if args.rows < 1 or args.runs < 3:
        parser.error("--rows must be positive and --runs must be at least 3")
    sizes = tuple(args.batch_sizes)
    try:
        methods = all_methods(sizes)
    except ValueError as error:
        parser.error(str(error))
    baseline = baseline_provenance(*baseline_paths(args.baseline_library, args.baseline_sql, args.baseline_manifest))
    current_rust_version = args.current_rust_version or subprocess.check_output(["rustc", "--version"], text=True).strip()
    words = read_csv((args.data / "pinyin_words.csv").read_bytes())
    characters = read_csv((args.data / "pinyin_mapping.csv").read_bytes())
    corpus = make_corpus(words, characters, args.rows)
    expected = [(row["id"], row["expected"]) for row in corpus]
    report: dict[str, Any] = {
        "started_at_utc": datetime.now(timezone.utc).isoformat(), "environment_notes": args.environment_note,
        "runtime_git_sha": subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip(),
        "baseline": baseline, "benchmark_script_sha256": digest(Path(__file__).read_bytes()),
        "dictionary_sha256": {path.name: digest(path.read_bytes()) for path in sorted(args.data.glob("*.csv"))},
        "runtime_source_sha256": {str(path.relative_to(ROOT)): digest(path.read_bytes()) for path in
                                  [*sorted((ROOT / "src").glob("*.rs")), ROOT / "Cargo.toml", ROOT / "Cargo.lock",
                                   ROOT / "pg_pinyin--0.0.8--0.0.9.sql", ROOT / "test/prepare_upgrade.sh",
                                   ROOT / "docker/Dockerfile.test-pg19beta4-trixie",
                                   ROOT / "docker/patches/paradedb-0.26.1-pg19beta4.patch"]},
        "corpus_sha256": serialized_digest(corpus), "gold_sha256": serialized_digest(GOLD_DOCUMENTS),
        "client_environment": {"python": platform.python_version(), "psycopg": psycopg.__version__,
                               "architecture": platform.machine(), "available_cpu_count": os.cpu_count()},
        "configuration": {"rows": args.rows, "runs": args.runs, "batch_sizes": list(sizes), "methods": methods,
                          "extension_development_version": CURRENT_VERSION,
                          "latest_published_release": "0.0.8",
                          "comparison": "pre-optimization P1 versus current source; both unreleased 0.0.9"},
        "reference": verify_outputs(expected, expected, "source dictionary"),
        "generation_samples": {method: [] for method in methods},
        "insert_samples": {method: [] for method in methods}, "tokenizer_samples": [],
    }
    with temporary_database(args.pgurl, args.keep_database) as database:
        report["database"] = database
        report["environment"] = setup(args.pgurl, database, corpus, sizes, args.data, baseline,
                                      args.current_build_artifact, current_rust_version)
        require_versions(report["environment"], args.pg_search_version)
        print("[gold] independent documents, token arrays and NULL/document boundaries", flush=True)
        report["gold"] = check_gold(args.pgurl, database)
        for round_index in range(args.runs):
            print(f"[generation] round {round_index + 1}/{args.runs}", flush=True)
            for method in rotated_methods(round_index, sizes):
                result = measure_generation(args.pgurl, database, method, expected)
                report["generation_samples"][method].append(result)
                print(f"  {method:24s} cold {result['cold_ms']:10.3f} ms warm {result['warm_ms']:10.3f} ms", flush=True)
        for round_index in range(args.runs):
            print(f"[insert] round {round_index + 1}/{args.runs}", flush=True)
            for method in rotated_methods(round_index, sizes):
                result = measure_insert(args.pgurl, database, method, expected)
                report["insert_samples"][method].append(result)
                print(f"  {method:24s} cold {result['cold']['ms']:10.3f} ms warm {result['warm']['ms']:10.3f} ms", flush=True)
        for round_index in range(args.runs):
            print(f"[separate ICU tokenizer] round {round_index + 1}/{args.runs}", flush=True)
            report["tokenizer_samples"].append(measure_tokenizer(args.pgurl, database, len(corpus)))
        summarize(report)
        args.output_prefix.parent.mkdir(parents=True, exist_ok=True)
        args.output_prefix.with_suffix(".json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        args.output_prefix.with_suffix(".md").write_text(render_markdown(report), encoding="utf-8")
        print(f"[reports] {args.output_prefix.with_suffix('.md')} and .json", flush=True)


if __name__ == "__main__":
    main()
