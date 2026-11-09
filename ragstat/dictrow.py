# -*- coding: utf-8 -*-
"""Dict v3 — row-level семантический индекс строк наблюдений.

Почему отдельный канал: записи справочника (dict v1) векторизуют КОРотКОЕ имя
показателя («Пиво»), а вопрос длинный и конкретный → косинус деградует
(rank 500-3000). Строка же = полный текст (показатель+подпись+кол-ка+период+
единица+вид) — семантически близка к вопросу.

Схема:
  dictrow(oid INTEGER PRIMARY KEY, vec BLOB)   — E5-вектор (1024×f16) строки
  dr_emb (heq TEXT PRIMARY KEY, vec BLOB)      — кэш текстов (инкремент)

Поиск: embed_query(вопрос) → косинус по всем строкам файла → top-K oids.
"""
from __future__ import annotations

import hashlib

import numpy as np

from . import embed as _emb

_KIND_RU = {"abs": "базовое значение", "rel": "индекс к сравниваемому периоду",
            "rel_pm": "к предыдущему месяцу", "rel_yoy": "к аналогичному периоду прошлого года",
            "ratio": "коэффициент кратности"}
_UNIT_RU = {"pct": "%", "mln_rub": "млн рублей", "rub": "рубли", "t": "тонны",
            "ch": "человек", "th": "тысяча", "mln": "миллион", "th_ch": "тысяча человек",
            "m2": "квадратных метров", "th_m2": "тысяча квадратных метров",
            "kopek": "копейки", "th_rub": "тысяча рублей", "units": "штуки/единицы",
            "pcs": "штуки", "m": "метры"}


def ensure_schema(conn) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS dictrow("
                 "oid INTEGER PRIMARY KEY, vec BLOB NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS dr_emb("
                 "heq TEXT PRIMARY KEY, vec BLOB NOT NULL)")
    conn.commit()


def row_text(r: dict) -> str:
    ind = (r.get("indicator_raw") or r.get("indicator") or "").strip()
    cap = (r.get("caption") or "").strip()
    hint = (r.get("col_hint") or "").strip()
    unit = _UNIT_RU.get(r.get("unit") or "", r.get("unit") or "")
    kind = _KIND_RU.get(r.get("metric_kind") or "", "")
    py, m1, m2, pt = r.get("period_year"), r.get("period_m1"), r.get("period_m2"), r.get("period_type")
    per = ""
    if py:
        per = str(py)
        if m1 or m2:
            per += " г., " + str(m1) + ("-" + str(m2) if m2 and m2 != m1 else "") + " мес."
        if pt:
            per += " " + str(pt)
    base = r.get("base_period") or ""
    parts = [ind, cap[:180], hint[:60], per, base and ("сравнивается с " + base) or "",
             kind, ("единица " + unit) if unit else ""]
    return " · ".join(p for p in parts if p).strip()


def _heq(t: str) -> str:
    return hashlib.sha256(t.encode("utf-8")).hexdigest()


def build(conn, verbose=True, batch: int = 64) -> int:
    """Эмбеддинги всех строк наблюдений c value. Вектор на строку (dedup по тексту)."""
    import sqlite3 as _s3
    conn.row_factory = _s3.Row
    ensure_schema(conn)
    rows = conn.execute(
        """SELECT o.id, o.indicator, o.indicator_raw, o.col_hint, o.unit, o.metric_kind,
                  o.period_type, o.period_year, o.period_m1, o.period_m2, o.base_period,
                  t.caption
           FROM observations o
           JOIN files f ON f.sha256 = o.file_sha
           LEFT JOIN logical_tables t ON t.file_sha=o.file_sha AND t.seq=o.table_seq
           WHERE o.value IS NOT NULL""").fetchall()
    texts = {}
    for r in rows:
        t = row_text(dict(r))
        if t and t not in texts:
            texts[t] = None
    if verbose:
        print(f"dictrow: строк {len(rows)}, уникальных текстов {len(texts)}")
    # из кэша
    vecs = {}
    todo = []
    for t in texts:
        h = _heq(t)
        row = conn.execute("SELECT vec FROM dr_emb WHERE heq=?", (h,)).fetchone()
        if row:
            vecs[t] = np.frombuffer(row[0], dtype=np.float16).astype(np.float32)
        else:
            todo.append(t)
    if verbose and todo:
        print(f"dictrow: из кэша {len(texts)-len(todo)}, в Оллуму {len(todo)}")
    todo.sort()
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        try:
            mats = _emb.embed_passages(chunk)
        except Exception as e:
            if verbose:
                print(f"dictrow: Оллума недоступна (пакет {i//batch}): {e}; "
                      f"остальные строки получат векторы следующего раза")
            break
        for t, m in zip(chunk, mats):
            a = np.asarray(m, dtype=np.float32)
            vecs[t] = a
            conn.execute("INSERT OR REPLACE INTO dr_emb(heq, vec) VALUES (?,?)",
                         (_heq(t), a.astype(np.float16).tobytes()))
        conn.commit()
    # пишем dictrow
    n = 0
    for r in rows:
        t = row_text(dict(r))
        v = vecs.get(t)
        if v is None:
            continue
        conn.execute("INSERT OR REPLACE INTO dictrow(oid, vec) VALUES (?,?)",
                     (r["id"], v.astype(np.float16).tobytes()))
        n += 1
    conn.commit()
    if verbose:
        print(f"dictrow: готово, {n} векторов строк")
    return n


def load(conn):
    """(oids, mat Nx1024 float32 нормирование). Пусто → ([], None)."""
    conn.row_factory = None
    rows = conn.execute("SELECT oid, vec FROM dictrow WHERE vec IS NOT NULL").fetchall()
    if not rows:
        return [], None
    oids = [int(r[0]) for r in rows]
    mat = np.vstack([np.frombuffer(r[1], dtype=np.uint8).copy()
                     for r in rows]).view(np.float16).astype(np.float32)
    mat /= (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9)
    return oids, mat


def search_topk(conn, question, k=60):
    """Топ-k строк (oid, score) по косинусу. [] при сбое/пустоте."""
    oids, mat = load(conn)
    if mat is None:
        return []
    try:
        vq = _emb.embed_query(question)
    except Exception:
        return []
    sc = mat @ np.asarray(vq, dtype=np.float32)
    kk = min(k, len(oids))
    idx = np.argpartition(-sc, kk - 1)[:kk]
    idx = idx[np.argsort(-sc[idx])]
    return [(int(oids[i]), float(sc[i])) for i in idx]
