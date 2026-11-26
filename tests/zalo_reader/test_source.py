"""ZaloReader colleague: pushes, state changes, browser give-up, threading."""

from __future__ import annotations

import threading
from datetime import datetime

from zalo_order_assistant.events import VN_TZ, Message, MessageReceived, ReaderState, ReaderStatus
from zalo_order_assistant.zalo_reader.adapter import PollResult
from zalo_order_assistant.zalo_reader.source import ZaloReader


def m(key):
    return Message(key, "Shop", "An", False, "x", datetime(2026, 11, 23, 9, tzinfo=VN_TZ))


class Rec:
    def __init__(self):
        self.events = []
        self.got = threading.Event()

    def notify(self, e):
        self.events.append(e)
        self.got.set()


class FakePoller:
    def __init__(self, results):
        self.results, self.acked = list(results), []

    def poll(self):
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]

    def ack(self, key):
        self.acked.append(key)


class FakeDriver:
    def __init__(self, relaunches=3):
        self.relaunches, self.opened = relaunches, 0

    def ensure_open(self):
        self.opened += 1

    def eval_json(self, js):
        return None

    def relaunch(self):
        self.relaunches -= 1
        return self.relaunches >= 0


def reader(results, driver=None):
    rec = Rec()
    return ZaloReader(rec, FakePoller(results), driver or FakeDriver(), poll_interval_seconds=0.01), rec


def test_each_message_is_pushed_once_and_state_on_change():
    ready = PollResult(ReaderStatus.READY, [m("a"), m("b")])
    r, rec = reader([ready, ready, PollResult(ReaderStatus.READY, [m("b"), m("c")])])
    r.run_once()
    r.run_once()
    r.run_once()
    assert rec.events == [
        ReaderState(ReaderStatus.READY),
        MessageReceived(m("a")),
        MessageReceived(m("b")),
        MessageReceived(m("c")),
    ]


def test_ack_goes_to_the_store():
    r, _ = reader([PollResult(ReaderStatus.READY)])
    r.ack("g1/1")
    assert r._poller.acked == ["g1/1"]


def test_states_are_reported_and_waiting_never_exits():
    r, rec = reader([PollResult(ReaderStatus.LOGIN_REQUIRED), PollResult(ReaderStatus.NO_CHAT_OPEN)])
    assert r.run_once() and r.run_once() and r.run_once()
    assert rec.events == [ReaderState(ReaderStatus.LOGIN_REQUIRED), ReaderState(ReaderStatus.NO_CHAT_OPEN)]


def test_browser_failure_relaunches_then_gives_up_fatally():
    bad = PollResult(ReaderStatus.BROWSER_FAILED)
    r, rec = reader([bad], FakeDriver(relaunches=2))
    assert [r.run_once() for _ in range(3)] == [True, True, False]
    assert rec.events[-1] == ReaderState(ReaderStatus.BROWSER_FAILED, fatal=True)


def test_thread_starts_opens_browser_and_stops():
    driver = FakeDriver()
    r, rec = reader([PollResult(ReaderStatus.READY, [m("a")])], driver)
    r.start()
    assert rec.got.wait(2)
    r.stop()
    assert not r._thread.is_alive() and driver.opened == 1
    assert MessageReceived(m("a")) in rec.events


def test_crash_is_reported_fatally():
    class Boom(FakePoller):
        def poll(self):
            raise RuntimeError("bug")

    rec = Rec()
    r = ZaloReader(rec, Boom([]), FakeDriver(), poll_interval_seconds=0.01)
    r.start()
    assert rec.got.wait(2)
    r.stop()
    assert rec.events == [ReaderState(ReaderStatus.BROWSER_FAILED, fatal=True)]
