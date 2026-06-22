from datetime import datetime, timezone

from bilibili_ranker.models import parse_ranking_records_with_issues


def test_parse_ranking_record_maps_nested_fields() -> None:
    item = {
        "aid": 123,
        "bvid": "BV1TEST",
        "title": "测试视频",
        "pubdate": 1_700_000_000,
        "owner": {"mid": 42, "name": "测试UP"},
        "stat": {"view": 1234, "like": 56},
        "dimension": {"width": 1920, "height": 1080},
    }

    result = parse_ranking_records_with_issues(
        [item],
        fetched_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    assert result.issues == ()
    record = result.records[0]
    assert record.rank == 1
    assert record.video_url == "https://www.bilibili.com/video/BV1TEST"
    assert record.uploader_name == "测试UP"
    assert record.published_at == "2023-11-15 06:13:20"
    assert record.view_count == 1234
    assert record.like_count == 56
    assert record.width == 1920


def test_parse_reports_invalid_records_without_stopping() -> None:
    result = parse_ranking_records_with_issues(
        [{"title": "缺少BV号"}, {"bvid": "BV1EMPTY", "title": "   "}]
    )

    assert result.records == ()
    assert [issue.reason for issue in result.issues] == ["缺少 bvid", "缺少标题"]
