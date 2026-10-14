"""cal_enctable.py — Encoder correction table.

A mixin of the calibrate menu (calibrate_menu.calibrate inherits it).  The
sweep, the fit, the bin selection, the 0x3027 upload, the retest and the
revert are p4sysid.enctable; this is the menu around it: the old-firmware
notice, the recalibrate/retest choice when compensation is already on, the
file names, the menubar ON/OFF marks, and the plots.
"""
import datetime
import math
import os

import wx
from p4sysid import enctable
from p4sysid.enctable import PASS_THRESHOLD, N_PER_CYCLE, N_HARMONICS

from .cal_common import GuiCalReporter


class _TableReporter(GuiCalReporter):
    """The menu's reporter, with progress onto the frame's gauge."""

    def progress(self, pct, text=None):
        try:
            self.frame.UpdateUI(pct)
        except Exception:
            pass
        super().progress(pct, text)


class EncTableCal:
    def generate_enc_correction_table(self, event):
        """
        Encoder sweep → position correction tables and the 0x3027 harmonic
        bins (p4sysid.enctable.generate_correction_table), then the plots.

          enc_correction_full_*.csv  — one signed-int entry per encoder count
          enc_correction_elec_*.csv  — one signed-int entry per count within one
                                       electrical cycle (electrical errors only)

        Table format:  corrected_pos = raw_pos + table[raw_pos % period]
        """
        if not self.check_for_node():
            return

        # Firmware < 4.4.0 has no encoder-compensation object (0x3027) to write, but the SWEEP still
        # yields useful info: the measured encoder-error harmonics + linearity reveal magnet/encoder
        # MISALIGNMENT. So on old firmware we run TEST-ONLY -- measure + report, skip the upload.
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
            from ..paths import session_path
            ts = datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
            node_id = getattr(self.node, 'id', '?')
            _pc = None
            try:
                _pc = int(self.node.sdo[0x1018][2].raw)
            except Exception:
                pass
            model_str = getattr(self, '_PRODUCT_CODE_MODELS', {}).get(_pc, 'unknown')
            _file_pfx = 'node{}_{}_'.format(node_id, model_str.replace(' ', '_'))

            def _enc_path(kind, name):
                p = session_path('encoder/{}/{}'.format(kind, name))
                os.makedirs(os.path.dirname(p), exist_ok=True)
                return p

            def _enc_data(name):
                stem, ext = os.path.splitext(name)
                return _enc_path('data', '{}{}_{}{}'.format(_file_pfx, stem, ts, ext))

            def _enc_img(stem):
                return _enc_path('images', '{}{}_{}.png'.format(_file_pfx, stem, ts))

            ctx = {'node_id': node_id, 'model': model_str, 'ts': ts, 'img': _enc_img}
            res = {}
            try:
                enctable.generate_correction_table(
                    self.node, _TableReporter(self), retest_only=_retest_only,
                    test_only=_test_only,
                    skip_k=getattr(self, 'enc_comp_skip_k', ()),
                    gear_ratio=getattr(self, 'gearRatio', 1.0),
                    data_path=_enc_data, model=model_str, timestamp=ts,
                    result=res)
            finally:
                # Whatever was measured before a failure still gets drawn.
                self._enc_table_plots(res, ctx)
            if res.get('comp_active') is not None:
                try:
                    self.frame_menubar.ON.Check(bool(res['comp_active']))
                    self.frame_menubar.OFF.Check(not res['comp_active'])
                except Exception:
                    pass
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
        from ..paths import resource_path
        return enctable.load_latest_correction_table(resource_path('logs'))

    # ---- plots (drawn from what p4sysid.enctable returned) -------------------
    def _enc_table_plots(self, res, ctx):
        if 'lin' not in res:
            return
        steps = [('Linearity plot', self._plot_enc_linearity)]
        if 'tables' in res:
            steps.append(('Plot', self._plot_enc_correction))
        if 'retest' in res:
            steps += [('Comparison plot', self._plot_enc_comparison),
                      ('Retest linearity plot', self._plot_enc_retest),
                      ('FFT plot', self._plot_enc_fft)]
        if 'tables' in res:
            steps.append(('Reconstruction plot', self._plot_enc_recon))
        for label, fn in steps:
            try:
                path = fn(res, ctx)
                print("  {} → {}".format(label, path))
            except ImportError as _pe:
                print("  ({} skipped — matplotlib not installed: {})".format(label, _pe))
            except Exception as _pe:
                print("  WARNING: {} failed: {}".format(label, _pe))

    @staticmethod
    def _plt():
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        return plt

    def _lissajous(self, ax, results, n_cycle, colors):
        """Per-electrical-cycle error drawn around the unit circle; returns
        the radial scale."""
        cyc_base = {}
        for r in results:
            if int(round(r[1] / 360.0 * n_cycle)) % n_cycle == 0:
                cyc_base.setdefault(r[2], r[3])
        profiles = {}
        for r in results:
            s = int(round(r[1] / 360.0 * n_cycle)) % n_cycle
            profiles.setdefault(r[2], {})[s] = r[3] - cyc_base.get(r[2], 0.0)
        all_wc = [e for d in profiles.values() for e in d.values()]
        max_ae = max(abs(e) for e in all_wc) if all_wc else 1.0
        scale = 0.4 / (max_ae or 1.0)
        circ = [i / 360 * 2 * math.pi for i in range(361)]
        ax.plot([math.cos(t) for t in circ], [math.sin(t) for t in circ],
                'k', linewidth=2.5, linestyle=':', label='Ideal', zorder=5)
        for cn in sorted(profiles.keys()):
            prof = profiles[cn]
            if len(prof) < n_cycle:
                continue
            st = sorted(prof.keys())
            ts2 = [s / n_cycle * 2.0 * math.pi for s in st] + [0.0]
            es2 = [prof[s] for s in st] + [0.0]
            ax.plot([(1.0 + scale * e) * math.cos(t) for t, e in zip(ts2, es2)],
                    [(1.0 + scale * e) * math.sin(t) for t, e in zip(ts2, es2)],
                    color=colors[(cn - 1) % 10], alpha=0.7, linewidth=0.9,
                    label='Cycle {}'.format(cn))
        ax.set_aspect('equal')
        ax.axhline(0, color='gray', linewidth=0.5, zorder=0)
        ax.axvline(0, color='gray', linewidth=0.5, zorder=0)
        ax.set_xlabel('cos(ε)')
        ax.set_ylabel('sin(ε)')
        return scale

    def _plot_lin3(self, results, n_cycle, pole_pairs, title, color, verdict,
                   sub_titles, path):
        """The 3-panel linearity figure (error, per-cycle overlay, Lissajous)."""
        plt = self._plt()
        import matplotlib.gridspec as gridspec
        fig = plt.figure(figsize=(15, 8))
        fig.suptitle(title, fontsize=13)
        gs = gridspec.GridSpec(2, 2, figure=fig, width_ratios=[1.2, 1])
        ax1 = fig.add_subplot(gs[0, 0])
        ax2 = fig.add_subplot(gs[1, 0])
        ax3 = fig.add_subplot(gs[:, 1])
        pf_label = 'Pass/Fail limit (±{:.0f}°)'.format(PASS_THRESHOLD)
        ax1.plot([r[0] for r in results], [r[3] for r in results], color + '-',
                 linewidth=0.8, label='Measured error')
        ax1.axhline(0, color='k', linewidth=2.5, linestyle=':',
                    label='Expected (0° error)', zorder=5)
        ax1.axhline(PASS_THRESHOLD, color='r', linewidth=1.5, linestyle='--',
                    label=pf_label)
        ax1.axhline(-PASS_THRESHOLD, color='r', linewidth=1.5, linestyle='--')
        ax1.set_xlabel('Mechanical angle (°)')
        ax1.set_ylabel('Error (° electrical)')
        ax1.set_title(sub_titles[0].format(verdict))
        ax1.legend(fontsize=8)
        ax1.grid(True, alpha=0.3)

        colors = plt.cm.tab10.colors
        for cyc in range(1, pole_pairs + 2):
            pts = [(r[1], r[3]) for r in results if r[2] == cyc]
            if pts:
                xs, ys = zip(*pts)
                ax2.plot(xs, ys, color=colors[(cyc - 1) % 10], alpha=0.8,
                         linewidth=0.9, label='Cycle {}'.format(cyc))
        ax2.axhline(0, color='k', linewidth=2.5, linestyle=':', zorder=5)
        ax2.axhline(PASS_THRESHOLD, color='r', linewidth=1.5, linestyle='--',
                    label=pf_label)
        ax2.axhline(-PASS_THRESHOLD, color='r', linewidth=1.5, linestyle='--')
        ax2.set_xlabel('Electrical angle (°)')
        ax2.set_ylabel('Error (° electrical)')
        ax2.set_title(sub_titles[1])
        ax2.legend(fontsize=7, ncol=4)
        ax2.grid(True, alpha=0.3)

        scale = self._lissajous(ax3, results, n_cycle, colors)
        ax3.set_title(sub_titles[2].format(verdict, 1.0 / scale))
        ax3.legend(fontsize=7, ncol=4)
        ax3.grid(True, alpha=0.3)
        plt.tight_layout()
        plt.savefig(path, dpi=100)
        plt.close(fig)
        return path

    def _plot_enc_linearity(self, res, ctx):
        lin, n_cycle = res['lin'], res['n_per_cycle']
        pp = res['setup']['pole_pairs']
        retest = res['mode'] == 'retest'
        return self._plot_lin3(
            lin['results'], n_cycle, pp,
            'Encoder Linearity{}— Node {}  {}  ({})\n'
            '{} pole pairs  {} steps/elec cycle'.format(
                ' [COMPENSATION ACTIVE] ' if retest else ' ',
                ctx['node_id'], ctx['model'], ctx['ts'], pp, n_cycle),
            'b', 'PASS' if lin['passed'] else 'FAIL',
            ('Encoder linearity — error vs mechanical angle  [{}]',
             'Overlaid by electrical cycle — '
             'consistent = electrical error; shifting = mechanical error',
             'Encoder Lissajous (per elec. cycle)  [{}]\n'
             '{:.0f}°/unit  —  overlap=electrical err, spread=mechanical err'),
            ctx['img']('enc_linearity'))

    def _plot_enc_retest(self, res, ctx):
        rt = res['retest']
        pp = res['setup']['pole_pairs']
        dc = rt['dc_bias']
        results = [(r[0], r[1], r[2], r[3] - dc) for r in rt['results']]
        # The Lissajous subtracts each cycle's base, so the DC shift above
        # does not move it: drawn as before from the bias-removed errors.
        return self._plot_lin3(
            results, rt['n_per_cycle'], pp,
            'Encoder Linearity [COMPENSATION ACTIVE] — Node {}  {}  ({})\n'
            '{} pole pairs  {} steps/elec cycle  '
            'AC RMS {:.3f}° → {:.3f}°  ({:.1f}% improvement)'
            '  |  DC bias {:+.1f}° subtracted'.format(
                ctx['node_id'], ctx['model'], ctx['ts'], pp, rt['n_per_cycle'],
                rt['lin_rms_ac'], rt['rms_ac'], rt['impr_ac'], dc),
            'g', 'PASS — AC RMS improved' if rt['passed'] else 'FAIL — no improvement',
            ('Compensated encoder linearity — error vs mechanical angle  [{}]',
             'Compensated — overlaid by electrical cycle\n'
             'consistent = residual electrical err; shifting = mechanical err',
             'Compensated Lissajous (per elec. cycle)  [{}]\n'
             '{:.0f}°/unit  —  residual electrical/mechanical error'),
            ctx['img']('enc_linearity_retest'))

    def _plot_enc_correction(self, res, ctx):
        plt = self._plt()
        tb, sweep, st = res['tables'], res['sweep'], res['setup']
        pp, cpe, enc_res = st['pole_pairs'], st['cts_per_elec'], st['enc_res']
        n_s = len(sweep)
        fig, axes = plt.subplots(2, 2, figsize=(14, 10))
        fig.suptitle(
            'Encoder Correction Table — Node {}  {}  ({})\n'
            '{} pole pairs  {:.2f} cts/elec  '
            '{} steps/cycle  {} harmonics'.format(
                ctx['node_id'], ctx['model'], ctx['ts'],
                pp, cpe, N_PER_CYCLE, N_HARMONICS))
        colors = plt.cm.tab10.colors

        # Top-left: raw correction vs mechanical angle + full-rev fit
        ax = axes[0, 0]
        mech_degs = [i / n_s * 360.0 for i in range(n_s)]
        ax.plot(mech_degs, tb['corr_seq'], 'b.', markersize=2, alpha=0.5, label='Measured')
        ax.plot(mech_degs, [tb['full'][sweep[i][3]] for i in range(n_s)], 'r-',
                linewidth=1.5, label='Fourier fit ({} harmonics)'.format(N_HARMONICS))
        ax.set_xlabel('Mechanical angle (°)')
        ax.set_ylabel('Correction (cts)')
        ax.set_title('Full-revolution: measured vs fit  '
                     '[dominant error: {}]'.format(tb['err_type'].split()[0]))
        ax.legend(fontsize=8)
        ax.grid(True, alpha=0.3)

        # Top-right: per-cycle overlay (spread = mechanical error)
        ax = axes[0, 1]
        for cyc in range(1, pp + 1):
            pts = [(s[0], s[2] - tb['cyc_base'].get(s[1], 0.0))
                   for s in sweep if s[1] == cyc]
            if pts:
                xs, ys = zip(*pts)
                ax.plot([x / N_PER_CYCLE * 360.0 for x in xs], ys,
                        color=colors[(cyc - 1) % 10], alpha=0.7,
                        linewidth=0.9, label='Cycle {}'.format(cyc))
        ax.plot([i / N_PER_CYCLE * 360.0 for i in range(N_PER_CYCLE)],
                tb['elec_avg'], 'k-', linewidth=2, label='Cycle avg')
        ax.set_xlabel('Electrical angle (°)')
        ax.set_ylabel('Correction − cycle base (cts)')
        ax.set_title('Per-cycle overlay  (overlap=electrical err, spread=mechanical err)')
        ax.legend(fontsize=7, ncol=4)
        ax.grid(True, alpha=0.3)

        # Bottom-left: full table indexed by encoder position
        ax = axes[1, 0]
        ax.plot(range(enc_res), tb['full'], 'b-', linewidth=0.8)
        ax.axhline(0, color='k', linewidth=0.5, linestyle='--')
        ax.set_xlabel('Encoder position (cts)')
        ax.set_ylabel('Correction (cts)')
        ax.set_title('Full-rev table  ({} entries  max={:+d} cts  RMS={:.1f} cts)'.format(
            enc_res, tb['max_full'], tb['rms_full']))
        ax.grid(True, alpha=0.3)

        # Bottom-right: electrical-cycle table + raw averaged data
        ax = axes[1, 1]
        elec_sz = tb['elec_size']
        ax.plot([i / N_PER_CYCLE * elec_sz for i in range(N_PER_CYCLE)],
                tb['elec_avg'], 'k.', markersize=4, alpha=0.7, label='Cycle avg')
        ax.plot(range(elec_sz), tb['elec'], 'g-', linewidth=1.5, label='Elec-cycle table')
        ax.axhline(0, color='k', linewidth=0.5, linestyle='--')
        ax.set_xlabel('Position within electrical cycle (cts)')
        ax.set_ylabel('Correction (cts)')
        ax.set_title('Electrical-cycle table  ({} entries  max={:+d} cts  RMS={:.1f} cts)'.format(
            elec_sz, tb['max_elec'], tb['rms_elec']))
        ax.legend(fontsize=8)
        ax.grid(True, alpha=0.3)

        plt.tight_layout()
        path = ctx['img']('enc_correction')
        plt.savefig(path, dpi=100)
        plt.close()
        return path

    def _plot_enc_comparison(self, res, ctx):
        plt = self._plt()
        rt, lin = res['retest'], res['lin']
        rt_mech = [r[0] for r in rt['results']]
        rt_edeg = [r[3] - rt['dc_bias'] for r in rt['results']]
        orig_mech = [r[0] for r in lin['results']]
        orig_edeg = [r[3] - rt['lin_dc_bias'] for r in lin['results']]
        yr = max(max(abs(e) for e in orig_edeg),
                 max(abs(e) for e in rt_edeg)) * 1.15 or 1.0
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5), sharey=True)
        fig.suptitle(
            'Encoder Linearity Comparison — Node {}  {}  ({})\n'
            'Before vs After Compensation  '
            '(RMS: {:.3f}° → {:.3f}° AC  {:.1f}% improvement'
            '  |  DC bias {:+.1f}° subtracted from plot)'.format(
                ctx['node_id'], ctx['model'], ctx['ts'],
                rt['lin_rms_ac'], rt['rms_ac'], rt['impr_ac'], rt['dc_bias']),
            fontsize=11)
        for ax, xs, ys, c in ((ax1, orig_mech, orig_edeg, 'b-'),
                              (ax2, rt_mech, rt_edeg, 'g-')):
            ax.plot(xs, ys, c, linewidth=0.8)
            ax.axhline(0, color='k', linewidth=0.8, linestyle='--')
            ax.axhline(PASS_THRESHOLD, color='r', linewidth=1.0, linestyle='--',
                       label='±{:.0f}° limit'.format(PASS_THRESHOLD))
            ax.axhline(-PASS_THRESHOLD, color='r', linewidth=1.0, linestyle='--')
            ax.set_ylim(-yr, yr)
            ax.set_xlabel('Mechanical angle (°)')
            ax.legend(fontsize=8)
            ax.grid(True, alpha=0.3)
        ax1.set_ylabel('Error (° electrical)')
        ax1.set_title('Before compensation  (AC RMS={:.3f}°)'.format(rt['lin_rms_ac']))
        ax2.set_title('[COMPENSATION ACTIVE]  (AC RMS={:.3f}°  DC bias {:+.1f}° removed)'.format(
            rt['rms_ac'], rt['dc_bias']))
        plt.tight_layout()
        path = ctx['img']('enc_linearity_compensation_active')
        plt.savefig(path, dpi=100)
        plt.close(fig)
        return path

    def _plot_enc_fft(self, res, ctx):
        plt = self._plt()
        an = res['analysis']
        amps, best_n, rms_curve = an['amps'], an['best_n'], an['rms_curve']
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 4))
        fig.suptitle('Encoder Error Spectrum — Node {}  {}  ({})'.format(
            ctx['node_id'], ctx['model'], ctx['ts']), fontsize=11)
        k_show = min(64, len(amps) - 1)
        ax1.bar(range(1, k_show + 1), amps[1:k_show + 1], color='steelblue', width=0.8)
        ax1.axvline(best_n, color='red', linestyle='--', label='N={} (RMS<1ct)'.format(best_n))
        ax1.set_xlabel('Harmonic k')
        ax1.set_ylabel('Amplitude (cts)')
        ax1.set_title('Harmonic amplitudes  ({} sweep pts)'.format(an['N']))
        ax1.legend(fontsize=8)
        ax1.grid(True, alpha=0.3)
        ax2.plot(range(1, len(rms_curve) + 1), rms_curve, 'b-o', markersize=3)
        ax2.axhline(1.0, color='red', linestyle='--', label='1 ct threshold')
        ax2.axvline(best_n, color='red', linestyle=':', label='N={}'.format(best_n))
        ax2.set_xlabel('Number of harmonics')
        ax2.set_ylabel('RMS error (cts)')
        ax2.set_title('Reconstruction RMS vs harmonic count')
        ax2.legend(fontsize=8)
        ax2.grid(True, alpha=0.3)
        plt.tight_layout()
        path = ctx['img']('enc_correction_fft')
        plt.savefig(path, dpi=100)
        plt.close(fig)
        return path

    def _plot_enc_recon(self, res, ctx):
        plt = self._plt()
        cpe = res['setup']['cts_per_elec']
        rc = enctable.reconstruction(res['tables']['corr_seq'], cpe)
        mech, err, best = rc['mech_deg'], rc['err_deg'], rc['best_n']
        yr = float(abs(err).max()) * 1.15 or 1.0
        bin_colors = {3: 'tab:red', 5: 'tab:orange', 10: 'tab:purple', best: 'darkgreen'}
        extra = ['tab:cyan', 'tab:brown', 'tab:pink']
        i = 0
        for nb in rc['counts']:
            if nb not in bin_colors:
                bin_colors[nb] = extra[i % len(extra)]
                i += 1
        fig, axes = plt.subplots(2, 2, figsize=(16, 10))
        fig.suptitle(
            'Harmonic Reconstruction vs Raw Encoder Error — Node {}  {}  ({})\n'
            'Top-3 k={}  |  Best-{} harmonics (RMS<1ct vs raw sweep)'.format(
                ctx['node_id'], ctx['model'], ctx['ts'],
                '/'.join(str(k) for k in rc['top_ks']), best),
            fontsize=11)

        ax = axes[0, 0]
        ax.plot(mech, err, 'b-', linewidth=0.7, label='Raw sweep error')
        ax.axhline(0, color='k', linewidth=0.8, linestyle='--')
        ax.set_ylim(-yr, yr)
        ax.set_xlabel('Mechanical angle (°)')
        ax.set_ylabel('Error (° electrical)')
        ax.set_title('Original measured encoder error  ({} pts)'.format(rc['N']))
        ax.legend(fontsize=8)
        ax.grid(True, alpha=0.3)

        ax = axes[0, 1]
        ax.plot(mech, err, 'b-', linewidth=0.6, alpha=0.4, label='Raw sweep (reference)')
        for nb in rc['counts']:
            ax.plot(mech, rc['recon_deg'][nb], color=bin_colors[nb],
                    linewidth=1.6 if nb == best else 1.0,
                    label='Top-{}{} (RMS={:.3f}°)'.format(
                        nb, ' ✓best' if nb == best else '', rc['rms_deg'][nb]))
        ax.axhline(0, color='k', linewidth=0.8, linestyle='--')
        ax.set_ylim(-yr, yr)
        ax.set_xlabel('Mechanical angle (°)')
        ax.set_ylabel('Error (° electrical)')
        ax.set_title('Reconstruction: top-3 / 5 / 10 / best-{}'.format(best))
        ax.legend(fontsize=8)
        ax.grid(True, alpha=0.3)

        ax = axes[1, 0]
        hcolors = ['tab:orange', 'tab:green', 'tab:purple']
        for hi, (wave, k, a) in enumerate(zip(rc['waves_deg'], rc['top_ks'], rc['top_amps'])):
            ax.plot(mech, wave, color=hcolors[hi % len(hcolors)], linewidth=1.2,
                    label='k={} ({:.3f} cts / {:.2f}°)'.format(k, a, a / cpe * 360.0))
        ax.axhline(0, color='k', linewidth=0.8, linestyle='--')
        ax.set_xlabel('Mechanical angle (°)')
        ax.set_ylabel('Error contribution (° electrical)')
        ax.set_title('Individual top-3 harmonic components')
        ax.legend(fontsize=8)
        ax.grid(True, alpha=0.3)

        ax = axes[1, 1]
        for nb in rc['counts']:
            ax.plot(mech, rc['resid_deg'][nb], color=bin_colors[nb],
                    linewidth=1.6 if nb == best else 0.9, alpha=0.85,
                    label='Residual top-{}{} (RMS={:.3f}°)'.format(
                        nb, ' ✓best' if nb == best else '', rc['rms_deg'][nb]))
        ax.axhline(0, color='k', linewidth=0.8, linestyle='--')
        ax.set_title('Residual after harmonic removal')
        ax.set_xlabel('Mechanical angle (°)')
        ax.set_ylabel('Residual (° electrical)')
        ax.legend(fontsize=8)
        ax.grid(True, alpha=0.3)

        plt.tight_layout()
        path = ctx['img']('enc_harmonic_recon')
        plt.savefig(path, dpi=100)
        plt.close(fig)
        return path
