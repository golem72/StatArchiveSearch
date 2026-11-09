#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ragstat — CLI: index | ask | stats | debug."""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB_DEFAULT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "ragstat.db")


def cmd_index(args):
    from ragstat import indexer
    st = indexer.incremental_index(args.root, args.db, limit=args.limit)
    print(json.dumps(st, ensure_ascii=False, indent=2))


def cmd_ask(args):
    from ragstat import db as dbmod
    from ragstat import answer as answer_mod
    conn = dbmod.connect(args.db)
    res = answer_mod.top_answer(conn, args.question)
    if not res:
        print("— ничего не найдено —")
        return
    print(f"Ответ: {res['answer']}")
    print(f"  показание: {res['indicator']}")
    print(f"  период:    {res['period']}")
    print(f"  kind:      {res['kind']}")
    print(f"  таблица:   {res['table']}")
    print(f"  файл:      {res['file']}")
    print("  остальные кандидаты:")
    for t in res["top"][1:]:
        print(f"    [{t['score']}] {t['answer']} — {t['indicator'][:60]} ({t['file']})")
    conn.close()


def cmd_stats(args):
    from ragstat import db as dbmod
    conn = dbmod.connect(args.db)
    n_files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    n_tabs = conn.execute("SELECT COUNT(*) FROM logical_tables").fetchone()[0]
    n_obs = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    n_val = conn.execute("SELECT COUNT(*) FROM observations WHERE value IS NOT NULL"
                          ).fetchone()[0]
    print(f"файлов: {n_files}, лог. таблиц: {n_tabs}, наблюдений: {n_obs} "
          f"(с ценностью: {n_val})")
    conn.close()


def cmd_debug(args):
    """Вытащить из БД все наблюдения по строке-показателю (для дебага)."""
    from ragstat import db as dbmod
    conn = dbmod.connect(args.db)
    rows = conn.execute(
        "SELECT o.indicator, o.metric_kind, o.period_year, o.period_m1, o.period_m2, "
        "o.value, o.unit, f.path FROM observations o "
        "JOIN files f ON f.sha256=o.file_sha WHERE o.indicator LIKE ? "
        "ORDER BY o.period_year, o.row_pos LIMIT 60",
        (f"%{args.indicator}%",)).fetchall()
    for r in rows:
        print(f"{r[1]:8} {r[2]}/{r[3]:>2}-{r[4]:>2}  {str(r[5]):>12}  {r[6]}  "
              f"{r[7][:40]}  {r[0][:60]}")
    conn.close()


def cmd_dict(args):
    from ragstat import db as dbmod, dictbuild
    conn = dbmod.connect(args.db)
    dictbuild.build(conn, use_vectors=not args.no_vec)
    if args.topq:
        tops = dictbuild.search_topk(conn, args.topq, 8)
        for t in tops:
            print(f"  {t['score']:.4f}  {t['name'][:70]}  (unit={t['unit']}, kind={t['kind']})")
    conn.close()


def cmd_dictrow(args):
    from ragstat import db as dbmod, dictrow
    conn = dbmod.connect(args.db)
    if args.topq:
        res = dictrow.search_topk(conn, args.topq, args.topk)
        import sqlite3 as _s3
        conn.row_factory = _s3.Row
        for oid, sc in res:
            rr = conn.execute("SELECT o.value, o.indicator_raw, o.unit, o.period_year, o.period_m1, o.period_m2, o.col_hint FROM observations o WHERE o.id=?", (oid,)).fetchone()
            if rr:
                print(f"  {sc:.4f}  {str(rr['ind_raw'] if 'ind_raw' in rr else rr['indicator_raw'])[:46]}  v={rr['value']} unit={rr['unit']} y={rr['period_year']} m={rr['period_m1']}-{rr['period_m2']} col={str(rr['col_hint'])[:30]}")
        conn.close()
        return
    dictrow.build(conn)
    conn.close()


def main():
    ap = argparse.ArgumentParser(prog="ragstat")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("index", help="инкрементальная индексация docx-дерева")
    p.add_argument("--root", default=os.getcwd())
    p.add_argument("--db", default=DB_DEFAULT)
    p.add_argument("--limit", type=int, default=None)
    p.set_defaults(fn=cmd_index)
    p = sub.add_parser("ask", help="спросить вопрос по данным")
    p.add_argument("question")
    p.add_argument("--db", default=DB_DEFAULT)
    p.set_defaults(fn=cmd_ask)
    p = sub.add_parser("stats", help="статистика индекса")
    p.add_argument("--db", default=DB_DEFAULT)
    p.set_defaults(fn=cmd_stats)
    p = sub.add_parser("debug", help="поиск наблюдений по строке-показателю")
    p.add_argument("indicator")
    p.add_argument("--db", default=DB_DEFAULT)
    p.set_defaults(fn=cmd_debug)
    p = sub.add_parser("dict", help="справочник показателей (имя+unit+kind) + E5-векторы")
    p.add_argument("--db", default=DB_DEFAULT)
    p.add_argument("--no-vec", action="store_true", help="без векторизации (только таблица+dict_id)")
    p.add_argument("--topq", default=None, help="запрос: показать топ-8 записей справочника")
    p.set_defaults(fn=cmd_dict)
    p = sub.add_parser("dictrow", help="dict v3: row-level E5-векторы строк наблюдений")
    p.add_argument("--db", default=DB_DEFAULT)
    p.add_argument("--topq", default=None, help="запрос: показать топ-K строк")
    p.add_argument("--topk", type=int, default=10)
    p.set_defaults(fn=cmd_dictrow)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
