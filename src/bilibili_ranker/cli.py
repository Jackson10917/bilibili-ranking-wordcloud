"""项目命令行入口。"""

from __future__ import annotations

import argparse
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
    create_output_bundle,
    load_frequency_csvs,
    write_frequencies_csv,
    write_records_csv,
)
from .wordcloud import render_wordcloud

# --aggregate 只合并时间戳形态（含 -2 跳号后缀）的词频 CSV。
_TIMESTAMPED_FREQUENCY_PATTERN = re.compile(r"word_frequency_\d{8}T\d{6}Z(?:-\d+)?\.csv")


def _history_order(path: Path) -> tuple[str, int]:
    # 同秒跳号后缀的 '-'（0x2D）排在 '.'（0x2E）之前，纯文件名排序会把 -2 排到无后缀之前，
    # 「取最新一份」反取到较早那份；按（时间戳, 跳号）排才是时间序。
    timestamp, _, number = path.stem.removeprefix("word_frequency_").partition("-")
    return timestamp, int(number or 0)


def _merged_history(output_dir: Path) -> dict[str, int]:
    # 白名单只收时间戳形态（含 -2 跳号后缀）：聚合产物固定名与备份/改名产物都不匹配。
    history = sorted(
        (
            path
            for path in output_dir.glob("word_frequency_*.csv")
            if _TIMESTAMPED_FREQUENCY_PATTERN.fullmatch(path.name)
        ),
        key=_history_order,
    )
    # 同一 UTC 日期只取最新一份：一天多跑会把当天词频计两次。
    latest_per_date = {path.name[len("word_frequency_") :][:8]: path for path in history}
    return load_frequency_csvs(latest_per_date.values())


def _language_codes(value: str) -> tuple[str, ...]:
    codes = tuple(dict.fromkeys(part.strip() for part in value.split(",") if part.strip()))
    if not codes:
        raise argparse.ArgumentTypeError("语言列表不能为空")
    return codes


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bilibili-rank",
        description="抓取 B站全站排行榜，输出中文 CSV 和词云。",
    )
    parser.add_argument("--output-dir", type=Path, default=Path("output"), help="输出目录")
    parser.add_argument("--font-path", type=Path, help="显式指定 TTF/TTC/OTF 字体")
    parser.add_argument("--resource-dir", type=Path, help="覆盖内置停用词资源目录")
    parser.add_argument(
        "--languages",
        type=_language_codes,
        default=DEFAULT_LANGUAGES,
        # 默认值不经 type 转换：写字符串会触发 stopwords.py 的字符串防护；help 的默认值文本手写。
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
        help="不请求排行榜，只做 --aggregate 的离线聚合与重渲染（需与 --aggregate 连用）",
    )
    return parser


def run_pipeline(args: argparse.Namespace) -> dict[str, Any]:
    # 显式字体（--font-path 或环境变量）在此校验：无效字体是参数错误而非可降级故障，
    # 抓不抓榜都应在动任何输出之前报出来；两处都没给时把自动探测留给渲染期——
    # 那是「环境缺中日韩字体」的可降级故障，不是用户输错参数。
    resolved_font: Path | None = None
    if args.font_path is not None or os.environ.get("BILIBILI_WORDCLOUD_FONT"):
        resolved_font = resolve_font_path(args.font_path)

    fetched_count = 0
    accepted_count = 0
    rejected_count = 0
    ranking_csv: Path | None = None
    frequency_csv: Path | None = None
    aggregate_csv: Path | None = None
    generated_wordcloud: Path | None = None
    cloud_frequencies: Mapping[str, int] | None = None
    cloud_destination: Path | None = None

    if not args.no_fetch:
        # 停用词与 jieba 词典只有分词路径需要：--no-fetch 不构造 TitleAnalyzer，
        # 提前加载只会白付词典构建的时间。校验仍全部排在抓取与 mkdir 之前——
        # 参数错误在发请求前报出来，不白抓一整榜；词典写错不留空目录。
        policy = load_stopword_policy(args.resource_dir, languages=args.languages)
        load_default_dictionary()
        if args.user_dict is not None:
            load_user_dictionary(args.user_dict)

        # 输出目录先探：CSV 先落盘的设计下，目录不可写会白抓一整榜。
        args.output_dir.mkdir(parents=True, exist_ok=True)

        fetched = fetch_all_ranking(timeout_seconds=args.timeout, rid=args.rid)
        fetched_count = len(fetched.items)

        bundle = create_output_bundle(args.output_dir, fetched.fetched_at)
        ranking_csv = bundle.ranking_csv

        # create_output_bundle 已用 O_CREAT|O_EXCL 占位一个 0 字节 CSV；占位后抛异常
        # （Ctrl+C、解析崩溃）会残留并占掉该编号。仅当文件仍是 0 字节才清理。
        try:
            records, parse_rejected_count = parse_ranking_records(fetched.items)
            accepted, duplicate_rejected_count = deduplicate_records(records)

            # 分词（内部导入 jieba）抛异常时已抓的榜单不能一个字节不留，
            # 与「词云失败只降级警告、CSV 照常写出」的处理一致。
            write_records_csv(bundle.ranking_csv, accepted)
        except BaseException:
            try:
                if bundle.ranking_csv.stat().st_size == 0:
                    bundle.ranking_csv.unlink()
            except OSError:
                pass
            raise
        rejected_count = parse_rejected_count + duplicate_rejected_count

        # 整榜解析失败（上游字段改名）时退出码 0 会让定时任务把表头 CSV 当成功结果，抛掉。
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
            # 词频表先于渲染落盘：渲染降级成仅警告时，机器可读的产物仍然在。
            frequency_csv = write_frequencies_csv(bundle.word_frequency_csv, frequencies)
        else:
            print("警告：标题清洗后没有可用词元，只输出 CSV。", file=sys.stderr)

        # --aggregate 时词云改用跨运行累计：单次快照的词频接近噪声，字号编码不出有效信息。
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
            print("警告：输出目录没有可聚合的词频 CSV。", file=sys.stderr)

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
        # 超大 --width/--height 会让 PIL 抛 MemoryError。
        except (RuntimeError, ValueError, OSError, MemoryError) as exc:
            print(f"警告：词云生成失败，仅输出 CSV：{exc}", file=sys.stderr)

    # 摘要的消费者是脚本（jq、CI），键名用 ASCII；字段含义由 README 的字段表承载。
    # --no-fetch 时 fetched/accepted/rejected 为 0，ranking_csv/frequency_csv 为 null。
    return {
        "fetched": fetched_count,
        "accepted": accepted_count,
        "rejected": rejected_count,
        "ranking_csv": str(ranking_csv.resolve()) if ranking_csv else None,
        "frequency_csv": str(frequency_csv.resolve()) if frequency_csv else None,
        "aggregate_frequency_csv": str(aggregate_csv.resolve()) if aggregate_csv else None,
        "wordcloud": str(generated_wordcloud) if generated_wordcloud else None,
    }


def main(argv: Sequence[str] | None = None) -> int:
    # Windows 上重定向到管道/文件时 stdout 用 ANSI 代码页，中文摘要会以 UnicodeEncodeError
    # 收场；Python 3.15 起 UTF-8 成为默认，届时这段是空操作。
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")

    parser = build_parser()
    args = parser.parse_args(argv)
    # inf/nan 都能绕过 `<= 0`；1e10 这类有限大值同样会让 socket.settimeout 抛 OverflowError。
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= MAX_TIMEOUT_SECONDS:
        parser.error(f"--timeout 必须是 0 到 {MAX_TIMEOUT_SECONDS:.0f} 之间的有限数")
    if args.width <= 0 or args.height <= 0 or args.max_words <= 0:
        parser.error("词云尺寸和最大词数必须大于 0")
    if args.minimum_token_length <= 0:
        parser.error("--minimum-token-length 必须大于 0")
    if args.rid < 0:
        parser.error("--rid 必须不小于 0")
    if args.no_fetch and not args.aggregate:
        parser.error("--no-fetch 需要与 --aggregate 一起使用")

    try:
        summary = run_pipeline(args)
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已取消。", file=sys.stderr)
        return 130

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
