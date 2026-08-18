"""DB 接続・スキーマ作成・マイグレーション。

DB パスは環境変数 KAJIFLOW_DB があればそれを使い、
なければプロジェクト直下の data/kajiflow.db を使う。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "kajiflow.db"

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  category TEXT NOT NULL DEFAULT 'その他',
  est_minutes INTEGER NOT NULL DEFAULT 10,
  schedule_type TEXT NOT NULL DEFAULT 'interval',
  interval_days REAL,
  weekdays TEXT,
  adaptive INTEGER NOT NULL DEFAULT 1,
  enabled INTEGER NOT NULL DEFAULT 1,
  notes TEXT DEFAULT '',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS completions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  completed_at TEXT NOT NULL,
  action TEXT NOT NULL DEFAULT 'done'
);

CREATE INDEX IF NOT EXISTS idx_completions_task ON completions(task_id, completed_at);

CREATE TABLE IF NOT EXISTS daily_plans (
  date TEXT PRIMARY KEY,
  task_ids TEXT NOT NULL,
  generated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT
);
"""


def get_db_path() -> Path:
    """環境変数 KAJIFLOW_DB を優先して DB パスを返す。"""
    env = os.environ.get("KAJIFLOW_DB")
    if env:
        return Path(env)
    return DEFAULT_DB_PATH


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """接続を開き、スキーマを保証して返す。"""
    path = Path(db_path) if db_path else get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """スキーマ作成とマイグレーションを行う（冪等）。"""
    conn.executescript(SCHEMA_SQL)
    migrate(conn)
    conn.commit()


def migrate(conn: sqlite3.Connection) -> None:
    """user_version ベースの簡易マイグレーション。"""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < SCHEMA_VERSION:
        # 将来のスキーマ変更はここに version 判定で追加する
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
