"""Turn an assembled .glb into a VRM 1.0 avatar (VSeeFace, Warudo, VRoid tools, three-vrm, ...).

Adds the VRMC_vrm extension to the glb the exporter already writes:
  * humanoid: BG3 bone names -> VRM human bones (Root_M = hips, Shoulder_L = leftUpperArm, ...)
  * expressions: the 52 ARKit shapes (facerig.py) as custom expressions, plus the VRM presets
    (happy, angry, sad, relaxed, surprised, aa/ih/ou/ee/oh, blink...) mixed from them
  * lookAt driven by the eye bones, firstPerson hiding head meshes, tail spring bones (VRMC_springBone)
The model already faces +Z with +X as its left, which is what VRM 1.0 expects.
"""

import json
import struct

HUMAN_BONES = {
    "hips": "Root_M", "spine": "Spine1_M", "chest": "Spine2_M", "upperChest": "Chest_M", "neck": "Neck_M",
    "head": "Head_M", "jaw": "jaw", "leftEye": "eye_l", "rightEye": "eye_r",
    "leftUpperLeg": "Hip_L", "leftLowerLeg": "Knee_L", "leftFoot": "Ankle_L", "leftToes": "Toes_L",
    "rightUpperLeg": "Hip_R", "rightLowerLeg": "Knee_R", "rightFoot": "Ankle_R", "rightToes": "Toes_R",
    "leftShoulder": "Scapula_L", "leftUpperArm": "Shoulder_L", "leftLowerArm": "Elbow_L", "leftHand": "Wrist_L",
    "rightShoulder": "Scapula_R", "rightUpperArm": "Shoulder_R", "rightLowerArm": "Elbow_R", "rightHand": "Wrist_R",
}
for _s, _S in (("L", "left"), ("R", "right")):
    HUMAN_BONES.update({
        _S + "ThumbMetacarpal": "ThumbFinger1_" + _s, _S + "ThumbProximal": "ThumbFinger2_" + _s,
        _S + "ThumbDistal": "ThumbFinger3_" + _s,
        _S + "IndexProximal": "IndexFinger1_" + _s, _S + "IndexIntermediate": "IndexFinger2_" + _s,
        _S + "IndexDistal": "IndexFinger3_" + _s,
        _S + "MiddleProximal": "MiddleFinger1_" + _s, _S + "MiddleIntermediate": "MiddleFinger2_" + _s,
        _S + "MiddleDistal": "MiddleFinger3_" + _s,
        _S + "RingProximal": "RingFinger1_" + _s, _S + "RingIntermediate": "RingFinger2_" + _s,
        _S + "RingDistal": "RingFinger3_" + _s,
        _S + "LittleProximal": "PinkyFinger1_" + _s, _S + "LittleIntermediate": "PinkyFinger2_" + _s,
        _S + "LittleDistal": "PinkyFinger3_" + _s,
    })
REQUIRED = ("hips", "spine", "head", "leftUpperLeg", "leftLowerLeg", "leftFoot", "rightUpperLeg", "rightLowerLeg",
            "rightFoot", "leftUpperArm", "leftLowerArm", "leftHand", "rightUpperArm", "rightLowerArm", "rightHand")

# VRM preset expressions mixed from ARKit shapes: name -> {arkit shape: weight}
PRESETS = {
    "happy": {"mouthSmileLeft": 1, "mouthSmileRight": 1, "cheekSquintLeft": 0.6, "cheekSquintRight": 0.6,
              "eyeSquintLeft": 0.3, "eyeSquintRight": 0.3},
    "angry": {"browDownLeft": 1, "browDownRight": 1, "mouthFrownLeft": 0.5, "mouthFrownRight": 0.5,
              "noseSneerLeft": 0.4, "noseSneerRight": 0.4, "eyeSquintLeft": 0.3, "eyeSquintRight": 0.3},
    "sad": {"browInnerUp": 1, "mouthFrownLeft": 0.8, "mouthFrownRight": 0.8, "mouthShrugLower": 0.3},
    "relaxed": {"mouthSmileLeft": 0.45, "mouthSmileRight": 0.45, "eyeBlinkLeft": 0.15, "eyeBlinkRight": 0.15},
    "surprised": {"browInnerUp": 1, "browOuterUpLeft": 1, "browOuterUpRight": 1, "eyeWideLeft": 1,
                  "eyeWideRight": 1, "jawOpen": 0.55},
    "aa": {"jawOpen": 0.85},
    "ih": {"jawOpen": 0.25, "mouthStretchLeft": 0.6, "mouthStretchRight": 0.6},
    "ou": {"mouthPucker": 1, "jawOpen": 0.2},
    "ee": {"mouthStretchLeft": 0.8, "mouthStretchRight": 0.8, "jawOpen": 0.2, "mouthSmileLeft": 0.3,
           "mouthSmileRight": 0.3},
    "oh": {"mouthFunnel": 1, "jawOpen": 0.35},
    "blink": {"eyeBlinkLeft": 1, "eyeBlinkRight": 1},
    "blinkLeft": {"eyeBlinkLeft": 1},
    "blinkRight": {"eyeBlinkRight": 1},
}
MOUTH = ("aa", "ih", "ou", "ee", "oh")
EMOTION = ("happy", "angry", "sad", "relaxed", "surprised")
HEAD_SLOTS = ("Head", "Hair", "Horns", "Beard", "Helmet", "Piercing")


def _read_glb(data):
    if struct.unpack_from("<I", data, 0)[0] != 0x46546C67:
        raise ValueError("not a .glb")
    n = struct.unpack_from("<I", data, 12)[0]
    js = json.loads(data[20:20 + n])
    rest = data[20 + n:]
    binc = rest[8:8 + struct.unpack_from("<I", rest, 0)[0]] if len(rest) >= 8 else b""
    return js, binc


def _write_glb(js, binc):
    j = json.dumps(js, separators=(",", ":")).encode()
    while len(j) % 4:
        j += b" "
    b = bytes(binc)
    while len(b) % 4:
        b += b"\0"
    out = struct.pack("<III", 0x46546C67, 2, 12 + 8 + len(j) + 8 + len(b))
    out += struct.pack("<II", len(j), 0x4E4F534A) + j + struct.pack("<II", len(b), 0x004E4942) + b
    return out


def glb_to_vrm(data, name="Character", author="Mei's BG3 Character Builder user"):
    js, binc = _read_glb(data)
    nodes = js.get("nodes") or []
    by_name = {}
    for i, n in enumerate(nodes):
        by_name.setdefault(n.get("name"), i)
    human = {vb: {"node": by_name[bn]} for vb, bn in HUMAN_BONES.items() if bn in by_name}
    missing = [b for b in REQUIRED if b not in human]
    if missing:
        raise ValueError("this skeleton can't be mapped to a VRM humanoid (missing %s)" % ", ".join(missing))

    # morph targets per mesh node
    targets = {}            # arkit name -> [(node, index)]
    head_meshes, other_meshes = [], []
    for i, n in enumerate(nodes):
        if "mesh" not in n:
            continue
        m = js["meshes"][n["mesh"]]
        slot = (m.get("extras") or {}).get("slot")
        (head_meshes if slot in HEAD_SLOTS else other_meshes).append(i)
        for k, t in enumerate((m.get("extras") or {}).get("targetNames") or []):
            targets.setdefault(t, []).append((i, k))

    def binds(mix):
        out = []
        for shape, w in mix.items():
            for node, idx in targets.get(shape, []):
                out.append({"node": node, "index": idx, "weight": float(w)})
        return out

    preset = {}
    for pname, mix in PRESETS.items():
        if pname in MOUTH and ("viseme_" + pname) in targets:
            mix = {"viseme_" + pname: 1.0}         # the dedicated lip-sync shape
        b = binds(mix)
        if not b:
            continue
        e = {"morphTargetBinds": b, "isBinary": False}
        if pname in MOUTH:
            e["overrideMouth"] = "none"
            e["overrideBlink"] = "none"
        if pname in EMOTION:
            e["overrideBlink"] = "blend"
            e["overrideMouth"] = "blend"
        preset[pname] = e
    for pname in ("neutral", "lookUp", "lookDown", "lookLeft", "lookRight"):
        preset.setdefault(pname, {"isBinary": False})
    custom = {t: {"morphTargetBinds": binds({t: 1}), "isBinary": False} for t in sorted(targets)}

    # eye offset from the head bone, for lookAt
    def world_pos(idx):
        parents = {}
        for i, n in enumerate(nodes):
            for c in n.get("children") or []:
                parents[c] = i
        chain = []
        while idx is not None:
            chain.append(idx)
            idx = parents.get(idx)
        m = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]
        for i in reversed(chain):
            n = nodes[i]
            t = n.get("translation") or [0, 0, 0]
            x, y, z, w = n.get("rotation") or [0, 0, 0, 1]
            s = n.get("scale") or [1, 1, 1]
            r = [[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                 [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                 [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]]
            loc = [[r[a][0] * s[0], r[a][1] * s[1], r[a][2] * s[2], t[a]] for a in range(3)] + [[0, 0, 0, 1]]
            m = [[sum(m[a][k] * loc[k][b] for k in range(4)) for b in range(4)] for a in range(4)]
        return [m[0][3], m[1][3], m[2][3]]

    look = {"type": "bone"}
    if "leftEye" in human and "rightEye" in human:
        h, l, r = (world_pos(human[k]["node"]) for k in ("head", "leftEye", "rightEye"))
        look["offsetFromHeadBone"] = [(l[a] + r[a]) / 2 - h[a] for a in range(3)]
        rng = {"inputMaxValue": 90.0, "outputScale": 12.0}
        look.update({"rangeMapHorizontalInner": dict(rng), "rangeMapHorizontalOuter": dict(rng),
                     "rangeMapVerticalDown": {"inputMaxValue": 90.0, "outputScale": 10.0},
                     "rangeMapVerticalUp": {"inputMaxValue": 90.0, "outputScale": 10.0}})
    else:
        look["type"] = "expression"

    vrm = {
        "specVersion": "1.0",
        "meta": {"name": name, "version": "1", "authors": [author],
                 "copyrightInformation": "Character assets from Baldur's Gate 3 (c) Larian Studios. Personal use only.",
                 "licenseUrl": "https://vrm.dev/licenses/1.0/", "avatarPermission": "onlyAuthor",
                 "allowExcessivelyViolentUsage": False, "allowExcessivelySexualUsage": False,
                 "commercialUsage": "personalNonProfit", "allowPoliticalOrReligiousUsage": False,
                 "allowAntisocialOrHateUsage": False, "creditNotation": "required",
                 "allowRedistribution": False, "modification": "prohibited"},
        "humanoid": {"humanBones": human},
        "firstPerson": {"meshAnnotations": [{"node": i, "type": "thirdPersonOnly"} for i in head_meshes] +
                                           [{"node": i, "type": "auto"} for i in other_meshes]},
        "lookAt": look,
        "expressions": {"preset": preset, "custom": custom},
    }
    ext = js.setdefault("extensions", {})
    ext["VRMC_vrm"] = vrm
    used = ["VRMC_vrm"]

    springs = _spring_chains(nodes, by_name, human)
    if springs:
        ext["VRMC_springBone"] = springs
        used.append("VRMC_springBone")
    js["extensionsUsed"] = sorted(set((js.get("extensionsUsed") or []) + used))
    js.pop("animations", None)          # VRM avatars are driven by the host app
    return _write_glb(js, binc)


# ---------------------------------------------------------------- spring bones

import re as _re

CHAIN = _re.compile(r"^(?P<pre>[A-Za-z_]*?(?:Tail|Hair|Braid|Pony|Plait|Cloth|Cape|Cloak|Skirt|Tassel|Chain|Ribbon|"
                    r"Strap|Feather|Robe|Coat|Scarf|Belt)[A-Za-z_]*?)(?P<n>\d+)(?P<side>_[A-Za-z]+)?(?:_endBone)?$", _re.I)
SPRING = {  # stiffness, gravity, drag, radius
    "tail": (1.2, 0.15, 0.4, 0.02), "wing": (3.0, 0.0, 0.7, 0.04), "chest": (1.1, 0.05, 0.3, 0.05), "cloth_chain": (0.7, 0.35, 0.45, 0.03), "hair": (0.9, 0.08, 0.45, 0.015), "cloth": (0.6, 0.3, 0.5, 0.03)}


def _chest_springs(nodes):
    """The Breast_L / Breast_R bones the assembler adds: a soft, lively spring each."""
    st, grav, drag, rad = SPRING["chest"]
    out = []
    for side in ("L", "R"):
        a = next((i for i, n in enumerate(nodes) if n.get("name") == "Breast_" + side), None)
        b = next((i for i, n in enumerate(nodes) if n.get("name") == "Breast_%s_Tip" % side), None)
        if a is not None and b is not None:
            out.append({"name": "Breast_" + side, "joints": [
                {"node": j, "hitRadius": rad, "stiffness": st, "gravityPower": grav, "gravityDir": [0, -1, 0],
                 "dragForce": drag} for j in (a, b)]})
    return out


def _wing_springs(nodes, slot="Wings", kind="wing"):
    """Bones the assembler tagged (wings, cloth chains) split into non-branching runs, one spring each.
    Wings are stiff with no gravity (sway and settle); cloth is soft and hangs."""
    wing = {i for i, n in enumerate(nodes) if (n.get("extras") or {}).get("bga_slot") == slot}
    if not wing:
        return []
    kids = {i: [c for c in nodes[i].get("children", []) if c in wing] for i in wing}
    parent = {c: i for i in wing for c in kids[i]}
    st, grav, drag, rad = SPRING[kind]
    out = []
    starts = [i for i in wing if i not in parent]
    seen = set()
    while starts:
        i = starts.pop()
        run = [i]
        while len(kids[run[-1]]) == 1:
            run.append(kids[run[-1]][0])
        if len(kids[run[-1]]) > 1:
            starts += kids[run[-1]]
        run = [j for j in run if j not in seen]
        seen.update(run)
        if len(run) >= 2:
            out.append({"name": ("Wing_" if kind == "wing" else "") + (nodes[run[0]].get("name") or str(run[0])),
                        "joints": [{"node": j, "hitRadius": rad, "stiffness": st, "gravityPower": grav,
                                    "gravityDir": [0, -1, 0], "dragForce": drag} for j in run]})
    return out


def _spring_chains(nodes, by_name, human):
    """VRMC_springBone springs for every dangling bone chain the game rig has (tail, hair strands, braids,
    capes/skirt panels where an item has them), plus head/chest/hips colliders so they don't clip."""
    chains = {}
    for name, idx in by_name.items():
        m = CHAIN.match(name or "")
        if not m:
            continue
        key = (m.group("pre"), m.group("side") or "")
        chains.setdefault(key, []).append((int(m.group("n")), name.endswith("_endBone"), idx))
    springs = []
    for (pre, side), joints in sorted(chains.items()):
        joints.sort()
        if len(joints) < 2:
            continue
        low = pre.lower()
        kind = "tail" if "tail" in low else "hair" if any(k in low for k in ("hair", "braid", "pony", "plait")) else "cloth"
        st, grav, drag, rad = SPRING[kind]
        springs.append({"name": (pre + side).strip("_"), "center": human["hips"]["node"],
                        "colliderGroups": [0] if kind != "tail" else [],
                        "joints": [{"node": i, "hitRadius": rad, "stiffness": st, "gravityPower": grav,
                                    "gravityDir": [0, -1, 0], "dragForce": drag} for _n, _e, i in joints]})
    springs += _wing_springs(nodes)
    springs += _chest_springs(nodes)
    springs += _wing_springs(nodes, "Cloth", "cloth_chain")
    if not springs:
        return None
    colliders = []
    for bone, radius, off in (("head", 0.09, [0, 0.07, 0.01]), ("upperChest", 0.13, [0, 0.02, 0]),
                              ("chest", 0.13, [0, 0, 0]), ("hips", 0.14, [0, 0, 0])):
        if bone in human:
            colliders.append({"node": human[bone]["node"], "shape": {"sphere": {"offset": off, "radius": radius}}})
    out = {"specVersion": "1.0", "springs": springs}
    if colliders:
        out["colliders"] = colliders
        out["colliderGroups"] = [{"name": "Body", "colliders": list(range(len(colliders)))}]
    else:
        for sp in springs:
            sp.pop("colliderGroups", None)
    return out
