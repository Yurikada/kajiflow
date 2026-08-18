# KajiFlow 仕様書

判断ハードルを最小化した個人向け家事実行システム。Donetick を参考にしつつ、以下の点をユーザー（稲田）向けにカスタマイズする。

## 設計原則（優先順位順）

1. **判断レス**: ユーザーに「何をやるか」を選ばせない。メイン画面は常に「今の1件」だけを提示し、操作は「完了」「スキップ」の2択のみ。
2. **サボり耐性**: 未実施が溜まっても赤字・滞納件数などの罪悪感UIを出さない。再開時は普通に「今の1件」が出るだけ。スケジュールは完了実績ベースで自然にずれる。
3. **日本語UI**: 全UIテキストは日本語。
4. **軽量・ローカル**: FastAPI + SQLite + vanilla JS（ビルド工程なし）。PC上で常駐し、Android からは LAN アクセス（PWA）+ ntfy プッシュ通知。
5. **エージェント連携可能**: REST API と週次サマリの Obsidian 書き出しを持つ。

## 技術スタック

- Python 3.13（`.venv` 済み: fastapi, uvicorn, pytest, httpx インストール済み）
- SQLite（標準ライブラリ `sqlite3` を直接使う。ORM 不使用。DB ファイルは `data/kajiflow.db`、gitignore 対象）
- フロントは `static/` 配下の素の HTML/CSS/JS。FastAPI の StaticFiles で配信。
- タイムゾーンは Asia/Tokyo 固定（`zoneinfo` 使用）。「今日」の境界は現地時間 0:00。

## ディレクトリ構成

```
kajiflow/
  SPEC.md
  CLAUDE.md / AGENTS.md      # ローダー（SPEC.md を読めと書く）
  app/
    __init__.py
    db.py          # 接続・スキーマ作成・マイグレーション
    engine.py      # スケジューリングエンジン（純ロジック、DB非依存の関数中心）
    main.py        # FastAPI アプリ・ルーティング
    seed.py        # 家事テンプレ初期データ投入
  static/
    index.html     # 「今の1件」画面（メイン）
    today.html     # 今日のリスト
    manage.html    # タスク管理（CRUD・テンプレ追加・設定）
    app.css / app.js など
    manifest.json  # PWA
    sw.js          # service worker（app shell キャッシュのみ）
  scripts/
    notify_digest.py    # 朝の「今日の家事」を ntfy に POST
    obsidian_weekly.py  # 週次サマリを Vault の 00_Inbox に書き出し
    run_server.ps1      # uvicorn 起動（0.0.0.0:8340）
    register_tasks.ps1  # Windows タスクスケジューラ登録（通知・週次）
  tests/
    test_engine.py
    test_api.py
  data/            # gitignore（DB 実体）
```

## データモデル（SQLite）

```sql
tasks(
  id INTEGER PK,
  name TEXT NOT NULL,              -- 例: 風呂掃除
  category TEXT NOT NULL DEFAULT 'その他',  -- 例: 水回り/リビング/キッチン/洗濯/その他
  est_minutes INTEGER NOT NULL DEFAULT 10,
  schedule_type TEXT NOT NULL DEFAULT 'interval',  -- 'interval' | 'weekly'
  interval_days REAL,              -- schedule_type='interval' のとき必須
  weekdays TEXT,                   -- 'weekly' のとき '0,3' (月=0..日=6) 形式
  adaptive INTEGER NOT NULL DEFAULT 1,  -- 完了実績で間隔を学習するか
  enabled INTEGER NOT NULL DEFAULT 1,
  notes TEXT DEFAULT '',
  created_at TEXT NOT NULL         -- ISO8601 (+09:00)
)
completions(
  id INTEGER PK,
  task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  completed_at TEXT NOT NULL,
  action TEXT NOT NULL DEFAULT 'done'   -- 'done' | 'skip'
)
daily_plans(
  date TEXT PK,                    -- 'YYYY-MM-DD'
  task_ids TEXT NOT NULL,          -- JSON 配列。提示順そのもの
  generated_at TEXT NOT NULL
)
settings(key TEXT PK, value TEXT)  -- daily_budget_min(既定30), ntfy_topic, ntfy_server(既定 https://ntfy.sh)
```

## スケジューリングエンジン（app/engine.py）

すべて決定的（乱数・現在時刻の暗黙参照なし。now は引数で渡す）。

- `effective_interval(task, gaps) -> float`
  - adaptive=0 または実績 gap が2件未満: `interval_days` をそのまま返す。
  - adaptive=1: 直近の完了間隔（'done' のみ、最大直近5件）の EWMA（α=0.3、新しい gap ほど重い）を取り、`[0.5*interval_days, 2.0*interval_days]` にクランプ。
- `urgency(task, last_done_at, now, eff_interval) -> float`
  - `elapsed_days / eff_interval`。last_done_at が無ければ created_at 起点。
  - weekly タスクは urgency 概念を使わず「今日が該当曜日で、今日未完了なら対象」とする。
- `build_plan(tasks, history, date, budget_min) -> list[task_id]`
  - 対象: weekly 該当分 + interval で urgency >= 1.0 のもの。
  - 並び順: weekly を先頭（登録順）、次に interval を urgency 降順。
  - 予算充填: 先頭から est_minutes を積み、budget_min を超えたら以降は入れない。ただし1件目は必ず入れる。
  - 対象ゼロなら空リスト（「今日はなし」を正当な結果とする。無理に1件出さない）。
- プランは日付単位で `daily_plans` に凍結保存する。同日中の再計算は明示操作（regenerate）のみ。日中にタスクを追加しても翌日から反映（判断レス性を守る）。
- スキップの扱い: その日のプランから外すだけ。ペナルティ・前倒しなし。completions に action='skip' で記録（統計用）。skip は adaptive 学習の gap に使わない。

## API（app/main.py）

すべて JSON。認証なし（LAN 内利用前提）。

- `GET /api/next` → `{task: {...} | null, done_count, total_count}` 今日のプランの未処理先頭1件。
- `POST /api/tasks/{id}/complete` → 完了記録。レスポンスは /api/next と同形（次の1件を返す）。
- `POST /api/tasks/{id}/skip` → 同上。
- `GET /api/today` → 今日のプラン全件と各状態（done/skip/pending）。
- `POST /api/plan/regenerate` → 今日のプランを作り直す。
- `GET/POST /api/tasks`, `GET/PUT/DELETE /api/tasks/{id}` → CRUD。
- `GET /api/templates` → seed テンプレ一覧、`POST /api/templates/apply` → 選択テンプレを tasks に追加。
- `GET /api/stats/weekly?weeks=1` → 直近 n 週の完了/スキップ集計（obsidian_weekly が使う）。
- `GET /api/settings`, `PUT /api/settings`。
- `GET /health` → `{"status":"ok"}`。
- `/` は static/index.html。起動時に db 初期化と当日プラン生成（未生成なら）を行う。

## UI 要件

- **index.html（メイン）**: 中央に大カード1枚（タスク名・カテゴリ・目安分数）。下に「完了」（大・primary）「スキップ」（小・目立たない）。完了で次カードへ。全部終わると「今日の家事は終わりです」。プランが空なら「今日はやることなし」。進捗は「2/4」程度の小さい表示のみ。**滞納・遅延の表示は一切しない**。
- **today.html**: 今日のリストと状態。ここも遅延表示なし。
- **manage.html**: タスク一覧（有効/無効トグル・編集・削除）、新規追加フォーム、テンプレからまとめて追加、設定（1日の予算分数、ntfy topic）。
- 3画面は下部タブで行き来。モバイルファースト（幅 375px 基準）、PC でもそのまま見られる。
- 配色はライト/ダーク両対応（prefers-color-scheme）。日本語フォントはシステムフォント。
- PWA: manifest.json（name: KajiFlow、日本語）、sw.js は静的アセットの cache-first のみ（API はキャッシュしない）。

## 家事テンプレ（app/seed.py に定数として持つ）

一人暮らし〜二人暮らし想定の日本の家事。例（この通りでなくてよいが 15〜20 件程度）:
キッチンリセット(毎日,10分)/洗濯(2日,20分)/掃除機(3日,15分)/風呂掃除(2日,10分)/トイレ掃除(4日,10分)/洗面台(4日,5分)/排水口(7日,10分)/床拭き(7日,20分)/シーツ交換(14日,15分)/冷蔵庫整理(30日,15分)/レンジ・コンロ(14日,15分)/玄関掃き(7日,5分)/ベランダ(30日,15分)/ゴミ出し準備(weekly,5分)/資源ごみ(weekly,5分)/観葉植物の水やり(3日,5分) など。
seed はテンプレ定義のみで、初期 DB へ自動投入はしない（manage 画面から選んで追加）。

## scripts/

- `notify_digest.py`: API（localhost:8340）から /api/today を取得し、ntfy_topic 設定があれば `{ntfy_server}/{topic}` へ日本語ダイジェスト（「今日の家事 3件・約35分: 風呂掃除, 掃除機, …」）を POST。プラン未生成なら regenerate を先に叩く。API 不達時は exit 0 で静かに終了（PC 未起動時の Task Scheduler エラー蓄積を避ける）。
- `obsidian_weekly.py`: /api/stats/weekly を取得し、`%USERPROFILE%\OneDrive\ドキュメント\KnowledgeBase\00_Inbox\家事週次サマリ.md` に週次セクションを追記（既存同週セクションがあれば置換）。
- `run_server.ps1`: `.venv` の uvicorn で `app.main:app` を `0.0.0.0:8340` で起動。
- `register_tasks.ps1`: Task Scheduler に「毎朝 7:30 notify_digest」「毎週日曜 21:00 obsidian_weekly」を登録（`Register-ScheduledTask`）。実行は登録のみで、ユーザーが明示実行する前提。

## テスト（pytest）

- engine: urgency 計算、EWMA とクランプ、weekly 判定、予算充填（1件目は必ず入る）、プラン決定性（同入力→同出力）、skip が学習に影響しないこと。
- API: TestClient で next→complete→next の遷移、skip、CRUD、テンプレ適用、regenerate、stats。テストは一時ディレクトリの DB を使う（環境変数 `KAJIFLOW_DB` で DB パスを差し替え可能にする）。

## 非スコープ（今回作らない）

- 認証・マルチユーザー・家族共有
- クラウドホスティング
- ネイティブ Android アプリ（PWA で代替）
- NFC タグ
