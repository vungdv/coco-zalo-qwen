"""Thin wrapper over `playwright-cli`: headed, persistent session, bounded timeouts, restart counting.

Ported from the zalo-adater spike.

Every call is an argv list (never a shell) with a timeout; JS is one argv element. Error messages never include page
output, which can hold message text.
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Callable, Sequence
from typing import Any, Protocol

from zalo_order_assistant.zalo_reader.config import ReaderConfig

log = logging.getLogger(__name__)

EVAL_TIMEOUT_SECONDS = 10.0
OPEN_TIMEOUT_SECONDS = 60.0

Runner = Callable[..., subprocess.CompletedProcess[str]]


class BrowserError(RuntimeError):
    """A `playwright-cli` call failed (non-zero exit, timeout, missing binary)."""


class EvalError(BrowserError):
    """`eval` ran but did not return JSON."""


class Driver(Protocol):
    def ensure_open(self) -> None: ...
    def eval_json(self, js: str) -> Any: ...
    def relaunch(self) -> bool: ...  # True while the caller may keep going


class BrowserDriver:
    def __init__(self, settings: ReaderConfig, *, cli: str = "playwright-cli", runner: Runner = subprocess.run) -> None:
        self.s = settings
        self.cli = cli
        self._run = runner
        self.consecutive_failures = 0

    def _call(self, args: Sequence[str], timeout: float) -> str:
        try:
            proc = self._run([self.cli, *args], capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            raise BrowserError(f"{self.cli} timed out after {timeout:g}s") from exc
        except OSError as exc:
            raise BrowserError(f"cannot run {self.cli}: {exc}") from exc
        if proc.returncode != 0:
            raise BrowserError(f"{self.cli} exited {proc.returncode}")
        return proc.stdout

    def _sess(self, *args: str) -> list[str]:
        return [f"-s={self.s.pw_session}", *args]

    def is_open(self) -> bool:
        out = self._call(["list", "--json"], EVAL_TIMEOUT_SECONDS)
        try:
            browsers = json.loads(out).get("browsers", [])
        except (json.JSONDecodeError, AttributeError) as exc:
            raise EvalError("unexpected `list --json` output") from exc
        return any(b.get("name") == self.s.pw_session and b.get("status") == "open" for b in browsers)

    def ensure_open(self) -> None:
        if self.is_open():
            return
        log.warning("browser_session_missing session=%s", self.s.pw_session)
        self._call(
            self._sess("open", self.s.zalo_url, "--headed", "--persistent", f"--profile={self.s.pw_profile_dir}"),
            OPEN_TIMEOUT_SECONDS,
        )

    def eval_json(self, js: str) -> Any:
        out = self._call(self._sess("--raw", "eval", js), EVAL_TIMEOUT_SECONDS).strip()
        try:
            value = json.loads(out)
            if isinstance(value, str):
                value = json.loads(value)
        except json.JSONDecodeError:
            raise EvalError("eval returned non-JSON output") from None
        self.consecutive_failures = 0
        return value

    def relaunch(self) -> bool:
        self.consecutive_failures += 1
        if self.consecutive_failures > self.s.browser_max_restarts:
            log.error("browser_restarts_exhausted attempts=%d", self.consecutive_failures)
            return False
        try:
            self.ensure_open()
        except BrowserError as exc:
            log.error("browser_relaunch_failed attempt=%d error=%s", self.consecutive_failures, exc)
        return True
