# -*- coding: utf-8 -*-
"""统一 SQLite 存储（WAL）。表结构 = 三项目 schema 的并集 + channel 维度。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from src.core.config import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts(
  id TEXT PRIMARY KEY, channel TEXT, name TEXT, status TEXT, site_region TEXT,
  proxy TEXT, profile_path TEXT, created_at REAL, last_used_at REAL,
  last_checked_at REAL, last_checkin_at REAL, last_checkin_status TEXT,
  total_generations INTEGER DEFAULT 0, total_requests INTEGER DEFAULT 0,
  total_success INTEGER DEFAULT 0, total_failure INTEGER DEFAULT 0,
  consecutive_errors INTEGER DEFAULT 0, cooldown_until REAL, error_count INTEGER DEFAULT 0,
  note TEXT, account_type TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS account_secrets(
  account_id TEXT PRIMARY KEY, encrypted_cookies TEXT, encrypted_browser_state TEXT,
  encrypted_oauth TEXT, encrypted_extra TEXT, cookie_updated_at REAL, token_updated_at REAL);
CREATE TABLE IF NOT EXISTS account_capabilities(
  account_id TEXT, model_id TEXT, available INTEGER, quota TEXT,
  raw_redacted_json TEXT, checked_at REAL, PRIMARY KEY(account_id, model_id));
CREATE TABLE IF NOT EXISTS account_credits(
  account_id TEXT PRIMARY KEY, total REAL DEFAULT 0, allocatable REAL DEFAULT 0,
  buckets_json TEXT, raw_redacted_json TEXT, checked_at REAL, error TEXT);
CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY, channel TEXT, account_id TEXT, upstream_task_id TEXT,
  kind TEXT, model TEXT, mode TEXT, status TEXT, prompt_redacted TEXT,
  request_json TEXT, response_json TEXT, media_json TEXT, transport TEXT DEFAULT 'requests',
  error TEXT, error_class TEXT, idempotency_key TEXT, credit_cost REAL,
  created_at REAL, updated_at REAL, finished_at REAL,
  next_poll_at REAL, last_poll_at REAL, poll_attempts INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS api_keys(
  id TEXT PRIMARY KEY, name TEXT, prefix TEXT, secret_hash TEXT, secret_plain TEXT,
  enabled INTEGER DEFAULT 1, created_at REAL, last_used_at REAL,
  rpm_limit INTEGER DEFAULT 120, max_concurrent INTEGER DEFAULT 8,
  allowed_channels TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS checkin_runs(
  id TEXT PRIMARY KEY, channel TEXT, account_id TEXT, started_at REAL, finished_at REAL,
  status TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, channel TEXT, account_id TEXT,
  direction TEXT, method TEXT, url TEXT, status INTEGER, body_shape TEXT, headers_shape TEXT);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, next_poll_at);
CREATE INDEX IF NOT EXISTS idx_accounts_channel ON accounts(channel, status);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
"""


class DB:
    def __init__(self):
        self.path = Path(config.database)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """轻量迁移：确保新增列存在（老库自动补列，避免 schema 与代码漂移）。"""
        wanted = [
            ("accounts", "account_type",
             "ALTER TABLE accounts ADD COLUMN account_type TEXT DEFAULT ''"),
        ]
        for table, col, ddl in wanted:
            try:
                cols = [r[1] for r in self.conn.execute("PRAGMA table_info(%s)" % table)]
                if col not in cols:
                    self.conn.execute(ddl)
            except Exception:  # noqa: BLE001
                pass

    def execute(self, sql, args=(), commit=False):
        with self.lock:
            cur = self.conn.execute(sql, args)
            if commit:
                self.conn.commit()
            return cur

    def one(self, sql, args=()):
        r = self.execute(sql, args).fetchone()
        return dict(r) if r else None

    def all(self, sql, args=()):
        return [dict(r) for r in self.execute(sql, args).fetchall()]


db = DB()


def now() -> float:
    return time.time()


def jd(x) -> str:
    return json.dumps(x, ensure_ascii=False, separators=(",", ":"))


def jl(x, default):
    try:
        return json.loads(x) if x else default
    except Exception:
        return default
