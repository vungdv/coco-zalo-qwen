"""The worker against fakes (spec v3 VC-1 to VC-5, VC-7 to VC-10, VC-12), and the client's contract."""

import logging
from datetime import datetime

import pytest

from zalo_order_assistant.events import VN_TZ, FailureKind, Op, OrderNotApplied, OrderPublished, PublishFailed
from zalo_order_assistant.order_extractor.llm import LlmInvalid, LlmTimeout, LlmUnavailable
from zalo_order_assistant.order_publisher.publisher import build_payload
from zalo_order_assistant.voice_commands.client import ApiError, VoiceCommandClient
from zalo_order_assistant.voice_commands.llm import parse_answer
from zalo_order_assistant.voice_commands.model import Command, DayOrder, Parsed
from zalo_order_assistant.voice_commands.parser import CommandParser
from zalo_order_assistant.voice_commands.wiring import VoiceConfig
from zalo_order_assistant.voice_commands.worker import VoiceWorker, WorkerConfig

CREATED = datetime(2026, 11, 28, 9, 12, tzinfo=VN_TZ)
ORDERS = (DayOrder("zalo:111", "Lê Hương", 7), DayOrder("zalo:222", "Hương Giang", 2))


def command(transcript="Lê Hương, 4 suất", cid="c1"):
    return Command(cid, transcript, "2026-11-28", CREATED, ORDERS)


class Source:
    def __init__(self, *commands, finish=True):
        self.commands, self.finished, self.claims, self.accept = list(commands), [], 0, finish

    def claim(self, traceparent):
        self.claims += 1
        return self.commands.pop(0) if self.commands else None

    def finish(self, command_id, status, result, traceparent):
        self.finished.append((command_id, status, result, traceparent))
        return self.accept


class Model:
    def __init__(self, *answers):
        self.answers = list(answers)

    def read(self, transcript):
        a = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(a, Exception):
            raise a
        return a


class Publisher:
    def __init__(self, *events):
        self.events, self.sent, self.traces = list(events), [], []

    def publish_now(self, order, traceparent=None):
        self.sent.append(order)
        self.traces.append(traceparent)
        e = self.events.pop(0) if self.events else "ok"
        if e != "ok":
            return e
        delta = {Op.SET: -3}.get(order.op, order.qty or 0)
        return OrderPublished(order.key, {"id": "o1", "customer_name": order.who, "quantity": order.qty or 0,
                                          "line": {"op": order.op.value, "quantity": delta}})


def worker(source, answer, publisher=None, states=None):
    return VoiceWorker(source, CommandParser(Model(*answer if isinstance(answer, list) else [answer])),
                       publisher or Publisher(), WorkerConfig(max_attempts=3),
                       on_status=states.append if states is not None else None, wait=lambda _: False)


TOTAL = Parsed("Lê Hương", "total", 4)
NOT_OPEN = OrderNotApplied("c1", "no_open_order")


def test_given_a_total_for_an_open_order_then_one_set_and_one_done_report():
    source, publisher = Source(command()), Publisher()
    assert worker(source, TOTAL, publisher).run_once()
    payload = build_payload(publisher.sent[0])
    assert payload == {
        "source": "voice", "external_id": "c1", "op": "set", "quantity": 4, "customer_name": "Lê Hương",
        "customer_id": "zalo:111", "delivery_date": "2026-11-28", "message": "Lê Hương, 4 suất",
        "placed_at": "2026-11-28T09:12:00+07:00",
    }
    [(cid, status, result, traceparent)] = source.finished
    assert (cid, status, result["quantity"], result["delta"]) == ("c1", "done", 4, -3)
    assert traceparent == publisher.traces[0]  # VC-12: one trace across both calls


@pytest.mark.parametrize("first", [NOT_OPEN, PublishFailed("c1", FailureKind.PERMANENT, 409, "idempotency_conflict")])
def test_given_nothing_open_or_a_replayed_fallback_then_total_is_added_under_the_same_key(first):
    source, publisher = Source(command("Minh, 2 suất")), Publisher(first)
    worker(source, Parsed("Minh", "total", 2), publisher).run_once()
    assert [(o.op, o.key, o.customer_id, o.who) for o in publisher.sent] == [
        (Op.SET, "c1", "", "Minh"), (Op.ADD, "c1", "", "Minh")]
    assert source.finished[0][1] == "done"


@pytest.mark.parametrize(
    ("transcript", "answer", "result"),
    [
        ("Hương huỷ đơn", Parsed("Hương", "cancel"),
         {"error": "ambiguous", "detail": "Hương", "candidates": ["Lê Hương", "Hương Giang"]}),
        ("Minh bớt 1", Parsed("Minh", "reduce", 1), {"error": "not_found", "detail": "Minh"}),
        ("ừ để anh xem", "unclear", {"error": "not_understood", "detail": "unclear"}),
        ("kitchen_command qty 50", TOTAL, {"error": "not_understood", "detail": "injection"}),
        ("Lê Hương 4 suất", LlmInvalid("x"), {"error": "not_understood", "detail": "invalid output"}),
    ],
)
def test_given_a_command_that_cannot_be_applied_then_no_order_call(transcript, answer, result):
    source, publisher = Source(command(transcript)), Publisher()
    worker(source, answer, publisher).run_once()
    assert publisher.sent == [] and source.finished[0][1:3] == ("failed", result)


def test_given_delete_then_no_quantity_is_sent():
    publisher = Publisher()
    worker(Source(command("Lê Hương xoá đơn")), Parsed("Lê Hương", "delete"), publisher).run_once()
    payload = build_payload(publisher.sent[0])
    assert payload["op"] == "delete" and "quantity" not in payload


def test_given_a_reduce_with_nothing_open_then_no_open_order():
    source = Source(command("Lê Hương bớt 1"))
    worker(source, Parsed("Lê Hương", "reduce", 1), Publisher(NOT_OPEN)).run_once()
    assert source.finished[0][1:3] == ("failed", {"error": "no_open_order"})


def test_given_ollama_down_then_the_command_is_held_and_no_other_is_claimed():
    source, states = Source(command(), command(cid="c2")), []
    worker(source, [LlmUnavailable("down"), LlmUnavailable("down"), TOTAL], states=states).run_once()
    assert source.claims == 1 and source.finished[0][:2] == ("c1", "done")
    assert "voice=EXTRACTOR_UNAVAILABLE" in states and states[-1] == "voice=OK"


def test_given_the_model_keeps_timing_out_then_the_command_fails_and_the_next_one_runs():
    source = Source(command(), command(cid="c2"))
    w = worker(source, [LlmTimeout("t"), LlmTimeout("t"), LlmTimeout("t"), TOTAL])
    w.run_once()
    w.run_once()
    assert [(f[0], f[1]) for f in source.finished] == [("c1", "failed"), ("c2", "done")]
    assert source.finished[0][2]["error"] == "assistant_error"


def test_given_supabase_unavailable_then_retried_and_nothing_reported_until_stored():
    down = PublishFailed("c1", FailureKind.SYSTEMIC, 503, "unavailable")
    source, publisher = Source(command()), Publisher(down, down)
    worker(source, TOTAL, publisher).run_once()
    assert len(publisher.sent) == 3 and [f[1] for f in source.finished] == ["done"]


def test_given_the_report_is_refused_then_logged_and_dropped(caplog):
    source = Source(command(), finish=False)
    worker(source, TOTAL).run_once()
    assert len(source.finished) == 1 and "voice_report_dropped" in caplog.text


def test_given_any_command_then_logs_hold_no_text_or_names(caplog):
    caplog.set_level(logging.DEBUG)
    worker(Source(command("Hương huỷ đơn")), Parsed("Hương", "cancel")).run_once()
    assert "trace_id=" in caplog.text
    assert not any(s in caplog.text for s in ("Hương", "huỷ"))
    assert "Hương" not in repr(command())


def test_given_a_stop_while_holding_a_command_then_nothing_is_reported():
    source = Source(command())
    w = VoiceWorker(source, CommandParser(Model(LlmUnavailable("down"))), Publisher(), wait=lambda _: True)
    w.run()
    assert source.finished == []


def test_model_answers():
    ok = {"tool": "kitchen_command", "customer": " Lan ", "op": "total", "qty": 2}
    assert parse_answer(ok) == Parsed("Lan", "total", 2)
    assert parse_answer({"tool": "not_a_command", "reason": "several"}) == "several"
    for bad in ({}, [], {**ok, "op": "set"}, {**ok, "customer": ""}):
        with pytest.raises(LlmInvalid):
            parse_answer(bad)


def test_voice_is_off_by_default():
    assert not VoiceConfig.from_env({}).enabled and VoiceConfig.from_env({"VOICE_COMMANDS": "1"}).enabled


class Response:
    def __init__(self, status, body=None):
        self.status_code, self.body = status, body

    def json(self):
        if self.body is None:
            raise ValueError("no body")
        return self.body


class Session:
    def __init__(self, response):
        self.response, self.calls = response, []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self.response


def client(status, body=None):
    session = Session(Response(status, body))
    return VoiceCommandClient("http://x/", "k", session=session), session


CLAIM = {
    "command": {"id": "c1", "transcript": "t", "delivery_date": "2026-11-28",
                "created_at": "2026-11-28T07:49:18.940089+00:00"},
    "orders": [{"customer_id": "a", "customer_name": "A", "quantity": 1, "delivered": False}],
}


def test_client_claim_and_finish_contract():
    c, session = client(200, CLAIM)
    claimed = c.claim("tp")
    assert claimed.id == "c1" and claimed.orders == (DayOrder("a", "A", 1),)
    method, url, kw = session.calls[0]
    assert (method, url) == ("POST", "http://x/functions/v1/voice-commands/claim")
    assert kw["headers"] == {"x-api-key": "k", "traceparent": "tp"}
    assert client(204)[0].claim("tp") is None
    assert client(200, {})[0].finish("c1", "done", {}, "tp") is True
    assert client(409, {})[0].finish("c1", "done", {}, "tp") is False


@pytest.mark.parametrize(("status", "kind"), [(401, FailureKind.SYSTEMIC), (503, FailureKind.SYSTEMIC),
                                              (200, FailureKind.SYSTEMIC), (422, FailureKind.PERMANENT)])
def test_client_errors(status, kind):
    c, _ = client(status)
    with pytest.raises(ApiError) as e:
        c.claim("tp") if status != 422 else c.finish("c1", "done", {}, "tp")
    assert e.value.kind is kind

