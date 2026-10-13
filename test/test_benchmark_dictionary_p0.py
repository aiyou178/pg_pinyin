import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("p0_benchmark", ROOT / "scripts/benchmark_dictionary_p0.py")
BENCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BENCH)


class BenchmarkProtocolTests(unittest.TestCase):
    def test_corpus_has_equal_unique_union_inputs_and_is_stable(self):
        fixtures = [{"text": "西安", "syllables": ["xi", "an"], "kind": "boundary"}]
        old = {"words": "西安,xian\n善感,shang an\n阿奶,an ai\n旧词,jiu ci\n".encode()}
        new = {"words": "阿奶,a nai\n善感,shan gan\n西安,xi an\n新词,xin ci\n".encode()}
        corpus, _ = BENCH.make_corpus(old, new, fixtures, 5)
        self.assertEqual(len(corpus), 5)
        self.assertEqual({word for _, word in corpus}, {"西安", "善感", "阿奶", "旧词", "新词"})
        self.assertEqual(corpus, BENCH.make_corpus(old, new, fixtures, 5)[0])

    def test_restored_and_candidate_only_strata_are_explicitly_sampled(self):
        retained = {f"词{index}": "ci" for index in range(100)}
        old_words = {**retained, "补回": "bu hui"}
        new_words = {**old_words, "新增": "xin zeng"}
        old = {"words": "".join(f"{word},{reading}\n" for word, reading in old_words.items()).encode()}
        new = {"words": "".join(f"{word},{reading}\n" for word, reading in new_words.items()).encode()}
        fixtures = [{"text": "词0", "syllables": ["ci"], "kind": "control"}]
        corpus, candidate = BENCH.make_corpus(old, new, fixtures, 8, {"补回"})
        words = {word for _, word in corpus}
        self.assertTrue({"补回", "新增"} <= words)
        categories = BENCH.coverage_categories(old_words, new_words, {"补回"})
        queries = BENCH.make_queries(corpus, candidate, fixtures, 8, categories)
        coverage = BENCH.corpus_coverage(old_words, new_words, corpus, categories, queries)
        self.assertEqual(coverage["restored_baseline_words"], 1)
        self.assertEqual(coverage["strata"]["shared_retained"]["dictionary_words"], 100)
        self.assertEqual(coverage["strata"]["shared_restored"]["corpus_words"], 1)
        self.assertEqual(coverage["strata"]["shared_restored"]["queries"]["synthetic_dictionary_derived"], 1)
        self.assertEqual(coverage["strata"]["candidate_only"]["queries"]["curated"], 0)
        synthetic = {query["text"]: query for query in queries if query["kind"] != "curated"}
        self.assertEqual(synthetic["补回"]["coverage_category"], "shared_restored")
        self.assertEqual(synthetic["新增"]["kind"], "synthetic_dictionary_derived")

    def test_fixture_ids_do_not_systematically_win_tied_rankings(self):
        words = ["西安", "善感", "阿奶", "旧词", "新词"]
        fixture_word = max(words, key=lambda word: BENCH.digest(word.encode()))
        data = {"words": "".join(f"{word},ci\n" for word in words).encode()}
        fixture = {"text": fixture_word, "syllables": ["ci"], "kind": "control"}
        corpus, _ = BENCH.make_corpus(data, data, [fixture], len(words))
        self.assertEqual(dict((word, ident) for ident, word in corpus)[fixture_word], len(words))
        self.assertEqual([word for _, word in corpus],
                         sorted(words, key=lambda word: (BENCH.digest(word.encode()), word)))

    def test_baseline_only_words_are_not_hidden_from_query_metrics(self):
        old = {"保留": "bao liu", "遗漏": "yi lou"}
        new = {"保留": "bao liu"}
        fixture = {"text": "保留", "syllables": ["bao", "liu"], "kind": "control"}
        categories = BENCH.coverage_categories(old, new, set())
        queries = BENCH.make_queries([(1, "保留"), (2, "遗漏")], new, [fixture], 2, categories, old)
        self.assertEqual(queries[1]["coverage_category"], "baseline_only")
        self.assertEqual(queries[1]["query"], "yi lou")
        self.assertEqual(queries[1]["reading_source"], "baseline_dictionary")

    def test_restore_audit_cannot_classify_new_or_missing_words_as_restored(self):
        for restored in ({"新增"}, {"缺失"}):
            with self.subTest(restored=restored), self.assertRaisesRegex(ValueError, "both benchmark dictionaries"):
                BENCH.coverage_categories({"保留": "bao liu"},
                                         {"保留": "bao liu", "新增": "xin zeng"}, restored)

    def test_sampling_handles_empty_minimal_and_full_pools(self):
        self.assertEqual(BENCH.sample_strata({}, 0), [])
        self.assertEqual(BENCH.sample_strata({"one": ["词"]}, 1), ["词"])
        self.assertEqual(set(BENCH.sample_strata({"one": ["词"], "two": ["字"]}, 2)), {"词", "字"})
        self.assertEqual(len(BENCH.sample_strata({"one": ["词"], "two": ["字"]}, 1)), 1)
        with self.assertRaises(ValueError):
            BENCH.sample_strata({"one": ["词"]}, 2)

    def test_restore_audit_must_be_a_unique_string_list(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "coverage_audit.json"
            path.write_text(json.dumps({"restored_words": ["补回"]}))
            self.assertEqual(BENCH.load_restored_words(path), {"补回"})
            for words in (["重复", "重复"], [1], "补回"):
                path.write_text(json.dumps({"restored_words": words}))
                with self.subTest(words=words), self.assertRaises(ValueError):
                    BENCH.load_restored_words(path)

    def test_curated_gold_is_not_generated_from_candidate_dictionary(self):
        fixture = {"text": "西安", "syllables": ["xi", "an"], "kind": "boundary"}
        queries = BENCH.make_queries([(1, "西安"), (2, "善感")],
                                    {"西安": "xian", "善感": "shan gan"}, [fixture], 2)
        self.assertEqual(queries[0]["query"], "xi an")
        self.assertEqual(queries[0]["relevant_ids"], [1])
        self.assertEqual(queries[0]["kind"], "curated")
        self.assertEqual(queries[1]["kind"], "synthetic_dictionary_derived")

    def test_ranked_metrics_not_aggregate_counts(self):
        results = [{"kind": "curated", "coverage_category": "shared_restored", "relevant_ids": [1], "returned_ids": [2, 1]},
                   {"kind": "curated", "coverage_category": "shared_retained", "relevant_ids": [3], "returned_ids": [2, 4]}]
        summary = BENCH.relevance_summary(results, 2)["curated"]
        self.assertEqual(summary["recall_at_2"], 0.5)
        self.assertEqual(summary["mrr"], 0.25)
        restored = BENCH.relevance_summary(results, 2)["curated:shared_restored"]
        self.assertEqual(restored["queries"], 1)
        self.assertEqual(restored["mrr"], 0.5)

    def test_quantiles_use_interpolation(self):
        self.assertEqual(BENCH.percentile([10, 0, 5], 0.5), 5)
        self.assertEqual(BENCH.percentile([10, 0], 0.95), 9.5)


if __name__ == "__main__":
    unittest.main()
