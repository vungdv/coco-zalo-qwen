"""ZaloReader configuration (spec §2.7). Only the Reader knows about the browser and Zalo selectors."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ReaderConfig:
    zalo_chat: str = ""  # group chat title to open; "" = the chat already open (must be a group)
    backfill_days: float = 3.0
    poll_interval_seconds: float = 2.0
    selectors_path: Path | None = None  # None = the selectors.json shipped with this package
    zalo_url: str = "https://chat.zalo.me"
    pw_session: str = "zalo"
    pw_profile_dir: Path = Path("./data/browser-profile")
    state_db_path: Path = Path("./data/acked.db")
    browser_max_restarts: int = 3
    backfill_max_steps: int = 500
    backfill_settle_seconds: float = 0.6

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> ReaderConfig:
        e = os.environ if env is None else env
        d = cls()

        def get(name: str, default: object) -> str:
            v = e.get(name)
            return v if v not in (None, "") else str(default)

        sel = e.get("SELECTORS_PATH")
        return cls(
            zalo_chat=(e.get("ZALO_CHAT") or "").strip(),
            backfill_days=float(get("BACKFILL_DAYS", d.backfill_days)),
            poll_interval_seconds=float(get("POLL_INTERVAL_SECONDS", d.poll_interval_seconds)),
            selectors_path=Path(sel) if sel else None,
            zalo_url=get("ZALO_URL", d.zalo_url),
            pw_session=get("PW_SESSION", d.pw_session),
            pw_profile_dir=Path(get("PW_PROFILE_DIR", d.pw_profile_dir)),
            state_db_path=Path(get("STATE_DB_PATH", d.state_db_path)),
            browser_max_restarts=int(get("BROWSER_MAX_RESTARTS", d.browser_max_restarts)),
            backfill_max_steps=int(get("BACKFILL_MAX_STEPS", d.backfill_max_steps)),
            backfill_settle_seconds=float(get("BACKFILL_SETTLE_SECONDS", d.backfill_settle_seconds)),
        )
