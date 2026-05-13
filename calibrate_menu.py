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
    MODE_IDLE, MODE_PHASE_VOLTAGE_ANGLE, MODE_PROFILE_TRQ,
)
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
    PVCA torque control via fire-and-forget SDO, driven by TPDO1 position
    feedback at ~1 kHz.

    RPDO4 was attempted for Theta_e + Motor.ud but the firmware does not
    route RPDO data to those objects regardless of the SDO mapping config.

    PDO isolation: RPDO1/2 have trans_type=0, so every SYNC caused the puck
    to re-apply ControlWord=0 ("Disable Voltage") from their default buffer,
    killing the motor on each tick.  Fix: disable RPDO1/2 and TPDO2/3 before
    starting SYNC, restore on stop.

    Control path: SYNC thread → TPDO1 callback (rx thread) → compute → two
    fire-and-forget SDO writes for Theta_e and Motor.ud.  No blocking on the
    rx thread; SDO ACK frames arrive later and are silently discarded.
    """
    _SYNC_PERIOD_S  = 0.001
    _STATUS_EVERY_N = 100

    # PDO communication-parameter indices to save/disable around PVCA
    _RPDO_COMM = [0x1400, 0x1401]        # RPDO1, RPDO2
    _TPDO_COMM = [0x1801, 0x1802]        # TPDO2, TPDO3

    def __init__(self, parent, node, table, table_path, mp):
        super().__init__(parent, title="PVCA Torque Control",
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self._node        = node
        self._table       = table
        self._mp          = mp
        self._torque_val  = 0.0   # GIL-safe; wx thread writes, rx thread reads
        self._running     = False
        self._stop_evt    = threading.Event()
        self._sync_thread = None
        self._iter_count  = 0
        self._t_start     = 0.0
        self._tpdo1_cob   = (0x180 | node.id) & 0x7FF
        self._sdo_cob     = 0x600 | node.id
        self._saved_cobs  = {}    # comm_idx → saved COB-ID (for restore)
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
        self._torque_ctrl = wx.TextCtrl(panel, value="50", size=(90, -1))
        self._torque_ctrl.Bind(wx.EVT_TEXT, self._on_torque_text)
        hs.Add(self._torque_ctrl, 0)
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

    def _toggle(self, _evt):
        if self._running:
            self._stop_from_ui()
        else:
            self._start()

    # ------------------------------------------------------------------
    # PDO isolation — disable interfering PDOs before SYNC starts
    # ------------------------------------------------------------------

    def _isolate_pdos(self):
        """Disable RPDO1/2 and TPDO2/3; store originals for restore.

        RPDO1/RPDO2 have trans_type=0.  Every SYNC causes the puck to
        re-apply their buffered data.  Default buffer = 0 → ControlWord=0
        = 'Disable Voltage', which kills the motor on every SYNC tick.
        """
        n = self._node
        for idx in self._RPDO_COMM + self._TPDO_COMM:
            cob = n.sdo[idx][1].raw
            self._saved_cobs[idx] = cob
            n.sdo[idx][1].raw = cob | 0x80000000   # set invalid bit → PDO disabled
        print("PVCA: disabled RPDO1/2, TPDO2/3 to isolate SYNC")

    def _restore_all_pdos(self):
        """Restore all PDOs saved during _isolate_pdos."""
        for idx, saved_cob in self._saved_cobs.items():
            try:
                self._node.sdo[idx][1].raw = saved_cob
            except Exception:
                pass
        self._saved_cobs.clear()
        print("PVCA: PDOs restored")

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

        enc_idx    = int(actual_pos) % mp['enc_resolution']
        correction = self._table[enc_idx]
        corrected_pos   = actual_pos + correction
        theta_e_rotor_f = (corrected_pos - mp['e_zero']) * mp['e_polarity'] \
                          / mp['cts_per_elec'] * 65536.0
        theta_e_rotor_i = int(round(theta_e_rotor_f)) % 65536
        advance     = 16384 if torque >= 0 else -16384
        theta_e_u   = (theta_e_rotor_i + advance) % 65536
        theta_e_raw = theta_e_u if theta_e_u < 32768 else theta_e_u - 65536
        iq_ma = abs(torque) * 1000.0 / mp['Kt']
        vq    = iq_ma / 1000.0 * mp['Rt']
        ud    = int(round(vq / mp['V_bus'] * 32767))
        ud    = max(0, min(ud, int(0.85 * 32767)))

        # Fire-and-forget expedited SDO writes — no blocking on the rx thread.
        # ACK frames sent by the puck arrive later and are silently discarded.
        # Expedited INT16 download: [cmd, idx_lo, idx_hi, sub, val_lo, val_hi, 0, 0]
        sdo = self._sdo_cob
        net = self._node.network
        net.send_message(sdo,
            struct.pack('<BBBBhxx', 0x2B, 0xEA, 0x60, 0x00, theta_e_raw))
        net.send_message(sdo,
            struct.pack('<BBBBhxx', 0x2B, 0x10, 0x30, 0x04, ud))

        n = self._iter_count + 1
        self._iter_count = n
        if n == 1:
            print("PVCA: first step — pos={} corr={:+d} θ_e={} ud={}".format(
                actual_pos, correction, theta_e_rotor_i, ud))
        if n % self._STATUS_EVERY_N == 0:
            elapsed = time.monotonic() - self._t_start
            hz = n / max(elapsed, 1e-9)
            wx.CallAfter(self._status.SetLabel,
                "{:.0f} Hz  θ_e={:6d}  corr={:+4d}  "
                "ud={:5d}  iq_est={:5.0f}mA".format(
                    hz, theta_e_rotor_i, correction, ud, iq_ma))

    # ------------------------------------------------------------------
    # SYNC driver (background thread)
    # ------------------------------------------------------------------

    def _sync_loop(self):
        """Sends SYNC at ~1 kHz; each SYNC triggers TPDO1 from the puck."""
        while not self._stop_evt.is_set():
            t0 = time.monotonic()
            try:
                self._node.network.send_message(0x80, bytes())
            except Exception as e:
                wx.CallAfter(self._fault_stop, "SYNC error: " + str(e))
                return
            rem = self._SYNC_PERIOD_S - (time.monotonic() - t0)
            if rem > 0.0:
                time.sleep(rem)

    # ------------------------------------------------------------------
    # Start / stop / cleanup
    # ------------------------------------------------------------------

    def _start(self):
        try:
            n = self._node
            n.nmt.state = 'OPERATIONAL'

            n.sdo["ControlWord"].raw = CLEAR_FAULT
            n.sdo["ControlWord"].raw = SHUTDOWN
            n.sdo["ControlWord"].raw = OP_ENABLED
            n.sdo["SetModeOfOperation"].raw = MODE_PHASE_VOLTAGE_ANGLE
            n.sdo['Theta_e'].raw = 0
            n.sdo['Motor']['ud'].raw = 0
            time.sleep(0.1)

            self._isolate_pdos()    # disable RPDO1/2, TPDO2/3 before SYNC
        except Exception as e:
            self._restore_all_pdos()
            wx.MessageBox("Failed to start:\n{}".format(e), "Error",
                          wx.OK | wx.ICON_ERROR)
            return

        try:
            self._torque_val = float(self._torque_ctrl.GetValue())
        except ValueError:
            self._torque_val = 0.0

        self._iter_count = 0
        self._t_start    = time.monotonic()
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
        self._restore_all_pdos()
        self._run_btn.SetLabel("Start PVCA")
        self._status.SetLabel("Stopped")

    def _motor_off(self):
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
        self._restore_all_pdos()
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
        self._restore_all_pdos()
        self.Destroy()


class calibrate():
    def _cal_fault(self, exc):
        """Shared cleanup called when an SDO or other exception aborts calibration."""
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

        if _sw is not None:
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

    def calibrate_all_pucks(self, event):
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
        if self.check_for_node() == False:
            # print("No active puck")
            return False
        print("Running full calibration for Puck {}".format(self.getID()))

        self.frame_statusbar.SetStatusText("Progress: 0%", 1)
        self.progress.Show()
        self.GetStatusBar().Refresh()
        self.GetStatusBar().Update()

        try:
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

            self.node.sdo['Motor']['ud'].raw = 000

            # Set Mode to Voltage
            print("Setting Mode = VOLTAGE MODE")
            self.node.sdo["SetModeOfOperation"].raw = MODE_PHASE_VOLTAGE_ANGLE

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

            # Average N_AVG fresh reads; round mean Q12.4 → Q12.0
            _sum = {'Alpha': 0, 'Beta': 0}
            for _i in range(_N_AVG):
                _upd(55 + _i * 40 // _N_AVG)  # 55→95%
                for _ch in ['Alpha', 'Beta']:
                    _sum[_ch] += self.node.sdo[_ch]['Filtered'].raw
                wx.Yield()

            # Calibrate iSense — store high-precision float bias for use by igainfactor
            # in the same session (avoids re-reading the rounded EEPROM value).
            self._alpha_bias_f = _sum['Alpha'] / _N_AVG / 16.0
            self._beta_bias_f  = _sum['Beta']  / _N_AVG / 16.0
            for channel in ['Alpha', 'Beta']:
                print("Previous {0} iSense bias = {1}".format(channel, self.node.sdo[channel]['Bias'].raw))
                filt = int(round(_sum[channel] / _N_AVG / 16))
                self.node.sdo[channel]['Bias'].raw = filt
                print("New {0} iSense bias = {1}  ({2}-sample avg)".format(channel, filt, _N_AVG))

            self.node.sdo['Save']['Single'].raw = ((0x3008 << 8) | 0x03) # Save Alpha iSense cal to EE
            self.node.sdo['Save']['Single'].raw = ((0x3009 << 8) | 0x03) # Save Beta iSense cal to EE

            # Check Bounds for error!!
            # 5% (~102 counts) is a conservative sentinel. Physical clipping limit is
            # ~19% (387 counts) for a channel gain of 3530/4096 at 28.24 A peak / 30 A range:
            #   headroom = 2048 × (gain/4096) × (1 − i_peak/i_range) = 387 counts
            error = 0.05 # 5%

            a_bias = self.node.sdo['Alpha']['Bias'].raw
            b_bias = self.node.sdo['Beta']['Bias'].raw

            # Set Mode to Idle (0)
            print("Setting Mode = IDLE")
            self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE

            out_of_bounds = (
                a_bias > 2048 * (1 + error) or a_bias < 2048 * (1 - error) or
                b_bias > 2048 * (1 + error) or b_bias < 2048 * (1 - error)
            )
            if out_of_bounds:
                print('iSense Bias out of bounds!')
                msg = "iSense Bias out of bounds!" \
                "\n\nAlpha Bias: {}" \
                "\nBeta Bias: {}" \
                "\nAcceptable Range: {} - {}" \
                "\n\nDebugging steps:" \
                "\n- Ensure proper configuration file has been loaded" \
                "\n- Verify phase leads are properly connected" \
                "\n\nWould you like to continue calibration?".format(
                    a_bias, b_bias, round(2048*(1-error)), round(2048*(1+error)))

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
                    _ramp_step = max(100, int((motor_ud * calibration_current / _id_now - motor_ud) / 4))
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
            a_filt_f = _sum_a / _N_IGAIN_AVG / 16.0  # float Q12.0 — no rounding yet
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
            b_filt_f = _sum_b / _N_IGAIN_AVG / 16.0  # float Q12.0 — no rounding yet
            _id_at_b = (_sum_id_b / _N_IGAIN_AVG) / 1000.0 * i_peak
            print("Peak Beta  = {0:.3f}  id={1:.1f} mA  theta_e={2:.2f} rad  ({3}-sample avg)".format(
                b_filt_f, _id_at_b,
                self.node.sdo['Theta_e'].raw / 32768.0 * 3.14159, _N_IGAIN_AVG))

            self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE

            # Use high-precision float bias from ibias (same session) if available;
            # fall back to rounded EEPROM value when igainfactor runs standalone.
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
        # ALGORITHM OVERVIEW
        # MaxSettlingTime is only applied by firmware at initialisation, so each
        # timing step requires a full save → NMT reset → configure_Puck cycle before
        # sampling.  The sweep is therefore structured as:
        #   outer loop: timing values  (one reset per step)
        #   inner loop: SVM sectors    (ramp + sample within the same boot)
        #
        # KNOWN LIMITATION — CYCLE-TO-CYCLE NOISE
        # The dominant noise source (~2 ADC counts plat_dev) is not within-step
        # measurement noise but between-step ramp variation: each reset produces a
        # slightly different motor_ud convergence, so the absolute Alpha.Raw value
        # drifts by ~2–4 counts even in the fully-settled plateau region.
        # Increasing N_SAMPLES does not help because the variation is between boots,
        # not within a single measurement window.
        #
        # FUTURE IMPROVEMENT — MULTI-CURRENT SLOPE DETECTION
        # Sampling at multiple current levels per sector per step (e.g. 25 %, 50 %,
        # 75 %, 100 % of calibration_current) and fitting a line through
        # Alpha.Raw vs commanded current would make the settling metric the ADC
        # gain slope rather than an absolute value.  The slope is insensitive to
        # the between-boot DC offset variation, and the settling transient shows up
        # as a nonlinearity / slope deviation that only disappears once MaxSettlingTime
        # exceeds the true ADC settling time.  This would likely tighten the per-sector
        # crossing estimates from ~±25 ns to ~±5 ns but at the cost of 3–4× more
        # ramp time per step (each of the four current levels needs its own ramp).
        if calAll == False:
            if self.check_for_node() == False:
                return False
            self.Disable()

        quick_test = self.choice_test.GetSelection()
        if quick_test != 0:
            self.lastMode = 0
            print("Setting Mode = IDLE")
            self.choice_test.SetSelection(0)
            self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE

        if self.ADC_ON == True:
            self.on_off_adc(self)
            self.adcWasON = True
        else:
            self.adcWasON = False

        self.frame_statusbar.SetStatusText("Calibrating current timing...", 1)
        self.frame_statusbar.Update()
        wx.Yield()

        try:
            print("Going OpEnabled")
            self.node.sdo["ControlWord"].raw = CLEAR_FAULT
            self.node.sdo["ControlWord"].raw = SHUTDOWN
            self.node.sdo["ControlWord"].raw = OP_ENABLED
            self.node.sdo["SetModeOfOperation"].raw = MODE_PHASE_VOLTAGE_ANGLE
    
            calibration_current = self.node.sdo['Calibration']['i_cal'].raw
            i_peak = self.node.sdo['Calibration']['i_peak'].raw
            if calibration_current > i_peak:
                calibration_current = i_peak
    
            dead_time         = self.node.sdo['Amp']['DeadTime'].raw         # ns
            sampling_time     = self.node.sdo['Amp']['SamplingTime'].raw     # ns (fixed, not touched)
            conversion_time   = self.node.sdo['Amp']['ConversionTime'].raw   # ns
            original_settling = self.node.sdo['Amp']['MaxSettlingTime'].raw  # ns
            freq_hz           = self.node.sdo['Amp']['Frequency'].raw
    
            half_period_ns = 1_000_000_000 // (2 * max(freq_hz, 1))
    
            # MaxSettlingTime is only read by firmware during initialization — runtime
            # SDO writes have no effect until the puck is reset.  The sweep must
            # therefore save→reset→re-init for every timing step.
    
            # Alpha/Beta raw ADC bias (zero-current midpoint) for centring the signal
            alpha_bias = self.node.sdo['Alpha']['Bias'].raw
            beta_bias  = self.node.sdo['Beta']['Bias'].raw
    
            # 6 SVM sector centers spaced 60° apart (theta_e raw: ±32767 = ±pi)
            sector_angles = [
                int( 32767 / 6),      #  30° = pi/6
                int( 32767 * 3 / 6),  #  90° = pi/2
                int( 32767 * 5 / 6),  # 150° = 5pi/6
                int(-32767 * 5 / 6),  # 210° = -5pi/6
                int(-32767 * 3 / 6),  # 270° = -pi/2
                int(-32767 / 6),      # 330° = -pi/6
            ]
    
            # --- MaxSettlingTime sanity check ---
            # Two save+reset cycles at opposite extremes confirm whether the parameter
            # has any effect on Alpha.Raw after the firmware reads it at init.
            print("--- MaxSettlingTime sanity check (2 resets) ---")
            node_id = self.node.id
            _DIAG_SAMPLES = 30
            _diag_theta = int(32767 / 6)  # 30°
            _diag_means = {}
            for _t_diag, _label in [(0, 'min'), (half_period_ns, 'max')]:
                self.node.sdo['Amp']['MaxSettlingTime'].raw = _t_diag
                self.node.sdo['Save']['Single'].raw = ((0x3001 << 8) | 0x05)
                self.network.send_message(0x0, [0x81, int(node_id)])
                _sleep_responsive(0.5)
                self.configure_Puck(configure_pdos=False)
                _readback = self.node.sdo['Amp']['MaxSettlingTime'].raw
                print("  MaxSettlingTime={:6d} ns  readback after reset={:6d} ns  {}".format(
                    _t_diag, _readback, "OK" if _readback == _t_diag else "MISMATCH"))
                self.node.sdo["ControlWord"].raw = CLEAR_FAULT
                self.node.sdo["ControlWord"].raw = SHUTDOWN
                self.node.sdo["ControlWord"].raw = OP_ENABLED
                self.node.sdo["SetModeOfOperation"].raw = MODE_PHASE_VOLTAGE_ANGLE
                self.node.sdo['Theta_e'].raw = _diag_theta
                self.node.sdo['Motor']['ud'].raw = 0
                _diag_ud = 0
                while (self.node.sdo['Motor']['id'].raw / 1000.0 * i_peak < 0.8 * calibration_current
                       and _diag_ud < 32000):
                    _diag_ud = min(_diag_ud + 500, 32000)
                    self.node.sdo['Motor']['ud'].raw = _diag_ud
                    time.sleep(0.01)
                    wx.Yield()
                while (self.node.sdo['Motor']['id'].raw / 1000.0 * i_peak < calibration_current
                       and _diag_ud < 32000):
                    _diag_ud = min(_diag_ud + 100, 32000)
                    self.node.sdo['Motor']['ud'].raw = _diag_ud
                    time.sleep(0.01)
                    wx.Yield()
                _sleep_responsive(0.2)
                _s = 0
                for _ in range(_DIAG_SAMPLES):
                    _s += self.node.sdo['Alpha']['Raw'].raw
                    time.sleep(0.005)
                _diag_means[_label] = _s / _DIAG_SAMPLES
                print("  Alpha.Raw mean={:.2f}".format(_diag_means[_label]))
                self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE
            _diag_diff = abs(_diag_means['max'] - _diag_means['min'])
            if _diag_diff < 5.0:
                print("  WARNING: MaxSettlingTime has NO measurable effect on Alpha.Raw "
                      "after reset (diff={:.3f} counts). Check with firmware team whether "
                      "0x3001/5 is wired to the ADC trigger in this build.".format(_diag_diff))
            else:
                print("  OK: MaxSettlingTime effect confirmed after reset "
                      "(diff={:.3f} counts).".format(_diag_diff))
            print("--- End sanity check ---")
            # ------------------------------------
    
            # Two-pass sweep: coarse (100 ns) locates the crossing region, fine (25 ns) resolves it.
            # sector_ud is determined once at coarse_max where the ADC is guaranteed settled,
            # giving an accurate ramp free of settling-transient bias.  Both passes then apply
            # the stored voltage directly — no re-ramp between passes.
            coarse_step   = 100
            fine_step     = 25
            N_SAMPLES_C   = 10    # fewer samples in coarse pass for speed
            N_SAMPLES_F   = 10    # fine pass: boot-to-boot variation dominates, not within-step noise
            SETTLE_C      = 0.15  # s  coarse inductive-settle wait
            SETTLE_F      = 0.20  # s  fine inductive-settle wait
            coarse_start  = 0
            coarse_max    = 1600
            coarse_values = list(range(coarse_start, coarse_max + 1, coarse_step))
            n_coarse      = len(coarse_values)
            sector_ud     = [None] * len(sector_angles)
    
            est_pre = len(sector_angles) * 3.0 + 0.5
            est_c   = n_coarse * (0.5 + len(sector_angles) * (SETTLE_C + N_SAMPLES_C * 0.005))
            est_f   = 20 * (0.5 + len(sector_angles) * (SETTLE_F + N_SAMPLES_F * 0.005))
            print("Two-pass sweep: coarse {} to {} ns ({} steps × {} ns)  half period={} ns".format(
                coarse_start, coarse_max, n_coarse, coarse_step, half_period_ns))
            print("Est. time: {:.0f} s  "
                  "(pre-ramp {:.0f} s + coarse {:.0f} s + fine ~{:.0f} s, ~20 fine steps assumed)".format(
                  est_pre + est_c + est_f, est_pre, est_c, est_f))
    
            # --- Pre-sweep: ramp at coarse_max where ADC is settled ---
            print("Pre-sweep ramp at {} ns...".format(coarse_max))
            self.node.sdo['Amp']['MaxSettlingTime'].raw = coarse_max
            self.node.sdo['Save']['Single'].raw = ((0x3001 << 8) | 0x05)
            self.network.send_message(0x0, [0x81, int(node_id)])
            _sleep_responsive(0.5)
            self.node.sdo["ControlWord"].raw = CLEAR_FAULT
            self.node.sdo["ControlWord"].raw = SHUTDOWN
            self.node.sdo["ControlWord"].raw = OP_ENABLED
            self.node.sdo["SetModeOfOperation"].raw = MODE_PHASE_VOLTAGE_ANGLE
            for sector_idx, theta_e in enumerate(sector_angles):
                self.node.sdo['Theta_e'].raw = theta_e
                self.node.sdo['Motor']['ud'].raw = 0
                motor_ud = 0
                _sleep_responsive(0.1)  # let inductive current decay before ramp check
                while (self.node.sdo['Motor']['id'].raw / 1000.0 * i_peak < calibration_current
                       and motor_ud < 32000):
                    _id_now = self.node.sdo['Motor']['id'].raw / 1000.0 * i_peak
                    if motor_ud > 0 and _id_now > 0:
                        _ramp_step = max(50, int((motor_ud * calibration_current / _id_now - motor_ud) / 4))
                    else:
                        _ramp_step = max(50, 32000 // 12)
                    motor_ud = min(motor_ud + _ramp_step, 32000)
                    self.node.sdo['Motor']['ud'].raw = motor_ud
                    time.sleep(0.01)
                    wx.Yield()
                sector_ud[sector_idx] = motor_ud
                print("  sector {}/6  motor_ud={} (reused for all sweep steps)".format(
                    sector_idx + 1, motor_ud))
            self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE
    
            # --- Pass 1: coarse ---
            coarse_data = [[None] * len(sector_angles) for _ in range(n_coarse)]
            for t_idx, t in enumerate(coarse_values):
                self.frame_statusbar.SetStatusText(
                    "Timing cal — coarse {}/{} ({} ns)".format(t_idx + 1, n_coarse, t), 1)
                self.frame_statusbar.Update()
                wx.Yield()
                self.node.sdo['Amp']['MaxSettlingTime'].raw = t
                self.node.sdo['Save']['Single'].raw = ((0x3001 << 8) | 0x05)
                self.network.send_message(0x0, [0x81, int(node_id)])
                _sleep_responsive(0.5)
                self.node.sdo["ControlWord"].raw = CLEAR_FAULT
                self.node.sdo["ControlWord"].raw = SHUTDOWN
                self.node.sdo["ControlWord"].raw = OP_ENABLED
                self.node.sdo["SetModeOfOperation"].raw = MODE_PHASE_VOLTAGE_ANGLE
                for sector_idx, theta_e in enumerate(sector_angles):
                    self.node.sdo['Theta_e'].raw = theta_e
                    self.node.sdo['Motor']['ud'].raw = sector_ud[sector_idx]
                    _sleep_responsive(SETTLE_C)
                    alpha_sum = 0
                    beta_sum  = 0
                    for _ in range(N_SAMPLES_C):
                        alpha_sum += self.node.sdo['Alpha']['Raw'].raw
                        beta_sum  += self.node.sdo['Beta']['Raw'].raw
                        time.sleep(0.005)
                        wx.Yield()
                    coarse_data[t_idx][sector_idx] = (alpha_sum / N_SAMPLES_C,
                                                       beta_sum  / N_SAMPLES_C)
                    self.node.sdo['Motor']['ud'].raw = 0  # de-energise between sectors
                self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE
                print("  Coarse {}/{}: {} ns  done".format(t_idx + 1, n_coarse, t))
    
            # --- Coarse analysis: find crossing bracket to set fine sweep bounds ---
            plat_c       = max(0, 3 * n_coarse // 4)
            cross_lo_all = []
            cross_hi_all = []
            for sector_idx in range(len(sector_angles)):
                for ch_idx, bias in ((0, alpha_bias), (1, beta_bias)):
                    ch_vals_c  = [coarse_data[t_idx][sector_idx][ch_idx] - bias
                                  for t_idx in range(n_coarse)]
                    plateau_c  = sum(ch_vals_c[plat_c:]) / len(ch_vals_c[plat_c:])
                    devs_c     = [abs(v - plateau_c) for v in ch_vals_c]
                    pd_sorted  = sorted(devs_c[plat_c:])
                    pd_mid     = len(pd_sorted) // 2
                    plat_dev_c = (pd_sorted[pd_mid] if len(pd_sorted) % 2
                                  else (pd_sorted[pd_mid - 1] + pd_sorted[pd_mid]) / 2.0)
                    amp_c      = max(devs_c) - plat_dev_c
                    if amp_c < max(8.0, 3.0 * plat_dev_c):
                        continue  # flat channel, skip
                    thresh_c = plat_dev_c + 0.10 * max(amp_c, 1.0)
                    smooth_c = list(devs_c)
                    for i in range(1, n_coarse - 1):
                        smooth_c[i] = (devs_c[i - 1] + devs_c[i] + devs_c[i + 1]) / 3.0
                    for i in range(1, n_coarse):
                        if smooth_c[i - 1] > thresh_c >= smooth_c[i]:
                            cross_lo_all.append(coarse_values[i - 1])
                            cross_hi_all.append(coarse_values[i])
                            break
    
            if cross_lo_all:
                fine_start = max(coarse_start, min(cross_lo_all) - coarse_step)
                fine_end   = min(coarse_max,   max(cross_hi_all) + 3 * coarse_step)
                print("Coarse crossing bracket: {}–{} ns  →  fine sweep: {}–{} ns  "
                      "({} steps × {} ns)".format(
                      min(cross_lo_all), max(cross_hi_all),
                      fine_start, fine_end,
                      len(range(fine_start, fine_end + 1, fine_step)), fine_step))
            else:
                fine_start = coarse_start
                fine_end   = coarse_max
                print("WARNING: no crossing found in coarse pass — "
                      "using full range for fine sweep")
    
            fine_values = list(range(fine_start, fine_end + 1, fine_step))
    
            # --- Pass 2: fine ---
            print("Pass 2 (fine): {} to {} ns  ({} steps × {} ns)".format(
                fine_start, fine_end, len(fine_values), fine_step))
            fine_data = [[None] * len(sector_angles) for _ in range(len(fine_values))]
            for t_idx, t in enumerate(fine_values):
                self.frame_statusbar.SetStatusText(
                    "Timing cal — fine {}/{} ({} ns)".format(
                        t_idx + 1, len(fine_values), t), 1)
                self.frame_statusbar.Update()
                wx.Yield()
                self.node.sdo['Amp']['MaxSettlingTime'].raw = t
                self.node.sdo['Save']['Single'].raw = ((0x3001 << 8) | 0x05)
                self.network.send_message(0x0, [0x81, int(node_id)])
                _sleep_responsive(0.5)
                self.node.sdo["ControlWord"].raw = CLEAR_FAULT
                self.node.sdo["ControlWord"].raw = SHUTDOWN
                self.node.sdo["ControlWord"].raw = OP_ENABLED
                self.node.sdo["SetModeOfOperation"].raw = MODE_PHASE_VOLTAGE_ANGLE
                for sector_idx, theta_e in enumerate(sector_angles):
                    self.node.sdo['Theta_e'].raw = theta_e
                    self.node.sdo['Motor']['ud'].raw = sector_ud[sector_idx]
                    _sleep_responsive(SETTLE_F)
                    alpha_sum = 0
                    beta_sum  = 0
                    for _ in range(N_SAMPLES_F):
                        alpha_sum += self.node.sdo['Alpha']['Raw'].raw
                        beta_sum  += self.node.sdo['Beta']['Raw'].raw
                        time.sleep(0.005)
                        wx.Yield()
                    a_raw = alpha_sum / N_SAMPLES_F
                    b_raw = beta_sum  / N_SAMPLES_F
                    fine_data[t_idx][sector_idx] = (a_raw, b_raw)
                    print("  sector {}/6  theta_e={:6d}  alpha={:7.1f}  beta={:7.1f}".format(
                        sector_idx + 1, theta_e, a_raw, b_raw))
                    self.node.sdo['Motor']['ud'].raw = 0  # de-energise between sectors
                self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE
    
            # Analysis uses the fine-pass results
            timing_values    = fine_values
            step_sector_data = fine_data
    
            # --- Analysis: per-sector threshold crossing on bias-subtracted signal ---
            sector_settling_times = []
            sector_sweet_spots    = []   # per-sector centre of valid ADC window
            sector_upper_bounds   = []   # per-sector upper window limit (None if not detected)
            # Populated during analysis for the debug plot (devs, smooth, threshold, t_ch per channel)
            plot_data = {}
    
            for sector_idx in range(len(sector_angles)):
                times        = timing_values
                signal_means = [step_sector_data[t_idx][sector_idx]
                                for t_idx in range(len(timing_values))]
                n             = len(times)
                plateau_start = max(0, 3 * n // 4)
                early_end     = max(1, n // 4)
    
                print("Sector {}/6 analysis:".format(sector_idx + 1))
                t_settle = 0.0
                plot_data[sector_idx] = {}
                ch_sweets  = []   # non-flat channel sweet spots this sector
                ch_uppers  = []   # non-flat channel upper bounds this sector (None if not found)
                for ch_name, ch_idx, bias in (('Alpha', 0, alpha_bias),
                                               ('Beta',  1, beta_bias)):
                    ch_vals   = [s[ch_idx] - bias for s in signal_means]
                    # Mean for plateau centre: averages Gaussian between-boot noise better than median
                    plateau   = sum(ch_vals[plateau_start:]) / len(ch_vals[plateau_start:])
                    devs      = [abs(v - plateau) for v in ch_vals]
                    peak_dev  = max(devs)
                    # MAD for noise floor: robust to the occasional outlier step in the plateau window
                    plat_devs = sorted(devs[plateau_start:])
                    plat_mid  = len(plat_devs) // 2
                    plat_dev  = (plat_devs[plat_mid] if len(plat_devs) % 2
                                 else (plat_devs[plat_mid - 1] + plat_devs[plat_mid]) / 2.0)
                    early_dev = sum(devs[:early_end]) / early_end
    
                    # 3-point centred moving average to smooth per-step cycle-to-cycle noise
                    smooth = list(devs)
                    for i in range(1, n - 1):
                        smooth[i] = (devs[i - 1] + devs[i] + devs[i + 1]) / 3.0
    
                    signal_amplitude = peak_dev - plat_dev
                    # Raised flat gate (was max(3,2×plat_dev)) to suppress noise channels
                    threshold = plat_dev + 0.10 * max(signal_amplitude, 1.0)
    
                    # Print per-step raw deviations to show curve shape
                    dev_str = "  {}  devs: ".format(ch_name) + "  ".join(
                        "{:4d}ns={:.1f}".format(times[i], devs[i]) for i in range(n))
                    print(dev_str)
    
                    t_ch = 0.0
                    if signal_amplitude < max(8.0, 3.0 * plat_dev):
                        reason = "flat (amplitude={:.2f})".format(signal_amplitude)
                    elif early_dev < 1.5 * plat_dev:
                        reason = "no early elevation (early_dev={:.2f})".format(early_dev)
                    else:
                        # Find crossing on the smoothed curve, interpolate for sub-step precision
                        reason = "no crossing in sweep range"
                        for i in range(1, n):
                            if smooth[i - 1] > threshold >= smooth[i]:
                                span = smooth[i - 1] - smooth[i]
                                frac = (smooth[i - 1] - threshold) / span if span > 0 else 0.0
                                t_ch = times[i - 1] + frac * (times[i] - times[i - 1])
                                reason = "crossed at {:.1f} ns (between {}–{} ns)".format(
                                    t_ch, times[i - 1], times[i])
                                break
    
                    t_ch = max(0.0, t_ch)
    
                    # --- Sweet spot: timing of minimum smoothed deviation in settled region ---
                    # Settled region starts at the lower-bound crossing; fall back to plateau_start
                    # for flat/no-crossing channels so we still report where the noise is lowest.
                    settled_from = plateau_start
                    if t_ch > 0:
                        settled_from = next(
                            (i for i, t in enumerate(times) if t >= t_ch), plateau_start)
                    min_smooth_val = min(smooth[settled_from:])
                    sweet_idx = settled_from + smooth[settled_from:].index(min_smooth_val)
                    t_sweet = times[sweet_idx]
    
                    # --- Upper window bound ---
                    # Scan right-to-left from the penultimate step (skip last step: its smoothed
                    # value is unaveraged and artificially noisy) back to the sweet spot.
                    # Flag if smooth rises above 3×plat_dev AND meaningfully above the sweet-spot
                    # minimum — this indicates the ADC enters a new disturbance region at high timing.
                    tight = 3.0 * plat_dev
                    t_upper = None
                    scan_end = n - 2  # always stop one before last (edge artefact)
                    if sweet_idx < scan_end:
                        for i in range(scan_end, sweet_idx, -1):
                            if smooth[i] > tight and smooth[i] > min_smooth_val + tight:
                                t_upper = times[i]
                                break
    
                    is_active = signal_amplitude >= max(8.0, 3.0 * plat_dev)
                    if is_active:
                        ch_sweets.append(t_sweet)
                        ch_uppers.append(t_upper)
    
                    upper_str = ("  upper={} ns".format(int(t_upper))
                                 if t_upper is not None else "  no upper bound in sweep")
                    print("  {}  threshold={:.2f}  plat_dev={:.2f}  amplitude={:.2f}  "
                          "t_settle={:.1f} ns  t_sweet={} ns{}  ({})".format(
                          ch_name, threshold, plat_dev, signal_amplitude,
                          t_ch, int(t_sweet), upper_str, reason))
                    t_settle = max(t_settle, t_ch)
    
                    plot_data[sector_idx][ch_name] = {
                        'devs': devs, 'smooth': smooth,
                        'threshold': threshold, 't_ch': t_ch,
                        't_sweet': t_sweet, 't_upper': t_upper,
                    }
    
                print("  Sector {} settling time: {:.1f} ns".format(sector_idx + 1, t_settle))
                sector_settling_times.append(t_settle)
    
                if ch_sweets:
                    s_sweet = sum(ch_sweets) / len(ch_sweets)
                    sector_sweet_spots.append(s_sweet)
                    # Upper bound for the sector: minimum detected across channels
                    # (the tightest constraint wins)
                    active_uppers = [u for u in ch_uppers if u is not None]
                    s_upper = min(active_uppers) if active_uppers else None
                    sector_upper_bounds.append(s_upper)
                    upper_note = ("  upper bound ~{} ns".format(int(s_upper))
                                  if s_upper is not None else "  no upper bound in sweep")
                    print("  Sector {} sweet spot: ~{:.0f} ns{}".format(
                        sector_idx + 1, s_sweet, upper_note))
                else:
                    sector_sweet_spots.append(None)
                    sector_upper_bounds.append(None)
    
            # --- Debug plot: deviation curves for all sectors ---
            try:
                import matplotlib
                matplotlib.use('Agg')  # non-interactive; avoids wx/Tk backend conflicts
                import matplotlib.pyplot as plt
                import os
    
                fig, axes = plt.subplots(2, 3, figsize=(16, 9), sharey=False)
                fig.suptitle(
                    'MaxSettlingTime calibration — ADC deviation vs settling time\n'
                    'fine sweep {} – {} ns, {} ns steps  '
                    '(coarse {} – {} ns, {} ns steps)'.format(
                        fine_start, fine_end, fine_step,
                        coarse_start, coarse_max, coarse_step),
                    fontsize=12)
    
                ch_colors = {'Alpha': ('steelblue', 'royalblue'),
                             'Beta':  ('tomato',    'firebrick')}
    
                for sector_idx in range(len(sector_angles)):
                    ax = axes.flat[sector_idx]
                    t_sector = sector_settling_times[sector_idx]
    
                    for ch_name, (light, dark) in ch_colors.items():
                        pd = plot_data[sector_idx][ch_name]
                        ax.plot(timing_values, pd['devs'], 'o', color=light,
                                markersize=3, alpha=0.5, label='{} raw'.format(ch_name))
                        ax.plot(timing_values, pd['smooth'], '-', color=dark,
                                linewidth=1.5, label='{} smooth'.format(ch_name))
                        ax.axhline(pd['threshold'], color=dark, linestyle='--',
                                   linewidth=0.8, alpha=0.7)
                        if pd['t_ch'] > 0:
                            ax.axvline(pd['t_ch'], color=dark, linestyle=':',
                                       linewidth=1.2, alpha=0.8)
                        if pd['t_sweet'] is not None:
                            ax.axvline(pd['t_sweet'], color=dark, linestyle=(0, (3, 1, 1, 1)),
                                       linewidth=1.0, alpha=0.6)
                        if pd['t_upper'] is not None:
                            ax.axvline(pd['t_upper'], color=dark, linestyle='--',
                                       linewidth=1.2, alpha=0.9)
    
                    # Sector result (worst-case channel lower bound)
                    ax.axvline(t_sector, color='black', linewidth=1.5,
                               label='lower {:.0f} ns'.format(t_sector))
                    # Sector sweet spot (centre of valid window)
                    if sector_sweet_spots[sector_idx] is not None:
                        ax.axvline(sector_sweet_spots[sector_idx], color='green',
                                   linewidth=1.2, linestyle='--',
                                   label='sweet ~{:.0f} ns'.format(sector_sweet_spots[sector_idx]))
                    if sector_upper_bounds[sector_idx] is not None:
                        ax.axvline(sector_upper_bounds[sector_idx], color='orange',
                                   linewidth=1.2, linestyle='--',
                                   label='upper {:.0f} ns'.format(sector_upper_bounds[sector_idx]))
                    ax.set_title('Sector {}/6  θ_e={}'.format(
                        sector_idx + 1, sector_angles[sector_idx]), fontsize=10)
                    ax.set_xlabel('MaxSettlingTime (ns)', fontsize=8)
                    ax.set_ylabel('|deviation| (counts)', fontsize=8)
                    ax.legend(fontsize=7, loc='upper right')
                    ax.grid(True, alpha=0.25)
    
                plt.tight_layout()
                plot_path = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    'logs',
                    'itiming_cal_{}.png'.format(
                        time.strftime('%Y-%m-%d_%H-%M-%S')))
                fig.savefig(plot_path, dpi=110, bbox_inches='tight')
                plt.close(fig)
                print("Calibration plot saved: {}".format(plot_path))
                webbrowser.open('file://' + plot_path)
            except ImportError:
                print("matplotlib not installed — skipping calibration plot")
            except Exception as _plot_err:
                print("Plot failed: {}".format(_plot_err))
    
            # Conservative choice: maximum settling time required across all sectors
            optimal_settling = int(max(sector_settling_times))
            optimal_settling = min(optimal_settling, fine_end)
    
            print("Per-sector settling times (ns): {}".format(
                [round(t, 1) for t in sector_settling_times]))
            valid_sweets = [s for s in sector_sweet_spots if s is not None]
            valid_uppers = [u for u in sector_upper_bounds if u is not None]
            if valid_sweets:
                overall_sweet = sum(valid_sweets) / len(valid_sweets)
                sweet_range   = (int(min(valid_sweets)), int(max(valid_sweets)))
                print("Per-sector sweet spots  (ns): {}".format(
                    [round(s, 0) if s is not None else None for s in sector_sweet_spots]))
                print("ADC window: lower bound={} ns  sweet spot=~{:.0f} ns (range {}–{} ns)  "
                      "upper bound={}".format(
                          optimal_settling,
                          overall_sweet, sweet_range[0], sweet_range[1],
                          "{} ns".format(int(min(valid_uppers))) if valid_uppers
                          else "not detected in sweep (>{} ns)".format(fine_end)))
            print("Optimal MaxSettlingTime: {} ns  (was {} ns)".format(
                optimal_settling, original_settling))
    
            self.node.sdo['Amp']['MaxSettlingTime'].raw = optimal_settling
            self.node.sdo['Save']['Single'].raw = ((0x3001 << 8) | 0x05)
            print("Saved Amp.MaxSettlingTime to EEPROM — rebooting to apply...")
            self.network.send_message(0x0, [0x81, int(node_id)])
            _sleep_responsive(0.5)
            self.configure_Puck()
            print("Puck rebooted with MaxSettlingTime={} ns active.".format(optimal_settling))
    
            self.frame_statusbar.SetStatusText(
                "Current timing calibrated: {} ns".format(optimal_settling), 1)
    
            if self.ADC_ON == False and self.adcWasON == True:
                self.on_off_adc(self)
    
            if calAll == False:
                self.Enable()

        except Exception as _exc:
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
            dlg = wx.MessageDialog(None,msg,'Warning!',wx.YES_NO | wx.ICON_WARNING)
            answer = dlg.ShowModal()
            dlg.Destroy()
  
            _upd(100)
            if self.ADC_ON == False and self.adcWasON == True:
                self.on_off_adc(self)
            if calAll == False:
                self.OnTaskComplete()
                self.Enable()
            try:
                if answer == wx.ID_YES:
                    return True
                if answer == wx.ID_NO:
                    return False
            except:
                pass

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
          
        # Set the number of lag increments to attempt without setting a new max_vel
        max_cycles = 50

        # Init: cycles = 0, max = 0, lag = 0
        cycles = 0
        max_vel = 0
        lag = 0

        while True:
          # Read vel.fbk
          vel = abs(self.node.sdo["VelocityFeedback"].raw)
          # If |vel.fbk| > max, update max, remember lag, reset cycles to zero
          if vel > max_vel:
            max_vel = vel
            saved_lag_1 = lag
            cycles = 0
          else: # Else, ++cycles
            cycles += 1
          
          print("Lag: {0}, Vel: {1}, MaxVel: {2}, Cycles: {3}".format(lag, vel, max_vel, cycles))
          # Increase EncoderLag until cycles == max_cycles
          lag += 1
          self.node.sdo['EncoderConfig']['LagFactor'].raw = lag
          if cycles > max_cycles:
            break
          time.sleep(0.05)
          wx.Yield() # keep wx event loop alive so Windows doesn't mark the app "Not Responding"

        # Invert TargetTorque
        self.node.sdo["TargetTorque"].raw = -cmd_value # Send
        self.node.sdo['EncoderConfig']['LagFactor'].raw = 0

        _sleep_responsive(0.5)

        # Init: cycles = 0, max = 0, lag = 0
        cycles = 0
        max_vel = 0
        lag = 0

        while True:
          # Read vel.fbk
          vel = abs(self.node.sdo["VelocityFeedback"].raw)
          # If |vel.fbk| > max, update max, remember lag, reset cycles to zero
          if vel > max_vel:
            max_vel = vel
            saved_lag_2 = lag
            cycles = 0
          else: # Else, ++cycles
            cycles += 1
          
          print("Lag: {0}, Vel: {1}, MaxVel: {2}, Cycles: {3}".format(lag, vel, max_vel, cycles))
          # Increase EncoderLag until cycles == max_cycles
          lag += 1
          self.node.sdo['EncoderConfig']['LagFactor'].raw = lag
          if cycles > max_cycles:
            break
          time.sleep(0.05)
          wx.Yield() # keep wx event loop alive so Windows doesn't mark the app "Not Responding"

        # Take the average of the two lags
        lag = (saved_lag_1 + saved_lag_2) / 2
        print("Lag_1: {0}, Lag_2: {1}, Setting LagFactor: {2}".format(saved_lag_1, saved_lag_2, lag))

        # Store the LagFactor
        self.node.sdo['EncoderConfig']['LagFactor'].raw = lag
        self.node.sdo['Save']['Single'].raw = ((0x3013 << 8) | 0x05) # Save lag to EE

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
        """Sweep theta_e through one full mechanical revolution and compare the
        actual encoder position to the ideal linear relationship.  Deviations
        reveal encoder nonlinearity and distinguish electrical errors (repeat at
        the same electrical angle every cycle) from mechanical errors (repeat at
        the same mechanical angle, appearing at different electrical angles each
        cycle)."""
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

                fig = plt.figure(figsize=(15, 8))
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
                plot_path = resource_path(os.path.join(
                    'logs', 'enc_linearity_{}.png'.format(ts)))
                os.makedirs(os.path.dirname(plot_path), exist_ok=True)
                plt.savefig(plot_path, dpi=100)
                plt.close()
                print("\nPlot saved: {}".format(plot_path))
            except ImportError:
                pass

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
        self.Disable()
        if self.ADC_ON:
            self.adcWasON = True
            self.on_off_adc(self)
        else:
            self.adcWasON = False

        try:
            import cmath as _cm
            import datetime, os
            from paths import resource_path

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

            N_PER_CYCLE = 192   # steps per electrical cycle → 1.875° per step
            STEP_S      = 0.05  # settle time per step (s)
            N_HARMONICS = 16    # Fourier harmonics retained
            N_TOTAL     = N_PER_CYCLE * pole_pairs  # exactly one mechanical revolution

            print("\nEncoder correction table — sweep parameters")
            print("  {} pole pairs  {:.2f} cts/elec  enc_res={}  "
                  "e_zero={}  e_polarity={}".format(
                      pole_pairs, cts_per_elec, enc_resolution, e_zero, e_polarity))
            print("  {} steps/cycle × {} cycles = {} steps  ~{:.0f} s".format(
                N_PER_CYCLE, pole_pairs, N_TOTAL, N_TOTAL * STEP_S + 15))

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
            enc_start       = self.node.sdo['Encoder']['RawPosition'].raw
            enc_prev        = enc_start
            enc_accumulated = 0
            # (step_in_cycle, cycle_n, correction_cts, abs_enc_pos)
            sweep = []

            print("Sweeping {} steps...".format(N_TOTAL))
            for step in range(N_TOTAL + 1):
                step_in_cycle = step % N_PER_CYCLE
                total_elec    = step / N_PER_CYCLE
                frac          = step_in_cycle / N_PER_CYCLE
                theta_e_u     = round(frac * 65536) % 65536
                theta_e_raw   = theta_e_u if theta_e_u < 32768 else theta_e_u - 65536
                self.node.sdo['Theta_e'].raw = theta_e_raw
                time.sleep(STEP_S)
                wx.Yield()
                enc   = self.node.sdo['Encoder']['RawPosition'].raw
                delta = enc - enc_prev
                if delta >  enc_resolution / 2: delta -= enc_resolution
                if delta < -enc_resolution / 2: delta += enc_resolution
                enc_accumulated += delta
                enc_prev = enc
                if step < N_TOTAL:  # exclude final return-to-start wrap point
                    expected   = e_polarity * total_elec * cts_per_elec
                    correction = expected - enc_accumulated   # counts to ADD for true pos
                    abs_pos    = (enc_start + enc_accumulated) % enc_resolution
                    cycle_n    = step // N_PER_CYCLE + 1
                    sweep.append((step_in_cycle, cycle_n, correction, abs_pos))

            self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE

            N_S = len(sweep)   # = N_TOTAL

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
            ts = datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
            node_id = getattr(self.node, 'id', '?')
            meta = ("# node={} e_zero={} e_polarity={} enc_res={} motor_poles={} "
                    "cts_per_elec={:.2f} sweep_steps_per_cycle={} harmonics={}\n"
                    "# corrected_pos = raw_pos + table[index]\n"
                    ).format(node_id, e_zero, e_polarity, enc_resolution,
                             motor_poles, cts_per_elec, N_PER_CYCLE, N_HARMONICS)

            full_path = resource_path(os.path.join(
                'logs', 'enc_correction_full_{}.csv'.format(ts)))
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, 'w') as _f:
                _f.write("# Encoder position correction — full mechanical revolution\n")
                _f.write("# index = raw_encoder_pos % enc_resolution\n")
                _f.write(meta)
                _f.write("enc_pos_cts,correction_cts\n")
                for _p, _c in enumerate(table_full):
                    _f.write("{},{}\n".format(_p, _c))

            elec_path = resource_path(os.path.join(
                'logs', 'enc_correction_elec_{}.csv'.format(ts)))
            with open(elec_path, 'w') as _f:
                _f.write("# Encoder position correction — per electrical cycle\n")
                _f.write("# index = (raw_encoder_pos - e_zero) % round(cts_per_elec)\n")
                _f.write(meta)
                _f.write("pos_in_elec_cycle_cts,correction_cts\n")
                for _p, _c in enumerate(table_elec):
                    _f.write("{},{}\n".format(_p, _c))

            print("\n  Full-rev CSV  → {}".format(full_path))
            print("  Elec-cyc CSV  → {}".format(elec_path))

        except Exception as _exc:
            self._cal_fault(_exc)
        finally:
            if self.ADC_ON == False and self.adcWasON:
                self.on_off_adc(self)
            self.Enable()

    def _load_enc_correction_table(self):
        """Return (table, path) from the most recent enc_correction_full CSV, or (None, None)."""
        import os, glob
        from paths import resource_path
        log_dir = resource_path('logs')
        hits = sorted(glob.glob(os.path.join(log_dir, 'enc_correction_full_*.csv')))
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

        mp = dict(e_zero=e_zero, e_polarity=e_polarity,
                  enc_resolution=enc_resolution, cts_per_elec=cts_per_elec,
                  Kt=Kt, Rt=Rt, V_bus=V_bus, i_peak=i_peak)

        print("PVCA Torque Control — table: {} ({} entries)".format(
            os.path.basename(path), len(table)))
        print("  Kt={}mNm/A  Rt={:.2f}Ω  V_bus={:.1f}V  i_peak={}mA".format(
            Kt, Rt, V_bus, i_peak))
        print("  e_zero={}  e_polarity={}  cts_per_elec={:.2f}".format(
            e_zero, e_polarity, cts_per_elec))

        dlg = _PVCATorqueDialog(self, self.node, table, path, mp)
        dlg.ShowModal()
        dlg.Destroy()

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
