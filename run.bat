@echo off
setlocal
cd /d "%~dp0"
set "PY=python"
where py >nul 2>&1 && set "PY=py -3"
%PY% tools\bootstrap.py || goto :fail
".venv\Scripts\python.exe" -m scope1008 %*
if errorlevel 1 goto :fail
exit /b 0
:fail
pause
exit /b 1
