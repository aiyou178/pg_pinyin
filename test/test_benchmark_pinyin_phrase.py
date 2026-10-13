import copy
import importlib.util
import inspect
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("native_phrase_benchmark", ROOT / "scripts/benchmark_pinyin_phrase.py")
BENCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BENCH)


class PinyinPhraseBenchmarkProtocolTests(unittest.TestCase):
    def test_configuration_rejects_invalid_or_under_sampled_runs(self):
        BENCH.validate_configuration(20000, 2000, 3, 10)
        for values in ((0, 1, 3, 10), (20, 21, 3, 10), (20, 2, 2, 10), (20, 2, 3, 0)):
            with self.subTest(values=values), self.assertRaises(ValueError):
                BENCH.validate_configuration(*values)

    def test_method_order_is_deterministic_rotated_and_complete(self):
        first = BENCH.rotated_methods(0)
        self.assertEqual(set(first), set(BENCH.METHODS))
        for repeat in range(3):
            methods = BENCH.rotated_methods(repeat)
            self.assertEqual(methods, BENCH.rotated_methods(repeat))
            self.assertEqual(set(methods), set(first))
            self.assertEqual(len(methods), len(first))
        self.assertNotEqual(first, BENCH.rotated_methods(1))
        self.assertNotEqual(first, BENCH.rotated_methods(2))

    def test_database_cleanup_is_always_attempted_after_workload_failure(self):
        admin, manager = MagicMock(), MagicMock()
        manager.__enter__.return_value = admin
        name = "pg_pinyin_native_0123456789abcdef"
        with patch.object(BENCH.psycopg, "connect", return_value=manager), \
                patch.object(BENCH, "new_database_name", return_value=name):
            with self.assertRaisesRegex(RuntimeError, "workload failed"):
                with BENCH.temporary_database("postgres://localhost/postgres") as allocated:
                    self.assertEqual(allocated, name)
                    raise RuntimeError("workload failed")
        statements = [str(call.args[0]) for call in admin.execute.call_args_list]
        self.assertEqual(len(statements), 2)
        self.assertIn("CREATE DATABASE", statements[0])
        self.assertIn("DROP DATABASE", statements[1])
        self.assertIn("WITH (FORCE)", statements[1])

    def test_cleanup_refuses_other_benchmark_or_application_databases(self):
        admin = MagicMock()
        for name in ("postgres", "pg_pinyin_p1_0123456789abcdef", "pg_pinyin_native_bad",
                     "pg_pinyin_native_0123456789abcdef;DROP DATABASE postgres"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                BENCH.drop_database(admin, name)
        admin.execute.assert_not_called()
        self.assertNotEqual(BENCH.new_database_name(), BENCH.new_database_name())

    def test_reuses_reproducible_standalone_corpus_without_dispatcher(self):
        words = {"西安": "xi an", "仙": "xian", "武汉汉": "wu han han"}
        corpus = BENCH.COMMON.make_corpus(words, 3)
        self.assertEqual(corpus, BENCH.COMMON.make_corpus(words, 3))
        queries = BENCH.COMMON.make_queries(corpus, 2)
        self.assertEqual(queries, BENCH.COMMON.make_queries(corpus, 2))
        self.assertTrue(all(query["inputs"]["full"] == words[corpus[query["source_id"] - 1]["name"]]
                            for query in queries))
        self.assertNotIn("dispatcher", inspect.getsource(BENCH).lower())

    def test_gold_is_handwritten_with_positive_negative_and_empty_cases(self):
        BENCH.validate_gold()
        cases = {case["input"]: case for case in BENCH.GOLD_CASES}
        self.assertIn(104, cases["xi'an"]["positive_ids"])
        self.assertIn(105, cases["xi'an"]["negative_ids"])
        self.assertIn(107, cases["xi an"]["positive_ids"])
        self.assertEqual(cases[""]["positive_ids"], [])
        self.assertEqual(set(cases[""]["negative_ids"]), {ident for ident, _ in BENCH.GOLD_ROWS})
        self.assertTrue(any("single" in case["label"] for case in BENCH.GOLD_CASES))

    def test_sql_expressions_have_fixed_identifiers_and_bound_values(self):
        self.assertIn("pinyin_phrase(%s,%s)", BENCH.search_statement("rust_native"))
        self.assertIn("sql_pinyin_phrase(%s,%s)", BENCH.search_statement("sql_native"))
        self.assertIn("pinyin_regex_phrase_mode(%s,'full',%s,NULL,false)", BENCH.search_statement("rust_regex"))
        self.assertIn("pdb.term('hao')", BENCH.search_statement("rust_native", "bench_gold", mixed_or=True))
        self.assertIn("ORDER BY pdb.score(id) DESC,id", BENCH.search_statement("rust_native"))
        self.assertNotIn("ORDER BY score DESC", BENCH.search_statement("rust_native"))
        with self.assertRaises(ValueError):
            BENCH.search_statement("rust_native", "untrusted;drop")
        with self.assertRaises(ValueError):
            BENCH.query_expression("untrusted")
        with self.assertRaises(ValueError):
            BENCH.query_expression("rust_native", "untrusted")

    def test_constructor_materializes_actual_query_once_per_row(self):
        statement = BENCH.constructor_statement("rust_native")
        self.assertIn("AS MATERIALIZED", statement)
        self.assertIn("pinyin_phrase(plain,NULL)::text", statement)
        self.assertIn("sum(octet_length(query))", statement)
        self.assertEqual(statement.count("pinyin_phrase("), 1)
        self.assertNotIn("pinyin_regex_phrase_patterns", statement)

    def test_full_hitset_checksum_is_not_limited_to_top_k(self):
        statement = BENCH.verification_statement("rust_native")
        hits = statement.split(") SELECT", 1)[0]
        self.assertNotIn("LIMIT", hits)
        self.assertIn("string_agg(id::text,',' ORDER BY id)", statement)
        self.assertIn("ORDER BY score DESC,id", statement)
        self.assertIn("bool_or(id=%s)", statement)
        self.assertIn("id::text || ':' || score::text", statement)

    def test_plan_requires_score_top_k_not_just_a_paradedb_scan(self):
        scan = {"Node Type": "Custom Scan", "Custom Plan Provider": "ParadeDB Base Scan",
                "Index": "bench_phrase_bm25", "Exec Method": "TopKScanExecState"}
        BENCH.verify_top_k_plan([{"Plan": {"Node Type": "Limit", "Plans": [scan]}}])
        scan["Exec Method"] = "NormalScanExecState"
        with self.assertRaisesRegex(RuntimeError, "BM25 score Top-K"):
            BENCH.verify_top_k_plan([{"Plan": {"Node Type": "Sort", "Plans": [scan]}}])

    def results(self):
        base = {"source_id": 1, "count": 2, "ids_md5": "same-ids", "rank_md5": "old-rank",
                "score_md5": "old-score", "top": [[1, 1.0], [2, 1.0]]}
        new = {**base, "rank_md5": "new-rank", "score_md5": "new-score", "top": [[2, 2.0], [1, 1.0]]}
        return {method: {"queries": [copy.deepcopy(new if method.endswith("native") else base)]}
                for method in BENCH.METHODS}

    def test_strategy_score_and_rank_differences_are_reported_not_hidden(self):
        comparison = BENCH.compare_results(self.results())
        self.assertTrue(comparison["all_hitsets_equal"])
        self.assertEqual(comparison["ranked_queries"], 1)
        self.assertEqual(comparison["scored_queries"], 1)
        self.assertEqual(comparison["top_k_queries"], 1)
        self.assertEqual(comparison["examples"][0]["native_top"], [[2, 2.0], [1, 1.0]])

    def test_equal_empty_or_wrong_hitsets_cannot_fake_equivalence(self):
        results = self.results()
        results["rust_native"]["queries"][0]["ids_md5"] = "different-ids"
        results["sql_native"] = copy.deepcopy(results["rust_native"])
        with self.assertRaisesRegex(RuntimeError, "full hitset differs"):
            BENCH.compare_results(results)
        results = self.results()
        for method in BENCH.METHODS:
            results[method]["queries"][0]["count"] = 0
            results[method]["queries"][0]["top"] = []
        with self.assertRaisesRegex(RuntimeError, "Empty performance hitsets"):
            BENCH.compare_results(results)

    def test_rust_sql_parity_checks_scores_not_only_ids(self):
        results = self.results()
        results["sql_native"]["queries"][0]["score_md5"] = "wrong-score"
        with self.assertRaisesRegex(RuntimeError, "ranking/score Rust/SQL parity"):
            BENCH.compare_results(results)

    def test_timed_same_ids_different_score_is_rejected(self):
        conn_manager, cursor_manager, cursor = MagicMock(), MagicMock(), MagicMock()
        conn_manager.__enter__.return_value.cursor.return_value = cursor_manager
        cursor_manager.__enter__.return_value = cursor
        cursor.fetchall.side_effect = [[(1, 1.0)], [(1, 2.0)]]
        query = {"inputs": {"full": "an"}}
        with patch.object(BENCH, "connection", return_value=conn_manager):
            with self.assertRaisesRegex(RuntimeError, "Timed ranked IDs/scores differ"):
                BENCH.measure_search("dsn", "database", [query], "rust_native", 10, [{"top": [[1, 1.0]]}])

    def test_single_index_and_report_caveats_are_explicit(self):
        index_source = inspect.getsource(BENCH.build_index)
        self.assertIn("(id,(plain::pdb.whitespace))", index_source)
        self.assertNotIn("mixed", index_source)
        report_source = inspect.getsource(BENCH.render_markdown)
        for caveat in ("not a parser rewrite", "not cold OS", "not just top-k", "opt-in",
                       "No index-reduction claim", "not general pronunciation accuracy",
                       "not directly comparable", "mixed-OR single-term"):
            self.assertIn(caveat, report_source)


if __name__ == "__main__":
    unittest.main()
