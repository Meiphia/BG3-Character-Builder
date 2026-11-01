"""Granny animation curves -> sampled bone rotations, for the viewport idle loop.

Curve formats follow Granny's CurveDataHeader.Format enum (decoding as in LSLib, MIT):
  2 DaIdentity, 4 D3Constant32f, 5 D4Constant32f, 1 DaK32fC32f, 0 DaKeyframes32f,
  8 D4nK16uC15u, 9 D4nK8uC7u (quantised unit quaternions), 10 D3K16uC16u, 11 D3K8uC8u
Curves are evaluated by interpolating the control points at their knots (what LSLib's exporter
does too); for a looping idle that is visually indistinguishable from the full B-spline.

Rotations are used for every bone (they transfer between body proportions); for the root bone the
change in position relative to the first frame is kept too, so jumps, crouches and steps still move.
"""

import math
import re
import struct

from .gr2 import read_gr2

_SCALE = [1.4142135, 0.70710677, 0.35355338, 0.35355338, 0.35355338, 0.17677669, 0.17677669, 0.17677669,
          -1.4142135, -0.70710677, -0.35355338, -0.35355338, -0.35355338, -0.17677669, -0.17677669, -0.17677669]
_OFFSET = [-0.70710677, -0.35355338, -0.53033006, -0.17677669, 0.17677669, -0.17677669, -0.088388346, 0.0,
           0.70710677, 0.35355338, 0.53033006, 0.17677669, -0.17677669, 0.17677669, 0.088388346, -0.0]


def _qnorm(q):
    l = math.sqrt(sum(c * c for c in q)) or 1.0
    return [c / l for c in q]


def _quant_quat(cd, a, b, c, bits):
    """D4n*: A = sign bit + data, B/C top bits = index of the dropped component, 15/7-bit data."""
    top = 1 << (bits - 1)
    mask = top - 1
    sel = int(cd["ScaleOffsetTableEntries"])
    q = 1.0 / mask
    scales = [_SCALE[(sel >> (4 * i)) & 15] * q for i in range(4)]
    offsets = [_OFFSET[(sel >> (4 * i)) & 15] for i in range(4)]
    s1 = (((b & top) >> (bits - 2)) | (c >> (bits - 1))) & 3
    s2, s3, s4 = (s1 + 1) & 3, (s1 + 2) & 3, (s1 + 3) & 3
    da = (a & mask) * scales[s2] + offsets[s2]
    db = (b & mask) * scales[s3] + offsets[s3]
    dc = (c & mask) * scales[s4] + offsets[s4]
    dd = math.sqrt(max(0.0, 1 - (da * da + db * db + dc * dc)))
    if a & top:
        dd = -dd
    f = [0.0] * 4
    f[s2], f[s3], f[s4], f[s1] = da, db, dc, dd
    return f


def _trunc_float(u):
    return struct.unpack("<f", struct.pack("<I", (int(u) & 0xFFFF) << 16))[0]


def decode_curve(curve):
    """-> (knots, values) or ("const", value) or None for identity."""
    cd = (curve or {}).get("CurveData")
    if not cd:
        return None
    hdr = next((v for k, v in cd.items() if k.startswith("CurveDataHeader")), None) or {}
    fmt = hdr.get("Format")
    if fmt == 2:
        return None
    if fmt in (4, 5):
        return "const", list(cd["Controls"])
    if fmt in (8, 9):
        kc = cd["KnotsControls"]
        n = len(kc) // 4
        inv = cd["OneOverKnotScale"] or 1.0
        knots = [kc[i] / inv for i in range(n)]
        bits = 16 if fmt == 8 else 8
        vals = [_quant_quat(cd, kc[n + 3 * i], kc[n + 3 * i + 1], kc[n + 3 * i + 2], bits) for i in range(n)]
        return knots, vals
    if fmt in (10, 11):
        kc = cd["KnotsControls"]
        n = len(kc) // 4
        inv = _trunc_float(cd["OneOverKnotScaleTrunc"]) or 1.0
        sc, of = cd["ControlScales"], cd["ControlOffsets"]
        knots = [kc[i] / inv for i in range(n)]
        vals = [[kc[n + 3 * i + j] * sc[j] + of[j] for j in range(3)] for i in range(n)]
        return knots, vals
    if fmt == 1:
        knots, ctr = list(cd["Knots"]), list(cd["Controls"])
        if not knots:
            return None
        dim = len(ctr) // len(knots)
        return knots, [ctr[i * dim:(i + 1) * dim] for i in range(len(knots))]
    if fmt == 0:
        dim = cd.get("Dimension") or 4
        ctr = list(cd.get("Controls") or [])
        k = len(ctr) // dim
        return [float(i) for i in range(k)], [ctr[i * dim:(i + 1) * dim] for i in range(k)]
    return None


def _sample(dec, t, is_quat):
    if dec is None:
        return None
    if dec[0] == "const":
        return dec[1]
    knots, vals = dec
    if not knots:
        return None
    if t <= knots[0]:
        return vals[0]
    if t >= knots[-1]:
        return vals[-1]
    lo, hi = 0, len(knots) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if knots[mid] <= t:
            lo = mid
        else:
            hi = mid
    span = knots[hi] - knots[lo] or 1.0
    u = (t - knots[lo]) / span
    a, b = vals[lo], vals[hi]
    if is_quat and sum(x * y for x, y in zip(a, b)) < 0:
        b = [-x for x in b]
    out = [x + (y - x) * u for x, y in zip(a, b)]
    return _qnorm(out) if is_quat else out


def load_animation(data, fps=30.0):
    """GR2 bytes -> {"duration", "times", "rot": {bone: [[x,y,z,w], ...]}}"""
    root = read_gr2(data)
    anims = root.get("Animations") or []
    if not anims:
        raise ValueError("no animation in file")
    an = anims[0]
    dur = float(an.get("Duration") or 0) or 1.0
    n = max(2, int(round(dur * fps)) + 1)
    times = [dur * i / (n - 1) for i in range(n)]
    rot = {}
    for tg in an.get("TrackGroups") or []:
        for tr in tg.get("TransformTracks") or []:
            dec = decode_curve(tr.get("OrientationCurve"))
            if dec is None:
                continue
            seq = [_sample(dec, t, True) for t in times]
            prev = None
            for i, q in enumerate(seq):           # keep a continuous hemisphere for interpolation
                if prev is not None and sum(x * y for x, y in zip(prev, q)) < 0:
                    seq[i] = q = [-x for x in q]
                prev = q
            rot[tr["Name"]] = seq
    # root motion: position of the root bone relative to its first frame
    root = None
    for tg in an.get("TrackGroups") or []:
        for tr in tg.get("TransformTracks") or []:
            if tr.get("Name") == "Root_M":
                dec = decode_curve(tr.get("PositionCurve"))
                if dec is not None and dec[0] != "const":
                    pos = [_sample(dec, t, False) for t in times]
                    if all(p and len(p) == 3 for p in pos):
                        root = [[p[i] - pos[0][i] for i in range(3)] for p in pos]
    return {"duration": dur, "times": times, "rot": rot, "root": root}


# ---------------------------------------------------------------- picking an idle for a skeleton

_RIG_BY_RACE = {"DWR": "dwr", "GNO": "gno", "HFL": "hfl", "DGB": "dgb"}
_PLAYABLE = {"HUM", "ELF", "HEL", "TIF", "GTY", "DRW", "DWR", "GNO", "HFL", "HRC", "DGB"}
_PREFER = ("dflt_idle_still_peace_01.", "dflt_idle_neutral_peace_01_loop.", "idle_still_peace_01.",
           "dflt_idle_neutral_peace_01_transit.", "dflt_idle")


def idle_for_skeleton(gd, skeleton_name):
    """Path of a calm looping idle for a skeleton like 'TIF_FS_Base' (humanoids share HUM rigs)."""
    m = re.match(r"([A-Z]+)_(FS|MS|F|M)_", (skeleton_name or "") + "_")
    if not m:
        return None
    race, body = m.group(1), m.group(2).lower()
    rigs = []
    if race in _RIG_BY_RACE:
        rigs.append("%s_%s_rig" % (_RIG_BY_RACE[race], body.rstrip("s")))
    rigs.append("hum_%s_rig" % body)
    if body in ("fs", "ms"):
        rigs.append("hum_%s_rig" % body[0])
    for rig in rigs:
        cands = [f for f in gd.files if f.endswith(".gr2") and ("/%s/" % rig) in f and "/%s_" % rig in f
                 and "idle" in f and "additive" not in f and "_creatures" not in f and "crowd" not in f
                 and "cinematic" not in f and "combat" not in f]
        for pref in _PREFER:
            hit = sorted(f for f in cands if pref in f)
            if hit:
                return hit[0]
    return None


# ---------------------------------------------------------------- browsing every animation for a rig

CATEGORIES = {"pm": "Photo mode", "pose": "Poses", "idle": "Idle", "cmbt": "Combat",
              "combat": "Combat", "spl": "Spells", "loco": "Movement", "trav": "Movement", "cust": "Emotes",
              "reac": "Reactions", "diag": "Dialogue", "dial": "Dialogue", "stat": "Status", "die": "Death",
              "dead": "Death", "sit": "Sitting", "cc": "Character creator", "kneel": "Poses"}
WEAPONS = {"1hs": "one-handed", "2hs": "two-handed", "dws": "dual wield", "dwp": "dual wield", "pole": "polearm",
           "jav": "javelin", "sw": "sword & shield", "bow": "bow", "xbow": "crossbow", "stf": "staff",
           "unarmed": "unarmed", "2hb": "two-handed blunt", "1hb": "one-handed blunt", "mag": "caster"}


def _rig_dirs(gd):
    """Names of every animation rig folder in the game (…/<name>_rig/…)."""
    cache = getattr(gd, "_rig_dirs", None)
    if cache is None:
        cache = set()
        for f in gd.files:
            if f.endswith(".gr2") and "_rig/" in f:
                cache.add(f.rsplit("/", 2)[-2])
        gd._rig_dirs = cache
    return cache


def _creature_rigs(gd, skeleton_name):
    """Creature skeletons (HAG_Base, MFL_M_Base, IMP_Base, DEVA_HUM_M_Base, SLY_Bhaalspawn_Base…) -> their rig
    folder: the skeleton name without _Base, else any rig sharing its first word (sly_player_rig)."""
    if gd is None:
        return []
    n = (skeleton_name or "").lower()
    n = re.sub(r"_base$", "", n)
    dirs = _rig_dirs(gd)
    out = [n + "_rig"] if n + "_rig" in dirs else []
    head = n.split("_")[0]
    if not out and head:
        out = sorted(d for d in dirs if d.startswith(head + "_") and "_head_" not in d and "_wing_" not in d)[:1]
    if n.endswith("_chd") and "hum_chd_rig" in dirs and "hum_chd_rig" not in out:
        out.append("hum_chd_rig")         # tiefling/githyanki children share the human child animations
    return out


def _rigs(skeleton_name, gd=None):
    first = (skeleton_name or "").split("_")[0].upper()
    if first not in _PLAYABLE:
        return _creature_rigs(gd, skeleton_name)
    m = re.match(r"([A-Z]+)_(FS|MS|F|M)_", (skeleton_name or "") + "_")
    if not m:
        return _creature_rigs(gd, skeleton_name)          # children: hum_chd_rig, tif_chd_rig, gty_chd_rig
    race, body = m.group(1), m.group(2).lower()
    rigs = []
    if race in _RIG_BY_RACE:
        rigs.append("%s_%s_rig" % (_RIG_BY_RACE[race], body.rstrip("s")))
    rigs.append("hum_%s_rig" % body)
    if body in ("fs", "ms"):
        rigs.append("hum_%s_rig" % body[0])
    return rigs


def list_animations(gd, skeleton_name):
    """[{path, name, group, sub}] for everything the character's rig can play (cutscenes and additive
    layers left out). Body-specific rigs win over the shared ones they fall back to."""
    out, seen = [], set()
    for rig in _rigs(skeleton_name, gd):
        creature = rig not in ("hum_m_rig", "hum_f_rig", "hum_ms_rig", "hum_fs_rig") and not any(
            rig.startswith(r) for r in _RIG_BY_RACE.values())
        for f in gd.files:
            if not f.endswith(".gr2") or ("/%s/" % rig) not in f or ("/%s_" % rig) not in f:
                continue
            if "cinematic" in f or "additive" in f or ("_creatures" in f and not creature) or "/_construction/" in f:
                continue
            base = f.rsplit("/", 1)[1][:-4]
            rest = base.split("_rig_", 1)[-1]                 # e.g. dws_cmbt_skill_x_01_cast
            if rest in seen:
                continue
            seen.add(rest)
            parts = rest.split("_")
            weapon = WEAPONS.get(parts[0])
            cat = next((CATEGORIES[p] for p in parts[:4] if p in CATEGORIES), "Other")
            words = [p for p in parts if p not in CATEGORIES and p not in WEAPONS and p != "dflt"]
            name = " ".join(words).replace("  ", " ").strip().capitalize() or rest
            out.append({"path": f, "name": name, "group": cat, "sub": (weapon + " · " if weapon else "") + rest})
    order = ["Photo mode", "Poses", "Idle", "Emotes", "Dialogue", "Reactions", "Sitting", "Movement", "Combat",
             "Spells", "Status", "Death", "Character creator", "Other"]
    out.sort(key=lambda a: (order.index(a["group"]) if a["group"] in order else 99, a["name"]))
    return out


# ---------------------------------------------------------------- facial expression poses

FACE_EMOTIONS = ("Happy", "Sad", "Angry", "Fear", "Surprise", "Disgust", "Confusion", "Thinking", "Pain",
                 "Sleeping", "Dead", "Neutral")


def _face_rig_files(gd):
    rigs = {}
    for f in gd.files:
        m = re.search(r"/([a-z]+_[a-z]+_head_[a-z0-9_]*?_rig)/[^/]+_emot_pose_([a-z]+)_([a-z])\.gr2$", f)
        if m:
            rigs.setdefault(m.group(1), []).append((m.group(2), m.group(3), f))
    return rigs


def face_rig_for(gd, head_visual_name, skeleton_name=None):
    """Best face-pose rig for a head: its own (TIF_F_NKD_Head_A -> tif_f_head_a_rig), else the fullest
    set for the same race/body, else any human one of that body."""
    cache = getattr(gd, "_face_rigs", None)
    if cache is None:
        cache = gd._face_rigs = _face_rig_files(gd)
    n = (head_visual_name or "").lower()
    m = re.match(r"^([a-z]+)_(fs|ms|f|m)_nkd_head_(.+?)(_remap)?$", n)
    cands = []
    if m:
        race, body, head, remap = m.group(1), m.group(2), m.group(3), m.group(4) or ""
        cands += ["%s_%s_head_%s%s_rig" % (race, body, head, remap), "%s_%s_head_%s_rig" % (race, body, head),
                  "%s_%s_head_%s_remap_rig" % (race, body, head)]
        same = sorted((r for r in cache if r.startswith("%s_%s_head_" % (race, body))), key=lambda r: -len(cache[r]))
        cands += same
        cands += sorted((r for r in cache if r.startswith("hum_%s_head_" % body)), key=lambda r: -len(cache[r]))
    cands += sorted(cache, key=lambda r: -len(cache[r]))
    # a set is only usable with its Neutral pose (expressions are measured against it); the creator heads'
    # small 14-pose sets lack it, so they borrow the closest full set for their race and body
    full = lambda r: r in cache and any(e == "neutral" for e, _v, _p in cache[r]) and len(cache[r]) >= 24
    for r in cands:
        if full(r):
            return r
    for r in cands:
        if r in cache and len(cache[r]) >= 12:
            return r
    return None


def list_face_poses(gd, rig):
    out = []
    for emo, var, path in sorted(getattr(gd, "_face_rigs", {}).get(rig or "", [])):
        out.append({"path": path, "name": "%s %s" % (emo.capitalize(), var.upper()), "group": emo.capitalize()})
    order = [e.lower() for e in FACE_EMOTIONS]
    out.sort(key=lambda p: (order.index(p["group"].lower()) if p["group"].lower() in order else 99, p["name"]))
    return out


def _mid_pose(data):
    root = read_gr2(data)
    an = (root.get("Animations") or [None])[0]
    if not an:
        return {}, {}
    t = float(an.get("Duration") or 0) * 0.5
    rot, pos = {}, {}
    for tg in an.get("TrackGroups") or []:
        for tr in tg.get("TransformTracks") or []:
            r = _sample(decode_curve(tr.get("OrientationCurve")), t, True)
            p = _sample(decode_curve(tr.get("PositionCurve")), t, False)
            if r:
                rot[tr["Name"]] = _qnorm(list(r))
            if p and len(p) == 3:
                pos[tr["Name"]] = list(p)
    return rot, pos


def load_face_pose(gd, path):
    """{"rot": {bone: quat}, "dpos": {bone: [dx, dy, dz]}} for the face bones (BG3 space). Positions are
    taken relative to the rig's own Neutral pose, so they transfer onto heads with other proportions."""
    rot, pos = _mid_pose(gd.read_file(path))
    neutral = re.sub(r"_emot_pose_[a-z]+_[a-z]\.gr2$", "_emot_pose_neutral_a.gr2", path)
    nrot, npos = _mid_pose(gd.read_file(neutral)) if neutral != path and neutral in gd.files else (rot, pos)
    face = lambda b: b[:1].islower() and not b.startswith(("piercing", "socket", "dummy"))
    conj = lambda q: [-q[0], -q[1], -q[2], q[3]]

    def qmul(a, b):
        ax, ay, az, aw = a
        bx, by, bz, bw = b
        return [aw * bx + ax * bw + ay * bz - az * by, aw * by - ax * bz + ay * bw + az * bx,
                aw * bz + ax * by - ay * bx + az * bw, aw * bw - ax * bx - ay * by - az * bz]
    # rotation relative to the neutral face, applied on top of each head's own bind pose
    out = {"drot": {b: _qnorm(qmul(conj(nrot.get(b, [0, 0, 0, 1])), q)) for b, q in rot.items() if face(b)},
           "dpos": {}}
    for b, p in pos.items():
        if face(b) and b in npos:
            d = [p[i] - npos[b][i] for i in range(3)]
            if max(abs(c) for c in d) > 1e-5:
                out["dpos"][b] = d
    return out
