# pg_pinyin（中文说明）

[English README](readme.md)

维护者：Liang Zhanzhao

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

首次调用时会自动将数据写入 `pinyin` schema 下的字典表。
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

## Benchmark

仅衡量分词/归一化性能（不比较检索性能）：

- `scripts/benchmark_pg18.sh`

运行示例：

```bash
ROWS=2000 PGURL=postgres://localhost/postgres ./scripts/benchmark_pg18.sh
```

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
