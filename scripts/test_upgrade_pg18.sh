#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONTAINER="${1:-${CONTAINER:-}}"
CURRENT_FEATURES="${2:-${EXTENSION_FEATURES:-pg18}}"
OLD_RELEASE_REF="${OLD_RELEASE_REF:-285ce1f}"
TARGET_VERSION="${TARGET_VERSION:-0.1.0}"
TRACKED_UPGRADE_SQL="${ROOT_DIR}/pg_pinyin--0.0.2--${TARGET_VERSION}.sql"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

if [[ -z "$CONTAINER" ]]; then
  echo "usage: $0 <container> [current-features]" >&2
  exit 2
fi

if [[ ! -f "$TRACKED_UPGRADE_SQL" ]]; then
  echo "missing tracked upgrade SQL: $TRACKED_UPGRADE_SQL" >&2
  exit 2
fi

git -C "$ROOT_DIR" archive "$OLD_RELEASE_REF" | tar -x -C "$TMP_DIR"
docker exec "$CONTAINER" bash -lc 'rm -rf /tmp/pg_pinyin-upgrade-old && mkdir -p /tmp/pg_pinyin-upgrade-old'
docker cp "$TMP_DIR/." "$CONTAINER:/tmp/pg_pinyin-upgrade-old/"

docker exec "$CONTAINER" bash -lc '
  set -euo pipefail
  psql -U postgres -d postgres -c "DROP EXTENSION IF EXISTS pg_pinyin CASCADE;"
'

docker exec "$CONTAINER" bash -lc '
  set -euo pipefail
  cd /tmp/pg_pinyin-upgrade-old
  cargo pgrx init --pg18 /usr/lib/postgresql/18/bin/pg_config --no-run
  cargo pgrx install --features pg18 --pg-config /usr/lib/postgresql/18/bin/pg_config
  psql -U postgres -d postgres -c "CREATE EXTENSION pg_pinyin VERSION '\''0.0.2'\'';"
  psql -At -U postgres -d postgres -c "SELECT public.pinyin_word_romanize('\''郑爽ABC'\'')" | grep -qx "zheng shuang abc"
'

docker exec "$CONTAINER" bash -lc "
  set -euo pipefail
  cd /work
  cargo pgrx install --features '$CURRENT_FEATURES' --pg-config /usr/lib/postgresql/18/bin/pg_config
  bash ./scripts/verify_upgrade_sql.sh \
    /usr/share/postgresql/18/extension/pg_pinyin--${TARGET_VERSION}.sql \
    ./pg_pinyin--0.0.2--${TARGET_VERSION}.sql \
    0.0.2 \
    ${TARGET_VERSION}
  cp ./pg_pinyin--0.0.2--${TARGET_VERSION}.sql /usr/share/postgresql/18/extension/pg_pinyin--0.0.2--${TARGET_VERSION}.sql
  psql -U postgres -d postgres -c \"ALTER EXTENSION pg_pinyin UPDATE TO '${TARGET_VERSION}';\"
  psql -At -U postgres -d postgres -c \"SELECT public.pinyin_word_romanize('郑爽ABC')\" | grep -qx 'zheng shuang abc'
  psql -At -U postgres -d postgres -c \"SELECT to_regprocedure('public.pinyin_model_romanize(text,text)') IS NOT NULL\" | grep -qx 't'
  psql -At -U postgres -d postgres -c \"SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema = 'pinyin' AND table_name = 'pinyin_model_registry')\" | grep -qx 't'
"
