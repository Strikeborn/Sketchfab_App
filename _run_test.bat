@echo off
cd /d "%~dp0"
call .venv\Scripts\python.exe src\collector.py --fix-encoding
call .venv\Scripts\python.exe src\collector.py --gui
pause