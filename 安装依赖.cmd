@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"
if errorlevel 1 exit /b 1
py -3.12 -c "import sys" >nul 2>nul
if errorlevel 1 goto use_python
py -3.12 -X utf8 -B code\setup_windows.py
goto complete
:use_python
python -X utf8 -B code\setup_windows.py
:complete
set "AGENT_EXIT_CODE=%ERRORLEVEL%"
if not "%AGENT_EXIT_CODE%"=="0" echo Setup failed. Install Python 3.12 x64 and check the error above.
pause
exit /b %AGENT_EXIT_CODE%
