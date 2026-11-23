"""Coordinator rules (spec §2.5) against fake colleagues. AC-4, AC-6, AC-7, AC-23, AC-24, AC-25."""

from __future__ import annotations

import logging
import threading
from datetime import datetime

import pytest

from zalo_order_assistant.audit_log import SqliteAuditLog
from zalo_order_assistant.coordinator import Coordinator
from zalo_order_assistant.events import (
    VN_TZ,
    ExtractFailed,
    FailureKind,
    Message,
    MessageReceived,
    NotAnOrder,
    Order,
    OrderExtracted,
    OrderPublished,
    PipelineState,
    PublishFailed,
    ReaderState,
    ReaderStatus,
    RetryDue,
)

SENT = datetime(2026, 11, 23, 9, 0, tzinfo=VN_TZ)
SECRET_TEXT = "Mai đặt 2 phần, ship nhé 0901234567"


def msg(key: str, sender: str = "Mai", is_self: bool = False, text: str = SECRET_TEXT) -> Message:
    return Message(key, "Bếp", sender, is_self, text, SENT)


def order(key: str) -> Order:
    return Order(key, "Mai", 2, "2026-11-24", SECRET_TEXT, SENT)


class FakeSource:
    def __init__(self) -> None:
        self.acked: list[str] = []
        self.started = self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def ack(self, key: str) -> None:
        self.acked.append(key)


class FakeExtractor:
    """Answers synchronously by calling `notify` from inside `extract`, like a colleague on a fast worker."""

    def __init__(self, coord: Coordinator) -> None:
        self.coord = coord
        self.script: dict[str, list[object]] = {}  # key -> outcomes, one per call; default: an order
        self.calls: list[str] = []

    def extract(self, m: Message) -> None:
        self.calls.append(m.key)
        outcomes = self.script.get(m.key)
        out = outcomes.pop(0) if outcomes else "order"
        if out == "order":
            self.coord.notify(OrderExtracted(order(m.key)))
        elif out == "none":
            self.coord.notify(NotAnOrder(m.key, "receipt"))
        elif isinstance(out, FailureKind):
            self.coord.notify(ExtractFailed(m.key, out, "boom"))
        # "silent": no answer (still working)


class FakePublisher:
    def __init__(self, coord: Coordinator) -> None:
        self.coord = coord
        self.script: dict[str, list[object]] = {}
        self.calls: list[str] = []
        self.default: object = 201

    def publish(self, o: Order) -> None:
        self.calls.append(o.key)
        outcomes = self.script.get(o.key)
        out = outcomes.pop(0) if outcomes else self.default
        if out in (200, 201):
            self.coord.notify(OrderPublished(o.key))
        elif out is None:
            self.coord.notify(PublishFailed(o.key, FailureKind.SYSTEMIC, None, "ConnectionError"))
        else:
            assert isinstance(out, int)
            kind = FailureKind.PERMANENT if out in (400, 405, 422) else FailureKind.SYSTEMIC
            self.coord.notify(PublishFailed(o.key, kind, out))


class FakeScheduler:
    def __init__(self) -> None:
        self.timers: list[FakeTimer] = []

    def __call__(self, delay, fn):
        t = FakeTimer(delay, fn)
        self.timers.append(t)
        return t

    @property
    def delays(self) -> list[float]:
        return [t.delay for t in self.timers]

    def fire(self) -> None:
        live = [t for t in self.timers if not t.cancelled and not t.fired]
        assert live, "no timer pending"
        live[-1].fired = True
        live[-1].fn()


class FakeTimer:
    def __init__(self, delay, fn) -> None:
        self.delay, self.fn, self.cancelled, self.fired = delay, fn, False, False

    def cancel(self) -> None:
        self.cancelled = True


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class Rig:
    def __init__(self, **kw) -> None:
        self.audit = SqliteAuditLog(":memory:")
        self.sched = FakeScheduler()
        self.clock = Clock()
        self.status: list[str] = []
        self.coord = Coordinator(self.audit, schedule=self.sched, clock=self.clock, on_status=self.status.append, **kw)
        self.source = FakeSource()
        self.ext = FakeExtractor(self.coord)
        self.pub = FakePublisher(self.coord)
        self.coord.attach(self.source, self.ext, self.pub)

    def receive(self, *messages: Message) -> None:
        for m in messages:
            self.coord.notify(MessageReceived(m))
        self.drain()

    def drain(self) -> None:
        self.coord.drain()

    def fire(self) -> None:
        self.sched.fire()
        self.drain()

    @property
    def audit_rows(self):
        return [(k, reason, status) for k, _sent, reason, status in self.audit.entries()]


@pytest.fixture
def rig() -> Rig:
    return Rig()


def test_order_is_extracted_published_then_acked(rig):
    rig.receive(msg("a"))
    assert rig.ext.calls == ["a"] and rig.pub.calls == ["a"] and rig.source.acked == ["a"]
    assert rig.audit_rows == [] and rig.coord.queued == 0


def test_second_separate_message_makes_a_second_order_AC6(rig):
    rig.receive(msg("a"), msg("b"))
    assert rig.pub.calls == ["a", "b"] and rig.source.acked == ["a", "b"]


def test_not_an_order_is_recorded_then_acked(rig):
    rig.ext.script["a"] = ["none"]
    rig.receive(msg("a"))
    assert rig.pub.calls == [] and rig.source.acked == ["a"]
    assert rig.audit_rows == [("a", "receipt", "not_an_order")]
    assert rig.audit.entries()[0][1] == SENT.isoformat()


def test_permanent_extract_failure_is_recorded_and_acked(rig):
    rig.ext.script["a"] = [FailureKind.PERMANENT]
    rig.receive(msg("a"), msg("b"))
    assert rig.source.acked == ["a", "b"] and rig.audit_rows == [("a", "extractor: boom", "failed")]
    assert rig.sched.timers == []


@pytest.mark.parametrize("status", [400, 405, 422])
def test_permanent_publish_failure_is_recorded_and_acked(rig, status):
    rig.pub.script["a"] = [status]
    rig.receive(msg("a"))
    assert rig.source.acked == ["a"] and rig.audit_rows == [("a", f"publisher {status}", "failed")]
    assert rig.coord.pipeline_state is PipelineState.OK


def test_per_message_failure_retries_with_backoff_then_acks_at_cap_AC24(rig):
    rig.ext.script["a"] = [FailureKind.PER_MESSAGE] * 10
    rig.receive(msg("a"), msg("b"))
    for _ in range(4):
        assert rig.source.acked == []
        rig.fire()
    assert rig.ext.calls == ["a"] * 5 + ["b"]
    assert rig.sched.delays == [2, 4, 8, 16]
    assert rig.source.acked == ["a", "b"]  # the next message is processed after the cap
    assert rig.audit_rows == [("a", "extractor: boom", "retry_cap")]


def test_per_message_failure_that_recovers_is_not_recorded(rig):
    rig.ext.script["a"] = [FailureKind.PER_MESSAGE, "order"]
    rig.receive(msg("a"))
    rig.fire()
    assert rig.source.acked == ["a"] and rig.audit_rows == []


def test_newer_messages_are_held_behind_a_retry(rig):
    rig.ext.script["a"] = [FailureKind.PER_MESSAGE, "order"]
    rig.receive(msg("a"), msg("b"), msg("c"))
    assert rig.ext.calls == ["a"] and rig.source.acked == []
    rig.fire()
    assert rig.ext.calls == ["a", "a", "b", "c"] and rig.source.acked == ["a", "b", "c"]


def test_systemic_extractor_failure_pauses_without_ack_or_count(rig):
    rig.ext.script["a"] = [FailureKind.SYSTEMIC] * 8 + ["order"]
    rig.receive(msg("a"), msg("b"))
    assert rig.coord.pipeline_state is PipelineState.EXTRACTOR_UNAVAILABLE
    for _ in range(8):
        assert rig.source.acked == []
        rig.fire()
    assert rig.sched.delays == [2, 4, 8, 16, 32, 60, 60, 60]  # capped, never gives up
    assert rig.source.acked == ["a", "b"] and rig.audit_rows == []
    assert rig.coord.pipeline_state is PipelineState.OK


def test_supabase_down_holds_then_saves_once_without_reextracting_AC7(rig):
    rig.pub.script["a"] = [503, None, 201]
    rig.receive(msg("a"))
    assert rig.coord.pipeline_state is PipelineState.PUBLISHER_UNAVAILABLE and rig.source.acked == []
    rig.fire()
    rig.fire()
    assert rig.pub.calls == ["a", "a", "a"] and rig.ext.calls == ["a"]
    assert rig.source.acked == ["a"] and rig.coord.pipeline_state is PipelineState.OK


@pytest.mark.parametrize("status", [401, 403])
def test_revoked_key_shows_unauthorized_acks_nothing_and_recovers_AC25(rig, status, caplog):
    rig.pub.default = status
    with caplog.at_level(logging.ERROR):
        rig.receive(msg("a"), msg("b"))
        for _ in range(7):  # more than MAX_ATTEMPTS: systemic failures are not counted
            rig.fire()
    assert rig.coord.pipeline_state is PipelineState.PUBLISHER_UNAUTHORIZED
    assert rig.source.acked == [] and rig.audit_rows == []
    assert any("PUBLISHER_UNAUTHORIZED" in r.getMessage() for r in caplog.records)
    assert any("pipeline=PUBLISHER_UNAUTHORIZED" in s for s in rig.status)
    rig.pub.default = 201  # key fixed
    rig.fire()
    assert rig.source.acked == ["a", "b"] and rig.coord.pipeline_state is PipelineState.OK
    assert rig.ext.calls == ["a", "b"]


def test_unauthorized_state_is_repeated_every_60s(rig, caplog):
    rig.pub.default = 401
    rig.receive(msg("a"))
    caplog.clear()
    with caplog.at_level(logging.ERROR):
        rig.clock.t += 30
        rig.coord._tick()
        assert not caplog.records
        rig.clock.t += 31
        rig.coord._tick()
    assert "still" in caplog.records[0].getMessage()


def test_duplicate_key_is_queued_once_AC4(rig):
    rig.ext.script["a"] = ["silent"]
    rig.receive(msg("a"), msg("a"), msg("b"), msg("b"))
    assert rig.coord.queued == 2
    rig.coord.notify(OrderExtracted(order("a")))
    rig.drain()
    assert rig.pub.calls == ["a", "b"] and rig.source.acked == ["a", "b"]
    rig.receive(msg("a"))  # re-read after the ack (the Reader raced the ack): ignored
    assert rig.ext.calls == ["a", "b"]


def test_own_message_is_skipped_recorded_and_acked_AC23(rig):
    rig.receive(msg("a", sender="Chủ quán", is_self=True))
    assert rig.ext.calls == [] and rig.source.acked == ["a"]
    assert rig.audit_rows == [("a", "own message", "skipped")]


def test_own_message_is_processed_when_configured():
    r = Rig(include_own_messages=True)
    r.receive(msg("a", is_self=True))
    assert r.pub.calls == ["a"]


@pytest.mark.parametrize("sender", ["", "   "])
def test_nameless_sender_is_skipped(rig, sender):
    rig.receive(msg("a", sender=sender))
    assert rig.ext.calls == [] and rig.audit_rows == [("a", "no sender name", "skipped")]


def test_notify_from_inside_a_command_does_not_reenter(rig):
    rig.receive(msg("a"))
    # The fake extractor called notify() inside extract(); the result was queued, not handled inline:
    assert rig.coord._inbox.empty()
    calls_during = []
    rig.ext.extract = lambda m: (rig.coord.notify(OrderExtracted(order(m.key))), calls_during.append(rig.pub.calls[:]))
    rig.receive(msg("b"))
    assert calls_during == [["a"]]  # publish of b had not run while extract(b) was still on the stack
    assert rig.pub.calls == ["a", "b"]


def test_stale_and_foreign_events_are_ignored(rig):
    rig.ext.script["a"] = ["silent"]
    rig.receive(msg("a"))
    rig.coord.notify(OrderPublished("zzz"))
    rig.coord.notify(NotAnOrder("zzz", "x"))
    rig.coord.notify(RetryDue("a"))  # not waiting for a retry
    rig.drain()
    assert rig.source.acked == [] and rig.coord.queued == 1


def test_reader_state_is_logged_and_fatal_state_stops(rig):
    rig.coord.notify(ReaderState(ReaderStatus.CHAT_NOT_FOUND))
    rig.drain()
    assert rig.coord.reader_state is ReaderStatus.CHAT_NOT_FOUND and "reader=CHAT_NOT_FOUND" in rig.status[-1]
    t = threading.Thread(target=rig.coord.run)
    t.start()
    rig.coord.notify(ReaderState(ReaderStatus.BROWSER_FAILED, fatal=True))
    t.join(2)
    assert not t.is_alive() and rig.source.stopped


def test_run_and_shutdown_leave_in_flight_message_unacked(rig):
    rig.ext.script["a"] = ["silent"]
    t = threading.Thread(target=rig.coord.run)
    t.start()
    rig.coord.notify(MessageReceived(msg("a")))
    rig.coord.stop()
    t.join(2)
    assert not t.is_alive() and rig.source.started and rig.source.stopped
    assert rig.source.acked == []  # re-read and re-processed on the next start


def test_shutdown_cancels_a_pending_retry(rig):
    rig.ext.script["a"] = [FailureKind.SYSTEMIC]
    rig.receive(msg("a"))
    rig.coord.stop()
    rig.coord.run()
    assert rig.sched.timers[0].cancelled


def test_message_text_is_never_logged(rig, caplog):
    rig.ext.script = {"b": ["none"], "c": [FailureKind.PER_MESSAGE] * 5, "d": [FailureKind.PERMANENT]}
    rig.pub.script = {"a": [401, 201]}
    with caplog.at_level(logging.DEBUG):
        rig.receive(msg("a"), msg("b"), msg("c"), msg("d"), msg("e", is_self=True))
        for _ in range(5):
            rig.fire()
    assert rig.source.acked == ["e", "a", "b", "c", "d"]
    assert caplog.records and all(SECRET_TEXT not in r.getMessage() for r in caplog.records)
    assert all(SECRET_TEXT not in str(row) for row in rig.audit.entries())


def test_audit_log_persists(tmp_path):
    path = tmp_path / "sub" / "audit.db"
    a = SqliteAuditLog(path)
    a.record("g1/1", SENT, "past day", "not_an_order")
    a.close()
    assert SqliteAuditLog(path).entries() == [("g1/1", SENT.isoformat(), "past day", "not_an_order")]
