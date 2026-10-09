@echo off
chcp 65001 >nul
cd /d "%~dp0"
".venv\Scripts\python.exe" forward_test.py --include-images --max-messages 1
pause
