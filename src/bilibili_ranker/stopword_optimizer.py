"""从原始标题重算证据：机器提名停用词，强证据自动生效，其余由人工决定并永久记住。"""

from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from .cleaner import TitleAnalyzer, _import_jieba, load_default_dictionary
from .stopwords import load_stopword_policy, normalize_token
from .storage import _atomic_csv_write, temporary_path, write_frequencies_csv, write_trend_csv

_RANKING = re.compile(r"ranking_(\d{8}T\d{6}Z)(?:-(\d+))?\.csv")

# 人工决定表：工具只读、缺失时建模板，每日任务从不改写，人工编辑不会和自动提交冲突。
DECISIONS_CSV_NAME = "stopword_decisions.csv"
_STOP, _KEEP = "停用", "保留"

# jieba 词性：人名、地名、机构、其他专名，以及内置热词表里的专名（无词性，标 x）永不提名。
_PROPER_POS = frozenset({"nr", "nrfg", "nrt", "ns", "nt", "nz", "x"})
# 代词、副词、连词、介词、助词、语气词、叹词、拟声词、量词：只有全由这些构成的词才可能自动生效。
_FUNCTION_POS = frozenset({"r", "d", "c", "p", "u", "y", "e", "o", "q"})

# ponytail: 自动生效的门槛按 22 天 / 966 个视频的真实数据定；数据攒多后按候选表复核再调。
_AUTO_MIN_DAYS = 14

_CANDIDATE_FIELDS = (
    "词",
    "词性",
    "出现天数",
    "天数占比",
    "累计词频",
    "独立视频数",
    "独立UP主数",
    "分区数",
    "有效分区数",
    "趋势状态",
    "上期词频",
    "本期词频",
    "自动生效",
    "示例标题",
)
_AUTO_MIN_CATEGORIES = 3.0


def _write_text(path: Path, text: str) -> None:
    temporary = temporary_path(path)
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_decisions(path: Path) -> dict[str, str]:
    """读取人工决定（词 → 停用/保留）；缺失时写出空模板。人工编辑的文件，值不合法就报错。"""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("词,决定,备注\n", encoding="utf-8-sig")
        return {}
    decisions: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        # 中文 Windows 的 Excel「CSV（逗号分隔）」存成 GBK，报错要说清怎么改。
        raise ValueError(f"{path.name} 不是 UTF-8 编码，请用 Excel 另存为「CSV UTF-8」") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if not {"词", "决定"}.issubset(reader.fieldnames or []):
        raise ValueError(f"{path.name} 需要「词」「决定」两列")
    for line, row in enumerate(reader, start=2):
        word = normalize_token((row.get("词") or "").removeprefix("'"))
        decision = (row.get("决定") or "").strip()
        if not word or not decision:
            continue
        if decision not in (_STOP, _KEEP):
            raise ValueError(f"{path.name} 第 {line} 行：决定只能填「停用」或「保留」")
        if decisions.setdefault(word, decision) != decision:
            raise ValueError(f"{path.name} 第 {line} 行：「{word}」的决定前后矛盾")
    return decisions


def _pos_flags(word: str) -> frozenset[str]:
    _import_jieba().setLogLevel("ERROR")
    import jieba.posseg as posseg

    return frozenset(pair.flag for pair in posseg.lcut(word))


def _effective_categories(counts: Counter[str]) -> float:
    """分区分布的困惑度 exp(熵)：相当于均匀摊在几个分区；话题词集中，套话分散。"""
    total = sum(counts.values())
    return math.exp(-sum(n / total * math.log(n / total) for n in counts.values()))


def optimize_stopwords(
    data_dir: Path,
    output_dir: Path,
    *,
    min_days: int = 7,
    min_day_ratio: float = 0.3,
    min_uploaders: int = 5,
) -> dict[str, Any]:
    """原始快照不修改；baseline/current、候选证据和自动词表写入输出目录。"""
    if min_days < 1 or min_uploaders < 1 or not 0 < min_day_ratio <= 1:
        raise ValueError("min-days/min-uploaders 必须为正数，min-day-ratio 必须在 (0, 1] 内")
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
    decisions = read_decisions(output_dir / DECISIONS_CSV_NAME)

    policy = load_stopword_policy()
    load_default_dictionary()
    analyzer = TitleAnalyzer(policy)
    daily: dict[str, dict[str, int]] = {}
    days: Counter[str] = Counter()
    # 证据以视频为单位：同一视频连续在榜只算一次，驻榜的系列视频不会冒充全站套话。
    video_category: dict[str, dict[str, str]] = defaultdict(dict)
    uploaders: dict[str, set[str]] = defaultdict(set)
    samples: dict[str, list[str]] = defaultdict(list)
    # 先读完并验证，损坏快照不应被静默当成正常缺席日。
    for day, (_, path) in sorted(snapshots.items()):
        counts: Counter[str] = Counter()
        seen: set[str] = set()
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            # 0 字节是抓榜进程被强杀留下的占位文件，与只有表头的空榜单同样按缺席日跳过；
            # 否则它随每日任务提交进仓库后，之后每天的分析都会失败。
            if reader.fieldnames is not None and not {"视频标题", "BV号"}.issubset(
                reader.fieldnames
            ):
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
                category = row.get("主分区") or row.get("视频分区") or ""
                for word in tokens:
                    video_category[word][bvid] = category
                    # 缺 UP主 列时退化为按视频计数。
                    uploaders[word].add(row.get("UP主") or bvid)
                    if title not in samples[word] and len(samples[word]) < 3:
                        samples[word].append(title)
        if not seen:
            # 抓榜整榜解析失败时会故意留下只有表头的 CSV；当缺席日跳过并留痕，不让一天坏数据卡死分析。
            # ponytail: 只看当天最新一份，同日较早的有效快照不回退补用；需要时按跳号倒序找第一份非空。
            print(f"警告：空榜单，已跳过：{path.name}", file=sys.stderr)
            continue
        daily[day] = dict(counts.most_common())
        days.update(counts.keys())
    if not daily:
        raise ValueError("没有可分析的非空榜单快照")

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    # 先在旁边生成完整结果；分词或暂存生成失败时保留上轮输出。
    with TemporaryDirectory(prefix=".stopword-", dir=output_dir.parent) as temporary:
        staging = Path(temporary)
        summary = _export_analysis(
            staging,
            daily,
            days,
            video_category,
            uploaders,
            samples,
            decisions,
            min_days=min_days,
            min_day_ratio=min_day_ratio,
            min_uploaders=min_uploaders,
            allowlist=policy.allowlist,
        )
        summary["output_dir"] = str(output_dir)
        _write_text(
            staging / "stopword_summary.json",
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        )
        moves = [
            (path, output_dir / path.relative_to(staging))
            for path in staging.rglob("*")
            if path.is_file()
        ]
        # 先确认每个目标都可写再动手：Windows 上 Excel 打开的 CSV 会让 os.replace 中途失败，
        # 留下一半新一半旧的分析结果。ponytail: 检查与替换之间仍有竞态，只挡住常见的「文件开着」。
        for _, destination in moves:
            if destination.exists():
                destination.open("ab").close()
        for path, destination in moves:
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
    video_category: dict[str, dict[str, str]],
    uploaders: dict[str, set[str]],
    samples: dict[str, list[str]],
    decisions: dict[str, str],
    *,
    min_days: int,
    min_day_ratio: float,
    min_uploaders: int,
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

    # 保留词优先：人工「停用」也不压过内置保留词表，但要留痕，免得以为已经生效。
    manual = {word for word, decision in decisions.items() if decision == _STOP}
    if manual & allowlist:
        ignored = "、".join(sorted(manual & allowlist))
        print(f"警告：这些词在内置保留词表里，「停用」不生效：{ignored}", file=sys.stderr)
    manual -= allowlist
    # 被 jieba 切碎的短语（全网最强 → 全网 + 最强）永远匹配不到词元，停用会静默失效。
    unmatched = manual - aggregate.keys()
    if unmatched:
        print(
            f"警告：这些「停用」词从未作为单个词出现，没有效果（已被内置停用词过滤，或被分词"
            f"切碎——可改标切开后的词，或加进内置词典 user_dict.txt）：{'、'.join(sorted(unmatched))}",
            file=sys.stderr,
        )
    rows: list[dict[str, Any]] = []
    automatic: set[str] = set()
    for word, total in aggregate.items():
        ratio = days[word] / len(daily)
        if (
            word in decisions
            or word in allowlist
            or days[word] < min_days
            or ratio < min_day_ratio
            or len(uploaders[word]) < min_uploaders
        ):
            continue
        pos = _pos_flags(word)
        if pos & _PROPER_POS:
            continue
        categories = Counter(video_category[word].values())
        # 先取整再比门槛：均匀 3 分区的 exp(ln 3) 算出 2.9999999999999996，与表中显示一致。
        spread = round(_effective_categories(categories), 2)
        evidence = trend_by_word.get(word, {})
        previous = evidence.get("上期词频") or 0
        recent = evidence.get("本期词频") or 0
        stable = previous > 0 and 0.5 <= recent / previous <= 2
        # 强证据才自动生效：纯虚词、跨分区均匀、两个完整 7 日窗口内词频稳定。
        apply = (
            pos <= _FUNCTION_POS
            and len(daily) >= _AUTO_MIN_DAYS
            and spread >= _AUTO_MIN_CATEGORIES
            and stable
        )
        if apply:
            automatic.add(word)
        rows.append(
            {
                "词": word,
                "词性": "/".join(sorted(pos)),
                "出现天数": days[word],
                "天数占比": round(ratio, 4),
                "累计词频": total,
                "独立视频数": len(video_category[word]),
                "独立UP主数": len(uploaders[word]),
                "分区数": len(categories),
                "有效分区数": spread,
                "趋势状态": evidence.get("状态", "不足两期"),
                "上期词频": previous,
                "本期词频": recent,
                "自动生效": "是" if apply else "否",
                "示例标题": " | ".join(samples[word]),
            }
        )
    # 越多 UP 主在用、分区越分散越像套话，排在前面先审。
    rows.sort(key=lambda row: -row["独立UP主数"] * row["有效分区数"])
    # 标题是外部输入；CSV 转义与榜单导出保持一致。
    from .storage import _spreadsheet_safe

    _atomic_csv_write(
        output_dir / "stopword_candidates.csv",
        _CANDIDATE_FIELDS,
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
        "# 自动层，每次重算；在 stopword_decisions.csv 标「保留」可永久撤回\n"
        + "".join(word + "\n" for word in sorted(automatic)),
    )
    removed = manual | automatic
    for day, frequencies in daily.items():
        filtered = {word: count for word, count in frequencies.items() if word not in removed}
        write_frequencies_csv(current / f"word_frequency_{day}T000000Z.csv", filtered)
    write_frequencies_csv(current / "word_frequency_aggregate.csv", _merged_history(current))
    current_trend = _trend_rows(current)
    if current_trend is not None:
        write_trend_csv(current / "word_frequency_trend.csv", current_trend)
    return {
        "snapshot_days": len(daily),
        "candidates": len(rows),
        "automatic_words": sorted(automatic),
        "manual_stopwords": len(manual),
        "reviewed_keep": sum(1 for decision in decisions.values() if decision == _KEEP),
        "min_days": min_days,
        "min_day_ratio": min_day_ratio,
        "min_uploaders": min_uploaders,
        "output_dir": str(output_dir),
    }
