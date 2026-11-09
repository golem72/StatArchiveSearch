# -*- coding: utf-8 -*-
"""Расшифровка файла → (pubdate, логические таблицы, наблюдения)."""
import re

from .textnorm import collapse, extract_unit, parse_value, is_data_cell
from .periodnorm import parse_period
from .docxread import body_blocks, cell_matrix
from .grouper import group_logical_blocks
from .headerparse import parse_columns

SUB_LABELS = {"в том числе", "в т.ч.", "в t.ч.", "из них", "итого", "всего"}


def _cell_value(raw):
    """Число ячейки. Если чистое число нет, но это фраза «в N р.», вернуть (N, 'ratio').

    Возвращает (value, ratio_kind). ratio_kind в ('ratio', None)."""
    v = parse_value(raw)
    if v is not None:
        return v, None
    m = re.match(r"^[вв]\s*(\d{1,3}(?:[.,]\d+)?)\s*[ра]з?а?\s*\.?\s*$", collapse(raw).strip())
    if m:
        try:
            return float(m.group(1).replace(",", ".")), "ratio"
        except ValueError:
            return None, None
    return None, None

_MONTH_PREFIX = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6,
    "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11,
    "нояб": 11, "декабр": 12,
}


def find_pubdate(blocks):
    """Первая встреча 'месяц + год' в первых ~40 непустых абзацах."""
    cnt = 0
    for kind, payload in blocks:
        if kind != "p":
            continue
        t = collapse(payload or "")
        if not t:
            continue
        cnt += 1
        if cnt > 40:
            break
        low = t.lower()
        found = None
        for k, v in sorted(_MONTH_PREFIX.items(), key=lambda x: -len(x[0])):
            if low.startswith(k) or (" " + k) in low:
                found = v
                break
        if found:
            ym = re.search(r"\b(19|20)\d{2}\b", t)
            if ym:
                return int(ym.group(0)), found
    # год в первых абзацах, месяц не распознан
    cnt = 0
    for kind, payload in blocks:
        if kind != "p":
            continue
        t = collapse(payload or "")
        if not t:
            continue
        cnt += 1
        if cnt > 40:
            break
        if re.match(r"^\d{4}\b", t) or re.search(r"\b(19|20)\d{2}\b", t):
            ym = re.search(r"\b(19|20)\d{2}\b", t)
            return int(ym.group(0)), None
    return None, None


def _norm_indicator(t: str) -> str:
    s = collapse(t or "")
    s = re.sub(r"\s*\d+\)", "", s)
    s = re.sub(r"[«»]+", "", s)
    s = s.strip(" .,;:")
    return s.lower()


_COMPOSITE_MARK_RX = re.compile(r"^\s*(всего|итог\w*|про[шщ]и[её])\b", re.I)


def _col_meaningful(ch):
    """col_hint = настоящее имя показателя (не пустое/не число/не голое 'в %')."""
    s = collapse(ch or "").strip(" .,;:—-()")
    if len(s) < 8:
        return False
    low = s.lower()
    if re.fullmatch(r"[\d.%\s.,%]+", low):
        return False
    # гóлая относительная пометка без имени показателя — не имя
    if re.search(r"в\s*%|процентов|к\s+предыдущ|к\s+предшест|к\s+базисн", low) \
            and not re.search(r"числ|организац|задолж|расход|доля|челов|выруч|фонд|сальдо|остат", low):
        return False
    return True


def _synth_indicator(ind_full, label_low, col_hint):
    """Строка с составной меткой (Всего/Итого/Прочие) — дописать реальное имя
    показателя (col_hint), чтобы FTS искал её по настоящему показателю, не по 'Всего'.
    Чистые метки — без изменений."""
    if not label_low:
        return ind_full
    if _COMPOSITE_MARK_RX.match(label_low):
        if _col_meaningful(col_hint) and (col_hint or "").lower() not in (ind_full or "").lower():
            return f"{ind_full}: {collapse(col_hint).strip()}"
    return ind_full


def _lrow(row):
    """Логическая строка: только ведущие ячейки (span==1), без дублей gridSpan."""
    return [c for c in row if c.get("span", 1) == 1]


def _is_data_row(row):
    row = _lrow(row)
    if len(row) < 2:
        return False
    cells = [collapse(c.get("text", "")) for c in row[1:]]
    nonempty = [c for c in cells if c]
    if not nonempty:
        return False
    data_like = sum(1 for c in nonempty if is_data_cell(c))
    if is_data_cell(nonempty[0]):
        return True
    return data_like / len(nonempty) >= 0.5


def _row_label(row) -> str:
    return collapse(row[0].get("text", "")) if row else ""


def _parse_cells(row, cols, unit_fallback, row_pos):
    """Одна строка данных → list[obs]."""
    label = _row_label(row)
    if not label or label.lower() in ("окончание", "продолжение"):
        return None
    label_low = label.lower().strip(": ")
    is_sub = label_low in SUB_LABELS
    # parent передаётся через out-param в вызывающем коде (через closure)
    return label, is_sub


def _col_specs(row, cols):
    """Для каждой ведущей ячейки строки — spec заголовка её ПРАВОЙ ГРАНИЦЫ span-диапазона.
    Если сетка строки отличается от шапки (логическая таблица из raw-таблиц разной
    ширины), используем 1:1 порядок значимых колонок."""
    if not cols:
        return []
    cells = [c for c in row if c.get("span", 1) == 1]
    usable = [c for c in cols if c.get("period") or c.get("kind") == "rel"]
    if cells and usable and len(cells) - 1 == len(usable):
        return [cols[0]] + usable
    out, gi = [], 0
    for c in row:
        gs = c.get("gs") or (1 if c.get("span", 1) == 1 else 0)
        if c.get("span", 1) == 1:
            out.append(cols[min(gi + gs - 1, len(cols) - 1)])
        gi += gs
    return out


def extract_single_row(matrix, caption, units):
    """Однорядная таблица формата [label][период/число...]."""
    row = _lrow(matrix[0])
    label = _row_label(row)
    if not label:
        return []
    ind_norm = _norm_indicator(label)
    last_period = parse_period(label)
    rel_seen = False
    out = []
    unit_default = extract_unit(units) if units else None
    cap_unit = None
    if caption:
        m = re.search(r"[,;:]\s*([а-яёa-z0-9%. ]{2,40})\s*$", caption)
        if m:
            cap_unit = extract_unit(m.group(1))
    for ci, cell in enumerate(row[1:], start=1):
        raw = collapse(cell.get("text", ""))
        low = raw.lower()
        if not raw:
            continue
        if re.search(r"в\s?%|сравнени|к пред", low) and not is_data_cell(raw):
            rel_seen = True
            continue
        p = parse_period(raw)
        if p and not is_data_cell(raw):
            last_period = p
            continue
        value = parse_value(raw)
        if value is None:
            continue
        unit = extract_unit(raw) or unit_default or cap_unit
        out.append({
            "row_pos": 1, "col": ci,
            "indicator": label, "indicator_norm": ind_norm,
            "kind": "rel_yoy" if rel_seen else "abs",
            "ref": 0,
            "period": last_period, "base": None,
            "unit": unit, "value": value, "value_raw": raw,
        })
        rel_seen = False
    return out


def extract_file(path):
    import docx
    d = docx.Document(path)
    blocks = body_blocks(d)
    rawno = {}
    cnt = 0
    for b in blocks:
        if b[0] == "t":
            cnt += 1
            rawno[id(b[1])] = cnt
    pub_year, pub_month = find_pubdate(blocks)
    logicals = group_logical_blocks(blocks)
    out = []
    for seq, L in enumerate(logicals, 1):
        caption, units = L.get("caption"), L.get("units")
        unit_fallback = None
        if units:
            unit_fallback = extract_unit(units)
        if not unit_fallback and caption:
            m = re.search(r"[,;:]\s*([а-яёa-z0-9%. ]{2,40})\s*$", caption)
            if m:
                unit_fallback = extract_unit(m.group(1))

        matrices = [cell_matrix(el) for el in L["parts"]]
        first = matrices[0]
        if not first:
            continue
        if len(first) == 1:
            obs = extract_single_row(first, caption, units)
            out.append({"seq": seq, "caption": caption, "units": units,
                        "raw_seq": min((rawno.get(id(p)) for p in L["parts"] if rawno.get(id(p))),
                                   default=None),
                        "raw_last": max((rawno.get(id(p)) for p in L["parts"] if rawno.get(id(p))),
                                   default=None),
                        "obs": obs, "rows": 1})
            continue
        # шапка: ведущие строки без чисел
        hi = 0
        while hi < len(first) and not _is_data_row(first[hi]):
            hi += 1
            if hi > 6:
                break
        # ---- транспонированные таблицы: 1-я колонка пуста в шапке, статьи в
        # колонках 2..N (шапка «опознана» как данные, т.к. содержит годы) ----
        if hi == 0 and first:
            _fr = _lrow(first[0])
            if _fr and not collapse(_fr[0].get("text") or ""):
                _c = 0
                while _c < len(first) and _c <= 3:
                    _rr = _lrow(first[_c])
                    if not _rr or collapse(_rr[0].get("text") or ""):
                        break
                    if any((t := collapse(x.get("text") or "")) and not is_data_cell(t)
                           for x in _rr[1:]):
                        _c += 1
                    else:
                        break
                if _c >= 2:
                    hi = _c
        rows = first[hi:]
        for extra in matrices[1:]:
            if extra and not _is_data_row(extra[0]):
                extra = extra[1:]
            rows.extend(extra)
        obs = extract_rows_from_rows(rows, hi, unit_fallback, first, matrices, L)
        out.append({"seq": seq, "caption": caption, "units": units,
                    "raw_seq": min((rawno.get(id(p)) for p in L["parts"] if rawno.get(id(p))),
                                   default=None),
                    "raw_last": max((rawno.get(id(p)) for p in L["parts"] if rawno.get(id(p))),
                                   default=None),
                    "obs": obs, "rows": len(rows)})
    return {
        "file": path,
        "pub_year": pub_year, "pub_month": pub_month,
        "logical": out,
    }


def extract_rows_from_rows(rows, header_len, unit_fallback, first, matrices, L):
    # cols из заголовка первой части
    if header_len:
        cols = parse_columns(first[:header_len])
    else:
        # шапка внутри данных? попробуем распознать по колонкам 2..N
        cols = [{ "kind": "abs", "period": None, "base": None,
                  "unit": None, "ref": False} for _ in range(1)]
    return _extract_data_rows(rows, cols, unit_fallback)


def _extract_data_rows(data, cols, unit_fallback):
    parent = None
    obs = []
    row_pos = 0
    panel_year = None  # год из разделительных строк «2007 год»/«2008 год»
    for row in data:
        row_pos += 1
        label = _row_label(row)
        if not label or label.lower() in ("окончание", "продолжение"):
            continue
        # разделительная полоса панели задаёт год для последующих строк
        # (проверяем ДО пропуска строк без индикатора — у таких строк слов нет)
        _py = parse_period(label)
        if not (_py and _py.get("year")):
            import re as _re
            _ym = _re.search(r'(?<!\d)(19\d{2}|20\d{2})(?!\d)', label)
            if _ym and not _py and not _re.search(r'\d[.,]\d', label):
                _py = {"year": int(_ym.group(1))}
        if _py and _py.get("year") and not _py.get("m1") \
                and not any(parse_value(collapse(c.get("text") or "")) is not None
                            for c in row[1:] if isinstance(c, dict)):
            panel_year = _py["year"]
        label_low = label.lower().strip(": ")
        if label_low in SUB_LABELS:
            parent = parent or "—"
            ind_full = f"{parent} — {label}"
        else:
            parent = label
            ind_full = label
        ind_norm = _norm_indicator(ind_full)
        if not ind_norm:
            continue
        row_unit = extract_unit(label)
        row_period = parse_period(label)
        lrow_ = _lrow(row)
        specs_ = _col_specs(row, cols)
        # ---- транспонированные: метка строки — ПЕРИОД, а статья показателя —
        # в шапке колонки ("Доходы", "Расходы", "Профицит", ...) ----
        _is_transposed = bool(row_period)
        if _is_transposed:
            _sp = (specs_[1] or {}).get("period") if len(specs_) > 1 else None
            _hh = (specs_[1] or {}).get("heading") if len(specs_) > 1 else None
            _is_transposed = bool(_hh) and _hh.lower() not in ("х", "хх") \
                and parse_period(_hh) is None \
                and (not _sp or not _sp.get("m1"))
        for ci in range(1, min(len(lrow_), len(specs_))):
            spec = specs_[ci] or {}
            raw = collapse(lrow_[ci].get("text", ""))
            if not raw:
                continue
            value, _rk = _cell_value(raw)
            if value is None and raw not in ("-", "—", "х", "x", "...", "…"):
                continue
            ch_h = spec.get("heading")
            _tr_now = _is_transposed and bool(ch_h) and (parse_period(ch_h) is None
                         and (not spec.get("period") or not spec["period"].get("m1")))
            if _tr_now:
                # период берём из метки строки, ГОД — из панели/шапки колонки
                period = dict(row_period or {})
                _sy = spec.get("year") or ((spec.get("period") or {}).get("year"))
                if panel_year is not None:
                    _sy = panel_year
                if _sy and not period.get("year"):
                    period["year"] = _sy
            else:
                period = spec["period"] or row_period
            unit = spec["unit"] or row_unit or unit_fallback
            # ---- многоячеечное значение: пара (товар/услуга → своё значение) ----
            _nums = [parse_value(tok) for tok in
                     re.findall(r"\d+(?:[.,]\d+)?", raw)]
            if len(_nums) >= 2 and ci >= 1:
                _prevtxt = lrow_[ci - 1].get("text", "") or ""
                _names = [x.strip() for x in re.split(r"\n+", _prevtxt)
                          if x.strip() and not is_data_cell(x.strip())]
                if len(_names) >= 1:
                    _paired = 0
                    for _nm, _vv in zip(_names, _nums):
                        if _vv is None:
                            continue
                        _nin = _norm_indicator(_nm)
                        if not _nin:
                            continue
                        obs.append({
                            "row_pos": row_pos, "col": ci,
                            "indicator": _nm, "indicator_norm": _nin,
                            "kind": _rk or spec["kind"],
                            "ref": 1 if spec.get("ref") else 0,
                            "period": period, "base": spec.get("base"),
                            "unit": unit, "value": _vv, "value_raw": raw,
                            "col_hint": ((ch_h + " (товарной статьи)")
                                         if ch_h else "товарной статьи"),
                        })
                        _paired += 1
                    if _paired:
                        continue
            # --------------------------------------------------------------------
            ind_s = _synth_indicator(ind_full, label_low, ch_h)
            if _tr_now and ch_h:
                _ch = collapse(ch_h).strip(" .,;:")
                if _ch and _ch.lower() not in ind_s.lower():
                    ind_s = f"{_ch}: {ind_s}"
            ind_ns = ind_norm if ind_s is ind_full else _norm_indicator(ind_s)
            obs.append({
                "row_pos": row_pos, "col": ci,
                "indicator": ind_s, "indicator_norm": ind_ns,
                "kind": _rk or spec["kind"], "ref": 1 if spec.get("ref") else 0,
                "period": period, "base": spec.get("base"),
                "unit": unit, "value": value, "value_raw": raw,
                "col_hint": spec.get("heading"),
            })
    return obs
