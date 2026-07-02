from __future__ import annotations

from datetime import date, datetime
import re


WHITESPACE_RE = re.compile(r"\s+")
MONEY_RE = re.compile(r"[^0-9]")


def clean_text(value: object) -> str:
    if value is None:
        return ""
    return WHITESPACE_RE.sub(" ", str(value)).strip()


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    text = value.strip()
    for fmt in ("%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"날짜 형식이 올바르지 않습니다: {value!r}")


def parse_money(value: str | None) -> int | None:
    if not value:
        return None
    digits = MONEY_RE.sub("", value)
    return int(digits) if digits else None


def unique_headers(headers: list[str]) -> list[str]:
    counts: dict[str, int] = {}
    result: list[str] = []
    for index, header in enumerate(headers, start=1):
        name = clean_text(header) or f"컬럼{index}"
        counts[name] = counts.get(name, 0) + 1
        result.append(name if counts[name] == 1 else f"{name}_{counts[name]}")
    return result

