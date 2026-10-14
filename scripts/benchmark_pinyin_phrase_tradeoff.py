#!/usr/bin/env python3
"""Compare default prefix regex, full regex, and native exact phrase retrieval.

Every invocation owns a fresh UUID database. Full-query latency includes server
query construction, BM25 retrieval, client round trips, and ranked top-k transfer.
Cold constructor timing means a new backend, not cold OS or shared-buffer caches.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import time
from typing import Any, Iterable

import psycopg


ROOT = Path(__file__).resolve().parents[1]
BASE_SPEC = importlib.util.spec_from_file_location(
    "native_phrase_benchmark", ROOT / "scripts/benchmark_pinyin_phrase.py"
)
BASE = importlib.util.module_from_spec(BASE_SPEC)
BASE_SPEC.loader.exec_module(BASE)
COMMON = BASE.COMMON
METHODS = (
    "rust_default", "rust_full", "rust_native",
    "sql_default", "sql_full", "sql_native",
)
STRATEGIES = ("default", "full", "native")
INTENTS = ("full", "initials", "autocomplete", "normalization")


def validate_fixture(fixture: dict[str, Any]) -> None:
    rows, cases = fixture.get("rows", []), fixture.get("cases", [])
    ids = {row["id"] for row in rows}
    if not ids or len(ids) != len(rows) or not cases:
        raise ValueError("Fixture requires nonempty uniquely identified rows and cases")
    labels = [case["label"] for case in cases]
    if len(set(labels)) != len(labels):
        raise ValueError("Fixture case labels must be unique")
    for row in rows:
        if not isinstance(row["name"], str) or not isinstance(row["plain"], str):
            raise ValueError("Fixture documents require name and plain text")
    for case in cases:
        positive, negative = set(case["positive_ids"]), set(case["negative_ids"])
        if positive & negative or positive | negative != ids:
            raise ValueError("Every gold case must exhaustively and disjointly label all fixture IDs")
        if len(positive) != len(case["positive_ids"]) or len(negative) != len(case["negative_ids"]):
            raise ValueError("Gold labels cannot contain duplicate IDs")
        if case["intent"] not in INTENTS or not isinstance(case["input"], str):
            raise ValueError("Unknown gold intent or non-text input")
        slope = case["slope"]
        if slope is not None and (isinstance(slope, bool) or not isinstance(slope, int) or slope < 0):
            raise ValueError("Gold slope must be NULL or a nonnegative integer")


def ratio(numerator: int | float, denominator: int | float) -> float | None:
    return numerator / denominator if denominator else None


def metrics_from_counts(tp: int, fp: int, fn: int, tn: int) -> dict[str, Any]:
    return {
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": ratio(tp, tp + fp), "recall": ratio(tp, tp + fn),
        "false_discovery_rate": ratio(fp, tp + fp),
        "false_positive_rate": ratio(fp, fp + tn),
    }


def confusion_metrics(returned_ids: Iterable[int], positive_ids: Iterable[int],
                      negative_ids: Iterable[int]) -> dict[str, Any]:
    returned, positive, negative = set(returned_ids), set(positive_ids), set(negative_ids)
    if positive & negative or not returned <= positive | negative:
        raise ValueError("Returned IDs must belong to an exhaustive disjoint oracle")
    return metrics_from_counts(len(returned & positive), len(returned & negative),
                               len(positive - returned), len(negative - returned))


def summarize_metrics(cases: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {key: sum(case["metrics"][key] for case in cases) for key in ("tp", "fp", "fn", "tn")}
    return {"cases": len(cases), **metrics_from_counts(**counts)}


def assert_report_paths_available(json_path: Path, markdown_path: Path) -> None:
    if json_path.resolve() == markdown_path.resolve():
        raise ValueError("JSON and Markdown outputs require distinct paths")
    for path in (json_path, markdown_path):
        if path.exists():
            raise ValueError(f"Refusing to overwrite an existing report: {path}")


def compute_scope(default_ids: Iterable[int], native_ids: Iterable[int]) -> dict[str, Any]:
    old, new = set(default_ids), set(native_ids)
    intersection, old_only, new_only = old & new, old - new, new - old
    return {
        "default_count": len(old), "native_count": len(new),
        "intersection_count": len(intersection),
        "default_only_count": len(old_only), "native_only_count": len(new_only),
        "intersection_ids": sorted(intersection),
        "default_only_ids": sorted(old_only), "native_only_ids": sorted(new_only),
    }


def rotated_methods(round_index: int) -> list[str]:
    shift = round_index % len(METHODS)
    return [*METHODS[shift:], *METHODS[:shift]]


def query_expression(method: str, value: str = "%s", slope: str = "%s") -> str:
    if method not in METHODS or value not in ("%s", "plain") or slope not in ("%s", "NULL"):
        raise ValueError("Unknown query method or expression")
    prefix = "sql_" if method.startswith("sql_") else ""
    strategy = method.split("_", 1)[1]
    if strategy == "native":
        return f"public.{prefix}pinyin_phrase({value},{slope})"
    if strategy == "full":
        return f"public.{prefix}pinyin_regex_phrase_mode({value},'full',{slope},NULL,false)"
    return f"public.{prefix}pinyin_regex_phrase({value},{slope},NULL,false)"


def search_statement(method: str, table: str = "bench_index") -> str:
    if table not in ("bench_index", "bench_gold"):
        raise ValueError("Unknown benchmark relation")
    return (
        f"SELECT id,pdb.score(id)::double precision AS score FROM {table} "
        f"WHERE plain @@@ {query_expression(method)} "
        "ORDER BY pdb.score(id) DESC,id LIMIT %s"
    )


def verification_statement(method: str) -> str:
    return (
        "WITH hits AS MATERIALIZED (SELECT id,pdb.score(id)::double precision AS score "
        f"FROM bench_index WHERE plain @@@ {query_expression(method)}) "
        "SELECT count(*),md5(coalesce(string_agg(id::text,',' ORDER BY id),'')),"
        "md5(coalesce(string_agg(id::text,',' ORDER BY score DESC,id),'')),"
        "md5(coalesce(string_agg(id::text || ':' || score::text,',' ORDER BY id),'')),"
        "coalesce(bool_or(id=%s),false),"
        "(SELECT coalesce(json_agg(r ORDER BY r.score DESC,r.id),'[]'::json) FROM "
        "(SELECT id,score FROM hits ORDER BY score DESC,id LIMIT %s) r),"
        "coalesce(array_agg(id ORDER BY id),'{}'::bigint[]) FROM hits"
    )


def setup(dsn: str, database: str, corpus: list[dict[str, Any]], data: Path,
          fixture: dict[str, Any]) -> dict[str, Any]:
    environment = BASE.setup(dsn, database, corpus, data)
    with BASE.connection(dsn, database) as conn, conn.cursor() as cursor:
        COMMON.configure(cursor)
        cursor.execute("DROP TABLE bench_gold CASCADE")
        cursor.execute("CREATE TABLE bench_gold (id bigint PRIMARY KEY,name text NOT NULL,plain text NOT NULL)")
        with cursor.copy("COPY bench_gold FROM STDIN") as copy:
            for row in fixture["rows"]:
                copy.write_row((row["id"], row["name"], row["plain"]))
        cursor.execute("CREATE INDEX bench_gold_bm25 ON bench_gold USING bm25 "
                       "(id,(plain::pdb.whitespace)) WITH (key_field='id')")
        cursor.execute("ANALYZE bench_gold")
        definitions = {}
        for method in METHODS:
            prefix = "sql_" if method.startswith("sql_") else ""
            strategy = method.split("_", 1)[1]
            if strategy == "native":
                signature = f"public.{prefix}pinyin_phrase(text,integer)"
            elif strategy == "full":
                signature = f"public.{prefix}pinyin_regex_phrase_mode(text,text,integer,integer,boolean)"
            else:
                signature = f"public.{prefix}pinyin_regex_phrase(text,integer,integer,boolean)"
            cursor.execute("SELECT pg_get_functiondef(%s::regprocedure)", (signature,))
            definition = cursor.fetchone()[0]
            definitions[method] = {
                "signature": signature, "sha256": COMMON.digest(definition.encode()),
                "definition": definition, "expression": query_expression(method),
            }
    environment["installed_query_functions"] = definitions
    return environment


def check_gold(dsn: str, database: str, fixture: dict[str, Any]) -> dict[str, Any]:
    outputs = {}
    with BASE.connection(dsn, database) as conn, conn.cursor() as cursor:
        COMMON.configure(cursor)
        for method in METHODS:
            cases = []
            for case in fixture["cases"]:
                cursor.execute(search_statement(method, "bench_gold"),
                               (case["input"], case["slope"], len(fixture["rows"])))
                returned = [[ident, score] for ident, score in cursor.fetchall()]
                ids = [row[0] for row in returned]
                metric = confusion_metrics(ids, case["positive_ids"], case["negative_ids"])
                if (method.split("_", 1)[1] in ("full", "native")
                        and case["intent"] in ("full", "normalization")
                        and (metric["fp"] or metric["fn"])):
                    raise RuntimeError(
                        f"Independent complete/normalized oracle failed: {method}/{case['label']}: {ids}"
                    )
                cases.append({**case, "returned": returned, "returned_ids": sorted(ids),
                              "metrics": metric,
                              "false_positive_ids": sorted(set(ids) & set(case["negative_ids"])),
                              "false_negative_ids": sorted(set(case["positive_ids"]) - set(ids))})
            outputs[method] = {"cases": cases, "sha256": COMMON.serialized_digest(cases)}
    for strategy in STRATEGIES:
        if outputs[f"rust_{strategy}"] != outputs[f"sql_{strategy}"]:
            raise RuntimeError(f"Gold Rust/SQL IDs/ranking/score parity failed: {strategy}")
    summarize_gold(outputs)
    return outputs


def summarize_gold(outputs: dict[str, Any]) -> None:
    for method in METHODS:
        per_intent = {}
        for intent in INTENTS:
            cases = [case for case in outputs[method]["cases"] if case["intent"] == intent]
            per_intent[intent] = summarize_metrics(cases)
        outputs[method]["per_intent_micro"] = per_intent
        outputs[method]["overall_micro"] = summarize_metrics(outputs[method]["cases"])


def constructor_statement(method: str) -> str:
    expression = query_expression(method, "plain", "NULL")
    return (
        "WITH constructed AS MATERIALIZED (SELECT id," + expression + "::text AS query "
        "FROM bench_corpus) SELECT count(*),sum(octet_length(query)),"
        "md5(string_agg(query,E'\\n' ORDER BY id)),"
        "count(*) FILTER (WHERE query IS NULL OR query='') FROM constructed"
    )


def measure_constructor(dsn: str, database: str, method: str) -> dict[str, Any]:
    statement = constructor_statement(method)
    with BASE.connection(dsn, database) as conn, conn.cursor() as cursor:
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
        raise RuntimeError(f"Empty query constructor output: {method}")
    if cold_output != warmup_output or warmup_output != warm_output:
        raise RuntimeError(f"Query constructor output drift: {method}")
    return {"cold_ms": cold_ms, "warm_ms": warm_ms, "rows": warm_output[0],
            "output_bytes": warm_output[1], "output_md5": warm_output[2], "empty_outputs": warm_output[3]}


def check_performance_results(dsn: str, database: str, queries: list[dict[str, Any]],
                              method: str, top_k: int) -> dict[str, Any]:
    outputs = []
    with BASE.connection(dsn, database) as conn, conn.cursor() as cursor:
        COMMON.configure(cursor)
        for query in queries:
            cursor.execute(verification_statement(method),
                           (query["inputs"]["full"], None, query["source_id"], top_k))
            count, ids_md5, rank_md5, score_md5, source_hit, top, ids = cursor.fetchone()
            if (count < 1 or not source_hit or query["source_id"] not in ids
                    or len(ids) != count or len(set(ids)) != count):
                raise RuntimeError(f"Source missing or invalid complete hitset: {method}/{query['source_id']}")
            outputs.append({"source_id": query["source_id"], "count": count, "ids": ids,
                            "ids_md5": ids_md5, "rank_md5": rank_md5, "score_md5": score_md5,
                            "top": [[row["id"], row["score"]] for row in top]})
        cursor.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + search_statement(method),
                       (queries[0]["inputs"]["full"], None, top_k))
        plan = cursor.fetchone()[0]
        BASE.verify_top_k_plan(plan)
    return {"queries": outputs, "all_results_sha256": COMMON.serialized_digest(outputs),
            "top_results_sha256": COMMON.serialized_digest([row["top"] for row in outputs]),
            "example_plan": plan}


def compare_results(results: dict[str, Any]) -> dict[str, Any]:
    for strategy in STRATEGIES:
        if results[f"rust_{strategy}"]["queries"] != results[f"sql_{strategy}"]["queries"]:
            raise RuntimeError(f"Rust/SQL complete ID/ranking/score parity failed: {strategy}")
    default = results["rust_default"]["queries"]
    full = results["rust_full"]["queries"]
    native = results["rust_native"]["queries"]
    if not default or len(default) != len(native) or len(full) != len(native):
        raise RuntimeError("Strategies must verify the same nonempty query workload")
    scopes = []
    changed_rank = changed_score = changed_top = 0
    control_rank = control_score = control_top = 0
    for old, control, new in zip(default, full, native):
        if len({old["source_id"], control["source_id"], new["source_id"]}) != 1:
            raise RuntimeError("Preflight strategy query identities differ")
        for item in (old, control, new):
            if not item["ids"] or len(set(item["ids"])) != item["count"] or not item["top"]:
                raise RuntimeError("Empty or inconsistent performance results invalidate timing")
        if set(control["ids"]) != set(new["ids"]):
            raise RuntimeError(f"Full regex/native exact hitsets differ: {old['source_id']}")
        scope = compute_scope(old["ids"], new["ids"])
        scopes.append({"source_id": old["source_id"], **scope})
        changed_rank += old["rank_md5"] != new["rank_md5"]
        changed_score += old["score_md5"] != new["score_md5"]
        changed_top += [row[0] for row in old["top"]] != [row[0] for row in new["top"]]
        control_rank += control["rank_md5"] != new["rank_md5"]
        control_score += control["score_md5"] != new["score_md5"]
        control_top += [row[0] for row in control["top"]] != [row[0] for row in new["top"]]
    total_old = sum(scope["default_count"] for scope in scopes)
    total_new = sum(scope["native_count"] for scope in scopes)
    shared = sum(scope["intersection_count"] for scope in scopes)
    return {
        "verified_queries": len(default), "scopes": scopes,
        "unchanged_hitset_queries": sum(not scope["default_only_count"] and not scope["native_only_count"]
                                          for scope in scopes),
        "changed_hitset_queries": sum(bool(scope["default_only_count"] or scope["native_only_count"])
                                        for scope in scopes),
        "default_hit_pairs": total_old, "native_hit_pairs": total_new,
        "intersection_hit_pairs": shared,
        "default_only_hit_pairs": sum(scope["default_only_count"] for scope in scopes),
        "native_only_hit_pairs": sum(scope["native_only_count"] for scope in scopes),
        "native_hits_as_fraction_of_default": ratio(total_new, total_old),
        "native_retention_of_default_hits": ratio(shared, total_old),
        "default_native_changed_ranking_queries": changed_rank,
        "default_native_changed_score_queries": changed_score,
        "default_native_changed_top_k_queries": changed_top,
        "full_native_all_hitsets_equal": True,
        "full_native_changed_ranking_queries": control_rank,
        "full_native_changed_score_queries": control_score,
        "full_native_changed_top_k_queries": control_top,
        "rust_sql_rank_and_score_equal_within_each_strategy": True,
    }


def add_scope_examples(comparison: dict[str, Any], corpus: list[dict[str, Any]],
                       queries: list[dict[str, Any]], results: dict[str, Any]) -> None:
    documents = {row["id"]: {key: row[key] for key in ("id", "name", "plain")} for row in corpus}
    inputs = {row["source_id"]: row["inputs"]["full"] for row in queries}
    old_top = {row["source_id"]: row["top"] for row in results["rust_default"]["queries"]}
    new_top = {row["source_id"]: row["top"] for row in results["rust_native"]["queries"]}
    differing = [scope for scope in comparison["scopes"]
                 if scope["default_only_count"] or scope["native_only_count"]]
    differing.sort(key=lambda scope: (-scope["default_only_count"], -scope["native_only_count"], scope["source_id"]))
    comparison["examples"] = [
        {"source_id": scope["source_id"], "input": inputs[scope["source_id"]],
         "source_document": documents[scope["source_id"]],
         "default_count": scope["default_count"], "native_count": scope["native_count"],
         "default_only_documents": [documents[ident] for ident in scope["default_only_ids"][:8]],
         "native_only_documents": [documents[ident] for ident in scope["native_only_ids"][:8]],
         "default_top": old_top[scope["source_id"]], "native_top": new_top[scope["source_id"]]}
        for scope in differing[:8]
    ]


def measure_search(dsn: str, database: str, queries: list[dict[str, Any]], method: str,
                   top_k: int, expected: list[dict[str, Any]]) -> dict[str, Any]:
    statement = search_statement(method)
    latencies, outputs = [], []
    with BASE.connection(dsn, database) as conn, conn.cursor() as cursor:
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
        raise RuntimeError(f"Timed ranked IDs/scores differ from preflight: {method}")
    return {"batch_ms": batch_ms, "per_query": COMMON.timings(latencies),
            "latencies_ms": latencies, "top_results_sha256": COMMON.serialized_digest(outputs)}


def summarize(report: dict[str, Any]) -> None:
    report["constructor"], report["search_timing"] = {}, {}
    scopes = report["comparison"]["scopes"]
    groups = {
        "unchanged_hitset": [index for index, scope in enumerate(scopes)
                             if not scope["default_only_count"] and not scope["native_only_count"]],
        "changed_hitset": [index for index, scope in enumerate(scopes)
                           if scope["default_only_count"] or scope["native_only_count"]],
    }
    for method in METHODS:
        constructors = report["constructor_samples"][method]
        if len({(item["rows"], item["output_bytes"], item["output_md5"]) for item in constructors}) != 1:
            raise RuntimeError(f"Constructor output changed across repeats: {method}")
        report["constructor"][method] = {
            "cold": COMMON.timings([item["cold_ms"] for item in constructors]),
            "warm": COMMON.timings([item["warm_ms"] for item in constructors]),
            "output_bytes": constructors[0]["output_bytes"], "output_md5": constructors[0]["output_md5"],
        }
        searches = report["search_samples"][method]
        expected_hash = report["performance_results"][method]["top_results_sha256"]
        if any(item["top_results_sha256"] != expected_hash for item in searches):
            raise RuntimeError(f"Timed top-k fingerprint drift: {method}")
        subgroup = {}
        for label, positions in groups.items():
            batches = [sum(item["latencies_ms"][index] for index in positions) for item in searches]
            subgroup[label] = {
                "queries": len(positions), "query_latency_sum": COMMON.timings(batches),
                "median_per_query_ms": (statistics.median(
                    statistics.median(item["latencies_ms"][index] for index in positions)
                    for item in searches) if positions else None),
            }
        report["search_timing"][method] = {
            "batch": COMMON.timings([item["batch_ms"] for item in searches]),
            "query_p50_ms": statistics.median(item["per_query"]["median_ms"] for item in searches),
            "query_p95_ms": statistics.median(item["per_query"]["p95_ms"] for item in searches),
            "top_results_sha256": expected_hash, "hitset_subgroups": subgroup,
        }
    index_samples = report["index_samples"]
    stable = ("rows", "plain_field_bytes", "position_tokens", "field_md5")
    for key in stable:
        if len({sample[key] for sample in index_samples}) != 1:
            raise RuntimeError(f"Fresh index inputs differ across builds: {key}")
    report["index"] = {key: index_samples[0][key] for key in stable}
    for key in ("table_relation_bytes", "table_total_bytes", "index_relation_bytes",
                "index_total_bytes", "database_bytes"):
        report["index"][key] = int(statistics.median(sample[key] for sample in index_samples))
    report["index"]["build"] = COMMON.timings([sample["build_ms"] for sample in index_samples])
    report["latency_comparison"] = {}
    for backend in ("rust", "sql"):
        native = report["search_timing"][f"{backend}_native"]["batch"]["median_ms"]
        changes = {}
        for strategy in ("default", "full"):
            reference = report["search_timing"][f"{backend}_{strategy}"]["batch"]["median_ms"]
            changes[f"native_elapsed_reduction_vs_{strategy}"] = ratio(reference - native, reference)
        report["latency_comparison"][backend] = changes


def percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.2f}%"


def md_text(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_markdown(report: dict[str, Any]) -> str:
    config, environment, comparison = report["configuration"], report["environment"], report["comparison"]
    lines = [
        "# Default Regex vs Exact Pinyin Phrase Tradeoff", "",
        f"- Runtime base HEAD: `{report['runtime_git_sha']}`; working tree dirty: `{report['runtime_git_worktree_dirty']}`. Exact source, fixture, corpus, script and installed-library hashes are retained in JSON.",
        f"- PostgreSQL: `{environment['postgresql']}`; extensions: `{json.dumps(environment['extensions'], sort_keys=True)}`.",
        f"- Fresh UUID database; {config['rows']:,} reproducible dictionary-derived rows; {config['queries']:,} identical complete queries ({config['query_syllable_count_min']}-{config['query_syllable_count_max']} syllables); {config['repeats']} rotated repeats; top-k {config['top_k']}.",
        "- Six independent public APIs: Rust and SQL default prefix regex, full regex control, and native exact term/phrase. Generated/pipe fields are not part of this plain-whitespace-index comparison.",
        "- Default means the original `pinyin_regex_phrase(value,slope,NULL,false)`, not mode `full`. All strategies use the same installed extension library/dictionary and one shared index. Full and native share the full parser; default retains its legacy compatibility grammar.",
        "- Full-client latency includes query construction, BM25 matching/scoring, round trips and ranked top-k transfer; connection startup, setup, complete-hitset checks, gold evaluation and EXPLAIN are not timed. Every measured method warms its entire workload first.",
        "- All six recorded performance plans must use real `TopKScanExecState` on the shared BM25 index. Timed ranked IDs and scores must exactly match untimed verification for that method.",
        "- Constructor timing materializes actual `pdb.query::text` values for every corpus row. Cold is a fresh PostgreSQL backend, not cold OS storage or shared buffers.",
        "- Every sampled query transfers all matching IDs during preflight. Differences below are computed from actual ID intersections, not just top-k, counts or hashes. Rust/SQL IDs, ranking and scores must agree within each strategy; full-regex/native complete hitset equality is required.",
        "- Hand-labeled gold is separate from dictionary-derived performance data. Each case exhaustively labels every gold document for its stated intent; corpus scope differences are not automatically classified as errors or measured recall.",
        *[f"- Run condition: {note}" for note in report.get("environment_notes", [])],
        "", "## Complete-Query Hit Scope", "",
        f"Of {comparison['verified_queries']:,} complete inputs, {comparison['changed_hitset_queries']:,} changed full hitsets and {comparison['unchanged_hitset_queries']:,} did not.",
        "", "| Query/document pairs | Count |", "| --- | ---: |",
        f"| Default regex matches | {comparison['default_hit_pairs']:,} |",
        f"| Native exact matches | {comparison['native_hit_pairs']:,} |",
        f"| Intersection | {comparison['intersection_hit_pairs']:,} |",
        f"| Default only | {comparison['default_only_hit_pairs']:,} |",
        f"| Native only | {comparison['native_only_hit_pairs']:,} |", "",
        f"Native retained {percent(comparison['native_retention_of_default_hits'])} of default matches in this sampled workload. This is scope retention, not relevance recall: additional prefix matches require an intent-specific oracle before they can be called true or false positives.",
        f"Default/native changed ranking for {comparison['default_native_changed_ranking_queries']:,} queries, scores for {comparison['default_native_changed_score_queries']:,}, and top-k IDs for {comparison['default_native_changed_top_k_queries']:,}.",
        f"The full-regex control and native path have identical full hitsets; {comparison['full_native_changed_ranking_queries']:,} queries changed ranking, {comparison['full_native_changed_score_queries']:,} changed scores, and {comparison['full_native_changed_top_k_queries']:,} changed top-k IDs.",
        "Singleton term scoring need not equal constant-score regex scoring; neither hitset parity nor this sampled multi-syllable workload proves universal ranking equivalence.",
        "", "### Representative Scope Differences", "",
        "| Input | Default/native hits | Default-only documents (ID: name; plain tokens) |",
        "| --- | ---: | --- |",
    ]
    for item in comparison.get("examples", []):
        documents = "<br>".join(f"{row['id']}: {md_text(row['name'])}; `{md_text(row['plain'])}`"
                               for row in item["default_only_documents"])
        lines.append(f"| `{md_text(item['input'])}` | {item['default_count']}/{item['native_count']} | {documents or '(none)'} |")
    if not comparison.get("examples"):
        lines.append("| (no differences) | | |")
    lines += [
        "", "## Intent-Labeled Accuracy", "",
        "Micro-averages sum query/document TP, FP, FN and TN within each intent, not top-k. `FDR = FP/(TP+FP)` is the false-match fraction of returned results; `FPR = FP/(FP+TN)` is the false-positive fraction of labeled negatives. Undefined denominators are `n/a`, not zero.",
        "Full and normalization intents are kept separate from initials/autocomplete. Native exact is not an initials/completion feature; empty native responses to partial input are counted as false negatives for those intents and are not included in complete-query speedup timing.",
        "Rust/SQL gold outputs, IDs, rankings and scores agree within each strategy. The following shows the Rust rows once; JSON retains all six methods and every returned ID.", "",
        "| Intent | Strategy | Cases | TP | FP | FN | TN | Precision | Recall | FDR | FPR |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for intent in INTENTS:
        for strategy in STRATEGIES:
            item = report["gold"][f"rust_{strategy}"]["per_intent_micro"][intent]
            lines.append(f"| {intent} | {strategy} | {item['cases']} | {item['tp']} | {item['fp']} | {item['fn']} | {item['tn']} | {percent(item['precision'])} | {percent(item['recall'])} | {percent(item['false_discovery_rate'])} | {percent(item['false_positive_rate'])} |")
    lines += ["", "### All Gold Returned IDs", "",
              "Every gold case uses independently stated positive/negative labels; the full list is retained in JSON and the fixture.", "",
              "| Intent/input | Relevant IDs | Default returned | Full returned | Native returned |",
              "| --- | --- | --- | --- | --- |"]
    methods = [report["gold"][f"rust_{strategy}"]["cases"] for strategy in STRATEGIES]
    for default, full, native in zip(*methods):
        values = [",".join(map(str, case["returned_ids"])) or "(none)" for case in (default, full, native)]
        positive = ",".join(map(str, default["positive_ids"])) or "(none)"
        lines.append(f"| {default['intent']}: `{md_text(default['input'])}` | {positive} | {' | '.join(values)} |")
    lines += ["", "## End-to-End Warm Indexed Performance", "",
              "Only identical complete query inputs are timed; each source phrase must occur in every strategy's actual full hitset.", "",
              "| Method | Batch ms | Query p50 ms | Query p95 ms |",
              "| --- | ---: | ---: | ---: |"]
    for method in METHODS:
        item = report["search_timing"][method]
        lines.append(f"| {method} | {item['batch']['median_ms']:,.3f} | {item['query_p50_ms']:.3f} | {item['query_p95_ms']:.3f} |")
    lines += ["", "| Backend | Native elapsed reduction vs default | Native elapsed reduction vs full control |",
              "| --- | ---: | ---: |"]
    for backend in ("rust", "sql"):
        item = report["latency_comparison"][backend]
        lines.append(f"| {backend} | {percent(item['native_elapsed_reduction_vs_default'])} | {percent(item['native_elapsed_reduction_vs_full'])} |")
    lines += ["", "Positive reductions mean lower native elapsed time; negative reductions mean slower. The full control isolates native construction/retrieval changes without narrowing complete hitsets. The default comparison combines that change with prefix-expansion and result-scope differences; it cannot attribute all speedup to an implementation improvement.",
              "", "### Unchanged vs Changed Hitsets", "",
              "These are post-stratified sums of measured per-query latency, not separate timed batches or causal estimates. The same queries appear in each strategy for a subgroup.", "",
              "| Hitset subgroup | Method | Queries | Latency sum median ms | Per-query median ms |",
              "| --- | --- | ---: | ---: | ---: |"]
    for label in ("unchanged_hitset", "changed_hitset"):
        for method in METHODS:
            item = report["search_timing"][method]["hitset_subgroups"][label]
            median = item["median_per_query_ms"]
            lines.append(f"| {label} | {method} | {item['queries']:,} | {item['query_latency_sum']['median_ms']:,.3f} | {median:.3f} |" if median is not None else f"| {label} | {method} | 0 | n/a | n/a |")
    lines += ["", "## Actual Query Construction", "",
              "Milliseconds for the complete corpus, median over fresh-backend samples; not BM25 search.", "",
              "| Method | Cold ms | Warm ms | Materialized bytes |", "| --- | ---: | ---: | ---: |"]
    for method in METHODS:
        item = report["constructor"][method]
        lines.append(f"| {method} | {item['cold']['median_ms']:,.3f} | {item['warm']['median_ms']:,.3f} | {item['output_bytes']:,} |")
    lines += ["", "## Shared Index", "",
              f"The plain whitespace BM25 index is shared by all methods: {report['index']['index_relation_bytes']:,} bytes; {report['index']['position_tokens']:,} tokens. Median fresh rebuild: {report['index']['build']['median_ms']:,.3f} ms. Database total includes dictionaries and the separate gold index.",
              "No index-size improvement is expected from substituting a query constructor; this run does not change indexed values.",
              "", "## Interpretation Limits", "",
              "- Gold is a small, deliberately contrastive fixture, not representative production prevalence or a broad pronunciation/relevance benchmark.",
              "- Performance inputs are deterministic dictionary readings with known source matches. Extra prefix matches are scope differences; only independent gold defines mis-matches.",
              "- Native exact preserves complete-syllable intent while removing prefix leakage, but is not a drop-in replacement for initials or completion. Input normalization can also add native matches that the ASCII-only default rejects.",
              "- Latency includes loopback client overhead and varies with the shared host/cache environment. Repeated rotated samples expose variance but do not establish statistical significance or a general speedup guarantee.",
              "- JSON retains full ID sets, rank/score fingerprints, top-k results, per-query/run timings, gold labels, installed function definitions, real Top-K plans and runtime/source hashes.", ""]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", required=True, help="Administrative DSN allowed to create an isolated database")
    parser.add_argument("--data", type=Path, default=ROOT / "sql/data")
    parser.add_argument("--fixture", type=Path, default=ROOT / "test/fixtures/pinyin_phrase_tradeoff.json")
    parser.add_argument("--rows", type=int, default=20000)
    parser.add_argument("--queries", type=int, default=2000)
    parser.add_argument("--repeats", type=int, default=6)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--pg-search-version", default="0.26.1")
    parser.add_argument("--postgresql-version-pattern", default="19beta4")
    parser.add_argument("--environment-note", action="append", default=[])
    parser.add_argument("--json-output", type=Path, default=ROOT / "benchmarks/native_phrase_tradeoff.json")
    parser.add_argument("--markdown-output", type=Path, default=ROOT / "benchmarks/native_phrase_tradeoff.md")
    args = parser.parse_args()
    fixture_bytes = args.fixture.read_bytes()
    fixture = json.loads(fixture_bytes)
    try:
        BASE.validate_configuration(args.rows, args.queries, args.repeats, args.top_k)
        validate_fixture(fixture)
        assert_report_paths_available(args.json_output, args.markdown_output)
    except ValueError as error:
        parser.error(str(error))
    corpus = COMMON.make_corpus(COMMON.read_words((args.data / "pinyin_words.csv").read_bytes()), args.rows)
    queries = COMMON.make_queries(corpus, args.queries)
    query_syllable_counts = [len(query["inputs"]["full"].split()) for query in queries]
    source_paths = [*sorted((ROOT / "src").glob("*.rs")), ROOT / "Cargo.lock", ROOT / "sql/word.sql",
                    ROOT / "sql/pinyin.sql", ROOT / "pg_pinyin--0.0.8--0.0.9.sql",
                    ROOT / "docker/Dockerfile.test-pg19beta4-trixie",
                    ROOT / "docker/patches/paradedb-0.26.1-pg19beta4.patch"]
    report: dict[str, Any] = {
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "runtime_git_sha": subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip(),
        "runtime_git_worktree_dirty": bool(subprocess.check_output(
            ["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=no",
             "--ignore-submodules=all"], text=True).strip()),
        "benchmark_script_sha256": COMMON.digest(Path(__file__).read_bytes()),
        "base_benchmark_script_sha256": COMMON.digest((ROOT / "scripts/benchmark_pinyin_phrase.py").read_bytes()),
        "shared_benchmark_utilities_sha256": COMMON.digest((ROOT / "scripts/benchmark_pinyin_query_modes.py").read_bytes()),
        "fixture_sha256": COMMON.digest(fixture_bytes), "fixture": fixture,
        "corpus_sha256": COMMON.serialized_digest(corpus), "queries_sha256": COMMON.serialized_digest(queries),
        "dictionary_sha256": {path.name: COMMON.digest(path.read_bytes()) for path in sorted(args.data.glob("*.csv"))},
        "runtime_source_sha256": {str(path.relative_to(ROOT)): COMMON.digest(path.read_bytes()) for path in source_paths},
        "client_environment": {"python": platform.python_version(), "psycopg": psycopg.__version__,
                               "architecture": platform.machine(), "available_cpu_count": os.cpu_count()},
        "configuration": {"rows": args.rows, "requested_queries": args.queries, "queries": len(queries),
                          "repeats": args.repeats, "top_k": args.top_k, "methods": list(METHODS),
                          "query_syllable_count_min": min(query_syllable_counts),
                          "query_syllable_count_max": max(query_syllable_counts),
                          "storage": "plain", "requested_pg_search_version": args.pg_search_version,
                          "requested_postgresql_version_pattern": args.postgresql_version_pattern,
                          "rotated_method_orders": [rotated_methods(index) for index in range(args.repeats)]},
        "environment_notes": args.environment_note, "performance_queries": queries,
        "constructor_samples": {method: [] for method in METHODS},
        "search_samples": {method: [] for method in METHODS}, "index_samples": [],
        "performance_results": {},
    }
    with BASE.temporary_database(args.dsn) as database:
        report["database"] = database
        report["environment"] = setup(args.dsn, database, corpus, args.data, fixture)
        if report["environment"]["extensions"].get("pg_search") != args.pg_search_version:
            raise RuntimeError(f"Expected pg_search {args.pg_search_version}; got {report['environment']['extensions']}")
        if args.postgresql_version_pattern not in report["environment"]["postgresql"]:
            raise RuntimeError(f"Expected PostgreSQL {args.postgresql_version_pattern}; got {report['environment']['postgresql']}")
        print(f"[gold] {len(fixture['rows'])} rows / {len(fixture['cases'])} exhaustive intent-labeled cases", flush=True)
        report["gold"] = check_gold(args.dsn, database, fixture)
        for repeat in range(args.repeats):
            for method in rotated_methods(repeat):
                result = measure_constructor(args.dsn, database, method)
                report["constructor_samples"][method].append(result)
                print(f"[constructor {repeat + 1}/{args.repeats}] {method} cold={result['cold_ms']:.3f} warm={result['warm_ms']:.3f} ms", flush=True)
            result = BASE.build_index(args.dsn, database)
            report["index_samples"].append(result)
            print(f"[fresh index {repeat + 1}/{args.repeats}] {result['build_ms']:.3f} ms, {result['index_relation_bytes']:,} bytes", flush=True)
        for method in METHODS:
            print(f"[all actual hitset preflight] {method}, {len(queries):,} queries", flush=True)
            report["performance_results"][method] = check_performance_results(args.dsn, database, queries, method, args.top_k)
        report["comparison"] = compare_results(report["performance_results"])
        add_scope_examples(report["comparison"], corpus, queries, report["performance_results"])
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
