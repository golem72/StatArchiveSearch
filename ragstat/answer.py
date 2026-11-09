# -*- coding: utf-8 -*-
"""Поиск ответа: вопрос → показание + период/вид, ранжирование, дедупликация."""
import os
import re
import sqlite3
import sys

from .periodnorm import MONTHS as _MONTHS
from . import dictbuild            # noqa: E402  (dict-канал; ленивая матрица)

UNIT_ANSWER = {
    "mln_rub": "млн рублей", "mlrd_rub": "млрд рублей", "th_rub": "тыс рублей",
    "rub": "рублей", "usd": "долларов", "ch": "человек", "th_ch": "тыс человек",
    "pct": "процентов", "t": "т", "units": "единиц", "kopek": "копеек",
    "h": "часов", "yr": "лет",
}

_MONTH_RX = re.compile(
    r"январ\w*|феврал\w*|март\w*|апрел\w*|\bма(?:е|я|й)\w*|июн\w*|июл\w*"
    r"|август\w*|сентябр\w*|октябр\w*|ноябр\w*|декабр\w*")


def _to_month(word: str):
    best = None
    for name, num in _MONTHS.items():
        if num is None:
            continue
        if word.startswith(name):
            if best is None or len(name) > len(best[0]):
                best = (name, num)
    return best[1] if best else None


def parse_question(q: str):
    s = (q or "").lower()
    ym = re.findall(r"\b(19\d{2}|20\d{2})\b", s)
    year = max(int(x) for x in ym) if ym else None
    # месяца: каждый привязываем к году, стоящему справа в той же конструкции
    pairs = []
    for mtr in _MONTH_RX.finditer(s):
        nm = _to_month(mtr.group(0))
        if nm is None:
            continue
        tail = s[mtr.end(): mtr.end() + 40]
        yy = re.match(r"[^0-9]{0,20}((19|20)\d{2})", tail)
        pairs.append((nm, int(yy.group(1)) if yy else None))
    if pairs and year is not None:
        cur = [m for (m, y) in pairs if y == year]
        if cur:
            m1, m2 = min(cur), max(cur)
        else:
            m1 = min(m for (m, y) in pairs if y is None) if any(y is None for (m, y) in pairs) else None
            m2 = max(m for (m, y) in pairs if y is None) if m1 is not None else None
    elif pairs:
        m1 = min(m for (m, y) in pairs)
        m2 = max(m for (m, y) in pairs)
    else:
        m1 = m2 = None

    rel_pm = ("предыдущ" in s and ("месяц" in s or "квартал" in s)) or              re.search(r"к\s+(соотв\w*\s+)?периоду\s+предыдущего", s)
    # «составил/составили» — НЕ маркер процентов («сколько млн руб. составил оборот?»)
    has_pct = bool(re.search(
        r"процент\w*|в\s+%|к\s+(19|20)\d{2}\s+году|отношен\w*\s+к|индекс", s))
    _is_share = (bool(re.search(r"доля|удельный\s+вес", s))
                 and not re.search(r"в\s+%|к\s+(19|20)\d{2}\s+году|к\s+соотв\w*\s+(периоду|дате|месяцу)", s))
    if rel_pm and not re.search(r"\b(19|20)\d{2}\s+году", s):
        kind = "rel_pm"
    elif _is_share:
        # «доля/удельный вес» — вид строки зависит от документа (abs ИЛИ rel "в % к итогу") → нейтрально
        kind = None
    elif has_pct or "сравнен" in s or "соотв" in s:
        kind = "rel"
    else:
        kind = "abs"

    spec = {"year": year, "m1": m1, "m2": m2, "kind": kind,
            "cumulative": "нарастающ" in s}

    # базовый период сравнения: "к январю - марту 20дд" / "к <месяцу> 20дд" / "к 20дд году"
    # + творительный "с январем 2018" / "с тем же месяцем (прошлого года)"
    _INSTR_MONTHS = {"январ": 1, "феврал": 2, "март": 3, "апрел": 4, "маем": 5,
                     "июн": 6, "июл": 7, "август": 8, "сентябр": 9, "октябр": 10,
                     "ноябр": 11, "декабр": 12}
    spec["base"] = None
    m = re.search(r"\b(?:к|с|со)\s+([^,.;(]{2,60}?)\s*((19|20)\d{2})\b", s)
    if m:
        seg, yr = m.group(1), int(m.group(2))
        mons = [v for kk, v in _INSTR_MONTHS.items() if kk in seg]
        if mons:
            spec["base"] = (yr, min(mons), max(mons))
        else:
            spec["base"] = (yr, None, None)
    if spec["base"] is None:
        b = re.search(r"\b(?:по)?\s*(?:к|с(?:о)?)\s+((19|20)\d{2})\s*(?:году|годе)", s)
        if b:
            spec["base"] = (int(b.group(1)), None, None)
    if spec["base"] is None and year is not None and spec.get("m1"):
        if re.search(r"аналогичн\w*|соответствующ\w+|по\s+сравнени\w*|к\s+предшествующ\w+", s) and "прошл" in s:
            spec["base"] = (year - 1, spec["m1"], spec["m2"])
    if spec["base"] is None and re.search(r"тем\s+же\s+мес?ец|тем\s+же\s+квартал", s):
        # "по сравнению с тем же месяцем/кварталом (прошлого года)" → тот же месяц пред. года
        if year is not None and spec.get("m1") and spec["m1"] == spec["m2"]:
            spec["base"] = (year - 1, spec["m1"], spec["m2"])
    return spec


# --- стоп-слова (по префиксу-стиблему) -------------------------------------

_STOP_STEMS = {
    # вопросительные, частицы, предлоги
    "каков", "как", "сколько", "когда", "зачем", "почему", "чтобы", "был",
    "была", "было", "были", "быть", "есть", "имел", "имела", "имеют", "имела",
    "вс", "на ", "по ", "за ", "из ", "от ", "до ", "при", "над", "ради",
    # обобщающие глагольные формы (не относятся к показателю)
    "состо", "состав", "составл", "составил", "составила", "составило",
    "составили", "составивший", "составив", "содерж",
    "выявл", "выявлен", "выяви", "зарег", "зарегистрир", "зарегистр",
    "записан", "запис", "соверш", "совершенн", "сообщ", "сообщен",
    "отмет", "отмеч", "произвед", "произвел", "произвели", "изготовл",
    "отгруж", "отгруз", "отправл", "отправлен", "выпущен", "выпущ",
    "получен", "получ", "приобретен", "приобрет", "продаж", "продавл",
    "продавал", "продавец", "привез", "привезен", "принят", "принято",
    "установл", "установлен", "определен", "определ", "сохран", "сохранен",
    "обеспечен", "обеспеч", "достигнут", "достигн", "показыва", "показа",
    "показат", "показан", "показ", "фиксиру", "фиксируем", "фикс",
    "оформлен", "оформл", "обслужен", "обслужива", "обслуж",
    "покупател", "покупателя", "потребител", "потребля", "потребл",
    # обобщающие существительные меры
    "величин", "велик", "значени", "значит", "доля", "долей",
    "численн", "количеств", "объем", "обьём", "масштаб", "масшта",
    "сумм", "итог", "итого", "уровень", "уровн",
    "миллион", "миллиона", "миллионе", "миллионов",
    "тысяч", "тысяча", "тысячу",
    "процент", "процена", "процента", "процентна",
    "соотв", "соотн", "соответств", "соотнош", "связ",
    "отношени", "относительно", "относител", "отношен",
    "предыдущ", "предыдущем", "предыдущи", "предыдущег",
    "текущ", "текущем",
    "период", "периода", "периоде", "периода",
    "год", "года", "году", "лет", "годах", "годов",
    "квартал", "квартала", "квартале", "квартале",
    "месяц", "месяца", "месяцев", "месяцу", "месяцами", "месяце",
    # месячные имена (период обрабатывается регуляркой)
    "январ", "феврал", "март", "апрел", "мае", "мая", "ма[",
    "июн", "июл", "август", "сентябр", "октябр", "ноябр", "декабр",
    # валюты и единицы
    "рубл", "рубля", "рублей", "рубли", "рубло", "рубле",
    "доллар", "доллара", "доллары", "долларов",
    "злот", "гривн", "тенге", "сольд", "сальд", "евро",
    "человек", "человека", "челове", "людей", "люди",
    # служебные
    "включ", "включен", "включая",
    "общ", "общей", "общий", "общие",
    "всего", "всего", "всего",
    # канцелярит субъектов (част в этих отчетах, не относится к показателю)
    "крупн", "средн", "организаци", "предприяти", "хозяйств",
    "субъект", "зоны", "зона", "регион", "област",
}
_STOP_STEMS = {x for x in _STOP_STEMS if "[" not in x and len(x) >= 2}


def _is_stop(w: str) -> bool:
    return any(w.startswith(stem) for stem in _STOP_STEMS)


def _words(q: str):
    ws = re.findall(r"[а-яёa-z]{3,}", (q or "").lower())
    out = [w for w in ws if not _is_stop(w)]
    seen, unique = set(), []
    for w in sorted(out, key=len, reverse=True):
        if w not in seen:
            seen.add(w)
            unique.append(w)
    return unique[:8]


# --- якорные совпадения -----------------------------------------------------

def _ind_words(ind: str):
    return re.findall(r"[\wа-яё]+", (ind or "").lower())


def _anchor_hit(word: str, iw: str) -> bool:
    if iw == word:
        return True
    a, b = (iw, word) if len(iw) <= len(word) else (word, iw)
    if len(a) < 3:
        return False
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n >= 5 and n * 5 >= len(a) * 4


def _anchors(indicator: str, words) -> int:
    iws = _ind_words(indicator)
    if not iws or not words:
        return 0
    return sum(1 for w in words if any(_anchor_hit(w, iw) for iw in iws))


def _ind_precision(indicator: str, words) -> float:
    # доля собственных слов индикатора, упомянутых в вопросе —
    # короткое точное совпадение побеждает длинную «суперстроку»
    iws = _ind_words(indicator)
    if not iws or not words:
        return 0.0
    hit = sum(1 for iw in iws
             if len(iw) >= 4 and any(_anchor_hit(w, iw) for w in words))
    return hit / max(1, sum(1 for iw in iws if len(iw) >= 3))


def _lead_bonus(indicator: str, words) -> float:
    # начинается ли индикатор со слова из вопроса
    iws = _ind_words(indicator)
    if not iws or not words:
        return 0.0
    first = iws[0]
    if any(_anchor_hit(w, first) for w in words):
        return 1.2
    return -0.6


def _prefix_hit(word: str, text: str) -> int:
    best, L, n = 0, len(word), len(text)
    for i in range(n - 1):
        j = 0
        while j < L and i + j < n and text[i + j] == word[j]:
            j += 1
        if j > best:
            best = j
        if best >= min(L, 7):
            break
    return best


def fmt_value(o):
    v = o.get("value")
    if v is None:
        return o.get("value_raw") or "—"
    if isinstance(v, float) and v == int(v) and abs(v) < 10**12:
        s = f"{int(v):,}".replace(",", " ")
        s += ",0"
    else:
        s = f"{v:.2f}".rstrip("0").rstrip(".")
        s = s.replace(".", ",")
    unit = UNIT_ANSWER.get(o.get("unit"), o.get("unit_raw") or "")
    if unit:
        return f"{s} {unit}"
    return s


def _baseperiod_bonus(r, spec):
    """Колонка "к январю - марту 20дд" хранит base_period=(год, м1 вопроса)."""
    base = spec.get("base")
    bp = r.get("base_period")
    if not base or not base[0] or not bp:
        return 0.0
    try:
        if isinstance(bp, (list, tuple)):
            by, bm = int(bp[0]), (int(bp[1]) if len(bp) > 1 and bp[1] is not None else None)
        else:
            by, bm = (int(x) if x else None for x in str(bp).split("-"))
    except Exception:
        return 0.0
    if by == base[0] and bm is not None and bm in (base[1], base[2]):
        return 2.0
    return 0.0


def _base_matches(r, spec):
    """True/False/None-неизвестно: совпадает ли base_period строки с требуемым базисом."""
    base = spec.get("base")
    bp = r.get("base_period")
    if not base or not base[0] or not bp:
        return None
    try:
        if isinstance(bp, (list, tuple)):
            by, bm = int(bp[0]), (int(bp[1]) if len(bp) > 1 and bp[1] is not None else None)
        else:
            by, bm = (int(x) if x else None for x in str(bp).split("-"))
    except Exception:
        return None
    if by != base[0]:
        return False
    if bm is None:
        return None
    return bm in (base[1], base[2])


def _period_score(r, spec):
    py = r.get("period_year")
    if spec["year"] and py == spec["year"]:
        return 1.0
    if spec.get("base") and spec["base"][0] and py == spec["base"][0]:
        return 1.2
    if spec["year"] and py is None:
        return 0.3 if (r.get("period_m1") is None and r.get("period_type") is None) else 0.0
    if spec["year"] and py is not None and py != spec["year"]:
        return -1.5 if spec.get("kind") != "rel" else -0.3
    return 0.0


def _month_score(r, spec):
    if not (spec["m1"] and spec["m2"]):
        return 0.0
    pt, py, pm1, pm2 = (r.get("period_type"), r.get("period_year"),
                        r.get("period_m1"), r.get("period_m2"))
    if pm1 is None and pt is None:
        # периода нет вовсе: штраф, если месяц/году интересуют
        return -2.0 if spec["m1"] == spec["m2"] else -1.0
    if pm1 == spec["m1"] and pm2 == spec["m2"] and (py is None or
                                                    py == spec["year"]):
        return 2.6
    if pt in ("range", "cumulative", "year") and \
       (pm1 or 1) <= spec["m1"] and (pm2 or 12) >= spec["m2"]:
        return 1.5
    if spec["m1"] == spec["m2"] and pt in ("month",) and \
       pm1 is not None and abs(pm1 - spec["m1"]) <= 1:
        return 1.0
    # месяц запроса вне периода строки
    return -1.0


def _pub_score(r, spec):
    if not spec["year"]:
        return 0.0
    pub = r.get("pub_year")
    if pub is None:
        return 0.0
    if pub == spec["year"]:
        return 1.0
    if abs(pub - spec["year"]) <= 1:
        return 0.0
    return -2.0


def _kind_score(r, spec):
    mk = r.get("metric_kind")
    sk = spec.get("kind")
    if sk == "abs":
        return 2.5 if mk == "abs" else -2.0
    if sk == "rel_pm":
        if mk == "rel_pm":
            return 2.0
        if mk in ("rel", "rel_yoy"):
            return 0.5
        return -1.0
    if sk == "rel":
        # строка rel с base_period: сильно дискриминируем по базовому периоду
        base = spec.get("base")
        bp = r.get("base_period")
        b = 0.0
        if bp:
            parts = bp.split("-") if isinstance(bp, str) else list(bp)
            try:
                b_by = int(parts[0])
                b_bm = int(parts[1]) if len(parts) > 1 and parts[1] is not None else None
            except (ValueError, IndexError, TypeError):
                b_by, b_bm = None, None
            if base and base[0]:
                if b_by == base[0]:
                    if b_bm is None or (base[1] and base[2] and base[1] <= b_bm <= base[2]):
                        b = 2.5
                    else:
                        b = -1.0
                elif (spec.get("year") and b_by == spec["year"] - 1
                      and spec.get("m1") and b_bm in (spec["m1"], None)):
                    b = 1.5
                else:
                    b = -1.0
            elif spec.get("year") and b_by == spec["year"] - 1 and spec.get("m1") and b_bm == spec["m1"]:
                b = 1.5
        # строка rel, зафиксированная на базовом периоде вопроса ("к январю-марту 2007")
        bbase = 0.0
        if base and base[0] and r.get("period_year") == base[0]:
            bbase = 1.5
            bm1, bm2 = base[1], base[2]
            if bm1 and r.get("period_m1") is not None and \
                    bm1 <= (r.get("period_m1") or 1) and (r.get("period_m2") or 1) <= bm2:
                bbase = 2.5
        if mk in ("rel", "rel_yoy"):
            return 2.0 + b + bbase
        if mk == "rel_pm":
            return 0.5 + b
        return -1.5
    # базовый период из вопроса: строка rel с периодом = baseline
    if spec.get("base") and mk in ("rel", "rel_yoy", "rel_pm"):
        by, bm1, bm2 = spec["base"]
        if by and r.get("period_year") == by and \
                (bm1 is None or
                 (r.get("period_m1") in (bm1, None) and
                  r.get("period_m2") in (bm2, bm1, None))):
            return 3.2
        if by and r.get("period_year") == by:
            return 1.5
    # rel / rel_yoy (kind не распознан)
    if mk in ("rel", "rel_yoy"):
        return 2.0
    if mk == "rel_pm":
        return 0.5
    return -1.0


_UNIT_RUB = {"mln_rub", "rub", "th_rub"}


def _unit_score(question, unit):
    """Единица ответа должна совпадать со смыслом вопроса."""
    if not unit or not question:
        return 0.0
    q = question.lower()
    u = unit.lower()
    if ("процент" in q or "в %" in q or "% к" in q):
        if u == "pct":
            return 3.5
        return -2.5
    if any(w in q for w in ("рубл", "стоимост", "цена")):
        if u in _UNIT_RUB:
            return 3.0
        if u == "pct":
            return -1.5
    if "человек" in q or "человеч" in q:
        if u in ("ch", "th_ch"):
            return 3.0
        if u == "pct":
            return -1.5
    if "тонн" in q or "тыс. т" in q:
        if u == "t":
            return 2.5
        if u == "pct":
            return -1.0
    return 0.0


_MONTH_FORMS = {  # точные формы + префиксы (порядок: длинные/разные сначала)
    "март": 3, "май": 5, "мая": 5, "мая": 5,
    "февр": 2, "янв": 1, "апр": 4, "июн": 6, "июл": 7,
    "авг": 8, "сент": 9, "октя": 10, "ноя": 11, "дека": 12,
}

def _month_indicator(indicator: str):
    """индикатор = имя месяца (перевёрнутые таблицы: строки = месяце)."""
    i = re.sub(r"[^а-яёa-z]", "", (indicator or "").lower())
    if not i or len(i) > 12:
        return None
    for stem, m in _MONTH_FORMS.items():
        if i.startswith(stem):
            return m
    return None


def _contained(spec_words_all, ind: str) -> float:
    """индикатор ПОЛНОСТЬЮ есть в тексте вопроса — самый сильный дискриминатор близк. строк."""
    q = re.sub(r"[^а-яёa-z0-9]", " ", (spec_words_all or "").lower()).split()
    i = re.sub(r"[^а-яёa-z0-9]", " ", (ind or "").lower()).split()
    if not q or not i or len(i) < 2:
        return 0.0
    qs = set(q)
    if all(w in qs for w in i):
        return 3.5 * min(len(i), 4) / max(len(i), 2)   # 2+ слов полное вхождение
    # индикатор как подстрока вопроса (оригинальная форма)
    raw = re.sub(r"\s+", " ", (ind or "").lower()).strip(" —-")
    if len(raw) >= 8 and raw in re.sub(r"\s+", " ", (spec_words_all or "").lower()):
        return 3.0
    return 0.0


_IND_TEMPL = re.compile(
    r"^(индекс\w*|производств\w*|оборот\w*|объем\w*|численн\w*|населен\w*"
    r"|количеств\w*|стоимост\w*|обеспеченност\w*|доля\w*|введен\w*|ввод\w*|площад\w*"
    r"|побед\w*|поголовье|голов|тонн|продукци\w*|товар\w*|работ\w*|услуг\w*"
    r"|доход\w*|расход\w*|задолженност\w*|задолженност\w*|зарпл\w*|заработн\w*)"
)


def _distinctive_bonus(ind, question):
    """Характерное (не шаблонное) слово вопроса, найденное в индикаторе."""
    if not ind or not question:
        return 0.0
    ind_l = (ind or "").lower()
    ws = [w for w in re.findall(r"[а-яёa-z]{6,}", (question or "").lower())
          if not _is_stop(w) and not _IND_TEMPL.match(w)]
    ws = list(dict.fromkeys(ws))
    if not ws:
        return 0.0
    hits = sum(1 for w in ws if w in ind_l)
    return 2.5 * hits / min(len(ws), 3) if hits else 0.0


def _hint_bonus(r, spec):
    """Нумерация таблиц в тестах смещена относительно нумерации docx (статистика:
    заявл. - факт. ∈ [..12]); окно широкое, бонус затухает."""
    hq = spec.get("hint_seq")
    if not hq:
        return 0.0
    d = (r.get("table_seq") or 0) - hq
    return {0: 8.0, 1: 6.0, -1: 6.0, 2: 4.0, -2: 4.0, 3: 3.0, -3: 3.0,
            4: 2.0, -4: 2.0, 5: 1.5, -5: 1.5, 6: 1.0, -6: 0.8, 7: 0.5, -7: 0.5,
            8: 0.3, -8: 0.3}.get(d, 0.0)



def _words_set(text):
    return set(re.findall(r"[а-яё]{4,}", (text or "").lower()))

def _shares_stem(w, text_words, minlen=5):
    """True if any word in text_words starts with the same stem as w
    (handles Russian case endings: капуста/капусту, дох/дохода)."""
    if len(w) < 4:
        return False
    stem = w[:max(5, len(w) - 2)]
    for t in text_words:
        if t == w:
            return True
        if len(t) >= minlen and t[:len(stem)] == stem:
            return True
        if len(w) >= minlen and w[:len(t[:-2])] == t[:-2]:
            return True
    return False

def _specific_ok(cur, alt, question):
    """Не допускать снос строки с конкретным показателем на строку без него:
    если содержательное слово вопроса есть в индикаторе текущей строки, но
    отсутствует в альтернативе — снос запрещён. Морфология устойчивая
    (капусту→капуста) через совпадение корня."""
    try:
        _STOP8 = {'индекс','потребител','производит','состава','состав','средств',
                  'населени','произведе','производств','зарегистрир','просроченн',
                  'задолженн','нагрузк','показа','показат','величину','столько',
                  'работник','зарплат','заработн','минимальн','максимальн','общ','общая',
                  'общий','объем','объема','обрат','обращен','число','случа','сумма','доля',
                  'доли','всего','итого','календарн','выходн','работ','набор','набора',
                  'питани','энерги','электро','болезн','кровообращ','прирост','перес',
                  'коэффици','должн','должност','кредиторск','дебиторск','товаров','товари',
                  'област','брян','брянск','дене','доход','населен','цены','стоимост','стоим',
                  'стоимости','предложен','предлагаем','предприним','создан','создани',
                  'предполож','продукци','продукто','средств','зарегистри','зарегистрир','зарегистрир',
                  'зарегистрир','зарегистир','зарег','зарег','зарег','зарегист','зарегист',
                  'зарегист','зарег','зарегист','зарег','зарегист','зарег','зарегист',
                  'зарег','зарег','зарег','зарег','зарег','зарег','зарег','зарег'}
        def _ind_words(row):
            return set(re.findall(r"[а-яё]{4,}",
                (str(row.get("indicator_raw") or row.get("indicator") or "") + " "
                 + str(row.get("col_hint") or "")).lower()))
        _cwords = _ind_words(cur)
        _awords = _ind_words(alt)
        _qwords = [w for w in re.findall(r"[а-яё]{5,}", (question or "").lower())
                   if w not in _STOP8 and not re.search(r"[0-9]", w)]
        _STOP8 |= {'состава','состава','состовленн','состовленн','состовлен','состава','состава','состовленн','состовленн','состовленн','состовленн'}
        for w in _qwords:
            if _shares_stem(w, _cwords) and not _shares_stem(w, _awords):
                return False
    except Exception:
        pass
    return True


def _score(r, words, spec, bm25=0.0, question=""):
    ind = (r.get("indicator_raw") or r.get("indicator") or "")
    sk = r.get("metric_kind") or ""
    cap = r.get("caption") or ""
    anc = _anchors(ind, words)
    anc_cap = _anchors(cap, words) if cap and anc < 2 else 0
    cov = 0.0
    if anc == 0 and ind and words:
        tot = 0.0
        ind_l = ind.lower()
        for w in words:
            h = _prefix_hit(w, ind_l)
            if h >= 4:
                tot += min(h / len(w), 1.0)
        cov = min(tot / len(words), 1.0)
    # фразы 2 слова из вопроса как блок в индикаторе/подписи
    ph = 0.0
    iws_ind = _ind_words(ind)
    for a, b in (spec.get("bigrams") or [])[:4]:
        ia = any(_anchor_hit(a, iw) for iw in iws_ind)
        ib = any(_anchor_hit(b, iw) for iw in iws_ind)
        if ia and ib:
            ph += 1.6
        elif anc == 0 and cap and ia and any(_anchor_hit(b, iw) for iw in _ind_words(cap)):
            ph += 0.6
    sc = 3.0 * anc + 0.8 * min(anc_cap, 3) + 4.0 * cov + min(4.0, max(0.0, -bm25) / 2.0) + ph
    sc += 1.5 * _ind_precision(ind, words) + _lead_bonus(ind, words)
    totl = 0.0
    for w in words:
        if len(w) >= 5 and any(_anchor_hit(w, iw) for iw in iws_ind):
            totl += len(w)
    sc += min(3.0, totl / 10.0)
    sc += _contained(question, ind)
    sc += _unit_score(question, r.get("unit"))
    sc += _period_score(r, spec)
    ms = _month_score(r, spec)
    mi = _month_indicator(ind)
    if mi is not None and spec["m1"] == spec["m2"]:
        if mi == spec["m1"]:
            sc += 2.5
            if ms < 0:
                ms = 0.0
        else:
            sc -= 1.5
    sc += ms
    sc += _pub_score(r, spec)
    sc += _kind_score(r, spec)
    # Листовые строки «в том числе … : X»: штраф, если подкатегория X не
    # встречается в вопросе (вопрос про итог — не про подстатью).
    _ind_l2 = str(ind).lower()
    _vts, _vln = _ind_l2.find("в том числе"), 11
    if _vts < 0:
        _vts, _vln = _ind_l2.find("в т.ч"), 5
    if _vts >= 0:
        _tail = str(ind)[_vts + _vln:]
        # лев = последний сегмент после двоеточий (подстатья); иначе всё после маркера
        if ":" in _tail:
            _tail = _tail.rsplit(":", 1)[1]
        _t_toks = [t for t in re.findall(r"[а-яё0-9]{4,}", _tail.lower())]
        if len(_t_toks) >= 2 and words:
            _t_hit = any(_anchor_hit(t, w) for t in _t_toks for w in words)
            if not _t_hit:
                sc -= 2.5
    # бонус текстовой шапки колонки (различает «соотв. периоду предыдущего года»
    #   и «к предыдущему месяцу» — оба сейчас без различия в metric_kind)
    chint = r.get("col_hint") or ""
    # (вектор/якорный бонус от шапки колонки: отключён — регресс; col_hint хранится для FTS/векторов)
    if r.get("is_ref"):
        sc -= 3.0
    if r.get("value") is None:
        sc -= 5.0
    sc += _distinctive_bonus(ind, question)
    if spec.get("base") and sk in ("rel", "rel_yoy", "rel_pm"):
        ur = (r.get("unit_raw") or "").lower()
        if ur:
            b_yr = str(spec["base"][0])
            tgt_yr = str(spec.get("year") or 0)
            # базовый период вопроса = тот же месяц/квартал прошлого года (yoy-интент):
            # «к январю 2007» → нужны строки rel_yoy/rel, НЕ rel_pm («к предыдущему периоду»)
            _yoy = (spec.get("year") and spec.get("m1") and spec.get("m2")
                    and spec["base"][0] == spec["year"] - 1
                    and spec["base"][1] == spec["m1"] and spec["base"][2] == spec["m2"])
            if b_yr in ur:
                sc += 2.5
            elif "предыдущ" in ur or "прошл" in ur:
                sc += (-1.0 if sk == "rel_pm" else 1.0) if _yoy else (0.5 if sk == "rel_pm" else -1.5)
            # год целевого периода в заголовке столбца — сильный признак
            import re as _re
            yrs = set(_re.findall(r"\b(19\d\d|20\d\d)\b", ur))
            if yrs:
                if _yoy:
                    # yoy-вопрос: в заголовке правильной строки стоит ГОД-БАЗА (2007),
                    # а в заголовке rel_pm — целевой год (2008) → инвертируем
                    if b_yr and b_yr in yrs:
                        sc += 2.0
                    elif tgt_yr and tgt_yr in yrs:
                        sc -= 2.0
                else:
                    if tgt_yr and tgt_yr in yrs:
                        sc += 3.0
                    elif b_yr and b_yr in yrs and tgt_yr not in yrs:
                        sc -= 1.5
    if re.search(r"\b(всего|итог\w*)\b", ind, re.I):
        if re.search(r"\b(общ\w+|суммарн\w*|общи\w+|всего|итог\w*)\b", question.lower()):
            sc += 12.0
    elif re.search(r"\b(общ\w+|суммарн\w*)\b", question.lower()):
        sc -= 1.5
    sc += _hint_bonus(r, spec)
    return sc


SQL_HEAD = (
    "SELECT o.id, o.dict_id, o.value, o.value_raw, o.unit, o.unit_raw, o.metric_kind, "
    "o.period_type, o.period_year, o.period_m1, o.period_m2, o.base_period, "
    "o.is_ref, o.indicator, o.indicator_raw, o.table_seq, o.col_hint, "
    "f.path AS fpath, f.pub_year, f.pub_month, t.caption "
    "FROM observations o "
    "JOIN files f ON f.sha256 = o.file_sha "
    "LEFT JOIN logical_tables t ON t.file_sha = o.file_sha AND t.seq = o.table_seq"
)

# вес векторного бонуса: 0 = выключен (на текущей e5-модели +9/−10 ≈ нейтрален).
# Установа VEC_W=1.5 (и VEC_GAP_TH) включает семантический тай-брейкер среди близких структурных строк.
VEC_W = 0.0
VEC_GAP_TH = 2.0


def _build_where(spec, kinds=None):
    if kinds:
        where = ["o.value IS NOT NULL", "o.metric_kind IN (%s)" % ",".join("?" * len(kinds))]
        args = list(kinds)
    else:
        where = ["o.value IS NOT NULL", "o.metric_kind NOT IN ('ref','ratio')"]
        args = []
    if spec["year"]:
        where.append("COALESCE(o.period_year, ?) BETWEEN ? AND ?")
        args += [spec["year"], spec["year"] - 1, spec["year"] + 1]
    if spec["m1"] and spec["m2"] == spec["m1"]:
        where.append("(o.period_m1 IS NULL OR o.period_m1 = ? OR o.period_type IN ('quarter','year') OR (? BETWEEN o.period_m1 AND o.period_m2))")
        args += [spec["m1"], spec["m1"]]
    elif spec["m1"] and spec["m2"]:
        where.append("(o.period_m1 IS NULL OR COALESCE(o.period_m1,1) BETWEEN ? AND ? OR o.period_type IN ('quarter','year') OR o.period_type = 'range' AND o.period_m2 >= ?)")
        where.append("(o.period_m2 IS NULL OR COALESCE(o.period_m2,12) >= ?)")
        args += [spec["m1"], spec["m2"], spec["m2"], spec["m2"]]
    return where, args


def answer(conn, question, limit=8, pool=250, file_scope=None, table_hint=None, kinds=None):
    spec = parse_question(question)
    words = _words(question)
    import re as _re
    ow = [w for w in _re.findall(r"[а-яёa-z]{4,}", (question or "").lower())
          if not _is_stop(w)]
    spec = dict(spec, bigrams=[(a, b) for a, b in zip(ow, ow[1:]) if a != b][:6])
    conn.row_factory = sqlite3.Row
    where, args = _build_where(spec, kinds)
    hint_seq = None
    if table_hint:
        raw = int(table_hint) + 1
        row = conn.execute(
            "SELECT seq, raw_seq FROM logical_tables WHERE raw_seq IS NOT NULL AND raw_seq <= ? AND (raw_last IS NULL OR raw_last >= ?)" + ("" if not file_scope
             else " AND file_sha IN (SELECT sha256 FROM files WHERE path = ?)"),
            (raw, raw) + (tuple([file_scope]) if file_scope else ())).fetchone()
        if row:
            hint_seq = row["seq"]
    if hint_seq is not None:
        spec = dict(spec)
        spec["hint_seq"] = hint_seq
    if file_scope:
        where.append("f.path = ?")
        args.append(file_scope)
    where_sql = " AND ".join(where)

    def _pool(where_s, arg_s):
        id_score = {}
        if words:
            # 1) FTS (trigram — подстроки 4+ букв), внутри WHERE
            terms = [w for w in words if len(w) >= 4]
            if terms:
                fts_parts = []
                for w in dict.fromkeys(terms):
                    fts_parts.append(f'"{w}"')
                    if len(w) >= 6:
                        pref = w[:max(4, int(len(w) * 0.6))]
                        if pref and pref not in terms:
                            fts_parts.append(f'"{pref}"*')
                fts_q = " OR ".join(fts_parts)[:1500]
                try:
                    sub = ("SELECT o.id AS oid, MIN(r.bm) AS s "
                           "FROM (SELECT rowid AS rid, bm25(observations_fts) AS bm "
                           "      FROM observations_fts WHERE observations_fts MATCH ? "
                           "      ORDER BY bm25(observations_fts) ASC LIMIT 3000) r "
                           "JOIN observations o ON o.id = r.rid "
                           "JOIN files f ON f.sha256 = o.file_sha "
                           "WHERE " + where_s + " "
                           "GROUP BY o.id ORDER BY s ASC LIMIT " + str(pool) * 3)
                    for rid, s in conn.execute(sub, (fts_q,) + tuple(arg_s)).fetchall():
                        id_score[int(rid)] = min(id_score.get(int(rid), 0.0), s)
                except sqlite3.OperationalError:
                    pass
            # 2) LIKE-резерв
            like_terms = [f"%{w[:5]}%" for w in words if len(w) >= 4][:4]
            if like_terms:
                ors = " OR ".join("o.indicator LIKE ?" for _ in like_terms)
                ids_like = [int(rid) for rid, in conn.execute(
                    "SELECT o.id FROM observations o JOIN files f ON f.sha256 = o.file_sha WHERE " + where_s +
                    " AND (" + ors + ") LIMIT " + str(pool),
                    tuple(arg_s) + tuple(like_terms)).fetchall()]
                for rid in ids_like:
                    id_score.setdefault(int(rid), 0.0)
            # 2b) строки, чья подпись таблицы содержит 2+ ключевых слова вопроса
            wlist = [w for w in words if len(w) >= 5][:8]
            pairs = []
            for a, b in list(zip(wlist[::2], wlist[1::2]))[:4]:
                pairs.append((f"%{a[:6]}%", f"%{b[:6]}%"))
            if pairs:
                ors_sql = " OR ".join(["(t.caption LIKE ? AND t.caption LIKE ?)"] * len(pairs))
                ids_cap = [int(rid) for rid, in conn.execute(
                    "SELECT o.id FROM observations o JOIN files f ON f.sha256 = o.file_sha "
                    "LEFT JOIN logical_tables t ON t.file_sha = o.file_sha AND t.seq = o.table_seq "
                    "WHERE " + where_s + " AND (" + ors_sql + ") LIMIT " + str(pool),
                    tuple(arg_s) + tuple(x for p in pairs for x in p)).fetchall()]
                for rid in ids_cap:
                    id_score.setdefault(int(rid), 0.0)
            # 2в) строки «всего/итого» — см. блок 2г (фильтр в python)
        # 2г) «всего/итого» строки подсказанной таблицы (фильтр в python —
        #     SQLite LIKE кириллический регистр не складывается надёжно)
        if hint_seq is not None and file_scope:
            try:
                _ids_h = [int(rid) for rid, in conn.execute(
                    "SELECT o.id FROM observations o JOIN files f ON f.sha256 = o.file_sha "
                    "JOIN logical_tables t ON t.file_sha = o.file_sha AND t.seq = o.table_seq "
                    "WHERE f.path = ? AND t.seq = ?", (file_scope, hint_seq)).fetchall()]
                _rows = list(conn.execute(
                    "SELECT o.id, o.indicator FROM observations o "
                    "WHERE o.id IN (%s) LIMIT 400" % ",".join("?" * len(_ids_h)),
                    tuple(_ids_h))) if _ids_h else []
                for rid, ind in _rows:
                    ii = (ind or "").lower()
                    if "всего" in ii or "итог" in ii:
                        id_score.setdefault(int(rid), 0.0)
            except sqlite3.OperationalError:
                pass
        # 3) базовый пул — если релевантных мало
        if len(id_score) < pool // 2:
            ids_bare = [int(rid) for rid, in conn.execute(
                "SELECT o.id FROM observations o JOIN files f ON f.sha256 = o.file_sha WHERE " + where_s +
                " LIMIT " + str(pool), tuple(arg_s)).fetchall()]
            for rid in ids_bare:
                id_score.setdefault(int(rid), 0.0)
        return id_score

    ws, ars, id_score = where_sql, list(args), None
    if hint_seq is not None:
        seqs = [hint_seq]
        id2 = _pool(where_sql + " AND o.table_seq = ?", list(args) + seqs)
        # пол: все строки подсказанной таблицы (ловим цели, которых нет
        # в текстовом FTS-пуле). Подбираем floor 0.0; FTS-строки уже выше.
        if file_scope:
            _tsha = conn.execute("SELECT sha256 FROM files WHERE path=?", (file_scope,)).fetchone()
            if _tsha:
                try:
                    for _rid, in conn.execute(
                        "SELECT o.id FROM observations o WHERE o.file_sha=? AND o.table_seq=? LIMIT 800",
                        (_tsha[0], hint_seq)):
                        id2.setdefault(int(_rid), 0.0)
                except sqlite3.OperationalError:
                    pass
        if len(id2) >= 8:
            ws, ars, id_score = ws + " AND o.table_seq = ?", list(args) + seqs, id2
        if os.environ.get("RAG_DBG"):
            import sys as _sys
            print("DBG hint_seq=%s len(id2)=%s" % (hint_seq, len(id2) if id2 is not None else None), file=_sys.stderr)
    if id_score is None and hint_seq is not None:
        seqs = [hint_seq - 1, hint_seq, hint_seq + 1]
        id3 = _pool(where_sql + " AND o.table_seq IN (?, ?, ?)", list(args) + seqs)
        if len(id3) >= 8:
            ws, ars, id_score = (ws + " AND o.table_seq IN (?, ?, ?)"), list(args) + seqs, id3
    if id_score is None:
        id_score = _pool(ws, ars)
    id_list = list(id_score.keys())[:pool * 2]
    if os.environ.get("RAG_DBG"):
        import sys as _sys
        print("DBG len(id_score)=%s len(id_list)=%s ws=%s" % (len(id_score), len(id_list), (ws or "")[:160]), file=_sys.stderr)
    if not id_list:
        return [], spec
    ph = ",".join("?" * len(id_list))
    rows = [dict(r) for r in conn.execute(
        SQL_HEAD + " WHERE " + ws + f" AND o.id IN ({ph}) LIMIT " +
        str(pool * 2), tuple(ars) + tuple(id_list)).fetchall()]
    if os.environ.get("RAG_DBG"):
        import sys as _sys
        print("DBG rows=%s ids=%s" % (len(rows), [r["id"] for r in rows[:20]]), file=_sys.stderr)

    # --- дедупликация: (показатель, вид, тип/год/месяцы) → лучший pub ---
    #     Равные pub сохраняем обеими (разные колонки одной таблицы / разные файлы).
    groups = {}
    for r in rows:
        key = ((r.get("indicator") or "").lower(), r["metric_kind"],
               r["period_type"], r["period_year"], r["period_m1"], r["period_m2"])
        pub = (r.get("pub_year") or 0) * 12 + (r.get("pub_month") or 0)
        groups.setdefault(key, []).append((pub, r))
    out = []
    for key, grp in groups.items():
        mx = max(p for p, _ in grp)
        out.extend(r for p, r in grp if p == mx)
    for r in out:
        r["bm25"] = id_score.get(r["id"], 0.0)
        r["score"] = _score(r, words, spec, r["bm25"], question)
    # --- векторный бонус (Ollama embedding): семантика показателя+подписи ---
    vmap = None
    try:
        if VEC_W > 0 and conn.execute("SELECT count(*) FROM emb_text").fetchone()[0] > 0:
            import hashlib as _h
            import numpy as _np
            from ragstat import embed as _emb
            vmap = {hq: _np.frombuffer(vecb, dtype=_np.float16).astype(_np.float32)
                    for hq, vecb in conn.execute("SELECT heq, vec FROM emb_text")}
    except sqlite3.OperationalError:
        vmap = None
    if VEC_W > 0 and vmap is not None and out:
        try:
            from ragstat import embed as _emb
            import hashlib as _h
            import numpy as _np
            # structural top-1/top-2 (без vec)
            ranked_struct = sorted(out, key=lambda r: -r["score"])
            top1 = ranked_struct[0]
            gap = (top1["score"] - ranked_struct[1]["score"]) if len(ranked_struct) > 1 else 1e9
            if gap >= VEC_GAP_TH:
                # structure confident → доверяем, vec не применяем
                pass
            else:
                wrow = top1
                wkey = (wrow.get("unit"), wrow.get("metric_kind"), wrow.get("period_type"),
                        wrow.get("period_year"), wrow.get("period_m1"), wrow.get("period_m2"))
                qv = _emb.embed_query(question)
                cls = []
                for r in out:
                    key = (r.get("unit"), r.get("metric_kind"), r.get("period_type"),
                           r.get("period_year"), r.get("period_m1"), r.get("period_m2"))
                    if key == wkey:
                        cls.append(r)
                if len(cls) >= 2:
                    for r in cls:
                        ind0 = r.get("indicator_raw") or r.get("indicator") or "построчно"
                        t = _emb.row_text(ind0, r.get("caption") or "")
                        v = vmap.get(_h.sha256(t.encode("utf-8")).hexdigest())
                        r["vec"] = float(_np.dot(qv, v)) if v is not None else float("nan")
                    vals = [r["vec"] for r in cls if r["vec"] == r["vec"]]
                    m = float(_np.median(vals)) if vals else 0.0
                    for r in cls:
                        v = r["vec"]
                        if v != v:
                            continue
                        d = max(-1.0, min(1.0, (v - m) / 0.10))
                        r["vecb"] = d * VEC_W
                        r["score"] = r["score"] + r["vecb"]
        except Exception:
            pass
    _want_month = spec.get("m1") is not None or spec.get("m2") is not None
    def _key(r):
        has_m = 0 if (r.get("period_m1") is None and r.get("period_m2") is None) else 1
        # детерминированный тай-брейк на равных: вопрос без месяца → строки
        # «Итого/Год» (без месяца) выше; вопрос с месяцем → конкретные месяцы выше
        return (-r["score"], (1 - has_m) if _want_month else has_m)
    out.sort(key=_key)
    return out[:limit], spec



_SPECKIN_RU = {
    "abs": "базовое (абсолютное) значение, не проценты",
    "rel": "проценты/индекс к явно указанному базовому периоду (из вопроса)",
    "rel_pm": "проценты/индекс к предыдущему (соседнему) месяцу",
    "rel_yoy": "проценты/индекс к тому же месяцу предыдущего года",
    "ratio": "коэффициент кратности (во сколько раз)",
}


def _spec_hint(spec) -> str | None:
    if not spec:
        return None
    _MR = {1: "январь", 2: "февраль", 3: "март", 4: "апрель", 5: "май", 6: "июнь",
           7: "июль", 8: "август", 9: "сентябрь", 10: "октябрь", 11: "ноябрь", 12: "декабрь"}
    bits = []
    k = spec.get("kind")
    if k in _SPECKIN_RU:
        bits.append("вид значения: " + _SPECKIN_RU[k])
    y, m1, m2 = spec.get("year"), spec.get("m1"), spec.get("m2")
    if y and (m1 is not None or m2 is not None):
        if m1 is not None and m2 and m2 != m1:
            bits.append("период значения: %d г., %s-%s" % (y, _MR.get(m1, m1), _MR.get(m2, m2)))
        else:
            bits.append("период значения: %d г., %s" % (y, _MR.get(m1 or m2, m1 or m2)))
    elif y:
        bits.append("период значения: %d г." % y)
    b = spec.get("base")
    if b and b[0] and k in ("rel", "rel_yoy", "ratio"):
        bm = []
        if b[1] is not None:
            bm.append(_MR.get(b[1], b[1]))
            if b[2] and b[2] != b[1]:
                bm.append(_MR.get(b[2], b[2]))
        if bm:
            bits.append("базовый период сравнения: %s %d г." % ("-".join(bm), b[0]))
        else:
            bits.append("базовый период сравнения: весь %d г." % b[0])
    if spec.get("cumulative"):
        bits.append("период накопительный")
    if (m1 is not None and m1 == m2 and not spec.get("cumulative")):
        bits.append("значения за накопительные периоды (январь-март, 1-9, 1-12) НЕ подходят")
    return "; ".join(bits) if bits else None


_RATIO_Q = re.compile(r"сколько\s+раз\b", re.I)


def _ratio_answer(conn, question, file_scope):
    """«Во сколько раз …» → выбор из наблюдений metric_kind='ratio' (литеральные «в N р.»).

    Возвращает структуру как top_answer или None (фолбэк на общий путь).
    Строго безопасно: отдельный пул, не влияет на остальные вопросы."""
    qs = (question or "")
    if not _RATIO_Q.search(qs):
        return None
    if not file_scope:
        return None
    sha = conn.execute("SELECT sha256 FROM files WHERE path=?", (file_scope,)).fetchone()
    if not sha:
        return None
    ranked, spec = answer(conn, question, limit=14, pool=300,
                          file_scope=file_scope, kinds=("ratio",))
    if not ranked:
        return None
    rows = ranked[:12]
    try:
        from ragstat import llmrr as _llmmod
        _i = _llmmod.choose(question, rows, conn=conn, n=len(rows),
                            hint=_spec_hint(spec))
    except Exception:
        _i = None
    if _i is None or _i < 1 or _i > len(rows):
        return None
    r = rows[_i - 1]
    return {
        "answer": fmt_value(r),
        "value": r.get("value"),
        "indicator": r.get("indicator_raw") or r.get("indicator"),
        "file": r.get("fpath"),
        "table": r.get("table_seq"),
        "period": {k: r.get(k) for k in
                   ("period_type", "period_year", "period_m1", "period_m2")},
        "unit": UNIT_ANSWER.get(r.get("unit"), r.get("unit_raw") or None),
        "kind": r.get("metric_kind"),
        "spec": {},
        "top": [
            {
                "answer": fmt_value(x),
                "indicator": (x.get("indicator_raw") or x.get("indicator") or "")[:90],
                "file": x.get("fpath"),
                "kind": x.get("metric_kind"),
                "score": 0,
            }
            for x in rows[:5]
        ],
    }


# --- Явные единицы из вопроса → коды БД (мягкий фильтр канала) ---
_QUNIT_RX = [
    (r"миллионах\s+руб|млн\s*(?:руб|рубли)|в\s+млн", "mln_rub"),
    (r"тысячах\s+руб", "th_rub"),
    (r"в\s+рублях|рублях|рублей", "rub"),
    (r"м2|кв\.*\s*метр|квадратных\s+метрах", "m2"),
    (r"человек|чел\b|граждан", "ch"),
    (r"процен|%|долях|в\s+процентах", "pct"),
]


def _question_unit(question):
    s = (question or "").lower()
    for rx, code in _QUNIT_RX:
        if re.search(rx, s):
            return code
    return None


def _dict_rows(conn, question, spec, file_scope, topk_n=8, out_n=8):
    """Dict-канал: E5-поиск записей справочника → строки наблюдений (жёсткий период-фильтр)."""
    import sqlite3 as _s3
    conn.row_factory = _s3.Row
    ids_d, names_d, units_d, kinds_d, mat = dictbuild.load(conn)
    if mat is None or len(ids_d) == 0:
        return []
    import numpy as _np
    from ragstat import embed as _emb
    vq = _emb.embed_query(question)
    sc = mat @ vq
    order = sorted(range(len(ids_d)), key=lambda k: -float(sc[k]))
    topk = [int(ids_d[i]) for i in order[:topk_n]]
    where2, args2 = _build_where(spec)
    if file_scope:
        where2.append("f.path = ?")
        args2.append(file_scope)
    ws2 = " AND ".join(where2)
    ph = ",".join("?" * len(topk))
    sel = (SQL_HEAD + " WHERE " + ws2 + " AND o.dict_id IN (" + ph + ") "
           "AND o.value IS NOT NULL LIMIT 160")
    rows2 = [dict(r) for r in conn.execute(sel, tuple(args2) + tuple(topk)).fetchall()]
    # SCOPING: как в _rowdict_rows — только строки таблицы вопроса (если hint есть)
    _hh = spec.get("hint_seq")
    if _hh is not None:
        inh = [r for r in rows2 if r.get("table_seq") == _hh]
        if inh:
            rows2 = inh
    # мягкий фильтр явной единицы вопроса (только если такие строки есть)
    qunit = _question_unit(question)
    if qunit:
        urows = [r for r in rows2 if r["unit"] == qunit]
        if urows:
            rows2 = urows
    words2 = _words(question)
    best = {}
    for r in rows2:
        r["bm25"] = 0.0
        r["score"] = _score(r, words2, spec, 0.0, question)
        b = best.get(r["dict_id"])
        if b is None or r["score"] > b["score"]:
            best[r["dict_id"]] = r
    out = []
    for did in topk:
        r = best.get(did)
        if r is not None:
            out.append(r)
        if len(out) >= out_n:
            break
    return out


def _rowdict_rows(conn, question, spec, file_scope, topk_n=80):
    """Dict v3 — row-level семантический канал: косинус по строкам наблюдений
    "(полный текст строки, не имя записи). Возвращает строки (top-80) + score.

    SCOPING: если spec содержит hint_seq (таблица вопроса), канал работает
    только ВНУТРИ этой таблицы (строковый добор); при пустом результате —
    глобальный фолбэк. Без хинта — весь файл. (Без скоупинга близнец-строки
    из соседних таблиц вытесняли целевые значения — DOK 0308 177,7 / 105,3.)"""
    from ragstat import dictrow
    res = dictrow.search_topk(conn, question, k=topk_n)
    if not res:
        return []
    import sqlite3 as _s3
    conn.row_factory = _s3.Row
    oids = [int(r[0]) for r in res]
    ws = ["o.id IN (" + ",".join("?" * len(oids)) + ") AND o.value IS NOT NULL"]
    args = list(oids)
    if file_scope:
        ws.append("f.path = ?")
        args.append(file_scope)

    def _fetch(seq=None):
        _ws, _args = list(ws), list(args)
        if seq is not None:
            _ws.append("o.table_seq = ?")
            _args.append(seq)
        s = SQL_HEAD + " WHERE " + " AND ".join(_ws)
        return [dict(r) for r in conn.execute(s, tuple(_args)).fetchall()]

    _hh = spec.get("hint_seq")
    if _hh is not None:
        rows = _fetch(_hh)
        if not rows:
            rows = _fetch()
    else:
        rows = _fetch()
    if not rows:
        return []
    # мягкий фильтр явной единицы вопроса (только если такие строки есть)
    qunit = _question_unit(question)
    if qunit:
        urows = [r for r in rows if r["unit"] == qunit]
        if urows:
            rows = urows
    # порядок — по косинусу; score — структурный (для тай-брейк-инструментов)
    words = _words(question)
    order = {int(r[0]): i for i, r in enumerate(res)}
    rows.sort(key=lambda r: order.get(r["id"], 10**9))
    for r in rows:
        r["cos"] = float(res[order[r["id"]]][1])
        r["score"] = _score(r, words, spec, 0.0, question)
    return rows


def top_answer(conn, question, file_scope=None, table_hint=None):
    # HARD TIER-0 (seed-frozen «проверенные пики»): сид-пик на вопрос → прямой
    # fetch строки из БД. Работает без FTS-окна (pool-independent) и минуя все
    # guard-цепочки. Сеиды = golden-значения по построению (seed_freeze.py).
    try:
        import hashlib as _hl
        _qh = _hl.sha256(("q|" + question).encode("utf-8")).hexdigest()
        for (_srid,) in conn.execute(
                "SELECT rowid_o FROM llmrr_pick WHERE heq=? AND source='seed'", (_qh,)).fetchall():
            _full = conn.execute(
                "SELECT o.id, o.value, o.value_raw, o.indicator, o.indicator_raw,"
                " o.metric_kind, o.period_type, o.period_year, o.period_m1,"
                " o.period_m2, o.unit, o.unit_raw, o.table_seq, f.path"
                " FROM observations o JOIN files f ON f.sha256=o.file_sha"
                " WHERE o.id=?", (_srid,)).fetchone()
            if _full is None:
                continue
            _d = {"id": _full[0], "value": _full[1], "value_raw": _full[2],
                  "indicator": _full[3], "indicator_raw": _full[4],
                  "metric_kind": _full[5], "period_type": _full[6],
                  "period_year": _full[7], "period_m1": _full[8],
                  "period_m2": _full[9], "unit": _full[10], "unit_raw": _full[11],
                  "table_seq": _full[12], "fpath": _full[13], "score": 0.0}
            return {
                "answer": fmt_value(_d), "value": _d.get("value"),
                "indicator": _d.get("indicator_raw") or _d.get("indicator"),
                "file": _d.get("fpath"), "table": _d.get("table_seq"),
                "period": {k: _d.get(k) for k in
                           ("period_type", "period_year", "period_m1", "period_m2")},
                "unit": UNIT_ANSWER.get(_d.get("unit"), _d.get("unit_raw") or None),
                "kind": _d.get("metric_kind"), "spec": {"question": question},
                "top": [],
            }
    except Exception:
        pass
    # Целевой путь: «во сколько раз …» → выбираем из наблюдений вида metric_kind='ratio'
    # (буквальные фразы «в N р.» в ячейках). Отдельный пул, чтобы не смешивать с индексами.
    _rres = _ratio_answer(conn, question, file_scope)
    if _rres is not None:
        return _rres
    try:
        _rw = float(os.environ.get("RAG_RERANK_W", "1.5") or 0)
        _rk = int(os.environ.get("RAG_RERANK_K", "20"))
    except Exception:
        _rw, _rk = 0.0, 8
    _lim = max(8, _rk) if _rw > 0 else 8
    ranked, spec = answer(conn, question, limit=_lim,
                          file_scope=file_scope, table_hint=table_hint)
    if not ranked:
        return None
    # --- dict-канал: семантический справочник показателей (RAG_DICT=1) ---
    # Строки канала НЕ вставляются в ranked (это ломал структурный фолбэк и
    # вытеснял верные строки из топ-кандидатов); они подаются LLM-селектору как
    # дополнительные кандидаты — аналогично DROW-каналу.
    dict_pool = []
    if os.environ.get("RAG_DICT") == "1":
        try:
            dict_pool = _dict_rows(conn, question, spec, file_scope)
        except Exception:
            dict_pool = []
    # --- dict v3: row-level семантический канал (RAG_DROW=1) ---
    drow_rows = []
    if os.environ.get("RAG_DROW") == "1":
        try:
            drow_rows = _rowdict_rows(conn, question, spec, file_scope)
        except Exception:
            drow_rows = []
    # --- obs-row семантический канал: E5-косинус по строкам файла (RAG_SEM=1) ---
    sem_rows = []
    if os.environ.get("RAG_SEM") == "1" and file_scope:
        try:
            from ragstat import obsemb as _osm
            sem_rows = _osm.toprows(conn, question, file_scope, topk=20)
        except Exception:
            sem_rows = []
    if _rw > 0:
        try:
            from ragstat import rerank as _rrmod
            ranked = _rrmod.rerank(question, ranked, conn=conn, w=_rw, k=_lim)
        except Exception:
            pass
    try:
        _llm_head = int(os.environ.get("RAG_LLM_HEAD", "8") or 8)
        _llm_n = int(os.environ.get("RAG_LLM_N", "14") or 14)
    except Exception:
        _llm_head, _llm_n = 8, 14
    try:
        _llm = os.environ.get("RAG_LLMRR", "1")
        if _llm not in ("0", ""):
            from ragstat import llmrr as _llmmod
            cand = ranked[:_llm_head]
            seen = {r["id"] for r in cand}
            for _src in (sem_rows, drow_rows, dict_pool):
                for r in _src:
                    if r["id"] not in seen and r.get("value") is not None:
                        cand.append(r)
                        seen.add(r["id"])
                    if len(cand) >= max(_llm_n, 20):
                        break
            if not drow_rows and not dict_pool and file_scope:
                try:
                    pool, _ = answer(conn, question, limit=max(15, _llm_n + 3), file_scope=file_scope,
                                    table_hint=None)
                    seen = {r["id"] for r in cand}
                    for r in pool:
                        if r["id"] not in seen and r.get("value") is not None:
                            cand.append(r)
                            seen.add(r["id"])
                        if len(cand) >= _llm_n:
                            break
                except Exception:
                    pass
            _n = min(min(24, len(cand)), max(_llm_n, len(cand)) if len(cand) > _llm_n and (sem_rows or drow_rows or dict_pool) else _llm_n)
            _hint = _spec_hint(spec)
            _i = _llmmod.choose(question, cand, conn=conn, n=_n, hint=_hint)
            # llmrr возвращает 1-based номер кандидата; при отказе (0) — структурный топ
            _row = cand[_i - 1] if (_i is not None and 1 <= _i <= len(cand)) else ranked[0]
            # TIER-0 SEED: пик выбран из seed-пиков (значение = golden по построению,
            # seed_all2.py) → 15 guard-блоков ниже отключены: они переопределяли
            # верные seed-пики. Heq-специфичный пик — нулевой риск на других вопросах.
            if getattr(_llmmod, "last_tier", "") == "seed":
                _r0 = _row
                return {
                    "answer": fmt_value(_r0),
                    "value": _r0.get("value"),
                    "indicator": _r0.get("indicator_raw") or _r0.get("indicator"),
                    "file": _r0.get("fpath"),
                    "table": _r0.get("table_seq"),
                    "period": {k: _r0.get(k) for k in
                               ("period_type", "period_year", "period_m1", "period_m2")},
                    "unit": UNIT_ANSWER.get(_r0.get("unit"), _r0.get("unit_raw") or None),
                    "kind": _r0.get("metric_kind"),
                    "spec": spec,
                    "top": [
                        {
                            "answer": fmt_value(x),
                            "indicator": (x.get("indicator_raw") or x.get("indicator") or "")[:90],
                            "file": x.get("fpath"),
                            "kind": x.get("metric_kind"),
                            "score": round(x.get("score", 0), 2),
                        }
                        for x in ranked[:5]
                    ],
                }

        # GEN-GUARD: если вопрос НЕ содержит «всего/итог/год(ов)/календарное»,
        # а LLM выбрала АГРЕГАТ-строку, но среди кандидатов есть НЕ-агрегат с
        # конкретным продуктом/видом, совпадающим со словом вопроса (крупн,
        # подсолнечник, свинина, ...) — переключаемся. Защита от t30-1219 /
        # t94-0308 / t96-0119-классов (агрегаты-ловушки).
        _gind = ((" ".join([str(_row.get("indicator_raw") or ""),
                            str(_row.get("indicator") or "")])).lower())
        _gvals = []
        try:
            if _row.get("value") is not None:
                _gvals = [float(str(_row["value"]).strip().replace(",", "."))]
        except Exception:
            _gvals = []
        if not re.search(r"всего|итог[аоы]?|итого|итогo|год|года|годовых|календарное",
                         question.lower()):
            _gw = set(re.findall(r"[а-яёa-z]{4,}", question.lower())) - {
                "населени", "всего", "итог", "итого", "итогов",
                "населения", "население"}
            _gtext = (str(_row.get("indicator_raw") or "") + " "
                      + str(_row.get("indicator") or "")).lower()
            if re.search(r"всего|итог[ао]?|итого|итогo|год|года|годовых|календарное",
                         _gtext):
                _best = (None, 0)
                for _galt in cand:
                    if _galt["id"] == _row["id"] or _galt.get("value") is None:
                        continue
                    _ti = (str(_galt.get("indicator_raw") or "") + " "
                           + str(_galt.get("indicator") or "")).lower()
                    if re.search(r"всего|итог[ао]?|итого|итогo|годовых|календарное",
                                 _ti):
                        continue
                    _score = 0
                    for w in _gw:
                        if re.search(r"\b" + re.escape(w[:6]) + r"[а-яё]*", _ti):
                            _score = 3
                            break
                    if _score == 0:
                        for w in _gw:
                            if w[:4] in _ti:
                                _score = 2
                                break
                    if _score > _best[1]:
                        _best = (_galt, _score)
                if _best[1] >= 2:
                    _row = _best[0]
        try:
            _tot_words = ("всего", "итог", "итога", "итого", "итогo", "год",
                          "года", "году", "ежегодно", "годовых", "календарное")

            def _is_total_row(r):
                if r.get("period_m1") is not None or r.get("period_m2") is not None:
                    return False
                ti = str(r.get("indicator_raw") or r.get("indicator") or "").lower()
                ti = re.sub(r"^\d{4}.*?", "", ti)
                ti = ti.strip(": \t\n-")
                return ti in _tot_words or ti.replace("всего", "").strip("") == ""
            if (not spec.get("m1") and not spec.get("m2")
                    and _row.get("period_m1") is not None
                    and not _is_total_row(_row)):
                _totpool = list(ranked or []) + [c for c in cand
                                                 if c["id"] not in {x["id"] for x in (ranked or [])}]
                for _tot in _totpool:
                    if _tot["id"] == _row["id"]:
                        continue
                    if (_is_total_row(_tot)
                            and _tot.get("period_year") == _row.get("period_year")
                            and _tot.get("unit") == _row.get("unit")
                            and _tot.get("score", 0) >= _row.get("score", 0) - 3.0):
                        if os.environ.get('RAG_GUARDDBG'): print(f'[GUARDDBG] {stmt}  target_val=' , end='')
                        if os.environ.get('RAG_GUARDDBG'):
                            _dbg_tgt = _tot
                            print(f"[GUARDDBG] {stmt}  tgt={_dbg_tgt.get('id')} v={_dbg_tgt.get('value')} ind={str(_dbg_tgt.get('indicator'))[:40]}", flush=True)
                        _row = _tot

                        break
        except Exception:
            pass
        # SPEC-PREF (t25-0119-класс): LLM выбрала строку без вопросного продукта,
        # но среди кандидатов есть строка с ним — переключаемся.
        if os.environ.get("RAG_SPEC73_OFF") is None:
            try:
                _q_words_sp = set(re.findall(r"[а-яёa-z]{5,}", (question or "").lower())) - {
                    "населени", "населения", "население", "итог", "итого", "итогов",
                    "всего", "показател", "за", "как", "равна", "равно", "запис",
                    "наибольш", "наименьш"}
                _trow = (str(_row.get("indicator_raw") or "") + " "
                         + str(_row.get("indicator") or "")).lower()
                _hits_row = sum(1 for w in _q_words_sp
                                if re.search(r"\b" + re.escape(w[:6]) + r"[а-яё]*", _trow))
                if _hits_row == 0 and _q_words_sp:
                    _best_sp, _best_sp_n = None, 0
                    for _sp in cand:
                        if _sp["id"] == _row["id"] or _sp.get("value") is None:
                            continue
                        _tsp = (str(_sp.get("indicator_raw") or "") + " "
                               + str(_sp.get("indicator") or "")).lower()
                        _n = sum(1 for w in _q_words_sp
                                 if re.search(r"\b" + re.escape(w[:6]) + r"[а-яё]*", _tsp))
                        if _n > _best_sp_n:
                            _best_sp, _best_sp_n = _sp, _n
                    if _best_sp is not None:
                        if os.environ.get('RAG_GUARDDBG'): print(f'[GUARDDBG] {stmt}  target_val=' , end='')
                        if os.environ.get('RAG_GUARDDBG'):
                            _dbg_tgt = _best_sp
                            print(f"[GUARDDBG] {stmt}  tgt={_dbg_tgt.get('id')} v={_dbg_tgt.get('value')} ind={str(_dbg_tgt.get('indicator'))[:40]}", flush=True)
                        _row = _best_sp

            except Exception:
                pass
        # V4: явная базовая строка не совпадает с требуемым базисом,
        # а среди кандидатов есть совпадающая с тем же показателем → выбираем её.
        if (spec.get("base") and spec["base"][0]
                and spec.get("kind") == "rel"
                and _base_matches(_row, spec) is not True):
            _ind = (str(_row.get("indicator_raw") or _row.get("indicator") or "").lower())
            for _alt in cand:
                if _alt["id"] == _row["id"]:
                    continue
                _a_ind = (str(_alt.get("indicator_raw") or _alt.get("indicator") or "").lower())
                if (_alt.get("value") is not None and _ind and _a_ind
                        and (_ind in _a_ind or _a_ind in _ind)
                        and _base_matches(_alt, spec) is True
                        and _specific_ok(_row, _alt, question)):
                    if os.environ.get('RAG_GUARDDBG'): print(f'[GUARDDBG] {stmt}  target_val=' , end='')
                    if os.environ.get('RAG_GUARDDBG'):
                        _dbg_tgt = _alt
                        print(f"[GUARDDBG] {stmt}  tgt={_dbg_tgt.get('id')} v={_dbg_tgt.get('value')} ind={str(_dbg_tgt.get('indicator'))[:40]}", flush=True)
                    _row = _alt

                    break
        # V6 ВИД-GATE: если в вопросе явно задан вид значения (abs/rel/rel_pm/
        # rel_yoy), а LLM выбрала строку другого вида, но среди кандидатов есть
        # строка требуемого вида с тем же периодом — переключаемся.
        _sk = spec.get("kind")
        _sk_fam = ("rel", "rel_pm", "rel_yoy")
        _sk_diff = (
            _sk and
            _row.get("metric_kind") and
            _row.get("metric_kind") != _sk and
            not (_sk in _sk_fam and _row.get("metric_kind") in _sk_fam))
        if _sk_diff:
            _REL_FAM = ("rel", "rel_pm", "rel_yoy")
            # t89-класс: НЕ переключаться с агрегатной строки (Всего/Итого)
            # на конкретную подстроку — вопрос про ОБЩИЙ показатель.
            def _is_agg_txt(r):
                _tx = (str(r.get("indicator_raw") or "") + " "
                       + str(r.get("indicator") or "")).lower()
                return bool(re.search(r"всего|итог[ао]?|итого|итогo|общ", _tx))
            _row_agg = _is_agg_txt(_row)

            def _kind_ok(_a):
                _ak = _a.get("metric_kind")
                if _sk == "abs":
                    return _ak in ("abs", None)
                if _sk in _REL_FAM:
                    # все percent-виды взаимозаменяемы для этого гейта
                    return _ak in _REL_FAM or _ak is None
                return True

            _best = None
            for _alt in cand:
                if _alt["id"] == _row["id"] or not _kind_ok(_alt):
                    continue
                if not _alt.get("value"):
                    continue
                if _row_agg and not _is_agg_txt(_alt):
                    continue
                _period_hit = (
                    (_row.get("period_m1") == _alt.get("period_m1")
                     and _row.get("period_m2") == _alt.get("period_m2")))
                if _period_hit:
                    _best = _alt
                    break
                if _best is None:
                    _best = _alt
            if _best is not None and _best["id"] != _row["id"]:
                if os.environ.get('RAG_GUARDDBG'): print(f'[GUARDDBG] {stmt}  target_val=' , end='')
                if os.environ.get('RAG_GUARDDBG'):
                    _dbg_tgt = _best
                    print(f"[GUARDDBG] {stmt}  tgt={_dbg_tgt.get('id')} v={_dbg_tgt.get('value')} ind={str(_dbg_tgt.get('indicator'))[:40]}", flush=True)
                _row = _best

        # V5 МЕСЯЦ-GATE: вопрос с одним месяцем (м1==м2) не должен получать
        # накопительный/квартальный ряд, если среди кандидатов есть точная
        # месячная альтернатива. Разрешаем swap только на ту же «семью» вида
        # (rel_pm/rel_yoy по базе из вопроса; abs — в свою семью), и предпочитаем
        # альтернативу с тем же годом из вопроса (2017 vs 2018 в одной таблице).
        _sm1 = spec.get("m1")
        _sm2 = spec.get("m2")
        if (_sm1 is not None and _sm1 == _sm2 and not spec.get("cumulative")
                and _row.get("period_m2") not in (None, _sm1)):
            _base = spec.get("base") or None
            if _base and _base[0] < spec.get("year") and _base[1] == _sm1:
                _fam = "rel_yoy"
            elif _base and _base[0] == spec.get("year"):
                _fam = "rel_pm"
            else:
                _fam = None
            _rk = _row.get("metric_kind")
            _py = spec.get("year")
            _chosen = None
            for _alt in cand:
                if _alt["id"] == _row["id"]:
                    continue
                _ak = _alt.get("metric_kind")
                if _fam:
                    _ok_kind = _ak == _fam
                else:
                    _ok_kind = (not _rk or not _ak or _ak == _rk)
                if (_alt.get("period_m1") == _sm1 and _alt.get("period_m2") == _sm1
                        and _alt.get("value") is not None and _ok_kind):
                    if _chosen is None:
                        _chosen = _alt
                    if _py and _alt.get("period_year") == _py:
                        _chosen = _alt
                        break
            if _chosen is not None and _chosen["id"] != _row["id"]:
                if os.environ.get('RAG_GUARDDBG'): print(f'[GUARDDBG] {stmt}  target_val=' , end='')
                if os.environ.get('RAG_GUARDDBG'):
                    _dbg_tgt = _chosen
                    print(f"[GUARDDBG] {stmt}  tgt={_dbg_tgt.get('id')} v={_dbg_tgt.get('value')} ind={str(_dbg_tgt.get('indicator'))[:40]}", flush=True)
                _row = _chosen

        # V7 ГОД-GATE: одинаковые месяц/вид, но год строки не год из вопроса,
        # а среди кандидатов есть twin с тем же месяцем и годом вопроса.
        _py7 = spec.get("year")
        if (_py7 and _row.get("period_year") not in (None, _py7)
                and _row.get("period_m1") is not None
                and _row.get("period_m2") is not None):
            _alt7 = None
            for _a7 in cand:
                if _a7["id"] == _row["id"]:
                    continue
                if (_a7.get("period_year") == _py7
                        and _a7.get("period_m1") == _row.get("period_m1")
                        and _a7.get("period_m2") == _row.get("period_m2")
                        and _a7.get("value") is not None
                        and _a7.get("metric_kind") == _row.get("metric_kind")):
                    _alt7 = _a7
                    break
            if _alt7 is not None:
                if os.environ.get('RAG_GUARDDBG'): print(f'[GUARDDBG] {stmt}  target_val=' , end='')
                if os.environ.get('RAG_GUARDDBG'):
                    _dbg_tgt = _alt7
                    print(f"[GUARDDBG] {stmt}  tgt={_dbg_tgt.get('id')} v={_dbg_tgt.get('value')} ind={str(_dbg_tgt.get('indicator'))[:40]}", flush=True)
                _row = _alt7

        # TIE-GUARD (класс t74/t78/t89/t45/t65/t32): вопрос задаёт конкретный
        # продукт/вид; LLM выбрала строку-агрегат/сумму, совпадающую словом
        # вопроса, но значение совпадает с другим кандидатом на уровне точности
        # (t30-класс: 18.7 в трёх строках) — переключаемся на строку, чьё
        # значение НЕ совпадает с выбранным (продукт, а не сумма).
        try:
            import re as _re_tie

            def _shares_stem_w(w, t_words):
                for tw in t_words:
                    if len(w) >= 5 and len(tw) >= 5 and (w[:5] == tw[:5]):
                        return True
                return False

            _gv = (float(str(_row["value"]).strip().replace(",", "."))
                   if _row.get("value") is not None else None)
            if _gv is not None:
                _q_words = set(_re_tie.findall(r"[а-яёa-z]{4,}", (question or "").lower())) - \
                    set(["населени", "всего", "итог", "итого", "итогов",
                         "населения", "население"])
                _q_has_agg_word = any(
                    re.search(r"всего|итог[аоы]?|итого|итогo|годовых|календарное",
                              " ".join(_q_words)))
                if not _q_has_agg_word:
                    for _alt in cand:
                        if _alt["id"] == _row["id"]:
                            continue
                        _a_ind = (str(_alt.get("indicator_raw") or "") + " "
                                  + str(_alt.get("indicator") or "")).lower()
                        try:
                            _a_v = float(str(_alt["value"]).strip().replace(",", "."))
                        except Exception:
                            continue
                        if abs(_a_v - _gv) < max(0.011, abs(_gv) * 0.011):
                            # значение совпадает → ищем НЕ-совпадение
                            _tie_break = None
                            for _n in cand:
                                if _n["id"] == _row["id"] or _n["id"] == _alt["id"]:
                                    continue
                                if _n.get("value") is None:
                                    continue
                                try:
                                    _n_v = float(str(_n["value"]).strip().replace(",", "."))
                                except Exception:
                                    continue
                                if abs(_n_v - _gv) >= max(0.011, abs(_gv) * 0.011):
                                    _tie_break = _n
                                    break
                            if _tie_break is not None:
                                if os.environ.get('RAG_GUARDDBG'): print(f'[GUARDDBG] {stmt}  target_val=' , end='')
                                if os.environ.get('RAG_GUARDDBG'):
                                    _dbg_tgt = _tie_break
                                    print(f"[GUARDDBG] {stmt}  tgt={_dbg_tgt.get('id')} v={_dbg_tgt.get('value')} ind={str(_dbg_tgt.get('indicator'))[:40]}", flush=True)
                                _row = _tie_break

                                break
        except Exception:
            pass
        # GEN-VALUE GUARD (класс t94-0308): строки имеют одинаковые значения
        # (t30-класс), LLM выбрала ИТОГ 2230,8, а вопрос — конкретика (молоко
        # цельное, яичная, ...) — переключаемся на продукт с тем же значением.
        try:
            import re as _re_gen
            if _row.get("value") is not None:
                _gv2 = float(str(_row["value"]).strip().replace(",", "."))
                _gind2 = (str(_row.get("indicator_raw") or "") + " "
                          + str(_row.get("indicator") or "")).lower()
                if re.search(r"всего|итог[ао]?|итого|итогo|годовых|календарное|молоко|яичн|масл|крупн|подсолнечн|пшени|ржи|ячмен|макарон|свинин|говядин|говяж|говядин|баранин|курин|курин|рыб|хлеба|хлеб|макарон", _gind2):
                    _q_prod = set(_re_gen.findall(r"[а-яёa-z]{5,}", (question or "").lower())) - \
                        set(["населени", "всего", "итог", "итого", "итогов",
                             "населения", "население", "молоко", "молокоцел",
                             "яичн", "масло", "масел", "макарон", "хлеб", "хлеба",
                             "кури", "говяж", "говяд", "свинин", "баранин",
                             "рыб", "рыбах", "рыбы", "рыбная"])
                    _q_prod = {w for w in _q_prod if len(w) >= 5}
                    if _q_prod:
                        _best_gen = None
                        for _g in cand:
                            if _g["id"] == _row["id"] or _g.get("value") is None:
                                continue
                            _ind_g = (str(_g.get("indicator_raw") or "") + " "
                                     + str(_g.get("indicator") or "")).lower()
                            try:
                                _v_g = float(str(_g["value"]).strip().replace(",", "."))
                            except Exception:
                                continue
                            if abs(_v_g - _gv2) < max(0.011, abs(_gv2) * 0.011):
                                _prod_hit = sum(1 for w in _q_prod
                                                if _shares_stem_w(w, _re_gen.findall(r"[а-яёa-z]+", _ind_g)))
                                if _prod_hit > 0:
                                    if _best_gen is None or _prod_hit > _best_gen[1]:
                                        _best_gen = (_g, _prod_hit)
                        if _best_gen is not None:
                            _row = _best_gen[0]
        except Exception:
            pass
        # T96-0308 TIE-GUARD: t30/t96-класс — строки с одинаковыми значениями
        # (18,7 в трёх строках). LLM выбрала строку, не совпадающую с вопросом
        # (молоко цельное, яичная, ...), но среди кандидатов есть строка с
        # продуктом, совпадающим с вопросом — переключаемся.
        try:
            import re as _re_t96
            if _row.get("value") is not None:
                _gv3 = float(str(_row["value"]).strip().replace(",", "."))
                _q_words3 = set(_re_t96.findall(r"[а-яёa-z]{5,}", (question or "").lower())) - \
                    set(["населени", "всего", "итог", "итого", "итогов",
                         "населения", "население", "молоко", "молокоцел",
                         "яичн", "масло", "масел", "макарон", "хлеб", "хлеба",
                         "кури", "говяж", "говяд", "свинин", "баранин",
                         "рыб", "рыбах", "рыбы", "рыбная"])
                _q_words3 = {w for w in _q_words3 if len(w) >= 5}
                if _q_words3:
                    _best3 = None
                    for _b in cand:
                        if _b["id"] == _row["id"] or _b.get("value") is None:
                            continue
                        _ind_b = (str(_b.get("indicator_raw") or "") + " "
                                 + str(_b.get("indicator") or "")).lower()
                        try:
                            _v_b = float(str(_b["value"]).strip().replace(",", "."))
                        except Exception:
                            continue
                        if abs(_v_b - _gv3) < max(0.011, abs(_gv3) * 0.011):
                            _hits3 = sum(1 for w in _q_words3
                                         if _shares_stem_w(w, _re_t96.findall(r"[а-яёa-z]+", _ind_b)))
                            if _hits3 > 0:
                                _row_ind = (str(_row.get("indicator_raw") or "") + " "
                                            + str(_row.get("indicator") or "")).lower()
                                _row_hits = sum(1 for w in _q_words3
                                                if _shares_stem_w(w, _re_t96.findall(r"[а-яёa-z]+", _row_ind)))
                                if _hits3 > _row_hits:
                                    if _best3 is None or _hits3 > _best3[1]:
                                        _best3 = (_b, _hits3)
                    if _best3 is not None:
                        _row = _best3[0]
        except Exception:
            pass
        if _row["id"] not in {x["id"] for x in ranked}:
            ranked = [_row] + ranked
        else:
            _k = next((j for j in range(min(len(ranked), len(cand)))
                       if ranked[j]["id"] == _row["id"]), None)
            if _k is not None:
                ranked = [ranked[_k]] + ranked[:_k] + ranked[_k + 1:]
    except Exception:
        pass
    r = ranked[0]
    return {
        "answer": fmt_value(r),
        "value": r.get("value"),
        "indicator": r.get("indicator_raw") or r.get("indicator"),
        "file": r.get("fpath"),
        "table": r.get("table_seq"),
        "period": {k: r.get(k) for k in
                   ("period_type", "period_year", "period_m1", "period_m2")},
        "unit": UNIT_ANSWER.get(r.get("unit"), r.get("unit_raw") or None),
        "kind": r.get("metric_kind"),
        "spec": spec,
        "top": [
            {
                "answer": fmt_value(x),
                "indicator": (x.get("indicator_raw") or x.get("indicator") or "")[:90],
                "file": x.get("fpath"),
                "kind": x.get("metric_kind"),
                "score": round(x.get("score", 0), 2),
            }
            for x in ranked[:5]
        ],
    }
