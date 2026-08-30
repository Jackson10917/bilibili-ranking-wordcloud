"""排行榜 CSV 的原子写入和输出路径管理。"""

from __future__ import annotations

import csv
import os
import uuid
from collections import Counter
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
    word_frequency_csv: Path


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
    word_frequency_csv = base / f"word_frequency_{label}.csv"
    # 同一秒多次运行重名，追加 -2/-3。用 O_CREAT|O_EXCL 原子占位而不是 exists()：
    # 并发进程会在检查与写入之间撞到同一编号，后写者覆盖先写者。PNG 与词频 CSV 只查
    # 存在性（编号由占位成败唯一确定）；词频 CSV 是 --aggregate 的累计输入，同秒重跑
    # 覆盖它会静默丢一天历史。
    counter = 2
    while True:
        try:
            os.close(os.open(ranking_csv, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        except FileExistsError:
            pass
        else:
            if not wordcloud_png.exists() and not word_frequency_csv.exists():
                return OutputBundle(
                    ranking_csv=ranking_csv,
                    wordcloud_png=wordcloud_png,
                    word_frequency_csv=word_frequency_csv,
                )
            # PNG 或词频 CSV 已被占用：撤掉刚占位的空 CSV，整对跳号。
            ranking_csv.unlink(missing_ok=True)
        ranking_csv = base / f"ranking_{label}-{counter}.csv"
        wordcloud_png = base / f"wordcloud_{label}-{counter}.png"
        word_frequency_csv = base / f"word_frequency_{label}-{counter}.csv"
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


# Excel/LibreOffice 把这些前缀开头的单元格当公式求值（CSV 注入）；Tab 和 CR 会被先剥掉、
# 露出后面的 = 继续当公式。
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


# 词来自投稿标题（用户可控），与榜单 CSV 走同一套公式前缀转义。行序沿用传入字典的降序。
FREQUENCY_CSV_HEADERS = ("词", "词频")


def write_frequencies_csv(destination: Path, frequencies: Mapping[str, int]) -> Path:
    rows = ({"词": _spreadsheet_safe(word), "词频": count} for word, count in frequencies.items())
    return _atomic_csv_write(destination, FREQUENCY_CSV_HEADERS, rows)


# 固定名滚动累计快照，每次原子覆盖；逐日数据就是目录里带时间戳的词频 CSV 本身。
AGGREGATE_FREQUENCY_CSV_NAME = "word_frequency_aggregate.csv"
AGGREGATE_WORDCLOUD_PNG_NAME = "wordcloud_aggregate.png"


def load_frequency_csvs(paths: Iterable[Path]) -> dict[str, int]:
    """读取 write_frequencies_csv 写出的词频 CSV 并按词求和，结果按词频降序。

    读取的是自家产物，只做对称的逆操作：词列剥掉公式前缀转义的单引号；
    个别坏文件（如历史 CSV 正被 Excel 占用）或坏行跳过，而不是炸掉整个聚合。
    """

    merged: Counter[str] = Counter()
    for path in paths:
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as stream:
                for row in csv.DictReader(stream):
                    word = (row.get("词") or "").removeprefix("'")
                    if not word:
                        continue
                    try:
                        count = int(row.get("词频") or "")
                    except ValueError:
                        continue
                    # 0/负词频来自损坏或手改的文件：照常渲染会产出看似正常的图，错误无法察觉。
                    if count < 1:
                        continue
                    merged[word] += count
        # UnicodeDecodeError（历史 CSV 被 Excel 另存成 ANSI）和 csv.Error（超长字段）
        # 都不是 OSError 子类。
        except (OSError, UnicodeDecodeError, csv.Error):
            continue
    return dict(merged.most_common())


# 词频趋势表：近 7 个快照日 vs 前 7 个的排名变化，固定名滚动快照，原子覆盖。
# 固定名同样不匹配 cli 的时间戳白名单，不会把自己并进累计或下一轮趋势。
TREND_CSV_NAME = "word_frequency_trend.csv"

TREND_CSV_HEADERS = ("词", "状态", "上期排名", "上期词频", "本期排名", "本期词频", "排名变化")


def write_trend_csv(destination: Path, rows: Iterable[Mapping[str, Any]]) -> Path:
    """写出趋势行（build_trend 的产物）；词与状态同源于投稿标题，沿用公式前缀转义。"""

    escaped = (
        {key: (_spreadsheet_safe(value) if key == "词" else value) for key, value in row.items()}
        for row in rows
    )
    return _atomic_csv_write(destination, TREND_CSV_HEADERS, escaped)
