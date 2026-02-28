#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

DEFAULT_SOURCE="$ROOT_DIR/third_party/pinyin-data"
FALLBACK_SOURCE="/Users/liangdeo/projects/pinyin-data"

if [[ -n "${PINYIN_DATA_DIR:-}" ]]; then
  SOURCE_DIR="$PINYIN_DATA_DIR"
elif [[ -d "$DEFAULT_SOURCE" ]]; then
  SOURCE_DIR="$DEFAULT_SOURCE"
elif [[ -d "$FALLBACK_SOURCE" ]]; then
  SOURCE_DIR="$FALLBACK_SOURCE"
else
  echo "No pinyin-data source found." >&2
  echo "Run: git submodule update --init --recursive third_party/pinyin-data" >&2
  echo "Or set PINYIN_DATA_DIR to an existing pinyin-data checkout." >&2
  exit 2
fi

echo "[generate] source: $SOURCE_DIR"

WORDS_SOURCE=""
if [[ -f "$SOURCE_DIR/hanzi_pinyin_words.csv" ]]; then
  WORDS_SOURCE="$SOURCE_DIR/hanzi_pinyin_words.csv"
elif [[ -f "$FALLBACK_SOURCE/hanzi_pinyin_words.csv" ]]; then
  WORDS_SOURCE="$FALLBACK_SOURCE/hanzi_pinyin_words.csv"
fi

ARGS=(--source-dir "$SOURCE_DIR")
if [[ -n "$WORDS_SOURCE" ]]; then
  echo "[generate] words source: $WORDS_SOURCE"
  ARGS+=(--words-source "$WORDS_SOURCE")
else
  echo "[generate] words source not found, creating empty words CSV"
fi

python3 "$ROOT_DIR/scripts/data/generate_extension_data.py" "${ARGS[@]}" "$@"
