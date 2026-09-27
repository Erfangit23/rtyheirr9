@echo off
title XAU Trader Bot
cd /d "%~dp0"

REM Check for virtual environment
if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
)

:loop
REM Optional: AI parsing/commentary key (or put "ai": {"api_key": "..."} in config.json)
REM set NVIDIA_API_KEY=nvapi-xxxxxxxxxxxxxxxx
echo [%date% %time%] Starting XAU Trader Bot...
python main.py
echo [%date% %time%] Bot stopped with exit code %errorlevel%
echo Restarting in 10 seconds...
timeout /t 10 /nobreak >nul
goto loop
