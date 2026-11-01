"""World-aligned bone axes for exported .glb files.

BG3's skeleton is Maya-style: every bone's local axes point along the bone, so in Blender, Slate, Unity etc.
"turn the head" happens around a sideways axis, "nod" around a different one, and so on. This pass rebuilds
the rig so every joint's rest orientation matches the world (X = character's side, Y = up, Z = forward):
rotating any bone about X nods, about Y turns, about Z tilts. The mesh, skinning and any animation or pose
in the file deform exactly as before; only the bones' frames change.
"""
import math
import struct

from .vrm import _read_glb, _write_glb


def _qmul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return [aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
            aw * bw - ax * bx - ay * by - az * bz]


def _qconj(q):
    return [-q[0], -q[1], -q[2], q[3]]


def _qrot(q, v):
    p = _qmul(_qmul(q, [v[0], v[1], v[2], 0.0]), _qconj(q))
    return p[:3]


def _qnorm(q):
    n = math.sqrt(sum(x * x for x in q)) or 1.0
    return [x / n for x in q]


def _mat_inv(m):
    """Inverse of a column-major 4x4 affine matrix."""
    a = [[m[c * 4 + r] for c in range(3)] for r in range(3)]
    det = (a[0][0] * (a[1][1] * a[2][2] - a[1][2] * a[2][1]) - a[0][1] * (a[1][0] * a[2][2] - a[1][2] * a[2][0])
           + a[0][2] * (a[1][0] * a[2][1] - a[1][1] * a[2][0]))
    if abs(det) < 1e-12:
        return None
    inv = [[0.0] * 3 for _ in range(3)]
    for r in range(3):
        for c in range(3):
            r1, r2 = [i for i in range(3) if i != c]
            c1, c2 = [i for i in range(3) if i != r]
            cof = a[r1][c1] * a[r2][c2] - a[r1][c2] * a[r2][c1]
            inv[r][c] = cof / det * (-1 if (r + c) % 2 else 1)
    t = [m[12], m[13], m[14]]
    it = [-sum(inv[r][k] * t[k] for k in range(3)) for r in range(3)]
    out = [0.0] * 16
    for c in range(3):
        for r in range(3):
            out[c * 4 + r] = inv[r][c]
    out[12:15] = it
    out[15] = 1.0
    return out


def _mat_quat(m):
    """Rotation of a column-major matrix (scale divided out)."""
    cols = [m[0:3], m[4:7], m[8:11]]
    s = [math.sqrt(sum(x * x for x in c)) or 1.0 for c in cols]
    r = [[cols[c][rr] / s[c] for c in range(3)] for rr in range(3)]
    tr = r[0][0] + r[1][1] + r[2][2]
    if tr > 0:
        k = 0.5 / math.sqrt(tr + 1.0)
        q = [(r[2][1] - r[1][2]) * k, (r[0][2] - r[2][0]) * k, (r[1][0] - r[0][1]) * k, 0.25 / k]
    elif r[0][0] > r[1][1] and r[0][0] > r[2][2]:
        k = 2.0 * math.sqrt(1.0 + r[0][0] - r[1][1] - r[2][2])
        q = [0.25 * k, (r[0][1] + r[1][0]) / k, (r[0][2] + r[2][0]) / k, (r[2][1] - r[1][2]) / k]
    elif r[1][1] > r[2][2]:
        k = 2.0 * math.sqrt(1.0 + r[1][1] - r[0][0] - r[2][2])
        q = [(r[0][1] + r[1][0]) / k, 0.25 * k, (r[1][2] + r[2][1]) / k, (r[0][2] - r[2][0]) / k]
    else:
        k = 2.0 * math.sqrt(1.0 + r[2][2] - r[0][0] - r[1][1])
        q = [(r[0][2] + r[2][0]) / k, (r[1][2] + r[2][1]) / k, 0.25 * k, (r[1][0] - r[0][1]) / k]
    return _qnorm(q)


class _Acc(object):
    def __init__(self, js, binc, idx):
        a = js["accessors"][idx]
        bv = js["bufferViews"][a["bufferView"]]
        self.n = {"SCALAR": 1, "VEC3": 3, "VEC4": 4, "MAT4": 16}[a["type"]]
        self.count = a["count"]
        self.off = bv.get("byteOffset", 0) + a.get("byteOffset", 0)
        self.stride = bv.get("byteStride") or self.n * 4
        self.binc = binc
        assert a["componentType"] == 5126

    def get(self, i):
        return list(struct.unpack_from("<%df" % self.n, self.binc, self.off + i * self.stride))

    def set(self, i, v):
        struct.pack_into("<%df" % self.n, self.binc, self.off + i * self.stride, *v)


def world_align(data):
    """.glb bytes in, .glb bytes out with world-aligned joints. Files without a skin come back unchanged."""
    js, binc = _read_glb(data)
    binc = bytearray(binc)
    skins = js.get("skins") or []
    if not skins:
        return data
    nodes = js["nodes"]
    parent = {}
    for i, n in enumerate(nodes):
        for c in n.get("children", []):
            parent[c] = i
    bind_q, bind_p = {}, {}
    skinned = set()
    for sk in skins:
        if "inverseBindMatrices" not in sk:
            continue
        acc = _Acc(js, binc, sk["inverseBindMatrices"])
        for k, j in enumerate(sk["joints"]):
            w = _mat_inv(acc.get(k))
            if w is None or any(abs(x - 1.0) > 1e-3 for x in nodes[j].get("scale", [1, 1, 1])):
                return data                 # sheared / scaled rigs: leave as authored
            bind_q.setdefault(j, _mat_quat(w))
            bind_p.setdefault(j, w[12:15])
            skinned.add(j)
    ident = [0.0, 0.0, 0.0, 1.0]

    def rp(j):
        p = parent.get(j)
        return bind_q.get(p, ident) if p is not None else ident

    # every non-joint ancestor of a joint must be unrotated for the frames to compose; ours are roots
    for j in skinned:
        p = parent.get(j)
        if p is not None and p not in skinned and any(abs(a - b) > 1e-6 for a, b in zip(nodes[p].get("rotation", ident), ident)):
            return data
    for j in skinned:
        n = nodes[j]
        R_p, R_b = rp(j), bind_q[j]
        q = n.get("rotation", ident)
        n["rotation"] = _qnorm(_qmul(_qmul(R_p, q), _qconj(R_b)))
        if "translation" in n:
            n["translation"] = _qrot(R_p, n["translation"])
    # inverse bind matrices: bones now sit unrotated at their bind positions
    for sk in skins:
        if "inverseBindMatrices" not in sk:
            continue
        acc = _Acc(js, binc, sk["inverseBindMatrices"])
        for k, j in enumerate(sk["joints"]):
            p = bind_p[j]
            acc.set(k, [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, -p[0], -p[1], -p[2], 1])
    done = set()
    for anim in js.get("animations") or []:
        for ch in anim.get("channels", []):
            j = ch["target"].get("node")
            path = ch["target"]["path"]
            if j not in skinned or path not in ("rotation", "translation"):
                continue
            out = anim["samplers"][ch["sampler"]]["output"]
            if (out, path) in done:
                continue
            done.add((out, path))
            acc = _Acc(js, binc, out)
            R_p, R_b = rp(j), bind_q[j]
            for i in range(acc.count):
                v = acc.get(i)
                acc.set(i, _qnorm(_qmul(_qmul(R_p, v), _qconj(R_b))) if path == "rotation" else _qrot(R_p, v))
            a = js["accessors"][out]
            if "min" in a or "max" in a:
                vals = [acc.get(i) for i in range(acc.count)]
                a["min"] = [min(v[c] for v in vals) for c in range(acc.n)]
                a["max"] = [max(v[c] for v in vals) for c in range(acc.n)]
    return _write_glb(js, bytes(binc))


def world_align_file(path):
    with open(path, "rb") as f:
        data = f.read()
    out = world_align(data)
    if out is not data:
        with open(path, "wb") as f:
            f.write(out)
