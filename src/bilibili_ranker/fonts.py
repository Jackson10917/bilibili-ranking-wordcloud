"""为 WordCloud 在 Windows、macOS 和 Linux 上定位中文字体。"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Iterable


class FontNotFoundError(RuntimeError):
    """找不到可用的中日韩字体文件。"""


_CANDIDATE_FILES = (
    "NotoSansCJKsc-Regular.otf",
    "NotoSansCJK-Regular.ttc",
    "NotoSansSC-Regular.otf",
    "SourceHanSansSC-Regular.otf",
    "SourceHanSansCN-Regular.otf",
    "SourceHanSansSC-VF.ttf",
    "msyh.ttc",
    "msyhbd.ttc",
    "simhei.ttf",
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

    yield Path("/System/Library/Fonts")
    yield Path("/Library/Fonts")
    yield Path.home() / "Library/Fonts"
    yield Path("/usr/share/fonts")
    yield Path("/usr/local/share/fonts")
    yield Path.home() / ".local/share/fonts"
    yield Path.home() / ".fonts"


def _validate_font_file(path: str | Path, *, source: str) -> Path:
    resolved = Path(path).expanduser()
    if not resolved.is_file():
        raise FontNotFoundError(f"{source} 指定的字体不存在：{resolved}")
    if resolved.suffix.casefold() not in {".ttf", ".ttc", ".otf"}:
        raise FontNotFoundError(f"{source} 不是受支持的字体文件：{resolved}")
    return resolved.resolve()


def _fontconfig_match() -> Path | None:
    executable = shutil.which("fc-match")
    if not executable:
        return None

    for family in _FONTCONFIG_FAMILIES:
        try:
            result = subprocess.run(
                (executable, "-f", "%{file}\n", family),
                check=False,
                capture_output=True,
                text=True,
                timeout=3,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        candidate = Path(result.stdout.strip().splitlines()[0]) if result.stdout.strip() else None
        if candidate and candidate.is_file():
            return candidate.resolve()
    return None


def resolve_font_path(explicit: str | Path | None = None) -> Path:
    """依次检查显式参数、环境变量、系统标准目录和 fontconfig。"""

    if explicit:
        return _validate_font_file(explicit, source="--font-path")

    configured = os.environ.get("BILIBILI_WORDCLOUD_FONT")
    if configured:
        return _validate_font_file(configured, source="BILIBILI_WORDCLOUD_FONT")

    for root in _standard_font_roots():
        if not root.is_dir():
            continue
        for filename in _CANDIDATE_FILES:
            direct = root / filename
            if direct.is_file():
                return direct.resolve()
        for filename in _CANDIDATE_FILES:
            try:
                match = next(root.rglob(filename), None)
            except OSError:
                match = None
            if match and match.is_file():
                return match.resolve()

    fontconfig_font = _fontconfig_match()
    if fontconfig_font:
        return fontconfig_font

    raise FontNotFoundError(
        "没有找到中日韩字体。请安装 Noto Sans CJK 或 Source Han Sans，"
        "然后使用 --font-path，或设置 BILIBILI_WORDCLOUD_FONT。"
    )
