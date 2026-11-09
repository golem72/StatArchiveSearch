# -*- coding: utf-8 -*-
"""Нормализация периодов: месяц/диапазон/квартал/год + базис сравнения."""
import re

MONTHS = {
    "янв": 1, "январ": 1, "январь": 1, "января": 1, "январю": 1, "январе": 1,
    "февр": 2, "февраль": 2, "февраля": 2, "февралю": 2, "феврале": 2,
    "март": 3, "марта": 3, "марту": 3, "марте": 3,
    "апрел": 4, "апреля": 4, "апрелю": 4, "апреле": 4,
    "май": 5, "мае": 5, "мая": None,           # «мая» неоднозначно
    "июн": 6, "июня": 6, "июню": 6, "июне": 6,
    "июл": 7, "июля": 7, "июлю": 7, "июле": 7,
    "авг": 8, "август": 8, "августа": 8, "августу": 8, "августе": 8,
    "сент": 9, "сентябр": 9, "сентября": 9, "сентябрю": 9, "сентябре": 9,
    "дек": 12, "декабр": 12, "декабря": 12, "декабрю": 12, "декабре": 12, "декабрь": 12,
    "окт": 10, "октябр": 10, "октября": 10, "октябрю": 10, "октябре": 10, "октябрь": 10,
    "нояб": 11, "ноябр": 11, "ноября": 11, "ноябрю": 11, "ноябре": 11, "ноябрь": 11,
    "сент": 9, "сентябр": 9, "сентября": 9, "сентябрю": 9, "сентябре": 9, "сентябрь": 9,
    "апр": 4, "апрел": 4, "апреля": 4, "апрелю": 4, "апреле": 4, "апрель": 4,
    "фев": 2, "июн": 6, "июня": 6, "июню": 6, "июне": 6, "июнь": 6,
    "июл": 7, "июля": 7, "июлю": 7, "июле": 7, "июль": 7,
    "мар": 3, "май": 5, "мае": 5,
}
_MONTH_ALT = "|".join(sorted((k for k in MONTHS if k != "мая"), key=len, reverse=True))
_ROMAN = {"1": 1, "i": 1, "ii": 2, "iii": 3, "iv": 4,
          "1-й": 1, "i-й": 1, "ii-й": 2, "iii-й": 3, "iv-й": 4,
          "первый": 1, "второй": 2, "третий": 3, "четвёртый": 4, "четвертый": 4}

def _norm_ws(s: str) -> str:
    s = re.sub(r"[\u2013\u2014]+", "-", s)
    return re.sub(r"\s+", " ", s).strip().lower()

def _find_months(s: str):
    """Список (month, year|None, start_pos)."""
    out = []
    for m in re.finditer(rf"(?<![а-яёa-z0-9])({_MONTH_ALT})(?![а-яёa-z])", s):
        mnum = MONTHS.get(m.group(1))
        if mnum is None:
            continue
        after = s[m.end():]
        ym = re.match(r"\s*\.?\s*(?:\d{1,2}\s+)?(\d{4})\s*(?:г\.?\.?|года|году|годе|го)?", after)
        year = int(ym.group(1)) if ym else None
        out.append((mnum, year, m.start()))
    return out

def _parse_quarter(s: str):
    pat = "([IiVv]+|[1-4]|первый|второй|третий|четвёртый|четвертый)"
    m = re.search(r"(?:^|[\s(])" + pat + r"\s*-?\s*кв", s)
    if m:
        key = m.group(1).lower()
        if key in _ROMAN:
            return _ROMAN[key]
        key = "iv" if key == "v" else key
        if key in ("i", "ii", "iii", "iv"):
            return len(key)
    m = re.search(r"кв(?:артал)?\s*(" + pat + r")\b", s)
    if m:
        key = m.group(1).lower()
        if key in _ROMAN:
            return _ROMAN[key]
    return None

def parse_period(text: str):
    """Вернуть dict(kind, year, m1, m2) или None.
    kind: 'month' | 'range' | 'quarter' | 'year' | 'asof' (на конец/на 1 число)"""
    s = _norm_ws(text)
    if not s:
        return None

    # Диапазон: «январь - ноябрь 2019», «янв–март 2018», «jan... по декабрь»
    mm = _find_months(s)
    rng = re.search(
        rf"({_MONTH_ALT})\.?\s*(?:-|по)\s*({_MONTH_ALT})\.?\s*(?:(?:\d{{1,2}}\s+)?(\d{{4}}))?", s)
    if rng:
        m1, m2 = MONTHS[rng.group(1)], MONTHS[rng.group(2)]
        yr = int(rng.group(3)) if rng.group(3) else (mm[-1][1] if mm and mm[-1][1] else None)
        return {"kind": "range", "year": yr, "m1": m1, "m2": m2}

    q = _parse_quarter(s)
    if q:
        ym = re.search(r"((?:\d{1,2}\s+)?\d{4})", s)
        return {"kind": "quarter", "year": int(ym.group(1)[-4:]) if ym else None,
                "m1": (q - 1) * 3 + 1, "m2": q * 3}

    if mm:
        mnum, yr, _ = mm[-1]
        asof = bool(re.search(r"на\s+(конец|1[\- ]?(?:числ|го)?)\s", s))
        single = len(mm) == 1 and mm[0][0] == mnum
        return {"kind": "asof" if asof else ("month" if single else "month"),
                "year": yr, "m1": mnum, "m2": mnum}

    ym = re.search(r"\b(\d{4})\s*(?:г|года|году|годе|го)\b", s)
    if ym:
        y = int(ym.group(1))
        if 1950 <= y <= 2150:
            return {"kind": "year", "year": y, "m1": 1, "m2": 12}
    return None

def canon_period(p):
    """Канонический ключ (year, m1, m2) для дедупликации.
    Quarter(1) ≡ range(1,3); year ≡ range(1,12)."""
    if not p:
        return None
    y, m1, m2 = p["year"], p["m1"], p["m2"]
    if p["kind"] == "month":
        m1 = m2 = p["m1"]
    elif p["kind"] == "year":
        m1, m2 = 1, 12
    return (y, m1, m2)

def canon_month_range(p):
    c = canon_period(p)
    return c
