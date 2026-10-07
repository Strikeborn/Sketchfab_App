@echo off
cd /d "%~dp0"
call .venv\Scripts\python.exe src\main.py
exit /b %ERRORLEVEL%
