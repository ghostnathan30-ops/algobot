# AlgoBot PowerShell Launcher
# ============================
# Starts the dashboard server and paper trading loop.
# Run from PowerShell (NOT cmd): .\scripts\start_bot.ps1
#
# Prerequisites:
#   - TWS is open on port 7497 (paper account, API enabled)
#   - Run at or before 09:00 ET on a US trading day

$ErrorActionPreference = "Stop"

$PROJECT = "C:\Users\ghost\Documents\Claude Workflow\Trading\AlgoBot"
$PYTHON  = "C:\Users\ghost\miniconda3\envs\algobot_env\python.exe"

if (-not (Test-Path $PYTHON)) {
    Write-Error "Python not found: $PYTHON`nCheck your conda env or update the PYTHON path in this script."
    exit 1
}

$LogDir = "$PROJECT\logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }
$Date    = Get-Date -Format "yyyy-MM-dd"
$LogFile = "$LogDir\bot_$Date.log"

Set-Location $PROJECT

Write-Host ""
Write-Host "=================================================" -ForegroundColor Cyan
Write-Host "  AlgoBot Paper Trading Launcher" -ForegroundColor Cyan
Write-Host "  $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -ForegroundColor Cyan
Write-Host "=================================================" -ForegroundColor Cyan

# ── Step 1: Start dashboard server in a new window ─────────────────────────
Write-Host "`n[1/2] Starting dashboard server (http://localhost:8000)..." -ForegroundColor Yellow
# NOTE: double-quotes around paths are required here — paths contain spaces.
$dashCmd = "cd `"$PROJECT`"; `$env:PYTHONUTF8='1'; `$env:PYTHONIOENCODING='utf-8'; & `"$PYTHON`" -m uvicorn dashboard.server:app --host 127.0.0.1 --port 8000"
Start-Process powershell -ArgumentList "-NoExit", "-Command", $dashCmd -WindowStyle Normal

Write-Host "  Waiting 8 seconds for dashboard to start..." -ForegroundColor Gray
Start-Sleep -Seconds 8

# ── Step 2: Run paper trading loop (blocks until 16:00 ET then exits) ──────
Write-Host "`n[2/2] Starting paper trading loop..." -ForegroundColor Yellow
Write-Host "  Log file: $LogFile" -ForegroundColor Gray
Write-Host "  Press Ctrl+C to stop the bot gracefully.`n" -ForegroundColor Gray

$env:PYTHONUTF8 = "1"
& $PYTHON "$PROJECT\scripts\run_paper_trading.py" 2>&1 | Tee-Object -FilePath $LogFile -Append

Write-Host "`nBot exited at $(Get-Date -Format 'HH:mm:ss')." -ForegroundColor Yellow
