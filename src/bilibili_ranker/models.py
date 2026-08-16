"""将 B 站排行榜响应转换为稳定、扁平的数据模型。"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

_CN_TIMEZONE = timezone(timedelta(hours=8))

# bvid 直接拼进 video_url、也裸写进 CSV 的 BV号 列。校验格式，脏值就不会经这两列外流。
_BVID_PATTERN = re.compile(r"BV[0-9A-Za-z]{10}")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    """只接受字符串。list/dict 经 str() 会变成 "['BV1xx']" 这种垃圾字段，直接判空。"""

    return value.strip() if isinstance(value, str) else ""


def _optional_int(value: Any) -> int | None:
    # bool 是 int 子类，True 会被静默解析成播放量 1，而不是判为脏数据。
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None


def _timestamp_to_datetime_text(value: int | None) -> str | None:
    if value is None:
        return None
    try:
        moment = datetime.fromtimestamp(value, tz=_CN_TIMEZONE)
    except (OSError, OverflowError, ValueError):
        return None
    return moment.strftime("%Y-%m-%d %H:%M:%S")


@dataclass(frozen=True, slots=True)
class VideoRankingRecord:
    """适合写入 CSV 和后续分析的排行榜记录。"""

    rank: int
    bvid: str
    video_url: str
    title: str
    category_name: str
    category_v2_name: str
    parent_category_v2_name: str
    uploader_name: str
    published_at: str | None
    duration_seconds: int | None
    view_count: int | None
    danmaku_count: int | None
    reply_count: int | None
    favorite_count: int | None
    coin_count: int | None
    share_count: int | None
    like_count: int | None

    @classmethod
    def from_api_item(
        cls,
        item: Mapping[str, Any],
        *,
        rank: int,
    ) -> VideoRankingRecord:
        bvid = _text(item.get("bvid"))
        title = _text(item.get("title"))
        if not _BVID_PATTERN.fullmatch(bvid):
            raise ValueError("bvid 格式非法")
        if not title:
            raise ValueError("缺少标题")

        owner = _mapping(item.get("owner"))
        stat = _mapping(item.get("stat"))
        # pubdate=0 是「时间未知」的哨兵值，不是 1970 年：直接格式化会往 CSV 里写
        # 1970-01-01 08:00:00，看着像真实发布时间。当缺失处理。
        published_timestamp = _optional_int(item.get("pubdate"))
        if published_timestamp == 0:
            published_timestamp = None

        return cls(
            rank=rank,
            bvid=bvid,
            video_url=f"https://www.bilibili.com/video/{bvid}",
            title=title,
            category_name=_text(item.get("tname")),
            category_v2_name=_text(item.get("tnamev2")),
            parent_category_v2_name=_text(item.get("pid_name_v2")),
            uploader_name=_text(owner.get("name")),
            published_at=_timestamp_to_datetime_text(published_timestamp),
            duration_seconds=_optional_int(item.get("duration")),
            view_count=_optional_int(stat.get("view")),
            danmaku_count=_optional_int(stat.get("danmaku")),
            reply_count=_optional_int(stat.get("reply")),
            favorite_count=_optional_int(stat.get("favorite")),
            coin_count=_optional_int(stat.get("coin")),
            share_count=_optional_int(stat.get("share")),
            like_count=_optional_int(stat.get("like")),
        )


def parse_ranking_records(
    items: Iterable[Any],
) -> tuple[list[VideoRankingRecord], int]:
    """容错解析，返回有效记录和拒绝条数。"""

    records: list[VideoRankingRecord] = []
    rejected_count = 0

    for rank, item in enumerate(items, start=1):
        mapping = _mapping(item)
        try:
            if not mapping:
                raise ValueError("记录不是 JSON 对象")
            records.append(
                VideoRankingRecord.from_api_item(
                    mapping,
                    rank=rank,
                )
            )
        except (TypeError, ValueError):
            rejected_count += 1

    return records, rejected_count
