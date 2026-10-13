#!/usr/bin/env bash
set -euo pipefail

# Install the actual previous catalog before installing the working tree.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_REF="${1:-v0.0.8}"
: "${PG_CONFIG:?set PG_CONFIG to the PostgreSQL build under test}"
PG_FEATURE="pg$("$PG_CONFIG" --version | sed -E 's/^PostgreSQL ([0-9]+).*/\1/')"
BASE_DIR="$(mktemp -d)"
trap 'rm -rf "$BASE_DIR"' EXIT

git -c safe.directory="$ROOT_DIR" -C "$ROOT_DIR" archive --format=tar --output="$BASE_DIR/source.tar" "$BASE_REF"
tar -xf "$BASE_DIR/source.tar" -C "$BASE_DIR"
BASE_COMMIT="$(git -c safe.directory="$ROOT_DIR" -C "$ROOT_DIR" rev-parse "$BASE_REF")"
# Shared final .so paths can retain a newer build even when Cargo reports the
# archived package as fresh. Keep the baseline artifact namespace independent.
export CARGO_TARGET_DIR="${CARGO_TARGET_DIR:-$ROOT_DIR/target}/upgrade-baseline-${BASE_COMMIT}"
cd "$BASE_DIR"
BUILD_FLAGS=()
if [ "${PG_PINYIN_BUILD_RELEASE:-0}" = "1" ]; then
  BUILD_FLAGS+=(--release)
fi
cargo pgrx install "${BUILD_FLAGS[@]}" --features "$PG_FEATURE" --no-default-features --pg-config "$PG_CONFIG"
test -f "$("$PG_CONFIG" --sharedir)/extension/pg_pinyin--0.0.8.sql"
if grep -q 'pinyin_regex_phrase_patterns_mode' "$("$PG_CONFIG" --sharedir)/extension/pg_pinyin--0.0.8.sql"; then
  printf '%s\n' 'upgrade baseline unexpectedly contains the new query-mode API' >&2
  exit 1
fi
