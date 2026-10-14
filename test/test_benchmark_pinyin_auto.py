import importlib.util
import inspect
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "auto_benchmark", ROOT / "scripts/benchmark_pinyin_auto.py"
)
BENCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BENCH)


class PinyinAutoBenchmarkProtocolTests(unittest.TestCase):
    def manager(self):
        connection, manager, cursor = MagicMock(), MagicMock(), MagicMock()
        connection.__enter__.return_value.cursor.return_value = manager
        manager.__enter__.return_value = cursor
        return connection, cursor

    def query(self):
        return {"source_id": 1, "inputs": {"full": "xi an", "initials": "x a", "autocomplete": "xi a"}}

    def result_row(self, ids=(1, 2), input_value="xi an"):
        return {"source_id": 1, "input": input_value, "source_hit": 1 in ids,
                "ids": list(ids), "count": len(ids), "ids_md5": "ids",
                "rank_md5": "ranking", "score_md5": "scores",
                "top": [[ident, 1.0] for ident in ids]}

    def results(self):
        results = {}
        for workload in BENCH.WORKLOADS:
            results[workload] = {}
            for method in BENCH.METHODS:
                row = self.result_row(ids=(1, 2) if method.endswith("default") else (1,),
                                      input_value=self.query()["inputs"][workload])
                results[workload][method] = {"queries": [row]}
        return results

    def test_default_uses_original_public_api_and_auto_is_explicit(self):
        for backend in ("rust", "sql"):
            prefix = "sql_" if backend == "sql" else ""
            self.assertEqual(BENCH.query_expression(f"{backend}_default"),
                             f"public.{prefix}pinyin_regex_phrase(%s,%s,NULL,false)")
            self.assertEqual(BENCH.query_expression(f"{backend}_auto"),
                             f"public.{prefix}pinyin_regex_phrase_mode(%s,'auto',%s,NULL,false)")
            self.assertEqual(BENCH.query_expression(f"{backend}_native"),
                             f"public.{prefix}pinyin_phrase(%s,%s)")

    def test_all_six_methods_rotate_and_full_workload_times_all_three_strategies(self):
        self.assertEqual(set(BENCH.METHODS), {
            "rust_default", "rust_auto", "rust_native", "sql_default", "sql_auto", "sql_native",
        })
        orders = [BENCH.rotated_methods(index) for index in range(6)]
        self.assertEqual({order[0] for order in orders}, set(BENCH.METHODS))
        self.assertEqual(BENCH.timed_methods("full"), BENCH.METHODS)
        for order in orders:
            self.assertEqual(set(order), set(BENCH.METHODS))
            self.assertEqual(len(order), 6)

    def test_native_partial_empty_paths_are_not_successful_search_speedups(self):
        for workload in ("initials", "autocomplete"):
            self.assertEqual(set(BENCH.timed_methods(workload)), {
                "rust_default", "rust_auto", "sql_default", "sql_auto",
            })
            for backend in ("rust", "sql"):
                with self.assertRaisesRegex(ValueError, "not successful-search timing"):
                    BENCH.measure_search("dsn", "db", [], f"{backend}_native", workload, 10, [])

    def test_sql_fragments_reject_untrusted_methods_relations_and_expressions(self):
        for call in (
            lambda: BENCH.query_expression("rust_full"),
            lambda: BENCH.query_expression("rust_auto", "input;DROP TABLE x"),
            lambda: BENCH.query_expression("rust_auto", slope="untrusted"),
            lambda: BENCH.search_statement("rust_auto", "bench_index;DROP TABLE x"),
            lambda: BENCH.timed_methods("untrusted"),
        ):
            with self.assertRaises(ValueError):
                call()
        statement = BENCH.search_statement("rust_auto")
        self.assertIn("plain @@@", statement)
        self.assertIn("ORDER BY pdb.score(id) DESC,id LIMIT %s", statement)
        self.assertNotIn("ORDER BY score DESC", statement)

    def test_route_reads_query_json_not_input_or_pattern_inference(self):
        cases = {
            '{"term":{"value":"an"}}': "term",
            '{"phrase":{"phrases":["xi","an"]}}': "phrase",
            '{"PhraseArray":{"phrases":["xi","an"]}}': "phrase",
            '{"phrase_prefix":{"phrases":["xi","a"],"max_expansion":16384}}': "phrase_prefix",
            '{"regex_phrase":{"regexes":["w.*","h.*","h.*"]}}': "regex",
            '{"regex":{"pattern":"a.*"}}': "regex",
            '{"slop":{"query":{"phrase":{"phrases":["xi","an"]}},"slop":1}}': "phrase",
            '{"empty":{}}': "empty",
            '"empty"': "empty",
            '"all"': "all",
        }
        for text, route in cases.items():
            with self.subTest(text=text):
                self.assertEqual(BENCH.query_route(text), route)
        with self.assertRaisesRegex(ValueError, "Unrecognized installed"):
            BENCH.query_route('{"unknown":{"pattern":"phrase_prefix"}}')
        with self.assertRaises(json.JSONDecodeError):
            BENCH.query_route("not query JSON")

    def test_reuses_independently_labeled_gold_including_xi_a_and_whh(self):
        fixture = json.loads((ROOT / "test/fixtures/pinyin_phrase_tradeoff.json").read_text(encoding="utf-8"))
        BENCH.TRADEOFF.validate_fixture(fixture)
        self.assertEqual({case["intent"] for case in fixture["cases"]}, set(BENCH.INTENTS))
        inputs = {(case["intent"], case["input"]) for case in fixture["cases"]}
        self.assertIn(("initials", "whh"), inputs)
        self.assertIn(("autocomplete", "xi a"), inputs)
        self.assertIn(("full", "xi an"), inputs)

    def test_partial_workloads_preserve_original_sources_without_cherry_picking(self):
        inputs = BENCH.COMMON.query_inputs("xi an")
        self.assertEqual(inputs["full"], "xi an")
        self.assertEqual(inputs["initials"], "x a")
        self.assertEqual(inputs["autocomplete"], "xi a")
        queries = BENCH.COMMON.make_queries([
            {"id": 1, "name": "source a", "queries": inputs},
            {"id": 2, "name": "source b", "queries": BENCH.COMMON.query_inputs("nan an")},
        ], 2)
        self.assertEqual({query["source_id"] for query in queries}, {1, 2})
        self.assertEqual({query["inputs"]["initials"] for query in queries}, {"x a", "n a"})

    def test_scope_uses_all_ids_and_retains_empty_auto_result_as_quality_tradeoff(self):
        old, new = self.result_row(), self.result_row(ids=())
        comparison = BENCH.scope_comparison([old], [new])
        self.assertEqual(comparison["reference_hit_pairs"], 2)
        self.assertEqual(comparison["candidate_hit_pairs"], 0)
        self.assertEqual(comparison["scope_retention"], 0)
        self.assertEqual(comparison["reference_only_hit_pairs"], 2)
        self.assertEqual(comparison["changed_hitset_queries"], 1)
        self.assertEqual(comparison["changed_top_k_queries"], 1)
        old, new = self.result_row(), self.result_row(ids=(2, 3))
        comparison = BENCH.scope_comparison([old], [new])
        self.assertEqual(comparison["intersection_hit_pairs"], 1)
        self.assertEqual(comparison["reference_only_hit_pairs"], 1)
        self.assertEqual(comparison["candidate_only_hit_pairs"], 1)

    def test_scope_rejects_unaligned_queries_duplicate_ids_and_wrong_counts(self):
        for update in ({"source_id": 99}, {"input": "other"}, {"ids": [1, 1]}, {"count": 99}):
            row = self.result_row()
            row.update(update)
            with self.subTest(update=update), self.assertRaises(RuntimeError):
                BENCH.scope_comparison([self.result_row()], [row])
        for left, right in (([], []), ([self.result_row()], [])):
            with self.assertRaises(RuntimeError):
                BENCH.scope_comparison(left, right)

    def test_backend_parity_requires_scores_rank_and_constructed_auto_queries(self):
        for key, value in (("score_md5", "different"), ("rank_md5", "different"),
                           ("constructed_query", '{"term":{"value":"other"}}')):
            results = self.results()
            results["initials"]["sql_auto"]["queries"][0][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(RuntimeError, "Rust/SQL"):
                BENCH.compare_results(results)
        comparison = BENCH.compare_results(self.results())
        self.assertEqual(set(comparison), set(BENCH.WORKLOADS))
        self.assertEqual(comparison["full"]["auto"]["reference_hit_pairs"], 2)
        self.assertEqual(comparison["full"]["auto"]["candidate_hit_pairs"], 1)

    def test_auto_partial_misses_are_measured_not_filtered_or_aborted(self):
        manager, cursor = self.manager()
        # PostgreSQL cursor.fetchone returns one-element tuples for query text.
        cursor.fetchone.side_effect = [
            (0, "ids", "ranks", "scores", False, [], []),
            ('{"phrase_prefix":{"phrases":["n","a"],"max_expansion":16384}}',),
            ([{"Plan": {"Exec Method": "TopKScanExecState", "Index": "bench_phrase_bm25"}}],),
        ]
        with patch.object(BENCH.BASE, "connection", return_value=manager), \
                patch.object(BENCH.COMMON, "configure"):
            result = BENCH.check_performance_results("dsn", "db", [self.query()], "rust_auto", "initials", 10)
        self.assertEqual(len(result["queries"]), 1)
        self.assertEqual(result["queries"][0]["ids"], [])
        self.assertEqual(result["source_hit_queries"], 0)
        self.assertEqual(result["source_hit_fraction"], 0)
        self.assertEqual(result["empty_hitset_queries"], 1)
        self.assertEqual(result["route_counts"], {"phrase_prefix": 1})

    def test_native_partial_preflight_accepts_empty_and_does_not_demand_top_k_plan(self):
        manager, cursor = self.manager()
        cursor.fetchone.return_value = (0, "ids", "ranks", "scores", False, [], [])
        with patch.object(BENCH.BASE, "connection", return_value=manager), \
                patch.object(BENCH.COMMON, "configure"), \
                patch.object(BENCH.BASE, "verify_top_k_plan") as verify:
            result = BENCH.check_performance_results("dsn", "db", [self.query()], "rust_native", "initials", 10)
        self.assertEqual(result["source_hit_fraction"], 0)
        self.assertIsNone(result["example_plan"])
        verify.assert_not_called()

    def test_complete_default_and_native_preflight_require_source_hit(self):
        for method, workload in (("rust_default", "full"), ("sql_default", "full"),
                                 ("rust_native", "full")):
            manager, cursor = self.manager()
            cursor.fetchone.return_value = (0, "ids", "ranks", "scores", False, [], [])
            with self.subTest(method=method, workload=workload), \
                    patch.object(BENCH.BASE, "connection", return_value=manager), \
                    patch.object(BENCH.COMMON, "configure"), \
                    self.assertRaisesRegex(RuntimeError, "Required source match missing"):
                BENCH.check_performance_results("dsn", "db", [self.query()], method, workload, 10)

    def test_default_completion_miss_and_auto_recovery_are_retained(self):
        query = {"source_id": 1, "inputs": {
            "full": "yon yun", "initials": "y y", "autocomplete": "yon yu",
        }}
        plan = ([{"Plan": {"Exec Method": "TopKScanExecState", "Index": "bench_phrase_bm25"}}],)
        results = {}
        for method in ("rust_default", "rust_auto"):
            manager, cursor = self.manager()
            if method.endswith("auto"):
                cursor.fetchone.side_effect = [
                    (1, "ids", "ranks", "scores", True, [{"id": 1, "score": 1.0}], [1]),
                    ('{"phrase_prefix":{"phrases":["yon","yu"],"max_expansion":16384}}',),
                    plan,
                ]
            else:
                cursor.fetchone.side_effect = [(0, "ids", "ranks", "scores", False, [], []), plan]
            with patch.object(BENCH.BASE, "connection", return_value=manager), \
                    patch.object(BENCH.COMMON, "configure"):
                results[method] = BENCH.check_performance_results("dsn", "db", [query], method, "autocomplete", 10)
        self.assertEqual(results["rust_default"]["source_hit_queries"], 0)
        self.assertEqual(results["rust_auto"]["source_hit_queries"], 1)
        coverage = BENCH.source_coverage(results["rust_default"]["queries"], results["rust_auto"]["queries"])
        self.assertEqual(coverage["source_coverage_relation"], "improved")
        self.assertEqual(coverage["source_hits_gained_vs_default"], 1)
        self.assertEqual(coverage["source_hits_lost_vs_default"], 0)
        self.assertFalse(coverage["equivalent_source_coverage"])

    def test_source_coverage_compares_masks_not_total_counts_or_perfect_default(self):
        miss, hit = self.result_row(ids=()), self.result_row(ids=(1,))
        same = BENCH.source_coverage([miss], [miss])
        self.assertTrue(same["equivalent_source_coverage"])
        self.assertEqual(same["source_coverage_relation"], "same")
        reduced = BENCH.source_coverage([hit], [miss])
        self.assertEqual(reduced["source_coverage_relation"], "reduced")
        self.assertEqual(reduced["source_hits_lost_vs_default"], 1)
        old_second = {**self.result_row(ids=()), "source_id": 2}
        new_second = {**self.result_row(ids=(2,)), "source_id": 2, "source_hit": True}
        mixed = BENCH.source_coverage([hit, old_second], [miss, new_second])
        self.assertEqual(mixed["source_coverage_relation"], "mixed")
        self.assertEqual(mixed["source_hits_gained_vs_default"], 1)
        self.assertEqual(mixed["source_hits_lost_vs_default"], 1)
        self.assertFalse(mixed["equivalent_source_coverage"])
        with self.assertRaises(RuntimeError):
            BENCH.source_coverage([], [])
        with self.assertRaises(RuntimeError):
            BENCH.source_coverage([hit], [{**hit, "source_id": 9}])

    def test_timed_top_k_scores_must_match_method_specific_preflight(self):
        for timed in ([(2, 1.0)], [(1, 2.0)]):
            manager, cursor = self.manager()
            cursor.fetchall.side_effect = [[(1, 1.0)], timed]
            with self.subTest(timed=timed), \
                    patch.object(BENCH.BASE, "connection", return_value=manager), \
                    patch.object(BENCH.COMMON, "configure"), \
                    self.assertRaisesRegex(RuntimeError, "Timed ranked IDs/scores differ"):
                BENCH.measure_search("dsn", "db", [self.query()], "rust_auto", "autocomplete", 10,
                                     [{"top": [[1, 1.0]]}])

    def test_postgres_json_integral_scores_and_cursor_floats_have_same_fingerprint(self):
        preflight_json = json.loads('[{"id":1,"score":0},{"id":2,"score":1}]')
        expected = BENCH.canonical_ranked_rows((row["id"], row["score"]) for row in preflight_json)
        self.assertEqual(expected, [[1, 0.0], [2, 1.0]])
        self.assertTrue(all(type(row[1]) is float for row in expected))
        manager, cursor = self.manager()
        cursor.fetchall.side_effect = [[(1, 0.0), (2, 1.0)], [(1, 0.0), (2, 1.0)]]
        with patch.object(BENCH.BASE, "connection", return_value=manager), \
                patch.object(BENCH.COMMON, "configure"):
            result = BENCH.measure_search("dsn", "db", [self.query()], "rust_auto", "full", 10,
                                         [{"top": expected}])
        self.assertEqual(result["top_results_sha256"], BENCH.COMMON.serialized_digest([expected]))
        self.assertNotEqual(BENCH.COMMON.serialized_digest([[[1, 0], [2, 1]]]), result["top_results_sha256"])

    def test_ranked_score_canonicalization_preserves_exact_values_and_rejects_invalid_data(self):
        positive = BENCH.canonical_ranked_rows([(1, 0.0), (2, 1.25)])
        negative = BENCH.canonical_ranked_rows([(1, -0.0), (2, 1.25)])
        self.assertEqual(BENCH.COMMON.serialized_digest(positive), BENCH.COMMON.serialized_digest(negative))
        self.assertEqual(positive[1][1], 1.25)
        for ident, score in ((True, 1.0), (1.0, 1.0), (1, True), (1, "0"),
                             (1, float("nan")), (1, float("inf")), (1, float("-inf"))):
            with self.subTest(ident=ident, score=score), self.assertRaises(ValueError):
                BENCH.canonical_ranked_rows([(ident, score)])
        with self.assertRaisesRegex(ValueError, "duplicate IDs"):
            BENCH.canonical_ranked_rows([(1, 0.0), (1, 1.0)])

    def test_preflight_json_zero_timed_float_and_summarize_fingerprints_agree(self):
        manager, cursor = self.manager()
        cursor.fetchone.side_effect = [
            (1, "ids", "ranks", "scores", True, [{"id": 1, "score": 0}], [1]),
            ('{"phrase_prefix":{"phrases":["xi","an"],"max_expansion":16384}}',),
            ([{"Plan": {"Exec Method": "TopKScanExecState", "Index": "bench_phrase_bm25"}}],),
        ]
        with patch.object(BENCH.BASE, "connection", return_value=manager), \
                patch.object(BENCH.COMMON, "configure"):
            verified = BENCH.check_performance_results("dsn", "db", [self.query()], "rust_auto", "full", 10)
        self.assertIs(type(verified["queries"][0]["top"][0][1]), float)
        manager, cursor = self.manager()
        cursor.fetchall.side_effect = [[(1, 0.0)], [(1, 0.0)]]
        with patch.object(BENCH.BASE, "connection", return_value=manager), \
                patch.object(BENCH.COMMON, "configure"):
            measured = BENCH.measure_search("dsn", "db", [self.query()], "rust_auto", "full", 10,
                                           verified["queries"])
        self.assertEqual(measured["top_results_sha256"], verified["top_results_sha256"])
        constructor = {"cold_ms": 1.0, "warm_ms": 0.5, "rows": 1, "output_bytes": 42,
                       "output_md5": "constructor"}
        index = {"rows": 1, "plain_field_bytes": 5, "position_tokens": 2, "field_md5": "field",
                 "table_relation_bytes": 8192, "table_total_bytes": 8192,
                 "index_relation_bytes": 4096, "index_total_bytes": 4096,
                 "database_bytes": 65536, "build_ms": 1.0}
        report = {
            "configuration": {"queries": 1},
            "constructor_samples": {method: [constructor] * 6 for method in BENCH.METHODS},
            "search_samples": {}, "performance_results": {}, "index_samples": [index] * 6,
        }
        for workload in BENCH.WORKLOADS:
            report["search_samples"][workload] = {
                method: [measured] * 6 for method in BENCH.timed_methods(workload)
            }
            report["performance_results"][workload] = {
                method: verified for method in BENCH.METHODS
            }
        BENCH.summarize(report)
        self.assertEqual(report["search_timing"]["full"]["rust_auto"]["top_results_sha256"],
                         verified["top_results_sha256"])
        report["search_samples"]["full"]["rust_auto"] = [{**measured, "top_results_sha256": "drift"}] * 6
        with self.assertRaisesRegex(RuntimeError, "Timed top-k fingerprint drift"):
            BENCH.summarize(report)

    def test_constructor_materializes_queries_and_does_not_time_search(self):
        statement = BENCH.constructor_statement("rust_auto")
        self.assertIn("WITH constructed AS MATERIALIZED", statement)
        self.assertIn("::text AS query", statement)
        self.assertIn("sum(octet_length(query))", statement)
        self.assertNotIn("@@@", statement)
        self.assertNotIn("bench_index", statement)
        manager, cursor = self.manager()
        cursor.fetchone.side_effect = [(2, 42, "fingerprint", 0)] * 3
        with patch.object(BENCH.BASE, "connection", return_value=manager), \
                patch.object(BENCH.COMMON, "configure"):
            result = BENCH.measure_constructor("dsn", "db", "rust_auto")
        self.assertEqual(result["rows"], 2)
        self.assertEqual(result["output_bytes"], 42)
        self.assertEqual(cursor.execute.call_count, 3)

    def test_constructor_rejects_empty_and_unstable_values(self):
        for records in (
            [(0, 0, "empty", 0)] * 3,
            [(2, 42, "fingerprint", 1)] * 3,
            [(2, 42, "fingerprint", 0), (2, 42, "changed", 0), (2, 42, "changed", 0)],
        ):
            manager, cursor = self.manager()
            cursor.fetchone.side_effect = records
            with self.subTest(records=records), \
                    patch.object(BENCH.BASE, "connection", return_value=manager), \
                    patch.object(BENCH.COMMON, "configure"), \
                    self.assertRaisesRegex(RuntimeError, "Empty or unstable"):
                BENCH.measure_constructor("dsn", "db", "rust_auto")

    def test_report_guard_refuses_overwrite_and_identical_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            json_path, markdown_path = Path(directory) / "new.json", Path(directory) / "new.md"
            BENCH.TRADEOFF.assert_report_paths_available(json_path, markdown_path)
            with self.assertRaises(ValueError):
                BENCH.TRADEOFF.assert_report_paths_available(json_path, json_path)
            json_path.touch()
            with self.assertRaisesRegex(ValueError, "Refusing to overwrite"):
                BENCH.TRADEOFF.assert_report_paths_available(json_path, markdown_path)

    def test_database_cleanup_is_owned_and_runs_after_errors(self):
        admin, manager = MagicMock(), MagicMock()
        manager.__enter__.return_value = admin
        database = "pg_pinyin_native_0123456789abcdef"
        with patch.object(BENCH.BASE.psycopg, "connect", return_value=manager), \
                patch.object(BENCH.BASE, "new_database_name", return_value=database):
            with self.assertRaisesRegex(RuntimeError, "failure during setup"):
                with BENCH.BASE.temporary_database("postgres://localhost/postgres"):
                    raise RuntimeError("failure during setup")
        self.assertEqual(admin.execute.call_count, 2)
        self.assertIn("DROP DATABASE", str(admin.execute.call_args.args[0]))
        with self.assertRaises(ValueError):
            BENCH.BASE.drop_database(admin, "production")

    def test_each_timing_round_rebuilds_shared_index_and_records_runtime_hashes(self):
        source = inspect.getsource(BENCH.main)
        self.assertIn("for repeat in range(args.repeats):\n            index = BASE.build_index", source)
        self.assertIn("benchmark_script_sha256", source)
        self.assertIn("runtime_source_sha256", source)
        self.assertIn("fixture_sha256", source)
        self.assertIn("dictionary_sha256", source)
        self.assertIn("runtime_git_worktree_dirty", source)
        self.assertNotIn("dispatcher", source.lower())


if __name__ == "__main__":
    unittest.main()
