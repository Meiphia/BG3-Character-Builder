"""Bake BG3 materials into plain PBR textures (base color, normal, ORM) so exports look right
in any app. Each shader family gets an approximation of Larian's shader math:

  skin   HMVY (hemoglobin/melanin/vein/yellowing masks) + CLEA (cavity/lips/eyebrows/AO) + presets
  base   gear: albedo x 12-colour MSK dye palette, physical map = metal/rough/AO
  hair   ID/Depth/Root/Alpha mask x hair colour preset
  eye    iris/sclera colours from the eye preset
Texture sources are DDS files in the paks and Granite virtual textures.
"""

import io
import os

import numpy as np

from .vt import TileSet
from . import skinmodel

# MSKColor dye key colours (sRGB 0..1) -> material parameter (bg3.wiki "Clothing Texture Maps")
MSK_KEYS = [
    ((1, 0.5, 0), "Cloth_Primary"), ((1, 0, 0), "Cloth_Secondary"), ((1, 0.5, 0.5), "Cloth_Tertiary"),
    ((0.5, 0, 1), "Leather_Primary"), ((0, 0, 1), "Leather_Secondary"), ((0.5, 0.5, 1), "Leather_Tertiary"),
    ((0, 1, 0.5), "Metal_Primary"), ((0, 1, 0), "Metal_Secondary"), ((0.5, 1, 0.5), "Metal_Tertiary"),
    ((1, 0, 0.5), "Accent_Color"), ((0, 0.5, 1), "Custom_1"), ((0.5, 1, 0), "Custom_2"),
]
# simple meshes use Color_01..03 instead (= Cloth/Metal/Leather Secondary keys)
MSK_ALT = {"Cloth_Secondary": "Color_01", "Metal_Secondary": "Color_02", "Leather_Secondary": "Color_03"}

def srgb_to_linear(c):
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def linear_to_srgb(c):
    c = np.clip(c, 0, 1)
    return np.where(c <= 0.0031308, 12.92 * c, 1.055 * np.power(c, 1 / 2.4) - 0.055)


def _resize_raw(img, size):
    """Resize every channel independently. PIL premultiplies RGB by alpha when resizing RGBA,
    which corrupts BG3 maps that store data in alpha (normal X, yellowing, iris masks...)."""
    from PIL import Image
    if img.mode in ("RGBA", "LA"):
        return Image.merge(img.mode, [b.resize(size, Image.BILINEAR) for b in img.split()])
    return img.resize(size, Image.BILINEAR)


class TextureSource(object):
    """Loads DDS textures and virtual textures referenced by a recipe."""

    def __init__(self, gd, recipe, vt_dirs=(), max_size=2048):
        self.gd, self.recipe, self.vt_dirs, self.max_size = gd, recipe, list(vt_dirs), max_size
        self._tilesets = {}
        self._cache = {}
        import threading
        self.io = threading.RLock()     # pak file handles and tile sets are shared: one reader at a time

    def _fit(self, img):
        if self.max_size and max(img.size) > self.max_size:
            s = self.max_size / float(max(img.size))
            img = _resize_raw(img, (max(1, int(img.size[0] * s)), max(1, int(img.size[1] * s))))
        return img

    def image(self, tid):
        """Decoded PIL image at native size (cached) — used for atlases."""
        key = ("img", tid)
        if key not in self._cache:
            from PIL import Image
            t = self.recipe["textures"].get(tid)
            im = None
            if t and t.get("pak"):
                try:
                    with self.io:
                        raw = self.gd.pak(t["pak"]).read(t["path"])
                    im = Image.open(io.BytesIO(raw))
                    im.load()
                    im = im.convert("RGBA")
                except Exception:
                    im = None
            self._cache[key] = im
        return self._cache[key]

    def dds(self, tid):
        """RGBA float array 0..1 (raw values, no colour conversion) or None."""
        key = ("dds", tid)
        if key in self._cache:
            return self._cache[key]
        from PIL import Image
        t = self.recipe["textures"].get(tid)
        arr = None
        if t and t.get("pak"):
            try:
                with self.io:
                    raw = self.gd.pak(t["pak"]).read(t["path"])
                im = Image.open(io.BytesIO(raw))
                im.load()
                arr = np.asarray(self._fit(im.convert("RGBA")), dtype=np.float32) / 255.0
            except Exception:
                arr = None
        self._cache[key] = arr
        return arr

    def _read_vt_file(self, name):
        base = name.replace("\\", "/").rsplit("/", 1)[-1]
        for d in self.vt_dirs:
            p = os.path.join(d, base)
            if os.path.exists(p):
                with open(p, "rb") as f:
                    return f.read()
        return self.gd.read_file("Generated/Public/VirtualTextures/" + base)

    def vt(self, vtid, layer):
        with self.io:
            return self._vt(vtid, layer)

    def _vt(self, vtid, layer):
        key = ("vt", vtid, layer)
        if key in self._cache:
            return self._cache[key]
        info = self.recipe["virtual_textures"].get(vtid)
        arr = None
        if info:
            gtex = info["gtex"]
            gts = "Albedo_Normal_Physical_%s.gts" % gtex[0]
            try:
                ts = self._tilesets.get(gts)
                if ts is None:
                    ts = TileSet(self._read_vt_file(gts), self._read_vt_file)
                    self._tilesets[gts] = ts
                arr = np.asarray(self._fit(ts.extract(gtex, layer)), dtype=np.float32) / 255.0
            except Exception as e:
                self.recipe.setdefault("warnings", []).append("virtual texture %s: %s" % (gtex, e))
        self._cache[key] = arr
        return arr


# ---------------------------------------------------------------- helpers

def _vec(mat, name, default):
    v = mat["vec3"].get(name)
    return np.array(v if v else default, dtype=np.float32)


def _scal(mat, name, default):
    v = mat["scalar"].get(name)
    return float(v) if v is not None else default


def _resize_float(a, shape):
    """Smooth float resize (per channel, bicubic) without 8-bit quantisation."""
    if a.shape[:2] == tuple(shape[:2]):
        return a
    from PIL import Image
    chans = [np.asarray(Image.fromarray(np.ascontiguousarray(a[..., c], dtype=np.float32), "F")
                        .resize((shape[1], shape[0]), Image.BICUBIC)) for c in range(a.shape[-1])]
    return np.clip(np.stack(chans, -1), 0, None)


def _resize_to(a, shape):
    if a is None or a.shape[:2] == shape[:2]:
        return a
    from PIL import Image
    im = Image.fromarray((np.clip(a, 0, 1) * 255).astype(np.uint8))
    return np.asarray(_resize_raw(im, (shape[1], shape[0])), dtype=np.float32) / 255.0


def _atlas_cell(src, tid, index, shape, pages=3, grid=4):
    """Mask for creator atlas slot `index`: channel = index // 16, cell = index % 16 (row-major)."""
    ch = index // (grid * grid)
    if ch >= pages:
        return None
    im = src.image(tid)
    if im is None:
        return None
    cell = index % (grid * grid)
    cw, chh = im.size[0] // grid, im.size[1] // grid
    x0, y0 = (cell % grid) * cw, (cell // grid) * chh
    from PIL import Image
    crop = im.crop((x0, y0, x0 + cw, y0 + chh)).getchannel("RGBA"[ch])
    crop = crop.resize((shape[1], shape[0]), Image.BILINEAR)
    return np.asarray(crop, dtype=np.float32) / 255.0


NORMAL_Y_SIGN = -1.0   # BG3 normal maps are DirectX (Y down); glTF/Blender expect OpenGL


def normal_from_packed(nm):
    """BG3 normal maps: X in alpha, Y in green -> tangent-space RGB normal map (OpenGL-style)."""
    x = nm[..., 3] * 2 - 1
    y = (nm[..., 1] * 2 - 1) * NORMAL_Y_SIGN
    z = np.sqrt(np.clip(1 - x * x - y * y, 0, 1))
    return np.stack([x * 0.5 + 0.5, y * 0.5 + 0.5, z * 0.5 + 0.5], -1)


def _png(arr, mode="RGBA", level=6):
    from PIL import Image
    a = (np.clip(arr, 0, 1) * 255 + 0.5).astype(np.uint8)
    im = Image.fromarray(a, mode if a.shape[-1] == len(mode) else None)
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=False, compress_level=level)
    return buf.getvalue()


def _orm(occ, rough, metal):
    return np.stack([occ, rough, metal], -1)


# ---------------------------------------------------------------- per-family bakers

def _skin_albedo(mat, H, M, V, Y, cancel_g):
    """Vectorised skinmodel.skin_color (see skinmodel.py for the derivation)."""
    p = lambda k: _scal(mat, k, skinmodel.DEFAULTS[k])
    c = lambda k: np.maximum(_vec(mat, k, skinmodel.DEFAULTS[k]), 1e-5)[None, None, :]
    thr, mult = p("MelaninDarkThreshold"), p("MelaninDarkMultiplier")
    b = 1.0 - M
    dark = np.where(b <= thr, b, thr + (mult - thr) * np.clip((b - thr) / max(1e-6, 1 - thr), 0, 1))
    mel = np.maximum((dark + p("MelaninAmount")) * (1 - cancel_g * p("MelaninRemovalAmount")), 0)
    hb = np.clip(H * p("HemoglobinAmount"), 0, 1)
    eh = (1 - np.exp(-hb * hb))[..., None]
    em = (1 - np.exp(-mel * mel))[..., None]
    vw = np.clip(V * p("VeinAmount"), 0, 1)[..., None]
    hm = (np.power(c("HemoglobinColor"), eh) * (1 - vw) + _vec(mat, "VeinColor", skinmodel.DEFAULTS["VeinColor"])[None, None, :] * vw)
    hm = hm * np.power(c("MelaninColor"), em)
    yw = (Y * p("YellowingAmount"))[..., None]
    hm = (hm * (1 - yw) + hm * c("YellowingColor") * yw) * np.array(skinmodel.SKIN_TINT, np.float32)
    lum = (hm * np.array(skinmodel.LUMA, np.float32)).sum(-1, keepdims=True)
    return lum + (hm - lum) * (1 - eh * em)


def lips_pre(clea):
    return clea[..., 2] if clea is not None else 0.0


def bake_skin(src, mat, vt_id):
    t = mat["textures"]
    body = "Body" in (mat.get("shader") or "") or _scal(mat, "isBody", 0) > 0
    if vt_id:
        clea, nm, hmvy = src.vt(vt_id, 0), src.vt(vt_id, 1), src.vt(vt_id, 2)
    else:
        clea = src.dds(t.get("CLEA")) if t.get("CLEA") else None
        hmvy = src.dds(t.get("HMVY")) if t.get("HMVY") else None
        nm = src.dds(t.get("normalmap")) if t.get("normalmap") else None
    if hmvy is None:
        return None
    shape = hmvy.shape
    hw = shape[:2]
    clea = _resize_to(clea, shape)
    nm = _resize_to(nm, shape)
    cancel = _resize_to(src.dds(t.get("CancelMSK")), shape) if t.get("CancelMSK") else None
    zeros = np.zeros(hw, np.float32)
    cancel_r, cancel_g = (cancel[..., 0], cancel[..., 1]) if cancel is not None else (zeros, zeros)
    H, M, V, Y = hmvy[..., 0], hmvy[..., 1], hmvy[..., 2], hmvy[..., 3]

    base = _skin_albedo(mat, H, M, V, Y, cancel_g)
    if clea is not None:
        cav, hairm, lips, ao = clea[..., 0], clea[..., 1], clea[..., 2], clea[..., 3]
    else:
        cav, hairm, lips, ao = np.ones(hw, np.float32), zeros, zeros, np.ones(hw, np.float32)

    if body:
        hairm = hairm * _scal(mat, "BG_BodyHair", 1.0)

    # ---- pigment extras from the shared GenericMaps (head: R T-zone, G freckles, B vitiligo, A age;
    #      body: R freckles, G T-zone, B vitiligo)
    gen = _resize_to(src.dds(t["BG_GenericMaps"]), shape) if t.get("BG_GenericMaps") else None
    tzone = None
    if gen is not None:
        frk_m, tzone, vit_m = (gen[..., 0], gen[..., 1], gen[..., 2]) if body else (gen[..., 1], gen[..., 0], gen[..., 2])
        age_m = None if body else gen[..., 3]
        fr = _scal(mat, "BG_Freckles", 0.0)
        if fr > 0:
            darker = _skin_albedo(_with_scalars(mat, MelaninAmount=_scal(mat, "MelaninAmount", 0.3) + 0.55),
                                  H, M, V, Y, cancel_g)
            w = np.clip(frk_m * (1.5 + 2.5 * fr) - (1 - fr) * 0.4, 0, 1) * min(1.0, fr * 1.6)
            base = base * (1 - w[..., None]) + darker * w[..., None]
        vi = _scal(mat, "BG_Vitiligo", 0.0)
        if vi > 0:
            pale = _skin_albedo(_with_scalars(mat, MelaninAmount=0.0, MelaninRemovalAmount=1.0), H, np.ones_like(M), V, Y,
                                np.ones_like(cancel_g))
            w = np.clip(((1 - vit_m) - (1 - vi)) / 0.12, 0, 1) * 0.8
            base = base * (1 - w[..., None]) + pale * w[..., None]
        ag = _scal(mat, "BG_Age", 0.0)
        if ag > 0:
            lum = (base * np.array(skinmodel.LUMA, np.float32)).sum(-1, keepdims=True)
            base = base + (lum - base) * (0.25 * ag)                     # older skin: less saturated
            if age_m is not None:
                base = base * (1 - 0.5 * ag * (1 - age_m))[..., None]     # creases, under-eye, jowls
                cav = cav * (1 - 0.3 * ag * (1 - age_m))

    # ceremorphosis (half-illithid): dark purple veins from the shared head/body vein masks
    ill = _scal(mat, "IllithidColorWeight", 0.0)
    if ill > 0 and t.get("IllithidMSK"):
        im = _resize_to(src.dds(t["IllithidMSK"]), shape)
        if im is not None:
            w = np.clip((im[..., 0] - 0.18) / 0.45, 0, 1) * ill * (1 - 0.7 * lips_pre(clea))
            vc = _vec(mat, "IllithidVeinColor", [0.012, 0.004, 0.02])
            base = base * (1 - w[..., None]) + vc[None, None, :] * w[..., None]

    # roughness: shared skin value broken up by the cavity map, hair regions rougher
    r0 = _scal(mat, "TMP_BODY_ROUGH", 0.55)
    rough = np.clip(r0 * cav + (1 - cav), 0, 1)
    if tzone is not None:                    # shared T-zone roughness: oilier nose/forehead
        rough = np.clip(rough * (0.75 + 0.5 * tzone), 0, 1)
    rough = rough * (1 - hairm) + 0.8 * hairm
    metal = zeros.copy()

    # creator tattoos: atlas = 3 channel pages x 4x4 cells (each cell a miniature of the full UV);
    # ink follows the skin's cavity (TattooCurvatureInfluence)
    pre = "Body" if body else ""
    ti = int(round(_scal(mat, pre + "TattooIndex", -1)))
    inten = mat["vec4"].get(pre + "TattooIntensity") or [0, 0, 0, 0]
    if ti >= 0 and t.get("TattooAtlas") and max(inten) > 0 and (_scal(mat, "doDrawTattoo", 1) > 0):
        # TattooIndex picks the cell (0-15) of the 4x4 atlas; which RGBA channel page is used is
        # decided by the non-zero component of TattooIntensity (presets 1-15 R, 16+31-44 G, 17-30 B)
        curv = _scal(mat, "TattooCurvatureInfluence", 1.0)
        for ch in range(4):
            if float(inten[ch]) <= 0:
                continue
            m = _atlas_cell(src, t["TattooAtlas"], ch * 16 + (ti % 16), shape, pages=4)
            if m is None:
                continue
            col_name = pre + "TattooColor" + ("", "G", "B", "A")[ch]
            tc = _vec(mat, col_name, _vec(mat, pre + "TattooColor", [0.02, 0.02, 0.02]))
            w = np.clip(m * float(inten[ch]) * (1 - curv + curv * cav), 0, 1)
            base = base * (1 - w[..., None]) + tc[None, None, :] * w[..., None]
            rough = np.clip(rough + w * _scal(mat, "TattooRoughnessOffset", 0.0), 0, 1)
            metal = metal + w * _scal(mat, "TattooMetalness", 0.0)
    # creator makeup (eye paint etc.): makeup atlas red channel, 4x4 cells
    mi = int(round(_scal(mat, "MakeUpIndex", -1)))
    mint = _scal(mat, "MakeupIntensity", 0.0)
    if mi >= 0 and mint > 0 and t.get("MakeUpAtlas") and not body and _scal(mat, "doDrawMakeup", 1) > 0:
        m = _atlas_cell(src, t["MakeUpAtlas"], mi, shape, pages=1)
        if m is not None:
            mc = _vec(mat, "MakeupColor", [0.05, 0.02, 0.02])
            w = np.clip(m * mint, 0, 1)
            base = base * (1 - w[..., None]) + mc[None, None, :] * w[..., None]
            rough = rough * (1 - w) + w * _scal(mat, "MakeupRoughness", 0.8)
            metal = metal + w * _scal(mat, "EyesMakeupMetalness", 0.0)
    # brows / stubble / body hair painted in the CLEA hair mask
    hc = _vec(mat, "Body_Hair_Color" if body else "Eyebrow_Color", [0.06, 0.04, 0.025])
    base = base * (1 - hairm[..., None]) + hc[None, None, :] * hairm[..., None]
    # lipstick (CLEA blue)
    if body:
        li, lc, lr = _scal(mat, "CLEA_Makeup_Weight", 0.0), _vec(mat, "CLEA_Makeup_Color", [0.4, 0.1, 0.1]), _scal(mat, "CLEA_Makeup_Roughness", 0.4)
    else:
        li, lc, lr = _scal(mat, "LipsMakeupIntensity", 0.0), _vec(mat, "Lips_Makeup_Color", [0.45, 0.23, 0.25]), _scal(mat, "LipsMakeupRoughness", 0.5)
    if li > 0:
        w = np.clip(lips * li * (1 - cancel_r), 0, 1)
        base = base * (1 - w[..., None]) + lc[None, None, :] * w[..., None]
        rough = rough * (1 - w) + lr * w
        metal = metal + w * _scal(mat, "LipsMakeupMetalness", 0.0)
    # non-skin parts (nails, tiefling horn plates, ...): BM map x NonSkinColor where CancelMSK.r
    if cancel is not None and t.get("BaseColor_NonSkin") and _scal(mat, "NonSkin_Weight", 1.0) > 0:
        bm = _resize_to(src.dds(t["BaseColor_NonSkin"]), shape)
        if bm is not None:
            w = np.clip(cancel_r * _scal(mat, "NonSkin_Weight", 1.0), 0, 1)
            ns = bm[..., :1] * _vec(mat, "NonSkinColor", [0.5, 0.5, 0.5])[None, None, :]
            base = base * (1 - w[..., None]) + ns * w[..., None]
            metal = metal + w * _scal(mat, "NonSkinMetalness", 0.0)
    # ---- weathering: dirt (R), bruises (G), blood (B) of the shared dirt maps; sweat (B) of the sweat maps
    dirt = _resize_to(src.dds(t["BG_DirtMaps"]), shape) if t.get("BG_DirtMaps") else None
    if dirt is not None:
        def grow(mask, amount, soft):
            return np.clip((mask - (1 - amount)) / soft, 0, 1) * min(1.0, amount * 3)
        d = _scal(mat, "Dirt", 0.0)
        if d > 0:
            w = grow(dirt[..., 0], d, 0.45) * 0.75
            dc = _vec(mat, "DirtColor", [0.117, 0.066, 0.048])
            base = base * (1 - w[..., None]) + dc[None, None, :] * w[..., None]
            wet = _scal(mat, "Dirt_Wetness", 0.2)
            rough = rough * (1 - w) + w * (0.9 - 0.6 * wet)
        br = _scal(mat, "Bruises", 0.0)
        if br > 0:
            w = grow(dirt[..., 1], br, 0.4) * 0.75
            base = base * (1 - w[..., None]) + base * np.array([0.45, 0.28, 0.45], np.float32) * w[..., None]
        bl = _scal(mat, "Blood", 0.0)
        if bl > 0:
            w = grow(dirt[..., 2], bl, 0.25)
            bc = _vec(mat, "Blood_Color", [0.13, 0.008, 0.004])
            base = base * (1 - w[..., None]) + bc[None, None, :] * w[..., None]
            rough = rough * (1 - w) + 0.25 * w
    sweat = _resize_to(src.dds(t["BG_SweatMaps"]), shape) if t.get("BG_SweatMaps") else None
    sw = _scal(mat, "Sweat", 0.0)
    if sweat is not None and sw > 0:
        w = np.clip(sweat[..., 2] * 1.6 * sw + 0.25 * sw, 0, 1)
        rough = rough * (1 - w) + 0.18 * w
        base = base * (1 - 0.06 * w[..., None])
    out = {"baseColor": np.concatenate([linear_to_srgb(base), np.ones(hw + (1,), np.float32)], -1),
           "orm": _orm(ao, rough, np.clip(metal, 0, 1))}
    if nm is not None:
        out["normal"] = normal_from_packed(nm)
    _add_glow(src, mat, shape, out)
    return out


def _with_scalars(mat, **kw):
    m = dict(mat)
    m["scalar"] = dict(mat["scalar"], **kw)
    return m


def _add_glow(src, mat, shape, out):
    """Glow maps (CHAR_*_GM shaders): mask x GlowColor, scaled by GlowMultiplier."""
    t = mat["textures"]
    tid = t.get("Glowmap") or t.get("glowmap") or t.get("GlowMap")
    mult = _scal(mat, "GlowMultiplier", 1.0)
    if not tid or mult <= 0:
        return
    if _scal(mat, "Use_BlackBody", 0.0) > 0 or "TemperatureMax" in mat.get("scalar", {}):
        emis, _m = _heat_glow(src, mat, shape)          # VFX heat shaders: glow like hot metal, not white
        if emis is not None:
            out["emissive"] = linear_to_srgb(np.clip(emis, 0, 1))
        return
    g = _resize_to(src.dds(tid), shape)
    if g is None:
        return
    m = g[..., :3].max(-1)
    if float(m.max()) < 0.02:
        return
    col = _vec(mat, "GlowColor", [1, 1, 1])
    out["emissive"] = linear_to_srgb(col[None, None, :] * (m * min(1.0, mult / 2.0))[..., None])


def blackbody(T):
    """Linear RGB of a black body at temperature T (Kelvin, array), normalised to max 1
    (Tanner Helland's fit, which is what VFX colour ramps of this kind approximate)."""
    t = np.clip(np.asarray(T, np.float32), 800.0, 12000.0) / 100.0
    r = np.where(t <= 66, 255.0, 329.698727446 * np.power(np.maximum(t - 60, 1e-3), -0.1332047592))
    g = np.where(t <= 66, 99.4708025861 * np.log(t) - 161.1195681661,
                 288.1221695283 * np.power(np.maximum(t - 60, 1e-3), -0.0755148492))
    b = np.where(t >= 66, 255.0, np.where(t <= 19, 0.0, 138.5177312231 * np.log(np.maximum(t - 10, 1e-3)) - 305.0447927307))
    rgb = np.clip(np.stack([r, g, b], -1) / 255.0, 0, 1)
    return srgb_to_linear(rgb)


def _heat_glow(src, mat, shape):
    """Larian's blackbody glow (Karlach's engine and the cracks it lights up): the glow map drives a
    temperature up to TemperatureMax, coloured as a black body. Returns (emissive linear RGB, heat mask)."""
    t = mat["textures"]
    tid = t.get("Glowmap") or t.get("glowmap") or t.get("GlowMap")
    if not tid:
        return None, None
    g = _resize_to(src.dds(tid), shape)
    if g is None:
        return None, None
    m = np.clip(g[..., :3].max(-1), 0, 1)
    if float(m.max()) < 0.02:
        return None, None
    tmax = _scal(mat, "TemperatureMax", 2500.0)
    power = _scal(mat, "Blackbody_PreRamp_Power", 0.8)
    temp = 900.0 + (tmax - 900.0) * np.power(m, max(power, 0.1))
    col = blackbody(temp)
    strength = np.clip(_scal(mat, "Emissive_Mult", 1.0) * _scal(mat, "Emissive_Power", 1.0), 0.6, 1.0)
    return col * (m * strength)[..., None], m


def bake_heat_metal(src, mat):
    """VFX_Model_Unlit_Glow_* (Karlach's infernal engine plates): there's no colour map, only a glow map.
    Dark forged iron where cool, glowing like hot metal where the map is bright."""
    t = mat["textures"]
    tid = t.get("Glowmap") or t.get("glowmap") or t.get("GlowMap")
    g = src.dds(tid) if tid else None
    if g is None:
        return None
    shape = g.shape
    emis, m = _heat_glow(src, mat, shape)
    if m is None:
        m = np.zeros(shape[:2], np.float32)
        emis = np.zeros(shape[:2] + (3,), np.float32)
    iron = np.array([0.045, 0.038, 0.034], np.float32)
    hot = blackbody(np.full(shape[:2], 1600.0)) * 0.35
    base = iron[None, None, :] * (1 - m[..., None]) + hot * m[..., None]
    ones = np.ones(shape[:2], np.float32)
    rough = 0.42 + 0.25 * m
    return {"baseColor": np.concatenate([linear_to_srgb(base), ones[..., None]], -1),
            "orm": _orm(ones, rough, np.clip(0.9 - m, 0.2, 0.9)),
            "emissive": linear_to_srgb(np.clip(emis * 1.4, 0, 1))}


def bake_base(src, mat, vt_id):
    t = mat["textures"]
    if vt_id:
        alb, nm, phys = src.vt(vt_id, 0), src.vt(vt_id, 1), src.vt(vt_id, 2)
    else:
        alb = src.dds(t.get("basecolor")) if t.get("basecolor") else None
        nm = src.dds(t.get("normalmap")) if t.get("normalmap") else None
        phys = src.dds(t.get("physicalmap")) if t.get("physicalmap") else None
    if alb is None:
        return None
    shape = alb.shape
    nm, phys = _resize_to(nm, shape), _resize_to(phys, shape)
    rgb = srgb_to_linear(alb[..., :3])
    msk = src.dds(t.get("MSKColor")) if t.get("MSKColor") else None
    if msk is not None:
        # soft key matching at the mask's own resolution (Larian-style threshold of 0.25 per key),
        # then a smooth upscale of the resulting tint: no stair-stepped dye borders
        m = msk[..., :3]
        keys = np.array([k for k, _ in MSK_KEYS], np.float32)
        dist = np.sqrt(((m[..., None, :] - keys[None, None, :, :]) ** 2).sum(-1))
        w = np.clip((1 - dist / np.sqrt(3.0) - 0.75) / 0.25, 0, 1)
        total = w.sum(-1, keepdims=True)
        w = w / np.maximum(total, 1.0)
        colors = []
        for _, name in MSK_KEYS:
            c = mat["vec3"].get(name) or mat["vec3"].get(MSK_ALT.get(name, "")) or [1, 1, 1]
            colors.append(c)
        tint_lo = (w[..., :, None] * np.array(colors, np.float32)[None, None, :, :]).sum(-2)
        tint_lo = tint_lo + (1 - np.clip(total, 0, 1))          # unmasked (black) texels stay undyed
        tint = _resize_float(tint_lo, shape)
        rgb = rgb * tint
    elif mat.get("shader_family") == "horns":
        grad = src.dds(t.get("SkinGradientHorn_MSK")) if t.get("SkinGradientHorn_MSK") else None
        g = _resize_to(grad, shape)[..., 1:2] if grad is not None else np.zeros(shape[:2] + (1,), np.float32)
        horn = _vec(mat, "NonSkinColor", [0.17, 0.14, 0.11])[None, None, :]
        tip = _vec(mat, "NonSkinTipColor", [0.09, 0.07, 0.05])[None, None, :]
        rgb = (rgb / 0.57) * (horn * (1 - g) + tip * g)
    alpha = alb[..., 3:4] if "AlphaTest" in (mat.get("shader") or "") else np.ones(shape[:2] + (1,), np.float32)
    out = {"baseColor": np.concatenate([linear_to_srgb(rgb), alpha], -1)}
    if phys is not None:
        out["orm"] = _orm(phys[..., 2], phys[..., 1], phys[..., 0])
    if nm is not None:
        out["normal"] = normal_from_packed(nm)
    _add_glow(src, mat, shape, out)
    return out


def _smooth(x, mid, soft):
    soft = max(soft, 1e-3)
    t = np.clip((x - (mid - soft)) / (2 * soft), 0, 1)
    return t * t * (3 - 2 * t)


def _bleed(col, alpha, cut=0.3):
    """Fill the colour of see-through texels from nearby strands (push-pull pyramid). Texture filtering and
    mipmaps blend across strand edges; without this they pull in black and hair gets dark halos/outlines."""
    from PIL import Image
    h, w = alpha.shape
    wgt = (alpha > cut).astype(np.float32)
    if wgt.all() or not wgt.any():
        return col
    levels = []
    c, a = col * wgt[..., None], wgt
    while min(c.shape[:2]) > 1:
        levels.append((c, a))
        nh, nw = max(1, c.shape[0] // 2), max(1, c.shape[1] // 2)
        c = np.stack([np.asarray(Image.fromarray(c[..., k], "F").resize((nw, nh), Image.BOX)) for k in range(3)], -1)
        a = np.asarray(Image.fromarray(a, "F").resize((nw, nh), Image.BOX))
    fill = c / np.maximum(a, 1e-6)[..., None]
    for c, a in reversed(levels):
        hh, ww = a.shape
        up = np.stack([np.asarray(Image.fromarray(fill[..., k].astype(np.float32), "F").resize((ww, hh), Image.BILINEAR))
                       for k in range(3)], -1)
        own = c / np.maximum(a, 1e-6)[..., None]
        k = np.clip(a * 4, 0, 1)[..., None]
        fill = own * k + up * (1 - k)
    return np.where(wgt[..., None] > 0, col, fill).astype(np.float32)


def bake_hair(src, mat):
    """CHAR_Hair / CHAR_Scalp / lashes. ID_Depth_Root_Alpha_MSKA: R per-strand random ID,
    G depth (0 = inner layers), B root gradient, A coverage."""
    t = mat["textures"]
    m = src.dds(t.get("ID_Depth_Root_Alpha_MSKA")) if t.get("ID_Depth_Root_Alpha_MSKA") else None
    if m is None:
        return None
    shader = mat.get("shader") or ""
    ident, depth, root, alpha = m[..., 0], m[..., 1], m[..., 2], m[..., 3]
    scalp = "CHAR_Scalp" in shader
    lashes = "LashesShadow" in shader
    beard = _scal(mat, "DontTouchMe_isBeard", 0) > 0
    pre = "Scalp_" if scalp else ""
    hair = _vec(mat, "Beard_Color" if beard else "Hair_Color", [0.25, 0.14, 0.08])
    if scalp:
        hair = _vec(mat, "Hair_Scalp_Color", hair * 0.8)
    if lashes:
        hair = _vec(mat, "Eyelashes_Color", [0.02, 0.015, 0.012])
    idc = _scal(mat, pre + "IDContrast", 0.5)
    idf = np.clip(0.5 + (ident - 0.5) * (0.6 + idc), 0, 1)
    col = hair[None, None, :] * (0.65 + 0.7 * idf)[..., None]
    if not scalp:
        # inner layers darker (self-shadowing the engine adds with depth offset)
        cdc = _scal(mat, "ColorDepthContrast", 0.5)
        col = col * (1 - cdc * 0.7 * (1 - depth))[..., None]
        # roots: darker, hue-shifted toward HueShiftColorWeight
        rw = _smooth(root, 1 - _scal(mat, "RootTransitionMidPoint", 0.1), _scal(mat, "RootTransitionSoftness", 0.2))
        hs = _vec(mat, "HueShiftColorWeight", hair * 0.5)
        hs = hs / max(float(hs.max()), 1e-4) * float(hair.max()) * 0.55
        col = col * (1 - 0.6 * rw[..., None]) + hs[None, None, :] * (0.6 * rw[..., None])
        # highlights and graying pick whole strands via the per-strand ID
        r2 = (ident * 7.31) % 1.0
        hli = _scal(mat, "Beard_Highlight_Intensity" if beard else "Highlight_Intensity", 0.0)
        hl = _vec(mat, "Beard_Highlight_Color" if beard else "Highlight_Color", hair)
        sel = np.clip((ident - (1 - hli * 0.6)) / 0.06, 0, 1)[..., None] * min(1.0, hli * 2.0)
        col = col * (1 - sel) + hl[None, None, :] * (0.75 + 0.5 * idf[..., None]) * sel
        gri = _scal(mat, "Beard_Graying_Intensity" if beard else "Graying_Intensity", 0.0)
        gc = _vec(mat, "Beard_Graying_Color" if beard else "Hair_Graying_Color", [0.9, 0.9, 0.9]) * 0.8
        gsel = np.clip((gri * 0.6 - r2) / 0.05, 0, 1)[..., None]
        col = col * (1 - gsel) + gc[None, None, :] * (0.7 + 0.4 * idf[..., None]) * gsel
    else:
        gri = _scal(mat, "Scalp_Graying_Intensity", 0.0)
        if gri > 0:
            gc = _vec(mat, "Hair_Scalp_Graying_Color", [0.7, 0.7, 0.7]) * 0.6
            col = col * (1 - gri * 0.6) + gc[None, None, :] * gri * 0.6
        # the scalp layer is a soft painted fade, strands show through its ID
        # Scalp_MinValue is the faint stubble tint *inside* the painted hairline; outside it (raw coverage ~0)
        # the scalp must stay fully clear, or the mesh edge shows as a pale band across the forehead
        inside = np.clip((alpha - 0.02) / 0.12, 0, 1)
        alpha = np.clip(np.maximum(_smooth(alpha, 0.45, 0.25) * (0.55 + 0.6 * idf),
                                   _scal(mat, "Scalp_MinValue", 0.0) * inside), 0, 1)
    rough = np.clip(_scal(mat, pre + "Roughness", 0.45) + _scal(mat, pre + "RoughnessContrast", 0.05) * (ident - 0.5) * 2, 0.5, 1)
    col = _bleed(col, alpha)
    return {"baseColor": np.concatenate([linear_to_srgb(col), alpha[..., None]], -1),
            "orm": _orm(np.clip(0.55 + 0.45 * depth, 0, 1) if not scalp else np.ones_like(alpha), rough, np.zeros_like(alpha))}


IRIS_SCALE = 0.75
IRIS_GAIN = 0.9      # keeps the iris close to the preset colour after lighting


def _shrink_center(a, s):
    """Scale texture content about the centre by s (<1 shrinks), sampling the source outward and
    clamping at its border so the off-iris filler values carry on."""
    h, w = a.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    sx = np.clip((xx - w / 2 + 0.5) / s + w / 2 - 0.5, 0, w - 1)
    sy = np.clip((yy - h / 2 + 0.5) / s + h / 2 - 0.5, 0, h - 1)
    x0, y0 = np.floor(sx).astype(np.int32), np.floor(sy).astype(np.int32)
    x1, y1 = np.minimum(x0 + 1, w - 1), np.minimum(y0 + 1, h - 1)
    fx, fy = (sx - x0)[..., None], (sy - y0)[..., None]
    top = a[y0, x0] * (1 - fx) + a[y0, x1] * fx
    bot = a[y1, x0] * (1 - fx) + a[y1, x1] * fx
    return top * (1 - fy) + bot * fy


def _iris_geometry(rm):
    """Centre and radius of the iris (annulus where RegionMask.G is 0, plus the pupil) in UV units."""
    h, w = rm.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w]
    u, v = (xx + 0.5) / w, (yy + 0.5) / h
    near = np.hypot(u - 0.5, v - 0.5) < 0.25
    g, disk = rm[..., 1], rm[..., 3]
    m = near & ((g < 0.5) | (disk > 0.5))
    if m.sum() < 16:
        return 0.5, 0.5, 0.15
    cu, cv = float(u[m].mean()), float(v[m].mean())
    return cu, cv, float(np.sqrt(m.sum() / np.pi) / w)


def _shift(a, du, dv):
    """Move texture content by (du, dv) UV, clamping at the borders."""
    h, w = a.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w]
    sx = np.clip(np.round(xx - du * w).astype(np.int32), 0, w - 1)
    sy = np.clip(np.round(yy - dv * h).astype(np.int32), 0, h - 1)
    return a[sy, sx]


def _ring_centre(cm):
    h, w = cm.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w]
    b = cm[..., 2] * (np.hypot((xx + 0.5) / w - 0.5, (yy + 0.5) / h - 0.5) < 0.3)
    s = float(b.sum())
    if s < 1:
        return 0.5, 0.5
    return float(((xx + 0.5) / w * b).sum() / s), float(((yy + 0.5) / h * b).sum() / s)


def bake_eye(src, mat):
    """Eyes (EYES_*_Parts / EYES_AO_*_Region, channel meaning checked by radial profile):
    ColorsMask  R sclera shading, G iris fibres, B limbal ring, A pupil-edge streaks
    RegionMask  R dome AO, G pupil + sclera (0 over the iris annulus), B iris+sclera, A soft iris disk
    The iris is re-centred (the masks are a few texels off from each other), drawn as a clean circle,
    and shrunk like the in-game parallax shader shows it."""
    t = mat["textures"]
    cm = src.dds(t.get("ColorsMask")) if t.get("ColorsMask") else None
    rm = src.dds(t.get("RegionMask")) if t.get("RegionMask") else None
    if cm is None or rm is None:
        return None
    rm = _resize_to(rm, cm.shape)
    cu, cv, R = _iris_geometry(rm)
    bu, bv = _ring_centre(cm)
    rm = _shrink_center(_shift(rm, 0.5 - cu, 0.5 - cv), IRIS_SCALE)
    cm = _shrink_center(_shift(cm, 0.5 - bu, 0.5 - bv), IRIS_SCALE)
    R *= IRIS_SCALE
    shade, fibres, outer = cm[..., 0], cm[..., 1], cm[..., 2]
    dome = rm[..., 0]
    h, w = shade.shape
    yy, xx = np.mgrid[0:h, 0:w]
    r = np.hypot((xx + 0.5) / w - 0.5, (yy + 0.5) / h - 0.5)
    a = np.clip((R - r) / 0.006 + 0.5, 0, 1)                 # iris coverage, soft 1.2% edge
    pupil = np.clip(rm[..., 1] * 1.4 - 0.2, 0, 1) * np.clip((R * 0.85 - r) / 0.01, 0, 1)
    rp = max(float(np.sqrt(pupil.sum() / np.pi) / w), 0.01)  # effective pupil radius
    iris = _vec(mat, "Eyes_IrisColour", [0.15, 0.08, 0.03])
    iris2 = _vec(mat, "Eyes_IrisSecondaryColour", iris * 0.6)
    sclera = _vec(mat, "Eyes_ScleraColour", [0.75, 0.7, 0.65])
    blood = _vec(mat, "Eyes_BloodColour", [0.36, 0.04, 0.04])
    sec_i = _scal(mat, "SecondaryColourIntensity", 1.0)
    edge = _scal(mat, "IrisEdgeStrength", 0.5)
    # sclera: preset colour (authored dark), soft shading, redder towards the corners
    redness = min(_scal(mat, "Redness", 1.0), 2.0)
    red = np.clip(np.clip((r * 2 - 0.55) * 1.5, 0, 1) * 0.25 * redness, 0, 0.8)
    col = sclera[None, None, :] * 1.6 * (0.8 + 0.2 * shade[..., None])
    col = col * (1 - red[..., None]) + blood[None, None, :] * red[..., None]
    # iris: secondary colour blooms out from the pupil, primary toward the rim, fibre detail,
    # and a dark limbal ring at the edge
    t_r = np.clip((r - rp) / max(R - rp, 1e-3), 0, 1)
    sec = (np.clip(1 - t_r / 0.55, 0, 1) ** 1.5 * sec_i)[..., None]
    ic = iris[None, None, :] * (1 - sec) + iris2[None, None, :] * sec
    ic = ic * (0.6 + 0.8 * fibres[..., None])
    limbal = np.clip(np.maximum(outer * 0.8, np.clip((t_r - 0.78) / 0.22, 0, 1)), 0, 1)
    ic = ic * (1 - (0.35 + 0.5 * edge) * limbal[..., None])
    ic = ic * (0.8 + 0.2 * dome[..., None]) * IRIS_GAIN
    # sclera blood vessels, denser toward the corners
    veins = _scal(mat, "BG_EyeVeins", 0.3)
    if veins > 0:
        vn = _vein_noise(h, w) * np.clip((r - R * 1.05) / 0.12, 0, 1) * np.clip(r * 3.0, 0, 1)
        vw = np.clip(vn * veins * 1.4, 0, 0.9)[..., None]
        col = col * (1 - vw) + blood[None, None, :] * 1.3 * vw
    # the lids shade the eyeball: the dome AO darkens the sclera toward the corners and edges, which keeps
    # it from reading as flat white paint
    col = col * (0.62 + 0.38 * np.clip(dome, 0, 1))[..., None]
    col = col * (1 - a[..., None]) + ic * a[..., None]
    col = col * (1 - pupil[..., None] * 0.97)
    ones = np.ones((h, w), np.float32)
    # cornea: a lens bulging over the iris, so the light's highlight sits on the iris and moves with the gaze
    # (the game does this with a parallax/refraction shader; a normal map is the portable equivalent)
    rc = R * 1.08
    hgt = np.sqrt(np.clip(rc * rc - r * r, 0, None)) / rc
    gy, gx = np.gradient(hgt * 0.06 * w)
    nrm = np.stack([-gx, gy, np.ones_like(gx)], -1)
    nrm /= np.linalg.norm(nrm, axis=-1, keepdims=True)
    out = {"baseColor": np.concatenate([linear_to_srgb(col), ones[..., None]], -1),
           "orm": _orm(ones, np.where(r < rc, 0.04, 0.12).astype(np.float32), ones * 0),
           "normal": nrm * 0.5 + 0.5}
    glow = _scal(mat, "GlowBrightness", 0.0)
    gp = _scal(mat, "GlowBrightnessPupil", 0.0)
    if glow > 0 or gp > 0:
        gcol = _vec(mat, "Eyes_GlowColour", iris)
        if float(gcol.max()) < 1e-3:              # no authored glow colour: glow in the iris colour
            gcol = np.clip(iris / max(float(iris.max()), 1e-3), 0, 1)
        # in-game glowing eyes (Karlach, Wyll's patron moments) read as a lit iris, not a lamp: cap it
        gm = np.clip(a * (1 - pupil) * (1 - 0.6 * limbal) * (0.4 + 0.6 * fibres) * min(0.55, glow / 30.0)
                     + pupil * min(0.5, gp / 16.0), 0, 1)
        out["emissive"] = linear_to_srgb(gcol[None, None, :] * gm[..., None])
    return out


HIDDEN_SHADERS = ("Helper_Invisible",)


def bake_tearline(src, mat):
    """Thin wet film along the eyelids: near-white, mostly transparent, very glossy (alpha-blended)."""
    t = mat["textures"]
    m = src.dds(t.get("Alpha")) if t.get("Alpha") else None
    if m is None:
        m = np.full((64, 64, 4), 0.5, np.float32)
    a = np.clip(m[..., 0], 0, 1) * 0.35
    h, w = a.shape
    col = np.ones((h, w, 3), np.float32) * np.array([0.92, 0.86, 0.84], np.float32)
    ones = np.ones((h, w), np.float32)
    return {"baseColor": np.concatenate([col, a[..., None]], -1), "orm": _orm(ones, ones * 0.04, ones * 0)}


def _vein_noise(h, w, seed=7):
    """Branching vessel pattern: ridged multi-octave value noise."""
    rng = np.random.RandomState(seed)
    from PIL import Image
    acc = np.zeros((h, w), np.float32)
    amp, tot = 1.0, 0.0
    for octave in (6, 12, 24, 48):
        g = rng.rand(octave, octave).astype(np.float32)
        up = np.asarray(Image.fromarray(g, "F").resize((w, h), Image.BICUBIC))
        ridge = 1 - np.abs(up * 2 - 1)
        acc += ridge ** 6 * amp
        tot += amp
        amp *= 0.6
    return np.clip(acc / tot * 2.2, 0, 1)


def bake_material(src, mat):
    fam = mat.get("shader_family")
    shader = mat.get("shader") or ""
    vt_id = next(iter(mat.get("virtual_textures", {}).values()), None)
    if any(h in shader for h in HIDDEN_SHADERS):
        return {"hidden": True}
    if "Tearline" in shader:
        return bake_tearline(src, mat)
    if "VFX_Model_Unlit_Glow" in shader:
        return bake_heat_metal(src, mat)
    if "VFX_Cinematic_CHAR_SSS" in shader:
        # a skin shader with a heat layer on top (Karlach's body): bake the skin, then the glowing cracks
        m2 = _with_scalars(mat, isBody=1.0)
        m2["textures"] = {k: v for k, v in mat["textures"].items() if k.lower() != "glowmap"}
        out = bake_skin(src, m2, vt_id)
        if out is not None:
            emis, _m = _heat_glow(src, mat, out["baseColor"].shape)
            if emis is not None:
                out["emissive"] = linear_to_srgb(np.clip(emis, 0, 1))
        return out
    if fam in ("skin_head", "skin_body") or "Skin_Head" in shader:
        return bake_skin(src, mat, vt_id)
    if fam in ("hair", "scalp") or "LashesShadow" in shader:
        return bake_hair(src, mat)
    if fam == "eye":
        return bake_eye(src, mat)
    return bake_base(src, mat, vt_id)


def bake_recipe(gd, recipe, vt_dirs=(), max_size=2048, log=None):
    """-> {material key: {"baseColor": png, "normal": png, "orm": png} | {"hidden": True}}"""
    src = TextureSource(gd, recipe, vt_dirs, max_size)
    out = {}
    for key, mat in recipe["materials"].items():
        try:
            res = bake_material(src, mat)
        except Exception as e:
            recipe.setdefault("warnings", []).append("bake %s: %s" % (mat.get("name"), e))
            res = None
        if res is None:
            continue
        if res.get("hidden"):
            out[key] = res
            continue
        out[key] = {k: _png(v, "RGBA" if v.shape[-1] == 4 else "RGB") for k, v in res.items()}
        if log:
            log("  baked %-44s %s" % (mat.get("name", "")[:44], ", ".join(sorted(out[key]))))
    return out
