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


RANKING_CSV_COLUMNS = (
    ("rank", "排名"),
    ("bvid", "BV号"),
    ("video_url", "视频链接"),
    ("title", "视频标题"),
    ("category_name", "视频分区"),
    ("parent_category_name", "主分区"),
    ("uploader_name", "UP主"),
    ("published_at", "发布时间（北京时间）"),
    ("duration_seconds", "视频时长（秒）"),
    ("view_count", "播放量"),
    ("danmaku_count", "弹幕数"),
    ("reply_count", "评论数"),
    ("favorite_count", "收藏数"),
    ("coin_count", "投币数"),
    ("share_count", "分享数"),
    ("like_count", "点赞数"),
)


def _record_to_csv_row(record: VideoRankingRecord) -> dict[str, Any]:
    data = {
        field: getattr(record, field)
        for field, _ in RANKING_CSV_COLUMNS
        if hasattr(record, field)
    }
    data["category_name"] = record.category_v2_name or record.category_name
    data["parent_category_name"] = record.parent_category_v2_name
    return {header: data.get(field) for field, header in RANKING_CSV_COLUMNS}


def write_records_csv(destination: Path, records: Iterable[VideoRankingRecord]) -> Path:
    fieldnames = tuple(header for _, header in RANKING_CSV_COLUMNS)
    return _atomic_csv_write(
        destination,
        fieldnames,
        (_record_to_csv_row(record) for record in records),
    )
