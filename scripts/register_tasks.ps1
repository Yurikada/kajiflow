# KajiFlow の定期実行タスクを Windows タスクスケジューラに登録する。
#   - KajiFlow_NotifyDigest   : 毎朝 7:30 に scripts\notify_digest.py
#   - KajiFlow_ObsidianWeekly : 毎週日曜 21:00 に scripts\obsidian_weekly.py
#   - KajiFlow_GTasksSync     : 30分ごとに scripts\gtasks_sync.py
#   - KajiFlow_Server         : ログオン時に uvicorn（ログは data\server.log）
# いずれも pythonw.exe + scripts\bg_run.py 経由で起動する。python.exe や cmd.exe を
# アクションに直接指定すると対話セッションでコンソールウィンドウが開き、常駐タスクでは
# 出っぱなしに、定期タスクでは実行のたびに最前面へ来てフォーカスを奪うため。
# 標準出力・標準エラーは bg_run.py がログファイルへ追記する（5MB で1世代退避）。
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
# コンソールウィンドウを出さないための実行体。タスクのアクションは常にこちらを使う。
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
$bgRun = Join-Path $root "scripts\bg_run.py"

if (-not (Test-Path $python)) {
    Write-Error ".venv が見つかりません: $python"
    exit 1
}

if (-not (Test-Path $pythonw)) {
    Write-Error "pythonw.exe が見つかりません: $pythonw"
    exit 1
}

if (-not (Test-Path $bgRun)) {
    Write-Error "bg_run.py が見つかりません: $bgRun"
    exit 1
}

# --log の相対パスは bg_run.py がリポジトリルート基準で解決する。
function Get-ScriptArgs([string]$scriptPath) {
    return "`"$bgRun`" --log data\tasks.log --script `"$scriptPath`""
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
                        -RepetitionDuration (New-TimeSpan -Days 3650)  # MaxValue はタスクXMLの Duration として不正
    },
    [pscustomobject]@{
        Name        = "KajiFlow_Server"
        Description = "KajiFlow: ログオン時にサーバを起動（127.0.0.1:8340、非表示）"
        Script      = Join-Path $root "scripts\run_server.ps1"
        Schedule    = "ログオン時"
        # 全ユーザー対象の AtLogOn は管理者権限が要る（0x80070005）ため自ユーザー限定にする
        Trigger     = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
        # cmd.exe でリダイレクトするとコンソールウィンドウが常駐してしまうため、
        # pythonw.exe + bg_run.py で起動し、ログはランチャ側でファイルへ書く。
        # 二重起動してもポート使用中で即終了するだけで無害。
        Exec        = $pythonw
        Args        = "`"$bgRun`" --log data\server.log --module uvicorn app.main:app --host 127.0.0.1 --port 8340"
        # 常駐タスクなので実行時間の上限を外す。既定の PT72H のままだと
        # PC をつけっぱなしにして 3 日でタスクスケジューラがサーバを止めてしまう。
        NoTimeLimit = $true
    }
)

# ---- 安全確認出力: 登録内容を先に表示する ----
Write-Host "以下のタスクをタスクスケジューラに登録します:"
foreach ($t in $tasks) {
    Write-Host ""
    Write-Host "  タスク名 : $($t.Name)"
    Write-Host "  説明     : $($t.Description)"
    Write-Host "  実行時刻 : $($t.Schedule)"
    $cmd = if ($t.PSObject.Properties["Exec"] -and $t.Exec) { "`"$($t.Exec)`" $($t.Args)" } else { "`"$pythonw`" $(Get-ScriptArgs $t.Script)" }
    Write-Host "  コマンド : $cmd"
    Write-Host "  作業DIR  : $root"
    if ($t.PSObject.Properties["NoTimeLimit"] -and $t.NoTimeLimit) {
        Write-Host "  実行時間 : 無制限（常駐）"
    }
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
    if ($t.PSObject.Properties["Exec"] -and $t.Exec) {
        $action = New-ScheduledTaskAction -Execute $t.Exec -Argument $t.Args -WorkingDirectory $root
    } else {
        $action = New-ScheduledTaskAction -Execute $pythonw -Argument (Get-ScriptArgs $t.Script) -WorkingDirectory $root
    }
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    if ($t.PSObject.Properties["NoTimeLimit"] -and $t.NoTimeLimit) {
        # TimeSpan::Zero（PT0S）はタスクスケジューラ上「無制限」の意味になる。
        $settings.ExecutionTimeLimit = [System.Xml.XmlConvert]::ToString([TimeSpan]::Zero)
    }
    Register-ScheduledTask -TaskName $t.Name -Description $t.Description `
        -Action $action -Trigger $t.Trigger -Settings $settings -Force | Out-Null
    Write-Host "登録しました: $($t.Name)（$($t.Schedule)）"
}

Write-Host ""
Write-Host "登録が完了しました。実行は登録スケジュールに従います（このスクリプトからの即時実行は行いません）。"
