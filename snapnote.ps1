param(
    [ValidateSet("menu", "start", "stop", "restart", "status", "logs")]
    [string]$Action = "menu",
    [switch]$SkipInstall,
    [switch]$NoBrowser,
    [switch]$Quiet,
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

function Get-ServiceUrls {
    param(
        [int]$SelectedWebPort = $WebPort,
        [int]$SelectedApiPort = $ApiPort
    )
    return @{
        Web = "http://127.0.0.1:$SelectedWebPort"
        Api = "http://127.0.0.1:$SelectedApiPort"
        Health = "http://127.0.0.1:$SelectedApiPort/api/health"
    }
}

function Test-HttpEndpoint {
    param([Parameter(Mandatory = $true)][string]$Uri)
    $response = $null
    try {
        # Loopback health checks must not be routed through HTTP_PROXY/HTTPS_PROXY.
        $request = [System.Net.HttpWebRequest]::Create($Uri)
        $request.Proxy = $null
        $request.Timeout = 2000
        $request.ReadWriteTimeout = 2000
        $response = $request.GetResponse()
        $statusCode = [int]$response.StatusCode
        return $statusCode -ge 200 -and $statusCode -lt 500
    } catch {
        return $false
    } finally {
        if ($response) { $response.Close() }
    }
}

function Add-LoopbackProxyBypass {
    # Child processes and the browser launched below inherit these values.
    # Preserve any existing bypass list while ensuring local API traffic never
    # goes through HTTP_PROXY/HTTPS_PROXY.
    $entries = @(
        @($env:NO_PROXY -split ',')
        @($env:no_proxy -split ',')
        '127.0.0.1'
        'localhost'
        '::1'
    ) | ForEach-Object { $_.Trim() } | Where-Object { $_ } | Select-Object -Unique
    $value = $entries -join ','
    $env:NO_PROXY = $value
    $env:no_proxy = $value
}

function Assert-PortAvailable {
    param(
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $probe = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
    try {
        $probe.Start()
    } catch [System.Net.Sockets.SocketException] {
        throw "$Label 端口 $Port 已被占用。请先运行 .\snapnote.cmd status，或指定其他端口。"
    } finally {
        $probe.Stop()
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
        } else { $line }
    }
    if (-not $found) { $updated += "$Key=$Value" }
    Set-Content -LiteralPath $Path -Value $updated -Encoding UTF8
}

function Get-SetupState {
    if (-not (Test-Path -LiteralPath $setupFile)) { return $null }
    try {
        return Get-Content -LiteralPath $setupFile -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch { return $null }
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

function Get-Sha256Hash {
    param([Parameter(Mandatory = $true)][string]$Path)
    $stream = $null
    $algorithm = $null
    try {
        $stream = [System.IO.File]::OpenRead($Path)
        $algorithm = [System.Security.Cryptography.SHA256]::Create()
        $bytes = $algorithm.ComputeHash($stream)
        return ([System.BitConverter]::ToString($bytes)).Replace("-", "")
    } finally {
        if ($algorithm) { $algorithm.Dispose() }
        if ($stream) { $stream.Dispose() }
    }
}

function Get-RunState {
    if (-not (Test-Path -LiteralPath $stateFile)) { return $null }
    try {
        $state = Get-Content -LiteralPath $stateFile -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($state.projectRoot -ne $projectRoot) { throw "进程记录不属于当前项目。" }
        return $state
    } catch {
        throw "无法读取进程记录 $stateFile：$($_.Exception.Message)"
    }
}

function Test-TrackedProcess {
    param([Parameter(Mandatory = $true)]$Record)
    $process = Get-Process -Id ([int]$Record.pid) -ErrorAction SilentlyContinue
    if (-not $process) { return $false }
    try {
        $recordedStart = [DateTime]::Parse([string]$Record.startedAtUtc).ToUniversalTime()
        $actualStart = $process.StartTime.ToUniversalTime()
        return [Math]::Abs(($actualStart - $recordedStart).TotalSeconds) -le 3
    } catch { return $false }
}

function Stop-TrackedProcessTree {
    param([Parameter(Mandatory = $true)]$Record)
    if (-not (Test-TrackedProcess -Record $Record)) { return }
    $trackedPid = [int]$Record.pid
    $taskkill = Get-Command taskkill.exe -ErrorAction SilentlyContinue
    if ($taskkill) {
        $previousPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = "Continue"
            & $taskkill.Source /PID $trackedPid /T /F *> $null
        } finally { $ErrorActionPreference = $previousPreference }
    }
    if (Get-Process -Id $trackedPid -ErrorAction SilentlyContinue) {
        Stop-Process -Id $trackedPid -Force -ErrorAction SilentlyContinue
    }
}

function Show-LogTail {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [int]$Lines = 40
    )
    if (Test-Path -LiteralPath $Path) {
        Write-Host "`n--- $(Split-Path -Leaf $Path) ---" -ForegroundColor DarkYellow
        Get-Content -LiteralPath $Path -Tail $Lines -Encoding UTF8
    }
}

function Show-AllLogs {
    New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    foreach ($name in @("backend.error.log", "backend.out.log", "frontend.error.log", "frontend.out.log")) {
        Show-LogTail -Path (Join-Path $logDirectory $name)
    }
    Write-Host "`n日志目录：$logDirectory" -ForegroundColor DarkGray
}

function Normalize-ProcessPathVariable {
    $variables = [Environment]::GetEnvironmentVariables()
    $pathKeys = @($variables.Keys | Where-Object {
        [string]::Equals([string]$_, "Path", [System.StringComparison]::OrdinalIgnoreCase)
    })
    if ($pathKeys.Count -le 1) { return }
    $pathValue = [string]$variables[$pathKeys[0]]
    foreach ($key in $pathKeys) {
        [Environment]::SetEnvironmentVariable([string]$key, $null, "Process")
    }
    [Environment]::SetEnvironmentVariable("Path", $pathValue, "Process")
}

function Resolve-EnvironmentPython {
    $output = @(& conda run -n $environmentName python -c "import sys; print(sys.executable)" 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "无法解析 Conda 环境 '$environmentName' 的 Python：$($output -join ' ')"
    }
    $candidate = $output | ForEach-Object { [string]$_ } |
        Where-Object { $_.Trim() -match "python(?:\.exe)?$" } | Select-Object -Last 1
    if (-not $candidate) { throw "Conda 没有返回可用的 Python 路径。" }
    $pythonPath = $candidate.Trim()
    if (-not (Test-Path -LiteralPath $pythonPath)) { throw "Python 不存在：$pythonPath" }
    return $pythonPath
}

function Invoke-Status {
    $state = Get-RunState
    if ($state) {
        $urls = Get-ServiceUrls -SelectedWebPort ([int]$state.webPort) -SelectedApiPort ([int]$state.apiPort)
    } else { $urls = Get-ServiceUrls }
    $webReady = Test-HttpEndpoint -Uri $urls.Web
    $apiReady = Test-HttpEndpoint -Uri $urls.Health
    $tracked = @()
    if ($state) { $tracked = @($state.processes | Where-Object { Test-TrackedProcess -Record $_ }) }
    if ($webReady -and $apiReady) {
        Write-Host "SnapNote 正在运行。" -ForegroundColor Green
    } elseif ($webReady -or $apiReady) {
        Write-Host "SnapNote 仅部分服务可用。" -ForegroundColor Yellow
    } else { Write-Host "SnapNote 未运行。" -ForegroundColor Yellow }
    Write-Host "  Web  $($urls.Web)  $(if ($webReady) {'可用'} else {'不可用'})"
    Write-Host "  API  $($urls.Api)/docs  $(if ($apiReady) {'可用'} else {'不可用'})"
    Write-Host "  跟踪中的有效进程：$($tracked.Count)"
    return $webReady -and $apiReady
}

function Invoke-Stop {
    param([switch]$Silent)
    $state = Get-RunState
    if (-not $state) {
        if (-not $Silent) { Write-Host "SnapNote 当前没有由统一脚本启动的服务。" -ForegroundColor Yellow }
        return
    }
    foreach ($record in @($state.processes)) { Stop-TrackedProcessTree -Record $record }
    $urls = Get-ServiceUrls -SelectedWebPort ([int]$state.webPort) -SelectedApiPort ([int]$state.apiPort)
    $deadline = [DateTime]::UtcNow.AddSeconds(5)
    do {
        Start-Sleep -Milliseconds 250
        $webAlive = Test-HttpEndpoint -Uri $urls.Web
        $apiAlive = Test-HttpEndpoint -Uri $urls.Health
    } while (($webAlive -or $apiAlive) -and [DateTime]::UtcNow -lt $deadline)
    if ($webAlive -or $apiAlive) {
        throw "已结束跟踪进程，但仍有服务占用 SnapNote 端口。请运行 .\snapnote.cmd status 检查。"
    }
    Remove-Item -LiteralPath $stateFile -Force -ErrorAction SilentlyContinue
    if (-not $Silent) {
        Write-Host "SnapNote 已关闭。" -ForegroundColor Green
        Write-Host "日志仍保留在 .snapnote-logs 目录。" -ForegroundColor DarkGray
    }
}

function Invoke-Start {
    $urls = Get-ServiceUrls
    $state = Get-RunState
    if ($state -and (Test-HttpEndpoint -Uri $urls.Health) -and (Test-HttpEndpoint -Uri $urls.Web)) {
        Write-Host "SnapNote 已经在运行。" -ForegroundColor Green
        Write-Host "  Web  $($urls.Web)"
        Write-Host "  API  $($urls.Api)/docs"
        if (-not $NoBrowser) { Start-Process $urls.Web }
        return
    }
    if ($state) { Invoke-Stop -Silent }

    $condaCommand = Get-Command conda -ErrorAction SilentlyContinue
    if (-not $condaCommand) { throw "未找到 Conda。请先安装 Miniconda 或 Anaconda。" }
    $nodeCommand = Get-Command node -ErrorAction SilentlyContinue
    if (-not $nodeCommand) { throw "未找到 Node.js。请安装 Node.js 22.13 或更高版本。" }
    $npmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if (-not $npmCommand) { throw "未找到 npm.cmd。请重新安装 Node.js并添加到 PATH。" }

    $environmentExists = conda env list | Select-String -Pattern "^$([regex]::Escape($environmentName))\s"
    if (-not $environmentExists) {
        if ($SkipInstall) { throw "尚未创建 Conda 环境 '$environmentName'，首次启动不能使用 -SkipInstall。" }
        Write-Host "正在创建 Python 3.10 环境（仅首次需要）…" -ForegroundColor Cyan
        conda create -n $environmentName python=3.10 -y
        if ($LASTEXITCODE -ne 0) { throw "Conda 环境创建失败。" }
    }

    $requirementsPath = Join-Path $backendPath "requirements.txt"
    $packageLockPath = Join-Path $frontendPath "package-lock.json"
    $requirementsHash = Get-Sha256Hash -Path $requirementsPath
    $packageLockHash = Get-Sha256Hash -Path $packageLockPath
    $setupState = Get-SetupState
    $backendNeedsInstall = -not $setupState -or $setupState.requirementsHash -ne $requirementsHash
    $frontendNeedsInstall = -not (Test-Path -LiteralPath (Join-Path $frontendPath "node_modules")) -or
        -not $setupState -or $setupState.packageLockHash -ne $packageLockHash
    if (-not $SkipInstall) {
        if ($backendNeedsInstall) {
            Write-Host "正在安装后端依赖…" -ForegroundColor Cyan
            conda run -n $environmentName python -m pip install -r $requirementsPath --disable-pip-version-check
            if ($LASTEXITCODE -ne 0) { throw "后端依赖安装失败。" }
        }
        if ($frontendNeedsInstall) {
            Write-Host "正在安装前端依赖…" -ForegroundColor Cyan
            Push-Location $frontendPath
            try {
                npm.cmd ci --no-audit --no-fund
                if ($LASTEXITCODE -ne 0) { throw "前端依赖安装失败。" }
            } finally { Pop-Location }
        }
        Save-SetupState -RequirementsHash $requirementsHash -PackageLockHash $packageLockHash
    } elseif ($backendNeedsInstall -or $frontendNeedsInstall) {
        throw "依赖尚未安装或锁文件已更新，请去掉 -SkipInstall 再启动一次。"
    }

    Assert-PortAvailable -Port $WebPort -Label "Web"
    Assert-PortAvailable -Port $ApiPort -Label "API"
    $pythonPath = Resolve-EnvironmentPython
    $frontendCli = Join-Path $frontendPath "node_modules\vinext\dist\cli.js"
    if (-not (Test-Path -LiteralPath $frontendCli)) {
        throw "未找到 Vinext CLI，请去掉 -SkipInstall 重新安装前端依赖。"
    }

    Set-DotEnvValue -Path (Join-Path $backendPath ".env") -TemplatePath (Join-Path $backendPath ".env.example") -Key "FRONTEND_ORIGIN" -Value $urls.Web
    Set-DotEnvValue -Path (Join-Path $frontendPath ".env.local") -TemplatePath (Join-Path $frontendPath ".env.example") -Key "NEXT_PUBLIC_API_BASE_URL" -Value $urls.Api
    New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
    $backendOutLog = Join-Path $logDirectory "backend.out.log"
    $backendErrorLog = Join-Path $logDirectory "backend.error.log"
    $frontendOutLog = Join-Path $logDirectory "frontend.out.log"
    $frontendErrorLog = Join-Path $logDirectory "frontend.error.log"

    Write-Host "正在启动 SnapNote…" -ForegroundColor Cyan
    Normalize-ProcessPathVariable
    Add-LoopbackProxyBypass
    $backendProcess = Start-Process -FilePath $pythonPath -ArgumentList @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$ApiPort", "--reload") -WorkingDirectory $backendPath -WindowStyle Hidden -PassThru -RedirectStandardOutput $backendOutLog -RedirectStandardError $backendErrorLog
    $quotedFrontendCli = '"' + $frontendCli + '"'
    $frontendProcess = Start-Process -FilePath $nodeCommand.Source -ArgumentList @($quotedFrontendCli, "dev", "--host", "127.0.0.1", "--port", "$WebPort") -WorkingDirectory $frontendPath -WindowStyle Hidden -PassThru -RedirectStandardOutput $frontendOutLog -RedirectStandardError $frontendErrorLog

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

    try {
        $deadline = [DateTime]::UtcNow.AddMinutes(3)
        $apiReady = $false
        $webReady = $false
        while ([DateTime]::UtcNow -lt $deadline) {
            if (-not $apiReady) { $apiReady = Test-HttpEndpoint -Uri $urls.Health }
            if (-not $webReady) { $webReady = Test-HttpEndpoint -Uri $urls.Web }
            if ($apiReady -and $webReady) { break }
            Start-Sleep -Seconds 1
        }
        if (-not ($apiReady -and $webReady)) { throw "服务在 3 分钟内未准备完成。" }
    } catch {
        $startupError = $_.Exception.Message
        try {
            Invoke-Stop -Silent
        } catch {
            Write-Host "自动清理未完成：$($_.Exception.Message)" -ForegroundColor Yellow
        }
        Show-LogTail -Path $backendErrorLog -Lines 30
        Show-LogTail -Path $frontendErrorLog -Lines 30
        throw $startupError
    }

    Write-Host "SnapNote 已启动。" -ForegroundColor Green
    Write-Host "  Web  $($urls.Web)"
    Write-Host "  API  $($urls.Api)/docs"
    Write-Host "  日志 $logDirectory" -ForegroundColor DarkGray
    Write-Host "管理命令：.\snapnote.cmd status|stop|restart|logs" -ForegroundColor DarkGray
    if (-not $NoBrowser) { Start-Process $urls.Web }
}

function Show-Menu {
    Clear-Host
    Write-Host "==============================" -ForegroundColor Cyan
    Write-Host "       SnapNote 控制台" -ForegroundColor Cyan
    Write-Host "==============================" -ForegroundColor Cyan
    Write-Host "  1. 启动"
    Write-Host "  2. 关闭"
    Write-Host "  3. 重启"
    Write-Host "  4. 查看状态"
    Write-Host "  5. 查看日志"
    Write-Host "  0. 退出"
    Write-Host ""
    $choice = Read-Host "请选择"
    switch ($choice) {
        "1" { Invoke-Start }
        "2" { Invoke-Stop }
        "3" { Invoke-Stop -Silent; Invoke-Start }
        "4" { Invoke-Status | Out-Null }
        "5" { Show-AllLogs }
        "0" { return }
        default { throw "无效选项：$choice" }
    }
    Write-Host ""
    Read-Host "按 Enter 退出" | Out-Null
}

try {
    switch ($Action) {
        "menu" { Show-Menu }
        "start" { Invoke-Start }
        "stop" { Invoke-Stop -Silent:$Quiet }
        "restart" { Invoke-Stop -Silent; Invoke-Start }
        "status" { Invoke-Status | Out-Null }
        "logs" { Show-AllLogs }
    }
    exit 0
} catch {
    Write-Host "SnapNote $Action 失败：$($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
