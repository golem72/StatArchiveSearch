# -*- coding: utf-8 -*-
"""SQLite: схема + CRUD. FTS5 trigram поверх названий показателей."""
import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  sha256       TEXT PRIMARY KEY,
  path         TEXT NOT NULL,
  size         INTEGER,
  mtime        REAL,
  region       TEXT,
  pub_month    INTEGER,
  pub_year     INTEGER,
  indexed_at   TEXT
);
CREATE TABLE IF NOT EXISTS logical_tables (
  file_sha  TEXT REFERENCES files(sha256),
  seq       INTEGER,
  caption   TEXT,
  row_range TEXT,
  units_hint TEXT,
  raw_seq   INTEGER,
  raw_last  INTEGER,
  PRIMARY KEY (file_sha, seq)
);
CREATE TABLE IF NOT EXISTS observations (
  id            INTEGER PRIMARY KEY,
  file_sha    TEXT REFERENCES files(sha256),
  table_seq   INTEGER,
  row_pos     INTEGER,
  col         INTEGER,
  indicator   TEXT NOT NULL,
  indicator_raw TEXT,
  metric_kind TEXT NOT NULL,
  period_type TEXT,
  period_year INTEGER,
  period_m1   INTEGER,
  period_m2   INTEGER,
  base_period TEXT,
  is_ref      INTEGER DEFAULT 0,
  unit        TEXT,
  value       REAL,
  value_raw   TEXT,
  unit_raw    TEXT,
  col_hint    TEXT
);
CREATE INDEX IF NOT EXISTS obs_lookup ON observations(indicator, metric_kind, period_type, period_year);
CREATE INDEX IF NOT EXISTS obs_value  ON observations(value);
"""

FTS_SQL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS observations_fts USING fts5("
    "indicator, indicator_raw, content='observations', content_rowid='id', "
    "tokenize='trigram')"
)
TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS obs_ai AFTER INSERT ON observations BEGIN
  INSERT INTO observations_fts(rowid, indicator, indicator_raw)
  VALUES (new.id, new.indicator, new.indicator_raw);
END;
CREATE TRIGGER IF NOT EXISTS obs_ad AFTER DELETE ON observations BEGIN
  INSERT INTO observations_fts(observations_fts, rowid, indicator, indicator_raw)
  VALUES ('delete', old.id, old.indicator, old.indicator_raw);
END;
CREATE TRIGGER IF NOT EXISTS obs_au AFTER UPDATE ON observations BEGIN
  INSERT INTO observations_fts(observations_fts, rowid, indicator, indicator_raw)
  VALUES ('delete', old.id, old.indicator, old.indicator_raw);
  INSERT INTO observations_fts(rowid, indicator, indicator_raw)
  VALUES (new.id, new.indicator, new.indicator_raw);
END;
"""

COLS = ("file_sha", "table_seq", "row_pos", "col", "indicator", "indicator_raw",
        "metric_kind", "period_type", "period_year", "period_m1", "period_m2",
        "base_period", "is_ref", "unit", "value", "value_raw", "unit_raw",
        "col_hint")


def connect(dbpath):
    conn = sqlite3.connect(dbpath)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    try:
        conn.execute(FTS_SQL)
        conn.executescript(TRIGGERS)
    except sqlite3.OperationalError:
        pass  # trigram недоступен — поиск продолжит работать без FTS
    return conn


def _enc(v):
    if v is None:
        return None
    if isinstance(v, (list, tuple, dict)):
        return json.dumps(v, ensure_ascii=False)
    return v


def upsert_file(conn, *, sha256, path, size, mtime, region, pub_month, pub_year):
    conn.execute(
        """INSERT INTO files(sha256, path, size, mtime, region, pub_month, pub_year, indexed_at)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(sha256) DO UPDATE SET
             path=excluded.path, size=excluded.size, mtime=excluded.mtime,
             indexed_at=excluded.indexed_at""",
        (sha256, path, size, mtime, region, pub_month, pub_year,
         time.strftime("%Y-%m-%dT%H:%M:%S")),
    )


def delete_file_data(conn, sha256):
    conn.execute("DELETE FROM logical_tables WHERE file_sha=?", (sha256,))
    conn.execute("DELETE FROM observations WHERE file_sha=?", (sha256,))
    conn.execute("DELETE FROM files WHERE sha256=?", (sha256,))


def insert_logical(conn, file_sha, seq, caption, row_range=None, units_hint=None, raw_seq=None, raw_last=None):
    conn.execute(
        "INSERT OR REPLACE INTO logical_tables(file_sha, seq, caption, row_range, units_hint, raw_seq, raw_last) "
        "VALUES (?,?,?,?,?,?,?)",
        (file_sha, seq, caption, row_range, units_hint, raw_seq, raw_last),
    )


def insert_observation(conn, file_sha, o: dict):
    p = o.get("period") or {}
    base = o.get("base")
    unit = o.get("unit")
    row = {
        "file_sha": file_sha,
        "table_seq": o.get("table_seq"),
        "row_pos": o.get("row_pos"),
        "col": o.get("col"),
        "indicator": o.get("indicator"),
        "indicator_raw": o.get("indicator"),
        "metric_kind": o.get("kind", "abs"),
        "period_type": p.get("kind"),
        "period_year": p.get("year"),
        "period_m1": p.get("m1"),
        "period_m2": p.get("m2"),
        "base_period": (f"{base[0]}-{int(base[1]):02d}" if base and base[1]
                        else (f"{base[0]}-" if base else None)),
        "is_ref": 1 if o.get("ref") else 0,
        "unit": unit[0] if unit else None,
        "value": o.get("value"),
        "value_raw": o.get("value_raw"),
        "unit_raw": unit[1] if unit else None,
        "col_hint": o.get("col_hint"),
    }
    conn.execute(
        f"INSERT INTO observations({','.join(COLS)}) "
        f"VALUES ({','.join('?' for _ in COLS)})",
        tuple(_enc(row[c]) for c in COLS),
    )
