#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${OUT_DIR:-$ROOT_DIR/dist}"
EXT_VERSION="${EXT_VERSION:-$(awk -F'\"' '/^version = / { print $2; exit }' "$ROOT_DIR/Cargo.toml")}"
PG_VERSIONS=(14 15 16 17 18)
ARCH="$(dpkg --print-architecture)"

mkdir -p "$OUT_DIR"

cd "$ROOT_DIR"

for pg in "${PG_VERSIONS[@]}"; do
  pg_config="/usr/lib/postgresql/$pg/bin/pg_config"
  if [[ ! -x "$pg_config" ]]; then
    echo "missing pg_config for PostgreSQL $pg at $pg_config" >&2
    exit 2
  fi
done

cargo pgrx init \
  --pg14=/usr/lib/postgresql/14/bin/pg_config \
  --pg15=/usr/lib/postgresql/15/bin/pg_config \
  --pg16=/usr/lib/postgresql/16/bin/pg_config \
  --pg17=/usr/lib/postgresql/17/bin/pg_config \
  --pg18=/usr/lib/postgresql/18/bin/pg_config

for pg in "${PG_VERSIONS[@]}"; do
  echo "[package] PostgreSQL $pg"
  pg_config="/usr/lib/postgresql/$pg/bin/pg_config"
  cargo pgrx install \
    -v \
    --release \
    --features "pg$pg" \
    --no-default-features \
    --pg-config "$pg_config"

  ext_dir="/usr/share/postgresql/$pg/extension"
  lib_dir="/usr/lib/postgresql/$pg/lib"
  if [[ ! -d "$ext_dir" || ! -d "$lib_dir" ]]; then
    echo "missing extension/lib install dirs for PostgreSQL $pg" >&2
    exit 1
  fi

  shopt -s nullglob
  sql_files=("$ext_dir"/pg_pinyin--*.sql)
  if [[ ${#sql_files[@]} -eq 0 ]]; then
    echo "no SQL files generated in $ext_dir for PostgreSQL $pg" >&2
    exit 1
  fi

  build_dir="$ROOT_DIR/target/pgrx-pkg/pg$pg"
  deb_root="$build_dir/deb"
  rm -rf "$deb_root"
  mkdir -p \
    "$deb_root/DEBIAN" \
    "$deb_root/usr/share/postgresql/$pg/extension" \
    "$deb_root/usr/lib/postgresql/$pg/lib"

  cp "$ROOT_DIR/pg_pinyin.control" "$deb_root/usr/share/postgresql/$pg/extension/pg_pinyin.control"
  cp "$lib_dir/pg_pinyin.so" "$deb_root/usr/lib/postgresql/$pg/lib/"
  cp "${sql_files[@]}" "$deb_root/usr/share/postgresql/$pg/extension/"

  cat > "$deb_root/DEBIAN/control" <<CONTROL
Package: postgresql-${pg}-pg-pinyin
Version: ${EXT_VERSION}
Section: database
Priority: optional
Architecture: ${ARCH}
Maintainer: Liang Zhanzhao <liangzhanzhao1985@gmail.com>
Depends: postgresql-${pg}
Description: pinyin romanization extension for PostgreSQL
 Rust+pgrx extension with character and word-level pinyin romanization.
CONTROL

  out_deb="$OUT_DIR/postgresql-${pg}-pg-pinyin_${EXT_VERSION}_trixie_${ARCH}.deb"
  dpkg-deb --build "$deb_root" "$out_deb"
  echo "[package] wrote $out_deb"
done
