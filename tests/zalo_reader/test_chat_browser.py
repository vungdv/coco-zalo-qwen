"""The chat search scripts in a real headless browser against a fixture page (run with `-m browser`).

Uses its own playwright-cli session, never the operator's Zalo session.
"""

from __future__ import annotations

import functools
import shutil
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from zalo_order_assistant.zalo_reader.browser import BrowserDriver
from zalo_order_assistant.zalo_reader.chat import ChatOpener
from zalo_order_assistant.zalo_reader.config import ReaderConfig
from zalo_order_assistant.zalo_reader.reader import load_selectors

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "zalo_chat_list.html"

pytestmark = pytest.mark.browser


@pytest.fixture(scope="module")
def driver(tmp_path_factory):
    if shutil.which("playwright-cli") is None:
        pytest.skip("playwright-cli not installed")
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(FIXTURE.parent))
    handler.log_message = lambda *a: None  # type: ignore[method-assign]
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    cfg = ReaderConfig(pw_session="zoa-test", pw_profile_dir=tmp_path_factory.mktemp("profile"))
    d = BrowserDriver(cfg)
    d._call(["-s=zoa-test", "open", f"http://127.0.0.1:{server.server_port}/{FIXTURE.name}"], 60)
    yield d
    d._call(["-s=zoa-test", "close"], 30)
    server.shutdown()


def test_opener_clicks_the_group_not_the_same_titled_private_chat(driver):
    opener = ChatOpener(driver, load_selectors(), settle_seconds=0.2)
    assert opener.open("bep phuong bac - dat com")
    assert driver.eval_json("() => JSON.stringify(document.getElementById('clicked').textContent)") == (
        "g2222222222222222222"
    )


def test_unknown_title_clicks_nothing_and_clears_the_search(driver):
    driver.eval_json("() => { document.getElementById('clicked').textContent = ''; return '1'; }")
    opener = ChatOpener(driver, load_selectors(), settle_seconds=0.2)
    assert not opener.open("Không có nhóm này")
    state = driver.eval_json(
        "() => JSON.stringify({clicked: document.getElementById('clicked').textContent,"
        " search: document.getElementById('contact-search-input').value})"
    )
    assert state == {"clicked": "", "search": ""}
