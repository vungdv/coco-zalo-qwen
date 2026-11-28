"""Configuration and construction of the voice pipeline (spec v3 §5.3)."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass

from zalo_order_assistant.events import Event
from zalo_order_assistant.order_publisher.publisher import PublisherConfig, SupabaseOrderPublisher
from zalo_order_assistant.voice_commands.client import VoiceCommandClient
from zalo_order_assistant.voice_commands.llm import VoiceLlm
from zalo_order_assistant.voice_commands.parser import CommandParser
from zalo_order_assistant.voice_commands.worker import VoiceWorker, WorkerConfig


@dataclass(frozen=True)
class VoiceConfig:
    enabled: bool = False  # V12: off by default
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:7b-instruct"
    llm_timeout_seconds: float = 15.0
    poll_seconds: float = 2.0
    max_attempts: int = 5
    max_backoff_seconds: float = 60.0

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> VoiceConfig:
        e = os.environ if env is None else env
        d = cls()
        flag = (e.get("VOICE_COMMANDS") or "0").strip().lower()
        if flag not in ("0", "1", "false", "true", "no", "yes", "off", "on"):
            raise ValueError(f"not a boolean: {flag!r}")
        return cls(
            enabled=flag in ("1", "true", "yes", "on"),
            ollama_url=e.get("OLLAMA_URL") or d.ollama_url,
            ollama_model=e.get("VOICE_OLLAMA_MODEL") or e.get("OLLAMA_MODEL") or d.ollama_model,
            llm_timeout_seconds=float(e.get("VOICE_LLM_TIMEOUT_SECONDS") or d.llm_timeout_seconds),
            poll_seconds=float(e.get("VOICE_POLL_SECONDS") or d.poll_seconds),
            max_attempts=int(e.get("MAX_ATTEMPTS") or d.max_attempts),
            max_backoff_seconds=float(e.get("RETRY_MAX_BACKOFF_SECONDS") or d.max_backoff_seconds),
        )


class _NoEvents:
    """The worker calls publish_now and reads the result itself, so nothing is ever notified."""

    def notify(self, event: Event) -> None:  # pragma: no cover
        raise AssertionError("unused")


def create_voice_worker(
    config: VoiceConfig, publisher: PublisherConfig, on_status: Callable[[str], None] | None = None
) -> VoiceWorker:
    return VoiceWorker(
        VoiceCommandClient(publisher.supabase_url, publisher.api_key, publisher.timeout_seconds),
        CommandParser(VoiceLlm(config.ollama_model, config.ollama_url, config.llm_timeout_seconds)),
        SupabaseOrderPublisher(_NoEvents(), publisher),
        WorkerConfig(config.poll_seconds, config.max_attempts, config.max_backoff_seconds),
        on_status=on_status,
    )
