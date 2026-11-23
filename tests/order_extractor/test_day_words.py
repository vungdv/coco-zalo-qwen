"""Date resolver in code (T4a): every rule of spec §1.4 by message text, AC-8 to AC-22, and model disagreement."""

from __future__ import annotations

import logging
from concurrent.futures import Executor, Future
from datetime import date, datetime

import pytest

from zalo_order_assistant.events import VN_TZ, Message, NotAnOrder, OrderExtracted
from zalo_order_assistant.order_extractor import OrderExtractor
from zalo_order_assistant.order_extractor.day_words import Resolution, plain, resolve
from zalo_order_assistant.order_extractor.llm import Answer

MON = date(2026, 11, 23)
D = date.fromisoformat


def r(text: str, created: date = MON) -> date | str | None:
    res = resolve(text, created, 31)
    return res.reason or (res.date.isoformat() if res.date else None)


@pytest.mark.parametrize(
    "text,expected",
    [
        # AC-8 .. AC-13
        ("mai lấy 2 phần", "2026-11-24"),
        ("ngày kia 3 phần", "2026-11-25"),
        ("mốt 3 phần", "2026-11-25"),
        ("ngày mốt 3 phần", "2026-11-25"),
        ("thứ sáu 1 phần", "2026-11-27"),
        ("thứ hai 1 phần", "2026-11-30"),
        ("hôm nay 1 phần", "2026-11-23"),
        ("2 phần", None),
        # AC-14, AC-17, AC-18, AC-19, AC-20, AC-22
        ("mai 2 phần, kia 1 phần", "several dates"),
        ("kia 2 phần", "2026-11-25"),
        ("kia đặt 3 phần", "2026-11-25"),
        ("cho mình 2 phần cái kia", None),
        ("mai nhé, trưa mai 2 phần", "2026-11-24"),
        ("ngày 28 2 phần", "2026-11-28"),
        ("ngày 23 2 phần", "2026-11-23"),
        ("ngày 5 2 phần", "2026-12-05"),
        ("31/11 2 phần", "invalid date"),
        ("30/2 cho mình 1 phần", "invalid date"),
        ("ngày 31 1 phần", "invalid date"),  # 31 Nov does not exist
        # every other row of the table
        ("nay 1 phần", "2026-11-23"),
        ("tối nay cho e 1 phần", "2026-11-23"),
        ("ngày mai 2 phần", "2026-11-24"),
        ("thứ ba", "2026-11-24"),
        ("thứ tư", "2026-11-25"),
        ("thứ năm", "2026-11-26"),
        ("thứ bảy", "2026-11-28"),
        ("thứ 7", "2026-11-28"),
        ("thứ 2 tuần sau mình đặt 2 phần", "2026-11-30"),
        ("chủ nhật", "2026-11-29"),
        ("t2 1 phần", "2026-11-30"),
        ("T6 1 phần", "2026-11-27"),
        ("cn cho mình 2 phần", "2026-11-29"),
        ("28/11 đặt 2 phần", "2026-11-28"),
        ("28/11/2026 2 phần", "2026-11-28"),
        ("1/12 2 phần", "2026-12-01"),
        # past words are not orders (AC-5)
        ("hôm qua mình đặt 2 phần", "past day"),
        ("hom qua minh dat 2 phan", "past day"),
        ("hôm kia đặt 3 phần mà chưa thấy giao", "past day"),
        ("tuần trước ăn 2 phần ngon lắm", "past day"),
        ("20/11/2026 2 phần", "past day"),
        # the time of day does not change the date
        ("trưa mai 1 phần", "2026-11-24"),
        ("sáng mai, chiều mai, tối mai", "2026-11-24"),
    ],
)
def test_resolution(text, expected):
    assert r(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("mai 2 phan", "2026-11-24"),
        ("ngay mai cho minh 3 phan", "2026-11-24"),
        ("ngay kia 2 phan nha", "2026-11-25"),
        ("ngay mot 2 phan", "2026-11-25"),
        ("thu sau 1 phan", "2026-11-27"),
        ("thu bay", "2026-11-28"),
        ("chu nhat 2 phan", "2026-11-29"),
        ("hom nay 1 phan", "2026-11-23"),
        ("NGÀY MAI 2 PHẦN", "2026-11-24"),
    ],
)
def test_without_diacritics_and_any_case(text, expected):
    assert r(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "cho mình 2 phần cái kia",  # AC-18
        "bên kia còn 2 phần không chị",
        "2 phần kia",
        "phần kia 2 hộp",
        "kìa chị ơi 2 phần",  # "kìa" (look!) is another word
        "cho e 2 phần cái này",  # "này" (this) is not "nay" (today)
        "ngon mãi 2 phần",  # "mãi" is not "mai"
        "mot phan com",  # plain "một phần": one portion, not "mốt"
        "một phần",
        "chị Mai ơi cho e 2 phần",  # Mai the person
        "chi mai oi cho e 2 phan",
        "cho Mai 2 phần",
        "Lan với Mai 2 phần",
    ],
)
def test_words_that_look_like_day_words_but_are_not(text):
    assert r(text) is None


def test_plain_mot_counts_before_a_quantity():
    assert r("mot 2 phan") == "2026-11-25"


def test_kia_after_a_clause_break_is_a_day_word():
    assert r("ok chị, kia 2 phần nhé") == "2026-11-25"


def test_horizon_AC21():
    assert r("28/11 2 phần", D("2026-12-01")) == "beyond horizon"
    assert r("ngày 23 2 phần", D("2026-11-24")) == "2026-12-23"  # 29 days ahead: fine
    assert r("24/12 2 phần") == "2026-12-24" and r("25/12 2 phần") == "beyond horizon"


def test_year_end():
    assert r("mai 2 phần", D("2026-12-31")) == "2027-01-01"
    assert r("2/1 2 phần", D("2026-12-30")) == "2027-01-02"
    assert r("ngày 3 2 phần", D("2026-12-20")) == "2027-01-03"
    assert r("thứ sáu", D("2026-12-31")) == "2027-01-01"


def test_same_day_written_two_ways_is_one_day():
    assert r("thứ ba, mai 2 phần") == "2026-11-24"
    assert r("24/11 tức là mai 2 phần") == "2026-11-24"


def test_several_different_days():
    assert r("2 phần cho mai, 3 phần cho thứ 6") == "several dates"
    assert r("hôm nay 1, mai 1") == "several dates"


def test_plain():
    assert plain("Ngày Mốt đặt") == "ngay mot dat"


def test_resolution_type():
    assert resolve("mai", MON, 31) == Resolution(D("2026-11-24"), None)


# --- the extractor uses the resolver, not the model, for the date ----------------------------------------------


class FakeLlm:
    def __init__(self, answer):
        self.answer, self.calls = answer, 0

    def classify(self, text, created):
        self.calls += 1
        return self.answer


class Inline(Executor):
    def submit(self, fn, /, *args, **kwargs):
        f: Future = Future()
        f.set_result(fn(*args, **kwargs))
        return f


def extract(text: str, answer: Answer, created: datetime = datetime(2026, 11, 23, 9, tzinfo=VN_TZ)):
    llm = FakeLlm(answer)
    ex = OrderExtractor(None, llm, executor=Inline())  # type: ignore[arg-type]
    return ex.extract_now(Message("g1/1", "Bếp", "Mai", False, text, created)), llm


@pytest.mark.parametrize(
    "text,model_date,expected",
    [
        ("thứ sáu 1 phần", "2026-11-29", "2026-11-27"),  # model miscounts the weekday: corrected
        ("thứ hai 1 phần", "2026-11-23", "2026-11-30"),  # model picks today: corrected to next Monday
        ("2 phần", "2026-11-24", "2026-11-23"),  # model invents a day: the created date wins (FR-6a)
        ("mai 2 phần", None, "2026-11-24"),  # model misses the day word
        ("mai 2 phần", "garbage", "2026-11-24"),
        ("ngày 5 2 phần", "2026-11-28", "2026-12-05"),
    ],
)
def test_model_date_is_corrected_by_the_resolver(text, model_date, expected, caplog):
    caplog.set_level(logging.INFO)
    args = {"qty": 1} | ({"date": model_date} if model_date else {})
    e, _ = extract(text, Answer(True, args))
    assert isinstance(e, OrderExtracted) and e.order.date == expected
    assert any("date_corrected" in rec.getMessage() for rec in caplog.records) or model_date is None


@pytest.mark.parametrize(
    "text,reason",
    [
        ("hôm qua mình đặt 2 phần", "past day"),  # AC-5
        ("mai 2 phần, kia 1 phần", "several dates"),  # AC-14
        ("31/11 2 phần", "invalid date"),  # AC-22
    ],
)
def test_day_word_rejections_never_reach_the_model(text, reason):
    e, llm = extract(text, Answer(True, {"qty": 2}))
    assert e == NotAnOrder("g1/1", reason) and llm.calls == 0


def test_beyond_horizon_AC21():
    e, _ = extract("28/11 2 phần", Answer(True, {"qty": 2}), datetime(2026, 12, 1, 10, tzinfo=VN_TZ))
    assert e == NotAnOrder("g1/1", "beyond horizon")


def test_backfilled_message_keeps_its_own_day_AC15():
    e, _ = extract("mai 2 phần", Answer(True, {"qty": 2}), datetime(2026, 11, 20, 20, tzinfo=VN_TZ))
    assert isinstance(e, OrderExtracted) and e.order.date == "2026-11-21"
