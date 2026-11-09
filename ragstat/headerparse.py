# -*- coding: utf-8 -*-
"""Построение спецификаций колонок (період/тип метрики/базис/единица) из шапки."""
import re

from .textnorm import extract_unit, collapse
from .periodnorm import parse_period, _find_months, _MONTH_ALT
REL_RE = re.compile(
    r"в\s?%\s?к|в\s?процент\w*\s?к|по\s?отношению\s?к|по\s?сравнению\s?с"
    r"|к\s+(?:аналог\w*|соответствующ\w"
    r"|предыдущ\w*\s+(?:месяц\w*|квартал\w*|период\w*)"
    r"|пред\.\s+(?:мес\w*|кв\w*|период\w*))",
    re.I,
)


def build_col(frags):
    """frags: [текст, ...] — фрагменты шапки столбца сверху вниз (без дублей)."""
    text = " ".join(frags)
    low = text.lower()
    spec = {
        "kind": "abs",            # abs | rel_yoy | rel_pm | rel
        "period": None,           # {'kind','year','m1','m2'} | None
        "year": None,
        "base": None,             # (year, month) | None
        "ref": "справочн" in low,
        "unit": extract_unit(text),
    }
    if not text or text in ("х", "хх", "«...»", "..."):
        spec["heading"] = None
        return spec
    # текст шапки колонки — сохраняем (для ранжирования и эмбеддингов),
    # иначе «схлопываются» столбцы «в % к соответствующему периоду» и «в % к предыдущему»
    spec["heading"] = collapse(text)[:250]

    # глобальный год (для месяцев без явного года)
    ym = re.search(r"\b(\d{4})\b", text)
    global_year = int(ym.group(1)) if ym else None

    months = _find_months(low)
    months = [(m, (yr if yr else global_year), pos) for m, yr, pos in months]

    rel = bool(REL_RE.search(low))
    if "справочн" in low:
        spec["ref"] = True

    if rel:
        if "предыдущ" in low:
            # «к предыдущему месяцу/периоду» — rel_pm;
            # «к ... предыдущего года» — rel_yoy.
            spec["kind"] = "rel_yoy" if re.search(r"предыдущ\w+\s+год\w*", low) else "rel_pm"
            if months:
                mnum, yr, _ = months[0]
                spec["period"] = {"kind": "month", "year": yr, "m1": mnum, "m2": mnum}
                spec["year"] = yr
                if spec["kind"] == "rel_pm" and yr and mnum:
                    # базис = предшествующий месяц («к предыдущему месяцу»)
                    spec["base"] = (yr - 1, 12) if mnum == 1 else (yr, mnum - 1)
        elif len(months) >= 2:
            tgt = max(months, key=lambda x: ((x[1] or 0), x[0]))
            base = min(months, key=lambda x: ((x[1] or 0), x[0]))
            t_year, b_year = tgt[1], base[1]
            if b_year is None:  # «к январю - марту2007»: год базиса у хвоста диапазона
                b_year = next((y for (mm, y, pos) in months
                               if y and (t_year is None or y < t_year)), None)
            spec["year"] = t_year
            if t_year and b_year is not None and t_year == b_year:
                # "к январю - марту 20дд" — сравнение с базовым диапазоном
                spec["kind"] = "rel"
                spec["period"] = {"kind": "range", "year": t_year, "m1": base[0], "m2": tgt[0]}
            else:
                tgt_pos = tgt[2]
                if b_year:
                    base_pos_min = min(pos for (mm, y, pos) in months if y == b_year)
                else:
                    base_pos_min = None
                v_months = sorted(set(mm for (mm, y, pos) in months
                                      if y == t_year
                                      or (y is None and pos < tgt_pos
                                          and (base_pos_min is None or pos < base_pos_min))))
                if len(v_months) >= 2:
                    spec["period"] = {"kind": "range", "year": t_year,
                                     "m1": v_months[0], "m2": v_months[-1]}
                else:
                    spec["period"] = {"kind": "month", "year": t_year, "m1": tgt[0], "m2": tgt[0]}
                if t_year and b_year is not None and t_year == b_year + 1:
                    spec["kind"] = "rel_yoy"
            spec["base"] = (b_year, base[0])
        else:
            # «в % к соответствующему периоду предыдущего года» — простое сравнение (rel);
            # тонкое разбиение yoy/pm — в ранжировании по тексту вопроса
            spec["kind"] = "rel"
            if months:
                mnum, yr, _ = months[0]
                spec["period"] = {"kind": "month", "year": yr, "m1": mnum, "m2": mnum}
    else:
        # abs: диапазон/квартал/месяц/год
        _tok_k = (re.search(r"к\s+(" + _MONTH_ALT + r")", low)
                  or re.search(r"\sк\s*[:к]*", low)
                  or "к" in re.findall(r"[а-яё]+", low))
        if len(months) >= 2 and _tok_k:
            # «март 2008 года к декабрю 2007 года» — индекс к базисному месяцу
            spec["kind"] = "rel"
            tgt = max(months, key=lambda x: ((x[1] or 0), x[0]))
            base = min(months, key=lambda x: ((x[1] or 0), x[0]))
            spec["period"] = {"kind": "month", "year": tgt[1], "m1": tgt[0], "m2": tgt[0]}
            spec["year"] = tgt[1]
            if base[1]:
                spec["base"] = (base[1], base[0])
        else:
            p = parse_period(text)
            if p:
                spec["period"] = p
                spec["year"] = p.get("year") or global_year
            elif global_year:
                spec["period"] = {"kind": "month", "year": global_year, "m1": None, "m2": None}
            else:
                # «х»-колонка или пустая
                pass
    return spec


def _rel_value_period(cols):
    """Индекс-таблицы «Март 2008 | Март 2008 в % к марту 2007»: у колонки % месяц/период
    значения берётся из соответствующей abs-колонки (то же сочетание месяцев),
    а её месяц + год — базис."""
    abs_cols = [c for c in cols if c.get("kind") == "abs" and c.get("period")]
    rels = [c for c in cols
            if c.get("kind") in ("rel", "rel_yoy")
            and c.get("period")
            and re.search(r"в\s?%|процент", (c.get("heading") or "").lower())]
    if not rels or not abs_cols:
        return
    for n, c in enumerate(rels):
        if n >= len(abs_cols):
            break
        p = abs_cols[n]["period"]
        q = c["period"]
        base_yr, base_m = c.get("base") or (q.get("year"), q.get("m1"))
        same_months = (p.get("m1") == q.get("m1") and p.get("m2") == q.get("m2")
                       and p.get("kind") == q.get("kind"))
        if same_months and p.get("year") and p["year"] != q.get("year"):
            c["period"] = dict(p)
            c["year"] = p.get("year")
            c["base"] = (base_yr, base_m)


def parse_columns(header_rows):
    """header_rows: список строк-матриц (list of list of cell dicts)."""
    if not header_rows:
        return []
    # Ячейки, закрытые gridSpan-ведущим (span==0), наследуют его текст
    # (иначе теряется "В процентах к:" над колонкой справа).
    hrows = []
    for r in header_rows:
        out, last = [], None
        for c in r:
            if c.get("span", 1) == 1:
                last = c
            out.append(last if last is not None else c)
        hrows.append(out)
    ncols = max(len(r) for r in hrows)
    cols = []
    for ci in range(ncols):
        frags = []
        for row in hrows:
            if ci < len(row):
                t = collapse(row[ci].get("text", ""))
                if t and (not frags or t != frags[-1]):
                    frags.append(t)
        cols.append(build_col(frags))
    _rel_value_period(cols)
    return cols
