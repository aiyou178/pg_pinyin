#!/usr/bin/env python3
"""Compare opt-in auto routing with unchanged default regex and exact phrase.

Every run owns a fresh database and rebuilds one shared plain-token BM25 index
for each timing round. Partial-input exact phrase results are evaluated, but are
not timed as if empty responses were successful initials or completion searches.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import importlib.util
import json
import math
import os
from pathlib import Path
import platform
import statistics
import subprocess
import time
from typing import Any, Iterable

import psycopg


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "auto_tradeoff_common", ROOT / "scripts/benchmark_pinyin_phrase_tradeoff.py"
)
TRADEOFF = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TRADEOFF)
BASE, COMMON = TRADEOFF.BASE, TRADEOFF.COMMON
METHODS = (
    "rust_default", "rust_auto", "rust_native",
    "sql_default", "sql_auto", "sql_native",
)
STRATEGIES = ("default", "auto", "native")
WORKLOADS = ("full", "initials", "autocomplete")
INTENTS = TRADEOFF.INTENTS


def canonical_ranked_rows(rows: Iterable[Iterable[Any]]) -> list[list[Any]]:
    # PostgreSQL JSON renders integral float scores as ints; the cursor does not.
    normalized, seen = [], set()
    for ident, score in rows:
        if type(ident) is not int or type(score) not in (int, float):
            raise ValueError("Ranked results require integer IDs and numeric scores")
        if ident in seen:
            raise ValueError("Ranked results cannot contain duplicate IDs")
        seen.add(ident)
        numeric_score = float(score)
        if not math.isfinite(numeric_score):
            raise ValueError("Ranked results require finite scores")
        normalized.append([ident, 0.0 if numeric_score == 0 else numeric_score])
    return normalized


def rotated_methods(round_index: int) -> list[str]:
    shift = round_index % len(METHODS)
    return [*METHODS[shift:], *METHODS[:shift]]


def timed_methods(workload: str) -> tuple[str, ...]:
    if workload not in WORKLOADS:
        raise ValueError("Unknown benchmark workload")
    return METHODS if workload == "full" else tuple(
        method for method in METHODS if not method.endswith("native")
    )


def query_expression(method: str, value: str = "%s", slope: str = "%s") -> str:
    if method not in METHODS or value not in ("%s", "plain") or slope not in ("%s", "NULL"):
        raise ValueError("Unknown query method or expression")
    prefix = "sql_" if method.startswith("sql_") else ""
    strategy = method.split("_", 1)[1]
    if strategy == "native":
        return f"public.{prefix}pinyin_phrase({value},{slope})"
    if strategy == "auto":
        return f"public.{prefix}pinyin_regex_phrase_mode({value},'auto',{slope},NULL,false)"
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


def query_route(query_text: str) -> str:
    """Read the installed constructor's serialized query, not a guessed parse."""
    value = json.loads(query_text)
    if isinstance(value, str) and value.lower() in ("empty", "all"):
        return value.lower()

    def keys(node: Any) -> Iterable[str]:
        if isinstance(node, dict):
            for key, child in node.items():
                yield "".join(character for character in key.lower() if character.isalnum())
                yield from keys(child)
        elif isinstance(node, list):
            for child in node:
                yield from keys(child)

    found = set(keys(value))
    for route, names in (
        ("phrase_prefix", {"phraseprefix"}),
        ("regex", {"regex", "regexphrase"}),
        ("phrase", {"phrase", "phrasearray"}),
        ("term", {"term"}),
        ("empty", {"empty"}),
    ):
        if found & names:
            return route
    raise ValueError(f"Unrecognized installed pdb.query serialization: {query_text}")


def setup(dsn: str, database: str, corpus: list[dict[str, Any]], data: Path,
          fixture: dict[str, Any]) -> dict[str, Any]:
    environment = TRADEOFF.setup(dsn, database, corpus, data, fixture)
    with BASE.connection(dsn, database) as conn, conn.cursor() as cursor:
        definitions = {}
        for method in METHODS:
            prefix = "sql_" if method.startswith("sql_") else ""
            strategy = method.split("_", 1)[1]
            if strategy == "native":
                signature = f"public.{prefix}pinyin_phrase(text,integer)"
            elif strategy == "auto":
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
                returned = canonical_ranked_rows(cursor.fetchall())
                ids = [row[0] for row in returned]
                metric = TRADEOFF.confusion_metrics(ids, case["positive_ids"], case["negative_ids"])
                if (method.endswith("native") and case["intent"] in ("full", "normalization")
                        and (metric["fp"] or metric["fn"])):
                    raise RuntimeError(f"Native complete gold failed: {method}/{case['label']}: {ids}")
                routing = {}
                if method.endswith("auto"):
                    cursor.execute("SELECT " + query_expression(method) + "::text",
                                   (case["input"], case["slope"]))
                    text = cursor.fetchone()[0]
                    routing = {"route": query_route(text), "constructed_query": text}
                cases.append({**case, **routing, "returned": returned, "returned_ids": sorted(ids),
                              "metrics": metric,
                              "false_positive_ids": sorted(set(ids) & set(case["negative_ids"])),
                              "false_negative_ids": sorted(set(case["positive_ids"]) - set(ids))})
            outputs[method] = {"cases": cases, "sha256": COMMON.serialized_digest(cases)}
    for strategy in STRATEGIES:
        if outputs[f"rust_{strategy}"] != outputs[f"sql_{strategy}"]:
            raise RuntimeError(f"Gold Rust/SQL IDs/ranking/score/query parity failed: {strategy}")
    for method in METHODS:
        outputs[method]["per_intent_micro"] = {
            intent: TRADEOFF.summarize_metrics([
                case for case in outputs[method]["cases"] if case["intent"] == intent
            ]) for intent in INTENTS
        }
        outputs[method]["overall_micro"] = TRADEOFF.summarize_metrics(outputs[method]["cases"])
    return outputs


def constructor_statement(method: str) -> str:
    return (
        "WITH constructed AS MATERIALIZED (SELECT id," + query_expression(method, "plain", "NULL")
        + "::text AS query FROM bench_corpus) SELECT count(*),sum(octet_length(query)),"
        "md5(string_agg(query,E'\\n' ORDER BY id)),"
        "count(*) FILTER (WHERE query IS NULL OR query='') FROM constructed"
    )


def measure_constructor(dsn: str, database: str, method: str) -> dict[str, Any]:
    statement = constructor_statement(method)
    with BASE.connection(dsn, database) as conn, conn.cursor() as cursor:
        COMMON.configure(cursor)
        start = time.perf_counter()
        cursor.execute(statement)
        cold = list(cursor.fetchone())
        cold_ms = (time.perf_counter() - start) * 1000
        cursor.execute(statement)
        warmup = list(cursor.fetchone())
        start = time.perf_counter()
        cursor.execute(statement)
        warm = list(cursor.fetchone())
        warm_ms = (time.perf_counter() - start) * 1000
    if not cold[0] or cold[3] or not cold[1] or cold != warmup or warmup != warm:
        raise RuntimeError(f"Empty or unstable materialized constructor: {method}")
    return {"cold_ms": cold_ms, "warm_ms": warm_ms, "rows": warm[0],
            "output_bytes": warm[1], "output_md5": warm[2], "empty_outputs": warm[3]}


def check_performance_results(dsn: str, database: str, queries: list[dict[str, Any]],
                              method: str, workload: str, top_k: int) -> dict[str, Any]:
    if workload not in WORKLOADS:
        raise ValueError("Unknown benchmark workload")
    outputs, routes = [], []
    with BASE.connection(dsn, database) as conn, conn.cursor() as cursor:
        COMMON.configure(cursor)
        for query in queries:
            value = query["inputs"][workload]
            cursor.execute(verification_statement(method), (value, None, query["source_id"], top_k))
            count, ids_md5, rank_md5, score_md5, source_hit, top, ids = cursor.fetchone()
            if len(ids) != count or len(set(ids)) != count or source_hit != (query["source_id"] in ids):
                raise RuntimeError(f"Inconsistent verified hitset: {method}/{workload}/{query['source_id']}")
            if workload == "full" and method.endswith(("default", "native")) and not source_hit:
                raise RuntimeError(f"Required source match missing: {method}/{workload}/{query['source_id']}")
            row = {"source_id": query["source_id"], "input": value, "source_hit": source_hit,
                   "count": count, "ids": ids, "ids_md5": ids_md5, "rank_md5": rank_md5,
                   "score_md5": score_md5,
                   "top": canonical_ranked_rows((item["id"], item["score"]) for item in top)}
            if method.endswith("auto"):
                cursor.execute("SELECT " + query_expression(method) + "::text", (value, None))
                text = cursor.fetchone()[0]
                row.update(route=query_route(text), constructed_query=text)
                routes.append(row["route"])
            outputs.append(row)
        plan = None
        if method in timed_methods(workload):
            cursor.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + search_statement(method),
                           (queries[0]["inputs"][workload], None, top_k))
            plan = cursor.fetchone()[0]
            BASE.verify_top_k_plan(plan)
    matched = sum(row["source_hit"] for row in outputs)
    return {"queries": outputs, "all_results_sha256": COMMON.serialized_digest(outputs),
            "top_results_sha256": COMMON.serialized_digest([row["top"] for row in outputs]),
            "source_hit_queries": matched, "source_hit_fraction": TRADEOFF.ratio(matched, len(outputs)),
            "empty_hitset_queries": sum(not row["count"] for row in outputs),
            "route_counts": dict(sorted(Counter(routes).items())), "example_plan": plan}


def scope_comparison(reference: list[dict[str, Any]], candidate: list[dict[str, Any]]) -> dict[str, Any]:
    if not reference or len(reference) != len(candidate):
        raise RuntimeError("Strategies must verify the same nonempty query workload")
    scopes = []
    changed_rank = changed_score = changed_top = 0
    for old, new in zip(reference, candidate):
        if old["source_id"] != new["source_id"] or old["input"] != new["input"]:
            raise RuntimeError("Preflight strategy query identities differ")
        for row in (old, new):
            if len(row["ids"]) != row["count"] or len(set(row["ids"])) != row["count"]:
                raise RuntimeError("Inconsistent actual ID lists invalidate scope comparisons")
        scopes.append({"source_id": old["source_id"], "input": old["input"],
                       **TRADEOFF.compute_scope(old["ids"], new["ids"])})
        changed_rank += old["rank_md5"] != new["rank_md5"]
        changed_score += old["score_md5"] != new["score_md5"]
        changed_top += [row[0] for row in old["top"]] != [row[0] for row in new["top"]]
    reference_pairs = sum(scope["default_count"] for scope in scopes)
    candidate_pairs = sum(scope["native_count"] for scope in scopes)
    intersection = sum(scope["intersection_count"] for scope in scopes)
    return {
        "verified_queries": len(scopes), "scopes": scopes,
        "reference_hit_pairs": reference_pairs, "candidate_hit_pairs": candidate_pairs,
        "intersection_hit_pairs": intersection,
        "reference_only_hit_pairs": sum(scope["default_only_count"] for scope in scopes),
        "candidate_only_hit_pairs": sum(scope["native_only_count"] for scope in scopes),
        "scope_retention": TRADEOFF.ratio(intersection, reference_pairs),
        "changed_hitset_queries": sum(bool(scope["default_only_count"] or scope["native_only_count"])
                                        for scope in scopes),
        "changed_ranking_queries": changed_rank, "changed_score_queries": changed_score,
        "changed_top_k_queries": changed_top,
    }


def compare_results(results: dict[str, Any]) -> dict[str, Any]:
    comparisons = {}
    for workload in WORKLOADS:
        methods = results[workload]
        for strategy in STRATEGIES:
            if methods[f"rust_{strategy}"]["queries"] != methods[f"sql_{strategy}"]["queries"]:
                raise RuntimeError(f"Rust/SQL ID/ranking/score/query parity failed: {workload}/{strategy}")
        reference = methods["rust_default"]["queries"]
        comparisons[workload] = {
            strategy: scope_comparison(reference, methods[f"rust_{strategy}"]["queries"])
            for strategy in ("auto", "native")
        }
        comparisons[workload]["rust_sql_rank_and_score_equal_within_each_strategy"] = True
    return comparisons


def source_coverage(reference: list[dict[str, Any]], candidate: list[dict[str, Any]]) -> dict[str, Any]:
    if not reference or len(reference) != len(candidate):
        raise RuntimeError("Source coverage requires aligned nonempty query workloads")
    gained = lost = 0
    for old, new in zip(reference, candidate):
        if old["source_id"] != new["source_id"] or old["input"] != new["input"]:
            raise RuntimeError("Source coverage query identities differ")
        gained += new["source_hit"] and not old["source_hit"]
        lost += old["source_hit"] and not new["source_hit"]
    relation = "mixed" if gained and lost else "improved" if gained else "reduced" if lost else "same"
    return {"equivalent_source_coverage": not gained and not lost,
            "source_coverage_relation": relation, "source_hits_gained_vs_default": gained,
            "source_hits_lost_vs_default": lost}


def measure_search(dsn: str, database: str, queries: list[dict[str, Any]], method: str,
                   workload: str, top_k: int, expected: list[dict[str, Any]]) -> dict[str, Any]:
    if method not in timed_methods(workload):
        raise ValueError("Exact native partial responses are quality checks, not successful-search timing")
    statement = search_statement(method)
    latencies, outputs = [], []
    with BASE.connection(dsn, database) as conn, conn.cursor() as cursor:
        COMMON.configure(cursor)
        for query in queries:
            cursor.execute(statement, (query["inputs"][workload], None, top_k))
            cursor.fetchall()
        batch_start = time.perf_counter()
        for query in queries:
            start = time.perf_counter()
            cursor.execute(statement, (query["inputs"][workload], None, top_k))
            outputs.append(canonical_ranked_rows(cursor.fetchall()))
            latencies.append((time.perf_counter() - start) * 1000)
        batch_ms = (time.perf_counter() - batch_start) * 1000
    if outputs != [row["top"] for row in expected]:
        raise RuntimeError(f"Timed ranked IDs/scores differ from preflight: {workload}/{method}")
    return {"batch_ms": batch_ms, "per_query": COMMON.timings(latencies),
            "latencies_ms": latencies, "top_results_sha256": COMMON.serialized_digest(outputs)}


def summarize(report: dict[str, Any]) -> None:
    report["constructor"], report["search_timing"], report["latency_comparison"] = {}, {}, {}
    for method in METHODS:
        samples = report["constructor_samples"][method]
        if len({(item["rows"], item["output_bytes"], item["output_md5"]) for item in samples}) != 1:
            raise RuntimeError(f"Constructor output changed across repeats: {method}")
        report["constructor"][method] = {
            "cold": COMMON.timings([item["cold_ms"] for item in samples]),
            "warm": COMMON.timings([item["warm_ms"] for item in samples]),
            "output_bytes": samples[0]["output_bytes"], "output_md5": samples[0]["output_md5"],
        }
    for workload in WORKLOADS:
        report["search_timing"][workload] = {}
        report["latency_comparison"][workload] = {}
        for method in timed_methods(workload):
            samples = report["search_samples"][workload][method]
            verified = report["performance_results"][workload][method]
            if any(sample["top_results_sha256"] != verified["top_results_sha256"] for sample in samples):
                raise RuntimeError(f"Timed top-k fingerprint drift: {workload}/{method}")
            report["search_timing"][workload][method] = {
                "batch": COMMON.timings([item["batch_ms"] for item in samples]),
                "query_p50_ms": statistics.median(item["per_query"]["median_ms"] for item in samples),
                "query_p95_ms": statistics.median(item["per_query"]["p95_ms"] for item in samples),
                "top_results_sha256": verified["top_results_sha256"],
                "source_hit_queries": verified["source_hit_queries"],
                "source_hit_fraction": verified["source_hit_fraction"],
                **source_coverage(
                    report["performance_results"][workload][method.split("_", 1)[0] + "_default"]["queries"],
                    verified["queries"],
                ),
            }
        for backend in ("rust", "sql"):
            reference = report["search_timing"][workload][f"{backend}_default"]["batch"]["median_ms"]
            auto = report["search_timing"][workload][f"{backend}_auto"]["batch"]["median_ms"]
            report["latency_comparison"][workload][backend] = {
                "auto_elapsed_reduction_vs_default": TRADEOFF.ratio(reference - auto, reference),
                "auto_equivalent_source_coverage": report["search_timing"][workload][f"{backend}_auto"]["equivalent_source_coverage"],
                "auto_source_coverage_relation": report["search_timing"][workload][f"{backend}_auto"]["source_coverage_relation"],
                "auto_source_hits_gained_vs_default": report["search_timing"][workload][f"{backend}_auto"]["source_hits_gained_vs_default"],
                "auto_source_hits_lost_vs_default": report["search_timing"][workload][f"{backend}_auto"]["source_hits_lost_vs_default"],
            }
    samples = report["index_samples"]
    stable = ("rows", "plain_field_bytes", "position_tokens", "field_md5")
    for key in stable:
        if len({sample[key] for sample in samples}) != 1:
            raise RuntimeError(f"Fresh index inputs differ across builds: {key}")
    report["index"] = {key: samples[0][key] for key in stable}
    for key in ("table_relation_bytes", "table_total_bytes", "index_relation_bytes",
                "index_total_bytes", "database_bytes"):
        report["index"][key] = int(statistics.median(sample[key] for sample in samples))
    report["index"]["build"] = COMMON.timings([sample["build_ms"] for sample in samples])


def render_markdown(report: dict[str, Any]) -> str:
    config, environment = report["configuration"], report["environment"]
    percent, escape = TRADEOFF.percent, TRADEOFF.md_text
    lines = [
        "# Pinyin Auto vs Default Regex vs Exact Phrase", "",
        f"- Runtime base HEAD: `{report['runtime_git_sha']}`; dirty source: `{report['runtime_git_worktree_dirty']}`. Source, dictionary, fixture, scripts and installed-library hashes are retained in JSON.",
        f"- PostgreSQL: `{environment['postgresql']}`; extensions: `{json.dumps(environment['extensions'], sort_keys=True)}`.",
        f"- Fresh UUID database; {config['rows']:,} dictionary-derived rows, {config['queries']:,} inputs per full/initials/autocomplete workload, {config['repeats']} rotated timing rounds; top-k {config['top_k']}.",
        "- Original default APIs are called directly. Auto is explicitly requested through mode `auto`. Native means `pinyin_phrase`, not full regex. All use the same plain whitespace BM25 index; generated/pipe fields are out of scope.",
        "- Every timing round recreates the shared index from the same corpus. Each method warms its entire workload before timing; index construction, setup, connection startup, complete-hitset preflight, gold checks and EXPLAIN are excluded from search latency.",
        "- Search latency includes PostgreSQL construction, BM25 matching/scoring, loopback round trips and ranked top-k transfer. Actual top-k IDs/scores must equal preflight and every timed method requires a real `TopKScanExecState` plan.",
        "- Ranked-score fingerprints use finite float scores on both PostgreSQL JSON and direct-cursor boundaries (including canonical zero); exact numeric score and ordered ID equality remains mandatory, without an approximate tolerance.",
        "- All methods transfer their full hitsets during untimed verification. Rust/SQL IDs, scores, rankings and Auto query serialization must match. Source-hit coverage is reported without filtering any method's failed partial queries; it is a known-source probe, not a broad relevance recall estimate.",
        "- Initial inputs use each source syllable's first letter separated by spaces. Completion inputs remove the final letter of the last source syllable when possible. Valid-looking initials and completions remain inherently ambiguous; workloads are not cherry-picked to make Auto succeed.",
        "- Exact phrase is evaluated for all intents, including its false negatives on partial input, but native initials/completion latency is deliberately omitted: empty or exact-only responses are not the same successful service.",
        "- Auto routing counts are read from the installed `pdb.query` JSON, not guessed from input or regex patterns. Constructor cold timing means a new backend, not cold storage or shared buffers.",
        "- Native phrase-prefix expansion is explicitly limited to 16,384 terms, not the upstream default of 50. A finite ceiling still may truncate broader external vocabularies; this plain pinyin corpus does not prove unbounded prefix equivalence.",
        *[f"- Run condition: {note}" for note in report.get("environment_notes", [])],
        "", "## Actual Hit Scope and Source Coverage", "",
        "Scope is measured over every matching query/document pair, not only top-k. Removed default matches are not automatically errors; relevance requires the separate intent oracle.", "",
        "| Workload | Strategy | Hit pairs | Default intersection | Default only | Strategy only | Changed queries | Source hits | Empty responses |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for workload in WORKLOADS:
        for strategy in STRATEGIES:
            method = f"rust_{strategy}"
            result = report["performance_results"][workload][method]
            if strategy == "default":
                pairs = sum(row["count"] for row in result["queries"])
                scope = {"candidate_hit_pairs": pairs, "intersection_hit_pairs": pairs,
                         "reference_only_hit_pairs": 0, "candidate_only_hit_pairs": 0,
                         "changed_hitset_queries": 0}
            else:
                scope = report["comparison"][workload][strategy]
            lines.append(f"| {workload} | {strategy} | {scope['candidate_hit_pairs']:,} | {scope['intersection_hit_pairs']:,} | {scope['reference_only_hit_pairs']:,} | {scope['candidate_only_hit_pairs']:,} | {scope['changed_hitset_queries']:,} | {result['source_hit_queries']:,}/{config['queries']:,} | {result['empty_hitset_queries']:,} |")
    lines += ["", "### Installed Auto Routes", "",
              "| Workload | Native term | Native phrase | Native phrase prefix | Regex | Empty |",
              "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for workload in WORKLOADS:
        counts = report["performance_results"][workload]["rust_auto"]["route_counts"]
        lines.append(f"| {workload} | " + " | ".join(str(counts.get(route, 0)) for route in
                     ("term", "phrase", "phrase_prefix", "regex", "empty")) + " |")
    lines += ["", "## Intent-Labeled Accuracy", "",
              "The reused independent contrastive gold fixture is unchanged from the previous regex/phrase study. Every case labels every document. Micro-averages pool TP/FP/FN/TN; `FDR=FP/(TP+FP)` is the fraction of returned matches that are false, while `FPR=FP/(FP+TN)` is the fraction of labeled negatives returned. Undefined denominators are `n/a`.",
              "Auto cannot infer whether a valid final `an` is complete or a prefix from spelling alone. Its conservative prefix policy may preserve completion while adding false matches to complete intent. Native full remains the exact-intent reference, not the gold-label generator.", "",
              "| Intent | Strategy | Cases | TP | FP | FN | TN | Precision | Recall | FDR | FPR |",
              "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for intent in INTENTS:
        for strategy in STRATEGIES:
            item = report["gold"][f"rust_{strategy}"]["per_intent_micro"][intent]
            lines.append(f"| {intent} | {strategy} | {item['cases']} | {item['tp']} | {item['fp']} | {item['fn']} | {item['tn']} | {percent(item['precision'])} | {percent(item['recall'])} | {percent(item['false_discovery_rate'])} | {percent(item['false_positive_rate'])} |")
    lines += ["", "### Gold Returned IDs", "",
              "| Intent/input | Relevant IDs | Default | Auto | Native | Auto route |",
              "| --- | --- | --- | --- | --- | --- |"]
    cases = [report["gold"][f"rust_{strategy}"]["cases"] for strategy in STRATEGIES]
    for default, auto, native in zip(*cases):
        ids = [",".join(map(str, case["returned_ids"])) or "(none)" for case in (default, auto, native)]
        positive = ",".join(map(str, default["positive_ids"])) or "(none)"
        lines.append(f"| {default['intent']}: `{escape(default['input'])}` | {positive} | {' | '.join(ids)} | {auto['route']} |")
    lines += ["", "## End-to-End Warm Indexed Performance", "",
              "Medians over rotated rounds. Inputs are identical across methods within each workload; all queries, including default or Auto misses, remain timed. Coverage compares the actual source-hit mask, not merely counts or a presumed perfect default. Improved coverage means gains without losses; reduced coverage means losses without gains; mixed means both. Lower time with lost source hits is a quality tradeoff, not a like-for-like successful-search speedup.", "",
              "| Workload | Method | Batch ms | Query p50 ms | Query p95 ms | Source hits | Coverage vs default (gained/lost) |",
              "| --- | --- | ---: | ---: | ---: | ---: | --- |"]
    for workload in WORKLOADS:
        for method in timed_methods(workload):
            item = report["search_timing"][workload][method]
            lines.append(f"| {workload} | {method} | {item['batch']['median_ms']:,.3f} | {item['query_p50_ms']:.3f} | {item['query_p95_ms']:.3f} | {item['source_hit_queries']:,}/{config['queries']:,} | {item['source_coverage_relation']} (+{item['source_hits_gained_vs_default']}/-{item['source_hits_lost_vs_default']}) |")
    lines += ["", "| Workload | Backend | Auto elapsed reduction vs default | Coverage vs default (gained/lost) |",
              "| --- | --- | ---: | --- |"]
    for workload in WORKLOADS:
        for backend in ("rust", "sql"):
            item = report["latency_comparison"][workload][backend]
            lines.append(f"| {workload} | {backend} | {percent(item['auto_elapsed_reduction_vs_default'])} | {item['auto_source_coverage_relation']} (+{item['auto_source_hits_gained_vs_default']}/-{item['auto_source_hits_lost_vs_default']}) |")
    lines += ["", "Positive reductions mean lower elapsed time, not a claim that semantics are identical. Scope changes and constructor choices both influence latency. Native partial timing is omitted intentionally; JSON retains native partial match coverage.",
              "", "## Materialized Construction and Shared Index", "",
              "Construction materializes actual query strings for all corpus rows, without BM25 execution.", "",
              "| Method | Fresh-backend cold ms | Warm ms | Query bytes |",
              "| --- | ---: | ---: | ---: |"]
    for method in METHODS:
        item = report["constructor"][method]
        lines.append(f"| {method} | {item['cold']['median_ms']:,.3f} | {item['warm']['median_ms']:,.3f} | {item['output_bytes']:,} |")
    lines += ["",
              f"Shared index: {report['index']['index_relation_bytes']:,} bytes, {report['index']['position_tokens']:,} position tokens; median fresh rebuild {report['index']['build']['median_ms']:,.3f} ms. Auto changes query construction, not stored fields, so no index-size reduction is expected.",
              "", "## Limits", "",
              "- Gold is small and deliberately contrastive, not representative production prevalence or a pronunciation benchmark.",
              "- Dictionary-derived source-hit coverage checks a known relevant source but does not label every extra performance-corpus match.",
              "- BM25 scores and rankings may change when Auto selects native term/phrase/prefix instead of regex; Rust/SQL parity is required only within the same strategy.",
              "- The plain whitespace field is intentionally shared. This does not evaluate generated/pipe indexes, custom analyzers or a lower index footprint.",
              "- Cold constructor means a new PostgreSQL connection. Indexed timing is warm and includes loopback client overhead; shared host load and caches limit generalization.",
              "- JSON retains every actual ID set, source coverage, scores/ranks, constructed Auto query, gold labels, Top-K plan, individual timing sample and source/runtime hash.", ""]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--data", type=Path, default=ROOT / "sql/data")
    parser.add_argument("--fixture", type=Path, default=ROOT / "test/fixtures/pinyin_phrase_tradeoff.json")
    parser.add_argument("--rows", type=int, default=20000)
    parser.add_argument("--queries", type=int, default=2000, help="Queries per full/initials/autocomplete workload")
    parser.add_argument("--repeats", type=int, default=6)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--pg-search-version", default="0.26.1")
    parser.add_argument("--postgresql-version-pattern", default="19beta4")
    parser.add_argument("--environment-note", action="append", default=[])
    parser.add_argument("--json-output", type=Path, default=ROOT / "benchmarks/pinyin_auto.json")
    parser.add_argument("--markdown-output", type=Path, default=ROOT / "benchmarks/pinyin_auto.md")
    args = parser.parse_args()
    fixture_bytes = args.fixture.read_bytes()
    fixture = json.loads(fixture_bytes)
    try:
        BASE.validate_configuration(args.rows, args.queries, args.repeats, args.top_k)
        TRADEOFF.validate_fixture(fixture)
        TRADEOFF.assert_report_paths_available(args.json_output, args.markdown_output)
    except ValueError as error:
        parser.error(str(error))
    corpus = COMMON.make_corpus(COMMON.read_words((args.data / "pinyin_words.csv").read_bytes()), args.rows)
    queries = COMMON.make_queries(corpus, args.queries)
    source_paths = [*sorted((ROOT / "src").glob("*.rs")), ROOT / "Cargo.lock", ROOT / "sql/word.sql",
                    ROOT / "sql/pinyin.sql", ROOT / "pg_pinyin--0.0.8--0.0.9.sql",
                    ROOT / "docker/Dockerfile.test-pg19beta4-trixie",
                    ROOT / "docker/patches/paradedb-0.26.1-pg19beta4.patch"]
    utility_paths = [ROOT / "scripts" / filename for filename in (
        "benchmark_pinyin_phrase.py", "benchmark_pinyin_phrase_tradeoff.py", "benchmark_pinyin_query_modes.py"
    )]
    report: dict[str, Any] = {
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "runtime_git_sha": subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip(),
        "runtime_git_worktree_dirty": bool(subprocess.check_output(
            ["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=no", "--ignore-submodules=all"],
            text=True).strip()),
        "benchmark_script_sha256": COMMON.digest(Path(__file__).read_bytes()),
        "benchmark_utility_sha256": {path.name: COMMON.digest(path.read_bytes()) for path in utility_paths},
        "fixture_sha256": COMMON.digest(fixture_bytes), "fixture": fixture,
        "corpus_sha256": COMMON.serialized_digest(corpus), "queries_sha256": COMMON.serialized_digest(queries),
        "dictionary_sha256": {path.name: COMMON.digest(path.read_bytes()) for path in sorted(args.data.glob("*.csv"))},
        "runtime_source_sha256": {str(path.relative_to(ROOT)): COMMON.digest(path.read_bytes()) for path in source_paths},
        "client_environment": {"python": platform.python_version(), "psycopg": psycopg.__version__,
                               "architecture": platform.machine(), "available_cpu_count": os.cpu_count()},
        "configuration": {"rows": args.rows, "requested_queries": args.queries, "queries": len(queries),
                          "repeats": args.repeats, "top_k": args.top_k, "methods": list(METHODS),
                          "workloads": list(WORKLOADS), "storage": "plain",
                          "requested_pg_search_version": args.pg_search_version,
                          "requested_postgresql_version_pattern": args.postgresql_version_pattern,
                          "rotated_method_orders": [rotated_methods(index) for index in range(args.repeats)],
                          "native_partial_latency": "omitted: exact native is not a partial-input service"},
        "environment_notes": args.environment_note, "performance_queries": queries,
        "constructor_samples": {method: [] for method in METHODS},
        "search_samples": {workload: {method: [] for method in timed_methods(workload)} for workload in WORKLOADS},
        "index_samples": [], "performance_results": {},
    }
    with BASE.temporary_database(args.dsn) as database:
        report["database"] = database
        report["environment"] = setup(args.dsn, database, corpus, args.data, fixture)
        if report["environment"]["extensions"].get("pg_search") != args.pg_search_version:
            raise RuntimeError(f"Expected pg_search {args.pg_search_version}; got {report['environment']['extensions']}")
        if args.postgresql_version_pattern not in report["environment"]["postgresql"]:
            raise RuntimeError(f"Expected PostgreSQL {args.postgresql_version_pattern}; got {report['environment']['postgresql']}")
        print(f"[gold] {len(fixture['rows'])} rows / {len(fixture['cases'])} exhaustive cases", flush=True)
        report["gold"] = check_gold(args.dsn, database, fixture)
        for repeat in range(args.repeats):
            for method in rotated_methods(repeat):
                result = measure_constructor(args.dsn, database, method)
                report["constructor_samples"][method].append(result)
                print(f"[constructor {repeat + 1}/{args.repeats}] {method} cold={result['cold_ms']:.3f} warm={result['warm_ms']:.3f} ms", flush=True)
        BASE.build_index(args.dsn, database)
        for workload in WORKLOADS:
            report["performance_results"][workload] = {}
            for method in METHODS:
                print(f"[all actual hitsets] {workload}/{method}, {len(queries):,} queries", flush=True)
                result = check_performance_results(args.dsn, database, queries, method, workload, args.top_k)
                report["performance_results"][workload][method] = result
                print(f"[coverage] {workload}/{method} source={result['source_hit_queries']}/{len(queries)} empty={result['empty_hitset_queries']} routes={result['route_counts']}", flush=True)
        report["comparison"] = compare_results(report["performance_results"])
        for repeat in range(args.repeats):
            index = BASE.build_index(args.dsn, database)
            report["index_samples"].append(index)
            print(f"[fresh shared index {repeat + 1}/{args.repeats}] {index['build_ms']:.3f} ms / {index['index_relation_bytes']:,} bytes", flush=True)
            shifted_workloads = [*WORKLOADS[repeat % len(WORKLOADS):], *WORKLOADS[:repeat % len(WORKLOADS)]]
            for workload in shifted_workloads:
                for method in rotated_methods(repeat):
                    if method not in timed_methods(workload):
                        continue
                    result = measure_search(args.dsn, database, queries, method, workload, args.top_k,
                                            report["performance_results"][workload][method]["queries"])
                    report["search_samples"][workload][method].append(result)
                    print(f"[indexed {repeat + 1}/{args.repeats}] {workload}/{method} batch={result['batch_ms']:.3f} ms p50/p95={result['per_query']['median_ms']:.3f}/{result['per_query']['p95_ms']:.3f} ms", flush=True)
        summarize(report)
        report["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
        for path in (args.json_output, args.markdown_output):
            path.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        args.markdown_output.write_text(render_markdown(report), encoding="utf-8")
        print(f"[reports] {args.markdown_output} and {args.json_output}", flush=True)


if __name__ == "__main__":
    main()
