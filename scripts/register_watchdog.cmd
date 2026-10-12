@echo off
chcp 65001 >nul
rem 注册看门狗计划任务（每 5 分钟；当前用户登录时运行；pythonw 无窗口）。
rem 卸载：schtasks /delete /tn "AI-Trading-Watchdog" /f
set "PYW=%~dp0..\.venv\Scripts\pythonw.exe"
set "SCRIPT=%~dp0..\backend\scripts\watchdog.py"
schtasks /create /f /tn "AI-Trading-Watchdog" /sc minute /mo 5 /tr "\"%PYW%\" \"%SCRIPT%\""
schtasks /query /tn "AI-Trading-Watchdog"
pause
