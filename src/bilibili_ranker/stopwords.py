"""多语言停用词、项目补充词和保留词策略。"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from importlib.resources import files

try:
    from importlib.abc import Traversable
except ImportError:  # Python 3.12+ 已从 importlib.abc 移除 Traversable
    from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Iterable


DEFAULT_LANGUAGES = ("zh", "en", "ja", "ko", "fr", "de", "es", "ru")


def normalize_token(token: str) -> str:
    normalized = unicodedata.normalize("NFKC", token).strip()
    return normalized.casefold()


def _read_word_file(path: Path | Traversable) -> set[str]:
    if not path.is_file():
        raise FileNotFoundError(f"停用词资源不存在：{path}")

    words: set[str] = set()
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        words.add(normalize_token(stripped))
    return words


def default_resource_dir() -> Traversable:
    return files("bilibili_ranker").joinpath("resources", "stopwords")


@dataclass(frozen=True, slots=True)
class StopwordPolicy:
    stopwords: frozenset[str]
    allowlist: frozenset[str]


def load_stopword_policy(
    resource_dir: str | Path | Traversable | None = None,
    *,
    languages: Iterable[str] = DEFAULT_LANGUAGES,
) -> StopwordPolicy:
    """加载 MIT 许可的 stopwordsiso，并叠加本项目规则。"""

    try:
        from stopwordsiso import has_lang, stopwords as iso_stopwords
    except ImportError as exc:
        raise RuntimeError("缺少 stopwordsiso，请先安装项目依赖") from exc

    if isinstance(languages, str):
        raise TypeError("languages 必须是语言代码的可迭代对象，不能是字符串")

    language_codes = tuple(dict.fromkeys(code.strip() for code in languages if code.strip()))
    unsupported = [code for code in language_codes if not has_lang(code)]
    if unsupported:
        raise ValueError(f"stopwordsiso 不支持这些语言：{', '.join(unsupported)}")

    directory = Path(resource_dir) if isinstance(resource_dir, (str, Path)) else resource_dir
    directory = directory or default_resource_dir()
    custom = _read_word_file(directory.joinpath("custom_stopwords.txt"))
    allowlist = _read_word_file(directory.joinpath("allowlist.txt"))
    base = {normalize_token(word) for word in iso_stopwords(language_codes)}
    effective = (base | custom) - allowlist

    return StopwordPolicy(
        stopwords=frozenset(effective),
        allowlist=frozenset(allowlist),
    )
