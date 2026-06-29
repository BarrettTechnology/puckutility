#!/usr/bin/env python3
"""Regenerate this app's window/launcher icon from the Barrett symbol.

Each app gets a function glyph built around the Barrett "puck" mark — dual-color:
the function glyph is orange (#FF7C1B), the Barrett disc stays navy (#0a2f65),
white outline rings/emblem:
    puckutility -> gear   pucktuner -> knob   puckfader -> fader   P4-checkout -> clipboard+check

Writes images/BarrettIcon.png (256x256) and images/BarrettIcon.ico (multi-size).
Source mark: images/Barrett_Symbol.png (Barrett puck symbol, brand file TD-59-Logo-003).
Needs Pillow + numpy (already in the app venv):  python scripts/make-icons.py
"""
import os, sys, math
import numpy as np
from PIL import Image, ImageDraw

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP  = os.path.basename(REPO).lower()
GLYPH = {"puckutility": "gear", "pucktuner": "knob",
         "puckfader": "fader", "p4-checkout": "checkout"}.get(APP)
if GLYPH is None:
    sys.exit("make-icons: unknown app '%s' (expected puckutility/pucktuner/puckfader/P4-checkout)" % APP)

S = 1024
NAVY = (10, 47, 101); ORANGE = (255, 124, 27, 255); WHITE = (255, 255, 255, 255)
SRC     = os.path.join(REPO, "images", "Barrett_Symbol.png")
OUT_PNG = os.path.join(REPO, "images", "BarrettIcon.png")
OUT_ICO = os.path.join(REPO, "images", "BarrettIcon.ico")
if not os.path.exists(SRC):
    sys.exit("make-icons: missing source mark %s" % SRC)

# --- Build the navy Barrett disc: recolor blue->navy, fill the knockout emblem
#     white (so it reads on any background), and drop the trademark/anything
#     outside the disc. ---
raw = Image.open(SRC).convert("RGBA")
a = np.array(raw); al = a[..., 3]; op = al > 0
for ch in range(3):
    a[..., ch] = np.where(op, NAVY[ch], a[..., ch])
ys, xs = np.where(al > 100); cxp, cyp = xs.mean(), ys.mean()
Rd = np.percentile(np.sqrt((xs - cxp) ** 2 + (ys - cyp) ** 2), 98)
yy, xx = np.indices(al.shape)
a[np.sqrt((xx - cxp) ** 2 + (yy - cyp) ** 2) > Rd * 1.04, 3] = 0   # remove the ® (sits at ~1.05-1.08 R)
navy_sym = Image.fromarray(a)
white = Image.new("RGBA", raw.size, (0, 0, 0, 0))
ImageDraw.Draw(white).ellipse([cxp - Rd * 0.985, cyp - Rd * 0.985,
                               cxp + Rd * 0.985, cyp + Rd * 0.985], fill=WHITE)
SYM = Image.alpha_composite(white, navy_sym); SYM = SYM.crop(SYM.getbbox())

NAVYA = NAVY + (255,); cx = cy = S / 2
def _mask(): m = Image.new("L", (S, S), 0); return m, ImageDraw.Draw(m)
def _glyph(m): img = Image.new("RGBA", (S, S), (0, 0, 0, 0)); return Image.composite(Image.new("RGBA", (S, S), ORANGE), img, m)  # function glyph = orange (Barrett disc stays navy)
def _place(img, d, X, Y): img.alpha_composite(d, (int(X - d.size[0] / 2), int(Y - d.size[1] / 2)))
def _fit(w): return SYM.resize((int(w), int(w * SYM.size[1] / SYM.size[0])), Image.LANCZOS)

def gear():
    m, d = _mask(); teeth = 8; rb = S * 0.31; rt = S * 0.44
    for i in range(teeth):
        A = (i / teeth) * 2 * math.pi; w = 0.20; wt = 0.12; base = rb * 0.92
        d.polygon([(cx + base * math.cos(A - w), cy + base * math.sin(A - w)),
                   (cx + rt * math.cos(A - wt), cy + rt * math.sin(A - wt)),
                   (cx + rt * math.cos(A + wt), cy + rt * math.sin(A + wt)),
                   (cx + base * math.cos(A + w), cy + base * math.sin(A + w))], fill=255)
    d.ellipse([cx - rb, cy - rb, cx + rb, cy + rb], fill=255)
    g = _glyph(m); gw = S * 0.46; r = gw / 2 + S * 0.026
    ImageDraw.Draw(g).ellipse([cx - r, cy - r, cx + r, cy + r], fill=WHITE)  # white outline ring
    _place(g, _fit(gw), cx, cy); return g

def knob():
    m, d = _mask(); st = math.radians(135); sw = math.radians(270)
    for i in range(11):
        A = st + sw * (i / 10)
        d.line([(cx + S * 0.40 * math.cos(A), cy + S * 0.40 * math.sin(A)),
                (cx + S * 0.46 * math.cos(A), cy + S * 0.46 * math.sin(A))], fill=255, width=int(S * 0.060))
    d.polygon([(cx - S * 0.035, cy - S * 0.365), (cx + S * 0.035, cy - S * 0.365), (cx, cy - S * 0.315)], fill=255)
    k = _glyph(m); kw = S * 0.56; r = kw / 2 + S * 0.026
    ImageDraw.Draw(k).ellipse([cx - r, cy - r, cx + r, cy + r], fill=WHITE)  # white outline ring
    _place(k, _fit(kw), cx, cy); return k

def fader():
    m, d = _mask(); tt = S * 0.14; tb = S * 0.86; tw = S * 0.052
    pos = [(S * 0.30, 0.34), (S * 0.70, 0.62)]
    for x, kp in pos:
        d.rounded_rectangle([x - tw / 2, tt, x + tw / 2, tb], radius=tw / 2, fill=255)
    f = _glyph(m)
    for x, kp in pos:
        ky = tt + (tb - tt) * kp; dw = S * 0.22; r = dw / 2 + S * 0.020
        ImageDraw.Draw(f).ellipse([x - r, ky - r, x + r, ky + r], fill=WHITE)  # white outline ring
        _place(f, _fit(dw), x, ky)
    return f

def checkout():
    m, d = _mask(); bw, bh = S * 0.52, S * 0.62; bx = (S - bw) / 2; by = (S - bh) / 2 + S * 0.03
    d.rounded_rectangle([bx, by, bx + bw, by + bh], radius=S * 0.05, fill=255)
    inner = [bx + S * 0.05, by + S * 0.05, bx + bw - S * 0.05, by + bh - S * 0.05]
    d.rounded_rectangle(inner, radius=S * 0.035, fill=0)
    cw, ch = S * 0.22, S * 0.11
    d.rounded_rectangle([(S - cw) / 2, by - ch * 0.55, (S + cw) / 2, by + ch * 0.45], radius=S * 0.025, fill=255)
    c = _glyph(m)
    # Fill the battery interior with white right up to the orange walls, so the
    # enlarged Barrett logo reads on white and its outline is coincident with the
    # battery walls (matching the white outline on the other app icons).
    ImageDraw.Draw(c).rounded_rectangle(inner, radius=S * 0.035, fill=WHITE)
    X, Y = cx, (inner[1] + inner[3]) / 2; _place(c, _fit(S * 0.36), X, Y)
    ImageDraw.Draw(c).line([(X - 0.105 * S, Y + 0.005 * S), (X - 0.025 * S, Y + 0.072 * S),
                            (X + 0.115 * S, Y - 0.072 * S)], fill=ORANGE, width=int(S * 0.055), joint="curve")
    return c

def _autofit(img, target=256, fill=0.95):
    """Scale the glyph so its bounding box fills `fill` of the square (minimal
    margin), centered — so the icon isn't lost in empty canvas."""
    bb = img.getbbox()
    c = img.crop(bb); w, h = c.size
    s = (target * fill) / max(w, h)
    nw, nh = max(1, round(w * s)), max(1, round(h * s))
    c = c.resize((nw, nh), Image.LANCZOS)
    out = Image.new("RGBA", (target, target), (0, 0, 0, 0))
    out.alpha_composite(c, ((target - nw) // 2, (target - nh) // 2))
    return out

GLYPHS = {"gear": gear, "knob": knob, "fader": fader, "checkout": checkout}

if __name__ == "__main__":
    icon = GLYPHS[GLYPH]()
    png = _autofit(icon, 256, 0.95)
    png.save(OUT_PNG)
    png.save(OUT_ICO, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("%s: wrote %s glyph -> images/BarrettIcon.png (256) + images/BarrettIcon.ico (multi-size)" % (APP, GLYPH))
