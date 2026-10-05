# calibrate.py
import wx
import canopen
import time
import math
import struct
import threading
import webbrowser
import configparser
import platform
from p4core.cia402 import (
    CLEAR_FAULT, SHUTDOWN, OP_ENABLED,
    MODE_IDLE, MODE_PHASE_VOLTAGE_ANGLE, MODE_PROFILE_TRQ, MODE_PROFILE_VEL,
    MODE_PROFILE_POS,
)
from canopen.sdo import SdoAbortedError
from p4core.can_backend import sdo_contention_message
from p4core import ezero
from p4core import flash as flashp4
from ..paths import _resolve_path, FIRMWARE_DIR, CONFIG_DIR

# TODO - No active issues


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


class _PVCATorqueDialog(wx.Dialog):
    """
    PVCA torque control at ~1 kHz.

    RPDO1 (trans_type=0) applies its buffer on every SYNC.  The startup buffer
    contains ControlWord=0 (Disable Voltage), so sending SYNC without
    preparation disables the motor on every tick — regardless of what was set
    via SDO.  Attempts to disable RPDO1 via its COB-ID invalid bit are silently
    ignored by the firmware in NMT Operational state.

    Fix: pre-fill the RPDO1 CAN buffer with ControlWord=OP_ENABLED +
    ModeOfOperation=PVCA before the SYNC loop starts.  Every SYNC then
    actively keeps the drive in "Operation Enabled / PVCA" state.

    Control path: SYNC thread → TPDO1 callback (rx thread) → compute →
    RPDO3 PDO frame (Theta_e + Motor.ud, trans_type=255).
    """
    _STATUS_EVERY_N = 100

    def __init__(self, parent, node, table, table_path, mp):
        super().__init__(parent, title="PVCA Torque Control",
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self._node        = node
        self._table       = table
        self._mp          = mp
        self._torque_val  = 0.0    # GIL-safe; wx thread writes, rx thread reads
        self._sync_period = 0.001  # GIL-safe; wx thread writes, sync thread reads
        self._running     = False
        self._stop_evt    = threading.Event()
        self._sync_thread = None
        self._iter_count  = 0
        self._t_start     = 0.0
        self._prev_pos    = None   # for velocity estimation
        self._adc_was_on  = False  # restored on close
        self._tpdo1_cob   = (0x180 | node.id) & 0x7FF
        self._rpdo1_cob   = (0x200 | node.id) & 0x7FF
        self._rpdo3_cob   = (0x400 | node.id) & 0x7FF
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self._build_ui(table_path)

    def _build_ui(self, table_path):
        import os
        panel = wx.Panel(self)
        vs    = wx.BoxSizer(wx.VERTICAL)

        vs.Add(wx.StaticText(panel,
            label="Table: {} ({} entries)".format(
                os.path.basename(table_path), len(self._table))),
            0, wx.ALL, 8)

        hs = wx.BoxSizer(wx.HORIZONTAL)
        hs.Add(wx.StaticText(panel, label="Torque (mNm):"),
               0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self._torque_ctrl = wx.TextCtrl(panel, value="50", size=(80, -1))
        self._torque_ctrl.Bind(wx.EVT_TEXT, self._on_torque_text)
        hs.Add(self._torque_ctrl, 0)
        hs.AddSpacer(16)
        hs.Add(wx.StaticText(panel, label="Rate (Hz):"),
               0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self._rate_ctrl = wx.TextCtrl(panel, value="1000", size=(80, -1))
        self._rate_ctrl.Bind(wx.EVT_TEXT, self._on_rate_text)
        hs.Add(self._rate_ctrl, 0)
        vs.Add(hs, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        self._run_btn = wx.Button(panel, label="Start PVCA")
        self._run_btn.Bind(wx.EVT_BUTTON, self._toggle)
        vs.Add(self._run_btn, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        self._status = wx.StaticText(panel, label="Stopped")
        self._status.SetFont(wx.Font(9, wx.FONTFAMILY_TELETYPE,
                                     wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        vs.Add(self._status, 0, wx.ALL, 8)

        close_btn = wx.Button(panel, wx.ID_CANCEL, label="Close")
        close_btn.Bind(wx.EVT_BUTTON, self._on_close)
        vs.Add(close_btn, 0, wx.ALIGN_RIGHT | wx.ALL, 8)

        panel.SetSizer(vs)
        vs.Fit(panel)
        self.Fit()
        self.SetMinSize(self.GetSize())

    def _on_torque_text(self, _evt):
        try:
            self._torque_val = float(self._torque_ctrl.GetValue())
        except ValueError:
            pass

    def _on_rate_text(self, _evt):
        try:
            hz = float(self._rate_ctrl.GetValue())
            self._sync_period = 1.0 / hz if hz > 0 else 0.0
        except ValueError:
            pass

    def _toggle(self, _evt):
        if self._running:
            self._stop_from_ui()
        else:
            self._start()

    # ------------------------------------------------------------------
    # Control (canopen rx thread, called for every TPDO1 frame)
    # ------------------------------------------------------------------

    def _on_tpdo1(self, can_id: int, data: bytearray, timestamp: float):
        """Fires on every TPDO1 (every SYNC).  Computes and writes via SDO."""
        if not self._running or len(data) < 7:
            return
        # TPDO1: StatusWord(u16) + ModeDisplay(u8) + ActualPosition(i32)
        # user_zero=0 per device config → ActualPosition == RawPosition
        _, _, actual_pos = struct.unpack_from('<HBi', data)
        mp     = self._mp
        torque = self._torque_val   # GIL-safe float read

        # Velocity estimate from consecutive position readings (counts/sec).
        period    = self._sync_period
        prev_pos  = self._prev_pos
        vel_cts_s = (actual_pos - prev_pos) / period if prev_pos is not None else 0.0
        self._prev_pos = actual_pos

        # Predict position at the moment RPDO3 voltage is applied (~250 µs ahead).
        # This compensates for the TPDO1-receive → compute → RPDO3-send latency.
        pred_pos = actual_pos + vel_cts_s * 0.00025

        enc_idx    = int(pred_pos) % mp['enc_resolution']
        correction = self._table[enc_idx]
        corrected_pos   = pred_pos + correction
        theta_e_rotor_f = (corrected_pos - mp['e_zero']) * mp['e_polarity'] \
                          / mp['cts_per_elec'] * 65536.0
        theta_e_rotor_i = int(round(theta_e_rotor_f)) % 65536
        advance     = 16384 if torque >= 0 else -16384
        theta_e_u   = (theta_e_rotor_i + advance) % 65536
        theta_e_raw = theta_e_u if theta_e_u < 32768 else theta_e_u - 65536

        iq_ma = abs(torque) * 1000.0 / mp['Kt']
        # Back-EMF feed-forward: vq = Rt·iq + ωe·λpm
        # omega_e is signed (positive = spinning in direction of positive torque).
        omega_e = vel_cts_s / mp['cts_per_elec'] * mp['e_polarity'] * (2.0 * math.pi)
        torque_sign = 1.0 if torque >= 0 else -1.0
        vq  = iq_ma / 1000.0 * mp['Rt'] + torque_sign * omega_e * mp['lambda_pm']
        ud  = int(round(max(0.0, vq) / mp['V_bus'] * 32767))
        ud  = min(ud, int(0.85 * 32767))

        # One PDO frame — no ACK, no round-trip penalty.
        # RPDO3 is configured (in Pre-Operational) with trans_type=255 so the
        # puck applies the values immediately on receipt, not on the next SYNC.
        try:
            self._node.network.send_message(self._rpdo3_cob,
                struct.pack('<hh', theta_e_raw, ud))
        except Exception:
            return  # skip this cycle if TX is momentarily saturated

        n = self._iter_count + 1
        self._iter_count = n
        if n == 1:
            print("PVCA: first step — pos={} corr={:+d} θ_e={} ud={} "
                  "  RPDO3 cob={:#05x} data={}".format(
                actual_pos, correction, theta_e_rotor_i, ud,
                self._rpdo3_cob, struct.pack('<hh', theta_e_raw, ud).hex()))
        if n % self._STATUS_EVERY_N == 0:
            elapsed = time.monotonic() - self._t_start
            hz = n / max(elapsed, 1e-9)
            rpm = vel_cts_s / mp['enc_resolution'] * 60.0
            wx.CallAfter(self._status.SetLabel,
                "{:.0f} Hz  {:+5.0f}rpm  θ_e={:6d}  "
                "ud={:5d}  iq={:5.0f}mA".format(
                    hz, rpm, theta_e_rotor_i, ud, iq_ma))

    # ------------------------------------------------------------------
    # SYNC driver (background thread)
    # ------------------------------------------------------------------

    def _sync_loop(self):
        """Sends SYNC at the configured rate with sleep+spin timing.

        time.sleep() has ~1 ms OS granularity.  For sub-ms periods we sleep
        most of the interval then busy-wait the last 200 µs so the SYNC
        fires at the right time without accumulating timer drift.
        """
        _SPIN_S = 0.0002  # busy-wait threshold: 200 µs
        while not self._stop_evt.is_set():
            t0 = time.monotonic()
            try:
                self._node.network.send_message(0x80, bytes())
            except Exception as e:
                wx.CallAfter(self._fault_stop, "SYNC error: " + str(e))
                return
            period = self._sync_period   # GIL-safe float read
            rem = period - (time.monotonic() - t0)
            if rem > _SPIN_S:
                time.sleep(rem - _SPIN_S)
            while (time.monotonic() - t0) < period:
                pass

    # ------------------------------------------------------------------
    # Start / stop / cleanup
    # ------------------------------------------------------------------

    def _start(self):
        try:
            n = self._node

            # Stop the ADC monitor's sync producer so PVCA has exclusive SYNC control.
            # The ADC and PVCA both send SYNC frames; when both run simultaneously the
            # Puck receives interleaved SYNCs at irregular intervals and TPDO1 delivery
            # becomes unreliable.
            parent = self.GetParent()
            self._adc_was_on = getattr(parent, 'ADC_ON', False)
            if self._adc_was_on:
                parent.on_off_adc(parent)

            # Configure PDOs in NMT Pre-Operational.  Firmware silently ignores PDO
            # config writes in Operational state, so Pre-Op is required.
            n.nmt.state = 'PRE-OPERATIONAL'
            # TPDO1: explicitly enable with trans_type=0 (transmit on every SYNC).
            # Without this the Puck may not send position feedback if a previous
            # operation left TPDO1 in a different state.
            n.sdo[0x1800][2].raw = 0   # trans_type = synchronous (every SYNC)
            # RPDO1: switch to async so TorqueTarget=0 isn't slammed in on every SYNC.
            n.sdo[0x1400][2].raw = 255
            # RPDO3: map Theta_e + Motor.ud
            n.sdo[0x1402][1].raw = self._rpdo3_cob | 0x80000000  # disable while mapping
            n.sdo[0x1602][0].raw = 0                               # clear entry count
            n.sdo[0x1602][1].raw = 0x60EA0010                     # Theta_e, sub0, 16-bit
            n.sdo[0x1602][2].raw = 0x30100410                     # Motor.ud, sub4, 16-bit
            n.sdo[0x1602][0].raw = 2
            n.sdo[0x1402][2].raw = 255                            # async (apply on receipt)
            n.sdo[0x1402][1].raw = self._rpdo3_cob                # enable
            print("PVCA RPDO config readback:")
            print("  RPDO1 trans_type: {}  (expect 255)".format(n.sdo[0x1400][2].raw))
            print("  RPDO3 COB-ID:     {:#010x}  (expect {:#010x})".format(
                n.sdo[0x1402][1].raw, self._rpdo3_cob))
            print("  RPDO3 trans_type: {}  (expect 255)".format(n.sdo[0x1402][2].raw))
            print("  RPDO3 map[1]:     {:#010x}  (expect 0x60ea0010)".format(n.sdo[0x1602][1].raw))
            print("  RPDO3 map[2]:     {:#010x}  (expect 0x30100410)".format(n.sdo[0x1602][2].raw))

            n.nmt.state = 'OPERATIONAL'
            n.sdo["ControlWord"].raw = CLEAR_FAULT
            n.sdo["ControlWord"].raw = SHUTDOWN
            n.sdo["ControlWord"].raw = OP_ENABLED
            n.sdo["SetModeOfOperation"].raw = MODE_PHASE_VOLTAGE_ANGLE
            n.sdo['Theta_e'].raw = 0
            n.sdo['Motor']['ud'].raw = 0
            # Prime RPDO1 once so ControlWord=OP_ENABLED + Mode=PVCA take effect immediately.
            # RPDO1 is now trans_type=255 (async) so it is NOT re-applied on every SYNC.
            n.network.send_message(self._rpdo1_cob,
                struct.pack('<HBh', OP_ENABLED, MODE_PHASE_VOLTAGE_ANGLE, 0))
            # Pre-fill RPDO3 with safe zeros before first SYNC.
            n.network.send_message(self._rpdo3_cob, struct.pack('<hh', 0, 0))
            time.sleep(0.1)
        except Exception as e:
            wx.MessageBox("Failed to start:\n{}".format(e), "Error",
                          wx.OK | wx.ICON_ERROR)
            return

        try:
            self._torque_val = float(self._torque_ctrl.GetValue())
        except ValueError:
            self._torque_val = 0.0
        try:
            hz = float(self._rate_ctrl.GetValue())
            self._sync_period = 1.0 / hz if hz > 0 else 0.0
        except ValueError:
            self._sync_period = 0.001
        print("PVCA: target rate={:.0f} Hz".format(
            1.0 / self._sync_period if self._sync_period > 0 else float('inf')))

        self._iter_count = 0
        self._t_start    = time.monotonic()
        self._prev_pos   = None
        self._running    = True

        self._node.network.subscribe(self._tpdo1_cob, self._on_tpdo1)
        self._stop_evt.clear()
        self._sync_thread = threading.Thread(
            target=self._sync_loop, daemon=True, name="pvca-sync")
        self._sync_thread.start()

        self._run_btn.SetLabel("Stop PVCA")
        self._status.SetLabel("Running…")

    def _stop_from_ui(self):
        self._running = False
        self._stop_evt.set()
        if self._sync_thread:
            self._sync_thread.join(timeout=0.5)
        try:
            self._node.network.unsubscribe(self._tpdo1_cob, self._on_tpdo1)
        except Exception:
            pass
        self._motor_off()
        self._run_btn.SetLabel("Start PVCA")
        self._status.SetLabel("Stopped")

    def _motor_off(self):
        try:
            self._node.network.send_message(self._rpdo3_cob, struct.pack('<hh', 0, 0))
        except Exception:
            pass
        try:
            self._node.sdo['Theta_e'].raw = 0
            self._node.sdo['Motor']['ud'].raw = 0
            self._node.sdo["SetModeOfOperation"].raw = MODE_IDLE
        except Exception:
            pass

    def _fault_stop(self, msg):  # called via wx.CallAfter from any thread
        self._running = False
        self._stop_evt.set()
        self._motor_off()
        self._run_btn.SetLabel("Start PVCA")
        self._status.SetLabel("FAULT: " + msg)

    def _on_close(self, _evt):
        self._running = False
        self._stop_evt.set()
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=0.5)
        try:
            self._node.network.unsubscribe(self._tpdo1_cob, self._on_tpdo1)
        except Exception:
            pass
        self._motor_off()
        if self._adc_was_on:
            try:
                parent = self.GetParent()
                parent.on_off_adc(parent)
            except Exception:
                pass
            self._adc_was_on = False
        self.Destroy()


class calibrate():
    def _cal_fault(self, exc):
        """Shared cleanup called when an SDO or other exception aborts calibration."""
        _contention = sdo_contention_message(exc)
        if _contention:
            print("Calibration fault: {}".format(exc))
            print("  >> Possible CAN bus contention — another app may be "
                  "connected to this bus.")
        else:
            print("Calibration fault: {}".format(exc))
        try:
            self.node.sdo['Motor']['ud'].raw = 0
        except Exception:
            pass
        try:
            self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE
        except Exception:
            pass
        self.OnTaskComplete()
        self.frame_statusbar.SetStatusText("Fault — calibration stopped", 1)

        _sw   = None
        _temp = None
        _bus_v = None
        _min_v = None
        _max_v = None
        # Skip the drive diagnostics under bus contention: those SDO reads would
        # just collide on the same congested bus and add more failed traffic.
        if not _contention:
            try:
                _sw = self.node.sdo["StatusWord"].raw
            except Exception:
                pass
            try:
                _temp = self.node.sdo['Amplifier']['Temperature'].raw
            except Exception:
                pass
            try:
                _bus_v = self.node.sdo['Amplifier']['BusVoltage'].raw
            except Exception:
                pass
            try:
                _min_v = self.node.sdo['Object2384']['AmplifierMinVoltage'].raw
                _max_v = self.node.sdo['Object2384']['AmplifierMaxVoltage'].raw
            except Exception:
                pass

        if _contention:
            # Bus contention is a protocol-level abort, not a drive fault — the
            # StatusWord/voltage diagnostics below would be misleading, so lead
            # with the contention explanation instead.
            _msg = _contention
        elif _sw is not None:
            _temp_str = "{}°C".format(_temp) if _temp is not None else "N/A"
            _volt_str = ""
            if _bus_v is not None:
                _volt_str = "  BusVoltage={}".format(_bus_v)
                if _min_v is not None and _max_v is not None:
                    _volt_str += "  (min={}, max={})".format(_min_v, _max_v)
            _msg = (
                "Drive is still in fault state after CLEAR_FAULT.\n"
                "StatusWord={}  Temp={}{}\n\n"
            ).format(hex(_sw), _temp_str, _volt_str)
            if _temp is not None and _temp > 60:
                _msg += "Allow the drive to cool and retry calibration."
            else:
                _msg += "Check the power supply voltage and retry calibration."
        else:
            _msg = "Calibration fault: {}".format(exc)

        self._prompt_ok("Calibration Fault", _msg)

    def _prompt(self, title, msg):
        """YES/NO dialog — returns True to continue, False to abort.
        Subclasses (or the headless CLI adapter) override this."""
        dlg = wx.MessageDialog(None, msg, title, wx.YES_NO | wx.ICON_WARNING)
        answer = dlg.ShowModal()
        dlg.Destroy()
        return answer == wx.ID_YES

    def _prompt_ok(self, title, msg):
        """Informational OK-only dialog. Subclasses override for headless use."""
        dlg = wx.MessageDialog(None, msg, title, wx.OK | wx.ICON_ERROR)
        dlg.ShowModal()
        dlg.Destroy()

    def _fw_ver_tuple(self):
        raw = self.node.sdo['MfgSoftwareVersion'].raw
        return ((raw >> 24) & 0xFF, (raw >> 8) & 0xFFFF, raw & 0xFF)

    def _fw_at_least(self, major, minor, patch):
        return self._fw_ver_tuple() >= (major, minor, patch)

    # 0x3027 encoder compensation: sub1 = active, then per bin i: 2+3i A_s (INT16),
    # 3+3i k (UNSIGNED16), 4+3i A_c (INT16).  The bin count is whatever the firmware
    # has - v4.4 builds have 2 (ENC_COMP_BINS; bins cost ~1 us each in the PWM ISR),
    # older experimental builds had 10 - so it is read from sub0, never assumed.  Newer
    # firmware follows the bins with two UNSIGNED16 subs for the encoder's tracking filter
    # (fn Hz, zeta x1000: 0x3027,8-9 on a 2-bin build), which scale each bin with speed;
    # sub0 = 3n+1 means bins only, 3n+3 bins plus filter.  Raw SDO throughout, so an EDS
    # with a different sub count cannot get in the way.
    def _enc_comp_count(self):
        try:
            return int(self.node.sdo.upload(0x3027, 0)[0])
        except Exception:
            return 0

    def _enc_comp_bins(self):
        """Number of encoder-compensation bins the node implements (0 if it has no 0x3027)."""
        return max(0, (self._enc_comp_count() - 1) // 3)

    def _enc_filt_subs(self):
        """(fn sub, zeta sub) of the encoder-filter model, or None if the firmware has none."""
        _hi = self._enc_comp_count()
        if _hi >= 4 and (_hi - 1) % 3 == 2:
            _n = (_hi - 1) // 3
            return (2 + 3 * _n, 3 + 3 * _n)
        return None

    def _enc_comp_signed(self, sub):
        return 2 <= sub < 2 + 3 * self._enc_comp_bins() and (sub - 2) % 3 != 1

    def _enc_comp_read(self, sub):
        return int.from_bytes(self.node.sdo.upload(0x3027, sub)[:2], 'little',
                              signed=self._enc_comp_signed(sub))

    def _enc_comp_write(self, sub, value):
        self.node.sdo.download(0x3027, sub, int(value).to_bytes(2, 'little',
                                                                signed=self._enc_comp_signed(sub)))

    def _enc_comp_save(self, n_bins):
        """Persist 0x3027 sub1 .. the last sub of n_bins bins, and the filter subs if present."""
        _subs = list(range(1, 2 + 3 * n_bins)) + list(self._enc_filt_subs() or ())
        for _si in _subs:
            self.node.sdo['Save']['Single'].raw = ((0x3027 << 8) | _si)

    def _clear_offset_reg(self):
        """Zero the drive-gated iSense offset (0x3008:8 / 0x3009:8) if the firmware has it, so a cal
        MEASURES raw current. The firmware applies this offset under ANY drive -- including the cal's own
        drive -- so a stale/nonzero value self-corrupts the offset measurement AND makes the voltage ramp
        overshoot (measured current reads low -> keeps pushing ud -> actual current railed -> brownout).
        Same reason the slope (0x3008:7) is cleared before measuring. Raw SDO (bypasses EDS); the probe
        SDO-aborts on pre-v3 firmware, so this is a safe no-op there."""
        try:
            self.node.sdo.upload(0x3008, 8)          # probe: present only on v3+ firmware
        except Exception:
            return
        try:
            _z = (0).to_bytes(2, 'little', signed=True)
            self.node.sdo.download(0x3008, 8, _z)
            self.node.sdo.download(0x3009, 8, _z)
            self.node.sdo['Save']['Single'].raw = ((0x3008 << 8) | 0x08)
            self.node.sdo['Save']['Single'].raw = ((0x3009 << 8) | 0x08)
        except Exception:
            pass

    def _menu_idle_takeover(self):
        """Take the drive over cleanly at the start of a MENU-triggered (standalone) drive cal. Resets the
        choice_test selector to Idle (UI only -- the _emcy_selection guard stops SetSelection from re-firing
        select_test's SDOs) AND stops the drive over SDO (zero the command, decelerate, then idle+disable,
        covering any active mode). Otherwise a puck the user left driving via the selector keeps spinning
        into the cal, and the box still reads e.g. 'Vel (RPM)'. Safe no-op if there's no node / SDOs fail.
        Call ONLY on the standalone (calAll == False) path -- in a full cal the parent already took over."""
        try:
            self._emcy_selection = True
            self.choice_test.SetSelection(0)     # 0 = Idle
        except Exception:
            pass
        finally:
            self._emcy_selection = False
        try:
            self.node.sdo['TargetVelocity'].raw = 0
            _sleep_responsive(0.3)               # decelerate a spinning rotor before disabling
            self.node.sdo['SetModeOfOperation'].raw = MODE_IDLE
            self.node.sdo['ControlWord'].raw = SHUTDOWN
        except Exception:
            pass

    def calibrate_all_pucks(self, event):
        # Gate: a fresh firmware flash leaves stale/default config on the puck
        # (i_peak, I_cont, current-sense scaling). Calibration drives control
        # modes off that config, so it must not run until configuration has been
        # applied since the flash -- mirrors the mode-switch gate in select_test().
        if getattr(self, 'requireConfig', False):
            msg = ("Configuration is required after a firmware update.\n"
                   "Apply configuration before calibrating.\n\n"
                   "Would you like to configure the active Puck now?")
            dlg = wx.MessageDialog(None, msg, 'Warning!', wx.YES_NO | wx.ICON_WARNING)
            answer = dlg.ShowModal()
            dlg.Destroy()
            if answer == wx.ID_YES:
                self.file_to_p4(None)
            return False
        print(self.network.scanner.nodes)
        starting_id = self.getID()
        if self.check_for_node() == False:
            # print("No active puck")
            return False
        self._menu_idle_takeover()   # standalone menu entry: take the drive over cleanly before the sweep
        for i in self.network.scanner.nodes:
            print(i)
            indexID = self.network.scanner.nodes.index(i)
            self.choice_id.SetSelection(indexID) # Move to next ID for calibration
            self.select_id(None)

            # print("Running full calibration for Puck {}".format(self.getID()))
            self.calibrate_all(None)

        indexID = self.network.scanner.nodes.index(starting_id)
        self.choice_id.SetSelection(indexID) # Return to starting ID after completion
        self.select_id(None)

    def calibrate_all(self, event):  # wxGlade: wxp3_frame.<event_handler>
        # Try to add calibrate all step!
        # Backstop for the config gate: never drive control modes for calibration
        # while a firmware flash is still pending configuration (see
        # calibrate_all_pucks / select_test). Config sets the current-sense scaling
        # and limits the calibration relies on.
        if getattr(self, 'requireConfig', False):
            print("Calibration blocked: configuration required after firmware "
                  "update — apply configuration before calibrating.")
            wx.MessageBox(
                "Configuration is required after a firmware update before you can calibrate.\n\n"
                "Apply the puck configuration first, then run calibration.",
                "Configuration Required", wx.OK | wx.ICON_WARNING)
            return False
        if self.check_for_node() == False:
            # print("No active puck")
            return False
        self._menu_idle_takeover()   # standalone menu entry: take the drive over cleanly before the sequence
        print("Running full calibration for Puck {}".format(self.getID()))
        _cal_t0 = time.time()   # baseline timer: total start-to-finish for the full cal

        self.frame_statusbar.SetStatusText("Progress: 0%", 1)
        self.progress.Show()
        self.GetStatusBar().Refresh()
        self.GetStatusBar().Update()

        try:
            # Disable the Current Sense Slope correction for the whole sequence. It distorts the raw
            # alpha/beta current, so Bias/Gain/enczero MUST run without it -- a stale or garbage slope
            # otherwise poisons the Gain cal (alpha reads ~half -> "Beta Gainfactor out of bounds")
            # and the whole cal cascades. The Slope step at the end re-measures and re-stores it.
            try:
                self.node.sdo[0x3008][7].raw = 0
                self.node.sdo[0x3009][7].raw = 0
                self.node.sdo['Save']['Single'].raw = ((0x3008 << 8) | 0x07)
                self.node.sdo['Save']['Single'].raw = ((0x3009 << 8) | 0x07)
            except Exception:
                pass
            self._clear_offset_reg()   # v3+: drive-gated offset applied under drive -> clear it too so
                                       # Bias/Gain/Slope all measure RAW current (else overshoot/brownout).

            continueCal = self.test_encoder(None, True)
            self.Disable()
            if continueCal == False:
                print('Ending calibration...')
                self.OnTaskComplete()
                self.Enable()
                return

            self.UpdateUI(5)
            continueCal = self.calibrate_ibias(None, True,
                              _upd=lambda v: self.UpdateUI(5 + v * 15 // 100))
            if continueCal == False:
                print('Ending calibration...')
                self.OnTaskComplete()
                self.Enable()
                return

            self.UpdateUI(20)
            continueCal = self.calibrate_igainfactor(None, True,
                              _upd=lambda v: self.UpdateUI(20 + v * 52 // 100))
            if continueCal == False:
                print('Ending calibration...')
                self.OnTaskComplete()
                self.Enable()
                return

            self.UpdateUI(72)
            # Re-measure the Current Sense Slope we cleared at the top (Bias+Gain are fresh now).
            # ROBUSTNESS: the slope is a REFINEMENT that streams over the FastPDO path, which can hit a
            # transient SYNC/SDO comms glitch (0x05040000 / "No SDO response"). That must NOT abort the
            # whole cal -- the slope was cleared at the top, so a failure just leaves it OFF (safe). Retry
            # once (transients clear), then continue to enczero + the fold regardless.
            for _slope_try in (1, 2):
                try:
                    self.calibrate_current_slope(None, True, force_sdo=(_slope_try == 2))
                    break
                except Exception as _slope_err:
                    print("  Current Sense Slope attempt {}/2 failed: {}".format(_slope_try, _slope_err))
                    try:                                   # leave the drive SAFE before retry/continue
                        self.node.sdo['Motor']['ud'].raw = 0
                        self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE
                        self.node.sdo["ControlWord"].raw = CLEAR_FAULT
                    except Exception:
                        pass
                    if _slope_try == 2:
                        print("  Slope left OFF (not stored); continuing the calibration.")
                        self._slope_stored = False

            self.UpdateUI(85)
            self.calibrate_enczero(None, True,
                              _upd=lambda v: self.UpdateUI(85 + v * 15 // 100))

            # FINAL STEP: fold the slope cal's fitted drive-on baseline (a0/b0) into the iSense Bias so
            # the current loop sees the true current under drive — the fixed offset the slope can't
            # remove (validated: makes low-settling smooth). Runs LAST, after every Bias write, so
            # nothing wipes it. Only when the slope stored a trustworthy geared fit (skips direct-drive,
            # where a0/b0 are degenerate). enczero does not touch Bias, so the fold's base is still fresh.
            if getattr(self, '_slope_stored', False):
                self.fold_baseline_offset(None, calAll=True)
            else:
                print("Baseline fold skipped (slope not stored — direct-drive / untrustworthy fit).")

            self.OnTaskComplete()
            self.requireCal = False
        except Exception as e:
            self._cal_fault(e)
        finally:
            print("Full calibration finished in {:.1f} s.".format(time.time() - _cal_t0))
            self.Enable()
        #event.Skip()

    def calibrate_quick_all_pucks(self, event):
        # QUICK cal across every scanned puck — mirrors calibrate_all_pucks but drives the fast
        # calibrate_quick per node instead of the Thorough calibrate_all.
        if getattr(self, 'requireConfig', False):
            msg = ("Configuration is required after a firmware update.\n"
                   "Apply configuration before calibrating.\n\n"
                   "Would you like to configure the active Puck now?")
            dlg = wx.MessageDialog(None, msg, 'Warning!', wx.YES_NO | wx.ICON_WARNING)
            answer = dlg.ShowModal()
            dlg.Destroy()
            if answer == wx.ID_YES:
                self.file_to_p4(None)
            return False
        print(self.network.scanner.nodes)
        starting_id = self.getID()
        if self.check_for_node() == False:
            return False
        self._menu_idle_takeover()   # standalone menu entry: take the drive over cleanly before the sweep
        for i in self.network.scanner.nodes:
            print(i)
            indexID = self.network.scanner.nodes.index(i)
            self.choice_id.SetSelection(indexID)   # Move to next ID for calibration
            self.select_id(None)
            self.calibrate_quick(None)

        indexID = self.network.scanner.nodes.index(starting_id)
        self.choice_id.SetSelection(indexID)       # Return to starting ID after completion
        self.select_id(None)

    def calibrate_quick(self, event):
        # QUICK calibration (~25 s/puck vs the Thorough calibrate_all's ~35 s, measured on a P4-16).
        #
        # Same OD writes / same registers / same values as calibrate_all — the sole intended
        # accuracy trade is the coarser enczero (calibrate_enczero(quick=True)): 8 steps per
        # electrical cycle instead of 16 over the same revolution sweep, within ~1 deg on a P4-42.
        # Everything else runs the identical Thorough step so Quick and Thorough
        # produce equivalent stored cal within tolerance.
        #
        # FOLDS EVALUATED BUT NOT APPLIED (correctness over speed, per design):
        #   * Shared drive session across ibias/gain/slope: each step function independently arms
        #     (CLEAR_FAULT/SHUTDOWN/OP_ENABLED) and drops to MODE_IDLE, and interleaves mode-specific
        #     ramps/settles. Threading one energised session through them safely needs an invasive
        #     refactor of the Thorough-path functions (untestable here) — the fold was NOT safe, so
        #     the steps are called as-is (identical OD writes, drive re-armed per step).
        #   * Gain folded into the slope's top level: the gain fit REQUIRES gainfactor=4096 (unity)
        #     and reads Alpha/Beta fundamentals, while the slope step REQUIRES the calibrated
        #     gainfactor already in effect and reads id/iq offsets at detented angles — conflicting
        #     preconditions. Sharing one rotation would corrupt BOTH cals (worst case: resets gain to
        #     unity). NOT safe to fold without firmware-internal confirmation + hardware validation,
        #     so gain and slope run as separate Thorough steps.
        # The realized Quick speedup comes from three SAFE, quick-gated trims (Thorough untouched):
        #   * calibrate_current_slope(quick=True): the 18.5 s sweep drops 7->4 (weighted-low) current
        #     levels — the dominant saving (~8 s). Same writes/model/gates; per-level offset quality
        #     (N angles, M samples, fwd+rev hysteresis cancel) is identical, only the level COUNT falls.
        #   * calibrate_ibias(quick=True): the pre-average iSense settle 0.5 s -> 0.35 s (~0.15 s).
        #   * calibrate_enczero(quick=True): coarser spiral-gated spin (auto-falls-back to the fine sweep).
        if getattr(self, 'requireConfig', False):
            print("Calibration blocked: configuration required after firmware "
                  "update — apply configuration before calibrating.")
            wx.MessageBox(
                "Configuration is required after a firmware update before you can calibrate.\n\n"
                "Apply the puck configuration first, then run calibration.",
                "Configuration Required", wx.OK | wx.ICON_WARNING)
            return False
        if self.check_for_node() == False:
            return False
        self._menu_idle_takeover()   # standalone menu entry: take the drive over cleanly before the sequence
        print("Running QUICK calibration for Puck {}".format(self.getID()))
        _cal_t0 = time.time()

        self.frame_statusbar.SetStatusText("Progress: 0%", 1)
        self.progress.Show()
        self.GetStatusBar().Refresh()
        self.GetStatusBar().Update()

        try:
            # Clear the Current Sense Slope for the whole sequence (same as calibrate_all): a stale
            # slope distorts raw alpha/beta and poisons Bias/Gain. The Slope step re-measures it.
            try:
                self.node.sdo[0x3008][7].raw = 0
                self.node.sdo[0x3009][7].raw = 0
                self.node.sdo['Save']['Single'].raw = ((0x3008 << 8) | 0x07)
                self.node.sdo['Save']['Single'].raw = ((0x3009 << 8) | 0x07)
            except Exception:
                pass
            self._clear_offset_reg()   # v3+: clear drive-gated offset so Bias/Gain/Slope measure RAW current

            continueCal = self.test_encoder(None, True)
            self.Disable()
            if continueCal == False:
                print('Ending calibration...')
                self.OnTaskComplete()
                self.Enable()
                return

            self.UpdateUI(5)
            continueCal = self.calibrate_ibias(None, True, quick=True,
                              _upd=lambda v: self.UpdateUI(5 + v * 15 // 100))
            if continueCal == False:
                print('Ending calibration...')
                self.OnTaskComplete()
                self.Enable()
                return

            self.UpdateUI(20)
            continueCal = self.calibrate_igainfactor(None, True,
                              _upd=lambda v: self.UpdateUI(20 + v * 52 // 100))
            if continueCal == False:
                print('Ending calibration...')
                self.OnTaskComplete()
                self.Enable()
                return

            self.UpdateUI(72)
            # Current Sense Slope — same robustness (retry once, continue if it can't store).
            # quick=True trims the sweep to 4 (weighted-low) current levels; same writes/model/gates.
            for _slope_try in (1, 2):
                try:
                    self.calibrate_current_slope(None, True, force_sdo=(_slope_try == 2), quick=True)
                    break
                except Exception as _slope_err:
                    print("  Current Sense Slope attempt {}/2 failed: {}".format(_slope_try, _slope_err))
                    try:
                        self.node.sdo['Motor']['ud'].raw = 0
                        self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE
                        self.node.sdo["ControlWord"].raw = CLEAR_FAULT
                    except Exception:
                        pass
                    if _slope_try == 2:
                        print("  Slope left OFF (not stored); continuing the calibration.")
                        self._slope_stored = False

            self.UpdateUI(85)
            # QUICK enczero: spiral-gated, auto-falls-back to the fine kinetic sweep on a bad spread.
            self.calibrate_enczero(None, True, quick=True,
                              _upd=lambda v: self.UpdateUI(85 + v * 15 // 100))

            # Same final baseline fold as Thorough (only on a trustworthy geared slope fit).
            if getattr(self, '_slope_stored', False):
                self.fold_baseline_offset(None, calAll=True)
            else:
                print("Baseline fold skipped (slope not stored — direct-drive / untrustworthy fit).")

            self.OnTaskComplete()
            self.requireCal = False
        except Exception as e:
            self._cal_fault(e)
        finally:
            print("Quick calibration finished in {:.1f} s.".format(time.time() - _cal_t0))
            self.Enable()

    def calibrate_ibias(self, event, calAll=False, _upd=None, quick=False):  # wxGlade: wxp3_frame.<event_handler>
        # print("Event handler 'calibrate_ibias'")
        if calAll==False:
          if self.check_for_node() == False:
            return False
          self._menu_idle_takeover()
          self.Disable()
        quick_test = self.choice_test.GetSelection()
        if quick_test != 0:
            self.lastMode = 0 # Reset lastMode
            print("Setting Mode = IDLE")
            self.choice_test.SetSelection(0)
            self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE

        if self.ADC_ON == True:
           self.on_off_adc(self)
           self.adcWasON = True
        else:
           self.adcWasON = False

        if calAll == False:
            self.OnStartTask(None)
            _upd = lambda v: self.UpdateUI(v)
        if _upd is None:
            _upd = lambda v: None

        self.frame_statusbar.SetStatusText("Calibrating ibias...", 1)
        self.frame_statusbar.Update()
        _yield()

        try:
            # Clear faults, RTSO, OpEnabled
            print("Going OpEnabled")
            self.node.sdo["ControlWord"].raw = CLEAR_FAULT
            self.node.sdo["ControlWord"].raw = SHUTDOWN
            self.node.sdo["ControlWord"].raw = OP_ENABLED

            self.node.sdo['Theta_e'].raw = 0x7FFF # Stall @ Alpha Peak (+pi)

            self.node.sdo['Motor']['ud'].raw = 0

            # Set Mode to Voltage
            print("Setting Mode = VOLTAGE MODE")
            self.node.sdo["SetModeOfOperation"].raw = MODE_PHASE_VOLTAGE_ANGLE

            # Zero uq AFTER mode switch — in profile-torque mode the PI overwrites
            # motor.uq every ISR cycle, so writing before the switch has no effect.
            # In PHASE_VOLTAGE_ANGLE mode the firmware reads uq directly as the
            # voltage reference and does not overwrite it, so this write sticks.
            self.node.sdo['Motor']['uq'].raw = 0

            # Fixed settle then high-sample-count average for sub-count bias precision.
            # Convergence polling was abandoned: this ADC's noise floor exceeds any
            # practical threshold for variance-based settling, so a fixed settle is used.
            # The mean converges much faster than the noise floor — reduce _SETTLE if
            # ibias results are consistent (5–10× the firmware filter time constant).
            _N_AVG  = 100
            # QUICK shortens the fixed pre-average settle from 0.5 s to 0.35 s. 0.5 s is 5-10x the
            # firmware filter TC (see note above); 0.35 s stays ~3.5-7x, still well past the mean's
            # convergence (the mean settles far faster than the noise floor, which the 100-sample
            # average handles). Conservative on purpose: this is the 0-A reference every downstream
            # step subtracts, so we do NOT trim it hard. Thorough (quick=False) keeps 0.5 s.
            _SETTLE = 0.35 if quick else 0.5
            print("Waiting {:.0f} ms for iSense filters to settle...".format(_SETTLE * 1000))
            _settle_end = time.time() + _SETTLE
            while time.time() < _settle_end:
                _frac = 1.0 - (_settle_end - time.time()) / _SETTLE
                _upd(int(_frac * 55))  # 0→55%
                time.sleep(0.05)
                _yield()

            # Average N_AVG fresh reads of Filtered (Q12.4); store as-is (no /16)
            _sum = {'Alpha': 0, 'Beta': 0}
            for _i in range(_N_AVG):
                _upd(55 + _i * 40 // _N_AVG)  # 55→95%
                for _ch in ['Alpha', 'Beta']:
                    _sum[_ch] += self.node.sdo[_ch]['Filtered'].raw
                _yield()

            _q12_4 = self._fw_at_least(4, 4, 0)

            if _q12_4:
                # fw >= 4.4.0: Bias register holds Q12.4 (ADC_count × 16).
                # Firmware applies: (bias_Q12_4 - raw<<4) * gainfactor >> 16.
                self._alpha_bias_f = _sum['Alpha'] / _N_AVG   # Q12.4, no /16
                self._beta_bias_f  = _sum['Beta']  / _N_AVG   # Q12.4, no /16
                _midpoint = 2048 * 16  # = 32768 in Q12.4
                _bias_scale = 16.0     # raw → counts for display
            else:
                # fw < 4.4.0: Bias register holds plain integer ADC counts (Q12.0).
                self._alpha_bias_f = _sum['Alpha'] / _N_AVG / 16.0
                self._beta_bias_f  = _sum['Beta']  / _N_AVG / 16.0
                _midpoint = 2048      # counts
                _bias_scale = 1.0

            for channel in ['Alpha', 'Beta']:
                prev = self.node.sdo[channel]['Bias'].raw
                print("Previous {0} iSense bias = {1:.3f} cts  (raw={2})".format(
                    channel, prev / _bias_scale, prev))
                if _q12_4:
                    new_bias = int(round(_sum[channel] / _N_AVG))  # Q12.4
                else:
                    new_bias = int(round(_sum[channel] / _N_AVG / 16.0))  # Q12.0
                self.node.sdo[channel]['Bias'].raw = new_bias
                print("New {0} iSense bias = {1:.3f} cts  (raw={2}, {3}-sample avg)".format(
                    channel, new_bias / _bias_scale, new_bias, _N_AVG))

            self.node.sdo['Save']['Single'].raw = ((0x3008 << 8) | 0x03) # Save Alpha iSense cal to EE
            self.node.sdo['Save']['Single'].raw = ((0x3009 << 8) | 0x03) # Save Beta iSense cal to EE

            # Bounds check: midpoint is 2048 counts (Q12.0) or 32768 (Q12.4). 5% sentinel.
            error = 0.05 # 5%

            a_bias = self.node.sdo['Alpha']['Bias'].raw
            b_bias = self.node.sdo['Beta']['Bias'].raw

            # Set Mode to Idle (0)
            print("Setting Mode = IDLE")
            self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE

            out_of_bounds = (
                a_bias > _midpoint * (1 + error) or a_bias < _midpoint * (1 - error) or
                b_bias > _midpoint * (1 + error) or b_bias < _midpoint * (1 - error)
            )
            if out_of_bounds:
                print('iSense Bias out of bounds!')
                msg = "iSense Bias out of bounds!" \
                "\n\nAlpha Bias: {:.3f} cts" \
                "\nBeta Bias: {:.3f} cts" \
                "\nAcceptable Range: {:.1f} - {:.1f} cts" \
                "\n\nDebugging steps:" \
                "\n- Ensure proper configuration file has been loaded" \
                "\n- Verify phase leads are properly connected" \
                "\n\nWould you like to continue calibration?".format(
                    a_bias / _bias_scale, b_bias / _bias_scale,
                    _midpoint*(1-error) / _bias_scale, _midpoint*(1+error) / _bias_scale)

            _upd(100)
            if self.ADC_ON == False and self.adcWasON == True:
                self.on_off_adc(self)
            if calAll == False:
                self.OnTaskComplete()
                self.Enable()
            if out_of_bounds:
                return self._prompt('Warning!', msg)
            return True

        except Exception as _exc:
            if calAll:
                raise
            self._cal_fault(_exc)
            self.Enable()

    def calibrate_igainfactor(self, event, calAll=False, _upd=None):  # wxGlade: wxp3_frame.<event_handler>
        # print("Event handler 'calibrate_igainfactor'")
        if calAll==False:
          if self.check_for_node() == False: #len(self.network.scanner.nodes) == 0:
            return False
          self._menu_idle_takeover()
          self.Disable()
        quick_test = self.choice_test.GetSelection()
        if quick_test != 0:
            self.lastMode = 0 # Reset lastMode
            print("Setting Mode = IDLE")
            self.choice_test.SetSelection(0)
            self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE

        if self.ADC_ON == True:
           self.on_off_adc(self)
           self.adcWasON = True
        else:
           self.adcWasON = False

        if calAll == False:
            self.OnStartTask(None)
            _upd = lambda v: self.UpdateUI(v)
        if _upd is None:
            _upd = lambda v: None

        self.frame_statusbar.SetStatusText("Calibrating igainfactor...", 1)
        self.frame_statusbar.Update()
        _yield()

        try:
            # Set Alpha & Beta gainfactors to 1.0 in Q4.12 (keep the pre-cal values to restore on reject)
            _orig_a_gf = self.node.sdo['Alpha']['Gainfactor'].raw
            _orig_b_gf = self.node.sdo['Beta']['Gainfactor'].raw
            self.node.sdo['Alpha']['Gainfactor'].raw = 4096
            self.node.sdo['Beta']['Gainfactor'].raw = 4096

            # Clear faults, RTSO, OpEnabled
            print("Going OpEnabled")
            self.node.sdo["ControlWord"].raw = CLEAR_FAULT
            self.node.sdo["ControlWord"].raw = SHUTDOWN
            self.node.sdo["ControlWord"].raw = OP_ENABLED

            # Set Mode to PhaseVoltageAngle (12)
            print("Setting Mode = VOLTAGE")
            self.node.sdo["SetModeOfOperation"].raw = MODE_PHASE_VOLTAGE_ANGLE

            # Zero uq after mode switch so firmware stall/drag path is active
            # (firmware checks uq==0 before entering D-axis stall block at theta_e overwrite).
            self.node.sdo['Motor']['uq'].raw = 0

            # Write theta_e, ud, StatsMode, vel
            # theta_e is 16-bit signed from -pi to +pi
            self.node.sdo['Theta_e'].raw = 0x7FFF # Stall @ Alpha Peak (+pi)

            # Read this motor's calibration current (mA)
            calibration_current = self.node.sdo['Calibration']['i_cal'].raw

            # Read the motor.peak (mA)
            i_peak = self.node.sdo['Calibration']['i_peak'].raw

            # If calibration current is greater than i_peak, limit
            if calibration_current > i_peak:
                calibration_current = i_peak

            _sleep_responsive(1) # Wait at least 75 ms for the filters to settle

            # Increase Motor d-axis voltage (/1000 of i_peak)
            # until measured d-axis current > calibration_current mA or ud > 32000
            motor_ud = 0
            motor_id = self.node.sdo['Motor']['id'].raw
            while (motor_id < 1000 and self.node.sdo['Motor']['id'].raw / 1000.0 * i_peak) < calibration_current and motor_ud < 32000:
                print("alpha = {0}, beta = {1}, id = {2}, iq = {3}, ud = {4}".format(
                    self.node.sdo['Alpha']['Raw'].raw,
                    self.node.sdo['Beta']['Raw'].raw,
                    round(self.node.sdo['Motor']['id'].raw / 1000.0 * i_peak, 2),
                    self.node.sdo['CurrentFeedback'].raw / 1000.0 * i_peak,
                    self.node.sdo['Motor']['ud'].raw))
                _id_now = self.node.sdo['Motor']['id'].raw / 1000.0 * i_peak
                _upd(3 + int(min(1.0, max(0.0, _id_now / calibration_current)) * 27))  # 3→30%
                if motor_ud > 0 and _id_now > 0:
                    # Cap the per-step increase. A falsely-low current reading used to
                    # make this Newton step slam ud to the 32000 ceiling in ONE jump,
                    # which (a) drives a small motor into overcurrent and (b) pushes the
                    # PWM to near-full duty, where the bottom-shunt iSense can't sample.
                    # Gentle ramp also yields a usable id-vs-ud curve for diagnosis.
                    _ramp_step = max(100, min(2000,
                        int((motor_ud * calibration_current / _id_now - motor_ud) / 4)))
                else:
                    _ramp_step = max(100, 32000 // 12)
                motor_ud = min(motor_ud + _ramp_step, 32000)
                self.node.sdo['Motor']['ud'].raw = motor_ud
                time.sleep(0.05)
                _yield() # keep wx event loop alive so Windows doesn't mark the app "Not Responding"

            # --- Rotating-vector fundamental fit (drift-immune gain measurement) ---
            # The old 2-angle method stalled at Theta_e=+pi (alpha) and -pi/2 (beta); if the
            # current-sense ZERO drifts between the two reads -- the alpha-channel thermal drift
            # at 100 kHz -- the alpha/beta ratio is biased -> "Beta Gainfactor out of bounds".
            # Instead ROTATE the current vector through whole electrical revolutions and fit the
            # FUNDAMENTAL amplitude of each channel:  Alpha ~ bias_a + A*cos(theta),
            # Beta ~ bias_b + B*sin(theta);  gainfactor = 4096*|A|/|B|.  The DC bias/phantom/drift
            # cancels (sum(cos)=sum(sin)=0 over whole revs) and the 2x-electrical term is
            # orthogonal to the fundamental -> immune to BOTH. Validated hot @100 kHz (stock cal
            # -> 6635 out-of-bounds; this -> 3886/3878/3889 stable). See scripts/gain_rotating.py.
            # ROTATION REDUCTION (2026-08-20): 1 whole electrical rev, densely sampled. The DC-bias/
            # drift AND 2x-electrical cancellation only need a WHOLE rev (sum cos/sin = 0), not three —
            # the extra revs were only noise-averaging. 1 rev x 48 keeps 48 samples (pucktuner's proven
            # count) so accuracy holds, cuts the rotor motion to 1/3 (3 elec rev -> 1; e.g. 14-pole:
            # ~154deg -> ~51deg mech), and is FASTER (48 vs 72 steps). Shorter sweep also accumulates
            # less thermal drift within the cal -> cleaner cancellation. Bump to 60-72 (still 1 rev) if
            # a motor ever looks noisy. See git note / firmware-state for the in-system-rotation table.
            _ROT_STEPS = 48
            _ROT_REVS  = 1
            _M   = _ROT_STEPS * _ROT_REVS
            _Ac = _As = _Bc = _Bs = 0.0
            _ids = []
            for _k in range(_M):
                _au  = (_k * 65536 // _ROT_STEPS) & 0xFFFF
                _ang = _au if _au < 32768 else _au - 65536
                self.node.sdo['Theta_e'].raw = _ang
                time.sleep(0.12)
                _th = 2.0 * math.pi * _k / _ROT_STEPS
                _a  = float(self.node.sdo['Alpha']['Filtered'].raw)
                _b  = float(self.node.sdo['Beta']['Filtered'].raw)
                _Ac += _a * math.cos(_th);  _As += _a * math.sin(_th)
                _Bc += _b * math.cos(_th);  _Bs += _b * math.sin(_th)
                _ids.append(self.node.sdo['Motor']['id'].raw / 1000.0 * i_peak)
                _upd(30 + int(65 * _k / _M))   # 30 -> 95%
                _yield()
            self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE

            _Aamp   = math.hypot(_Ac, _As) * 2.0 / _M
            _Bamp   = math.hypot(_Bc, _Bs) * 2.0 / _M
            _id_med = sorted(_ids)[len(_ids) // 2] if _ids else 0.0
            print("Rotating gain fit: |A|={:.1f}  |B|={:.1f}  ({} rev x {} steps, id~{:.0f} mA)".format(
                _Aamp, _Bamp, _ROT_REVS, _ROT_STEPS, _id_med))

            # Guard: need a real, measurable fundamental on BOTH channels.
            if (_Aamp < 30.0 or _Bamp < 30.0 or _id_med < 0.3 * calibration_current):
                raise RuntimeError(
                    "iSense gain cal ABORTED: no measurable rotating current "
                    "(|A|={:.1f} |B|={:.1f} cts, id~{:.0f} mA, target {:.0f} mA). Check that the "
                    "build updates Motor.id and triggers the iSense ADC in VOLTAGE mode.".format(
                        _Aamp, _Bamp, _id_med, calibration_current))
            gainfactor = round(4096.0 * _Aamp / _Bamp)
            print("New Beta Gainfactor = {0}  (a_sens/b_sens = {1:.5f})".format(
                gainfactor, _Aamp / _Bamp))

            # Check Bounds -- VALIDATE BEFORE WRITING so a rejected value never reaches the OD.
            error = 0.10 # 10%
            out_of_bounds = gainfactor > round(4096 * (1 + error)) or gainfactor < round(4096 * (1 - error))
            if out_of_bounds:
                print('Beta Gainfactor out of bounds!')
                msg = "Beta Gainfactor out of bounds! \n\nGainfactor: {}" \
                    "\nAcceptable Range: {} - {}" \
                    "\n\nDebugging steps:" \
                    "\n- Ensure proper configuration file has been loaded" \
                    "\n- Verify phase leads are properly connected" \
                    "\n\nWould you like to continue calibration?".format(
                        gainfactor, round(4096*(1-error)), round(4096*(1+error)))
                if not self._prompt('Warning!', msg):
                    # leave the pre-cal gainfactors intact (4096 was set only to measure)
                    self.node.sdo['Alpha']['Gainfactor'].raw = _orig_a_gf
                    self.node.sdo['Beta']['Gainfactor'].raw = _orig_b_gf
                    return False

            # In-bounds (or user confirmed): NOW write to the OD and persist.
            self.node.sdo['Beta']['Gainfactor'].raw = gainfactor
            self.node.sdo['Save']['Single'].raw = ((0x3008 << 8) | 0x06) # Save Alpha gainfactor to EE
            self.node.sdo['Save']['Single'].raw = ((0x3009 << 8) | 0x06) # Save Beta gainfactor to EE

            a_gf_rb = self.node.sdo['Alpha']['Gainfactor'].raw
            b_gf_rb = self.node.sdo['Beta']['Gainfactor'].raw
            print("Readback — Alpha Gainfactor: {}  Beta Gainfactor: {}".format(a_gf_rb, b_gf_rb))
            if b_gf_rb != gainfactor:
                print("  WARNING: Beta Gainfactor readback ({}) does not match written value ({}).".format(
                    b_gf_rb, gainfactor))

            _upd(100)
            if self.ADC_ON == False and self.adcWasON == True:
                self.on_off_adc(self)
            if calAll == False:
                self.OnTaskComplete()
                self.Enable()
            return True

        except Exception as _exc:
            if calAll:
                raise
            self._cal_fault(_exc)
            self.Enable()

    def calibrate_itiming(self, event, calAll=False):  # wxGlade: wxp3_frame.<event_handler>
        # ALGORITHM OVERVIEW — RING-EDGE (ACCURACY + NOISE) MaxSettlingTime CALIBRATION
        # MaxSettlingTime (0x3001:5) is applied live by firmware on write while idle
        # (parsePwmTiming -> pwm_update_pwm_timing, stm32 app/pwm.c), so each settling step just
        # drops to IDLE, writes the value, then re-energises to sample — no per-step save or NMT
        # reset (fw >= 4.4.0).
        #
        # WHY NOT THE OLD 2×-RIPPLE KNEE.  The previous version minimised the 2×-electrical ripple of
        # |I| around a full rotation.  That number is an ALPHA/BETA channel-MATCH (ellipse-vs-circle)
        # metric — it barely moves with settling (settling shifts BOTH channels in common mode), so on
        # hardware the ripple curve was ~flat and the "knee" pick collapsed onto extremes (0/200 ns),
        # contradicting the empirically-good ~800–1000 ns.  Ripple is the wrong observable: it does
        # not see whether the ADC sampled ON the switching ring.
        #
        # THE CRITERION (new).  Find the EARLIEST settling that is safely PAST THE RING, because the
        # smallest such value is accurate + clean AND preserves the most duty/current headroom (a
        # longer settling eats the low-side conduction window and lowers max duty).  Two observables,
        # both a DIRECT function of where the sample sits on the shunt settling transient:
        #   mean|I| = ACCURACY.  Fixed ud ⇒ constant TRUE current, so the reported magnitude tracks
        #             the sample point: it DRIFTS while on the ring, FLATTENS at the settled shunt
        #             asymptote, then falls again when the sample slides past the shrinking window.
        #   cv%     = NOISE.  Std/mean of a fast read-burst at a FIXED angle.  Sampling on the fast
        #             ring scatters consecutive reads (steep dv/dt + sample jitter); once settled the
        #             burst collapses to the ADC floor.  Averaged over a few angles so an alpha/beta
        #             MEAN imbalance can't leak into the noise number.
        #
        #   1. Establish drive once: voltage-angle mode, seed+settle rotor at θ=0, ramp ud to ~i_cal.
        #   2. Sweep MaxSettlingTime over a COARSE grid (0..just-below half_period_ns); set live in
        #      IDLE, re-enter voltage-angle mode at the SAME ud each time (fixed ud ⇒ constant current).
        #   3. At each settling, take fast read-bursts at a few fixed angles → mean|I| and cv%.
        #   4. accurate = mean|I| >= 0.85×max (rejects the high-settling UNDER-READ tail);
        #      clean     = cv% within ~1.8× the noise floor (rejects the ring region);
        #      converged = |Δmean|I||/mean per step small (accuracy stopped drifting = past the ring).
        #   5. RESULT = the LOWEST settling that is accurate AND clean AND converged.  If noise never
        #      resolves, fall back to the accuracy-convergence knee; if neither resolves, keep the
        #      incumbent (never store garbage).  Also reports the duty headroom the pick leaves.
        if calAll == False:
            if self.check_for_node() == False:
                return False
            if not self._fw_at_least(4, 4, 0):
                self._prompt_ok("Firmware Too Old",
                    "ADC settling-time calibration requires firmware v4.4.0 or later.\n"
                    "Please update the firmware and try again.")
                return False
            self._menu_idle_takeover()
            self.Disable()

        quick_test = self.choice_test.GetSelection()
        if quick_test != 0:
            self.lastMode = 0
            print("Setting Mode = IDLE")
            self.choice_test.SetSelection(0)
            self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE

        # ADC-monitor gating: the async ADC monitor contends for SDO reads and corrupts the tight
        # per-angle sampling below — quiet it here and restore it at the end AND in the except.
        if self.ADC_ON == True:
            self.on_off_adc(self)
            self.adcWasON = True
        else:
            self.adcWasON = False

        self.frame_statusbar.SetStatusText("Calibrating current timing...", 1)
        self.frame_statusbar.Update()
        _yield()

        import math, os

        try:
            print("Going OpEnabled")
            self.node.sdo["ControlWord"].raw = CLEAR_FAULT
            self.node.sdo["ControlWord"].raw = SHUTDOWN
            self.node.sdo["ControlWord"].raw = OP_ENABLED
            self.node.sdo["SetModeOfOperation"].raw = MODE_PHASE_VOLTAGE_ANGLE

            i_peak = self.node.sdo['Calibration']['i_peak'].raw
            i_cal  = self.node.sdo['Calibration']['i_cal'].raw
            if i_cal > i_peak:
                i_cal = i_peak
            # WORST-CASE STIMULUS (per the canonical settling-cal): the switching RING scales with
            # current (bigger di/dt = bigger charge-kickback ring), so drive HIGH to make the ring, and
            # thus the settling edge, most pronounced.  Target ~0.85 x I_cont (max sustained) rather
            # than i_cal.  Read I_cont fresh (0x3011:8, mA) and stay under it.
            #   NOTE: with the fast PDO read + only tiny pulses per settling point, this could push to
            #   i_peak for maximum ring (brief peaks above continuous are OK) — start at 0.85 x I_cont;
            #   raise _RING_FRAC toward i_peak once the pulse duration is confirmed short/safe.
            # Ring-stimulus current. The switching ring the settling cal measures SCALES WITH the drive
            # current: the sense front-end slews toward I_true, so an early-sample error ≈ I_true·e^(-t/τ).
            # A weak ring near the noise floor is what makes the pick wander, so drive AT I_cont -- it is
            # thermally safe BY DEFINITION for the ~60 s sweep (it IS the continuous rating), and gives the
            # biggest resolvable ring within the safe envelope (I_cont ≥ i_cal always). MIN guarantees enough
            # ring on tiny motors; the absolute MAX is only a big-GEARED-motor guard (I_cont ~9 A) so we don't
            # dump pointless heat once SNR has saturated -- it is NOT meant to bind a direct-drive motor.
            _STIM_MIN_MA = 500
            _STIM_MAX_MA = 2500   # absolute ceiling ONLY (big-geared guard); the target is I_cont
            try:
                _i_cont = int.from_bytes(self.node.sdo.upload(0x3011, 8), 'little', signed=False)
            except Exception:
                _i_cont = int(getattr(self, 'i_cont', 0) or 0)
            _base = _i_cont if _i_cont > 0 else int(i_cal)
            calibration_current = min(max(_STIM_MIN_MA, int(_base)), _STIM_MAX_MA)
            calibration_current = min(calibration_current, int(i_peak))   # absolute backstop: never > i_peak
            _stim_note = ("= I_cont" if calibration_current == _i_cont else
                          ("MIN floor" if calibration_current == _STIM_MIN_MA else
                           ("MAX-capped (big-geared guard)" if calibration_current == _STIM_MAX_MA else "≤ i_peak")))
            print("  Ring stimulus: {} mA  ({}; I_cont={} mA, i_peak={} mA) -- higher current = bigger ring"
                  " = more consistent pick.".format(calibration_current, _stim_note, _i_cont, int(i_peak)))

            original_settling = self.node.sdo['Amp']['MaxSettlingTime'].raw  # ns
            freq_hz           = self.node.sdo['Amp']['Frequency'].raw
            half_period_ns    = 1_000_000_000 // (2 * max(freq_hz, 1))

            # GUARD: make the operating frequency unmissable.  The settling result is ONLY valid at
            # the frequency it was measured at — a mismatch (cal at 16 kHz, run at 80 kHz) produces
            # audible current-loop oscillation.  If this banner isn't the frequency you intend to
            # RUN at, stop and fix pwm_freq (0x3001:1) before trusting the result.
            print("=" * 70)
            print("  MaxSettlingTime cal @ PWM = {:.1f} kHz  (half-period {} ns)".format(
                freq_hz / 1000.0, half_period_ns))
            print("  Result is valid ONLY at this frequency — verify it is your RUN frequency.")
            print("=" * 70)
            _t_cal0 = time.time()   # wall-clock for the whole cal (timer printed at the end)

            alpha_bias = float(self.node.sdo['Alpha']['Bias'].raw)
            beta_bias  = float(self.node.sdo['Beta']['Bias'].raw)

            # --- current / bus / fault helpers ---
            # Drive current is gauged by MAGNITUDE √(id²+iq²), NOT id alone: with the gearbox holding
            # the rotor a commanded electrical angle puts the current on the q-axis, so id reads ~0
            # while real current flows on iq — magnitude is alignment-proof.
            def _read_imag():
                try:
                    _idv = self.node.sdo['Motor']['id'].raw / 1000.0 * i_peak
                    _iqv = self.node.sdo['CurrentFeedback'].raw / 1000.0 * i_peak
                    return (_idv * _idv + _iqv * _iqv) ** 0.5
                except Exception:
                    return None
            try:
                _bus_min = self.node.sdo['Object2384']['AmplifierMinVoltage'].raw
            except Exception:
                _bus_min = None

            def _read_bus():
                try:
                    return self.node.sdo['Amplifier']['BusVoltage'].raw
                except Exception:
                    return None

            def _read_amp_temp():
                # Temperature is logging-only (N/A fallback). At high PWM the node occasionally can't ack
                # this SDO in time while draining PDO/SYNC traffic -- a handled transient. Retry once (a few
                # ms later it answers) and quiet the canopen abort log so a benign miss doesn't spam a scary
                # ERROR line. NOT a blanket suppression -- scoped to this one benign read.
                import logging as _lg
                _cl = _lg.getLogger('canopen'); _pl = _cl.level
                try:
                    _cl.setLevel(_lg.CRITICAL)
                    for _i in range(2):
                        try:
                            return self.node.sdo['Amplifier']['Temperature'].raw
                        except Exception:
                            if _i == 0:
                                time.sleep(0.01)
                    return None
                finally:
                    _cl.setLevel(_pl)

            def _read_motor_temp():
                val = None
                try:
                    val = self.node.sdo['Motor']['Therm'].raw / 10.0
                except Exception:
                    try:
                        val = self.node.tpdo[3]['Motor.Therm'].raw / 10.0
                    except Exception:
                        return None
                return val if val is not None and val > 0 else None

            def _fmt_temp(v):
                return "{:.1f}°C".format(v) if v is not None else "N/A"

            TEMP_LIMIT_C = 85     # bail if puck OR motor reaches this (°C)

            def _restore_idle():
                """De-energise, restore the ORIGINAL settling, and drop to IDLE.  Used by every
                abort path so a failed run never leaves a changed settling behind."""
                try:
                    self.node.sdo['Motor']['ud'].raw = 0
                    self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE
                    self.node.sdo['Amp']['MaxSettlingTime'].raw = original_settling
                except Exception:
                    pass

            def _check_fault(where):
                """Actively log the puck fault state (StatusWord fault bit + bus voltage) so a
                brownout during the cal lands in THIS log.  Returns True if faulted / comms dropped."""
                try:
                    _sw = self.node.sdo["StatusWord"].raw
                except Exception:
                    print("  !! FAULT/COMMS LOST during {} — no StatusWord response "
                          "(supply brownout / puck reset likely).".format(where))
                    return True
                if _sw & 0x08:  # CiA-402 fault bit
                    print("  !! PUCK FAULT during {}: StatusWord={:#06x}  BusVoltage={}  "
                          "(undervolt limit {})".format(where, _sw, _read_bus(), _bus_min))
                    return True
                return False

            def _check_overheat(puck_c, motor_c, where):
                """De-energise and ABORT if puck or motor is at/over TEMP_LIMIT_C so we never push
                more current into an already-hot motor.  Leaves MaxSettlingTime at its original."""
                over = [(n, v) for n, v in (("puck", puck_c), ("motor", motor_c))
                        if v is not None and v >= TEMP_LIMIT_C]
                if over:
                    _restore_idle()
                    _w = ", ".join("{} {:.1f}°C".format(n, v) for n, v in over)
                    raise RuntimeError(
                        "Itiming cal ABORTED on overheat at {} ({} ≥ {}°C). De-energised and "
                        "left MaxSettlingTime unchanged ({} ns). Let it cool and retry.".format(
                            where, _w, TEMP_LIMIT_C, original_settling))

            def _wait_settled(timeout=1.5):
                # Wait for the rotor to actually STOP after a theta_e step.  If it's still moving its
                # back-EMF modulates |I| and swamps the tiny α/β imbalance we measure.  Poll the raw
                # encoder until it holds within 2 counts for ~0.2 s, or bail at timeout.
                _p0 = self.node.sdo['Encoder']['RawPosition'].raw
                _t0 = time.time(); _stable = 0
                while time.time() - _t0 < timeout:
                    time.sleep(0.06); _yield()
                    _p1 = self.node.sdo['Encoder']['RawPosition'].raw
                    if abs(_p1 - _p0) <= 2:
                        _stable += 1
                        if _stable >= 3:
                            return
                    else:
                        _stable = 0
                    _p0 = _p1

            # --- Establish drive: seed+settle rotor at θ=0, ramp ud to ~i_cal (rotor stopped) ---
            # Ramp at original_settling: the config value the gain cal just used to reach current
            # cleanly — it reads accurately on both platforms (a high settling under-reads ~3× on
            # high-PWM parts and the ramp would stall).  drive_ud (found here) is FIXED and reused at
            # every settling so the ACTUAL current stays constant across the sweep.
            _UD_CEILING = 16000   # backstop only; the ramp STOPS at target current (low-R motors halt
                                  # at ud~200-733). High-R parts (P4-16 ~ud 8000 for 300 mA) need the
                                  # headroom. Below the ~20000 ADC sample-safe limit so it can't wedge.
            # Establish + measure the fresh bias at a FIXED CLEAN settling, NOT the stored value. A
            # corrupted stored setting (e.g. 0 ns left by a prior bad run) makes id/iq UNDER-READ, so
            # the ramp can't reach the target current and ABORTS, and it biases the zero reference on the
            # ring. A mid settling reads accurately and gives a clean bias, independent of what's stored;
            # the sweep sets each settling value afterward, and drive_ud (found here) is reused at all of
            # them so the ACTUAL current is constant regardless.
            _EST_SETTLE = 450
            print("Establishing drive at {} ns (fixed clean; target |I| {} mA, bus min {})...".format(
                _EST_SETTLE, calibration_current, _bus_min))
            self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE  # idle: settling write applies live
            self.node.sdo['Amp']['MaxSettlingTime'].raw = _EST_SETTLE
            self.node.sdo["ControlWord"].raw = CLEAR_FAULT
            self.node.sdo["ControlWord"].raw = SHUTDOWN
            self.node.sdo["ControlWord"].raw = OP_ENABLED
            self.node.sdo["SetModeOfOperation"].raw = MODE_PHASE_VOLTAGE_ANGLE
            self.node.sdo['Theta_e'].raw = 0
            # FRESH zero-current bias — SELF-CONTAINED. itiming runs FIRST (before bias/gain/slope are
            # calibrated), so we can't trust the stored bias. Read raw alpha/beta at ud=0 = the sense
            # zero; subtracting it makes each per-settling reading a real current independent of every
            # other cal (this is the "V_ideal reference" the settling routine needs, measured here).
            self.node.sdo['Motor']['uq'].raw = 0
            self.node.sdo['Motor']['ud'].raw = 0
            time.sleep(0.4)                            # let the iSense settle at zero drive
            _sa0 = _sb0 = 0.0
            _NB = 160   # a stable zero reference: the offset is measured relative to it, so bias noise
            for _ in range(_NB):   # feeds straight into every per-settling offset — average it down.
                _sa0 += self.node.sdo['Alpha']['Raw'].raw   # RAW ADC (not Filtered) — see the ring
                _sb0 += self.node.sdo['Beta']['Raw'].raw
                _yield()
            alpha_bias0 = _sa0 / _NB
            beta_bias0  = _sb0 / _NB
            print("  fresh zero-current bias: A={:.1f} B={:.1f} (self-contained; RAW ADC zero)"
                  .format(alpha_bias0, beta_bias0))
            self.node.sdo['Motor']['ud'].raw = 2000   # seed to pull the rotor to θ=0 and let it stop
            _wait_settled()
            motor_ud = 2000
            _cur = _read_imag() or 0.0
            _cur_best = _cur
            _stall = 0
            while _cur < calibration_current and motor_ud < _UD_CEILING:
                _bus = _read_bus()
                if _bus_min is not None and _bus is not None and _bus < _bus_min:
                    _restore_idle()
                    raise RuntimeError(
                        "Itiming cal ABORTED: bus voltage sagged to {} (min {}) at ud={} chasing "
                        "{} mA — supply can't deliver this current.".format(
                            _bus, _bus_min, motor_ud, calibration_current))
                motor_ud = min(motor_ud + 150, _UD_CEILING)
                self.node.sdo['Motor']['ud'].raw = motor_ud
                time.sleep(0.04)
                _yield()
                if _check_fault("drive ramp (ud={})".format(motor_ud)):
                    _restore_idle()
                    raise RuntimeError("Itiming cal ABORTED: puck faulted / comms lost while "
                                       "ramping (see fault line above).")
                _cur = _read_imag() or 0.0
                # Stall guard: ud climbing but current not following (bus sag / bad tune / bad sense).
                if _cur > _cur_best + max(10.0, 2.0 * i_peak / 1000.0):
                    _cur_best = _cur; _stall = 0
                else:
                    _stall += 1
                    if _stall >= 40:
                        _restore_idle()
                        raise RuntimeError(
                            "Itiming cal ABORTED: current |I| stalled at ≈{:.0f} mA (ud={}) chasing "
                            "{} mA — drive not responding (bus sag / unstable tune / sense).".format(
                                _cur, motor_ud, calibration_current))
            _wait_settled()
            _cur = _read_imag() or 0.0
            if motor_ud >= _UD_CEILING and _cur < 0.9 * calibration_current:
                _restore_idle()
                raise RuntimeError(
                    "Itiming cal ABORTED: hit ud ceiling {} at only {:.0f} mA (target {} mA) — "
                    "can't reach target current. Check commutation/iSense cal and the supply.".format(
                        _UD_CEILING, _cur, calibration_current))
            drive_ud = motor_ud   # FIXED for every settling step (constant actual current)
            print("Drive established: ud={}  |I|≈{:.0f} mA (settled, target {} mA)".format(
                drive_ud, _cur, calibration_current))
            _check_overheat(_read_amp_temp(), _read_motor_temp(), "drive establish")

            # --- Sample-timing measurement primitive: RAW alpha/beta OFFSET (circle center) vs settling ---
            # The magnitude alone is ambiguous (ring-inflated at low settling AND current-ramping at high
            # settling — no true baseline). The RING instead shows as an OFFSET of the current-vector
            # CENTER: sweep a full circle of electrical angles and the true rotating current averages to
            # ~0, leaving the ring-induced offset. That offset is huge on the ring and FLOORS once it
            # rings out — the real, floor-able observable. We return BOTH: offset (the ring signal we
            # select on) and magnitude (the current, kept for context). Raw alpha/beta minus the fresh
            # zero-current bias => independent of the not-yet-run bias/gain/slope.
            _N_ANGLES = 6    # UNIFORM angles so the rotating current cancels in the vector mean (offset)
            _N_AVG    = 32   # average out thermal noise, keep the deterministic ring

            def _measure_point():
                # Per-angle SETTLES dominate the sweep time, so keep the angle count LEAN (6) — the
                # cleanliness comes from AVERAGING, not from more angles. PDO streams alpha/beta fast, so
                # afford 2x samples/angle (cleaner offset U) for ~free; SDO fallback stays leaner.
                # 6 angles at 60 deg = the 6 SVM commutation patterns (sector centers), where the max-duty
                # phase is extremal so the ADC sample sits CLOSEST to the switching edge = worst-case ring.
                # 6 evenly-spaced angles still cancel the fundamental for a valid circle-center OFFSET, and
                # holding each fixed while sweeping settling isolates the ring PER PATTERN (the current
                # vector is constant at a fixed angle, so any change vs settling IS the ring).
                _na = 6
                _ns = 64 if fp else _N_AVG         # samples/angle: PDO can afford more averaging
                _theta = [int(round(-32768 + i * 65536.0 / _na)) for i in range(_na)]
                _mags, _scat = [], []
                _allres = []                               # every per-sample |αβ| residual (the raw spray)
                _patt = []                                 # per-pattern (mean_a, mean_b, scatter%) this settling
                _asum = _bsum = 0.0                        # accumulate per-angle mean vectors -> OFFSET
                for _k, _th in enumerate(_theta):
                    self.frame_statusbar.SetStatusText(
                        "timing sample {}/{}".format(_k + 1, _na), 1)
                    # Theta_e set + _wait_settled are SDO — they MUST run with SYNC OFF, else they
                    # collide with continuous SYNC (0x05040000/1). On a held (geared) rotor _wait_settled
                    # returns instantly so it rarely bit; on a DIRECT-DRIVE rotor it polls RawPosition
                    # many times while the rotor detents, so the collision timed out the whole cal.
                    self.node.sdo['Theta_e'].raw = _th
                    _wait_settled()
                    if fp: fp.sync_on()                  # SYNC on ONLY for the streamed sample burst
                    _aa = _bb = 0.0; _av = []
                    for _ in range(_ns):
                        _v = fp.read() if fp else None   # fp (if enabled) streams the alphabeta_raw pair
                        if _v is not None:
                            _a = _v[0] - alpha_bias0; _b = _v[1] - beta_bias0
                        else:                            # SDO: RAW ADC alpha/beta minus the fresh bias
                            _a = self.node.sdo['Alpha']['Raw'].raw - alpha_bias0
                            _b = self.node.sdo['Beta']['Raw'].raw  - beta_bias0
                            time.sleep(0.002)
                        _aa += _a; _bb += _b
                        _av.append((_a * _a + _b * _b) ** 0.5)
                        _yield()
                    if fp: fp.sync_off()                 # SYNC off before the next angle's SDO
                    _n = len(_av) or 1
                    _am = _aa / _n; _bm = _bb / _n        # per-angle MEAN vector (α, β)
                    _mags.append((_am * _am + _bm * _bm) ** 0.5)   # per-angle current magnitude
                    _asum += _am; _bsum += _bm
                    _mu = sum(_av) / _n
                    _sd = (sum((_m - _mu) ** 2 for _m in _av) / _n) ** 0.5
                    _scat.append((_sd / _mu * 100.0) if _mu > 1.0 else 0.0)
                    _allres.extend(_m - _mu for _m in _av)   # per-sample spray around this angle's mean
                    _patt.append((_am, _bm, _scat[-1]))      # this pattern's mean vector + jitter
                _mag = sum(_mags) / len(_mags)                                   # current magnitude (ramps)
                _off = ((_asum / _na) ** 2 + (_bsum / _na) ** 2) ** 0.5   # circle center = ring
                return _mag, _off, sum(_scat) / len(_scat), _allres, _patt   # +per-pattern for the ring plot

            def _circle_residual(_patt):
                # REFERENCE-FREE ring metric. The 6 held patterns' mean vectors should lie on ONE circle
                # (radius |I|, centre = the DC offset). The ring displaces each pattern-dependently, so
                # the RMS radial residual of a least-squares (Kasa) circle fit IS the ring distortion:
                # high while the ring is present, collapsing to the ~scatter floor once it clears. Unlike
                # the offset/|αβ|, a UNIFORM window-collapse just shrinks the radius -> residual stays low,
                # so this isolates the ring from window collapse.
                _pts = [(a, b) for (a, b, _s) in (_patt or [])]
                if len(_pts) < 4:
                    return 0.0
                try:
                    import numpy as _np
                    _A = _np.array([[2.0 * a, 2.0 * b, 1.0] for (a, b) in _pts])
                    _z = _np.array([a * a + b * b for (a, b) in _pts])
                    _sol = _np.linalg.lstsq(_A, _z, rcond=None)[0]
                    _cx, _cy, _c = float(_sol[0]), float(_sol[1]), float(_sol[2])
                    _R2 = _c + _cx * _cx + _cy * _cy
                    if _R2 <= 0:
                        return 0.0
                    _R = _R2 ** 0.5
                    _res = [(((a - _cx) ** 2 + (b - _cy) ** 2) ** 0.5 - _R) for (a, b) in _pts]
                    return (sum(r * r for r in _res) / len(_res)) ** 0.5
                except Exception:
                    return 0.0

            def _reading_drift(_row_a, _row_b):
                # CONVERGENCE metric = how far the 6-pattern constellation MOVED between two settlings,
                # per 75 ns. At a fixed angle the true current is constant, so this motion is: (a) the ring
                # clearing = systematic DRIFT (high at low settling), (b) the converged plateau = near-zero
                # (ring gone, window not yet collapsed), (c) window collapse = erratic JUMPING (high again).
                # So it has a clean MINIMUM at the ring-clear convergence -- unlike the circle residual,
                # which a uniform collapse keeps shrinking. The pick is the velocity minimum.
                _pa = _row_a.get('patterns') or []
                _pb = _row_b.get('patterns') or []
                if not _pa or not _pb or len(_pa) != len(_pb):
                    return None
                _ds = max(1.0, abs(float(_row_a['settling'] - _row_b['settling'])))
                _d = sum(((_pa[k][0] - _pb[k][0]) ** 2 + (_pa[k][1] - _pb[k][1]) ** 2) ** 0.5
                         for k in range(len(_pa))) / len(_pa)
                return _d / _ds * 75.0

            # --- Settling sweep values: coarse (fast) grid 0..just-below half_period_ns ---
            # Few points keep the whole cal to a few seconds.  The pick is a KNEE in accuracy+noise,
            # robust to spacing, so 150 ns steps are plenty.  Clip below half_period so a step can
            # never land in the no-current zone (at the full half-period there is no room left for the
            # ADC sample+convert and the firmware stops generating current entirely).
            # Production grid. The 10 ns diagnostic PROVED the switching ring is invisible to any
            # open-loop held-angle mean (offset/|αβ|/scatter all smooth from 0 ns, and misleadingly
            # FAVOR lower settling — which we know fails closed-loop). So we no longer chase a ring edge:
            # we sweep to confirm the window-collapse upper bound, and the selection applies a SAFE
            # FLOORED value past the 0 ns-fail region. The baseline fold handles the DC offset.
            settle_values = list(range(0, min(451, half_period_ns), 75)) + [600, 750]
            _SETTLE_MAX  = min(1650, half_period_ns)
            settle_values = [s for s in settle_values if 0 <= s < half_period_ns]
            if 0 < original_settling < _SETTLE_MAX and original_settling not in settle_values:
                settle_values.append(int(original_settling))   # always probe the incumbent
            settle_values = sorted(set(settle_values))
            if not settle_values:
                settle_values = [0]
            print("Randomized settling sweep (de-correlates heating from settling), then thermal-detrend + "
                  "average, then pick the reading-drift minimum = convergence. Half-period {} ns."
                  .format(half_period_ns))

            temp_start  = _read_amp_temp()
            mtemp_start = _read_motor_temp()
            print("Sweep start: puck={}  motor={}".format(
                _fmt_temp(temp_start), _fmt_temp(mtemp_start)))
            _check_overheat(temp_start, mtemp_start, "start")  # don't even begin if already hot

            results = []   # list of dicts per settling: settling, meanI, offset, residual, cv

            # --- Sweep: HELD-ANGLE (stepped) --------------------------------------------------------
            # FINDING (hardware): the continuous SPIN is BLIND to the settling — |αβ| magnitude, offset
            # AND circle residual all come out FLAT across every settling, because spinning AVERAGES over
            # all PWM duties and washes out the settling effect (worst-case at a FIXED, max-duty held
            # angle; the held sweep's |αβ| window-collapse ramp + offset U DO resolve it, the spin's are
            # dead flat). Possibly compounded by the settling not applying without the per-step mode
            # transition the held path does (IDLE->PVA). So SpinScanner is right for SLOPE (wants the
            # averaged circle CENTER) but WRONG for itiming. Disabled here (the `if _used_spin:` block
            # below is thus dead, kept for reference); the shared primitive still serves the slope cal.
            _SPIN_RATE = 1.0
            _used_spin = False
            _spin = None

            if _used_spin:
                print("  SpinScanner active — continuous-circle sweep @ {:g} Hz (no per-angle settle)."
                      .format(_SPIN_RATE))
                for s_idx, t in enumerate(settle_values):
                    self.frame_statusbar.SetStatusText(
                        "Timing cal — {}/{} ({} ns)".format(s_idx + 1, len(settle_values), t), 1)
                    self.frame_statusbar.Update(); _yield()
                    self.node.sdo['Amp']['MaxSettlingTime'].raw = t      # applies live (fw>=4.4.0)
                    _readback = self.node.sdo['Amp']['MaxSettlingTime'].raw
                    _r = _spin.scan_offset(drive_ud, alpha_bias=alpha_bias0, a_sens=1.0,
                                           beta_bias=beta_bias0, b_sens=1.0,
                                           rate_hz=_SPIN_RATE, cycles=1.25, settle_s=0.3)
                    if _r is not None:
                        _oa, _ob, _mean = _r
                        _off = (_oa * _oa + _ob * _ob) ** 0.5
                        _resid = float(_spin.last_residual)
                    else:
                        _off = 0.0; _mean = 0.0; _resid = float('inf')
                    results.append({'settling': t, 'meanI': _mean, 'offset': _off,
                                    'cv': 0.0, 'residual': _resid})
                    _t_now = _read_amp_temp(); _m_now = _read_motor_temp()
                    print("  step {}/{}: {:5d} ns (rb {:5d})  |αβ|={:7.1f}  offset={:7.1f}  "
                          "resid={:.4f}  puck={} motor={}".format(
                              s_idx + 1, len(settle_values), t, _readback, _mean, _off, _resid,
                              _fmt_temp(_t_now), _fmt_temp(_m_now)))
                    _check_overheat(_t_now, _m_now, "step {}/{}".format(s_idx + 1, len(settle_values)))
                try: _spin.__exit__(None, None, None)
                except Exception: pass

            # --- Held-angle sweep (the working itiming path). PDO-stream alpha/beta so we can average
            # many samples per angle FAST -> cleaner offset U, less choppy (same FastPDO('alphabeta')
            # the slope/spin use). Falls back to SDO if PDO can't set up. ---------------------------
            fp = None
            try:
                from .fast_pdo import FastPDO
                _fp = FastPDO(self.node, pair='alphabeta_raw', sync_period_ms=0.5)
                _fp.__enter__()
                if getattr(_fp, 'ok', False):
                    fp = _fp
                    print("  PDO alpha/beta stream active @ 0.5 ms SYNC (2 kHz) — held-angle sweep.")
                else:
                    _fp.__exit__(None, None, None)
                    print("  PDO setup failed ({}); held-angle sweep on SDO.".format(
                        getattr(_fp, 'err', '?')))
            except Exception as _pe:
                print("  PDO unavailable ({}); held-angle sweep on SDO.".format(_pe))
            # === ADAPTIVE settling sweep. Climb settling watching the circle RESIDUAL (the ring); LOCK IN
            # EARLY once it has bottomed (ring cleared) so we skip the wasted high-settling window-collapse
            # tail, then FINE-sweep around the knee for accuracy. Settling can't be set live while driving,
            # so each step does a mode transition (IDLE -> write -> re-enter voltage-angle at the same ud).
            def _measure_at(t):
                # One mode-transition + measurement (no dedup, no store) -> returns the row for the caller.
                t = int(max(0, min(t, half_period_ns - 1)))
                self.frame_statusbar.SetStatusText("Timing cal — {} ns".format(t), 1)
                self.frame_statusbar.Update(); _yield()
                self.node.sdo['Motor']['ud'].raw = 0
                self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE
                self.node.sdo['Amp']['MaxSettlingTime'].raw = t
                self.node.sdo["ControlWord"].raw = CLEAR_FAULT
                self.node.sdo["ControlWord"].raw = SHUTDOWN
                self.node.sdo["ControlWord"].raw = OP_ENABLED
                self.node.sdo["SetModeOfOperation"].raw = MODE_PHASE_VOLTAGE_ANGLE
                self.node.sdo['Theta_e'].raw = 0
                self.node.sdo['Motor']['ud'].raw = drive_ud
                _wait_settled()
                # SYNC is toggled per-angle inside _measure_point, so this fault check + the mode-switch
                # SDO above all run SYNC-off (no 0x0504 collision).
                if _check_fault("sweep {} ns".format(t)):
                    if fp: fp.__exit__(None, None, None)
                    _restore_idle()
                    raise RuntimeError("Itiming cal ABORTED: puck faulted / comms lost during sweep.")
                _mean, _off, _cv, _samples, _patt = _measure_point()
                return {'settling': t, 'meanI': _mean, 'offset': _off, 'cv': _cv,
                        'samples': _samples, 'patterns': [list(p) for p in _patt]}

            # === RANDOMIZED + thermally-detrended sweep. A monotonic 0->N sweep CONFOUNDS settling with the
            # puck heating: both climb together, so the ring (a settling effect) and thermal drift (a TIME
            # effect) land on the same axis and can't be separated -- the pick slides with thermal state.
            # Fix: visit the settlings in RANDOM order over several passes, then remove the linear drift vs
            # measurement-TIME (the heating) and AVERAGE each settling. Because the order is random, that
            # time-trend is the thermal drift, NOT the ring -- so the ring (tied to settling) survives clean.
            import random
            _grid = [s for s in range(0, min(481, half_period_ns), 60)]
            _REPEATS = 3     # 3 passes = 27 measurements (~+22 s vs 2). 1 pass is NOT reproducible; the 2nd
                             #   makes the pick stable, and the 3rd tightens the per-settling means (~1.22x
                             #   lower noise) so the envelope FIT gets a cleaner cloud. 4 doubles the time
                             #   premium for only ~1.15x more -- 3 is the knee. Speed is from live-settling.
            _order = _grid * _REPEATS
            random.Random(20260709).shuffle(_order)   # fixed seed: reproducible order, still de-correlated
            print("  randomized sweep: {} settlings x {} passes = {} measurements (de-correlates heating "
                  "from settling)".format(len(_grid), _REPEATS, len(_order)))
            _raw = []
            _i2t_hi = 0.0    # track the worst i2t seen across the sweep (folding corrupts |αβ|)
            for _ti, _s in enumerate(_order):
                _r = _measure_at(_s); _r['tidx'] = _ti; _raw.append(_r)
                _t_now = _read_amp_temp(); _m_now = _read_motor_temp()
                # i2t accumulator (0x3025:1) is per-mille of the energy limit: 1000 = 100% = folding. If it
                # climbs here the limiter is folding current mid-sweep -> bimodal |αβ| -> corrupt ring. A
                # zeroed/stale slope inflates the firmware current estimate and can trip this even under i_cont.
                try:
                    _i2t_pct = self.node.sdo[0x3025][1].raw / 10.0
                    _i2t_hi = max(_i2t_hi, _i2t_pct)
                    _i2t_tag = "  i2t={:3.0f}%{}".format(_i2t_pct, "  <<< LIMITING" if _i2t_pct >= 90.0 else "")
                except Exception:
                    _i2t_tag = ""
                print("  [{:2d}/{:2d}] {:5d} ns  |αβ|={:7.1f}  offset={:6.1f}  puck={}{}".format(
                    _ti + 1, len(_order), _s, _r['meanI'], _r['offset'], _fmt_temp(_t_now), _i2t_tag))
                _check_overheat(_t_now, _m_now, "meas {}/{}".format(_ti + 1, len(_order)))
            if _i2t_hi >= 50.0:
                print("  ⚠ i2t reached {:.0f}% during the sweep — the limiter is folding current, which "
                      "corrupts the ring. Likely a stale/zeroed current-sense (inflated |I| estimate) or a "
                      "too-low i_cont vs the {} mA stimulus.".format(_i2t_hi, calibration_current))
            if fp: fp.__exit__(None, None, None)   # stop SYNC + restore callbacks after the sweep

            # THERMAL DETREND: subtract the linear trend vs measurement-time from every pattern-vector
            # component. Randomized order => that trend is the puck heating (time), not the ring (settling).
            _np6 = min((len(r['patterns']) for r in _raw), default=0)
            _tt = [r['tidx'] for r in _raw]
            _tm = sum(_tt) / len(_tt) if _tt else 0.0
            _tv = sum((t - _tm) ** 2 for t in _tt) or 1.0
            for k in range(_np6):
                for _c in (0, 1):
                    _vv = [r['patterns'][k][_c] for r in _raw]
                    _vm = sum(_vv) / len(_vv)
                    _sl = sum((_tt[i] - _tm) * (_vv[i] - _vm) for i in range(len(_raw))) / _tv
                    for i in range(len(_raw)):
                        _raw[i]['patterns'][k][_c] -= _sl * (_tt[i] - _tm)

            # AVERAGE the detrended repeats per settling -> one clean row per settling.
            results = []
            for _s in sorted(set(_grid)):
                _grp = [r for r in _raw if r['settling'] == _s]
                if not _grp:
                    continue
                _np_s = min(len(r['patterns']) for r in _grp)
                _patt_avg = [(sum(r['patterns'][k][0] for r in _grp) / len(_grp),
                              sum(r['patterns'][k][1] for r in _grp) / len(_grp),
                              sum(r['patterns'][k][2] for r in _grp) / len(_grp)) for k in range(_np_s)]
                _ca = sum(p[0] for p in _patt_avg) / len(_patt_avg)
                _cb = sum(p[1] for p in _patt_avg) / len(_patt_avg)
                results.append({'settling': _s,
                                'meanI': sum(r['meanI'] for r in _grp) / len(_grp),
                                'offset': (_ca * _ca + _cb * _cb) ** 0.5,
                                'cv': sum(r['cv'] for r in _grp) / len(_grp),
                                'samples': (_grp[0].get('samples') or []),
                                'patterns': _patt_avg,
                                'residual': _circle_residual(_patt_avg)})
            results.sort(key=lambda r: r['settling'])
            self.node.sdo['Motor']['ud'].raw = 0
            self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE

            temp_end  = _read_amp_temp()
            mtemp_end = _read_motor_temp()
            if temp_start is not None and temp_end is not None:
                print("Sweep ΔT (puck): {:.1f}°C → {:.1f}°C  (rise {:+.1f}°C)".format(
                    float(temp_start), float(temp_end), float(temp_end) - float(temp_start)))
            if mtemp_start is not None and mtemp_end is not None:
                print("Sweep ΔT (motor): {:.1f}°C → {:.1f}°C  (rise {:+.1f}°C)".format(
                    mtemp_start, mtemp_end, mtemp_end - mtemp_start))

            # --- Analysis: the CONVERGENCE POINT via reading DRIFT. The per-pattern diagnostic proved the
            # ring IS visible when you DON'T average across patterns: at a fixed angle the true current is
            # constant, so any change in the 6-pattern reading as settling increases is either the ring
            # clearing (low settling) or window collapse (high settling). The ring-clear = where the reading
            # STOPS changing = minimum settling-to-settling drift. (The circle RESIDUAL fails here: a uniform
            # collapse keeps shrinking the circle while it still FITS, so residual falls into the tail -- the
            # drift instead climbs back up when the reading destabilises, giving a true minimum at converge.)
            _srt = sorted(results, key=lambda r: r['settling'])
            n = len(_srt)
            _means = [r['meanI'] for r in _srt]
            _mag_max = max(_means) if _means else 0.0
            _MAG_OK  = 0.90
            # SAFETY FLOOR (fallback only). The drift metric measures the convergence directly; this floor
            # applies ONLY when there's no clear convergence (drift flat). Kept at 50 ns to leave room on
            # the low side. 0 ns is never picked. The baseline fold handles the DC offset.
            _FLOOR_NS = 50

            # RING vs the RING-FREE END. Thermal is now detrended out, so the only thing left that moves the
            # per-pattern reading with settling is the ring (clears as settling RISES) and window collapse
            # (drops |αβ| -- gated out). Because the ring clears as settling rises, the highest non-collapsed
            # settlings are ring-FREE: average their pattern vectors as the reference. Each settling's WORST
            # per-pattern deviation from that reference = the residual ring there. Pick the LOWEST settling
            # whose ring has fallen to the reference-region noise = ring just cleared = max headroom. (Drift
            # kept as a context column; differencing adjacent settlings amplifies noise on a weak ring.)
            def _drift_at(i):
                _S = _srt[i]['settling']
                _below = [j for j in range(n) if _srt[j]['settling'] <= _S - 45]
                if not _below:
                    return None
                _j = min(_below, key=lambda j: abs(_srt[j]['settling'] - (_S - 75)))
                return _reading_drift(_srt[i], _srt[_j])
            for i in range(n):
                _srt[i]['drift'] = _drift_at(i)

            _acc = [i for i in range(n) if _means[i] >= _MAG_OK * _mag_max]       # not window-collapsed
            _ref_idxs = sorted(_acc, key=lambda i: _srt[i]['settling'])[-3:] if _acc else []
            _np_r = min((len(_srt[i]['patterns']) for i in _ref_idxs), default=0)
            _ref = [(sum(_srt[i]['patterns'][k][0] for i in _ref_idxs) / len(_ref_idxs),
                     sum(_srt[i]['patterns'][k][1] for i in _ref_idxs) / len(_ref_idxs))
                    for k in range(_np_r)]
            def _ring_at(i):
                _p = _srt[i]['patterns']
                if _np_r == 0 or len(_p) < _np_r:
                    return None
                return max((((_p[k][0] - _ref[k][0]) ** 2 + (_p[k][1] - _ref[k][1]) ** 2) ** 0.5)
                           for k in range(_np_r))
            _rings = [_ring_at(i) for i in range(n)]
            for i in range(n):
                _srt[i]['ring'] = _rings[i]
            _ref_rings = sorted(_rings[i] for i in _ref_idxs if _rings[i] is not None)
            _noise = _ref_rings[len(_ref_rings) // 2] if _ref_rings else 0.0     # ref-region self-scatter
            _ring_peak = max((r for r in _rings if r is not None), default=0.0)
            # GATE ("is there a real ring") scales with the signal |αβ|: a low-sensing-gain motor (P4-37,
            # |αβ|~68) shows a real ring of only ~2-3 counts where the P4-16 (|αβ|~120) shows ~5-10, so a
            # fixed absolute count gated the small-signal motor out. ~2.5% of |αβ| (floored) instead.
            _ring_sig = max(1.5, 0.025 * _mag_max)
            # BAND ("ring cleared") = the ring has dropped ~70% of the way from its PEAK back to the noise
            # floor. Using the peak->noise RANGE (not a fixed offset) auto-scales across motors AND stops a
            # HALF-cleared low settling from qualifying (a fixed band let a still-ringing 120 ns through).
            _band  = max(1.5 * _noise, _noise + 0.30 * (_ring_peak - _noise))

            # === ENVELOPE-FIT pick. The per-settling worst-pattern ring OSCILLATES (underdamped switching
            # ring), so a "lowest cleared settling" pick can grab a NOISE TROUGH -- e.g. a 60 ns dip sitting
            # BELOW a real 120 ns peak. Instead fit a decaying envelope A*exp(-t/tau)+c to the MONOTONE upper
            # envelope of the ring (cumulative max from the settled end, so a single dip can't pull it down),
            # using ALL settlings -> immune to any one lucky-low reading. Pick where the fitted TRANSIENT has
            # decayed below the noise floor. A fit-quality gate (amplitude + RMS residual) falls the pick back
            # to the original downward ring-clear scan when the fit is poor/absent.
            def _fit_env_pick():
                import math as _math
                _pts = sorted((r['settling'], r['ring']) for r in _srt if r.get('ring') is not None)
                if len(_pts) < 4:
                    return None
                _xf = [p[0] for p in _pts]; _wf = [p[1] for p in _pts]
                _env = [0.0] * len(_wf); _run = -1e18            # monotone upper envelope from the settled end
                for _i in range(len(_wf) - 1, -1, -1):
                    _run = max(_run, _wf[_i]); _env[_i] = _run
                _c = float(_noise)                               # asymptote = ref-region noise floor
                _lx, _ly = [], []                                # log-linear fit of (env-c)=A*exp(-t/tau)
                for _i in range(len(_xf)):
                    _d = _env[_i] - _c
                    if _d > max(0.3, 0.20 * _noise):
                        _lx.append(_xf[_i]); _ly.append(_math.log(_d))
                if len(_lx) < 3:
                    return None
                _mx = sum(_lx) / len(_lx); _my = sum(_ly) / len(_ly)
                _sxx = sum((x - _mx) ** 2 for x in _lx) or 1e-9
                _slope = sum((_lx[_i] - _mx) * (_ly[_i] - _my) for _i in range(len(_lx))) / _sxx
                if _slope >= -1e-6:                              # not decaying -> unusable
                    return None
                _tau = -1.0 / _slope
                _A = _math.exp(_my - _slope * _mx)
                def _envf(t): return _A * _math.exp(-t / _tau) + _c
                _rms = (sum((_envf(_xf[_i]) - _env[_i]) ** 2 for _i in range(len(_xf))) / len(_xf)) ** 0.5
                _tol = max(0.75, float(_noise))                 # "settled" = transient within ~1 floor of c
                _tcross = _tau * _math.log(_A / _tol) if _A > _tol else float(_FLOOR_NS)
                _pick_t = int(max(_FLOOR_NS, min(_math.ceil(_tcross / 10.0) * 10, half_period_ns - 1)))
                _cx = list(range(0, int(max(_xf)) + 1, 5)); _cy = [_envf(x) for x in _cx]
                return {'A': _A, 'tau': _tau, 'c': _c, 'tol': _tol, 'thr': _c + _tol,
                        'pick': _pick_t, 'tcross': _tcross, 'rms': _rms,
                        # Trust the fit when the ring is clearly above the NOISE FLOOR (relative to _noise,
                        # NOT scaled to |αβ| -- the 2.5%-of-|αβ| _ring_sig over-gates high-sensing-gain
                        # motors, e.g. |αβ|=185 demands 4.6 cts but a clean ring is only ~3.5) AND the decay
                        # fits an exponential well (RMS small vs amplitude).
                        'trust': (_A >= max(2.5 * _noise, 1.0)) and (_rms <= max(0.6, 0.5 * _A)),
                        'extrap': _tcross > max(_xf) + 1, 'xmax': int(max(_xf)), 'cx': _cx, 'cy': _cy}

            optimal_settling = original_settling
            _resolvable = False
            _pick = None
            _t_settle = None
            _sel_desc = "none"
            _ring_real = _ring_peak >= max(2.0 * max(_noise, 1e-6), _noise + _ring_sig)
            # Run the envelope fit ALWAYS: its noise-relative trust gate is a better "is there a resolvable
            # ring" test than the |αβ|-scaled _ring_real heuristic (which kept an atrocious incumbent on a
            # clean-but-small ring). The fit is PRIMARY; _ring_real only triggers the scan/keep fallbacks.
            _fit = _fit_env_pick()
            if _fit is not None and _fit['trust']:
                _t_settle = _fit['pick']
                _pick = min(_srt, key=lambda r: abs(r['settling'] - _t_settle))
                _sel_desc = ("ENVELOPE FIT {:.1f}*exp(-t/{:.0f})+{:.1f}, settled when transient<{:.2f} "
                             "-> {} ns (fit RMS {:.2f}, ref-noise {:.1f})".format(
                                 _fit['A'], _fit['tau'], _fit['c'], _fit['tol'], _t_settle,
                                 _fit['rms'], _noise)
                             + ("  [EXTRAPOLATED past {} ns -- sweep longer to confirm]".format(_fit['xmax'])
                                if _fit['extrap'] else ""))
            elif _ring_real:
                # FALLBACK (fit poor/absent): the original scan from the HIGHEST settling downward, tracking
                # the LOWEST cleared settling and TOLERATING up to ONE not-cleared settling (an isolated
                # thermal spike). A SECOND not-cleared settling = the real ring wall -> stop.
                _acc_s = sorted([i for i in range(n) if _means[i] >= _MAG_OK * _mag_max
                                 and _srt[i]['settling'] >= _FLOOR_NS], key=lambda i: _srt[i]['settling'])
                _pi = None; _skips = 0
                for i in reversed(_acc_s):
                    if _rings[i] is not None and _rings[i] <= _band:
                        _pi = i
                    else:
                        _skips += 1
                        if _skips > 1:
                            break
                if _pi is not None:
                    _t_settle = int(_srt[_pi]['settling']); _pick = _srt[_pi]
                    _sel_desc = ("ring-clear SCAN fallback (fit poor; lowest cleared settling, ring <= {:.1f}, "
                                 "tolerating 1 spike; ref-noise {:.1f})".format(_band, _noise))
            if _pick is None:
                if not _ring_real:
                    # No resolvable ring on this motor (e.g. flat |αβ|, ring buried in noise). Do NOT drop to
                    # the floor -- that would be a destructive guess AND it zeroes the slope. Leave _t_settle
                    # None so the existing MaxSettlingTime is KEPT untouched (see the resolvable branch below).
                    _t_settle = None
                    _sel_desc = "no ring resolvable -- keeping existing {} ns (no change)".format(
                        original_settling)
                else:
                    # ring WAS real but nothing cleared the band (odd) -> safe floor
                    _cands2 = [i for i in range(n) if _means[i] >= _MAG_OK * _mag_max
                               and _srt[i]['settling'] >= _FLOOR_NS]
                    if _cands2:
                        _b = min(_cands2, key=lambda i: _srt[i]['settling'])
                        _t_settle = int(_srt[_b]['settling']); _pick = _srt[_b]
                    _sel_desc = "{} ns safety floor".format(_FLOOR_NS)

            print("Settling sweep results (RING = worst-pattern dev from the ring-free end; pick = lowest cleared):")
            print("  {:>7} {:>9} {:>9} {:>9} {:>9}".format("settle", "ring", "drift", "offset", "|αβ|"))
            for r in _srt:
                _mk = "  <- pick" if (_pick is not None and r is _pick) else ""
                _rr = "{:9.2f}".format(r['ring']) if r.get('ring') is not None else "{:>9}".format("-")
                _ds = "{:9.2f}".format(r['drift']) if r.get('drift') is not None else "{:>9}".format("-")
                print("  {:7d} {} {} {:9.1f} {:9.1f}{}".format(
                    r['settling'], _rr, _ds, r['offset'], r['meanI'], _mk))
            print("  ring: ref-noise={:.2f} peak={:.2f} cleared<={:.2f}  real-ring={}  accurate |αβ| >= {:.0f}"
                  .format(_noise, _ring_peak, _band, _ring_real, _MAG_OK * _mag_max))
            if _fit is not None:
                print("  envelope fit: {:.1f}*exp(-t/{:.0f} ns)+{:.1f}  RMS={:.2f}  trust={}  "
                      "settled(transient<{:.2f}) -> {} ns{}".format(
                          _fit['A'], _fit['tau'], _fit['c'], _fit['rms'], _fit['trust'], _fit['tol'],
                          _fit['pick'], "  [EXTRAPOLATED]" if _fit['extrap'] else ""))

            # --- PER-PATTERN ring table. Deviation of each pattern's mean vector from the RING-FREE END
            # (average of the highest non-collapsed settlings -- the SAME reference the pick uses, NOT a
            # hardcoded 300 ns). This is the honest residual ring per pattern: LARGE at low settling, falling
            # to the reference noise once cleared. NO settling is artificially zero here. ---
            if _np_r > 0 and _ref:
                print("--- per-PATTERN ring: |dev from ring-free end (avg of top settlings)| (counts) ---")
                print("  {:>7}".format("settle") + "".join(
                    "  P{}({:+d})".format(p, int(round(-180 + p * 360.0 / _np_r))) for p in range(_np_r)))
                for r in _srt:
                    _pt = r.get('patterns') or []
                    _cells = []
                    for p in range(_np_r):
                        if p < len(_pt) and p < len(_ref):
                            _a, _b, _ = _pt[p]; _ra, _rb = _ref[p]
                            _cells.append("{:9.1f}".format(((_a - _ra) ** 2 + (_b - _rb) ** 2) ** 0.5))
                        else:
                            _cells.append("{:>9}".format("-"))
                    print("  {:7d}".format(r['settling']) + "".join(_cells))

            if _t_settle is not None:
                optimal_settling = _t_settle
                _resolvable = True
                _reason = ("{}: {} ns (the fold handles the DC offset).".format(_sel_desc, _t_settle))
                print("Optimal MaxSettlingTime: {} ns  (was {} ns)".format(
                    optimal_settling, original_settling))
                print("  reason: {}".format(_reason))
            else:
                _reason = ("NOT resolvable: no accurate settling candidate. Left "
                           "MaxSettlingTime at {} ns.".format(original_settling))
                print(_reason)

            # Headroom report: does the sample sequence still fit the low-side window at high duty?
            _max_duty = None
            try:
                _dead = self.node.sdo[0x3001][2].raw
                _prop = self.node.sdo[0x3001][3].raw
                _samp = self.node.sdo[0x3001][6].raw
                _period_ns = 1_000_000_000.0 / max(freq_hz, 1)
                _needed = _dead + _prop + optimal_settling + _samp
                _max_duty = max(0.0, 1.0 - _needed / _period_ns) * 100.0
                print("  headroom: sample seq = dead {}+prop {}+settle {}+samp {} = {} ns "
                      "=> ~{:.1f}% max duty @ {:.0f} kHz.".format(
                          _dead, _prop, optimal_settling, _samp, _needed,
                          _max_duty, freq_hz / 1000.0))
                if _resolvable and _max_duty < 80.0:
                    print("  NOTE: max duty <80% — the high-speed current ceiling is reduced. A lower "
                          "settling would buy headroom if the ring allows it.")
            except Exception:
                pass

            # --- Debug plot: cv% (ring/noise) and mean|I| (accuracy) vs settling, marking the pick ---
            try:
                import matplotlib
                matplotlib.use('Agg')  # non-interactive; avoids wx/Tk backend conflicts
                import matplotlib.pyplot as plt

                _pc = None
                try:
                    _pc = int(self.node.sdo[0x1018][2].raw)
                except Exception:
                    pass
                _puck_model = getattr(self, '_PRODUCT_CODE_MODELS', {}).get(_pc, 'unknown')
                _node_label = 'Node {}  {}'.format(self.node.id, _puck_model)

                _xs     = [r['settling'] for r in _srt]
                _ringp  = [(r.get('ring') if r.get('ring') is not None else float('nan')) for r in _srt]
                _mean   = [r['meanI'] for r in _srt]

                fig, (ax, ax2, ax3) = plt.subplots(1, 3, figsize=(23, 6))
                axr = ax.twinx()
                # LEFT PANEL: the residual RING (worst-pattern deviation from the ring-free end) = the pick
                # driver, after thermal detrend. HIGH at low settling (ring present), falls to the ref-region
                # noise once cleared -> pick the LOWEST settling that has cleared. |αβ| on the twin axis is
                # the window-collapse guard (drops only when over-settled).
                ax.plot(_xs, _ringp, '-o', color='tomato', markersize=6, linewidth=1.8,
                        label='residual ring (worst pattern) — falls to noise = cleared')
                ax.axhline(_band, color='gray', linestyle='--', linewidth=0.9, alpha=0.6,
                           label='cleared <= {:.1f}'.format(_band))
                axr.plot(_xs, _mean, '--s', color='steelblue', markersize=5,
                         linewidth=1.0, alpha=0.6, label='|αβ| magnitude (counts, window collapse)')
                # Fitted decay envelope + settled-threshold: the pick is where the smooth fit (not any single
                # noisy point) crosses the threshold, so a ring TROUGH can no longer win the pick.
                if _fit is not None:
                    ax.plot(_fit['cx'], _fit['cy'], '-', color='seagreen', linewidth=1.8, alpha=0.9,
                            label='envelope fit  {:.1f}·e^(−t/{:.0f})+{:.1f}'.format(
                                _fit['A'], _fit['tau'], _fit['c']))
                    ax.axhline(_fit['thr'], color='green', linestyle=':', linewidth=1.4,
                               label='settled threshold  {:.1f}'.format(_fit['thr']))
                if _resolvable:
                    ax.axvline(optimal_settling, color='black', linewidth=2.0,
                               label='PICK {} ns'.format(optimal_settling))
                    _result_note = '{} ns'.format(optimal_settling)
                    if _max_duty is not None:
                        _result_note += '  (~{:.0f}% max duty)'.format(_max_duty)
                else:
                    _result_note = 'NOT RESOLVABLE (kept {} ns)'.format(original_settling)

                ax.set_title('MaxSettlingTime cal — measured ring-clear (detrended)\n'
                             '{}   (drive {} mA, ud {})   |   RESULT: {}'.format(
                                 _node_label, calibration_current, drive_ud, _result_note),
                             fontsize=11)
                ax.set_xlabel('MaxSettlingTime (ns)')
                ax.set_ylabel('residual ring (counts) — worst pattern', color='tomato')
                axr.set_ylabel('|αβ| magnitude (counts)', color='steelblue')
                ax.grid(True, alpha=0.25)
                _l1, _b1 = ax.get_legend_handles_labels()
                _l2, _b2 = axr.get_legend_handles_labels()
                ax.legend(_l1 + _l2, _b1 + _b2, fontsize=8, loc='upper right')

                # RIGHT PANEL: the RAW sample cloud — EVERY per-sample |αβ| residual (deviation from its
                # own settling's mean) plotted at its settling. This is the un-aggregated "spray": if the
                # ring adds structure the std buries (outliers, fat tails, bimodality at low settling), it
                # shows here. Envelope = ±1σ per settling so the width trend is visible against the cloud.
                _nsamp = max((len(r.get('samples') or []) for r in results), default=0)
                for r in results:
                    _sm = r.get('samples') or []
                    if _sm:
                        ax2.scatter([r['settling']] * len(_sm), _sm, s=4, color='purple',
                                    alpha=0.12, edgecolors='none')
                _ex, _ehi, _elo = [], [], []
                for r in results:
                    _sm = r.get('samples') or []
                    if _sm:
                        _m = sum(_sm) / len(_sm)
                        _sd = (sum((x - _m) ** 2 for x in _sm) / len(_sm)) ** 0.5
                        _ex.append(r['settling']); _ehi.append(_m + _sd); _elo.append(_m - _sd)
                if _ex:
                    ax2.plot(_ex, _ehi, '-', color='darkorange', linewidth=1.3, label='±1σ envelope')
                    ax2.plot(_ex, _elo, '-', color='darkorange', linewidth=1.3)
                ax2.axhline(0, color='gray', linewidth=0.8, alpha=0.5)
                if _resolvable:
                    ax2.axvline(optimal_settling, color='black', linewidth=2.0,
                                label='PICK {} ns'.format(optimal_settling))
                ax2.set_title('RAW per-sample spray (~{} samples/settling)\n'
                              'residual = sample |αβ| − that settling\'s mean (counts)'.format(_nsamp),
                              fontsize=11)
                ax2.set_xlabel('MaxSettlingTime (ns)')
                ax2.set_ylabel('per-sample residual (counts)', color='purple')
                ax2.grid(True, alpha=0.25)
                ax2.legend(fontsize=8, loc='upper right')

                # THIRD PANEL: the PER-PATTERN ring, referenced to the RING-FREE END (avg of the highest
                # non-collapsed settlings -- the same reference the pick uses, NOT a hardcoded 300 ns, which
                # would force that settling to a FALSE zero). Each pattern's deviation from that reference is
                # its residual ring: LARGE at low settling, falling to the reference noise once cleared. No
                # settling is artificially zero. (A settling that dips to ~0 here is genuinely ring-free.)
                if _np_r > 0 and _ref:
                    for p in range(_np_r):
                        _ra, _rb = _ref[p]
                        _dev = []
                        for r in _srt:
                            _pt = r.get('patterns') or []
                            if p < len(_pt):
                                _a, _b, _ = _pt[p]
                                _dev.append(((_a - _ra) ** 2 + (_b - _rb) ** 2) ** 0.5)
                            else:
                                _dev.append(float('nan'))
                        _deg = int(round(-180 + p * 360.0 / _np_r))
                        ax3.plot(_xs, _dev, '-o', markersize=4, linewidth=1.4, label='{:+d}°'.format(_deg))
                    if _resolvable:
                        ax3.axvline(optimal_settling, color='black', linewidth=2.0)
                    if _fit is not None:
                        ax3.axhline(_fit['thr'], color='green', linestyle=':', linewidth=1.2,
                                    label='settled threshold {:.1f}'.format(_fit['thr']))
                    ax3.set_title('Per-PATTERN ring: |dev from the RING-FREE end|\n'
                                  '(6 SVM patterns; ring → rises at LOW settle; flat = cleared)', fontsize=11)
                    ax3.set_xlabel('MaxSettlingTime (ns)')
                    ax3.set_ylabel('deviation from ring-free end (counts)')
                    ax3.grid(True, alpha=0.25)
                    ax3.legend(fontsize=7, loc='upper right', ncol=2, title='pattern angle')

                plt.tight_layout()
                # Store in the session log (timing/images/) like the other cal plots, not the cwd.
                from ..paths import session_path
                import datetime as _dt
                _pc2 = None
                try:
                    _pc2 = int(self.node.sdo[0x1018][2].raw)
                except Exception:
                    pass
                _model2 = getattr(self, '_PRODUCT_CODE_MODELS', {}).get(_pc2, 'unknown').replace(' ', '_')
                _pfx2 = 'node{}_{}_'.format(getattr(self.node, 'id', '?'), _model2)
                _ts2 = _dt.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
                plot_path = session_path('timing/images/{}maxsettling_{}.png'.format(_pfx2, _ts2))
                os.makedirs(os.path.dirname(plot_path), exist_ok=True)
                fig.savefig(plot_path, dpi=110, bbox_inches='tight')
                plt.close(fig)
                print("Calibration plot saved: {}".format(plot_path))
                try:
                    webbrowser.open('file://' + plot_path)
                except Exception:
                    pass
            except ImportError:
                print("matplotlib not installed — skipping calibration plot")
            except Exception as _plot_err:
                print("Plot failed: {}".format(_plot_err))

            # --- Apply + save (gated on _resolvable) ---
            if _resolvable:
                self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE  # idle: write applies live
                self.node.sdo['Amp']['MaxSettlingTime'].raw = optimal_settling
                self.node.sdo['Save']['Single'].raw = ((0x3001 << 8) | 0x05)
                print("MaxSettlingTime={} ns applied live and saved to EEPROM.".format(optimal_settling))
                _el = time.time() - _t_cal0
                print("  ⏱ MaxSettlingTime cal total time: {:.1f} s  ({} measurements)".format(
                    _el, len(_order)))
                # GUARD: changing the settling shifts the iSense zero-point AND scale (both are
                # sample-timing dependent), and the Current Sense Slope (0x3008:7 / 0x3009:7) is a
                # sample-timing artifact too -- so it is now WRONG and, worse, still ACTIVE in firmware
                # every cycle.  In a full cal (calAll) the iSense + slope cals re-run next and fix this;
                # standalone, zero the stale slope now and make the caller re-run iSense + slope.
                if optimal_settling != original_settling and not calAll:
                    try:  # disable the now-stale slope so it isn't applied at the new timing
                        self.node.sdo[0x3008][7].raw = 0
                        self.node.sdo[0x3009][7].raw = 0
                        self.node.sdo['Save']['Single'].raw = ((0x3008 << 8) | 0x07)
                        self.node.sdo['Save']['Single'].raw = ((0x3009 << 8) | 0x07)
                    except Exception:
                        pass
                    print("!" * 70)
                    print("  WARNING: MaxSettlingTime changed {} -> {} ns.  iSense bias+gain are now"
                          " STALE, and the Current Sense Slope was ZEROED (it was measured at the old"
                          " timing).".format(original_settling, optimal_settling))
                    print("  RE-RUN iSense/gain + Current Sense Slope before driving, or the current")
                    print("  loop may pull phantom current / oscillate at 0 torque.  Puck left in IDLE.")
                    print("!" * 70)

                    # Settling just invalidated Bias/Gain/Slope, so offer (or, with the enforce flag,
                    # automatically run) the follow-on current-sense re-cal right now -- the puck is never
                    # left half-calibrated. Each chained step uses calAll=True to match calibrate_all's
                    # proven sequencing (no per-step Enable/complete; itiming's Enable at the end covers it).
                    # Enforce path (self.settling_autochain = True) skips the prompt for scripted/auto use.
                    _enforce = bool(getattr(self, 'settling_autochain', False))
                    _do_chain = _enforce
                    if not _enforce:
                        _do_chain = (wx.MessageBox(
                            "MaxSettlingTime changed {} -> {} ns, so iSense Bias/Gain and the Current "
                            "Sense Slope are now stale.\n\nRe-calibrate current sense now "
                            "(Bias → Gain → Slope)?".format(original_settling, optimal_settling),
                            "Re-calibrate Current Sense?", wx.YES_NO | wx.ICON_QUESTION) == wx.YES)
                    if _do_chain:
                        print("\nChaining current-sense re-cal (Bias -> Gain -> Slope){} ...".format(
                            "  [enforced]" if _enforce else ""))
                        try:
                            if self.calibrate_ibias(None, True) is not False \
                               and self.calibrate_igainfactor(None, True) is not False:
                                for _st in (1, 2):          # slope may hit a transient SYNC/SDO glitch
                                    try:
                                        self.calibrate_current_slope(None, True, force_sdo=(_st == 2))
                                        break
                                    except Exception as _se:
                                        print("  Slope attempt {}/2 failed: {}".format(_st, _se))
                                        try:
                                            self.node.sdo['Motor']['ud'].raw = 0
                                            self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE
                                            self.node.sdo["ControlWord"].raw = CLEAR_FAULT
                                        except Exception:
                                            pass
                                print("Current-sense re-cal complete — puck fully calibrated at {} ns."
                                      .format(optimal_settling))
                            else:
                                print("  Current-sense chain stopped early (a step returned abort); "
                                      "re-run current sense manually.")
                        except Exception as _ce:
                            print("  Current-sense chain aborted: {} (settling is saved; re-run current "
                                  "sense manually).".format(_ce))
                            try:
                                self.node.sdo['Motor']['ud'].raw = 0
                                self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE
                            except Exception:
                                pass
            else:
                self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE
                self.node.sdo['Amp']['MaxSettlingTime'].raw = original_settling

            self.frame_statusbar.SetStatusText(
                "Current timing calibrated: {} ns".format(optimal_settling), 1)

            if self.ADC_ON == False and self.adcWasON == True:
                self.on_off_adc(self)

            if calAll == False:
                self.Enable()

        except Exception as _exc:
            try:
                self.node.sdo['Motor']['ud'].raw = 0
                self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE
                self.node.sdo['Amp']['MaxSettlingTime'].raw = original_settling
            except Exception:
                pass
            if self.ADC_ON == False and self.adcWasON == True:
                self.on_off_adc(self)
            if calAll:
                raise
            self._cal_fault(_exc)
            self.Enable()

    def measure_gain_ripple(self, event, calAll=False):  # wxGlade: puckutilityapp_frame.<event_handler>
        """Measure α/β current-sense balance as a spinning-vector CIRCULARITY ripple.

        Drives a constant voltage vector around a full electrical rotation and records the current
        MAGNITUDE |I|=sqrt(id²+iq²) at each angle.  Balanced α/β sense -> constant |I| (a circle);
        a gain imbalance makes |I| ripple at 2×-electrical -- the exact fingerprint of the low-speed,
        inertia-smoothed, encoder-independent 'cogging' we're chasing.  Reports pk-pk and the
        2×-electrical amplitude as % of mean |I|.  Run before/after the gain cal to see the imbalance
        (and, once the circularity FIT lands, to prove it dropped)."""
        if calAll == False:
            if self.check_for_node() == False:
                return False
            self.Disable()
        if self.ADC_ON == True:
            self.on_off_adc(self)       # quiet the ADC monitor -- it contends for SDO reads
            self.adcWasON = True
        else:
            self.adcWasON = False
        import math, os
        try:
            i_peak   = self.node.sdo['Calibration']['i_peak'].raw
            i_cal    = self.node.sdo['Calibration']['i_cal'].raw
            beta_gf  = self.node.sdo['Beta']['Gainfactor'].raw
            alpha_gf = self.node.sdo['Alpha']['Gainfactor'].raw
            alpha_bias = float(self.node.sdo['Alpha']['Bias'].raw)
            beta_bias  = float(self.node.sdo['Beta']['Bias'].raw)
            _asens = 2.96   # ~counts/mA (from gain cal) to convert the raw-ADC offset to mA
            print("--- iSense alpha/beta current-ripple (circularity) measurement ---")
            print("Gainfactors in effect: Alpha={}  Beta={}  (target {} mA)".format(
                alpha_gf, beta_gf, i_cal))

            def _imag():
                _id = self.node.sdo['Motor']['id'].raw / 1000.0 * i_peak
                _iq = self.node.sdo['CurrentFeedback'].raw / 1000.0 * i_peak
                return (_id * _id + _iq * _iq) ** 0.5

            def _wait_settled(timeout=1.5):
                # Wait for the rotor to actually STOP after a theta_e step.  If it's still moving its
                # back-EMF modulates |I| and swamps the tiny alpha/beta imbalance (the 0.08 s first
                # build read pure rotor motion -> a 71% swing).  Poll the raw encoder until it holds
                # within 2 counts for ~0.2 s, or bail at timeout.
                _p0 = self.node.sdo['Encoder']['RawPosition'].raw
                _t0 = time.time(); _stable = 0
                while time.time() - _t0 < timeout:
                    time.sleep(0.06); _yield()
                    _p1 = self.node.sdo['Encoder']['RawPosition'].raw
                    if abs(_p1 - _p0) <= 2:
                        _stable += 1
                        if _stable >= 3:
                            return
                    else:
                        _stable = 0
                    _p0 = _p1

            # Energise in voltage-angle mode.  Seed a voltage at theta=0 and let the rotor ALIGN and
            # STOP before ramping, so the ramp reads a stationary (motion-free) current.
            self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE
            self.node.sdo["ControlWord"].raw = CLEAR_FAULT
            self.node.sdo["ControlWord"].raw = SHUTDOWN
            self.node.sdo["ControlWord"].raw = OP_ENABLED
            self.node.sdo["SetModeOfOperation"].raw = MODE_PHASE_VOLTAGE_ANGLE
            self.node.sdo['Theta_e'].raw = 0
            self.node.sdo['Motor']['ud'].raw = 2000   # seed to pull the rotor to theta=0
            _wait_settled()
            motor_ud = 2000
            _cur = _imag() or 0.0
            while _cur < i_cal and motor_ud < 16000:
                motor_ud += 150
                self.node.sdo['Motor']['ud'].raw = motor_ud
                time.sleep(0.04)
                _yield()
                _cur = _imag() or 0.0
            _wait_settled()
            _cur = _imag() or 0.0
            print("Drive established: ud={}  |I|~={:.0f} mA (settled)".format(motor_ud, _cur))

            # --- OFFSET vs MaxSettlingTime: does the sample timing drive the offset? ---
            # Fixed ud => constant ACTUAL current across settlings, so any change in the measured
            # offset is purely a sample-timing effect.
            _N, _M = 24, 20
            _orig_settling = self.node.sdo['Amp']['MaxSettlingTime'].raw

            def _sweep_offset():
                # One full-electrical-cycle sweep at the current ud+settling; rotor stopped each step.
                # Returns (mean|I|, pkpk%, offset_mA, iA, iB, amp1x_mA, amp2x_pct).
                _theta = [int(round(-32768 + i * 65536.0 / _N)) for i in range(_N)]
                _mg, _idl, _iql, _afl, _bfl = [], [], [], [], []
                for _k, _th in enumerate(_theta):
                    self.frame_statusbar.SetStatusText("offset/settling - {}/{}".format(_k + 1, _N), 1)
                    self.node.sdo['Theta_e'].raw = _th
                    _wait_settled()
                    _sid = _siq = _saf = _sbf = 0.0
                    for _ in range(_M):
                        _sid += self.node.sdo['Motor']['id'].raw
                        _siq += self.node.sdo['CurrentFeedback'].raw
                        _saf += self.node.sdo['Alpha']['Filtered'].raw
                        _sbf += self.node.sdo['Beta']['Filtered'].raw
                        time.sleep(0.003); _yield()
                    _idm = (_sid / _M) / 1000.0 * i_peak
                    _iqm = (_siq / _M) / 1000.0 * i_peak
                    _idl.append(_idm); _iql.append(_iqm)
                    _afl.append(_saf / _M); _bfl.append(_sbf / _M)
                    _mg.append((_idm * _idm + _iqm * _iqm) ** 0.5)
                _r = [_theta[i] / 32768.0 * math.pi for i in range(_N)]
                _mnv = (sum(_mg) / _N) or 1.0
                _pk = (max(_mg) - min(_mg)) / _mnv * 100.0
                _oa = sum(_idl[i] * math.cos(_r[i]) - _iql[i] * math.sin(_r[i]) for i in range(_N)) / _N
                _ob = sum(_idl[i] * math.sin(_r[i]) + _iql[i] * math.cos(_r[i]) for i in range(_N)) / _N
                _offv = (_oa * _oa + _ob * _ob) ** 0.5
                # RAW cross-check: mean raw Alpha/Beta over the full turn, minus bias -- the offset
                # straight from the ADC (no Park, no theta_e assumption).  The true rotating current
                # averages to zero, so a nonzero mean here IS a real sense offset.  If this disagrees
                # with _offv, the firmware Parks by the encoder (not theta_e) and _offv was an artifact.
                _rawoff = (((sum(_afl) / _N - alpha_bias) / _asens) ** 2 +
                           ((sum(_bfl) / _N - beta_bias) / _asens) ** 2) ** 0.5
                _c1 = sum(_mg[i] * math.cos(_r[i]) for i in range(_N))
                _s1 = sum(_mg[i] * math.sin(_r[i]) for i in range(_N))
                _a1 = 2.0 * math.sqrt(_c1 * _c1 + _s1 * _s1) / _N
                _c2 = sum(_mg[i] * math.cos(2.0 * _r[i]) for i in range(_N))
                _s2 = sum(_mg[i] * math.sin(2.0 * _r[i]) for i in range(_N))
                _a2 = 2.0 * math.sqrt(_c2 * _c2 + _s2 * _s2) / _N / _mnv * 100.0
                return _mnv, _pk, _offv, _oa, _ob, _a1, _a2, _rawoff

            # OFFSET vs CURRENT at the current settling -- learn how the offset scales with load so we
            # can pick the right correction (fixed subtract vs current-proportional).
            _levels = sorted(set(max(20, int(i_cal * _f)) for _f in (0.3, 0.55, 0.8, 1.0, 1.3)))
            print("--- OFFSET vs CURRENT (fixed settling={} ns) ---".format(_orig_settling))
            print("  target(mA)  mean|I|   recon-OFFSET(mA)   raw-ADC-OFFSET(mA)   pk-pk%   off/|I|%")
            _rows = []
            for _lvl in _levels:
                # ramp ud to ~_lvl at theta=0 (rotor stopped), then measure offset over a full turn
                self.node.sdo['Theta_e'].raw = 0
                _ud = 500
                self.node.sdo['Motor']['ud'].raw = _ud
                _wait_settled()
                _cur = _imag() or 0.0
                while _cur < _lvl and _ud < 16000:
                    _ud += 150
                    self.node.sdo['Motor']['ud'].raw = _ud
                    time.sleep(0.04); _yield()
                    _cur = _imag() or 0.0
                _wait_settled()
                _mnv, _pk, _offv, _oa, _ob, _a1, _a2, _rawoff = _sweep_offset()
                _frac = _offv / _mnv * 100.0 if _mnv else 0.0
                _rows.append((_mnv, _offv, _rawoff))
                print("  {:8d}  {:7.1f}   {:10.1f}       {:10.1f}        {:6.1f}  {:7.1f}".format(
                    _lvl, _mnv, _offv, _rawoff, _pk, _frac))

            self.node.sdo['Motor']['ud'].raw = 0
            self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE
            self.node.sdo['Amp']['MaxSettlingTime'].raw = _orig_settling    # RESTORE original
            print("Restored MaxSettlingTime = {} ns".format(_orig_settling))

            # Least-squares fit offset = a + b*|I| across the levels -> the correction model.
            _xs = [r[0] for r in _rows]; _ys = [r[1] for r in _rows]
            _nL = max(len(_xs), 1)
            _mx = sum(_xs) / _nL; _my = sum(_ys) / _nL
            _den = sum((x - _mx) ** 2 for x in _xs) or 1.0
            _b = sum((_xs[i] - _mx) * (_ys[i] - _my) for i in range(len(_xs))) / _den
            _a = _my - _b * _mx
            print(">>> fit  offset(mA) = {:.1f} + {:.3f}*|I|   (baseline {:.1f} mA + {:.1f}% of current)".format(
                _a, _b, _a, _b * 100.0))
            if abs(_a) < 0.3 * max(_my, 1.0):
                print(">>>   -> mostly CURRENT-PROPORTIONAL (~{:.1f}% of |I|): correct with a "
                      "current-scaled subtraction along the fixed offset direction.".format(_b * 100.0))
            else:
                print(">>>   -> significant FIXED baseline ({:.1f} mA): a constant subtract handles most; "
                      "add the {:.1f}%/|I| slope term for exactness.".format(_a, _b * 100.0))

            try:
                import matplotlib
                matplotlib.use('Agg')
                import matplotlib.pyplot as plt
                fig, ax = plt.subplots(figsize=(8, 4))
                ax.plot([r[0] for r in _rows], [r[1] for r in _rows], 'o-', label='offset')
                ax.plot(_xs, [_a + _b * x for x in _xs], 'r--', lw=0.8,
                        label='fit {:.0f}+{:.2f}|I|'.format(_a, _b))
                ax.set_xlabel('current |I| (mA)'); ax.set_ylabel('stator current OFFSET (mA)')
                ax.set_title('current-sense offset vs load (P4-16, settling={} ns)'.format(_orig_settling))
                ax.grid(True, alpha=0.3); ax.legend()
                _path = os.path.abspath('offset_vs_current.png')
                fig.savefig(_path, dpi=110, bbox_inches='tight'); plt.close(fig)
                print("Plot saved: {}".format(_path))
            except Exception as _pe:
                print("(plot skipped: {})".format(_pe))

            self.frame_statusbar.SetStatusText("offset = {:.0f} + {:.2f}|I| mA".format(_a, _b), 1)
            if self.ADC_ON == False and self.adcWasON == True:
                self.on_off_adc(self)   # restore ADC monitor
            if calAll == False:
                self.Enable()
        except Exception as _exc:
            try:
                self.node.sdo['Motor']['ud'].raw = 0
                self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE
            except Exception:
                pass
            if self.ADC_ON == False and self.adcWasON == True:
                self.on_off_adc(self)
            if calAll:
                raise
            self._cal_fault(_exc)
            self.Enable()

    def fold_baseline_offset(self, event, calAll=False):
        """Apply the slope cal's fitted drive-on baseline (a0/b0, mA) so it's removed under drive.

        TWO PATHS, chosen by firmware capability (probed at run time):
          * v3+ (has 0x3008:8 / 0x3009:8): write a0/b0 to the DRIVE-GATED offset register. The firmware
            subtracts it ONLY while driving (`iAlBe -= offset`), so there is NO idle phantom, Bias stays
            clean, and a later Bias re-cal doesn't cancel it. This is the production fix.
          * pre-v3 (no register): LEGACY fold into the always-on iSense Bias. This still removes the offset
            under drive, but Bias is unconditional, so it injects a0/b0 as an equal/opposite error at TRUE
            idle, and a later Bias re-cal / full cal OVERWRITES it. Diagnostic stopgap only.
        Either path uses the last 'Current Sense Slope' result (a0/b0) and requires it to have PASSED its
        store gate (_slope_stored). Legacy revert = re-run 'Current Sense Bias' (or a full cal)."""
        if calAll == False:
            if self.check_for_node() == False:
                return False
            self.Disable()
        try:
            # GATE: only fold a baseline from a slope cal that actually PASSED its store gate. _slope_a0/b0
            # are set unconditionally by the slope cal (even on a failed/degenerate fit), so without this a
            # manual "Baseline Fold" after a non-passing slope would inject a bad a0/b0 into the iSense Bias.
            # (calibrate_all already checks _slope_stored before calling this; this closes the standalone path.)
            if not getattr(self, '_slope_stored', False):
                print("Baseline fold: the last Current Sense Slope did NOT pass its gate (or wasn't run) — "
                      "refusing to fold an untrustworthy/degenerate baseline. Run a passing slope cal first. "
                      "Nothing changed.")
                if calAll == False:
                    self.Enable()
                return False
            a0 = getattr(self, '_slope_a0', None)
            b0 = getattr(self, '_slope_b0', None)
            if a0 is None or b0 is None:
                print("Baseline fold: no slope result in memory — run 'Current Sense Slope' (or a full "
                      "cal) first, then this. Nothing changed.")
                if calAll == False:
                    self.Enable()
                return False

            # ── Drive-gated offset register (firmware v3+) ──────────────────────────────────────────────
            # If the firmware has the drive-gated offset object (0x3008:8 / 0x3009:8), write a0/b0 there --
            # the firmware subtracts them ONLY while driving, so there is NO phantom at true idle (unlike the
            # always-on Bias fold below). This is the clean production fix. a0/b0 are the measured alpha/beta
            # intercepts in mA; the firmware does `iAlBe -= offset`, so store them as-measured (signed). Probe
            # the object raw (bypasses the EDS): if present -> use it and SKIP the Bias fold; Bias stays clean.
            _has_offset_reg = False
            try:
                self.node.sdo.upload(0x3008, 8)   # raw probe -- SDO-aborts (0x06020000) on pre-v3 firmware
                _has_offset_reg = True
            except Exception:
                _has_offset_reg = False
            if _has_offset_reg:
                def _i16le(v):
                    return max(-32768, min(32767, int(round(v)))).to_bytes(2, 'little', signed=True)
                self.node.sdo.download(0x3008, 8, _i16le(a0))
                self.node.sdo.download(0x3009, 8, _i16le(b0))
                self.node.sdo['Save']['Single'].raw = ((0x3008 << 8) | 0x08)   # persist Alpha offset
                self.node.sdo['Save']['Single'].raw = ((0x3009 << 8) | 0x08)   # persist Beta offset
                print("=== Drive-gated iSense offset: a0={:+.2f} b0={:+.2f} mA -> 0x3008:8 / 0x3009:8 ==="
                      .format(a0, b0))
                print("  Firmware applies it ONLY while driving (no idle phantom); saved to EEPROM. "
                      "Bias left clean.")
                if calAll == False:
                    self.Enable()
                return True
            # ── Legacy fallback (pre-v3 firmware, no register): fold into the ALWAYS-ON Bias. This injects
            # an equal/opposite phantom at true idle -- the register above is the clean fix. ────────────────

            # ma_per_ct from Alpha shunt (0x3008:5) + designed gain (0x3008:4), per the firmware.
            shunt = float(self.node.sdo[0x3008][5].raw)
            gain  = float(self.node.sdo[0x3008][4].raw)
            ma_per_ct = 3.3 / 4096.0 * 1000.0 / shunt * 1000.0 / gain * 1000.0
            gf_a = float(self.node.sdo[0x3008][6].raw)   # Gainfactor (Q4.12)
            gf_b = float(self.node.sdo[0x3009][6].raw)

            # dI/dBias = gainfactor/65536 * ma_per_ct (>0). To SUBTRACT the +a0 offset from the driven
            # reading, dBias = -a0 * 65536/(gainfactor*ma_per_ct). Per channel; Bias is Q12.4 counts.
            dbias_a = -a0 * 65536.0 / (gf_a * ma_per_ct)
            dbias_b = -b0 * 65536.0 / (gf_b * ma_per_ct)
            old_a = int(self.node.sdo['Alpha']['Bias'].raw)
            old_b = int(self.node.sdo['Beta']['Bias'].raw)
            new_a = int(round(old_a + dbias_a))
            new_b = int(round(old_b + dbias_b))

            print("=== Baseline Offset Fold: a0={:+.2f} b0={:+.2f} mA (drive-on residual) ===".format(a0, b0))
            print("  Alpha Bias {} -> {} ({:+.0f})   Beta Bias {} -> {} ({:+.0f})".format(
                old_a, new_a, dbias_a, old_b, new_b, dbias_b))
            self.node.sdo['Alpha']['Bias'].raw = new_a
            self.node.sdo['Beta']['Bias'].raw = new_b
            self.node.sdo['Save']['Single'].raw = ((0x3008 << 8) | 0x03)   # persist Alpha iSense
            self.node.sdo['Save']['Single'].raw = ((0x3009 << 8) | 0x03)   # persist Beta iSense
            # Over-corrects by a0/b0 at TRUE zero current (Bias is unconditional); the firmware drive-gated
            # offset is the clean fix. To revert, re-run Current Sense Bias (or a full cal) -- re-measures clean.
            print("  applied + saved  (revert = re-run Current Sense Bias).")
            if calAll == False:
                self.Enable()
            return True
        except Exception as _exc:
            if calAll:
                raise
            self._cal_fault(_exc)
            self.Enable()
            return False

    def calibrate_current_slope(self, event, calAll=False, force_sdo=False, quick=False):  # wxGlade: puckutilityapp_frame.<event_handler>
        """Calibrate the current-PROPORTIONAL alpha/beta current-sense offset (Current Sense Slope).

        With Bias (0 A) and Gain (counts/mA) already calibrated, the P4-16 still shows a residual
        current-sense offset whose magnitude grows linearly with load and points in a fixed
        direction.  This sweeps a set of load currents, measures the current-vector OFFSET
        (displacement of the |I| circle's centre) via an inverse-Park mean at each level, then fits
            off_alpha = a0 + kA*|I|      off_beta = b0 + kB*|I|
        The slope coefficients kA, kB (mA offset per mA current) are the stored model; firmware later
        subtracts kA*|I| from ialpha and kB*|I| from ibeta.  slope_mag=sqrt(kA^2+kB^2) (~0.146 on the
        P4-16), direction=atan2(kB,kA).  Run AFTER a fresh, warm Bias/Gain: the slope subtracts the
        stored Bias as its 0-A reference, so a stale/cold bias becomes a false intercept (and warns).
        In the full-cal sequence, place a Bias re-cal immediately before this step.  Firmware
        sign-check: after enabling the correction, re-run -- the offset-vs-current sweep should
        collapse toward the residual baseline; flip both stored coefficient signs if it grows."""
        if calAll == False and self.check_for_node() == False:
            return False
        # Firmware < 4.4.0 has no slope-correction object (0x3008:7 / 0x3009:7), so SKIP the whole cal
        # rather than drive the motor for a result it can't apply -- same firmware gate as the mag/encoder
        # compensation cal. (In a full cal, skip quietly so the sequence continues; standalone, tell the user.)
        if not self._fw_at_least(4, 4, 0):
            self._slope_stored = False   # nothing stored -> the baseline fold stays gated off
            print("Current Sense Slope cal SKIPPED: needs firmware v4.4.0+ (no 0x3008:7/0x3009:7 "
                  "slope-correction object on older firmware).")
            if calAll == False:
                self._prompt_ok("Firmware Too Old",
                    "Current Sense Slope calibration requires firmware v4.4.0 or later.\n"
                    "Skipping — older firmware has no slope-correction object.")
            return False
        if calAll == False:
            self._menu_idle_takeover()
            self.Disable()
        if self.ADC_ON == True:
            self.on_off_adc(self)       # quiet the ADC monitor -- it contends for SDO reads
            self.adcWasON = True
        else:
            self.adcWasON = False
        import math, os
        try:
            fp = None   # PDO fast-read handle; set up after the drive is energised (see below)
            i_peak   = self.node.sdo['Calibration']['i_peak'].raw
            i_cal    = self.node.sdo['Calibration']['i_cal'].raw
            alpha_gf = self.node.sdo['Alpha']['Gainfactor'].raw
            beta_gf  = self.node.sdo['Beta']['Gainfactor'].raw
            print("--- Current Sense Slope calibration (current-proportional alpha/beta offset) ---")
            print("Gainfactors in effect: Alpha={}  Beta={}  (i_cal {} mA)".format(
                alpha_gf, beta_gf, i_cal))

            # NOTE: no gear-ratio gate — the REAL-EFFECT gate at store time (below) handles every
            # assembly with one cal, using the sweep we ALREADY have (no re-measure, no added time):
            # a geared puck's offset grows cleanly with load; a direct-drive puck's id/iq collapse, so
            # mean|I| is IDENTICAL across levels and the offset direction is inconsistent -> gated out,
            # nothing stored, correction left OFF (it was cleared at the start of the cal).

            # EARLY ABORT: a valid gainfactor is ~4096 (Q4.12 = 1.0). A reset/garbage gain -- e.g. the
            # calibrated gain is wiped by a firmware flash and not restored by config -- makes that
            # channel read ~0, so the offset measurement is meaningless AND driving with a stored bad
            # slope is unsafe. Clear any stored slope (disable the correction) and bail BEFORE driving.
            if not (1024 <= alpha_gf <= 16384 and 1024 <= beta_gf <= 16384):
                print(">>> ABORT: gainfactor out of range (Alpha={}, Beta={}; ~4096 expected). Run "
                      "'Current Sense Gainfactor' or a full calibration FIRST.".format(alpha_gf, beta_gf))
                try:
                    self.node.sdo[0x3008][7].raw = 0
                    self.node.sdo[0x3009][7].raw = 0
                    self.node.sdo['Save']['Single'].raw = ((0x3008 << 8) | 0x07)
                    self.node.sdo['Save']['Single'].raw = ((0x3009 << 8) | 0x07)
                    print(">>> cleared 0x3008:7 / 0x3009:7 to 0 (slope correction disabled until a valid cal).")
                except Exception:
                    pass
                if self.ADC_ON == False and self.adcWasON == True:
                    self.on_off_adc(self)
                if calAll == False:
                    self.Enable()
                return

            # Disable any active slope correction BEFORE measuring, so the sweep sees the RAW
            # alpha/beta offset -- not a signal the firmware is already correcting.  Otherwise a
            # re-run measures only the residual and the stored coefficient drifts instead of
            # converging.  This also clears a stale/garbage stored value up front.
            try:
                self.node.sdo[0x3008][7].raw = 0
                self.node.sdo[0x3009][7].raw = 0
                self.node.sdo['Save']['Single'].raw = ((0x3008 << 8) | 0x07)
                self.node.sdo['Save']['Single'].raw = ((0x3009 << 8) | 0x07)
            except Exception:
                pass
            self._clear_offset_reg()   # v3+: same reason -- the drive-gated offset is applied during THIS
                                       # cal's own drive and would self-corrupt the measurement / overshoot.

            def _imag():
                _id = self.node.sdo['Motor']['id'].raw / 1000.0 * i_peak
                _iq = self.node.sdo['CurrentFeedback'].raw / 1000.0 * i_peak
                return (_id * _id + _iq * _iq) ** 0.5

            def _wait_settled(timeout=1.5):
                # Wait for the rotor to actually STOP after a theta_e step so back-EMF doesn't
                # modulate |I| and swamp the tiny alpha/beta offset (see measure_gain_ripple).
                # This cal is GATED to geared assemblies (rotor held), so it settles almost instantly
                # — a short poll + 2-stable check is enough; a longer one was pure per-angle overhead
                # (24 angles x 5 levels). If ever reused on a ringy rotor, raise these back.
                _p0 = self.node.sdo['Encoder']['RawPosition'].raw
                _t0 = time.time(); _stable = 0
                while time.time() - _t0 < timeout:
                    time.sleep(0.03); _yield()
                    _p1 = self.node.sdo['Encoder']['RawPosition'].raw
                    if abs(_p1 - _p0) <= 2:
                        _stable += 1
                        if _stable >= 2:
                            return
                    else:
                        _stable = 0
                    _p0 = _p1

            # PDO fast-read: stream id/iq at SYNC rate so we can average MANY more samples/angle in the
            # same wall-clock -> cleaner per-level offset -> tighter, repeatable a0 (which the auto
            # baseline-fold now depends on). Unified FastPDO keeps the drive ENABLED during SYNC (RPDO1
            # async prime) and captures via a raw callback (hardware-validated). Set up BEFORE energising
            # so its PRE-OP/remap happens first; robust SDO fallback if it can't come up. read()->[id,iq].
            _sync_ms = 0.5   # SYNC period (ms): 0.5 = 2 kHz. Faster rate -> faster sample bursts. The
                             # puck streamed 1 kHz fine; 2 kHz is the next step. 0.25 (4 kHz) is possible
                             # but watch CAN bus load / notifier-thread keep-up (frames would drop ->
                             # per-sample SDO fallback = slower, not faster).
            fp = None
            if force_sdo:
                # Retry path: skip the FastPDO after a prior SYNC/SDO comms glitch and use the reliable
                # (slower) per-sample SDO stream instead.
                print("  Slope on SDO path (FastPDO skipped after a prior comms glitch).")
            else:
                try:
                    from .fast_pdo import FastPDO
                    _fp = FastPDO(self.node, sync_period_ms=_sync_ms)
                    _fp.__enter__()
                    if getattr(_fp, 'ok', False):
                        fp = _fp
                        print("  PDO fast-read active (id/iq stream @ {:g} ms SYNC = {:.0f} Hz).".format(
                            _sync_ms, 1000.0 / _sync_ms))
                    else:
                        _fp.__exit__(None, None, None)
                        print("  PDO setup failed ({}); using SDO.".format(getattr(_fp, 'err', '?')))
                except Exception as _pe:
                    print("  PDO unavailable ({}); using SDO.".format(_pe))

            # Energise in voltage-angle mode; align+stop the rotor at theta=0 before ramping.
            self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE
            self.node.sdo["ControlWord"].raw = CLEAR_FAULT
            self.node.sdo["ControlWord"].raw = SHUTDOWN
            self.node.sdo["ControlWord"].raw = OP_ENABLED
            self.node.sdo["SetModeOfOperation"].raw = MODE_PHASE_VOLTAGE_ANGLE
            self.node.sdo['Theta_e'].raw = 0
            self.node.sdo['Motor']['ud'].raw = 2000   # seed to pull the rotor to theta=0
            _wait_settled()

            _N = 8 if fp else 10   # angles (BIDIRECTIONAL: each read both approach directions). 8 uniform
                                   # angles fully cancel the fundamental; with 48 clean PDO samples each,
                                   # fewer angles hold the offset quality while cutting settle count
                                   # (settles dominate the sweep time). SDO path keeps 10.
            _M = 48 if fp else 12  # samples/angle: PDO streams fast, so afford ~4x for a cleaner offset
                                   # (mean of more id/iq -> less per-level noise -> tighter a0); SDO 12
            import time as _time; _t_slope0 = _time.time()

            def _sweep_offset():
                # One full-electrical-cycle sweep at the current ud; rotor stopped each step.
                # Returns (mean|I|, off_alpha, off_beta) in mA -- the current-vector offset, i.e. the
                # displacement of the |I| circle's centre, via an inverse-Park mean (same convention
                # as measure_gain_ripple: the true rotating current averages to zero so the residual
                # inverse-Park mean IS the fixed sense offset).
                _theta = [int(round(-32768 + i * 65536.0 / _N)) for i in range(_N)]

                def _read_angle(_th):   # settle at _th (SYNC OFF), then stream _M id/iq (SYNC ON)
                    # Theta_e set + _wait_settled are SDO — they MUST run SYNC-OFF, else they collide
                    # with continuous SYNC (0x05040000/1). Harmless on a held (geared) rotor where
                    # _wait_settled returns at once, but FATAL on a DIRECT-DRIVE rotor that polls
                    # RawPosition many times while detenting (aborted the slope cal mid-sweep).
                    self.node.sdo['Theta_e'].raw = _th
                    _wait_settled()
                    if fp: fp.sync_on()                  # SYNC on ONLY for the streamed id/iq burst
                    _sid = _siq = 0.0
                    for _ in range(_M):
                        _v = fp.read() if fp else None   # coherent streamed [id, iq], else SDO
                        if _v is not None:
                            _sid += _v[0]; _siq += _v[1]
                        else:
                            _sid += self.node.sdo['Motor']['id'].raw
                            _siq += self.node.sdo['CurrentFeedback'].raw
                            time.sleep(0.003)
                        _yield()
                    if fp: fp.sync_off()                 # SYNC off before the next angle's SDO
                    return (_sid / _M) / 1000.0 * i_peak, (_siq / _M) / 1000.0 * i_peak

                # FORWARD pass (theta increasing) then REVERSE pass (theta decreasing) at the SAME
                # angles: each angle is thus approached from BOTH directions, so averaging the two
                # cancels the friction/backlash hysteresis (the rotor detents slightly off-angle by
                # approach direction) that wobbled kB/offset-direction run-to-run.
                _fwd = [None] * _N
                for _k in range(_N):
                    self.frame_statusbar.SetStatusText("slope offset fwd {}/{}".format(_k + 1, _N), 1)
                    _fwd[_k] = _read_angle(_theta[_k])
                _rev = [None] * _N
                for _k in range(_N - 1, -1, -1):
                    self.frame_statusbar.SetStatusText("slope offset rev {}/{}".format(_N - _k, _N), 1)
                    _rev[_k] = _read_angle(_theta[_k])

                _idl = [0.5 * (_fwd[i][0] + _rev[i][0]) for i in range(_N)]   # average both approaches
                _iql = [0.5 * (_fwd[i][1] + _rev[i][1]) for i in range(_N)]
                _mg  = [(_idl[i] * _idl[i] + _iql[i] * _iql[i]) ** 0.5 for i in range(_N)]
                _r   = [_theta[i] / 32768.0 * math.pi for i in range(_N)]
                _mnv = (sum(_mg) / _N) or 1.0
                _oa = sum(_idl[i] * math.cos(_r[i]) - _iql[i] * math.sin(_r[i]) for i in range(_N)) / _N
                _ob = sum(_idl[i] * math.sin(_r[i]) + _iql[i] * math.cos(_r[i]) for i in range(_N)) / _N
                return _mnv, _oa, _ob

            # (fp was set up above — the earlier freeze was the RPDO1-disable-during-SYNC bug, now fixed
            # in FastPDO; if setup failed, fp is None and _sweep_offset uses the SDO path.)

            # OFFSET vs CURRENT: sweep load levels; _ud is carried across the (increasing) levels so we
            # don't re-ramp from zero each time. 7 levels, WEIGHTED LOW: the offset saturates at high
            # current (nonlinear via duty), so extra low-current points anchor the intercept a0 that the
            # baseline fold consumes — three near/below 0.35*i_cal pin |I|=0 instead of extrapolating a
            # long way down. The PDO's cheap samples pay for the extra levels' settles.
            # Sweep 0.1 -> 1.0x i_cal (NOT above it): the offset fit only needs to cover the operating
            # point, and driving past i_cal just burns I^2*R (a big low-KV winding is watts). Weighted LOW
            # (3 levels below 0.35x) to pin the |I|=0 baseline the fold consumes.
            _i_top = int(i_cal)   # HARD ceiling on the sweep current -- never drive above the operating pt
            # QUICK trims the sweep to 4 levels (still weighted LOW: two points <=0.35*i_cal pin the a0
            # intercept the baseline fold consumes, one mid + top define the slope). A degree-1 fit stays
            # well-determined at 4 points and every downstream write/model/gate is unchanged — the ONLY
            # difference is level COUNT (per-level offset quality: same N angles, same M samples, same
            # fwd+rev hysteresis cancellation as Thorough). Dropping ~3 levels is where the ~18.5 s sweep's
            # time lives (per-level ramp + settles). Thorough (quick=False) keeps the full 7-level set,
            # byte-identical to before. beta R^2 is intrinsically noisy, so we do NOT trim below 4.
            _slope_fracs = ((0.12, 0.3, 0.6, 1.0) if quick
                            else (0.1, 0.2, 0.32, 0.5, 0.7, 0.85, 1.0))
            _levels = sorted(set(max(20, int(i_cal * _f)) for _f in _slope_fracs))
            _rows = []   # (meanI, off_alpha, off_beta) per level
            _ud = 500
            for _lvl in _levels:
                self.node.sdo['Theta_e'].raw = 0
                self.node.sdo['Motor']['ud'].raw = _ud
                _wait_settled()
                _cur = _imag() or 0.0
                # stop at the target OR at i_cal (whichever first) -- the i_cal clamp guards the overshoot
                # a geared rotor's back-EMF can inflate, so we never ramp ud past the operating current.
                while _cur < _lvl and _cur < _i_top and _ud < 16000:
                    _ud += 300
                    self.node.sdo['Motor']['ud'].raw = _ud
                    time.sleep(0.02); _yield()
                    _cur = _imag() or 0.0
                _wait_settled()
                _mnv, _oa, _ob = _sweep_offset()
                _rows.append((_mnv, _oa, _ob))

            self.node.sdo['Motor']['ud'].raw = 0
            self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE
            print("  (slope sweep took {:.1f} s)".format(_time.time() - _t_slope0))
            if fp:
                fp.__exit__(None, None, None); fp = None   # stop SYNC + restore callbacks

            # --- FIT: off_alpha = a0 + kA*|I|,  off_beta = b0 + kB*|I|  (degree-1 least squares) ---
            import numpy as np
            _mi = [r[0] for r in _rows]
            _oa = [r[1] for r in _rows]
            _ob = [r[2] for r in _rows]

            def _fit1(_x, _y):
                _xa = np.asarray(_x, dtype=float); _ya = np.asarray(_y, dtype=float)
                _slope, _icpt = np.polyfit(_xa, _ya, 1)
                _yp = _slope * _xa + _icpt
                _sr = float(np.sum((_ya - _yp) ** 2))
                _st = float(np.sum((_ya - _ya.mean()) ** 2))
                _r2 = 1.0 - _sr / _st if _st > 0 else 0.0
                return float(_slope), float(_icpt), _r2

            kA, a0, r2A = _fit1(_mi, _oa)
            kB, b0, r2B = _fit1(_mi, _ob)
            slope_mag = (kA * kA + kB * kB) ** 0.5
            direction_deg = math.degrees(math.atan2(kB, kA))
            # Stash the fitted drive-on baseline (a0/b0, mA) for the opt-in baseline-fold TEST.
            self._slope_a0, self._slope_b0 = a0, b0

            # per-level offset ANGLE spread (circular std) -- a clean fixed-direction slope keeps it low
            _angs = [math.atan2(r[2], r[1]) for r in _rows]
            _msin = sum(math.sin(a) for a in _angs); _mcos = sum(math.cos(a) for a in _angs)
            _mean_ang = math.atan2(_msin, _mcos)
            _devs = [((math.degrees(a - _mean_ang) + 180.0) % 360.0) - 180.0 for a in _angs]
            angle_spread = (sum(d * d for d in _devs) / len(_devs)) ** 0.5 if _devs else 0.0

            # --- PRINT block ---
            print("--- per-level current-sense offset ---")
            print("   mean|I|(mA)   off_alpha(mA)   off_beta(mA)   angle(deg)")
            for r in _rows:
                print("   {:9.1f}     {:10.2f}     {:10.2f}     {:8.1f}".format(
                    r[0], r[1], r[2], math.degrees(math.atan2(r[2], r[1]))))
            # --- MODEL SUMMARY: off(|I|) = OFFSET + SLOPE·|I|.  Show BOTH terms explicitly so it's clear
            # which is doing the work: small motors (P4-16) carry a real current-proportional SLOPE; large
            # motors (P4-37) are ~flat and the fixed drive-on OFFSET (a0/b0) is the whole correction.  ---
            _base_mag      = (a0 * a0 + b0 * b0) ** 0.5
            _base_dir      = math.degrees(math.atan2(b0, a0))
            _slope_at_ical = slope_mag * i_cal                        # mA the SLOPE term removes at i_cal
            _off_op        = (((a0 + kA * i_cal) ** 2) + ((b0 + kB * i_cal) ** 2)) ** 0.5   # net offset at i_cal
            _slope_sig     = slope_mag >= 0.005                       # >= 0.5%/A -> a real proportional term
            print(">>> current-sense offset model:  off(|I|) = OFFSET + SLOPE·|I|")
            print(">>>   SLOPE  (∝ current):    kA={:+.4f} kB={:+.4f}   |k|={:.4f} mA/mA ({:.2f}%/A)   "
                  "-> {:.1f} mA @{} mA   [{}]".format(
                      kA, kB, slope_mag, slope_mag * 100.0, _slope_at_ical, i_cal,
                      "SIGNIFICANT" if _slope_sig else "~flat, negligible on this motor"))
            print(">>>          stored Q4.12: kA*4096={:+d}  kB*4096={:+d}   -> 0x3008:7 / 0x3009:7".format(
                int(round(kA * 4096)), int(round(kB * 4096))))
            print(">>>   OFFSET (fixed drive-on): a0={:+.2f} b0={:+.2f} mA   |{:.1f}| mA @{:+.0f}°   [{}]".format(
                a0, b0, _base_mag, _base_dir,
                "DOMINANT term here" if (_base_mag >= max(_slope_at_ical, 8.0)) else "secondary"))
            print(">>>          -> drive-gated register 0x3008:8 / 0x3009:8 (fw v3+), else bias fold")
            print(">>>   net offset at {} mA = {:.1f} mA   |   fit R² α={:.3f} β={:.3f}   dir-spread {:.1f}°".format(
                i_cal, _off_op, r2A, r2B, angle_spread))
            if angle_spread > 20.0:
                print(">>> WARNING: offset direction not consistent ({:.1f}° spread) -- not a clean "
                      "fixed-direction slope.".format(angle_spread))

            # --- SANITY GATE: never store garbage. A reset/bad gainfactor (e.g. after a firmware
            #     flash) makes that channel read ~0, collapsing the |I| circle so the measured
            #     "offset" equals the current -> kA~=1.0 with a perfect R^2. Storing that cripples
            #     the FOC (subtracts ~100% of |I|). Refuse it. ---
            _gain_ok  = (1024 <= alpha_gf <= 16384 and 1024 <= beta_gf <= 16384)  # ~4096 = 1.0 (Q4.12)
            _slope_ok = (slope_mag <= 0.5)   # an iSense offset can't be >50% of the current
            # REAL-EFFECT gate (assembly-agnostic, NO re-measure — reuses this sweep): a genuine
            # current-proportional slope needs the offset to TRACK load.  Direct-drive collapses id/iq,
            # so mean|I| is frozen (identical across levels) and the offset direction is inconsistent
            # (huge angle-spread); a geared puck's |I| spreads across the level range with a consistent
            # direction.  (Geared here: |I| 187->452, spread 2.8 deg.  Direct-drive: |I| ~149 flat,
            # spread 101 deg.)  Both must hold to store; else nothing is written (slope stays cleared).
            _i_vals    = [r[0] for r in _rows]
            _i_spread  = max(_i_vals) - min(_i_vals)
            _effect_ok = (_i_spread > 0.25 * max(_i_vals) and angle_spread < 30.0)
            if not (_gain_ok and _slope_ok and _effect_ok):
                self._slope_stored = False   # untrustworthy (e.g. direct-drive) -> no auto baseline fold
                print(">>> NOT STORING (slope left OFF — it was cleared at the start of this cal):")
                if not _gain_ok:
                    print(">>>   gainfactor out of range (Alpha={}, Beta={}; ~4096 expected). Run "
                          "'Current Sense Gainfactor' / a full calibration FIRST.".format(alpha_gf, beta_gf))
                if not _slope_ok:
                    print(">>>   slope_mag={:.3f} absurd (offset ~= current -> a channel reads ~0, "
                          "almost always a bad gain).".format(slope_mag))
                if not _effect_ok:
                    print(">>>   NO REAL EFFECT: offset does not track load (|I| spread {:.0f} mA over "
                          "levels, direction spread {:.0f} deg) — degenerate, e.g. direct-drive id/iq "
                          "collapse. Not a geared assembly; slope left OFF.".format(
                              _i_spread, angle_spread))
            else:
                # --- STORE (guarded: OD entries may not exist in firmware yet) ---
                try:
                    self.node.sdo[0x3008][7].raw = int(round(kA * 4096))   # Q4.12, signed (I16)
                    self.node.sdo[0x3009][7].raw = int(round(kB * 4096))
                    self.node.sdo['Save']['Single'].raw = ((0x3008 << 8) | 0x07)   # persist Alpha slope
                    self.node.sdo['Save']['Single'].raw = ((0x3009 << 8) | 0x07)   # persist Beta slope
                    print(">>> stored to 0x3008:7 / 0x3009:7 (and saved to EEPROM).")
                    self._slope_stored = True    # trustworthy geared fit -> auto baseline fold may run
                except Exception as _se:
                    self._slope_stored = False
                    print(">>> firmware OD entries 0x3008:7/0x3009:7 not present yet -- coefficients "
                          "above; wire them once firmware adds them. ({})".format(_se))

            # --- PLOT ---
            try:
                import matplotlib
                matplotlib.use('Agg')
                import matplotlib.pyplot as plt
                fig, ax = plt.subplots(figsize=(8, 4))
                ax.plot(_mi, _oa, 'o', color='C0', label='off_alpha')
                ax.plot(_mi, _ob, 's', color='C1', label='off_beta')
                _xf = [min(_mi), max(_mi)] if _mi else [0, 1]
                ax.plot(_xf, [a0 + kA * x for x in _xf], '--', color='C0', lw=0.8,
                        label='fit a: {:+.0f}+{:.3f}|I|'.format(a0, kA))
                ax.plot(_xf, [b0 + kB * x for x in _xf], '--', color='C1', lw=0.8,
                        label='fit b: {:+.0f}+{:.3f}|I|'.format(b0, kB))
                ax.set_xlabel('current |I| (mA)'); ax.set_ylabel('current-sense offset (mA)')
                ax.set_title('Current Sense Slope: mag={:.3f} mA/mA, dir={:.0f} deg'.format(
                    slope_mag, direction_deg))
                ax.grid(True, alpha=0.3); ax.legend(fontsize=8)
                from ..paths import session_path
                import datetime as _dt
                _pc = None
                try:
                    _pc = int(self.node.sdo[0x1018][2].raw)
                except Exception:
                    pass
                _model = getattr(self, '_PRODUCT_CODE_MODELS', {}).get(_pc, 'unknown').replace(' ', '_')
                _pfx = 'node{}_{}_'.format(getattr(self.node, 'id', '?'), _model)
                _ts = _dt.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
                _path = session_path('isense/images/{}current_sense_slope_{}.png'.format(_pfx, _ts))
                os.makedirs(os.path.dirname(_path), exist_ok=True)
                fig.savefig(_path, dpi=110, bbox_inches='tight'); plt.close(fig)
                print("Plot saved: {}".format(_path))
            except Exception as _pe:
                print("(plot skipped: {})".format(_pe))

            self.frame_statusbar.SetStatusText(
                "slope kA={:+.3f} kB={:+.3f} (mag {:.3f})".format(kA, kB, slope_mag), 1)
            if self.ADC_ON == False and self.adcWasON == True:
                self.on_off_adc(self)   # restore ADC monitor
            # Re-assert IDLE as the LAST mode set (the ADC-monitor restore above runs a brief torque
            # test that re-enables the drive) and sync the GUI mode selector, so the puck is left
            # cleanly idle instead of energised in / showing a drive mode.
            try:
                self.node.sdo['Motor']['ud'].raw = 0
                self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE
                self.choice_test.SetSelection(0)
                self.lastMode = 0
            except Exception:
                pass
            if calAll == False:
                self.Enable()
        except Exception as _exc:
            try:
                if fp: fp.__exit__(None, None, None)   # stop SYNC before anything else
            except Exception:
                pass
            try:
                self.node.sdo['Motor']['ud'].raw = 0
                self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE
            except Exception:
                pass
            if self.ADC_ON == False and self.adcWasON == True:
                self.on_off_adc(self)
            if calAll:
                raise
            self._cal_fault(_exc)
            self.Enable()

    def calibrate_slope_spin_test(self, event, calAll=False):  # wxGlade: <event_handler>
        """Assembly-agnostic current-sense SLOPE — stepped HOLD + raw alpha/beta circle fit.

        A free rotor won't stay put for a swept field: a gearbox is the only thing that pins it for
        the stepped cal, and it SYNCHRONIZES with (or, under RPDO3 open-loop, simply isn't driven by)
        a continuously-rotated field.  So HOLD the field at discrete angles via plain SDO (the drive
        path the working cals use): the rotor magnetically DETENTS (stops) at each angle, DC current
        flows (no back-EMF), and the RAW alpha/beta stator current there is a point on the current
        circle.  Circle-fit -> center = sense offset, radius = |I|; several drive levels -> slope
        kA,kB.  Measures RAW alpha/beta (not Park'd id/iq, which collapse when the rotor follows the
        field).  No gearbox needed — the rotor detents itself."""
        if self.check_for_node() == False:
            return False
        if calAll == False:
            self._menu_idle_takeover()
        self.Disable()
        if self.ADC_ON:
            self.on_off_adc(self); self.adcWasON = True   # quiet ADC monitor (SDO contention)
        else:
            self.adcWasON = False
        import numpy as np
        n = self.node
        try:
            i_peak     = n.sdo['Calibration']['i_peak'].raw
            i_cal      = n.sdo['Calibration']['i_cal'].raw
            try:
                i_cont = int.from_bytes(n.sdo.upload(0x3011, 8), 'little', signed=False)  # I_cont, mA
            except Exception:
                i_cont = 0
            alpha_gf   = n.sdo['Alpha']['Gainfactor'].raw
            beta_gf    = n.sdo['Beta']['Gainfactor'].raw
            alpha_bias = float(n.sdo['Alpha']['Bias'].raw)
            beta_bias  = float(n.sdo['Beta']['Bias'].raw)
            # a_sens ~2.96 counts/mA is already in the firmware's Filtered units (the gain cal
            # measures counts/mA as Filtered-bias vs id directly) — do NOT rescale by 16.  Filtered
            # and Bias are read in the SAME units, so (Filtered - bias) / a_sens is mA.
            _A_SENS = 2.96
            a_sens  = _A_SENS * alpha_gf / 4096.0
            b_sens  = _A_SENS * beta_gf  / 4096.0
            N_ANG, N_AVG = 12, 12   # 12 angles is plenty for a circle fit; halves the settle time
            _UD_CAP = 16000
            _i_ceil = 0.9 * i_cont if i_cont > 0 else float('inf')   # never drive above continuous
            _levels = sorted(set(max(40, int(i_cal * _f)) for _f in (0.4, 0.7, 1.0, 1.3)))
            _levels = [l for l in _levels if l <= _i_ceil] or [int(min(_levels[0], _i_ceil))]

            print("=" * 70)
            print("  CURRENT SENSE SLOPE — stepped-HOLD raw alpha/beta (assembly-agnostic)")
            print("  {} angles x {} avg/level; levels {} mA (I_cont={} mA)".format(
                N_ANG, N_AVG, _levels, i_cont))
            print("=" * 70)

            def _ab_ma():   # raw alpha/beta CURRENT in mA (sense offset included)
                _a = (n.sdo['Alpha']['Filtered'].raw - alpha_bias) / a_sens
                _b = (n.sdo['Beta']['Filtered'].raw  - beta_bias)  / b_sens
                return _a, _b

            def _imag_ab():
                _a, _b = _ab_ma(); return (_a * _a + _b * _b) ** 0.5

            def _wait_settled(timeout=1.5):
                # wait for the rotor to actually STOP (detent) after a Theta_e step
                _p0 = n.sdo['Encoder']['RawPosition'].raw
                _t0 = time.time(); _st = 0
                while time.time() - _t0 < timeout:
                    time.sleep(0.05); _yield()
                    _p1 = n.sdo['Encoder']['RawPosition'].raw
                    if abs(_p1 - _p0) <= 2:
                        _st += 1
                        if _st >= 3:
                            return
                    else:
                        _st = 0
                    _p0 = _p1

            def _circle_fit(_xs, _ys):
                # Kasa fit: x^2+y^2 = 2a·x + 2b·y + c  ->  center (a,b), R = sqrt(c+a^2+b^2)
                _x = np.asarray(_xs, float); _y = np.asarray(_ys, float)
                _M = np.column_stack([2.0 * _x, 2.0 * _y, np.ones_like(_x)])
                _sol, *_rest = np.linalg.lstsq(_M, _x * _x + _y * _y, rcond=None)
                _cx, _cy, _c = _sol
                return float(_cx), float(_cy), float((max(_c + _cx * _cx + _cy * _cy, 0.0)) ** 0.5)

            # Clear any active slope correction so we read the RAW offset (not a residual).
            try:
                n.sdo[0x3008][7].raw = 0; n.sdo[0x3009][7].raw = 0
                n.sdo['Save']['Single'].raw = ((0x3008 << 8) | 0x07)
                n.sdo['Save']['Single'].raw = ((0x3009 << 8) | 0x07)
            except Exception:
                pass

            # Energise; align + detent the rotor at theta_e = 0 via plain SDO (this DRIVES current).
            n.sdo["ControlWord"].raw = CLEAR_FAULT
            n.sdo["ControlWord"].raw = SHUTDOWN
            n.sdo["ControlWord"].raw = OP_ENABLED
            n.sdo["SetModeOfOperation"].raw = MODE_PHASE_VOLTAGE_ANGLE
            n.sdo['Motor']['uq'].raw = 0
            n.sdo['Theta_e'].raw = 0
            n.sdo['Motor']['ud'].raw = 2000
            _wait_settled()

            def _sweep_level(_lvl):
                # Establish current at theta_e=0 (rotor detents), then step a full electrical cycle
                # holding+settling at each angle; return (ud, cx, cy, R) — the raw alpha/beta circle.
                n.sdo['Theta_e'].raw = 0
                _wait_settled()
                _ud = int(n.sdo['Motor']['ud'].raw)
                _t0 = time.time()
                while _ud < _UD_CAP and time.time() - _t0 < 6.0:
                    _ud += 300
                    n.sdo['Motor']['ud'].raw = _ud
                    _wait_settled()   # FULL settle before gauging (a partial settle under-reads the
                                      # ringing rotor and overshoots the target).
                    if _imag_ab() >= min(_lvl, _i_ceil):   # stop at target OR the continuous ceiling
                        break
                _xs, _ys = [], []
                for _th in _theta:
                    n.sdo['Theta_e'].raw = _th
                    _wait_settled()
                    _sa = _sb = 0.0
                    for _ in range(N_AVG):
                        _a, _b = _ab_ma(); _sa += _a; _sb += _b
                        time.sleep(0.003); _yield()
                    _xs.append(_sa / N_AVG); _ys.append(_sb / N_AVG)
                _cx, _cy, _R = _circle_fit(_xs, _ys)
                return _ud, _cx, _cy, _R

            _theta = [int(round(-32768 + i * 65536.0 / N_ANG)) for i in range(N_ANG)]
            _rows = []
            print("  {:>9} {:>7} {:>11} {:>11} {:>9}".format(
                "target", "ud", "off_a(mA)", "off_b(mA)", "|I|(mA)"))
            for _lvl in _levels:
                _ud, _cx, _cy, _R = _sweep_level(_lvl)
                _rows.append((_R, _cx, _cy))
                print("  {:>9d} {:>7d} {:>11.2f} {:>11.2f} {:>9.1f}".format(_lvl, _ud, _cx, _cy, _R))

            # --- fit offset-vs-|I|:  off_alpha = a0 + kA*|I|,  off_beta = b0 + kB*|I| ---
            print("-" * 70)
            _maxI = max((r[0] for r in _rows), default=0.0)
            if len(_rows) < 2 or _maxI < 10.0:
                print(">>> current never established (max |I|={:.1f} mA up to ud={}). The rotor isn't "
                      "holding current at a detented angle — needs a higher ud or a brief closed-loop "
                      "hold. Paste this and we'll adjust.".format(_maxI, _UD_CAP))
            else:
                _mi = np.asarray([r[0] for r in _rows]); _oa = np.asarray([r[1] for r in _rows])
                _ob = np.asarray([r[2] for r in _rows])
                kA, a0 = (float(v) for v in np.polyfit(_mi, _oa, 1))
                kB, b0 = (float(v) for v in np.polyfit(_mi, _ob, 1))
                slope_mag = (kA * kA + kB * kB) ** 0.5
                direction = math.degrees(math.atan2(kB, kA))
                print(">>> slope: kA={:+.4f}  kB={:+.4f}  |slope|={:.4f} mA/mA  dir={:+.0f} deg".format(
                    kA, kB, slope_mag, direction))
                print(">>> baseline intercept: a0={:+.1f} mA  b0={:+.1f} mA".format(a0, b0))

                # MEASURE-ONLY — DO NOT STORE.  On the same geared board this raw-alpha/beta value
                # (kA~+0.13) makes the motor WORSE, while the original inverse-Park "Current Sense
                # Slope" cal (kA~-0.05) makes it smooth — so this method does NOT yet match the frame /
                # sign / reference the firmware's slope correction expects (different sign AND ~2.6x
                # magnitude, and a much smaller baseline, so it's not a pure sign flip).  The slope was
                # CLEARED at the start of this run and is left cleared (correction OFF).  For an actual,
                # working correction on a geared setup, use the original "Current Sense Slope" cal.
                print(">>> MEASURE-ONLY (NOT stored, correction left OFF): kA={:+.4f} kB={:+.4f}.".format(
                    kA, kB))
                print(">>>   This raw-alpha/beta value does not match the firmware's slope convention "
                      "(it degrades the motor) — use the original 'Current Sense Slope' cal to correct.")
            print("=" * 70)
        except Exception as _e:
            print("Slope stepped-hold error: {}".format(_e))
        finally:
            try:
                n.sdo['Motor']['ud'].raw = 0
                n.sdo["SetModeOfOperation"].raw = MODE_IDLE
            except Exception:
                pass
            if self.ADC_ON == False and self.adcWasON == True:
                self.on_off_adc(self)
            if calAll == False:
                self.Enable()

    def calibrate_islope(self, event):  # wxGlade: wxp3_frame.<event_handler>
        print("Event handler 'calibrate_islope' not implemented!")
        event.Skip()

    def calibrate_enczero(self, event, calAll=False, _upd=None, quick=False):  # wxGlade: wxp3_frame.<event_handler>
        # REVOLUTION-SWEEP electrical-zero calibration (ezero.py, shared with p4gui).
        #
        # The spin-through this replaces swept Theta_e +-22.5 deg around 0 at ONE spot and averaged
        # the two crossings. That cancels friction but not the encoder nonlinearity or cogging at
        # that spot: on a P4-42 single points scatter +-20 deg electrical about the true zero, and
        # a one-spot calibration left e_zero 21 deg off (the back-EMF of a zero-current coast,
        # p4gui tools/bench/enc_latency_coast.py, measured it; the sweep then agreed within ~1 deg).
        #
        # This steps the field through every electrical cycle of one mechanical revolution, forward
        # then back, and takes the circular mean of e_zero over all the settled points: the periodic
        # errors average out and friction cancels between the directions. The same sweep gives
        # e_polarity and checks the pole count (the rotor must turn once per revolution of field).
        # The rotor turns ONE FULL REVOLUTION each way.
        #
        # quick=True (calibrate_quick): 8 steps per electrical cycle instead of 16, ~2x faster,
        # within ~1 deg on the P4-42.
        if calAll == False:
          if self.check_for_node() == False:
            return False
          self._menu_idle_takeover()
          self.Disable()
        quick_test = self.choice_test.GetSelection()
        if quick_test != 0:
            self.lastMode = 0
            print("Setting Mode = IDLE")
            self.choice_test.SetSelection(0)
            self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE

        if self.ADC_ON == True:
            self.adcWasON = True
            self.on_off_adc(self)
        else:
            self.adcWasON = False

        if calAll == False:
            self.OnStartTask(None)
            _upd = lambda v: self.UpdateUI(v)
        if _upd is None:
            _upd = lambda v: None

        self.frame_statusbar.SetStatusText("Calibrating encoder zero (one revolution each way)...", 1)
        self.frame_statusbar.Update()
        _yield()

        def _finish():
            if self.ADC_ON == False and self.adcWasON == True:
                self.on_off_adc(self)
            if calAll == False:
                self.OnTaskComplete()
                self.Enable()

        try:
          self.node.sdo["ControlWord"].raw = CLEAR_FAULT
          try:
              r = ezero.sweep(self.node, steps=8 if quick else 16,
                              progress=lambda f: _upd(int(f * 95)), sleep=_sleep_responsive)
          except ezero.EZeroError as exc:
              print('Encoder Zero Failed! {}'.format(exc))
              cal_torque = (self.node.sdo['Calibration']['i_cal'].raw
                            * self.node.sdo['Calibration']['kt'].raw / 1000)
              msg = "Encoder Zero Failed!\n\n{}\n\nNothing was stored." \
                    "\n\nDebugging steps:" \
                    "\n- Ensure proper configuration file has been loaded (poles, i_cal)" \
                    "\n- Verify output friction is less than cal torque for the motor ({:.0f}mNm)" \
                    "\n\nWould you like to continue calibration?".format(exc, cal_torque)
              continue_cal = self._prompt('Warning!', msg)
              _finish()
              return continue_cal

          previous_polarity = self.node.sdo['Calibration']['e_polarity'].raw
          previous_zero = self.node.sdo['Calibration']['e_zero'].raw
          cpe = r['cts_per_elec_cycle']
          print("Electrical polarity = {} (was {})".format(r['e_polarity'], previous_polarity))
          print("Electrical zero = {} (was {}, {:+.1f} deg elec)".format(
              r['e_zero'], previous_zero, ezero.wrap(r['e_zero'] - previous_zero, cpe) * 360.0 / cpe))
          print("  friction split {:.1f} deg elec; single points scatter +-{:.1f} deg "
                "(rms {:.1f}) about it".format(r['friction_split_deg'], r['spread_max_deg'],
                                               r['spread_rms_deg']))
          self.node.sdo['Calibration']['e_polarity'].raw = r['e_polarity']
          self.node.sdo['Save']['Single'].raw = ((0x3011 << 8) | 0x02)   # Save e_polarity to EE
          self.node.sdo['Calibration']['e_zero'].raw = r['e_zero']
          self.node.sdo['Save']['Single'].raw = ((0x3011 << 8) | 0x01)   # Save e_zero to EE
          _upd(100)
          _finish()
          return True

        except Exception as _exc:
            if calAll:
                raise
            self._cal_fault(_exc)
            self.Enable()

    def calibrate_encdir(self, event):  # wxGlade: wxp3_frame.<event_handler>
        print("Event handler 'calibrate_encdir' not implemented!")
        event.Skip()

    def calibrate_enclag(self, event, calAll=False):  # wxGlade: wxp3_frame.<event_handler>
        # MAX-VELOCITY (FIELD-WEAKENING) encoder-lag calibration.
        #
        # The commutation-DELAY lag (what a ud/id-null sweep tries to measure) is only ~3-4 deg even at
        # top speed -- below the current-sense noise -- so it is not measurable and barely matters. What
        # DOES matter for TOP SPEED: advancing LagFactor advances the commutation angle, which FIELD-
        # WEAKENS the machine (injects -d current, cuts effective back-EMF) and RAISES the voltage-limited
        # top speed. LagFactor is a SPEED-PROPORTIONAL advance (enc.est = raw + enc_inc*lag/256, enc_inc ~
        # speed), so a fixed value auto-scales: big advance at high speed (FW), negligible at low speed
        # (so it doesn't reintroduce the low-speed asymmetry that e_zero fixes). We find the LagFactor
        # that maximizes achievable velocity, then back off a margin.
        #
        # Revives the ORIGINAL max-velocity sweep (git HEAD, commented out) with the guards it lacked (it
        # ran into FW, lost sync, browned out the bus 0x3220). Each step checks fault, bus sag, over-
        # current, and velocity COLLAPSE. IMPORTANT: the velocity-max detection IGNORES steps where the
        # i2t transient inflates current (|I| >> i_cont) -- on the FIRST direction the current hasn't
        # de-rated yet and spikes to i_peak, which otherwise corrupts the early "best". This is a TOP-
        # SPEED optimization / stress test -- watch it, and confirm SUSTAINED stability at the banked lag.
        # Firmware from stm32 a896b09 on has 0x3013,7: 0x3013,5 is then no longer an angle-advance
        # knob but the fixed (us) part of the measured encoder latency (0x3013,7 is the PWM-period
        # part), set from the configuration CSV (p4gui tools/bench/enc_latency_coast.py measures
        # both). Field weakening is the FWLIM regulator's job there. Writing a Q8.8 advance into it
        # would put hundreds of us of error into commutation, so this only runs on older firmware.
        try:
            self.node.sdo.upload(0x3013, 7)
            has_latency_split = True
        except Exception:
            has_latency_split = False
        if has_latency_split:
            self._prompt_ok('Encoder Lag',
                            'This firmware measures encoder latency instead (0x3013,5 in us and '
                            '0x3013,7 in PWM periods, from the configuration file).\n\n'
                            'Encoder Lag calibration only applies to older firmware; nothing was changed.')
            return True

        if self.ADC_ON == True:
            self.adcWasON = True
            self.on_off_adc(self)
        else:
            self.adcWasON = False

        if calAll == False:
          if self.check_for_node() == False:
            return False
          self.Disable()

        self.frame_statusbar.SetStatusText("Calibrating Encoder Lag (max-velocity / field-weakening)...", 1)
        self.frame_statusbar.Update()
        _yield()

        try:
            if calAll == False:
                self._menu_idle_takeover()   # stop an active puck + reset the drive selector to Idle

            i_peak = self.node.sdo['Calibration']['i_peak'].raw
            enc_resolution = self.node.sdo['EncoderConfig']['Resolution'].raw
            try:
                i_cont = int.from_bytes(self.node.sdo.upload(0x3011, 8), 'little', signed=False)
            except Exception:
                i_cont = 0
            if i_cont <= 0:
                i_cont = max(1, int(0.3 * i_peak))
            try:
                max_vel = int(self.node.sdo['max_velocity'].raw)          # cts/s, the motor's top-speed cap
            except Exception:
                max_vel = 0
            if max_vel <= 0:
                max_vel = int(round(15000.0 / 60.0 * enc_resolution))     # fallback ~15k RPM
            try:
                BUS_FLOOR = int(self.node.sdo['Object2384']['AmplifierMinVoltage'].raw)   # 0.1 V
            except Exception:
                BUS_FLOOR = 250

            LAG_HARD_CAP  = 450    # FW advance ceiling. Was 256 (=1.0 control-cycle in Q8.8); LagFactor
                                   # is a FIELD-WEAKENING advance, not a delay fix, and the FW optimum on
                                   # a fast motor sits well past one cycle -- measured ~370 on the P4-16,
                                   # stable to 380+. Raised to reach it. (If a high-lag runaway recurs,
                                   # lower this -- the old 256 was set after an ~290 runaway.)
            LAG_STEP      = 2
            SETTLE        = 0.10
            SPIN_S        = 2.5    # spin-up at commanded max velocity
            SETTLE_TIMEOUT= 15.0   # s max to wait for the i2t transient to de-rate before sweeping
            N_AVG         = 6
            I_TRANSIENT   = 1.3 * i_cont         # ABOVE this = i2t/spin-up transient -> excluded from "best"
            I_CLAMP       = int(i_peak)          # hard overcurrent STOP (rated peak; i2t is the backup)
            COLLAPSE_FRAC = 0.85                 # vel < this*best -> over-weakened/sync loss -> stop
            ARM_LAG       = 6
            GAIN_MIN      = 1.02                 # need >=2% velocity gain vs low-lag to call it real FW
            MARGIN_LAGS   = 20                   # ROLLOVER case: bank this far BELOW the peak-velocity lag
            SUSTAIN_MARGIN = 8                    # CAP-LIMITED case: bank this far below the max reliable lag

            orig_lag = self.node.sdo[0x3013][5].raw

            def _imag_mA():
                _id = self.node.sdo['Motor']['id'].raw / 1000.0 * i_peak
                _iq = self.node.sdo['CurrentFeedback'].raw / 1000.0 * i_peak
                return (_id * _id + _iq * _iq) ** 0.5

            def _sweep_fw(vel_sign):
                """Advance LagFactor at commanded max velocity; return the full [(lag, vel, imag)] series.
                Safety stops (fault, bus sag, overcurrent, velocity collapse) act online."""
                self.node.sdo[0x3013][5].raw = 0
                self.node.sdo['TargetVelocity'].raw = int(vel_sign * max_vel)
                _sleep_responsive(SPIN_S)
                # Let the i2t transient DE-RATE before sweeping. Commanding max velocity saturates the
                # loop, so the FIRST direction pulls toward i_peak while the i2t integrator charges (~10 s);
                # sweeping through that corrupts the low-lag velocities. Poll |I| until it settles to the
                # sustained (~i_cont) level, THEN sweep clean from lag 0. (2nd direction is already warm.)
                print("  waiting for the i2t current to settle (de-rate) before the sweep...")
                _t0 = time.time()
                while time.time() - _t0 < SETTLE_TIMEOUT:
                    if _imag_mA() <= I_TRANSIENT:
                        break
                    time.sleep(0.3); _yield()
                else:
                    print("  (current still elevated after {:.0f}s -- proceeding; transient steps are "
                          "excluded anyway)".format(SETTLE_TIMEOUT))
                series = []; run_max = 0.0; lag = 0
                while lag <= LAG_HARD_CAP:
                    self.node.sdo[0x3013][5].raw = lag
                    time.sleep(SETTLE); _yield()
                    sw = self.node.sdo['StatusWord'].raw
                    if sw & 0x08:
                        print("  drive FAULT (StatusWord={:#06x}) at lag {} -> stop".format(sw, lag)); break
                    busv = self.node.sdo['Amplifier']['BusVoltage'].raw
                    acc_v = acc_i = 0.0
                    for _ in range(N_AVG):
                        acc_v += abs(self.node.sdo['VelocityFeedback'].raw)
                        acc_i += _imag_mA()
                        _yield()
                    vel = acc_v / N_AVG; imag = acc_i / N_AVG
                    _tag = "" if imag <= I_TRANSIENT else "  (i2t transient, excluded)"
                    print("  dir {:+d}  Lag: {:3d}  vel: {:9.0f} ({:6.0f} RPM)  |I|: {:6.1f} mA  bus: {:.1f} V{}"
                          .format(vel_sign, lag, vel, vel * 60.0 / enc_resolution, imag, busv / 10.0, _tag))
                    if busv < BUS_FLOOR:
                        print("  bus sag {:.1f} V (< {:.1f}) at lag {} -> stop".format(
                              busv / 10.0, BUS_FLOOR / 10.0, lag)); break
                    if imag > I_CLAMP:
                        print("  overcurrent {:.0f} mA (> {}) at lag {} -> stop".format(imag, I_CLAMP, lag)); break
                    if lag >= ARM_LAG and run_max > 0 and vel < COLLAPSE_FRAC * run_max:
                        print("  velocity COLLAPSE ({:.0f} < {:.0f}% of {:.0f}) at lag {} -> over-weakened, stop"
                              .format(vel, COLLAPSE_FRAC * 100.0, run_max, lag)); break
                    if imag <= I_TRANSIENT and vel > run_max:
                        run_max = vel
                    series.append((lag, vel, imag))
                    lag += LAG_STEP
                return series

            def _analyze(series):
                """From the raw series, keep SUSTAINED steps (|I| <= I_TRANSIENT), SMOOTH the velocity
                (5-pt moving avg) to beat the ~1-2% step noise, then return
                (peak_lag, peak_vel, v_lo, last_lag, still_rising).
                still_rising = the smoothed velocity at the END is within 2% of the smoothed peak -- i.e.
                the sweep NEVER rolled over, it was climbing right into the cap/guard ceiling. In that
                regime the argmax is just noise near the top and the reliable ceiling is last_lag, so the
                banker should use last_lag (max reliable), NOT argmax - MARGIN_LAGS.
                Smoothing is what stops a lone near-transient sample from stealing the peak."""
                sus = [(l, v) for (l, v, i) in series if i <= I_TRANSIENT]
                if len(sus) < 5:
                    return 0, 0.0, 0.0, 0, False
                lags = [l for l, v in sus]; vels = [v for l, v in sus]
                n = len(vels); w = 2; sm = []
                for k in range(n):
                    a = max(0, k - w); b = min(n, k + w + 1)
                    sm.append(sum(vels[a:b]) / (b - a))
                bi = max(range(n), key=lambda k: sm[k])
                still_rising = sm[-1] >= 0.98 * sm[bi]
                return lags[bi], sm[bi], min(sm), lags[-1], still_rising

            self.node.sdo['SetModeOfOperation'].raw = MODE_IDLE
            self.node.sdo['ControlWord'].raw = CLEAR_FAULT
            self.node.sdo['ControlWord'].raw = SHUTDOWN
            self.node.sdo['ControlWord'].raw = OP_ENABLED
            print("Setting Mode = PROFILE_VEL (max-velocity field-weakening lag sweep, cmd {} cts/s = "
                  "{:.0f} RPM)".format(max_vel, max_vel * 60.0 / enc_resolution))
            self.node.sdo['SetModeOfOperation'].raw = MODE_PROFILE_VEL

            saved = False
            try:
                ser_f = _sweep_fw(+1)
                self.node.sdo['TargetVelocity'].raw = 0
                self.node.sdo[0x3013][5].raw = 0
                _sleep_responsive(0.8)
                ser_r = _sweep_fw(-1)
                self.node.sdo['TargetVelocity'].raw = 0

                bl_f, bv_f, vlo_f, last_f, rise_f = _analyze(ser_f)
                bl_r, bv_r, vlo_r, last_r, rise_r = _analyze(ser_r)
                gain_f = (bv_f / vlo_f) if vlo_f > 0 else 1.0
                gain_r = (bv_r / vlo_r) if vlo_r > 0 else 1.0
                print("FW result: fwd peak {:.0f} RPM @lag {} (+{:.1f}%)   rev peak {:.0f} RPM @lag {} (+{:.1f}%)"
                      .format(bv_f * 60.0 / enc_resolution, bl_f, (gain_f - 1) * 100.0,
                              bv_r * 60.0 / enc_resolution, bl_r, (gain_r - 1) * 100.0))
                if gain_f >= GAIN_MIN and gain_r >= GAIN_MIN:
                    if rise_f and rise_r:
                        # NO rollover: velocity was still climbing into the reliable ceiling (the
                        # LAG_HARD_CAP the sweep ran clean at, or wherever a guard stopped it). The argmax
                        # is just noise near the top, so argmax - MARGIN_LAGS throws away real gain. Bank
                        # the MAX RELIABLE lag = min swept-clean ceiling - a small sustain margin instead.
                        ceil = min(last_f, last_r)
                        lag = max(0, ceil - SUSTAIN_MARGIN)
                        print("Both dirs field-weaken, still RISING at the lag {} ceiling (no rollover) -> "
                              "banking MAX RELIABLE LagFactor {} (ceiling {} - {} sustain margin).".format(
                                  ceil, lag, ceil, SUSTAIN_MARGIN))
                    else:
                        # A real field-weakening ROLLOVER (velocity peaked then fell) -> bank below the
                        # conservative (lower) peak lag to clear the drop-off edge + velocity noise.
                        lag = max(0, min(bl_f, bl_r) - MARGIN_LAGS)
                        print("Both dirs field-weaken (rollover peak). Banking min peak-lag {} - {} margin "
                              "= LagFactor {}".format(min(bl_f, bl_r), MARGIN_LAGS, lag))
                else:
                    lag = 0
                    print(">>> No consistent field-weakening gain (need >= {:.0f}% in BOTH dirs; got fwd {:.1f}% "
                          "rev {:.1f}%). Leaving LagFactor=0.".format((GAIN_MIN - 1) * 100.0,
                          (gain_f - 1) * 100.0, (gain_r - 1) * 100.0))
                self.node.sdo[0x3013][5].raw = lag
                self.node.sdo['Save']['Single'].raw = ((0x3013 << 8) | 0x05)   # persist to EE
                print("Saved LagFactor={} to EEPROM.".format(lag))
                saved = True
            except Exception as _e:
                print("Encoder lag cal ABORTED: {}".format(_e))
                print("Restoring LagFactor={} (prior), TargetVelocity=0.".format(orig_lag))
                try: self.node.sdo[0x3013][5].raw = orig_lag
                except Exception: pass
            finally:
                try: self.node.sdo['TargetVelocity'].raw = 0
                except Exception: pass

            print("Setting Mode = IDLE")
            self.node.sdo['SetModeOfOperation'].raw = MODE_IDLE

        except Exception as _exc:
            if calAll:
                raise
            self._cal_fault(_exc)

        self.frame_statusbar.SetStatusText("Ready", 1)
        if self.ADC_ON == False and self.adcWasON == True:
            self.on_off_adc(self)
        if calAll == False:
            self.Enable()

    def test_encoder(self,event,calAll=False):
        # print("Testing Encoder...")
        if calAll==False:
          if self.check_for_node() == False: #len(self.network.scanner.nodes) == 0:
            # print("No active puck")
            return False
          self.Disable()
        self.frame_statusbar.SetStatusText("Testing Encoder...", 1)
        self.frame_statusbar.Update()
        _yield()

        if self.ADC_ON == True:
            self.adcWasON = True
            self.on_off_adc(self)
        else:
            self.adcWasON = False

        # Set Mode to Idle (0)
        print("Setting Mode = IDLE")
        self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE
        _sleep_responsive(1) # Wait at least 75 ms for the filters to settle
        timeEnd = time.time() + 1
        Pos = []
        while time.time() < timeEnd:
          Pos.append(self.node.sdo['PositionFeedback'].raw)
          _yield() # keep wx event loop alive so Windows doesn't mark the app "Not Responding"
        posDif = max(Pos) - min(Pos)
        print("Max Pos: {} Min Pos: {} Diff: {}".format(max(Pos), min(Pos), posDif))
        maxDif = 8

        out_of_bounds = posDif > maxDif
        if out_of_bounds:
          msg = "Encoder Readings Unstable! \n\nEncoder variation: {} counts" \
          "\nMax Acceptable Variation: {} counts" \
          "\n\nDebugging steps:" \
          "\n- Ensure magnet to encoder spacing is 1.5mm +/- 0.5mm" \
          "\n- Verify magnet concentric to the shaft and rotates properly" \
          "\n\nWould you like to continue calibration?".format(posDif, maxDif)
          print("Encoder readings unstable...")

        self.frame_statusbar.SetStatusText("Ready", 1)
        if self.ADC_ON == False and self.adcWasON == True:
            self.on_off_adc(self)
        if calAll == False:
          self.Enable()

        if out_of_bounds:
          return self._prompt('Warning!', msg)
        return True

    def test_encoder_linearity(self, event):
        """Merged into generate_enc_correction_table — redirect."""
        self.generate_enc_correction_table(event)

    def _test_encoder_linearity_legacy_body(self):
        """Kept for reference only — no longer called."""
        if not self.check_for_node():
            return
        self.Disable()

        if self.ADC_ON:
            self.adcWasON = True
            self.on_off_adc(self)
        else:
            self.adcWasON = False

        try:
            e_zero          = self.node.sdo['Calibration']['e_zero'].raw
            e_polarity      = int(self.node.sdo['Calibration']['e_polarity'].raw)
            enc_resolution  = self.node.sdo['EncoderConfig']['Resolution'].raw
            motor_poles     = self.node.sdo['Calibration']['poles'].raw
            cts_per_elec    = enc_resolution * 2.0 / motor_poles
            pole_pairs      = motor_poles // 2
            cal_current     = self.node.sdo['Calibration']['i_cal'].raw
            i_peak          = self.node.sdo['Calibration']['i_peak'].raw
            if cal_current > i_peak:
                cal_current = i_peak

            print("Encoder linearity sweep — {} pole pairs  {:.1f} cts/elec_cyc  "
                  "e_zero={}  e_polarity={}".format(
                pole_pairs, cts_per_elec, e_zero, e_polarity))

            # Enter PHASE_VOLTAGE_ANGLE and ramp to calibration current at theta_e = 0
            self.node.sdo["ControlWord"].raw = CLEAR_FAULT
            self.node.sdo["ControlWord"].raw = SHUTDOWN
            self.node.sdo["ControlWord"].raw = OP_ENABLED
            self.node.sdo["SetModeOfOperation"].raw = MODE_PHASE_VOLTAGE_ANGLE
            self.node.sdo['Theta_e'].raw = 0
            time.sleep(0.3)
            _yield()

            motor_ud = 0
            while (self.node.sdo['Motor']['id'].raw / 1000.0 * i_peak) < cal_current and motor_ud < 32000:
                _id_now = self.node.sdo['Motor']['id'].raw / 1000.0 * i_peak
                if motor_ud > 0 and _id_now > 0:
                    _step = max(100, int((motor_ud * cal_current / _id_now - motor_ud) / 4))
                else:
                    _step = max(100, 32000 // 12)
                motor_ud = min(motor_ud + _step, 32000)
                self.node.sdo['Motor']['ud'].raw = motor_ud
                time.sleep(0.05)
                _yield()

            _sleep_responsive(0.3)

            # Sweep: N_PER_CYCLE steps per electrical cycle × pole_pairs cycles = 1 mech rev
            N_PER_CYCLE = 48          # 7.5° electrical per step
            N_TOTAL     = N_PER_CYCLE * pole_pairs
            STEP_S      = 0.08        # seconds per step

            enc_prev       = self.node.sdo['Encoder']['RawPosition'].raw
            enc_accumulated = 0
            results        = []       # (mech_deg, elec_deg, cycle, error_deg)

            print("Sweeping {} steps ({} per elec cycle × {} cycles) — ~{:.0f} s ...".format(
                N_TOTAL, N_PER_CYCLE, pole_pairs, N_TOTAL * STEP_S))

            for step in range(N_TOTAL + 1):
                step_in_cycle = step % N_PER_CYCLE
                frac_in_cycle = step_in_cycle / N_PER_CYCLE   # 0 → 1 within cycle
                total_elec_cycles = step / N_PER_CYCLE         # monotonically increasing

                # Theta_e command for this step (wraps modulo 2π each electrical cycle)
                theta_e_u = round(frac_in_cycle * 65536) % 65536
                theta_e_raw = theta_e_u if theta_e_u < 32768 else theta_e_u - 65536
                self.node.sdo['Theta_e'].raw = theta_e_raw
                time.sleep(STEP_S)
                _yield()

                enc = self.node.sdo['Encoder']['RawPosition'].raw
                delta = enc - enc_prev
                if delta >  enc_resolution / 2: delta -= enc_resolution
                if delta < -enc_resolution / 2: delta += enc_resolution
                enc_accumulated += delta
                enc_prev = enc

                # Expected accumulated encoder displacement
                # theta_e = e_polarity × (pos − e_zero) / cts_per_elec × 2π
                # → Δpos = e_polarity × Δtheta_e / (2π) × cts_per_elec
                # Δtheta_e over total_elec_cycles full cycles = total_elec_cycles × 2π
                expected = e_polarity * total_elec_cycles * cts_per_elec
                error_cts = enc_accumulated - expected
                error_deg = error_cts / cts_per_elec * 360.0

                mech_deg = total_elec_cycles / pole_pairs * 360.0
                elec_deg = frac_in_cycle * 360.0
                cycle_n  = step // N_PER_CYCLE + 1
                results.append((mech_deg, elec_deg, cycle_n, error_deg))

            self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE

            # ---- Report ----
            errors   = [r[3] for r in results]
            max_err  = max(errors, key=abs)
            max_idx  = max(range(len(errors)), key=lambda i: abs(errors[i]))
            rms_err  = (sum(e * e for e in errors) / len(errors)) ** 0.5
            PASS_THRESHOLD = 5.0  # engineering pass/fail limit in electrical degrees
            passed = abs(max_err) <= PASS_THRESHOLD

            print("\nEncoder linearity results:")
            print("  Max error : {:.2f}° elec  at mech={:.1f}°  elec={:.1f}°  (cycle {})".format(
                max_err, results[max_idx][0], results[max_idx][1], results[max_idx][2]))
            print("  RMS error : {:.2f}° elec".format(rms_err))
            print("  Pass/Fail : {} (threshold ±{:.1f}° elec)".format(
                "PASS" if passed else "FAIL", PASS_THRESHOLD))
            print("\n  {:>8}  {:>8}  {:>6}  {:>10}".format(
                "Mech(°)", "Elec(°)", "Cycle", "Error(°el)"))
            for mech, elec, cyc, err in results:
                flag = " <<<" if abs(err) >= PASS_THRESHOLD else ""
                print("  {:8.1f}  {:8.1f}  {:6d}  {:+10.2f}{}".format(
                    mech, elec, cyc, err, flag))

            # ---- Plot ----
            try:
                import matplotlib
                matplotlib.use('Agg')
                import matplotlib.pyplot as plt
                import matplotlib.gridspec as gridspec
                import datetime, os
                from ..paths import resource_path

                mech_all = [r[0] for r in results]
                err_all  = [r[3] for r in results]

                _enc_node_id = getattr(self.node, 'id', '?')
                _enc_pc = None
                try:
                    _enc_pc = int(self.node.sdo[0x1018][2].raw)
                except Exception:
                    pass
                _enc_model = getattr(self, '_PRODUCT_CODE_MODELS', {}).get(_enc_pc, 'unknown')

                fig = plt.figure(figsize=(15, 8))
                fig.suptitle('Encoder Linearity — Node {}  {}'.format(_enc_node_id, _enc_model),
                             fontsize=13)
                gs  = gridspec.GridSpec(2, 2, figure=fig, width_ratios=[1.2, 1])
                ax1 = fig.add_subplot(gs[0, 0])
                ax2 = fig.add_subplot(gs[1, 0])
                ax3 = fig.add_subplot(gs[:, 1])

                pf_label = 'Pass/Fail limit (±{:.0f}°)'.format(PASS_THRESHOLD)
                pf_color = 'r'
                pf_ls    = '--'
                pf_lw    = 1.5
                exp_lw   = 2.5
                exp_ls   = ':'

                ax1.plot(mech_all, err_all, 'b-', linewidth=0.8, label='Measured error')
                ax1.axhline(0, color='k', linewidth=exp_lw, linestyle=exp_ls,
                            label='Expected (0° error)', zorder=5)
                ax1.axhline( PASS_THRESHOLD, color=pf_color, linewidth=pf_lw,
                             linestyle=pf_ls, label=pf_label)
                ax1.axhline(-PASS_THRESHOLD, color=pf_color, linewidth=pf_lw,
                             linestyle=pf_ls)
                ax1.set_xlabel('Mechanical angle (°)')
                ax1.set_ylabel('Error (° electrical)')
                ax1.set_title('Encoder linearity — error vs mechanical angle  [{}]'.format(
                    'PASS' if passed else 'FAIL'))
                ax1.legend(fontsize=8)
                ax1.grid(True, alpha=0.3)

                colors = plt.cm.tab10.colors
                for cyc in range(1, pole_pairs + 2):
                    cyc_pts = [(r[1], r[3]) for r in results if r[2] == cyc]
                    if cyc_pts:
                        xs, ys = zip(*cyc_pts)
                        ax2.plot(xs, ys, color=colors[(cyc - 1) % 10],
                                 alpha=0.8, linewidth=0.9,
                                 label='Cycle {}'.format(cyc))
                ax2.axhline(0, color='k', linewidth=exp_lw, linestyle=exp_ls,
                            label='Expected (0° error)', zorder=5)
                ax2.axhline( PASS_THRESHOLD, color=pf_color, linewidth=pf_lw,
                             linestyle=pf_ls, label=pf_label)
                ax2.axhline(-PASS_THRESHOLD, color=pf_color, linewidth=pf_lw,
                             linestyle=pf_ls)
                ax2.set_xlabel('Electrical angle (°)')
                ax2.set_ylabel('Error (° electrical)')
                ax2.set_title('Overlaid by electrical cycle — consistent = electrical error; '
                              'shifting = mechanical encoder error')
                ax2.legend(fontsize=7, ncol=4)
                ax2.grid(True, alpha=0.3)

                # --- ax3: Lissajous — one trace per electrical cycle ---
                # Normalize each cycle to its own starting error so inter-cycle
                # drift doesn't shift the loops. Overlaid loops that all land on
                # the same shape = electrical error; loops that spread = mechanical.
                _cyc_base = {}
                for _r in results:
                    if int(round(_r[1] / 360.0 * N_PER_CYCLE)) % N_PER_CYCLE == 0:
                        _cyc_base.setdefault(_r[2], _r[3])
                _cyc_profiles = {}
                for _r in results:
                    _s = int(round(_r[1] / 360.0 * N_PER_CYCLE)) % N_PER_CYCLE
                    _cyc_profiles.setdefault(_r[2], {})[_s] = (
                        _r[3] - _cyc_base.get(_r[2], 0.0))
                # Radial scale from worst within-cycle error across all cycles
                _all_wc  = [e for d in _cyc_profiles.values() for e in d.values()]
                _max_ae  = max(abs(e) for e in _all_wc) if _all_wc else 1.0
                _r_scale = 0.4 / (_max_ae or 1.0)
                # Ideal unit circle
                _circ_t = [i / 360 * 2 * math.pi for i in range(361)]
                ax3.plot([math.cos(t) for t in _circ_t],
                         [math.sin(t) for t in _circ_t],
                         'k', linewidth=2.5, linestyle=':', label='Ideal', zorder=5)
                # One coloured trace per complete cycle
                _liss_colors = plt.cm.tab10.colors
                for _cyc in sorted(_cyc_profiles.keys()):
                    _prof = _cyc_profiles[_cyc]
                    if len(_prof) < N_PER_CYCLE:
                        continue  # skip incomplete trailing stub
                    _steps = sorted(_prof.keys())
                    _ts = [s / N_PER_CYCLE * 2.0 * math.pi for s in _steps] + [0.0]
                    _es = [_prof[s] for s in _steps] + [0.0]
                    _xs = [(1.0 + _r_scale * e) * math.cos(t) for t, e in zip(_ts, _es)]
                    _ys = [(1.0 + _r_scale * e) * math.sin(t) for t, e in zip(_ts, _es)]
                    ax3.plot(_xs, _ys, color=_liss_colors[(_cyc - 1) % 10],
                             alpha=0.7, linewidth=0.9, label='Cycle {}'.format(_cyc))
                ax3.set_aspect('equal')
                ax3.axhline(0, color='gray', linewidth=0.5, zorder=0)
                ax3.axvline(0, color='gray', linewidth=0.5, zorder=0)
                ax3.set_xlabel('cos(ε)')
                ax3.set_ylabel('sin(ε)')
                ax3.set_title(
                    'Encoder Lissajous (per elec. cycle)  [{}]\n'
                    '{:.0f}°/unit  —  overlap=electrical err,  spread=mechanical err'.format(
                        'PASS' if passed else 'FAIL', 1.0 / _r_scale))
                ax3.legend(fontsize=7, ncol=4)
                ax3.grid(True, alpha=0.3)

                plt.tight_layout()
                ts = datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
                from ..paths import session_path
                plot_path = session_path('enc_linearity_{}.png'.format(ts))
                os.makedirs(os.path.dirname(plot_path), exist_ok=True)
                plt.savefig(plot_path, dpi=100)
                plt.close()
                print("\nPlot saved: {}".format(plot_path))
            except ImportError as _e:
                print("\nWARNING: enc_linearity plot not saved — matplotlib not installed: {}".format(_e))
            except Exception as _e:
                print("\nWARNING: enc_linearity plot failed: {}".format(_e))

        except Exception as _exc:
            self._cal_fault(_exc)

        finally:
            if self.ADC_ON == False and self.adcWasON:
                self.on_off_adc(self)
            self.Enable()

    def generate_enc_correction_table(self, event):
        """
        High-resolution encoder sweep → position correction lookup table.

        Sweeps theta_e through one full mechanical revolution at 192 steps per
        electrical cycle, fits a Fourier series to the measured position errors,
        and writes two CSV correction tables:

          enc_correction_full_*.csv  — one signed-int entry per encoder count
                                       (covers both electrical and mechanical errors)
          enc_correction_elec_*.csv  — one signed-int entry per count within one
                                       electrical cycle (smaller; electrical errors only)

        Table format:  corrected_pos = raw_pos + table[raw_pos % period]
        """
        if not self.check_for_node():
            return

        # Firmware < 4.4.0 has no encoder-compensation object (0x3027) to write, but the SWEEP still
        # yields useful info: the measured encoder-error harmonics + linearity reveal magnet/encoder
        # MISALIGNMENT. So on old firmware we run TEST-ONLY -- measure + report, skip the upload.
        class _EncTestOnly(Exception):
            pass
        _test_only = not self._fw_at_least(4, 4, 0)
        if _test_only:
            self._prompt_ok("Firmware Too Old — Test Only",
                "Encoder compensation requires firmware v4.4.0 or later.\n"
                "Running TEST-ONLY: measures the encoder error (magnet alignment), no upload.")

        # If compensation is already active, ask whether to recalibrate or retest.
        # Recalibration always runs an automatic retest sweep afterwards.
        _retest_only = False
        try:
            if not _test_only and self._enc_comp_read(1):
                _cdlg = wx.Dialog(self, title="Encoder Compensation Active")
                _cdlg_sizer = wx.BoxSizer(wx.VERTICAL)
                _cdlg_msg = wx.StaticText(
                    _cdlg, label=
                    "Encoder compensation is currently active on this node.\n\n"
                    "Recalibrate: replace compensation with a new bidirectional sweep\n"
                    "  (retest runs automatically afterwards).\n\n"
                    "Retest Linearity: verify current compensation accuracy only.")
                _cdlg_sizer.Add(_cdlg_msg, 0, wx.ALL, 12)
                _cdlg_btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
                _btn_recal  = wx.Button(_cdlg, label="Recalibrate")
                _btn_retest = wx.Button(_cdlg, label="Retest Linearity")
                _btn_cancel = wx.Button(_cdlg, wx.ID_CANCEL, label="Cancel")
                _cdlg_btn_sizer.Add(_btn_recal,  0, wx.ALL, 4)
                _cdlg_btn_sizer.Add(_btn_retest, 0, wx.ALL, 4)
                _cdlg_btn_sizer.Add(_btn_cancel, 0, wx.ALL, 4)
                _cdlg_sizer.Add(_cdlg_btn_sizer, 0, wx.ALIGN_CENTER | wx.BOTTOM, 8)
                _cdlg.SetSizerAndFit(_cdlg_sizer)
                _cdlg_choice = [None]
                def _on_recal(e):  _cdlg_choice[0] = 'recal';  _cdlg.EndModal(wx.ID_YES)
                def _on_retest(e): _cdlg_choice[0] = 'retest'; _cdlg.EndModal(wx.ID_NO)
                _btn_recal.Bind(wx.EVT_BUTTON,  _on_recal)
                _btn_retest.Bind(wx.EVT_BUTTON, _on_retest)
                _cdlg_result = _cdlg.ShowModal()
                _cdlg.Destroy()
                if _cdlg_result == wx.ID_CANCEL:
                    return
                _retest_only = (_cdlg_choice[0] == 'retest')
        except Exception:
            pass

        self._menu_idle_takeover()   # standalone menu handler (no calAll): take the drive over cleanly at entry
        self.Disable()
        if self.ADC_ON:
            self.adcWasON = True
            self.on_off_adc(self)
        else:
            self.adcWasON = False

        self.OnStartTask(None)
        self.frame_statusbar.SetStatusText("Generating encoder correction table...", 1)
        self.frame_statusbar.Update()

        try:
            import cmath as _cm
            import datetime, os
            from ..paths import resource_path, session_path
            ts = datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')

            e_zero         = self.node.sdo['Calibration']['e_zero'].raw
            e_polarity     = int(self.node.sdo['Calibration']['e_polarity'].raw)
            enc_resolution = self.node.sdo['EncoderConfig']['Resolution'].raw
            motor_poles    = self.node.sdo['Calibration']['poles'].raw
            cts_per_elec   = enc_resolution * 2.0 / motor_poles
            pole_pairs     = motor_poles // 2
            cal_current    = self.node.sdo['Calibration']['i_cal'].raw
            i_peak         = self.node.sdo['Calibration']['i_peak'].raw
            if cal_current > i_peak:
                cal_current = i_peak

            node_id = getattr(self.node, 'id', '?')
            _pc = None
            try:
                _pc = int(self.node.sdo[0x1018][2].raw)
            except Exception:
                pass
            model_str = getattr(self, '_PRODUCT_CODE_MODELS', {}).get(_pc, 'unknown')
            _file_pfx = 'node{}_{}_'.format(node_id, model_str.replace(' ', '_'))

            def _enc_img(name):
                p = session_path('encoder/images/{}'.format(name))
                os.makedirs(os.path.dirname(p), exist_ok=True)
                return p

            def _enc_data(name):
                p = session_path('encoder/data/{}'.format(name))
                os.makedirs(os.path.dirname(p), exist_ok=True)
                return p

            N_PER_CYCLE        = 64    # steps per electrical cycle → 5.625° per step
            STEP_S             = 0.025 # settle time per step (s) — calibration sweeps
            RETEST_STEP_S      = 0.015 # settle time for retest (EncPos is smoother than RawPos)
            N_HARMONICS        = 16    # Fourier harmonics retained
            N_TOTAL            = N_PER_CYCLE * pole_pairs  # exactly one mechanical revolution
            RETEST_N_PER_CYCLE = 48   # retest only needs to verify, not characterise
            RETEST_N_TOTAL     = RETEST_N_PER_CYCLE * pole_pairs

            print("\nEncoder correction table — sweep parameters")
            print("  {} pole pairs  {:.2f} cts/elec  enc_res={}  "
                  "e_zero={}  e_polarity={}".format(
                      pole_pairs, cts_per_elec, enc_resolution, e_zero, e_polarity))
            if _retest_only:
                _est_s = RETEST_N_TOTAL * RETEST_STEP_S * 2 + 15
                print("  {} steps/cycle × {} cycles = {} steps (bidir)  ~{:.0f} s (retest only)".format(
                    RETEST_N_PER_CYCLE, pole_pairs, RETEST_N_TOTAL, _est_s))
            else:
                _est_s = (N_TOTAL * STEP_S * 2                  # forward + reverse cal sweeps
                          + RETEST_N_TOTAL * RETEST_STEP_S * 2  # retest bidir (motor stays powered)
                          + 15)                                  # one ramp-up
                print("  cal: {} steps/cycle × {} cycles = {} steps  "
                      "retest: {} steps/cycle (bidir)  ~{:.0f} s total".format(
                    N_PER_CYCLE, pole_pairs, N_TOTAL, RETEST_N_PER_CYCLE, _est_s))

            # Disable encoder compensation during calibration sweep so the
            # measurement reflects the true encoder error, not a previously
            # saved (possibly wrong) correction. Re-enabled after upload.
            _enc_was_active = False
            if not _retest_only:
                try:
                    _enc_was_active = bool(self._enc_comp_read(1))
                    if _enc_was_active:
                        self._enc_comp_write(1, 0)
                        print("  Encoder compensation disabled for calibration sweep.")
                except Exception:
                    pass

            # ---- Enable in PHASE_VOLTAGE_ANGLE mode and ramp current ----
            self.node.sdo["ControlWord"].raw = CLEAR_FAULT
            self.node.sdo["ControlWord"].raw = SHUTDOWN
            self.node.sdo["ControlWord"].raw = OP_ENABLED
            self.node.sdo["SetModeOfOperation"].raw = MODE_PHASE_VOLTAGE_ANGLE
            self.node.sdo['Theta_e'].raw = 0
            time.sleep(0.3)
            _yield()

            # Close in on cal_current from below. A fixed first step (32000/12 used to be one)
            # drove 7.5 A into node 127 against a 1 A i_cal and never stepped back: the sweep
            # heated the motor ~17 C and the stator's stray field reached the encoder as a
            # spurious k = pp-1 term. So: grow ud geometrically until current flows, then move
            # half-way to the proportional estimate each step, which also backs off an overshoot.
            motor_ud = 0
            for _ in range(200):
                _id_now = self.node.sdo['Motor']['id'].raw / 1000.0 * i_peak
                if motor_ud > 0 and abs(_id_now - cal_current) < 0.05 * cal_current:
                    break
                if motor_ud > 0 and _id_now > 50:
                    motor_ud = int(motor_ud * (1 + 0.5 * (cal_current / _id_now - 1)))
                else:
                    motor_ud = max(motor_ud + 30, int(motor_ud * 1.5))
                motor_ud = max(0, min(motor_ud, 32000))
                self.node.sdo['Motor']['ud'].raw = motor_ud
                time.sleep(0.05)
                _yield()
            _sleep_responsive(0.3)

            # Report the drive the sweep actually runs at + how it got there. Too little holding current
            # (or too short a settle) vs a load shows up as a systematic low-order "encoder error" that
            # isn't real -- this makes that visible instead of silent.
            _id_drive = self.node.sdo['Motor']['id'].raw / 1000.0 * i_peak
            print("  Sweep drive: {} mA target (from Calibration/i_cal) -> ramped ud to {} -> holding "
                  "id={:.0f} mA".format(cal_current, motor_ud, _id_drive))
            print("  Sweep step: {} steps/elec-cycle ({:.2f} deg/step), {:.0f} ms settle each".format(
                N_PER_CYCLE, 360.0 / N_PER_CYCLE, STEP_S * 1000.0))

            # ---- Sweep one full mechanical revolution ----
            # Retest uses compensated EncPos (0x3012,2) to verify correction is applied;
            # calibration uses raw RawPosition (0x3012,1) to measure true encoder error.
            # Both paths run bidirectional (forward + reverse) to cancel friction bias.
            _sweep_pos_key = 'EncPos' if _retest_only else 'RawPosition'
            _sw_n_total = RETEST_N_TOTAL if _retest_only else N_TOTAL
            _sw_n_cycle = RETEST_N_PER_CYCLE if _retest_only else N_PER_CYCLE
            _step_s     = RETEST_STEP_S if _retest_only else STEP_S
            enc_start       = self.node.sdo['Encoder'][_sweep_pos_key].raw
            enc_prev        = enc_start
            enc_accumulated = 0
            sweep_fwd       = []

            print("Sweeping {} steps ({}) — forward ...".format(
                _sw_n_total, 'EncPos (compensated)' if _retest_only else 'RawPosition'))
            for step in range(_sw_n_total + 1):
                step_in_cycle = step % _sw_n_cycle
                total_elec    = step / _sw_n_cycle
                frac          = step_in_cycle / _sw_n_cycle
                theta_e_u     = round(frac * 65536) % 65536
                theta_e_raw   = theta_e_u if theta_e_u < 32768 else theta_e_u - 65536
                self.node.sdo['Theta_e'].raw = theta_e_raw
                time.sleep(_step_s)
                self.UpdateUI(5 + step * 30 // (_sw_n_total + 1))
                _yield()
                enc   = self.node.sdo['Encoder'][_sweep_pos_key].raw
                delta = enc - enc_prev
                if delta >  enc_resolution / 2: delta -= enc_resolution
                if delta < -enc_resolution / 2: delta += enc_resolution
                enc_accumulated += delta
                enc_prev = enc
                if step < _sw_n_total:
                    expected   = e_polarity * total_elec * cts_per_elec
                    correction = expected - enc_accumulated
                    abs_pos    = (enc_start + enc_accumulated) % enc_resolution
                    cycle_n    = step // _sw_n_cycle + 1
                    sweep_fwd.append((step_in_cycle, cycle_n, correction, abs_pos))

            # Reverse sweep: theta_e descends through one full mechanical revolution.
            # Friction/cogging bias flips sign vs the forward pass; averaging cancels it.
            # Both calibration and retest paths run bidirectional.
            enc_prev_rev = self.node.sdo['Encoder'][_sweep_pos_key].raw
            enc_acc_rev  = 0
            sweep_rev    = []
            print("Sweeping {} steps ({} — reverse) ...".format(
                _sw_n_total, 'EncPos' if _retest_only else 'RawPosition'))
            for step in range(_sw_n_total + 1):
                step_in_cycle_r = step % _sw_n_cycle
                total_elec_r    = step / _sw_n_cycle
                frac_r      = (_sw_n_cycle - step_in_cycle_r) % _sw_n_cycle / _sw_n_cycle
                theta_e_u   = round(frac_r * 65536) % 65536
                theta_e_raw = theta_e_u if theta_e_u < 32768 else theta_e_u - 65536
                self.node.sdo['Theta_e'].raw = theta_e_raw
                time.sleep(_step_s)
                self.UpdateUI(35 + step * 30 // (_sw_n_total + 1))
                _yield()
                enc_r   = self.node.sdo['Encoder'][_sweep_pos_key].raw
                delta_r = enc_r - enc_prev_rev
                if delta_r >  enc_resolution / 2: delta_r -= enc_resolution
                if delta_r < -enc_resolution / 2: delta_r += enc_resolution
                enc_acc_rev  += delta_r
                enc_prev_rev  = enc_r
                if step < _sw_n_total:
                    expected_r   = -e_polarity * total_elec_r * cts_per_elec
                    correction_r = expected_r - enc_acc_rev
                    abs_pos_r    = (enc_start + enc_acc_rev) % enc_resolution
                    sweep_rev.append((step_in_cycle_r, step // _sw_n_cycle + 1,
                                      correction_r, abs_pos_r))

            # Forward step i and reverse step (N_SF - i) % N_SF are at the same
            # absolute encoder position. Average to cancel friction/cogging bias.
            N_SF     = len(sweep_fwd)
            avg_corr = [(sweep_fwd[i][2] + sweep_rev[(N_SF - i) % N_SF][2]) / 2.0
                        for i in range(N_SF)]
            sweep    = [(sweep_fwd[i][0], sweep_fwd[i][1], avg_corr[i], sweep_fwd[i][3])
                        for i in range(N_SF)]
            print("  Bidirectional average complete ({} points).".format(N_SF))

            if _retest_only:
                self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE
            self.UpdateUI(95 if _retest_only else 66)

            N_S = len(sweep)

            # ---- Derive linearity results from high-density sweep ----
            PASS_THRESHOLD = 5.0  # engineering pass/fail limit (° electrical)
            lin_results = []
            for _lidx, (s_cyc, cyc_n, corr, _abs) in enumerate(sweep):
                _mech_deg  = _lidx / N_S * 360.0
                _elec_deg  = s_cyc / _sw_n_cycle * 360.0
                _error_deg = -corr / cts_per_elec * 360.0
                lin_results.append((_mech_deg, _elec_deg, cyc_n, _error_deg))

            _lin_errs = [r[3] for r in lin_results]
            _lin_max_err  = max(_lin_errs, key=abs)
            _lin_max_idx  = max(range(len(_lin_errs)), key=lambda i: abs(_lin_errs[i]))
            _lin_rms_err  = (sum(e * e for e in _lin_errs) / len(_lin_errs)) ** 0.5
            _lin_passed   = abs(_lin_max_err) <= PASS_THRESHOLD

            print("\nEncoder linearity results:")
            print("  Max error : {:.2f}° elec  at mech={:.1f}°  elec={:.1f}°  (cycle {})".format(
                _lin_max_err, lin_results[_lin_max_idx][0],
                lin_results[_lin_max_idx][1], lin_results[_lin_max_idx][2]))
            print("  RMS error : {:.2f}° elec".format(_lin_rms_err))
            print("  Pass/Fail : {} (threshold ±{:.1f}° elec)".format(
                "PASS" if _lin_passed else "FAIL", PASS_THRESHOLD))

            # ---- Linearity plot (enc_linearity PNG — 3 panels) ----
            try:
                import matplotlib
                matplotlib.use('Agg')
                import matplotlib.pyplot as _lplt
                import matplotlib.gridspec as _lgs

                _mech_all = [r[0] for r in lin_results]
                _err_all  = [r[3] for r in lin_results]

                _lfig = _lplt.figure(figsize=(15, 8))
                _lfig.suptitle(
                    'Encoder Linearity{}— Node {}  {}  ({})\n'
                    '{} pole pairs  {} steps/elec cycle'.format(
                        ' [COMPENSATION ACTIVE] ' if _retest_only else ' ',
                        node_id, model_str, ts, pole_pairs, _sw_n_cycle),
                    fontsize=13)
                _lgs_obj = _lgs.GridSpec(2, 2, figure=_lfig, width_ratios=[1.2, 1])
                _lax1 = _lfig.add_subplot(_lgs_obj[0, 0])
                _lax2 = _lfig.add_subplot(_lgs_obj[1, 0])
                _lax3 = _lfig.add_subplot(_lgs_obj[:, 1])

                _pf_label = 'Pass/Fail limit (±{:.0f}°)'.format(PASS_THRESHOLD)
                _lax1.plot(_mech_all, _err_all, 'b-', linewidth=0.8, label='Measured error')
                _lax1.axhline(0, color='k', linewidth=2.5, linestyle=':',
                              label='Expected (0° error)', zorder=5)
                _lax1.axhline( PASS_THRESHOLD, color='r', linewidth=1.5,
                               linestyle='--', label=_pf_label)
                _lax1.axhline(-PASS_THRESHOLD, color='r', linewidth=1.5, linestyle='--')
                _lax1.set_xlabel('Mechanical angle (°)')
                _lax1.set_ylabel('Error (° electrical)')
                _lax1.set_title('Encoder linearity — error vs mechanical angle  [{}]'.format(
                    'PASS' if _lin_passed else 'FAIL'))
                _lax1.legend(fontsize=8)
                _lax1.grid(True, alpha=0.3)

                _lcolors = _lplt.cm.tab10.colors
                for _lcyc in range(1, pole_pairs + 2):
                    _cyc_pts = [(r[1], r[3]) for r in lin_results if r[2] == _lcyc]
                    if _cyc_pts:
                        _xs, _ys = zip(*_cyc_pts)
                        _lax2.plot(_xs, _ys, color=_lcolors[(_lcyc - 1) % 10],
                                   alpha=0.8, linewidth=0.9, label='Cycle {}'.format(_lcyc))
                _lax2.axhline(0, color='k', linewidth=2.5, linestyle=':', zorder=5)
                _lax2.axhline( PASS_THRESHOLD, color='r', linewidth=1.5, linestyle='--',
                               label=_pf_label)
                _lax2.axhline(-PASS_THRESHOLD, color='r', linewidth=1.5, linestyle='--')
                _lax2.set_xlabel('Electrical angle (°)')
                _lax2.set_ylabel('Error (° electrical)')
                _lax2.set_title('Overlaid by electrical cycle — '
                                'consistent = electrical error; shifting = mechanical error')
                _lax2.legend(fontsize=7, ncol=4)
                _lax2.grid(True, alpha=0.3)

                # Lissajous per electrical cycle
                _lcyc_base = {}
                for _lr in lin_results:
                    if int(round(_lr[1] / 360.0 * _sw_n_cycle)) % _sw_n_cycle == 0:
                        _lcyc_base.setdefault(_lr[2], _lr[3])
                _lcyc_profiles = {}
                for _lr in lin_results:
                    _ls = int(round(_lr[1] / 360.0 * _sw_n_cycle)) % _sw_n_cycle
                    _lcyc_profiles.setdefault(_lr[2], {})[_ls] = (
                        _lr[3] - _lcyc_base.get(_lr[2], 0.0))
                _lall_wc = [e for d in _lcyc_profiles.values() for e in d.values()]
                _lmax_ae = max(abs(e) for e in _lall_wc) if _lall_wc else 1.0
                _lr_scale = 0.4 / (_lmax_ae or 1.0)
                _lcirc_t = [i / 360 * 2 * math.pi for i in range(361)]
                _lax3.plot([math.cos(t) for t in _lcirc_t],
                           [math.sin(t) for t in _lcirc_t],
                           'k', linewidth=2.5, linestyle=':', label='Ideal', zorder=5)
                for _lcn in sorted(_lcyc_profiles.keys()):
                    _lprof = _lcyc_profiles[_lcn]
                    if len(_lprof) < _sw_n_cycle:
                        continue
                    _lsteps = sorted(_lprof.keys())
                    _lts2 = [s / _sw_n_cycle * 2.0 * math.pi for s in _lsteps] + [0.0]
                    _les2 = [_lprof[s] for s in _lsteps] + [0.0]
                    _lxs  = [(1.0 + _lr_scale * e) * math.cos(t) for t, e in zip(_lts2, _les2)]
                    _lys  = [(1.0 + _lr_scale * e) * math.sin(t) for t, e in zip(_lts2, _les2)]
                    _lax3.plot(_lxs, _lys, color=_lcolors[(_lcn - 1) % 10],
                               alpha=0.7, linewidth=0.9, label='Cycle {}'.format(_lcn))
                _lax3.set_aspect('equal')
                _lax3.axhline(0, color='gray', linewidth=0.5, zorder=0)
                _lax3.axvline(0, color='gray', linewidth=0.5, zorder=0)
                _lax3.set_xlabel('cos(ε)')
                _lax3.set_ylabel('sin(ε)')
                _lax3.set_title(
                    'Encoder Lissajous (per elec. cycle)  [{}]\n'
                    '{:.0f}°/unit  —  overlap=electrical err, spread=mechanical err'.format(
                        'PASS' if _lin_passed else 'FAIL', 1.0 / _lr_scale))
                _lax3.legend(fontsize=7, ncol=4)
                _lax3.grid(True, alpha=0.3)

                _lplt.tight_layout()
                _lin_plot_path = _enc_img('{}enc_linearity_{}.png'.format(_file_pfx, ts))
                _lplt.savefig(_lin_plot_path, dpi=100)
                _lplt.close(_lfig)
                print("  Linearity plot → {}".format(_lin_plot_path))
            except ImportError as _le:
                print("  WARNING: linearity plot skipped — matplotlib not installed: {}".format(_le))
            except Exception as _le:
                print("  WARNING: linearity plot failed: {}".format(_le))

            if _retest_only:
                print("\nRetest complete — compensation active, skipping recalibration.")
                return

            # ---- Fourier fitting helpers (stdlib only, no numpy) ----
            def _dft(samples, n_harm):
                """DFT of `samples`, return first n_harm+1 complex coefficients."""
                N, X = len(samples), []
                for k in range(n_harm + 1):
                    wk = _cm.exp(-2j * math.pi * k / N)
                    val, w = 0.0, 1.0 + 0j
                    for c in samples:
                        val += c * w
                        w   *= wk
                    X.append(val / N)
                return X

            def _reconstruct(X, out_size):
                """Evaluate the real Fourier series at `out_size` evenly-spaced points."""
                table = []
                for p in range(out_size):
                    val = X[0].real
                    for k in range(1, len(X)):
                        a = 2.0 * math.pi * k * p / out_size
                        val += 2.0 * (X[k].real * math.cos(a) - X[k].imag * math.sin(a))
                    table.append(round(val))
                return table

            print("Fitting Fourier series ({} harmonics)...".format(N_HARMONICS))

            # ---- Full mechanical revolution table ----
            corr_seq = [s[2] for s in sweep]
            X_full   = _dft(corr_seq, N_HARMONICS)
            table_full = _reconstruct(X_full, enc_resolution)
            # _reconstruct yields the table in sweep-phase order (index 0 = sweep start,
            # i.e. enc_start), but every consumer (CSV "enc_pos_cts", the fit overlay at
            # table_full[abs_pos], the plot x-axis, and firmware table[raw_pos]) treats the
            # index as a TRUE absolute encoder count. Re-index to absolute so they agree and
            # tables are comparable across runs. Forward sweep step j sits at absolute
            # position (enc_start + e_polarity*j) mod res. RMS/max are roll-invariant; the
            # uploaded harmonics use the _psi path, not this table, so comp is unaffected.
            table_full = [table_full[(e_polarity * (_p - enc_start)) % enc_resolution]
                          for _p in range(enc_resolution)]

            # ---- Per-electrical-cycle table ----
            # Normalize each cycle to its own start so mechanical drift doesn't
            # contaminate the within-cycle (electrical) error shape.
            cyc_base = {}
            for s_cyc, cyc_n, corr, _ in sweep:
                if s_cyc == 0:
                    cyc_base.setdefault(cyc_n, corr)

            elec_acc   = [0.0] * N_PER_CYCLE
            elec_cnt   = [0]   * N_PER_CYCLE
            for s_cyc, cyc_n, corr, _ in sweep:
                elec_acc[s_cyc] += corr - cyc_base.get(cyc_n, 0.0)
                elec_cnt[s_cyc] += 1
            elec_avg = [elec_acc[s] / max(elec_cnt[s], 1) for s in range(N_PER_CYCLE)]

            elec_sz    = round(cts_per_elec)
            X_elec     = _dft(elec_avg, N_HARMONICS)
            table_elec = _reconstruct(X_elec, elec_sz)

            # ---- Classify dominant error type ----
            elec_rms = (sum(e ** 2 for e in elec_avg) / N_PER_CYCLE) ** 0.5
            full_rms = (sum(c ** 2 for c in corr_seq) / N_S) ** 0.5
            ratio    = elec_rms / (full_rms + 1e-9)
            if ratio > 0.65:
                err_type = "ELECTRICAL  — use per-cycle table (fewer entries, lower latency)"
            elif ratio < 0.30:
                err_type = "MECHANICAL  — use full-revolution table"
            else:
                err_type = "MIXED       — use full-revolution table"

            max_full = max(abs(c) for c in table_full)
            rms_full = (sum(c ** 2 for c in table_full) / enc_resolution) ** 0.5
            max_elec = max(abs(c) for c in table_elec)
            rms_elec = (sum(c ** 2 for c in table_elec) / elec_sz) ** 0.5

            print("\nCorrection table results:")
            print("  Dominant error  : {}".format(err_type))
            print("  Full-rev table  : {} entries  "
                  "max={:+d} cts ({:.1f}° elec)  RMS={:.1f} cts".format(
                      enc_resolution, max_full,
                      max_full / cts_per_elec * 360.0, rms_full))
            print("  Elec-cyc table  : {} entries  "
                  "max={:+d} cts ({:.1f}° elec)  RMS={:.1f} cts".format(
                      elec_sz, max_elec,
                      max_elec / cts_per_elec * 360.0, rms_elec))
            print("\n  Mode-12 usage:")
            print("    corrected_theta_e = (raw_pos + table[raw_pos % {}] - e_zero)".format(
                elec_sz if ratio > 0.65 else enc_resolution))
            print("                        * e_polarity / {:.2f} * 65536".format(cts_per_elec))

            # ---- Save CSV files ----
            meta = ("# node={} e_zero={} e_polarity={} enc_res={} motor_poles={} "
                    "cts_per_elec={:.2f} sweep_steps_per_cycle={} harmonics={}\n"
                    "# corrected_pos = raw_pos + table[index]\n"
                    ).format(node_id, e_zero, e_polarity, enc_resolution,
                             motor_poles, cts_per_elec, N_PER_CYCLE, N_HARMONICS)

            full_path = _enc_data('{}enc_correction_full_{}.csv'.format(_file_pfx, ts))
            with open(full_path, 'w') as _f:
                _f.write("# Encoder position correction — full mechanical revolution\n")
                _f.write("# index = raw_encoder_pos % enc_resolution\n")
                _f.write(meta)
                _f.write("enc_pos_cts,correction_cts\n")
                for _p, _c in enumerate(table_full):
                    _f.write("{},{}\n".format(_p, _c))

            elec_path = _enc_data('{}enc_correction_elec_{}.csv'.format(_file_pfx, ts))
            with open(elec_path, 'w') as _f:
                _f.write("# Encoder position correction — per electrical cycle\n")
                _f.write("# index = (raw_encoder_pos - e_zero) % round(cts_per_elec)\n")
                _f.write(meta)
                _f.write("pos_in_elec_cycle_cts,correction_cts\n")
                for _p, _c in enumerate(table_elec):
                    _f.write("{},{}\n".format(_p, _c))

            print("\n  Full-rev CSV  → {}".format(full_path))
            print("  Elec-cyc CSV  → {}".format(elec_path))

            # ---- Plot ----
            try:
                import matplotlib
                matplotlib.use('Agg')
                import matplotlib.pyplot as plt

                fig, axes = plt.subplots(2, 2, figsize=(14, 10))
                fig.suptitle(
                    'Encoder Correction Table — Node {}  {}  ({})\n'
                    '{} pole pairs  {:.2f} cts/elec  '
                    '{} steps/cycle  {} harmonics'.format(
                        node_id, model_str, ts,
                        pole_pairs, cts_per_elec, N_PER_CYCLE, N_HARMONICS))

                colors = plt.cm.tab10.colors

                # Top-left: raw correction vs mechanical angle + full-rev fit
                ax = axes[0, 0]
                mech_degs = [i / N_S * 360.0 for i in range(N_S)]
                ax.plot(mech_degs, corr_seq, 'b.', markersize=2, alpha=0.5, label='Measured')
                fit_at_pos = [table_full[sweep[i][3]] for i in range(N_S)]
                ax.plot(mech_degs, fit_at_pos, 'r-', linewidth=1.5,
                        label='Fourier fit ({} harmonics)'.format(N_HARMONICS))
                ax.set_xlabel('Mechanical angle (°)')
                ax.set_ylabel('Correction (cts)')
                ax.set_title('Full-revolution: measured vs fit  '
                             '[dominant error: {}]'.format(err_type.split()[0]))
                ax.legend(fontsize=8)
                ax.grid(True, alpha=0.3)

                # Top-right: per-cycle overlay (spread = mechanical error)
                ax = axes[0, 1]
                for cyc in range(1, pole_pairs + 1):
                    pts = [(s[0], s[2] - cyc_base.get(s[1], 0.0))
                           for s in sweep if s[1] == cyc]
                    if pts:
                        xs, ys = zip(*pts)
                        ax.plot([x / N_PER_CYCLE * 360.0 for x in xs], ys,
                                color=colors[(cyc - 1) % 10], alpha=0.7,
                                linewidth=0.9, label='Cycle {}'.format(cyc))
                ax.plot([i / N_PER_CYCLE * 360.0 for i in range(N_PER_CYCLE)],
                        elec_avg, 'k-', linewidth=2, label='Cycle avg')
                ax.set_xlabel('Electrical angle (°)')
                ax.set_ylabel('Correction − cycle base (cts)')
                ax.set_title('Per-cycle overlay  '
                             '(overlap=electrical err, spread=mechanical err)')
                ax.legend(fontsize=7, ncol=4)
                ax.grid(True, alpha=0.3)

                # Bottom-left: full table indexed by encoder position
                ax = axes[1, 0]
                ax.plot(range(enc_resolution), table_full, 'b-', linewidth=0.8)
                ax.axhline(0, color='k', linewidth=0.5, linestyle='--')
                ax.set_xlabel('Encoder position (cts)')
                ax.set_ylabel('Correction (cts)')
                ax.set_title('Full-rev table  '
                             '({} entries  max={:+d} cts  RMS={:.1f} cts)'.format(
                                 enc_resolution, max_full, rms_full))
                ax.grid(True, alpha=0.3)

                # Bottom-right: electrical-cycle table + raw averaged data
                ax = axes[1, 1]
                ax.plot([i / N_PER_CYCLE * elec_sz for i in range(N_PER_CYCLE)],
                        elec_avg, 'k.', markersize=4, alpha=0.7, label='Cycle avg')
                ax.plot(range(elec_sz), table_elec, 'g-', linewidth=1.5,
                        label='Elec-cycle table')
                ax.axhline(0, color='k', linewidth=0.5, linestyle='--')
                ax.set_xlabel('Position within electrical cycle (cts)')
                ax.set_ylabel('Correction (cts)')
                ax.set_title('Electrical-cycle table  '
                             '({} entries  max={:+d} cts  RMS={:.1f} cts)'.format(
                                 elec_sz, max_elec, rms_elec))
                ax.legend(fontsize=8)
                ax.grid(True, alpha=0.3)

                plt.tight_layout()
                plot_path = _enc_img('{}enc_correction_{}.png'.format(_file_pfx, ts))
                plt.savefig(plot_path, dpi=100)
                plt.close()
                print("  Plot  → {}".format(plot_path))
            except ImportError as _pe:
                print("  WARNING: plot skipped — matplotlib not installed: {}".format(_pe))
            except Exception as _pe:
                print("  WARNING: plot failed: {}".format(_pe))

            # ── FFT harmonic analysis ─────────────────────────────────────────
            try:
                import numpy as _np
                import json as _json

                # Use raw sweep data so harmonics reflect actual encoder error,
                # not the 16-harmonic-filtered Fourier table.
                N = N_S          # sweep points = one full mechanical revolution
                tf = _np.array(corr_seq, dtype=_np.float64)
                X = _np.fft.rfft(tf)

                # amplitude and phase per harmonic (k=0 is DC)
                amps   = 2.0 * _np.abs(X) / N
                phases = _np.angle(X)
                amps[0]  /= 2.0  # DC bin is not doubled
                amps[-1] /= 2.0  # Nyquist bin (if N even) is not doubled

                # sort by amplitude descending (skip DC k=0)
                order = 1 + _np.argsort(amps[1:])[::-1]
                top_n = min(40, len(order))

                print("\n  FFT harmonic analysis  (N={})".format(N))
                print("  {:>4s}  {:>10s}  {:>10s}  {:>12s}  {:>12s}".format(
                    "k", "Amplitude", "Phase(rad)", "cos coeff", "sin coeff"))
                print("  " + "-" * 54)

                harmonic_list = []
                for _ki in range(top_n):
                    k   = int(order[_ki])
                    A   = float(amps[k])
                    phi = float(phases[k])
                    a_k = A * _np.cos(phi)   # coefficient of cos(2π k pos/N)
                    b_k = -A * _np.sin(phi)  # coefficient of sin(2π k pos/N)
                    print("  {:>4d}  {:>10.4f}  {:>10.5f}  {:>12.4f}  {:>12.4f}".format(
                        k, A, phi, float(a_k), float(b_k)))
                    harmonic_list.append({
                        "k": k,
                        "frequency_cycles_per_rev": k,
                        "amplitude": A, "phase_rad": phi,
                        "cos_coeff": float(a_k), "sin_coeff": float(b_k)
                    })

                # find minimum harmonics needed for <1 ct RMS reconstruction error
                sorted_ks = [int(order[i]) for i in range(len(order))]
                X_recon = _np.zeros(N // 2 + 1, dtype=_np.complex128)
                X_recon[0] = X[0]  # always keep DC
                best_n = len(sorted_ks)
                for _ni in range(1, len(sorted_ks) + 1):
                    for _ki in range(_ni):
                        X_recon[sorted_ks[_ki]] = X[sorted_ks[_ki]]
                    recon = _np.fft.irfft(X_recon, n=N)
                    rms_err = float(_np.sqrt(_np.mean((tf - recon) ** 2)))
                    if rms_err < 1.0:
                        best_n = _ni
                        break
                print("\n  Harmonics needed for RMS < 1 ct: {}".format(best_n))

                table_bytes    = enc_resolution * 2
                harmonic_bytes = best_n * (2 + 4 + 4)  # k(u16) + cos(f32) + sin(f32)
                print("  Memory: lookup table = {} B  |  {} harmonics = {} B  ({}x smaller)".format(
                    table_bytes, best_n, harmonic_bytes,
                    int(round(table_bytes / harmonic_bytes)) if harmonic_bytes else "∞"))

                print("\n  Formula:  correction(pos) = Σ A_k · cos(2π·k·pos/{} + φ_k)".format(enc_resolution))
                print("  where pos = raw encoder count (0..{}),".format(enc_resolution - 1))
                print("  k = harmonic (cycles/rev), A_k = amplitude (counts),")
                print("  φ_k = phase (radians). DC offset: {:.3f} cts\n".format(float(amps[0])))

                fft_data = {
                    "enc_resolution": enc_resolution,
                    "fft_input": "raw_sweep_{}_points".format(N),
                    "formula": "correction(pos) = dc + sum(A_k * cos(2*pi*k*pos/{} + phi_k))".format(enc_resolution),
                    "harmonics_for_1ct_rms": best_n,
                    "table_bytes": table_bytes,
                    "harmonic_bytes": harmonic_bytes,
                    "dc_offset_cts": float(amps[0]),
                    "harmonics_by_amplitude": harmonic_list
                }
                fft_path = _enc_data('{}enc_correction_harmonics_{}.json'.format(_file_pfx, ts))
                with open(fft_path, 'w') as _jf:
                    _json.dump(fft_data, _jf, indent=2)
                print("  FFT JSON → {}".format(fft_path))

                # ── Upload significant harmonic bins to Puck (0x3027) ────────
                N_BINS = self._enc_comp_bins()
                # Bins the firmware implements (2 on v4.4). Upload at most N_UPLOAD_MAX. Measured on a P4-42
                # (node 127, 2026-09-17, notes/enc_comp_shape.md in the stm32 repo):
                # each uploaded bin costs ~0.98 us of case 2 in the PWM ISR, and case 2
                # only has 7.7 us of headroom at 80 kHz / 5.2 us at 100 kHz. Ten bins put
                # case 2 at 131% of the 80 kHz budget; two put it at 68%.
                #
                # And the tail buys almost nothing. On that unit the encoder-synchronous
                # error was 8.582 ct RMS, of which k=2 alone is 94%. Residual after the
                # top-N, against a 26.1% ceiling set by the electrical-synchronous content
                # that encoder comp cannot touch:
                #     1 bin  -> 1.422 ct, 25.3%      3 bins -> 0.604 ct, 25.9%
                #     2 bins -> 0.959 ct, 25.7%     10 bins -> 0.194 ct, 26.1%
                # Bins three through ten together are worth 0.4 percentage points and
                # 7.8 us of every control cycle. Raise this only with timing evidence.
                N_UPLOAD_MAX = min(2, N_BINS)
                # AC harmonics by amplitude (k≥1). DC offset is not uploaded to the puck;
                # it is subtracted from the retest plots for display only.
                #
                # Trim insignificant harmonics. Two independent reasons to drop a bin:
                #  (1) amplitude in the spectral noise floor — adds only noise to the
                #      correction and costs a GFLIB_SinCos per ISR (firmware now uses the
                #      direct-calc model: O(1) per bin, so cost scales with bin COUNT);
                #  (2) harmonic order above K_MAX_ENC — the stepped open-loop sweep can't
                #      resolve high-k phase reliably (phase error ≈ 2π·k·Δenc_start/N, so a
                #      ~2 ct anchor uncertainty is ~9° at k=42 but ~116° at k=126), and a
                #      wrong-phase high-k bin injects ×k-amplified ripple into theta_e when
                #      applied (observed: a 1.3 ct k=126 bin injected ~29 mA at 420 Hz and
                #      corrupted the cogging DFT). Keep bins above the noise floor and within
                #      the order cap, never fewer than needed for <1 ct RMS (best_n).
                #
                # FUTURE — phase-reproducibility gate (not implemented; would ~double cal time):
                #   Run two sweeps from different start positions, convert each bin's phase to
                #   ABSOLUTE frame (φ_abs = φ_fft − 2π·k·enc_start/N), and keep only bins whose
                #   φ_abs agrees between runs (e.g. <~30°). This cuts the unreliable bins from
                #   DATA per-bin, regardless of order, instead of the fixed K_MAX_ENC heuristic
                #   — it would keep a genuinely-stable high-k bin and drop a noisy low-k one.
                #   Deferred for now because it costs a second full sweep.
                #
                # CAVEAT — k = n×pole_pairs are electrical/cogging harmonics (e.g. k=42 = 2×21
                # on a 21-pole-pair motor). In the open-loop sweep, content there is ambiguous
                # (encoder error vs cogging feedthrough); an enc-comp bin there injects onto a
                # cogging-DFT bin and overlaps cogging comp's domain. Prefer letting cogging
                # comp own pole-pair-multiple content, and always calibrate cogging enc-comp-OFF.
                K_MAX_ENC    = 42   # order cap: above this the stepped sweep can't pin phase
                _amp_all     = sorted(float(amps[_k]) for _k in range(1, len(amps)))
                _noise_floor = _amp_all[len(_amp_all) // 2] if _amp_all else 0.0  # median ≈ noise
                _sig_thresh  = max(4.0 * _noise_floor, 0.5)  # cts: 4× noise floor, ≥0.5 ct
                _cap_ks      = [_k for _k in sorted_ks if _k <= K_MAX_ENC]   # in-band (phase-reliable)
                _capped      = [_k for _k in sorted_ks if _k >  K_MAX_ENC]   # over order cap

                # --- SAFETY (dynamic-instability guard) ------------------------------------------
                # The correction adds to the COMMUTATION angle (pwm.c): the table's spatial slope
                # d(corr)/d(pos) modulates commutation gain (1 + slope). If the TOTAL slope reaches 1.0
                # the corrected angle goes NON-MONOTONIC -> commutation runaway (the original geared k=6
                # failure). The OLD guard used a per-harmonic cap (0.06) + summed budget (0.12); that SUM
                # is a worst-case "all peaks aligned" proxy that massively overcounts (e.g. it reports 1.5
                # for a table whose REAL max slope is 0.66), so it wrongly deleted the DOMINANT REAL
                # encoder harmonic -- a direct-drive k=2 ellipticity. Rework:
                #  * GEOMETRIC vs ELECTRICAL split: k that IS a multiple of pole_pairs is electrical/cogging
                #    feedthrough (and is exactly what pushes total slope past 1.0) -> hand to cogging comp.
                #    k that is NOT a pole-pair multiple is a real geometric ENCODER error and belongs here.
                #  * REAL total-slope gate: greedily keep geometric harmonics (amplitude-descending) while
                #    the ACTUAL summed table slope max|Σ A_k(2πk/res)sin(·)| stays under TOTAL_SLOPE_MAX --
                #    the true monotonicity test over the real waveform, not the per-harmonic proxy.
                #  * GEAR-GATE: only direct-drive (gearRatio≈1.0) gets this. On a GEARED unit, shaft
                #    mechanical content is NOT a rotor-encoder error, so keep the conservative per-harmonic
                #    cap the geared k=6 runaway motivated.
                _gear = float(getattr(self, 'gearRatio', 1.0) or 1.0)
                _direct_drive = abs(_gear - 1.0) < 0.05
                def _slope_of(_k):
                    return float(amps[_k]) * 2.0 * math.pi * _k / float(enc_resolution)
                def _table_slope(_ks):
                    # REAL max |d(corr)/d(pos)| of the summed harmonics over one full mechanical period.
                    if not _ks:
                        return 0.0
                    _kk = _np.array(list(_ks)); _P = min(int(enc_resolution), 2048)
                    _pos = _np.arange(_P, dtype=_np.float64) * (float(enc_resolution) / _P)
                    _w   = 2.0 * _np.pi * _kk / float(enc_resolution)     # d/dpos of A cos(2πk pos/res + φ)
                    _ang = _np.outer(_pos, _w) + phases[_kk][_np.newaxis, :]
                    _der = -(amps[_kk] * _w)[_np.newaxis, :] * _np.sin(_ang)
                    return float(_np.max(_np.abs(_der.sum(axis=1))))
                _steep = []; _ppcut = []; _budcut = []; _elec = []
                # k = pole_pairs - 1 is the sweep's own drive current, not the encoder: the stator
                # field (at pp x theta) reaches the sensor and beats against the magnet's 1 x theta.
                # Node 127 (pp 7): k=6 0.40 ct at 1 A, 0.72 at 2 A, 1.6 at 7.5 A, while k=2 held
                # still. In use that field sits on the q axis (90 deg electrical on) at a size set
                # by load, so the sweep's bin would be wrong in both phase and amplitude.
                _stray = [_k for _k in _cap_ks if pole_pairs >= 2 and _k == pole_pairs - 1]
                _cap_ks = [_k for _k in _cap_ks if _k not in _stray]
                if _stray:
                    print("    Skipped (k = pole-pairs - 1 = {}, stator stray field at the drive current, "
                          "{:.2f} ct): not encoder error".format(_stray[0], float(amps[_stray[0]])))
                if _direct_drive:
                    TOTAL_SLOPE_MAX = 0.75     # real table-slope ceiling (25% margin under the 1.0 limit)
                    if pole_pairs >= 2:
                        _elec = [_k for _k in _cap_ks if _k % pole_pairs == 0]   # electrical -> cogging comp
                        _geo  = [_k for _k in _cap_ks if _k % pole_pairs != 0]   # geometric  -> encoder
                    else:
                        # 1-pole-pair: every k is an electrical multiple; only k<=2 are real encoder modes
                        # (eccentricity/ellipticity), a dominant k>2 is mechanical -> drop.
                        _ppcut = [_k for _k in _cap_ks if _k > 2]
                        _geo   = [_k for _k in _cap_ks if _k <= 2]
                    _kept = []
                    for _k in _geo:            # amplitude-descending; keep while the REAL table slope fits
                        if _table_slope(_kept + [_k]) <= TOTAL_SLOPE_MAX:
                            _kept.append(_k)
                        else:
                            _budcut.append(_k)
                    _cap_ks = _kept
                    print("    Slope guard: direct-drive — geometric harmonics, real table-slope {:.3f} "
                          "(ceiling {:.2f}, non-monotonic at 1.0)".format(_table_slope(_kept), TOTAL_SLOPE_MAX))
                else:
                    # GEARED: keep the original conservative per-harmonic guard (built for the k=6 runaway).
                    SLOPE_CAP  = 0.06    # max per-harmonic |d(corr)/d(pos)|
                    SLOPE_BUDG = 0.12    # max SUM of kept slopes
                    _steep = [_k for _k in _cap_ks if _slope_of(_k) > SLOPE_CAP]
                    _cap_ks = [_k for _k in _cap_ks if _slope_of(_k) <= SLOPE_CAP]
                    if pole_pairs <= 1:
                        _ppcut  = [_k for _k in _cap_ks if _k > 2]
                        _cap_ks = [_k for _k in _cap_ks if _k <= 2]
                    _ssum = 0.0; _kept = []
                    for _k in _cap_ks:   # amplitude-descending; keep until the slope budget is spent
                        if _ssum + _slope_of(_k) <= SLOPE_BUDG:
                            _kept.append(_k); _ssum += _slope_of(_k)
                        else:
                            _budcut.append(_k)
                    _cap_ks = _kept
                    print("    Slope guard: geared (ratio {:.2f}) — per-harmonic cap {:.2f}, budget "
                          "{:.2f}".format(_gear, SLOPE_CAP, SLOPE_BUDG))
                if _elec:
                    print("    Skipped (k = n×{} pole-pairs, electrical, not encoder error): ".format(pole_pairs)
                          + ", ".join("k={}".format(_k) for _k in _elec[:8]))
                if _steep:
                    print("    Dropped (per-harmonic slope > 0.06 — geared): "
                          + ", ".join("k={}(sl {:.3f})".format(_k, _slope_of(_k)) for _k in _steep[:8]))
                if _ppcut:
                    print("    Dropped (1-pole-pair: k>2 is mechanical, not encoder): "
                          + ", ".join("k={}".format(_k) for _k in _ppcut[:8]))
                if _budcut:
                    print("    Dropped (would exceed slope ceiling): "
                          + ", ".join("k={}".format(_k) for _k in _budcut[:8]))
                # ---------------------------------------------------------------------------------

                _n_sig       = sum(1 for _k in _cap_ks if float(amps[_k]) >= _sig_thresh)
                # best_n is computed over the WHOLE spectrum, including the electrical /
                # cogging harmonics this list has already excluded, so it is not a
                # meaningful floor for encoder bins -- it is what used to force all ten.
                # Report it, do not obey it. _cap_ks is amplitude-descending, so this
                # takes the top N_UPLOAD_MAX by amplitude.
                _keep_n      = min(N_UPLOAD_MAX, max(1, _n_sig))
                _top_bins    = _cap_ks[:_keep_n]    # significant, in-band bins, amplitude-descending
                _dropped     = _cap_ks[_keep_n:]    # in-band but below noise floor — not uploaded
                n_upload     = len(_top_bins)

                print("\n  Harmonic trim: noise floor ≈ {:.3f} ct, threshold {:.3f} ct, "
                      "k≤{}, best_n(<1ct, whole spectrum)={} → keeping top {} of {} "
                      "by amplitude.".format(
                          _noise_floor, _sig_thresh, K_MAX_ENC, best_n, n_upload, len(sorted_ks)))
                _capped_sig = [_k for _k in _capped if float(amps[_k]) >= _sig_thresh]
                if _capped_sig:
                    print("    Dropped (k>{}, phase unreliable): ".format(
                        K_MAX_ENC) + ", ".join(
                        "k={}({:.2f}ct)".format(_k, float(amps[_k])) for _k in _capped_sig[:10]))
                if _dropped:
                    print("    Dropped (sub-noise, not uploaded): " + ", ".join(
                        "k={}({:.2f}ct)".format(_dk, float(amps[_dk]))
                        for _dk in _dropped[:10]) + (" …" if len(_dropped) > 10 else ""))

                print("\n  Top {} harmonics by amplitude (most impactful first):".format(n_upload))
                print("  Rank  {:>4}  {:>10}".format("k", "Amplitude"))
                print("  " + "-" * 24)
                for _ri, _rk in enumerate(_top_bins):
                    print("  {:>4d}  {:>4d}  {:>10.4f}".format(_ri + 1, _rk, float(amps[_rk])))

                # Firmware pwm.c:correct_pos applies:
                #   out -= [A_s·sin(kθ) + A_c·cos(kθ)] / 256,  θ = 2π·(pos mod 4096)/4096
                # which equals out += amp·cos(kθ + ψ) — adding the correction to raw pos.
                #   A_s = +256·_bA·sin(ψ),   A_c = -256·_bA·cos(ψ)   (Q8.8 int16)
                #   ψ = e_polarity·_bphi - 2π·k·enc_start/enc_resolution  (sweep-relative → absolute)
                def _clamp_i16(v):
                    return max(-32768, min(32767, int(round(v))))

                # Sort by k for firmware's iterative complex-rotation optimization
                _top_bins = sorted(_top_bins, key=lambda _k: _k)

                # Real spatial slope of the ACTUAL uploaded correction -- gates the dynamic hold-stability
                # check below (only steep corrections can destabilise the closed-loop hold).
                _applied_slope = _table_slope(_top_bins)

                if _test_only:
                    print("\n  TEST-ONLY (firmware < 4.4.0): measured, not uploaded.")
                    print("  Magnet alignment: k=1 = ring off-center/eccentric; "
                          "k={} (pole-pairs) = magnet spacing.".format(pole_pairs))
                    # normal path stays powered in phase-voltage mode until the retest idles it (4577);
                    # test-only skips the retest, so de-energize the motor here before bailing out.
                    self.node.sdo['Theta_e'].raw = 0
                    self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE
                    raise _EncTestOnly()

                print("\n  Uploading encoder compensation harmonics to node {} ...".format(node_id))
                print("  {:>4}  {:>6}  {:>10}  {:>8}  {:>8}".format(
                    "Bin", "k", "Amp(cts)", "A_s(Q88)", "A_c(Q88)"))
                print("  " + "-" * 44)

                # Disable compensation while writing bins
                self._enc_comp_write(1, 0)

                for _bi in range(N_BINS):
                    _as_sub = 2 + _bi * 3
                    _k_sub  = 3 + _bi * 3
                    _ac_sub = 4 + _bi * 3

                    if _bi < n_upload:
                        _bk      = int(_top_bins[_bi])
                        _bA      = float(amps[_bk])
                        _bphi    = float(phases[_bk])
                        # e_polarity flips the _bphi sign: on a -1 motor the encoder counts DOWN as the
                        # forward sweep advances, so the sweep-relative phase is mirrored. (Reduces to the
                        # bare +_bphi for +1 motors -- no change there.) enc_start term is polarity-invariant.
                        _psi     = e_polarity * _bphi - 2.0 * math.pi * _bk * float(enc_start) / float(enc_resolution)
                        _A_s_val = _clamp_i16( 256.0 * _bA * math.sin(_psi))
                        _A_c_val = _clamp_i16(-256.0 * _bA * math.cos(_psi))
                        _k_val   = _bk
                        print("  {:>4d}  {:>6d}  {:>10.4f}  {:>8d}  {:>8d}".format(
                            _bi, _k_val, _bA, _A_s_val, _A_c_val))
                    else:
                        _A_s_val = _A_c_val = _k_val = 0

                    self._enc_comp_write(_as_sub, _A_s_val)
                    self._enc_comp_write(_k_sub, _k_val)
                    self._enc_comp_write(_ac_sub, _A_c_val)

                # The sweep is quasi-static, so the table is the encoder's error at rest. At speed the
                # MA702's own tracking filter scales and rotates it (firmware applies H(j k w), fn/zeta
                # in 0x3027,8-9). Node 127 at filter setting 0xA0: fn 150 Hz, zeta 0.88, fitted from
                # coasts to 12k rpm (stm32 notes/field_weakening_collapse.md). Other settings change
                # the bandwidth and have no fit yet, so leave the model off there.
                _filt = self._enc_filt_subs()
                if _filt:
                    try:
                        _hwf = self.node.sdo.upload(0x3013, 6)[0]
                    except Exception:
                        _hwf = None
                    _fn, _zt = (150, 880) if _hwf == 0xA0 else (0, 0)
                    self._enc_comp_write(_filt[0], _fn)
                    self._enc_comp_write(_filt[1], _zt)
                    if _fn:
                        print("  Encoder filter model: fn {} Hz, zeta {:.2f} (0x3027,{}-{})".format(
                            _fn, _zt / 1000.0, _filt[0], _filt[1]))
                    else:
                        print("  Encoder filter model OFF: no fit for encoder filter setting {} "
                              "(0x3013,6); the table is exact at rest only".format(
                                  "unknown" if _hwf is None else "{:#04x}".format(_hwf)))

                # Enable compensation
                self._enc_comp_write(1, 1)
                print("  Encoder Compensation Active → 1")
                try:
                    self.frame_menubar.ON.Check(True)
                    self.frame_menubar.OFF.Check(False)
                except Exception:
                    pass
                if n_upload < N_BINS:
                    print("  (Bins {}–{} zeroed — upload capped at {} to stay inside the "
                          "case-2 ISR budget; see N_UPLOAD_MAX)".format(
                              n_upload, N_BINS - 1, N_UPLOAD_MAX))

                # Readback verification
                print("\n  Readback verification:")
                print("  {:>4}  {:>6}  {:>8}  {:>8}  {}".format(
                    "Bin", "k", "A_s", "A_c", "OK?"))
                print("  " + "-" * 40)
                _active_rb = self._enc_comp_read(1)
                print("  Active flag readback: {}".format(_active_rb))
                _rb_ok = True
                for _bi in range(N_BINS):
                    _as_rb = self._enc_comp_read(2 + _bi * 3)
                    _k_rb  = self._enc_comp_read(3 + _bi * 3)
                    _ac_rb = self._enc_comp_read(4 + _bi * 3)
                    if _bi < n_upload:
                        _bk_exp  = int(_top_bins[_bi])
                        _bA_exp  = float(amps[_top_bins[_bi]])
                        # MUST mirror the upload's psi EXACTLY (line ~4694), including the e_polarity
                        # factor on the phase -- omitting it sign-flips the expected A_s/A_c on
                        # e_polarity=-1 (direct-drive) motors and false-alarms every bin as MISMATCH.
                        _psi_exp = (e_polarity * float(phases[_top_bins[_bi]])
                                    - 2.0 * math.pi * _bk_exp * float(enc_start) / float(enc_resolution))
                        _as_exp  = _clamp_i16( 256.0 * _bA_exp * math.sin(_psi_exp))
                        _ac_exp  = _clamp_i16(-256.0 * _bA_exp * math.cos(_psi_exp))
                    else:
                        _bk_exp = _as_exp = _ac_exp = 0
                    _ok = (_as_rb == _as_exp and _k_rb == _bk_exp
                           and _ac_rb == _ac_exp)
                    if not _ok:
                        _rb_ok = False
                    print("  {:>4d}  {:>6}  {:>8}  {:>8}  {}".format(
                        _bi,
                        "{} (exp {})".format(_k_rb, _bk_exp) if _k_rb != _bk_exp
                            else str(_k_rb),
                        "{} (exp {})".format(_as_rb, _as_exp) if _as_rb != _as_exp
                            else str(_as_rb),
                        "{} (exp {})".format(_ac_rb, _ac_exp) if _ac_rb != _ac_exp
                            else str(_ac_rb),
                        "OK" if _ok else "MISMATCH"))
                if _rb_ok:
                    print("  All bins verified OK.")
                else:
                    print("  WARNING: one or more bins did not readback correctly.")

                # Save every NV subindex of 0x3027 to EEPROM
                print("\n  Saving 0x3027 to EEPROM ...")
                self._enc_comp_save(N_BINS)
                print("  Saved.")

                # Write top-10 harmonics JSON (full detail for each uploaded bin)
                try:
                    _top10_bins = []
                    for _bi, _bk in enumerate(_top_bins):
                        _bk     = int(_bk)
                        _bA_raw = float(amps[_bk])
                        _bphi   = float(phases[_bk])
                        _psi = _bphi - 2.0 * math.pi * _bk * float(enc_start) / float(enc_resolution)
                        _top10_bins.append({
                            "rank":               _bi + 1,
                            "k":                  _bk,
                            "amplitude_fft_cts":  _bA_raw,
                            "phase_fft_rad":      _bphi,
                            "psi_rad":            _psi,
                            "A_s_q88":            _clamp_i16( 256.0 * _bA_raw * math.sin(_psi)),
                            "A_c_q88":            _clamp_i16(-256.0 * _bA_raw * math.cos(_psi)),
                            "cos_coeff":          _bA_raw * math.cos(_bphi),
                            "sin_coeff":          -_bA_raw * math.sin(_bphi),
                        })
                    _top10_data = {
                        "node_id":        node_id,
                        "model":          model_str,
                        "timestamp":      ts,
                        "enc_resolution": enc_resolution,
                        "enc_start":      int(enc_start),
                        "pole_pairs":     pole_pairs,
                        "n_bins":         len(_top10_bins),
                        "bins":           _top10_bins,
                    }
                    _top10_path = _enc_data(
                        '{}enc_compensation_top10_{}.json'.format(_file_pfx, ts))
                    with open(_top10_path, 'w') as _jf:
                        _json.dump(_top10_data, _jf, indent=2)
                    print("  Top-10 harmonics JSON → {}".format(_top10_path))
                except Exception as _j10e:
                    print("  WARNING: top-10 JSON failed: {}".format(_j10e))

                # Retest always runs automatically after calibration.
                # Motor stays powered in PVCA throughout analysis/upload — no ramp-up needed.
                print("\n  Retesting linearity with compensation active ...")
                self.node.sdo['Theta_e'].raw = 0
                _sleep_responsive(0.3)
                _yield()

                # Sweep: use RawPosition for delta tracking and EncPos for compensation.
                # Forward pass.
                _rt_raw_prev = self.node.sdo['Encoder']['RawPosition'].raw
                _rt_enc_start_raw = _rt_raw_prev  # absolute raw position at retest start; used for cogging DFT phase
                _rt_raw_acc  = 0
                _rt_results_fwd = []
                print("  Retest forward sweep ({} steps) ...".format(RETEST_N_TOTAL))
                for _rts in range(RETEST_N_TOTAL + 1):
                    _rts_cyc  = _rts % RETEST_N_PER_CYCLE
                    _rts_elec = _rts / RETEST_N_PER_CYCLE
                    _rts_frac = _rts_cyc / RETEST_N_PER_CYCLE
                    _rts_teu  = round(_rts_frac * 65536) % 65536
                    _rts_ter  = _rts_teu if _rts_teu < 32768 else _rts_teu - 65536
                    self.node.sdo['Theta_e'].raw = _rts_ter
                    time.sleep(RETEST_STEP_S)
                    self.UpdateUI(70 + _rts * 13 // (RETEST_N_TOTAL + 1))
                    _yield()
                    _rt_raw = self.node.sdo['Encoder']['RawPosition'].raw
                    _rt_enc = self.node.sdo['Encoder']['EncPos'].raw
                    _rt_d   = _rt_raw - _rt_raw_prev
                    if _rt_d >  enc_resolution / 2: _rt_d -= enc_resolution
                    if _rt_d < -enc_resolution / 2: _rt_d += enc_resolution
                    _rt_raw_acc  += _rt_d
                    _rt_raw_prev  = _rt_raw
                    _rt_comp = (_rt_enc - _rt_raw + enc_resolution // 2) % enc_resolution - enc_resolution // 2
                    _rt_enc_acc = _rt_raw_acc + _rt_comp
                    if _rts < RETEST_N_TOTAL:
                        _rt_exp = e_polarity * _rts_elec * cts_per_elec
                        _rt_err = (_rt_enc_acc - _rt_exp) / cts_per_elec * 360.0
                        _rt_results_fwd.append((_rts / RETEST_N_TOTAL * 360.0,
                                                _rts_cyc / RETEST_N_PER_CYCLE * 360.0,
                                                _rts // RETEST_N_PER_CYCLE + 1,
                                                _rt_err))

                # Reverse pass.
                _rt_raw_prev_r = self.node.sdo['Encoder']['RawPosition'].raw
                _rt_raw_acc_r  = 0
                _rt_results_rev = []
                print("  Retest reverse sweep ({} steps) ...".format(RETEST_N_TOTAL))
                for _rts in range(RETEST_N_TOTAL + 1):
                    _rts_cyc_r  = _rts % RETEST_N_PER_CYCLE
                    _rts_elec_r = _rts / RETEST_N_PER_CYCLE
                    _rts_frac_r = (RETEST_N_PER_CYCLE - _rts_cyc_r) % RETEST_N_PER_CYCLE / RETEST_N_PER_CYCLE
                    _rts_teu_r  = round(_rts_frac_r * 65536) % 65536
                    _rts_ter_r  = _rts_teu_r if _rts_teu_r < 32768 else _rts_teu_r - 65536
                    self.node.sdo['Theta_e'].raw = _rts_ter_r
                    time.sleep(RETEST_STEP_S)
                    self.UpdateUI(83 + _rts * 13 // (RETEST_N_TOTAL + 1))
                    _yield()
                    _rt_raw_r = self.node.sdo['Encoder']['RawPosition'].raw
                    _rt_enc_r = self.node.sdo['Encoder']['EncPos'].raw
                    _rt_d_r   = _rt_raw_r - _rt_raw_prev_r
                    if _rt_d_r >  enc_resolution / 2: _rt_d_r -= enc_resolution
                    if _rt_d_r < -enc_resolution / 2: _rt_d_r += enc_resolution
                    _rt_raw_acc_r  += _rt_d_r
                    _rt_raw_prev_r  = _rt_raw_r
                    _rt_comp_r = (_rt_enc_r - _rt_raw_r + enc_resolution // 2) % enc_resolution - enc_resolution // 2
                    _rt_enc_acc_r = _rt_raw_acc_r + _rt_comp_r
                    if _rts < RETEST_N_TOTAL:
                        _rt_exp_r = -e_polarity * _rts_elec_r * cts_per_elec
                        _rt_err_r = (_rt_enc_acc_r - _rt_exp_r) / cts_per_elec * 360.0
                        _rt_results_rev.append((_rts / RETEST_N_TOTAL * 360.0,
                                                _rts_cyc_r / RETEST_N_PER_CYCLE * 360.0,
                                                _rts // RETEST_N_PER_CYCLE + 1,
                                                _rt_err_r))

                # Average forward and reverse at matched positions to cancel friction.
                _rt_n_sf = len(_rt_results_fwd)
                _rt_results = [(_rt_results_fwd[i][0], _rt_results_fwd[i][1],
                                _rt_results_fwd[i][2],
                                (_rt_results_fwd[i][3]
                                 + _rt_results_rev[(_rt_n_sf - i) % _rt_n_sf][3]) / 2.0)
                               for i in range(_rt_n_sf)]
                print("  Retest bidirectional average complete ({} points).".format(_rt_n_sf))

                self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE
                self.UpdateUI(98)

                # Stats comparison
                _rt_errs   = [r[3] for r in _rt_results]
                _rt_maxerr = max(_rt_errs, key=abs)
                _rt_rms    = (sum(e*e for e in _rt_errs) / len(_rt_errs)) ** 0.5
                print("\n  Linearity comparison:")
                print("  {:30s}  {:>10s}  {:>10s}".format("", "Max err (°)", "RMS (°)"))
                print("  {:30s}  {:>10.3f}  {:>10.3f}".format(
                    "Before compensation:", _lin_max_err, _lin_rms_err))
                print("  {:30s}  {:>10.3f}  {:>10.3f}".format(
                    "With compensation active:", _rt_maxerr, _rt_rms))
                _impr_rms = (1.0 - _rt_rms / _lin_rms_err) * 100.0 if _lin_rms_err else 0.0
                print("  RMS improvement: {:.1f}%".format(_impr_rms))

                # DC bias: mean of bidirectional-averaged errors. Friction is canceled by
                # averaging, so this is the encoder geometric mean offset — a sweep-anchor
                # artifact (it tracks where the sweep started), NOT stored to the puck.
                # Remove it from BOTH before and after so the AC-only metric compares like
                # with like; stripping it from only the "after" side overstated improvement.
                _rt_dc_bias  = sum(_rt_errs) / len(_rt_errs) if _rt_errs else 0.0
                _rt_errs_ac  = [e - _rt_dc_bias for e in _rt_errs]
                _rt_rms_ac   = (sum(e*e for e in _rt_errs_ac) / len(_rt_errs_ac)) ** 0.5
                _lin_dc_bias = sum(_lin_errs) / len(_lin_errs) if _lin_errs else 0.0
                _lin_rms_ac  = (sum((e - _lin_dc_bias) ** 2 for e in _lin_errs)
                                / len(_lin_errs)) ** 0.5 if _lin_errs else 0.0
                _impr_ac     = (1.0 - _rt_rms_ac / _lin_rms_ac) * 100.0 if _lin_rms_ac else 0.0
                print("  DC bias (not stored to puck): before {:+.3f}°  after {:+.3f}°".format(
                    _lin_dc_bias, _rt_dc_bias))
                print("  AC-only RMS: {:.3f}° → {:.3f}°  ({:.1f}% improvement)".format(
                    _lin_rms_ac, _rt_rms_ac, _impr_ac))
                _rt_passed_stat = _rt_rms_ac < _lin_rms_ac
                print("  Retest result: {}".format(
                    "PASS — AC RMS improved" if _rt_passed_stat else "FAIL — no improvement"))

                # DYNAMIC HOLD-STABILITY GATE (steep corrections only). The static retest above is open-loop
                # stepped (~zero speed) and CANNOT see the CLOSED-LOOP 0-cmd hold runaway the slope guard
                # exists for. enc-comp fades out at speed, so the risk lives entirely at hold/low speed. When
                # the uploaded correction is steep enough to matter, drive a brief closed-loop hold + low
                # crawl and compare velocity oscillation comp-ON vs comp-OFF: if ON hunts materially more
                # than OFF, the correction destabilises the hold -> fail. Gentle corrections skip this (fast).
                HOLD_GATE_SLOPE = 0.20
                def _hold_stability():
                    _CRAWL = max(1, int(round(8.0 * enc_resolution / 60.0)))   # ~8 RPM (below the comp fade)
                    def _sample(_vcmd, _secs):
                        self.node.sdo['TargetVelocity'].raw = int(_vcmd)
                        _sleep_responsive(0.6)                                  # reach the command
                        _vs = []; _imax = 0.0; _t0 = time.time()
                        while time.time() - _t0 < _secs:
                            _v  = self.node.sdo['VelocityFeedback'].raw
                            _id = self.node.sdo['Motor']['id'].raw / 1000.0 * i_peak
                            _iq = self.node.sdo['CurrentFeedback'].raw / 1000.0 * i_peak
                            _imax = max(_imax, (_id * _id + _iq * _iq) ** 0.5)
                            if self.node.sdo['StatusWord'].raw & 0x08:          # drive faulted -> unstable
                                return None, _imax
                            _vs.append(_v); _yield()
                        return _vs, _imax
                    def _measure(_tag):
                        _hv, _hi = _sample(0, 2.2)                              # 0-cmd HOLD
                        if _hv is None:
                            return None
                        _cv, _ci = _sample(_CRAWL, 2.2)                         # low-speed CRAWL
                        if _cv is None:
                            return None
                        _hpp  = (max(_hv) - min(_hv)) if _hv else 0.0
                        _cm   = (sum(_cv) / len(_cv)) if _cv else 0.0
                        _cstd = ((sum((x - _cm) ** 2 for x in _cv) / len(_cv)) ** 0.5) if _cv else 0.0
                        print("    {}: hold pk-pk {:.0f} cts/s | crawl ripple {:.0f} cts/s(rms) | "
                              "|I|max {:.0f} mA".format(_tag, _hpp, _cstd, max(_hi, _ci)))
                        return max(_hpp, _cstd)
                    print("  Dynamic hold-stability gate (slope {:.2f} > {:.2f}) — closed-loop hold + ~8 RPM "
                          "crawl, comp ON vs OFF:".format(_applied_slope, HOLD_GATE_SLOPE))
                    self.node.sdo['SetModeOfOperation'].raw = MODE_IDLE
                    self.node.sdo['ControlWord'].raw = CLEAR_FAULT
                    self.node.sdo['ControlWord'].raw = SHUTDOWN
                    self.node.sdo['ControlWord'].raw = OP_ENABLED
                    self.node.sdo['SetModeOfOperation'].raw = MODE_PROFILE_VEL
                    try:
                        self._enc_comp_write(1, 1);  _on  = _measure("comp ON ")
                        self._enc_comp_write(1, 0);  _off = _measure("comp OFF")
                        self._enc_comp_write(1, 1)                        # restore ON; the gate decides
                    finally:
                        self.node.sdo['TargetVelocity'].raw = 0
                        self.node.sdo['SetModeOfOperation'].raw = MODE_IDLE
                    if _on is None:
                        print("    comp ON faulted/hunted the hold -> UNSTABLE"); return False
                    _off = 0.0 if _off is None else _off
                    _floor = max(30.0, 0.02 * _CRAWL)                           # cts/s oscillation noise floor
                    _bad = _on > _floor and _on > 2.0 * max(_off, _floor)
                    print("    verdict: ON osc {:.0f} vs OFF osc {:.0f} cts/s (floor {:.0f}) -> {}".format(
                        _on, _off, _floor, "HUNTS — revert" if _bad else "stable — keep"))
                    return not _bad

                _hold_ok = True
                if _rt_passed_stat and not _test_only and _applied_slope > HOLD_GATE_SLOPE:
                    try:
                        _hold_ok = _hold_stability()
                    except Exception as _he:
                        print("  (hold-stability gate error: {} — leaving comp as-is per static retest)"
                              .format(_he))
                        _hold_ok = True   # a harness error must not revert a statically-good correction

                # GATE: never LEAVE a compensation that didn't help (static) or that destabilises the hold
                # (dynamic).  It had to be uploaded + activated + saved above so both tests could measure it
                # live — but if either fails, REVERT: zero the bins, deactivate, and persist OFF, so a
                # poor/counterproductive table is never left active in EEPROM.  (Static FAIL is common on an
                # UNGEARED motor with no load/damping: the free rotor rings + cogs at each commanded step, so
                # the measured "error" isn't a repeatable map and a fit to it makes linearity WORSE.)
                if (not _rt_passed_stat) or (not _hold_ok):
                    print("  GATING: compensation {} — reverting to OFF "
                          "(clearing table + disabling + saving OFF).".format(
                              "did not improve linearity" if not _rt_passed_stat
                              else "destabilised the closed-loop hold (dynamic)"))
                    try:
                        self._enc_comp_write(1, 0)            # Encoder Compensation Active = OFF
                        for _bi in range(N_BINS):                    # zero every bin so nothing stale
                            self._enc_comp_write(2 + _bi * 3, 0)   # A_s
                            self._enc_comp_write(3 + _bi * 3, 0)   # k
                            self._enc_comp_write(4 + _bi * 3, 0)   # A_c
                        self._enc_comp_save(N_BINS)                  # persist the OFF/cleared state
                        try:
                            self.frame_menubar.ON.Check(False)
                            self.frame_menubar.OFF.Check(True)
                        except Exception:
                            pass
                        print("  Encoder compensation DISABLED and OFF state saved to EEPROM.")
                    except Exception as _ge:
                        print("  (gating revert failed: {})".format(_ge))

                # Comparison plot
                try:
                    import matplotlib
                    matplotlib.use('Agg')
                    import matplotlib.pyplot as _cplt

                    _rt_mech   = [r[0] for r in _rt_results]
                    _rt_edeg   = [r[3] - _rt_dc_bias for r in _rt_results]
                    _orig_mech = [r[0] for r in lin_results]
                    _orig_edeg = [r[3] - _lin_dc_bias for r in lin_results]
                    _yr2 = max(max(abs(e) for e in _orig_edeg),
                               max(abs(e) for e in _rt_edeg)) * 1.15 or 1.0

                    _cfig, (_cax1, _cax2) = _cplt.subplots(1, 2, figsize=(14, 5),
                                                             sharey=True)
                    _cfig.suptitle(
                        'Encoder Linearity Comparison — Node {}  {}  ({})\n'
                        'Before vs After Compensation  '
                        '(RMS: {:.3f}° → {:.3f}° AC  {:.1f}% improvement'
                        '  |  DC bias {:+.1f}° subtracted from plot)'.format(
                            node_id, model_str, ts,
                            _lin_rms_ac, _rt_rms_ac, _impr_ac, _rt_dc_bias),
                        fontsize=11)

                    _cax1.plot(_orig_mech, _orig_edeg, 'b-', linewidth=0.8)
                    _cax1.axhline(0, color='k', linewidth=0.8, linestyle='--')
                    _cax1.axhline( PASS_THRESHOLD, color='r', linewidth=1.0,
                                   linestyle='--', label='±{:.0f}° limit'.format(
                                       PASS_THRESHOLD))
                    _cax1.axhline(-PASS_THRESHOLD, color='r', linewidth=1.0,
                                   linestyle='--')
                    _cax1.set_ylim(-_yr2, _yr2)
                    _cax1.set_xlabel('Mechanical angle (°)')
                    _cax1.set_ylabel('Error (° electrical)')
                    _cax1.set_title('Before compensation  (AC RMS={:.3f}°)'.format(
                        _lin_rms_ac))
                    _cax1.legend(fontsize=8)
                    _cax1.grid(True, alpha=0.3)

                    _cax2.plot(_rt_mech, _rt_edeg, 'g-', linewidth=0.8)
                    _cax2.axhline(0, color='k', linewidth=0.8, linestyle='--')
                    _cax2.axhline( PASS_THRESHOLD, color='r', linewidth=1.0,
                                   linestyle='--', label='±{:.0f}° limit'.format(
                                       PASS_THRESHOLD))
                    _cax2.axhline(-PASS_THRESHOLD, color='r', linewidth=1.0,
                                   linestyle='--')
                    _cax2.set_ylim(-_yr2, _yr2)
                    _cax2.set_xlabel('Mechanical angle (°)')
                    _cax2.set_title('[COMPENSATION ACTIVE]  (AC RMS={:.3f}°  DC bias {:+.1f}° removed)'.format(
                        _rt_rms_ac, _rt_dc_bias))
                    _cax2.legend(fontsize=8)
                    _cax2.grid(True, alpha=0.3)

                    _cplt.tight_layout()
                    _cplot_path = _enc_img(
                        '{}enc_linearity_compensation_active_{}.png'.format(_file_pfx, ts))
                    _cplt.savefig(_cplot_path, dpi=100)
                    _cplt.close(_cfig)
                    print("  Comparison plot → {}".format(_cplot_path))
                except ImportError:
                    print("  (Comparison plot skipped — matplotlib not installed)")
                except Exception as _cpe:
                    print("  WARNING: Comparison plot failed: {}".format(_cpe))

                # Retest linearity plot (3-panel) — same format as calibration plot
                try:
                    import matplotlib
                    matplotlib.use('Agg')
                    import matplotlib.pyplot as _rtplt
                    import matplotlib.gridspec as _rtgs

                    _rt_mech_all2 = [r[0] for r in _rt_results]
                    _rt_err_all2  = [r[3] - _rt_dc_bias for r in _rt_results]
                    _rt_maxerr_ac = max(_rt_errs_ac, key=abs)
                    _rt_passed    = _rt_rms_ac < _lin_rms_ac

                    _rtfig = _rtplt.figure(figsize=(15, 8))
                    _rtfig.suptitle(
                        'Encoder Linearity [COMPENSATION ACTIVE] — Node {}  {}  ({})\n'
                        '{} pole pairs  {} steps/elec cycle  '
                        'AC RMS {:.3f}° → {:.3f}°  ({:.1f}% improvement)'
                        '  |  DC bias {:+.1f}° subtracted'.format(
                            node_id, model_str, ts, pole_pairs, RETEST_N_PER_CYCLE,
                            _lin_rms_ac, _rt_rms_ac, _impr_ac, _rt_dc_bias),
                        fontsize=13)
                    _rtgs_obj = _rtgs.GridSpec(2, 2, figure=_rtfig,
                                               width_ratios=[1.2, 1])
                    _rtax1 = _rtfig.add_subplot(_rtgs_obj[0, 0])
                    _rtax2 = _rtfig.add_subplot(_rtgs_obj[1, 0])
                    _rtax3 = _rtfig.add_subplot(_rtgs_obj[:, 1])

                    _rt_pf_lbl = 'Pass/Fail limit (±{:.0f}°)'.format(PASS_THRESHOLD)
                    _rtax1.plot(_rt_mech_all2, _rt_err_all2, 'g-', linewidth=0.8,
                                label='Measured error')
                    _rtax1.axhline(0, color='k', linewidth=2.5, linestyle=':',
                                   label='Expected (0° error)', zorder=5)
                    _rtax1.axhline( PASS_THRESHOLD, color='r', linewidth=1.5,
                                    linestyle='--', label=_rt_pf_lbl)
                    _rtax1.axhline(-PASS_THRESHOLD, color='r', linewidth=1.5,
                                    linestyle='--')
                    _rtax1.set_xlabel('Mechanical angle (°)')
                    _rtax1.set_ylabel('Error (° electrical)')
                    _rtax1.set_title(
                        'Compensated encoder linearity — error vs mechanical angle  [{}]'.format(
                            'PASS — AC RMS improved' if _rt_passed else 'FAIL — no improvement'))
                    _rtax1.legend(fontsize=8)
                    _rtax1.grid(True, alpha=0.3)

                    _rt_colors = _rtplt.cm.tab10.colors
                    for _rt_cyc in range(1, pole_pairs + 2):
                        _rt_cyc_pts = [(r[1], r[3] - _rt_dc_bias) for r in _rt_results
                                       if r[2] == _rt_cyc]
                        if _rt_cyc_pts:
                            _rt_xs, _rt_ys = zip(*_rt_cyc_pts)
                            _rtax2.plot(_rt_xs, _rt_ys,
                                        color=_rt_colors[(_rt_cyc - 1) % 10],
                                        alpha=0.8, linewidth=0.9,
                                        label='Cycle {}'.format(_rt_cyc))
                    _rtax2.axhline(0, color='k', linewidth=2.5, linestyle=':', zorder=5)
                    _rtax2.axhline( PASS_THRESHOLD, color='r', linewidth=1.5,
                                    linestyle='--', label=_rt_pf_lbl)
                    _rtax2.axhline(-PASS_THRESHOLD, color='r', linewidth=1.5,
                                    linestyle='--')
                    _rtax2.set_xlabel('Electrical angle (°)')
                    _rtax2.set_ylabel('Error (° electrical)')
                    _rtax2.set_title(
                        'Compensated — overlaid by electrical cycle\n'
                        'consistent = residual electrical err; shifting = mechanical err')
                    _rtax2.legend(fontsize=7, ncol=4)
                    _rtax2.grid(True, alpha=0.3)

                    # Lissajous per electrical cycle
                    _rt_cyc_base = {}
                    for _rtr in _rt_results:
                        if int(round(_rtr[1] / 360.0 * RETEST_N_PER_CYCLE)) % RETEST_N_PER_CYCLE == 0:
                            _rt_cyc_base.setdefault(_rtr[2], _rtr[3])
                    _rt_cyc_profs = {}
                    for _rtr in _rt_results:
                        _rt_s = int(round(_rtr[1] / 360.0 * RETEST_N_PER_CYCLE)) % RETEST_N_PER_CYCLE
                        _rt_cyc_profs.setdefault(_rtr[2], {})[_rt_s] = (
                            _rtr[3] - _rt_cyc_base.get(_rtr[2], 0.0))
                    _rt_all_wc = [e for d in _rt_cyc_profs.values()
                                  for e in d.values()]
                    _rt_max_ae = max(abs(e) for e in _rt_all_wc) if _rt_all_wc else 1.0
                    _rt_scale  = 0.4 / (_rt_max_ae or 1.0)
                    _rt_circ_t = [i / 360 * 2 * math.pi for i in range(361)]
                    _rtax3.plot([math.cos(t) for t in _rt_circ_t],
                                [math.sin(t) for t in _rt_circ_t],
                                'k', linewidth=2.5, linestyle=':', label='Ideal',
                                zorder=5)
                    for _rt_cn in sorted(_rt_cyc_profs.keys()):
                        _rt_prof = _rt_cyc_profs[_rt_cn]
                        if len(_rt_prof) < RETEST_N_PER_CYCLE:
                            continue
                        _rt_steps = sorted(_rt_prof.keys())
                        _rt_ts2 = ([s / RETEST_N_PER_CYCLE * 2.0 * math.pi
                                    for s in _rt_steps] + [0.0])
                        _rt_es2 = [_rt_prof[s] for s in _rt_steps] + [0.0]
                        _rt_xs3 = [(1.0 + _rt_scale * e) * math.cos(t)
                                   for t, e in zip(_rt_ts2, _rt_es2)]
                        _rt_ys3 = [(1.0 + _rt_scale * e) * math.sin(t)
                                   for t, e in zip(_rt_ts2, _rt_es2)]
                        _rtax3.plot(_rt_xs3, _rt_ys3,
                                    color=_rt_colors[(_rt_cn - 1) % 10],
                                    alpha=0.7, linewidth=0.9,
                                    label='Cycle {}'.format(_rt_cn))
                    _rtax3.set_aspect('equal')
                    _rtax3.axhline(0, color='gray', linewidth=0.5, zorder=0)
                    _rtax3.axvline(0, color='gray', linewidth=0.5, zorder=0)
                    _rtax3.set_xlabel('cos(ε)')
                    _rtax3.set_ylabel('sin(ε)')
                    _rtax3.set_title(
                        'Compensated Lissajous (per elec. cycle)  [{}]\n'
                        '{:.0f}°/unit  —  residual electrical/mechanical error'.format(
                            'PASS — AC RMS improved' if _rt_passed else 'FAIL — no improvement',
                            1.0 / _rt_scale))
                    _rtax3.legend(fontsize=7, ncol=4)
                    _rtax3.grid(True, alpha=0.3)

                    _rtplt.tight_layout()
                    _rt_lin_path = _enc_img(
                        '{}enc_linearity_retest_{}.png'.format(_file_pfx, ts))
                    _rtplt.savefig(_rt_lin_path, dpi=100)
                    _rtplt.close(_rtfig)
                    print("  Retest linearity plot → {}".format(_rt_lin_path))
                except ImportError:
                    print("  (Retest linearity plot skipped — matplotlib not installed)")
                except Exception as _rtpe:
                    print("  WARNING: Retest linearity plot failed: {}".format(_rtpe))

                # 2-panel FFT plot
                try:
                    import matplotlib
                    matplotlib.use('Agg')
                    import matplotlib.pyplot as _plt

                    fig, (ax1, ax2) = _plt.subplots(1, 2, figsize=(12, 4))
                    fig.suptitle('Encoder Error Spectrum — Node {}  {}  ({})'.format(
                        node_id, model_str, ts), fontsize=11)

                    k_show = min(64, len(amps) - 1)
                    ax1.bar(range(1, k_show + 1), amps[1:k_show + 1],
                            color='steelblue', width=0.8)
                    ax1.axvline(best_n, color='red', linestyle='--',
                                label='N={} (RMS<1ct)'.format(best_n))
                    ax1.set_xlabel('Harmonic k')
                    ax1.set_ylabel('Amplitude (cts)')
                    ax1.set_title('Harmonic amplitudes  ({} sweep pts)'.format(N))
                    ax1.legend(fontsize=8)
                    ax1.grid(True, alpha=0.3)

                    rms_curve = []
                    X_r2 = _np.zeros(N // 2 + 1, dtype=_np.complex128)
                    X_r2[0] = X[0]
                    for _ni2 in range(1, min(50, len(sorted_ks)) + 1):
                        X_r2[sorted_ks[_ni2 - 1]] = X[sorted_ks[_ni2 - 1]]
                        recon2 = _np.fft.irfft(X_r2, n=N)
                        rms_curve.append(float(_np.sqrt(_np.mean((tf - recon2) ** 2))))
                    ax2.plot(range(1, len(rms_curve) + 1), rms_curve, 'b-o', markersize=3)
                    ax2.axhline(1.0, color='red', linestyle='--', label='1 ct threshold')
                    ax2.axvline(best_n, color='red', linestyle=':',
                                label='N={}'.format(best_n))
                    ax2.set_xlabel('Number of harmonics')
                    ax2.set_ylabel('RMS error (cts)')
                    ax2.set_title('Reconstruction RMS vs harmonic count')
                    ax2.legend(fontsize=8)
                    ax2.grid(True, alpha=0.3)

                    _plt.tight_layout()
                    fft_plot_path = _enc_img('{}enc_correction_fft_{}.png'.format(_file_pfx, ts))
                    _plt.savefig(fft_plot_path, dpi=100)
                    _plt.close(fig)
                    print("  FFT plot → {}".format(fft_plot_path))
                except ImportError:
                    print("  (FFT plot skipped — matplotlib not installed)")
                except Exception as _fpe:
                    print("  WARNING: FFT plot failed: {}".format(_fpe))

            except _EncTestOnly:
                pass    # test-only (old firmware): measured + reported, upload/retest intentionally skipped
            except ImportError:
                print("  (FFT analysis skipped — numpy not installed)")
            except SdoAbortedError as _fft_exc:
                if _fft_exc.code == 0x06020000:
                    # Object does not exist — look it up in the EDS so we can
                    # name the missing object and describe what it should hold.
                    _DTYPE_MAP = {
                        0x0002: 'INTEGER8',   0x0003: 'INTEGER16', 0x0004: 'INTEGER32',
                        0x0005: 'UNSIGNED8',  0x0006: 'UNSIGNED16', 0x0007: 'UNSIGNED32',
                        0x0008: 'REAL32',     0x0009: 'VISIBLE_STRING',
                    }
                    # In this block the first 0x06020000 is the 0x3027
                    # (EncCompensation) write — firmware doesn't support it.
                    _obj_idx = 0x3027
                    _obj_desc = '0x{:04X}'.format(_obj_idx)
                    _sub_descs = []
                    try:
                        _od = self.node.object_dictionary[_obj_idx]
                        _obj_desc = '0x{:04X} "{}" ({} sub-entries per EDS)'.format(
                            _obj_idx, _od.name, len(_od))
                        for _si, _label in [(1, 'active flag'), (2, 'bin amplitude'),
                                            (3, 'bin harmonic k'), (4, 'bin phase (mrad)')]:
                            try:
                                _sv = _od[_si]
                                _dt = _DTYPE_MAP.get(
                                    getattr(_sv, 'data_type', None), 'unknown')
                                _ac = getattr(_sv, 'access_type', 'rw')
                                _sub_descs.append(
                                    'sub{} "{}" ({}, {}) — {}'.format(
                                        _si, _sv.name, _dt, _ac, _label))
                            except (KeyError, AttributeError):
                                pass
                    except (KeyError, AttributeError):
                        pass
                    print("  WARNING: FFT analysis failed — object does not exist on node {}:".format(
                        node_id))
                    print("    {}".format(_obj_desc))
                    for _sd in _sub_descs:
                        print("    {}".format(_sd))
                    print("  Encoder harmonic compensation (0x3027) is not supported by "
                          "this firmware — analysis results saved, upload skipped.")
                else:
                    print("  WARNING: FFT analysis failed — SDO abort 0x{:08X}: {}".format(
                        _fft_exc.code, _fft_exc))
            except Exception as _fft_exc:
                print("  WARNING: FFT analysis failed: {}".format(_fft_exc))

            # ── Harmonic reconstruction vs original linearity figure ───────────
            try:
                import numpy as _np2
                import matplotlib
                matplotlib.use('Agg')
                import matplotlib.pyplot as _rplt

                # FFT on raw sweep data (same source as the analysis block above)
                _N_sw = N_S
                _tf2  = _np2.array(corr_seq, dtype=_np2.float64)
                _X2   = _np2.fft.rfft(_tf2)
                _amps2   = 2.0 * _np2.abs(_X2) / _N_sw
                _phases2 = _np2.angle(_X2)
                _amps2[0]  /= 2.0
                _amps2[-1] /= 2.0
                _order2 = 1 + _np2.argsort(_amps2[1:])[::-1]

                # Top-3 dominant harmonics (for individual component panel)
                _n_harm_plot = min(3, len(_order2))
                _top_ks   = [int(_order2[i]) for i in range(_n_harm_plot)]
                _top_amps = [float(_amps2[k]) for k in _top_ks]
                _top_phis = [float(_phases2[k]) for k in _top_ks]
                _dc_off   = float(_amps2[0])

                # Best-n harmonics: min count for <1 ct RMS vs raw sweep data
                _sorted_ks2 = [int(_order2[i]) for i in range(len(_order2))]
                _X2_recon = _np2.zeros(_N_sw // 2 + 1, dtype=_np2.complex128)
                _X2_recon[0] = _X2[0]
                _best_n_val = len(_sorted_ks2)
                for _bni in range(1, len(_sorted_ks2) + 1):
                    for _bki in range(_bni):
                        _X2_recon[_sorted_ks2[_bki]] = _X2[_sorted_ks2[_bki]]
                    _brecon = _np2.fft.irfft(_X2_recon, n=_N_sw)
                    if float(_np2.sqrt(_np2.mean((_tf2 - _brecon) ** 2))) < 1.0:
                        _best_n_val = _bni
                        break

                # Reconstruct at sweep points using irfft (exact, fast)
                _mech_sw = _np2.arange(_N_sw) / _N_sw * 360.0

                # Build irfft reconstructions for each bin count (+ DC always included)
                def _irfft_topn(n_bins):
                    _Xr = _np2.zeros(_N_sw // 2 + 1, dtype=_np2.complex128)
                    _Xr[0] = _X2[0]
                    for _ki in _sorted_ks2[:n_bins]:
                        _Xr[_ki] = _X2[_ki]
                    return _np2.fft.irfft(_Xr, n=_N_sw)

                _bins_to_test = [3, 5, 10, _best_n_val]
                # Deduplicate and clamp to available harmonics
                _n_avail = len(_sorted_ks2)
                _bins_to_test = sorted(set(min(b, _n_avail) for b in _bins_to_test))

                _recon_degs = {}
                _resid_degs = {}
                _rms_vals   = {}
                _err_orig_deg = -_tf2 / cts_per_elec * 360.0
                for _nb in _bins_to_test:
                    _rc = _irfft_topn(_nb)
                    _rd = -_rc / cts_per_elec * 360.0
                    _rs = _err_orig_deg - _rd
                    _recon_degs[_nb] = _rd
                    _resid_degs[_nb] = _rs
                    _rms_vals[_nb]   = float(_np2.sqrt(_np2.mean(_rs ** 2)))

                _yr = float(_np2.abs(_err_orig_deg).max()) * 1.15 or 1.0

                # Individual top-3 harmonic waves at sweep points
                _harm_waves_deg = []
                for _k, _A, _phi in zip(_top_ks, _top_amps, _top_phis):
                    _w = _A * _np2.cos(2.0 * _np2.pi * _k * _np2.arange(_N_sw) / _N_sw + _phi)
                    _harm_waves_deg.append(-_w / cts_per_elec * 360.0)

                # Colour palette: one colour per bin count
                _bin_colors = {3: 'tab:red', 5: 'tab:orange', 10: 'tab:purple',
                               _best_n_val: 'darkgreen'}
                # Fill any extra deduplicated values
                _extra_colors = ['tab:cyan', 'tab:brown', 'tab:pink']
                _ec_idx = 0
                for _nb in _bins_to_test:
                    if _nb not in _bin_colors:
                        _bin_colors[_nb] = _extra_colors[_ec_idx % len(_extra_colors)]
                        _ec_idx += 1

                _rfig, _raxes = _rplt.subplots(2, 2, figsize=(16, 10))
                _rfig.suptitle(
                    'Harmonic Reconstruction vs Raw Encoder Error — Node {}  {}  ({})\n'
                    'Top-3 k={}  |  Best-{} harmonics (RMS<1ct vs raw sweep)'.format(
                        node_id, model_str, ts,
                        '/'.join(str(k) for k in _top_ks),
                        _best_n_val),
                    fontsize=11)

                # [0,0]: Original raw sweep error
                _ax = _raxes[0, 0]
                _ax.plot(_mech_sw, _err_orig_deg, 'b-', linewidth=0.7,
                         label='Raw sweep error')
                _ax.axhline(0, color='k', linewidth=0.8, linestyle='--')
                _ax.set_ylim(-_yr, _yr)
                _ax.set_xlabel('Mechanical angle (°)')
                _ax.set_ylabel('Error (° electrical)')
                _ax.set_title('Original measured encoder error  ({} pts)'.format(_N_sw))
                _ax.legend(fontsize=8)
                _ax.grid(True, alpha=0.3)

                # [0,1]: Reconstruction comparison — top-3 / top-5 / top-10 / best-n
                _ax = _raxes[0, 1]
                _ax.plot(_mech_sw, _err_orig_deg, 'b-', linewidth=0.6, alpha=0.4,
                         label='Raw sweep (reference)')
                for _nb in _bins_to_test:
                    _lw = 1.6 if _nb == _best_n_val else 1.0
                    _label = 'Top-{}{} (RMS={:.3f}°)'.format(
                        _nb,
                        ' ✓best' if _nb == _best_n_val else '',
                        _rms_vals[_nb])
                    _ax.plot(_mech_sw, _recon_degs[_nb],
                             color=_bin_colors[_nb], linewidth=_lw, label=_label)
                _ax.axhline(0, color='k', linewidth=0.8, linestyle='--')
                _ax.set_ylim(-_yr, _yr)
                _ax.set_xlabel('Mechanical angle (°)')
                _ax.set_ylabel('Error (° electrical)')
                _ax.set_title('Reconstruction: top-3 / 5 / 10 / best-{}'.format(_best_n_val))
                _ax.legend(fontsize=8)
                _ax.grid(True, alpha=0.3)

                # [1,0]: Individual top-3 harmonic components
                _ax = _raxes[1, 0]
                _hcolors = ['tab:orange', 'tab:green', 'tab:purple']
                for _hi, (_hwave, _hk, _hA) in enumerate(
                        zip(_harm_waves_deg, _top_ks, _top_amps)):
                    _ax.plot(_mech_sw, _hwave,
                             color=_hcolors[_hi % len(_hcolors)], linewidth=1.2,
                             label='k={} ({:.3f} cts / {:.2f}°)'.format(
                                 _hk, _hA, _hA / cts_per_elec * 360.0))
                _ax.axhline(0, color='k', linewidth=0.8, linestyle='--')
                _ax.set_xlabel('Mechanical angle (°)')
                _ax.set_ylabel('Error contribution (° electrical)')
                _ax.set_title('Individual top-3 harmonic components')
                _ax.legend(fontsize=8)
                _ax.grid(True, alpha=0.3)

                # [1,1]: Residual comparison — top-3 / top-5 / top-10 / best-n
                _ax = _raxes[1, 1]
                for _nb in _bins_to_test:
                    _lw = 1.6 if _nb == _best_n_val else 0.9
                    _label = 'Residual top-{}{} (RMS={:.3f}°)'.format(
                        _nb,
                        ' ✓best' if _nb == _best_n_val else '',
                        _rms_vals[_nb])
                    _ax.plot(_mech_sw, _resid_degs[_nb],
                             color=_bin_colors[_nb], linewidth=_lw,
                             alpha=0.85, label=_label)
                _ax.axhline(0, color='k', linewidth=0.8, linestyle='--')
                _ax.set_title('Residual after harmonic removal')
                _ax.set_xlabel('Mechanical angle (°)')
                _ax.set_ylabel('Residual (° electrical)')
                _ax.legend(fontsize=8)
                _ax.grid(True, alpha=0.3)

                _rplt.tight_layout()
                _recon_plot_path = _enc_img('{}enc_harmonic_recon_{}.png'.format(_file_pfx, ts))
                _rplt.savefig(_recon_plot_path, dpi=100)
                _rplt.close(_rfig)
                print("  Reconstruction plot → {}".format(_recon_plot_path))
            except ImportError:
                print("  (Reconstruction plot skipped — numpy/matplotlib not installed)")
            except Exception as _rpe:
                print("  WARNING: Reconstruction plot failed: {}".format(_rpe))

        except Exception as _exc:
            self._cal_fault(_exc)
        finally:
            self.OnTaskComplete()
            self.choice_test.SetSelection(0)
            if self.ADC_ON == False and self.adcWasON:
                self.on_off_adc(self)
            self.Enable()

    def _load_enc_correction_table(self):
        """Return (table, path) from the most recent enc_correction_full CSV, or (None, None)."""
        import os, glob
        from ..paths import resource_path
        log_dir = resource_path('logs')
        hits = sorted(glob.glob(os.path.join(log_dir, '**', 'enc_correction_full_*.csv'),
                                recursive=True))
        if not hits:
            return None, None
        path  = hits[-1]
        table = []
        with open(path) as _f:
            for _line in _f:
                _line = _line.strip()
                if not _line or _line.startswith('#') \
                        or _line.startswith('enc') or _line.startswith('pos'):
                    continue
                _parts = _line.split(',')
                if len(_parts) == 2:
                    try:
                        table.append(int(_parts[1]))
                    except ValueError:
                        pass
        return (table, path) if table else (None, None)

    def test_pvca_torque(self, event):
        """
        Open the PVCA Torque Control dialog.

        Loads the most recent encoder correction table from logs/ and starts a
        50 Hz control loop that applies the user-specified torque using corrected
        theta_e (Mode 12 — Phase Voltage Commutation Angle).
        """
        if not self.check_for_node():
            return

        table, path = self._load_enc_correction_table()
        if table is None:
            wx.MessageBox(
                "No encoder correction table found in logs/.\n"
                "Run 'Generate Encoder Correction Table' first.",
                "PVCA Control", wx.OK | wx.ICON_ERROR)
            return

        try:
            import os
            e_zero         = self.node.sdo['Calibration']['e_zero'].raw
            e_polarity     = int(self.node.sdo['Calibration']['e_polarity'].raw)
            enc_resolution = self.node.sdo['EncoderConfig']['Resolution'].raw
            motor_poles    = self.node.sdo['Calibration']['poles'].raw
            cts_per_elec   = enc_resolution * 2.0 / motor_poles
            Kt             = self.node.sdo['Calibration']['kt'].raw           # mNm/A
            Rt             = self.node.sdo['Calibration']['rt'].raw * 0.01    # 0.01Ω → Ω
            V_bus          = self.node.sdo['Amp']['NominalBusVoltage'].raw * 0.1  # V×10 → V
            i_peak         = self.node.sdo['Calibration']['i_peak'].raw       # mA
        except Exception as e:
            wx.MessageBox("Error reading motor parameters:\n{}".format(e),
                          "PVCA Control", wx.OK | wx.ICON_ERROR)
            return

        if len(table) != enc_resolution:
            wx.MessageBox(
                "Table has {} entries but encoder resolution is {} cts.\n"
                "Re-run 'Generate Encoder Correction Table'.".format(
                    len(table), enc_resolution),
                "PVCA Control", wx.OK | wx.ICON_WARNING)

        # Back-EMF constant: lambda_pm ≈ V_bus / omega_e_no_load
        # Used to compensate for speed-dependent voltage drop in _on_tpdo1.
        try:
            no_load_rpm = self.node.sdo[0x3024][6].raw
        except Exception:
            no_load_rpm = 0
        pole_pairs = motor_poles // 2
        omega_e_nl = no_load_rpm * (math.pi / 30.0) * pole_pairs  # elec rad/s
        lambda_pm  = V_bus / omega_e_nl if omega_e_nl > 0 else 0.0

        mp = dict(e_zero=e_zero, e_polarity=e_polarity,
                  enc_resolution=enc_resolution, cts_per_elec=cts_per_elec,
                  Kt=Kt, Rt=Rt, V_bus=V_bus, i_peak=i_peak,
                  lambda_pm=lambda_pm)

        print("PVCA Torque Control — table: {} ({} entries)".format(
            os.path.basename(path), len(table)))
        print("  Kt={}mNm/A  Rt={:.2f}Ω  V_bus={:.1f}V  i_peak={}mA".format(
            Kt, Rt, V_bus, i_peak))
        print("  e_zero={}  e_polarity={}  cts_per_elec={:.2f}".format(
            e_zero, e_polarity, cts_per_elec))

        dlg = _PVCATorqueDialog(self, self.node, table, path, mp)
        dlg.ShowModal()
        dlg.Destroy()

    def error_compensation_state(self, event):  # wxGlade: puckutilityapp_frame.<event_handler>
        if self.check_for_node() == False:
            return
        if not self._fw_at_least(4, 4, 0):
            self._prompt_ok("Firmware Too Old",
                "Encoder error compensation requires firmware v4.4.0 or later.\n"
                "Please update the firmware and try again.")
            # Revert the radio selection -- the feature is unavailable on this
            # firmware, so it cannot be turned ON.
            self.frame_menubar.ON.Check(False)
            self.frame_menubar.OFF.Check(True)
            return
        enable = event.GetId() == self.frame_menubar.ON.GetId()
        try:
            self._enc_comp_write(1, 1 if enable else 0)
            self.node.sdo['Save']['Single'].raw = ((0x3027 << 8) | 1)
            state_str = "ON" if enable else "OFF"
            print("Encoder error compensation set to {} and saved.".format(state_str))
            self.frame_menubar.ON.Check(enable)
            self.frame_menubar.OFF.Check(not enable)
        except Exception as e:
            print("Error setting encoder compensation state: {}".format(e))

    def set_user_dir(self, event):  # wxGlade: wxp3_frame.<event_handler>
        if self.check_for_node() == False: #len(self.network.scanner.nodes) == 0:
          return False
        
        print("Event handler 'set_user_dir'")
        
        self.node.sdo['EncoderConfig']['UserPolarity'].raw = 1 # Assume positive to start
        encoder_resolution = self.node.sdo['EncoderConfig']['Resolution'].raw
        starting_position = self.node.sdo['PositionFeedback'].raw

        self.frame_statusbar.SetStatusText("Please turn motor in positive (+) direction...", 1)
        self.frame_statusbar.Update()
        _yield()

        done = False
        while not done:
          print("Waiting...")
          _sleep_responsive(1)
          ending_position = self.node.sdo['PositionFeedback'].raw
          if abs(starting_position - ending_position) > (encoder_resolution / 8):
            done = True
          
    def open_support_page(self, event):
        print('Opening support page...')
        webbrowser.open_new(r'PuckUtilityAppGuide.pdf')

    def update_all(self, event):
        print('Updating all Pucks...')
        if self.check_for_node() == False: #len(self.network.scanner.nodes) == 0:
            return False
        print(self.network.scanner.nodes)
        starting_id = self.getID()

        # File browser
        if platform.system() == "Windows":
            directory = '../firmware'
        else:
            directory = 'firmware/'

        # File browser
        with wx.FileDialog(self, "Select firmware file", directory, wildcard="BIN files (*.bin;*.ebin)|*.bin;*.ebin",
                      style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as fileDialog:
            if fileDialog.ShowModal() == wx.ID_CANCEL:
                if self.adcWasON == True:
                    self.on_off_adc(self)
                return     # the user changed their mind
            # Proceed loading the file chosen by the user
            pathname = fileDialog.GetPath()

        for i in self.network.scanner.nodes:
            print(i)
            indexID = self.network.scanner.nodes.index(i)
            self.choice_id.SetSelection(indexID) # Move to next ID for calibration
            self.select_id(None)

            print("Updating firmware for Puck {}".format(self.getID()))
            self.browse_fw(self,pathname)

        indexID = self.network.scanner.nodes.index(starting_id)
        self.choice_id.SetSelection(indexID) # Return to starting ID after completion
        self.select_id(None)

    def system_config(self, event, filepath=False): 
        if self.check_for_node() == False: #len(self.network.scanner.nodes) == 0:
            return False
        if filepath == False:
          # File browser
          if platform.system() == "Windows":
              directory = '../'
          else:
              directory = ''

          # File browser
          with wx.FileDialog(self, "Select firmware file", directory, wildcard="Configu files (*.ini|*.ini",
                        style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as fileDialog:
              if fileDialog.ShowModal() == wx.ID_CANCEL:
                  return     # the user changed their mind
              # Proceed loading the file chosen by the user
              filepath = fileDialog.GetPath()
        else:
            filepath = filepath
    
        print("Reading config file...")
        config = configparser.ConfigParser()
        config.read(filepath)
        options = config.sections()
        _processed = []

        for option in options:
            print(option)
            config_id = int(config[option]['ID'])
            fw_version = config[option].get('fw_version')
            fwpath = _resolve_path(config[option].get('fw'), FIRMWARE_DIR)

            if config_id not in self.network.scanner.nodes:
                print('Puck {} not found on network, skipping'.format(config_id))
                continue

            print("Found defaults for Puck {}!".format(config_id))

            # Switch active node to this puck before any operations
            idx = self.network.scanner.nodes.index(config_id)
            self.choice_id.SetSelection(idx)
            self.select_id(None)

            version = flashp4.read_version(self.node)
            if fw_version and fwpath and version != fw_version:
                print('Version {} found. Updating firmware to {}'.format(version, fw_version))
                self.browse_fw(None, fwpath)
                # browse_fw disconnects and rescans — re-select this puck if still present
                if config_id in self.network.scanner.nodes:
                    idx = self.network.scanner.nodes.index(config_id)
                    self.choice_id.SetSelection(idx)
                    self.select_id(None)
            else:
                print('Version {} found.'.format(version))

            csvpath = _resolve_path(config[option]['CSV'], CONFIG_DIR)
            self.file_to_p4(None, csvpath)
            # file_to_p4 replaces self.network with a fresh canopen.Network() that has
            # no scanner data — rescan so subsequent INI entries can be found.
            self.network.scanner.reset()
            self.network.scanner.search()
            time.sleep(0.5)
            self.choice_id.SetItems([str(i) for i in self.network.scanner.nodes])
            _processed.append(config_id)

        if not _processed:
            print('No matching pucks found in INI file on network.')
            return

        # All pucks updated — prompt for calibration once
        msg = "Calibration is required after configuration.\nWould you like to calibrate all Pucks?"
        dlg = wx.MessageDialog(None, msg, 'Warning!', wx.YES_NO | wx.ICON_WARNING)
        answer = dlg.ShowModal()
        if answer == wx.ID_YES:
            self.calibrate_all_pucks(None)
        dlg.Destroy()

    def check_for_node(self):
      #  print('Checking')
      if len(self.network.scanner.nodes) == 0:
        print('No Active Puck! Ending process...')
        return False
      else:
        return True

    # def upload_system_config(self,event):
    #    print('uploading...')

    # def tune_gains(self, event):  # wxGlade: wxp3_frame.<event_handler>
    #     print("Event handler 'tune_gains' not implemented!")
    #     event.Skip()

    # def save_calibration(self, event):  # wxGlade: wxp3_frame.<event_handler>
    #     print("Event handler 'save_calibration' not implemented!")
    #     event.Skip()
    
    def exit_program(self, event):  # wxGlade: wxp3_frame.<event_handler>
        self.Close()
