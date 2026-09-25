#!/usr/bin/env python3
"""
Furuta Pendulum — Energy Swingup + PID Balance Controller
  Puck 1 (node 1) — rotating arm motor driven via Cyclic-Sync Torque (CST) mode
  Puck 2 (node 2) — passive pendulum encoder (read-only)

Three automatic modes:
  SWINGUP  — energy-based pump builds pendulum amplitude from rest
  BRAKING  — energy-based damping absorbs excess energy near upright
  BALANCE  — PID holds upright once within ±15°

  Kp  [torque/rad]         Balance: proportional angle correction.
  Ki  [torque/(rad·s)]     Balance: integral, eliminates steady-state drift.
  Kd  [torque/(rad/s)]     Balance: derivative velocity damping.
  Ks  [torque units]       Swingup: peak torque magnitude per pump half-cycle.
  Kb  [torque units]       Braking: peak torque magnitude per brake half-cycle.
  Kv  [torque units/s]     Swingup/braking torque slew-rate limit.

Angle convention: θ = 0 upright, ±π hanging down (pendulum, P2).

CST vs CSP:
  In CSP mode the output was a target arm position (counts); the Puck's
  internal servo loop tracked it.  In CST mode the output is a direct
  torque command (drive units per the EDS); the Puck applies that torque
  with no inner position loop.  Balance gains Kp/Ki/Kd are therefore in
  torque/rad rather than counts/rad, and Ks/Kb are peak torque values
  rather than peak arm-travel distances.  When the controller is off the
  arm moves freely (zero torque); it no longer holds position.

  Note on TargetTorque units: the EDS defines the scaling.  For DS402
  drives this is typically 0.1 % of rated torque (1000 = 100 %).  Verify
  against your puck4.eds and scale Ks, Kb, and Torque limit accordingly.

Usage:
  python3 furuta_pendulum.py                 # engineering GUI (gains, scan, zero, bias)
  python3 furuta_pendulum.py --touchscreen   # full-screen customer kiosk (see
                                             # furuta_kiosk.py): auto-connect,
                                             # START/STOP only, fixed gains
      [--auto-stop SECONDS]                  # each run stops itself after this
                                             # long (default 60; 0 = never)
"""

import kiosk_widgets  # noqa: F401  -- sets the Linux display env; must precede wx
import wx
import canopen
import platform
import time
import math
import threading
import traceback
import glob
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from canopen_runner import (
    CLEAR_FAULT, SHUTDOWN, OP_ENABLED,
    MODE_IDLE, MODE_CYCLIC_SYNC_POS          # keep import; MODE_IDLE still used
)

# CANopen DS402 mode 10 — Cyclic Synchronous Torque
MODE_CYCLIC_SYNC_TORQUE = 10

ENCODER_RES     = 4096
EDS_FILE        = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'puck4.eds')
SYNC_HZ         = 500
BALANCE_ENTRY   = math.radians(15)   # engage PID inside ±15°
BALANCE_EXIT    = math.radians(25)   # disengage outside ±25°
BALANCE_VEL_MAX = 10.0               # rad/s — max velocity to engage
INTEGRAL_CLAMP  = 2048               # rad·s — anti-windup clamp on integral
PEND_LENGTH_M   = 0.2413             # pendulum rod length (m) — 9.5 inches
ARM_LENGTH_M    = 0.1238             # rotating arm length (m) — 4.875 inches
COUPLING        = ARM_LENGTH_M / PEND_LENGTH_M   # κ = L₁/L₂
OMEGA_N_SQ      = 9.8 / PEND_LENGTH_M            # g/L₂ (rad/s)²
MAX_ARM_REV     = 5.0                # soft travel guard (revolutions)
MAX_ARM_CTS     = int(MAX_ARM_REV * ENCODER_RES)
DISPLAY_HZ      = 25
BALANCE_RAMP_ON_RATE  = 1.0
BALANCE_RAMP_OFF_RATE = 1.0

# Tunable parameter defaults
KP_DEFAULT          = 4000   # [torque / rad]
KI_DEFAULT          = 0      # [torque / (rad·s)]
KD_DEFAULT          = 100    # [torque / (rad/s)]
ADZ_DEFAULT         = 0.7    # [deg]
BI_DEFAULT          = 0      # [deg]
KT_DEFAULT          = 0.005  # [rad/rad]    — outer position loop (unchanged)
KF_DEFAULT          = 0.009  # [rad/(rad/s)] — outer position loop (unchanged)
PDZ_DEFAULT         = 0      # [deg]
TMAX_DEFAULT        = 4      # [deg]
KS_DEFAULT          = 350    # [torque units] peak swingup torque
KB_DEFAULT               = 250   # [torque units] peak braking torque
KV_DEFAULT               = 4000  # [torque units/s] torque slew-rate limit
KDA_DEFAULT              = 25    # [torque units / (rad/s)] arm velocity damping during swingup
TORQUE_LIM_SWING_DEFAULT = 400   # [torque units] output clamp during swingup/braking
TORQUE_LIM_BAL_DEFAULT   = 300   # [torque units] output clamp during balance
DISABLE_TEMP        = 90

#TODO: Make shutdown temperature settable

# ──────────────────────────────────────────────────────────── canvas ──────

class FurutaCanvas(wx.Panel):
    """Cart-pole style visualization (arm angle shown as cart position)."""

    BG        = wx.Colour(28, 30, 38)
    TRACK     = wx.Colour(75, 80, 100)
    CART      = wx.Colour(65, 110, 200)
    CART_EDGE = wx.Colour(120, 165, 255)
    WHEEL     = wx.Colour(42, 44, 58)
    PIVOT     = wx.Colour(210, 215, 230)
    LABEL     = wx.Colour(185, 192, 210)

    def __init__(self, parent, kiosk=False, **kw):
        super().__init__(parent, **kw)
        self._pend_rad = 0.0
        self._arm_cts  = 0
        self._mode     = "idle"
        self._kiosk    = kiosk      # bigger drawing, no numeric labels
        self.SetBackgroundColour(self.BG)
        self.SetMinSize((-1, 290))
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_SIZE, lambda _: self.Refresh())

    def update(self, pend_rad, arm_cts, mode):
        self._pend_rad = pend_rad
        self._arm_cts  = arm_cts
        self._mode     = mode
        self.Refresh()

    def _on_paint(self, _):
        dc = wx.BufferedPaintDC(self)
        W, H = self.GetClientSize()
        dc.SetBackground(wx.Brush(self.BG))
        dc.Clear()

        # ── track ────────────────────────────────────────────────────────
        ty  = int(H * 0.68)
        tx0 = int(W * 0.06)
        tx1 = int(W * 0.94)
        tw  = tx1 - tx0
        dc.SetPen(wx.Pen(self.TRACK, 3))
        dc.DrawLine(tx0, ty + 16, tx1, ty + 16)

        # travel-limit tick marks
        dc.SetPen(wx.Pen(wx.Colour(120, 60, 60), 1))
        for side in (-1, 1):
            lx = int(tx0 + tw / 2 + side * tw / 2 * 0.92)
            dc.DrawLine(lx, ty + 8, lx, ty + 24)

        # ── cart (arm position mapped to track) ──────────────────────────
        # Kiosk: scale the fixed-pixel parts with the canvas height (the
        # desktop GUI keeps its original look at s = 1).
        s    = max(1.0, H / 290.0) if self._kiosk else 1.0
        frac = max(-1.0, min(1.0, self._arm_cts / MAX_ARM_CTS))
        cx   = int(tx0 + tw * (0.5 + 0.5 * frac))
        cw, ch = int(46 * s), int(22 * s)

        dc.SetBrush(wx.Brush(self.CART))
        dc.SetPen(wx.Pen(self.CART_EDGE, int(2 * s)))
        dc.DrawRoundedRectangle(cx - cw // 2, ty, cw, ch, int(5 * s))

        dc.SetBrush(wx.Brush(self.WHEEL))
        dc.SetPen(wx.Pen(wx.Colour(85, 90, 115), 1))
        for wx_ in (cx - int(14 * s), cx + int(14 * s)):
            dc.DrawCircle(wx_, ty + ch + int(4 * s), int(6 * s))

        # ── pendulum ─────────────────────────────────────────────────────
        px, py  = cx, ty + 2
        arm_len = int(H * 0.56)
        ang     = self._pend_rad
        ex = int(px + arm_len * math.sin(ang))
        ey = int(py - arm_len * math.cos(ang))

        nearness = max(0.0, 1.0 - abs(ang) / math.pi)
        r = int(255 * (1.0 - nearness ** 1.5))
        g = int(210 * nearness)
        arm_col = wx.Colour(r, g, 40)

        dc.SetPen(wx.Pen(arm_col, int(7 * s)))
        dc.DrawLine(px, py, ex, ey)

        dc.SetBrush(wx.Brush(arm_col))
        dc.SetPen(wx.Pen(wx.Colour(240, 240, 240), 1))
        dc.DrawCircle(ex, ey, int(11 * s))

        dc.SetBrush(wx.Brush(self.PIVOT))
        dc.DrawCircle(px, py, int(5 * s))

        dc.SetPen(wx.Pen(wx.Colour(80, 180, 80, 90), 1, wx.PENSTYLE_DOT))
        dc.DrawLine(px, py, px, py - arm_len)

        if self._kiosk:
            return      # the kiosk shows state in its own status line

        # ── labels ───────────────────────────────────────────────────────
        dc.SetTextForeground(self.LABEL)
        dc.SetFont(wx.Font(9, wx.FONTFAMILY_MODERN,
                           wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        ang_deg = math.degrees(ang)
        arm_deg = self._arm_cts * 360.0 / ENCODER_RES
        mode_colours = {
            "balance": (wx.Colour(60,  210, 100), "[ BALANCING ]"),
            "brake":   (wx.Colour(220,  90,  40), "[ BRAKING   ]"),
            "swing":   (wx.Colour(210, 150,  40), "[ SWINGUP   ]"),
            "rest":    (wx.Colour(210, 150,  40), "[ RESTING   ]"),
            "idle":    (wx.Colour(140, 140, 160), "[ IDLE      ]"),
        }
        col, mode_str = mode_colours.get(self._mode, mode_colours["idle"])
        dc.DrawText(f"Pendulum (P2): {ang_deg:+7.2f}°", 8, 6)
        dc.DrawText(f"Arm      (P1): {arm_deg:+7.2f}°", 8, 22)
        dc.SetTextForeground(col)
        dc.DrawText(mode_str, W - 120, 6)


# ──────────────────────────────────────────────────────── main frame ──────

class FurutaPIDFrame(wx.Frame):

    TEMP_SHUTDOWN_C = 85

    def __init__(self):
        super().__init__(None, title="Furuta Pendulum Controller  [CST]", size=(720, 520))

        self._network = None
        self._node1   = None
        self._node2   = None
        self._lock    = threading.Lock()

        self._puck1_pos  = 0
        self._puck2_pos  = 0
        self._puck1_zero = 0
        self._puck2_zero = 0

        self._last_draw        = 0.0
        self._connected        = False
        self._enabled          = False
        self._controlling      = False
        self._no_swing         = False
        self._in_balance       = False
        self._ramping_balance  = False
        self._in_braking       = False
        self._ctrl_thread      = None
        self._bi = 0.0
        self._rpdo2_backup     = None   # saved before remapping to TargetTorque
        # canopen's SDO client isn't thread-safe: serialise the enable/disable
        # sequences against the background temperature/health polling.
        self._sdo_lock         = threading.RLock()
        self._last_rx          = [0.0, 0.0]   # monotonic time of last TPDO (P1, P2)

        self._build_ui()
        kiosk_widgets.set_app_icon(self)
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.SetMinSize((580, 460))
        self.Centre()

    # ───────────────────────────────────────────────── UI ───────────────

    def _build_ui(self):
        root = wx.Panel(self)
        root.SetBackgroundColour(wx.Colour(235, 238, 248))
        vsz = wx.BoxSizer(wx.VERTICAL)

        def gain(sizer, label, default, tip):
            sizer.Add(wx.StaticText(root, label=label),
                      0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 8)
            tc = wx.TextCtrl(root, value=str(default), size=(62, -1))
            tc.SetToolTip(tip)
            sizer.Add(tc, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT | wx.RIGHT, 3)
            return tc

        # Row 1 — CAN port + scan
        csz1 = wx.BoxSizer(wx.HORIZONTAL)
        csz1.Add(wx.StaticText(root, label="CAN port:"),
                 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 8)
        self._port = wx.Choice(root, choices=self._can_ports())
        if self._port.GetCount():
            self._port.SetSelection(0)
        csz1.Add(self._port, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT | wx.RIGHT, 6)

        self._btn_scan = wx.Button(root, label="Scan", size=(70, -1))
        self._btn_scan.SetToolTip("Scan the CAN bus for nodes, then assign Motor and Encoder below.")
        self._btn_scan.Bind(wx.EVT_BUTTON, self._on_scan)
        csz1.Add(self._btn_scan, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 12)

        csz1.Add(wx.StaticText(root, label="Motor:"),
                 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self._motor_choice = wx.Choice(root, choices=[], size=(70, -1))
        self._motor_choice.SetToolTip("Node ID of the arm-drive puck (Puck 1).")
        self._motor_choice.Disable()
        csz1.Add(self._motor_choice, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)

        csz1.Add(wx.StaticText(root, label="Encoder:"),
                 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        self._enc_choice = wx.Choice(root, choices=[], size=(70, -1))
        self._enc_choice.SetToolTip("Node ID of the passive pendulum encoder puck (Puck 2).")
        self._enc_choice.Disable()
        csz1.Add(self._enc_choice, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        vsz.Add(csz1, 0, wx.EXPAND | wx.TOP | wx.BOTTOM, 4)

        # Row 2 — connect / motor controls
        csz2 = wx.BoxSizer(wx.HORIZONTAL)
        self._btn_conn = wx.Button(root, label="Connect", size=(90, -1))
        self._btn_conn.SetToolTip("Connect to the selected Motor and Encoder nodes.")
        self._btn_conn.Bind(wx.EVT_BUTTON, self._on_conn_toggle)
        self._btn_conn.Disable()
        csz2.Add(self._btn_conn, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT | wx.RIGHT, 8)

        self._btn_en = wx.Button(root, label="Enable Motor", size=(105, -1))
        self._btn_en.Bind(wx.EVT_BUTTON, self._on_enable_toggle)
        self._btn_en.Disable()
        csz2.Add(self._btn_en, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)

        self._btn_zero = wx.Button(root, label="Zero", size=(58, -1))
        self._btn_zero.SetToolTip(
            "Let the pendulum hang freely then click to capture the downward rest position.")
        self._btn_zero.Bind(wx.EVT_BUTTON, self._on_zero)
        self._btn_zero.Disable()
        csz2.Add(self._btn_zero, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)

        vsz.Add(csz2, 0, wx.EXPAND | wx.BOTTOM, 4)

        # Status + temperature row
        ssz = wx.BoxSizer(wx.HORIZONTAL)
        self._status = wx.StaticText(root, label="Disconnected", style=wx.ALIGN_CENTER_HORIZONTAL)
        self._status.SetForegroundColour(wx.Colour(160, 60, 60))
        f = self._status.GetFont(); f.MakeBold(); self._status.SetFont(f)
        ssz.Add(self._status, 1, wx.EXPAND | wx.ALL, 8)
        self._temp_label = wx.StaticText(root, label="Temp: --°C", style=wx.ALIGN_CENTER_HORIZONTAL)
        self._temp_label.SetForegroundColour(wx.Colour(120, 130, 160))
        ssz.Add(self._temp_label, 1, wx.EXPAND | wx.ALL, 10)
        vsz.Add(ssz, 0, wx.EXPAND | wx.BOTTOM, 8)

        # Canvas
        self._canvas = FurutaCanvas(root)
        vsz.Add(self._canvas, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 6)

        # Gains — three rows inside one static box
        gbx = wx.StaticBox(root, label="Controller Gains  (CST mode — torque units per EDS)")
        outer = wx.StaticBoxSizer(gbx, wx.VERTICAL)

        # Row 1 — balance angle PID
        row1 = wx.BoxSizer(wx.HORIZONTAL)
        row1.Add(wx.StaticText(root, label="Balance (angle):"),
                 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 6)
        self._kp = gain(row1, "Kp:", KP_DEFAULT,
            "Proportional angle correction [torque/rad]. Too high → oscillation.")
        self._ki = gain(row1, "Ki:", KI_DEFAULT,
            "Integral: eliminates steady-state drift [torque/(rad·s)]. Too high → windup.")
        self._kd = gain(row1, "Kd:", KD_DEFAULT,
            "Derivative velocity damping [torque/(rad/s)]. Too high → sluggish.")
        self._adz = gain(row1, "Adz:", ADZ_DEFAULT,
            "Deadzone [deg] for pendulum angle error feedback.")
        row1.AddStretchSpacer()
        self._btn_bias = wx.Button(root, label="Bias", size=(90, -1))
        self._btn_bias.Bind(wx.EVT_BUTTON, self._on_bias)
        self._btn_bias.Disable()
        row1.Add(self._btn_bias, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        outer.Add(row1, 0, wx.EXPAND | wx.TOP | wx.BOTTOM, 3)

        # Row 2 — balance position PD (outer loop — still outputs θr in rad, unchanged)
        row2 = wx.BoxSizer(wx.HORIZONTAL)
        row2.Add(wx.StaticText(root, label="Balance (position):"),
                 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 6)
        self._kt = gain(row2, "Kt:", KT_DEFAULT,
            "Proportional arm position correction [rad/rad]. Feeds θr into inner PID.")
        self._kf = gain(row2, "Kf:", KF_DEFAULT,
            "Derivative arm position correction [rad/(rad/s)].")
        self._pdz = gain(row2, "Pdz:", PDZ_DEFAULT,
            "Deadzone [deg] for arm position error feedback.")
        self._tmax = gain(row2, "Tmax:", TMAX_DEFAULT,
            "Maximum tilt angle command [deg] for arm position correction.")
        row2.AddStretchSpacer()
        self._btn_swing = wx.Button(root, label="No Swing", size=(90, -1))
        self._btn_swing.Bind(wx.EVT_BUTTON, self._on_swing_toggle)
        self._btn_swing.Disable()
        row2.Add(self._btn_swing, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        outer.Add(row2, 0, wx.EXPAND | wx.TOP | wx.BOTTOM, 3)

        # Row 3 — swingup energy gains + torque limit + button
        row3 = wx.BoxSizer(wx.HORIZONTAL)
        row3.Add(wx.StaticText(root, label="Swingup:"),
                 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 6)
        self._ks = gain(row3, "Ks:", KS_DEFAULT,
            "Peak swingup torque [drive units]. Negate if pendulum damps instead of grows.")
        self._kb = gain(row3, "Kb:", KB_DEFAULT,
            "Peak braking torque [drive units]. Increase if pendulum overshoots upright.")
        self._kv = gain(row3, "Kv:", KV_DEFAULT,
            "Torque slew-rate limit [drive units/s]. Lower to reduce mechanical shock.")
        self._kda = gain(row3, "Kda:", KDA_DEFAULT,
            "Arm velocity damping during swingup [torque/(rad/s)]. "
            "Prevents runaway arm spin — increase if arm accelerates continuously in one direction.")
        self._torque_lim_swing = gain(row3, "Swing limit:", TORQUE_LIM_SWING_DEFAULT,
            "Hard torque clamp during swingup and braking [drive units]. "
            "Should be >= Ks and Kb or the energy pump will be clipped.")
        self._torque_lim_bal = gain(row3, "Balance limit:", TORQUE_LIM_BAL_DEFAULT,
            "Hard torque clamp during balance [drive units]. "
            "Lower than swing limit — balance needs far less torque than swingup.")
        row3.AddStretchSpacer()
        self._btn_ctrl = wx.Button(root, label="Start", size=(90, -1))
        self._btn_ctrl.Bind(wx.EVT_BUTTON, self._on_ctrl_toggle)
        self._btn_ctrl.Disable()
        row3.Add(self._btn_ctrl, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        outer.Add(row3, 0, wx.EXPAND | wx.TOP | wx.BOTTOM, 3)

        vsz.Add(outer, 0, wx.EXPAND | wx.ALL, 6)
        root.SetSizer(vsz)

    # ───────────────────────────────────────────────── helpers ──────────

    @staticmethod
    def _can_ports():
        if platform.system() != "Linux":
            return ["PCAN_USBBUS1", "PCAN_USBBUS2"]
        ports = sorted(os.path.basename(p) for p in glob.glob('/sys/class/net/can*'))
        return ports or ["can0"]

    def _set_status(self, msg, r=60, g=160, b=60):
        self._status.SetLabel(msg)
        self._status.SetForegroundColour(wx.Colour(r, g, b))

    # ───────────────────────────────────────────────── connection ───────

    @staticmethod
    def _bus_connect(net, port):
        if platform.system() == "Windows":
            net.connect(bustype='pcan', channel=port, bitrate=1_000_000)
        else:
            net.connect(bustype='socketcan', channel=port, bitrate=1_000_000)

    @classmethod
    def _scan_nodes(cls, port):
        """Node IDs answering on `port`.  Raises if the bus can't be opened."""
        net = canopen.Network()
        cls._bus_connect(net, port)
        try:
            net.scanner.reset()
            net.scanner.search()
            time.sleep(0.5)
            return list(net.scanner.nodes)
        finally:
            net.disconnect()

    def _on_scan(self, _):
        port = self._port.GetStringSelection()
        self._set_status("Scanning…", 180, 120, 0)
        self._btn_scan.Disable()
        wx.Yield()
        try:
            nodes = self._scan_nodes(port)
        except Exception as ex:
            self._set_status(f"Scan failed: {ex}", 180, 0, 0)
            self._btn_scan.Enable()
            return

        if not nodes:
            self._set_status("No nodes found on bus", 180, 0, 0)
            self._btn_scan.Enable()
            return

        choices = [str(n) for n in nodes]
        self._motor_choice.SetItems(choices)
        self._enc_choice.SetItems(choices)
        self._motor_choice.SetSelection(0)
        self._enc_choice.SetSelection(min(1, len(choices) - 1))
        self._motor_choice.Enable()
        self._enc_choice.Enable()
        self._btn_conn.Enable()
        self._btn_scan.Enable()
        self._set_status(
            f"Found {len(nodes)} node(s): {nodes}  —  assign Motor / Encoder then Connect",
            80, 140, 200)

    def _on_conn_toggle(self, _):
        if self._connected:
            self._disconnect()
        else:
            self._connect()

    def _open_bus(self, port, motor_id, enc_id):
        """Open the bus, attach both pucks, seed positions and start SYNC.
        Raises on failure, with the bus closed again so a retry starts clean."""
        net = canopen.Network()
        self._bus_connect(net, port)
        try:
            n1 = net.add_node(motor_id, EDS_FILE)
            n2 = net.add_node(enc_id,   EDS_FILE)
            n1.nmt.state = 'OPERATIONAL'
            n2.nmt.state = 'OPERATIONAL'
            n1.tpdo.read(); n1.rpdo.read(); n2.tpdo.read()

            if n1.rpdo[1].cob_id is None:
                n1.rpdo[1].cob_id = 0x200 + motor_id
            if n1.rpdo[2].cob_id is None:
                n1.rpdo[2].cob_id = 0x300 + motor_id

            n1.tpdo[1].add_callback(self._cb_p1_pos)
            n2.tpdo[1].add_callback(self._cb_p2_pos)

            self._network = net
            self._node1   = n1
            self._node2   = n2
            self._connected = True

            p1_now = n1.sdo["PositionFeedback"].raw
            p2_now = n2.sdo["PositionFeedback"].raw
            with self._lock:
                self._puck1_pos  = p1_now
                self._puck2_pos  = p2_now
                self._puck1_zero = p1_now
                self._puck2_zero = p2_now

            self._last_rx = [time.monotonic()] * 2
            net.sync.start(1.0 / SYNC_HZ)
        except Exception:
            self._network = self._node1 = self._node2 = None
            self._connected = False
            try:
                net.disconnect()
            except Exception:
                pass
            raise

    def _connect(self):
        port     = self._port.GetStringSelection()
        motor_id = int(self._motor_choice.GetStringSelection())
        enc_id   = int(self._enc_choice.GetStringSelection())
        self._set_status("Connecting…", 180, 120, 0)
        wx.Yield()
        try:
            self._open_bus(port, motor_id, enc_id)

            self._btn_scan.Disable()
            self._motor_choice.Disable()
            self._enc_choice.Disable()
            self._btn_conn.SetLabel("Disconnect")
            self._btn_en.Enable()
            self._btn_zero.Enable()
            self._btn_bias.Enable()
            self._btn_swing.Enable()
            self._set_status(
                f"Connected  (Motor={motor_id}, Encoder={enc_id})  —  waiting for pendulum to settle…",
                180, 120, 0)

            threading.Thread(target=self._auto_zero_thread, daemon=True).start()
            threading.Thread(target=self._temp_monitor_thread, daemon=True).start()

        except Exception as ex:
            self._set_status(f"Connect failed: {ex}", 180, 0, 0)

    def _disconnect(self):
        self._stop_control()
        self._disable_motor(quiet=True)
        if self._network:
            try:
                self._network.disconnect()
            except Exception:
                pass
        self._network = self._node1 = self._node2 = None
        self._connected = self._enabled = False
        self._btn_scan.Enable()
        self._motor_choice.Enable()
        self._enc_choice.Enable()
        self._btn_conn.SetLabel("Connect")
        self._btn_en.SetLabel("Enable Motor")
        self._btn_en.Disable()
        self._btn_zero.Disable()
        self._btn_bias.Disable()
        self._btn_swing.Disable()
        self._btn_ctrl.Disable()
        self._temp_label.SetLabel("Temp: --°C")
        self._temp_label.SetForegroundColour(wx.Colour(120, 130, 160))
        self._set_status("Disconnected", 160, 60, 60)

    # ───────────────────────────────────────────────── TPDO callbacks ───

    def _cb_p1_pos(self, _):
        try:
            with self._lock:
                self._puck1_pos = self._node1.tpdo[1]['PositionFeedback'].raw
            self._last_rx[0] = time.monotonic()
        except Exception:
            pass
        now = time.monotonic()
        if now - self._last_draw >= 1.0 / DISPLAY_HZ:
            self._last_draw = now
            wx.CallAfter(self._refresh_canvas)

    def _cb_p2_pos(self, _):
        try:
            with self._lock:
                self._puck2_pos = self._node2.tpdo[1]['PositionFeedback'].raw
            self._last_rx[1] = time.monotonic()
        except Exception:
            pass

    def _refresh_canvas(self):
        with self._lock:
            p1 = self._puck1_pos - self._puck1_zero
            p2 = self._puck2_pos - self._puck2_zero
        pend_rad = _wrap(p2 * 2.0 * math.pi / ENCODER_RES + math.pi)
        if not self._controlling:
            mode = "idle"
        elif self._in_balance:
            mode = "balance"
        elif self._in_braking:
            mode = "brake"
        else:
            if self._no_swing:
                mode = "rest"
            else:
                mode = "swing"
        self._canvas.update(pend_rad, p1, mode)

    # ───────────────────────────────────────────────── auto-zero ────────

    def _auto_zero_thread(self, timeout_s=15.0):
        """Zero the pendulum once it hangs still.  timeout_s=None waits forever."""
        WINDOW = 80
        THRESH = 12
        history = []
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        while self._connected and (deadline is None or time.monotonic() < deadline):
            with self._lock:
                pos = self._puck2_pos
            history.append(pos)
            if len(history) > WINDOW:
                history.pop(0)
            if len(history) == WINDOW and (max(history) - min(history)) <= THRESH:
                avg = sum(history) // len(history)
                with self._lock:
                    self._puck2_zero = avg
                wx.CallAfter(self._on_auto_zeroed)
                return
            time.sleep(1.0 / SYNC_HZ)
        if self._connected:
            wx.CallAfter(self._set_status,
                         "Pendulum didn't settle — click Zero when hanging at rest",
                         180, 120, 0)

    def _on_auto_zeroed(self):
        self._set_status("Auto-zeroed at rest  |  ready to enable")

    # ───────────────────────────────────────────────── temperature ───────

    def _temp_monitor_thread(self):
        while self._connected:
            try:
                with self._sdo_lock:
                    temp_c = self._node1.sdo['Amplifier']['Temperature'].raw
                if temp_c >= self.TEMP_SHUTDOWN_C:
                    wx.CallAfter(self._on_overtemp, temp_c)
                    return
                if temp_c >= 70:
                    colour = (220, 80, 40)
                elif temp_c >= 55:
                    colour = (200, 150, 40)
                else:
                    colour = (120, 130, 160)
                wx.CallAfter(self._update_temp_label, temp_c, colour)
            except Exception:
                pass
            time.sleep(2.0)

    def _update_temp_label(self, temp_c, colour):
        self._temp_label.SetLabel(f"Temp: {temp_c}°C")
        self._temp_label.SetForegroundColour(wx.Colour(*colour))

    def _on_overtemp(self, temp_c):
        self._update_temp_label(temp_c, (220, 40, 40))
        self._set_status(
            f"OVER-TEMPERATURE ({temp_c}°C ≥ {self.TEMP_SHUTDOWN_C}°C) — motor disabled",
            220, 40, 40)
        self._stop_control()
        self._disable_motor()

    # ───────────────────────────────────────────────── zero ─────────────

    def _on_zero(self, _):
        if not self._connected:
            return
        with self._lock:
            self._puck2_zero = self._puck2_pos
        self._set_status("Zeroed — downward rest position captured")

    # ───────────────────────────────────────────────── motor enable ─────

    def _on_enable_toggle(self, _):
        if self._enabled:
            self._stop_control()
            self._disable_motor()
        else:
            self._enable_motor()

    def _enable_drive(self):
        """Remap RPDO2 to TargetTorque, enable the drive in CST mode at zero
        torque.  Raises on failure; SYNC is running again either way."""
        n = self._node1
        with self._sdo_lock:
            self._network.sync.stop()
            time.sleep(0.04)
            try:
                self._enable_drive_locked(n)
            finally:
                self._network.sync.start(1.0 / SYNC_HZ)

    def _enable_drive_locked(self, n):
        cur = n.sdo["PositionFeedback"].raw   # still used to seed puck1_zero

        # ── Remap RPDO2 to TargetTorque (0x6071, INT16) ──────────────
        # puck4.eds only maps TargetPosition/TargetVelocity in RPDO2;
        # 0x6071 is absent from its PDO-mappable object list.  We remap
        # at the drive level via SDO.  DS402 requires PRE-OPERATIONAL.
        n.nmt.state = 'PRE-OPERATIONAL'
        time.sleep(0.05)
        # Back up only the ORIGINAL mapping: on a re-enable (e.g. fault
        # recovery) RPDO2 may already hold our TargetTorque mapping.
        if self._rpdo2_backup is None:
            orig_count = n.sdo[0x1601][0].raw
            backup = {0: orig_count}
            for i in range(1, orig_count + 1):
                backup[i] = n.sdo[0x1601][i].raw
            self._rpdo2_backup = backup
        n.sdo[0x1601][0].raw = 0            # disable mapping
        n.sdo[0x1601][1].raw = 0x60710010  # 0x6071 sub 0, INT16 (0x10 bits)
        n.sdo[0x1601][0].raw = 1            # re-enable with 1 object
        n.nmt.state = 'OPERATIONAL'
        time.sleep(0.05)
        # ─────────────────────────────────────────────────────────────

        n.sdo["ControlWord"].raw = CLEAR_FAULT;  time.sleep(0.05)
        n.sdo["ControlWord"].raw = SHUTDOWN;      time.sleep(0.05)
        n.sdo["ControlWord"].raw = OP_ENABLED

        # Switch to Cyclic Synchronous Torque mode
        n.rpdo[1]["SetModeOfOperation"].raw = MODE_CYCLIC_SYNC_TORQUE
        n.rpdo[1]["ControlWord"].raw = OP_ENABLED
        n.rpdo[1].transmit()

        # Zero torque.  Write 2-byte INT16 directly via send_message so
        # we bypass the EDS name lookup (0x6071 not in puck4.eds names).
        self._network.send_message(n.rpdo[2].cob_id, struct.pack('<h', 0))

        with self._lock:
            self._puck1_zero = cur

        self._enabled = True

    def _enable_motor(self):
        try:
            self._enable_drive()
            self._btn_en.SetLabel("Disable Motor")
            self._btn_ctrl.Enable()
            self._set_status("Motor enabled  |  zero torque — arm moves freely")
        except Exception as ex:
            traceback.print_exc()
            self._set_status(f"Enable failed ({type(ex).__name__}): {ex}", 180, 0, 0)

    def _disable_drive(self):
        """Disable the drive and restore RPDO2.  Best effort, never raises.
        Leaves SYNC stopped (as v4.1 did)."""
        with self._sdo_lock:
            self._disable_drive_locked()
        self._enabled = False

    def _disable_drive_locked(self):
        try:
            if self._node1:
                self._node1.rpdo[1]["SetModeOfOperation"].raw = MODE_IDLE
                self._node1.rpdo[1]["ControlWord"].raw = SHUTDOWN
                self._node1.rpdo[1].transmit()
            time.sleep(0.05)
            if self._network:
                self._network.sync.stop()
                time.sleep(0.04)
            if self._node1:
                self._node1.sdo["ControlWord"].raw = SHUTDOWN
            # Restore RPDO2 to its original mapping (TargetPosition / TargetVelocity)
            if self._node1 and self._rpdo2_backup:
                n1 = self._node1
                n1.nmt.state = 'PRE-OPERATIONAL'
                time.sleep(0.05)
                orig_count = self._rpdo2_backup[0]
                n1.sdo[0x1601][0].raw = 0
                for i in range(1, orig_count + 1):
                    n1.sdo[0x1601][i].raw = self._rpdo2_backup[i]
                n1.sdo[0x1601][0].raw = orig_count
                n1.nmt.state = 'OPERATIONAL'
                time.sleep(0.05)
                self._rpdo2_backup = None
        except Exception:
            pass

    def _disable_motor(self, quiet=False):
        self._disable_drive()
        if not quiet:
            self._btn_en.SetLabel("Enable Motor")
            self._btn_ctrl.Disable()
            self._set_status("Motor disabled", 150, 110, 0)

    # ───────────────────────────────────────────────── bias ─────────────

    def _on_bias(self, _):
        if not self._connected:
            return
        with self._lock:
            self._bi = _wrap(2 * math.pi * self._puck2_pos / ENCODER_RES + math.pi) * 180.0 / math.pi
        self._set_status("Biased — upward rest position captured")

    # ───────────────────────────────────────────────── toggle swing ─────

    def _on_swing_toggle(self, _):
        if self._no_swing:
            self._no_swing = False
            self._btn_swing.SetLabel("No Swing")
        else:
            self._no_swing = True
            self._btn_swing.SetLabel("Swing")

    # ───────────────────────────────────────────────── control loop ─────

    def _on_ctrl_toggle(self, _):
        if self._controlling:
            self._stop_control()
        else:
            self._start_control()

    def _read_gains(self):
        """Controller gains in _control_loop argument order.  Raises ValueError."""
        return (float(self._kp.GetValue()), float(self._ki.GetValue()),
                float(self._kd.GetValue()), float(self._adz.GetValue()),
                float(self._bi),
                float(self._kt.GetValue()), float(self._kf.GetValue()),
                float(self._pdz.GetValue()), float(self._tmax.GetValue()),
                float(self._ks.GetValue()), float(self._kb.GetValue()),
                float(self._kv.GetValue()), float(self._kda.GetValue()),
                float(self._torque_lim_swing.GetValue()),
                float(self._torque_lim_bal.GetValue()))

    def _launch_control(self, gains):
        """Start _control_loop on its own thread with the given gains."""
        self._in_balance      = False
        self._ramping_balance = False
        self._in_braking      = False
        self._controlling     = True
        self._ctrl_thread = threading.Thread(
            target=self._control_loop, args=gains, daemon=True)
        self._ctrl_thread.start()

    def _start_control(self):
        try:
            gains = self._read_gains()
        except ValueError:
            self._set_status("Invalid gain — use numeric values", 180, 0, 0)
            return

        self._btn_bias.Disable()
        self._btn_swing.Disable()
        self._btn_ctrl.SetLabel("Stop")
        for tc in (self._kp, self._ki, self._kd, self._adz,
                   self._kt, self._kf, self._pdz, self._tmax,
                   self._ks, self._kb, self._kv, self._kda,
                   self._torque_lim_swing, self._torque_lim_bal):
            tc.Disable()
        if self._no_swing:
            self._set_status("Resting…", 200, 130, 0)
        else:
            self._set_status("Swingup…", 200, 130, 0)

        self._launch_control(gains)

    def _stop_control(self):
        self._controlling = False
        if self._ctrl_thread:
            self._ctrl_thread.join(timeout=1.5)
            self._ctrl_thread = None
        wx.CallAfter(self._on_ctrl_stopped)

    def _on_ctrl_stopped(self):
        self._in_balance      = False
        self._ramping_balance = False
        self._in_braking      = False
        self._btn_bias.Enable()
        self._btn_swing.Enable()
        self._btn_ctrl.SetLabel("Start")
        for tc in (self._kp, self._ki, self._kd, self._adz,
                   self._kt, self._kf, self._pdz, self._tmax,
                   self._ks, self._kb, self._kv, self._kda,
                   self._torque_lim_swing, self._torque_lim_bal):
            tc.Enable()
        if self._enabled:
            try:
                # Zero torque on stop — arm coasts freely
                self._network.send_message(
                    self._node1.rpdo[2].cob_id, struct.pack('<h', 0))
            except Exception:
                pass
            self._set_status("Motor enabled  |  zero torque — arm moves freely")

    def _control_loop(self, kp, ki, kd, adz, bi, kt, kf, pdz, tmax, ks, kb, kv, kda,
                      torque_lim_swing, torque_lim_bal,
                      soft_start_s=0.0, soft_start_from=1.0, arm_vel_max=0.0):
        # soft_start_s / soft_start_from / arm_vel_max: optional swing-up guards
        # (the kiosk uses them; 0 / 1.0 / 0 = off = v4.1 behaviour):
        #   soft start  -- swing torque scaled from soft_start_from up to 1.0
        #                  over the first soft_start_s seconds of a run, so the
        #                  first swing-up from rest builds energy gradually
        #   arm_vel_max -- [rad/s] swing torque that would speed the arm up is
        #                  faded to zero as |arm_vel| approaches this (it may
        #                  always slow the arm) -- no multi-revolution runaway
        # ── Parameter reference ───────────────────────────────────────────
        #
        # BALANCE  (|θ| < BALANCE_ENTRY, |ω| < BALANCE_VEL_MAX)
        #
        #   Outer loop — arm position → reference lean angle θr  (unchanged):
        #   θr = -(Kt·p_dz + Kf·ω_arm)            [rad]
        #   Kt  [rad/rad]     position correction  — too high → arm oscillation
        #   Kf  [rad/(rad/s)] velocity damping     — too high → sluggish return
        #   Pdz [deg]         arm position deadzone
        #   Tmax[deg]         θr magnitude cap
        #
        #   Inner loop — torque PID on (θ − θr)  (replaces CSP position cmd):
        #   T  = balance_ramp · (Kp·(θ−θr) + Ki·∫(θ−θr)dt + Kd·ω_fast)
        #   Kp  [torque/rad]      angle stiffness  — too high → oscillation
        #   Ki  [torque/(rad·s)]  integral drift   — too high → windup
        #   Kd  [torque/(rad/s)]  velocity damping — too high → sluggish
        #   Integral resets to zero on every balance exit.
        #   Adz [deg] pendulum angle deadzone
        #
        # SWINGUP / BRAKING  (energy controller)
        #   de = ½ω² + ½κ²·ω_arm²·sin²θ − (g/L)(1−cosθ)
        #   pump = sign(de · ω · cos θ)
        #   fraction = min(1, |de| / OMEGA_N_SQ)
        #   T = pump · fraction · (Ks if de<0 else Kb) − Kda · ω_arm
        #   Ks  [torque]          peak swingup torque
        #   Kb  [torque]          peak braking torque
        #   Kv  [torque/s]        torque slew-rate limit
        #   Kda [torque/(rad/s)]  arm velocity damping — prevents arm runaway
        #                         (replaces the implicit damping the Puck's
        #                          internal servo provided in CSP mode)
        #
        # RESTING  (no-swing mode, or |ω| too low to pump)
        #   T = -Kda · ω_arm   — passive arm damping only
        #
        # TRAVEL GUARD  (all modes)
        #   If |arm| > MAX_ARM_CTS, clamp torque so it cannot drive further
        #   into the stop (allows only the returning direction).
        # ─────────────────────────────────────────────────────────────────

        dt_target  = 1.0 / SYNC_HZ

        # Pre-compute torque slew per 500 Hz cycle
        slew_per_cycle = kv / SYNC_HZ         # [torque units / cycle]

        adz_rad  = math.radians(adz)
        bi_rad   = math.radians(bi)
        pdz_rad  = math.radians(pdz)
        tmax_rad = math.radians(tmax)

        # Clamp torque limits to positive values
        torque_lim_swing = max(0.0, torque_lim_swing)
        torque_lim_bal   = max(0.0, torque_lim_bal)

        # LP filter coefficients
        # Fixed: original code had min(1,0,...) which always returned 0.
        a_very_slow = min(1.0, 2 * math.pi * 0.16 / SYNC_HZ)  # ~0.16 Hz position ref drift
        a_slow      = min(1.0, 2 * math.pi * 3.0  / SYNC_HZ)  # ~3 Hz  energy direction
        a_fast      = min(1.0, 2 * math.pi * 8.0  / SYNC_HZ)  # ~8 Hz  PD velocity

        in_balance      = False
        balance_ramp    = 0.0
        prev_pend       = 0.0
        vel_slow        = 0.0
        vel_fast        = 0.0
        prev_arm_rad    = 0.0
        arm_vel         = 0.0
        integral        = 0.0
        prev_torque     = 0.0
        arm_rad_target  = 0.0   # initialised properly when balance is entered
        arm_rad_target_z = 0.0

        prev_t = time.monotonic()
        run_t0 = prev_t
        self._run_peak_arm_vel = 0.0      # run stats (the kiosk logs them)
        self._run_first_balance_s = None

        while self._controlling and self._enabled:
            t0 = time.monotonic()
            dt = t0 - prev_t;  prev_t = t0
            if dt <= 0:
                dt = dt_target

            with self._lock:
                p1_abs  = self._puck1_pos
                p1_zero = self._puck1_zero
                p1      = p1_abs - p1_zero
                p2      = self._puck2_pos - self._puck2_zero

            pend_rad = _wrap(p2 * 2.0 * math.pi / ENCODER_RES + math.pi) - bi_rad
            arm_rad  = p1 * 2.0 * math.pi / ENCODER_RES

            # Pendulum velocity (wrap delta to avoid ±π spike)
            delta = pend_rad - prev_pend
            if delta >  math.pi: delta -= 2 * math.pi
            elif delta < -math.pi: delta += 2 * math.pi
            raw_vel  = delta / dt
            vel_slow = a_slow * raw_vel + (1.0 - a_slow) * vel_slow
            vel_fast = a_fast * raw_vel + (1.0 - a_fast) * vel_fast
            prev_pend = pend_rad

            # Arm angular velocity (continuous — no wrapping needed)
            raw_arm_vel = (arm_rad - prev_arm_rad) / dt
            arm_vel     = a_slow * raw_arm_vel + (1.0 - a_slow) * arm_vel
            prev_arm_rad = arm_rad

            # ── mode transitions ─────────────────────────────────────────
            if in_balance:
                if abs(pend_rad) > BALANCE_EXIT or abs(vel_fast) > BALANCE_VEL_MAX:
                    in_balance = False
                    integral   = 0.0
                    if self._no_swing:
                        wx.CallAfter(self._set_status, "Resting…", 200, 130, 0)
                    else:
                        wx.CallAfter(self._set_status, "Swingup…", 200, 130, 0)
            else:
                if abs(pend_rad) < BALANCE_ENTRY and abs(vel_fast) < BALANCE_VEL_MAX:
                    in_balance       = True
                    arm_rad_target   = arm_rad   # hold current arm position as reference
                    arm_rad_target_z = arm_rad
                    wx.CallAfter(self._set_status, "Balancing…", 0, 110, 185)
            self._in_balance = in_balance

            # ── compute torque command ────────────────────────────────────
            torque = 0.0

            if in_balance:

                # Ramp up balance feedback over 1 s to avoid an entry jolt
                balance_ramp = min(1.0, balance_ramp + dt * BALANCE_RAMP_ON_RATE)

                # Outer loop: very slowly drift the arm position reference toward
                # the arm's natural rest point (fixes the min(1,0,...) bug from CSP)
                arm_rad_target_z = (1.0 - a_very_slow) * arm_rad_target_z
                arm_rad_target   = (a_very_slow  * arm_rad_target_z
                                    + (1.0 - a_very_slow) * arm_rad_target)

                # Arm position error with deadzone
                arm_rad_dz = arm_rad - arm_rad_target
                if arm_rad_dz > pdz_rad:
                    arm_rad_dz -= pdz_rad
                elif arm_rad_dz < -pdz_rad:
                    arm_rad_dz += pdz_rad
                else:
                    arm_rad_dz = 0.0

                # Outer PD → reference tilt angle θr
                tilt_angle_rad = kt * arm_rad_dz + kf * arm_vel
                tilt_angle_rad = max(-tmax_rad, min(tmax_rad, tilt_angle_rad))

                # Pendulum angle error = θ − θr, with angle deadzone
                pend_rad_dz = pend_rad + tilt_angle_rad   # +tilt because θr = −tilt
                if pend_rad_dz > adz_rad:
                    pend_rad_dz -= adz_rad
                elif pend_rad_dz < -adz_rad:
                    pend_rad_dz += adz_rad
                else:
                    pend_rad_dz = 0.0

                # Inner PID → torque  (replaces CSP: p1_abs + u)
                integral = max(-INTEGRAL_CLAMP,
                               min(INTEGRAL_CLAMP, integral + pend_rad_dz * dt))
                torque   = balance_ramp * (kp * pend_rad_dz
                                           + ki * integral
                                           + kd * vel_fast)
                torque   = max(-torque_lim_bal, min(torque_lim_bal, torque))

                self._in_braking = False

            elif not self._no_swing and abs(vel_slow) > 0.1:  # Swinging

                # Let balance ramp decay so re-entry starts smoothly
                balance_ramp = max(0.0, balance_ramp - dt * BALANCE_RAMP_OFF_RATE)

                # Proportional energy controller with Furuta centripetal correction
                de = (0.5 * vel_slow ** 2
                      + 0.5 * COUPLING ** 2 * arm_vel ** 2 * math.sin(pend_rad) ** 2
                      - OMEGA_N_SQ * (1.0 - math.cos(pend_rad)))
                pump     = math.copysign(1.0, de * vel_slow * math.cos(pend_rad))
                braking  = de > 0
                cap      = kb if braking else ks
                fraction = min(1.0, abs(de) / OMEGA_N_SQ)
                # Arm velocity damping subtracted from pump torque — this is
                # what prevents the arm from accelerating indefinitely in one
                # direction.  In CSP mode the Puck's internal servo provided
                # this implicitly; in CST it must be explicit.
                raw_t    = pump * cap * fraction - kda * arm_vel

                # Torque slew-rate limit (smooths direction reversals)
                step   = max(-slew_per_cycle, min(slew_per_cycle, raw_t - prev_torque))
                torque = prev_torque + step
                torque = max(-torque_lim_swing, min(torque_lim_swing, torque))

                # Optional swing-up guards (off unless the caller asks)
                if soft_start_s > 0:
                    ramp = min(1.0, soft_start_from
                               + (1.0 - soft_start_from) * (t0 - run_t0) / soft_start_s)
                    torque *= ramp
                if arm_vel_max > 0 and torque * arm_vel > 0:
                    torque *= max(0.0, 1.0 - abs(arm_vel) / arm_vel_max)

                self._in_braking = braking

            else:  # Resting — no swing mode, or pendulum barely moving

                balance_ramp = max(0.0, balance_ramp - dt * BALANCE_RAMP_OFF_RATE)
                # Apply passive arm damping even at rest so the arm doesn't
                # coast freely after a balance exit.
                torque       = max(-torque_lim_swing, min(torque_lim_swing, -kda * arm_vel))
                self._in_braking = False

            # Hard travel guard — never command torque that drives the arm
            # further into the end-stop; only allow the returning direction.
            if p1 > MAX_ARM_CTS:
                torque = min(0.0, torque)
            elif p1 < -MAX_ARM_CTS:
                torque = max(0.0, torque)

            prev_torque = torque
            self._run_peak_arm_vel = max(self._run_peak_arm_vel, abs(arm_vel))
            if in_balance and self._run_first_balance_s is None:
                self._run_first_balance_s = t0 - run_t0

            try:
                # INT16 torque sent as a 2-byte CAN frame directly, bypassing
                # the EDS name lookup (0x6071 absent from puck4.eds names).
                self._network.send_message(
                    self._node1.rpdo[2].cob_id,
                    struct.pack('<h', max(-32768, min(32767, int(torque)))))
            except Exception:
                break

            elapsed = time.monotonic() - t0
            if elapsed < dt_target:
                time.sleep(dt_target - elapsed)

    # ───────────────────────────────────────────────── close ────────────

    def _on_close(self, _):
        self._disconnect()
        self.Destroy()


# ──────────────────────────────────────────────────────── helpers ──────────

def _wrap(angle):
    return (angle + math.pi) % (2 * math.pi) - math.pi


# ──────────────────────────────────────────────────────── entry point ─────

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Furuta pendulum controller (CST)")
    ap.add_argument('--touchscreen', action='store_true',
                    help='full-screen customer kiosk for the Raspberry Pi touch display')
    ap.add_argument('--auto-stop', type=float, default=60, metavar='SECONDS',
                    help='--touchscreen only: stop each run after SECONDS and go back '
                         'to START (default 60; 0 = never)')
    args = ap.parse_args()
    if args.touchscreen:
        import furuta_kiosk
        furuta_kiosk.main(args.auto_stop)
    else:
        app = wx.App()
        FurutaPIDFrame().Show()
        app.MainLoop()
