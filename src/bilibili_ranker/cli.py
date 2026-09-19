"""项目命令行入口。"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .cleaner import (
    TitleAnalyzer,
    deduplicate_records,
    load_default_dictionary,
    load_user_dictionary,
)
from .client import MAX_TIMEOUT_SECONDS, fetch_all_ranking
from .fonts import resolve_font_path
from .models import parse_ranking_records
from .stopwords import DEFAULT_LANGUAGES, load_stopword_policy
from .storage import (
    AGGREGATE_FREQUENCY_CSV_NAME,
    AGGREGATE_WORDCLOUD_PNG_NAME,
    TREND_CSV_NAME,
    create_output_bundle,
    load_frequency_csvs,
    write_frequencies_csv,
    write_records_csv,
    write_trend_csv,
)
from .wordcloud import render_wordcloud

# --aggregate 与 --trend 只认时间戳形态（含 -2 跳号后缀）的词频 CSV。
_TIMESTAMPED_FREQUENCY_PATTERN = re.compile(r"word_frequency_\d{8}T\d{6}Z(?:-\d+)?\.csv")


def _history_order(path: Path) -> tuple[str, int]:
    # 同秒跳号后缀的 '-'（0x2D）排在 '.'（0x2E）之前，纯文件名排序会把 -2 排到无后缀之前，
    # 「取最新一份」反取到较早那份；按（时间戳，跳号）排才是时间序。
    timestamp, _, number = path.stem.removeprefix("word_frequency_").partition("-")
    return timestamp, int(number or 0)


def _latest_frequency_paths(output_dir: Path) -> list[Path]:
    # 白名单只收时间戳形态（含 -2 跳号后缀）：聚合/趋势产物固定名与备份/改名产物都不匹配。
    history = sorted(
        (
            path
            for path in output_dir.glob("word_frequency_*.csv")
            if _TIMESTAMPED_FREQUENCY_PATTERN.fullmatch(path.name)
        ),
        key=_history_order,
    )
    # 同一 UTC 日期只取最新一份：一天多跑会把当天词频计两次；按时间序返回。
    latest_per_date = {path.name[len("word_frequency_") :][:8]: path for path in history}
    return sorted(latest_per_date.values(), key=_history_order)


def _merged_history(output_dir: Path) -> dict[str, int]:
    return load_frequency_csvs(_latest_frequency_paths(output_dir))


# 趋势窗口：近 7 个快照日 vs 前 7 个。窗口按快照日数而不是日历天切——上游断更时窗口
# 顺延，口径始终是「最近 7 期对比再往前 7 期」。
_TREND_WINDOW = 7

# 趋势表只收窗内总词频 >=2 的词：并列词频靠文件读入顺序定序，而单次出现的词是最大的一片
# 并列组（21 天里 53.7% 的词至多出现 2 天），给它们算出的排名不是测量值而是插入产物。
_TREND_MIN_COUNT = 2


def _trend_rows(output_dir: Path) -> list[dict[str, Any]] | None:
    """近 7 个快照日 vs 前 7 个的词频排名变化；可对比的历史不足两期时返回 None。"""

    paths = _latest_frequency_paths(output_dir)
    # 上期窗口必须右闭在 2*_TREND_WINDOW：写成 paths[:-_TREND_WINDOW] 会把「前 7 期」
    # 变成「最近 7 期之外的全部历史」，随日更累计成累计平均，排名变化被稀释到看不出变化。
    previous_paths, recent_paths = (
        paths[-2 * _TREND_WINDOW : -_TREND_WINDOW],
        paths[-_TREND_WINDOW:],
    )
    if not previous_paths:
        return None
    # 两窗对称过滤：门槛只算一次窗内总词频，掉出高频区的词照样记为掉出。
    previous = {
        word: count
        for word, count in load_frequency_csvs(previous_paths).items()
        if count >= _TREND_MIN_COUNT
    }
    recent = {
        word: count
        for word, count in load_frequency_csvs(recent_paths).items()
        if count >= _TREND_MIN_COUNT
    }
    # 排名就是词频降序的位次（load_frequency_csvs 已按降序返回）；并列词频按日期先后定序。
    previous_rank = {word: rank for rank, word in enumerate(previous, start=1)}
    rows: list[dict[str, Any]] = []
    for rank, (word, count) in enumerate(recent.items(), start=1):
        old_rank = previous_rank.get(word)
        if old_rank is None:
            delta: Any = ""
            status = "新进"
        else:
            delta = old_rank - rank
            status = "持平" if delta == 0 else ("上升" if delta > 0 else "下降")
        rows.append(
            {
                "词": word,
                "状态": status,
                "上期排名": old_rank if old_rank is not None else "",
                "上期词频": previous.get(word, ""),
                "本期排名": rank,
                "本期词频": count,
                "排名变化": delta,
            }
        )
    # 掉出词垫底，沿用上期词频降序（previous 本身就是降序字典）；缺席侧的单元格留空。
    for rank, (word, count) in enumerate(previous.items(), start=1):
        if word not in recent:
            rows.append(
                {
                    "词": word,
                    "状态": "掉出",
                    "上期排名": rank,
                    "上期词频": count,
                    "本期排名": "",
                    "本期词频": "",
                    "排名变化": "",
                }
            )
    return rows


def _language_codes(value: str) -> tuple[str, ...]:
    codes = tuple(dict.fromkeys(part.strip() for part in value.split(",") if part.strip()))
    if not codes:
        raise argparse.ArgumentTypeError("语言列表不能为空")
    return codes


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bilibili-rank",
        description="抓取 B 站全站排行榜，输出中文 CSV 和词云。",
    )
    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    p_analyze = subparsers.add_parser(
        "stopword-analyze", aliases=["stopword-candidates"], help="分析历史并更新私有自动停用词表"
    )
    p_analyze.add_argument("--data-dir", type=Path, default=Path("data"))
    p_analyze.add_argument("--output-dir", type=Path, default=Path("output"))
    p_analyze.add_argument("--min-days", type=int, default=7)
    p_analyze.add_argument("--min-day-ratio", type=float, default=0.6)
    p_analyze.add_argument("--min-total", type=int, default=20)

    # 主命令参数
    parser.add_argument("--output-dir", type=Path, default=Path("output"), help="输出目录")
    parser.add_argument("--font-path", type=Path, help="显式指定 TTF/TTC/OTF 字体")
    parser.add_argument("--resource-dir", type=Path, help="覆盖内置停用词资源目录")
    parser.add_argument(
        "--languages",
        type=_language_codes,
        default=DEFAULT_LANGUAGES,
        help=f"逗号分隔的 stopwordsiso 语言代码（默认：{','.join(DEFAULT_LANGUAGES)}）",
    )
    parser.add_argument("--timeout", type=float, default=15.0, help="请求超时秒数")
    parser.add_argument(
        "--rid",
        type=int,
        default=0,
        help="排行榜分区 ID；0 为全站榜，其余为上游接口定义的分区 rid",
    )
    parser.add_argument("--width", type=int, default=1920, help="词云宽度")
    parser.add_argument("--height", type=int, default=1080, help="词云高度")
    parser.add_argument("--max-words", type=int, default=300, help="词云最大词数")
    parser.add_argument(
        "--minimum-token-length",
        type=int,
        default=2,
        help="普通词最短长度；保留词不受此限制",
    )
    parser.add_argument(
        "--user-dict",
        type=Path,
        help="jieba 用户词典路径（dict 格式），在内置热词表之上追加",
    )
    parser.add_argument(
        "--aggregate",
        action="store_true",
        help="合并输出目录已有词频 CSV，输出累计词频 CSV 与按累计词频渲染的词云"
        "（替代本次时间戳词云）",
    )
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        help="不请求排行榜，只做离线的聚合重渲染或趋势分析（需与 --aggregate 或"
        " --trend 连用，单独使用按参数错误退出）",
    )
    parser.add_argument(
        "--trend",
        action="store_true",
        help="对比输出目录近 7 个快照日与前 7 个快照日的词频排名，输出趋势 CSV"
        "（可对比的历史不足两期时只警告不产出）",
    )
    return parser


def run_pipeline(args: argparse.Namespace) -> dict[str, Any]:
    resolved_font: Path | None = None
    if args.font_path is not None or os.environ.get("BILIBILI_WORDCLOUD_FONT"):
        resolved_font = resolve_font_path(args.font_path)

    fetched_count = 0
    accepted_count = 0
    rejected_count = 0
    ranking_csv: Path | None = None
    frequency_csv: Path | None = None
    aggregate_csv: Path | None = None
    trend_csv: Path | None = None
    generated_wordcloud: Path | None = None
    cloud_frequencies: Mapping[str, int] | None = None
    cloud_destination: Path | None = None

    if not args.no_fetch:
        policy = load_stopword_policy(args.resource_dir, languages=args.languages)
        load_default_dictionary()
        if args.user_dict is not None:
            load_user_dictionary(args.user_dict)

        args.output_dir.mkdir(parents=True, exist_ok=True)

        fetched = fetch_all_ranking(timeout_seconds=args.timeout, rid=args.rid)
        fetched_count = len(fetched.items)

        bundle = create_output_bundle(args.output_dir, fetched.fetched_at)
        ranking_csv = bundle.ranking_csv

        try:
            records, parse_rejected_count = parse_ranking_records(fetched.items)
            accepted, duplicate_rejected_count = deduplicate_records(records)

            write_records_csv(bundle.ranking_csv, accepted)
        except BaseException:
            try:
                if bundle.ranking_csv.stat().st_size == 0:
                    bundle.ranking_csv.unlink()
            except OSError:
                pass
            raise
        rejected_count = parse_rejected_count + duplicate_rejected_count

        if not accepted:
            raise RuntimeError(
                f"没有解析出任何有效记录（抓取 {len(fetched.items)} 条，全部被拒绝），"
                f"CSV 只有表头：{bundle.ranking_csv}"
            )
        accepted_count = len(accepted)

        frequencies = TitleAnalyzer(
            policy,
            minimum_token_length=args.minimum_token_length,
        ).analyze(accepted)

        if frequencies:
            frequency_csv = write_frequencies_csv(bundle.word_frequency_csv, frequencies)
        else:
            print("警告：标题清洗后没有可用词元，只输出 CSV。", file=sys.stderr)

        if not args.aggregate:
            cloud_frequencies = frequencies or None
            cloud_destination = bundle.wordcloud_png

    if args.aggregate:
        merged = _merged_history(args.output_dir)
        if merged:
            aggregate_csv = write_frequencies_csv(
                args.output_dir / AGGREGATE_FREQUENCY_CSV_NAME, merged
            )
            cloud_frequencies = merged
            cloud_destination = args.output_dir / AGGREGATE_WORDCLOUD_PNG_NAME
        else:
            (args.output_dir / AGGREGATE_FREQUENCY_CSV_NAME).unlink(missing_ok=True)
            (args.output_dir / AGGREGATE_WORDCLOUD_PNG_NAME).unlink(missing_ok=True)
            print("警告：输出目录没有可聚合的词频 CSV。", file=sys.stderr)

    if args.trend:
        trend_rows = _trend_rows(args.output_dir)
        if trend_rows is None:
            (args.output_dir / TREND_CSV_NAME).unlink(missing_ok=True)
            print("警告：可对比的词频快照不足两期，未产出趋势表。", file=sys.stderr)
        else:
            trend_csv = write_trend_csv(args.output_dir / TREND_CSV_NAME, trend_rows)

    if cloud_frequencies is not None and cloud_destination is not None:
        try:
            generated_wordcloud = render_wordcloud(
                cloud_frequencies,
                cloud_destination,
                font_path=resolved_font,
                width=args.width,
                height=args.height,
                max_words=args.max_words,
            )
        except (RuntimeError, ValueError, OSError, MemoryError) as exc:
            print(f"警告：词云生成失败，仅输出 CSV：{exc}", file=sys.stderr)

    return {
        "fetched": fetched_count,
        "accepted": accepted_count,
        "rejected": rejected_count,
        "ranking_csv": str(ranking_csv.resolve()) if ranking_csv else None,
        "frequency_csv": str(frequency_csv.resolve()) if frequency_csv else None,
        "aggregate_frequency_csv": str(aggregate_csv.resolve()) if aggregate_csv else None,
        "trend_csv": str(trend_csv.resolve()) if trend_csv else None,
        "wordcloud": str(generated_wordcloud) if generated_wordcloud else None,
    }


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")

    parser = build_parser()
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= MAX_TIMEOUT_SECONDS:
        parser.error(f"--timeout 必须是 0 到 {MAX_TIMEOUT_SECONDS:.0f} 之间的有限数")
    if args.width <= 0 or args.height <= 0 or args.max_words <= 0:
        parser.error("词云尺寸和最大词数必须大于 0")
    if args.minimum_token_length <= 0:
        parser.error("--minimum-token-length 必须大于 0")
    if args.rid < 0:
        parser.error("--rid 必须不小于 0")
    if args.no_fetch and not (args.aggregate or args.trend):
        parser.error("--no-fetch 需要与 --aggregate 或 --trend 一起使用")

    try:
        if args.command in ("stopword-analyze", "stopword-candidates"):
            from .stopword_optimizer import optimize_stopwords

            summary = optimize_stopwords(
                args.data_dir,
                args.output_dir,
                min_days=args.min_days,
                min_day_ratio=args.min_day_ratio,
                min_total=args.min_total,
            )
        else:
            summary = run_pipeline(args)
    except (RuntimeError, ValueError, OSError, csv.Error) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已取消。", file=sys.stderr)
        return 130

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
