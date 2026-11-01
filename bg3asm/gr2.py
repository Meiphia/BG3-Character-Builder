"""Granny 2 (.GR2) reader for BG3 files: little-endian, 64-bit (the game's own) or 32-bit pointers (some
mod exporters), v6/v7, BitKnit2-compressed sections.

GR2 files embed their own type definitions, so the reader is generic: it walks the type tree
from the root and returns plain Python data (dicts / lists). Helper functions then turn that
into meshes and skeletons.

    g = read_gr2(bytes)          -> dict (granny_file_info: Skeletons, Meshes, Models, ...)
    meshes = extract_meshes(g)   -> list of Mesh (positions, normals, uvs, weights, indices ...)
    skel = extract_skeleton(g)   -> list of bones
"""

import math
import struct

from . import bitknit

MAGIC_LE64 = bytes.fromhex("e59b495e6f631f141e13eba990beedc4")
MAGIC_LE32 = bytes.fromhex("29de6cc0baa40e4fc8ae6311b830fa9b")
MAGIC_LE32_V7 = bytes.fromhex("e52f4ae16fc28aee1ed2b44c90d755af")

(T_NONE, T_INLINE, T_REF, T_REF_ARRAY, T_ARRAY_OF_REFS, T_VARIANT_REF, _T6, T_REF_VARIANT_ARRAY,
 T_STRING, T_TRANSFORM, T_REAL32, T_INT8, T_UINT8, T_BINORMAL_INT8, T_NORMAL_UINT8, T_INT16, T_UINT16,
 T_BINORMAL_INT16, T_NORMAL_UINT16, T_INT32, T_UINT32, T_REAL16, T_EMPTY_REF) = range(23)

_PRIM = {
    T_REAL32: ("f", 4), T_INT8: ("b", 1), T_UINT8: ("B", 1), T_BINORMAL_INT8: ("b", 1),
    T_NORMAL_UINT8: ("B", 1), T_INT16: ("h", 2), T_UINT16: ("H", 2), T_BINORMAL_INT16: ("h", 2),
    T_NORMAL_UINT16: ("H", 2), T_INT32: ("i", 4), T_UINT32: ("I", 4), T_REAL16: ("e", 2),
}


class GR2Error(Exception):
    pass


class Member(object):
    __slots__ = ("type", "name", "definition", "array_size", "extra")

    def __repr__(self):
        return "<%s %d[%d]>" % (self.name, self.type, self.array_size)


class GR2File(object):
    def __init__(self, data):
        if data[:16] in (MAGIC_LE64, MAGIC_LE32_V7):
            # some mod exporters write the "32-bit" magic over what is really a 64-bit layout
            self.P = 8
        elif data[:16] == MAGIC_LE32:
            self.P = 4                      # 32-bit pointers: same format, narrower references
        else:
            raise GR2Error("unsupported GR2 flavour (only little-endian files are handled)")
        hdr = 0x20
        (self.version, _fsize, _crc, sec_off, nsec) = struct.unpack_from("<IIIII", data, hdr)
        if self.version not in (6, 7):
            raise GR2Error("unsupported GR2 version %d" % self.version)
        self.root_type = struct.unpack_from("<II", data, hdr + 20)
        self.root_node = struct.unpack_from("<II", data, hdr + 28)
        self.sections = []
        self.relocs = []
        for i in range(nsec):
            (comp, off, csize, usize, _al, _f16, _f8, roff, nrel, _moff, _nmix) = \
                struct.unpack_from("<11I", data, hdr + sec_off + i * 44)
            raw = data[off:off + csize]
            if usize == 0:
                sec = b""
            elif comp == 0:
                sec = raw
            elif comp == 4:
                sec = bitknit.decompress(raw, usize)
            else:
                raise GR2Error("section compression %d not supported" % comp)
            self.sections.append(sec)
            rel = {}
            if nrel:
                if comp == 0:
                    rblob = data[roff:roff + nrel * 12]
                else:
                    (rc,) = struct.unpack_from("<I", data, roff)
                    rblob = bitknit.decompress(data[roff + 4:roff + 4 + rc], nrel * 12)
                for j in range(nrel):
                    o, ts, to = struct.unpack_from("<III", rblob, j * 12)
                    rel[o] = (ts, to)
            self.relocs.append(rel)
        self._defs = {}
        self._sizes = {}

    # ------------------------------------------------------------ primitives

    def ptr(self, sec, off):
        return self.relocs[sec].get(off)

    def u32(self, sec, off):
        return struct.unpack_from("<I", self.sections[sec], off)[0]

    def cstr(self, ref):
        if ref is None:
            return None
        sec, off = ref
        s = self.sections[sec]
        end = s.index(b"\0", off)
        return s[off:end].decode("utf-8", "replace")

    # ------------------------------------------------------------ types

    def definition(self, ref):
        """List of Member for the type definition at ref."""
        d = self._defs.get(ref)
        if d is not None:
            return d
        sec, off = ref
        members = []
        while True:
            t = self.u32(sec, off)
            if t == T_NONE:
                break
            m = Member()
            m.type = t
            P = self.P
            m.name = self.cstr(self.ptr(sec, off + 4))
            m.definition = self.ptr(sec, off + 4 + P)
            m.array_size, e0, e1, e2 = struct.unpack_from("<IIII", self.sections[sec], off + 4 + 2 * P)
            m.extra = (e0, e1, e2)
            members.append(m)
            off += 20 + 3 * P              # type, name, definition, array size, extra[3], unused pointer
        self._defs[ref] = members
        return members

    def struct_size(self, ref):
        s = self._sizes.get(ref)
        if s is None:
            s = sum(self.member_size(m) for m in self.definition(ref))
            self._sizes[ref] = s
        return s

    def member_size(self, m):
        n = max(1, m.array_size)
        t = m.type
        if t == T_INLINE:
            return self.struct_size(m.definition) * n
        if t in _PRIM:
            return _PRIM[t][1] * n
        P = self.P
        return {T_REF: P, T_REF_ARRAY: 4 + P, T_ARRAY_OF_REFS: 4 + P, T_VARIANT_REF: 2 * P,
                T_REF_VARIANT_ARRAY: 4 + 2 * P, T_STRING: P, T_TRANSFORM: 68, T_EMPTY_REF: P}[t] * n

    # ------------------------------------------------------------ values

    def read_struct(self, type_ref, data_ref, depth=0):
        if data_ref is None or type_ref is None:
            return None
        sec, off = data_ref
        out = {}
        for m in self.definition(type_ref):
            n = max(1, m.array_size)
            if n > 1 and m.type != T_INLINE and m.type in _PRIM:
                fmt, sz = _PRIM[m.type]
                out[m.name] = list(struct.unpack_from("<%d%s" % (n, fmt), self.sections[sec], off))
                off += sz * n
                continue
            vals = []
            for _ in range(n):
                v, off = self._read_member(m, sec, off, depth)
                vals.append(v)
            out[m.name] = vals[0] if n == 1 else vals
        return out

    def _read_member(self, m, sec, off, depth):
        t = m.type
        if t in _PRIM:
            fmt, sz = _PRIM[t]
            return struct.unpack_from("<" + fmt, self.sections[sec], off)[0], off + sz
        if t == T_INLINE:
            return self.read_struct(m.definition, (sec, off), depth + 1), off + self.struct_size(m.definition)
        P = self.P
        if t == T_STRING:
            return self.cstr(self.ptr(sec, off)), off + P
        if t == T_TRANSFORM:
            v = struct.unpack_from("<I3f4f9f", self.sections[sec], off)
            return {"flags": v[0], "translation": list(v[1:4]), "rotation": list(v[4:8]),
                    "scale_shear": list(v[8:17])}, off + 68
        if t == T_REF:
            return self.read_struct(m.definition, self.ptr(sec, off), depth + 1), off + P
        if t == T_EMPTY_REF:
            return None, off + P
        if t == T_REF_ARRAY:
            (count,) = struct.unpack_from("<I", self.sections[sec], off)
            items = self.ptr(sec, off + 4)
            return self.read_array(m.definition, items, count, depth), off + 4 + P
        if t == T_ARRAY_OF_REFS:
            (count,) = struct.unpack_from("<I", self.sections[sec], off)
            items = self.ptr(sec, off + 4)
            out = []
            if items:
                isec, ioff = items
                for i in range(count):
                    out.append(self.read_struct(m.definition, self.ptr(isec, ioff + i * P), depth + 1))
            return out, off + 4 + P
        if t == T_VARIANT_REF:
            tref = self.ptr(sec, off)
            dref = self.ptr(sec, off + P)
            return self.read_struct(tref, dref, depth + 1), off + 2 * P
        if t == T_REF_VARIANT_ARRAY:
            tref = self.ptr(sec, off)
            (count,) = struct.unpack_from("<I", self.sections[sec], off + P)
            items = self.ptr(sec, off + P + 4)
            return VariantArray(self, tref, items, count), off + 4 + 2 * P
        raise GR2Error("member type %d not handled (%s)" % (t, m.name))

    def read_array(self, type_ref, items, count, depth=0):
        if not items or not count:
            return []
        defs = self.definition(type_ref)
        # fast path: arrays of a single primitive member (index buffers etc.)
        if len(defs) == 1 and defs[0].type in _PRIM:
            fmt, sz = _PRIM[defs[0].type]
            n = max(1, defs[0].array_size)
            sec, off = items
            flat = struct.unpack_from("<%d%s" % (count * n, fmt), self.sections[sec], off)
            return list(flat) if n == 1 else [list(flat[i * n:(i + 1) * n]) for i in range(count)]
        size = self.struct_size(type_ref)
        sec, off = items
        return [self.read_struct(type_ref, (sec, off + i * size), depth + 1) for i in range(count)]

    def root(self):
        return self.read_struct(tuple(self.root_type), tuple(self.root_node))


class VariantArray(object):
    """Lazily-decoded array of structs (vertex buffers): decode columns on demand."""

    def __init__(self, f, tref, items, count):
        self.f, self.tref, self.items, self.count = f, tref, items, count

    @property
    def members(self):
        return self.f.definition(self.tref) if self.tref else []

    def columns(self):
        """{member name: (gr2 type, components, flat list of values)}"""
        f = self.f
        if not self.items or not self.count:
            return {}
        stride = f.struct_size(self.tref)
        fmt, layout = "<", []
        for m in self.members:
            if m.type not in _PRIM:
                raise GR2Error("non-primitive vertex member %s" % m.name)
            c, sz = _PRIM[m.type]
            n = max(1, m.array_size)
            fmt += "%d%s" % (n, c)
            layout.append((m.name, m.type, n))
        sec, off = self.items
        raw = struct.iter_unpack(fmt, self.f.sections[sec][off:off + stride * self.count])
        rows = list(raw)
        out, col = {}, 0
        for name, t, n in layout:
            vals = []
            for r in rows:
                vals.extend(r[col:col + n])
            out[name] = (t, n, vals)
            col += n
        return out

    def __len__(self):
        return self.count


def read_gr2(data):
    f = GR2File(data)
    return f.root()


# ======================================================================== mesh helpers

class Mesh(object):
    def __init__(self, name):
        self.name = name
        self.positions = []      # [x, y, z] * n
        self.normals = []
        self.tangents = []       # [x, y, z, w]
        self.uvs = {}            # set index -> [u, v] * n
        self.colors = {}         # set index -> [r, g, b, a] * n (0..1)
        self.bone_indices = []   # [i0..i3] * n  (indices into bone_bindings)
        self.bone_weights = []   # [w0..w3] * n  (0..1)
        self.indices = []        # triangle list
        self.groups = []         # (material_index, first_tri, tri_count)
        self.bone_bindings = []  # bone names
        self.materials = []      # material names
        self.extended = {}

    def __repr__(self):
        return "<Mesh %s: %d verts, %d tris, %d bones>" % (
            self.name, len(self.positions), len(self.indices) // 3, len(self.bone_bindings))


def _norm(v):
    l = math.sqrt(sum(c * c for c in v)) or 1.0
    return [c / l for c in v]


def qtangent_to_frame(q):
    """BG3 QTangent (x, y, z, w quaternion, sign in w) -> (normal, tangent, bitangent sign)."""
    x, y, z, w = q
    l = math.sqrt(x * x + y * y + z * z + w * w) or 1.0
    x, y, z, w = x / l, y / l, z / l, w / l
    sign = -1.0 if w < 0 else 1.0
    # the sign of w flags reflected (mirrored-UV) frames; the normal flips with it
    # (verified: normals then agree with triangle winding on 100% of faces)
    normal = [sign * (2 * (x * z + y * w)), sign * (2 * (y * z - x * w)), sign * (1 - 2 * (x * x + y * y))]
    tangent = [1 - 2 * (y * y + z * z), 2 * (x * y + z * w), 2 * (x * z - y * w)]
    return normal, tangent, sign


def extract_meshes(root):
    out = []
    for gm in root.get("Meshes") or []:
        if not gm:
            continue
        m = Mesh(gm.get("Name"))
        vd = gm.get("PrimaryVertexData") or {}
        va = vd.get("Vertices")
        if isinstance(va, VariantArray) and va.count:
            cols = va.columns()
            n = va.count
            for name, (t, comps, vals) in cols.items():
                k = name.lower()
                rows = [vals[i * comps:(i + 1) * comps] for i in range(n)]
                if k == "position":
                    m.positions = [list(r[:3]) for r in rows]
                elif k == "boneweights":
                    if t in (T_NORMAL_UINT8, T_UINT8):
                        m.bone_weights = [[c / 255.0 for c in r] for r in rows]
                    elif t in (T_NORMAL_UINT16, T_UINT16):
                        m.bone_weights = [[c / 65535.0 for c in r] for r in rows]
                    else:
                        m.bone_weights = [list(r) for r in rows]
                elif k == "boneindices":
                    m.bone_indices = [list(r) for r in rows]
                elif k == "qtangent":
                    scale = 32767.0 if t == T_BINORMAL_INT16 else (127.0 if t == T_BINORMAL_INT8 else 1.0)
                    for r in rows:
                        nrm, tan, sgn = qtangent_to_frame([c / scale for c in r])
                        m.normals.append(nrm)
                        m.tangents.append(tan + [sgn])
                elif k == "normal":
                    m.normals = [_norm(_unpack_dir(r, t)) for r in rows]
                elif k == "tangent":
                    m.tangents = [_norm(_unpack_dir(r, t)[:3]) + [1.0] for r in rows]
                elif k.startswith("texturecoordinates"):
                    idx = int(k[len("texturecoordinates"):] or 0)
                    m.uvs[idx] = [[r[0], r[1]] for r in rows]
                elif k.startswith("diffusecolor"):
                    idx = int(k[len("diffusecolor"):] or 0)
                    div = 255.0 if t in (T_NORMAL_UINT8, T_UINT8) else (65535.0 if t in (T_NORMAL_UINT16, T_UINT16) else 1.0)
                    m.colors[idx] = [[c / div for c in r] + [1.0] * (4 - len(r)) for r in rows]
        topo = gm.get("PrimaryTopology") or {}
        if topo.get("Indices"):
            m.indices = [i & 0xFFFFFFFF for i in topo["Indices"]]
        else:  # 16-bit indices may be declared signed: values above 32767 come back negative
            m.indices = [i & 0xFFFF for i in topo.get("Indices16") or []]
        for g in topo.get("Groups") or []:
            m.groups.append((g.get("MaterialIndex", 0), g.get("TriFirst", 0), g.get("TriCount", 0)))
        m.bone_bindings = [b.get("BoneName") for b in gm.get("BoneBindings") or []]
        m.materials = [((mb or {}).get("Material") or {}).get("Name") for mb in gm.get("MaterialBindings") or []]
        m.extended = gm.get("ExtendedData") or {}
        out.append(m)
    return out


def _unpack_dir(r, t):
    if t == T_BINORMAL_INT16:
        return [c / 32767.0 for c in r]
    if t == T_BINORMAL_INT8:
        return [c / 127.0 for c in r]
    return list(r)


def extract_skeletons(root):
    out = []
    for s in root.get("Skeletons") or []:
        bones = []
        for b in s.get("Bones") or []:
            bones.append({"name": b.get("Name"), "parent": b.get("ParentIndex", -1),
                          "local": b.get("LocalTransform"), "inverse_world": b.get("InverseWorldTransform")})
        out.append({"name": s.get("Name"), "bones": bones})
    return out


def model_bindings(root):
    """[(model name, skeleton name, [mesh names])]"""
    out = []
    for md in root.get("Models") or []:
        sk = (md.get("Skeleton") or {}).get("Name")
        meshes = [((b or {}).get("Mesh") or {}).get("Name") for b in md.get("MeshBindings") or []]
        out.append((md.get("Name"), sk, meshes))
    return out
