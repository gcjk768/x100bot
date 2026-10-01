"""SQLite schema and connection. WAL mode, autocommit, rows as dicts."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS rate_state(
  bucket TEXT PRIMARY KEY, day TEXT, day_count INTEGER DEFAULT 0, last_call_at REAL, cooldown_until REAL,
  consecutive_failures INTEGER DEFAULT 0, cooldown_reason TEXT);
CREATE TABLE IF NOT EXISTS requests_log(
  id INTEGER PRIMARY KEY, job TEXT, bucket TEXT, at REAL, target TEXT, status INTEGER, latency_ms INTEGER, outcome TEXT);
CREATE INDEX IF NOT EXISTS requests_log_bucket_at ON requests_log(bucket, at);
CREATE TABLE IF NOT EXISTS recipes(
  id INTEGER PRIMARY KEY, name TEXT, author TEXT, source_url TEXT UNIQUE, sensor TEXT, compat_label TEXT,
  film_simulation TEXT, settings_json TEXT, adapted_settings_json TEXT, settings_hash TEXT UNIQUE,
  light_tags_json TEXT, fetched_at REAL, last_posted_at REAL, times_posted INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS people(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE, kind TEXT, identity TEXT, official_url TEXT, urls_json TEXT,
  facts_json TEXT, themes_json TEXT, verified_at REAL, last_posted_at REAL);
CREATE TABLE IF NOT EXISTS videos(
  id INTEGER PRIMARY KEY, url TEXT UNIQUE, channel TEXT, title TEXT, description TEXT, published_at TEXT,
  verified_at REAL, last_posted_at REAL);
CREATE TABLE IF NOT EXISTS tips(
  id INTEGER PRIMARY KEY, topic TEXT, title TEXT, facts_json TEXT, menu_path TEXT, manual_url TEXT,
  verified_at REAL, last_posted_at REAL);
CREATE TABLE IF NOT EXISTS lessons(
  id INTEGER PRIMARY KEY, type TEXT, concept_title TEXT, theme TEXT, posted_at REAL);
CREATE TABLE IF NOT EXISTS pages(
  url TEXT PRIMARY KEY, etag TEXT, last_modified TEXT, fetched_at REAL, status INTEGER, body_path TEXT);
CREATE TABLE IF NOT EXISTS link_checks(
  url TEXT PRIMARY KEY, status INTEGER, final_url TEXT, checked_at REAL);
CREATE TABLE IF NOT EXISTS light_days(
  date TEXT PRIMARY KEY, sunrise TEXT, sunset TEXT, golden_morning_end TEXT, golden_evening_start TEXT,
  blue_end TEXT, weather_json TEXT);
CREATE TABLE IF NOT EXISTS queue(
  id INTEGER PRIMARY KEY, slot_at TEXT UNIQUE, type TEXT, ref_type TEXT, ref_id INTEGER, facts_json TEXT,
  fields_json TEXT, text_html TEXT, preview_url TEXT, notify INTEGER,
  status TEXT CHECK(status IN ('pending','sending','posted','failed','skipped')),
  message_id INTEGER, posted_at REAL, used_fallback INTEGER);
CREATE TABLE IF NOT EXISTS custom_banks(bank INTEGER PRIMARY KEY, recipe_id INTEGER, set_at REAL);
CREATE TABLE IF NOT EXISTS favorites(id INTEGER PRIMARY KEY, message_id INTEGER, queue_id INTEGER, saved_at REAL);
CREATE TABLE IF NOT EXISTS assignments(date TEXT PRIMARY KEY, text TEXT, done_at REAL);
CREATE TABLE IF NOT EXISTS critiques(id INTEGER PRIMARY KEY, at REAL, result_json TEXT, cost_usd REAL);
CREATE TABLE IF NOT EXISTS firmware(version TEXT PRIMARY KEY, found_at REAL, url TEXT);
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY, job TEXT, started_at REAL, finished_at REAL, status TEXT, cost_usd REAL,
  claude_session_id TEXT, note TEXT);
-- small key value store: the getUpdates offset
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def tx(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE takes the write lock up front, so two processes can never claim the same row."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def kv_get(conn, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def kv_set(conn, key: str, value) -> None:
    conn.execute("INSERT OR REPLACE INTO kv(key, value) VALUES(?, ?)", (key, str(value)))
