"""Page-state machine over Zalo Web: read the open chat, backfill its history, map bubbles to `Message`s.

Ported from the zalo-adater spike (`adapter.py`). Changes for the push design:
- the history of a chat is backfilled once each time the chat is opened, not again on every poll until all of it is
  acked: the Coordinator holds what was emitted, and a restart backfills again;
- `ack(key)` replaces `mark_published(keys)`;
- a sender with no display name is "" (FR-4) instead of the numeric uid.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from pydantic import ValidationError

from zalo_order_assistant.events import VN_TZ, Message, ReaderStatus
from zalo_order_assistant.zalo_reader.browser import BrowserError, Driver
from zalo_order_assistant.zalo_reader.reader import Conversation, PageState, ReaderMessage
from zalo_order_assistant.zalo_reader.store import AckedStore

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PollResult:
    state: ReaderStatus
    messages: list[Message] = field(default_factory=list)


class ZaloWebAdapter:
    def __init__(
        self,
        driver: Driver,
        store: AckedStore,
        reader_script: str,
        *,
        scroll_script: Callable[[int | None], str] | None = None,
        backfill_max_steps: int = 500,
        backfill_days: float = 3.0,  # only read this far back; 0 = the whole history
        clock: Callable[[], float] = time.time,
        settle_seconds: float = 0.6,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.driver = driver
        self.store = store
        self.reader_script = reader_script
        self.scroll_script = scroll_script  # None disables backfill (only the visible messages are read)
        self.backfill_max_steps = backfill_max_steps
        self.backfill_days = backfill_days
        self.clock = clock
        self.settle_seconds = settle_seconds
        self.sleep = sleep
        self._synced: str | None = None  # conversation key whose history has been read since it was opened

    def read_page(self) -> tuple[PageState | None, PollResult | None]:
        """Read the page once. Returns (page, None) when a chat is readable, else (None, the state to report)."""
        try:
            value = self.driver.eval_json(self.reader_script)
        except BrowserError as exc:
            return None, PollResult(self._browser_failed(exc))
        try:
            page = PageState.model_validate(value)
        except ValidationError as exc:
            log.warning("selector_drift reason=unexpected_shape errors=%d", exc.error_count())
            return None, PollResult(ReaderStatus.SELECTOR_DRIFT)
        if page.state == "LOGIN":
            return None, PollResult(ReaderStatus.LOGIN_REQUIRED)
        if page.state == "ERROR" or page.drift:
            log.warning("selector_drift reason=%s", page.drift or "reader_error")
            return None, PollResult(ReaderStatus.SELECTOR_DRIFT)
        if page.state == "NO_CHAT":
            if page.rendered_bubbles > 0:  # bubbles render but no chat was recognised
                log.warning("selector_drift reason=bubbles_without_chat bubbles=%d", page.rendered_bubbles)
                return None, PollResult(ReaderStatus.SELECTOR_DRIFT)
            return None, PollResult(ReaderStatus.NO_CHAT_OPEN)
        if page.conversation is None:
            log.warning("selector_drift reason=no_conversation_key")
            return None, PollResult(ReaderStatus.SELECTOR_DRIFT)
        if not page.messages and page.rendered_bubbles > 0:
            log.warning("selector_drift reason=bubbles_not_parsed bubbles=%d", page.rendered_bubbles)
            return None, PollResult(ReaderStatus.SELECTOR_DRIFT)
        return page, None

    def poll(self) -> PollResult:
        page, failure = self.read_page()
        if failure is not None:
            return failure
        assert page is not None and page.conversation is not None
        return self.poll_page(page)

    def poll_page(self, page: PageState) -> PollResult:
        """Messages of an already-read page, with the chat's history the first time the chat is seen."""
        assert page.conversation is not None
        conv = page.conversation
        raws: list[ReaderMessage] = list(page.messages)
        senders = dict(page.senders)
        if self.scroll_script is not None and conv.key != self._synced:
            log.info("conversation_opened key=%s: reading %g days of history", conv.key, self.backfill_days)
            try:
                full, senders = self._backfill(page)
            except BrowserError as exc:
                return PollResult(self._browser_failed(exc))
            if full is None:  # the user switched chat while reading; start over on the next poll
                return PollResult(ReaderStatus.READY)
            raws = full
            self._synced = conv.key
        return PollResult(ReaderStatus.READY, self._to_messages(conv, senders, raws))

    def _backfill(self, first: PageState) -> tuple[list[ReaderMessage] | None, dict[str, str]]:
        """Scroll up to the first message, collecting every bubble the virtualized list mounts on the way."""
        assert self.scroll_script is not None and first.conversation is not None
        key = first.conversation.key
        collected = {m.message_key: m for m in first.messages}
        senders = dict(first.senders)
        step_px = -max(int(first.client_height * 0.8), 200)
        cutoff_ms = int((self.clock() - self.backfill_days * 86400) * 1000) if self.backfill_days > 0 else 0
        stalls = 0
        try:
            for _ in range(self.backfill_max_steps):
                self.driver.eval_json(self.scroll_script(step_px))
                self.sleep(self.settle_seconds)
                page, _ = self.read_page()
                if page is None or page.conversation is None or page.conversation.key != key:
                    log.info(
                        "backfill_aborted key=%s reason=%s", key, "unreadable" if page is None else "chat_switched"
                    )
                    return None, {}
                before = len(collected)
                collected.update({m.message_key: m for m in page.messages})
                senders.update(page.senders)
                at_top = page.scroll_top <= 1
                stalls = stalls + 1 if at_top and len(collected) == before else 0
                known = [m.sent_at_ms for m in collected.values() if m.sent_at_ms > 0]
                if cutoff_ms and known and min(known) < cutoff_ms:  # reached messages older than the window
                    break
                if stalls >= 2:  # at the top and nothing more loaded after waiting
                    break
            else:
                log.warning("backfill_step_limit key=%s steps=%d", key, self.backfill_max_steps)
        finally:
            try:
                self.driver.eval_json(self.scroll_script(None))  # leave the chat at the newest message
            except BrowserError as exc:
                log.warning("backfill_scroll_back_failed error=%s", exc)
        # messages without a timestamp (0) are kept: their age is unknown
        return [m for m in collected.values() if m.sent_at_ms == 0 or m.sent_at_ms >= cutoff_ms], senders

    def _to_messages(self, conv: Conversation, senders: dict[str, str], raws: list[ReaderMessage]) -> list[Message]:
        title = conv.title or conv.key
        out: list[Message] = []
        for raw in sorted(raws, key=lambda m: m.sent_at_ms):
            key = f"{conv.key}/{raw.message_key}"
            if raw.kind != "text" or not raw.text or self.store.contains(key):
                continue
            sender = (
                raw.sender
                or senders.get(raw.sender_uid)
                or (None if raw.is_self or conv.is_group else conv.title)
                or ""  # only a numeric uid is known: the Coordinator skips and records it (FR-4)
            )
            out.append(
                Message(
                    key=key,
                    chat=title,
                    sender=sender,
                    is_self=raw.is_self,
                    text=raw.text,
                    sent_at=datetime.fromtimestamp(raw.sent_at_ms / 1000, VN_TZ),
                )
            )
        return out

    def ack(self, key: str) -> None:
        self.store.add([key])

    def _browser_failed(self, exc: BrowserError) -> ReaderStatus:
        log.error("browser_failed error=%s", exc)
        return ReaderStatus.BROWSER_FAILED
