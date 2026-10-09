@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 測試群續傳：最多十分鐘、四則；停止請按 Ctrl+C。
echo 目前仍需 LINE 來源聊天室保持前景、Windows 不鎖定。
".venv\Scripts\python.exe" forward_test.py --resume-progress --allow-normal-text --include-images --max-messages 4
pause
