"""One command at a time: claim, understand, find the customer, apply, report (spec v3 §5.1, V9, V10)."""

from __future__ import annotations

import logging
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from zalo_order_assistant.events import FailureKind, Op, Order, OrderNotApplied, OrderPublished, PublishFailed
from zalo_order_assistant.order_extractor.llm import LlmTimeout, LlmUnavailable
from zalo_order_assistant.voice_commands.apply import add_instead, done, plan
from zalo_order_assistant.voice_commands.client import ApiError
from zalo_order_assistant.voice_commands.model import Command, Failed
from zalo_order_assistant.voice_commands.names import resolve_customer
from zalo_order_assistant.voice_commands.ports import CommandSource, LinePublisher, Parser

log = logging.getLogger(__name__)
T = TypeVar("T")
BASE_BACKOFF_SECONDS = 2.0


class _Stopped(Exception):
    pass


@dataclass(frozen=True)
class WorkerConfig:
    poll_seconds: float = 2.0
    max_attempts: int = 5
    max_backoff_seconds: float = 60.0


def _traceparent() -> str:
    return f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-01"


class VoiceWorker:
    def __init__(
        self,
        source: CommandSource,
        parser: Parser,
        publisher: LinePublisher,
        config: WorkerConfig | None = None,
        *,
        on_status: Callable[[str], None] | None = None,
        wait: Callable[[float], bool] | None = None,
    ) -> None:
        self._source, self._parser, self._publisher = source, parser, publisher
        self._cfg = config or WorkerConfig()
        self._on_status = on_status
        self._stop = threading.Event()
        self._wait = wait or self._stop.wait  # returns True when asked to stop
        self._state = ""
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, name="voice", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def run(self) -> None:
        try:
            while not self._stop.is_set():
                if not self.run_once() and self._wait(self._cfg.poll_seconds):
                    break
        except _Stopped:
            pass  # a held command stays `processing`; its claim runs out and it is claimed again (V9)

    def run_once(self) -> bool:
        """Claim and finish one command. False when there was nothing to do."""
        traceparent = _traceparent()
        try:
            command = self._until_up(lambda: self._source.claim(traceparent))
        except ApiError as e:  # permanent: nothing to hold, try again at the next poll
            log.error("voice_claim_failed reason=%s status=%s", e.reason, e.status)
            return False
        self._set_state("OK")
        if command is None:
            return False
        trace_id = traceparent.split("-")[1]
        started = time.monotonic()
        try:
            status, result, op = self._handle(command, traceparent)
        except _Stopped:
            raise
        except Exception as exc:  # a bug must not leave the chef on "working" (V9)
            log.error("voice_crashed id=%s error_type=%s trace_id=%s", command.id, type(exc).__name__, trace_id)
            status, result, op = "failed", Failed("assistant_error", "crash").result(), None
        self._report(command, status, result, traceparent, trace_id)
        log.info(
            "voice_command id=%s op=%s outcome=%s error=%s latency_ms=%d trace_id=%s",
            command.id, op, status, result.get("error"), (time.monotonic() - started) * 1000, trace_id,
        )
        return True

    def _handle(self, command: Command, traceparent: str) -> tuple[str, dict, str | None]:
        parsed = self._parse(command)
        if isinstance(parsed, Failed):
            return "failed", parsed.result(), None
        order = plan(command, parsed, resolve_customer(parsed.customer, command.orders))
        if isinstance(order, Failed):
            return "failed", order.result(), parsed.op
        event = self._publish(order, traceparent)
        # V8: nothing open, or a replay of a run that already fell back to add
        if parsed.op == "total" and order.op is Op.SET and (
            isinstance(event, OrderNotApplied)
            or (isinstance(event, PublishFailed) and event.reason == "idempotency_conflict")
        ):
            event = self._publish(add_instead(order), traceparent)
        if isinstance(event, OrderPublished):
            return "done", done(event.body or {}), parsed.op
        if isinstance(event, OrderNotApplied):
            return "failed", Failed("no_open_order").result(), parsed.op
        return "failed", Failed("order_api_error", f"{event.status} {event.reason}").result(), parsed.op

    def _parse(self, command: Command):
        attempts = unavailable = 0
        while True:
            try:
                return self._parser.parse(command)
            except LlmUnavailable:
                self._set_state("EXTRACTOR_UNAVAILABLE")
                unavailable += 1
                self._back_off(unavailable)
            except LlmTimeout:
                attempts += 1
                if attempts >= self._cfg.max_attempts:
                    return Failed("assistant_error", "model timeout")
                self._back_off(attempts)

    def _publish(self, order: Order, traceparent: str):
        tries = 0
        while True:
            event = self._publisher.publish_now(order, traceparent)
            if not (isinstance(event, PublishFailed) and event.kind is FailureKind.SYSTEMIC):
                self._set_state("OK")
                return event
            self._set_state(_publisher_state(event.status))
            tries += 1
            self._back_off(tries)

    def _until_up(self, call: Callable[[], T]) -> T:
        tries = 0
        while True:
            try:
                return call()
            except ApiError as e:
                if e.kind is not FailureKind.SYSTEMIC:
                    raise
                self._set_state(_publisher_state(e.status))
                tries += 1
                self._back_off(tries)

    def _report(self, command: Command, status: str, result: dict, traceparent: str, trace_id: str) -> None:
        try:
            if not self._until_up(lambda: self._source.finish(command.id, status, result, traceparent)):
                log.warning("voice_report_dropped id=%s status=409 trace_id=%s", command.id, trace_id)
        except ApiError as e:
            log.error("voice_report_failed id=%s status=%s trace_id=%s", command.id, e.status, trace_id)

    def _back_off(self, attempt: int) -> None:
        delay = min(BASE_BACKOFF_SECONDS * 2 ** (attempt - 1), self._cfg.max_backoff_seconds)
        if self._wait(delay):
            raise _Stopped

    def _set_state(self, state: str) -> None:
        if state != self._state:
            self._state = state
            if self._on_status:
                self._on_status(f"voice={state}")


def _publisher_state(status: int | None) -> str:
    return "PUBLISHER_UNAUTHORIZED" if status in (401, 403) else "PUBLISHER_UNAVAILABLE"
