"""为 WordCloud 在 Windows、macOS 和 Linux 上定位中文字体。"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterable
from pathlib import Path


class FontNotFoundError(RuntimeError):
    """找不到可用的中日韩字体文件。"""


_CANDIDATE_FILES = (
    "NotoSansCJKsc-Regular.otf",
    "NotoSansCJKsc-Medium.otf",
    "NotoSansCJK-Regular.ttc",
    "NotoSansSC-Regular.otf",
    "NotoSansSC-Medium.otf",
    "SourceHanSansSC-Regular.otf",
    "SourceHanSansCN-Regular.otf",
    "SourceHanSansSC-VF.ttf",
    "msyh.ttc",
    "msyh.ttf",
    "msyhbd.ttc",
    "simhei.ttf",
    "simsun.ttc",
    "PingFang.ttc",
    "STHeiti Medium.ttc",
    "wqy-zenhei.ttc",
)

_FONTCONFIG_FAMILIES = (
    "Noto Sans CJK SC",
    "Noto Sans SC",
    "Source Han Sans SC",
    "WenQuanYi Zen Hei",
)


def _standard_font_roots() -> Iterable[Path]:
    windows_dir = os.environ.get("WINDIR")
    if windows_dir:
        yield Path(windows_dir) / "Fonts"

    # Windows 字体右键「为我安装」只写用户目录，全局 Fonts 里看不到。
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        yield Path(local_appdata) / "Microsoft" / "Windows" / "Fonts"

    yield Path("/System/Library/Fonts")
    yield Path("/Library/Fonts")
    yield Path.home() / "Library/Fonts"
    yield Path("/usr/share/fonts")
    yield Path("/usr/local/share/fonts")
    yield Path.home() / ".local/share/fonts"
    yield Path.home() / ".fonts"


# sfnt 容器的魔数：TrueType(00 01 00 00 / true)、TrueType Collection(ttcf)、OpenType CFF(OTTO)。
# 只看后缀会放过改名成 .ttf 的文本文件，拖到渲染期才炸。
_SFNT_MAGICS = (b"\x00\x01\x00\x00", b"true", b"ttcf", b"OTTO", b"typ1")


def _validate_font_file(path: str | Path, *, source: str) -> Path:
    resolved = Path(path).expanduser()
    if not resolved.is_file():
        raise FontNotFoundError(f"{source} 指定的字体不存在：{resolved}")
    if resolved.suffix.casefold() not in {".ttf", ".ttc", ".otf"}:
        raise FontNotFoundError(f"{source} 不是受支持的字体文件：{resolved}")
    # 深度校验用 PIL 试载：与渲染走同一条 ImageFont.truetype 路径，能过校验就一定能渲染。
    try:
        with resolved.open("rb") as stream:
            header = stream.read(4)
    except OSError as exc:
        raise FontNotFoundError(f"{source} 指定的字体无法读取：{resolved}（{exc}）") from exc
    if header not in _SFNT_MAGICS:
        raise FontNotFoundError(f"{source} 指定的字体文件已损坏或不是字体：{resolved}")
    # 魔数对但字形表损坏的文件在这里拦截，错误信息带字体路径。
    try:
        from PIL import ImageFont

        ImageFont.truetype(str(resolved))
    except ImportError as exc:
        raise RuntimeError("缺少 Pillow，请先安装项目依赖") from exc
    except (OSError, ValueError) as exc:
        raise FontNotFoundError(
            f"{source} 指定的字体文件已损坏，无法加载：{resolved}（{exc}）"
        ) from exc
    return resolved.resolve()


def _fontconfig_match() -> Path | None:
    executable = shutil.which("fc-match")
    if not executable:
        return None

    for family in _FONTCONFIG_FAMILIES:
        try:
            result = subprocess.run(
                (executable, "-f", "%{file}\n%{family}\n", family),
                check=False,
                capture_output=True,
                text=True,
                timeout=3,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        lines = result.stdout.strip().splitlines()
        if result.returncode != 0 or len(lines) < 2 or not lines[0]:
            continue
        candidate = Path(lines[0])
        if not candidate.is_file():
            continue
        # fc-match 找不到指定 family 时会静默回退到默认字体，所以要比对返回的 family 名。
        # %{family} 会输出该字体的全部别名（如 "Microsoft YaHei,微软雅黑"），逐个比对。
        names = {name.strip().casefold() for name in lines[1].split(",")}
        if family.casefold() not in names:
            continue
        return candidate.resolve()
    return None


def resolve_font_path(explicit: str | Path | None = None) -> Path:
    """依次检查显式参数、环境变量、系统标准目录和 fontconfig。"""

    if explicit:
        return _validate_font_file(explicit, source="--font-path")

    configured = os.environ.get("BILIBILI_WORDCLOUD_FONT")
    if configured:
        return _validate_font_file(configured, source="BILIBILI_WORDCLOUD_FONT")

    # 只按候选文件名在目录顶层命中，不递归子目录（家目录可能是网络盘，整树遍历会卡住）；
    # 嵌套安装的字体（Debian 的 opentype/noto 等）由 fontconfig 兜底，两者皆无时用 --font-path。
    for root in _standard_font_roots():
        if not root.is_dir():
            continue
        for filename in _CANDIDATE_FILES:
            direct = root / filename
            if direct.is_file():
                return direct.resolve()

    fontconfig_font = _fontconfig_match()
    if fontconfig_font:
        return fontconfig_font

    raise FontNotFoundError(
        "没有找到中日韩字体。请安装 Noto Sans CJK 或 Source Han Sans，"
        "然后使用 --font-path，或设置 BILIBILI_WORDCLOUD_FONT。"
    )
