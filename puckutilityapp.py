#!/usr/bin/env python3
# -*- coding: UTF-8 -*-
# Dependencies:
#   - python3 -m pip install wxpython canopen semver uptime
#   - Peak PCAN USB hardware
#
# If you are using Windows, you'll need to install PCAN Basic >= 4.2.
#
# If you are using Python >= 3.8 on Linux, you might need to make these changes to wxpython:
# https://github.com/wxGlade/wxGlade/commit/e866729f91363a9c16cc6595e3424a0df817e048


# --- Linux display-environment defaults (set BEFORE GTK initializes) ---------
# Make the fixed-pixel wxGlade layout render correctly on modern GNOME/Wayland.
# Each is overridable from the real environment (setdefault).
#   GDK_BACKEND=x11 : use the X11/XWayland backend, which scales by INTEGER
#       factors and lets the compositor upscale to the desktop's (possibly
#       fractional) scale. The native Wayland backend instead hands GTK
#       *fractional* widget allocations, which squeeze widgets to a few px (the
#       "Negative content height" warnings) and break the fixed-pixel layout.
#       No-op on an X11 session (e.g. Ubuntu 20.04). We deliberately do NOT set
#       GDK_SCALE — forcing an integer scale makes the app the wrong size on
#       fractional-scaled desktops; letting the compositor scale matches better.
#   GTK_THEME=Adwaita:light : force a light theme. The app's labels are plain
#       wx.StaticText with NO explicit colour, so they inherit the theme's text
#       colour; under a dark desktop theme (Yaru-*-dark) that is light text on
#       the app's white panels = invisible/faint. GTK_THEME overrides both
#       gsettings AND the XSETTINGS daemon (which still delivers the dark theme
#       under XWayland even with GSETTINGS_BACKEND=memory), so it is the
#       deterministic fix. Override with your own GTK_THEME if you prefer.
#   GSETTINGS_BACKEND=memory : avoids a fatal GLib-GIO xsettings-schema error on
#       Ubuntu 26 (GNOME removed the 'antialiasing' key the old binary reads).
#   GTK_IM_MODULE / NO_AT_BRIDGE : silence ibus/at-spi noise on Ubuntu 26.
import os, sys
if sys.platform.startswith('linux'):
    os.environ.setdefault('GDK_BACKEND', 'x11')
    os.environ.setdefault('GTK_THEME', 'Adwaita:light')
    os.environ.setdefault('GSETTINGS_BACKEND', 'memory')
    os.environ.setdefault('GTK_IM_MODULE', 'gtk-im-context-simple')
    os.environ.setdefault('NO_AT_BRIDGE', '1')

import wx
import wx.adv
import log_viewer
import splash_screen
from puckutilityapp_gui import puckutilityapp_frame
from calibrate_menu import calibrate
from factory_menu import factory
import OnOffButton
import widgets

import os
import canopen
import can_backend
from canopen.sdo import SdoAbortedError
import platform
import time
import subprocess
import math
import semver
from threading import Thread
import multiprocessing
multiprocessing.freeze_support() 
from canopen_runner import progressbar
from app_settings import AppSettings
from flashp4 import progressbar
import time
import webbrowser
import sys
import math
import datetime
import canopen_runner
from canopen_runner import (
    CLEAR_FAULT, SHUTDOWN, OP_ENABLED,
    MODE_IDLE, MODE_PROFILE_POS, MODE_PROFILE_VEL,
    MODE_PROFILE_TRQ, MODE_HOMING,
)
import csv
import flashp4
import logging

# Puck-specific EMCY fault descriptions (from firmware app/faults.c codes[]). The canopen
# library's get_desc() only knows generic CiA ranges -- e.g. it renders 0x2311 as just
# "Current", which hides what actually tripped. Map the puck's real meanings (+ a remedy for
# the ones a user can act on) and fall back to get_desc() for anything not listed here.
PUCK_FAULT_DESC = {
    0x2310: "current limited (i2t / current-limit active) -- self-recovers",
    0x2311: ("MAGNET DEMAG protection -- reverse d-axis current (-id) exceeded the "
             "temperature-derated safe limit, so the drive stopped to protect the motor "
             "magnets. Trips when driving hard at high speed while the magnet is HOT "
             "(natural field-weakening drives -id negative). REMEDY: let the motor cool, "
             "then clear the fault. This is protection working, not a hardware failure."),
    0x2320: "SHORT CIRCUIT (output stage) -- blown FET / phase short",
    0x3210: "bus OVER-voltage",
    0x3220: "bus UNDER-voltage",
    0x4210: "AMPLIFIER over-temperature",
    0x4310: "MOTOR over-temperature",
    0x7122: "phasing (commutation / encoder alignment)",
    0x7320: "position sensor",
    0x7321: "encoder magnet distance",
    0x8411: "velocity tracking warning",
    0x8414: "velocity tracking fault",
    0x8418: "velocity limit",
    0x8611: "position tracking warning",
    0x8613: "position tracking",
    0x8614: "position tracking fault",
}


def puck_fault_desc(emcy_error):
    """Puck-specific description for an EMCY, falling back to the library's generic text."""
    return PUCK_FAULT_DESC.get(emcy_error.code, emcy_error.get_desc())

# Give the app its own Application User Model ID so Windows groups the
# taskbar entry under our icon instead of the generic python.exe icon.
# Must run before the frame is shown for Windows to honour it.
if sys.platform == 'win32':
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            'Barrett.PuckUtilityApp')
    except Exception:
        pass

# Silence python-can's PCAN warnings — "Bus error: an error counter reached the
# 'heavy'/'warning' limit" floods the terminal/logger when the CAN bus has
# transient errors (e.g. unpowered Puck, marginal cabling). The same condition
# is already handled at the application level via SDO timeouts and the
# "buffer"/"heavy" string checks in scan_pucks; the underlying exception text
# is unaffected by the log level.
logging.getLogger("can.pcan").setLevel(logging.ERROR)
import threading
import wx.lib.agw.pygauge as PG
import argparse
import configparser

def resource_path(relative_path):
    if getattr(sys, 'frozen', False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, relative_path)


def _find_dfu_device():
    """Return True if an STM32 DFU bootloader (0483:df11) is connected."""
    try:
        import usb.core
        return usb.core.find(idVendor=0x0483, idProduct=0xdf11) is not None
    except Exception:
        return False


# TODO

# KNOWN BUGS
# If gainfactor is 0 don't run calc and fail
# Auto focus when coming out of disabled??
# Auto reset cob IDs so configuration isn't required??
# Calibration steps individually still popup issue for multiple cal

# WISH LIST
# Update Calibration procedure to calculate settling time

# NICE TO HAVE 
# Firmware update to flashp4.py to program multiple pucks at once?? - nice to have 

def get_version(vers): # Convert uint32_t to semantic version: Major.Minor.Patch
    return "{0}.{1}.{2}".format(
        (vers >> 24) & 0xFF, (vers >> 8) & 0xFFFF, (vers & 0xFF))

def is_jlink_detected():
    if platform.system() == "Windows":
        jlink_app = "JLink.exe"
        return False # For now, until we can suppress JLink's Windows GUI
    elif platform.system() == "Linux":
        jlink_app =  "JLinkExe"

    string_to_find = "Connecting to J-Link via USB...O.K."
    with subprocess.Popen([jlink_app, "-commandfile", "jlink_detect.txt"], 
                stdout=subprocess.PIPE, bufsize=1, universal_newlines=True) as p:
        for line in p.stdout:
            if line.find( string_to_find ) != -1:
                return True
    return False

# Class for DropTarger
class DropTarget(wx.FileDropTarget):
    def __init__(self,window):
        wx.FileDropTarget.__init__(self)
        self.window = window

    def OnDropFiles(self,x,y,filenames):
        for filepath in filenames:
            wx.CallAfter(self.window.ProcessDroppedFile, filepath)
        return True

# Build class, then set drop target as frame

# wxGlade auto-generates the puckutilityapp_frame's event handler stubs (in wxp3_glade.py).
# We are overriding these stubs with real event handler code here.
# I'd rather use XRC files, but wxGlade 0.9.3 isn't generating event handler bindings for menu items!
# This app's functions are grouped into separate files by responsibility:
#  - puckutilityapp.py: Basic configuration and operation
#  - calibrate_menu.py: Functions related to calibration
#  - factory_menu.py: Functions related to commissioning
# Functions from these files are merged into this class using Python's "mixin" ability.
# Fun fact: Python class "mixins" override from left to right, so put the base class on the right.

class MyFrame(calibrate, factory, puckutilityapp_frame):
    def __init__(self, *args, **kwds):

        # On Windows, native wx.Choice ignores SetMinSize height and native
        # wx.TextCtrl draws text top-aligned regardless of control height.
        # Patch both during the GUI-generated __init__ so combo boxes match
        # text-box height and text is vertically centred in input boxes.
        # The patch is scoped to this constructor so puckutilityapp_gui.py
        # remains untouched and other call sites are unaffected.
        if wx.Platform == '__WXMSW__':
            _orig_choice = wx.Choice
            _orig_textctrl = wx.TextCtrl
            wx.Choice = widgets.WindowsFriendlyChoice
            wx.TextCtrl = widgets.TallTextCtrl
        try:
            puckutilityapp_frame.__init__(self, *args, **kwds)
        finally:
            if wx.Platform == '__WXMSW__':
                wx.Choice = _orig_choice
                wx.TextCtrl = _orig_textctrl

        # Cache references to the cogging compensation ON/OFF radio items on
        # frame_menubar so the handler and startup sync can use them without
        # storing them in puckutilityapp_gui.py.  Find the "Cogging Error
        # Compensation" submenu item (the one with a sub-menu, not the bare
        # calibration entry of the same name), then take the first two items.
        try:
            _items = list(self.frame_menubar.GetMenu(0).GetMenuItems())
            for _it in _items:
                if (_it.GetItemLabel() == "Cogging Error Compensation"
                        and _it.GetSubMenu() is not None):
                    _sub_items = list(_it.GetSubMenu().GetMenuItems())
                    if len(_sub_items) >= 2:
                        self.frame_menubar.COG_ON  = _sub_items[0]
                        self.frame_menubar.COG_OFF = _sub_items[1]
                    break
        except Exception:
            pass

        # ── Cogging compensation greyed out in the menu (2026-06-17) ─────────────
        # Cogging comp is blocked on the noisy velocity-feedback estimate (the FF
        # measured against that loop validates net neutral-to-harmful — see the
        # cogging-comp investigation). DISABLE both menu-0 "Cogging Error Compensation"
        # entries — (a) the ON/OFF submenu (dropdown) and (b) the bare calibration
        # item — so they're greyed and inert until the velocity feedback is fixed.
        #
        # We DISABLE rather than Remove(): the submenu's ON/OFF radio items are cached
        # (frame_menubar.COG_ON/COG_OFF) and referenced in ~10 places. Remove() detaches
        # then GC-destroys them, leaving those refs dangling → use-after-free segfault on
        # the next menu interaction. Enable(False) keeps the items alive (no dangling),
        # greys them, and prevents the submenu from opening. Fully reversible: comment
        # out this block and relaunch to re-enable. (Underlying calibrate_menu.py
        # cogging_* functions are untouched.)
        try:
            _cog_menu0 = self.frame_menubar.GetMenu(0)
            for _cog_it in list(_cog_menu0.GetMenuItems()):
                if _cog_it.GetItemLabel() == "Cogging Error Compensation":
                    _cog_it.Enable(False)
        except Exception:
            pass

        # Frame-level Tab/Shift-Tab interception. EVT_CHAR_HOOK on the focused
        # window bubbles up to the frame; binding here gives us a single hook
        # that fires for keystrokes from ANY control (buttons, choices, the
        # TallTextCtrl inner, etc.), so Tab navigation works from every focus
        # target — not just the TallTextCtrl-bound ones.  Windows-only because
        # GTK already handles Tab natively.
        if wx.Platform == '__WXMSW__':
            self.Bind(wx.EVT_CHAR_HOOK, self._on_tab_nav)

        self._replace_static_texts()
        icons = wx.Icon(resource_path("images/BarrettLogo.png"))
        self.SetBackgroundColour(wx.Colour(255,255,255))
        USE_BUFFERED_DC = True

        # Init drop target
        dt = DropTarget(self)
        self.SetDropTarget(dt)

        # Initialize self variables
        self.encoderResolution = 4096 # cts / revolution
        self.adcWasON = False
        self.lastMode = 0 
        self.firstRun = True
        self.lastPosRad = 0
        self.lastSysTime = 0
        self.y = 0
        self.dirCounter = 0
        #self.NoPattern = 0 # Future implementation to avoid no motor spazzing on dial / rpm count
        self.motorPresent = True
        self.init = True
        self.initialize = []
        self.ID = 0
        self.settingID = False
        self.NetworkActive = True
        self.Rescanning = False
        # CAN auto-reconnect (after a mid-session adapter loss) is BOUNDED so a
        # port that keeps erroring -- e.g. a CANable on an empty/unterminated bus
        # that goes bus-off -- can't loop reconnects + USB resets + popups
        # forever and lock the user out of the port dropdown.
        self._reconnect_timer = None
        self._reconnect_attempts = 0
        self._reconnect_gave_up = False
        self._MAX_RECONNECT = 3
        self.node = None

        self.outputShaft = True

        self.ADC_ON = False

        self.progressbar_EN = True # False
        self.update = []

        # Extra Safety Flags
        self.requireCal = False
        self.requireConfig = False
        # Re-entrancy guard: True while a firmware flash or config upload is in flight. The upload
        # loops call wx.Yield (to keep the UI alive), which lets a second dropped file / task start
        # CONCURRENTLY and collide on the CAN bus -> both fail with SDO aborts. Any task must hold off
        # until the in-flight upload finishes. Set/cleared in browse_fw and file_to_p4.
        self._upload_busy = False

        # Barrett colors
        self.blue = '#253B92'
        self.orange = '#FF7C1B'
        self.gray = '#8C8C8C'

        self.peak_factor = 0.75 # % Peak for Current Colors

        # Setup Window + Icon. On Windows we have to wire the icon through
        # four channels to cover every shell surface; see _set_windows_icon.
        # Linux GTK ignores .ico, so fall back to the .png there. Resolve
        # relative to this file so the icon loads regardless of cwd (the
        # silent except in _set_windows_icon would otherwise swallow a
        # missing-file failure when launched from another directory).
        if sys.platform == 'win32':
            self._set_windows_icon(resource_path(os.path.join('images', 'BarrettIcon.ico')))
        else:
            self.SetIcon(wx.Icon(resource_path(os.path.join('images', 'BarrettIcon.png'))))
        self.SetTitle("Puck Utility - v1.3.0 - DEV")
        self.button_6.SetBackgroundColour(self.gray) # Initialize with gray button in idle
        self.Bind(wx.EVT_CHAR_HOOK, self.onKeyDown)  # EVT_CHAR_HOOK fires before focused child consumes the key
        self.Bind(wx.EVT_KEY_UP, self.onKeyUp)
        self.Bind(wx.EVT_CLOSE, self.onCloseFrame)
        self.Bind(wx.EVT_ACTIVATE, self._on_activate)  # ensure frame has focus for hotkeys
        self.backgroundBMP = wx.Bitmap(resource_path("images/Background.png")) # recreating the BMP each rewrite causes massive lagging this is much better!
        # Bind backgound function to assign bitmap
        self.Bind(wx.EVT_ERASE_BACKGROUND, self.OnEraseBackground)

        # Add button to onoffpanel
        # MAY WANT TO INCREASE THE SIZE OF THIS IN CASE IT GIVES BETTER RESOLUTION
        sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.onoff1 = widgets.TransparentOnOffButton(self.onoffpanel, -1, size=(50, 34), initial=0, border=False, name="2")
        self.onoff1.Bind(OnOffButton.EVT_ON_OFF, self.on_off_adc)
        # Demonstrate individual control adjustments
        self.onoff1.SetOnColour(self.orange) # Barrett Orange
        self.onoff1.SetOnForegroundColour(self.gray) # Barrett Blue
        self.onoff1.SetOffColour(self.blue) # Barret Gray
        self.onoff1.SetOffForegroundColour(self.gray) # Barrett Blue
        self.onoff1.SetToolTip("ADC Monitor ON/OFF")
        sizer.Add(self.onoff1, 0, wx.ALIGN_CENTER)
        self.onoffpanel.SetSizer(sizer)
        self.onoffpanel.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.onoffpanel.Bind(wx.EVT_ERASE_BACKGROUND, lambda e: None)
        self.onoffpanel.Bind(wx.EVT_PAINT, self._paint_onoffpanel)

        # Replace wx.StaticBitmap Dial with transparent version
        self._dial_base_img = wx.Image(resource_path("images/dialnobgcroppedscaled.png"))
        dial_bmp = wx.Bitmap(self._dial_base_img)
        new_dial = widgets.TransparentBitmap(self, wx.ID_ANY, dial_bmp)
        new_dial.SetMinSize(self.Dial.GetMinSize())
        dial_sizer = self.Dial.GetContainingSizer()
        if dial_sizer:
            dial_sizer.Replace(self.Dial, new_dial)
        self.Dial.Destroy()
        self.Dial = new_dial
        self.Layout()

        # Disable the unimplemented menu items
        menu = "Menu"
        for item in [#"Calibrate All", 
          "Encoder Direction", # "Current Sense Slope", "Current Sense Timing", # Comment out to enable those cal features
          "Tune Gains...", "Save to CSV..."]:
          menu_item = self.frame_menubar.FindMenuItem(menu, item)
          self.frame_menubar.Enable(menu_item, False)

        menu = "Factory"
        for item in ["Initialize Puck", "Test All", "Test Flash", "Test RAM", "Test EEPROM", "Test Amplifier", "Test Encoder"]:
          menu_item = self.frame_menubar.FindMenuItem(menu, item)
          self.frame_menubar.Enable(menu_item, False)

        # Hide the "Factory" menu if JLink is not detected
        # if not is_jlink_detected():
        self.frame_menubar.Remove(self.frame_menubar.FindMenu("Factory"))

        # Add a "Log" menu (Open Log...) that opens the in-app live log viewer.
        log_viewer.add_log_menu(self)

        self.progress = PG.PyGauge(self.frame_statusbar, range=100, style=wx.ALIGN_CENTER_VERTICAL | wx.ALL)
        self.progress.SetBarGradient(('#FFFFFF',self.orange))
        self.dc = wx.ScreenDC()

        # Initial Positioniing
        self.RepositionGauge()

        # Intercept field-1 status writes: keep gauge and text from competing.
        # "Progress: X%" text  → show gauge (UpdateUI may call this while gauge is hidden).
        # Any other field-1 text → hide gauge so the full field is available for the text.
        _orig_set_status = self.frame_statusbar.SetStatusText
        def _status_sync_gauge(text, number=0):
            if number == 1:
                if text.startswith("Progress:"):
                    if not self.progress.IsShown():
                        self.progress.Show()
                        self.frame_statusbar.Refresh()
                else:
                    if self.progress.IsShown():
                        self.progress.Hide()
                        self.frame_statusbar.Refresh()
            _orig_set_status(text, number)
        self.frame_statusbar.SetStatusText = _status_sync_gauge

        # NOW need to work on pass the update thread into other programs??
        self.update_queue = multiprocessing.Queue()

        self._install_can_error_hook()

    def _install_can_error_hook(self):
        """Intercept unhandled thread exceptions from python-can's notifier.

        When the USB CAN adapter is disconnected mid-session, the can.Notifier
        receive thread raises CanOperationError (ENODEV=19) or hits ENOBUFS=105
        on a full tx buffer.  Without this hook Python prints a noisy
        'Exception in thread' traceback for every affected background thread.
        The hook catches those specific errors, suppresses the traceback, and
        schedules a clean reconnect attempt on the wx main thread.
        """
        import can as _can
        _frame        = self
        _original     = threading.excepthook

        def _hook(args):
            exc         = args.exc_value
            thread_name = getattr(args.thread, 'name', '') or ''
            # Any CanOperationError or OSError from the can.notifier receive
            # thread is a device-loss event (unplug, bus-off, buffer full).
            # Check thread name rather than specific errno values — Linux
            # produces different codes depending on how the adapter disappears
            # (ENODEV=19, ENETDOWN=100, ENXIO=6, ENOBUFS=105, etc.).
            if 'can.notifier' in thread_name and isinstance(exc, (_can.CanOperationError, OSError)):
                if not _frame.Rescanning:
                    wx.CallAfter(_frame._on_can_device_lost)
            else:
                _original(args)

        threading.excepthook = _hook

    def _replace_network(self, can_device, bitrate=1_000_000):
        """Tear down any existing self.network so its underlying bus releases
        its USB handles cleanly, then build a new one through can_backend.

        Without the explicit disconnect, an old CandlelightBus instance gets
        dropped without bus.shutdown() being called -- it then lingers in
        Python's GC, and pyusb's finalizer eventually crashes the process
        with an access violation on libusb_unref_device. canopen.Network's
        disconnect() does call bus.shutdown(), which releases the USB
        interface and disposes the device wrapper before GC sees it.
        """
        old = getattr(self, 'network', None)
        if old is not None:
            try:
                old.disconnect()
            except Exception:
                pass
            # Drop the reference so the old bus is collectable now that its
            # underlying USB state has been cleanly released.
            self.network = None
        import can_backend
        self.network = can_backend.make_network(can_device, bitrate=bitrate)
        return self.network

    def _on_can_device_lost(self):
        """Runs on the wx main thread when the CAN adapter is unexpectedly lost."""
        if self.Rescanning:
            return
        # Only auto-reconnect when a node was previously active. Without this
        # guard, bus-off errors fired by the CAN controller during an empty
        # scan (no ACK receivers on Ubuntu 26+) loop indefinitely even though
        # there is nothing to reconnect to.
        if self.node is None:
            return
        # Don't stack reconnects: if one is already pending, ignore further
        # notifier errors until it fires.
        if self._reconnect_timer is not None and self._reconnect_timer.IsRunning():
            return
        # BOUNDED: after a few failed attempts, give up quietly (status text only,
        # no modal) instead of looping resets/popups. The user re-selecting the
        # port resets this counter (see can_port).
        if self._reconnect_attempts >= self._MAX_RECONNECT:
            if not self._reconnect_gave_up:        # surface the message once
                self._reconnect_gave_up = True
                self.frame_statusbar.SetStatusText(
                    'CAN adapter lost — reconnect failed; re-select the port to retry', 1)
                self.frame_statusbar.Refresh()
                self.frame_statusbar.Update()
            return
        self.Rescanning = True
        self._reconnect_attempts += 1
        print('CAN device lost — reconnect attempt {}/{}…'.format(
            self._reconnect_attempts, self._MAX_RECONNECT))
        self.frame_statusbar.SetStatusText('CAN device lost — reconnecting…', 1)
        self.frame_statusbar.Refresh()
        self.frame_statusbar.Update()
        # Wait 2 s to allow USB re-enumeration if the cable was replugged, then
        # retry. auto_reconnect=True keeps it quiet (no modal dialog, no USB
        # reset) so a still-failing port can't spam the user.
        self._reconnect_timer = wx.CallLater(
            2000, self.can_port, None, auto_reconnect=True)

    def _paint_onoffpanel(self, event):
        panel = self.onoffpanel
        dc = wx.PaintDC(panel)
        pos = self.ScreenToClient(panel.GetScreenPosition())
        dc.DrawBitmap(self.backgroundBMP, -pos.x, -pos.y)

    def _on_tab_nav(self, event):
        # Frame-level Tab/Shift-Tab interception. Fires for the focused window
        # via EVT_CHAR_HOOK regardless of which control has focus, so Tab works
        # from buttons, choices, and TallTextCtrl inners alike. Routes through
        # widgets._navigate_to_sibling to avoid wx's broken default navigation.
        if event.GetKeyCode() == wx.WXK_TAB:
            focused = wx.Window.FindFocus()
            if focused is not None:
                # If focus is inside a TallTextCtrl wrapper, navigate from the
                # wrapper itself — its inner is the wrapper's only child, so
                # navigating from the inner finds no siblings.
                nav_from = focused
                parent = focused.GetParent()
                if isinstance(parent, widgets.TallTextCtrl):
                    nav_from = parent
                forward = not event.ShiftDown()
                wx.CallAfter(widgets._navigate_to_sibling, nav_from, forward)
                return
        event.Skip()

    def _replace_static_texts(self):
        """Swap every wx.StaticText child with a TransparentText in-place."""
        instance_attrs = {id(getattr(self, a)): a
                         for a in ('VBus', 'PTemp', 'MTemp', 'Vrpm')
                         if isinstance(getattr(self, a, None), wx.StaticText)}
        for child in list(self.GetChildren()):
            if not isinstance(child, wx.StaticText):
                continue
            style = child.GetWindowStyle() & (wx.ALIGN_CENTER_HORIZONTAL | wx.ALIGN_RIGHT)
            new = widgets.TransparentText(self, wx.ID_ANY, child.GetLabel(), style=style)
            new.SetFont(child.GetFont())
            new.SetForegroundColour(wx.BLACK)  # don't copy system colour — may be white on some Ubuntu themes
            new.SetMinSize(child.GetMinSize())
            sizer = child.GetContainingSizer()
            if sizer:
                sizer.Replace(child, new)
            attr = instance_attrs.get(id(child))
            if attr:
                setattr(self, attr, new)
            child.Destroy()
        self.Layout()

    def set_tool_tips(self,event):
        wx.ToolTip.SetDelay(3000)
        wx.ToolTip.SetReshow(3000)
        # if self.getMode() == 'Current':
            # Specifications
        # print('Setting Tool Tips...')   # cosmetic UI wiring -- no diagnostic value
        self.choice_port.SetToolTip('Select CAN port')
        self.settings = AppSettings("puckutility")   # persistent "memory" of user defaults
        _saved_port = self.settings.get("can_port")
        if _saved_port:
            try:
                self.choice_port.SetStringSelection(_saved_port)
            except Exception:
                pass
        self.button_1.SetToolTip('Scan to find all Pucks on the CAN bus')
        self.choice_id.SetToolTip('Select active Puck')
        self.text_version.SetToolTip('Firmware version of active Puck')
        self.button_8.SetToolTip('Update firmware for active Puck')
        self.button_10.SetToolTip('Browse for a new configuration file to upload')
        self.choice_test.SetToolTip('Select control mode')
        self.text_testvalue.SetToolTip('Input a command value for the control mode')
        self.button_6.SetToolTip('Send the command value to active Puck')
        self.Dial.SetToolTip('Displays output position')
        self.onoffpanel.SetToolTip('Turn ON/OFF ADC Monitor')
        self.button_2.SetToolTip('Set new Puck ID')
        self.text_id.SetToolTip('Input new Puck ID')

    #this may be unnecessary
    # def OnResize(self,event):
    #     self.RepositionGauge()
    #     event.Skip()

    def RepositionGauge(self):
        rect = self.frame_statusbar.GetFieldRect(1)
        # print('repo')
        # Get text width and add this to the start spot!!
        # get the actual in use text for width? or just make smaller so it doesn't block text?
        text = "Progress: 100%"
        width, height = self.dc.GetTextExtent(text)
        self.progress.SetPosition((rect.x + 20 + width, int(rect.y * 2 + 2)))
        self.progress.SetSize((rect.width - 6, rect.height - 8))

    def UpdateProgress(self,value):
        # print('called')
        if value > 100:
            value = 100
        self.progress.SetValue(value)

    def OnStartTask(self,event):
        # Set color
        # print('Start')
        # if self.progressbar_EN == True:
        self.frame_statusbar.SetStatusText(f"Progress: 0%",1)
        self.progress.Show()
        self.GetStatusBar().Refresh()
        self.GetStatusBar().Update()
        if self.adcWasON == True: # THIS IS ALWAYs changing?? i think at least? either way, not picking up the difference
            self.progress.SetBarGradient(('#FFFFFF',self.blue))
            # print('on')
        else:
            self.progress.SetBarGradient(('#FFFFFF',self.orange))
            # print('off')
        
    def OnTaskComplete(self):
        # self.thread.join()
        self.progress.Hide()
        self.UpdateProgress(0)
        self.frame_statusbar.SetStatusText("Ready", 1)

    def UpdateUI(self,value):
        # SetStatusText already invalidates the field it writes to; PyGauge
        # does NOT auto-refresh on SetValue. Refresh+Update on the gauge
        # alone (NOT the whole status bar) draws synchronously without the
        # flicker the original whole-bar refresh produced. The synchronous
        # Update() is required so the bar paints even when the window has
        # lost focus — Refresh() alone only queues a WM_PAINT, which
        # Windows deprioritises on background windows.
        self.frame_statusbar.SetStatusText(f"Progress: {value}%",1)
        self.UpdateProgress(value)
        self.progress.Refresh(eraseBackground=False)
        # PyGauge.Update(value, time) is the ANIMATED-update method and
        # shadows wx.Window.Update() (the force-immediate-paint one we
        # actually want here). Call the base-class method explicitly.
        wx.Window.Update(self.progress)
        # self.Refresh()
        # self.Update()
        # force refresh to help windows?

    def _set_windows_icon(self, ico_relpath):
        # Wire the icon through every Windows shell surface; each one falls
        # back to python.exe's icon if the channel below isn't set:
        #   wx.IconBundle / SetIcons        -> title bar, taskbar, Alt-Tab
        #   WM_SETICON ICON_SMALL2          -> thumbnail-preview small icon
        #   SetClassLongPtrW HICON/HICONSM  -> shell UIs that read the class icon
        #   IPropertyStore (AppUserModel)   -> thumbnail-preview group icon
        self.SetIcons(wx.IconBundle(ico_relpath, wx.BITMAP_TYPE_ICO))

        import ctypes
        from ctypes import wintypes
        ico_path = os.path.abspath(ico_relpath)
        hwnd = int(self.GetHandle())
        is64 = ctypes.sizeof(ctypes.c_void_p) == 8

        try:
            user32 = ctypes.windll.user32
            user32.LoadImageW.restype = ctypes.c_void_p
            user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                            ctypes.c_void_p, ctypes.c_void_p]
            user32.SendMessageW.restype = ctypes.c_void_p
            IMAGE_ICON, LR_LOADFROMFILE, WM_SETICON = 1, 0x10, 0x0080
            hicon_small = user32.LoadImageW(None, ico_path, IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
            hicon_big = user32.LoadImageW(None, ico_path, IMAGE_ICON, 32, 32, LR_LOADFROMFILE)
            for which, h in ((0, hicon_small), (2, hicon_small), (1, hicon_big)):
                user32.SendMessageW(hwnd, WM_SETICON, which, h)
            GCLP_HICON, GCLP_HICONSM = -14, -34
            if is64:
                user32.SetClassLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                                    ctypes.c_void_p]
                user32.SetClassLongPtrW.restype = ctypes.c_void_p
                user32.SetClassLongPtrW(hwnd, GCLP_HICON, hicon_big)
                user32.SetClassLongPtrW(hwnd, GCLP_HICONSM, hicon_small)
            else:
                user32.SetClassLongW(hwnd, GCLP_HICON, hicon_big)
                user32.SetClassLongW(hwnd, GCLP_HICONSM, hicon_small)
        except Exception:
            pass

        try:
            class GUID(ctypes.Structure):
                _fields_ = [('Data1', wintypes.DWORD), ('Data2', wintypes.WORD),
                            ('Data3', wintypes.WORD), ('Data4', ctypes.c_ubyte * 8)]

            class PROPERTYKEY(ctypes.Structure):
                _fields_ = [('fmtid', GUID), ('pid', wintypes.DWORD)]

            class PROPVARIANT(ctypes.Structure):
                _fields_ = [('vt', wintypes.WORD), ('wReserved1', wintypes.WORD),
                            ('wReserved2', wintypes.WORD), ('wReserved3', wintypes.WORD),
                            ('pwszVal', ctypes.c_wchar_p),
                            ('padding', ctypes.c_byte * 8)]

            AUM_FMTID = GUID(0x9F4C2855, 0x9F79, 0x4B39,
                             (ctypes.c_ubyte * 8)(0xA8, 0xD0, 0xE1, 0xD4,
                                                  0x2D, 0xE1, 0xD5, 0xF3))
            PKEY_AUM_ID = PROPERTYKEY(AUM_FMTID, 5)
            PKEY_AUM_RelaunchIcon = PROPERTYKEY(AUM_FMTID, 3)
            IID_IPropertyStore = GUID(0x886D8EEB, 0x8CF2, 0x4446,
                                      (ctypes.c_ubyte * 8)(0x8D, 0x02, 0xCD, 0xBA,
                                                           0x1D, 0xBD, 0xCF, 0x99))
            VT_LPWSTR = 31

            shell32 = ctypes.windll.shell32
            shell32.SHGetPropertyStoreForWindow.argtypes = [
                wintypes.HWND, ctypes.POINTER(GUID),
                ctypes.POINTER(ctypes.c_void_p)]
            shell32.SHGetPropertyStoreForWindow.restype = ctypes.HRESULT
            pps = ctypes.c_void_p()
            if shell32.SHGetPropertyStoreForWindow(
                    hwnd, ctypes.byref(IID_IPropertyStore),
                    ctypes.byref(pps)) != 0:
                return

            vtbl = ctypes.cast(pps, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
            SetValue = ctypes.WINFUNCTYPE(
                ctypes.HRESULT, ctypes.c_void_p,
                ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT)
            )(vtbl[6])
            Commit = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p)(vtbl[7])
            Release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])

            pv_id = PROPVARIANT(); pv_id.vt = VT_LPWSTR
            pv_id.pwszVal = 'Barrett.PuckUtilityApp'
            SetValue(pps, ctypes.byref(PKEY_AUM_ID), ctypes.byref(pv_id))

            pv_icon = PROPVARIANT(); pv_icon.vt = VT_LPWSTR
            pv_icon.pwszVal = ico_path + ',0'
            SetValue(pps, ctypes.byref(PKEY_AUM_RelaunchIcon), ctypes.byref(pv_icon))

            Commit(pps)
            Release(pps)
        except Exception:
            pass

    def _busy_reject(self, what="operation"):
        """Return True (and warn) when a firmware flash or config upload is in flight. Callers bail
        so NO task runs concurrently with an upload -- the upload loops call wx.Yield, so a button/
        menu/drop can otherwise re-enter mid-upload and collide on the CAN bus (FLASH_FAILED / SDO
        aborts, then a cal against a half-written puck)."""
        if getattr(self, '_upload_busy', False):
            print("Upload in progress — {} deferred until it finishes.".format(what))
            try:
                dlg = wx.MessageDialog(None,
                                       "An upload is in progress.\n\nWait for it to finish before "
                                       "starting another operation.",
                                       "Upload in progress", wx.OK | wx.ICON_INFORMATION)
                dlg.ShowModal(); dlg.Destroy()
            except Exception:
                pass
            return True
        return False

    def ProcessDroppedFile(self,filepath):
        # HOLD OFF: never start a new upload/task while a flash or config upload is already in flight.
        if self._busy_reject("dropped file '{}'".format(os.path.basename(filepath))):
            return
        # print(filepath)
        root, extension = os.path.splitext(filepath)
        # print(extension)
        if extension == '.ebin' or extension == '.bin':
            print('P4 Firmware Detected...')
            self.browse_fw(None,filepath)
            # Run firmware upload
        elif extension == '.csv':
            print('Motor Configuration Detected...')
            # Run configuration upload
            self.file_to_p4(None,filepath)
        elif extension == '.ini':
            print('System Configuration Detected...')
            start = time.time()
            self.system_config(None,filepath)
            finish = time.time()
            time_elapsed = round(finish - start,2)
            print('System Configuration Complete! Time elapsed: {} seconds'.format(time_elapsed))
        else:
            print('Invalid file...')
            # add a popup
            msg = 'Invalid file type! \n\n\nExpected Extensions-\nFirmware: ".ebin"\nMotor Config: ".csv"\nSystem Config: ".ini"'
            dlg = wx.MessageDialog(None,msg,'Warning!', wx.ICON_WARNING)
            dlg.ShowModal()
            dlg.Destroy()

    def OnEraseBackground(self, evt):
        # yanked from ColourDB.py
        dc = evt.GetDC()

        if not dc:
            dc = wx.ClientDC(self)
            rect = self.GetUpdateRegion().GetBox()
            dc.SetClippingRect(rect)
        dc.Clear()
        dc.DrawBitmap(self.backgroundBMP, 0, 0)

    # HOTKEYS

    def _on_activate(self, event):
        # On GTK, EVT_CHAR_HOOK only fires when the top-level frame is the active
        # (focused) window. Grab focus on activation so hotkeys work immediately
        # without requiring a click on the frame background first.
        if event.GetActive():
            self.SetFocus()
        event.Skip()

    def onKeyUp(self, event):
        event.Skip()

    def onKeyDown(self,event):
        # https://archie-adams.github.io/keyboard-shortcut-map-maker/ to make map!
        # print(event.GetKeyCode())# Use to print key code
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self.onCloseFrame(None)
        elif event.ControlDown() and event.GetKeyCode() == 67: # CTRL-C = Cal
            self.calibrate_all(None)
        elif event.ControlDown() and event.GetKeyCode() == 85: # CTRL-U = Update
            self.update_all(None)
        elif event.ControlDown() and event.GetKeyCode() == 83: # CTRL-S = Scan
            self.scan_pucks(None)
        elif event.ControlDown() and event.GetKeyCode() == 80: # CTRL-P = Play/Pause ADC Monitor
            if self.ADC_ON == False:
                self.onoff1.SetValue(1)
            elif self.ADC_ON == True:
                self.onoff1.SetValue(0)
            self.on_off_adc(self)
        else:
            event.Skip()
            return
    
    def setID(self,i):
        self.ID = i

    def getID(self):
        return self.ID

    def _wait_for_node(self, node_id, timeout=4.0, interval=0.15):
        """Poll a single CAN node until it answers an SDO read, or until timeout.
        Used after a node-ID change so the UI re-scans only once the puck has
        actually rebooted onto the new ID. Cheap on the bus -- one small SDO
        request per try -- unlike a full scanner.search() over all 127 IDs."""
        try:
            node = (self.network[node_id] if node_id in self.network
                    else self.network.add_node(node_id, 'puck4.eds'))
        except Exception:
            return False
        _timeout = canopen.sdo.SdoClient.RESPONSE_TIMEOUT
        canopen.sdo.SdoClient.RESPONSE_TIMEOUT = interval
        try:
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    node.sdo['MfgSoftwareVersion'].raw
                    return True
                except Exception:
                    time.sleep(0.03)
            return False
        finally:
            canopen.sdo.SdoClient.RESPONSE_TIMEOUT = _timeout

    def configure_Puck(self, configure_pdos=True):
        print("Configuring Puck...")

        # Read and set gear ratio from object dictionary
        motor_rev = self.node.sdo.upload(0x6091,1)
        motor_rev = int.from_bytes(motor_rev, byteorder='little',signed=False)
        shaft_rev = self.node.sdo.upload(0x6091,2)
        shaft_rev = int.from_bytes(shaft_rev, byteorder='little',signed=False)
        self.gearRatio = motor_rev / shaft_rev

        self.i_cont = self.node.sdo.upload(0x3011,8)
        self.i_cont = int.from_bytes(self.i_cont, byteorder='little',signed=False)
        print('I_cont: {}'.format(self.i_cont))
        self.i_peak = self.node.sdo.upload(0x3011,9)
        self.i_peak = int.from_bytes(self.i_peak, byteorder='little',signed=False)

        self.temp_limit = self.node.sdo.upload(0x2384,9)
        self.temp_limit = int.from_bytes(self.temp_limit, byteorder='little',signed=False)

        self.temp_limited_current = self.node.sdo.upload(0x3025,3)
        self.temp_limited_current = int.from_bytes(self.temp_limited_current, byteorder='little',signed=False)

        # Get peak velocity
        self.peak_velocity = self.node.sdo.upload(0x6080,0)
        self.peak_velocity = int.from_bytes(self.peak_velocity, byteorder='little',signed=False)

        print('Gear Ratio determined: {}'.format(self.gearRatio))

        try:
            self.node.tpdo.read()
            self.node.rpdo.read()
        except Exception as e:
            print(f"Failed to read PDOs: {e!r}")

        if self.node.rpdo[1].cob_id is None:
            self.node.rpdo[1].cob_id = 0x200 + self.node.id
        if self.node.rpdo[2].cob_id is None:
            self.node.rpdo[2].cob_id = 0x300 + self.node.id

        # NEW

        if configure_pdos:
            try:
                self.node.tpdo.read()
                self.node.tpdo[3].clear()
                self.node.tpdo[3].add_variable('Amplifier', 'Temperature')
                self.node.tpdo[3].add_variable('Motor', 'Therm')
                self.node.tpdo[3].trans_type = 10
                self.node.tpdo[3].enabled = True
                # TPDO4: Motor.id on every SYNC — the cal fast-read reads it under continuous SYNC.
                # PDO mapping only changes while not operational, so it's set here at configure time.
                self.node.tpdo[4].clear()
                self.node.tpdo[4].add_variable('Motor', 'id')
                self.node.tpdo[4].trans_type = 1
                self.node.tpdo[4].enabled = True
                self.node.tpdo.save()
            except Exception as e:
                print(f"Failed to set up TPDOs: {e}")
            finally:
                # Re-sync local map with what firmware actually accepted.
                # If save() was rejected, this prevents a local/firmware mismatch
                # that would cause Motor.Therm to read the wrong bytes in TPDO[3].
                try:
                    self.node.tpdo.read()
                except Exception:
                    pass

            self.node.tpdo[1].callbacks.clear()
            self.node.tpdo[2].callbacks.clear()
            self.node.tpdo[3].callbacks.clear()
            self.node.emcy.callbacks.clear()
            self.node.tpdo[1].add_callback(self.tpdo1_callback)
            self.node.tpdo[2].add_callback(self.tpdo2_callback)
            self.node.tpdo[3].add_callback(self.tpdo3_callback)
            self.node.emcy.add_callback(self.on_emcy_received)

            try:
                self.node.rpdo[2].clear()
                self.node.rpdo[2].add_variable('TargetVelocity')  # 0x60FF, 32-bit
                self.node.rpdo[2].add_variable('TargetPosition')  # 0x607A, 32-bit
                self.node.rpdo[2].enabled = True
                self.node.rpdo.save()
            except Exception as e:
                print(f"Failed to configure RPDOs: {e}")

        # Each time we receive this PDO from the puck, execute a callback
        # node.tpdo[1].add_callback(self.tpdo1_callback)
        # node.tpdo[2].add_callback(self.tpdo2_callback)
        # node.tpdo[3].add_callback(self.tpdo3_callback)
        # self.node.tpdo[4].add_callback(self.tpdo4_callback)

        self.node.sdo["HeartbeatPeriod"].raw = 0

        # Sync the Encoder Error Compensation menu to the puck's current state.
        try:
            comp_on = bool(self.node.sdo[0x3027][1].raw)
            self.frame_menubar.ON.Check(comp_on)
            self.frame_menubar.OFF.Check(not comp_on)
        except Exception:
            pass

        # Sync the Cogging Compensation menu to the puck's current state.
        try:
            cog_on = bool(self.node.sdo[0x3028][1].raw)
            self.frame_menubar.COG_ON.Check(cog_on)
            self.frame_menubar.COG_OFF.Check(not cog_on)
        except Exception:
            pass

    def tpdo1_callback(self, msg):
        global node

        # Call function to update Position / Velocity Data
        wx.CallAfter(self.getPosition)

    def tpdo2_callback(self, msg):
        global node

        # Call function to update ADC Monitor
        wx.CallAfter(self.getMonitor)

    def tpdo3_callback(self, msg):
        global node

        # Call function to update ADC Monitor
        wx.CallAfter(self.getMonitor)

    def tpdo4_callback(self, msg):
        global node

        # Call function to update Position / Velocity Data
        wx.CallAfter(self.getPosition)

    def on_emcy_received(self, emcy_error):
        # Called from the canopen receiver thread — only wx.CallAfter is safe here.
        code     = emcy_error.code
        register = emcy_error.register

        # Error reset — puck cleared its fault/warning state
        if code == 0x0000:
            wx.CallAfter(self.frame_statusbar.SetStatusText, 'Ready', 1)
            return

        # EMCY codes to suppress entirely — no status bar update, no mode change.
        # Add codes here as they are identified from puck behaviour in the field.
        SUPPRESSED_CODES = {
            0x8418,  # Velocity tracking — puck self-recovers, no action needed
        }
        if code in SUPPRESSED_CODES:
            print(f"EMCY {hex(code)} suppressed: {emcy_error.get_desc()}")
            return

        # EMCY codes that are warnings even when the error register is non-zero.
        # Add codes here as they are identified from puck behaviour in the field.
        WARNING_CODES = {
            0x2310,  # Current — puck self-recovers, does not leave operational state
        }

        # In CANopen the Error Register (0x1001) is set when the device has
        # entered a fault state.  A warning EMCY is sent with register == 0
        # because the device is still operational.  Any non-zero register
        # means a real fault that requires the drive to stop.
        is_fault = (register != 0x00) and (code not in WARNING_CODES)
        prefix    = "Fault" if is_fault else "Warning"
        error_msg = f"{prefix} {hex(code)}: {puck_fault_desc(emcy_error)}"

        print(error_msg)
        if emcy_error.data:
            print(f"  EMCY data: {emcy_error.data.hex()}")
        wx.CallAfter(self.frame_statusbar.SetStatusText, error_msg, 1)

        if code in (0x3210, 0x3220):  # Voltage fault — read bus voltage vs. limits
            def _log_voltage():
                try:
                    bus_v = self.node.sdo['Amplifier']['BusVoltage'].raw
                    nom_v = self.node.sdo['Amp']['NominalBusVoltage'].raw
                    min_v = self.node.sdo['Object2384']['AmplifierMinVoltage'].raw
                    max_v = self.node.sdo['Object2384']['AmplifierMaxVoltage'].raw
                    print(f"  BusVoltage={bus_v}  Nominal={nom_v}  "
                          f"Min={min_v}  Max={max_v}")
                except Exception as _e:
                    print(f"  Could not read voltage details: {_e}")
            wx.CallAfter(_log_voltage)

        if code in (0x2310, 0x2320):  # Current fault — read current feedback vs. limit
            def _log_current():
                try:
                    iq = self.node.sdo['CurrentFeedback'].raw
                    i_peak = self.node.sdo['Calibration']['i_peak'].raw
                    print(f"  CurrentFeedback={iq} mA  i_peak={i_peak} mA")
                except Exception as _e:
                    print(f"  Could not read current details: {_e}")
            wx.CallAfter(_log_current)

        if code in (0x4210, 0x4310):  # Temperature fault — read temperature vs. limit
            def _log_temp():
                try:
                    temp   = self.node.sdo['Amplifier']['Temperature'].raw
                    max_t  = self.node.sdo['Object2384']['AmplifierMaxTemperature'].raw
                    print(f"  Temperature={temp} °C  AmplifierMaxTemperature={max_t} °C")
                except Exception as _e:
                    print(f"  Could not read temperature details: {_e}")
            wx.CallAfter(_log_temp)

        if is_fault:
            # SetSelection on wxGTK fires EVT_CHOICE, which would trigger
            # select_test and send SDO commands. The _emcy_selection flag
            # tells select_test to ignore this programmatic change.
            def _set_idle():
                self._emcy_selection = True
                self.choice_test.SetSelection(0)
                self._emcy_selection = False
            wx.CallAfter(_set_idle)


    @staticmethod
    def _reset_can_usb(can_device):
        """Reset the USB CAN adapter backing a SocketCAN interface via pyusb.

        Walks sysfs to find the USB device for the given interface, then issues
        a USB-level reset.  Requires the user to be in the plugdev group (standard
        on Ubuntu/Debian) — no sudo needed.  Returns True if a reset was issued.
        """
        if platform.system() != 'Linux':
            return False
        try:
            import usb.core
            sysfs = os.path.realpath(f'/sys/class/net/{can_device}')
            path  = sysfs
            for _ in range(12):
                path = os.path.dirname(path)
                if os.path.exists(os.path.join(path, 'idVendor')):
                    vid = int(open(os.path.join(path, 'idVendor')).read().strip(), 16)
                    pid = int(open(os.path.join(path, 'idProduct')).read().strip(), 16)
                    dev = usb.core.find(idVendor=vid, idProduct=pid)
                    if dev:
                        dev.reset()
                        return True
                    break
        except Exception:
            pass
        return False

    def _recover_can_adapter(self, can_device):
        """USB-reset a wedged CANable and reopen. When the bus power is cycled while the app is
        open, the CANable's TX queue fills (frames the powered-off puck never ACKed), so every
        subsequent SEND fails with ENOBUFS — the interface still opens, so can_port's open-failure
        recovery never fires. A USB re-enumeration is the only thing that flushes that queue.
        Called from scan_pucks when a rescan hits the TX-buffer error, so the user doesn't have to
        close/reopen the app. Returns True if the bus reopened. Best-effort, no sudo; CANable only."""
        if not can_backend.iface_is_candlelight(can_device):
            return False
        try:
            self.network.disconnect()   # stop RX thread before the reset re-enumerates the link
        except Exception:
            pass
        if not self._reset_can_usb(can_device):
            return False
        print('USB-resetting CAN adapter (TX buffer wedged after bus power-cycle)…')
        _deadline = time.monotonic() + 1.5
        while time.monotonic() < _deadline:
            try:
                wx.SafeYield()
            except Exception:
                pass
            time.sleep(0.05)
        try:
            self._replace_network(can_device, bitrate=1000000)
            self.network.scanner.reset()
            self.network.scanner.search()
            print('CAN reset recovered the adapter — rescanning…')
            return True
        except Exception:
            return False

    def can_port(self,event,skipADC=False,silent=False,auto_reconnect=False):
        self.Rescanning = False
        # Cancel any pending auto-reconnect: this connect attempt supersedes it.
        if self._reconnect_timer is not None:
            try:
                self._reconnect_timer.Stop()
            except Exception:
                pass
        # A user-initiated connect (selecting a port, pressing Scan) is a fresh
        # start -- reset the bounded auto-reconnect counter so re-selecting a
        # port always retries.
        if not auto_reconnect:
            self._reconnect_attempts = 0
            self._reconnect_gave_up = False
        if skipADC == True:
            pass
        elif self.ADC_ON == True:
            self.on_off_adc(self)
            self.adcWasON = True
        else:
            self.adcWasON = False

        try:
            self.network.disconnect() # Close any open networks
        except:
            pass

        print("Establishing a new network...")
        can_device = self.choice_port.GetStringSelection()

        try:
            self._replace_network(can_device, bitrate=1000000)
            # This will attempt to read an SDO from nodes 1 - 127
            self.network.scanner.reset()
            self.network.scanner.search()
        #   return True
        except Exception as e:
            print(f'CAN connect failed ({e}) — attempting USB reset…')
            self.frame_statusbar.SetStatusText('CAN error — resetting adapter…', 1)
            self.frame_statusbar.Refresh()
            self.frame_statusbar.Update()
            # Disconnect before resetting USB so the notifier thread stops
            # cleanly — without this the thread hits ENODEV when the device
            # disappears and can trigger error callbacks that close the frame.
            try:
                self.network.disconnect()
            except Exception:
                pass
            # Only USB-reset an actual CANable hitting the full-TX-buffer error
            # (a puck that never ACKs). A Peak, a down bus, or an absent dongle
            # gains nothing from a reset and recovers on the retry below -- which
            # is what made it fire "any time a puck doesn't load". Also skip it
            # during an auto-reconnect: resetting on every quiet retry is what
            # produced the "continuously loops resets" symptom (a genuine replug
            # re-enumerates on its own).
            _should_reset = (not auto_reconnect
                             and can_backend.iface_is_candlelight(can_device)
                             and can_backend.is_tx_buffer_error(e))
            _did_reset = self._reset_can_usb(can_device) if _should_reset else False
            if _did_reset:
                self.frame_statusbar.SetStatusText('Resetting CAN adapter…', 1)
                self.frame_statusbar.Refresh()
                self.frame_statusbar.Update()
                # Yield to the event loop while waiting for the USB device to
                # re-enumerate — a plain sleep here blocks wx and causes the
                # frame to go unresponsive on GTK compositors.
                _reset_deadline = time.monotonic() + 1.5
                while time.monotonic() < _reset_deadline:
                    wx.SafeYield()
                    time.sleep(0.05)
            # else: no USB device to reset — proceed directly to the retry
            # Retry once after reset
            try:
                self._replace_network(can_device, bitrate=1000000)
                self.network.scanner.reset()
                self.network.scanner.search()
                print('CAN reset succeeded — continuing scan')
            except Exception as e2:
                # print(e)
                if "buffer" in str(e2) or "heavy" in str(e2):
                    print('No Pucks Found') # Establish error for no pucks
                    status_msg = 'No Pucks Found'
                    msg = 'No Pucks Found! \nDebug:\nPower Connection\nCAN Connection\n\nVerify Connection and Retry'
                else:
                    print('No CAN device found!')
                    status_msg = 'Scan Error: No CAN device found'
                    if _find_dfu_device():
                        status_msg = 'CAN adapter in DFU bootloader mode'
                        msg = ('No CAN device found!\n\n'
                               'A CandleLight adapter was detected in DFU bootloader mode.\n'
                               'Flash CandleLight firmware with:\n\n'
                               '  puckutilityapp.py --flash-canable')
                    else:
                        msg = 'No CAN device found! \n\nCheck connection and try again\nVerify CAN termination is present'
                # Hide the gauge so it doesn't keep painting over the error text;
                # Refresh the status bar so its previous gauge area is repainted
                # cleanly.
                self.progress.Hide()
                self.frame_statusbar.SetStatusText(status_msg, 1)
                self.frame_statusbar.Refresh()
                self.frame_statusbar.Update()
                # silent=True (startup) and auto_reconnect=True (background
                # retry) both skip the modal dialog so the app isn't walled off
                # by an interaction prompt -- repeated modals during a reconnect
                # loop are what made the port dropdown unusable. The status text
                # still surfaces the error.
                if not silent and not auto_reconnect:
                    dlg = wx.MessageDialog(None,msg)
                    dlg.ShowModal()
                    dlg.Destroy()
                # No active puck — clear node reference, ID, firmware-version
                # readout, and the Select ID dropdown so stale data isn't
                # shown. On Windows the dropdown is an OwnerDrawnComboBox
                # whose displayed text persists past SetItems([]), so
                # explicitly drop the selection.
                self.node = None
                self.choice_id.SetItems([])
                self.choice_id.SetSelection(wx.NOT_FOUND)
                self.text_id.ChangeValue('')
                self.text_version.ChangeValue('')
                return False
        # Connected OK -- clear the bounded auto-reconnect state so a future
        # genuine adapter loss gets a fresh set of retries.
        self._reconnect_attempts = 0
        self._reconnect_gave_up = False
        # We may need to wait a short while here to allow all nodes to respond
        time.sleep(0.05)
        if skipADC == True:
            pass
        elif self.adcWasON == True:
            self.on_off_adc(self)
            self.adcWasON = False
        return True

    def scan_pucks(self, event,selfCALL=False,skipADC=False):  # wxGlade: wxp3_frame.<event_handler>
        #print("Event handler 'scan_pucks'")
        #print(str(datetime.datetime.now()) + " Event handler 'scan_pucks'")

        # Track whether an error path set a status-bar message during the
        # scan; if so, the closing "Ready" reset gets skipped so the error
        # stays visible after the modal dialog dismisses.
        self._scan_error = False

        # Set Mode to IDLE in case test is active
        if skipADC == True:
            pass
        elif self.ADC_ON == True:
            self.on_off_adc(self)
            self.adcWasON = True
        else:
            self.adcWasON = False

        self.progress.Hide()

        self.frame_statusbar.SetStatusText("Scanning Pucks...", 1)
        self.frame_statusbar.Update()
        wx.Yield()
        
        try:
            if self.lastMode != 0:
                self.lastMode = 0 # Reset lastMode
                self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE
                self.button_6.SetBackgroundColour(self.gray)
                self.button_6.SetLabel("Go")
                print("Idling...")

            # This will attempt to read an SDO from nodes 1 - 127
            self.network.scanner.reset()
            self.network.scanner.search()
            time.sleep(0.5)
            # print('made it here')
            for node_id in self.network.scanner.nodes:
                print("Found node %d!" % node_id) 
            scan_length = len(self.network.scanner.nodes)
            if(scan_length > 0):
                MyApp.updateNodes(self, self.network.scanner.nodes)
                # Remember the working CAN port so next launch defaults to it.
                try:
                    self.settings.set("can_port", self.choice_port.GetStringSelection())
                except Exception:
                    pass
                # Populate the node choice list
                self.choice_id.SetItems([str(i) for i in self.network.scanner.nodes])
            if self.init:                   
                self.initialize = self.network.scanner.nodes              
                print('Initializing CAN bus...')
                init_length = len(self.initialize)
                if init_length > 0:
                    self.init = False
                    print('Success!')

            # If we found at least one, select the first
            if scan_length > 0:
                # print('here')
                if self.getID() == 0:
                    self.choice_id.SetSelection(self.getID()) # This is actually what sets the initial
                else:
                    # Select the current puck's ID in the dropdown. If it isn't
                    # in the freshly-scanned list (e.g. a transient miss right
                    # after a node-ID change), fall back to the first node found
                    # instead of raising "x not in list".
                    if self.getID() in self.network.scanner.nodes:
                        indexID = self.network.scanner.nodes.index(self.getID())
                    else:
                        indexID = 0
                        self.setID(self.network.scanner.nodes[0])
                    self.choice_id.SetSelection(indexID)
                self.select_id(None)
            else:
                # No active puck — clear ID, firmware-version, and the
                # Select ID dropdown so stale data isn't shown. SetSelection
                # is needed for the Windows OwnerDrawnComboBox path whose
                # displayed text doesn't follow SetItems([]).
                self.text_id.ChangeValue('')
                self.text_version.ChangeValue('')
                self.choice_id.SetItems([])
                self.choice_id.SetSelection(wx.NOT_FOUND)
                try:
                    result = self.can_port(None)
                    # print(result)
                    if result == True:
                        if selfCALL == False:
                            self.scan_pucks(None,True)
                        else:
                            print('Scan Error: No Pucks Found') # Establish error for no pucks
                            self._scan_error = True
                            self.progress.Hide()
                            self.frame_statusbar.SetStatusText('No Pucks Found', 1)
                            self.frame_statusbar.Refresh()
                            self.frame_statusbar.Update()
                            msg = 'No Pucks Found! \nDebug:\nPower Connection\nCAN Connection\n\nVerify Connection and Retry'
                            dlg = wx.MessageDialog(None,msg)
                            dlg.ShowModal()
                            dlg.Destroy()
                            # Set ADC Monitor Button Off after lost connection
                            self.ADC_ON == False
                            self.onoff1.SetValue(0) # Only sets button off
                            # Reset MyApp nodes
                            MyApp.updateNodes(self, self.network.scanner.nodes)

                except:
                    # print('No Pucks Found') # Establish error for no pucks
                    # msg = 'No Pucks Found! \nDebug:\nPower Connection\nCAN Connection\n\nVerify Connection and Retry'
                    # dlg = wx.MessageDialog(None,msg)
                    # dlg.ShowModal()
                    # dlg.Destroy()
                    pass
                # self.choice_id.SetSelection(0)
            #print(str(datetime.datetime.now()) + " Complete!!!")
        except Exception as e:
            if "buffer" in str(e) or "heavy" in str(e):
                # TX-buffer wedge: the bus was power-cycled while the app was open, so the CANable's
                # send queue is full and every scan send fails with ENOBUFS. USB-reset the adapter
                # ONCE and rescan automatically — no app restart needed. Gated to a real TX-buffer
                # error on a CANable, one retry only (_scan_reset_done) so a dead bus still reports.
                _cd = self.choice_port.GetStringSelection()
                if (not getattr(self, '_scan_reset_done', False)
                        and can_backend.is_tx_buffer_error(e)
                        and can_backend.iface_is_candlelight(_cd)):
                    self._scan_reset_done = True
                    print(f'Scan hit TX-buffer error ({e}) — auto-resetting CANable and rescanning...')
                    try:
                        _recovered = self._recover_can_adapter(_cd)
                    finally:
                        self._scan_reset_done = False
                    if _recovered:
                        return self.scan_pucks(event, selfCALL=selfCALL, skipADC=skipADC)
                print('No Pucks Found') # Establish error for no pucks
                # No active puck — clear ID, firmware-version, and the
                # Select ID dropdown.
                self.text_id.ChangeValue('')
                self.text_version.ChangeValue('')
                self.choice_id.SetItems([])
                self.choice_id.SetSelection(wx.NOT_FOUND)
                self._scan_error = True
                self.progress.Hide()
                self.frame_statusbar.SetStatusText('No Pucks Found', 1)
                self.frame_statusbar.Refresh()
                self.frame_statusbar.Update()
                msg = 'No Pucks Found! \nDebug:\nPower Connection\nCAN Connection\n\nVerify Connection and Retry'
                dlg = wx.MessageDialog(None,msg)
                dlg.ShowModal()
                dlg.Destroy()
            else:
                # Surface the underlying error so it isn't silently swallowed.
                # Without the selfCALL guard, a persistent SDO failure (e.g. a
                # node whose 0x100A read keeps faulting) traps startup in an
                # infinite scan -> can_port -> scan loop.
                import can as _can
                _is_device_loss = isinstance(e, (_can.CanOperationError, OSError))
                if not _is_device_loss:
                    print(f'Scan exception: {e!r}')
                if selfCALL:
                    self._scan_error = True
                    self.progress.Hide()
                    self.frame_statusbar.SetStatusText('Scan Error', 1)
                    self.frame_statusbar.Refresh()
                    self.frame_statusbar.Update()
                    msg = ('Scan Error!\n\n'
                           'Debug:\n'
                           '  • Power Connection\n'
                           '  • CAN Connection\n'
                           '  • Duplicate node IDs on the bus\n'
                           '    (two pucks with the same ID will collide\n'
                           '     and cause SDO timeouts/aborts)\n\n'
                           'Error: {}\n\n'
                           'Verify and Retry').format(repr(e))
                    dlg = wx.MessageDialog(None, msg, 'Scan Error', wx.OK | wx.ICON_ERROR)
                    dlg.ShowModal()
                    dlg.Destroy()
                    return
                try:
                    result = self.can_port(None)
                    if result == True:
                        self.scan_pucks(None, True)
                except:
                    pass

        if skipADC == True:
            pass
        elif self.adcWasON == True:
            self.on_off_adc(self)
            self.adcWasON = False

        # Don't clobber an error message set earlier in this scan — leave
        # the error visible until the next operation overwrites it.
        if not self._scan_error:
            self.frame_statusbar.SetStatusText("Ready", 1)
            self.frame_statusbar.Update()
        wx.Yield()

    def select_id(self, event):  # wxGlade: wxp3_frame.<event_handler>
        if self.check_for_node() == False:
            return False
        #print("Event handler 'select_id'")
        if self.firstRun:
            active = MyApp.getPucks(self)
            compare = []
            for element in MyApp.getNodes(self):
                if element not in active:
                    compare.append(element)
            if self.getID() == 0:
                self.setID(compare)
                node_idx = compare.index(min(compare)) # idx of lowest inactive puck
            else:
                self.initialize = MyApp.getNodes(self)
                node_idx = self.initialize.index(self.getID())
            node_id = self.initialize[node_idx]
            self.setID(node_id)
            self.firstRun = False
            # Mirror the non-firstRun branch's bookkeeping: register the
            # selected puck in ActiveID. Without this, a retry-scan that
            # succeeds after an initial empty scan leaves the puck active
            # in the UI but missing from ActiveID, which then blows up
            # onCloseFrame -> removePuck.
            MyApp.addPucks(self, self.getID())
        else: # Not first run
            node_id = int(self.choice_id.GetString(self.choice_id.GetSelection()))
            # Popup error if Node is already active and not the selected frames current node
            if node_id in MyApp.getPucks(self) and self.settingID != True and node_id != self.getID():
                indexID = self.network.scanner.nodes.index(self.getID())
                self.choice_id.SetSelection(indexID)
                print('Node already active')
                msg = ('Node already active!')
                dlg = wx.MessageDialog(None,msg)
                dlg.ShowModal()
                dlg.Destroy()
                return
            else:
                if self.getID() in MyApp.getPucks(self): 
                    MyApp.removePuck(self,self.getID())    
                self.setID(node_id)
                MyApp.addPucks(self,self.getID())

        print("Selected node = {0}".format(node_id))
        if node_id not in self.network:
          # Add our canopen node along with its object dictionary (for parsing)
          print("Adding new node: {0}".format(node_id))
          self.node = self.network.add_node(node_id, 'puck4.eds')
        else:
          self.node = self.network[node_id]

        # Reset the puck to a CLEAN PDO state before the first SDO reads. A prior run that
        # crashed can leave the puck streaming TPDOs (capture-PDO config never restored); that
        # traffic floods the bus and can time out the reads below. NMT PRE-OPERATIONAL halts all
        # TPDO transmission while keeping the SDO server alive; configure_Puck + OPERATIONAL
        # (below) re-establish the map and streaming. Mirrors pucktuner's boot-flood fix; here
        # SYNC is ADC-gated, so this mainly guards against a prior crash's leftover streaming.
        try:
            self.node.nmt.state = 'PRE-OPERATIONAL'
            time.sleep(0.05)
            try:
                self.network.bus.flush_tx_buffer()
            except Exception:
                pass
        except Exception as _preop_e:
            print('  (pre-op reset before import skipped: {})'.format(_preop_e))

        self.text_id.ChangeValue(str(node_id))

        version = get_version(self.node.sdo['MfgSoftwareVersion'].raw)
        self.text_version.ChangeValue(version)

        # Detect flashloader by attempting to read SetModeOfOperation, which only
        # exists in application firmware. The flashloader aborts this SDO — use
        # that as the discriminator rather than node ID or version number.
        try:
            current_mode = self.node.sdo["SetModeOfOperation"].raw
        except SdoAbortedError:
            print("Node {} is in flashloader mode (v{}) — skipping puck configuration.".format(
                node_id, version))
            self.frame_statusbar.SetStatusText(
                "Flashloader v{} on node {}".format(version, node_id), 1)
            return
        if(current_mode == 0):     
            # update select test to idle and input to 0
            self.choice_test.SetSelection(0)
            self.text_testvalue.SetValue('0')
        elif(current_mode == 4):
            # update select test to trq mode and update input to current target torque
            self.choice_test.SetSelection(1)
            input = self.node.sdo["TargetTorque"].raw # This is out of 1000% maximum, needs conversion
            rated_torque = self.node.sdo["RatedTorque"].raw
            cmd_value = input * rated_torque * self.gearRatio / 1000 # cmd_value * 1000 / (rated_torque * self.gearRatio) # Scale
            self.text_testvalue.SetValue(str(round(cmd_value)))
        elif(current_mode == 3):
            # update select test to velocity mode and update input to current target torque
            print('updating mode...')
            self.choice_test.SetSelection(2)
            input = self.node.sdo["TargetVelocity"].raw
            cmd_value = (input * 60) / (4096 * self.gearRatio) # ctspersec = cmd_value * 4096 / 60 * self.gearRatio
            self.text_testvalue.SetValue(str(round(cmd_value)))
        elif(current_mode == 1):
            # update select test to trq mode and update input to current target torque
            print('updating mode...')
            self.choice_test.SetSelection(3)
            # No good way to get last position update in degrees, and no real reason to have this
            self.text_testvalue.SetValue('0')
        elif(current_mode == 6):
            # update select test to trq mode and update input to current target torque
            print('updating mode...')
            self.choice_test.SetSelection(4)
            self.text_testvalue.SetValue('0')
        
        # may want to make this more centralized (like for loop to configure all at once)
        # print('Configure...')
        self.configure_Puck() # This makes sure all pucks are configured to remove bug with first round adc on turning puck idle
        # Back to OPERATIONAL so ADC-on TPDO streaming works (we dropped to PRE-OPERATIONAL
        # above to read on a quiet bus).
        try:
            self.node.nmt.state = 'OPERATIONAL'
        except Exception:
            pass

    def set_id(self, event):  # wxGlade: wxp3_frame.<event_handler>
        if self.check_for_node() == False:
            return
        
        if self.ADC_ON == True:
            self.on_off_adc(self)
            self.adcWasON = True
        else:
            self.adcWasON = False

        if self.lastMode != 0:
            self.lastMode = 0 # Reset lastMode
            self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE
            self.button_6.SetBackgroundColour(self.gray)
            self.button_6.SetLabel("Go")
            print("Idling...")

        #print("Event handler 'set_id'")
        if int(self.text_id.GetValue()) in MyApp.getNodes(self): # self.network.scanner.nodes: # Try this with active nodes??
            # Error message - resets ID to active if error
            indexID = self.network.scanner.nodes.index(self.getID())
            msg = ('ID already in use!')
            dlg = wx.MessageDialog(None,msg)
            dlg.ShowModal()
            dlg.Destroy()
            return
        if int(self.text_id.GetValue()) < 1: # or int(self.text_id.GetValue()) > 127: # Try to stop 127 loop
            # Error message - resets ID to active if error
            indexID = self.network.scanner.nodes.index(self.getID())
            self.text_id.ChangeValue(str(self.getID()))
            msg = ('Invalid CAN ID!')
            dlg = wx.MessageDialog(None,msg)
            dlg.ShowModal()
            dlg.Destroy()
            return
        self.settingID = True
        old_id = self.getID()
        node_id = int(self.text_id.GetValue())

        try:
            MyApp.removePuck(self, old_id)
        except Exception:
            pass

        # Change the puck's CAN ID. A node-ID change only takes effect after the
        # puck reboots, so write NetCfg then NMT-reset it. (Verified on-bench:
        # NetCfg self-persists and the puck comes up on the new ID after the
        # reset -- an explicit Save is neither needed nor supported here.)
        print('Setting new node ID {} -> {}...'.format(old_id, node_id))
        try:
            self.node.sdo['NetCfg'].raw = node_id
        except Exception as e:
            print('Failed to write new node ID: {}'.format(e))
        try:
            self.network.send_message(0x0, [0x81, old_id])  # NMT reset node
        except Exception as e:
            print('Failed to reboot puck: {}'.format(e))

        # The app now expects the puck on the new ID.
        self.setID(node_id)
        MyApp.addPucks(self, self.getID())

        # Wait for the puck to actually come up on the new ID before re-scanning,
        # rather than guessing a fixed delay (too short -> the old "not in list"
        # crash) or hammering the bus with repeated full scans. _wait_for_node
        # polls only the new node -- one small SDO read per try.
        if not self._wait_for_node(node_id):
            print('Warning: node {} did not respond after the ID change.'.format(node_id))

        # One re-scan to refresh the dropdown / node list.
        self.scan_pucks(None)

        self.settingID = False

        if self.adcWasON == True:
            self.on_off_adc(self)

        # We could also add something to set_id to automatically reload cob ids, but for now this is how we ensure IDs get updated
        self.choice_test.SetSelection(0)
        # Should tell user calibration is required, and ask to perform 'calibrate all'
        msg = "Configuration is required after changing ID.\nWould you like to configure the active Puck?"
        dlg = wx.MessageDialog(None,msg,'Warning!',wx.YES_NO | wx.ICON_WARNING)
        answer = dlg.ShowModal()
        if answer == wx.ID_YES:
            self.file_to_p4(None)
        else:
            pass
        dlg.Destroy()

    def browse_fw(self, event, path=False):  # wxGlade: wxp3_frame.<event_handler>
        #print("Event handler 'browse_fw'")
        if self._busy_reject("firmware flash"):
            return
        if self.check_for_node() == False:
            return
        if self.choice_id.GetSelection() == wx.NOT_FOUND:
            return

        quick_test = self.choice_test.GetSelection()
        if quick_test != 0:
            self.lastMode = 0 # Reset lastMode
            print("Setting Mode = IDLE")
            self.choice_test.SetSelection(0)
            self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE
            # Set Go Color to Gray
            self.button_6.SetBackgroundColour(self.gray)

        if self.ADC_ON == True:
            self.on_off_adc(self)
            self.adcWasON = True
        else:
            self.adcWasON = False

        can_device = self.choice_port.GetStringSelection()
        node_id = self.choice_id.GetString(self.choice_id.GetSelection())

        if path == False:
            # File browser. Resolve relative to this file so the dialog opens
            # in <puckutility>/firmware regardless of the cwd the app was
            # launched from.
            directory = resource_path('firmware')

            with wx.FileDialog(self, "Select firmware file", directory, wildcard="BIN files (*.bin;*.ebin)|*.bin;*.ebin",
                          style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as fileDialog:
                if fileDialog.ShowModal() == wx.ID_CANCEL:
                    if self.adcWasON == True:
                        self.on_off_adc(self)
                    return     # the user changed their mind
                # Proceed loading the file chosen by the user
                pathname = fileDialog.GetPath()
        else:
            pathname = path

        self.frame_statusbar.SetStatusText("Updating firmware... (~30 seconds)", 1)
        self.frame_statusbar.Update()
        wx.Yield()

        # timeStart = time.time()

        can_device = self.choice_port.GetStringSelection()
        node_id = self.choice_id.GetString(self.choice_id.GetSelection())

        # print("Writing OD entries")
        self.network.disconnect()

        # Using multithreading!
        self.OnStartTask(None) # need this to show!! 

        process = multiprocessing.Process(target=flashp4.start,args=(can_device, int(node_id),pathname,self.update_queue,))
        process.start()
        self.progress.Show()

        self.update = []
        self.update.clear()

        # Drain everything that arrived since the last tick, but only repaint
        # with the most recent numeric value — bursty updates would otherwise
        # paint every intermediate step and tear. wx.YieldIfNeeded() pumps
        # the event loop so paints/focus changes still flow even though
        # we're inside this synchronous polling block (the previous version
        # froze the gauge whenever the window lost focus).
        # HOLD OFF other tasks/drops for the whole flash + recovery. Without this, a config file
        # dropped mid-flash re-enters (the drain loop calls wx.Yield) and collides on the CAN bus,
        # which is exactly the FLASH_FAILED + SDO-abort cascade seen when a flash and config overlap.
        # try/finally guarantees the flag clears even if the reconnect/rescan throws.
        self._upload_busy = True
        try:
            result = None
            while result is None:
                latest = None
                try:
                    while True:
                        item = self.update_queue.get_nowait()
                        self.update.append(item)
                        if item == "Pass" or item == "Fail" or item == "Done":
                            result = item
                            break
                        latest = item
                except multiprocessing.queues.Empty:
                    pass
                if latest is not None:
                    self.UpdateUI(latest)
                if result is None:
                    wx.YieldIfNeeded()
                    time.sleep(0.05)

            process.terminate()
            process.join()
            self.OnTaskComplete()

            print(result)

            self.requireCal = True
            self.requireConfig = True

            # Reconnect, then send NMT Reset Node to the flashed puck so it exits
            # the flashloader and boots application firmware. flashp4 only sends
            # LAUNCH and disconnects — without this reset the puck stays in the
            # flashloader.
            self.can_port(None, True)
            try:
                print("Sending NMT reset to node {} ...".format(node_id))
                self.network.send_message(0x0, [0x81, int(node_id)])
            except Exception as _nmt_e:
                print("WARNING: NMT reset failed: {}".format(_nmt_e))
            time.sleep(0.5)

            # Re-scan
            self.scan_pucks(None, False, True)
            self.frame_statusbar.SetStatusText("Ready", 1)

            if self.adcWasON == True:
                self.on_off_adc(self)
        finally:
            self._upload_busy = False

    # Mapping of CANopen 0x1018 sub-2 product codes to Puck model names.
    # Shared resolver handles both legacy numeric codes and the newer
    # ASCII-packed model tags (e.g. 1345598258 -> 'P4-32'). calibrate_menu
    # reads this same attribute via getattr(self, '_PRODUCT_CODE_MODELS').
    _PRODUCT_CODE_MODELS = can_backend.PRODUCT_CODE_MODELS

    def _format_product_code(self, code):
        """Format a product code as e.g. '5707 (P4-16)', or '<unknown>' if code is None."""
        if code is None:
            return '<unknown>'
        model = self._PRODUCT_CODE_MODELS.get(code)
        return f"{code} ({model})" if model else f"{code} (unknown model)"

    def _read_csv_product_code(self, pathname):
        """Return the 0x1018 sub-2 (Product Code) value from a CANopen CSV
        config, or None if not present / parse error. CSV row format is:
        description, variable, index, subindex, type, value."""
        try:
            with open(pathname, 'r') as f:
                for row in csv.reader(f):
                    if len(row) < 6:
                        continue
                    idx = row[2].strip().lower()
                    sub = row[3].strip()
                    if idx == '0x1018' and sub == '2':
                        return int(row[5].strip(), 0)  # base 0 handles 0x.. and decimal
        except Exception as e:
            print(f"Failed to parse product code from CSV: {e}")
        return None

    def file_to_p4(self, event, path=False):  # wxGlade: wxp3_frame.<event_handler>
        if self._busy_reject("config upload"):
            return
        if self.check_for_node() == False:
            return
        #print("Event handler 'file_to_p4'")
        # If motor is not idled, idle
        quick_test = self.choice_test.GetSelection()
        if quick_test != 0:
            self.lastMode = 0 # Reset lastMode
            print("Setting Mode = IDLE")
            self.choice_test.SetSelection(0)
            self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE
            # Set Go Color to Gray
            self.button_6.SetBackgroundColour(self.gray)

        if self.ADC_ON == True:
            self.on_off_adc(self)
            self.adcWasON = True

        if path == False:
            # File browser. Resolve relative to this file so the dialog opens
            # in <puckutility>/config regardless of the cwd the app was
            # launched from.
            directory = resource_path('config')

            with wx.FileDialog(self, "Open CANopen CSV file", directory, wildcard="CSV files (*.csv)|*.csv",
                          style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as fileDialog:

              if fileDialog.ShowModal() == wx.ID_CANCEL:
                  if self.adcWasON == True:
                    self.on_off_adc(self)
                  return     # the user changed their mind
              # Proceed loading the file chosen by the user
              pathname = fileDialog.GetPath()
        else:
            pathname = path

        # Verify the CSV's product code (0x1018 sub-2) matches the active
        # Puck before disconnecting/uploading. Flashing a config from a
        # mismatched motor variant can leave the drive in an unusable state.
        csv_product_code = self._read_csv_product_code(pathname)
        try:
            node_product_code = int(self.node.sdo[0x1018][2].raw)
        except Exception as e:
            node_product_code = None
            print(f"Failed to read active node product code: {e}")
        # Compare by resolved MODEL, not the raw code. A legacy CSV stores the
        # small integer code (e.g. 5760 = P4-32) while newer firmware reports the
        # ASCII-packed code (0x50343332 = "P432" = P4-32) for the SAME model, so a
        # raw-value compare would falsely flag a mismatch and block a valid upload.
        # _PRODUCT_CODE_MODELS.get() resolves both encodings to a model name.
        csv_model = (self._PRODUCT_CODE_MODELS.get(csv_product_code)
                     if csv_product_code is not None else None)
        node_model = (self._PRODUCT_CODE_MODELS.get(node_product_code)
                      if node_product_code is not None else None)
        if csv_model is not None and node_model is not None and csv_model != node_model:
            csv_str = self._format_product_code(csv_product_code)
            node_str = self._format_product_code(node_product_code)
            print(f"Product Code Mismatch: CSV={csv_str}, node={node_str}")
            msg = ("Product Code Mismatch — configuration NOT uploaded.\n\n"
                   f"CSV File product code: {csv_str}\n"
                   f"Active Puck product code: {node_str}\n\n"
                   "Select a configuration file that matches this Puck "
                   "variant and try again.")
            dlg = wx.MessageDialog(None, msg, "Product Code Mismatch",
                                   wx.OK | wx.ICON_ERROR)
            dlg.ShowModal()
            dlg.Destroy()
            if self.adcWasON == True:
                self.on_off_adc(self)
            return
        elif node_product_code is not None and node_model is None:
            # Puck's product code doesn't map to any known model — we can't
            # reliably identify the variant, so allow the upload (blocking would
            # just trap the user) and log the bypass.
            print(f"Product code {self._format_product_code(node_product_code)} "
                  f"not recognized; allowing upload anyway.")

        self.frame_statusbar.SetStatusText("Updating Config...", 1)
        self.frame_statusbar.Update()
        wx.Yield()

        can_device = self.choice_port.GetStringSelection()
        node_id = self.choice_id.GetString(self.choice_id.GetSelection())

        print("Writing OD entries...")
        self.network.disconnect()

        # Using multithreading!
        self.OnStartTask(None) # need this to show!! 
        process = multiprocessing.Process(target=canopen_runner.start,args=(can_device, int(node_id),'puck4.eds',pathname,self.update_queue,))
        process.start()
        self.progress.Show()

        self.update = []

        # Drain-and-pump pattern: see the matching loop in browse_fw for the
        # rationale (focus-loss freeze + flicker avoidance).
        # HOLD OFF other tasks/drops for the ENTIRE upload+configure. The drain loop below calls
        # wx.Yield, so without this a second dropped file re-enters mid-upload and collides on the CAN
        # bus. The try/finally guarantees the flag clears even if an SDO abort escapes configure_Puck.
        self._upload_busy = True
        try:
            result = None
            while result is None:
                latest = None
                try:
                    while True:
                        item = self.update_queue.get_nowait()
                        self.update.append(item)
                        if item == "Pass" or item == "Fail":
                            result = item
                            break
                        latest = item
                except multiprocessing.queues.Empty:
                    pass
                if latest is not None:
                    self.UpdateUI(latest)
                if result is None:
                    wx.YieldIfNeeded()
                    time.sleep(0.05)

            process.terminate()
            process.join()
            self.OnTaskComplete()

            print(result)

            if result != "Pass":
                # FAILED upload -> the puck may be half-written. Do NOT save/reboot/configure or run
                # a calibration against it (that is exactly what threw the SDO aborts / cal fault).
                # Reconnect so the app isn't left with a dead network handle, flag config-still-needed,
                # and bail -- the user must fix the file/comms and retry.
                print("Configuration file failed to upload...")
                msg = "Configuration file failed to upload..." \
                "\n\nDebug:" \
                "\n-Verify proper configuration file formatting" \
                "\n-Verify correct version of config file" \
                "\n-View terminal log for additional details"
                dlg = wx.MessageDialog(None,msg)
                dlg.ShowModal()
                dlg.Destroy()
                try:
                    self._replace_network(can_device, bitrate=1000000)
                    self.node = self.network.add_node(int(node_id), 'puck4.eds')
                except Exception as _e:
                    print("  (could not re-establish network after failed upload: {})".format(_e))
                self.requireConfig = True   # config NOT applied; block driving/cal until a good upload
                self.frame_statusbar.SetStatusText("Config upload FAILED — not calibrating", 1)
                if self.adcWasON == True:
                    self.on_off_adc(self)
                    self.adcWasON = False
                return

            print("Success!")

            self.requireCal = True
            self.requireConfig = False

            print("Establishing a new network...")
            self._replace_network(can_device, bitrate=1000000)
            self.node = self.network.add_node(int(node_id), 'puck4.eds')

            # Save all OD entries to EEPROM (takes about 0.55 sec)
            print("Saving OD entries")
            default_timeout = canopen.sdo.SdoClient.RESPONSE_TIMEOUT
            canopen.sdo.SdoClient.RESPONSE_TIMEOUT = 1.0
            self.node.sdo['Save']['All'].raw = 0x65766173 # Key = 'SAVE'
            canopen.sdo.SdoClient.RESPONSE_TIMEOUT = default_timeout

            # Transmit an NMT reboot command to this node
            print("Rebooting puck")
            self.network.send_message(0x0, [0x81, int(node_id)])
            time.sleep(0.5) # wait for puck to reboot (avoids loss of communication)
            self.configure_Puck()
            self.frame_statusbar.SetStatusText("Ready", 1)

            if self.adcWasON == True:
                self.on_off_adc(self)
                self.adcWasON = False
        finally:
            self._upload_busy = False

    def _cal_params_sane(self):
        """Sanity-check the active puck's iSense calibration before enabling a drive mode.
        A bad bias / gainfactor / current-slope produces phantom current -> oscillation at 0 torque
        (and worse).  Returns (ok, reason).  On any read failure (transient SDO timeout, older
        firmware) returns (True, None): only ever block on a CONFIRMED-bad value, never a false
        positive that would strand a good puck."""
        try:
            a_gf = self.node.sdo['Alpha']['Gainfactor'].raw   # Q4.12, ~4096 = 1.0
            b_gf = self.node.sdo['Beta']['Gainfactor'].raw
            a_bi = self.node.sdo['Alpha']['Bias'].raw         # Q12.4, ~32768 = mid-scale
            b_bi = self.node.sdo['Beta']['Bias'].raw
        except Exception:
            return True, None
        if not (2048 <= a_gf <= 8192 and 2048 <= b_gf <= 8192):
            return False, "gainfactor out of range (Alpha={}, Beta={}; ~4096 expected)".format(a_gf, b_gf)
        # Bias is Q12.4 (~32768 mid) on current firmware but Q12.0 (~2048 mid) on older firmware.
        # Accept EITHER mid-scale so an older-firmware puck is never falsely blocked from driving.
        _bias_ok = lambda v: (25600 <= v <= 38400) or (1536 <= v <= 2560)   # Q12.4 | Q12.0
        if not (_bias_ok(a_bi) and _bias_ok(b_bi)):
            return False, ("iSense bias out of range (Alpha={}, Beta={}; expect ~32768 Q12.4 or "
                           "~2048 Q12.0)".format(a_bi, b_bi))
        try:  # current-slope (0x3008:7 / 0x3009:7) -- absent on older firmware, so skip on error
            a_sl = self.node.sdo[0x3008][7].raw
            b_sl = self.node.sdo[0x3009][7].raw
            if a_sl > 32767: a_sl -= 65536
            if b_sl > 32767: b_sl -= 65536
            if abs(a_sl) >= 2048 or abs(b_sl) >= 2048:
                return False, "current-slope absurd (Alpha={}, Beta={} Q4.12; |k|<0.5 expected)".format(a_sl, b_sl)
        except Exception:
            pass
        return True, None

    def select_test(self, event):  # wxGlade: wxp3_frame.<event_handler>
        if getattr(self, '_emcy_selection', False):
            return
        if self._busy_reject("drive/test"):
            self.choice_test.SetSelection(0)
            return

        if self.ADC_ON == True:
            try:
                self.node.network.sync.stop()
            except Exception:
                pass  # BCM rejects stop if sync was already stopped; harmless
            time.sleep(0.05)  # Let any in-flight sync frame clear before SDO transactions

        if len(self.network.scanner.nodes) == 0:
            self.choice_test.SetSelection(0)
            print('No active node!')
            dlg = wx.MessageDialog(None, 'No active node!')
            dlg.ShowModal()
            dlg.Destroy()
            return
        elif self.requireConfig:
            self.choice_test.SetSelection(0)
            msg = "Configuration is required after a firmware update.\nWould you like to configure the active Puck?"
            dlg = wx.MessageDialog(None, msg, 'Warning!', wx.YES_NO | wx.ICON_WARNING)
            answer = dlg.ShowModal()
            dlg.Destroy()
            if answer == wx.ID_YES:
                self.file_to_p4(None)
            return
        elif self.requireCal:
            self.choice_test.SetSelection(0)
            msg = "Calibration is required after configuration.\nWould you like to calibrate all Pucks?"
            dlg = wx.MessageDialog(None, msg, 'Warning!', wx.YES_NO | wx.ICON_WARNING)
            answer = dlg.ShowModal()
            dlg.Destroy()
            if answer == wx.ID_YES:
                self.calibrate_all_pucks(None)
            return

        quick_test = self.choice_test.GetSelection()

        if quick_test == 0:
            print("Setting Mode = IDLE")
            self.node.sdo["ControlWord"].raw = SHUTDOWN
            self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE
            self.node.rpdo[1]["SetModeOfOperation"].raw = MODE_IDLE
            self.node.rpdo[1]["ControlWord"].raw = SHUTDOWN
            self.button_6.SetBackgroundColour(self.gray)
            self.lastMode = 0
            if self.ADC_ON == True:
                self.node.rpdo[1].transmit()
                self.node.network.sync.start()
            return

        # Cal-sanity gate: never enable a drive mode with iSense cal that would cause phantom
        # current / oscillation at 0 torque. (IDLE, quick_test==0, already returned above.)
        _ok, _reason = self._cal_params_sane()
        if not _ok:
            self.choice_test.SetSelection(0)
            print("DRIVE BLOCKED -- calibration looks bad: {}".format(_reason))
            print("  Run a full calibration before driving (this state causes phantom current / "
                  "oscillation at 0 torque).")
            dlg = wx.MessageDialog(None, "Drive blocked -- calibration looks bad:\n{}\n\n"
                                   "Run a full calibration before driving.".format(_reason),
                                   'Bad calibration', wx.OK | wx.ICON_WARNING)
            dlg.ShowModal(); dlg.Destroy()
            return

        # Clear faults, RTSO, OpEnabled
        print("Going OpEnabled")
        try:
            self.node.sdo["ControlWord"].raw = CLEAR_FAULT
            self.node.sdo["ControlWord"].raw = SHUTDOWN
            self.node.sdo["ControlWord"].raw = OP_ENABLED
        except SdoAbortedError as _e:
            from can_backend import sdo_contention_message
            _contention = sdo_contention_message(_e)
            if _contention:
                # Protocol-level abort from competing bus traffic, not a drive
                # fault — reading StatusWord would just collide again, so report
                # the contention directly.
                print("Enable failed — CAN bus contention.")
                print("  " + _contention.replace("\n\n", "\n  ").replace("\n", "\n  "))
                self.button_6.SetBackgroundColour(self.gray)
                self.choice_test.SetSelection(0)
                return
            # Drive refused the ControlWord sequence — most commonly because an
            # active fault (e.g. undervoltage) prevents the state transition.
            # Read StatusWord so we can report which fault is blocking enable.
            try:
                _sw = self.node.sdo["StatusWord"].raw
                _fault_active = bool(_sw & (1 << 3))
                print(f"Enable failed — drive rejected ControlWord (SDO abort 0x{_e.code:08X}).")
                if _fault_active:
                    print(f"  Drive is in Fault state (StatusWord: {hex(_sw)}). "
                          "Resolve the fault and retry.")
                else:
                    print(f"  StatusWord: {hex(_sw)}")
            except Exception:
                print(f"Enable failed — SDO abort 0x{_e.code:08X}. "
                      "Check drive fault status.")
            self.button_6.SetBackgroundColour(self.gray)
            self.choice_test.SetSelection(0)
            if self.ADC_ON:
                try:
                    self.node.network.sync.start()
                except Exception:
                    pass
            return

        self.button_6.SetBackgroundColour(self.orange)

        status = self.node.sdo["StatusWord"].raw
        # DS402 "Operation Enabled" with Voltage Enabled asserted:
        # bits 0,1,2,4,5 = 1, bits 3,6 = 0. Mask 0x7F isolates the state
        # machine bits plus Voltage Enabled, ignoring Warning, Target
        # Reached, mode-specific bits, etc.
        if (status & 0x7F) == 0x37:
            print("Drive is ENABLED and ready.")
        else:
            print(f"Drive NOT enabled. StatusWord: {hex(status)}")
            # DS402 bit 3 = FAULT. A drive mode CANNOT be set while faulted -- the firmware
            # rejects SetModeOfOperation with SDO abort 0x05040001, which used to crash this
            # handler with a traceback. Abort cleanly with an actionable message instead.
            if status & 0x08:
                print("  Drive is FAULTED -- clear the fault before starting a test.")
                print("  (If this was a 0x2311 magnet-demag / thermal trip, let the motor "
                      "COOL first, then clear. That fault is protection working, not damage.)")
                self.button_6.SetBackgroundColour(self.orange)
                return
            # Not faulted, just not yet enabled -- continue (used as a debug tool).

        if quick_test == 1:  # Torque
            print("Setting Mode = TORQUE")
            self.node.sdo["TargetTorque"].raw = 0
            self.node.rpdo[1]["TargetTorque"].raw = 0
            self.node.sdo["SetModeOfOperation"].raw = MODE_PROFILE_TRQ
            self.node.rpdo[1]["SetModeOfOperation"].raw = MODE_PROFILE_TRQ
            self.node.rpdo[1]["ControlWord"].raw = OP_ENABLED
            self.text_testvalue.SetValue("0")

        elif quick_test == 2:  # Velocity
            print("Setting Mode = VELOCITY")
            self.node.sdo["TargetVelocity"].raw = 0
            self.node.rpdo[2]["TargetVelocity"].raw = 0
            self.node.rpdo[2].transmit()
            self.node.sdo["SetModeOfOperation"].raw = MODE_PROFILE_VEL
            self.node.rpdo[1]["SetModeOfOperation"].raw = MODE_PROFILE_VEL
            self.node.rpdo[1]["ControlWord"].raw = OP_ENABLED
            self.text_testvalue.SetValue("0")

        elif quick_test == 3:  # Position
            print("Setting Mode = POSITION")
            _cur_pos = self.node.sdo["PositionFeedback"].raw
            self.node.sdo["TargetPosition"].raw = _cur_pos
            self.node.rpdo[2]["TargetPosition"].raw = _cur_pos
            self.node.rpdo[2].transmit()
            self.node.sdo["ProfileVelocity"].raw = 130000
            self.node.sdo["SetModeOfOperation"].raw = MODE_PROFILE_POS
            self.node.sdo["ControlWord"].raw = 0x2F  # OP_ENABLED | new setpoint
            self.node.rpdo[1]["SetModeOfOperation"].raw = MODE_PROFILE_POS
            self.node.rpdo[1]["ControlWord"].raw = 0x2F
            self.text_testvalue.SetValue("0")

        elif quick_test == 4:  # Homing
            # Homing ran as a silent no-op because the CiA-402 Homing Method (0x6098) is never set:
            # its EDS default is 0 = "no homing method", so the bit-4 start armed a state machine with
            # no procedure to execute. Program a method here: respect one already stored in
            # EEPROM/config, else default to 35 = "home on current position" (latches the present
            # position as home with NO commanded motion — the only universally safe default when the
            # mechanism's real homing procedure is unknown). For a home that physically seeks a
            # reference, store the mechanism-appropriate method in 0x6098 (EEPROM) and it's respected.
            try:
                _hm = self.node.sdo[0x6098].raw
            except Exception:
                _hm = 0
            if _hm:
                print("HomingMethod (0x6098) = {} (from config/EEPROM)".format(_hm))
            else:
                _hm = 35
                self.node.sdo[0x6098].raw = _hm
                print("HomingMethod (0x6098) was unset; defaulting to {} (home on current position).".format(_hm))
            print("Setting Mode = HOMING")
            self.node.sdo["SetModeOfOperation"].raw = MODE_HOMING
            self.node.rpdo[1]["SetModeOfOperation"].raw = MODE_HOMING
            self.node.rpdo[1]["ControlWord"].raw = OP_ENABLED   # bit4 low -> clean rising edge in run_test
            self.text_testvalue.SetValue("0")

        if self.ADC_ON == True:
            # Push fresh RPDO state so the first SYNC tick latches the new mode/CW,
            # not whatever stale data was last in the drive's RPDO inbox.
            self.node.rpdo[1].transmit()
            self.node.network.sync.start()

    def run_test(self, event):  # wxGlade: wxp3_frame.<event_handler>
        if self._busy_reject("drive command"):
            return
        if len(self.network.scanner.nodes) == 0:
            print('No active node!')
            dlg = wx.MessageDialog(None, 'No active node!')
            dlg.ShowModal()
            dlg.Destroy()
            return

        if len(self.text_testvalue.GetValue()) == 0:
            print('No input value!')
            dlg = wx.MessageDialog(None, 'No input value!')
            dlg.ShowModal()
            dlg.Destroy()
            return

        quick_test = self.choice_test.GetSelection()
        cmd_value = float(self.text_testvalue.GetValue())

        if quick_test == 0:
            print('No mode selected!')
            dlg = wx.MessageDialog(None, 'No mode selected!')
            dlg.ShowModal()
            dlg.Destroy()
            return

        if self.ADC_ON == True:
            try:
                self.node.network.sync.stop()
            except Exception:
                pass  # BCM rejects stop if already stopped / bus hiccup -- harmless
            time.sleep(0.05)  # Let any in-flight sync frame clear before SDO transactions

        if quick_test == 1:  # Torque
            try:
                rated_torque = self.node.sdo["RatedTorque"].raw
                if abs(cmd_value / self.gearRatio) > rated_torque:
                    cmd_value = math.copysign(rated_torque * self.gearRatio, cmd_value)
                trq_value = round(cmd_value * 1000 / (rated_torque * self.gearRatio))
                print(f"Set TargetTorque = {cmd_value} mNm ({round(trq_value / 10, 2)}% max)")
                self.node.sdo["TargetTorque"].raw = trq_value
                self.node.rpdo[1]["TargetTorque"].raw = trq_value
                self.node.rpdo[1]["ControlWord"].raw = OP_ENABLED
            except Exception as _e:
                print("Torque command aborted -- puck not responding (comms): {}".format(_e))
                self.choice_test.SetSelection(0)
                try:
                    if self.ADC_ON == True:
                        self.node.network.sync.start()
                except Exception:
                    pass
                return

        elif quick_test == 2:  # Velocity
            ctspersec = round(cmd_value * 4096 / 60 * self.gearRatio)
            print(f"Set TargetVelocity = {cmd_value} RPM")
            self.node.rpdo[2]["TargetVelocity"].raw = ctspersec
            self.node.rpdo[2].transmit()
            self.node.rpdo[1]["ControlWord"].raw = OP_ENABLED
            self.node.rpdo[1].transmit()
            self.node.network.sync.transmit()

        elif quick_test == 3:  # Position
            ctsvalue = cmd_value / 360 * 4096 * self.gearRatio
            print(f"Set Target Position += {cmd_value} degrees")
            # Wait for ready to receive new waypoint (StatusWord bit 12 = 0), 1 s timeout
            for _ in range(100):
                if not (self.node.sdo["StatusWord"].raw & 0x1000):
                    break
                time.sleep(0.01)
            target_pos = int(self.node.sdo["PositionFeedback"].raw + ctsvalue)
            self.node.sdo["TargetPosition"].raw = target_pos
            self.node.rpdo[2]["TargetPosition"].raw = target_pos
            # Push the new target into the drive's RPDO[2] inbox before SYNC latches it
            self.node.rpdo[2].transmit()
            # Raise new setpoint flag via RPDO + single SYNC (avoids SDO lock on ControlWord)
            self.node.rpdo[1]["ControlWord"].raw = 0x3F
            self.node.rpdo[1].transmit()
            self.node.network.sync.transmit()
            # Wait for setpoint acknowledged (StatusWord bit 12 = 1), 1 s timeout
            for _ in range(100):
                if self.node.sdo["StatusWord"].raw & 0x1000:
                    break
                time.sleep(0.01)
            # Clear new setpoint flag via RPDO + single SYNC
            self.node.rpdo[1]["ControlWord"].raw = 0x2F
            self.node.rpdo[1].transmit()
            self.node.network.sync.transmit()
            # Wait for falling edge acknowledgment, 1 s timeout
            for _ in range(100):
                if not (self.node.sdo["StatusWord"].raw & 0x1000):
                    break
                time.sleep(0.01)

        elif quick_test == 4:  # Homing
            self.node.sdo["HomingOffset"].raw = int(cmd_value)   # coordinate offset only, NOT a trigger
            print("Starting homing (Controlword bit4 rising edge)...")
            self.node.rpdo[1]["ControlWord"].raw = OP_ENABLED | 0x10  # 0->1 on bit4 = homing start
            self.node.rpdo[1].transmit()
            self.node.network.sync.transmit()
            # Poll: bit 13 (0x2000) = homing error, bit 12 (0x1000) = homing attained (success), else
            # timeout. The old poll waited on bit 12 only, so a failed home was indistinguishable
            # from a 30 s hang.
            _sw = 0
            for _ in range(300):  # 30 s timeout
                _sw = self.node.sdo["StatusWord"].raw
                if _sw & 0x2000:
                    print("HOMING ERROR - StatusWord {:#06x}".format(_sw))
                    break
                if _sw & 0x1000:
                    print("Homing attained (success) - StatusWord {:#06x}".format(_sw))
                    # Recenter the position dial on the freshly-homed spot (see getPosition).
                    try:
                        self._dial_zero = self.node.sdo['PositionFeedback'].raw
                        print("  dial recentered to homed position (PositionFeedback={}).".format(
                            self._dial_zero))
                    except Exception:
                        pass
                    break
                time.sleep(0.1)
            else:
                print("Homing timed out: no attained/error bit within 30 s - StatusWord {:#06x}".format(_sw))
            # Clear bit 4 (falling edge) so the next run re-triggers cleanly.
            self.node.rpdo[1]["ControlWord"].raw = OP_ENABLED
            self.node.rpdo[1].transmit()
            self.node.network.sync.transmit()

        if self.ADC_ON == True:
            # Push fresh RPDO state so the first SYNC tick latches it, not stale inbox data.
            self.node.rpdo[1].transmit()
            self.node.network.sync.start()

        self.lastMode = quick_test

    def logo_click(self,event): # wxGlade: wxp3_frame.<event_handler>
        #print("Event Handler 'logo_click'")

        if self.ADC_ON == True:
           self.on_off_adc(self)
           self.adcWasON = True
        else:
           self.adcWasON = False

        pageURL = 'https://barrett.com/puck-motor-controller'
        webbrowser.open(pageURL) # Opens Barrett Support Site for marketing / customer help!
        print('Opening support site...')
        if self.ADC_ON == False and self.adcWasON == True:
            self.on_off_adc(self)

    def getMonitor(self):
        if self.ADC_ON == False:
            return
        try:
            # Read ADC for Puck Temperature, format properly, and update Frame
            ampTemp = self.node.tpdo[3]['Amplifier.Temperature'].raw
            ampTempString = str(ampTemp) + "C"
            if ampTempString != self.PTemp.GetLabel(): # Only updates label if there is a change
                #self.PTemp.SetLabel(ampTempString)
                #Colour Setting
                if ampTemp >= 250:
                    #Also needs buffer
                    if self.PTemp.GetLabel() != 'N/A':
                        print(self.PTemp.GetLabel())
                        self.PTemp.SetForegroundColour(wx.Colour(0,0,0))
                        self.PTemp.SetLabel('N/A')
                elif ampTemp >= self.temp_limit:
                    self.PTemp.SetLabel(ampTempString)
                    self.PTemp.SetForegroundColour(wx.Colour(245,16,0)) # Red
                elif 50 <= ampTemp < self.temp_limit:
                    self.PTemp.SetLabel(ampTempString)
                    self.PTemp.SetForegroundColour(self.orange) # Orange
                elif ampTemp < 0:
                    self.PTemp.SetLabel(ampTempString)
                    self.PTemp.SetForegroundColour(wx.Colour(115,155,208)) # Icy blue
                else:    
                    self.PTemp.SetLabel(ampTempString)
                    self.PTemp.SetForegroundColour(wx.Colour(0,0,0)) # Black
            if ampTemp >= 100:
                # Turn off test
                #Set Mode to IDLE
                self.lastMode = 0 # Reset lastMode
                self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE
                self.button_6.SetBackgroundColour(self.orange)
                self.button_6.SetLabel("Go")
                print("Puck Overheating - Stopping test...")

            # Read ADC for Bus Voltage, format properly, and update Frame
            #currentbyte = self.node.sdo.upload(0x3000,1)
            current = self.node.tpdo[2]['CurrentFeedback'].raw
            current = (current / 1000 * self.i_peak) * 1/math.sqrt(2) / 1000
            currentString = str(round(current,1)) + "A"
            # If current is 0 remove negative sign (if present)
            if round(current,1) == 0 and currentString[0] == "-":
                currentString = currentString[1:]
                
            if currentString != self.VBus.GetLabel():
                self.VBus.SetLabel(currentString)
                #Colour Setting
                if (current <= -self.i_peak / math.sqrt(2) / 1000 * self.peak_factor) or (current >= self.i_peak / math.sqrt(2) / 1000 * self.peak_factor):
                    self.VBus.SetForegroundColour(wx.Colour(245,16,0)) # Red
                elif (current <= -self.i_cont / math.sqrt(2) / 1000) or (current >= self.i_cont / math.sqrt(2) / 1000):
                    self.VBus.SetForegroundColour(self.orange) # Orange
                else:
                    self.VBus.SetForegroundColour(wx.Colour(0,0,0))
            # Read ADC for Motor Temperature, format properly, and update Frame
            #motorTempbyte = self.node.sdo.upload(0x3010,3) # This needs to be the correct value
            motorTemp = self.node.tpdo[3]['Motor.Therm'].raw / 10
            #motorTemp = int.from_bytes(motorTempbyte, byteorder='little', signed='signed')
            motorTempString = str(motorTemp) + "C"
            if True: # adding automatic N/A for Dev Kit App #motorTempString != self.MTemp.GetLabel() and motorTemp != 0 and motorTemp != -8 and motorTemp != -9 and motorTemp < ampTemp + 15:
                #Colour Setting
                if motorTemp == -273:
                    self.MTemp.SetForegroundColour(wx.Colour(0,0,0))
                    motorTempString = 'N/A'
                elif motorTemp >= 100:
                    self.MTemp.SetForegroundColour(wx.Colour(245,16,0))
                elif 75 <= motorTemp < 100:
                    self.MTemp.SetForegroundColour(wx.Colour(255,132,0))
                elif motorTemp < 0:
                    self.MTemp.SetForegroundColour(wx.Colour(115,155,208))
                else:
                    self.MTemp.SetForegroundColour(wx.Colour(0,0,0))
                self.MTemp.SetLabel(motorTempString)
            # elif motorTemp == 0 or motorTemp == -8 or motorTemp == -9 or motorTemp > ampTemp + 15: # Handles case of no motor thermistor present
            #     self.MTemp.SetLabel('N/A')
            #     self.MTemp.SetForegroundColour(wx.Colour(0,0,0))
        except:
            pass

    def getPosition(self): #Get RPM + Update every 10th cycle for 10Hz
        if self.ADC_ON == False:
            return        
        try:
            # Subtract the dial-zero captured at the last successful HOME so the dial recenters on
            # home. home-on-current-position (method 35/37) attains without necessarily resetting the
            # reported PositionFeedback, so we zero the DISPLAY here instead.
            encPos = self.node.tpdo[1]['PositionFeedback'].raw - getattr(self, '_dial_zero', 0)
            currentSysTime = time.time() # Get Current System time for accurate calc
            
            encPosRad = encPos * 2.0 * math.pi / self.encoderResolution / self.gearRatio # * 0.0015339 / self.gearRatio # added division by gear ratio 

            if self.motorPresent: # and Mode != 0: # Add Mode != 0 to stop updates when in idle (only useful for annoying graphics when no motor attached)
                if abs(encPosRad - self.lastPosRad) > 0.005: # if encPos has changed - this saves CPU usage and limits screen refreshes
                    img = self._dial_base_img.Copy()
                    img._W, img._H = img.GetSize()
                    center = (int(img._W/2),int(img._H/2))
                    img = img.Rotate(encPosRad, center,interpolating=True)
                    self.Dial.SetBitmap(img)
            else:
                self.Dial.SetBitmap(wx.Bitmap(self._dial_base_img))

            #if True: #self.firstRun != True:
               
            PVel = self.node.tpdo[2]['VelocityFeedback'].raw
            RPM = PVel * 60 / 4096 / self.gearRatio 
            RPM = round(RPM / 10, 1)
            RPM = round(RPM *10)
            RPMString = str(RPM)
            self.y = self.y + 1

            if self.y == 10:
                if self.motorPresent != True: # or Mode == 0: # Add Mode == 0 to turn off RPM during idle (only useful for annoying graphics with no motor)
                    self.Vrpm.SetLabel('N/A')
                    self.Vrpm.SetForegroundColour((0,0,0))
                elif RPMString != self.Vrpm.GetLabel():
                    self.Vrpm.SetLabel(RPMString)
                    self.Vrpm.SetForegroundColour((0,0,0))
                self.y = 0
                
            self.lastPosRad = encPosRad #set current position to last
            self.lastSysTime = currentSysTime #set current time to last
            self.firstRun = False
        except:
            #self.network.disconnect()
            print('Lost connection with node ' + str(self.getID()))
            print('Disconnecting...')
            #MyApp.removePuck(self,self.getID())

            self.on_off_adc(self)
            # If ADC Thread is turned off, still need to rescan after disconnect
            self.scan_pucks(None)
            self.on_off_adc(self)

            pass
   
    def onCloseFrame(self,event):
        # Surface immediate visual feedback that the close was registered.
        # The idle handshake + disconnect below can take up to ~1s, during
        # which the wx event loop is blocked and the window otherwise
        # appears frozen. Update() forces a synchronous paint so the new
        # status text reaches screen before we start the slow path.
        try:
            self.frame_statusbar.SetStatusText("Exiting...", 1)
            self.frame_statusbar.Update()
        except Exception:
            pass

        # Stop SYNC traffic first so it doesn't fight the SDOs below.
        try:
            self.network.sync.stop()
        except Exception:
            pass

        # Idle the active puck if there is one. Use a tight SDO timeout so a
        # disconnected/unresponsive puck can't hold the close path hostage
        # for the default 30-second timeout. Catch any exception so a stale
        # node, a torn-down bus, or a missing self.node never blocks exit.
        if len(MyApp.getNodes(self)) == 0:
            print("No active node; skipping idle handshake.")
        else:
            prev_timeout = canopen.sdo.SdoClient.RESPONSE_TIMEOUT
            canopen.sdo.SdoClient.RESPONSE_TIMEOUT = 0.5
            try:
                self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE
                # Verification loop: wait up to 500 ms for the hardware to confirm
                success = False
                timeout = time.time() + 0.5
                while time.time() < timeout:
                    if self.node.sdo[0x6061].raw == 0:
                        success = True
                        break
                    time.sleep(0.05)
                if success:
                    print("Puck successfully transitioned to IDLE.")
                else:
                    print("Warning: Puck did not confirm IDLE mode, but proceeding with removal.")
            except Exception as e:
                print(f"Idle handshake failed at close ({e!r}); proceeding.")
            finally:
                canopen.sdo.SdoClient.RESPONSE_TIMEOUT = prev_timeout
                MyApp.removePuck(self, self.getID())

        # Always release the CAN handle so the underlying transport (PCAN
        # on Windows, socketcan on Linux) doesn't keep its driver open
        # across the exit. Without this the close path can hang on Windows.
        try:
            self.network.disconnect()
        except Exception:
            pass

        self.Destroy()
        print('Closing Frame...')
        os._exit(0)

    def on_off_adc(self,event):
        try:
            if len(MyApp.getNodes(self)) > 0:
                # print(event.GetId())
                if self.ADC_ON == False:
                    print('Turning on ADC Monitor...')
                    # Refresh RPDO buffers from live drive state and TRANSMIT them
                    # so the firmware's RPDO inbox holds the current state before
                    # the first sync tick arrives (otherwise stale/empty inbox
                    # data is latched on first sync, idling the motor).
                    try:
                        mode = self.node.sdo[0x6061].raw  # Modes of Operation Display (read-only)
                        self.node.rpdo[1]["SetModeOfOperation"].raw = mode
                        if mode == 1:  # Position: hold at current feedback so a stale TargetPosition can't move us
                            self.node.rpdo[2]["TargetPosition"].raw = self.node.sdo["PositionFeedback"].raw
                            self.node.rpdo[2].transmit()
                        self.node.rpdo[1].transmit()
                    except Exception:
                        pass
                    self.network.sync.start(0.01)
                    self.ADC_ON = True
                    # Sync button state — required when on_off_adc is called
                    # from a path other than the button click itself (e.g. the
                    # Ctrl+P menu accelerator, which fires EVT_MENU directly).
                    self.onoff1.SetValue(1)

                elif self.ADC_ON == True:
                    print('Turning off ADC Monitor...')
                    #Turn off ADC Monitoring
                    # Stop sync transmission
                    try:
                        self.network.sync.stop()
                    except:
                        pass
                    self.ADC_ON = False

                    # Reset monitor values to N/A
                    self.VBus.SetLabel('N/A')
                    self.PTemp.SetLabel('N/A')
                    self.MTemp.SetLabel('N/A')
                    self.Vrpm.SetLabel('N/A')
                    self.VBus.SetForegroundColour((0,0,0))
                    self.PTemp.SetForegroundColour((0,0,0))
                    self.MTemp.SetForegroundColour((0,0,0))
                    self.Vrpm.SetForegroundColour((0,0,0))

                    self.Dial.SetBitmap(wx.Bitmap(self._dial_base_img))
                    # Sync button state — required when on_off_adc is called
                    # from a path other than the button click itself (e.g. the
                    # Ctrl+P menu accelerator, which fires EVT_MENU directly).
                    self.onoff1.SetValue(0)
            else:
                # Need to update the custom button to allow setting!
                print('No Puck Connected -')
                print('Turning off ADC Monitor...')
                # Explicitly set ON first so the visual feedback fires even
                # when on_off_adc was triggered by the Ctrl+P menu accelerator
                # (which never toggles the button). When invoked from the
                # button click itself, the click handler already toggled the
                # button to ON, so SetValue(1) is a harmless no-op.
                # CallLater (not time.sleep) lets the event loop process the
                # pending ON paint and briefly render it before we reset to
                # OFF — gives the user visual feedback that the request was
                # received and intentionally rejected.
                self.onoff1.SetValue(1)
                wx.CallLater(100, self.onoff1.SetValue, 0)
        except:
            # self.on_off_adc(None) # incorrect
            # Reset Button to off
            print('No Puck Connected -')
            print('Turning off ADC Monitor...')
            self.onoff1.SetValue(1)
            wx.CallLater(100, self.onoff1.SetValue, 0)
            pass

class MyApp(wx.App):

    def _bring_to_front(self, frame):
        """Pull `frame` to the foreground at startup. A plain Raise() is unreliable
        under GTK/GNOME focus-stealing prevention -- the window lands behind the
        terminal that launched it -- so momentarily mark it always-on-top to force
        the window manager to raise + focus it, then drop that flag a beat later so
        it behaves like a normal window."""
        if frame is None:
            return
        try:
            frame.Show()
            if frame.IsIconized():
                frame.Iconize(False)
            base_style = frame.GetWindowStyle()
            frame.SetWindowStyle(base_style | wx.STAY_ON_TOP)
            frame.Raise()
            frame.RequestUserAttention()
            wx.CallLater(600, lambda: self._drop_topmost(frame, base_style))
        except Exception:
            pass

    def _drop_topmost(self, frame, base_style):
        try:
            frame.SetWindowStyle(base_style)
            frame.Raise()
        except Exception:
            pass

    # Set by __main__ before instantiation when --touchscreen is passed.
    touchscreen = False

    def OnInit(self):
        #self.SetTopWindow(self.frame)
        wx.App.ActiveID = []
        wx.App.Nodes = []

        # X11/XWayland fallback for the dock-icon association (the Wayland
        # app_id is pinned via g_set_prgname in _setup_linux_desktop_integration).
        self.SetAppName('PuckUtilityApp')
        self.SetClassName('PuckUtilityApp')

        # Rounded, transparent-corner splash (shared splash_screen helper).
        self._splash = splash_screen.show_splash(
            resource_path(os.path.join("images", "Splash.png")))
        # Return immediately so the event loop starts and the splash is fully
        # painted by the OS before the heavy frame construction begins.
        wx.CallLater(100, self._finish_init)
        return True

    def _make_splash_bitmap(self):
        # Splash is pre-composed (Background.png + the navy Barrett logo) into
        # images/Splash.png, so the banner logo (BarrettLogoScaled-NoBG.png) and
        # the splash can be sized independently of each other.
        return wx.Bitmap(resource_path(os.path.join("images", "Splash.png")), wx.BITMAP_TYPE_PNG)

    def _finish_init(self):
        self.frame = MyFrame(None, wx.ID_ANY, "")
        # Show the frame first so it gets its initial paint while the
        # STAY_ON_TOP splash still covers it.  Then destroy the splash —
        # the compositor reveals an already-rendered frame with no gap.
        # (Previous order was Destroy→Show, which left a brief instant with
        # no window on screen between the two operations.)
        self.frame.Show()
        wx.SafeYield()             # let GTK map the window and allocate its REAL size
        if MyApp.touchscreen:
            # Maximize so the window fills the work area on the 7" Pi screen
            # while keeping the menu bar, status bar, and frame border all
            # intact (ShowFullScreen strips that chrome, which isn't what we
            # want here).
            self.frame.Maximize(True)
        else:
            # Center AFTER Show(): on wxGTK the frame's real on-screen size is
            # only known once GTK has mapped the window, so positioning before
            # Show() mis-centers on the FIRST launch — the window manager only
            # "fixes" it on later launches by restoring the previous geometry,
            # which is exactly why a second open looked centred but the first
            # didn't. The splash (STAY_ON_TOP) still covers the frame here, so
            # the move is not visible. Center on the display under the mouse
            # cursor (the monitor in use); plain Centre() can land on the wrong
            # display or the seam between monitors on a multi-monitor desktop.
            try:
                _d = wx.Display.GetFromPoint(wx.GetMousePosition())
                _area = wx.Display(_d if _d != wx.NOT_FOUND else 0).GetClientArea()
                _w, _h = self.frame.GetSize()
                self.frame.SetPosition((_area.x + max(0, _area.width - _w) // 2,
                                        _area.y + max(0, _area.height - _h) // 2))
            except Exception:
                self.frame.CentreOnScreen()
        self._splash.Destroy()     # now remove splash — frame is positioned + rendered underneath
        # Pull the window to the foreground. Raise() alone often loses to GNOME's
        # focus-stealing prevention (the window opens behind the launching terminal
        # with no focus), so _bring_to_front momentarily marks it always-on-top to
        # force the window manager to raise + focus it.
        self._bring_to_front(self.frame)

        self.Bind(wx.EVT_KEY_DOWN, self.frame.onKeyDown)
        self.Bind(wx.EVT_KEY_UP,   self.frame.onKeyUp)
        self.frame.set_tool_tips(None)

        # Defer CAN connection until the frame is fully composited.  Running
        # can_port immediately after Show() risks a brief event-loop stall
        # from socket operations landing before the compositor has settled.
        wx.CallLater(200, self._startup_connect)

    def _startup_connect(self):
        # silent=True so a missing CAN device surfaces only on the status bar.
        result = self.frame.can_port(None, silent=True)

        # Transmit an NMT reboot command to this node
        if result == True:
            try:
                print("Booting...")
                self.frame.network.send_message(0x0, [0x81, 0])
                time.sleep(0.5) # wait for puck to reboot (avoids loss of communication)
                self.frame.scan_pucks(self)
                self.initialize = self.frame.network.scanner.nodes
                # Placement causes node not to get added!!
                if len(self.getNodes()) == 0:
                    # print('No Pucks active')
                    return
                # print(self.frame.GetSize())

                self.addPucks(self.frame.getID())
                i = len(self.getNodes())
                if i == 0:
                    return
            except Exception as e:
                # print(e)
                pass

    def addPucks(self,i): # Adds Puck ID to list of Active Frames
        # Idempotent: select_id's firstRun branch and OnInit can both end up
        # calling this for the same id when a retry-scan succeeds; without
        # this guard ActiveID grows duplicates that confuse later bookkeeping.
        if i in wx.App.ActiveID:
            return
        wx.App.ActiveID.append(i)
        print('Adding Puck...')

    def removePuck(self,i):
        # Defensive: if the id was never added (e.g. retry-scan path that
        # bypassed addPucks before the bug fix, or any future bookkeeping
        # gap), don't let it block the close path with a ValueError.
        if i not in wx.App.ActiveID:
            print('Removing Puck (not tracked, skipping)...')
            return
        wx.App.ActiveID.remove(i)
        print('Removing Puck...')
        return

    def getPucks(self): # Gets list of IDs in Active Frames
        return wx.App.ActiveID

    def updateNodes(self,i):
        wx.App.Nodes = i

    def getNodes(self):
        return wx.App.Nodes

# ---- Logging ----------------------------------------------------------------

def _setup_logging():
    """Create a per-session log folder, tee stdout/stderr into it, and expose
    the folder via paths.SESSION_LOG_DIR so calibration outputs land there too.
    Keeps the 50 most recent session folders."""
    import paths as _paths

    # In a PyInstaller --onefile bundle, __file__ resolves into the extracted
    # MEIPASS temp dir which is wiped on exit.  Anchor next to the executable.
    # Fall back to XDG user data dir when the app directory is not writable.
    if getattr(sys, 'frozen', False):
        base_dir = os.path.dirname(sys.executable)
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))
    log_dir = os.path.join(base_dir, 'logs')
    try:
        os.makedirs(log_dir, exist_ok=True)
        if not os.access(log_dir, os.W_OK):
            raise OSError("not writable")
    except OSError:
        log_dir = os.path.join(
            os.path.expanduser('~'), '.local', 'share', 'PuckUtilityApp', 'logs')
        os.makedirs(log_dir, exist_ok=True)

    # Rotate: keep at most 50 session folders (oldest first).
    # Also tolerate legacy bare .log files left by older versions.
    existing = sorted(
        e for e in os.listdir(log_dir) if e.startswith('puck_')
    )
    while len(existing) >= 50:
        victim = os.path.join(log_dir, existing.pop(0))
        if os.path.isdir(victim):
            import shutil
            shutil.rmtree(victim, ignore_errors=True)
        else:
            try:
                os.remove(victim)
            except OSError:
                pass

    timestamp = datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    session_dir = os.path.join(log_dir, f'puck_{timestamp}')
    os.makedirs(session_dir, exist_ok=True)
    _paths.SESSION_LOG_DIR = session_dir

    log_path = os.path.join(session_dir, f'puck_{timestamp}.log')
    # Expose the path to the in-app Log viewer (Log menu -> Open Log...).
    log_viewer.set_log_path(log_path)
    try:
        log_file = open(log_path, 'w', buffering=1)
    except OSError as e:
        print(f"Warning: could not open log file {log_path}: {e}")
        return

    log_file.write(f"=== Puck Utility App ===\n")
    log_file.write(f"Started : {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    log_file.write(f"Command : {' '.join(sys.argv)}\n")
    log_file.write(f"{'=' * 23}\n\n")
    log_file.flush()

    class _Tee:
        # PyInstaller --noconsole bundles can set sys.stdout/sys.stderr
        # to None, in which case writing to it raises AttributeError and
        # would kill the first print() after the tee is installed.
        # Guard each delegated call so an absent original silently
        # drops to logfile-only.
        def __init__(self, original, log):
            self._original = original
            self._log = log
        def write(self, data):
            if self._original is not None:
                try:
                    self._original.write(data)
                except (OSError, ValueError):
                    pass
            self._log.write(data)
        def flush(self):
            if self._original is not None:
                try:
                    self._original.flush()
                except (OSError, ValueError):
                    pass
            self._log.flush()
        def fileno(self):
            if self._original is None:
                raise OSError("no fileno (stdout/stderr unavailable)")
            return self._original.fileno()
        def isatty(self):
            return self._original is not None and self._original.isatty()

    sys.stdout = _Tee(sys.stdout, log_file)
    sys.stderr = _Tee(sys.stderr, log_file)

# ---- CLI helpers ------------------------------------------------------------
# Moved to cli_ops.py (no wx dependency). Imported here only so __main__ can
# dispatch to them when CLI flags are present.
from cli_ops import (
    _cli_connect, _cli_flash, _cli_config, _cli_calibrate_all,
    _cli_calibrate_itiming, _cli_calibrate_slope,
    _cli_make_network, _cli_system_config,
    _cli_info,
)


# ---- Entry point ------------------------------------------------------------

def _setup_linux_desktop_integration(app_id, display_name):
    """Make the GNOME/Wayland dock show our window's icon when run from source.

    Under Wayland the compositor matches a window to a .desktop file via its
    app_id and shows that file's Icon=; wx's SetIcon()/_NET_WM_ICON is ignored.
    GTK3 derives the app_id from GLib's program name, so we (1) pin it via
    g_set_prgname() through ctypes (no PyGObject dependency) before the first
    window is mapped, and (2) when running from source — where no installed
    .desktop exists (frozen builds get one from install-ubuntu.sh) — drop a
    matching <app_id>.desktop into the per-user applications dir pointing Icon=
    at the in-tree PNG. Both steps are best-effort; failures are non-fatal.
    """
    if not sys.platform.startswith('linux'):
        return
    try:
        import ctypes
        ctypes.CDLL('libglib-2.0.so.0').g_set_prgname(app_id.encode())
    except Exception:
        pass
    if getattr(sys, 'frozen', False):
        return
    # Defer to a system-installed (.deb) entry of the same id. Writing a
    # per-user entry would shadow it (XDG precedence) and make the dock launch
    # the source tree instead of the installed app. Clean up any stale per-user
    # entry a previous source run left, so the installed one wins.
    try:
        if os.path.exists(os.path.join(
                '/usr/share/applications', f'{app_id}.desktop')):
            user_entry = os.path.join(
                os.environ.get('XDG_DATA_HOME',
                               os.path.expanduser('~/.local/share')),
                'applications', f'{app_id}.desktop')
            try:
                os.remove(user_entry)
            except OSError:
                pass
            return
    except Exception:
        pass
    try:
        src_dir = os.path.dirname(os.path.abspath(__file__))
        entry = (
            "[Desktop Entry]\n"
            "Version=1.0\n"
            "Type=Application\n"
            "Terminal=false\n"
            f"Name={display_name}\n"
            # sys.executable is the venv interpreter when run from source, so
            # launching from the dock entry picks up the right dependencies.
            f"Exec={sys.executable} {os.path.join(src_dir, os.path.basename(__file__))}\n"
            f"Path={src_dir}\n"
            f"Icon={os.path.join(src_dir, 'images', 'BarrettIcon.png')}\n"
            f"StartupWMClass={app_id}\n"
            "Categories=Utility;\n"
        )
        apps_dir = os.path.join(
            os.environ.get('XDG_DATA_HOME', os.path.expanduser('~/.local/share')),
            'applications')
        dest = os.path.join(apps_dir, f'{app_id}.desktop')
        # Only (re)write when missing or stale (e.g. the source tree moved) so
        # we don't churn the file or needlessly clobber an installed entry.
        current = None
        if os.path.isfile(dest):
            with open(dest, encoding='utf-8') as f:
                current = f.read()
        if current != entry:
            os.makedirs(apps_dir, exist_ok=True)
            with open(dest, 'w', encoding='utf-8') as f:
                f.write(entry)
    except Exception:
        pass


if __name__ == "__main__":
    # When frozen, ensure cwd is the app directory so bare relative paths
    # (puck4.eds, images/, config/, etc.) resolve correctly regardless of
    # how the binary was launched (desktop file, terminal, etc.).
    if getattr(sys, 'frozen', False):
        os.chdir(os.path.dirname(sys.executable))

    _setup_logging()

    parser = argparse.ArgumentParser(
        prog='puckutilityapp.py',
        description='Puck Utility App — launches GUI when run with no arguments, '
                    'or runs headlessly when CLI flags are provided.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Must be run from the puckutility directory so that puck4.eds is accessible.

Examples:
  # Launch GUI
  python3 puckutilityapp.py

  # Flash firmware to node 1
  python3 puckutilityapp.py --can can0 --id 1 --flash firmware/P4-v1.1.5.bin

  # Upload config CSV to nodes 1 and 2
  python3 puckutilityapp.py --can can0 --id 1 2 --config config/motor.csv

  # Calibrate all discovered pucks
  python3 puckutilityapp.py --can can0 --all --calibrate

  # Apply system config INI (handles firmware check, config upload, optional calibration)
  python3 puckutilityapp.py --can can0 --system-config system.ini

  # Flash bundled CandleLight Multiboard firmware to an STM32G431 canable via USB DFU
  python3 puckutilityapp.py --flash-canable

  # Flash a specific firmware file instead
  python3 puckutilityapp.py --flash-canable path/to/custom.bin
"""
    )
    parser.add_argument('--can', metavar='DEVICE',
                        help='CAN device (e.g. can0 on Linux, 0 for PCAN_USBBUS1 on Windows)')
    parser.add_argument('--id', type=int, nargs='+', metavar='ID',
                        help='One or more target node IDs')
    parser.add_argument('--all', action='store_true',
                        help='Scan the bus and apply operation to all discovered pucks')
    parser.add_argument('--touchscreen', action='store_true',
                        help='GUI mode only: launch fullscreen (e.g. for the 7" Raspberry Pi touchscreen)')

    ops = parser.add_mutually_exclusive_group()
    ops.add_argument('--scan', action='store_true',
                     help='Scan the CAN bus and print all discovered node IDs')
    ops.add_argument('--info', action='store_true',
                     help='Print firmware version, flashloader version, model, motor '
                          'params, and live status for each node. Reading the flashloader '
                          'version briefly reboots each puck into the bootloader and back. '
                          'Use with --id or --all to target specific nodes; omit both to '
                          'show all found nodes.')
    ops.add_argument('--flash', metavar='FIRMWARE',
                     help='Path to firmware file (.bin or .ebin)')
    ops.add_argument('--config', metavar='CSV',
                     help='Path to motor configuration CSV file')
    ops.add_argument('--calibrate', action='store_true',
                     help='Run full calibration (test_encoder, ibias, igainfactor, slope, enczero, fold)')
    ops.add_argument('--calibrate-settling', action='store_true', dest='calibrate_settling',
                     help='Run ONLY the MaxSettlingTime (ADC settling) calibration')
    ops.add_argument('--calibrate-slope', action='store_true', dest='calibrate_slope',
                     help='Run ONLY the Current Sense Slope calibration')
    ops.add_argument('--system-config', metavar='INI', dest='system_config',
                     help='Path to system configuration INI file')
    ops.add_argument('--flash-canable', metavar='FIRMWARE', nargs='?', const='',
                     dest='flash_canable',
                     help='Flash CandleLight Multiboard firmware via USB DFU '
                          '(uses bundled firmware when no path is given)')

    parser.add_argument('--verbose', action='store_true',
                        help='Show detailed output during --flash-canable')

    args = parser.parse_args()

    # No operation flag → launch GUI. --touchscreen is a GUI-mode flag, so
    # passing it alone (or with nothing else) still falls into this branch.
    if not (args.scan or args.info or args.flash or args.config
            or args.calibrate or args.calibrate_settling or args.calibrate_slope
            or args.system_config or args.flash_canable is not None):
        MyApp.touchscreen = args.touchscreen
        # Must run before MyApp() creates the first window so the Wayland
        # app_id is set when the toplevel is mapped.
        _setup_linux_desktop_integration('PuckUtilityApp', 'Puck Utility')
        app = MyApp(0)
        app.MainLoop()
        sys.exit(0)

    # --flash-canable uses pyusb directly; --can is not required
    if args.flash_canable is not None:
        from cli_ops import flash_canable
        ok = flash_canable(args.flash_canable or None, verbose=args.verbose)
        sys.exit(0 if ok else 1)

    # All other operations require --can
    if not args.can:
        parser.error('--can is required')

    # --scan: list nodes on the bus and exit
    if args.scan:
        net, found = _cli_connect(args.can)
        net.disconnect()
        sys.exit(0)

    # --info: reads versions/status; the flashloader read reboots each puck into
    # the bootloader and relaunches it. --id/--all optional (omit both for all).
    if args.info:
        node_ids = args.id if args.id else None
        _cli_info(args.can, node_ids)
        sys.exit(0)

    # --system-config is self-contained; --id/--all are not used with it
    if args.system_config:
        _cli_system_config(args.can, args.system_config)
        sys.exit(0)

    # Remaining operations need an explicit target
    if not args.id and not args.all:
        parser.error('specify target nodes with --id or use --all to scan')
    if not (args.flash or args.config or args.calibrate
            or args.calibrate_settling or args.calibrate_slope):
        parser.error('specify an operation: --scan, --flash, --config, --calibrate, '
                     '--calibrate-settling, --calibrate-slope, or --system-config')

    # Always scan first so we can validate requested IDs against the live bus
    scan_net, found_ids = _cli_connect(args.can)
    scan_net.disconnect()
    if not found_ids:
        print("No nodes found on bus.")
        sys.exit(1)

    if args.all:
        node_ids = found_ids
    else:
        node_ids = []
        for nid in args.id:
            if nid in found_ids:
                node_ids.append(nid)
            else:
                print(f"Warning: node {nid} not found on bus, skipping.")
        if not node_ids:
            print("None of the requested nodes are present on the bus.")
            sys.exit(1)

    # Execute operation across all validated target nodes
    _any_cal = args.calibrate or args.calibrate_settling or args.calibrate_slope
    if _any_cal:
        cal_net = _cli_make_network(args.can)
    for node_id in node_ids:
        print(f"\n--- Node {node_id} ---")
        if args.flash:
            _cli_flash(args.can, node_id, args.flash)
        elif args.config:
            _cli_config(args.can, node_id, args.config)
        elif args.calibrate:
            cal_node = cal_net.add_node(node_id, 'puck4.eds')
            _cli_calibrate_all(cal_node)
        elif args.calibrate_settling:
            cal_node = cal_net.add_node(node_id, 'puck4.eds')
            _cli_calibrate_itiming(cal_node)
        elif args.calibrate_slope:
            cal_node = cal_net.add_node(node_id, 'puck4.eds')
            _cli_calibrate_slope(cal_node)
    if _any_cal:
        cal_net.disconnect()
