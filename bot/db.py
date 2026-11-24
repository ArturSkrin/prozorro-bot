"""SQLite (WAL). Общая БД для процесса бота и веба."""
import json
import os
import sqlite3
import time

from .match import DEFAULT_SEARCH

SCHEMA = """
CREATE TABLE IF NOT EXISTS searches (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    params TEXT NOT NULL,                 -- JSON, см. match.DEFAULT_PARAMS
    chat_ids TEXT NOT NULL DEFAULT '[]',  -- JSON; пусто = все чаты из .env
    send_changes INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS tenders (
    id TEXT PRIMARY KEY,          -- внутренний id API
    tender_id TEXT,               -- UA-...
    status TEXT,
    amount REAL,
    currency TEXT,
    winner TEXT,                  -- ЄДРПОУ победителя(ей) через запятую, для отслеживания смены
    date_modified TEXT,
    json TEXT
);
CREATE TABLE IF NOT EXISTS matches (
    search_id INTEGER NOT NULL,
    tender_id TEXT NOT NULL,      -- tenders.id
    sent_at REAL,
    state TEXT NOT NULL DEFAULT 'new',   -- new | taken | skip
    taken_by TEXT,
    reasons TEXT,
    PRIMARY KEY (search_id, tender_id)
);
CREATE TABLE IF NOT EXISTS messages (     -- куда отправлено, чтобы обновлять «взял Имя» у всех
    tender_id TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS errors (ts REAL, text TEXT);
"""


def connect(path=None):
    con = sqlite3.connect(path or os.environ.get("DB_PATH", "prozorro.db"), timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    if not con.execute("SELECT 1 FROM searches LIMIT 1").fetchone():
        save_search(con, DEFAULT_SEARCH["name"], DEFAULT_SEARCH["params"])
    return con


def get_state(con, key, default=None):
    row = con.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_state(con, key, value):
    con.execute("INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, None if value is None else str(value)))


def log_error(con, text):
    con.execute("INSERT INTO errors VALUES(?,?)", (time.time(), text[:500]))
    con.execute("DELETE FROM errors WHERE ts < ?", (time.time() - 7 * 86400,))


def searches(con, enabled_only=False):
    sql = "SELECT * FROM searches" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY id"
    return [{**dict(r), "params": json.loads(r["params"]), "chat_ids": json.loads(r["chat_ids"])}
            for r in con.execute(sql)]


def get_search(con, sid):
    return next((s for s in searches(con) if s["id"] == sid), None)


def save_search(con, name, params, sid=None, enabled=1, chat_ids=(), send_changes=1):
    args = (name, json.dumps(params, ensure_ascii=False), int(enabled), json.dumps(list(chat_ids)), int(send_changes))
    if sid:
        con.execute("UPDATE searches SET name=?, params=?, enabled=?, chat_ids=?, send_changes=? WHERE id=?", args + (sid,))
        return sid
    return con.execute("INSERT INTO searches(name,params,enabled,chat_ids,send_changes) VALUES(?,?,?,?,?)", args).lastrowid


def delete_search(con, sid):
    con.execute("DELETE FROM searches WHERE id=?", (sid,))
    con.execute("DELETE FROM matches WHERE search_id=?", (sid,))


def upsert_tender(con, t, winner):
    """Сохраняет тендер, возвращает предыдущую строку (или None) — для отслеживания изменений."""
    old = con.execute("SELECT status, winner FROM tenders WHERE id=?", (t["id"],)).fetchone()
    v = t.get("value") or {}
    con.execute("INSERT OR REPLACE INTO tenders VALUES(?,?,?,?,?,?,?,?)",
                (t["id"], t.get("tenderID"), t.get("status"), v.get("amount"), v.get("currency"), winner,
                 t.get("dateModified"), json.dumps(t, ensure_ascii=False)))
    return old


def add_match(con, search_id, tender_id, reasons):
    """Дедуп: True только при первом совпадении пары (search_id, tender_id)."""
    cur = con.execute("INSERT OR IGNORE INTO matches(search_id,tender_id,sent_at,reasons) VALUES(?,?,?,?)",
                      (search_id, tender_id, time.time(), json.dumps(reasons, ensure_ascii=False)))
    return cur.rowcount == 1


def matched_ids(con):
    return {r[0] for r in con.execute("SELECT DISTINCT tender_id FROM matches")}
