param(
    [switch]$SkipInstall,
    [switch]$NoBrowser,
    [ValidateRange(1024, 49151)]
    [int]$WebPort = 43871,
    [ValidateRange(1024, 49151)]
    [int]$ApiPort = 43872
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$backendPath = Join-Path $projectRoot "backend"
$frontendPath = Join-Path $projectRoot "frontend"
$environmentName = "snapnote"
$stateFile = Join-Path $projectRoot ".snapnote-pids.json"
$setupFile = Join-Path $projectRoot ".snapnote-setup.json"
$logDirectory = Join-Path $projectRoot ".snapnote-logs"
$stopScript = Join-Path $projectRoot "stop.ps1"

function Test-HttpEndpoint {
    param([Parameter(Mandatory = $true)][string]$Uri)

    try {
        $response = Invoke-WebRequest -Uri $Uri -UseBasicParsing -TimeoutSec 2
        return $response.StatusCode -ge 200 -and $response.StatusCode -lt 500
    } catch {
        return $false
    }
}

function Assert-PortAvailable {
    param(
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($listener) {
        throw "$Label 端口 $Port 已被进程 $($listener.OwningProcess) 占用。可关闭占用程序，或使用 -WebPort / -ApiPort 指定其他端口。"
    }
}

function Set-DotEnvValue {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Key,
        [Parameter(Mandatory = $true)][string]$Value,
        [string]$TemplatePath
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        if ($TemplatePath -and (Test-Path -LiteralPath $TemplatePath)) {
            Copy-Item -LiteralPath $TemplatePath -Destination $Path
        } else {
            New-Item -ItemType File -Path $Path -Force | Out-Null
        }
    }

    $lines = @(Get-Content -LiteralPath $Path -Encoding UTF8)
    $found = $false
    $updated = foreach ($line in $lines) {
        if ($line -match "^$([regex]::Escape($Key))=") {
            $found = $true
            "$Key=$Value"
        } else {
            $line
        }
    }
    if (-not $found) {
        $updated += "$Key=$Value"
    }
    Set-Content -LiteralPath $Path -Value $updated -Encoding UTF8
}

function Get-SetupState {
    if (-not (Test-Path -LiteralPath $setupFile)) {
        return $null
    }
    try {
        return Get-Content -LiteralPath $setupFile -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        return $null
    }
}

function Save-SetupState {
    param(
        [Parameter(Mandatory = $true)][string]$RequirementsHash,
        [Parameter(Mandatory = $true)][string]$PackageLockHash
    )

    @{
        requirementsHash = $RequirementsHash
        packageLockHash = $PackageLockHash
        updatedAtUtc = [DateTime]::UtcNow.ToString("o")
    } | ConvertTo-Json | Set-Content -LiteralPath $setupFile -Encoding UTF8
}

function Show-LogTail {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (Test-Path -LiteralPath $Path) {
        Write-Host "`n--- $(Split-Path -Leaf $Path) ---" -ForegroundColor DarkYellow
        Get-Content -LiteralPath $Path -Tail 30 -Encoding UTF8
    }
}

function Normalize-ProcessPathVariable {
    $variables = [Environment]::GetEnvironmentVariables()
    $pathKeys = @($variables.Keys | Where-Object {
        [string]::Equals([string]$_, "Path", [System.StringComparison]::OrdinalIgnoreCase)
    })
    if ($pathKeys.Count -le 1) {
        return
    }

    $pathValue = [string]$variables[$pathKeys[0]]
    foreach ($key in $pathKeys) {
        [Environment]::SetEnvironmentVariable([string]$key, $null, "Process")
    }
    [Environment]::SetEnvironmentVariable("Path", $pathValue, "Process")
}

$webUrl = "http://127.0.0.1:$WebPort"
$apiUrl = "http://127.0.0.1:$ApiPort"
$healthUrl = "$apiUrl/api/health"
$backendProcess = $null
$frontendProcess = $null

try {
    if ((Test-Path -LiteralPath $stateFile) -and
        (Test-HttpEndpoint -Uri $healthUrl) -and
        (Test-HttpEndpoint -Uri $webUrl)) {
        Write-Host "SnapNote 已经在运行。" -ForegroundColor Green
        Write-Host "  Web  $webUrl"
        Write-Host "  API  $apiUrl/docs"
        if (-not $NoBrowser) {
            Start-Process $webUrl
        }
        exit 0
    }

    if (Test-Path -LiteralPath $stateFile) {
        & $stopScript -Quiet
    }

    $condaCommand = Get-Command conda -ErrorAction SilentlyContinue
    if (-not $condaCommand) {
        throw "未找到 Conda。请先安装 Miniconda 或 Anaconda。"
    }
    $nodeCommand = Get-Command node -ErrorAction SilentlyContinue
    if (-not $nodeCommand) {
        throw "未找到 Node.js。请安装 Node.js 22.13 或更高版本。"
    }
    $npmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if (-not $npmCommand) {
        throw "未找到 npm.cmd。请重新安装 Node.js 并勾选添加到 PATH。"
    }

    $environmentExists = conda env list | Select-String -Pattern "^$([regex]::Escape($environmentName))\s"
    if (-not $environmentExists) {
        if ($SkipInstall) {
            throw "尚未创建 Conda 环境 '$environmentName'，首次启动不能使用 -SkipInstall。"
        }
        Write-Host "正在创建 Python 3.10 环境（仅首次需要）…" -ForegroundColor Cyan
        conda create -n $environmentName python=3.10 -y
        if ($LASTEXITCODE -ne 0) {
            throw "Conda 环境创建失败。"
        }
    }

    $requirementsPath = Join-Path $backendPath "requirements.txt"
    $packageLockPath = Join-Path $frontendPath "package-lock.json"
    $requirementsHash = (Get-FileHash -LiteralPath $requirementsPath -Algorithm SHA256).Hash
    $packageLockHash = (Get-FileHash -LiteralPath $packageLockPath -Algorithm SHA256).Hash
    $setupState = Get-SetupState
    $backendNeedsInstall = -not $setupState -or $setupState.requirementsHash -ne $requirementsHash
    $frontendNeedsInstall = -not (Test-Path -LiteralPath (Join-Path $frontendPath "node_modules")) -or
        -not $setupState -or $setupState.packageLockHash -ne $packageLockHash

    if (-not $SkipInstall) {
        if ($backendNeedsInstall) {
            Write-Host "正在安装后端依赖…" -ForegroundColor Cyan
            conda run -n $environmentName python -m pip install -r $requirementsPath --disable-pip-version-check
            if ($LASTEXITCODE -ne 0) {
                throw "后端依赖安装失败。"
            }
        }
        if ($frontendNeedsInstall) {
            Write-Host "正在安装前端依赖…" -ForegroundColor Cyan
            Push-Location $frontendPath
            try {
                npm.cmd ci --no-audit --no-fund
                if ($LASTEXITCODE -ne 0) {
                    throw "前端依赖安装失败。"
                }
            } finally {
                Pop-Location
            }
        }
        Save-SetupState -RequirementsHash $requirementsHash -PackageLockHash $packageLockHash
    } elseif ($backendNeedsInstall -or $frontendNeedsInstall) {
        throw "依赖尚未安装或锁文件已更新，请去掉 -SkipInstall 再启动一次。"
    }

    Assert-PortAvailable -Port $WebPort -Label "Web"
    Assert-PortAvailable -Port $ApiPort -Label "API"

    $backendEnv = Join-Path $backendPath ".env"
    $frontendEnv = Join-Path $frontendPath ".env.local"
    Set-DotEnvValue -Path $backendEnv -TemplatePath (Join-Path $backendPath ".env.example") -Key "FRONTEND_ORIGIN" -Value $webUrl
    Set-DotEnvValue -Path $frontendEnv -TemplatePath (Join-Path $frontendPath ".env.example") -Key "NEXT_PUBLIC_API_BASE_URL" -Value $apiUrl

    New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    $backendOutLog = Join-Path $logDirectory "backend.out.log"
    $backendErrorLog = Join-Path $logDirectory "backend.error.log"
    $frontendOutLog = Join-Path $logDirectory "frontend.out.log"
    $frontendErrorLog = Join-Path $logDirectory "frontend.error.log"

    Write-Host "正在启动 SnapNote…" -ForegroundColor Cyan
    Normalize-ProcessPathVariable
    $backendProcess = Start-Process -FilePath $condaCommand.Source `
        -ArgumentList @("run", "-n", $environmentName, "python", "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$ApiPort", "--reload") `
        -WorkingDirectory $backendPath -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $backendOutLog -RedirectStandardError $backendErrorLog

    $frontendProcess = Start-Process -FilePath $npmCommand.Source `
        -ArgumentList @("run", "dev", "--", "--host", "127.0.0.1", "--port", "$WebPort") `
        -WorkingDirectory $frontendPath -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $frontendOutLog -RedirectStandardError $frontendErrorLog

    @{
        projectRoot = $projectRoot
        startedAtUtc = [DateTime]::UtcNow.ToString("o")
        webPort = $WebPort
        apiPort = $ApiPort
        processes = @(
            @{ role = "api"; pid = $backendProcess.Id; startedAtUtc = $backendProcess.StartTime.ToUniversalTime().ToString("o") }
            @{ role = "web"; pid = $frontendProcess.Id; startedAtUtc = $frontendProcess.StartTime.ToUniversalTime().ToString("o") }
        )
    } | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $stateFile -Encoding UTF8

    $deadline = [DateTime]::UtcNow.AddMinutes(3)
    $apiReady = $false
    $webReady = $false
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($backendProcess.HasExited) {
            throw "API 进程提前退出。"
        }
        if ($frontendProcess.HasExited) {
            throw "Web 进程提前退出。"
        }

        if (-not $apiReady) {
            $apiReady = Test-HttpEndpoint -Uri $healthUrl
        }
        if (-not $webReady) {
            $webReady = Test-HttpEndpoint -Uri $webUrl
        }
        if ($apiReady -and $webReady) {
            break
        }
        Start-Sleep -Seconds 1
        $backendProcess.Refresh()
        $frontendProcess.Refresh()
    }

    if (-not ($apiReady -and $webReady)) {
        throw "服务在 3 分钟内未准备完成。"
    }

    Write-Host "SnapNote 已启动。" -ForegroundColor Green
    Write-Host "  Web  $webUrl"
    Write-Host "  API  $apiUrl/docs"
    Write-Host "  日志 $logDirectory" -ForegroundColor DarkGray
    Write-Host "关闭时双击 stop-snapnote.cmd，或运行 .\stop.ps1。" -ForegroundColor DarkGray

    if (-not $NoBrowser) {
        Start-Process $webUrl
    }
} catch {
    Write-Host "启动失败：$($_.Exception.Message)" -ForegroundColor Red
    if (Test-Path -LiteralPath $stateFile) {
        & $stopScript -Quiet
    } else {
        foreach ($process in @($backendProcess, $frontendProcess)) {
            if ($process -and -not $process.HasExited) {
                Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
            }
        }
    }
    Show-LogTail -Path (Join-Path $logDirectory "backend.error.log")
    Show-LogTail -Path (Join-Path $logDirectory "frontend.error.log")
    exit 1
}
