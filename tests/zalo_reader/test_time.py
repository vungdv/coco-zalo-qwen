"""T5c: a message whose time is unknown is never emitted; times stay right across midnight and in a backfill."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from zalo_order_assistant.events import VN_TZ
from zalo_order_assistant.zalo_reader.adapter import ZaloWebAdapter
from zalo_order_assistant.zalo_reader.store import AckedStore

FIXTURE = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "reader_backfill.json").read_text("utf-8"))


class ReplayDriver:
    """Replays the recorded reader output: each scroll up shows the next (older) window."""

    def __init__(self, windows):
        self.windows, self.i = windows, 0

    def ensure_open(self):
        pass

    def eval_json(self, js):
        if js.startswith("scroll:"):
            if js != "scroll:bottom":
                self.i = min(self.i + 1, len(self.windows) - 1)
            else:
                self.i = 0
            return {"ok": True}
        return {
            "state": "READING",
            "conversation": FIXTURE["conversation"],
            "messages": self.windows[self.i],
            "scroll_top": 0 if self.i == len(self.windows) - 1 else 500,
            "client_height": 800,
        }

    def relaunch(self):
        return True


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def adapter(windows=None, clock=None):
    driver = ReplayDriver(windows or FIXTURE["windows"])
    clock = clock or Clock(FIXTURE["now_ms"] / 1000)
    a = ZaloWebAdapter(
        driver,
        AckedStore(":memory:"),
        "js",
        scroll_script=lambda dy: "scroll:bottom" if dy is None else f"scroll:{dy}",
        backfill_days=3,
        clock=clock,
        settle_seconds=0,
        sleep=lambda _: None,
    )
    return a, driver, clock


def test_backfill_over_three_days_and_midnight_has_the_right_vietnam_dates():
    a, _, _ = adapter()
    got = {m.key.split("/")[1]: m.sent_at for m in a.poll().messages}
    assert list(got) == ["e", "a", "c", "d"]  # oldest first; f (08:59 on the 20th) is just past the 3-day window
    assert got["a"].date().isoformat() == "2026-11-22" and got["a"].strftime("%H:%M:%S") == "23:59:59"
    assert got["c"].date().isoformat() == "2026-11-23" and got["c"].strftime("%H:%M:%S") == "00:00:00"
    assert got["e"] == datetime(2026, 11, 20, 9, 0, tzinfo=VN_TZ)
    assert all(t.utcoffset().total_seconds() == 7 * 3600 for t in got.values())


def test_bubble_with_no_time_is_not_emitted_and_not_acked_AC26():
    a, _, _ = adapter()
    keys = [m.key for m in a.poll().messages]
    assert "g5/h0" not in keys and not a.store.contains("g5/h0")
    assert all(m.sent_at.year >= 2020 for m in a.poll().messages)  # never a 1970 date


def test_bubble_is_emitted_once_its_time_is_readable():
    windows = json.loads(json.dumps(FIXTURE["windows"]))
    a, _, _ = adapter(windows)
    a.poll()
    windows[0][2]["sent_at_ms"] = FIXTURE["now_ms"] - 60_000  # the bubble now carries its id/time
    assert "g5/h0" in [m.key for m in a.poll().messages]


def test_history_is_read_again_later_when_it_held_an_unknown_time():
    windows = json.loads(json.dumps(FIXTURE["windows"]))
    windows[0].pop(2)
    windows[2].append(
        {
            "message_key": "h1",
            "sent_at_ms": 0,
            "sender_uid": "unknown",
            "sender": "Mai",
            "is_self": False,
            "kind": "text",
            "text": "kia 2 phần",
        }
    )
    a, d, clock = adapter(windows)
    a.poll()
    d.i = 0
    first = a.poll()  # no re-read yet: only the visible window
    assert d.i == 0 and first.messages
    windows[2][-1]["sent_at_ms"] = FIXTURE["now_ms"] - 3 * 3600_000
    clock.t += 301
    assert "g5/h1" in [m.key for m in a.poll().messages]  # history re-read after rebackfill_seconds


def test_no_re_read_when_every_time_was_known():
    windows = json.loads(json.dumps(FIXTURE["windows"]))
    windows[0].pop(2)
    a, d, clock = adapter(windows)
    a.poll()
    clock.t += 10_000
    d.i = 0
    a.poll()
    assert d.i == 0
