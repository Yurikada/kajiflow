"""Google Tasks 連携（v3）。

kajiflow が唯一の認証保持者。Google Tasks は「完了操作」と「新規追加」の
入力面であり、タスクの存在は Vault / kajiflow が正（SPEC「Google Tasks 連携」）。

構成:
- GTasksClient: Tasks API 呼び出しを隔離する薄いラッパ（実 API）。
  認証は data/gtasks_token.json（自動リフレッシュ）。未認証は GTasksAuthError。
  テストは同じメソッド群を持つフェイクを sync_all に注入する。
- sync_all(conn, client, now): 全体調停。vault 4項目・chore 2項目を実行し
  {pushed, completed_in_vault, completed_chores, new_from_google, warnings}
  を返す。冪等（同一入力で2回実行しても差分ゼロ）。
- add_task_to_vault: agent_memory_io.py add-task を subprocess で呼ぶ
  （スクリプトパスは環境変数 KAJIFLOW_MEMORY_IO で差し替え可能）。

conn は sqlite3.Row を row_factory に持つ接続（db.connect() が返すもの）を前提。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

from . import vault
from .db import PROJECT_ROOT
from .engine import JST

# ---------------------------------------------------------------- constants

DATA_DIR = PROJECT_ROOT / "data"
CLIENT_SECRET_PATH = DATA_DIR / "client_secret.json"   # 初回認証（scripts/gtasks_auth.py）用
TOKEN_PATH = DATA_DIR / "gtasks_token.json"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
SCOPES = [
    "https://www.googleapis.com/auth/tasks",
    CALENDAR_SCOPE,  # ゴミカレンダー連動（v4）
]

VAULT_LIST_TITLE = "Vault タスク"
CHORE_LIST_TITLE = "KajiFlow 家事"
SYNC_MARK = "(KajiFlow同期)"

AUTH_HINT = "Google Tasks は未認証です。scripts/gtasks_auth.py を実行してください"
CALENDAR_REAUTH_HINT = (
    "カレンダー連動には再認証が必要です。"
    "scripts/gtasks_auth.py を再実行してください"
)

# ゴミカレンダー連動: 対象カレンダー ID の settings キーと取得期間（日数）
GOMI_CALENDAR_SETTING = "gcal_gomi_calendar_id"
GOMI_WINDOW_DAYS = 7
GOMI_TASK_PREFIX = "ゴミ出し: "
GOMI_CATEGORY = "ゴミ"
GOMI_EST_MINUTES = 5

# notes 先頭行の uid マーカー（例: `uid: 0123abcd…` / `uid: T2026…`）
UID_LINE_RE = re.compile(r"^uid: (\S+)$", re.MULTILINE)
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


# ---------------------------------------------------------------- errors

class GTasksError(Exception):
    """Google Tasks 連携の基底エラー。str(e) は日本語メッセージ。"""


class GTasksAuthError(GTasksError):
    """未認証・認証切れ（API では 503）。"""


class GTasksApiError(GTasksError):
    """Tasks API 呼び出し失敗（項目単位では warnings、全体では 502）。"""


# ---------------------------------------------------------------- real client

def is_authorized() -> bool:
    """ネットワークを叩かずに認証状態を判定する（status 表示用）。"""
    if not TOKEN_PATH.exists():
        return False
    try:
        from google.oauth2.credentials import Credentials

        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    except Exception:
        return False
    return bool(creds.valid or creds.refresh_token)


class GTasksClient:
    """Tasks API の薄いラッパ（実ネットワーク）。

    テストのフェイクは同じメソッド契約を実装して sync_all に注入する:

    - list_tasklists() -> list[dict]        各 {"id", "title"}
    - insert_tasklist(title) -> dict        {"id", "title"}
    - list_tasks(list_id) -> list[dict]     完了済みも含む全件。
        各 {"id", "title", "notes"?, "status", "due"?}
        （status は 'needsAction' | 'completed'。due は RFC3339）
    - insert_task(list_id, body) -> dict    body: title/notes/status/due
    - patch_task(list_id, gtask_id, body) -> dict   部分更新（due=None はクリア）
    - delete_task(list_id, gtask_id) -> None        404 は成功扱い（冪等）
    - list_gomi_events(calendar_id, start_date, end_date) -> list[dict]
        期間 [start_date, end_date]（両端含む）の終日イベントのみ。
        各 {"date": "YYYY-MM-DD", "summary": str}。
        スコープ不足（RefreshError / 403）は GTasksAuthError（再認証案内）。

    失敗は GTasksError 系で送出する（認証切れは GTasksAuthError）。
    """

    def __init__(self) -> None:
        self._creds = self._load_credentials()
        from googleapiclient.discovery import build

        self._service = build(
            "tasks", "v1", credentials=self._creds, cache_discovery=False
        )
        self._calendar_service = None  # calendar v3 は必要になるまで作らない

    @staticmethod
    def _load_credentials():
        if not TOKEN_PATH.exists():
            raise GTasksAuthError(AUTH_HINT)
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        try:
            # scopes は指定せず、token 保存時に同意済みのスコープのまま読み込む。
            # 新 SCOPES（calendar.readonly 込み）を強制すると、v3 時代の
            # tasks のみの token では refresh が invalid_scope で落ちて
            # GTasksClient の生成自体が失敗し、家事・Vault 同期まで巻き込んで
            # 503 になる。SPEC はスコープ不足時「カレンダー取得だけを諦めて
            # warnings」を要求するため、不足判定は list_gomi_events 側で行う。
            creds = Credentials.from_authorized_user_file(str(TOKEN_PATH))
        except Exception:
            raise GTasksAuthError(
                f"{AUTH_HINT}（{TOKEN_PATH.name} を読み取れません）"
            )
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception as e:
                    raise GTasksAuthError(
                        f"Google Tasks のトークン更新に失敗しました（{e}）。"
                        "scripts/gtasks_auth.py を再実行してください"
                    )
                try:
                    TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
                except OSError:
                    pass  # 保存失敗は次回また refresh すればよい
            else:
                raise GTasksAuthError(AUTH_HINT)
        return creds

    def _execute(self, request):
        from googleapiclient.errors import HttpError

        try:
            return request.execute()
        except HttpError as e:
            status = getattr(getattr(e, "resp", None), "status", None)
            if status in (401, 403):
                raise GTasksAuthError(
                    "Google Tasks の認証が拒否されました。"
                    "scripts/gtasks_auth.py を再実行してください"
                )
            raise GTasksApiError(f"Tasks API 呼び出しに失敗しました（HTTP {status}）")
        except GTasksError:
            raise
        except Exception as e:
            raise GTasksApiError(f"Tasks API との通信に失敗しました（{e}）")

    # -------------------------------------------------- tasklists

    def list_tasklists(self) -> list[dict]:
        items: list[dict] = []
        page_token = None
        while True:
            resp = self._execute(
                self._service.tasklists().list(maxResults=100, pageToken=page_token)
            )
            items.extend(resp.get("items", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                return items

    def insert_tasklist(self, title: str) -> dict:
        return self._execute(self._service.tasklists().insert(body={"title": title}))

    # -------------------------------------------------- tasks

    def list_tasks(self, list_id: str) -> list[dict]:
        items: list[dict] = []
        page_token = None
        while True:
            resp = self._execute(
                self._service.tasks().list(
                    tasklist=list_id,
                    showCompleted=True,
                    showHidden=True,
                    maxResults=100,
                    pageToken=page_token,
                )
            )
            items.extend(resp.get("items", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                return items

    def insert_task(self, list_id: str, body: dict) -> dict:
        return self._execute(self._service.tasks().insert(tasklist=list_id, body=body))

    def patch_task(self, list_id: str, gtask_id: str, body: dict) -> dict:
        return self._execute(
            self._service.tasks().patch(tasklist=list_id, task=gtask_id, body=body)
        )

    def delete_task(self, list_id: str, gtask_id: str) -> None:
        from googleapiclient.errors import HttpError

        try:
            self._service.tasks().delete(tasklist=list_id, task=gtask_id).execute()
        except HttpError as e:
            status = getattr(getattr(e, "resp", None), "status", None)
            if status == 404:
                return  # 既に無い＝削除済み扱い（冪等）
            raise GTasksApiError(f"Tasks API の削除に失敗しました（HTTP {status}）")
        except Exception as e:
            raise GTasksApiError(f"Tasks API との通信に失敗しました（{e}）")

    # -------------------------------------------------- calendar（ゴミカレンダー）

    def list_gomi_events(
        self, calendar_id: str, start_date: date, end_date: date
    ) -> list[dict]:
        """期間 [start_date, end_date]（両端含む）の終日イベントを返す。

        calendar v3 events.list（singleEvents=true で繰り返しを展開）。
        終日イベント（start.date を持つもの）のみ対象とし、
        各 {"date": "YYYY-MM-DD", "summary": str} に正規化する。
        スコープ不足（RefreshError / HTTP 401・403）は GTasksAuthError
        （再認証案内）、その他の失敗は GTasksApiError。
        """
        # v3 時代の tasks のみの token はネットワークを叩く前に再認証案内で
        # 劣化させる（呼び出し側が warnings に集約する）。token に scopes の
        # 記録が無い場合はここでは判定できないため API の 401/403 に任せる。
        scopes = set(self._creds.scopes or [])
        if scopes and CALENDAR_SCOPE not in scopes:
            raise GTasksAuthError(CALENDAR_REAUTH_HINT)
        if self._calendar_service is None:
            from googleapiclient.discovery import build

            self._calendar_service = build(
                "calendar", "v3", credentials=self._creds, cache_discovery=False
            )
        from google.auth.exceptions import RefreshError
        from googleapiclient.errors import HttpError

        time_min = datetime.combine(start_date, time.min, tzinfo=JST).isoformat()
        time_max = datetime.combine(
            end_date + timedelta(days=1), time.min, tzinfo=JST
        ).isoformat()
        items: list[dict] = []
        page_token = None
        while True:
            try:
                resp = (
                    self._calendar_service.events()
                    .list(
                        calendarId=calendar_id,
                        timeMin=time_min,
                        timeMax=time_max,
                        singleEvents=True,
                        orderBy="startTime",
                        maxResults=250,
                        pageToken=page_token,
                    )
                    .execute()
                )
            except RefreshError:
                # 既存 token がスコープ不足（calendar.readonly 未同意）だと
                # refresh が invalid_scope で落ちる。再認証で解消する。
                raise GTasksAuthError(CALENDAR_REAUTH_HINT)
            except HttpError as e:
                status = getattr(getattr(e, "resp", None), "status", None)
                # 403 でも accessNotConfigured は認証の問題ではなく、
                # Cloud プロジェクト側で Calendar API が未有効なだけ。
                # 再認証を案内すると誤誘導になるため区別する。
                if status == 403 and "accessNotConfigured" in str(e):
                    raise GTasksApiError(
                        "Google Calendar API が Cloud プロジェクトで未有効です。"
                        "https://console.cloud.google.com/apis/library/calendar-json.googleapis.com"
                        " から有効化してください"
                    )
                if status in (401, 403):
                    raise GTasksAuthError(CALENDAR_REAUTH_HINT)
                raise GTasksApiError(
                    f"Calendar API 呼び出しに失敗しました（HTTP {status}）"
                )
            except GTasksError:
                raise
            except Exception as e:
                raise GTasksApiError(f"Calendar API との通信に失敗しました（{e}）")
            items.extend(resp.get("items", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        events: list[dict] = []
        for ev in items:
            d = (ev.get("start") or {}).get("date")  # 終日イベントのみ date を持つ
            summary = (ev.get("summary") or "").strip()
            if d and summary:
                events.append({"date": d, "summary": summary})
        return events


def build_client() -> GTasksClient:
    """実クライアントを生成する。API 層はこれ経由で呼ぶ（テストで差し替え可能）。"""
    return GTasksClient()


# ---------------------------------------------------------------- add-task

def memory_io_script_path() -> Path:
    """agent_memory_io.py のパス。環境変数 KAJIFLOW_MEMORY_IO を優先する。"""
    env = os.environ.get("KAJIFLOW_MEMORY_IO")
    if env:
        return Path(env)
    home = os.environ.get("USERPROFILE") or str(Path.home())
    return (
        Path(home) / "OneDrive" / "ドキュメント" / "KnowledgeBase"
        / "95_System" / "scripts" / "agent_memory_io.py"
    )


def _sanitize_title(title: str | None) -> str:
    """Google 側タイトルを Vault 正本の1行タイトルとして安全な形に正規化する。

    Tasks API 経由では改行入りタイトルを設定できるため、そのまま追記すると
    正本に任意の Markdown 行（偽の `- [x]` 行や偽ハンドオフメタ等）を注入
    できてしまう。改行・制御文字を空白1つに畳む（vault.title_uid の正規化と
    同じ空白圧縮なので、追記後に再パースしても uid は一致する）。
    """
    t = re.sub(r"[\x00-\x1f\x7f]", " ", title or "")
    return re.sub(r"\s+", " ", t).strip()


def add_task_to_vault(title: str, due: str | None = None) -> None:
    """Google 側で直接追加されたタスクを Vault 正本へ追記する。

    agent_memory_io.py add-task を subprocess で呼ぶ（Vault 正本への追記の
    唯一の正規手段）。失敗は GTasksError（呼び出し側で warnings に集約）。
    """
    script = memory_io_script_path()
    title = _sanitize_title(title)
    if not title:
        raise GTasksError("タイトルが空のため Vault へ追記できません")
    cmd = [sys.executable, str(script), "add-task", "--source", "Google Tasks"]
    if due:
        cmd += ["--due", due]
    # argparse は `--` 以降を positional として扱うため、`-` で始まる
    # タイトル（例 "--due=..."）も option と誤解釈されずに渡せる。
    cmd += ["--", title]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GTasksError(f"agent_memory_io.py の実行に失敗しました（{e}）")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = detail[-1] if detail else ""
        raise GTasksError(
            f"agent_memory_io.py add-task が失敗しました（exit {proc.returncode}）{tail}"
        )


# ---------------------------------------------------------------- helpers

def _due_rfc3339(due_text: str | None) -> str | None:
    """期限テキストから RFC3339（Tasks API の due 形式）を作る。日付が無ければ None。"""
    if not due_text:
        return None
    m = DATE_RE.search(str(due_text))
    if not m:
        return None
    return f"{m.group(0)}T00:00:00.000Z"


def _date_from_due(due: str | None) -> str | None:
    """Google の due（RFC3339）から 'YYYY-MM-DD' を取り出す。"""
    if not due:
        return None
    m = DATE_RE.match(due)
    return m.group(0) if m else None


def _extract_uid(notes: str | None) -> str | None:
    m = UID_LINE_RE.search(notes or "")
    return m.group(1) if m else None


def build_vault_notes(task: dict) -> str:
    """vault タスクの Google Tasks notes（uid マーカー + 目的/完了条件 + 固定文言）。"""
    lines = [f"uid: {task['uid']}"]
    if task.get("purpose"):
        lines.append(f"目的: {task['purpose']}")
    if task.get("done_when"):
        lines.append(f"完了条件: {task['done_when']}")
    lines.append(SYNC_MARK)
    return "\n".join(lines)


def _ensure_list(client, title: str) -> str:
    """タイトル一致のリスト id を返す。無ければ作成する。"""
    for tl in client.list_tasklists():
        if tl.get("title") == title:
            return tl["id"]
    return client.insert_tasklist(title)["id"]


def _get_links(conn: sqlite3.Connection, kind: str) -> dict[str, sqlite3.Row]:
    rows = conn.execute(
        "SELECT * FROM gtasks_links WHERE kind = ?", (kind,)
    ).fetchall()
    return {row["local_id"]: row for row in rows}


def _save_link(
    conn: sqlite3.Connection, kind: str, local_id: str,
    gtask_id: str, list_id: str, now_iso: str,
) -> None:
    conn.execute(
        "INSERT INTO gtasks_links (kind, local_id, gtask_id, list_id, synced_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(kind, local_id) DO UPDATE SET "
        "gtask_id = excluded.gtask_id, list_id = excluded.list_id, "
        "synced_at = excluded.synced_at",
        (kind, local_id, gtask_id, list_id, now_iso),
    )


def _delete_link(conn: sqlite3.Connection, kind: str, local_id: str) -> None:
    conn.execute(
        "DELETE FROM gtasks_links WHERE kind = ? AND local_id = ?", (kind, local_id)
    )


# ---------------------------------------------------------------- sync: gomi calendar

def _sync_gomi_events(conn: sqlite3.Connection, client, now: datetime, result: dict) -> None:
    """SPEC「ゴミカレンダー連動（v4）」: gomi_events 洗い替え + 合成タスク upsert。

    - settings の gcal_gomi_calendar_id が空なら機能全体を無効（何もしない）。
    - 対象カレンダーの [今日, 今日+7日] の終日イベントを取得し、この期間の行を
      洗い替える（期間外の過去行も削除）。
    - 取得失敗（スコープ不足の GTasksAuthError 含む）は warnings に集約して
      同期継続。家事・Vault 同期は従来どおり動く。
    - 各 summary について「ゴミ出し: {summary}」の task 行を upsert
      （category='ゴミ', est_minutes=5, schedule_type='calendar',
      adaptive=0, enabled=1）。既存同名はそのまま。イベントに現れなくなっても
      task 行は消さない（プランに出なくなるだけ）。
    """
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (GOMI_CALENDAR_SETTING,)
    ).fetchone()
    calendar_id = (row["value"] or "").strip() if row else ""
    if not calendar_id:
        return

    today = now.astimezone(JST).date()
    end = today + timedelta(days=GOMI_WINDOW_DAYS)
    try:
        events = client.list_gomi_events(calendar_id, today, end)
    except GTasksError as e:
        result["warnings"].append(f"ゴミカレンダーの取得に失敗しました: {e}")
        return

    # 洗い替え: 取得期間の行を入れ替え、期間外の過去行も削除する。
    # （窓は常に [今日, 今日+7日] なので end より先の行は存在しない）
    conn.execute("DELETE FROM gomi_events WHERE date <= ?", (end.isoformat(),))
    for ev in events:
        conn.execute(
            "INSERT OR IGNORE INTO gomi_events (date, summary) VALUES (?, ?)",
            (ev["date"], ev["summary"]),
        )

    # 合成タスクの upsert（既存同名の schedule_type='calendar' はそのまま）
    for summary in sorted({ev["summary"] for ev in events}):
        name = f"{GOMI_TASK_PREFIX}{summary}"
        exists = conn.execute(
            "SELECT 1 FROM tasks WHERE name = ? AND schedule_type = 'calendar' LIMIT 1",
            (name,),
        ).fetchone()
        if exists:
            continue
        conn.execute(
            "INSERT INTO tasks (name, category, est_minutes, schedule_type, "
            "interval_days, weekdays, adaptive, enabled, notes, created_at) "
            "VALUES (?, ?, ?, 'calendar', NULL, NULL, 0, 1, '', ?)",
            (name, GOMI_CATEGORY, GOMI_EST_MINUTES,
             now.astimezone(JST).isoformat()),
        )
    conn.commit()


# ---------------------------------------------------------------- sync: vault

def _sync_vault_tasks(
    conn: sqlite3.Connection, client, now: datetime,
    list_id: str, gmap: dict[str, dict], result: dict,
) -> None:
    """SPEC「Vault タスク（kind='vault'）」1〜4。

    実行順は 1(push) → 2(pull 完了) → 4(Vault 側完了/消滅の反映) → 3(pull 新規)。
    3 を最後に回すのは、3 で作った直後のリンク（vault 再同期前で vault_tasks に
    まだ無い uid）を 4 の消滅判定が誤って削除しないため。
    """
    warnings: list[str] = result["warnings"]
    now_iso = now.astimezone(JST).isoformat()

    # --- 1. push: 未完了・非ハンドオフを「Vault タスク」リストへ upsert
    open_tasks = vault.list_tasks(conn)  # 直近同期で見えている未完了のみ
    links = _get_links(conn, "vault")
    for t in open_tasks:
        if t["is_handoff"]:
            continue  # ハンドオフチケットは Google Tasks に出さない
        desired: dict = {"title": t["title"], "notes": build_vault_notes(t)}
        due = _due_rfc3339(t.get("due"))
        if due:
            desired["due"] = due
        link = links.get(t["uid"])
        g = gmap.get(link["gtask_id"]) if link else None
        if g is None:
            # 自己修復: リンクが無い/壊れていても、Google 側に同じ uid マーカー
            # 付きの既存タスクがあればそれへ再リンクする（pull 新規の副作用後・
            # commit 前に強制終了した場合等に insert を二重実行しないため）。
            g = next(
                (
                    x for x in gmap.values()
                    if _extract_uid(x.get("notes")) == t["uid"]
                ),
                None,
            )
            if g is not None:
                _save_link(conn, "vault", t["uid"], g["id"], list_id, now_iso)
                conn.commit()
        try:
            if g is None:
                created = client.insert_task(
                    list_id, dict(desired, status="needsAction")
                )
                gmap[created["id"]] = created
                _save_link(conn, "vault", t["uid"], created["id"], list_id, now_iso)
                conn.commit()  # 項目単位で確定（冪等なので途中終了しても収束する）
                result["pushed"] += 1
            else:
                # タイトル等は kajiflow→Google の一方向上書き。
                # status には触れない（Google 側の完了は 2 の pull が扱う）。
                patch = {k: v for k, v in desired.items() if g.get(k) != v}
                if "due" not in desired and g.get("due"):
                    patch["due"] = None
                if patch:
                    client.patch_task(list_id, g["id"], patch)
                    g.update(patch)
                    _save_link(conn, "vault", t["uid"], g["id"], list_id, now_iso)
                    conn.commit()
                    result["pushed"] += 1
        except GTasksError as e:
            warnings.append(f"Vault タスクの push に失敗しました（{t['title']}）: {e}")

    # --- 2. pull 完了: Google 側 completed → Vault 書き戻し（安全規則そのまま）
    for uid, link in _get_links(conn, "vault").items():
        g = gmap.get(link["gtask_id"])
        if g is None or g.get("status") != "completed":
            continue
        row = conn.execute(
            "SELECT title, checked FROM vault_tasks WHERE uid = ?", (uid,)
        ).fetchone()
        if row is None or row["checked"]:
            continue
        try:
            vault.complete_in_vault(conn, uid)
            result["completed_in_vault"] += 1
        except vault.VaultError as e:
            # 409（曖昧・ハンドオフ化・消滅）等は書き込まず warnings へ。
            # Google 側は completed のまま放置する。
            warnings.append(
                f"Vault への完了書き戻しを中断しました（{row['title']}）: {e}"
            )

    # --- 4. Vault 側で完了/消滅 → Google を completed / delete
    listed = vault.list_tasks(conn, include_done=True)
    checked_by_uid = {t["uid"]: t["checked"] for t in listed}
    for uid, link in _get_links(conn, "vault").items():
        g = gmap.get(link["gtask_id"])
        checked = checked_by_uid.get(uid)
        try:
            if checked is None:
                # 消滅（正本ファイルから消えた / ハンドオフ化で uid が変わった）
                if g is not None:
                    client.delete_task(link["list_id"], link["gtask_id"])
                    gmap.pop(link["gtask_id"], None)
                _delete_link(conn, "vault", uid)
                conn.commit()
            elif checked and g is not None and g.get("status") != "completed":
                client.patch_task(link["list_id"], g["id"], {"status": "completed"})
                g["status"] = "completed"
        except GTasksError as e:
            warnings.append(f"Google Tasks 側の反映に失敗しました（{uid}）: {e}")

    # --- 3. pull 新規: notes に uid が無いタスク → agent_memory_io.py add-task
    links_now = _get_links(conn, "vault")
    linked_gids = {link["gtask_id"] for link in links_now.values()}
    # タイトル衝突の検出用: 最新同期で見えている全 uid（4 の listed 由来。
    # 完了済み含む）+ 既存リンクの uid。ここに既にある uid のタイトルを
    # 追記すると正本に同一タイトル行が2本でき、完了書き戻しの完全一致
    # 判定（安全規則2）が常に 409 になるため、追記もリンク上書きもしない。
    known_uids = set(checked_by_uid) | set(links_now)
    for g in list(gmap.values()):
        if g["id"] in linked_gids:
            continue
        if _extract_uid(g.get("notes")):
            continue  # KajiFlow 管理済みマーカーあり
        if g.get("status") == "completed" or g.get("deleted"):
            continue
        title = _sanitize_title(g.get("title"))
        if not title:
            continue
        uid = vault.title_uid(title)
        if uid in known_uids:
            warnings.append(
                f"Google 側の新規タスクをスキップしました（{title}）: "
                "同名のタスクが既に Vault にあります。"
                "Google Tasks 側の重複タスクを削除してください"
            )
            continue
        try:
            add_task_to_vault(title, due=_date_from_due(g.get("due")))
        except GTasksError as e:
            warnings.append(f"Vault への新規タスク追記に失敗しました（{title}）: {e}")
            continue
        # 再実行で二重追記しないよう、uid マーカーを付けてリンクしておく。
        # （vault 再同期後の次回 push で title-hash uid が一致し、内容が収束する）
        try:
            client.patch_task(
                list_id, g["id"], {"notes": f"uid: {uid}\n{SYNC_MARK}"}
            )
        except GTasksError as e:
            warnings.append(
                f"Google Tasks 側への uid 記録に失敗しました（{title}）: {e}"
            )
        _save_link(conn, "vault", uid, g["id"], list_id, now_iso)
        # Vault への追記は既に済んでいるため、リンクも即確定して
        # 「追記済みなのにリンク未保存」の窓を最小化する。
        conn.commit()
        known_uids.add(uid)
        result["new_from_google"] += 1


# ---------------------------------------------------------------- sync: chores

def _today_statuses(conn: sqlite3.Connection, date_str: str) -> dict[int, str]:
    """当日の completions から task_id -> 'done' | 'skip'（done 優先）。"""
    rows = conn.execute(
        "SELECT task_id, action FROM completions "
        "WHERE substr(completed_at, 1, 10) = ?",
        (date_str,),
    ).fetchall()
    statuses: dict[int, str] = {}
    for r in rows:
        if r["action"] not in ("done", "skip"):
            continue  # 'undo'（取り消しの墓標）は状態に影響しない
        if statuses.get(r["task_id"]) != "done":
            statuses[r["task_id"]] = r["action"]
    return statuses


def _undo_time(conn: sqlite3.Connection, date_str: str, task_id: int):
    """当日の取り消し（action='undo'）の最新時刻を aware datetime で返す。無ければ None。"""
    row = conn.execute(
        "SELECT MAX(completed_at) AS t FROM completions "
        "WHERE task_id = ? AND action = 'undo' AND substr(completed_at, 1, 10) = ?",
        (task_id, date_str),
    ).fetchone()
    if row is None or row["t"] is None:
        return None
    return datetime.fromisoformat(row["t"])


def _parse_google_ts(value: str | None):
    """Google の RFC3339（末尾 Z）を aware datetime にする。無効なら None。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def revert_chore_on_google(conn: sqlite3.Connection, task_id: int, d) -> None:
    """完了取り消し時の即時リバート（best effort）。

    当日 chore リンクがあれば Google 側タスクを needsAction に戻す。
    失敗しても墓標（undo）ベースの次回同期で収束するため、例外は握りつぶす。
    """
    try:
        link = conn.execute(
            "SELECT gtask_id, list_id FROM gtasks_links WHERE kind = 'chore' AND local_id = ?",
            (f"{d.isoformat()}:{task_id}",),
        ).fetchone()
        if link is None:
            return
        client = build_client()
        client.patch_task(
            link["list_id"], link["gtask_id"],
            {"status": "needsAction", "completed": None},
        )
    except Exception:
        pass


def _sync_chores(
    conn: sqlite3.Connection, client, now: datetime,
    list_id: str, gmap: dict[str, dict], result: dict,
) -> None:
    """SPEC「家事（kind='chore'）」1〜2。リストは常に「今日」を映す。"""
    warnings: list[str] = result["warnings"]
    local_now = now.astimezone(JST)
    today = local_now.date().isoformat()
    now_iso = local_now.isoformat()
    prefix = today + ":"

    # --- 1. push（前段）: 前日以前の chore リンクは Google 側を削除
    for local_id, link in _get_links(conn, "chore").items():
        if local_id.startswith(prefix):
            continue
        try:
            client.delete_task(link["list_id"], link["gtask_id"])
        except GTasksError as e:
            warnings.append(f"前日分の家事タスク削除に失敗しました（{local_id}）: {e}")
            continue  # リンクを残して次回再試行
        gmap.pop(link["gtask_id"], None)
        _delete_link(conn, "chore", local_id)
        conn.commit()

    # --- 1. push: 当日プランを upsert（pending は due=当日、完了/スキップは completed）
    row = conn.execute(
        "SELECT task_ids FROM daily_plans WHERE date = ?", (today,)
    ).fetchone()
    plan: list[int] = json.loads(row["task_ids"]) if row else []
    statuses = _today_statuses(conn, today)
    links = _get_links(conn, "chore")
    due = f"{today}T00:00:00.000Z"
    for tid in plan:
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
        if task is None:
            continue  # プラン凍結後に削除されたタスク
        local_id = f"{today}:{tid}"
        link = links.get(local_id)
        g = gmap.get(link["gtask_id"]) if link else None
        status = statuses.get(tid)
        try:
            if status is None:  # pending
                # 取り消し済みなのに Google 側が古い completed のまま → needsAction へ戻す。
                # 取り消し後にスマホで改めて完了した場合（completed が墓標より新しい）は
                # 戻さず、pull が通常どおり記録する。
                if g is not None and g.get("status") == "completed":
                    undo = _undo_time(conn, today, tid)
                    g_completed = _parse_google_ts(g.get("completed"))
                    if undo is not None and (g_completed is None or g_completed <= undo):
                        client.patch_task(
                            list_id, g["id"], {"status": "needsAction", "completed": None}
                        )
                        g["status"] = "needsAction"
                        g.pop("completed", None)
                        result["pushed"] += 1
                desired = {"title": task["name"], "notes": SYNC_MARK, "due": due}
                if g is None:
                    created = client.insert_task(
                        list_id, dict(desired, status="needsAction")
                    )
                    gmap[created["id"]] = created
                    _save_link(conn, "chore", local_id, created["id"], list_id, now_iso)
                    conn.commit()  # 項目単位で確定（冪等なので途中終了しても収束する）
                    result["pushed"] += 1
                else:
                    patch = {k: v for k, v in desired.items() if g.get(k) != v}
                    if patch:
                        client.patch_task(list_id, g["id"], patch)
                        g.update(patch)
                        result["pushed"] += 1
            else:  # done / skip → Google 側を completed に
                if g is not None and g.get("status") != "completed":
                    client.patch_task(list_id, g["id"], {"status": "completed"})
                    g["status"] = "completed"
                    result["pushed"] += 1
        except GTasksError as e:
            warnings.append(f"家事タスクの push に失敗しました（{task['name']}）: {e}")

    # --- 2. pull: Google 側 completed になった当日 pending → action='done' で記録
    for local_id, link in _get_links(conn, "chore").items():
        if not local_id.startswith(prefix):
            continue
        g = gmap.get(link["gtask_id"])
        if g is None or g.get("status") != "completed":
            continue
        tid = int(local_id.split(":", 1)[1])
        if statuses.get(tid) is not None:
            continue  # 既に done/skip 記録済み
        # 取り消し墓標より古い completed は「取り消し済みの残骸」なので記録しない
        # （push フェーズの needsAction 戻しが失敗した場合の二重防御）。
        undo = _undo_time(conn, today, tid)
        if undo is not None:
            g_completed = _parse_google_ts(g.get("completed"))
            if g_completed is None or g_completed <= undo:
                continue
        # statuses はループ前のスナップショットのため、UI の complete と同時実行
        # されると二重記録になりうる。BEGIN IMMEDIATE で書き込みロックを取り、
        # 当日記録の有無を確認してから挿入する（_record_action と同じ規則）。
        day_start = datetime.combine(local_now.date(), time.min, tzinfo=JST).isoformat()
        day_end = datetime.combine(
            local_now.date() + timedelta(days=1), time.min, tzinfo=JST
        ).isoformat()
        conn.execute("BEGIN IMMEDIATE")
        try:
            dup = conn.execute(
                "SELECT 1 FROM completions WHERE task_id = ? "
                "AND action IN ('done', 'skip') "
                "AND completed_at >= ? AND completed_at < ? LIMIT 1",
                (tid, day_start, day_end),
            ).fetchone()
            if dup is None:
                conn.execute(
                    "INSERT INTO completions (task_id, completed_at, action) "
                    "VALUES (?, ?, 'done')",
                    (tid, now_iso),
                )
                result["completed_chores"] += 1
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        statuses[tid] = "done"


# ---------------------------------------------------------------- sync_all

def sync_all(conn: sqlite3.Connection, client, now: datetime) -> dict:
    """全体調停。冒頭のゴミカレンダー連動 + vault 4項目 + chore 2項目を実行して結果を返す。

    - client は GTasksClient と同じメソッド契約を持つオブジェクト（注入可能）。
    - now は aware datetime（「今日」は Asia/Tokyo で判定）。
    - 冪等: 同一入力で2回実行しても Google / DB / Vault に差分を生まない。
    - 項目単位の失敗は warnings（日本語）に集約し、同期全体は落とさない。
      リスト解決などの前提が満たせない場合は GTasksError、認証切れは
      GTasksAuthError を送出する。
    - 当日プラン（daily_plans）は生成しない。呼び出し側（API 層）が
      ensure_today_plan 済みであることを前提とする。

    戻り値: {pushed, completed_in_vault, completed_chores, new_from_google,
             warnings: [str, ...]}
    """
    result: dict = {
        "pushed": 0,
        "completed_in_vault": 0,
        "completed_chores": 0,
        "new_from_google": 0,
        "warnings": [],
    }

    # ゴミカレンダー連動（v4）: gomi_events 洗い替え + 合成タスク upsert。
    # スコープ不足・取得失敗は warnings に集約して以降の同期を継続する。
    _sync_gomi_events(conn, client, now, result)

    # まず Vault 正本を DB に反映する（前回 pull 新規で追記した分の uid も
    # ここで vault_tasks に載り、push でリンク済みタスクとして収束する）。
    try:
        vault.sync_vault(conn)
    except vault.VaultReadError as e:
        result["warnings"].append(
            f"Vault 同期をスキップしました（キャッシュ済み DB で続行）: {e}"
        )

    vault_list_id = _ensure_list(client, VAULT_LIST_TITLE)
    chore_list_id = _ensure_list(client, CHORE_LIST_TITLE)
    vault_gmap = {
        t["id"]: t for t in client.list_tasks(vault_list_id) if not t.get("deleted")
    }
    chore_gmap = {
        t["id"]: t for t in client.list_tasks(chore_list_id) if not t.get("deleted")
    }

    _sync_vault_tasks(conn, client, now, vault_list_id, vault_gmap, result)
    _sync_chores(conn, client, now, chore_list_id, chore_gmap, result)
    conn.commit()
    return result
