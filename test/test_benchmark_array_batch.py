import importlib.util
import inspect
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("p2_benchmark", ROOT / "scripts/benchmark_array_batch.py")
BENCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BENCH)


class ArrayBatchBenchmarkProtocolTests(unittest.TestCase):
    def test_bounded_chunks_preserve_order_nulls_and_each_document_once(self):
        documents = ["银", None, "行", "", "银行"]
        for size in (1, 2, 3, 32):
            chunks = BENCH.bounded_chunks(documents, size)
            flattened = [value for _, batch in chunks for value in batch]
            self.assertEqual(flattened, documents)
            self.assertTrue(all(len(batch) <= size for _, batch in chunks))
            ids = [start + offset for start, batch in chunks for offset in range(len(batch))]
            self.assertEqual(ids, [1, 2, 3, 4, 5])
        self.assertEqual(BENCH.bounded_chunks([], 1), [])
        with self.assertRaises(ValueError):
            BENCH.bounded_chunks(documents, 0)

    def test_corpus_is_deterministic_han_only_with_precomputed_character_tokens(self):
        words = {"银行": "yin hang", "吕梁": "lv liang", "ABC": "abc", "未知": "wei zhi", "银行A": "yin hang a"}
        characters = {char: "value" for char in "银行吕梁"}
        corpus = BENCH.make_corpus(words, characters, 2)
        self.assertEqual([row["document"] for row in corpus],
                         sorted(["银行", "吕梁"], key=lambda word: (BENCH.digest(word.encode()), word)))
        self.assertEqual(corpus, BENCH.make_corpus(words, characters, 2))
        for row in corpus:
            self.assertEqual(row["tokens"], list(row["document"]))
            self.assertEqual(row["expected"], words[row["document"]])
        with self.assertRaises(ValueError):
            BENCH.make_corpus(words, characters, 3)

    def test_rotated_methods_cover_real_old_and_new_methods_without_duplicates(self):
        sizes = (1, 32, 128, 512, 2048)
        first = BENCH.rotated_methods(0, sizes)
        self.assertEqual(len(first), 9)
        self.assertIn("legacy_tokens", first)
        self.assertIn("current_batch_2048", first)
        for round_index in range(3):
            current = BENCH.rotated_methods(round_index, sizes)
            self.assertEqual(set(current), set(first))
            self.assertEqual(len(current), len(set(current)))
        self.assertNotEqual(first, BENCH.rotated_methods(1, sizes))
        for sizes in ((), (1, 1), (0, 2), (8193,)):
            with self.subTest(sizes=sizes), self.assertRaises(ValueError):
                BENCH.all_methods(sizes)

    def test_real_baseline_requires_matching_compiled_library_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            library = directory / "pg_pinyin_baseline_0_0_9.so"
            old_sql = directory / "pg_pinyin--0.0.9.sql"
            manifest = directory / "baseline.json"
            library.write_bytes(b"different real compiled library fixture")
            old_sql.write_text("pinyin_word_romanize_wrapper pinyin_word_romanize_with_tokenizer_wrapper")
            declaration = {"source_commit": BENCH.BASELINE_COMMIT,
                           "extension_version": "0.0.9",
                           "build_profile": "release", "rust_version": "rustc 1.98.1 (fixture)",
                           "library_sha256": BENCH.digest(library.read_bytes()),
                           "installation_sql_sha256": BENCH.digest(old_sql.read_bytes())}
            manifest.write_text(json.dumps(declaration))
            result = BENCH.baseline_provenance(library, old_sql, manifest)
            self.assertEqual(result["source_commit"], "f1094beeb69c093085403f5b02d616a4de7adabe")
            self.assertEqual(result["build_profile"], "release")
            declaration["build_profile"] = "debug"
            manifest.write_text(json.dumps(declaration))
            with self.assertRaisesRegex(RuntimeError, "release profile"):
                BENCH.baseline_provenance(library, old_sql, manifest)
            declaration["build_profile"] = "release"
            manifest.write_text(json.dumps(declaration))
            library.write_bytes(b"collision or wrong cached build")
            with self.assertRaisesRegex(RuntimeError, "manifest mismatch"):
                BENCH.baseline_provenance(library, old_sql, manifest)
            manifest.unlink()
            with self.assertRaisesRegex(RuntimeError, "Real rebuilt pre-optimization P1"):
                BENCH.baseline_provenance(library, old_sql, manifest)

    def test_baseline_sql_with_new_batch_api_is_rejected_even_with_matching_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            library, old_sql, manifest = (directory / name for name in ("old.so", "old.sql", "old.json"))
            library.write_bytes(b"old")
            old_sql.write_text("pinyin_word_romanize_wrapper pinyin_word_romanize_with_tokenizer_wrapper pinyin_word_romanize_batch")
            manifest.write_text(json.dumps({"source_commit": BENCH.BASELINE_COMMIT,
                "extension_version": "0.0.9", "library_sha256": BENCH.digest(library.read_bytes()),
                "build_profile": "release", "rust_version": "rustc 1.98.1 (fixture)",
                "installation_sql_sha256": BENCH.digest(old_sql.read_bytes())}))
            with self.assertRaisesRegex(RuntimeError, "pre-batch Rust API"):
                BENCH.baseline_provenance(library, old_sql, manifest)

    def test_old_aliases_bind_separate_c_module_and_exact_old_symbols(self):
        cursor = MagicMock()
        module = "/lib/pg_pinyin_baseline_0_0_9.so"
        cursor.fetchall.return_value = [
            (name, module, symbol, "c") for name, (_, symbol) in BENCH.LEGACY_SYMBOLS.items()]
        aliases = BENCH.create_legacy_aliases(cursor, {"library_path": module})
        self.assertEqual(len(aliases), 2)
        declarations = " ".join(str(call.args[0]) for call in cursor.execute.call_args_list[:2])
        self.assertIn("LANGUAGE c", declarations)
        self.assertIn("pinyin_word_romanize_with_tokenizer_wrapper", declarations)
        self.assertNotIn("SELECT public.pinyin_word_romanize", declarations)
        cursor.fetchall.return_value = [("bench_legacy_scalar", "$libdir/pg_pinyin", "pinyin_word_romanize_wrapper", "c")]
        with self.assertRaisesRegex(RuntimeError, "real old C module"):
            BENCH.create_legacy_aliases(cursor, {"library_path": module})

    def test_current_installed_module_and_sql_are_fingerprinted_separately_from_source(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            extension = directory / "extension"
            extension.mkdir()
            (directory / "pg_pinyin.so").write_bytes(b"current compiled fixture")
            (directory / "release").mkdir()
            build_artifact = directory / "release/libpg_pinyin.so"
            build_artifact.write_bytes(b"current compiled fixture")
            (extension / "pg_pinyin--0.0.9.sql").write_text("current installation SQL fixture")
            cursor = MagicMock()
            cursor.fetchall.return_value = [
                ("pinyin_word_romanize(text)", "$libdir/pg_pinyin", "pinyin_word_romanize_wrapper", "i", "s", True),
                ("pinyin_word_romanize(anyelement)", "$libdir/pg_pinyin", "pinyin_word_romanize_with_tokenizer_wrapper", "i", "s", True),
                ("pinyin_word_romanize_batch(text[])", "$libdir/pg_pinyin", "pinyin_word_romanize_batch_wrapper", "s", "s", True),
            ]
            with patch.object(BENCH.subprocess, "check_output", return_value=str(directory)):
                artifact = BENCH.current_artifacts(cursor, build_artifact, "rustc 1.98.1 (fixture)")
            self.assertEqual(artifact["library_sha256"], BENCH.digest(b"current compiled fixture"))
            self.assertEqual(artifact["installation_sql_sha256"], BENCH.digest(b"current installation SQL fixture"))
            self.assertEqual(len(artifact["catalog"]), 3)
            self.assertEqual(artifact["build_profile"], "release")
            build_artifact.write_bytes(b"wrong cached current build")
            with patch.object(BENCH.subprocess, "check_output", return_value=str(directory)):
                with self.assertRaisesRegex(RuntimeError, "declared release build artifact"):
                    BENCH.current_artifacts(cursor, build_artifact, "rustc 1.98.1 (fixture)")
            cursor.fetchall.return_value[2] = (*cursor.fetchall.return_value[2][:3], "i", "s", True)
            with self.assertRaisesRegex(RuntimeError, "catalog properties"):
                BENCH.current_artifacts(cursor, build_artifact, "rustc 1.98.1 (fixture)")

    def test_same_development_version_requires_distinct_dll_and_same_compiler(self):
        self.assertEqual(BENCH.CURRENT_VERSION, "0.0.9")
        self.assertEqual(BENCH.BASELINE_VERSION, "0.0.9")
        baseline = {"rust_version": "rustc fixture", "library_sha256": "old"}
        current = {"rust_version": "rustc fixture", "library_sha256": "new"}
        BENCH.require_comparable_artifacts(current, baseline)
        with self.assertRaisesRegex(RuntimeError, "identical to the pre-optimization"):
            BENCH.require_comparable_artifacts(baseline, baseline)
        with self.assertRaisesRegex(RuntimeError, "same recorded Rust compiler"):
            BENCH.require_comparable_artifacts({**current, "rust_version": "rustc different"}, baseline)
        with patch.object(BENCH.subprocess, "check_output", return_value="/pg/lib"):
            library, old_sql, manifest = BENCH.baseline_paths(None, None, None)
        self.assertEqual(old_sql, Path("/pg/lib/pg_pinyin_baseline_0_0_9.sql"))
        self.assertEqual(library.parent, old_sql.parent)
        self.assertEqual(manifest.parent, old_sql.parent)

    def test_output_forcing_checks_content_ids_bytes_and_not_just_count(self):
        expected = [(1, "yin"), (2, "xing")]
        signature = BENCH.output_signature(expected)
        self.assertEqual(signature[:2], (2, 7))
        result = BENCH.verify_outputs(expected, expected, "probe")
        self.assertEqual(result["verified_rows"], 2)
        self.assertNotEqual(BENCH.output_signature([(1, "yin"), (2, "hang")]), signature)
        for actual in ([], [(1, "")], [(1, None)], [(2, "yin")], [(1, "yin"), (1, "xing")]):
            with self.subTest(actual=actual), self.assertRaises(RuntimeError):
                BENCH.output_signature(actual)
        with self.assertRaisesRegex(RuntimeError, "differs from reference"):
            BENCH.verify_outputs([(1, "yin"), (2, "hang")], expected, "cross document")
        statement = BENCH.forced_statement("SELECT id,output FROM source")
        self.assertIn("MATERIALIZED", statement)
        self.assertIn("sum(octet_length(output))", statement)
        self.assertIn("md5(string_agg(id::text", statement)
        self.assertIn("ORDER BY id", statement)

    def test_timed_aggregate_checksum_mismatch_is_fatal(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = (2, 7, "wrong outputs")
        with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
            BENCH.checked_forced_call(cursor, "SELECT probe", (), (2, 7, "expected"))

    def test_batch_conversion_uses_precomputed_chunks_and_ordinality_not_document_joining(self):
        select, params = BENCH.conversion_select("current_batch_128")
        self.assertEqual(params, (128,))
        self.assertIn("pinyin_word_romanize_batch(c.documents)", select)
        self.assertIn("WITH ORDINALITY", select)
        self.assertIn("c.start_id+o.ordinality-1", select)
        self.assertNotIn("string_agg", select)
        self.assertNotIn("array_agg", select)
        self.assertIn("bench_legacy_tokens(tokens)", BENCH.conversion_select("legacy_tokens")[0])
        for method in ("arbitrary", "current_batch_0", "current_batch_1;DROP"):
            with self.assertRaises(ValueError):
                BENCH.conversion_select(method)

    def test_independent_gold_includes_restored_umlaut_null_and_cross_document_polyphony(self):
        gold = {label: (document, output) for label, document, output in BENCH.GOLD_DOCUMENTS}
        self.assertEqual(gold["umlaut"], ("吕梁", "lv liang"))
        self.assertEqual(gold["restored polyphony"], ("銀行", "yin hang"))
        self.assertEqual(gold["NULL document"], (None, None))
        self.assertEqual(gold["document boundary right"], ("行", "xing"))
        self.assertEqual(gold["phrase polyphony"], ("银行", "yin hang"))
        self.assertIn("ARRAY[['银',NULL],['行','银行']]", inspect.getsource(BENCH.check_gold))

    def test_fresh_database_cleanup_runs_after_failure(self):
        admin, manager = MagicMock(), MagicMock()
        manager.__enter__.return_value = admin
        name = "pg_pinyin_p2_0123456789abcdef"
        with patch.object(BENCH.psycopg, "connect", return_value=manager), patch.object(BENCH, "new_database_name", return_value=name):
            with self.assertRaisesRegex(RuntimeError, "probe failure"):
                with BENCH.temporary_database("postgres://example/postgres") as allocated:
                    self.assertEqual(allocated, name)
                    raise RuntimeError("probe failure")
        statements = [str(call.args[0]) for call in admin.execute.call_args_list]
        self.assertEqual(len(statements), 2)
        self.assertIn("CREATE DATABASE", statements[0])
        self.assertIn("DROP DATABASE", statements[1])
        self.assertIn("WITH (FORCE)", statements[1])

    def test_cleanup_guard_cannot_drop_p0_p1_or_unrelated_databases(self):
        cursor = MagicMock()
        for name in ("postgres", "pg_pinyin_p1_0123456789abcdef", "pg_pinyin_p0_0123456789abcdef", "pg_pinyin_p2_bad"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                BENCH.drop_database(cursor, name)
        cursor.execute.assert_not_called()
        self.assertNotEqual(BENCH.new_database_name(), BENCH.new_database_name())

    def test_insert_tables_refreshed_and_scalar_generated_vs_explicit_output_are_distinct(self):
        cursor = MagicMock()
        statement = BENCH.refresh_insert_table(cursor, "legacy_scalar")
        declarations = [str(call.args[0]) for call in cursor.execute.call_args_list]
        self.assertIn("DROP TABLE IF EXISTS bench_insert", declarations[0])
        self.assertIn("GENERATED ALWAYS AS (public.bench_legacy_scalar(document)) STORED", declarations[1])
        self.assertNotIn("output)", statement)
        cursor.reset_mock()
        statement = BENCH.refresh_insert_table(cursor, "current_batch_32")
        self.assertIn("INSERT INTO bench_insert (id,document,output)", statement)
        self.assertIn("JOIN converted o ON o.id=c.id", statement)
        self.assertNotIn("USING bm25", statement)

    def test_setup_records_effective_settings_and_requires_current_extension_versions(self):
        source = inspect.getsource(BENCH.setup)
        self.assertLess(source.index("configure(cursor)"), source.index("SELECT name,setting,unit"))
        environment = {"extensions": {"pg_search": "0.26.1", "pg_pinyin": "0.0.9"}}
        BENCH.require_versions(environment, "0.26.1")
        environment["extensions"]["pg_search"] = "0.25.1"
        with self.assertRaisesRegex(RuntimeError, "Expected pg_search"):
            BENCH.require_versions(environment, "0.26.1")

    def test_reports_preserve_protocol_limits_and_separate_p2_namespace(self):
        main = inspect.getsource(BENCH.main)
        self.assertIn("benchmarks/p2_array_batch", main)
        self.assertNotIn("benchmarks/p1_query_modes", main)
        self.assertIn("args.runs < 3", main)
        self.assertIn('"runtime_source_sha256"', main)
        self.assertIn("pg_pinyin--0.0.8--0.0.9.sql", main)
        report = inspect.getsource(BENCH.render_markdown)
        for requirement in ("separately rebuilt old C module", "precomputed outside timing",
                            "No BM25 index", "NOT tokens within one document", "not cached for an entire transaction",
                            "not a release/tag claim", "absolute timings are not directly comparable",
                            "No BM25 index build or index-reduction claim", "pre-optimization P1 snapshot",
                            "not a comparison between two released extension versions"):
            self.assertIn(requirement, report)


if __name__ == "__main__":
    unittest.main()
