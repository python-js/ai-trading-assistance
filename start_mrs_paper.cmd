@echo off
chcp 65001 >nul
rem MRS-1.0 paper trading runner (OKX BTC-USDT-SWAP 1m, PaperBroker only - NO real orders).
rem Close this window to stop. Ledger: data\mrs_paper\. Do NOT run two instances.
rem NOTE: keep this file ASCII-only - cmd.exe misparses UTF-8 text after chcp.
cd /d %~dp0
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
".venv\Scripts\python.exe" backend\scripts\run_mrs_paper.py --out-dir data\mrs_paper > data\logs\mrs_paper.log 2>&1
pause
