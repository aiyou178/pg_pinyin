#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_SQL="${1:?usage: verify_upgrade_sql.sh <install-sql> <tracked-upgrade-sql> <from-version> <to-version>}"
TRACKED_UPGRADE_SQL="${2:?usage: verify_upgrade_sql.sh <install-sql> <tracked-upgrade-sql> <from-version> <to-version>}"
FROM_VERSION="${3:?usage: verify_upgrade_sql.sh <install-sql> <tracked-upgrade-sql> <from-version> <to-version>}"
TO_VERSION="${4:?usage: verify_upgrade_sql.sh <install-sql> <tracked-upgrade-sql> <from-version> <to-version>}"

TMP_OUTPUT="$(mktemp)"
trap 'rm -f "$TMP_OUTPUT"' EXIT

python3 "$ROOT_DIR/scripts/generate_upgrade_sql.py" \
  --install-sql "$INSTALL_SQL" \
  --from-version "$FROM_VERSION" \
  --to-version "$TO_VERSION" \
  --output "$TMP_OUTPUT" >/dev/null

if ! diff -u "$TRACKED_UPGRADE_SQL" "$TMP_OUTPUT"; then
  echo "tracked upgrade SQL is stale: $TRACKED_UPGRADE_SQL" >&2
  exit 1
fi
