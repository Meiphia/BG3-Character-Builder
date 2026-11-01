"""ARKit (Perfect Sync) face shapes baked from BG3's face rig.

Every humanoid BG3 head is skinned to the same named face bones (jaw, upperlip_00..12, lowerlip_00..12,
eye lids, brows, cheeks, smile lines, nose, tongue). Each of Apple's 52 ARKit blendshapes is defined
here once as a small pose of those bones; bake() poses the assembled skeleton, skins every mesh the face
bones touch, and returns per-mesh vertex offsets that the glTF writer stores as named morph targets
(Blender imports them as shape keys with the ARKit names, which is what Perfect Sync tools look for).

Space: the assembled scene after _mirror_z, i.e. glTF space. +X = the character's left, +Y = up,
+Z = forward (out of the face). Distances in centimetres for a reference head (eye spacing 7 cm) and
scaled to the actual head; angles in degrees about the bone's own pivot.
"""

import math

import numpy as np

REF_EYE_SPACING = 0.0698      # metres, between eye_l and eye_r on the reference (Body 1) head

ARKIT_NAMES = (
    "eyeBlinkLeft", "eyeLookDownLeft", "eyeLookInLeft", "eyeLookOutLeft", "eyeLookUpLeft", "eyeSquintLeft",
    "eyeWideLeft", "eyeBlinkRight", "eyeLookDownRight", "eyeLookInRight", "eyeLookOutRight", "eyeLookUpRight",
    "eyeSquintRight", "eyeWideRight", "jawForward", "jawLeft", "jawRight", "jawOpen", "mouthClose",
    "mouthFunnel", "mouthPucker", "mouthLeft", "mouthRight", "mouthSmileLeft", "mouthSmileRight",
    "mouthFrownLeft", "mouthFrownRight", "mouthDimpleLeft", "mouthDimpleRight", "mouthStretchLeft",
    "mouthStretchRight", "mouthRollLower", "mouthRollUpper", "mouthShrugLower", "mouthShrugUpper",
    "mouthPressLeft", "mouthPressRight", "mouthLowerDownLeft", "mouthLowerDownRight", "mouthUpperUpLeft",
    "mouthUpperUpRight", "browDownLeft", "browDownRight", "browInnerUp", "browOuterUpLeft", "browOuterUpRight",
    "cheekPuff", "cheekSquintLeft", "cheekSquintRight", "noseSneerLeft", "noseSneerRight", "tongueOut",
)

# ---------------------------------------------------------------- pose vocabulary
# A pose is {bone: {"t": (x, y, z) cm, "r": [(axis, degrees), ...]}}; axes are "x", "y", "z" (world).


def _lip_side(i, side):
    """Weight of lip bone i (0 = right corner, 6 = centre, 12 = left corner) for one side, 1 at the corner."""
    w = (i - 6) / 6.0 if side == "l" else (6 - i) / 6.0
    return max(0.0, w)


def _lips(which, side=None, fn=None):
    """Bone names with weights. which: 'upper' / 'lower' / 'both'. side None = all, weight 1 (or fn)."""
    out = []
    for kind in (("upper", "lower") if which == "both" else (which,)):
        for i in range(13):
            w = 1.0 if side is None else _lip_side(i, side)
            if fn:
                w = fn(i) if side is None else w * fn(i)
            if w > 1e-3:
                out.append(("%slip_%02d" % (kind, i), w, i))
    return out


def _add(pose, bone, t=None, r=None, w=1.0):
    p = pose.setdefault(bone, {"t": [0.0, 0.0, 0.0], "r": []})
    if t:
        for k in range(3):
            p["t"][k] += t[k] * w
    if r:
        for axis, deg in r:
            p["r"].append((axis, deg * w))


def _centre(i):            # 1 at the middle of the lip, 0 at the corners
    return 1.0 - abs(i - 6) / 6.0


def _corner(i):            # 1 at the corners, 0 in the middle
    return abs(i - 6) / 6.0


def _sx(side, x):          # mirror an x offset for the right side
    return x if side == "l" else -x


LIDS_UP = ("upperlid_01", "upperlid_02", "upperlid_03")
LIDS_LO = ("lowerlid_01", "lowerlid_02", "lowerlid_03")


def _eye_shapes(side):
    S = "Left" if side == "l" else "Right"
    e = "eye_" + side
    sh = {}
    # lids pivot at the eye centre: rotating about +X swings the front of the lid down
    p = {}
    for b, a in zip(LIDS_UP, (42, 48, 43)):
        _add(p, "%s_%s" % (e, b), r=[("x", a)])
    for b, a in zip(LIDS_LO, (-9, -12, -9)):
        _add(p, "%s_%s" % (e, b), r=[("x", a)])
    sh["eyeBlink" + S] = p
    out = 1 if side == "l" else -1           # looking "out" turns the left eye towards +X
    sh["eyeLookOut" + S] = {e: {"t": [0, 0, 0], "r": [("y", 24 * out)]}}
    sh["eyeLookIn" + S] = {e: {"t": [0, 0, 0], "r": [("y", -24 * out)]}}
    p = {e: {"t": [0, 0, 0], "r": [("x", -18)]}}
    for b in LIDS_UP:
        _add(p, "%s_%s" % (e, b), r=[("x", -7)])
    sh["eyeLookUp" + S] = p
    p = {e: {"t": [0, 0, 0], "r": [("x", 18)]}}
    for b in LIDS_UP:
        _add(p, "%s_%s" % (e, b), r=[("x", 12)])
    for b in LIDS_LO:
        _add(p, "%s_%s" % (e, b), r=[("x", 5)])
    sh["eyeLookDown" + S] = p
    p = {}
    for b, a in zip(LIDS_LO, (-10, -13, -11)):
        _add(p, "%s_%s" % (e, b), r=[("x", a)])
    for b in LIDS_UP:
        _add(p, "%s_%s" % (e, b), r=[("x", 6)])
    _add(p, "cheek_upper_" + side, t=(0, 0.12, 0.05))
    sh["eyeSquint" + S] = p
    p = {}
    for b, a in zip(LIDS_UP, (-10, -13, -11)):
        _add(p, "%s_%s" % (e, b), r=[("x", a)])
    for b in LIDS_LO:
        _add(p, "%s_%s" % (e, b), r=[("x", 4)])
    sh["eyeWide" + S] = p
    return sh


def _mouth_side_shapes(side):
    S = "Left" if side == "l" else "Right"
    sh = {}
    # smile: corners up, out and back; cheeks and smile lines ride up
    p = {}
    for b, w, i in _lips("both", side):
        w = w ** 1.5
        _add(p, b, t=(_sx(side, 0.55), 0.85, -0.3), w=w)
    for k, (dx, dy) in enumerate(((0.15, 0.3), (0.3, 0.42), (0.32, 0.38), (0.2, 0.25))):
        _add(p, "smilelines_%s_%02d" % (side, k + 1), t=(_sx(side, dx), dy, 0.05))
    _add(p, "cheek_lower_" + side, t=(_sx(side, 0.12), 0.4, 0.12))
    _add(p, "cheek_middle_" + side, t=(0, 0.2, 0.08))
    _add(p, "cheek_upper_" + side, t=(0, 0.1, 0.04))
    sh["mouthSmile" + S] = p
    p = {}
    for b, w, i in _lips("both", side):
        _add(p, b, t=(_sx(side, 0.08), -0.48, 0.0), w=w ** 1.5)
    _add(p, "smilelines_%s_04" % side, t=(0, -0.15, 0))
    _add(p, "chin_" + side, t=(0, -0.08, 0))
    sh["mouthFrown" + S] = p
    p = {}
    for b, w, i in _lips("both", side):
        _add(p, b, t=(_sx(side, 0.15), 0.0, -0.35), w=w ** 2)
    _add(p, "smilelines_%s_03" % side, t=(0, 0, -0.12))
    sh["mouthDimple" + S] = p
    p = {}
    for b, w, i in _lips("both", side):
        _add(p, b, t=(_sx(side, 0.45), -0.22, -0.1), w=w ** 1.3)
    for b, w, i in _lips("lower", side):
        _add(p, b, t=(0, -0.15, 0), w=w)
    _add(p, "smilelines_%s_04" % side, t=(_sx(side, 0.12), -0.1, 0))
    sh["mouthStretch" + S] = p
    p = {}
    for b, w, i in _lips("upper", side):
        _add(p, b, t=(0, -0.2, -0.1), w=min(1.0, w * 1.6))
    for b, w, i in _lips("lower", side):
        _add(p, b, t=(0, 0.2, -0.1), w=min(1.0, w * 1.6))
    sh["mouthPress" + S] = p
    p = {}
    # ARKit: a strong arc that peaks midway between the centre and the corner, reaching a little past the
    # centre, so the lips part and the teeth show on that side
    peak = 9 if side == "l" else 3
    arc = lambda i: math.exp(-((i - peak) / 2.8) ** 2)
    for b, w, i in _lips("lower"):
        _add(p, b, t=(_sx(side, 0.08), -0.8, 0.08), w=arc(i))
    _add(p, "chin_" + side, t=(0, -0.15, 0))
    sh["mouthLowerDown" + S] = p
    p = {}
    for b, w, i in _lips("upper"):
        _add(p, b, t=(_sx(side, 0.08), 0.8, 0.08), w=arc(i))
    _add(p, "nose_wing_" + side, t=(0, 0.18, 0))
    _add(p, "smilelines_%s_01" % side, t=(0, 0.2, 0))
    _add(p, "smilelines_%s_02" % side, t=(0, 0.15, 0))
    sh["mouthUpperUp" + S] = p
    # brows
    p = {}
    for k, w in enumerate((1.0, 0.8, 0.55, 0.35)):
        _add(p, "brow_%s_%02d" % (side, k + 1), t=(_sx(side, -0.22), -0.85, 0.05), w=w)
    _add(p, "brow_m", t=(0, -0.3, 0))
    _add(p, "nose_nasal_%s_01" % side, t=(0, -0.08, 0))
    sh["browDown" + S] = p
    p = {}
    for k, w in enumerate((0.15, 0.4, 0.8, 1.0)):
        _add(p, "brow_%s_%02d" % (side, k + 1), t=(0, 1.0, 0.03), w=w)
    sh["browOuterUp" + S] = p
    p = {}
    _add(p, "cheek_upper_" + side, t=(0, 0.4, 0.1))
    _add(p, "cheek_outer_" + side, t=(0, 0.3, 0.05))
    _add(p, "cheek_middle_" + side, t=(0, 0.15, 0.05))
    for b, a in zip(LIDS_LO, (-5, -7, -6)):
        _add(p, "eye_%s_%s" % (side, b), r=[("x", a)])
    sh["cheekSquint" + S] = p
    p = {}
    _add(p, "nose_wing_" + side, t=(_sx(side, 0.12), 0.85, 0.03))
    _add(p, "nose_nasal_%s_01" % side, t=(0, 0.45, 0))
    _add(p, "nose_nasal_%s_02" % side, t=(0, 0.65, 0))
    _add(p, "smilelines_%s_01" % side, t=(0, 0.5, 0))
    _add(p, "cheek_upper_" + side, t=(0, 0.15, 0))
    for b, w, i in _lips("upper", side, fn=_centre):
        _add(p, b, t=(0, 0.18, 0), w=min(1.0, w * 2.5))
    _add(p, "brow_%s_01" % side, t=(0, -0.12, 0))
    sh["noseSneer" + S] = p
    return sh


def _central_shapes():
    sh = {}
    p = {"jaw": {"t": [0, -0.15, 0], "r": [("x", 21)]}}
    for b, w, i in _lips("lower"):          # lower lip rides the jaw; ease its corners back up
        c = _corner(i)
        _add(p, b, t=(-(i - 6) / 6.0 * 0.25, 1.9 * c ** 1.6 - 0.25 * _centre(i), 0))
    for b, w, i in _lips("upper"):          # upper lip arches up in the middle, corners drop a little
        c = _corner(i)
        _add(p, b, t=(-(i - 6) / 6.0 * 0.25, 0.3 * (1 - c * c) - 0.35 * c ** 2, 0.05))
    sh["jawOpen"] = p
    sh["jawForward"] = {"jaw": {"t": [0, 0, 0.55], "r": []}}
    sh["jawLeft"] = {"jaw": {"t": [0.5, 0, 0], "r": [("y", 4)]}}
    sh["jawRight"] = {"jaw": {"t": [-0.5, 0, 0], "r": [("y", -4)]}}
    p = {}
    for b, w, i in _lips("upper"):
        _add(p, b, t=(0, -1.0, 0.05), w=_centre(i) ** 0.6)
    for b, w, i in _lips("lower"):
        _add(p, b, t=(0, 1.0, 0.05), w=_centre(i) ** 0.6)
    sh["mouthClose"] = p
    p = {}
    for kind, dy in (("upper", 0.42), ("lower", -0.6)):
        for b, w, i in _lips(kind):
            x = (i - 6) / 6.0
            r = (1 - x * x) ** 0.7          # rounder opening, corners pulled well in
            _add(p, b, t=(-x * 1.4, dy * r, 0.5 + 0.3 * r))
    sh["mouthFunnel"] = p
    p = {}
    for kind, dy in (("upper", 0.14), ("lower", -0.16)):
        for b, w, i in _lips(kind):
            x = (i - 6) / 6.0
            _add(p, b, t=(-x * 1.05, dy * (1 - x * x), 0.6 + 0.25 * (1 - x * x)))
    for s in "lr":
        _add(p, "smilelines_%s_03" % s, t=(_sx(s, -0.15), 0, 0.1))
    sh["mouthPucker"] = p
    for name, dx in (("mouthLeft", 0.95), ("mouthRight", -0.95)):
        p = {}
        lead = "l" if dx > 0 else "r"
        for b, w, i in _lips("both"):
            # whole mouth slides over; the leading corner rides up and back into the cheek (ARKit reference)
            wl = _lip_side(i, lead) ** 1.5
            _add(p, b, t=(dx, 0.4 * wl - 0.06 * _lip_side(i, "r" if lead == "l" else "l"), -0.25 * wl))
        _add(p, "cheek_lower_" + lead, t=(_sx(lead, 0.15), 0.3, 0.05))
        _add(p, "cheek_middle_" + lead, t=(0, 0.15, 0.03))
        _add(p, "smilelines_%s_02" % lead, t=(0, 0.2, 0))
        _add(p, "smilelines_%s_03" % lead, t=(0, 0.25, 0))
        for s in "lr":
            for k in (2, 3, 4):
                _add(p, "smilelines_%s_%02d" % (s, k), t=(dx * 0.45, 0, 0))
        sh[name] = p
    p = {}
    for b, w, i in _lips("lower"):
        _add(p, b, t=(0, 0.45, -0.65), r=[("x", -58)], w=0.4 + 0.6 * _centre(i))
    sh["mouthRollLower"] = p
    p = {}
    for b, w, i in _lips("upper"):
        _add(p, b, t=(0, -0.45, -0.65), r=[("x", 58)], w=0.4 + 0.6 * _centre(i))
    sh["mouthRollUpper"] = p
    p = {}
    for b, w, i in _lips("lower"):
        _add(p, b, t=(0, 0.45, 0.22), w=0.5 + 0.5 * _centre(i))
    for s in "lr":
        _add(p, "chin_" + s, t=(0, 0.28, 0.08))
    sh["mouthShrugLower"] = p
    p = {}
    for b, w, i in _lips("upper"):
        _add(p, b, t=(0, 0.25, 0.12), w=0.5 + 0.5 * _centre(i))
    sh["mouthShrugUpper"] = p
    p = {}
    for s in "lr":
        _add(p, "brow_%s_01" % s, t=(_sx(s, -0.08), 1.0, 0.03))
        _add(p, "brow_%s_02" % s, t=(0, 0.6, 0.02))
        _add(p, "brow_%s_03" % s, t=(0, 0.2, 0))
    _add(p, "brow_m", t=(0, 0.75, 0.03))
    sh["browInnerUp"] = p
    p = {}
    for s in "lr":
        _add(p, "cheek_lower_" + s, t=(_sx(s, 0.9), -0.05, 0.35))
        _add(p, "cheek_middle_" + s, t=(_sx(s, 0.65), 0, 0.28))
        for k, w in ((2, 0.5), (3, 0.7), (4, 0.5)):
            _add(p, "smilelines_%s_%02d" % (s, k), t=(_sx(s, 0.3), 0, 0.15), w=w)
    for b, w, i in _lips("both"):
        _add(p, b, t=(0, 0, 0.12))
    sh["cheekPuff"] = p
    p = {}
    for b, w in (("tongue_l_01", 0.6), ("tongue_r_01", 0.6), ("tongue_l_02", 1.0), ("tongue_r_02", 1.0)):
        _add(p, b, t=(0, -0.45, 2.6), w=w)
    for b, w, i in _lips("lower"):
        _add(p, b, t=(0, -0.2, 0.1), w=_centre(i))
    sh["tongueOut"] = p
    return sh


# Lip-sync visemes (VRM aa/ih/ou/ee/oh, also named for Oculus/VRChat-style use). Each is its own shape, mixed
# here from the ARKit rig poses with weights tuned per mouth position, so speech reads cleanly instead of two
# ARKit shapes stacking (pucker + jaw used to cancel each other out).
VISEMES = {
    "viseme_aa": {"jawOpen": 0.72, "mouthUpperUpLeft": 0.18, "mouthUpperUpRight": 0.18,
                  "mouthLowerDownLeft": 0.22, "mouthLowerDownRight": 0.22, "mouthStretchLeft": 0.08,
                  "mouthStretchRight": 0.08},
    "viseme_ih": {"jawOpen": 0.3, "mouthStretchLeft": 0.42, "mouthStretchRight": 0.42, "mouthSmileLeft": 0.18,
                  "mouthSmileRight": 0.18, "mouthUpperUpLeft": 0.15, "mouthUpperUpRight": 0.15},
    "viseme_ou": {"mouthPucker": 0.9, "mouthFunnel": 0.3, "jawOpen": 0.22},
    "viseme_ee": {"jawOpen": 0.16, "mouthStretchLeft": 0.5, "mouthStretchRight": 0.5, "mouthSmileLeft": 0.42,
                  "mouthSmileRight": 0.42, "mouthUpperUpLeft": 0.25, "mouthUpperUpRight": 0.25,
                  "mouthLowerDownLeft": 0.25, "mouthLowerDownRight": 0.25},
    "viseme_oh": {"mouthFunnel": 0.8, "jawOpen": 0.48, "mouthPucker": 0.15},
}
VISEME_NAMES = tuple(VISEMES)


def _mix(defs, weights):
    """A pose that is the weighted sum of ARKit poses (translations and rotation angles scale linearly)."""
    pose = {}
    for name, w in weights.items():
        for bn, op in defs[name].items():
            _add(pose, bn, op.get("t"), op.get("r"), w)
    return pose


def shapes():
    sh = {}
    sh.update(_eye_shapes("l"))
    sh.update(_eye_shapes("r"))
    sh.update(_mouth_side_shapes("l"))
    sh.update(_mouth_side_shapes("r"))
    sh.update(_central_shapes())
    missing = [n for n in ARKIT_NAMES if n not in sh]
    assert not missing, missing
    out = {n: sh[n] for n in ARKIT_NAMES}
    for vn, mix in VISEMES.items():
        out[vn] = _mix(out, mix)
    return out


# ---------------------------------------------------------------- baking

def _rot(axis, deg):
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    if axis == "x":
        return np.array([[1, 0, 0], [0, c, -s], [0, s, c]])
    if axis == "y":
        return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])


def _m4(flat):
    return np.array(flat, dtype=np.float64).reshape(4, 4).T        # column-major flat -> row matrix


def has_face_rig(scene):
    need = ("jaw", "eye_l", "eye_r", "upperlip_06", "lowerlip_06")
    return all(n in scene.by_name for n in need)


def bake(scene, names=ARKIT_NAMES + VISEME_NAMES, min_offset=1e-5):
    """-> {SceneMesh: [(shape name, vertex indices, offsets float32[n, 3])]}, only non-zero shapes."""
    if not has_face_rig(scene):
        return {}
    bones = scene.bones
    nb = len(bones)
    bind = np.stack([_m4(b.world) for b in bones])                 # world bind matrices
    local = np.stack([_m4(b.local) for b in bones])
    parent = np.array([b.parent for b in bones])
    inv_bind = np.linalg.inv(bind)
    el, er = bind[scene.by_name["eye_l"]][:3, 3], bind[scene.by_name["eye_r"]][:3, 3]
    scale = (np.linalg.norm(el - er) / REF_EYE_SPACING) * 0.01      # cm on the reference head -> metres here

    face = set()                         # bones whose pose can move: face bones and their descendants
    head = scene.by_name.get("Head_M")
    for i, b in enumerate(bones):
        p = i
        while p >= 0:
            if p == head:
                face.add(i)
                break
            p = bones[p].parent

    meshes = []
    for sm in scene.meshes:
        J = np.array(sm.joints, dtype=np.int64)
        W = np.array(sm.weights, dtype=np.float64)
        if not len(J):
            continue
        touched = np.isin(J, list(face)) & (W > 0)
        rows = np.nonzero(touched.any(axis=1))[0]
        if not len(rows):
            continue
        P = np.array(sm.mesh.positions, dtype=np.float64)[rows]
        meshes.append((sm, rows, P, J[rows], W[rows]))
    if not meshes:
        return {}

    out = {}
    defs = shapes()
    for name in names:
        pose = defs[name]
        delta = np.tile(np.eye(4), (nb, 1, 1))                     # local-space delta per bone
        for bn, op in pose.items():
            i = scene.by_name.get(bn)
            if i is None:
                continue
            Wb = bind[i]
            pivot = Wb[:3, 3]
            R = np.eye(3)
            for axis, deg in op["r"]:
                R = _rot(axis, deg) @ R
            G = np.eye(4)                                          # world-space edit: rotate about pivot, then move
            G[:3, :3] = R
            G[:3, 3] = pivot - R @ pivot + np.array(op["t"]) * scale
            delta[i] = inv_bind[i] @ G @ Wb                         # same edit expressed in the bone's frame
        posed = np.empty_like(bind)
        for i in range(nb):
            pi = parent[i]
            base = posed[pi] @ local[i] if pi >= 0 else bind[i]
            posed[i] = base @ delta[i]
        skin = posed @ inv_bind                                    # bind -> posed, per bone
        for sm, rows, P, J, W in meshes:
            Ph = np.concatenate([P, np.ones((len(P), 1))], axis=1)
            moved = np.zeros_like(P)
            for k in range(J.shape[1]):
                M = skin[J[:, k]]                                  # (n, 4, 4)
                moved += W[:, k:k + 1] * np.einsum("nij,nj->ni", M, Ph)[:, :3]
            d = moved - P
            mask = np.abs(d).max(axis=1) > min_offset
            if mask.any():
                out.setdefault(sm, []).append((name, rows[mask], d[mask].astype(np.float32)))
    return out
