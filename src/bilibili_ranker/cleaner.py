"""排行榜记录去重，以及标题的分词和词频统计。"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Iterable
from importlib.resources import as_file, files
from pathlib import Path

from .models import VideoRankingRecord
from .stopwords import StopwordPolicy, normalize_token


def _jieba_lcut(text: str) -> list[str]:
    """延迟导入 jieba，缺依赖时给出与 wordcloud/stopwordsiso 一致的友好错误。"""
    try:
        import jieba as _jieba
    except ImportError as exc:
        raise RuntimeError("缺少 jieba，请先安装项目依赖") from exc
    # 首次分词往 stderr 打 "Building prefix dict ..."，压掉；list() 消 Any 满足 no-any-return。
    _jieba.setLogLevel("ERROR")
    return list(_jieba.lcut(text, cut_all=False))


def load_user_dictionary(path: str | Path) -> None:
    """加载 jieba 用户词典，保证专有名词（游戏名、番名）不被切碎；应在首次分词前调用。"""

    import jieba as _jieba

    _jieba.setLogLevel("ERROR")
    _jieba.load_userdict(str(path))


# 内置热词表：收录累计词频里高频且被 jieba 切碎的专名，发现新专名回补一行。
DEFAULT_USER_DICT = (
    files("bilibili_ranker").joinpath("resources").joinpath("dict").joinpath("user_dict.txt")
)


def load_default_dictionary() -> None:
    """加载内置热词表；高频专有名词（游戏名、番名）默认不被切碎。"""

    import jieba as _jieba

    _jieba.setLogLevel("ERROR")
    with as_file(DEFAULT_USER_DICT) as path:
        _jieba.load_userdict(str(path))


def _has_out_of_dict_char(chunk: str) -> bool:
    """块内是否含 jieba 中文词典外的字符——日文汉字（転/気/況）的语种信号。"""

    import jieba as _jieba

    _jieba.initialize()
    # dt.FREQ 是私有 API（依赖钉 jieba<1）；上游改结构时放弃信号、退回不补整块。
    freq: object = getattr(getattr(_jieba, "dt", None), "FREQ", None)
    if not isinstance(freq, dict):
        return False
    return any(freq.get(character, 0) == 0 for character in chunk)


# 々（叠字符）与 〇（表意零）归 CJK Symbols 块，不在表意文字区间，漏掉会切碎「人々」。
_CJK_RANGE = r"\u3005\u3007\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U000323af"

# 含重音字母（café）；挖掉数学符号 × ÷——它们不是字母。
_LATIN_LETTERS = r"A-Za-z\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u024f\u1e00-\u1eff"
_LATIN_ALNUM = r"A-Za-z0-9\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u024f\u1e00-\u1eff"

# 撇号是词内连接符（ain't、quelqu'un 须整体成词才能命中停用词表）；U+2019 不被 NFKC 折叠，两个都列。
_CHUNK_PATTERN = re.compile(
    rf"[{_LATIN_LETTERS}][{_LATIN_ALNUM}]*(?:['\u2019._-][{_LATIN_ALNUM}]+)*(?:\+\+|#)?"
    rf"|\d+[{_LATIN_LETTERS}][{_LATIN_ALNUM}]*"
    rf"|[{_CJK_RANGE}]+"
    r"|[\u3040-\u30fa\u30fc-\u30ff\u31f0-\u31ff]+"
    # NFKC 会把兼容型谚文字母（ㅋ U+314B）折叠到 Hangul Jamo 区，两段都要收。
    r"|[\u1100-\u11ff\uac00-\ud7af]+"
    r"|[\u0400-\u04ff]+"
    r"|\d+(?:\.\d+)?"
)

_CJK_PATTERN = re.compile(rf"[{_CJK_RANGE}]+")
_NUMBER_PATTERN = re.compile(r"\d+(?:\.\d+)?")

_BVID_STACK_PATTERN = re.compile(r"(?:bv[0-9a-z]{10})+", re.IGNORECASE)

_FILE_EXTENSIONS = frozenset({"apk", "exe", "pdf", "zip"})


# 链接、邮箱、BV 号是标识符不是词，分词前整段剥掉；主体限 ASCII（\S 会吞掉紧邻的中日韩文字），
# URL 字符挖掉 , ;（「链接,词」无空格拼接时连带吞掉后面的词元）；边界对 ASCII 做 lookaround
# （\b 对 CJK 永不成立）；域名显式名单不通配 TLD；粘连前缀（xbilibili.com）整段剥掉防碎片。
_NOISY_DOMAINS = (
    "bilibili.com",
    "b23.tv",
    "youtube.com",
    "youtu.be",
    "weibo.com",
    "weibo.cn",
    "douyin.com",
    "xiaohongshu.com",
    "zhihu.com",
    "baidu.com",
    "acfun.cn",
    "github.com",
    "nicovideo.jp",
    "tiktok.com",
)
_NOISY_DOMAIN_PATTERN = "|".join(domain.replace(".", r"\.") for domain in _NOISY_DOMAINS)
_URL_CHARS = r"[!-+\--:<-~]"
_NOISE_PATTERN = re.compile(
    rf"https?://{_URL_CHARS}+"
    rf"|www\.{_URL_CHARS}+"
    rf"|(?:[a-z0-9-]+\.)*(?:{_NOISY_DOMAIN_PATTERN})(?:[/?]{_URL_CHARS}*)?"
    rf"|[a-z0-9-]*(?:{_NOISY_DOMAIN_PATTERN})(?:[/?]{_URL_CHARS}*)?"
    r"|[a-z0-9._%+\-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+"
    r"|(?<![0-9A-Za-z])BV[0-9A-Za-z]{10}(?![0-9A-Za-z])",
    re.IGNORECASE,
)


def normalize_title(title: str) -> str:
    # NFKC 不动 Cf 类不可见字符（零宽空格、软连字符），「防和谐」标题靠它们拆词，一并剔除。
    normalized = "".join(
        character
        for character in unicodedata.normalize("NFKC", title)
        if unicodedata.category(character) != "Cf"
    )
    # 多种噪声直接拼接时单趟替换会露出新的可剥片段，反复剥到不再变化（单调缩短，必终止）。
    while True:
        stripped = _NOISE_PATTERN.sub(" ", normalized)
        if stripped == normalized:
            return " ".join(stripped.split())
        normalized = stripped


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

    def _candidate_tokens(self, title: str) -> list[str]:
        """提取文字词元；Emoji、标点和其他符号不会被模式匹配。"""

        normalized = normalize_title(title)
        tokens: list[str] = []

        for match in _CHUNK_PATTERN.finditer(normalized):
            chunk = match.group(0)
            if _CJK_PATTERN.fullmatch(chunk):
                pieces = _jieba_lcut(chunk)
                tokens.extend(pieces)
                # jieba 无日文词典，日文汉字词全切成单字后被 min-length 丢干净，补留整块；
                # 「全单字」分不出中日文（猫和狗也全在词典），语种信号用 _has_out_of_dict_char。
                if (
                    self._minimum_token_length > 1
                    and len(chunk) > 1
                    and all(len(piece) == 1 for piece in pieces)
                    and _has_out_of_dict_char(chunk)
                ):
                    tokens.append(chunk)
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
        if _BVID_STACK_PATTERN.fullmatch(token):
            return None
        if token.rpartition(".")[2] in _FILE_EXTENSIONS:
            return None
        # 日文、西里尔按整块匹配，块内混着符号（・、҂），要求词元至少含一个字母。
        if not any(character.isalpha() for character in token):
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
