"""LZ4 block decompression. Uses the `lz4` package if installed, else a pure-Python fallback."""

try:
    import lz4.block as _lz4block
except ImportError:  # pragma: no cover - depends on environment
    _lz4block = None


def _py_decompress(src, uncompressed_size):
    dst = bytearray()
    i = 0
    n = len(src)
    while i < n:
        token = src[i]
        i += 1
        lit = token >> 4
        if lit == 15:
            while True:
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        dst += src[i:i + lit]
        i += lit
        if i >= n:
            break
        off = src[i] | (src[i + 1] << 8)
        i += 2
        ml = token & 15
        if ml == 15:
            while True:
                b = src[i]
                i += 1
                ml += b
                if b != 255:
                    break
        ml += 4
        start = len(dst) - off
        if start < 0 or off == 0:
            raise ValueError("corrupt LZ4 block (bad offset)")
        if off >= ml:
            dst += dst[start:start + ml]
        else:
            seg = bytes(dst[start:])
            dst += (seg * (ml // off + 1))[:ml]
    if uncompressed_size is not None and len(dst) != uncompressed_size:
        raise ValueError("LZ4 size mismatch: got %d, expected %d" % (len(dst), uncompressed_size))
    return bytes(dst)


def decompress(src, uncompressed_size):
    if _lz4block is not None:
        return _lz4block.decompress(src, uncompressed_size=uncompressed_size)
    return _py_decompress(src, uncompressed_size)


def backend():
    return "lz4 (native)" if _lz4block is not None else "pure-python fallback (slow for big files; `pip3 install lz4` to speed up)"
