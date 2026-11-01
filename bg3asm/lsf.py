"""Larian resource files: binary LSF (v2-v7) and XML LSX, loaded into one Node tree.

    doc = load_resource(bytes_or_path)        # auto-detects LSF / LSX
    for region in doc.regions: ...
    node.attrs  -> {name: value}   (values decoded to Python types; GUIDs as str)
    node.types  -> {name: type_name}
    node.children -> [Node]
"""

import struct
import uuid
import zlib
import xml.etree.ElementTree as ET

from . import lz4block

# ---------------------------------------------------------------- tree


class Node(object):
    __slots__ = ("name", "attrs", "types", "children", "parent")

    def __init__(self, name, parent=None):
        self.name = name
        self.attrs = {}
        self.types = {}
        self.children = []
        self.parent = parent

    def get(self, key, default=None):
        return self.attrs.get(key, default)

    def __getitem__(self, key):
        return self.attrs[key]

    def kids(self, name=None):
        return [c for c in self.children if name is None or c.name == name]

    def child(self, name):
        for c in self.children:
            if c.name == name:
                return c
        return None

    def walk(self, name=None):
        stack = [self]
        while stack:
            n = stack.pop()
            if name is None or n.name == name:
                yield n
            stack.extend(reversed(n.children))

    def __repr__(self):
        return "<Node %s %d attrs %d children>" % (self.name, len(self.attrs), len(self.children))


class Resource(object):
    def __init__(self):
        self.regions = {}  # region name -> root Node

    def region(self, name):
        return self.regions.get(name)


# ---------------------------------------------------------------- types

TYPE_NAMES = {
    0: "None", 1: "uint8", 2: "int16", 3: "uint16", 4: "int32", 5: "uint32", 6: "float", 7: "double",
    8: "ivec2", 9: "ivec3", 10: "ivec4", 11: "fvec2", 12: "fvec3", 13: "fvec4",
    14: "mat2x2", 15: "mat3x3", 16: "mat3x4", 17: "mat4x3", 18: "mat4x4", 19: "bool",
    20: "string", 21: "path", 22: "FixedString", 23: "LSString", 24: "uint64", 25: "ScratchBuffer",
    26: "old_int64", 27: "int8", 28: "TranslatedString", 29: "WString", 30: "LSWString",
    31: "guid", 32: "int64", 33: "TranslatedFSString",
}
_SCALAR = {1: "<B", 2: "<h", 3: "<H", 4: "<i", 5: "<I", 6: "<f", 7: "<d", 19: "<B", 24: "<Q", 26: "<q", 27: "<b", 32: "<q"}
_VEC = {8: ("<2i"), 9: "<3i", 10: "<4i", 11: "<2f", 12: "<3f", 13: "<4f",
        14: "<4f", 15: "<9f", 16: "<12f", 17: "<12f", 18: "<16f"}
_STRINGS = (20, 21, 22, 23, 29, 30)


def guid_from_lsf(b):
    """LSF stores .NET Guid bytes with BG3's byte-swapped tail; return canonical string."""
    b = bytearray(b)
    for i in range(8, 16, 2):
        b[i], b[i + 1] = b[i + 1], b[i]
    return str(uuid.UUID(bytes_le=bytes(b)))


class TranslatedString(object):
    __slots__ = ("handle", "version", "value", "args")

    def __init__(self, handle, version=0, value=None, args=None):
        self.handle, self.version, self.value, self.args = handle, version, value, args

    def __repr__(self):
        return "TS(%s)" % self.handle

    def __str__(self):
        return self.handle


# ---------------------------------------------------------------- compression

def _lz4_frame(data, size):
    try:
        import lz4.frame
        return lz4.frame.decompress(data)
    except ImportError:
        pass
    if struct.unpack_from("<I", data, 0)[0] != 0x184D2204:
        raise ValueError("bad LZ4 frame magic")
    flg = data[4]
    pos = 6 + (8 if flg & 0x08 else 0) + (4 if flg & 0x01 else 0) + 1
    block_checksum = bool(flg & 0x10)
    out = bytearray()
    while True:
        (bsize,) = struct.unpack_from("<I", data, pos)
        pos += 4
        if bsize == 0:
            break
        raw = bsize & 0x80000000
        bsize &= 0x7FFFFFFF
        chunk = data[pos:pos + bsize]
        pos += bsize + (4 if block_checksum else 0)
        if raw:
            out += chunk
        else:
            out += lz4block._py_decompress(chunk, None) if lz4block._lz4block is None else \
                lz4block._lz4block.decompress(chunk, uncompressed_size=4 << 20)
    return bytes(out)


def _decompress(data, size, flags, chunked):
    method = flags & 0x0F
    if method == 0:
        return data
    if method == 1:
        return zlib.decompress(data)
    if method == 2:
        if chunked:
            return _lz4_frame(data, size)
        return lz4block.decompress(data, size)
    if method == 3:
        from .pak import _zstd_decompress
        return _zstd_decompress(data, size)
    raise ValueError("unknown LSF compression %d" % method)


# ---------------------------------------------------------------- LSF


class _Reader(object):
    def __init__(self, buf):
        self.b = buf
        self.p = 0

    def take(self, n):
        v = self.b[self.p:self.p + n]
        self.p += n
        return v


def _read_names(buf):
    names = []
    (n,) = struct.unpack_from("<I", buf, 0)
    p = 4
    for _ in range(n):
        (cnt,) = struct.unpack_from("<H", buf, p)
        p += 2
        bucket = []
        for _ in range(cnt):
            (ln,) = struct.unpack_from("<H", buf, p)
            p += 2
            bucket.append(buf[p:p + ln].decode("utf-8", "replace"))
            p += ln
        names.append(bucket)
    return names


def _read_ts(buf, p, end, version):
    if version >= 4:
        (ver,) = struct.unpack_from("<H", buf, p)
        p += 2
        value = None
        if ver == 0 and end - p >= 4:
            # some files store a value string before the handle
            (ln,) = struct.unpack_from("<i", buf, p)
            if 0 < ln <= end - p - 8 and p + 4 + ln + 4 <= end:
                peek = struct.unpack_from("<i", buf, p + 4 + ln)[0]
                if 0 < peek <= end - (p + 8 + ln):
                    value = buf[p + 4:p + 4 + ln].rstrip(b"\0").decode("utf-8", "replace")
                    p += 4 + ln
    else:
        ver = 0
        (ln,) = struct.unpack_from("<i", buf, p)
        value = buf[p + 4:p + 4 + ln].rstrip(b"\0").decode("utf-8", "replace")
        p += 4 + ln
    (hl,) = struct.unpack_from("<i", buf, p)
    handle = buf[p + 4:p + 4 + hl].rstrip(b"\0").decode("utf-8", "replace")
    p += 4 + hl
    return TranslatedString(handle, ver, value), p


def _read_tfs(buf, p, end, version):
    ts, p = _read_ts(buf, p, end, version)
    (argc,) = struct.unpack_from("<i", buf, p)
    p += 4
    args = []
    for _ in range(argc):
        (kl,) = struct.unpack_from("<i", buf, p)
        key = buf[p + 4:p + 4 + kl].rstrip(b"\0").decode("utf-8", "replace")
        p += 4 + kl
        sub, p = _read_tfs(buf, p, end, version)
        (vl,) = struct.unpack_from("<i", buf, p)
        val = buf[p + 4:p + 4 + vl].rstrip(b"\0").decode("utf-8", "replace")
        p += 4 + vl
        args.append((key, sub, val))
    ts.args = args
    return ts, p


def _decode_value(t, buf, p, ln, version):
    if t in _STRINGS:
        return buf[p:p + ln].rstrip(b"\0").decode("utf-8", "replace")
    if t in _SCALAR:
        v = struct.unpack_from(_SCALAR[t], buf, p)[0]
        return bool(v) if t == 19 else v
    if t in _VEC:
        return list(struct.unpack_from(_VEC[t], buf, p))
    if t == 31:
        return guid_from_lsf(buf[p:p + 16])
    if t == 28:
        return _read_ts(buf, p, p + ln, version)[0]
    if t == 33:
        return _read_tfs(buf, p, p + ln, version)[0]
    if t == 25:
        return bytes(buf[p:p + ln])
    return bytes(buf[p:p + ln])


def read_lsf(data):
    if data[:4] != b"LSOF":
        raise ValueError("not an LSF file")
    (version,) = struct.unpack_from("<I", data, 4)
    p = 8
    if version >= 5:
        p += 8  # engine version (int64)
    else:
        p += 4
    if version >= 6:
        (s_u, s_d, k_u, k_d, n_u, n_d, a_u, a_d, v_u, v_d, cflags, _u2, _u3, mformat) = \
            struct.unpack_from("<IIIIIIIIIIBBHI", data, p)
        p += 48
    else:
        (s_u, s_d, n_u, n_d, a_u, a_d, v_u, v_d, cflags, _u2, _u3, mformat) = \
            struct.unpack_from("<IIIIIIIIBBHI", data, p)
        k_u = k_d = 0
        p += 40

    def section(usize, dsize, chunked):
        nonlocal p
        if usize == 0 and dsize == 0:
            return b""
        if dsize == 0:  # stored uncompressed
            blob = data[p:p + usize]
            p += usize
            return blob
        blob = data[p:p + dsize]
        p += dsize
        return _decompress(blob, usize, cflags, chunked and version >= 2)

    names = _read_names(section(s_u, s_d, False))
    nodes_blob = section(n_u, n_d, True)
    attrs_blob = section(a_u, a_d, True)
    values = section(v_u, v_d, True)
    keys_blob = section(k_u, k_d, True) if version >= 6 else b""  # keys section is stored last

    adjacency = (mformat == 1)

    def name_of(h):
        return names[h >> 16][h & 0xFFFF]

    # nodes
    nodes = []  # (name, parent, first_attr)
    if adjacency:
        for i in range(len(nodes_blob) // 16):
            h, parent, _next, first = struct.unpack_from("<IiiI", nodes_blob, i * 16)
            nodes.append((name_of(h), parent, struct.unpack("<i", struct.pack("<I", first))[0]))
    else:
        for i in range(len(nodes_blob) // 12):
            h, first, parent = struct.unpack_from("<Iii", nodes_blob, i * 12)
            nodes.append((name_of(h), parent, first))

    # attributes -> per-node ordered lists of (name, type, offset, length)
    per_node = {}
    if adjacency:
        attrs = []
        for i in range(len(attrs_blob) // 16):
            h, tl, nxt, off = struct.unpack_from("<IIiI", attrs_blob, i * 16)
            attrs.append((name_of(h), tl & 0x3F, tl >> 6, nxt, off))
        for ni, (_, _, first) in enumerate(nodes):
            lst = []
            a = first
            while a != -1 and a < len(attrs):
                nm, t, ln, nxt, off = attrs[a]
                lst.append((nm, t, off, ln))
                a = nxt
            per_node[ni] = lst
    else:
        off = 0
        for i in range(len(attrs_blob) // 12):
            h, tl, ni = struct.unpack_from("<IIi", attrs_blob, i * 12)
            t, ln = tl & 0x3F, tl >> 6
            per_node.setdefault(ni, []).append((name_of(h), t, off, ln))
            off += ln

    res = Resource()
    objs = []
    for ni, (nm, parent, _first) in enumerate(nodes):
        parent_node = objs[parent] if parent >= 0 else None
        n = Node(nm, parent_node)
        for an, t, off, ln in per_node.get(ni, ()):
            try:
                n.attrs[an] = _decode_value(t, values, off, ln, version)
            except Exception as e:  # keep going on odd attributes
                n.attrs[an] = ("<decode error %s>" % e)
            n.types[an] = TYPE_NAMES.get(t, str(t))
        objs.append(n)
        if parent_node is not None:
            parent_node.children.append(n)
        else:
            res.regions[nm] = n
    res.version = version
    res._keys = keys_blob
    return res


# ---------------------------------------------------------------- LSX

_LSX_PARSERS = {
    "uint8": int, "int8": int, "int16": int, "uint16": int, "int32": int, "uint32": int,
    "int64": int, "uint64": int, "old_int64": int, "float": float, "double": float,
    "bool": lambda s: s.lower() in ("true", "1"),
}


def _lsx_value(el):
    t = el.get("type")
    if t in ("TranslatedString", "TranslatedFSString"):
        return TranslatedString(el.get("handle"), int(el.get("version") or 0), el.get("value"))
    v = el.get("value")
    if v is None:
        return None
    if t in _LSX_PARSERS:
        try:
            return _LSX_PARSERS[t](v)
        except ValueError:
            return v
    if t and (t.startswith("fvec") or t.startswith("ivec") or t.startswith("mat")):
        conv = int if t.startswith("ivec") else float
        return [conv(x) for x in v.split()]
    return v


def read_lsx(data):
    if isinstance(data, (bytes, bytearray)):
        root = ET.fromstring(bytes(data))
    else:
        root = ET.parse(data).getroot()
    res = Resource()

    def build(el, parent):
        n = Node(el.get("id"), parent)
        for a in el.findall("attribute"):
            n.attrs[a.get("id")] = _lsx_value(a)
            n.types[a.get("id")] = a.get("type")
        ch = el.find("children")
        if ch is not None:
            for c in ch.findall("node"):
                n.children.append(build(c, n))
        return n

    for region in root.findall("region"):
        for top in region.findall("node"):
            res.regions[region.get("id")] = build(top, None)
    return res


def load_resource(src):
    if not isinstance(src, (bytes, bytearray)):
        with open(src, "rb") as f:
            src = f.read()
    if src[:4] == b"LSOF":
        return read_lsf(src)
    return read_lsx(src)


# ---------------------------------------------------------------- debug dump

def dump(node, indent=0, out=None, max_depth=99):
    import sys
    out = out or sys.stdout
    pad = "  " * indent
    out.write("%s%s\n" % (pad, node.name))
    for k, v in node.attrs.items():
        s = repr(v) if not isinstance(v, str) else v
        out.write("%s  .%s [%s] = %s\n" % (pad, k, node.types.get(k), s[:160]))
    if indent < max_depth:
        for c in node.children:
            dump(c, indent + 1, out, max_depth)
