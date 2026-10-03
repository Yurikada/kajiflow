import copy
import sqlite3
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from app import linear_sync as mod
from app import gtasks
from test_gtasks import FakeGTasksClient


@pytest.fixture
def fixture():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    now = datetime.now(timezone.utc)
    config = {"workspace_id": "w", "project_ids": ["p"],
              "workspace_url": "https://linear.app/test", "list_title": "Linear test"}
    issue = {"uuid": str(uuid4()), "id": "T-1", "projectId": "p", "title": "one",
             "statusType": "unstarted", "url": "https://linear.app/test/issue/T-1",
             "dueDate": "2026-10-01"}
    payload = {"schema": "linear-portfolio/v1", "complete": True, "workspace": {"id": "w"},
               "projects": [{"uuid": "p"}], "issues": [issue], "fetched_at": now.isoformat()}
    return conn, FakeGTasksClient(), payload, config, now


def run(f):
    return mod.sync(*f)


def remote(f):
    return next(iter(f[1]._tasks.values()))


def test_isolated_idempotent_and_due_clear(fixture):
    f = fixture
    other = f[1].insert_tasklist("Vault タスク")
    f[1].insert_task(other["id"], {"title": "untouched"})
    assert run(f)["pushed"] == 1
    f[1].mutations.clear()
    assert run(f)["pushed"] == 0
    assert not f[1].mutations
    f[2]["issues"][0]["dueDate"] = None
    assert run(f)["pushed"] == 1
    assert f[1].list_tasks(other["id"])[0]["title"] == "untouched"


@pytest.mark.parametrize("terminal", ["completed", "canceled", "duplicate"])
def test_completion_request_and_reopen(fixture, terminal):
    f = fixture
    run(f)
    g = next(iter(remote(f).values()))
    g["status"] = "completed"
    assert run(f)["new_completion_requests"] == ["T-1"]
    assert mod.tasks(f[0])[0]["statusType"] == "unstarted"
    assert run(f)["new_completion_requests"] == []
    f[2]["issues"][0]["statusType"] = terminal
    assert run(f)["completion_requests"] == []
    f[2]["issues"][0]["statusType"] = "started"
    assert run(f)["completion_requests"] == []
    assert g["status"] == "needsAction"


def test_recover_after_insert_response_lost(fixture):
    f = fixture
    original = f[1].insert_task
    def lost(*args):
        original(*args)
        raise gtasks.GTasksApiError("response lost")
    f[1].insert_task = lost
    assert run(f)["warnings"]
    f[1].insert_task = original
    assert run(f)["pushed"] == 0
    assert len(remote(f)) == 1


@pytest.mark.parametrize("terminal", ["completed", "canceled", "duplicate"])
def test_missing_and_terminal_do_not_delete_or_create_history(fixture, terminal):
    f = fixture
    f[2]["issues"][0]["statusType"] = terminal
    assert run(f)["pushed"] == 0
    assert len(remote(f)) == 0
    f[2]["issues"][0]["statusType"] = "started"
    run(f)
    f[2]["issues"] = []
    run(f)
    assert len(remote(f)) == 1


def test_failed_patch_does_not_emit_unpersisted_completion_request(fixture):
    f = fixture
    run(f)
    g = next(iter(remote(f).values()))
    g["status"] = "completed"
    g["title"] = "phone edit"
    original = f[1].patch_task
    def failed(*args):
        raise gtasks.GTasksApiError("offline")
    f[1].patch_task = failed
    result = run(f)
    assert result["warnings"] and result["new_completion_requests"] == []
    f[1].patch_task = original
    assert run(f)["new_completion_requests"] == ["T-1"]
    assert run(f)["new_completion_requests"] == []


@pytest.mark.parametrize("bad", ["workspace", "scope", "partial", "stale", "duplicate", "url", "status"])
def test_invalid_snapshot_never_mutates_google(fixture, bad):
    f = fixture
    p = f[2]
    if bad == "workspace": p["workspace"]["id"] = "wrong"
    if bad == "scope": p["projects"] = []
    if bad == "partial": p["complete"] = False
    if bad == "stale": p["fetched_at"] = (f[4] - timedelta(hours=1)).isoformat()
    if bad == "duplicate": p["issues"].append(copy.deepcopy(p["issues"][0]))
    if bad == "url": p["issues"][0]["url"] = "https://example.com"
    if bad == "status": p["issues"][0]["statusType"] = "unknown"
    with pytest.raises(ValueError): run(f)
    assert not f[1].mutations


def test_api_lock_and_validation(client, monkeypatch, fixture):
    from app import main
    monkeypatch.setattr(mod, "load_config", lambda: fixture[3])
    monkeypatch.setattr(gtasks, "build_client", lambda: fixture[1])
    assert main._gtasks_sync_lock.acquire(blocking=False)
    try:
        assert client.post("/api/linear/sync", json=fixture[2]).status_code == 409
    finally:
        main._gtasks_sync_lock.release()
    assert client.post("/api/linear/sync", json={}).status_code == 422
    assert client.post("/api/linear/sync", json=fixture[2]).status_code == 200
    assert len(client.get("/api/linear/tasks").json()["tasks"]) == 1
