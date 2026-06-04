# AGENTS.md

## Repo Workflow

- Prefer running validation inside the Debian trixie Docker environment defined by [docker/Dockerfile.test-trixie](docker/Dockerfile.test-trixie).
- Prefer the wrapper script [scripts/docker_pg18_test_and_bench.sh](scripts/docker_pg18_test_and_bench.sh) for repeatable Docker-backed validation.
- Avoid running `cargo`, `cargo pgrx`, `psql`, or benchmark commands on the host by default. Prefer the Docker wrapper so Rust toolchains, `PGRX_HOME`, PostgreSQL data directories, and cargo caches stay inside Docker-managed volumes instead of polluting the host.
- For model-assisted benchmark runs, prefer passing tuning at the wrapper layer so PostgreSQL receives it through `ALTER SYSTEM` and session `SET`:
  - `PG_PINYIN_G2PW_WINDOW_SIZE=32`
  - `PG_PINYIN_G2PW_INTRA_OP_NUM_THREADS=2`
  - `PG_PINYIN_MODEL_MIN_CONFIDENCE=0`
  - `PG_PINYIN_MODEL_MIN_MARGIN=0`
- When a real g2pW benchmark is needed, prefer:
  - `EXTENSION_FEATURES='pg18 hybrid_onnx'`
  - `G2PW_MODEL_PATH=/tmp/G2PWModel/g2pw.onnx`
  - `G2PW_TOKENIZER_PATH=/tmp/g2pw_support/vocab.txt`
- For a normal validation pass, prefer:
  - `RUN_BENCHMARK=0 ./scripts/docker_pg18_test_and_bench.sh`
- For a full validation + benchmark pass, prefer:
  - `RUN_RUST_TESTS=1 RUN_UPGRADE_TESTS=1 RUN_BENCHMARK=1 BENCH_DATASET=synthetic ROWS=2000 ./scripts/docker_pg18_test_and_bench.sh`
- If you need an interactive shell for investigation, keep the container around and then `docker exec` into it:
  - `KEEP_CONTAINER=1 RUN_BENCHMARK=0 ./scripts/docker_pg18_test_and_bench.sh`
  - `docker exec -it pg-pinyin-test-trixie bash`
- After any code change that can affect runtime behavior or performance, run:
  - pgTAP tests
  - upgrade validation via [scripts/test_upgrade_pg18.sh](scripts/test_upgrade_pg18.sh) when extension SQL, control version, package layout, or release workflows change
  - Rust `cargo pgrx test` tests when the container environment is available
  - benchmark(s) via [scripts/benchmark_pg18.sh](scripts/benchmark_pg18.sh)
  - when touching model-assisted romanization code, model docs, or benchmark docs, also run [scripts/benchmark_g2pw_cpp.sh](scripts/benchmark_g2pw_cpp.sh)
- When validating the `g2pm` backend with real assets, prefer exporting them inside the Docker container instead of on the host:
  - `docker exec -it pg-pinyin-test-trixie bash`
  - `cd /work && VENV_DIR=/tmp/.venv-g2pm-export ./scripts/export_g2pm_assets.sh --output-dir /tmp/g2pm-export`
- After benchmark results change, update the benchmark comparison tables in [readme.md](readme.md) and [README.zh-CN.md](README.zh-CN.md) in the same change.

## Benchmark Expectations

- Default synthetic benchmark dataset is still required for regressions.
- Also try a corpus-driven benchmark using the public polyphone data linked from the Interspeech 2023 paper:
  - paper: [zhang23h_interspeech.pdf](https://www.isca-archive.org/interspeech_2023/zhang23h_interspeech.pdf)
  - repo/data entry: `https://github.com/NewZsh/polyphone`
- The repo vendors the minimum CPP split under `benchmark/testdata/cpp/test.sent` and `benchmark/testdata/cpp/test.lb`; prefer those files by default.
- When using corpus-driven data, record which split/file was used and the `ROWS` value in the README benchmark notes.
- If a change affects model-assisted romanization, benchmark at least dictionary word mode, `pinyin_word_romanize(..., model => 'g2pw')`, and `pinyin_model_romanize(..., 'g2pw')`.
- For model-enabled PostgreSQL benchmarks, include at least one accuracy line from the extension-side path itself, currently `public.pinyin__polyphone_romanize(name, query_pos)` on the CPP dataset.
- Keep PostgreSQL in-process benchmark numbers and standalone official ONNX Runtime benchmark numbers separate in docs. Do not present the standalone `g2pW` Python/ORT benchmark as if it were already the extension's in-database execution path.
- If comparing against the paper's `99.08%` g2pW number, state which evaluation you used:
  - original g2pM/CPP split (`10254` test rows)
  - refined CPP split from CVTE-Poly (`8935` test rows)
  - public `G2PWModel-v2-onnx` release
  - or a paper-specific CPP-trained checkpoint

## Docker Hygiene

- Keep `.dockerignore` current so local build artifacts such as `target/` are not sent into Docker build context.
- Prefer Docker-managed named volumes for cargo caches, `PGRX_HOME`, and PostgreSQL data directories.
- Only bind-mount host paths that are intentionally part of the task, such as the repository working tree or explicit external model asset directories.
- `docker/Dockerfile.test-trixie` is written to work with the Docker engine's bundled frontend, so `docker build` should not need to fetch a remote Dockerfile syntax image before cache mounts can be used.

## Release Tracks

- Normal release tags use `v*.*.*` and publish `postgresql-<pg>-pg-pinyin`.
- Model release tags use `model-v*.*.*` and publish `postgresql-<pg>-pg-pinyin-model`.
- Both package lines install SQL extension name `pg_pinyin`.
- Normal packages are expected to bundle compact `g2pm` assets.
- Model packages are expected to bundle compact `g2pm` assets but not ONNX Runtime or `g2pW`; docs should explain how to install ORT and place official `g2pW` files under `/usr/share/postgresql/<major>/extension/pg_pinyin/g2pw/`.
