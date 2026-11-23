"""Ported from zalo-adater tests/test_all.py (adapter, reader, store parts) and adapted to the push design."""

from __future__ import annotations

import pytest

from zalo_order_assistant.events import ReaderStatus
from zalo_order_assistant.zalo_reader.adapter import ZaloWebAdapter
from zalo_order_assistant.zalo_reader.browser import BrowserError
from zalo_order_assistant.zalo_reader.config import ReaderConfig
from zalo_order_assistant.zalo_reader.reader import build_reader_script, load_selectors
from zalo_order_assistant.zalo_reader.store import AckedStore


class FakeDriver:
    def __init__(self, value=None, error=None):
        self.value, self.error = value, error

    def ensure_open(self):
        if self.error:
            raise self.error

    def eval_json(self, js):
        if self.error:
            raise self.error
        return self.value

    def relaunch(self):
        return True


def page(messages, **kw):
    return {
        "state": "READING",
        "conversation": {"key": "g1", "title": "Shop", "is_group": True},
        "messages": messages,
        **kw,
    }


def raw(key, ts, text="hi", kind="text", **kw):
    return {"message_key": key, "sent_at_ms": ts, "sender_uid": "u1", "sender": "An", "kind": kind, "text": text, **kw}


def make(value=None, error=None, store=None):
    store = store or AckedStore(":memory:")
    return ZaloWebAdapter(FakeDriver(value, error), store, "js"), store


def test_poll_returns_new_text_oldest_first_and_dedupes():
    a, _ = make(
        page([raw("2_u1", 2000), raw("1_u1", 1000), raw("3_u1", 3000, kind="image"), raw("4_u1", 4000, text=None)])
    )
    res = a.poll()
    assert res.state is ReaderStatus.READY
    assert [m.key for m in res.messages] == ["g1/1_u1", "g1/2_u1"]
    assert res.messages[0].sent_at.utcoffset().total_seconds() == 7 * 3600
    assert (res.messages[0].chat, res.messages[0].sender, res.messages[0].is_self) == ("Shop", "An", False)
    a.ack("g1/1_u1")
    assert [m.key for m in a.poll().messages] == ["g1/2_u1"]


def test_restart_does_not_re_emit_acked_messages(tmp_path):
    db = tmp_path / "acked.db"
    a, _ = make(page([raw("1_u1", 1000), raw("2_u1", 2000)]), store=AckedStore(db))
    a.ack("g1/1_u1")
    b, _ = make(page([raw("1_u1", 1000), raw("2_u1", 2000)]), store=AckedStore(db))  # new process
    assert [m.key for m in b.poll().messages] == ["g1/2_u1"]


class ScrollDriver:
    """A virtualized list: each scroll up mounts older messages; the reader sees one window at a time."""

    def __init__(self, windows, conv="g1"):
        self.windows, self.conv, self.i, self.scrolls = windows, conv, 0, []

    def ensure_open(self):
        pass

    def eval_json(self, js):
        if js.startswith("scroll:"):
            self.scrolls.append(js)
            if js != "scroll:bottom":
                self.i = min(self.i + 1, len(self.windows) - 1)
            return {"ok": True}
        i = self.i
        return page(
            self.windows[i],
            conversation={"key": self.conv, "title": "Shop", "is_group": True},
            scroll_top=0 if i == len(self.windows) - 1 else 500,
            client_height=1000,
        )

    def relaunch(self):
        return True


def make_backfill(windows, conv="g1", **kw):
    store = AckedStore(":memory:")
    driver = ScrollDriver(windows, conv)
    a = ZaloWebAdapter(
        driver,
        store,
        "js",
        scroll_script=lambda dy: "scroll:bottom" if dy is None else f"scroll:{dy}",
        settle_seconds=0,
        sleep=lambda _: None,
        **kw,
    )
    return a, store, driver


def test_conversation_change_reads_whole_history_then_only_visible():
    windows = [
        [raw("5_u1", 5000), raw("6_u1", 6000)],
        [raw("3_u1", 3000), raw("4_u1", 4000)],
        [raw("1_u1", 1000), raw("2_u1", 2000)],
    ]
    a, _, d = make_backfill(windows, backfill_days=0)
    res = a.poll()
    assert [m.key for m in res.messages] == [f"g1/{n}_u1" for n in range(1, 7)]
    assert d.scrolls[-1] == "scroll:bottom"  # left at the newest message
    for m in res.messages:
        a.ack(m.key)
    d.i = 0
    d.scrolls.clear()
    assert a.poll().messages == [] and d.scrolls == []  # synced: no more scrolling


def test_backfill_runs_once_per_chat_open_even_before_acks():
    """Push design (changed from the spike): the Coordinator holds what was emitted, so the history is not
    scrolled again on every poll while it is being processed. Visible un-acked messages are still returned."""
    windows = [[raw("2_u1", 2000)], [raw("1_u1", 1000)]]
    a, _, d = make_backfill(windows, backfill_days=0)
    assert [m.key for m in a.poll().messages] == ["g1/1_u1", "g1/2_u1"]
    d.i = 0
    d.scrolls.clear()
    assert [m.key for m in a.poll().messages] == ["g1/2_u1"] and d.scrolls == []


def test_opening_another_chat_backfills_it():
    windows = [[raw("2_u1", 2000)], [raw("1_u1", 1000)]]
    a, _, d = make_backfill(windows, backfill_days=0)
    a.poll()
    d.conv, d.i = "g2", 0
    d.scrolls.clear()
    assert [m.key for m in a.poll().messages] == ["g2/1_u1", "g2/2_u1"] and d.scrolls


def test_backfill_stops_at_the_day_window():
    day = 86400 * 1000
    now = 10 * day
    windows = [[raw("a", now - 1000)], [raw("b", now - 2 * day)], [raw("c", now - 4 * day)], [raw("d", now - 5 * day)]]
    a, _, d = make_backfill(windows, backfill_days=3, clock=lambda: now / 1000)
    assert [m.key for m in a.poll().messages] == ["g1/b", "g1/a"]  # c is older than 3 days: dropped; d never loaded
    assert d.i == 2


def test_private_chat_sender_falls_back_to_title():
    v = page([raw("1_u1", 1000, sender=None)])
    v["conversation"]["is_group"] = False
    a, _ = make(v)
    assert a.poll().messages[0].sender == "Shop"


def test_group_sender_with_only_a_uid_is_empty_FR4():
    a, _ = make(page([raw("1_u9", 1000, sender=None, sender_uid="u9")]))
    assert a.poll().messages[0].sender == ""


def test_sender_name_from_another_bubble_of_the_same_uid():
    a, _ = make(page([raw("1_u1", 1000, sender=None)], senders={"u1": "Mai"}))
    assert a.poll().messages[0].sender == "Mai"


def test_own_message_is_marked_is_self():
    a, _ = make(page([raw("1_0", 1000, sender=None, sender_uid="0", is_self=True)]))
    m = a.poll().messages[0]
    assert m.is_self and m.sender == ""


@pytest.mark.parametrize(
    "value,state",
    [
        ({"state": "LOGIN"}, ReaderStatus.LOGIN_REQUIRED),
        ({"state": "NO_CHAT"}, ReaderStatus.NO_CHAT_OPEN),
        ({"state": "NO_CHAT", "rendered_bubbles": 5}, ReaderStatus.SELECTOR_DRIFT),
        ({"state": "ERROR", "error": "x"}, ReaderStatus.SELECTOR_DRIFT),
        (page([], rendered_bubbles=4), ReaderStatus.SELECTOR_DRIFT),
        (page([], drift="x"), ReaderStatus.SELECTOR_DRIFT),
        ({"nonsense": 1}, ReaderStatus.SELECTOR_DRIFT),
    ],
)
def test_states(value, state):
    a, _ = make(value)
    assert a.poll().state is state


def test_browser_failure():
    a, _ = make(error=BrowserError("x"))
    assert a.poll().state is ReaderStatus.BROWSER_FAILED


def test_reader_script_builds_from_packaged_selectors():
    script = build_reader_script(load_selectors())
    assert "readState" in script and "#messageViewContainer" in script


def test_store_is_shared_safely_between_threads(tmp_path):
    import threading

    s = AckedStore(tmp_path / "x.db")
    ts = [threading.Thread(target=lambda i=i: s.add([f"k{i}"])) for i in range(20)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert all(s.contains(f"k{i}") for i in range(20))


def test_config_from_env():
    c = ReaderConfig.from_env({"ZALO_CHAT": "  Bếp Phương Bắc ", "BACKFILL_DAYS": "1", "POLL_INTERVAL_SECONDS": ""})
    assert (c.zalo_chat, c.backfill_days, c.poll_interval_seconds, c.selectors_path) == (
        "Bếp Phương Bắc",
        1.0,
        2.0,
        None,
    )
