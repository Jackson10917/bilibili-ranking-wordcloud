"""包级元数据测试。"""

from __future__ import annotations


def test_pytyped_marker_is_shipped() -> None:
    # PEP 561 类型标记：没有它，下游 import bilibili_ranker 拿不到任何类型注解。
    # 空文件容易被当成垃圾清掉，用测试钉住；能否进 wheel 由 pyproject 的 package-data 保证。
    import importlib.resources

    assert (importlib.resources.files("bilibili_ranker") / "py.typed").is_file()
