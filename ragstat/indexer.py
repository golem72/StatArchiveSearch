# -*- coding: utf-8 -*-
"""Инкрементальная индексация: walk → diff по sha256 → per-file парсинг."""
import hashlib
import os

from .extract import extract_file
from . import db as dbmod


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def walk_docx(root):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in sorted(filenames):
            if name.lower().endswith((".docx",)) and not name.startswith("~$"):
                p = os.path.join(dirpath, name)
                rel = os.path.relpath(p, root)
                st = os.stat(p)
                out.append((rel, p, st.st_size, st.st_mtime))
    return out


def diff(found, indexed):
    """found: {relpath: (path, size, mtime, sha)}; indexed: {relpath_or_sha...}.

    Упрощение: сопоставляем по sha; файл удалён = sha не найден в found.
    Возвращает (to_add, to_update, to_remove) — множества (rel, sha,...).
    """
    indexed_sha = {row[0]: row for row in indexed.values()}  # sha -> (sha, path)
    found_sha = {v[3] for v in found.values()}
    to_add = [v for v in found.values() if v[3] not in indexed_sha]
    to_remove = [v for v in indexed_sha.values() if v[0] not in found_sha]
    return to_add, to_remove


def _detect_region(text):
    low = (text or "").lower()
    if "брянск" in low:
        return "Брянская область"
    if "орловск" in low:
        return "Орловская область"
    return None


def index_file(conn, root, rel, abs_path, size, mtime):
    sha = sha256_of(abs_path)
    conn.execute("BEGIN")
    try:
        res = extract_file(abs_path)
        pub_y, pub_m = res["pub_year"], res["pub_month"]
        region = _detect_region((res["logical"][0]["caption"] if res["logical"] else "") or "")
        dbmod.upsert_file(conn, sha256=sha, path=rel, size=size, mtime=mtime,
                          region=region, pub_month=pub_m, pub_year=pub_y)
        for L in res["logical"]:
            dbmod.insert_logical(conn, sha, L["seq"], L.get("caption"),
                                 units_hint=L.get("units"),
                                 raw_seq=L.get("raw_seq"), raw_last=L.get("raw_last"))
            for o in L.get("obs", []):
                o = dict(o)
                o["table_seq"] = L["seq"]
                dbmod.insert_observation(conn, sha, o)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return len(res["logical"]), sum(len(L.get("obs", [])) for L in res["logical"])


def incremental_index(root, dbpath, limit=None):
    conn = dbmod.connect(dbpath)
    found_rows = walk_docx(root)
    # хешируем только кандидатов сравнения: для diff нужен sha всех файлов
    # (хеширование 1МБ занимает <0.5с — для Pi приемлемо при diff по списку)
    n = len(found_rows)
    if limit:
        found_rows = found_rows[:limit]
    found = {rel: (rel, p, size, sha256_of(p)) for rel, p, size, mtime in found_rows}

    indexed = {}
    for sha, path, mtime in conn.execute("SELECT sha256, path, mtime FROM files"):
        indexed[sha] = (sha, path, mtime)

    to_add = [(rel, p, s, sha) for rel, p, s, sha in found.values()
              if sha not in {i[0] for i in indexed.values()}]
    to_remove = [(sha, path, mt) for sha, path, mt in indexed.values()
                 if sha not in {v[3] for v in found.values()}]

    stats = {"found": n, "added": 0, "removed": 0, "obs": 0, "tables": 0,
             "skipped": len(found) - len(to_add) - len([x for x in indexed.values()
                                                         if x[0] not in {t[3] for t in to_add}])}
    print(f"Найдено файлов: {n}, добавлять: {len(to_add)}, удалять: {len(to_remove)}")
    for sha, path, mt in to_remove:
        dbmod.delete_file_data(conn, sha)
        stats["removed"] += 1
        print(f"  - удалён {path}")
    for rel, p, s, sha in to_add:
        try:
            nt, no = index_file(conn, root, rel, p, s, s)
        except Exception as e:
            print(f"  ! ошибка {rel}: {e}")
            continue
        stats["added"] += 1
        stats["tables"] += nt
        stats["obs"] += no
        print(f"  + {rel}: {nt} таблиц, {no} наблюдений")
    conn.close()
    return stats
