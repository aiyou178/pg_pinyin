import pytest


@pytest.mark.parametrize(
    "_input,_output",
    [
        ("我们", "我们"),
        ("我 们", "我 们"),
        ("我ABC们", "我ABC们"),
        ("我Phone是1376000", "我Phone是1376000"),
        ("", ""),
    ],
)
def test_normalize2text(dbe, _input, _output):
    actual = dbe.execute(f"select normalize2text('{_input}')").scalar()
    assert actual == _output


@pytest.mark.parametrize(
    "_input,_output",
    [
        ("我们", ["我", "们"]),
        ("我 们", ["我", " ", "们"]),
        ("我ABC们123", ["我", "ABC", "们", "123"]),
        ("", [""]),
    ],
)
def test_normalize2array(dbe, _input, _output):
    actual = dbe.execute(f"select normalize2array('{_input}')").scalar()
    assert actual == _output


@pytest.mark.parametrize(
    "_input,_output",
    [
        ("我ABC们123", "|wo| |ABC| |men| |123|"),
        ("重起", "|tong|zhong|chong| |qi|"),
        ("", "||"),
    ],
)
def test_characters2pinyin(dbe, _input, _output):
    actual = dbe.execute(f"select characters2pinyin('{_input}')").scalar()
    assert actual == _output


@pytest.mark.parametrize(
    "func,_output",
    [("normalize2text", None), ("normalize2array", None), ("characters2pinyin", None)],
)
def test_null_input(dbe, func, _output):
    actual = dbe.execute(f"select {func}(null)").scalar()
    assert actual == _output


@pytest.mark.parametrize(
    "_input,is_full,prefix,_output",
    [
        ("zh sh", "false", "false", r"\S*\|zh[^\|]*\|\S* \S*\|sh[^\|]*\|\S*"),
        ("zh sh", "false", "true", r"^\S*\|zh[^\|]*\|\S* \S*\|sh[^\|]*\|\S*"),
        ("zhang sheng", "true", "false", r"\S*\|zhang\|\S* \S*\|sheng\|\S*"),
        ("zhang sheng", "true", "true", r"^\S*\|zhang\|\S* \S*\|sheng\|\S*"),
    ],
)
def test_pinyin_search(dbe, _input, is_full, prefix, _output):
    actual = dbe.execute(
        f"select pinyin_search('{_input}', {is_full}, {prefix})"
    ).scalar()
    assert actual == _output


@pytest.mark.parametrize(
    "_input,prefix,include_zhchsh,_output",
    [
        (
            "wangchongyang",
            "false",
            "false",
            r"\S*\|wang\|\S* \S*\|chong\|\S* \S*\|yang\|\S*",
        ),
        (
            "wchongy",
            "false",
            "false",
            r"\S*\|w[^\|]*\|\S* \S*\|chong\|\S* \S*\|y[^\|]*\|\S*",
        ),
        (
            "wchy",
            "false",
            "true",
            r"\S*\|w[^\|]*\|\S* \S*\|ch[^\|]*\|\S* \S*\|y[^\|]*\|\S*",
        ),
        (
            "wchy",
            "false",
            "false",
            r"\S*\|w[^\|]*\|\S* \S*\|c[^\|]*\|\S* "
            r"\S*\|h[^\|]*\|\S* \S*\|y[^\|]*\|\S*",
        ),
        ("xi''an", "true", "false", r"^\S*\|xi\|\S* \S*\|an\|\S*"),
    ],
)
def test_pinyin_search(dbe, _input, prefix, include_zhchsh, _output):
    actual = dbe.execute(
        f"select pinyin_isearch('{_input}', {prefix}, {include_zhchsh})"
    ).scalar()
    assert actual == _output


def test_real_application(dbe):
    dbe.execute("create temp table names (name text primary key)")
    # 姓名 拼音
    # 郑爽 |zheng| |shuang|
    # 郑世昀 |zheng| |shi| |yun|
    # 赵仕英 |zhao| |shi| |ying|yang|
    # 李Richard
    # 小李子123
    dbe.execute(
        "insert into names values "
        "('郑爽'), ('郑世昀'), ('赵仕英'), ('李Richard'), ('小李子123')"
    )
    dbe.execute(
        "CREATE INDEX names_idx ON names "
        "USING gist (characters2pinyin(name) gist_trgm_ops);"
    )
    # 全文检索
    sql = (
        "select * from names "
        "where characters2pinyin(name) ~* pinyin_search('shuang', true)"
    )
    actual = dbe.execute(sql).fetch_dictlist()
    assert actual == [{"name": "郑爽"}]
    # 首字母
    sql = "select * from names where characters2pinyin(name) ~* pinyin_search('sh y')"
    actual = dbe.execute(sql).fetch_dictlist()
    assert actual == [{"name": "郑世昀"}, {"name": "赵仕英"}]

    # 首字母, 智能
    sql = (
        "select * from names "
        "where characters2pinyin(name) ~* pinyin_isearch('shy', false, true)"
    )
    actual = dbe.execute(sql).fetch_dictlist()
    assert actual == [{"name": "郑世昀"}, {"name": "赵仕英"}]
    # 智能
    sql = (
        "select * from names "
        "where characters2pinyin(name) ~* "
        "pinyin_isearch('shying', false, true)"
    )
    actual = dbe.execute(sql).fetch_dictlist()
    assert actual == [{"name": "赵仕英"}]

    # 多音字
    sql = "select * from names where characters2pinyin(name) ~* pinyin_search('yang')"
    actual = dbe.execute(sql).fetch_dictlist()
    assert actual == [{"name": "赵仕英"}]

    # 英文
    sql = "select * from names where characters2pinyin(name) ilike '%richard%'"
    actual = dbe.execute(sql).fetch_dictlist()
    assert actual == [{"name": "李Richard"}]

    # 数字
    sql = "select * from names where characters2pinyin(name) ~ '23'"
    actual = dbe.execute(sql).fetch_dictlist()
    assert actual == [{"name": "小李子123"}]

    # 中文混合英文
    sql = (
        "select * from names "
        "where characters2pinyin(name) ~* pinyin_search('li Richard')"
    )
    actual = dbe.execute(sql).fetch_dictlist()
    assert actual == [{"name": "李Richard"}]

    # 中文混合数字
    sql = "select * from names where characters2pinyin(name) ~* pinyin_search('zi 123')"
    actual = dbe.execute(sql).fetch_dictlist()
    assert actual == [{"name": "小李子123"}]
