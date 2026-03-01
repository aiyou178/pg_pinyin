# pg_pinyin（中文说明）

[English README](readme.md)

`pg_pinyin` 包含两套实现：

1. SQL 基线方案（`sql/pinyin.sql`）
2. Rust 扩展方案（`src/lib.rs`）

## 扩展接口（精简）

仅提供两类拼音化能力：

- `pinyin_char_romanize(text)`
- `pinyin_char_romanize(text, suffix text)`
- `pinyin_word_romanize(text)`
- `pinyin_word_romanize(text, suffix text)`
- `pinyin_word_romanize(tokenizer_input anyelement)`（重载，支持 `pdb` tokenizer 输入，如 `name::pdb.icu::text[]`）
- `pinyin_word_romanize(tokenizer_input anyelement, suffix text)`（带用户词典后缀的重载）

推荐组合：

1. 字级拼音化 + `pg_trgm`
2. 词级拼音化 + `pg_search`

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

## Docker（通用上游地址）

- `docker/Dockerfile.test-trixie`
- `docker/Dockerfile.release-trixie`

默认使用上游地址（不再改写镜像源）：

- 基础镜像：`postgres:18.3-trixie`
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

脚本会覆盖以下场景：

- SQL 字级：`characters2romanize(name)`
- Rust 字级：`pinyin_char_romanize(name)`
- Rust 字级（用户后缀词典叠加）：`pinyin_char_romanize(name, '_<suffix>')`
- SQL 词级：`icu_romanize(name::pdb.icu::text[])`（当存在 `pg_search`）
- Rust 词级：`pinyin_word_romanize(name::pdb.icu::text[])`（有 `pg_search`）或 `pinyin_word_romanize(name)`（无 `pg_search`）
- Rust 词级（用户后缀词典叠加）：`pinyin_word_romanize(name::pdb.icu::text[], '_<suffix>')`

所有基准查询都使用 `EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)`，包含内存占用。

运行示例：

```bash
ROWS=2000 USER_TABLE_SUFFIX=_bench PGURL=postgres://localhost/postgres ./scripts/benchmark_pg18.sh
```

### Benchmark Session（PG18）

会话命令：

```bash
ROWS=2000 USER_TABLE_SUFFIX=_bench PGURL=postgres://postgres@localhost:5432/postgres ./scripts/benchmark_pg18.sh
```

最新一次结果（PG18，`ROWS=2000`）：

| 场景                                                                                                                     |     Rust 扩展 |      SQL 基线 | 加速比（`SQL` / `Rust`） |
| ------------------------------------------------------------------------------------------------------------------------ | ------------: | ------------: | -----------------------: |
| 字级拼音化（`pinyin_char_romanize` vs `characters2romanize`）                                                            |  `344.609 ms` | `9303.897 ms` |                  `27.0x` |
| 字级拼音化（后缀词典，`pinyin_char_romanize(name, '_bench')` vs `characters2romanize`）                                  | `5377.008 ms` | `9303.897 ms` |                   `1.7x` |
| 词级拼音化（`pinyin_word_romanize(name::pdb.icu::text[])` vs `icu_romanize(name::pdb.icu::text[])`）                     |   `69.968 ms` |  `313.753 ms` |                   `4.5x` |
| 词级拼音化（后缀词典，`pinyin_word_romanize(name::pdb.icu::text[], '_bench')` vs `icu_romanize(name::pdb.icu::text[])`） | `5487.158 ms` |  `313.753 ms` |                   `0.1x` |

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
