"""cal_encoder.py — Encoder test, linearity and lag.

A mixin of the calibrate menu (calibrate_menu.calibrate inherits it): the
dialogs and plots.  The measurements are p4sysid.enctest (test_encoder is
p4core.calibrate.test_encoder).
"""
import math

from p4core.calibrate import CalibrationError
from p4sysid import enctest


class EncoderCal:
    def calibrate_enclag(self, event, calAll=False):  # wxGlade: wxp3_frame.<event_handler>
        """Max-velocity (field-weakening) LagFactor sweep: p4sysid.enctest."""
        if enctest.has_latency_split(self.node):
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

        try:
            if calAll == False:
                self._menu_idle_takeover()   # stop an active puck + reset the drive selector to Idle
            enctest.calibrate_encoder_lag(self.node, self.cal_reporter())
        except Exception as _exc:
            if calAll:
                raise
            self._cal_fault(_exc)

        self.frame_statusbar.SetStatusText("Ready", 1)
        if self.ADC_ON == False and self.adcWasON == True:
            self.on_off_adc(self)
        if calAll == False:
            self.Enable()

    def test_encoder(self, event, calAll=False):
        """Encoder stability at rest: p4core.calibrate.test_encoder."""
        if calAll==False:
          if self.check_for_node() == False:
            return False
          self.Disable()

        if self.ADC_ON == True:
            self.adcWasON = True
            self.on_off_adc(self)
        else:
            self.adcWasON = False

        try:
            enctest.test_encoder(self.node, self.cal_reporter())
            ok = True
        except CalibrationError:
            # Unstable, and the user declined to continue.
            ok = False
        finally:
            self.frame_statusbar.SetStatusText("Ready", 1)
            if self.ADC_ON == False and self.adcWasON == True:
                self.on_off_adc(self)
            if calAll == False:
              self.Enable()
        return ok

    def test_encoder_linearity(self, event):
        """Merged into generate_enc_correction_table — redirect."""
        self.generate_enc_correction_table(event)

    def _test_encoder_linearity_legacy_body(self):
        """The standalone linearity sweep (p4sysid.enctest) and its plot.
        Kept for reference — the menu item runs the correction table."""
        if not self.check_for_node():
            return
        self.Disable()

        if self.ADC_ON:
            self.adcWasON = True
            self.on_off_adc(self)
        else:
            self.adcWasON = False

        try:
            res = enctest.test_encoder_linearity(self.node, self.cal_reporter())
            self._plot_encoder_linearity(res)
        except Exception as _exc:
            self._cal_fault(_exc)

        finally:
            if self.ADC_ON == False and self.adcWasON:
                self.on_off_adc(self)
            self.Enable()

    def _plot_encoder_linearity(self, res):
        """Print the table and save the three-panel plot of a
        test_encoder_linearity result."""
        results = res['results']
        pole_pairs = res['pole_pairs']
        N_PER_CYCLE = res['steps_per_cycle']
        PASS_THRESHOLD = res['threshold']
        passed = res['passed']
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
