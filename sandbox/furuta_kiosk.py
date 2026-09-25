#!/usr/bin/env python3
"""
Furuta pendulum — full-screen customer kiosk for the Raspberry Pi touch display.

  python3 furuta_pendulum.py --touchscreen      (or: python3 furuta_kiosk.py)

Same controller as the engineering GUI (FurutaPIDFrame's control loop and
CAN/drive code are reused unchanged); only the screen differs:

  * Auto-connect on launch, retrying forever.  A CONNECT button appears only
    after a few failed tries, to force an immediate retry.
  * One big START / STOP button.  Gains are the v4.1 defaults, fixed and hidden.
  * STOP = zero torque, arm limp.
  * Faults (drive fault bit, dead control loop, over-temperature, lost CAN)
    stop the pendulum and show a plain-language message; START clears the
    drive fault and re-enables.
  * No exit button.  Staff: hold the Barrett logo for 5 s -> Exit / Reboot /
    Shut down.
"""

import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import kiosk_widgets as kw          # sets the Linux display env before wx
import wx

import furuta_pendulum as fp

# Node IDs the kiosk expects (Puck 1 = arm motor, Puck 2 = pendulum encoder).
# If the scan finds exactly two other IDs, the lower one is taken as the motor.
MOTOR_NODE_ID = 1
ENC_NODE_ID   = 2

RETRY_S            = 3.0     # auto-connect retry period
SHOW_CONNECT_AFTER = 3       # failed tries before the CONNECT button appears
COMM_TIMEOUT_S     = 1.5     # no TPDOs for this long = connection lost
STATUS_POLL_S      = 0.5     # StatusWord poll period while enabled
TEMP_POLL_S        = 2.0
TEMP_RESUME_C      = 70      # after an over-temp stop, START returns below this
FAULT_BIT          = 0x0008  # DS402 StatusWord: Fault
STAFF_HOLD_S       = 5.0
STAFF_MENU_TIMEOUT_S = 30    # staff menu closes itself if left open

# Palette
BG        = kw.WHITE
TEXT      = (30, 34, 48)
MUTED     = (110, 116, 135)
GREEN     = (34, 160, 84)
RED       = (205, 50, 50)
AMBER     = (215, 140, 20)
BLUE      = (30, 110, 200)
NAVY      = kw.NAVY

# Kiosk states
CONNECTING, SETTLING, READY, STARTING, RUNNING, FAULT, COOLING = (
    'connecting', 'settling', 'ready', 'starting', 'running', 'fault', 'cooling')

# Control-loop status messages (FurutaPIDFrame._control_loop) -> kiosk wording.
LOOP_PHRASES = {
    "Swingup…":   ("Swinging up…", AMBER),
    "Resting…":   ("Swinging up…", AMBER),
    "Balancing…": ("Balancing!",   GREEN),
}


class ConnectProblem(RuntimeError):
    """A connect failure with a plain-language hint for the screen."""
    def __init__(self, reason, hint):
        super().__init__(reason)
        self.hint = hint


def log(msg):
    print(f"[pendulum-kiosk {time.strftime('%H:%M:%S')}] {msg}", flush=True)


class FurutaKioskFrame(fp.FurutaPIDFrame):

    def __init__(self):
        # State used by _build_ui / hooks must exist before the base __init__.
        self._state = CONNECTING
        self._closing = False
        self._bus_gen = 0                  # bumped per connection; stale threads exit
        self._needs_reenable = False
        self._connect_failures = 0
        self._retry_now = threading.Event()
        self._connect_thread = None
        super().__init__()
        self.SetMinSize((-1, -1))

    # ───────────────────────────────────────────────── UI ───────────────

    def _build_ui(self):
        # Designed at 1280x720; scaled to the screen the desktop actually reports.
        sc = self._scale = kw.screen_scale()
        S = lambda v: max(1, int(v * sc))
        self.TEXT_WIDTH = S(370)
        root = wx.Panel(self)
        root.SetBackgroundColour(wx.Colour(*BG))
        self._root = root
        vsz = wx.BoxSizer(wx.VERTICAL)

        # Top bar: logo (5 s hold = staff menu) + status line
        top = wx.BoxSizer(wx.HORIZONTAL)
        self._logo = kw.LogoPanel(root, kw.load_logo(S(60)), align=wx.ALIGN_LEFT,
                                  hold_s=STAFF_HOLD_S, on_long_press=self._staff_menu)
        top.Add(self._logo, 0, wx.EXPAND | wx.ALL, S(18))
        top.AddStretchSpacer()
        vsz.Add(top, 0, wx.EXPAND)

        body = wx.BoxSizer(wx.HORIZONTAL)
        self._canvas = fp.FurutaCanvas(root, kiosk=True)
        body.Add(self._canvas, 1, wx.EXPAND | wx.LEFT | wx.BOTTOM, S(24))

        side = wx.BoxSizer(wx.VERTICAL)
        self._status = wx.StaticText(root, label="", style=wx.ALIGN_CENTRE_HORIZONTAL)
        self._status.SetFont(kw.px_font(S(46), bold=True))
        side.Add(self._status, 0, wx.ALIGN_CENTER | wx.TOP, S(10))
        self._hint = wx.StaticText(root, label="", style=wx.ALIGN_CENTRE_HORIZONTAL)
        self._hint.SetFont(kw.px_font(S(28)))
        self._hint.SetForegroundColour(wx.Colour(*MUTED))
        side.Add(self._hint, 0, wx.ALIGN_CENTER | wx.TOP, S(12))
        side.AddStretchSpacer()

        self._btn_main = kw.BigButton(root, "START", GREEN, self._on_main_button,
                                      size=(S(380), S(220)))
        side.Add(self._btn_main, 0, wx.EXPAND | wx.TOP, S(16))
        self._btn_connect = kw.BigButton(root, "CONNECT", BLUE, self._on_connect_button,
                                         size=(S(380), S(110)))
        side.Add(self._btn_connect, 0, wx.EXPAND | wx.TOP, S(16))
        side.AddStretchSpacer()

        body.Add(side, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, S(24))
        vsz.Add(body, 1, wx.EXPAND)
        root.SetSizer(vsz)
        self._btn_connect.Hide()
        self._set_state(CONNECTING, "Connecting…", MUTED, "Starting up the pendulum")

    def _set_state(self, state, status, colour, hint=""):
        self._state = state
        self._show_text(status, colour, hint)
        if state == RUNNING:
            self._btn_main.set("STOP", RED, True)
        else:
            self._btn_main.set("START", GREEN, state in (READY, FAULT))
        self._root.Layout()
        log(f"{state}: {status} {hint}".strip())

    def _show_text(self, status, colour, hint=None):
        self._status.SetLabel(status)
        self._status.Wrap(self.TEXT_WIDTH)
        self._status.SetForegroundColour(wx.Colour(*colour))
        if hint is not None:
            self._hint.SetLabel(hint)
            self._hint.Wrap(self.TEXT_WIDTH)
        self._root.Layout()

    def _set_status(self, msg, r=60, g=160, b=60):
        # Engineering status messages from the shared code: only the control
        # loop's mode changes are shown to customers; everything goes to the log.
        if self._state == RUNNING and msg in LOOP_PHRASES:
            text, colour = LOOP_PHRASES[msg]
            self._show_text(text, colour)
        log(f"status: {msg}")

    # ───────────────────────────────────────────────── gains ────────────

    def _read_gains(self):
        # Fixed at the v4.1 defaults; bias 0 (upright = encoder zero + π).
        return (fp.KP_DEFAULT, fp.KI_DEFAULT, fp.KD_DEFAULT, fp.ADZ_DEFAULT,
                float(fp.BI_DEFAULT),
                fp.KT_DEFAULT, fp.KF_DEFAULT, fp.PDZ_DEFAULT, fp.TMAX_DEFAULT,
                fp.KS_DEFAULT, fp.KB_DEFAULT, fp.KV_DEFAULT, fp.KDA_DEFAULT,
                fp.TORQUE_LIM_SWING_DEFAULT, fp.TORQUE_LIM_BAL_DEFAULT)

    # ───────────────────────────────────────────────── connection ───────

    def start(self):
        self.ShowFullScreen(True)
        self._start_connect_worker()

    def _start_connect_worker(self):
        if self._connect_thread and self._connect_thread.is_alive():
            self._retry_now.set()
            return
        self._connect_thread = threading.Thread(target=self._connect_worker, daemon=True)
        self._connect_thread.start()

    @staticmethod
    def _pick_port():
        ports = sorted(os.path.basename(p) for p in fp.glob.glob('/sys/class/net/can*'))
        if not ports:
            raise ConnectProblem("no can* interface",
                                 "No CAN adapter found. Is the CANable plugged in?")
        return 'can0' if 'can0' in ports else ports[0]

    @staticmethod
    def _pick_nodes(nodes):
        if MOTOR_NODE_ID in nodes and ENC_NODE_ID in nodes:
            return MOTOR_NODE_ID, ENC_NODE_ID
        if len(nodes) == 2:
            lo, hi = sorted(nodes)
            return lo, hi
        if not nodes:
            raise ConnectProblem("scan found no pucks",
                                 "No pucks are answering. Check that the pendulum is "
                                 "powered and the CAN cable is plugged in.")
        found = ", ".join(str(n) for n in sorted(nodes))
        raise ConnectProblem(
            f"expected pucks {MOTOR_NODE_ID}+{ENC_NODE_ID}, found [{found}]",
            f"Found {len(nodes)} puck{'s' if len(nodes) != 1 else ''} (ID {found}). "
            f"The pendulum needs two: the motor (ID {MOTOR_NODE_ID}) and the "
            f"encoder (ID {ENC_NODE_ID}).")

    def _connect_worker(self):
        while not self._closing and not self._connected:
            try:
                port = self._pick_port()
                motor_id, enc_id = self._pick_nodes(self._scan_nodes(port))
                self._open_bus(port, motor_id, enc_id)
                log(f"connected on {port}: motor={motor_id} encoder={enc_id}")
                wx.CallAfter(self._on_bus_up)
                return
            except ConnectProblem as ex:
                self._connect_failures += 1
                wx.CallAfter(self._on_connect_failed, self._connect_failures, str(ex), ex.hint)
            except Exception as ex:
                self._connect_failures += 1
                wx.CallAfter(self._on_connect_failed, self._connect_failures, str(ex),
                             "Check that the pendulum is powered and the CAN cable "
                             "is plugged in, then tap CONNECT.")
            self._retry_now.wait(RETRY_S)
            self._retry_now.clear()

    def _on_connect_failed(self, failures, reason, hint):
        if self._closing or self._connected:
            return
        log(f"connect attempt {failures} failed: {reason}")
        if failures >= SHOW_CONNECT_AFTER:
            self._set_state(CONNECTING, "Can't reach the pendulum", RED, hint)
            self._btn_connect.set(enabled=True)
            self._btn_connect.Show()
            self._root.Layout()

    def _on_connect_button(self):
        self._btn_connect.set(enabled=False)
        self._show_text("Connecting…", MUTED, "")
        self._start_connect_worker()

    def _on_bus_up(self):
        self._bus_gen += 1
        gen = self._bus_gen
        self._connect_failures = 0
        self._needs_reenable = False
        self._btn_connect.Hide()
        self._set_state(SETTLING, "Getting ready…", MUTED,
                        "Waiting for the pendulum to hang still")
        threading.Thread(target=self._auto_zero_thread, args=(None,), daemon=True).start()
        threading.Thread(target=self._monitor_thread, args=(gen,), daemon=True).start()

    def _on_auto_zeroed(self):
        if self._state == SETTLING:
            self._set_state(READY, "Ready", NAVY, "Tap START to begin")

    def _temp_monitor_thread(self):
        pass    # the base GUI's temperature thread is folded into _monitor_thread

    # ───────────────────────────────────────────────── health monitor ───

    def _monitor_thread(self, gen):
        """Watches for lost CAN, drive faults, a dead control loop and temperature."""
        next_status = next_temp = 0.0
        sdo_errors = 0
        while not self._closing and self._connected and gen == self._bus_gen:
            now = time.monotonic()

            # SYNC (and so the TPDOs) pauses while the drive is reconfigured
            # under _sdo_lock; only judge staleness when nobody holds it.
            if self._sdo_lock.acquire(blocking=False):
                try:
                    stale = now - min(self._last_rx) > COMM_TIMEOUT_S
                finally:
                    self._sdo_lock.release()
                if stale:
                    wx.CallAfter(self._on_comm_lost, gen, "no position updates from the pucks")
                    return

            t = self._ctrl_thread
            if self._controlling and t is not None and not t.is_alive():
                wx.CallAfter(self._kiosk_fault, "control loop stopped (CAN send failed)")

            try:
                if self._enabled and now >= next_status:
                    next_status = now + STATUS_POLL_S
                    with self._sdo_lock:
                        sw = self._node1.sdo['StatusWord'].raw
                    if sw & FAULT_BIT and self._state in (RUNNING, READY):
                        wx.CallAfter(self._kiosk_fault, f"drive fault (StatusWord 0x{sw:04X})")
                if now >= next_temp:
                    next_temp = now + TEMP_POLL_S
                    with self._sdo_lock:
                        temp_c = self._node1.sdo['Amplifier']['Temperature'].raw
                    wx.CallAfter(self._on_temp, temp_c)
                sdo_errors = 0
            except Exception as ex:
                sdo_errors += 1
                if sdo_errors >= 3:
                    wx.CallAfter(self._on_comm_lost, gen, f"SDO timeouts ({ex})")
                    return
            time.sleep(0.1)

    def _on_temp(self, temp_c):
        if self._state == COOLING:
            if temp_c <= TEMP_RESUME_C:
                self._set_state(READY, "Ready", NAVY, "Tap START to begin")
            else:
                self._show_text("Cooling down", AMBER, f"Motor at {temp_c}°C — back shortly")
        elif temp_c >= self.TEMP_SHUTDOWN_C:
            self._on_overtemp(temp_c)

    def _on_overtemp(self, temp_c):
        log(f"over-temperature {temp_c}°C")
        self._halt(disable_drive=True)
        self._set_state(COOLING, "Cooling down", AMBER,
                        f"Motor at {temp_c}°C — back shortly")

    def _kiosk_fault(self, reason):
        if self._state in (FAULT, COOLING, CONNECTING) or self._closing:
            return
        log(f"FAULT: {reason}")
        self._halt(disable_drive=False)
        self._needs_reenable = True
        self._set_state(FAULT, "Stopped", RED, "Tap START to try again")

    def _on_comm_lost(self, gen, reason):
        if gen != self._bus_gen or self._closing:
            return
        log(f"connection lost: {reason}")
        self._bus_gen += 1                      # retire this connection's threads
        self._controlling = False
        if self._ctrl_thread:
            self._ctrl_thread.join(timeout=1.5)
            self._ctrl_thread = None
        net = self._network
        self._network = self._node1 = self._node2 = None
        self._connected = self._enabled = False
        self._rpdo2_backup = None       # drive likely power-cycled; volatile mapping is gone
        if net:
            try:
                net.disconnect()
            except Exception:
                pass
        self._set_state(CONNECTING, "Reconnecting…", AMBER, "Lost contact with the pendulum")
        self._start_connect_worker()

    def _halt(self, disable_drive):
        """Stop the control loop and command zero torque; optionally disable
        the drive (SYNC is restarted so the display and watchdog keep running)."""
        self._controlling = False
        if self._ctrl_thread:
            self._ctrl_thread.join(timeout=1.5)
            self._ctrl_thread = None
        self._send_zero_torque()
        if disable_drive and self._enabled:
            with self._sdo_lock:
                self._disable_drive()
                self._restart_sync()

    def _restart_sync(self):
        """Restart SYNC after a reconfiguration and give the TPDO watchdog a
        fresh grace period.  Call with _sdo_lock held."""
        try:
            # canopen's sync.start() doesn't stop a running task -- a second
            # start would double the SYNC rate -- so always stop first.
            self._network.sync.stop()
            self._network.sync.start(1.0 / fp.SYNC_HZ)
        except Exception:
            pass
        self._last_rx = [time.monotonic()] * 2

    def _send_zero_torque(self):
        if self._enabled and self._network:
            try:
                self._network.send_message(self._node1.rpdo[2].cob_id,
                                           fp.struct.pack('<h', 0))
            except Exception:
                pass

    # ───────────────────────────────────────────────── START / STOP ─────

    def _on_main_button(self):
        if self._state == RUNNING:
            self._stop_control()            # -> _on_ctrl_stopped
        elif self._state in (READY, FAULT):
            self._set_state(STARTING, "Starting…", MUTED)
            threading.Thread(target=self._start_worker, daemon=True).start()

    def _start_worker(self):
        with self._sdo_lock:                # watchdog stands down during the re-enable
            try:
                if self._needs_reenable:
                    self._disable_drive()   # clears the faulted enable; restores RPDO2
                if not self._enabled:
                    self._enable_drive()    # CLEAR_FAULT -> enable, zero torque
                self._needs_reenable = False
                ok, err = True, None
            except Exception as ex:
                self._needs_reenable = True
                ok, err = False, str(ex)
            self._restart_sync()
        if ok:
            wx.CallAfter(self._begin_run)
        else:
            wx.CallAfter(self._start_failed, err)

    def _start_failed(self, reason):
        log(f"start failed: {reason}")
        self._set_state(FAULT, "Couldn't start", RED, "Tap START to try again")

    def _begin_run(self):
        if self._state != STARTING or self._closing:
            return
        self._launch_control(self._read_gains())
        self._set_state(RUNNING, "Swinging up…", AMBER, "Tap STOP at any time")

    def _on_ctrl_stopped(self):
        # Base _stop_control() schedules this.  STOP = zero torque, arm limp.
        self._in_balance = self._ramping_balance = self._in_braking = False
        self._send_zero_torque()
        if self._state == RUNNING:
            self._set_state(READY, "Stopped", NAVY, "Tap START to begin")

    # ───────────────────────────────────────────────── staff menu ───────

    def _staff_menu(self):
        dlg = StaffMenu(self)
        choice = dlg.ShowModal()
        dlg.Destroy()
        if choice == StaffMenu.EXIT:
            self.Close()
        elif choice in (StaffMenu.REBOOT, StaffMenu.POWEROFF):
            self._shutdown_hw()
            cmd = 'reboot' if choice == StaffMenu.REBOOT else 'poweroff'
            log(f"systemctl {cmd}")
            subprocess.Popen(['systemctl', cmd])
            self.Destroy()

    # ───────────────────────────────────────────────── close ────────────

    def _shutdown_hw(self):
        if self._closing:
            return
        self._closing = True
        self._retry_now.set()
        self._controlling = False
        if self._ctrl_thread:
            self._ctrl_thread.join(timeout=1.5)
            self._ctrl_thread = None
        self._send_zero_torque()
        if self._connected:
            self._disable_drive()
        if self._network:
            try:
                self._network.disconnect()
            except Exception:
                pass
        self._network = self._node1 = self._node2 = None
        self._connected = False

    def _on_close(self, _):
        self._shutdown_hw()
        self.Destroy()


class StaffMenu(wx.Dialog):
    """Hidden maintenance menu (5 s hold on the logo)."""

    EXIT, REBOOT, POWEROFF = 101, 102, 103

    def __init__(self, parent):
        super().__init__(parent, style=wx.BORDER_SIMPLE | wx.STAY_ON_TOP)
        self.SetBackgroundColour(wx.Colour(*BG))
        sz = wx.BoxSizer(wx.VERTICAL)
        S = lambda v: max(1, int(v * kw.screen_scale(parent)))
        title = wx.StaticText(self, label="Staff menu")
        title.SetFont(kw.px_font(S(32), bold=True))
        sz.Add(title, 0, wx.ALIGN_CENTER | wx.ALL, S(20))
        for label, colour, rc in (("Exit to desktop", NAVY, self.EXIT),
                                  ("Reboot", AMBER, self.REBOOT),
                                  ("Shut down", RED, self.POWEROFF),
                                  ("Cancel", (150, 155, 170), wx.ID_CANCEL)):
            btn = kw.BigButton(self, label, colour, lambda rc=rc: self.EndModal(rc),
                               size=(S(420), S(80)))
            sz.Add(btn, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, S(20))
        self.SetSizerAndFit(sz)
        self.CentreOnParent()
        # Don't leave the menu up for customers if staff walk away.
        self._timeout = wx.CallLater(STAFF_MENU_TIMEOUT_S * 1000,
                                     lambda: self.IsModal() and self.EndModal(wx.ID_CANCEL))

    def Destroy(self):
        self._timeout.Stop()
        return super().Destroy()


def main():
    app = wx.App()
    frame = FurutaKioskFrame()
    frame.Show()
    frame.start()
    app.MainLoop()


if __name__ == "__main__":
    main()
