@echo off
setlocal
cd /d "%~dp0"

set PORT=8777
set URL=http://127.0.0.1:%PORT%

echo Installing/checking dependencies...
python -m pip install -q -r requirements.txt
if errorlevel 1 (
    echo.
    echo Failed to install dependencies. Is Python installed and on PATH?
    pause
    exit /b 1
)

echo Starting Broadlink SmartIR Studio on %URL% ...
start "Broadlink SmartIR Studio" cmd /k "python server.py"

echo Waiting for the server to come up...
:waitloop
timeout /t 1 /nobreak >nul
powershell -NoProfile -Command "try { (Invoke-WebRequest -Uri '%URL%/api/platforms' -UseBasicParsing -TimeoutSec 1) | Out-Null; exit 0 } catch { exit 1 }"
if errorlevel 1 goto waitloop

start "" "%URL%"

echo.
echo The server is running in the other window titled "Broadlink SmartIR Studio".
echo Close that window (or press Ctrl+C inside it) to stop the server.
endlocal
