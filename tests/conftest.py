from datetime import datetime, timezone
from typing import Callable

import pytest

from bilibili_ranker.models import VideoRankingRecord


@pytest.fixture
def make_record() -> Callable[[str, str, int], VideoRankingRecord]:
    def factory(bvid: str, title: str, rank: int = 1) -> VideoRankingRecord:
        return VideoRankingRecord.from_api_item(
            {
                "bvid": bvid,
                "title": title,
                "owner": {"name": "测试UP"},
                "stat": {},
            },
            rank=rank,
            fetched_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

    return factory
