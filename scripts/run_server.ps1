# KajiFlow サーバ起動スクリプト
# .venv の uvicorn で app.main:app を 0.0.0.0:8340 で起動する。
# 使い方: powershell -ExecutionPolicy Bypass -File scripts\run_server.ps1

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Error ".venv が見つかりません: $python"
    exit 1
}

Set-Location $root
Write-Host "KajiFlow を起動します: http://0.0.0.0:8340 （停止は Ctrl+C）"
& $python -m uvicorn app.main:app --host 0.0.0.0 --port 8340
