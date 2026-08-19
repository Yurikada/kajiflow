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


# ---------------------------------------------------------------- calendar タスク（v4）

def insert_calendar_task(raw_conn, name: str) -> int:
    """gtasks 同期の合成タスク相当を直接 DB に作る（システム管理経路の模擬）。"""
    conn = raw_conn()
    try:
        cur = conn.execute(
            "INSERT INTO tasks (name, category, est_minutes, schedule_type, "
            "interval_days, weekdays, adaptive, enabled, notes, created_at) "
            "VALUES (?, 'ゴミ', 5, 'calendar', NULL, NULL, 0, 1, '', ?)",
            (name, datetime.now(JST).isoformat()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def insert_gomi_event(raw_conn, date_str: str, summary: str) -> None:
    conn = raw_conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO gomi_events (date, summary) VALUES (?, ?)",
            (date_str, summary),
        )
        conn.commit()
    finally:
        conn.close()


class TestCalendarTasks:
    def test_create_calendar_type_is_422(self, client):
        res = client.post("/api/tasks", json={
            "name": "ゴミ出し: 燃えるゴミ", "schedule_type": "calendar",
        })
        assert res.status_code == 422
        assert "システム管理" in res.json()["detail"]

    def test_change_to_calendar_is_422(self, client):
        task = create_task(client)
        res = client.put(
            f"/api/tasks/{task['id']}", json={"schedule_type": "calendar"}
        )
        assert res.status_code == 422
        assert "システム管理" in res.json()["detail"]

    def test_change_from_calendar_is_422(self, client, raw_conn):
        tid = insert_calendar_task(raw_conn, "ゴミ出し: 燃えるゴミ")
        res = client.put(f"/api/tasks/{tid}", json={
            "schedule_type": "weekly", "weekdays": "0",
        })
        assert res.status_code == 422

    def test_edit_name_minutes_notes_allowed(self, client, raw_conn):
        # 編集フォーム相当: 名前・分数・メモの変更は可（種別はそのまま）
        tid = insert_calendar_task(raw_conn, "ゴミ出し: 燃えるゴミ")
        res = client.put(f"/api/tasks/{tid}", json={
            "name": "ゴミ出し: 可燃", "est_minutes": 10, "notes": "8時まで",
        })
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["name"] == "ゴミ出し: 可燃"
        assert body["est_minutes"] == 10
        assert body["schedule_type"] == "calendar"

    def test_toggle_and_delete_allowed(self, client, raw_conn):
        tid = insert_calendar_task(raw_conn, "ゴミ出し: 燃えるゴミ")
        res = client.put(f"/api/tasks/{tid}", json={"enabled": 0})
        assert res.status_code == 200
        assert res.json()["enabled"] == 0
        assert client.delete(f"/api/tasks/{tid}").status_code == 200

    def test_plan_injects_calendar_task_at_head_on_due_day(self, client, raw_conn):
        # 当日該当の calendar タスクは weekly より前（プラン先頭）に入る。
        # gomi_events に無い calendar タスクは対象外。
        weekly = create_task(
            client, name="毎週の家事", schedule_type="weekly",
            interval_days=None, weekdays=str(today_weekday()),
        )
        due_id = insert_calendar_task(raw_conn, "ゴミ出し: 燃えるゴミ")
        insert_calendar_task(raw_conn, "ゴミ出し: 古紙")  # 当日該当なし
        today = datetime.now(JST).date().isoformat()
        insert_gomi_event(raw_conn, today, "燃えるゴミ")

        res = client.post("/api/plan/regenerate")
        assert res.status_code == 200
        items = client.get("/api/today").json()["items"]
        ids = [it["task"]["id"] for it in items]
        assert ids == [due_id, weekly["id"]]


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
        assert first["recorded"] is True    # 初回は記録された
        assert resend["recorded"] is False  # 再送は記録されていない

        conn = raw_conn()
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM completions WHERE task_id = ?", (t1["id"],)
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 1  # 記録は1件のみ

    def test_complete_concurrent_requests_record_once(self, client, raw_conn):
        """同時並行の complete でも記録は1件（BEGIN IMMEDIATE による原子化）。"""
        import threading

        t1, _t2 = self._setup_two_due_tasks(client, raw_conn)
        barrier = threading.Barrier(4)
        results = []

        def hit():
            barrier.wait()
            results.append(client.post(f"/api/tasks/{t1['id']}/complete").status_code)

        threads = [threading.Thread(target=hit) for _ in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        assert all(code == 200 for code in results)
        conn = raw_conn()
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM completions WHERE task_id = ?", (t1["id"],)
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 1

    def test_uncomplete_restores_pending(self, client, raw_conn):
        """完了取り消しで pending に戻り、次の1件にも復帰する。"""
        t1, t2 = self._setup_two_due_tasks(client, raw_conn)

        client.post(f"/api/tasks/{t1['id']}/complete")
        res = client.post(f"/api/tasks/{t1['id']}/uncomplete")
        assert res.status_code == 200
        body = res.json()
        assert body["restored"] is True
        assert body["done_count"] == 0
        assert body["task"]["id"] == t1["id"]  # 先頭に復帰

        items = client.get("/api/today").json()["items"]
        statuses = {i["task"]["id"]: i["status"] for i in items}
        assert statuses[t1["id"]] == "pending"

        # 取り消し後の再完了は普通にできる（undo 墓標が冪等ガードを塞がない）
        body = client.post(f"/api/tasks/{t1['id']}/complete").json()
        assert body["recorded"] is True
        assert body["done_count"] == 1

    def test_uncomplete_without_record_reports_false(self, client, raw_conn):
        t1, _t2 = self._setup_two_due_tasks(client, raw_conn)
        body = client.post(f"/api/tasks/{t1['id']}/uncomplete").json()
        assert body["restored"] is False

    def test_uncomplete_removes_legacy_skip(self, client, raw_conn):
        t1, _t2 = self._setup_two_due_tasks(client, raw_conn)
        client.post(f"/api/tasks/{t1['id']}/skip")
        body = client.post(f"/api/tasks/{t1['id']}/uncomplete").json()
        assert body["restored"] is True
        items = client.get("/api/today").json()["items"]
        statuses = {i["task"]["id"]: i["status"] for i in items}
        assert statuses[t1["id"]] == "pending"

    def test_defer_moves_task_to_end_without_record(self, client, raw_conn):
        """あとまわしは最後尾へ回すだけで completions に記録しない。"""
        t1, t2 = self._setup_two_due_tasks(client, raw_conn)

        res = client.post(f"/api/tasks/{t1['id']}/defer")
        assert res.status_code == 200
        body = res.json()
        assert body["deferred"] is True
        assert body["task"]["id"] == t2["id"]  # 次の1件は t2 に進む
        assert body["done_count"] == 0

        # プラン順序: t2 → t1（t1 は消えず最後尾）
        items = client.get("/api/today").json()["items"]
        assert [i["task"]["id"] for i in items] == [t2["id"], t1["id"]]
        assert all(i["status"] == "pending" for i in items)  # 記録なし

        # t2 を完了すると t1 が戻ってくる（取り返しがつく）
        body = client.post(f"/api/tasks/{t2['id']}/complete").json()
        assert body["task"]["id"] == t1["id"]

        conn = raw_conn()
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM completions WHERE task_id = ?", (t1["id"],)
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 0  # defer は記録を残さない

    def test_defer_last_pending_reports_not_deferred(self, client, raw_conn):
        t1, t2 = self._setup_two_due_tasks(client, raw_conn)
        client.post(f"/api/tasks/{t2['id']}/complete")
        body = client.post(f"/api/tasks/{t1['id']}/defer").json()
        assert body["deferred"] is False
        assert body["task"]["id"] == t1["id"]  # 残り1件なので先頭のまま

    def test_defer_task_not_in_plan_is_409(self, client, raw_conn):
        t1, _t2 = self._setup_two_due_tasks(client, raw_conn)
        t3 = create_task(client, name="プラン外", interval_days=30)
        res = client.post(f"/api/tasks/{t3['id']}/defer")
        assert res.status_code == 409

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
