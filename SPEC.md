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

## Vault タスク連携（v2）

Obsidian Vault のタスク正本 `00_Inbox/AIエージェント連携タスク.md` を kajiflow に取り込み、「AIに指示するタスク」「ユーザーが処理するタスク」に分類して表示し、完了を Vault 側へ書き戻す。

### 正本ファイルのフォーマット（実測）

- タスクは `- [ ] タイトル` / `- [x] タイトル`（行頭）で始まり、直後にインデント2の `- キー: 値` メタ行が続く（追加日 / Project / 出所 / 目的 / 完了条件 / 期限 / input など）。
- `## 見出し` でセクション分けされている。
- ハンドオフチケットはメタに `id: T...` / `owner:` / `status:` / `handoff-to:` を持つ。**ハンドオフチケットの本文は絶対に手編集しない**（`agent_handoff.py` 経由が唯一の正規手段）。
- 完了済みタスクには `- 完了: 日付、…` または `- クローズ: 日付、…` のメタ行が付く慣行。

### データモデル追加

```sql
vault_tasks(
  uid TEXT PRIMARY KEY,        -- ハンドオフは id（T...）、それ以外は sha1(タイトル正規化)先頭16桁
  title TEXT NOT NULL,
  section TEXT DEFAULT '',
  checked INTEGER NOT NULL DEFAULT 0,      -- Vault 側の [x] 状態（Vault が正）
  project TEXT DEFAULT '', due TEXT DEFAULT '',
  purpose TEXT DEFAULT '', done_when TEXT DEFAULT '', source TEXT DEFAULT '',
  is_handoff INTEGER NOT NULL DEFAULT 0,
  handoff_owner TEXT DEFAULT '', handoff_status TEXT DEFAULT '',
  classification TEXT,         -- 'ai' | 'user' | NULL。kajiflow ローカルの状態で、再同期で消えない
  suggested TEXT,              -- ヒューリスティック提案 'ai' | 'user' | NULL
  completed_via TEXT,          -- kajiflow から完了書き戻しした日時（未実施は NULL）
  last_seen_at TEXT NOT NULL,
  meta_json TEXT NOT NULL DEFAULT '{}'
)
```

### 同期ポリシー

- Vault ファイルが正本。同期はファイル読取→パース→upsert。タイトル・メタ・checked は Vault 値で上書きし、classification / suggested（既存値優先）/ completed_via は保持する。
- 今回の同期で見つからなかった uid は削除せず `last_seen_at` を据え置き、一覧 API の既定では返さない。
- Vault パスは環境変数 `KAJIFLOW_VAULT`（既定 `%USERPROFILE%\OneDrive\ドキュメント\KnowledgeBase`）、タスクファイル相対パスは `KAJIFLOW_VAULT_TASKFILE`（既定 `00_Inbox/AIエージェント連携タスク.md`）。テストはこの2変数で一時ファイルに差し替える。
- ファイル不在・読取失敗は 503 と日本語メッセージ。

### 分類ヒューリスティック（suggested）

タイトル+目的の文字列に対する単純キーワード判定。AI 寄り: 実装/調査/分析/作成/集計/スクリーニング/レビュー/スクリプト/PR/デモ。ユーザー寄り: 本人の言葉/所有権/判断/見直す/振り返り/語り/面接/書く。両方または どちらも該当しなければ NULL。あくまで提案で、確定は classification（ユーザーまたは AI が API で設定）。

### 完了書き戻し（安全規則）

1. 書き戻し時にファイルを読み直す（同期時のスナップショットを使わない）。
2. `- [ ] {タイトル}` の完全一致行を探す。0件または2件以上なら 409 で中断し、**書き込まない**。
3. 該当行の `[ ]` を `[x]` に置換し、そのタスクのメタブロック末尾に `  - 完了: YYYY-MM-DD、KajiFlowから完了` （note があれば「。{note}」を付加）を1行挿入する。それ以外の行は1バイトも変更しない。
4. `is_handoff` のタスクは 409 で拒否し、レスポンスに `agent_handoff.py complete {id}` のコマンド文字列を含める（UI はこれをコピー可能に表示）。
5. 改行コードはファイルの既存状態（LF/CRLF）を検出して維持。エンコーディングは UTF-8。
6. 成功後は completed_via に日時を記録し、DB の checked も 1 にする。

### API 追加

- `POST /api/vault/sync` → `{open, done, new_count, updated}` 同期実行。
- `GET /api/vault/tasks` → 未完了のみ（`?include_done=1` で全件）。各要素: uid, title, section, project, due, purpose, done_when, is_handoff, handoff_status, classification, suggested, checked。
- `PUT /api/vault/tasks/{uid}/classify` body `{"classification": "ai" | "user" | null}`。
- `POST /api/vault/tasks/{uid}/complete` body `{"note": "..."}`（note 任意）→ 書き戻し。ハンドオフ・曖昧一致は 409。
- `GET /api/vault/tasks/{uid}/prompt` → text/plain の AI 指示文（タイトル・目的・完了条件・出所・input を整形。AI タスクをそのままエージェントに渡せる形式）。

### UI 追加（vault.html + vault.js）

- 下部タブに4つ目「📥 タスク」を追加（全ページのタブバーを更新）。
- 画面は3グループ: 「🤖 AIに指示する」「👤 自分でやる」「❓未分類」。未分類で suggested があるものは提案チップ（例: 「提案: AI」タップで採用）を表示。
- 各タスク: タイトル、Project/期限チップ、分類切替（AI/自分/解除）、完了ボタン（確認ダイアログ→ note 入力任意）。
- AI グループには「指示文コピー」ボタン（/prompt の内容をクリップボードへ）。
- ハンドオフチケットは owner/status バッジ付きで表示し、完了ボタンの代わりに `agent_handoff.py` コマンドのコピーを提示。
- 画面表示時に自動 sync（失敗時はトーストで日本語表示、キャッシュ済み DB 内容を表示）。手動「同期」ボタンも置く。
- 期限切れ・滞納の色付けはここでもしない（設計原則2）。期限は事実として表示するのみ。

### sw.js

ASSETS に /vault.html, /vault.js を追加し、CACHE_NAME を v4 に上げる。

## 非スコープ（今回作らない）

- 認証・マルチユーザー・家族共有
- クラウドホスティング
- ネイティブ Android アプリ（PWA で代替）
- NFC タグ
