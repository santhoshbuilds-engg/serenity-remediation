from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class Store:
    def __init__(self, path):
        self.path = str(Path(path).resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, cache_key TEXT UNIQUE, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS failures (id TEXT PRIMARY KEY, run_id TEXT, fingerprint TEXT, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS failure_run ON failures(run_id);
                CREATE TABLE IF NOT EXISTS evidence (id TEXT PRIMARY KEY, run_id TEXT, name TEXT, content TEXT);
                CREATE TABLE IF NOT EXISTS validations (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS resolutions (id TEXT PRIMARY KEY, failure_id TEXT, project TEXT, framework TEXT,
                    fingerprint TEXT, disposition TEXT, verified INTEGER, data TEXT NOT NULL);
                CREATE VIRTUAL TABLE IF NOT EXISTS repair_search USING fts5(resolution_id UNINDEXED, text);
                CREATE TABLE IF NOT EXISTS attempts (run_id TEXT, fingerprint TEXT, patch_ref TEXT,
                    PRIMARY KEY(run_id, fingerprint, patch_ref));
                CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, kind TEXT, data TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP);
                PRAGMA user_version=1;
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, table, key):
        if table not in {"runs", "failures", "validations", "resolutions"}:
            raise ValueError("Unknown table")
        with self.connect() as db:
            row = db.execute(f"SELECT data FROM {table} WHERE id=?", (key,)).fetchone()
        if not row:
            raise ValueError(f"Unknown {table} identifier")
        return json.loads(row["data"])

    def event(self, kind, data):
        with self.connect() as db:
            db.execute("INSERT INTO events(kind,data) VALUES (?,?)", (kind, json.dumps(data)))
