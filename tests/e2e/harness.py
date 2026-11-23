"""End-to-end harness: a Reader that replays a recorded Zalo chat, and a local stand-in for the Supabase API."""

from __future__ import annotations

import json
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from zalo_order_assistant.events import Message, MessageReceived, ReaderState, ReaderStatus
from zalo_order_assistant.ports import Mediator
from zalo_order_assistant.zalo_reader.store import AckedStore

CHAT = Path(__file__).resolve().parents[1] / "fixtures" / "e2e_chat.jsonl"


def load_chat(prefix: str = "") -> list[tuple[Message, dict]]:
    out = []
    for line in CHAT.read_text("utf-8").splitlines():
        r = json.loads(line)
        m = Message(
            prefix + r["key"],
            "Bếp Phương Bắc - Đặt cơm",
            r["sender"],
            r["is_self"],
            r["text"],
            datetime.fromisoformat(r["sent_at"]),
        )
        out.append((m, r["expect"]))
    return out


class ReplayReader:
    """MessageSource that emits a recorded chat, oldest first, skipping what the store says was acked."""

    def __init__(self, mediator: Mediator, messages: list[Message], store: AckedStore) -> None:
        self.mediator, self.messages, self.store = mediator, messages, store
        self.acked: list[str] = []
        self.changed = threading.Condition()

    def start(self) -> None:
        self.mediator.notify(ReaderState(ReaderStatus.READY))
        for m in self.messages:
            if not self.store.contains(m.key):
                self.mediator.notify(MessageReceived(m))

    def stop(self) -> None:
        pass

    def ack(self, key: str) -> None:
        self.store.add([key])
        with self.changed:
            self.acked.append(key)
            self.changed.notify_all()

    def wait_acked(self, n: int, timeout: float) -> bool:
        with self.changed:
            return self.changed.wait_for(lambda: len(self.acked) >= n, timeout)


class FakeSupabase:
    """The `orders` Edge Function contract (bep-phuong-bac-supabase spec S17-S23): x-api-key auth, idempotent
    upsert on (source, external_id) answering 201 on create and 200 on update, and `delivered` never reset."""

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.rows: dict[tuple[str, str], dict] = {}
        self.lock = threading.Lock()
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                status, out = fake.handle(self.path, self.headers.get("x-api-key"), body)
                data = json.dumps(out).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def handle(self, path: str, key: str | None, b: dict) -> tuple[int, dict]:
        if path != "/functions/v1/orders":
            return 404, {"error": "not_found"}
        if key != self.api_key:
            return 401, {"error": "unauthorized"}
        bad = {}
        if not str(b.get("customer_name") or "").strip():
            bad["customer_name"] = "required"
        if not isinstance(b.get("quantity"), int) or b["quantity"] <= 0:
            bad["quantity"] = "positive integer"
        try:
            datetime.strptime(b.get("delivery_date") or "", "%Y-%m-%d")
        except ValueError:
            bad["delivery_date"] = "YYYY-MM-DD"
        if len(b.get("message") or "") > 2000:
            bad["message"] = "too long"
        if bad:
            return 422, {"error": "validation_failed", "fields": bad}
        k = (b["source"], b["external_id"])
        fields = {f: b[f] for f in ("customer_name", "quantity", "delivery_date", "message")}
        with self.lock:
            if k in self.rows:
                self.rows[k].update(fields)  # delivered is untouched
                return 200, self.rows[k]
            self.rows[k] = {"source": k[0], "external_id": k[1], **fields, "delivered": False}
            return 201, self.rows[k]

    def close(self) -> None:
        self.server.shutdown()
