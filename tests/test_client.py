from typing import Any

import pytest

from bilibili_ranker.client import BilibiliAPIError, BilibiliRankingClient


class FakeResponse:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._payload


class FakeSession:
    def __init__(self, payload: Any) -> None:
        self.response = FakeResponse(payload)
        self.request: dict[str, Any] | None = None

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.request = {"url": url, **kwargs}
        return self.response


def test_client_validates_and_returns_ranking_items() -> None:
    session = FakeSession({"code": 0, "data": {"list": [{"bvid": "BV1TEST"}]}})
    client = BilibiliRankingClient(session=session, timeout_seconds=3)

    result = client.fetch_all_ranking()

    assert len(result.items) == 1
    assert session.request is not None
    assert session.request["params"] == {"rid": 0, "type": "all"}
    assert session.request["timeout"] == 3


def test_client_rejects_bilibili_business_error() -> None:
    session = FakeSession({"code": -400, "message": "请求错误"})
    client = BilibiliRankingClient(session=session)

    with pytest.raises(BilibiliAPIError, match="code=-400"):
        client.fetch_all_ranking()
