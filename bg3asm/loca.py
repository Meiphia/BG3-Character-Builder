"""Larian .loca localization files: handle -> text."""

import struct


def read_loca(data):
    if not isinstance(data, (bytes, bytearray)):
        with open(data, "rb") as f:
            data = f.read()
    if data[:4] != b"LOCA":
        raise ValueError("not a LOCA file")
    n, texts_off = struct.unpack_from("<II", data, 4)
    out = {}
    p = 12
    t = texts_off
    for _ in range(n):
        key = data[p:p + 64].split(b"\0", 1)[0].decode("ascii", "replace")
        _ver, ln = struct.unpack_from("<HI", data, p + 64)
        p += 70
        out[key] = data[t:t + ln].rstrip(b"\0").decode("utf-8", "replace")
        t += ln
    return out
