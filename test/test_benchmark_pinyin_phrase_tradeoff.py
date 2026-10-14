import copy
import importlib.util
import inspect
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "phrase_tradeoff_benchmark", ROOT / "scripts/benchmark_pinyin_phrase_tradeoff.py"
)
BENCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BENCH)


class PinyinPhraseTradeoffProtocolTests(unittest.TestCase):
    def fixture(self):
        return json.loads((ROOT / "test/fixtures/pinyin_phrase_tradeoff.json").read_text(encoding="utf-8"))

    def sample_fixture(self):
        return {
            "rows": [{"id": 1, "name": "exact", "plain": "an"},
                     {"id": 2, "name": "prefix contrast", "plain": "ang"}],
            "cases": [{"label": "exact an", "input": "an", "intent": "full", "slope": None,
                       "positive_ids": [1], "negative_ids": [2],
                       "rationale": "A complete syllable is not a longer syllable prefix."}],
        }

    def test_fixture_labels_are_independent_exhaustive_and_disjoint(self):
        fixture = self.fixture()
        BENCH.validate_fixture(fixture)
        ids = {row["id"] for row in fixture["rows"]}
        self.assertEqual(len(ids), len(fixture["rows"]))
        self.assertEqual({case["intent"] for case in fixture["cases"]},
                         {"full", "initials", "autocomplete", "normalization"})
        for case in fixture["cases"]:
            with self.subTest(case=case["label"]):
                positives, negatives = set(case["positive_ids"]), set(case["negative_ids"])
                self.assertFalse(positives & negatives)
                self.assertEqual(positives | negatives, ids)
                self.assertTrue(case["rationale"].strip())
        cases = {(case["intent"], case["input"], case["slope"]): case for case in fixture["cases"]}
        self.assertEqual(cases[("full", "xi'an", 0)]["positive_ids"],
                         cases[("full", "xi an", 0)]["positive_ids"])
        self.assertNotEqual(cases[("full", "xi'an", 0)]["positive_ids"],
                            cases[("full", "xian", 0)]["positive_ids"])
        self.assertIn(12, cases[("full", "xi an", 1)]["positive_ids"])
        self.assertNotIn(12, cases[("full", "xi an", 0)]["positive_ids"])
        self.assertIn(("initials", "whh", 0), cases)
        self.assertIn(("normalization", "l\u00fc liang", 0), cases)
        self.assertNotEqual(cases[("full", "an", 0)]["positive_ids"],
                            cases[("autocomplete", "an", 0)]["positive_ids"])

    def test_fixture_validation_rejects_missing_conflicting_or_unknown_labels(self):
        changes = [
            lambda fixture: fixture["rows"].append(copy.deepcopy(fixture["rows"][0])),
            lambda fixture: fixture["cases"][0].update(negative_ids=[]),
            lambda fixture: fixture["cases"][0].update(negative_ids=[1, 2]),
            lambda fixture: fixture["cases"][0].update(negative_ids=[2, 999]),
            lambda fixture: fixture["cases"].append(copy.deepcopy(fixture["cases"][0])),
            lambda fixture: fixture["cases"][0].update(slope=-1),
        ]
        for change in changes:
            fixture = self.sample_fixture()
            change(fixture)
            with self.subTest(fixture=fixture), self.assertRaises(ValueError):
                BENCH.validate_fixture(fixture)

    def test_all_six_backend_strategy_combinations_rotate_without_bias(self):
        self.assertEqual(set(BENCH.METHODS), {
            "rust_default", "rust_full", "rust_native",
            "sql_default", "sql_full", "sql_native",
        })
        orders = [BENCH.rotated_methods(index) for index in range(len(BENCH.METHODS))]
        for index, order in enumerate(orders):
            self.assertEqual(order, BENCH.rotated_methods(index))
            self.assertEqual(set(order), set(BENCH.METHODS))
            self.assertEqual(len(order), len(set(order)))
        self.assertEqual({order[0] for order in orders}, set(BENCH.METHODS))

    def test_default_comparator_calls_original_public_api_not_compat_alias(self):
        for backend in ("rust", "sql"):
            prefix = "sql_" if backend == "sql" else ""
            default = BENCH.query_expression(f"{backend}_default")
            self.assertEqual(default, f"public.{prefix}pinyin_regex_phrase(%s,%s,NULL,false)")
            self.assertNotIn("regex_phrase_mode", default)
            self.assertNotIn("compat", default)
            self.assertEqual(BENCH.query_expression(f"{backend}_full"),
                             f"public.{prefix}pinyin_regex_phrase_mode(%s,'full',%s,NULL,false)")
            self.assertEqual(BENCH.query_expression(f"{backend}_native"),
                             f"public.{prefix}pinyin_phrase(%s,%s)")

    def test_search_statements_bind_inputs_and_preserve_real_score_top_k(self):
        for method in BENCH.METHODS:
            statement = BENCH.search_statement(method)
            self.assertIn("plain @@@", statement)
            self.assertIn("ORDER BY pdb.score(id) DESC,id LIMIT %s", statement)
            self.assertNotIn("ORDER BY score DESC", statement)
        for call in (lambda: BENCH.query_expression("untrusted"),
                     lambda: BENCH.query_expression("rust_default", "untrusted"),
                     lambda: BENCH.query_expression("rust_default", slope="untrusted"),
                     lambda: BENCH.search_statement("rust_native", "bench_index;DROP TABLE x")):
            with self.assertRaises(ValueError):
                call()

    def test_false_discovery_and_false_positive_rate_use_different_denominators(self):
        metric = BENCH.confusion_metrics([1, 3], [1, 2], [3, 4, 5, 6])
        self.assertEqual({key: metric[key] for key in ("tp", "fp", "fn", "tn")},
                         {"tp": 1, "fp": 1, "fn": 1, "tn": 3})
        self.assertEqual(metric["precision"], 0.5)
        self.assertEqual(metric["recall"], 0.5)
        self.assertEqual(metric["false_discovery_rate"], 0.5)
        self.assertEqual(metric["false_positive_rate"], 0.25)

    def test_micro_metrics_pool_counts_instead_of_averaging_case_rates(self):
        cases = [{"metrics": BENCH.confusion_metrics([1], [1], range(2, 11))},
                 {"metrics": BENCH.confusion_metrics(range(1, 11), [1], range(2, 11))}]
        metric = BENCH.summarize_metrics(cases)
        self.assertEqual(metric["cases"], 2)
        self.assertEqual(metric["tp"], 2)
        self.assertEqual(metric["fp"], 9)
        self.assertEqual(metric["tn"], 9)
        self.assertAlmostEqual(metric["precision"], 2 / 11)
        self.assertAlmostEqual(metric["false_discovery_rate"], 9 / 11)
        self.assertEqual(metric["false_positive_rate"], 0.5)
        self.assertEqual(metric["recall"], 1)
        self.assertNotAlmostEqual(metric["precision"], (1 + 0.1) / 2)

    def test_empty_results_do_not_fake_perfect_precision_or_recall(self):
        metric = BENCH.confusion_metrics([], [1, 2], [3, 4])
        self.assertIsNone(metric["precision"])
        self.assertIsNone(metric["false_discovery_rate"])
        self.assertEqual(metric["recall"], 0)
        self.assertEqual(metric["false_positive_rate"], 0)
        no_positives = BENCH.confusion_metrics([], [], [1, 2])
        self.assertIsNone(no_positives["recall"])
        no_negatives = BENCH.confusion_metrics([1], [1], [])
        self.assertIsNone(no_negatives["false_positive_rate"])

    def test_false_negatives_follow_gold_not_native_output(self):
        default = BENCH.confusion_metrics([1, 2], [1, 2], [3])
        native = BENCH.confusion_metrics([], [1, 2], [3])
        self.assertEqual(default["recall"], 1)
        self.assertEqual(native["recall"], 0)
        self.assertEqual(native["fn"], 2)
        self.assertEqual(native["fp"], 0)

    def test_metrics_reject_unlabeled_or_conflicting_ids(self):
        for returned, positive, negative in (([99], [1], [2]), ([1], [1], [1, 2])):
            with self.subTest(returned=returned), self.assertRaises(ValueError):
                BENCH.confusion_metrics(returned, positive, negative)

    def test_scope_compares_actual_intersections_not_only_equal_counts(self):
        scope = BENCH.compute_scope([1, 2], [2, 3])
        self.assertEqual(scope["default_count"], 2)
        self.assertEqual(scope["native_count"], 2)
        self.assertEqual(scope["intersection_count"], 1)
        self.assertEqual(scope["default_only_count"], 1)
        self.assertEqual(scope["native_only_count"], 1)
        self.assertEqual(scope["default_only_ids"], [1])
        self.assertEqual(scope["native_only_ids"], [3])
        self.assertEqual(scope["intersection_ids"], [2])

    def results(self):
        default = {"source_id": 1, "ids": [1, 2], "count": 2,
                   "rank_md5": "default-ranking", "score_md5": "default-scores",
                   "top": [[1, 1.0], [2, 1.0]]}
        exact = {"source_id": 1, "ids": [1], "count": 1,
                 "rank_md5": "exact-ranking", "score_md5": "exact-scores",
                 "top": [[1, 2.0]]}
        return {method: {"queries": [copy.deepcopy(default if method.endswith("default") else exact)]}
                for method in BENCH.METHODS}

    def test_default_native_scope_differences_are_allowed_and_reported(self):
        comparison = BENCH.compare_results(self.results())
        self.assertEqual(comparison["changed_hitset_queries"], 1)
        self.assertEqual(comparison["default_hit_pairs"], 2)
        self.assertEqual(comparison["native_hit_pairs"], 1)
        self.assertEqual(comparison["intersection_hit_pairs"], 1)
        self.assertEqual(comparison["default_only_hit_pairs"], 1)
        self.assertEqual(comparison["native_retention_of_default_hits"], 0.5)
        self.assertTrue(comparison["full_native_all_hitsets_equal"])

    def test_full_native_control_requires_identical_ids_even_with_same_count(self):
        results = self.results()
        for backend in ("rust", "sql"):
            results[f"{backend}_native"]["queries"][0]["ids"] = [3]
        with self.assertRaises(RuntimeError):
            BENCH.compare_results(results)
        results = self.results()
        for backend in ("rust", "sql"):
            row = results[f"{backend}_native"]["queries"][0]
            row["rank_md5"] = "native-ranking"
            row["score_md5"] = "native-scoring"
            row["top"] = [[1, 3.0]]
        comparison = BENCH.compare_results(results)
        self.assertTrue(comparison["full_native_all_hitsets_equal"])
        self.assertEqual(comparison["full_native_changed_score_queries"], 1)
        self.assertEqual(comparison["full_native_changed_ranking_queries"], 1)

    def test_backend_parity_rejects_score_drift_even_when_ids_are_equal(self):
        results = self.results()
        results["sql_native"]["queries"][0]["score_md5"] = "different-score"
        with self.assertRaises(RuntimeError):
            BENCH.compare_results(results)

    def test_normal_scan_plus_sort_cannot_be_presented_as_bm25_top_k(self):
        scan = {"Node Type": "Custom Scan", "Custom Plan Provider": "ParadeDB Base Scan",
                "Index": "bench_phrase_bm25", "Exec Method": "TopKScanExecState"}
        BENCH.BASE.verify_top_k_plan([{"Plan": {"Node Type": "Limit", "Plans": [scan]}}])
        scan["Exec Method"] = "NormalScanExecState"
        with self.assertRaises(RuntimeError):
            BENCH.BASE.verify_top_k_plan([{"Plan": {"Node Type": "Sort", "Plans": [scan]}}])

    def test_timed_ids_and_scores_must_equal_each_methods_untimed_top_k(self):
        for method in BENCH.METHODS:
            for timed in ([(2, 1.0)], [(1, 2.0)]):
                manager, cursor_manager, cursor = MagicMock(), MagicMock(), MagicMock()
                manager.__enter__.return_value.cursor.return_value = cursor_manager
                cursor_manager.__enter__.return_value = cursor
                cursor.fetchall.side_effect = [[(1, 1.0)], timed]
                with self.subTest(method=method, timed=timed), \
                        patch.object(BENCH.BASE, "connection", return_value=manager), \
                        patch.object(BENCH.COMMON, "configure"), \
                        self.assertRaisesRegex(RuntimeError, "Timed ranked IDs/scores differ"):
                    BENCH.measure_search("dsn", "database", [{"inputs": {"full": "an"}}],
                                         method, 10, [{"top": [[1, 1.0]]}])

    def test_fresh_database_cleanup_runs_after_failure(self):
        admin, manager = MagicMock(), MagicMock()
        manager.__enter__.return_value = admin
        name = "pg_pinyin_native_0123456789abcdef"
        with patch.object(BENCH.BASE.psycopg, "connect", return_value=manager), \
                patch.object(BENCH.BASE, "new_database_name", return_value=name):
            with self.assertRaisesRegex(RuntimeError, "probe failed"):
                with BENCH.BASE.temporary_database("postgres://localhost/postgres") as database:
                    self.assertEqual(database, name)
                    raise RuntimeError("probe failed")
        statements = [str(call.args[0]) for call in admin.execute.call_args_list]
        self.assertEqual(len(statements), 2)
        self.assertIn("CREATE DATABASE", statements[0])
        self.assertIn("DROP DATABASE", statements[1])
        self.assertIn("WITH (FORCE)", statements[1])

    def test_namespace_cleanup_cannot_drop_an_application_database(self):
        admin = MagicMock()
        for database in ("postgres", "pg_pinyin_p1_0123456789abcdef", "pg_pinyin_native_bad",
                         "pg_pinyin_native_0123456789abcdef;DROP DATABASE postgres"):
            with self.subTest(database=database), self.assertRaises(ValueError):
                BENCH.BASE.drop_database(admin, database)
        admin.execute.assert_not_called()
        self.assertNotEqual(BENCH.BASE.new_database_name(), BENCH.BASE.new_database_name())

    def test_report_guard_preserves_existing_outputs_and_rejects_identical_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            json_path, markdown_path = root / "report.json", root / "report.md"
            BENCH.assert_report_paths_available(json_path, markdown_path)
            json_path.write_text("previous report", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "overwrite"):
                BENCH.assert_report_paths_available(json_path, markdown_path)
            self.assertEqual(json_path.read_text(encoding="utf-8"), "previous report")
            with self.assertRaisesRegex(ValueError, "distinct paths"):
                BENCH.assert_report_paths_available(markdown_path, root / "." / "report.md")

    def test_runtime_source_provenance_and_scope_caveats_are_explicit(self):
        source = inspect.getsource(BENCH)
        self.assertNotIn("dispatcher", source.lower())
        self.assertIn("benchmark_pinyin_phrase.py", source)
        main = inspect.getsource(BENCH.main)
        self.assertLess(main.index("assert_report_paths_available("), main.index("temporary_database("))
        for key in ("benchmark_script_sha256", "base_benchmark_script_sha256",
                    "shared_benchmark_utilities_sha256", "fixture_sha256", "runtime_source_sha256"):
            self.assertIn(key, main)
        self.assertIn("args.pg_search_version", main)
        self.assertIn("args.postgresql_version_pattern", main)
        self.assertIn('glob("*.rs")', main)
        setup = inspect.getsource(BENCH.setup)
        self.assertIn("pg_get_functiondef", setup)
        self.assertIn("pinyin_regex_phrase(text,integer,integer,boolean)", setup)
        self.assertIn("installed_query_functions", setup)
        report = inspect.getsource(BENCH.render_markdown)
        for caveat in ("scope retention, not relevance recall", "not cold OS",
                       "not a drop-in replacement", "not just top-k", "not representative"):
            self.assertIn(caveat, report)


if __name__ == "__main__":
    unittest.main()
