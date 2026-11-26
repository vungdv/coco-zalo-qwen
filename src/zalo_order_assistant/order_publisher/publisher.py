"""OrderPublisher: append one line to the customer's order through the Supabase `orders` Edge Function (spec §2.3,
FR-9, spec v2 §4.4).

POST {SUPABASE_URL}/functions/v1/orders with `x-api-key`. Each message is one immutable line (`op` = add, reduce, set
or cancel), idempotent on (source, external_id), and external_id is the Zalo message key: a retry with the same
content is answered 200 and never duplicates a line. `delivered` is never sent. `placed_at` is the Zalo message
time. `customer_id` is `zalo:<Zalo user id>` when the sender's id is known. The API key and the message text are
never logged.

409 answers carry a Problem Details `error`: `no_open_order` (a change with nothing open to change, nothing stored)
becomes OrderNotApplied; `idempotency_conflict` (the key was stored with other content) is a permanent failure.

Runs on its own worker thread: `publish()` returns at once and the result arrives as an event.
"""

from __future__ import annotations

import logging
import os
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass, field

import requests

from zalo_order_assistant.events import FailureKind, Op, Order, OrderNotApplied, OrderPublished, PublishFailed
from zalo_order_assistant.ports import Mediator

log = logging.getLogger(__name__)

SOURCE = "zalo"
MESSAGE_MAX_CHARS = 2000
PERMANENT_STATUSES = frozenset({400, 405, 422})
NO_OPEN_ORDER = "no_open_order"

PublishResult = OrderPublished | OrderNotApplied | PublishFailed


@dataclass(frozen=True)
class PublisherConfig:
    supabase_url: str
    api_key: str = field(repr=False)
    timeout_seconds: float = 30.0

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> PublisherConfig:
        e = os.environ if env is None else env
        missing = [k for k in ("SUPABASE_URL", "SUPABASE_API_KEY") if not e.get(k)]
        if missing:
            raise ValueError(f"{', '.join(missing)} required")
        return cls(
            supabase_url=e["SUPABASE_URL"].rstrip("/"),
            api_key=e["SUPABASE_API_KEY"],
            timeout_seconds=float(e.get("PUBLISH_TIMEOUT_SECONDS", "30")),
        )


def build_payload(order: Order) -> dict:
    payload: dict[str, object] = {
        "source": SOURCE,
        "external_id": order.key,
        "op": order.op.value,
        "customer_name": order.who,  # sent on every line: the latest name is shown, and it identifies a customer
        "delivery_date": order.date,
        "message": order.message[:MESSAGE_MAX_CHARS],
        # the message time, not the publish time: a backfilled or retried order keeps when it was placed
        "placed_at": order.placed_at.isoformat(timespec="seconds"),
    }
    if order.op is not Op.CANCEL:  # the backend rejects a quantity on cancel
        payload["quantity"] = order.qty
    if order.customer_id:
        # the stable Zalo user id: the backend keys the customer by it, so a renamed customer keeps one order
        payload["customer_id"] = f"{SOURCE}:{order.customer_id}"
    return payload


def classify(key: str, status: int, error: str | None = None) -> PublishResult:
    """Map an HTTP status (and a 409's Problem Details `error`) to the result event (spec §2.3, spec v2 §4.4).

    Nothing here is PER_MESSAGE.
    """
    if status in (200, 201):
        return OrderPublished(key)
    if status == 409:
        if error == NO_OPEN_ORDER:
            return OrderNotApplied(key, NO_OPEN_ORDER)
        return PublishFailed(key, FailureKind.PERMANENT, status, error or "conflict")
    if status in PERMANENT_STATUSES:
        return PublishFailed(key, FailureKind.PERMANENT, status, "rejected")
    if status in (401, 403):
        return PublishFailed(key, FailureKind.SYSTEMIC, status, "unauthorized")
    if status in (404, 408, 429) or status >= 500:
        # 404: the function is not deployed or the URL is wrong, so every message would fail the same way.
        return PublishFailed(key, FailureKind.SYSTEMIC, status, "unavailable")
    return PublishFailed(key, FailureKind.PERMANENT, status, "unexpected status")


_ERROR_CODE = frozenset({NO_OPEN_ORDER, "idempotency_conflict"})


def _problem_error(resp: requests.Response) -> str | None:
    """The `error` code of a Problem Details body, only when it is a known code (it goes to the audit log)."""
    try:
        body = resp.json()
    except ValueError:
        return None
    error = body.get("error") if isinstance(body, dict) else None
    return error if error in _ERROR_CODE else None


class SupabaseOrderPublisher:
    def __init__(
        self,
        mediator: Mediator,
        config: PublisherConfig,
        *,
        session: requests.Session | None = None,
        executor: Executor | None = None,
    ) -> None:
        self._mediator = mediator
        self._cfg = config
        self._http = session or requests.Session()
        self._pool = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="publisher")

    def publish(self, order: Order) -> None:
        self._pool.submit(self._run, order)

    def _run(self, order: Order) -> None:
        event: PublishResult
        try:
            event = self.publish_now(order)
        except Exception as exc:  # a bug must not lose the message: report it, the Coordinator retries
            log.exception("publisher_crashed key=%s error_type=%s", order.key, type(exc).__name__)
            event = PublishFailed(order.key, FailureKind.SYSTEMIC, None, type(exc).__name__)
        self._mediator.notify(event)

    def publish_now(self, order: Order) -> PublishResult:
        try:
            resp = self._http.post(
                f"{self._cfg.supabase_url}/functions/v1/orders",
                json=build_payload(order),
                headers={"x-api-key": self._cfg.api_key},
                timeout=self._cfg.timeout_seconds,
            )
        except requests.RequestException as exc:
            log.warning("order_publish_unreachable key=%s error_type=%s", order.key, type(exc).__name__)
            return PublishFailed(order.key, FailureKind.SYSTEMIC, None, type(exc).__name__)
        event = classify(order.key, resp.status_code, _problem_error(resp) if resp.status_code == 409 else None)
        op = order.op.value
        if isinstance(event, OrderPublished):
            log.info("order_published key=%s op=%s status=%d", order.key, op, resp.status_code)
        elif isinstance(event, OrderNotApplied):
            log.warning("order_not_applied key=%s op=%s date=%s reason=%s", order.key, op, order.date, event.reason)
        else:
            log.warning(
                "order_publish_failed key=%s op=%s status=%d kind=%s reason=%s",
                order.key,
                op,
                resp.status_code,
                event.kind,
                event.reason,
            )
        return event

    def close(self) -> None:
        if isinstance(self._pool, ThreadPoolExecutor):
            self._pool.shutdown(wait=False, cancel_futures=True)
