"""BitKnit2 decompressor (Granny 2 compression type 4), pure Python.

Ported from pybg3's rans.h (MIT, Copyright 2024 Mackenzie Straight):
https://github.com/eiz/pybg3  — see that project for the format notes.
"""

import struct

MAGIC = 0x75B1
FREQ_BITS = 15
TOTAL = 1 << FREQ_BITS
LOOKUP_SHIFT = FREQ_BITS - 10
INTERVAL = 1024


class _Model(object):
    __slots__ = ("V", "sums", "lookup", "acc", "counter", "incr", "last")

    def __init__(self, V, nmin):
        self.V = V
        neq = V - nmin
        sums = [0] * (V + 1)
        for i in range(neq):
            sums[i] = (TOTAL - nmin) * i // neq
        for i in range(neq, V + 1):
            sums[i] = TOTAL - V + i
        self.sums = sums
        self.acc = [1] * V
        self.counter = 0
        self.incr = (TOTAL - V) // INTERVAL
        self.last = 1 + TOTAL - V - self.incr * INTERVAL
        self.lookup = [0] * (1 << 10)
        self._rebuild()

    def _rebuild(self):
        sums, lookup = self.sums, self.lookup
        code, sym, nxt = 0, 0, sums[1]
        step = 1 << LOOKUP_SHIFT
        while code < TOTAL:
            if code < nxt:
                lookup[code >> LOOKUP_SHIFT] = sym
                code += step
            else:
                sym += 1
                nxt = sums[sym + 1]

    def observe(self, s):
        acc = self.acc
        acc[s] += self.incr
        self.counter = (self.counter + 1) % INTERVAL
        if self.counter == 0:
            acc[s] += self.last
            sums = self.sums
            total = 0
            for i in range(1, self.V + 1):
                total += acc[i - 1]
                sums[i] = (sums[i] + ((total - sums[i]) >> 1)) & 0xFFFF
                acc[i - 1] = 1
            self._rebuild()


class BitKnitError(Exception):
    pass


def decompress(src, out_size):
    """Decompress one BitKnit2 stream (bytes) into out_size bytes."""
    n_words = len(src) // 2
    words = struct.unpack_from("<%dH" % n_words, src, 0)
    if not n_words or words[0] != MAGIC:
        raise BitKnitError("bad BitKnit magic")
    pos = 1
    dst = bytearray(out_size)

    cmd_models = [_Model(300, 36) for _ in range(4)]
    ref_models = [_Model(40, 0) for _ in range(4)]
    off_model = _Model(21, 0)
    cache = [1] * 8
    order = 0x76543210
    delta = 1
    cur = 0

    while cur < out_size:
        if pos >= n_words:
            raise BitKnitError("unexpected end of stream")
        q_end = min(out_size, (cur & ~0xFFFF) + 0x10000)

        if words[pos] == 0:  # raw quantum
            pos += 1
            n = min((n_words - pos) * 2, q_end - cur)
            dst[cur:cur + n] = src[pos * 2:pos * 2 + n]
            cur += n
            pos += n // 2
            continue

        # ---- initial state ("tying the knot")
        merged = (words[pos] << 16) | words[pos + 1]
        pos += 2
        split = merged & 15
        merged >>= 4
        if merged < 0x10000:
            merged = (merged << 16) | words[pos]
            pos += 1
        s1 = merged >> split
        if s1 < 0x10000:
            s1 = (s1 << 16) | words[pos]
            pos += 1
        s2 = ((merged << 16) | words[pos]) & ((1 << (16 + split)) - 1)
        pos += 1
        s2 |= 1 << (16 + split)

        if cur == 0:
            dst[0] = s1 & 0xFF
            s1 >>= 8
            if s1 < 0x10000:
                s1 = (s1 << 16) | words[pos]
                pos += 1
            s1, s2 = s2, s1
            cur = 1

        while cur < q_end:
            # ---- command word (literal delta or copy)
            m = cmd_models[cur & 3]
            code = s1 & 0x7FFF
            sums = m.sums
            sym = m.lookup[code >> LOOKUP_SHIFT]
            while code >= sums[sym + 1]:
                sym += 1
            s1 = (s1 >> FREQ_BITS) * (sums[sym + 1] - sums[sym]) + code - sums[sym]
            if s1 < 0x10000:
                s1 = (s1 << 16) | words[pos]
                pos += 1
            m.observe(sym)
            s1, s2 = s2, s1

            if sym < 256:
                dst[cur] = (sym + dst[cur - delta]) & 0xFF
                cur += 1
                continue

            # ---- copy
            mi = cur & 3
            if sym < 288:
                length = sym - 254
            else:
                nb = sym - 287
                bits = s1 & ((1 << nb) - 1)
                s1 >>= nb
                if s1 < 0x10000:
                    s1 = (s1 << 16) | words[pos]
                    pos += 1
                s1, s2 = s2, s1
                length = (1 << nb) + bits + 32

            m = ref_models[mi]
            code = s1 & 0x7FFF
            sums = m.sums
            ref = m.lookup[code >> LOOKUP_SHIFT]
            while code >= sums[ref + 1]:
                ref += 1
            s1 = (s1 >> FREQ_BITS) * (sums[ref + 1] - sums[ref]) + code - sums[ref]
            if s1 < 0x10000:
                s1 = (s1 << 16) | words[pos]
                pos += 1
            m.observe(ref)
            s1, s2 = s2, s1

            if ref < 8:
                sh = ref * 4
                slot = (order >> sh) & 15
                mask = ((16 << sh) - 1) & 0xFFFFFFFF
                order = ((order & ~mask) | (((order << 4) | slot) & mask)) & 0xFFFFFFFF
                offset = cache[slot]
            else:
                m = off_model
                code = s1 & 0x7FFF
                sums = m.sums
                ol = m.lookup[code >> LOOKUP_SHIFT]
                while code >= sums[ol + 1]:
                    ol += 1
                s1 = (s1 >> FREQ_BITS) * (sums[ol + 1] - sums[ol]) + code - sums[ol]
                if s1 < 0x10000:
                    s1 = (s1 << 16) | words[pos]
                    pos += 1
                m.observe(ol)
                s1, s2 = s2, s1

                nb = ol & 15
                bits = s1 & ((1 << nb) - 1)
                s1 >>= nb
                if s1 < 0x10000:
                    s1 = (s1 << 16) | words[pos]
                    pos += 1
                s1, s2 = s2, s1
                if ol >= 16:
                    bits = (bits << 16) | words[pos]
                    pos += 1
                offset = (32 << ol) + (bits << 5) - 32 + (ref - 7)
                cache[order >> 28] = cache[(order >> 24) & 15]
                cache[(order >> 24) & 15] = offset

            if offset > cur or length > out_size - cur:
                raise BitKnitError("invalid copy (offset %d length %d at %d)" % (offset, length, cur))
            delta = offset
            if offset >= length:
                dst[cur:cur + length] = dst[cur - offset:cur - offset + length]
            else:
                for i in range(length):
                    dst[cur + i] = dst[cur + i - offset]
            cur += length

        if s1 != 0x10000 or s2 != 0x10000:
            raise BitKnitError("rANS stream corrupted")
    return bytes(dst)
