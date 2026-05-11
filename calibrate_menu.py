# calibrate.py
import wx
import canopen
import time
import math
import webbrowser
import configparser
import platform
from canopen_runner import (
    CLEAR_FAULT, SHUTDOWN, OP_ENABLED,
    MODE_IDLE, MODE_PHASE_VOLTAGE_ANGLE, MODE_PROFILE_TRQ,
)
from cli_ops import _resolve_path, FIRMWARE_DIR, CONFIG_DIR

# TODO - No active issues


def _sleep_responsive(seconds, chunk=0.05):
    """Block for `seconds` seconds while letting wx process pending
    events every `chunk` seconds — keeps Windows from marking the app
    "Not Responding" during long calibration waits."""
    end = time.time() + seconds
    while time.time() < end:
        time.sleep(min(chunk, max(0, end - time.time())))
        wx.Yield()


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

        dlg = wx.MessageDialog(None, _msg, "Calibration Fault", wx.OK | wx.ICON_ERROR)
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
                dlg = wx.MessageDialog(None, msg, 'Warning!', wx.YES_NO | wx.ICON_WARNING)
                answer = dlg.ShowModal()
                dlg.Destroy()
                print("iSense Bias readings out of bounds...")

            _upd(100)
            if self.ADC_ON == False and self.adcWasON == True:
                self.on_off_adc(self)
            if calAll == False:
                self.OnTaskComplete()
                self.Enable()
            if out_of_bounds:
                return answer == wx.ID_YES
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
                    _correction = int(motor_ud * _err / _id_now / 4)
                else:
                    _correction = 200 if _err > 0 else -200
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
                    _correction = int(motor_ud * _err / _id_now / 4)
                else:
                    _correction = 200 if _err > 0 else -200
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

            # Compute gainfactor in full float precision; round only for firmware write
            gainfactor = 4096.0 * (a_filt_f - abias_f) / (b_filt_f - bbias_f)
            gainfactor = round(gainfactor)
            self.node.sdo['Beta']['Gainfactor'].raw = gainfactor
            print("New Beta Gainfactor = {0}  (a_delta={1:.3f}  b_delta={2:.3f})".format(
                gainfactor, a_filt_f - abias_f, b_filt_f - bbias_f))

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
                dlg = wx.MessageDialog(None, msg, 'Warning!', wx.YES_NO | wx.ICON_WARNING)
                answer = dlg.ShowModal()
                dlg.Destroy()
                if answer == wx.ID_NO:
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
          self.node.sdo['Theta_e'].raw = -0x1000 # -pi/2
  
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
          print("After approaching theta_e = 0 from -90°, Encoder raw = {0}".format(pos1))

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
          print("After approaching theta_e = 0 from +90°, Encoder raw = {0}".format(pos2))
          zeroPos2 = self.node.sdo['PositionFeedback'].raw
  
          # Take the average of the two measurements, store e_zero
          encoder_resolution = self.node.sdo['EncoderConfig']['Resolution'].raw
          motor_poles = self.node.sdo['Calibration']['poles'].raw
          cts_per_elec_cyc = encoder_resolution * 2 / motor_poles
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
  
          previous_zero = self.node.sdo['Calibration']['e_zero'].raw
  
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

        if(posDif <= maxDif):
          # Good, passed test
          pass
        if(posDif > maxDif):
          # Bad Encoder reading (error dialog! debug steps)
          # Offer to continue or cancel calibration?
          msg = "Encoder Readings Unstable! \n\nEncoder variation: {} counts" \
          "\nMax Acceptable Variation: {} counts" \
          "\n\nDebugging steps:" \
          "\n- Ensure magnet to encoder spacing is 1.5mm +/- 0.5mm" \
          "\n- Verify magnet concentric to the shaft and rotates properly" \
          "\n\nWould you like to continue calibration?"  .format(posDif,maxDif)
          dlg = wx.MessageDialog(None,msg,'Warning!',wx.YES_NO | wx.ICON_WARNING)
          answer = dlg.ShowModal()
          dlg.Destroy()
          print("Encoder readings unstable...")

        self.frame_statusbar.SetStatusText("Ready", 1)

        if self.ADC_ON == False and self.adcWasON == True:
            self.on_off_adc(self)
        if calAll==False:
          self.Enable()
        try:
          if answer == wx.ID_YES:
              return True
          if answer == wx.ID_NO:
              return False
        except:
          pass

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
