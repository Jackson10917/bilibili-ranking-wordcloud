"""从原始标题重算证据，保守更新可撤销的自动停用词层。"""

from __future__ import annotations

import csv
import json
import os
import re
from collections import Counter, defaultdict
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from .cleaner import TitleAnalyzer, load_default_dictionary
from .stopwords import load_stopword_policy
from .storage import _atomic_csv_write, temporary_path, write_frequencies_csv, write_trend_csv

# ponytail: 统计不能判断语义；仅明确的互动套话自动生效，其他高频词留作候选。
# 如需扩展语义覆盖，先审查此集合，不按热度或「掉出」状态自动删词。
_AUTO_NOISE = frozenset(
    "一键三连 求三连 三连 求点赞 求关注 求收藏 求投币 点个赞 点赞三连 "
    "下期再见 敬请期待 不见不散 持续更新 点赞关注 感谢观看".split()
)
_RANKING = re.compile(r"ranking_(\d{8}T\d{6}Z)(?:-(\d+))?\.csv")


def _write_text(path: Path, text: str) -> None:
    temporary = temporary_path(path)
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def optimize_stopwords(
    data_dir: Path,
    output_dir: Path,
    *,
    min_days: int = 7,
    min_day_ratio: float = 0.6,
    min_total: int = 20,
) -> dict[str, Any]:
    """原始快照不修改；baseline/current、候选证据和自动词表写入输出目录。"""
    if min_days < 1 or min_total < 1 or not 0 < min_day_ratio <= 1:
        raise ValueError("min-days/min-total 必须为正数，min-day-ratio 必须在 (0, 1] 内")
    if not data_dir.is_dir():
        raise ValueError(f"数据目录不存在：{data_dir}")
    if output_dir.resolve() == data_dir.resolve() or data_dir.resolve().is_relative_to(
        output_dir.resolve()
    ):
        raise ValueError("分析输出目录不能等于原始数据目录或包含原始数据目录")
    snapshots: dict[str, tuple[tuple[str, int], Path]] = {}
    for path in data_dir.glob("ranking_*.csv"):
        match = _RANKING.fullmatch(path.name)
        if match:
            key = (match[1], int(match[2] or 0))
            day = match[1][:8]
            if day not in snapshots or key > snapshots[day][0]:
                snapshots[day] = (key, path)
    if not snapshots:
        raise ValueError("没有可分析的带时间戳榜单快照")

    policy = load_stopword_policy()
    load_default_dictionary()
    analyzer = TitleAnalyzer(policy)
    daily: dict[str, dict[str, int]] = {}
    days: Counter[str] = Counter()
    videos: dict[str, set[str]] = defaultdict(set)
    categories: dict[str, set[str]] = defaultdict(set)
    samples: dict[str, list[str]] = defaultdict(list)
    # 先读完并验证，损坏快照不应被静默当成正常缺席日。
    for day, (_, path) in sorted(snapshots.items()):
        counts: Counter[str] = Counter()
        seen: set[str] = set()
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if not {"视频标题", "BV号"}.issubset(reader.fieldnames or []):
                raise ValueError(f"榜单字段不完整：{path.name}")
            for row in reader:
                title, bvid = row.get("视频标题"), row.get("BV号")
                if not title or not bvid:
                    raise ValueError(f"榜单有空标题或 BV 号：{path.name}")
                if bvid in seen:
                    continue
                seen.add(bvid)
                tokens = analyzer.analyze_titles([title.removeprefix("'")])
                counts.update(tokens)
                for word in tokens:
                    videos[word].add(bvid)
                    category = row.get("主分区") or row.get("视频分区")
                    if category:
                        categories[word].add(category)
                    if title not in samples[word] and len(samples[word]) < 3:
                        samples[word].append(title)
        if not seen:
            raise ValueError(f"空榜单：{path.name}")
        daily[day] = dict(counts.most_common())
        days.update(counts.keys())

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    # 先在旁边生成完整结果；分词或暂存生成失败时保留上轮输出。
    with TemporaryDirectory(prefix=".stopword-", dir=output_dir.parent) as temporary:
        staging = Path(temporary)
        summary = _export_analysis(
            staging,
            daily,
            days,
            videos,
            categories,
            samples,
            min_days=min_days,
            min_day_ratio=min_day_ratio,
            min_total=min_total,
            allowlist=policy.allowlist,
        )
        summary["output_dir"] = str(output_dir)
        _write_text(
            staging / "stopword_summary.json",
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        )
        for path in staging.rglob("*"):
            if path.is_file():
                destination = output_dir / path.relative_to(staging)
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(path, destination)
        for directory in (output_dir / "baseline", output_dir / "current"):
            expected = {f"word_frequency_{day}T000000Z.csv" for day in daily}
            expected.add("word_frequency_aggregate.csv")
            if len(daily) > 7:
                expected.add("word_frequency_trend.csv")
            for path in directory.glob("word_frequency_*.csv"):
                owned = re.fullmatch(
                    r"word_frequency_(?:\d{8}T000000Z|aggregate|trend)\.csv", path.name
                )
                if owned and path.name not in expected:
                    path.unlink()
            # 优化命令不渲染 PNG，旧图不能冒充新词表结果。
            (directory / "wordcloud_aggregate.png").unlink(missing_ok=True)
    return summary


def _export_analysis(
    output_dir: Path,
    daily: dict[str, dict[str, int]],
    days: Counter[str],
    videos: dict[str, set[str]],
    categories: dict[str, set[str]],
    samples: dict[str, list[str]],
    *,
    min_days: int,
    min_day_ratio: float,
    min_total: int,
    allowlist: frozenset[str],
) -> dict[str, Any]:
    from .cli import _merged_history, _trend_rows

    baseline = output_dir / "baseline"
    current = output_dir / "current"
    for day, frequencies in daily.items():
        write_frequencies_csv(baseline / f"word_frequency_{day}T000000Z.csv", frequencies)
    aggregate = _merged_history(baseline)
    write_frequencies_csv(baseline / "word_frequency_aggregate.csv", aggregate)
    trend = _trend_rows(baseline)
    if trend is not None:
        write_trend_csv(baseline / "word_frequency_trend.csv", trend)
    trend_by_word = {row["词"]: row for row in trend or []}

    rows: list[dict[str, Any]] = []
    automatic: set[str] = set()
    for word, total in aggregate.items():
        ratio = days[word] / len(daily)
        if days[word] < min_days or ratio < min_day_ratio or total < min_total:
            continue
        evidence = trend_by_word.get(word, {})
        previous = evidence.get("上期词频") or 0
        recent = evidence.get("本期词频") or 0
        stable = previous > 0 and 0.5 <= recent / previous <= 2
        # 不足两个完整 7 日窗口不自动应用；保留词永远优先。
        apply = (
            word in _AUTO_NOISE
            and word not in allowlist
            and len(daily) >= 14
            and len(videos[word]) >= 5
            and len(categories[word]) >= 3
            and stable
        )
        if apply:
            automatic.add(word)
        rows.append(
            {
                "词": word,
                "出现天数": days[word],
                "天数占比": round(ratio, 4),
                "累计词频": total,
                "独立视频数": len(videos[word]),
                "分区数": len(categories[word]),
                "趋势状态": evidence.get("状态", "不足两期"),
                "上期词频": previous,
                "本期词频": recent,
                "自动生效": "是" if apply else "否",
                "示例标题": " | ".join(samples[word]),
            }
        )
    fields = (
        "词",
        "出现天数",
        "天数占比",
        "累计词频",
        "独立视频数",
        "分区数",
        "趋势状态",
        "上期词频",
        "本期词频",
        "自动生效",
        "示例标题",
    )
    # 标题是外部输入；CSV 转义与榜单导出保持一致。
    from .storage import _spreadsheet_safe

    _atomic_csv_write(
        output_dir / "stopword_candidates.csv",
        fields,
        (
            {
                key: _spreadsheet_safe(value) if isinstance(value, str) else value
                for key, value in row.items()
            }
            for row in rows
        ),
    )
    _write_text(
        output_dir / "auto_stopwords.txt",
        "# 自动层，每次重算；保留词优先\n" + "".join(word + "\n" for word in sorted(automatic)),
    )
    for day, frequencies in daily.items():
        filtered = {word: count for word, count in frequencies.items() if word not in automatic}
        write_frequencies_csv(current / f"word_frequency_{day}T000000Z.csv", filtered)
    write_frequencies_csv(current / "word_frequency_aggregate.csv", _merged_history(current))
    current_trend = _trend_rows(current)
    if current_trend is not None:
        write_trend_csv(current / "word_frequency_trend.csv", current_trend)
    summary = {
        "snapshot_days": len(daily),
        "candidates": len(rows),
        "automatic_words": sorted(automatic),
        "min_days": min_days,
        "min_day_ratio": min_day_ratio,
        "min_total": min_total,
        "output_dir": str(output_dir),
    }
    return summary
