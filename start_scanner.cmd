@echo off
chcp 65001 >nul
rem P0 扫描器常驻启动：实时信号 + 企业微信推送。关闭本窗口即停止。
rem 日志见 data\logs\scanner.log；请勿同时运行两个实例。
cd /d %~dp0
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
".venv\Scripts\python.exe" backend\scripts\run_scanner.py > data\logs\scanner.log 2>&1
pause
