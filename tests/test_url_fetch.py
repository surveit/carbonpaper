"""The kernel GET, against a local server that answers from a queue of canned replies."""
from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.core.errors import SourceUnavailable
from app.core.url_fetch import MAX_RATE_LIMIT_WAITS, MAX_RETRY_AFTER_S, open_url, read_url


class _Server:
    """Answers each request with the next queued reply, and records who asked."""

    def __init__(self) -> None:
        self.replies: list[tuple[int, dict[str, str], bytes]] = []
        self.user_agents: list[str] = []
        self.base = ""

    def queue(self, status: int, *, headers: dict[str, str] | None = None,
              body: bytes = b"") -> None:
        self.replies.append((status, headers or {}, body))

    def url(self, path: str) -> str:
        return f"{self.base}{path}"


@pytest.fixture
def server() -> Iterator[_Server]:
    served = _Server()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            served.user_agents.append(self.headers.get("User-Agent", ""))
            status, headers, body = served.replies.pop(0)
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    http_server = HTTPServer(("127.0.0.1", 0), Handler)
    served.base = f"http://127.0.0.1:{http_server.server_port}"
    thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    thread.start()
    yield served
    http_server.shutdown()
    http_server.server_close()


@pytest.fixture
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    seconds: list[float] = []
    monkeypatch.setattr("app.core.url_fetch.time.sleep", seconds.append)
    return seconds


def test_a_429_with_retry_after_is_waited_out_then_read(server: _Server, slept: list[float]) -> None:
    server.queue(429, headers={"Retry-After": "3"})
    server.queue(200, body=b"ok")
    assert read_url(server.url("/f"), user_agent="t", timeout_s=5) == b"ok"
    assert slept == [3]


def test_open_url_waits_out_a_429_too(server: _Server, slept: list[float]) -> None:
    server.queue(429, headers={"Retry-After": "2"})
    server.queue(200, body=b"ok")
    with open_url(server.url("/f"), user_agent="t", timeout_s=5) as response:
        assert response.read() == b"ok"
    assert slept == [2]


def test_a_429_with_no_retry_after_refuses(server: _Server, slept: list[float]) -> None:
    server.queue(429, headers={})
    with pytest.raises(SourceUnavailable, match="Retry-After"):
        read_url(server.url("/f"), user_agent="t", timeout_s=5)
    assert slept == []


def test_a_retry_after_that_is_not_seconds_refuses(server: _Server, slept: list[float]) -> None:
    server.queue(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
    with pytest.raises(SourceUnavailable, match="Retry-After"):
        read_url(server.url("/f"), user_agent="t", timeout_s=5)
    assert slept == []


def test_a_retry_after_over_the_cap_refuses(server: _Server, slept: list[float]) -> None:
    server.queue(429, headers={"Retry-After": str(MAX_RETRY_AFTER_S + 1)})
    with pytest.raises(SourceUnavailable):
        read_url(server.url("/f"), user_agent="t", timeout_s=5)
    assert slept == []


def test_rate_limited_past_the_last_wait_refuses(server: _Server, slept: list[float]) -> None:
    for _ in range(MAX_RATE_LIMIT_WAITS + 1):
        server.queue(429, headers={"Retry-After": "1"})
    with pytest.raises(SourceUnavailable, match="rate-limited"):
        read_url(server.url("/f"), user_agent="t", timeout_s=5)
    assert len(slept) == MAX_RATE_LIMIT_WAITS


def test_a_404_refuses_without_waiting(server: _Server, slept: list[float]) -> None:
    server.queue(404)
    with pytest.raises(SourceUnavailable, match="404"):
        read_url(server.url("/f"), user_agent="t", timeout_s=5)
    assert slept == []


def test_a_server_that_is_not_listening_refuses(slept: list[float]) -> None:
    with socket.socket() as closed:
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
    with pytest.raises(SourceUnavailable, match="failed"):
        read_url(f"http://127.0.0.1:{port}/f", user_agent="t", timeout_s=5)
    assert slept == []


def test_the_user_agent_is_sent(server: _Server, slept: list[float]) -> None:
    server.queue(200, body=b"ok")
    read_url(server.url("/f"), user_agent="carbonpaper-test", timeout_s=5)
    assert server.user_agents == ["carbonpaper-test"]
