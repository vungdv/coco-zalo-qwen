"""OrderExtractor with a fake model: date rules, validation, failure kinds (FR-2 to FR-6b, AC-1 to AC-22)."""

from __future__ import annotations

from concurrent.futures import Executor, Future
from datetime import date, datetime

import pytest
import requests

from zalo_order_assistant.events import VN_TZ, ExtractFailed, FailureKind, Message, NotAnOrder, OrderExtracted
from zalo_order_assistant.order_extractor import ExtractorConfig, OrderExtractor
from zalo_order_assistant.order_extractor.calendar import (
    check_range,
    day_month,
    day_of_month,
    day_word_table,
    next_weekday,
    plus_days,
)
from zalo_order_assistant.order_extractor.llm import (
    Answer,
    LlmInvalid,
    LlmTimeout,
    LlmUnavailable,
    OllamaLlm,
    parse_answer,
    system_prompt,
)
from zalo_order_assistant.order_extractor.validate import Rejected, Valid, parse_qty, validate

MON = date(2026, 11, 23)  # Monday
D = date.fromisoformat


# --- calendar: every row of the §1.4 table --------------------------------------------------------------------------


def test_offsets():
    assert [plus_days(MON, n) for n in (0, 1, 2)] == [D("2026-11-23"), D("2026-11-24"), D("2026-11-25")]


@pytest.mark.parametrize(
    "weekday,expected",
    [
        (0, "2026-11-30"),
        (1, "2026-11-24"),
        (2, "2026-11-25"),
        (3, "2026-11-26"),
        (4, "2026-11-27"),
        (5, "2026-11-28"),
        (6, "2026-11-29"),
    ],
)
def test_weekday_is_strictly_after_created_AC10_AC11(weekday, expected):
    assert next_weekday(MON, weekday) == D(expected)


def test_weekday_across_year_end():
    assert next_weekday(date(2026, 12, 31), 4) == D("2027-01-01")  # Thu 31 Dec -> Fri 1 Jan


@pytest.mark.parametrize("day,expected", [(28, "2026-11-28"), (23, "2026-11-23"), (5, "2026-12-05"), (31, None)])
def test_day_of_month_AC20(day, expected):
    assert day_of_month(MON, day) == (D(expected) if expected else None)


def test_day_of_month_rolls_into_january():
    assert day_of_month(date(2026, 12, 20), 3) == D("2027-01-03")


@pytest.mark.parametrize(
    "created,args,expected",
    [
        (MON, (28, 11), "2026-11-28"),
        (MON, (28, 11, 2026), "2026-11-28"),
        (MON, (28, 11, 26), "2026-11-28"),
        (date(2026, 12, 1), (28, 11), "2027-11-28"),  # next year, then cut by the horizon (AC-21)
        (date(2026, 12, 30), (2, 1), "2027-01-02"),
        (MON, (31, 11), None),  # AC-22
        (MON, (30, 2), None),
        (MON, (5, 13), None),  # day first: 5/13 is not 13 May
    ],
)
def test_day_month(created, args, expected):
    assert day_month(created, *args) == (D(expected) if expected else None)


def test_range_rules():
    assert check_range(MON, MON, 31) is None
    assert check_range(D("2026-11-22"), MON, 31) == "past day"
    assert check_range(D("2026-12-24"), MON, 31) is None
    assert check_range(D("2026-12-25"), MON, 31) == "beyond horizon"
    assert check_range(D("2027-11-28"), date(2026, 12, 1), 31) == "beyond horizon"  # AC-21


def test_table_has_every_day_word_resolved_against_created():
    rows = dict(day_word_table(MON))
    assert rows["mai, ngày mai (tomorrow)"] == D("2026-11-24")
    assert rows["thứ hai, thứ 2, t2"] == D("2026-11-30") and rows["chủ nhật, cn"] == D("2026-11-29")


def test_prompt_states_created_date_weekday_and_table_and_never_yesterday():
    p = system_prompt(MON)
    assert "2026-11-23 (thứ hai)" in p and "2026-11-24" in p and "2026-11-27" in p
    assert "hôm qua" in p and "-> 2026-11-22" not in p  # past words are named as not-orders, never resolved
    assert system_prompt(date(2026, 11, 20)) != p  # computed fresh for each created date


# --- validation ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,qty",
    [
        (None, 1),
        ("", 1),
        (2, 2),
        ("3", 3),
        (2.0, 2),
        (0, None),
        (-1, None),
        (True, None),
        ("hai", None),
        (100, None),
        (1.5, None),
    ],
)
def test_qty(raw, qty):
    assert parse_qty(raw) == qty


@pytest.mark.parametrize(
    "args,expected",
    [
        ({"qty": 2, "date": "2026-11-24"}, Valid(2, D("2026-11-24"))),
        ({"qty": 2}, Valid(2, MON)),  # AC-13: no day word -> created date
        ({}, Valid(1, MON)),  # AC-2: no quantity -> 1
        ({"qty": 2, "date": ""}, Valid(2, MON)),
        ({"qty": 2, "date": "2026-11-22"}, Rejected("past day")),  # AC-5
        ({"qty": 2, "date": "2026-12-25"}, Rejected("beyond horizon")),
        ({"qty": 2, "date": "2026-11-31"}, Rejected("invalid date")),
        ({"qty": 2, "date": "24/11"}, Rejected("invalid date")),
        ({"qty": 2, "multipleOrders": True}, Rejected("several orders")),  # AC-14
        ({"qty": 0}, Rejected("bad quantity")),
    ],
)
def test_validate(args, expected):
    assert validate(args, MON, 31) == expected


# --- model client failure kinds -------------------------------------------------------------------------------------


class Resp:
    def __init__(self, status, body):
        self.status_code, self.body = status, body

    def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


class Session:
    def __init__(self, out):
        self.out, self.sent = out, None

    def post(self, url, json, timeout):
        self.sent = json
        if isinstance(self.out, Exception):
            raise self.out
        return self.out


def ollama(out) -> OllamaLlm:
    return OllamaLlm("qwen", "http://o", 5, session=Session(out))  # type: ignore[arg-type]


def content(s: str) -> Resp:
    return Resp(200, {"message": {"content": s}})


def test_ollama_answer():
    llm = ollama(content('{"tool": "record_order", "qty": 2}'))
    assert llm.classify("x", MON) == Answer(True, {"qty": 2})
    assert llm.http.sent["options"] == {"temperature": 0} and "2026-11-23" in llm.http.sent["messages"][0]["content"]


@pytest.mark.parametrize(
    "out,exc",
    [
        (requests.ConnectionError(), LlmUnavailable),
        (requests.ConnectTimeout(), LlmUnavailable),
        (Resp(404, {"error": "model not found"}), LlmUnavailable),
        (requests.ReadTimeout(), LlmTimeout),
        (Resp(500, {}), LlmTimeout),
        (Resp(400, {}), LlmInvalid),
        (content("not json"), LlmInvalid),
        (content('{"tool": "delete_all"}'), LlmInvalid),
        (content("[1]"), LlmInvalid),
        (Resp(200, {"oops": 1}), LlmInvalid),
        (Resp(200, ValueError()), LlmInvalid),
    ],
)
def test_ollama_failure_kinds(out, exc):
    with pytest.raises(exc):
        ollama(out).classify("x", MON)


def test_parse_answer():
    assert parse_answer({"tool": "ignore_message", "reason": "chat"}) == Answer(False, {"reason": "chat"})


# --- the colleague ---------------------------------------------------------------------------------------------------


class FakeLlm:
    def __init__(self, out):
        self.out, self.created = out, None

    def classify(self, text, created):
        self.created = created
        if isinstance(self.out, Exception):
            raise self.out
        return self.out


class Inline(Executor):
    def submit(self, fn, /, *args, **kwargs):
        f: Future = Future()
        f.set_result(fn(*args, **kwargs))
        return f


class Recorder:
    def __init__(self):
        self.events = []

    def notify(self, e):
        self.events.append(e)


def message(text="mai lấy 2 phần", sent_at=datetime(2026, 11, 23, 9, 30, tzinfo=VN_TZ), sender="Mai") -> Message:
    return Message("g1/1", "Bếp", sender, False, text, sent_at)


def run(out, **kw):
    return OrderExtractor(Recorder(), FakeLlm(out), executor=Inline()).extract_now(message(**kw))


def test_order_AC1():
    e = run(Answer(True, {"qty": 2}), text="Mai đặt 2 phần, ship nhé")
    assert isinstance(e, OrderExtracted)
    assert (e.order.key, e.order.who, e.order.qty, e.order.message) == ("g1/1", "Mai", 2, "Mai đặt 2 phần, ship nhé")


def test_reference_day_is_the_created_date_in_vietnam_not_utc():
    llm = FakeLlm(Answer(True, {"qty": 1}))
    ex = OrderExtractor(Recorder(), llm, executor=Inline())
    e = ex.extract_now(message(sent_at=datetime.fromisoformat("2026-11-22T17:30:00+00:00")))  # 00:30 on the 23rd in VN
    assert llm.created == D("2026-11-23") and isinstance(e, OrderExtracted) and e.order.date == "2026-11-23"


def test_backfilled_message_resolves_against_its_own_day_AC15():
    e = run(Answer(True, {"qty": 2, "date": "2026-11-21"}), sent_at=datetime(2026, 11, 20, 20, tzinfo=VN_TZ))
    assert isinstance(e, OrderExtracted) and e.order.date == "2026-11-21"


def test_text_is_cut_at_2000():
    e = run(Answer(True, {"qty": 1}), text="x" * 2500)
    assert isinstance(e, OrderExtracted) and len(e.order.message) == 2000


@pytest.mark.parametrize(
    "answer,reason",
    [
        (Answer(False, {"reason": "receipt"}), "model: receipt"),  # AC-16
        (Answer(False, {"reason": "Mai said: secret"}), "model: other"),  # model text never reaches the audit log
        (Answer(True, {"qty": 2, "date": "2026-11-22"}), "past day"),
    ],
)
def test_not_an_order(answer, reason):
    assert run(answer) == NotAnOrder("g1/1", reason)


@pytest.mark.parametrize(
    "exc,kind",
    [
        (LlmUnavailable("down"), FailureKind.SYSTEMIC),
        (LlmTimeout("timeout"), FailureKind.PER_MESSAGE),
        (LlmInvalid("not json"), FailureKind.PERMANENT),
    ],
)
def test_failure_kinds(exc, kind):
    e = run(exc)
    assert isinstance(e, ExtractFailed) and e.kind is kind and e.key == "g1/1"


@pytest.mark.parametrize("sent_at", [None, datetime(1970, 1, 1, 7, tzinfo=VN_TZ), datetime(2026, 11, 23, 9)])
def test_missing_or_bogus_created_date_is_permanent(sent_at):
    e = run(Answer(True, {"qty": 1}), sent_at=sent_at)
    assert e == ExtractFailed("g1/1", FailureKind.PERMANENT, "invalid created date")


def test_crash_is_reported_as_per_message():
    rec = Recorder()
    ex = OrderExtractor(rec, FakeLlm(RuntimeError("bug")), executor=Inline())
    ex.extract(message())
    assert rec.events[0].kind is FailureKind.PER_MESSAGE


def test_config_from_env():
    c = ExtractorConfig.from_env({"OLLAMA_MODEL": "m", "MAX_DAYS_AHEAD": "10", "LLM_TIMEOUT_SECONDS": ""})
    assert (c.ollama_model, c.max_days_ahead, c.timeout_seconds) == ("m", 10, 20.0)
