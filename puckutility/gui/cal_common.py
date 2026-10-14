"""cal_common.py — helpers shared by the calibrate menu mixins."""

import time

import wx

from p4core.reporter import Reporter


def _yield():
    """Let the window repaint during a long wait; a no-op without a wx.App
    (the command line runs these routines headless)."""
    if wx.GetApp() is not None:
        wx.Yield()


def _sleep_responsive(seconds, chunk=0.05):
    """Block for `seconds` seconds while letting wx process pending
    events every `chunk` seconds — keeps Windows from marking the app
    "Not Responding" during long calibration waits."""
    end = time.time() + seconds
    while time.time() < end:
        time.sleep(min(chunk, max(0, end - time.time())))
        _yield()


class GuiCalReporter(Reporter):
    """The p4core Reporter the library calibrations (p4core.calibrate,
    p4sysid) get from the calibrate menu.  They run on the GUI thread, as
    the menu's routines always have: status goes to the status bar, notes
    to stdout (the log window), questions to the frame's _prompt, and every
    wait keeps the window painting.  Without a wx.App (the headless CLI
    adapter) the same calls are just prints and sleeps."""

    interactive = True

    def __init__(self, frame):
        super().__init__()
        self.frame = frame

    def status(self, text):
        print(text)
        try:
            self.frame.frame_statusbar.SetStatusText(text, 1)
            self.frame.frame_statusbar.Update()
        except Exception:
            pass
        _yield()

    def progress(self, pct, text=None):
        # The status bar only: a progress line per sample would flood the log.
        if text:
            try:
                self.frame.frame_statusbar.SetStatusText(text, 1)
            except Exception:
                pass
        _yield()

    def note(self, text):
        print(text)
        _yield()

    def warn(self, text):
        print('WARNING: ' + text)
        _yield()

    def confirm(self, question, default=False):
        title, _, msg = question.partition('\n')
        return bool(self.frame._prompt(title if msg else 'Calibration',
                                       msg or question))

    def check_abort(self):
        _yield()
        super().check_abort()

    def sleep(self, seconds):
        end = time.monotonic() + seconds
        while True:
            self.check_abort()
            left = end - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(0.05, left))
