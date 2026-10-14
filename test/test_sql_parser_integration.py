"""Independent SQL parser contracts, live inventory, and read-only workers."""

from contextlib import contextmanager
import gzip
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo


ROOT = Path(__file__).resolve().parents[1]
MODES = ("compat", "full", "initials", "autocomplete", "auto")
SPEC = importlib.util.spec_from_file_location(
    "sql_parser_integration_benchmark", ROOT / "scripts/benchmark_sql_parser.py"
)
BENCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BENCH)


def plan_nodes(plan):
    yield plan
    for child in plan.get("Plans", []):
        yield from plan_nodes(child)


@unittest.skipUnless(
    os.environ.get("PG_LOADER_TESTS") == "1" and os.environ.get("PGURL"),
    "set PG_LOADER_TESTS=1 and PGURL in a prepared pg_search test container",
)
class SqlParserIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin_url = os.environ["PGURL"]
        cls.database = "pinyin_sql_parser_" + uuid.uuid4().hex[:12]
        cls.url = make_conninfo(cls.admin_url, dbname=cls.database)
        with psycopg.connect(cls.admin_url, autocommit=True) as connection:
            connection.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(cls.database))
            )
        cls.addClassCleanup(cls.remove_database)
        with psycopg.connect(cls.url, autocommit=True) as connection:
            connection.execute("CREATE EXTENSION pg_search CASCADE")
            connection.execute((ROOT / "sql/pinyin.sql").read_text(encoding="utf-8"))
            connection.execute((ROOT / "sql/word.sql").read_text(encoding="utf-8"))
            connection.execute("TRUNCATE pinyin.pinyin_mapping,pinyin.pinyin_token,pinyin.pinyin_words")
            for table, filename in (
                ("pinyin_mapping", "pinyin_mapping.csv"),
                ("pinyin_token", "pinyin_token.csv"),
                ("pinyin_words", "pinyin_words.csv"),
            ):
                with connection.cursor().copy(
                    sql.SQL("COPY pinyin.{} FROM STDIN WITH (FORMAT CSV)").format(sql.Identifier(table))
                ) as copy:
                    copy.write((ROOT / "sql/data" / filename).read_bytes())
                connection.execute(sql.SQL("ANALYZE pinyin.{}").format(sql.Identifier(table)))
            connection.execute(
                "CREATE TABLE sql_parser_parallel_probe AS "
                "SELECT CASE n % 4 WHEN 0 THEN 'whh' WHEN 1 THEN 'xi a' "
                "WHEN 2 THEN 'xi ang' ELSE 'invalid!' END AS value "
                "FROM generate_series(1,2000) AS n"
            )
            connection.execute("ALTER TABLE sql_parser_parallel_probe SET (parallel_workers = 2)")
            connection.execute("ANALYZE sql_parser_parallel_probe")

    @classmethod
    def remove_database(cls):
        with psycopg.connect(cls.admin_url, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(cls.database))
            )

    def patterns(self, connection, value, mode="full", generated=False):
        return connection.execute(
            "SELECT public.sql_pinyin_regex_phrase_patterns_mode(%s,%s,%s)",
            (value, mode, generated),
        ).fetchone()[0]

    @contextmanager
    def connection(self):
        connection = psycopg.connect(self.url)
        try:
            yield connection
        finally:
            try:
                connection.rollback()
            finally:
                connection.close()

    def test_parser_installation_has_no_rust_extension_or_function_dependency(self):
        signatures = (
            "public.pinyin_tokenize(text,boolean)",
            "public.sql_pinyin_regex_phrase_patterns(text,boolean)",
            "public.sql_pinyin_regex_phrase_patterns_mode(text,text,boolean)",
        )
        with self.connection() as connection:
            self.assertFalse(connection.execute(
                "SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname='pg_pinyin')"
            ).fetchone()[0])
            self.assertIsNone(connection.execute(
                "SELECT to_regprocedure('public.pinyin_regex_phrase_patterns(text,boolean)')"
            ).fetchone()[0])
            for signature in signatures:
                with self.subTest(signature=signature):
                    self.assertEqual(connection.execute(
                        "SELECT proparallel,provolatile FROM pg_proc WHERE oid=%s::regprocedure",
                        (signature,),
                    ).fetchone(), ("s", "s"))

    def test_benchmark_cli_publishes_consumed_sql_only_samples_and_ranked_plans(self):
        with BENCH.connection(self.admin_url, self.database) as connection:
            self.assertIsNone(connection.prepare_threshold)
            self.assertTrue(connection.autocommit)
            major = int(connection.execute("SHOW server_version_num").fetchone()[0]) // 10000
        self.assertIn(major, (18, 19))
        with tempfile.TemporaryDirectory(prefix="pinyin_sql_parser_report_") as directory:
            raw_path = Path(directory) / "report.json.gz"
            markdown_path = Path(directory) / "report.md"
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts/benchmark_sql_parser.py"),
                 "--rows", "20", "--queries", "3", "--repeats", "1", "--top-k", "3",
                 "--postgresql-version-pattern", f"PostgreSQL {major}",
                 "--json-output", str(raw_path), "--markdown-output", str(markdown_path)],
                cwd=ROOT, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            databases = re.findall(r"\[setup\] created (pg_pinyin_sql_parser_[0-9a-f]{16})", result.stdout)
            self.assertEqual(len(set(databases)), 2)
            with psycopg.connect(self.admin_url, autocommit=True) as connection:
                self.assertEqual(connection.execute(
                    "SELECT datname FROM pg_database WHERE datname=ANY(%s)", (databases,),
                ).fetchall(), [])
            raw = gzip.decompress(raw_path.read_bytes())
            report = json.loads(raw)
            self.assertIn(hashlib.sha256(raw).hexdigest(), markdown_path.read_text(encoding="utf-8"))

        expected_cells = {(implementation, mode)
                          for implementation in ("baseline", "current") for mode in MODES}
        self.assertEqual(len(report["setups"]), 2)
        self.assertEqual({setup["implementation"] for setup in report["setups"]}, {"baseline", "current"})
        for setup in report["setups"]:
            with self.subTest(setup=setup["implementation"]):
                self.assertEqual(setup["round"], 0)
                self.assertEqual(setup["rows"], 20)
                self.assertEqual(setup["extensions"]["pg_search"], "0.26.1")
                self.assertNotIn("pg_pinyin", setup["extensions"])
                self.assertGreater(setup["index_bytes"], 0)
                self.assertTrue(setup["installed_functions"])
                self.assertTrue(all(function["language"] in ("sql", "plpgsql")
                                    for function in setup["installed_functions"].values()))

        self.assertEqual(len(report["verification"]), 1)
        verification = report["verification"][0]
        self.assertEqual(set(verification["workloads"]), set(MODES))
        null_gold = [case for case in verification["gold"]
                     if case["mode"] == "auto" and case["input"] is None]
        self.assertEqual(len(null_gold), 1)
        self.assertIsNone(null_gold[0]["patterns"])
        self.assertTrue(null_gold[0]["null_constructor_preserved"])
        self.assertEqual(null_gold[0]["retrieval_skipped"], "NULL query is not evaluated with @@@")
        self.assertNotIn("hit_count", null_gold[0])

        self.assertEqual(len(report["samples"]), 10)
        self.assertEqual({(sample["implementation"], sample["mode"])
                          for sample in report["samples"]}, expected_cells)
        for sample in report["samples"]:
            with self.subTest(implementation=sample["implementation"], mode=sample["mode"]):
                self.assertEqual(sample["round"], 0)
                for stage in ("patterns", "constructor"):
                    aggregate = sample[stage]
                    self.assertEqual(aggregate["output_rows"], 3)
                    self.assertEqual(aggregate["null_outputs"], 0)
                    self.assertGreater(aggregate["output_bytes"], 0)
                    for key in ("first_output_sha256", "output_sha256"):
                        self.assertRegex(aggregate[key], r"^[0-9a-f]{64}$")
                    for key in ("first_fresh_backend_call_ms", "server_execution_ms", "client_explain_ms"):
                        self.assertTrue(math.isfinite(aggregate[key]))
                        self.assertGreater(aggregate[key], 0)

                search = sample["search"]
                self.assertEqual(search["query_count"], 3)
                self.assertEqual(search["top_k"], 3)
                self.assertEqual(len(search["per_query_ms"]), 3)
                for duration in (*search["per_query_ms"], search["elapsed_ms"],
                                 search["first_fresh_backend_call_ms"]):
                    self.assertTrue(math.isfinite(duration))
                    self.assertGreater(duration, 0)
                checked = verification["workloads"][sample["mode"]]["queries"]
                self.assertEqual(len(checked), 3)
                ranked = [query["top_k"] for query in checked]
                self.assertTrue(all(0 < len(rows) <= 3 for rows in ranked))
                encoded = json.dumps(ranked, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()
                self.assertEqual(search["ranked_output_sha256"], hashlib.sha256(encoded).hexdigest())
                nodes = list(plan_nodes(search["plan"][0]["Plan"]))
                self.assertTrue(any(node.get("Node Type") == "Custom Scan"
                                    and "ParadeDB" in node.get("Custom Plan Provider", "")
                                    and "TopK" in node.get("Exec Method", "") for node in nodes))

    def test_all_existing_pattern_fixtures(self):
        fixtures = json.loads((ROOT / "test/fixtures/pinyin_query_modes.json").read_text(encoding="utf-8"))
        with self.connection() as connection:
            for case in fixtures["pattern_cases"]:
                with self.subTest(label=case["label"]):
                    self.assertEqual(
                        self.patterns(connection, case["input"], case["mode"], case["generated"]),
                        case["patterns"],
                    )
                    if case["mode"] == "compat":
                        self.assertEqual(connection.execute(
                            "SELECT public.sql_pinyin_regex_phrase_patterns(%s,%s)",
                            (case["input"], case["generated"]),
                        ).fetchone()[0], case["patterns"])

    def test_auto_full_initials_and_completion_contracts_in_both_storages(self):
        cases = (
            ("xi ang", ["xi", "ang"], ["xi", "ang"]),
            ("xi an", ["xi", "an.*"], ["xi", "an"]),
            ("xi a", ["xi", "a.*"], ["xi", "a"]),
            ("xi'an", ["xi", "an.*"], ["xi", "an"]),
            ("xi\u2019an", ["xi", "an.*"], ["xi", "an"]),
            ("xi\u2018an", ["xi", "an.*"], ["xi", "an"]),
            ("xi1 an1", ["xi", "an"], ["xi", "an"]),
            ("whh", ["w.*", "h.*", "h.*"], ["w", "h", "h"]),
            ("w h h", ["w.*", "h.*", "h.*"], ["w", "h", "h"]),
            ("n h", ["n.*", "h.*"], ["n", "h"]),
            ("m n", ["m.*", "n.*"], ["m", "n"]),
            ("a h", ["a.*", "h.*"], ["a", "h"]),
            ("e o", ["e.*", "o.*"], ["e", "o"]),
            ("a1 h", ["a", "h.*"], ["a", "h"]),
            ("wu hh", ["wu.*", "h.*", "h.*"], ["wu", "h", "h"]),
            ("ni yon", ["ni", "yon.*"], ["ni", "yo", "n"]),
            ("niyon", ["ni", "yon.*"], ["ni", "yo", "n"]),
            ("L\u01db LI\u00c1NG", ["lv", "liang"], ["lv", "liang"]),
            ("lu:4liang2", ["lv", "liang"], ["lv", "liang"]),
        )
        with self.connection() as connection:
            for value, plain, generated_tokens in cases:
                with self.subTest(value=value, storage="plain"):
                    self.assertEqual(self.patterns(connection, value, "auto"), plain)
                with self.subTest(value=value, storage="generated"):
                    self.assertEqual(
                        self.patterns(connection, value, "auto", True),
                        [r".*\|" + token + ".*" for token in generated_tokens],
                    )

    def test_null_empty_malformed_tones_and_bounds(self):
        invalid = (
            "", " \t ", "invalid!", "xi-an", "ni33", "ni6", "u::", "\u00fc:",
            "h\u01ce\u00f3", "\u0101\u00ed", "n\u01d03", "m\u0301a", "n\u0304\u0301i",
        )
        # Compatibility intentionally rejects accents/numeric tones rather than normalizing them.
        with self.connection() as connection:
            for mode in MODES:
                for value in invalid:
                    with self.subTest(mode=mode, value=value):
                        self.assertEqual(self.patterns(connection, value, mode), [])
                for value, argument_mode, generated in ((None, mode, False), ("xi", None, False)):
                    with self.subTest(null=(value, argument_mode, generated)):
                        self.assertIsNone(self.patterns(connection, value, argument_mode, generated))
                self.assertIsNone(self.patterns(connection, "xi", mode, None))
                if mode != "compat":
                    for value in ("a" * 257, " ".join(["a"] * 65)):
                        with self.subTest(mode=mode, over_bound=value):
                            self.assertEqual(self.patterns(connection, value, mode), [])
            # Bounds were added only to the opt-in modes, not the default API.
            self.assertEqual(self.patterns(connection, "a" * 257, "compat"), ["a.*"] * 257)
            self.assertEqual(self.patterns(connection, " ".join(["a"] * 65), "compat"), ["a.*"] * 65)
            self.assertEqual(connection.execute(
                "SELECT public.sql_pinyin_regex_phrase_patterns('xi an',NULL)"
            ).fetchone()[0], ["xi.*", "an.*"])
            for mode in ("full", "autocomplete", "auto"):
                expected = ["a"] * 64
                if mode == "autocomplete":
                    expected[-1] += ".*"
                elif mode == "auto":
                    expected = ["a.*"] * 64
                with self.subTest(boundary_mode=mode):
                    self.assertEqual(self.patterns(connection, " ".join(["a"] * 64), mode), expected)
            self.assertEqual(self.patterns(connection, "lu\u0308\u030cliang", "full"), ["lv", "liang"])
            self.assertEqual(self.patterns(connection, "ni3hao3", "full"), ["ni", "hao"])
            self.assertEqual(self.patterns(connection, "n\u01d0h\u01ceo", "full"), ["ni", "hao"])
            self.assertEqual(self.patterns(connection, "xi\u00a0an\u2003", "full"), ["xi", "an"])
            with self.assertRaises(psycopg.errors.InvalidParameterValue):
                self.patterns(connection, "xi", "unknown")

    def test_live_inventory_insert_update_delete_and_rollback(self):
        with self.connection() as connection:
            self.assertEqual(self.patterns(connection, "zz"), [])
            self.assertEqual(self.patterns(connection, "zz", "compat"), ["z.*", "z.*"])
            connection.execute("INSERT INTO pinyin.pinyin_token VALUES ('zz',1)")
            self.assertEqual(self.patterns(connection, "zz"), ["zz"])
            self.assertEqual(self.patterns(connection, "zz", "compat"), ["zz.*"])
            self.assertEqual(self.patterns(connection, "zz", "auto"), ["zz"])
            self.assertEqual(self.patterns(connection, "zzan"), ["zz", "an"])
            connection.execute("INSERT INTO pinyin.pinyin_token VALUES ('zzx',1)")
            self.assertEqual(self.patterns(connection, "zz", "auto"), ["zz.*"])
            connection.execute("DELETE FROM pinyin.pinyin_token WHERE character='zzx'")
            self.assertEqual(self.patterns(connection, "zz", "auto"), ["zz"])
            connection.execute("UPDATE pinyin.pinyin_token SET character='zzx' WHERE character='zz'")
            self.assertEqual(self.patterns(connection, "zz"), [])
            self.assertEqual(self.patterns(connection, "zz", "autocomplete"), ["zz.*"])
            self.assertEqual(self.patterns(connection, "zz", "auto"), ["zz.*"])
            self.assertEqual(self.patterns(connection, "zzxan"), ["zzx", "an"])
            self.assertEqual(self.patterns(connection, "zzx", "compat"), ["zzx.*"])
            connection.execute("DELETE FROM pinyin.pinyin_token WHERE character='zzx'")
            self.assertEqual(self.patterns(connection, "zz", "autocomplete"), [])
            self.assertEqual(self.patterns(connection, "zzx", "compat"), ["z.*", "z.*", "x.*"])
            connection.execute("INSERT INTO pinyin.pinyin_token VALUES ('zz',1)")
            self.assertEqual(self.patterns(connection, "zz"), ["zz"])
            connection.rollback()
            self.assertEqual(self.patterns(connection, "zz"), [])
            self.assertEqual(self.patterns(connection, "zz", "compat"), ["z.*", "z.*"])

    def test_live_inventory_longest_first_categories_and_malformed_rows(self):
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO pinyin.pinyin_token VALUES ('zz',1),('zza',1),('zzaa',1),"
                "('qq',3),('qqz',2)"
            )
            for mode in ("compat", "full", "autocomplete", "auto"):
                with self.subTest(longest_mode=mode):
                    expected = ["zzaa", "an"]
                    if mode == "compat":
                        expected = [token + ".*" for token in expected]
                    elif mode in ("autocomplete", "auto"):
                        expected[-1] += ".*"
                    self.assertEqual(self.patterns(connection, "zzaaan", mode), expected)
            self.assertEqual(self.patterns(connection, "qq"), ["qq"])
            self.assertEqual(self.patterns(connection, "qq", "compat"), ["q.*", "q.*"])
            self.assertEqual(self.patterns(connection, "qqz"), [])
            self.assertEqual(self.patterns(connection, "qqz", "autocomplete"), ["qq", "z.*"])
            connection.execute("UPDATE pinyin.pinyin_token SET category=2 WHERE character='qq'")
            self.assertEqual(self.patterns(connection, "qq"), [])
            connection.execute("UPDATE pinyin.pinyin_token SET category=1 WHERE character='qq'")
            self.assertEqual(self.patterns(connection, "qq"), ["qq"])
            self.assertEqual(self.patterns(connection, "qq", "compat"), ["qq.*"])
            connection.execute(
                "INSERT INTO pinyin.pinyin_token VALUES ('',1),('UPPER',1),('bad!',1),('x.y',1)"
            )
            # Modes filter malformed inventory rows; do not change the raw legacy regex contract.
            for mode in ("full", "autocomplete", "auto"):
                with self.subTest(filtered_mode=mode):
                    self.assertEqual(
                        self.patterns(connection, "upper", mode),
                        [] if mode != "auto" else ["u.*", "p.*", "p.*", "er.*"],
                    )
                    self.assertEqual(self.patterns(connection, "bad!", mode), [])
            self.assertEqual(self.patterns(connection, "xi an", "full"), ["xi", "an"])
            connection.rollback()

    def test_truncate_is_visible_and_rollback_restores_inventory_in_same_session(self):
        with self.connection() as connection:
            self.assertEqual(self.patterns(connection, "xi an"), ["xi", "an"])
            connection.execute("TRUNCATE pinyin.pinyin_token")
            self.assertEqual(connection.execute(
                "SELECT array(SELECT public.pinyin_tokenize('whh',true))"
            ).fetchone()[0], [])
            self.assertEqual(self.patterns(connection, "whh", "compat"), [])
            self.assertEqual(self.patterns(connection, "xi an"), [])
            self.assertEqual(self.patterns(connection, "m n"), ["m", "n"])
            self.assertEqual(self.patterns(connection, "whh", "auto"), ["w.*", "h.*", "h.*"])
            connection.rollback()
            self.assertEqual(self.patterns(connection, "xi an"), ["xi", "an"])
            self.assertEqual(self.patterns(connection, "whh", "compat"), ["w.*", "h.*", "h.*"])

    def test_compat_raw_empty_regex_row_preserves_shared_tokenizer_results(self):
        with self.connection() as connection:
            connection.execute("INSERT INTO pinyin.pinyin_token VALUES ('',1)")
            for value in ("whh", "xi an", " "):
                for generated in (False, True, None):
                    with self.subTest(value=value, generated=generated):
                        actual, reference = connection.execute(
                            "SELECT public.sql_pinyin_regex_phrase_patterns(%s,%s), "
                            "ARRAY(SELECT CASE WHEN %s::boolean THEN '.*\\|' || token || '.*' "
                            "ELSE token || '.*' END "
                            "FROM public.pinyin_tokenize(%s,true) AS token)",
                            (value, generated, generated, value),
                        ).fetchone()
                        self.assertEqual(actual, reference)

    def test_compat_custom_capture_selects_the_outer_token_group(self):
        with self.connection() as connection:
            connection.execute("INSERT INTO pinyin.pinyin_token VALUES ('(qq|rr)',1)")
            actual, reference = connection.execute(
                "SELECT public.sql_pinyin_regex_phrase_patterns('qqrr'), "
                "ARRAY(SELECT token || '.*' FROM public.pinyin_tokenize('qqrr',true) AS token)"
            ).fetchone()
            self.assertEqual(actual, ["qq.*", "rr.*"])
            self.assertEqual(actual, reference)
            self.assertEqual(self.patterns(connection, "qqrr", "compat", True), [r".*\|qq.*", r".*\|rr.*"])
            self.assertEqual(self.patterns(connection, "qqrr", "full"), [])
            self.assertEqual(self.patterns(connection, "qqrr", "auto"), ["q.*", "q.*", "r.*", "r.*"])

    def test_compat_quoted_regex_row_does_not_break_sql_quoting(self):
        with self.connection() as connection:
            connection.execute("INSERT INTO pinyin.pinyin_token VALUES (%s,1)", ("qq'rr",))
            # Static regexp arguments fix the legacy dynamic SQL quoting failure.
            # Do not use the unchanged shared tokenizer as a reference for quoted rows.
            self.assertEqual(self.patterns(connection, "whh", "compat"), ["w.*", "h.*", "h.*"])
            self.assertEqual(self.patterns(connection, "qq'rr", "compat"), ["q.*", "q.*", "r.*", "r.*"])
            self.assertEqual(self.patterns(connection, "qqrr", "full"), [])
            self.assertEqual(self.patterns(connection, "qqrr", "auto"), ["q.*", "q.*", "r.*", "r.*"])

    def test_read_only_parallel_scans_launch_workers_without_assigning_xid(self):
        cases = (
            ("default", "public.sql_pinyin_regex_phrase_patterns(value)", 1500),
            ("compat", "public.sql_pinyin_regex_phrase_patterns_mode(value,'compat')", 1500),
            ("full", "public.sql_pinyin_regex_phrase_patterns_mode(value,'full')", 1000),
            ("initials", "public.sql_pinyin_regex_phrase_patterns_mode(value,'initials')", 500),
            ("autocomplete", "public.sql_pinyin_regex_phrase_patterns_mode(value,'autocomplete')", 1000),
            ("auto", "public.sql_pinyin_regex_phrase_patterns_mode(value,'auto')", 1500),
        )
        for label, expression, expected in cases:
            with self.subTest(mode=label), self.connection() as connection:
                connection.execute("SET TRANSACTION READ ONLY")
                connection.execute("SET LOCAL max_parallel_workers_per_gather = 2")
                connection.execute("SET LOCAL min_parallel_table_scan_size = 0")
                connection.execute("SET LOCAL parallel_setup_cost = 0")
                connection.execute("SET LOCAL parallel_tuple_cost = 0")
                connection.execute("SET LOCAL parallel_leader_participation = off")
                connection.execute("SET LOCAL jit = off")
                self.assertTrue(connection.execute(
                    "SELECT pg_current_xact_id_if_assigned() IS NULL"
                ).fetchone()[0])
                statement = (
                    "SELECT count(*) FROM sql_parser_parallel_probe "
                    f"WHERE cardinality({expression}) > 0"
                )
                plan = connection.execute(
                    "EXPLAIN (ANALYZE,VERBOSE,FORMAT JSON,TIMING OFF) " + statement
                ).fetchone()[0][0]["Plan"]
                nodes = list(plan_nodes(plan))
                self.assertTrue(any(node["Node Type"] in {"Gather", "Gather Merge"} for node in nodes))
                self.assertGreater(sum(node.get("Workers Launched", 0) for node in nodes), 0)
                function_name = expression.split("(", 1)[0].split(".")[-1]
                self.assertTrue(any(
                    node.get("Parallel Aware") and function_name in node.get("Filter", "")
                    for node in nodes
                ), "parser must execute in the parallel scan below Gather")
                self.assertEqual(connection.execute(statement).fetchone()[0], expected)
                self.assertTrue(connection.execute(
                    "SELECT pg_current_xact_id_if_assigned() IS NULL"
                ).fetchone()[0])


if __name__ == "__main__":
    unittest.main()
