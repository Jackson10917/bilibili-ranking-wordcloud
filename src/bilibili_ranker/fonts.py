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
# 只看后缀的话，改名成 .ttf 的文本文件要拖到 PIL 才炸成 "cannot open resource"，
# 用户根本判断不出是自己指定的字体损坏。
_SFNT_MAGICS = (b"\x00\x01\x00\x00", b"true", b"ttcf", b"OTTO", b"typ1")


def _validate_font_file(path: str | Path, *, source: str) -> Path:
    resolved = Path(path).expanduser()
    if not resolved.is_file():
        raise FontNotFoundError(f"{source} 指定的字体不存在：{resolved}")
    if resolved.suffix.casefold() not in {".ttf", ".ttc", ".otf"}:
        raise FontNotFoundError(f"{source} 不是受支持的字体文件：{resolved}")
    # ponytail: 只校验容器魔数，字形表损坏仍要等 PIL 报错（错误信息已有字体路径）；
    # 用户频繁遇到渲染期字体报错时，再引入 fontTools 做深度校验。
    try:
        with resolved.open("rb") as stream:
            header = stream.read(4)
    except OSError as exc:
        raise FontNotFoundError(f"{source} 指定的字体无法读取：{resolved}（{exc}）") from exc
    if header not in _SFNT_MAGICS:
        raise FontNotFoundError(f"{source} 指定的字体文件已损坏或不是字体：{resolved}")
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

    for root in _standard_font_roots():
        if not root.is_dir():
            continue
        for filename in _CANDIDATE_FILES:
            direct = root / filename
            if direct.is_file():
                return direct.resolve()
        try:
            matches: dict[str, Path] = {}
            for path in root.rglob("*"):
                # 同名字体取排序最小的路径：rglob 顺序依赖文件系统，不排序两台机器可能选到
                # 不同字体，词云就不可复现（渲染另有 random_state=42）。
                if path.name in _CANDIDATE_FILES and path.is_file():
                    existing = matches.get(path.name)
                    if existing is None or str(path) < str(existing):
                        matches[path.name] = path
        except OSError:
            continue
        for filename in _CANDIDATE_FILES:
            if filename in matches:
                return matches[filename].resolve()

    fontconfig_font = _fontconfig_match()
    if fontconfig_font:
        return fontconfig_font

    raise FontNotFoundError(
        "没有找到中日韩字体。请安装 Noto Sans CJK 或 Source Han Sans，"
        "然后使用 --font-path，或设置 BILIBILI_WORDCLOUD_FONT。"
    )
