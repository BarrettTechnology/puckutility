"""Regenerate the quick start guide's screenshot (main-window.png).

Starts the app on a private Xvfb, grabs the main window before any scan, and
draws the numbered callouts the guide refers to.  Run from the repo root:

    __EGL_VENDOR_LIBRARY_FILENAMES=/usr/share/glvnd/egl_vendor.d/50_mesa.json \\
    xvfb-run -a -s "-screen 0 1280x1024x24 -noreset" \\
        env -u WAYLAND_DISPLAY GDK_BACKEND=x11 python docs/quickstart/make_screenshots.py

(Mesa EGL: the NVIDIA EGL library crashes Xvfb.)  Needs Pillow (ImageGrab with X11 support).  When a
control moves, adjust CALLOUTS: (number, widget or None, (x, y) in the frame).
"""
import os
import sys

import wx
from PIL import Image, ImageDraw, ImageFont, ImageGrab

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, '..', '..')))

from puckutility.gui import app as A                                  # noqa: E402

MARGIN = 40          # px added left and right for the badges
NAVY = (31, 56, 120)
#: number, the widget whose row/edge it marks (None: a fixed point), and
#: the badge centre: 'left' = in the left margin on the widget's row,
#: 'tr' = the widget's top-right corner, (x, y) = frame coordinates.
CALLOUTS = [
    (1, None, (95, 13)),                 # Menu / Log
    (2, 'choice_port', 'left'),
    (3, 'button_1', 'tr'),
    (4, 'choice_id', 'left'),
    (5, 'text_id', 'left'),
    (6, 'text_version', 'left'),
    (7, 'button_10', 'left'),
    (8, 'choice_test', 'left'),
    (9, 'button_6', 'tr'),
    (10, 'Dial', 'tr'),
    (11, 'onoffpanel', 'right'),
    (12, 'PTemp', 'right'),
]


def _font(size):
    for name in ('DejaVuSans-Bold.ttf', '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default()


def grab(frame):
    frame.Raise()
    r = frame.GetScreenRect()
    # The X server's own pixels: a wx.ScreenDC blit comes back faded
    # (GTK backdrop) and can miss a tab change.
    shot = ImageGrab.grab(xdisplay=None).crop((r.x, r.y, r.x + r.width, r.y + r.height))

    out = Image.new('RGB', (shot.width + 2 * MARGIN, shot.height), 'white')
    out.paste(shot, (MARGIN, 0))
    draw = ImageDraw.Draw(out)
    font = _font(15)
    for n, name, where in CALLOUTS:
        if name is None:
            x, y = where
        else:
            w = getattr(frame, name)
            wr = w.GetScreenRect()
            x0, y0 = wr.x - r.x, wr.y - r.y
            cy = y0 + wr.height // 2
            if where == 'left':
                x, y = -MARGIN // 2, cy
            elif where == 'right':
                x, y = r.width + MARGIN // 2 - 4, cy
            else:                                   # 'tr'
                x, y = x0 + wr.width, y0
        cx, cy = x + MARGIN, y
        rad = 12
        draw.ellipse((cx - rad, cy - rad, cx + rad, cy + rad), fill=NAVY, outline='white', width=2)
        draw.text((cx, cy), str(n), fill='white', font=font, anchor='mm')
    out.save(os.path.join(HERE, 'main-window.png'), optimize=True)
    print('wrote', os.path.join(HERE, 'main-window.png'))
    sys.stdout.flush()
    os._exit(0)


def main():
    os.chdir(os.path.join(HERE, '..', '..'))
    app = A.MyApp(0)
    wx.CallLater(4000, lambda: wx.CallLater(1500, grab, wx.GetApp().frame))
    wx.CallLater(60000, lambda: os._exit(1))
    app.MainLoop()


if __name__ == '__main__':
    main()
