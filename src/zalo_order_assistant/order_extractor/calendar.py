"""Calendar rules of spec §1.4, relative to the message created date. Pure functions, no I/O, no system clock."""

from __future__ import annotations

from datetime import date, timedelta

WEEKDAY_NAMES = ["thứ hai", "thứ ba", "thứ tư", "thứ năm", "thứ sáu", "thứ bảy", "chủ nhật"]  # date.weekday() order
WEEKDAY_SHORT = ["t2", "t3", "t4", "t5", "t6", "t7", "cn"]

PAST_DAY = "past day"
BEYOND_HORIZON = "beyond horizon"
INVALID_DATE = "invalid date"


def plus_days(created: date, n: int) -> date:
    return created + timedelta(days=n)


def next_weekday(created: date, weekday: int) -> date:
    """The next such day strictly after `created`; the created date's own weekday means one week later."""
    return created + timedelta(days=(weekday - created.weekday() - 1) % 7 + 1)


def day_of_month(created: date, day: int) -> date | None:
    """'ngày D': this month if D is on or after the created day, otherwise next month. None if no such date."""
    year, month = created.year, created.month
    if day < created.day:
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    try:
        return date(year, month, day)
    except ValueError:
        return None


def day_month(created: date, day: int, month: int, year: int | None = None) -> date | None:
    """'D/M' or 'D/M/YYYY', day first. Without a year: this year if on or after `created`, else next year."""
    if year is not None:
        if year < 100:
            year += 2000
        try:
            return date(year, month, day)
        except ValueError:
            return None
    try:
        d = date(created.year, month, day)
    except ValueError:
        return None
    if d < created:
        try:
            d = date(created.year + 1, month, day)
        except ValueError:
            return None
    return d


def check_range(d: date, created: date, max_days_ahead: int) -> str | None:
    """None when `d` is an acceptable pre-order date, else the NotAnOrder reason."""
    if d < created:
        return PAST_DAY
    if (d - created).days > max_days_ahead:
        return BEYOND_HORIZON
    return None


def day_word_table(created: date) -> list[tuple[str, date]]:
    """The resolved day words that go into the prompt, computed fresh for each message."""
    rows: list[tuple[str, date]] = [
        ("hôm nay, nay (today)", created),
        ("mai, ngày mai (tomorrow)", plus_days(created, 1)),
        ("ngày kia, ngày mốt, mốt (day after tomorrow)", plus_days(created, 2)),
        (
            'kia, ONLY directly before a quantity or an order word ("kia 2 phần", "kia đặt 3 phần")',
            plus_days(created, 2),
        ),
    ]
    for wd in range(7):
        number = "" if wd == 6 else f", thứ {wd + 2}"
        rows.append((f"{WEEKDAY_NAMES[wd]}{number}, {WEEKDAY_SHORT[wd]}", next_weekday(created, wd)))
    return rows
