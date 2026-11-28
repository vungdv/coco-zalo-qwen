"""Entry point: the only place that picks the concrete colleagues and wires them to the Coordinator (spec §2.2).

    uv run zalo-order-assistant

Configuration comes from the environment and `.env` (spec §2.7). Ctrl+C (SIGINT) or SIGTERM stops cleanly: the
message in flight is not acked, so it is read and processed again on the next start.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from dotenv import find_dotenv, load_dotenv

from zalo_order_assistant.audit_log import SqliteAuditLog
from zalo_order_assistant.coordinator import Coordinator
from zalo_order_assistant.order_extractor import ExtractorConfig, OrderExtractor, create_extractor
from zalo_order_assistant.order_publisher import PublisherConfig, SupabaseOrderPublisher, create_publisher
from zalo_order_assistant.ports import Mediator, MessageSource
from zalo_order_assistant.voice_commands.wiring import VoiceConfig, create_voice_worker
from zalo_order_assistant.voice_commands.worker import VoiceWorker
from zalo_order_assistant.zalo_reader import ReaderConfig, create_reader

log = logging.getLogger("zalo_order_assistant")

REDACTED = "***"


def env_bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    v = value.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"not a boolean: {value!r}")


@dataclass(frozen=True)
class PipelineConfig:
    max_attempts: int = 5
    retry_max_backoff_seconds: float = 60.0
    include_own_messages: bool = False
    audit_db_path: Path = Path("./data/audit.db")

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> PipelineConfig:
        e = os.environ if env is None else env
        d = cls()
        cfg = cls(
            max_attempts=int(e.get("MAX_ATTEMPTS") or d.max_attempts),
            retry_max_backoff_seconds=float(e.get("RETRY_MAX_BACKOFF_SECONDS") or d.retry_max_backoff_seconds),
            include_own_messages=env_bool(e.get("INCLUDE_OWN_MESSAGES"), d.include_own_messages),
            audit_db_path=Path(e.get("AUDIT_DB_PATH") or d.audit_db_path),
        )
        if cfg.max_attempts < 1:
            raise ValueError("MAX_ATTEMPTS must be at least 1")
        return cfg


@dataclass(frozen=True)
class Config:
    pipeline: PipelineConfig
    reader: ReaderConfig
    extractor: ExtractorConfig
    publisher: PublisherConfig = field(repr=False)
    voice: VoiceConfig = field(default_factory=VoiceConfig)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Config:
        return cls(
            PipelineConfig.from_env(env),
            ReaderConfig.from_env(env),
            ExtractorConfig.from_env(env),
            PublisherConfig.from_env(env),
            VoiceConfig.from_env(env),
        )

    @property
    def secrets(self) -> list[str]:
        return [s for s in (self.publisher.api_key,) if s]


class RedactSecrets(logging.Filter):
    """Last line of defence for NFR-3: no secret value ever reaches a log line, whatever logged it."""

    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self.secrets = [s for s in secrets if len(s) >= 4]

    def filter(self, record: logging.LogRecord) -> bool:
        if not self.secrets:
            return True
        msg = record.getMessage()
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        for s in self.secrets:
            msg = msg.replace(s, REDACTED)
            if record.exc_text:
                record.exc_text = record.exc_text.replace(s, REDACTED)
        record.msg, record.args = msg, None
        return True


def setup_logging(secrets: list[str], level: str = "INFO", stream: TextIO | None = None) -> logging.Handler:
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    handler.addFilter(RedactSecrets(secrets))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return handler


def status_printer(stream: TextIO | None = None):
    def show(line: str) -> None:
        print(f"[status] {line}", file=stream or sys.stdout, flush=True)

    return show


@dataclass
class App:
    coordinator: Coordinator
    reader: MessageSource
    extractor: OrderExtractor
    publisher: SupabaseOrderPublisher
    audit: SqliteAuditLog
    voice: VoiceWorker | None = None

    def run(self) -> None:
        try:
            if self.voice:
                self.voice.start()
            self.coordinator.run()
        finally:
            if self.voice:
                self.voice.stop()
            self.extractor.close()
            self.publisher.close()
            self.audit.close()


def build(
    config: Config,
    *,
    on_status: Callable[[str], None] | None = None,
    reader_factory: Callable[[Mediator], MessageSource] | None = None,
) -> App:
    """Create the Coordinator and the three colleagues and wire them. `reader_factory` replaces the Zalo Reader
    (for example with a Reader that replays recorded messages)."""
    audit = SqliteAuditLog(config.pipeline.audit_db_path)
    coordinator = Coordinator(
        audit,
        max_attempts=config.pipeline.max_attempts,
        include_own_messages=config.pipeline.include_own_messages,
        max_backoff_seconds=config.pipeline.retry_max_backoff_seconds,
        on_status=on_status,
    )
    source = reader_factory(coordinator) if reader_factory else create_reader(coordinator, config.reader)
    extractor = create_extractor(coordinator, config.extractor)
    publisher = create_publisher(coordinator, config.publisher)
    coordinator.attach(source, extractor, publisher)
    voice = create_voice_worker(config.voice, config.publisher, on_status) if config.voice.enabled else None
    return App(coordinator, source, extractor, publisher, audit, voice)


def main() -> None:
    load_dotenv(find_dotenv(usecwd=True))  # .env of the working directory, like the ./data paths
    try:
        config = Config.from_env()
    except ValueError as exc:
        print(f"configuration error: {exc} (see .env.example)", file=sys.stderr)
        raise SystemExit(2) from exc
    setup_logging(config.secrets, os.environ.get("LOG_LEVEL", "INFO"))
    log.info(
        "starting chat=%s model=%s backfill_days=%g max_attempts=%d include_own=%s voice=%s",
        "configured" if config.reader.zalo_chat else "the open group",
        config.extractor.ollama_model,
        config.reader.backfill_days,
        config.pipeline.max_attempts,
        config.pipeline.include_own_messages,
        "on" if config.voice.enabled else "OFF",
    )
    app = build(config, on_status=status_printer())

    def stop(signum: int, _frame: object) -> None:
        log.info("stopping (signal %d)", signum)
        app.coordinator.stop()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    app.run()
    log.info("stopped")


if __name__ == "__main__":
    main()
