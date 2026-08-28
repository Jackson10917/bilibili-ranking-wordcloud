"""排行榜记录去重，以及标题的分词和词频统计。"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Iterable

from .models import VideoRankingRecord
from .stopwords import StopwordPolicy, normalize_token


def _jieba_lcut(text: str) -> list[str]:
    """延迟导入 jieba，缺依赖时给出与 wordcloud/stopwordsiso 一致的友好错误。"""
    try:
        import jieba as _jieba
    except ImportError as exc:
        raise RuntimeError("缺少 jieba，请先安装项目依赖") from exc
    # jieba 首次分词会往 stderr 打 "Building prefix dict ..."，对 CLI 输出是纯噪音。
    # setLogLevel 在导入后立即调用，只在第一次真正使用时执行，避免全局副作用。
    _jieba.setLogLevel("ERROR")
    # list() 同时消掉无存根依赖的 Any 返回值，满足 --strict 的 no-any-return。
    return list(_jieba.lcut(text, cut_all=False))


# U+3005 々 是叠字符（人々、時々、様々），U+3007 〇 是表意数字零，两者 Unicode 都归 CJK
# Symbols 块，不在统一表意文字区间里，漏掉会把「人々」整词切碎后丢干净。
_CJK_RANGE = r"\u3005\u3007\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U000323af"

# 拉丁字母词元包含重音字符（café、déjà），否则会被拆成单字母碎片。
# 区间挖掉 ×(U+00D7) 和 ÷(U+00F7)：它们是数学符号，不是字母（XML NameChar 经典区间）。
# Latin Extended Additional（U+1E00-U+1EFF，含 ẞ 和越南语声调字母）整段都是字母。
_LATIN_LETTERS = r"A-Za-z\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u024f\u1e00-\u1eff"
_LATIN_ALNUM = r"A-Za-z0-9\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u024f\u1e00-\u1eff"

# 撇号是词内连接符：ain't、quelqu'un 必须整体成词，否则停用词表命中不了，
# 反而留下 ain、quelqu 这类噪声。U+2019 不被 NFKC 折叠成 U+0027，两个都要列。
_CHUNK_PATTERN = re.compile(
    rf"[{_LATIN_LETTERS}][{_LATIN_ALNUM}]*(?:['\u2019._-][{_LATIN_ALNUM}]+)*(?:\+\+|#)?"
    rf"|\d+[{_LATIN_LETTERS}][{_LATIN_ALNUM}]*"
    rf"|[{_CJK_RANGE}]+"
    r"|[\u3040-\u30fa\u30fc-\u30ff\u31f0-\u31ff]+"
    # NFKC 会把兼容型谚文字母（ㅋ U+314B）折叠到 Hangul Jamo 区，所以两段都要收。
    r"|[\u1100-\u11ff\uac00-\ud7af]+"
    r"|[\u0400-\u04ff]+"
    r"|\d+(?:\.\d+)?"
)

_CJK_PATTERN = re.compile(rf"[{_CJK_RANGE}]+")
_NUMBER_PATTERN = re.compile(r"\d+(?:\.\d+)?")

# 多个 BV 号无分隔堆叠时每个号的右边界都被下一个 B 挡住、剥噪声管不到；但整串形状是
# 「bv+10 位字母数字」的重复，自然语言的词没有这种形状，整体丢弃不误伤。
_BVID_STACK_PATTERN = re.compile(r"(?:bv[0-9a-z]{10})+", re.IGNORECASE)


# 链接和 BV 号是标识符不是词，停用词表只能收精确词、盖不住域名和随机 BV 号，分词前整段剥掉。
# 主体限定 ASCII 可见字符（RFC 3986 本就是 ASCII）：\S 会把紧贴链接的中日韩文字一起吞掉。
# 可见字符挖掉 , ;（_URL_CHARS）：标题里「链接,词」无空格拼接时，后面的词元会被连带吞掉
# 且不报任何错；留在原地的 , ; 本就不是词元，分词自然消失。, ; 在 URL 里合法但罕见，
# 真遇到会漏出尾巴碎片——词云场景丢词比多一个碎片更难察觉。
# B站标题多是无协议裸链，bilibili.com 与 b23.tv 单列一支、协议和 www. 可省，路径和挂在
# 裸域名后的查询串一并吃掉。边界不用 \b——CJK 也是 \w 词字符，紧贴中文时永不成立——
# 改对 ASCII 字母数字做 lookaround。域名显式名单、不通配 TLD，避免误伤正常词元；
# 名单外的域名出现噪声时往 _NOISY_DOMAINS 加一行即可。
_NOISY_DOMAINS = (
    # B站自家。
    "bilibili.com",
    "b23.tv",
    # 常见他站转发/导流源。
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
    r"|(?<![0-9A-Za-z])BV[0-9A-Za-z]{10}(?![0-9A-Za-z])",
    re.IGNORECASE,
)


def normalize_title(title: str) -> str:
    # NFKC 不动 Cf 类不可见字符（U+200B 零宽空格、U+FEFF、U+00AD 软连字符），它们也不是
    # 空白：「防和谐」标题「黑​丝」会被劈成单字块双双被 minimum_token_length 丢掉，
    # 拉丁词同样被截断，归一化时一并剔除。
    normalized = "".join(
        character
        for character in unicodedata.normalize("NFKC", title)
        if unicodedata.category(character) != "Cf"
    )
    # 剥噪声在剔除零宽字符之后（链接里插了零宽字符时正则同样能命中）。多种噪声直接
    # 拼接时单趟替换会露出新的可剥片段，反复剥到不再变化；每次替换单调缩短，必终止。
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
                # jieba 只有中文词典，日文汉字词（実況、洗濯）会被全切成单字后被
                # minimum_token_length 丢干净，整块补留一份；--minimum-token-length 1
                # 时单字本就能存活，再留整块会同处文本计两次，不回退。中文虚词串
                # （「他也是」）整块会绕过停用词过滤，全部单字都是停用词才判为虚词串
                # 丢弃；日语汉字词只个别字撞中文停用词（自転車的「自」），不受影响。
                # 已知上限：精确切日语需 mecab/UniDic 重依赖，不为边角引入。
                if (
                    self._minimum_token_length > 1
                    and len(chunk) > 1
                    and all(len(piece) == 1 for piece in pieces)
                ):
                    if not all(
                        normalize_token(piece) in self._policy.stopwords for piece in pieces
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
        # 日文、西里尔按整块匹配，块内混着符号（・U+30FB、҂U+0482）。
        # 逐块手挖区间会漏，直接要求词元至少含一个字母。
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
