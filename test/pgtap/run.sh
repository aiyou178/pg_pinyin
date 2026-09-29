#!/usr/bin/env bash
set -euo pipefail

: "${PGURL:=postgres://localhost/postgres}"

pg_prove --dbname "$PGURL" "$(dirname "$0")"/*.sql
PGURL="$PGURL" python3 "$(dirname "$0")/../parallel_read_only.py"
