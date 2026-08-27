"""API 字段解析与数据模型的回归测试。

由 tests/test_core.py 按源码模块拆分而来；统一由 pytest 收集运行：python -m pytest tests
"""

from __future__ import annotations

import json
from pathlib import Path

from bilibili_ranker.models import VideoRankingRecord, parse_ranking_records


def test_from_api_item_maps_every_field() -> None:
    # 逐字段锁死 API 字段名映射、video_url 拼装与发布时间转换：上游改字段名时，
    # 缺断言的字段会让测试照旧全绿而 CSV 整列静默变空。
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


def test_real_response_fixture_parses_cleanly() -> None:
    # 手写 dict 只覆盖想象中的形状；fixtures 里是真实响应的前 5 条 items 原样截取
    # （信封按接口契约最小重构），上游字段改名或结构漂移在这里先于线上暴露。
    fixture = Path(__file__).resolve().parent / "fixtures" / "ranking_v2_sample.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    items = payload["data"]["list"]
    records, rejected = parse_ranking_records(items)
    assert rejected == 0
    assert len(records) == len(items) >= 5
    assert all(record.bvid.startswith("BV") for record in records)
    assert records[0].view_count is not None
    assert records[0].title


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
