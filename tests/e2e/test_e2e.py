"""T7 end-to-end: replayed Zalo chat -> Coordinator -> real local Qwen -> Supabase orders API.

    uv run pytest -m e2e

Supabase is a local stand-in with the same contract by default. To run against a real Supabase TEST project, set
E2E_SUPABASE_URL and E2E_SUPABASE_API_KEY; message keys then get a unique prefix per run, and the rows created are
left in that project (named E2E ...). Never point this at the production project.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import replace

import pytest
import requests

from tests.e2e.harness import FakeSupabase, ReplayReader, load_chat
from zalo_order_assistant.events import PipelineState
from zalo_order_assistant.main import App, Config, build
from zalo_order_assistant.zalo_reader.store import AckedStore

pytestmark = pytest.mark.e2e

API_KEY = "sk-e2e-test-key"
# (customer, delivery_date) -> order totals, from tests/fixtures/e2e_chat.jsonl
EXPECTED_TOTALS = {
    ("Hà", "2026-11-21"): [2],
    ("Mai", "2026-11-24"): [2],
    ("Mai", "2026-11-23"): [4],  # "đặt cơm" (1) and "đặt thêm 3 phần nữa nhé" (3) merge into one order (AC-6)
    ("Hùng", "2026-11-24"): [2],
    ("Lan", "2026-11-27"): [1],
    ("Tú", "2026-11-28"): [2],
    ("Ngọc", "2026-11-24"): [0],  # add 3, cancel: listed as cancelled (AZ-2)
    ("Phúc", "2026-11-23"): [2],  # add 2, set 3, reduce 1 (AZ-3)
    ("Vy", "2026-11-25"): [0],  # "kia 2 phần", "kia hủy nhé" (AZ-10)
}
REAL_URL = os.environ.get("E2E_SUPABASE_URL")
REAL_KEY = os.environ.get("E2E_SUPABASE_API_KEY")


@pytest.fixture(scope="module", autouse=True)
def ollama():
    try:
        requests.get("http://localhost:11434/api/tags", timeout=2).raise_for_status()
    except requests.RequestException:
        pytest.skip("Ollama is not running")


@pytest.fixture
def supabase():
    if REAL_URL and REAL_KEY:
        yield None
        return
    fake = FakeSupabase(API_KEY)
    yield fake
    fake.close()


class Recorder:
    """Wraps the publisher's HTTP session: every Supabase answer per external_id, in order."""

    def __init__(self, session: requests.Session) -> None:
        self.session, self.statuses = session, {}
        self.lock = threading.Lock()

    def post(self, url, json, **kw):
        r = self.session.post(url, json=json, **kw)
        with self.lock:
            self.statuses.setdefault(json["external_id"], []).append(r.status_code)
        return r


class Run:
    def __init__(self, tmp_path, supabase, caplog):
        self.tmp, self.caplog = tmp_path, caplog
        self.prefix = f"e2e-{int(time.time())}/" if supabase is None else ""
        self.chat = load_chat(self.prefix)
        self.url = REAL_URL if supabase is None else supabase.url
        self.key = REAL_KEY if supabase is None else API_KEY
        self.recorder = Recorder(requests.Session())
        if supabase is None:  # real project: make the rows recognisable
            self.chat = [(replace(m, sender=f"E2E {m.sender}" if m.sender else ""), e) for m, e in self.chat]

    def app(self) -> tuple[App, ReplayReader]:
        env = {"SUPABASE_URL": self.url, "SUPABASE_API_KEY": self.key, "AUDIT_DB_PATH": str(self.tmp / "audit.db")}
        holder: dict = {}

        def reader(mediator):
            holder["r"] = ReplayReader(mediator, [m for m, _ in self.chat], AckedStore(self.tmp / "acked.db"))
            return holder["r"]

        app = build(Config.from_env(env), reader_factory=reader)
        app.coordinator.base_backoff, app.coordinator.max_backoff = 0.2, 1.0
        app.coordinator.repeat_state_seconds = 1.0
        app.publisher._http = self.recorder  # type: ignore[assignment]
        return app, holder["r"]

    def start(self, app: App) -> threading.Thread:
        t = threading.Thread(target=app.run, daemon=True)
        t.start()
        return t

    def stop(self, app: App, t: threading.Thread) -> None:
        app.coordinator.stop()
        t.join(30)
        assert not t.is_alive()

    def orders(self) -> dict[str, dict]:
        return {m.key: e["order"] for m, e in self.chat if "order" in e}

    def audited(self) -> dict[str, str]:
        return {m.key: e["audit"] for m, e in self.chat if "audit" in e}

    def check_final(self, supabase) -> None:
        not_applied = {k for k, status in self.audited().items() if status == "not_applied"}
        # one line per order message, stored once (201) and only ever replayed after (200): no duplicates.
        # A change with nothing open to change is posted and answered 409 no_open_order, never stored.
        assert set(self.recorder.statuses) == set(self.orders()) | not_applied
        for key, statuses in self.recorder.statuses.items():
            stored = [s for s in statuses if s != 401]  # 401s are the revoked-key attempts
            if key in not_applied:
                assert set(stored) == {409}, (key, statuses)
            else:
                assert stored[0] == 201 and all(s == 200 for s in stored[1:]), (key, statuses)
        if supabase is not None:
            lines = {ext: line for (_src, ext), line in supabase.lines.items()}
            assert set(lines) == set(self.orders())
            for key, want in self.orders().items():
                line = lines[key]
                order = supabase.orders[line["order"]]
                assert (order["customer_name"], line["op"], line["requested_quantity"], order["delivery_date"]) == (
                    want["who"],
                    want.get("op", "add"),
                    want["qty"],
                    want["date"],
                ), key
                assert line["message"] == next(m.text for m, _ in self.chat if m.key == key)
            # what the kitchen sees: one order per customer and day, with its running total (spec v2 §6)
            assert supabase.totals() == EXPECTED_TOTALS
        # every message that made no order is in the audit log, without its text
        from zalo_order_assistant.audit_log import SqliteAuditLog

        log = SqliteAuditLog(self.tmp / "audit.db")
        entries = log.entries()
        assert {k: status for k, _sent, _reason, status in entries} == self.audited()
        texts = [m.text for m, _ in self.chat]
        assert not any(t in str(e) for e in entries for t in texts)
        log.close()
        # message text never reached a log line
        logged = "\n".join(r.getMessage() for r in self.caplog.records)
        assert not any(t in logged for t in texts if len(t) > 6)
        assert self.key not in logged


@pytest.fixture
def run(tmp_path, supabase, caplog):
    caplog.set_level(logging.INFO)
    return Run(tmp_path, supabase, caplog)


def test_one_line_per_order_message_none_for_chat_AC1_to_AC23_AZ2_to_AZ10(run, supabase):
    app, reader = run.app()
    t = run.start(app)
    assert reader.wait_acked(len(run.chat), 300), f"acked {len(reader.acked)} of {len(run.chat)}"
    run.stop(app, t)
    assert sorted(reader.acked) == sorted(m.key for m, _ in run.chat)  # each message acked exactly once
    run.check_final(supabase)


def test_restart_mid_run_and_replay_again_make_no_duplicates_AC4(run, supabase):
    app, reader = run.app()
    t = run.start(app)
    assert reader.wait_acked(5, 300)
    run.stop(app, t)  # mid-run: the message in flight is not acked
    first = len(reader.acked)
    assert first < len(run.chat)

    app2, reader2 = run.app()  # restart with the same local stores
    t2 = run.start(app2)
    assert reader2.wait_acked(len(run.chat) - first, 300)
    run.stop(app2, t2)
    assert not set(reader.acked) & set(reader2.acked)

    posts_before = sum(len(s) for s in run.recorder.statuses.values())
    app3, reader3 = run.app()  # the same chat read a third time (restart or backfill): nothing to do
    t3 = run.start(app3)
    time.sleep(2)
    run.stop(app3, t3)
    assert reader3.acked == [] and sum(len(s) for s in run.recorder.statuses.values()) == posts_before
    run.check_final(supabase)


def test_revoked_key_holds_the_queue_then_recovers_AC25(run, supabase):
    app, reader = run.app()
    t = run.start(app)
    deadline = time.time() + 120
    while not any(201 in v for v in run.recorder.statuses.values()) and time.time() < deadline:
        time.sleep(0.1)  # wait until one order went through with the good key
    good = app.publisher._cfg
    if supabase is not None:
        supabase.api_key = "rotated"  # the key the assistant holds is revoked server-side
    else:
        app.publisher._cfg = replace(good, api_key="sk-revoked")  # real project: send a key it rejects
    deadline = time.time() + 120
    while app.coordinator.pipeline_state is not PipelineState.PUBLISHER_UNAUTHORIZED and time.time() < deadline:
        time.sleep(0.2)
    assert app.coordinator.pipeline_state is PipelineState.PUBLISHER_UNAUTHORIZED
    held = len(reader.acked)
    time.sleep(3)  # retries keep failing: nothing is acked, nothing is skipped
    assert len(reader.acked) == held
    assert any(r.levelno == logging.ERROR and "PUBLISHER_UNAUTHORIZED" in r.getMessage() for r in run.caplog.records)

    if supabase is not None:  # the operator fixes the key
        supabase.api_key = API_KEY
    else:
        app.publisher._cfg = good
    assert reader.wait_acked(len(run.chat), 300)
    assert app.coordinator.pipeline_state is PipelineState.OK
    run.stop(app, t)
    run.check_final(supabase)
