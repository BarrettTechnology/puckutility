#!/usr/bin/env python3
"""
Boot-time prompt for the Raspberry Pi pendulum demo.

Full-screen Barrett logo + "Start the pendulum demo?" with YES / NO.
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


class BootPrompt(wx.Frame):

    def __init__(self, auto_yes_s=0):
        super().__init__(None, title="Barrett Pendulum")
        self.choice = None
        self._remaining = auto_yes_s
        root = wx.Panel(self)
        root.SetBackgroundColour(wx.Colour(*kw.WHITE))
        vsz = wx.BoxSizer(wx.VERTICAL)
        vsz.AddStretchSpacer(3)
        vsz.Add(kw.LogoPanel(root, kw.load_logo(150, max_width=1000)), 0, wx.EXPAND)
        vsz.AddStretchSpacer(2)

        q = wx.StaticText(root, label="Start the pendulum demo?")
        q.SetFont(wx.Font(34, wx.FONTFAMILY_SWISS, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        q.SetForegroundColour(wx.Colour(*kw.NAVY))
        vsz.Add(q, 0, wx.ALIGN_CENTER)
        vsz.AddSpacer(36)

        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(kw.BigButton(root, "YES", GREEN, lambda: self._pick(True),
                             size=(320, 150), font_pt=48), 0, wx.RIGHT, 40)
        row.Add(kw.BigButton(root, "NO", GREY, lambda: self._pick(False),
                             size=(320, 150), font_pt=48))
        vsz.Add(row, 0, wx.ALIGN_CENTER)

        self._countdown = wx.StaticText(root, label="")
        self._countdown.SetFont(wx.Font(16, wx.FONTFAMILY_SWISS,
                                        wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        self._countdown.SetForegroundColour(wx.Colour(*GREY))
        vsz.Add(self._countdown, 0, wx.ALIGN_CENTER | wx.TOP, 20)
        vsz.AddStretchSpacer(3)
        root.SetSizer(vsz)

        if auto_yes_s > 0:
            self._timer = wx.Timer(self)
            self.Bind(wx.EVT_TIMER, self._tick, self._timer)
            self._timer.Start(1000)
            self._update_countdown()

    def _update_countdown(self):
        self._countdown.SetLabel(f"Starting automatically in {self._remaining} s")
        self._countdown.GetParent().Layout()

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
