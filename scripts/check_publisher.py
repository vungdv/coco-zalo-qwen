"""Manual T3 check against the real Supabase app: create one test order, then update it.

    uv run python scripts/check_publisher.py

Reads SUPABASE_URL and SUPABASE_API_KEY from .env. After the first call, open the web app, find the order for
"T3 publisher check" and tick it delivered, then press Enter: the second call updates the quantity and the
order must stay delivered. Delete the test order in the app afterwards.
"""

from __future__ import annotations

import time
from datetime import date

from dotenv import load_dotenv

from zalo_order_assistant.events import Order
from zalo_order_assistant.order_publisher import PublisherConfig, SupabaseOrderPublisher


class _Print:
    def notify(self, e) -> None:  # unused: publish_now is synchronous
        print(e)


def main() -> None:
    load_dotenv()
    p = SupabaseOrderPublisher(_Print(), PublisherConfig.from_env())
    key = f"manual-check/{int(time.time())}"
    today = date.today().isoformat()
    print("create:", p.publish_now(Order(key, "T3 publisher check", 1, today, "T3 publisher check, safe to delete")))
    input(f"Tick the order external_id={key} delivered in the web app, then press Enter... ")
    print("update:", p.publish_now(Order(key, "T3 publisher check", 2, today, "T3 publisher check, updated")))
    print("Now check in the app: quantity is 2 and the order is still delivered.")


if __name__ == "__main__":
    main()
