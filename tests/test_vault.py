"""app/vault.py の Vault タスク連携テスト（SPEC「Vault タスク連携（v2）」節）。

- パーサ: 実ファイルを模した合成 Markdown（テスト内フィクスチャ）→ タスク dict
- 書き戻し: 一時ファイルでの安全規則（[ ]→[x]・完了行挿入・他行不変・
  曖昧一致で中断・ハンドオフ拒否・CRLF/LF 維持）
- API: sync → 一覧 → classify（再同期後も保持）→ complete → ファイル反映

実在の Vault は一切読み書きしない。パスは必ず環境変数
KAJIFLOW_VAULT / KAJIFLOW_VAULT_TASKFILE で tmp_path に差し替える。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app import db, vault
from app.engine import JST

# ---------------------------------------------------------------- フィクスチャ

# 実ファイルのフォーマット（SPEC「正本ファイルのフォーマット（実測）」）を模した合成 Markdown
SAMPLE_MD = """\
# AIエージェント連携タスク

## 進行中

- [ ] kaggleコンペのベースライン実装
  - 追加日: 2026-08-10
  - Project: kaggle
  - 出所: 2026-08-10の会話
  - 目的: 提出可能なスクリプト作成
  - 完了条件: submission.csv が生成される
  - 期限: 2026-08-20
  - input: data/train.csv
- [ ] キャリアの振り返りを書く
  - 追加日: 2026-08-11
  - 目的: 本人の言葉で語り直す

## ハンドオフ

- [ ] コードレビュー対応
  - id: T20260816-203634-cbd6
  - owner: codex
  - status: open
  - handoff-to: claude
  - 目的: PR レビュー

## 完了

- [x] 過去ログの整理
  - 追加日: 2026-08-01
  - 完了: 2026-08-05、手動で完了
"""

HANDOFF_UID = "T20260816-203634-cbd6"

# 書き戻しテスト用（バイト比較で expected を組み立てやすい最小構成）
WRITEBACK_MD = (
    "# AIエージェント連携タスク\n"
    "\n"
    "## 進行中\n"
    "\n"
    "- [ ] 風呂の防カビ剤を調べる\n"
    "  - 追加日: 2026-08-10\n"
    "  - 目的: 手間を減らす\n"
    "- [ ] 次のタスク\n"
    "  - 追加日: 2026-08-11\n"
    "\n"
    "備考: 末尾のメモ行\n"
)

# 同名タスクが2件ある（曖昧一致）ケース
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

# 同名タスクが後からハンドオフチケット化されるケース
# （旧 title-hash 行が DB に残り、その uid で complete される経路の検証用）
PLAIN_REVIEW_MD = (
    "## ハンドオフ\n"
    "\n"
    "- [ ] コードレビュー対応\n"
)
TICKETIZED_REVIEW_MD = (
    "## ハンドオフ\n"
    "\n"
    "- [ ] コードレビュー対応\n"
    "  - id: T20260817-000000-abcd\n"
    "  - owner: codex\n"
    "  - status: open\n"
    "  - handoff-to: claude\n"
)


def today_str() -> str:
    return datetime.now(JST).date().isoformat()


@pytest.fixture
def vault_file(tmp_path, monkeypatch):
    """一時 Vault のタスクファイルパス。

    KAJIFLOW_VAULT / KAJIFLOW_VAULT_TASKFILE を tmp_path に向ける
    （実在の Vault を読み書きしないための必須フィクスチャ）。
    """
    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.setenv("KAJIFLOW_VAULT", str(root))
    monkeypatch.setenv("KAJIFLOW_VAULT_TASKFILE", "AIエージェント連携タスク.md")
    return root / "AIエージェント連携タスク.md"


@pytest.fixture
def conn(tmp_path):
    """vault モジュールを直接呼ぶテスト用の一時 DB 接続。"""
    c = db.connect(tmp_path / "kajiflow_vault_test.db")
    yield c
    c.close()


# ---------------------------------------------------------------- パーサ

class TestParser:
    def test_open_and_done_tasks(self):
        tasks = vault.parse_tasks(SAMPLE_MD)
        assert len(tasks) == 4
        by_title = {t["title"]: t for t in tasks}
        assert by_title["kaggleコンペのベースライン実装"]["checked"] == 0
        assert by_title["キャリアの振り返りを書く"]["checked"] == 0
        assert by_title["過去ログの整理"]["checked"] == 1

    def test_sections(self):
        tasks = vault.parse_tasks(SAMPLE_MD)
        sections = {t["title"]: t["section"] for t in tasks}
        assert sections["kaggleコンペのベースライン実装"] == "進行中"
        assert sections["コードレビュー対応"] == "ハンドオフ"
        assert sections["過去ログの整理"] == "完了"

    def test_meta_extraction(self):
        t = vault.parse_tasks(SAMPLE_MD)[0]
        assert t["project"] == "kaggle"
        assert t["due"] == "2026-08-20"
        assert t["purpose"] == "提出可能なスクリプト作成"
        assert t["done_when"] == "submission.csv が生成される"
        assert t["source"] == "2026-08-10の会話"
        assert t["meta"]["input"] == "data/train.csv"
        assert t["meta"]["追加日"] == "2026-08-10"

    def test_uid_for_normal_task_is_title_hash(self):
        t = vault.parse_tasks(SAMPLE_MD)[0]
        assert t["is_handoff"] == 0
        assert t["uid"] == vault.title_uid("kaggleコンペのベースライン実装")
        assert len(t["uid"]) == 16

    def test_uid_for_handoff_is_ticket_id(self):
        by_title = {t["title"]: t for t in vault.parse_tasks(SAMPLE_MD)}
        t = by_title["コードレビュー対応"]
        assert t["is_handoff"] == 1
        assert t["uid"] == HANDOFF_UID
        assert t["handoff_owner"] == "codex"
        assert t["handoff_status"] == "open"

    def test_normal_task_has_no_handoff_fields(self):
        t = vault.parse_tasks(SAMPLE_MD)[0]
        assert t["handoff_owner"] == ""
        assert t["handoff_status"] == ""

    def test_title_uid_normalization(self):
        # NFKC + 空白圧縮で同一タイトルとみなす
        assert vault.title_uid("タスク　Ａ") == vault.title_uid("タスク A")
        assert vault.title_uid("  タスク A  ") == vault.title_uid("タスク A")

    def test_sub_items_and_meta_block_end(self):
        text = (
            "## テスト\n"
            "\n"
            "- [ ] タスクA\n"
            "  - 目的: 検証\n"
            "    - サブ項目: 無視される\n"
            "  - 期限: 2026-09-01\n"
            "  - artifacts:\n"
            "\n"
            "段落テキスト\n"
            "  - これはメタブロック外なので拾わない\n"
        )
        tasks = vault.parse_tasks(text)
        assert len(tasks) == 1
        t = tasks[0]
        # インデント4のサブ項目は無視しつつ、後続のインデント2メタは拾う
        assert "サブ項目" not in t["meta"]
        assert t["due"] == "2026-09-01"
        assert t["meta"]["artifacts"] == ""  # 値なしメタも拾う
        assert "これはメタブロック外なので拾わない" not in str(t["meta"])

    def test_file_title_heading_is_not_a_section(self):
        # `# ` のファイルタイトルは section にしない
        text = "# ファイルタイトル\n\n- [ ] タスクA\n"
        assert vault.parse_tasks(text)[0]["section"] == ""


# ---------------------------------------------------------------- 分類ヒューリスティック

class TestSuggestClassification:
    @pytest.mark.parametrize("title, purpose, expected", [
        ("ログ集計スクリプト作成", "", "ai"),
        ("週次データのスクリーニング", "", "ai"),
        ("自分の言葉で書く", "", "user"),
        ("キャリアの振り返り", "", "user"),
        ("実装方針を見直す", "", None),   # AI 寄り+ユーザー寄りの両方該当 → None
        ("散歩する", "", None),           # どちらも非該当 → None
        ("タスク", "データ分析をする", "ai"),      # 目的も判定対象
        ("タスク", "所有権を確認する", "user"),
    ])
    def test_keywords(self, title, purpose, expected):
        assert vault.suggest_classification(title, purpose) == expected


# ---------------------------------------------------------------- 書き戻し

class TestWriteBack:
    def _sync(self, vault_file, conn, text: str) -> None:
        vault_file.write_bytes(text.encode("utf-8"))
        vault.sync_vault(conn)

    def test_complete_rewrites_only_target_lines(self, vault_file, conn):
        self._sync(vault_file, conn, WRITEBACK_MD)
        uid = vault.title_uid("風呂の防カビ剤を調べる")

        result = vault.complete_in_vault(conn, uid)
        assert result["ok"] is True

        # [ ]→[x] とメタブロック末尾への完了行1行挿入のみ。他行は1バイトも変わらない
        expected = WRITEBACK_MD.replace(
            "- [ ] 風呂の防カビ剤を調べる\n"
            "  - 追加日: 2026-08-10\n"
            "  - 目的: 手間を減らす\n",
            "- [x] 風呂の防カビ剤を調べる\n"
            "  - 追加日: 2026-08-10\n"
            "  - 目的: 手間を減らす\n"
            f"  - 完了: {today_str()}、KajiFlowから完了\n",
        )
        data = vault_file.read_bytes()
        assert data == expected.encode("utf-8")
        assert b"\r" not in data  # LF ファイルは LF のまま

    def test_complete_with_note(self, vault_file, conn):
        self._sync(vault_file, conn, WRITEBACK_MD)
        uid = vault.title_uid("風呂の防カビ剤を調べる")

        vault.complete_in_vault(conn, uid, note="ドラッグストアで購入\n2本")

        text = vault_file.read_bytes().decode("utf-8")
        # note は「。{note}」形式で1行に収める
        assert (
            f"  - 完了: {today_str()}、KajiFlowから完了。ドラッグストアで購入 2本\n"
            in text
        )

    def test_crlf_file_stays_crlf(self, vault_file, conn):
        crlf_md = WRITEBACK_MD.replace("\n", "\r\n")
        self._sync(vault_file, conn, crlf_md)
        uid = vault.title_uid("風呂の防カビ剤を調べる")

        vault.complete_in_vault(conn, uid)

        expected = WRITEBACK_MD.replace(
            "- [ ] 風呂の防カビ剤を調べる\n"
            "  - 追加日: 2026-08-10\n"
            "  - 目的: 手間を減らす\n",
            "- [x] 風呂の防カビ剤を調べる\n"
            "  - 追加日: 2026-08-10\n"
            "  - 目的: 手間を減らす\n"
            f"  - 完了: {today_str()}、KajiFlowから完了\n",
        ).replace("\n", "\r\n")
        data = vault_file.read_bytes()
        assert data == expected.encode("utf-8")
        assert data.count(b"\n") == data.count(b"\r\n")  # 裸の LF が混ざらない

    def test_complete_at_eof_without_trailing_newline(self, vault_file, conn):
        text = "- [ ] 最終行タスク\n  - 目的: 検証"  # 改行なしで終わるファイル
        self._sync(vault_file, conn, text)
        uid = vault.title_uid("最終行タスク")

        vault.complete_in_vault(conn, uid)

        expected = (
            "- [x] 最終行タスク\n"
            "  - 目的: 検証\n"
            f"  - 完了: {today_str()}、KajiFlowから完了\n"
        )
        assert vault_file.read_bytes() == expected.encode("utf-8")

    def test_ambiguous_duplicate_title_aborts_without_writing(self, vault_file, conn):
        self._sync(vault_file, conn, DUP_MD)
        uid = vault.title_uid("同名タスク")
        before = vault_file.read_bytes()

        with pytest.raises(vault.VaultConflictError):
            vault.complete_in_vault(conn, uid)

        assert vault_file.read_bytes() == before  # 1バイトも書き込まない

    def test_zero_match_aborts_without_writing(self, vault_file, conn):
        self._sync(vault_file, conn, WRITEBACK_MD)
        uid = vault.title_uid("風呂の防カビ剤を調べる")
        # 同期後に Vault 側で先に完了された（[ ] 行が消えた）状況
        changed = WRITEBACK_MD.replace(
            "- [ ] 風呂の防カビ剤を調べる", "- [x] 風呂の防カビ剤を調べる"
        )
        vault_file.write_bytes(changed.encode("utf-8"))

        with pytest.raises(vault.VaultConflictError):
            vault.complete_in_vault(conn, uid)

        assert vault_file.read_bytes() == changed.encode("utf-8")

    def test_handoff_is_rejected_with_command(self, vault_file, conn):
        self._sync(vault_file, conn, SAMPLE_MD)
        before = vault_file.read_bytes()

        with pytest.raises(vault.VaultConflictError) as ei:
            vault.complete_in_vault(conn, HANDOFF_UID)

        assert ei.value.command is not None
        assert "agent_handoff.py" in ei.value.command
        assert f"complete {HANDOFF_UID}" in ei.value.command
        assert vault_file.read_bytes() == before
        # DB 側も未完了のまま
        task = vault.fetch_vault_task(conn, HANDOFF_UID)
        assert task["checked"] == 0
        assert task["completed_via"] is None

    def test_stale_uid_of_ticketized_task_is_rejected(self, vault_file, conn):
        # メタなしの同名タスクを同期 → その後チケット化されて再同期。
        # 旧 title-hash 行（is_handoff=0）が DB に残るが、その uid で complete
        # してもファイル側のメタで再検証してチケット本文には書き込まない
        self._sync(vault_file, conn, PLAIN_REVIEW_MD)
        self._sync(vault_file, conn, TICKETIZED_REVIEW_MD)
        stale_uid = vault.title_uid("コードレビュー対応")
        before = vault_file.read_bytes()

        with pytest.raises(vault.VaultConflictError) as ei:
            vault.complete_in_vault(conn, stale_uid)

        assert "agent_handoff.py" in (ei.value.command or "")
        assert "complete T20260817-000000-abcd" in ei.value.command
        assert vault_file.read_bytes() == before  # チケット本文は1バイトも変えない
        # DB 側も未完了のまま
        task = vault.fetch_vault_task(conn, stale_uid)
        assert task["checked"] == 0
        assert task["completed_via"] is None

    def test_write_failure_raises_write_error_and_keeps_db(
        self, vault_file, conn, monkeypatch
    ):
        # os.replace 失敗（OneDrive ロック等）は VaultWriteError（日本語メッセージ）。
        # 正本は無傷で、DB も未完了のまま
        self._sync(vault_file, conn, WRITEBACK_MD)
        uid = vault.title_uid("風呂の防カビ剤を調べる")

        def _fail(src, dst):
            raise PermissionError("locked by OneDrive")

        monkeypatch.setattr(vault.os, "replace", _fail)
        with pytest.raises(vault.VaultWriteError) as ei:
            vault.complete_in_vault(conn, uid)

        assert "書き込めません" in str(ei.value)
        assert vault_file.read_bytes() == WRITEBACK_MD.encode("utf-8")
        task = vault.fetch_vault_task(conn, uid)
        assert task["checked"] == 0
        assert task["completed_via"] is None

    def test_complete_leaves_no_tmp_file(self, vault_file, conn):
        self._sync(vault_file, conn, WRITEBACK_MD)
        uid = vault.title_uid("風呂の防カビ剤を調べる")

        vault.complete_in_vault(conn, uid)

        leftovers = [p.name for p in vault_file.parent.iterdir()
                     if p.name != vault_file.name]
        assert leftovers == []  # 一時ファイルを残さない

    def test_complete_updates_db_state(self, vault_file, conn):
        self._sync(vault_file, conn, WRITEBACK_MD)
        uid = vault.title_uid("風呂の防カビ剤を調べる")

        vault.complete_in_vault(conn, uid)

        task = vault.fetch_vault_task(conn, uid)
        assert task["checked"] == 1
        assert task["completed_via"] is not None

    def test_unknown_uid_raises_not_found(self, vault_file, conn):
        self._sync(vault_file, conn, WRITEBACK_MD)
        with pytest.raises(vault.VaultNotFoundError):
            vault.complete_in_vault(conn, "ffffffffffffffff")

    def test_missing_file_raises_read_error(self, vault_file, conn):
        # ファイル未作成のまま読むと日本語メッセージの VaultReadError
        with pytest.raises(vault.VaultReadError) as ei:
            vault.read_taskfile()
        assert "見つかりません" in str(ei.value)


# ---------------------------------------------------------------- API

class TestVaultApi:
    def _sync(self, client, vault_file, text: str = SAMPLE_MD) -> dict:
        vault_file.write_bytes(text.encode("utf-8"))
        res = client.post("/api/vault/sync")
        assert res.status_code == 200, res.text
        return res.json()

    def test_sync_counts(self, vault_file, client):
        body = self._sync(client, vault_file)
        assert body == {"open": 3, "done": 1, "new_count": 4, "updated": 0}
        # 変更なしの再同期は new_count / updated ともに 0
        again = client.post("/api/vault/sync").json()
        assert again == {"open": 3, "done": 1, "new_count": 0, "updated": 0}

    def test_sync_counts_updated_on_meta_change(self, vault_file, client):
        self._sync(client, vault_file)
        edited = SAMPLE_MD.replace("提出可能なスクリプト作成", "提出可能なスクリプト作成v2")
        body = self._sync(client, vault_file, edited)
        assert body["new_count"] == 0
        assert body["updated"] == 1

    def test_sync_missing_file_returns_503(self, vault_file, client):
        res = client.post("/api/vault/sync")  # ファイル未作成
        assert res.status_code == 503
        assert "見つかりません" in res.json()["detail"]

    def test_list_default_excludes_done(self, vault_file, client):
        self._sync(client, vault_file)
        tasks = client.get("/api/vault/tasks").json()
        titles = [t["title"] for t in tasks]
        assert titles == [
            "kaggleコンペのベースライン実装",
            "キャリアの振り返りを書く",
            "コードレビュー対応",
        ]
        all_tasks = client.get("/api/vault/tasks", params={"include_done": 1}).json()
        assert len(all_tasks) == 4

    def test_list_fields(self, vault_file, client):
        self._sync(client, vault_file)
        t = client.get("/api/vault/tasks").json()[0]
        assert t["uid"] == vault.title_uid("kaggleコンペのベースライン実装")
        assert t["section"] == "進行中"
        assert t["project"] == "kaggle"
        assert t["due"] == "2026-08-20"
        assert t["purpose"] == "提出可能なスクリプト作成"
        assert t["done_when"] == "submission.csv が生成される"
        assert t["is_handoff"] == 0
        assert t["classification"] is None
        assert t["checked"] == 0

    def test_suggested_heuristics_in_list(self, vault_file, client):
        self._sync(client, vault_file)
        by_title = {t["title"]: t for t in client.get("/api/vault/tasks").json()}
        assert by_title["kaggleコンペのベースライン実装"]["suggested"] == "ai"
        assert by_title["キャリアの振り返りを書く"]["suggested"] == "user"

    def test_classify_persists_after_resync(self, vault_file, client):
        self._sync(client, vault_file)
        uid = vault.title_uid("kaggleコンペのベースライン実装")

        # 提案は ai だが、ユーザーが user に確定
        res = client.put(f"/api/vault/tasks/{uid}/classify",
                         json={"classification": "user"})
        assert res.status_code == 200
        assert res.json()["classification"] == "user"

        # Vault 側でメタが変わって再同期しても classification / suggested は保持
        edited = SAMPLE_MD.replace("提出可能なスクリプト作成", "提出可能なスクリプト作成v2")
        self._sync(client, vault_file, edited)
        by_title = {t["title"]: t for t in client.get("/api/vault/tasks").json()}
        t = by_title["kaggleコンペのベースライン実装"]
        assert t["classification"] == "user"
        assert t["suggested"] == "ai"

    def test_classify_null_clears(self, vault_file, client):
        self._sync(client, vault_file)
        uid = vault.title_uid("キャリアの振り返りを書く")
        client.put(f"/api/vault/tasks/{uid}/classify", json={"classification": "ai"})
        res = client.put(f"/api/vault/tasks/{uid}/classify",
                         json={"classification": None})
        assert res.status_code == 200
        assert res.json()["classification"] is None

    def test_classify_validation(self, vault_file, client):
        self._sync(client, vault_file)
        uid = vault.title_uid("キャリアの振り返りを書く")
        res = client.put(f"/api/vault/tasks/{uid}/classify",
                         json={"classification": "robot"})
        assert res.status_code == 422
        res = client.put("/api/vault/tasks/ffffffffffffffff/classify",
                         json={"classification": "ai"})
        assert res.status_code == 404

    def test_complete_flow_reflects_vault_file(self, vault_file, client):
        self._sync(client, vault_file)
        uid = vault.title_uid("キャリアの振り返りを書く")

        res = client.post(f"/api/vault/tasks/{uid}/complete",
                          json={"note": "メモです"})
        assert res.status_code == 200
        assert res.json()["ok"] is True

        text = vault_file.read_bytes().decode("utf-8")
        assert "- [x] キャリアの振り返りを書く" in text
        assert f"  - 完了: {today_str()}、KajiFlowから完了。メモです" in text

        # 既定一覧から消え、include_done=1 では checked=1 で見える
        titles = [t["title"] for t in client.get("/api/vault/tasks").json()]
        assert "キャリアの振り返りを書く" not in titles
        by_title = {
            t["title"]: t
            for t in client.get("/api/vault/tasks",
                                params={"include_done": 1}).json()
        }
        assert by_title["キャリアの振り返りを書く"]["checked"] == 1

    def test_complete_without_body(self, vault_file, client):
        self._sync(client, vault_file)
        uid = vault.title_uid("キャリアの振り返りを書く")
        res = client.post(f"/api/vault/tasks/{uid}/complete")
        assert res.status_code == 200
        text = vault_file.read_bytes().decode("utf-8")
        assert f"  - 完了: {today_str()}、KajiFlowから完了\n" in text

    def test_complete_handoff_returns_409_with_command(self, vault_file, client):
        self._sync(client, vault_file)
        res = client.post(f"/api/vault/tasks/{HANDOFF_UID}/complete")
        assert res.status_code == 409
        detail = res.json()["detail"]
        assert "agent_handoff.py" in detail["command"]
        assert f"complete {HANDOFF_UID}" in detail["command"]
        # ファイルは無変更
        assert vault_file.read_bytes() == SAMPLE_MD.encode("utf-8")

    def test_complete_ambiguous_returns_409(self, vault_file, client):
        self._sync(client, vault_file, DUP_MD)
        uid = vault.title_uid("同名タスク")
        res = client.post(f"/api/vault/tasks/{uid}/complete")
        assert res.status_code == 409
        assert vault_file.read_bytes() == DUP_MD.encode("utf-8")

    def test_complete_stale_uid_of_ticketized_task_returns_409(
        self, vault_file, client
    ):
        # 同期前の古い画面から旧 title-hash uid で完了を押した場合も
        # チケット本文には書き込まず 409（agent_handoff.py コマンド付き）
        self._sync(client, vault_file, PLAIN_REVIEW_MD)
        self._sync(client, vault_file, TICKETIZED_REVIEW_MD)
        stale_uid = vault.title_uid("コードレビュー対応")

        res = client.post(f"/api/vault/tasks/{stale_uid}/complete")

        assert res.status_code == 409
        detail = res.json()["detail"]
        assert "complete T20260817-000000-abcd" in detail["command"]
        assert vault_file.read_bytes() == TICKETIZED_REVIEW_MD.encode("utf-8")

    def test_complete_write_failure_returns_503(
        self, vault_file, client, monkeypatch
    ):
        self._sync(client, vault_file)
        uid = vault.title_uid("キャリアの振り返りを書く")

        def _fail(src, dst):
            raise PermissionError("locked by OneDrive")

        monkeypatch.setattr(vault.os, "replace", _fail)
        res = client.post(f"/api/vault/tasks/{uid}/complete")

        assert res.status_code == 503
        assert "書き込めません" in res.json()["detail"]
        assert vault_file.read_bytes() == SAMPLE_MD.encode("utf-8")

    def test_complete_unknown_uid_returns_404(self, vault_file, client):
        self._sync(client, vault_file)
        res = client.post("/api/vault/tasks/ffffffffffffffff/complete")
        assert res.status_code == 404

    def test_prompt(self, vault_file, client):
        self._sync(client, vault_file)
        uid = vault.title_uid("kaggleコンペのベースライン実装")
        res = client.get(f"/api/vault/tasks/{uid}/prompt")
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("text/plain")
        assert res.text == (
            "次のタスクを実行してください。\n"
            "\n"
            "タスク: kaggleコンペのベースライン実装\n"
            "目的: 提出可能なスクリプト作成\n"
            "完了条件: submission.csv が生成される\n"
            "出所: 2026-08-10の会話\n"
            "input: data/train.csv\n"
            "\n"
            "完了条件を満たしたら、結果と生成物の場所を報告してください。\n"
        )

    def test_prompt_unknown_uid_returns_404(self, vault_file, client):
        self._sync(client, vault_file)
        assert client.get("/api/vault/tasks/ffffffffffffffff/prompt").status_code == 404

    def test_removed_task_hidden_after_resync(self, vault_file, client):
        self._sync(client, vault_file)
        # ファイルから1件消して再同期 → 既定一覧に出ない（削除はしない）
        edited = SAMPLE_MD.replace(
            "- [ ] キャリアの振り返りを書く\n"
            "  - 追加日: 2026-08-11\n"
            "  - 目的: 本人の言葉で語り直す\n",
            "",
        )
        self._sync(client, vault_file, edited)
        titles = [t["title"] for t in client.get("/api/vault/tasks").json()]
        assert "キャリアの振り返りを書く" not in titles

    def test_all_tasks_removed_disappear_from_list(self, vault_file, client):
        # 空同期（タスク0件）でも settings の vault_last_sync_at が進むため、
        # ファイルから消えたタスクは一覧に残らない

        self._sync(client, vault_file)
        self._sync(client, vault_file, "# AIエージェント連携タスク\n")
        assert client.get("/api/vault/tasks").json() == []
