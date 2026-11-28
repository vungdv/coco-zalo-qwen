"""Which customer did the chef mean? Matching in code against that day's orders (spec v3 V6). Pure functions."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from zalo_order_assistant.order_extractor.day_words import plain
from zalo_order_assistant.voice_commands.model import DayOrder

HONORIFICS = frozenset(plain(w) for w in ("chị", "anh", "em", "cô", "chú", "bác", "bà", "ông", "bạn"))


@dataclass(frozen=True)
class Match:
    customer_id: str
    customer_name: str = field(repr=False)


@dataclass(frozen=True)
class NoMatch:
    pass


@dataclass(frozen=True)
class Ambiguous:
    names: tuple[str, ...] = field(repr=False)


def words(name: str) -> frozenset[str]:
    """Lowercase words, order ignored. Only a LEADING honorific is dropped: "Anh Minh" is Minh, but "Minh Anh"
    is someone else."""
    tokens = re.findall(r"\w+", unicodedata.normalize("NFC", name).lower())
    if len(tokens) > 1 and plain(tokens[0]) in HONORIFICS:
        tokens = tokens[1:]
    return frozenset(tokens)


def _bare(name: str) -> frozenset[str]:
    return frozenset(plain(w) for w in words(name))


Tier = tuple[Callable[[str], frozenset[str]], Callable[[frozenset[str], frozenset[str]], bool]]
TIERS: tuple[Tier, ...] = (
    (words, lambda said, order: said == order),
    (words, lambda said, order: said <= order),
    (_bare, lambda said, order: said <= order),  # accents ignored: a last resort
)


def resolve_customer(spoken: str, orders: Iterable[DayOrder]) -> Match | NoMatch | Ambiguous:
    customers = {o.customer_id: o.customer_name for o in orders}
    for norm, matches in TIERS:
        said = norm(spoken)
        if not said:
            break
        found = [(cid, name) for cid, name in customers.items() if matches(said, norm(name))]
        if len(found) == 1:
            return Match(*found[0])
        if found:
            return Ambiguous(tuple(name for _, name in found))
    return NoMatch()
