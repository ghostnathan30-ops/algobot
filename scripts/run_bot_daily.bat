@echo off
REM AlgoBot Daily Runner
REM Launched by Windows Task Scheduler every weekday at 9:00 AM ET
REM Keeps a log in logs\bot_YYYY-MM-DD.log

set PROJECT=C:\Users\ghost\Documents\Claude Workflow\Trading\AlgoBot
set LOG_DIR=%PROJECT%\logs
set LOG_FILE=%LOG_DIR%\bot_%date:~10,4%-%date:~4,2%-%date:~7,2%.log

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

echo ============================================= >> "%LOG_FILE%"
echo AlgoBot started at %date% %time%             >> "%LOG_FILE%"
echo ============================================= >> "%LOG_FILE%"

cd /d "%PROJECT%"

REM Fix Windows encoding (avoid garbled UTF-16 in logs)
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

REM Step 1: Start dashboard server in a separate window (stays open all day)
set DASH_LOG=%LOG_DIR%\dashboard_%date:~10,4%-%date:~4,2%-%date:~7,2%.log
start "AlgoBot Dashboard" cmd /c "set PYTHONUTF8=1 && set PYTHONIOENCODING=utf-8 && cd /d "%PROJECT%" && "C:\Users\ghost\miniconda3\envs\algobot_env\python.exe" -m uvicorn dashboard.server:app --host 127.0.0.1 --port 8000 >> "%DASH_LOG%" 2>&1"

REM Wait 8 seconds for dashboard to start
timeout /t 8 /nobreak > nul

REM Step 2: Run paper trading loop (blocks until 16:00 ET then exits)
"C:\Users\ghost\miniconda3\envs\algobot_env\python.exe" scripts\run_paper_trading.py >> "%LOG_FILE%" 2>&1

echo Bot exited at %time% >> "%LOG_FILE%"
