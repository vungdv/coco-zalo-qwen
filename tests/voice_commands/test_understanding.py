"""Names and code checks (spec v3 V3, V5, V6, §6), no model and no I/O."""

from datetime import date

import pytest

from zalo_order_assistant.voice_commands.checks import check, numbers
from zalo_order_assistant.voice_commands.model import DayOrder, Failed, Parsed
from zalo_order_assistant.voice_commands.names import Ambiguous, Match, NoMatch, resolve_customer

ORDERS = [
    DayOrder("zalo:111", "Lê Hương"), DayOrder("zalo:222", "Hương Giang"), DayOrder("zalo:333", "Chị Mai"),
    DayOrder("hoàng nam", "Hoàng Nam"), DayOrder("zalo:555", "Anh Minh"),
]
CREATED = date(2026, 11, 28)


@pytest.mark.parametrize(
    ("said", "customer_id"),
    [("Lê Hương", "zalo:111"), ("hương lê", "zalo:111"), ("Le Huong", "zalo:111"), ("Lê Hường", "zalo:111"),
     ("Mai", "zalo:333"), ("chi mai", "zalo:333"), ("Minh", "zalo:555"), ("Giang", "zalo:222")],
)
def test_given_a_name_variant_then_it_finds_that_customer(said, customer_id):
    who = resolve_customer(said, ORDERS)
    assert isinstance(who, Match) and who.customer_id == customer_id


def test_given_hương_with_two_such_customers_then_ambiguous_so_nobody_elses_food_changes():
    who = resolve_customer("Hương", ORDERS)
    assert who == Ambiguous(("Lê Hương", "Hương Giang"))


def test_given_minh_anh_then_it_is_not_anh_minh():
    assert resolve_customer("Minh Anh", ORDERS) == NoMatch()


def test_given_an_exact_name_then_it_wins_over_partial_ones():
    who = resolve_customer("Hương", [*ORDERS, DayOrder("walk", "Hương")])
    assert isinstance(who, Match) and who.customer_id == "walk"


def test_given_two_orders_of_one_customer_then_they_count_once():
    who = resolve_customer("Mai", [*ORDERS, DayOrder("zalo:333", "Chị Mai", delivered=True)])
    assert isinstance(who, Match)


@pytest.mark.parametrize(
    ("text", "values"),
    [("4s", [4]), ("bốn suất", [4]), ("mười hai", [12]), ("hai mươi lăm", [25]), ("mười", [10]),
     ("2 suất 12h", [2, 12]), ("không lấy", [])],
)
def test_numbers_written_in_the_text(text, values):
    assert numbers(text) == values


def checked(text, customer, op, qty=None, day="2026-11-28"):
    return check(Parsed(customer, op, qty), text, day, CREATED)


@pytest.mark.parametrize(
    ("text", "customer", "op", "qty"),
    [
        ("Lê Hương, 4 suất", "Lê Hương", "total", 4),
        ("Mai bớt 1", "Mai", "reduce", 1),  # "mai" is the name here, not tomorrow (V3)
        ("Anh Huy 3 suất", "Anh Huy", "total", 3),  # "Huy" is not the word "huỷ"
        ("Chị Ba thêm 2", "Chị Ba", "add", 2),  # "Ba" is not the number three
        ("Lan không lấy nữa", "Lan", "cancel", None),
        ("Lan xoá đơn", "Lan", "delete", None),
    ],
)
def test_given_what_was_said_then_it_passes(text, customer, op, qty):
    assert checked(text, customer, op, qty) == Parsed(customer, op, qty)


@pytest.mark.parametrize(
    ("text", "customer", "op", "qty", "detail"),
    [
        ("Lê Hương 4 suất", "Hương Giang", "total", 4, "name not said"),
        ("Lê Hương 5 suất", "Lê Hương", "add", 5, "unclear change"),  # no "thêm"
        ("Lê Hương thêm 5", "Lê Hương", "total", 5, "unclear change"),
        ("Lê Hương bớt 1 thêm 2", "Lê Hương", "add", 2, "unclear change"),
        ("Lê Hương thêm", "Lê Hương", "add", 1, "bad quantity"),  # the model invented the 1
        ("Lê Hương 4 suất", "Lê Hương", "total", 14, "bad quantity"),
        ("Lê Hương 150 suất", "Lê Hương", "total", 150, "bad quantity"),
        ("Lê Hương thêm 0", "Lê Hương", "add", 0, "bad quantity"),
        ("Lê Hương huỷ 1 phần", "Lê Hương", "cancel", None, "bad quantity"),  # not a whole-order cancel
        ("Lê Hương 4 suất", "Lê Hương", "total", True, "bad quantity"),
        ("Lê Hương mai 2 suất", "Lê Hương", "total", 2, "day_word"),
    ],
)
def test_given_something_the_text_does_not_say_then_refused(text, customer, op, qty, detail):
    assert checked(text, customer, op, qty) == Failed("not_understood", detail)


def test_given_the_day_said_is_the_day_on_screen_then_it_passes():
    assert checked("Lê Hương mai 2 suất", "Lê Hương", "total", 2, "2026-11-29") == Parsed("Lê Hương", "total", 2)
