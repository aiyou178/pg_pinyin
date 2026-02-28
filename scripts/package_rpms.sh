#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${OUT_DIR:-$ROOT_DIR/dist}"
EXT_VERSION="${EXT_VERSION:-$(awk -F'\"' '/^version = / { print $2; exit }' "$ROOT_DIR/Cargo.toml")}"
DPKG_ARCH="$(dpkg --print-architecture)"
RPM_ARCH="x86_64"

if [[ "$DPKG_ARCH" == "arm64" ]]; then
  RPM_ARCH="aarch64"
fi

if ! command -v fpm >/dev/null 2>&1; then
  echo "fpm is required to build RPM packages" >&2
  exit 1
fi

if ! command -v dpkg-deb >/dev/null 2>&1; then
  echo "dpkg-deb is required to unpack DEB packages before RPM conversion" >&2
  exit 1
fi

shopt -s nullglob
DEBS=("$OUT_DIR"/postgresql-*-pg-pinyin_"${EXT_VERSION}"_trixie_*.deb)
if [[ ${#DEBS[@]} -eq 0 ]]; then
  echo "no DEB artifacts found in $OUT_DIR for version $EXT_VERSION" >&2
  exit 1
fi

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

for deb in "${DEBS[@]}"; do
  base="$(basename "$deb")"
  pg="$(echo "$base" | sed -E 's/^postgresql-([0-9]+)-pg-pinyin_.*/\1/')"
  if [[ -z "$pg" || "$pg" == "$base" ]]; then
    echo "failed to parse PostgreSQL major version from $base" >&2
    exit 1
  fi

  root="$tmp_dir/root-$pg"
  rm -rf "$root"
  mkdir -p "$root"
  dpkg-deb -x "$deb" "$root"

  fpm -s dir -t rpm \
    --name "postgresql-${pg}-pg-pinyin" \
    --version "$EXT_VERSION" \
    --iteration "1" \
    --architecture "$RPM_ARCH" \
    --maintainer "Liang Zhanzhao <liangzhanzhao1985@gmail.com>" \
    --description "pinyin normalization extension for PostgreSQL" \
    --chdir "$root" \
    --package "$OUT_DIR" \
    usr

done

echo "[rpm] wrote packages to $OUT_DIR"
