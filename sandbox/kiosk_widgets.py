"""
Shared touch-screen widgets for the pendulum kiosk and the boot prompt.

  Importing this module applies the same Linux display defaults as
  puckutilityapp.py (GDK_BACKEND=x11 etc.), so import it BEFORE `import wx`.
  load_logo(h)         -- Barrett logo, cropped to its visible pixels, scaled to h px.
  LogoPanel            -- paints the logo; optional long-press callback
                          (the hidden staff menu).
  BigButton            -- large custom-drawn touch button.  wx.Button ignores
                          background colours on GTK, so the big green/red
                          START/STOP buttons are painted by hand.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
LOGO_WIDE = os.path.join(REPO_ROOT, 'images', 'BarrettLogo.png')           # white bg, full name
LOGO_SMALL = os.path.join(REPO_ROOT, 'images', 'BarrettLogoScaled-NoBG.png')  # "Barrett(TM)", as on the splash
# High-res branding for the Pi screens (sandbox/assets):
#   barrett-logo.png       2020x582 "Barrett(TM)", navy on transparent (from the
#                          icon-mockups master, background keyed out)
#   pendulum-backdrop.png  1500x625 pucktuner splash backdrop (pucks + orange glow)
LOGO_HIRES = os.path.join(HERE, 'assets', 'barrett-logo.png')
APP_ICON = os.path.join(HERE, 'assets', 'pendulum-icon.png')   # make-pendulum-icon.py
BACKDROP = os.path.join(HERE, 'assets', 'pendulum-backdrop.png')

# Layouts are designed for the Touch Display 2 in landscape; everything is
# scaled by screen_scale() so they also fit whatever size the desktop reports
# (e.g. with display scaling on).
DESIGN_W, DESIGN_H = 1280, 720

NAVY = (13, 51, 110)
WHITE = (255, 255, 255)


# The full-screen pendulum screens (boot prompt, --touchscreen kiosk) run as
# NATIVE Wayland clients.  Under GDK_BACKEND=x11 a scaled desktop (125-200%,
# common on the Pi touch displays) renders the app at low resolution and
# stretches it -- everything goes soft.  Natively, GTK gets the real scale
# factor and we draw bitmaps at physical resolution (see hidpi_bitmap).
# Their layouts are sized from the screen, so they don't need the x11
# backend's integer-scaling workaround that puckutility's fixed-pixel wxGlade
# layout (and the engineering GUI here) relies on.
KIOSK_MODE = ('--touchscreen' in sys.argv
              or os.path.basename(sys.argv[0]) in ('boot_prompt.py', 'furuta_kiosk.py'))


def apply_display_env():
    """See the comment block at the top of puckutilityapp.py for why each is set."""
    if sys.platform.startswith('linux'):
        if not KIOSK_MODE:
            os.environ.setdefault('GDK_BACKEND', 'x11')
        os.environ.setdefault('GTK_THEME', 'Adwaita:light')
        os.environ.setdefault('GSETTINGS_BACKEND', 'memory')
        os.environ.setdefault('GTK_IM_MODULE', 'gtk-im-context-simple')
        os.environ.setdefault('NO_AT_BRIDGE', '1')


apply_display_env()
import wx  # noqa: E402  -- must come after apply_display_env()


def _content_box(img):
    """Bounding box (x, y, w, h) of the non-background pixels: alpha when the
    image has it, otherwise anything that isn't near-white."""
    w, h = img.GetWidth(), img.GetHeight()
    has_alpha = img.HasAlpha()
    data = img.GetData()
    alpha = img.GetAlpha() if has_alpha else None
    x0, y0, x1, y1 = w, h, -1, -1
    for y in range(h):
        row = y * w
        for x in range(w):
            i = row + x
            if has_alpha:
                on = alpha[i] > 16
            else:
                r, g, b = data[3 * i], data[3 * i + 1], data[3 * i + 2]
                on = min(r, g, b) < 235
            if on:
                if x < x0: x0 = x
                if x > x1: x1 = x
                if y < y0: y0 = y
                if y > y1: y1 = y
    if x1 < 0:
        return 0, 0, w, h
    return x0, y0, x1 - x0 + 1, y1 - y0 + 1


def hidpi_bitmap(img, w, h, scale=1.0):
    """Bitmap of `img` for a w x h (logical px) area, rendered at physical
    resolution (w*scale x h*scale) so it stays sharp on a scaled display."""
    pw, ph = max(1, round(w * scale)), max(1, round(h * scale))
    if (img.GetWidth(), img.GetHeight()) != (pw, ph):
        img = img.Scale(pw, ph, wx.IMAGE_QUALITY_HIGH)
    bmp = wx.Bitmap(img)
    if scale != 1.0:
        bmp.SetScaleFactor(scale)
    return bmp


def display_info(window):
    """One-line description of how the window is being rendered, for the log."""
    idx = wx.Display.GetFromWindow(window)
    geo = wx.Display(idx if idx != wx.NOT_FOUND else 0).GetGeometry()
    return (f"display {geo.width}x{geo.height} (logical), content scale "
            f"{window.GetContentScaleFactor():g}, GDK_BACKEND="
            f"{os.environ.get('GDK_BACKEND', 'auto')}, "
            f"session={os.environ.get('XDG_SESSION_TYPE', '?')}")


def load_logo(height, path=LOGO_HIRES, max_width=None, scale=1.0):
    """Logo bitmap cropped to its visible content, `height` logical px tall
    (or narrower if it would exceed max_width), rendered at `scale`x for
    HiDPI.  Use GetLogicalSize() for layout.  None if the file is missing."""
    img = wx.Image(path)
    if not img.IsOk():
        return None
    if path != LOGO_HIRES:          # the hi-res master is already tightly cropped
        x, y, w, h = _content_box(img)
        img = img.GetSubImage(wx.Rect(x, y, w, h))
    w, h = img.GetWidth(), img.GetHeight()
    if not img.HasAlpha():
        # The JPEG-sourced logo's "white" is ~(250,250,250): snap near-white
        # to pure white so it doesn't show as a grey box on the white screen.
        buf = bytearray(img.GetData())
        for i in range(0, len(buf), 3):
            if buf[i] >= 235 and buf[i + 1] >= 235 and buf[i + 2] >= 235:
                buf[i] = buf[i + 1] = buf[i + 2] = 255
        img.SetData(bytes(buf))
    new_h = height
    new_w = round(w * new_h / h)
    if max_width and new_w > max_width:
        new_w = max_width
        new_h = round(h * new_w / w)
    return hidpi_bitmap(img, new_w, new_h, scale)


def set_app_icon(frame):
    """Barrett Pendulum icon for the window (dock / app switcher)."""
    try:
        frame.SetIcon(wx.Icon(APP_ICON, wx.BITMAP_TYPE_PNG))
    except Exception:
        pass


def screen_scale(window=None):
    """Size of the screen `window` is on (or the primary one) relative to
    the 1280x720 design size."""
    idx = wx.Display.GetFromWindow(window) if window else wx.NOT_FOUND
    area = wx.Display(idx if idx != wx.NOT_FOUND else 0).GetGeometry()
    return min(area.width / DESIGN_W, area.height / DESIGN_H)


def px_font(px, bold=False):
    """Font sized in pixels (not points), so text scales with the bitmaps."""
    f = wx.Font(wx.FontInfo().Family(wx.FONTFAMILY_SWISS).Bold(bold))
    f.SetPixelSize(wx.Size(0, max(8, int(px))))
    return f


def fit_font(gc_or_dc, text, max_w, max_h, bold=True):
    """Largest pixel font (up to max_h) whose `text` fits in max_w."""
    px = max_h
    while px > 8:
        f = px_font(px, bold)
        if isinstance(gc_or_dc, wx.GraphicsContext):
            gc_or_dc.SetFont(f, wx.BLACK)
        else:
            gc_or_dc.SetFont(f)
        w = max(gc_or_dc.GetTextExtent(line)[0] for line in text.split('\n'))
        if w <= max_w:
            return f
        px = int(px * 0.9)
    return px_font(8, bold)


def cover_bitmap(img, W, H, anchor_x=0.35, scale=1.0):
    """Scale `img` to cover W x H logical px (cropping the overflow) -- like
    CSS background-size: cover -- at `scale`x physical resolution.  anchor_x
    picks which part of an over-wide image survives the crop (0 = keep the
    left edge, 1 = the right)."""
    PW, PH = max(1, round(W * scale)), max(1, round(H * scale))
    iw, ih = img.GetWidth(), img.GetHeight()
    s = max(PW / iw, PH / ih)
    sw, sh = max(PW, round(iw * s)), max(PH, round(ih * s))
    scaled = img.Scale(sw, sh, wx.IMAGE_QUALITY_HIGH)
    x = round((sw - PW) * anchor_x)
    y = round((sh - PH) / 2)
    return hidpi_bitmap(scaled.GetSubImage(wx.Rect(x, y, PW, PH)), W, H, scale)


class LogoPanel(wx.Panel):
    """Draws the logo `height` logical px tall, at the display's physical
    resolution; fires on_long_press after the user holds it for `hold_s`
    seconds.  A plain wx.StaticBitmap is a no-window widget on GTK and doesn't
    receive mouse/touch events, hence a painted panel."""

    def __init__(self, parent, height, path=LOGO_HIRES, bg=WHITE, align=wx.ALIGN_CENTER,
                 hold_s=None, on_long_press=None):
        super().__init__(parent)
        self._height, self._path = height, path
        self._bmp = load_logo(height, path)          # logical size for layout
        self._bmp_scale = 1.0
        self._align = align
        self._hold_ms = int((hold_s or 0) * 1000)
        self._on_long_press = on_long_press
        self._timer = None
        self.SetBackgroundColour(wx.Colour(*bg))
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        if self._bmp:
            self.SetMinSize(self._bmp.GetLogicalSize())
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_SIZE, lambda e: (self.Refresh(), e.Skip()))
        if on_long_press and self._hold_ms:
            self.Bind(wx.EVT_LEFT_DOWN, self._on_down)
            self.Bind(wx.EVT_LEFT_DCLICK, self._on_down)
            self.Bind(wx.EVT_LEFT_UP, self._cancel)
            self.Bind(wx.EVT_LEAVE_WINDOW, self._cancel)

    def _on_paint(self, _):
        dc = wx.AutoBufferedPaintDC(self)
        dc.SetBackground(wx.Brush(self.GetBackgroundColour()))
        dc.Clear()
        if not self._bmp:
            return
        scale = self.GetContentScaleFactor()
        if scale != self._bmp_scale:                 # re-render for this display
            self._bmp = load_logo(self._height, self._path, scale=scale)
            self._bmp_scale = scale
        W, H = self.GetClientSize()
        bw, bh = self._bmp.GetLogicalSize()
        x = (W - bw) // 2 if self._align == wx.ALIGN_CENTER else 0
        gc = wx.GraphicsContext.Create(dc)
        gc.DrawBitmap(self._bmp, x, (H - bh) // 2, bw, bh)

    def _on_down(self, _):
        self._cancel(None)
        self._timer = wx.CallLater(self._hold_ms, self._fire)

    def _cancel(self, evt):
        if self._timer:
            self._timer.Stop()
            self._timer = None
        if evt:
            evt.Skip()

    def _fire(self):
        self._timer = None
        self._on_long_press()


class BigButton(wx.Panel):
    """Large rounded touch button.  on_click fires on release inside the button."""

    def __init__(self, parent, label, colour, on_click, size=(300, 140),
                 text_colour=WHITE):
        super().__init__(parent, size=size)
        self._label = label
        self._colour = colour
        self._text_colour = text_colour
        self._on_click = on_click
        self._pressed = False
        self._enabled = True
        self.SetMinSize(size)
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_SIZE, lambda e: (self.Refresh(), e.Skip()))
        self.Bind(wx.EVT_LEFT_DOWN, self._on_down)
        self.Bind(wx.EVT_LEFT_DCLICK, self._on_down)   # fast double-taps
        self.Bind(wx.EVT_LEFT_UP, self._on_up)
        self.Bind(wx.EVT_LEAVE_WINDOW, self._on_leave)

    def set(self, label=None, colour=None, enabled=None):
        if label is not None:
            self._label = label
        if colour is not None:
            self._colour = colour
        if enabled is not None:
            self._enabled = enabled
            if not enabled:
                self._pressed = False
        self.Refresh()

    def _on_paint(self, _):
        dc = wx.AutoBufferedPaintDC(self)
        dc.SetBackground(wx.Brush(self.GetParent().GetBackgroundColour()))
        dc.Clear()
        gc = wx.GraphicsContext.Create(dc)
        W, H = self.GetClientSize()
        r, g, b = self._colour if self._enabled else (185, 188, 198)
        if self._pressed:
            r, g, b = int(r * 0.75), int(g * 0.75), int(b * 0.75)
        gc.SetBrush(wx.Brush(wx.Colour(r, g, b)))
        gc.SetPen(wx.TRANSPARENT_PEN)
        gc.DrawRoundedRectangle(2, 2, W - 4, H - 4, min(W, H) * 0.14)
        n_lines = self._label.count('\n') + 1
        font = fit_font(gc, self._label, W * 0.8, H * 0.42 / n_lines)
        gc.SetFont(font, wx.Colour(*(self._text_colour if self._enabled
                                     else (245, 245, 245))))
        lines = self._label.split('\n')
        sizes = [gc.GetTextExtent(line) for line in lines]
        total_h = sum(s[1] for s in sizes)
        y = (H - total_h) / 2
        for line, (tw, th) in zip(lines, sizes):
            gc.DrawText(line, (W - tw) / 2, y)
            y += th

    def _on_down(self, _):
        if self._enabled:
            self._pressed = True
            self.Refresh()

    def _on_leave(self, evt):
        if self._pressed:
            self._pressed = False
            self.Refresh()
        evt.Skip()

    def _on_up(self, evt):
        was = self._pressed
        self._pressed = False
        self.Refresh()
        W, H = self.GetClientSize()
        x, y = evt.GetPosition()
        if was and self._enabled and 0 <= x < W and 0 <= y < H:
            self._on_click()
