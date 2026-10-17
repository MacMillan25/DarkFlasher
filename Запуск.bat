@echo off
cd /d "%~dp0"
for /f "usebackq delims=" %%P in (`python -c "import sys;print(sys.executable)" 2^>nul`) do set "PY=%%P"
if not defined PY (
    echo Python не найден. Запусти DarkFlasher\DarkFlasher.exe
    pause
    exit /b 1
)
set "PYW=%PY:python.exe=pythonw.exe%"
if not exist "%PYW%" set "PYW=%PY%"
start "" "%PYW%" "%~dp0DarkFlasher.py"
