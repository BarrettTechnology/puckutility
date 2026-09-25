#!/usr/bin/env python3
"""Generate the Barrett Pendulum launcher icon, in the family style of
scripts/make-icons.py (puckutility gear, pucktuner knob, ...): the navy Barrett
puck disc with its white emblem + an orange function glyph.

Pendulum glyph: an orange cart on wheels riding an orange track, an orange rod
balanced upright on it (slightly tilted), and the navy Barrett disc as the
pendulum bob, with orange motion arcs.

Writes sandbox/assets/pendulum-icon.png (512) and pendulum-icon-256.png.
Needs Pillow + numpy:  python sandbox/make-pendulum-icon.py
"""
import math
import os

import numpy as np
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SRC = os.path.join(REPO, "images", "Barrett_Symbol.png")
OUT_DIR = os.path.join(HERE, "assets")

S = 1024
NAVY = (10, 47, 101); ORANGE = (255, 124, 27, 255); WHITE = (255, 255, 255, 255)

# --- navy Barrett disc: identical recipe to scripts/make-icons.py -------------
raw = Image.open(SRC).convert("RGBA")
a = np.array(raw); al = a[..., 3]; op = al > 0
for ch in range(3):
    a[..., ch] = np.where(op, NAVY[ch], a[..., ch])
ys, xs = np.where(al > 100); cxp, cyp = xs.mean(), ys.mean()
Rd = np.percentile(np.sqrt((xs - cxp) ** 2 + (ys - cyp) ** 2), 98)
yy, xx = np.indices(al.shape)
a[np.sqrt((xx - cxp) ** 2 + (yy - cyp) ** 2) > Rd * 1.04, 3] = 0   # drop the (R)
navy_sym = Image.fromarray(a)
white = Image.new("RGBA", raw.size, (0, 0, 0, 0))
ImageDraw.Draw(white).ellipse([cxp - Rd * 0.985, cyp - Rd * 0.985,
                               cxp + Rd * 0.985, cyp + Rd * 0.985], fill=WHITE)
SYM = Image.alpha_composite(white, navy_sym); SYM = SYM.crop(SYM.getbbox())


def _fit(w):
    return SYM.resize((int(w), int(w * SYM.size[1] / SYM.size[0])), Image.LANCZOS)


def _place(img, d, X, Y):
    img.alpha_composite(d, (int(X - d.size[0] / 2), int(Y - d.size[1] / 2)))


def pendulum():
    m = Image.new("L", (S, S), 0); d = ImageDraw.Draw(m)

    # track
    ty, tt = S * 0.905, S * 0.045
    d.rounded_rectangle([S * 0.06, ty - tt / 2, S * 0.94, ty + tt / 2], radius=tt / 2, fill=255)

    # wheels sit on the track; cart body above them
    wr = S * 0.055
    wy = ty - tt / 2 - wr
    cart_x, cw, ch = S * 0.40, S * 0.40, S * 0.14
    cart_bot = wy - wr * 0.35
    cart_top = cart_bot - ch
    d.rounded_rectangle([cart_x - cw / 2, cart_top, cart_x + cw / 2, cart_bot],
                        radius=S * 0.035, fill=255)
    for dx in (-cw * 0.30, cw * 0.30):
        d.ellipse([cart_x + dx - wr, wy - wr, cart_x + dx + wr, wy + wr], fill=255)

    # rod from the pivot on top of the cart, tilted a little to the right
    tilt = math.radians(16)
    dw = S * 0.34                          # Barrett disc (bob) diameter
    bob_y = S * 0.215
    L = (cart_top - bob_y) / math.cos(tilt)
    bob_x = cart_x + L * math.sin(tilt)
    d.line([(cart_x, cart_top), (bob_x, bob_y)], fill=255, width=int(S * 0.075))
    pr = S * 0.058
    d.ellipse([cart_x - pr, cart_top - pr, cart_x + pr, cart_top + pr], fill=255)

    # swing trail: dashes along the bob's arc (centred on the pivot), behind
    # the bob, fading out -- reads as "swinging up", not as a signal icon
    R = dw / 2 + S * 0.026
    edge = math.degrees(math.asin(R / L))           # bob's angular half-width
    phi_bob = math.degrees(tilt)
    gap, dash = 5, 9
    for k, width in enumerate((0.048, 0.036, 0.026)):
        hi = phi_bob - edge - gap - k * (dash + gap)
        lo = hi - dash
        # PIL arc angles: 0 = 3 o'clock, clockwise; straight up = 270
        d.arc([cart_x - L, cart_top - L, cart_x + L, cart_top + L],
              270 + lo, 270 + hi, fill=255, width=int(S * width))

    g = Image.composite(Image.new("RGBA", (S, S), ORANGE),
                        Image.new("RGBA", (S, S), (0, 0, 0, 0)), m)
    # white outline ring + the navy disc as the bob (as on the other app icons)
    ImageDraw.Draw(g).ellipse([bob_x - R, bob_y - R, bob_x + R, bob_y + R], fill=WHITE)
    _place(g, _fit(dw), bob_x, bob_y)
    return g


def _autofit(img, target, fill=0.95):
    """Fill `fill` of the square with the glyph's bounding box, centered
    (same as scripts/make-icons.py)."""
    c = img.crop(img.getbbox()); w, h = c.size
    s = (target * fill) / max(w, h)
    nw, nh = max(1, round(w * s)), max(1, round(h * s))
    c = c.resize((nw, nh), Image.LANCZOS)
    out = Image.new("RGBA", (target, target), (0, 0, 0, 0))
    out.alpha_composite(c, ((target - nw) // 2, (target - nh) // 2))
    return out


if __name__ == "__main__":
    icon = pendulum()
    os.makedirs(OUT_DIR, exist_ok=True)
    _autofit(icon, 512).save(os.path.join(OUT_DIR, "pendulum-icon.png"))
    _autofit(icon, 256).save(os.path.join(OUT_DIR, "pendulum-icon-256.png"))
    print("wrote sandbox/assets/pendulum-icon.png (512) + pendulum-icon-256.png")
