"""B 站全站排行榜的 HTTP 客户端。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


RANKING_PAGE_URL = "https://www.bilibili.com/v/popular/rank/all"
RANKING_API_URL = "https://api.bilibili.com/x/web-interface/ranking/v2"


class BilibiliAPIError(RuntimeError):
    """HTTP、业务码或响应结构不符合预期。"""


@dataclass(frozen=True, slots=True)
class RankingFetchResult:
    fetched_at: datetime
    source_page_url: str
    api_url: str
    payload: Mapping[str, Any]
    items: tuple[Mapping[str, Any], ...]


class BilibiliRankingClient:
    """只负责请求和响应校验，不负责存储、清洗或绘图。"""

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        timeout_seconds: float = 15.0,
        user_agent: str = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    ) -> None:
        self._owns_session = session is None
        self._session = session or self._build_session()
        self._timeout_seconds = timeout_seconds
        self._headers = {
            "Accept": "application/json, text/plain, */*",
            "Referer": RANKING_PAGE_URL,
            "User-Agent": user_agent,
        }

    @staticmethod
    def _build_session() -> requests.Session:
        retry = Retry(
            total=2,
            connect=2,
            read=2,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            respect_retry_after_header=True,
        )
        session = requests.Session()
        session.mount("https://", HTTPAdapter(max_retries=retry))
        return session

    def fetch_all_ranking(self) -> RankingFetchResult:
        """请求当前全站榜，接口和参数保持为 `rid=0&type=all`。"""

        fetched_at = datetime.now(timezone.utc)
        try:
            response = self._session.get(
                RANKING_API_URL,
                params={"rid": 0, "type": "all"},
                headers=self._headers,
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise BilibiliAPIError(f"排行榜请求失败：{exc}") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise BilibiliAPIError("排行榜响应不是有效 JSON") from exc

        if not isinstance(payload, Mapping):
            raise BilibiliAPIError("排行榜响应根节点不是 JSON 对象")

        code = payload.get("code")
        if code != 0:
            message = payload.get("message") or payload.get("msg") or "未知错误"
            raise BilibiliAPIError(f"B站返回 code={code}：{message}")

        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise BilibiliAPIError("排行榜响应缺少 data 对象")
        items = data.get("list")
        if not isinstance(items, list):
            raise BilibiliAPIError("排行榜响应的 data.list 不是数组")
        if not all(isinstance(item, Mapping) for item in items):
            raise BilibiliAPIError("排行榜响应包含非对象记录")

        return RankingFetchResult(
            fetched_at=fetched_at,
            source_page_url=RANKING_PAGE_URL,
            api_url=RANKING_API_URL,
            payload=payload,
            items=tuple(items),
        )

    def close(self) -> None:
        if self._owns_session:
            self._session.close()

    def __enter__(self) -> "BilibiliRankingClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
