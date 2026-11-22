#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${OUT_DIR:-$ROOT_DIR/dist}"
EXT_VERSION="${EXT_VERSION:-$(awk -F'\"' '/^version = / { print $2; exit }' "$ROOT_DIR/Cargo.toml")}"
DPKG_ARCH="$(dpkg --print-architecture)"
case "$DPKG_ARCH" in
  amd64) RPM_ARCH=x86_64 ;;
  arm64) RPM_ARCH=aarch64 ;;
  *) echo "unsupported package architecture: $DPKG_ARCH" >&2; exit 2 ;;
esac

if ! command -v fpm >/dev/null 2>&1; then
  echo "fpm is required to build RPM packages" >&2
  exit 1
fi

if ! command -v dpkg-deb >/dev/null 2>&1; then
  echo "dpkg-deb is required to unpack DEB packages before RPM conversion" >&2
  exit 1
fi

shopt -s nullglob
DEBS=("$OUT_DIR"/postgresql-*-pg-pinyin_"${EXT_VERSION}"_trixie_"${DPKG_ARCH}".deb)
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
  for file in NOTICE.md MIT-sources.txt CC-BY-SA-3.0.txt CC-BY-SA-4.0.txt manifest.json sources.lock.json LICENSE.code; do
    test -s "$root/usr/share/doc/postgresql-${pg}-pg-pinyin/$file" || {
      echo "missing dictionary attribution in $base: $file" >&2
      exit 1
    }
  done

  # PGDG RPMs use /usr/pgsql-N, not the Debian runtime layout.
  mkdir -p "$root/usr/pgsql-$pg/share" "$root/usr/pgsql-$pg/lib"
  mv "$root/usr/share/postgresql/$pg/extension" "$root/usr/pgsql-$pg/share/"
  mv "$root/usr/lib/postgresql/$pg/lib/"* "$root/usr/pgsql-$pg/lib/"
  rm -rf "$root/usr/share/postgresql" "$root/usr/lib/postgresql"
  case "$pg" in
    15) minimum=15.20 ;; 16) minimum=16.16 ;; 17) minimum=17.12 ;;
    18) minimum=18.7 ;; 19) minimum=19.0 ;; *) exit 2 ;;
  esac

  fpm -s dir -t rpm \
    --name "postgresql-${pg}-pg-pinyin" \
    --version "$EXT_VERSION" \
    --iteration "1" \
    --architecture "$RPM_ARCH" \
    --depends "postgresql${pg}-server >= $minimum" \
    --depends "glibc >= 2.41" \
    --depends "libgcc" \
    --license 'MIT AND CC-BY-SA-3.0 AND CC-BY-SA-4.0' \
    --maintainer "Liang Zhanzhao <liangzhanzhao1985@gmail.com>" \
    --description "pinyin romanization extension for PostgreSQL" \
    --chdir "$root" \
    --package "$OUT_DIR" \
    usr

done

echo "[rpm] wrote packages to $OUT_DIR"
