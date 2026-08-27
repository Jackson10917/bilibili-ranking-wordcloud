"""B 站全站排行榜的 HTTP 客户端。"""

from __future__ import annotations

import math
import os
import time
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

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)


def _default_user_agent() -> str:
    """运行时读取 BILIBILI_UA：绑成函数默认值的话，进程内改环境变量不生效也没法测。"""

    return os.environ.get("BILIBILI_UA") or _DEFAULT_UA


# socket.settimeout 对超大值会抛 OverflowError（1e9 起就 "doesn't fit into C timeval"），
# 不是调用方能理解的错误。一天足够覆盖任何合理超时，超过就是参数写错了。
MAX_TIMEOUT_SECONDS = 86_400.0

_RISK_CONTROL_CODE = -352
_RISK_CONTROL_STATUS = 412
_RISK_CONTROL_ATTEMPTS = 3
_RISK_CONTROL_BACKOFF_SECONDS = 1.0


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
    user_agent: str | None = None,
) -> RankingFetchResult:
    """请求当前全站榜，接口和参数保持为 `rid=0&type=all`。

    被风控拦截（业务码 -352，或只有 HTTP 412 没有 JSON 体）时刷新 buvid cookie 后重试。
    """

    # 在公共 API 入口统一校验：CLI 之外的调用方直接传 -1/inf/1e10 时，底层会抛
    # ValueError/OverflowError，与本模块其余错误（BilibiliAPIError）不是一类，调用方没法统一处理。
    if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise BilibiliAPIError(
            f"timeout_seconds 必须是 0 到 {MAX_TIMEOUT_SECONDS:.0f} 之间的有限数：{timeout_seconds!r}"
        )

    user_agent = user_agent or _default_user_agent()
    fetched_at = datetime.now(timezone.utc)
    session = build_session()
    try:
        last_response: requests.Response | None = None
        for attempt in range(_RISK_CONTROL_ATTEMPTS):
            # 连接在 body 读取阶段被截断（IncompleteRead 的 requests 包装
            # ChunkedEncodingError）与截断的垃圾 body 同属瞬时 CDN 故障。stream=False
            # 时 body 在 get() 内部就已读完、异常从这里抛出，而 urllib3 Retry 只管到
            # 响应到达为止，不覆盖这个阶段，所以在原轮次上限内重试、不叠乘。最后一轮
            # 仍失败才交给外层包装成 BilibiliAPIError。
            try:
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
            except requests.exceptions.ChunkedEncodingError:
                if attempt < _RISK_CONTROL_ATTEMPTS - 1:
                    continue
                raise
            # 风控拦截常伴随 HTTP 412，业务码在响应体里，所以先读体再判 HTTP 状态。
            try:
                payload = response.json()
            except ValueError:
                payload = None
            code = payload.get("code") if isinstance(payload, Mapping) else None

            # 风控也可能只回 412 + HTML（无 JSON 业务码），此时同样要刷 buvid 重试。
            # 但 412 带了业务码就按业务码判：代理/CDN 的 412 不该把真实错误码盖成"风控"。
            # （412 + code=0 会被 raise_for_status 作为 HTTP 错误上报，至少保留了真实状态码。）
            last_response = response

            # 200 但拿不到有效 JSON：CDN 偶发返回截断/HTML 错误页或中途断连，
            # 重试一次通常就好。只在 200 上重试，412 仍交给下面的风控分支处理。
            if (
                payload is None
                and response.status_code == 200
                and attempt < _RISK_CONTROL_ATTEMPTS - 1
            ):
                continue

            if code == _RISK_CONTROL_CODE or (
                response.status_code == _RISK_CONTROL_STATUS and code is None
            ):
                # 最后一轮再刷新也没有消费者，其余轮次刷新 buvid 后重试。
                if attempt < _RISK_CONTROL_ATTEMPTS - 1:
                    _refresh_buvid(
                        session,
                        user_agent=user_agent,
                        timeout_seconds=timeout_seconds,
                    )
                    # 立刻二连会加重风控。首轮不等（冷启动必吃一次 -352），之后递增退避。
                    if attempt > 0:
                        time.sleep(_RISK_CONTROL_BACKOFF_SECONDS * attempt)
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

        # 附上最后一轮的诊断信息：非风控 412（代理/CDN 错误页）至少留下可排查线索。
        _diag = ""
        if last_response is not None:
            _body = last_response.text[:200].replace("\n", " ")
            _diag = f"（最后响应：HTTP {last_response.status_code}，body={_body!r}）"
        raise BilibiliAPIError(f"B站风控拦截（code=-352 或 HTTP 412），请稍后重试{_diag}")
    except requests.RequestException as exc:
        raise BilibiliAPIError(f"排行榜请求失败：{exc}") from exc
    finally:
        session.close()
