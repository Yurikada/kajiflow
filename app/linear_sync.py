"""Linear-owned tasks mirrored through KajiFlow's existing Google credentials.

Google completion is a request, never proof of Linear acceptance. No deletions.
The caller must hold the same process lock as the existing Google sync endpoint.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from . import gtasks

CONFIG = gtasks.DATA_DIR / "linear-config.json"
TERMINAL = {"completed", "canceled", "duplicate"}
STATES = TERMINAL | {"backlog", "unstarted", "started"}


def initialize(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS linear_mirrors (
        uuid TEXT PRIMARY KEY, issue_json TEXT NOT NULL, list_id TEXT,
        task_id TEXT, source_state TEXT, completion_requested INTEGER NOT NULL DEFAULT 0,
        last_seen TEXT NOT NULL)""")
    conn.commit()


def validate(payload, config, now):
    if payload.get("complete") is not True or payload.get("schema") != "linear-portfolio/v1":
        raise ValueError("完全なLinearスナップショットが必要です")
    if payload.get("workspace", {}).get("id") != config["workspace_id"]:
        raise ValueError("Linear workspaceが一致しません")
    fetched = datetime.fromisoformat(payload["fetched_at"].replace("Z", "+00:00"))
    if fetched.tzinfo is None or not -300 <= (now - fetched).total_seconds() <= 900:
        raise ValueError("Linearの最新スナップショットを取得してください")
    projects = {p.get("uuid", p.get("id")) for p in payload["projects"]}
    if projects != set(config["project_ids"]):
        raise ValueError("Linear project範囲が一致しません")
    seen = set()
    for issue in payload["issues"]:
        uid = str(UUID(issue["uuid"]))
        if uid != issue["uuid"] or uid in seen or issue["projectId"] not in projects:
            raise ValueError("Linear issueのIDまたはprojectが不正です")
        seen.add(uid)
        if issue.get("statusType") not in STATES or not isinstance(issue.get("title"), str) or not issue["title"]:
            raise ValueError("Linear issueの状態またはタイトルが不正です")
        if not issue.get("url", "").startswith(config["workspace_url"] + "/issue/"):
            raise ValueError("Linear issue URLが一致しません")
        if issue.get("dueDate"):
            datetime.strptime(issue["dueDate"], "%Y-%m-%d")
    return fetched.isoformat()


def tasks(conn):
    initialize(conn)
    return [dict(json.loads(r["issue_json"]), completion_requested=bool(r["completion_requested"]),
                 last_seen=r["last_seen"]) for r in conn.execute("SELECT * FROM linear_mirrors ORDER BY uuid")]


def sync(conn, client, payload, config, now=None):
    now = now or datetime.now(timezone.utc)
    fetched = validate(payload, config, now)  # all validation before any remote mutation
    initialize(conn)
    latest = conn.execute("SELECT MAX(last_seen) FROM linear_mirrors").fetchone()[0]
    if latest and datetime.fromisoformat(latest) > datetime.fromisoformat(fetched):
        raise ValueError("古いLinearスナップショットへの巻き戻しを拒否しました")
    matches = [x for x in client.list_tasklists() if x.get("title") == config["list_title"]]
    if len(matches) > 1:
        raise ValueError("同名のLinear専用リストが複数あります")
    target = matches[0] if matches else client.insert_tasklist(config["list_title"])
    lid = target["id"]
    remote = [t for t in client.list_tasks(lid) if not t.get("deleted")]
    by_id = {t["id"]: t for t in remote}
    result = {"pushed": 0, "new_completion_requests": [], "warnings": [], "fetched_at": fetched}
    for issue in payload["issues"]:
        uid = issue["uuid"]
        marker = "linear-issue: " + uid
        old = conn.execute("SELECT * FROM linear_mirrors WHERE uuid=?", (uid,)).fetchone()
        conn.execute("""INSERT INTO linear_mirrors(uuid,issue_json,last_seen) VALUES(?,?,?)
            ON CONFLICT(uuid) DO UPDATE SET issue_json=excluded.issue_json,last_seen=excluded.last_seen""",
            (uid, json.dumps(issue, ensure_ascii=False), fetched))
        conn.commit()
        g = by_id.get(old["task_id"]) if old and old["list_id"] == lid else None
        if g is None:
            candidates = [t for t in remote if (t.get("notes") or "").splitlines()[:1] == [marker]]
            if len(candidates) > 1:
                result["warnings"].append(f"{issue['id']}: Google側に同じUUIDが複数あります")
                continue
            g = candidates[0] if candidates else None
        terminal = issue["statusType"] in TERMINAL
        requested = bool(g and g.get("status") == "completed" and not terminal)
        # A known Linear terminal -> open transition explicitly reopens the mirror.
        reopening = bool(old and old["source_state"] in TERMINAL and not terminal)
        if reopening:
            requested = False
        new_request = requested and not (old and old["completion_requested"])
        blockers = [x.get("id", "?") for x in (issue.get("relations") or {}).get("blockedBy", [])]
        notes = "\n".join([marker, issue["url"], f"Project: {issue.get('project', issue['projectId'])}",
            f"Linear: {issue.get('status', issue['statusType'])}", "先行タスク: " + (", ".join(blockers) or "なし"),
            "完了チェックは完了申告として記録します。LinearのDoneは確認後に変更してください。",
            (issue.get("description") or "")[:5000]])
        desired = {"title": f"[{issue['id']}] {issue['title']}"[:1024], "notes": notes,
                   "due": issue["dueDate"] + "T00:00:00.000Z" if issue.get("dueDate") else None}
        try:
            if g is None and terminal:
                pass  # never flood the list with historical completed issues
            elif g is None:
                g = client.insert_task(lid, {k: v for k, v in dict(desired, status="needsAction").items() if v is not None})
                result["pushed"] += 1
            else:
                patch = {k: v for k, v in desired.items() if g.get(k) != v}
                state = "completed" if terminal or requested else "needsAction"
                if g.get("status") != state:
                    patch["status"] = state
                if patch:
                    client.patch_task(lid, g["id"], patch)
                    result["pushed"] += 1
            conn.execute("""UPDATE linear_mirrors SET list_id=?,task_id=?,source_state=?,
                completion_requested=? WHERE uuid=?""",
                (lid, g["id"] if g else None, issue["statusType"], int(requested), uid))
            conn.commit()
            if new_request:
                result["new_completion_requests"].append(issue["id"])
        except gtasks.GTasksError as exc:
            result["warnings"].append(f"{issue['id']}: {exc}")
    result["completion_requests"] = [t["id"] for t in tasks(conn) if t["completion_requested"]]
    return result


def load_config():
    return json.loads(Path(CONFIG).read_text(encoding="utf-8-sig"))
