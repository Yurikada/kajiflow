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

SCHEMA_VERSION = 3

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

CREATE TABLE IF NOT EXISTS vault_tasks (
  uid TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  section TEXT DEFAULT '',
  checked INTEGER NOT NULL DEFAULT 0,
  project TEXT DEFAULT '',
  due TEXT DEFAULT '',
  purpose TEXT DEFAULT '',
  done_when TEXT DEFAULT '',
  source TEXT DEFAULT '',
  is_handoff INTEGER NOT NULL DEFAULT 0,
  handoff_owner TEXT DEFAULT '',
  handoff_status TEXT DEFAULT '',
  classification TEXT,
  suggested TEXT,
  completed_via TEXT,
  last_seen_at TEXT NOT NULL,
  meta_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS gtasks_links (
  kind TEXT,
  local_id TEXT,
  gtask_id TEXT,
  list_id TEXT,
  synced_at TEXT,
  PRIMARY KEY (kind, local_id)
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
    # WAL で読み書きの並行性を上げる（gtasks 同期のような長めの書き込み中でも
    # 他 API の読み取りがブロックされない）。設定は DB ファイルに永続化されるが、
    # 毎接続で実行しても冪等。
    conn.execute("PRAGMA journal_mode = WAL")
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
        # v2: vault_tasks 追加 / v3: gtasks_links 追加。テーブル作成自体は
        # SCHEMA_SQL の CREATE TABLE IF NOT EXISTS が既存 DB にも冪等に
        # 適用する。将来のスキーマ変更はここに version 判定で追加する
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
