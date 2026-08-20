"""Vault タスク連携（v2）。

Obsidian Vault のタスク正本 `00_Inbox/AIエージェント連携タスク.md` を読み取り、
vault_tasks テーブルへ同期する。Vault ファイルが正本で、kajiflow 側は
分類（classification）と完了書き戻しだけを担う。

- parse_tasks: Markdown テキスト → タスク dict のリスト（純ロジック）
- sync_vault: ファイル読取 → パース → upsert（classification 等のローカル状態は保持）
- complete_in_vault: SPEC の安全規則 1〜6 に従う完了書き戻し
- suggest_classification: キーワードによる 'ai' | 'user' | None の分類提案

パスは環境変数 KAJIFLOW_VAULT / KAJIFLOW_VAULT_TASKFILE で差し替え可能
（テストは必ずこの2変数で一時ファイルへ差し替えること）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import unicodedata
from datetime import datetime
from pathlib import Path

from .engine import JST

DEFAULT_TASKFILE_REL = "00_Inbox/AIエージェント連携タスク.md"

# 行頭タスク行: `- [ ] タイトル` / `- [x] タイトル`
TASK_RE = re.compile(r"^- \[([ xX])\] (.+)$")
# インデント2のメタ行: `  - キー: 値`（値なしの `  - artifacts:` も拾う）
META_RE = re.compile(r"^  - ([^:]+?):[ \t]*(.*)$")
# セクション見出し（`# ` のファイルタイトルは含めない）
SECTION_RE = re.compile(r"^#{2,}\s+(.+?)\s*$")
# ハンドオフチケットの id（例: T20260816-203634-cbd6）
HANDOFF_ID_RE = re.compile(r"^T\S+$")

AI_KEYWORDS = (
    "実装", "調査", "分析", "作成", "集計",
    "スクリーニング", "レビュー", "スクリプト", "PR", "デモ",
)
USER_KEYWORDS = (
    "本人の言葉", "所有権", "判断", "見直す",
    "振り返り", "語り", "面接", "書く",
)


# ---------------------------------------------------------------- errors

class VaultError(Exception):
    """Vault 連携の基底エラー。str(e) は日本語メッセージ。"""


class VaultReadError(VaultError):
    """ファイル不在・読取失敗（API では 503）。"""


class VaultWriteError(VaultError):
    """書き戻しの書込失敗（ロック・ディスクフル等。API では 503）。"""


class VaultNotFoundError(VaultError):
    """uid が vault_tasks に無い（API では 404）。"""


class VaultConflictError(VaultError):
    """書き戻し中断（曖昧一致・ハンドオフ。API では 409）。"""

    def __init__(self, message: str, command: str | None = None):
        super().__init__(message)
        self.command = command


# ---------------------------------------------------------------- paths

def get_vault_root() -> Path:
    """Vault ルート。環境変数 KAJIFLOW_VAULT を優先する。"""
    env = os.environ.get("KAJIFLOW_VAULT")
    if env:
        return Path(env)
    home = os.environ.get("USERPROFILE") or str(Path.home())
    return Path(home) / "OneDrive" / "ドキュメント" / "KnowledgeBase"


def get_taskfile_path() -> Path:
    """タスク正本ファイルのフルパス。相対部は KAJIFLOW_VAULT_TASKFILE。"""
    rel = os.environ.get("KAJIFLOW_VAULT_TASKFILE") or DEFAULT_TASKFILE_REL
    return get_vault_root() / rel


def read_taskfile() -> str:
    """タスク正本を UTF-8 で読む。失敗は VaultReadError（日本語メッセージ）。"""
    path = get_taskfile_path()
    try:
        return path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        raise VaultReadError(f"Vault のタスクファイルが見つかりません: {path}")
    except OSError as e:
        raise VaultReadError(f"Vault のタスクファイルを読み取れません: {path}（{e}）")
    except UnicodeDecodeError:
        raise VaultReadError(
            f"Vault のタスクファイルを UTF-8 として読み取れません: {path}"
        )


def write_taskfile(data: bytes) -> None:
    """タスク正本へアトミックに書き込む。失敗は VaultWriteError（日本語メッセージ）。

    正本への直接上書き（open('wb') は先に切り詰める）だと書込途中の
    強制終了・電源断・ディスクフルで正本が壊れるため、同一ディレクトリの
    一時ファイルに書いてから os.replace() で差し替える。
    """
    path = get_taskfile_path()
    tmp = path.with_name(path.name + ".kajiflow.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except OSError as e:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise VaultWriteError(
            f"Vault のタスクファイルに書き込めません: {path}（{e}）。"
            "OneDrive の同期・ロック状態を確認して再試行してください"
        )


# ---------------------------------------------------------------- parse

def normalize_title(title: str) -> str:
    """uid 計算用のタイトル正規化（NFKC + 空白の圧縮）。"""
    t = unicodedata.normalize("NFKC", title).strip()
    return re.sub(r"\s+", " ", t)


def title_uid(title: str) -> str:
    """sha1(正規化タイトル) の先頭16桁。"""
    return hashlib.sha1(normalize_title(title).encode("utf-8")).hexdigest()[:16]


def suggest_classification(title: str, purpose: str = "") -> str | None:
    """タイトル+目的の単純キーワード判定で 'ai' | 'user' | None を返す。

    両方該当・どちらも非該当なら None（あくまで提案。確定は classification）。
    """
    text = f"{title} {purpose}"
    ai = any(k in text for k in AI_KEYWORDS)
    user = any(k in text for k in USER_KEYWORDS)
    if ai == user:
        return None
    return "ai" if ai else "user"


def parse_tasks(text: str) -> list[dict]:
    """タスク正本の Markdown テキストをタスク dict のリストにする。

    - 行頭の `- [ ]` / `- [x]` をタスクとし、直後のインデント2の
      `- キー: 値` をメタとして拾う（インデント4以上のサブ項目は無視）。
    - 空行・非インデント行でメタブロック終了。
    - `## 見出し` を section として付与。
    - uid はハンドオフなら id（T...）、それ以外は sha1(タイトル正規化)先頭16桁。
    - line はタスク行の 0 始まり行番号（書き戻し時の照合に使う）。
    """
    tasks: list[dict] = []
    section = ""
    meta: dict[str, str] | None = None

    for lineno, line in enumerate(text.splitlines()):
        m = SECTION_RE.match(line)
        if m:
            section = m.group(1)
            meta = None
            continue
        m = TASK_RE.match(line)
        if m:
            meta = {}
            tasks.append({
                "title": m.group(2).strip(),
                "section": section,
                "checked": 0 if m.group(1) == " " else 1,
                "line": lineno,
                "meta": meta,
            })
            continue
        if meta is None:
            continue
        if not line.strip() or not line.startswith(" "):
            meta = None  # メタブロック終了
            continue
        mm = META_RE.match(line)
        if mm:
            meta[mm.group(1).strip()] = mm.group(2).strip()

    for t in tasks:
        m = t["meta"]
        ticket_id = m.get("id", "")
        is_handoff = 1 if HANDOFF_ID_RE.match(ticket_id) else 0
        t["uid"] = ticket_id if is_handoff else title_uid(t["title"])
        t["is_handoff"] = is_handoff
        t["project"] = m.get("Project", "")
        t["due"] = m.get("期限", "")
        t["purpose"] = m.get("目的", "")
        t["done_when"] = m.get("完了条件", "")
        t["source"] = m.get("出所", "")
        t["handoff_owner"] = m.get("owner", "") if is_handoff else ""
        t["handoff_status"] = m.get("status", "") if is_handoff else ""
    return tasks


# ---------------------------------------------------------------- sync

# Vault 側の値で上書きするカラム（classification / completed_via は含めない）
_VAULT_COLUMNS = (
    "title", "section", "checked", "project", "due", "purpose",
    "done_when", "source", "is_handoff", "handoff_owner", "handoff_status",
    "meta_json",
)


def sync_vault(conn: sqlite3.Connection) -> dict:
    """ファイル読取 → パース → upsert。

    - タイトル・メタ・checked は Vault 値で上書きする。
    - classification / suggested（既存値優先）/ completed_via は保持する。
    - 今回見つからなかった uid は削除せず last_seen_at を据え置く。

    戻り値: {open, done, new_count, updated}
    """
    parsed = parse_tasks(read_taskfile())
    now = datetime.now(JST).isoformat()
    new_count = 0
    updated = 0

    # 空同期（タスク0件）でも「最新同期」を進められるよう settings に記録する
    conn.execute(
        "INSERT INTO settings (key, value) VALUES ('vault_last_sync_at', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (now,),
    )

    for t in parsed:
        values = {
            "title": t["title"],
            "section": t["section"],
            "checked": t["checked"],
            "project": t["project"],
            "due": t["due"],
            "purpose": t["purpose"],
            "done_when": t["done_when"],
            "source": t["source"],
            "is_handoff": t["is_handoff"],
            "handoff_owner": t["handoff_owner"],
            "handoff_status": t["handoff_status"],
            "meta_json": json.dumps(t["meta"], ensure_ascii=False),
        }
        suggested = suggest_classification(t["title"], t["purpose"])
        row = conn.execute(
            "SELECT * FROM vault_tasks WHERE uid = ?", (t["uid"],)
        ).fetchone()
        if row is None:
            conn.execute(
                """
                INSERT INTO vault_tasks
                  (uid, title, section, checked, project, due, purpose,
                   done_when, source, is_handoff, handoff_owner, handoff_status,
                   suggested, last_seen_at, meta_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    t["uid"], values["title"], values["section"],
                    values["checked"], values["project"], values["due"],
                    values["purpose"], values["done_when"], values["source"],
                    values["is_handoff"], values["handoff_owner"],
                    values["handoff_status"], suggested, now,
                    values["meta_json"],
                ),
            )
            new_count += 1
        else:
            changed = any(row[col] != values[col] for col in _VAULT_COLUMNS)
            keep_suggested = (
                row["suggested"] if row["suggested"] is not None else suggested
            )
            conn.execute(
                """
                UPDATE vault_tasks SET
                  title = ?, section = ?, checked = ?, project = ?, due = ?,
                  purpose = ?, done_when = ?, source = ?, is_handoff = ?,
                  handoff_owner = ?, handoff_status = ?, suggested = ?,
                  last_seen_at = ?, meta_json = ?
                WHERE uid = ?
                """,
                (
                    values["title"], values["section"], values["checked"],
                    values["project"], values["due"], values["purpose"],
                    values["done_when"], values["source"], values["is_handoff"],
                    values["handoff_owner"], values["handoff_status"],
                    keep_suggested, now, values["meta_json"], t["uid"],
                ),
            )
            if changed:
                updated += 1
    conn.commit()
    return {
        "open": sum(1 for t in parsed if not t["checked"]),
        "done": sum(1 for t in parsed if t["checked"]),
        "new_count": new_count,
        "updated": updated,
    }


# ---------------------------------------------------------------- listing

LIST_FIELDS = (
    "uid", "title", "section", "project", "due", "purpose", "done_when",
    "is_handoff", "handoff_owner", "handoff_status",
    "classification", "suggested", "checked",
)


def list_tasks(conn: sqlite3.Connection, include_done: bool = False) -> list[dict]:
    """一覧を返す。既定は未完了のみ。include_done=True で完了も含む。

    直近の同期で見つからなかった行（last_seen_at が最新同期より古い）は返さない。
    最新同期は settings の vault_last_sync_at で判定する（タスク0件の同期でも
    進む）。無ければ後方互換で MAX(last_seen_at) にフォールバックする。
    """
    row = conn.execute(
        "SELECT value FROM settings WHERE key = 'vault_last_sync_at'"
    ).fetchone()
    params: tuple = ()
    if row and row["value"]:
        where = "last_seen_at = ?"
        params = (row["value"],)
    else:
        where = "last_seen_at = (SELECT MAX(last_seen_at) FROM vault_tasks)"
    if not include_done:
        where += " AND checked = 0"
    rows = conn.execute(
        f"SELECT * FROM vault_tasks WHERE {where} ORDER BY rowid", params
    ).fetchall()
    return [{k: row[k] for k in LIST_FIELDS} for row in rows]


def fetch_vault_task(conn: sqlite3.Connection, uid: str) -> dict:
    """uid で1件取得。無ければ VaultNotFoundError。"""
    row = conn.execute(
        "SELECT * FROM vault_tasks WHERE uid = ?", (uid,)
    ).fetchone()
    if row is None:
        raise VaultNotFoundError("Vault タスクが見つかりません")
    return dict(row)


# ---------------------------------------------------------------- complete

def handoff_complete_command(ticket_id: str) -> str:
    """ハンドオフチケット用の agent_handoff.py 完了コマンド文字列。"""
    home = os.environ.get("USERPROFILE") or str(Path.home())
    script = (
        Path(home) / "OneDrive" / "ドキュメント" / "KnowledgeBase"
        / "95_System" / "scripts" / "agent_handoff.py"
    )
    return f'python "{script}" complete {ticket_id}'


def _line_eol(line: str) -> str:
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\n"):
        return "\n"
    return ""


def _strip_eol(line: str) -> str:
    return line.rstrip("\r\n")


def complete_in_vault(conn: sqlite3.Connection, uid: str, note: str | None = None) -> dict:
    """完了を Vault へ書き戻す（SPEC 安全規則 1〜6）。

    1. ファイルを読み直す（同期時のスナップショットは使わない）。
    2. `- [ ] {タイトル}` の完全一致行が 1 件でなければ 409 で中断・非書込。
    3. `[ ]`→`[x]` 置換とメタブロック末尾への完了行1行挿入以外は変更しない。
    4. ハンドオフは 409（agent_handoff.py コマンドを添える）。DB 行だけでなく
       読み直したファイル側のメタでも再検証する。
    5. 改行コードは既存（LF/CRLF）を維持。UTF-8。書込はアトミック
       （一時ファイル→os.replace）で、失敗は VaultWriteError。
    6. 成功後 completed_via 記録・checked=1。
    """
    task = fetch_vault_task(conn, uid)
    if task["is_handoff"]:
        raise VaultConflictError(
            "ハンドオフチケットは KajiFlow からは完了できません。"
            "agent_handoff.py で完了してください",
            command=handoff_complete_command(uid),
        )

    # 規則1: 読み直し
    text = read_taskfile()
    lines = text.splitlines(keepends=True)

    # 規則2: 完全一致行の特定
    target = f"- [ ] {task['title']}"
    hits = [i for i, line in enumerate(lines) if _strip_eol(line) == target]
    if len(hits) != 1:
        raise VaultConflictError(
            f"Vault 側で完全一致する未完了行が {len(hits)} 件見つかったため"
            "中断しました（期待は1件）。先に同期し直してください"
        )
    idx = hits[0]

    # 規則4の再検証: DB 行だけでなく、読み直したファイル側のメタでも同一タスク
    # であることを確認する。同期は行を削除しないため、同名タスクがチケット化
    # された後も旧 title-hash 行（is_handoff=0）が DB に残り、その uid で
    # ハンドオフチケット本文を書き換え得る（SPEC「本文は絶対に手編集しない」違反）。
    current = next(
        (t for t in parse_tasks(text) if t["line"] == idx), None
    )
    if current is not None and current["is_handoff"]:
        raise VaultConflictError(
            "ハンドオフチケットは KajiFlow からは完了できません。"
            "agent_handoff.py で完了してください",
            command=handoff_complete_command(current["uid"]),
        )
    if current is None or current["uid"] != uid:
        raise VaultConflictError(
            "Vault 側でタスクの内容が変わっています。先に同期し直してください"
        )

    # 規則5: 改行コードの検出（挿入行はファイル既定、置換行は元の改行を維持）
    default_eol = "\r\n" if "\r\n" in text else "\n"

    # 規則3: [ ] → [x] 置換（当該行のみ）
    lines[idx] = "- [x] " + task["title"] + _line_eol(lines[idx])

    # メタブロック末尾（連続するインデント行の直後）を探す
    end = idx + 1
    while (
        end < len(lines)
        and lines[end].strip()
        and lines[end][0] in (" ", "\t")
    ):
        end += 1

    date_str = datetime.now(JST).date().isoformat()
    note_part = ""
    if note and note.strip():
        note_part = "。" + " ".join(note.split())  # 1行に収める
    new_line = f"  - 完了: {date_str}、KajiFlowから完了{note_part}{default_eol}"

    # 挿入位置の直前行が改行なしで終わる（EOF）なら改行を補う
    if not _line_eol(lines[end - 1]):
        lines[end - 1] += default_eol
    lines.insert(end, new_line)

    write_taskfile("".join(lines).encode("utf-8"))

    # 規則6: DB 反映
    now = datetime.now(JST).isoformat()
    conn.execute(
        "UPDATE vault_tasks SET checked = 1, completed_via = ? WHERE uid = ?",
        (now, uid),
    )
    conn.commit()
    return {"ok": True, "uid": uid, "completed_via": now}


# ---------------------------------------------------------------- prompt

# 完了条件の客観性判定（実装系タスクの自動完了記録に使う）。
# 客観語: 成果物の存在・状態で検証できることを示す語。
OBJECTIVE_MARKERS = (
    "実装", "作成", "生成", "登録", "通過", "通る", "公開", "push", "PR",
    "コミット", "マージ", "スクリプト", "テスト", "検証", "動作", "ファイル",
    "リポジトリ", "デモ", "完成", "存在", "デプロイ", "ビルド",
)
# 主観語: 本人の判断・言語化・選択が完了条件に含まれる場合は自動記録しない。
SUBJECTIVE_MARKERS = (
    "判断", "納得", "所有権", "本人の言葉", "自分の言葉", "選ぶ", "選定",
    "決める", "見直す", "振り返", "語り", "評価し",
)


def is_objective_done_when(done_when: str) -> bool:
    """完了条件が客観的に検証可能（実装系）かをヒューリスティックに判定する。

    完了条件が空なら検証基準が無いため False。主観語を1つでも含めば False
    （完了の所有権はユーザーに残す）。その上で客観語を含む場合のみ True。
    """
    if not (done_when or "").strip():
        return False
    if any(m in done_when for m in SUBJECTIVE_MARKERS):
        return False
    return any(m in done_when for m in OBJECTIVE_MARKERS)


def build_prompt(task: dict) -> str:
    """AI タスクをそのままエージェントに渡せる text/plain の指示文を作る。"""
    meta = json.loads(task.get("meta_json") or "{}")
    lines = [
        "次のタスクを実行してください。",
        "",
        f"タスク: {task['title']}",
    ]
    for label, value in (
        ("目的", task.get("purpose", "")),
        ("完了条件", task.get("done_when", "")),
        ("出所", task.get("source", "")),
        ("input", meta.get("input", "")),
    ):
        if value:
            lines.append(f"{label}: {value}")
    lines.append("")
    lines.append("完了条件を満たしたら、結果と生成物の場所を報告してください。")
    if task.get("is_handoff"):
        # ハンドオフチケットの完了遷移は agent_handoff.py が唯一の正規経路
        lines.append(
            "完了遷移はハンドオフ運用に従い、"
            f"`{handoff_complete_command(task['uid'])} --agent <自分>` で行ってください。"
        )
    elif is_objective_done_when(task.get("done_when", "")):
        # 実装系（完了条件が客観的）のみ、完了記録まで自動で行わせる。
        # 主観的な完了条件のタスクは報告止まりとし、記録はユーザーが行う。
        lines.append(
            "このタスクの完了条件は客観的に検証可能です。満たしたことを確認したら、"
            "KajiFlow API で完了を記録してください（Vault 正本に書き戻されます）:"
        )
        lines.append(
            f"  POST http://localhost:8340/api/vault/tasks/{task['uid']}/complete "
            '（JSON ボディ {"note": "結果の一言"} は任意）'
        )
    return "\n".join(lines) + "\n"
