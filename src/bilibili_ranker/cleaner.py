"""排行榜记录去重，以及标题的分词和词频统计。"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from typing import Iterable

import jieba

from .models import VideoRankingRecord
from .stopwords import StopwordPolicy, normalize_token


_CJK_RANGE = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U000323af"

# 拉丁字母词元包含重音字符（café、déjà），否则会被拆成单字母碎片。
# 区间挖掉 ×(U+00D7) 和 ÷(U+00F7)：它们是数学符号，不是字母（XML NameChar 经典区间）。
_LATIN_LETTERS = r"A-Za-z\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u024f"
_LATIN_ALNUM = r"A-Za-z0-9\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u024f"

_CHUNK_PATTERN = re.compile(
    rf"[{_LATIN_LETTERS}][{_LATIN_ALNUM}]*(?:[._-][{_LATIN_ALNUM}]+)*(?:\+\+|#)?"
    rf"|\d+[{_LATIN_LETTERS}][{_LATIN_ALNUM}]*"
    rf"|[{_CJK_RANGE}]+"
    r"|[\u3040-\u30ff\u31f0-\u31ff]+"
    r"|[\uac00-\ud7af]+"
    r"|[\u0400-\u04ff]+"
    r"|\d+(?:\.\d+)?"
)

_CJK_PATTERN = re.compile(rf"[{_CJK_RANGE}]+")
_NUMBER_PATTERN = re.compile(r"\d+(?:\.\d+)?")


def normalize_title(title: str) -> str:
    normalized = unicodedata.normalize("NFKC", title)
    return " ".join(normalized.split())


def deduplicate_records(
    records: Iterable[VideoRankingRecord],
) -> tuple[list[VideoRankingRecord], int]:
    """按 BV 号确定性去重，保留排名靠前的记录。"""

    accepted: list[VideoRankingRecord] = []
    rejected_count = 0
    seen_bvids: set[str] = set()

    for record in records:
        if record.bvid in seen_bvids:
            rejected_count += 1
            continue
        seen_bvids.add(record.bvid)
        accepted.append(record)

    return accepted, rejected_count


class TitleAnalyzer:
    """按 Unicode 文字片段处理混合语言标题，不给整条标题强行定语言。"""

    def __init__(
        self,
        stopword_policy: StopwordPolicy,
        *,
        minimum_token_length: int = 2,
    ) -> None:
        self._policy = stopword_policy
        self._minimum_token_length = minimum_token_length

    @staticmethod
    def _candidate_tokens(title: str) -> list[str]:
        """提取文字词元；Emoji、标点和其他符号不会被模式匹配。"""

        normalized = normalize_title(title)
        tokens: list[str] = []

        for match in _CHUNK_PATTERN.finditer(normalized):
            chunk = match.group(0)
            if _CJK_PATTERN.fullmatch(chunk):
                tokens.extend(jieba.lcut(chunk, cut_all=False))
            else:
                tokens.append(chunk)
        return tokens

    def _keep_token(self, raw_token: str) -> str | None:
        token = normalize_token(raw_token)
        if not token:
            return None
        if token in self._policy.allowlist:
            return token
        if _NUMBER_PATTERN.fullmatch(token):
            return None
        if len(token) < self._minimum_token_length:
            return None
        if token in self._policy.stopwords:
            return None
        return token

    def analyze(self, records: Iterable[VideoRankingRecord]) -> dict[str, int]:
        words: Counter[str] = Counter()

        for record in records:
            for candidate in self._candidate_tokens(record.title):
                token = self._keep_token(candidate)
                if token:
                    words[token] += 1

        return dict(words.most_common())
