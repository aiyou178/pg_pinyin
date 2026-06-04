# pg_pinyin（中文说明）

[English README](readme.md)

`pg_pinyin` 包含两套实现：

1. SQL 基线方案（`sql/pinyin.sql`）
2. Rust 扩展方案（`src/lib.rs`）

## 扩展接口

拼音化能力分成三组：

- `pinyin_char_romanize(text)`
- `pinyin_char_romanize(text, suffix text)`
- `pinyin_word_romanize(text)`
- `pinyin_word_romanize(text, suffix text)`
- `pinyin_word_romanize(tokenizer_input anyelement)`（重载，支持 `pdb` tokenizer 输入，如 `name::pdb.icu::text[]`）
- `pinyin_word_romanize(tokenizer_input anyelement, suffix text)`（带用户词典后缀的重载）
- `pinyin_word_romanize(text, model pinyin.model_identifier)`（调用时写成 `model => 'g2pw'`）
- `pinyin_word_romanize(text, suffix text, model pinyin.model_identifier)`
- `pinyin_word_romanize(tokenizer_input anyelement, model pinyin.model_identifier)`
- `pinyin_word_romanize(tokenizer_input anyelement, suffix text, model pinyin.model_identifier)`
- `pinyin_model_romanize(text, model text)`
- `pinyin_model_romanize(tokenizer_input anyelement, model text)`
- `pinyin_word_romanize_debug(text, suffix text default '', model text default '')`，返回 `jsonb`
- `pinyin_model_romanize_debug(text, model text)`，返回 `jsonb`

推荐组合：

1. 字级拼音化 + `pg_trgm`
2. 词级拼音化 + `pg_search`
3. `pinyin_word_romanize(..., model => 'g2pm')` 适合直接使用内置的小模型能力
4. `pinyin_word_romanize(..., model => 'g2pw')` 适合“词典优先，剩余多音字再走模型”的路径
5. `pinyin_model_romanize(..., 'g2pw')` 适合跳过词级词典、直接看上下文做模型选音

Volatility：

- `pinyin_char_romanize*` 与 `pinyin_word_romanize*` 仍然是 `IMMUTABLE`
- 带 `model` 的 `pinyin_word_romanize`、`pinyin_model_romanize*`、以及 debug API 是 `STABLE` 且 `PARALLEL UNSAFE`

## Release 轨道

`pg_pinyin` 现在有两条发布轨道：

- `v0.1.0` 这类 tag 发布 `postgresql-<pg>-pg-pinyin`
  - 包含 char/word API 和内置紧凑 `g2pm`
  - 不依赖 ONNX Runtime
- `model-v0.1.0` 这类 tag 发布 `postgresql-<pg>-pg-pinyin-model`
  - 同样包含内置紧凑 `g2pm`
  - 额外启用 `g2pw_onnx`
  - 不打包 ONNX Runtime，也不打包 `g2pW` 模型文件

两条轨道安装后的 SQL 扩展名都仍然是 `pg_pinyin`。

## 生成列用法示例（Raw SQL）

```sql
CREATE EXTENSION IF NOT EXISTS pg_pinyin;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE voice (
  id bigserial PRIMARY KEY,
  description text NOT NULL,
  pinyin text GENERATED ALWAYS AS (public.pinyin_char_romanize(description)) STORED
);

CREATE INDEX voice_pinyin_trgm_idx ON voice USING gin (pinyin gin_trgm_ops);

INSERT INTO voice (description) VALUES ('郑爽ABC');
SELECT id, description, pinyin FROM voice;
```

## 用户词典后缀表

你可以在 `pinyin` schema 下提供后缀表：

- `pinyin.pinyin_mapping_suffix1`
- `pinyin.pinyin_words_suffix1`

当调用 `...(..., '_suffix1')` 时，拼音化会使用合并后的词典：

1. 基础表（`pinyin_mapping` / `pinyin_words`）
2. 后缀表（`pinyin_mapping_suffix1` / `pinyin_words_suffix1`），并且后缀表优先级更高

打包后的发布物会自动 seed 一条 bundled 模型记录：

- `model_name = 'bundled_g2pm'`
- `kind = 'g2pm_numpy'`
- `model_path = /usr/share/postgresql/<major>/extension/pg_pinyin/g2pm/manifest.json`

所以安装 normal 包或 model 包之后，`model => 'g2pm'` 默认应当可以直接使用。

手工注册 `g2pW` 的示例：

```sql
CREATE TABLE IF NOT EXISTS pinyin.pinyin_mapping_suffix1 (
  character text PRIMARY KEY,
  pinyin text NOT NULL
);

CREATE TABLE IF NOT EXISTS pinyin.pinyin_words_suffix1 (
  word text PRIMARY KEY,
  pinyin text NOT NULL
);

INSERT INTO pinyin.pinyin_mapping_suffix1 (character, pinyin)
VALUES ('郑', '|zhengx|')
ON CONFLICT (character) DO UPDATE SET pinyin = EXCLUDED.pinyin;

INSERT INTO pinyin.pinyin_words_suffix1 (word, pinyin)
VALUES ('郑爽', '|zhengx| |shuangx|')
ON CONFLICT (word) DO UPDATE SET pinyin = EXCLUDED.pinyin;

SELECT public.pinyin_char_romanize('郑爽ABC', '_suffix1');
SELECT public.pinyin_word_romanize('郑爽ABC'::pdb.icu::text[], '_suffix1');
```

## 多音字模型注册表

只有在显式传入 `model => ...` 时，才会启用模型辅助的多音字消歧；纯词典 API 的行为保持不变。

涉及的表：

- `pinyin.pinyin_model_registry`
- `pinyin.pinyin_model_meta`

示例：

```sql
INSERT INTO pinyin.pinyin_model_registry (
  model_name,
  kind,
  model_path,
  tokenizer_path,
  labels_path,
  config
) VALUES (
  'g2pw_v1',
  'g2pw_onnx',
  '/absolute/path/to/G2PWModel-v2-onnx/g2pW.onnx',
  '/absolute/path/to/tokenizer-assets',
  '/absolute/path/to/labels.txt',
  '{"min_confidence":0.80,"min_margin":0.05,"disable_on_error":true}'::jsonb
);

SELECT public.pinyin_word_romanize('重启', model => 'g2pw');
SELECT public.pinyin_word_romanize('银行行长', model => 'g2pw');
SELECT public.pinyin_model_romanize('银行行长', 'g2pw');
SELECT public.pinyin_word_romanize_debug('银行行长', model => 'g2pw');
SELECT public.pinyin_model_romanize_debug('银行行长', 'g2pw');
```

如果你想接入自定义 `g2pM` 资产，可以先用下面的脚本把 wheel 导出成 Rust 侧可直接加载的紧凑格式：

```bash
./scripts/export_g2pm_assets.sh --output-dir /absolute/path/to/g2pm-export
```

然后注册：

```sql
INSERT INTO pinyin.pinyin_model_registry (
  model_name,
  kind,
  model_path,
  tokenizer_path,
  labels_path,
  config
) VALUES (
  'g2pm_v1',
  'g2pm_numpy',
  '/absolute/path/to/g2pm-export/manifest.json',
  NULL,
  NULL,
  '{"min_confidence":0.80,"min_margin":0.05,"disable_on_error":true}'::jsonb
);

SELECT public.pinyin_word_romanize('重门', model => 'g2pm');
SELECT public.pinyin_model_romanize('银行行长', 'g2pm');
```

`model => 'g2pw'` 会被解析成 kind alias `g2pw_onnx`，不是 `model_name`。
`model => 'g2pm'` 会被解析成 kind alias `g2pm_numpy`。
公开 API 要求该 kind 在 `pinyin_model_registry` 里恰好只有一条 enabled 记录。

`pinyin_word_romanize(..., model => 'g2pw')` 的优先级：

1. suffix word
2. base word
3. suffix char 仅当它能解析出唯一候选时覆盖
4. 模型在 base char candidates 中做选择
5. base char 第一候选
6. 原文保留

`pinyin_model_romanize(..., 'g2pw')` 则完全跳过词级词典命中，只走候选解析 + 模型选音 + 第一候选回退。

如果模型加载失败，或置信度 / margin 低于阈值，模型辅助路径默认都会平滑回退到 base char 的第一候选。

## 模型缓存语义

模型 runtime 会按 PostgreSQL backend-local singleton 的方式缓存：

- 每个 PostgreSQL backend 进程各自维护一份缓存
- 每个规范化后的 model kind 各有一个 cache entry
- `pinyin_model_meta.version` 变化时重新加载
- 相关 GUC 签名变化时重新加载

不会尝试跨 PostgreSQL backend 进程共享模型实例。
在并发 SQL 负载下，不同 PostgreSQL backend 仍然可能各自加载一份模型 runtime。

### 延后的 Server-Wide Singleton

`v0.1.0` 明确只做到 backend-local cache。
这一版不会承诺真正的 PostgreSQL server-wide singleton model host，因为那通常需要 background worker、shared memory IPC，以及 `shared_preload_libraries` 依赖。

这类 server-wide 方案属于后续 roadmap，不是当前发布物的保证。

## 模型 GUC

带模型的 API 可以通过 PostgreSQL GUC 在数据库层面调参：

- `pg_pinyin.g2pw_window_size`
- `pg_pinyin.g2pw_intra_op_num_threads`
- `pg_pinyin.model_min_confidence`
- `pg_pinyin.model_min_margin`

其中 `g2pw_window_size` 和 `g2pw_intra_op_num_threads` 只对 `g2pw_onnx` 生效。
`g2pm_numpy` 目前只使用共享的阈值 GUC，还没有额外的 backend 专属 GUC。

当前 Docker 测试镜像里，推荐的 CPU 默认值是：

```sql
ALTER SYSTEM SET pg_pinyin.g2pw_window_size = '32';
ALTER SYSTEM SET pg_pinyin.g2pw_intra_op_num_threads = '2';
ALTER SYSTEM SET pg_pinyin.model_min_confidence = '0';
ALTER SYSTEM SET pg_pinyin.model_min_margin = '0';
SELECT pg_reload_conf();
```

说明：

- `window_size=32` 与官方 g2pW 的 CPP 配置一致。
- `intra_op_num_threads=2` 与官方 converter 一致，也是我们目前在 CPU 上测到更稳妥的速度 / 简洁性折中。
- `model_min_confidence=0` 和 `model_min_margin=0` 会关闭额外阈值，benchmark 时能看到真实模型行为，而不是过于保守地回退到第一候选。
- 这些设置能明显改善当前 CPU 路径，而且扩展现在已经会先把一句话里未决的多音字合并后，再统一调用一次 ORT。
- PostgreSQL 内部推理仍然比 standalone ORT 慢，主要原因是扩展当前只在单条 SQL 行 / 单句范围内做 batching，而 standalone benchmark 可以驱动更大的跨句批次。

## `hybrid_onnx` 构建说明

基础 PostgreSQL 构建现在已经包含 `g2pm_numpy`。
只有 `g2pw_onnx` 需要额外启用 Cargo feature `hybrid_onnx`：

```bash
cargo build --features "pg18"
cargo build --features "pg18 hybrid_onnx"
```

说明：

- `pg18` 本身就对应 normal 包行为，包含内置 `g2pm`。
- 开启 `hybrid_onnx` 后才会加入 `g2pw_onnx`，并且要求系统额外安装 ONNX Runtime。
- 模型资产使用外部路径，不会打包进本仓库。
- 使用本扩展的 PostgreSQL 数据库建议采用 UTF-8 编码。

## `model-v*` 发布物里的 `g2pW` 启用方式

`_model` 包默认不自带 `g2pW`，需要你手工准备：

1. 安装对应 PostgreSQL 主版本的 `model-v*` 包。
2. 在系统里安装 ONNX Runtime，让 PostgreSQL 能找到 `libonnxruntime.so`。
3. 把官方 `g2pW` 文件放到：
   `/usr/share/postgresql/<major>/extension/pg_pinyin/g2pw/`
4. 在 `pinyin.pinyin_model_registry` 中插入或更新 `g2pw_onnx` 记录。
5. 用 `SELECT public.pinyin_word_romanize('银行行长', model => 'g2pw');` 做 smoke test。

该目录下预期至少有：

- `g2pw.onnx`
- `POLYPHONIC_CHARS.txt`
- `vocab.txt`（或包含它的目录）

排查建议：

- 如果提示找不到 `libonnxruntime.so`，先检查 ONNX Runtime 是否已安装并进入系统 loader path。
- 如果模型加载失败，检查 `model_path`、`tokenizer_path`、`labels_path`。
- 如果扩展不是用 `hybrid_onnx` 构建的，`g2pw` 会报错，但 `g2pm` 仍然可用。

## 扩展内置词典数据

Rust 扩展在编译时内置以下数据：

- `sql/data/pinyin_mapping.csv`
- `sql/data/pinyin_token.csv`
- `sql/data/pinyin_words.csv`

在执行 `CREATE EXTENSION pg_pinyin` 时，会把内嵌 CSV 数据通过 PostgreSQL `COPY`
写入 `pinyin` schema 下的字典表（失败时会回退到 `INSERT`）。
使用扩展时无需额外执行 `sql/load_data.sql`。

## 数据准备（一键）

数据脚本已迁入本仓库：

- `scripts/data/generate_extension_data.py`
- `scripts/generate_data.sh`

子模块路径：

- `third_party/pinyin-data`

初始化：

```bash
git submodule update --init third_party/pinyin-data
```

一键生成扩展所需数据：

```bash
./scripts/generate_data.sh
```

输出文件：

- `sql/data/pinyin_mapping.csv`
- `sql/data/pinyin_token.csv`
- `sql/data/pinyin_words.csv`

## SQL 基线加载

```bash
psql "$PGURL" -f sql/pinyin.sql

psql "$PGURL" \
  -v mapping_file='/absolute/path/sql/data/pinyin_mapping.csv' \
  -v token_file='/absolute/path/sql/data/pinyin_token.csv' \
  -v words_file='/absolute/path/sql/data/pinyin_words.csv' \
  -f sql/load_data.sql
```

## 测试

pgTAP：

```bash
./test/pgtap/run.sh
```

Rust 扩展测试：

```bash
cargo pgrx test pg18 --features pg18
```

## Docker（通用上游地址）

- `docker/Dockerfile.test-trixie`
- `docker/Dockerfile.release-trixie`

默认使用上游地址（不再改写镜像源）：

- 基础镜像：`postgres:18.4-trixie`
- apt 源：基础镜像默认配置
- rustup/cargo：官方默认地址

构建测试镜像：

```bash
docker build -f docker/Dockerfile.test-trixie -t pg_pinyin/test:trixie .
```

构建发布镜像：

```bash
docker build -f docker/Dockerfile.release-trixie -t pg_pinyin/release:trixie .
```

Dockerfile 已使用 BuildKit cache mount 缓存 Rust 下载/索引。
如需显式开启 BuildKit：

```bash
DOCKER_BUILDKIT=1 docker build -f docker/Dockerfile.test-trixie -t pg_pinyin/test:trixie .
```

## Benchmark

仅衡量分词/拼音化性能（不比较检索性能）：

- `scripts/benchmark_pg18.sh`
- `scripts/docker_pg18_test_and_bench.sh`（基于 `docker/Dockerfile.test-trixie` 构建测试镜像，运行 pgTAP，并在容器里跑 benchmark）
- `scripts/benchmark_g2pw_cpp.sh`（首次运行会下载官方公开 `g2pW` ONNX 模型，并在 CPP 数据集上做独立 ORT benchmark）

脚本会覆盖以下场景：

- SQL 字级：`characters2romanize(name)`（`cold` + `warm`）
- Rust 字级：`pinyin_char_romanize(name)`（`cold` + `warm`）
- Rust 字级（用户后缀词典叠加）：`pinyin_char_romanize(name, '_<suffix>')`（`cold` + `warm`）
- SQL 词级：`icu_romanize(name::pdb.icu::text[])`（`cold` + `warm`，当存在 `pg_search`）
- Rust 词级（tokenizer 输入）：`pinyin_word_romanize(name::pdb.icu::text[])`（`cold` + `warm`）
- Rust 词级（后缀词典叠加）：`pinyin_word_romanize(name::pdb.icu::text[], '_<suffix>')`（`cold` + `warm`）
- Rust 词级（纯文本输入）：`pinyin_word_romanize(name)`（`cold` + `warm`）
- Rust 词级 + 模型回退：`pinyin_word_romanize(name, model => 'g2pw')`（`cold` + `warm`）
- Rust 词级 + 模型回退：`pinyin_word_romanize(name, model => 'g2pm')`（`cold` + `warm`）
- Rust model-only：`pinyin_model_romanize(name, 'g2pw')`（`cold` + `warm`）
- Rust model-only：`pinyin_model_romanize(name, 'g2pm')`（`cold` + `warm`）
- Rust tokenizer 输入 + 模型回退：`pinyin_word_romanize(name::pdb.icu::text[], model => 'g2pw')`（`cold` + `warm`）
- Rust tokenizer 输入 + 模型回退：`pinyin_word_romanize(name::pdb.icu::text[], model => 'g2pm')`（`cold` + `warm`）
- 内部 `g2pw` batch helper：`pinyin__benchmark_model_target_batch_length(payload, 'g2pw')`（仅 CPP，`cold` + `warm`）

所有基准查询都使用 `EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)`，包含内存占用。
`scripts/benchmark_pg18.sh` 现在还会在报告头部输出 metadata，记录执行环境、架构、行数、模型/GUC 配置和 batching scope。

运行示例：

```bash
ROWS=2000 USER_TABLE_SUFFIX=_bench PGURL=postgres://localhost/postgres ./scripts/benchmark_pg18.sh
```

在 Docker 中跑同一套流程：

```bash
RUN_RUST_TESTS=0 RUN_BENCHMARK=1 BENCH_DATASET=synthetic ROWS=2000 ./scripts/docker_pg18_test_and_bench.sh
```

可选数据集：

- `BENCH_DATASET=synthetic`
- `BENCH_DATASET=cpp`，默认使用仓库内置的 `benchmark/testdata/cpp/test.sent` 和 `benchmark/testdata/cpp/test.lb`。这组文件来自 Interspeech 2023 论文公开数据对应仓库：
  [zhang23h_interspeech.pdf](https://www.isca-archive.org/interspeech_2023/zhang23h_interspeech.pdf)

运行官方 g2pW ONNX 的独立 benchmark：

```bash
./scripts/benchmark_g2pw_cpp.sh --report ./benchmark_g2pw_cpp_report.txt
```

### Benchmark Session（PG18）

最新一次 Docker 结果（PG18，2026-04-12 已刷新基础路径与内置 `g2pm`；下面 `g2pw` 的库内行保留自 2026-04-11 最近一次完整 Docker model 跑数，因为这次 release split 没有改动 `g2pw` 执行路径，模型 GUC 为 `window_size=32`、`intra_op_num_threads=2`、`min_confidence=0`、`min_margin=0`）：

synthetic 数据集（`ROWS=2000`）：

字级模式

| 场景                                                                                     |        Cold |        Warm | 相对 SQL 提升（`cold` / `warm`） |
| ---------------------------------------------------------------------------------------- | ----------: | ----------: | -------------------------------: |
| SQL 基线（`characters2romanize`）                                                        |  `9937.718` |  `9859.117` |                      `1.0x / 1.0x` |
| Rust（`pinyin_char_romanize`）                                                           |   `109.390` |    `31.505` |                     `90.8x / 312.9x` |
| Rust + 后缀词典（`pinyin_char_romanize(name, '_bench')`）                                |   `272.373` |    `33.326` |                     `36.5x / 295.8x` |

词级模式（`pg_search` tokenizer 输入）

| 场景                                                                                     |        Cold |        Warm | 相对 SQL 提升（`cold` / `warm`） |
| ---------------------------------------------------------------------------------------- | ----------: | ----------: | -------------------------------: |
| SQL 基线（`icu_romanize(name::pdb.icu::text[])`）                                        |   `281.951` |   `250.884` |                      `1.0x / 1.0x` |
| Rust tokenizer（`pinyin_word_romanize(name::pdb.icu::text[])`）                          |    `77.337` |    `77.464` |                        `3.6x / 3.2x` |
| Rust tokenizer + 后缀词典（`pinyin_word_romanize(name::pdb.icu::text[], '_bench')`）    |    `78.970` |    `77.460` |                        `3.6x / 3.2x` |
| Rust 纯文本（`pinyin_word_romanize(name)`）                                              |   `418.301` |    `34.014` |                        `0.7x / 7.4x` |
| Rust 纯文本 + 后缀词典（`pinyin_word_romanize(name, '_bench')`）                         |  `1511.666` |    `41.509` |                        `0.2x / 6.0x` |
| Rust 纯文本 + 模型（`pinyin_word_romanize(name, model => 'g2pw')`）                      | `10918.896` |  `3840.944` |                        `0.0x / 0.1x` |
| Rust model-only（`pinyin_model_romanize(name, 'g2pw')`）                                 | `14567.764` |  `7969.875` |                        `0.0x / 0.0x` |
| Rust tokenizer + 模型（`pinyin_word_romanize(name::pdb.icu::text[], model => 'g2pw')`） |  `3462.680` |  `3373.820` |                        `0.1x / 0.1x` |
| Rust 纯文本 + 模型（`pinyin_word_romanize(name, model => 'g2pm')`）                      |  `1869.567` |  `1862.360` |                        `0.2x / 0.1x` |
| Rust model-only（`pinyin_model_romanize(name, 'g2pm')`）                                 |  `2734.517` |  `2658.187` |                        `0.1x / 0.1x` |
| Rust tokenizer + 模型（`pinyin_word_romanize(name::pdb.icu::text[], model => 'g2pm')`） |  `1789.931` |  `1811.543` |                        `0.2x / 0.1x` |

CPP 计时数据集（`benchmark/testdata/cpp/test.sent`，去掉 sentencepiece 标记后的前 `ROWS=500` 句）：

字级模式

| 场景                                                                                     |        Cold |        Warm | 相对 SQL 提升（`cold` / `warm`） |
| ---------------------------------------------------------------------------------------- | ----------: | ----------: | -------------------------------: |
| SQL 基线（`characters2romanize`）                                                        |  `1256.717` |  `1229.955` |                        `1.0x / 1.0x` |
| Rust（`pinyin_char_romanize`）                                                           |    `96.955` |    `31.466` |                       `13.0x / 39.1x` |
| Rust + 后缀词典（`pinyin_char_romanize(name, '_bench')`）                                |   `258.189` |    `33.340` |                        `4.9x / 36.9x` |

词级模式（`pg_search` tokenizer 输入）

| 场景                                                                                     |        Cold |        Warm | 相对 SQL 提升（`cold` / `warm`） |
| ---------------------------------------------------------------------------------------- | ----------: | ----------: | -------------------------------: |
| SQL 基线（`icu_romanize(name::pdb.icu::text[])`）                                        |   `113.244` |    `81.492` |                        `1.0x / 1.0x` |
| Rust tokenizer（`pinyin_word_romanize(name::pdb.icu::text[])`）                          |    `50.533` |    `49.279` |                        `2.2x / 1.7x` |
| Rust tokenizer + 后缀词典（`pinyin_word_romanize(name::pdb.icu::text[], '_bench')`）    |    `51.875` |    `51.279` |                        `2.2x / 1.6x` |
| Rust 纯文本（`pinyin_word_romanize(name)`）                                              |   `391.888` |    `62.857` |                        `0.3x / 1.3x` |
| Rust 纯文本 + 后缀词典（`pinyin_word_romanize(name, '_bench')`）                         |  `1456.842` |    `66.582` |                        `0.1x / 1.2x` |
| Rust 纯文本 + 模型（`pinyin_word_romanize(name, model => 'g2pw')`）                      | `12865.518` | `11889.673` |                        `0.0x / 0.0x` |
| Rust model-only（`pinyin_model_romanize(name, 'g2pw')`）                                 |  `6104.084` |  `4917.590` |                        `0.0x / 0.0x` |
| Rust tokenizer + 模型（`pinyin_word_romanize(name::pdb.icu::text[], model => 'g2pw')`） | `11426.707` | `11455.037` |                        `0.0x / 0.0x` |
| Rust 纯文本 + 模型（`pinyin_word_romanize(name, model => 'g2pm')`）                      |  `5730.846` |  `5511.569` |                        `0.0x / 0.0x` |
| Rust model-only（`pinyin_model_romanize(name, 'g2pm')`）                                 |  `6843.033` |  `6679.726` |                        `0.0x / 0.0x` |
| Rust tokenizer + 模型（`pinyin_word_romanize(name::pdb.icu::text[], model => 'g2pm')`） |  `5398.908` |  `5242.447` |                        `0.0x / 0.0x` |

CPP 目标字准确率（`benchmark/testdata/cpp/test.sent` + `test.lb`，全量 `CPP_ACCURACY_ROWS=8935`，去声调）：

| 方法 | 去声调准确率 |
| ---- | -----------: |
| 字级词典（`pinyin__char_target_debug`） | `88.4051%` |
| 词级词典（`pinyin__word_target_debug`） | `92.4566%` |
| 词级 + 模型（`pinyin__word_target_debug(..., 'g2pw')`） | `93.4303%` |
| 模型直出（`pinyin__model_target_debug(..., 'g2pw')`） | `91.4941%` |
| 词级 + 模型（`pinyin__word_target_debug(..., 'g2pm')`） | `97.2132%` |
| 模型直出（`pinyin__model_target_debug(..., 'g2pm')`） | `98.1309%` |

以上数值为 `EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)` 的 `Execution Time`（毫秒）。
Rust 基线路径的 `cold` 在执行前会先 bump 一次字典版本，用于模拟首次加载缓存。
后缀词典会在首次使用时加载缓存并跨语句复用。若后缀表发生更新，可调用 `public.pinyin_clear_suffix_cache('_suffix')`（或 `public.pinyin_clear_suffix_cache()` 清空全部）手动失效缓存。
当前 CPP 备注里，计时表使用 `ROWS=500`，准确率表使用全量 vendored split，即 `CPP_ACCURACY_ROWS=8935`。
`public.pinyin__polyphone_romanize(name, query_pos)` 依然适合做更窄的扩展内 ORT 核心 benchmark，但这里优先展示公开 API 家族的表现。

新增的 `public.pinyin__benchmark_model_target_batch_length(...)` 仅用于 benchmark，不建议作为普通 SQL API 使用。
它会在一次 SQL 调用里处理很多目标字，用来剥离掉大部分逐行 SQL 调用开销；但它仍然使用扩展当前的 backend-local model runtime，以及“按句子发起模型请求”的执行方式。

### Runtime Gap Investigation

2026-04-12 在 aarch64 Docker 环境里，使用同一组 benchmark GUC（`window_size=32`、`intra_op_num_threads=2`、`min_confidence=0`、`min_margin=0`）做了一次单独调查：

- PostgreSQL 外部的 standalone 原始 `g2pw`，在 10 行 CPP 切片上 warm 推理总耗时 `372.473 ms`，约 `37.247 ms/row`
- 数据库内 public API 路径 `pinyin_word_romanize(name, model => 'g2pw')`，在同样的 10 行 CPP 切片上，90 秒内仍未完成
- 内部 helper `pinyin__benchmark_model_target_batch_length(..., 'g2pw')` 已经去掉了大部分逐行 SQL 调用开销，但在同一环境里的 500 行 CPP 切片上，8 分钟内也仍未完成

这说明当前差距并不只是 SQL wrapper 开销。
在这个 Docker/aarch64 CPU 路径上，和 standalone `g2pw` 的性能差，主要看起来还是卡在扩展内部的模型/runtime 执行本身，而不只是 public SQL 入口的调度。
这次调查的原始生成报告不纳入版本控制；这里保留摘要结论。

### 真实 g2pW ONNX Benchmark

最新一次独立运行（2026-04-12）使用官方公开 `G2PWModel-v2-onnx.zip`、仓库内置的 `benchmark/testdata/cpp/test.sent` + `test.lb` 全量 `8935` 行，并将 `intra_op_threads=2` 设为与官方 converter 相同的配置：

| 指标 | 数值 |
| ---- | ---: |
| 行数 | `8935` |
| 目标字覆盖 | `onnx=7654, mono=1224, char_default=57, unknown=0` |
| 全量带声调准确率 | `85.7079%` |
| 全量去声调准确率 | `92.5797%` |
| 仅 ONNX 行带声调准确率 | `86.5691%` |
| 仅 ONNX 行去声调准确率 | `92.5398%` |
| 模型+tokenizer 加载 | `7692.993 ms` |
| 特征准备 | `1629.357 ms` |
| 冷启动推理 | `256054.396 ms` |
| 热启动推理 | `252729.680 ms` |
| 首次总耗时 | `265376.747 ms` |
| 热启动单行推理 | `28.285359 ms` |

这个 benchmark 是在 PostgreSQL 之外直接调用公开 `g2pW` 推理模型与官方支持文件得到的，适合跟踪原始模型的准确率和 ORT 延迟。它需要和上面的“数据库内真实 ORT”结果分开看待：扩展侧现在已经有独立的插件内速度与准确率数据，而这里保留的是 PostgreSQL 外部的参考值。

### 为什么不是 `99.08%`

g2pW 论文里给出的 CPP `99.08%` 很容易被直接拿来和这里的数字对比，但这里其实有两个重要口径差异：

- 论文明确写了公开发布的是 “model weights trained from the MPB dataset”；而 README / PyPI 页面里，CPP benchmark 用的是单独的 `saved_models/CPP_...` checkpoint，而不是公开下载的 `G2PWModel-v2-onnx.zip`。这很强烈地说明，公开 ONNX 模型并不等同于论文里拿到 `99.08%` 的那个 CPP checkpoint。[论文](https://www.isca-archive.org/interspeech_2022/chen22d_interspeech.pdf)，[PyPI](https://pypi.org/project/g2pw/)
- CVTE-Poly 论文随后指出，CPP 本身有错误标注，并且测试集里有 `1319` 条目标字实际上不是多音字；他们据此定义了 `8935` 行的 refined CPP test split。这意味着“CPP accuracy”本身就取决于你测的是原始 g2pM split（`10254` 行）还是 refined split（`8935` 行）。[CVTE-Poly](https://www.isca-archive.org/interspeech_2023/zhang23h_interspeech.pdf)

我们当前对公开 ONNX release 的参考测量是：

- vendored refined CPP（`8935` 行）：去声调准确率 `92.5797%`
- 原始 g2pM/CPP test split（`10254` 行）：去声调准确率 `92.5005%`

所以和 `99.08%` 的差距，并不只是 PostgreSQL 接入造成的。相当一部分差距来自：你在拿公开 released inference model，去对比一个大概率使用了不同 checkpoint、并且 benchmark 口径也不同的论文结果。

## Roadmap

1. 梳理数据生成流水线，并持续扩充词级字典覆盖。
2. ~~支持用户自定义词典，并可按指定词典执行拼音化。~~
3. 提供平滑升级路径（扩展内置词典与用户词典的升级策略）。
4. 改进英文处理能力（包括 stemming）。
5. 提供更多不依赖 `pg_search` 的示例。
6. 持续优化性能与内存平衡（例如评估 frozen hash 相对表查找的收益）。

## 可在线更新字典表

以下表支持用户直接增删改：

- `pinyin.pinyin_mapping`
- `pinyin.pinyin_words`
- `pinyin.pinyin_token`

更新后无需重编译扩展。

## SQL 基线专利引用

若使用 SQL 基线方法（`sql/pinyin.sql`），请引用：

- CN115905297A： [一种支持拼音检索和排序的方法及系统](https://patents.google.com/patent/CN115905297A/zh)

```bibtex
@patent{CN115905297A,
  author  = {梁展钊},
  title   = {一种支持拼音检索和排序的方法及系统},
  number  = {CN115905297A},
  country = {CN},
  year    = {2023},
  url     = {https://patents.google.com/patent/CN115905297A/zh}
}
```

## 致谢

- 汉字词组拼音 TSV 数据来源： [tsroten/dragonmapper `hanzi_pinyin_words.tsv`](https://github.com/tsroten/dragonmapper/blob/main/src/dragonmapper/data/hanzi_pinyin_words.tsv)
