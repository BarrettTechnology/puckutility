#!/usr/bin/env python3
"""
Furuta Pendulum — Energy Swingup + PID Balance Controller
<<<<<<< HEAD
  Puck 1 (node 1) — rotating arm motor driven via PVCA (Phase-Voltage/Commutation-Angle)
=======
  Puck 1 (node 1) — rotating arm motor driven via Cyclic-Sync Position mode
>>>>>>> feature/cm-control-updates
  Puck 2 (node 2) — passive pendulum encoder (read-only)

Three automatic modes:
  SWINGUP  — energy-based pump builds pendulum amplitude from rest
  BRAKING  — energy-based damping absorbs excess energy near upright
  BALANCE  — PID holds upright once within ±15°

<<<<<<< HEAD
  Kp  [mNm/rad]       Balance: proportional angle correction.
  Ki  [mNm/(rad·s)]   Balance: integral, eliminates steady-state drift.
  Kd  [mNm/(rad/s)]   Balance: derivative velocity damping.
  Ks  [mNm]           Swingup: peak torque amplitude.
  Kb  [mNm]           Braking: peak torque amplitude.
  Kv  [mNm/cycle]     Swingup/braking torque slew-rate limit.

Angle convention: θ = 0 upright, ±π hanging down (pendulum, P2).

PVCA control path (per SYNC):
  TPDO1 fires → _cb_p1_pos reads actual position → applies encoder correction
  table → computes electrical angle (theta_e) + voltage amplitude (ud) →
  fire-and-forget SDO writes.  The control loop runs in a separate thread and
  publishes _torque_cmd [mNm]; the TPDO callback reads it GIL-safely each cycle.
=======
  Kp  [counts/rad]       Balance: proportional angle correction.
  Ki  [counts/(rad·s)]   Balance: integral, eliminates steady-state drift.
  Kd  [counts/(rad/s)]   Balance: derivative velocity damping.
  Ks  [revolutions]      Swingup: max arm travel per cycle.
  Kb  [revolutions]      Braking: max arm travel per cycle.
  Kv  [rev/s]            Swingup/braking slew-rate limit.

Angle convention: θ = 0 upright, ±π hanging down (pendulum, P2).
>>>>>>> feature/cm-control-updates
"""

import wx
import canopen
import platform
<<<<<<< HEAD
import struct
=======
>>>>>>> feature/cm-control-updates
import time
import math
import threading
import traceback
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from canopen_runner import (
    CLEAR_FAULT, SHUTDOWN, OP_ENABLED,
<<<<<<< HEAD
    MODE_IDLE, MODE_PHASE_VOLTAGE_ANGLE,
=======
    MODE_IDLE, MODE_CYCLIC_SYNC_POS
>>>>>>> feature/cm-control-updates
)

ENCODER_RES     = 4096
EDS_FILE        = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'puck4.eds')
SYNC_HZ         = 500
BALANCE_ENTRY   = math.radians(15)   # engage PID inside ±15°
BALANCE_EXIT    = math.radians(25)   # disengage outside ±25°
<<<<<<< HEAD
BALANCE_VEL_MAX = 3.0                # rad/s — max velocity to engage
INTEGRAL_CLAMP  = 300.0              # rad·s — anti-windup clamp on integral state
=======
BALANCE_VEL_MAX = 10#3.0                # rad/s — max velocity to engage
INTEGRAL_CLAMP  = 2048               # counts — anti-windup clamp on integral
>>>>>>> feature/cm-control-updates
PEND_LENGTH_M   = 0.3048             # pendulum rod length (m) — 12 inches
ARM_LENGTH_M    = 0.127              # rotating arm length (m) — 5 inches
COUPLING        = ARM_LENGTH_M / PEND_LENGTH_M   # κ = L₁/L₂
OMEGA_N_SQ      = 9.8 / PEND_LENGTH_M            # g/L₂ (rad/s)²
<<<<<<< HEAD
MAX_ARM_REV     = 3.0                # soft travel guard (revolutions)
MAX_ARM_CTS     = int(MAX_ARM_REV * ENCODER_RES)
DISPLAY_HZ      = 25
=======
MAX_ARM_REV     = 5.0                # soft travel guard (revolutions)
MAX_ARM_CTS     = int(MAX_ARM_REV * ENCODER_RES)
DISPLAY_HZ      = 25
BALANCE_RAMP_ON_RATE = 1.0
BALANCE_RAMP_OFF_RATE = 1.0

# Tunable parameter defaults
KP_DEFAULT   = 8000
KI_DEFAULT   = 0
KD_DEFAULT   = 55 # 30–55
ADZ_DEFAULT  = 1
BI_DEFAULT   = 0
KT_DEFAULT   = 0.005 # 0.005–0.01 
KF_DEFAULT   = 0.009
PDZ_DEFAULT  = 150 # 0–150
TMAX_DEFAULT = 4
KS_DEFAULT   = 0.3
KB_DEFAULT   = 0.3
KV_DEFAULT   = 5.0
ERROR_LIMIT_DEFAULT = 0.5
>>>>>>> feature/cm-control-updates


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

    def __init__(self, parent, **kw):
        super().__init__(parent, **kw)
<<<<<<< HEAD
        self._pend_rad  = 0.0
        self._arm_cts   = 0
        self._mode      = "idle"
        self._motor_id  = 1
        self._enc_label = "2"
=======
        self._pend_rad = 0.0
        self._arm_cts  = 0
        self._mode     = "idle"
>>>>>>> feature/cm-control-updates
        self.SetBackgroundColour(self.BG)
        self.SetMinSize((-1, 290))
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_SIZE, lambda _: self.Refresh())

<<<<<<< HEAD
    def update(self, pend_rad, arm_cts, mode, motor_id=1, enc_label="2"):
        self._pend_rad  = pend_rad
        self._arm_cts   = arm_cts
        self._mode      = mode
        self._motor_id  = motor_id
        self._enc_label = enc_label
=======
    def update(self, pend_rad, arm_cts, mode):
        self._pend_rad = pend_rad
        self._arm_cts  = arm_cts
        self._mode     = mode
>>>>>>> feature/cm-control-updates
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
        frac = max(-1.0, min(1.0, self._arm_cts / MAX_ARM_CTS))
        cx   = int(tx0 + tw * (0.5 + 0.5 * frac))
        cw, ch = 46, 22

        dc.SetBrush(wx.Brush(self.CART))
        dc.SetPen(wx.Pen(self.CART_EDGE, 2))
        dc.DrawRoundedRectangle(cx - cw // 2, ty, cw, ch, 5)

        dc.SetBrush(wx.Brush(self.WHEEL))
        dc.SetPen(wx.Pen(wx.Colour(85, 90, 115), 1))
        for wx_ in (cx - 14, cx + 14):
            dc.DrawCircle(wx_, ty + ch + 4, 6)

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

        dc.SetPen(wx.Pen(arm_col, 7))
        dc.DrawLine(px, py, ex, ey)

        dc.SetBrush(wx.Brush(arm_col))
        dc.SetPen(wx.Pen(wx.Colour(240, 240, 240), 1))
        dc.DrawCircle(ex, ey, 11)

        dc.SetBrush(wx.Brush(self.PIVOT))
        dc.DrawCircle(px, py, 5)

        dc.SetPen(wx.Pen(wx.Colour(80, 180, 80, 90), 1, wx.PENSTYLE_DOT))
        dc.DrawLine(px, py, px, py - arm_len)

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
            "idle":    (wx.Colour(140, 140, 160), "[ IDLE      ]"),
        }
        col, mode_str = mode_colours.get(self._mode, mode_colours["idle"])
<<<<<<< HEAD
        enc_tag = f"FAKE" if self._enc_label == "FAKE" else f"N{self._enc_label}"
        dc.DrawText(f"Pendulum ({enc_tag}):  {ang_deg:+7.2f}°", 8, 6)
        dc.DrawText(f"Arm      (N{self._motor_id}):  {arm_deg:+7.2f}°", 8, 22)
=======
        dc.DrawText(f"Pendulum (P2): {ang_deg:+7.2f}°", 8, 6)
        dc.DrawText(f"Arm      (P1): {arm_deg:+7.2f}°", 8, 22)
>>>>>>> feature/cm-control-updates
        dc.SetTextForeground(col)
        dc.DrawText(mode_str, W - 120, 6)


# ──────────────────────────────────────────────────────── main frame ──────

class FurutaPIDFrame(wx.Frame):

    TEMP_SHUTDOWN_C = 85

    def __init__(self):
        super().__init__(None, title="Furuta Pendulum Controller", size=(720, 520))

        self._network = None
        self._node1   = None
        self._node2   = None
        self._lock    = threading.Lock()

        self._puck1_pos  = 0
        self._puck2_pos  = 0
        self._puck1_zero = 0
        self._puck2_zero = 0

<<<<<<< HEAD
        self._last_draw        = 0.0
=======
        self._last_draw   = 0.0
>>>>>>> feature/cm-control-updates
        self._connected        = False
        self._enabled          = False
        self._controlling      = False
        self._in_balance       = False
<<<<<<< HEAD
        self._in_braking       = False
        self._ctrl_thread      = None
        self._torque_limit_pct = 50.0
        self._fake_enc         = False
        self._motor_id         = 1
        self._enc_label        = "2"
        self._puck1_target     = 0   # last arm position exposed to fake physics thread

        # PVCA control state
        self._pvca_table    = None   # encoder correction table (list[int]) or None
        self._pvca_mp       = {}     # motor parameters (Kt, Rt, V_bus, etc.)
        self._pvca_sdo_cob  = 0      # SDO COB-ID for fire-and-forget writes
        self._torque_cmd    = 0.0    # torque command [mNm]; written by control loop,
                                     # read by _cb_p1_pos on every SYNC (GIL-safe float)
=======
        self._ramping_balnace  = False
        self._in_braking       = False
        self._ctrl_thread      = None
        # self._debug_val = 0.0
>>>>>>> feature/cm-control-updates

        self._build_ui()
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.SetMinSize((580, 460))
        self.Centre()

    # ───────────────────────────────────────────────── UI ───────────────

    def _build_ui(self):
        root = wx.Panel(self)
        root.SetBackgroundColour(wx.Colour(235, 238, 248))
        vsz = wx.BoxSizer(wx.VERTICAL)

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

<<<<<<< HEAD
        # Status + temperature row
        ssz = wx.BoxSizer(wx.HORIZONTAL)
        self._status = wx.StaticText(root, label="Disconnected")
        self._status.SetForegroundColour(wx.Colour(160, 60, 60))
        f = self._status.GetFont(); f.MakeBold(); self._status.SetFont(f)
        ssz.Add(self._status, 1, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 8)
        self._temp_label = wx.StaticText(root, label="Temp: --°C")
        self._temp_label.SetForegroundColour(wx.Colour(120, 130, 160))
        ssz.Add(self._temp_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
=======
        # Status + debug + temperature row
        ssz = wx.BoxSizer(wx.HORIZONTAL)
        self._status = wx.StaticText(root, label="Disconnected", style=wx.ALIGN_CENTER_HORIZONTAL)
        self._status.SetForegroundColour(wx.Colour(160, 60, 60))
        f = self._status.GetFont(); f.MakeBold(); self._status.SetFont(f)
        ssz.Add(self._status, 1, wx.EXPAND | wx.ALL, 8)
        # self._debug_label = wx.StaticText(root, label="Debug: --", style=wx.ALIGN_CENTER_HORIZONTAL)
        # self._debug_label.SetForegroundColour(wx.Colour(120, 130, 160))
        # ssz.Add(self._debug_label, 1, wx.EXPAND | wx.ALL, 10)
        self._temp_label = wx.StaticText(root, label="Temp: --°C", style=wx.ALIGN_CENTER_HORIZONTAL)
        self._temp_label.SetForegroundColour(wx.Colour(120, 130, 160))
        ssz.Add(self._temp_label, 1, wx.EXPAND | wx.ALL, 10)
>>>>>>> feature/cm-control-updates
        vsz.Add(ssz, 0, wx.EXPAND | wx.BOTTOM, 8)

        # Canvas
        self._canvas = FurutaCanvas(root)
        vsz.Add(self._canvas, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 6)

<<<<<<< HEAD
        # Gains — two rows inside one static box
        gbx = wx.StaticBox(root, label="Controller Gains  (PVCA mode)")
=======
        # Gains — three rows inside one static box
        gbx = wx.StaticBox(root, label="Controller Gains  (CSP mode)")
>>>>>>> feature/cm-control-updates
        outer = wx.StaticBoxSizer(gbx, wx.VERTICAL)

        def gain(sizer, label, default, tip):
            sizer.Add(wx.StaticText(root, label=label),
                      0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 8)
            tc = wx.TextCtrl(root, value=str(default), size=(62, -1))
            tc.SetToolTip(tip)
            sizer.Add(tc, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT | wx.RIGHT, 3)
            return tc

<<<<<<< HEAD
        # Row 1 — balance PID
        row1 = wx.BoxSizer(wx.HORIZONTAL)
        row1.Add(wx.StaticText(root, label="Balance:"),
                 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 6)
        self._kp = gain(row1, "Kp:", 150,
            "Proportional angle correction [mNm/rad]. Too high → oscillation.")
        self._ki = gain(row1, "Ki:", 2,
            "Integral: eliminates steady-state drift [mNm/(rad·s)]. Too high → windup.")
        self._kd = gain(row1, "Kd:", 6,
            "Derivative velocity damping [mNm/(rad/s)]. Too high → sluggish.")
        outer.Add(row1, 0, wx.EXPAND | wx.TOP | wx.BOTTOM, 3)

        # Row 2 — swingup energy gains + torque limit + button
        row2 = wx.BoxSizer(wx.HORIZONTAL)
        row2.Add(wx.StaticText(root, label="Swingup:"),
                 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 6)
        self._ks = gain(row2, "Ks:", 200,
            "Swingup peak torque [mNm]. Negate if pendulum damps instead of grows.")
        self._kb = gain(row2, "Kb:", 400,
            "Braking peak torque [mNm]. Increase if pendulum overshoots upright.")
        self._kv = gain(row2, "Kv:", 5,
            "Torque slew-rate limit [mNm/cycle] for swingup and braking.")
        row2.Add(wx.StaticText(root, label="Max torque:"),
                 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 12)
        self._torque_limit = wx.TextCtrl(root, value="50", size=(46, -1))
        self._torque_limit.SetToolTip(
            "Hard torque cap as % of i_peak × Kt.\n"
            "Applied in _pvca_compute — clamps iq below i_peak regardless of gain output.\n"
            "50% = continuous-safe for most motors.")
        row2.Add(self._torque_limit, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 3)
        row2.Add(wx.StaticText(root, label="%"),
                 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 2)
        self._torque_label = wx.StaticText(root, label="")
        self._torque_label.SetForegroundColour(wx.Colour(100, 170, 100))
        row2.Add(self._torque_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 6)
        row2.AddStretchSpacer()
        self._btn_ctrl = wx.Button(root, label="Start", size=(90, -1))
        self._btn_ctrl.Bind(wx.EVT_BUTTON, self._on_ctrl_toggle)
        self._btn_ctrl.Disable()
        row2.Add(self._btn_ctrl, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        outer.Add(row2, 0, wx.EXPAND | wx.TOP | wx.BOTTOM, 3)
=======
        # Row 1 — balance angle PID
        row1 = wx.BoxSizer(wx.HORIZONTAL)
        row1.Add(wx.StaticText(root, label="Balance (angle):"),
                 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 6)
        self._kp = gain(row1, "Kp:", KP_DEFAULT,
            "Proportional angle correction [counts/rad]. Too high → oscillation.")
        self._ki = gain(row1, "Ki:", KI_DEFAULT,
            "Integral: eliminates steady-state drift [counts/(rad·s)]. Too high → windup.")
        self._kd = gain(row1, "Kd:", KD_DEFAULT,
            "Derivative velocity damping [counts/(rad/s)]. Too high → sluggish.")
        self._adz = gain(row1, "Adz:", ADZ_DEFAULT,
            "Deadzone [deg] for pendulum angle error feedback.")
        self._bi = gain(row1, "Bias:", BI_DEFAULT,
            "Bias [deg] for pendulum angle error feedback.")
        outer.Add(row1, 0, wx.EXPAND | wx.TOP | wx.BOTTOM, 3)

        # Row 2 - balance position PD
        row2 = wx.BoxSizer(wx.HORIZONTAL)
        row2.Add(wx.StaticText(root, label="Balance (position):"),
                 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 6)
        self._kt = gain(row2, "Kt:", KT_DEFAULT,
            "Proportional arm position correction [rad/rad].")
        self._kf = gain(row2, "Kf:", KF_DEFAULT,
            "Derivative arm position correction [rad/(rad/s)].")
        self._pdz = gain(row2, "Pdz:", PDZ_DEFAULT,
            "Deadzone [deg] for arm position error feedback.")
        self._tmax = gain(row2, "Tmax:", TMAX_DEFAULT,
            "Maximum tilt angle command [deg] for arm position correction.")
        outer.Add(row2, 0, wx.EXPAND | wx.TOP | wx.BOTTOM, 3)

        # Row 3 — swingup energy gains + torque limit + button
        row3 = wx.BoxSizer(wx.HORIZONTAL)
        row3.Add(wx.StaticText(root, label="Swingup:"),
                 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 6)
        self._ks = gain(row3, "Ks:", KS_DEFAULT,
            "Swingup max arm travel [revolutions]. Negate if pendulum damps instead of grows.")
        self._kb = gain(row3, "Kb:", KB_DEFAULT,
            "Braking max arm travel [revolutions]. Increase if pendulum overshoots upright.")
        self._kv = gain(row3, "Kv:", KV_DEFAULT,
            "Max arm velocity [rev/s] for swingup and braking (slew-rate limit).")
        self._el = gain(row3, "Error limit:", ERROR_LIMIT_DEFAULT,
            "Limit on position error (revolutions). Lower to protect against overheating.")
        row3.AddStretchSpacer()
        self._btn_ctrl = wx.Button(root, label="Start", size=(90, -1))
        self._btn_ctrl.Bind(wx.EVT_BUTTON, self._on_ctrl_toggle)
        self._btn_ctrl.Disable()
        row3.Add(self._btn_ctrl, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        outer.Add(row3, 0, wx.EXPAND | wx.TOP | wx.BOTTOM, 3)
>>>>>>> feature/cm-control-updates

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

    def _on_scan(self, _):
        port = self._port.GetStringSelection()
        self._set_status("Scanning…", 180, 120, 0)
        self._btn_scan.Disable()
        wx.Yield()
        try:
            net = canopen.Network()
            if platform.system() == "Windows":
                net.connect(bustype='pcan', channel=port, bitrate=1_000_000)
            else:
                net.connect(bustype='socketcan', channel=port, bitrate=1_000_000)
            net.scanner.reset()
            net.scanner.search()
            time.sleep(0.5)
            nodes = list(net.scanner.nodes)
            net.disconnect()
        except Exception as ex:
            self._set_status(f"Scan failed: {ex}", 180, 0, 0)
            self._btn_scan.Enable()
            return

        if not nodes:
            self._set_status("No nodes found on bus", 180, 0, 0)
            self._btn_scan.Enable()
            return

        choices = [str(n) for n in nodes]
<<<<<<< HEAD
        enc_choices = choices + ["FAKE"]
        self._motor_choice.SetItems(choices)
        self._enc_choice.SetItems(enc_choices)
        self._motor_choice.SetSelection(0)
        # Auto-select FAKE when only one real node is found (can't use same node for both)
        self._enc_choice.SetSelection(1 if len(choices) >= 2 else len(enc_choices) - 1)
=======
        self._motor_choice.SetItems(choices)
        self._enc_choice.SetItems(choices)
        self._motor_choice.SetSelection(0)
        self._enc_choice.SetSelection(min(1, len(choices) - 1))
>>>>>>> feature/cm-control-updates
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

    def _connect(self):
<<<<<<< HEAD
        port      = self._port.GetStringSelection()
        motor_id  = int(self._motor_choice.GetStringSelection())
        enc_sel   = self._enc_choice.GetStringSelection()
        fake_enc  = enc_sel == "FAKE"
=======
        port     = self._port.GetStringSelection()
        motor_id = int(self._motor_choice.GetStringSelection())
        enc_id   = int(self._enc_choice.GetStringSelection())
>>>>>>> feature/cm-control-updates
        self._set_status("Connecting…", 180, 120, 0)
        wx.Yield()
        try:
            net = canopen.Network()
            if platform.system() == "Windows":
                net.connect(bustype='pcan', channel=port, bitrate=1_000_000)
            else:
                net.connect(bustype='socketcan', channel=port, bitrate=1_000_000)

            n1 = net.add_node(motor_id, EDS_FILE)
<<<<<<< HEAD
            n1.nmt.state = 'OPERATIONAL'
            n1.tpdo.read(); n1.rpdo.read()
=======
            n2 = net.add_node(enc_id,   EDS_FILE)
            n1.nmt.state = 'OPERATIONAL'
            n2.nmt.state = 'OPERATIONAL'
            n1.tpdo.read(); n1.rpdo.read(); n2.tpdo.read()
>>>>>>> feature/cm-control-updates

            if n1.rpdo[1].cob_id is None:
                n1.rpdo[1].cob_id = 0x200 + motor_id
            if n1.rpdo[2].cob_id is None:
                n1.rpdo[2].cob_id = 0x300 + motor_id

            n1.tpdo[1].add_callback(self._cb_p1_pos)
<<<<<<< HEAD

            if fake_enc:
                n2 = None
                # Fake encoder: pendulum permanently reads as hanging straight down.
                # _puck2_pos stays 0; auto-zero fires immediately on the stable window.
                with self._lock:
                    self._puck2_pos  = 0
                    self._puck2_zero = 0
            else:
                enc_id = int(enc_sel)
                n2 = net.add_node(enc_id, EDS_FILE)
                n2.nmt.state = 'OPERATIONAL'
                n2.tpdo.read()
                n2.tpdo[1].add_callback(self._cb_p2_pos)

            # Load PVCA motor params + correction table before SYNC starts
            # (SDO reads must not overlap with SYNC-driven TPDO traffic).
            pvca_ok, pvca_msg = self._load_pvca_params(n1)
            if not pvca_ok:
                net.disconnect()
                self._set_status(pvca_msg, 180, 0, 0)
                return
            print(f"[connect] {pvca_msg}")

            self._network   = net
            self._node1     = n1
            self._node2     = n2
            self._fake_enc  = fake_enc
            self._motor_id  = motor_id
            self._enc_label = "FAKE" if fake_enc else enc_sel
            self._connected = True

            p1_now = n1.sdo["PositionFeedback"].raw
            with self._lock:
                self._puck1_pos    = p1_now
                self._puck1_zero   = p1_now
                self._puck1_target = p1_now
            if not fake_enc:
                p2_now = n2.sdo["PositionFeedback"].raw
                with self._lock:
                    self._puck2_pos  = p2_now
                    self._puck2_zero = p2_now
=======
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
>>>>>>> feature/cm-control-updates

            net.sync.start(1.0 / SYNC_HZ)

            self._btn_scan.Disable()
            self._motor_choice.Disable()
            self._enc_choice.Disable()
            self._btn_conn.SetLabel("Disconnect")
            self._btn_en.Enable()
            self._btn_zero.Enable()
<<<<<<< HEAD
            enc_desc = "FAKE encoder" if fake_enc else f"Encoder={enc_sel}"
            self._set_status(
                f"Connected  (Motor={motor_id}, {enc_desc})  —  "
                + ("fake encoder active" if fake_enc else "waiting for pendulum to settle…"),
=======
            self._set_status(
                f"Connected  (Motor={motor_id}, Encoder={enc_id})  —  waiting for pendulum to settle…",
>>>>>>> feature/cm-control-updates
                180, 120, 0)

            threading.Thread(target=self._auto_zero_thread, daemon=True).start()
            threading.Thread(target=self._temp_monitor_thread, daemon=True).start()
<<<<<<< HEAD
            if fake_enc:
                threading.Thread(target=self._fake_encoder_thread, daemon=True).start()
=======
            # threading.Thread(target=self._debug_thread, daemon=True).start()
>>>>>>> feature/cm-control-updates

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
        self._btn_ctrl.Disable()
<<<<<<< HEAD
=======
        # self._debug_label.SetLabel("Debug: --")
        # self._debug_label.SetForegroundColour(wx.Colour(120, 130, 160))
>>>>>>> feature/cm-control-updates
        self._temp_label.SetLabel("Temp: --°C")
        self._temp_label.SetForegroundColour(wx.Colour(120, 130, 160))
        self._set_status("Disconnected", 160, 60, 60)

<<<<<<< HEAD
    # ───────────────────────────────────────────── PVCA param loading ───

    def _load_pvca_params(self, node):
        """Read motor params from SDO and load the encoder correction table.

        Must be called before SYNC starts (SDO access is synchronous here).
        Returns (ok, status_message).
        """
        # Encoder correction table (optional — run 'Generate Encoder Correction
        # Table' in PuckUtilityApp first; PVCA still works without it, just
        # without correction).
        log_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'logs')
        hits = sorted(glob.glob(os.path.join(log_dir, 'enc_correction_full_*.csv')))
        table = None
        if hits:
            table = []
            try:
                with open(hits[-1]) as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith('#') \
                                or line.startswith('enc') or line.startswith('pos'):
                            continue
                        parts = line.split(',')
                        if len(parts) == 2:
                            try:
                                table.append(int(parts[1]))
                            except ValueError:
                                pass
            except Exception:
                table = None

        # Motor parameters — required for the torque → ud conversion.
        try:
            e_zero         = node.sdo['Calibration']['e_zero'].raw
            e_polarity     = int(node.sdo['Calibration']['e_polarity'].raw)
            enc_resolution = node.sdo['EncoderConfig']['Resolution'].raw
            motor_poles    = node.sdo['Calibration']['poles'].raw
            cts_per_elec   = enc_resolution * 2.0 / motor_poles
            Kt             = node.sdo['Calibration']['kt'].raw           # mNm/A
            Rt             = node.sdo['Calibration']['rt'].raw * 0.01   # 0.01 Ω → Ω
            V_bus          = node.sdo['Amp']['NominalBusVoltage'].raw * 0.1  # V×10 → V
            i_peak         = node.sdo['Calibration']['i_peak'].raw       # mA
        except Exception as ex:
            return False, f"Cannot read motor params from node {node.id}: {ex}"

        if Kt == 0 or V_bus == 0 or Rt == 0:
            return False, f"Motor node {node.id} not calibrated — run calibration first"

        self._pvca_table   = table
        self._pvca_mp      = dict(
            e_zero=e_zero, e_polarity=e_polarity,
            enc_resolution=enc_resolution, cts_per_elec=cts_per_elec,
            Kt=Kt, Rt=Rt, V_bus=V_bus, i_peak=i_peak)
        self._pvca_sdo_cob = (0x600 | node.id) & 0x7FF

        table_info = f"{len(table)} entries" if table else "none — run calibration for correction"
        return True, (f"PVCA ready  Kt={Kt}mNm/A  Rt={Rt:.2f}Ω  "
                      f"V_bus={V_bus:.1f}V  table={table_info}")

=======
>>>>>>> feature/cm-control-updates
    # ───────────────────────────────────────────────── TPDO callbacks ───

    def _cb_p1_pos(self, _):
        try:
<<<<<<< HEAD
            pos = self._node1.tpdo[1]['PositionFeedback'].raw
            with self._lock:
                self._puck1_pos    = pos
                self._puck1_target = pos   # keep fake-encoder thread in sync
        except Exception:
            return

        if self._enabled and self._pvca_mp:
            torque = self._torque_cmd   # GIL-safe float read
            theta_raw, ud = _pvca_compute(
                pos, self._pvca_table, self._pvca_mp, torque)
            sdo = self._pvca_sdo_cob
            net = self._node1.network
            try:
                net.send_message(sdo,
                    struct.pack('<BBBBhxx', 0x2B, 0xEA, 0x60, 0x00, theta_raw))
                net.send_message(sdo,
                    struct.pack('<BBBBhxx', 0x2B, 0x10, 0x30, 0x04, ud))
            except Exception:
                pass

=======
            with self._lock:
                self._puck1_pos = self._node1.tpdo[1]['PositionFeedback'].raw
        except Exception:
            pass
>>>>>>> feature/cm-control-updates
        now = time.monotonic()
        if now - self._last_draw >= 1.0 / DISPLAY_HZ:
            self._last_draw = now
            wx.CallAfter(self._refresh_canvas)

    def _cb_p2_pos(self, _):
        try:
            with self._lock:
                self._puck2_pos = self._node2.tpdo[1]['PositionFeedback'].raw
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
            mode = "swing"
<<<<<<< HEAD
        self._canvas.update(pend_rad, p1, mode, self._motor_id, self._enc_label)
=======
        self._canvas.update(pend_rad, p1, mode)
>>>>>>> feature/cm-control-updates

    # ───────────────────────────────────────────────── auto-zero ────────

    def _auto_zero_thread(self):
        WINDOW = 80
        THRESH = 12
        history = []
        deadline = time.monotonic() + 15.0
        while self._connected and time.monotonic() < deadline:
            with self._lock:
                pos = self._puck2_pos
            history.append(pos)
            if len(history) > WINDOW:
                history.pop(0)
            if len(history) == WINDOW and (max(history) - min(history)) <= THRESH:
                avg = sum(history) // len(history)
                with self._lock:
                    self._puck2_zero = avg
                wx.CallAfter(self._set_status,
                             "Auto-zeroed at rest  |  ready to enable")
                return
            time.sleep(1.0 / SYNC_HZ)
        if self._connected:
            wx.CallAfter(self._set_status,
                         "Pendulum didn't settle — click Zero when hanging at rest",
                         180, 120, 0)

<<<<<<< HEAD
    # ───────────────────────────────────────────── fake encoder physics ──

    def _fake_encoder_thread(self):
        """Simulate Furuta pendulum dynamics driven by actual arm position feedback.

        Uses the simplified Furuta ODE:
            θ̈ = (g/L₂)·sin θ  −  (L₁/L₂)·φ̈·cos θ  −  b·θ̇
        where φ̈ is the arm angular acceleration (derived from TPDO counts).

        The thread waits until the motor is enabled before perturbing the
        pendulum, so auto-zero can complete against a stable position (0 counts)
        while the motor is still off.
        """
        g       = 9.81
        L1      = ARM_LENGTH_M
        L2      = PEND_LENGTH_M
        damping = 0.3
        dt      = 1.0 / SYNC_HZ

        # 30 Hz cutoff — high enough to pass arm direction-reversals (~1 Hz) with
        # minimal phase lag, while still rejecting pure quantisation spikes.
        # We track the COMMANDED target (not feedback) so position is already
        # slew-rate-limited and free of encoder quantisation noise.
        a = min(1.0, 2 * math.pi * 30.0 / SYNC_HZ)

        theta     = math.pi
        theta_dot = 0.0

        prev_arm_cts = self._puck1_target
        arm_vel_filt = 0.0
        arm_acc_filt = 0.0

        # Stay idle until motor enables so auto-zero fires against a stable window.
        while self._connected and self._fake_enc and not self._enabled:
            prev_arm_cts = self._puck1_target
            time.sleep(0.02)
        if not (self._connected and self._fake_enc):
            return

        # Bootstrap the energy controller: give the pendulum a small initial
        # velocity so vel_slow crosses the 0.1 rad/s threshold quickly.
        theta_dot = 0.5

        prev_t = time.monotonic()

        while self._connected and self._fake_enc:
            t0     = time.monotonic()
            act_dt = min(max(t0 - prev_t, 1e-5), 0.05)
            prev_t = t0

            # Read commanded target — smooth and quantisation-free between cycles.
            arm_cts      = self._puck1_target
            d_cts        = arm_cts - prev_arm_cts
            prev_arm_cts = arm_cts

            arm_vel_raw  = d_cts * 2.0 * math.pi / ENCODER_RES / act_dt
            arm_vel_new  = a * arm_vel_raw  + (1.0 - a) * arm_vel_filt
            arm_acc_raw  = (arm_vel_new - arm_vel_filt) / act_dt
            # Clamp raised to 200 rad/s² — target trajectory is smooth so this
            # only clips genuine large direction reversals, not noise.
            arm_acc_filt = max(-200.0, min(200.0,
                               a * arm_acc_raw + (1.0 - a) * arm_acc_filt))
            arm_vel_filt = arm_vel_new

            # Furuta pendulum ODE (θ = 0 upright, θ = π down)
            theta_ddot = ((g / L2) * math.sin(theta)
                          - (L1 / L2) * arm_acc_filt * math.cos(theta)
                          - damping * theta_dot)

            theta_dot += theta_ddot * act_dt
            theta     += theta_dot  * act_dt

            # Map θ → encoder counts.
            # Control loop: pend_rad = _wrap(p2 * 2π/ENC + π)
            # → p2 = (θ − π) * ENC / (2π)  gives pend_rad = θ (before wrap).
            with self._lock:
                self._puck2_pos = int(
                    (theta - math.pi) * ENCODER_RES / (2.0 * math.pi))

            elapsed = time.monotonic() - t0
            if elapsed < dt:
                time.sleep(dt - elapsed)
=======
    # ───────────────────────────────────────────────── debug ─────────────

    # def _debug_thread(self):
    #     while self._connected:
    #         try:
    #             colour = (120, 130, 160)
    #             wx.CallAfter(self._update_debug_label, self._debug_val, colour)
    #         except Exception:
    #             pass
    #         time.sleep(0.1)

    # def _update_debug_label(self, val, colour):
    #     self._debug_label.SetLabel(f"Debug: {val}")
    #     self._debug_label.SetForegroundColour(wx.Colour(*colour))
>>>>>>> feature/cm-control-updates

    # ───────────────────────────────────────────────── temperature ───────

    def _temp_monitor_thread(self):
        while self._connected:
            try:
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

    def _enable_motor(self):
        try:
<<<<<<< HEAD
            n   = self._node1
            mp  = self._pvca_mp
=======
            n = self._node1
>>>>>>> feature/cm-control-updates
            self._network.sync.stop()
            time.sleep(0.04)

            cur = n.sdo["PositionFeedback"].raw

            n.sdo["ControlWord"].raw = CLEAR_FAULT;  time.sleep(0.05)
            n.sdo["ControlWord"].raw = SHUTDOWN;      time.sleep(0.05)
            n.sdo["ControlWord"].raw = OP_ENABLED

<<<<<<< HEAD
            # Switch to Phase-Voltage/Commutation-Angle mode and zero outputs.
            n.sdo["SetModeOfOperation"].raw = MODE_PHASE_VOLTAGE_ANGLE
            n.sdo['Theta_e'].raw     = 0
            n.sdo['Motor']['ud'].raw = 0

            # Pre-fill RPDO1 CAN buffer: ControlWord + ModeOfOperation + 0.
            # RPDO1 is trans_type=0 — it applies its buffer on every SYNC.  Without
            # this pre-fill the startup buffer holds ControlWord=0 (Disable Voltage)
            # which kills the drive on every tick.  We cannot disable RPDO1 via its
            # COB-ID invalid bit — firmware silently ignores that in NMT Operational.
            rpdo1_cob = n.rpdo[1].cob_id if n.rpdo[1].cob_id is not None \
                        else (0x200 + n.id)
            n.network.send_message(rpdo1_cob,
                struct.pack('<HBh', OP_ENABLED, MODE_PHASE_VOLTAGE_ANGLE, 0))
            time.sleep(0.1)

            # Compute and display the hard torque cap in mNm.
            try:
                lim_pct = max(1.0, min(100.0, float(self._torque_limit.GetValue())))
            except ValueError:
                lim_pct = 50.0
            self._torque_limit_pct = lim_pct
            max_torque_mNm = lim_pct / 100.0 * mp['i_peak'] * mp['Kt'] / 1000.0
            wx.CallAfter(self._torque_label.SetLabel,
                         f"(cap {max_torque_mNm:.0f} mNm)")
            print(f"[enable] PVCA  Kt={mp['Kt']} mNm/A  Rt={mp['Rt']:.2f}Ω  "
                  f"V_bus={mp['V_bus']:.1f}V  cap={max_torque_mNm:.0f}mNm  "
                  f"table={'yes' if self._pvca_table else 'none'}")

            self._torque_cmd = 0.0
=======
            n.rpdo[1]["SetModeOfOperation"].raw = MODE_CYCLIC_SYNC_POS
            n.rpdo[1]["ControlWord"].raw = OP_ENABLED
            n.rpdo[1].transmit()

            n.rpdo[2]["TargetPosition"].raw = cur
            n.rpdo[2].transmit()

>>>>>>> feature/cm-control-updates
            with self._lock:
                self._puck1_zero = cur

            self._enabled = True
            self._btn_en.SetLabel("Disable Motor")
            self._btn_ctrl.Enable()
<<<<<<< HEAD
            self._set_status("Motor enabled  |  PVCA  (arm free until controller starts)")
=======
            self._set_status("Motor enabled  |  holding position")
>>>>>>> feature/cm-control-updates
            self._network.sync.start(1.0 / SYNC_HZ)

        except Exception as ex:
            traceback.print_exc()
            self._set_status(f"Enable failed ({type(ex).__name__}): {ex}", 180, 0, 0)
            try:
                self._network.sync.start(1.0 / SYNC_HZ)
            except Exception:
                pass

    def _disable_motor(self, quiet=False):
<<<<<<< HEAD
        self._torque_cmd = 0.0
        try:
            if self._node1 and self._pvca_sdo_cob:
                # Zero PVCA outputs before killing the drive
                sdo = self._pvca_sdo_cob
                net = self._node1.network
                net.send_message(sdo,
                    struct.pack('<BBBBhxx', 0x2B, 0xEA, 0x60, 0x00, 0))
                net.send_message(sdo,
                    struct.pack('<BBBBhxx', 0x2B, 0x10, 0x30, 0x04, 0))
                time.sleep(0.02)
                self._node1.sdo['Theta_e'].raw     = 0
                self._node1.sdo['Motor']['ud'].raw = 0
                self._node1.sdo['SetModeOfOperation'].raw = MODE_IDLE
        except Exception:
            pass
        try:
=======
        try:
            if self._node1:
                self._node1.rpdo[1]["SetModeOfOperation"].raw = MODE_IDLE
                self._node1.rpdo[1]["ControlWord"].raw = SHUTDOWN
                self._node1.rpdo[1].transmit()
            time.sleep(0.05)
>>>>>>> feature/cm-control-updates
            if self._network:
                self._network.sync.stop()
                time.sleep(0.04)
            if self._node1:
                self._node1.sdo["ControlWord"].raw = SHUTDOWN
        except Exception:
            pass
        self._enabled = False
        if not quiet:
            self._btn_en.SetLabel("Enable Motor")
            self._btn_ctrl.Disable()
            self._set_status("Motor disabled", 150, 110, 0)

    # ───────────────────────────────────────────────── control loop ─────

    def _on_ctrl_toggle(self, _):
        if self._controlling:
            self._stop_control()
        else:
            self._start_control()

    def _start_control(self):
        try:
            kp = float(self._kp.GetValue())
            ki = float(self._ki.GetValue())
            kd = float(self._kd.GetValue())
<<<<<<< HEAD
            ks = float(self._ks.GetValue())
            kb = float(self._kb.GetValue())
            kv = float(self._kv.GetValue())
=======
            adz = float(self._adz.GetValue())
            bi = float(self._bi.GetValue())
            kt = float(self._kt.GetValue())
            kf = float(self._kf.GetValue())
            pdz = float(self._pdz.GetValue())
            tmax = float(self._tmax.GetValue())
            ks = float(self._ks.GetValue())
            kb = float(self._kb.GetValue())
            kv = float(self._kv.GetValue())
            el = float(self._el.GetValue())
>>>>>>> feature/cm-control-updates
        except ValueError:
            self._set_status("Invalid gain — use numeric values", 180, 0, 0)
            return

<<<<<<< HEAD
        self._in_balance  = False
        self._in_braking  = False
        self._controlling = True
        self._btn_ctrl.SetLabel("Stop")
        for tc in (self._kp, self._ki, self._kd, self._ks, self._kb, self._kv):
=======
        self._in_balance      = False
        self._ramping_balnace = False
        self._in_braking      = False
        self._controlling     = True
        self._btn_ctrl.SetLabel("Stop")
        for tc in (self._kp, self._ki, self._kd, self._adz, self._bi,
            self._kt, self._kf, self._pdz, self._tmax,
            self._ks, self._kb, self._kv, self._el):
>>>>>>> feature/cm-control-updates
            tc.Disable()
        self._set_status("Swingup…", 200, 130, 0)

        self._ctrl_thread = threading.Thread(
<<<<<<< HEAD
            target=self._control_loop, args=(kp, ki, kd, ks, kb, kv), daemon=True)
=======
            target=self._control_loop, args=(kp, ki, kd, adz, bi, kt, kf,
                pdz, tmax, ks, kb, kv, el), daemon=True)
>>>>>>> feature/cm-control-updates
        self._ctrl_thread.start()

    def _stop_control(self):
        self._controlling = False
        if self._ctrl_thread:
            self._ctrl_thread.join(timeout=1.5)
            self._ctrl_thread = None
        wx.CallAfter(self._on_ctrl_stopped)

    def _on_ctrl_stopped(self):
<<<<<<< HEAD
        self._in_balance = False
        self._in_braking = False
        self._btn_ctrl.SetLabel("Start")
        for tc in (self._kp, self._ki, self._kd, self._ks, self._kb, self._kv):
            tc.Enable()
        if self._enabled:
            self._torque_cmd = 0.0   # PVCA: zero torque → arm free-wheels
            self._set_status("Motor enabled  |  arm free (controller off)")

    def _control_loop(self, kp, ki, kd, ks, kb, kv):
        # ── Parameter reference ────────────────────────────────────────────
        #
        # BALANCE  (|θ| < 15°, |ω| < BALANCE_VEL_MAX)
        #   torque = −(Kp·θ + Ki·∫θ dt + Kd·ω)          [mNm]
        #   Kp  [mNm/rad]      angle correction — too high → oscillation
        #   Ki  [mNm/(rad·s)]  integral drift correction — too high → windup
        #   Kd  [mNm/(rad/s)]  velocity damping — too high → sluggish
        #   Integral resets to zero on every balance exit (no windup carry-over).
        #
        # SWINGUP / BRAKING  (energy controller, all other angles)
        #   Furuta energy:  de = ½ω² + ½κ²·ω₁²·sin²θ − (g/L)(1−cosθ)
        #   de < 0 → need energy → SWINGUP  (cap: ks mNm)
        #   de > 0 → too much   → BRAKING  (cap: kb mNm)
        #   Ks [mNm]         peak torque amplitude for swingup
        #   Kb [mNm]         peak torque amplitude for braking
        #   Kv [mNm/cycle]   torque slew-rate limit for swingup/braking
        #
        # Torque output is written to self._torque_cmd each cycle.
        # _cb_p1_pos reads it on every TPDO1 (every SYNC) and converts to
        # theta_e + ud via _pvca_compute, applying the encoder correction table.
        # ──────────────────────────────────────────────────────────────────

        dt_target = 1.0 / SYNC_HZ
        slew_mNm  = max(1.0, kv)   # mNm per cycle

        # Hard cap: torque_limit_pct × i_peak × Kt / 1000  [mNm]
        mp = self._pvca_mp
        cap_mNm = (self._torque_limit_pct / 100.0
                   * mp['i_peak'] * mp['Kt'] / 1000.0)

        a_slow     = min(1.0, 2 * math.pi * 3.0  / SYNC_HZ)  # ~3 Hz  — energy direction
        a_fast     = min(1.0, 2 * math.pi * 8.0  / SYNC_HZ)  # ~8 Hz  — PD velocity
        a_arm_damp = min(1.0, 2 * math.pi * 15.0 / SYNC_HZ)  # ~15 Hz — arm vel for damping

        in_balance   = False
=======
        self._in_balance =      False
        self._ramping_balnace = False
        self._in_braking =      False
        self._btn_ctrl.SetLabel("Start")
        for tc in (self._kp, self._ki, self._kd, self._adz, self._bi,
            self._kt, self._kf, self._pdz, self._tmax,
            self._ks, self._kb, self._kv, self._el):
            tc.Enable()
        if self._enabled:
            try:
                with self._lock:
                    cur = self._puck1_pos
                self._node1.rpdo[2]["TargetPosition"].raw = cur
                self._node1.rpdo[2].transmit()
            except Exception:
                pass
            self._set_status("Motor enabled  |  controller off")

    def _control_loop(self, kp, ki, kd, adz, bi, kt, kf, pdz, tmax, ks, kb, kv, el):
        # ── Parameter reference ────────────────────────────────────────────
        #
        # BALANCE  (|θ| < BALANCE_ENTRY, |ω| < BALANCE_VEL_MAX)
        #   θr = -(Kt·p + Kf·v)
        #   Kt  [rad/rad]            position correction — too high → oscillation
        #   Kf  [rad/(rad/s)]        velocity damping — too high → sluggish
        #   position deadzone [deg]  applies to position correction
        #   tilt max          [deg]  maximum tilt (θr) for position correction
        #
        #   u  = Kp·(θ - θr) + Ki·∫(θ - θr) dt + Kd·ω
        #   Kp  [counts/rad]      angle correction — too high → oscillation
        #   Ki  [counts/(rad·s)]  integral drift correction — too high → windup
        #   Kd  [counts/(rad/s)]  velocity damping — too high → sluggish
        #   Integral resets to zero on every balance exit (no windup carry-over).
        #   angle deadzone [deg]  applies to angle correction
        #
        # SWINGUP / BRAKING  (energy controller, all other angles)
        #   Furuta energy:  de = ½ω² + ½κ²·ω₁²·sin²θ − (g/L)(1−cosθ)
        #   de < 0 → need energy → SWINGUP  (cap: swing_cts)
        #   de > 0 → too much   → BRAKING  (cap: brake_cts)
        #   Ks [revolutions]  max arm travel swingup
        #   Kb [revolutions]  max arm travel braking
        #   Kv [rev/s]        slew-rate limit on swingup/braking commands
        # ──────────────────────────────────────────────────────────────────

        dt_target  = 1.0 / SYNC_HZ
        swing_cts  = int(ks * ENCODER_RES)
        brake_cts  = int(kb * ENCODER_RES)
        slew_cts   = max(1, int(kv * ENCODER_RES / SYNC_HZ))
        adz_rad = math.radians(adz)
        bi_rad = math.radians(bi)
        pdz_rad = math.radians(pdz)
        tmax_rad = math.radians(tmax)

        # Position-error clamp.
        # Limiting how far the commanded position can deviate from the measured
        # position caps the error the internal position controller must fight.
        el = max(0.0, el)
        err_limit_cts = int(el * ENCODER_RES)

        a_very_slow = 0.001#min(1,0, 2 * math.pi * 0.08 / SYNC_HZ)  # balance position zeroing
        a_slow      = min(1.0, 2 * math.pi * 3.0 / SYNC_HZ)  # ~3 Hz — energy direction
        a_fast      = min(1.0, 2 * math.pi * 8.0 / SYNC_HZ)  # ~8 Hz — PD velocity

        in_balance   = False
        balance_ramp = 0.0
>>>>>>> feature/cm-control-updates
        prev_pend    = 0.0
        vel_slow     = 0.0
        vel_fast     = 0.0
        prev_arm_rad = 0.0
        arm_vel      = 0.0
<<<<<<< HEAD
        arm_vel_damp = 0.0
        integral     = 0.0
        prev_torque  = 0.0
        prev_t       = time.monotonic()
=======
        integral     = 0.0
        with self._lock:
            prev_target = self._puck1_pos
            steady_target = self._puck1_pos - self._puck1_zero
        steady_target_z = steady_target
        prev_t = time.monotonic()
>>>>>>> feature/cm-control-updates

        while self._controlling and self._enabled:
            t0 = time.monotonic()
            dt = t0 - prev_t;  prev_t = t0
            if dt <= 0:
                dt = dt_target

            with self._lock:
                p1_abs  = self._puck1_pos
                p1_zero = self._puck1_zero
<<<<<<< HEAD
                p2      = self._puck2_pos - self._puck2_zero

            pend_rad = _wrap(p2 * 2.0 * math.pi / ENCODER_RES + math.pi)
            arm_rad  = p1_abs * 2.0 * math.pi / ENCODER_RES
=======
                p1      = p1_abs - p1_zero
                p2      = self._puck2_pos - self._puck2_zero

            pend_rad = _wrap(p2 * 2.0 * math.pi / ENCODER_RES + math.pi) - bi_rad
            arm_rad  = p1 * 2.0 * math.pi / ENCODER_RES
>>>>>>> feature/cm-control-updates

            # Pendulum velocity (wrap delta to avoid ±π spike)
            delta = pend_rad - prev_pend
            if delta > math.pi:    delta -= 2 * math.pi
            elif delta < -math.pi: delta += 2 * math.pi
            raw_vel  = delta / dt
            vel_slow = a_slow * raw_vel + (1.0 - a_slow) * vel_slow
            vel_fast = a_fast * raw_vel + (1.0 - a_fast) * vel_fast
            prev_pend = pend_rad

<<<<<<< HEAD
            # Arm angular velocity
            raw_arm_vel  = (arm_rad - prev_arm_rad) / dt
            arm_vel      = a_slow     * raw_arm_vel + (1.0 - a_slow)     * arm_vel
            arm_vel_damp = a_arm_damp * raw_arm_vel + (1.0 - a_arm_damp) * arm_vel_damp
=======
            # Arm angular velocity (continuous — no wrapping needed)
            raw_arm_vel  = (arm_rad - prev_arm_rad) / dt
            arm_vel      = a_slow * raw_arm_vel + (1.0 - a_slow) * arm_vel
>>>>>>> feature/cm-control-updates
            prev_arm_rad = arm_rad

            # ── mode transitions ─────────────────────────────────────────
            if in_balance:
<<<<<<< HEAD
                if abs(pend_rad) > BALANCE_EXIT or abs(vel_fast) > BALANCE_VEL_MAX:
                    in_balance = False
                    integral   = 0.0
=======
                if abs(vel_fast) > BALANCE_VEL_MAX:
                    self._debug_val = 1.0
                if abs(pend_rad) > BALANCE_EXIT or abs(vel_fast) > BALANCE_VEL_MAX:
                    in_balance = False
                    integral   = 0.0
                    steady_target = p1
                    steady_target_z = steady_target
>>>>>>> feature/cm-control-updates
                    wx.CallAfter(self._set_status, "Swingup…", 200, 130, 0)
            else:
                if abs(pend_rad) < BALANCE_ENTRY and abs(vel_fast) < BALANCE_VEL_MAX:
                    in_balance = True
<<<<<<< HEAD
                    wx.CallAfter(self._set_status, "Balancing…", 0, 110, 185)
            self._in_balance = in_balance

            if in_balance:
                # PID balance — output in mNm
                integral = max(-INTEGRAL_CLAMP,
                               min(INTEGRAL_CLAMP, integral + pend_rad * dt))
                torque   = -(kp * pend_rad + ki * integral + kd * vel_fast)
                torque   = max(-cap_mNm, min(cap_mNm, torque))
                prev_torque      = torque
                self._in_braking = False

            elif abs(vel_slow) > 0.1:
                # Proportional energy controller with Furuta centripetal correction
                de      = (0.5 * vel_slow**2
                           + 0.5 * COUPLING**2 * arm_vel**2 * math.sin(pend_rad)**2
                           - OMEGA_N_SQ * (1.0 - math.cos(pend_rad)))
                pump    = math.copysign(1.0, de * vel_slow * math.cos(pend_rad))
                braking = de > 0
                cap     = kb if braking else ks    # peak torque in mNm
                amp     = min(cap, abs(de) / OMEGA_N_SQ * cap)
                torque  = pump * amp

                # Arm velocity damping: without this, pure torque control
                # accelerates the arm indefinitely in one direction.
                # Terminal speed ≈ ks / K_ARM_DAMP; at ks=200 → ~20 rad/s.
                torque += -10.0 * arm_vel_damp

                # Quadratic arm centering spring: negligible near center,
                # equals ks at the travel limit — prevents arm runaway without
                # fighting the swingup at small displacements.
                arm_frac = (p1_abs - p1_zero) / MAX_ARM_CTS   # signed, ±1 at limit
                torque  += -math.copysign(ks * min(1.0, arm_frac**2), arm_frac)

                # Hard travel guard: past limit, actively return to center
                if abs(p1_abs - p1_zero) > MAX_ARM_CTS:
                    torque = -math.copysign(ks * 0.5, p1_abs - p1_zero)

                # Slew-rate limit
                step        = max(-slew_mNm, min(slew_mNm, torque - prev_torque))
                torque      = prev_torque + step
                prev_torque = torque
                self._in_braking = braking

            else:
                torque      = 0.0   # pendulum nearly stationary — coast
                prev_torque = 0.0
                self._in_braking = False

            # Publish for _cb_p1_pos to consume on the next TPDO1 (next SYNC)
            self._torque_cmd = torque
=======
                    arm_rad_target = p1 * 2.0 * math.pi / ENCODER_RES
                    arm_rad_target_z = arm_rad_target
                    wx.CallAfter(self._set_status, "Balancing…", 0, 110, 185)
            self._in_balance = in_balance


            if in_balance:

                # Ramping up balance feedback
                if balance_ramp < 1.0:
                  balance_ramp = balance_ramp + dt * BALANCE_RAMP_ON_RATE
                if balance_ramp > 1.0:
                  balance_ramp = 1.0

                # Slow return to zero for balance position reference
                arm_rad_target_z = (1.0 - a_very_slow) * arm_rad_target_z
                arm_rad_target = a_very_slow * arm_rad_target_z + (1.0 - a_very_slow) * arm_rad_target
                self._debug_val = arm_rad_target * 180.0 / (2 * math.pi)

                # Add deadzone for balance position feedback
                arm_rad_dz = arm_rad - arm_rad_target
                if arm_rad_dz > pdz_rad:
                  arm_rad_dz -= pdz_rad
                elif arm_rad_dz < -pdz_rad:
                  arm_rad_dz += pdz_rad
                else:
                  arm_rad_dz = 0.0
                
                # PD balance position
                tilt_angle_rad = kt * arm_rad_dz + kf * arm_vel
                tilt_angle_rad = max(-tmax_rad, min(tmax_rad, tilt_angle_rad))

                # Add deadzone for balance angle feedback
                pend_rad_dz = pend_rad + tilt_angle_rad
                if pend_rad_dz > adz_rad:
                  pend_rad_dz -= adz_rad
                elif pend_rad_dz < -adz_rad:
                  pend_rad_dz += adz_rad
                else:
                  pend_rad_dz = 0.0

                # PID balance angle
                integral    = max(-INTEGRAL_CLAMP, min(INTEGRAL_CLAMP,
                              integral + pend_rad_dz * dt))
                u           = balance_ramp * (kp * pend_rad_dz + ki * integral +  kd * vel_fast)
                target      = int(p1_abs + u)

                # Limit total arm travel
                target      = max(p1_zero - MAX_ARM_CTS,
                              min(p1_zero + MAX_ARM_CTS, target))

                prev_target = target
                self._in_braking = False

            elif abs(vel_slow) > 0.1: # Swinging

                # Ramp down this parameter when not in balance state. This
                # does not mean we continue to apply balance feedback.
                if balance_ramp > 0.0:
                  balance_ramp = balance_ramp - dt * BALANCE_RAMP_OFF_RATE
                if balance_ramp < 0.0:
                  balance_ramp = 0.0

                # Slow return to zero for swing-up position reference
                steady_target_z = (1.0 - a_very_slow) * steady_target_z
                steady_target = a_very_slow * steady_target_z + (1.0 - a_very_slow) * steady_target

                # Proportional energy controller with Furuta centripetal correction
                de     = (0.5 * vel_slow**2
                          + 0.5 * COUPLING**2 * arm_vel**2 * math.sin(pend_rad)**2
                          - OMEGA_N_SQ * (1.0 - math.cos(pend_rad)))
                pump   = math.copysign(1.0, de * vel_slow * math.cos(pend_rad))
                braking = de > 0
                cap    = brake_cts if braking else swing_cts
                amp    = min(cap, int(abs(de) / OMEGA_N_SQ * cap))
                target = int(p1_zero + steady_target + pump * amp)
                target = max(p1_zero + steady_target - MAX_ARM_CTS,
                         min(p1_zero + steady_target + MAX_ARM_CTS, target))
                
                # Slew-rate limit
                step   = max(-slew_cts, min(slew_cts, target - prev_target))
                target = prev_target + step
                prev_target = target
                self._in_braking = braking

            else: # Resting

                # Ramp down this parameter when not in balance state. This
                # does not mean we continue to apply balance feedback.
                if balance_ramp > 0.0:
                  balance_ramp = balance_ramp - dt * BALANCE_RAMP_OFF_RATE
                if balance_ramp < 0.0:
                  balance_ramp = 0.0
                
                # Slow return to zero for swing-up position reference
                steady_target_z = (1.0 - a_very_slow) * steady_target_z
                steady_target = a_very_slow * steady_target_z + (1.0 - a_very_slow) * steady_target
                
                target = p1_zero + steady_target
                self._in_braking = False

            # Position-error clamp — software backstop regardless of mode.
            # Keeps commanded position within max_err_cts of current position.
            err = target - p1_abs
            if err > err_limit_cts:
                target = p1_abs + err_limit_cts
            elif err < -err_limit_cts:
                target = p1_abs - err_limit_cts

            try:
                self._node1.rpdo[2]["TargetPosition"].raw = target
                self._node1.rpdo[2].transmit()
            except Exception:
                break
>>>>>>> feature/cm-control-updates

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


<<<<<<< HEAD
def _pvca_compute(actual_pos, table, mp, torque_mNm):
    """Convert arm position + torque command to Theta_e (i16) and ud (i16) for PVCA.

    Mirrors the calculation in calibrate_menu._PVCATorqueDialog._on_tpdo1.
    Returns (theta_e_raw, ud) ready for fire-and-forget SDO writes.
    """
    enc_res  = mp['enc_resolution']
    enc_idx  = int(actual_pos) % enc_res
    corr     = table[enc_idx] if table is not None else 0
    corr_pos = actual_pos + corr
    theta_f  = (corr_pos - mp['e_zero']) * mp['e_polarity'] / mp['cts_per_elec'] * 65536.0
    theta_i  = int(round(theta_f)) % 65536
    advance  = 16384 if torque_mNm >= 0 else -16384
    theta_u  = (theta_i + advance) % 65536
    theta_raw = theta_u if theta_u < 32768 else theta_u - 65536

    iq_ma = min(abs(torque_mNm) * 1000.0 / mp['Kt'], mp['i_peak'])
    vq    = iq_ma / 1000.0 * mp['Rt']
    ud    = int(round(vq / mp['V_bus'] * 32767))
    ud    = max(0, min(ud, int(0.85 * 32767)))
    return theta_raw, ud


=======
>>>>>>> feature/cm-control-updates
# ──────────────────────────────────────────────────────── entry point ─────

if __name__ == "__main__":
    app = wx.App()
    FurutaPIDFrame().Show()
    app.MainLoop()
