"""回归检查：去重、停用词、解析容错与 CSV 原子写入。

需要已安装项目依赖（jieba、stopwordsiso）。可直接运行：
python tests/test_core.py
或由 pytest 收集。
"""

from __future__ import annotations

import csv
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bilibili_ranker.cleaner import deduplicate_records
from bilibili_ranker.models import VideoRankingRecord, parse_ranking_records
from bilibili_ranker.stopwords import load_stopword_policy
from bilibili_ranker.storage import write_records_csv


def test_stopword_policy() -> None:
    policy = load_stopword_policy()

    # allowlist 词在加载时已从 stopwords 中剔除，should_remove 无需再判 allowlist。
    for word in ("ai", "acg", "asmr", "c++"):
        assert word not in policy.stopwords
        assert policy.should_remove(word) is False

    # 项目自定义停用词仍被移除。
    for word in ("视频", "bilibili", "完整版"):
        assert policy.should_remove(word) is True

    # languages 传字符串应被拒绝，而不是逐字符迭代。
    try:
        load_stopword_policy(languages="zh,en")
    except TypeError:
        pass
    else:
        raise AssertionError("languages 传字符串未抛出 TypeError")


def test_deduplicate_records() -> None:
    # BV 号 base58 大小写敏感，仅按完整字符串精确去重，不依赖 rank 值。
    items = [
        {"bvid": "BV1aa", "title": "t1", "owner": {}, "stat": {}},
        {"bvid": "BV1aa", "title": "t2", "owner": {}, "stat": {}},  # 重复项
        {"bvid": "bv1aa", "title": "t3", "owner": {}, "stat": {}},  # 大小写不同，不同视频
        {"bvid": "BV1bb", "title": "t4", "owner": {}, "stat": {}},
    ]
    records, _ = parse_ranking_records(items)
    accepted, rejected = deduplicate_records(records)
    assert rejected == 1
    assert [r.bvid for r in accepted] == ["BV1aa", "bv1aa", "BV1bb"]


def test_parse_ranking_records_tolerance() -> None:
    items = [
        {"bvid": "BV1aa", "title": "t1", "owner": {}, "stat": {}},
        {"bvid": "", "title": "t2", "owner": {}, "stat": {}},  # 缺 bvid，拒绝
        {"bvid": "BV1bb", "title": "", "owner": {}, "stat": {}},  # 缺标题，拒绝
        {
            "bvid": "BV1cc",
            "title": "t4",
            "owner": {},
            "stat": {"view": "123", "danmaku": "-1", "reply": "1.9"},
        },
        {"bvid": "BV1dd", "title": "t5", "owner": {}, "stat": {"view": 1.9, "coin": 2.0}},
    ]
    records, rejected = parse_ranking_records(items)
    assert rejected == 2
    assert [r.bvid for r in records] == ["BV1aa", "BV1cc", "BV1dd"]

    # 整数文本正常解析，负数与小数文本拒绝。
    assert records[1].view_count == 123
    assert records[1].danmaku_count is None
    assert records[1].reply_count is None
    # 非整型 float 拒绝，整型 float 可解析。
    assert records[2].view_count is None
    assert records[2].coin_count == 2


def test_atomic_csv_write() -> None:
    record = VideoRankingRecord.from_api_item(
        {"bvid": "BV1aa", "title": "标题", "owner": {}, "stat": {}},
        rank=1,
    )
    with tempfile.TemporaryDirectory() as directory:
        destination = Path(directory) / "out.csv"
        write_records_csv(destination, [record])
        with destination.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        assert len(rows) == 1
        assert rows[0]["BV号"] == "BV1aa"
        assert rows[0]["排名"] == "1"
        # 失败写入不留临时文件。
        leftovers = [p.name for p in Path(directory).iterdir() if p.name.endswith(".tmp")]
        assert leftovers == []


if __name__ == "__main__":
    test_stopword_policy()
    test_deduplicate_records()
    test_parse_ranking_records_tolerance()
    test_atomic_csv_write()
    print("ok")
