"""项目命令行入口。"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .cleaner import TitleAnalyzer, deduplicate_records
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
        # 默认值不经 type 转换，不能写成字符串（会触发 stopwords.py 的字符串防护）；
        # 元组直接进 help 会显示成 ('zh', 'en', ...)，所以手写默认值文本。
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
        "--aggregate",
        action="store_true",
        help="合并输出目录已有词频 CSV，输出累计词频 CSV 与按累计词频渲染的词云"
        "（替代本次时间戳词云）",
    )
    return parser


def run_pipeline(args: argparse.Namespace) -> dict[str, Any]:
    # 先加载停用词：不依赖网络，让 --languages/--resource-dir 的错误在发请求前就报出来，
    # 而不是抓完整个榜单再抛异常、CSV 一个字节都不落盘。
    policy = load_stopword_policy(args.resource_dir, languages=args.languages)

    # 显式配置的字体（--font-path 或环境变量）同理提前到任何写盘和请求之前：路径写错一个
    # 字母不该等抓完整榜后被词云阶段的降级逻辑吞成警告、退出码还是 0。两处都没给时仍把
    # 自动探测留给渲染期——那属于「环境缺中日韩字体」的可降级故障，不是用户输错参数。
    resolved_font: Path | None = None
    if args.font_path is not None or os.environ.get("BILIBILI_WORDCLOUD_FONT"):
        resolved_font = resolve_font_path(args.font_path)

    # 同理先探输出目录：CSV 先落盘的设计下，目录不可写会让抓完的整榜数据白抓一遍。
    args.output_dir.mkdir(parents=True, exist_ok=True)

    fetched = fetch_all_ranking(timeout_seconds=args.timeout, rid=args.rid)

    bundle = create_output_bundle(args.output_dir, fetched.fetched_at)

    # create_output_bundle 用 O_CREAT|O_EXCL 占位了一个 0 字节 CSV。这里到 write_records_csv
    # 之间若抛异常（Ctrl+C、解析崩溃），占位文件会永久残留并占掉该编号，下次运行跳到 -2。
    # 只在文件仍是 0 字节时清理，绝不碰已经写入内容的结果。
    try:
        records, parse_rejected_count = parse_ranking_records(fetched.items)
        accepted, duplicate_rejected_count = deduplicate_records(records)

        # CSV 先落盘：分词（内部导入 jieba）抛任何异常都不该让已经抓完的榜单一个字节不留，
        # 否则与「词云失败只降级警告、CSV 照常写出」的处理自相矛盾。
        write_records_csv(bundle.ranking_csv, accepted)
    except BaseException:
        try:
            if bundle.ranking_csv.stat().st_size == 0:
                bundle.ranking_csv.unlink()
        except OSError:
            pass
        raise
    rejected_count = parse_rejected_count + duplicate_rejected_count

    # 接口成功但整榜无法解析（上游字段改名）时，退出码 0 会让定时任务把只有表头的 CSV
    # 当成功结果。CSV 已经落盘，抛异常即可：main 会打印错误并返回 1。
    if not accepted:
        raise RuntimeError(
            f"没有解析出任何有效记录（抓取 {len(fetched.items)} 条，全部被拒绝），"
            f"CSV 只有表头：{bundle.ranking_csv}"
        )

    analyzer = TitleAnalyzer(
        policy,
        minimum_token_length=args.minimum_token_length,
    )
    frequencies = analyzer.analyze(accepted)

    generated_wordcloud: Path | None = None
    frequency_csv: Path | None = None
    if frequencies:
        # 词频表先于渲染落盘：渲染降级成仅警告时，机器可读的产物仍然在。
        frequency_csv = write_frequencies_csv(bundle.word_frequency_csv, frequencies)
    else:
        print("警告：标题清洗后没有可用词元，只输出 CSV。", file=sys.stderr)

    # 词云来源：默认本次词频；--aggregate 时换成跨运行累计，单次快照里九成词
    # 只出现一次，字号编码的差异接近噪声。
    cloud_frequencies: Mapping[str, int | float] | None = None
    cloud_destination: Path | None = None
    aggregate_csv: Path | None = None
    if args.aggregate:
        aggregate_target = args.output_dir / AGGREGATE_FREQUENCY_CSV_NAME
        # 聚合产物自身固定名也匹配 word_frequency_*.csv，必须从合并范围排除：
        # 否则连续两次 --aggregate 会把上次的累计值再翻一倍。
        history = sorted(
            path
            for path in args.output_dir.glob("word_frequency_*.csv")
            if path != aggregate_target
        )
        merged = load_frequency_csvs(history)
        if merged:
            aggregate_csv = write_frequencies_csv(aggregate_target, merged)
            cloud_frequencies = merged
            cloud_destination = args.output_dir / AGGREGATE_WORDCLOUD_PNG_NAME
        else:
            print("警告：输出目录没有可聚合的词频 CSV。", file=sys.stderr)
    elif frequencies:
        cloud_frequencies = frequencies
        cloud_destination = bundle.wordcloud_png

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
        # 超大 --width/--height 会让 PIL 抛 MemoryError，不能以 traceback 收场。
        except (RuntimeError, ValueError, OSError, MemoryError) as exc:
            print(f"警告：词云生成失败，仅输出 CSV：{exc}", file=sys.stderr)

    return {
        "抓取条数": len(fetched.items),
        "有效条数": len(accepted),
        "拒绝条数": rejected_count,
        "排行榜CSV": str(bundle.ranking_csv.resolve()),
        "词频CSV": str(frequency_csv.resolve()) if frequency_csv else None,
        "聚合词频CSV": str(aggregate_csv.resolve()) if aggregate_csv else None,
        "词云": str(generated_wordcloud) if generated_wordcloud else None,
    }


def main(argv: Sequence[str] | None = None) -> int:
    # 摘要 JSON 和警告都含中文。Windows 上重定向到管道/文件时 stdout 用的是 ANSI 代码页
    # （西文机器是 cp1252），print 会以 UnicodeEncodeError 收场——活干完了却报错退出。
    # Python 3.15 起 UTF-8 成为默认，届时这段是空操作。
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
