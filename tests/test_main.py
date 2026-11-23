"""T6: configuration, wiring, secret redaction, clean start and stop."""

from __future__ import annotations

import io
import logging
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from zalo_order_assistant.main import Config, PipelineConfig, RedactSecrets, build, env_bool, setup_logging
from zalo_order_assistant.zalo_reader import ZaloReader

ENV = {"SUPABASE_URL": "http://127.0.0.1:9/", "SUPABASE_API_KEY": "sk-live-VERY-SECRET"}


def env(tmp_path: Path, **extra) -> dict[str, str]:
    return ENV | {"AUDIT_DB_PATH": str(tmp_path / "audit.db"), "STATE_DB_PATH": str(tmp_path / "acked.db")} | extra


def test_defaults_match_the_spec(tmp_path):
    c = Config.from_env(env(tmp_path))
    assert (c.pipeline.max_attempts, c.pipeline.retry_max_backoff_seconds, c.pipeline.include_own_messages) == (
        5,
        60.0,
        False,
    )
    assert (c.reader.zalo_chat, c.reader.backfill_days, c.reader.poll_interval_seconds) == ("", 3.0, 2.0)
    assert (c.extractor.ollama_url, c.extractor.ollama_model, c.extractor.timeout_seconds) == (
        "http://localhost:11434",
        "qwen2.5:7b-instruct",
        20.0,
    )
    assert c.extractor.max_days_ahead == 31 and c.publisher.supabase_url == "http://127.0.0.1:9"
    assert "VERY-SECRET" not in repr(c)


def test_overrides(tmp_path):
    c = Config.from_env(
        env(
            tmp_path,
            MAX_ATTEMPTS="3",
            INCLUDE_OWN_MESSAGES="true",
            ZALO_CHAT="Bếp",
            MAX_DAYS_AHEAD="14",
            RETRY_MAX_BACKOFF_SECONDS="30",
        )
    )
    assert (c.pipeline.max_attempts, c.pipeline.include_own_messages, c.reader.zalo_chat) == (3, True, "Bếp")
    assert (c.extractor.max_days_ahead, c.pipeline.retry_max_backoff_seconds) == (14, 30.0)


def test_supabase_settings_are_required():
    with pytest.raises(ValueError, match="SUPABASE_URL"):
        Config.from_env({})


@pytest.mark.parametrize("v,b", [("true", True), ("1", True), ("no", False), ("", False), (None, False)])
def test_env_bool(v, b):
    assert env_bool(v, False) is b


def test_bad_values_are_rejected():
    with pytest.raises(ValueError):
        env_bool("maybe", False)
    with pytest.raises(ValueError):
        PipelineConfig.from_env({"MAX_ATTEMPTS": "0"})


def test_wiring(tmp_path):
    app = build(Config.from_env(env(tmp_path)))
    c = app.coordinator
    assert isinstance(app.reader, ZaloReader)
    assert (c._source, c._extractor, c._publisher) == (app.reader, app.extractor, app.publisher)
    assert app.reader._mediator is c and app.extractor._mediator is c and app.publisher._mediator is c


def test_secrets_are_redacted_from_every_log_line():
    out = io.StringIO()
    handler = setup_logging(["sk-live-VERY-SECRET"], stream=out)
    try:
        logging.getLogger("x").error("key=%s", "sk-live-VERY-SECRET")
        try:
            raise RuntimeError("header x-api-key: sk-live-VERY-SECRET")
        except RuntimeError:
            logging.getLogger("x").exception("boom")
    finally:
        logging.getLogger().removeHandler(handler)
    text = out.getvalue()
    assert "VERY-SECRET" not in text and text.count("***") >= 2


def test_redact_filter_ignores_short_values():
    assert RedactSecrets(["", "ab"]).secrets == []


class ReplaySource:
    def __init__(self):
        self.started = threading.Event()
        self.stopped = False

    def start(self):
        self.started.set()

    def stop(self):
        self.stopped = True

    def ack(self, key):
        pass


def test_starts_and_stops_cleanly(tmp_path):
    source = ReplaySource()
    lines: list[str] = []
    app = build(Config.from_env(env(tmp_path)), on_status=lines.append, reader=source)
    t = threading.Thread(target=app.run)
    t.start()
    assert source.started.wait(2)
    app.coordinator.stop()
    t.join(5)
    assert not t.is_alive() and source.stopped
    assert lines and lines[0].startswith("reader=STARTING pipeline=OK")


def test_missing_config_exits_with_a_message(tmp_path):
    r = subprocess.run(
        [sys.executable, "-m", "zalo_order_assistant.main"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
        timeout=60,
    )
    assert r.returncode == 2 and "SUPABASE_URL" in r.stderr
