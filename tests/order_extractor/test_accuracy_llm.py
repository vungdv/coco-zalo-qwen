"""Fixture set against the real local model (T4). Run with `uv run pytest -m llm`. No pass mark yet (risk R2)."""

import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from extractor_accuracy import evaluate, load_cases, summary  # noqa: E402


def test_fixture_set_has_60_plus_cases_with_counter_examples():
    cases = load_cases()
    texts = {c["text"] for c in cases}
    assert len(cases) >= 60
    assert {
        "đã nhận 2 phần rồi nhé",
        "cho mình 2 phần cái kia",
        "bên kia còn 2 phần không chị",
        "31/11 2 phần",
        "28/11 2 phần",
        "mai nhé, trưa mai 2 phần",
    } <= texts


@pytest.mark.llm
def test_accuracy_against_local_model():
    try:
        requests.get("http://localhost:11434/api/tags", timeout=2)
    except requests.RequestException:
        pytest.skip("Ollama is not running")
    rows = evaluate()
    print(summary(rows))
    assert all(not r["got"].startswith("FAILED") for r in rows)
