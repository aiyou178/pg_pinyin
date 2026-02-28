#!/usr/bin/env bash
set -euo pipefail

: "${PGURL:=postgres://localhost/postgres}"

pg_prove --dbname "$PGURL" "$(dirname "$0")/01_pinyin.sql"
