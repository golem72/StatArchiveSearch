# -*- coding: utf-8 -*-
"""Векторный переранкер финальной подборки (Ollama embedding).

Схема:
  1. Структурный скорер (answer._score) выдаёт отсортированный список.
  2. Берём верх K строк; для каждой собираем «текст строки»:
     показатель · подпись таблицы · шапка колонки · период · период сравнения ·
     единица · вид.
  3. Эмбеддимг вопроса и текстов строк (Ollama /api/embed, батчами),
     cos-сходство; score' = score + W * (cos - mean_cos_topK).
  4. Кэш эмбеддингов — SQLite rr_emb(heq, vec, dim): повторные вызовы
     (тесты) не ходят в Ollama.
  Любая ошибка → исходный порядок (фолбэк на структурный скорер).
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.request

import numpy as np

OLLAMA_URL = os.environ.get("RAG_OLLAMA_URL", "http://10.132.20.63:11434")
MODEL = os.environ.get("RAG_EMB_MODEL", "qllama/multilingual-e5-large:latest")
_BATCH = 16
_TIMEOUT = 30.0

_server_state = None  # None=unknown, True/False


def _server_up():
    global _server_state
    if _server_state is not None:
        return _server_state
    try:
        with urllib.request.urlopen(OLLAMA_URL + "/api/version", timeout=2.0) as r:
            _server_state = r.status == 200
    except Exception:
        _server_state = False
    return _server_state


def _embed_batch(texts: list[str]) -> list[list[float]]:
    from ragstat import olhttp as _oh
    data = _oh.http_json(OLLAMA_URL + "/api/embed",
                         {"model": MODEL, "input": texts}, _TIMEOUT)
    embs = data.get("embeddings") or data.get("embedding")
    if not embs:
        raise RuntimeError("no embeddings: %r" % str(data)[:200])
    return [list(e) for e in embs]


def embed_texts(conn, texts: list[str]) -> list:
    """Вектор (np.float32) на каждый текст; None там, где получить не удалось."""
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS rr_emb("
            "heq TEXT PRIMARY KEY, vec BLOB NOT NULL, dim INTEGER NOT NULL)")
    except Exception:
        return [None] * len(texts)
    out = [None] * len(texts)
    todo = []
    for i, t in enumerate(texts):
        h = hashlib.sha256((t or "").encode("utf-8")).hexdigest()
        row = conn.execute("SELECT vec, dim FROM rr_emb WHERE heq=?", (h,)).fetchone()
        if row:
            out[i] = np.frombuffer(row[0], dtype=np.float16).astype(np.float32)
        else:
            todo.append((i, h))
    for j in range(0, len(todo), _BATCH):
        chunk = todo[j:j + _BATCH]
        try:
            vecs = _embed_batch([(texts[i] or "").strip() or "." for (i, _h) in chunk])
        except Exception:
            break  # остальное оставим None, фолбэк сработает
        for (i, h), v in zip(chunk, vecs):
            arr = np.asarray(v, dtype=np.float32)
            out[i] = arr
            conn.execute("INSERT OR REPLACE INTO rr_emb(heq, vec, dim) VALUES (?,?,?)",
                         (h, arr.astype(np.float16).tobytes(), len(v)))
        conn.commit()
    return out


def _row_text(r: dict) -> str:
    def g(k):
        v = r.get(k)
        return str(v) if v not in (None, "") else ""
    parts = [g("indicator_raw") or g("indicator"),
             g("caption"), g("col_hint"),
             g("period_type"),
             ("%s-%s-%s" % (g("period_year"), g("period_m1"), g("period_m2"))
              if (g("period_year") or g("period_m1") or g("period_m2")) else ""),
             g("base_period"),
             g("unit_raw") or g("unit"),
             g("metric_kind")]
    return " · ".join(p for p in parts if p)


def rerank(question: str, rows: list, *, conn, w: float, k: int | None = None):
    """rows — отсортированные структурно (с полем score). Возвращает новый порядок.

    score' = score + w * (cos - mean_cos_topK). Ошибка → исходный порядок.
    """
    if w <= 0 or not rows:
        return rows
    if not _server_up():
        return rows
    k = len(rows) if k is None else min(k, len(rows))
    top = rows[:k]
    try:
        texts = ["query: " + question] + ["passage: " + _row_text(r) for r in top]
        vecs = embed_texts(conn, texts)
        qv, tvs = vecs[0], vecs[1:]
        if qv is None or any(v is None for v in tvs):
            return rows
        qn = np.linalg.norm(qv) or 1.0
        cos = [float(np.dot(qv, v / (np.linalg.norm(v) or 1.0)) / qn) for v in tvs]
        mean = sum(cos) / len(cos)
        for r, c in zip(top, cos):
            r["score"] = r.get("score", 0.0) + w * (c - mean)
        head = sorted(top, key=lambda r: -r["score"])
        return head + rows[k:]
    except Exception:
        return rows
