# KajiFlow の定期実行タスクを Windows タスクスケジューラに登録する。
#   - KajiFlow_NotifyDigest   : 毎朝 7:30 に scripts\notify_digest.py
#   - KajiFlow_ObsidianWeekly : 毎週日曜 21:00 に scripts\obsidian_weekly.py
#   - KajiFlow_GTasksSync     : 30分ごとに scripts\gtasks_sync.py
# 登録のみを行う（このスクリプトはタスクを即時実行しない）。
#
# 使い方:
#   powershell -ExecutionPolicy Bypass -File scripts\register_tasks.ps1 -WhatIf   # 内容確認のみ
#   powershell -ExecutionPolicy Bypass -File scripts\register_tasks.ps1           # 実際に登録

[CmdletBinding()]
param(
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Error ".venv が見つかりません: $python"
    exit 1
}

$tasks = @(
    [pscustomobject]@{
        Name        = "KajiFlow_NotifyDigest"
        Description = "KajiFlow: 毎朝 7:30 に今日の家事ダイジェストを ntfy へ通知"
        Script      = Join-Path $root "scripts\notify_digest.py"
        Schedule    = "毎日 7:30"
        Trigger     = New-ScheduledTaskTrigger -Daily -At "07:30"
    },
    [pscustomobject]@{
        Name        = "KajiFlow_ObsidianWeekly"
        Description = "KajiFlow: 毎週日曜 21:00 に週次サマリを Obsidian へ書き出し"
        Script      = Join-Path $root "scripts\obsidian_weekly.py"
        Schedule    = "毎週日曜 21:00"
        Trigger     = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At "21:00"
    },
    [pscustomobject]@{
        Name        = "KajiFlow_GTasksSync"
        Description = "KajiFlow: 30分ごとに Google Tasks 同期（API 経由）を発火"
        Script      = Join-Path $root "scripts\gtasks_sync.py"
        Schedule    = "30分ごと"
        Trigger     = New-ScheduledTaskTrigger -Once -At (Get-Date).Date `
                        -RepetitionInterval (New-TimeSpan -Minutes 30) `
                        -RepetitionDuration ([TimeSpan]::MaxValue)
    }
)

# ---- 安全確認出力: 登録内容を先に表示する ----
Write-Host "以下のタスクをタスクスケジューラに登録します:"
foreach ($t in $tasks) {
    Write-Host ""
    Write-Host "  タスク名 : $($t.Name)"
    Write-Host "  説明     : $($t.Description)"
    Write-Host "  実行時刻 : $($t.Schedule)"
    Write-Host "  コマンド : `"$python`" `"$($t.Script)`""
    Write-Host "  作業DIR  : $root"
    $existing = Get-ScheduledTask -TaskName $t.Name -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "  既存     : あり（上書きされます）"
    } else {
        Write-Host "  既存     : なし（新規登録）"
    }
}
Write-Host ""

if ($WhatIf) {
    Write-Host "-WhatIf が指定されたため、登録は行いません（確認のみ）。"
    exit 0
}

foreach ($t in $tasks) {
    $action = New-ScheduledTaskAction -Execute $python -Argument "`"$($t.Script)`"" -WorkingDirectory $root
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $t.Name -Description $t.Description `
        -Action $action -Trigger $t.Trigger -Settings $settings -Force | Out-Null
    Write-Host "登録しました: $($t.Name)（$($t.Schedule)）"
}

Write-Host ""
Write-Host "登録が完了しました。実行は登録スケジュールに従います（このスクリプトからの即時実行は行いません）。"
