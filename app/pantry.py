"""購買記録・買い物リスト（SPEC「購買記録・買い物リスト（v5）」）。

- 購入間隔の推定・提案は純関数（now は引数で受け、DB を参照しない）。
- items / receipts / purchases の DB 操作はここに集約し、main.py は API 層に徹する。
- 在庫数は持たない。購入日時の実績だけから「そろそろ切れる」を推定する
  （家事の adaptive 学習と同じ思想。更新をサボっても数字が嘘にならない）。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from . import db as dbmod
from .engine import JST, parse_dt

EWMA_ALPHA = 0.3      # 新しい gap ほど重い（engine と同じ）
MAX_GAPS = 5          # 学習に使う直近 gap 数
MIN_GAP_DAYS = 0.5    # これ未満は同一買い物・二重記録由来とみなし学習に使わない
THIN_GAPS = 2         # gap がこれ未満なら推定は「目安薄い」

VALID_CATEGORIES = ("食材", "日用品", "消耗品", "その他")

# 受け入れる画像タイプ。拡張子は保存ファイル名に使う
IMAGE_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
}
MAX_IMAGE_BYTES = 10 * 1024 * 1024


# ---------------------------------------------------------------- 純ロジック

def purchase_gaps(purchased_ats: list[datetime]) -> list[float]:
    """購入日時列（順不同可）から学習に使う gap（日数）列を返す。

    MIN_GAP_DAYS 未満の gap は同一買い物の複数レシート・二重記録由来と
    みなして除外する（engine の MIN_GAP_DAYS と同じ理由）。
    """
    dts = sorted(purchased_ats)
    gaps = [(b - a).total_seconds() / 86400.0 for a, b in zip(dts, dts[1:])]
    return [g for g in gaps if g >= MIN_GAP_DAYS]


def estimate_interval(gaps: list[float]) -> float | None:
    """gap 列から購入間隔（日数）を EWMA で推定する。gap ゼロ件なら None。

    家事と違い基準の interval_days を持たないため、クランプはしない。
    """
    if not gaps:
        return None
    recent = gaps[-MAX_GAPS:]
    ewma = recent[0]
    for gap in recent[1:]:
        ewma = EWMA_ALPHA * gap + (1.0 - EWMA_ALPHA) * ewma
    return ewma


def shopping_suggestions(
    items: list[dict],
    purchases_by_item: dict[int, list[datetime]],
    now: datetime,
) -> list[dict]:
    """「そろそろ切れる」提案を ratio 降順で返す。

    - ratio = 最終購入からの経過日数 / 推定間隔。1.0 以上のみ載せる。
    - gap が THIN_GAPS 件未満は thin=True を付ける（提案から隠しはしない。
      隠すと品目マスタを育てる動機が消える）。
    - 提案は事実の提示のみ。滞納・警告の意味づけは UI でもしない。
    """
    result = []
    for item in items:
        if not item.get("enabled", 1):
            continue
        dts = purchases_by_item.get(item["id"], [])
        if not dts:
            continue
        gaps = purchase_gaps(dts)
        interval = estimate_interval(gaps)
        if interval is None or interval <= 0:
            continue
        last = max(dts)
        elapsed = (now - last).total_seconds() / 86400.0
        ratio = elapsed / interval
        if ratio < 1.0:
            continue
        result.append({
            "item": {k: item[k] for k in ("id", "name", "category")},
            "last_purchased_at": last.isoformat(),
            "interval_days": round(interval, 1),
            "ratio": round(ratio, 2),
            "thin": len(gaps) < THIN_GAPS,
        })
    result.sort(key=lambda s: (-s["ratio"], s["item"]["id"]))
    return result


# ---------------------------------------------------------------- items

def load_aliases(row: sqlite3.Row | dict) -> list[str]:
    try:
        parsed = json.loads(row["aliases"] or "[]")
    except (ValueError, TypeError):
        return []
    return [str(a) for a in parsed] if isinstance(parsed, list) else []


def item_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["aliases"] = load_aliases(row)
    d["enabled"] = int(d.get("enabled") or 0)
    return d


def find_item(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    """正規化名または alias の完全一致で品目を探す。"""
    name = name.strip()
    row = conn.execute("SELECT * FROM items WHERE name = ?", (name,)).fetchone()
    if row is not None:
        return row
    for row in conn.execute("SELECT * FROM items").fetchall():
        if name in load_aliases(row):
            return row
    return None


def upsert_item(
    conn: sqlite3.Connection,
    name: str,
    category: str,
    raw_label: str,
    now_iso: str,
) -> int:
    """品目を名前/alias 一致で再利用し、無ければ作る。raw_label は alias に育てる。

    既存品目の category は上書きしない（本人が管理画面で直した分類を
    解析のたびに戻さないため）。
    """
    name = name.strip()
    row = find_item(conn, name)
    if row is None:
        category = category if category in VALID_CATEGORIES else "その他"
        cur = conn.execute(
            "INSERT INTO items (name, category, aliases, created_at) VALUES (?, ?, '[]', ?)",
            (name, category, now_iso),
        )
        item_id = cur.lastrowid
        aliases: list[str] = []
    else:
        item_id = row["id"]
        aliases = load_aliases(row)
    raw_label = (raw_label or "").strip()
    if raw_label and raw_label != name and raw_label not in aliases:
        aliases.append(raw_label)
        conn.execute(
            "UPDATE items SET aliases = ? WHERE id = ?",
            (json.dumps(aliases, ensure_ascii=False), item_id),
        )
    return item_id


def list_items(conn: sqlite3.Connection) -> list[dict]:
    """品目一覧（購入回数・最終購入日付き）。"""
    rows = conn.execute(
        """
        SELECT i.*, COUNT(p.id) AS purchase_count, MAX(p.purchased_at) AS last_purchased_at
        FROM items i LEFT JOIN purchases p ON p.item_id = i.id
        GROUP BY i.id ORDER BY last_purchased_at DESC, i.id
        """
    ).fetchall()
    return [item_to_dict(r) for r in rows]


# ---------------------------------------------------------------- receipts

def receipts_dir() -> Path:
    """画像の保存先。DB と同じディレクトリ配下（テストは KAJIFLOW_DB で丸ごと差し替わる）。"""
    return dbmod.get_db_path().parent / "receipts"


def receipt_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


def save_receipt_image(
    conn: sqlite3.Connection,
    data: bytes,
    content_type: str,
    now_iso: str,
) -> tuple[dict, bool]:
    """画像を保存して pending 行を作る。同一 sha256 は既存行を返す（冪等）。

    戻り値: (receipt dict, created)。
    """
    sha = hashlib.sha256(data).hexdigest()
    existing = conn.execute("SELECT * FROM receipts WHERE sha256 = ?", (sha,)).fetchone()
    if existing is not None:
        return receipt_to_dict(existing), False
    ext = IMAGE_TYPES[content_type]
    directory = receipts_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{sha[:16]}{ext}"
    path.write_bytes(data)
    cur = conn.execute(
        "INSERT INTO receipts (sha256, image_path, uploaded_at) VALUES (?, ?, ?)",
        (sha, str(path), now_iso),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM receipts WHERE id = ?", (cur.lastrowid,)).fetchone()
    return receipt_to_dict(row), True


def apply_parse(
    conn: sqlite3.Connection,
    receipt: sqlite3.Row,
    payload: dict,
    now_iso: str,
) -> dict:
    """解析結果を書き戻す（SPEC「解析書き戻し」）。

    - 再解析は冪等: 同 receipt_id の既存 purchases を削除してから挿入する。
    - 明細合計と total_jpy の差が 1 円超なら warnings に載せる（拒否しない。
      軽減税率・ポイント値引きで恒常的にずれるため）。
    """
    warnings: list[str] = []
    lines = payload.get("lines") or []
    purchased_at = str(payload.get("purchased_at") or now_iso)
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM purchases WHERE receipt_id = ?", (receipt["id"],))
        line_sum = 0.0
        for line in lines:
            item_id = upsert_item(
                conn,
                name=str(line["item_name"]),
                category=str(line.get("category") or "その他"),
                raw_label=str(line.get("raw_label") or ""),
                now_iso=now_iso,
            )
            amount = float(line["amount_jpy"])
            line_sum += amount
            conn.execute(
                """
                INSERT INTO purchases
                  (receipt_id, item_id, raw_label, qty, amount_jpy, purchased_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt["id"],
                    item_id,
                    str(line.get("raw_label") or ""),
                    float(line.get("qty") or 1),
                    amount,
                    purchased_at,
                    now_iso,
                ),
            )
        total = payload.get("total_jpy")
        if total is not None and abs(float(total) - line_sum) > 1.0:
            warnings.append(
                f"明細合計 {line_sum:.0f}円 とレシート合計 {float(total):.0f}円 が一致しません"
                "（軽減税率・値引きの可能性。事実として記録しました）"
            )
        conn.execute(
            """
            UPDATE receipts SET store = ?, purchased_at = ?, total_jpy = ?,
              status = 'parsed', parsed_at = ?, parsed_by = ?, note = ''
            WHERE id = ?
            """,
            (
                str(payload.get("store") or ""),
                purchased_at,
                float(total) if total is not None else None,
                now_iso,
                str(payload.get("parsed_by") or "agent"),
                receipt["id"],
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    row = conn.execute("SELECT * FROM receipts WHERE id = ?", (receipt["id"],)).fetchone()
    return {"receipt": receipt_to_dict(row), "lines": len(lines), "warnings": warnings}


def delete_receipt(conn: sqlite3.Connection, receipt: sqlite3.Row) -> None:
    """レシート行・明細（CASCADE）・画像ファイルを削除する。"""
    image_path = Path(receipt["image_path"])
    conn.execute("DELETE FROM receipts WHERE id = ?", (receipt["id"],))
    conn.commit()
    # 画像は receipts_dir 配下のときだけ消す（DB 移設等でパスが外を指していたら触らない）
    try:
        if image_path.is_relative_to(receipts_dir()):
            image_path.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


# ---------------------------------------------------------------- purchases

def fetch_purchases_by_item(conn: sqlite3.Connection) -> dict[int, list[datetime]]:
    by_item: dict[int, list[datetime]] = {}
    for row in conn.execute("SELECT item_id, purchased_at FROM purchases").fetchall():
        by_item.setdefault(row["item_id"], []).append(parse_dt(row["purchased_at"]))
    return by_item


def list_purchases(conn: sqlite3.Connection, since_iso: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT p.id, p.receipt_id, p.item_id, i.name AS item_name, i.category,
               p.raw_label, p.qty, p.amount_jpy, p.purchased_at,
               r.store
        FROM purchases p
        JOIN items i ON i.id = p.item_id
        LEFT JOIN receipts r ON r.id = p.receipt_id
        WHERE p.purchased_at >= ?
        ORDER BY p.purchased_at DESC, p.id DESC
        """,
        (since_iso,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- エージェント指示文

# base_url は build_pending_prompt が __BASE_URL__ を実リクエストの値で置換する
# （JSON 例が波括弧だらけのため str.format は使わない）
PARSE_API_DOC = """\
書き戻し先: POST __BASE_URL__api/receipts/{id}/parse
body 例:
{
  "store": "スーパー〇〇",
  "purchased_at": "2026-08-24T18:30:00+09:00",
  "total_jpy": 2480,
  "parsed_by": "claude",
  "lines": [
    {"raw_label": "ｷｭｷｭｯﾄ ﾎﾟﾝﾌﾟ", "item_name": "食器用洗剤", "category": "日用品",
     "qty": 1, "amount_jpy": 328}
  ]
}
規則:
- item_name は日本語の正規化名。下の既存品目カタログに同じものがあれば必ず同じ名前を使う。
- category は 食材 / 日用品 / 消耗品 / その他 のいずれか。
- amount_jpy は行合計（値引き後）。qty が読めなければ 1。
- 値引き行・ポイント行は品目にしない（合計のずれは warnings で返るのでそのままでよい）。
- 読めない画像は POST __BASE_URL__api/receipts/{id}/fail に {"note": "理由"} を送る。
"""


def build_pending_prompt(conn: sqlite3.Connection, base_url: str = "http://localhost:8340/") -> str:
    """未処理レシートの解析指示文（vault の /prompt と同じ流儀）。"""
    pending = conn.execute(
        "SELECT * FROM receipts WHERE status = 'pending' ORDER BY id"
    ).fetchall()
    lines = ["# レシート解析キュー", ""]
    if not pending:
        lines.append("未処理のレシートはありません。")
        return "\n".join(lines) + "\n"
    lines.append(f"未処理 {len(pending)}件。各画像を読み、品目・金額を構造化して書き戻してください。")
    lines.append("")
    for r in pending:
        lines.append(f"- id={r['id']} 画像: {r['image_path']} （アップロード {r['uploaded_at'][:16]}）")
    lines.append("")
    lines.append(PARSE_API_DOC.replace("__BASE_URL__", base_url))
    names = [row["name"] for row in conn.execute("SELECT name FROM items ORDER BY name").fetchall()]
    lines.append("既存品目カタログ（この名前に揃える）:")
    lines.append("、".join(names) if names else "（まだありません）")
    return "\n".join(lines) + "\n"
