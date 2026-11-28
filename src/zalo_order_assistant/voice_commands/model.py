"""Data of one voice command on its way through the pipeline (spec v3 §5). No logic, no I/O."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class DayOrder:
    """One order of the command's date, as the claim lists it: the context for name matching only."""

    customer_id: str
    customer_name: str = field(repr=False)
    quantity: int = 0
    delivered: bool = False


@dataclass(frozen=True)
class Command:
    id: str
    transcript: str = field(repr=False)  # never logged (V11)
    delivery_date: str  # YYYY-MM-DD, the date on the chef's screen
    created_at: datetime
    orders: tuple[DayOrder, ...] = ()


@dataclass(frozen=True)
class Parsed:
    """What was said: the name as spoken, total/add/reduce/cancel/delete, and the number."""

    customer: str = field(repr=False)
    op: str
    qty: int | None = None


@dataclass(frozen=True)
class Failed:
    """The `result` of a failed command (§4.4)."""

    error: str
    detail: str = field(default="", repr=False)  # may be the spoken name: PATCH body only, never logs
    candidates: tuple[str, ...] = field(default=(), repr=False)

    def result(self) -> dict:
        out: dict = {"error": self.error}
        if self.detail:
            out["detail"] = self.detail
        if self.candidates:
            out["candidates"] = list(self.candidates)
        return out
