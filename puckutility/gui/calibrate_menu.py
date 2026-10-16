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
from p4core.calibrate import read_kt
from p4core.calibrate import firmware_version
from p4core import calibrate as calibrate_lib
from p4core import flash as flashp4
from ..paths import _resolve_path, FIRMWARE_DIR, CONFIG_DIR
from p4sysid import drivecal
from .cal_common import _yield, _sleep_responsive, GuiCalReporter
from .cal_board import BoardCal
from .cal_settling import SettlingCal
from .cal_slope import SlopeCal
from .cal_encoder import EncoderCal
from .cal_enctable import EncTableCal

# TODO - No active issues


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


class calibrate(BoardCal, SettlingCal, SlopeCal, EncoderCal, EncTableCal):
    def _cal_fault(self, exc):
        """Shared cleanup called when an SDO or other exception aborts calibration."""
        print("Calibration fault: {}".format(exc))
        if sdo_contention_message(exc):
            print("  >> Possible CAN bus contention — another app may be "
                  "connected to this bus.")
        msg = drivecal.fault_message(self.node, exc)
        self.OnTaskComplete()
        self.frame_statusbar.SetStatusText("Fault — calibration stopped", 1)
        self._prompt_ok("Calibration Fault", msg)

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
        return firmware_version(self.node)

    def _fw_at_least(self, major, minor, patch):
        return self._fw_ver_tuple() >= (major, minor, patch)

    # 0x3027 encoder compensation and the iSense offset: p4sysid.drivecal.
    def _enc_comp_count(self):
        return drivecal.enc_comp_count(self.node)

    def _enc_comp_bins(self):
        return drivecal.enc_comp_bins(self.node)

    def _enc_filt_subs(self):
        return drivecal.enc_filt_subs(self.node)

    def _enc_comp_signed(self, sub):
        return drivecal.enc_comp_signed(self.node, sub)

    def _enc_comp_read(self, sub):
        return drivecal.enc_comp_read(self.node, sub)

    def _enc_comp_write(self, sub, value):
        drivecal.enc_comp_write(self.node, sub, value)

    def _enc_comp_save(self, n_bins):
        drivecal.enc_comp_save(self.node, n_bins)

    def _clear_offset_reg(self):
        drivecal.clear_offset_reg(self.node)

    def cal_reporter(self):
        """A Reporter for the library calibrations, onto this frame."""
        return GuiCalReporter(self)

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
            Kt             = read_kt(self.node)                         # mNm/A
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
            drivecal.enc_comp_write(self.node, 1, 1 if enable else 0)
            drivecal.save_single(self.node, 0x3027, 1)
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
        try:
            r = calibrate_lib.set_user_direction(self.node, self.cal_reporter())
        except Exception as exc:
            self._cal_fault(exc)
            return False
        self.frame_statusbar.SetStatusText(
            "User direction set: user polarity {:+d}".format(r['user_polarity']), 1)
        return True

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
