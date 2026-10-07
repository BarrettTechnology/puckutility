# In-app log viewer shared by the Puck apps (puckutility / pucktuner /
# P4-checkout). Single source of truth -- copied verbatim across the three repos.
#
# Provides a NON-MODAL dialog that live-tails the current session log file from
# inside the app (no external shell, terminal, or editor needed -- so it behaves
# identically on Windows and Linux). It shows the log file location and offers a
# button to open the logs FOLDER in the OS file manager, so users can browse to
# older logs.
#
# Wiring (done in each app):
#   * _setup_logging() calls  log_viewer.set_log_path(log_path)
#   * the frame's __init__ calls  log_viewer.add_log_menu(self)  after its menu
#     bar is set -- that adds a "Log" menu with an "Open Log..." item.

import os
import sys
import subprocess

import wx

# Path of the current session log file, set by each app's _setup_logging() via
# set_log_path(). Stays None until logging is initialized (or if it failed) so
# the apps do not need a shared paths module.
_LOG_PATH = None


def set_log_path(path):
    """Record the current session log file path (called from _setup_logging)."""
    global _LOG_PATH
    _LOG_PATH = path


def get_log_path():
    return _LOG_PATH


def open_in_file_manager(path):
    """Open `path` (file or directory) in the OS file manager. Cross-platform,
    best-effort. Returns True if an opener was launched."""
    try:
        if sys.platform.startswith('win'):
            os.startfile(path)  # noqa: E1101 -- Windows-only attribute
        elif sys.platform == 'darwin':
            subprocess.Popen(['open', path])
        else:
            subprocess.Popen(['xdg-open', path])
        return True
    except Exception as e:
        wx.LogError("Could not open '{}': {}".format(path, e))
        return False


class _LogViewerDialog(wx.Dialog):
    """Non-modal dialog that tails the current log file via a wx.Timer."""

    _POLL_MS = 150            # snappy live updates without busy-polling
    _DOCK_WIDTH = 460         # width of the strip docked beside the app
    # Cap the initial read so opening a huge log is instant; we keep the tail.
    _MAX_INITIAL_BYTES = 256 * 1024

    def __init__(self, parent, log_path):
        super().__init__(parent, title="Log", size=(self._DOCK_WIDTH, 600),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self._log_path = log_path
        self._pos = 0  # byte offset read so far
        self._build_ui()
        self._dock_beside(parent)
        self._timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_tick, self._timer)
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self._append_new()                 # initial load
        self._timer.Start(self._POLL_MS)

    # -- UI -----------------------------------------------------------------
    def _build_ui(self):
        panel = wx.Panel(self)
        vs = wx.BoxSizer(wx.VERTICAL)

        # Location row: label + read-only (selectable) path.
        loc = wx.BoxSizer(wx.HORIZONTAL)
        loc.Add(wx.StaticText(panel, label="Log file:"),
                0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self._path_ctrl = wx.TextCtrl(
            panel, value=self._log_path or "(no log file available)",
            style=wx.TE_READONLY | wx.BORDER_NONE)
        self._path_ctrl.SetBackgroundColour(panel.GetBackgroundColour())
        loc.Add(self._path_ctrl, 1, wx.ALIGN_CENTER_VERTICAL)
        vs.Add(loc, 0, wx.EXPAND | wx.ALL, 8)

        # Log text (monospace, read-only, horizontal scroll, no wrap).
        self._text = wx.TextCtrl(
            panel, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2
                         | wx.HSCROLL | wx.TE_DONTWRAP)
        self._text.SetFont(wx.Font(9, wx.FONTFAMILY_TELETYPE,
                                   wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        vs.Add(self._text, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        # Buttons.
        btns = wx.BoxSizer(wx.HORIZONTAL)
        self._autoscroll = wx.CheckBox(panel, label="Auto-scroll")
        self._autoscroll.SetValue(True)
        btns.Add(self._autoscroll, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 12)

        self._folder_btn = wx.Button(panel, label="Open Log Folder")
        self._folder_btn.SetToolTip("Open the logs folder to browse older logs")
        self._folder_btn.Bind(wx.EVT_BUTTON, self._on_open_folder)
        btns.Add(self._folder_btn, 0, wx.RIGHT, 6)

        self._copy_btn = wx.Button(panel, label="Copy Path")
        self._copy_btn.Bind(wx.EVT_BUTTON, self._on_copy_path)
        btns.Add(self._copy_btn, 0, wx.RIGHT, 6)

        btns.AddStretchSpacer()
        close_btn = wx.Button(panel, wx.ID_CLOSE, "Close")
        close_btn.Bind(wx.EVT_BUTTON, lambda e: self.Close())
        btns.Add(close_btn, 0)
        vs.Add(btns, 0, wx.EXPAND | wx.ALL, 8)

        panel.SetSizer(vs)
        if not self._log_path:
            self._folder_btn.Disable()
            self._copy_btn.Disable()

    # -- placement ----------------------------------------------------------
    def _dock_beside(self, parent):
        """Place the dialog as a tall strip immediately to the right of the
        parent window, matching its height, so both are visible at once. Falls
        back to the left side if there is no room on the right, and always stays
        within the display's work area."""
        try:
            pr = parent.GetScreenRect()
            di = wx.Display.GetFromWindow(parent)
            area = wx.Display(di if di != wx.NOT_FOUND else 0).GetClientArea()
        except Exception:
            return  # keep the default centered size if geometry is unavailable
        y = max(area.y, pr.y)
        height = min(pr.height, area.y + area.height - y)
        right_space = (area.x + area.width) - (pr.x + pr.width)
        left_space = pr.x - area.x
        # Prefer the right of the app; use the left only if it has more room.
        if right_space >= left_space:
            width = min(self._DOCK_WIDTH, max(200, right_space))
            x = pr.x + pr.width
        else:
            width = min(self._DOCK_WIDTH, max(200, left_space))
            x = pr.x - width
        self.SetSize(int(x), int(y), int(width), int(height))

    # -- tailing ------------------------------------------------------------
    def _append_new(self):
        """Append any bytes written to the log since the last read. Handles the
        log being rotated/truncated, and caps the very first read to the tail."""
        path = self._log_path
        if not path:
            return
        try:
            size = os.path.getsize(path)
        except OSError:
            return
        if size < self._pos:                       # rotated / truncated -> reset
            self._text.ChangeValue('')
            self._pos = 0
        if self._pos == 0 and size > self._MAX_INITIAL_BYTES:
            self._pos = size - self._MAX_INITIAL_BYTES  # skip ahead, keep the tail
        if size <= self._pos:
            return
        try:
            with open(path, 'rb') as f:
                f.seek(self._pos)
                data = f.read()
                self._pos = f.tell()
        except OSError:
            return
        if data:
            self._text.AppendText(data.decode('utf-8', errors='replace'))
            if self._autoscroll.GetValue():
                self._text.ShowPosition(self._text.GetLastPosition())

    def _on_tick(self, _evt):
        self._append_new()

    # -- buttons ------------------------------------------------------------
    def _on_open_folder(self, _evt):
        if self._log_path:
            open_in_file_manager(os.path.dirname(self._log_path))

    def _on_copy_path(self, _evt):
        if self._log_path and wx.TheClipboard.Open():
            wx.TheClipboard.SetData(wx.TextDataObject(self._log_path))
            wx.TheClipboard.Close()

    def _on_close(self, _evt):
        self._timer.Stop()
        parent = self.GetParent()
        if getattr(parent, '_log_viewer_dialog', None) is self:
            parent._log_viewer_dialog = None
        self.Destroy()


def show_log_viewer(parent, log_path=None):
    """Open (or raise) the live log viewer. Uses the stored session log path when
    none is supplied. Non-modal, so the user can watch errors while using the app.
    Re-uses a single dialog instance per parent frame."""
    path = log_path if log_path is not None else _LOG_PATH
    existing = getattr(parent, '_log_viewer_dialog', None)
    if existing is not None:
        try:
            existing._dock_beside(parent)  # re-snap beside the app, then raise
            existing.Raise()
            return existing
        except RuntimeError:               # was already destroyed
            parent._log_viewer_dialog = None
    dlg = _LogViewerDialog(parent, path)
    parent._log_viewer_dialog = dlg
    dlg.Show()
    return dlg


def add_log_menu(frame, menu_label="Log", item_label="Open Log...\tCtrl+L"):
    """Append a top-level `menu_label` menu with an 'Open Log...' item to the
    frame's menu bar, wired to show_log_viewer(frame). Call AFTER the frame's
    menu bar has been set. No-op if the frame has no menu bar yet."""
    mb = frame.GetMenuBar()
    if mb is None:
        return
    menu = wx.Menu()
    item = menu.Append(wx.ID_ANY, item_label, "View the current session log")
    mb.Append(menu, menu_label)
    frame.Bind(wx.EVT_MENU, lambda e: show_log_viewer(frame), item)
