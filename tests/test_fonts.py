"""跨平台字体查找的回归测试。

由 tests/test_core.py 按源码模块拆分而来；统一由 pytest 收集运行：python -m pytest tests
"""

from __future__ import annotations

import tempfile
from pathlib import Path


def test_font_explicit_path_validated() -> None:
    # 显式路径不存在时应抛 FontNotFoundError，而不是 FileNotFoundError 或 AttributeError。
    import tempfile

    from bilibili_ranker.fonts import FontNotFoundError, resolve_font_path

    with tempfile.TemporaryDirectory() as d:
        # 后缀不对
        bad_ext = Path(d) / "font.bmp"
        bad_ext.write_bytes(b"")
        try:
            resolve_font_path(bad_ext)
        except FontNotFoundError:
            pass
        else:
            raise AssertionError("不受支持的后缀未抛出 FontNotFoundError")

        # 文件不存在
        try:
            resolve_font_path(Path(d) / "nonexistent.ttf")
        except FontNotFoundError:
            pass
        else:
            raise AssertionError("不存在的路径未抛出 FontNotFoundError")


def test_font_env_var_overrides_default() -> None:
    # BILIBILI_WORDCLOUD_FONT 指向有效字体时应返回该路径；无效路径应抛 FontNotFoundError。
    import os
    import tempfile

    import pytest

    from bilibili_ranker.fonts import FontNotFoundError, resolve_font_path

    # 深度校验走 PIL 试载，有效样例直接用系统里已发现的 CJK 字体。
    try:
        valid = resolve_font_path(None)
    except RuntimeError:
        pytest.skip("环境无 CJK 字体")

    with tempfile.TemporaryDirectory() as d:
        old = os.environ.get("BILIBILI_WORDCLOUD_FONT")
        try:
            os.environ["BILIBILI_WORDCLOUD_FONT"] = str(valid)
            result = resolve_font_path()
            assert result == valid.resolve()

            os.environ["BILIBILI_WORDCLOUD_FONT"] = str(Path(d) / "missing.ttf")
            try:
                resolve_font_path()
            except FontNotFoundError:
                pass
            else:
                raise AssertionError("无效环境变量路径未抛出 FontNotFoundError")
        finally:
            if old is None:
                os.environ.pop("BILIBILI_WORDCLOUD_FONT", None)
            else:
                os.environ["BILIBILI_WORDCLOUD_FONT"] = old


def test_corrupt_font_rejected_before_pil() -> None:
    # 内容是垃圾的 fake.ttf 必须在校验阶段就报"损坏"，而不是拖到 PIL 抛
    # "cannot open resource"，那时用户判断不出是自己指定的字体有问题。
    import pytest

    from bilibili_ranker.fonts import FontNotFoundError, resolve_font_path

    with tempfile.TemporaryDirectory() as d:
        junk = Path(d) / "fake.ttf"
        junk.write_bytes(b"dummy")
        try:
            resolve_font_path(junk)
        except FontNotFoundError as exc:
            assert "损坏" in str(exc)
        else:
            raise AssertionError("垃圾内容的字体未被拒绝")

        # 魔数对、字形表是垃圾的文件由 PIL 试载拦截——渲染走的就是同一条加载路径。
        fake = Path(d) / "magic_only.ttf"
        fake.write_bytes(b"\x00\x01\x00\x00garbage-glyph-table")
        try:
            resolve_font_path(fake)
        except FontNotFoundError as exc:
            assert "无法加载" in str(exc)
        else:
            raise AssertionError("魔数合法的损坏字体未被拒绝")

        # 真实字体不能被误伤：能过校验的必然也能被渲染端加载。
        try:
            real_font = resolve_font_path(None)
        except RuntimeError:
            pytest.skip("环境无 CJK 字体")
        assert resolve_font_path(real_font) == real_font.resolve()


def test_fontconfig_match_verifies_family() -> None:
    # fc-match 找不到 family 时会静默回退到默认字体，必须比对返回的 family 名，
    # 否则 Linux 上可能拿到一个不含中日韩字形的字体。
    import subprocess
    from unittest.mock import patch

    from bilibili_ranker import fonts as fonts_module

    with tempfile.TemporaryDirectory() as directory:
        font = Path(directory) / "wqy-zenhei.ttc"
        font.write_bytes(b"ttcf")

        def fake_run(argv: tuple[str, ...], **_: object) -> subprocess.CompletedProcess:
            family = argv[-1]
            # 只有最后一个 family 精确命中，前面的都回退到 DejaVu Sans。
            if family == "WenQuanYi Zen Hei":
                stdout = f"{font}\nWenQuanYi Zen Hei,文泉驿正黑\n"
            else:
                stdout = f"{font}\nDejaVu Sans\n"
            return subprocess.CompletedProcess(argv, 0, stdout, "")

        with patch.object(fonts_module.shutil, "which", lambda _: "fc-match"):
            with patch.object(fonts_module.subprocess, "run", fake_run):
                assert fonts_module._fontconfig_match() == font.resolve()

                # 所有 family 都回退时必须返回 None，而不是交出错字体。
                def always_fallback(argv: tuple[str, ...], **_: object):
                    return subprocess.CompletedProcess(argv, 0, f"{font}\nDejaVu Sans\n", "")

                with patch.object(fonts_module.subprocess, "run", always_fallback):
                    assert fonts_module._fontconfig_match() is None


def test_standard_font_roots_include_windows_user_dir() -> None:
    # 「为我安装」的字体只在用户目录，漏掉这条路径 Windows 上会误报找不到字体。
    import os
    from unittest.mock import patch

    from bilibili_ranker.fonts import _standard_font_roots

    with patch.dict(os.environ, {"WINDIR": r"C:\Windows", "LOCALAPPDATA": r"C:\Users\u\AppData"}):
        roots = [str(path) for path in _standard_font_roots()]
    assert any(root.endswith(os.path.join("Windows", "Fonts")) for root in roots)
    assert any("Microsoft" in root and root.endswith("Fonts") for root in roots)


def test_font_not_found_when_system_has_none() -> None:
    # 标准目录与 fontconfig 全部落空时，必须抛带指引的 FontNotFoundError 而非静默返回。
    from unittest.mock import patch

    from bilibili_ranker import fonts as fonts_module
    from bilibili_ranker.fonts import FontNotFoundError, resolve_font_path

    with patch.object(fonts_module, "_standard_font_roots", lambda: iter(())):
        with patch.object(fonts_module.shutil, "which", lambda _: None):
            try:
                resolve_font_path(None)
            except FontNotFoundError as exc:
                assert "--font-path" in str(exc)
            else:
                raise AssertionError("无字体环境未抛出 FontNotFoundError")


def test_font_discovered_by_direct_filename() -> None:
    # 顶层直查按候选名单顺序返回：NotoSansCJKsc 在名单里排在 msyh 之前，两者同时存在时
    # 必须返回 Noto。目录顶层没有的字体不再递归找（嵌套安装交给 fontconfig），直接落空时
    # 抛带指引的 FontNotFoundError 而不是崩溃。
    import os
    from unittest.mock import patch

    from bilibili_ranker import fonts as fonts_module
    from bilibili_ranker.fonts import FontNotFoundError, resolve_font_path

    old = os.environ.get("BILIBILI_WORDCLOUD_FONT")
    try:
        os.environ.pop("BILIBILI_WORDCLOUD_FONT", None)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            noto = root / "NotoSansCJKsc-Regular.otf"
            noto.write_bytes(b"\x00\x01\x00\x00")
            (root / "msyh.ttc").write_bytes(b"ttcf")

            with (
                patch.object(fonts_module, "_standard_font_roots", lambda: iter((root,))),
                patch.object(fonts_module.shutil, "which", lambda _: None),
            ):
                assert resolve_font_path(None) == noto.resolve()

        # 只有嵌套在子目录里的候选：顶层直查不命中，fontconfig 也没有 → 可降级失败。
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nested = root / "opentype" / "noto" / "NotoSansCJK-Regular.ttc"
            nested.parent.mkdir(parents=True)
            nested.write_bytes(b"ttcf")

            with (
                patch.object(fonts_module, "_standard_font_roots", lambda: iter((root,))),
                patch.object(fonts_module.shutil, "which", lambda _: None),
            ):
                try:
                    resolve_font_path(None)
                except FontNotFoundError as exc:
                    assert "--font-path" in str(exc)
                else:
                    raise AssertionError("嵌套字体不应被顶层直查发现")
    finally:
        if old is not None:
            os.environ["BILIBILI_WORDCLOUD_FONT"] = old


def test_fontconfig_skips_unusable_results() -> None:
    # fc-match 子进程崩溃/超时、返回的字体文件不存在时必须跳过该 family继续找，
    # 不能让 OSError 逸出成崩溃，也不能把不存在的路径交出去。
    import subprocess
    from unittest.mock import patch

    from bilibili_ranker import fonts as fonts_module

    def raise_os_error(*_: object, **__: object) -> object:
        raise OSError("fc-match 不可执行")

    def raise_timeout(*_: object, **__: object) -> object:
        raise subprocess.TimeoutExpired(cmd="fc-match", timeout=3)

    with patch.object(fonts_module.shutil, "which", lambda _: "fc-match"):
        with patch.object(fonts_module.subprocess, "run", raise_os_error):
            assert fonts_module._fontconfig_match() is None
        with patch.object(fonts_module.subprocess, "run", raise_timeout):
            assert fonts_module._fontconfig_match() is None

        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.ttc"

            def nonexistent_file(argv: tuple[str, ...], **_: object) -> subprocess.CompletedProcess:
                return subprocess.CompletedProcess(argv, 0, f"{missing}\n{argv[-1]}\n", "")

            with patch.object(fonts_module.subprocess, "run", nonexistent_file):
                assert fonts_module._fontconfig_match() is None
