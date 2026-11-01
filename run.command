#!/bin/bash
# Mei's BG3 Character Builder launcher — double-click in Finder or run ./run.command
# Creates a private Python environment in .venv on first run, then starts the app.
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "First run: setting up Python environment in .venv ..."
  python3 -m venv .venv || { echo "Could not create venv (is python3 installed?)"; exit 1; }
  .venv/bin/python -m pip install --upgrade pip -q
  .venv/bin/python -m pip install -q numpy pillow lz4 zstandard pywebview || {
    echo "pywebview failed to install; continuing without native window"; 
    .venv/bin/python -m pip install -q numpy pillow lz4 zstandard; }
fi
exec .venv/bin/python -m bg3asm app "$@"
