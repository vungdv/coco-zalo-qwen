"""OrderExtractor colleague: one message in, one of OrderExtracted / NotAnOrder / ExtractFailed out (spec §2.3).

Runs in-process on its own worker thread with the local model. Stateless: the message key is passed through so each
event names its message. Reasons are fixed phrases (they go to the audit log), never message text.
"""

from __future__ import annotations

import logging
import os
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime

from zalo_order_assistant.events import VN_TZ, ExtractFailed, FailureKind, Message, NotAnOrder, Order, OrderExtracted
from zalo_order_assistant.order_extractor.day_words import resolve
from zalo_order_assistant.order_extractor.llm import IGNORE_REASONS, Llm, LlmInvalid, LlmTimeout, LlmUnavailable
from zalo_order_assistant.order_extractor.validate import Rejected, parse_model_date, validate
from zalo_order_assistant.ports import Mediator

log = logging.getLogger(__name__)

EARLIEST_CREATED = datetime(2020, 1, 1, tzinfo=VN_TZ)
MESSAGE_MAX_CHARS = 2000

ExtractResult = OrderExtracted | NotAnOrder | ExtractFailed


@dataclass(frozen=True)
class ExtractorConfig:
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:7b-instruct"
    timeout_seconds: float = 20.0
    max_days_ahead: int = 31

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> ExtractorConfig:
        e = os.environ if env is None else env
        d = cls()
        return cls(
            ollama_url=e.get("OLLAMA_URL") or d.ollama_url,
            ollama_model=e.get("OLLAMA_MODEL") or d.ollama_model,
            timeout_seconds=float(e.get("LLM_TIMEOUT_SECONDS") or d.timeout_seconds),
            max_days_ahead=int(e.get("MAX_DAYS_AHEAD") or d.max_days_ahead),
        )


class OrderExtractor:
    def __init__(
        self, mediator: Mediator, llm: Llm, *, max_days_ahead: int = 31, executor: Executor | None = None
    ) -> None:
        self._mediator = mediator
        self._llm = llm
        self.max_days_ahead = max_days_ahead
        self._pool = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="extractor")

    def extract(self, message: Message) -> None:
        self._pool.submit(self._run, message)

    def _run(self, message: Message) -> None:
        event: ExtractResult
        try:
            event = self.extract_now(message)
        except Exception as exc:  # a bug must not lose the message: count it as a per-message failure
            log.exception("extractor_crashed key=%s error_type=%s", message.key, type(exc).__name__)
            event = ExtractFailed(message.key, FailureKind.PER_MESSAGE, f"crash {type(exc).__name__}")
        self._mediator.notify(event)

    def extract_now(self, message: Message) -> ExtractResult:
        key = message.key
        sent_at = message.sent_at
        if sent_at is None or sent_at.tzinfo is None or sent_at < EARLIEST_CREATED:
            return ExtractFailed(key, FailureKind.PERMANENT, "invalid created date")
        created = sent_at.astimezone(VN_TZ).date()
        who = message.sender.strip()
        if not who:
            return NotAnOrder(key, "no sender name")
        day = resolve(message.text, created, self.max_days_ahead)
        if day.reason:  # past day, invalid date, several days, beyond the horizon: no need to ask the model
            return NotAnOrder(key, day.reason)
        try:
            answer = self._llm.classify(message.text, created)
        except LlmUnavailable as e:
            log.warning("extract_failed key=%s kind=SYSTEMIC reason=%s", key, e)
            return ExtractFailed(key, FailureKind.SYSTEMIC, str(e))
        except LlmTimeout as e:
            log.warning("extract_failed key=%s kind=PER_MESSAGE reason=%s", key, e)
            return ExtractFailed(key, FailureKind.PER_MESSAGE, str(e))
        except LlmInvalid as e:
            log.warning("extract_failed key=%s kind=PERMANENT reason=%s", key, e)
            return ExtractFailed(key, FailureKind.PERMANENT, f"invalid output: {e}")
        if not answer.is_order:
            reason = answer.args.get("reason")
            return NotAnOrder(key, f"model: {reason if reason in IGNORE_REASONS else 'other'}")
        v = validate(answer.args)
        if isinstance(v, Rejected):
            return NotAnOrder(key, v.reason)
        when = day.date or created  # FR-6b: no day word -> the created date
        model_day = parse_model_date(answer.args.get("date"))
        if model_day is not None and model_day != when:  # the model invented or miscounted a day: resolver wins
            model_text = model_day.isoformat() if isinstance(model_day, date) else model_day
            log.info("date_corrected key=%s model=%s resolved=%s", key, model_text, when.isoformat())
        log.info("order_extracted key=%s qty=%d date=%s", key, v.qty, when.isoformat())
        return OrderExtracted(
            Order(
                key,
                who,
                v.qty,
                when.isoformat(),
                message.text[:MESSAGE_MAX_CHARS],
                sent_at,
                customer_id=message.sender_id,
            )
        )

    def close(self) -> None:
        if isinstance(self._pool, ThreadPoolExecutor):
            self._pool.shutdown(wait=False, cancel_futures=True)
