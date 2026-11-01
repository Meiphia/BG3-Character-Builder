"""Entry point for the packaged app (PyInstaller): starts the app window.

A packaged app can be launched from anywhere (Finder starts it in /), so it keeps its cache, saved characters
and Mods folder in the per-user settings folder instead of the current directory:
  macOS   ~/Library/Application Support/Mei's BG3 Character Builder/
  Windows %APPDATA%\\Mei's BG3 Character Builder\\
"""
import os
import sys


def _selftest():
    """Used by the release build: the bundle imports everything and finds its web UI."""
    from bg3asm import __version__, app
    import numpy, PIL, lz4, zstandard, webview  # noqa: F401
    assert os.path.isfile(os.path.join(app.WEB_DIR, "index.html")), app.WEB_DIR
    print("selftest ok", __version__)
    return 0


def main():
    if "--selftest" in sys.argv:
        try:                    # windowed builds have no console: report through the exit code, never a dialog
            return _selftest()
        except Exception:
            import traceback
            with open("selftest-error.txt", "w") as f:
                traceback.print_exc(file=f)
            return 1
    if getattr(sys, "frozen", False):
        from bg3asm import locate
        d = locate.config_dir()
        os.makedirs(d, exist_ok=True)
        os.chdir(d)
    from bg3asm.__main__ import main as cli
    return cli(["app"] + sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
