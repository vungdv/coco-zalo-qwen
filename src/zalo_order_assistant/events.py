"""Shared data types: the messages, orders and events passed between the Coordinator and its colleagues (spec §2.4).

Data only, no logic. Every colleague and the Coordinator may import this module; nothing else is shared.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum

# Vietnam has no daylight saving time, so a fixed offset is exact and needs no tz database.
VN_TZ = timezone(timedelta(hours=7), name="Asia/Ho_Chi_Minh")


class FailureKind(StrEnum):
    SYSTEMIC = "SYSTEMIC"  # nothing works until fixed (Ollama down, Supabase unreachable or 401): retry forever
    PER_MESSAGE = "PER_MESSAGE"  # this one input keeps failing (e.g. an Ollama timeout): retry up to MAX_ATTEMPTS
    PERMANENT = "PERMANENT"  # will never pass (400/405/422, unusable model output): never retried


class ReaderStatus(StrEnum):
    READY = "READY"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    CHAT_NOT_FOUND = "CHAT_NOT_FOUND"
    NO_CHAT_OPEN = "NO_CHAT_OPEN"
    SELECTOR_DRIFT = "SELECTOR_DRIFT"
    BROWSER_FAILED = "BROWSER_FAILED"


class PipelineState(StrEnum):
    OK = "OK"
    PUBLISHER_UNAUTHORIZED = "PUBLISHER_UNAUTHORIZED"
    PUBLISHER_UNAVAILABLE = "PUBLISHER_UNAVAILABLE"
    EXTRACTOR_UNAVAILABLE = "EXTRACTOR_UNAVAILABLE"


@dataclass(frozen=True)
class Message:
    """One Zalo text message. `key` is the Zalo message key, the idempotency key end to end (FR-7)."""

    key: str
    chat: str
    sender: str  # display name; "" when only a numeric uid is known (FR-4)
    is_self: bool
    text: str = field(repr=False)  # never logged (NFR-3)
    sent_at: datetime  # tz-aware, Asia/Ho_Chi_Minh, always known (FR-6a)
    sender_id: str = ""  # Zalo user id of the sender: stable when the display name changes; "" when unknown or own


@dataclass(frozen=True)
class Order:
    key: str
    who: str
    qty: int
    date: str  # YYYY-MM-DD
    message: str = field(repr=False)
    placed_at: datetime  # when the customer sent the message (Message.sent_at), tz-aware
    customer_id: str = ""  # Message.sender_id; "" when unknown


# --- Reader -> Coordinator ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class MessageReceived:
    message: Message


@dataclass(frozen=True)
class ReaderState:
    state: ReaderStatus
    fatal: bool = False  # True when the Reader gave up (browser restarts exhausted): the Coordinator stops


# --- Extractor -> Coordinator ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class OrderExtracted:
    order: Order


@dataclass(frozen=True)
class NotAnOrder:
    key: str
    reason: str


@dataclass(frozen=True)
class ExtractFailed:
    key: str
    kind: FailureKind
    reason: str = ""


# --- Publisher -> Coordinator ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class OrderPublished:
    key: str


@dataclass(frozen=True)
class PublishFailed:
    key: str
    kind: FailureKind
    status: int | None = None  # HTTP status, None for a network error
    reason: str = ""


# --- Coordinator internal -------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RetryDue:
    key: str


Event = (
    MessageReceived
    | ReaderState
    | OrderExtracted
    | NotAnOrder
    | ExtractFailed
    | OrderPublished
    | PublishFailed
    | RetryDue
)
