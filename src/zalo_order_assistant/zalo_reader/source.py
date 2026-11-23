"""ZaloReader colleague: polls Zalo Web on its own thread and pushes each new text message to the Coordinator.

Emits `MessageReceived` once per message per run (the Coordinator holds it until it is acked; a restart re-reads
everything not acked) and `ReaderState` on every state change. Gives up, with a fatal `ReaderState`, only when the
browser cannot be relaunched.
"""

from __future__ import annotations

import logging
import threading
from typing import Protocol

from zalo_order_assistant.events import MessageReceived, ReaderState, ReaderStatus
from zalo_order_assistant.ports import Mediator
from zalo_order_assistant.zalo_reader.adapter import PollResult
from zalo_order_assistant.zalo_reader.browser import BrowserError, Driver

log = logging.getLogger(__name__)


class Poller(Protocol):
    def poll(self) -> PollResult: ...
    def ack(self, key: str) -> None: ...


class ZaloReader:
    def __init__(self, mediator: Mediator, poller: Poller, driver: Driver, *, poll_interval_seconds: float) -> None:
        self._mediator = mediator
        self._poller = poller
        self._driver = driver
        self.poll_interval = poll_interval_seconds
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._emitted: set[str] = set()
        self._state: ReaderStatus | None = None

    # --- MessageSource ----------------------------------------------------------------------------------------------

    def start(self) -> None:
        self._stopping.clear()
        self._thread = threading.Thread(target=self._loop, name="zalo-reader", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=15)

    def ack(self, key: str) -> None:
        self._poller.ack(key)

    # --- loop -------------------------------------------------------------------------------------------------------

    def _loop(self) -> None:
        try:
            self._open_browser()
            while not self._stopping.is_set():
                if not self.run_once():
                    return
                self._stopping.wait(self.poll_interval)
        except Exception as exc:  # never die silently: tell the Coordinator
            log.exception("reader_crashed error_type=%s", type(exc).__name__)
            self._mediator.notify(ReaderState(ReaderStatus.BROWSER_FAILED, fatal=True))

    def _open_browser(self) -> None:
        try:
            self._driver.ensure_open()
        except BrowserError as exc:
            log.error("browser_open_failed error=%s", exc)

    def run_once(self) -> bool:
        """One poll. False when the Reader gave up (browser restarts exhausted)."""
        result = self._poller.poll()
        self._report(result.state)
        if result.state is ReaderStatus.BROWSER_FAILED:
            if not self._driver.relaunch():
                self._mediator.notify(ReaderState(ReaderStatus.BROWSER_FAILED, fatal=True))
                return False
            return True
        for m in result.messages:  # oldest first
            if m.key not in self._emitted:
                self._emitted.add(m.key)
                self._mediator.notify(MessageReceived(m))
        return True

    def _report(self, state: ReaderStatus) -> None:
        if state != self._state:
            self._state = state
            self._mediator.notify(ReaderState(state))
