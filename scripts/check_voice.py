"""Live check (spec v3 T6): insert a voice command as a staff user and wait for the Assistant's result.

Needs SUPABASE_URL, SUPABASE_ANON_KEY, STAFF_EMAIL, STAFF_PASSWORD, and the Assistant running with VOICE_COMMANDS=1.
Usage: uv run python scripts/check_voice.py "Kiểm Tra, 2 suất" [YYYY-MM-DD]
It changes real orders: use a test name, then "Kiểm Tra xoá đơn" to remove it.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime

import requests

from zalo_order_assistant.events import VN_TZ


def main() -> int:
    url, anon = os.environ["SUPABASE_URL"].rstrip("/"), os.environ["SUPABASE_ANON_KEY"]
    transcript = sys.argv[1]
    day = sys.argv[2] if len(sys.argv) > 2 else datetime.now(VN_TZ).date().isoformat()
    login = requests.post(f"{url}/auth/v1/token?grant_type=password", headers={"apikey": anon}, timeout=30,
                          json={"email": os.environ["STAFF_EMAIL"], "password": os.environ["STAFF_PASSWORD"]})
    login.raise_for_status()
    headers = {"apikey": anon, "authorization": f"Bearer {login.json()['access_token']}"}
    created = requests.post(f"{url}/rest/v1/voice_commands", timeout=30,
                            headers={**headers, "prefer": "return=representation"},
                            json={"transcript": transcript, "delivery_date": day})
    created.raise_for_status()
    command_id, started = created.json()[0]["id"], time.monotonic()
    while time.monotonic() - started < 60:
        row = requests.get(f"{url}/rest/v1/voice_commands?id=eq.{command_id}&select=status,result",
                           headers=headers, timeout=30).json()[0]
        if row["status"] in ("done", "failed"):
            took = time.monotonic() - started
            print(f"{row['status']} in {took:.1f}s", json.dumps(row["result"], ensure_ascii=False))
            return 0 if row["status"] == "done" else 1
        time.sleep(0.5)
    print("not picked up within 60s: is the Assistant running with VOICE_COMMANDS=1?")
    return 2


if __name__ == "__main__":
    sys.exit(main())
