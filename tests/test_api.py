"""app/main.py の API テスト（SPEC「テスト」節: API）。

TestClient + 一時ディレクトリ DB（環境変数 KAJIFLOW_DB、conftest.py 参照）。

- next → complete → next の遷移
- skip
- CRUD（バリデーション含む）
- テンプレ適用
- regenerate（当日プランの凍結と作り直し）
- stats
- settings
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.engine import JST


def today_weekday() -> int:
    return datetime.now(JST).weekday()


def backdate_created(raw_conn, task_id: int, days: float) -> None:
    """created_at を過去に書き換えて interval タスクを「期限到来」にする。"""
    conn = raw_conn()
    past = (datetime.now(JST) - timedelta(days=days)).isoformat()
    conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (past, task_id))
    conn.commit()
    conn.close()


def create_task(client, **overrides) -> dict:
    payload = {
        "name": "テストタスク",
        "category": "その他",
        "est_minutes": 10,
        "schedule_type": "interval",
        "interval_days": 3,
        "adaptive": 0,
    }
    payload.update(overrides)
    res = client.post("/api/tasks", json=payload)
    assert res.status_code == 201, res.text
    return res.json()


# ---------------------------------------------------------------- health / 初期状態

class TestBasics:
    def test_health(self, client):
        res = client.get("/health")
        assert res.status_code == 200
        assert res.json() == {"status": "ok"}

    def test_next_with_empty_plan(self, client):
        # タスクゼロ → 「今日はなし」（task: null）が正当な結果
        res = client.get("/api/next")
        assert res.status_code == 200
        body = res.json()
        assert body["task"] is None
        assert body["done_count"] == 0
        assert body["total_count"] == 0

    def test_db_connections_use_wal(self, db_path):
        # gtasks 同期のような長めの書き込み中でも他 API の読み取りが
        # ブロックされないよう、接続は WAL モードで開く
        from app import db as dbmod

        conn = dbmod.connect(db_path)
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
        assert mode == "wal"

    def test_today_with_empty_plan(self, client):
        res = client.get("/api/today")
        assert res.status_code == 200
        body = res.json()
        assert body["items"] == []
        assert body["date"] == datetime.now(JST).date().isoformat()


# ---------------------------------------------------------------- CRUD

class TestTaskCrud:
    def test_create_and_get(self, client):
        task = create_task(client, name="風呂掃除", category="水回り")
        assert task["id"] > 0
        assert task["name"] == "風呂掃除"
        assert task["category"] == "水回り"

        res = client.get(f"/api/tasks/{task['id']}")
        assert res.status_code == 200
        assert res.json()["name"] == "風呂掃除"

    def test_list(self, client):
        create_task(client, name="A")
        create_task(client, name="B")
        res = client.get("/api/tasks")
        assert res.status_code == 200
        names = [t["name"] for t in res.json()]
        assert names == ["A", "B"]

    def test_partial_update(self, client):
        task = create_task(client, name="旧名", est_minutes=10)
        res = client.put(f"/api/tasks/{task['id']}", json={"name": "新名"})
        assert res.status_code == 200
        updated = res.json()
        assert updated["name"] == "新名"
        assert updated["est_minutes"] == 10  # 未指定フィールドは維持

    def test_toggle_enabled(self, client):
        task = create_task(client)
        res = client.put(f"/api/tasks/{task['id']}", json={"enabled": 0})
        assert res.status_code == 200
        assert res.json()["enabled"] == 0

    def test_delete(self, client):
        task = create_task(client)
        res = client.delete(f"/api/tasks/{task['id']}")
        assert res.status_code == 200
        assert client.get(f"/api/tasks/{task['id']}").status_code == 404

    def test_get_missing_returns_404(self, client):
        assert client.get("/api/tasks/9999").status_code == 404
        assert client.put("/api/tasks/9999", json={"name": "x"}).status_code == 404
        assert client.delete("/api/tasks/9999").status_code == 404

    def test_validation_empty_name(self, client):
        res = client.post("/api/tasks", json={
            "name": "  ", "schedule_type": "interval", "interval_days": 3,
        })
        assert res.status_code == 422

    def test_validation_interval_requires_interval_days(self, client):
        res = client.post("/api/tasks", json={
            "name": "x", "schedule_type": "interval",
        })
        assert res.status_code == 422

    def test_validation_weekly_requires_weekdays(self, client):
        res = client.post("/api/tasks", json={
            "name": "x", "schedule_type": "weekly",
        })
        assert res.status_code == 422

    def test_validation_bad_schedule_type(self, client):
        res = client.post("/api/tasks", json={
            "name": "x", "schedule_type": "monthly", "interval_days": 3,
        })
        assert res.status_code == 422


# ---------------------------------------------------------------- next → complete → next

class TestNextCompleteFlow:
    def _setup_two_due_tasks(self, client, raw_conn) -> tuple[dict, dict]:
        """期限到来済みの interval タスクを2件用意し、プランを作り直す。

        t1（10日経過, interval 3）の方が t2（5日経過, interval 3）より緊急。
        """
        t1 = create_task(client, name="タスク1", interval_days=3)
        t2 = create_task(client, name="タスク2", interval_days=3)
        backdate_created(raw_conn, t1["id"], 10)
        backdate_created(raw_conn, t2["id"], 5)
        res = client.post("/api/plan/regenerate")
        assert res.status_code == 200
        return t1, t2

    def test_next_complete_next_transition(self, client, raw_conn):
        t1, t2 = self._setup_two_due_tasks(client, raw_conn)

        res = client.get("/api/next")
        body = res.json()
        assert body["task"]["id"] == t1["id"]  # urgency 降順で t1 が先頭
        assert body["done_count"] == 0
        assert body["total_count"] == 2

        res = client.post(f"/api/tasks/{t1['id']}/complete")
        assert res.status_code == 200
        body = res.json()
        assert body["task"]["id"] == t2["id"]  # 次の1件が返る
        assert body["done_count"] == 1
        assert body["total_count"] == 2

        res = client.post(f"/api/tasks/{t2['id']}/complete")
        body = res.json()
        assert body["task"] is None  # 全部終わり
        assert body["done_count"] == 2
        assert body["total_count"] == 2

    def test_complete_resend_is_idempotent(self, client, raw_conn):
        """同日の complete 再送は completions を重複させない（EWMA 誤学習防止）。"""
        t1, t2 = self._setup_two_due_tasks(client, raw_conn)

        first = client.post(f"/api/tasks/{t1['id']}/complete").json()
        resend = client.post(f"/api/tasks/{t1['id']}/complete").json()
        assert resend["done_count"] == first["done_count"] == 1  # 増えない

        conn = raw_conn()
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM completions WHERE task_id = ?", (t1["id"],)
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 1  # 記録は1件のみ

    def test_skip_moves_to_next_without_done_count(self, client, raw_conn):
        t1, t2 = self._setup_two_due_tasks(client, raw_conn)

        res = client.post(f"/api/tasks/{t1['id']}/skip")
        assert res.status_code == 200
        body = res.json()
        assert body["task"]["id"] == t2["id"]  # skip でも次の1件へ進む
        assert body["done_count"] == 0        # skip は完了数に入らない
        assert body["total_count"] == 2

        # /api/today で skip 状態が見える
        items = client.get("/api/today").json()["items"]
        statuses = {i["task"]["id"]: i["status"] for i in items}
        assert statuses[t1["id"]] == "skip"
        assert statuses[t2["id"]] == "pending"

    def test_today_statuses(self, client, raw_conn):
        t1, t2 = self._setup_two_due_tasks(client, raw_conn)
        client.post(f"/api/tasks/{t1['id']}/complete")

        body = client.get("/api/today").json()
        statuses = {i["task"]["id"]: i["status"] for i in body["items"]}
        assert statuses == {t1["id"]: "done", t2["id"]: "pending"}

    def test_complete_missing_task_returns_404(self, client):
        assert client.post("/api/tasks/9999/complete").status_code == 404
        assert client.post("/api/tasks/9999/skip").status_code == 404

    def test_weekly_task_comes_first(self, client, raw_conn):
        ti = create_task(client, name="interval", interval_days=3)
        tw = create_task(
            client, name="weekly", schedule_type="weekly",
            interval_days=None, weekdays=str(today_weekday()),
        )
        backdate_created(raw_conn, ti["id"], 30)  # 非常に緊急にしても
        client.post("/api/plan/regenerate")
        body = client.get("/api/next").json()
        assert body["task"]["id"] == tw["id"]  # weekly が先頭


# ---------------------------------------------------------------- プラン凍結と regenerate

class TestPlanFreezeAndRegenerate:
    def test_plan_is_frozen_within_the_day(self, client, raw_conn):
        # 起動時に当日プラン（空）が凍結される。日中にタスクを追加しても
        # regenerate するまで反映されない（判断レス性）。
        assert client.get("/api/next").json()["total_count"] == 0

        task = create_task(client)
        backdate_created(raw_conn, task["id"], 10)

        body = client.get("/api/next").json()
        assert body["task"] is None
        assert body["total_count"] == 0  # まだ空のまま

    def test_regenerate_rebuilds_plan(self, client, raw_conn):
        task = create_task(client)
        backdate_created(raw_conn, task["id"], 10)

        res = client.post("/api/plan/regenerate")
        assert res.status_code == 200
        body = res.json()  # /api/next と同形
        assert body["task"]["id"] == task["id"]
        assert body["total_count"] == 1


# ---------------------------------------------------------------- テンプレ

class TestTemplates:
    def test_list_templates(self, client):
        res = client.get("/api/templates")
        assert res.status_code == 200
        templates = res.json()
        assert len(templates) >= 15
        names = [t["name"] for t in templates]
        assert "風呂掃除" in names

    def test_apply_templates(self, client):
        res = client.post("/api/templates/apply",
                          json={"names": ["風呂掃除", "洗濯"]})
        assert res.status_code == 201
        created = res.json()["created"]
        assert [t["name"] for t in created] == ["風呂掃除", "洗濯"]
        assert all(t["id"] > 0 for t in created)

        names = [t["name"] for t in client.get("/api/tasks").json()]
        assert names == ["風呂掃除", "洗濯"]

    def test_apply_unknown_template_returns_422(self, client):
        res = client.post("/api/templates/apply",
                          json={"names": ["存在しない家事"]})
        assert res.status_code == 422


# ---------------------------------------------------------------- stats

class TestStats:
    def test_weekly_stats_counts_done_and_skip(self, client, raw_conn):
        t1 = create_task(client, name="完了する家事")
        t2 = create_task(client, name="スキップする家事")
        backdate_created(raw_conn, t1["id"], 10)
        backdate_created(raw_conn, t2["id"], 10)
        client.post("/api/plan/regenerate")
        client.post(f"/api/tasks/{t1['id']}/complete")
        client.post(f"/api/tasks/{t2['id']}/skip")

        res = client.get("/api/stats/weekly", params={"weeks": 1})
        assert res.status_code == 200
        weeks = res.json()["weeks"]
        assert len(weeks) == 1
        wk = weeks[0]
        assert wk["done"] == 1
        assert wk["skip"] == 1
        by_name = {t["name"]: t for t in wk["tasks"]}
        assert by_name["完了する家事"]["done"] == 1
        assert by_name["スキップする家事"]["skip"] == 1

    def test_weeks_param_validation(self, client):
        assert client.get("/api/stats/weekly", params={"weeks": 0}).status_code == 422
        res = client.get("/api/stats/weekly", params={"weeks": 2})
        assert len(res.json()["weeks"]) == 2


# ---------------------------------------------------------------- settings

class TestSettings:
    def test_defaults(self, client):
        res = client.get("/api/settings")
        assert res.status_code == 200
        body = res.json()
        assert body["daily_budget_min"] == "30"
        assert body["ntfy_server"] == "https://ntfy.sh"
        assert body["ntfy_topic"] == ""

    def test_put_and_get(self, client):
        res = client.put("/api/settings", json={
            "daily_budget_min": "45",
            "ntfy_topic": "kajiflow-test",
        })
        assert res.status_code == 200
        body = res.json()
        assert body["daily_budget_min"] == "45"
        assert body["ntfy_topic"] == "kajiflow-test"
        # 再取得でも維持される
        again = client.get("/api/settings").json()
        assert again["daily_budget_min"] == "45"

    def test_budget_setting_affects_plan(self, client, raw_conn):
        client.put("/api/settings", json={"daily_budget_min": "15"})
        t1 = create_task(client, name="A", est_minutes=10)
        t2 = create_task(client, name="B", est_minutes=10)
        t3 = create_task(client, name="C", est_minutes=10)
        backdate_created(raw_conn, t1["id"], 12)
        backdate_created(raw_conn, t2["id"], 11)
        backdate_created(raw_conn, t3["id"], 10)
        client.post("/api/plan/regenerate")
        # 10 + 10 = 20 > 15 なので 1 件のみ（1件目は必ず入る）
        assert client.get("/api/next").json()["total_count"] == 1
