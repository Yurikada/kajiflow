"""KajiFlow FastAPI アプリ・ルーティング。

- すべて JSON。認証なし（LAN 内利用前提）。
- 起動時に DB 初期化と当日プラン生成（未生成なら）を行う。
- `/` は static/index.html（static/ が無くても起動は失敗しない）。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import asynccontextmanager, closing
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db as dbmod
from . import engine, gtasks, seed, vault
from .engine import JST

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

SETTINGS_DEFAULTS: dict[str, str] = {
    "daily_budget_min": "30",
    "ntfy_topic": "",
    "ntfy_server": "https://ntfy.sh",
    "ntfy_token": "",
}

# 'calendar' はシステム管理（gtasks 同期が upsert）。ユーザー API からの
# 作成・変更は 422（validate_task_fields / api_tasks_update 参照）。
VALID_SCHEDULE_TYPES = ("interval", "weekly", "calendar")
CALENDAR_TYPE_ERROR = (
    "schedule_type='calendar' はシステム管理のため作成・変更できません"
    "（ゴミカレンダー連動が自動生成します）"
)


# ---------------------------------------------------------------- helpers

def now_jst() -> datetime:
    return datetime.now(engine.JST)


def today_jst() -> date:
    return now_jst().date()


def get_conn() -> sqlite3.Connection:
    return dbmod.connect()


def task_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["adaptive"] = int(d.get("adaptive") or 0)
    d["enabled"] = int(d.get("enabled") or 0)
    return d


def fetch_tasks(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM tasks ORDER BY id").fetchall()
    return [task_to_dict(r) for r in rows]


def fetch_task(conn: sqlite3.Connection, task_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return task_to_dict(row) if row else None


def fetch_history(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT task_id, completed_at, action FROM completions ORDER BY completed_at"
    ).fetchall()
    return [dict(r) for r in rows]


def get_setting(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is not None and row["value"] is not None:
        return str(row["value"])
    return SETTINGS_DEFAULTS.get(key, "")


def get_budget_min(conn: sqlite3.Connection) -> int:
    try:
        return int(get_setting(conn, "daily_budget_min"))
    except ValueError:
        return 30


def fetch_gomi_due(conn: sqlite3.Connection, d: date, tasks: list[dict]) -> set[int]:
    """その日の gomi_events に該当する calendar タスクの id 集合を返す。

    gomi_events の summary と「ゴミ出し: {summary}」の名前一致で対応づける
    （gtasks の合成タスク upsert と同じ規則）。engine は DB を参照しない
    純関数のため、ここで集合に組み立てて build_plan へ渡す。
    """
    rows = conn.execute(
        "SELECT summary FROM gomi_events WHERE date = ?", (d.isoformat(),)
    ).fetchall()
    names = {f"{gtasks.GOMI_TASK_PREFIX}{r['summary']}" for r in rows}
    return {
        t["id"] for t in tasks
        if t.get("schedule_type") == "calendar" and t["name"] in names
    }


def compute_plan(conn: sqlite3.Connection, d: date) -> list[int]:
    """現在の tasks / completions からその日のプランを計算する（保存はしない）。"""
    tasks = [t for t in fetch_tasks(conn) if t["enabled"]]
    history = fetch_history(conn)
    gomi_due = fetch_gomi_due(conn, d, tasks)
    return engine.build_plan(tasks, history, d, get_budget_min(conn), gomi_due=gomi_due)


def ensure_today_plan(conn: sqlite3.Connection, d: date | None = None) -> list[int]:
    """当日プランが未生成なら生成して凍結保存し、task_id リストを返す。

    sync エンドポイントはスレッドプールで並行実行されるため、同日初回アクセスが
    同時に来ると SELECT→INSERT が競合しうる。INSERT OR IGNORE で一意制約違反を
    避け、他スレッドが先に挿入していた場合は保存済みプランを読み直して返す。
    """
    d = d or today_jst()
    date_str = d.isoformat()
    row = conn.execute(
        "SELECT task_ids FROM daily_plans WHERE date = ?", (date_str,)
    ).fetchone()
    if row is not None:
        return json.loads(row["task_ids"])
    plan = compute_plan(conn, d)
    cur = conn.execute(
        "INSERT OR IGNORE INTO daily_plans (date, task_ids, generated_at) "
        "VALUES (?, ?, ?)",
        (date_str, json.dumps(plan), now_jst().isoformat()),
    )
    conn.commit()
    if cur.rowcount == 0:
        # 他スレッドが先に生成済み: 凍結保存されたプランを正とする
        row = conn.execute(
            "SELECT task_ids FROM daily_plans WHERE date = ?", (date_str,)
        ).fetchone()
        if row is not None:
            return json.loads(row["task_ids"])
    return plan


def today_statuses(conn: sqlite3.Connection, d: date) -> dict[int, str]:
    """当日の completions から task_id -> 'done' | 'skip' を返す（done 優先）。"""
    date_str = d.isoformat()
    rows = conn.execute(
        "SELECT task_id, action FROM completions WHERE substr(completed_at, 1, 10) = ?",
        (date_str,),
    ).fetchall()
    statuses: dict[int, str] = {}
    for r in rows:
        tid, action = r["task_id"], r["action"]
        if statuses.get(tid) != "done":
            statuses[tid] = action
    return statuses


def next_payload(conn: sqlite3.Connection) -> dict:
    """/api/next のレスポンスを組み立てる。"""
    d = today_jst()
    plan = ensure_today_plan(conn, d)
    statuses = today_statuses(conn, d)
    next_task = None
    done_count = 0
    for tid in plan:
        status = statuses.get(tid)
        if status == "done":
            done_count += 1
        elif status is None and next_task is None:
            next_task = fetch_task(conn, tid)
    return {
        "task": next_task,
        "done_count": done_count,
        "total_count": len(plan),
    }


def validate_task_fields(data: dict) -> str | None:
    """タスクの整合性チェック。問題があれば日本語エラーメッセージを返す。"""
    if not str(data.get("name") or "").strip():
        return "タスク名を入力してください"
    st = data.get("schedule_type")
    if st not in VALID_SCHEDULE_TYPES:
        return "schedule_type は 'interval' か 'weekly' を指定してください"
    if st == "interval":
        iv = data.get("interval_days")
        if iv is None or float(iv) <= 0:
            return "interval タスクには 1 以上の interval_days が必要です"
    elif st == "weekly":
        if not engine.parse_weekdays(data.get("weekdays")):
            return "weekly タスクには weekdays（例: '0,3'、月=0〜日=6）が必要です"
    # calendar は追加条件なし（システム管理。作成・変更拒否は API 層で行う）
    return None


# ---------------------------------------------------------------- models

class TaskCreate(BaseModel):
    name: str
    category: str = "その他"
    est_minutes: int = 10
    schedule_type: str = "interval"
    interval_days: float | None = None
    weekdays: str | None = None
    adaptive: int = 1
    enabled: int = 1
    notes: str = ""


class TaskUpdate(BaseModel):
    name: str | None = None
    category: str | None = None
    est_minutes: int | None = None
    schedule_type: str | None = None
    interval_days: float | None = None
    weekdays: str | None = None
    adaptive: int | None = None
    enabled: int | None = None
    notes: str | None = None


class TemplatesApply(BaseModel):
    names: list[str]


class VaultClassify(BaseModel):
    classification: str | None = None


class VaultComplete(BaseModel):
    note: str | None = None


# ---------------------------------------------------------------- app

@asynccontextmanager
async def lifespan(app: FastAPI):
    with closing(get_conn()) as conn:
        ensure_today_plan(conn)
    yield


app = FastAPI(title="KajiFlow", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/next")
def api_next() -> dict:
    with closing(get_conn()) as conn:
        return next_payload(conn)


def _record_action(task_id: int, action: str) -> dict:
    with closing(get_conn()) as conn:
        task = fetch_task(conn, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="タスクが見つかりません")
        # 冪等ガード: 同日に done/skip 記録が既にあれば再送とみなして記録しない。
        # 完了ボタンの二度押し・通信リトライで completions が重複すると、
        # 極小 gap が EWMA に入り実効間隔が下限まで誤学習するため。
        # 素の SELECT→INSERT では同時リクエストが両方「記録なし」を観測して
        # 二重挿入になる（Codex レビューで再現）ため、BEGIN IMMEDIATE で
        # 書き込みロックを取ってから確認・挿入を行い原子化する。
        d = today_jst()
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute(
                "SELECT 1 FROM completions WHERE task_id = ? AND completed_at >= ? AND completed_at < ? LIMIT 1",
                (
                    task_id,
                    datetime.combine(d, time.min, tzinfo=JST).isoformat(),
                    datetime.combine(d + timedelta(days=1), time.min, tzinfo=JST).isoformat(),
                ),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO completions (task_id, completed_at, action) VALUES (?, ?, ?)",
                    (task_id, now_jst().isoformat(), action),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        payload = next_payload(conn)
        # 前倒し完了 UI が「記録された / 既に記録済み」を案内できるようにする
        payload["recorded"] = existing is None
        return payload


@app.post("/api/tasks/{task_id}/complete")
def api_complete(task_id: int) -> dict:
    return _record_action(task_id, "done")


@app.post("/api/tasks/{task_id}/skip")
def api_skip(task_id: int) -> dict:
    return _record_action(task_id, "skip")


@app.get("/api/today")
def api_today() -> dict:
    with closing(get_conn()) as conn:
        d = today_jst()
        plan = ensure_today_plan(conn, d)
        statuses = today_statuses(conn, d)
        items = []
        for tid in plan:
            task = fetch_task(conn, tid)
            if task is None:
                continue
            items.append({"task": task, "status": statuses.get(tid, "pending")})
        return {"date": d.isoformat(), "items": items}


@app.post("/api/plan/regenerate")
def api_regenerate() -> dict:
    with closing(get_conn()) as conn:
        d = today_jst()
        # DELETE→再生成の間に別リクエストが割り込んで INSERT しないよう、
        # 同一トランザクション（BEGIN IMMEDIATE で書き込みロック確保）で行う。
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DELETE FROM daily_plans WHERE date = ?", (d.isoformat(),))
            plan = compute_plan(conn, d)
            conn.execute(
                "INSERT INTO daily_plans (date, task_ids, generated_at) "
                "VALUES (?, ?, ?)",
                (d.isoformat(), json.dumps(plan), now_jst().isoformat()),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return next_payload(conn)


# -------------------------------------------------- tasks CRUD

@app.get("/api/tasks")
def api_tasks_list() -> list[dict]:
    with closing(get_conn()) as conn:
        return fetch_tasks(conn)


def _insert_task(conn: sqlite3.Connection, data: dict) -> dict:
    cur = conn.execute(
        """
        INSERT INTO tasks
          (name, category, est_minutes, schedule_type, interval_days,
           weekdays, adaptive, enabled, notes, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            data["name"].strip(),
            data.get("category") or "その他",
            int(data.get("est_minutes") or 10),
            data["schedule_type"],
            data.get("interval_days"),
            data.get("weekdays"),
            1 if data.get("adaptive", 1) else 0,
            1 if data.get("enabled", 1) else 0,
            data.get("notes") or "",
            now_jst().isoformat(),
        ),
    )
    return fetch_task(conn, cur.lastrowid)


@app.post("/api/tasks", status_code=201)
def api_tasks_create(payload: TaskCreate) -> dict:
    data = payload.model_dump()
    if data.get("schedule_type") == "calendar":
        raise HTTPException(status_code=422, detail=CALENDAR_TYPE_ERROR)
    error = validate_task_fields(data)
    if error:
        raise HTTPException(status_code=422, detail=error)
    with closing(get_conn()) as conn:
        task = _insert_task(conn, data)
        conn.commit()
        return task


@app.get("/api/tasks/{task_id}")
def api_tasks_get(task_id: int) -> dict:
    with closing(get_conn()) as conn:
        task = fetch_task(conn, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="タスクが見つかりません")
        return task


@app.put("/api/tasks/{task_id}")
def api_tasks_update(task_id: int, payload: TaskUpdate) -> dict:
    with closing(get_conn()) as conn:
        task = fetch_task(conn, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="タスクが見つかりません")
        updates = payload.model_dump(exclude_unset=True)
        # schedule_type='calendar' への変更・からの変更はシステム管理のため 422。
        # calendar タスク自体の名前・分数・メモ等の編集や enabled トグルは可。
        if "schedule_type" in updates and updates["schedule_type"] != task["schedule_type"]:
            if "calendar" in (updates["schedule_type"], task["schedule_type"]):
                raise HTTPException(status_code=422, detail=CALENDAR_TYPE_ERROR)
        merged = {**task, **updates}
        error = validate_task_fields(merged)
        if error:
            raise HTTPException(status_code=422, detail=error)
        conn.execute(
            """
            UPDATE tasks SET
              name = ?, category = ?, est_minutes = ?, schedule_type = ?,
              interval_days = ?, weekdays = ?, adaptive = ?, enabled = ?, notes = ?
            WHERE id = ?
            """,
            (
                str(merged["name"]).strip(),
                merged["category"],
                int(merged["est_minutes"]),
                merged["schedule_type"],
                merged["interval_days"],
                merged["weekdays"],
                1 if merged["adaptive"] else 0,
                1 if merged["enabled"] else 0,
                merged["notes"] or "",
                task_id,
            ),
        )
        conn.commit()
        return fetch_task(conn, task_id)


@app.delete("/api/tasks/{task_id}")
def api_tasks_delete(task_id: int) -> dict:
    with closing(get_conn()) as conn:
        task = fetch_task(conn, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="タスクが見つかりません")
        conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        conn.commit()
        return {"ok": True, "deleted_id": task_id}


# -------------------------------------------------- templates

@app.get("/api/templates")
def api_templates() -> list[dict]:
    return seed.TEMPLATES


@app.post("/api/templates/apply", status_code=201)
def api_templates_apply(payload: TemplatesApply) -> dict:
    by_name = {t["name"]: t for t in seed.TEMPLATES}
    unknown = [n for n in payload.names if n not in by_name]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"不明なテンプレ名です: {', '.join(unknown)}",
        )
    created = []
    with closing(get_conn()) as conn:
        for name in payload.names:
            tpl = dict(by_name[name])
            tpl.setdefault("adaptive", 1)
            tpl.setdefault("enabled", 1)
            created.append(_insert_task(conn, tpl))
        conn.commit()
    return {"created": created}


# -------------------------------------------------- stats / settings

@app.get("/api/stats/weekly")
def api_stats_weekly(weeks: int = Query(default=1, ge=1, le=52)) -> dict:
    with closing(get_conn()) as conn:
        today = today_jst()
        this_monday = today - timedelta(days=today.weekday())
        result_weeks = []
        for i in range(weeks):
            start = this_monday - timedelta(days=7 * i)
            end = start + timedelta(days=6)
            rows = conn.execute(
                """
                SELECT c.task_id, t.name, c.action, COUNT(*) AS cnt
                FROM completions c
                JOIN tasks t ON t.id = c.task_id
                WHERE substr(c.completed_at, 1, 10) BETWEEN ? AND ?
                GROUP BY c.task_id, t.name, c.action
                """,
                (start.isoformat(), end.isoformat()),
            ).fetchall()
            by_task: dict[int, dict] = {}
            done_total = 0
            skip_total = 0
            for r in rows:
                entry = by_task.setdefault(
                    r["task_id"],
                    {"task_id": r["task_id"], "name": r["name"], "done": 0, "skip": 0},
                )
                if r["action"] == "done":
                    entry["done"] += r["cnt"]
                    done_total += r["cnt"]
                elif r["action"] == "skip":
                    entry["skip"] += r["cnt"]
                    skip_total += r["cnt"]
            iso = start.isocalendar()
            result_weeks.append({
                "label": f"{iso.year}-W{iso.week:02d}",
                "start": start.isoformat(),
                "end": end.isoformat(),
                "done": done_total,
                "skip": skip_total,
                "tasks": sorted(by_task.values(), key=lambda x: x["task_id"]),
            })
        return {"weeks": result_weeks}


@app.get("/api/settings")
def api_settings_get() -> dict:
    with closing(get_conn()) as conn:
        settings = dict(SETTINGS_DEFAULTS)
        for row in conn.execute("SELECT key, value FROM settings").fetchall():
            settings[row["key"]] = row["value"]
        return settings


@app.put("/api/settings")
def api_settings_put(payload: dict[str, Any] = Body(...)) -> dict:
    with closing(get_conn()) as conn:
        for key, value in payload.items():
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(key), str(value)),
            )
        conn.commit()
    return api_settings_get()


# -------------------------------------------------- vault 連携


@app.post("/api/vault/sync")
def api_vault_sync() -> dict:
    with closing(get_conn()) as conn:
        try:
            return vault.sync_vault(conn)
        except vault.VaultReadError as e:
            raise HTTPException(status_code=503, detail=str(e))


@app.get("/api/vault/tasks")
def api_vault_tasks(include_done: int = Query(default=0)) -> list[dict]:
    with closing(get_conn()) as conn:
        return vault.list_tasks(conn, include_done=bool(include_done))


@app.put("/api/vault/tasks/{uid}/classify")
def api_vault_classify(uid: str, payload: VaultClassify) -> dict:
    if payload.classification not in ("ai", "user", None):
        raise HTTPException(
            status_code=422,
            detail="classification は 'ai' | 'user' | null を指定してください",
        )
    with closing(get_conn()) as conn:
        try:
            vault.fetch_vault_task(conn, uid)
        except vault.VaultNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        conn.execute(
            "UPDATE vault_tasks SET classification = ? WHERE uid = ?",
            (payload.classification, uid),
        )
        conn.commit()
        task = vault.fetch_vault_task(conn, uid)
        return {k: task[k] for k in vault.LIST_FIELDS}


@app.post("/api/vault/tasks/{uid}/complete")
def api_vault_complete(uid: str, payload: VaultComplete | None = None) -> dict:
    note = payload.note if payload else None
    with closing(get_conn()) as conn:
        try:
            return vault.complete_in_vault(conn, uid, note)
        except vault.VaultNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except vault.VaultConflictError as e:
            detail: dict = {"message": str(e)}
            if e.command:
                detail["command"] = e.command
            raise HTTPException(status_code=409, detail=detail)
        except (vault.VaultReadError, vault.VaultWriteError) as e:
            raise HTTPException(status_code=503, detail=str(e))


@app.get("/api/vault/tasks/{uid}/prompt")
def api_vault_prompt(uid: str) -> PlainTextResponse:
    with closing(get_conn()) as conn:
        try:
            task = vault.fetch_vault_task(conn, uid)
        except vault.VaultNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        return PlainTextResponse(
            vault.build_prompt(task), media_type="text/plain; charset=utf-8"
        )


# -------------------------------------------------- gtasks 連携

# sync def エンドポイントはスレッドプールで並行実行されるため、Task Scheduler
# （30分ごと）・manage の「今すぐ同期」・エージェントの同期発火が重なると
# sync_all の insert が二重実行されて Google / Vault に重複タスクが生まれる。
# プロセス内 Lock で直列化する（uvicorn 単一プロセス前提なので十分）。
_gtasks_sync_lock = threading.Lock()


@app.post("/api/gtasks/sync")
def api_gtasks_sync() -> dict:
    if not _gtasks_sync_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail="Google Tasks 同期は実行中です。完了を待って再試行してください",
        )
    try:
        with closing(get_conn()) as conn:
            try:
                client = gtasks.build_client()
            except gtasks.GTasksAuthError as e:
                raise HTTPException(status_code=503, detail=str(e))
            ensure_today_plan(conn)
            try:
                result = gtasks.sync_all(conn, client, now_jst())
            except gtasks.GTasksAuthError as e:
                raise HTTPException(status_code=503, detail=str(e))
            except gtasks.GTasksError as e:
                raise HTTPException(
                    status_code=502,
                    detail=f"Google Tasks との同期に失敗しました: {e}",
                )
            now = now_jst().isoformat()
            for key, value in (
                ("gtasks_last_sync_at", now),
                ("gtasks_last_result", json.dumps(result, ensure_ascii=False)),
            ):
                conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )
            conn.commit()
            return result
    finally:
        _gtasks_sync_lock.release()


@app.get("/api/gtasks/status")
def api_gtasks_status() -> dict:
    with closing(get_conn()) as conn:
        last_sync_at = get_setting(conn, "gtasks_last_sync_at") or None
        raw = get_setting(conn, "gtasks_last_result")
        try:
            last_result = json.loads(raw) if raw else None
        except ValueError:
            last_result = None
        return {
            "authorized": gtasks.is_authorized(),
            "last_sync_at": last_sync_at,
            "last_result": last_result,
        }


# -------------------------------------------------- static

if STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
else:
    @app.get("/")
    def index_placeholder() -> JSONResponse:
        return JSONResponse(
            {"message": "KajiFlow API は起動しています（static/ が未配置です）"}
        )
