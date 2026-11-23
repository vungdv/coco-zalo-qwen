"""Interfaces between the Coordinator (mediator) and its colleagues (spec §2.2).

The Coordinator imports only these Protocols and `events`; `main.py` is the only place that picks implementations.
Commands return at once; each result arrives later as an event through `Mediator.notify`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from zalo_order_assistant.events import Event, Message, Order


class Mediator(Protocol):
    def notify(self, event: Event) -> None:
        """Queue an event for the Coordinator. Safe from any thread; never runs a handler inline."""
        ...


class MessageSource(Protocol):  # ZaloReader
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def ack(self, key: str) -> None: ...


class Extractor(Protocol):  # OrderExtractor
    def extract(self, message: Message) -> None: ...  # -> OrderExtracted | NotAnOrder | ExtractFailed


class Publisher(Protocol):  # OrderPublisher
    def publish(self, order: Order) -> None: ...  # -> OrderPublished | PublishFailed


class AuditLog(Protocol):
    """Messages read but turned into no order (FR-8). Never the message text."""

    def record(self, key: str, sent_at: datetime, reason: str, status: str) -> None: ...
