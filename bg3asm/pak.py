"""Reader for Larian LSPK .pak archives (BG3: version 18; also 15/16).

Only the file table is read on open, so opening a 13 GB pak is instant.
"""

import os
import struct
import zlib

from . import lz4block

COMP_NONE, COMP_ZLIB, COMP_LZ4, COMP_ZSTD = 0, 1, 2, 3


def _zstd_decompress(data, size):
    try:
        from compression import zstd  # Python 3.14+
        return zstd.decompress(data)
    except ImportError:
        pass
    try:
        import zstandard
    except ImportError:
        raise RuntimeError("file is zstd-compressed: install `zstandard` (pip3 install zstandard) or use Python 3.14+")
    return zstandard.ZstdDecompressor().decompress(data, max_output_size=size)


class PakEntry(object):
    __slots__ = ("name", "offset", "part", "flags", "size_on_disk", "size", "pak")

    def __init__(self, pak, name, offset, part, flags, size_on_disk, size):
        self.pak = pak
        self.name = name
        self.offset = offset
        self.part = part
        self.flags = flags
        self.size_on_disk = size_on_disk
        self.size = size

    @property
    def compression(self):
        return self.flags & 0x0F

    @property
    def real_size(self):
        return self.size if self.size else self.size_on_disk

    def read(self):
        return self.pak.read(self)

    def __repr__(self):
        return "<PakEntry %s %d bytes>" % (self.name, self.real_size)


class Pak(object):
    def __init__(self, path):
        self.path = path
        self._parts = {}
        with open(path, "rb") as f:
            magic = f.read(4)
            if magic != b"LSPK":
                raise ValueError("%s: not an LSPK pak (magic %r)" % (path, magic))
            (self.version,) = struct.unpack("<I", f.read(4))
            if self.version in (16, 18):
                file_list_offset, file_list_size, self.flags, self.priority, self.md5, self.num_parts = \
                    struct.unpack("<QIBB16sH", f.read(32))
            elif self.version == 15:
                file_list_offset, file_list_size, self.flags, self.priority, self.md5 = \
                    struct.unpack("<QIBB16s", f.read(30))
                self.num_parts = 1
            else:
                raise ValueError("%s: unsupported pak version %d" % (path, self.version))
            if self.flags & 0x04:
                raise ValueError("%s: solid archives are not supported" % path)

            f.seek(file_list_offset)
            num_files, compressed_size = struct.unpack("<II", f.read(8))
            if self.version == 18:
                fmt, esize = "<256sIHBBII", 272
            else:
                fmt, esize = "<256sQQQII8x", 296
            table = lz4block.decompress(f.read(compressed_size), num_files * esize)

        self.entries = []
        self.by_name = {}
        for i in range(num_files):
            rec = struct.unpack_from(fmt, table, i * esize)
            name = rec[0].split(b"\0", 1)[0].decode("utf-8", "replace").replace("\\", "/")
            if self.version == 18:
                off_lo, off_hi, part, flags, size_on_disk, size = rec[1:]
                offset = off_lo | (off_hi << 32)
            else:
                offset, size_on_disk, size, part, flags = rec[1:]
            e = PakEntry(self, name, offset, part, flags, size_on_disk, size)
            self.entries.append(e)
            self.by_name[name.lower()] = e

    def _part_path(self, part):
        if part == 0:
            return self.path
        stem, ext = os.path.splitext(self.path)
        return "%s_%d%s" % (stem, part, ext)

    def _fh(self, part):
        fh = self._parts.get(part)
        if fh is None:
            fh = open(self._part_path(part), "rb")
            self._parts[part] = fh
        return fh

    def read(self, entry):
        if isinstance(entry, str):
            entry = self.by_name[entry.lower()]
        fh = self._fh(entry.part)
        fh.seek(entry.offset)
        data = fh.read(entry.size_on_disk)
        method = entry.compression
        if method == COMP_NONE or entry.size == 0:
            return data
        if method == COMP_ZLIB:
            return zlib.decompress(data)
        if method == COMP_LZ4:
            return lz4block.decompress(data, entry.size)
        if method == COMP_ZSTD:
            return _zstd_decompress(data, entry.size)
        raise ValueError("%s: unknown compression method %d" % (entry.name, method))

    def close(self):
        for fh in self._parts.values():
            fh.close()
        self._parts.clear()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
