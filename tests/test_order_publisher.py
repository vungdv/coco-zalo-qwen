"""OrderPublisher contract against a mock HTTP server, for every status of spec §2.3 (FR-9, FR-10, AC-7, AC-25)."""

from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import Executor, Future
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from zalo_order_assistant.events import VN_TZ, FailureKind, Op, Order, OrderNotApplied, OrderPublished, PublishFailed
from zalo_order_assistant.order_publisher import PublisherConfig, SupabaseOrderPublisher, build_payload

TEXT = "Mai đặt 2 phần, ship nhé"
PLACED = datetime(2026, 11, 23, 8, 5, 7, 123456, tzinfo=VN_TZ)
ORDER = Order("g123/1732330800000_42", "Mai", 2, "2026-11-24", TEXT, PLACED)
API_KEY = "sk-test-SECRET"


class MockSupabase:
    def __init__(self) -> None:
        self.status = 201
        self.body = b"{}"
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
                self.wfile.write(mock.body)

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
    _server.status, _server.body, _server.requests = 201, b"{}", []
    return _server


def publisher(url: str, rec: Recorder | None = None) -> SupabaseOrderPublisher:
    cfg = PublisherConfig(url, API_KEY, timeout_seconds=2)
    return SupabaseOrderPublisher(rec or Recorder(), cfg, executor=Inline())


def test_payload_maps_fields_and_never_sends_delivered():
    assert build_payload(ORDER) == {
        "source": "zalo",
        "external_id": "g123/1732330800000_42",
        "op": "add",
        "customer_name": "Mai",
        "quantity": 2,
        "delivery_date": "2026-11-24",
        "message": TEXT,
        "placed_at": "2026-11-23T08:05:07+07:00",
    }


def test_placed_at_is_the_message_time_with_offset_so_a_backfilled_order_keeps_its_real_time():
    # Given a message sent at 08:05 VN but published hours later, Then Supabase receives 08:05 +07:00, not now
    assert build_payload(ORDER)["placed_at"] == "2026-11-23T08:05:07+07:00"
    assert datetime.fromisoformat(build_payload(ORDER)["placed_at"]) == PLACED.replace(microsecond=0)


def test_message_is_cut_at_2000_chars():
    long = Order("k", "Mai", 1, "2026-11-24", "x" * 2500, PLACED)
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
        (409, FailureKind.PERMANENT),  # a 409 with no known error code
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


def test_payload_sends_customer_id_namespaced_by_source():
    from dataclasses import replace

    assert build_payload(replace(ORDER, customer_id="zalo:123456789"))["customer_id"] == "zalo:123456789"
    assert "customer_id" not in build_payload(ORDER)


@pytest.mark.parametrize("op,qty", [(Op.ADD, 2), (Op.REDUCE, 1), (Op.SET, 3)])
def test_payload_sends_the_op_and_its_quantity(op, qty):
    from dataclasses import replace

    p = build_payload(replace(ORDER, op=op, qty=qty))
    assert (p["op"], p["quantity"], p["customer_name"]) == (op.value, qty, "Mai")


def test_cancel_sends_no_quantity():
    from dataclasses import replace

    p = build_payload(replace(ORDER, op=Op.CANCEL, qty=None))
    assert p["op"] == "cancel" and "quantity" not in p and p["customer_name"] == "Mai"


def problem(error: str) -> bytes:
    return json.dumps({"type": "about:blank", "title": "Conflict", "status": 409, "error": error}).encode()


def test_no_open_order_is_not_applied_and_not_a_failure(sb):
    from dataclasses import replace

    sb.status, sb.body = 409, problem("no_open_order")
    rec = Recorder()
    publisher(sb.url, rec).publish(replace(ORDER, op=Op.REDUCE, qty=1))
    assert rec.events == [OrderNotApplied(ORDER.key, "no_open_order")]


def test_idempotency_conflict_is_permanent_with_its_code(sb):
    sb.status, sb.body = 409, problem("idempotency_conflict")
    e = publisher(sb.url).publish_now(ORDER)
    assert e == PublishFailed(ORDER.key, FailureKind.PERMANENT, 409, "idempotency_conflict")


@pytest.mark.parametrize("body", [b"not json", b"[1]", problem("something we do not know: Mai 0901")])
def test_unreadable_or_unknown_409_is_a_permanent_conflict_and_never_echoes_the_body(sb, body):
    sb.status, sb.body = 409, body
    e = publisher(sb.url).publish_now(ORDER)
    assert e == PublishFailed(ORDER.key, FailureKind.PERMANENT, 409, "conflict")


def test_not_applied_log_names_op_and_date_but_not_the_text(sb, caplog):
    from dataclasses import replace

    sb.status, sb.body = 409, problem("no_open_order")
    with caplog.at_level(logging.INFO):
        publisher(sb.url).publish_now(replace(ORDER, op=Op.CANCEL, qty=None))
    out = " ".join(r.getMessage() for r in caplog.records)
    assert "op=cancel" in out and "date=2026-11-24" in out and TEXT not in out and API_KEY not in out
