# pg_pinyin（中文说明）

[English README](readme.md)

`pg_pinyin` 包含两套实现：

1. SQL 基线方案（`sql/pinyin.sql`）
2. Rust 扩展方案（`src/lib.rs`）

## 扩展接口（精简）

仅提供两类归一化能力：

- `pinyin_char_normalize(text)`
- `pinyin_word_normalize(text)`
- `pinyin_word_normalize(tokenizer_input anyelement)`（重载，支持 `pdb` tokenizer 输入，如 `name::pdb.icu`）

推荐组合：

1. 字级归一化 + `pg_trgm`
2. 词级归一化 + `pg_search`

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

仅衡量分词/归一化性能（不比较检索性能）：

- `scripts/benchmark_pg18.sh`

运行示例：

```bash
ROWS=2000 PGURL=postgres://localhost/postgres ./scripts/benchmark_pg18.sh
```

## Roadmap

1. 梳理数据生成流水线，并持续扩充词级字典覆盖。
2. 支持用户自定义词典，并可按指定词典执行归一化。
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
