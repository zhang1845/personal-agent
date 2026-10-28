@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"
if errorlevel 1 exit /b 1
if not exist ".venv\Scripts\python.exe" (
  echo Python environment not found. Run the setup CMD in this folder first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -X utf8 -B code\manage_service.py stop
set "AGENT_EXIT_CODE=%ERRORLEVEL%"
if not "%AGENT_EXIT_CODE%"=="0" pause
exit /b %AGENT_EXIT_CODE%
