# pg_pinyin（中文说明）

[English README](readme.md)

`pg_pinyin` 包含两套实现：

1. SQL 基线方案（`sql/pinyin.sql`）
2. Rust 扩展方案（`src/lib.rs`）

## 核心扩展公开接口

`CREATE EXTENSION pg_pinyin` 会安装 Rust-backed 核心接口：

- `pinyin_char_romanize(text)`
- `pinyin_char_romanize(text, suffix text)`
- `pinyin_word_romanize(text)`
- `pinyin_word_romanize(text, suffix text)`
- `pinyin_word_romanize(tokenizer_input anyelement)`（重载，支持 `pdb` tokenizer 输入，如 `name::pdb.icu::text[]`）
- `pinyin_word_romanize(tokenizer_input anyelement, suffix text)`（带用户词典后缀的重载）
- `pinyin_regex_phrase(text, slope integer DEFAULT NULL, max_expansions integer DEFAULT NULL, generated_pinyin boolean DEFAULT false)`（`pg_search` query helper；当 `pg_search` 已在当前数据库启用时，由 `CREATE EXTENSION pg_pinyin` 安装，返回 `pdb.query`）

`pinyin_regex_phrase` 是 Rust backend 的公开接口，但返回类型是 `pdb.query`，因此必须先在当前数据库启用 `pg_search`，再 `CREATE EXTENSION pg_pinyin`。PostgreSQL extension script 不能可靠地在安装过程中启用另一个 extension。如果先安装 `pg_pinyin`、后安装 `pg_search`，拼音化接口仍会安装，`pinyin_regex_phrase` 会安装为 error stub，调用时给出明确异常。

## 核心内部接口

`CREATE EXTENSION pg_pinyin` 还会安装 `pinyin_regex_phrase_patterns(text, generated_pinyin boolean DEFAULT false)`。这是 Rust-backed 的内部 helper，用于 `pinyin_regex_phrase`；业务 SQL 通常应调用 `pinyin_regex_phrase(...)`。

当输入为空、仅空白、或无法解析为拼音 token 时，`pinyin_regex_phrase_patterns` 返回空 `text[]`。SQL NULL 输入仍返回 SQL NULL，因为该函数是 strict。

## 可选 pg_search SQL Helper

`sql/word.sql` 不会随 `CREATE EXTENSION pg_pinyin` 自动安装。它是 `sql/pinyin.sql` 的 SQL-backend 配套文件；需要先加载 `sql/pinyin.sql`，并且只在数据库里已安装 `pg_search` 时再加载。`pg_search` 0.24.0 需要先 preload，例如启动 PostgreSQL 时加 `-c shared_preload_libraries=pg_search`，之后才能 `CREATE EXTENSION pg_search`。

- `sql_pinyin_regex_phrase(text, slope integer DEFAULT NULL, max_expansions integer DEFAULT NULL, generated_pinyin boolean DEFAULT false)`（SQL 分词，返回 `pdb.query`）
- `sql_pinyin_regex_phrase_patterns(text, generated_pinyin boolean DEFAULT false)`（SQL helper，返回 regex phrase token 的 `text[]`）
`sql_pinyin_regex_phrase_patterns` 使用同样的空数组语义。`sql_pinyin_regex_phrase` 和 Rust-backed `pinyin_regex_phrase` 会把空 pattern 数组映射为 `pdb.empty()`，因此可以安全用于 `@@@`；如果调用方希望空用户查询不影响其他过滤条件，应省略 `@@@` 条件，或显式使用 `pdb.all()`。

推荐组合：

1. 字级拼音化 + `pg_trgm`
2. 词级拼音化 + `pg_search`

可选 `pg_search` 查询 helper：

```sql
CREATE EXTENSION IF NOT EXISTS pg_search;
CREATE EXTENSION IF NOT EXISTS pg_pinyin;
\i sql/pinyin.sql
\i sql/word.sql

CREATE TABLE voice (
  id bigserial PRIMARY KEY,
  description text NOT NULL,
  pinyin text GENERATED ALWAYS AS (public.pinyin_word_romanize(description)) STORED
);

CREATE INDEX voice_pinyin_bm25_idx
ON voice
USING bm25 (id, pinyin)
WITH (key_field='id');

-- Rust backend：当 pg_search 已在当前数据库启用时，CREATE EXTENSION pg_pinyin
-- 会直接导出 pinyin_regex_phrase()。
SELECT *
FROM voice
WHERE pinyin @@@ public.pinyin_regex_phrase('zhengshuang');

-- SQL backend fallback：未安装 pg_pinyin Rust extension 时使用。
SELECT *
FROM voice
WHERE pinyin @@@ public.sql_pinyin_regex_phrase('zhengshuang');
```

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

示例：

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

PostgreSQL 19 beta 1 通过 `pgrx` 0.19.1 支持：

```bash
cargo pgrx init --pg19=/usr/lib/postgresql/19/bin/pg_config
cargo pgrx install --features pg19 --no-default-features --pg-config /usr/lib/postgresql/19/bin/pg_config
```

## Docker（通用上游地址）

- `docker/Dockerfile.test-trixie`
- `docker/Dockerfile.test-pg19beta1-trixie`
- `docker/Dockerfile.release-trixie`

默认使用上游地址（不再改写镜像源）：

- 基础镜像：PG18 测试使用 `postgres:18.3-trixie`，PG19 beta 测试使用 `postgres:19beta1-trixie`
- apt 源：基础镜像默认配置
- rustup/cargo：官方默认地址

构建测试镜像：

```bash
docker build -f docker/Dockerfile.test-trixie -t pg_pinyin/test:trixie .

# 可选：本地临时使用镜像源构建，默认 CI/远端仍不替换源
docker build -f docker/Dockerfile.test-trixie -t pg_pinyin/test:trixie \
  --build-arg DEBIAN_MIRROR=http://mirrors.tuna.tsinghua.edu.cn/debian \
  --build-arg DEBIAN_SECURITY_MIRROR=http://mirrors.tuna.tsinghua.edu.cn/debian-security \
  --build-arg RUSTUP_DIST_SERVER=https://mirrors.tuna.tsinghua.edu.cn/rustup \
  --build-arg RUSTUP_UPDATE_ROOT=https://mirrors.tuna.tsinghua.edu.cn/rustup/rustup \
  --build-arg CARGO_REGISTRIES_CRATES_IO_INDEX=sparse+https://mirrors.tuna.tsinghua.edu.cn/crates.io-index/ \
  .
```

构建 PostgreSQL 19 beta 测试镜像：

```bash
docker build -f docker/Dockerfile.test-pg19beta1-trixie -t pg_pinyin/test:pg19beta1 .
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

脚本会覆盖以下场景：

- SQL 字级：`characters2romanize(name)`（`cold` + `warm`）
- Rust 字级：`pinyin_char_romanize(name)`（`cold` + `warm`）
- Rust 字级（用户后缀词典叠加）：`pinyin_char_romanize(name, '_<suffix>')`（`cold` + `warm`）
- SQL 词级：`icu_romanize(name::pdb.icu::text[])`（`cold` + `warm`，当存在 `pg_search`）
- Rust 词级（tokenizer 输入）：`pinyin_word_romanize(name::pdb.icu::text[])`（`cold` + `warm`）
- Rust 词级（后缀词典叠加）：`pinyin_word_romanize(name::pdb.icu::text[], '_<suffix>')`（`cold` + `warm`）
- Rust 词级（纯文本输入）：`pinyin_word_romanize(name)`（`cold` + `warm`）
- SQL backend 查询构造：`sql_pinyin_regex_phrase_patterns(query)`，以及构造 `pdb.query` 的 `sql_pinyin_regex_phrase(query)`（`warm`，来自 `sql/word.sql`）
- Rust backend 查询 token 构造：`pinyin_regex_phrase_patterns(query)`（`warm`，来自 `CREATE EXTENSION pg_pinyin`）
- Rust backend `pg_search` query 构造：`pinyin_regex_phrase(query)`（`warm`，当 `pg_search` 已在当前数据库启用时由 `CREATE EXTENSION pg_pinyin` 导出）
- Rust 独立查询 token 构造：`cargo run --release --bin benchmark_pinyin_regex_phrase -- --mode tokens`
- Python 独立查询 token 构造：`scripts/benchmark_pinyin_regex_phrase.py --mode tokens`，使用相同 token 输出

所有基准查询都使用 `EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)`，包含内存占用。

默认情况下，`scripts/benchmark_pg18.sh` 会在每次 run 前 drop/create 专用 benchmark database（`BENCHMARK_DATABASE=pg_pinyin_benchmark`），并在每个 benchmark task 前刷新 runtime state。这样可以避免复用旧 extension、SQL helper 定义、BM25 index、bench table 和 Rust 字典缓存。只有在明确想复用 `PGURL` 指向的数据库时，才设置 `BENCHMARK_FRESH_DATABASE=0`。

运行示例：

```bash
ROWS=2000 REGEX_BENCH_ROWS=20000 USER_TABLE_SUFFIX=_bench PGURL=postgres://localhost/postgres ./scripts/benchmark_pg18.sh
```

独立 helper benchmark：

```bash
cargo run --release --bin benchmark_pinyin_regex_phrase -- --rows 20000 --runs 5 --mode tokens
python3 scripts/benchmark_pinyin_regex_phrase.py --rows 20000 --runs 5
```

### Benchmark Session（PG18）

会话命令：

```bash
ROWS=2000 REGEX_BENCH_ROWS=20000 USER_TABLE_SUFFIX=_bench PGURL=postgres://postgres@localhost:5432/postgres ./scripts/benchmark_pg18.sh
```

最新一次结果（PG18，`pg_search=0.24.0`，`pg_pinyin=0.0.4`，fresh benchmark database，`ROWS=2000`，`REGEX_BENCH_ROWS=20000`，2026-06-08）：

字级模式：

| 场景                                                                                     |       Cold |       Warm | 相对 SQL 提升（`cold` / `warm`） |
| ---------------------------------------------------------------------------------------- | ---------: | ---------: | -------------------------------: |
| SQL 基线（`characters2romanize`）                                                        |  `8291.975` | `8072.434` |                      `1.0x / 1.0x` |
| Rust（`pinyin_char_romanize`）                                                           |    `90.025` |   `32.087` |                     `92.1x / 251.6x` |
| Rust + 后缀词典（`pinyin_char_romanize(name, '_bench')`）                                |   `176.412` |   `33.863` |                     `47.0x / 238.4x` |

词级模式（`pg_search` tokenizer 输入）：

| 场景                                                                                     |      Cold |      Warm | 相对 SQL 提升（`cold` / `warm`） |
| ---------------------------------------------------------------------------------------- | --------: | --------: | -------------------------------: |
| SQL 基线（`icu_romanize(name::pdb.icu::text[])`）                                        | `255.649` | `249.832` |                      `1.0x / 1.0x` |
| Rust（`pinyin_word_romanize(name::pdb.icu::text[])`）                                    | `332.474` |  `71.259` |                      `0.8x / 3.5x` |
| Rust + 后缀词典（`pinyin_word_romanize(name::pdb.icu::text[], '_bench')`）              | `788.152` |  `69.230` |                      `0.3x / 3.6x` |
| Rust 纯文本（`pinyin_word_romanize(name)`）                                              | `334.510` |  `36.570` |                      `0.8x / 6.8x` |

查询 token 构造（`pinyin_regex_phrase_patterns`，20,000 行）：

| 场景                                                                                     | Cold-ish / Best | Warm / Best | 说明 |
| ---------------------------------------------------------------------------------------- | --------------: | ----------: | ---- |
| SQL backend tokens（`sql_pinyin_regex_phrase_patterns`）                                 |    `1555.603` ms | `1572.450` ms | 来自 `sql/word.sql` 的 PostgreSQL SQL helper |
| Rust backend tokens（`pinyin_regex_phrase_patterns`）                                    |      `41.784` ms |   `43.334` ms | PostgreSQL UDF 路径，包含 `text[]` 返回开销 |
| Rust backend generated-pinyin tokens（`pinyin_regex_phrase_patterns(query, true)`）      |              - |   `44.439` ms | PostgreSQL UDF 路径 |
| Rust backend `pg_search` query（`pinyin_regex_phrase`）                                |              - |   `60.588` ms | 由 `CREATE EXTENSION pg_pinyin` 导出的 public helper |
| Rust backend slope/max（`pinyin_regex_phrase(query, 2, 4096)`）                        |              - |   `57.838` ms | 由 `CREATE EXTENSION pg_pinyin` 导出的 public helper |
| SQL backend + `pg_search` query（`sql_pinyin_regex_phrase`）                             |              - | `1611.476` ms | 构造 `pdb.query` |
| SQL backend + slope/max（`sql_pinyin_regex_phrase(query, 2, 4096)`）                     |              - | `1607.444` ms | 构造 `pdb.query` |
| SQL backend generated-pinyin query（`sql_pinyin_regex_phrase(query, NULL, NULL, true)`） |              - | `1568.413` ms | 构造 `pdb.query` |

独立查询 token 构造（`tokens` mode，20,000 行，不包含 PostgreSQL executor 或 SQL 数组返回开销）：

| 场景 | Best | Median | Best per row | Checksum |
| ---- | ---: | -----: | -----------: | -------: |
| Rust standalone | `3.538` ms | `3.581` ms | `0.177` us | `219996` |
| Python standalone | `33.353` ms | `33.631` ms | `1.668` us | `219996` |

server-side 完整 `pg_search` 查询 benchmark（来自 `bench_pinyin_regex_queries` 的 20,000 行，不包含每次 query 的 client round trip）：

| 场景 | Execution Time | 说明 |
| ---- | -------------: | ---- |
| Rust parser in PostgreSQL + `pg_search`，memoize enabled | `1027.056` ms | query mix 只有 9 个 distinct query，PostgreSQL 会 memoize 重复 LATERAL 结果 |
| Rust parser in PostgreSQL + `pg_search`，memoize disabled 且 JIT off | `5739.186` ms | 执行 20,000 次 BM25 lookup |

完整 `pg_search` 查询 benchmark（20,000 次 client query，目标为 2,000 行 BM25 表，结果 checksum 相同）：

| 场景 | Best | Median | Best per query | Checksum |
| ---- | ---: | -----: | -------------: | -------: |
| Python client parse + `text[]` patterns + `pg_search` | `7439.585` ms | `7503.080` ms | `371.979` us | `1337644` |
| Rust in-Postgres parse + `pg_search` | `7529.758` ms | `7530.354` ms | `376.488` us | `1337644` |

完整 client-query benchmark 说明：一旦每次查询都包含真实 `pg_search` 索引 lookup 和 client/server round trip，parser 本身不再是主要瓶颈。把解析放在 PostgreSQL 内仍然有价值：调用方可以使用单个参数化 SQL API，不需要在客户端维护 token dictionary，不需要动态拼 SQL，也适合 server-side batch 查询。

以上数值为 `EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)` 的 `Execution Time`（毫秒）。
Rust 基线路径的 `cold` 在执行前会先 bump 一次字典版本，用于模拟首次加载缓存。
后缀词典会在首次使用时加载缓存并跨语句复用。若后缀表发生更新，可调用 `public.pinyin_clear_suffix_cache('_suffix')`（或 `public.pinyin_clear_suffix_cache()` 清空全部）手动失效缓存。
独立 Rust/Python 查询 token 数字刻意排除了 PostgreSQL executor、UDF 调用和 SQL 数组物化开销，只比较分词和 pattern 构造路径。

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
