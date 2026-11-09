# -*- coding: utf-8 -*-
"""Чтение docx: блоки тела в порядке документа + матрица ячеек таблицы
с разбором gridSpan / vMerge (на уровне OOXML)."""
from docx.oxml.ns import qn


def body_blocks(document):
    """Последовательность ('p', text) / ('t', tbl_element) в порядке документа."""
    out = []
    for child in document.element.body.iterchildren():
        tag = child.tag
        if tag == qn("w:p"):
            text = "".join(child.itertext())
            out.append(("p", text))
        elif tag == qn("w:tbl"):
            out.append(("t", child))
    return out


def _dedup_periodic(s: str) -> str:
    """'абабаб' → 'аб'; '106,8106,8106,8' → '106,8' (дублирующиеся вставки)."""
    L = len(s)
    if L < 4:
        return s
    for p in range(1, L // 2 + 1):
        if L % p == 0:
            rep = L // p
            if rep >= 2 and s[:p] * rep == s:
                return s[:p]
    return s


def _tc_text(tc):
    parts = []
    for p in tc.findall(qn("w:p")):  # только прямые абзацы ячейки
        t = "".join(node.text or "" for node in p.iter(qn("w:t")))
        if t:
            parts.append(t)
    txt = "\n".join(parts).strip()
    return _dedup_periodic(txt) if len(txt) >= 6 else txt


def _tc_props(tc):
    tcpr = tc.find(qn("w:tcPr"))
    span = 1
    vmerge = None
    if tcpr is not None:
        gs = tcpr.find(qn("w:gridSpan"))
        if gs is not None and gs.get(qn("w:val")):
            try:
                span = int(gs.get(qn("w:val")))
            except ValueError:
                span = 1
        vm = tcpr.find(qn("w:vMerge"))
        if vm is not None:
            vmerge = "cont" if not vm.get(qn("w:val")) else "restart"
    return span, vmerge


def cell_matrix(tbl_el):
    """Прямоугольная матрица строк×столбцов.

    cell = {'text': str, 'span': int, 'vmerge': 'restart'|'cont'|None}
    vMerge-континуум получает текст из ячейки выше (forward fill).
    """
    rows = []
    for tr in tbl_el.findall(qn("w:tr")):
        tcs = tr.findall(qn("w:tc"))
        cols = []
        col = 0
        for tc in tcs:
            span, vmerge = _tc_props(tc)
            text = _tc_text(tc)
            for k in range(span):
                cell = {"text": text, "span": 1 if k == 0 else 0,
                        "gs": span if k == 0 else 0,
                        "vmerge": vmerge if k == 0 else None}
                cols.append(cell)
            col += span
        rows.append(cols)
    n = max((len(r) for r in rows), default=0)
    # vMerge forward fill
    for r in rows:
        while len(r) < n:
            r.append({"text": "", "span": 0, "gs": 0, "vmerge": None})
    for ci in range(n):
        last = ""
        for row in rows:
            c = row[ci]
            if c["vmerge"] == "cont" or (c["vmerge"] == "restart" and not c["text"]):
                c["text"] = last
            elif c["text"]:
                last = c["text"]
            # restart с текстом обновляет last
    return rows


def matrix_text(m):
    """Матрица → строка 'r|c|text|...' (отладка, тесты)."""
    return "\n".join(" || ".join(c["text"].replace("\n", "·") for c in row) for row in m)
