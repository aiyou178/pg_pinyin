#!/usr/bin/env bash
set -euo pipefail

image="${1:-pg-pinyin-postgres:19.0-trixie}"
revision=8672300fdf4f4ce8a8eac7511031fd08b6d2c886
context="$(mktemp -d)"
trap 'rm -rf "$context"' EXIT

curl --retry 3 -fsSL "https://codeload.github.com/docker-library/postgres/tar.gz/$revision" \
  | tar -xzf - -C "$context" --strip-components=3 "postgres-$revision/19/trixie"

# Retain CA certificates for PGDG; the upstream recipe uses HTTP.
sed -e 's|http://apt.postgresql.org/|https://apt.postgresql.org/|g' \
    -e 's|gnupg \\|ca-certificates gnupg \\|' "$context/Dockerfile" \
  | docker build -t "$image" -f - "$context"
