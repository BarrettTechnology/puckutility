#!/usr/bin/env python3
"""plot_outsweep.py -- graph the hold-twitch vs GEARBOX OUTPUT ANGLE from twitch_test --out-sweep data.

Reads the JSON that `twitch_test.py --out-sweep N --repeat R` dumps and renders:
  * a POLAR map (output shaft angle around the circle) -- bar height = ring PROBABILITY across reps,
    colour = mean velocity ripple (vel_rms). Deterministic ring zones stand out as tall/hot sectors.
  * a CARTESIAN panel -- ring probability (bars) + mean vel_rms with min..max band (line) vs output angle.

More reps -> finer probability (1/R resolution) -> cleaner separation of DETERMINISTIC (always rings =
gearbox mesh/backlash zone) from STOCHASTIC. Pass ONE json from a single run (home drifts between runs,
so angles only line up within one run).

Usage:
  scripts/plot_outsweep.py twitchtests/outsweep_node127_36pts_8rep.json [--out fig.png]
"""
import argparse
import json
import os
import sys

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np


def load(paths):
    """Aggregate records by position index across all given json files -> per-position stats."""
    thr = None
    byidx = {}
    for p in paths:
        with open(p) as f:
            d = json.load(f)
        thr = d.get('vel_rms_thr', thr)
        for r in d['records']:
            if r.get('idx') is None or r.get('vel_rms') is None:
                continue
            byidx.setdefault(r['idx'], {'out': [], 'ring': [], 'vr': [], 'fr': []})
            byidx[r['idx']]['out'].append(r['out_deg'])
            byidx[r['idx']]['ring'].append(1.0 if r['ring'] else 0.0)
            byidx[r['idx']]['vr'].append(r['vel_rms'])
            byidx[r['idx']]['fr'].append(r.get('dom_freq') or 0.0)
    rows = []
    for i in sorted(byidx):
        b = byidx[i]
        rows.append({
            'out_deg': float(np.mean(b['out'])),
            'prob': float(np.mean(b['ring'])),
            'vr_mean': float(np.mean(b['vr'])),
            'vr_min': float(np.min(b['vr'])),
            'vr_max': float(np.max(b['vr'])),
            'fr_mean': float(np.mean([f for f in b['fr'] if f > 0]) if any(f > 0 for f in b['fr']) else 0.0),
            'n': len(b['ring']),
        })
    rows.sort(key=lambda r: r['out_deg'])
    return rows, thr


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('json', nargs='+', help='outsweep_*.json from twitch_test --out-sweep (one run)')
    ap.add_argument('--out', default=None, help='output PNG path (default: alongside the first json)')
    args = ap.parse_args()

    rows, thr = load(args.json)
    if not rows:
        print('no records found', file=sys.stderr)
        return 1
    reps = max(r['n'] for r in rows)
    ang = np.array([r['out_deg'] for r in rows])
    prob = np.array([r['prob'] for r in rows])
    vr_mean = np.array([r['vr_mean'] for r in rows])
    vr_min = np.array([r['vr_min'] for r in rows])
    vr_max = np.array([r['vr_max'] for r in rows])
    ndet = int(np.sum(prob >= 0.999))
    nsto = int(np.sum((prob > 0.001) & (prob < 0.999)))

    plt.rcParams.update({'font.size': 10, 'axes.grid': True, 'grid.alpha': 0.25})
    fig = plt.figure(figsize=(13, 6.2))
    fig.suptitle('Hold-twitch vs gearbox OUTPUT angle  (node data, {} positions x {} reps)\n'
                 '{} deterministic (always ring) + {} stochastic  -> output-position-locked = gearbox'
                 .format(len(rows), reps, ndet, nsto), fontsize=12, fontweight='bold')

    # ---- polar: ring probability as bars, coloured by mean vel_rms ----
    axp = fig.add_subplot(1, 2, 1, projection='polar')
    theta = np.deg2rad(ang)
    width = np.deg2rad(360.0 / len(rows)) * 0.9
    cmap = plt.get_cmap('inferno')
    norm = plt.Normalize(vmin=float(vr_mean.min()), vmax=float(vr_mean.max()))
    bars = axp.bar(theta, prob, width=width, bottom=0.05, color=cmap(norm(vr_mean)),
                   edgecolor='k', linewidth=0.4, alpha=0.95)
    axp.set_theta_zero_location('N')
    axp.set_theta_direction(-1)
    axp.set_rlabel_position(95)
    axp.set_ylim(0, 1.08)
    axp.set_yticks([0.25, 0.5, 0.75, 1.0])
    axp.set_yticklabels(['25%', '50%', '75%', 'RING\n100%'], fontsize=7)
    axp.set_title('Ring probability by output angle\n(bar=prob, colour=vel ripple)', fontsize=10, pad=14)
    sm = plt.cm.ScalarMappable(cmap=cmap, norm=norm); sm.set_array([])
    cb = fig.colorbar(sm, ax=axp, pad=0.10, shrink=0.7)
    cb.set_label('mean vel_rms (cts/s)', fontsize=8)

    # ---- cartesian: prob bars (left) + vel_rms line w/ band (right) ----
    axc = fig.add_subplot(1, 2, 2)
    axc.bar(ang, prob, width=360.0 / len(rows) * 0.85, color='#4C78A8', alpha=0.45,
            label='ring probability', zorder=1)
    axc.set_xlabel('gearbox OUTPUT angle (deg)')
    axc.set_ylabel('ring probability', color='#2C5175')
    axc.set_ylim(0, 1.05)
    axc.set_xlim(-5, 365)
    axc.set_xticks(range(0, 361, 45))
    for x in ang[prob >= 0.999]:
        axc.axvspan(x - 5, x + 5, color='#C0392B', alpha=0.07, zorder=0)

    axr = axc.twinx()
    axr.fill_between(ang, vr_min, vr_max, color='#E45756', alpha=0.18, label='vel_rms min..max')
    axr.plot(ang, vr_mean, '-o', color='#C0392B', ms=3.5, lw=1.4, label='mean vel_rms', zorder=3)
    if thr:
        axr.axhline(thr, color='#888', ls='--', lw=1.0, label='ring threshold {:.0f}'.format(thr))
    axr.set_ylabel('velocity ripple vel_rms (cts/s)', color='#8B2A20')
    axr.set_ylim(0, max(vr_max.max() * 1.08, (thr or 0) * 1.2))
    axc.set_title('Ring probability + velocity ripple vs output angle', fontsize=10)
    l1, la1 = axc.get_legend_handles_labels()
    l2, la2 = axr.get_legend_handles_labels()
    axr.legend(l1 + l2, la1 + la2, fontsize=7.5, loc='upper right', framealpha=0.9)

    fig.tight_layout(rect=[0, 0, 1, 0.93])
    out = args.out or (os.path.splitext(args.json[0])[0] + '.png')
    fig.savefig(out, dpi=140)
    print('wrote {}'.format(out))
    print('  {} deterministic (100%) ring positions, {} stochastic, {} clean; {} reps.'.format(
        ndet, nsto, len(rows) - ndet - nsto, reps))
    return 0


if __name__ == '__main__':
    sys.exit(main())
