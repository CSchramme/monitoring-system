import asyncio
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.checks import (
    check_dns,
    check_tcp,
    evaluate_push,
    is_valid_host,
    parse_host_port,
    parse_push_status,
    run_check,
    status_matches,
)


def test_status_matches():
    assert status_matches(200, "200-399")
    assert status_matches(302, "200-399")
    assert not status_matches(404, "200-399")
    assert status_matches(204, "200,204")
    assert status_matches(418, "4xx")
    assert not status_matches(500, "2xx,301")
    with pytest.raises(ValueError):
        status_matches(200, "abc")


def test_parse_host_port():
    assert parse_host_port("example.com:443") == ("example.com", 443)
    assert parse_host_port("[::1]:22") == ("::1", 22)
    assert parse_host_port("10.0.0.1:5432") == ("10.0.0.1", 5432)
    for bad in ("example.com", "example.com:0", "example.com:99999", "-oProxy:22", ":80"):
        with pytest.raises(ValueError):
            parse_host_port(bad)


def test_is_valid_host():
    assert is_valid_host("example.com")
    assert is_valid_host("192.168.1.1")
    assert is_valid_host("::1")
    assert not is_valid_host("-c 100 example.com")
    assert not is_valid_host("exa mple.com")
    assert not is_valid_host("")


def test_parse_push_status():
    assert parse_push_status(None) is True
    assert parse_push_status("OK") is True
    assert parse_push_status("down") is False
    assert parse_push_status("error") is False
    assert parse_push_status("banana") is None


def test_evaluate_push_thresholds():
    rules = [{"metric": "cpu", "op": ">", "value": 90}, {"metric": "disk", "op": ">=", "value": 95}]
    ok = evaluate_push(True, "", {"cpu": 50, "disk": 80}, rules)
    assert ok.ok and ok.message == "Signal empfangen"

    bad = evaluate_push(True, "web01", {"cpu": 97.5, "disk": 95}, rules)
    assert not bad.ok
    assert "cpu=97.5 > 90" in bad.message and "disk=95 >= 95" in bad.message and "web01" in bad.message

    # missing metric is ignored, reported failure wins
    failed = evaluate_push(False, "backup failed", {}, rules)
    assert not failed.ok and failed.message == "backup failed"


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        code = 500 if self.path == "/fail" else 200
        body = b"hello monitoring world"
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def http_server():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def _http_monitor(url, **cfg):
    config = {"method": "GET", "expected_status": "200-399", "verify_tls": True, "cert_expiry_days": 0}
    config.update(cfg)
    return {"type": "http", "target": url, "timeout": 5, "config": config}


def test_http_check(http_server):
    result = asyncio.run(run_check(_http_monitor(http_server + "/")))
    assert result.ok and result.message == "HTTP 200" and result.latency is not None

    result = asyncio.run(run_check(_http_monitor(http_server + "/fail")))
    assert not result.ok and "HTTP 500" in result.message

    result = asyncio.run(run_check(_http_monitor(http_server + "/", keyword="monitoring")))
    assert result.ok
    result = asyncio.run(run_check(_http_monitor(http_server + "/", keyword="missing")))
    assert not result.ok
    result = asyncio.run(run_check(_http_monitor(http_server + "/", keyword="hello", keyword_invert=True)))
    assert not result.ok


def test_http_check_connection_refused():
    result = asyncio.run(run_check(_http_monitor("http://127.0.0.1:1/")))
    assert not result.ok and result.message


def test_tcp_check():
    async def scenario():
        server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            up = await check_tcp({"target": f"127.0.0.1:{port}", "timeout": 3, "config": {}})
        down = await check_tcp({"target": f"127.0.0.1:{port}", "timeout": 3, "config": {}})
        return up, down

    up, down = asyncio.run(scenario())
    assert up.ok and "offen" in up.message
    assert not down.ok


def test_dns_check():
    result = asyncio.run(check_dns({"target": "localhost", "timeout": 5, "config": {"expected": ""}}))
    assert result.ok
    result = asyncio.run(check_dns({"target": "localhost", "timeout": 5, "config": {"expected": "10.9.9.9"}}))
    assert not result.ok
