# -*- coding: utf-8 -*-
"""Справочник показателей (dict): уникальные (имя, unit, kind) + E5-вектор имени.

Идея: имя показателя очищается от периодов (месяцы/кварталы/годы),
для «периодных» строк (лейбл = месяц/квартал) имя берётся из col_hint/подписи.
Каждая строка observations получает dict_id.
Запрос: embed вопроса → косинус по dict → top-K entries → фильтр по периоду.

Надёжно (без векторов) работает как «каталог имён»: dict_id — точный back-join.
"""
import re

import numpy as np

from . import embed as _emb
from .textnorm import UNIT_PATTERNS, collapse

_UNIT_RU = {c: lab for p, c, lab in UNIT_PATTERNS}

# «чистый период» как вся строка: месяц, квартал, полугодие, год, «месяц»,
# N месяцев, диапазон «Месяц-Месяц», «Месяц 20XX»
Months = ("январ[ья]|феврал[ья]|марта?|апреля?|ма[йй]а|июн[ья]"
          "|июл[ья]|августа?|сентябр[ья]|октябр[ья]|ноябр[ья]|декабр[ья]")
PURE_PERIOD_RE = re.compile(
    rf"^\s*(?:{Months}|[iv]{{1,3}}(?:\s+кв(?:арта?л)?)?|квартал[аы]?(?:\s+[iv]{{1,3}})?"
    rf"|полугод[ийе]?|перв(?:ой|ое)\s+|втор(?:ой|ое)\s+|месяц|\d+\s+месяц[ае]в?"
    rf"|за\s+год(овый)?|годов[ойае]?|{Months}\s*[-–]\s*{Months}"
    rf"|20\d{{2}}\b|{Months}\s*20\d{{2}}.*)\s*$", re.I)
_DASH_SPLIT = re.compile(r"\s+[·:;–—]\s+")
_DROP_RE = re.compile(rf""
    rf"\d{{1,2}}\s+месяц[ае]в?"
    rf"|{Months}"
    rf"|полугод[ийе]?(?:\s+20?\d{{2}})?"
    rf"|квартал[аы]?(?:\s+(?:20?\d{{2}}|[iv]{{1,3}}))?"
    rf"|перв(?:ой|ое)\s+(?:полугоди?е|квартал[аы]?)"
    rf"|втор(?:ой|ое)\s+(?:полугоди?е|квартал[аы]?)"
    rf"|\b[iv]{{1,3}}\b"
    rf"|\b(20)?\d{{2,4}}\s+г\."
    rf"|\b20\d{{2}}\b"
    rf"|месяц[ае]в?"
    rf"|за\s+год\b|годов[ойае]", re.I)


def _strip_periods(s: str) -> str:
    """Убирает периодные слова из имени, сохраняет метрическую суть."""
    s = s.replace("/", " ")
    s = _DASH_SPLIT.sub(" ", s)
    s = _DROP_RE.sub(" ", s)
    s = collapse(re.sub(r"\s{2,}", " ", s)).strip(" -–—:·")
    return s


def _noisy_label(s: str) -> bool:
    """Индикатор не несёт смысла: чистый период / «Всего» / «Итого» / только дефисы."""
    t = collapse((s or "").replace("-", "").replace("–", " ").replace("—", " ")).lower()
    t = t.strip()
    if not t:
        return True
    if PURE_PERIOD_RE.match(s or ""):
        return True
    if re.fullmatch(r"(всего|итого|total|общая численность|в т\.ч\.|в том числе)", t):
        return True
    return bool(re.fullmatch(r"[—–\-\.\sа-я]{0,4}", t) and not re.search(r"[а-я]", t))


def entry_name(indicator, col_hint=None, caption=None) -> str:
    """Имя записи справочника: имя показателя БЕЗ периода.

    Правило: 1) индикатор, если он не «шумовой» (чистый период/«Всего»);
    2) иначе col_hint (в периодных таблицах он = имя метрики);
    3) иначе подпись таблицы. Без конкатенаций-мусора.
    Периодные слова из имени убираются (период — поле отдельное).
    """
    ind = collapse(indicator or "")
    hint = collapse(col_hint or "")
    cap = collapse(caption or "")
    cands = []
    if ind and not _noisy_label(ind):
        cands.append(_strip_periods(ind))
    if hint:
        cands.append(_strip_periods(hint))
    if cap:
        cands.append(_strip_periods(cap))
    for c in cands:
        if c:
            return c[:200]
    return "показатель"


def entry_key(name, unit, kind) -> str:
    return "|".join([name.lower(), (unit or "").strip(), (kind or "abs").strip()])


def embedding_text(name, unit) -> str:
    ru = _UNIT_RU.get(unit or "", "")
    return (name + (" " + ru if ru else "")).strip()


def ensure_schema(conn) -> None:
    cols = [r[1] for r in conn.execute("PRAGMA table_info(observations)")]
    if "dict_id" not in cols:
        conn.execute("ALTER TABLE observations ADD COLUMN dict_id INTEGER")
    conn.execute("CREATE INDEX IF NOT EXISTS obs_dict ON observations(dict_id)")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS dict(
             dict_id INTEGER PRIMARY KEY,
             name    TEXT NOT NULL,
             unit    TEXT,
             kind    TEXT,
             n_rows  INTEGER,
             vec     BLOB)""")
    conn.commit()


def build(conn, verbose=True, use_vectors=True) -> int:
    """Собрать dict + назначить dict_id строкам. Возвращает число записей."""
    ensure_schema(conn)
    rows = conn.execute(
        """SELECT o.id, o.indicator, o.col_hint, o.unit, o.metric_kind, t.caption
           FROM observations o
           LEFT JOIN logical_tables t
             ON t.file_sha=o.file_sha AND t.seq=o.table_seq""").fetchall()
    if verbose:
        print(f"dictbuild: строк {len(rows)}")
    entries: dict[str, dict] = {}
    for oid, ind, col_hint, unit, kind, caption in rows:
        name = entry_name(ind, col_hint, caption)
        key = entry_key(name, unit, kind)
        e = entries.get(key)
        if e is None:
            e = entries[key] = {"name": name, "unit": unit, "kind": kind, "ids": []}
        e["ids"].append(oid)
    if verbose:
        uniq = {embedding_text(e["name"], e["unit"]) for e in entries.values()}
        print(f"dictbuild: записей {len(entries)}, уникальных текстов {len(uniq)}")

    conn.execute("DELETE FROM dict")
    if use_vectors:
        uniq_texts = sorted({embedding_text(e["name"], e["unit"]) for e in entries.values()})
        if verbose:
            print(f"dictbuild: векторизую {len(uniq_texts)} (E5)...")
        mats = _emb.embed_passages(uniq_texts)
        vecmap = {t: mats[i] for i, t in enumerate(uniq_texts)}
    else:
        vecmap = {}

    seq = 1
    for key, e in sorted(entries.items()):
        vec = vecmap.get(embedding_text(e["name"], e["unit"]))
        conn.execute(
            "INSERT INTO dict(dict_id, name, unit, kind, n_rows, vec) "
            "VALUES(?,?,?,?,?,?)",
            (seq, e["name"], e["unit"], e["kind"], len(e["ids"]),
             (vec.astype(np.float16).tobytes() if vec is not None else None)))
        for oid in e["ids"]:
            conn.execute("UPDATE observations SET dict_id=? WHERE id=?",
                         (seq, oid))
        seq += 1
    conn.commit()
    if verbose:
        print(f"dictbuild: готово, {seq-1} записей справочника")
    return seq - 1


def load(conn):
    """(ids, names, units, kinds, mat Nx1024 float32) — для косинус-поиска."""
    rows = conn.execute(
        "SELECT dict_id, name, unit, kind, n_rows, vec FROM dict").fetchall()
    if not rows:
        return [], [], [], [], None
    ids, names, units, kinds, vecs = [], [], [], [], []
    for did, name, unit, kind, n, vec in rows:
        if vec is None:
            continue
        ids.append(did)
        names.append(name)
        units.append(unit)
        kinds.append(kind)
        vecs.append(np.frombuffer(vec, dtype=np.float16).astype(np.float32))
    if not vecs:
        return [], [], [], [], None
    mat = np.vstack(vecs)
    mat /= (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9)
    return ids, names, units, kinds, mat


def search_topk(conn, question, k=8):
    """Векторный топ-k записей справочника по вопросу. Грейсфуло: [] при сбое."""
    ids, names, units, kinds, mat = load(conn)
    if mat is None:
        return []
    try:
        qv = _emb.embed_query(question)
    except Exception:
        return []
    scores = mat @ qv
    idx = np.argpartition(-scores, k)[:k]
    idx = idx[np.argsort(-scores[idx])]
    out = []
    for i in idx:
        out.append({"dict_id": int(ids[i]), "name": names[i], "unit": units[i],
                    "kind": kinds[i], "score": float(scores[i])})
    return out
