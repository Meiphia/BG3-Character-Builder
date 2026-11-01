"""Granite virtual textures (.gts tile sets + .gtp page files), as used by BG3 for bodies and gear.

Format knowledge from LSLib (MIT, Norbyte): LSLib/VirtualTextures/*.cs.

    ts = TileSet(gts_bytes, page_loader)      # page_loader(gtp_filename) -> bytes
    img = ts.extract(gtex_name, layer=0)      # PIL.Image (RGBA), full resolution
Layers in BG3's Albedo_Normal_Physical sets: 0 = albedo, 1 = normal, 2 = physical.
"""

import io
import struct

from . import lz4block

CODEC_UNIFORM, CODEC_BC = 0, 9
LAYER_NAMES = ("albedo", "normal", "physical")

_GTS_HDR = struct.Struct("<III16sIQIQiiiIIQIIIQ7IIIQIQIQQ4I")


class VTError(Exception):
    pass


# ---------------------------------------------------------------- FastLZ (0.1.0) decompression

def fastlz_decompress(src, max_out):
    level = (src[0] >> 5) + 1
    out = bytearray()
    ip, n = 0, len(src)
    ctrl = src[ip] & 31
    ip += 1
    while True:
        if ctrl >= 32:
            length = (ctrl >> 5) - 1
            ofs = (ctrl & 31) << 8
            back = ofs
            if level == 1:
                if length == 6:
                    length += src[ip]
                    ip += 1
                back += src[ip]
                ip += 1
            else:
                if length == 6:
                    while True:
                        code = src[ip]
                        ip += 1
                        length += code
                        if code != 255:
                            break
                code = src[ip]
                ip += 1
                back += code
                if code == 255 and ofs == (31 << 8):
                    back = ((src[ip] << 8) | src[ip + 1]) + 8191
                    ip += 2
            start = len(out) - back - 1
            total = length + 3
            if start < 0:
                raise VTError("FastLZ: bad back-reference")
            if back + 1 >= total:
                out += out[start:start + total]
            else:
                for i in range(total):
                    out.append(out[start + i])
        else:
            ctrl += 1
            out += src[ip:ip + ctrl]
            ip += ctrl
        if ip >= n:
            break
        ctrl = src[ip]
        ip += 1
    if len(out) > max_out:
        del out[max_out:]
    return bytes(out)


# ---------------------------------------------------------------- tile set

class TileSet(object):
    def __init__(self, data, page_loader):
        self.data = data
        self.page_loader = page_loader
        h = _GTS_HDR.unpack_from(data, 0)
        (magic, self.version, _u, _guid, n_layers, layers_off, n_levels, levels_off,
         self.tile_w, self.tile_h, self.border, _i2, n_flat, flat_off, _i6, _i7,
         n_packed, packed_off) = h[:18]
        rest = h[18:]
        (_m, _n, _o, _p, _q, _r, _s, self.page_size, n_pagefiles, pagefile_off,
         fourcc_size, fourcc_off, n_pblocks, pblocks_off, _thumbs, _a, _b, _c, _d) = rest
        if magic != 0x47505247:
            raise VTError("not a GTS tile set")
        self.layers = [struct.unpack_from("<II", data, layers_off + i * 8) for i in range(n_layers)]
        self.levels = []
        for i in range(n_levels):
            w, hgt, off = struct.unpack_from("<IIQ", data, levels_off + i * 16)
            self.levels.append((w, hgt, off))
        self.n_layers = n_layers
        self.packed_off = packed_off
        self._tile_img = {}
        self.flat = [struct.unpack_from("<HHHHI", data, flat_off + i * 12) for i in range(n_flat)]
        self.pblocks = {}
        for i in range(n_pblocks):
            pid, codec, size, off = struct.unpack_from("<IIIQ", data, pblocks_off + i * 20)
            if codec == CODEC_BC:
                comp1 = data[off + 2:off + 18].split(b"\0", 1)[0].decode()
                comp2 = data[off + 18:off + 34].split(b"\0", 1)[0].decode()
                self.pblocks[pid] = ("bc", comp1, comp2)
            else:
                self.pblocks[pid] = ("uniform",)
        self.page_files = []
        for i in range(n_pagefiles):
            off = pagefile_off + i * 536
            raw = data[off:off + 512]
            name = raw.decode("utf-16-le", "replace").split("\0", 1)[0]
            (npages,) = struct.unpack_from("<I", data, off + 512)
            self.page_files.append((name, npages))
        self.textures = {}
        self._read_fourcc(fourcc_off, fourcc_size)
        self._pages = {}

    # FourCC metadata tree -> texture placement in the atlas
    def _read_fourcc(self, off, size):
        def parse(p, end):
            items = []
            while p < end:
                cc, fmt, ext, ln = struct.unpack_from("<4sBBH", self.data, p)
                p += 8
                if ext == 1:
                    ln |= struct.unpack_from("<I", self.data, p)[0] << 16
                    p += 4
                val = None
                if fmt == 1:
                    val = parse(p, p + ln)
                elif fmt == 2:
                    val = self.data[p:p + ln - 2].decode("utf-16-le", "replace")
                elif fmt == 3:
                    val = struct.unpack_from("<I", self.data, p)[0]
                else:
                    val = self.data[p:p + ln]
                p += ln
                p += (-p) % 4
                items.append((cc.decode("ascii", "replace"), val))
            return items
        tree = parse(off, off + size)

        def child(items, cc):
            for k, v in items or []:
                if k == cc:
                    return v
            return None
        atls = child(tree[0][1] if tree and isinstance(tree[0][1], list) else tree, "ATLS")
        for _k, tex in child(atls, "TXTS") or []:
            name = child(tex, "NAME")
            self.textures[name] = (child(tex, "XXXX"), child(tex, "YYYY"), child(tex, "WDTH"), child(tex, "HGHT"))

    # ------------------------------------------------------------ tiles

    def _page_file(self, idx):
        pf = self._pages.get(idx)
        if pf is None:
            name = self.page_files[idx][0]
            blob = self.page_loader(name)
            offsets = []
            npages = len(blob) // self.page_size
            for page in range(npages):
                base = page * self.page_size + (24 if page == 0 else 0)
                (cnt,) = struct.unpack_from("<I", blob, base)
                offsets.append(struct.unpack_from("<%dI" % cnt, blob, base + 4))
            pf = (blob, offsets)
            self._pages[idx] = pf
        return pf

    def _flat_bc3(self, ti):
        """BC3 bytes (main mip only) of flat tile `ti`."""
        pfi, page, chunk, _d, _packed = self.flat[ti]
        blob, offsets = self._page_file(pfi)
        pos = offsets[page][chunk] + page * self.page_size
        codec, pid, size = struct.unpack_from("<III", blob, pos)
        payload = blob[pos + 12:pos + 12 + size]
        bw, bh = (self.tile_w + 3) // 4, (self.tile_h + 3) // 4
        main = 16 * bw * bh
        full = main + 16 * ((self.tile_w // 2 + 3) // 4) * ((self.tile_h // 2 + 3) // 4)
        if codec == CODEC_UNIFORM:
            r, g, b, a = payload[:4]
            return _uniform_bc3(r, g, b, a) * (bw * bh)
        kind = self.pblocks.get(pid, ("bc", "raw", ""))
        if kind[1] == "raw":
            raw = payload
        elif kind[1] == "lz4":
            raw = lz4block.decompress(payload, full)
        elif kind[1] == "lz77":
            raw = fastlz_decompress(payload, full)
        else:
            raise VTError("unsupported tile compression %s" % (kind,))
        return raw[:main]

    def _flat_image(self, ti):
        """Decoded inner (border-less) RGBA image of flat tile `ti` (cached)."""
        im = self._tile_img.get(ti)
        if im is None:
            from PIL import Image
            im = Image.open(io.BytesIO(dds_bc3(self._flat_bc3(ti), self.tile_w, self.tile_h)))
            im.load()
            b = self.border
            im = im.convert("RGBA").crop((b, b, self.tile_w - b, self.tile_h - b))
            if len(self._tile_img) > 512:
                self._tile_img.clear()
            self._tile_img[ti] = im
        return im

    def _tile_image(self, level, layer, x, y):
        """Inner tile image at (level, x, y). Tiles stored only at a coarser mip (flagged index)
        are reconstructed by cropping the matching part of that coarser tile and upscaling."""
        lw, lh, off = self.levels[level]
        (ti,) = struct.unpack_from("<I", self.data, off + 4 * (layer + self.n_layers * (x + y * lw)))
        if not ti & 0x80000000:
            return self._flat_image(ti)
        fi = ti & 0x7FFFFFFF
        if fi >= len(self.flat):
            return None
        packed = struct.unpack_from("<I", self.data, self.packed_off + 4 * self.flat[fi][4])[0]
        src_level, py, px = (packed >> 4) & 0xF, (packed >> 8) & 0xFFF, packed >> 20
        scale = 1 << max(0, src_level - level)
        coarse = self._flat_image(fi)
        iw, ih = coarse.size
        sw, sh = iw // scale, ih // scale
        sx, sy = (x - px * scale) * sw, (y - py * scale) * sh
        if not (0 <= sx < iw and 0 <= sy < ih):
            return None
        from PIL import Image
        crop = coarse.crop((sx, sy, sx + sw, sy + sh))
        # per channel: PIL would premultiply RGB by alpha, and alpha holds data (normal X etc.)
        return Image.merge("RGBA", [b.resize((iw, ih), Image.BILINEAR) for b in crop.split()])

    def extract(self, name, layer=0, level=0):
        """Full texture `name` as a PIL RGBA image."""
        from PIL import Image
        if name not in self.textures:
            raise VTError("texture %s not in tile set" % name)
        X, Y, W, H = self.textures[name]
        inner_w, inner_h = self.tile_w - 2 * self.border, self.tile_h - 2 * self.border
        s = 1 << level
        tx0, ty0 = X // inner_w // s, Y // inner_h // s
        tx1 = -(-((X + W) // inner_w) // s) - 1
        ty1 = -(-((Y + H) // inner_h) // s) - 1
        out = Image.new("RGBA", ((tx1 - tx0 + 1) * inner_w, (ty1 - ty0 + 1) * inner_h), (0, 0, 0, 0))
        for ty in range(ty0, ty1 + 1):
            for tx in range(tx0, tx1 + 1):
                tile = self._tile_image(level, layer, tx, ty)
                if tile is not None:
                    out.paste(tile, ((tx - tx0) * inner_w, (ty - ty0) * inner_h))
        return out


def _uniform_bc3(r, g, b, a):
    c = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
    return bytes([a, a, 0, 0, 0, 0, 0, 0]) + struct.pack("<HHI", c, c, 0)


def dds_bc3(data, w, h):
    hdr = struct.pack("<4sIIIIIII44xII4sIIIIIIIIII", b"DDS ", 124, 0x1 | 0x2 | 0x4 | 0x1000 | 0x80000, h, w,
                      len(data), 0, 1, 32, 0x4, b"DXT5", 0, 0, 0, 0, 0, 0x1000, 0, 0, 0, 0)
    return hdr + data
