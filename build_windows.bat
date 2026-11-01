@echo off
rem Builds "Baldur's Gate Assembler.exe" (a folder in dist\) with PyInstaller. Run once on Windows.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" call run.bat --help >nul 2>&1
".venv\Scripts\python.exe" -m pip install -q pyinstaller
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --windowed --name "Baldur's Gate Assembler" ^
  --icon bg3asm\web\icon.ico --add-data "bg3asm\web;bg3asm\web" --collect-submodules bg3asm ^
  --hidden-import webview -p . bga_app.py
echo.
echo Done: dist\Baldur's Gate Assembler\Baldur's Gate Assembler.exe
pause
