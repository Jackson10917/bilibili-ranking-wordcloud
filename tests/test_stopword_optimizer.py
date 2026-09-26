"""停用词分析的关键边界：按日去重、同一视频驻榜、专名与保留词、人工决定、趋势与失败退出。"""

import csv
from pathlib import Path

import pytest

from bilibili_ranker.cli import main
from bilibili_ranker.stopword_optimizer import optimize_stopwords


def _snapshot(path: Path, title: str = "这种 ai 原神", *, diverse: bool = True) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["BV号", "视频标题", "主分区"])
        for index in range(6):
            bvid = f"BV{index}" if diverse else "BV0"
            writer.writerow([bvid, title, f"分区{index % 3}"])
            writer.writerow([bvid, title, f"分区{index % 3}"])


def test_optimizer_applies_only_stable_function_words_and_recomputes(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    output = tmp_path / "analysis"
    for day in range(1, 15):
        _snapshot(data / f"ranking_202601{day:02d}T000000Z.csv")
    # 同秒后缀 10 比 2 新；一天三份不能重复计数或按字典序取旧文件。
    _snapshot(data / "ranking_20260114T000000Z-2.csv", "过时标题")
    _snapshot(data / "ranking_20260114T000000Z-10.csv")
    result = optimize_stopwords(data, output)
    # 这种=代词、跨 3 分区均匀、两窗稳定 → 自动；原神=专名、ai=保留词 → 永不提名。
    assert result["snapshot_days"] == 14
    assert result["automatic_words"] == ["这种"]
    with (output / "stopword_candidates.csv").open(encoding="utf-8-sig", newline="") as stream:
        assert [row["词"] for row in csv.DictReader(stream)] == ["这种"]
    baseline = (output / "baseline/word_frequency_aggregate.csv").read_text(encoding="utf-8-sig")
    current = (output / "current/word_frequency_aggregate.csv").read_text(encoding="utf-8-sig")
    assert "这种,84" in baseline and "这种," not in current
    assert "原神,84" in current and "ai,84" in current
    assert "过时" not in baseline
    assert (output / "current/word_frequency_trend.csv").is_file()
    original = {path: path.read_bytes() for path in data.iterdir()}
    assert (
        main(
            [
                "stopword-analyze",
                "--data-dir",
                str(data),
                "--output-dir",
                str(output),
                "--min-uploaders",
                "999",
            ]
        )
        == 0
    )
    assert "这种" not in (output / "auto_stopwords.txt").read_text(encoding="utf-8")
    assert "这种,84" in (output / "current/word_frequency_aggregate.csv").read_text(
        encoding="utf-8-sig"
    )
    assert all(path.read_bytes() == content for path, content in original.items())
    # 缩短历史后不保留旧趋势文件或自动停用词。
    for day in range(2, 15):
        for path in data.glob(f"ranking_202601{day:02d}*.csv"):
            path.unlink()
    assert main(["stopword-candidates", "--data-dir", str(data), "--output-dir", str(output)]) == 0
    assert not (output / "current/word_frequency_trend.csv").exists()


def test_repeated_video_does_not_qualify(tmp_path):
    for day in range(1, 15):
        _snapshot(tmp_path / f"ranking_202601{day:02d}T000000Z.csv", diverse=False)
    result = optimize_stopwords(tmp_path, tmp_path / "analysis")
    assert result["automatic_words"] == []


@pytest.mark.parametrize("contents", ["bad,headers\n", "BV号,视频标题\n", "BV号,视频标题\na,\n"])
def test_bad_snapshot_does_not_replace_previous_outputs(tmp_path, contents):
    (tmp_path / "ranking_20260101T000000Z.csv").write_text(contents, encoding="utf-8")
    output = tmp_path / "analysis"
    output.mkdir()
    existing = output / "auto_stopwords.txt"
    existing.write_text("previous", encoding="utf-8")
    assert main(["stopword-analyze", "--data-dir", str(tmp_path), "--output-dir", str(output)]) == 1
    assert existing.read_text(encoding="utf-8") == "previous"


def test_invalid_options_and_missing_history(tmp_path):
    for options in ({"min_days": 0}, {"min_uploaders": 0}, {"min_day_ratio": 1.1}):
        with pytest.raises(ValueError):
            optimize_stopwords(tmp_path, tmp_path / "analysis", **options)
    for data, output in (
        (tmp_path / "missing", tmp_path / "analysis"),
        (tmp_path, tmp_path),
        (tmp_path, tmp_path / "analysis"),
    ):
        with pytest.raises(ValueError):
            optimize_stopwords(data, output)


def test_generation_failure_preserves_outputs_and_unrelated_files(tmp_path, monkeypatch):
    import bilibili_ranker.stopword_optimizer as optimizer

    data = tmp_path / "data"
    data.mkdir()
    _snapshot(data / "ranking_20260101T000000Z.csv")
    output = tmp_path / "analysis"
    optimize_stopwords(data, output)
    note = output / "current/word_frequency_notes.csv"
    note.write_text("user notes", encoding="utf-8")
    png = output / "current/wordcloud_aggregate.png"
    png.write_bytes(b"old image")
    previous = {p.relative_to(output): p.read_bytes() for p in output.rglob("*") if p.is_file()}
    original = optimizer.write_frequencies_csv

    def fail(*args, **kwargs):
        raise OSError("simulated disk failure during generation")

    monkeypatch.setattr(optimizer, "write_frequencies_csv", fail)
    with pytest.raises(OSError):
        optimize_stopwords(data, output)
    assert previous == {
        p.relative_to(output): p.read_bytes() for p in output.rglob("*") if p.is_file()
    }
    monkeypatch.setattr(optimizer, "write_frequencies_csv", original)
    optimize_stopwords(data, output)
    assert note.read_text(encoding="utf-8") == "user notes"
    assert not png.exists()
    assert not list(tmp_path.glob(".stopword-*"))


def test_manual_decisions_are_remembered(tmp_path):
    # 人工决定表：停用 → 从 current 去掉且不再提名；保留 → 不再提名、永不自动生效。
    data = tmp_path / "data"
    data.mkdir()
    for day in range(1, 15):
        _snapshot(data / f"ranking_202601{day:02d}T000000Z.csv", "这种 挑战 原神")
    output = tmp_path / "analysis"
    optimize_stopwords(data, output)
    decisions = output / "stopword_decisions.csv"
    assert decisions.read_text(encoding="utf-8-sig") == "词,决定,备注\n"  # 缺失时建模板
    decisions.write_text("词,决定,备注\n挑战,停用,\n这种,保留,误伤\n", encoding="utf-8-sig")
    result = optimize_stopwords(data, output)
    assert result["automatic_words"] == [] and result["candidates"] == 0
    assert (result["manual_stopwords"], result["reviewed_keep"]) == (1, 1)
    current = (output / "current/word_frequency_aggregate.csv").read_text(encoding="utf-8-sig")
    assert "挑战," not in current and "这种,84" in current
    assert decisions.read_text(encoding="utf-8-sig").endswith("误伤\n")  # 工具从不改写
    for bad in ("词,决定\n挑战,删掉\n", "词,决定\n挑战,停用\n挑战,保留\n", "词\n挑战\n"):
        decisions.write_text(bad, encoding="utf-8-sig")
        with pytest.raises(ValueError):
            optimize_stopwords(data, output)


def test_header_only_snapshot_is_skipped_not_fatal(tmp_path, capsys):
    # 抓榜整榜解析失败会故意留下只有表头的 CSV，不能让它卡死整次分析。
    data = tmp_path / "data"
    data.mkdir()
    _snapshot(data / "ranking_20260101T000000Z.csv")
    (data / "ranking_20260102T000000Z.csv").write_text("BV号,视频标题\n", encoding="utf-8-sig")
    assert optimize_stopwords(data, tmp_path / "analysis")["snapshot_days"] == 1
    assert "ranking_20260102T000000Z.csv" in capsys.readouterr().err
    # 全部为空仍按错误退出。
    (data / "ranking_20260101T000000Z.csv").write_text("BV号,视频标题\n", encoding="utf-8-sig")
    with pytest.raises(ValueError):
        optimize_stopwords(data, tmp_path / "analysis")


def test_decisions_file_errors_are_actionable(tmp_path, capsys):
    data = tmp_path / "data"
    data.mkdir()
    _snapshot(data / "ranking_20260101T000000Z.csv")
    output = tmp_path / "analysis"
    decisions = output / "stopword_decisions.csv"
    output.mkdir()
    # 中文 Windows 的 Excel 默认把 CSV 存成 GBK：报错要点名文件和改法，而不是裸 UnicodeDecodeError。
    decisions.write_bytes("词,决定\n可能,停用\n".encode("gbk"))
    with pytest.raises(ValueError, match="CSV UTF-8"):
        optimize_stopwords(data, output)
    # 备注里带换行（Excel 单元格内换行）不能把后面的行切坏。
    decisions.write_text('词,决定,备注\nai,停用,"第一行\n第二行"\n', encoding="utf-8-sig")
    assert optimize_stopwords(data, output)["manual_stopwords"] == 0
    # 保留词表优先，但不能静默：要告诉用户这条「停用」没生效。
    assert "ai" in capsys.readouterr().err


def test_zero_byte_snapshot_is_skipped(tmp_path, capsys):
    # 抓榜进程被强杀会留下 0 字节占位 CSV；每日任务会把它提交进仓库，不能让之后每天的分析都失败。
    _snapshot(tmp_path / "ranking_20260101T000000Z.csv")
    (tmp_path / "ranking_20260102T000000Z.csv").write_bytes(b"")
    assert optimize_stopwords(tmp_path, tmp_path / "analysis")["snapshot_days"] == 1
    assert "ranking_20260102T000000Z.csv" in capsys.readouterr().err


def test_split_phrase_decision_warns(tmp_path, capsys):
    # 「全网最强」会被切成 全网 + 最强，停用永远匹配不到；要提示，而不是静默计入人工停用词。
    _snapshot(tmp_path / "ranking_20260101T000000Z.csv", "全网最强攻略")
    output = tmp_path / "analysis"
    output.mkdir()
    (output / "stopword_decisions.csv").write_text("词,决定\n全网最强,停用\n", encoding="utf-8-sig")
    optimize_stopwords(tmp_path, output)
    assert "全网最强" in capsys.readouterr().err


def test_locked_destination_leaves_outputs_untouched(tmp_path, monkeypatch):
    # Windows 上 Excel 开着 CSV 时替换会失败；必须在动任何文件之前失败，不能留下半新半旧的结果。
    _snapshot(tmp_path / "ranking_20260101T000000Z.csv")
    output = tmp_path / "analysis"
    optimize_stopwords(tmp_path, output)
    before = {p: p.read_bytes() for p in output.rglob("*") if p.is_file()}
    locked = output / "stopword_candidates.csv"
    real_open = Path.open

    def fake_open(self, mode="r", *args, **kwargs):
        if self == locked and "a" in mode:
            raise PermissionError("locked by Excel")
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fake_open)
    _snapshot(tmp_path / "ranking_20260102T000000Z.csv", "全新标题")
    with pytest.raises(PermissionError):
        optimize_stopwords(tmp_path, output)
    assert before == {p: p.read_bytes() for p in output.rglob("*") if p.is_file()}
