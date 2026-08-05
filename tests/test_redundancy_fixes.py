"""回归检查：冗余清理（去重/停用词/字体查找）不改变行为。

零依赖，直接运行：python tests/test_redundancy_fixes.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bilibili_ranker.cleaner import deduplicate_records
from bilibili_ranker.models import parse_ranking_records
from bilibili_ranker.stopwords import load_stopword_policy


def _check() -> None:
    policy = load_stopword_policy()

    # allowlist 词在加载时已从 stopwords 中剔除，should_remove 无需再判 allowlist。
    for word in ("ai", "acg", "asmr", "c++"):
        assert word not in policy.stopwords
        assert policy.should_remove(word) is False

    # 项目自定义停用词仍被移除。
    for word in ("视频", "bilibili", "完整版"):
        assert policy.should_remove(word) is True

    # 去重只关心 bvid（大小写不敏感），不依赖 rank 值。
    items = [
        {"bvid": "BV1aa", "title": "t1", "owner": {}, "stat": {}},
        {"bvid": "bv1aa", "title": "t2", "owner": {}, "stat": {}},  # 同 BV，大小写不同
        {"bvid": "BV1bb", "title": "t3", "owner": {}, "stat": {}},
    ]
    records, _ = parse_ranking_records(items)
    accepted, rejected = deduplicate_records(records)
    assert rejected == 1
    assert [r.bvid for r in accepted] == ["BV1aa", "BV1bb"]


if __name__ == "__main__":
    _check()
    print("ok")
