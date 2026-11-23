"""Coordinator: the mediator. All cross-component rules of spec §2.5 live here.

Single-threaded and event-driven: `notify` only puts an event on the inbox, and one loop handles one event at a time,
so the queue and the per-message state need no locks. Commands to colleagues return at once; their results come back
as events. Backoff is a timer that posts `RetryDue` to the same inbox, so the loop never sleeps.

Guarantees: one message at a time, oldest first; a message is acked only after its order is stored or after the
reason it produced none is recorded; per-message failures stop at `max_attempts`; systemic failures hold the queue,
retry without limit and are shown as the pipeline state.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from typing import Protocol

from zalo_order_assistant.events import (
    Event,
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
from zalo_order_assistant.ports import AuditLog, Extractor, MessageSource, Publisher

log = logging.getLogger(__name__)


class Cancellable(Protocol):
    def cancel(self) -> None: ...


Scheduler = Callable[[float, Callable[[], None]], Cancellable]


def thread_timer(delay: float, fn: Callable[[], None]) -> Cancellable:
    t = threading.Timer(delay, fn)
    t.daemon = True
    t.start()
    return t


class _Stop:
    pass


_STOP = _Stop()

# Audit statuses (FR-8)
SKIPPED = "skipped"
NOT_AN_ORDER = "not_an_order"
FAILED = "failed"
RETRY_CAP = "retry_cap"


class Coordinator:
    def __init__(
        self,
        audit: AuditLog,
        *,
        max_attempts: int = 5,
        include_own_messages: bool = False,
        base_backoff_seconds: float = 2.0,
        max_backoff_seconds: float = 60.0,
        repeat_state_seconds: float = 60.0,
        tick_seconds: float = 1.0,
        schedule: Scheduler = thread_timer,
        clock: Callable[[], float] = time.monotonic,
        on_status: Callable[[str], None] | None = None,
        remember_completed: int = 50_000,
    ) -> None:
        self._audit = audit
        self.max_attempts = max_attempts
        self.include_own_messages = include_own_messages
        self.base_backoff = base_backoff_seconds
        self.max_backoff = max_backoff_seconds
        self.repeat_state_seconds = repeat_state_seconds
        self.tick_seconds = tick_seconds
        self._schedule = schedule
        self._clock = clock
        self._on_status = on_status
        self._remember_completed = remember_completed

        self._source: MessageSource | None = None
        self._extractor: Extractor | None = None
        self._publisher: Publisher | None = None

        self._inbox: queue.Queue[Event | _Stop] = queue.Queue()
        self._pending: deque[Message] = deque()
        self._current: Message | None = None  # extracting, publishing, or waiting for its RetryDue
        self._order: Order | None = None  # the current message's order once extracted (a retry re-publishes it)
        self._waiting = False
        self._timer: Cancellable | None = None
        self._keys: set[str] = set()  # queued or in flight
        self._completed: OrderedDict[str, None] = OrderedDict()  # recently acked keys, so a late re-read is ignored
        self._attempts: dict[str, int] = {}
        self._systemic_failures = 0

        self.pipeline_state = PipelineState.OK
        self.reader_state: ReaderStatus | None = None
        self._state_logged_at = 0.0
        self.running = False

    # --- wiring and lifecycle ---------------------------------------------------------------------------------------

    def attach(self, source: MessageSource, extractor: Extractor, publisher: Publisher) -> None:
        self._source, self._extractor, self._publisher = source, extractor, publisher

    def notify(self, event: Event) -> None:
        """Mediator entry point. Thread-safe; only enqueues, never runs a handler inline."""
        self._inbox.put(event)

    def run(self) -> None:
        """Start the Reader and handle events until `stop()` or a fatal Reader state."""
        assert self._source is not None, "attach() the colleagues first"
        self.running = True
        self._emit_status()
        self._source.start()
        try:
            while self.running:
                try:
                    event = self._inbox.get(timeout=self.tick_seconds)
                except queue.Empty:
                    self._tick()
                    continue
                if isinstance(event, _Stop):
                    break
                self._dispatch(event)
                self._tick()
        finally:
            self.running = False
            self._cancel_timer()
            self._source.stop()
            log.info("coordinator_stopped queued=%d in_flight=%s", len(self._pending), self._current is not None)

    def stop(self) -> None:
        """Ask the loop to stop. Safe from any thread or a signal handler. Un-acked messages are re-read later."""
        self._inbox.put(_STOP)

    def drain(self) -> int:
        """Handle every event already in the inbox without blocking (for tests and replay). Returns how many."""
        n = 0
        while True:
            try:
                event = self._inbox.get_nowait()
            except queue.Empty:
                return n
            if isinstance(event, _Stop):
                return n
            self._dispatch(event)
            n += 1

    @property
    def queued(self) -> int:
        return len(self._pending) + (1 if self._current else 0)

    # --- event handling ---------------------------------------------------------------------------------------------

    def _dispatch(self, event: Event) -> None:
        match event:
            case MessageReceived(message=m):
                self._on_message(m)
            case ReaderState():
                self._on_reader_state(event)
            case OrderExtracted(order=o):
                if self._is_current(o.key, event):
                    self._order = o
                    self._publish(o)
            case NotAnOrder(key=k, reason=r):
                if self._is_current(k, event):
                    self._record(r, NOT_AN_ORDER)
                    self._complete()
            case OrderPublished(key=k):
                if self._is_current(k, event):
                    self._complete()
            case ExtractFailed(key=k, kind=kind, reason=r):
                if self._is_current(k, event):
                    self._on_failure(kind, f"extractor: {r}" if r else "extractor", PipelineState.EXTRACTOR_UNAVAILABLE)
            case PublishFailed(key=k, kind=kind, status=status, reason=r):
                if self._is_current(k, event):
                    state = (
                        PipelineState.PUBLISHER_UNAUTHORIZED
                        if status in (401, 403)
                        else PipelineState.PUBLISHER_UNAVAILABLE
                    )
                    detail = f"publisher {status if status is not None else 'network'}" + (f": {r}" if r else "")
                    self._on_failure(kind, detail, state)
            case RetryDue(key=k):
                self._on_retry_due(k)

    def _on_message(self, m: Message) -> None:
        if m.key in self._keys or m.key in self._completed:
            return
        if m.is_self and not self.include_own_messages:
            self._skip(m, "own message")
        elif not m.sender.strip():
            self._skip(m, "no sender name")
        else:
            self._pending.append(m)
            self._keys.add(m.key)
            self._pump()

    def _skip(self, m: Message, reason: str) -> None:
        log.info("message_skipped key=%s reason=%s", m.key, reason)
        self._audit.record(m.key, m.sent_at, reason, SKIPPED)
        self._ack(m.key)

    def _on_reader_state(self, event: ReaderState) -> None:
        if event.state != self.reader_state:
            self.reader_state = event.state
            level = logging.INFO if event.state is ReaderStatus.READY else logging.WARNING
            log.log(level, "reader_state state=%s", event.state.value)
            self._emit_status()
        if event.fatal:
            log.error("reader_gave_up state=%s: stopping", event.state.value)
            self.running = False
            self._inbox.put(_STOP)

    def _on_failure(self, kind: FailureKind, reason: str, systemic_state: PipelineState) -> None:
        assert self._current is not None
        key = self._current.key
        if kind is FailureKind.PERMANENT:
            log.warning("message_failed key=%s kind=PERMANENT reason=%s", key, reason)
            self._record(reason, FAILED)
            self._complete()
        elif kind is FailureKind.PER_MESSAGE:
            n = self._attempts[key] = self._attempts.get(key, 0) + 1
            if n >= self.max_attempts:
                log.warning("message_failed key=%s kind=PER_MESSAGE attempts=%d: giving up", key, n)
                self._record(reason, RETRY_CAP)
                self._complete()
            else:
                delay = self._backoff(n)
                log.warning("message_retry key=%s attempt=%d delay=%gs reason=%s", key, n, delay, reason)
                self._retry_later(delay)
        else:  # SYSTEMIC: not counted, not acked, retried without limit; the queue waits
            self._systemic_failures += 1
            self._set_pipeline(systemic_state)
            self._retry_later(self._backoff(self._systemic_failures))

    def _on_retry_due(self, key: str) -> None:
        if not self._waiting or self._current is None or self._current.key != key:
            return  # stale timer
        self._waiting = False
        self._timer = None
        if self._order is not None:
            self._publish(self._order)  # already extracted: only the publish is repeated
        else:
            self._extract(self._current)

    # --- helpers ----------------------------------------------------------------------------------------------------

    def _is_current(self, key: str, event: Event) -> bool:
        if self._current is not None and self._current.key == key and not self._waiting:
            return True
        log.warning("event_ignored type=%s key=%s (not the message in flight)", type(event).__name__, key)
        return False

    def _pump(self) -> None:
        if self._current is None and self._pending:
            self._current = self._pending.popleft()
            self._extract(self._current)

    def _extract(self, m: Message) -> None:
        assert self._extractor is not None
        self._extractor.extract(m)

    def _publish(self, o: Order) -> None:
        assert self._publisher is not None
        self._publisher.publish(o)

    def _record(self, reason: str, status: str) -> None:
        assert self._current is not None
        self._audit.record(self._current.key, self._current.sent_at, reason, status)

    def _ack(self, key: str) -> None:
        assert self._source is not None
        self._source.ack(key)
        self._completed[key] = None
        while len(self._completed) > self._remember_completed:
            self._completed.popitem(last=False)

    def _complete(self) -> None:
        """The current message is done (order stored, or the reason it has none is recorded): ack and move on."""
        assert self._current is not None
        key = self._current.key
        self._ack(key)
        self._keys.discard(key)
        self._attempts.pop(key, None)
        self._current, self._order = None, None
        self._systemic_failures = 0
        self._set_pipeline(PipelineState.OK)
        self._pump()

    def _backoff(self, n: int) -> float:
        return float(min(self.base_backoff * (2 ** (n - 1)), self.max_backoff))

    def _retry_later(self, delay: float) -> None:
        assert self._current is not None
        key = self._current.key
        self._waiting = True
        self._cancel_timer()
        self._timer = self._schedule(delay, lambda: self.notify(RetryDue(key)))

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _set_pipeline(self, state: PipelineState) -> None:
        if state == self.pipeline_state:
            return
        previous, self.pipeline_state = self.pipeline_state, state
        self._state_logged_at = self._clock()
        if state is PipelineState.OK:
            log.warning("pipeline_state state=OK (recovered from %s)", previous.value)
        else:
            log.error("pipeline_state state=%s: %s", state.value, _ACTION[state])
        self._emit_status()

    def _tick(self) -> None:
        """Repeat a non-OK pipeline state in the log every `repeat_state_seconds` while it lasts."""
        if self.pipeline_state is PipelineState.OK:
            return
        now = self._clock()
        if now - self._state_logged_at >= self.repeat_state_seconds:
            self._state_logged_at = now
            log.error(
                "pipeline_state state=%s still (retrying without limit): %s",
                self.pipeline_state.value,
                _ACTION[self.pipeline_state],
            )

    def _emit_status(self) -> None:
        if self._on_status is not None:
            reader = self.reader_state.value if self.reader_state else "STARTING"
            self._on_status(f"reader={reader} pipeline={self.pipeline_state.value} queued={self.queued}")


_ACTION = {
    PipelineState.OK: "",
    PipelineState.PUBLISHER_UNAUTHORIZED: "Supabase rejected the API key (401/403); fix SUPABASE_API_KEY",
    PipelineState.PUBLISHER_UNAVAILABLE: "Supabase is unreachable or overloaded; check the network and Supabase status",
    PipelineState.EXTRACTOR_UNAVAILABLE: "Ollama is not reachable or the model is missing; start Ollama",
}
