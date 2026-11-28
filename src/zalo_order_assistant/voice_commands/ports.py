"""What the VoiceWorker depends on (spec v3 §5). `main.py` picks the implementations."""

from __future__ import annotations

from typing import Protocol

from zalo_order_assistant.events import Order, OrderNotApplied, OrderPublished, PublishFailed
from zalo_order_assistant.voice_commands.model import Command, Failed, Parsed


class CommandSource(Protocol):
    def claim(self, traceparent: str) -> Command | None: ...
    def finish(self, command_id: str, status: str, result: dict, traceparent: str) -> bool: ...


class Parser(Protocol):
    def parse(self, command: Command) -> Parsed | Failed: ...


class LinePublisher(Protocol):
    def publish_now(
        self, order: Order, traceparent: str | None = None
    ) -> OrderPublished | OrderNotApplied | PublishFailed: ...
