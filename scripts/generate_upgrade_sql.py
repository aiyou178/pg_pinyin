#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from pathlib import Path


CREATE_FUNCTION_RE = re.compile(r"^CREATE\s+FUNCTION\b")
CONNECTED_OBJECT_RE = re.compile(
    r"/\* <begin connected objects> \*/\n(.*?)/\* </end connected objects> \*/",
    re.DOTALL,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a PostgreSQL extension upgrade script from a fresh install SQL file."
    )
    parser.add_argument("--install-sql", required=True, help="Path to pg_pinyin--<version>.sql")
    parser.add_argument("--from-version", required=True, help="Source extension version")
    parser.add_argument("--to-version", required=True, help="Target extension version")
    parser.add_argument("--output", required=True, help="Output upgrade SQL path")
    return parser.parse_args()


def upgrade_transform(text: str) -> str:
    blocks: list[str] = []

    for match in CONNECTED_OBJECT_RE.finditer(text):
        block_lines: list[str] = []
        for raw_line in match.group(1).splitlines():
            raw_line = raw_line.rstrip()
            stripped = raw_line.strip()
            if not stripped:
                block_lines.append("")
                continue
            if raw_line.lstrip().startswith("--"):
                continue
            if CREATE_FUNCTION_RE.match(raw_line.lstrip()):
                indent = raw_line[: len(raw_line) - len(raw_line.lstrip())]
                transformed = CREATE_FUNCTION_RE.sub(
                    "CREATE OR REPLACE FUNCTION", raw_line.lstrip(), count=1
                )
                block_lines.append(f"{indent}{transformed}")
            else:
                block_lines.append(raw_line)

        normalized_block = "\n".join(block_lines).strip()
        if normalized_block:
            blocks.append(normalized_block)

    blocks.sort(key=lambda block: "\n".join(line.strip() for line in block.splitlines()))
    return "\n\n".join(blocks) + "\n"


def main() -> None:
    args = parse_args()
    install_sql = Path(args.install_sql).expanduser().resolve()
    output = Path(args.output).expanduser().resolve()

    text = install_sql.read_text(encoding="utf-8")
    header = (
        f"-- Generated from {install_sql.name}\n"
        f"-- Upgrade path: {args.from_version} -> {args.to_version}\n\n"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(header + upgrade_transform(text), encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
