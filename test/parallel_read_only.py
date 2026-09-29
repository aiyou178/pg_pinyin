"""Run real parallel workers in fresh, read-only transactions (no preassigned XID)."""

import os
import unittest
from uuid import uuid4

import psycopg
from psycopg import sql


class ParallelReadOnlyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.url = os.environ.get("PGURL", "postgres://localhost/postgres")
        cls.schema = "pinyin_parallel_" + uuid4().hex
        cls.suffix = "_" + cls.schema
        cls.mapping = sql.Identifier("pinyin", "pinyin_mapping" + cls.suffix)
        cls.words = sql.Identifier("pinyin", "pinyin_words" + cls.suffix)
        cls.probe = sql.Identifier(cls.schema, "probe")
        with psycopg.connect(cls.url, autocommit=True) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS pg_pinyin")
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(cls.schema)))
            cls.addClassCleanup(cls.cleanup)
            conn.execute(
                sql.SQL(
                    "CREATE TABLE {} AS SELECT '郑爽'::text AS name "
                    "FROM generate_series(1, 2000)"
                ).format(cls.probe)
            )
            conn.execute(
                sql.SQL("ALTER TABLE {} SET (parallel_workers = 2)").format(cls.probe)
            )
            conn.execute(sql.SQL("ANALYZE {}").format(cls.probe))
            conn.execute(
                sql.SQL(
                    "CREATE TABLE {} (character text PRIMARY KEY, pinyin text)"
                ).format(cls.mapping)
            )
            conn.execute(
                sql.SQL("INSERT INTO {} VALUES ('郑', '|zhengx|')").format(cls.mapping)
            )
            conn.execute(
                sql.SQL("CREATE TABLE {} (word text PRIMARY KEY, pinyin text)").format(
                    cls.words
                )
            )
            conn.execute(
                sql.SQL("INSERT INTO {} VALUES ('郑爽', '|zhengx| |shuangx|')").format(
                    cls.words
                )
            )

    @classmethod
    def cleanup(cls):
        with psycopg.connect(cls.url, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(cls.schema))
            )
            conn.execute(
                sql.SQL("DROP TABLE IF EXISTS {}, {}").format(cls.mapping, cls.words)
            )

    def test_romanization_in_parallel_workers(self):
        cases = [
            ("pinyin_char_romanize(name || 'ABC')", "zheng shuang abc"),
            ("pinyin_word_romanize(name || 'ABC')", "zheng shuang abc"),
            ("pinyin_word_romanize(ARRAY[name, 'ABC'])", "zheng shuang abc"),
            ("pinyin_char_romanize(name || 'ABC', {suffix})", "zhengx shuang abc"),
            ("pinyin_word_romanize(name || 'ABC', {suffix})", "zhengx shuangx abc"),
            (
                "pinyin_word_romanize(ARRAY[name, 'ABC'], {suffix})",
                "zhengx shuangx abc",
            ),
            (
                "pinyin_word_romanize(name || 'ABC', '_missing_parallel_suffix')",
                "zheng shuang abc",
            ),
        ]
        for expression, expected in cases:
            with self.subTest(expression=expression):
                query = sql.SQL("SELECT count(*) FROM {} WHERE public.{} = {}").format(
                    self.probe,
                    sql.SQL(expression).format(suffix=sql.Literal(self.suffix)),
                    sql.Literal(expected),
                )
                with psycopg.connect(self.url) as conn:
                    conn.execute("SET TRANSACTION READ ONLY")
                    conn.execute("SET LOCAL max_parallel_workers_per_gather = 2")
                    conn.execute("SET LOCAL min_parallel_table_scan_size = 0")
                    conn.execute("SET LOCAL parallel_setup_cost = 0")
                    conn.execute("SET LOCAL parallel_tuple_cost = 0")
                    self.assertIsNone(
                        conn.execute(
                            "SELECT pg_current_xact_id_if_assigned()"
                        ).fetchone()[0]
                    )
                    plan = conn.execute(
                        sql.SQL("EXPLAIN (ANALYZE, FORMAT JSON) ") + query
                    ).fetchone()[0][0]["Plan"]
                    nodes = [plan]
                    launched = 0
                    while nodes:
                        node = nodes.pop()
                        launched += node.get("Workers Launched", 0)
                        nodes.extend(node.get("Plans", []))
                    self.assertGreater(launched, 0, plan)
                    self.assertEqual(conn.execute(query).fetchone()[0], 2000)
                    self.assertIsNone(
                        conn.execute(
                            "SELECT pg_current_xact_id_if_assigned()"
                        ).fetchone()[0]
                    )


if __name__ == "__main__":
    unittest.main()
