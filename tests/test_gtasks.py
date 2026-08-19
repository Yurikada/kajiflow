"""app/gtasks.py の Google Tasks 連携テスト（SPEC「Google Tasks 連携（v3）」節）。

Google API のネットワークは一切叩かない。GTasksClient と同じメソッド契約を持つ
FakeGTasksClient（メモリ上のリスト/タスク辞書）を sync_all に注入して検証する:

- vault push: 非ハンドオフのみ・notes に uid マーカー・due 反映・冪等
- ハンドオフ除外（Google 側に絶対現れない）
- pull 完了 → Vault 一時ファイルへ [x] 書き戻し、409系は warnings 行き
- pull 新規 → agent_memory_io.py add-task の subprocess 呼び出し（monkeypatch 捕捉）
- Vault 側完了/消滅 → Google completed / delete
- chore: 当日プラン push・完了 pull（action='done'）・日替わりの前日リンク削除
- API: 未認証 503（日本語案内）と /api/gtasks/status の last_sync 保存

実在の Vault / DB / token は一切読み書きしない。パスは必ず環境変数
KAJIFLOW_VAULT / KAJIFLOW_VAULT_TASKFILE / KAJIFLOW_DB / KAJIFLOW_MEMORY_IO と
monkeypatch（gtasks.TOKEN_PATH）で tmp_path に差し替える。
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from datetime import datetime, timedelta

import pytest

from app import db, gtasks, vault
from app.engine import JST

# ---------------------------------------------------------------- フィクスチャ

# 実ファイルのフォーマットを模した合成 Markdown（非ハンドオフ2件 + ハンドオフ1件）
GTASKS_MD = """\
# AIエージェント連携タスク

## 進行中

- [ ] 掃除ロボットの選定
  - 追加日: 2026-08-10
  - 目的: 手間を減らす
  - 完了条件: 候補を3つ挙げる
  - 期限: 2026-08-20
- [ ] 領収書の整理
  - 追加日: 2026-08-11

## ハンドオフ

- [ ] コードレビュー対応
  - id: T20260816-203634-cbd6
  - owner: codex
  - status: open
  - handoff-to: claude
"""

HANDOFF_UID = "T20260816-203634-cbd6"
HANDOFF_TITLE = "コードレビュー対応"

# 同名タスクが2件ある（完了書き戻しが曖昧一致で 409 になる）ケース
DUP_MD = (
    "## セクションA\n"
    "\n"
    "- [ ] 同名タスク\n"
    "  - 追加日: 2026-08-10\n"
    "\n"
    "## セクションB\n"
    "\n"
    "- [ ] 同名タスク\n"
    "  - 追加日: 2026-08-11\n"
)


def now_jst() -> datetime:
    return datetime.now(JST)


def today_str() -> str:
    return now_jst().date().isoformat()


# ---------------------------------------------------------------- フェイク

class FakeGTasksClient:
    """GTasksClient と同じメソッド契約のメモリ実装（app/gtasks.py の docstring 参照）。

    - list_tasks / insert_task / patch_task はコピーを返す。内部状態の変更は
      必ずメソッド経由になるため、mutations で「Google 側への書き込み回数」を
      数えられる（冪等性 = 2回目の sync で mutations が空）。
    - テストから内部 dict を直接書き換えることで「Google 側での操作
      （スマホでの完了・改名・直接追加）」を模す（tasks_in が実 dict を返す）。
    """

    def __init__(self) -> None:
        self._lists: dict[str, dict] = {}
        self._tasks: dict[str, dict[str, dict]] = {}
        self._seq = 0
        self.mutations: list[tuple] = []
        # ゴミカレンダー（v4）: テストが直接詰める全期間のイベント
        # [{"date": "YYYY-MM-DD", "summary": str}, ...]
        self.gomi_events: list[dict] = []
        self.gomi_calls: list[tuple] = []

    def _new_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}{self._seq:04d}"

    # ---------------- 契約メソッド（GTasksClient と同一）

    def list_tasklists(self) -> list[dict]:
        return [dict(tl) for tl in self._lists.values()]

    def insert_tasklist(self, title: str) -> dict:
        lid = self._new_id("L")
        self._lists[lid] = {"id": lid, "title": title}
        self._tasks[lid] = {}
        self.mutations.append(("insert_tasklist", title))
        return dict(self._lists[lid])

    def list_tasks(self, list_id: str) -> list[dict]:
        return [copy.deepcopy(t) for t in self._tasks[list_id].values()]

    def insert_task(self, list_id: str, body: dict) -> dict:
        gid = self._new_id("G")
        task = {"id": gid, "status": "needsAction"}
        task.update({k: v for k, v in body.items() if v is not None})
        self._tasks[list_id][gid] = task
        self.mutations.append(("insert_task", list_id, gid, dict(body)))
        return copy.deepcopy(task)

    def patch_task(self, list_id: str, gtask_id: str, body: dict) -> dict:
        task = self._tasks[list_id][gtask_id]
        for k, v in body.items():
            if v is None:
                task.pop(k, None)  # due=None はクリア（実 API と同じ）
            else:
                task[k] = v
        self.mutations.append(("patch_task", list_id, gtask_id, dict(body)))
        return copy.deepcopy(task)

    def delete_task(self, list_id: str, gtask_id: str) -> None:
        removed = self._tasks[list_id].pop(gtask_id, None)
        if removed is not None:
            self.mutations.append(("delete_task", list_id, gtask_id))
        # 既に無い場合は 404 相当 → 成功扱い（冪等）

    def list_gomi_events(self, calendar_id, start_date, end_date) -> list[dict]:
        self.gomi_calls.append((calendar_id, start_date, end_date))
        lo, hi = start_date.isoformat(), end_date.isoformat()
        return [dict(ev) for ev in self.gomi_events if lo <= ev["date"] <= hi]

    # ---------------- テスト補助

    def list_id_of(self, title: str) -> str | None:
        for tl in self._lists.values():
            if tl["title"] == title:
                return tl["id"]
        return None

    def tasks_in(self, list_title: str) -> list[dict]:
        """リスト内の実 dict（変異で Google 側操作を模す）。リスト未作成なら空。"""
        lid = self.list_id_of(list_title)
        return list(self._tasks[lid].values()) if lid else []

    def task_by_title(self, list_title: str, title: str) -> dict:
        matches = [t for t in self.tasks_in(list_title) if t["title"] == title]
        assert len(matches) == 1, f"{list_title} 内の {title!r} が {len(matches)} 件"
        return matches[0]

    def add_google_side_task(
        self, list_title: str, title: str,
        notes: str | None = None, due: str | None = None,
        status: str = "needsAction",
    ) -> dict:
        """ユーザーが Google Tasks 側で直接追加した状況を作る（mutations に数えない）。"""
        lid = self.list_id_of(list_title)
        assert lid is not None, "先に一度 sync してリストを作ること"
        gid = self._new_id("U")
        task = {"id": gid, "title": title, "status": status}
        if notes is not None:
            task["notes"] = notes
        if due is not None:
            task["due"] = due
        self._tasks[lid][gid] = task
        return task

    def snapshot(self) -> dict:
        return copy.deepcopy(self._tasks)


class GomiFailingClient(FakeGTasksClient):
    """list_gomi_events だけ失敗するフェイク（warnings 継続の検証用）。"""

    def __init__(self, exc: Exception) -> None:
        super().__init__()
        self.exc = exc

    def list_gomi_events(self, calendar_id, start_date, end_date) -> list[dict]:
        raise self.exc


class InsertFailingClient(FakeGTasksClient):
    """特定タイトルの insert だけ失敗するフェイク（項目単位 warnings の検証用）。"""

    def __init__(self, failing_title: str) -> None:
        super().__init__()
        self.failing_title = failing_title

    def insert_task(self, list_id: str, body: dict) -> dict:
        if body.get("title") == self.failing_title:
            raise gtasks.GTasksApiError("Tasks API 呼び出しに失敗しました（HTTP 500）")
        return super().insert_task(list_id, body)


# ---------------------------------------------------------------- 共通フィクスチャ

@pytest.fixture
def vault_file(tmp_path, monkeypatch):
    """一時 Vault のタスクファイルパス（実在の Vault を読み書きしないための必須）。"""
    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.setenv("KAJIFLOW_VAULT", str(root))
    monkeypatch.setenv("KAJIFLOW_VAULT_TASKFILE", "AIエージェント連携タスク.md")
    return root / "AIエージェント連携タスク.md"


@pytest.fixture
def conn(tmp_path):
    """gtasks モジュールを直接呼ぶテスト用の一時 DB 接続。"""
    c = db.connect(tmp_path / "kajiflow_gtasks_test.db")
    yield c
    c.close()


@pytest.fixture
def fake():
    return FakeGTasksClient()


@pytest.fixture
def add_task_calls(monkeypatch, tmp_path):
    """agent_memory_io.py add-task の subprocess 呼び出しを捕捉する（実行しない）。

    実スクリプトの契約（Vault 正本にタスク行を追記する）だけを模して
    一時タスクファイルへ追記する。呼び出しコマンドのリストを返す。
    """
    calls: list[list[str]] = []
    script = tmp_path / "memory_io" / "agent_memory_io.py"
    monkeypatch.setenv("KAJIFLOW_MEMORY_IO", str(script))

    def _run(cmd, **kwargs):
        calls.append(list(cmd))
        title = cmd[cmd.index("--") + 1]  # `--` の直後が positional の title
        line = f"- [ ] {title}\n"
        if "--due" in cmd:
            line += f"  - 期限: {cmd[cmd.index('--due') + 1]}\n"
        path = vault.get_taskfile_path()
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(gtasks.subprocess, "run", _run)
    return calls


def write_vault(vault_file, text: str = GTASKS_MD) -> None:
    vault_file.write_bytes(text.encode("utf-8"))


def run_sync(conn, client) -> dict:
    return gtasks.sync_all(conn, client, now_jst())


def get_links(conn, kind: str) -> dict[str, dict]:
    rows = conn.execute(
        "SELECT * FROM gtasks_links WHERE kind = ?", (kind,)
    ).fetchall()
    return {r["local_id"]: dict(r) for r in rows}


def assert_noop(result: dict) -> None:
    assert result == {
        "pushed": 0,
        "completed_in_vault": 0,
        "completed_chores": 0,
        "new_from_google": 0,
        "warnings": [],
    }


def add_chore_task(conn, name: str) -> int:
    cur = conn.execute(
        "INSERT INTO tasks (name, created_at) VALUES (?, ?)",
        (name, now_jst().isoformat()),
    )
    conn.commit()
    return cur.lastrowid


def set_plan(conn, date_str: str, task_ids: list[int]) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO daily_plans (date, task_ids, generated_at) "
        "VALUES (?, ?, ?)",
        (date_str, json.dumps(task_ids), now_jst().isoformat()),
    )
    conn.commit()


# ---------------------------------------------------------------- vault push

class TestVaultPush:
    def test_push_creates_lists_and_tasks(self, vault_file, conn, fake):
        write_vault(vault_file)

        result = run_sync(conn, fake)

        # 2リストが作成される
        titles = {tl["title"] for tl in fake.list_tasklists()}
        assert titles == {gtasks.VAULT_LIST_TITLE, gtasks.CHORE_LIST_TITLE}

        # 非ハンドオフ2件だけが「Vault タスク」リストへ
        assert result["pushed"] == 2
        assert result["warnings"] == []
        pushed_titles = {t["title"] for t in fake.tasks_in(gtasks.VAULT_LIST_TITLE)}
        assert pushed_titles == {"掃除ロボットの選定", "領収書の整理"}

    def test_pushed_task_fields(self, vault_file, conn, fake):
        write_vault(vault_file)
        run_sync(conn, fake)

        uid = vault.title_uid("掃除ロボットの選定")
        g = fake.task_by_title(gtasks.VAULT_LIST_TITLE, "掃除ロボットの選定")
        # notes = uid マーカー + 目的/完了条件 + 固定文言
        assert g["notes"] == (
            f"uid: {uid}\n"
            "目的: 手間を減らす\n"
            "完了条件: 候補を3つ挙げる\n"
            "(KajiFlow同期)"
        )
        assert g["due"] == "2026-08-20T00:00:00.000Z"  # 期限 → RFC3339
        assert g["status"] == "needsAction"

        # メタなしタスクは uid + 固定文言のみ、due なし
        uid2 = vault.title_uid("領収書の整理")
        g2 = fake.task_by_title(gtasks.VAULT_LIST_TITLE, "領収書の整理")
        assert g2["notes"] == f"uid: {uid2}\n(KajiFlow同期)"
        assert "due" not in g2

    def test_push_saves_links(self, vault_file, conn, fake):
        write_vault(vault_file)
        run_sync(conn, fake)

        links = get_links(conn, "vault")
        assert set(links) == {
            vault.title_uid("掃除ロボットの選定"),
            vault.title_uid("領収書の整理"),
        }
        list_id = fake.list_id_of(gtasks.VAULT_LIST_TITLE)
        for link in links.values():
            assert link["list_id"] == list_id
            assert link["gtask_id"]
            assert link["synced_at"]

    def test_push_is_idempotent(self, vault_file, conn, fake):
        write_vault(vault_file)
        run_sync(conn, fake)
        before_google = fake.snapshot()
        before_file = vault_file.read_bytes()
        fake.mutations.clear()

        result = run_sync(conn, fake)

        # 同一入力の2回目は Google / DB / Vault に差分ゼロ
        assert_noop(result)
        assert fake.mutations == []
        assert fake.snapshot() == before_google
        assert vault_file.read_bytes() == before_file

    def test_due_change_is_patched(self, vault_file, conn, fake):
        write_vault(vault_file)
        run_sync(conn, fake)

        write_vault(vault_file, GTASKS_MD.replace("期限: 2026-08-20", "期限: 2026-08-21"))
        fake.mutations.clear()
        result = run_sync(conn, fake)

        assert result["pushed"] == 1
        g = fake.task_by_title(gtasks.VAULT_LIST_TITLE, "掃除ロボットの選定")
        assert g["due"] == "2026-08-21T00:00:00.000Z"

    def test_google_side_rename_is_reverted(self, vault_file, conn, fake):
        # タイトルは kajiflow→Google の一方向上書き（Google 側での改名は次回同期で戻る）
        write_vault(vault_file)
        run_sync(conn, fake)
        g = fake.task_by_title(gtasks.VAULT_LIST_TITLE, "領収書の整理")
        g["title"] = "改名されたタスク"  # Google 側での改名を模す

        run_sync(conn, fake)

        assert g["title"] == "領収書の整理"

    def test_lost_link_self_repairs_from_uid_marker(self, vault_file, conn, fake):
        # リンク行だけが失われても（commit 前の強制終了等）、Google 側 notes の
        # uid マーカーから再リンクし、insert の二重実行で重複を作らない
        write_vault(vault_file)
        run_sync(conn, fake)
        expected = {
            uid: link["gtask_id"] for uid, link in get_links(conn, "vault").items()
        }
        conn.execute("DELETE FROM gtasks_links WHERE kind = 'vault'")
        conn.commit()
        fake.mutations.clear()

        result = run_sync(conn, fake)

        assert_noop(result)
        assert fake.mutations == []  # Google 側へは一切書き込まない
        titles = sorted(
            t["title"] for t in fake.tasks_in(gtasks.VAULT_LIST_TITLE)
        )
        assert titles == ["掃除ロボットの選定", "領収書の整理"]
        restored = {
            uid: link["gtask_id"] for uid, link in get_links(conn, "vault").items()
        }
        assert restored == expected

    def test_item_failure_goes_to_warnings_and_sync_continues(
        self, vault_file, conn
    ):
        # 1件の API 失敗は warnings に集約され、他の項目は同期される
        write_vault(vault_file)
        client = InsertFailingClient("掃除ロボットの選定")

        result = run_sync(conn, client)

        assert result["pushed"] == 1  # もう1件は成功
        assert len(result["warnings"]) == 1
        assert "push に失敗しました" in result["warnings"][0]
        assert "掃除ロボットの選定" in result["warnings"][0]

        # 失敗分は次回同期で再試行される（今度は成功させる）
        client.failing_title = ""
        result2 = run_sync(conn, client)
        assert result2["pushed"] == 1
        assert result2["warnings"] == []
        titles = {t["title"] for t in client.tasks_in(gtasks.VAULT_LIST_TITLE)}
        assert titles == {"掃除ロボットの選定", "領収書の整理"}


# ---------------------------------------------------------------- ハンドオフ除外

class TestHandoffExclusion:
    def test_handoff_never_appears_on_google(self, vault_file, conn, fake):
        write_vault(vault_file)
        run_sync(conn, fake)
        run_sync(conn, fake)  # 2回実行しても現れない

        for list_title in (gtasks.VAULT_LIST_TITLE, gtasks.CHORE_LIST_TITLE):
            titles = [t["title"] for t in fake.tasks_in(list_title)]
            assert HANDOFF_TITLE not in titles

        # リンクも作られない
        assert HANDOFF_UID not in get_links(conn, "vault")


# ---------------------------------------------------------------- pull 完了

class TestPullCompleted:
    def test_completed_on_google_writes_back_to_vault(self, vault_file, conn, fake):
        write_vault(vault_file)
        run_sync(conn, fake)
        g = fake.task_by_title(gtasks.VAULT_LIST_TITLE, "領収書の整理")
        g["status"] = "completed"  # スマホでの完了操作を模す

        result = run_sync(conn, fake)

        assert result["completed_in_vault"] == 1
        assert result["warnings"] == []
        text = vault_file.read_bytes().decode("utf-8")
        assert "- [x] 領収書の整理" in text
        assert f"  - 完了: {today_str()}、KajiFlowから完了" in text
        # DB も完了
        row = conn.execute(
            "SELECT checked FROM vault_tasks WHERE uid = ?",
            (vault.title_uid("領収書の整理"),),
        ).fetchone()
        assert row["checked"] == 1
        # 他のタスクは未完了のまま
        assert "- [ ] 掃除ロボットの選定" in text

    def test_writeback_is_idempotent(self, vault_file, conn, fake):
        write_vault(vault_file)
        run_sync(conn, fake)
        fake.task_by_title(gtasks.VAULT_LIST_TITLE, "領収書の整理")["status"] = (
            "completed"
        )
        run_sync(conn, fake)
        before_file = vault_file.read_bytes()
        before_google = fake.snapshot()
        fake.mutations.clear()

        result = run_sync(conn, fake)

        assert_noop(result)
        assert fake.mutations == []
        assert vault_file.read_bytes() == before_file
        assert fake.snapshot() == before_google

    def test_ambiguous_writeback_goes_to_warnings(self, vault_file, conn, fake):
        # 曖昧一致（同名2行）は 409 相当 → warnings 行き。
        # Vault は非書込・Google 側は completed のまま放置
        write_vault(vault_file, DUP_MD)
        run_sync(conn, fake)
        g = fake.task_by_title(gtasks.VAULT_LIST_TITLE, "同名タスク")
        g["status"] = "completed"
        before = vault_file.read_bytes()

        result = run_sync(conn, fake)

        assert result["completed_in_vault"] == 0
        assert len(result["warnings"]) == 1
        assert "完了書き戻しを中断しました" in result["warnings"][0]
        assert vault_file.read_bytes() == before  # 1バイトも書き込まない
        assert g["status"] == "completed"  # Google 側は放置
        row = conn.execute(
            "SELECT checked FROM vault_tasks WHERE uid = ?",
            (vault.title_uid("同名タスク"),),
        ).fetchone()
        assert row["checked"] == 0

    def test_vanished_task_completed_on_google_warns_without_writing(
        self, vault_file, conn, fake
    ):
        # 同期後に Vault 側から行が消滅（消滅は SPEC vault-4 により Google 側 delete。
        # pull 完了の書き戻しは中断され warnings に載り、Vault は非書込）
        write_vault(vault_file)
        run_sync(conn, fake)
        g = fake.task_by_title(gtasks.VAULT_LIST_TITLE, "領収書の整理")
        g["status"] = "completed"
        removed = GTASKS_MD.replace(
            "- [ ] 領収書の整理\n  - 追加日: 2026-08-11\n", ""
        )
        write_vault(vault_file, removed)

        result = run_sync(conn, fake)

        assert result["completed_in_vault"] == 0
        assert any("完了書き戻しを中断しました" in w for w in result["warnings"])
        assert vault_file.read_bytes() == removed.encode("utf-8")  # 非書込
        # 消滅タスクは Google 側から削除され、リンクも消える（SPEC vault-4）
        titles = [t["title"] for t in fake.tasks_in(gtasks.VAULT_LIST_TITLE)]
        assert "領収書の整理" not in titles
        assert vault.title_uid("領収書の整理") not in get_links(conn, "vault")


# ---------------------------------------------------------------- pull 新規

class TestPullNew:
    DUE_RFC = "2026-08-25T00:00:00.000Z"

    def _prepare(self, vault_file, conn, fake) -> dict:
        """初回 sync でリストを作り、Google 側に直接追加されたタスクを置く。"""
        write_vault(vault_file)
        run_sync(conn, fake)
        return fake.add_google_side_task(
            gtasks.VAULT_LIST_TITLE, "牛乳を買う", due=self.DUE_RFC
        )

    def test_new_google_task_calls_add_task_subprocess(
        self, vault_file, conn, fake, add_task_calls, tmp_path
    ):
        self._prepare(vault_file, conn, fake)

        result = run_sync(conn, fake)

        assert result["new_from_google"] == 1
        assert result["warnings"] == []
        # subprocess は捕捉のみ（実行しない）。引数の契約を検証する
        assert len(add_task_calls) == 1
        cmd = add_task_calls[0]
        assert cmd[0] == sys.executable
        assert cmd[1] == str(tmp_path / "memory_io" / "agent_memory_io.py")
        # option 類を先に置き、`--` の後に positional の title を置く
        # （`-` で始まるタイトルが option と誤解釈されないため）
        assert cmd[2:] == [
            "add-task",
            "--source", "Google Tasks",
            "--due", "2026-08-25",
            "--", "牛乳を買う",
        ]

    def test_new_task_gets_uid_marker_and_link(
        self, vault_file, conn, fake, add_task_calls
    ):
        g = self._prepare(vault_file, conn, fake)
        run_sync(conn, fake)

        uid = vault.title_uid("牛乳を買う")
        # 再実行で二重追記しないよう Google 側 notes に uid マーカーが付く
        assert g["notes"] == f"uid: {uid}\n{gtasks.SYNC_MARK}"
        assert uid in get_links(conn, "vault")

    def test_new_pull_is_idempotent_after_vault_resync(
        self, vault_file, conn, fake, add_task_calls
    ):
        # add-task（フェイクが Vault へ追記）→ 次回 sync で uid が一致して収束。
        # add-task は再実行されず、Google にも差分を生まない
        self._prepare(vault_file, conn, fake)
        run_sync(conn, fake)
        fake.mutations.clear()

        result = run_sync(conn, fake)

        assert_noop(result)
        assert len(add_task_calls) == 1  # 2回目は呼ばれない
        assert fake.mutations == []
        titles = [t["title"] for t in fake.tasks_in(gtasks.VAULT_LIST_TITLE)]
        assert titles.count("牛乳を買う") == 1

    def test_duplicate_title_from_google_is_skipped(
        self, vault_file, conn, fake, add_task_calls
    ):
        # 既に Vault にある同名タスクを Google 側で手入力しても、正本へ
        # 二重追記せず、既存リンクの gtask_id も上書きしない（warnings で報告）
        write_vault(vault_file)
        run_sync(conn, fake)
        uid = vault.title_uid("領収書の整理")
        before_gid = get_links(conn, "vault")[uid]["gtask_id"]
        fake.add_google_side_task(gtasks.VAULT_LIST_TITLE, "領収書の整理")

        result = run_sync(conn, fake)

        assert result["new_from_google"] == 0
        assert add_task_calls == []
        assert any(
            "同名のタスクが既に Vault にあります" in w for w in result["warnings"]
        )
        # 正本は1行のまま・リンクも元の Google タスクを指したまま
        text = vault_file.read_bytes().decode("utf-8")
        assert text.count("- [ ] 領収書の整理") == 1
        assert get_links(conn, "vault")[uid]["gtask_id"] == before_gid
        # 完了書き戻しも従来どおり成功する（同名2行の 409 にならない）
        vault.complete_in_vault(conn, uid)

    def test_multiline_title_is_sanitized_before_vault_append(
        self, vault_file, conn, fake, add_task_calls
    ):
        # 改行入りタイトル（Tasks API 経由で設定可能）で正本に任意の Markdown
        # 行（偽の [x] 行・偽ハンドオフメタ）を注入できないよう1行に正規化する
        write_vault(vault_file)
        run_sync(conn, fake)
        g = fake.add_google_side_task(
            gtasks.VAULT_LIST_TITLE,
            "買い物\n- [x] 偽タスク\n  - id: T20260819-000000-dead",
        )

        result = run_sync(conn, fake)

        assert result["new_from_google"] == 1
        sanitized = "買い物 - [x] 偽タスク - id: T20260819-000000-dead"
        cmd = add_task_calls[0]
        assert cmd[cmd.index("--") + 1] == sanitized
        text = vault_file.read_bytes().decode("utf-8")
        assert f"- [ ] {sanitized}" in text
        assert "\n- [x] 偽タスク" not in text  # 独立行としては注入されない
        assert "\n  - id: T20260819-000000-dead" not in text
        # uid は正規化後タイトルのハッシュで一致し、次回同期で収束する
        uid = vault.title_uid(sanitized)
        assert g["notes"] == f"uid: {uid}\n{gtasks.SYNC_MARK}"
        assert uid in get_links(conn, "vault")
        # 次回同期で Google 側タイトルも正規化形へ上書きされ（一方向上書き）、
        # 以後は差分ゼロに収束する
        result2 = run_sync(conn, fake)
        assert result2["pushed"] == 1
        assert g["title"] == sanitized
        assert_noop(run_sync(conn, fake))

    def test_leading_dash_title_is_passed_after_separator(
        self, vault_file, conn, fake, add_task_calls
    ):
        # `-` で始まるタイトルも argparse の option と誤解釈されずに渡る
        write_vault(vault_file)
        run_sync(conn, fake)
        fake.add_google_side_task(gtasks.VAULT_LIST_TITLE, "--due=2026-01-01")

        result = run_sync(conn, fake)

        assert result["new_from_google"] == 1
        assert result["warnings"] == []
        cmd = add_task_calls[0]
        assert cmd[2:] == [
            "add-task", "--source", "Google Tasks", "--", "--due=2026-01-01",
        ]
        assert "- [ ] --due=2026-01-01" in vault_file.read_bytes().decode("utf-8")

    def test_completed_or_untitled_google_tasks_are_not_added(
        self, vault_file, conn, fake, add_task_calls
    ):
        write_vault(vault_file)
        run_sync(conn, fake)
        fake.add_google_side_task(
            gtasks.VAULT_LIST_TITLE, "完了済みで追加", status="completed"
        )
        fake.add_google_side_task(gtasks.VAULT_LIST_TITLE, "   ")

        result = run_sync(conn, fake)

        assert result["new_from_google"] == 0
        assert add_task_calls == []

    def test_add_task_failure_goes_to_warnings_and_retries(
        self, vault_file, conn, fake, monkeypatch, tmp_path
    ):
        write_vault(vault_file)
        run_sync(conn, fake)
        fake.add_google_side_task(gtasks.VAULT_LIST_TITLE, "牛乳を買う")
        monkeypatch.setenv(
            "KAJIFLOW_MEMORY_IO", str(tmp_path / "agent_memory_io.py")
        )
        calls: list[list[str]] = []

        def _fail(cmd, **kwargs):
            calls.append(list(cmd))
            return subprocess.CompletedProcess(
                cmd, 1, stdout="", stderr="boom"
            )

        monkeypatch.setattr(gtasks.subprocess, "run", _fail)

        result = run_sync(conn, fake)

        assert result["new_from_google"] == 0
        assert any("新規タスク追記に失敗しました" in w for w in result["warnings"])
        # リンク未作成・notes 無変更（次回同期で再試行される）
        assert vault.title_uid("牛乳を買う") not in get_links(conn, "vault")
        g = fake.task_by_title(gtasks.VAULT_LIST_TITLE, "牛乳を買う")
        assert "notes" not in g
        run_sync(conn, fake)
        assert len(calls) == 2  # 再試行された


# ---------------------------------------------------------------- Vault 側完了/消滅

class TestVaultSideChanges:
    def test_vault_completed_marks_google_completed(self, vault_file, conn, fake):
        write_vault(vault_file)
        run_sync(conn, fake)
        # Vault 側（手動）で完了された
        write_vault(
            vault_file,
            GTASKS_MD.replace("- [ ] 領収書の整理", "- [x] 領収書の整理"),
        )

        run_sync(conn, fake)

        g = fake.task_by_title(gtasks.VAULT_LIST_TITLE, "領収書の整理")
        assert g["status"] == "completed"
        # リンクは残る（削除ではない）
        assert vault.title_uid("領収書の整理") in get_links(conn, "vault")

    def test_vault_vanished_deletes_google_task(self, vault_file, conn, fake):
        write_vault(vault_file)
        run_sync(conn, fake)
        removed = GTASKS_MD.replace(
            "- [ ] 領収書の整理\n  - 追加日: 2026-08-11\n", ""
        )
        write_vault(vault_file, removed)

        result = run_sync(conn, fake)

        assert result["warnings"] == []
        titles = [t["title"] for t in fake.tasks_in(gtasks.VAULT_LIST_TITLE)]
        assert "領収書の整理" not in titles
        assert "掃除ロボットの選定" in titles  # 残存タスクは無傷
        assert vault.title_uid("領収書の整理") not in get_links(conn, "vault")


# ---------------------------------------------------------------- chore

class TestChores:
    def _prepare(self, vault_file, conn, names: list[str]) -> list[int]:
        write_vault(vault_file)
        ids = [add_chore_task(conn, n) for n in names]
        set_plan(conn, today_str(), ids)
        return ids

    def test_push_today_plan(self, vault_file, conn, fake):
        ids = self._prepare(vault_file, conn, ["風呂掃除", "洗濯"])

        result = run_sync(conn, fake)

        assert result["warnings"] == []
        chores = fake.tasks_in(gtasks.CHORE_LIST_TITLE)
        assert {t["title"] for t in chores} == {"風呂掃除", "洗濯"}
        for t in chores:
            assert t["due"] == f"{today_str()}T00:00:00.000Z"
            assert t["status"] == "needsAction"
            assert t["notes"] == gtasks.SYNC_MARK
        assert set(get_links(conn, "chore")) == {
            f"{today_str()}:{tid}" for tid in ids
        }

    def test_chore_push_is_idempotent(self, vault_file, conn, fake):
        self._prepare(vault_file, conn, ["風呂掃除"])
        run_sync(conn, fake)
        fake.mutations.clear()

        result = run_sync(conn, fake)

        assert_noop(result)
        assert fake.mutations == []

    def test_completed_on_google_records_done(self, vault_file, conn, fake):
        (tid,) = self._prepare(vault_file, conn, ["風呂掃除"])
        run_sync(conn, fake)
        g = fake.task_by_title(gtasks.CHORE_LIST_TITLE, "風呂掃除")
        g["status"] = "completed"  # スマホでの完了操作を模す

        result = run_sync(conn, fake)

        assert result["completed_chores"] == 1
        rows = conn.execute(
            "SELECT task_id, action, completed_at FROM completions"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["task_id"] == tid
        assert rows[0]["action"] == "done"
        assert rows[0]["completed_at"].startswith(today_str())

        # 冪等: 再実行しても completions は増えない
        result2 = run_sync(conn, fake)
        assert result2["completed_chores"] == 0
        count = conn.execute("SELECT COUNT(*) AS c FROM completions").fetchone()
        assert count["c"] == 1

    def test_undo_reverts_stale_google_completed(self, vault_file, conn, fake):
        """取り消し墓標より古い Google 側 completed は needsAction に戻し、再記録しない。"""
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz

        (tid,) = self._prepare(vault_file, conn, ["風呂掃除"])
        run_sync(conn, fake)
        g = fake.task_by_title(gtasks.CHORE_LIST_TITLE, "風呂掃除")
        # スマホで完了（1時間前）→ kajiflow 側で取り消し（墓標の方が新しい）
        g["status"] = "completed"
        g["completed"] = (_dt.now(_tz.utc) - _td(hours=1)).isoformat()
        conn.execute(
            "INSERT INTO completions (task_id, completed_at, action) VALUES (?, ?, 'undo')",
            (tid, _dt.now(gtasks.JST).isoformat()),
        )
        conn.commit()

        result = run_sync(conn, fake)

        assert g["status"] == "needsAction"  # push フェーズが Google 側を戻した
        assert result["completed_chores"] == 0
        done = conn.execute(
            "SELECT COUNT(*) AS c FROM completions WHERE action = 'done'"
        ).fetchone()
        assert done["c"] == 0  # pull が再完了を記録していない

    def test_recomplete_after_undo_records_done(self, vault_file, conn, fake):
        """取り消し後にスマホで改めて完了（completed が墓標より新しい）→ 通常どおり記録。"""
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz

        (tid,) = self._prepare(vault_file, conn, ["風呂掃除"])
        run_sync(conn, fake)
        g = fake.task_by_title(gtasks.CHORE_LIST_TITLE, "風呂掃除")
        conn.execute(
            "INSERT INTO completions (task_id, completed_at, action) VALUES (?, ?, 'undo')",
            (tid, _dt.now(gtasks.JST).isoformat()),
        )
        conn.commit()
        g["status"] = "completed"
        g["completed"] = (_dt.now(_tz.utc) + _td(hours=1)).isoformat()

        result = run_sync(conn, fake)

        assert g["status"] == "completed"  # push は戻さない
        assert result["completed_chores"] == 1
        done = conn.execute(
            "SELECT COUNT(*) AS c FROM completions WHERE action = 'done'"
        ).fetchone()
        assert done["c"] == 1

    def test_locally_done_chore_marks_google_completed(self, vault_file, conn, fake):
        (tid,) = self._prepare(vault_file, conn, ["風呂掃除"])
        run_sync(conn, fake)
        # kajiflow 側で完了記録（complete API 相当）
        conn.execute(
            "INSERT INTO completions (task_id, completed_at, action) "
            "VALUES (?, ?, 'done')",
            (tid, now_jst().isoformat()),
        )
        conn.commit()

        result = run_sync(conn, fake)

        g = fake.task_by_title(gtasks.CHORE_LIST_TITLE, "風呂掃除")
        assert g["status"] == "completed"
        assert result["completed_chores"] == 0  # pull で二重記録しない

    def test_day_rollover_removes_yesterday_from_google(
        self, vault_file, conn, fake
    ):
        # 前日に同期された chore は、日替わり後の同期で Google 側から削除され、
        # 当日分だけが残る（リストは常に「今日」を映す）
        write_vault(vault_file)
        tid = add_chore_task(conn, "風呂掃除")
        now = now_jst()
        yesterday = now - timedelta(days=1)
        y_str = yesterday.date().isoformat()
        set_plan(conn, y_str, [tid])

        gtasks.sync_all(conn, fake, yesterday)  # 前日の同期
        assert f"{y_str}:{tid}" in get_links(conn, "chore")
        y_task = fake.task_by_title(gtasks.CHORE_LIST_TITLE, "風呂掃除")
        assert y_task["due"] == f"{y_str}T00:00:00.000Z"

        set_plan(conn, today_str(), [tid])
        gtasks.sync_all(conn, fake, now)  # 日替わり後の同期

        links = get_links(conn, "chore")
        assert set(links) == {f"{today_str()}:{tid}"}  # 前日リンクは削除
        chores = fake.tasks_in(gtasks.CHORE_LIST_TITLE)
        assert len(chores) == 1  # 前日タスクは Google 側から消えている
        assert chores[0]["due"] == f"{today_str()}T00:00:00.000Z"
        assert chores[0]["status"] == "needsAction"


# ---------------------------------------------------------------- gomi calendar（v4）

def set_gomi_calendar(conn, calendar_id: str = "gomi@example.com") -> str:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (gtasks.GOMI_CALENDAR_SETTING, calendar_id),
    )
    conn.commit()
    return calendar_id


def gomi_rows(conn) -> set[tuple[str, str]]:
    return {
        (r["date"], r["summary"])
        for r in conn.execute("SELECT date, summary FROM gomi_events").fetchall()
    }


def calendar_tasks(conn) -> list[dict]:
    return [
        dict(r) for r in conn.execute(
            "SELECT * FROM tasks WHERE schedule_type = 'calendar' ORDER BY id"
        ).fetchall()
    ]


class TestGomiCalendar:
    def test_disabled_when_calendar_id_empty(self, vault_file, conn, fake):
        # settings に calendar_id が無ければ機能全体が無効（取得もしない）
        write_vault(vault_file)
        result = run_sync(conn, fake)
        assert result["warnings"] == []
        assert fake.gomi_calls == []
        assert gomi_rows(conn) == set()
        assert calendar_tasks(conn) == []

    def test_refresh_and_synthetic_task_upsert(self, vault_file, conn, fake):
        write_vault(vault_file)
        cid = set_gomi_calendar(conn)
        today = today_str()
        later = (now_jst() + timedelta(days=2)).date().isoformat()
        fake.gomi_events = [
            {"date": today, "summary": "燃えるゴミ"},
            {"date": later, "summary": "プラスチック"},
        ]

        result = run_sync(conn, fake)

        assert result["warnings"] == []
        # [今日, 今日+7日] の窓で取得している
        assert fake.gomi_calls == [
            (cid, now_jst().date(), now_jst().date() + timedelta(days=7))
        ]
        assert gomi_rows(conn) == {
            (today, "燃えるゴミ"), (later, "プラスチック"),
        }
        tasks = calendar_tasks(conn)
        assert [t["name"] for t in tasks] == [
            "ゴミ出し: プラスチック", "ゴミ出し: 燃えるゴミ",
        ]
        for t in tasks:
            assert t["category"] == "ゴミ"
            assert t["est_minutes"] == 5
            assert t["schedule_type"] == "calendar"
            assert t["adaptive"] == 0
            assert t["enabled"] == 1

    def test_refresh_is_idempotent(self, vault_file, conn, fake):
        write_vault(vault_file)
        set_gomi_calendar(conn)
        fake.gomi_events = [{"date": today_str(), "summary": "燃えるゴミ"}]
        run_sync(conn, fake)
        rows_before = gomi_rows(conn)
        tasks_before = calendar_tasks(conn)

        result2 = run_sync(conn, fake)

        assert_noop(result2)
        assert gomi_rows(conn) == rows_before
        assert calendar_tasks(conn) == tasks_before  # 既存同名はそのまま

    def test_stale_rows_are_replaced(self, vault_file, conn, fake):
        # 過去行・イベントから消えた期間内の行はどちらも洗い替えで消える。
        # イベントに現れなくなっても task 行は消さない。
        write_vault(vault_file)
        set_gomi_calendar(conn)
        fake.gomi_events = [{"date": today_str(), "summary": "燃えるゴミ"}]
        run_sync(conn, fake)
        yesterday = (now_jst() - timedelta(days=1)).date().isoformat()
        conn.execute(
            "INSERT INTO gomi_events (date, summary) VALUES (?, ?)",
            (yesterday, "古紙"),
        )
        conn.commit()
        fake.gomi_events = []  # カレンダー側からイベントが消えた

        run_sync(conn, fake)

        assert gomi_rows(conn) == set()
        assert [t["name"] for t in calendar_tasks(conn)] == ["ゴミ出し: 燃えるゴミ"]

    def test_fetch_failure_goes_to_warnings_and_sync_continues(
        self, vault_file, conn
    ):
        write_vault(vault_file)
        failing = GomiFailingClient(
            gtasks.GTasksApiError("Calendar API 呼び出しに失敗しました（HTTP 500）")
        )
        set_gomi_calendar(conn)

        result = run_sync(conn, failing)

        assert any(
            "ゴミカレンダーの取得に失敗しました" in w for w in result["warnings"]
        )
        assert result["pushed"] == 2  # vault 同期は従来どおり動く

    def test_load_credentials_keeps_token_scopes(self, tmp_path, monkeypatch):
        # v3 時代の tasks のみの token に新 SCOPES を強制しない。
        # （from_authorized_user_file に SCOPES を渡すと refresh が
        # invalid_scope で落ちて GTasksClient の生成自体が失敗し、
        # 家事・Vault 同期まで巻き込んで 503 になる。SPEC は「カレンダー
        # 取得だけを諦めて warnings」の劣化動作を要求している）
        token = tmp_path / "gtasks_token.json"
        token.write_text(json.dumps({
            "token": "dummy-access-token",
            "refresh_token": "dummy-refresh-token",
            "client_id": "dummy-client-id",
            "client_secret": "dummy-client-secret",
            "scopes": ["https://www.googleapis.com/auth/tasks"],
            "expiry": "2099-01-01T00:00:00Z",  # 未失効 → refresh せず返る
        }), encoding="utf-8")
        monkeypatch.setattr(gtasks, "TOKEN_PATH", token)

        creds = gtasks.GTasksClient._load_credentials()

        assert set(creds.scopes) == {"https://www.googleapis.com/auth/tasks"}

    def test_list_gomi_events_scope_check_before_network(self):
        # tasks のみのスコープでは Calendar API を呼ぶ前に再認証案内で落とす
        # （calendar service を作らない = ネットワークに一切触れない）
        class TasksOnlyCreds:
            scopes = ["https://www.googleapis.com/auth/tasks"]

        client = object.__new__(gtasks.GTasksClient)
        client._creds = TasksOnlyCreds()
        client._calendar_service = None

        with pytest.raises(gtasks.GTasksAuthError) as ei:
            client.list_gomi_events(
                "gomi@example.com", now_jst().date(), now_jst().date()
            )
        assert gtasks.CALENDAR_REAUTH_HINT in str(ei.value)
        assert client._calendar_service is None

    def test_reauth_required_goes_to_warnings(self, vault_file, conn):
        # スコープ不足（RefreshError / 403 相当）→ 再認証案内を warnings に集約
        write_vault(vault_file)
        failing = GomiFailingClient(
            gtasks.GTasksAuthError(gtasks.CALENDAR_REAUTH_HINT)
        )
        set_gomi_calendar(conn)

        result = run_sync(conn, failing)

        assert any(
            "カレンダー連動には再認証が必要です" in w for w in result["warnings"]
        )
        assert result["pushed"] == 2


# ---------------------------------------------------------------- API

class TestGtasksApi:
    def test_sync_unauthorized_returns_503_with_hint(
        self, vault_file, client, monkeypatch, tmp_path
    ):
        # token 不在（実 data/ の token は絶対に読まない）
        monkeypatch.setattr(gtasks, "TOKEN_PATH", tmp_path / "no_token.json")

        res = client.post("/api/gtasks/sync")

        assert res.status_code == 503
        assert "scripts/gtasks_auth.py を実行してください" in res.json()["detail"]

    def test_status_unauthorized_before_any_sync(
        self, vault_file, client, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(gtasks, "TOKEN_PATH", tmp_path / "no_token.json")

        res = client.get("/api/gtasks/status")

        assert res.status_code == 200
        assert res.json() == {
            "authorized": False,
            "last_sync_at": None,
            "last_result": None,
        }

    def test_sync_result_and_status_last_sync(
        self, vault_file, client, monkeypatch
    ):
        write_vault(vault_file)
        fake = FakeGTasksClient()
        monkeypatch.setattr(gtasks, "build_client", lambda: fake)
        monkeypatch.setattr(gtasks, "is_authorized", lambda: True)

        res = client.post("/api/gtasks/sync")

        assert res.status_code == 200, res.text
        body = res.json()
        assert body == {
            "pushed": 2,
            "completed_in_vault": 0,
            "completed_chores": 0,
            "new_from_google": 0,
            "warnings": [],
        }

        # /api/gtasks/status に last_sync_at / last_result が保存される
        status = client.get("/api/gtasks/status").json()
        assert status["authorized"] is True
        assert status["last_sync_at"] is not None
        assert status["last_sync_at"].startswith(today_str())
        assert status["last_result"] == body

    def test_sync_is_mutually_exclusive(self, vault_file, client, monkeypatch):
        # 同期の並行実行は insert の二重実行（重複タスク増殖）を招くため、
        # 実行中の同期があれば 409 を即返す（プロセス内 Lock で直列化）
        from app import main as main_mod

        write_vault(vault_file)
        fake = FakeGTasksClient()
        monkeypatch.setattr(gtasks, "build_client", lambda: fake)

        assert main_mod._gtasks_sync_lock.acquire(blocking=False)
        try:
            res = client.post("/api/gtasks/sync")
        finally:
            main_mod._gtasks_sync_lock.release()
        assert res.status_code == 409
        assert "同期は実行中です" in res.json()["detail"]

        # ロック解放後は通常どおり同期できる
        res2 = client.post("/api/gtasks/sync")
        assert res2.status_code == 200
        assert res2.json()["pushed"] == 2

    def test_status_keeps_latest_result(self, vault_file, client, monkeypatch):
        # 2回目の同期（差分ゼロ）の結果で last_result が上書きされる
        write_vault(vault_file)
        fake = FakeGTasksClient()
        monkeypatch.setattr(gtasks, "build_client", lambda: fake)

        first = client.post("/api/gtasks/sync").json()
        assert first["pushed"] == 2
        second = client.post("/api/gtasks/sync").json()
        assert second["pushed"] == 0

        status = client.get("/api/gtasks/status").json()
        assert status["last_result"] == second
