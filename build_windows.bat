@echo off
REM build_windows.bat — builds sidor-logger.exe on Windows using PyInstaller.
REM Run this from a Windows command prompt, inside the project folder,
REM with your virtual environment activated.

echo Installing build dependencies...
pip install -r requirements.txt
pip install pyinstaller

echo Building sidor-logger.exe ...
pyinstaller --noconfirm --onedir --windowed ^
    --name sidor-logger ^
    --collect-submodules matplotlib ^
    --add-data "sidor_logger\assets;assets" ^
    run.py

echo.
echo Done. The executable is at dist\sidor-logger.exe
pause
