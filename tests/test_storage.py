"""CSV 原子写入与输出路径的回归测试。

由 tests/test_core.py 按源码模块拆分而来；统一由 pytest 收集运行：python -m pytest tests
"""

from __future__ import annotations

import csv
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

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


def test_output_bundle_releases_placeholder_when_frequency_csv_taken() -> None:
    # --aggregate 靠攒词频 CSV 做累计，ranking CSV 可能被清掉只留词频。同秒重跑时
    # ranking 占位能成功、编号不跳，词频 CSV 不查存在性就会被 os.replace 静默覆盖，
    # 累计历史少一天——与 PNG 一样纳入存在性检查，整对跳号。
    from bilibili_ranker.storage import create_output_bundle

    moment = datetime(2024, 1, 1, tzinfo=timezone.utc)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "word_frequency_20240101T000000Z.csv").touch()

        bundle = create_output_bundle(root, moment)
        assert bundle.ranking_csv.name == "ranking_20240101T000000Z-2.csv"
        assert bundle.word_frequency_csv.name == "word_frequency_20240101T000000Z-2.csv"
        assert bundle.wordcloud_png.name == "wordcloud_20240101T000000Z-2.png"
        # 被跳过编号的占位必须撤销，不留 0 字节垃圾。
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


def test_load_frequency_csvs_skips_unreadable_file() -> None:
    # 坏文件与坏行同权跳过：历史 CSV 正被 Excel 占用时（README 推荐用 Excel 打开产物，
    # Windows 上是 PermissionError），聚合不能整个炸掉——此时本次榜单与词频 CSV 已落盘，
    # 抛出去会把一次正常运行误判成退出码 1。
    from bilibili_ranker.storage import load_frequency_csvs

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        good = root / "word_frequency_20240101T000000Z.csv"
        good.write_text("词,词频\n魔方,2\n", encoding="utf-8-sig")
        locked = root / "word_frequency_20240102T000000Z.csv"
        # 打开目录在 Windows 是 PermissionError、POSIX 是 IsADirectoryError，同为 OSError，
        # 可移植地模拟「文件存在但读不开」。
        locked.mkdir()
        bad_row = root / "word_frequency_20240103T000000Z.csv"
        bad_row.write_text("词,词频\n魔方,1\n模型,不是数字\n", encoding="utf-8-sig")

        merged = load_frequency_csvs((locked, bad_row, good))

    assert merged == {"魔方": 3}


def test_load_frequency_csvs_skips_nonpositive_counts() -> None:
    # 词频 0/负数只可能来自手改或损坏的文件：照常聚合会让 wordcloud 渲染出一张看似
    # 正常、数据错误的图，与「坏行跳过」的其余分支同权处理。
    from bilibili_ranker.storage import load_frequency_csvs

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        bad = root / "word_frequency_20240101T000000Z.csv"
        bad.write_text("词,词频\n魔方,5\n零,0\n负数,-3\n", encoding="utf-8-sig")

        merged = load_frequency_csvs((bad,))

    assert merged == {"魔方": 5}


def test_load_frequency_csvs_skips_bad_encoding_and_oversized_field() -> None:
    # Excel「另存为」默认 ANSI/GBK，重读抛 UnicodeDecodeError——它是 ValueError 的子类
    # 而非 OSError；超过 csv 模块默认字段上限（131072 字符）的行抛 csv.Error。
    # 两类都按坏文件跳过，旁边的正常文件照常计入。
    from bilibili_ranker.storage import load_frequency_csvs

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        good = root / "word_frequency_20240101T000000Z.csv"
        good.write_text("词,词频\n魔方,2\n", encoding="utf-8-sig")
        gbk = root / "word_frequency_20240102T000000Z.csv"
        gbk.write_bytes("词,词频\n测试,5\n".encode("gbk"))
        oversized = root / "word_frequency_20240103T000000Z.csv"
        oversized.write_text("词,词频\n" + "长" * 140000 + ",1\n", encoding="utf-8")

        merged = load_frequency_csvs((gbk, oversized, good))

    assert merged == {"魔方": 2}


def test_load_frequency_csvs_warns_skipped_files_to_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # 坏文件跳过不能是静默的：聚合卖点是跨天累计，整份快照消失时退出码与摘要照常
    # 正常，日更跑在 Actions 上没人比对行数，累计口径少一天无法察觉。跳过必须警告
    # 到 stderr 并点名文件；全部可读时一个字都不能有，不给 CI 添噪声。
    from bilibili_ranker.storage import load_frequency_csvs

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        good = root / "word_frequency_20240101T000000Z.csv"
        good.write_text("词,词频\n魔方,2\n", encoding="utf-8-sig")
        gbk = root / "word_frequency_20240102T000000Z.csv"
        gbk.write_bytes("词,词频\n测试,5\n".encode("gbk"))

        merged = load_frequency_csvs((gbk, good))
        warned = capsys.readouterr().err

        clean = load_frequency_csvs((good,))
        quiet = capsys.readouterr().err

    assert merged == {"魔方": 2}
    assert "word_frequency_20240102T000000Z.csv" in warned
    assert clean == {"魔方": 2}
    assert quiet == ""


@given(
    # 域 = 真实可达的词：utf-8 可编码（surrogate 连 utf-8-sig 都编不出）且非 NUL——
    # 3.10 的 csv 读写两侧都把未设置的 escapechar 存成 '\0'，词里含 NUL 会被当转义符
    # 吞掉或直接抛 "need to escape"（3.14 已无此问题）。分词 chunk 模式只匹配字母/
    # 数字/中日韩文区间，这两类控制字符本就不可达。
    word=st.text(
        min_size=1, alphabet=st.characters(codec="utf-8", exclude_characters="\x00")
    ).filter(lambda value: not value.startswith("'")),
    count=st.integers(min_value=1),
)
def test_frequency_csv_write_read_roundtrip(word: str, count: int) -> None:
    # 写读往返是 --aggregate 闭环的根基：聚合的输入正是 write_frequencies_csv 的产物，
    # 词列经 _spreadsheet_safe 转义、读取侧剥单引号，对任意词必须无损往返（含全部六个
    # 公式前缀、逗号、引号、换行等 csv 元字符）。词首带 ' 的输入读取时被剥一层引号，
    # 是读取侧的既定防御行为——分词 chunk 永不以 ' 开头，仅手改文件可达，不在域内。
    from bilibili_ranker.storage import load_frequency_csvs, write_frequencies_csv

    with tempfile.TemporaryDirectory() as directory:
        destination = Path(directory) / "freq.csv"
        write_frequencies_csv(destination, {word: count})
        loaded = load_frequency_csvs((destination,))

    assert loaded == {word: count}
