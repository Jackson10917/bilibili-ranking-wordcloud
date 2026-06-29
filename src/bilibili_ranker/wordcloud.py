"""从已经清洗的词频表生成可复现词云。"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from .fonts import resolve_font_path


def render_wordcloud(
    frequencies: Mapping[str, int | float],
    output_path: str | Path,
    *,
    font_path: str | Path | None = None,
    width: int = 1920,
    height: int = 1080,
    max_words: int = 300,
) -> Path:
    if not frequencies:
        raise ValueError("词频为空，无法生成词云")

    try:
        from wordcloud import WordCloud
    except ImportError as exc:
        raise RuntimeError("缺少 wordcloud，请先安装项目依赖") from exc

    resolved_font = resolve_font_path(font_path)
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)

    cloud = WordCloud(
        font_path=str(resolved_font),
        background_color="white",
        width=width,
        height=height,
        max_words=max_words,
        random_state=42,
        collocations=False,
        prefer_horizontal=0.9,
    )
    cloud.generate_from_frequencies(dict(frequencies))
    cloud.to_file(str(destination))
    return destination.resolve()
