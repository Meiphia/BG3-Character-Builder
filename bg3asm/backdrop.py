"""Character-creator backdrop: the druid platform in the Astral Plane (level SYS_CC_I), read from the
user's own game files and baked once into a static, instanced .glb for the viewport.

The level stores placed objects (Scenery/Items) as root-template ids + transforms. Templates give a
VisualTemplate (a visual resource -> GR2 model + per-object materials), which is baked with the same
material pipeline as characters. The result is centred on the spot where Tav stands in the creator and
turned so the creator's close-up camera direction is the viewport's front (+Z).
"""

import json
import math
import os
import re
import struct
import time

from .assemble import _object_mesh_name
from .gltf import _Buf, FLOAT, UINT
from .gr2 import read_gr2, extract_meshes
from .lsf import load_resource
from .recipe import resolve_material, _empty_ov

LEVEL = "Mods/GustavDev/Levels/SYS_CC_I/"
LAYERS = ("Scenery/_merged.lsf", "Items/_merged.lsf")
TAV_SPOT = "TimelineSceneTrigger_CC_Intros_B"      # where Tav stands in the creator
TAV_CAMERA = "TIF_F_Camera_Closeup_A"              # the creator's close-up camera for that spot
SKIP = re.compile(r"^(VFX_|FX_|LIGHT|Light|Helper|Trigger|SND_|Decal|CINE_|TL_|Camera)", re.I)
MAX_DIST = 140.0                                   # metres from Tav; beyond that it is hidden by the sky anyway
VERSION = 2


def _transform(o):
    tr = next((c.attrs for c in o.children if c.name == "Transform"), None)
    if not tr:
        return None
    s = tr.get("Scale", 1.0)
    return tr.get("Position") or [0, 0, 0], tr.get("RotationQuat") or [0, 0, 0, 1], s if s else 1.0


def _read_level(gd, read, name):
    loc = gd.locate(LEVEL + name)
    if not loc:
        return []
    res = load_resource(read(loc[0], loc[1]))
    return [o for reg in res.regions.values() for o in reg.walk() if o.get("MapKey")]


def _root_templates(gd, read, wanted):
    """MapKey -> {VisualTemplate, ParentTemplateId, Name, Type} for the wanted ids (+ their parents)."""
    found = {}
    files = sorted(p for p in gd.files if re.match(r"^public/[^/]+/roottemplates/_merged\.lsf$", p))
    for low in files:
        loc = gd.locate(low)
        if not loc:
            continue
        try:
            res = load_resource(read(loc[0], loc[1]))
        except Exception:
            continue
        for reg in res.regions.values():
            for o in reg.children:
                k = o.get("MapKey")
                if k:
                    found[k] = {"VisualTemplate": o.get("VisualTemplate"), "Parent": o.get("ParentTemplateId"),
                                "Name": o.get("Name"), "Type": o.get("Type")}
    out = {}
    for k in wanted:
        seen, t = set(), k
        while t and t in found and t not in seen:
            seen.add(t)
            if found[t].get("VisualTemplate"):
                out[k] = found[t]
                break
            t = found[t].get("Parent")
    return out


def _yaw_to_front(dx, dz):
    """Angle about +Y (glTF space) that turns direction (dx, dz) to +Z."""
    for th in (math.atan2(-dx, dz), math.atan2(-dx, dz) + math.pi):
        x2 = dx * math.cos(th) + dz * math.sin(th)
        z2 = -dx * math.sin(th) + dz * math.cos(th)
        if abs(x2) < 1e-6 and z2 > 0:
            return th
    return math.atan2(dx, dz)


def _qmul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return [aw * bx + ax * bw + ay * bz - az * by, aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw, aw * bw - ax * bx - ay * by - az * bz]


def build(engine, out_path, max_tex=512, log=print):
    gd = engine.gd
    read = engine._read
    t0 = time.time()
    trig = {o.get("Name"): _transform(o) for o in _read_level(gd, read, "Triggers/_merged.lsf")}
    spot = trig.get(TAV_SPOT)
    cam = trig.get(TAV_CAMERA)
    if not spot:
        raise RuntimeError("character-creator level not found in the game files")
    sx, sy, sz = spot[0]
    # BG3 -> glTF: mirror Z. Front direction = from Tav towards the creator camera.
    dx, dz = (cam[0][0] - sx, -(cam[0][2] - sz)) if cam else (0.0, 1.0)
    yaw = _yaw_to_front(dx, dz)
    qyaw = [0.0, math.sin(yaw / 2), 0.0, math.cos(yaw / 2)]
    cy, syaw = math.cos(yaw), math.sin(yaw)

    placed = []
    for layer in LAYERS:
        for o in _read_level(gd, read, layer):
            name = o.get("Name") or ""
            if SKIP.match(name) or not o.get("TemplateName"):
                continue
            tr = _transform(o)
            if not tr:
                continue
            p = tr[0]
            if math.dist((p[0], p[2]), (sx, sz)) > MAX_DIST:
                continue
            placed.append((o.get("TemplateName"), name, tr))
    log("backdrop: %d placed objects" % len(placed))
    tpls = _root_templates(gd, read, {t for t, _n, _tr in placed})

    rec = {"materials": {}, "textures": {}, "virtual_textures": {}, "warnings": []}
    models = {}           # visual id -> [(Mesh, material key)]
    for tid, _n, _tr in placed:
        vid = (tpls.get(tid) or {}).get("VisualTemplate")
        if not vid or vid in models:
            continue
        v = gd.visuals.get(vid)
        models[vid] = []
        if not v or not v.get("source"):
            continue
        loc = gd.locate(v["source"])
        if not loc:
            continue
        try:
            meshes = {m.name: m for m in extract_meshes(read_gr2(read(loc[0], loc[1])))}
        except Exception as e:
            rec["warnings"].append("%s: %s" % (v["source"], e))
            continue
        for obj, mid, lod in v.get("objects") or []:
            if lod not in (0, None):
                continue
            mname = _object_mesh_name(obj)
            m = meshes.get(mname)
            if m is None or not m.positions or not m.indices:
                continue
            if mid and mid not in rec["materials"]:
                mat = resolve_material(gd, mid, _empty_ov(), rec, rec["warnings"])
                if mat:
                    rec["materials"][mid] = mat
            if mid in rec["materials"]:
                models[vid].append((m, mid))
    log("backdrop: %d models, %d materials — baking textures…" % (sum(1 for x in models.values() if x),
                                                                   len(rec["materials"])))
    images = engine._bake_images(rec, max_tex) or {}

    # ---------------------------------------------------------------- glTF (instanced static meshes)
    buf = _Buf()
    gl = {"asset": {"version": "2.0", "generator": "Baldur's Gate Assembler backdrop"}, "scene": 0,
          "scenes": [{"nodes": []}], "nodes": [], "meshes": [], "materials": [], "textures": [],
          "images": [], "samplers": [{"magFilter": 9729, "minFilter": 9987}]}
    mat_index = {}

    def material(key):
        if key in mat_index:
            return mat_index[key]
        info = rec["materials"][key]
        imgs = images.get(key) or {}
        if imgs.get("hidden"):
            mat_index[key] = None
            return None
        m = {"name": info.get("name") or key, "pbrMetallicRoughness": {"metallicFactor": 0.0, "roughnessFactor": 0.85}}
        sh = info.get("shader") or ""
        if "AlphaTest" in sh or "Foliage" in sh or "Vegetation" in sh or info.get("shader_family") == "hair":
            if not imgs.get("baseColor"):          # a leaf card without its texture is just a grey square
                mat_index[key] = None
                return None
            m["alphaMode"], m["alphaCutoff"], m["doubleSided"] = "MASK", 0.4, True
        for slot, gk in (("baseColor", "baseColorTexture"), ("normal", "normalTexture"),
                         ("orm", "metallicRoughnessTexture"), ("emissive", "emissiveTexture")):
            png = imgs.get(slot)
            if not png:
                continue
            gl["images"].append({"bufferView": buf.image(png), "mimeType": "image/png"})
            gl["textures"].append({"sampler": 0, "source": len(gl["images"]) - 1})
            ref = {"index": len(gl["textures"]) - 1}
            if gk == "normalTexture":
                m[gk] = ref
            elif gk == "emissiveTexture":
                m[gk], m["emissiveFactor"] = ref, [1.0, 1.0, 1.0]
            else:
                m["pbrMetallicRoughness"][gk] = ref
        if imgs.get("orm"):
            m["pbrMetallicRoughness"]["metallicFactor"] = 1.0
            m["pbrMetallicRoughness"]["roughnessFactor"] = 1.0
        if not imgs.get("baseColor"):
            m["pbrMetallicRoughness"]["baseColorFactor"] = [0.45, 0.43, 0.42, 1.0]
        gl["materials"].append(m)
        mat_index[key] = len(gl["materials"]) - 1
        return mat_index[key]

    mesh_index = {}

    def mesh_for(vid):
        if vid in mesh_index:
            return mesh_index[vid]
        prims = []
        for m, key in models.get(vid) or []:
            mi = material(key)
            if mi is None:
                continue
            n = len(m.positions)
            # BG3 is left-handed: mirror Z and flip the winding
            pos = [c for p in m.positions for c in (p[0], p[1], -p[2])]
            attrs = {"POSITION": buf.accessor(pos, FLOAT, n, "VEC3", 34962, minmax=True)}
            if len(m.normals) == n:
                attrs["NORMAL"] = buf.accessor([c for v in m.normals for c in (v[0], v[1], -v[2])], FLOAT, n, "VEC3", 34962)
            if len(m.tangents) == n:
                attrs["TANGENT"] = buf.accessor([c for v in m.tangents for c in (v[0], v[1], -v[2], -v[3])],
                                                FLOAT, n, "VEC4", 34962)
            if 0 in m.uvs and len(m.uvs[0]) == n:
                attrs["TEXCOORD_0"] = buf.accessor([c for uv in m.uvs[0] for c in uv], FLOAT, n, "VEC2", 34962)
            idx = m.indices
            tri = [x for t in range(0, len(idx) - 2, 3) if max(idx[t:t + 3]) < n for x in (idx[t], idx[t + 2], idx[t + 1])]
            if not tri:
                continue
            prims.append({"attributes": attrs, "material": mi,
                          "indices": buf.accessor(tri, UINT, len(tri), "SCALAR", 34963)})
        if not prims:
            mesh_index[vid] = None
            return None
        gl["meshes"].append({"name": gd.visuals.get(vid, {}).get("name") or vid, "primitives": prims})
        mesh_index[vid] = len(gl["meshes"]) - 1
        return mesh_index[vid]

    count = 0
    for tid, name, (p, q, s) in placed:
        vid = (tpls.get(tid) or {}).get("VisualTemplate")
        mi = mesh_for(vid) if vid else None
        if mi is None:
            continue
        # BG3 -> glTF (mirror Z), then centre on Tav and turn the creator camera to +Z
        x, y, z = p[0] - sx, p[1] - sy, -(p[2] - sz)
        tx, tz = x * cy + z * syaw, -x * syaw + z * cy
        if 0.4 < tz < 9.0 and abs(tx) < 3.2 and y < 4.0:
            continue        # keep the strip between the viewport camera and Tav clear (the game's camera is closer)
        rq = _qmul(qyaw, [-q[0], -q[1], q[2], q[3]])
        sc = s if isinstance(s, (int, float)) else 1.0
        gl["nodes"].append({"name": name, "mesh": mi, "translation": [tx, y, tz], "rotation": rq,
                            "scale": [sc, sc, sc]})
        gl["scenes"][0]["nodes"].append(len(gl["nodes"]) - 1)
        count += 1

    if not gl["images"]:
        for k in ("textures", "images", "samplers"):
            gl.pop(k)
    gl["bufferViews"], gl["accessors"] = buf.views, buf.accessors
    while len(buf.data) % 4:
        buf.data.append(0)
    gl["buffers"] = [{"byteLength": len(buf.data)}]
    gl["extras"] = {"bg3asm_backdrop": VERSION}
    js = json.dumps(gl, separators=(",", ":")).encode()
    while len(js) % 4:
        js += b" "
    tmp = out_path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(js) + 8 + len(buf.data)))
        f.write(struct.pack("<II", len(js), 0x4E4F534A))
        f.write(js)
        f.write(struct.pack("<II", len(buf.data), 0x004E4942))
        f.write(buf.data)
    os.replace(tmp, out_path)
    log("backdrop: %d instances of %d meshes, %.1f MB, %.0fs" % (count, len(gl["meshes"]),
                                                                 os.path.getsize(out_path) / 1e6, time.time() - t0))
    return out_path, rec["warnings"]
