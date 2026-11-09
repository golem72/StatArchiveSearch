# -*- coding: utf-8 -*-
"""Текстовая нормализация, извлечение единиц измерения, парсинг чисел."""
import re

WS = re.compile(r"\s+")

def collapse(s: str) -> str:
    return WS.sub(" ", s or "").strip()

# ---------------------------------------------------------------- units ----
# (regex, canonical, label_rus) — порядок важен: сначала длинные фразы.
UNIT_PATTERNS = [
    (r"миллионов? рублей|млн\.? руб", "mln_rub",  "млн руб"),
    (r"миллиардов? рублей|млрд\.? руб", "mlrd_rub", "млрд руб"),
    (r"тысяч(а|и)? рублей|тыс\.? руб", "th_rub",  "тыс руб"),
    (r"руб[е]й|рубл|руб\.?|руб\b", "rub", "руб"),
    (r"тысяч(а|и)? человек|тыс\.? ч[е]л", "th_ch", "тыс чел"),
    (r"человек|чел\.?|чел\b", "ch", "человек"),
    (r"копее?к|коп\.?", "kopek", "коп"),
    (r"долл\.?|долларов?", "usd", "долл"),
    (r"тонн[аи]?|т\.?\b", "t", "т"),
    (r"единиц[аы]?|ед\.?|ед\b", "units", "ед"),
    (r"процентов?|%|п\.?\s?п\.", "pct", "%"),
    (r"часов?|ч\.?\b(?!\S)", "h", "час"),
    (r"лет\b", "yr", "лет"),
]
_UNIT_RE = [(re.compile(p, re.I), c, l) for p, c, l in UNIT_PATTERNS]

def extract_unit(text: str):
    """Вернуть (unit_canon, unit_raw) для текста или None.
    Ищем краткую фразу с единицей; сначала проверяем сам текст."""
    if not text:
        return None
    t = collapse(text)
    if len(t) > 80:                      # длинные абзацы — не строки ед.изм.
        t2 = t
        # возможно, единица в конце фразы: "..., человек", "..., в %"
        m = re.search(r"[,;]\s*([а-яёa-z%\.\- ]{2,40})$", t)
        if not m:
            return None
        s = m.group(1)
    else:
        s = t
    for rx, canon, label in _UNIT_RE:
        m = rx.search(s)
        if m:
            raw = s.strip(" .,;").strip()
            return canon, raw
    return None

UNIT_LABELS = {c: l for _, c, l in UNIT_PATTERNS}

# ---------------------------------------------------------------- numbers --
def parse_value(raw: str):
    """'125,6' → 125.6; '3971,5 3)' → 3971.5; '-' / 'х' / 'кусок 100 гр' → None."""
    s = collapse(raw)
    if not s or s in ("-", "—", "х", "x", "...", "…", "« ... »", "«...»", "« … »"):
        return None
    s = re.sub(r"\(\d+\)", " ", s)                   # сноски (3)
    s = s.replace("\u00a0", "")
    # «106,8» → «106.8» (запятая-десятичная)
    s = re.sub(r"(?<=\d),(?=\d)", ".", s)
    m = re.search(r"[-+]?\d[\d\s]*(?:\.[\d]+)?", s)
    if not m:
        return None
    val = m.group(0).replace(" ", "")
    # если вокруг числа идут буквы — это не чистое значение ('кусок 100 гр')
    rest = re.sub(re.escape(m.group(0)), "", s)
    if re.search(r"[а-яёa-z%]", rest, re.I):
        return None
    try:
        return float(val)
    except ValueError:
        return None

def is_data_cell(text: str) -> bool:
    """Ячейка-значение (число, '-', 'х', '...')."""
    s = collapse(text)
    if not s:
        return False
    if s in ("-", "—", "х", "x", "...", "…"):
        return True
    return bool(re.fullmatch(r"-?\d[\d\s]*(?:[,:]\d+)?\d*\)?", s))
