"""Larian's skin colour model (CHAR_Skin_* shaders), shared by the baker and the custom-colour picker.

Reconstruction follows the community recreation of the in-game shader by Volno
(github.com/V0ln0/BG3-Substance-Painter-Shaders, CC-BY-SA-4.0) and Larian's own description
(Adobe Substance magazine, "Bringing Life to Legends"):

  HMVY map: R hemoglobin, G melanin, B vein, A yellowing   (masks, not colours)
  CLEA map: R cavity, G hair (brows / stubble), B lips, A ambient occlusion
  CancelMSK: R non-skin (nails, horn plates), G melanin removal, B cancel

  dark   = (1 - M) remapped by MelaninDarkThreshold / MelaninDarkMultiplier, + MelaninAmount
  mel    = dark * (1 - CancelG * MelaninRemovalAmount)
  eH, eM = 1 - exp(-(H * HemoglobinAmount)^2),  1 - exp(-mel^2)
  hm     = mix(HemoglobinColor^eH, VeinColor, V * VeinAmount) * MelaninColor^eM
  hmy    = mix(hm, hm * YellowingColor, Y * YellowingAmount) * (0.9522, 0.7441, 0.5952)
  skin   = mix(luminance(hmy), hmy, 1 - eH * eM)
All colours linear.
"""

import math

SKIN_TINT = (0.9522, 0.7441, 0.5952)
LUMA = (0.2126, 0.7152, 0.0722)
# a typical spot of facial skin (means of the human/elf/tiefling head HMVY + CancelMSK maps)
TYPICAL = {"H": 0.32, "M": 0.83, "V": 0.08, "Y": 0.18, "CancelG": 0.06}

DEFAULTS = {
    "HemoglobinAmount": 0.77, "MelaninAmount": 0.3, "MelaninRemovalAmount": 0.0, "VeinAmount": 0.6,
    "YellowingAmount": 0.3, "MelaninDarkThreshold": 0.9, "MelaninDarkMultiplier": 2.0,
    "HemoglobinColor": (0.6524, 0.009, 0.009), "MelaninColor": (0.032, 0.0073, 0.0014),
    "VeinColor": (0.0, 0.267, 0.586), "YellowingColor": (0.987, 0.786, 0.188),
}


def _p(params, k):
    v = params.get(k)
    return DEFAULTS[k] if v is None else v


def melanin_dark(b, thr, mult):
    """(1 - M) remap: identity below the threshold, ramps to `mult` above it."""
    if b <= thr:
        return b
    t = min(1.0, (b - thr) / max(1e-6, 1.0 - thr))
    return thr + (mult - thr) * t


def skin_color(params, H=None, M=None, V=None, Y=None, cancel_g=None):
    """Linear RGB of the skin at one texel (defaults: a typical facial texel)."""
    H = TYPICAL["H"] if H is None else H
    M = TYPICAL["M"] if M is None else M
    V = TYPICAL["V"] if V is None else V
    Y = TYPICAL["Y"] if Y is None else Y
    cg = TYPICAL["CancelG"] if cancel_g is None else cancel_g
    dark = melanin_dark(1.0 - M, _p(params, "MelaninDarkThreshold"), _p(params, "MelaninDarkMultiplier"))
    mel = max(0.0, (dark + _p(params, "MelaninAmount")) * (1.0 - cg * _p(params, "MelaninRemovalAmount")))
    hb = min(1.0, max(0.0, H * _p(params, "HemoglobinAmount")))
    eh = 1.0 - math.exp(-hb * hb)
    em = 1.0 - math.exp(-mel * mel)
    vw = min(1.0, max(0.0, V * _p(params, "VeinAmount")))
    yw = Y * _p(params, "YellowingAmount")
    out = []
    for i in range(3):
        hc = max(_p(params, "HemoglobinColor")[i], 1e-5) ** eh
        hm = (hc * (1 - vw) + _p(params, "VeinColor")[i] * vw) * max(_p(params, "MelaninColor")[i], 1e-5) ** em
        hm = hm * (1 - yw) + hm * _p(params, "YellowingColor")[i] * yw
        out.append(hm * SKIN_TINT[i])
    lum = sum(c * w for c, w in zip(out, LUMA))
    s = 1.0 - eh * em
    return [lum + (c - lum) * s for c in out]


def invert(target, params, iterations=12):
    """MelaninColor that makes a typical texel come out as `target` (linear RGB), keeping the
    other preset parameters. Returns (MelaninColor, achieved colour)."""
    mel_amount = _p(params, "MelaninAmount")
    p = dict(params)
    b = 1.0 - TYPICAL["M"]
    mel = (b + mel_amount) * (1.0 - TYPICAL["CancelG"] * _p(params, "MelaninRemovalAmount"))
    em = max(0.05, 1.0 - math.exp(-mel * mel))
    cur = [max(1e-4, c) for c in (target or (0.5, 0.3, 0.2))]
    for _ in range(iterations):
        p["MelaninColor"] = cur
        got = skin_color(p)
        cur = [min(1.0, max(1e-5, c * (max(t, 1e-5) / max(g, 1e-5)) ** (1.0 / em)))
               for c, t, g in zip(cur, target, got)]
    p["MelaninColor"] = cur
    return cur, skin_color(p)
