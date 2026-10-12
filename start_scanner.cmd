@echo off
chcp 65001 >nul
rem Long-running scanner: realtime signals + WeCom/Feishu push. Close this window to stop.
rem Log: data\logs\scanner.log. Do NOT run two instances.
rem NOTE: keep this file ASCII-only - cmd.exe misparses UTF-8 text after chcp.
cd /d %~dp0
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
".venv\Scripts\python.exe" backend\scripts\run_scanner.py > data\logs\scanner.log 2>&1
pause
