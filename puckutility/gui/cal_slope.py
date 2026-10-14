"""cal_slope.py — Current-sense slope, baseline offset fold, gain ripple and the slope spin test.

A mixin of the calibrate menu (calibrate_menu.calibrate inherits it).  The
measurements, fits and OD writes are p4sysid.slope; this keeps the frame
handling (Disable/Enable, the ADC monitor, dialogs, status bar) and the plots.
"""
import os

from p4core.cia402 import MODE_IDLE
from p4sysid import slope as slope_lib
from p4sysid.drivecal import CalError


def _agg_pyplot():
    import matplotlib
    matplotlib.use('Agg')  # non-interactive; avoids wx/Tk backend conflicts
    import matplotlib.pyplot as plt
    return plt


class SlopeCal:
    def _adc_quiet(self):
        """Quiet the ADC monitor: it contends for SDO reads."""
        if self.ADC_ON == True:
            self.on_off_adc(self)
            self.adcWasON = True
        else:
            self.adcWasON = False

    def _adc_restore(self):
        if self.ADC_ON == False and self.adcWasON == True:
            self.on_off_adc(self)

    def measure_gain_ripple(self, event, calAll=False):  # wxGlade: puckutilityapp_frame.<event_handler>
        """Current-sense offset vs load at the present settling time
        (p4sysid.slope.measure_gain_ripple), plotted to offset_vs_current.png."""
        if calAll == False:
            if self.check_for_node() == False:
                return False
            self.Disable()
        self._adc_quiet()
        try:
            res = slope_lib.measure_gain_ripple(self.node, self.cal_reporter())
            a, b = res['fit_a'], res['fit_b']
            try:
                plt = _agg_pyplot()
                xs = [r['mean'] for r in res['rows']]
                fig, ax = plt.subplots(figsize=(8, 4))
                ax.plot(xs, [r['offset'] for r in res['rows']], 'o-', label='offset')
                ax.plot(xs, [a + b * x for x in xs], 'r--', lw=0.8,
                        label='fit {:.0f}+{:.2f}|I|'.format(a, b))
                ax.set_xlabel('current |I| (mA)'); ax.set_ylabel('stator current OFFSET (mA)')
                ax.set_title('current-sense offset vs load (P4-16, settling={} ns)'.format(
                    res['settling_ns']))
                ax.grid(True, alpha=0.3); ax.legend()
                _path = os.path.abspath('offset_vs_current.png')
                fig.savefig(_path, dpi=110, bbox_inches='tight'); plt.close(fig)
                print("Plot saved: {}".format(_path))
            except Exception as _pe:
                print("(plot skipped: {})".format(_pe))
            self.frame_statusbar.SetStatusText("offset = {:.0f} + {:.2f}|I| mA".format(a, b), 1)
            self._adc_restore()
            if calAll == False:
                self.Enable()
        except Exception as _exc:
            self._adc_restore()
            if calAll:
                raise
            self._cal_fault(_exc)
            self.Enable()

    def fold_baseline_offset(self, event, calAll=False):
        """Apply the last passing Current Sense Slope's drive-on baseline (a0/b0)
        (p4sysid.slope.fold_baseline_offset: the drive-gated offset register on
        fw v3+, else a legacy fold into the Bias)."""
        if calAll == False:
            if self.check_for_node() == False:
                return False
            self.Disable()
        try:
            slope_lib.fold_baseline_offset(
                self.node, dict(stored=getattr(self, '_slope_stored', False),
                                a0=getattr(self, '_slope_a0', None),
                                b0=getattr(self, '_slope_b0', None)),
                self.cal_reporter())
            if calAll == False:
                self.Enable()
            return True
        except CalError as _ce:
            print(_ce)
            if calAll == False:
                self.Enable()
            return False
        except Exception as _exc:
            if calAll:
                raise
            self._cal_fault(_exc)
            self.Enable()
            return False

    def calibrate_current_slope(self, event, calAll=False, force_sdo=False, quick=False):  # wxGlade: puckutilityapp_frame.<event_handler>
        """Calibrate the current-proportional alpha/beta current-sense offset
        (p4sysid.slope.calibrate_current_slope).  Sets self._slope_stored and
        self._slope_a0/_b0 for the baseline fold, and plots the fit."""
        if calAll == False and self.check_for_node() == False:
            return False
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
        self._adc_quiet()
        try:
            res = slope_lib.calibrate_current_slope(
                self.node, self.cal_reporter(), force_sdo=force_sdo, quick=quick)
            self._slope_stored = bool(res.get('stored'))
            if res.get('skipped'):
                self._adc_restore()
                if calAll == False:
                    self.Enable()
                return
            # Stash the fitted drive-on baseline (a0/b0, mA) for the baseline fold.
            self._slope_a0, self._slope_b0 = res['a0'], res['b0']
            self._plot_current_slope(res)
            self.frame_statusbar.SetStatusText(
                "slope kA={:+.3f} kB={:+.3f} (mag {:.3f})".format(
                    res['kA'], res['kB'], res['slope_mag']), 1)
            self._adc_restore()
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
            self._adc_restore()
            if calAll:
                raise
            self._cal_fault(_exc)
            self.Enable()

    def _plot_current_slope(self, res):
        try:
            plt = _agg_pyplot()
            _mi = [r[0] for r in res['rows']]
            a0, b0, kA, kB = res['a0'], res['b0'], res['kA'], res['kB']
            fig, ax = plt.subplots(figsize=(8, 4))
            ax.plot(_mi, [r[1] for r in res['rows']], 'o', color='C0', label='off_alpha')
            ax.plot(_mi, [r[2] for r in res['rows']], 's', color='C1', label='off_beta')
            _xf = [min(_mi), max(_mi)] if _mi else [0, 1]
            ax.plot(_xf, [a0 + kA * x for x in _xf], '--', color='C0', lw=0.8,
                    label='fit a: {:+.0f}+{:.3f}|I|'.format(a0, kA))
            ax.plot(_xf, [b0 + kB * x for x in _xf], '--', color='C1', lw=0.8,
                    label='fit b: {:+.0f}+{:.3f}|I|'.format(b0, kB))
            ax.set_xlabel('current |I| (mA)'); ax.set_ylabel('current-sense offset (mA)')
            ax.set_title('Current Sense Slope: mag={:.3f} mA/mA, dir={:.0f} deg'.format(
                res['slope_mag'], res['direction_deg']))
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

    def calibrate_slope_spin_test(self, event, calAll=False):  # wxGlade: <event_handler>
        """Stepped-hold raw alpha/beta circle-fit slope (p4sysid.slope.slope_spin_test).
        Measure-only: the slope is cleared and left cleared."""
        if self.check_for_node() == False:
            return False
        if calAll == False:
            self._menu_idle_takeover()
        self.Disable()
        self._adc_quiet()
        try:
            slope_lib.slope_spin_test(self.node, self.cal_reporter())
        except Exception as _e:
            print("Slope stepped-hold error: {}".format(_e))
        finally:
            self._adc_restore()
            if calAll == False:
                self.Enable()

    def calibrate_islope(self, event):  # wxGlade: wxp3_frame.<event_handler>
        print("Event handler 'calibrate_islope' not implemented!")
        event.Skip()
