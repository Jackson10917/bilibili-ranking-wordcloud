"""词云渲染的回归测试。

由 tests/test_core.py 按源码模块拆分而来；统一由 pytest 收集运行：python -m pytest tests
"""

from __future__ import annotations

import sys
import tempfile
import types
from pathlib import Path


def test_wordcloud_write_is_atomic() -> None:
    # 渲染失败不能截断已有 PNG，也不能留下临时文件。
    with tempfile.TemporaryDirectory() as directory:
        destination = Path(directory) / "wc.png"
        destination.write_bytes(b"old")

        import bilibili_ranker.wordcloud as wordcloud_module

        class _Boom:
            def __init__(self, **_: object) -> None:
                pass

            def generate_from_frequencies(self, _: dict) -> None:
                pass

            def to_image(self) -> object:
                raise OSError("disk full")

        original = sys.modules.get("wordcloud")
        sys.modules["wordcloud"] = types.SimpleNamespace(WordCloud=_Boom)
        original_font = wordcloud_module.resolve_font_path
        wordcloud_module.resolve_font_path = lambda _=None: Path("fake.ttf")
        try:
            try:
                wordcloud_module.render_wordcloud({"词": 1}, destination)
            except OSError:
                pass
            else:
                raise AssertionError("应该抛出 OSError")
        finally:
            wordcloud_module.resolve_font_path = original_font
            if original is None:
                del sys.modules["wordcloud"]
            else:
                sys.modules["wordcloud"] = original

        assert destination.read_bytes() == b"old"
        leftovers = [p.name for p in Path(directory).iterdir() if p.name.endswith(".tmp")]
        assert leftovers == []


def test_wordcloud_renders_real_png() -> None:
    # 走真实 WordCloud + PIL 保存路径：临时文件扩展名是 .tmp，PIL 推不出格式，
    # 必须显式 format="PNG"，否则整个词云功能 100% 失效。
    import pytest
    from PIL import Image

    from bilibili_ranker.fonts import resolve_font_path
    from bilibili_ranker.wordcloud import render_wordcloud

    try:
        resolve_font_path(None)
    except RuntimeError:
        # 必须 skip 而不是 return：静默通过的话，这条测试要防的"PNG 保存回归"在 CI 上
        # 永远是绿的。CI 已装 fonts-noto-cjk，正常情况下不会走到这里。
        pytest.skip("环境无 CJK 字体")

    with tempfile.TemporaryDirectory() as directory:
        destination = Path(directory) / "wc.png"
        rendered = render_wordcloud({"魔方": 5, "教程": 3}, destination, width=200, height=100)
        assert rendered.exists()
        assert destination.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
        leftovers = [p.name for p in Path(directory).iterdir() if p.name.endswith(".tmp")]
        assert leftovers == []

        # 默认尺寸契约（README：默认 1920×1080）与嵌套输出目录自动创建
        # （两层深：parents=False 的 mkdir 撑不住）。
        default_sized = render_wordcloud(
            {"魔方": 5, "教程": 3}, Path(directory) / "deep" / "nested" / "out.png"
        )
        with Image.open(default_sized) as image:
            assert image.size == (1920, 1080)


def test_wordcloud_render_is_reproducible() -> None:
    # 模块承诺「可复现词云」：random_state 固定后，同输入两次渲染必须逐字节一致。
    import pytest

    from bilibili_ranker.fonts import resolve_font_path
    from bilibili_ranker.wordcloud import render_wordcloud

    try:
        resolve_font_path(None)
    except RuntimeError:
        pytest.skip("环境无 CJK 字体")

    with tempfile.TemporaryDirectory() as directory:
        frequencies = {"魔方": 5, "教程": 3, "入门": 2, "直播": 1}
        # 预热渲染：进程内首次渲染可能带一次性初始化状态，先排掉再比对。
        render_wordcloud(frequencies, Path(directory) / "warmup.png", width=100, height=50)
        first = render_wordcloud(frequencies, Path(directory) / "a.png", width=300, height=150)
        second = render_wordcloud(frequencies, Path(directory) / "b.png", width=300, height=150)
        assert first.read_bytes() == second.read_bytes()


def test_render_wordcloud_direct_guards() -> None:
    # 绕过 CLI 直接调 render_wordcloud 时，空词频与非法尺寸都要在导入前被拦下；
    # 断言自家错误消息：变异若跳过校验，会坠入 wordcloud 库的同型异常而被掩护。
    from bilibili_ranker.wordcloud import render_wordcloud

    for kwargs in (
        {"frequencies": {}, "output_path": "unused.png"},
        {"frequencies": {"词": 1}, "output_path": "unused.png", "width": 0},
        {"frequencies": {"词": 1}, "output_path": "unused.png", "max_words": 0},
    ):
        try:
            render_wordcloud(**kwargs)
        except ValueError as exc:
            assert str(exc) in ("词云尺寸和最大词数必须大于 0", "词频为空，无法生成词云"), exc
        else:
            raise AssertionError(f"非法参数未被拒绝：{kwargs}")
