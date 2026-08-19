"""SPEC「ゴミカレンダー連動（v4）」テスト節の一括検証。

フェイクのカレンダーイベント（FakeGTasksClient.gomi_events）で:

- 当日該当日のみプラン注入・先頭配置（weekly / interval との並び順）
- gomi_events 洗い替えの冪等性（2回目 sync は差分ゼロ・過去行/消滅行の削除）
- calendar タスクのユーザー API 422（作成・種別変更の双方向）
- カレンダー取得失敗・再認証必要時の warnings 継続（家事・Vault 同期は動く）
- est_minutes の予算算入（ただし先頭配置なので必ず入る）

実ネットワーク・実 Vault・実 DB は一切使わない（KAJIFLOW_* 環境変数と
フェイク注入）。フェイクと共通フィクスチャは tests/test_gtasks.py を共用する
（pytest の prepend import mode で tests/ が sys.path に入るため直接 import
できる。vault_file / conn / fake は fixture としてこのモジュールでも有効）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app import gtasks
from app.engine import JST

from test_gtasks import (  # noqa: F401  (vault_file / conn / fake は fixture)
    FakeGTasksClient,
    GomiFailingClient,
    assert_noop,
    calendar_tasks,
    conn,
    fake,
    gomi_rows,
    now_jst,
    run_sync,
    set_gomi_calendar,
    today_str,
    vault_file,
    write_vault,
)

GOMI_CAL_ID = "gomi@example.com"


def offset_str(days: int) -> str:
    return (now_jst() + timedelta(days=days)).date().isoformat()


def today_weekday() -> int:
    return datetime.now(JST).weekday()


# ---------------------------------------------------------------- API 経由ヘルパ

def put_settings(client, **kv) -> None:
    res = client.put("/api/settings", json=kv)
    assert res.status_code == 200, res.text


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


def backdate_created(raw_conn, task_id: int, days: float) -> None:
    """created_at を過去に書き換えて interval タスクを「期限到来」にする。"""
    c = raw_conn()
    try:
        past = (now_jst() - timedelta(days=days)).isoformat()
        c.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (past, task_id))
        c.commit()
    finally:
        c.close()


def insert_calendar_task(raw_conn, name: str) -> int:
    """gtasks 同期の合成タスク相当を直接 DB に作る（システム管理経路の模擬）。"""
    c = raw_conn()
    try:
        cur = c.execute(
            "INSERT INTO tasks (name, category, est_minutes, schedule_type, "
            "interval_days, weekdays, adaptive, enabled, notes, created_at) "
            "VALUES (?, 'ゴミ', 5, 'calendar', NULL, NULL, 0, 1, '', ?)",
            (name, now_jst().isoformat()),
        )
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def sync_via_api(client, monkeypatch, fake_client) -> dict:
    """フェイクを注入して POST /api/gtasks/sync（実クライアントは作らない）。"""
    monkeypatch.setattr(gtasks, "build_client", lambda: fake_client)
    res = client.post("/api/gtasks/sync")
    assert res.status_code == 200, res.text
    return res.json()


def plan_ids(client) -> list[int]:
    """プランを作り直して当日プランの task_id リスト（提示順）を返す。"""
    res = client.post("/api/plan/regenerate")
    assert res.status_code == 200, res.text
    items = client.get("/api/today").json()["items"]
    return [it["task"]["id"] for it in items]


def task_by_name(client, name: str) -> dict:
    matches = [t for t in client.get("/api/tasks").json() if t["name"] == name]
    assert len(matches) == 1, f"{name!r} が {len(matches)} 件"
    return matches[0]


# ---------------------------------------------------------------- 当日注入・並び順・予算

class TestPlanInjection:
    """フェイクイベント → sync → regenerate のエンドツーエンドで注入を検証する。"""

    def _sync_events(self, client, vault_file, monkeypatch, events) -> dict:
        write_vault(vault_file)
        put_settings(client, **{gtasks.GOMI_CALENDAR_SETTING: GOMI_CAL_ID})
        f = FakeGTasksClient()
        f.gomi_events = events
        result = sync_via_api(client, monkeypatch, f)
        assert result["warnings"] == []
        return result

    def test_only_due_day_injected_at_head(
        self, client, raw_conn, vault_file, monkeypatch
    ):
        # 当日該当のイベントだけがプランに入り、weekly より前（先頭）に置かれる。
        # 2日後のイベントは合成タスクこそ作られるが、当日プランには出ない。
        weekly = create_task(
            client, name="毎週の家事", schedule_type="weekly",
            interval_days=None, weekdays=str(today_weekday()),
        )
        self._sync_events(client, vault_file, monkeypatch, [
            {"date": today_str(), "summary": "燃えるゴミ"},
            {"date": offset_str(2), "summary": "プラスチック"},
        ])

        moe = task_by_name(client, "ゴミ出し: 燃えるゴミ")
        pla = task_by_name(client, "ゴミ出し: プラスチック")  # 合成タスクは存在する
        assert moe["schedule_type"] == pla["schedule_type"] == "calendar"

        ids = plan_ids(client)
        assert ids == [moe["id"], weekly["id"]]  # 先頭配置・当日のみ
        assert pla["id"] not in ids

    def test_order_calendar_then_weekly_then_interval(
        self, client, raw_conn, vault_file, monkeypatch
    ):
        # 並び順: calendar（先頭）→ weekly（id 順）→ interval（urgency 降順）
        weekly = create_task(
            client, name="毎週の家事", schedule_type="weekly",
            interval_days=None, weekdays=str(today_weekday()), est_minutes=5,
        )
        iv = create_task(client, name="間隔の家事", interval_days=3, est_minutes=5)
        backdate_created(raw_conn, iv["id"], 10)  # urgency > 1.0
        self._sync_events(client, vault_file, monkeypatch, [
            {"date": today_str(), "summary": "燃えるゴミ"},
        ])
        cal_id = task_by_name(client, "ゴミ出し: 燃えるゴミ")["id"]

        assert plan_ids(client) == [cal_id, weekly["id"], iv["id"]]

    def test_est_minutes_counted_into_budget(
        self, client, raw_conn, vault_file, monkeypatch
    ):
        # calendar の 5 分も予算に算入される: 予算 35 で cal(5) + A(25) = 30 の
        # 後に B(10) は 40 > 35 で入らない（cal が算入されないなら A+B=35 で
        # ちょうど収まってしまうため、この予算値で算入を判別できる）。
        put_settings(client, daily_budget_min="35")
        a = create_task(client, name="家事A", interval_days=3, est_minutes=25)
        b = create_task(client, name="家事B", interval_days=3, est_minutes=10)
        backdate_created(raw_conn, a["id"], 9)  # A の方が緊急
        backdate_created(raw_conn, b["id"], 5)
        self._sync_events(client, vault_file, monkeypatch, [
            {"date": today_str(), "summary": "燃えるゴミ"},
        ])
        cal_id = task_by_name(client, "ゴミ出し: 燃えるゴミ")["id"]

        ids = plan_ids(client)
        assert ids == [cal_id, a["id"]]
        assert b["id"] not in ids  # cal の 5 分が算入された証拠

    def test_head_slot_guaranteed_even_over_budget(
        self, client, raw_conn, vault_file, monkeypatch
    ):
        # est_minutes が予算超過でも calendar は先頭で必ず入る（1件目ルール）
        weekly = create_task(
            client, name="毎週の家事", schedule_type="weekly",
            interval_days=None, weekdays=str(today_weekday()),
        )
        self._sync_events(client, vault_file, monkeypatch, [
            {"date": today_str(), "summary": "粗大ゴミ"},
        ])
        cal = task_by_name(client, "ゴミ出し: 粗大ゴミ")
        # 編集フォーム相当で分数を予算（既定 30）超に変更（calendar でも編集可）
        res = client.put(f"/api/tasks/{cal['id']}", json={"est_minutes": 60})
        assert res.status_code == 200, res.text

        ids = plan_ids(client)
        assert ids == [cal["id"]]  # 先頭確保。予算超過で weekly は入らない
        assert weekly["id"] not in ids

    def test_done_today_not_reinjected(
        self, client, raw_conn, vault_file, monkeypatch
    ):
        # 当日完了済みの calendar タスクは regenerate しても再注入されない
        weekly = create_task(
            client, name="毎週の家事", schedule_type="weekly",
            interval_days=None, weekdays=str(today_weekday()),
        )
        self._sync_events(client, vault_file, monkeypatch, [
            {"date": today_str(), "summary": "燃えるゴミ"},
        ])
        cal_id = task_by_name(client, "ゴミ出し: 燃えるゴミ")["id"]
        assert plan_ids(client) == [cal_id, weekly["id"]]

        assert client.post(f"/api/tasks/{cal_id}/complete").status_code == 200

        assert plan_ids(client) == [weekly["id"]]


# ---------------------------------------------------------------- 洗い替え・冪等

class TestGomiRefresh:
    def test_second_sync_is_noop(self, vault_file, conn, fake):
        # 同一イベント入力の2回目 sync は DB / Google に差分ゼロ
        write_vault(vault_file)
        cid = set_gomi_calendar(conn, GOMI_CAL_ID)
        fake.gomi_events = [
            {"date": today_str(), "summary": "燃えるゴミ"},
            {"date": offset_str(2), "summary": "プラスチック"},
        ]
        run_sync(conn, fake)
        rows_before = gomi_rows(conn)
        tasks_before = calendar_tasks(conn)
        fake.mutations.clear()

        result = run_sync(conn, fake)

        assert_noop(result)
        assert fake.mutations == []
        assert gomi_rows(conn) == rows_before
        assert calendar_tasks(conn) == tasks_before  # 既存同名の再作成なし
        # 取得窓は常に [今日, 今日+7日]
        assert fake.gomi_calls[-1] == (
            cid, now_jst().date(), now_jst().date() + timedelta(days=7)
        )

    def test_refresh_drops_past_and_vanished_rows_but_keeps_tasks(
        self, vault_file, conn, fake
    ):
        # 過去行・カレンダーから消えたイベント行は洗い替えで消えるが、
        # 合成タスク行は消えない（enabled のままプランに出なくなるだけ）
        write_vault(vault_file)
        set_gomi_calendar(conn, GOMI_CAL_ID)
        fake.gomi_events = [{"date": today_str(), "summary": "燃えるゴミ"}]
        run_sync(conn, fake)
        conn.execute(
            "INSERT INTO gomi_events (date, summary) VALUES (?, ?)",
            (offset_str(-1), "古紙"),  # 過去行（前日以前の残骸を模す）
        )
        conn.commit()
        fake.gomi_events = []  # カレンダー側からイベントが消えた

        run_sync(conn, fake)

        assert gomi_rows(conn) == set()
        tasks = calendar_tasks(conn)
        assert [t["name"] for t in tasks] == ["ゴミ出し: 燃えるゴミ"]
        assert tasks[0]["enabled"] == 1


# ---------------------------------------------------------------- API 422

class TestCalendarApi422:
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
        # 種別は変わっていない
        assert client.get(f"/api/tasks/{task['id']}").json()[
            "schedule_type"
        ] == "interval"

    def test_change_from_calendar_is_422(self, client, raw_conn):
        tid = insert_calendar_task(raw_conn, "ゴミ出し: 燃えるゴミ")
        res = client.put(f"/api/tasks/{tid}", json={
            "schedule_type": "weekly", "weekdays": "0",
        })
        assert res.status_code == 422
        assert "システム管理" in res.json()["detail"]
        assert client.get(f"/api/tasks/{tid}").json()[
            "schedule_type"
        ] == "calendar"


# ---------------------------------------------------------------- warnings 継続

class TestWarningsContinuation:
    def test_fetch_failure_warns_and_sync_continues(self, vault_file, conn):
        # 取得失敗は warnings に集約され、家事・Vault 同期は従来どおり動く。
        # 洗い替えは行われず、前回取得した gomi_events キャッシュは残る。
        write_vault(vault_file)
        set_gomi_calendar(conn, GOMI_CAL_ID)
        ok = FakeGTasksClient()
        ok.gomi_events = [{"date": today_str(), "summary": "燃えるゴミ"}]
        first = run_sync(conn, ok)
        assert first["pushed"] == 2  # vault 2件（フィクスチャの非ハンドオフ）
        rows_before = gomi_rows(conn)
        assert rows_before != set()

        failing = GomiFailingClient(
            gtasks.GTasksApiError("Calendar API 呼び出しに失敗しました（HTTP 500）")
        )
        result = run_sync(conn, failing)

        assert any(
            "ゴミカレンダーの取得に失敗しました" in w for w in result["warnings"]
        )
        assert gomi_rows(conn) == rows_before  # 失敗時はキャッシュを消さない
        # gomi 以外の同期は例外で落ちず完走している（failing は Google 側が
        # 空の別クライアントなので、vault 2件が改めて push される）
        assert result["pushed"] == 2
        assert len(result["warnings"]) == 1

    def test_reauth_required_warns_and_sync_continues(self, vault_file, conn):
        # スコープ不足（RefreshError / 401・403 相当の GTasksAuthError）でも
        # 同期全体は落ちず、再認証案内が warnings に載る
        write_vault(vault_file)
        set_gomi_calendar(conn, GOMI_CAL_ID)
        failing = GomiFailingClient(
            gtasks.GTasksAuthError(gtasks.CALENDAR_REAUTH_HINT)
        )

        result = run_sync(conn, failing)

        assert any(
            "カレンダー連動には再認証が必要です" in w for w in result["warnings"]
        )
        assert result["pushed"] == 2  # vault push は従来どおり

    def test_api_sync_returns_200_with_warnings_on_reauth(
        self, client, vault_file, monkeypatch
    ):
        # カレンダーだけの再認証必要は API でも 503 にせず、200 + warnings で返す
        write_vault(vault_file)
        put_settings(client, **{gtasks.GOMI_CALENDAR_SETTING: GOMI_CAL_ID})
        failing = GomiFailingClient(
            gtasks.GTasksAuthError(gtasks.CALENDAR_REAUTH_HINT)
        )

        body = sync_via_api(client, monkeypatch, failing)  # 200 を内部で assert

        assert body["pushed"] == 2
        assert any(
            "scripts/gtasks_auth.py を再実行してください" in w
            for w in body["warnings"]
        )
