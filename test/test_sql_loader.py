"""CLI integration: isolated SQL-only database, no pg_pinyin extension required."""

import csv
import json
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

    def test_sql_matching_mode_patterns_and_legacy_null_resolution(self):
        cases = [
            ("xian", "full", False, ["xian"]),
            ("xi'an", "full", False, ["xi", "an"]),
            ("xi’an", "full", False, ["xi", "an"]),
            ("xi an", "autocomplete", False, ["xi", "an.*"]),
            ("xi ang", "full", False, ["xi", "ang"]),
            ("xi zh", "autocomplete", False, ["xi", "zh.*"]),
            ("zh xi", "autocomplete", False, []),
            ("whh", "initials", False, ["w.*", "h.*", "h.*"]),
            ("zh ch sh", "initials", False, ["zh.*", "ch.*", "sh.*"]),
            ("whh", "full", False, []),
            ("whh", "autocomplete", False, []),
            ("LǛ LIÁNG", "full", False, ["lv", "liang"]),
            ("lu:4lu:e0", "full", False, ["lv", "lve"]),
            ("lu\u0308\u030cliang", "full", False, ["lv", "liang"]),
            ("ni3hao3", "full", False, ["ni", "hao"]),
            ("nǐhao3", "full", False, ["ni", "hao"]),
            ("nǐhao3", "autocomplete", False, ["ni", "hao.*"]),
            ("nǐ3", "full", False, []),
            ("zh3", "autocomplete", False, []),
            ("hǎo3", "initials", False, []),
            ("hǎó", "full", False, []),
            ("hǎó", "autocomplete", False, []),
            ("āí", "full", False, []),
            ("āí", "initials", False, []),
            ("nǐhǎo", "full", False, ["ni", "hao"]),
            ("nǐhǎo", "autocomplete", False, ["ni", "hao.*"]),
            ("m\u0301", "full", False, ["m"]),
            ("m\u0301a", "full", False, []),
            ("n\u0304\u0301i", "full", False, []),
            ("xi\u00a0an\u2003", "full", False, ["xi", "an"]),
            ("xi-an", "full", False, []),
            ("中文", "full", False, []),
            ("ni33", "full", False, []),
            ("ni6", "full", False, []),
            ("u::", "full", False, []),
            ("ü:", "full", False, []),
            ("", "full", False, []),
            ("  ", "full", False, []),
            ("a" * 257, "full", False, []),
            (" ".join(["a"] * 64), "full", False, ["a"] * 64),
            (" ".join(["a"] * 65), "full", False, []),
            ("xi an", "full", True, [r"(xi|.*\|xi\|.*)", r"(an|.*\|an\|.*)"]),
            ("xi an", "autocomplete", True, [r"(xi|.*\|xi\|.*)", r"(an.*|.*\|an[^|]*\|.*)"]),
            ("whh", "initials", True, [r"(w.*|.*\|w[^|]*\|.*)", r"(h.*|.*\|h[^|]*\|.*)", r"(h.*|.*\|h[^|]*\|.*)"]),
            ("whh", "compat", False, ["w.*", "h.*", "h.*"]),
            ("lun", "compat", True, [r".*\|lun.*"]),
        ]
        with psycopg.connect(self.url, autocommit=True) as connection:
            for value, mode, generated, expected in cases:
                with self.subTest(value=value, mode=mode, generated=generated):
                    patterns = connection.execute(
                        "SELECT public.sql_pinyin_regex_phrase_patterns_mode(%s, %s, %s)",
                        (value, mode, generated),
                    ).fetchone()[0]
                    self.assertEqual(patterns, expected)
            self.assertEqual(connection.execute(
                "SELECT public.sql_pinyin_regex_phrase('lunlun', NULL, 4) IS NOT NULL, "
                "public.sql_pinyin_regex_phrase_patterns_mode(NULL, 'full') IS NULL, "
                "public.sql_pinyin_regex_phrase_patterns_mode('xi', NULL) IS NULL, "
                "public.sql_pinyin_regex_phrase_patterns_mode('xi', 'full', NULL) IS NULL, "
                "public.sql_pinyin_regex_phrase_mode(NULL, 'full') IS NULL"
            ).fetchone(), (True, True, True, True, True))
            self.assertEqual(connection.execute(
                "SELECT proparallel, provolatile FROM pg_proc WHERE oid = "
                "'public.sql_pinyin_regex_phrase_patterns_mode(text,text,boolean)'::regprocedure"
            ).fetchone(), ("s", "s"))
            with self.assertRaises(psycopg.errors.InvalidParameterValue):
                connection.execute("SELECT public.sql_pinyin_regex_phrase_patterns_mode('xi', 'unknown')")

    def test_sql_only_legacy_apostrophe_patterns_match_compat_contract(self):
        fixtures = json.loads((ROOT / "test/fixtures/pinyin_query_modes.json").read_text())
        cases = [case for case in fixtures["pattern_cases"] if case["mode"] == "compat"]
        with psycopg.connect(self.url, autocommit=True) as connection:
            self.assertFalse(connection.execute(
                "SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname='pg_pinyin')"
            ).fetchone()[0])
            self.assertIsNone(connection.execute(
                "SELECT to_regprocedure('public.pinyin_regex_phrase_patterns(text,boolean)')"
            ).fetchone()[0])
            for case in cases:
                with self.subTest(label=case["label"]):
                    row = connection.execute(
                        "SELECT public.sql_pinyin_regex_phrase_patterns(%s, %s), "
                        "public.sql_pinyin_regex_phrase_patterns_mode(%s, 'compat', %s)",
                        (case["input"], case["generated"], case["input"], case["generated"]),
                    ).fetchone()
                    self.assertEqual(row, (case["patterns"], case["patterns"]))

    def test_matching_modes_use_live_sql_token_inventory(self):
        with psycopg.connect(self.url) as connection:
            self.assertEqual(connection.execute(
                "SELECT public.sql_pinyin_regex_phrase_patterns_mode('zz', 'full')"
            ).fetchone()[0], [])
            connection.execute(
                "INSERT INTO pinyin.pinyin_token(character, category) "
                "VALUES ('zz', 1), ('qqaiu', 1)"
            )
            self.assertEqual(connection.execute(
                "SELECT public.sql_pinyin_regex_phrase_patterns_mode('zz', 'full')"
            ).fetchone()[0], ["zz"])
            self.assertEqual(connection.execute(
                "SELECT public.sql_pinyin_regex_phrase_patterns('zz''an'), "
                "public.sql_pinyin_regex_phrase_patterns_mode('zz’an', 'compat')"
            ).fetchone(), (["zz.*", "an.*"], ["zz.*", "an.*"]))
            connection.execute(
                "INSERT INTO pinyin.pinyin_token(character, category) "
                "VALUES ('', 1), ('bad!', 1), ('ZZ', 1)"
            )
            self.assertEqual(connection.execute(
                "SELECT public.sql_pinyin_regex_phrase_patterns_mode('zz', 'full')"
            ).fetchone()[0], ["zz"])
            self.assertEqual(connection.execute(
                "SELECT public.sql_pinyin_regex_phrase_patterns_mode('b', 'full')"
            ).fetchone()[0], [])
            self.assertEqual(connection.execute(
                "SELECT public.sql_pinyin_regex_phrase_patterns_mode('xx', 'full')"
            ).fetchone()[0], [])
            self.assertEqual(connection.execute(
                "SELECT public.sql_pinyin_regex_phrase_patterns_mode('qqāí', 'autocomplete')"
            ).fetchone()[0], [])
            self.assertEqual(connection.execute(
                "SELECT public.sql_pinyin_regex_phrase_patterns_mode('qqā', 'autocomplete')"
            ).fetchone()[0], ["qqa.*"])
            connection.rollback()

    def test_sql_only_matching_modes_on_real_bm25_index(self):
        with psycopg.connect(self.url, autocommit=True) as connection:
            connection.execute("CREATE TABLE sql_mode_probe(id integer PRIMARY KEY, pinyin text NOT NULL)")
            try:
                connection.execute(
                    "INSERT INTO sql_mode_probe VALUES "
                    "(1, 'xi an'), (2, 'xian'), (3, 'xi ang'), "
                    "(4, '|xi| |an|'), (5, '|xi| |ang|'), "
                    "(6, 'wu han hu'), (7, 'wu hu han'), (8, 'wu han zhou'), "
                    "(9, '|xi| |an|anxiety|'), (10, '|xi| |anxiety|'), "
                    "(11, 'xia n'), (12, 'xi a n'), (13, '|xian|'), "
                    "(14, '|xia| |n|'), (15, '|xi| |a| |n|')"
                )
                connection.execute(
                    "CREATE INDEX sql_mode_probe_bm25 ON sql_mode_probe "
                    "USING bm25(id, (pinyin::pdb.whitespace)) WITH (key_field='id')"
                )
                self.assertEqual(connection.execute(
                    "SELECT '|xi| |an|'::pdb.whitespace::text[]"
                ).fetchone()[0], ["|xi|", "|an|"])
                cases = [
                    ("xi an", "full", False, [1]),
                    ("xi an", "autocomplete", False, [1, 3]),
                    ("xian", "full", False, [2]),
                    ("xi'an", "full", False, [1]),
                    ("xi’an", "full", False, [1]),
                    ("xi‘an", "full", False, [1]),
                    ("xi'an", "compat", False, [1, 3]),
                    ("xi’an", "compat", True, [4, 5, 9, 10]),
                    ("xi‘an", "autocomplete", False, [1, 3]),
                    ("whh", "initials", False, [6, 7]),
                    ("xi an", "full", True, [1, 4, 9]),
                    ("xi an", "autocomplete", True, [1, 3, 4, 5, 9, 10]),
                    ("中文", "full", False, []),
                ]
                for value, mode, generated, expected in cases:
                    with self.subTest(value=value, mode=mode, generated=generated):
                        # Keep the tokenizer cast in the index, not the pdb.query operator's LHS.
                        ids = connection.execute(
                            "SELECT id FROM sql_mode_probe WHERE pinyin @@@ "
                            "public.sql_pinyin_regex_phrase_mode(%s, %s, NULL, NULL, %s) ORDER BY id",
                            (value, mode, generated),
                        ).fetchall()
                        self.assertEqual([row[0] for row in ids], expected)
                for value, generated, expected in [
                    ("xi'an", False, [1, 3]), ("xi’an", False, [1, 3]),
                    ("xi‘an", False, [1, 3]), ("''xi’’‘‘an'''", False, [1, 3]),
                    ("xi'an", True, [4, 5, 9, 10]), ("xi‘an", True, [4, 5, 9, 10]),
                    ("xian", False, [2]), ("xian", True, [13]),
                    ("'‘’", False, []), ("xi'an!", True, []),
                ]:
                    with self.subTest(default_value=value, generated=generated):
                        ids = connection.execute(
                            "SELECT id FROM sql_mode_probe WHERE pinyin @@@ "
                            "public.sql_pinyin_regex_phrase(%s, NULL, NULL, %s) ORDER BY id",
                            (value, generated),
                        ).fetchall()
                        self.assertEqual([row[0] for row in ids], expected)
                self.assertEqual(connection.execute(
                    "SELECT id FROM sql_mode_probe WHERE id = 2 OR pinyin @@@ "
                    "public.sql_pinyin_regex_phrase_mode('中文', 'full') ORDER BY id"
                ).fetchall(), [(2,)])
                self.assertEqual(connection.execute(
                    "SELECT id FROM sql_mode_probe WHERE id = 2 OR pinyin @@@ "
                    "public.sql_pinyin_regex_phrase(%s) ORDER BY id", ("'‘’",)
                ).fetchall(), [(2,)])
                self.assertEqual(connection.execute(
                    "SELECT id FROM sql_mode_probe WHERE id = 2 AND pinyin @@@ "
                    "public.sql_pinyin_regex_phrase(%s) ORDER BY id", ("xi'an!",)
                ).fetchall(), [])
                self.assertFalse(connection.execute(
                    "SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname='pg_pinyin')"
                ).fetchone()[0])
            finally:
                connection.execute("DROP TABLE sql_mode_probe")

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
