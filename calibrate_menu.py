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
)
from canopen.sdo import SdoAbortedError
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
    
                _pc = None
                try:
                    _pc = int(self.node.sdo[0x1018][2].raw)
                except Exception:
                    pass
                _puck_model = getattr(self, '_PRODUCT_CODE_MODELS', {}).get(_pc, 'unknown')
                _node_label = 'Node {}  {}'.format(node_id, _puck_model)

                fig, axes = plt.subplots(2, 3, figsize=(16, 9), sharey=False)
                fig.suptitle(
                    'MaxSettlingTime calibration — ADC deviation vs settling time\n'
                    '{} — fine sweep {} – {} ns, {} ns steps  '
                    '(coarse {} – {} ns, {} ns steps)'.format(
                        _node_label,
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
                from paths import session_path
                plot_path = session_path('itiming_cal_{}.png'.format(
                    time.strftime('%Y-%m-%d_%H-%M-%S')))
                os.makedirs(os.path.dirname(plot_path), exist_ok=True)
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
                _lin_plot_path = session_path('{}enc_linearity_{}.png'.format(_file_pfx, ts))
                os.makedirs(os.path.dirname(_lin_plot_path), exist_ok=True)
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

            full_path = session_path('{}enc_correction_full_{}.csv'.format(_file_pfx, ts))
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, 'w') as _f:
                _f.write("# Encoder position correction — full mechanical revolution\n")
                _f.write("# index = raw_encoder_pos % enc_resolution\n")
                _f.write(meta)
                _f.write("enc_pos_cts,correction_cts\n")
                for _p, _c in enumerate(table_full):
                    _f.write("{},{}\n".format(_p, _c))

            elec_path = session_path('{}enc_correction_elec_{}.csv'.format(_file_pfx, ts))
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
                plot_path = session_path('{}enc_correction_{}.png'.format(_file_pfx, ts))
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
                fft_path = session_path('{}enc_correction_harmonics_{}.json'.format(_file_pfx, ts))
                with open(fft_path, 'w') as _jf:
                    _json.dump(fft_data, _jf, indent=2)
                print("  FFT JSON → {}".format(fft_path))

                # ── Upload top-10 harmonic bins to Puck (0x3027) ─────────────
                N_BINS = 10
                # Top 10 AC harmonics by amplitude (k≥1). DC offset is not uploaded
                # to the puck; it is subtracted from the retest plots for display only.
                _top_bins = sorted_ks[:N_BINS]  # top 10 by amplitude, most impactful first
                n_upload  = len(_top_bins)

                print("\n  Top {} harmonics by amplitude (most impactful first):".format(n_upload))
                print("  Rank  {:>4}  {:>10}".format("k", "Amplitude"))
                print("  " + "-" * 24)
                for _ri, _rk in enumerate(_top_bins):
                    print("  {:>4d}  {:>4d}  {:>10.4f}".format(_ri + 1, _rk, float(amps[_rk])))

                # New OD format (matches firmware pwm.c:correct_pos):
                #   out -= [A_s·sin(kθ) + A_c·cos(kθ)] / 256,  θ = 2π·(pos mod 4096)/4096
                # We need this to reproduce the legacy correction
                #   δ_old(pos) = -_bA · cos(kθ + ψ),   ψ = _bphi - 2π·k·enc_start/enc_resolution
                # Expanding -cos(kθ + ψ) gives:
                #   A_s =  256·_bA·sin(ψ),   A_c = -256·_bA·cos(ψ)   (Q8.8 int16)
                # enc_start offset stays (FFT phase is sweep-relative); the legacy +π sign
                # flip is now absorbed into the signs of A_s/A_c, so no separate flip.
                def _clamp_i16(v):
                    return max(-32768, min(32767, int(round(v))))

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
                    _top10_path = session_path(
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

                # DC bias: mean of bidirectional-averaged retest errors. Friction is
                # canceled by averaging so this is the true encoder geometric mean
                # offset (reads consistently ahead/behind ideal over a full revolution).
                # Not stored to the puck — subtracted from plots only so the AC
                # residual is visible centred on zero.
                _rt_dc_bias = sum(_rt_errs) / len(_rt_errs) if _rt_errs else 0.0
                _rt_errs_ac = [e - _rt_dc_bias for e in _rt_errs]
                _rt_rms_ac  = (sum(e*e for e in _rt_errs_ac) / len(_rt_errs_ac)) ** 0.5
                _impr_ac    = (1.0 - _rt_rms_ac / _lin_rms_err) * 100.0 if _lin_rms_err else 0.0
                print("  DC bias (not stored to puck): {:+.3f}°".format(_rt_dc_bias))
                print("  AC-only RMS: {:.3f}°  ({:.1f}% improvement)".format(
                    _rt_rms_ac, _impr_ac))
                _rt_passed_stat = _rt_rms_ac < _lin_rms_err
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
                    _orig_edeg = [r[3] for r in lin_results]
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
                            _lin_rms_err, _rt_rms_ac, _impr_ac, _rt_dc_bias),
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
                    _cax1.set_title('Before compensation  (RMS={:.3f}°)'.format(
                        _lin_rms_err))
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
                    _cplot_path = session_path(
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
                    _rt_passed    = _rt_rms_ac < _lin_rms_err

                    _rtfig = _rtplt.figure(figsize=(15, 8))
                    _rtfig.suptitle(
                        'Encoder Linearity [COMPENSATION ACTIVE] — Node {}  {}  ({})\n'
                        '{} pole pairs  {} steps/elec cycle  '
                        'AC RMS {:.3f}° → {:.3f}°  ({:.1f}% improvement)'
                        '  |  DC bias {:+.1f}° subtracted'.format(
                            node_id, model_str, ts, pole_pairs, RETEST_N_PER_CYCLE,
                            _lin_rms_err, _rt_rms_ac, _impr_ac, _rt_dc_bias),
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
                    _rt_lin_path = session_path(
                        '{}enc_linearity_retest_{}.png'.format(_file_pfx, ts))
                    os.makedirs(os.path.dirname(_rt_lin_path), exist_ok=True)
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
                    fft_plot_path = session_path('{}enc_correction_fft_{}.png'.format(_file_pfx, ts))
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
                _recon_plot_path = session_path('{}enc_harmonic_recon_{}.png'.format(_file_pfx, ts))
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

        SEND_TO_PUCK = False: no SDO upload; firmware cogging object not yet implemented.
        """
        SEND_TO_PUCK = False  # Set True when firmware 0x3028 cogging object is ready

        if not self.check_for_node():
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

            node_id   = getattr(self.node, 'id', '?')
            _pc       = None
            try:
                _pc = int(self.node.sdo[0x1018][2].raw)
            except Exception:
                pass
            model_str = getattr(self, '_PRODUCT_CODE_MODELS', {}).get(_pc, 'unknown')
            _file_pfx = 'node{}_{}_'.format(node_id, model_str.replace(' ', '_'))

            N_BINS       = 128    # angle bins per revolution (k_max=64; resolves k=21,42,63)
            TARGET_RPM   = 5.0   # mechanical RPM — faster traversal reduces per-tooth velocity ripple
            SETTLE_S     = 2.5   # wait for speed to settle before sampling
            N_REVS       = 2     # mechanical revolutions to collect per direction
            SAMPLE_S     = 0.025 # sampling interval (s)
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

            # ---- Enable in profile-velocity mode ----
            self.node.sdo["ControlWord"].raw = CLEAR_FAULT
            self.node.sdo["ControlWord"].raw = SHUTDOWN
            self.node.sdo["ControlWord"].raw = OP_ENABLED
            self.node.sdo["SetModeOfOperation"].raw = MODE_PROFILE_VEL
            time.sleep(0.2)
            wx.Yield()

            def _sample_pass(vel_cmd, label, upd_start, upd_end):
                """Collect (angle_deg, iq_mA) at each sample tick over N_REVS."""
                print("  {} pass  ({:+d} cts/s = {:.1f} RPM, {} revs) ...".format(
                    label, vel_cmd, abs(vel_cmd) * 60.0 / enc_resolution, N_REVS))
                self.node.sdo['TargetVelocity'].raw = vel_cmd
                _sleep_responsive(SETTLE_S)
                wx.Yield()

                samples = []  # list of (angle_deg, iq_mA)
                for step in range(n_samples):
                    time.sleep(SAMPLE_S)
                    raw_now  = self.node.sdo['Encoder']['RawPosition'].raw
                    iq_norm  = self.node.sdo['CurrentFeedback'].raw  # ±1000 = ±i_peak
                    iq_ma    = iq_norm / 1000.0 * i_peak
                    angle_deg = (raw_now % enc_resolution) / enc_resolution * 360.0
                    samples.append((angle_deg, iq_ma))
                    _upd(upd_start + step * (upd_end - upd_start) // n_samples)
                    wx.Yield()
                return samples

            print("Cogging sweep:")
            samples_fwd = _sample_pass(+vel_cts_per_sec, 'forward', 5, 44)

            self.node.sdo['TargetVelocity'].raw = 0
            _sleep_responsive(1.5)
            wx.Yield()

            samples_rev = _sample_pass(-vel_cts_per_sec, 'reverse', 47, 86)

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
            csv_path = session_path('{}cogging_sweep_{}.csv'.format(_file_pfx, ts))
            os.makedirs(os.path.dirname(csv_path), exist_ok=True)
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
                json_path = session_path('{}cogging_harmonics_{}.json'.format(_file_pfx, ts))
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
                    plot_path = session_path(
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
                    spec_path = session_path(
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

            _upd(98)

            if SEND_TO_PUCK:
                pass  # Future: upload harmonic coefficients to firmware cogging object
            else:
                print("\n  SEND_TO_PUCK = False — compensation not uploaded.")
                print("  Firmware cogging compensation object not yet implemented.")
                print("  Review output files and re-run after firmware update.")

            print("\nCogging characterisation complete.")

        except Exception as _exc:
            self._cal_fault(_exc)
        finally:
            self.OnTaskComplete()
            if self.ADC_ON == False and self.adcWasON:
                self.on_off_adc(self)
            self.Enable()
            _upd(100)

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
