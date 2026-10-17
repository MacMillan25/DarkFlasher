@echo off
cd /d "%~dp0"
pyinstaller --noconfirm --clean --onedir --windowed --name DarkFlasher ^
  --icon app.ico --add-data "app.ico;." --version-file version_info.txt DarkFlasher.py
echo.
echo Готово: dist\DarkFlasher\DarkFlasher.exe
pause
