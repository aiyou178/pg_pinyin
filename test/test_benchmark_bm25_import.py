"""Offline guards for the real BM25 bulk-import benchmark protocol."""

import argparse
from copy import deepcopy
import gzip
import importlib.util
import inspect
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "bm25_import_protocol_runner", ROOT / "scripts/benchmark_bm25_import.py"
)
BENCH = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = BENCH
SPEC.loader.exec_module(BENCH)


class BM25ImportProtocolTests(unittest.TestCase):
    def corpus(self):
        return [
            {"id": ident, "document": document, "expected": expected, "label": label}
            for ident, (label, document, expected) in enumerate(BENCH.GOLD_DOCUMENTS, 1)
        ]

    def test_transaction_chunks_preserve_every_document_null_and_original_ids(self):
        rows = self.corpus()
        for size in (1, 3, 5, len(rows), len(rows) + 1):
            with self.subTest(size=size):
                chunks = list(BENCH.transaction_chunks(rows, size))
                self.assertEqual([row for chunk in chunks for row in chunk], rows)
                self.assertTrue(all(0 < len(chunk) <= size for chunk in chunks))
                self.assertEqual([row["id"] for chunk in chunks for row in chunk],
                                 list(range(1, len(rows) + 1)))
        self.assertEqual(list(BENCH.transaction_chunks([], 5)), [])
        for size in (0, -1):
            with self.subTest(size=size), self.assertRaises(ValueError):
                list(BENCH.transaction_chunks(rows, size))

    def test_case_matrix_and_rotation_never_skip_a_method_or_index_variant(self):
        initial = BENCH.cases((500, 20000))
        signatures = {(case["method"], case["index_mode"], case["transaction_rows"])
                      for case in initial}
        expected = {(method, mode, size) for method in BENCH.METHODS
                    for mode in BENCH.INDEX_MODES for size in (500, 20000)}
        self.assertEqual(signatures, expected)
        self.assertEqual(len(initial), len(expected))
        self.assertEqual(len(BENCH.METHODS), 5)
        for index in range(6):
            rotated = BENCH.rotated_cases(index, (500, 20000))
            self.assertEqual({(case["method"], case["index_mode"], case["transaction_rows"])
                              for case in rotated}, expected)
            self.assertEqual(len(rotated), len(expected))
        self.assertNotEqual(initial, BENCH.rotated_cases(1, (500, 20000)))
        for sizes in ((), (1, 1), (0,), (-1,)):
            with self.subTest(sizes=sizes), self.assertRaises(ValueError):
                BENCH.cases(sizes)

    def test_independent_gold_covers_polyphony_boundaries_empty_null_and_unicode(self):
        gold = {label: (document, expected) for label, document, expected in BENCH.GOLD_DOCUMENTS}
        self.assertEqual(gold["phrase polyphony"], ("银行", "yin hang"))
        self.assertEqual(gold["document boundary left"], ("银", "yin"))
        self.assertEqual(gold["document boundary right"], ("行", "xing"))
        self.assertEqual(gold["umlaut"], ("吕梁", "lv liang"))
        self.assertEqual(gold["restored polyphony"], ("銀行", "yin hang"))
        self.assertEqual(gold["mixed Han and ASCII"], ("郑爽ABC", "zheng shuang abc"))
        self.assertEqual(gold["empty document"], ("", ""))
        self.assertEqual(gold["NULL document"], (None, None))

    def test_twenty_thousand_dictionary_rows_are_deterministic_and_exact(self):
        data = ROOT / "sql/data"
        words = BENCH.ARRAY.read_csv((data / "pinyin_words.csv").read_bytes())
        characters = BENCH.ARRAY.read_csv((data / "pinyin_mapping.csv").read_bytes())
        first = BENCH.ARRAY.make_corpus(words, characters, 20000)
        second = BENCH.ARRAY.make_corpus(words, characters, 20000)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 20000)
        self.assertEqual([row["id"] for row in first], list(range(1, 20001)))
        self.assertEqual(len({row["document"] for row in first}), 20000)
        self.assertTrue(all(row["expected"] == words[row["document"]] for row in first))
        self.assertTrue(all(len(row["document"]) == len(row["expected"].split()) for row in first))
        for transaction_rows in (127, 500, 20000):
            actual = [row for chunk in BENCH.transaction_chunks(first, transaction_rows) for row in chunk]
            self.assertEqual(actual, first)

    def test_row_verification_checks_ids_documents_outputs_including_null(self):
        corpus = self.corpus()
        expected = [(row["id"], row["document"], row["expected"]) for row in corpus]
        cursor = MagicMock()
        cursor.fetchall.return_value = expected
        cursor.fetchone.return_value = (
            len(corpus), len(corpus), sum(row["expected"] is not None for row in corpus),
            sum(row["expected"] == "" for row in corpus),
        )
        BENCH.verify_rows(cursor, corpus, "gold")
        statements = " ".join(str(call.args[0]) for call in cursor.execute.call_args_list)
        self.assertIn("ORDER BY id", statements)
        mutations = (
            [], expected[:-1], expected + [expected[0]],
            [(999, *expected[0][1:]), *expected[1:]],
            [(expected[0][0], "wrong document", expected[0][2]), *expected[1:]],
            [(*expected[0][:2], "wrong pinyin"), *expected[1:]],
            [*expected[:-1], (*expected[-1][:2], "")],
        )
        for actual in mutations:
            cursor.fetchall.return_value = actual
            with self.subTest(actual=actual[:1]), self.assertRaises(RuntimeError):
                BENCH.verify_rows(cursor, corpus, "deliberate mismatch")

    def test_search_probes_define_complete_matches_and_source_presence(self):
        corpus = self.corpus()
        probes = BENCH.search_probes(corpus, 5)
        self.assertEqual(len(probes), 5)
        for probe in probes:
            self.assertIn(probe["source_id"], probe["expected_ids"])
            expected = []
            for row in corpus:
                tokens = (row["expected"] or "").split()
                wanted = probe["tokens"]
                if any(tokens[offset:offset + len(wanted)] == wanted
                       for offset in range(len(tokens) - len(wanted) + 1)):
                    expected.append(row["id"])
            self.assertEqual(probe["expected_ids"], expected)
        source = inspect.getsource(BENCH.verify_search) + inspect.getsource(BENCH.search_statement)
        self.assertIn("EXPLAIN", source)
        self.assertIn("Custom", source)
        self.assertIn("pdb.phrase_array", source)
        self.assertIn("expected_ids", source)
        self.assertIn("ORDER BY id", source)
        self.assertIn("complete positional hitset/source mismatch", source)

    def test_configuration_defaults_and_invalid_values_are_rejected_before_setup(self):
        with tempfile.TemporaryDirectory() as directory:
            args = BENCH.argument_parser().parse_args([
                "--json-output", str(Path(directory) / "new.json"),
                "--markdown-output", str(Path(directory) / "new.md"),
            ])
            self.assertEqual(args.rows, 20000)
            self.assertEqual(args.repeats, 3)
            self.assertEqual(BENCH.validate_configuration(args), (512, 4096, 20000))
            for key, value in (
                ("rows", 0), ("repeats", 0), ("search_probes", 0),
                ("transaction_rows", "1,1"), ("transaction_rows", "0"),
                ("transaction_rows", "3;DROP DATABASE postgres"),
                ("batch_documents", 0), ("batch_documents", 8193),
                ("batch_documents", True), ("batch_documents", 1.5),
                ("memory_interval_ms", 0), ("memory_interval_ms", float("nan")),
                ("pg_search_version", ""), ("postgresql_version_pattern", ""),
            ):
                invalid = argparse.Namespace(**vars(args))
                setattr(invalid, key, value)
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    BENCH.validate_configuration(invalid)
            args.json_output = Path(directory) / "plaintext.gz"
            with self.assertRaisesRegex(ValueError, "json"):
                BENCH.validate_configuration(args)

    def test_unknown_method_and_index_case_cannot_reach_sql(self):
        cursor = MagicMock()
        for method in ("unknown", "copy_generated;DROP DATABASE postgres"):
            with self.subTest(method=method), self.assertRaises(ValueError):
                BENCH.create_table(cursor, method)
            with self.assertRaises(ValueError):
                BENCH.import_rows(MagicMock(), self.corpus(), method, 5, 3)
        for case in (
            {"method": "unknown", "index_mode": "online", "transaction_rows": 5},
            {"method": "copy_generated", "index_mode": "wrong", "transaction_rows": 5},
            {"method": "copy_generated", "index_mode": "online", "transaction_rows": 0},
        ):
            with self.subTest(case=case), self.assertRaises(ValueError):
                BENCH.case_key(case)
        cursor.execute.assert_not_called()

    def test_generated_and_explicit_tables_and_two_real_tokenized_index_fields(self):
        for method in BENCH.METHODS:
            with self.subTest(method=method):
                cursor = MagicMock()
                BENCH.create_table(cursor, method)
                statements = " ".join(call.args[0].as_string() for call in cursor.execute.call_args_list)
                self.assertIn("DROP TABLE IF EXISTS", statements)
                self.assertEqual("GENERATED ALWAYS" in statements,
                                 method in ("insert_generated", "copy_generated"))
                self.assertEqual("ON COMMIT DELETE ROWS" in statements,
                                 method in ("copy_stage_scalar", "copy_stage_batch"))
        cursor = MagicMock()
        BENCH.create_index(cursor)
        statement = cursor.execute.call_args.args[0].as_string()
        self.assertIn("USING bm25", statement)
        self.assertIn("document::pdb.icu", statement)
        self.assertIn("pinyin::pdb.whitespace", statement)
        self.assertIn("key_field='id'", statement)

    def test_copy_write_row_preserves_nulls_escapes_ids_and_explicit_lowerbound(self):
        rows = [
            {"id": 101, "document": None, "expected": None},
            {"id": 203, "document": "", "expected": ""},
            {"id": 309, "document": "Hello\tWorld\n\\N\"'", "expected": "reference"},
        ]
        for precomputed in (False, True):
            with self.subTest(precomputed=precomputed):
                cursor = MagicMock()
                writer = cursor.copy.return_value.__enter__.return_value
                BENCH.copy_rows(cursor, 'bench_import"; DROP DATABASE postgres;--', rows, precomputed)
                self.assertEqual([call.args[0] for call in writer.write_row.call_args_list], [
                    (row["id"], row["document"], row["expected"]) if precomputed
                    else (row["id"], row["document"]) for row in rows
                ])
                writer.write.assert_not_called()
                statement = cursor.copy.call_args.args[0].as_string()
                self.assertIn('"bench_import""; DROP DATABASE postgres;--"', statement)
                self.assertEqual('"pinyin"' in statement, precomputed)

    def test_array_aggregation_is_inside_conversion_and_documents_are_not_joined(self):
        statement = BENCH.stage_insert_statement("copy_stage_batch").as_string()
        self.assertIn("array_agg(document ORDER BY id)", statement)
        self.assertIn("row_number() OVER (ORDER BY id)-1)/%s", statement)
        self.assertIn("public.pinyin_word_romanize_batch(g.documents)", statement)
        self.assertIn("WITH ORDINALITY", statement)
        self.assertIn("g.ids[o.ordinality::integer]", statement)
        self.assertNotIn("string_agg", statement)
        scalar = BENCH.stage_insert_statement("copy_stage_scalar").as_string()
        self.assertIn("public.pinyin_word_romanize(document)", scalar)
        self.assertNotIn("romanize_batch", scalar)
        for method in ("copy_generated", "copy_precomputed", "arbitrary"):
            with self.subTest(method=method), self.assertRaises(ValueError):
                BENCH.stage_insert_statement(method)
        source = inspect.getsource(BENCH.import_rows)
        self.assertLess(source.index("started_load = time.perf_counter()"), source.index("copy_rows("))
        self.assertLess(source.index("started = time.perf_counter()"), source.index("stage_insert_statement(method)"))
        self.assertLess(source.index("conn.commit()"), source.index('result["load_total_ms"]'))

    def test_import_commit_count_and_array_bound_are_independent(self):
        rows = self.corpus()
        for method in BENCH.METHODS:
            with self.subTest(method=method):
                connection, cursor = MagicMock(), MagicMock()
                connection.cursor.return_value.__enter__.return_value = cursor
                stage_counts = iter([len(chunk) for chunk in BENCH.transaction_chunks(rows, 5)])
                def execute(statement, parameters=None):
                    cursor.rowcount = next(stage_counts)
                cursor.execute.side_effect = execute
                counter = iter(index / 1000 for index in range(1000))
                with patch.object(BENCH.time, "perf_counter", side_effect=lambda: next(counter)):
                    phases = BENCH.import_rows(connection, rows, method, 5, 3)
                self.assertEqual(phases["committed_transactions"], 3)
                self.assertEqual(phases["uploaded_rows"], len(rows))
                self.assertEqual(connection.commit.call_count, 3)
                connection.rollback.assert_not_called()
                self.assertFalse(connection.autocommit)
                self.assertGreater(phases["transaction_commit_ms"], 0)
                self.assertGreaterEqual(phases["load_total_ms"], phases["transaction_commit_ms"])
                self.assertEqual(phases["conversion_insert_ms"] > 0, method.startswith("copy_stage_"))
                if method == "copy_stage_batch":
                    self.assertEqual([call.args[1] for call in cursor.execute.call_args_list], [(3,), (3,), (3,)])

    def test_copy_failure_rolls_back_current_transaction_without_hiding_the_error(self):
        connection, cursor = MagicMock(), MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        with patch.object(BENCH, "copy_rows", side_effect=[None, RuntimeError("COPY failure")]):
            with self.assertRaisesRegex(RuntimeError, "COPY failure"):
                BENCH.import_rows(connection, self.corpus(), "copy_generated", 5, 3)
        self.assertEqual(connection.commit.call_count, 1)
        connection.rollback.assert_called_once()

    def test_staged_conversion_count_mismatch_rolls_back_instead_of_silently_omitting_rows(self):
        connection, cursor = MagicMock(), MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        cursor.rowcount = 4
        with self.assertRaisesRegex(RuntimeError, "exactly one row"):
            BENCH.import_rows(connection, self.corpus(), "copy_stage_batch", 5, 3)
        connection.commit.assert_not_called()
        connection.rollback.assert_called_once()

    def test_fresh_database_is_removed_after_failure_with_namespace_guard(self):
        admin, manager = MagicMock(), MagicMock()
        manager.__enter__.return_value = admin
        name = "pg_pinyin_import_0123456789abcdef"
        with patch.object(BENCH.psycopg, "connect", return_value=manager), \
                patch.object(BENCH, "new_database_name", return_value=name):
            with self.assertRaisesRegex(RuntimeError, "conversion failure"):
                with BENCH.temporary_database("postgresql://example/postgres") as allocated:
                    self.assertEqual(allocated, name)
                    raise RuntimeError("conversion failure")
        statements = [str(call.args[0]) for call in admin.execute.call_args_list]
        self.assertEqual(len(statements), 2)
        self.assertIn("CREATE DATABASE", statements[0])
        self.assertIn("DROP DATABASE", statements[1])
        self.assertIn("WITH (FORCE)", statements[1])
        for unsafe in ("postgres", "pg_pinyin_p2_0123456789abcdef",
                       "pg_pinyin_import_bad", name + ";DROP DATABASE postgres"):
            with self.subTest(unsafe=unsafe), self.assertRaises(ValueError):
                BENCH.drop_database(admin, unsafe)
        self.assertNotEqual(BENCH.new_database_name(), BENCH.new_database_name())

    def test_report_output_cannot_overwrite_existing_evidence_or_share_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            json_path, markdown_path = directory / "report.json", directory / "report.md"
            json_path.write_text("previous evidence", encoding="utf-8")
            with self.assertRaises((ValueError, FileExistsError)):
                BENCH.write_report({}, json_path, markdown_path)
            self.assertEqual(json_path.read_text(encoding="utf-8"), "previous evidence")
            self.assertFalse(markdown_path.exists())
            with self.assertRaises((ValueError, FileExistsError)):
                BENCH.write_report({}, directory / "new.json", directory / "new.json")
            self.assertFalse((directory / "new.json").exists())

    def test_compressed_report_is_real_utf8_gzip_and_missing_memory_is_not_zero(self):
        report = {"unicode_source": "银行", "memory": {"available": False, "observed_peak_rss_bytes": None}}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(BENCH, "render_markdown", return_value="# Independent report\n"):
            raw, markdown = Path(directory) / "report.json.gz", Path(directory) / "report.md"
            BENCH.write_report(report, raw, markdown)
            self.assertTrue(raw.read_bytes().startswith(b"\x1f\x8b"))
            self.assertEqual(json.loads(gzip.decompress(raw.read_bytes()).decode("utf-8")), report)
            self.assertEqual(markdown.read_text(encoding="utf-8"), "# Independent report\n")
        self.assertEqual(BENCH.memory_display({"memory": {}}, "postgresql_backend"), "n/a")
        self.assertEqual(BENCH.memory_display({"memory": {
            "postgresql_backend": {"available": False, "observed_peak_rss_bytes": None}
        }}, "postgresql_backend"), "n/a")

    def test_release_artifact_validation_requires_verified_local_server(self):
        cursor = MagicMock()
        database = "pg_pinyin_import_0123456789abcdef"
        cursor.fetchone.return_value = (123, database)
        with patch.object(BENCH.METRICS, "backend_identity", return_value=(True, None)) as identity:
            self.assertTrue(BENCH.require_local_server(cursor, database)["verified"])
            identity.assert_called_once_with(123, database)
        with patch.object(BENCH.METRICS, "backend_identity", return_value=(False, "remote or PID collision")):
            with self.assertRaisesRegex(RuntimeError, "verified local Linux"):
                BENCH.require_local_server(cursor, database)
        cursor.fetchone.return_value = (123, "wrong_database")
        with self.assertRaisesRegex(RuntimeError, "unexpected database"):
            BENCH.require_local_server(cursor, database)
        source = inspect.getsource(BENCH.install_dictionary)
        self.assertLess(source.index("require_local_server"), source.index("ARRAY.current_artifacts"))

    def test_current_release_artifact_checks_remain_mandatory_and_versions_are_explicit(self):
        source = inspect.getsource(BENCH.install_dictionary)
        self.assertIn("ARRAY.current_artifacts(cursor, build_artifact, rust_version)", source)
        self.assertNotIn("create_legacy_aliases", source)
        self.assertNotIn("baseline_provenance", source)
        environment = {"postgresql": "PostgreSQL 19beta4 fixture", "extensions": {
            "pg_search": "0.26.1", "pg_pinyin": BENCH.CURRENT_VERSION
        }}
        BENCH.require_versions(environment, "0.26.1", "19beta4")
        for observed in (
            {**environment, "extensions": {"pg_search": "0.25.1", "pg_pinyin": BENCH.CURRENT_VERSION}},
            {**environment, "extensions": {"pg_search": "0.26.1", "pg_pinyin": "0.0.8"}},
            {**environment, "postgresql": "PostgreSQL 18.3"},
        ):
            with self.subTest(environment=observed), self.assertRaises(RuntimeError):
                BENCH.require_versions(observed, "0.26.1", "19beta4")

    @staticmethod
    def sample_report():
        report = {
            "configuration": {"rows": 100, "repeats": 3, "transaction_rows": [100], "actual_search_probes": 2},
            "reference": {"id_document_output_sha256": "independent reference"}, "samples": [],
        }
        for case in BENCH.cases((100,)):
            for round_index, ready in enumerate((200.0, 100.0, 300.0)):
                timings = {phase: float(index + 1) for index, phase in enumerate(BENCH.PHASES)}
                timings["total_ready_ms"] = ready
                report["samples"].append({
                    **case, "case_key": BENCH.case_key(case), "round": round_index,
                    "timed_sample": True, "timings": timings,
                    "verification": {"verified_rows": 100, "id_document_output_sha256": "independent reference"},
                    "search_verification": {"verified_source_hits": 2},
                    "physical_sizes": {"bm25_relation_bytes": 4096},
                    "wal": {"global_lsn_delta_bytes": 1024},
                    "memory": {"postgresql_backend": {"available": False, "observed_peak_rss_bytes": None}},
                    "classification": "precomputed lower-bound/control; excludes romanization"
                    if case["method"] == "copy_precomputed" else "raw-document pipeline",
                })
        return report

    def test_summary_uses_all_three_rounds_median_readiness_and_correct_rows_per_second(self):
        report = self.sample_report()
        BENCH.summarize(report)
        self.assertEqual(len(report["summary"]), 10)
        for key, summary in report["summary"].items():
            self.assertEqual(summary["phases"]["total_ready_ms"]["samples_ms"], [200.0, 100.0, 300.0])
            self.assertEqual(summary["phases"]["total_ready_ms"]["median_ms"], 200.0)
            self.assertEqual(summary["ready_rows_per_second_at_median"], 500.0)
            self.assertEqual(set(summary["phases"]), set(BENCH.PHASES))
            self.assertTrue(all(value["postgresql_backend"]["observed_peak_rss_bytes"] is None
                                for value in summary["memory_samples"]))
            if key.endswith("copy_precomputed"):
                self.assertIn("excludes romanization", summary["classification"])
        self.assertIn("post_load_index_build_ms", BENCH.PHASES)
        self.assertIn("post_load_index_commit_ms", BENCH.PHASES)
        self.assertIn("transaction_commit_ms", BENCH.PHASES)
        self.assertIn("analyze_ms", BENCH.PHASES)
        self.assertIn("analyze_commit_ms", BENCH.PHASES)

    def test_summary_refuses_missing_rounds_unverified_rows_and_missing_source_hits(self):
        initial = self.sample_report()
        altered = []
        missing = deepcopy(initial)
        missing["samples"].pop()
        altered.append(missing)
        for path, value in (
            (("verification", "verified_rows"), 99),
            (("verification", "id_document_output_sha256"), "same count wrong outputs"),
            (("search_verification", "verified_source_hits"), 1),
        ):
            changed = deepcopy(initial)
            changed["samples"][0][path[0]][path[1]] = value
            altered.append(changed)
        duplicate = deepcopy(initial)
        duplicate["samples"][1]["round"] = 0
        altered.append(duplicate)
        for report in altered:
            with self.subTest(samples=len(report["samples"])), self.assertRaises(RuntimeError):
                BENCH.summarize(report)


if __name__ == "__main__":
    unittest.main()
