"""Snapshot, rollback, and explicit sync contracts on real disposable databases."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sync_dictionary", ROOT / "scripts/sync_dictionary.py")
SYNC = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SYNC)
DSN = os.environ.get("TEST_UPGRADE_DSN") or (
    os.environ.get("PGURL") if os.environ.get("PG_LOADER_TESTS") == "1" else None)


@unittest.skipUnless(DSN, "requires a prepared disposable PostgreSQL server")
class RuntimeRemediationTests(unittest.TestCase):
    def setUp(self):
        self.name = "pinyin_remediation_" + uuid.uuid4().hex[:14]
        self.url = make_conninfo(DSN, dbname=self.name)
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(self.name)))
        self.addCleanup(self.drop_database)
        with self.connect() as conn:
            conn.execute("CREATE EXTENSION pg_pinyin")
            conn.execute("TRUNCATE pinyin.pinyin_mapping,pinyin.pinyin_words")
            conn.execute("INSERT INTO pinyin.pinyin_mapping VALUES ('甲','|jia|')")

    def connect(self):
        return psycopg.connect(self.url, autocommit=True, prepare_threshold=None)

    def drop_database(self):
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(self.name)))

    def reading(self, conn):
        return conn.execute("SELECT public.pinyin_char_romanize(%s)", ("甲",)).fetchone()[0]

    def test_snapshot_cache_sees_commits_and_savepoint_rollback(self):
        with self.connect() as reader, self.connect() as writer:
            self.assertEqual(self.reading(reader), "jia")
            with reader.transaction():
                self.assertEqual(self.reading(reader), "jia")
                writer.execute("UPDATE pinyin.pinyin_mapping SET pinyin='|external|'")
                self.assertEqual(self.reading(reader), "external")
                reader.execute("SAVEPOINT local_change")
                reader.execute("UPDATE pinyin.pinyin_mapping SET pinyin='|temporary|'")
                self.assertEqual(self.reading(reader), "temporary")
                reader.execute("ROLLBACK TO SAVEPOINT local_change")
                self.assertEqual(self.reading(reader), "external")
            with reader.transaction():
                reader.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                self.assertEqual(self.reading(reader), "external")
                writer.execute("UPDATE pinyin.pinyin_mapping SET pinyin='|later|'")
                self.assertEqual(self.reading(reader), "external")
            self.assertEqual(self.reading(reader), "later")

    def test_explicit_sync_preserves_overrides_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory, self.connect() as conn:
            data = Path(directory)
            (data / "pinyin_mapping.csv").write_text('甲,|official|\n乙,|yi|\n')
            (data / "pinyin_words.csv").write_text('甲乙,jia yi\n')
            (data / "pinyin_token.csv").write_text('testword,1\n')
            self.assertEqual(SYNC.sync(conn,data), {"pinyin_mapping":1,"pinyin_words":1,"pinyin_token":1})
            self.assertEqual(self.reading(conn), "jia")
            self.assertEqual(conn.execute("SELECT public.pinyin_word_romanize(%s)",("甲乙",)).fetchone()[0],"jia yi")
            self.assertEqual(set(SYNC.sync(conn,data).values()), {0})

    def test_explicit_sync_rolls_back_all_tables_when_a_later_copy_fails(self):
        with tempfile.TemporaryDirectory() as directory, self.connect() as conn:
            data = Path(directory)
            (data / "pinyin_mapping.csv").write_text('乙,|yi|\n')
            (data / "pinyin_words.csv").write_text('甲乙,jia yi\n')
            (data / "pinyin_token.csv").write_text('broken,not-an-integer\n')
            version = conn.execute("SELECT version FROM pinyin.pinyin_dictionary_meta").fetchone()[0]
            self.assertEqual(self.reading(conn), "jia")
            with self.assertRaises(psycopg.errors.InvalidTextRepresentation):
                SYNC.sync(conn, data)
            self.assertEqual(conn.execute("SELECT * FROM pinyin.pinyin_mapping").fetchall(), [("甲", "|jia|")])
            self.assertEqual(conn.execute("SELECT count(*) FROM pinyin.pinyin_words").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT version FROM pinyin.pinyin_dictionary_meta").fetchone()[0], version)
            self.assertEqual(self.reading(conn), "jia")

    def test_direct_cast_preserves_execute_permissions(self):
        role = "pinyin_cast_" + uuid.uuid4().hex[:12]
        with self.connect() as conn:
            conn.execute(sql.SQL("CREATE ROLE {}").format(sql.Identifier(role)))
        def drop_role():
            with self.connect() as conn:
                conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
                conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
        self.addCleanup(drop_role)
        with self.connect() as conn:
            conn.execute("CREATE TYPE public.remediation_token AS ENUM ('probe')")
            conn.execute("CREATE FUNCTION public.remediation_cast(public.remediation_token) RETURNS text[] "
                         "LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$SELECT ARRAY['甲']::text[]$$")
            conn.execute("CREATE CAST (public.remediation_token AS text[]) WITH FUNCTION public.remediation_cast(public.remediation_token)")
            conn.execute("REVOKE EXECUTE ON FUNCTION public.remediation_cast(public.remediation_token) FROM PUBLIC")
            conn.execute(sql.SQL("GRANT USAGE ON SCHEMA pinyin TO {}").format(sql.Identifier(role)))
            conn.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA pinyin TO {}").format(sql.Identifier(role)))
            conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                conn.execute("SELECT public.pinyin_word_romanize('probe'::public.remediation_token)")
            conn.execute("RESET ROLE")
            conn.execute(sql.SQL("GRANT EXECUTE ON FUNCTION public.remediation_cast(public.remediation_token) TO {}").format(sql.Identifier(role)))
            conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
            self.assertEqual(conn.execute("SELECT public.pinyin_word_romanize('probe'::public.remediation_token)").fetchone()[0], "jia")
