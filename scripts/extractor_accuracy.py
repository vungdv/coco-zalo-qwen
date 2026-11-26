"""Run the fixture set through the OrderExtractor with the real local model and write an accuracy report (T4).

    uv run python scripts/extractor_accuracy.py [--out reports/extractor-accuracy.md] [--label "after T4a"]

Needs Ollama running with OLLAMA_MODEL (default qwen2.5:7b-instruct). The fixtures are synthetic messages written
for this project, not real chat data. The report contains the fixture text, which is not customer data.
"""

from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import Executor, Future
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from zalo_order_assistant.events import ExtractFailed, Message, OrderExtracted
from zalo_order_assistant.order_extractor import ExtractorConfig, create_extractor

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "messages.jsonl"


class _Inline(Executor):
    def submit(self, fn, /, *args, **kwargs):
        f: Future = Future()
        f.set_result(fn(*args, **kwargs))
        return f


class _Null:
    def notify(self, e) -> None: ...


def load_cases(path: Path = FIXTURES) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def run_case(extractor, case: dict) -> dict:
    m = Message(
        f"fixture/{case['id']}", "fixture", "Khách", False, case["text"], datetime.fromisoformat(case["created"])
    )
    t0 = time.monotonic()
    e = extractor.extract_now(m)
    exp = case["expect"]
    got_order = isinstance(e, OrderExtracted)
    row = {
        "id": case["id"],
        "text": case["text"],
        "created": case["created"][:10],
        "expect": exp,
        "seconds": round(time.monotonic() - t0, 2),
    }
    if isinstance(e, ExtractFailed):
        row.update(got=f"FAILED {e.kind}: {e.reason}", ok_class=False, ok_op=False, ok_qty=False, ok_date=False)
    elif got_order:
        o = e.order
        row.update(
            got=f"{o.op.value} qty={o.qty} date={o.date}",
            ok_class=exp["order"],
            ok_op=exp["order"] and o.op.value == exp.get("op", "add"),
            ok_qty=exp["order"] and o.qty == exp["qty"],
            ok_date=exp["order"] and (exp["date"] is None or o.date == exp["date"]),
        )
    else:
        row.update(
            got=f"not an order ({e.reason})",
            ok_class=not exp["order"],
            ok_op=not exp["order"],
            ok_qty=not exp["order"],
            ok_date=not exp["order"],
        )
    row["ok"] = row["ok_class"] and row["ok_op"] and row["ok_qty"] and row["ok_date"]
    return row


def evaluate(config: ExtractorConfig | None = None) -> list[dict]:
    extractor = create_extractor(_Null(), config or ExtractorConfig.from_env())
    extractor._pool = _Inline()  # synchronous for the report
    return [run_case(extractor, c) for c in load_cases()]


def summary(rows: list[dict]) -> dict:
    orders = [r for r in rows if r["expect"]["order"]]
    adds = [r for r in orders if r["expect"].get("op", "add") == "add"]
    changes = [r for r in orders if r["expect"].get("op", "add") != "add"]
    non = [r for r in rows if not r["expect"]["order"]]

    def pct(xs: list[bool]) -> str:
        return f"{sum(xs)}/{len(xs)} ({100 * sum(xs) / max(len(xs), 1):.0f}%)"

    return {
        "all correct": pct([r["ok"] for r in rows]),
        "order vs not-an-order": pct([r["ok_class"] for r in rows]),
        "orders detected (recall)": pct([r["ok_class"] for r in orders]),
        "non-orders ignored": pct([r["ok_class"] for r in non]),
        "op correct on new orders (add)": pct([r["ok_op"] for r in adds]),
        "op correct on changes (reduce/set/cancel)": pct([r["ok_op"] for r in changes]),
        "add fully correct": pct([r["ok"] for r in adds]),
        "qty correct on orders": pct([r["ok_qty"] for r in orders]),
        "date correct on orders": pct([r["ok_date"] for r in orders]),
    }


def report(rows: list[dict], model: str, label: str) -> str:
    s = summary(rows)
    secs = sorted(r["seconds"] for r in rows)
    lines = [
        f"# Extractor accuracy: {label}",
        "",
        f"Model `{model}`, {len(rows)} synthetic fixture messages ([tests/fixtures/messages.jsonl]"
        "(../tests/fixtures/messages.jsonl)), run on " + datetime.now().strftime("%Y-%m-%d %H:%M") + ".",
        f"Median time per message {secs[len(secs) // 2]:.1f} s, slowest {secs[-1]:.1f} s.",
        "",
        "| Measure | Result |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in s.items()],
        "",
        "## Misses",
        "",
        "| # | Created | Message | Expected | Got |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        if not r["ok"]:
            exp = r["expect"]
            want = (
                f"{exp.get('op', 'add')} qty={exp['qty']} date={exp['date'] or 'any'}"
                if exp["order"]
                else f"no order ({exp['why']})"
            )
            text = r["text"].replace("|", "\\|")
            lines.append(f"| {r['id']} | {r['created']} | {text} | {want} | {r['got']} |")
    if all(r["ok"] for r in rows):
        lines.append("| | | none | | |")
    return "\n".join(lines) + "\n"


def main() -> None:
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "reports" / "extractor-accuracy.md"))
    ap.add_argument("--label", default="first run")
    args = ap.parse_args()
    cfg = ExtractorConfig.from_env()
    rows = evaluate(cfg)
    Path(args.out).write_text(report(rows, cfg.ollama_model, args.label), encoding="utf-8")
    for k, v in summary(rows).items():
        print(f"{k}: {v}")
    print(f"report: {args.out}")


if __name__ == "__main__":
    main()
