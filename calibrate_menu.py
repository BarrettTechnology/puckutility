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
from canopen_runner import (
    CLEAR_FAULT, SHUTDOWN, OP_ENABLED,
    MODE_IDLE, MODE_PHASE_VOLTAGE_ANGLE, MODE_PROFILE_TRQ, MODE_PROFILE_VEL,
    MODE_PROFILE_POS,
)
from canopen.sdo import SdoAbortedError
from can_backend import sdo_contention_message
from paths import _resolve_path, FIRMWARE_DIR, CONFIG_DIR

# TODO - No active issues


def _sleep_responsive(seconds, chunk=0.05):
    """Block for `seconds` seconds while letting wx process pending
    events every `chunk` seconds — keeps Windows from marking the app
    "Not Responding" during long calibration waits."""
    end = time.time() + seconds
    while time.time() < end:
        time.sleep(min(chunk, max(0, end - time.time())))
        wx.Yield()


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
            return False
        if self.check_for_node() == False:
            # print("No active puck")
            return False
        print("Running full calibration for Puck {}".format(self.getID()))

        self.frame_statusbar.SetStatusText("Progress: 0%", 1)
        self.progress.Show()
        self.GetStatusBar().Refresh()
        self.GetStatusBar().Update()

        _cog_was_active = False
        try:
            try:
                _cog_was_active = bool(self.node.sdo[0x3028][1].raw)
                if _cog_was_active:
                    self.node.sdo[0x3028][1].raw = 0
                    print("  Cogging compensation disabled for calibration sequence.")
            except Exception:
                pass

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
            self.calibrate_enczero(None, True,
                              _upd=lambda v: self.UpdateUI(72 + v * 28 // 100))

            self.OnTaskComplete()
            self.requireCal = False
        except Exception as e:
            self._cal_fault(e)
        finally:
            if _cog_was_active:
                try:
                    self.node.sdo[0x3028][1].raw = 1
                    print("  Cogging compensation restored (ON).")
                except Exception:
                    pass
            self.Enable()
        #event.Skip()

    def calibrate_ibias(self, event, calAll=False, _upd=None):  # wxGlade: wxp3_frame.<event_handler>
        # print("Event handler 'calibrate_ibias'")
        if calAll==False:
          if self.check_for_node() == False:
            return False
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
        wx.Yield()

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
            _SETTLE = 0.5
            print("Waiting {:.0f} ms for iSense filters to settle...".format(_SETTLE * 1000))
            _settle_end = time.time() + _SETTLE
            while time.time() < _settle_end:
                _frac = 1.0 - (_settle_end - time.time()) / _SETTLE
                _upd(int(_frac * 55))  # 0→55%
                time.sleep(0.05)
                wx.Yield()

            # Average N_AVG fresh reads of Filtered (Q12.4); store as-is (no /16)
            _sum = {'Alpha': 0, 'Beta': 0}
            for _i in range(_N_AVG):
                _upd(55 + _i * 40 // _N_AVG)  # 55→95%
                for _ch in ['Alpha', 'Beta']:
                    _sum[_ch] += self.node.sdo[_ch]['Filtered'].raw
                wx.Yield()

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
        wx.Yield()

        try:
            # Set Alpha & Beta gainfactors to 1.0 in Q4.12
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
                wx.Yield() # keep wx event loop alive so Windows doesn't mark the app "Not Responding"

            _N_IGAIN_AVG  = 100
            _HOLD_TOL_A   = 2.0   # mA — acceptable current error at each hold position
            _HOLD_MAX_S   = 5.0   # s  — max time for closed-loop hold
            _sleep_responsive(1) # Wait for filter to settle after ramp

            # Closed-loop hold at Alpha peak: fine-tune motor_ud so id == calibration_current
            _hold_t0 = time.time()
            while time.time() - _hold_t0 < _HOLD_MAX_S:
                _id_now = self.node.sdo['Motor']['id'].raw / 1000.0 * i_peak
                _err = calibration_current - _id_now
                _upd(30 + int(min(1.0, (time.time() - _hold_t0) / _HOLD_MAX_S) * 15))  # 30→45%
                if abs(_err) < _HOLD_TOL_A:
                    break
                if _id_now > 0:
                    _correction = int(motor_ud * _err / _id_now / 8)
                else:
                    _correction = 100 if _err > 0 else -100
                _correction = max(-300, min(300, _correction))
                motor_ud = max(0, min(32000, motor_ud + _correction))
                self.node.sdo['Motor']['ud'].raw = motor_ud
                time.sleep(0.05)
                wx.Yield()
            _id_now_a = self.node.sdo['Motor']['id'].raw / 1000.0 * i_peak
            print("  Alpha hold: id={:.1f} mA  ud={}  target={:.1f} mA".format(
                _id_now_a, motor_ud, calibration_current))

            _sum_a = _sum_id_a = 0
            for _i in range(_N_IGAIN_AVG):
                _upd(45 + _i * 12 // _N_IGAIN_AVG)  # 45→57%
                _sum_a    += self.node.sdo['Alpha']['Filtered'].raw
                _sum_id_a += self.node.sdo['Motor']['id'].raw
                wx.Yield()
            _q12_4 = self._fw_at_least(4, 4, 0)
            a_filt_f = _sum_a / _N_IGAIN_AVG if _q12_4 else _sum_a / _N_IGAIN_AVG / 16.0
            _id_at_a = (_sum_id_a / _N_IGAIN_AVG) / 1000.0 * i_peak
            print("Peak Alpha = {0:.3f}  id={1:.1f} mA  theta_e={2:.2f} rad  ({3}-sample avg)".format(
                a_filt_f, _id_at_a,
                self.node.sdo['Theta_e'].raw / 32768.0 * 3.14159, _N_IGAIN_AVG))

            self.node.sdo['Theta_e'].raw = -0x4000 # Stall @ Beta Peak (-pi/2)
            _sleep_responsive(1) # Wait for current to settle after theta_e change
            _upd(60)

            # Closed-loop hold at Beta peak: re-tune motor_ud so id == calibration_current
            _hold_t0 = time.time()
            while time.time() - _hold_t0 < _HOLD_MAX_S:
                _id_now = self.node.sdo['Motor']['id'].raw / 1000.0 * i_peak
                _err = calibration_current - _id_now
                _upd(60 + int(min(1.0, (time.time() - _hold_t0) / _HOLD_MAX_S) * 15))  # 60→75%
                if abs(_err) < _HOLD_TOL_A:
                    break
                if _id_now > 0:
                    _correction = int(motor_ud * _err / _id_now / 8)
                else:
                    _correction = 100 if _err > 0 else -100
                _correction = max(-300, min(300, _correction))
                motor_ud = max(0, min(32000, motor_ud + _correction))
                self.node.sdo['Motor']['ud'].raw = motor_ud
                time.sleep(0.05)
                wx.Yield()
            _id_now_b = self.node.sdo['Motor']['id'].raw / 1000.0 * i_peak
            print("  Beta hold:  id={:.1f} mA  ud={}  target={:.1f} mA".format(
                _id_now_b, motor_ud, calibration_current))

            _sum_b = _sum_id_b = 0
            for _i in range(_N_IGAIN_AVG):
                _upd(75 + _i * 20 // _N_IGAIN_AVG)  # 75→95%
                _sum_b    += self.node.sdo['Beta']['Filtered'].raw
                _sum_id_b += self.node.sdo['Motor']['id'].raw
                wx.Yield()
            b_filt_f = _sum_b / _N_IGAIN_AVG if _q12_4 else _sum_b / _N_IGAIN_AVG / 16.0
            _id_at_b = (_sum_id_b / _N_IGAIN_AVG) / 1000.0 * i_peak
            print("Peak Beta  = {0:.3f}  id={1:.1f} mA  theta_e={2:.2f} rad  ({3}-sample avg)".format(
                b_filt_f, _id_at_b,
                self.node.sdo['Theta_e'].raw / 32768.0 * 3.14159, _N_IGAIN_AVG))

            self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE

            # Use high-precision bias from ibias (same session) if available;
            # fall back to the OD value, which fw stores in the same scale it reads.
            if hasattr(self, '_alpha_bias_f') and hasattr(self, '_beta_bias_f'):
                abias_f = self._alpha_bias_f
                bbias_f = self._beta_bias_f
            else:
                abias_f = float(self.node.sdo['Alpha']['Bias'].raw)
                bbias_f = float(self.node.sdo['Beta']['Bias'].raw)

            # Compute gainfactor in full float precision; round only for firmware write.
            # Normalize each channel's ADC deflection by the actual current at that
            # measurement position — makes the result correct even when the closed-loop
            # hold converges to different currents for Alpha vs Beta.
            a_delta = a_filt_f - abias_f
            b_delta = b_filt_f - bbias_f
            # Guard: the gainfactor divide needs real current AND real ADC deflection
            # on BOTH channels. If the hold produced no measurable current (id ~ 0)
            # or the ADC never moved off its bias (delta ~ 0), abort with a clear
            # message instead of a "float division by zero" crash.
            _min_id = 0.3 * calibration_current    # need >= 30% of target current
            if (abs(_id_at_a) < _min_id or abs(_id_at_b) < _min_id
                    or abs(a_delta) < 1.0 or abs(b_delta) < 1.0):
                raise RuntimeError(
                    "iSense gain cal ABORTED: hold produced no measurable current "
                    "(Alpha id={:.1f} mA, Beta id={:.1f} mA, target {:.0f} mA; "
                    "ADC delta alpha={:.1f} beta={:.1f} cts). Rotor holds but the "
                    "current sense reads ~0 -- check that this build updates Motor.id "
                    "and triggers the iSense ADC in VOLTAGE mode.".format(
                        _id_at_a, _id_at_b, calibration_current, a_delta, b_delta))
            gainfactor = 4096.0 * (a_delta / _id_at_a) / (b_delta / _id_at_b)
            gainfactor = round(gainfactor)
            self.node.sdo['Beta']['Gainfactor'].raw = gainfactor
            print("New Beta Gainfactor = {0}  (a_sens={1:.5f}  b_sens={2:.5f}  counts/mA)".format(
                gainfactor, a_delta / _id_at_a, b_delta / _id_at_b))

            # Check Bounds for error!! Can increase to 10% if needed
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
                    return False

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
        # ALGORITHM OVERVIEW — RIPPLE-MINIMISING MaxSettlingTime CALIBRATION
        # MaxSettlingTime (0x3001:5) is applied live by firmware on write while idle
        # (parsePwmTiming -> pwm_update_pwm_timing, stm32 app/pwm.c), so each settling step just
        # drops to IDLE, writes the value, then re-energises to sample — no per-step save or NMT
        # reset (fw >= 4.4.0).
        #
        # THE CRITERION.  We pick the settling that MINIMISES the current-measurement RIPPLE within
        # the ACCURATE range.  We reuse measure_gain_ripple's primitive: drive a fixed voltage vector
        # around a full electrical cycle with the rotor STOPPED at each angle (_wait_settled polls the
        # raw encoder until stable), average Motor.id / CurrentFeedback per angle, form
        # |I|=sqrt(id²+iq²), and pull the 2×-electrical amplitude out by DFT.  A gain/timing error in
        # the α/β sense makes |I| ripple at 2×-electrical (an ellipse instead of a circle) — that 2×
        # amplitude % IS the roughness we are minimising.  A single fixed ud ⇒ constant ACTUAL current
        # across every settling, so mean|I| tracks the ACCURACY (how much of the real current the ADC
        # captures) and the 2× amplitude tracks the ROUGHNESS.
        #
        #   1. Establish drive once: voltage-angle mode, seed+settle rotor at θ=0, ramp ud to ~i_cal.
        #   2. Sweep MaxSettlingTime over ~7 values (0..just-below half_period_ns); set live in IDLE,
        #      re-enter voltage-angle mode at the SAME ud each time (fixed ud ⇒ constant current).
        #   3. At each settling, one ~12-angle rotation → mean|I| (accuracy) and 2×-amp % (ripple).
        #   4. Accurate PLATEAU = settlings with mean|I| >= 0.90 × max(mean|I|).  This rejects the
        #      high-settling UNDER-READ zone (late sample reads past the short conduction window —
        #      that is what makes ~1500 ns bad) so the pick can never land there.
        #   5. min_ripple = the smallest 2×-amp among plateau settlings.
        #   6. RESULT = the LOWEST plateau settling whose 2×-amp <= min_ripple × 1.15 (the earliest
        #      settling not paying a meaningful ripple penalty).
        #        * strong-ripple part (P4-16): low settlings ripple clearly worse → excluded → the
        #          result climbs to the ripple minimum (~800 ns).
        #        * flat-ripple part (P4-37 / STM32): nobody pays a penalty → result = the LOWEST
        #          accurate settling (earliest).  THIS IS INTENDED — the motor-agnostic behaviour.
        if calAll == False:
            if self.check_for_node() == False:
                return False
            if not self._fw_at_least(4, 4, 0):
                self._prompt_ok("Firmware Too Old",
                    "ADC settling-time calibration requires firmware v4.4.0 or later.\n"
                    "Please update the firmware and try again.")
                return False
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
        wx.Yield()

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
            # Calibrate at i_cal — the settling edge is current-dependent so don't under-drive, but
            # driving high is dangerous (a weak supply browns out into 0x3220 undervoltage).
            calibration_current = max(1, int(i_cal))

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
                try:
                    return self.node.sdo['Amplifier']['Temperature'].raw
                except Exception:
                    return None

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
                    time.sleep(0.06); wx.Yield()
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
            print("Establishing drive at {} ns (target |I| {} mA, bus min {})...".format(
                original_settling, calibration_current, _bus_min))
            self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE  # idle: settling write applies live
            self.node.sdo['Amp']['MaxSettlingTime'].raw = original_settling
            self.node.sdo["ControlWord"].raw = CLEAR_FAULT
            self.node.sdo["ControlWord"].raw = SHUTDOWN
            self.node.sdo["ControlWord"].raw = OP_ENABLED
            self.node.sdo["SetModeOfOperation"].raw = MODE_PHASE_VOLTAGE_ANGLE
            self.node.sdo['Theta_e'].raw = 0
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
                wx.Yield()
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

            # --- Rotation measurement primitive (reuses measure_gain_ripple's approach) ---
            _N_ANGLES = 12   # ~12 angles keeps the whole sweep to ~2-3 min
            _N_AVG    = 12    # samples averaged per angle (rotor stopped)

            def _measure_ripple():
                """At the CURRENT settling+ud, drive a full electrical cycle with the rotor stopped
                at each of _N_ANGLES angles; return (mean|I| mA, 2×-electrical amplitude % of mean).
                mean|I| = accuracy (fixed ud ⇒ constant true current, so this reads how much the ADC
                captures); 2×-amp % = ripple = the roughness metric we minimise."""
                _theta = [int(round(-32768 + i * 65536.0 / _N_ANGLES)) for i in range(_N_ANGLES)]
                _mg = []
                for _k, _th in enumerate(_theta):
                    self.frame_statusbar.SetStatusText(
                        "timing ripple {}/{}".format(_k + 1, _N_ANGLES), 1)
                    self.node.sdo['Theta_e'].raw = _th
                    _wait_settled()
                    _sid = _siq = 0.0
                    for _ in range(_N_AVG):
                        _sid += self.node.sdo['Motor']['id'].raw
                        _siq += self.node.sdo['CurrentFeedback'].raw
                        time.sleep(0.003); wx.Yield()
                    _idm = (_sid / _N_AVG) / 1000.0 * i_peak
                    _iqm = (_siq / _N_AVG) / 1000.0 * i_peak
                    _mg.append((_idm * _idm + _iqm * _iqm) ** 0.5)
                _r = [_theta[i] / 32768.0 * math.pi for i in range(_N_ANGLES)]
                _mnv = (sum(_mg) / _N_ANGLES) or 1.0
                _c2 = sum(_mg[i] * math.cos(2.0 * _r[i]) for i in range(_N_ANGLES))
                _s2 = sum(_mg[i] * math.sin(2.0 * _r[i]) for i in range(_N_ANGLES))
                _a2 = 2.0 * math.sqrt(_c2 * _c2 + _s2 * _s2) / _N_ANGLES / _mnv * 100.0
                return _mnv, _a2

            # --- Settling sweep values: 0..just-below half_period_ns ---
            # Clip to < half_period so a step can never land in the no-current zone (at the full
            # half-period there's no room left for ADC sample+convert and the firmware stops
            # generating current entirely).
            _SETTLE_STEP = 100  # ns per step (50 for a finer curve; the knee pick is robust to spacing)
            _SETTLE_MAX  = min(1500, half_period_ns)   # useful range; above this it's all under-read
            settle_values = list(range(0, _SETTLE_MAX, _SETTLE_STEP))
            if not settle_values:
                settle_values = [0]
            print("Settling sweep: {} ns  (kept < half-period {} ns)".format(
                settle_values, half_period_ns))
            print("Est. time: ~{:.0f} s  ({} settlings × {} angles)".format(
                len(settle_values) * _N_ANGLES * (0.25 + _N_AVG * 0.003) + 5.0,
                len(settle_values), _N_ANGLES))

            temp_start  = _read_amp_temp()
            mtemp_start = _read_motor_temp()
            print("Sweep start: puck={}  motor={}".format(
                _fmt_temp(temp_start), _fmt_temp(mtemp_start)))
            _check_overheat(temp_start, mtemp_start, "start")  # don't even begin if already hot

            results = []   # list of dicts: settling, meanI, amp2x, plateau
            for s_idx, t in enumerate(settle_values):
                self.frame_statusbar.SetStatusText(
                    "Timing cal — {}/{} ({} ns)".format(s_idx + 1, len(settle_values), t), 1)
                self.frame_statusbar.Update(); wx.Yield()
                # Set the settling live while IDLE, then re-enter voltage-angle mode at the SAME ud.
                self.node.sdo['Motor']['ud'].raw = 0
                self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE  # idle: write applies live
                self.node.sdo['Amp']['MaxSettlingTime'].raw = t
                _readback = self.node.sdo['Amp']['MaxSettlingTime'].raw
                self.node.sdo["ControlWord"].raw = CLEAR_FAULT
                self.node.sdo["ControlWord"].raw = SHUTDOWN
                self.node.sdo["ControlWord"].raw = OP_ENABLED
                self.node.sdo["SetModeOfOperation"].raw = MODE_PHASE_VOLTAGE_ANGLE
                self.node.sdo['Theta_e'].raw = 0
                self.node.sdo['Motor']['ud'].raw = drive_ud
                _wait_settled()
                if _check_fault("sweep step {}/{} ({} ns)".format(s_idx + 1, len(settle_values), t)):
                    _restore_idle()
                    raise RuntimeError("Itiming cal ABORTED: puck faulted / comms lost during "
                                       "sweep (see fault line above).")
                _mnv, _a2 = _measure_ripple()
                results.append({'settling': t, 'meanI': _mnv, 'amp2x': _a2, 'plateau': False})
                _t_now = _read_amp_temp(); _m_now = _read_motor_temp()
                print("  step {}/{}: {:5d} ns (rb {:5d})  mean|I|={:7.1f} mA  2×={:6.2f}%  "
                      "puck={} motor={}".format(
                          s_idx + 1, len(settle_values), t, _readback, _mnv, _a2,
                          _fmt_temp(_t_now), _fmt_temp(_m_now)))
                _check_overheat(_t_now, _m_now,
                                "step {}/{}".format(s_idx + 1, len(settle_values)))
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

            # --- Analysis: accurate plateau → minimum ripple → lowest settling with no penalty ---
            _PLATEAU_FRAC = 0.85   # accurate = mean|I| within this fraction of the sweep peak; a mild
                                   # under-read at higher settling is fine (the gain cal compensates it)
            _DROP_FRAC    = 0.70   # pick the KNEE: lowest settling where the ripple has dropped this
                                   # fraction of the way from its peak to its floor. Felt smoothness
                                   # saturates at the knee; chasing the true min just over-reads (edge
                                   # of the plateau) with no felt gain.
            _MEANI_GATE   = max(20.0, 0.3 * calibration_current)  # need real current to resolve

            # Smooth the 2× curve (3-point moving average) so the pick fits the trend, not point noise.
            _a2raw = [r['amp2x'] for r in results]
            for i, r in enumerate(results):
                _lo, _hi = max(0, i - 1), min(len(_a2raw), i + 2)
                r['amp2x_s'] = sum(_a2raw[_lo:_hi]) / (_hi - _lo)

            max_meanI = max(r['meanI'] for r in results)
            for r in results:
                r['plateau'] = r['meanI'] >= _PLATEAU_FRAC * max_meanI
            plateau = [r for r in results if r['plateau']]

            print("Settling sweep results:")
            print("  {:>8}  {:>9}  {:>7}  {:>7}  {:>6}".format("settle", "mean|I|", "2×%", "2×_s%", "plat"))
            for r in results:
                print("  {:8d}  {:9.1f}  {:7.2f}  {:7.2f}  {:>6}".format(
                    r['settling'], r['meanI'], r['amp2x'], r['amp2x_s'], "yes" if r['plateau'] else "no"))

            if max_meanI < _MEANI_GATE or not plateau:
                # Never got real current on the ADC — nothing trustworthy to minimise.
                optimal_settling = original_settling
                _resolvable = False
                _result = None
                _reason = ("NOT resolvable: peak mean|I|={:.1f} mA below gate {:.1f} mA — no "
                           "usable current. Left MaxSettlingTime unchanged at {} ns.".format(
                               max_meanI, _MEANI_GATE, original_settling))
                print(_reason)
            else:
                _rmax = max(r['amp2x_s'] for r in plateau)   # on the SMOOTHED curve
                _rmin = min(r['amp2x_s'] for r in plateau)
                _knee = _rmax - _DROP_FRAC * (_rmax - _rmin)  # ripple dropped _DROP_FRAC of the way
                # LOWEST plateau settling at/below the knee -- stops where smoothness saturates.
                pick = min((r for r in plateau if r['amp2x_s'] <= _knee),
                           key=lambda r: r['settling'])
                optimal_settling = int(pick['settling'])
                _result = float(optimal_settling)
                _resolvable = True
                _lowest_plateau = min(r['settling'] for r in plateau)
                if optimal_settling == _lowest_plateau:
                    _reason = ("lowest accurate settling ({} ns) is already at/below the ripple knee "
                               "(2×_s={:.2f}%) — flat-ripple / motor-agnostic earliest pick.".format(
                                   optimal_settling, pick['amp2x_s']))
                else:
                    _reason = ("ripple knee: dropped {:.0f}% from peak {:.2f}% toward floor {:.2f}%; "
                               "lowest settling at/below the knee ({:.2f}%) → {} ns (2×_s={:.2f}%).".format(
                                   _DROP_FRAC * 100.0, _rmax, _rmin, _knee, optimal_settling,
                                   pick['amp2x_s']))
                print("Optimal MaxSettlingTime: {} ns  (was {} ns)".format(
                    optimal_settling, original_settling))
                print("  reason: {}".format(_reason))

            # --- Debug plot: 2×-ripple (and mean|I|) vs settling, marking the pick ---
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

                _xs   = [r['settling'] for r in results]
                _rip  = [r['amp2x']    for r in results]
                _mean = [r['meanI']    for r in results]

                fig, ax = plt.subplots(figsize=(10, 6))
                axr = ax.twinx()
                ax.plot(_xs, _rip, '-o', color='tomato', markersize=6,
                        linewidth=1.8, label='2×-elec ripple %')
                axr.plot(_xs, _mean, '--s', color='steelblue', markersize=5,
                         linewidth=1.0, alpha=0.7, label='mean|I| (mA)')
                # shade the accurate plateau and the 15% ripple band
                for r in results:
                    if r['plateau']:
                        ax.axvspan(r['settling'] - 20, r['settling'] + 20,
                                   color='green', alpha=0.06)
                if _resolvable:
                    _minr = min(r['amp2x'] for r in results if r['plateau'])
                    ax.axhline(_minr, color='gray', linestyle=':', linewidth=0.9,
                               label='plateau min 2× ({:.2f}%)'.format(_minr))
                    ax.axhline(_knee, color='gray', linestyle='--',
                               linewidth=0.7, alpha=0.6,
                               label='ripple knee ({:.2f}%)'.format(_knee))
                    ax.axvline(optimal_settling, color='black', linewidth=2.0,
                               label='PICK {} ns'.format(optimal_settling))
                    _result_note = '{} ns'.format(optimal_settling)
                else:
                    _result_note = 'NOT RESOLVABLE (kept {} ns)'.format(original_settling)

                ax.set_title('MaxSettlingTime cal — minimise 2×-electrical ripple in accurate range\n'
                             '{}   (drive {} mA, ud {})   |   RESULT: {}'.format(
                                 _node_label, calibration_current, drive_ud, _result_note),
                             fontsize=11)
                ax.set_xlabel('MaxSettlingTime (ns)')
                ax.set_ylabel('2×-electrical ripple (% of mean|I|)', color='tomato')
                axr.set_ylabel('mean|I| (mA)', color='steelblue')
                ax.grid(True, alpha=0.25)
                _l1, _b1 = ax.get_legend_handles_labels()
                _l2, _b2 = axr.get_legend_handles_labels()
                ax.legend(_l1 + _l2, _b1 + _b2, fontsize=8, loc='upper right')

                plt.tight_layout()
                plot_path = os.path.abspath('itiming_ripple_cal_{}.png'.format(
                    time.strftime('%Y-%m-%d_%H-%M-%S')))
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
                    time.sleep(0.06); wx.Yield()
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
                wx.Yield()
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
                        time.sleep(0.003); wx.Yield()
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
                    time.sleep(0.04); wx.Yield()
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

    def calibrate_current_slope(self, event, calAll=False):  # wxGlade: puckutilityapp_frame.<event_handler>
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
            alpha_gf = self.node.sdo['Alpha']['Gainfactor'].raw
            beta_gf  = self.node.sdo['Beta']['Gainfactor'].raw
            print("--- Current Sense Slope calibration (current-proportional alpha/beta offset) ---")
            print("Gainfactors in effect: Alpha={}  Beta={}  (i_cal {} mA)".format(
                alpha_gf, beta_gf, i_cal))

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

            def _imag():
                _id = self.node.sdo['Motor']['id'].raw / 1000.0 * i_peak
                _iq = self.node.sdo['CurrentFeedback'].raw / 1000.0 * i_peak
                return (_id * _id + _iq * _iq) ** 0.5

            def _wait_settled(timeout=1.5):
                # Wait for the rotor to actually STOP after a theta_e step so back-EMF doesn't
                # modulate |I| and swamp the tiny alpha/beta offset (see measure_gain_ripple).
                _p0 = self.node.sdo['Encoder']['RawPosition'].raw
                _t0 = time.time(); _stable = 0
                while time.time() - _t0 < timeout:
                    time.sleep(0.06); wx.Yield()
                    _p1 = self.node.sdo['Encoder']['RawPosition'].raw
                    if abs(_p1 - _p0) <= 2:
                        _stable += 1
                        if _stable >= 3:
                            return
                    else:
                        _stable = 0
                    _p0 = _p1

            # Energise in voltage-angle mode; align+stop the rotor at theta=0 before ramping.
            self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE
            self.node.sdo["ControlWord"].raw = CLEAR_FAULT
            self.node.sdo["ControlWord"].raw = SHUTDOWN
            self.node.sdo["ControlWord"].raw = OP_ENABLED
            self.node.sdo["SetModeOfOperation"].raw = MODE_PHASE_VOLTAGE_ANGLE
            self.node.sdo['Theta_e'].raw = 0
            self.node.sdo['Motor']['ud'].raw = 2000   # seed to pull the rotor to theta=0
            _wait_settled()

            _N, _M = 24, 20

            def _sweep_offset():
                # One full-electrical-cycle sweep at the current ud; rotor stopped each step.
                # Returns (mean|I|, off_alpha, off_beta) in mA -- the current-vector offset, i.e. the
                # displacement of the |I| circle's centre, via an inverse-Park mean (same convention
                # as measure_gain_ripple: the true rotating current averages to zero so the residual
                # inverse-Park mean IS the fixed sense offset).
                _theta = [int(round(-32768 + i * 65536.0 / _N)) for i in range(_N)]
                _mg, _idl, _iql = [], [], []
                for _k, _th in enumerate(_theta):
                    self.frame_statusbar.SetStatusText("slope offset - {}/{}".format(_k + 1, _N), 1)
                    self.node.sdo['Theta_e'].raw = _th
                    _wait_settled()
                    _sid = _siq = 0.0
                    for _ in range(_M):
                        _sid += self.node.sdo['Motor']['id'].raw
                        _siq += self.node.sdo['CurrentFeedback'].raw
                        time.sleep(0.003); wx.Yield()
                    _idm = (_sid / _M) / 1000.0 * i_peak
                    _iqm = (_siq / _M) / 1000.0 * i_peak
                    _idl.append(_idm); _iql.append(_iqm)
                    _mg.append((_idm * _idm + _iqm * _iqm) ** 0.5)
                _r = [_theta[i] / 32768.0 * math.pi for i in range(_N)]
                _mnv = (sum(_mg) / _N) or 1.0
                _oa = sum(_idl[i] * math.cos(_r[i]) - _iql[i] * math.sin(_r[i]) for i in range(_N)) / _N
                _ob = sum(_idl[i] * math.sin(_r[i]) + _iql[i] * math.cos(_r[i]) for i in range(_N)) / _N
                return _mnv, _oa, _ob

            # OFFSET vs CURRENT: sweep the same load levels as the ripple diagnostic.
            _levels = sorted(set(max(20, int(i_cal * _f)) for _f in (0.3, 0.55, 0.8, 1.0, 1.3)))
            _rows = []   # (meanI, off_alpha, off_beta) per level
            for _lvl in _levels:
                self.node.sdo['Theta_e'].raw = 0
                _ud = 500
                self.node.sdo['Motor']['ud'].raw = _ud
                _wait_settled()
                _cur = _imag() or 0.0
                while _cur < _lvl and _ud < 16000:
                    _ud += 150
                    self.node.sdo['Motor']['ud'].raw = _ud
                    time.sleep(0.04); wx.Yield()
                    _cur = _imag() or 0.0
                _wait_settled()
                _mnv, _oa, _ob = _sweep_offset()
                _rows.append((_mnv, _oa, _ob))

            self.node.sdo['Motor']['ud'].raw = 0
            self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE

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
            print(">>> slope coefficients:  kA = {:+.4f}   kB = {:+.4f}".format(kA, kB))
            print(">>>   stored ints (Q4.12, signed):  kA*4096 = {:+d}   kB*4096 = {:+d}".format(
                int(round(kA * 4096)), int(round(kB * 4096))))
            print(">>> slope_mag = {:.4f} (mA/mA)   direction = {:.1f} deg".format(slope_mag, direction_deg))
            print(">>> intercepts:  a0 = {:+.2f} mA   b0 = {:+.2f} mA".format(a0, b0))
            print(">>> R^2:  alpha = {:.4f}   beta = {:.4f}   angle-spread = {:.1f} deg".format(
                r2A, r2B, angle_spread))
            if angle_spread > 20.0:
                print(">>> WARNING: offset direction not consistent ({:.1f} deg spread) -- not a clean "
                      "fixed-direction slope.".format(angle_spread))
            if abs(a0) > 5.0 or abs(b0) > 5.0:
                print(">>> WARNING: large intercept (a0={:+.1f}, b0={:+.1f} mA) -- iSense bias looks "
                      "stale; re-run Bias/Gain first.".format(a0, b0))

            # --- FIRST-CUT SLOPE vs RESIDUAL BASELINE (what the stored fix does / does NOT remove) ---
            _base_mag = (a0 * a0 + b0 * b0) ** 0.5
            _base_dir = math.degrees(math.atan2(b0, a0))
            _off_op   = (((a0 + kA * i_cal) ** 2) + ((b0 + kB * i_cal) ** 2)) ** 0.5
            print("=" * 64)
            print(">>> CORRECTION SUMMARY  (operating |I| = i_cal = {} mA)".format(i_cal))
            print(">>>   SLOPE     (firmware subtracts kA*|I|, kB*|I|):  {:.3f} mA/mA @ {:+.0f} deg"
                  .format(slope_mag, direction_deg))
            print(">>>   BASELINE  (residual, NOT removed by the slope):  {:.1f} mA @ {:+.0f} deg"
                  .format(_base_mag, _base_dir))
            print(">>>   NET at |I|={} mA:  {:.1f} mA offset  -->  ~{:.1f} mA left after slope correction"
                  .format(i_cal, _off_op, _base_mag))
            if _base_mag > 8.0:
                print(">>>   (baseline > 8 mA: this slope term is a PARTIAL fix -- it leaves a residual")
                print(">>>    fixed offset that a separate baseline-offset correction could remove)")
            print("=" * 64)

            # --- SANITY GATE: never store garbage. A reset/bad gainfactor (e.g. after a firmware
            #     flash) makes that channel read ~0, collapsing the |I| circle so the measured
            #     "offset" equals the current -> kA~=1.0 with a perfect R^2. Storing that cripples
            #     the FOC (subtracts ~100% of |I|). Refuse it. ---
            _gain_ok  = (1024 <= alpha_gf <= 16384 and 1024 <= beta_gf <= 16384)  # ~4096 = 1.0 (Q4.12)
            _slope_ok = (slope_mag <= 0.5)   # an iSense offset can't be >50% of the current
            if not (_gain_ok and _slope_ok):
                print(">>> NOT STORING -- measurement invalid:")
                if not _gain_ok:
                    print(">>>   gainfactor out of range (Alpha={}, Beta={}; ~4096 expected). Run "
                          "'Current Sense Gainfactor' / a full calibration FIRST.".format(alpha_gf, beta_gf))
                if not _slope_ok:
                    print(">>>   slope_mag={:.3f} absurd (offset ~= current -> a channel reads ~0, "
                          "almost always a bad gain).".format(slope_mag))
            else:
                # --- STORE (guarded: OD entries may not exist in firmware yet) ---
                try:
                    self.node.sdo[0x3008][7].raw = int(round(kA * 4096))   # Q4.12, signed (I16)
                    self.node.sdo[0x3009][7].raw = int(round(kB * 4096))
                    self.node.sdo['Save']['Single'].raw = ((0x3008 << 8) | 0x07)   # persist Alpha slope
                    self.node.sdo['Save']['Single'].raw = ((0x3009 << 8) | 0x07)   # persist Beta slope
                    print(">>> stored to 0x3008:7 / 0x3009:7 (and saved to EEPROM).")
                except Exception as _se:
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
                from paths import session_path
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

    def calibrate_islope(self, event):  # wxGlade: wxp3_frame.<event_handler>
        print("Event handler 'calibrate_islope' not implemented!")
        event.Skip()

    def calibrate_enczero(self, event, calAll=False, _upd=None):  # wxGlade: wxp3_frame.<event_handler>
        # print("Event handler 'calibrate_enczero'")
        if calAll==False:
          if self.check_for_node() == False: #len(self.network.scanner.nodes) == 0:
            return False
          self.Disable()
        quick_test = self.choice_test.GetSelection()
        if quick_test != 0:
            self.lastMode = 0 # Reset lastMode
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

        self.frame_statusbar.SetStatusText("Calibrating encoder...", 1)
        self.frame_statusbar.Update()
        wx.Yield()

        try:
          # Clear faults, RTSO, OpEnabled
          print("Going OpEnabled")
          self.node.sdo["ControlWord"].raw = CLEAR_FAULT
          self.node.sdo["ControlWord"].raw = SHUTDOWN
          self.node.sdo["ControlWord"].raw = OP_ENABLED
          
          # Set Mode to PhaseVoltageAngle (12)
          print("Setting Mode = VOLTAGE")
          self.node.sdo["SetModeOfOperation"].raw = MODE_PHASE_VOLTAGE_ANGLE
  
          # Write theta_e, ud, StatsMode, vel
          # theta_e is 16-bit signed from -pi to +pi
          self.node.sdo['Theta_e'].raw = -0x1000 # -pi/8 (-22.5°)
  
          # Read this motor's calibration current (mA)
          calibration_current = self.node.sdo['Calibration']['i_cal'].raw
  
          # Read the motor.peak (mA)
          i_peak = self.node.sdo['Calibration']['i_peak'].raw
  
          # If calibration current is greater than i_peak, limit
          if calibration_current > i_peak:
             calibration_current = i_peak
  
          # Increase Motor d-axis voltage until measured d-axis current > calibration_current mA or ud > 32000
          motor_ud = 0
          motor_id = self.node.sdo['Motor']['id'].raw
          while (motor_id < 1000 and self.node.sdo['Motor']['id'].raw / 1000.0 * i_peak) < calibration_current and motor_ud < 32000:
            print("id = {0}, ud = {1}".format(
              self.node.sdo['Motor']['id'].raw / 1000.0 * i_peak, 
              self.node.sdo['Motor']['ud'].raw))
            _id_now = self.node.sdo['Motor']['id'].raw / 1000.0 * i_peak
            _upd(int(min(1.0, max(0.0, _id_now / calibration_current)) * 38))  # 0→38%
            if motor_ud > 0 and _id_now > 0:
                _ramp_step = max(100, int((motor_ud * calibration_current / _id_now - motor_ud) / 4))
            else:
                _ramp_step = max(100, 32000 // 12)
            motor_ud = min(motor_ud + _ramp_step, 32000)
            self.node.sdo['Motor']['ud'].raw = motor_ud
            time.sleep(0.05)
            wx.Yield() # keep wx event loop alive so Windows doesn't mark the app "Not Responding"
  
          # Drive from theta_e = -90 to 0 in 32 steps of 0.05s
          # Capture RawPosition when commanding theta_e = 0
          # Also determine e_polarity by watching the raw encoder direction
          pos0 = self.node.sdo['Encoder']['RawPosition'].raw
          startPos1 = self.node.sdo['PositionFeedback'].raw
          _approach1_steps = list(range(int(-0x1000), 1, int(0x1000/32)))
          for _si, i in enumerate(_approach1_steps):
            _upd(38 + _si * 22 // len(_approach1_steps))  # 38→60%
            self.node.sdo['Theta_e'].raw = i
            time.sleep(0.05)
            wx.Yield() # keep wx event loop alive so Windows doesn't mark the app "Not Responding"
          _sleep_responsive(0.25)
          pos1 = self.node.sdo['Encoder']['RawPosition'].raw
          print("After approaching theta_e = 0 from -22.5°, Encoder raw = {0}".format(pos1))

          zeroPos1 = self.node.sdo['PositionFeedback'].raw

          # Drive from theta_e = +90 to 0 in 32 steps of 0.05s
          # Capture RawPosition when commanding theta_e = 0
          self.node.sdo['Theta_e'].raw = 0x1000
          _sleep_responsive(1)
          _upd(65)
          startPos2 = self.node.sdo['PositionFeedback'].raw
          _approach2_steps = list(range(int(0x1000), -1, int(-0x1000/32)))
          for _si, i in enumerate(_approach2_steps):
            _upd(65 + _si * 23 // len(_approach2_steps))  # 65→88%
            self.node.sdo['Theta_e'].raw = i
            time.sleep(0.05)
            wx.Yield() # keep wx event loop alive so Windows doesn't mark the app "Not Responding"
          _sleep_responsive(0.25)
          pos2 = self.node.sdo['Encoder']['RawPosition'].raw
          print("After approaching theta_e = 0 from +22.5°, Encoder raw = {0}".format(pos2))
          zeroPos2 = self.node.sdo['PositionFeedback'].raw
  
          # Take the average of the two measurements, store e_zero
          encoder_resolution = self.node.sdo['EncoderConfig']['Resolution'].raw
          motor_poles = self.node.sdo['Calibration']['poles'].raw
          cts_per_elec_cyc = encoder_resolution * 2 / motor_poles
          print("Encoder resolution = {}  Motor poles (EEPROM) = {}  cts/elec_cyc = {:.1f}".format(
              encoder_resolution, motor_poles, cts_per_elec_cyc))
          if abs(pos1-pos2) >  encoder_resolution / 2:
            if pos1 > pos2:
              pos1 += encoder_resolution
            else:
              pos2 += encoder_resolution
          friction_spread = abs(pos1 - pos2)
          friction_pct = friction_spread / cts_per_elec_cyc * 100.0
          print("Approach spread: {} counts ({:.1f}% of electrical cycle) — friction hysteresis".format(
              friction_spread, friction_pct))
          if friction_pct > 10.0:
              print("  WARNING: large friction spread may bias e_zero — check motor load/friction")
          pos = (pos1 + pos2) / 2
          pos = pos % cts_per_elec_cyc
          pos = int(pos)
  
          # Calculate e_polarity
          if abs(pos1-pos0) < (cts_per_elec_cyc / 2): 
            # If there was no rollover during the initial -90..0 movement
            self.node.sdo['Calibration']['e_polarity'].raw = math.copysign(1, pos1-pos0)
          else: 
            # We rolled over
            self.node.sdo['Calibration']['e_polarity'].raw = -math.copysign(1, pos1-pos0)
          self.node.sdo['Save']['Single'].raw = ((0x3011 << 8) | 0x02) # Save e_polarity to EE
          print("Electrical polarity = {0}".format(self.node.sdo['Calibration']['e_polarity'].raw))
  
          previous_polarity = self.node.sdo['Calibration']['e_polarity'].raw
          previous_zero     = self.node.sdo['Calibration']['e_zero'].raw

          print("Previous electrical polarity = {0}".format(previous_polarity))
          print("Previous electrical zero = {0}".format(previous_zero))
          self.node.sdo['Calibration']['e_zero'].raw = pos
          self.node.sdo['Save']['Single'].raw = ((0x3011 << 8) | 0x01) # Save e_zero to EE
          print("New electrical zero = {0}".format(pos))
  
          pos_change1 = round(abs(startPos1 - zeroPos1) * (360/4096) * motor_poles)
          pos_change2 = round(abs(startPos2 - zeroPos2) * (360/4096) * motor_poles)
  
          # Check Bounds for error!!
          error = .25 # 25%
          expected_change = 22.5
  
          self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE

          if pos_change1 < round(expected_change * (1 - error)) or pos_change2 < round(expected_change * (1 - error)):
            print('Encoder Zero Failed!')
            # Bad Encoder reading (error dialog! debug steps)
            # Can grab kt and cal current to determine required torque
            cal_torque = calibration_current * self.node.sdo['Calibration']['kt'].raw / 1000
            msg = "Encoder Zero Failed! \n\nFirst Jump: {}°" \
            "\nSecond Jump: {}°" \
            "\nExpected Jump: >= {}°" \
            "\n\nDebugging steps:" \
            "\n- Ensure proper configuration file has been loaded" \
            "\n- Verify output friction is less than cal torque for the motor ({}mNm)" \
            "\n\nWould you like to continue calibration?"  .format(pos_change1,pos_change2,round(22.5*(1-error)),cal_torque)
            # Route through _prompt so the headless CLI adapter can answer via
            # stdin instead of popping a wx dialog (which would crash/block a
            # headless run). Returns True to continue, False to abort.
            continue_cal = self._prompt('Warning!', msg)

            _upd(100)
            if self.ADC_ON == False and self.adcWasON == True:
                self.on_off_adc(self)
            if calAll == False:
                self.OnTaskComplete()
                self.Enable()
            return continue_cal
          else:
            # SUCCESS path. The ADC-monitor restore + task cleanup above lived
            # only in the failure branch, so a successful calibration (and thus a
            # successful calibrate_all, where enczero runs last) left the ADC
            # monitor turned off. Restore it here too.
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

    def calibrate_encdir(self, event):  # wxGlade: wxp3_frame.<event_handler>
        print("Event handler 'calibrate_encdir' not implemented!")
        event.Skip()

    def calibrate_enclag(self, event,calAll=False):  # wxGlade: wxp3_frame.<event_handler>
        # print("Event handler 'calibrate_enclag'")

        if self.ADC_ON == True:
            self.adcWasON = True
            self.on_off_adc(self)
        else:
            self.adcWasON = False

        if calAll==False:
          if self.check_for_node() == False: #len(self.network.scanner.nodes) == 0:
            return False
          self.Disable()

        self.frame_statusbar.SetStatusText("Calibrating Encoder Lag...", 1)
        self.frame_statusbar.Update()
        wx.Yield()

        # Set Mode to Idle (0)
        print("Setting Mode = IDLE")
        self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE
        _sleep_responsive(1) # Wait at least 75 ms for the filters to settle

        # Clear faults, RTSO, OpEnabled
        print("Going OpEnabled")
        self.node.sdo["ControlWord"].raw = CLEAR_FAULT
        self.node.sdo["ControlWord"].raw = SHUTDOWN
        self.node.sdo["ControlWord"].raw = OP_ENABLED
      
        # Set Mode to Torque (4)
        print("Setting Mode = TORQUE")
        self.node.sdo["SetModeOfOperation"].raw = MODE_PROFILE_TRQ
        self.node.sdo['EncoderConfig']['LagFactor'].raw = 0

        # Increase TargetTorque until iq.fbk = 1000 mA
        cmd_value = 0
        self.node.sdo["TargetTorque"].raw = cmd_value # Send
        # q_fbk = 0
        while True:
         self.node.sdo["TargetTorque"].raw = cmd_value # Send
         time.sleep(0.05)
         wx.Yield() # keep wx event loop alive so Windows doesn't mark the app "Not Responding"
         q_fbk = self.node.sdo['CurrentFeedback'].raw
         print("TargetTorque = {0}, CurrentFeedback = {1} mA".format(cmd_value, q_fbk))
         if q_fbk > 1000 or cmd_value == 1000:
           break
         cmd_value += 50
          
        # ================= OLD max-velocity lag sweep (DISABLED) =================
        # Kept commented for reference/revert. It maximized VELOCITY, which at
        # no-load is the field-weakening operating point: velocity rises
        # monotonically with lag (no true peak), so the sweep ran past the correct
        # lag into field weakening, lost commutation sync, and browned out the bus
        # (0x3220). Replaced by the d-axis-current-null sweep below. To revert:
        # uncomment this block and delete the id-null block that follows.
        # -------------------------------------------------------------------------
        # # Set the number of lag increments to attempt without setting a new max_vel
        # max_cycles = 50
#
        # # Init: cycles = 0, max = 0, lag = 0
        # cycles = 0
        # max_vel = 0
        # lag = 0
#
        # while True:
          # # Read vel.fbk
          # vel = abs(self.node.sdo["VelocityFeedback"].raw)
          # # If |vel.fbk| > max, update max, remember lag, reset cycles to zero
          # if vel > max_vel:
            # max_vel = vel
            # saved_lag_1 = lag
            # cycles = 0
          # else: # Else, ++cycles
            # cycles += 1
#
          # print("Lag: {0}, Vel: {1}, MaxVel: {2}, Cycles: {3}".format(lag, vel, max_vel, cycles))
          # # Increase EncoderLag until cycles == max_cycles
          # lag += 1
          # self.node.sdo['EncoderConfig']['LagFactor'].raw = lag
          # if cycles > max_cycles:
            # break
          # time.sleep(0.05)
          # wx.Yield() # keep wx event loop alive so Windows doesn't mark the app "Not Responding"
#
        # # Invert TargetTorque
        # self.node.sdo["TargetTorque"].raw = -cmd_value # Send
        # self.node.sdo['EncoderConfig']['LagFactor'].raw = 0
#
        # _sleep_responsive(0.5)
#
        # # Init: cycles = 0, max = 0, lag = 0
        # cycles = 0
        # max_vel = 0
        # lag = 0
#
        # while True:
          # # Read vel.fbk
          # vel = abs(self.node.sdo["VelocityFeedback"].raw)
          # # If |vel.fbk| > max, update max, remember lag, reset cycles to zero
          # if vel > max_vel:
            # max_vel = vel
            # saved_lag_2 = lag
            # cycles = 0
          # else: # Else, ++cycles
            # cycles += 1
#
          # print("Lag: {0}, Vel: {1}, MaxVel: {2}, Cycles: {3}".format(lag, vel, max_vel, cycles))
          # # Increase EncoderLag until cycles == max_cycles
          # lag += 1
          # self.node.sdo['EncoderConfig']['LagFactor'].raw = lag
          # if cycles > max_cycles:
            # break
          # time.sleep(0.05)
          # wx.Yield() # keep wx event loop alive so Windows doesn't mark the app "Not Responding"
#
        # # Take the average of the two lags
        # lag = (saved_lag_1 + saved_lag_2) / 2
        # print("Lag_1: {0}, Lag_2: {1}, Setting LagFactor: {2}".format(saved_lag_1, saved_lag_2, lag))
#
        # # Store the LagFactor
        # self.node.sdo['EncoderConfig']['LagFactor'].raw = lag
        # self.node.sdo['Save']['Single'].raw = ((0x3013 << 8) | 0x05) # Save lag to EE
#
        # =========================================================================

        # ================= NEW: d-axis-current-null lag calibration ==============
        # Correct target: null Motor.id (0x3010:6, signed). Perfect commutation
        # advance -> the intended q-voltage lands on q -> id ~= 0. Under-advanced
        # leaves residual id of one sign; over-advanced drives id NEGATIVE (that IS
        # field weakening). So id crosses zero at the correct lag -- a real zero to
        # converge on, and we STOP just past it so we never march into the
        # field-weakening / runaway region that crashed the old sweep.
        #
        # Safety: hard lag cap, and abort (restore LagFactor=0, no save) on drive
        # fault, bus sag, velocity collapse (loss of sync), or any SDO error.
        # NOTE: at no-load top speed the current collapses (~25 mA), so id is a
        # weak/noisy signal near the null; N_AVG + the "3 consecutive sign-flips"
        # debounce guard against that. If it aborts with "never crossed zero",
        # raise N_AVG / SETTLE or nudge the current, or fall back to a small manual
        # lag verified on a scope.
        LAG_MAX   = 160     # hard cap; the runaway last time lost sync near ~290
        SETTLE    = 0.08    # s dwell per lag step
        N_AVG     = 6       # id samples averaged per step (small-signal denoise)
        BUS_FLOOR = 250     # abort if bus < 25.0 V (units 0.1 V; nominal ~410)
        VEL_EST   = 50000   # velocity considered "established" before collapse-guard arms

        def _sweep_id_null(tq_sign):
            """Spin at tq_sign*cmd_value, ramp lag until id crosses zero.
            Returns the interpolated zero-crossing lag, or raises RuntimeError."""
            self.node.sdo["TargetTorque"].raw = int(tq_sign * cmd_value)
            _sleep_responsive(0.4)                 # spin up / settle at this direction
            samples = []                           # list of (lag, id_avg)
            vmax = 1
            s0 = None                              # sign of id at the start (lag 0)
            opp = 0                                # consecutive samples with flipped sign
            lag = 0
            while lag <= LAG_MAX:
                self.node.sdo['EncoderConfig']['LagFactor'].raw = lag
                time.sleep(SETTLE)
                wx.Yield()
                # --- safety gates ---
                sw = self.node.sdo["StatusWord"].raw
                if sw & 0x08:                      # DS402 Fault bit
                    raise RuntimeError("drive FAULT (StatusWord={:#06x}) at lag {}".format(sw, lag))
                busv = self.node.sdo['Amplifier']['BusVoltage'].raw
                if busv < BUS_FLOOR:
                    raise RuntimeError("bus sag {:.1f} V at lag {}".format(busv / 10.0, lag))
                vel = abs(self.node.sdo["VelocityFeedback"].raw)
                if vel > vmax:
                    vmax = vel
                elif vmax > VEL_EST and vel < vmax * 0.5:
                    raise RuntimeError("velocity collapse ({} < 50% of {}) at lag {} -- loss of sync".format(
                                       vel, vmax, lag))
                # --- averaged signed d-axis current ---
                acc = 0
                for _ in range(N_AVG):
                    acc += self.node.sdo['Motor']['id'].raw
                    wx.Yield()
                id_avg = acc / float(N_AVG)
                samples.append((lag, id_avg))
                print("  dir {:+d}  Lag: {:3d}  id: {:+8.1f}  vel: {:9d}  bus: {:.1f} V".format(
                      tq_sign, lag, id_avg, vel, busv / 10.0))
                # stop a few steps past the zero crossing: id flips sign from its
                # initial value. Direction-agnostic so it works for either torque
                # sign / Park convention; debounced against single-sample noise.
                if s0 is None and id_avg != 0:
                    s0 = 1 if id_avg > 0 else -1
                cur = 1 if id_avg >= 0 else -1
                opp = opp + 1 if (s0 is not None and cur != s0) else 0
                if opp >= 3:
                    break
                lag += 1
            # interpolate the first sign change (either direction) from the curve
            for i in range(1, len(samples)):
                l0, i0 = samples[i - 1]
                l1, i1 = samples[i]
                if (i0 >= 0) != (i1 >= 0) and (abs(i0) + abs(i1)) > 0:
                    return l0 + (abs(i0) / (abs(i0) + abs(i1))) * (l1 - l0)
            raise RuntimeError("id never crossed zero within LAG_MAX={} (signal too weak or wrong sign)".format(LAG_MAX))

        try:
            null_fwd = _sweep_id_null(+1)
            self.node.sdo['EncoderConfig']['LagFactor'].raw = 0
            self.node.sdo["TargetTorque"].raw = 0
            _sleep_responsive(0.5)
            null_rev = _sweep_id_null(-1)
            self.node.sdo["TargetTorque"].raw = 0
            lag = int(round((null_fwd + null_rev) / 2.0))
            print("Encoder lag (id-null): fwd={:.1f}  rev={:.1f}  ->  LagFactor={}".format(
                  null_fwd, null_rev, lag))
            self.node.sdo['EncoderConfig']['LagFactor'].raw = lag
            self.node.sdo['Save']['Single'].raw = ((0x3013 << 8) | 0x05)   # persist to EE
            print("Saved LagFactor={} to EEPROM.".format(lag))
        except Exception as _e:
            print("Encoder lag cal ABORTED: {}".format(_e))
            print("Restoring LagFactor=0 and TargetTorque=0 (nothing saved).")
            try:
                self.node.sdo["TargetTorque"].raw = 0
                self.node.sdo['EncoderConfig']['LagFactor'].raw = 0
            except Exception:
                pass
        # =========================================================================

        # Set Mode to Idle (0)
        print("Setting Mode = IDLE")
        self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE

        if calAll==False:
          self.Enable()

        self.frame_statusbar.SetStatusText("Ready", 1)

        if self.ADC_ON == False and self.adcWasON == True:
            self.on_off_adc(self)
        if calAll==False:
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
        wx.Yield()

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
          wx.Yield() # keep wx event loop alive so Windows doesn't mark the app "Not Responding"
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
            wx.Yield()

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
                wx.Yield()

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
                wx.Yield()

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
                from paths import resource_path

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
                from paths import session_path
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
        if not self._fw_at_least(4, 4, 0):
            self._prompt_ok("Firmware Too Old",
                "Magnetic encoder compensation requires firmware v4.4.0 or later.\n"
                "Please update the firmware and try again.")
            return

        # If compensation is already active, ask whether to recalibrate or retest.
        # Recalibration always runs an automatic retest sweep afterwards.
        _retest_only = False
        try:
            if self.node.sdo[0x3027][1].raw:
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
            from paths import resource_path, session_path
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
                    _enc_was_active = bool(self.node.sdo[0x3027][1].raw)
                    if _enc_was_active:
                        self.node.sdo[0x3027][1].raw = 0
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
            wx.Yield()

            motor_ud = 0
            while (self.node.sdo['Motor']['id'].raw / 1000.0 * i_peak) < cal_current \
                  and motor_ud < 32000:
                _id_now = self.node.sdo['Motor']['id'].raw / 1000.0 * i_peak
                if motor_ud > 0 and _id_now > 0:
                    _step = max(100, int((motor_ud * cal_current / _id_now - motor_ud) / 4))
                else:
                    _step = max(100, 32000 // 12)
                motor_ud = min(motor_ud + _step, 32000)
                self.node.sdo['Motor']['ud'].raw = motor_ud
                time.sleep(0.05)
                wx.Yield()
            _sleep_responsive(0.3)

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
                wx.Yield()
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
                wx.Yield()
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
                N_BINS = 10
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
                _n_sig       = sum(1 for _k in _cap_ks if float(amps[_k]) >= _sig_thresh)
                _keep_n      = min(N_BINS, max(best_n, _n_sig))
                _top_bins    = _cap_ks[:_keep_n]    # significant, in-band bins, amplitude-descending
                _dropped     = _cap_ks[_keep_n:]    # in-band but below noise floor — not uploaded
                n_upload     = len(_top_bins)

                print("\n  Harmonic trim: noise floor ≈ {:.3f} ct, threshold {:.3f} ct, "
                      "k≤{}, best_n(<1ct)={} → keeping {} of {} bins.".format(
                          _noise_floor, _sig_thresh, K_MAX_ENC, best_n, n_upload, len(sorted_ks)))
                _capped_sig = [_k for _k in _capped if float(amps[_k]) >= _sig_thresh]
                if _capped_sig:
                    print("    Dropped (k>{}, phase unreliable — leave to cogging comp): ".format(
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
                #   ψ = _bphi - 2π·k·enc_start/enc_resolution  (sweep-relative → absolute)
                def _clamp_i16(v):
                    return max(-32768, min(32767, int(round(v))))

                # Sort by k for firmware's iterative complex-rotation optimization
                _top_bins = sorted(_top_bins, key=lambda _k: _k)

                print("\n  Uploading encoder compensation harmonics to node {} ...".format(node_id))
                print("  {:>4}  {:>6}  {:>10}  {:>8}  {:>8}".format(
                    "Bin", "k", "Amp(cts)", "A_s(Q88)", "A_c(Q88)"))
                print("  " + "-" * 44)

                # Disable compensation while writing bins
                self.node.sdo[0x3027][1].raw = 0

                for _bi in range(N_BINS):
                    _as_sub = 2 + _bi * 3
                    _k_sub  = 3 + _bi * 3
                    _ac_sub = 4 + _bi * 3

                    if _bi < n_upload:
                        _bk      = int(_top_bins[_bi])
                        _bA      = float(amps[_bk])
                        _bphi    = float(phases[_bk])
                        _psi     = _bphi - 2.0 * math.pi * _bk * float(enc_start) / float(enc_resolution)
                        _A_s_val = _clamp_i16( 256.0 * _bA * math.sin(_psi))
                        _A_c_val = _clamp_i16(-256.0 * _bA * math.cos(_psi))
                        _k_val   = _bk
                        print("  {:>4d}  {:>6d}  {:>10.4f}  {:>8d}  {:>8d}".format(
                            _bi, _k_val, _bA, _A_s_val, _A_c_val))
                    else:
                        _A_s_val = _A_c_val = _k_val = 0

                    self.node.sdo[0x3027][_as_sub].raw = _A_s_val
                    self.node.sdo[0x3027][_k_sub].raw  = _k_val
                    self.node.sdo[0x3027][_ac_sub].raw = _A_c_val

                # Enable compensation
                self.node.sdo[0x3027][1].raw = 1
                print("  Encoder Compensation Active → 1")
                try:
                    self.frame_menubar.ON.Check(True)
                    self.frame_menubar.OFF.Check(False)
                except Exception:
                    pass
                if n_upload < N_BINS:
                    print("  (Bins {}–{} zeroed — only {} needed for <1ct RMS)".format(
                        n_upload, N_BINS - 1, n_upload))

                # Readback verification
                print("\n  Readback verification:")
                print("  {:>4}  {:>6}  {:>8}  {:>8}  {}".format(
                    "Bin", "k", "A_s", "A_c", "OK?"))
                print("  " + "-" * 40)
                _active_rb = self.node.sdo[0x3027][1].raw
                print("  Active flag readback: {}".format(_active_rb))
                _rb_ok = True
                for _bi in range(N_BINS):
                    _as_rb = self.node.sdo[0x3027][2 + _bi * 3].raw
                    _k_rb  = self.node.sdo[0x3027][3 + _bi * 3].raw
                    _ac_rb = self.node.sdo[0x3027][4 + _bi * 3].raw
                    if _bi < n_upload:
                        _bk_exp  = int(_top_bins[_bi])
                        _bA_exp  = float(amps[_top_bins[_bi]])
                        _psi_exp = (float(phases[_top_bins[_bi]])
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

                # Save all 31 NV subindices of 0x3027 to EEPROM
                print("\n  Saving 0x3027 to EEPROM ...")
                for _si in range(1, 32):
                    self.node.sdo['Save']['Single'].raw = ((0x3027 << 8) | _si)
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
                wx.Yield()

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
                    wx.Yield()
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
                    wx.Yield()
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

    def cogging_error_compensation(self, event, calAll=False, _upd=None):
        """
        Cogging torque characterisation sweep.

        Drives the motor at constant low velocity (forward + reverse) and samples
        q-axis current (Iq) vs mechanical angle.  Bidirectional averaging cancels
        the constant friction bias, isolating the periodic cogging profile.

        Math:
          Iq_fwd(θ) = [ T_fric + T_cog_load(θ)] / Kt   (positive; CW motion)
          Iq_rev(θ) = [-T_fric + T_cog_load(θ)] / Kt   (negative; CCW motion)
          avg(θ)    = (Iq_fwd + Iq_rev) / 2 = T_cog_load(θ) / Kt

        Output files (in logs/):
          cogging_sweep_*.csv       — per-bin (angle, fwd, rev, avg, fit)
          cogging_harmonics_*.json  — FFT harmonic decomposition
          cogging_profile_*.png     — 2×2: fwd/rev/avg, AC+fit, per-pole overlay, spectrum
          cogging_spectrum_*.png    — harmonic bar chart + RMS-vs-N reconstruction curve
          cogging_retest_*.png      — before/after Iq AC comparison (generated if upload succeeds)

        Upload: dominant harmonic written to 0x3028 (Active, ASAmpCos, ACAmpSin, HarmonicK),
        saved to EEPROM, then a bidirectional retest sweep is run with compensation active.
        """

        if not self.check_for_node():
            return
        if not self._fw_at_least(4, 4, 0):
            self._prompt_ok("Firmware Too Old",
                "Cogging compensation calibration requires firmware v4.4.0 or later.\n"
                "Please update the firmware and try again.")
            return

        if self.ADC_ON:
            self.adcWasON = True
            self.on_off_adc(self)
        else:
            self.adcWasON = False

        if calAll:
            if _upd is None:
                _upd = lambda v: None
        else:
            self.OnStartTask(None)
            _upd = lambda v: self.UpdateUI(v)
        if _upd is None:
            _upd = lambda v: None

        self.Disable()
        self.frame_statusbar.SetStatusText("Cogging characterisation sweep...", 1)
        self.frame_statusbar.Update()
        wx.Yield()

        try:
            import cmath as _cm
            import datetime, os
            from paths import resource_path, session_path
            ts = datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')

            enc_resolution = self.node.sdo['EncoderConfig']['Resolution'].raw
            motor_poles    = self.node.sdo['Calibration']['poles'].raw
            i_peak         = self.node.sdo['Calibration']['i_peak'].raw
            kt             = self.node.sdo['Calibration']['kt'].raw  # mNm/A
            pole_pairs     = motor_poles // 2
            cts_per_elec   = enc_resolution * 2.0 / motor_poles

            # High-resolution current measurement via Alpha/Beta filtered ADC + Park transform.
            # CurrentFeedback resolution = i_peak/1000 mA/LSB — unusable for small cogging signals
            # on high-current motors.  Alpha.Filtered (Q12.4) gives ~i_peak/32768/16 mA/LSB.
            _alpha_bias   = self.node.sdo[0x3008][3].raw / 16.0  # Q12.4 → Q12.0 ADC counts
            _beta_bias    = self.node.sdo[0x3009][3].raw / 16.0
            _alpha_gf     = self.node.sdo[0x3008][6].raw   # Q4.12 (4096 = 1.0)
            _beta_gf      = self.node.sdo[0x3009][6].raw
            _isense_shunt = self.node.sdo[0x3008][5].raw   # mΩ
            _isense_gain  = self.node.sdo[0x3008][4].raw   # ×1000 (e.g. 20000 = gain 20)
            _e_zero       = self.node.sdo['Calibration']['e_zero'].raw
            _e_polarity   = int(self.node.sdo['Calibration']['e_polarity'].raw)
            _cts_per_elec = enc_resolution // pole_pairs   # integer, matches firmware

            # mA per raw ADC count — mirrors firmware pwm.c ma_per_ct calculation
            _ma_per_ct = (3.3 / 4096.0 * 1000.0 / _isense_shunt
                          * 1000.0 / _isense_gain * 1000.0)
            _cfb_lsb_ma = i_peak / 1000.0
            print("  Current resolution: Park={:.3f} mA/ct  "
                  "CurrentFeedback={:.2f} mA/LSB".format(_ma_per_ct, _cfb_lsb_ma))

            node_id   = getattr(self.node, 'id', '?')
            _pc       = None
            try:
                _pc = int(self.node.sdo[0x1018][2].raw)
            except Exception:
                pass
            model_str = getattr(self, '_PRODUCT_CODE_MODELS', {}).get(_pc, 'unknown')
            _file_pfx = 'node{}_{}_'.format(node_id, model_str.replace(' ', '_'))

            def _cog_img(name):
                p = session_path('cogging/images/{}'.format(name))
                os.makedirs(os.path.dirname(p), exist_ok=True)
                return p

            def _cog_data(name):
                p = session_path('cogging/data/{}'.format(name))
                os.makedirs(os.path.dirname(p), exist_ok=True)
                return p

            N_BINS       = 128    # angle bins per revolution (k_max=64; resolves k=21,42,63)

            # ── Quasi-static stepped measurement (PROFILE_POSITION) ──────────────────
            # The continuous spin contaminated the profile (velocity surge → variable
            # inertial term → non-repeatable, k=21 swung ±20% even at 16 revs). Instead,
            # step to discrete positions, hold (closed-loop position), and measure the
            # holding iq = cogging torque / Kt at zero velocity. Current-controlled → no
            # d-to-q leakage; settled → no surge / no J·α. Same step→settle→measure pattern
            # that made enc comp repeatable, but with current (not position) feedback in a
            # current-controlled mode. FLAG OFF restores the original continuous-spin path.
            # NOTE: first hardware draft — verify settle behaviour and holding-iq sign.
            STEPPED_MEASURE = True
            N_STEP          = N_BINS   # positions/rev (Nyquist 64 > k=42)
            STEP_SETTLE_S   = 0.30     # settle after each PP move before measuring
            N_AVG_STEP      = 8        # holding-iq reads averaged per held position
            PP_PROFILE_VEL  = 130000   # cts/s move speed between steps (per run_test)

            # Cal speed: hold the cogging fundamental near a fixed, velocity-loop-trackable
            # frequency regardless of pole count, so measured Iq ≈ the true cogging torque
            # (in-phase) rather than the loop's attenuated/lagged response above its bandwidth.
            # cogging fundamental = pole_pairs × RPM/60 [Hz]  →  RPM = f_target × 60 / pole_pairs.
            # f_target ~5 Hz is safely inside any reasonable velocity-loop bandwidth. When the
            # velocity-loop BW is exposed (newer FW), raise f_target toward BW/margin to cal
            # faster on high-bandwidth motors. Stall-retry below still raises RPM if friction
            # stalls the motor at the computed speed (floor); this just sets the start point.
            COG_CAL_FUND_HZ = 5.0
            TARGET_RPM   = max(5.0, COG_CAL_FUND_HZ * 60.0 / max(pole_pairs, 1))  # mech RPM, pole-adaptive
            SETTLE_S     = 1.5   # velocity PI settles in <1s; 1.5s is conservative
            SAMPLE_S     = 0.025 # sampling interval (s)
            # N_REVS from a target samples-per-bin. Raised from ~4 to ~20: the cogging
            # profile was non-repeatable (k=21 swung ~50% run-to-run at 3 revs). More revs
            # → more samples averaged per angle bin → tighter profile. DIAGNOSTIC: if this
            # makes the profile repeatable, the variability was random noise (fixed); if it
            # still swings, the contamination is systematic (position-locked inertial surge)
            # and needs the quasi-static method instead. Costs sweep time (~1 min/pass here).
            SAMPLES_PER_BIN = 20
            N_REVS       = max(2, int(math.ceil(
                SAMPLES_PER_BIN * N_BINS * SAMPLE_S * TARGET_RPM / 60.0)))
            N_HARMONICS  = 16    # Fourier harmonics to fit

            vel_cts_per_sec = int(round(TARGET_RPM / 60.0 * enc_resolution))
            n_samples       = int(N_REVS * 60.0 / TARGET_RPM / SAMPLE_S)
            est_s           = (SETTLE_S + N_REVS * 60.0 / TARGET_RPM) * 2 + 5
            bin_width_deg   = 360.0 / N_BINS

            print("\nCogging characterisation — sweep parameters")
            print("  {} pole pairs  {:.2f} cts/elec  enc_res={}"
                  "  i_peak={} mA  Kt={} mNm/A".format(
                      pole_pairs, cts_per_elec, enc_resolution, i_peak, kt))
            print("  {:.1f} RPM  {} revs/pass  {} bins/rev  ~{:.0f} s total".format(
                TARGET_RPM, N_REVS, N_BINS, est_s))
            _upd(2)

            # ---- Disable any active cogging compensation before sweeping ----
            # Sweeping with compensation active would corrupt the measurement:
            # the FFT would fit the residual pattern (not raw cogging), and
            # the uploaded correction would then replace, not improve, the
            # previous compensation.
            # Unconditional write + readback verify with retry — a silent failure
            # here is the worst possible outcome (corrupted calibration data).
            _cog_was_active = False
            for _dis_attempt in range(3):
                try:
                    _cog_was_active = bool(self.node.sdo[0x3028][1].raw)
                    self.node.sdo[0x3028][1].raw = 0
                    if int(self.node.sdo[0x3028][1].raw) == 0:
                        break
                except Exception:
                    pass
                time.sleep(0.1)
            else:
                raise RuntimeError(
                    "Could not disable cogging compensation before sweep — "
                    "check CAN connection and retry.")
            if _cog_was_active:
                print("  NOTE: Cogging compensation was active — disabled for sweep.")

            # ---- Reboot to reset velocity integrator state ----
            # Ki=0 silences the cogging signal: inertia absorbs the disturbance and
            # it never appears in Iq.  The integral IS the signal — it winds up over
            # multiple revolutions to pre-compensate the periodic torque ripple, and
            # that steady-state integral value is what we bin and FFT.  Stale integral
            # state from a previous run biases the measured Iq phase.  A reboot is
            # the only way to reset the integrator to zero before the sweep.
            print("  Rebooting node {} to clear velocity integrator state...".format(node_id))
            self.network.send_message(0x0, [0x81, int(node_id)])
            _sleep_responsive(1.5)
            self.configure_Puck(configure_pdos=False)
            # Re-disable cogging compensation (reboot restored EEPROM value).
            for _dis_attempt in range(3):
                try:
                    self.node.sdo[0x3028][1].raw = 0
                    if int(self.node.sdo[0x3028][1].raw) == 0:
                        break
                except Exception:
                    pass
                time.sleep(0.1)
            else:
                raise RuntimeError(
                    "Could not disable cogging compensation after reboot — "
                    "check CAN connection and retry.")

            if STEPPED_MEASURE:
                # ---- Enable in profile-position mode (quasi-static stepping) ----
                self.node.sdo["ControlWord"].raw = CLEAR_FAULT
                self.node.sdo["ControlWord"].raw = SHUTDOWN
                self.node.sdo["ControlWord"].raw = OP_ENABLED
                self.node.sdo["SetModeOfOperation"].raw = MODE_PROFILE_POS
                self.node.rpdo[1]["SetModeOfOperation"].raw = MODE_PROFILE_POS
                self.node.sdo["ProfileVelocity"].raw = PP_PROFILE_VEL
                # Seed RPDO ControlWord = OP_ENABLED | change-set-immediately (bit5=0x20)
                # so the bit-4 new-setpoint handshake toggles from a valid base (0x2F↔0x3F).
                self.node.rpdo[1]["ControlWord"].raw = 0x2F
                self.node.rpdo[1].transmit()
                self.node.network.sync.transmit()
                time.sleep(0.2)
            else:
                # ---- Enable in profile-velocity mode ----
                self.node.sdo["ControlWord"].raw = CLEAR_FAULT
                self.node.sdo["ControlWord"].raw = SHUTDOWN
                self.node.sdo["ControlWord"].raw = OP_ENABLED
                self.node.sdo["SetModeOfOperation"].raw = MODE_PROFILE_VEL
            time.sleep(0.2)
            wx.Yield()

            def _sample_pass(vel_cmd, label, upd_start, upd_end, settle_s=SETTLE_S):
                """Collect (angle_deg, iq_mA) at each sample tick over N_REVS.

                iq is measured via Alpha.Filtered + Beta.Filtered + Park transform
                for ~16× better resolution than CurrentFeedback on high-i_peak motors.
                """
                print("  {} pass  ({:+d} cts/s = {:.1f} RPM, {} revs) ...".format(
                    label, vel_cmd, abs(vel_cmd) * 60.0 / enc_resolution, N_REVS))
                self.node.sdo['TargetVelocity'].raw = vel_cmd
                _sleep_responsive(settle_s)
                wx.Yield()

                samples = []  # list of (angle_deg, iq_mA)
                # IIR filter (α=15/16) group delay = 15 samples at 8 kHz update
                # rate (40 kHz PWM / 5 patterns/cycle) = 1.875 ms.  The snapshot
                # OD (0x3014) captures alpha, beta, and enc atomically in one CAN
                # callback, so inter-value skew is ≤1 ISR period (25 µs) = <0.3°
                # phase error at k=7.  Correct the remaining lag by rewinding the
                # encoder position by the distance the rotor moved in 1.875 ms.
                _lag_cts = round(1.875e-3 * abs(vel_cmd))
                _dir     = 1 if vel_cmd >= 0 else -1
                for step in range(n_samples):
                    time.sleep(SAMPLE_S)
                    # Sub1 read triggers the freeze; sub2 and sub3 return the
                    # same snapshot regardless of how long Python takes to read them.
                    alpha_filt = self.node.sdo[0x3014][1].raw   # Q12.4; triggers freeze
                    beta_filt  = self.node.sdo[0x3014][2].raw   # Q12.4
                    enc_snap   = self.node.sdo[0x3014][3].raw   # 0-4095

                    # Rewind encoder to when the current was actually flowing.
                    enc_corr = (enc_snap - _dir * _lag_cts) % enc_resolution

                    # Convert filtered ADC counts to mA (firmware sign: bias - raw)
                    alpha_mA = ((_alpha_bias - alpha_filt / 16.0)
                                * (_alpha_gf / 4096.0) * _ma_per_ct)
                    beta_mA  = ((_beta_bias  - beta_filt  / 16.0)
                                * (_beta_gf  / 4096.0) * _ma_per_ct)

                    # theta_e matching firmware: signed counts → F16 angle → radians
                    theta_m    = _e_polarity * (enc_corr - _e_zero)
                    theta_e_ct = (enc_resolution + theta_m) % _cts_per_elec
                    if theta_e_ct >= _cts_per_elec // 2:
                        theta_e_ct -= _cts_per_elec
                    theta_e    = theta_e_ct / _cts_per_elec * 2.0 * math.pi

                    # Park: q = -α·sin(θ_e) + β·cos(θ_e)
                    iq_ma     = -alpha_mA * math.sin(theta_e) + beta_mA * math.cos(theta_e)
                    angle_deg = enc_corr / enc_resolution * 360.0
                    samples.append((angle_deg, iq_ma))
                    _upd(upd_start + step * (upd_end - upd_start) // n_samples)
                    wx.Yield()
                return samples

            def _check_vel_quality(samples, is_fwd):
                """Return (vel_mean_dps, vel_rms_ac_dps). Raises if motor is stalling."""
                vels = []
                for i in range(1, len(samples)):
                    d = samples[i][0] - samples[i - 1][0]
                    if is_fwd and d < -180.0:
                        d += 360.0
                    elif not is_fwd and d > 180.0:
                        d -= 360.0
                    vels.append(d / SAMPLE_S)
                if not vels:
                    return 0.0, 0.0
                vel_mean   = sum(vels) / len(vels)
                vel_rms_ac = (sum((v - vel_mean) ** 2 for v in vels) / len(vels)) ** 0.5
                ratio      = vel_rms_ac / abs(vel_mean) if vel_mean != 0 else float('inf')
                print("  velocity: mean={:.1f} deg/s  ripple={:.1f} deg/s  ({:.0f}%)".format(
                    vel_mean, vel_rms_ac, ratio * 100))
                if ratio > 1.0:
                    raise RuntimeError(
                        "Motor stalled during cogging sweep "
                        "(velocity ripple {:.0f}% of mean — motor is reversing). "
                        "Increase TARGET_RPM from {:.0f} to at least {:.0f} RPM and retry.".format(
                            ratio * 100, TARGET_RPM,
                            TARGET_RPM * (ratio / 0.2) ** 0.5))
                if ratio > 0.5:
                    raise RuntimeError(
                        "Motor stalled at {:.0f} RPM: velocity ripple {:.0f}% exceeds 50% — "
                        "phase data too noisy for reliable FFT. "
                        "Retry at at least {:.0f} RPM.".format(
                            TARGET_RPM, ratio * 100,
                            TARGET_RPM * (ratio / 0.2) ** 0.5))
                return vel_mean, vel_rms_ac

            # Retry at progressively higher RPM if motor stalls.
            # _sample_pass and _check_vel_quality close over n_samples, N_REVS,
            # vel_cts_per_sec, and TARGET_RPM — reassigning them here is enough.
            # Keep retrying even past the upload-frequency guard (10 Hz threshold)
            # so the profile plots are clean; the guard skips the upload separately.
            import re as _re_rpm
            MAX_SWEEP_RPM = 500.0
            def _pp_step_to(target_cts):
                """Profile-Position set-point handshake (mirrors run_test): load the new
                absolute target into RPDO2, raise ControlWord bit4 + one SYNC to latch it,
                wait for set-point ACK (StatusWord bit12=1), then drop the request line.
                ~1 s timeouts on each rendezvous."""
                for _ in range(100):
                    if not (self.node.sdo["StatusWord"].raw & 0x1000): break
                    time.sleep(0.01)
                self.node.rpdo[2]["TargetPosition"].raw = int(target_cts)
                self.node.rpdo[2].transmit()
                self.node.rpdo[1]["ControlWord"].raw = 0x3F   # bit4 ↑ : latch + start move
                self.node.rpdo[1].transmit()
                self.node.network.sync.transmit()
                for _ in range(100):
                    if self.node.sdo["StatusWord"].raw & 0x1000: break
                    time.sleep(0.01)
                self.node.rpdo[1]["ControlWord"].raw = 0x2F   # bit4 ↓ : re-arm
                self.node.rpdo[1].transmit()
                self.node.network.sync.transmit()
                for _ in range(100):
                    if not (self.node.sdo["StatusWord"].raw & 0x1000): break
                    time.sleep(0.01)

            def _sample_stepped(direction, label, upd_start, upd_end):
                """Step one mechanical rev in relative PP moves; at each settled position
                measure the holding iq = cogging torque / Kt (zero velocity → no inertial
                term, current-controlled → no d-to-q leakage). Returns the same
                (angle_deg, iq_mA) list the continuous pass produces."""
                print("  {} stepped pass ({} positions) ...".format(label, N_STEP))
                step_cts  = direction * max(1, int(round(enc_resolution / N_STEP)))
                start_pos = int(self.node.sdo["PositionFeedback"].raw)
                _samps = []
                for _si in range(N_STEP):
                    _pp_step_to(start_pos + (_si + 1) * step_cts)
                    _sleep_responsive(STEP_SETTLE_S)   # let velocity settle to ~0
                    _iqs = []; _angs = []
                    for _ in range(N_AVG_STEP):
                        _af = self.node.sdo[0x3014][1].raw   # Q12.4 alpha (triggers freeze)
                        _bf = self.node.sdo[0x3014][2].raw   # Q12.4 beta (same snapshot)
                        _en = self.node.sdo[0x3014][3].raw   # 0..4095 (same snapshot)
                        _amA = (_alpha_bias - _af / 16.0) * (_alpha_gf / 4096.0) * _ma_per_ct
                        _bmA = (_beta_bias  - _bf / 16.0) * (_beta_gf  / 4096.0) * _ma_per_ct
                        _tm  = _e_polarity * (_en - _e_zero)
                        _tec = (enc_resolution + _tm) % _cts_per_elec
                        if _tec >= _cts_per_elec // 2: _tec -= _cts_per_elec
                        _te  = _tec / _cts_per_elec * 2.0 * math.pi
                        _iqs.append(-_amA * math.sin(_te) + _bmA * math.cos(_te))
                        _angs.append((_en % enc_resolution) / enc_resolution * 360.0)
                        time.sleep(0.004)
                    _samps.append((sum(_angs) / len(_angs), sum(_iqs) / len(_iqs)))
                    if callable(_upd):
                        _upd(upd_start + _si * (upd_end - upd_start) // N_STEP)
                    wx.Yield()
                return _samps

            if STEPPED_MEASURE:
                samples_fwd = _sample_stepped(+1, 'forward', 5, 44)
                _sleep_responsive(0.5)
                samples_rev = _sample_stepped(-1, 'reverse', 47, 86)

            for _rpm_attempt in ([] if STEPPED_MEASURE else range(6)):
                print("Cogging sweep:")
                try:
                    samples_fwd = _sample_pass(+vel_cts_per_sec, 'forward', 5, 44)
                    _check_vel_quality(samples_fwd, is_fwd=True)

                    self.node.sdo['TargetVelocity'].raw = 0
                    _sleep_responsive(1.5)
                    wx.Yield()

                    samples_rev = _sample_pass(-vel_cts_per_sec, 'reverse', 47, 86)
                    _check_vel_quality(samples_rev, is_fwd=False)
                    break  # success

                except RuntimeError as _stall_exc:
                    self.node.sdo['TargetVelocity'].raw = 0
                    _sleep_responsive(1.0)
                    wx.Yield()
                    if 'stalled' not in str(_stall_exc).lower():
                        raise
                    if TARGET_RPM >= MAX_SWEEP_RPM:
                        raise RuntimeError(
                            "Motor stalled at {:.0f} RPM — cannot sweep.".format(TARGET_RPM))
                    _m = _re_rpm.search(r'at least (\d+(?:\.\d+)?)', str(_stall_exc))
                    _min_rpm = float(_m.group(1)) if _m else TARGET_RPM * 2.0
                    # Cap increment to 2× current RPM per step.
                    _capped = min(_min_rpm * 1.3, TARGET_RPM * 2.0, MAX_SWEEP_RPM)
                    TARGET_RPM      = math.ceil(_capped / 5) * 5.0
                    N_REVS          = max(2, int(math.ceil(
                        SAMPLES_PER_BIN * N_BINS * SAMPLE_S * TARGET_RPM / 60.0)))
                    vel_cts_per_sec = int(round(TARGET_RPM / 60.0 * enc_resolution))
                    n_samples       = int(N_REVS * 60.0 / TARGET_RPM / SAMPLE_S)
                    print("  Retrying at {:.0f} RPM  ({} revs/pass) ...".format(
                        TARGET_RPM, N_REVS))

            self.node.sdo['TargetVelocity'].raw = 0
            _sleep_responsive(1.0)
            self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE
            wx.Yield()

            _upd(88)

            print("  {} fwd samples, {} rev samples".format(
                len(samples_fwd), len(samples_rev)))

            # ---- Bin by absolute mechanical angle ----
            fwd_bins = [[] for _ in range(N_BINS)]
            rev_bins = [[] for _ in range(N_BINS)]
            for deg, iq in samples_fwd:
                fwd_bins[int(deg / bin_width_deg) % N_BINS].append(iq)
            for deg, iq in samples_rev:
                rev_bins[int(deg / bin_width_deg) % N_BINS].append(iq)

            avg_fwd = [sum(b) / len(b) if b else 0.0 for b in fwd_bins]
            avg_rev = [sum(b) / len(b) if b else 0.0 for b in rev_bins]
            avg_iq  = [(avg_fwd[b] + avg_rev[b]) / 2.0 for b in range(N_BINS)]
            bin_deg = [b * bin_width_deg for b in range(N_BINS)]

            # ---- DFT / Fourier fit ----
            def _dft(samples, n_harm):
                N, X = len(samples), []
                for k in range(n_harm + 1):
                    wk  = _cm.exp(-2j * math.pi * k / N)
                    val = 0.0 + 0j
                    w   = 1.0 + 0j
                    for c in samples:
                        val += c * w
                        w   *= wk
                    X.append(val / N)
                return X

            def _reconstruct_f(X, out_size):
                """Evaluate Fourier series at out_size evenly-spaced points (float)."""
                table = []
                for p in range(out_size):
                    val = X[0].real
                    for k in range(1, len(X)):
                        a = 2.0 * math.pi * k * p / out_size
                        val += 2.0 * (X[k].real * math.cos(a) - X[k].imag * math.sin(a))
                    table.append(val)
                return table

            print("Fitting Fourier series ({} harmonics) ...".format(N_HARMONICS))
            X_iq   = _dft(avg_iq, N_HARMONICS)
            fit_iq = _reconstruct_f(X_iq, N_BINS)

            dc_offset_ma = X_iq[0].real
            iq_ac        = [v - dc_offset_ma for v in avg_iq]
            rms_iq_ma    = (sum(v * v for v in iq_ac) / len(iq_ac)) ** 0.5
            max_iq_ma    = max(abs(v) for v in iq_ac)
            rms_cog_mnm  = kt * rms_iq_ma / 1000.0   # Kt mNm/A × Iq mA × 1e-3 → mNm
            max_cog_mnm  = kt * max_iq_ma / 1000.0

            print("\n  Cogging profile stats:")
            print("  DC offset: {:+.2f} mA  (friction/load bias)".format(dc_offset_ma))
            print("  AC RMS Iq: {:.2f} mA  →  RMS cogging {:.2f} mNm".format(
                rms_iq_ma, rms_cog_mnm))
            print("  AC peak Iq: {:.2f} mA  →  peak cogging {:.2f} mNm".format(
                max_iq_ma, max_cog_mnm))

            # ---- Per-pole-pair overlay ----
            elec_bins = max(1, N_BINS // pole_pairs)
            pp_acc  = [0.0] * elec_bins
            pp_cnt  = [0]   * elec_bins
            for b in range(N_BINS):
                eb = b % elec_bins
                pp_acc[eb] += iq_ac[b]
                pp_cnt[eb] += 1
            pp_avg = [pp_acc[e] / max(pp_cnt[e], 1) for e in range(elec_bins)]

            # ---- CSV ----
            csv_path = _cog_data('{}cogging_sweep_{}.csv'.format(_file_pfx, ts))
            with open(csv_path, 'w') as _f:
                _f.write("# Cogging torque sweep — Node {}  {}  ({})\n".format(
                    node_id, model_str, ts))
                _f.write("# {:.1f} RPM  {} revs/pass  {} bins/rev\n".format(
                    TARGET_RPM, N_REVS, N_BINS))
                _f.write("# DC offset (friction bias): {:+.3f} mA\n".format(dc_offset_ma))
                _f.write("# T_cogging ≈ Kt × avg_iq_mA × 1e-3  (Kt={} mNm/A)\n".format(kt))
                _f.write("angle_deg,iq_fwd_mA,iq_rev_mA,iq_avg_mA,iq_fit_mA\n")
                for b in range(N_BINS):
                    _f.write("{:.3f},{:.4f},{:.4f},{:.4f},{:.4f}\n".format(
                        bin_deg[b], avg_fwd[b], avg_rev[b], avg_iq[b], fit_iq[b]))
            print("\n  Cogging sweep CSV → {}".format(csv_path))

            # ---- FFT harmonic analysis (numpy) ----
            try:
                import numpy as _np
                import json as _json

                tf      = _np.array(avg_iq, dtype=_np.float64)
                X_np    = _np.fft.rfft(tf)
                N_fft   = len(avg_iq)
                amps    = 2.0 * _np.abs(X_np) / N_fft
                phases  = _np.angle(X_np)
                amps[0]  /= 2.0
                amps[-1] /= 2.0

                order  = 1 + _np.argsort(amps[1:])[::-1]
                top_n  = min(40, len(order))

                print("\n  FFT harmonic analysis  (N={})".format(N_fft))
                print("  {:>4s}  {:>10s}  {:>10s}  {:>12s}  {:>12s}".format(
                    "k", "Amp(mA)", "Phase(rad)", "cos coeff", "sin coeff"))
                print("  " + "-" * 54)

                harmonic_list = []
                for _ki in range(top_n):
                    k   = int(order[_ki])
                    A   = float(amps[k])
                    phi = float(phases[k])
                    a_k = A * _np.cos(phi)
                    b_k = -A * _np.sin(phi)
                    print("  {:>4d}  {:>10.4f}  {:>10.5f}  {:>12.4f}  {:>12.4f}".format(
                        k, A, phi, float(a_k), float(b_k)))
                    harmonic_list.append({
                        "k":                 k,
                        "cycles_per_rev":    k,
                        "amplitude_mA":      A,
                        "amplitude_mNm":     float(kt * A / 1000.0),
                        "phase_rad":         phi,
                        "cos_coeff":         float(a_k),
                        "sin_coeff":         float(b_k),
                    })

                # Minimum harmonics for RMS reconstruction < 0.5 mA
                sorted_ks       = [int(order[i]) for i in range(len(order))]
                X_recon         = _np.zeros(N_fft // 2 + 1, dtype=_np.complex128)
                X_recon[0]      = X_np[0]
                best_n          = len(sorted_ks)
                RECON_THRESH_MA = 0.5
                for _ni in range(1, len(sorted_ks) + 1):
                    for _ki in range(_ni):
                        X_recon[sorted_ks[_ki]] = X_np[sorted_ks[_ki]]
                    recon   = _np.fft.irfft(X_recon, n=N_fft)
                    rms_err = float(_np.sqrt(_np.mean((tf - recon) ** 2)))
                    if rms_err < RECON_THRESH_MA:
                        best_n = _ni
                        break

                print("\n  Harmonics for RMS < {:.1f} mA: {}".format(RECON_THRESH_MA, best_n))
                print("  Expected dominant cogging periods/rev: {} (= 2 × {} pole pairs)".format(
                    2 * pole_pairs, pole_pairs))

                fft_data = {
                    "node_id":              node_id,
                    "model":                model_str,
                    "timestamp":            ts,
                    "enc_resolution":       enc_resolution,
                    "pole_pairs":           pole_pairs,
                    "target_rpm":           TARGET_RPM,
                    "n_bins":               N_fft,
                    "dc_offset_mA":         float(amps[0]),
                    "rms_iq_ac_mA":         rms_iq_ma,
                    "max_iq_ac_mA":         max_iq_ma,
                    "rms_cogging_mNm":      rms_cog_mnm,
                    "max_cogging_mNm":      max_cog_mnm,
                    "kt_mNm_per_A":         kt,
                    "harmonics_for_0p5mA":  best_n,
                    "harmonics_by_amplitude": harmonic_list,
                }
                json_path = _cog_data('{}cogging_harmonics_{}.json'.format(_file_pfx, ts))
                with open(json_path, 'w') as _jf:
                    _json.dump(fft_data, _jf, indent=2)
                print("  Harmonics JSON → {}".format(json_path))

                # ---- Profile plot: 2×2 ----
                try:
                    import matplotlib
                    matplotlib.use('Agg')
                    import matplotlib.pyplot as plt

                    _colors = plt.cm.tab10.colors
                    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
                    fig.suptitle(
                        'Cogging Torque Profile — Node {}  {}  ({})\n'
                        '{} pole pairs  {:.1f} RPM  {} bins/rev'
                        '  DC={:+.2f} mA  AC RMS={:.2f} mA  peak={:.2f} mA'.format(
                            node_id, model_str, ts,
                            pole_pairs, TARGET_RPM, N_BINS,
                            dc_offset_ma, rms_iq_ma, max_iq_ma))

                    ax = axes[0, 0]
                    ax.plot(bin_deg, avg_fwd, 'b-', linewidth=0.9, alpha=0.7,
                            label='Forward (CW)')
                    ax.plot(bin_deg, avg_rev, 'r-', linewidth=0.9, alpha=0.7,
                            label='Reverse (CCW)')
                    ax.plot(bin_deg, avg_iq,  'k-', linewidth=1.5,
                            label='Bidir avg')
                    ax.axhline(dc_offset_ma, color='gray', linewidth=0.8,
                               linestyle='--',
                               label='DC bias {:+.2f} mA'.format(dc_offset_ma))
                    ax.set_xlabel('Mechanical angle (°)')
                    ax.set_ylabel('Iq (mA)')
                    ax.set_title('Forward / reverse / bidirectional average')
                    ax.legend(fontsize=8)
                    ax.grid(True, alpha=0.3)

                    ax = axes[0, 1]
                    ax.plot(bin_deg, iq_ac, 'b.', markersize=2, alpha=0.5,
                            label='AC component')
                    fit_iq_ac = [v - dc_offset_ma for v in fit_iq]
                    ax.plot(bin_deg, fit_iq_ac, 'r-', linewidth=1.5,
                            label='Fourier fit ({} harmonics)'.format(N_HARMONICS))
                    ax.axhline(0, color='k', linewidth=0.5, linestyle='--')
                    ax.set_xlabel('Mechanical angle (°)')
                    ax.set_ylabel('Iq AC component (mA)')
                    ax.set_title('Cogging profile (DC removed) + Fourier fit')
                    ax.legend(fontsize=8)
                    ax.grid(True, alpha=0.3)

                    ax = axes[1, 0]
                    elec_deg = [e / elec_bins * 360.0 for e in range(elec_bins)]
                    for pp in range(pole_pairs):
                        start = pp * elec_bins
                        pp_pts = [iq_ac[start + e] for e in range(elec_bins)
                                  if start + e < N_BINS]
                        if pp_pts:
                            ax.plot(elec_deg[:len(pp_pts)], pp_pts,
                                    color=_colors[pp % 10], alpha=0.7,
                                    linewidth=0.9, label='Pole pair {}'.format(pp + 1))
                    ax.plot(elec_deg, pp_avg, 'k-', linewidth=2, label='Average')
                    ax.axhline(0, color='k', linewidth=0.5, linestyle='--')
                    ax.set_xlabel('Electrical angle (°)')
                    ax.set_ylabel('Iq AC (mA)')
                    ax.set_title('Per-pole-pair overlay\n'
                                 '(consistent = electrical cogging; '
                                 'spread = mechanical variation)')
                    ax.legend(fontsize=7, ncol=min(4, pole_pairs + 1))
                    ax.grid(True, alpha=0.3)

                    ax = axes[1, 1]
                    k_show = min(48, len(amps) - 1)
                    ax.bar(range(1, k_show + 1), amps[1:k_show + 1],
                           color='steelblue', width=0.8)
                    dom_k = pole_pairs  # fundamental cogging harmonic (1×/elec cycle)
                    if 0 < dom_k <= k_show:
                        ax.axvline(dom_k, color='orange', linestyle='--',
                                   label='k={} (1×/elec)'.format(dom_k))
                    if 0 < 2 * dom_k <= k_show:
                        ax.axvline(2 * dom_k, color='red', linestyle='--',
                                   label='k={} (2×/elec)'.format(2 * dom_k))
                    ax.set_xlabel('Harmonic k (cycles/rev)')
                    ax.set_ylabel('Amplitude (mA)')
                    ax.set_title('Harmonic spectrum  ({} bins)'.format(N_fft))
                    ax.legend(fontsize=8)
                    ax.grid(True, alpha=0.3)

                    plt.tight_layout()
                    plot_path = _cog_img(
                        '{}cogging_profile_{}.png'.format(_file_pfx, ts))
                    plt.savefig(plot_path, dpi=100)
                    plt.close()
                    print("  Profile plot → {}".format(plot_path))
                except ImportError:
                    print("  (Profile plot skipped — matplotlib not installed)")
                except Exception as _pe:
                    print("  WARNING: profile plot failed: {}".format(_pe))

                # ---- Spectrum plot: bar + RMS-vs-N ----
                try:
                    import matplotlib
                    matplotlib.use('Agg')
                    import matplotlib.pyplot as _plt2

                    rms_curve = []
                    X_r2  = _np.zeros(N_fft // 2 + 1, dtype=_np.complex128)
                    X_r2[0] = X_np[0]
                    for _ni2 in range(1, min(50, len(sorted_ks)) + 1):
                        X_r2[sorted_ks[_ni2 - 1]] = X_np[sorted_ks[_ni2 - 1]]
                        recon2 = _np.fft.irfft(X_r2, n=N_fft)
                        rms_curve.append(float(_np.sqrt(_np.mean((tf - recon2) ** 2))))

                    _fig2, (_ax1, _ax2) = _plt2.subplots(1, 2, figsize=(12, 4))
                    _fig2.suptitle(
                        'Cogging Spectrum — Node {}  {}  ({})'.format(
                            node_id, model_str, ts), fontsize=11)

                    _ax1.bar(range(1, k_show + 1), amps[1:k_show + 1],
                             color='steelblue', width=0.8)
                    if best_n <= k_show:
                        _ax1.axvline(best_n, color='red', linestyle='--',
                                     label='N={} (RMS<{:.1f} mA)'.format(
                                         best_n, RECON_THRESH_MA))
                        _ax1.legend(fontsize=8)
                    else:
                        _ax1.set_title('Harmonic amplitudes  ({} bins)'
                                       '  N_min={}'.format(N_fft, best_n))
                    _ax1.set_xlabel('Harmonic k')
                    _ax1.set_ylabel('Amplitude (mA)')
                    _ax1.grid(True, alpha=0.3)

                    _ax2.plot(range(1, len(rms_curve) + 1), rms_curve, 'b-o', markersize=3)
                    _ax2.axhline(RECON_THRESH_MA, color='red', linestyle='--',
                                 label='{:.1f} mA threshold'.format(RECON_THRESH_MA))
                    _ax2.axvline(best_n, color='red', linestyle=':',
                                 label='N={}'.format(best_n))
                    _ax2.set_xlabel('Number of harmonics')
                    _ax2.set_ylabel('RMS error (mA)')
                    _ax2.set_title('Reconstruction RMS vs harmonic count')
                    _ax2.legend(fontsize=8)
                    _ax2.grid(True, alpha=0.3)

                    _plt2.tight_layout()
                    spec_path = _cog_img(
                        '{}cogging_spectrum_{}.png'.format(_file_pfx, ts))
                    _plt2.savefig(spec_path, dpi=100)
                    _plt2.close(_fig2)
                    print("  Spectrum plot → {}".format(spec_path))
                except ImportError:
                    print("  (Spectrum plot skipped — matplotlib not installed)")
                except Exception as _spe:
                    print("  WARNING: spectrum plot failed: {}".format(_spe))

            except ImportError:
                print("  (FFT analysis skipped — numpy not installed)")
            except Exception as _fft_exc:
                print("  WARNING: FFT analysis failed: {}".format(_fft_exc))

            _upd(93)

            # ── Upload top-10 cogging harmonic bins to 0x3028 ───────────────
            # Layout mirrors 0x3027: bin i uses subindices 2+i*3 (a_s), 3+i*3 (k), 4+i*3 (a_c)
            # Bins written amplitude-descending: bin 0 = dominant harmonic.
            # Firmware convention (straight mA, no ×256):
            #   a_s = -A·sin(φ)  (sin Fourier coefficient of measured Iq profile)
            #   a_c = +A·cos(φ)  (cos Fourier coefficient of measured Iq profile)
            #   verify: √(a_s²+a_c²) = A;  φ = atan2(-a_s, a_c)
            N_COG_BINS = 10
            _upload_ok = False
            try:
                def _clamp_i16_cog(v):
                    return max(-32768, min(32767, int(round(v))))

                # SNR guard: check the dominant physical harmonic against the
                # per-harmonic noise floor.  RMS-vs-ADC-step is the wrong metric —
                # the FFT averages over N_BINS/2 harmonics, so per-harmonic noise is
                # much lower than the raw ADC step.
                # Noise floor ≈ ADC_step / sqrt(3 × n_samples / (N_BINS/2))
                _noise_floor_ma = _ma_per_ct / math.sqrt(
                    3.0 * max(n_samples, 1) / max(N_BINS // 2, 1))
                _dom_k        = next((k for k in sorted_ks if k > 0 and k <= N_BINS // 2), 0)
                _dom_phys_amp = float(amps[_dom_k]) if _dom_k else 0.0
                print("  Per-harmonic noise floor: {:.2f} mA  "
                      "dominant harmonic k={} at {:.2f} mA  "
                      "(SNR {:.1f}×)".format(
                          _noise_floor_ma, _dom_k, _dom_phys_amp,
                          _dom_phys_amp / max(_noise_floor_ma, 1e-9)))
                if _dom_phys_amp < 2.0 * _noise_floor_ma:
                    print("  SKIPPING upload: dominant harmonic ({:.2f} mA) "
                          "below 2× noise floor ({:.2f} mA). "
                          "Motor cogging too small to compensate reliably.".format(
                              _dom_phys_amp, 2.0 * _noise_floor_ma))
                    raise NameError("snr_too_low")

                # Phase quality is guaranteed by the retry loop: _check_vel_quality
                # raised if ripple > 50%, so reaching here means sweep data is clean
                # regardless of what RPM was needed.  Report sweep RPM for reference only.
                _cog_freq_hz = TARGET_RPM / 60.0 * pole_pairs
                if _cog_freq_hz > 25.0:
                    print("  Note: sweep required {:.0f} RPM (k={} at {:.1f} Hz); "
                          "data quality validated by <50%% velocity ripple.".format(
                              TARGET_RPM, pole_pairs, _cog_freq_hz))

                # Upload top-N harmonics by amplitude above the per-harmonic noise floor.
                # Only upload physical cogging harmonics (k = n × pole_pairs).
                # Non-multiples cluster at the practical measurement noise floor
                # (~15–35 mA from friction hysteresis and positioning jitter) and
                # cannot be reliably distinguished from noise regardless of sweep method.
                #
                _cog_ks  = [k for k in sorted_ks
                            if k > 0 and k % pole_pairs == 0 and k <= N_BINS // 2
                            and float(amps[k]) >= 2.0 * _noise_floor_ma]
                _top_cog = _cog_ks[:N_COG_BINS]
                _n_cog   = len(_top_cog)
                print("  Physical cogging harmonics above 2× noise floor "
                      "(k = n×{}): {}".format(
                    pole_pairs, [k for k in _top_cog]))
                _cog_drop = [k for k in sorted_ks
                             if k > 0 and k % pole_pairs == 0 and k <= N_BINS // 2
                             and float(amps[k]) < 2.0 * _noise_floor_ma]
                if _cog_drop:
                    print("    Dropped (sub-noise, not uploaded): " + ", ".join(
                        "k={}({:.1f}mA)".format(_dk, float(amps[_dk]))
                        for _dk in _cog_drop[:8]) + (" …" if len(_cog_drop) > 8 else ""))
                if len(_cog_ks) > N_COG_BINS:
                    print("    Dropped ({}-bin cap, not uploaded): {}".format(
                        N_COG_BINS, list(_cog_ks[N_COG_BINS:])))

                print("\n  Uploading cogging compensation (0x3028) to node {} ...".format(node_id))
                print("  {:>4}  {:>6}  {:>10}  {:>8}  {:>8}".format(
                    "Bin", "k", "Amp(mA)", "a_s", "a_c"))
                print("  " + "-" * 44)

                self.node.sdo[0x3028][1].raw = 0  # disable while writing

                _n_written = 0
                for _bi in range(N_COG_BINS):
                    _as_sub = 2 + _bi * 3
                    _k_sub  = 3 + _bi * 3
                    _ac_sub = 4 + _bi * 3
                    if _bi < _n_cog:
                        _bk      = int(_top_cog[_bi])
                        _bA      = float(amps[_bk])
                        _bphi    = float(phases[_bk])
                        _A_s_val = _clamp_i16_cog(-_bA * math.sin(_bphi))
                        _A_c_val = _clamp_i16_cog(+_bA * math.cos(_bphi))
                        _k_val   = _bk
                        print("  {:>4d}  {:>6d}  {:>10.4f}  {:>8d}  {:>8d}".format(
                            _bi, _k_val, _bA, _A_s_val, _A_c_val))
                    else:
                        _A_s_val = _A_c_val = _k_val = 0
                    try:
                        self.node.sdo[0x3028][_as_sub].raw = _A_s_val
                        self.node.sdo[0x3028][_k_sub].raw  = _k_val
                        self.node.sdo[0x3028][_ac_sub].raw = _A_c_val
                        _n_written += 1
                    except SdoAbortedError as _bin_exc:
                        if _bin_exc.code == 0x06020000:
                            print("  NOTE: firmware supports {} bin(s) — "
                                  "stopping at bin {}.".format(_n_written, _bi))
                        else:
                            print("  WARNING: bin {} SDO abort 0x{:08X} — "
                                  "stopping upload.".format(_bi, _bin_exc.code))
                        break

                if _n_written == 0:
                    raise RuntimeError("No cogging bins could be written to 0x3028.")

                self.node.sdo[0x3028][1].raw = 1
                print("  Cogging Compensation Active → 1  ({} bin(s) written)".format(_n_written))
                try:
                    self.frame_menubar.COG_ON.Check(True)
                    self.frame_menubar.COG_OFF.Check(False)
                except Exception:
                    pass

                # Readback verification (only bins that were written)
                print("\n  Readback verification:")
                print("  {:>4}  {:>6}  {:>8}  {:>8}  {}".format(
                    "Bin", "k", "a_s", "a_c", "OK?"))
                print("  " + "-" * 40)
                _active_rb = self.node.sdo[0x3028][1].raw
                print("  Active flag readback: {}".format(_active_rb))
                _rb_ok = True
                for _bi in range(_n_written):
                    _as_rb = self.node.sdo[0x3028][2 + _bi * 3].raw
                    _k_rb  = self.node.sdo[0x3028][3 + _bi * 3].raw
                    _ac_rb = self.node.sdo[0x3028][4 + _bi * 3].raw
                    if _bi < _n_cog:
                        _bk_exp  = int(_top_cog[_bi])
                        _bA_exp  = float(amps[_bk_exp])
                        _bph_exp = float(phases[_bk_exp])
                        _as_exp  = _clamp_i16_cog(-_bA_exp * math.sin(_bph_exp))
                        _ac_exp  = _clamp_i16_cog(+_bA_exp * math.cos(_bph_exp))
                    else:
                        _bk_exp = _as_exp = _ac_exp = 0
                    _ok = (_as_rb == _as_exp and _k_rb == _bk_exp and _ac_rb == _ac_exp)
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

                print("\n  Saving 0x3028 to EEPROM ...")
                _save_subs = list(range(1, 2 + _n_written * 3))
                for _si in _save_subs:
                    self.node.sdo['Save']['Single'].raw = ((0x3028 << 8) | _si)
                print("  Saved.")
                _upload_ok = True

            except SdoAbortedError as _cog_sdo_exc:
                if _cog_sdo_exc.code == 0x06020000:
                    print("\n  WARNING: 0x3028 (CoggingCompensation) not found on "
                          "node {} — firmware does not support cogging upload.".format(node_id))
                else:
                    print("\n  WARNING: Cogging upload SDO abort "
                          "0x{:08X}: {}".format(_cog_sdo_exc.code, _cog_sdo_exc))
            except NameError as _ne:
                if str(_ne) == "sweep_rpm_too_high":
                    pass  # message already printed in the guard above
                else:
                    print("\n  Cogging upload skipped — FFT analysis did not complete.")
            except Exception as _cog_exc:
                print("\n  WARNING: Cogging upload failed: {}".format(_cog_exc))

            if STEPPED_MEASURE:
                print("\n  Stepped (quasi-static) measurement complete — comp uploaded"
                      + (" + saved." if _upload_ok else " (upload may have failed)."))

                # ── VALIDATION (A): harmonic-resolved velocity ripple, comp OFF vs ON ──
                # Spin at the cal speed and FFT the enc-comp-CORRECTED velocity vs angle.
                # Cogging-induced velocity ripple lives at k=pole_pairs and 2× — broadband
                # RMS was swamped by surge+noise, so we read ONLY those harmonics. A drop
                # ON-vs-OFF = cogging comp is reducing the cogging-frequency ripple.
                # Reboot before each spin so the velocity integrator starts from zero
                # (stale windup would let the OFF run "pre-cancel" and bias the comparison).
                try:
                    import numpy as _np_va
                    _va_lut = [0.0] * enc_resolution
                    try:
                        if int(self.node.sdo[0x3027][1].raw) == 1:
                            _vb_list = []
                            for _vbi in range(10):
                                _vas = int(self.node.sdo[0x3027][2 + _vbi * 3].raw)
                                _vk  = int(self.node.sdo[0x3027][3 + _vbi * 3].raw)
                                _vac = int(self.node.sdo[0x3027][4 + _vbi * 3].raw)
                                if (_vas | _vac) == 0: break
                                _vb_list.append((_vas, _vk, _vac))
                            for _p in range(enc_resolution):
                                _th = 2.0 * math.pi * _p / enc_resolution
                                _va_lut[_p] = -sum(
                                    _a * math.sin(_k * _th) + _c * math.cos(_k * _th)
                                    for _a, _k, _c in _vb_list) / 256.0
                    except Exception:
                        pass

                    # Metric: SAMPLE-DWELL ripple (no differentiation → no finite-difference
                    # quantization noise). samples-per-angle-bin ∝ time-in-bin ∝ 1/velocity,
                    # so the dwell histogram's cogging-harmonic content IS the velocity ripple
                    # (×mean velocity → deg/s). Derived from OUR position samples, so it is
                    # INDEPENDENT of the puck's (known-noisy) velocity-feedback estimate.
                    #
                    # TEST A: comp-ON at FF position shifts {0, ±24 cts}. A constant offset Δ
                    # between FF measurement and application rotates harmonic k by k·2πΔ/N, so
                    # k=42's error is 2× k=21's — matching "k=42 consistently worse". If a
                    # shift turns the increase into a reduction → offset is real and found. If
                    # NO shift helps → the FF source itself is noise-limited (velocity-feedback
                    # bug corrupting the measured profile) and phase-shifting can't save it.
                    SAMPLE_S  = 0.0125    # ~80 Hz (#2)
                    n_samples = 4000      # ~16 revs coherent averaging (#1), 4 spins
                    _mean_v   = vel_cts_per_sec / enc_resolution * 360.0   # deg/s, motor mech

                    _base_bins = []
                    for _bi in range(10):
                        _ba = int(self.node.sdo[0x3028][2 + _bi * 3].raw)
                        _bk = int(self.node.sdo[0x3028][3 + _bi * 3].raw)
                        _bc = int(self.node.sdo[0x3028][4 + _bi * 3].raw)
                        if (_ba | _bc) == 0: break
                        _base_bins.append((_ba, _bk, _bc))

                    def _shifted_bins(shift):
                        _out = []
                        for (_ba, _bk, _bc) in _base_bins:
                            _kd = 2.0 * math.pi * _bk * shift / enc_resolution
                            _cs, _sn = math.cos(_kd), math.sin(_kd)
                            _out.append((max(-32768, min(32767, int(round( _ba*_cs + _bc*_sn)))),
                                         _bk,
                                         max(-32768, min(32767, int(round(-_ba*_sn + _bc*_cs))))))
                        return _out

                    def _write_bins(bins):
                        for _i in range(10):
                            _na, _bk, _nc = bins[_i] if _i < len(bins) else (0, 0, 0)
                            self.node.sdo[0x3028][2 + _i * 3].raw = _na
                            self.node.sdo[0x3028][3 + _i * 3].raw = _bk
                            self.node.sdo[0x3028][4 + _i * 3].raw = _nc

                    def _cog_dwell(comp_on, label, shift_cts=None):
                        self.network.send_message(0x0, [0x81, int(node_id)])
                        _sleep_responsive(1.5)
                        self.configure_Puck(configure_pdos=False)
                        if comp_on and shift_cts:
                            try: _write_bins(_shifted_bins(shift_cts))
                            except Exception: pass
                        for _ in range(3):
                            try:
                                self.node.sdo[0x3028][1].raw = 1 if comp_on else 0
                                if int(self.node.sdo[0x3028][1].raw) == (1 if comp_on else 0):
                                    break
                            except Exception:
                                pass
                            time.sleep(0.1)
                        self.node.sdo["ControlWord"].raw = CLEAR_FAULT
                        self.node.sdo["ControlWord"].raw = SHUTDOWN
                        self.node.sdo["ControlWord"].raw = OP_ENABLED
                        self.node.sdo["SetModeOfOperation"].raw = MODE_PROFILE_VEL
                        print("  validation spin [{}] ...".format(label))
                        _s = _sample_pass(+vel_cts_per_sec, label, 5, 95)
                        self.node.sdo['TargetVelocity'].raw = 0
                        _sleep_responsive(1.0)
                        self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE
                        _cnt = [0] * N_BINS
                        for (_deg, _iq) in _s:
                            _raw  = int(round(_deg / 360.0 * enc_resolution)) % enc_resolution
                            _cpos = (_raw + _va_lut[_raw]) % enc_resolution
                            _cnt[int(_cpos) * N_BINS // enc_resolution] += 1
                        _m = sum(_cnt) / N_BINS
                        if _m <= 0:
                            return (0.0, 0.0)
                        _frac = [(_c - _m) / _m for _c in _cnt]
                        _av = 2.0 * _np_va.abs(_np_va.fft.rfft(_np_va.array(_frac))) / N_BINS
                        _k1, _k2 = pole_pairs, 2 * pole_pairs
                        return (float(_av[_k1]) * _mean_v if _k1 < len(_av) else 0.0,
                                float(_av[_k2]) * _mean_v if _k2 < len(_av) else 0.0)

                    _off = _cog_dwell(False, 'comp OFF')
                    _off_mag = (_off[0] ** 2 + _off[1] ** 2) ** 0.5
                    _rows = []; _best = None
                    for _sh in [0, -24, 24]:
                        _r = _cog_dwell(True, 'comp ON shift {:+d}'.format(_sh), shift_cts=_sh)
                        _mag = (_r[0] ** 2 + _r[1] ** 2) ** 0.5
                        _rows.append((_sh, _r[0], _r[1], _mag))
                        if _best is None or _mag < _best[3]:
                            _best = (_sh, _r[0], _r[1], _mag)
                    try:   # leave the best FF active + saved
                        _write_bins(_shifted_bins(_best[0]))
                        self.node.sdo[0x3028][1].raw = 1
                        for _si in range(1, 2 + max(1, len(_base_bins)) * 3):
                            self.node.sdo['Save']['Single'].raw = ((0x3028 << 8) | _si)
                    except Exception:
                        pass
                    print("\n  TEST A — FF position-shift validation (dwell metric, deg/s):")
                    print("    comp OFF baseline:  k={}={:.2f}  k={}={:.2f}  (mag {:.2f})".format(
                        pole_pairs, _off[0], 2 * pole_pairs, _off[1], _off_mag))
                    for (_sh, _r1, _r2, _mag) in _rows:
                        _pc  = (_mag - _off_mag) / _off_mag * 100.0 if _off_mag else 0.0
                        _tag = "  <-- best" if _best and _sh == _best[0] else ""
                        print("    shift {:>+4d}:  k={}={:6.2f}  k={}={:6.2f}  mag={:6.2f}  ({:+.0f}% vs OFF){}".format(
                            _sh, pole_pairs, _r1, 2 * pole_pairs, _r2, _mag, _pc, _tag))
                    if _best:
                        _bpc = (_best[3] - _off_mag) / _off_mag * 100.0 if _off_mag else 0.0
                        print("    BEST shift {:+d} → {:+.0f}% vs comp-OFF (negative = improvement). Saved.".format(
                            _best[0], _bpc))
                except Exception as _va_exc:
                    print("\n  Validation skipped (error): {}".format(_va_exc))

                try:
                    self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE
                except Exception:
                    pass
                print("\nCogging characterisation + validation complete (stepped).")
                return   # `finally` below still restores ADC / re-enables the GUI

            # ── Retest sweep with compensation active ────────────────────────
            if _upload_ok:
                print("\n  Retest sweep (compensation active) ...")
                try:
                    # Reboot to zero the velocity PI integrator.  Without this,
                    # the integrator wound up during the "before" sweep provides
                    # ~T_cog/Kt on its own; adding the feedforward on top gives
                    # ~2× cogging correction and makes velocity ripple WORSE, not
                    # better.  Comp is saved to EEPROM so it survives the reboot.
                    print("  Rebooting node {} to zero velocity integrator before retest...".format(node_id))
                    self.network.send_message(0x0, [0x81, int(node_id)])
                    _sleep_responsive(1.5)
                    self.configure_Puck(configure_pdos=False)
                    # Compensation was saved to EEPROM — verify it is still active.
                    _rt_active = int(self.node.sdo[0x3028][1].raw)
                    if not _rt_active:
                        print("  WARNING: compensation not active after reboot — "
                              "EEPROM save may have failed.  Retest may be unreliable.")

                    self.node.sdo["ControlWord"].raw = CLEAR_FAULT
                    self.node.sdo["ControlWord"].raw = SHUTDOWN
                    self.node.sdo["ControlWord"].raw = OP_ENABLED
                    self.node.sdo["SetModeOfOperation"].raw = MODE_PROFILE_VEL
                    time.sleep(0.2)
                    wx.Yield()

                    _retest_settle = SETTLE_S
                    samples_fwd_rt = _sample_pass(+vel_cts_per_sec, 'retest fwd', 94, 97,
                                                  settle_s=_retest_settle)
                    self.node.sdo['TargetVelocity'].raw = 0
                    _sleep_responsive(1.0)
                    wx.Yield()
                    samples_rev_rt = _sample_pass(-vel_cts_per_sec, 'retest rev', 97, 99,
                                                  settle_s=_retest_settle)
                    self.node.sdo['TargetVelocity'].raw = 0
                    _sleep_responsive(1.0)
                    self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE
                    wx.Yield()

                    fwd_bins_rt = [[] for _ in range(N_BINS)]
                    rev_bins_rt = [[] for _ in range(N_BINS)]
                    for _rdeg, _riq in samples_fwd_rt:
                        fwd_bins_rt[int(_rdeg / bin_width_deg) % N_BINS].append(_riq)
                    for _rdeg, _riq in samples_rev_rt:
                        rev_bins_rt[int(_rdeg / bin_width_deg) % N_BINS].append(_riq)
                    avg_fwd_rt = [sum(b) / len(b) if b else 0.0 for b in fwd_bins_rt]
                    avg_rev_rt = [sum(b) / len(b) if b else 0.0 for b in rev_bins_rt]
                    avg_iq_rt  = [(avg_fwd_rt[b] + avg_rev_rt[b]) / 2.0
                                  for b in range(N_BINS)]

                    dc_rt    = sum(avg_iq_rt) / len(avg_iq_rt)
                    iq_ac_rt = [v - dc_rt for v in avg_iq_rt]
                    rms_rt   = (sum(v * v for v in iq_ac_rt) / len(iq_ac_rt)) ** 0.5
                    rms_diff = (rms_rt - rms_iq_ma) / rms_iq_ma * 100.0 if rms_iq_ma > 0 else 0.0

                    # Velocity ripple from consecutive angle samples (wrap-aware).
                    # At constant command velocity, cogging appears as angle-periodic
                    # velocity variation; feedforward reduces this even when Iq RMS is
                    # insensitive (PI shifts who provides the cogging current, not how
                    # much is provided, so Iq RMS is a weak metric for feedforward
                    # effectiveness in velocity mode).
                    def _vel_ac(samps, is_fwd):
                        """AC velocity series (deg/s) from consecutive angle samples."""
                        vels = []
                        for _vi in range(1, len(samps)):
                            _d = samps[_vi][0] - samps[_vi - 1][0]
                            if is_fwd and _d < -180.0:
                                _d += 360.0
                            elif not is_fwd and _d > 180.0:
                                _d -= 360.0
                            vels.append(_d / SAMPLE_S)
                        if not vels:
                            return []
                        _mv = sum(vels) / len(vels)
                        return [_v - _mv for _v in vels]

                    # ── PRIMARY metric: velocity ripple from enc-comp-CORRECTED position ──
                    # Raw-encoder velocity ripple is dominated by encoder non-uniformity and
                    # can't see cogging. Apply the active 0x3027 correction (same math as
                    # firmware correct_pos: corr(raw) = -Σ[a_s·sin(kθ)+a_c·cos(kθ)]/256,
                    # θ=2π·raw/N) to every sample so the ripple reflects TRUE rotor velocity.
                    # The identical correction is applied to before AND after, so the delta
                    # isolates cogging comp.
                    _enc_corr_lut = [0.0] * enc_resolution
                    try:
                        if int(self.node.sdo[0x3027][1].raw) == 1:   # enc comp active
                            _ec_bins = []
                            for _eb in range(10):
                                _eas = int(self.node.sdo[0x3027][2 + _eb * 3].raw)
                                _ek  = int(self.node.sdo[0x3027][3 + _eb * 3].raw)
                                _eac = int(self.node.sdo[0x3027][4 + _eb * 3].raw)
                                if (_eas | _eac) == 0:
                                    break
                                _ec_bins.append((_eas, _ek, _eac))
                            for _p in range(enc_resolution):
                                _th = 2.0 * math.pi * _p / enc_resolution
                                _cc = 0.0
                                for _eas, _ek, _eac in _ec_bins:
                                    _cc += _eas * math.sin(_ek * _th) + _eac * math.cos(_ek * _th)
                                _enc_corr_lut[_p] = -_cc / 256.0
                    except Exception:
                        pass  # no/failed enc-comp read → LUT stays zero → falls back to raw
                    _enc_comp_on = any(_v != 0.0 for _v in _enc_corr_lut)

                    def _vel_ac_corr(samps, is_fwd):
                        """AC velocity (deg/s) from enc-comp-corrected position."""
                        _cd = []
                        for _s in samps:
                            _raw = int(round(_s[0] / 360.0 * enc_resolution)) % enc_resolution
                            _cd.append(_s[0] + _enc_corr_lut[_raw] / enc_resolution * 360.0)
                        _vs = []
                        for _vi in range(1, len(_cd)):
                            _d = _cd[_vi] - _cd[_vi - 1]
                            if is_fwd and _d < -180.0: _d += 360.0
                            elif not is_fwd and _d > 180.0: _d -= 360.0
                            _vs.append(_d / SAMPLE_S)
                        if not _vs:
                            return []
                        _mv = sum(_vs) / len(_vs)
                        return [_v - _mv for _v in _vs]

                    _vac_b = _vel_ac(samples_fwd,    is_fwd=True)
                    _vac_a = _vel_ac(samples_fwd_rt, is_fwd=True)
                    vel_rms_b = (sum(_v*_v for _v in _vac_b) / len(_vac_b)) ** 0.5 if _vac_b else 0.0
                    vel_rms_a = (sum(_v*_v for _v in _vac_a) / len(_vac_a)) ** 0.5 if _vac_a else 0.0
                    vel_diff  = (vel_rms_a - vel_rms_b) / vel_rms_b * 100.0 if vel_rms_b > 0 else 0.0

                    _vac_b_c = _vel_ac_corr(samples_fwd,    is_fwd=True)
                    _vac_a_c = _vel_ac_corr(samples_fwd_rt, is_fwd=True)
                    vel_rms_b_c = (sum(_v*_v for _v in _vac_b_c) / len(_vac_b_c)) ** 0.5 if _vac_b_c else 0.0
                    vel_rms_a_c = (sum(_v*_v for _v in _vac_a_c) / len(_vac_a_c)) ** 0.5 if _vac_a_c else 0.0
                    vel_diff_c  = (vel_rms_a_c - vel_rms_b_c) / vel_rms_b_c * 100.0 if vel_rms_b_c > 0 else 0.0

                    # Harmonic comparison: compare dominant cogging harmonics before vs after.
                    # This is the real effectiveness metric — compensation reduces the
                    # PI's job at cogging frequencies so the dominant harmonics in the
                    # Iq profile should decrease.  (Overall Iq AC RMS often INCREASES
                    # when comp is active because the feedforward injects current at the
                    # same frequencies; that is expected and does not mean comp failed.)
                    _harm_cmp = []
                    try:
                        import numpy as _np_rt
                        _iq_rt_arr  = _np_rt.array(avg_iq_rt, dtype=_np_rt.float64)
                        _X_rt       = _np_rt.fft.rfft(_iq_rt_arr)
                        _N_rt       = len(avg_iq_rt)
                        _amps_rt    = 2.0 * _np_rt.abs(_X_rt) / _N_rt
                        # Compare each uploaded harmonic's amplitude before vs after.
                        for _bk_rt in _top_cog[:_n_cog]:
                            _bk_rt = int(_bk_rt)
                            if _bk_rt < len(amps) and _bk_rt < len(_amps_rt):
                                _harm_cmp.append((_bk_rt,
                                                  float(amps[_bk_rt]),
                                                  float(_amps_rt[_bk_rt])))
                    except Exception:
                        pass

                    print("\n  Retest results:")
                    print("  AC RMS Iq:       before={:.2f} mA     after={:.2f} mA     ({:+.1f}%)".format(
                        rms_iq_ma, rms_rt, rms_diff))
                    print("  NOTE: Iq AC RMS increases when comp is active (feedforward injects")
                    print("        current at cogging frequencies — this is expected, not a failure).")
                    if _enc_comp_on:
                        print("  Velocity ripple [enc-comp CORRECTED — PRIMARY metric]:")
                        print("        before={:.2f} deg/s  after={:.2f} deg/s  ({:+.1f}%)".format(
                            vel_rms_b_c, vel_rms_a_c, vel_diff_c))
                        print("        (encoder non-uniformity removed via active 0x3027; a real")
                        print("         drop here = cogging comp working. Negative % = improvement.)")
                        print("  Velocity ripple [raw encoder, reference only]:")
                        print("        before={:.2f} deg/s  after={:.2f} deg/s  ({:+.1f}%)".format(
                            vel_rms_b, vel_rms_a, vel_diff))
                    else:
                        print("  Velocity ripple: before={:.2f} deg/s  after={:.2f} deg/s  ({:+.1f}%)".format(
                            vel_rms_b, vel_rms_a, vel_diff))
                        print("  NOTE: enc comp not active — raw-encoder velocity ripple is dominated")
                        print("        by encoder non-uniformity and is NOT a reliable cogging metric.")
                    if _harm_cmp:
                        print("  Cogging harmonic amplitudes (primary metric):")
                        _harm_pass = True
                        for _hk, _hb, _ha in _harm_cmp:
                            _hdiff = (_ha - _hb) / _hb * 100.0 if _hb > 0 else 0.0
                            _hok   = _ha <= _hb
                            if not _hok:
                                _harm_pass = False
                            print("    k={:3d}:  before={:.2f} mA  after={:.2f} mA  ({:+.1f}%)  {}".format(
                                _hk, _hb, _ha, _hdiff, "OK" if _hok else "INCREASED"))
                        print("  Harmonic result: {}".format(
                            "PASS — cogging harmonics reduced" if _harm_pass
                            else "PARTIAL — some harmonics did not reduce (check phase)"))
                    else:
                        print("  (Harmonic comparison unavailable — numpy not installed)")

                    try:
                        import matplotlib
                        matplotlib.use('Agg')
                        import matplotlib.pyplot as _plt_rt

                        _yr_cog = max(max(abs(v) for v in iq_ac),
                                      max(abs(v) for v in iq_ac_rt)) * 1.15 or 1.0
                        _yr_vel = max(max(abs(v) for v in _vac_b) if _vac_b else 1.0,
                                      max(abs(v) for v in _vac_a) if _vac_a else 1.0) * 1.15 or 1.0

                        _fig_rt, _axes_rt = _plt_rt.subplots(2, 2, figsize=(14, 10))
                        _fig_rt.suptitle(
                            'Cogging Compensation — Node {}  {}  ({})\n'
                            'Iq RMS: {:.2f} → {:.2f} mA  ({:+.1f}%)  [increase expected — feedforward]    '
                            'Velocity ripple: {:.2f} → {:.2f} deg/s  ({:+.1f}%)'.format(
                                node_id, model_str, ts,
                                rms_iq_ma, rms_rt, rms_diff,
                                vel_rms_b, vel_rms_a, vel_diff),
                            fontsize=9)

                        _cax_b = _axes_rt[0, 0]
                        _cax_b.plot(bin_deg, iq_ac, 'b-', linewidth=1.0)
                        _cax_b.axhline(0, color='k', linewidth=0.8, linestyle='--')
                        _cax_b.set_ylim(-_yr_cog, _yr_cog)
                        _cax_b.set_xlabel('Mechanical angle (°)')
                        _cax_b.set_ylabel('Iq AC (mA)')
                        _cax_b.set_title('Iq — before  (RMS={:.2f} mA)'.format(rms_iq_ma))
                        _cax_b.grid(True, alpha=0.3)

                        _cax_a = _axes_rt[0, 1]
                        _cax_a.plot(bin_deg, iq_ac_rt, 'g-', linewidth=1.0)
                        _cax_a.axhline(0, color='k', linewidth=0.8, linestyle='--')
                        _cax_a.set_ylim(-_yr_cog, _yr_cog)
                        _cax_a.set_xlabel('Mechanical angle (°)')
                        _cax_a.set_title('[COMP ACTIVE]  Iq — after  (RMS={:.2f} mA  {:+.1f}%)\n'
                                         'Iq increase expected — feedforward injects at cogging freqs'.format(
                            rms_rt, rms_diff))
                        _cax_a.grid(True, alpha=0.3)

                        _t_b = [_vi * SAMPLE_S for _vi in range(len(_vac_b))]
                        _t_a = [_vi * SAMPLE_S for _vi in range(len(_vac_a))]

                        _vax_b = _axes_rt[1, 0]
                        _vax_b.plot(_t_b, _vac_b, 'b-', linewidth=0.8)
                        _vax_b.axhline(0, color='k', linewidth=0.8, linestyle='--')
                        _vax_b.set_ylim(-_yr_vel, _yr_vel)
                        _vax_b.set_xlabel('Time (s)')
                        _vax_b.set_ylabel('Velocity AC (deg/s)')
                        _vax_b.set_title('Velocity ripple — before  (RMS={:.2f} deg/s)'.format(vel_rms_b))
                        _vax_b.grid(True, alpha=0.3)

                        _vax_a = _axes_rt[1, 1]
                        _vax_a.plot(_t_a, _vac_a, 'g-', linewidth=0.8)
                        _vax_a.axhline(0, color='k', linewidth=0.8, linestyle='--')
                        _vax_a.set_ylim(-_yr_vel, _yr_vel)
                        _vax_a.set_xlabel('Time (s)')
                        _vax_a.set_title('Velocity ripple — after  (RMS={:.2f} deg/s  {:+.1f}%)'.format(
                            vel_rms_a, vel_diff))
                        _vax_a.grid(True, alpha=0.3)

                        _plt_rt.tight_layout()
                        _rt_plot_path = _cog_img(
                            '{}cogging_retest_{}.png'.format(_file_pfx, ts))
                        _plt_rt.savefig(_rt_plot_path, dpi=100)
                        _plt_rt.close(_fig_rt)
                        print("  Before/after plot → {}".format(_rt_plot_path))
                    except ImportError:
                        pass
                    except Exception as _rt_plt_exc:
                        print("  WARNING: Retest plot failed: {}".format(_rt_plt_exc))

                except Exception as _rt_exc:
                    print("  WARNING: Retest sweep failed: {}".format(_rt_exc))

            _upd(100)
            print("\nCogging characterisation complete.")

        except Exception as _exc:
            self._cal_fault(_exc)
        finally:
            self.OnTaskComplete()
            self.choice_test.SetSelection(0)
            try:
                self.node.nmt.state = 'PRE-OPERATIONAL'
                time.sleep(0.1)
                self.configure_Puck()
                self.node.nmt.state = 'OPERATIONAL'
                time.sleep(0.1)
            except Exception:
                pass
            if self.ADC_ON == False and self.adcWasON:
                self.on_off_adc(self)
            self.Enable()

    def cogging_position_sweep(self, event, calAll=False, _upd=None, fast=False):
        """
        Cogging characterisation via bidirectional position hold.

        Alternative to cogging_error_compensation for motors that stall at low
        RPM (high cogging/friction ratio, e.g. high-gear-ratio actuators).

        Holds the motor at each of 128 mechanical positions using a software
        P-controller running over velocity-mode SDO commands at ~100 Hz.
        Approaches each position from CW and CCW directions; stiction cancels
        in the average exactly as with the bidirectional velocity sweep.

        No speed dependency → no stall, no velocity-PI phase distortion, no
        gear-resonance excitation.  Measurement is quasi-static (DC), which is
        the correct reference for a position-dependent feedforward.

        Outputs, upload, and retest are identical to cogging_error_compensation.
        """
        if not self.check_for_node():
            return
        if not self._fw_at_least(4, 4, 0):
            self._prompt_ok("Firmware Too Old",
                "Cogging compensation calibration requires firmware v4.4.0 or later.\n"
                "Please update the firmware and try again.")
            return

        if self.ADC_ON:
            self.adcWasON = True
            self.on_off_adc(self)
        else:
            self.adcWasON = False

        if calAll:
            if _upd is None: _upd = lambda v: None
        else:
            self.OnStartTask(None)
            _upd = lambda v: self.UpdateUI(v)
        if _upd is None: _upd = lambda v: None

        self.Disable()
        self.frame_statusbar.SetStatusText("Cogging position-hold sweep...", 1)
        self.frame_statusbar.Update()
        wx.Yield()

        try:
            import cmath as _cm
            import datetime, os
            from paths import resource_path, session_path
            ts = datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')

            enc_resolution = self.node.sdo['EncoderConfig']['Resolution'].raw
            motor_poles    = self.node.sdo['Calibration']['poles'].raw
            i_peak         = self.node.sdo['Calibration']['i_peak'].raw
            kt             = self.node.sdo['Calibration']['kt'].raw
            pole_pairs     = motor_poles // 2
            cts_per_elec   = enc_resolution * 2.0 / motor_poles

            _alpha_bias   = self.node.sdo[0x3008][3].raw / 16.0  # Q12.4 → Q12.0 ADC counts
            _beta_bias    = self.node.sdo[0x3009][3].raw / 16.0
            _alpha_gf     = self.node.sdo[0x3008][6].raw
            _beta_gf      = self.node.sdo[0x3009][6].raw
            _isense_shunt = self.node.sdo[0x3008][5].raw
            _isense_gain  = self.node.sdo[0x3008][4].raw
            _e_zero       = self.node.sdo['Calibration']['e_zero'].raw
            _e_polarity   = int(self.node.sdo['Calibration']['e_polarity'].raw)
            _cts_per_elec = enc_resolution // pole_pairs
            _ma_per_ct    = (3.3 / 4096.0 * 1000.0 / _isense_shunt
                             * 1000.0 / _isense_gain * 1000.0)

            node_id   = getattr(self.node, 'id', '?')
            _pc       = None
            try: _pc = int(self.node.sdo[0x1018][2].raw)
            except Exception: pass
            model_str = getattr(self, '_PRODUCT_CODE_MODELS', {}).get(_pc, 'unknown')
            _file_pfx = 'node{}_{}_'.format(node_id, model_str.replace(' ', '_'))

            def _cog_img(name):
                p = session_path('cogging/images/{}'.format(name))
                os.makedirs(os.path.dirname(p), exist_ok=True)
                return p
            def _cog_data(name):
                p = session_path('cogging/data/{}'.format(name))
                os.makedirs(os.path.dirname(p), exist_ok=True)
                return p

            # fast=True halves bins and steps for quick debug iterations (~1.5 min vs ~5 min).
            # Applies equally to calibration and retest passes.
            N_BINS      = 64  if fast else 128   # k_max=32/64; k=7 well-resolved either way
            N_HARMONICS = 16
            N_COG_BINS  = 10
            UPDATE_S    = 0.005   # position controller period (200 Hz over SDO)
            POS_KP      = 80      # P gain: cts/s per count error (stable @ 200 Hz)
            MAX_VEL     = 2048    # velocity limit during hold (30 RPM)
            HOLD_STEPS  = 24 if fast else 40     # steps per bin (fast: 0.12 s, normal: 0.2 s)
            SAMPLE_FROM = 8  if fast else 16     # settle steps before sampling

            bin_cts = enc_resolution // N_BINS   # 32 counts/bin for 4096-count encoder
            bin_deg = [b * bin_cts / enc_resolution * 360.0 for b in range(N_BINS)]

            print("\nCogging characterisation (position hold) — parameters")
            print("  {} pole pairs  {:.2f} cts/elec  enc_res={}"
                  "  i_peak={} mA  Kt={} mNm/A".format(
                      pole_pairs, cts_per_elec, enc_resolution, i_peak, kt))
            print("  {} bins  {} steps/bin ({:.1f}s hold, {:.1f}s sample)".format(
                N_BINS, HOLD_STEPS,
                HOLD_STEPS * UPDATE_S,
                (HOLD_STEPS - SAMPLE_FROM) * UPDATE_S))
            est_min = 2 * N_BINS * HOLD_STEPS * UPDATE_S / 60.0
            print("  ~{:.0f} min total  (2 passes × {} bins × {:.1f}s)".format(
                est_min, N_BINS, HOLD_STEPS * UPDATE_S))
            _upd(2)

            # ── Disable cogging compensation before sweep ──────────────────────
            _cog_was_active = False
            for _dis in range(3):
                try:
                    _cog_was_active = bool(self.node.sdo[0x3028][1].raw)
                    self.node.sdo[0x3028][1].raw = 0
                    if int(self.node.sdo[0x3028][1].raw) == 0: break
                except Exception: pass
                time.sleep(0.1)
            else:
                raise RuntimeError("Could not disable cogging compensation before sweep.")
            if _cog_was_active:
                print("  NOTE: Cogging compensation was active — disabled for sweep.")

            # ── Reboot to clear velocity integrator ────────────────────────────
            print("  Rebooting node {} to clear velocity integrator state...".format(node_id))
            self.network.send_message(0x0, [0x81, int(node_id)])
            _sleep_responsive(1.5)
            self.configure_Puck(configure_pdos=False)
            for _dis in range(3):
                try:
                    self.node.sdo[0x3028][1].raw = 0
                    if int(self.node.sdo[0x3028][1].raw) == 0: break
                except Exception: pass
                time.sleep(0.1)

            # ── Enable velocity mode ───────────────────────────────────────────
            self.node.sdo["ControlWord"].raw        = CLEAR_FAULT
            self.node.sdo["ControlWord"].raw        = SHUTDOWN
            self.node.sdo["ControlWord"].raw        = OP_ENABLED
            self.node.sdo["SetModeOfOperation"].raw = MODE_PROFILE_VEL
            time.sleep(0.2)
            wx.Yield()

            # ── Detect velocity-to-raw-position sign ───────────────────────────
            # Positive TargetVelocity may increase OR decrease RawPosition
            # depending on u_polarity × e_polarity.  Detect empirically.
            _r0 = int(self.node.sdo["Encoder"]["RawPosition"].raw)
            self.node.sdo["TargetVelocity"].raw = 256
            time.sleep(0.15)
            _r1 = int(self.node.sdo["Encoder"]["RawPosition"].raw)
            self.node.sdo["TargetVelocity"].raw = 0
            time.sleep(0.1)
            _dr = (_r1 - _r0 + enc_resolution) % enc_resolution
            if _dr > enc_resolution // 2: _dr -= enc_resolution
            _vel_sign = 1 if _dr >= 0 else -1
            print("  Velocity sign: {} (positive vel {} raw encoder)".format(
                _vel_sign, "increases" if _vel_sign > 0 else "decreases"))

            # ── Position hold kernel ───────────────────────────────────────────
            def _hold_and_measure(target_raw, n_steps, sample_from):
                """
                P-controller: hold motor at target_raw for n_steps.
                Returns (mean_iq_mA, mean_actual_raw) from steps >= sample_from.
                mean_actual_raw is the encoder position actually achieved —
                may differ from target_raw when the motor slips to a cogging detent.
                """
                samples = []
                actual_raws = []
                for _step in range(n_steps):
                    raw_now = int(self.node.sdo["Encoder"]["RawPosition"].raw)
                    err = (target_raw - raw_now + enc_resolution) % enc_resolution
                    if err > enc_resolution // 2: err -= enc_resolution
                    vel = max(-MAX_VEL, min(MAX_VEL,
                                            int(POS_KP * err * _vel_sign)))
                    self.node.sdo["TargetVelocity"].raw = vel
                    time.sleep(UPDATE_S)

                    if _step >= sample_from:
                        alpha_f = self.node.sdo[0x3008][2].raw
                        beta_f  = self.node.sdo[0x3009][2].raw
                        enc_c   = self.node.sdo[0x3012][2].raw
                        a_mA = ((_alpha_bias - alpha_f / 16.0)
                                * (_alpha_gf / 4096.0) * _ma_per_ct)
                        b_mA = ((_beta_bias  - beta_f  / 16.0)
                                * (_beta_gf  / 4096.0) * _ma_per_ct)
                        th_m  = _e_polarity * (enc_c - _e_zero)
                        th_ct = (enc_resolution + th_m) % _cts_per_elec
                        if th_ct >= _cts_per_elec // 2: th_ct -= _cts_per_elec
                        th_e  = th_ct / _cts_per_elec * 2.0 * math.pi
                        samples.append(-a_mA * math.sin(th_e)
                                       + b_mA * math.cos(th_e))
                        actual_raws.append(raw_now)
                    wx.Yield()
                iq_mean  = sum(samples)     / len(samples)     if samples     else 0.0
                pos_mean = sum(actual_raws) / len(actual_raws) if actual_raws else float(target_raw)
                return iq_mean, pos_mean

            def _fill_bins(bins_list):
                """Average each bin's samples; linearly interpolate empty bins.
                Empty bins occur when the motor slipped to a stable cogging detent
                and bypassed the unstable target position entirely."""
                n    = len(bins_list)
                raw  = [sum(b) / len(b) if b else None for b in bins_list]
                filled = list(raw)
                for _i in range(n):
                    if filled[_i] is not None:
                        continue
                    prev_d = next((d for d in range(1, n)
                                   if raw[(_i - d) % n] is not None), None)
                    next_d = next((d for d in range(1, n)
                                   if raw[(_i + d) % n] is not None), None)
                    if prev_d is not None and next_d is not None:
                        p_val = raw[(_i - prev_d) % n]
                        n_val = raw[(_i + next_d) % n]
                        filled[_i] = p_val + (n_val - p_val) * prev_d / (prev_d + next_d)
                    elif prev_d is not None:
                        filled[_i] = raw[(_i - prev_d) % n]
                    elif next_d is not None:
                        filled[_i] = raw[(_i + next_d) % n]
                    else:
                        filled[_i] = 0.0
                return filled

            # ── CW pass: bins 0 → 127 (motor moves in + direction) ────────────
            print("\nCogging position sweep (CW pass, bins 0→127) ...")
            fwd_bins = [[] for _ in range(N_BINS)]
            for _b in range(N_BINS):
                iq, actual_pos = _hold_and_measure(_b * bin_cts, HOLD_STEPS, SAMPLE_FROM)
                actual_bin = int(round(actual_pos)) % enc_resolution // bin_cts % N_BINS
                fwd_bins[actual_bin].append(iq)
                if _b % 16 == 15 or _b == 0:
                    print("  bin {:>3d}/{} ...".format(_b + 1, N_BINS))
                _upd(3 + _b * 38 // N_BINS)

            # ── CCW pass: bins 127 → 0 (motor moves in − direction) ───────────
            print("Cogging position sweep (CCW pass, bins 127→0) ...")
            rev_bins = [[] for _ in range(N_BINS)]
            for _b in range(N_BINS - 1, -1, -1):
                iq, actual_pos = _hold_and_measure(_b * bin_cts, HOLD_STEPS, SAMPLE_FROM)
                actual_bin = int(round(actual_pos)) % enc_resolution // bin_cts % N_BINS
                rev_bins[actual_bin].append(iq)
                if (N_BINS - 1 - _b) % 16 == 15 or _b == N_BINS - 1:
                    print("  bin {:>3d}/{} ...".format(N_BINS - _b, N_BINS))
                _upd(41 + (N_BINS - 1 - _b) * 38 // N_BINS)

            self.node.sdo["TargetVelocity"].raw = 0
            _sleep_responsive(0.5)
            self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE
            wx.Yield()

            n_empty_fwd = sum(1 for b in fwd_bins if not b)
            n_empty_rev = sum(1 for b in rev_bins if not b)
            if n_empty_fwd or n_empty_rev:
                print("  NOTE: {}/{} fwd and {}/{} rev bins empty "
                      "(motor slipped to detents; interpolated).".format(
                          n_empty_fwd, N_BINS, n_empty_rev, N_BINS))
            fwd_iq = _fill_bins(fwd_bins)
            rev_iq = _fill_bins(rev_bins)
            avg_iq = [(fwd_iq[_b] + rev_iq[_b]) / 2.0 for _b in range(N_BINS)]

            dc_offset_ma = sum(avg_iq) / len(avg_iq)
            iq_ac        = [v - dc_offset_ma for v in avg_iq]
            rms_iq_ma    = (sum(v * v for v in iq_ac) / len(iq_ac)) ** 0.5
            max_iq_ma    = max(abs(v) for v in iq_ac)
            rms_cog_mnm  = kt * rms_iq_ma / 1000.0
            max_cog_mnm  = kt * max_iq_ma / 1000.0

            print("\n  Position-hold cogging profile stats:")
            print("  DC offset: {:+.2f} mA  (net load bias)".format(dc_offset_ma))
            print("  AC RMS Iq: {:.2f} mA  →  RMS cogging {:.2f} mNm".format(
                rms_iq_ma, rms_cog_mnm))
            print("  AC peak Iq: {:.2f} mA  →  peak cogging {:.2f} mNm".format(
                max_iq_ma, max_cog_mnm))

            # ── Fit Fourier series (for CSV / plots) ───────────────────────────
            def _dft(samples, n_harm):
                N, X = len(samples), []
                for k in range(n_harm + 1):
                    wk  = _cm.exp(-2j * math.pi * k / N)
                    val = 0.0 + 0j; w = 1.0 + 0j
                    for c in samples:
                        val += c * w; w *= wk
                    X.append(val / N)
                return X

            def _reconstruct_f(X, out_size):
                table = []
                for p in range(out_size):
                    val = X[0].real
                    for k in range(1, len(X)):
                        a = 2.0 * math.pi * k * p / out_size
                        val += 2.0 * (X[k].real * math.cos(a)
                                      - X[k].imag * math.sin(a))
                    table.append(val)
                return table

            X_iq   = _dft(avg_iq, N_HARMONICS)
            fit_iq = _reconstruct_f(X_iq, N_BINS)

            # ── CSV ────────────────────────────────────────────────────────────
            csv_path = _cog_data('{}cogging_pos_sweep_{}.csv'.format(_file_pfx, ts))
            with open(csv_path, 'w') as _f:
                _f.write("# Cogging position-hold sweep — Node {}  {}  ({})\n".format(
                    node_id, model_str, ts))
                _f.write("# DC offset: {:+.3f} mA\n".format(dc_offset_ma))
                _f.write("angle_deg,iq_fwd_mA,iq_rev_mA,iq_avg_mA,iq_fit_mA\n")
                for _b in range(N_BINS):
                    _f.write("{:.3f},{:.4f},{:.4f},{:.4f},{:.4f}\n".format(
                        bin_deg[_b], fwd_iq[_b], rev_iq[_b],
                        avg_iq[_b], fit_iq[_b]))
            print("\n  CSV → {}".format(csv_path))

            # ── FFT + upload (numpy) ───────────────────────────────────────────
            _upload_ok = False
            try:
                import numpy as _np
                import json as _json

                tf      = _np.array(avg_iq, dtype=_np.float64)
                X_np    = _np.fft.rfft(tf)
                N_fft   = len(avg_iq)
                amps    = 2.0 * _np.abs(X_np) / N_fft
                phases  = _np.angle(X_np)
                amps[0] /= 2.0; amps[-1] /= 2.0

                order  = 1 + _np.argsort(amps[1:])[::-1]
                top_n  = min(40, len(order))

                print("\n  FFT harmonic analysis  (N={})".format(N_fft))
                print("  {:>4s}  {:>10s}  {:>10s}  {:>12s}  {:>12s}".format(
                    "k", "Amp(mA)", "Phase(rad)", "cos coeff", "sin coeff"))
                print("  " + "-" * 54)

                harmonic_list = []
                for _ki in range(top_n):
                    k   = int(order[_ki])
                    A   = float(amps[k])
                    phi = float(phases[k])
                    a_k = A * _np.cos(phi)
                    b_k = -A * _np.sin(phi)
                    print("  {:>4d}  {:>10.4f}  {:>10.5f}  {:>12.4f}  {:>12.4f}".format(
                        k, A, phi, float(a_k), float(b_k)))
                    harmonic_list.append({
                        "k": k, "cycles_per_rev": k,
                        "amplitude_mA": A, "amplitude_mNm": float(kt * A / 1000.0),
                        "phase_rad": phi, "cos_coeff": float(a_k), "sin_coeff": float(b_k),
                    })

                sorted_ks = [int(order[i]) for i in range(len(order))]

                # Noise floor estimate for position hold.
                # Each of N_BINS positions is measured independently with n_per_bin
                # samples; the FFT harmonic amplitude noise is:
                #   σ_harm = σ_bin / sqrt(N_BINS/2) = (ma_per_ct/sqrt(n)) / sqrt(N/2)
                #          = ma_per_ct / sqrt(n * N/2) = ma_per_ct / sqrt(n * N / 2)
                # Equivalently: use the velocity-sweep formula with total samples =
                # n_per_bin × N_BINS (each bin measured n_per_bin times independently).
                _n_per_bin = max(HOLD_STEPS - SAMPLE_FROM, 1)
                _noise_floor_ma = _ma_per_ct / math.sqrt(
                    3.0 * _n_per_bin * N_BINS / max(N_BINS // 2, 1))
                _dom_k        = next((k for k in sorted_ks if k > 0 and k <= N_BINS // 2), 0)
                _dom_phys_amp = float(amps[_dom_k]) if _dom_k else 0.0
                print("\n  Per-harmonic noise floor: {:.2f} mA  "
                      "dominant harmonic k={} at {:.2f} mA  "
                      "(SNR {:.1f}×)".format(
                          _noise_floor_ma, _dom_k, _dom_phys_amp,
                          _dom_phys_amp / max(_noise_floor_ma, 1e-9)))

                if _dom_phys_amp < 2.0 * _noise_floor_ma:
                    print("  SKIPPING upload: dominant harmonic below 2× noise floor.")
                    raise NameError("snr_too_low")

                # Only upload physical cogging harmonics (k = n × pole_pairs).
                # Non-multiples cluster at the practical measurement noise floor
                # (~15–35 mA from friction hysteresis and positioning jitter) and
                # cannot be reliably distinguished from noise regardless of sweep method.
                _cog_ks  = [k for k in sorted_ks
                            if k > 0 and k % pole_pairs == 0 and k <= N_BINS // 2
                            and float(amps[k]) >= 2.0 * _noise_floor_ma]
                _top_cog = _cog_ks[:N_COG_BINS]
                _n_cog   = len(_top_cog)
                print("  Physical cogging harmonics above 2× noise floor (k = n×{}): {}".format(
                    pole_pairs, [k for k in _top_cog]))
                _cog_drop = [k for k in sorted_ks
                             if k > 0 and k % pole_pairs == 0 and k <= N_BINS // 2
                             and float(amps[k]) < 2.0 * _noise_floor_ma]
                if _cog_drop:
                    print("    Dropped (sub-noise, not uploaded): " + ", ".join(
                        "k={}({:.1f}mA)".format(_dk, float(amps[_dk]))
                        for _dk in _cog_drop[:8]) + (" …" if len(_cog_drop) > 8 else ""))
                if len(_cog_ks) > N_COG_BINS:
                    print("    Dropped ({}-bin cap, not uploaded): {}".format(
                        N_COG_BINS, list(_cog_ks[N_COG_BINS:])))

                def _clamp_i16_cog(v):
                    return max(-32768, min(32767, int(round(v))))

                print("\n  Uploading cogging compensation (0x3028) to node {} ...".format(
                    node_id))
                print("  {:>4}  {:>6}  {:>10}  {:>8}  {:>8}".format(
                    "Bin", "k", "Amp(mA)", "a_s", "a_c"))
                print("  " + "-" * 44)

                self.node.sdo[0x3028][1].raw = 0
                _n_written = 0
                for _bi in range(N_COG_BINS):
                    _as_sub = 2 + _bi * 3
                    _k_sub  = 3 + _bi * 3
                    _ac_sub = 4 + _bi * 3
                    if _bi < _n_cog:
                        _bk   = int(_top_cog[_bi])
                        _bA   = float(amps[_bk])
                        _bphi = float(phases[_bk])
                        _as_v = _clamp_i16_cog(-_bA * math.sin(_bphi))
                        _ac_v = _clamp_i16_cog(+_bA * math.cos(_bphi))
                        _k_v  = _bk
                        print("  {:>4d}  {:>6d}  {:>10.4f}  {:>8d}  {:>8d}".format(
                            _bi, _k_v, _bA, _as_v, _ac_v))
                    else:
                        _as_v = _ac_v = _k_v = 0
                    try:
                        self.node.sdo[0x3028][_as_sub].raw = _as_v
                        self.node.sdo[0x3028][_k_sub].raw  = _k_v
                        self.node.sdo[0x3028][_ac_sub].raw = _ac_v
                        _n_written += 1
                    except SdoAbortedError as _bin_exc:
                        if _bin_exc.code == 0x06020000:
                            print("  NOTE: firmware supports {} bin(s) — "
                                  "stopping at bin {}.".format(_n_written, _bi))
                        else:
                            print("  WARNING: bin {} SDO abort 0x{:08X} — "
                                  "stopping upload.".format(_bi, _bin_exc.code))
                        break

                if _n_written == 0:
                    raise RuntimeError("No cogging bins could be written to 0x3028.")

                self.node.sdo[0x3028][1].raw = 1
                print("  Cogging Compensation Active → 1  ({} bin(s) written)".format(
                    _n_written))
                try:
                    self.frame_menubar.COG_ON.Check(True)
                    self.frame_menubar.COG_OFF.Check(False)
                except Exception:
                    pass

                # Readback verification
                print("\n  Readback verification:")
                print("  {:>4}  {:>6}  {:>8}  {:>8}  {}".format(
                    "Bin", "k", "a_s", "a_c", "OK?"))
                print("  " + "-" * 40)
                _active_rb = self.node.sdo[0x3028][1].raw
                print("  Active flag readback: {}".format(_active_rb))
                _rb_ok = True
                for _bi in range(_n_written):
                    _as_rb = self.node.sdo[0x3028][2 + _bi * 3].raw
                    _k_rb  = self.node.sdo[0x3028][3 + _bi * 3].raw
                    _ac_rb = self.node.sdo[0x3028][4 + _bi * 3].raw
                    if _bi < _n_cog:
                        _bk_exp  = int(_top_cog[_bi])
                        _bA_exp  = float(amps[_bk_exp])
                        _bph_exp = float(phases[_bk_exp])
                        _as_exp  = _clamp_i16_cog(-_bA_exp * math.sin(_bph_exp))
                        _ac_exp  = _clamp_i16_cog(+_bA_exp * math.cos(_bph_exp))
                    else:
                        _bk_exp = _as_exp = _ac_exp = 0
                    _ok = (_as_rb == _as_exp and _k_rb == _bk_exp and _ac_rb == _ac_exp)
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

                print("\n  Saving 0x3028 to EEPROM ...")
                for _si in range(1, 2 + _n_written * 3):
                    self.node.sdo['Save']['Single'].raw = ((0x3028 << 8) | _si)
                print("  Saved.")
                _upload_ok = True

            except SdoAbortedError as _cog_sdo_exc:
                if _cog_sdo_exc.code == 0x06020000:
                    print("\n  WARNING: 0x3028 (CoggingCompensation) not found on "
                          "node {} — firmware does not support cogging upload.".format(node_id))
                else:
                    print("\n  WARNING: Cogging upload SDO abort "
                          "0x{:08X}: {}".format(_cog_sdo_exc.code, _cog_sdo_exc))
            except NameError:
                pass
            except ImportError:
                print("  (FFT skipped — numpy not installed)")
            except Exception as _fft_e:
                print("  WARNING: FFT/upload failed: {}".format(_fft_e))

            _upd(93)

            # ── Retest: position hold with compensation active ─────────────────
            if _upload_ok:
                try:
                    print("  Rebooting node {} to zero velocity integrator before retest...".format(
                        node_id))
                    self.network.send_message(0x0, [0x81, int(node_id)])
                    _sleep_responsive(1.5)
                    self.configure_Puck(configure_pdos=False)
                    _rt_active = int(self.node.sdo[0x3028][1].raw)
                    if not _rt_active:
                        print("  WARNING: compensation not active after reboot.")

                    self.node.sdo["ControlWord"].raw        = CLEAR_FAULT
                    self.node.sdo["ControlWord"].raw        = SHUTDOWN
                    self.node.sdo["ControlWord"].raw        = OP_ENABLED
                    self.node.sdo["SetModeOfOperation"].raw = MODE_PROFILE_VEL
                    time.sleep(0.2)
                    wx.Yield()

                    print("\n  Retest (compensation active, bidirectional) ...")
                    rt_fwd_bins = [[] for _ in range(N_BINS)]
                    for _b in range(N_BINS):
                        iq, actual_pos = _hold_and_measure(
                            _b * bin_cts, HOLD_STEPS, SAMPLE_FROM)
                        actual_bin = (int(round(actual_pos)) % enc_resolution
                                      // bin_cts % N_BINS)
                        rt_fwd_bins[actual_bin].append(iq)
                        if _b % 16 == 15 or _b == N_BINS - 1:
                            print("  CW bin {:>3d}/{} ...".format(_b + 1, N_BINS))
                        _upd(93 + _b * 3 // N_BINS)

                    rt_rev_bins = [[] for _ in range(N_BINS)]
                    for _b in range(N_BINS - 1, -1, -1):
                        iq, actual_pos = _hold_and_measure(
                            _b * bin_cts, HOLD_STEPS, SAMPLE_FROM)
                        actual_bin = (int(round(actual_pos)) % enc_resolution
                                      // bin_cts % N_BINS)
                        rt_rev_bins[actual_bin].append(iq)
                        if (N_BINS - 1 - _b) % 16 == 15 or _b == N_BINS - 1:
                            print("  CCW bin {:>3d}/{} ...".format(
                                N_BINS - _b, N_BINS))
                        _upd(96 + (N_BINS - 1 - _b) * 3 // N_BINS)

                    self.node.sdo["TargetVelocity"].raw = 0
                    _sleep_responsive(0.5)
                    self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE

                    rt_fwd_iq = _fill_bins(rt_fwd_bins)
                    rt_rev_iq = _fill_bins(rt_rev_bins)
                    rt_iq = [(rt_fwd_iq[_b] + rt_rev_iq[_b]) / 2.0
                             for _b in range(N_BINS)]
                    dc_rt  = sum(rt_iq) / len(rt_iq)
                    ac_rt  = [v - dc_rt for v in rt_iq]
                    rms_rt = (sum(v * v for v in ac_rt) / len(ac_rt)) ** 0.5
                    rms_diff = (rms_rt - rms_iq_ma) / rms_iq_ma * 100.0

                    print("\n  Retest results (bidirectional, compensation active):")
                    print("  AC RMS Iq:  before={:.2f} mA   after={:.2f} mA   ({:+.1f}%)".format(
                        rms_iq_ma, rms_rt, rms_diff))
                    if rms_diff < -5:
                        print("  Compensation is reducing Iq ripple ✓")
                    elif rms_diff > 10:
                        print("  WARNING: Iq increased — compensation may be destabilising.")
                    else:
                        print("  Iq approximately unchanged.")

                    try:
                        import matplotlib
                        matplotlib.use('Agg')
                        import matplotlib.pyplot as _plt_rt

                        _yr = max(max(abs(v) for v in iq_ac),
                                  max(abs(v) for v in ac_rt)) * 1.15 or 1.0
                        _fig_rt, (_ax_b, _ax_a) = _plt_rt.subplots(1, 2, figsize=(12, 4))
                        _fig_rt.suptitle(
                            'Cogging Compensation (position hold) — Node {}  {}  ({})\n'
                            'Iq RMS: {:.2f} → {:.2f} mA  ({:+.1f}%)'.format(
                                node_id, model_str, ts,
                                rms_iq_ma, rms_rt, rms_diff),
                            fontsize=10)
                        _ax_b.plot(bin_deg, iq_ac, 'b-', linewidth=1.0)
                        _ax_b.axhline(0, color='k', linewidth=0.8, linestyle='--')
                        _ax_b.set_ylim(-_yr, _yr)
                        _ax_b.set_xlabel('Mechanical angle (°)')
                        _ax_b.set_ylabel('Iq AC (mA)')
                        _ax_b.set_title('Iq — before  (RMS={:.2f} mA)'.format(rms_iq_ma))
                        _ax_b.grid(True, alpha=0.3)
                        _ax_a.plot(bin_deg, ac_rt, 'g-', linewidth=1.0)
                        _ax_a.axhline(0, color='k', linewidth=0.8, linestyle='--')
                        _ax_a.set_ylim(-_yr, _yr)
                        _ax_a.set_xlabel('Mechanical angle (°)')
                        _ax_a.set_title('Iq — after  (RMS={:.2f} mA  {:+.1f}%)'.format(
                            rms_rt, rms_diff))
                        _ax_a.grid(True, alpha=0.3)
                        _plt_rt.tight_layout()
                        _rt_plot_path = _cog_img(
                            '{}cogging_pos_retest_{}.png'.format(_file_pfx, ts))
                        _plt_rt.savefig(_rt_plot_path, dpi=100)
                        _plt_rt.close(_fig_rt)
                        print("  Before/after plot → {}".format(_rt_plot_path))
                    except ImportError:
                        pass
                    except Exception as _rt_plt_exc:
                        print("  WARNING: Retest plot failed: {}".format(_rt_plt_exc))

                except Exception as _rt_e:
                    print("  WARNING: Retest failed: {}".format(_rt_e))

            _upd(100)
            print("\nCogging position-hold sweep complete.")

        except Exception as _exc:
            self._cal_fault(_exc)
        finally:
            try:
                self.node.sdo["TargetVelocity"].raw = 0
                self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE
            except Exception:
                pass
            self.OnTaskComplete()
            self.choice_test.SetSelection(0)
            try:
                self.node.nmt.state = 'PRE-OPERATIONAL'
                time.sleep(0.1)
                self.configure_Puck()
                self.node.nmt.state = 'OPERATIONAL'
                time.sleep(0.1)
            except Exception:
                pass
            if self.ADC_ON == False and self.adcWasON:
                self.on_off_adc(self)
            self.Enable()

    def cogging_calibrate_auto(self, event, calAll=False, _upd=None, fast=False):
        """Entry point for cogging calibration menu item — runs the low-speed host sweep
        (cogging_error_compensation): measures Iq vs angle at a velocity-loop-trackable speed
        and validates on velocity ripple + per-harmonic before/after. The firmware DFT path
        (cogging_fw_calibrate) spins at 200 RPM where the higher cogging harmonics sit above
        the velocity-loop bandwidth, so its Iq is attenuated/lagged and its iq-ratio gate
        can't see efficacy — kept callable but no longer the default."""
        self.cogging_error_compensation(event, calAll=calAll, _upd=_upd)

    def cogging_fw_calibrate(self, event, calAll=False, _upd=None):
        """
        Firmware-side DFT cogging calibration using 0x3029.

        Spins motor at TARGET_MOTOR_RPM, triggers the ISR DFT accumulator,
        polls until done, then copies a_s/k/a_c from 0x3029 to 0x3028.

        If compensation is already active, offers:
          Recalibrate — new DFT sweep; before/after Iq comparison plot generated.
          Retest      — verify current compensation without new calibration.

        Both paths output cogging_fw_retest_*.png (before vs after Iq AC profile
        + harmonic bar chart with PASS/CHECK per dominant harmonic).
        """
        if not self.check_for_node():
            return
        # Firmware gate FIRST -- before toggling the ADC monitor, starting the
        # task gauge, or popping the "Cogging Compensation Active" dialog -- so an
        # old-firmware puck shows only the "Firmware Too Old" notice and nothing
        # else.
        if not self._fw_at_least(4, 4, 0):
            self._prompt_ok("Firmware Too Old",
                "Cogging compensation calibration requires firmware v4.4.0 or later.\n"
                "Please update the firmware and try again.")
            return
        if self.ADC_ON:
            self.adcWasON = True
            self.on_off_adc(self)
        else:
            self.adcWasON = False
        if calAll:
            if _upd is None:
                _upd = lambda v: None
        else:
            self.OnStartTask(None)
            _upd = lambda v: self.UpdateUI(v)
        if _upd is None:
            _upd = lambda v: None

        # Always show a dialog — options differ based on whether comp is already active.
        _retest_only = False
        _do_test     = True   # whether to run before/after Iq comparison
        try:
            _comp_active = bool(self.node.sdo[0x3028][1].raw)
        except Exception:
            _comp_active = False

        try:
            if _comp_active:
                _dlg_title = "Cogging Compensation Active"
                _dlg_body  = (
                    "Cogging compensation is currently active on this node.\n\n"
                    "Recalibrate + Test: run a new DFT sweep, replace coefficients,\n"
                    "  then compare Iq before/after.  ~55 s.\n\n"
                    "Retest Only: verify current compensation — spin with comp off\n"
                    "  then on, compare Iq profiles.  No new calibration.  ~35 s.")
                _btn1_lbl = "Recalibrate + Test"
                _btn2_lbl = "Retest Only"
            else:
                _dlg_title = "Cogging DFT Calibration"
                _dlg_body  = (
                    "No cogging compensation is active on this node.\n\n"
                    "Calibrate + Test: run DFT, upload results, then compare Iq\n"
                    "  before/after to confirm quality.  ~55 s.\n\n"
                    "Calibrate Only: run DFT and upload.  Faster, no comparison.\n"
                    "  ~20 s.")
                _btn1_lbl = "Calibrate + Test"
                _btn2_lbl = "Calibrate Only"

            _cdlg        = wx.Dialog(self, title=_dlg_title)
            _cdlg_sizer  = wx.BoxSizer(wx.VERTICAL)
            _cdlg_msg    = wx.StaticText(_cdlg, label=_dlg_body)
            _cdlg_sizer.Add(_cdlg_msg, 0, wx.ALL, 12)
            _cdlg_btn_sz = wx.BoxSizer(wx.HORIZONTAL)
            _btn1        = wx.Button(_cdlg, label=_btn1_lbl)
            _btn2        = wx.Button(_cdlg, label=_btn2_lbl)
            _btn_cancel  = wx.Button(_cdlg, wx.ID_CANCEL, label="Cancel")
            _cdlg_btn_sz.Add(_btn1,       0, wx.ALL, 4)
            _cdlg_btn_sz.Add(_btn2,       0, wx.ALL, 4)
            _cdlg_btn_sz.Add(_btn_cancel, 0, wx.ALL, 4)
            _cdlg_sizer.Add(_cdlg_btn_sz, 0, wx.ALIGN_CENTER | wx.BOTTOM, 8)
            _cdlg.SetSizerAndFit(_cdlg_sizer)
            _cdlg_choice = [None]
            def _on_btn1(e): _cdlg_choice[0] = 'btn1'; _cdlg.EndModal(wx.ID_YES)
            def _on_btn2(e): _cdlg_choice[0] = 'btn2'; _cdlg.EndModal(wx.ID_NO)
            _btn1.Bind(wx.EVT_BUTTON, _on_btn1)
            _btn2.Bind(wx.EVT_BUTTON, _on_btn2)
            _cdlg_result = _cdlg.ShowModal()
            _cdlg.Destroy()
            if _cdlg_result == wx.ID_CANCEL:
                if self.adcWasON and not self.ADC_ON:
                    self.on_off_adc(self)
                return
            if _comp_active:
                _retest_only = (_cdlg_choice[0] == 'btn2')   # "Retest Only"
                _do_test     = True
            else:
                _retest_only = False
                _do_test     = (_cdlg_choice[0] == 'btn1')   # "Calibrate + Test"
        except Exception:
            pass

        self.Disable()
        _task_lbl = ("Cogging retest..." if _retest_only
                     else "Cogging firmware DFT calibration...")
        self.frame_statusbar.SetStatusText(_task_lbl, 1)
        self.frame_statusbar.Update()
        wx.Yield()

        COG_CAL_DONE     = 2
        COG_CAL_ERROR    = 3
        TARGET_MOTOR_RPM = 200   # motor shaft RPM
        SETTLE_S        = 2.0   # settle before DFT1
        VERIFY_SETTLE_S = 20.0  # settle before DFT2 — allows velocity PI to fully adapt to feedforward
        DFT_SNR_MIN     = 2.0   # k=7 must be ≥ 2× mean of higher harmonics
        N_REVS          = 50    # motor shaft revolutions per DFT run
        DFT_RATIO_MAX   = 1.3   # PASS if DFT2_rms / DFT1_rms < 1.3 (PI adapted, feedforward took over)

        try:
            import datetime, os
            from paths import session_path
            ts = datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')

            enc_resolution  = int(self.node.sdo['EncoderConfig']['Resolution'].raw)
            node_id         = getattr(self.node, 'id', '?')
            pole_pairs      = int(self.node.sdo['Calibration']['poles'].raw) // 2
            kt              = int(self.node.sdo['Calibration']['kt'].raw)  # mNm/A
            vel_cts_per_sec = int(round(TARGET_MOTOR_RPM / 60.0 * enc_resolution))

            _pc = None
            try:
                _pc = int(self.node.sdo[0x1018][2].raw)
            except Exception:
                pass
            model_str = getattr(self, '_PRODUCT_CODE_MODELS', {}).get(_pc, 'unknown')
            _file_pfx = 'node{}_{}_'.format(node_id, model_str.replace(' ', '_'))

            def _cog_img(name):
                p = session_path('cogging/images/{}'.format(name))
                os.makedirs(os.path.dirname(p), exist_ok=True)
                return p

            # ---------------------------------------------------------------- #
            # Inner helpers                                                     #
            # ---------------------------------------------------------------- #

            def _run_dft(settle_s, comp_label):
                """Spin at TARGET_MOTOR_RPM, settle settle_s, trigger firmware DFT.
                Returns (results, rms) where results = list of (a_s, k, a_c) tuples.
                Caller must have already called _enter_vel_mode()."""
                self.node.sdo[0x3029][2].raw = N_REVS
                n_revs_actual = int(self.node.sdo[0x3029][2].raw)
                print("  Spinning at {} RPM, settling {:.0f} s ({}) ...".format(
                    TARGET_MOTOR_RPM, settle_s, comp_label))
                self.node.sdo['TargetVelocity'].raw = vel_cts_per_sec
                _sleep_responsive(settle_s)
                wx.Yield()
                print("  Triggering DFT ({} revs) ...".format(n_revs_actual))
                self.node.sdo[0x3029][1].raw = 1
                _t0, _tout = time.time(), 120.0
                while True:
                    time.sleep(0.25)
                    wx.Yield()
                    _st = int(self.node.sdo[0x3029][1].raw)
                    _el = time.time() - _t0
                    if _st == COG_CAL_DONE:
                        print("  DFT done in {:.1f} s.".format(_el))
                        break
                    elif _st == COG_CAL_ERROR:
                        raise RuntimeError("Firmware DFT ERROR — check poles/encoder config.")
                    elif _el > _tout:
                        self.node.sdo[0x3029][1].raw = 0
                        raise RuntimeError("DFT timed out after {:.0f} s.".format(_el))
                self.node.sdo['TargetVelocity'].raw = 0
                _sleep_responsive(0.5)
                print("\n  DFT results ({})".format(comp_label))
                print("  {:>4}  {:>6}  {:>8}  {:>8}  {:>10}".format(
                    "Bin", "k", "a_s", "a_c", "Amp(mA)"))
                print("  " + "-" * 44)
                _res = []
                for _bi in range(10):
                    _as = int(self.node.sdo[0x3029][3 + _bi * 3].raw)
                    _k  = int(self.node.sdo[0x3029][4 + _bi * 3].raw)
                    _ac = int(self.node.sdo[0x3029][5 + _bi * 3].raw)
                    _res.append((_as, _k, _ac))
                    print("  {:>4d}  {:>6d}  {:>8d}  {:>8d}  {:>10.2f}".format(
                        _bi, _k, _as, _ac, (_as**2 + _ac**2)**0.5))
                _rms = (sum((_r[0]**2 + _r[2]**2) / 2.0 for _r in _res)) ** 0.5
                print("  Total RMS = {:.1f} mA".format(_rms))
                return _res, _rms

            def _fw_plot_dft_comparison(results1, results2, rms1, rms2, passed, label_suffix):
                try:
                    import matplotlib
                    matplotlib.use('Agg')
                    import matplotlib.pyplot as _plt

                    _pf_col = 'green' if passed else 'darkorange'
                    _ratio  = rms2 / rms1 if rms1 > 0 else 999.0
                    _pf_lbl = ('PASS — ratio {:.2f} (PI adapted, feedforward active)'.format(_ratio)
                               if passed else
                               'FAIL — ratio {:.2f} (PI not adapted, comp wrong or stale)'.format(_ratio))
                    _fig, _axes = _plt.subplots(1, 3, figsize=(18, 5))
                    _ax1, _ax2, _ax3 = _axes

                    _fig.suptitle(
                        'Cogging Comp {} — Node {}  {}  ({})\n'
                        'DFT2/DFT1 ratio: {:.2f}  (pass < {:.1f})  [{}]'.format(
                            label_suffix.replace('_', ' ').title(),
                            node_id, model_str, ts,
                            _ratio, DFT_RATIO_MAX, _pf_lbl),
                        fontsize=11, color=_pf_col)

                    _ymax = max(
                        max((_r[0]**2 + _r[2]**2)**0.5 for _r in results1) if results1 else 1.0,
                        max((_r[0]**2 + _r[2]**2)**0.5 for _r in results2) if results2 else 1.0
                    ) * 1.25 or 1.0

                    def _dft_bars(ax, results, title, color):
                        ks   = [_r[1] for _r in results]
                        amps = [(_r[0]**2 + _r[2]**2)**0.5 for _r in results]
                        _bars = ax.bar(range(len(ks)), amps, color=color, alpha=0.85, width=0.6)
                        ax.set_xticks(range(len(ks)))
                        ax.set_xticklabels(['k={}'.format(_k) for _k in ks],
                                           rotation=45, ha='right', fontsize=8)
                        ax.set_ylabel('Amplitude (mA)')
                        ax.set_title(title)
                        ax.grid(True, alpha=0.3, axis='y')
                        ax.set_ylim(0, _ymax)
                        for _bar, _val in zip(_bars, amps):
                            if _val > 0.5:
                                ax.text(_bar.get_x() + _bar.get_width() / 2,
                                        _val + _ymax * 0.02,
                                        '{:.1f}'.format(_val),
                                        ha='center', va='bottom', fontsize=7)

                    _dft_bars(_ax1, results1,
                              'DFT 1 — comp OFF\n(RMS={:.1f} mA)'.format(rms1), 'steelblue')
                    _dft_bars(_ax2, results2,
                              'DFT 2 — comp ON\n(RMS={:.1f} mA)'.format(rms2),
                              'seagreen' if passed else 'tomato')

                    # Panel 3: total RMS comparison with pass threshold
                    _rms_bars = _ax3.bar(['DFT 1\n(comp OFF)', 'DFT 2\n(comp ON)'],
                                         [rms1, rms2],
                                         color=['steelblue', 'seagreen' if passed else 'tomato'],
                                         width=0.5)
                    _thresh = rms1 * DFT_RATIO_MAX
                    _ax3.axhline(_thresh, color='darkorange', linewidth=1.5, linestyle='--',
                                 label='Pass threshold (DFT2 < {:.1f}× DFT1)'.format(
                                     DFT_RATIO_MAX))
                    for _bar, _val in zip(_rms_bars, [rms1, rms2]):
                        _ax3.text(_bar.get_x() + _bar.get_width() / 2,
                                  _val + rms1 * 0.03,
                                  '{:.1f} mA'.format(_val),
                                  ha='center', va='bottom', fontsize=10)
                    _ax3.set_ylabel('Total cogging amplitude RMS (mA)')
                    _ax3.set_title('DFT2/DFT1 ratio  [{}]'.format('PASS' if passed else 'FAIL'),
                                   color=_pf_col)
                    _ax3.legend(fontsize=9)
                    _ax3.grid(True, alpha=0.3, axis='y')
                    _ax3.set_ylim(bottom=0)

                    _plt.tight_layout()
                    _plot_path = _cog_img(
                        '{}cogging_fw_dft_comp_{}.png'.format(_file_pfx, ts))
                    _plt.savefig(_plot_path, dpi=110, bbox_inches='tight')
                    _plt.close(_fig)
                    print("  Comparison plot → {}".format(_plot_path))
                except ImportError:
                    print("  (Plot skipped — matplotlib not installed)")
                except Exception as _pe:
                    print("  WARNING: plot failed: {}".format(_pe))

            def _disable_comp():
                for _da in range(3):
                    try:
                        self.node.sdo[0x3028][1].raw = 0
                        if int(self.node.sdo[0x3028][1].raw) == 0:
                            return
                    except Exception:
                        pass
                    time.sleep(0.1)
                raise RuntimeError("Could not disable cogging compensation — check CAN.")

            def _reboot_node(msg):
                print("  Rebooting node {} ({}) ...".format(node_id, msg))
                self.network.send_message(0x0, [0x81, int(node_id)])
                _sleep_responsive(1.5)
                self.configure_Puck(configure_pdos=False)

            def _enter_vel_mode():
                self.node.sdo["ControlWord"].raw        = CLEAR_FAULT
                self.node.sdo["ControlWord"].raw        = SHUTDOWN
                self.node.sdo["ControlWord"].raw        = OP_ENABLED
                self.node.sdo["SetModeOfOperation"].raw = MODE_PROFILE_VEL
                time.sleep(0.2)
                wx.Yield()

            # ================================================================ #
            #  RETEST ONLY — velocity sweep before (comp OFF) and after       #
            # ================================================================ #
            if _retest_only:
                print("\nCogging retest  ({} motor RPM  DFT1 {:.0f}s + DFT2 {:.0f}s settle)".format(
                    TARGET_MOTOR_RPM, SETTLE_S, VERIFY_SETTLE_S))
                print("  {} pole pairs  enc_res={}  Kt={} mNm/A".format(
                    pole_pairs, enc_resolution, kt))
                _upd(2)

                _reboot_node("clear integrator for DFT1")
                _disable_comp()
                _upd(5)
                _enter_vel_mode()

                print("\n  DFT 1 — comp OFF ...")
                results1, rms1 = _run_dft(SETTLE_S, 'comp OFF')
                self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE

                if len(results1) > 1:
                    _fund = (results1[0][0]**2 + results1[0][2]**2) ** 0.5
                    _oth  = [(r[0]**2 + r[2]**2)**0.5 for r in results1[1:]]
                    _snr  = _fund / (sum(_oth) / len(_oth)) if _oth and sum(_oth) > 0 else 999.0
                    print("  DFT1 SNR = {:.2f} (min {:.1f})".format(_snr, DFT_SNR_MIN))
                    if _snr < DFT_SNR_MIN:
                        raise RuntimeError(
                            "DFT1 quality check failed: SNR {:.2f} < {:.1f}.".format(
                                _snr, DFT_SNR_MIN))
                _upd(40)

                # Upload DFT1 coefficients directly to 0x3028.
                # cog_cal computes a_s=(2/N)*sum(iq*sin), a_c=(2/N)*sum(iq*cos) using the
                # same p and convention as the feedforward — no conversion needed.
                # Skip k<42: low-order harmonics (k=7,14,21,28,35) include both true cogging
                # AND encoder-error-induced theta_e oscillations. Uploading these as feedforward
                # injects current at the wrong phase, amplifying stiction rather than canceling
                # cogging. k>=42 are slot harmonics where encoder error is negligible.
                print("\n  Uploading DFT1 → 0x3028 (cogging feedforward, k>=42 only) ...")
                print("  {:>4}  {:>6}  {:>8}  {:>8}  {:>10}".format(
                    "Bin", "k", "a_s", "a_c", "Amp(mA)"))
                print("  " + "-" * 44)
                try:
                    self.node.sdo[0x3028][1].raw = 0
                    _n_cog_wb = 0
                    for _as_v, _k_v, _ac_v in results1:
                        if _k_v < 42:
                            print("  skip  {:>6d}  (k<42, enc-error dominated)".format(_k_v))
                            continue
                        try:
                            self.node.sdo[0x3028][2 + _n_cog_wb * 3].raw = _as_v
                            self.node.sdo[0x3028][3 + _n_cog_wb * 3].raw = _k_v
                            self.node.sdo[0x3028][4 + _n_cog_wb * 3].raw = _ac_v
                            _n_cog_wb += 1
                            print("  {:>4d}  {:>6d}  {:>8d}  {:>8d}  {:>10.2f}".format(
                                _n_cog_wb - 1, _k_v, _as_v, _ac_v, (_as_v**2 + _ac_v**2)**0.5))
                        except SdoAbortedError as _cbe:
                            if _cbe.code == 0x06020000:
                                print("  NOTE: firmware supports {} bin(s).".format(_n_cog_wb))
                            else:
                                raise
                            break
                    if _n_cog_wb > 0:
                        self.node.sdo[0x3028][1].raw = 1
                        print("  Cogging Compensation Active → 1  ({} bins)".format(_n_cog_wb))
                        for _csi in range(1, 2 + _n_cog_wb * 3):
                            self.node.sdo['Save']['Single'].raw = ((0x3028 << 8) | _csi)
                        print("  Saved to EEPROM.")
                    else:
                        print("  WARNING: no bins written to 0x3028.")
                except SdoAbortedError as _e28:
                    if _e28.code == 0x06020000:
                        print("  NOTE: 0x3028 not in firmware — cogging comp skipped.")
                    else:
                        raise
                _upd(48)

                _reboot_node("load new comp for DFT2")
                _act = int(self.node.sdo[0x3028][1].raw)
                if not _act:
                    raise RuntimeError(
                        "Comp not active after reboot — 0x3028:1=0. "
                        "Run calibration first.")
                _upd(55)

                _enter_vel_mode()
                print("\n  DFT 2 — comp ON ({:.0f} s settle) ...".format(VERIFY_SETTLE_S))
                results2, rms2 = _run_dft(VERIFY_SETTLE_S, 'comp ON')
                self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE
                _upd(90)

                _rt_ratio  = rms2 / rms1 if rms1 > 0 else 999.0
                _rt_passed = _rt_ratio < DFT_RATIO_MAX
                print("\n  DFT2/DFT1 ratio = {:.2f}  (pass threshold < {:.1f})".format(
                    _rt_ratio, DFT_RATIO_MAX))
                print("  Result: {}".format(
                    "PASS — PI adapted, feedforward active" if _rt_passed
                    else "FAIL — PI not adapted (comp wrong or profile stale)"))

                _fw_plot_dft_comparison(results1, results2, rms1, rms2, _rt_passed, 'Retest')
                _upd(98)
                self.frame_statusbar.SetStatusText(
                    "Cogging retest: {}".format(
                        "PASS" if _rt_passed else "FAIL — check compensation"), 1)
                return   # finally still runs

            # ================================================================ #
            #  RECALIBRATE — optional before snap → DFT → after snap → plot   #
            # ================================================================ #
            print("\nCogging firmware DFT calibration")
            print("  {} pole pairs  enc_res={}  Kt={} mNm/A  {} motor RPM".format(
                pole_pairs, enc_resolution, kt, TARGET_MOTOR_RPM))

            _disable_comp()

            # Reboot clears integrator for DFT.
            _reboot_node("clear integrator for DFT")
            _disable_comp()

            # Write n_revs AFTER reboot — firmware cog_cal_init_OD clamps on every boot.
            self.node.sdo[0x3029][2].raw = N_REVS
            n_revs = int(self.node.sdo[0x3029][2].raw)
            print("  n_revs={} motor shaft revs".format(n_revs))
            _upd(22)

            _enter_vel_mode()
            print("  Spinning at {} motor RPM, settling {:.1f} s ...".format(
                TARGET_MOTOR_RPM, SETTLE_S))
            self.node.sdo['TargetVelocity'].raw = vel_cts_per_sec
            _sleep_responsive(SETTLE_S)
            wx.Yield()
            _upd(27)

            # Trigger firmware DFT.
            print("  Triggering firmware DFT (0x3029:1 = 1) ...")
            self.node.sdo[0x3029][1].raw = 1
            _t_start   = time.time()
            _timeout_s = 120.0

            while True:
                time.sleep(0.25)
                wx.Yield()
                _status  = int(self.node.sdo[0x3029][1].raw)
                _elapsed = time.time() - _t_start
                if _status == COG_CAL_DONE:
                    print("  DFT complete in {:.1f} s.".format(_elapsed))
                    break
                elif _status == COG_CAL_ERROR:
                    raise RuntimeError(
                        "Firmware DFT returned ERROR — check poles/encoder config.")
                elif _elapsed > _timeout_s:
                    self.node.sdo[0x3029][1].raw = 0
                    raise RuntimeError(
                        "Firmware DFT timed out after {:.0f} s — motor not spinning?".format(
                            _elapsed))
                _upd(int(27 + 50 * min(_elapsed / 30.0, 1.0)))

            self.node.sdo['TargetVelocity'].raw = 0
            _sleep_responsive(0.5)
            self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE
            wx.Yield()
            _upd(78)

            # Read and display DFT results.
            print("\n  DFT results from 0x3029:")
            print("  {:>4}  {:>6}  {:>8}  {:>8}  {:>10}".format(
                "Bin", "k", "a_s", "a_c", "Amp(mA)"))
            print("  " + "-" * 44)
            results = []
            for _bi in range(10):
                _as  = int(self.node.sdo[0x3029][3 + _bi * 3].raw)
                _k   = int(self.node.sdo[0x3029][4 + _bi * 3].raw)
                _ac  = int(self.node.sdo[0x3029][5 + _bi * 3].raw)
                _amp = (_as ** 2 + _ac ** 2) ** 0.5
                results.append((_as, _k, _ac))
                print("  {:>4d}  {:>6d}  {:>8d}  {:>8d}  {:>10.2f}".format(
                    _bi, _k, _as, _ac, _amp))

            # DFT quality check: k=7 (fundamental) must dominate over higher harmonics.
            # If all bins are roughly equal, the DFT captured a transient, not cogging.
            if len(results) > 1:
                _fund_amp  = (results[0][0]**2 + results[0][2]**2) ** 0.5
                _others    = [(_r[0]**2 + _r[2]**2)**0.5 for _r in results[1:]]
                _mean_oth  = sum(_others) / len(_others)
                _dft_snr   = _fund_amp / _mean_oth if _mean_oth > 0 else 999.0
                print("\n  DFT quality: k={} = {:.1f} mA, mean(others) = {:.1f} mA, "
                      "SNR = {:.2f} (min {:.1f})".format(
                          results[0][1], _fund_amp, _mean_oth, _dft_snr, DFT_SNR_MIN))
                if _dft_snr < DFT_SNR_MIN:
                    raise RuntimeError(
                        "DFT quality check failed: fundamental SNR {:.2f} < {:.1f}. "
                        "Higher harmonics are unusually large — DFT likely captured a "
                        "transient. Retry calibration.".format(_dft_snr, DFT_SNR_MIN))

            # Expected feedforward IQ RMS: sqrt(sum(A_k^2 / 2)) over all DFT bins.
            _iq_ff_rms = (sum((_r[0]**2 + _r[2]**2) / 2.0 for _r in results)) ** 0.5
            print("  Expected feedforward IQ RMS = {:.1f} mA".format(_iq_ff_rms))

            # Upload to 0x3028 — skip k<42.
            # k=7,14,21,28,35 DFT values are contaminated by encoder-error-induced
            # theta_e oscillations. Wrong-phase feedforward at these frequencies causes
            # oscillation that corrupts DFT2 and reduces average torque at low RPM.
            # k>=42 are slot harmonics where encoder error is negligible.
            print("\n  Uploading to 0x3028 (k>=42 only) ...")
            print("  {:>4}  {:>6}  {:>8}  {:>8}  {:>10}".format(
                "Bin", "k", "a_s", "a_c", "Amp(mA)"))
            print("  " + "-" * 44)
            self.node.sdo[0x3028][1].raw = 0
            _n_written = 0
            _uploaded = []
            for _as, _k, _ac in results:
                if _k < 42:
                    print("  skip  {:>6d}  (k<42, enc-error dominated)".format(_k))
                    continue
                try:
                    self.node.sdo[0x3028][2 + _n_written * 3].raw = _as
                    self.node.sdo[0x3028][3 + _n_written * 3].raw = _k
                    self.node.sdo[0x3028][4 + _n_written * 3].raw = _ac
                    _uploaded.append((_as, _k, _ac))
                    _n_written += 1
                    print("  {:>4d}  {:>6d}  {:>8d}  {:>8d}  {:>10.2f}".format(
                        _n_written - 1, _k, _as, _ac, (_as**2 + _ac**2)**0.5))
                except SdoAbortedError as _bin_exc:
                    if _bin_exc.code == 0x06020000:
                        print("  NOTE: firmware supports {} bin(s).".format(_n_written))
                    else:
                        print("  WARNING: bin {} abort 0x{:08X}.".format(
                            _n_written, _bin_exc.code))
                    break

            # RMS of uploaded bins only — used as ratio denominator so threshold is meaningful.
            _iq_ff_rms_uploaded = (
                sum((_r[0]**2 + _r[2]**2) / 2.0 for _r in _uploaded) ** 0.5
                if _uploaded else 0.0)

            if _n_written == 0:
                raise RuntimeError("No cogging bins could be written to 0x3028.")

            self.node.sdo[0x3028][1].raw = 1
            print("  Active → 1  ({} bins written)".format(_n_written))
            try:
                self.frame_menubar.COG_ON.Check(True)
                self.frame_menubar.COG_OFF.Check(False)
            except Exception:
                pass

            print("\n  Saving 0x3028 to EEPROM ...")
            for _si in range(1, 2 + _n_written * 3):
                self.node.sdo['Save']['Single'].raw = ((0x3028 << 8) | _si)
            print("  Saved.")
            _upd(83)

            if _do_test:
                # Reboot clears integrator; comp loads from EEPROM.
                _reboot_node("load EEPROM comp for DFT2")
                _act = int(self.node.sdo[0x3028][1].raw)
                if not _act:
                    print("  WARNING: comp not active after reboot — EEPROM save may have failed.")
                _upd(86)

                _enter_vel_mode()
                print("\n  DFT 2 — comp ON ({:.0f} s settle, {} revs) ...".format(
                    VERIFY_SETTLE_S, N_REVS))
                results2, rms2 = _run_dft(VERIFY_SETTLE_S, 'comp ON')
                self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE
                _upd(94)

                _ratio      = rms2 / _iq_ff_rms_uploaded if _iq_ff_rms_uploaded > 0 else 999.0
                _cal_passed = _ratio < DFT_RATIO_MAX
                print("\n  DFT2/DFT1 ratio = {:.2f}  (pass threshold < {:.1f})".format(
                    _ratio, DFT_RATIO_MAX))
                print("  Result: {}".format(
                    "PASS — PI adapted, feedforward active" if _cal_passed
                    else "FAIL — PI not adapted (comp wrong or profile corrupted)"))

                _fw_plot_dft_comparison(results, results2, _iq_ff_rms, rms2,
                                        _cal_passed, 'Calibration')
                _upd(98)
                self.frame_statusbar.SetStatusText(
                    "Cogging FW DFT complete — {} harmonics  {}".format(
                        _n_written,
                        "PASS" if _cal_passed else "FAIL — see plot"), 1)
            else:
                _upd(98)
                self.frame_statusbar.SetStatusText(
                    "Cogging FW DFT complete — {} harmonics uploaded.".format(_n_written), 1)
                print("\n  Done.  Reboot recommended before motion to zero integrator.")

        except SdoAbortedError as _exc:
            if _exc.code == 0x06020000:
                _emsg = ("This puck firmware does not support DFT cogging "
                         "calibration (object 0x3029 not found). Update the "
                         "firmware and try again.")
                print("\n  ERROR: 0x3029 not found — "
                      "firmware does not support DFT calibration (upgrade to v4.3.11+).")
            else:
                _emsg = "CAN/SDO error during cogging calibration (abort 0x{:08X}).".format(
                    _exc.code)
                print("\n  SDO abort 0x{:08X}: {}".format(_exc.code, _exc))
            self.frame_statusbar.SetStatusText("Cogging FW DFT failed.", 1)
            # Surface it in a dialog -- the packaged build has no console, so the
            # status bar alone wouldn't explain the failure. Deferred via
            # CallAfter so the finally below restores the bus first.
            wx.CallAfter(wx.MessageBox, _emsg, "Cogging Calibration Failed",
                         wx.OK | wx.ICON_ERROR)
        except Exception as _exc:
            print("\n  Cogging FW DFT failed: {}".format(_exc))
            import traceback
            traceback.print_exc()
            self.frame_statusbar.SetStatusText("Cogging FW DFT failed.", 1)
            try:
                self.node.sdo['TargetVelocity'].raw = 0
                self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE
            except Exception:
                pass
            # Surface the reason in a dialog (str(_exc) carries the detail, e.g.
            # the SNR/"retry calibration" hint) -- essential in the packaged
            # build with no live log. Deferred so the finally restores first.
            wx.CallAfter(wx.MessageBox, "Cogging calibration failed:\n\n{}".format(_exc),
                         "Cogging Calibration Failed", wx.OK | wx.ICON_ERROR)
        finally:
            try:
                self.node.nmt.state = 'PRE-OPERATIONAL'
                time.sleep(0.1)
                self.configure_Puck()
                self.node.nmt.state = 'OPERATIONAL'
                time.sleep(0.1)
            except Exception:
                pass
            if self.adcWasON and not self.ADC_ON:
                self.on_off_adc(self)
            self.Enable()
            _upd(100)
            self.OnTaskComplete()

    def _load_enc_correction_table(self):
        """Return (table, path) from the most recent enc_correction_full CSV, or (None, None)."""
        import os, glob
        from paths import resource_path
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
            self.node.sdo[0x3027][1].raw = 1 if enable else 0
            self.node.sdo['Save']['Single'].raw = ((0x3027 << 8) | 1)
            state_str = "ON" if enable else "OFF"
            print("Encoder error compensation set to {} and saved.".format(state_str))
            self.frame_menubar.ON.Check(enable)
            self.frame_menubar.OFF.Check(not enable)
        except Exception as e:
            print("Error setting encoder compensation state: {}".format(e))

    def cogging_compensation_state(self, event):  # wxGlade: puckutilityapp_frame.<event_handler>
        if self.check_for_node() == False:
            return
        if not self._fw_at_least(4, 4, 0):
            self._prompt_ok("Firmware Too Old",
                "Cogging compensation requires firmware v4.4.0 or later.\n"
                "Please update the firmware and try again.")
            # Revert the radio selection -- the feature is unavailable on this
            # firmware, so it cannot be turned ON.
            self.frame_menubar.COG_ON.Check(False)
            self.frame_menubar.COG_OFF.Check(True)
            return
        enable = event.GetId() == self.frame_menubar.COG_ON.GetId()
        try:
            self.node.sdo[0x3028][1].raw = 1 if enable else 0
            self.node.sdo['Save']['Single'].raw = ((0x3028 << 8) | 1)
            state_str = "ON" if enable else "OFF"
            print("Cogging compensation set to {} and saved.".format(state_str))
            self.frame_menubar.COG_ON.Check(enable)
            self.frame_menubar.COG_OFF.Check(not enable)
        except Exception as e:
            print("Error setting cogging compensation state: {}".format(e))

    def set_user_dir(self, event):  # wxGlade: wxp3_frame.<event_handler>
        if self.check_for_node() == False: #len(self.network.scanner.nodes) == 0:
          return False
        
        print("Event handler 'set_user_dir'")
        
        self.node.sdo['EncoderConfig']['UserPolarity'].raw = 1 # Assume positive to start
        encoder_resolution = self.node.sdo['EncoderConfig']['Resolution'].raw
        starting_position = self.node.sdo['PositionFeedback'].raw

        self.frame_statusbar.SetStatusText("Please turn motor in positive (+) direction...", 1)
        self.frame_statusbar.Update()
        wx.Yield()

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

    def get_version(self, vers): # Convert uint32_t to semantic version: Major.Minor.Patch
        return "{0}.{1}.{2}".format(
            (vers >> 24) & 0xFF, (vers >> 8) & 0xFFFF, (vers & 0xFF))

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

            version = self.get_version(self.node.sdo['MfgSoftwareVersion'].raw)
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
