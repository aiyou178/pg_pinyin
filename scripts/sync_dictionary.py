#!/usr/bin/env python3
"""Explicit, transactional dictionary additions; existing rows always win."""
from __future__ import annotations

import argparse
from pathlib import Path

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]


def sync(connection, data_dir: Path = ROOT / "sql/data") -> dict[str, int]:
    added = {}
    with connection.transaction():
        for name, key, value in (
            ("pinyin_mapping", "character", "pinyin"),
            ("pinyin_words", "word", "pinyin"),
            ("pinyin_token", "character", "category"),
        ):
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL("CREATE TEMP TABLE pinyin_sync_stage (LIKE pinyin.{}) ON COMMIT DROP").format(sql.Identifier(name)))
                with cursor.copy("COPY pinyin_sync_stage FROM STDIN WITH (FORMAT CSV)") as copy:
                    copy.write((data_dir / f"{name}.csv").read_bytes())
                cursor.execute(sql.SQL("INSERT INTO pinyin.{} ({},{}) SELECT {},{} FROM pinyin_sync_stage "
                                       "ON CONFLICT ({}) DO NOTHING").format(
                    sql.Identifier(name), sql.Identifier(key), sql.Identifier(value),
                    sql.Identifier(key), sql.Identifier(value), sql.Identifier(key)))
                added[name] = cursor.rowcount
                cursor.execute("DROP TABLE pinyin_sync_stage")
    return added


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "sql/data")
    args = parser.parse_args()
    with psycopg.connect(args.dsn, autocommit=True) as connection:
        for name, count in sync(connection, args.data_dir).items():
            print(f"{name}: added {count} rows")


if __name__ == "__main__":
    main()
