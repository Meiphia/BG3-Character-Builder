"""Assemble a recipe's GR2 parts into one skinned character scene (bind pose).

    scene = assemble(recipe, read_file)     # read_file(pak, path) -> bytes
    scene.bones  : [Bone]  merged skeleton (base skeleton + extra bones from parts)
    scene.meshes : [SceneMesh] LOD0 meshes, skinned to scene.bones, with recipe material keys
"""

import math
import re

from .gr2 import read_gr2, extract_meshes

HAND_BONES = {"Main Hand": "Dummy_R_Hand", "Off Hand": "Dummy_L_Hand",
              "Ranged": "Dummy_L_Hand", "Ranged Off Hand": "Dummy_R_Hand"}


# ---------------------------------------------------------------- matrices (column-major 4x4, column vectors)

def mat_identity():
    return [1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0]


def mat_mul(a, b):
    out = [0.0] * 16
    for c in range(4):
        for r in range(4):
            out[c * 4 + r] = sum(a[k * 4 + r] * b[c * 4 + k] for k in range(4))
    return out


def mat_from_trs(t, q, s3x3=None):
    x, y, z, w = q
    r = [1 - 2 * (y * y + z * z), 2 * (x * y + z * w), 2 * (x * z - y * w), 0,
         2 * (x * y - z * w), 1 - 2 * (x * x + z * z), 2 * (y * z + x * w), 0,
         2 * (x * z + y * w), 2 * (y * z - x * w), 1 - 2 * (x * x + y * y), 0,
         0, 0, 0, 1.0]
    if s3x3:
        # Granny ScaleShear is row-major 3x3; convert to column-major 4x4
        s = [s3x3[0], s3x3[3], s3x3[6], 0, s3x3[1], s3x3[4], s3x3[7], 0, s3x3[2], s3x3[5], s3x3[8], 0, 0, 0, 0, 1.0]
        r = mat_mul(r, s)
    r[12], r[13], r[14] = t
    return r


def mat_inverse_affine(m):
    a, b, c = m[0], m[4], m[8]
    d, e, f = m[1], m[5], m[9]
    g, h, i = m[2], m[6], m[10]
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    if abs(det) < 1e-20:
        return mat_identity()
    inv = 1.0 / det
    r00, r01, r02 = (e * i - f * h) * inv, (c * h - b * i) * inv, (b * f - c * e) * inv
    r10, r11, r12 = (f * g - d * i) * inv, (a * i - c * g) * inv, (c * d - a * f) * inv
    r20, r21, r22 = (d * h - e * g) * inv, (b * g - a * h) * inv, (a * e - b * d) * inv
    tx, ty, tz = m[12], m[13], m[14]
    return [r00, r10, r20, 0, r01, r11, r21, 0, r02, r12, r22, 0,
            -(r00 * tx + r01 * ty + r02 * tz), -(r10 * tx + r11 * ty + r12 * tz),
            -(r20 * tx + r21 * ty + r22 * tz), 1.0]


def mat_apply(m, p, w=1.0):
    x, y, z = p
    return [m[0] * x + m[4] * y + m[8] * z + m[12] * w,
            m[1] * x + m[5] * y + m[9] * z + m[13] * w,
            m[2] * x + m[6] * y + m[10] * z + m[14] * w]


def mat_to_trs(m):
    t = [m[12], m[13], m[14]]
    sx = math.sqrt(m[0] ** 2 + m[1] ** 2 + m[2] ** 2) or 1.0
    sy = math.sqrt(m[4] ** 2 + m[5] ** 2 + m[6] ** 2) or 1.0
    sz = math.sqrt(m[8] ** 2 + m[9] ** 2 + m[10] ** 2) or 1.0
    r = [m[0] / sx, m[1] / sx, m[2] / sx, m[4] / sy, m[5] / sy, m[6] / sy, m[8] / sz, m[9] / sz, m[10] / sz]
    tr = r[0] + r[4] + r[8]
    if tr > 0:
        s = math.sqrt(tr + 1.0) * 2
        q = [(r[5] - r[7]) / s, (r[6] - r[2]) / s, (r[1] - r[3]) / s, 0.25 * s]
    elif r[0] > r[4] and r[0] > r[8]:
        s = math.sqrt(1.0 + r[0] - r[4] - r[8]) * 2
        q = [0.25 * s, (r[3] + r[1]) / s, (r[6] + r[2]) / s, (r[5] - r[7]) / s]
    elif r[4] > r[8]:
        s = math.sqrt(1.0 + r[4] - r[0] - r[8]) * 2
        q = [(r[3] + r[1]) / s, 0.25 * s, (r[7] + r[5]) / s, (r[6] - r[2]) / s]
    else:
        s = math.sqrt(1.0 + r[8] - r[0] - r[4]) * 2
        q = [(r[6] + r[2]) / s, (r[7] + r[5]) / s, 0.25 * s, (r[1] - r[3]) / s]
    return t, q, [sx, sy, sz]


# ---------------------------------------------------------------- scene types

class Bone(object):
    __slots__ = ("name", "parent", "local", "world", "slot")

    def __init__(self, name, parent, local):
        self.name, self.parent, self.local, self.world = name, parent, local, None
        self.slot = None              # appearance slot that brought this bone (wing bones -> "Wings")


class SceneMesh(object):
    def __init__(self, mesh, part, material):
        self.name = mesh.name
        self.mesh = mesh
        self.part = part              # recipe part dict
        self.material = material      # recipe material key
        self.joints = []              # per-vertex 4 scene-bone indices
        self.weights = []


class Scene(object):
    def __init__(self):
        self.bones = []
        self.by_name = {}
        self.meshes = []
        self.warnings = []

    def add_bone(self, name, parent, local):
        b = Bone(name, parent, local)
        b.world = mat_mul(self.bones[parent].world, local) if parent >= 0 else local
        self.by_name[name] = len(self.bones)
        self.bones.append(b)
        return self.by_name[name]

    def bone_world(self, name):
        i = self.by_name.get(name)
        return self.bones[i].world if i is not None else None


def _bone_local(b):
    tr = b.get("Transform") or b.get("LocalTransform")
    if not tr:
        return mat_identity()
    return mat_from_trs(tr["translation"], tr["rotation"], tr["scale_shear"])


def _merge_skeleton(scene, gskel):
    """Add bones of a GR2 skeleton that the scene doesn't have yet (parents first)."""
    bones = gskel.get("Bones") or []
    for b in bones:
        name = b.get("Name")
        if name in scene.by_name:
            continue
        p = b.get("ParentIndex", -1)
        parent = scene.by_name.get(bones[p]["Name"], -1) if p >= 0 else -1
        scene.add_bone(name, parent, _bone_local(b))


def _skeleton_world(gskel):
    bones = gskel.get("Bones") or []
    world = []
    for b in bones:
        loc = _bone_local(b)
        p = b.get("ParentIndex", -1)
        world.append(mat_mul(world[p], loc) if p >= 0 else loc)
    return {b["Name"]: w for b, w in zip(bones, world)}


def _object_mesh_name(object_id):
    # "TIF_F_NKD_Head_B.TIF_F_NKD_Head_B_Mesh.0" -> "TIF_F_NKD_Head_B_Mesh"
    parts = (object_id or "").split(".")
    return parts[1] if len(parts) >= 3 else (parts[-1] if parts else None)


def assemble(recipe, read_file, log=None, loader=None):
    scene = Scene()
    cache = {}

    def load(pak, path):
        if loader:
            return loader(pak, path)
        key = (pak, path)
        if key not in cache:
            cache[key] = read_gr2(read_file(pak, path))
        return cache[key]

    sk = recipe.get("skeleton")
    if sk and sk.get("pak"):
        root = load(sk["pak"], sk["path"])
        for s in root.get("Skeletons") or []:
            _merge_skeleton(scene, s)
    if not scene.bones:
        scene.add_bone("Root", -1, mat_identity())

    # skin regions hidden by equipped items (NakedBody vertex color G channel codes)
    codes = recipe.get("body_mask_codes") or {}
    hidden = set()
    for part in recipe["parts"]:
        if part.get("kind") == "equipment":
            for slot in part.get("mask_slots") or []:
                c = codes.get(str(slot).lower())
                if c is not None:
                    hidden.add(c)
    scene.hidden_codes = hidden
    hidden_names = set()
    for part in recipe["parts"]:
        if part.get("kind") == "equipment":
            hidden_names.update(_key(x) for x in part.get("mask_slots") or [])

    proxies = {}
    for part in recipe["parts"]:
        g = part.get("gr2") or {}
        if not g.get("pak"):
            continue
        if part.get("kind") == "appearance" and _part_hidden(part, hidden_names):
            continue
        try:
            root = load(g["pak"], g["path"])
        except Exception as e:
            scene.warnings.append("%s: %s" % (g.get("path"), e))
            continue
        meshes = _meshes_cached(root)
        skels = root.get("Skeletons") or []
        is_weapon = part.get("kind") == "equipment" and part.get("slot") in HAND_BONES
        attach_xf = None
        if is_weapon and skels:
            # line the weapon's Dummy_Attachment up with the character's hand bone
            wworld = _skeleton_world(skels[0])
            hand = scene.bone_world(part.get("sheath") or HAND_BONES[part["slot"]])
            if hand is None and part.get("sheath"):          # skeleton without that sheath point
                part["sheath"] = None
                hand = scene.bone_world(HAND_BONES[part["slot"]])
            att = wworld.get("Dummy_Attachment")
            if hand is not None and att is not None:
                attach_xf = mat_mul(hand, mat_inverse_affine(att))
        elif part.get("attach_bone") and skels:
            # bone-attached accessories (piercings): their GR2 root sits at the origin
            target = scene.bone_world(part["attach_bone"])
            if target is not None:
                own = _skeleton_world(skels[0])
                rootw = own.get(skels[0]["Bones"][0]["Name"]) if skels[0].get("Bones") else None
                attach_xf = mat_mul(target, mat_inverse_affine(rootw or mat_identity()))
        part_world = {}
        nbones0 = len(scene.bones)
        ref = part.get("remap_reference")
        if not attach_xf and ref and skels:
            # remappable part (hair): authored for a reference body; move it by the difference between
            # that body and this character, keeping its socket bones' authored placement.
            try:
                ref_world = _skeleton_world(load(ref["pak"], ref["path"])["Skeletons"][0])
            except Exception as e:
                scene.warnings.append("remap reference %s: %s" % (ref.get("path"), e))
                ref_world = {}
            for s in skels:
                part_world.update(_merge_remapped(scene, s, ref_world, part.get("remap_scale") or 1.0))
        elif not attach_xf:
            for s in skels:
                _merge_skeleton(scene, s)
                part_world.update(_skeleton_world(s))

        for b in scene.bones[nbones0:]:
            b.slot = part.get("slot")
        used = set()
        for oi, obj in enumerate(part["objects"]):
            mname = _object_mesh_name(obj["object"])
            m = meshes.get(mname)
            if m is None:
                m = _fallback_mesh(meshes, mname, oi, len(part["objects"]), used)
                if m is not None:
                    scene.warnings.append("mesh %s not in %s; using %s" % (mname, g["path"], m.name))
            if m is not None:
                used.add(m.name)
            if m is None:
                scene.warnings.append("mesh %s not in %s" % (mname, g["path"]))
                continue
            shader = ((recipe.get("materials") or {}).get(obj.get("material")) or {}).get("shader") or ""
            mn = m.name or ""
            sibling = mn.endswith("_Cloth_Mesh") and (mn[:-len("_Cloth_Mesh")] + "_Mesh") in meshes
            if _is_physics_proxy(m) or sibling or ("_Cloth" in mn and "Engine_Default" in shader):
                # cloth-simulation proxy (flagged, or an unflagged "_Cloth" mesh on the engine's placeholder
                # material, like the hag's hair and dress proxies): drives the real mesh in-game, never rendered. Kept (in this
                # character's bind pose) to find which parts of the cloth hang free, for cloth bones.
                if recipe.get("cloth_bones") and not attach_xf:
                    try:
                        _rebind(m, scene, part_world)
                        proxies.setdefault(id(part), (part, []))[1].append(m)
                    except Exception:
                        pass
                continue
            if hidden and part.get("supports_mask") and 0 in m.colors:
                _apply_body_mask(m, hidden)
            sm = SceneMesh(m, part, obj["material"])
            n = len(m.positions)
            if attach_xf:
                bone = (part.get("sheath") or HAND_BONES.get(part.get("slot"))) if is_weapon else part.get("attach_bone")
                bi = scene.by_name.get(bone, 0)
                m.positions = [mat_apply(attach_xf, p) for p in m.positions]
                m.normals = [_norm3(mat_apply(attach_xf, v, 0.0)) for v in m.normals]
                m.tangents = [_norm3(mat_apply(attach_xf, v[:3], 0.0)) + [v[3]] for v in m.tangents]
                sm.joints = [[bi, 0, 0, 0]] * n
                sm.weights = [[1.0, 0, 0, 0]] * n
            else:
                _rebind(m, scene, part_world)
                remap = [scene.by_name.get(b, 0) for b in m.bone_bindings]
                if m.bone_indices and m.bone_weights:
                    sm.joints, sm.weights = _remap_weights(remap, m.bone_indices, m.bone_weights)
                else:  # rigid: whole mesh follows its first bound bone
                    bi = remap[0] if remap else 0
                    sm.joints = [[bi, 0, 0, 0]] * n
                    sm.weights = [[1.0, 0, 0, 0]] * n
            scene.meshes.append(sm)
            if obj.get("material_left"):
                _split_left(scene, sm, obj["material_left"])
    if recipe.get("cloth_bones") and proxies:
        try:
            _add_cloth_bones(scene, proxies)
        except Exception as e:
            scene.warnings.append("cloth bones skipped: %s" % e)
    if recipe.get("breast_bones"):
        try:
            _add_breast_bones(scene)
        except Exception as e:
            scene.warnings.append("chest bones skipped: %s" % e)
    for slot, k in (recipe.get("part_scale") or {}).items():
        try:
            if k and abs(float(k) - 1.0) > 1e-3:
                _scale_part(scene, slot, float(k))
        except Exception as e:
            scene.warnings.append("%s size skipped: %s" % (slot, e))
    try:
        _weld_neck(scene)
    except Exception as e:                  # cosmetic; never fail a build over it
        scene.warnings.append("neck blend skipped: %s" % e)
    _mirror_z(scene)
    return scene


def _split_left(scene, sm, left_material):
    """Heterochromia: both eyes are one mesh; give the character's left eye (+X in BG3 space,
    the model faces -Z) its own material."""
    import copy
    m = sm.mesh
    idx, pos = m.indices, m.positions
    keep, left = [], []
    for t in range(0, len(idx) - 2, 3):
        a, b, c = idx[t], idx[t + 1], idx[t + 2]
        (left if pos[a][0] + pos[b][0] + pos[c][0] > 0 else keep).extend((a, b, c))
    if not left or not keep:
        return
    lm = copy.copy(m)
    lm.indices = left
    lm.name = (m.name or "Eyes") + "_L"
    m.indices = keep
    ls = SceneMesh(lm, sm.part, left_material)
    ls.name = sm.name + "_L"
    ls.joints, ls.weights = sm.joints, sm.weights
    scene.meshes.append(ls)


_Z_ENTRIES = (2, 6, 8, 9, 14)   # column-major entries flipped by S*M*S with S = diag(1, 1, -1)


def _fallback_mesh(meshes, wanted, index, nobjects, used):
    """Mod visuals often point at a mesh name the GR2 doesn't use (a Blender-renamed copy, ".007"
    suffixes, a template ObjectID left over from the mesh they started from). Try a suffix-stripped
    match, then — when the file's renderable meshes line up with the visual's objects — go by order."""
    import re
    strip = lambda n: re.sub(r"(\.\d+)+$", "", n or "")
    for name, m in meshes.items():
        if name not in used and strip(name) == strip(wanted):
            return m
    pool = [m for name, m in meshes.items() if name not in used and not _is_physics_proxy(m)]
    if not pool:
        return None
    if nobjects == 1 or len(pool) == nobjects - index:
        return pool[0]
    return None


def _meshes_cached(root):
    """Extracted meshes are cached on the (cached) GR2 root; each build gets shallow copies. Nothing
    downstream mutates the vertex lists in place (they are always replaced), so sharing them is safe."""
    import copy
    cached = root.get("__bg3asm_meshes") if hasattr(root, "get") else None
    if cached is None:
        cached = extract_meshes(root)
        try:
            root["__bg3asm_meshes"] = cached
        except TypeError:
            pass
    return {m.name: copy.copy(m) for m in cached}


SCALE_ANCHOR = {"Horns": ("Head_M",), "Wings": ("Chest_M", "Spine2_M", "Spine1_M")}


def _scale_part(scene, slot, k):
    """Resize a part (horns, wings, tail) where it grows from the body: each side scales about the point of
    that side closest to its anchor bone, so horns stay planted in the skull and wings at the back.
    The part's own bones scale with it, so skinning and animation still line up."""
    import numpy as np
    anchor_bone = next((scene.bones[scene.by_name[n]] for n in SCALE_ANCHOR.get(slot, ()) if n in scene.by_name), None)
    if anchor_bone is None:
        return
    ab = np.array(anchor_bone.world[12:15])
    meshes = [sm for sm in scene.meshes if sm.part.get("slot") == slot]
    pts = [np.asarray(sm.mesh.positions, dtype=np.float64) for sm in meshes if sm.mesh.positions]
    if not pts:
        return
    allp = np.concatenate(pts)
    centre_x = ab[0]
    bases = {}
    for side in (1, -1):
        sel = allp[(allp[:, 0] - centre_x) * side >= 0] if slot != "Tail" else allp
        if len(sel):
            bases[side] = sel[((sel - ab) ** 2).sum(1).argmin()]

    def base_for(p):
        side = 1 if (p[..., 0] - centre_x) >= 0 else -1
        return bases.get(side, bases.get(-side))

    for sm in meshes:
        P = np.asarray(sm.mesh.positions, dtype=np.float64)
        if not len(P):
            continue
        out = P.copy()
        for side in (1, -1):
            msk = ((P[:, 0] - centre_x) * side >= 0) if slot != "Tail" else np.ones(len(P), bool)
            b = bases.get(side, bases.get(-side))
            out[msk] = b + (P[msk] - b) * k
            if slot == "Tail":
                break
        sm.mesh.positions = out.tolist()
    # the part's own bones (wing bones, tail segments) move with the mesh; rotations stay
    for i, b in enumerate(scene.bones):
        if b.slot != slot:
            continue
        w = list(b.world)
        base = base_for(np.array(w[12:15]))
        for j in range(3):
            w[12 + j] = float(base[j] + (w[12 + j] - base[j]) * k)
        b.world = w
        pw = scene.bones[b.parent].world if b.parent >= 0 else mat_identity()
        b.local = mat_mul(mat_inverse_affine(pw), w)


CLOTH_LEVELS = (0.14, 0.38, 0.62, 0.86, 1.0)      # chain joints down a hanging strip (last one is the tip)


def _components(indices, pos):
    """Connected pieces of a mesh (vertices welded by position), as a label per vertex."""
    import numpy as np
    key = np.round(np.asarray(pos) / 1e-4).astype(np.int64)
    _, weld = np.unique(key, axis=0, return_inverse=True)
    weld = weld.reshape(-1)
    parent = list(range(int(weld.max()) + 1))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a
    idx = weld[np.asarray(indices, dtype=np.int64)]
    for t in range(0, len(idx) - 2, 3):
        a, b, c = find(idx[t]), find(idx[t + 1]), find(idx[t + 2])
        if a != b:
            parent[b] = a
        if a != c:
            parent[find(c)] = a
    roots = np.array([find(i) for i in range(len(parent))])
    _, lab = np.unique(roots, return_inverse=True)
    return lab.reshape(-1)[weld]


def _add_cloth_bones(scene, proxies):
    """BG3 simulates capes, tabards, skirt panels and loincloths with a hidden cloth proxy; outside the game
    they hang rigid. For every free-hanging piece of a proxy, add bone chains (one per ~15 cm of width) down
    its length, then weight the real mesh onto them. Weights are worked out on the proxy and carried to the
    real mesh by a soft blend of the nearest proxy vertices, so neighbouring panels, seams and the two
    stacked sheets a cape is made of (outside + lining) all get the same smooth weights and never tear apart."""
    import numpy as np
    made = 0
    levels = np.array(CLOTH_LEVELS[:-1])
    for part, pmeshes in proxies.values():
        render = [sm for sm in scene.meshes if sm.part is part and sm.joints and sm.weights]
        if not render:
            continue
        P = np.concatenate([np.asarray(m.positions, dtype=np.float64) for m in pmeshes])
        lab = np.concatenate([_components(m.indices, m.positions) + 1000 * k for k, m in enumerate(pmeshes)])
        slot = (part.get("slot") or "Cloth").replace(" ", "")
        RV = [np.asarray(sm.mesh.positions, dtype=np.float64) for sm in render]
        K = min(6, len(P))
        # nearest proxy vertices of every render vertex (indices + distances), computed once
        knn = []
        for V in RV:
            idx = np.zeros((len(V), K), dtype=np.int64)
            dist = np.zeros((len(V), K))
            for a in range(0, len(V), 1500):
                d = ((V[a:a + 1500, None, :] - P[None, :, :]) ** 2).sum(-1)
                part_idx = np.argpartition(d, K - 1, axis=1)[:, :K] if K < len(P) else np.tile(np.arange(len(P)), (len(d), 1))
                idx[a:a + 1500] = part_idx
                dist[a:a + 1500] = np.sqrt(np.take_along_axis(d, part_idx, 1))
            knn.append((idx, dist))
        pv_blend = np.zeros(len(P))                    # 0 = stays on the body, 1 = fully on the chains
        pv_map = [None] * len(P)                       # chain bone -> weight (sums to 1) per proxy vertex
        for ci, comp in enumerate(np.unique(lab)):
            cidx = np.nonzero(lab == comp)[0]
            C = P[cidx]
            top, bot = C[:, 1].max(), C[:, 1].min()
            height = top - bot
            if height < 0.15 or len(C) < 12:
                continue                                    # too short to swing (cuffs, collars, belts)
            xz = C[:, [0, 2]]
            ctr = xz.mean(0)
            cov = np.cov((xz - ctr).T) if len(C) > 2 else np.eye(2)
            _w, vecs = np.linalg.eigh(cov)
            u = vecs[:, -1]
            sproj = (xz - ctr) @ u
            smin, smax = sproj.min(), sproj.max()
            width = smax - smin
            ncol = int(np.clip(round(width / 0.15), 1, 5))
            col_s = [smin + width * (j + 0.5) / ncol for j in range(ncol)]
            # the body bone the piece hangs from: dominant influence of the render mesh near its top band
            topset = set(cidx[C[:, 1] > top - height * 0.12].tolist())
            votes = {}
            for sm, (idx, dist) in zip(render, knn):
                near = np.nonzero(np.isin(idx[:, 0], list(topset)) & (dist[:, 0] < 0.1))[0]
                for vi in near[:400]:
                    for jn, wt in zip(sm.joints[vi], sm.weights[vi]):
                        votes[jn] = votes.get(jn, 0.0) + wt
            if not votes:
                continue
            root = max(votes, key=votes.get)
            chains = []
            for j, sc in enumerate(col_s):
                band = np.abs(sproj - sc) < max(width / ncol * 0.6, 0.03)
                prev, bones = root, []
                for li, lv in enumerate(CLOTH_LEVELS):
                    y = top - height * lv
                    near = band & (np.abs(C[:, 1] - y) < max(height * 0.08, 0.02))
                    pos = C[near].mean(0) if near.any() else np.array([ctr[0] + u[0] * sc, y, ctr[1] + u[1] * sc])
                    wm = mat_identity()
                    wm[12:15] = [float(x) for x in pos]
                    name = "Cloth_%s_%d_%d_%d" % (slot, ci, j, li)
                    if name in scene.by_name:
                        break
                    local = mat_mul(mat_inverse_affine(scene.bones[prev].world), wm)
                    bi = scene.add_bone(name, prev, local)
                    scene.bones[bi].slot = "Cloth"
                    bones.append(bi)
                    prev = bi
                if len(bones) == len(CLOTH_LEVELS):
                    chains.append(bones)
            if not chains:
                continue
            made += 1
            for k_, pi in enumerate(cidx):
                t = float(np.clip((top - C[k_, 1]) / height, 0, 1))
                bl = float(np.clip((t - 0.1) / 0.25, 0, 1))
                pv_blend[pi] = bl * bl * (3 - 2 * bl)
                cf = float(np.clip((sproj[k_] - col_s[0]) / max(width / ncol, 1e-6), 0, ncol - 1)) if ncol > 1 else 0.0
                c0 = int(np.floor(cf)); c1 = min(c0 + 1, len(chains) - 1); cw = cf - c0
                c0 = min(c0, len(chains) - 1)
                lf = float(np.interp(t, levels, np.arange(len(levels))))
                l0 = int(np.floor(lf)); l1 = min(l0 + 1, len(levels) - 1); lw = lf - l0
                mp = {}
                for cc, a in ((c0, 1 - cw), (c1, cw)):
                    for ll, b2 in ((l0, 1 - lw), (l1, lw)):
                        if a * b2 > 0:
                            bone = chains[cc][ll]
                            mp[bone] = mp.get(bone, 0.0) + a * b2
                pv_map[pi] = mp
        if not pv_blend.any():
            continue
        # carry the proxy weights over to the real mesh: inverse-distance blend of the nearest proxy vertices
        for sm, (idx, dist) in zip(render, knn):
            wk = 1.0 / (dist + 0.03) ** 2          # soft: sheets ~1 cm apart get (nearly) the same mix
            wk /= wk.sum(1, keepdims=True)
            B = (wk * pv_blend[idx]).sum(1)
            todo = np.nonzero(B > 0.01)[0]
            if not len(todo):
                continue
            joints, weights = list(sm.joints), list(sm.weights)
            for vi in todo:
                new = {}
                for pi, w_ in zip(idx[vi], wk[vi]):
                    bl = pv_blend[pi]
                    if bl <= 0 or pv_map[pi] is None:
                        continue
                    for bone, x in pv_map[pi].items():
                        new[bone] = new.get(bone, 0.0) + w_ * bl * x
                keep = 1.0 - float(B[vi])
                for jn, wt in zip(joints[vi], weights[vi]):
                    if wt > 0:
                        new[jn] = new.get(jn, 0.0) + wt * keep
                top4 = sorted(new.items(), key=lambda kv: -kv[1])[:4]
                tot = sum(x for _, x in top4) or 1.0
                joints[vi] = [b_ for b_, _ in top4] + [0] * (4 - len(top4))
                weights[vi] = [x / tot for _, x in top4] + [0.0] * (4 - len(top4))
            sm.joints, sm.weights = joints, weights
    if made:
        scene.warnings.append("cloth bones added to %d hanging piece(s)" % made)


def _add_breast_bones(scene):
    """BG3 rigs have no breast bones, so chests stay rigid in any animation or physics outside the game.
    Add Breast_L / Breast_R (pivot on the chest wall, under Chest_M) with a _Tip child at the front of the
    breast, and move the skin weights of the breast (body and everything worn over it, so clothes follow)
    onto them with a smooth falloff. Springs/jiggle in Blender, Unity, VRM etc. then just work."""
    import numpy as np
    parent_name = next((n for n in ("Chest_M", "Spine2_M") if n in scene.by_name), None)
    if not parent_name or "Breast_L" in scene.by_name:
        return
    pi = scene.by_name[parent_name]
    C = np.array(scene.bones[pi].world[12:15])
    body = next((sm for sm in scene.meshes if sm.part.get("kind") == "body"
                 and re.search(r"_Body_[A-Z]_Mesh$", sm.mesh.name or "")), None)
    if body is None:
        return
    P = np.asarray(body.mesh.positions, dtype=np.float64)
    # the model faces -Z: the breast's most forward point on each side, around chest height
    band = (P[:, 1] > C[1] - 0.2) & (P[:, 1] < C[1] + 0.1)
    tips = {}
    for side, sign in (("L", 1), ("R", -1)):
        m = band & (P[:, 0] * sign > 0.03) & (P[:, 0] * sign < 0.2)
        if m.sum() < 20:
            return
        cand = P[m]
        tips[side] = cand[cand[:, 2].argmin()]
    depth = C[2] - (tips["L"][2] + tips["R"][2]) / 2       # how far the chest sticks out past the chest bone
    if depth < 0.06:                                           # flat chest: nothing to move
        return
    radius = float(np.clip(depth * 0.95, 0.07, 0.14))
    new_bones = {}
    for side in ("L", "R"):
        tip = tips[side]
        base = tip + np.array([0.0, 0.01, radius * 0.75])      # pivot sits back on the chest wall
        def add(name, parent, pos):
            w = mat_identity()
            w[12:15] = [float(x) for x in pos]
            local = mat_mul(mat_inverse_affine(scene.bones[parent].world), w)
            i = scene.add_bone(name, parent, local)
            scene.bones[i].slot = "Chest"
            return i
        bi = add("Breast_" + side, pi, base)
        add("Breast_%s_Tip" % side, bi, tip)
        new_bones[side] = (bi, base, tip)
    for sm in scene.meshes:
        if not sm.joints or not sm.weights or sm.part.get("attach_bone") or sm.part.get("kind") == "attachment":
            continue
        V = np.asarray(sm.mesh.positions, dtype=np.float64)
        if not len(V):
            continue
        joints, weights = list(sm.joints), list(sm.weights)
        changed = False
        for side, (bi, base, tip) in new_bones.items():
            centre = (base + tip) / 2
            d = np.linalg.norm((V - centre) / np.array([1.0, 1.15, 1.0]), axis=1)
            front = V[:, 2] < base[2] + 0.015
            w = np.clip((radius - d) / (radius * 0.75), 0, 1)
            w = w * w * (3 - 2 * w) * front
            for vi in np.nonzero(w > 0.01)[0]:
                js, ws = joints[vi], weights[vi]
                if bi in js:
                    continue
                k = float(w[vi])
                ws2 = [x * (1 - k) for x in ws]
                slot = int(np.argmin(ws2))
                js2 = list(js)
                js2[slot] = bi
                ws2[slot] = k
                tot = sum(ws2) or 1.0
                joints[vi], weights[vi] = js2, [x / tot for x in ws2]
                changed = True
        if changed:
            sm.joints, sm.weights = joints, weights


def _open_boundary(indices, n):
    """Vertices on edges used by a single triangle (positions-welded, since UV seams split vertices)."""
    from collections import Counter
    edges = Counter()
    for t in range(0, len(indices) - 2, 3):
        a, b, c = indices[t], indices[t + 1], indices[t + 2]
        for u, v in ((a, b), (b, c), (c, a)):
            edges[(u, v) if u < v else (v, u)] += 1
    return edges


def _weld_neck(scene, reach=0.035, blend=0.05):
    """Heads made for another race's body (an elf origin head on a tiefling body, say) don't meet the
    body's neck exactly, leaving a visible crease. Pull the head's open neck edge onto the body's neck
    edge and fade that correction a few centimetres up the neck; normals follow so the shading joins."""
    import numpy as np
    head = next((sm for sm in scene.meshes if sm.part.get("slot") == "Head"
                 and re.search(r"_Head_.*?_Mesh$|_Head_[A-Z]_Mesh$", sm.mesh.name or "")
                 and not re.search(r"Eyes|Ears|Mouth|Tearline|Lashes|Scalp|Eyeshadow", sm.mesh.name or "")), None)
    body = next((sm for sm in scene.meshes if sm.part.get("slot") == "Body" and sm.part.get("kind") == "body"
                 and re.search(r"_Body_[A-Z]_Mesh$", sm.mesh.name or "")), None)
    if not head or not body:
        return
    hp = np.asarray(head.mesh.positions, dtype=np.float64)
    bp = np.asarray(body.mesh.positions, dtype=np.float64)

    def boundary(m, pos):
        # weld by position first so UV-split vertices don't look like open edges
        key = np.round(pos / 1e-4).astype(np.int64)
        _, inv = np.unique(key, axis=0, return_inverse=True)
        inv = inv.reshape(-1)
        idx = inv[np.asarray(m.indices, dtype=np.int64)]
        edges = _open_boundary(idx.tolist(), len(pos))
        open_w = {v for (a, b), c in edges.items() if c == 1 for v in (a, b)}
        return np.array([i for i in range(len(pos)) if inv[i] in open_w], dtype=np.int64)

    hb = boundary(head.mesh, hp)
    bb = boundary(body.mesh, bp)
    if not len(hb) or not len(bb):
        return
    bbp = bp[bb]
    # nearest body-edge vertex for each head-edge vertex
    d2 = ((hp[hb][:, None, :] - bbp[None, :, :]) ** 2).sum(-1)
    near = d2.argmin(1)
    dist = np.sqrt(d2[np.arange(len(hb)), near])
    ok = dist < reach
    if ok.sum() < 8 or dist[ok].max() < 1e-4:
        return
    hb, near = hb[ok], near[ok]
    delta = bbp[near] - hp[hb]
    bn = np.asarray(body.mesh.normals, dtype=np.float64) if body.mesh.normals else None
    hn = np.asarray(head.mesh.normals, dtype=np.float64) if head.mesh.normals else None
    # fade the correction over the neck: each head vertex takes the delta of its nearest edge vertex
    edge_p = hp[hb]
    dd = ((hp[:, None, :] - edge_p[None, :, :]) ** 2).sum(-1)
    j = dd.argmin(1)
    w = np.clip(1.0 - np.sqrt(dd[np.arange(len(hp)), j]) / blend, 0.0, 1.0) ** 2
    new_p = hp + delta[j] * w[:, None]
    head.mesh.positions = new_p.tolist()
    if hn is not None and bn is not None and len(bn) == len(bp):
        target = bn[bb][near][j]
        nn = hn * (1 - w[:, None]) + target * w[:, None]
        nn /= np.maximum(np.linalg.norm(nn, axis=1, keepdims=True), 1e-9)
        head.mesh.normals = nn.tolist()


def _mirror_z(scene):
    """BG3 is left-handed and faces -Z; mirroring Z gives right-handed glTF facing +Z."""
    for b in scene.bones:
        for i in _Z_ENTRIES:
            b.local[i] = -b.local[i]
    for b in scene.bones:
        b.world = mat_mul(scene.bones[b.parent].world, b.local) if b.parent >= 0 else list(b.local)
    try:
        import numpy as np
    except ImportError:
        np = None
    for sm in scene.meshes:
        m = sm.mesh
        if np is not None:
            def flip(rows, sign):
                if rows is None or not len(rows):
                    return rows
                a = np.asarray(rows, dtype=np.float64)
                if a.ndim != 2 or a.shape[1] != len(sign):
                    return [list(r) for r in rows]
                return (a * np.asarray(sign)).tolist()
            m.positions = flip(m.positions, (1, 1, -1))
            m.normals = flip(m.normals, (1, 1, -1))
            m.tangents = flip(m.tangents, (1, 1, -1, -1))
            idx = np.asarray(m.indices, dtype=np.int64)
            n3 = len(idx) // 3 * 3
            if n3:
                t = idx[:n3].reshape(-1, 3)[:, [0, 2, 1]]
                m.indices = t.reshape(-1).tolist()
            continue
        m.positions = [[p[0], p[1], -p[2]] for p in m.positions]
        m.normals = [[n[0], n[1], -n[2]] for n in m.normals]
        m.tangents = [[t[0], t[1], -t[2], -t[3]] for t in m.tangents]
        idx = m.indices
        m.indices = [v for t in range(0, len(idx) - 2, 3) for v in (idx[t], idx[t + 2], idx[t + 1])]


def _norm3(v):
    l = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]) or 1.0
    return [v[0] / l, v[1] / l, v[2] / l]


def _remap_weights(remap, indices, weights):
    """Mesh-local bone indices -> scene bones, weights normalised (numpy; this runs on every vertex)."""
    try:
        import numpy as np
        bi = np.asarray(indices, dtype=np.int64)
        bw = np.asarray(weights, dtype=np.float64)
        table = np.asarray(list(remap) + [0], dtype=np.int64)
        bi = np.where((bi >= 0) & (bi < len(remap)), bi, len(remap))
        s = bw.sum(1, keepdims=True)
        w = np.where(s > 0, bw / np.where(s > 0, s, 1.0), np.array([1.0, 0, 0, 0])[:bw.shape[1]])
        return table[bi].tolist(), w.tolist()
    except (ImportError, ValueError):
        return ([[remap[i] if i < len(remap) else 0 for i in js] for js in indices],
                [_renorm(w) for w in weights])


def _renorm(w):
    s = sum(w)
    return [x / s for x in w] if s > 0 else [1.0, 0, 0, 0]


def _apply_body_mask(m, hidden_codes):
    """Drop triangles touching vertices whose region code (vertex color G) is covered by gear."""
    g = [int(round(c[1] * 255)) for c in m.colors[0]]
    idx = m.indices
    keep = []
    for t in range(0, len(idx) - 2, 3):
        a, b, c = idx[t], idx[t + 1], idx[t + 2]
        if g[a] in hidden_codes or g[b] in hidden_codes or g[c] in hidden_codes:
            continue
        keep.extend((a, b, c))
    m.indices = keep


def _key(s):
    return "".join(ch for ch in str(s).lower() if ch.isalnum())


def _is_physics_proxy(m):
    """LSLib MeshFlags: UserMeshProperties.Flags[2] bit 0x100 = ClothPhysics (simulation proxy)."""
    ud = (m.extended or {}).get("UserMeshProperties") or {}
    flags = ud.get("Flags") or []
    return len(flags) > 2 and bool(int(flags[2] or 0) & 0x100)


def _part_hidden(part, hidden_names):
    """Whole body add-ons (private parts, modesty leaf, nipple covers) hidden by worn gear."""
    names = {_key(part.get("slot") or ""), _key(part.get("visual_slot") or "")}
    if "nipplecover" in _key(part.get("name") or ""):
        names = {"nipplecovers"}
    return bool(names & hidden_names)


def _authored_in_character_space(m, scene, part_world):
    """Some mod exports carry a skeleton that doesn't match their own mesh (e.g. a Z-up Blender rig under
    Y-up vertices, so the head bone sits a metre away from the hair). The game draws those vertices as
    they are; so do we, when the mesh sits on the character's bones but nowhere near its own."""
    pos = m.positions
    if not pos:
        return False
    step = max(1, len(pos) // 2000)
    pts = pos[::step]
    c = [sum(p[i] for p in pts) / len(pts) for i in range(3)]
    own, ours = [], []
    for name in m.bone_bindings:
        bind = part_world.get(name)
        idx = scene.by_name.get(name)
        if bind is None or idx is None:
            continue
        w = scene.bones[idx].world
        own.append(math.sqrt(sum((bind[12 + i] - c[i]) ** 2 for i in range(3))))
        ours.append(math.sqrt(sum((w[12 + i] - c[i]) ** 2 for i in range(3))))
    if not own:
        return False
    return min(own) > 0.5 and min(ours) < 0.5


def _rebind(m, scene, part_world):
    """Deform a mesh from its file's bind pose onto the assembled skeleton (linear blend skinning).

    Parts authored on another skeleton (e.g. hair made on the reference male head, flagged
    NeedsSkeletonRemap) get refitted: shared bones take the character's positions and the
    part's extra bones (hair sockets) follow at their authored offsets. Identity for most parts.
    """
    if _authored_in_character_space(m, scene, part_world):
        return
    deltas = []
    any_move = False
    for name in m.bone_bindings:
        bind = part_world.get(name)
        idx = scene.by_name.get(name)
        if bind is None or idx is None:
            deltas.append(None)
            continue
        world = scene.bones[idx].world
        d = mat_mul(world, mat_inverse_affine(bind))
        cos_a = max(-1.0, min(1.0, (d[0] + d[5] + d[10] - 1.0) / 2.0))
        if math.degrees(math.acos(cos_a)) > 25.0:
            # the part's own skeleton has bones oriented differently from the game's (typical of mod
            # assets exported from Blender). The game skins such parts in model space, so keep only the
            # change in bone position, never swing the mesh around the joint.
            d = mat_identity()
            for i in range(3):
                d[12 + i] = world[12 + i] - bind[12 + i]
        if max(abs(d[i] - mat_identity()[i]) for i in range(16)) > 1e-5:
            any_move = True
        deltas.append(d)
    if not any_move:
        return
    n = len(m.positions)
    if m.bone_indices and m.bone_weights:
        joints, weights = m.bone_indices, m.bone_weights
    else:
        joints, weights = [[0, 0, 0, 0]] * n, [[1.0, 0, 0, 0]] * n
    try:
        _rebind_numpy(m, deltas, joints, weights)
        return
    except ImportError:
        pass
    new_p, new_n, new_t = [], [], []
    for vi in range(n):
        acc = [0.0] * 16
        tot = 0.0
        for j, w in zip(joints[vi], weights[vi]):
            if w <= 0 or j >= len(deltas) or deltas[j] is None:
                continue
            dj = deltas[j]
            for k in range(16):
                acc[k] += dj[k] * w
            tot += w
        if tot <= 0:
            new_p.append(m.positions[vi])
            if m.normals:
                new_n.append(m.normals[vi])
            if m.tangents:
                new_t.append(m.tangents[vi])
            continue
        acc = [a / tot for a in acc]
        new_p.append(mat_apply(acc, m.positions[vi]))
        if m.normals:
            new_n.append(_norm3(mat_apply(acc, m.normals[vi], 0.0)))
        if m.tangents:
            t = m.tangents[vi]
            new_t.append(_norm3(mat_apply(acc, t[:3], 0.0)) + [t[3]])
    m.positions = new_p
    if m.normals:
        m.normals = new_n
    if m.tangents:
        m.tangents = new_t


def _rebind_numpy(m, deltas, joints, weights):
    """Vectorised version of the per-vertex blend in _rebind (same maths)."""
    import numpy as np
    n = len(m.positions)
    nd = len(deltas)
    D = np.zeros((nd + 1, 16))
    valid = np.zeros(nd + 1, dtype=bool)
    for j, d in enumerate(deltas):
        if d is not None:
            D[j] = d
            valid[j] = True
    J = np.asarray(joints, dtype=np.int64).reshape(n, -1)
    W = np.asarray(weights, dtype=np.float64).reshape(n, -1)
    J = np.where((J >= 0) & (J < nd), J, nd)                  # out of range -> the empty slot
    W = np.where((W > 0) & valid[J], W, 0.0)
    tot = W.sum(axis=1)
    acc = np.einsum("nk,nkc->nc", W, D[J])
    has = tot > 0
    acc[has] /= tot[has, None]
    M = acc.reshape(n, 4, 4).transpose(0, 2, 1)               # column-major -> row matrices
    P = np.asarray(m.positions, dtype=np.float64)
    newP = np.einsum("nij,nj->ni", M[:, :3, :3], P) + M[:, :3, 3]
    newP[~has] = P[~has]
    m.positions = newP.tolist()

    def dirs(V):
        V = np.asarray(V, dtype=np.float64)
        out = np.einsum("nij,nj->ni", M[:, :3, :3], V[:, :3])
        out /= (np.linalg.norm(out, axis=1, keepdims=True) + 1e-12)
        out[~has] = V[~has, :3]
        return out
    if m.normals:
        m.normals = dirs(m.normals).tolist()
    if m.tangents:
        T = np.asarray(m.tangents, dtype=np.float64)
        m.tangents = np.concatenate([dirs(T), T[:, 3:4]], axis=1).tolist()


def _merge_remapped(scene, gskel, ref_world, scale=1.0):
    """Merge a remappable part's skeleton. Returns the bind worlds its mesh was authored against.
    scale: head-size ratio between this character and the body the part was authored on; the
    part's own bones are scaled about their anchor so e.g. female hair fits a strong-body head."""
    bones = gskel.get("Bones") or []
    file_world = _skeleton_world(gskel)
    authored = {}
    for b in bones:
        n = b["Name"]
        authored[n] = ref_world.get(n) if n in ref_world else file_world[n]
    for b in bones:
        n = b["Name"]
        if n in scene.by_name:
            continue
        # nearest ancestor that both the reference body and the character have
        p = b.get("ParentIndex", -1)
        anc = None
        while p >= 0:
            pn = bones[p]["Name"]
            if pn in ref_world and pn in scene.by_name:
                anc = pn
                break
            p = bones[p].get("ParentIndex", -1)
        if anc:
            delta = mat_mul(scene.bones[scene.by_name[anc]].world, mat_inverse_affine(ref_world[anc]))
            if abs(scale - 1.0) > 1e-3:
                a = ref_world[anc][12:15]
                sc = [scale, 0, 0, 0, 0, scale, 0, 0, 0, 0, scale, 0,
                      a[0] * (1 - scale), a[1] * (1 - scale), a[2] * (1 - scale), 1]
                delta = mat_mul(delta, sc)
            world = mat_mul(delta, file_world[n])
        else:
            world = file_world[n]
        pi = b.get("ParentIndex", -1)
        parent = scene.by_name.get(bones[pi]["Name"], -1) if pi >= 0 else -1
        local = mat_mul(mat_inverse_affine(scene.bones[parent].world), world) if parent >= 0 else world
        scene.add_bone(n, parent, local)
    return authored
