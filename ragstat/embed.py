"""Embedding-матчинг через Ollama (multilingual-e5-large).
Текст строки = indicator_raw/'sign' + ' ' + caption(250). Скоринг в answer.py.
Грейсфуло: сервер недоступен → вклад 0.0.
"""
import hashlib
import os

import numpy as np
import requests

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://10.132.20.63:11434")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "qllama/multilingual-e5-large:latest")


def row_text(indicator, caption):
    t = (indicator or "").strip()[:120]
    c = (caption or "").strip()[:250]
    return (t + " " + c).strip()


def _heq(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _embed(texts, prefix, timeout=180):
    r = requests.post(OLLAMA_URL + "/api/embed",
                      json={"model": EMBED_MODEL, "input": [prefix + x for x in texts]},
                      timeout=timeout)
    r.raise_for_status()
    return np.array(r.json()["embeddings"], dtype=np.float32)


def embed_passages(texts, batch=64):
    out = []
    for i in range(0, len(texts), batch):
        a = _embed(texts[i:i + batch], "passage: ")
        a /= (np.linalg.norm(a, axis=1, keepdims=True) + 1e-9)
        out.append(a)
    return np.vstack(out)


def embed_query(text):
    a = _embed([text], "query: ")[0]
    return a / (np.linalg.norm(a) + 1e-9)


def _ensure_table(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS emb_text(heq TEXT PRIMARY KEY, vec BLOB)")


def build(conn, verbose=True):
    """Embed distinct row texts → таблица emb_text."""
    _ensure_table(conn)
    rows = conn.execute(
        """SELECT DISTINCT COALESCE(NULLIF(o.indicator_raw, ''), NULLIF(o.indicator, ''), 'построчно') AS ind,
               COALESCE(t.caption, '') AS cap
           FROM observations o
           JOIN files f ON f.sha256 = o.file_sha
           LEFT JOIN logical_tables t ON t.file_sha = o.file_sha AND t.seq = o.table_seq"""
    ).fetchall()
    texts = []
    seen = set()
    for (ind, cap) in rows:
        t = row_text(ind, cap)
        if t not in seen:
            seen.add(t)
            texts.append(t)
    if verbose:
        print(f"embed: {len(texts)} distinct текстов")
    mats = embed_passages(texts)
    _ensure_table(conn)
    conn.execute("DELETE FROM emb_text")
    for t, m in zip(texts, mats):
        conn.execute("INSERT OR REPLACE INTO emb_text(heq, vec) VALUES (?, ?)",
                     (_heq(t), m.astype(np.float16).tobytes()))
    conn.commit()
    if verbose:
        print("embed: готово,", len(texts), "векторов")
    return len(texts)
