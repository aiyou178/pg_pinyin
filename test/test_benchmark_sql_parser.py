import gzip
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("standalone_sql_parser_benchmark",
                                          ROOT / "scripts/benchmark_sql_parser.py")
BENCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BENCH)


class StandaloneSqlParserBenchmarkTests(unittest.TestCase):
    def test_baseline_is_pinned_and_rejects_moving_or_option_refs(self):
        self.assertEqual(len(BENCH.BASELINE), 40)
        for value in ("main", "origin/main", "--help", "abc;drop", "a" * 41):
            with self.subTest(value=value), self.assertRaises(ValueError):
                BENCH.acquire_sources(value)
        with patch.object(BENCH, "git", return_value="wrong"):
            with self.assertRaisesRegex(ValueError, "full baseline"):
                BENCH.acquire_sources("abcdef1")

    def test_acquires_each_source_from_resolved_commit_not_checkout_baseline(self):
        head = "a" * 40
        with patch.object(BENCH, "git", side_effect=[head, "b" * 40, " M sql/word.sql"]), \
                patch.object(BENCH.subprocess, "check_output", return_value="old SQL") as show:
            sources, provenance = BENCH.acquire_sources("abcdef1")
        self.assertEqual(provenance["baseline_commit"], head)
        self.assertTrue(provenance["current_worktree_dirty"])
        self.assertEqual(sources["baseline"]["sql/word.sql"], "old SQL")
        self.assertEqual([call.args[0][-1] for call in show.call_args_list],
                         [f"{head}:{path}" for path in BENCH.SQL_FILES])

    def test_sources_capture_exact_dollar_quoted_body(self):
        source = "CREATE OR REPLACE FUNCTION public.demo(value text) RETURNS text LANGUAGE sql AS $$\n SELECT value;\n$$;"
        functions = BENCH.source_functions({"sql/word.sql": source})
        self.assertEqual(functions[0]["body"], "\n SELECT value;\n")
        self.assertEqual(functions[0]["name"], "public.demo")
        with self.assertRaises(ValueError):
            BENCH.source_functions({"empty": "-- nothing"})

    def test_installed_missing_or_native_body_fails_before_measurement(self):
        source = "CREATE OR REPLACE FUNCTION public.demo() RETURNS text LANGUAGE sql AS $$SELECT 'x'$$;"
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        with self.assertRaisesRegex(RuntimeError, "does not match source"):
            BENCH.installed_definitions(cursor, {"file": source})
        cursor.fetchall.return_value = [("public.demo()", "SELECT 'x'", "definition", "c", "i", "s", False)]
        with self.assertRaisesRegex(RuntimeError, "native-language"):
            BENCH.installed_definitions(cursor, {"file": source})

    def test_catalog_flags_are_checked_against_source_not_only_body(self):
        source = "CREATE OR REPLACE FUNCTION public.demo() RETURNS text LANGUAGE sql STABLE PARALLEL SAFE AS $$SELECT 'x'$$;"
        cursor = MagicMock()
        cursor.fetchall.return_value = [("public.demo()", "SELECT 'x'", "definition", "sql", "v", "s", False)]
        with self.assertRaisesRegex(RuntimeError, "catalog flags"):
            BENCH.installed_definitions(cursor, {"file": source})
        cursor.fetchall.return_value = [("public.demo()", "SELECT 'x'", "definition", "sql", "s", "s", False)]
        result = BENCH.installed_definitions(cursor, {"file": source})
        self.assertIn("public.demo()", result)

    def test_owned_database_is_dropped_even_when_work_fails(self):
        admin, manager = MagicMock(), MagicMock()
        manager.__enter__.return_value = admin
        name = BENCH.NAMESPACE + "0123456789abcdef"
        with patch.object(BENCH.psycopg, "connect", return_value=manager), \
                patch.object(BENCH, "new_database_name", return_value=name):
            with self.assertRaisesRegex(RuntimeError, "failed"):
                with BENCH.temporary_database("dsn") as allocated:
                    self.assertEqual(allocated, name)
                    raise RuntimeError("failed")
        statements = [str(call.args[0]) for call in admin.execute.call_args_list]
        self.assertEqual(len(statements), 2)
        self.assertIn("TEMPLATE template0", statements[0])
        self.assertIn("DROP DATABASE", statements[1])
        self.assertIn("WITH (FORCE)", statements[1])

    def test_cleanup_refuses_unowned_or_malformed_database(self):
        admin = MagicMock()
        for name in ("postgres", "pg_pinyin_p1_0123456789abcdef", BENCH.NAMESPACE + "oops",
                     BENCH.NAMESPACE + "0123456789abcdef;DROP"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                BENCH.drop_database(admin, name)
        admin.execute.assert_not_called()

    def test_rotation_has_complete_same_mode_old_new_pairs(self):
        expected = {(implementation, mode) for implementation in BENCH.IMPLEMENTATIONS for mode in BENCH.MODES}
        orders = [BENCH.rotated_methods(index) for index in range(3)]
        for order in orders:
            self.assertEqual(set(order), expected)
            self.assertEqual(len(order), len(expected))
        self.assertEqual(len({tuple(order) for order in orders}), 3)
        for mode in BENCH.MODES:
            baseline_first = {order.index(("baseline", mode)) < order.index(("current", mode))
                              for order in orders}
            self.assertEqual(baseline_first, {True, False}, f"biased measurement order: {mode}")

    def test_small_validation_runs_supported_but_invalid_sizes_rejected(self):
        BENCH.validate_configuration(20, 10, 1, 3)
        for values in ((0, 1, 3, 10), (20, 21, 3, 10), (20, 2, 0, 10), (20, 2, 3, 0), (True, 1, 3, 10)):
            with self.subTest(values=values), self.assertRaises(ValueError):
                BENCH.validate_configuration(*values)

    def test_actual_pattern_and_query_outputs_are_materialized_and_consumed(self):
        for stage, function in (("patterns", "sql_pinyin_regex_phrase_patterns_mode"),
                                ("constructor", "sql_pinyin_regex_phrase_mode")):
            statement = BENCH.aggregate_statement(stage, "full")
            self.assertIn("AS MATERIALIZED", statement)
            self.assertIn(function + "(value,'full'", statement)
            self.assertEqual(statement.count(function + "("), 1)
            self.assertIn("sum(octet_length(output))", statement)
            self.assertIn("string_agg(output", statement)
            self.assertIn("count(*) FILTER (WHERE output IS NULL)", statement)
        with self.assertRaises(ValueError):
            BENCH.aggregate_statement("search", "full")

    def test_compat_uses_direct_legacy_function_not_mode_dispatch_overhead(self):
        self.assertEqual(BENCH.pattern_expression("value", "compat"),
                         "public.sql_pinyin_regex_phrase_patterns(value,false)")
        self.assertEqual(BENCH.query_expression("value", "compat"),
                         "public.sql_pinyin_regex_phrase(value,NULL,NULL,false)")
        self.assertNotIn("patterns_mode", BENCH.aggregate_statement("patterns", "compat"))
        self.assertNotIn("phrase_mode", BENCH.search_statement("compat"))

    def test_measured_search_uses_ranking_and_all_hit_verification_is_unlimited(self):
        ranked = BENCH.search_statement("full")
        self.assertIn("ORDER BY pdb.score(id) DESC,id LIMIT %s", ranked)
        self.assertIn("sql_pinyin_regex_phrase_mode(%s,'full',NULL,NULL,false)", ranked)
        all_hits = BENCH.search_statement("full", all_hits=True)
        self.assertNotIn("LIMIT", all_hits)
        self.assertIn("ORDER BY id", all_hits)
        with self.assertRaises(ValueError):
            BENCH.search_statement("full", "untrusted;drop")
        with self.assertRaises(ValueError):
            BENCH.query_expression("untrusted")

    def test_checksum_and_equality_cover_exact_scores_and_order(self):
        left = BENCH.canonical_rows([(1, 1.0), (2, 2.0)])
        self.assertEqual(BENCH.sha256(left), BENCH.sha256(BENCH.canonical_rows([[1, 1], [2, 2]])))
        # JSON int/float normalization is explicit before generating checksums.
        self.assertEqual(left, [[1, 1.0], [2, 2.0]])
        for right in ([[1, 1.1], [2, 2.0]], [[2, 2.0], [1, 1.0]], [[1, 1.0]]):
            with self.assertRaisesRegex(RuntimeError, "parity failed"):
                BENCH.assert_equal(left, right, "IDs and scores")
        for rows in ([(1, float("nan"))], [(1, 1), (1, 2)], [("1", 1)]):
            with self.assertRaises(ValueError):
                BENCH.canonical_rows(rows)

    def test_independent_oracle_preserves_positions_and_token_boundaries(self):
        documents = [{"id": 1, "plain": "xi an"}, {"id": 2, "plain": "xi ang"},
                     {"id": 3, "plain": "xian"}, {"id": 4, "plain": "xi hao an"}]
        self.assertEqual(BENCH.positional_oracle(documents, ["xi", "an"]), [1])
        self.assertEqual(BENCH.positional_oracle(documents, ["xi", "a.*"]), [1, 2])
        self.assertEqual(BENCH.positional_oracle(documents, []), [])
        with self.assertRaises(ValueError):
            BENCH.positional_oracle(documents, [".*\\|an.*"])

    def test_real_top_k_plan_required_not_any_custom_scan(self):
        scan = {"Node Type": "Custom Scan", "Custom Plan Provider": "ParadeDB Base Scan",
                "Exec Method": "TopKScanExecState"}
        BENCH.verify_top_k_plan([{"Plan": {"Node Type": "Limit", "Plans": [scan]}}])
        scan["Exec Method"] = "NormalScanExecState"
        with self.assertRaisesRegex(RuntimeError, "Top-K"):
            BENCH.verify_top_k_plan([{"Plan": scan}])

    def test_auto_workload_mixes_full_initials_and_completion_reproducibly(self):
        corpus = BENCH.COMMON.make_corpus({"西安": "xi an", "武汉": "wu han", "北京": "bei jing"}, 3)
        queries = BENCH.make_queries(corpus, 3)
        self.assertEqual(queries, BENCH.make_queries(corpus, 3))
        for index, query in enumerate(queries):
            self.assertEqual(query["inputs"]["auto"], query["inputs"][("full", "initials", "autocomplete")[index]])

    def test_no_overwrite_and_deterministic_gzip(self):
        with tempfile.TemporaryDirectory() as directory:
            first, second = Path(directory) / "report.json.gz", Path(directory) / "report.md"
            BENCH.assert_report_paths_available(first, second)
            BENCH.write_report(first, b"raw evidence", True)
            self.assertEqual(gzip.decompress(first.read_bytes()), b"raw evidence")
            self.assertEqual(first.read_bytes()[4:8], b"\0\0\0\0")
            with self.assertRaises(ValueError):
                BENCH.assert_report_paths_available(first, second)
            with self.assertRaises(FileExistsError):
                BENCH.write_report(first, b"replacement", True)
            with self.assertRaises(ValueError):
                BENCH.assert_report_paths_available(second, second)

    def samples(self):
        return [{"round": index, "implementation": implementation, "mode": mode,
                 **{stage: {"server_execution_ms": 2 if implementation == "baseline" else 1,
                             "elapsed_ms": 4 if implementation == "baseline" else 2,
                             "first_fresh_backend_call_ms": 3} for stage in BENCH.STAGES}}
                for index in range(3) for implementation in BENCH.IMPLEMENTATIONS for mode in BENCH.MODES]

    def test_summary_requires_unique_complete_rounds_and_uses_separate_stage_timings(self):
        rows = self.samples()
        result = BENCH.summarize(rows, 3)
        self.assertEqual(result["auto"]["patterns"]["baseline"]["median_ms"], 2)
        self.assertEqual(result["auto"]["search"]["baseline"]["median_ms"], 4)
        self.assertEqual(result["auto"]["search"]["speedup"], 2)
        for wrong in (rows[:-1], [*rows, rows[0]], [{**row, "round": 0} for row in rows]):
            with self.assertRaises(ValueError):
                BENCH.summarize(wrong, 3)


if __name__ == "__main__":
    unittest.main()
