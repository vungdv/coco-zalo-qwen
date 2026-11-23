"""OrderPublisher: create or update the order through the Supabase `orders` Edge Function (spec §2.3, FR-9).

POST {SUPABASE_URL}/functions/v1/orders with `x-api-key`. The function is an idempotent upsert on
(source, external_id) and external_id is the Zalo message key, so a retry never duplicates an order. `delivered` is
never sent, so an update never resets it (Supabase S18). The API key and the message text are never logged.

Runs on its own worker thread: `publish()` returns at once and the result arrives as an event.
"""

from __future__ import annotations

import logging
import os
from concurrent.futures import Executor, ThreadPoolExecutor
from dataclasses import dataclass, field

import requests

from zalo_order_assistant.events import FailureKind, Order, OrderPublished, PublishFailed
from zalo_order_assistant.ports import Mediator

log = logging.getLogger(__name__)

SOURCE = "zalo"
MESSAGE_MAX_CHARS = 2000
PERMANENT_STATUSES = frozenset({400, 405, 422})


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
    return {
        "source": SOURCE,
        "external_id": order.key,
        "customer_name": order.who,
        "quantity": order.qty,
        "delivery_date": order.date,
        "message": order.message[:MESSAGE_MAX_CHARS],
    }


def classify(key: str, status: int) -> OrderPublished | PublishFailed:
    """Map an HTTP status to the result event (spec §2.3). Nothing here is PER_MESSAGE."""
    if status in (200, 201):
        return OrderPublished(key)
    if status in PERMANENT_STATUSES:
        return PublishFailed(key, FailureKind.PERMANENT, status, "rejected")
    if status in (401, 403):
        return PublishFailed(key, FailureKind.SYSTEMIC, status, "unauthorized")
    if status in (404, 408, 429) or status >= 500:
        # 404: the function is not deployed or the URL is wrong, so every message would fail the same way.
        return PublishFailed(key, FailureKind.SYSTEMIC, status, "unavailable")
    return PublishFailed(key, FailureKind.PERMANENT, status, "unexpected status")


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
        event: OrderPublished | PublishFailed
        try:
            event = self.publish_now(order)
        except Exception as exc:  # a bug must not lose the message: report it, the Coordinator retries
            log.exception("publisher_crashed key=%s error_type=%s", order.key, type(exc).__name__)
            event = PublishFailed(order.key, FailureKind.SYSTEMIC, None, type(exc).__name__)
        self._mediator.notify(event)

    def publish_now(self, order: Order) -> OrderPublished | PublishFailed:
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
        event = classify(order.key, resp.status_code)
        if isinstance(event, OrderPublished):
            log.info("order_published key=%s status=%d", order.key, resp.status_code)
        else:
            log.warning("order_publish_failed key=%s status=%d kind=%s", order.key, resp.status_code, event.kind)
        return event

    def close(self) -> None:
        if isinstance(self._pool, ThreadPoolExecutor):
            self._pool.shutdown(wait=False, cancel_futures=True)
