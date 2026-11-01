"""Item icons from the game files, served as cached PNG thumbnails.

Each item template names an icon (its `Icon` attribute, a MapKey such as "Item_ARM_Scalemail_1").
BG3 ships those in two forms:
  * individual images, e.g. Public/Game/GUI/Assets/Tooltips/ItemIcons/<Icon>.DDS (the big tooltip
    pictures) or the controller UI's items_png/<Icon>.DDS — preferred, higher resolution;
  * icon atlases: an .lsx/.lsf "TextureAtlasInfo" listing IconUV {MapKey, U1, V1, U2, V2} per icon,
    plus TextureAtlasPath {Path} pointing at the atlas .dds — used as a fallback.
"""

import io
import os
import re
import threading

from .lsf import load_resource

THUMB = 128
_SINGLE = re.compile(r"/(tooltips/itemicons|itemicons|items_png|icons/items?)/([^/]+)\.dds$", re.I)
_ATLAS = re.compile(r"(^|/)gui/[^/]*\.ls[xf]$", re.I)          # icon sheets (the game's and mods', e.g. FCAtlas.lsx)
_ATLAS_SKIP = re.compile(r"metadata|layout|font|controller|_lang|keybind", re.I)


class Icons(object):
    def __init__(self, gd, cache_dir):
        self.gd = gd
        self.dir = os.path.join(cache_dir, "icons")
        self.lock = threading.Lock()
        self._single = None      # icon name (lower) -> game path, best source first
        self._atlas = None       # icon name (lower) -> (atlas texture path, u1, v1, u2, v2)
        self._atlas_img = {}
        try:                                  # retry earlier misses once per app start
            for f in os.listdir(self.dir):
                fp = os.path.join(self.dir, f)
                if f.startswith(("cc_", "tex_", "mesh_", "portrait_")) and os.path.getsize(fp) == 0:
                    os.remove(fp)
        except OSError:
            pass

    # ---------------------------------------------------------------- indexes

    def _index(self):
        if self._single is not None:
            return
        single = {}
        # full-res tooltip pictures first, then the controller-UI icons; "assetslowres" copies last
        rank = lambda p: (10 if "assetslowres" in p else 0) + (0 if "tooltips" in p else (1 if "items_png" in p else 2))
        for low in self.gd.files:
            m = _SINGLE.search(low)
            if m:
                k = m.group(2)
                if k not in single or rank(low) < rank(single[k]):
                    single[k] = low
        self._single = single
        self._atlas = {}

    def _index_atlases(self):
        if self._atlas:
            return
        atlas = {}
        for low in list(self.gd.files):
            if not _ATLAS.search(low) or _ATLAS_SKIP.search(low.rsplit("/", 1)[-1]):
                continue
            loc = self.gd.locate(low)
            if not loc:
                continue
            try:
                raw = self.gd.pak(loc[0]).read(loc[1])
                res = load_resource(raw)
            except Exception:
                continue
            path, uvs = None, []
            for reg in res.regions.values():          # path and UVs live in separate regions
                for n in reg.walk():
                    if n.get("Path") and str(n.get("Path")).lower().endswith(".dds"):
                        path = str(n.get("Path"))
                    if n.get("MapKey") is not None and n.get("U1") is not None:
                        uvs.append((str(n.get("MapKey")).lower(), float(n.get("U1")), float(n.get("V1")),
                                    float(n.get("U2")), float(n.get("V2"))))
            if path and uvs:
                parts = low.split("/")
                module = parts[1] if len(parts) > 2 and parts[0] in ("public", "mods") else None
                for k, u1, v1, u2, v2 in uvs:
                    atlas.setdefault(k, (path, u1, v1, u2, v2, module))
        self._atlas = atlas or {"": None}

    # ---------------------------------------------------------------- lookup

    # ---------------------------------------------------------------- character-creator sheets

    def _cc_index(self):
        """(prefix, slot, id) -> (atlas game path, u1, v1, u2, v2) from every Generated_CCIcons.lsx.
        ids are VisualResource ids (hair, heads, beards, horns...) or MaterialPreset ids (tattoos, makeup).
        Later modules (GustavDev) override earlier ones (SharedDev)."""
        if getattr(self, "_cc", None) is not None:
            return
        cc = {}
        for low in sorted(p for p in self.gd.files if p.endswith("charactercreation/generated_ccicons.lsx")
                          or p.endswith("charactercreation/generated_ccicons.lsf")):
            loc = self.gd.locate(low)
            if not loc:
                continue
            try:
                res = load_resource(self.gd.pak(loc[0]).read(loc[1]))
            except Exception as e:
                self._cc_errors = getattr(self, "_cc_errors", []) + ["%s: %s: %s" % (low, type(e).__name__, e)]
                continue
            module = low.split("/")[1]
            # the sheet's image path (TextureAtlasInfo) and the icon UVs (IconUVList) are separate regions
            path, uvs = None, []
            for reg in res.regions.values():
                for n in reg.walk():
                    if n.get("Path") and str(n.get("Path")).lower().endswith(".dds"):
                        path = "public/%s/%s" % (module, str(n.get("Path")).lower())
                    if n.get("MapKey") is not None and n.get("U1") is not None:
                        uvs.append(n)
            for n in uvs:
                parts = str(n.get("MapKey")).split("_", 2)
                if len(parts) == 3 and path:
                    cc[(parts[0], parts[1].lower(), parts[2].lower())] = (
                        path, float(n.get("U1")), float(n.get("V1")), float(n.get("U2")), float(n.get("V2")))
        self._cc = cc

    def debug(self, name):
        """Step-by-step report of a character-creator icon lookup (for /api/icon_debug)."""
        out = {"name": name}
        try:
            slot, ident, prefix = name[3:].split(":")
            self._cc = None
            self._cc_index()
            out["index_entries"] = len(self._cc)
            out["sheet_index_files"] = [p for p in self.gd.files if "generated_ccicons" in p]
            out["parse_errors"] = getattr(self, "_cc_errors", [])
            out["sheets"] = sorted({v[0] for v in self._cc.values()})
            out["sheet_files_found"] = {p: bool(self.gd.locate(p)) for p in out["sheets"]}
            out["slots_in_index"] = sorted({k[1] for k in self._cc})[:40]
            hits = {k: v for k, v in self._cc.items() if k[2] == ident.lower()}
            out["hits_for_id"] = {"%s_%s" % (k[0], k[1]): v for k, v in hits.items()}
            if hits:
                path = next(iter(hits.values()))[0]
                loc = self.gd.locate(path)
                from PIL import Image
                raw = self.gd.pak(loc[0]).read(loc[1])
                out["dds_bytes"] = len(raw)
                out["dds_header"] = raw[:4].decode("latin-1") + " fourcc=" + raw[84:88].decode("latin-1")
                try:
                    im = Image.open(io.BytesIO(raw))
                    out["pil"] = [im.format, im.mode, im.size]
                    im.load()
                    out["decoded"] = True
                except Exception as e:
                    out["pil_error"] = "%s: %s" % (type(e).__name__, e)
        except Exception:
            import traceback
            out["error"] = traceback.format_exc()
        return out

    def _crop(self, path, u1, v1, u2, v2):
        if path not in self._atlas_img:
            if len(self._atlas_img) > 2:      # the creator sheets are 8192x4096; keep only a couple decoded
                self._atlas_img.clear()
            self._atlas_img[path] = self._open(path)
        im = self._atlas_img[path]
        if im is None:
            return None
        w, h = im.size
        box = (int(round(min(u1, u2) * w)), int(round(min(v1, v2) * h)),
               int(round(max(u1, u2) * w)), int(round(max(v1, v2) * h)))
        return im.crop(box) if box[2] > box[0] and box[3] > box[1] else None

    def _load_cc(self, spec):
        # spec "Slot:id:prefix"
        try:
            slot, ident, prefix = spec.split(":")
        except ValueError:
            return None
        self._cc_index()
        for pre in (prefix, "1" if prefix == "0" else "0"):
            hit = self._cc.get((pre, slot.lower(), ident.lower()))
            if hit:
                return self._crop(*hit)
        return None

    def _load_portrait(self, who):
        """An origin character's portrait (Gui/Assets/Portraits/...(icon_origin_<who>).dds and friends)."""
        who = re.sub(r"[^a-z]", "", who.lower())
        if not who:
            return None
        cands = [f for f in self.gd.files if "/gui/assets/portraits/" in f and f.endswith(".dds") and who in f]

        def score(f):
            b = f.rsplit("/", 1)[-1]
            return ((0 if "(icon_origin_%s)" % who in b else 1 if "(icon_%s)" % who in b else 2),
                    (0 if re.search(r"-eqp?_%s_\(" % who, b) else 1),
                    (0 if "not_" not in b and "dummy" not in b and "genital" not in b else 1),
                    (0 if "/gustavdev/" in f else 1 if "/gustav/" in f else 2), b)
        for f in sorted(cands, key=score)[:3]:
            im = self._open(f)
            if im is not None:
                return im
        return None

    def _render_visual(self, vid, size=THUMB):
        """No icon in the game: draw the model itself (shaded, 3/4 front view) as a thumbnail."""
        import numpy as np
        from PIL import Image
        from .gr2 import read_gr2, extract_meshes
        from .assemble import _object_mesh_name
        v = self.gd.visuals.get(vid)
        if not v or not v.get("source"):
            return None
        loc = self.gd.locate(v["source"])
        if not loc:
            return None
        meshes = {m.name: m for m in extract_meshes(read_gr2(self.gd.pak(loc[0]).read(loc[1])))}
        want = {_object_mesh_name(o) for o, _m, lod in v.get("objects") or [] if lod in (0, None)}
        P, T = [], []
        for name, m in meshes.items():
            if (want and name not in want) or not m.positions or "_LOD" in name or "Proxy" in name:
                continue
            base = sum(len(p) for p in P)
            P.append(np.asarray(m.positions, dtype=np.float64))
            idx = np.asarray(m.indices, dtype=np.int64)
            T.append(idx[: len(idx) // 3 * 3].reshape(-1, 3) + base)
        if not P:
            return None
        P = np.concatenate(P)
        T = np.concatenate(T)
        T = T[(T < len(P)).all(axis=1)]
        # BG3 characters face -Z: look from the front, turned 30 degrees and tipped 12 degrees down
        a, b = np.radians(30), np.radians(12)
        ry = np.array([[np.cos(a), 0, -np.sin(a)], [0, 1, 0], [np.sin(a), 0, np.cos(a)]])
        rx = np.array([[1, 0, 0], [0, np.cos(b), np.sin(b)], [0, -np.sin(b), np.cos(b)]])
        Q = P @ (rx @ ry).T
        Q[:, 0] = -Q[:, 0]                     # mirror so the view isn't left-right flipped
        ss = 2                                 # supersample for smooth edges
        W = size * ss
        lo, hi = Q[:, :2].min(0), Q[:, :2].max(0)
        span = max(hi - lo) or 1.0
        scale = W * 0.88 / span
        off = (W - (hi - lo) * scale) / 2
        X = (Q[:, 0] - lo[0]) * scale + off[0]
        Y = W - ((Q[:, 1] - lo[1]) * scale + off[1])
        Z = Q[:, 2]                            # larger = further from the camera (-Z faces it)
        zbuf = np.full((W, W), np.inf)
        img = np.zeros((W, W), dtype=np.float64)
        light = np.array([-0.4, 0.6, -0.7])
        light /= np.linalg.norm(light)
        tri = Q[T]
        n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        n /= (np.linalg.norm(n, axis=1, keepdims=True) + 1e-12)
        shade = 0.3 + 0.7 * np.abs(n @ light)
        for k in np.argsort(-Z[T].mean(axis=1)):          # far to near; z-test still decides
            i0, i1, i2 = T[k]
            xs, ys = np.array([X[i0], X[i1], X[i2]]), np.array([Y[i0], Y[i1], Y[i2]])
            x0, x1 = int(max(0, np.floor(xs.min()))), int(min(W - 1, np.ceil(xs.max())))
            y0, y1 = int(max(0, np.floor(ys.min()))), int(min(W - 1, np.ceil(ys.max())))
            if x1 < x0 or y1 < y0:
                continue
            gx, gy = np.meshgrid(np.arange(x0, x1 + 1) + 0.5, np.arange(y0, y1 + 1) + 0.5)
            d = (ys[1] - ys[2]) * (xs[0] - xs[2]) + (xs[2] - xs[1]) * (ys[0] - ys[2])
            if abs(d) < 1e-9:
                continue
            w0 = ((ys[1] - ys[2]) * (gx - xs[2]) + (xs[2] - xs[1]) * (gy - ys[2])) / d
            w1 = ((ys[2] - ys[0]) * (gx - xs[2]) + (xs[0] - xs[2]) * (gy - ys[2])) / d
            w2 = 1 - w0 - w1
            inside = (w0 >= 0) & (w1 >= 0) & (w2 >= 0)
            if not inside.any():
                continue
            z = w0 * Z[i0] + w1 * Z[i1] + w2 * Z[i2]
            zb = zbuf[y0:y1 + 1, x0:x1 + 1]
            upd = inside & (z < zb)
            zb[upd] = z[upd]
            img[y0:y1 + 1, x0:x1 + 1][upd] = shade[k]
        mask = np.isfinite(zbuf)
        # warm bone/horn tint; gentle depth cue
        zn = np.where(mask, zbuf, 0)
        if mask.any():
            zmin, zmax = zbuf[mask].min(), zbuf[mask].max()
            depth = 1 - 0.25 * (zn - zmin) / ((zmax - zmin) or 1)
        else:
            depth = 1
        tint = np.array([0.86, 0.78, 0.68])
        rgb = np.clip(img[..., None] * depth[..., None] * tint * 255, 0, 255)
        rgba = np.dstack([rgb, mask * 255.0]).astype(np.uint8)
        return Image.fromarray(rgba, "RGBA").resize((size, size), Image.LANCZOS)

    def _load_texture(self, name):
        """A game texture by name (e.g. an eye-shape mask), shown as a picture."""
        for t in self.gd.textures.values():
            if t.get("name") == name:
                loc = self.gd.locate(t["source"])
                im = self._open(loc[1]) if loc else None
                if im is None:
                    return None
                from PIL import Image
                r, g, b, a = im.split()
                # eye masks: R = dome shading, G = pupil + sclera, A = soft disk -> readable greyscale eye
                return Image.merge("RGBA", (g, g, b if b.getextrema()[1] else g, a if a.getextrema()[0] < 250 else
                                            Image.new("L", im.size, 255)))
        return None

    def _path(self, name):
        return os.path.join(self.dir, re.sub(r"[^A-Za-z0-9_.-]", "_", name) + ".png")

    def cached(self, name):
        """Bytes from the disk cache (b"" = known to have no icon), or None if not looked up yet."""
        p = self._path(name or "")
        if name and os.path.exists(p):
            with open(p, "rb") as f:
                return f.read()
        return None

    def png(self, name):
        """PNG bytes for an icon name, or None. Names: an item Icon MapKey, "cc:Slot:id:prefix" for
        character-creator thumbnails, or "tex:TextureName" for a texture preview."""
        if not name:
            return None
        path = self._path(name)
        if os.path.exists(path):
            with open(path, "rb") as f:
                return f.read() or None
        with self.lock:
            if name.startswith("cc:"):
                im = self._load_cc(name[3:])
            elif name.startswith("mesh:"):
                try:
                    im = self._render_visual(name[5:])
                except Exception:
                    im = None
            elif name.startswith("portrait:"):
                im = self._load_portrait(name[9:])
            elif name.startswith("tex:"):
                im = self._load_texture(name[4:])
            else:
                self._index()
                im = self._load_single(name.lower())
                if im is None:
                    self._index_atlases()
                    im = self._load_atlas(name.lower())
        os.makedirs(self.dir, exist_ok=True)
        data = b""
        if im is not None:
            im.thumbnail((THUMB, THUMB))
            b = io.BytesIO()
            im.save(b, "PNG")
            data = b.getvalue()
        with open(path, "wb") as f:          # an empty file remembers "no icon" so we don't search again
            f.write(data)
        return data or None

    def _open(self, low_path):
        from PIL import Image
        loc = self.gd.locate(low_path)
        if not loc:
            return None
        try:
            im = Image.open(io.BytesIO(self.gd.pak(loc[0]).read(loc[1])))
            im.load()
            return im.convert("RGBA")
        except Exception:
            return None

    def _load_single(self, key):
        p = self._single.get(key)
        return self._open(p) if p else None

    def _load_atlas(self, key):
        hit = (self._atlas or {}).get(key)
        if not hit:
            return None
        path, u1, v1, u2, v2 = hit[:5]
        module = hit[5] if len(hit) > 5 else None
        if path not in self._atlas_img:
            cands = [path] + (["Public/%s/%s" % (module, path)] if module else []) + \
                    ["Public/Shared/" + path, "Public/Game/" + path]
            self._atlas_img[path] = next((im for im in (self._open(c) for c in cands) if im is not None), None)
        im = self._atlas_img[path]
        if im is None:
            return None
        w, h = im.size
        box = (int(round(min(u1, u2) * w)), int(round(min(v1, v2) * h)),
               int(round(max(u1, u2) * w)), int(round(max(v1, v2) * h)))
        return im.crop(box) if box[2] > box[0] and box[3] > box[1] else None


def _lsx(raw):
    """Minimal .lsx (XML) reader exposing the same regions/walk()/get() surface as lsf.load_resource."""
    import xml.etree.ElementTree as ET

    class N(object):
        def __init__(self, el):
            self.name = el.get("id")
            self.attrs = {a.get("id"): a.get("value") for a in el.findall("attribute")}
            ch = el.find("children")
            self.children = [N(c) for c in ch.findall("node")] if ch is not None else []

        def get(self, k, d=None):
            return self.attrs.get(k, d)

        def walk(self):
            yield self
            for c in self.children:
                yield from c.walk()

    class R(object):
        regions = {}

    r = R()
    r.regions = {}
    root = ET.fromstring(raw)
    for reg in root.iter("region"):
        nodes = [N(n) for n in reg.findall("node")]
        top = N(ET.Element("node"))
        top.children = nodes
        r.regions[reg.get("id")] = top
    return r
