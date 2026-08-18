"""朝の「今日の家事」ダイジェストを ntfy に POST する。

- KajiFlow API（既定: http://localhost:8340）から /api/today を取得。
- 設定 ntfy_topic があれば `{ntfy_server}/{topic}` へ日本語ダイジェストを送る。
- API 不達時は exit 0 で静かに終了する（PC 未起動時に Task Scheduler の
  エラーが蓄積しないようにするため）。
- 標準ライブラリのみ使用（urllib）。

起動例:
    .venv\\Scripts\\python.exe scripts\\notify_digest.py
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

API_BASE = os.environ.get("KAJIFLOW_API", "http://localhost:8340")
TIMEOUT = 10


def api_json(path: str, method: str = "GET") -> dict:
    """API へリクエストし JSON を返す。失敗時は例外を送出。"""
    req = urllib.request.Request(API_BASE + path, method=method)
    if method == "POST":
        req.data = b""
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def build_message(items: list[dict]) -> str:
    """日本語ダイジェスト本文を組み立てる。"""
    if not items:
        return "今日の家事はありません。ゆっくりどうぞ。"
    names = [it["task"]["name"] for it in items]
    total_min = sum(int(it["task"].get("est_minutes") or 0) for it in items)
    return f"今日の家事 {len(items)}件・約{total_min}分: {', '.join(names)}"


def main() -> int:
    # --- API からデータ取得（不達なら exit 0 で静かに終了） ---
    try:
        settings = api_json("/api/settings")
        try:
            today = api_json("/api/today")
        except urllib.error.HTTPError:
            # プラン未生成などでエラーになった場合は regenerate を先に叩いて再試行
            api_json("/api/plan/regenerate", method="POST")
            today = api_json("/api/today")
    except (urllib.error.URLError, OSError, TimeoutError):
        print("KajiFlow API に接続できないため通知をスキップしました")
        return 0

    topic = (settings.get("ntfy_topic") or "").strip()
    if not topic:
        print("ntfy_topic が未設定のため通知をスキップしました")
        return 0
    server = (settings.get("ntfy_server") or "https://ntfy.sh").rstrip("/")

    message = build_message(today.get("items") or [])

    # --- ntfy へ POST ---
    req = urllib.request.Request(
        f"{server}/{topic}",
        data=message.encode("utf-8"),
        method="POST",
        headers={
            "Title": "KajiFlow",  # ntfy ヘッダは ASCII のみ安全なため本文に日本語を置く
            "Tags": "broom",
            "Content-Type": "text/plain; charset=utf-8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            print(f"ntfy へ通知を送信しました ({resp.status}): {message}")
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        # 通知失敗もエラー蓄積を避けて静かに終了
        print(f"ntfy への送信に失敗しました: {e}")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
