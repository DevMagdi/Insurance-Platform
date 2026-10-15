"""Validate the published structure of an Egyptian 14-digit national ID.

This is a syntactic/date/governorate validation, not an authoritative identity check.
"""

from __future__ import annotations

from datetime import date

# 01..35 are governorates historically assigned in the national ID scheme;
# 88 represents an Egyptian born abroad.
GOVERNORATE_CODES = {f"{code:02}" for code in range(1, 36)} | {"88"}


def validate_national_id(value: str) -> str:
    if len(value) != 14 or not value.isascii() or not value.isdecimal():
        raise ValueError("National ID must contain exactly 14 ASCII digits")
    if value[0] not in {"2", "3"}:
        raise ValueError("National ID has an invalid birth century")
    century = 1900 if value[0] == "2" else 2000
    try:
        born = date(century + int(value[1:3]), int(value[3:5]), int(value[5:7]))
    except ValueError as exc:
        raise ValueError("National ID contains an invalid birth date") from exc
    if born > date.today():
        raise ValueError("National ID birth date cannot be in the future")
    if value[7:9] not in GOVERNORATE_CODES:
        raise ValueError("National ID contains an unknown governorate code")
    return value
