"""Write an assembled Scene as a binary glTF (.glb): skeleton, skinned meshes, materials, textures."""

import json
import struct
from array import array

from .assemble import mat_to_trs, mat_inverse_affine

FLOAT, USHORT, UINT = 5126, 5123, 5125


class _Buf(object):
    def __init__(self):
        self.data = bytearray()
        self.views = []
        self.accessors = []

    def _view(self, blob, target=None):
        while len(self.data) % 4:
            self.data.append(0)
        v = {"buffer": 0, "byteOffset": len(self.data), "byteLength": len(blob)}
        if target:
            v["target"] = target
        self.data += blob
        self.views.append(v)
        return len(self.views) - 1

    def accessor(self, flat, comp, count, typ, target=None, minmax=False):
        code = {FLOAT: "f", USHORT: "H", UINT: "I"}[comp]
        blob = array(code, flat).tobytes()
        acc = {"bufferView": self._view(blob, target), "componentType": comp, "count": count, "type": typ}
        if minmax:
            n = {"VEC3": 3, "VEC2": 2, "SCALAR": 1, "VEC4": 4}[typ]
            acc["min"] = [min(flat[i::n]) for i in range(n)]
            acc["max"] = [max(flat[i::n]) for i in range(n)]
        self.accessors.append(acc)
        return len(self.accessors) - 1

    def sparse_vec3(self, count, indices, values):
        """Morph-target accessor that stores only the moved vertices (glTF sparse accessor)."""
        idx = array("I", [int(i) for i in indices]).tobytes()
        flat = [float(c) for v in values for c in v]
        val = array("f", flat).tobytes()
        acc = {"componentType": FLOAT, "count": count, "type": "VEC3",
               "min": [min(0.0, min(flat[i::3])) for i in range(3)],
               "max": [max(0.0, max(flat[i::3])) for i in range(3)],
               "sparse": {"count": len(indices),
                          "indices": {"bufferView": self._view(idx), "componentType": UINT},
                          "values": {"bufferView": self._view(val)}}}
        self.accessors.append(acc)
        return len(self.accessors) - 1

    def image(self, blob):
        return self._view(blob)


def sample_pose(animation, t):
    """Bone rotations (+ root offset) of an animation at time t, for exporting a frozen pose."""
    times = animation["times"]
    if not times:
        return None
    t = max(times[0], min(times[-1], t))
    i = min(range(len(times)), key=lambda k: abs(times[k] - t))
    pose = {"rot": {b: seq[i] for b, seq in animation["rot"].items() if len(seq) == len(times)}}
    if animation.get("root") and len(animation["root"]) == len(times):
        pose["root"] = animation["root"][i]
    return pose


def _qmul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return [aw * bx + ax * bw + ay * bz - az * by, aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw, aw * bw - ax * bx - ay * by - az * bz]


def apply_face(scene, face, animation=None, pose=None):
    """Layer a face pose ({"drot", "dpos"} relative to neutral, BG3 space) onto an animation and/or a
    frozen pose. Returns (animation, pose) with the face bones set (constant over the clip)."""
    if not face:
        return animation, pose
    local = {}
    for b in scene.bones:
        t, q, _s = mat_to_trs(b.local)                 # glTF space (already mirrored)
        local[b.name] = (t, q)
    frot, fpos = {}, {}
    for bn, dq in face.get("drot", {}).items():
        if bn in local:
            m = [-dq[0], -dq[1], dq[2], dq[3]]            # mirror to glTF space
            q = _qmul(local[bn][1], m)
            frot[bn] = [-q[0], -q[1], q[2], q[3]]         # back to BG3 space, like animation tracks
    for bn, d in face.get("dpos", {}).items():
        if bn in local:
            fpos[bn] = d
    if animation:
        n = len(animation["times"])
        animation = dict(animation, rot=dict(animation["rot"]), trans=dict(animation.get("trans") or {}))
        for bn, q in frot.items():
            animation["rot"][bn] = [q] * n
        for bn, d in fpos.items():
            animation["trans"][bn] = [d] * n
    if pose is not None or not animation:
        pose = dict(pose or {"rot": {}})
        pose["rot"] = dict(pose.get("rot") or {}, **frot)
        pose["trans"] = dict(pose.get("trans") or {}, **fpos)
    return animation, pose


def _material_extensions(m, fam, info, imgs, used):
    """Physically richer materials for Blender, Unity, Slate and three.js (all optional glTF extensions that
    importers ignore when unsupported): soft skin specular, sheen on hair, a wet clear coat on eyes,
    and HDR strength for glowing parts. The material family also goes in extras so the Blender
    finishing script can add subsurface scattering, which glTF has no standard way to say."""
    ext = {}
    shader = info.get("shader") or ""
    if fam in ("skin_head", "skin_body"):
        pass        # plain PBR reads best in Blender; the sheen/specular layer made skin look orange and waxy
    elif fam in ("hair", "scalp"):
        ext["KHR_materials_sheen"] = {"sheenColorFactor": [0.9, 0.85, 0.8], "sheenRoughnessFactor": 0.35}
        ext["KHR_materials_specular"] = {"specularFactor": 0.6}
    elif fam == "eye":
        ext["KHR_materials_clearcoat"] = {"clearcoatFactor": 1.0, "clearcoatRoughnessFactor": 0.03}
        ext["KHR_materials_ior"] = {"ior": 1.376}
    if imgs.get("emissive") and fam not in ("skin_head", "skin_body"):
        ext["KHR_materials_emissive_strength"] = {"emissiveStrength": 3.0 if "VFX" in shader else 1.5}
    if ext:
        m["extensions"] = ext
        used.update(ext)
    m.setdefault("extras", {})["bga_family"] = fam or "base"


def _write_clip(gl, buf, scene, bone_node0, animation, seen_names):
    """One game animation as a glTF animation (several can go in one file; Blender makes each an action)."""
    if True:
        times = animation["times"]
        tacc = buf.accessor(times, FLOAT, len(times), "SCALAR", minmax=True)
        samplers, channels = [], []
        for i, b in enumerate(scene.bones):
            seq = animation["rot"].get(b.name)
            if not seq or len(seq) != len(times):
                continue
            # BG3 is mirrored on Z for glTF: q -> (-x, -y, z, w)
            flat = [c for q in seq for c in (-q[0], -q[1], q[2], q[3])]
            samplers.append({"input": tacc, "output": buf.accessor(flat, FLOAT, len(seq), "VEC4"),
                             "interpolation": "LINEAR"})
            channels.append({"sampler": len(samplers) - 1, "target": {"node": bone_node0 + i, "path": "rotation"}})
        for i, b in enumerate(scene.bones):            # face-bone offsets (face poses)
            seq = (animation.get("trans") or {}).get(b.name)
            if not seq or len(seq) != len(times):
                continue
            bt = mat_to_trs(b.local)[0]
            flat = [c for d in seq for c in (bt[0] + d[0], bt[1] + d[1], bt[2] - d[2])]
            samplers.append({"input": tacc, "output": buf.accessor(flat, FLOAT, len(seq), "VEC3"),
                             "interpolation": "LINEAR"})
            channels.append({"sampler": len(samplers) - 1, "target": {"node": bone_node0 + i, "path": "translation"}})
        root = animation.get("root")
        ri = next((i for i, b in enumerate(scene.bones) if b.name == "Root_M"), None)
        if root and ri is not None and len(root) == len(times):
            bt = mat_to_trs(scene.bones[ri].local)[0]
            flat = [c for d in root for c in (bt[0] + d[0], bt[1] + d[1], bt[2] - d[2])]
            samplers.append({"input": tacc, "output": buf.accessor(flat, FLOAT, len(root), "VEC3"),
                             "interpolation": "LINEAR"})
            channels.append({"sampler": len(samplers) - 1, "target": {"node": bone_node0 + ri, "path": "translation"}})
        if channels:
            name = animation.get("name") or "Idle"
            base, n = name, 2
            while name in seen_names:
                name = "%s.%d" % (base, n)
                n += 1
            seen_names.add(name)
            gl.setdefault("animations", []).append({"name": name, "samplers": samplers, "channels": channels})



def _write_heartbeat(gl, buf, mats, mat_index, images):
    """Karlach's engine beats (materials with Use_HeartBeat): a looping "Heartbeat" clip that animates their
    emissive strength through KHR_animation_pointer, so the pulse survives export (Blender 4.2+, three.js)."""
    targets = []
    for key, idx in mat_index.items():
        info = mats.get(key) or {}
        sc = info.get("scalar") or {}
        if float(sc.get("Use_HeartBeat") or 0) > 0 and ((images or {}).get(key) or {}).get("emissive"):
            targets.append((idx, float(sc.get("BPM") or 33.0)))
    if not targets:
        return
    import math as _m
    bpm = targets[0][1] or 33.0
    period = 60.0 / bpm
    n = 32
    times = [period * i / n for i in range(n + 1)]
    vals = []
    for t in times:
        ph = t / period
        beat = _m.exp(-((ph - 0.08) / 0.05) ** 2) + 0.6 * _m.exp(-((ph - 0.26) / 0.06) ** 2)
        vals.append(0.7 + 0.55 * beat)
    tacc = buf.accessor(times, FLOAT, len(times), "SCALAR", minmax=True)
    samplers, channels = [], []
    for idx, _bpm in targets:
        m = gl["materials"][idx]
        strength = ((m.get("extensions") or {}).get("KHR_materials_emissive_strength") or {}).get("emissiveStrength")
        if strength is None:
            out = [c for v in vals for c in (v, v, v)]
            pointer, typ = "/materials/%d/emissiveFactor" % idx, "VEC3"
            cnt = len(vals)
        else:
            out = [v * strength for v in vals]
            pointer, typ = "/materials/%d/extensions/KHR_materials_emissive_strength/emissiveStrength" % idx, "SCALAR"
            cnt = len(vals)
        samplers.append({"input": tacc, "output": buf.accessor(out, FLOAT, cnt, typ), "interpolation": "LINEAR"})
        channels.append({"sampler": len(samplers) - 1, "target": {"path": "pointer", "extensions": {
            "KHR_animation_pointer": {"pointer": pointer}}}})
    gl.setdefault("animations", []).append({"name": "Heartbeat", "samplers": samplers, "channels": channels})
    gl["extensionsUsed"] = sorted(set(gl.get("extensionsUsed", [])) | {"KHR_animation_pointer"})


def write_glb(scene, path, recipe=None, images=None, name="Character", animation=None, morphs=None, pose=None,
              heartbeat=False):
    """images: optional {material_key: {"baseColor": png_bytes, "normal": png_bytes, ...}}
    morphs: optional {SceneMesh: [(name, vertex indices, offsets[n, 3])]} -> sparse morph targets (shape keys)
    animation: optional {"name", "times": [...], "rot": {bone: [[x,y,z,w] (BG3 space), ...]}}"""
    buf = _Buf()
    gl = {"asset": {"version": "2.0", "generator": "Mei's BG3 Character Builder"},
          "scene": 0, "scenes": [{"name": name, "nodes": []}], "nodes": [], "meshes": [],
          "materials": [], "skins": [], "textures": [], "images": [], "samplers": [{}]}

    # ---- skeleton nodes
    bone_node0 = 0
    for i, b in enumerate(scene.bones):
        t, q, s = mat_to_trs(b.local)
        if pose and b.name in pose.get("rot", {}):         # frozen pose: posed nodes, bind-pose IBMs
            pq = pose["rot"][b.name]
            q = [-pq[0], -pq[1], pq[2], pq[3]]
        if pose and pose.get("root") and b.name == "Root_M":
            d = pose["root"]
            t = [t[0] + d[0], t[1] + d[1], t[2] - d[2]]
        if pose and b.name in (pose.get("trans") or {}):    # face bones: offset from the head's bind
            d = pose["trans"][b.name]
            t = [t[0] + d[0], t[1] + d[1], t[2] - d[2]]
        node = {"name": b.name, "translation": t, "rotation": q}
        if any(abs(x - 1.0) > 1e-4 for x in s):
            node["scale"] = s
        if getattr(b, "slot", None) in ("Wings", "Chest", "Cloth"):
            node["extras"] = {"bga_slot": b.slot}     # lets the viewer / VRM export find wing bones
        gl["nodes"].append(node)
    for i, b in enumerate(scene.bones):
        if b.parent >= 0:
            gl["nodes"][b.parent].setdefault("children", []).append(bone_node0 + i)
        else:
            gl["scenes"][0]["nodes"].append(bone_node0 + i)
    ibm = []
    for b in scene.bones:
        ibm.extend(mat_inverse_affine(b.world))
    skin = {"joints": list(range(len(scene.bones))),
            "inverseBindMatrices": buf.accessor(ibm, FLOAT, len(scene.bones), "MAT4")}
    roots = [i for i, b in enumerate(scene.bones) if b.parent < 0]
    if roots:
        skin["skeleton"] = roots[0]
    gl["skins"].append(skin)

    # ---- materials
    mat_index = {}
    used_ext = set()
    mats = (recipe or {}).get("materials", {})
    hidden = {k for k, v in (images or {}).items() if v.get("hidden")}
    scene.meshes = [sm for sm in scene.meshes if sm.material not in hidden]
    for sm in scene.meshes:
        key = sm.material
        if key in mat_index:
            continue
        info = mats.get(key, {})
        m = {"name": info.get("name") or str(key), "pbrMetallicRoughness": {"metallicFactor": 0.0, "roughnessFactor": 0.7},
             "extras": {"bg3_shader": info.get("shader"), "bg3_material": key}}
        fam = info.get("shader_family")
        if "Tearline" in (info.get("shader") or ""):
            m["alphaMode"] = "BLEND"
            m["doubleSided"] = True
        elif fam == "scalp":
            m["alphaMode"] = "BLEND"      # soft painted hairline, not cut-out cards
            m["doubleSided"] = True
        elif fam == "hair" or "AlphaTest" in (info.get("shader") or "") or "Lashes" in (info.get("shader") or ""):
            m["alphaMode"] = "MASK"
            m["alphaCutoff"] = 0.5
            # only shaders the game marks two-sided ("_2S"), hair and lashes; capes and cloth are modelled
            # as two coincident one-sided sheets that z-fight when both draw both sides
            sh = info.get("shader") or ""
            m["doubleSided"] = fam == "hair" or "_2S" in sh or "Lashes" in sh
        elif "_2S" in (info.get("shader") or ""):
            m["doubleSided"] = True
        imgs = (images or {}).get(key) or {}
        for slot, gltf_key in (("baseColor", "baseColorTexture"), ("normal", "normalTexture"),
                               ("orm", "metallicRoughnessTexture"), ("emissive", "emissiveTexture")):
            png = imgs.get(slot)
            if not png:
                continue
            gl["images"].append({"bufferView": buf.image(png), "mimeType": "image/png", "name": "%s_%s" % (m["name"], slot)})
            gl["textures"].append({"sampler": 0, "source": len(gl["images"]) - 1})
            ref = {"index": len(gl["textures"]) - 1}
            if gltf_key == "normalTexture":
                m["normalTexture"] = ref
            elif gltf_key == "emissiveTexture":
                m["emissiveTexture"] = ref
                m["emissiveFactor"] = [1.0, 1.0, 1.0]
            else:
                m["pbrMetallicRoughness"][gltf_key] = ref
        _material_extensions(m, fam, info, imgs, used_ext)
        if imgs.get("orm"):
            m["pbrMetallicRoughness"]["metallicFactor"] = 1.0
            m["pbrMetallicRoughness"]["roughnessFactor"] = 1.0
            m["occlusionTexture"] = dict(m["pbrMetallicRoughness"]["metallicRoughnessTexture"])
        gl["materials"].append(m)
        mat_index[key] = len(gl["materials"]) - 1

    # ---- meshes
    for sm in scene.meshes:
        m = sm.mesh
        n = len(m.positions)
        if not n or not m.indices:
            continue
        if max(m.indices) >= n or min(m.indices) < 0:      # drop triangles with bad indices
            idx = m.indices
            m.indices = [v for t in range(0, len(idx) - 2, 3) if all(0 <= x < n for x in idx[t:t + 3])
                         for v in idx[t:t + 3]]
            if not m.indices:
                continue
        flat = lambda rows: [c for r in rows for c in r]
        attrs = {"POSITION": buf.accessor(flat(m.positions), FLOAT, n, "VEC3", 34962, minmax=True)}
        if len(m.normals) == n:
            attrs["NORMAL"] = buf.accessor(flat(m.normals), FLOAT, n, "VEC3", 34962)
        if len(m.tangents) == n:
            attrs["TANGENT"] = buf.accessor(flat(m.tangents), FLOAT, n, "VEC4", 34962)
        for idx in sorted(m.uvs):
            if len(m.uvs[idx]) == n:
                attrs["TEXCOORD_%d" % idx] = buf.accessor(flat(m.uvs[idx]), FLOAT, n, "VEC2", 34962)
        if 0 in m.colors and len(m.colors[0]) == n:
            # BG3 vertex colours are masks (body region codes etc.); COLOR_0 would tint the
            # material in every viewer, so keep them as a custom attribute instead.
            attrs["_BG3_MASK"] = buf.accessor(flat(m.colors[0]), FLOAT, n, "VEC4", 34962)
        joints = [[j if w > 0 else 0 for j, w in zip(js, ws)] for js, ws in zip(sm.joints, sm.weights)]
        attrs["JOINTS_0"] = buf.accessor(flat(joints), USHORT, n, "VEC4", 34962)
        attrs["WEIGHTS_0"] = buf.accessor(flat(sm.weights), FLOAT, n, "VEC4", 34962)
        prim = {"attributes": attrs, "indices": buf.accessor(m.indices, UINT, len(m.indices), "SCALAR", 34963),
                "material": mat_index[sm.material]}
        mesh = {"name": m.name, "primitives": [prim],
                "extras": {"slot": sm.part.get("slot"), "option": sm.part.get("option")}}
        targets = (morphs or {}).get(sm)
        if targets:
            prim["targets"] = []
            for tname, idx, vals in targets:
                prim["targets"].append({"POSITION": buf.sparse_vec3(n, idx, vals)})
            mesh["weights"] = [0.0] * len(targets)
            mesh["extras"]["targetNames"] = [t[0] for t in targets]
        gl["meshes"].append(mesh)
        gl["nodes"].append({"name": m.name, "mesh": len(gl["meshes"]) - 1, "skin": 0})
        gl["scenes"][0]["nodes"].append(len(gl["nodes"]) - 1)

    clips = [a for a in (animation if isinstance(animation, (list, tuple)) else [animation]) if a and a.get("rot")]
    seen_names = set()
    for animation in clips:
        _write_clip(gl, buf, scene, bone_node0, animation, seen_names)
    if heartbeat:                       # exports only: the viewer animates the pulse itself
        _write_heartbeat(gl, buf, mats, mat_index, images)

    if not gl["images"]:
        for k in ("textures", "images", "samplers"):
            gl.pop(k)
    gl["bufferViews"] = buf.views
    gl["accessors"] = buf.accessors
    while len(buf.data) % 4:
        buf.data.append(0)
    if used_ext:
        gl["extensionsUsed"] = sorted(set(gl.get("extensionsUsed", [])) | used_ext)
    gl["buffers"] = [{"byteLength": len(buf.data)}]
    js = json.dumps(gl, separators=(",", ":")).encode("utf-8")
    while len(js) % 4:
        js += b" "
    total = 12 + 8 + len(js) + 8 + len(buf.data)
    with open(path, "wb") as f:
        f.write(struct.pack("<III", 0x46546C67, 2, total))
        f.write(struct.pack("<II", len(js), 0x4E4F534A))
        f.write(js)
        f.write(struct.pack("<II", len(buf.data), 0x004E4942))
        f.write(buf.data)
    return path
