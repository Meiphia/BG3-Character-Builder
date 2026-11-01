"""Find the Baldur's Gate 3 game folder on macOS, Windows and Linux, and remember the user's choice.

The app needs the game's *Data* folder (the one with Shared.pak, Gustav.pak, Models.pak ...).
Users can point at the install folder, the .app bundle or Data itself; normalize() works it out.
"""

import json
import os
import re
import subprocess
import sys

REQUIRED = ("Shared.pak", "Gustav.pak")
APP_NAME = "Baldur's Gate Assembler"


# ---------------------------------------------------------------- settings

def config_dir():
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    elif os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, APP_NAME)


def _settings_path():
    return os.path.join(config_dir(), "settings.json")


def load_settings():
    try:
        with open(_settings_path(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(**kw):
    s = load_settings()
    s.update(kw)
    os.makedirs(config_dir(), exist_ok=True)
    with open(_settings_path(), "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)
    return s


# ---------------------------------------------------------------- validation

def is_data_dir(path):
    return bool(path) and os.path.isdir(path) and all(os.path.isfile(os.path.join(path, p)) for p in REQUIRED)


def normalize(path):
    """Install folder / .app bundle / Data folder (or a file inside one) -> the Data folder, or None."""
    if not path:
        return None
    path = os.path.abspath(os.path.expanduser(path.strip().strip('"')))
    if os.path.isfile(path):
        path = os.path.dirname(path)
    tries = [path,
             os.path.join(path, "Data"),
             os.path.join(path, "Contents", "Data"),
             os.path.join(path, "Baldur's Gate 3.app", "Contents", "Data"),
             os.path.join(path, "Baldurs Gate 3", "Data"),
             os.path.join(path, "Baldurs Gate 3", "Baldur's Gate 3.app", "Contents", "Data"),
             os.path.dirname(path)]                 # picked bin/ or Localization/
    for t in tries:
        if is_data_dir(t):
            return t
    return None


# ---------------------------------------------------------------- discovery

def _steam_roots():
    home = os.path.expanduser("~")
    roots = []
    if sys.platform == "darwin":
        roots.append(os.path.join(home, "Library", "Application Support", "Steam"))
    elif os.name == "nt":
        try:
            import winreg
            for hive, key in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
                              (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
                              (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam")):
                try:
                    with winreg.OpenKey(hive, key) as k:
                        for name in ("SteamPath", "InstallPath"):
                            try:
                                roots.append(winreg.QueryValueEx(k, name)[0])
                            except OSError:
                                pass
                except OSError:
                    pass
        except ImportError:
            pass
        for env in ("ProgramFiles(x86)", "ProgramFiles"):
            if os.environ.get(env):
                roots.append(os.path.join(os.environ[env], "Steam"))
    else:
        roots += [os.path.join(home, ".steam", "steam"), os.path.join(home, ".local", "share", "Steam"),
                  os.path.join(home, ".var", "app", "com.valvesoftware.Steam", ".local", "share", "Steam")]
    return [os.path.normpath(r) for r in roots if r]


def _steam_libraries():
    libs = []
    for root in _steam_roots():
        libs.append(root)
        vdf = os.path.join(root, "steamapps", "libraryfolders.vdf")
        try:
            with open(vdf, encoding="utf-8", errors="replace") as f:
                for m in re.finditer(r'"path"\s+"([^"]+)"', f.read()):
                    libs.append(m.group(1).replace("\\\\", "\\"))
        except OSError:
            pass
    out = []
    for lib in libs:
        lib = os.path.normpath(lib)
        if lib not in out:
            out.append(lib)
    return out


def candidates():
    """Likely install locations, most likely first (they may not exist)."""
    c = []
    for lib in _steam_libraries():
        game = os.path.join(lib, "steamapps", "common", "Baldurs Gate 3")
        c += [os.path.join(game, "Baldur's Gate 3.app", "Contents", "Data"), os.path.join(game, "Data")]
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        for apps in ("/Applications", os.path.join(home, "Applications")):
            c.append(os.path.join(apps, "Baldur's Gate 3.app", "Contents", "Data"))        # GOG / App Store
            c.append(os.path.join(apps, "Baldurs Gate 3.app", "Contents", "Data"))
    elif os.name == "nt":
        drives = [d + ":\\" for d in "CDEFG" if os.path.exists(d + ":\\")]
        for d in drives:
            c += [os.path.join(d, "GOG Games", "Baldurs Gate 3", "Data"),
                  os.path.join(d, "Program Files (x86)", "GOG Galaxy", "Games", "Baldurs Gate 3", "Data"),
                  os.path.join(d, "Program Files", "GOG Galaxy", "Games", "Baldurs Gate 3", "Data"),
                  os.path.join(d, "SteamLibrary", "steamapps", "common", "Baldurs Gate 3", "Data"),
                  os.path.join(d, "Games", "Baldurs Gate 3", "Data")]
    else:
        c.append(os.path.join(home, "GOG Games", "Baldurs Gate 3", "Data"))
    seen, out = set(), []
    for p in c:
        p = os.path.normpath(p)
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def detect():
    """All valid Data folders found on this machine."""
    return [p for p in candidates() if is_data_dir(p)]


def resolve(explicit=None):
    """-> (data_dir or None, source). Order: --data, saved setting, auto-detect."""
    if explicit:
        d = normalize(explicit)
        return (d, "argument") if d else (None, "argument-invalid")
    saved = normalize(load_settings().get("data_dir"))
    if saved:
        return saved, "saved"
    return None, "unset"


# ---------------------------------------------------------------- native folder picker

def pick_folder(start=None):
    """Open the OS folder dialog (works without pywebview). Returns a path or None."""
    start = start if start and os.path.isdir(start) else os.path.expanduser("~")
    try:
        if sys.platform == "darwin":
            script = ('POSIX path of (choose folder with prompt "Select the Baldur\'s Gate 3 install folder '
                      '(or the Baldur\'s Gate 3 app)" default location POSIX file "%s")' % start.replace('"', ''))
            r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=600)
            return r.stdout.strip() or None
        if os.name == "nt":
            ps = ("Add-Type -AssemblyName System.Windows.Forms;"
                  "$d=New-Object System.Windows.Forms.FolderBrowserDialog;"
                  "$d.Description='Select the Baldur''s Gate 3 install folder';"
                  "$d.SelectedPath='%s';"
                  "if($d.ShowDialog() -eq 'OK'){$d.SelectedPath}" % start.replace("'", "''"))
            r = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", ps],
                               capture_output=True, text=True, timeout=600)
            return r.stdout.strip() or None
        for cmd in (["zenity", "--file-selection", "--directory", "--filename", start + "/"],
                    ["kdialog", "--getexistingdirectory", start]):
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
                return r.stdout.strip() or None
            except FileNotFoundError:
                continue
    except Exception:
        return None
    return None
