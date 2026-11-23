"""ZaloReader colleague: Zalo Web through playwright-cli. Only this package knows the browser and the selectors."""

from zalo_order_assistant.ports import Mediator
from zalo_order_assistant.zalo_reader.adapter import ZaloWebAdapter
from zalo_order_assistant.zalo_reader.browser import BrowserDriver
from zalo_order_assistant.zalo_reader.config import ReaderConfig
from zalo_order_assistant.zalo_reader.reader import build_reader_script, build_scroll_script, load_selectors
from zalo_order_assistant.zalo_reader.source import ZaloReader
from zalo_order_assistant.zalo_reader.store import AckedStore


def create_reader(mediator: Mediator, config: ReaderConfig) -> ZaloReader:
    selectors = load_selectors(config.selectors_path)
    driver = BrowserDriver(config)
    adapter = ZaloWebAdapter(
        driver,
        AckedStore(config.state_db_path),
        build_reader_script(selectors),
        scroll_script=lambda dy: build_scroll_script(selectors, dy),
        backfill_max_steps=config.backfill_max_steps,
        backfill_days=config.backfill_days,
        settle_seconds=config.backfill_settle_seconds,
    )
    return ZaloReader(mediator, adapter, driver, poll_interval_seconds=config.poll_interval_seconds)


__all__ = ["ReaderConfig", "ZaloReader", "create_reader"]
