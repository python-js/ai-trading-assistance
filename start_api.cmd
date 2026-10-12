@echo off
chcp 65001 >nul
rem H5 dashboard backend (FastAPI) for the frontend; log: data\logs\api.log
rem Frontend runs separately: cd frontend && npm run dev (port 3000). Close this window to stop.
rem NOTE: keep this file ASCII-only - cmd.exe misparses UTF-8 text after chcp.
cd /d %~dp0
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
".venv\Scripts\python.exe" -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000 > data\logs\api.log 2>&1
pause
