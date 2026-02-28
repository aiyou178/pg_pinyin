#!/usr/bin/env python3
"""
Generate extension dictionaries from pinyin-data sources.

Ported and simplified from the pinyin-data prep scripts:
- prepare_tables.py
- prepare_words.py
- prepare_regex.py

Outputs:
- pinyin_mapping.csv  (character -> |pinyin1|pinyin2|...|)
- pinyin_token.csv    (token -> category)
- pinyin_words.csv    (word -> space-separated pinyin tokens)
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import unicodedata
from pathlib import Path

PINYIN_LINE_RE = re.compile(r"^U\+([0-9A-F]+):\s*([^#]+?)\s*(?:#.*)?$")

INITIALS = [
    "zh",
    "ch",
    "sh",
    "b",
    "c",
    "d",
    "f",
    "g",
    "h",
    "j",
    "k",
    "l",
    "m",
    "n",
    "p",
    "q",
    "r",
    "s",
    "t",
    "v",
    "w",
    "x",
    "y",
    "z",
]

VOWEL_TOKENS = {"a", "e", "i", "o", "u"}


def normalize_tone(raw: str, normalizer: dict[str, str] | None) -> str:
    text = raw.strip().lower()
    if normalizer:
        out = "".join(normalizer.get(ch, ch) for ch in text)
    else:
        decomposed = unicodedata.normalize("NFKD", text)
        chunks = []
        for ch in decomposed:
            if unicodedata.combining(ch):
                continue
            if ch == "ü":
                chunks.append("v")
            else:
                chunks.append(ch)
        out = "".join(chunks)

    out = out.replace("u:", "v").replace("ü", "v").replace("’", "'")
    out = re.sub(r"[^a-zv']", "", out)
    return out


def load_char_map(source_dir: Path) -> dict[str, list[str]]:
    ok_path = source_dir / "ok.json"
    pinyin_txt = source_dir / "pinyin.txt"

    normalizer = (
        json.loads(ok_path.read_text(encoding="utf-8")) if ok_path.exists() else None
    )

    char_map: dict[str, list[str]] = {}
    for raw_line in pinyin_txt.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        match = PINYIN_LINE_RE.match(line)
        if not match:
            continue

        codepoint_text, pinyin_part = match.groups()
        try:
            character = chr(int(codepoint_text, 16))
        except ValueError:
            continue

        seen = set()
        normalized = []
        for pron in pinyin_part.split(","):
            token = normalize_tone(pron, normalizer)
            if token and token not in seen:
                seen.add(token)
                normalized.append(token)

        if normalized:
            char_map[character] = normalized

    return char_map


def write_mapping_csv(path: Path, char_map: dict[str, list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        for character, pinyins in sorted(char_map.items(), key=lambda item: ord(item[0])):
            writer.writerow([character, f"|{'|'.join(pinyins)}|"])


def build_token_rows(char_map: dict[str, list[str]]) -> list[tuple[str, int]]:
    syllables = set()
    for values in char_map.values():
        syllables.update(values)

    initial_set = set(INITIALS)

    full_tokens = sorted(
        [token for token in syllables if token not in initial_set and token not in VOWEL_TOKENS],
        key=lambda token: (-len(token), token),
    )
    vowel_tokens = sorted([token for token in syllables if token in VOWEL_TOKENS])

    rows: list[tuple[str, int]] = []
    rows.extend((token, 1) for token in full_tokens)
    rows.extend((token, 3) for token in vowel_tokens)
    rows.extend((token, 2) for token in INITIALS)
    return rows


def write_token_csv(path: Path, rows: list[tuple[str, int]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerows(rows)


def split_joined_pinyin(joined: str, syllables: set[str], lengths_desc: list[int]) -> tuple[str, bool]:
    text = joined.strip().lower().replace("u:", "v").replace("ü", "v").replace("’", "'")
    if not text:
        return "", False

    tokens = []
    i = 0
    had_fallback = False

    while i < len(text):
        ch = text[i]
        if ch == "'" or ch.isspace():
            i += 1
            continue

        matched = None
        for width in lengths_desc:
            end = i + width
            if end > len(text):
                continue
            candidate = text[i:end]
            if candidate in syllables:
                matched = candidate
                break

        if matched is None:
            had_fallback = True
            j = i + 1
            while j < len(text) and text[j].isalpha() and text[j] != "'":
                j += 1
            token = text[i:j] if j > i else text[i]
            tokens.append(token)
            i = j if j > i else i + 1
            continue

        tokens.append(matched)
        i += len(matched)

    return " ".join(tokens), had_fallback


def write_words_csv(
    path: Path,
    source_dir: Path,
    syllables: set[str],
    words_source: Path | None,
) -> tuple[int, int]:
    words_in = words_source or (source_dir / "hanzi_pinyin_words.csv")
    if not words_in.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        return 0, 0

    path.parent.mkdir(parents=True, exist_ok=True)

    lengths_desc = sorted({len(token) for token in syllables}, reverse=True)
    unique_words: dict[str, str] = {}
    fallback_count = 0

    with words_in.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        for row in reader:
            if len(row) < 2:
                continue

            word = row[0].strip()
            joined = row[1].strip()
            if not word or not joined:
                continue

            if word in unique_words:
                continue

            segmented, used_fallback = split_joined_pinyin(joined, syllables, lengths_desc)
            if not segmented:
                continue

            if used_fallback:
                fallback_count += 1

            unique_words[word] = segmented

    with path.open("w", encoding="utf-8", newline="") as out_fh:
        writer = csv.writer(out_fh, lineterminator="\n")
        for word, segmented in unique_words.items():
            writer.writerow([word, segmented])

    return len(unique_words), fallback_count


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate pg_pinyin extension dictionary CSV files")
    parser.add_argument(
        "--source-dir",
        default="third_party/pinyin-data",
        help="Path to pinyin-data repository (default: third_party/pinyin-data)",
    )
    parser.add_argument(
        "--mapping-out",
        default="sql/data/pinyin_mapping.csv",
        help="Output path for character mapping CSV",
    )
    parser.add_argument(
        "--token-out",
        default="sql/data/pinyin_token.csv",
        help="Output path for token CSV",
    )
    parser.add_argument(
        "--words-out",
        default="sql/data/pinyin_words.csv",
        help="Output path for word mapping CSV",
    )
    parser.add_argument(
        "--words-source",
        default="",
        help="Optional explicit hanzi_pinyin_words.csv path",
    )
    parser.add_argument(
        "--legacy-mapping-out",
        default="sql_patent/pinyin_mapping.csv",
        help="Optional legacy output path for mapping CSV",
    )
    parser.add_argument(
        "--legacy-token-out",
        default="sql_patent/pinyin_token.csv",
        help="Optional legacy output path for token CSV",
    )
    parser.add_argument(
        "--no-legacy-copy",
        action="store_true",
        help="Disable writing compatibility copies into sql_patent/",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    source_dir = Path(args.source_dir)
    if not source_dir.exists():
        raise SystemExit(f"source directory does not exist: {source_dir}")

    char_map = load_char_map(source_dir)
    if not char_map:
        raise SystemExit("no pinyin character mapping loaded from source")

    mapping_out = Path(args.mapping_out)
    token_out = Path(args.token_out)
    words_out = Path(args.words_out)

    write_mapping_csv(mapping_out, char_map)

    token_rows = build_token_rows(char_map)
    write_token_csv(token_out, token_rows)

    syllables = {token for token, cat in token_rows if cat in (1, 3)}
    words_source = Path(args.words_source) if args.words_source else None
    word_count, fallback_count = write_words_csv(words_out, source_dir, syllables, words_source)

    if not args.no_legacy_copy:
        write_mapping_csv(Path(args.legacy_mapping_out), char_map)
        write_token_csv(Path(args.legacy_token_out), token_rows)

    print(f"[ok] characters: {len(char_map)}")
    print(f"[ok] token rows: {len(token_rows)}")
    if words_source:
        print(f"[ok] words source: {words_source}")
    else:
        print(f"[ok] words source: {source_dir / 'hanzi_pinyin_words.csv'}")
    print(f"[ok] words: {word_count} (fallback segmentation: {fallback_count})")
    print(f"[ok] wrote: {mapping_out}, {token_out}, {words_out}")
    if not args.no_legacy_copy:
        print(f"[ok] legacy copies: {args.legacy_mapping_out}, {args.legacy_token_out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
