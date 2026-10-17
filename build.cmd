@echo off
cd /d "%~dp0"
pyinstaller --noconfirm --clean --onedir --windowed --name DarkFlasher ^
  --icon app.ico --add-data "app.ico;." --version-file version_info.txt ^
  --hidden-import serial --hidden-import serial.tools.list_ports ^
  DarkFlasher.py
echo.
echo Готово: dist\DarkFlasher\DarkFlasher.exe
pause
