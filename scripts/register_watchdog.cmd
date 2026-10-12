@echo off
chcp 65001 >nul
rem Register the watchdog scheduled task (every 5 min, pythonw, no window).
rem Uninstall: schtasks /delete /tn "AI-Trading-Watchdog" /f
rem NOTE: keep this file ASCII-only - cmd.exe misparses UTF-8 text after chcp.
set "PYW=%~dp0..\.venv\Scripts\pythonw.exe"
set "SCRIPT=%~dp0..\backend\scripts\watchdog.py"
schtasks /create /f /tn "AI-Trading-Watchdog" /sc minute /mo 5 /tr "\"%PYW%\" \"%SCRIPT%\""
schtasks /query /tn "AI-Trading-Watchdog"
pause
