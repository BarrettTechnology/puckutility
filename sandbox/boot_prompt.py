#!/usr/bin/env python3
"""
Boot-time prompt for the Raspberry Pi pendulum demo.

Full screen, in the style of the pucktuner splash: the pucks backdrop with
the orange glow, the Barrett logo, "Start the pendulum demo?" and YES / NO.
  YES -> replaces this process with the pendulum kiosk (furuta_pendulum.py --touchscreen)
  NO  -> exits, leaving the normal desktop

Launched at login by the XDG autostart entry that setup-pi.sh installs.

  --auto-yes N   pick YES automatically after N seconds (default: wait forever)
"""

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import kiosk_widgets as kw          # sets the Linux display env before wx
import wx

GREEN = (34, 160, 84)
GREY  = (120, 126, 145)


class PromptCanvas(wx.Panel):
    """The whole prompt is painted in one panel, sized from the actual window,
    so it fits any screen and the buttons sit cleanly on the backdrop."""

    def __init__(self, parent, on_pick):
        super().__init__(parent)
        self._on_pick = on_pick
        self._countdown = ""
        self._pressed = None
        self._buttons = {}          # 'yes'/'no' -> wx.Rect (set while painting)
        self._backdrop = wx.Image(kw.BACKDROP)
        self._logo_cache = self._back_cache = None
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_SIZE, lambda e: (self.Refresh(), e.Skip()))
        self.Bind(wx.EVT_LEFT_DOWN, self._on_down)
        self.Bind(wx.EVT_LEFT_DCLICK, self._on_down)
        self.Bind(wx.EVT_LEFT_UP, self._on_up)

    def set_countdown(self, text):
        self._countdown = text
        self.Refresh()

    def _scaled(self, W, H):
        if self._back_cache and self._back_cache[0] == (W, H):
            return self._back_cache[1], self._logo_cache
        back = kw.cover_bitmap(self._backdrop, W, H) if self._backdrop.IsOk() else None
        self._logo_cache = kw.load_logo(int(H * 0.17), max_width=int(W * 0.55))
        self._back_cache = ((W, H), back)
        return back, self._logo_cache

    def _on_paint(self, _):
        dc = wx.AutoBufferedPaintDC(self)
        W, H = self.GetClientSize()
        dc.SetBackground(wx.Brush(wx.Colour(*kw.WHITE)))
        dc.Clear()
        if W < 50 or H < 50:
            return
        back, logo = self._scaled(W, H)
        if back:
            dc.DrawBitmap(back, 0, 0)

        gc = wx.GraphicsContext.Create(dc)
        y = H * 0.14
        if logo:
            lw, lh = logo.GetSize()
            gc.DrawBitmap(logo, (W - lw) / 2, y, lw, lh)
            y += lh
        y += H * 0.10

        q = "Start the pendulum demo?"
        gc.SetFont(kw.fit_font(gc, q, W * 0.8, H * 0.075), wx.Colour(*kw.NAVY))
        tw, th = gc.GetTextExtent(q)
        gc.DrawText(q, (W - tw) / 2, y)
        y += th + H * 0.06

        bw, bh, gap = W * 0.24, H * 0.20, W * 0.04
        x0 = (W - (2 * bw + gap)) / 2
        for key, label, colour, x in (('yes', "YES", GREEN, x0),
                                      ('no',  "NO",  GREY,  x0 + bw + gap)):
            rect = wx.Rect(int(x), int(y), int(bw), int(bh))
            self._buttons[key] = rect
            r, g, b = colour
            if self._pressed == key:
                r, g, b = int(r * 0.75), int(g * 0.75), int(b * 0.75)
            gc.SetBrush(wx.Brush(wx.Colour(r, g, b)))
            gc.SetPen(wx.TRANSPARENT_PEN)
            gc.DrawRoundedRectangle(rect.x, rect.y, rect.width, rect.height, bh * 0.14)
            gc.SetFont(kw.fit_font(gc, label, bw * 0.8, bh * 0.42), wx.WHITE)
            lw, lh = gc.GetTextExtent(label)
            gc.DrawText(label, rect.x + (bw - lw) / 2, rect.y + (bh - lh) / 2)
        y += bh + H * 0.03

        if self._countdown:
            gc.SetFont(kw.px_font(H * 0.03), wx.Colour(*GREY))
            tw, _ = gc.GetTextExtent(self._countdown)
            gc.DrawText(self._countdown, (W - tw) / 2, y)

    def _hit(self, pos):
        for key, rect in self._buttons.items():
            if rect.Contains(pos):
                return key
        return None

    def _on_down(self, evt):
        self._pressed = self._hit(evt.GetPosition())
        self.Refresh()

    def _on_up(self, evt):
        key, self._pressed = self._pressed, None
        self.Refresh()
        if key and self._hit(evt.GetPosition()) == key:
            self._on_pick(key == 'yes')


class BootPrompt(wx.Frame):

    def __init__(self, auto_yes_s=0):
        super().__init__(None, title="Barrett Pendulum")
        self.choice = None
        self._remaining = auto_yes_s
        self._canvas = PromptCanvas(self, self._pick)
        sz = wx.BoxSizer(wx.VERTICAL)
        sz.Add(self._canvas, 1, wx.EXPAND)
        self.SetSizer(sz)

        if auto_yes_s > 0:
            self._timer = wx.Timer(self)
            self.Bind(wx.EVT_TIMER, self._tick, self._timer)
            self._timer.Start(1000)
            self._update_countdown()

    def _update_countdown(self):
        self._canvas.set_countdown(f"Starting automatically in {self._remaining} s")

    def _tick(self, _):
        self._remaining -= 1
        if self._remaining <= 0:
            self._pick(True)
        else:
            self._update_countdown()

    def _pick(self, yes):
        if self.choice is not None:
            return
        self.choice = yes
        self.Close()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument('--auto-yes', type=int, default=0, metavar='N',
                    help='choose YES automatically after N seconds (0 = wait)')
    args = ap.parse_args()

    app = wx.App()
    frame = BootPrompt(args.auto_yes)
    frame.Show()
    frame.ShowFullScreen(True)
    app.MainLoop()

    if frame.choice:
        kiosk = os.path.join(HERE, 'furuta_pendulum.py')
        os.execv(sys.executable, [sys.executable, kiosk, '--touchscreen'])


if __name__ == "__main__":
    main()
