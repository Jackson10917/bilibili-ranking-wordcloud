"""自动层的关键边界：按日去重、同一视频驻榜、保护词、趋势与失败退出。"""

import csv
from pathlib import Path

import pytest

from bilibili_ranker.cli import main
from bilibili_ranker.stopword_optimizer import optimize_stopwords


def _snapshot(path: Path, title: str = "三连 ai 原神", *, diverse: bool = True) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["BV号", "视频标题", "主分区"])
        for index in range(6):
            bvid = f"BV{index}" if diverse else "BV0"
            writer.writerow([bvid, title, f"分区{index % 3}"])
            writer.writerow([bvid, title, f"分区{index % 3}"])


def test_optimizer_applies_only_stable_noise_and_recomputes(tmp_path, monkeypatch):
    import bilibili_ranker.stopword_optimizer as optimizer

    data = tmp_path / "data"
    data.mkdir()
    output = tmp_path / "analysis"
    for day in range(1, 15):
        _snapshot(data / f"ranking_202601{day:02d}T000000Z.csv")
    # 同秒后缀 10 比 2 新；一天三份不能重复计数或按字典序取旧文件。
    _snapshot(data / "ranking_20260114T000000Z-2.csv", "过时标题")
    _snapshot(data / "ranking_20260114T000000Z-10.csv")
    monkeypatch.setattr(optimizer, "_AUTO_NOISE", frozenset({"三连", "ai"}))
    result = optimize_stopwords(data, output)
    assert result["snapshot_days"] == 14
    assert result["automatic_words"] == ["三连"]
    baseline = (output / "baseline/word_frequency_aggregate.csv").read_text(encoding="utf-8-sig")
    current = (output / "current/word_frequency_aggregate.csv").read_text(encoding="utf-8-sig")
    assert "三连,84" in baseline and "三连," not in current
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
                "--min-total",
                "999",
            ]
        )
        == 0
    )
    assert "三连" not in (output / "auto_stopwords.txt").read_text(encoding="utf-8")
    assert "三连,84" in (output / "current/word_frequency_aggregate.csv").read_text(
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
    result = optimize_stopwords(tmp_path, tmp_path / "analysis", min_total=1)
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
    for options in ({"min_days": 0}, {"min_total": 0}, {"min_day_ratio": 1.1}):
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


def test_multiword_noise_can_actually_be_learned(tmp_path):
    for day in range(1, 15):
        _snapshot(tmp_path / f"ranking_202601{day:02d}T000000Z.csv", "感谢观看 原神")
    result = optimize_stopwords(tmp_path, tmp_path / "analysis")
    assert result["automatic_words"] == ["感谢观看"]
