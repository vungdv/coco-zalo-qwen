"""Load `selectors.json`, build the in-page reader script and validate what it returns. Ported from the spike."""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

REQUIRED_SELECTORS = frozenset(
    {
        "login_title",
        "chat_container",
        "scroller",
        "conversation_title",
        "group_subtitle",
        "chat_item",
        "self_item_class",
        "message_item",
        "qid_node",
        "qid_regex",
        "message_text",
        "sender_name",
    }
)


class SelectorsError(ValueError):
    pass


def load_selectors(path: str | Path | None = None) -> dict[str, str]:
    """`path` None loads the selectors.json shipped with this package."""
    try:
        if path is None:
            path = "selectors.json (packaged)"
            raw = resources.files(__package__).joinpath("selectors.json").read_text(encoding="utf-8")
        else:
            raw = Path(path).read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise SelectorsError(f"cannot read selectors from {path}: {exc}") from exc
    if not isinstance(data, dict) or not all(isinstance(v, str) for v in data.values()):
        raise SelectorsError(f"{path} must be a JSON object of strings")
    missing = sorted(REQUIRED_SELECTORS - data.keys())
    if missing:
        raise SelectorsError(f"{path} lacks required selectors: {', '.join(missing)}")
    return data


def build_reader_script(selectors: dict[str, str]) -> str:
    src = resources.files(__package__).joinpath("reader.js").read_text(encoding="utf-8")
    return (
        "() => {\n"
        f"const SEL = {json.dumps(selectors, ensure_ascii=False)};\n"
        f"{src}\n"
        "try { return JSON.stringify(readState(SEL)); }\n"
        'catch (e) { return JSON.stringify({state: "ERROR", error: String((e && e.message) || e)}); }\n'
        "}"
    )


def build_scroll_script(selectors: dict[str, str], dy: int | None) -> str:
    """Script that scrolls the message list by `dy` pixels (None = to the bottom) and returns its position."""
    target = "el.scrollHeight" if dy is None else f"el.scrollTop + ({int(dy)})"
    return (
        "() => {\n"
        f"const el = document.querySelector({json.dumps(selectors['scroller'])});\n"
        "if (!el) return JSON.stringify({ok: false});\n"
        f"el.scrollTop = {target};\n"
        "return JSON.stringify({ok: true, scroll_top: el.scrollTop, client_height: el.clientHeight});\n"
        "}"
    )


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Conversation(_Lenient):
    key: str = Field(min_length=1)
    title: str | None = None
    is_group: bool = False


class ReaderMessage(_Lenient):
    message_key: str = Field(min_length=1)
    sent_at_ms: int = Field(ge=0)
    sender_uid: str
    sender: str | None = None
    is_self: bool = False
    kind: str = "other"
    text: str | None = None


class PageState(_Lenient):
    state: Literal["LOGIN", "NO_CHAT", "READING", "ERROR"]
    conversation: Conversation | None = None
    senders: dict[str, str] = Field(default_factory=dict)
    messages: list[ReaderMessage] = Field(default_factory=list)
    rendered_bubbles: int = 0
    scroll_top: float = 0
    client_height: float = 0
    drift: str | None = None
    error: str | None = None
