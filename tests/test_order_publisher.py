"""OrderPublisher contract against a mock HTTP server, for every status of spec §2.3 (FR-9, FR-10, AC-7, AC-25)."""

from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import Executor, Future
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from zalo_order_assistant.events import FailureKind, Order, OrderPublished, PublishFailed
from zalo_order_assistant.order_publisher import PublisherConfig, SupabaseOrderPublisher, build_payload

TEXT = "Mai đặt 2 phần, ship nhé"
ORDER = Order("g123/1732330800000_42", "Mai", 2, "2026-11-24", TEXT)
API_KEY = "sk-test-SECRET"


class MockSupabase:
    def __init__(self) -> None:
        self.status = 201
        self.requests: list[tuple[str, dict, dict]] = []
        mock = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                mock.requests.append((self.path, dict(self.headers), body))
                self.send_response(mock.status)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"


class Inline(Executor):
    def submit(self, fn, /, *args, **kwargs):
        f: Future = Future()
        f.set_result(fn(*args, **kwargs))
        return f


class Recorder:
    def __init__(self) -> None:
        self.events: list = []

    def notify(self, e) -> None:
        self.events.append(e)


@pytest.fixture(scope="module")
def _server():
    m = MockSupabase()
    yield m
    m.server.shutdown()


@pytest.fixture
def sb(_server):
    _server.status, _server.requests = 201, []
    return _server


def publisher(url: str, rec: Recorder | None = None) -> SupabaseOrderPublisher:
    cfg = PublisherConfig(url, API_KEY, timeout_seconds=2)
    return SupabaseOrderPublisher(rec or Recorder(), cfg, executor=Inline())


def test_payload_maps_fields_and_never_sends_delivered():
    assert build_payload(ORDER) == {
        "source": "zalo",
        "external_id": "g123/1732330800000_42",
        "customer_name": "Mai",
        "quantity": 2,
        "delivery_date": "2026-11-24",
        "message": TEXT,
    }


def test_message_is_cut_at_2000_chars():
    long = Order("k", "Mai", 1, "2026-11-24", "x" * 2500)
    assert len(build_payload(long)["message"]) == 2000


def test_request_shape(sb):
    publisher(sb.url).publish_now(ORDER)
    ((path, headers, body),) = sb.requests
    assert path == "/functions/v1/orders" and headers["x-api-key"] == API_KEY and body == build_payload(ORDER)


@pytest.mark.parametrize("status", [200, 201])
def test_success(sb, status):
    sb.status = status
    assert publisher(sb.url).publish_now(ORDER) == OrderPublished(ORDER.key)


@pytest.mark.parametrize(
    "status,kind",
    [
        (401, FailureKind.SYSTEMIC),
        (403, FailureKind.SYSTEMIC),
        (404, FailureKind.SYSTEMIC),
        (408, FailureKind.SYSTEMIC),
        (429, FailureKind.SYSTEMIC),
        (500, FailureKind.SYSTEMIC),
        (502, FailureKind.SYSTEMIC),
        (503, FailureKind.SYSTEMIC),
        (504, FailureKind.SYSTEMIC),
        (400, FailureKind.PERMANENT),
        (405, FailureKind.PERMANENT),
        (422, FailureKind.PERMANENT),
        (409, FailureKind.PERMANENT),
    ],
)
def test_failure_kinds(sb, status, kind):
    sb.status = status
    e = publisher(sb.url).publish_now(ORDER)
    assert isinstance(e, PublishFailed) and (e.key, e.kind, e.status) == (ORDER.key, kind, status)


def test_network_error_is_systemic_without_status():
    e = publisher("http://127.0.0.1:9").publish_now(ORDER)  # nothing listens on the discard port
    assert isinstance(e, PublishFailed) and e.kind is FailureKind.SYSTEMIC and e.status is None


def test_publish_reports_through_the_mediator(sb):
    rec = Recorder()
    publisher(sb.url, rec).publish(ORDER)
    assert rec.events == [OrderPublished(ORDER.key)]


def test_crash_in_worker_is_reported_not_lost():
    rec = Recorder()
    p = publisher("http://x", rec)
    p.publish_now = lambda o: 1 / 0  # type: ignore[method-assign]
    p.publish(ORDER)
    assert isinstance(rec.events[0], PublishFailed) and rec.events[0].kind is FailureKind.SYSTEMIC


def test_real_worker_thread_returns_at_once(sb):
    rec = Recorder()
    p = SupabaseOrderPublisher(rec, PublisherConfig(sb.url, API_KEY, 2))
    p.publish(ORDER)
    p._pool.shutdown(wait=True)
    assert rec.events == [OrderPublished(ORDER.key)]


def test_secrets_and_text_are_never_logged(sb, caplog):
    with caplog.at_level(logging.DEBUG):
        for status in (201, 401, 422, 503):
            sb.status = status
            publisher(sb.url).publish_now(ORDER)
        publisher("http://127.0.0.1:9").publish_now(ORDER)
    out = " ".join(r.getMessage() for r in caplog.records)
    assert API_KEY not in out and TEXT not in out
    assert API_KEY not in repr(PublisherConfig(sb.url, API_KEY))


def test_config_from_env():
    with pytest.raises(ValueError, match="SUPABASE_API_KEY"):
        PublisherConfig.from_env({"SUPABASE_URL": "http://sb"})
    c = PublisherConfig.from_env({"SUPABASE_URL": "http://sb/", "SUPABASE_API_KEY": "k"})
    assert c.supabase_url == "http://sb" and c.api_key == "k"
