"""Mei's BG3 Character Builder — desktop app (local web UI + three.js viewport).

    python3 -m bg3asm app [--data DIR] [--port 8765]

Serves the creator menus as JSON, builds characters on demand (cached), and exports
GLB / project folders / FBX (through Blender, if installed).
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from .gamedata import GameData
from .creator import Creator
from .recipe import build_recipe
from . import assemble as asm
from .gltf import write_glb

WEB_DIR = os.path.join(os.path.dirname(__file__), "web")
BAKE_VERSION = 3       # bump when bake.py changes what it produces (invalidates the on-disk texture cache)
EQUIP_UI_SLOTS = ("Helmet", "Cloak", "Chest", "Gloves", "Boots", "Underwear", "Camp Clothes", "Camp Shoes",
                  "Main Hand", "Off Hand", "Ranged")    # no Amulet: jewelry has no worn model in BG3
BLENDER_CANDIDATES = ["/Applications/Blender.app/Contents/MacOS/Blender",
                      os.path.expanduser("~/Applications/Blender.app/Contents/MacOS/Blender"),
                      "/usr/bin/blender", "/snap/bin/blender", "C:/Program Files/Blender Foundation/Blender/blender.exe"]


def default_mod_dirs(data_dir, work_dir):
    """BG3's own mod folder (Mod Manager / Vortex / BG3MM install there) plus a Mods folder next to the app."""
    home = os.path.expanduser("~")
    cands = [
        # macOS: BG3 keeps its user folder (saves, mods) in Documents
        os.path.join(home, "Documents", "Larian Studios", "Baldur's Gate 3", "Mods"),
        os.path.join(home, "Library", "Application Support", "Larian Studios", "Baldur's Gate 3", "Mods"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Larian Studios", "Baldur's Gate 3", "Mods") if os.environ.get("LOCALAPPDATA") else "",
        os.path.join(work_dir or ".", "Mods"),
        os.path.join(data_dir, "Mods"),
    ]
    return [c for c in cands if c and os.path.isdir(c)]


class Engine(object):
    """Holds the game database and caches across builds."""

    def __init__(self, data_dir, work_dir, vt_dirs=(), mod_dirs=None):
        self.mod_dirs = default_mod_dirs(data_dir, work_dir) if mod_dirs is None else list(mod_dirs)
        self.gd = GameData.open(data_dir, cache_dir=os.path.join(work_dir, "_cache"), mod_dirs=self.mod_dirs)
        self.creator = Creator(self.gd)
        self.work_dir = work_dir
        self.vt_dirs = list(vt_dirs)
        self._anims = {}
        self.lock = threading.Lock()
        self._gr2 = {}           # (pak, path) -> parsed GR2 root
        self._bake = {}          # (material hash, size) -> images
        self._races = None

    # ---------------------------------------------------------------- menus

    def races(self):
        if self._races is None:
            self._races = self.creator.races()
        return self._races

    def menu(self, race, subrace, bt, bs, head=None, child=False, creature=False):
        c = self.creator
        if creature:                            # built from the game's own definition: colours/markings only
            m = c.menu(race, subrace, bt, bs, head)
            m["slots"] = {}
            m["equipment"] = {slot: [] for slot in EQUIP_UI_SLOTS}
            m["equipment_race"] = None
            m["defaults"] = {}
            m["creature"] = True
            return m
        if child:
            m = c.child_menu(race, subrace)
            m["equipment"] = {slot: [] for slot in EQUIP_UI_SLOTS}
            m["equipment_race"] = None
            m["defaults"] = {}
            return m
        m = c.menu(race, subrace, bt, bs, head)
        pre = c.preset_for(race, subrace, bt, bs)
        er = c.equipment_race(pre["root_template"]) if pre else None
        m["equipment_race"] = c.equipment_race_name(er) if er else None
        m["equipment"] = {}
        for slot in EQUIP_UI_SLOTS:
            items = c.equipment(slot, er)
            seen, lst = set(), []
            for it in items:
                k = (it["name"], it["internal"])
                if k in seen:
                    continue
                seen.add(k)
                lst.append({"uuid": it["uuid"], "name": it["name"], "internal": it["internal"], "mod": it.get("mod"),
                            "icon": self.gd.template_attr(it["uuid"], "Icon")})
            m["equipment"][slot] = lst
        m["defaults"] = self.defaults(race, subrace, bt, bs)
        return m

    def defaults(self, race, subrace, bt, bs):
        """Selections the template character starts with (so the UI can show 'default')."""
        pre = self.creator.preset_for(race, subrace, bt, bs)
        cv = self.gd.charvisuals.get(self.gd.template_attr(pre["root_template"], "CharacterVisualResourceID") or "") if pre else None
        return {"slots": {s: v for s, v, _b in (cv or {}).get("slots", [])}} if cv else {}

    # ---------------------------------------------------------------- build

    def _read(self, pak, path):
        return self.gd.pak(pak).read(path)

    def build(self, sel, textures=True, max_tex=512):
        """Recipe -> assembled scene -> baked textures. The last result is reused when nothing that
        affects the model changed (switching animation, expression or light doesn't rebuild)."""
        key = json.dumps([sel, bool(textures), max_tex], sort_keys=True, default=str)
        with self.lock:
            last = getattr(self, "_last_build", None)
            if last and last[0] == key:
                rec, scene, images = last[1]
                self.timing = {"cached": True}
                return rec, scene, images, 0.0
            t0 = time.time()
            rec = build_recipe(self.gd, sel, self.creator)
            t1 = time.time()
            try:  # keep the last build around for debugging (python3 -m bg3asm debugtex)
                with open(os.path.join(self.work_dir, "_cache", "last_recipe.json"), "w") as f:
                    json.dump({"selection": sel, "recipe": rec}, f)
            except Exception:
                pass
            scene = self._assemble(rec)
            t2 = time.time()
            images = self._bake_images(rec, max_tex) if textures else None
            t3 = time.time()
            self.timing = {"recipe": round(t1 - t0, 2), "assemble": round(t2 - t1, 2), "textures": round(t3 - t2, 2)}
            self._last_build = (key, (rec, scene, images))
            return rec, scene, images, t3 - t0

    # ---------------------------------------------------------------- saved characters

    def _char_dir(self):
        d = os.path.join(self.work_dir, "Characters")
        os.makedirs(d, exist_ok=True)
        return d

    @staticmethod
    def _char_file(name):
        safe = "".join(c for c in (name or "").strip() if c.isalnum() or c in " -_.'()").strip() or "Character"
        return safe + ".json"

    def list_characters(self):
        out = []
        for f in sorted(os.listdir(self._char_dir())):
            if f.endswith(".json"):
                p = os.path.join(self._char_dir(), f)
                out.append({"name": f[:-5], "mtime": os.path.getmtime(p)})
        out.sort(key=lambda c: -c["mtime"])
        return out

    def save_character(self, name, selection):
        path = os.path.join(self._char_dir(), self._char_file(name))
        with open(path, "w") as f:
            json.dump({"format": "bg3asm-character/1", "name": name, "saved": time.time(), "selection": selection},
                      f, indent=1)
        return {"ok": True, "name": os.path.basename(path)[:-5], "path": path}

    def load_character(self, name):
        with open(os.path.join(self._char_dir(), self._char_file(name))) as f:
            return json.load(f)

    def delete_character(self, name):
        path = os.path.join(self._char_dir(), self._char_file(name))
        if os.path.exists(path):
            os.remove(path)
        return {"ok": True}

    def animations(self, skeleton):
        from . import anim
        key = ("list", skeleton)
        if key not in self._anims:
            self._anims[key] = anim.list_animations(self.gd, skeleton)
        return self._anims[key]

    def _head_name(self, rec):
        return next((p.get("name") for p in rec.get("parts", []) if p.get("slot") == "Head"), None)

    def face_poses(self, head, skeleton=None):
        from . import anim
        rig = anim.face_rig_for(self.gd, head, skeleton)
        return {"rig": rig, "poses": anim.list_face_poses(self.gd, rig) if rig else []}

    def face_pose(self, rec, path):
        if not path:
            return None
        from . import anim
        key = ("face", path)
        try:
            if key not in self._anims:
                self._anims[key] = anim.load_face_pose(self.gd, path)
            return self._anims[key]
        except Exception as e:
            rec["warnings"].append("face pose: %s" % e)
            return None

    def idle(self, rec, path=None):
        """An animation for the character's rig: the given game path, else a calm looping idle."""
        try:
            from . import anim
            path = path or anim.idle_for_skeleton(self.gd, (rec.get("skeleton") or {}).get("name"))
            if not path:
                return None
            if path not in self._anims:
                a = anim.load_animation(self.gd.read_file(path))
                a["name"] = path.rsplit("/", 1)[-1][:-4]
                self._anims[path] = a
            return self._anims[path]
        except Exception as e:
            rec["warnings"].append("animation: %s" % e)
            return None

    def export_party(self, save_path, fmt, out_dir, max_tex=2048, arkit=True):
        """Export every party member in a save (Tav + companions in their current gear)."""
        from . import savegame
        cands = [c for c in savegame.read_save(self.gd, save_path) if c.get("party")]
        if not cands:
            raise RuntimeError("no party members found in this save")
        folder = os.path.join(out_dir, os.path.basename(os.path.dirname(save_path)).split("__")[-1] or "Party")
        done, warns = [], []
        for c in cands:
            name = c.get("name") or c["label"].split(" · ")[0]
            try:
                path, w = self.export(c["selection"], fmt, name, folder, max_tex, arkit)
                done.append(path)
                warns += ["%s: %s" % (name, x) for x in w[:3]]
            except Exception as e:
                warns.append("%s: %s" % (name, e))
        return folder, done, warns

    def icon(self, name):
        if getattr(self, "_icons", None) is None:
            from .icons import Icons
            self._icons = Icons(self.gd, os.path.join(self.work_dir, "_cache"))
        hit = self._icons.cached(name)
        if hit is not None:
            return hit or None
        with self.lock:                 # pak file handles are shared with builds
            return self._icons.png(name)

    # ---------------------------------------------------------------- game skies (image-based light)

    SKIES = ("ATM_HDRiSkydome_Noon_Vue_A", "ATM_HDRiSkydome_Astral_A", "ATM_HDRiSkydome_Ethereal_VUE_A",
             "ATM_HDRi_EyesCatchlight")

    def sky(self, name):
        """The game's own HDRi sky domes as an equirectangular PNG (for lighting the viewer)."""
        if name not in self.SKIES:
            return None
        cache = os.path.join(self.work_dir, "_cache", "sky_%s.png" % name)
        if os.path.exists(cache):
            with open(cache, "rb") as f:
                return f.read()
        tex = next((t for t in self.gd.textures.values() if t.get("name") == name), None)
        loc = self.gd.locate(tex["source"]) if tex and tex.get("source") else None
        if not loc:
            return None
        from PIL import Image
        import io as _io
        with self.lock:
            raw = self.gd.pak(loc[0]).read(loc[1])
        im = Image.open(_io.BytesIO(raw))
        im.load()
        im = im.convert("RGB")
        if im.width > 2048:
            im = im.resize((2048, max(1, im.height * 2048 // im.width)), Image.LANCZOS)
        out = _io.BytesIO()
        im.save(out, "PNG")
        data = out.getvalue()
        with open(cache, "wb") as f:
            f.write(data)
        return data

    # ---------------------------------------------------------------- creator backdrop

    def backdrop_file(self):
        from . import backdrop
        return os.path.join(self.work_dir, "_cache", "backdrop_v%d.glb" % backdrop.VERSION)

    def backdrop_status(self):
        st = getattr(self, "_backdrop", None) or {}
        if os.path.exists(self.backdrop_file()):
            return {"state": "ready"}
        return {"state": st.get("state", "missing"), "message": st.get("message", "")}

    def backdrop_build(self):
        if (getattr(self, "_backdrop", None) or {}).get("state") == "building" or os.path.exists(self.backdrop_file()):
            return self.backdrop_status()
        self._backdrop = {"state": "building", "message": "Reading the character-creator level…"}

        def work():
            from . import backdrop
            try:
                def log(msg):
                    print(msg)
                    self._backdrop["message"] = msg.replace("backdrop: ", "")
                with self.lock:
                    backdrop.build(self, self.backdrop_file(), max_tex=512, log=log)
                self._backdrop = {"state": "ready"}
            except Exception as e:
                traceback.print_exc()
                self._backdrop = {"state": "error", "message": str(e)}
        threading.Thread(target=work, daemon=True).start()
        return self.backdrop_status()

    def _assemble(self, rec):
        cache = self._gr2

        def load_cached(pak, path):
            key = (pak, path)
            if key not in cache:
                if len(cache) > 400:
                    cache.clear()
                cache[key] = asm.read_gr2(self._read(pak, path))
            return cache[key]

        return asm.assemble(rec, self._read, loader=load_cached)

    def _bake_images(self, rec, max_tex):
        """Bake every material's textures. Results are cached in memory and on disk by a hash of everything
        that goes into them, and the misses are baked in parallel (numpy, zlib and the DDS decoder release
        the GIL; pak reads are serialised inside TextureSource)."""
        try:
            from .bake import TextureSource, bake_material, _png
        except ImportError as e:
            rec["warnings"].insert(0, "Textures disabled: %s — start the app with run.command so numpy/Pillow get installed" % e)
            return None
        import pickle
        from concurrent.futures import ThreadPoolExecutor
        src = TextureSource(self.gd, rec, self.vt_dirs, max_tex)
        disk = os.path.join(self.work_dir, "_cache", "bake")
        os.makedirs(disk, exist_ok=True)
        level = 1 if max_tex <= 1024 else 6          # the preview favours speed, exports file size
        hashes, todo = {}, []
        for key, mat in rec["materials"].items():
            h = hashlib.sha1(json.dumps([mat, max_tex, BAKE_VERSION], sort_keys=True, default=str).encode()).hexdigest()
            hashes[key] = h
            if h in self._bake:
                continue
            f = os.path.join(disk, h + ".pkl")
            if os.path.exists(f):
                try:
                    with open(f, "rb") as fh:
                        self._bake[h] = pickle.load(fh)
                    try:
                        os.utime(f)                 # mark as recently used for the cache pruning
                    except OSError:
                        pass
                    continue
                except Exception:
                    pass
            if h not in [t[1] for t in todo]:
                todo.append((key, h, mat))

        def work(item):
            key, h, mat = item
            try:
                res = bake_material(src, mat)
            except Exception as e:
                rec["warnings"].append("bake %s: %s" % (mat.get("name"), e))
                res = None
            if res is None:
                return h, {}
            if res.get("hidden"):
                return h, {"hidden": True}
            return h, {k: _png(v, "RGBA" if v.shape[-1] == 4 else "RGB", level) for k, v in res.items()}
        if todo:
            with ThreadPoolExecutor(max_workers=min(6, max(1, (os.cpu_count() or 4) - 1))) as ex:
                for h, hit in ex.map(work, todo):
                    if len(self._bake) > 300:
                        self._bake.clear()
                    self._bake[h] = hit
                    try:
                        with open(os.path.join(disk, h + ".pkl"), "wb") as fh:
                            pickle.dump(hit, fh, protocol=pickle.HIGHEST_PROTOCOL)
                    except Exception:
                        pass
            self._prune_bake_cache(disk)
        out = {}
        for key, h in hashes.items():
            hit = self._bake.get(h)
            if hit:
                out[key] = hit
        return out

    @staticmethod
    def _prune_bake_cache(disk, keep=1500, max_bytes=2 * 1024 ** 3):
        """Least-recently-used first, until under both the file count and ~2 GB (Ultra 4K bakes are big)."""
        try:
            files = []
            for f in os.listdir(disk):
                if f.endswith(".pkl"):
                    st = os.stat(os.path.join(disk, f))
                    files.append((st.st_mtime, st.st_size, f))
            files.sort()
            total = sum(sz for _t, sz, _f in files)
            n = len(files)
            for _t, sz, f in files:
                if n <= keep and total <= max_bytes:
                    break
                os.remove(os.path.join(disk, f))
                n -= 1
                total -= sz
        except Exception:
            pass

    # ---------------------------------------------------------------- export

    def export(self, sel, fmt, name, out_dir, max_tex=2048, arkit=True, motion=None):
        """motion: {"anim": game path or None, "include": bool, "pose_time": seconds or None}"""
        rec, scene, images, _dt = self.build(sel, textures=True, max_tex=max_tex)
        motion = motion or {}
        animation = pose = None
        clips = []
        if fmt != "vrm" and motion.get("include"):
            # several game animations -> one clip each (Blender actions / Slate takes)
            for path in (motion.get("clips") or [motion.get("anim")]):
                a = self.idle(rec, path or None)          # None: the rig's default idle
                if a:
                    clips.append(a)
                elif path:
                    rec["warnings"].append("animation %s doesn't fit this skeleton" % path.rsplit("/", 1)[-1])
        if fmt != "vrm" and motion.get("pose_rot"):
            # the exact pose in the viewer (posed by hand, or the current animation frame); glTF -> BG3 space
            pose = {"rot": {b: [-q[0], -q[1], q[2], q[3]] for b, q in motion["pose_rot"].items()}}
        elif fmt != "vrm" and motion.get("pose_time") is not None:
            a = self.idle(rec, motion.get("anim"))
            if a:
                from .gltf import sample_pose
                pose = sample_pose(a, float(motion["pose_time"]))
        face = self.face_pose(rec, motion.get("face")) if fmt != "vrm" else None
        if face:
            from .gltf import apply_face
            clips = [apply_face(scene, face, c, None)[0] for c in clips]
            _a, pose = apply_face(scene, face, None, pose) if (pose is not None or not clips) else (None, pose)
        animation = clips or None
        heartbeat = motion.get("heartbeat", True)
        morphs = None
        if arkit:
            from . import facerig
            morphs = facerig.bake(scene)
            if not morphs:
                rec["warnings"].append("ARKit shapes skipped: this head has no standard face rig")
        align = motion.get("world_axes", True)

        def finish(glb_path):
            # BG3 bones point their local X along the bone; re-frame them so X nods, Y turns, Z tilts
            if align:
                from .axes import world_align_file
                try:
                    world_align_file(glb_path)
                except Exception as e:
                    rec["warnings"].append("bone axes left as in the game: %s" % e)
        safe = "".join(ch if ch.isalnum() or ch in "-_ " else "_" for ch in name).strip() or "character"
        os.makedirs(out_dir, exist_ok=True)
        if fmt == "glb":
            path = os.path.join(out_dir, safe + ".glb")
            write_glb(scene, path, rec, images, name=safe, morphs=morphs, animation=animation, pose=pose,
                      heartbeat=heartbeat)
            finish(path)
            return path, rec["warnings"]
        if fmt == "vrm":
            from .vrm import glb_to_vrm
            from . import facerig
            if morphs is None:
                morphs = facerig.bake(scene)          # VRM expressions need the face shapes
            tmp = os.path.join(self.work_dir, "_cache", "vrm_src.glb")
            write_glb(scene, tmp, rec, images, name=safe, morphs=morphs)
            finish(tmp)
            path = os.path.join(out_dir, safe + ".vrm")
            with open(tmp, "rb") as f:
                data = glb_to_vrm(f.read(), name=name)
            with open(path, "wb") as f:
                f.write(data)
            return path, rec["warnings"]
        folder = os.path.join(out_dir, safe)
        os.makedirs(os.path.join(folder, "textures"), exist_ok=True)
        glb = os.path.join(folder, safe + ".glb")
        write_glb(scene, glb, rec, images, name=safe, morphs=morphs, animation=animation, pose=pose,
                  heartbeat=heartbeat)
        finish(glb)
        for key, imgs in (images or {}).items():
            mname = rec["materials"].get(key, {}).get("name") or key
            for slot, png in imgs.items():
                if slot == "hidden":
                    continue
                with open(os.path.join(folder, "textures", "%s_%s.png" % (mname, slot)), "wb") as f:
                    f.write(png)
        with open(os.path.join(folder, "blender_finish_materials.py"), "w") as f:
            f.write(BLENDER_FINISH)
        with open(os.path.join(folder, "recipe.json"), "w") as f:
            json.dump({"selection": sel, "recipe": rec}, f, indent=1)
        if fmt == "fbx":
            blender = find_blender()
            if not blender:
                raise RuntimeError("Blender not found — FBX export needs Blender installed")
            fbx = os.path.join(folder, safe + ".fbx")
            script = os.path.join(folder, "_to_fbx.py")
            with open(script, "w") as f:
                f.write("BLENDER_FINISH = %r\n" % BLENDER_FINISH + BLENDER_FBX_SCRIPT)
            r = subprocess.run([blender, "-b", "--factory-startup", "--python", script, "--", glb, fbx],
                               capture_output=True, text=True, timeout=600)
            os.remove(script)
            if not os.path.exists(fbx):
                raise RuntimeError("Blender FBX export failed:\n" + (r.stdout[-2000:] + r.stderr[-2000:]))
            return fbx, rec["warnings"]
        return folder, rec["warnings"]


BLENDER_FINISH = r'''
# Mei's BG3 Character Builder: finish materials after importing the .glb (Scripting tab > open > Run Script).
# Skin gets subsurface scattering, hair dithered transparency, eyes a wet clear coat.
import bpy
for mat in bpy.data.materials:
    fam = mat.get("bga_family") or ""
    nt = mat.node_tree
    bsdf = next((n for n in (nt.nodes if nt else []) if n.type == "BSDF_PRINCIPLED"), None)
    if not bsdf:
        continue
    def put(name, value):
        if name in bsdf.inputs:
            bsdf.inputs[name].default_value = value
    if fam in ("skin_head", "skin_body"):
        pass                                    # left as imported (reads closest to the game)
    elif fam in ("hair", "scalp"):
        try:
            mat.surface_render_method = "DITHERED"
        except Exception:
            mat.blend_method = "HASHED"
        put("Sheen Weight", 0.4)
    elif fam == "eye":
        put("Coat Weight", 1.0)
        put("Coat Roughness", 0.03)
'''

BLENDER_FBX_SCRIPT = r'''
import bpy, sys
argv = sys.argv[sys.argv.index("--") + 1:]
glb, fbx = argv[0], argv[1]
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=glb, bone_heuristic="BLENDER")   # keep the file's (world-aligned) bone frames
exec(compile(BLENDER_FINISH, "finish", "exec"))
bpy.ops.export_scene.fbx(filepath=fbx, path_mode="COPY", embed_textures=True, add_leaf_bones=False,
                         armature_nodetype="NULL", bake_anim=True, bake_anim_use_all_actions=True,
                         bake_anim_force_startend_keying=True, use_mesh_modifiers=False)
'''


def find_blender():
    for c in BLENDER_CANDIDATES:
        if os.path.exists(c):
            return c
    if os.name == "nt":       # C:\Program Files\Blender Foundation\Blender 4.2\blender.exe
        import glob
        for root in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
            hits = sorted(glob.glob(os.path.join(root or "", "Blender Foundation", "Blender*", "blender.exe")))
            if hits:
                return hits[-1]
        steam = glob.glob("C:/Program Files (x86)/Steam/steamapps/common/Blender/blender.exe")
        if steam:
            return steam[0]
    return shutil.which("blender")


# ==================================================================== HTTP

class Holder(object):
    """The engine is loaded in the background so the window can open (and ask for the game folder) first."""

    def __init__(self, work_dir, vt_dirs):
        self.engine = None
        self.data_dir = None
        self.work_dir, self.vt_dirs = work_dir, vt_dirs
        self.state, self.message = "setup", ""
        self._lock = threading.Lock()

    def load(self, data_dir):
        with self._lock:
            if self.state == "loading":
                return
            self.state, self.message, self.data_dir = "loading", "Reading game data…", data_dir

        def work():
            try:
                print("Loading game data from %s ..." % data_dir)
                eng = Engine(data_dir, self.work_dir, self.vt_dirs)
                self.engine, self.state, self.message = eng, "ready", ""
            except Exception as e:
                traceback.print_exc()
                self.state, self.message = "setup", "Could not load the game data: %s" % e
        threading.Thread(target=work, daemon=True).start()

    def status(self):
        from . import locate
        return {"state": self.state, "message": self.message, "data_dir": self.data_dir,
                "detected": locate.detect(), "saved": locate.load_settings().get("data_dir"),
                "platform": sys.platform}


def make_handler(holder, export_dir):
    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _send(self, code, body, ctype="application/json", headers=None):
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode()
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if "Cache-Control" not in (headers or {}):
                self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json_body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def _trusted(self, post=False):
            """Only the app's own page may talk to the server: the Host must be this loopback address
            (stops DNS rebinding), and changes need a custom header, which other websites can't send
            to us without a CORS preflight this server never answers (stops cross-site requests)."""
            host = (self.headers.get("Host") or "").split(":")[0].lower()
            if host not in ("127.0.0.1", "localhost"):
                return False
            return not post or self.headers.get("X-BGA") == "1"

        def do_GET(self):
            if not self._trusted():
                return self._send(403, {"error": "forbidden"})
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            engine = holder.engine
            try:
                if u.path in ("/", "/index.html"):
                    with open(os.path.join(WEB_DIR, "index.html"), "rb") as f:
                        return self._send(200, f.read(), "text/html; charset=utf-8")
                if u.path == "/api/setup":
                    return self._send(200, holder.status())
                if engine is None and u.path.startswith("/api/"):
                    return self._send(503, {"error": "game data not loaded yet", "setup": holder.status()})
                if u.path == "/api/races":
                    return self._send(200, {"races": engine.races(), "blender": bool(find_blender()),
                                            "export_dir": export_dir, "mods": sorted(engine.gd.mods or {}),
                                            "mod_dirs": engine.mod_dirs})
                if u.path == "/api/characters":
                    return self._send(200, {"characters": engine.list_characters()})
                if u.path == "/api/characters/load":
                    return self._send(200, engine.load_character(q["name"]))
                if u.path == "/api/saves":
                    from . import savegame
                    saves = savegame.list_saves()
                    return self._send(200, {"saves": [dict(s_, path=s_["path"]) for s_ in saves],
                                            "dirs": savegame.save_dirs()})
                if u.path == "/api/face_poses":
                    return self._send(200, engine.face_poses(q.get("head", ""), q.get("skeleton")))
                if u.path == "/api/animations":
                    return self._send(200, {"animations": engine.animations(q.get("skeleton", ""))})
                if u.path == "/api/icon_debug":
                    engine.icon("")          # make sure the icon store exists
                    with engine.lock:
                        return self._send(200, engine._icons.debug(q.get("name", "")))
                if u.path == "/api/icon":
                    data = engine.icon(q.get("name"))
                    if not data:
                        return self._send(404, {"error": "no icon"}, headers={"Cache-Control": "max-age=3600"})
                    return self._send(200, data, "image/png", {"Cache-Control": "max-age=604800"})
                if u.path == "/api/sky":
                    data = engine.sky(q.get("name"))
                    if not data:
                        return self._send(404, {"error": "sky not found"})
                    return self._send(200, data, "image/png", {"Cache-Control": "max-age=604800"})
                if u.path == "/api/backdrop/status":
                    return self._send(200, engine.backdrop_status())
                if u.path == "/api/backdrop.glb":
                    with open(engine.backdrop_file(), "rb") as f:
                        return self._send(200, f.read(), "model/gltf-binary")
                if u.path == "/api/origins":
                    return self._send(200, {"origins": engine.creator.origin_characters()})
                if u.path == "/api/menu":
                    m = engine.menu(q["race"], q.get("subrace") or None, int(q.get("bt", 1)), int(q.get("bs", 0)),
                                    q.get("head") or None, child=q.get("child") == "1",
                                    creature=q.get("creature") == "1")
                    return self._send(200, m)
                return self._send(404, {"error": "not found"})
            except Exception as e:
                traceback.print_exc()
                return self._send(500, {"error": str(e)})

        def do_POST(self):
            if not self._trusted(post=True):
                return self._send(403, {"error": "forbidden"})
            u = urlparse(self.path)
            engine = holder.engine
            try:
                if u.path == "/api/save_render":          # PNG portrait / turntable video from the viewport
                    q = {k: v[0] for k, v in parse_qs(u.query).items()}
                    n = int(self.headers.get("Content-Length") or 0)
                    data = self.rfile.read(n)
                    ext = q.get("ext", "png") if q.get("ext") in ("png", "webm", "mp4") else "png"
                    safe = "".join(ch if ch.isalnum() or ch in "-_ " else "_" for ch in q.get("name", "render")).strip() or "render"
                    os.makedirs(export_dir, exist_ok=True)
                    path = os.path.join(export_dir, "%s_%s.%s" % (safe, time.strftime("%Y%m%d-%H%M%S"), ext))
                    with open(path, "wb") as f:
                        f.write(data)
                    return self._send(200, {"path": path})
                body = self._json_body()
                if u.path == "/api/setup/pick":
                    from . import locate
                    picked = locate.pick_folder(body.get("start") or holder.data_dir)
                    return self._send(200, {"path": picked, "data_dir": locate.normalize(picked) if picked else None})
                if u.path == "/api/setup":
                    from . import locate
                    d = locate.normalize(body.get("path"))
                    if not d:
                        return self._send(400, {"error": "That folder doesn't look like a Baldur's Gate 3 install "
                                                         "(no Shared.pak / Gustav.pak inside its Data folder)."})
                    locate.save_settings(data_dir=d)
                    if holder.engine is None or os.path.normpath(d) != os.path.normpath(holder.data_dir or ""):
                        holder.engine = None
                        holder.load(d)
                    return self._send(200, holder.status())
                if engine is None and u.path.startswith("/api/"):
                    return self._send(503, {"error": "game data not loaded yet", "setup": holder.status()})
                if u.path == "/api/build":
                    rec, scene, images, dt = engine.build(body["selection"], body.get("textures", True),
                                                          int(body.get("max_tex", 512)))
                    tmp = os.path.join(engine.work_dir, "_cache", "preview.glb")
                    morphs = None
                    if body.get("arkit", True):          # live face-shape preview in the viewport
                        from . import facerig
                        try:
                            morphs = facerig.bake(scene)
                        except Exception:
                            traceback.print_exc()
                    t_write = time.time()
                    anim_ = engine.idle(rec, body.get("anim")) if body.get("animate", True) else None
                    pose_ = None
                    face = engine.face_pose(rec, body.get("face"))
                    if face:
                        from .gltf import apply_face
                        anim_, pose_ = apply_face(scene, face, anim_, None)
                    write_glb(scene, tmp, rec, images, animation=anim_, pose=pose_,
                              morphs=morphs)
                    with open(tmp, "rb") as f:
                        data = f.read()
                    timing = dict(getattr(engine, "timing", {}) or {})
                    timing["write"] = round(time.time() - t_write, 2)
                    info = {"seconds": round(dt, 2), "timing": timing, "warnings": rec["warnings"][:20],
                            "skeleton": (rec.get("skeleton") or {}).get("name"), "head": engine._head_name(rec),
                            "parts": [{"slot": p["slot"], "option": p["option"], "kind": p["kind"]} for p in rec["parts"]]}
                    return self._send(200, data, "model/gltf-binary", {"X-Build-Info": json.dumps(info)})
                if u.path == "/api/backdrop/build":
                    return self._send(200, engine.backdrop_build())
                if u.path == "/api/characters/save":
                    return self._send(200, engine.save_character(body["name"], body["selection"]))
                if u.path == "/api/characters/delete":
                    return self._send(200, engine.delete_character(body["name"]))
                if u.path == "/api/import_save":
                    from . import savegame
                    cands = savegame.read_save(engine.gd, body["path"])
                    return self._send(200, {"candidates": cands})
                if u.path == "/api/export_party":
                    folder, done, warns = engine.export_party(body["path"], body.get("format", "glb"),
                                                              body.get("dir") or export_dir,
                                                              int(body.get("max_tex", 2048)), bool(body.get("arkit", True)))
                    return self._send(200, {"path": folder, "files": done, "warnings": warns[:30]})
                if u.path == "/api/export":
                    path, warns = engine.export(body["selection"], body.get("format", "glb"),
                                                body.get("name", "character"), body.get("dir") or export_dir,
                                                int(body.get("max_tex", 2048)), bool(body.get("arkit", True)),
                                                body.get("motion"))
                    return self._send(200, {"path": path, "warnings": warns[:20]})
                return self._send(404, {"error": "not found"})
            except Exception as e:
                traceback.print_exc()
                return self._send(500, {"error": str(e)})

    return H


def _brand_app():
    """Show our name and icon in the Dock / menu bar instead of Python's (macOS, best effort)."""
    if sys.platform != "darwin":
        return
    try:
        from Foundation import NSBundle
        from AppKit import NSApplication, NSImage
        info = NSBundle.mainBundle().localizedInfoDictionary() or NSBundle.mainBundle().infoDictionary()
        if info is not None:
            info["CFBundleName"] = "Mei's BG3 Character Builder"
        icon = os.environ.get("BGA_APP_ICON") or os.path.join(WEB_DIR, "icon.png")
        if os.path.exists(icon):
            NSApplication.sharedApplication().setApplicationIconImage_(NSImage.alloc().initByReferencingFile_(icon))
    except Exception:
        pass


def default_export_dir(work_dir):
    desk = os.path.join(os.path.expanduser("~"), "Desktop")
    return os.path.join(desk, "BG3 Exports") if os.path.isdir(desk) else os.path.join(work_dir, "exports")


def run(data_dir=None, port=8765, work_dir=None, open_window=True, vt_dirs=()):
    """data_dir: explicit --data, else the saved choice. With neither, the window opens on a setup screen
    that suggests any installs it detected and lets the user browse for the game folder."""
    from . import locate
    work_dir = work_dir or os.getcwd()
    export_dir = default_export_dir(work_dir)
    holder = Holder(work_dir, vt_dirs)
    chosen, source = locate.resolve(data_dir)
    if source == "argument-invalid":
        print("Not a BG3 Data folder: %s — choose one in the app." % data_dir)
    if not chosen and source == "unset":
        found = locate.detect()
        if found:                       # first launch with a standard install: just use it (changeable in the app)
            chosen = found[0]
            locate.save_settings(data_dir=chosen)
            print("Found the game at %s" % chosen)
    if chosen:
        holder.load(chosen)
    else:
        print("Game folder not set yet — the app will ask for it.")
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(holder, export_dir))
    url = "http://127.0.0.1:%d/" % port
    print("Mei's BG3 Character Builder running at %s  (Ctrl+C to quit)" % url)
    if open_window:
        try:
            import webview  # pywebview: native window if installed
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            _brand_app()
            webview.create_window("Mei's BG3 Character Builder", url, width=1440, height=900)
            webview.start()
            return
        except ImportError:
            webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
