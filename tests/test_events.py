from dataclasses import FrozenInstanceError
from datetime import datetime

import pytest

from zalo_order_assistant import ports
from zalo_order_assistant.events import VN_TZ, FailureKind, Message, Order, PublishFailed


def test_message_text_is_never_in_repr():
    m = Message("g1/1", "Shop", "Mai", False, "secret text", datetime(2026, 11, 23, 9, tzinfo=VN_TZ))
    o = Order("g1/1", "Mai", 2, "2026-11-24", "secret text")
    assert "secret text" not in repr(m) and "secret text" not in repr(o)


def test_events_are_immutable():
    e = PublishFailed("k", FailureKind.SYSTEMIC, 401)
    with pytest.raises(FrozenInstanceError):
        e.status = 200  # type: ignore[misc]


def test_ports_expose_the_four_protocols():
    assert {"MessageSource", "Extractor", "Publisher", "AuditLog", "Mediator"} <= set(dir(ports))
