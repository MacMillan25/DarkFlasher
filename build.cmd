@echo off
cd /d "%~dp0"
if not exist build mkdir build
python -m PyInstaller --noconfirm --clean --onefile --windowed --name DarkFlasher ^
  --specpath build ^
  --icon "%CD%\app.ico" --add-data "%CD%\app.ico;." ^
  --version-file "%CD%\version_info.txt" ^
  --hidden-import serial --hidden-import serial.tools.list_ports ^
  darkflasher_qt.py
if errorlevel 1 goto error
echo.
echo Готово: dist\DarkFlasher.exe
goto end
:error
echo.
echo СБОРКА НЕ УДАЛАСЬ — смотри сообщения выше.
:end
pause
