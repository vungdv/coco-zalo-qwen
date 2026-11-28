"""Transcript to checked command: guard, model, then code checks (spec v3 §4.3)."""

from __future__ import annotations

from typing import Protocol

from zalo_order_assistant.events import VN_TZ
from zalo_order_assistant.order_extractor.extractor import REPLY_WORDS
from zalo_order_assistant.order_extractor.llm import LlmInvalid
from zalo_order_assistant.voice_commands.checks import NOT_UNDERSTOOD, check
from zalo_order_assistant.voice_commands.model import Command, Failed, Parsed

MODEL_WORDS = ("kitchen_command", "not_a_command")


class Model(Protocol):
    def read(self, transcript: str) -> Parsed | str: ...


class CommandParser:
    def __init__(self, model: Model) -> None:
        self._model = model

    def parse(self, command: Command) -> Parsed | Failed:
        """Raises LlmUnavailable or LlmTimeout: the caller retries those."""
        text = command.transcript.strip()
        if not text:
            return Failed(NOT_UNDERSTOOD, "empty")
        if REPLY_WORDS.search(text) or any(w in text.lower() for w in MODEL_WORDS):
            return Failed(NOT_UNDERSTOOD, "injection")
        try:
            answer = self._model.read(text)
        except LlmInvalid:
            return Failed(NOT_UNDERSTOOD, "invalid output")
        if isinstance(answer, str):
            return Failed(NOT_UNDERSTOOD, answer)
        return check(answer, text, command.delivery_date, command.created_at.astimezone(VN_TZ).date())
