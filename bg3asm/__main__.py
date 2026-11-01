"""Baldur's Gate Assembler — command line.

  python3 -m bg3asm list     [--data DIR] [--out DIR]
  python3 -m bg3asm extract  PAK REGEX [--data DIR] [--out DIR] [--max-mb N]
  python3 -m bg3asm bootstrap [--data DIR]       # listings + dev extract for development
"""

import argparse
import glob
import os
import re
import sys
import time

from . import lz4block
from .pak import Pak

DEFAULT_DATA = os.path.expanduser(
    "~/Library/Application Support/Steam/steamapps/common/Baldurs Gate 3/Baldur's Gate 3.app/Contents/Data")
DEV_DIR = "_dev"

# Files the character creator + visual assembly will need.
BOOTSTRAP_EXTRACT = [
    ("Shared.pak", r"CharacterCreation|/Content/.*_merged\.lsf$|/Races/|/Progressions/|BodyType|Equipment"),
    ("Gustav.pak", r"CharacterCreation|/Content/Assets/Characters/.*_merged\.lsf$|/Content/\[PAK\]_.*_merged\.lsf$|/Races/"),
    ("GustavX.pak", r"CharacterCreation|/Content/.*_merged\.lsf$"),
    ("Localization/English.pak", r"\.loca$"),
]


def find_paks(data):
    paks = sorted(glob.glob(os.path.join(data, "*.pak")) + glob.glob(os.path.join(data, "Localization", "*.pak")))
    # skip multi-part continuation files like Foo_1.pak only when a Foo.pak references them as parts;
    # BG3's Textures_1.pak etc. are standalone, so list everything that has an LSPK header.
    return paks


def cmd_list(args):
    out = os.path.join(args.out, "listings")
    os.makedirs(out, exist_ok=True)
    for path in find_paks(args.data):
        rel = os.path.relpath(path, args.data)
        try:
            pak = Pak(path)
        except Exception as e:
            print("  skip %-40s %s" % (rel, e))
            continue
        name = rel.replace(os.sep, "__")[:-4] + ".tsv"
        with open(os.path.join(out, name), "w", encoding="utf-8") as f:
            f.write("# pak=%s version=%d files=%d parts=%d\n" % (rel, pak.version, len(pak.entries), pak.num_parts))
            for e in pak.entries:
                f.write("%s\t%d\t%d\t%d\n" % (e.name, e.real_size, e.compression, e.part))
        print("  listed %-40s v%d  %7d files" % (rel, pak.version, len(pak.entries)))
        pak.close()


def extract(data, pak_rel, pattern, out, max_mb):
    path = os.path.join(data, pak_rel)
    rx = re.compile(pattern, re.I)
    limit = max_mb * 1024 * 1024
    n = total = skipped = 0
    t0 = time.time()
    with Pak(path) as pak:
        for e in pak.entries:
            if not rx.search(e.name):
                continue
            if e.real_size > limit:
                skipped += 1
                continue
            dest = os.path.join(out, "extracted", e.name)
            if os.path.exists(dest) and os.path.getsize(dest) == e.real_size:
                n += 1
                total += e.real_size
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            try:
                blob = pak.read(e)
            except Exception as ex:
                print("    ! %s: %s" % (e.name, ex))
                continue
            with open(dest, "wb") as f:
                f.write(blob)
            n += 1
            total += len(blob)
    print("  %-28s %5d files  %8.1f MB  (%d over %d MB skipped)  %.1fs"
          % (pak_rel, n, total / 1048576.0, skipped, max_mb, time.time() - t0))


def cmd_extract(args):
    extract(args.data, args.pak, args.regex, args.out, args.max_mb)


def cmd_bootstrap(args):
    print("Python %s | LZ4: %s" % (sys.version.split()[0], lz4block.backend()))
    print("Data: %s\n\n[1/2] Listing paks (file tables only)" % args.data)
    cmd_list(args)
    print("\n[2/2] Extracting dev files")
    for pak_rel, rx in BOOTSTRAP_EXTRACT:
        extract(args.data, pak_rel, rx, args.out, args.max_mb)
    print("\nDone. Output in %s/" % os.path.abspath(args.out))


def cmd_index(args):
    from .gamedata import GameData
    print("Building game index from %s" % args.data)
    gd = GameData.open(args.data, cache_dir=os.path.join(args.out, "..", "_cache") if args.out != DEV_DIR else None,
                       rebuild=args.rebuild)
    print("cc tables:", {k: len(v) for k, v in gd.cc.items()})


def cmd_demo(args):
    """Resolve an example character and check every referenced file exists."""
    import json
    from .gamedata import GameData
    from .creator import Creator
    from .recipe import build_recipe
    gd = GameData.open(args.data)
    c = Creator(gd)
    races = {r["name"]: r for r in c.races()}
    r = races[args.race]
    sub = r["subraces"][0]["uuid"] if r["subraces"] else None
    body = r["bodies"][args.body - 1]
    bt, bs = body["body_type"], body["body_shape"]
    m = c.menu(r["uuid"], sub, bt, bs)
    pick = lambda lst, i=0: lst[min(i, len(lst) - 1)]["uuid"] if lst else None
    pre = c.preset_for(r["uuid"], sub, bt, bs)
    er = c.equipment_race(pre["root_template"])
    eq = {}
    for slot, want in (("Chest", args.chest), ("Helmet", args.helmet), ("Main Hand", args.weapon), ("Off Hand", args.shield)):
        items = [i for i in c.equipment(slot, er) if want.lower() in i["name"].lower()]
        if items:
            eq[slot] = items[0]["uuid"]
    sel = {
        "race": r["uuid"], "subrace": sub, "body_type": bt, "body_shape": bs,
        "slots": {s: pick(m["slots"][s], 1) for s in ("Head", "Hair", "Horns", "Tail") if m["slots"][s]},
        "piercings": [x["uuid"] for x in m["slots"]["Piercing"][:2]],
        "colors": {k: pick(v, 3) for k, v in m["colors"].items() if v},
        "materials": {"Tattoo": pick(m["materials"]["Tattoo"], 3), "Makeup": pick(m["materials"]["Makeup"], 2)},
        "intensity": {"Tattoo": 1.0, "Makeup": 0.8, "LipsMakeup": 0.6},
        "equipment": eq,
    }
    rec = build_recipe(gd, sel, c)
    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, "recipe_demo.json")
    with open(path, "w") as f:
        json.dump({"selection": sel, "recipe": rec}, f, indent=1)
    ch = rec["character"]
    print("%s %s, %s  (equipment race: %s)" % (ch["subrace"] or "", ch["race"], body["label"], ch["equipment_race"]))
    for p in rec["parts"]:
        print("  %-11s %-10s %-34s %s %s" % (p["kind"], p["slot"] or "", (p["option"] or "")[:34], p["gr2"]["pak"] or "MISSING", (p["gr2"]["path"] or "<no source>").rsplit("/", 1)[-1]))
    fam = {}
    for mm in rec["materials"].values():
        fam[mm["shader_family"]] = fam.get(mm["shader_family"], 0) + 1
    print("  materials: %d %s" % (len(rec["materials"]), fam))
    tex_ok = sum(1 for t in rec["textures"].values() if t["pak"])
    print("  textures: %d/%d located   virtual textures: %d" % (tex_ok, len(rec["textures"]), len(rec["virtual_textures"])))
    print("  skeleton:", rec["skeleton"] and rec["skeleton"]["path"])
    for w in rec["warnings"]:
        print("  ! " + w)
    print("Recipe written to", path)


def cmd_vtfetch(args):
    """Copy the virtual-texture tilesets (.gts) and pages (.gtp) a recipe needs out of the install."""
    import json
    with open(args.recipe) as f:
        rec = json.load(f)
    rec = rec.get("recipe", rec)
    want = {v["gtex"] for v in rec["virtual_textures"].values()}
    path = os.path.join(args.data, "VirtualTextures.pak")
    out = os.path.join(args.out, "vt")
    os.makedirs(out, exist_ok=True)
    pak = Pak(path)
    gts_needed = set()
    n = total = 0
    for e in pak.entries:
        base = e.name.rsplit("/", 1)[-1]
        if base.endswith(".gtp") and base[:-4].rsplit("_", 1)[-1] in want:
            gts_needed.add(base.rsplit("_", 1)[0] + ".gts")
    for e in pak.entries:
        base = e.name.rsplit("/", 1)[-1]
        hit = base in gts_needed or (base.endswith(".gtp") and base[:-4].rsplit("_", 1)[-1] in want)
        if not hit:
            continue
        dest = os.path.join(out, base)
        if not (os.path.exists(dest) and os.path.getsize(dest) == e.real_size):
            with open(dest, "wb") as f:
                f.write(pak.read(e))
        n += 1
        total += e.real_size
        print("  %-70s %7.1f MB  (part %d)" % (base, e.real_size / 1048576.0, e.part))
    print("%d files, %.0f MB -> %s" % (n, total / 1048576.0, out))


def cmd_build(args):
    """Assemble a recipe into a skinned .glb (geometry + skeleton; textures come later)."""
    import json
    from .gamedata import GameData
    from .assemble import assemble
    from .gltf import write_glb
    with open(args.recipe) as f:
        rec = json.load(f)
    rec = rec.get("recipe", rec)
    gd = GameData.open(args.data)
    t = time.time()
    scene = assemble(rec, lambda pak, path: gd.pak(pak).read(path))
    images = None
    if args.textures:
        from .bake import bake_recipe
        print("Baking textures (max %dpx)..." % args.max_tex)
        images = bake_recipe(gd, rec, vt_dirs=[os.path.join(args.out, "vt")], max_size=args.max_tex, log=print)
    out = args.output or os.path.join(args.out, "character.glb")
    write_glb(scene, out, rec, images)
    verts = sum(len(m.mesh.positions) for m in scene.meshes)
    tris = sum(len(m.mesh.indices) // 3 for m in scene.meshes)
    print("%d meshes, %d verts, %d tris, %d bones -> %s (%.1fs)" % (
        len(scene.meshes), verts, tris, len(scene.bones), out, time.time() - t))
    for w in scene.warnings + rec.get("warnings", []):
        print("  ! " + w)


def cmd_app(args):
    from .app import run
    run(args.data_explicit, port=args.port, open_window=not args.no_window,
        vt_dirs=[os.path.join(DEV_DIR, "vt")])


def cmd_debugtex(args):
    """Dump raw virtual-texture layers + baked maps of the last app build to _cache/debug/."""
    import json
    from .gamedata import GameData
    from .bake import TextureSource, bake_material, _png
    path = args.recipe or os.path.join("_cache", "last_recipe.json")
    with open(path) as f:
        rec = json.load(f)["recipe"]
    gd = GameData.open(args.data)
    out = os.path.join("_cache", "debug")
    os.makedirs(out, exist_ok=True)
    src = TextureSource(gd, rec, [os.path.join(DEV_DIR, "vt")], 1024)
    for key, mat in rec["materials"].items():
        name = mat.get("name") or key
        for slot, vid in mat.get("virtual_textures", {}).items():
            for layer in range(3):
                a = src.vt(vid, layer)
                if a is not None:
                    with open(os.path.join(out, "%s__VT%d.png" % (name, layer)), "wb") as f:
                        f.write(_png(a, "RGBA"))
        try:
            res = bake_material(src, mat)
        except Exception as e:
            print("  ! %s: %s" % (name, e))
            res = None
        for k, v in (res or {}).items():
            if k != "hidden":
                with open(os.path.join(out, "%s__baked_%s.png" % (name, k)), "wb") as f:
                    f.write(_png(v, "RGBA" if v.shape[-1] == 4 else "RGB"))
        print("  %-44s %s" % (name[:44], mat.get("shader", "").rsplit("/", 1)[-1]))
    for w in rec.get("warnings", []):
        print("  ! " + w)
    print("Wrote %s/" % out)


def main(argv=None):
    p = argparse.ArgumentParser(prog="bg3asm")
    p.add_argument("--data", default=DEFAULT_DATA, help="BG3 Data folder")
    p.add_argument("--out", default=DEV_DIR)
    p.add_argument("--max-mb", type=int, default=64, help="skip files bigger than this")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("list").set_defaults(fn=cmd_list)
    sub.add_parser("bootstrap").set_defaults(fn=cmd_bootstrap)
    ix = sub.add_parser("index", help="build/load the cached game database")
    ix.add_argument("--rebuild", action="store_true")
    ix.set_defaults(fn=cmd_index)
    dm = sub.add_parser("demo", help="assemble an example character recipe")
    dm.add_argument("--race", default="Tiefling")
    dm.add_argument("--body", type=int, default=1, help="1-4 as in the game")
    dm.add_argument("--chest", default="Scale Mail")
    dm.add_argument("--helmet", default="Circlet")
    dm.add_argument("--weapon", default="Longsword")
    dm.add_argument("--shield", default="Shield")
    dm.set_defaults(fn=cmd_demo)
    vf = sub.add_parser("vtfetch", help="copy the virtual textures a recipe needs")
    vf.add_argument("recipe", nargs="?", default="_dev/recipe_demo.json")
    vf.set_defaults(fn=cmd_vtfetch)
    bd = sub.add_parser("build", help="assemble a recipe into a .glb")
    bd.add_argument("recipe", nargs="?", default="_dev/recipe_demo.json")
    bd.add_argument("-o", "--output")
    bd.add_argument("--textures", action="store_true", help="bake and embed PBR textures")
    bd.add_argument("--max-tex", type=int, default=2048)
    bd.set_defaults(fn=cmd_build)
    dt = sub.add_parser("debugtex", help="dump texture layers of the last app build")
    dt.add_argument("recipe", nargs="?")
    dt.set_defaults(fn=cmd_debugtex)
    ap = sub.add_parser("app", help="start the character assembler app")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-window", action="store_true")
    ap.set_defaults(fn=cmd_app)
    x = sub.add_parser("extract")
    x.add_argument("pak", help="pak path relative to Data, e.g. Shared.pak")
    x.add_argument("regex")
    x.set_defaults(fn=cmd_extract)
    raw = list(sys.argv[1:] if argv is None else argv)
    args = p.parse_args(argv)
    if not getattr(args, "fn", None):
        p.print_help()
        return 1
    args.data_explicit = args.data if "--data" in raw or any(a.startswith("--data=") for a in raw) else None
    if args.cmd == "app":
        args.fn(args)           # the app finds / asks for the game folder itself
        return 0
    if not os.path.isdir(args.data):
        from . import locate
        found = locate.resolve()[0] or (locate.detect() or [None])[0]
        if found:
            args.data = found
    if not os.path.isdir(args.data):
        print("BG3 Data folder not found: %s\nPass --data <path>" % args.data)
        return 1
    args.fn(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
