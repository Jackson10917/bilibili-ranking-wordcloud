"""B 站全站排行榜的 HTTP 客户端。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


RANKING_PAGE_URL = "https://www.bilibili.com/v/popular/rank/all"
RANKING_API_URL = "https://api.bilibili.com/x/web-interface/ranking/v2"
SPI_API_URL = "https://api.bilibili.com/x/frontend/finger/spi"

_RISK_CONTROL_CODE = -352
_RISK_CONTROL_ATTEMPTS = 2


class BilibiliAPIError(RuntimeError):
    """HTTP、业务码或响应结构不符合预期。"""


@dataclass(frozen=True, slots=True)
class RankingFetchResult:
    fetched_at: datetime
    items: tuple[Mapping[str, Any], ...]


def build_session() -> requests.Session:
    """带瞬时故障重试的会话；单独成函数便于测试直接断言 Retry 配置。"""

    session = requests.Session()
    adapter = HTTPAdapter(
        max_retries=Retry(
            total=2,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
        )
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _refresh_buvid(
    session: requests.Session,
    *,
    user_agent: str,
    timeout_seconds: float,
) -> None:
    """向会话补充风控所需的 buvid3/buvid4 cookie；请求或响应异常时静默忽略。"""

    try:
        response = session.get(
            SPI_API_URL,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Referer": "https://www.bilibili.com/",
                "User-Agent": user_agent,
            },
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError):
        return
    if not isinstance(payload, Mapping):
        return
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return
    b_3 = data.get("b_3")
    b_4 = data.get("b_4")
    # 必须查类型：非字符串会让 cookies.set() 抛 AttributeError 逸出 CLI 的错误处理。
    if not isinstance(b_3, str) or not isinstance(b_4, str) or not b_3 or not b_4:
        return
    session.cookies.set("buvid3", b_3, domain=".bilibili.com")
    session.cookies.set("buvid4", b_4, domain=".bilibili.com")


def fetch_all_ranking(
    *,
    timeout_seconds: float = 15.0,
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
) -> RankingFetchResult:
    """请求当前全站榜，接口和参数保持为 `rid=0&type=all`。

    被风控拦截（code=-352）时刷新 buvid cookie 后重试。
    """

    fetched_at = datetime.now(timezone.utc)
    session = build_session()
    try:
        for attempt in range(_RISK_CONTROL_ATTEMPTS):
            response = session.get(
                RANKING_API_URL,
                params={"rid": 0, "type": "all"},
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Referer": RANKING_PAGE_URL,
                    "User-Agent": user_agent,
                },
                timeout=timeout_seconds,
            )
            # 风控拦截常伴随 HTTP 412，业务码在响应体里，所以先读体再判 HTTP 状态。
            try:
                payload = response.json()
            except ValueError:
                payload = None
            code = payload.get("code") if isinstance(payload, Mapping) else None

            if code == _RISK_CONTROL_CODE:
                # 最后一轮再刷新也没有消费者，其余轮次刷新 buvid 后重试。
                if attempt < _RISK_CONTROL_ATTEMPTS - 1:
                    _refresh_buvid(
                        session,
                        user_agent=user_agent,
                        timeout_seconds=timeout_seconds,
                    )
                continue

            response.raise_for_status()
            if payload is None:
                raise BilibiliAPIError("排行榜响应不是有效 JSON")
            if not isinstance(payload, Mapping):
                raise BilibiliAPIError("排行榜响应根节点不是 JSON 对象")
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
            return RankingFetchResult(fetched_at=fetched_at, items=tuple(items))

        raise BilibiliAPIError(
            f"B站风控拦截（code={_RISK_CONTROL_CODE}），请稍后重试"
        )
    except requests.RequestException as exc:
        raise BilibiliAPIError(f"排行榜请求失败：{exc}") from exc
    finally:
        session.close()
