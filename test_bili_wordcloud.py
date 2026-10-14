import csv
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest
from PIL import Image

import bili_wordcloud as bili


def item(title="世界 世界 动画 ＡＩ"):
    return {
        "bvid": "BV1xx411c7mD",
        "title": title,
        "tname": "旧分区",
        "tnamev2": "新分区",
        "pid_name_v2": "主分区",
        "owner": {"name": "测试UP"},
        "pubdate": 1767225600,
        "duration": 60,
        "stat": {
            "view": 10,
            "danmaku": 20,
            "reply": 30,
            "favorite": 40,
            "coin": 50,
            "share": 60,
            "like": 70,
        },
    }


def test_tokenize_and_keep():
    stopwords, keep, phrases = bili.load_words()
    tokens = bili.tokenize("ＡＩ MV vs 4Ｋ 世界 今天 123 !!! 人", stopwords, phrases)
    assert tokens == ["ai", "mv", "vs", "4k", "世界"]
    assert {"ai", "mv", "vs"} <= keep
    assert not ({"今天", "123", "!!!", "人"} & set(tokens))


def test_remove_phrases():
    stopwords, _, phrases = bili.load_words()
    assert bili.tokenize("全民制作人 世界", stopwords, phrases) == ["世界"]
    assert bili.tokenize("制作 世界", stopwords, phrases) == ["制作", "世界"]


def test_interaction_stopwords():
    stopwords, _, phrases = bili.load_words()
    interactions = (
        "一键三连 求三连 三连 求点赞 求关注 求收藏 求投币 点个赞 点赞三连 "
        "下期再见 敬请期待 不见不散 持续更新 点赞关注 感谢观看"
    )
    assert set(interactions.split()) <= stopwords
    assert bili.tokenize(interactions, stopwords, phrases) == []


@pytest.mark.parametrize("prefix", ["=", "+", "-", "@", "\t", "\r"])
def test_snapshot_roundtrip(tmp_path, monkeypatch, prefix):
    clock = Mock(wraps=datetime)
    clock.now.return_value = datetime(2026, 10, 14, 16, 30, tzinfo=UTC).astimezone(
        bili.BEIJING
    )
    monkeypatch.setattr(bili, "datetime", clock)
    path = bili.write_snapshot([item(prefix + "世界")], tmp_path)
    assert path.name == "ranking_2026-10-15.csv"
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.reader(stream))
    assert rows[0] == [
        "排名",
        "BV号",
        "视频链接",
        "视频标题",
        "视频分区",
        "主分区",
        "UP主",
        "发布时间（北京时间）",
        "视频时长（秒）",
        "播放量",
        "弹幕数",
        "评论数",
        "收藏数",
        "投币数",
        "分享数",
        "点赞数",
    ]
    assert rows[1] == [
        "1",
        "BV1xx411c7mD",
        "https://www.bilibili.com/video/BV1xx411c7mD",
        "'" + prefix + "世界",
        "新分区",
        "主分区",
        "测试UP",
        "2026-01-01 08:00:00",
        "60",
        "10",
        "20",
        "30",
        "40",
        "50",
        "60",
        "70",
    ]
    assert bili.read_days(tmp_path, set(), []) == [[("测试UP", ["世界"])]]
    assert bili.write_snapshot([item("动画")], tmp_path) == path
    assert len(list(tmp_path.glob("ranking_*.csv"))) == 1
    assert bili.read_days(tmp_path, set(), []) == [[("测试UP", ["动画"])]]


def test_auto_starts_on_day_14():
    day = [(f"up-{n}", ["到底", "到底"]) for n in range(5)]
    assert bili.auto_stopwords([day] * 13, set()) == set()
    assert bili.auto_stopwords([day] * 14, set()) == {"到底"}
    assert bili.auto_stopwords([day[:4]] * 14, set()) == set()


def test_auto_retains_at_quarter_and_recovers_below():
    day = [(f"up-{n}", ["到底"]) for n in range(5)]
    days = [day] * 7 + [[]] * 7
    assert bili.auto_stopwords(days, set()) == {"到底"}
    assert bili.auto_stopwords(days + [[]] * 14, set()) == {"到底"}
    assert bili.auto_stopwords(days + [[]] * 15, set()) == set()
    assert bili.auto_stopwords([day] * 6 + [[]] * 8, set()) == set()


def test_auto_keep_and_pos():
    day = [(f"up-{n}", ["到底", "世界", "到底世界"]) for n in range(5)]
    assert bili.auto_stopwords([day] * 14, set()) == {"到底"}
    assert bili.auto_stopwords([day] * 14, {"到底"}) == set()


def test_trend_counts_and_bounded_windows():
    old = [Counter({"旧历史": 100})] * 7
    previous = [Counter({"增加": 1, "减少": 2, "零星": 1})] + [Counter()] * 6
    recent = [Counter({"增加": 3, "新增": 2, "零星": 1})] + [Counter()] * 6
    assert bili.trend_rows(old + previous + recent) == [
        ("增加", 1, 3, 2),
        ("新增", 0, 2, 2),
        ("减少", 2, 0, -2),
    ]


@pytest.mark.parametrize("codes", [(-352, 0), (-352, -352, -352)])
def test_fetch_cookies_retries_and_limit(tmp_path, monkeypatch, codes):
    session = MagicMock()
    session.__enter__.return_value = session
    session.cookies = bili.requests.cookies.RequestsCookieJar()
    payloads = [{"data": {"b_3": "cookie3", "b_4": "cookie4"}}] + [
        {"code": code, "message": "测试", "data": {"list": [item()]}} for code in codes
    ]
    session.get.side_effect = [
        Mock(
            json=Mock(return_value=payload),
            raise_for_status=Mock(
                side_effect=bili.requests.HTTPError("HTTP 412")
                if payload.get("code") == -352
                else None
            ),
        )
        for payload in payloads
    ]
    monkeypatch.setattr(bili.requests, "Session", lambda: session)
    sleep = Mock()
    monkeypatch.setattr(bili.time, "sleep", sleep)
    if codes[-1] == 0:
        assert bili.fetch(tmp_path).is_file()
    else:
        with pytest.raises(RuntimeError, match="code=-352"):
            bili.fetch(tmp_path)
        assert not list(tmp_path.glob("*.csv"))
    assert session.get.call_args_list[0].args == (
        "https://api.bilibili.com/x/frontend/finger/spi",
    )
    assert session.get.call_count == len(codes) + 1
    assert session.get.call_args.kwargs["params"] == {"rid": 0, "type": "all"}
    assert {
        (cookie.name, cookie.value, cookie.domain) for cookie in session.cookies
    } == {
        ("buvid3", "cookie3", ".bilibili.com"),
        ("buvid4", "cookie4", ".bilibili.com"),
    }
    assert sleep.call_count == len(codes) - 1


def test_font_priority(monkeypatch):
    monkeypatch.setattr(Path, "is_file", lambda path: True)
    assert bili.resolve_font("chosen.ttf") == "chosen.ttf"
    assert bili.resolve_font() == "C:/Windows/Fonts/msyh.ttc"
    monkeypatch.setattr(Path, "is_file", lambda path: path == Path(bili.FONT_PATHS[1]))
    assert (
        bili.resolve_font() == "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
    )


def test_report_cli(tmp_path):
    data, out = tmp_path / "data", tmp_path / "results"
    bili.write_snapshot([item()], data)
    subprocess.run(
        [
            sys.executable,
            str(Path(bili.__file__)),
            "report",
            "--data",
            str(data),
            "--out",
            str(out),
        ],
        check=True,
    )
    assert {path.name for path in out.iterdir()} == {
        "word_frequency.csv",
        "trend.csv",
        "auto_stopwords.txt",
        "wordcloud.png",
    }
    with (out / "word_frequency.csv").open(encoding="utf-8-sig", newline="") as stream:
        assert list(csv.reader(stream)) == [
            ["词", "次数"],
            ["世界", "2"],
            ["ai", "1"],
            ["动画", "1"],
        ]
    with (out / "trend.csv").open(encoding="utf-8-sig", newline="") as stream:
        assert list(csv.reader(stream)) == [
            ["词", "上周", "本周", "变化"],
            ["世界", "0", "2", "2"],
        ]
    assert (out / "auto_stopwords.txt").read_text(encoding="utf-8") == ""
    with Image.open(out / "wordcloud.png") as image:
        assert image.size == (1920, 1080)
        assert image.format == "PNG"
