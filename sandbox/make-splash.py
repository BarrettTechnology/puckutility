#!/usr/bin/env python3
"""Generate the Barrett pendulum boot-splash (Plymouth) images, plus a preview.

  sandbox/assets/splash/logo.png       Barrett logo, upright (landscape screens)
  sandbox/assets/splash/logo-ccw.png   same, rotated 90 deg CCW -- for the Touch
                                       Display 2, whose raw framebuffer is portrait
                                       and appears rotated 90 deg CW to the viewer
  sandbox/assets/splash/spinner.png    orange arc on a light track (rotated at runtime)
  sandbox/assets/splash/preview-*.png  what the viewer sees (not installed)

install-splash.sh installs these as a Plymouth "script" theme.
Needs Pillow:  python sandbox/make-splash.py
"""
import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "assets", "splash")
LOGO = os.path.join(HERE, "assets", "barrett-logo.png")
ORANGE = (255, 124, 27, 255)
TRACK = (226, 230, 238, 255)

os.makedirs(OUT, exist_ok=True)

# Logo at ~the size it's drawn on a 1280-wide landscape view (theme rescales).
logo = Image.open(LOGO).convert("RGBA")
lw = 640
logo = logo.resize((lw, round(logo.height * lw / logo.width)), Image.LANCZOS)
logo.save(os.path.join(OUT, "logo.png"))
logo.rotate(90, expand=True).save(os.path.join(OUT, "logo-ccw.png"))      # PIL: +90 = CCW

# Spinner: 3/4 orange arc on a light full ring, drawn at 4x then downsampled.
S, R = 112, 4
big = Image.new("RGBA", (S * R, S * R), (0, 0, 0, 0))
d = ImageDraw.Draw(big)
w = 11 * R
box = [w // 2, w // 2, S * R - w // 2 - 1, S * R - w // 2 - 1]
d.arc(box, 0, 360, fill=TRACK, width=w)
d.arc(box, -90, 180, fill=ORANGE, width=w)
big.resize((S, S), Image.LANCZOS).save(os.path.join(OUT, "spinner.png"))


# ── preview: replicate the theme's layout maths on the raw framebuffer, then
#    rotate the framebuffer 90 deg CW the way the panel shows it to the viewer.
def compose(W, H):
    fb = Image.new("RGBA", (W, H), (255, 255, 255, 255))
    spin = Image.open(os.path.join(OUT, "spinner.png"))
    if W < H:                                           # rotated panel
        img = Image.open(os.path.join(OUT, "logo-ccw.png"))
        long_side = H * 0.47
        img = img.resize((round(long_side * img.width / img.height), round(long_side)))
        cx_logo, cy_logo = W / 2 - W * 0.08, H / 2
        cx_spin, cy_spin = W / 2 + W * 0.22, H / 2
    else:                                               # upright screen
        img = Image.open(os.path.join(OUT, "logo.png"))
        long_side = W * 0.47
        img = img.resize((round(long_side), round(long_side * img.height / img.width)))
        cx_logo, cy_logo = W / 2, H / 2 - H * 0.08
        cx_spin, cy_spin = W / 2, H / 2 + H * 0.22
    sd = round(min(W, H) * 0.09)
    spin = spin.resize((sd, sd))
    fb.alpha_composite(img, (round(cx_logo - img.width / 2), round(cy_logo - img.height / 2)))
    fb.alpha_composite(spin, (round(cx_spin - sd / 2), round(cy_spin - sd / 2)))
    return fb


compose(720, 1280).rotate(-90, expand=True).save(os.path.join(OUT, "preview-touch-display.png"))
compose(1280, 720).save(os.path.join(OUT, "preview-landscape.png"))
print("wrote", OUT)
