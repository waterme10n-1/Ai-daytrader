"""Tal på dansk, som de står i bottenes beskeder: komma som decimaltegn og punktum mellem tusinder.

    num(1234.5)            → "1.234,50"
    num(3.456, 1, sign=True) → "+3,5"
    pct(-0.75)             → "-0,8 %"
    money(-12.3, "DKK")    → "-12,30 kr."
    short(2.0)             → "2"   (som :g, til grænser og indstillinger)
"""

from __future__ import annotations

import re

SIGNS = {"USD": "$", "DKK": "kr."}


def num(value: float | None, decimals: int = 2, *, sign: bool = False) -> str:
    if value is None:
        return "–"
    number = round(float(value), decimals)
    if number == 0:
        number = 0.0  # aldrig "-0,00"
    text = f"{abs(number):,.{decimals}f}".replace(",", "\x00").replace(".", ",").replace("\x00", ".")
    if number < 0:
        return "-" + text
    return ("+" if sign and number > 0 else "") + text


def pct(value: float | None, decimals: int = 1, *, sign: bool = True) -> str:
    return "–" if value is None else f"{num(value, decimals, sign=sign)} %"


def money(value: float | None, currency: str = "USD", *, sign: bool = False, decimals: int = 2) -> str:
    return "–" if value is None else f"{num(value, decimals, sign=sign)} {SIGNS.get(currency, currency)}"


def short(value: float | None) -> str:
    """Som formatet :g (2.0 → 2, 0.5 → 0,5), men med komma."""
    return "–" if value is None else format(float(value), "g").replace(".", ",")


def tidy(text: str) -> str:
    """Et beløb i kroner sidst i en sætning giver 'kr..'. Det bliver til ét punktum."""
    return re.sub(r"\bkr\.\.", "kr.", text)
