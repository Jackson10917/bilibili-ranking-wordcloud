"""项目命令行入口。"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Sequence

from .cleaner import TitleAnalyzer, deduplicate_records
from .client import fetch_all_ranking
from .models import parse_ranking_records
from .stopwords import DEFAULT_LANGUAGES, load_stopword_policy
from .storage import create_output_bundle, write_records_csv
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
        help="逗号分隔的 stopwordsiso 语言代码",
    )
    parser.add_argument("--timeout", type=float, default=15.0, help="请求超时秒数")
    parser.add_argument("--width", type=int, default=1920, help="词云宽度")
    parser.add_argument("--height", type=int, default=1080, help="词云高度")
    parser.add_argument("--max-words", type=int, default=300, help="词云最大词数")
    parser.add_argument(
        "--minimum-token-length",
        type=int,
        default=2,
        help="普通词最短长度；保留词不受此限制",
    )
    return parser


def run_pipeline(args: argparse.Namespace) -> dict[str, Any]:
    fetched = fetch_all_ranking(timeout_seconds=args.timeout)

    bundle = create_output_bundle(args.output_dir, fetched.fetched_at)

    records, parse_rejected_count = parse_ranking_records(fetched.items)
    accepted, duplicate_rejected_count = deduplicate_records(records)
    rejected_count = parse_rejected_count + duplicate_rejected_count

    policy = load_stopword_policy(args.resource_dir, languages=args.languages)
    analyzer = TitleAnalyzer(
        policy,
        minimum_token_length=args.minimum_token_length,
    )
    frequencies = analyzer.analyze(accepted)

    write_records_csv(bundle.ranking_csv, accepted)
    if not accepted:
        print("警告：没有解析出任何有效记录，CSV 只有表头。", file=sys.stderr)

    generated_wordcloud: Path | None = None
    if frequencies:
        try:
            generated_wordcloud = render_wordcloud(
                frequencies,
                bundle.wordcloud_png,
                font_path=args.font_path,
                width=args.width,
                height=args.height,
                max_words=args.max_words,
            )
        # 超大 --width/--height 会让 PIL 抛 MemoryError，不能以 traceback 收场。
        except (RuntimeError, ValueError, OSError, MemoryError) as exc:
            print(f"警告：词云生成失败，仅输出 CSV：{exc}", file=sys.stderr)
    elif accepted:
        print("警告：标题清洗后没有可用词元，只输出 CSV。", file=sys.stderr)

    return {
        "抓取条数": len(fetched.items),
        "有效条数": len(accepted),
        "拒绝条数": rejected_count,
        "排行榜CSV": str(bundle.ranking_csv.resolve()),
        "词云": str(generated_wordcloud) if generated_wordcloud else None,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # inf/nan 都能绕过 `<= 0`：inf 会让 socket.settimeout 抛 OverflowError 逸出错误处理。
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout 必须是大于 0 的有限数")
    if args.width <= 0 or args.height <= 0 or args.max_words <= 0:
        parser.error("词云尺寸和最大词数必须大于 0")
    if args.minimum_token_length <= 0:
        parser.error("--minimum-token-length 必须大于 0")

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
