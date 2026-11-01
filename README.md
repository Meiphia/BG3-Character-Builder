# Baldur's Gate Assembler

Build Baldur's Gate 3 characters (race, body, head, hair, colours, tattoos, makeup, gear and dyes) from your own
game install and export them for Blender, Unity, Slate or VTubing apps. It works like the game's character creator,
with a live 3D preview. The game's `.pak` files are read in place: nothing is extracted, downloaded or redistributed.

## Features
- The full character creator: races and subraces, bodies, heads, hair, horns, tails, scars, tattoos and makeup,
  piercings, all palettes plus custom colours. Origin characters, NPCs and creatures as presets.
- All armour, clothing, helmets, weapons and shields, with dyes and per-race fitting.
- Import Tav and the party (with gear and dyes) from a save file.
- Mods from BG3's Mods folder (Mod Manager, Vortex, BG3MM) or a `Mods` folder next to the app.
- Game animations and poses, ARKit / Perfect Sync face shapes baked from the face rig.
- Viewer: lighting and sky presets, shadows, pose mode with IK, screenshots and turntable video.
- Exports: **GLB**, a **project folder** (GLB + PNG maps + recipe), **FBX** (through Blender) and **VRM 1.0**
  (humanoid map, expressions, spring bones for hair, tails, wings and cloth). Bones are re-oriented to world
  axes so rotations behave in Blender, Unity and face tracking.

## Run
**macOS:** double-click **Baldur's Gate Assembler.app** (keep it inside this folder for the first launch; after
that you can move it to /Applications or the Dock). Because the app isn't signed, macOS may block it the first
time: right-click it → **Open**, or run `xattr -dr com.apple.quarantine "Baldur's Gate Assembler.app"`.
`run.command` does the same from Terminal.

**Windows:** double-click `run.bat`. `build_windows.bat` makes a standalone `Baldur's Gate Assembler.exe` with PyInstaller.

You need **Python 3.10+** (python.org or Homebrew). The first launch creates a private environment in `.venv`
and installs numpy, Pillow, lz4, zstandard and pywebview (`requirements.txt`). The app finds the game through
Steam; the first launch indexes it (~1–2 minutes) into `_cache/`, after which it starts straight away.
Exports go to `~/Desktop/BG3 Exports/`. Log (macOS app): `~/Library/Logs/Baldurs Gate Assembler.log`.

## Status
| Piece | State |
|---|---|
| `.pak` reader (LSPK v15/16/18, zlib/LZ4/zstd, multi-part), LSF/LSX resources, `.loca` strings | done |
| Game database (visuals, materials, textures, presets, templates, stats), cached | done |
| Character creator menus, gear fitting per race/body, body masking under gear | done |
| Granny GR2 meshes/skeletons/animations incl. BitKnit2 decompression (pure Python) | done |
| Granite virtual textures (GTS/GTP) | done |
| PBR texture baking (skin, gear dyes, hair, eyes, horns, tattoos/makeup) | approximate |
| Save import, mods, VRM, ARKit, animation export | done |

## Command line (development)
```
python3 -m bg3asm app [--no-window]                        # the app (server: http://127.0.0.1:8765/)
python3 -m bg3asm --data PAKs list                         # file tables of every pak -> _dev/listings
python3 -m bg3asm --data PAKs index                        # build/load the cached game database
python3 -m bg3asm --data PAKs demo --race Tiefling --body 1   # example recipe -> _dev/recipe_demo.json
python3 -m bg3asm --data PAKs build --textures             # -> skinned, textured GLB
```
Leave out `--data` to read the Steam install directly.

## Layout
- `bg3asm/pak.py`: pak archives · `lsf.py`, `loca.py`: Larian resource and localisation formats
- `bg3asm/gamedata.py`: indexes the paks into one cached database · `locate.py`: finds the game
- `bg3asm/creator.py`: the character-creator view · `recipe.py`: selection → assembly recipe
- `bg3asm/bitknit.py`, `gr2.py`: Granny GR2 (BitKnit port of pybg3)
- `bg3asm/assemble.py`, `gltf.py`, `vrm.py`, `axes.py`: skeleton merge, GLB/VRM writers, world-aligned bones
- `bg3asm/vt.py`: Granite virtual textures (format per LSLib, MIT) · `bake.py`: shaders → PBR textures
- `bg3asm/anim.py`, `facerig.py`, `savegame.py`: animations, ARKit shapes, save import
- `bg3asm/app.py`, `web/index.html`: local server, exports and the app UI

## Credits
- BitKnit2 decompression ported from [pybg3](https://github.com/eiz/pybg3) (MIT, © 2024 Mackenzie Straight).
- Save-file (LSMF) reader vendored from [bg3-newage](https://github.com/NodeJSmith/bg3-newage) (MIT, Jessica Smith).
- Granny, virtual-texture and resource formats as documented by [LSLib](https://github.com/Norbyte/lslib) (MIT, Norbyte).
- Skin shading informed by [BG3-Substance-Painter-Shaders](https://github.com/V0ln0/BG3-Substance-Painter-Shaders) (CC-BY-SA-4.0).

## License
MIT, see [LICENSE](LICENSE). Game data is not included and stays the property of its owners.

## Disclaimer
Fan-made tool, not affiliated with or endorsed by Larian Studios. Baldur's Gate 3 © Larian Studios. You need your
own copy of the game; respect its terms of service when sharing anything you export.
