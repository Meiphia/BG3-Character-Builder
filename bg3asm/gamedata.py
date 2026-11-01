"""Game database: scans the BG3 paks once and caches everything the assembler needs.

    gd = GameData.open(data_dir)        # builds or loads cache
    gd.visuals[id], gd.materials[id], gd.textures[id], gd.vtextures[id],
    gd.presets[id], gd.charvisuals[id], gd.skeletons[id], gd.templates[id]
    gd.cc[table][uuid]                  # character creator tables (plain dicts)
    gd.text(handle)                     # English string
    gd.locate(path) -> (Pak, entry)     # find a file across paks
"""

import glob
import os
import pickle
import re
import time

from .lsf import load_resource, TranslatedString
from .loca import read_loca
from .pak import Pak

CACHE_VERSION = 12
NULL_GUID = "00000000-0000-0000-0000-000000000000"

_RX_BANK = re.compile(r"^Public/[^/]+/Content/.*_merged\.lsf$", re.I)
_RX_BANK_SKIP = re.compile(r"/HLOD/|DialogTimelines|/Timeline", re.I)
_RX_TEMPLATES = re.compile(r"^Public/[^/]+/RootTemplates/_merged\.lsf$", re.I)
_RX_CC = re.compile(r"^(Public/[^/]+/((CharacterCreation[^/]*|Races)/[^/]+|Lists/ColorDefinitions)\.lsx"
                    r"|Mods/[^/]+/EquipmentSettings/[^/]+\.ls[xf])$", re.I)
_RX_STATS = re.compile(r"^Public/[^/]+/Stats/Generated/Data/(Armor|Weapon)\.txt$", re.I)
_RX_LOCA = re.compile(r"^Localization/English/english\.loca$", re.I)
# mods are laid out more freely than the base game: accept any resource in the usual folders
_RX_MOD_BANK = re.compile(r"^Public/[^/]+/Content/.*\.ls[xf]$", re.I)
_RX_MOD_TEMPLATES = re.compile(r"^Public/[^/]+/RootTemplates/.*\.ls[xf]$", re.I)
_RX_MOD_CC = re.compile(r"^Public/[^/]+/((CharacterCreation[^/]*|Races)/[^/]+|Lists/ColorDefinitions)\.ls[xf]$", re.I)
_RX_MOD_STATS = re.compile(r"^Public/[^/]+/Stats/Generated/Data/[^/]+\.txt$", re.I)
_RX_MOD_LOCA = re.compile(r"^(Mods/[^/]+/)?Localization/English/[^/]+\.(loca|xml)$", re.I)
_RX_MOD_META = re.compile(r"^Mods/[^/]+/meta\.lsx$", re.I)

KEEP_BANKS = ("VisualBank", "MaterialBank", "TextureBank", "VirtualTextureBank",
              "MaterialPresetBank", "CharacterVisualBank", "SkeletonBank")


def _clean(v):
    if isinstance(v, TranslatedString):
        return v.handle
    return v


def node_to_dict(n):
    d = {k: _clean(v) for k, v in n.attrs.items()}
    if n.children:
        ch = {}
        for c in n.children:
            ch.setdefault(c.name, []).append(node_to_dict(c))
        d["_children"] = ch
    return d


def _params(node):
    """Collect material parameter lists from a Material resource / preset node."""
    out = {"scalar": {}, "vec3": {}, "vec4": {}, "tex": {}, "vt": {}}
    for c in node.children:
        name = c.get("ParameterName") or c.get("Parameter")
        if not name:
            continue
        if c.name == "ScalarParameters":
            out["scalar"][name] = (c.get("Value"), c.get("Enabled", True))
        elif c.name == "Vector3Parameters":
            out["vec3"][name] = (c.get("Value"), c.get("Enabled", True))
        elif c.name == "VectorParameters":
            out["vec4"][name] = (c.get("Value"), c.get("Enabled", True))
        elif c.name == "Texture2DParameters":
            out["tex"][name] = (c.get("ID") or c.get("Value"), c.get("Enabled", True))
        elif c.name == "VirtualTextureParameters":
            out["vt"][name] = (c.get("ID") or c.get("Value"), c.get("Enabled", True))
    return out


def _material_overrides(node):
    """MaterialOverrides / Presets block -> {'presets': {group: preset_id}, 'scalar':..., 'vec3':...}"""
    if node is None:
        return None
    out = _params(node)
    out["presets"] = {}
    mp = node.child("MaterialPresets")
    if mp is not None:
        for o in mp.children:
            pid = o.get("MaterialPresetResource")
            if pid:
                out["presets"][o.get("MapKey") or o.get("GroupName")] = pid
    cp = node.child("ColorPreset")
    out["color_preset"] = cp.get("MaterialPresetResource") if cp is not None else None
    out["material"] = node.get("MaterialResource") or None
    return out


def _read_loca_xml(data):
    """Mod localisation: <contentList><content contentuid="h..." version="1">text</content>..."""
    import xml.etree.ElementTree as ET
    out = {}
    root = ET.fromstring(data.decode("utf-8-sig", "replace"))
    for c in root.iter("content"):
        uid = c.get("contentuid")
        if uid:
            out[uid] = c.text or ""
    return out


class GameData(object):
    def __init__(self):
        self.visuals = {}
        self.materials = {}
        self.textures = {}
        self.vtextures = {}
        self.presets = {}
        self.charvisuals = {}
        self.skeletons = {}
        self.templates = {}
        self.cc = {}          # table -> {uuid: dict}
        self.races = {}
        self.strings = {}
        self.body_mask_codes = {}  # NakedBody region name -> vertex color (G) code
        self.stats = {}       # stat entry name -> {"type", "using", "data": {}}
        self.files = {}       # lower path -> pak filename
        self.mods = {}        # mod name -> pak (relative to data_dir)
        self._mod = None
        self.data_dir = None
        self._paks = {}

    # ------------------------------------------------------------ open / cache

    @classmethod
    def open(cls, data_dir, cache_dir=None, rebuild=False, log=print, mod_dirs=()):
        data_dir = os.path.abspath(data_dir)
        cache_dir = cache_dir or os.path.join(os.getcwd(), "_cache")
        os.makedirs(cache_dir, exist_ok=True)
        paks = cls.find_paks(data_dir)
        mods = cls.find_mods(mod_dirs)
        sig = [CACHE_VERSION] + [(os.path.relpath(p, data_dir), os.path.getsize(p), int(os.path.getmtime(p)))
                                 for p in paks + mods]
        import hashlib
        cache = os.path.join(cache_dir, "gamedb_%s.pkl" % hashlib.sha1(data_dir.encode()).hexdigest()[:10])
        if not rebuild and os.path.exists(cache):
            with open(cache, "rb") as f:
                stored_sig, state = pickle.load(f)
            if stored_sig == sig:
                gd = cls()
                gd.__dict__.update(state)
                gd.data_dir = data_dir
                gd._paks = {}
                return gd
            log("Game files changed; rebuilding index")
        gd = cls()
        gd.data_dir = data_dir
        gd.build(paks, log, mods)
        gd._mod = None
        state = {k: v for k, v in gd.__dict__.items() if k not in ("_paks", "data_dir")}
        with open(cache, "wb") as f:
            pickle.dump((sig, state), f, protocol=pickle.HIGHEST_PROTOCOL)
        return gd

    @staticmethod
    def find_mods(mod_dirs):
        out = []
        for d in mod_dirs or ():
            if d and os.path.isdir(d):
                out += sorted(glob.glob(os.path.join(d, "*.pak")))
        return out

    @staticmethod
    def find_paks(data_dir):
        found = glob.glob(os.path.join(data_dir, "*.pak")) + glob.glob(os.path.join(data_dir, "Localization", "*.pak"))
        out = []
        for p in sorted(found):
            # skip continuation parts (Textures_1.pak) of a multi-part archive
            m = re.match(r"^(.*)_(\d+)\.pak$", p)
            if m and os.path.exists(m.group(1) + ".pak"):
                try:
                    if Pak(m.group(1) + ".pak").num_parts > int(m.group(2)):
                        continue
                except Exception:
                    pass
            out.append(p)
        return out

    def pak(self, rel):
        p = self._paks.get(rel)
        if p is None:
            p = Pak(os.path.join(self.data_dir, rel))
            self._paks[rel] = p
        return p

    # ------------------------------------------------------------ build

    def build(self, paks, log=print, mods=()):
        t0 = time.time()
        opened = []
        mod_set = set(mods)
        for path in list(paks) + list(mods):
            try:
                pk = Pak(path)
            except Exception as e:
                log("  skip %s: %s" % (os.path.basename(path), e))
                continue
            opened.append((pk.priority, os.path.basename(path), path, pk))
        # load order: base game data first so story/expansion paks override it; mods last
        order = {"engine": 0, "shared": 1, "gustav": 2, "gustavx": 3}
        opened.sort(key=lambda x: (x[2] in mod_set, x[0], order.get(os.path.splitext(x[1])[0].lower(), 10), x[1]))
        self.mods = {}

        for _prio, _base, path, pk in opened:
            rel = os.path.relpath(path, self.data_dir)
            for e in pk.entries:
                self.files[e.name.lower()] = rel

        for _prio, base, path, pk in opened:
            rel = os.path.relpath(path, self.data_dir)
            n_bank = n_cc = 0
            t = time.time()
            is_mod = path in mod_set
            self._mod = None
            if is_mod:
                self._mod = os.path.splitext(base)[0]
                for e in pk.entries:
                    if _RX_MOD_META.match(e.name):
                        try:
                            res = load_resource(pk.read(e))
                            for root in res.regions.values():
                                for n in root.walk("ModuleInfo"):
                                    if n.get("Name"):
                                        self._mod = str(n.get("Name"))
                        except Exception:
                            pass
                        break
                self.mods[self._mod] = rel
            rx_cc, rx_stats = (_RX_MOD_CC, _RX_MOD_STATS) if is_mod else (_RX_CC, _RX_STATS)
            rx_tpl, rx_bank = (_RX_MOD_TEMPLATES, _RX_MOD_BANK) if is_mod else (_RX_TEMPLATES, _RX_BANK)
            # CC tables first (sorted so Shared < SharedDev)
            for e in sorted(pk.entries, key=lambda e: e.name):
                try:
                    if rx_cc.match(e.name) or _RX_CC.match(e.name):
                        self._ingest_cc(e.name, load_resource(pk.read(e)))
                        n_cc += 1
                    elif rx_stats.match(e.name):
                        self._ingest_stats(pk.read(e).decode("utf-8", "replace"))
                    elif _RX_LOCA.match(e.name) or (is_mod and _RX_MOD_LOCA.match(e.name)):
                        data = pk.read(e)
                        self.strings.update(read_loca(data) if data[:4] == b"LOCA" else _read_loca_xml(data))
                except Exception as ex:
                    log("    ! %s: %s" % (e.name, ex))
            for e in sorted(pk.entries, key=lambda e: e.name):
                if rx_tpl.match(e.name):
                    try:
                        self._ingest_templates(load_resource(pk.read(e)))
                    except Exception as ex:
                        log("    ! %s: %s" % (e.name, ex))
                    n_bank += 1
                elif rx_bank.match(e.name) and not _RX_BANK_SKIP.search(e.name):
                    try:
                        self._ingest_banks(load_resource(pk.read(e)))
                    except Exception as ex:
                        log("    ! %s: %s" % (e.name, ex))
                    n_bank += 1
            if n_bank or n_cc:
                log("  %-22s %4d bank files, %3d creator tables  (%.1fs)" % (rel, n_bank, n_cc, time.time() - t))
            pk.close()
        self._finish()
        log("  index: %d visuals, %d materials, %d textures, %d virtual textures, %d presets, "
            "%d character visuals, %d templates, %d strings  (%.1fs total)" % (
                len(self.visuals), len(self.materials), len(self.textures), len(self.vtextures),
                len(self.presets), len(self.charvisuals), len(self.templates), len(self.strings), time.time() - t0))

    def _ingest_cc(self, name, res):
        for root in res.regions.values():
            for n in root.walk("NakedBodySystem"):
                for o in n.children:
                    if o.get("MapKey") is not None:
                        self.body_mask_codes[str(o.get("MapKey")).lower()] = int(o.get("MapValue"))
        for region, root in res.regions.items():
            table = self.cc.setdefault(region, {})
            for n in root.children:
                d = node_to_dict(n)
                d["_source"] = name
                if self._mod:
                    d["_mod"] = self._mod
                key = d.get("UUID") or d.get("Guid") or d.get("Name") or str(len(table))
                table[key] = d

    def _ingest_stats(self, text):
        cur = None
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("new entry"):
                name = line.split('"')[1]
                cur = self.stats[name] = {"type": None, "using": None, "data": {}}
            elif cur is None:
                continue
            elif line.startswith("type "):
                cur["type"] = line.split('"')[1]
            elif line.startswith("using "):
                cur["using"] = line.split('"')[1]
            elif line.startswith("data "):
                parts = line.split('"')
                if len(parts) >= 4:
                    cur["data"][parts[1]] = parts[3]

    def stat(self, name, key):
        seen = set()
        while name and name in self.stats and name not in seen:
            seen.add(name)
            e = self.stats[name]
            if key in e["data"]:
                return e["data"][key]
            name = e["using"]
        return None

    def _ingest_templates(self, res):
        for root in res.regions.values():
            for o in root.children:
                if o.get("Type") not in ("character", "item"):
                    continue
                d = {k: _clean(v) for k, v in o.attrs.items()
                     if k in ("MapKey", "Name", "Type", "ParentTemplateId", "CharacterVisualResourceID",
                              "VisualTemplate", "Stats", "DisplayName", "Description", "Icon",
                              "EquipmentRace", "Race", "PhysicsTemplate", "ColorPreset")}
                eq = o.child("Equipment")
                if eq is not None:
                    d["Equipment"] = self._parse_equipment(eq)
                if self._mod:
                    d["_mod"] = self._mod
                self.templates[o.get("MapKey")] = d

    @staticmethod
    def _parse_equipment(eq):
        out = {"visuals": {}, "parent_race": {}, "hair": {}, "visual_set": None, "slots": []}
        for c in eq.children:
            if c.name == "Visuals":
                for o in c.children:
                    out["visuals"][o.get("MapKey")] = [mv.get("Object") for mv in o.children if mv.get("Object")]
            elif c.name == "ParentRace":
                for o in c.children:
                    mv = o.child("MapValue")
                    out["parent_race"][o.get("MapKey")] = mv.get("Object") if mv is not None else o.get("MapValue")
            elif c.name == "VisualSet":
                out["visual_set"] = _material_overrides(c.child("MaterialOverrides"))
            elif c.name.endswith("Hair"):
                vals = {}
                for o in c.children:
                    vals[o.get("MapKey")] = [mv.get("Object") for mv in o.children if mv.get("Object")]
                if vals:
                    out["hair"][c.name] = vals
            elif c.name == "Slot":
                out["slots"] = [o.get("Object") for o in c.children]
        return out

    def _ingest_banks(self, res):
        for region in KEEP_BANKS:
            root = res.regions.get(region)
            if root is None:
                continue
            for r in root.children:
                rid = r.get("ID")
                if not rid:
                    continue
                if region == "VisualBank":
                    objs = [(o.get("ObjectID"), o.get("MaterialID"), o.get("LOD", 0)) for o in r.kids("Objects")]
                    self.visuals[rid] = {
                        "name": r.get("Name"), "source": r.get("SourceFile"), "template": r.get("Template"),
                        "skeleton": r.get("SkeletonResource"), "attach_bone": r.get("AttachBone"),
                        "attach_skeleton": r.get("AttachmentSkeletonResource"),
                        "hair_type": r.get("HairType"), "objects": objs,
                        "bounds": (r.get("BoundsMin"), r.get("BoundsMax")),
                        "blueprint": r.get("BlueprintInstanceResourceID"),
                        "slot": r.get("Slot"),
                        "scalp_material": r.get("ScalpMaterialId") or None,
                        "needs_remap": bool(r.get("NeedsSkeletonRemap")),
                        "supports_mask": bool(r.get("SupportsVertexColorMask")),
                        "mask_slots": [c.get("Object") for c in r.kids("VertexColorMaskSlots") if c.get("Object")],
                        "mod": self._mod,
                    }
                elif region == "MaterialBank":
                    m = _params(r)
                    m.update(name=r.get("Name"), source=r.get("SourceFile"),
                             material_type=r.get("MaterialType"), diffusion=r.get("DiffusionProfileUUID"))
                    self.materials[rid] = m
                elif region == "TextureBank":
                    self.textures[rid] = {"name": r.get("Name"), "source": r.get("SourceFile"),
                                          "srgb": r.get("SRGB"), "size": (r.get("Width"), r.get("Height"))}
                elif region == "VirtualTextureBank":
                    self.vtextures[rid] = {"name": r.get("Name"), "gtex": r.get("GTexFileName"),
                                           "layers": r.get("VirtualTextureLayerConfig")}
                elif region == "MaterialPresetBank":
                    p = _material_overrides(r.child("Presets"))
                    if p is not None:
                        p["name"] = r.get("Name")
                        self.presets[rid] = p
                elif region == "CharacterVisualBank":
                    self.charvisuals[rid] = {
                        "name": r.get("Name"), "base_visual": r.get("BaseVisual"),
                        "body_set_visual": r.get("BodySetVisual"),
                        "slots": [(s.get("Slot"), s.get("VisualResource"), s.get("Bone")) for s in r.kids("Slots")],
                        "overrides": _material_overrides(r.child("MaterialOverrides")),
                        "show_equipment": r.get("ShowEquipmentVisuals"),
                    }
                elif region == "SkeletonBank":
                    self.skeletons[rid] = {"name": r.get("Name"), "source": r.get("SourceFile"),
                                           "template": r.get("Template")}

    def _finish(self):
        races = {}
        for key, d in self.cc.get("Races", {}).items():
            races[key] = d
        self.races = races

    # ------------------------------------------------------------ helpers

    def text(self, handle, default=""):
        if handle is None:
            return default
        return self.strings.get(str(handle), default)

    def locate(self, path):
        """Find a game file; tries the path as-is and with the Generated/ prefix GR2/DDS files use."""
        if not path:
            return None
        cands = [path, "Generated/" + path]
        if not os.path.splitext(path)[1]:
            cands += [c + ".DDS" for c in cands]
        for c in cands:
            rel = self.files.get(c.lower())
            if rel:
                return rel, c
        return None

    def read_file(self, path):
        hit = self.locate(path)
        if not hit:
            raise KeyError(path)
        rel, name = hit
        return self.pak(rel).read(name)

    def template_chain(self, tid):
        seen = []
        while tid and tid in self.templates and tid not in seen:
            seen.append(tid)
            tid = self.templates[tid].get("ParentTemplateId")
        return [self.templates[t] for t in seen]

    def template_attr(self, tid, key):
        for t in self.template_chain(tid):
            v = t.get(key)
            if v not in (None, "", NULL_GUID):
                return v
        return None
