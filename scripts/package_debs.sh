#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${OUT_DIR:-$ROOT_DIR/dist}"
EXT_VERSION="${EXT_VERSION:-$(awk -F'\"' '/^version = / { print $2; exit }' "$ROOT_DIR/Cargo.toml")}"
PACKAGE_FLAVOR="${PACKAGE_FLAVOR:-base}"
PG_VERSIONS=(14 15 16 17 18)
ARCH="$(dpkg --print-architecture)"
G2PM_TMP_DIR="${G2PM_TMP_DIR:-/tmp/pg_pinyin_g2pm_export}"
G2PM_VENV_DIR="${G2PM_VENV_DIR:-/tmp/.venv-g2pm-release}"
TRACKED_UPGRADE_SQL="$ROOT_DIR/pg_pinyin--0.0.2--${EXT_VERSION}.sql"

mkdir -p "$OUT_DIR"
cd "$ROOT_DIR"

if [[ ! -f "$TRACKED_UPGRADE_SQL" ]]; then
  echo "missing tracked upgrade SQL: $TRACKED_UPGRADE_SQL" >&2
  exit 2
fi

case "$PACKAGE_FLAVOR" in
  base)
    PACKAGE_SUFFIX=""
    FEATURE_SUFFIX=""
    DESCRIPTION="pinyin romanization extension for PostgreSQL with bundled g2pM support"
    ;;
  model)
    PACKAGE_SUFFIX="-model"
    FEATURE_SUFFIX=" hybrid_onnx"
    DESCRIPTION="pinyin romanization extension for PostgreSQL with bundled g2pM and optional g2pW support"
    ;;
  *)
    echo "unsupported PACKAGE_FLAVOR: $PACKAGE_FLAVOR" >&2
    exit 2
    ;;
esac

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

rm -rf "$G2PM_TMP_DIR"
VENV_DIR="$G2PM_VENV_DIR" "$ROOT_DIR/scripts/export_g2pm_assets.sh" \
  --force \
  --output-dir "$G2PM_TMP_DIR"

for pg in "${PG_VERSIONS[@]}"; do
  echo "[package] PostgreSQL $pg ($PACKAGE_FLAVOR)"
  pg_config="/usr/lib/postgresql/$pg/bin/pg_config"
  cargo pgrx install \
    -v \
    --release \
    --features "pg$pg$FEATURE_SUFFIX" \
    --no-default-features \
    --pg-config "$pg_config"

  ext_dir="/usr/share/postgresql/$pg/extension"
  lib_dir="/usr/lib/postgresql/$pg/lib"
  if [[ ! -d "$ext_dir" || ! -d "$lib_dir" ]]; then
    echo "missing extension/lib install dirs for PostgreSQL $pg" >&2
    exit 1
  fi

  install_sql="$ext_dir/pg_pinyin--${EXT_VERSION}.sql"
  if [[ ! -f "$install_sql" ]]; then
    echo "missing fresh install SQL: $install_sql" >&2
    exit 1
  fi
  bash "$ROOT_DIR/scripts/verify_upgrade_sql.sh" \
    "$install_sql" \
    "$TRACKED_UPGRADE_SQL" \
    "0.0.2" \
    "$EXT_VERSION"

  build_dir="$ROOT_DIR/target/pgrx-pkg/pg$pg-$PACKAGE_FLAVOR"
  deb_root="$build_dir/deb"
  doc_root="$deb_root/usr/share/doc/postgresql-${pg}-pg-pinyin${PACKAGE_SUFFIX}"
  asset_root="$deb_root/usr/share/postgresql/$pg/extension/pg_pinyin"
  rm -rf "$deb_root"
  mkdir -p \
    "$deb_root/DEBIAN" \
    "$deb_root/usr/share/postgresql/$pg/extension" \
    "$deb_root/usr/lib/postgresql/$pg/lib" \
    "$asset_root/g2pm" \
    "$doc_root"

  cp "$ROOT_DIR/pg_pinyin.control" "$deb_root/usr/share/postgresql/$pg/extension/pg_pinyin.control"
  cp "$lib_dir/pg_pinyin.so" "$deb_root/usr/lib/postgresql/$pg/lib/"
  cp "$install_sql" "$deb_root/usr/share/postgresql/$pg/extension/"
  cp "$ROOT_DIR"/pg_pinyin--*.sql "$deb_root/usr/share/postgresql/$pg/extension/"
  cp "$TRACKED_UPGRADE_SQL" "$deb_root/usr/share/postgresql/$pg/extension/"
  cp -R "$G2PM_TMP_DIR"/. "$asset_root/g2pm/"

  if [[ "$PACKAGE_FLAVOR" == "model" ]]; then
    mkdir -p "$asset_root/g2pw"
    cat > "$asset_root/g2pw/README.txt" <<'EOF'
Place the official g2pW model files in this directory:
- g2pw.onnx
- POLYPHONIC_CHARS.txt
- vocab.txt (or a directory containing vocab.txt)

Also install ONNX Runtime on the system so libonnxruntime.so is discoverable.
EOF
  fi

  if [[ -f "$ROOT_DIR/THIRD_PARTY_NOTICES.md" ]]; then
    cp "$ROOT_DIR/THIRD_PARTY_NOTICES.md" "$doc_root/"
  fi
  if [[ -f "$ROOT_DIR/third_party/g2pm/LICENSE" ]]; then
    cp "$ROOT_DIR/third_party/g2pm/LICENSE" "$doc_root/LICENSE.g2pm"
  fi
  if [[ -f "$ROOT_DIR/LICENSE" ]]; then
    cp "$ROOT_DIR/LICENSE" "$doc_root/LICENSE.pg_pinyin"
  fi

  package_name="postgresql-${pg}-pg-pinyin${PACKAGE_SUFFIX}"
  if [[ "$PACKAGE_FLAVOR" == "base" ]]; then
    conflicts="postgresql-${pg}-pg-pinyin-model"
  else
    conflicts="postgresql-${pg}-pg-pinyin"
  fi

  cat > "$deb_root/DEBIAN/control" <<CONTROL
Package: ${package_name}
Version: ${EXT_VERSION}
Section: database
Priority: optional
Architecture: ${ARCH}
Maintainer: Liang Zhanzhao <liangzhanzhao1985@gmail.com>
Depends: postgresql-${pg}
Conflicts: ${conflicts}
Description: ${DESCRIPTION}
 Rust+pgrx extension with character and word-level pinyin romanization.
CONTROL

  out_deb="$OUT_DIR/${package_name}_${EXT_VERSION}_trixie_${ARCH}.deb"
  dpkg-deb --build "$deb_root" "$out_deb"
  echo "[package] wrote $out_deb"
done
