@echo off
rem Baldur's Gate Assembler launcher for Windows: double-click to start.
rem Creates a private Python environment in .venv on first run, then starts the app.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo First run: setting up Python environment in .venv ...
  py -3 -m venv .venv 2>nul || python -m venv .venv || (echo Could not create venv - install Python 3 from python.org & pause & exit /b 1)
  ".venv\Scripts\python.exe" -m pip install --upgrade pip -q
  ".venv\Scripts\python.exe" -m pip install -q numpy pillow lz4 zstandard pywebview || (
    echo pywebview failed to install; continuing without native window
    ".venv\Scripts\python.exe" -m pip install -q numpy pillow lz4 zstandard )
)
".venv\Scripts\python.exe" -m bg3asm app %*
if errorlevel 1 pause
