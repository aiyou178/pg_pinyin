"""CLI integration: isolated SQL-only database, no pg_pinyin extension required."""

import csv
import os
from pathlib import Path
import subprocess
import unittest
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get("PG_LOADER_TESTS") == "1", "enable PG_LOADER_TESTS in a shared-path PostgreSQL test container")
class SqlLoaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin_url = os.environ["PGURL"]
        cls.database = "pinyin_loader_test_" + uuid.uuid4().hex[:12]
        cls.url = make_conninfo(cls.admin_url, dbname=cls.database)
        with psycopg.connect(cls.admin_url, autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(cls.database)))
        cls.addClassCleanup(cls.remove_database)
        with psycopg.connect(cls.url, autocommit=True) as connection:
            connection.execute("CREATE EXTENSION pg_search CASCADE")
        result = subprocess.run(
            ["psql", cls.url, "-v", "ON_ERROR_STOP=1", "-f", str(ROOT / "sql/pinyin.sql"),
             "-f", str(ROOT / "sql/word.sql")], capture_output=True, text=True
        )
        if result.returncode:
            raise RuntimeError(result.stderr)
        result = cls.load_csv(ROOT / "sql/data/pinyin_words.csv")
        if result.returncode:
            raise RuntimeError(result.stderr)

    @classmethod
    def remove_database(cls):
        with psycopg.connect(cls.admin_url, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(cls.database)))

    @classmethod
    def load_csv(cls, words_file):
        return subprocess.run(
            ["psql", cls.url, "-v", "mapping_file=" + str(ROOT / "sql/data/pinyin_mapping.csv"),
             "-v", "token_file=" + str(ROOT / "sql/data/pinyin_token.csv"),
             "-v", "words_file=" + str(words_file), "-f", str(ROOT / "sql/load_data.sql")],
            capture_output=True, text=True
        )

    def test_sql_backend_without_rust_and_analyzed_dictionary(self):
        with psycopg.connect(self.url, autocommit=True) as connection:
            row = connection.execute(
                "SELECT NOT EXISTS(SELECT 1 FROM pg_extension WHERE extname='pg_pinyin'), "
                "public.icu_romanize_tokens(ARRAY['西安','吕梁','善感']), "
                "public.sql_pinyin_regex_phrase_patterns('whh')"
            ).fetchone()
            self.assertEqual(row, (True, "xi an lv liang shan gan", ["w.*", "h.*", "h.*"]))
            analyzed = connection.execute(
                "SELECT bool_and(last_analyze IS NOT NULL) FROM pg_stat_user_tables "
                "WHERE schemaname='pinyin' AND relname IN ('pinyin_mapping','pinyin_token','pinyin_words')"
            ).fetchone()[0]
            self.assertTrue(analyzed)

    def test_failed_copy_rolls_back_all_tables_and_version(self):
        statement = (
            "SELECT (SELECT count(*) FROM pinyin.pinyin_mapping), "
            "(SELECT count(*) FROM pinyin.pinyin_token), "
            "(SELECT count(*) FROM pinyin.pinyin_words), "
            "(SELECT version FROM pinyin.pinyin_dictionary_meta WHERE singleton)"
        )
        with psycopg.connect(self.url, autocommit=True) as connection:
            before = connection.execute(statement).fetchone()
        missing = ROOT / "sql/data" / ("missing_" + uuid.uuid4().hex + ".csv")
        result = self.load_csv(missing)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not open file", result.stderr)
        with psycopg.connect(self.url, autocommit=True) as connection:
            self.assertEqual(connection.execute(statement).fetchone(), before)
        with (ROOT / "sql/data/pinyin_words.csv").open(newline="") as source:
            self.assertEqual(before[2], sum(1 for _ in csv.reader(source)))


if __name__ == "__main__":
    unittest.main()
