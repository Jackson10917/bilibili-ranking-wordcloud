"""排行榜 HTTP 客户端的回归测试。

由 tests/test_core.py 按源码模块拆分而来；统一由 pytest 收集运行：python -m pytest tests
"""

from __future__ import annotations

import json

import requests


def _fake_json_response(payload: dict, status_code: int = 200) -> requests.Response:
    response = requests.Response()
    response.status_code = status_code
    response._content = json.dumps(payload).encode("utf-8")
    return response


def test_fetch_retries_risk_control() -> None:
    # 第一次返回 -352 风控拦截时刷新 buvid 重试，第二次成功。
    from unittest.mock import patch

    from bilibili_ranker.client import SPI_API_URL, fetch_all_ranking

    # 风控拦截在真实环境伴随 HTTP 412，业务码只在响应体里。
    ranking_responses = [
        ({"code": -352, "message": "-352"}, 412),
        ({"code": 0, "data": {"list": [{"bvid": "BV1aa0000000", "title": "t"}]}}, 200),
    ]
    spi_calls = 0
    retry_cookies: list[dict[str, str]] = []

    def fake_get(self: requests.Session, url: str, **kwargs: object) -> requests.Response:
        nonlocal spi_calls
        if url == SPI_API_URL:
            spi_calls += 1
            return _fake_json_response({"code": 0, "data": {"b_3": "b3", "b_4": "b4"}})
        retry_cookies.append(self.cookies.get_dict())
        payload, status_code = ranking_responses.pop(0)
        return _fake_json_response(payload, status_code)

    with patch.object(requests.Session, "get", fake_get):
        result = fetch_all_ranking()

    assert [item["bvid"] for item in result.items] == ["BV1aa0000000"]
    assert spi_calls == 1  # 无前置刷新，成功后也不再刷
    assert retry_cookies == [
        {},
        {"buvid3": "b3", "buvid4": "b4"},
    ]  # buvid 确实写进 cookie jar 并随重试发送


def test_fetch_raises_when_risk_control_persists() -> None:
    # 每轮都返回 -352 时抛出 BilibiliAPIError，且最后一轮不再空刷 buvid。
    from unittest.mock import patch

    from bilibili_ranker import client as client_module
    from bilibili_ranker.client import SPI_API_URL, BilibiliAPIError, fetch_all_ranking

    spi_calls = 0
    sleeps: list[float] = []

    def fake_get(self: requests.Session, url: str, **kwargs: object) -> requests.Response:
        nonlocal spi_calls
        if url == SPI_API_URL:
            spi_calls += 1
            return _fake_json_response({"code": 0, "data": {"b_3": "b3", "b_4": "b4"}})
        return _fake_json_response({"code": -352, "message": "-352"}, 412)

    with patch.object(requests.Session, "get", fake_get):
        with patch.object(client_module.time, "sleep", sleeps.append):
            try:
                fetch_all_ranking()
            except BilibiliAPIError as exc:
                assert "风控" in str(exc)
            else:
                raise AssertionError("持续 -352 后未抛出 BilibiliAPIError")
    # 最后一轮不再空刷 buvid：刷新次数 = 总轮次 - 1。
    assert spi_calls == client_module._RISK_CONTROL_ATTEMPTS - 1
    # 首轮不等待（冷启动必吃一次 -352），之后递增退避，不能立刻连打风控接口。
    assert sleeps == [1.0]


def test_fetch_survives_malformed_buvid() -> None:
    # SPI 返回非字符串 cookie 值时不能让 cookies.set() 的 AttributeError 逸出，
    # 必须仍收敛成 BilibiliAPIError，由 CLI 统一转成退出码 1。
    from unittest.mock import patch

    from bilibili_ranker.client import SPI_API_URL, BilibiliAPIError, fetch_all_ranking

    def fake_get(self: requests.Session, url: str, **kwargs: object) -> requests.Response:
        if url == SPI_API_URL:
            return _fake_json_response({"code": 0, "data": {"b_3": ["bad"], "b_4": "ok"}})
        return _fake_json_response({"code": -352, "message": "-352"}, 412)

    with patch.object(requests.Session, "get", fake_get):
        try:
            fetch_all_ranking()
        except BilibiliAPIError as exc:
            assert "风控" in str(exc)
        else:
            raise AssertionError("畸形 buvid 未收敛成 BilibiliAPIError")


def test_fetch_retries_on_412_without_json_body() -> None:
    # 真实风控常返回 412 + HTML（无 JSON 业务码），此时也必须刷 buvid 重试。
    from unittest.mock import patch

    from bilibili_ranker.client import SPI_API_URL, fetch_all_ranking

    spi_calls = 0
    ranking_calls = 0

    def fake_get(self: requests.Session, url: str, **kwargs: object) -> requests.Response:
        nonlocal spi_calls, ranking_calls
        if url == SPI_API_URL:
            spi_calls += 1
            return _fake_json_response({"code": 0, "data": {"b_3": "b3", "b_4": "b4"}})
        ranking_calls += 1
        if ranking_calls == 1:
            response = requests.Response()
            response.status_code = 412
            response._content = b"<html>risk control</html>"
            return response
        return _fake_json_response(
            {"code": 0, "data": {"list": [{"bvid": "BV1aa0000000", "title": "t"}]}}
        )

    with patch.object(requests.Session, "get", fake_get):
        result = fetch_all_ranking()

    assert [item["bvid"] for item in result.items] == ["BV1aa0000000"]
    assert spi_calls == 1
    assert ranking_calls == 2


def test_fetch_reports_http_error() -> None:
    # 非风控的 HTTP 错误仍要抛错，不能因为先读响应体而被吞掉。
    from unittest.mock import patch

    from bilibili_ranker.client import BilibiliAPIError, fetch_all_ranking

    def fake_get(self: requests.Session, url: str, **kwargs: object) -> requests.Response:
        return _fake_json_response({"code": 0, "data": {"list": []}}, 503)

    with patch.object(requests.Session, "get", fake_get):
        try:
            fetch_all_ranking()
        except BilibiliAPIError as exc:
            assert "请求失败" in str(exc)
        else:
            raise AssertionError("HTTP 503 未抛出 BilibiliAPIError")


def test_fetch_over_real_http_retries_after_412() -> None:
    """走完整 requests 收发链路，锁死 412 的响应体能回到业务代码并触发第二轮。

    注意：buvid cookie 的 domain 是 `.bilibili.com`，不会发给 127.0.0.1，
    所以这里不断言请求头里的 cookie，domain 正确性由
    test_buvid_cookie_reaches_request_header 负责。
    """

    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from bilibili_ranker import client as client_module

    ranking_calls: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            if self.path.startswith("/spi"):
                body, status = {"code": 0, "data": {"b_3": "b3", "b_4": "b4"}}, 200
            else:
                ranking_calls.append(self.path)
                if len(ranking_calls) == 1:
                    body, status = {"code": -352, "message": "risk"}, 412
                else:
                    body, status = (
                        {"code": 0, "data": {"list": [{"bvid": "BV1aa0000000", "title": "标题"}]}},
                        200,
                    )
            payload = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    host, port = server.server_address[0], server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    original_ranking = client_module.RANKING_API_URL
    original_spi = client_module.SPI_API_URL
    try:
        client_module.RANKING_API_URL = f"http://{host}:{port}/ranking"
        client_module.SPI_API_URL = f"http://{host}:{port}/spi"
        result = client_module.fetch_all_ranking(timeout_seconds=5.0)
    finally:
        client_module.RANKING_API_URL = original_ranking
        client_module.SPI_API_URL = original_spi
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert [item["bvid"] for item in result.items] == ["BV1aa0000000"]
    # 412 的响应体确实交回业务代码，-352 分支可达并触发了第二轮请求。
    assert len(ranking_calls) == 2


def test_buvid_cookie_reaches_request_header() -> None:
    """cookie jar 有值不等于请求头带上了：domain 写错时 get_dict() 照样能查到。

    这里走 requests 真实的 prepare_request，把 domain 正确性锁死。
    """

    import re
    from unittest.mock import patch

    from bilibili_ranker.client import (
        RANKING_API_URL,
        SPI_API_URL,
        _refresh_buvid,
        build_session,
    )

    def fake_get(self: requests.Session, url: str, **kwargs: object) -> requests.Response:
        assert url == SPI_API_URL
        return _fake_json_response({"code": 0, "data": {"b_3": "b3", "b_4": "b4"}})

    session = build_session()
    try:
        with patch.object(requests.Session, "get", fake_get):
            _refresh_buvid(session, user_agent="ua", timeout_seconds=5.0)
        prepared = session.prepare_request(requests.Request("GET", RANKING_API_URL))
    finally:
        session.close()

    header = prepared.headers.get("Cookie") or ""
    assert re.search(r"\bbuvid3=b3\b", header), header
    assert re.search(r"\bbuvid4=b4\b", header), header


def test_session_keeps_transient_retry_config() -> None:
    # 删掉 HTTPAdapter/Retry 配置时，纯 mock 测试不会有任何反应，这里直接断言。
    from urllib3.util.retry import Retry

    from bilibili_ranker.client import build_session

    session = build_session()
    try:
        retries = session.get_adapter("https://api.bilibili.com/").max_retries
    finally:
        session.close()
    assert isinstance(retries, Retry)
    assert retries.total == 2
    assert set(retries.status_forcelist or ()) == {429, 500, 502, 503, 504}


def test_retries_on_200_with_invalid_json() -> None:
    # CDN 偶发返回截断 body 或 HTML 错误页（HTTP 仍是 200），一次就报死属于把瞬时故障
    # 当永久失败。urllib3 Retry 只覆盖异常和 429/5xx，这一类不在范围内。
    import http.server
    import json as _json
    import threading

    import bilibili_ranker.client as client_module

    good = _json.dumps({"code": 0, "data": {"list": [{"bvid": "BV1aa0000000", "title": "测试"}]}})
    counter = {"n": 0}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 接口
            counter["n"] += 1
            body = b"<html>502</html>" if counter["n"] == 1 else good.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    original = client_module.RANKING_API_URL
    client_module.RANKING_API_URL = f"http://127.0.0.1:{server.server_port}/x"
    try:
        result = client_module.fetch_all_ranking(timeout_seconds=5)
        assert counter["n"] == 2, counter
        assert result.items[0]["bvid"] == "BV1aa0000000"
    finally:
        client_module.RANKING_API_URL = original
        server.shutdown()
        server.server_close()


def test_fetch_retries_on_truncated_body() -> None:
    # CDN 在 Content-Length 读满前断连（IncompleteRead → RequestException）与截断的
    # 垃圾 body 是同一类故障，必须同样走 200 重试路径，不能一次就报死。
    import http.server
    import json as _json
    import threading

    import bilibili_ranker.client as client_module

    good = _json.dumps({"code": 0, "data": {"list": [{"bvid": "BV1aa0000000", "title": "测试"}]}})
    counter = {"n": 0}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 接口
            counter["n"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            if counter["n"] == 1:
                # 谎报 Content-Length 后只发一小段就断开，逼客户端读出 IncompleteRead。
                self.send_header("Content-Length", "200")
                self.end_headers()
                self.wfile.write(b'{"code": 0, "data": {"list": [{"bvid": "BV1aa')
                self.close_connection = True
                return
            body = good.encode()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    original = client_module.RANKING_API_URL
    client_module.RANKING_API_URL = f"http://127.0.0.1:{server.server_port}/x"
    try:
        result = client_module.fetch_all_ranking(timeout_seconds=5)
        assert counter["n"] == 2, counter
        assert result.items[0]["bvid"] == "BV1aa0000000"
    finally:
        client_module.RANKING_API_URL = original
        server.shutdown()
        server.server_close()


def test_412_with_business_code_not_treated_as_risk_control() -> None:
    # 代理/CDN 也会回 412。带了业务码就按业务码判，否则真实错误被盖成"风控"，
    # 极端情况下 412 + code=0 的有效响应会被整轮丢弃。
    from unittest.mock import patch

    from bilibili_ranker.client import SPI_API_URL, BilibiliAPIError, fetch_all_ranking

    def fake_get(self: requests.Session, url: str, **kwargs: object) -> requests.Response:
        if url == SPI_API_URL:
            raise AssertionError("非风控的 412 不应触发 buvid 刷新")
        return _fake_json_response({"code": -404, "message": "啥都木有"}, 412)

    with patch.object(requests.Session, "get", fake_get):
        try:
            fetch_all_ranking()
        except BilibiliAPIError as exc:
            assert "412" in str(exc)  # 按 HTTP 错误上报，不再被误标成风控
        else:
            raise AssertionError("412 + code=-404 未抛出 BilibiliAPIError")


def test_user_agent_read_at_call_time() -> None:
    # _UA 绑成函数默认值时，进程内改 BILIBILI_UA 不生效也无法测试覆盖。
    import os
    from unittest.mock import patch

    from bilibili_ranker.client import fetch_all_ranking

    seen: list[str] = []

    def fake_get(self: requests.Session, url: str, **kwargs: object) -> requests.Response:
        headers = kwargs.get("headers") or {}
        seen.append(headers["User-Agent"])
        return _fake_json_response({"code": 0, "data": {"list": []}})

    old = os.environ.get("BILIBILI_UA")
    try:
        os.environ["BILIBILI_UA"] = "custom-ua/1.0"
        with patch.object(requests.Session, "get", fake_get):
            fetch_all_ranking()
        assert seen == ["custom-ua/1.0"]

        del os.environ["BILIBILI_UA"]
        seen.clear()
        with patch.object(requests.Session, "get", fake_get):
            fetch_all_ranking()
        assert seen and "Chrome" in seen[0]  # 回落到内置默认 UA
    finally:
        if old is None:
            os.environ.pop("BILIBILI_UA", None)
        else:
            os.environ["BILIBILI_UA"] = old


def test_client_rejects_malformed_api_shapes() -> None:
    # 响应校验阶梯逐级都有专属错误信息：接口被代理劫持或字段改名时，
    # 每一级都要给出可排查的 BilibiliAPIError，而不是 KeyError 逸出。
    from unittest.mock import patch

    from bilibili_ranker.client import BilibiliAPIError, fetch_all_ranking

    malformed_payloads = [
        None,  # 200 + 垃圾 body，重试耗尽后仍要报「不是有效 JSON」
        ["不是", "对象"],  # 根节点不是 JSON 对象
        {"code": -404, "message": "啥都木有"},  # 业务码非 0
        {"code": 0},  # 缺 data
        {"code": 0, "data": {"list": "不是数组"}},
        {"code": 0, "data": {"list": ["不是对象"]}},
    ]
    for payload in malformed_payloads:

        def fake_get(
            self: requests.Session,
            url: str,
            *,
            _payload: object = payload,
            **kwargs: object,
        ) -> requests.Response:
            return _fake_json_response(_payload, 200)

        with patch.object(requests.Session, "get", fake_get):
            try:
                fetch_all_ranking()
            except BilibiliAPIError:
                pass
            else:
                raise AssertionError(f"畸形响应未被拒绝：{payload!r}")


def test_refresh_buvid_silent_on_garbage() -> None:
    # SPI 响应的各种畸形（非 200、垃圾 JSON、结构不对）都静默跳过，cookie 一个都不写。
    from unittest.mock import patch

    from bilibili_ranker.client import SPI_API_URL, _refresh_buvid, build_session

    garbage_responses = [
        _fake_json_response({"code": 0, "data": {"b_3": "b3", "b_4": "b4"}}, 500),
        _fake_json_response(["不是", "对象"]),
        _fake_json_response({"code": 0}),
        _fake_json_response({"code": 0, "data": "不是对象"}),
    ]
    # 200 + 坏 JSON 单独构造（helper 只能产合法 JSON），json() 抛 ValueError 后同样静默。
    invalid_json = requests.Response()
    invalid_json.status_code = 200
    invalid_json._content = b"<html>not json</html>"
    garbage_responses.insert(1, invalid_json)
    for response in garbage_responses:

        def fake_get(
            self: requests.Session,
            url: str,
            *,
            _response: requests.Response = response,
            **kwargs: object,
        ) -> requests.Response:
            assert url == SPI_API_URL
            return _response

        session = build_session()
        try:
            with patch.object(requests.Session, "get", fake_get):
                _refresh_buvid(session, user_agent="ua", timeout_seconds=5.0)
            assert session.cookies.get_dict() == {}
        finally:
            session.close()


def test_fetch_reports_persistent_truncation() -> None:
    # 每一轮 body 都被截断时：重试耗尽后按请求失败上报，且请求总数不超轮次上限。
    import http.server
    import threading

    import bilibili_ranker.client as client_module
    from bilibili_ranker.client import BilibiliAPIError

    counter = {"n": 0}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 接口
            counter["n"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "200")
            self.end_headers()
            self.wfile.write(b'{"code": 0, "data": {"list": [{"bvid": "BV1aa')
            self.close_connection = True

        def log_message(self, *_: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    original = client_module.RANKING_API_URL
    client_module.RANKING_API_URL = f"http://127.0.0.1:{server.server_port}/x"
    try:
        try:
            client_module.fetch_all_ranking(timeout_seconds=5)
        except BilibiliAPIError as exc:
            assert "请求失败" in str(exc)
        else:
            raise AssertionError("持续截断未抛出 BilibiliAPIError")
        assert counter["n"] == client_module._RISK_CONTROL_ATTEMPTS, counter
    finally:
        client_module.RANKING_API_URL = original
        server.shutdown()
        server.server_close()


def test_client_rejects_negative_rid() -> None:
    # 非法 rid 与非法 timeout 同类：公共 API 入口先挡下，且绝不发起网络请求。
    from unittest.mock import patch

    import bilibili_ranker.client as client_module
    from bilibili_ranker.client import BilibiliAPIError

    def boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("非法 rid 不应该发起网络请求")

    with patch.object(requests.Session, "get", boom):
        try:
            client_module.fetch_all_ranking(rid=-1)
        except BilibiliAPIError as exc:
            assert "-1" in str(exc)
        else:
            raise AssertionError("负数 rid 未被拒绝")


def test_rid_passes_through_to_query_string() -> None:
    # 分区透传必须真的落进 query：若写死回 rid=0，选了分区也会静默抓成全站榜。
    # 单次成功响应即可，重试语义由其他测试覆盖。
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs

    from bilibili_ranker import client as client_module

    ranking_paths: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            if self.path.startswith("/spi"):
                body, status = {"code": 0, "data": {"b_3": "b3", "b_4": "b4"}}, 200
            else:
                ranking_paths.append(self.path)
                body, status = {"code": 0, "data": {"list": []}}, 200
            payload = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    host, port = server.server_address[0], server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    original_ranking = client_module.RANKING_API_URL
    original_spi = client_module.SPI_API_URL
    try:
        client_module.RANKING_API_URL = f"http://{host}:{port}/ranking"
        client_module.SPI_API_URL = f"http://{host}:{port}/spi"
        result = client_module.fetch_all_ranking(timeout_seconds=5.0, rid=4)
    finally:
        client_module.RANKING_API_URL = original_ranking
        client_module.SPI_API_URL = original_spi
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert result.items == ()
    assert parse_qs(ranking_paths[0].split("?", 1)[1]) == {"rid": ["4"], "type": ["all"]}
