# KajiFlow サーバ起動スクリプト
# .venv の uvicorn で app.main:app を起動する。
# 既定バインドは 127.0.0.1（ループバックのみ）。API には認証がないため、
# LAN へ直接公開しない。外部（スマホ等）からのアクセスは Tailscale Serve が
# 127.0.0.1:8340 へプロキシする https://<host>.ts.net を使う。
# どうしても LAN 公開が必要な場合のみ環境変数 KAJIFLOW_BIND で上書きする。
# 使い方: powershell -ExecutionPolicy Bypass -File scripts\run_server.ps1

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Error ".venv が見つかりません: $python"
    exit 1
}

$bind = if ($env:KAJIFLOW_BIND) { $env:KAJIFLOW_BIND } else { "127.0.0.1" }

Set-Location $root
Write-Host "KajiFlow を起動します: http://${bind}:8340 （停止は Ctrl+C）"
& $python -m uvicorn app.main:app --host $bind --port 8340
