"""From a checked command to the order line to send, and from the answer to the result (spec v3 V2, V7, V8, §4.4)."""

from __future__ import annotations

from dataclasses import replace

from zalo_order_assistant.events import Op, Order
from zalo_order_assistant.voice_commands.model import Command, Failed, Parsed
from zalo_order_assistant.voice_commands.names import Ambiguous, Match, NoMatch

SOURCE = "voice"
_OPS = {"total": Op.SET, "add": Op.ADD, "reduce": Op.REDUCE, "cancel": Op.CANCEL, "delete": Op.DELETE}


def plan(command: Command, parsed: Parsed, who: Match | NoMatch | Ambiguous) -> Order | Failed:
    if isinstance(who, Ambiguous):
        return Failed("ambiguous", parsed.customer, who.names)
    if isinstance(who, Match):
        name, customer_id = who.customer_name, who.customer_id
    elif parsed.op in ("total", "add"):  # a walk-in: the backend keys it by the name
        name, customer_id = " ".join(w[:1].upper() + w[1:] for w in parsed.customer.split()), ""
    else:
        return Failed("not_found", parsed.customer)
    op, qty = _OPS[parsed.op], parsed.qty
    if op is Op.SET and qty == 0:
        op, qty = Op.CANCEL, None
    return Order(
        command.id,
        name,
        qty,
        command.delivery_date,
        command.transcript,
        command.created_at,
        customer_id=customer_id,
        op=op,
        source=SOURCE,
    )


def add_instead(order: Order) -> Order:
    """V8: "<name>, N suất" with nothing open is a new order, under the same idempotency key."""
    return replace(order, op=Op.ADD)


def done(body: dict) -> dict:
    line = body["line"]
    return {
        "op": line["op"],
        "order_id": body["id"],
        "customer_name": body["customer_name"],
        "quantity": body["quantity"],
        "delta": line["quantity"],
    }
