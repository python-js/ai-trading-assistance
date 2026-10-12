@echo off
chcp 65001 >nul
rem H5 看板后端（FastAPI）：给前端 next dev/prod 提供 /api/*；关闭本窗口即停止。
rem 日志见 data\logs\api.log；前端见 frontend/（npm run dev，端口 3000）。
cd /d %~dp0
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
".venv\Scripts\python.exe" -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000 > data\logs\api.log 2>&1
pause
