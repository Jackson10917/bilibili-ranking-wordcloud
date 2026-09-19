"""停用词与保留词策略的回归测试。

由 tests/test_core.py 按源码模块拆分而来；统一由 pytest 收集运行：python -m pytest tests
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from bilibili_ranker.stopwords import load_stopword_policy


def test_stopword_policy() -> None:
    policy = load_stopword_policy()

    # allowlist 词在加载时已从 stopwords 中剔除。
    for word in ("ai", "acg", "asmr", "c++"):
        assert word in policy.allowlist
        assert word not in policy.stopwords

    # 项目自定义停用词仍被移除。
    for word in ("视频", "bilibili", "完整版"):
        assert word in policy.stopwords

    # languages 传字符串应被拒绝，而不是逐字符迭代。
    try:
        load_stopword_policy(languages="zh,en")
    except TypeError:
        pass
    else:
        raise AssertionError("languages 传字符串未抛出 TypeError")


def test_empty_language_list_rejected() -> None:
    # 过滤后为空时 iso_stopwords(()) 返回空集、unsupported 也为空，
    # 静默放行会让全部基础停用词失效，词云满屏虚词。
    # 非字符串语言码即使混在合法码里也要报错：静默丢掉会让停用词表少一门语言而无提示。
    for languages in ([" "], [123], [], ["zh", 123], ["zh", None]):
        try:
            load_stopword_policy(languages=languages)
        except ValueError:
            pass
        else:
            raise AssertionError(f"languages={languages!r} 未被拒绝")


def test_resource_dir_override_loads_custom_words() -> None:
    # README 承诺 --resource-dir 覆盖内置停用词目录，这里锁正向路径：
    # str 与 Path 两种入参都要能加载，自定义词整体替换目录而非追加。
    import shutil

    from bilibili_ranker.stopwords import load_stopword_policy

    source = (
        Path(__file__).resolve().parents[1] / "src" / "bilibili_ranker" / "resources" / "stopwords"
    )
    with tempfile.TemporaryDirectory() as d:
        directory = Path(d)
        for name in ("custom_stopwords.txt", "allowlist.txt"):
            shutil.copy(source / name, directory / name)

        # str 入参（覆盖 isinstance 的 str 分支）加载内置内容。
        policy = load_stopword_policy(str(directory))
        assert "视频" in policy.stopwords
        assert "ai" in policy.allowlist
        assert "ai" not in policy.stopwords

        # 覆盖版 custom_stopwords.txt 整体替换默认词表，allowlist 照常生效。
        (directory / "custom_stopwords.txt").write_text(
            "# 覆盖版\n魔方教程\n", encoding="utf-8-sig"
        )
        policy = load_stopword_policy(directory)
        assert "魔方教程" in policy.stopwords
        assert "视频" not in policy.stopwords
        assert "ai" in policy.allowlist
        assert "ai" not in policy.stopwords
