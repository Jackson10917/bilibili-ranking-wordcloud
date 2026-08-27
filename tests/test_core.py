"""回归检查：去重、停用词、分词、排行榜请求重试与 CSV 原子写入。

需要已安装项目依赖（jieba、stopwordsiso）。由 pytest 收集运行：
python -m pytest tests
也可直接运行 python tests/test_core.py，但聚合器同样依赖 pytest（用于 skip 语义），
需先安装 `pip install -e ".[test]"`。
"""

from __future__ import annotations

import csv
import json
import sys
import tempfile
import types
from datetime import datetime, timezone
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


def test_from_api_item_maps_every_field() -> None:
    # 变异测试发现 16 个字段里 9 个的 API 字段名映射零断言：上游改名时测试照旧
    # 全绿而 CSV 整列变空。逐字段锁死映射、video_url 拼装与发布时间转换。
    item = {
        "bvid": "BV1aa0000000",
        "title": "标题",
        "tname": "知识",
        "tnamev2": "科学",
        "pid_name_v2": "科普",
        "owner": {"name": "UP主"},
        "pubdate": 1704067200,
        "duration": 245,
        "stat": {
            "view": 100,
            "danmaku": 200,
            "reply": 300,
            "favorite": 400,
            "coin": 500,
            "share": 600,
            "like": 700,
        },
    }
    record = VideoRankingRecord.from_api_item(item, rank=1)
    assert record.category_name == "知识"
    assert record.category_v2_name == "科学"
    assert record.parent_category_v2_name == "科普"
    assert record.uploader_name == "UP主"
    assert record.video_url == "https://www.bilibili.com/video/BV1aa0000000"
    assert record.published_at == "2024-01-01 08:00:00"
    assert record.duration_seconds == 245
    assert record.view_count == 100
    assert record.danmaku_count == 200
    assert record.reply_count == 300
    assert record.favorite_count == 400
    assert record.coin_count == 500
    assert record.share_count == 600
    assert record.like_count == 700
    # frozen 契约：记录创建后不可变，防止下游误改榜单数据。
    import pytest

    with pytest.raises(AttributeError):
        record.rank = 99


def test_zero_stats_are_preserved() -> None:
    # _optional_int 的 >= 0 边界：0 是合法统计值（新视频零播放），不能当缺失丢掉。
    item = {
        "bvid": "BV1aa0000000",
        "title": "t",
        "owner": {},
        "pubdate": 1704067200,
        "stat": {"view": 0, "danmaku": 0, "coin": 0},
    }
    record = VideoRankingRecord.from_api_item(item, rank=1)
    assert record.view_count == 0
    assert record.danmaku_count == 0
    assert record.coin_count == 0
    assert record.duration_seconds is None  # duration 缺失仍必须是缺失


def test_parse_ranking_records_ranks_are_one_based() -> None:
    # enumerate 从 1 起：排名列直接写进 CSV，0 基/2 基都要被拦下。
    records, _ = parse_ranking_records(
        [
            {"bvid": "BV1aa0000000", "title": "a"},
            {"bvid": "BV1bb0000000", "title": "b"},
        ]
    )
    assert [r.rank for r in records] == [1, 2]


def test_parse_ranking_records_tolerance() -> None:
    items = [
        {"bvid": "BV1aa0000000", "title": "t1", "owner": {}, "stat": {}},
        {"bvid": "", "title": "t2", "owner": {}, "stat": {}},  # 缺 bvid，拒绝
        {"bvid": "BV1bb0000000", "title": "", "owner": {}, "stat": {}},  # 缺标题，拒绝
        {
            "bvid": "BV1cc0000000",
            "title": "t4",
            "owner": {},
            "stat": {"view": "123", "danmaku": "-1", "reply": "1.9"},
        },
        {"bvid": "BV1dd0000000", "title": "t5", "owner": {}, "stat": {"view": 1.9, "coin": 2.0}},
    ]
    records, rejected = parse_ranking_records(items)
    assert rejected == 2
    assert [r.bvid for r in records] == ["BV1aa0000000", "BV1cc0000000", "BV1dd0000000"]

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


def test_non_string_fields_rejected() -> None:
    # bvid/title 是 list、dict 时不能被 str() 伪造成有效记录。
    items = [
        {"bvid": ["BV1xx0000000"], "title": "t1"},
        {"bvid": "BV1aa0000000", "title": {"bad": "title"}},
        {"bvid": "BV1bb0000000", "title": 12345},
        {"bvid": "BV1cc0000000", "title": "t4"},
    ]
    records, rejected = parse_ranking_records(items)
    assert rejected == 3
    assert [r.bvid for r in records] == ["BV1cc0000000"]


def test_csv_formula_prefix_escaped() -> None:
    # 标题是投稿者可控内容，Excel 会把 = + - @ 开头的单元格当公式求值。
    record = VideoRankingRecord.from_api_item(
        {"bvid": "BV1aa0000000", "title": "=1+1", "owner": {"name": "@up"}}, rank=1
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
        ({"code": 0, "data": {"list": [{"bvid": "BV1aa0000000", "title": "t"}]}}, 200),
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

    assert [item["bvid"] for item in result.items] == ["BV1aa0000000"]
    assert spi_calls == 1  # 无前置刷新，成功后也不再刷
    assert retry_cookies == [
        {},
        {"buvid3": "b3", "buvid4": "b4"},
    ]  # buvid 确实写进 cookie jar 并随重试发送


def test_fetch_raises_when_risk_control_persists() -> None:
    # 每轮都返回 -352 时抛出 BilibiliAPIError，且最后一轮不再空刷 buvid。
    from unittest.mock import patch

    from bilibili_ranker import client as client_module
    from bilibili_ranker.client import SPI_API_URL, BilibiliAPIError, fetch_all_ranking

    spi_calls = 0
    sleeps: list[float] = []

    def fake_get(self: requests.Session, url: str, **kwargs: object) -> requests.Response:
        nonlocal spi_calls
        if url == SPI_API_URL:
            spi_calls += 1
            return _fake_json_response({"code": 0, "data": {"b_3": "b3", "b_4": "b4"}})
        return _fake_json_response({"code": -352, "message": "-352"}, 412)

    with patch.object(requests.Session, "get", fake_get):
        with patch.object(client_module.time, "sleep", sleeps.append):
            try:
                fetch_all_ranking()
            except BilibiliAPIError as exc:
                assert "风控" in str(exc)
            else:
                raise AssertionError("持续 -352 后未抛出 BilibiliAPIError")
    # 最后一轮不再空刷 buvid：刷新次数 = 总轮次 - 1。
    assert spi_calls == client_module._RISK_CONTROL_ATTEMPTS - 1
    # 首轮不等待（冷启动必吃一次 -352），之后递增退避，不能立刻连打风控接口。
    assert sleeps == [1.0]


def test_fetch_survives_malformed_buvid() -> None:
    # SPI 返回非字符串 cookie 值时不能让 cookies.set() 的 AttributeError 逸出，
    # 必须仍收敛成 BilibiliAPIError，由 CLI 统一转成退出码 1。
    from unittest.mock import patch

    from bilibili_ranker.client import SPI_API_URL, BilibiliAPIError, fetch_all_ranking

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
            {"code": 0, "data": {"list": [{"bvid": "BV1aa0000000", "title": "t"}]}}
        )

    with patch.object(requests.Session, "get", fake_get):
        result = fetch_all_ranking()

    assert [item["bvid"] for item in result.items] == ["BV1aa0000000"]
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
                        {"code": 0, "data": {"list": [{"bvid": "BV1aa0000000", "title": "标题"}]}},
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

    assert [item["bvid"] for item in result.items] == ["BV1aa0000000"]
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
        {"bvid": "BV1aa0000000", "title": "标题", "owner": {}, "stat": {}},
        rank=1,
    )
    with tempfile.TemporaryDirectory() as directory:
        destination = Path(directory) / "out.csv"
        write_records_csv(destination, [record])
        with destination.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        assert len(rows) == 1
        assert rows[0]["BV号"] == "BV1aa0000000"
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


def test_oversized_timeout_rejected() -> None:
    # 1e9 起 socket.settimeout 就抛 OverflowError，只校验"有限且 > 0"挡不住，
    # main 也不捕获 OverflowError，用户会看到 traceback。
    from bilibili_ranker.client import BilibiliAPIError, fetch_all_ranking

    for value in ("1e10", "1e100", "86401"):
        try:
            main(["--timeout", value])
        except SystemExit as exc:
            assert exc.code == 2, value
        else:
            raise AssertionError(f"--timeout {value} 应该被拒绝")

    # 库函数直接调用同样要挡住，且错误类型与本模块其余错误一致。
    for bad in (1e10, -1, 0, float("inf"), float("nan")):
        try:
            fetch_all_ranking(timeout_seconds=bad)
        except BilibiliAPIError:
            pass
        else:
            raise AssertionError(f"timeout_seconds={bad} 应该被拒绝")


def test_retries_on_200_with_invalid_json() -> None:
    # CDN 偶发返回截断 body 或 HTML 错误页（HTTP 仍是 200），一次就报死属于把瞬时故障
    # 当永久失败。urllib3 Retry 只覆盖异常和 429/5xx，这一类不在范围内。
    import http.server
    import json as _json
    import threading

    import bilibili_ranker.client as client_module

    good = _json.dumps({"code": 0, "data": {"list": [{"bvid": "BV1aa0000000", "title": "测试"}]}})
    counter = {"n": 0}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 接口
            counter["n"] += 1
            body = b"<html>502</html>" if counter["n"] == 1 else good.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    original = client_module.RANKING_API_URL
    client_module.RANKING_API_URL = f"http://127.0.0.1:{server.server_port}/x"
    try:
        result = client_module.fetch_all_ranking(timeout_seconds=5)
        assert counter["n"] == 2, counter
        assert result.items[0]["bvid"] == "BV1aa0000000"
    finally:
        client_module.RANKING_API_URL = original
        server.shutdown()
        server.server_close()


def test_fetch_retries_on_truncated_body() -> None:
    # CDN 在 Content-Length 读满前断连（IncompleteRead → RequestException）与截断的
    # 垃圾 body 是同一类故障，必须同样走 200 重试路径，不能一次就报死。
    import http.server
    import json as _json
    import threading

    import bilibili_ranker.client as client_module

    good = _json.dumps({"code": 0, "data": {"list": [{"bvid": "BV1aa0000000", "title": "测试"}]}})
    counter = {"n": 0}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 接口
            counter["n"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            if counter["n"] == 1:
                # 谎报 Content-Length 后只发一小段就断开，逼客户端读出 IncompleteRead。
                self.send_header("Content-Length", "200")
                self.end_headers()
                self.wfile.write(b'{"code": 0, "data": {"list": [{"bvid": "BV1aa')
                self.close_connection = True
                return
            body = good.encode()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    original = client_module.RANKING_API_URL
    client_module.RANKING_API_URL = f"http://127.0.0.1:{server.server_port}/x"
    try:
        result = client_module.fetch_all_ranking(timeout_seconds=5)
        assert counter["n"] == 2, counter
        assert result.items[0]["bvid"] == "BV1aa0000000"
    finally:
        client_module.RANKING_API_URL = original
        server.shutdown()
        server.server_close()


def test_empty_result_exits_nonzero() -> None:
    # 接口成功但整榜解析失败时返回 0，会让定时任务把只有表头的 CSV 当成功结果。
    from unittest.mock import patch

    import bilibili_ranker.cli as cli_module
    from bilibili_ranker.client import RankingFetchResult

    fetched = RankingFetchResult(
        fetched_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        items=({"bvid": "bad", "title": "字段变更"},),
    )

    with tempfile.TemporaryDirectory() as directory:
        with patch.object(cli_module, "fetch_all_ranking", lambda **_: fetched):
            assert main(["--output-dir", directory]) == 1
        # 退出码非零，但 CSV 仍要落盘，便于排查上游字段变化。
        csv_files = list(Path(directory).glob("ranking_*.csv"))
        assert len(csv_files) == 1, csv_files


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
    # 数据驱动：名单里每一条域名都必须真实生效——变异测试显示新增条目容易
    # 出现「加了名单没加测试」的死条目，这里随名单增长自动全覆盖。
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


def test_output_bundle_reserves_csv_atomically() -> None:
    # exists() 检查在"检查"和"写入"之间有窗口：同秒并发的两个进程会拿到同一编号，
    # 后写者的 os.replace 覆盖先写者的结果。占位必须是原子的。
    from bilibili_ranker.storage import create_output_bundle

    moment = datetime(2024, 1, 1, tzinfo=timezone.utc)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        # 连续调用而不写入任何内容，模拟两个并发进程都还没走到 os.replace。
        names = [create_output_bundle(root, moment).ranking_csv.name for _ in range(3)]
        assert len(set(names)) == 3, names


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
    import pytest
    from PIL import Image

    from bilibili_ranker.fonts import resolve_font_path
    from bilibili_ranker.wordcloud import render_wordcloud

    try:
        resolve_font_path(None)
    except RuntimeError:
        # 必须 skip 而不是 return：静默通过的话，这条测试要防的"PNG 保存回归"在 CI 上
        # 永远是绿的。CI 已装 fonts-noto-cjk，正常情况下不会走到这里。
        pytest.skip("环境无 CJK 字体")

    with tempfile.TemporaryDirectory() as directory:
        destination = Path(directory) / "wc.png"
        rendered = render_wordcloud({"魔方": 5, "教程": 3}, destination, width=200, height=100)
        assert rendered.exists()
        assert destination.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
        leftovers = [p.name for p in Path(directory).iterdir() if p.name.endswith(".tmp")]
        assert leftovers == []

        # 默认尺寸契约（README：默认 1920×1080）与嵌套输出目录自动创建
        # （两层深：parents=False 的 mkdir 撑不住）。
        default_sized = render_wordcloud(
            {"魔方": 5, "教程": 3}, Path(directory) / "deep" / "nested" / "out.png"
        )
        with Image.open(default_sized) as image:
            assert image.size == (1920, 1080)


def test_wordcloud_render_is_reproducible() -> None:
    # 模块承诺「可复现词云」：random_state 固定后，同输入两次渲染必须逐字节一致。
    import pytest

    from bilibili_ranker.fonts import resolve_font_path
    from bilibili_ranker.wordcloud import render_wordcloud

    try:
        resolve_font_path(None)
    except RuntimeError:
        pytest.skip("环境无 CJK 字体")

    with tempfile.TemporaryDirectory() as directory:
        frequencies = {"魔方": 5, "教程": 3, "入门": 2, "直播": 1}
        # 预热渲染：进程内首次渲染可能带一次性初始化状态，先排掉再比对。
        render_wordcloud(frequencies, Path(directory) / "warmup.png", width=100, height=50)
        first = render_wordcloud(frequencies, Path(directory) / "a.png", width=300, height=150)
        second = render_wordcloud(frequencies, Path(directory) / "b.png", width=300, height=150)
        assert first.read_bytes() == second.read_bytes()


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

    「他也是」被 jieba 切成三个单字，逐字都是停用词而整块不是，不加判断就会绕过过滤。
    反向情况：日语汉字词里个别汉字撞上中文停用词（自転車 的「自」、本気 的「本」）时
    仍须保留，所以判据是"全部单字都是停用词"，而不是"存在停用词"。
    """

    analyzer = TitleAnalyzer(load_stopword_policy())

    def tokens(title: str) -> dict[str, int]:
        record = VideoRankingRecord.from_api_item({"bvid": "BV1aa0000000", "title": title}, rank=1)
        return analyzer.analyze([record])

    for noise in ("他也是", "我的了", "和你的", "也是的"):
        assert noise not in tokens(noise), noise

    for word in ("自転車", "本気", "実況"):
        assert word in tokens(word), word


def test_bvid_format_validated() -> None:
    # bvid 拼进 video_url、也裸写进 CSV 的 BV号 列，脏值不能落盘。
    items = [
        {"bvid": "=cmd|'/c calc'!A", "title": "t1"},
        {"bvid": "BV1aa", "title": "t2"},
        {"bvid": "BV1aa0000000", "title": "t3"},
    ]
    records, rejected = parse_ranking_records(items)
    assert rejected == 2
    assert [r.bvid for r in records] == ["BV1aa0000000"]


def test_412_with_business_code_not_treated_as_risk_control() -> None:
    # 代理/CDN 也会回 412。带了业务码就按业务码判，否则真实错误被盖成"风控"，
    # 极端情况下 412 + code=0 的有效响应会被整轮丢弃。
    from unittest.mock import patch

    from bilibili_ranker.client import SPI_API_URL, BilibiliAPIError, fetch_all_ranking

    def fake_get(self: requests.Session, url: str, **kwargs: object) -> requests.Response:
        if url == SPI_API_URL:
            raise AssertionError("非风控的 412 不应触发 buvid 刷新")
        return _fake_json_response({"code": -404, "message": "啥都木有"}, 412)

    with patch.object(requests.Session, "get", fake_get):
        try:
            fetch_all_ranking()
        except BilibiliAPIError as exc:
            assert "412" in str(exc)  # 按 HTTP 错误上报，不再被误标成风控
        else:
            raise AssertionError("412 + code=-404 未抛出 BilibiliAPIError")


def test_stopword_errors_surface_before_network() -> None:
    # 语言代码打错时必须在发请求前就报错，而不是抓完整个榜单再抛异常、CSV 零落盘。
    from unittest.mock import patch

    import bilibili_ranker.cli as cli_module

    def boom(**_: object) -> object:
        raise AssertionError("参数错误时不应该发起网络请求")

    with patch.object(cli_module, "fetch_all_ranking", boom):
        # boom 的 AssertionError 不被 main 的 except 捕获，抓到网络请求就会直接冒出来。
        assert main(["--languages", "zh,xx"]) == 1


def test_font_explicit_path_validated() -> None:
    # 显式路径不存在时应抛 FontNotFoundError，而不是 FileNotFoundError 或 AttributeError。
    import tempfile

    from bilibili_ranker.fonts import FontNotFoundError, resolve_font_path

    with tempfile.TemporaryDirectory() as d:
        # 后缀不对
        bad_ext = Path(d) / "font.bmp"
        bad_ext.write_bytes(b"")
        try:
            resolve_font_path(bad_ext)
        except FontNotFoundError:
            pass
        else:
            raise AssertionError("不受支持的后缀未抛出 FontNotFoundError")

        # 文件不存在
        try:
            resolve_font_path(Path(d) / "nonexistent.ttf")
        except FontNotFoundError:
            pass
        else:
            raise AssertionError("不存在的路径未抛出 FontNotFoundError")


def test_font_env_var_overrides_default() -> None:
    # BILIBILI_WORDCLOUD_FONT 指向有效字体时应返回该路径；无效路径应抛 FontNotFoundError。
    import os
    import tempfile

    import pytest

    from bilibili_ranker.fonts import FontNotFoundError, resolve_font_path

    # 深度校验走 PIL 试载，有效样例直接用系统里已发现的 CJK 字体。
    try:
        valid = resolve_font_path(None)
    except RuntimeError:
        pytest.skip("环境无 CJK 字体")

    with tempfile.TemporaryDirectory() as d:
        old = os.environ.get("BILIBILI_WORDCLOUD_FONT")
        try:
            os.environ["BILIBILI_WORDCLOUD_FONT"] = str(valid)
            result = resolve_font_path()
            assert result == valid.resolve()

            os.environ["BILIBILI_WORDCLOUD_FONT"] = str(Path(d) / "missing.ttf")
            try:
                resolve_font_path()
            except FontNotFoundError:
                pass
            else:
                raise AssertionError("无效环境变量路径未抛出 FontNotFoundError")
        finally:
            if old is None:
                os.environ.pop("BILIBILI_WORDCLOUD_FONT", None)
            else:
                os.environ["BILIBILI_WORDCLOUD_FONT"] = old


def test_corrupt_font_rejected_before_pil() -> None:
    # 内容是垃圾的 fake.ttf 必须在校验阶段就报"损坏"，而不是拖到 PIL 抛
    # "cannot open resource"，那时用户判断不出是自己指定的字体有问题。
    import pytest

    from bilibili_ranker.fonts import FontNotFoundError, resolve_font_path

    with tempfile.TemporaryDirectory() as d:
        junk = Path(d) / "fake.ttf"
        junk.write_bytes(b"dummy")
        try:
            resolve_font_path(junk)
        except FontNotFoundError as exc:
            assert "损坏" in str(exc)
        else:
            raise AssertionError("垃圾内容的字体未被拒绝")

        # 魔数对、字形表是垃圾的文件由 PIL 试载拦截——渲染走的就是同一条加载路径。
        fake = Path(d) / "magic_only.ttf"
        fake.write_bytes(b"\x00\x01\x00\x00garbage-glyph-table")
        try:
            resolve_font_path(fake)
        except FontNotFoundError as exc:
            assert "无法加载" in str(exc)
        else:
            raise AssertionError("魔数合法的损坏字体未被拒绝")

        # 真实字体不能被误伤：能过校验的必然也能被渲染端加载。
        try:
            real_font = resolve_font_path(None)
        except RuntimeError:
            pytest.skip("环境无 CJK 字体")
        assert resolve_font_path(real_font) == real_font.resolve()


def test_zero_width_characters_do_not_split_tokens() -> None:
    # B站"防和谐"标题会插零宽空格（U+200B，Cf 类，NFKC 不动它、split() 也不认），
    # 不剔除的话「黑​丝」被劈成两个单字块，双双被 minimum_token_length 丢掉。
    analyzer = TitleAnalyzer(load_stopword_policy())
    record = VideoRankingRecord.from_api_item(
        {"bvid": "BV1aa0000000", "title": "魔\u200b方 caf\u200bé"}, rank=1
    )
    assert analyzer.analyze([record]) == {"魔方": 1, "café": 1}


def test_empty_language_list_rejected() -> None:
    # 过滤后为空时 iso_stopwords(()) 返回空集、unsupported 也为空，
    # 静默放行会让全部基础停用词失效，词云满屏虚词。
    # 非字符串语言码即使混在合法码里也要报错：静默丢掉会让停用词表少一门语言而无提示。
    for languages in ([" "], [123], [], ["zh", 123], ["zh", None]):
        try:
            load_stopword_policy(languages=languages)
        except ValueError:
            pass
        else:
            raise AssertionError(f"languages={languages!r} 未被拒绝")


def test_csv_written_before_tokenization() -> None:
    # 分词阶段炸掉（jieba 缺失、Ctrl+C 等）不能让已抓完的榜单一个字节不落盘。
    from unittest.mock import patch

    import bilibili_ranker.cli as cli_module
    from bilibili_ranker.client import RankingFetchResult

    fetched = RankingFetchResult(
        fetched_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        items=({"bvid": "BV1aa0000000", "title": "魔方教程"},),
    )

    def boom(self: object, _: object) -> dict[str, int]:
        raise RuntimeError("缺少 jieba，请先安装项目依赖")

    with tempfile.TemporaryDirectory() as directory:
        with patch.object(cli_module, "fetch_all_ranking", lambda **_: fetched):
            with patch.object(cli_module.TitleAnalyzer, "analyze", boom):
                assert main(["--output-dir", directory]) == 1
        csv_files = list(Path(directory).glob("ranking_*.csv"))
        assert len(csv_files) == 1, csv_files
        with csv_files[0].open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        assert [row["BV号"] for row in rows] == ["BV1aa0000000"]


def test_main_reconfigures_stdout_to_utf8() -> None:
    # Windows 上重定向 stdout 时用的是 ANSI 代码页，含中文键的摘要 JSON 会抛
    # UnicodeEncodeError：活干完了却报错退出。main 开头必须把两个流都拉到 UTF-8。
    import io
    import sys
    from unittest.mock import patch

    calls: list[dict[str, object]] = []

    class RecordingStream(io.StringIO):
        def reconfigure(self, **kwargs: object) -> None:
            calls.append(kwargs)

    with (
        patch.object(sys, "stdout", RecordingStream()),
        patch.object(sys, "stderr", RecordingStream()),
    ):
        try:
            main(["--timeout", "0"])  # parser.error 退出前 reconfigure 已执行
        except SystemExit:
            pass

    assert len(calls) == 2, calls
    for kwargs in calls:
        assert kwargs == {"encoding": "utf-8", "errors": "replace"}


def test_cli_success_path_writes_both_outputs() -> None:
    # 端到端成功路径：summary JSON 字段齐全，CSV 与 PNG 都真实落盘。
    from unittest.mock import patch

    import pytest

    import bilibili_ranker.cli as cli_module
    from bilibili_ranker.client import RankingFetchResult
    from bilibili_ranker.fonts import resolve_font_path

    try:
        resolve_font_path(None)
    except RuntimeError:
        pytest.skip("环境无 CJK 字体")

    fetched = RankingFetchResult(
        fetched_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        items=(
            {"bvid": "BV1aa0000000", "title": "魔方教程 入门"},
            {"bvid": "BV1aa0000000", "title": "重复项"},  # 去重命中
            {"bvid": "bad", "title": "非法 bvid"},  # 解析拒绝
        ),
    )

    with tempfile.TemporaryDirectory() as directory:
        with patch.object(cli_module, "fetch_all_ranking", lambda **_: fetched):
            assert main(["--output-dir", directory, "--width", "200", "--height", "100"]) == 0

        csv_files = list(Path(directory).glob("ranking_*.csv"))
        png_files = list(Path(directory).glob("wordcloud_*.png"))
        assert len(csv_files) == 1 and len(png_files) == 1
        assert png_files[0].read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
        assert not list(Path(directory).glob("*.tmp"))


def test_zero_pubdate_is_missing_not_1970() -> None:
    # B 站对部分视频下发 pubdate=0（时间未知）：直接格式化会写出 1970-01-01 08:00:00，
    # 看着像真实发布时间。当缺失处理，CSV 该列留空。
    record = VideoRankingRecord.from_api_item(
        {"bvid": "BV1aa0000000", "title": "魔方教程", "pubdate": 0},
        rank=1,
    )
    assert record.published_at is None
    # 正常时间戳仍要正确转成北京时间。
    normal = VideoRankingRecord.from_api_item(
        {"bvid": "BV1aa0000000", "title": "魔方教程", "pubdate": 1704067200},
        rank=1,
    )
    assert normal.published_at == "2024-01-01 08:00:00"


def test_wordcloud_failure_keeps_csv_and_exits_zero() -> None:
    # 词云渲染失败是降级路径：警告 + CSV 保留 + 退出码 0，不能升级成失败。
    from unittest.mock import patch

    import bilibili_ranker.cli as cli_module
    from bilibili_ranker.client import RankingFetchResult

    fetched = RankingFetchResult(
        fetched_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        items=({"bvid": "BV1aa0000000", "title": "魔方教程"},),
    )

    def boom(*_: object, **__: object) -> Path:
        raise RuntimeError("找不到字体")

    with tempfile.TemporaryDirectory() as directory:
        with patch.object(cli_module, "fetch_all_ranking", lambda **_: fetched):
            with patch.object(cli_module, "render_wordcloud", boom):
                assert main(["--output-dir", directory]) == 0
        assert len(list(Path(directory).glob("ranking_*.csv"))) == 1
        assert not list(Path(directory).glob("wordcloud_*.png"))


def test_empty_frequencies_still_writes_csv() -> None:
    # 标题清洗后无词元（全是停用词/表情）时只输出 CSV，退出码仍为 0。
    from unittest.mock import patch

    import bilibili_ranker.cli as cli_module
    from bilibili_ranker.client import RankingFetchResult

    fetched = RankingFetchResult(
        fetched_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        items=({"bvid": "BV1aa0000000", "title": "🎉🎉🎉"},),
    )

    with tempfile.TemporaryDirectory() as directory:
        with patch.object(cli_module, "fetch_all_ranking", lambda **_: fetched):
            assert main(["--output-dir", directory]) == 0
        assert len(list(Path(directory).glob("ranking_*.csv"))) == 1
        assert not list(Path(directory).glob("wordcloud_*.png"))


def test_output_bundle_releases_placeholder_when_png_taken() -> None:
    # PNG 已被占用时整对跳号，且刚占位的空 CSV 必须撤销，否则留下 0 字节垃圾文件。
    from bilibili_ranker.storage import create_output_bundle

    moment = datetime(2024, 1, 1, tzinfo=timezone.utc)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "wordcloud_20240101T000000Z.png").write_bytes(b"x")

        bundle = create_output_bundle(root, moment)
        assert bundle.ranking_csv.name == "ranking_20240101T000000Z-2.csv"
        assert not (root / "ranking_20240101T000000Z.csv").exists()


def test_output_bundle_suffix_keeps_pair_aligned() -> None:
    # 同一秒重复运行时追加 -2/-3 后缀，且 CSV 与 PNG 共用同一后缀：
    # 拆开编号会产出 ranking_...-3.csv 配 wordcloud_...-2.png 这种无法配对的组合。
    from bilibili_ranker.storage import create_output_bundle

    moment = datetime(2024, 1, 1, tzinfo=timezone.utc)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        first = create_output_bundle(root, moment)
        assert first.ranking_csv.name == "ranking_20240101T000000Z.csv"
        assert first.wordcloud_png.name == "wordcloud_20240101T000000Z.png"

        first.ranking_csv.write_text("x", encoding="utf-8")
        second = create_output_bundle(root, moment)
        assert second.ranking_csv.name == "ranking_20240101T000000Z-2.csv"
        assert second.wordcloud_png.name == "wordcloud_20240101T000000Z-2.png"

        # 只有 PNG 被占用时同样整对跳号，绝不覆盖已有文件。
        second.wordcloud_png.write_text("x", encoding="utf-8")
        third = create_output_bundle(root, moment)
        assert third.ranking_csv.name == "ranking_20240101T000000Z-3.csv"
        assert third.wordcloud_png.name == "wordcloud_20240101T000000Z-3.png"


def test_user_agent_read_at_call_time() -> None:
    # _UA 绑成函数默认值时，进程内改 BILIBILI_UA 不生效也无法测试覆盖。
    import os
    from unittest.mock import patch

    from bilibili_ranker.client import fetch_all_ranking

    seen: list[str] = []

    def fake_get(self: requests.Session, url: str, **kwargs: object) -> requests.Response:
        headers = kwargs.get("headers") or {}
        seen.append(headers["User-Agent"])
        return _fake_json_response({"code": 0, "data": {"list": []}})

    old = os.environ.get("BILIBILI_UA")
    try:
        os.environ["BILIBILI_UA"] = "custom-ua/1.0"
        with patch.object(requests.Session, "get", fake_get):
            fetch_all_ranking()
        assert seen == ["custom-ua/1.0"]

        del os.environ["BILIBILI_UA"]
        seen.clear()
        with patch.object(requests.Session, "get", fake_get):
            fetch_all_ranking()
        assert seen and "Chrome" in seen[0]  # 回落到内置默认 UA
    finally:
        if old is None:
            os.environ.pop("BILIBILI_UA", None)
        else:
            os.environ["BILIBILI_UA"] = old


def test_fontconfig_match_verifies_family() -> None:
    # fc-match 找不到 family 时会静默回退到默认字体，必须比对返回的 family 名，
    # 否则 Linux 上可能拿到一个不含中日韩字形的字体。
    import subprocess
    from unittest.mock import patch

    from bilibili_ranker import fonts as fonts_module

    with tempfile.TemporaryDirectory() as directory:
        font = Path(directory) / "wqy-zenhei.ttc"
        font.write_bytes(b"ttcf")

        def fake_run(argv: tuple[str, ...], **_: object) -> subprocess.CompletedProcess:
            family = argv[-1]
            # 只有最后一个 family 精确命中，前面的都回退到 DejaVu Sans。
            if family == "WenQuanYi Zen Hei":
                stdout = f"{font}\nWenQuanYi Zen Hei,文泉驿正黑\n"
            else:
                stdout = f"{font}\nDejaVu Sans\n"
            return subprocess.CompletedProcess(argv, 0, stdout, "")

        with patch.object(fonts_module.shutil, "which", lambda _: "fc-match"):
            with patch.object(fonts_module.subprocess, "run", fake_run):
                assert fonts_module._fontconfig_match() == font.resolve()

                # 所有 family 都回退时必须返回 None，而不是交出错字体。
                def always_fallback(argv: tuple[str, ...], **_: object):
                    return subprocess.CompletedProcess(argv, 0, f"{font}\nDejaVu Sans\n", "")

                with patch.object(fonts_module.subprocess, "run", always_fallback):
                    assert fonts_module._fontconfig_match() is None


def test_standard_font_roots_include_windows_user_dir() -> None:
    # 「为我安装」的字体只在用户目录，漏掉这条路径 Windows 上会误报找不到字体。
    import os
    from unittest.mock import patch

    from bilibili_ranker.fonts import _standard_font_roots

    with patch.dict(os.environ, {"WINDIR": r"C:\Windows", "LOCALAPPDATA": r"C:\Users\u\AppData"}):
        roots = [str(path) for path in _standard_font_roots()]
    assert any(root.endswith(os.path.join("Windows", "Fonts")) for root in roots)
    assert any("Microsoft" in root and root.endswith("Fonts") for root in roots)


def test_missing_resource_dir_exits_one() -> None:
    # README 承诺 --resource-dir 缺文件退出码 1（值有效但资源不可用），且必须在发请求前报错。
    from unittest.mock import patch

    import bilibili_ranker.cli as cli_module

    def boom(**_: object) -> object:
        raise AssertionError("资源缺失时不应该发起网络请求")

    with tempfile.TemporaryDirectory() as empty_dir:
        with patch.object(cli_module, "fetch_all_ranking", boom):
            assert main(["--resource-dir", empty_dir]) == 1


def test_resource_dir_override_loads_custom_words() -> None:
    # README 承诺 --resource-dir 覆盖内置停用词目录，正向路径此前零覆盖：
    # str 与 Path 两种入参都要能加载，自定义词整体替换目录而非追加。
    import shutil

    from bilibili_ranker.stopwords import load_stopword_policy

    source = (
        Path(__file__).resolve().parents[1] / "src" / "bilibili_ranker" / "resources" / "stopwords"
    )
    with tempfile.TemporaryDirectory() as d:
        directory = Path(d)
        for name in ("custom_stopwords.txt", "allowlist.txt"):
            shutil.copy(source / name, directory / name)

        # str 入参（覆盖 isinstance 的 str 分支）加载内置内容。
        policy = load_stopword_policy(str(directory))
        assert "视频" in policy.stopwords
        assert "ai" in policy.allowlist
        assert "ai" not in policy.stopwords

        # 覆盖版 custom_stopwords.txt 整体替换默认词表，allowlist 照常生效。
        (directory / "custom_stopwords.txt").write_text(
            "# 覆盖版\n魔方教程\n", encoding="utf-8-sig"
        )
        policy = load_stopword_policy(directory)
        assert "魔方教程" in policy.stopwords
        assert "视频" not in policy.stopwords
        assert "ai" in policy.allowlist
        assert "ai" not in policy.stopwords


def test_cli_rejects_out_of_range_args() -> None:
    # 尺寸/最大词数/最短词长取 0、语言列表剥空、rid 为负，都属 argparse 层的取值越界，退出码 2。
    for argv in (
        ["--width", "0"],
        ["--height", "-1"],
        ["--max-words", "0"],
        ["--minimum-token-length", "0"],
        ["--languages", " ,,"],
        ["--rid", "-1"],
    ):
        try:
            main(argv)
        except SystemExit as exc:
            assert exc.code == 2, argv
        else:
            raise AssertionError(f"{argv} 应该被拒绝")


def test_analyzer_keeps_allowlisted_word() -> None:
    # allowlist 的短路返回分支此前零覆盖：分析结果里必须保留 ai 这类保留词。
    analyzer = TitleAnalyzer(load_stopword_policy())
    record = VideoRankingRecord.from_api_item({"bvid": "BV1aa0000000", "title": "AI 教程"}, rank=1)
    assert analyzer.analyze([record]) == {"ai": 1, "教程": 1}


def test_client_rejects_malformed_api_shapes() -> None:
    # 响应校验阶梯逐级都有专属错误信息，此前整段零覆盖：接口被代理劫持或字段改名时，
    # 每一级都要给出可排查的 BilibiliAPIError，而不是 KeyError 逸出。
    from unittest.mock import patch

    from bilibili_ranker.client import BilibiliAPIError, fetch_all_ranking

    malformed_payloads = [
        None,  # 200 + 垃圾 body，重试耗尽后仍要报「不是有效 JSON」
        ["不是", "对象"],  # 根节点不是 JSON 对象
        {"code": -404, "message": "啥都木有"},  # 业务码非 0
        {"code": 0},  # 缺 data
        {"code": 0, "data": {"list": "不是数组"}},
        {"code": 0, "data": {"list": ["不是对象"]}},
    ]
    for payload in malformed_payloads:

        def fake_get(
            self: requests.Session,
            url: str,
            *,
            _payload: object = payload,
            **kwargs: object,
        ) -> requests.Response:
            return _fake_json_response(_payload, 200)

        with patch.object(requests.Session, "get", fake_get):
            try:
                fetch_all_ranking()
            except BilibiliAPIError:
                pass
            else:
                raise AssertionError(f"畸形响应未被拒绝：{payload!r}")


def test_refresh_buvid_silent_on_garbage() -> None:
    # SPI 响应的各种畸形（非 200、垃圾 JSON、结构不对）都静默跳过，cookie 一个都不写。
    from unittest.mock import patch

    from bilibili_ranker.client import SPI_API_URL, _refresh_buvid, build_session

    garbage_responses = [
        _fake_json_response({"code": 0, "data": {"b_3": "b3", "b_4": "b4"}}, 500),
        _fake_json_response(["不是", "对象"]),
        _fake_json_response({"code": 0}),
        _fake_json_response({"code": 0, "data": "不是对象"}),
    ]
    # 200 + 坏 JSON 单独构造（helper 只能产合法 JSON），json() 抛 ValueError 后同样静默。
    invalid_json = requests.Response()
    invalid_json.status_code = 200
    invalid_json._content = b"<html>not json</html>"
    garbage_responses.insert(1, invalid_json)
    for response in garbage_responses:

        def fake_get(
            self: requests.Session,
            url: str,
            *,
            _response: requests.Response = response,
            **kwargs: object,
        ) -> requests.Response:
            assert url == SPI_API_URL
            return _response

        session = build_session()
        try:
            with patch.object(requests.Session, "get", fake_get):
                _refresh_buvid(session, user_agent="ua", timeout_seconds=5.0)
            assert session.cookies.get_dict() == {}
        finally:
            session.close()


def test_fetch_reports_persistent_truncation() -> None:
    # 每一轮 body 都被截断时：重试耗尽后按请求失败上报，且请求总数不超轮次上限。
    import http.server
    import threading

    import bilibili_ranker.client as client_module
    from bilibili_ranker.client import BilibiliAPIError

    counter = {"n": 0}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 接口
            counter["n"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "200")
            self.end_headers()
            self.wfile.write(b'{"code": 0, "data": {"list": [{"bvid": "BV1aa')
            self.close_connection = True

        def log_message(self, *_: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    original = client_module.RANKING_API_URL
    client_module.RANKING_API_URL = f"http://127.0.0.1:{server.server_port}/x"
    try:
        try:
            client_module.fetch_all_ranking(timeout_seconds=5)
        except BilibiliAPIError as exc:
            assert "请求失败" in str(exc)
        else:
            raise AssertionError("持续截断未抛出 BilibiliAPIError")
        assert counter["n"] == client_module._RISK_CONTROL_ATTEMPTS, counter
    finally:
        client_module.RANKING_API_URL = original
        server.shutdown()
        server.server_close()


def test_lazy_imports_report_friendly_errors() -> None:
    # jieba / stopwordsiso / wordcloud 缺依赖时都要给出中文安装提示，而不是 ImportError。
    import sys as _sys
    from unittest.mock import patch

    from bilibili_ranker.cleaner import _jieba_lcut
    from bilibili_ranker.stopwords import load_stopword_policy
    from bilibili_ranker.wordcloud import render_wordcloud

    with patch.dict(_sys.modules, {"jieba": None}):
        try:
            _jieba_lcut("测试")
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


def test_font_not_found_when_system_has_none() -> None:
    # 标准目录与 fontconfig 全部落空时，必须抛带指引的 FontNotFoundError 而非静默返回。
    from unittest.mock import patch

    from bilibili_ranker import fonts as fonts_module
    from bilibili_ranker.fonts import FontNotFoundError, resolve_font_path

    with patch.object(fonts_module, "_standard_font_roots", lambda: iter(())):
        with patch.object(fonts_module.shutil, "which", lambda _: None):
            try:
                resolve_font_path(None)
            except FontNotFoundError as exc:
                assert "--font-path" in str(exc)
            else:
                raise AssertionError("无字体环境未抛出 FontNotFoundError")


def test_render_wordcloud_direct_guards() -> None:
    # 绕过 CLI 直接调 render_wordcloud 时，空词频与非法尺寸都要在导入前被拦下；
    # 断言自家错误消息：变异若跳过校验，会坠入 wordcloud 库的同型异常而被掩护。
    from bilibili_ranker.wordcloud import render_wordcloud

    for kwargs in (
        {"frequencies": {}, "output_path": "unused.png"},
        {"frequencies": {"词": 1}, "output_path": "unused.png", "width": 0},
        {"frequencies": {"词": 1}, "output_path": "unused.png", "max_words": 0},
    ):
        try:
            render_wordcloud(**kwargs)
        except ValueError as exc:
            assert str(exc) in ("词云尺寸和最大词数必须大于 0", "词频为空，无法生成词云"), exc
        else:
            raise AssertionError(f"非法参数未被拒绝：{kwargs}")


def test_storage_accepts_naive_datetime() -> None:
    # naive datetime 按 UTC 处理，不抛 NaiveDatetime 相关异常。
    from bilibili_ranker.storage import create_output_bundle

    with tempfile.TemporaryDirectory() as directory:
        bundle = create_output_bundle(Path(directory), datetime(2024, 1, 1))
        assert bundle.ranking_csv.name == "ranking_20240101T000000Z.csv"


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


def test_keyboard_interrupt_exits_130() -> None:
    # Ctrl+C 必须走退出码 130（README 已承诺），不能以 KeyboardInterrupt traceback 收场。
    from unittest.mock import patch

    import bilibili_ranker.cli as cli_module

    def interrupt(**_: object) -> object:
        raise KeyboardInterrupt

    with tempfile.TemporaryDirectory() as directory:
        with patch.object(cli_module, "fetch_all_ranking", interrupt):
            assert main(["--output-dir", directory]) == 130


def test_failed_run_removes_empty_placeholder_csv() -> None:
    # create_output_bundle 的 O_EXCL 占位若在 write_records_csv 之前崩溃，会永久残留
    # 0 字节 CSV 并占掉编号（下次运行跳到 -2）。失败路径必须清理空占位。
    from unittest.mock import patch

    import bilibili_ranker.cli as cli_module
    from bilibili_ranker.client import RankingFetchResult

    fetched = RankingFetchResult(
        fetched_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        items=({"bvid": "BV1aa0000000", "title": "魔方教程"},),
    )

    def boom(*_: object, **__: object) -> Path:
        raise KeyboardInterrupt

    with tempfile.TemporaryDirectory() as directory:
        with patch.object(cli_module, "fetch_all_ranking", lambda **_: fetched):
            with patch.object(cli_module, "write_records_csv", boom):
                assert main(["--output-dir", directory]) == 130
        assert not list(Path(directory).glob("ranking_*.csv"))

        # 清理只针对 0 字节占位：写成功后的 CSV 绝不能被后续失败误删。
        with patch.object(cli_module, "fetch_all_ranking", lambda **_: fetched):
            with patch.object(cli_module.TitleAnalyzer, "analyze", boom):
                assert main(["--output-dir", directory]) == 130
        remaining = list(Path(directory).glob("ranking_*.csv"))
        assert len(remaining) == 1 and remaining[0].stat().st_size > 0


def test_version_exported() -> None:
    # 库调用方需要 __version__；未安装时回落到 dev 占位而不是 ImportError。
    import bilibili_ranker

    assert isinstance(bilibili_ranker.__version__, str)
    assert bilibili_ranker.__version__


def test_explicit_font_errors_surface_before_network() -> None:
    # --font-path / BILIBILI_WORDCLOUD_FONT 是用户显式输入，路径写错一个字母时必须与
    # 停用词错误同理在发请求前退出 1；若被词云阶段的降级吞成警告（退出码 0），定时任务
    # 会把没有词云的一次运行误判为成功。自动探测失败才保留「仅警告」降级。
    import os
    from unittest.mock import patch

    import bilibili_ranker.cli as cli_module

    def boom(**_: object) -> object:
        raise AssertionError("显式字体路径无效时不应该发起网络请求")

    with tempfile.TemporaryDirectory() as directory:
        typo = str(Path(directory) / "typo.ttf")
        with patch.object(cli_module, "fetch_all_ranking", boom):
            assert main(["--output-dir", directory, "--font-path", typo]) == 1

            old = os.environ.get("BILIBILI_WORDCLOUD_FONT")
            try:
                os.environ["BILIBILI_WORDCLOUD_FONT"] = typo
                assert main(["--output-dir", directory]) == 1
            finally:
                if old is None:
                    os.environ.pop("BILIBILI_WORDCLOUD_FONT", None)
                else:
                    os.environ["BILIBILI_WORDCLOUD_FONT"] = old


def test_client_rejects_negative_rid() -> None:
    # 非法 rid 与非法 timeout 同类：公共 API 入口先挡下，且绝不发起网络请求。
    from unittest.mock import patch

    import bilibili_ranker.client as client_module
    from bilibili_ranker.client import BilibiliAPIError

    def boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("非法 rid 不应该发起网络请求")

    with patch.object(requests.Session, "get", boom):
        try:
            client_module.fetch_all_ranking(rid=-1)
        except BilibiliAPIError as exc:
            assert "-1" in str(exc)
        else:
            raise AssertionError("负数 rid 未被拒绝")


def test_rid_passes_through_to_query_string() -> None:
    # 分区透传必须真的落进 query：若写死回 rid=0，选了分区也会静默抓成全站榜。
    # 单次成功响应即可，重试语义由其他测试覆盖。
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs

    from bilibili_ranker import client as client_module

    ranking_paths: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            if self.path.startswith("/spi"):
                body, status = {"code": 0, "data": {"b_3": "b3", "b_4": "b4"}}, 200
            else:
                ranking_paths.append(self.path)
                body, status = {"code": 0, "data": {"list": []}}, 200
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
        result = client_module.fetch_all_ranking(timeout_seconds=5.0, rid=4)
    finally:
        client_module.RANKING_API_URL = original_ranking
        client_module.SPI_API_URL = original_spi
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert result.items == ()
    assert parse_qs(ranking_paths[0].split("?", 1)[1]) == {"rid": ["4"], "type": ["all"]}


if __name__ == "__main__":
    import pytest as _pytest

    # 动态收集，避免手工罗列漏掉新测试导致静默漏跑。
    # pytest.skip() 抛 _pytest.outcomes.Skipped，不捕获会中断循环导致后续测试静默漏跑。
    # 断言失败也不能中断：否则第一条失败之后的测试全部静默漏跑，最后仍打印 ok。
    _failures: list[str] = []
    for _name, _function in sorted(globals().items()):
        if _name.startswith("test_") and callable(_function):
            try:
                _function()
            except _pytest.skip.Exception as _e:
                print(f"SKIP {_name}: {_e}")
            except BaseException as _e:  # noqa: BLE001 - 聚合报告，最后再退出
                print(f"FAIL {_name}: {type(_e).__name__}: {_e}")
                _failures.append(_name)
    if _failures:
        raise SystemExit(f"{len(_failures)} 个测试失败：{', '.join(_failures)}")
    print("ok")
