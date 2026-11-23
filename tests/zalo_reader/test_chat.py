"""T5b: search and open the configured group chat by title; never read any other chat."""

from __future__ import annotations

import pytest

from zalo_order_assistant.events import ReaderStatus
from zalo_order_assistant.zalo_reader.adapter import ZaloWebAdapter
from zalo_order_assistant.zalo_reader.browser import BrowserError
from zalo_order_assistant.zalo_reader.chat import (
    Candidate,
    ChatOpener,
    TargetedPoller,
    candidates_script,
    normalize_title,
    pick,
)
from zalo_order_assistant.zalo_reader.reader import load_selectors
from zalo_order_assistant.zalo_reader.store import AckedStore

SEL = load_selectors()
TARGET = "Bếp Phương Bắc - Đặt cơm"


def c(i, title, id="", group=False):
    return Candidate(index=i, title=title, id=id, group_marker=group)


def test_normalize_title():
    assert normalize_title("  Bếp  Phương BẮC - Đặt cơm ") == "bep phuong bac - dat com"
    assert normalize_title("BEP PHUONG BAC - DAT COM") == normalize_title(TARGET)


@pytest.mark.parametrize(
    "cands,reason,index",
    [
        ([c(0, "Mai"), c(1, TARGET, "g1")], "match", 1),
        ([c(0, "bep phuong bac - dat com ", "g1")], "match", 0),  # trimmed, no case, no diacritics
        ([c(0, TARGET, "123"), c(1, TARGET, "g9")], "match", 1),  # same-titled private chat is skipped
        ([c(0, TARGET, "", group=True)], "match", 0),  # group known by its marker
        ([c(0, TARGET, "123")], "private_only", None),
        ([c(0, TARGET + " 2", "g1"), c(1, "Bếp", "g2")], "partial_only", None),
        ([c(0, TARGET, "g1"), c(1, TARGET, "g2")], "ambiguous", None),
        ([c(0, TARGET, "g1"), c(3, TARGET, "g1")], "match", 0),  # the same group listed twice
        ([], "none", None),
    ],
)
def test_pick(cands, reason, index):
    p = pick(cands, TARGET)
    assert p.reason == reason and (p.candidate.index if p.candidate else None) == index


class FakeDriver:
    """Serves candidate lists for the conversation list and the search results; records real input."""

    def __init__(self, listed=(), found=(), mark_ok=True):
        self.listed, self.found, self.mark_ok = list(listed), list(found), mark_ok
        self.searched = ""
        self.actions: list[tuple] = []

    def eval_json(self, js):
        if "setAttribute" in js:
            self.actions.append(("mark",))
            return {"ok": self.mark_ok}
        scope = "list" if SEL["chat_list_item"] in js.split("querySelectorAll(")[2] else "search"
        items = self.listed if scope == "list" else (self.found if self.searched else [])
        return [{"index": i, "title": t, "id": id_, "group_marker": False} for i, (t, id_) in enumerate(items)]

    def fill(self, selector, text):
        self.actions.append(("fill", selector, text))
        self.searched = text

    def click(self, selector):
        self.actions.append(("click", selector))

    def press(self, key):
        self.actions.append(("press", key))


def opener(driver):
    return ChatOpener(driver, SEL, settle_seconds=0, sleep=lambda _: None)


def test_group_in_the_conversation_list_is_clicked_without_typing():
    d = FakeDriver(listed=[("Mai", "123"), (TARGET, "g5")])
    assert opener(d).open(TARGET)
    assert d.actions == [("mark",), ("click", '[data-zoa-target="1"]')]


def test_otherwise_the_search_box_is_used_then_cleared():
    d = FakeDriver(listed=[("Mai", "123")], found=[(TARGET, "123"), (TARGET, "g5")])
    assert opener(d).open(TARGET)
    assert d.actions == [
        ("fill", "#contact-search-input", TARGET),
        ("mark",),
        ("click", '[data-zoa-target="1"]'),
        ("fill", "#contact-search-input", ""),
        ("press", "Escape"),
    ]


@pytest.mark.parametrize(
    "found",
    [[], [(TARGET, "123")], [(TARGET + " cũ", "g1")], [(TARGET, "g1"), (TARGET, "g2")]],
    ids=["none", "private-only", "partial-only", "ambiguous"],
)
def test_nothing_is_opened_when_no_single_exact_group_matches(found):
    d = FakeDriver(found=found)
    assert not opener(d).open(TARGET)
    assert not any(a[0] == "click" for a in d.actions)
    assert d.actions[-2:] == [("fill", "#contact-search-input", ""), ("press", "Escape")]


def test_list_re_rendered_between_read_and_click():
    d = FakeDriver(listed=[(TARGET, "g5")], mark_ok=False)
    assert not opener(d).open(TARGET)
    assert not any(a[0] == "click" for a in d.actions)


def test_candidate_script_uses_the_selectors():
    js = candidates_script(SEL, "list")
    assert "#conversationList .msg-item" in js and "anim-data-id" in js


# --- TargetedPoller: the guard before any read -----------------------------------------------------------------------


class PageDriver:
    """The Zalo page: which chat is open, its header and its messages."""

    def __init__(self, state="READING", title=TARGET, is_group=True, key="g5"):
        self.state, self.title, self.is_group, self.key = state, title, is_group, key
        self.evals = 0

    def eval_json(self, js):
        self.evals += 1
        if self.state != "READING":
            return {"state": self.state}
        return {
            "state": "READING",
            "conversation": {"key": self.key, "title": self.title, "is_group": self.is_group},
            "messages": [
                {
                    "message_key": "1_u1",
                    "sent_at_ms": 1795400000000,
                    "sender_uid": "u1",
                    "sender": "An",
                    "kind": "text",
                    "text": "2 phần",
                }
            ],
        }


class FakeOpener:
    def __init__(self, page: PageDriver, result=True, error=None):
        self.page, self.result, self.error, self.calls = page, result, error, 0

    def open(self, target):
        self.calls += 1
        if self.error:
            raise self.error
        if self.result:
            self.page.state, self.page.title, self.page.is_group, self.page.key = "READING", target, True, "g5"
        return self.result


class Clock:
    t = 100.0

    def __call__(self):
        return self.t


def poller(page, target=TARGET, **kw):
    adapter = ZaloWebAdapter(page, AckedStore(":memory:"), "js")
    op = FakeOpener(page, **kw)
    clock = Clock()
    return TargetedPoller(adapter, op, target, clock=clock), op, clock  # type: ignore[arg-type]


def test_target_chat_already_open_is_read():
    p, op, _ = poller(PageDriver())
    res = p.poll()
    assert res.state is ReaderStatus.READY and [m.key for m in res.messages] == ["g5/1_u1"] and op.calls == 0


@pytest.mark.parametrize(
    "page",
    [
        PageDriver(title="Gia đình", key="g7"),  # another group
        PageDriver(title=TARGET, is_group=False, key="777"),  # a private chat with the same title
        PageDriver(state="NO_CHAT"),  # after a reload nothing is open
    ],
    ids=["other-group", "same-title-private", "nothing-open"],
)
def test_other_chat_is_never_read_the_target_is_opened_first(page):
    p, op, _ = poller(page)
    first = p.poll()
    assert first.messages == [] and op.calls == 1  # opened, nothing read from the other chat
    res = p.poll()
    assert res.state is ReaderStatus.READY and [m.key for m in res.messages] == ["g5/1_u1"]


def test_chat_not_found_keeps_retrying_slowly():
    page = PageDriver(title="Gia đình", key="g7")
    p, op, clock = poller(page, result=False)
    assert p.poll().state is ReaderStatus.CHAT_NOT_FOUND and op.calls == 1
    clock.t += 2
    assert p.poll().state is ReaderStatus.CHAT_NOT_FOUND and op.calls == 1  # not every poll
    clock.t += 30
    assert p.poll().state is ReaderStatus.CHAT_NOT_FOUND and op.calls == 2
    assert all(not r for r in [p.poll().messages])


def test_navigating_away_reopens_the_target():
    page = PageDriver()
    p, op, clock = poller(page)
    p.poll()
    page.title, page.key = "Gia đình", "g7"
    clock.t += 60
    assert p.poll().messages == [] and op.calls == 1
    assert p.poll().state is ReaderStatus.READY


def test_open_browser_error_is_browser_failed():
    p, _, _ = poller(PageDriver(state="NO_CHAT"), error=BrowserError("x"))
    assert p.poll().state is ReaderStatus.BROWSER_FAILED


def test_no_target_reads_the_open_chat_only_if_it_is_a_group():
    p, op, _ = poller(PageDriver(title="Mai", is_group=False, key="777"), target="")
    assert p.poll().state is ReaderStatus.NO_CHAT_OPEN and op.calls == 0
    p2, _, _ = poller(PageDriver(title="Any group"), target="")
    assert p2.poll().state is ReaderStatus.READY


def test_no_target_and_nothing_open_waits():
    p, op, _ = poller(PageDriver(state="NO_CHAT"), target="")
    assert p.poll().state is ReaderStatus.NO_CHAT_OPEN and op.calls == 0


def test_login_required_passes_through():
    p, op, _ = poller(PageDriver(state="LOGIN"))
    assert p.poll().state is ReaderStatus.LOGIN_REQUIRED and op.calls == 0


def test_a_click_that_does_not_open_the_chat_backs_off():
    page = PageDriver(state="NO_CHAT")

    class ClickNoEffect(FakeOpener):
        def open(self, target):
            self.calls += 1
            return True  # clicked, but the page does not change

    adapter = ZaloWebAdapter(page, AckedStore(":memory:"), "js")
    op, clock = ClickNoEffect(page), Clock()
    p = TargetedPoller(adapter, op, TARGET, clock=clock)  # type: ignore[arg-type]
    p.poll()
    clock.t += 6
    p.poll()  # second click: verify window passed
    clock.t += 6
    p.poll()
    assert op.calls == 2  # now waits the full 30 s
    clock.t += 30
    p.poll()
    assert op.calls == 3
