"""回归检查：去重、停用词、分词、排行榜请求重试与 CSV 原子写入。

需要已安装项目依赖（jieba、stopwordsiso）。可直接运行：
python tests/test_core.py
或由 pytest 收集。
"""

from __future__ import annotations

import csv
import json
import sys
import tempfile
import types
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bilibili_ranker.cleaner import TitleAnalyzer, deduplicate_records
from bilibili_ranker.cli import main
from bilibili_ranker.models import VideoRankingRecord, parse_ranking_records
from bilibili_ranker.stopwords import load_stopword_policy
from bilibili_ranker.storage import write_records_csv


def test_stopword_policy() -> None:
    policy = load_stopword_policy()

    # allowlist 词在加载时已从 stopwords 中剔除。
    for word in ("ai", "acg", "asmr", "c++"):
        assert word in policy.allowlist
        assert word not in policy.stopwords

    # 项目自定义停用词仍被移除。
    for word in ("视频", "bilibili", "完整版"):
        assert word in policy.stopwords

    # languages 传字符串应被拒绝，而不是逐字符迭代。
    try:
        load_stopword_policy(languages="zh,en")
    except TypeError:
        pass
    else:
        raise AssertionError("languages 传字符串未抛出 TypeError")


def test_deduplicate_records() -> None:
    # BV 号 base58 大小写敏感，仅按完整字符串精确去重，不依赖 rank 值。
    items = [
        {"bvid": "BV1aa", "title": "t1", "owner": {}, "stat": {}},
        {"bvid": "BV1aa", "title": "t2", "owner": {}, "stat": {}},  # 重复项
        {"bvid": "bv1aa", "title": "t3", "owner": {}, "stat": {}},  # 大小写不同，不同视频
        {"bvid": "BV1bb", "title": "t4", "owner": {}, "stat": {}},
    ]
    records, _ = parse_ranking_records(items)
    accepted, rejected = deduplicate_records(records)
    assert rejected == 1
    assert [r.bvid for r in accepted] == ["BV1aa", "bv1aa", "BV1bb"]


def test_parse_ranking_records_tolerance() -> None:
    items = [
        {"bvid": "BV1aa", "title": "t1", "owner": {}, "stat": {}},
        {"bvid": "", "title": "t2", "owner": {}, "stat": {}},  # 缺 bvid，拒绝
        {"bvid": "BV1bb", "title": "", "owner": {}, "stat": {}},  # 缺标题，拒绝
        {
            "bvid": "BV1cc",
            "title": "t4",
            "owner": {},
            "stat": {"view": "123", "danmaku": "-1", "reply": "1.9"},
        },
        {"bvid": "BV1dd", "title": "t5", "owner": {}, "stat": {"view": 1.9, "coin": 2.0}},
    ]
    records, rejected = parse_ranking_records(items)
    assert rejected == 2
    assert [r.bvid for r in records] == ["BV1aa", "BV1cc", "BV1dd"]

    # 整数文本正常解析，负数与小数文本拒绝。
    assert records[1].view_count == 123
    assert records[1].danmaku_count is None
    assert records[1].reply_count is None
    # 非整型 float 拒绝，整型 float 可解析。
    assert records[2].view_count is None
    assert records[2].coin_count == 2


def test_accented_latin_tokens() -> None:
    # 重音字母必须与相邻拉丁字母组成同一词元，而不是被拆成单字母碎片。
    analyzer = TitleAnalyzer(load_stopword_policy())
    record = VideoRankingRecord.from_api_item(
        {"bvid": "BV1aa", "title": "déjà vu café"}, rank=1
    )
    assert analyzer.analyze([record]) == {"café": 1, "déjà": 1}


def test_math_symbols_not_merged_into_tokens() -> None:
    # ×(U+00D7)/÷(U+00F7) 是数学符号而非字母，不能与相邻字符拼成词元。
    analyzer = TitleAnalyzer(load_stopword_policy())
    record = VideoRankingRecord.from_api_item(
        {"bvid": "BV1aa", "title": "5×5 魔方 ××× 6÷2"}, rank=1
    )
    assert analyzer.analyze([record]) == {"魔方": 1}


def test_symbols_inside_cjk_and_cyrillic_blocks_dropped() -> None:
    # 日文、西里尔按整块匹配，块内符号（・U+30FB、҂U+0482）不能被当成词元。
    analyzer = TitleAnalyzer(load_stopword_policy())
    record = VideoRankingRecord.from_api_item(
        {"bvid": "BV1aa", "title": "・・ ҂҂ ゲーム"}, rank=1
    )
    assert analyzer.analyze([record]) == {"ゲーム": 1}


def test_extended_latin_and_hangul_jamo_tokens() -> None:
    # ẞ 在 Latin Extended Additional，casefold 后为 strasse，不能被截成 stra。
    # ㅋ 经 NFKC 折叠到 Hangul Jamo 区，正则必须覆盖该区间。
    analyzer = TitleAnalyzer(load_stopword_policy())
    record = VideoRankingRecord.from_api_item(
        {"bvid": "BV1aa", "title": "STRAẞE ㅋㅋㅋ"}, rank=1
    )
    assert analyzer.analyze([record]) == {"strasse": 1, "ᄏᄏᄏ": 1}


def test_apostrophe_stopwords_filtered_whole() -> None:
    # 撇号是词内连接符，否则 ain't 会退化成噪声词 ain、quelqu'un 退化成 quelqu。
    analyzer = TitleAnalyzer(load_stopword_policy())
    record = VideoRankingRecord.from_api_item(
        {"bvid": "BV1aa", "title": "ain't quelqu'un 魔方"}, rank=1
    )
    assert analyzer.analyze([record]) == {"魔方": 1}


def test_non_string_fields_rejected() -> None:
    # bvid/title 是 list、dict 时不能被 str() 伪造成有效记录。
    items = [
        {"bvid": ["BV1xx"], "title": "t1"},
        {"bvid": "BV1aa", "title": {"bad": "title"}},
        {"bvid": "BV1bb", "title": 12345},
        {"bvid": "BV1cc", "title": "t4"},
    ]
    records, rejected = parse_ranking_records(items)
    assert rejected == 3
    assert [r.bvid for r in records] == ["BV1cc"]


def test_csv_formula_prefix_escaped() -> None:
    # 标题是投稿者可控内容，Excel 会把 = + - @ 开头的单元格当公式求值。
    record = VideoRankingRecord.from_api_item(
        {"bvid": "BV1aa", "title": "=1+1", "owner": {"name": "@up"}}, rank=1
    )
    with tempfile.TemporaryDirectory() as directory:
        destination = Path(directory) / "out.csv"
        write_records_csv(destination, [record])
        with destination.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
    assert rows[0]["视频标题"] == "'=1+1"
    assert rows[0]["UP主"] == "'@up"


def _fake_json_response(payload: dict, status_code: int = 200) -> requests.Response:
    response = requests.Response()
    response.status_code = status_code
    response._content = json.dumps(payload).encode("utf-8")
    return response


def test_fetch_retries_risk_control() -> None:
    # 第一次返回 -352 风控拦截时刷新 buvid 重试，第二次成功。
    from unittest.mock import patch

    from bilibili_ranker.client import SPI_API_URL, fetch_all_ranking

    # 风控拦截在真实环境伴随 HTTP 412，业务码只在响应体里。
    ranking_responses = [
        ({"code": -352, "message": "-352"}, 412),
        ({"code": 0, "data": {"list": [{"bvid": "BV1aa", "title": "t"}]}}, 200),
    ]
    spi_calls = 0
    retry_cookies: list[dict[str, str]] = []

    def fake_get(self: requests.Session, url: str, **kwargs: object) -> requests.Response:
        nonlocal spi_calls
        if url == SPI_API_URL:
            spi_calls += 1
            return _fake_json_response({"code": 0, "data": {"b_3": "b3", "b_4": "b4"}})
        retry_cookies.append(self.cookies.get_dict())
        payload, status_code = ranking_responses.pop(0)
        return _fake_json_response(payload, status_code)

    with patch.object(requests.Session, "get", fake_get):
        result = fetch_all_ranking()

    assert [item["bvid"] for item in result.items] == ["BV1aa"]
    assert spi_calls == 1  # 无前置刷新，成功后也不再刷
    assert retry_cookies == [
        {},
        {"buvid3": "b3", "buvid4": "b4"},
    ]  # buvid 确实写进 cookie jar 并随重试发送


def test_fetch_raises_when_risk_control_persists() -> None:
    # 两轮都返回 -352 时抛出 BilibiliAPIError，且最后一轮不再空刷 buvid。
    from unittest.mock import patch

    from bilibili_ranker.client import BilibiliAPIError, SPI_API_URL, fetch_all_ranking

    spi_calls = 0

    def fake_get(self: requests.Session, url: str, **kwargs: object) -> requests.Response:
        nonlocal spi_calls
        if url == SPI_API_URL:
            spi_calls += 1
            return _fake_json_response({"code": 0, "data": {"b_3": "b3", "b_4": "b4"}})
        return _fake_json_response({"code": -352, "message": "-352"}, 412)

    with patch.object(requests.Session, "get", fake_get):
        try:
            fetch_all_ranking()
        except BilibiliAPIError as exc:
            assert "风控" in str(exc)
        else:
            raise AssertionError("两轮 -352 后未抛出 BilibiliAPIError")
    assert spi_calls == 1  # 只在第 1 轮后刷新一次


def test_fetch_survives_malformed_buvid() -> None:
    # SPI 返回非字符串 cookie 值时不能让 cookies.set() 的 AttributeError 逸出，
    # 必须仍收敛成 BilibiliAPIError，由 CLI 统一转成退出码 1。
    from unittest.mock import patch

    from bilibili_ranker.client import BilibiliAPIError, SPI_API_URL, fetch_all_ranking

    def fake_get(self: requests.Session, url: str, **kwargs: object) -> requests.Response:
        if url == SPI_API_URL:
            return _fake_json_response({"code": 0, "data": {"b_3": ["bad"], "b_4": "ok"}})
        return _fake_json_response({"code": -352, "message": "-352"}, 412)

    with patch.object(requests.Session, "get", fake_get):
        try:
            fetch_all_ranking()
        except BilibiliAPIError as exc:
            assert "风控" in str(exc)
        else:
            raise AssertionError("畸形 buvid 未收敛成 BilibiliAPIError")


def test_fetch_retries_on_412_without_json_body() -> None:
    # 真实风控常返回 412 + HTML（无 JSON 业务码），此时也必须刷 buvid 重试。
    from unittest.mock import patch

    from bilibili_ranker.client import SPI_API_URL, fetch_all_ranking

    spi_calls = 0
    ranking_calls = 0

    def fake_get(self: requests.Session, url: str, **kwargs: object) -> requests.Response:
        nonlocal spi_calls, ranking_calls
        if url == SPI_API_URL:
            spi_calls += 1
            return _fake_json_response({"code": 0, "data": {"b_3": "b3", "b_4": "b4"}})
        ranking_calls += 1
        if ranking_calls == 1:
            response = requests.Response()
            response.status_code = 412
            response._content = b"<html>risk control</html>"
            return response
        return _fake_json_response(
            {"code": 0, "data": {"list": [{"bvid": "BV1aa", "title": "t"}]}}
        )

    with patch.object(requests.Session, "get", fake_get):
        result = fetch_all_ranking()

    assert [item["bvid"] for item in result.items] == ["BV1aa"]
    assert spi_calls == 1
    assert ranking_calls == 2


def test_fetch_reports_http_error() -> None:
    # 非风控的 HTTP 错误仍要抛错，不能因为先读响应体而被吞掉。
    from unittest.mock import patch

    from bilibili_ranker.client import BilibiliAPIError, fetch_all_ranking

    def fake_get(self: requests.Session, url: str, **kwargs: object) -> requests.Response:
        return _fake_json_response({"code": 0, "data": {"list": []}}, 503)

    with patch.object(requests.Session, "get", fake_get):
        try:
            fetch_all_ranking()
        except BilibiliAPIError as exc:
            assert "请求失败" in str(exc)
        else:
            raise AssertionError("HTTP 503 未抛出 BilibiliAPIError")


def test_fetch_over_real_http_retries_after_412() -> None:
    """走完整 requests 收发链路，锁死 412 的响应体能回到业务代码并触发第二轮。

    注意：buvid cookie 的 domain 是 `.bilibili.com`，不会发给 127.0.0.1，
    所以这里不断言请求头里的 cookie，domain 正确性由
    test_buvid_cookie_reaches_request_header 负责。
    """

    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from bilibili_ranker import client as client_module

    ranking_calls: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            if self.path.startswith("/spi"):
                body, status = {"code": 0, "data": {"b_3": "b3", "b_4": "b4"}}, 200
            else:
                ranking_calls.append(self.path)
                if len(ranking_calls) == 1:
                    body, status = {"code": -352, "message": "risk"}, 412
                else:
                    body, status = (
                        {"code": 0, "data": {"list": [{"bvid": "BV1aa", "title": "标题"}]}},
                        200,
                    )
            payload = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    host, port = server.server_address[0], server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    original_ranking = client_module.RANKING_API_URL
    original_spi = client_module.SPI_API_URL
    try:
        client_module.RANKING_API_URL = f"http://{host}:{port}/ranking"
        client_module.SPI_API_URL = f"http://{host}:{port}/spi"
        result = client_module.fetch_all_ranking(timeout_seconds=5.0)
    finally:
        client_module.RANKING_API_URL = original_ranking
        client_module.SPI_API_URL = original_spi
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert [item["bvid"] for item in result.items] == ["BV1aa"]
    # 412 的响应体确实交回业务代码，-352 分支可达并触发了第二轮请求。
    assert len(ranking_calls) == 2


def test_buvid_cookie_reaches_request_header() -> None:
    """cookie jar 有值不等于请求头带上了：domain 写错时 get_dict() 照样能查到。

    这里走 requests 真实的 prepare_request，把 domain 正确性锁死。
    """

    import re
    from unittest.mock import patch

    from bilibili_ranker.client import (
        RANKING_API_URL,
        SPI_API_URL,
        _refresh_buvid,
        build_session,
    )

    def fake_get(self: requests.Session, url: str, **kwargs: object) -> requests.Response:
        assert url == SPI_API_URL
        return _fake_json_response({"code": 0, "data": {"b_3": "b3", "b_4": "b4"}})

    session = build_session()
    try:
        with patch.object(requests.Session, "get", fake_get):
            _refresh_buvid(session, user_agent="ua", timeout_seconds=5.0)
        prepared = session.prepare_request(requests.Request("GET", RANKING_API_URL))
    finally:
        session.close()

    header = prepared.headers.get("Cookie") or ""
    assert re.search(r"\bbuvid3=b3\b", header), header
    assert re.search(r"\bbuvid4=b4\b", header), header


def test_session_keeps_transient_retry_config() -> None:
    # 删掉 HTTPAdapter/Retry 配置时，纯 mock 测试不会有任何反应，这里直接断言。
    from urllib3.util.retry import Retry

    from bilibili_ranker.client import build_session

    session = build_session()
    try:
        retries = session.get_adapter("https://api.bilibili.com/").max_retries
    finally:
        session.close()
    assert isinstance(retries, Retry)
    assert retries.total == 2
    assert set(retries.status_forcelist or ()) == {429, 500, 502, 503, 504}


def test_atomic_csv_write() -> None:
    record = VideoRankingRecord.from_api_item(
        {"bvid": "BV1aa", "title": "标题", "owner": {}, "stat": {}},
        rank=1,
    )
    with tempfile.TemporaryDirectory() as directory:
        destination = Path(directory) / "out.csv"
        write_records_csv(destination, [record])
        with destination.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        assert len(rows) == 1
        assert rows[0]["BV号"] == "BV1aa"
        assert rows[0]["排名"] == "1"
        # 失败写入不留临时文件。
        leftovers = [p.name for p in Path(directory).iterdir() if p.name.endswith(".tmp")]
        assert leftovers == []


def test_non_finite_timeout_rejected() -> None:
    # nan/inf 都能通过 `<= 0`，inf 会在 socket.settimeout 抛 OverflowError 逸出错误处理。
    for value in ("nan", "inf", "-inf", "0"):
        try:
            main(["--timeout", value])
        except SystemExit as exc:
            assert exc.code == 2, value
        else:
            raise AssertionError(f"--timeout {value} 应该被拒绝")


def test_wordcloud_write_is_atomic() -> None:
    # 渲染失败不能截断已有 PNG，也不能留下临时文件。
    with tempfile.TemporaryDirectory() as directory:
        destination = Path(directory) / "wc.png"
        destination.write_bytes(b"old")

        import bilibili_ranker.wordcloud as wordcloud_module

        class _Boom:
            def __init__(self, **_: object) -> None:
                pass

            def generate_from_frequencies(self, _: dict) -> None:
                pass

            def to_image(self) -> object:
                raise OSError("disk full")

        original = sys.modules.get("wordcloud")
        sys.modules["wordcloud"] = types.SimpleNamespace(WordCloud=_Boom)
        original_font = wordcloud_module.resolve_font_path
        wordcloud_module.resolve_font_path = lambda _=None: Path("fake.ttf")
        try:
            try:
                wordcloud_module.render_wordcloud({"词": 1}, destination)
            except OSError:
                pass
            else:
                raise AssertionError("应该抛出 OSError")
        finally:
            wordcloud_module.resolve_font_path = original_font
            if original is None:
                del sys.modules["wordcloud"]
            else:
                sys.modules["wordcloud"] = original

        assert destination.read_bytes() == b"old"
        leftovers = [p.name for p in Path(directory).iterdir() if p.name.endswith(".tmp")]
        assert leftovers == []


def test_wordcloud_renders_real_png() -> None:
    # 走真实 WordCloud + PIL 保存路径：临时文件扩展名是 .tmp，PIL 推不出格式，
    # 必须显式 format="PNG"，否则整个词云功能 100% 失效。
    from bilibili_ranker.fonts import resolve_font_path
    from bilibili_ranker.wordcloud import render_wordcloud

    try:
        resolve_font_path(None)
    except RuntimeError:
        return  # 环境无 CJK 字体，跳过

    with tempfile.TemporaryDirectory() as directory:
        destination = Path(directory) / "wc.png"
        rendered = render_wordcloud({"魔方": 5, "教程": 3}, destination, width=200, height=100)
        assert rendered.exists()
        assert destination.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
        leftovers = [p.name for p in Path(directory).iterdir() if p.name.endswith(".tmp")]
        assert leftovers == []


def test_japanese_kanji_word_survives_jieba() -> None:
    # jieba 只有中文词典，「実況」会被切成単字后被最短长度过滤掉；
    # 全单字时保留整块，日文汉字词才不会系统性丢失。
    analyzer = TitleAnalyzer(load_stopword_policy())
    record = VideoRankingRecord.from_api_item(
        {"bvid": "BV1aa", "title": "ゲーム実況"}, rank=1
    )
    frequencies = analyzer.analyze([record])
    assert "実況" in frequencies
    assert "ゲーム" in frequencies


if __name__ == "__main__":
    test_stopword_policy()
    test_deduplicate_records()
    test_parse_ranking_records_tolerance()
    test_accented_latin_tokens()
    test_math_symbols_not_merged_into_tokens()
    test_symbols_inside_cjk_and_cyrillic_blocks_dropped()
    test_extended_latin_and_hangul_jamo_tokens()
    test_apostrophe_stopwords_filtered_whole()
    test_non_string_fields_rejected()
    test_csv_formula_prefix_escaped()
    test_fetch_retries_risk_control()
    test_fetch_raises_when_risk_control_persists()
    test_fetch_survives_malformed_buvid()
    test_fetch_reports_http_error()
    test_fetch_retries_on_412_without_json_body()
    test_fetch_over_real_http_retries_after_412()
    test_buvid_cookie_reaches_request_header()
    test_session_keeps_transient_retry_config()
    test_atomic_csv_write()
    test_non_finite_timeout_rejected()
    test_wordcloud_write_is_atomic()
    test_wordcloud_renders_real_png()
    test_japanese_kanji_word_survives_jieba()
    print("ok")
