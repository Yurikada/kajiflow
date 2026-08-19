# KajiFlow

判断ハードルを最小化した個人向け家事実行システム（FastAPI + SQLite + vanilla JS PWA）。

- 仕様の正本は [SPEC.md](SPEC.md)。実装・変更の前に必ず読むこと。
- 実行環境: `.venv\Scripts\python.exe`（fastapi / uvicorn / pytest / httpx 導入済み）。
- テスト: `.venv\Scripts\python.exe -m pytest tests/ -q`
- サーバ起動: `scripts/run_server.ps1`（既定 **127.0.0.1:8340**。API は認証を持たないため LAN へ直接公開しない）。
- 外部アクセス（スマホ等）は Tailscale Serve が 127.0.0.1:8340 へプロキシする `https://<host>.ts.net` を使う。PWA / Service Worker は secure context（HTTPS）必須のため、LAN の HTTP では動作しない。
- DB は `data/kajiflow.db`（gitignore）。テストでは環境変数 `KAJIFLOW_DB` で差し替える。
- UI 文言はすべて日本語。滞納・遅延を責める表示は設計原則違反（SPEC の設計原則参照）。
- プロジェクト文脈の正本: `%USERPROFILE%\OneDrive\ドキュメント\KnowledgeBase\90_Projects\kajiflow\agent_context.md`

## エージェント接続

codex / claude などのエージェントは **kajiflow REST API と Vault 経由**で接続する（`http://localhost:8340`）。

### API 一覧

| エンドポイント | 用途 |
| --- | --- |
| `GET /health` | 稼働確認 |
| `GET /api/next` | 次の1件（今日のプランの先頭 pending） |
| `POST /api/tasks/{id}/complete` / `POST /api/tasks/{id}/skip` | 家事の完了 / スキップ記録 |
| `GET /api/today` | 今日のプランと状態一覧 |
| `POST /api/plan/regenerate` | 今日のプランを作り直す |
| `GET/POST /api/tasks`・`GET/PUT/DELETE /api/tasks/{id}` | 家事タスク CRUD |
| `GET /api/templates`・`POST /api/templates/apply` | テンプレ一覧 / まとめて追加 |
| `GET /api/stats/weekly?weeks=N` | 週次統計 |
| `GET/PUT /api/settings` | 設定（予算分数・ntfy 等） |
| `POST /api/vault/sync` | Vault 正本 → DB 同期 |
| `GET /api/vault/tasks?include_done=0/1` | Vault タスク一覧（分類・ハンドオフ含む） |
| `PUT /api/vault/tasks/{uid}/classify` | 分類（'ai' / 'user' / null） |
| `POST /api/vault/tasks/{uid}/complete` | Vault 正本へ完了書き戻し（409 は指示 command 付き） |
| `GET /api/vault/tasks/{uid}/prompt` | AI 向け指示文（text/plain） |
| `POST /api/gtasks/sync` | Google Tasks 全体同期（未認証は 503） |
| `GET /api/gtasks/status` | 認証状態・最終同期時刻・前回結果 |

### Google 認証情報の規約

- **kajiflow が唯一の認証保持者**。エージェントは Google Tasks API を直接叩かない。
- `data/client_secret.json` / `data/gtasks_token.json` をエージェントが読む・コピーする・別の場所へ渡すことは禁止。
- 初回認証（`scripts/gtasks_auth.py`）はユーザー自身が実行する。エージェントは認証フローを起動しない。未認証時は API が 503 で案内を返すので、それをそのままユーザーに伝える。

### 同期の発火方法

- API: `POST /api/gtasks/sync`（結果 JSON: pushed / completed_in_vault / completed_chores / new_from_google / warnings）。
- CLI: `.venv\Scripts\python.exe scripts\gtasks_sync.py`（API 経由の薄いラッパ。API 不達は exit 0）。
- 定期実行: `scripts/register_tasks.ps1` が登録する `KajiFlow_GTasksSync`（30分ごと）。
- UI: 管理画面（/manage.html）の「Google Tasks 連携」カードの「今すぐ同期」。
