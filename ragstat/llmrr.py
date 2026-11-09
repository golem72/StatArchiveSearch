# -*- coding: utf-8 -*-
"""LLM-реранкер: финальный выбор среди топ-K кандидатов.

27B-модель на Ollama получает вопрос и K вариантов
(значение, единица, показатель, период, вид) и возвращает номер лучшего (0 — ни один).
Сбой/тормоз → None → сохраняется порядок структурного скорера.

Кэш решений: llmrr_cache(heq, cset, choice).
v2 (2026-11): чищенный формат кандидата (единицы без шума «т» на коэффициентах,
явный «не указан» для периода), явные критерии выбора в промпте, период из вопроса.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.request

OLLAMA_URL = os.environ.get("RAG_OLLAMA_URL", "http://10.132.20.63:11434")
MODEL = os.environ.get("RAG_LLM_MODEL", "qwen3.8:27b-mtp-64k")
_TIMEOUT = 180.0

_KIND_RU = {
    "abs": "базовое значение",
    "rel": "индекс / отношение к сравниваемому периоду",
    "rel_pm": "значение помесячно (к предыдущему месяцу)",
    "rel_yoy": "значение к аналогичному периоду прошлого года",
    "ratio": "во сколько раз (коэффициент кратности)",
}
_UNIT_RU = {
    "pct": "%", "mln_rub": "млн руб", "rub": "руб", "t": "т", "ch": "чел",
    "th": "тыс", "mln": "млн", "ch_t": "чел", "th_ch": "тыс чел", "m2": "м2",
    "th_m2": "тыс м2", "kopek": "коп", "th_rub": "тыс руб", "units": "шт/ед",
    "pcs": "шт", "m": "м",
}
_MONTHS_RX = (r"(?:с (?:1|первым[им]?) по\s+)?(январ[ья]м|феврал[ья]м|март[?]|апрел[ья]м|ма[е?й]|"
              r"июн[ья]м|июл[ья]м|август[а]|сентябр[ья]м|октябр[ья]м|ноябр[ья]м|декабр[ья]м|"
              r"первом|втором|третьем|четвёртом|четвертом|I+V+ квартал)")


def _row_line(i: int, r: dict) -> str:
    def g(k):
        v = r.get(k)
        return str(v) if v not in (None, "") else ""
    val = r.get("value")
    kind = _KIND_RU.get(g("metric_kind"), g("metric_kind") or "—")
    if r.get("unit") in _UNIT_RU:
        unit = _UNIT_RU[r["unit"]]
    elif r.get("unit") and not re.fullmatch(r"[\d./\- ]+", str(r.get("unit"))):
        unit = str(r["unit"])
    else:
        unit = "не указана"
    pyear, p1, p2, ptype = g("period_year"), g("period_m1"), g("period_m2"), g("period_type")
    pp = []
    if p1 != "":
        pp.append("%s г., мес. %s%s" % (pyear or "?", p1,
                                       ("-%s" % p2) if (p2 and p2 not in (p1, "None")) else ""))
    elif pyear:
        pp.append("%s г." % pyear)
    if ptype:
        pp.append(ptype)
    period = " ".join(x.replace("None", "") for x in pp) if pp else "не указан (любой период)"
    base = ("; сравнивается с: " + str(r["base_period"])) if r.get("base_period") else ""
    colhint = str(r.get("col_hint") or "").strip()[:120]
    colhint = ("; колонка: " + colhint) if colhint else ""
    cap = (r.get("caption") or "").replace("\n", " ").strip()[:80]
    tab = ("; таблица: " + cap) if cap else ""
    ind = (g("indicator_raw") or g("indicator") or "—")[:200]
    return ("%d) значение: %s; единица: %s; показатель: %s; период: %s%s; вид: %s%s%s"
            % (i, "—" if val is None else val, unit, ind, period, base, kind, colhint, tab))


def _q_hint(question: str) -> str:
    """Подсказка: период/год из вопроса (для выбора среди близнецов-периодов)."""
    q = (question or "").lower()
    bits = []
    ym = re.search(r"\b((?:19|20)\d{2})", q)
    if ym:
        bits.append("Год в вопросе: " + ym.group(1))
    mm = re.search(_MONTHS_RX, q)
    if mm:
        bits.append("Месяц/квартал в вопросе: " + mm.group(1))
    return (" ".join(bits) + ".") if bits else ""


last_tier = "none"  # "none"/"seed"/"live"/"cache"/"llm" — последний источник пика (для answer.py: seed → пропуск guard-цепочки)


def choose(question: str, rows: list, *, conn, n: int | None = None,
           hint: str | None = None) -> int | None:
    """Возвращает индекс лучшего кандидата (1-based) или None (фолбэк)."""
    global last_tier
    last_tier = "none"
    if not rows:
        return None
    n = min(n or 5, len(rows))
    cand = rows[:n]
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS llmrr_cache("
                     "heq TEXT NOT NULL, cset TEXT NOT NULL, choice INTEGER NOT NULL, "
                     "PRIMARY KEY (heq, cset))")
        conn.execute("CREATE TABLE IF NOT EXISTS llmrr_pick("
                     "heq TEXT NOT NULL, rowid_o INTEGER NOT NULL, source TEXT, "
                     "PRIMARY KEY (heq, rowid_o))")
        _vp = (int(os.environ.get("RAG_LLM_VOTES", "1")))
        qh = hashlib.sha256(("q|" + question).encode("utf-8")).hexdigest()
        # TIER-0: id-пикы (не зависят от состава окна): seed > live (не мешаем)
        _allids = {r.get("id"): i for i, r in enumerate(rows)}
        _seed_rows = conn.execute("SELECT rowid_o FROM llmrr_pick WHERE heq=? AND source='seed'", (qh,)).fetchall()
        for (_po,) in _seed_rows:
            if _po in _allids:
                last_tier = "seed"
                return _allids[_po] + 1
        if not _seed_rows:
            for (_po,) in conn.execute("SELECT rowid_o FROM llmrr_pick WHERE heq=? AND source='live'", (qh,)).fetchall():
                if _po in _allids:
                    last_tier = "live"
                    return _allids[_po] + 1
        # order-independent набор id кандидатов (порядок в SQL без ORDER BY не гарантирован)
        _cids = sorted((r.get("id") for r in cand), key=lambda x: str(x))
        cset = hashlib.sha256(("k5|" + ",".join(str(x) for x in _cids)).encode("utf-8")).hexdigest()
        row = conn.execute("SELECT choice FROM llmrr_cache WHERE heq=? AND cset=?",
                           (qh, cset)).fetchone()
        if row is not None:
            _ch = int(row[0])
            if _ch < 0:
                return None
            # choice хранит id строки наблюдения (order-independent); маппим на позицию в cand
            _byid = {r.get("id"): i for i, r in enumerate(cand)}
            _pos = _byid.get(_ch)
            if _pos is None:
                return None
            last_tier = "cache"
            return _pos + 1
        hint = _q_hint(question)
        head = ("Ты эксперт по статистическим отчётам Росстата. Выбери ОДИН кандидат, который "
                "точнее всего отвечает на вопрос. Критерии (по убыванию важности):\n"
                "1) точное совпадение ПОКАЗАТЕЛЯ (строки таблицы) с формулировкой вопроса;\n"
                "2) совпадение ПЕРИОДА (год и месяц) с вопросом — если у кандидата период совпадает "
                "с вопросом, а у другого «не указан», выбирай совпадающий;\n"
                "3) совпадение ЕДИНИЦЫ ИЗМЕРЕНИЯ с вопросом;\n"
                "4) ВИД метрики (базовое значение / индекс / проценты / во сколько раз).\n"
                "5) ЗЕРНИСТОСТЬ СТОЛБЦА: в одной строке таблицы бывают два значения — «в среднем по "
                "группе» и конкретная позиция (столбец «максимальное/минимальное изменение…»). "
                "Вопрос о конкретной позиции (называется конкретный товар/услуга/вид) → значение "
                "конкретной позиции; вопрос о «среднем»/«по группе» → среднее по группе.\n"
                "Часто в таблице несколько строк с ОДИНАКОВЫМ показателем, но разным месяцем — "
                "значения у них разные; подбирай месяц из вопроса. "
                "Не выбирай строку с чужой статьёй (другой показатель), даже если значение близкое.\n"
                "ПРАВО ПЕРИОДА: если в вопросе назван КОНКРЕТНЫЙ месяц (январь, июль и т.п. — один "
                "месяц), а не квартал/полугодие — среди кандидатов выбирай строку, у которой период "
                "ровно этот месяц (например, «Январь»), а НЕ «Январь-март», «I квартал» и т.п., "
                "даже если у той строки точнее совпадает название показателя.\n")
        cand_txt = "\n".join(_row_line(i + 1, r) for i, r in enumerate(cand))
        hint_line = ("Ориентиры по вопросу (извлечены автоматически; при конфликте с "
                     "формулировкой вопроса доверяй формулировке): " + hint + ". "
                     "Выбирай кандидата, удовлетворяющий этим ориентирам по виду/периоду/базе; "
                     "если таких нет — выбирай по формулировке вопроса.\n" if hint else "")
        prompt = (head + (hint_line if hint else "")
                  + "Вопрос: %s\nКандидаты:\n%s\nОтветь ОДНИМ ЧИСЛОМ (номер кандидата) 1-%d. "
                  "Если ни один не подходит — 0.\n" % (question, cand_txt, n))
        _votes = int(os.environ.get("RAG_LLM_VOTES", "1"))
        _vtemp = float(os.environ.get("RAG_LLM_VTEMP", "0.7"))

        def _one_call(temp: float) -> int:
            payload = json.dumps({
                "model": MODEL, "stream": False,
                "think": False,  # think-токенy съедают 2–3x времени ответа (проверено на сервере)
                "options": {"temperature": temp},
                "messages": [{"role": "user", "content": prompt + "\n/no_think"}],
            }).encode("utf-8")
            from ragstat import olhttp as _oh
            data = _oh.http_json(OLLAMA_URL + "/api/chat", json.loads(payload), _TIMEOUT)
            msg = data.get("message", {})
            content = (msg.get("content") or "").strip()
            thinking = (msg.get("thinking") or "").strip()
            ch = 0
            if content:
                nums = [int(x) for x in re.findall(r"\d+", content)]
                nums = [x for x in nums if 1 <= x <= n]
                ch = nums[-1] if nums else 0
            else:
                nums = [int(x) for x in re.findall(r"\d+", thinking) if 1 <= int(x) <= n]
                ch = nums[-1] if nums else 0
            return ch

        votes = [_one_call(0.0 if _votes <= 1 else _vtemp) for _ in range(max(1, _votes))]
        # мажоритарное голосование; ничья → первый встреченный максимум
        best, best_n = votes[0], 1
        for v in votes[1:]:
            cnt = votes.count(v)
            if cnt > best_n:
                best, best_n = v, cnt
        choice = best
        if choice == 0:
            choice = -1  # → фолбэк
        # храним id строки наблюдения (order-independent), а не позицию
        _store: int = -1
        if choice >= 1:
            _rowid = cand[choice - 1].get("id")
            if _rowid is not None:
                _store = int(_rowid)
        conn.execute("INSERT OR REPLACE INTO llmrr_cache(heq, cset, choice) VALUES (?,?,?)",
                     (qh, cset, _store))
        if _store > 0:
            conn.execute("INSERT OR REPLACE INTO llmrr_pick(heq, rowid_o, source) VALUES (?,?,?)",
                         (qh, _store, "live"))
        conn.commit()
        last_tier = "llm"
        return None if choice < 0 else choice
    except Exception:
        return None
