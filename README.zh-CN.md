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
- `pinyin_word_romanize_batch(origins text[])`（多个独立文本文档，返回 `text[]`）
- `pinyin_word_romanize_batch(origins text[], suffix text)`（带用户词典后缀的文档批次）
- `pinyin_regex_phrase(text, slope integer DEFAULT NULL, max_expansions integer DEFAULT NULL, generated_pinyin boolean DEFAULT false)`（`pg_search` query helper；当 `pg_search` 已在当前数据库启用时，由 `CREATE EXTENSION pg_pinyin` 安装，返回 `pdb.query`）
- `pinyin_regex_phrase_mode(text, mode text, slope integer DEFAULT NULL, max_expansions integer DEFAULT NULL, generated_pinyin boolean DEFAULT false)`（显式查询模式，安装条件相同，按需使用）
- `pinyin_phrase(text, slope integer DEFAULT NULL)`（按需使用的原生精确 term／phrase 查询，仅用于普通音节字段，需要 pg_search 原生 phrase API）

`pinyin_regex_phrase` 是 Rust backend 的公开接口，但返回类型是 `pdb.query`，因此必须先在当前数据库启用 `pg_search`，再 `CREATE EXTENSION pg_pinyin`。PostgreSQL extension script 不能可靠地在安装过程中启用另一个 extension。如果先安装 `pg_pinyin`、后安装 `pg_search`，拼音化接口仍会安装，`pinyin_regex_phrase` 会安装为 error stub，调用时给出明确异常。

## 核心内部接口

`CREATE EXTENSION pg_pinyin` 还会安装 `pinyin_regex_phrase_patterns(text, generated_pinyin boolean DEFAULT false)`。这是 Rust-backed 的内部 helper，用于 `pinyin_regex_phrase`；业务 SQL 通常应调用 `pinyin_regex_phrase(...)`。

当输入为空、仅空白、或无法解析为拼音 token 时，`pinyin_regex_phrase_patterns` 返回空 `text[]`。SQL NULL 输入仍返回 SQL NULL，因为该函数是 strict。

`pinyin_regex_phrase_patterns_mode(text, mode text, generated_pinyin boolean DEFAULT false)`
是模式感知的内部 helper。Rust 分词仍使用内嵌数据，不执行 runtime SPI，保持
`PARALLEL SAFE`。旧 helper 与签名均不改变，包括未显式标注类型的 NULL 参数调用。

## 可选 pg_search SQL Helper

`sql/word.sql` 不会随 `CREATE EXTENSION pg_pinyin` 自动安装。它是 `sql/pinyin.sql` 的 SQL-backend 配套文件；需要先加载 `sql/pinyin.sql`，并且只在数据库里已安装 `pg_search` 时再加载。`pg_search` 需要先 preload，例如启动 PostgreSQL 时加 `-c shared_preload_libraries=pg_search`，之后才能 `CREATE EXTENSION pg_search`。

- `sql_pinyin_regex_phrase(text, slope integer DEFAULT NULL, max_expansions integer DEFAULT NULL, generated_pinyin boolean DEFAULT false)`（SQL 分词，返回 `pdb.query`）
- `sql_pinyin_regex_phrase_patterns(text, generated_pinyin boolean DEFAULT false)`（SQL helper，返回 regex phrase token 的 `text[]`）
- `sql_pinyin_regex_phrase_mode(text, mode text, slope integer DEFAULT NULL, max_expansions integer DEFAULT NULL, generated_pinyin boolean DEFAULT false)`（独立 SQL 后端的模式查询接口）
- `sql_pinyin_regex_phrase_patterns_mode(text, mode text, generated_pinyin boolean DEFAULT false)`（独立 SQL 后端的模式 pattern helper）
- `sql_pinyin_phrase(text, slope integer DEFAULT NULL)`（独立 SQL 后端的原生精确 term／phrase 查询）
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

### 显式音节边界

默认的 `pinyin_regex_phrase` 和 `compat` 现在把 ASCII 撇号 `'` 及弯引号
`‘`、`’` 当作硬音节边界，行为与空格一致；独立的 `sql_` 后端也支持。
不会删除分隔符后重新连写：`xi'an -> xi.* / an.*`，无分隔的 `xian` 仍保留
原有单路径贪心结果 `xian.*`；`xin'gan -> xin.* / gan.*`，不会变成 `xing.* / an.*`。
首尾、重复分隔符按空白处理，只有分隔符的输入生成空查询；其他不支持的标点
仍使整个输入无效，不生成自动替代切分。

```sql
-- SQL 字符串中的撇号需要写成两个单引号。
SELECT public.pinyin_regex_phrase_patterns('xi''an'); -- {xi.*,an.*}
SELECT * FROM voice
WHERE pinyin @@@ public.pinyin_regex_phrase('xi''an');
SELECT * FROM voice
WHERE pinyin @@@ public.sql_pinyin_regex_phrase('xi''an');

-- 需要精确音节 term，而不是默认前缀匹配时：
SELECT * FROM voice
WHERE pinyin @@@ public.pinyin_regex_phrase_mode('xi''an', 'full');
```

本项只扩展默认／compat 的输入边界，不改变前缀、首字母、generated-pinyin
pattern、NULL 行为或函数签名。声调／ü 归一化仍由下述显式模式提供。
Rust 后端重编译动态库后使用新连接；独立 SQL 后端重新加载 `sql/word.sql`。
仍属于未发布的 0.0.9，不额外 bump 版本或新增迁移函数。

### 显式查询模式

开发版本 0.0.9 增加独立的模式接口，不通过可能干扰旧调用的重载实现。
按用户输入意图选择模式：

| 模式 | 输入 | 普通字段 pattern |
| --- | --- | --- |
| `full` | `xi an` 或 `xi'an` | `xi`、`an` |
| `initials` | `whh` | `w.*`、`h.*`、`h.*` |
| `initials` | `zh ch sh` | `zh.*`、`ch.*`、`sh.*` |
| `autocomplete` | `xi a` | `xi`、`a.*` |
| `compat` | `xi an` 或 `xi'an` | 旧前缀 pattern `xi.*`、`an.*` |

```sql
SELECT * FROM voice
WHERE pinyin @@@ public.pinyin_regex_phrase_mode('xi an', 'full');
SELECT * FROM voice
WHERE pinyin @@@ public.pinyin_regex_phrase_mode('whh', 'initials');
SELECT * FROM voice
WHERE pinyin @@@ public.sql_pinyin_regex_phrase_mode('xi a', 'autocomplete');
```

新模式把 `ü`、分解形式、`u:` 和 `v` 统一为 `v`，去掉声调符号，
数字声调 0-5 和撇号保留为音节边界，例如 `lv4liang2 -> lv liang`。
`compat` 保留旧 ASCII 切分与前缀行为，并增加上述显式撇号边界。无分隔输入仍使用最长优先切分，
尚未做语境消歧；`xin gan` 和 `xing an` 应明确给出边界。
紧凑首字母按单字母拆分，只有完整的独立 `zh`／`ch`／`sh` 块按双字母声母处理。
自动补全总是扩展最后一个 token，包括输入末尾带空白的情况。
合法的末尾音节前缀优先保持完整：`yon -> yon.*`，不拆成 `yo`、`n.*`；
`niyon -> ni yon.*`。

`full` 精确匹配索引音节 term，不等于全文相等；`an` 不再扩展匹配 `ang`。
新模式的 `generated_pinyin=true` 同时支持普通词组 term 和管道分隔的多读音，
限定左右管道边界，避免 `an` 命中 `|ang|`。这类混合字段需要保留管道的
whitespace tokenizer，例如索引 `(pinyin::pdb.whitespace)`；结构化查询使用
`WHERE pinyin @@@ ...`，左侧不加 tokenizer cast。
Rust 普通词级输出不需要这个标志。选择模式不会删减索引内容或词典覆盖。

新模式对不支持的标点、错误声调、超过 256 个 Unicode 字符或 64 个 token
的输入返回空 pattern 数组／`pdb.empty()`，不截断后搜索。未知模式报 SQLSTATE
`22023`，SQL NULL 仍保留为 NULL。已有数据库安装新二进制和迁移文件后执行
`ALTER EXTENSION pg_pinyin UPDATE TO '0.0.9'`；不会覆盖在线词典或 stored generated 值。
替换 Rust 动态库后，应从新数据库连接执行升级，避免旧连接继续持有已加载的旧库。

### 独立 SQL Parser

可选的 `sql/word.sql` 使用静态参数化正则聚合处理兼容查询，新模式使用最长
前缀正则和字符串边界查找，避免逐音节遍历整个词表。仍然每次读取实时
`pinyin.pinyin_token`，不增加缓存表或 Rust 依赖，也不改变查询范围、边界和
空查询语义。已有 SQL-only 安装重新加载此文件即可，不需要重建索引：

```bash
psql "$PGURL" -v ON_ERROR_STOP=1 -f sql/word.sql
```

共享 tokenizer、Rust API 和版本保持不变。实现约束、回归要求及分开的解析、
query 构造和端到端 benchmark 见 [SQL parser spec](spec/pinyin-sql-parser-perf.md)。

本机 PG19 beta4 / pg_search 0.26.1 测量使用 2 万文档、每模式 2 千查询、三轮
预热测试。下表是同模式新旧三轮中位数的耗时降幅，不跨模式比较：

| 模式 | Patterns 耗时降低 | 端到端查询耗时降低 |
| --- | ---: | ---: |
| compat | 14.6% | 4.4% |
| full | 17.3% | 11.3% |
| initials | 2.0% | -2.1%（更慢） |
| autocomplete | 14.9% | 18.3% |
| auto | 22.3% | 10.2% |

首字母端到端并未改善：三轮配对分别慢 2.1%、9.5%、13.1%；其解析的小幅差异
和 compat 的端到端结果有波动。共享宿主内存压力较高，这些只是本次观测值，
不保证生产环境收益。新旧 patterns、query、完整命中／分数、Top-K 精确一致，
六个新建索引均为 3,350,528 bytes。客户端计时含逐次 planning，未测 prepared
plan 和并发。详见 [报告](benchmarks/sql_parser.md)、[原始数据](benchmarks/sql_parser.json.gz)
及保留的 [首轮试验](benchmarks/sql_parser_first_pass.md)。

### 原生精确音节查询

普通音节字段可以按需使用 `pinyin_phrase`。它复用 `full` 输入语法，包括
`xi'an` 边界、声调和 ü 归一化；单音节构造精确 `pdb.term`，多音节构造
`pdb.phrase_array`。后者直接使用音节 tokens，不像 `pdb.phrase(text[])`
那样再交给字段的 search analyzer 分词。

```sql
-- Rust 后端，由 CREATE EXTENSION pg_pinyin 安装。
SELECT id, pdb.score(id) FROM voice
WHERE pinyin @@@ public.pinyin_phrase('xi''an')
ORDER BY pdb.score(id) DESC, id;

-- 允许间隔；NULL／0 slope 要求音节相邻。
SELECT id FROM voice WHERE pinyin @@@ public.pinyin_phrase('xi an', 1);

-- 无需 Rust 扩展的独立 SQL 后端，从 sql/word.sql 加载。
SELECT id FROM voice WHERE pinyin @@@ public.sql_pinyin_phrase('xi''an');
```

**评分不兼容，不能静默替换旧接口**：原生单 term 使用 BM25，单 regex 是
常量评分。排序可能改变，尤其单音节查询与其他搜索分支组合时。
原有 regex 接口和 `full` 行为完全保留；首字母、末尾补全、
管道多读音及显式 `max_expansions` 仍用 regex 接口。新接口不接受
generated-pinyin 标志或 regex 展开上限，也不改变索引内容。

SQL NULL 输入返回 NULL，空／无效输入返回 `pdb.empty()`。非 NULL 输入的
负 `slope` 报 `22023`；单 term 忽略非负 slop。两端均为 `STABLE`、
`PARALLEL SAFE`，保留 full 的输入／token 限制，Rust 分词仍不执行 SPI。
Slop 沿用 pg_search phrase 语义：一个 token 间隔成本为 1，交换相邻音节成本
为 2，正值不等于只允许按原顺序增加间隔。
pg_search 0.26.1 对重复 term 加正 slop 时可能复用同一个索引位置：
`an an` 配合 slop 1 能命中只有一个 `an` 的文档，旧 full regex 也有此行为。
必须要求相邻重复音节分别出现时使用零 slop；正 slop 不保证出现次数。
应先启用 pg_search，再安装／升级 pg_pinyin；未启用时安装明确报错的 stub，
已有 pg_search 但缺少原生构造／slop API 时也明确提示升级，不自动退回 regex
改变评分。本实现验证目标为 pg_search 0.26.1。安装新动态库和现有未发布
`0.0.8 → 0.0.9` 迁移后，从新连接升级；不额外 bump version。

## Token 数组与文档批次

未发布的开发版本 0.0.9 对真正的 `text[]` tokenizer 输入直接在 Rust 中读取，移除
原来的 SPI／JSON 中转。旧 `anyelement` 签名、函数 OID 和生成列表达式绑定
不变，其他可转换输入保留旧 fallback。Token 会去除首尾空白、丢弃空项，
纯 ASCII 字母数字 token 转小写；NULL token 仍报错。数组维度和下界不改变
按存储顺序读取 token 的行为。

新 `pinyin_word_romanize_batch` 的每个数组项是一个完整的独立文档，不能当作
单个文档的 token 来拼接。一次显式批次只检查一次在线词典版本，并复用同一份
词典快照；普通调用仍然每次检查版本。缓存标识还包含元数据行的 `xmin`，避免
savepoint 回滚后重用版本号时，误命中先前未提交的词典内容。
后缀词典延续已有的显式缓存清理和基础
词典版本失效规则。批量函数由 Rust 直接导出，声明为 `STABLE`、`STRICT`、
`PARALLEL SAFE`；stored generated 列继续使用原来的 `IMMUTABLE` 标量接口。

```sql
-- 一个文档的 tokens，已有调用也自动使用快路径。
SELECT public.pinyin_word_romanize(ARRAY['吕', '梁', 'ABC']);
-- lv liang abc

-- 三个独立文档，保留 NULL 的位置。
SELECT public.pinyin_word_romanize_batch(ARRAY['吕梁', NULL, '银行']);
-- {"lv liang",NULL,"yin hang"}

-- 显式限制每批数量，与源 ID 对齐后导入。
INSERT INTO imported_voice (id, description, pinyin)
SELECT ids[i], documents[i], outputs[i]
FROM (SELECT ARRAY[1, 2]::bigint[] AS ids,
             ARRAY['吕梁', '银行']::text[] AS documents) AS batch
CROSS JOIN LATERAL public.pinyin_word_romanize_batch(documents) AS converted(outputs)
CROSS JOIN LATERAL generate_subscripts(documents, 1) AS item(i);
```

示例中 `imported_voice.pinyin` 是普通 text 列，不是生成列。应使用有界批次，
不要把整个导入聚合为一个数组。结果是一维、下界为 1 的数组；多维输入按存储
顺序展平。SQL NULL 输入返回 NULL，空数组保持为空，NULL 文档项保持原位置；
空／全 NULL 批次不加载词典。不删减任何词条、拼音输出或索引内容，独立 SQL
后端不变。安装新动态库和迁移后，从新连接执行
`ALTER EXTENSION pg_pinyin UPDATE TO '0.0.9'`。目前最新发布版本仍是 0.0.8，
本次功能合并进现有的 `0.0.8 → 0.0.9` 迁移，不再额外提升版本。

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
- `third_party/phrase-pinyin-data`

初始化：

```bash
git submodule update --init third_party/pinyin-data third_party/phrase-pinyin-data
```

一键生成扩展所需数据：

```bash
./scripts/generate_data.sh
```

输出文件：

- `sql/data/pinyin_mapping.csv`
- `sql/data/pinyin_token.csv`
- `sql/data/pinyin_words.csv`
- `sql/data/manifest.json` 和 `sql/data/reports/*.json`

来源提交、SHA-256 和合并优先级固定在 `scripts/data/sources.lock.json`。
`ü`（含声调和分解形式）保留为 ASCII `v`；直接保留上游音节边界，例如
`西安 -> xi an`、`善感 -> shan gan`。手工修正优先于基础词典，再合并
CC-CEDICT；冲突读音和拒绝记录均生成报告，不再先连接再贪心切分。
缺失或哈希不符的来源会在写入前失败，不会生成空词典，也不读取本机旁边的仓库。
原始 CC-CEDICT 同时保留简繁词形；低优先级补回来源恢复全部已发布词条。
锁定的旧 CSV 只用来检查 key 覆盖，不作为读音来源；`coverage_audit.json`
逐条追溯初版 P0 缺失的 64,091 个词条。新增词条数量不能抵消旧 key 丢失。

参见[导入说明](scripts/data/README.md)、[P0/P1/P2 spec](spec/pinyin-data-and-retrieval.md)
和[数据许可声明](sql/data/NOTICE.md)。代码仍为 MIT；CC-CEDICT 派生词典另有
CC-BY-SA 署名和相同方式共享要求。

```bash
python3 -m unittest discover -s scripts/data/tests
python3 test/test_dictionary_accuracy.py
```

在数据库可读取同一路径的测试容器中，还可运行 SQL-only 导入/回滚保护：
`PGURL="$PGURL" PG_LOADER_TESTS=1 python3 -m unittest discover -s test`。

已有数据库不会自动覆盖在线字典。显式重新加载 CSV（或使用新编译二进制调用
内部 `pinyin__seed_embedded_data()`）前，请备份自定义基础词典。
还需要刷新已有 stored generated column，例如 `UPDATE voice SET description = description`，
并重建表达式索引；仅更新字典表不会改写已存储值。后缀自定义词典独立保留。

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

该命令包含 pgTAP 并行只读回归测试：在独立的
只读事务中确认实际启动 parallel worker，并检查拼音转换、分词输入及后缀词典
查询不会分配事务 ID。扩展内部的只读 SQL 应使用 `Spi::connect` 配合
`client.select`；`Spi::get_one` / `get_one_with_args` 即使执行 `SELECT`，
也会走可写 SPI 路径，与 `PARALLEL SAFE` 声明冲突。

Rust 扩展测试：

```bash
cargo pgrx test pg18 --features pg18
```

PostgreSQL 19 beta 4 通过 `pgrx` 0.19.3 支持：

```bash
cargo pgrx init --pg19=/usr/lib/postgresql/19/bin/pg_config
cargo pgrx install --features pg19 --no-default-features --pg-config /usr/lib/postgresql/19/bin/pg_config
```

完整 Python 集成与真实升级测试需要 psycopg 3、能创建数据库的管理员 `PGURL`，
以及预加载的 pg_search 0.26.1。初始化 pgrx 后，先在隔离的 Cargo target 中
安装旧 catalog，再恢复当前动态库后测试（以下以 PG19 为例）：

```bash
export PG_CONFIG=/usr/lib/postgresql/19/bin/pg_config
bash test/prepare_upgrade.sh v0.0.8
cargo pgrx install --features pg19 --no-default-features --pg-config "$PG_CONFIG"
cp pg_pinyin--*--*.sql "$("$PG_CONFIG" --sharedir)/extension/"
PGURL="$PGURL" PG_LOADER_TESTS=1 TEST_UPGRADE_DSN="$PGURL" python3 -m unittest discover -s test
python3 -m unittest discover -s scripts/data/tests
cargo test --lib --no-default-features --locked
```

数据库测试独立创建并清理临时库。pgTAP 还确认真实 parallel worker 执行
带模式的公开 query API 和 pattern API。

## Docker（通用上游地址）

- `docker/Dockerfile.test-trixie`
- `docker/Dockerfile.test-pg19beta4-trixie`
- `docker/Dockerfile.release-trixie`

默认使用上游地址（不再改写镜像源）：

- 基础镜像：PG18 测试使用 `postgres:18.3-trixie`，PG19 beta 测试使用 `postgres:19beta4-trixie`
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
docker build -f docker/Dockerfile.test-pg19beta4-trixie -t pg_pinyin/test:pg19beta4 .
```

测试镜像固定为 2026-10-13 核验的最新稳定版
[pg_search 0.26.1](https://github.com/paradedb/paradedb/releases/tag/v0.26.1)，该版本没有
PostgreSQL 19 安装包。PG19 beta 4 Dockerfile 从 `v0.26.1` 的固定 commit
`529c77e9dffe4bd86b6b7971c019bb26b8eadbfa` 构建，使用上游已有的 pgrx
0.19.3，并应用限定范围的 `docker/patches/paradedb-0.26.1-pg19beta4.patch`
兼容补丁。这是本项目集成测试和性能测试使用的实验性兼容构建，并非 ParadeDB
官方发布的 PG19 二进制包。

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

### 原生精确查询

`scripts/benchmark_pinyin_phrase.py` 在独立 UUID 数据库、同一个普通字段 BM25
索引上，对比两端的新原生查询与保留的 `full` regex 路径，区分实际 query
对象构造与完整 client 查询；结束后删除本次数据库。安装新动态库后运行：

```bash
python3 scripts/benchmark_pinyin_phrase.py --dsn "$PGADMINURL" \
  --rows 20000 --queries 2000 --repeats 3 \
  --json-output benchmarks/native_phrase.json \
  --markdown-output benchmarks/native_phrase.md
```

计时外核对完整命中集合及 Rust／SQL 一致性，单独记录原生与 regex 的评分／
排序差异，不伪装为完全相同的排名。对比使用同一分词器和动态库，不是 parser
单独提速或索引体积缩减。下方历史报告保留原样。

本次 [报告](benchmarks/native_phrase.md) 和 [原始 JSON](benchmarks/native_phrase.json)
使用 PG19 beta4／pg_search 0.26.1，2 万行、2 千次查询、三轮轮换。
四种方法的计划均确认使用 BM25 score Top-K，排序表达式没有额外类型转换。
Warm 中位数，单位 ms：

| 路径 | 实际 query 构造，2 万输入 | 完整索引查询，2 千输入 | 单次 p95 |
| --- | ---: | ---: | ---: |
| Rust full regex | 49.321 | 366.692 | 0.222 |
| Rust 原生 phrase | 45.005 | 282.677 | 0.181 |
| SQL full regex | 5,560.614 | 1,037.258 | 0.651 |
| SQL 原生 phrase | 5,530.890 | 869.343 | 0.523 |

本轮完整查询耗时分别减少约 22.9%／16.2%，分词器未变，是构造和执行路径的
合并对比。2 千个计时样本均为多音节，完整命中、评分、排序全部一致；独立的
单音节及 OR gold 确认评分／排名会改变，因此新接口仍需主动选择。
共用索引均为 3,350,528 bytes，不代表索引缩减。共享宿主约有 28 GB swap
占用，三轮结果不能证明普遍或统计显著提速。PG19 使用实验性 source-built
兼容镜像，不是官方 PG19 包或重新完成的完整 Docker 构建。
本地通过 26 项 Rust 测试、90 项 Python 集成／协议测试、22 项数据测试及
八个文件的 1,356 项 pgTAP 断言，覆盖真实 0.0.8 升级、SQL-only 和只读并行
worker。Rust 格式／检查通过，重新生成的词典字节完全一致。

### Native 数组与文档批次

独立脚本 `scripts/benchmark_array_batch.py` 在相同的 20,000 个词组上，对比
真正的 P1 优化前提交 `f1094be` 与当前工作代码。两者都是未发布的 0.0.9 开发
快照，不是两个已发布版本。先重编译 P1 快照，再安装当前库；两者使用
相同 Rust 工具链和 release profile。请在测试容器内运行，保证 `pg_config`
和动态库路径对应服务端文件系统：

```bash
export PG_CONFIG=/usr/lib/postgresql/19/bin/pg_config
PG_PINYIN_BUILD_RELEASE=1 bash test/prepare_upgrade.sh f1094beeb69c093085403f5b02d616a4de7adabe
cargo pgrx install --release --features pg19 --no-default-features --pg-config "$PG_CONFIG"
cp pg_pinyin--*--*.sql "$("$PG_CONFIG" --sharedir)/extension/"
python3 scripts/benchmark_array_batch.py --pgurl "$PGADMINURL" --rows 20000 --runs 3
```

分别测转换和全新 heap／主键表的实际 INSERT。Token 数组及批次在计时前准备，
ICU 分词单独测量。Cold 指新的 backend 缓存，不是 OS 冷缓存。脚本拒绝 debug
旧库或不匹配的 release 构建产物，并逐条校验与未改动词典的输出一致。
不包含 BM25 索引维护，也不声称缩小索引。

2026-10-13 的 [报告](benchmarks/p2_array_batch.md) 和
[原始 JSON](benchmarks/p2_array_batch.json) 使用 PG19 beta 4／pg_search 0.26.1，
20,000 个词组、3 轮轮换对比。以下为 warm 中位数，单位 ms：

| 路径 | 转换 | 全新表 INSERT |
| --- | ---: | ---: |
| P1 快照标量 | 300.489 | 321.993 |
| 当前标量 | 258.099 | 296.844 |
| P1 快照 token 数组 | 623.978 | 649.027 |
| Native token 数组 | 261.685 | 277.513 |
| 每批 128 个文档 | 25.482 | 41.774 |
| 每批 512 个文档 | 23.418 | 41.592 |

这次 native 数组转换和 INSERT 分别快约 2.38 倍、2.34 倍；每批 512 个文档
相对旧标量路径，转换快约 12.8 倍、INSERT 快约 7.74 倍，逐条输出一致。
128 和 512 的 INSERT 中位数很接近，批次更大也不一定更快。
共享主机计时前使用 `31946.69M` swap，这些是本次观测，不保证固定收益或通用
最优批次。不含输入准备、客户端传输、BM25 维护，不能直接视为生产导入吞吐。

本地通过 88 项 Python 测试、875 个 pgTAP 断言和 16 项 Rust 测试，覆盖真实
发布版 `0.0.8 → 开发版 0.0.9` 升级、在线缓存修改／回滚、TOAST 和实际只读
parallel worker。词典可复现生成通过，`sql/data` 无变化。

### P1 查询模式（pg_search 0.26.1）

独立的 [P1 报告](benchmarks/p1_query_modes.md) 和
[原始 JSON](benchmarks/p1_query_modes.json) 记录 2026-10-13 的 PG19 beta 4
实验性构建：2 万条词组、每种方法 2 千条查询、3 轮轮换顺序、top-k 10，
每次使用新的 UUID 数据库并在结束后清理。需要 psycopg 3 和管理员 DSN：

```bash
python3 scripts/benchmark_pinyin_query_modes.py --pgurl "$PGADMINURL" \
  --rows 20000 --query-limit 2000 --runs 3 --pg-search-version 0.26.1
```

普通拼音字段的中位耗时（毫秒）。构造器处理全部 2 万条输入；完整查询包含
构造、BM25、客户端往返和结果传输：

| 模式 | Rust warm 构造 | SQL warm 构造 | Rust 完整查询总耗时 | SQL 完整查询总耗时 |
| --- | ---: | ---: | ---: | ---: |
| `compat` | 12.152 | 1,878.998 | 1,378.673 | 1,576.698 |
| `full` | 13.902 | 5,729.506 | 400.313 | 1,167.757 |
| `initials` | 12.593 | 4,251.153 | 2,059.790 | 2,457.391 |
| `autocomplete` | 14.346 | 5,373.237 | 788.271 | 1,440.392 |

同一模式、同一存储格式下，两端的 pattern 数组和排序结果 ID 全部一致。
autocomplete 的每条语料输出还独立核对原始音节位置，防止两端共享错误。
三组相同输入、相同标签的精确边界对照中，compat 出现 4 个可见已标注误匹配，
full 为 0，7 个正例都保留。57 个 pattern 用例和 30 个对抗检索用例属于定向
接口检查，不代表通用检索准确率，也不能证明没有任何误匹配。

full 与 compat 的语义不同，不能把耗时差异当作 parser 算法提速。
管道编码的单读音字段在这里明显更慢：Rust full／initials／autocomplete
总耗时分别为 4,867.482／15,883.333／9,140.413 ms；并非真实多音字性能语料。
三次重建的同一双字段索引均为 3,514,368 bytes；查询模式不减少索引内容。
主机运行时使用约 33.3 GB swap 且有其他任务，不能据此声称稳定性能增益。
JSON 保留原始重复采样、ID、执行计划和源码哈希。下方历史结果仍标记原来的
pg_search 版本与协议，不重标，也不与本次绝对耗时直接比较。

### P0 词典覆盖

P0 数据修正对比使用相同的 20,000 条词组和编译后端，每个版本独立新建数据库：

```bash
python3 scripts/benchmark_dictionary_p0.py --pgurl "$PGADMINURL" --baseline-ref v0.0.8 --rows 20000 --runs 3
```

见 [P0 测量报告](benchmarks/p0_dictionary.md)。定向读音准确率、warm 延迟、
生成文本字节数和实际 BM25/GIN 大小分开报告；合成查询不代表真实用户流量。
下面通用 benchmark 的语料不同，不能直接横向比较耗时。

覆盖修复后的 benchmark 按保留、补回和新增词条分层取样，两版使用完全相同
的输入语料。人工准确率与合成检索一致性分开报告，fixture ID 不再优先占据
最小值。当前测量见报告；此前仅测共有词条的实验没有验证丢失的 64,091 个
词条，已被本次实验替代。脚本需要 psycopg 3（测试镜像已包含）和可建数据库的管理连接。

2026-10-13 在 PG19 beta4 / pg_search 0.25.1 上完成覆盖修复测量
（20,000 条词组、2,000 次查询、三轮交错顺序）：

| 指标 | v0.0.8 数据 | 修正后数据 |
| --- | ---: | ---: |
| 词组 key 数 | 157,702 | 196,188；旧 key 零丢失 |
| 人工定向读音正确数 | 33/55 | 55/55 |
| 人工查询 Rust Recall@10 / MRR | 0.6000 / 0.4606 | 1.0000 / 0.8273 |
| Rust 文本拼音化 warm 中位耗时 | 333.270 ms | 292.435 ms（-12.25%） |
| Rust 完整查询批次中位耗时 | 1,841.729 ms | 1,867.646 ms（+1.41%） |
| BM25 索引字节数 | 3,309,568 | 3,309,568 |

定向回归不代表通用读音准确率。完整检索耗时基本持平，未观察到索引缩小；
生成词级字段字节数增加 0.19%。SQL 精确数组拼音化 warm 耗时增加 22.97%。
旧 SQL 词组／逐字回退路径在该语料上明显加速，与新增词组覆盖避免昂贵逐字回退
相符，并非解析算法变化。主机存在 swap 压力，轮次波动较大，三轮计时不能证明稳定提速。

衡量分词/拼音化、query builder 和完整检索性能：

- `scripts/benchmark_pg18.sh`
- `scripts/benchmark_pg19beta4.sh`（PG19 beta 4 报告文件名 wrapper）

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

通用 shell benchmark 使用 `EXPLAIN (ANALYZE, BUFFERS, MEMORY, SUMMARY)`，包含内存占用。
独立 P0/P1 脚本分别记录客户端耗时和计时外执行计划，绝对耗时不能与该协议直接比较。

默认情况下，`scripts/benchmark_pg18.sh` 会在每次 run 前 drop/create 专用 benchmark database（`BENCHMARK_DATABASE=pg_pinyin_benchmark`），并在每个 benchmark task 前刷新 runtime state。这样可以避免复用旧 extension、SQL helper 定义、BM25 index、bench table 和 Rust 字典缓存。只有在明确想复用 `PGURL` 指向的数据库时，才设置 `BENCHMARK_FRESH_DATABASE=0`。

运行示例：

```bash
ROWS=2000 REGEX_BENCH_ROWS=20000 USER_TABLE_SUFFIX=_bench PGURL=postgres://localhost/postgres ./scripts/benchmark_pg18.sh

# 在 PostgreSQL 19 beta 4 + ParadeDB 0.25.1 容器内
ROWS=20000 REGEX_BENCH_ROWS=20000 USER_TABLE_SUFFIX=_bench PGURL=postgres://localhost/postgres ./scripts/benchmark_pg19beta4.sh
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

### Benchmark Session（PG19 Beta 4）

2026-09-28 在 PostgreSQL 19 beta 4、`pg_search=0.25.1`、已发布的 `pg_pinyin=0.0.7` arm64 DEB（pgrx `0.19.3`）上运行；使用 fresh benchmark database，`ROWS=20000`、`REGEX_BENCH_ROWS=20000`。以下耗时单位为毫秒，越低越好。

PostgreSQL 函数（20,000 行）：

| 场景 | Cold / Cold-ish | Warm | 说明 |
| --- | ---: | ---: | --- |
| SQL 字级 tokenizer | `80726.375` | `2943.466` | 主机内存压力下的 cold 异常值 |
| Rust 字级 tokenizer | `346.601` | `296.710` | warm 时相对 SQL 快 `9.9x` |
| SQL 词级 tokenizer（`pdb.icu` 输入） | `3373.882` | `3340.056` | 与下方 Rust 使用相同 tokenizer 输入 |
| Rust 词级 tokenizer（`pdb.icu` 输入） | `773.751` | `635.176` | warm 时相对 SQL 快 `5.3x` |
| Rust 词级 tokenizer（纯文本输入） | `402.054` | `241.737` | 不含 `pdb.icu` 输入转换的模式 |
| SQL regex token UDF | `1896.137` | `1815.796` | 20,000 个 query |
| Rust regex token UDF | `6.552` | `6.323` | warm 时相对 SQL 快 `287x` |
| SQL `pdb.query` 构造 | - | `1929.006` | `sql_pinyin_regex_phrase(query)` |
| Rust `pdb.query` 构造 | - | `22.151` | `pinyin_regex_phrase(query)` |

独立 token 构造（20,000 个 query，每条路径计时五次；不含 PostgreSQL executor 和 SQL 数组开销）：

| 场景 | 最好 | 中位数 | Checksum |
| --- | ---: | ---: | ---: |
| Rust | `3.489` | `3.533` | `219996` |
| Python | `34.677` | `35.724` | `219996` |

针对 20,000 行 BM25 表的完整 `pg_search` client 查询（20,000 次查询，每条路径各计时三次）：

| 场景 | 最好 | 中位数 | 单次查询最好 | 结果 checksum |
| --- | ---: | ---: | ---: | ---: |
| Python client parse + `text[]` patterns | `11420.440` | `12274.454` | `571.022` us | `13336444` |
| Rust in-Postgres parse | `11549.828` | `12339.904` | `577.491` us | `13336444` |

完整查询中位数只差 `0.5%`，这次运行不能证明任一路径有稳定的端到端优势。Python 三次耗时为 `11420.440`、`12650.059`、`12274.454` ms；Rust 为 `11549.828`、`12339.904`、`13478.456` ms。运行时主机使用约 `27.8 GB` swap，SQL 字级 cold 结果不宜视为代表值。完整原始报告会生成到 `benchmark_pg19beta4_report.txt`，该文件保持 untracked。

## Roadmap

1. 已实现 P0 可复现、保留音节的导入；后续 P1/P2 参见[检索与数据 spec](spec/pinyin-data-and-retrieval.md)。
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

- 字级读音：[mozillazg/pinyin-data](https://github.com/mozillazg/pinyin-data)。
- 保留音节的词组：[mozillazg/phrase-pinyin-data](https://github.com/mozillazg/phrase-pinyin-data) 和 [CC-CEDICT contributors / MDBG](https://www.mdbg.net/chinese/dictionary?page=cc-cedict)，参见[数据声明](sql/data/NOTICE.md)。
- P0 前历史连接拼音来自 [tsroten/dragonmapper](https://github.com/tsroten/dragonmapper/blob/main/src/dragonmapper/data/hanzi_pinyin_words.tsv)，现在只作覆盖基线；补回读音来自锁定的原始 CC-CEDICT 音节，不重新猜分段。
