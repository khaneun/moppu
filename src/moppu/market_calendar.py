"""KRX(한국거래소) 휴장 판단 유틸.

`holidays` 패키지가 인식하는 대체공휴일·임시공휴일까지 포괄한다.
근로자의 날(5/1)은 공휴일로는 등록되지 않지만 KRX 휴장이므로 별도 보정.
"""

from __future__ import annotations

from datetime import date
from functools import lru_cache

import holidays


@lru_cache(maxsize=8)
def _kr_holidays(year: int) -> holidays.HolidayBase:
    return holidays.country_holidays("KR", years=[year])


def kr_holiday_name(d: date) -> str | None:
    """공휴일이면 이름, 아니면 None. 근로자의 날(5/1)도 포함."""
    name = _kr_holidays(d.year).get(d)
    if name:
        return name
    if d.month == 5 and d.day == 1:
        return "근로자의 날"
    return None


def is_kr_market_holiday(d: date) -> bool:
    """주말 또는 한국 공휴일(근로자의 날 포함)이면 True."""
    if d.weekday() >= 5:  # 토(5), 일(6)
        return True
    return kr_holiday_name(d) is not None
