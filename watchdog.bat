@echo off
REM ===================================================================
REM  XAU Trader Bot - watchdog
REM
REM  Run every 5 minutes from Task Scheduler (as the same user that
REM  runs the bot). It restarts whatever is missing so a reboot, a
REM  crash or a logoff cannot leave the bot dead:
REM    1. MetaTrader 5 (terminal64.exe)
REM    2. the bot itself (python.exe)
REM
REM  EDIT the MT5_EXE line below if MetaTrader is installed elsewhere.
REM ===================================================================
setlocal
cd /d "%~dp0"

set MT5_EXE=C:\Program Files\MetaTrader 5\terminal64.exe
set LOG=logs\watchdog.log

if not exist logs mkdir logs

REM ---------- 1) MetaTrader 5 ----------
tasklist /FI "IMAGENAME eq terminal64.exe" 2>nul | find /I "terminal64.exe" >nul
if errorlevel 1 (
    if exist "%MT5_EXE%" (
        echo [%date% %time%] MT5 was not running - starting it >> "%LOG%"
        start "" "%MT5_EXE%"
        timeout /t 20 /nobreak >nul
    ) else (
        echo [%date% %time%] MT5 not running and MT5_EXE not found: %MT5_EXE% >> "%LOG%"
    )
)

REM ---------- 2) the bot ----------
REM Only this bot runs python on the VPS, so a python.exe process is a
REM reliable "the bot is alive" marker. start.bat keeps it running.
tasklist /FI "IMAGENAME eq python.exe" 2>nul | find /I "python.exe" >nul
if errorlevel 1 (
    echo [%date% %time%] Bot was not running - starting start.bat >> "%LOG%"
    start "XAU Trader Bot" /min cmd /c start.bat
)

endlocal
