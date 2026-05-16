#!/usr/bin/env python3
"""
Furuta Pendulum — Energy Swingup + PID Balance Controller
  Puck 1 (node 1) — rotating arm motor driven via Cyclic-Sync Position mode
  Puck 2 (node 2) — passive pendulum encoder (read-only)

Three automatic modes:
  SWINGUP  — energy-based pump builds pendulum amplitude from rest
  BRAKING  — energy-based damping absorbs excess energy near upright
  BALANCE  — PID holds upright once within ±15°

  Kp  [counts/rad]       Balance: proportional angle correction.
  Ki  [counts/(rad·s)]   Balance: integral, eliminates steady-state drift.
  Kd  [counts/(rad/s)]   Balance: derivative velocity damping.
  Ks  [revolutions]      Swingup: max arm travel per cycle.
  Kb  [revolutions]      Braking: max arm travel per cycle.
  Kv  [rev/s]            Swingup/braking slew-rate limit.

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
    MODE_IDLE, MODE_CYCLIC_SYNC_POS,
    MODE_CYCLIC_SYNC_TRQ
)

ENCODER_RES     = 4096
EDS_FILE        = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'puck4.eds')
SYNC_HZ         = 500
BALANCE_ENTRY   = math.radians(15)   # engage PID inside ±15°
BALANCE_EXIT    = math.radians(25)   # disengage outside ±25°
BALANCE_VEL_MAX = 3.0                # rad/s — max velocity to engage
INTEGRAL_CLAMP  = 2048               # counts — anti-windup clamp on integral
PEND_LENGTH_M   = 0.3048             # pendulum rod length (m) — 12 inches
ARM_LENGTH_M    = 0.127              # rotating arm length (m) — 5 inches
COUPLING        = ARM_LENGTH_M / PEND_LENGTH_M   # κ = L₁/L₂
OMEGA_N_SQ      = 9.8 / PEND_LENGTH_M            # g/L₂ (rad/s)²
MAX_ARM_REV     = 3.0                # soft travel guard (revolutions)
MAX_ARM_CTS     = int(MAX_ARM_REV * ENCODER_RES)
DISPLAY_HZ      = 25


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

        self._last_draw   = 0.0
        self._connected        = False
        self._enabled          = False
        self._controlling      = False
        self._in_balance       = False
        self._in_braking       = False
        self._ctrl_thread      = None
        self._torque_limit_pct = 50.0
        self._debug_val = 0.0

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

        # Status + debug + temperature row
        ssz = wx.BoxSizer(wx.HORIZONTAL)
        self._status = wx.StaticText(root, label="Disconnected", style=wx.ALIGN_CENTER_HORIZONTAL)
        self._status.SetForegroundColour(wx.Colour(160, 60, 60))
        f = self._status.GetFont(); f.MakeBold(); self._status.SetFont(f)
        ssz.Add(self._status, 1, wx.EXPAND | wx.ALL, 8)
        self._debug_label = wx.StaticText(root, label="Debug: --", style=wx.ALIGN_CENTER_HORIZONTAL)
        self._debug_label.SetForegroundColour(wx.Colour(120, 130, 160))
        ssz.Add(self._debug_label, 1, wx.EXPAND | wx.ALL, 10)
        self._temp_label = wx.StaticText(root, label="Temp: --°C", style=wx.ALIGN_CENTER_HORIZONTAL)
        self._temp_label.SetForegroundColour(wx.Colour(120, 130, 160))
        ssz.Add(self._temp_label, 1, wx.EXPAND | wx.ALL, 10)
        vsz.Add(ssz, 0, wx.EXPAND | wx.BOTTOM, 8)

        # Canvas
        self._canvas = FurutaCanvas(root)
        vsz.Add(self._canvas, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 6)

        # Gains — two rows inside one static box
        gbx = wx.StaticBox(root, label="Controller Gains  (CSP mode)")
        outer = wx.StaticBoxSizer(gbx, wx.VERTICAL)

        def gain(sizer, label, default, tip):
            sizer.Add(wx.StaticText(root, label=label),
                      0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 8)
            tc = wx.TextCtrl(root, value=str(default), size=(62, -1))
            tc.SetToolTip(tip)
            sizer.Add(tc, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT | wx.RIGHT, 3)
            return tc

        # Row 1 — balance PID
        row1 = wx.BoxSizer(wx.HORIZONTAL)
        row1.Add(wx.StaticText(root, label="Balance:"),
                 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 6)
        self._kp = gain(row1, "Kp:", 3500, #350,
            "Proportional angle correction [counts/rad]. Too high → oscillation.")
        self._ki = gain(row1, "Ki:", 0, #5,
            "Integral: eliminates steady-state drift [counts/(rad·s)]. Too high → windup.")
        self._kd = gain(row1, "Kd:", 0, #15,
            "Derivative velocity damping [counts/(rad/s)]. Too high → sluggish.")
        self._dz = gain(row1, "Dz:", 3,
            "Deadzone [deg] for pendulum angle error feedback (only PI, not D).")
        outer.Add(row1, 0, wx.EXPAND | wx.TOP | wx.BOTTOM, 3)

        # Row 2 — swingup energy gains + torque limit + button
        row2 = wx.BoxSizer(wx.HORIZONTAL)
        row2.Add(wx.StaticText(root, label="Swingup:"),
                 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 6)
        self._ks = gain(row2, "Ks:", 0.3,
            "Swingup max arm travel [revolutions]. Negate if pendulum damps instead of grows.")
        self._kb = gain(row2, "Kb:", 0.05,
            "Braking max arm travel [revolutions]. Increase if pendulum overshoots upright.")
        self._kv = gain(row2, "Kv:", 100.0,
            "Max arm velocity [rev/s] for swingup and braking (slew-rate limit).")
        row2.Add(wx.StaticText(root, label="Torque limit:"),
                 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 12)
        self._torque_limit = wx.TextCtrl(root, value="10000", size=(46, -1))
        self._torque_limit.SetToolTip(
            "Max output as % of rated torque (written to firmware via DS402 0x6073).\n"
            "50% = continuous-safe for most motors.  Lower to protect against overheating.")
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
            threading.Thread(target=self._debug_thread, daemon=True).start()

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
        self._debug_label.SetLabel("Debug: --")
        self._debug_label.SetForegroundColour(wx.Colour(120, 130, 160))
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

    # ───────────────────────────────────────────────── debug ─────────────

    def _debug_thread(self):
        while self._connected:
            try:
                colour = (120, 130, 160)
                wx.CallAfter(self._update_debug_label, self._debug_val, colour)
            except Exception:
                pass
            time.sleep(0.1)

    def _update_debug_label(self, val, colour):
        self._debug_label.SetLabel(f"Debug: {val}")
        self._debug_label.SetForegroundColour(wx.Colour(*colour))

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

            cur = n.sdo["PositionFeedback"].raw

            n.sdo["ControlWord"].raw = CLEAR_FAULT;  time.sleep(0.05)
            n.sdo["ControlWord"].raw = SHUTDOWN;      time.sleep(0.05)
            n.sdo["ControlWord"].raw = OP_ENABLED

            # Apply torque limit via DS402 0x6073 (max torque, in permil of rated).
            # The puck enforces this in firmware; the position loop cannot exceed it.
            try:
                lim_pct = max(1.0, min(100.0, float(self._torque_limit.GetValue())))
            except ValueError:
                lim_pct = 50.0
            try:
                n.sdo[0x6073].raw = int(lim_pct * 10)   # permil
                wx.CallAfter(self._torque_label.SetLabel, f"({lim_pct:.0f}% applied)")
                print(f"[enable] torque limit set to {lim_pct:.0f}% ({int(lim_pct*10)} permil)")
            except Exception as tex:
                wx.CallAfter(self._torque_label.SetLabel, "(SDO limit unsupported)")
                print(f"[enable] torque limit SDO failed: {tex}")
            self._torque_limit_pct = lim_pct

            n.rpdo[1]["SetModeOfOperation"].raw = MODE_CYCLIC_SYNC_POS
            # n.rpdo[1]["SetModeOfOperation"].raw = MODE_CYCLIC_SYNC_TRQ
            n.rpdo[1]["ControlWord"].raw = OP_ENABLED
            n.rpdo[1].transmit()

            n.rpdo[2]["TargetPosition"].raw = cur
            n.rpdo[2].transmit()

            with self._lock:
                self._puck1_zero = cur

            self._enabled = True
            self._btn_en.SetLabel("Disable Motor")
            self._btn_ctrl.Enable()
            self._set_status("Motor enabled  |  holding position")
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
            kp = float(self._kp.GetValue())
            ki = float(self._ki.GetValue())
            kd = float(self._kd.GetValue())
            dz = float(self._dz.GetValue())
            ks = float(self._ks.GetValue())
            kb = float(self._kb.GetValue())
            kv = float(self._kv.GetValue())
        except ValueError:
            self._set_status("Invalid gain — use numeric values", 180, 0, 0)
            return

        self._in_balance  = False
        self._in_braking  = False
        self._controlling = True
        self._btn_ctrl.SetLabel("Stop")
        for tc in (self._kp, self._ki, self._kd, self._dz, self._ks, self._kb, self._kv):
            tc.Disable()
        self._set_status("Swingup…", 200, 130, 0)

        self._ctrl_thread = threading.Thread(
            target=self._control_loop, args=(kp, ki, kd, dz, ks, kb, kv), daemon=True)
        self._ctrl_thread.start()

    def _stop_control(self):
        self._controlling = False
        if self._ctrl_thread:
            self._ctrl_thread.join(timeout=1.5)
            self._ctrl_thread = None
        wx.CallAfter(self._on_ctrl_stopped)

    def _on_ctrl_stopped(self):
        self._in_balance = False
        self._in_braking = False
        self._btn_ctrl.SetLabel("Start")
        for tc in (self._kp, self._ki, self._kd, self._dz, self._ks, self._kb, self._kv):
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

    def _control_loop(self, kp, ki, kd, dz, ks, kb, kv):
        # ── Parameter reference ────────────────────────────────────────────
        #
        # BALANCE  (|θ| < 15°, |ω| < BALANCE_VEL_MAX)
        #   u = −(Kp·θ + Ki·∫θ dt + Kd·ω)
        #   Kp  [counts/rad]      angle correction — too high → oscillation
        #   Ki  [counts/(rad·s)]  integral drift correction — too high → windup
        #   Kd  [counts/(rad/s)]  velocity damping — too high → sluggish
        #   Integral resets to zero on every balance exit (no windup carry-over).
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
        dz_rad = math.radians(dz)

        # Position-error clamp derived from torque limit.
        # Limiting how far the commanded position can deviate from the measured
        # position caps the error the internal position controller must fight,
        # which in turn caps the demanded current.  Scaled so that at 100% the
        # arm can be commanded up to ¼ revolution ahead of its current position.
        torque_limit_pct = self._torque_limit_pct
        max_err_cts = max(1, int(torque_limit_pct / 100.0 * ENCODER_RES // 4))

        a_slow = min(1.0, 2 * math.pi * 3.0 / SYNC_HZ)  # ~3 Hz — energy direction
        a_fast = min(1.0, 2 * math.pi * 8.0 / SYNC_HZ)  # ~8 Hz — PD velocity

        in_balance   = False
        prev_pend    = 0.0
        vel_slow     = 0.0
        vel_fast     = 0.0
        prev_arm_rad = 0.0
        arm_vel      = 0.0
        integral     = 0.0
        with self._lock:
            prev_target = self._puck1_pos
        prev_t = time.monotonic()

        while self._controlling and self._enabled:
            t0 = time.monotonic()
            dt = t0 - prev_t;  prev_t = t0
            if dt <= 0:
                dt = dt_target

            with self._lock:
                p1_abs  = self._puck1_pos
                p1_zero = self._puck1_zero
                p2      = self._puck2_pos - self._puck2_zero

            pend_rad = _wrap(p2 * 2.0 * math.pi / ENCODER_RES + math.pi)
            arm_rad  = p1_abs * 2.0 * math.pi / ENCODER_RES

            # Pendulum velocity (wrap delta to avoid ±π spike)
            # TODO fix velocity transient on start up
            delta = pend_rad - prev_pend
            if delta > math.pi:    delta -= 2 * math.pi
            elif delta < -math.pi: delta += 2 * math.pi
            raw_vel  = delta / dt
            vel_slow = a_slow * raw_vel + (1.0 - a_slow) * vel_slow
            vel_fast = a_fast * raw_vel + (1.0 - a_fast) * vel_fast
            prev_pend = pend_rad

        #     # # Debugging
        #     # self._debug_val = raw_vel
        #     # self._debug_val = vel_slow
        #     # self._debug_val = vel_fast

            # Arm angular velocity (continuous — no wrapping needed)
            raw_arm_vel  = (arm_rad - prev_arm_rad) / dt
            arm_vel      = a_slow * raw_arm_vel + (1.0 - a_slow) * arm_vel
            prev_arm_rad = arm_rad

        #     # ── mode transitions ─────────────────────────────────────────
        #     if in_balance:
        #         if abs(pend_rad) > BALANCE_EXIT or abs(vel_fast) > BALANCE_VEL_MAX:
        #             in_balance = False

        #             n.rpdo[1]["SetModeOfOperation"].raw = MODE_CYCLIC_SYNC_POS
        #             n.rpdo[1]["ControlWord"].raw = OP_ENABLED
        #             n.rpdo[1].transmit()

        #             integral   = 0.0
        #             wx.CallAfter(self._set_status, "Swingup…", 200, 130, 0)
        #     else:
        #         if abs(pend_rad) < BALANCE_ENTRY and abs(vel_fast) < BALANCE_VEL_MAX:
        #             in_balance = True

        #             n.rpdo[1]["SetModeOfOperation"].raw = MODE_CYCLIC_SYNC_TRQ
        #             n.rpdo[1]["ControlWord"].raw = OP_ENABLED
        #             n.rpdo[1].transmit()

        #             wx.CallAfter(self._set_status, "Balancing…", 0, 110, 185)
        #     self._in_balance = in_balance

            # PID Inverted Inverted Balance
            # x = pend_rad
            # if x >= 0.0:
            #   x = x - math.pi
            # elif x < 0.0:
            #   x = x + math.pi
            # integral    = max(-INTEGRAL_CLAMP, min(INTEGRAL_CLAMP,
            #               integral + x * dt))
            # u           = -(kp * x + ki * integral + kd * vel_fast)
            # target      = int(p1_abs + u)

            # PID Balance
            # Add deadzone for proportional and integral
            pend_rad_dz = pend_rad
            if pend_rad_dz > dz_rad:
              pend_rad_dz = pend_rad_dz - dz_rad
            elif pend_rad_dz < -dz_rad:
              pend_rad_dz = pend_rad_dz + dz_rad
            else:
              pend_rad_dz = 0.0
            integral    = max(-INTEGRAL_CLAMP, min(INTEGRAL_CLAMP,
                          integral + pend_rad_dz * dt))
            u           = kp * pend_rad_dz + ki * integral + kd * vel_fast
            target      = int(p1_abs + u)
            if abs(pend_rad) > BALANCE_EXIT:
              target = int(p1_abs)

            self._debug_val = u


        #     if in_balance:
        #         # PID balance
        #         integral    = max(-INTEGRAL_CLAMP, min(INTEGRAL_CLAMP,
        #                       integral + pend_rad * dt))
        #         u           = -(kp * pend_rad + ki * integral + kd * vel_fast)
        #         target      = int(p1_abs + u)
                
        #         # Debugging
        #         # is_positive = u > 0
        #         # if is_positive:
        #         #   self._debug_val = 1
        #         # else:
        #         #   self._debug_val = 0
        #         self._debug_val = u

        #         # Debugging
        #         # is_max = False
        #         # if target > p1_zero + MAX_ARM_CTS:
        #         #   is_max = True
        #         # elif target < p1_zero - MAX_ARM_CTS:
        #         #   is_max = True
        #         # if is_max:
        #         #   self._debug_val = 1
        #         # else:
        #         #   self._debug_val = 0
                
        #         # target      = max(p1_zero - MAX_ARM_CTS,
        #         #               min(p1_zero + MAX_ARM_CTS, target))

        #         prev_target = target
        #         self._in_braking = False

        #     elif abs(vel_slow) > 0.1:
        #         # Proportional energy controller with Furuta centripetal correction
        #         de     = (0.5 * vel_slow**2
        #                   + 0.5 * COUPLING**2 * arm_vel**2 * math.sin(pend_rad)**2
        #                   - OMEGA_N_SQ * (1.0 - math.cos(pend_rad)))
        #         pump   = math.copysign(1.0, de * vel_slow * math.cos(pend_rad))
        #         braking = de > 0
        #         cap    = brake_cts if braking else swing_cts
        #         amp    = min(cap, int(abs(de) / OMEGA_N_SQ * cap))
        #         target = int(p1_zero + pump * amp)
        #         target = max(p1_zero - MAX_ARM_CTS,
        #                  min(p1_zero + MAX_ARM_CTS, target))
                
                
        #         # Debugging
        #         # is_slew = False
        #         # if target - prev_target > slew_cts:
        #         #   is_slew = True
        #         # elif target - prev_target < -slew_cts:
        #         #   is_slew = True
        #         # if is_slew:
        #         #   self._debug_val = 1
        #         # else:
        #         #   self._debug_val = 0
                
        #         # Slew-rate limit
        #         step   = max(-slew_cts, min(slew_cts, target - prev_target))
        #         target = prev_target + step
        #         prev_target = target
        #         self._in_braking = braking

        #         # Debugging
        #         # self._debug_val = de

        #     else:
        #         target = prev_target
        #         self._in_braking = False

        #     # Position-error clamp — software backstop regardless of mode.
        #     # Keeps commanded position within max_err_cts of current position
        #     # so the internal controller never demands more than the torque limit.
        #     err = target - p1_abs

        #     # # Debugging
        #     # is_clamp = False
        #     # if err > max_err_cts:
        #     #   is_clamp = True
        #     # elif err < -max_err_cts:
        #     #   is_clamp = True
        #     # if is_clamp:
        #     #   self._debug_val = 1
        #     # else:
        #     #   self._debug_val = 0

        #     if err > max_err_cts:
        #         target = p1_abs + max_err_cts
        #     elif err < -max_err_cts:
        #         target = p1_abs - max_err_cts


            try:
                # if in_balance:
                #   self._node1.rpdo[2]["TargetTorque"].raw = 0
                # else:
                #   self._node1.rpdo[2]["TargetPosition"].raw = target
                self._node1.rpdo[2]["TargetPosition"].raw = target
                self._node1.rpdo[2].transmit()
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
    app = wx.App()
    FurutaPIDFrame().Show()
    app.MainLoop()
