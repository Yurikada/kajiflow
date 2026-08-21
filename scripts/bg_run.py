r"""コンソールウィンドウを出さずに Python スクリプト／モジュールを実行するランチャ。

なぜ必要か:
    タスクスケジューラの「ユーザーがログオンしているときのみ実行」タスクは
    対話セッションで起動されるため、アクションに python.exe や cmd.exe を
    直接指定するとコンソールウィンドウが出る。常駐タスク（サーバ）では
    ウィンドウが出っぱなしになり、定期タスク（30分ごとの同期）では実行の
    たびにウィンドウが一瞬前面へ来てフォーカスを奪う。
    pythonw.exe からこのランチャを起動すればウィンドウは一切出ない。

    pythonw.exe では sys.stdout / sys.stderr が None になるため、ここで
    ログファイルへ差し替えてから対象を実行する（uvicorn のログも残る）。

使い方:
    pythonw.exe scripts\bg_run.py --log data\server.log --module uvicorn app.main:app --host 127.0.0.1 --port 8340
    pythonw.exe scripts\bg_run.py --log data\tasks.log  --script scripts\gtasks_sync.py

    --log      ログの追記先（相対パスはリポジトリルート基準）
    --module   以降を `python -m <module> <args...>` として実行
    --script   以降を `python <script> <args...>` として実行
    --module / --script より後ろの引数はすべて対象へそのまま渡す。
"""

from __future__ import annotations

import datetime as dt
import os
import runpy
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAX_LOG_BYTES = 5 * 1024 * 1024  # これを超えたら .1 へ退避して新しく書き始める


def parse_args(argv: list[str]) -> tuple[Path, str, str, list[str]]:
    """(ログパス, "module"|"script", 対象, 残りの引数) を返す。"""
    log: str | None = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--log":
            if i + 1 >= len(argv):
                raise SystemExit("--log に値がありません")
            log = argv[i + 1]
            i += 2
        elif arg in ("--module", "--script"):
            if i + 1 >= len(argv):
                raise SystemExit(f"{arg} に値がありません")
            if log is None:
                raise SystemExit("--log は --module/--script より前に指定してください")
            return _resolve(log), arg[2:], argv[i + 1], list(argv[i + 2 :])
        else:
            raise SystemExit(f"不明な引数: {arg}")
    raise SystemExit("--module または --script を指定してください")


def _resolve(path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else ROOT / p


def rotate(log_path: Path) -> None:
    """ログが大きくなりすぎたら .1 へ退避する（保持は1世代）。"""
    try:
        if log_path.exists() and log_path.stat().st_size > MAX_LOG_BYTES:
            backup = log_path.with_suffix(log_path.suffix + ".1")
            backup.unlink(missing_ok=True)
            log_path.replace(backup)
    except OSError:
        pass  # 退避に失敗しても本処理は続ける


def main(argv: list[str]) -> int:
    log_path, mode, target, rest = parse_args(argv)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    rotate(log_path)

    # pythonw.exe では sys.stdout / sys.stderr が None なので必ず差し替える。
    stream = open(log_path, "a", encoding="utf-8", errors="replace", buffering=1)
    sys.stdout = stream
    sys.stderr = stream

    # 対象は「リポジトリルートで python を叩いた」のと同じ状態で動かす。
    os.chdir(ROOT)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[bg_run {stamp}] {mode}={target} args={rest}")

    try:
        if mode == "module":
            sys.argv = [target, *rest]
            runpy.run_module(target, run_name="__main__", alter_sys=True)
        else:
            script = _resolve(target)
            sys.argv = [str(script), *rest]
            runpy.run_path(str(script), run_name="__main__")
    except SystemExit as exc:  # 対象が sys.exit() した場合はその終了コードを返す
        code = exc.code
        return code if isinstance(code, int) else (0 if code is None else 1)
    except BaseException:
        traceback.print_exc()
        return 1
    finally:
        stream.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
