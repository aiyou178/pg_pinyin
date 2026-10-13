"""Real 0.0.8 -> 0.0.9 installs/upgrades; opt in with TEST_UPGRADE_DSN.

The server must already have both version installation SQL files, the upgrade
SQL file, and the current shared library. This test never synthesizes an old
catalog by changing pg_extension.extversion.
"""

import os
import unittest
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo


OLD_VERSION = "0.0.8"
NEW_VERSION = "0.0.9"
PATTERNS_SIGNATURE = "public.pinyin_regex_phrase_patterns_mode(text,text,boolean)"
QUERY_SIGNATURE = "public.pinyin_regex_phrase_mode(text,text,integer,integer,boolean)"
NEW_SIGNATURES = (PATTERNS_SIGNATURE, QUERY_SIGNATURE)
CUSTOM_WORD = "升级探针"
CUSTOM_READING = "sheng ji tan zhen"


def plan_nodes(node):
    yield node
    for child in node.get("Plans", []):
        yield from plan_nodes(child)


@unittest.skipUnless(
    os.environ.get("TEST_UPGRADE_DSN"),
    "set TEST_UPGRADE_DSN after installing genuine 0.0.8 and 0.0.9 SQL files",
)
class QueryModesUpgradeTests(unittest.TestCase):
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
                    "upgrade validation requires genuine pg_pinyin installation "
                    f"SQL for {OLD_VERSION} and {NEW_VERSION}; available: {sorted(versions)}"
                )
            if not connection.execute(
                "SELECT EXISTS(SELECT 1 FROM pg_available_extensions WHERE name = 'pg_search')"
            ).fetchone()[0]:
                raise RuntimeError("pg_search must be available for the integration upgrade cases")

    def database(self, label, version, with_search):
        name = "pinyin_mode_upgrade_" + label + "_" + uuid.uuid4().hex[:10]
        url = make_conninfo(self.admin_url, dbname=name)
        with psycopg.connect(self.admin_url, autocommit=True) as connection:
            connection.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name))
            )
        self.addCleanup(self.drop_database, name)
        with psycopg.connect(url, autocommit=True) as connection:
            if with_search:
                connection.execute("CREATE EXTENSION pg_search CASCADE")
            connection.execute(
                sql.SQL("CREATE EXTENSION pg_pinyin VERSION {}").format(sql.Literal(version))
            )
            self.assertEqual(self.version(connection), version)
        return url

    def drop_database(self, name):
        with psycopg.connect(self.admin_url, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )

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
            "JOIN pg_extension e ON e.oid = d.refobjid "
            "AND d.refclassid = 'pg_extension'::regclass "
            "WHERE e.extname = 'pg_pinyin'"
        ).fetchall())

    @staticmethod
    def mode_catalog(connection):
        return connection.execute(
            "SELECT p.oid::regprocedure::text, pg_get_function_arguments(p.oid), "
            "pg_get_function_result(p.oid), p.proparallel, p.provolatile, "
            "p.proisstrict, p.pronargdefaults, l.lanname, e.extname "
            "FROM pg_proc p JOIN pg_language l ON l.oid = p.prolang "
            "LEFT JOIN pg_depend d ON d.classid = 'pg_proc'::regclass "
            "AND d.objid = p.oid AND d.deptype = 'e' "
            "AND d.refclassid = 'pg_extension'::regclass "
            "LEFT JOIN pg_extension e ON e.oid = d.refobjid "
            "WHERE p.oid IN (%s::regprocedure, %s::regprocedure) "
            "ORDER BY p.oid::regprocedure::text",
            NEW_SIGNATURES,
        ).fetchall()

    def assert_mode_catalog(self, connection, with_search):
        rows = self.mode_catalog(connection)
        self.assertEqual(len(rows), 2)
        by_name = {row[0].split("(")[0].split(".")[-1]: row for row in rows}
        patterns = by_name["pinyin_regex_phrase_patterns_mode"]
        query = by_name["pinyin_regex_phrase_mode"]
        self.assertEqual(patterns[2:], ("text[]", "s", "s", True, 1, "c", "pg_pinyin"))
        self.assertEqual(
            query[2:],
            ("pdb.query" if with_search else "text", "s", "s", False, 3, "plpgsql", "pg_pinyin"),
        )
        return rows

    def assert_patterns(self, connection):
        cases = [
            ("whh", "initials", ["w.*", "h.*", "h.*"]),
            ("xi'an", "full", ["xi", "an"]),
            ("xi an", "autocomplete", ["xi", "an.*"]),
            ("LǛ LIÁNG", "full", ["lv", "liang"]),
            ("lu:4lu:e0", "full", ["lv", "lve"]),
            ("whh", "full", []),
            ("中文", "full", []),
            ("zz", "full", []),
        ]
        for value, mode, expected in cases:
            with self.subTest(value=value, mode=mode):
                self.assertEqual(connection.execute(
                    "SELECT public.pinyin_regex_phrase_patterns_mode(%s, %s)",
                    (value, mode),
                ).fetchone()[0], expected)
        with self.assertRaises(psycopg.errors.InvalidParameterValue):
            connection.execute("SELECT public.pinyin_regex_phrase_patterns_mode('xi', 'unknown')")

    def assert_parallel_read_only(self, url):
        with psycopg.connect(url, autocommit=True) as connection:
            connection.execute(
                "CREATE TABLE upgrade_parallel_probe AS SELECT "
                "CASE WHEN n % 2 = 0 THEN 'whh' ELSE 'xi''an' END::text AS value "
                "FROM generate_series(1, 100000) AS n"
            )
            connection.execute("ALTER TABLE upgrade_parallel_probe SET (parallel_workers = 2)")
            connection.execute("ANALYZE upgrade_parallel_probe")
        cases = [
            ("initials", ["w.*", "h.*", "h.*"]),
            ("full", ["xi", "an"]),
            ("autocomplete", ["xi", "an.*"]),
        ]
        for mode, expected in cases:
            with self.subTest(parallel_mode=mode), psycopg.connect(url, autocommit=True) as connection:
                connection.execute("BEGIN READ ONLY")
                try:
                    connection.execute("SET LOCAL max_parallel_workers_per_gather = 2")
                    connection.execute("SET LOCAL min_parallel_table_scan_size = 0")
                    connection.execute("SET LOCAL parallel_setup_cost = 0")
                    connection.execute("SET LOCAL parallel_tuple_cost = 0")
                    self.assertTrue(connection.execute(
                        "SELECT pg_current_xact_id_if_assigned() IS NULL"
                    ).fetchone()[0])
                    query = (
                        "SELECT count(*) FROM upgrade_parallel_probe "
                        "WHERE public.pinyin_regex_phrase_patterns_mode(value, %s) = %s::text[]"
                    )
                    plan = connection.execute(
                        "EXPLAIN (ANALYZE, FORMAT JSON) " + query, (mode, expected)
                    ).fetchone()[0][0]["Plan"]
                    nodes = list(plan_nodes(plan))
                    self.assertTrue(any(node["Node Type"] in ("Gather", "Gather Merge") for node in nodes))
                    self.assertGreater(sum(node.get("Workers Launched", 0) for node in nodes), 0)
                    self.assertTrue(any(
                        node.get("Parallel Aware")
                        and "pinyin_regex_phrase_patterns_mode" in node.get("Filter", "")
                        for node in nodes
                    ), "pattern construction must execute below Gather in a parallel scan")
                    self.assertEqual(connection.execute(query, (mode, expected)).fetchone()[0], 50000)
                    self.assertTrue(connection.execute(
                        "SELECT pg_current_xact_id_if_assigned() IS NULL"
                    ).fetchone()[0])
                finally:
                    connection.execute("ROLLBACK")

    def assert_bm25_query(self, connection):
        connection.execute("CREATE TABLE upgrade_search_probe(id integer PRIMARY KEY, pinyin text NOT NULL)")
        connection.execute(
            "INSERT INTO upgrade_search_probe VALUES (1, 'xi an'), (2, 'xi ang'), "
            "(3, 'xian'), (4, '|xi| |an|')"
        )
        connection.execute(
            "CREATE INDEX upgrade_search_probe_bm25 ON upgrade_search_probe "
            "USING bm25(id, (pinyin::pdb.whitespace)) WITH (key_field='id')"
        )
        for mode, generated, expected in [("full", False, [1]), ("autocomplete", False, [1, 2]), ("full", True, [1, 4])]:
            with self.subTest(search_mode=mode, generated=generated):
                rows = connection.execute(
                    "SELECT id FROM upgrade_search_probe WHERE pinyin @@@ "
                    "public.pinyin_regex_phrase_mode('xi an', %s, NULL, NULL, %s) ORDER BY id",
                    (mode, generated),
                ).fetchall()
                self.assertEqual([row[0] for row in rows], expected)
        self.assertEqual(connection.execute(
            "SELECT id FROM upgrade_search_probe WHERE id = 3 OR pinyin @@@ "
            "public.pinyin_regex_phrase_mode('中文', 'full') ORDER BY id"
        ).fetchall(), [(3,)])

    def test_real_upgrade_preserves_legacy_data_and_matches_fresh_install(self):
        upgraded_url = self.database("search_old", OLD_VERSION, True)
        with psycopg.connect(upgraded_url, autocommit=True) as connection:
            for signature in NEW_SIGNATURES:
                self.assertIsNone(connection.execute("SELECT to_regprocedure(%s)", (signature,)).fetchone()[0])
            old_oids = self.owned_function_oids(connection)
            self.assertTrue(old_oids)
            old_null = connection.execute(
                "SELECT public.pinyin_regex_phrase('lunlun', NULL, 4)::text, "
                "public.pinyin_regex_phrase(NULL, NULL, 4) IS NULL, "
                "public.pinyin_regex_phrase_patterns('whh')"
            ).fetchone()
            connection.execute("INSERT INTO pinyin.pinyin_token(character, category) VALUES ('zz', 1)")
            connection.execute(
                "INSERT INTO pinyin.pinyin_words(word, pinyin) VALUES (%s, %s) "
                "ON CONFLICT (word) DO UPDATE SET pinyin = EXCLUDED.pinyin",
                (CUSTOM_WORD, CUSTOM_READING),
            )
            connection.execute(
                "CREATE TABLE upgrade_stored_probe(name text, pinyin text "
                "GENERATED ALWAYS AS (public.pinyin_word_romanize(name)) STORED)"
            )
            connection.execute("INSERT INTO upgrade_stored_probe(name) VALUES (%s)", (CUSTOM_WORD,))
            preserved = connection.execute(
                "SELECT (SELECT version FROM pinyin.pinyin_dictionary_meta WHERE singleton), "
                "(SELECT pinyin FROM upgrade_stored_probe), "
                "(SELECT count(*) FROM pinyin.pinyin_words)"
            ).fetchone()
            self.assertEqual(preserved[1], CUSTOM_READING)
            connection.execute("ALTER EXTENSION pg_pinyin UPDATE TO '0.0.9'")
            self.assertEqual(self.version(connection), NEW_VERSION)
            new_oids = self.owned_function_oids(connection)
            self.assertEqual({name: new_oids.get(name) for name in old_oids}, old_oids)
            # The unreleased 0.0.9 also adds the two document-batch functions.
            self.assertEqual(len(new_oids), len(old_oids) + len(NEW_SIGNATURES) + 2)
            self.assertEqual(connection.execute(
                "SELECT public.pinyin_regex_phrase('lunlun', NULL, 4)::text, "
                "public.pinyin_regex_phrase(NULL, NULL, 4) IS NULL, "
                "public.pinyin_regex_phrase_patterns('whh')"
            ).fetchone(), old_null)
            self.assertEqual(connection.execute(
                "SELECT (SELECT version FROM pinyin.pinyin_dictionary_meta WHERE singleton), "
                "(SELECT pinyin FROM upgrade_stored_probe), "
                "(SELECT count(*) FROM pinyin.pinyin_words)"
            ).fetchone(), preserved)
            self.assertEqual(connection.execute(
                "SELECT category FROM pinyin.pinyin_token WHERE character = 'zz'"
            ).fetchone()[0], 1)
            self.assertEqual(connection.execute(
                "SELECT pinyin FROM pinyin.pinyin_words WHERE word = %s", (CUSTOM_WORD,)
            ).fetchone()[0], CUSTOM_READING)
            upgraded_catalog = self.assert_mode_catalog(connection, True)
            self.assert_patterns(connection)
            self.assert_bm25_query(connection)
        self.assert_parallel_read_only(upgraded_url)

        fresh_url = self.database("search_new", NEW_VERSION, True)
        with psycopg.connect(fresh_url, autocommit=True) as connection:
            self.assertEqual(self.assert_mode_catalog(connection, True), upgraded_catalog)
            self.assert_patterns(connection)
            self.assert_bm25_query(connection)

    def test_available_but_uninstalled_pg_search_uses_stub_on_install_and_upgrade(self):
        catalogs = []
        for version in (OLD_VERSION, NEW_VERSION):
            with self.subTest(start_version=version):
                url = self.database("no_search", version, False)
                with psycopg.connect(url, autocommit=True) as connection:
                    self.assertFalse(connection.execute(
                        "SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname = 'pg_search')"
                    ).fetchone()[0])
                    if version == OLD_VERSION:
                        for signature in NEW_SIGNATURES:
                            self.assertIsNone(connection.execute("SELECT to_regprocedure(%s)", (signature,)).fetchone()[0])
                        old_oids = self.owned_function_oids(connection)
                        connection.execute("ALTER EXTENSION pg_pinyin UPDATE TO '0.0.9'")
                        new_oids = self.owned_function_oids(connection)
                        self.assertEqual({name: new_oids.get(name) for name in old_oids}, old_oids)
                    self.assertEqual(self.version(connection), NEW_VERSION)
                    catalogs.append(self.assert_mode_catalog(connection, False))
                    self.assert_patterns(connection)
                    with self.assertRaisesRegex(
                        psycopg.errors.RaiseException,
                        "pinyin_regex_phrase_mode requires CREATE EXTENSION pg_search",
                    ):
                        connection.execute("SELECT public.pinyin_regex_phrase_mode('xi an', 'full')")
                    self.assertFalse(connection.execute(
                        "SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname = 'pg_search')"
                    ).fetchone()[0])
        self.assertEqual(catalogs[0], catalogs[1])


if __name__ == "__main__":
    unittest.main()
