"""Genuine 0.0.8 -> 0.0.9 native-array/batch upgrades, opt-in via TEST_UPGRADE_DSN.

Install both release SQL files and the current shared library before running.
Every database is isolated and removed, including after an assertion failure.
"""

import os
import unittest
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo


OLD_VERSION = "0.0.8"
NEW_VERSION = "0.0.9"
NEW_SIGNATURES = (
    "public.pinyin_word_romanize_batch(text[])",
    "public.pinyin_word_romanize_batch(text[],text)",
)
ALL_NEW_SIGNATURES = NEW_SIGNATURES + (
    "public.pinyin_regex_phrase_patterns_mode(text,text,boolean)",
    "public.pinyin_regex_phrase_mode(text,text,integer,integer,boolean)",
    "public.pinyin_phrase(text,integer)",
)
CUSTOM_WORD = "升级批量探针"
CUSTOM_READING = "sheng ji pi liang tan zhen"
SUFFIX = "_array_batch_upgrade"


def plan_nodes(node):
    yield node
    for child in node.get("Plans", []):
        yield from plan_nodes(child)


@unittest.skipUnless(
    os.environ.get("TEST_UPGRADE_DSN"),
    "set TEST_UPGRADE_DSN with genuine 0.0.8/0.0.9 SQL and current library installed",
)
class ArrayBatchUpgradeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin_url = os.environ["TEST_UPGRADE_DSN"]
        with psycopg.connect(cls.admin_url, autocommit=True) as connection:
            versions = {
                row[0] for row in connection.execute(
                    "SELECT version FROM pg_available_extension_versions "
                    "WHERE name = 'pg_pinyin'"
                )
            }
            if not {OLD_VERSION, NEW_VERSION} <= versions:
                raise RuntimeError(
                    f"need genuine installation SQL for {OLD_VERSION} and {NEW_VERSION}; "
                    f"available versions: {sorted(versions)}"
                )
            cls.search_available = connection.execute(
                "SELECT EXISTS(SELECT 1 FROM pg_available_extensions WHERE name = 'pg_search')"
            ).fetchone()[0]

    def database(self, label, version, with_search=False):
        name = "pinyin_batch_upgrade_" + label + "_" + uuid.uuid4().hex[:10]
        url = make_conninfo(self.admin_url, dbname=name)
        with psycopg.connect(self.admin_url, autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
        self.addCleanup(self.drop_database, name)
        with psycopg.connect(url, autocommit=True) as connection:
            if with_search:
                connection.execute("CREATE EXTENSION pg_search CASCADE")
            connection.execute(sql.SQL("CREATE EXTENSION pg_pinyin VERSION {}").format(sql.Literal(version)))
            self.assertEqual(self.version(connection), version)
        return url

    def drop_database(self, name):
        with psycopg.connect(self.admin_url, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))

    @staticmethod
    def version(connection):
        return connection.execute(
            "SELECT extversion FROM pg_extension WHERE extname = 'pg_pinyin'"
        ).fetchone()[0]

    @staticmethod
    def owned_function_oids(connection):
        return dict(connection.execute(
            "SELECT p.oid::regprocedure::text, p.oid "
            "FROM pg_proc p JOIN pg_depend d ON d.classid = 'pg_proc'::regclass "
            "AND d.objid = p.oid AND d.deptype = 'e' "
            "AND d.refclassid = 'pg_extension'::regclass "
            "JOIN pg_extension e ON e.oid = d.refobjid WHERE e.extname = 'pg_pinyin'"
        ).fetchall())

    def catalog(self, connection):
        for signature in ALL_NEW_SIGNATURES:
            self.assertIsNotNone(connection.execute("SELECT to_regprocedure(%s)", (signature,)).fetchone()[0])
        rows = connection.execute(
            "SELECT p.oid::regprocedure::text, pg_get_function_arguments(p.oid), "
            "pg_get_function_result(p.oid), p.proparallel, p.provolatile, "
            "p.proisstrict, p.pronargdefaults, l.lanname, e.extname "
            "FROM pg_proc p JOIN pg_language l ON l.oid = p.prolang "
            "LEFT JOIN pg_depend d ON d.classid = 'pg_proc'::regclass AND d.objid = p.oid "
            "AND d.deptype = 'e' AND d.refclassid = 'pg_extension'::regclass "
            "LEFT JOIN pg_extension e ON e.oid = d.refobjid "
            "WHERE p.oid IN (%s::regprocedure, %s::regprocedure) "
            "ORDER BY p.oid::regprocedure::text", NEW_SIGNATURES,
        ).fetchall()
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row[2:], ("text[]", "s", "s", True, 0, "c", "pg_pinyin"))
        self.assertIsNone(connection.execute(
            "SELECT to_regprocedure('public.pinyin_word_romanize(text[])')"
        ).fetchone()[0])
        self.assertIsNone(connection.execute(
            "SELECT to_regprocedure('public.pinyin_word_romanize(text[],text)')"
        ).fetchone()[0])
        return rows

    @staticmethod
    def legacy_values(connection):
        return connection.execute(
            "SELECT public.pinyin_word_romanize(ARRAY[' 吕梁 ', '', 'ABC']), "
            "public.pinyin_word_romanize('[0:2]={吕,梁,ABC}'::text[]), "
            "public.pinyin_word_romanize(ARRAY[['吕','梁'],['ABC','123']]), "
            "public.pinyin_word_romanize(ARRAY['吕梁','ABC']::varchar[]), "
            "public.pinyin_word_romanize(ARRAY[123,456]::integer[]), "
            "public.pinyin_word_romanize(ARRAY['ABC-DEF','Ü']), "
            "public.pinyin_word_romanize(NULL), public.pinyin_word_romanize(NULL,NULL)"
        ).fetchone()

    @staticmethod
    def non_array_outcome(connection):
        try:
            return ("value", connection.execute("SELECT public.pinyin_word_romanize(123)").fetchone()[0])
        except psycopg.Error as error:
            return ("error", error.sqlstate)

    def assert_batch_values(self, connection):
        documents = ["吕梁", "女儿", "丙酸氟替卡鬆", "遵时養晦", "ABC-DEF", "", None]
        expected = [connection.execute(
            "SELECT public.pinyin_word_romanize(%s::text)", (value,)
        ).fetchone()[0] for value in documents]
        self.assertEqual(expected[:4], ["lv liang", "nv er", "bing suan fu ti ka song", "zun shi yang hui"])
        self.assertEqual(connection.execute(
            "SELECT public.pinyin_word_romanize_batch(%s::text[])", (documents,)
        ).fetchone()[0], expected)
        self.assertEqual(connection.execute(
            "SELECT public.pinyin_word_romanize_batch(ARRAY[['ABC',NULL],['','吕梁']]), "
            "public.pinyin_word_romanize_batch('[0:2]={ABC,NULL,吕梁}'::text[]), "
            "public.pinyin_word_romanize_batch(ARRAY[]::text[]), "
            "public.pinyin_word_romanize_batch(ARRAY[NULL,NULL]::text[]), "
            "public.pinyin_word_romanize_batch(NULL), "
            "public.pinyin_word_romanize_batch(ARRAY['ABC'],NULL)"
        ).fetchone(), (["abc", None, "", "lv liang"], ["abc", None, "lv liang"], [], [None, None], None, None))
        connection.execute("CREATE DOMAIN token_array_domain AS text[]")
        self.assertEqual(connection.execute(
            "SELECT public.pinyin_word_romanize(ARRAY[' 吕梁 ','ABC']::token_array_domain)"
        ).fetchone()[0], "lv liang abc")
        for suffix in (False, True):
            query = "SELECT public.pinyin_word_romanize(ARRAY['ABC',NULL]::text[]"
            query += ", '_array_batch_upgrade')" if suffix else ")"
            with self.subTest(null_token_suffix=suffix), self.assertRaises(psycopg.errors.InternalError) as caught:
                connection.execute(query)
            self.assertEqual(caught.exception.sqlstate, "XX000")
            self.assertTrue(caught.exception.diag.message_primary)
            self.assertIn("null", caught.exception.diag.message_primary.lower())

    def assert_invalidation_and_rollback(self, connection):
        connection.execute(
            "INSERT INTO pinyin.pinyin_words(word,pinyin) VALUES (%s,%s) "
            "ON CONFLICT (word) DO UPDATE SET pinyin=EXCLUDED.pinyin", (CUSTOM_WORD, CUSTOM_READING),
        )
        self.assertEqual(connection.execute(
            "SELECT public.pinyin_word_romanize_batch(ARRAY[%s])", (CUSTOM_WORD,)
        ).fetchone()[0], [CUSTOM_READING])
        connection.execute("BEGIN")
        try:
            connection.execute("UPDATE pinyin.pinyin_words SET pinyin='changed reading' WHERE word=%s", (CUSTOM_WORD,))
            self.assertEqual(connection.execute(
                "SELECT public.pinyin_word_romanize_batch(ARRAY[%s]), public.pinyin_word_romanize(ARRAY[%s])",
                (CUSTOM_WORD, CUSTOM_WORD),
            ).fetchone(), (["changed reading"], "changed reading"))
        finally:
            connection.execute("ROLLBACK")
        self.assertEqual(connection.execute(
            "SELECT public.pinyin_word_romanize_batch(ARRAY[%s]), public.pinyin_word_romanize(ARRAY[%s])",
            (CUSTOM_WORD, CUSTOM_WORD),
        ).fetchone(), ([CUSTOM_READING], CUSTOM_READING))

    def assert_parallel_read_only(self, url):
        with psycopg.connect(url, autocommit=True) as connection:
            connection.execute(
                "CREATE TABLE batch_upgrade_parallel_probe AS SELECT %s::text AS value "
                "FROM generate_series(1,100000)", (CUSTOM_WORD,),
            )
            connection.execute("ALTER TABLE batch_upgrade_parallel_probe SET (parallel_workers=2)")
            connection.execute("ANALYZE batch_upgrade_parallel_probe")
            connection.execute(
                "CREATE TABLE pinyin.pinyin_words_array_batch_upgrade(word text PRIMARY KEY,pinyin text NOT NULL)"
            )
            connection.execute(
                "INSERT INTO pinyin.pinyin_words_array_batch_upgrade VALUES (%s, 'suffix reading')", (CUSTOM_WORD,),
            )
        cases = [
            ("native", "public.pinyin_word_romanize(ARRAY[value,'ABC']) = %s", CUSTOM_READING + " abc", "pinyin_word_romanize"),
            ("native suffix", "public.pinyin_word_romanize(ARRAY[value,'ABC'], '_array_batch_upgrade') = %s",
             "suffix reading abc", "pinyin_word_romanize"),
            ("batch", "public.pinyin_word_romanize_batch(ARRAY[value,NULL,'']) = %s::text[]",
             [CUSTOM_READING, None, ""], "pinyin_word_romanize_batch"),
            ("batch suffix", "public.pinyin_word_romanize_batch(ARRAY[value,NULL,''], '_array_batch_upgrade') = %s::text[]",
             ["suffix reading", None, ""], "pinyin_word_romanize_batch"),
        ]
        for label, expression, expected, function_name in cases:
            with self.subTest(parallel=label), psycopg.connect(url, autocommit=True) as connection:
                connection.execute("BEGIN READ ONLY")
                try:
                    for setting in (
                        "max_parallel_workers_per_gather=2", "min_parallel_table_scan_size=0",
                        "parallel_setup_cost=0", "parallel_tuple_cost=0", "jit=off",
                    ):
                        connection.execute("SET LOCAL " + setting)
                    self.assertTrue(connection.execute("SELECT pg_current_xact_id_if_assigned() IS NULL").fetchone()[0])
                    query = "SELECT count(*) FROM batch_upgrade_parallel_probe WHERE " + expression
                    plan = connection.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + query, (expected,)).fetchone()[0][0]["Plan"]
                    nodes = list(plan_nodes(plan))
                    self.assertTrue(any(node["Node Type"] in ("Gather", "Gather Merge") for node in nodes))
                    self.assertGreater(sum(node.get("Workers Launched", 0) for node in nodes), 0)
                    self.assertTrue(any(
                        node.get("Parallel Aware") and function_name in node.get("Filter", "") for node in nodes
                    ), "the native/batch function must run below Gather")
                    self.assertEqual(connection.execute(query, (expected,)).fetchone()[0], 100000)
                    self.assertTrue(connection.execute("SELECT pg_current_xact_id_if_assigned() IS NULL").fetchone()[0])
                finally:
                    connection.execute("ROLLBACK")

    def assert_rollback_version_reuse(self, connection):
        suffix = "_array_batch_reuse_upgrade"
        connection.execute("CREATE TEMP TABLE rollback_version_reuse_input(phrase text,character text)")
        connection.execute("INSERT INTO rollback_version_reuse_input VALUES ('吕梁','吕')")
        connection.execute(
            "CREATE TABLE pinyin.pinyin_words_array_batch_reuse_upgrade(word text PRIMARY KEY,pinyin text NOT NULL)"
        )
        connection.execute("INSERT INTO pinyin.pinyin_words_array_batch_reuse_upgrade VALUES ('吕梁','baseline override')")
        query = (
            "SELECT jsonb_build_object("
            "'native',public.pinyin_word_romanize(ARRAY[phrase]),"
            "'word',public.pinyin_word_romanize(phrase),"
            "'batch',public.pinyin_word_romanize_batch(ARRAY[phrase]),"
            "'suffix_native',public.pinyin_word_romanize(ARRAY[phrase],%s),"
            "'suffix_word',public.pinyin_word_romanize(phrase,%s),"
            "'suffix_batch',public.pinyin_word_romanize_batch(ARRAY[phrase],%s),"
            "'char',public.pinyin_char_romanize(character),"
            "'suffix_char',public.pinyin_char_romanize(character,%s)) "
            "FROM rollback_version_reuse_input"
        )
        suffixes = (suffix,) * 4

        def mutation(phase):
            connection.execute("UPDATE pinyin.pinyin_mapping SET pinyin=%s WHERE character='吕'", ("lv" + phase,))
            connection.execute("UPDATE pinyin.pinyin_words SET pinyin=%s WHERE word='吕梁'", ("word" + phase + " phase" + phase,))
            connection.execute(
                "UPDATE pinyin.pinyin_words_array_batch_reuse_upgrade SET pinyin=%s WHERE word='吕梁'",
                ("suffix" + phase + " phase" + phase,),
            )
            stamp = connection.execute(
                "SELECT version,xmin::text FROM pinyin.pinyin_dictionary_meta WHERE singleton"
            ).fetchone()
            return stamp, connection.execute(query, suffixes).fetchone()[0]

        def expected(phase):
            word = "word" + phase + " phase" + phase
            override = "suffix" + phase + " phase" + phase
            return {
                "native": word, "word": word, "batch": [word],
                "suffix_native": override, "suffix_word": override, "suffix_batch": [override],
                "char": "lv" + phase, "suffix_char": "lv" + phase,
            }

        connection.execute("BEGIN")
        try:
            connection.execute("SAVEPOINT reused_dictionary_version")
            stamp_a, values_a = mutation("a")
            self.assertEqual(values_a, expected("a"))
            connection.execute("ROLLBACK TO SAVEPOINT reused_dictionary_version")
            # No conversion may run before the next mutations reuse counter A.
            stamp_b, values_b = mutation("b")
            self.assertEqual(stamp_b[0], stamp_a[0])
            self.assertNotEqual(stamp_b[1], stamp_a[1])
            self.assertEqual(values_b, expected("b"))
        finally:
            connection.execute("ROLLBACK")
        self.assertEqual(connection.execute(query, suffixes).fetchone()[0], {
            "native": "lv liang", "word": "lv liang", "batch": ["lv liang"],
            "suffix_native": "baseline override", "suffix_word": "baseline override",
            "suffix_batch": ["baseline override"], "char": "lv", "suffix_char": "lv",
        })
        connection.execute("DROP TABLE rollback_version_reuse_input")
        connection.execute("DROP TABLE pinyin.pinyin_words_array_batch_reuse_upgrade")

    def assert_real_upgrade(self, with_search):
        upgraded_url = self.database("old", OLD_VERSION, with_search)
        with psycopg.connect(upgraded_url, autocommit=True) as connection:
            # The genuine released baseline must lack both mode and batch APIs.
            for signature in ALL_NEW_SIGNATURES:
                self.assertIsNone(connection.execute("SELECT to_regprocedure(%s)", (signature,)).fetchone()[0])
            old_oids = self.owned_function_oids(connection)
            self.assertIn("pinyin_word_romanize(anyelement)", old_oids)
            self.assertIn("pinyin_word_romanize(anyelement,text)", old_oids)
            old_values = self.legacy_values(connection)
            old_non_array = self.non_array_outcome(connection)
            connection.execute(
                "INSERT INTO pinyin.pinyin_words(word,pinyin) VALUES (%s,%s) "
                "ON CONFLICT (word) DO UPDATE SET pinyin=EXCLUDED.pinyin", (CUSTOM_WORD, CUSTOM_READING),
            )
            connection.execute("INSERT INTO pinyin.pinyin_token(character,category) VALUES ('zzbatch',1)")
            connection.execute(
                "CREATE TABLE stored_batch_upgrade_probe(name text, tokens text[], "
                "scalar_pinyin text GENERATED ALWAYS AS (public.pinyin_word_romanize(name)) STORED, "
                "token_pinyin text GENERATED ALWAYS AS (public.pinyin_word_romanize(tokens)) STORED)"
            )
            connection.execute(
                "INSERT INTO stored_batch_upgrade_probe(name,tokens) VALUES (%s,ARRAY[%s,'ABC'])", (CUSTOM_WORD, CUSTOM_WORD),
            )
            preserved = connection.execute(
                "SELECT (SELECT version FROM pinyin.pinyin_dictionary_meta WHERE singleton), "
                "(SELECT count(*) FROM pinyin.pinyin_words), "
                "(SELECT scalar_pinyin FROM stored_batch_upgrade_probe), "
                "(SELECT token_pinyin FROM stored_batch_upgrade_probe)"
            ).fetchone()
            expressions = connection.execute(
                "SELECT a.attname, pg_get_expr(d.adbin,d.adrelid) "
                "FROM pg_attrdef d JOIN pg_attribute a ON a.attrelid=d.adrelid AND a.attnum=d.adnum "
                "WHERE d.adrelid='stored_batch_upgrade_probe'::regclass ORDER BY a.attname"
            ).fetchall()
            self.assertEqual(preserved[2:], (CUSTOM_READING, CUSTOM_READING + " abc"))
            connection.execute("ALTER EXTENSION pg_pinyin UPDATE TO '0.0.9'")
            self.assertEqual(self.version(connection), NEW_VERSION)
            new_oids = self.owned_function_oids(connection)
            self.assertEqual({name: new_oids.get(name) for name in old_oids}, old_oids)
            self.assertEqual(len(new_oids), len(old_oids) + len(ALL_NEW_SIGNATURES))
            self.assertEqual(self.legacy_values(connection), old_values)
            self.assertEqual(self.non_array_outcome(connection), old_non_array)
            self.assertEqual(connection.execute(
                "SELECT (SELECT version FROM pinyin.pinyin_dictionary_meta WHERE singleton), "
                "(SELECT count(*) FROM pinyin.pinyin_words), "
                "(SELECT scalar_pinyin FROM stored_batch_upgrade_probe), "
                "(SELECT token_pinyin FROM stored_batch_upgrade_probe)"
            ).fetchone(), preserved)
            self.assertEqual(connection.execute(
                "SELECT a.attname, pg_get_expr(d.adbin,d.adrelid) "
                "FROM pg_attrdef d JOIN pg_attribute a ON a.attrelid=d.adrelid AND a.attnum=d.adnum "
                "WHERE d.adrelid='stored_batch_upgrade_probe'::regclass ORDER BY a.attname"
            ).fetchall(), expressions)
            self.assertEqual(connection.execute("SELECT category FROM pinyin.pinyin_token WHERE character='zzbatch'").fetchone()[0], 1)
            upgraded_catalog = self.catalog(connection)
            self.assert_batch_values(connection)
            self.assert_invalidation_and_rollback(connection)
            self.assert_rollback_version_reuse(connection)
        self.assert_parallel_read_only(upgraded_url)

        fresh_url = self.database("new", NEW_VERSION, with_search)
        with psycopg.connect(fresh_url, autocommit=True) as connection:
            self.assertEqual(self.catalog(connection), upgraded_catalog)
            self.assertEqual(self.non_array_outcome(connection), old_non_array)
            self.assert_batch_values(connection)
            self.assert_rollback_version_reuse(connection)

    def test_real_upgrade_without_pg_search_preserves_oids_data_and_parallel_safety(self):
        self.assert_real_upgrade(False)

    def test_real_upgrade_with_pg_search_matches_fresh_install(self):
        if not self.search_available:
            self.skipTest("pg_search installation SQL is unavailable")
        self.assert_real_upgrade(True)
