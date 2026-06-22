"""将 B 站排行榜响应转换为稳定、扁平的数据模型。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    parsed = int(value)
    return parsed if parsed >= 0 else None


def _timestamp_to_datetime_text(value: int | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class RecordIssue:
    """某条记录被拒绝或去重时留下的可审计信息。"""

    stage: str
    rank: int
    bvid: str
    title: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class VideoRankingRecord:
    """适合写入 CSV 和后续分析的排行榜记录。"""

    rank: int
    aid: int | None
    bvid: str
    cid: int | None
    video_url: str
    short_link_url: str
    title: str
    description: str
    dynamic_text: str
    cover_url: str
    cover_4_3_url: str
    first_frame_url: str
    category_id: int | None
    category_name: str
    category_v2_id: int | None
    category_v2_name: str
    parent_category_v2_id: int | None
    parent_category_v2_name: str
    copyright_type: int | None
    publication_location: str
    uploader_id: int | None
    uploader_name: str
    uploader_face_url: str
    published_timestamp: int | None
    published_at: str | None
    created_timestamp: int | None
    created_at: str | None
    duration_seconds: int | None
    videos_count: int | None
    width: int | None
    height: int | None
    rotate: int | None
    view_count: int | None
    danmaku_count: int | None
    reply_count: int | None
    favorite_count: int | None
    coin_count: int | None
    share_count: int | None
    like_count: int | None
    dislike_count: int | None
    current_rank: int | None
    historical_rank: int | None
    ranking_score: int | None
    vt_count: int | None
    vv_count: int | None
    favorite_group_count: int | None
    like_group_count: int | None
    no_reprint: int | None
    is_cooperation: int | None
    enable_vt: int | None
    fetched_at: str

    @classmethod
    def from_api_item(
        cls,
        item: Mapping[str, Any],
        *,
        rank: int,
        fetched_at: datetime,
    ) -> "VideoRankingRecord":
        bvid = _text(item.get("bvid"))
        title = _text(item.get("title"))
        if not bvid:
            raise ValueError("缺少 bvid")
        if not title:
            raise ValueError("缺少标题")

        owner = _mapping(item.get("owner"))
        stat = _mapping(item.get("stat"))
        rights = _mapping(item.get("rights"))
        dimension = _mapping(item.get("dimension"))
        published_timestamp = _optional_int(item.get("pubdate"))
        created_timestamp = _optional_int(item.get("ctime"))

        return cls(
            rank=rank,
            aid=_optional_int(item.get("aid")),
            bvid=bvid,
            cid=_optional_int(item.get("cid")),
            video_url=f"https://www.bilibili.com/video/{bvid}",
            short_link_url=_text(item.get("short_link_v2")),
            title=title,
            description=_text(item.get("desc")),
            dynamic_text=_text(item.get("dynamic")),
            cover_url=_text(item.get("pic")),
            cover_4_3_url=_text(item.get("cover43")),
            first_frame_url=_text(item.get("first_frame")),
            category_id=_optional_int(item.get("tid")),
            category_name=_text(item.get("tname")),
            category_v2_id=_optional_int(item.get("tidv2")),
            category_v2_name=_text(item.get("tnamev2")),
            parent_category_v2_id=_optional_int(item.get("pid_v2")),
            parent_category_v2_name=_text(item.get("pid_name_v2")),
            copyright_type=_optional_int(item.get("copyright")),
            publication_location=_text(item.get("pub_location")),
            uploader_id=_optional_int(owner.get("mid")),
            uploader_name=_text(owner.get("name")),
            uploader_face_url=_text(owner.get("face")),
            published_timestamp=published_timestamp,
            published_at=_timestamp_to_datetime_text(published_timestamp),
            created_timestamp=created_timestamp,
            created_at=_timestamp_to_datetime_text(created_timestamp),
            duration_seconds=_optional_int(item.get("duration")),
            videos_count=_optional_int(item.get("videos")),
            width=_optional_int(dimension.get("width")),
            height=_optional_int(dimension.get("height")),
            rotate=_optional_int(dimension.get("rotate")),
            view_count=_optional_int(stat.get("view")),
            danmaku_count=_optional_int(stat.get("danmaku")),
            reply_count=_optional_int(stat.get("reply")),
            favorite_count=_optional_int(stat.get("favorite")),
            coin_count=_optional_int(stat.get("coin")),
            share_count=_optional_int(stat.get("share")),
            like_count=_optional_int(stat.get("like")),
            dislike_count=_optional_int(stat.get("dislike")),
            current_rank=_optional_int(stat.get("now_rank")),
            historical_rank=_optional_int(stat.get("his_rank")),
            ranking_score=_optional_int(item.get("score")),
            vt_count=_optional_int(stat.get("vt")),
            vv_count=_optional_int(stat.get("vv")),
            favorite_group_count=_optional_int(stat.get("fav_g")),
            like_group_count=_optional_int(stat.get("like_g")),
            no_reprint=_optional_int(rights.get("no_reprint")),
            is_cooperation=_optional_int(rights.get("is_cooperation")),
            enable_vt=_optional_int(item.get("enable_vt")),
            fetched_at=_as_utc(fetched_at).isoformat(),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RankingParseResult:
    records: tuple[VideoRankingRecord, ...]
    issues: tuple[RecordIssue, ...]


def parse_ranking_records(
    items: Iterable[Mapping[str, Any]],
    *,
    fetched_at: datetime | None = None,
) -> list[VideoRankingRecord]:
    """严格解析；任意记录不合法时抛出异常。"""

    captured_at = _as_utc(fetched_at or datetime.now(timezone.utc))
    return [
        VideoRankingRecord.from_api_item(item, rank=rank, fetched_at=captured_at)
        for rank, item in enumerate(items, start=1)
    ]


def parse_ranking_records_with_issues(
    items: Iterable[Any],
    *,
    fetched_at: datetime | None = None,
) -> RankingParseResult:
    """容错解析，将坏记录及原因单独返回，不中断整次抓取。"""

    captured_at = _as_utc(fetched_at or datetime.now(timezone.utc))
    records: list[VideoRankingRecord] = []
    issues: list[RecordIssue] = []

    for rank, item in enumerate(items, start=1):
        mapping = _mapping(item)
        try:
            if not mapping:
                raise ValueError("记录不是 JSON 对象")
            records.append(
                VideoRankingRecord.from_api_item(
                    mapping,
                    rank=rank,
                    fetched_at=captured_at,
                )
            )
        except (TypeError, ValueError) as exc:
            issues.append(
                RecordIssue(
                    stage="parse",
                    rank=rank,
                    bvid=_text(mapping.get("bvid")),
                    title=_text(mapping.get("title")),
                    reason=str(exc),
                )
            )

    return RankingParseResult(records=tuple(records), issues=tuple(issues))
