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
    """The `orders` Edge Function contract of bep-phuong-bac-supabase spec v2: x-api-key auth, validation (422), and
    an append-only ledger. Each request is one line (`op` add/reduce/set/cancel) on the customer's one open order for
    the day; the customer is `customer_id`, else lower(trim(customer_name)). A replay of (source, external_id) with
    the same content is 200, with other content 409 idempotency_conflict; a change with no open order is 409
    no_open_order and stores nothing. A reduce clamps at 0. `delivered` is only set by `deliver()` (staff)."""

    OPS = ("add", "reduce", "set", "cancel")

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.orders: list[dict] = []  # id, customer_id, customer_name, delivery_date, delivered
        self.lines: dict[tuple[str, str], dict] = {}  # (source, external_id) -> line
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

    @staticmethod
    def validate(b: dict) -> dict:
        bad = {}
        op = b.get("op", "add")
        if op not in FakeSupabase.OPS:
            bad["op"] = "unknown"
        name = str(b.get("customer_name") or "").strip()
        if op == "add" and not name:
            bad["customer_name"] = "required"
        elif not name and not b.get("customer_id"):
            bad["customer_name"] = "customer_name or customer_id is required"
        q = b.get("quantity")
        if op == "cancel":
            if q is not None:
                bad["quantity"] = "must be omitted for cancel"
        elif not isinstance(q, int) or isinstance(q, bool) or q < (0 if op == "set" else 1):
            bad["quantity"] = "bad quantity"
        try:
            datetime.strptime(b.get("delivery_date") or "", "%Y-%m-%d")
        except ValueError:
            bad["delivery_date"] = "YYYY-MM-DD"
        if len(b.get("message") or "") > 2000:
            bad["message"] = "too long"
        return bad

    def handle(self, path: str, key: str | None, b: dict) -> tuple[int, dict]:
        if path != "/functions/v1/orders":
            return 404, {"error": "not_found"}
        if key != self.api_key:
            return 401, {"error": "unauthorized"}
        if bad := self.validate(b):
            return 422, {"error": "validation_failed", "fields": bad}
        op, q, day = b.get("op", "add"), b.get("quantity"), b["delivery_date"]
        name = str(b.get("customer_name") or "").strip()
        customer = (b.get("customer_id") or "").strip() or name.lower()
        k = (b.get("source", "api"), b["external_id"])
        with self.lock:
            if k in self.lines:  # D4
                line = self.lines[k]
                order = self.orders[line["order"]]
                same = (line["op"], line["requested_quantity"], line["message"], line["placed_at"]) == (
                    op,
                    q,
                    b.get("message", ""),
                    b.get("placed_at"),
                ) and (order["delivery_date"], order["customer_id"]) == (day, customer)
                if not same:
                    return 409, {"title": "Conflict", "status": 409, "error": "idempotency_conflict"}
                return 200, {**self.view(line["order"]), "line": line}
            open_ = [
                i
                for i, o in enumerate(self.orders)
                if (o["customer_id"], o["delivery_date"]) == (customer, day) and not o["delivered"]
            ]
            if not open_:
                if op != "add":  # D6
                    return 409, {"title": "Conflict", "status": 409, "error": "no_open_order"}
                self.orders.append(
                    {"customer_id": customer, "customer_name": name, "delivery_date": day, "delivered": False}
                )
                i = len(self.orders) - 1
            else:
                i = open_[0]
                if name:
                    self.orders[i]["customer_name"] = name  # latest name seen
            total = self.total(i)
            delta = {"add": q, "reduce": -min(q or 0, total), "set": (q or 0) - total, "cancel": -total}[op]
            line = {
                "order": i,
                "op": op,
                "requested_quantity": q,
                "quantity": delta,
                "message": b.get("message", ""),
                "placed_at": b.get("placed_at"),
                "external_id": k[1],
            }
            self.lines[k] = line
            return 201, {**self.view(i), "line": line}

    def total(self, i: int) -> int:
        return sum(line["quantity"] for line in self.lines.values() if line["order"] == i)

    def view(self, i: int) -> dict:
        return {**self.orders[i], "quantity": self.total(i)}

    def deliver(self, customer: str, day: str) -> None:
        """Staff tick the customer's open order for that day delivered."""
        with self.lock:
            for o in self.orders:
                if (o["customer_id"], o["delivery_date"]) == (customer, day) and not o["delivered"]:
                    o["delivered"] = True

    def totals(self) -> dict[tuple[str, str], list[int]]:
        """(customer_name, delivery_date) -> the totals of that customer's orders on that day, oldest first."""
        out: dict[tuple[str, str], list[int]] = {}
        for i, o in enumerate(self.orders):
            out.setdefault((o["customer_name"], o["delivery_date"]), []).append(self.total(i))
        return out

    def close(self) -> None:
        self.server.shutdown()
