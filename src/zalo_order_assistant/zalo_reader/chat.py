"""Find the configured group chat by title and open it, and never read any other chat (spec §2.3, T5b).

The title match is exact after trimming, ignoring case and diacritics, and only a group is accepted: a private chat
with the same title, a partial match, or two groups with the same title give CHAT_NOT_FOUND, and nothing is opened.
The chat is checked before every read, so after a reload or a navigation away the Reader opens it again before it
reads (and backfills) anything.

Search order: the visible conversation list first (no typing), then the search box. JS only lists the candidates and
tags the chosen one; the match is decided here, and the click and typing are real browser input (playwright-cli).
Chat titles are never logged: they name the operator's other conversations.
"""

from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

from zalo_order_assistant.events import ReaderStatus
from zalo_order_assistant.zalo_reader.adapter import PollResult, ZaloWebAdapter
from zalo_order_assistant.zalo_reader.browser import BrowserError

log = logging.getLogger(__name__)

TARGET_ATTR = "data-zoa-target"


def normalize_title(s: str) -> str:
    s = unicodedata.normalize("NFD", s.replace("đ", "d").replace("Đ", "D"))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s).strip().casefold()


class Candidate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    index: int
    title: str = ""
    id: str = ""
    group_marker: bool = False

    @property
    def is_group(self) -> bool:
        return self.id.startswith("g") or self.group_marker


@dataclass(frozen=True)
class Pick:
    candidate: Candidate | None
    reason: str  # "match", "none", "partial_only", "private_only", "ambiguous"


def pick(candidates: list[Candidate], target: str) -> Pick:
    want = normalize_title(target)
    exact = [c for c in candidates if normalize_title(c.title) == want]
    groups = [c for c in exact if c.is_group]
    if len({c.id or c.index for c in groups}) > 1:
        return Pick(None, "ambiguous")
    if groups:
        return Pick(groups[0], "match")
    if exact:
        return Pick(None, "private_only")
    if any(want in normalize_title(c.title) for c in candidates):
        return Pick(None, "partial_only")
    return Pick(None, "none")


class InputDriver(Protocol):
    def eval_json(self, js: str) -> object: ...
    def fill(self, selector: str, text: str) -> None: ...
    def click(self, selector: str) -> None: ...
    def press(self, key: str) -> None: ...


def candidates_script(selectors: dict[str, str], scope: str) -> str:
    item = selectors["chat_list_item"] if scope == "list" else selectors["search_result_item"]
    return (
        "() => {\n"
        f"const SEL = {json.dumps(selectors, ensure_ascii=False)};\n"
        f"document.querySelectorAll('[{TARGET_ATTR}]').forEach(e => e.removeAttribute('{TARGET_ATTR}'));\n"
        f"const items = Array.from(document.querySelectorAll({json.dumps(item)}));\n"
        "const textOf = (n) => (n ? (n.innerText || n.textContent || '').trim() : '');\n"
        "return JSON.stringify(items.map((e, i) => ({index: i,\n"
        "  title: textOf(e.querySelector(SEL.conv_item_title)) || e.getAttribute('title') || '',\n"
        "  id: e.getAttribute(SEL.conv_item_id_attr) || '',\n"
        "  group_marker: !!e.querySelector(SEL.conv_item_group_marker)})));\n"
        "}"
    )


def mark_script(selectors: dict[str, str], scope: str, index: int, title: str) -> str:
    """Tag the chosen candidate for a real click, if it is still the same item (the list may have re-rendered)."""
    item = selectors["chat_list_item"] if scope == "list" else selectors["search_result_item"]
    return (
        "() => {\n"
        f"const SEL = {json.dumps(selectors, ensure_ascii=False)};\n"
        f"const e = Array.from(document.querySelectorAll({json.dumps(item)}))[{int(index)}];\n"
        "const textOf = (n) => (n ? (n.innerText || n.textContent || '').trim() : '');\n"
        "const t = e ? (textOf(e.querySelector(SEL.conv_item_title)) || e.getAttribute('title') || '') : null;\n"
        f"if (t !== {json.dumps(title, ensure_ascii=False)}) return JSON.stringify({{ok: false}});\n"
        f"e.setAttribute('{TARGET_ATTR}', '1');\n"
        "return JSON.stringify({ok: true});\n"
        "}"
    )


class ChatOpener:
    def __init__(
        self,
        driver: InputDriver,
        selectors: dict[str, str],
        *,
        settle_seconds: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.driver = driver
        self.sel = selectors
        self.settle_seconds = settle_seconds
        self.sleep = sleep

    def open(self, target: str) -> bool:
        """Open the group chat titled `target`. True when it was clicked; the next read verifies the header."""
        if self._try("list", target):
            return True
        self.driver.fill(self.sel["search_input"], target)
        self.sleep(self.settle_seconds)
        try:
            return self._try("search", target)
        finally:
            self._clear_search()

    def _try(self, scope: str, target: str) -> bool:
        raw = self.driver.eval_json(candidates_script(self.sel, scope))
        try:
            cands = [Candidate.model_validate(c) for c in raw] if isinstance(raw, list) else []
        except ValidationError:
            cands = []
        p = pick(cands, target)
        log.info("chat_search scope=%s candidates=%d result=%s", scope, len(cands), p.reason)
        if p.candidate is None:
            return False
        marked = self.driver.eval_json(mark_script(self.sel, scope, p.candidate.index, p.candidate.title))
        if not (isinstance(marked, dict) and marked.get("ok")):
            log.info("chat_search scope=%s result=list_changed", scope)
            return False
        self.driver.click(f'[{TARGET_ATTR}="1"]')
        self.sleep(self.settle_seconds)
        return True

    def _clear_search(self) -> None:
        try:
            self.driver.fill(self.sel["search_input"], "")
            self.driver.press("Escape")
        except BrowserError as exc:
            log.warning("chat_search_clear_failed error=%s", exc)


class TargetedPoller:
    """Wraps the adapter: reads only the configured group chat (or, with no title set, only a group)."""

    def __init__(
        self,
        adapter: ZaloWebAdapter,
        opener: ChatOpener,
        target: str,
        *,
        retry_open_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.adapter = adapter
        self.opener = opener
        self.target = target.strip()
        self.retry_open_seconds = retry_open_seconds
        self.clock = clock
        self._next_open_at = 0.0
        self._last_open_failed = False
        self._clicked = False  # the last attempt clicked the chat; not yet seen open

    def ack(self, key: str) -> None:
        self.adapter.ack(key)

    def poll(self) -> PollResult:
        page, failure = self.adapter.read_page()
        if failure is not None:
            if failure.state is ReaderStatus.NO_CHAT_OPEN and self.target:
                return self._open()
            return failure
        assert page is not None and page.conversation is not None
        conv = page.conversation
        if not self.target:
            if not conv.is_group:
                log.warning("open_chat_not_a_group: open a group chat or set ZALO_CHAT")
                return PollResult(ReaderStatus.NO_CHAT_OPEN)
            return self.adapter.poll_page(page)
        if conv.is_group and normalize_title(conv.title or "") == normalize_title(self.target):
            self._next_open_at, self._last_open_failed, self._clicked = 0.0, False, False
            return self.adapter.poll_page(page)
        return self._open()

    def _open(self) -> PollResult:
        now = self.clock()
        if now < self._next_open_at:  # do not type into the search box on every poll
            return PollResult(ReaderStatus.CHAT_NOT_FOUND if self._last_open_failed else ReaderStatus.NO_CHAT_OPEN)
        self._next_open_at = now + self.retry_open_seconds
        try:
            opened = self.opener.open(self.target)
        except BrowserError as exc:
            log.error("chat_open_failed error=%s", exc)
            return PollResult(ReaderStatus.BROWSER_FAILED)
        self._last_open_failed = not opened
        if not opened:
            self._clicked = False
            return PollResult(ReaderStatus.CHAT_NOT_FOUND)
        if self._clicked:  # the previous click did not open it either: back off to the normal interval
            log.warning("chat_open_unverified: clicked but the chat header does not match")
        else:
            self._next_open_at = now + min(self.retry_open_seconds, 5.0)  # verify on the next poll
        self._clicked = True
        return PollResult(ReaderStatus.NO_CHAT_OPEN)
