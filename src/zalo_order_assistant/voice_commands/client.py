"""Voice Command API client (backend spec v3 §6): claim and report. The key is never logged."""

from __future__ import annotations

from datetime import datetime

import requests

from zalo_order_assistant.events import FailureKind
from zalo_order_assistant.voice_commands.model import Command, DayOrder


class ApiError(Exception):
    def __init__(self, kind: FailureKind, status: int | None, reason: str) -> None:
        super().__init__(f"{reason} {status or ''}".strip())
        self.kind, self.status, self.reason = kind, status, reason


def _error(status: int) -> ApiError:
    if status in (401, 403):
        return ApiError(FailureKind.SYSTEMIC, status, "unauthorized")
    if status in (404, 408, 429) or status >= 500:
        return ApiError(FailureKind.SYSTEMIC, status, "unavailable")
    return ApiError(FailureKind.PERMANENT, status, "rejected")


class VoiceCommandClient:
    def __init__(self, supabase_url: str, api_key: str, timeout: float = 30.0,
                 session: requests.Session | None = None) -> None:
        self._url = f"{supabase_url.rstrip('/')}/functions/v1/voice-commands"
        self._key, self._timeout = api_key, timeout
        self._http = session or requests.Session()

    def _send(self, method: str, path: str, traceparent: str, body: dict | None = None) -> requests.Response:
        try:
            return self._http.request(method, self._url + path, json=body, timeout=self._timeout,
                                      headers={"x-api-key": self._key, "traceparent": traceparent})
        except requests.RequestException as e:
            raise ApiError(FailureKind.SYSTEMIC, None, type(e).__name__) from e

    def claim(self, traceparent: str) -> Command | None:
        r = self._send("POST", "/claim", traceparent)
        if r.status_code == 204:
            return None
        if r.status_code != 200:
            raise _error(r.status_code)
        try:
            body = r.json()
            c = body["command"]
            return Command(
                str(c["id"]),
                c["transcript"],
                c["delivery_date"],
                datetime.fromisoformat(c["created_at"]),
                tuple(DayOrder(o["customer_id"], o["customer_name"], o["quantity"], o["delivered"])
                      for o in body["orders"]),
            )
        except (KeyError, TypeError, ValueError) as e:  # not our API (wrong URL, a proxy page)
            raise ApiError(FailureKind.SYSTEMIC, r.status_code, "unreadable claim") from e

    def finish(self, command_id: str, status: str, result: dict, traceparent: str) -> bool:
        """False when the command is no longer processing (409): drop it (V9)."""
        r = self._send("PATCH", f"/{command_id}", traceparent, {"status": status, "result": result})
        if r.status_code == 200:
            return True
        if r.status_code == 409:
            return False
        raise _error(r.status_code)
