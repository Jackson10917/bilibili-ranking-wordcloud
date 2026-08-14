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
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bilibili_ranker.cleaner import TitleAnalyzer, deduplicate_records
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


if __name__ == "__main__":
    test_stopword_policy()
    test_deduplicate_records()
    test_parse_ranking_records_tolerance()
    test_accented_latin_tokens()
    test_math_symbols_not_merged_into_tokens()
    test_fetch_retries_risk_control()
    test_fetch_raises_when_risk_control_persists()
    test_fetch_reports_http_error()
    test_atomic_csv_write()
    print("ok")
