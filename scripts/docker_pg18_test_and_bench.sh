#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${IMAGE:-pg_pinyin/test:trixie}"
CONTAINER="${CONTAINER:-pg-pinyin-test-trixie}"
PGURL_IN_CONTAINER="${PGURL_IN_CONTAINER:-postgres://postgres@localhost/postgres}"
RUN_RUST_TESTS="${RUN_RUST_TESTS:-1}"
RUN_UPGRADE_TESTS="${RUN_UPGRADE_TESTS:-1}"
RUN_BENCHMARK="${RUN_BENCHMARK:-1}"
BENCH_DATASET="${BENCH_DATASET:-synthetic}"
ROWS="${ROWS:-2000}"
KEEP_CONTAINER="${KEEP_CONTAINER:-0}"
EXTENSION_FEATURES="${EXTENSION_FEATURES:-pg18}"
VOLUME_PREFIX="${VOLUME_PREFIX:-$CONTAINER}"
PGDATA_VOLUME="${PGDATA_VOLUME:-${VOLUME_PREFIX}-pgdata}"
CARGO_TARGET_VOLUME="${CARGO_TARGET_VOLUME:-${VOLUME_PREFIX}-cargo-target}"
PGRX_HOME_VOLUME="${PGRX_HOME_VOLUME:-${VOLUME_PREFIX}-pgrx-home}"
HOST_G2PW_MODEL_DIR="${HOST_G2PW_MODEL_DIR:-/tmp/G2PWModel}"
HOST_G2PW_SUPPORT_DIR="${HOST_G2PW_SUPPORT_DIR:-/tmp/g2pw_support}"
PG_PINYIN_G2PW_WINDOW_SIZE="${PG_PINYIN_G2PW_WINDOW_SIZE:-32}"
PG_PINYIN_G2PW_INTRA_OP_NUM_THREADS="${PG_PINYIN_G2PW_INTRA_OP_NUM_THREADS:-2}"
PG_PINYIN_MODEL_MIN_CONFIDENCE="${PG_PINYIN_MODEL_MIN_CONFIDENCE:-0}"
PG_PINYIN_MODEL_MIN_MARGIN="${PG_PINYIN_MODEL_MIN_MARGIN:-0}"

cleanup() {
  if [[ "$KEEP_CONTAINER" != "1" ]]; then
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    docker volume rm -f "$PGDATA_VOLUME" >/dev/null 2>&1 || true
    docker volume rm -f "$CARGO_TARGET_VOLUME" >/dev/null 2>&1 || true
    docker volume rm -f "$PGRX_HOME_VOLUME" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
docker volume rm -f "$PGDATA_VOLUME" >/dev/null 2>&1 || true
docker volume rm -f "$CARGO_TARGET_VOLUME" >/dev/null 2>&1 || true
docker volume rm -f "$PGRX_HOME_VOLUME" >/dev/null 2>&1 || true
docker volume create "$PGDATA_VOLUME" >/dev/null
docker volume create "$CARGO_TARGET_VOLUME" >/dev/null
docker volume create "$PGRX_HOME_VOLUME" >/dev/null

DOCKER_BUILDKIT=1 docker build -f "$ROOT_DIR/docker/Dockerfile.test-trixie" -t "$IMAGE" "$ROOT_DIR"

EXTRA_RUN_ARGS=()
if [[ -d "$HOST_G2PW_MODEL_DIR" ]]; then
  EXTRA_RUN_ARGS+=(-v "$HOST_G2PW_MODEL_DIR:/tmp/G2PWModel")
fi
if [[ -d "$HOST_G2PW_SUPPORT_DIR" ]]; then
  EXTRA_RUN_ARGS+=(-v "$HOST_G2PW_SUPPORT_DIR:/tmp/g2pw_support")
fi

docker run -d \
  --name "$CONTAINER" \
  -e POSTGRES_HOST_AUTH_METHOD=trust \
  -e PGDATA=/var/lib/postgresql/18/docker \
  -e CARGO_HOME=/cargo-home \
  -e RUSTUP_HOME=/rustup \
  -e PGRX_HOME=/pgrx \
  -e CARGO_TARGET_DIR=/cargo-target \
  -v "$ROOT_DIR:/work" \
  -v "$PGDATA_VOLUME:/var/lib/postgresql/18/docker" \
  -v "$CARGO_TARGET_VOLUME:/cargo-target" \
  -v "$PGRX_HOME_VOLUME:/pgrx" \
  "${EXTRA_RUN_ARGS[@]}" \
  "$IMAGE" >/dev/null

docker exec "$CONTAINER" bash -lc '
  set -euo pipefail
  until pg_isready -U postgres -d postgres >/dev/null 2>&1; do
    sleep 1
  done
'

docker exec "$CONTAINER" bash -lc "
  set -euo pipefail
  psql -U postgres -d postgres <<'SQL'
ALTER SYSTEM SET pg_pinyin.g2pw_window_size = '$PG_PINYIN_G2PW_WINDOW_SIZE';
ALTER SYSTEM SET pg_pinyin.g2pw_intra_op_num_threads = '$PG_PINYIN_G2PW_INTRA_OP_NUM_THREADS';
ALTER SYSTEM SET pg_pinyin.model_min_confidence = '$PG_PINYIN_MODEL_MIN_CONFIDENCE';
ALTER SYSTEM SET pg_pinyin.model_min_margin = '$PG_PINYIN_MODEL_MIN_MARGIN';
SELECT pg_reload_conf();
SQL
"

docker exec "$CONTAINER" bash -lc '
  set -euo pipefail
  cd /work
  cargo pgrx init --pg18 /usr/lib/postgresql/18/bin/pg_config --no-run
  chmod -R a+rX "$PGRX_HOME"
  cargo pgrx install --features "'"$EXTENSION_FEATURES"'" --pg-config /usr/lib/postgresql/18/bin/pg_config
  bash ./scripts/verify_upgrade_sql.sh \
    /usr/share/postgresql/18/extension/pg_pinyin--0.1.0.sql \
    ./pg_pinyin--0.0.2--0.1.0.sql \
    0.0.2 \
    0.1.0
  cp ./pg_pinyin--0.0.2--0.1.0.sql /usr/share/postgresql/18/extension/pg_pinyin--0.0.2--0.1.0.sql
  VENV_DIR=/tmp/.venv-g2pm-bundled ./scripts/export_g2pm_assets.sh --force --output-dir /tmp/g2pm-bundled
  mkdir -p /usr/share/postgresql/18/extension/pg_pinyin/g2pm
  cp -R /tmp/g2pm-bundled/. /usr/share/postgresql/18/extension/pg_pinyin/g2pm/
'

docker exec "$CONTAINER" bash -lc "
  set -euo pipefail
  cd /work
  PGURL='$PGURL_IN_CONTAINER' ./test/pgtap/run.sh
"

if [[ "$RUN_UPGRADE_TESTS" == "1" ]]; then
  bash "$ROOT_DIR/scripts/test_upgrade_pg18.sh" "$CONTAINER" "$EXTENSION_FEATURES"
fi

if [[ "$RUN_RUST_TESTS" == "1" ]]; then
  docker exec "$CONTAINER" bash -lc '
    set -euo pipefail
    rm -rf /cargo-target/*
    chown -R postgres:postgres /cargo-home /rustup /pgrx /cargo-target
    chmod 0777 /cargo-target
    chmod 0777 /usr/share/postgresql/18/extension /usr/lib/postgresql/18/lib
    rm -f /usr/share/postgresql/18/extension/pg_pinyin.control
    rm -f /usr/share/postgresql/18/extension/pg_pinyin--0.0.2.sql
    rm -f /usr/share/postgresql/18/extension/pg_pinyin--0.1.0.sql
    rm -f /usr/share/postgresql/18/extension/pg_pinyin--0.0.2--0.1.0.sql
    rm -f /usr/lib/postgresql/18/lib/pg_pinyin.so
    su postgres -s /bin/bash -lc "
      set -euo pipefail
      export PATH=/cargo-home/bin:\$PATH
      export CARGO_HOME=/cargo-home
      export RUSTUP_HOME=/rustup
      export PGRX_HOME=/pgrx
      export CARGO_TARGET_DIR=/cargo-target
      cd /work
      cargo pgrx test pg18 --features "'"$EXTENSION_FEATURES"'"
    "
  '
fi

if [[ "$RUN_BENCHMARK" == "1" ]]; then
  docker exec \
    -e PGURL="$PGURL_IN_CONTAINER" \
    -e ROWS="$ROWS" \
    -e CPP_ACCURACY_ROWS="${CPP_ACCURACY_ROWS:-$ROWS}" \
    -e BENCH_DATASET="$BENCH_DATASET" \
    -e PG_PINYIN_G2PW_WINDOW_SIZE="$PG_PINYIN_G2PW_WINDOW_SIZE" \
    -e PG_PINYIN_G2PW_INTRA_OP_NUM_THREADS="$PG_PINYIN_G2PW_INTRA_OP_NUM_THREADS" \
    -e PG_PINYIN_MODEL_MIN_CONFIDENCE="$PG_PINYIN_MODEL_MIN_CONFIDENCE" \
    -e PG_PINYIN_MODEL_MIN_MARGIN="$PG_PINYIN_MODEL_MIN_MARGIN" \
    -e G2PW_MODEL_PATH="${G2PW_MODEL_PATH:-${HYBRID_MODEL_PATH:-}}" \
    -e G2PW_TOKENIZER_PATH="${G2PW_TOKENIZER_PATH:-${HYBRID_TOKENIZER_PATH:-}}" \
    -e G2PW_LABELS_PATH="${G2PW_LABELS_PATH:-${HYBRID_LABELS_PATH:-}}" \
    -e G2PW_MODEL_CONFIG="${G2PW_MODEL_CONFIG:-${HYBRID_MODEL_CONFIG:-}}" \
    -e G2PM_MODEL_PATH="${G2PM_MODEL_PATH:-/usr/share/postgresql/18/extension/pg_pinyin/g2pm/manifest.json}" \
    "$CONTAINER" \
    bash -lc '
      set -euo pipefail
      cd /work
      bash ./scripts/benchmark_pg18.sh /work/benchmark_pg18_report_${BENCH_DATASET}.txt
    '
fi
