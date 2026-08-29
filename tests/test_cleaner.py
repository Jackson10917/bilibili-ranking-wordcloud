"""去重、分词、噪声剥离与词频统计的回归测试。

由 tests/test_core.py 按源码模块拆分而来；统一由 pytest 收集运行：python -m pytest tests
"""

from __future__ import annotations

import pytest

from bilibili_ranker.cleaner import TitleAnalyzer, deduplicate_records
from bilibili_ranker.models import VideoRankingRecord, parse_ranking_records
from bilibili_ranker.stopwords import load_stopword_policy


def test_deduplicate_records() -> None:
    # BV 号 base58 大小写敏感，仅按完整字符串精确去重，不依赖 rank 值。
    items = [
        {"bvid": "BV1aa0000000", "title": "t1", "owner": {}, "stat": {}},
        {"bvid": "BV1aa0000000", "title": "t2", "owner": {}, "stat": {}},  # 重复项
        # 大小写不同是不同视频；BV 前缀本身大小写敏感，bv... 不是合法 bvid。
        {"bvid": "BV1Aa0000000", "title": "t3", "owner": {}, "stat": {}},
        {"bvid": "BV1bb0000000", "title": "t4", "owner": {}, "stat": {}},
    ]
    records, _ = parse_ranking_records(items)
    accepted, rejected = deduplicate_records(records)
    assert rejected == 1
    assert [r.bvid for r in accepted] == ["BV1aa0000000", "BV1Aa0000000", "BV1bb0000000"]


def test_accented_latin_tokens() -> None:
    # 重音字母必须与相邻拉丁字母组成同一词元，而不是被拆成单字母碎片。
    analyzer = TitleAnalyzer(load_stopword_policy())
    record = VideoRankingRecord.from_api_item(
        {"bvid": "BV1aa0000000", "title": "déjà vu café"}, rank=1
    )
    assert analyzer.analyze([record]) == {"café": 1, "déjà": 1}


def test_math_symbols_not_merged_into_tokens() -> None:
    # ×(U+00D7)/÷(U+00F7) 是数学符号而非字母，不能与相邻字符拼成词元。
    analyzer = TitleAnalyzer(load_stopword_policy())
    record = VideoRankingRecord.from_api_item(
        {"bvid": "BV1aa0000000", "title": "5×5 魔方 ××× 6÷2"}, rank=1
    )
    assert analyzer.analyze([record]) == {"魔方": 1}


def test_symbols_inside_cjk_and_cyrillic_blocks_dropped() -> None:
    # 日文、西里尔按整块匹配，块内符号（・U+30FB、҂U+0482）不能被当成词元。
    analyzer = TitleAnalyzer(load_stopword_policy())
    record = VideoRankingRecord.from_api_item(
        {"bvid": "BV1aa0000000", "title": "・・ ҂҂ ゲーム"}, rank=1
    )
    assert analyzer.analyze([record]) == {"ゲーム": 1}


def test_extended_latin_and_hangul_jamo_tokens() -> None:
    # ẞ 在 Latin Extended Additional，casefold 后为 strasse，不能被截成 stra。
    # ㅋ 经 NFKC 折叠到 Hangul Jamo 区，正则必须覆盖该区间。
    analyzer = TitleAnalyzer(load_stopword_policy())
    record = VideoRankingRecord.from_api_item(
        {"bvid": "BV1aa0000000", "title": "STRAẞE ㅋㅋㅋ"}, rank=1
    )
    assert analyzer.analyze([record]) == {"strasse": 1, "ᄏᄏᄏ": 1}


def test_apostrophe_stopwords_filtered_whole() -> None:
    # 撇号是词内连接符，否则 ain't 会退化成噪声词 ain、quelqu'un 退化成 quelqu。
    analyzer = TitleAnalyzer(load_stopword_policy())
    record = VideoRankingRecord.from_api_item(
        {"bvid": "BV1aa0000000", "title": "ain't quelqu'un 魔方"}, rank=1
    )
    assert analyzer.analyze([record]) == {"魔方": 1}


def test_link_and_bvid_noise_stripped() -> None:
    # 链接片段和 BV 号不是词：停用词表只能收精确词，覆盖不了域名和随机 BV 号。
    policy = load_stopword_policy()
    analyzer = TitleAnalyzer(policy)
    record = VideoRankingRecord.from_api_item(
        {
            "bvid": "BV1aa0000000",
            "title": "传送门 https://b23.tv/abc www.bilibili.com/video/BV1xx411c7mD BV1yy411c7mD",
        },
        rank=1,
    )
    frequencies = analyzer.analyze([record])
    assert "传送门" in frequencies
    for noise in ("https", "b23.tv", "abc", "www.bilibili.com", "video", "bv1xx411c7md"):
        assert noise not in frequencies, frequencies


def test_schemeless_links_stripped() -> None:
    # B站标题里的链接大多不带协议：「点击 b23.tv/abc 看教程」。只匹配 https?:// 和 www.
    # 会把 b23.tv、bilibili.com、video 当成词元推进词云。
    from bilibili_ranker.cleaner import normalize_title

    assert normalize_title("点击 b23.tv/abc123 看教程") == "点击 看教程"
    assert normalize_title("传送门 bilibili.com/video/BV1xx411c7mD 见简介") == "传送门 见简介"
    # 子域名（m./www./space.）与无路径裸域名同样要剥。
    assert normalize_title("看这里 m.bilibili.com/video/av123 测评") == "看这里 测评"
    assert normalize_title("跳转 b23.tv 即可") == "跳转 即可"
    # 只收 B站自家域名：正常词元里的点号不能被误伤。
    assert normalize_title("版本 3.5 上线 vs. 旧版") == "版本 3.5 上线 vs. 旧版"
    # 查询串直接挂在裸域名后（无路径）同样要整段剥掉，残片 from/tag 会混进词云。
    assert normalize_title("信息 bilibili.com?from=tag 看看") == "信息 看看"
    assert normalize_title("跳 b23.tv?a=1 走") == "跳 走"


def test_foreign_domain_links_stripped() -> None:
    # 台账债务清偿：常见他站域名入显式名单（通配 TLD 会误伤 3.5/vs. 这类正常词元）。
    from bilibili_ranker.cleaner import normalize_title

    assert normalize_title("同步更新 youtube.com/watch?v=1 求关注") == "同步更新 求关注"
    assert normalize_title("微博 weibo.com/xxx 同id") == "微博 同id"
    assert normalize_title("主页 m.weibo.cn/u/123 来撩") == "主页 来撩"
    assert normalize_title("搬运自 youtu.be/abc 说明") == "搬运自 说明"
    # IGNORECASE 对组合出的名单同样生效。
    assert normalize_title("原曲来自 WWW.NICOVIDEO.JP/sm9 注") == "原曲来自 注"
    # 名单是用户可见契约，必须字面独立断言——数据驱动循环遍历的正是被变异的
    # 元组，条目被改时循环会跟着改测，永远杀不掉名单类变异（mutmut 实证）。
    assert normalize_title("看 bilibili.com/video 收藏") == "看 收藏"
    assert normalize_title("跳 b23.tv 领奖") == "跳 领奖"
    assert normalize_title("油管 youtube.com/x 同步") == "油管 同步"
    assert normalize_title("抖音 douyin.com/@name 同款") == "抖音 同款"
    assert normalize_title("小红书 xiaohongshu.com/explore 笔记") == "小红书 笔记"
    assert normalize_title("知乎 zhihu.com/question 答主") == "知乎 答主"
    assert normalize_title("网盘链接 pan.baidu.com/s/1 密码") == "网盘链接 密码"
    assert normalize_title("弹幕站 acfun.cn/v 投喂") == "弹幕站 投喂"
    assert normalize_title("源码 github.com/x/y 提交") == "源码 提交"
    # 断言形态不能带 www./http 前缀，否则会被对应的通用分支掩护而失去杀伤力。
    assert normalize_title("原曲来自 nicovideo.jp/sm9 注") == "原曲来自 注"
    assert normalize_title("同步 tiktok.com/@id 视频") == "同步 视频"
    assert normalize_title("微博移动版 weibo.cn/u/123 同人") == "微博移动版 同人"
    # 名单外长尾域名维持不误伤；出现噪声时往 _NOISY_DOMAINS 加一行即可。
    assert normalize_title("小众站 example.org/about 看看") == "小众站 example.org/about 看看"


def test_noisy_domain_list_is_fully_exercised() -> None:
    # 数据驱动：名单里每一条域名都必须真实生效——新增条目容易出现
    # 「加了名单没加测试」的死条目，这里随名单增长自动全覆盖。
    import bilibili_ranker.cleaner as cleaner_module
    from bilibili_ranker.cleaner import normalize_title

    for domain in cleaner_module._NOISY_DOMAINS:
        title = f"传送 {domain}/abc 说明"
        assert normalize_title(title) == "传送 说明", domain
        # 无路径裸域名同样剥除。
        assert normalize_title(f"跳 {domain} 即可") == "跳 即可", domain


def test_jieba_uses_accurate_mode() -> None:
    # cut_all=True 的全模式会给词频混入大量冗余切分（研究生命起源 → 研究生/研究/…），
    # 精确模式的切分结果是词频统计的契约，必须锁死。
    from bilibili_ranker.cleaner import _jieba_lcut

    assert _jieba_lcut("研究生命起源") == ["研究", "生命", "起源"]


def test_link_stripping_keeps_adjacent_cjk() -> None:
    # 链接主体用 \S 会连紧贴的中日韩文字一起吞掉，整条标题被剥空。
    from bilibili_ranker.cleaner import normalize_title

    assert normalize_title("传送门https://b23.tv/abc教程") == "传送门 教程"
    assert normalize_title("看这里www.bilibili.com/video测评") == "看这里 测评"
    # 纯链接仍要整段剥掉，不能因为收窄字符集而漏出残片。
    assert normalize_title("https://b23.tv/abc?a=1#f") == ""


def test_link_stripping_stops_at_comma_and_semicolon() -> None:
    # 「链接,词」无空格拼接在转载标题里不算罕见：, ; 若混进链接主体，后面的词元会被
    # 连带吞掉且静默无报错。剥完留在原地的 , ; 本就不是词元，分词自然消失。
    from bilibili_ranker.cleaner import normalize_title

    assert normalize_title("https://b23.tv/abc,Minecraft 真好玩") == ",Minecraft 真好玩"
    assert normalize_title("看 https://example.org/a;RTX4090 评测") == "看 ;RTX4090 评测"

    analyzer = TitleAnalyzer(load_stopword_policy())
    record = VideoRankingRecord.from_api_item(
        {"bvid": "BV1aa0000000", "title": "https://b23.tv/abc,Minecraft 真好玩"}, rank=1
    )
    assert analyzer.analyze([record]) == {"minecraft": 1, "真好玩": 1}


def test_bvid_noise_stripped_adjacent_to_cjk() -> None:
    # \b 按 Unicode 词符判界，中文字符也算词字符，紧贴中文的 BV 号永远匹配不上，
    # 噪声 bv1xx411c7md 会整号混进词云。改用 ASCII 边界后相邻汉字必须保留。
    from bilibili_ranker.cleaner import normalize_title

    analyzer = TitleAnalyzer(load_stopword_policy())
    record = VideoRankingRecord.from_api_item(
        {"bvid": "BV1aa0000000", "title": "围观BV1xx411c7mD魔方"}, rank=1
    )
    assert analyzer.analyze([record]) == {"围观": 1, "魔方": 1}
    # 反向约束：作为更长标识符一部分时不能误剥。
    assert normalize_title("xBV1xx411c7mD") == "xBV1xx411c7mD"
    # 两种噪声直接拼接时单趟替换会露出新的可剥片段：BV 右边界被 w 挡住，
    # 随后 www 吃掉域名，留下整个 BV 号。必须反复剥到不再变化。
    assert normalize_title("BV1aa0000000www.bilibili.com") == ""


def test_glued_bvid_stack_dropped() -> None:
    # 两个以上 bvid 无分隔堆叠时噪声剥离管不到（每个 BV 的右边界都被下一个 B 挡住），
    # 会整体残留成垃圾词元。归一化后形状是「bv+10 位字母数字」的重复、长度必为 12
    # 的倍数，自然语言的词没有这种形状，token 层整体丢弃。
    analyzer = TitleAnalyzer(load_stopword_policy())
    record = VideoRankingRecord.from_api_item(
        {"bvid": "BV1aa0000000", "title": "BV1aa0000000BV1bb1111111"}, rank=1
    )
    assert analyzer.analyze([record]) == {}
    # 三连堆叠同样命中。
    record = VideoRankingRecord.from_api_item(
        {"bvid": "BV1aa0000000", "title": "BV1aa0000000BV1bb1111111BV1cc2222222"}, rank=1
    )
    assert analyzer.analyze([record]) == {}
    # 常规分隔的 BV 号仍由噪声剥离处理，词形不受 token 层判定影响。
    record = VideoRankingRecord.from_api_item(
        {"bvid": "BV1aa0000000", "title": "xbv1aa0000000def"}, rank=1
    )
    assert analyzer.analyze([record]) == {"xbv1aa0000000def": 1}


def test_japanese_iteration_mark_kept() -> None:
    # 々(U+3005) 归 CJK Symbols 块，不在统一表意文字区间：漏掉会把「人々」整词丢干净。
    policy = load_stopword_policy()
    analyzer = TitleAnalyzer(policy)
    tokens = analyzer._candidate_tokens("人々 時々 様々")
    for word in ("人々", "時々", "様々"):
        assert word in tokens, tokens


def test_japanese_kanji_word_survives_jieba() -> None:
    # jieba 只有中文词典，「実況」会被切成単字后被最短长度过滤掉；
    # 全单字时保留整块，日文汉字词才不会系统性丢失。
    analyzer = TitleAnalyzer(load_stopword_policy())
    record = VideoRankingRecord.from_api_item(
        {"bvid": "BV1aa0000000", "title": "ゲーム実況"}, rank=1
    )
    frequencies = analyzer.analyze([record])
    assert "実況" in frequencies
    assert "ゲーム" in frequencies


def test_chinese_function_word_runs_not_kept_whole() -> None:
    """全单字回退分支必须只救日语，不能把中文虚词串整块放进词云。

    「他也是」被 jieba 切成三个单字，整块补回会绕过停用词与最短长度过滤。
    语种信号是「块内含 jieba 词典外字符」：中文单字连排全在词典里，一律拒绝；
    日语汉字（転/気/況）不在中文词典，个别字撞上中文停用词（自転車 的「自」、
    本気 的「本」）也仍须保留。
    """

    analyzer = TitleAnalyzer(load_stopword_policy())

    def tokens(title: str) -> dict[str, int]:
        record = VideoRankingRecord.from_api_item({"bvid": "BV1aa0000000", "title": title}, rank=1)
        return analyzer.analyze([record])

    for noise in ("他也是", "我的了", "和你的", "也是的"):
        assert noise not in tokens(noise), noise

    for word in ("自転車", "本気", "実況"):
        assert word in tokens(word), word


def test_single_char_fallback_requires_out_of_dict_char() -> None:
    # 「猫和狗」「吃了吗」被 jieba 全切成单字且都在中文词典里，整块补回会把标题
    # 碎片伪造成一个词，还与 min-len=1 的结果矛盾——min-len=2 的词表必须是它的
    # 子集。回退只对含词典外字符（日文汉字）的块触发；対戦这类字形全在词典的
    # 日语词救不回，是已知上限。
    analyzer = TitleAnalyzer(load_stopword_policy())

    def tokens(title: str) -> dict[str, int]:
        record = VideoRankingRecord.from_api_item({"bvid": "BV1aa0000000", "title": title}, rank=1)
        return analyzer.analyze([record])

    for noise in ("猫和狗", "我把它吃了", "吃了吗"):
        assert noise not in tokens(noise), noise

    # 同一标题在 min-len=1 下给出单字词，两种设置不再互相矛盾。
    single = TitleAnalyzer(load_stopword_policy(), minimum_token_length=1)
    record = VideoRankingRecord.from_api_item({"bvid": "BV1aa0000000", "title": "猫和狗"}, rank=1)
    assert single.analyze([record]) == {"猫": 1, "狗": 1}


def test_missing_jieba_freq_warns_and_drops_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    # dt.FREQ 是私有 API：上游改结构时降级必须可察觉（警告），否则日文汉字词
    # 会被 min-length 静默丢干净，词云少一门语言而无人知道。
    import jieba

    from bilibili_ranker.cleaner import _has_out_of_dict_char

    monkeypatch.setattr(jieba.dt, "FREQ", None)
    with pytest.warns(UserWarning, match="jieba.dt.FREQ"):
        assert _has_out_of_dict_char("転生") is False


def test_email_and_filename_noise_stripped() -> None:
    # 邮箱整体剥除：@ 前后片段（zhang.san、gmail.com）都不是词元形状，逐段剥会漏。
    from bilibili_ranker.cleaner import normalize_title

    assert normalize_title("联系 zhang.san@gmail.com 谢谢") == "联系 谢谢"
    assert normalize_title("邮箱 abc@qq.com 联系我") == "邮箱 联系我"
    # 无 @ 的名单外裸域名维持既有语义，不误伤。
    assert normalize_title("搜 qq.com 一下") == "搜 qq.com 一下"

    # 词元级丢弃：分发文件名（setup.exe）与 CJK 相邻的扩展名碎片（说明.pdf 的 pdf）
    # 都按点号末段判扩展名；裸的 pdf/exe 在词云里不承载话题信息，一并丢弃。
    analyzer = TitleAnalyzer(load_stopword_policy())

    def tokens(title: str) -> dict[str, int]:
        record = VideoRankingRecord.from_api_item({"bvid": "BV1aa0000000", "title": title}, rank=1)
        return analyzer.analyze([record])

    assert tokens("下载 setup.exe 安装") == {"下载": 1, "安装": 1}
    assert "pdf" not in tokens("下载 说明.pdf 教程")
    # 版本号与缩写词不受影响。
    assert tokens("版本 3.5 上线 vs. 旧版") == {"版本": 1, "上线": 1, "vs": 1, "旧版": 1}


def test_user_dictionary_keeps_proper_nouns_whole() -> None:
    # jieba 通用词典没有破晓传说，拆成 破晓+传说 分头进榜；用户词典加载后整词保留。
    # 用不在内置热词表里的专名做对照：cli 会默认加载内置词典，jieba 词典是进程级
    # 全局状态，「加载前不整词」的断言对内置词不可靠。
    import tempfile
    from pathlib import Path

    from bilibili_ranker.cleaner import load_user_dictionary

    analyzer = TitleAnalyzer(load_stopword_policy())
    record = VideoRankingRecord.from_api_item(
        {"bvid": "BV1aa0000000", "title": "破晓传说 攻略"}, rank=1
    )
    assert "破晓传说" not in analyzer.analyze([record])

    with tempfile.TemporaryDirectory() as directory:
        dict_file = Path(directory) / "userdict.txt"
        dict_file.write_text("破晓传说\n", encoding="utf-8")
        load_user_dictionary(dict_file)

    assert "破晓传说" in analyzer.analyze([record])


def test_default_dictionary_keeps_builtin_proper_nouns_whole() -> None:
    # 内置热词表随包分发且由 cli 默认加载：榜单主体的游戏名不被切成 星穹+铁道
    # 两个独立词条（崩坏 直接丢失）。
    from bilibili_ranker.cleaner import load_default_dictionary

    load_default_dictionary()
    analyzer = TitleAnalyzer(load_stopword_policy())
    record = VideoRankingRecord.from_api_item(
        {"bvid": "BV1aa0000000", "title": "崩坏星穹铁道 攻略"}, rank=1
    )
    frequencies = analyzer.analyze([record])
    assert "崩坏星穹铁道" in frequencies and "星穹铁道" not in frequencies, frequencies


def test_domain_prefix_fragments_stripped() -> None:
    # 域名剥除分支原先没有左边界：xbilibili.com 会从中间命中剥成 x、abcyoutube.com
    # 剥成 abc——碎片过 minimum_token_length 直接进词云。
    from bilibili_ranker.cleaner import normalize_title

    assert normalize_title("xbilibili.com 测试") == "测试"
    assert normalize_title("abcyoutube.com/watch 转载") == "转载"
    # 紧贴中文的裸域名照旧剥除（CJK 不挡 lookbehind）。
    assert normalize_title("传送门bilibili.com/video 收藏") == "传送门 收藏"


def test_brand_glued_token_dropped() -> None:
    # bilibililionly 这类品牌粘连词逃过停用词精确匹配（bilibili 已在表、粘连后命中不了），
    # 直接入表。通用「品牌前缀 + 纯拉丁后缀」规则试过并否决：停用词里的 mine 会把
    # minecraft 一并杀掉。
    analyzer = TitleAnalyzer(load_stopword_policy())
    record = VideoRankingRecord.from_api_item(
        {"bvid": "BV1aa0000000", "title": "bilibilionly 独家 活动"}, rank=1
    )
    frequencies = analyzer.analyze([record])
    assert "bilibilionly" not in frequencies, frequencies
    assert "独家" in frequencies and "活动" in frequencies


def test_zero_width_characters_do_not_split_tokens() -> None:
    # B站"防和谐"标题会插零宽空格（U+200B，Cf 类，NFKC 不动它、split() 也不认），
    # 不剔除的话「黑​丝」被劈成两个单字块，双双被 minimum_token_length 丢掉。
    analyzer = TitleAnalyzer(load_stopword_policy())
    record = VideoRankingRecord.from_api_item(
        {"bvid": "BV1aa0000000", "title": "魔\u200b方 caf\u200bé"}, rank=1
    )
    assert analyzer.analyze([record]) == {"魔方": 1, "café": 1}


def test_analyzer_keeps_allowlisted_word() -> None:
    # allowlist 的短路返回分支：分析结果里必须保留 ai 这类保留词。
    analyzer = TitleAnalyzer(load_stopword_policy())
    record = VideoRankingRecord.from_api_item({"bvid": "BV1aa0000000", "title": "AI 教程"}, rank=1)
    assert analyzer.analyze([record]) == {"ai": 1, "教程": 1}


def test_lazy_imports_report_friendly_errors() -> None:
    # jieba / stopwordsiso / wordcloud 缺依赖时都要给出中文安装提示，而不是 ImportError。
    # jieba 的四个入口（分词、两个词典加载、语种信号）共用一个导入函数：
    # CLI 先加载词典再分词，只包 _jieba_lcut 一处的话友好错误在主路径上不可达。
    import sys as _sys
    from unittest.mock import patch

    from bilibili_ranker.cleaner import (
        _has_out_of_dict_char,
        _jieba_lcut,
        load_default_dictionary,
        load_user_dictionary,
    )
    from bilibili_ranker.stopwords import load_stopword_policy
    from bilibili_ranker.wordcloud import render_wordcloud

    with patch.dict(_sys.modules, {"jieba": None}):
        for call in (
            lambda: _jieba_lcut("测试"),
            lambda: load_default_dictionary(),
            lambda: load_user_dictionary("不存在的词典.txt"),
            lambda: _has_out_of_dict_char("転生"),
        ):
            try:
                call()
            except RuntimeError as exc:
                assert str(exc) == "缺少 jieba，请先安装项目依赖"
            else:
                raise AssertionError("缺 jieba 未报友好错误")

    with patch.dict(_sys.modules, {"stopwordsiso": None}):
        try:
            load_stopword_policy()
        except RuntimeError as exc:
            assert str(exc) == "缺少 stopwordsiso，请先安装项目依赖"
        else:
            raise AssertionError("缺 stopwordsiso 未报友好错误")

    with patch.dict(_sys.modules, {"wordcloud": None}):
        try:
            render_wordcloud({"词": 1}, "unused.png")
        except RuntimeError as exc:
            assert str(exc) == "缺少 wordcloud，请先安装项目依赖"
        else:
            raise AssertionError("缺 wordcloud 未报友好错误")


def test_unicode_input_properties() -> None:
    # 属性测试：任意 Unicode 标题（含零宽字符、组合记号、未分配码点）下，
    # 归一化必须幂等、分词与统计不得抛异常；候选层只保证词元非空——未分配
    # 码点（如 U+FADA，CJK 兼容区尾部 Cn 类）会合法出现在候选里；
    # isprintable 断言在过滤后的 analyze 输出上，那才是用户可见面。
    import hypothesis.strategies as st
    from hypothesis import assume, given, settings

    from bilibili_ranker.cleaner import normalize_title

    analyzer = TitleAnalyzer(load_stopword_policy())

    @given(st.text(max_size=60))
    @settings(deadline=None, max_examples=50, database=None)
    def run(title: str) -> None:
        assume(bool(title.strip()))

        once = normalize_title(title)
        assert normalize_title(once) == once

        for token in analyzer._candidate_tokens(title):
            assert token, repr(token)

        record = VideoRankingRecord.from_api_item({"bvid": "BV1aa0000000", "title": title}, rank=1)
        for word, count in analyzer.analyze([record]).items():
            assert word and word.isprintable(), repr(word)
            assert isinstance(count, int) and count > 0

    run()


def test_unassigned_cjk_codepoint_dropped() -> None:
    # hypothesis 反例固化：U+FADA 是 CJK 兼容区尾部的未分配码点（Cn 类），
    # _CJK_RANGE 整段收录使其进入候选层，但字母检查必须把它挡在词频之外。
    analyzer = TitleAnalyzer(load_stopword_policy())
    record = VideoRankingRecord.from_api_item(
        {"bvid": "BV1aa0000000", "title": "\ufada教程"}, rank=1
    )
    assert analyzer.analyze([record]) == {"教程": 1}


def test_minimum_token_length_one_does_not_double_count() -> None:
    # --minimum-token-length 1 时单字本就存活，全单字回退不再整块重复保留：
    # 否则同一处文本计两次（実/況/実況 各一份）。
    analyzer = TitleAnalyzer(load_stopword_policy(), minimum_token_length=1)
    record = VideoRankingRecord.from_api_item(
        {"bvid": "BV1aa0000000", "title": "ゲーム実況"}, rank=1
    )
    frequencies = analyzer.analyze([record])
    assert "実況" not in frequencies
    assert frequencies == {"実": 1, "況": 1, "ゲーム": 1}
