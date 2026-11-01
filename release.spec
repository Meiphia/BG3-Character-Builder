# PyInstaller spec for the release builds (macOS .app and Windows folder). Build with:
#   python -m PyInstaller --noconfirm release.spec
# Output: dist/Mei's BG3 Character Builder(.app)
import sys
from PyInstaller.utils.hooks import collect_submodules

sys.path.insert(0, ".")
from bg3asm import __version__

NAME = "Mei's BG3 Character Builder"
mac = sys.platform == "darwin"

a = Analysis(["bga_app.py"], pathex=["."],
             datas=[("bg3asm/web", "bg3asm/web")],
             hiddenimports=collect_submodules("bg3asm") + ["webview"],
             excludes=["tkinter"])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name=NAME, console=False,
          icon=("Mei's BG3 Character Builder.app/Contents/Resources/AppIcon.icns" if mac else "bg3asm/web/icon.ico"))
coll = COLLECT(exe, a.binaries, a.datas, name=NAME)
if mac:
    app = BUNDLE(coll, name=NAME + ".app", icon="Mei's BG3 Character Builder.app/Contents/Resources/AppIcon.icns",
                 bundle_identifier="io.github.meiphia.bg3-character-builder",
                 info_plist={"CFBundleShortVersionString": __version__, "CFBundleVersion": __version__,
                             "NSHighResolutionCapable": True, "LSMinimumSystemVersion": "11.0"})
