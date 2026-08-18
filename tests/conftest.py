"""pytest 共通フィクスチャ。

- プロジェクトルートを sys.path に追加して `app` パッケージを import 可能にする。
- API テスト用に、一時ディレクトリの DB（環境変数 KAJIFLOW_DB）を使う
  TestClient フィクスチャを提供する。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """一時ディレクトリの DB パスを KAJIFLOW_DB に設定して返す。"""
    path = tmp_path / "kajiflow_test.db"
    monkeypatch.setenv("KAJIFLOW_DB", str(path))
    return path


@pytest.fixture
def client(db_path):
    """一時 DB を使う TestClient。

    with ブロックで lifespan（起動時の当日プラン生成）も実行される。
    """
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def raw_conn(db_path):
    """テストから直接 DB を操作するための sqlite3 接続ファクトリ。

    created_at の過去日付への書き換え（バックデート）などに使う。
    """

    def _connect() -> sqlite3.Connection:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn

    return _connect
