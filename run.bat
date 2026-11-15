@echo off
cd /d "%~dp0"
python -m scope1008 %*
if errorlevel 1 pause
