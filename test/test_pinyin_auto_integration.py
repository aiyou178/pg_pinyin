"""Auto install-source parity and opt-in independent SQL backend integration."""

import csv
import os
from pathlib import Path
import re
import unittest
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo


ROOT = Path(__file__).resolve().parents[1]


def mode_install_block(source):
    match = re.search(r"DO \$pinyin_query_modes\$(.*?)\$pinyin_query_modes\$;", source, re.S)
    if match is None:
        raise AssertionError("missing mode API installation block")
    return " ".join(match.group(1).split())


class AutoInstallationSourceTests(unittest.TestCase):
    def test_fresh_and_unreleased_upgrade_mode_wrappers_are_identical(self):
        fresh = (ROOT / "src/lib.rs").read_text(encoding="utf-8")
        upgrade = (ROOT / "pg_pinyin--0.0.8--0.0.9.sql").read_text(encoding="utf-8")
        self.assertEqual(mode_install_block(fresh), mode_install_block(upgrade))
        self.assertIn("pdb.phrase_prefix", mode_install_block(fresh))
        self.assertIn("16384", mode_install_block(fresh))

    def test_sql_backend_never_calls_a_rust_query_helper(self):
        source = (ROOT / "sql/word.sql").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"public\.pinyin_(?:regex_phrase|phrase)\s*\(", source))
        self.assertIsNone(re.search(
            r"^\s*CREATE\s+EXTENSION(?:\s+IF\s+NOT\s+EXISTS)?\s+pg_pinyin\b",
            source, re.M | re.I,
        ))


@unittest.skipUnless(
    os.environ.get("TEST_UPGRADE_DSN")
    or (os.environ.get("PG_LOADER_TESTS") == "1" and os.environ.get("PGURL")),
    "set TEST_UPGRADE_DSN or PG_LOADER_TESTS=1 and PGURL in a prepared pg_search container",
)
class AutoSqlOnlyIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin_url = os.environ.get("TEST_UPGRADE_DSN") or os.environ["PGURL"]
        cls.name = "pinyin_auto_sql_" + uuid.uuid4().hex[:12]
        cls.url = make_conninfo(cls.admin_url, dbname=cls.name)
        with psycopg.connect(cls.admin_url, autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(cls.name)))
        cls.addClassCleanup(cls.drop_database)
        with psycopg.connect(cls.url, autocommit=True) as connection:
            connection.execute("CREATE EXTENSION pg_search CASCADE")
            connection.execute((ROOT / "sql/pinyin.sql").read_text(encoding="utf-8"))
            connection.execute((ROOT / "sql/word.sql").read_text(encoding="utf-8"))
            with connection.cursor().copy("COPY pinyin.pinyin_token FROM STDIN WITH (FORMAT CSV)") as copy:
                copy.write((ROOT / "sql/data/pinyin_token.csv").read_bytes())
            connection.execute("ANALYZE pinyin.pinyin_token")
            connection.execute("CREATE TABLE auto_sql_gold(id bigint PRIMARY KEY,pinyin text NOT NULL)")
            connection.execute(
                "INSERT INTO auto_sql_gold VALUES (1,'xi an'),(2,'xi ang'),(3,'xin an'),"
                "(4,'xian'),(5,'xi a'),(6,'wu han han'),(7,'wu han hang'),(8,'lv liang'),(9,'lve liang'),"
                "(10,'ni hao'),(11,'man ni'),(12,'ming nan'),(13,'n hao'),(14,'m n'),"
                "(15,'ao hao'),(16,'a hao'),(17,'e ou'),(18,'er ou'),(19,'o yang'),"
                "(20,'ou yang'),(21,'a e o'),(22,'ao er ou')"
            )
            connection.execute(
                "CREATE INDEX auto_sql_bm25 ON auto_sql_gold USING bm25"
                "(id,(pinyin::pdb.whitespace)) WITH (key_field='id')"
            )

    @classmethod
    def drop_database(cls):
        with psycopg.connect(cls.admin_url, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(cls.name)))

    def test_auto_is_functional_without_the_rust_extension(self):
        cases = [
            ("xi a", ["xi", "a.*"], [1, 2, 5]),
            ("xi ang", ["xi", "ang"], [2]),
            ("xi1 an1", ["xi", "an"], [1]),
            ("whh", ["w.*", "h.*", "h.*"], [6, 7]),
            ("nh", ["n.*", "h.*"], [10, 13]),
            ("n h", ["n.*", "h.*"], [10, 13]),
            ("m n", ["m.*", "n.*"], [11, 12, 14]),
            ("n1 h", ["n", "h.*"], [13]),
            ("a h", ["a.*", "h.*"], [15, 16]),
            ("e o", ["e.*", "o.*"], [17, 18, 21, 22]),
            ("o y", ["o.*", "y.*"], [19, 20]),
            ("a e o", ["a.*", "e.*", "o.*"], [21, 22]),
            ("a1 h", ["a", "h.*"], [16]),
            ("ā h", ["a", "h.*"], [16]),
            ("wu han ha", ["wu", "han", "ha.*"], [6, 7]),
            ("LǛ LIÁNG", ["lv", "liang"], [8]),
        ]
        with psycopg.connect(self.url, autocommit=True) as connection:
            self.assertFalse(connection.execute(
                "SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname='pg_pinyin')"
            ).fetchone()[0])
            self.assertIsNone(connection.execute(
                "SELECT to_regprocedure('public.pinyin_regex_phrase_mode(text,text,integer,integer,boolean)')"
            ).fetchone()[0])
            for value, patterns, expected_ids in cases:
                with self.subTest(value=value):
                    self.assertEqual(connection.execute(
                        "SELECT public.sql_pinyin_regex_phrase_patterns_mode(%s,'auto')", (value,),
                    ).fetchone()[0], patterns)
                    self.assertEqual(connection.execute(
                        "SELECT id FROM auto_sql_gold WHERE pinyin @@@ "
                        "public.sql_pinyin_regex_phrase_mode(%s,'auto') ORDER BY id", (value,),
                    ).fetchall(), [(item,) for item in expected_ids])

    def test_sql_only_auto_falls_back_if_a_native_api_is_missing(self):
        with psycopg.connect(self.url) as connection:
            connection.execute(
                "ALTER FUNCTION pdb.phrase_prefix(text[],integer) RENAME TO auto_sql_hidden_phrase_prefix"
            )
            self.assertTrue(connection.execute(
                "SELECT public.sql_pinyin_regex_phrase_mode('xi a','auto')::text "
                "= pdb.regex_phrase(ARRAY['xi','a.*'])::text"
            ).fetchone()[0])
            self.assertEqual(connection.execute(
                "SELECT id FROM auto_sql_gold WHERE pinyin @@@ "
                "public.sql_pinyin_regex_phrase_mode('xi a','auto') ORDER BY id"
            ).fetchall(), [(1,), (2,), (5,)])
            connection.rollback()

    def test_every_full_syllable_and_nonempty_proper_prefix(self):
        with (ROOT / "sql/data/pinyin_token.csv").open(encoding="utf-8", newline="") as token_file:
            inventory = {
                token for token, category in csv.reader(token_file)
                if category in {"1", "3"}
            } | {"m", "n"}
        prefixes = {
            token[:length] for token in inventory
            for length in range(1, len(token))
        }
        cases = {
            token: [token + ".*" if token in prefixes else token]
            for token in inventory
        }
        cases.update({prefix: [prefix + ".*"] for prefix in prefixes})
        cases.update({token + "1": [token] for token in inventory})
        with psycopg.connect(self.url, autocommit=True) as connection:
            rows = connection.execute(
                "SELECT value,public.sql_pinyin_regex_phrase_patterns_mode(value,'auto') "
                "FROM unnest(%s::text[]) AS inputs(value)", (sorted(cases),),
            ).fetchall()
        self.assertEqual(len(rows), len(cases))
        for value, patterns in rows:
            with self.subTest(value=value):
                self.assertEqual(patterns, cases[value])


if __name__ == "__main__":
    unittest.main()
