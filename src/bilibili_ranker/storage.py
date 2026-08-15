"""排行榜 CSV 的原子写入和输出路径管理。"""

from __future__ import annotations

import csv
import os
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
    base.mkdir(parents=True, exist_ok=True)
    ranking_csv = base / f"ranking_{label}.csv"
    wordcloud_png = base / f"wordcloud_{label}.png"
    # 同一秒内多次运行会重名，追加 -2/-3 后缀。用 O_CREAT|O_EXCL 原子占位 CSV 而不是
    # exists()：后者在"检查"和"写入"之间有窗口，同秒并发的两个进程会拿到同一编号，
    # 后写者的 os.replace 直接覆盖先写者的结果。PNG 只做存在性检查，跟随 CSV 的编号
    # （编号由占位成败唯一确定，因此并发进程不会撞 PNG），避免词云不生成时留下空文件。
    counter = 2
    while True:
        try:
            os.close(os.open(ranking_csv, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        except FileExistsError:
            pass
        else:
            if not wordcloud_png.exists():
                return OutputBundle(ranking_csv=ranking_csv, wordcloud_png=wordcloud_png)
            # PNG 已被占用：撤掉刚占位的空 CSV，整对跳号。
            ranking_csv.unlink(missing_ok=True)
        ranking_csv = base / f"ranking_{label}-{counter}.csv"
        wordcloud_png = base / f"wordcloud_{label}-{counter}.png"
        counter += 1


def temporary_path(destination: Path) -> Path:
    """带 uuid 后缀的同目录临时路径；CSV 和 PNG 共用，避免并发互相踩踏。"""

    destination.parent.mkdir(parents=True, exist_ok=True)
    return destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")


def _atomic_csv_write(
    destination: Path,
    fieldnames: Sequence[str],
    rows: Iterable[Mapping[str, Any]],
) -> Path:
    temporary = temporary_path(destination)
    try:
        with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


# Excel/LibreOffice 会把以这些字符开头的单元格当公式求值（CSV 注入）。
# Tab 和 CR 也在列：Excel 会先剥掉它们，露出后面的 = 继续当公式。
# 所有来自 API 响应的字符串字段统一转义，不区分"官方枚举"与"用户可控"。
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

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


def _spreadsheet_safe(value: str) -> str:
    """给公式前缀加单引号，避免 Excel 把用户投稿的标题当公式求值。"""

    return "'" + value if value[:1] in _FORMULA_PREFIXES else value


def _record_to_csv_row(record: VideoRankingRecord) -> dict[str, Any]:
    return {
        "排名": record.rank,
        "BV号": record.bvid,
        "视频链接": record.video_url,
        "视频标题": _spreadsheet_safe(record.title),
        "视频分区": _spreadsheet_safe(record.category_v2_name or record.category_name),
        "主分区": _spreadsheet_safe(record.parent_category_v2_name),
        "UP主": _spreadsheet_safe(record.uploader_name),
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
