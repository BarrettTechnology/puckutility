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

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGO_WIDE = os.path.join(REPO_ROOT, 'images', 'BarrettLogo.png')           # white bg, full name
LOGO_SMALL = os.path.join(REPO_ROOT, 'images', 'BarrettLogoScaled-NoBG.png')

NAVY = (13, 51, 110)
WHITE = (255, 255, 255)


def apply_display_env():
    """See the comment block at the top of puckutilityapp.py for why each is set."""
    if sys.platform.startswith('linux'):
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


def load_logo(height, path=LOGO_WIDE, max_width=None):
    """Logo bitmap cropped to its visible content and scaled to `height` px
    (or narrower if it would exceed max_width).  None if the file is missing."""
    img = wx.Image(path)
    if not img.IsOk():
        return None
    x, y, w, h = _content_box(img)
    img = img.GetSubImage(wx.Rect(x, y, w, h))
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
    return wx.Bitmap(img.Scale(new_w, new_h, wx.IMAGE_QUALITY_HIGH))


class LogoPanel(wx.Panel):
    """Draws a bitmap; fires on_long_press after the user holds it for
    `hold_s` seconds.  A plain wx.StaticBitmap is a no-window widget on GTK
    and doesn't receive mouse/touch events, hence a painted panel."""

    def __init__(self, parent, bitmap, bg=WHITE, align=wx.ALIGN_CENTER,
                 hold_s=None, on_long_press=None):
        super().__init__(parent)
        self._bmp = bitmap
        self._align = align
        self._hold_ms = int((hold_s or 0) * 1000)
        self._on_long_press = on_long_press
        self._timer = None
        self.SetBackgroundColour(wx.Colour(*bg))
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        if bitmap:
            self.SetMinSize(bitmap.GetSize())
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
        W, H = self.GetClientSize()
        bw, bh = self._bmp.GetSize()
        x = (W - bw) // 2 if self._align == wx.ALIGN_CENTER else 0
        dc.DrawBitmap(self._bmp, x, (H - bh) // 2, True)

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
                 font_pt=40, text_colour=WHITE):
        super().__init__(parent, size=size)
        self._label = label
        self._colour = colour
        self._text_colour = text_colour
        self._on_click = on_click
        self._font_pt = font_pt
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
        font = wx.Font(self._font_pt, wx.FONTFAMILY_SWISS,
                       wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)
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
