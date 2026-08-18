# KajiFlow

判断ハードルを最小化した個人向け家事実行システム（FastAPI + SQLite + vanilla JS PWA）。

- 仕様の正本は [SPEC.md](SPEC.md)。実装・変更の前に必ず読むこと。
- 実行環境: `.venv\Scripts\python.exe`（fastapi / uvicorn / pytest / httpx 導入済み）。
- テスト: `.venv\Scripts\python.exe -m pytest tests/ -q`
- サーバ起動: `scripts/run_server.ps1`（0.0.0.0:8340）。
- DB は `data/kajiflow.db`（gitignore）。テストでは環境変数 `KAJIFLOW_DB` で差し替える。
- UI 文言はすべて日本語。滞納・遅延を責める表示は設計原則違反（SPEC の設計原則参照）。
- プロジェクト文脈の正本: `%USERPROFILE%\OneDrive\ドキュメント\KnowledgeBase\90_Projects\kajiflow\agent_context.md`
