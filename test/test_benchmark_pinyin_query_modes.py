import importlib.util
import inspect
import json
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("p1_benchmark", ROOT / "scripts/benchmark_pinyin_query_modes.py")
BENCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BENCH)


class QueryModeBenchmarkProtocolTests(unittest.TestCase):
    def fixtures(self):
        return json.loads((ROOT / "test/fixtures/pinyin_query_modes.json").read_text(encoding="utf-8"))

    def test_gold_requires_independent_positive_and_negative_labels(self):
        fixtures = self.fixtures()
        BENCH.validate_fixtures(fixtures)
        self.assertIn("not generated", fixtures["description"])
        self.assertTrue(all(case["positive_ids"] and case["negative_ids"]
                            for case in fixtures["retrieval_queries"]))
        fixtures["retrieval_queries"][0]["negative_ids"] = [101]
        with self.assertRaisesRegex(ValueError, "disjoint"):
            BENCH.validate_fixtures(fixtures)

    def test_adversarial_gold_preserves_legacy_false_positives_as_negative_labels(self):
        fixtures = self.fixtures()
        legacy = {case["query"]: case for case in fixtures["retrieval_queries"] if case["mode"] == "compat"}
        self.assertIn(102, legacy["an"]["negative_ids"])
        self.assertIn(118, legacy["an"]["negative_ids"])
        self.assertIn(116, legacy["lv liang"]["negative_ids"])
        self.assertIn(112, legacy["zhan an"]["negative_ids"])
        full = {case["query"]: case for case in fixtures["retrieval_queries"] if case["mode"] == "full"}
        self.assertEqual(full["xi'an"]["positive_ids"], [105])
        self.assertEqual(full["xian"]["negative_ids"], [105])

    def test_compact_initials_and_explicit_digraphs_have_handwritten_patterns(self):
        cases = {case["label"]: case for case in self.fixtures()["pattern_cases"]}
        self.assertEqual(cases["initials compact whh"]["patterns"], ["w.*", "h.*", "h.*"])
        self.assertEqual(cases["initials compact zhh is per letter"]["patterns"], ["z.*", "h.*", "h.*"])
        self.assertEqual(cases["initials explicit digraph chunks"]["patterns"], ["zh.*", "ch.*", "sh.*"])
        self.assertEqual(cases["autocomplete only final prefix"]["patterns"], ["zhan", "a.*"])
        self.assertEqual(cases["initials normalized v is not a syllable prefix"]["patterns"], [])

    def test_corpus_is_hash_sampled_without_forcing_gold_at_low_ids(self):
        words = {"西安": "xi an", "仙": "xian", "银行": "yin hang", "吕梁": "lv liang"}
        corpus = BENCH.make_corpus(words, 4)
        self.assertEqual([row["name"] for row in corpus],
                         sorted(words,key=lambda word: (BENCH.digest(word.encode()),word)))
        self.assertEqual(corpus, BENCH.make_corpus(words, 4))
        self.assertEqual([row["id"] for row in corpus], [1,2,3,4])
        for row in corpus:
            self.assertEqual(row["plain"],words[row["name"]])
            self.assertEqual(row["mixed"]," ".join(f"|{token}|"
                                                      for token in row["plain"].split()))
        self.assertNotIn("fixture", inspect.getsource(BENCH.make_corpus).split("def make_corpus",1)[1].split("return [",1)[1])

    def test_synthetic_mode_queries_are_explicitly_scoped(self):
        words = {"武汉汉": "wu han han", "西安": "xi an"}
        corpus = BENCH.make_corpus(words,2)
        queries = BENCH.make_queries(corpus,2)
        self.assertTrue(all(query["kind"] == "synthetic_dictionary_derived" for query in queries))
        self.assertEqual(BENCH.query_inputs("wu han han"),
                         {"compat": "wu han han", "full": "wu han han",
                          "initials": "w h h", "autocomplete": "wu han ha"})
        self.assertEqual(BENCH.query_inputs("an")["autocomplete"], "a")
        with self.assertRaises(ValueError):
            BENCH.query_inputs(" ".join(["an"] * 65))
        with self.assertRaises(ValueError):
            BENCH.make_queries(corpus,3)

    def test_all_backend_mode_storage_combinations_rotate_without_omission(self):
        first = BENCH.rotated_methods(0)
        self.assertEqual(len(first),16)
        for index in range(3):
            self.assertEqual(set(BENCH.rotated_methods(index)),set(first))
            self.assertEqual(len(BENCH.rotated_methods(index)),len(set(first)))
        self.assertNotEqual(first,BENCH.rotated_methods(1))
        self.assertNotEqual(first,BENCH.rotated_methods(2))

    def test_fresh_database_cleanup_runs_even_after_failure(self):
        admin = MagicMock()
        manager = MagicMock()
        manager.__enter__.return_value = admin
        name = "pg_pinyin_p1_0123456789abcdef"
        with patch.object(BENCH.psycopg,"connect",return_value=manager), patch.object(BENCH,"new_database_name",return_value=name):
            with self.assertRaisesRegex(RuntimeError,"probe failure"):
                with BENCH.temporary_database("postgres://example/postgres") as allocated:
                    self.assertEqual(allocated,name)
                    raise RuntimeError("probe failure")
        statements = [str(call.args[0]) for call in admin.execute.call_args_list]
        self.assertEqual(len(statements),2)
        self.assertIn("CREATE DATABASE",statements[0])
        self.assertIn("DROP DATABASE",statements[1])
        self.assertIn(name,statements[1])

    def test_cleanup_cannot_drop_unrelated_databases(self):
        admin = MagicMock()
        for name in ("postgres","pg_pinyin_p0_0123456789abcdef","pg_pinyin_p1_bad","pg_pinyin_p1_0123456789abcdef;drop"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                BENCH.drop_database(admin,name)
        admin.execute.assert_not_called()
        self.assertNotEqual(BENCH.new_database_name(),BENCH.new_database_name())

    def test_labeled_precision_does_not_count_unlabeled_returns_as_correct(self):
        result = {"positive_ids": [1],"negative_ids": [2,3],"returned_ids": [2,1,99]}
        metric = BENCH.labeled_metrics([result],10)
        self.assertEqual(metric["precision_on_labeled"],0.5)
        self.assertEqual(metric["false_positive_hits"],1)
        self.assertEqual(metric["false_positive_rate_on_labeled_negatives"],0.5)
        self.assertEqual(metric["recall_at_10"],1)
        self.assertEqual(metric["mrr"],0.5)
        self.assertEqual(metric["unlabeled_returned"],1)

    def test_matched_contrasts_use_identical_query_and_label_sets(self):
        gold = {}
        for backend in BENCH.BACKENDS:
            for storage in BENCH.STORAGES:
                gold[BENCH.method_key(backend,"compat",storage)] = {"results": [
                    {"query": "an","positive_ids": [1],"negative_ids": [2],"returned_ids": [2,1]}]}
                gold[BENCH.method_key(backend,"full",storage)] = {"results": [
                    {"query": "an","positive_ids": [1],"negative_ids": [2,3],"returned_ids": [1]},
                    {"query": "another","positive_ids": [4],"negative_ids": [5],"returned_ids": [4]}]}
        groups = BENCH.matched_contrast_metrics(gold,10)
        for group in groups.values():
            self.assertEqual(group["metrics"]["compat"]["queries"],1)
            self.assertEqual(group["metrics"]["full"]["queries"],1)
            self.assertEqual(group["metrics"]["compat"]["precision_on_labeled"],0.5)
            self.assertEqual(group["metrics"]["full"]["precision_on_labeled"],1)
            self.assertEqual(group["cases"]["compat"][0]["negative_ids"],[2,3])
            self.assertEqual(group["cases"]["full"][0]["negative_ids"],[2,3])

    def test_shared_empty_patterns_or_results_cannot_fake_backend_parity(self):
        for outputs in ([],[[]],[None],[[1],[]]):
            with self.subTest(outputs=outputs), self.assertRaisesRegex(RuntimeError,"Empty performance output"):
                BENCH.require_nonempty_outputs(outputs,"probe")
        BENCH.require_nonempty_outputs([[1],[2]],"probe")

    def test_autocomplete_prefix_has_one_source_position_for_yong_and_long(self):
        self.assertEqual(BENCH.expected_autocomplete_patterns("yong","plain"),["yon.*"])
        self.assertEqual(BENCH.expected_autocomplete_patterns("ni long","plain"),["ni","lon.*"])
        self.assertEqual(BENCH.expected_autocomplete_patterns("ni yong","mixed"),
                         ["(ni|.*\\|ni\\|.*)","(yon.*|.*\\|yon[^|]*\\|.*)"])
        reference = BENCH.validate_autocomplete_outputs(
            [(1,"yong",["yon.*"]),(2,"ni long",["ni","lon.*"])],"plain")
        self.assertEqual(reference["verified_rows"],2)
        self.assertEqual(reference["expected_sha256"],reference["actual_sha256"])

    def test_shared_nonempty_autocomplete_missegmentation_cannot_pass(self):
        for reading,patterns in (("yong",["yo","n.*"]),
                                 ("ni long",["ni","lo","n.*"]),
                                 ("ni yong",["ni","yo.*"])):
            with self.subTest(reading=reading), self.assertRaisesRegex(RuntimeError,"source syllable positions"):
                BENCH.validate_autocomplete_outputs([(1,reading,patterns)],"plain")
        with self.assertRaisesRegex(RuntimeError,"source syllable positions"):
            BENCH.validate_autocomplete_outputs(
                [(1,"ni yong",["(ni|.*\\|ni\\|.*)","(yo|.*\\|yo\\|.*)","(n.*|.*\\|n[^|]*\\|.*)"])],
                "mixed")

    def test_autocomplete_yon_lon_gold_has_independent_split_negatives(self):
        fixtures = self.fixtures()
        cases = {case["label"]: case for case in fixtures["pattern_cases"]}
        self.assertEqual(cases["autocomplete joined niyon"]["patterns"],["ni","yon.*"])
        self.assertEqual(cases["autocomplete explicit ni yo n stays three positions"]["patterns"],
                         ["ni","yo","n.*"])
        queries = {case["query"]: case for case in fixtures["retrieval_queries"] if case["mode"] == "autocomplete"}
        self.assertEqual(queries["yon"]["positive_ids"],[123,127])
        self.assertEqual(queries["yon"]["negative_ids"],[125,129])
        self.assertEqual(queries["lon"]["positive_ids"],[124,128])
        self.assertEqual(queries["niyon"]["negative_ids"],[129,128])

    def test_setup_captures_effective_timing_settings(self):
        source = inspect.getsource(BENCH.setup)
        self.assertLess(source.index("configure(cursor)"),source.index("SELECT name,setting,unit"))

    def test_real_search_uses_mode_api_and_pipe_preserving_index_expression(self):
        statement = BENCH.search_statement("rust","mixed")
        self.assertIn("WHERE mixed @@@",statement)
        self.assertNotIn("::pdb.whitespace", statement)
        self.assertIn("pinyin_regex_phrase_mode(%s,%s,NULL,NULL,%s)",statement)
        self.assertIn("ORDER BY pdb.score(id) DESC,id LIMIT",statement)
        self.assertIn("sql_pinyin_regex_phrase_mode",BENCH.search_statement("sql","plain","bench_gold"))
        with self.assertRaises(ValueError):
            BENCH.search_statement("rust","arbitrary_column")

    def test_timed_ranked_ids_must_match_untimed_ids_not_just_count(self):
        conn_manager,cursor_manager,cursor = MagicMock(),MagicMock(),MagicMock()
        conn_manager.__enter__.return_value.cursor.return_value = cursor_manager
        cursor_manager.__enter__.return_value = cursor
        cursor.fetchall.side_effect = [[(1,)],[(2,)]]
        query = {"inputs": {"full": "an"}}
        with patch.object(BENCH,"connection",return_value=conn_manager):
            with self.assertRaisesRegex(RuntimeError,"result IDs differ"):
                BENCH.measure_search("url","database",[query],"rust","full","plain",10,[[1]])

    def test_report_namespace_and_semantics_caveats_are_separate_from_p0(self):
        source = inspect.getsource(BENCH.main)
        self.assertIn("benchmarks/p1_query_modes",source)
        self.assertNotIn("p0_dictionary",source)
        report_source = inspect.getsource(BENCH.render_markdown)
        self.assertIn("not general pronunciation accuracy",report_source)
        self.assertIn("No stopword filtering or index-reduction claim",report_source)
        self.assertIn("Precision is calculated only over explicitly labeled",report_source)
        self.assertIn("different protocol from P0",report_source)
        self.assertIn("pipe-wrapped single-reading",report_source)
        self.assertIn("one shared dual-field index",report_source)
        self.assertIn("visible within top-k only",report_source)


if __name__ == "__main__":
    unittest.main()
