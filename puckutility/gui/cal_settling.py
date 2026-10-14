"""cal_settling.py — ADC settling time (MaxSettlingTime) calibration.

A mixin of the calibrate menu (calibrate_menu.calibrate inherits it).  The
measurement and the pick are p4sysid.settling; this is the menu around it:
the firmware check, ADC-monitor gating, the plot and the offer to re-run the
current-sense cals the new timing made stale.
"""
import os
import webbrowser

import wx
from p4core.cia402 import CLEAR_FAULT, MODE_IDLE
from p4sysid import settling


class SettlingCal:
    def calibrate_itiming(self, event, calAll=False):  # wxGlade: wxp3_frame.<event_handler>
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
        # per-angle sampling — quiet it here and restore it at the end AND in the except.
        if self.ADC_ON == True:
            self.on_off_adc(self)
            self.adcWasON = True
        else:
            self.adcWasON = False

        try:
            res = settling.calibrate_settling(
                self.node, self.cal_reporter(), clear_stale_slope=not calAll,
                i_cont_fallback=int(getattr(self, 'i_cont', 0) or 0))
            self._plot_settling(res)
            if res['changed'] and not calAll:
                self._settling_chain(res['original_ns'], res['settling_ns'])
            self.frame_statusbar.SetStatusText(
                "Current timing calibrated: {} ns".format(res['settling_ns']), 1)
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

    def _settling_chain(self, original, optimal):
        """Settling just invalidated Bias/Gain/Slope, so offer (or, with the enforce flag,
        automatically run) the follow-on current-sense re-cal right now -- the puck is never
        left half-calibrated. Each chained step uses calAll=True to match calibrate_all's
        sequencing. Enforce path (self.settling_autochain = True) skips the prompt."""
        _enforce = bool(getattr(self, 'settling_autochain', False))
        _do_chain = _enforce
        if not _enforce:
            _do_chain = (wx.MessageBox(
                "MaxSettlingTime changed {} -> {} ns, so iSense Bias/Gain and the Current "
                "Sense Slope are now stale.\n\nRe-calibrate current sense now "
                "(Bias → Gain → Slope)?".format(original, optimal),
                "Re-calibrate Current Sense?", wx.YES_NO | wx.ICON_QUESTION) == wx.YES)
        if not _do_chain:
            return
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
                      .format(optimal))
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

    def _plot_settling(self, res):
        """Debug plot of the settling sweep, saved to the session log (timing/images/)."""
        an = res['analysis']
        _srt = an['rows']
        _fit = an['fit']
        _ref = an['ref']
        _np_r = len(_ref)
        _band = an['band']
        _resolvable = res['resolvable']
        optimal_settling = res['settling_ns']
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

            _xs = [r['settling'] for r in _srt]
            _ringp = [(r.get('ring') if r.get('ring') is not None else float('nan')) for r in _srt]
            _mean = [r['meanI'] for r in _srt]

            fig, (ax, ax2, ax3) = plt.subplots(1, 3, figsize=(23, 6))
            axr = ax.twinx()
            # LEFT PANEL: the residual RING (worst-pattern deviation from the ring-free end) = the pick
            # driver, after thermal detrend. |αβ| on the twin axis is the window-collapse guard.
            ax.plot(_xs, _ringp, '-o', color='tomato', markersize=6, linewidth=1.8,
                    label='residual ring (worst pattern) — falls to noise = cleared')
            ax.axhline(_band, color='gray', linestyle='--', linewidth=0.9, alpha=0.6,
                       label='cleared <= {:.1f}'.format(_band))
            axr.plot(_xs, _mean, '--s', color='steelblue', markersize=5,
                     linewidth=1.0, alpha=0.6, label='|αβ| magnitude (counts, window collapse)')
            # Fitted decay envelope + settled-threshold: the pick is where the smooth fit crosses it.
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
                if res['max_duty_pct'] is not None:
                    _result_note += '  (~{:.0f}% max duty)'.format(res['max_duty_pct'])
            else:
                _result_note = 'NOT RESOLVABLE (kept {} ns)'.format(res['original_ns'])

            ax.set_title('MaxSettlingTime cal — measured ring-clear (detrended)\n'
                         '{}   (drive {} mA, ud {})   |   RESULT: {}'.format(
                             _node_label, res['calibration_current_ma'], res['drive_ud'],
                             _result_note),
                         fontsize=11)
            ax.set_xlabel('MaxSettlingTime (ns)')
            ax.set_ylabel('residual ring (counts) — worst pattern', color='tomato')
            axr.set_ylabel('|αβ| magnitude (counts)', color='steelblue')
            ax.grid(True, alpha=0.25)
            _l1, _b1 = ax.get_legend_handles_labels()
            _l2, _b2 = axr.get_legend_handles_labels()
            ax.legend(_l1 + _l2, _b1 + _b2, fontsize=8, loc='upper right')

            # MIDDLE PANEL: the RAW sample cloud — every per-sample |αβ| residual (deviation from its
            # own settling's mean) at its settling. Envelope = ±1σ per settling.
            _nsamp = max((len(r.get('samples') or []) for r in _srt), default=0)
            for r in _srt:
                _sm = r.get('samples') or []
                if _sm:
                    ax2.scatter([r['settling']] * len(_sm), _sm, s=4, color='purple',
                                alpha=0.12, edgecolors='none')
            _ex, _ehi, _elo = [], [], []
            for r in _srt:
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

            # RIGHT PANEL: the PER-PATTERN ring, referenced to the RING-FREE END (the same reference
            # the pick uses). LARGE at low settling, falling to the reference noise once cleared.
            if _np_r > 0:
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
            from ..paths import session_path
            import datetime as _dt
            _model2 = _puck_model.replace(' ', '_')
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
