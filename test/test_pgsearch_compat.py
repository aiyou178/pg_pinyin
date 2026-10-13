"""Focused pg_search 0.26.1 smoke tests, including the experimental PG19 patch.

Enable PG_LOADER_TESTS=1 and set PGURL to an administrative connection with
CREATE DATABASE privileges. Every case owns a fresh database. These checks
cover private exact bitmaps only, not shared or lossy bitmap execution.
"""

import os
import unittest
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo


PG_SEARCH_VERSION = "0.26.1"


def plan_nodes(node):
    yield node
    for child in node.get("Plans", []):
        yield from plan_nodes(child)


@unittest.skipUnless(
    os.environ.get("PG_LOADER_TESTS") == "1" and os.environ.get("PGURL"),
    "enable PG_LOADER_TESTS=1 and PGURL for isolated PostgreSQL integration tests",
)
class PgSearchCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin_url = os.environ["PGURL"]
        with psycopg.connect(cls.admin_url, autocommit=True) as connection:
            versions = {
                row[0] for row in connection.execute(
                    "SELECT version FROM pg_available_extension_versions "
                    "WHERE name = 'pg_search'"
                )
            }
        if PG_SEARCH_VERSION not in versions:
            raise unittest.SkipTest(
                f"pg_search {PG_SEARCH_VERSION} is required; available: {sorted(versions)}"
            )

    def setUp(self):
        self.database = "pgsearch_compat_" + uuid.uuid4().hex[:16]
        self.url = make_conninfo(self.admin_url, dbname=self.database)
        with psycopg.connect(self.admin_url, autocommit=True) as connection:
            connection.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                    sql.Identifier(self.database)
                )
            )
        self.addCleanup(self.remove_database)
        with self.connect() as connection:
            connection.execute(
                sql.SQL("CREATE EXTENSION pg_search VERSION {} CASCADE").format(
                    sql.Literal(PG_SEARCH_VERSION)
                )
            )
            self.assertEqual(connection.execute(
                "SELECT extversion FROM pg_extension WHERE extname = 'pg_search'"
            ).fetchone()[0], PG_SEARCH_VERSION)

    def connect(self):
        connection = psycopg.connect(self.url, autocommit=True, prepare_threshold=None)
        connection.execute("SET statement_timeout = '30s'")
        connection.execute("SET max_parallel_workers_per_gather = 0")
        connection.execute("SET paradedb.enable_aggregate_custom_scan = off")
        return connection

    def remove_database(self):
        with psycopg.connect(self.admin_url, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                    sql.Identifier(self.database)
                )
            )

    @staticmethod
    def create_docs(connection, many=False):
        connection.execute(
            "CREATE TABLE compat_docs (id bigint PRIMARY KEY, body text, "
            "tag text, note text) WITH (autovacuum_enabled = false, fillfactor = 70)"
        )
        if many:
            connection.execute(
                "INSERT INTO compat_docs SELECT i, 'alpha', 'tag' || (i % 50), "
                "repeat('x', 300) FROM generate_series(0, 9999) AS i"
            )
        else:
            connection.execute(
                "INSERT INTO compat_docs VALUES "
                "(1, 'alpha', 'tag-a', 'before'), "
                "(2, 'beta', 'tag-b', 'before'), "
                "(3, 'alpha', 'tag-a', 'before')"
            )
        connection.execute(
            "CREATE INDEX compat_docs_search ON compat_docs USING paradedb "
            "(id, (body::pdb.whitespace)) WITH (key_field = 'id', "
            "sort_by = 'ctid ASC NULLS FIRST', mutable_segment_rows = 0, "
            "target_segment_count = 1)"
        )
        connection.execute("VACUUM (ANALYZE) compat_docs")

    @staticmethod
    def explain(connection, query, parameters=()):
        return connection.execute(
            "EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF, FORMAT JSON) " + query,
            parameters,
        ).fetchone()[0][0]["Plan"]

    @staticmethod
    def ids(connection, value):
        return [row[0] for row in connection.execute(
            "SELECT id FROM compat_docs WHERE body @@@ pdb.regex(%s) ORDER BY id",
            (value,),
        )]

    def assert_ids(self, connection, value, expected):
        self.assertEqual(self.ids(connection, value), expected)
        ordinary = [row[0] for row in connection.execute(
            "SELECT id FROM compat_docs WHERE body = %s ORDER BY id", (value,)
        )]
        self.assertEqual(ordinary, expected)

    def test_plain_operator_forced_sequential_fallback(self):
        with self.connect() as connection:
            self.create_docs(connection)
            connection.execute("SET paradedb.enable_custom_scan = off")
            connection.execute("SET paradedb.enable_join_custom_scan = off")
            connection.execute("SET enable_indexscan = off")
            connection.execute("SET enable_indexonlyscan = off")
            connection.execute("SET enable_bitmapscan = off")
            plan = self.explain(
                connection,
                "SELECT id FROM compat_docs WHERE body @@@ pdb.regex('alpha') ORDER BY id",
            )
            nodes = list(plan_nodes(plan))
            self.assertTrue(any(
                node["Node Type"] == "Seq Scan"
                and node.get("Relation Name") == "compat_docs"
                for node in nodes
            ), plan)
            self.assertFalse(any(node["Node Type"] == "Custom Scan" for node in nodes), plan)
            self.assertFalse(any(
                node["Node Type"] in ("Index Scan", "Index Only Scan", "Bitmap Heap Scan")
                for node in nodes
            ), plan)
            self.assert_ids(connection, "alpha", [1, 3])
            self.assert_ids(connection, "beta", [2])
            self.assert_ids(connection, "missing", [])
            self.assertEqual(connection.execute(
                "SELECT id FROM compat_docs WHERE body @@@ pdb.empty() OR id = 2"
            ).fetchall(), [(2,)])

    def test_update_delete_commit_and_rollback_visibility(self):
        with self.connect() as writer, self.connect() as observer:
            self.create_docs(writer)
            writer.execute("SET enable_seqscan = off")
            observer.execute("SET enable_seqscan = off")
            plan = self.explain(
                observer,
                "SELECT id FROM compat_docs WHERE body @@@ pdb.regex('alpha') ORDER BY id",
            )
            self.assertTrue(any(
                node.get("Custom Plan Provider") == "ParadeDB Base Scan"
                or node.get("Index Name") == "compat_docs_search"
                for node in plan_nodes(plan)
            ), plan)
            self.assert_ids(observer, "alpha", [1, 3])

            with writer.transaction(force_rollback=True):
                writer.execute("UPDATE compat_docs SET body = 'beta' WHERE id = 1")
                writer.execute("DELETE FROM compat_docs WHERE id = 3")
                writer.execute(
                    "INSERT INTO compat_docs VALUES (4, 'alpha', 'tag-a', 'rolled-back')"
                )
                self.assert_ids(writer, "alpha", [4])
                self.assert_ids(writer, "beta", [1, 2])
                self.assert_ids(observer, "alpha", [1, 3])
                self.assert_ids(observer, "beta", [2])
            self.assert_ids(observer, "alpha", [1, 3])
            self.assert_ids(observer, "beta", [2])

            with writer.transaction():
                writer.execute("UPDATE compat_docs SET body = 'beta' WHERE id = 1")
                writer.execute("DELETE FROM compat_docs WHERE id = 3")
                writer.execute(
                    "INSERT INTO compat_docs VALUES (4, 'alpha', 'tag-a', 'committed')"
                )
            self.assert_ids(observer, "alpha", [4])
            self.assert_ids(observer, "beta", [1, 2])
            writer.execute("UPDATE compat_docs SET note = 'changed' WHERE id = 4")
            self.assert_ids(observer, "alpha", [4])
            self.assertEqual(observer.execute(
                "SELECT note FROM compat_docs WHERE body @@@ pdb.regex('alpha')"
            ).fetchall(), [("changed",)])
            writer.execute("VACUUM (INDEX_CLEANUP ON, ANALYZE) compat_docs")
            self.assert_ids(observer, "alpha", [4])
            self.assert_ids(observer, "beta", [1, 2])

    def test_private_exact_btree_bitmap_intersection(self):
        with self.connect() as connection:
            self.create_docs(connection, many=True)
            connection.execute("CREATE INDEX compat_docs_tag ON compat_docs (tag)")
            connection.execute("ANALYZE compat_docs")
            connection.execute("SET paradedb.enable_filter_pushdown = on")
            connection.execute("SET paradedb.enable_bitmap_intersection = on")
            query = (
                "SELECT id FROM compat_docs WHERE body @@@ pdb.regex('alpha') "
                "AND tag = 'tag13' ORDER BY id LIMIT 5"
            )
            plan = self.explain(connection, query)
            nodes = list(plan_nodes(plan))
            intersections = [
                node for node in nodes
                if "compat_docs_tag" in node.get("Bitmap Intersection", "")
            ]
            self.assertTrue(intersections, plan)
            self.assertTrue(any(
                node["Node Type"] == "Bitmap Index Scan"
                and node.get("Index Name") == "compat_docs_tag" for node in nodes
            ), plan)
            self.assertGreater(sum(
                node.get("Bitmap Exact Pages", 0) for node in intersections
            ), 0, plan)
            self.assertEqual(connection.execute(query).fetchall(),
                             [(13,), (63,), (113,), (163,), (213,)])
            all_query = query.removesuffix(" LIMIT 5")
            expected = [(i,) for i in range(13, 10000, 50)]
            self.assertEqual(connection.execute(all_query).fetchall(), expected)

            connection.execute("SET paradedb.enable_bitmap_intersection = off")
            without_bitmap = self.explain(connection, query)
            self.assertFalse(any(
                "Bitmap Intersection" in node for node in plan_nodes(without_bitmap)
            ), without_bitmap)
            self.assertEqual(connection.execute(all_query).fetchall(), expected)


if __name__ == "__main__":
    unittest.main()
