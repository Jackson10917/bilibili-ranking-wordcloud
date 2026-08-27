"""CSV 原子写入与输出路径的回归测试。

由 tests/test_core.py 按源码模块拆分而来；统一由 pytest 收集运行：python -m pytest tests
"""

from __future__ import annotations

import csv
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from bilibili_ranker.models import VideoRankingRecord
from bilibili_ranker.storage import write_records_csv


def test_csv_formula_prefix_escaped() -> None:
    # 标题是投稿者可控内容，Excel 会把 = + - @ 开头的单元格当公式求值。
    record = VideoRankingRecord.from_api_item(
        {"bvid": "BV1aa0000000", "title": "=1+1", "owner": {"name": "@up"}}, rank=1
    )
    with tempfile.TemporaryDirectory() as directory:
        destination = Path(directory) / "out.csv"
        write_records_csv(destination, [record])
        with destination.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
    assert rows[0]["视频标题"] == "'=1+1"
    assert rows[0]["UP主"] == "'@up"


def test_frequency_csv_escaped_and_ordered() -> None:
    # 词元源自投稿标题，与榜单 CSV 走同一套公式前缀转义；行序沿用传入的降序，
    # 二次分析按行序取 TopN 时不允许乱序。
    from bilibili_ranker.storage import write_frequencies_csv

    with tempfile.TemporaryDirectory() as directory:
        destination = Path(directory) / "freq.csv"
        write_frequencies_csv(destination, {"=HYPERLINK": 3, "魔方": 1})
        with destination.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
    assert rows[0]["词"] == "'=HYPERLINK"
    assert rows[0]["词频"] == "3"
    assert rows[1]["词"] == "魔方"
    assert rows[1]["词频"] == "1"


def test_atomic_csv_write() -> None:
    record = VideoRankingRecord.from_api_item(
        {"bvid": "BV1aa0000000", "title": "标题", "owner": {}, "stat": {}},
        rank=1,
    )
    with tempfile.TemporaryDirectory() as directory:
        destination = Path(directory) / "out.csv"
        write_records_csv(destination, [record])
        with destination.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        assert len(rows) == 1
        assert rows[0]["BV号"] == "BV1aa0000000"
        assert rows[0]["排名"] == "1"
        # 失败写入不留临时文件。
        leftovers = [p.name for p in Path(directory).iterdir() if p.name.endswith(".tmp")]
        assert leftovers == []


def test_output_bundle_reserves_csv_atomically() -> None:
    # exists() 检查在"检查"和"写入"之间有窗口：同秒并发的两个进程会拿到同一编号，
    # 后写者的 os.replace 覆盖先写者的结果。占位必须是原子的。
    from bilibili_ranker.storage import create_output_bundle

    moment = datetime(2024, 1, 1, tzinfo=timezone.utc)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        # 连续调用而不写入任何内容，模拟两个并发进程都还没走到 os.replace。
        names = [create_output_bundle(root, moment).ranking_csv.name for _ in range(3)]
        assert len(set(names)) == 3, names


def test_output_bundle_releases_placeholder_when_png_taken() -> None:
    # PNG 已被占用时整对跳号，且刚占位的空 CSV 必须撤销，否则留下 0 字节垃圾文件。
    from bilibili_ranker.storage import create_output_bundle

    moment = datetime(2024, 1, 1, tzinfo=timezone.utc)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "wordcloud_20240101T000000Z.png").write_bytes(b"x")

        bundle = create_output_bundle(root, moment)
        assert bundle.ranking_csv.name == "ranking_20240101T000000Z-2.csv"
        assert not (root / "ranking_20240101T000000Z.csv").exists()


def test_output_bundle_suffix_keeps_pair_aligned() -> None:
    # 同一秒重复运行时追加 -2/-3 后缀，且 CSV 与 PNG 共用同一后缀：
    # 拆开编号会产出 ranking_...-3.csv 配 wordcloud_...-2.png 这种无法配对的组合。
    from bilibili_ranker.storage import create_output_bundle

    moment = datetime(2024, 1, 1, tzinfo=timezone.utc)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        first = create_output_bundle(root, moment)
        assert first.ranking_csv.name == "ranking_20240101T000000Z.csv"
        assert first.wordcloud_png.name == "wordcloud_20240101T000000Z.png"

        first.ranking_csv.write_text("x", encoding="utf-8")
        second = create_output_bundle(root, moment)
        assert second.ranking_csv.name == "ranking_20240101T000000Z-2.csv"
        assert second.wordcloud_png.name == "wordcloud_20240101T000000Z-2.png"

        # 只有 PNG 被占用时同样整对跳号，绝不覆盖已有文件。
        second.wordcloud_png.write_text("x", encoding="utf-8")
        third = create_output_bundle(root, moment)
        assert third.ranking_csv.name == "ranking_20240101T000000Z-3.csv"
        assert third.wordcloud_png.name == "wordcloud_20240101T000000Z-3.png"


def test_storage_accepts_naive_datetime() -> None:
    # naive datetime 按 UTC 处理，不抛 NaiveDatetime 相关异常。
    from bilibili_ranker.storage import create_output_bundle

    with tempfile.TemporaryDirectory() as directory:
        bundle = create_output_bundle(Path(directory), datetime(2024, 1, 1))
        assert bundle.ranking_csv.name == "ranking_20240101T000000Z.csv"


def test_load_frequency_csvs_merges_and_orders() -> None:
    # 聚合读取的是 write_frequencies_csv 的产物：按词求和、转义逆操作、行序降序、坏行跳过。
    from bilibili_ranker.storage import load_frequency_csvs, write_frequencies_csv

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        write_frequencies_csv(root / "a.csv", {"魔方": 2, "教程": 1})
        write_frequencies_csv(root / "b.csv", {"魔方": 3, "pv": 1})
        (root / "c.csv").write_text("词,词频\n'模型,2\n垃圾行\n", encoding="utf-8")

        merged = load_frequency_csvs([root / "a.csv", root / "b.csv", root / "c.csv"])

    assert list(merged) == ["魔方", "模型", "教程", "pv"]
    assert merged == {"魔方": 5, "教程": 1, "pv": 1, "模型": 2}
