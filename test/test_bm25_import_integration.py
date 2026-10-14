"""Opt-in real COPY/BM25 import checks; every test owns a fresh database."""

import importlib.util
import os
from pathlib import Path
import sys
import unittest

import psycopg
from psycopg.conninfo import make_conninfo


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "bm25_import_integration_runner", ROOT / "scripts/benchmark_bm25_import.py"
)
BENCH = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = BENCH
SPEC.loader.exec_module(BENCH)


@unittest.skipUnless(
    os.environ.get("PG_LOADER_TESTS") == "1" and os.environ.get("PGURL"),
    "enable PG_LOADER_TESTS=1 and PGURL in the prepared pg_search container",
)
class BM25ImportIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin_url = os.environ["PGURL"]
        with psycopg.connect(cls.admin_url, autocommit=True) as connection:
            available = {
                row[0] for row in connection.execute(
                    "SELECT name FROM pg_available_extensions "
                    "WHERE name IN ('pg_search','pg_pinyin')"
                )
            }
        if available != {"pg_search", "pg_pinyin"}:
            raise RuntimeError("prepared pg_search and pg_pinyin extensions are required")

    @staticmethod
    def corpus():
        return [
            {"id": ident, "document": document, "expected": expected, "label": label}
            for ident, (label, document, expected) in enumerate(BENCH.GOLD_DOCUMENTS, 1)
        ]

    def install(self, database):
        url = make_conninfo(self.admin_url, dbname=database)
        with psycopg.connect(url, autocommit=True) as connection:
            connection.execute("CREATE EXTENSION pg_search CASCADE")
            connection.execute("CREATE EXTENSION pg_pinyin")
        return url

    def assert_database_absent(self, name):
        with psycopg.connect(self.admin_url, autocommit=True) as connection:
            self.assertFalse(connection.execute(
                "SELECT EXISTS(SELECT FROM pg_database WHERE datname=%s)", (name,)
            ).fetchone()[0])

    def test_all_import_methods_indexes_and_transaction_boundaries_preserve_gold(self):
        corpus = self.corpus()
        self.assertIn(("银行", "yin hang"), [(row["document"], row["expected"]) for row in corpus])
        self.assertIn(("行", "xing"), [(row["document"], row["expected"]) for row in corpus])
        self.assertTrue(any(row["document"] is None for row in corpus))
        self.assertTrue(any(row["document"] == "" for row in corpus))
        probes = BENCH.search_probes(corpus, 5)
        with BENCH.temporary_database(self.admin_url) as name:
            url = self.install(name)
            for case in BENCH.cases((5, len(corpus))):
                with self.subTest(case=case):
                    # Array batches (3) deliberately do not coincide with transaction chunks (5).
                    sample = BENCH.measure_case(
                        self.admin_url, name, corpus, case, 3, probes, 0, measure=False
                    )
                    self.assertEqual(sample["rows"], len(corpus))
                    with psycopg.connect(url, autocommit=True) as observer:
                        actual = observer.execute(
                            "SELECT id,document,pinyin FROM bench_import ORDER BY id"
                        ).fetchall()
                        self.assertEqual(actual, [
                            (row["id"], row["document"], row["expected"]) for row in corpus
                        ])
                        self.assertIsNotNone(observer.execute(
                            "SELECT to_regclass('bench_import_bm25')"
                        ).fetchone()[0])
                        BENCH.verify_search(observer.cursor(), corpus, probes)
        self.assert_database_absent(name)

    def test_copy_failure_rolls_back_and_removes_database_then_next_case_succeeds(self):
        corpus = self.corpus()
        for fault in ("duplicate", "invalid_bigint"):
            failed = [dict(row) for row in corpus]
            failed[-1]["id"] = failed[0]["id"] if fault == "duplicate" else "not-a-bigint"
            with self.subTest(fault=fault):
                with self.assertRaises(psycopg.Error):
                    with BENCH.temporary_database(self.admin_url) as name:
                        url = self.install(name)
                        with psycopg.connect(url) as connection, connection.cursor() as cursor:
                            BENCH.create_table(cursor, "copy_generated")
                            connection.commit()
                            BENCH.import_rows(connection, failed, "copy_generated", len(failed), 3)
                self.assert_database_absent(name)
        with BENCH.temporary_database(self.admin_url) as good_name:
            self.install(good_name)
            good_case = {"method": "copy_generated", "index_mode": "after_load", "transaction_rows": 5}
            sample = BENCH.measure_case(
                self.admin_url, good_name, corpus, good_case, 3,
                BENCH.search_probes(corpus, 5), 0, measure=False,
            )
            self.assertEqual(sample["rows"], len(corpus))
        self.assert_database_absent(good_name)

    def test_exact_row_verification_rejects_doc_output_and_id_mutations(self):
        corpus = self.corpus()
        with BENCH.temporary_database(self.admin_url) as name:
            url = self.install(name)
            case = {"method": "copy_precomputed", "index_mode": "after_load", "transaction_rows": 5}
            BENCH.measure_case(
                self.admin_url, name, corpus, case, 3,
                BENCH.search_probes(corpus, 5), 0, measure=False,
            )
            with psycopg.connect(url) as connection, connection.cursor() as cursor:
                mutations = (
                    "UPDATE bench_import SET document='wrong source' WHERE id=1",
                    "UPDATE bench_import SET pinyin='wrong output' WHERE id=1",
                    "UPDATE bench_import SET id=10000 WHERE id=1",
                    "DELETE FROM bench_import WHERE id=1",
                )
                for statement in mutations:
                    with self.subTest(statement=statement):
                        cursor.execute(statement)
                        with self.assertRaises(RuntimeError):
                            BENCH.verify_rows(cursor, corpus, "deliberate mutation")
                        connection.rollback()
        self.assert_database_absent(name)


if __name__ == "__main__":
    unittest.main()
