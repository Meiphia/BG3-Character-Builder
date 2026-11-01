@echo off
rem Builds "Mei's BG3 Character Builder.exe" (a folder in dist\) with PyInstaller. Run once on Windows.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" call run.bat --help >nul 2>&1
".venv\Scripts\python.exe" -m pip install -q pyinstaller
".venv\Scripts\python.exe" -m PyInstaller --noconfirm release.spec
echo.
echo Done: dist\Mei's BG3 Character Builder\Mei's BG3 Character Builder.exe
pause
