# -*- coding: utf-8 -*-
"""obs-row семантический канал: E5-векторы строк наблюдений.

Схема:
  obsrow(oid INTEGER PRIMARY KEY, vec BLOB)   — (rowid observations, 1024×f16)
Кэш эмбеддингов — общая таблица emb_text(heq, vec) (float16).

build(conn)  — оффлайн проход: эмбедим тексты всех строк (кэширование).
toprows()    — запрос: косинус вопрос→строки файла, top-k строка-сущности.
"""
from __future__ import annotations

import hashlib

import numpy as np

from . import embed


def _heq(t: str) -> str:
    return hashlib.sha256(t.encode("utf-8")).hexdigest()


def _ensure(conn) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS obsrow("
                 "oid INTEGER PRIMARY KEY, vec BLOB NOT NULL)")


def build(conn, verbose=True) -> int:
    _ensure(conn)
    embed._ensure_table(conn)
    rows = conn.execute(
        "SELECT rowid, indicator_raw, caption FROM observations WHERE value IS NOT NULL"
    ).fetchall() if False else None
    # caption живёт в logical_tables; join по file_sha+table_seq
    rows = conn.execute("""
        SELECT o.rowid AS oid, o.indicator_raw, lt.caption
        FROM observations o
        JOIN files f ON f.sha256 = o.file_sha
        LEFT JOIN logical_tables lt ON lt.file_sha = o.file_sha AND lt.seq = o.table_seq
        WHERE o.value IS NOT NULL
    """).fetchall()
    have = {r[0] for r in conn.execute("SELECT oid FROM obsrow")}
    texts = {}
    for oid, ind, cap in rows:
        if oid in have:
            continue
        t = embed.row_text(ind or "", cap or "")
        texts[oid] = t
    if verbose:
        print("obsrow: total=%d missing=%d" % (len(rows), len(texts)), flush=True)
    # кэш текстовых эмбеддингов
    vecs = {}
    missing_texts = set(texts.values())
    if missing_texts:
        crows = conn.execute("SELECT heq FROM emb_text").fetchall()
        cached = {h for (h,) in crows}
        todo = sorted(missing_texts)
        todo = [t for t in todo if _heq(t) not in cached]
        if verbose:
            print("emb: unique_missing_texts=%d todo=%d" % (len(missing_texts), len(todo)), flush=True)
        for i in range(0, len(todo), 64):
            batch = todo[i:i + 64]
            a = embed._embed(batch, "passage: ")
            a = a / (np.linalg.norm(a, axis=1, keepdims=True) + 1e-9)
            for t, v in zip(batch, a):
                conn.execute("INSERT OR REPLACE INTO emb_text(heq, vec) VALUES (?,?)",
                             (_heq(t), v.astype(np.float16).tobytes()))
                vecs[t] = v
            conn.commit()
            if verbose and (i // 64) % 10 == 0:
                print("emb: %d/%d" % (i + len(batch), len(todo)), flush=True)
    for oid, t in texts.items():
        if t not in vecs:
            r = conn.execute("SELECT vec FROM emb_text WHERE heq=?", (_heq(t),)).fetchone()
            if r is None:
                continue
            vecs[t] = np.frombuffer(r[0], dtype=np.float16).astype(np.float32)
            vecs[t] /= (np.linalg.norm(vecs[t]) + 1e-9)
        v = vecs.get(t)
        if v is None:
            continue
        conn.execute("INSERT OR REPLACE INTO obsrow(oid, vec) VALUES (?,?)",
                     (oid, vecs[t].astype(np.float16).tobytes()))
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM obsrow").fetchone()[0]
    if verbose:
        print("obsrow built: rows=%d" % n, flush=True)
    return n


def toprows(conn, question: str, file_scope: str, topk: int = 25):
    """Top-k строки файла по косинусу эмбеддингов (полные сущности)."""
    import sqlite3 as _s3
    conn.row_factory = _s3.Row
    sha = conn.execute("SELECT sha256 FROM files WHERE path=?", (file_scope,)).fetchone()
    if sha is None:
        return []
    data = conn.execute("""
        SELECT o.rowid AS oid, orow.vec FROM obsrow orow
        JOIN observations o ON o.rowid = orow.oid
        WHERE o.file_sha = ? AND o.value IS NOT NULL
    """, (sha[0],)).fetchall()
    if not data:
        return []
    qv = embed.embed_query(question)
    qv = qv / (np.linalg.norm(qv) + 1e-9)
    scored = []
    for oid, vecb in data:
        v = np.frombuffer(vecb, dtype=np.float16).astype(np.float32)
        v /= (np.linalg.norm(v) + 1e-9)
        scored.append((float(np.dot(qv, v)), oid))
    scored.sort(key=lambda x: -x[0])
    oids = [str(o) for _, o in scored[:topk]]
    if not oids:
        return []
    ph = ",".join("?" * len(oids))
    return [dict(r) for r in conn.execute(
        """SELECT o.*, f.path AS fpath, lt.caption FROM observations o
           JOIN files f ON f.sha256 = o.file_sha
           LEFT JOIN logical_tables lt ON lt.file_sha=o.file_sha AND lt.seq=o.table_seq
           WHERE o.rowid IN (%s)""" % ph, tuple(oids)).fetchall()]
