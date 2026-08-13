"""排行榜 CSV 的原子写入和输出路径管理。"""

from __future__ import annotations

import csv
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .models import VideoRankingRecord


@dataclass(frozen=True, slots=True)
class OutputBundle:
    ranking_csv: Path
    wordcloud_png: Path


def _utc_label(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def create_output_bundle(root: str | Path, fetched_at: datetime) -> OutputBundle:
    base = Path(root)
    label = _utc_label(fetched_at)
    return OutputBundle(
        ranking_csv=base / f"ranking_{label}.csv",
        wordcloud_png=base / f"wordcloud_{label}.png",
    )


def _temporary_path(destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    return destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")


def _atomic_csv_write(
    destination: Path,
    fieldnames: Sequence[str],
    rows: Iterable[Mapping[str, Any]],
) -> Path:
    temporary = _temporary_path(destination)
    try:
        with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


RANKING_CSV_HEADERS = (
    "排名",
    "BV号",
    "视频链接",
    "视频标题",
    "视频分区",
    "主分区",
    "UP主",
    "发布时间（北京时间）",
    "视频时长（秒）",
    "播放量",
    "弹幕数",
    "评论数",
    "收藏数",
    "投币数",
    "分享数",
    "点赞数",
)


def _record_to_csv_row(record: VideoRankingRecord) -> dict[str, Any]:
    return {
        "排名": record.rank,
        "BV号": record.bvid,
        "视频链接": record.video_url,
        "视频标题": record.title,
        "视频分区": record.category_v2_name or record.category_name,
        "主分区": record.parent_category_v2_name,
        "UP主": record.uploader_name,
        "发布时间（北京时间）": record.published_at,
        "视频时长（秒）": record.duration_seconds,
        "播放量": record.view_count,
        "弹幕数": record.danmaku_count,
        "评论数": record.reply_count,
        "收藏数": record.favorite_count,
        "投币数": record.coin_count,
        "分享数": record.share_count,
        "点赞数": record.like_count,
    }


def write_records_csv(destination: Path, records: Iterable[VideoRankingRecord]) -> Path:
    return _atomic_csv_write(
        destination,
        RANKING_CSV_HEADERS,
        (_record_to_csv_row(record) for record in records),
    )
