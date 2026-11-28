"""The model's answer checked against the words of the command (spec v3 V3, V5, §4.3). Pure functions.

A wrong action changes someone's food, a failure only costs the chef a second try: whatever the text does not
plainly say is refused. Name words are cut out before looking for op, number and day words, because names such as
"Huy" (plain "huỷ"), "Mai" (tomorrow) or "Ba" (three) are also command words.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date

from zalo_order_assistant.order_extractor.day_words import plain, resolve
from zalo_order_assistant.voice_commands.model import Failed, Parsed

NOT_UNDERSTOOD = "not_understood"
OPS = ("total", "add", "reduce", "cancel", "delete")
_TOKEN = re.compile(r"\w+")


def _forms(*words: str) -> frozenset[str]:
    """Written with its diacritics, or fully without them."""
    return frozenset(words) | frozenset(plain(w) for w in words)


OP_WORDS = {
    "add": _forms("thêm"),
    "reduce": _forms("bớt", "giảm", "trừ"),
    "cancel": _forms("huỷ", "hủy"),
    "delete": _forms("xoá", "xóa"),
}
DIGITS = {
    word: value
    for value, names in enumerate(
        (("một", "mốt"), ("hai",), ("ba",), ("bốn", "tư"), ("năm", "lăm"), ("sáu",), ("bảy", "bẩy"), ("tám",),
         ("chín",)),
        start=1,
    )
    for word in _forms(*names)
}
TEN, TENS = _forms("mười"), _forms("mươi")


def _tokens(text: str) -> list[str]:
    return [m.group().lower() for m in _TOKEN.finditer(text)]


def without_name(transcript: str, name: str) -> str | None:
    """The transcript with the name cut out, or None when the name is not written in it (accents optional)."""
    found = list(_TOKEN.finditer(transcript))
    said = [plain(m.group()) for m in found]
    wanted = [plain(t) for t in _tokens(name)]
    if not wanted:
        return None
    for i in range(len(said) - len(wanted) + 1):
        if said[i : i + len(wanted)] == wanted:
            return transcript[: found[i].start()] + " " + transcript[found[i + len(wanted) - 1].end() :]
    return None


def numbers(text: str) -> list[int]:
    """Every number written in the text: digits ("4", "4s") and number words, "mười hai" and "hai mươi" included."""
    tokens = _tokens(text)
    out: list[int] = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        digits = re.match(r"\d+", t)
        if digits:
            out.append(int(digits.group()))
        elif t in TEN or (t in DIGITS and i + 1 < len(tokens) and tokens[i + 1] in TENS):
            if t in TEN:
                value = 10
            else:
                value = DIGITS[t] * 10
                i += 1
            if i + 1 < len(tokens) and tokens[i + 1] in DIGITS:
                value += DIGITS[tokens[i + 1]]
                i += 1
            out.append(value)
        elif t in DIGITS:
            out.append(DIGITS[t])
        i += 1
    return out


def said_ops(text: str) -> set[str]:
    tokens = _tokens(text)
    found = {op for op, words in OP_WORDS.items() if words & set(tokens)}
    if any(a in _forms("không") and b in _forms("lấy") for a, b in zip(tokens, tokens[1:], strict=False)):
        found.add("cancel")
    return found


def check(answer: Parsed, transcript: str, delivery_date: str, created: date) -> Parsed | Failed:
    """V5 (a) name, (b) op word, (c) number, then V3: a day said must be the day on screen."""
    transcript = unicodedata.normalize("NFC", transcript)
    rest = without_name(transcript, unicodedata.normalize("NFC", answer.customer))
    if rest is None:
        return Failed(NOT_UNDERSTOOD, "name not said")
    if answer.op not in OPS or said_ops(rest) != ({answer.op} if answer.op != "total" else set()):
        return Failed(NOT_UNDERSTOOD, "unclear change")
    day = resolve(rest, created, 366)
    if day.reason or (day.date is not None and day.date.isoformat() != delivery_date):
        return Failed(NOT_UNDERSTOOD, "day_word")
    if day.date is not None:  # "ngày 28", "thứ 7": its digits are not the quantity, so refuse rather than guess
        rest = re.sub(r"(?i)\b(ngày|ngay|thứ|thu)\s+\w+|\d+/\d+(/\d+)?", " ", rest)
    said = numbers(rest)
    if answer.op in ("cancel", "delete"):
        # "huỷ 1 phần" is not a whole-order cancel
        return Parsed(answer.customer, answer.op) if not said else Failed(NOT_UNDERSTOOD, "bad quantity")
    low = 0 if answer.op == "total" else 1
    qty = answer.qty
    if type(qty) is not int or not low <= qty <= 99 or set(said) != {qty}:
        return Failed(NOT_UNDERSTOOD, "bad quantity")
    return Parsed(answer.customer, answer.op, qty)
