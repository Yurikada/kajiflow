"""週次サマリを Obsidian Vault の 00_Inbox/家事週次サマリ.md に書き出す。

- KajiFlow API（既定: http://localhost:8340）から /api/stats/weekly を取得。
- Vault パスは環境変数 KAJIFLOW_VAULT があれば優先、なければ
  %USERPROFILE%\\OneDrive\\ドキュメント\\KnowledgeBase を使う。
- 同じ週（例: `## 2026-W34`）のセクションが既にあれば置換、なければ末尾に追記。
- 標準ライブラリのみ使用（urllib）。

起動例:
    .venv\\Scripts\\python.exe scripts\\obsidian_weekly.py
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

API_BASE = os.environ.get("KAJIFLOW_API", "http://localhost:8340")
TIMEOUT = 10
NOTE_TITLE = "# 家事週次サマリ"


def default_vault() -> Path:
    home = os.environ.get("USERPROFILE") or str(Path.home())
    return Path(home) / "OneDrive" / "ドキュメント" / "KnowledgeBase"


def vault_path() -> Path:
    env = os.environ.get("KAJIFLOW_VAULT")
    return Path(env) if env else default_vault()


def fetch_weekly() -> dict:
    url = API_BASE + "/api/stats/weekly?weeks=1"
    with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def build_section(week: dict) -> str:
    """1週分の Markdown セクションを組み立てる（先頭は `## {label}`）。"""
    lines = [
        f"## {week['label']}（{week['start']}〜{week['end']}）",
        "",
        f"- 完了: {week['done']}件 / スキップ: {week['skip']}件",
    ]
    tasks = week.get("tasks") or []
    if tasks:
        lines += [
            "",
            "| タスク | 完了 | スキップ |",
            "| --- | ---: | ---: |",
        ]
        for t in tasks:
            lines.append(f"| {t['name']} | {t['done']} | {t['skip']} |")
    lines += [
        "",
        f"（KajiFlow 自動生成: {datetime.now().strftime('%Y-%m-%d %H:%M')}）",
        "",
    ]
    return "\n".join(lines)


def upsert_section(content: str, label: str, section: str) -> str:
    """同じ週のセクションがあれば置換、なければ末尾に追記した本文を返す。"""
    header = f"## {label}"
    lines = content.split("\n")
    start = None
    for i, line in enumerate(lines):
        if line.strip() == header or line.startswith(header + "（"):
            start = i
            break
    if start is None:
        body = content.rstrip("\n")
        if body:
            return body + "\n\n" + section
        return section
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break
    new_lines = lines[:start] + section.split("\n") + lines[end:]
    return "\n".join(new_lines)


def main() -> int:
    try:
        stats = fetch_weekly()
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        print(f"KajiFlow API に接続できませんでした: {e}")
        return 1

    weeks = stats.get("weeks") or []
    if not weeks:
        print("週次データがないため書き出しをスキップしました")
        return 0
    week = weeks[0]

    target = vault_path() / "00_Inbox" / "家事週次サマリ.md"
    if not target.parent.is_dir():
        print(f"Vault の書き出し先が見つかりません: {target.parent}")
        return 1

    if target.exists():
        content = target.read_text(encoding="utf-8")
    else:
        content = NOTE_TITLE + "\n"

    section = build_section(week)
    new_content = upsert_section(content, week["label"], section)
    if not new_content.endswith("\n"):
        new_content += "\n"
    target.write_text(new_content, encoding="utf-8", newline="\n")
    print(f"週次サマリを書き出しました: {target}（{week['label']}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
