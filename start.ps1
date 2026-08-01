param(
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$backendPath = Join-Path $projectRoot "backend"
$frontendPath = Join-Path $projectRoot "frontend"
$environmentName = "snapnote"

if (-not (Get-Command conda -ErrorAction SilentlyContinue)) {
    throw "未找到 Conda。请先安装 Miniconda/Anaconda，或手动使用 Python 3.10 启动 backend。"
}
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    throw "未找到 Node.js。请安装 Node.js 22.13 或更高版本。"
}

$environmentExists = conda env list | Select-String -Pattern "^$environmentName\s"
if (-not $environmentExists) {
    Write-Host "正在创建 Python 3.10 环境…" -ForegroundColor Cyan
    conda create -n $environmentName python=3.10 -y
}

if (-not $SkipInstall) {
    Write-Host "正在检查后端依赖…" -ForegroundColor Cyan
    conda run -n $environmentName pip install -r (Join-Path $backendPath "requirements.txt") -q
    Write-Host "正在检查前端依赖…" -ForegroundColor Cyan
    Push-Location $frontendPath
    try { npm.cmd install --no-audit --no-fund } finally { Pop-Location }
}

$backendEnv = Join-Path $backendPath ".env"
if (-not (Test-Path $backendEnv)) {
    Copy-Item (Join-Path $backendPath ".env.example") $backendEnv
}

$frontendEnv = Join-Path $frontendPath ".env.local"
if (-not (Test-Path $frontendEnv)) {
    "NEXT_PUBLIC_API_BASE_URL=http://localhost:8001" | Set-Content -Encoding UTF8 $frontendEnv
}

Write-Host "SnapNote 正在启动：" -ForegroundColor Green
Write-Host "  Web  http://localhost:3000"
Write-Host "  API  http://localhost:8001/docs"
Write-Host "按 Ctrl+C 停止。" -ForegroundColor DarkGray

$backendJob = Start-Job -Name "SnapNote-API" -ScriptBlock {
    param($path, $envName)
    Set-Location $path
    conda run -n $envName python -m uvicorn app.main:app --host 127.0.0.1 --port 8001 --reload
} -ArgumentList $backendPath, $environmentName

$frontendJob = Start-Job -Name "SnapNote-Web" -ScriptBlock {
    param($path)
    Set-Location $path
    npm.cmd run dev
} -ArgumentList $frontendPath

try {
    while ($backendJob.State -eq "Running" -and $frontendJob.State -eq "Running") {
        Receive-Job $backendJob
        Receive-Job $frontendJob
        Start-Sleep -Milliseconds 500
    }
} finally {
    Stop-Job $backendJob, $frontendJob -ErrorAction SilentlyContinue
    Remove-Job $backendJob, $frontendJob -Force -ErrorAction SilentlyContinue
}
