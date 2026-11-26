"""Manual check of the publisher against the real Supabase app (T3, spec v2 U3).

    uv run python scripts/check_publisher.py

Reads SUPABASE_URL and SUPABASE_API_KEY from .env. Sends, for a throw-away test customer and today's date:
add 3 -> reduce 1 -> set 4 -> cancel, then the first line again (a replay) and a reduce with nothing open. It prints
the order total after each step; expected: 3, 2, 4, 0, replay 200, then not applied. The test order ends cancelled
(listed muted at the bottom of today's list); delete it in the app afterwards.
"""

from __future__ import annotations

import time
from datetime import date, datetime

import requests
from dotenv import load_dotenv

from zalo_order_assistant.events import Op, Order
from zalo_order_assistant.order_publisher import PublisherConfig, build_payload
from zalo_order_assistant.order_publisher.publisher import classify

NAME = "T3 publisher check"


def main() -> None:
    load_dotenv()
    cfg = PublisherConfig.from_env()
    run = int(time.time())
    today = date.today().isoformat()
    http = requests.Session()

    def send(n: int, op: Op, qty: int | None, expect: str) -> Order:
        o = Order(
            f"manual-check/{run}/{n}",
            NAME,
            qty,
            today,
            f"{NAME}: {op.value} {qty or ''}, safe to delete",
            datetime.now().astimezone(),
            customer_id=f"check-{run}",
            op=op,
        )
        r = http.post(
            f"{cfg.supabase_url}/functions/v1/orders",
            json=build_payload(o),
            headers={"x-api-key": cfg.api_key},
            timeout=cfg.timeout_seconds,
        )
        body = r.json() if r.headers.get("content-type", "").endswith("json") else {}
        event = classify(o.key, r.status_code, body.get("error"))
        total = body.get("quantity", "-")
        print(
            f"{op.value:7} {qty if qty is not None else '':>2} -> {r.status_code} total={total:<3} "
            f"{type(event).__name__:16} (expected {expect})"
        )
        return o

    first = send(1, Op.ADD, 3, "201, total 3")
    send(2, Op.REDUCE, 1, "201, total 2")
    send(3, Op.SET, 4, "201, total 4")
    send(4, Op.CANCEL, None, "201, total 0: cancelled")
    r = http.post(
        f"{cfg.supabase_url}/functions/v1/orders",
        json=build_payload(first),
        headers={"x-api-key": cfg.api_key},
        timeout=cfg.timeout_seconds,
    )
    print(f"replay of the first line -> {r.status_code} (expected 200)")
    run += 1  # a new customer: nothing open
    send(5, Op.REDUCE, 1, "409 no_open_order -> OrderNotApplied")
    print(f"Delete the test order '{NAME}' of {today} in the app.")


if __name__ == "__main__":
    main()
