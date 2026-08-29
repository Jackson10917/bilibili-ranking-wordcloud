"""命令行流程的回归测试。

由 tests/test_core.py 按源码模块拆分而来；统一由 pytest 收集运行：python -m pytest tests
"""

from __future__ import annotations

import csv
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from bilibili_ranker.cli import main


def test_non_finite_timeout_rejected() -> None:
    # nan/inf 都能通过 `<= 0`，inf 会在 socket.settimeout 抛 OverflowError 逸出错误处理。
    for value in ("nan", "inf", "-inf", "0"):
        try:
            main(["--timeout", value])
        except SystemExit as exc:
            assert exc.code == 2, value
        else:
            raise AssertionError(f"--timeout {value} 应该被拒绝")


def test_oversized_timeout_rejected() -> None:
    # 1e9 起 socket.settimeout 就抛 OverflowError，只校验"有限且 > 0"挡不住，
    # main 也不捕获 OverflowError，用户会看到 traceback。
    from bilibili_ranker.client import BilibiliAPIError, fetch_all_ranking

    for value in ("1e10", "1e100", "86401"):
        try:
            main(["--timeout", value])
        except SystemExit as exc:
            assert exc.code == 2, value
        else:
            raise AssertionError(f"--timeout {value} 应该被拒绝")

    # 库函数直接调用同样要挡住，且错误类型与本模块其余错误一致。
    for bad in (1e10, -1, 0, float("inf"), float("nan")):
        try:
            fetch_all_ranking(timeout_seconds=bad)
        except BilibiliAPIError:
            pass
        else:
            raise AssertionError(f"timeout_seconds={bad} 应该被拒绝")


def test_empty_result_exits_nonzero() -> None:
    # 接口成功但整榜解析失败时返回 0，会让定时任务把只有表头的 CSV 当成功结果。
    from unittest.mock import patch

    import bilibili_ranker.cli as cli_module
    from bilibili_ranker.client import RankingFetchResult

    fetched = RankingFetchResult(
        fetched_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        items=({"bvid": "bad", "title": "字段变更"},),
    )

    with tempfile.TemporaryDirectory() as directory:
        with patch.object(cli_module, "fetch_all_ranking", lambda **_: fetched):
            assert main(["--output-dir", directory]) == 1
        # 退出码非零，但 CSV 仍要落盘，便于排查上游字段变化。
        csv_files = list(Path(directory).glob("ranking_*.csv"))
        assert len(csv_files) == 1, csv_files


def test_stopword_errors_surface_before_network() -> None:
    # 语言代码打错时必须在发请求前就报错，而不是抓完整个榜单再抛异常、CSV 零落盘。
    from unittest.mock import patch

    import bilibili_ranker.cli as cli_module

    def boom(**_: object) -> object:
        raise AssertionError("参数错误时不应该发起网络请求")

    with patch.object(cli_module, "fetch_all_ranking", boom):
        # boom 的 AssertionError 不被 main 的 except 捕获，抓到网络请求就会直接冒出来。
        assert main(["--languages", "zh,xx"]) == 1


def test_csv_written_before_tokenization() -> None:
    # 分词阶段炸掉（jieba 缺失、Ctrl+C 等）不能让已抓完的榜单一个字节不落盘。
    from unittest.mock import patch

    import bilibili_ranker.cli as cli_module
    from bilibili_ranker.client import RankingFetchResult

    fetched = RankingFetchResult(
        fetched_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        items=({"bvid": "BV1aa0000000", "title": "魔方教程"},),
    )

    def boom(self: object, _: object) -> dict[str, int]:
        raise RuntimeError("缺少 jieba，请先安装项目依赖")

    with tempfile.TemporaryDirectory() as directory:
        with patch.object(cli_module, "fetch_all_ranking", lambda **_: fetched):
            with patch.object(cli_module.TitleAnalyzer, "analyze", boom):
                assert main(["--output-dir", directory]) == 1
        csv_files = list(Path(directory).glob("ranking_*.csv"))
        assert len(csv_files) == 1, csv_files
        with csv_files[0].open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        assert [row["BV号"] for row in rows] == ["BV1aa0000000"]


def test_main_reconfigures_stdout_to_utf8() -> None:
    # Windows 上重定向 stdout 时用的是 ANSI 代码页，摘要 JSON 含中文值（输出路径）会抛
    # UnicodeEncodeError：活干完了却报错退出。main 开头必须把两个流都拉到 UTF-8。
    import io
    from unittest.mock import patch

    calls: list[dict[str, object]] = []

    class RecordingStream(io.StringIO):
        def reconfigure(self, **kwargs: object) -> None:
            calls.append(kwargs)

    with (
        patch.object(sys, "stdout", RecordingStream()),
        patch.object(sys, "stderr", RecordingStream()),
    ):
        try:
            main(["--timeout", "0"])  # parser.error 退出前 reconfigure 已执行
        except SystemExit:
            pass

    assert len(calls) == 2, calls
    for kwargs in calls:
        assert kwargs == {"encoding": "utf-8", "errors": "replace"}


def test_cli_success_path_writes_all_outputs() -> None:
    # 端到端成功路径：summary JSON 字段齐全，榜单 CSV、词频 CSV 与 PNG 都真实落盘。
    from unittest.mock import patch

    import pytest

    import bilibili_ranker.cli as cli_module
    from bilibili_ranker.client import RankingFetchResult
    from bilibili_ranker.fonts import resolve_font_path

    try:
        resolve_font_path(None)
    except RuntimeError:
        pytest.skip("环境无 CJK 字体")

    fetched = RankingFetchResult(
        fetched_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        items=(
            {"bvid": "BV1aa0000000", "title": "魔方教程 入门"},
            {"bvid": "BV1aa0000000", "title": "重复项"},  # 去重命中
            {"bvid": "bad", "title": "非法 bvid"},  # 解析拒绝
        ),
    )

    with tempfile.TemporaryDirectory() as directory:
        with patch.object(cli_module, "fetch_all_ranking", lambda **_: fetched):
            assert main(["--output-dir", directory, "--width", "200", "--height", "100"]) == 0

        csv_files = list(Path(directory).glob("ranking_*.csv"))
        png_files = list(Path(directory).glob("wordcloud_*.png"))
        freq_files = list(Path(directory).glob("word_frequency_*.csv"))
        assert len(csv_files) == 1 and len(png_files) == 1 and len(freq_files) == 1
        assert png_files[0].read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
        assert not list(Path(directory).glob("*.tmp"))
        with freq_files[0].open(encoding="utf-8-sig", newline="") as stream:
            freq_rows = list(csv.DictReader(stream))
        assert {"词": "魔方", "词频": "1"} in freq_rows


def test_wordcloud_failure_keeps_csv_and_exits_zero() -> None:
    # 词云渲染失败是降级路径：警告 + CSV 保留 + 退出码 0，不能升级成失败。
    from unittest.mock import patch

    import bilibili_ranker.cli as cli_module
    from bilibili_ranker.client import RankingFetchResult

    fetched = RankingFetchResult(
        fetched_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        items=({"bvid": "BV1aa0000000", "title": "魔方教程"},),
    )

    def boom(*_: object, **__: object) -> Path:
        raise RuntimeError("找不到字体")

    with tempfile.TemporaryDirectory() as directory:
        with patch.object(cli_module, "fetch_all_ranking", lambda **_: fetched):
            with patch.object(cli_module, "render_wordcloud", boom):
                assert main(["--output-dir", directory]) == 0
        assert len(list(Path(directory).glob("ranking_*.csv"))) == 1
        # 词频表先于渲染落盘：降级路径下机器可读产物必须仍然在。
        assert len(list(Path(directory).glob("word_frequency_*.csv"))) == 1
        assert not list(Path(directory).glob("wordcloud_*.png"))


def test_empty_frequencies_still_writes_csv() -> None:
    # 标题清洗后无词元（全是停用词/表情）时只输出榜单 CSV，退出码仍为 0；
    # 词频 CSV 没有内容可写，不产出空表文件。
    from unittest.mock import patch

    import bilibili_ranker.cli as cli_module
    from bilibili_ranker.client import RankingFetchResult

    fetched = RankingFetchResult(
        fetched_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        items=({"bvid": "BV1aa0000000", "title": "🎉🎉🎉"},),
    )

    with tempfile.TemporaryDirectory() as directory:
        with patch.object(cli_module, "fetch_all_ranking", lambda **_: fetched):
            assert main(["--output-dir", directory]) == 0
        assert len(list(Path(directory).glob("ranking_*.csv"))) == 1
        assert not list(Path(directory).glob("wordcloud_*.png"))
        assert not list(Path(directory).glob("word_frequency_*.csv"))


def test_missing_resource_dir_exits_one() -> None:
    # README 承诺 --resource-dir 缺文件退出码 1（值有效但资源不可用），且必须在发请求前报错。
    from unittest.mock import patch

    import bilibili_ranker.cli as cli_module

    def boom(**_: object) -> object:
        raise AssertionError("资源缺失时不应该发起网络请求")

    with tempfile.TemporaryDirectory() as empty_dir:
        with patch.object(cli_module, "fetch_all_ranking", boom):
            assert main(["--resource-dir", empty_dir]) == 1


def test_cli_rejects_out_of_range_args() -> None:
    # 尺寸/最大词数/最短词长取 0、语言列表剥空、rid 为负，都属 argparse 层的取值越界，退出码 2。
    for argv in (
        ["--width", "0"],
        ["--height", "-1"],
        ["--max-words", "0"],
        ["--minimum-token-length", "0"],
        ["--languages", " ,,"],
        ["--rid", "-1"],
    ):
        try:
            main(argv)
        except SystemExit as exc:
            assert exc.code == 2, argv
        else:
            raise AssertionError(f"{argv} 应该被拒绝")


def test_keyboard_interrupt_exits_130() -> None:
    # Ctrl+C 必须走退出码 130（README 已承诺），不能以 KeyboardInterrupt traceback 收场。
    from unittest.mock import patch

    import bilibili_ranker.cli as cli_module

    def interrupt(**_: object) -> object:
        raise KeyboardInterrupt

    with tempfile.TemporaryDirectory() as directory:
        with patch.object(cli_module, "fetch_all_ranking", interrupt):
            assert main(["--output-dir", directory]) == 130


def test_failed_run_removes_empty_placeholder_csv() -> None:
    # create_output_bundle 的 O_EXCL 占位若在 write_records_csv 之前崩溃，会永久残留
    # 0 字节 CSV 并占掉编号（下次运行跳到 -2）。失败路径必须清理空占位。
    from unittest.mock import patch

    import bilibili_ranker.cli as cli_module
    from bilibili_ranker.client import RankingFetchResult

    fetched = RankingFetchResult(
        fetched_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        items=({"bvid": "BV1aa0000000", "title": "魔方教程"},),
    )

    def boom(*_: object, **__: object) -> Path:
        raise KeyboardInterrupt

    with tempfile.TemporaryDirectory() as directory:
        with patch.object(cli_module, "fetch_all_ranking", lambda **_: fetched):
            with patch.object(cli_module, "write_records_csv", boom):
                assert main(["--output-dir", directory]) == 130
        assert not list(Path(directory).glob("ranking_*.csv"))

        # 清理只针对 0 字节占位：写成功后的 CSV 绝不能被后续失败误删。
        with patch.object(cli_module, "fetch_all_ranking", lambda **_: fetched):
            with patch.object(cli_module.TitleAnalyzer, "analyze", boom):
                assert main(["--output-dir", directory]) == 130
        remaining = list(Path(directory).glob("ranking_*.csv"))
        assert len(remaining) == 1 and remaining[0].stat().st_size > 0


def test_version_exported() -> None:
    # 库调用方需要 __version__；未安装时回落到 dev 占位而不是 ImportError。
    import bilibili_ranker

    assert isinstance(bilibili_ranker.__version__, str)
    assert bilibili_ranker.__version__


def test_explicit_font_errors_surface_before_network() -> None:
    # --font-path / BILIBILI_WORDCLOUD_FONT 是用户显式输入，路径写错一个字母时必须与
    # 停用词错误同理在发请求前退出 1；若被词云阶段的降级吞成警告（退出码 0），定时任务
    # 会把没有词云的一次运行误判为成功。自动探测失败才保留「仅警告」降级。
    import os
    from unittest.mock import patch

    import bilibili_ranker.cli as cli_module

    def boom(**_: object) -> object:
        raise AssertionError("显式字体路径无效时不应该发起网络请求")

    with tempfile.TemporaryDirectory() as directory:
        typo = str(Path(directory) / "typo.ttf")
        with patch.object(cli_module, "fetch_all_ranking", boom):
            assert main(["--output-dir", directory, "--font-path", typo]) == 1

            old = os.environ.get("BILIBILI_WORDCLOUD_FONT")
            try:
                os.environ["BILIBILI_WORDCLOUD_FONT"] = typo
                assert main(["--output-dir", directory]) == 1
            finally:
                if old is None:
                    os.environ.pop("BILIBILI_WORDCLOUD_FONT", None)
                else:
                    os.environ["BILIBILI_WORDCLOUD_FONT"] = old


def test_cli_aggregate_merges_history_and_skips_itself() -> None:
    # --aggregate 把目录里已有词频 CSV 与本次结果按词求和；聚合产物自身必须排除，
    # 否则第二次 --aggregate 会把上次的累计值整个再翻一倍。
    # 词云是「替代」而非「额外」：只渲染累计词云，时间戳词云不产出（README 已如此承诺）。
    from unittest.mock import patch

    import bilibili_ranker.cli as cli_module
    from bilibili_ranker.client import RankingFetchResult
    from bilibili_ranker.storage import (
        AGGREGATE_FREQUENCY_CSV_NAME,
        AGGREGATE_WORDCLOUD_PNG_NAME,
    )

    def fetched_on(day: int) -> RankingFetchResult:
        return RankingFetchResult(
            fetched_at=datetime(2024, 3, day, tzinfo=timezone.utc),
            items=({"bvid": "BV1aa0000000", "title": "魔方教程 魔方"},),
        )

    destinations: list[Path] = []

    def fake_render(*args: object, **__: object) -> Path:
        destination = Path(str(args[1]))
        destinations.append(destination)
        destination.write_bytes(b"\x89PNG\r\n\x1a\n")
        return destination

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "word_frequency_20240101T000000Z.csv").write_text(
            "词,词频\n魔方,2\n", encoding="utf-8-sig"
        )
        (root / "word_frequency_20240102T000000Z.csv").write_text(
            "词,词频\n'模型,3\n", encoding="utf-8-sig"
        )

        with (
            patch.object(cli_module, "fetch_all_ranking", lambda **_: fetched_on(1)),
            patch.object(cli_module, "render_wordcloud", fake_render),
        ):
            assert main(["--output-dir", directory, "--aggregate"]) == 0

        aggregate = root / AGGREGATE_FREQUENCY_CSV_NAME
        assert aggregate.exists()
        with aggregate.open(encoding="utf-8-sig", newline="") as stream:
            counts = {row["词"]: row["词频"] for row in csv.DictReader(stream)}
        # 两份历史 + 本次标题（魔方+2、教程+1）。
        assert counts == {"魔方": "4", "教程": "1", "模型": "3"}
        assert destinations == [root / AGGREGATE_WORDCLOUD_PNG_NAME]
        assert not list(root.glob("wordcloud_2024*.png"))

        # 第二次运行：历史里多了第一轮的时间戳词频 CSV，按词继续累加；
        # 若聚合产物没被排除，魔方会变成 10。
        with (
            patch.object(cli_module, "fetch_all_ranking", lambda **_: fetched_on(2)),
            patch.object(cli_module, "render_wordcloud", fake_render),
        ):
            assert main(["--output-dir", directory, "--aggregate"]) == 0
        with aggregate.open(encoding="utf-8-sig", newline="") as stream:
            assert {row["词"]: row["词频"] for row in csv.DictReader(stream)} == {
                "魔方": "6",
                "教程": "2",
                "模型": "3",
            }
        assert destinations == [root / AGGREGATE_WORDCLOUD_PNG_NAME] * 2


def test_cli_aggregate_ignores_non_timestamp_frequency_files() -> None:
    # 合并白名单只收时间戳形态（含 -2 跳号后缀）的词频 CSV：备份/改名产物
    # （word_frequency_aggregate-2.csv 这类）不匹配，不会静默污染累计。
    from unittest.mock import patch

    import bilibili_ranker.cli as cli_module
    from bilibili_ranker.client import RankingFetchResult
    from bilibili_ranker.storage import AGGREGATE_FREQUENCY_CSV_NAME

    def fetched_on(day: int) -> RankingFetchResult:
        return RankingFetchResult(
            fetched_at=datetime(2024, 3, day, tzinfo=timezone.utc),
            items=({"bvid": "BV1aa0000000", "title": "魔方教程"},),
        )

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "word_frequency_20240101T000000Z.csv").write_text(
            "词,词频\n魔方,2\n", encoding="utf-8-sig"
        )
        # 模拟用户给聚合产物做的备份：固定名 + 跳号后缀。
        (root / "word_frequency_aggregate-2.csv").write_text(
            "词,词频\n魔方,8\n", encoding="utf-8-sig"
        )

        with (
            patch.object(cli_module, "fetch_all_ranking", lambda **_: fetched_on(1)),
            patch.object(cli_module, "render_wordcloud", lambda *args, **__: Path(str(args[1]))),
        ):
            assert main(["--output-dir", directory, "--aggregate"]) == 0

        with (root / AGGREGATE_FREQUENCY_CSV_NAME).open(encoding="utf-8-sig", newline="") as stream:
            counts = {row["词"]: row["词频"] for row in csv.DictReader(stream)}
        # 魔方 = 历史 2 + 本次 1；备份里的 8 没被并进来。
        assert counts == {"魔方": "3", "教程": "1"}


def test_cli_user_dict_missing_file_fails_before_network() -> None:
    # 用户词典路径错误必须在抓取前报错退出（退出码 1）；fetch 若被触达会让测试炸出
    # ZeroDivisionError，保证「不白抓一整榜」是实测的而不是假设的。
    from unittest.mock import patch

    import bilibili_ranker.cli as cli_module

    with tempfile.TemporaryDirectory() as directory:
        missing = Path(directory) / "nope.txt"
        with patch.object(cli_module, "fetch_all_ranking", lambda **_: 1 / 0):
            assert main(["--output-dir", directory, "--user-dict", str(missing)]) == 1


def test_cli_user_dict_missing_file_leaves_no_output_dir() -> None:
    # 与 --languages/--font-path 一致：参数写错不能留下空输出目录，所以词典加载
    # 必须排在 mkdir 之前。
    from unittest.mock import patch

    import bilibili_ranker.cli as cli_module

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "out"
        missing = Path(directory) / "nope.txt"
        with patch.object(cli_module, "fetch_all_ranking", lambda **_: 1 / 0):
            assert main(["--output-dir", str(root), "--user-dict", str(missing)]) == 1
        assert not root.exists()
