#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_FILE="${1:-$ROOT_DIR/benchmark_pg19beta2_report.txt}"

exec "$ROOT_DIR/scripts/benchmark_pg18.sh" "$OUT_FILE"
