import argparse
import csv
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from functools import cache
from pathlib import Path

import jieba
import requests
from jieba import posseg
from stopwordsiso import stopwords as iso_stopwords
from wordcloud import WordCloud

WORDS = Path(__file__).with_name("words")
BEIJING = timezone(timedelta(hours=8))
FONT_PATHS = (
    "C:/Windows/Fonts/msyh.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
)
RANKING_HEADERS = (
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
)


def normalize(text):
    return unicodedata.normalize("NFKC", text).casefold()


def read_words(name):
    return {
        normalize(line.strip())
        for line in (WORDS / name).read_text(encoding="utf-8-sig").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def load_words():
    jieba.setLogLevel("ERROR")
    jieba.load_userdict(str(WORDS / "userdict.txt"))
    keep = read_words("keep.txt")
    stopwords = {normalize(word) for word in iso_stopwords(["zh", "en"])}
    stopwords = (stopwords | read_words("stopwords.txt")) - keep
    phrases = sorted(read_words("phrases.txt"), key=lambda word: (-len(word), word))
    return stopwords, keep, phrases


def write_csv(path, headers, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(headers)
        writer.writerows(rows)


def spreadsheet_safe(text):
    return "'" + text if text.startswith(("=", "+", "-", "@")) else text


def write_snapshot(items, data):
    rows = []
    for rank, item in enumerate(items, 1):
        rows.append(
            [
                rank,
                item["bvid"],
                f"https://www.bilibili.com/video/{item['bvid']}",
                spreadsheet_safe(item["title"]),
                spreadsheet_safe(item.get("tnamev2") or item["tname"]),
                spreadsheet_safe(item.get("pid_name_v2", "")),
                spreadsheet_safe(item["owner"]["name"]),
                datetime.fromtimestamp(item["pubdate"], BEIJING).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                item["duration"],
                *(
                    item["stat"].get(key)
                    for key in (
                        "view",
                        "danmaku",
                        "reply",
                        "favorite",
                        "coin",
                        "share",
                        "like",
                    )
                ),
            ]
        )
    path = data / f"ranking_{datetime.now(BEIJING):%Y-%m-%d}.csv"
    write_csv(path, RANKING_HEADERS, rows)
    return path


def fetch(data):
    with requests.Session() as session:
        session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/140.0.0.0 Safari/537.36"
                ),
                "Referer": "https://www.bilibili.com/v/popular/rank/all",
                "Accept": "application/json, text/plain, */*",
            }
        )
        response = session.get(
            "https://api.bilibili.com/x/frontend/finger/spi", timeout=15
        )
        response.raise_for_status()
        cookies = response.json()["data"]
        session.cookies.set("buvid3", cookies["b_3"], domain=".bilibili.com")
        session.cookies.set("buvid4", cookies["b_4"], domain=".bilibili.com")
        for attempt in range(3):
            response = session.get(
                "https://api.bilibili.com/x/web-interface/ranking/v2",
                params={"rid": 0, "type": "all"},
                timeout=15,
            )
            response.raise_for_status()
            payload = response.json()
            if payload["code"] == 0:
                return write_snapshot(payload["data"]["list"], data)
            if attempt < 2:
                time.sleep(1)
        raise RuntimeError(f"B站返回 code={payload['code']}：{payload.get('message')}")


def tokenize(title, stopwords, phrases):
    title = normalize(title)
    for phrase in phrases:
        title = title.replace(phrase, " ")
    return [
        word
        for word in jieba.lcut(title)
        if len(word) >= 2
        and any(character.isalpha() for character in word)
        and word not in stopwords
    ]


def read_days(data, stopwords, phrases):
    days = []
    for path in sorted(data.glob("ranking_*.csv")):
        with path.open(encoding="utf-8-sig", newline="") as stream:
            days.append(
                [
                    (
                        row["UP主"] or row["BV号"],
                        tokenize(row["视频标题"], stopwords, phrases),
                    )
                    for row in csv.DictReader(stream)
                ]
            )
    return days


@cache
def pos_flags(word):
    return {piece.flag for piece in posseg.lcut(word)}


def auto_stopwords(days, keep):
    current = set()
    for end in range(14, len(days) + 1):
        window = days[max(0, end - 28) : end]
        present, uploaders = Counter(), defaultdict(set)
        for day in window:
            present.update({word for _, words in day for word in words})
            for uploader, words in day:
                for word in set(words):
                    uploaders[word].add(uploader)
        current = {word for word in current if present[word] / len(window) >= 0.25}
        current.update(
            word
            for word, count in present.items()
            if count / len(window) >= 0.5
            and len(uploaders[word]) >= 5
            and pos_flags(word) <= {"r", "d", "c", "p", "u", "y", "e", "o", "q"}
        )
    return current - keep


def trend_rows(counts):
    previous = sum(counts[-14:-7], Counter())
    recent = sum(counts[-7:], Counter())
    rows = [
        (word, previous[word], recent[word], recent[word] - previous[word])
        for word in previous.keys() | recent.keys()
        if max(previous[word], recent[word]) >= 2
    ]
    return sorted(rows, key=lambda row: (-row[3], -row[2], row[0]))


def resolve_font(font=None):
    return font or next(path for path in FONT_PATHS if Path(path).is_file())


def report(data, out, font=None):
    stopwords, keep, phrases = load_words()
    days = read_days(data, stopwords, phrases)
    automatic = auto_stopwords(days, keep)
    counts = [
        Counter(word for _, words in day for word in words if word not in automatic)
        for day in days
    ]
    total = dict(
        sorted(sum(counts, Counter()).items(), key=lambda item: (-item[1], item[0]))
    )
    write_csv(out / "word_frequency.csv", ("词", "次数"), total.items())
    write_csv(out / "trend.csv", ("词", "上周", "本周", "变化"), trend_rows(counts))
    (out / "auto_stopwords.txt").write_text(
        "".join(word + "\n" for word in sorted(automatic)), encoding="utf-8"
    )
    WordCloud(
        font_path=str(resolve_font(font)),
        width=1920,
        height=1080,
        max_words=300,
        background_color="white",
        random_state=42,
    ).generate_from_frequencies(total).to_file(str(out / "wordcloud.png"))


def main():
    parser = argparse.ArgumentParser(description="抓取B站排行榜并生成词云")
    commands = parser.add_subparsers(dest="command", required=True)
    fetch_parser = commands.add_parser("fetch", help="抓取当天榜单")
    fetch_parser.add_argument("--data", type=Path, default=Path("data"))
    report_parser = commands.add_parser("report", help="生成累计词频、趋势和词云")
    report_parser.add_argument("--data", type=Path, default=Path("data"))
    report_parser.add_argument("--out", type=Path, default=Path("results"))
    report_parser.add_argument("--font", type=Path)
    args = parser.parse_args()
    if args.command == "fetch":
        print(fetch(args.data))
    else:
        report(args.data, args.out, args.font)
        print(args.out)


if __name__ == "__main__":
    main()
