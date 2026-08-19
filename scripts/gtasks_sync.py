"""Google Tasks 同期を KajiFlow API 経由で発火する薄いラッパ。

- POST {KAJIFLOW_API}/api/gtasks/sync を叩いて結果を1行表示する。
- API 不達時は exit 0 で静かに終了する（PC 未起動時に Task Scheduler の
  エラーが蓄積しないようにするため。notify_digest.py と同じ流儀）。
- 未認証（503）等の HTTP エラーも日本語メッセージを表示して exit 0。
- 標準ライブラリのみ使用（urllib）。Google API は直接叩かない。

起動例:
    .venv\\Scripts\\python.exe scripts\\gtasks_sync.py
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

API_BASE = os.environ.get("KAJIFLOW_API", "http://localhost:8340")
TIMEOUT = 120  # 同期は Google API を複数回呼ぶため長め


def main() -> int:
    req = urllib.request.Request(
        API_BASE + "/api/gtasks/sync", data=b"", method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # 未認証 503 など: detail の日本語メッセージを表示して静かに終了
        detail = ""
        try:
            data = json.loads(e.read().decode("utf-8"))
            if isinstance(data.get("detail"), str):
                detail = data["detail"]
        except Exception:
            pass
        print(f"同期に失敗しました (HTTP {e.code}): {detail}")
        return 0
    except (urllib.error.URLError, OSError, TimeoutError):
        print("KajiFlow API に接続できないため同期をスキップしました")
        return 0

    warnings = result.get("warnings") or []
    print(
        "同期しました: "
        f"push {result.get('pushed', 0)}件・"
        f"Vault完了 {result.get('completed_in_vault', 0)}件・"
        f"家事完了 {result.get('completed_chores', 0)}件・"
        f"新規取込 {result.get('new_from_google', 0)}件・"
        f"警告 {len(warnings)}件"
    )
    for w in warnings:
        print(f"  - {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
