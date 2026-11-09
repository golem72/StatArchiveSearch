# -*- coding: utf-8 -*-
"""Логическая группировка таблиц: «Окончание/Продолжение» → одна логическая
таблица; подпись (caption) и строка единиц (units) — параграфы перед таблицей."""
import re

from .textnorm import extract_unit, collapse

CONT_RE = re.compile(r"^\s*(Окончание|Продолж[eн]иe)", re.I)


def is_continuation_marker(text: str) -> bool:
    return bool(CONT_RE.match(collapse(text) or ""))


def is_gap_paragraph(text: str) -> bool:
    """Параграф, который может стоять между частями логической таблицы."""
    t = collapse(text or "")
    if not t:
        return True
    if is_continuation_marker(t):
        return True
    if len(t) <= 70 and (extract_unit(t) or
            re.search(r"конец|к пред|в\s?%$|на \d{1,2}\s+\w+ \d{4}", t, re.I)):
        return True
    return False


def group_logical_blocks(blocks):
    """blocks: [('p', text) | ('t', tbl_el), ...] в порядке документа.

    Возвращает список: {'caption': str|None, 'units': str|None, 'parts': [tbl_el...]}

    Правило: таблица открывает новую логическую, если до неё (после предыдущей
    таблицы) был «обычный» параграф. Между частями одной логической таблицы
    допускаются только маркеры «Окончание/Продолжение» и строки единиц."""
    n = len(blocks)
    logical = []
    i = 0
    while i < n:
        kind = blocks[i][0]
        if kind == "p":
            i += 1
            continue
        # начало региона с блоком i (таблица)
        j = i + 1
        while j < n:
            b = blocks[j]
            if b[0] == "t":
                j += 1
                continue
            if is_gap_paragraph(b[1]):
                j += 1
                continue
            break
        region = [blocks[k][1] for k in range(i, j) if blocks[k][0] == "t"]
        if not region:
            i = j
            continue
        # подпись/единицы: до 2 последовательных непустых параграфов сразу перед i
        pre = []
        idx = i - 1
        while idx >= 0 and len(pre) < 2 and blocks[idx][0] == "p":
            pre.append(collapse(blocks[idx][1] or ""))
            idx -= 1
        pre.reverse()
        pre = [t for t in pre if t and not is_continuation_marker(t)]
        caption = units = None
        if len(pre) == 1:
            t = pre[0]
            # строка единиц или подпись — решаем эвристикой
            if extract_unit(t) and not re.search(r"[А-Яа-яа-яё]{6,}\s+[А-яё]{6,}", t):
                units = t
            else:
                caption = t
        elif len(pre) >= 2:
            caption = pre[0]
            units = pre[1]
        logical.append({"caption": caption, "units": units, "parts": region})
        i = j
    return logical


def extract_caption_units(caption, units):
    """Разложить caption+units на (caption', units'); единица может быть
    внутри подписи ('... , человек', '... в %')."""
    unit = extract_unit(units) if units else None
    if not unit and caption:
        m = re.search(r"[,;:]\s*([а-яёa-z0-9%. ]{2,40})\s*$", caption or "")
        if m:
            u = extract_unit(m.group(1))
            if u:
                unit = u
                caption = (caption or "")[:m.start()].strip()
    return caption, (units or (unit[1] if unit else None)) or None, unit
