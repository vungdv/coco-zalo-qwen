"""Day words of spec §1.4 found in the message text and resolved in code (T4a). Pure functions, no I/O.

The model is asked for a date too, but the stored date always comes from here: the resolved date when the message
names one day, the created date when it names none, and NotAnOrder when it names a past day, an invalid date, more
than one different day, or a day beyond the horizon.

Vietnamese is matched with and without diacritics, but only as a whole: a word matches when written exactly with its
diacritics or fully without them. So "mai" (tomorrow) does not match "mãi" or "mái", and "nay" (today) does not match
"này" (this). Plain forms that collide with common words need context: "mot" (= "một", one) is a day word only
before a quantity or an order word, like "kia".
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date

from zalo_order_assistant.order_extractor.calendar import (
    INVALID_DATE,
    PAST_DAY,
    check_range,
    day_month,
    day_of_month,
    next_weekday,
    plus_days,
)

SEVERAL_DATES = "several dates"


def plain(s: str) -> str:
    """Lowercase without diacritics: 'Ngày Mốt' -> 'ngay mot'."""
    s = unicodedata.normalize("NFD", s.lower()).replace("đ", "d")
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def _forms(*words: str) -> frozenset[str]:
    return frozenset({w for w in words} | {plain(w) for w in words})


WEEKDAYS = {  # date.weekday()
    **{w: 0 for w in _forms("hai", "2")},
    **{w: 1 for w in _forms("ba", "3")},
    **{w: 2 for w in _forms("tư", "4")},
    **{w: 3 for w in _forms("năm", "5")},
    **{w: 4 for w in _forms("sáu", "6")},
    **{w: 5 for w in _forms("bảy", "bẩy", "7")},
}
SHORT_WEEKDAYS = {"t2": 0, "t3": 1, "t4": 2, "t5": 3, "t6": 4, "t7": 5, "cn": 6}
NUMBER_WORDS = _forms("một", "hai", "ba", "bốn", "năm", "sáu", "bảy", "tám", "chín", "mười")
ORDER_WORDS = _forms(
    "đặt",
    "lấy",
    "cho",
    "order",
    "book",
    "đăng",
    "ship",
    "giao",
    "xin",
    "để",
    # change words (spec v2 §4.1): "kia hủy nhé", "kia bớt 1 phần"
    "hủy",
    "bớt",
    "giảm",
    "thêm",
    "sửa",
    "đổi",
    "chốt",
)
# "kia" after these is "that one" / "over there", never a day: "cái kia", "bên kia", "phần kia"
NOT_DAY_BEFORE_KIA = _forms(
    "cái",
    "bên",
    "phía",
    "đằng",
    "chỗ",
    "người",
    "món",
    "loại",
    "phần",
    "hộp",
    "suất",
    "xuất",
    "đơn",
    "con",
    "chiếc",
    "nhà",
    "bàn",
    "đó",
    "ấy",
)
VOCATIVE = _forms("ơi")

_DM = re.compile(r"(?<![\d/])(\d{1,2})/(\d{1,2})(?:/(\d{4}|\d{2}))?(?![\d/])")
_TOKEN = re.compile(r"\w+", re.UNICODE)
_CLAUSE_BREAK = re.compile(r"[,.;:!?\n()\-]")


@dataclass
class _Tok:
    text: str  # lowercase, with diacritics
    raw: str  # as written
    clause_start: bool

    def is_(self, *words: str) -> bool:
        """Exactly one of `words`, written with its diacritics or fully without them."""
        return self.text in _forms(*words)

    @property
    def is_quantity(self) -> bool:
        return self.text[:1].isdigit() or self.text in NUMBER_WORDS


@dataclass
class Scan:
    dates: list[date] = field(default_factory=list)
    past: bool = False
    invalid: bool = False

    def add(self, d: date | None) -> None:
        if d is None:
            self.invalid = True
        elif d not in self.dates:
            self.dates.append(d)


def _tokens(text: str) -> list[_Tok]:
    out: list[_Tok] = []
    last_end = 0
    for m in _TOKEN.finditer(text):
        gap = text[last_end : m.start()]
        out.append(_Tok(m.group().lower(), m.group(), not out or bool(_CLAUSE_BREAK.search(gap))))
        last_end = m.end()
    return out


def _before_quantity_or_order(toks: list[_Tok], i: int) -> bool:
    nxt = toks[i + 1] if i + 1 < len(toks) else None
    return nxt is not None and not nxt.clause_start and (nxt.is_quantity or nxt.text in ORDER_WORDS)


def _is_name_mai(toks: list[_Tok], i: int) -> bool:
    """'chị Mai ơi', 'cho Mai 2 phần': a person called Mai, not 'tomorrow'."""
    t = toks[i]
    nxt = toks[i + 1] if i + 1 < len(toks) else None
    if nxt is not None and nxt.text in VOCATIVE:
        return True
    return t.raw[:1].isupper() and not t.clause_start


def scan(text: str, created: date) -> Scan:
    s = Scan()
    for m in _DM.finditer(text):  # D/M and D/M/YYYY first, day before month
        d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
        s.add(day_month(created, d, mo, int(y) if y else None))
    toks = _tokens(_DM.sub(" ", text))
    i = 0
    while i < len(toks):
        t = toks[i]
        nxt = toks[i + 1] if i + 1 < len(toks) else None
        step = 1
        if t.is_("hôm", "bữa") and nxt is not None and nxt.is_("qua", "kia"):
            s.past, step = True, 2
        elif t.is_("tuần") and nxt is not None and nxt.is_("trước"):
            s.past, step = True, 2
        elif t.is_("hôm") and nxt is not None and nxt.is_("nay"):
            s.add(created)
            step = 2
        elif t.is_("ngày") and nxt is not None and nxt.is_("mai"):
            s.add(plus_days(created, 1))
            step = 2
        elif t.is_("ngày") and nxt is not None and nxt.is_("kia", "mốt"):
            s.add(plus_days(created, 2))
            step = 2
        elif t.is_("ngày") and nxt is not None and nxt.text.isdigit() and 1 <= int(nxt.text) <= 31:
            s.add(day_of_month(created, int(nxt.text)))
            step = 2
        elif t.is_("thứ") and nxt is not None and nxt.text in WEEKDAYS:
            s.add(next_weekday(created, WEEKDAYS[nxt.text]))
            step = 2
        elif t.is_("chủ") and nxt is not None and nxt.is_("nhật"):
            s.add(next_weekday(created, 6))
            step = 2
        elif t.text in SHORT_WEEKDAYS:
            s.add(next_weekday(created, SHORT_WEEKDAYS[t.text]))
        elif t.text == "nay":  # exactly "nay": "này" (this) is not a day word
            s.add(created)
        elif t.text == "mai" and not _is_name_mai(toks, i):
            s.add(plus_days(created, 1))
        elif t.text == "mốt" or (t.text == "mot" and _before_quantity_or_order(toks, i)):
            s.add(plus_days(created, 2))
        elif (
            t.text == "kia"
            and _before_quantity_or_order(toks, i)
            and (t.clause_start or toks[i - 1].text not in NOT_DAY_BEFORE_KIA)
        ):
            s.add(plus_days(created, 2))
        i += step
    return s


@dataclass(frozen=True)
class Resolution:
    date: date | None  # None: the message names no day (the caller uses the created date)
    reason: str | None  # set when the message is not an order because of its day words


def resolve(text: str, created: date, max_days_ahead: int) -> Resolution:
    s = scan(text, created)
    if s.past:
        return Resolution(None, PAST_DAY)
    if s.invalid:
        return Resolution(None, INVALID_DATE)
    if len(s.dates) > 1:
        return Resolution(None, SEVERAL_DATES)
    if not s.dates:
        return Resolution(None, None)
    d = s.dates[0]
    if reason := check_range(d, created, max_days_ahead):
        return Resolution(None, reason)
    return Resolution(d, None)
