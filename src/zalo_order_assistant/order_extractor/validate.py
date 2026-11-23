"""Validation of the model answer (spec §1.3). First failure wins. Pure functions, no I/O."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from zalo_order_assistant.order_extractor.calendar import INVALID_DATE

MAX_QTY = 99  # sanity bound: a larger number is a phone number or an address the model misread, not portions
BAD_QUANTITY = "bad quantity"
SEVERAL_ORDERS = "several orders"


@dataclass(frozen=True)
class Valid:
    qty: int


@dataclass(frozen=True)
class Rejected:
    reason: str  # a fixed phrase, never message text: it goes to the audit log


def parse_qty(raw: object) -> int | None:
    """FR-5: a whole number >= 1; missing means 1. None when unusable."""
    if raw is None or raw == "":
        return 1
    if isinstance(raw, bool):
        return None
    if isinstance(raw, float) and raw.is_integer():
        raw = int(raw)
    if isinstance(raw, str) and raw.strip().isdigit():
        raw = int(raw.strip())
    if isinstance(raw, int) and 1 <= raw <= MAX_QTY:
        return raw
    return None


def parse_model_date(raw: object) -> date | None | str:
    """The model's date, only to log a disagreement with the resolver: a date, None, or INVALID_DATE."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if not isinstance(raw, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw.strip()):
        return INVALID_DATE
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        return INVALID_DATE


def validate(args: dict) -> Valid | Rejected:
    """The model's record_order arguments. The date is not taken from the model: see day_words.resolve."""
    if args.get("multipleOrders") is True:
        return Rejected(SEVERAL_ORDERS)
    qty = parse_qty(args.get("qty"))
    if qty is None:
        return Rejected(BAD_QUANTITY)
    return Valid(qty)
