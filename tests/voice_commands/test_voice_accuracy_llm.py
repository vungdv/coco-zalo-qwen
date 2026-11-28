"""The whole understanding path with the real local model over the fixture set (spec v3 §8).

Run: uv run pytest -m llm tests/voice_commands. Writes reports/voice-accuracy-v3.md.
Pass bar: 0 wrong actions, and at least 95% of the commands that should work are done.
"""

import json
import os
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import pytest

from zalo_order_assistant.events import VN_TZ
from zalo_order_assistant.voice_commands.apply import plan
from zalo_order_assistant.voice_commands.llm import VoiceLlm
from zalo_order_assistant.voice_commands.model import Command, DayOrder, Failed
from zalo_order_assistant.voice_commands.names import resolve_customer
from zalo_order_assistant.voice_commands.parser import CommandParser

ROOT = Path(__file__).parents[2]
MODEL = os.environ.get("VOICE_OLLAMA_MODEL") or "qwen2.5:7b-instruct"


def run(parser, case, orders):
    command = Command("c", case["transcript"], "2026-11-28", datetime(2026, 11, 28, 9, tzinfo=VN_TZ), orders)
    parsed = parser.parse(command)
    if isinstance(parsed, Failed):
        return {"outcome": "failed", "error": parsed.error}
    order = plan(command, parsed, resolve_customer(parsed.customer, orders))
    if isinstance(order, Failed):
        return {"outcome": "failed", "error": order.error}
    return {"outcome": "done", "op": order.op.value, "qty": order.qty, "customer_id": order.customer_id}


@pytest.mark.llm
def test_voice_accuracy():
    cases = [json.loads(line) for line in (ROOT / "tests/fixtures/voice_commands.jsonl").read_text().splitlines()]
    orders = tuple(DayOrder(**o) for o in json.loads((ROOT / "tests/fixtures/voice_orders.json").read_text()))
    parser = CommandParser(VoiceLlm(MODEL, "http://localhost:11434", 60))
    rows, latencies, kinds = [], [], Counter()
    for case in cases:
        start = time.monotonic()
        got = run(parser, case, orders)
        latencies.append(time.monotonic() - start)
        want = case["expect"]
        if got == want:
            kinds["correct"] += 1
            kinds["done as expected"] += want["outcome"] == "done"
            continue
        # a wrong action changed an order differently from what was meant; anything else is a harmless failure
        kind = "wrong action" if got["outcome"] == "done" else (
            "missed" if want["outcome"] == "done" else "other error code")
        kinds[kind] += 1
        rows.append(f"| {case['transcript']} | {json.dumps(want, ensure_ascii=False)} | "
                    f"{json.dumps(got, ensure_ascii=False)} | {kind} |")
    valid = sum(c["expect"]["outcome"] == "done" for c in cases)
    done = kinds["done as expected"]
    latencies.sort()
    p95 = latencies[int(len(latencies) * 0.95)]
    report = [
        f"# Voice command accuracy ({MODEL})", "",
        f"- Cases: {len(cases)} ({valid} should change an order)",
        f"- Fully correct: {kinds['correct']} ({kinds['correct'] / len(cases):.0%})",
        f"- **Wrong actions: {kinds['wrong action']}**",
        f"- Valid commands done correctly: {done}/{valid} ({done / valid:.0%})",
        f"- Different error code only: {kinds['other error code']}",
        f"- Latency: median {latencies[len(latencies) // 2]:.1f}s, p95 {p95:.1f}s", "",
        "| Transcript | Expected | Got | Kind |", "|---|---|---|---|", *rows, "",
    ]
    (ROOT / "reports/voice-accuracy-v3.md").write_text("\n".join(report))
    assert kinds["wrong action"] == 0, "\n".join(rows)
    assert done / valid >= 0.95, "\n".join(rows)
