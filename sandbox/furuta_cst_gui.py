#!/usr/bin/env python3
"""
Furuta Pendulum — Energy Swingup + PID Balance Controller  (CST mode)
  Puck 1 (node 1) — rotating arm motor driven via Cyclic-Sync Torque mode
  Puck 2 (node 2) — passive pendulum encoder (read-only)

Three automatic modes:
  SWINGUP  — energy-based pump builds pendulum amplitude from rest
  BRAKING  — energy-based damping absorbs excess energy near upright
  BALANCE  — PID holds upright once within ±15°

TargetTorque units: raw INTEGER16 sent to 0x6071.
  DS402 convention: 1000 = 100 % of RatedTorque (0x6076).
  Confirm the Puck's actual scaling before increasing Tmax.

  Kp   [trq/rad]        Balance: proportional angle correction.
  Ki   [trq/(rad·s)]    Balance: integral, eliminates steady-state drift.
  Kd   [trq/(rad/s)]    Balance: derivative velocity damping.
  Ka   [trq/rad]        Arm centering: restoring torque toward home position.
  Kaf  [trq/(rad/s)]    Arm centering: velocity damping term.
  Ks   [trq]            Swingup: peak arm torque during energy pump.
  Kb   [trq]            Braking: peak arm torque while absorbing energy.
  Kv   [trq/cycle]      Slew-rate limit on torque changes (at 500 Hz).
  Tmax [trq]            Absolute torque cap applied to all modes.

Angle convention: θ = 0 upright, ±π hanging down (pendulum, P2).
"""

import wx
import canopen
import platform
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
    MODE_IDLE, MODE_CYCLIC_SYNC_TRQ
)

ENCODER_RES     = 4096
EDS_FILE        = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'puck4.eds')
SYNC_HZ         = 500
BALANCE_ENTRY   = math.radians(42)   # engage PID inside ±42°
BALANCE_EXIT    = math.radians(55)   # disengage outside ±55°
BALANCE_VEL_MAX = 15                 # rad/s — max velocity to engage
INTEGRAL_CLAMP  = 500                # torque units — anti-windup clamp
PEND_LENGTH_M   = 0.2413             # pendulum rod length (m) — 9.5 inches
ARM_LENGTH_M    = 0.12383            # rotating arm length (m) — 4 7/8 inches
COUPLING        = ARM_LENGTH_M / PEND_LENGTH_M   # κ = L₁/L₂
OMEGA_N_SQ      = 9.8 / PEND_LENGTH_M            # g/L₂ (rad/s)²
MAX_ARM_REV     = 5.0                # soft travel guard (revolutions)
MAX_ARM_CTS     = int(MAX_ARM_REV * ENCODER_RES)
MAX_ARM_VEL     = 10.0               # rad/s — arm speed limit (~1.6 rev/s)
DISPLAY_HZ      = 25
BALANCE_RAMP_ON_RATE  = 5.0
BALANCE_RAMP_OFF_RATE = 1.0
ARM_RESTORE_FRAC      = 0.03         # max fraction of Tmax used for arm centering

# Tunable parameter defaults
KP_DEFAULT   = 400
KI_DEFAULT   = 0
KD_DEFAULT   = 50
ADZ_DEFAULT  = 1
BI_DEFAULT   = 0
KA_DEFAULT   = 50
KAF_DEFAULT  = 10
ADZ_ARM_DEFAULT = 150   # arm centering deadzone, deg (matches CSP Pdz default)
KS_DEFAULT   = 800
KB_DEFAULT   = 400
KV_DEFAULT   = 120      # slew rate (torque units per cycle at 250 Hz control rate)
TMAX_DEFAULT = 990


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
        self._pend_rad = 0.0
        self._arm_cts  = 0
        self._mode     = "idle"
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
        dc.DrawText(f"Pendulum (P2): {ang_deg:+7.2f}°", 8, 6)
        dc.DrawText(f"Arm      (P1): {arm_deg:+7.2f}°", 8, 22)
        dc.SetTextForeground(col)
        dc.DrawText(mode_str, W - 120, 6)


# ──────────────────────────────────────────────────────── main frame ──────

class FurutaCSTFrame(wx.Frame):

    TEMP_SHUTDOWN_C = 85

    def __init__(self):
        super().__init__(None, title="Furuta Pendulum Controller (CST)", size=(720, 520))

        self._network = None
        self._node1   = None
        self._node2   = None
        self._lock    = threading.Lock()

        self._puck1_pos  = 0
        self._puck2_pos  = 0
        self._puck1_zero = 0
        self._puck2_zero = 0

        self._last_draw   = 0.0
        self._connected        = False
        self._enabled          = False
        self._controlling      = False
        self._in_balance       = False
        self._in_braking       = False
        self._cs               = None   # control state, set by _start_control
        self._last_i2t         = -1
        self._ctrl_tick        = 0     # counts TPDO callbacks; control runs every 2nd

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
        gbx = wx.StaticBox(root, label="Controller Gains  (CST mode — TargetTorque units, 1000 ≈ rated)")
        outer = wx.StaticBoxSizer(gbx, wx.VERTICAL)

        def gain(sizer, label, default, tip):
            sizer.Add(wx.StaticText(root, label=label),
                      0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 8)
            tc = wx.TextCtrl(root, value=str(default), size=(62, -1))
            tc.SetToolTip(tip)
            sizer.Add(tc, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT | wx.RIGHT, 3)
            return tc

        # Row 1 — balance angle PID
        row1 = wx.BoxSizer(wx.HORIZONTAL)
        row1.Add(wx.StaticText(root, label="Balance (angle):"),
                 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 6)
        self._kp = gain(row1, "Kp:", KP_DEFAULT,
            "Proportional angle correction [trq/rad]. Too high → oscillation.")
        self._ki = gain(row1, "Ki:", KI_DEFAULT,
            "Integral: eliminates steady-state drift [trq/(rad·s)]. Too high → windup.")
        self._kd = gain(row1, "Kd:", KD_DEFAULT,
            "Derivative velocity damping [trq/(rad/s)]. Too high → sluggish.")
        self._adz = gain(row1, "Adz:", ADZ_DEFAULT,
            "Deadzone [deg] for pendulum angle error feedback.")
        self._bi = gain(row1, "Bias:", BI_DEFAULT,
            "Angle bias [deg] for pendulum angle feedback.")
        outer.Add(row1, 0, wx.EXPAND | wx.TOP | wx.BOTTOM, 3)

        # Row 2 — arm centering (replaces CSP position reference)
        row2 = wx.BoxSizer(wx.HORIZONTAL)
        row2.Add(wx.StaticText(root, label="Arm centering:"),
                 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 6)
        self._ka = gain(row2, "Ka:", KA_DEFAULT,
            "Arm centering proportional [trq/rad]. Applies in all modes to restore arm to home.")
        self._kaf = gain(row2, "Kaf:", KAF_DEFAULT,
            "Arm centering velocity damping [trq/(rad/s)].")
        self._adz_arm = gain(row2, "Adz:", ADZ_ARM_DEFAULT,
            "Deadzone [deg] for arm position error (prevents dithering at center).")
        outer.Add(row2, 0, wx.EXPAND | wx.TOP | wx.BOTTOM, 3)

        # Row 3 — swingup torque limits + button
        row3 = wx.BoxSizer(wx.HORIZONTAL)
        row3.Add(wx.StaticText(root, label="Swingup:"),
                 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 6)
        self._ks = gain(row3, "Ks:", KS_DEFAULT,
            "Peak torque during swingup energy pump. Negate if pendulum damps instead of grows.")
        self._kb = gain(row3, "Kb:", KB_DEFAULT,
            "Peak torque during braking. Increase if pendulum overshoots upright.")
        self._kv = gain(row3, "Kv:", KV_DEFAULT,
            "Max torque change per cycle (slew rate). Lower to reduce jerk.")
        self._tmax = gain(row3, "Tmax:", TMAX_DEFAULT,
            "Absolute torque cap applied to all modes. Keep well within rated torque.")
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

    def _connect(self):
        port     = self._port.GetStringSelection()
        motor_id = int(self._motor_choice.GetStringSelection())
        enc_id   = int(self._enc_choice.GetStringSelection())
        self._set_status("Connecting…", 180, 120, 0)
        wx.Yield()
        try:
            net = canopen.Network()
            if platform.system() == "Windows":
                net.connect(bustype='pcan', channel=port, bitrate=1_000_000)
            else:
                net.connect(bustype='socketcan', channel=port, bitrate=1_000_000)

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

            net.sync.start(1.0 / SYNC_HZ)

            self._btn_scan.Disable()
            self._motor_choice.Disable()
            self._enc_choice.Disable()
            self._btn_conn.SetLabel("Disconnect")
            self._btn_en.Enable()
            self._btn_zero.Enable()
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
        self._btn_ctrl.Disable()
        self._temp_label.SetLabel("Temp: --°C")
        self._temp_label.SetForegroundColour(wx.Colour(120, 130, 160))
        self._set_status("Disconnected", 160, 60, 60)

    # ───────────────────────────────────────────────── TPDO callbacks ───

    def _cb_p1_pos(self, _):
        try:
            with self._lock:
                self._puck1_pos = self._node1.tpdo[1]['PositionFeedback'].raw
        except Exception:
            pass
        now = time.monotonic()
        if now - self._last_draw >= 1.0 / DISPLAY_HZ:
            self._last_draw = now
            wx.CallAfter(self._refresh_canvas)
        self._ctrl_tick += 1
        if self._controlling and self._enabled and self._cs is not None:
            if self._ctrl_tick % 2 == 0:
                try:
                    self._control_step()
                except Exception:
                    self._controlling = False
                    wx.CallAfter(self._on_ctrl_stopped)

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
        self._canvas.update(pend_rad, p1, mode)

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
            n = self._node1
            self._network.sync.stop()
            time.sleep(0.04)

            n.sdo["ControlWord"].raw = CLEAR_FAULT;  time.sleep(0.05)
            n.sdo["ControlWord"].raw = SHUTDOWN;      time.sleep(0.05)
            n.sdo["ControlWord"].raw = OP_ENABLED

            n.rpdo[1]["SetModeOfOperation"].raw = MODE_CYCLIC_SYNC_TRQ
            n.rpdo[1]["ControlWord"].raw = OP_ENABLED
            n.rpdo[1].transmit()

            # Zero torque before starting SYNC — safe starting state.
            # If TargetTorque is mapped to a different RPDO on your firmware,
            # change rpdo[2] to the correct index here and in _control_loop.
            n.rpdo[1]["TargetTorque"].raw = 0
            n.rpdo[1].transmit()

            with self._lock:
                self._puck1_zero = n.sdo["PositionFeedback"].raw

            self._enabled = True
            self._btn_en.SetLabel("Disable Motor")
            self._btn_ctrl.Enable()
            self._set_status("Motor enabled  |  holding zero torque")
            self._network.sync.start(1.0 / SYNC_HZ)

        except Exception as ex:
            traceback.print_exc()
            self._set_status(f"Enable failed ({type(ex).__name__}): {ex}", 180, 0, 0)
            try:
                self._network.sync.start(1.0 / SYNC_HZ)
            except Exception:
                pass

    def _disable_motor(self, quiet=False):
        try:
            if self._node1:
                # Zero torque before switching to idle
                try:
                    self._node1.rpdo[1]["TargetTorque"].raw = 0
                    self._node1.rpdo[1].transmit()
                    time.sleep(0.02)
                except Exception:
                    pass
                self._node1.rpdo[1]["SetModeOfOperation"].raw = MODE_IDLE
                self._node1.rpdo[1]["ControlWord"].raw = SHUTDOWN
                self._node1.rpdo[1].transmit()
            time.sleep(0.05)
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
            kp      = float(self._kp.GetValue())
            ki      = float(self._ki.GetValue())
            kd      = float(self._kd.GetValue())
            adz     = float(self._adz.GetValue())
            bi      = float(self._bi.GetValue())
            ka      = float(self._ka.GetValue())
            kaf     = float(self._kaf.GetValue())
            adz_arm = float(self._adz_arm.GetValue())
            ks      = float(self._ks.GetValue())
            kb      = float(self._kb.GetValue())
            kv      = float(self._kv.GetValue())
            tmax    = float(self._tmax.GetValue())
        except ValueError:
            self._set_status("Invalid gain — use numeric values", 180, 0, 0)
            return

        tq_limit = int(abs(tmax))
        bi_rad   = math.radians(bi)
        with self._lock:
            p2_init = self._puck2_pos - self._puck2_zero
            p1_init = self._puck1_pos - self._puck1_zero
        pend_rad_init = _wrap(p2_init * 2.0 * math.pi / ENCODER_RES + math.pi) - bi_rad
        arm_rad_init  = p1_init * 2.0 * math.pi / ENCODER_RES
        self._cs = {
            'kp': kp, 'ki': ki, 'kd': kd, 'ka': ka, 'kaf': kaf,
            'swing_trq':  int(abs(ks)),
            'brake_trq':  int(abs(kb)),
            'slew_trq':   max(1, int(kv)),
            'tq_limit':   tq_limit,
            'restore_cap': int(tq_limit * ARM_RESTORE_FRAC),
            'adz_rad':    math.radians(adz),
            'bi_rad':     bi_rad,
            'adz_arm_rad': math.radians(adz_arm),
            'a_slow': min(1.0, 2 * math.pi * 5.0 / SYNC_HZ),
            'a_fast': min(1.0, 2 * math.pi * 8.0 / SYNC_HZ),
            'dt_target':  1.0 / SYNC_HZ,
            'in_balance': False, 'balance_ramp': 0.0,
            'prev_pend': pend_rad_init, 'vel_slow': 0.0, 'vel_fast': 0.0,
            'prev_arm_rad': arm_rad_init, 'arm_vel': 0.0,
            'integral': 0.0, 'prev_torque': 0,
            'prev_t': time.monotonic(),
            'min_pend_rad': math.pi, 'min_pend_de': 0.0, 'min_pend_vf': 0.0,
            'last_de': 0.0, 'diag_t': time.monotonic(),
            'kick_remaining': int(SYNC_HZ * 0.1),  # 100 ms initial kick
        }
        self._in_balance  = False
        self._in_braking  = False
        self._controlling = True
        threading.Thread(target=self._i2t_monitor_thread, daemon=True).start()
        self._btn_ctrl.SetLabel("Stop")
        for tc in (self._kp, self._ki, self._kd, self._adz, self._bi,
                   self._ka, self._kaf, self._adz_arm,
                   self._ks, self._kb, self._kv, self._tmax):
            tc.Disable()
        self._set_status("Swingup…", 200, 130, 0)

    def _stop_control(self):
        self._controlling = False
        wx.CallAfter(self._on_ctrl_stopped)

    def _on_ctrl_stopped(self):
        self._cs = None
        self._in_balance = False
        self._in_braking = False
        self._btn_ctrl.SetLabel("Start")
        for tc in (self._kp, self._ki, self._kd, self._adz, self._bi,
                   self._ka, self._kaf, self._adz_arm,
                   self._ks, self._kb, self._kv, self._tmax):
            tc.Enable()
        if self._enabled:
            try:
                self._node1.rpdo[1]["TargetTorque"].raw = 0
                self._node1.rpdo[1].transmit()
            except Exception:
                pass
            self._set_status("Motor enabled  |  controller off")

    def _control_step(self):
        """Single control iteration, called from TPDO1 callback at SYNC rate."""
        cs = self._cs
        if cs is None:
            return

        t0 = time.monotonic()
        dt = t0 - cs['prev_t']
        cs['prev_t'] = t0
        if dt <= 0:
            dt = cs['dt_target']

        with self._lock:
            p1_abs  = self._puck1_pos
            p1_zero = self._puck1_zero
            p1      = p1_abs - p1_zero
            p2      = self._puck2_pos - self._puck2_zero

        pend_rad = _wrap(p2 * 2.0 * math.pi / ENCODER_RES + math.pi) - cs['bi_rad']
        arm_rad  = p1 * 2.0 * math.pi / ENCODER_RES

        # Pendulum velocity (wrap delta to avoid ±π spike)
        delta = pend_rad - cs['prev_pend']
        if delta > math.pi:    delta -= 2 * math.pi
        elif delta < -math.pi: delta += 2 * math.pi
        raw_vel       = delta / dt
        vel_slow      = cs['a_slow'] * raw_vel + (1.0 - cs['a_slow']) * cs['vel_slow']
        vel_fast      = cs['a_fast'] * raw_vel + (1.0 - cs['a_fast']) * cs['vel_fast']
        cs['vel_slow']  = vel_slow
        cs['vel_fast']  = vel_fast
        cs['prev_pend'] = pend_rad

        # Arm angular velocity
        raw_arm_vel      = (arm_rad - cs['prev_arm_rad']) / dt
        arm_vel          = cs['a_slow'] * raw_arm_vel + (1.0 - cs['a_slow']) * cs['arm_vel']
        cs['arm_vel']      = arm_vel
        cs['prev_arm_rad'] = arm_rad

        # Arm centering
        arm_err     = arm_rad
        adz_arm_rad = cs['adz_arm_rad']
        if arm_err > adz_arm_rad:
            arm_err -= adz_arm_rad
        elif arm_err < -adz_arm_rad:
            arm_err += adz_arm_rad
        else:
            arm_err = 0.0
        restore_cap = cs['restore_cap']
        arm_restore = int(-(cs['ka'] * arm_err + cs['kaf'] * arm_vel))
        arm_restore = max(-restore_cap, min(restore_cap, arm_restore))

        in_balance  = cs['in_balance']
        tq_limit    = cs['tq_limit']
        prev_torque = cs['prev_torque']

        # Mode transitions
        if in_balance:
            if abs(pend_rad) > BALANCE_EXIT or abs(vel_fast) > BALANCE_VEL_MAX:
                in_balance     = False
                cs['integral'] = 0.0
                wx.CallAfter(self._set_status, "Swingup…", 200, 130, 0)
        else:
            if abs(pend_rad) < BALANCE_ENTRY and abs(vel_fast) < BALANCE_VEL_MAX:
                in_balance = True
                wx.CallAfter(self._set_status, "Balancing…", 0, 110, 185)
        cs['in_balance'] = in_balance
        self._in_balance = in_balance

        if in_balance:
            cs['balance_ramp'] = min(1.0, cs['balance_ramp'] + dt * BALANCE_RAMP_ON_RATE)

            pend_rad_dz = pend_rad
            adz_rad     = cs['adz_rad']
            if pend_rad_dz > adz_rad:
                pend_rad_dz -= adz_rad
            elif pend_rad_dz < -adz_rad:
                pend_rad_dz += adz_rad
            else:
                pend_rad_dz = 0.0

            cs['integral'] = max(-INTEGRAL_CLAMP, min(INTEGRAL_CLAMP,
                             cs['integral'] + pend_rad_dz * dt))
            u          = cs['balance_ramp'] * (cs['kp'] * pend_rad_dz
                         + cs['ki'] * cs['integral'] + cs['kd'] * vel_fast)
            torque_cmd = int(u) + arm_restore
            self._in_braking = False

        elif abs(vel_slow) > 0.1:
            cs['balance_ramp'] = max(0.0, cs['balance_ramp'] - dt * BALANCE_RAMP_OFF_RATE)

            cos_theta  = math.cos(pend_rad)
            de         = 0.5 * vel_fast**2 - OMEGA_N_SQ * (1.0 - cos_theta)
            cs['last_de'] = de
            if abs(cos_theta) < 0.05 or abs(vel_slow) < 0.5:
                torque_cmd = arm_restore
                self._in_braking = False
            else:
                braking   = de > 0
                cap       = cs['brake_trq'] if braking else cs['swing_trq']
                direction = math.copysign(1.0, de * vel_slow * cos_theta)
                if braking:
                    amp = min(cap, int(abs(de) / OMEGA_N_SQ * cap))
                else:
                    amp = cap
                torque_cmd = int(direction * amp) + arm_restore
                self._in_braking = braking

            slew_trq   = cs['slew_trq']
            step       = max(-slew_trq, min(slew_trq, torque_cmd - prev_torque))
            torque_cmd = prev_torque + step

        else:
            cs['balance_ramp'] = max(0.0, cs['balance_ramp'] - dt * BALANCE_RAMP_OFF_RATE)
            if cs['kick_remaining'] > 0:
                # Brief initial burst to get the pendulum moving from rest
                torque_cmd = cs['swing_trq']
                cs['kick_remaining'] -= 1
            else:
                torque_cmd = arm_restore
            self._in_braking = False

        # Re-zero display position when arm crosses travel limit
        if abs(p1) > MAX_ARM_CTS:
            with self._lock:
                self._puck1_zero = self._puck1_pos

        # Arm velocity limit — override torque to brake if arm spins too fast
        if abs(arm_vel) > MAX_ARM_VEL:
            torque_cmd = int(-math.copysign(tq_limit * 0.3, arm_vel))

        # Absolute torque cap
        torque_cmd        = max(-tq_limit, min(tq_limit, torque_cmd))
        cs['prev_torque'] = torque_cmd

        # Diagnostics (i2t read by background thread — no SDO in callback)
        if not in_balance and abs(pend_rad) < cs['min_pend_rad']:
            cs['min_pend_rad'] = abs(pend_rad)
            cs['min_pend_de']  = cs['last_de']
            cs['min_pend_vf']  = vel_fast
        if t0 - cs['diag_t'] >= 2.0:
            cs['diag_t'] = t0
            print("  swingup diag: closest={:.1f}° from upright  de@top={:.2f}  vf@top={:.2f}  trq_now={}  i2t={}".format(
                math.degrees(cs['min_pend_rad']), cs['min_pend_de'],
                cs['min_pend_vf'], torque_cmd, self._last_i2t))
            cs['min_pend_rad'] = math.pi

        self._node1.rpdo[1]["TargetTorque"].raw = torque_cmd
        self._node1.rpdo[1].transmit()

    def _i2t_monitor_thread(self):
        """Background thread — reads i2t accumulator via SDO every 2 s."""
        while self._controlling and self._enabled:
            try:
                self._last_i2t = self._node1.sdo[0x3025][1].raw
            except Exception:
                pass
            time.sleep(2.0)

    # ───────────────────────────────────────────────── close ────────────

    def _on_close(self, _):
        self._disconnect()
        self.Destroy()


# ──────────────────────────────────────────────────────── helpers ──────────

def _wrap(angle):
    return (angle + math.pi) % (2 * math.pi) - math.pi


# ──────────────────────────────────────────────────────── entry point ─────

if __name__ == "__main__":
    app = wx.App()
    FurutaCSTFrame().Show()
    app.MainLoop()
