"""Google Tasks の初回 OAuth 認証を行う（ユーザー自身が実行する）。

- data/client_secret.json（Google Cloud Console から取得）を使って
  InstalledAppFlow を起動し、ブラウザで同意 → data/gtasks_token.json に保存。
- 保存後にタスクリスト一覧を表示して疎通確認する。
- kajiflow サーバ・エージェントはこのフローを起動しない（SPEC「認証」）。
  未認証時の API は 503 で本スクリプトの実行を案内する。

起動例:
    .venv\\Scripts\\python.exe scripts\\gtasks_auth.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# app パッケージ（定数・クライアント）を scripts/ 直下実行でも読めるようにする
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import gtasks  # noqa: E402

CLIENT_SECRET_GUIDE = f"""\
data/client_secret.json が見つかりません: {gtasks.CLIENT_SECRET_PATH}

Google Cloud Console で OAuth クライアントを作成して配置してください:

  1. https://console.cloud.google.com/ でプロジェクトを作成（既存でも可）
  2. 「APIとサービス」→「ライブラリ」で Google Tasks API と
     Google Calendar API を検索して有効化
  3. 「APIとサービス」→「認証情報」→「認証情報を作成」→「OAuth クライアント ID」
     - 同意画面が未設定なら先に設定する（外部・テストユーザーに自分の
       Google アカウントを追加）
     - アプリケーションの種類は「デスクトップアプリ」を選ぶ
  4. 作成したクライアントの JSON をダウンロードし、
     data/client_secret.json という名前で配置する

配置後にもう一度このスクリプトを実行してください。\
"""


def main() -> int:
    if not gtasks.CLIENT_SECRET_PATH.exists():
        print(CLIENT_SECRET_GUIDE)
        return 1

    from google_auth_oauthlib.flow import InstalledAppFlow

    if gtasks.TOKEN_PATH.exists():
        print(
            "既存のトークンが見つかりました。スコープ不足"
            "（ゴミカレンダー連動の calendar.readonly 未同意など）の場合も、"
            "このまま再同意すると新しいスコープで上書き保存されます。"
        )
    print("ブラウザで Google アカウントの同意画面を開きます…")
    flow = InstalledAppFlow.from_client_secrets_file(
        str(gtasks.CLIENT_SECRET_PATH), gtasks.SCOPES
    )
    creds = flow.run_local_server(port=0)

    gtasks.TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    gtasks.TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    print(f"トークンを保存しました: {gtasks.TOKEN_PATH}")

    # --- 疎通確認: タスクリスト一覧を表示 ---
    try:
        client = gtasks.build_client()
        tasklists = client.list_tasklists()
    except gtasks.GTasksError as e:
        print(f"疎通確認に失敗しました: {e}")
        return 1
    print(f"疎通確認 OK。タスクリスト {len(tasklists)}件:")
    for tl in tasklists:
        print(f"  - {tl.get('title')}")
    print(
        "認証が完了しました。同期は POST /api/gtasks/sync"
        "（scripts/gtasks_sync.py）で実行できます。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
