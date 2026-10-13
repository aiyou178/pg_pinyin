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
BASE_COMMIT="$(git -c safe.directory="$ROOT_DIR" -C "$ROOT_DIR" rev-parse "${BASE_REF}^{commit}")"
BASE_VERSION="$(sed -n "s/^default_version = '\([^']*\)'$/\1/p" "$BASE_DIR/pg_pinyin.control")"
# Shared final .so paths can retain a newer build even when Cargo reports the
# archived package as fresh. Keep the baseline artifact namespace independent.
export CARGO_TARGET_DIR="${CARGO_TARGET_DIR:-$ROOT_DIR/target}/upgrade-baseline-${BASE_COMMIT}"
cd "$BASE_DIR"
BUILD_FLAGS=()
if [ "${PG_PINYIN_BUILD_RELEASE:-0}" = "1" ]; then
  BUILD_FLAGS+=(--release)
fi
cargo pgrx install "${BUILD_FLAGS[@]}" --features "$PG_FEATURE" --no-default-features --pg-config "$PG_CONFIG"
BASE_SQL="$("$PG_CONFIG" --sharedir)/extension/pg_pinyin--${BASE_VERSION}.sql"
test -f "$BASE_SQL"
if [ "$BASE_VERSION" = "0.0.8" ] && grep -q 'pinyin_regex_phrase_patterns_mode' "$BASE_SQL"; then
  printf '%s\n' 'upgrade baseline unexpectedly contains the new query-mode API' >&2
  exit 1
fi
if [ "$BASE_VERSION" = "0.0.9" ]; then
  if grep -q 'pinyin_word_romanize_batch' "$BASE_SQL"; then
    printf '%s\n' 'upgrade baseline unexpectedly contains the document-batch API' >&2
    exit 1
  fi
  # Retain the unreleased pre-optimization P1 binary/catalog independently.
  BASE_LIBRARY="$("$PG_CONFIG" --pkglibdir)/pg_pinyin_baseline_0_0_9.so"
  cp "$("$PG_CONFIG" --pkglibdir)/pg_pinyin.so" "$BASE_LIBRARY"
  BASE_SNAPSHOT_SQL="$("$PG_CONFIG" --pkglibdir)/pg_pinyin_baseline_0_0_9.sql"
  cp "$BASE_SQL" "$BASE_SNAPSHOT_SQL"
  python3 - "$BASE_COMMIT" "$BASE_SNAPSHOT_SQL" "$BASE_LIBRARY" "${PG_PINYIN_BUILD_RELEASE:-0}" "$(rustc --version)" <<'PY'
import hashlib
import json
from pathlib import Path
import sys

commit, sql, library, release, rust_version = sys.argv[1:]
manifest = {
    "source_commit": commit,
    "extension_version": "0.0.9",
    "library_sha256": hashlib.sha256(Path(library).read_bytes()).hexdigest(),
    "installation_sql_sha256": hashlib.sha256(Path(sql).read_bytes()).hexdigest(),
    "build_profile": "release" if release == "1" else "debug",
    "rust_version": rust_version,
}
Path(library).with_suffix(".json").write_text(json.dumps(manifest, indent=2) + "\n")
PY
fi
