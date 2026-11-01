"""Entry point for packaged builds (PyInstaller): starts the app window."""
import sys
from bg3asm.__main__ import main

if __name__ == "__main__":
    sys.exit(main(["app"] + sys.argv[1:]))
