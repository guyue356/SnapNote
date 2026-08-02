param(
    [switch]$Quiet
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$stateFile = Join-Path $projectRoot ".snapnote-pids.json"

function Get-ProcessTreeIds {
    param(
        [Parameter(Mandatory = $true)][int[]]$RootIds,
        [Parameter(Mandatory = $true)][object[]]$ProcessSnapshot
    )

    $allIds = [System.Collections.Generic.HashSet[int]]::new()
    $queue = [System.Collections.Generic.Queue[int]]::new()
    foreach ($rootId in $RootIds) {
        if ($allIds.Add($rootId)) {
            $queue.Enqueue($rootId)
        }
    }

    while ($queue.Count -gt 0) {
        $parentId = $queue.Dequeue()
        foreach ($child in $ProcessSnapshot | Where-Object { [int]$_.ParentProcessId -eq $parentId }) {
            $childId = [int]$child.ProcessId
            if ($allIds.Add($childId)) {
                $queue.Enqueue($childId)
            }
        }
    }
    return @($allIds)
}

if (-not (Test-Path -LiteralPath $stateFile)) {
    if (-not $Quiet) {
        Write-Host "SnapNote 当前没有由一键脚本启动的服务。" -ForegroundColor Yellow
    }
    exit 0
}

try {
    $state = Get-Content -LiteralPath $stateFile -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($state.projectRoot -ne $projectRoot) {
        throw "进程记录不属于当前项目，已停止操作。"
    }

    $validRootIds = @()
    foreach ($record in @($state.processes)) {
        $process = Get-Process -Id ([int]$record.pid) -ErrorAction SilentlyContinue
        if (-not $process) {
            continue
        }

        $recordedStart = [DateTime]::Parse($record.startedAtUtc).ToUniversalTime()
        $actualStart = $process.StartTime.ToUniversalTime()
        if ([Math]::Abs(($actualStart - $recordedStart).TotalSeconds) -le 2) {
            $validRootIds += $process.Id
        }
    }

    if ($validRootIds.Count -gt 0) {
        $snapshot = @(Get-CimInstance Win32_Process | Select-Object ProcessId, ParentProcessId)
        $treeIds = @(Get-ProcessTreeIds -RootIds $validRootIds -ProcessSnapshot $snapshot)
        foreach ($processId in ($treeIds | Sort-Object -Descending)) {
            Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
        }
    }

    Start-Sleep -Milliseconds 500
    Remove-Item -LiteralPath $stateFile -Force

    if (-not $Quiet) {
        Write-Host "SnapNote 已关闭。" -ForegroundColor Green
        Write-Host "日志仍保留在 .snapnote-logs 目录。" -ForegroundColor DarkGray
    }
} catch {
    Write-Host "关闭失败：$($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
