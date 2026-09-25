#!/usr/bin/env python3
"""Play a MIDI file on a P4 motor with the 0x3015 current-injection sine.

One d-axis HOLD (pins the rotor, keeps the phases past the dead-time knee),
then a SINE on q per note, so the winding and rotor sing at the note's pitch.
Needs firmware that has 0x3015 and re-arms SINE from a running SINE
(stm32 app/inject.c, which keeps the DC lock and phase), so notes are legato.
A rest drops back to a HOLD and the next note waits --gap for it to settle.

Up to three notes sound at once on firmware with SINE voices 2 and 3
(0x3015,11-14); older firmware plays melodies only.  Where more notes are
held than there are voices, the most recently struck ones sound.  --iac is
per voice, so a three-note chord peaks at three times it.  Channel 10
(drums) is ignored.

Raw SDO throughout, so any EDS (or none) will do.  Nothing is saved to NVM;
the drive is returned to idle in a finally block.  Does not suspend a purr
overlay (0x3027): turn it off first on firmware that has one.

Usage:
  play_song.py [song.mid] [--channel can2] [--node 127] [--bpm N]
               [--transpose N] [--bias A] [--iac A] [--gap S] [--dry-run]
"""
import argparse
import math
import os
import struct
import time

import canopen
import mido

DEFAULT_MIDI = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'song.mid')
NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']
#: Rests shorter than this (MIDI articulation gaps) extend the note before.
MIN_REST_S = 0.02

# 0x3015, the injection primitive (app/inject.h).  Mode is written LAST: it
# latches the rest.  The dead-man (,8) is mandatory; 0 is rejected.
INJ = 0x3015
INJ_MODE, INJ_CURRENT, INJ_THETA, INJ_AMP, INJ_FREQ, INJ_AXIS, INJ_MS, \
    INJ_STATE = 1, 2, 4, 5, 6, 7, 8, 9
#: (amplitude, frequency) subindexes of SINE voices 1-3.
VOICE_SUBS = ((INJ_AMP, INJ_FREQ), (11, 12), (13, 14))
INJ_OFF, INJ_HOLD, INJ_SINE = 0, 1, 3
ST_INJECTING, ST_TRIPPED = 3, 4
INJ_MAX_MS = 5000
AXIS_D, AXIS_Q = 0, 1
MODE_IDLE, MODE_PHASE_VOLTAGE_ANGLE = 0, 12
PWM_PATTERNS_PER_CONTROL_CYCLE = 4
FRAC16_ONE = 32767


def note_name(n):
    return '{}{}'.format(NAMES[n % 12], n // 12 - 1)


def load_midi(path, bpm=None, transpose=0, voices=len(VOICE_SUBS)):
    """Reduce *path* to at most *voices* notes at a time.

    Returns [(label, [Hz, ...] lowest first, or [] for a rest, seconds)].

    *bpm* rescales the whole file against its first tempo; tempo changes
    within the file are kept relative to it.
    """
    mid = mido.MidiFile(path)
    scale = 1.0
    if bpm:
        first = next((m.tempo for t in mid.tracks for m in t
                      if m.type == 'set_tempo'), 500000)
        scale = mido.bpm2tempo(bpm) / first

    segs, held = [], []        # held: notes down, in the order struck
    now, start, sounding = 0.0, 0.0, None

    def change(to):
        nonlocal start, sounding
        if to == sounding:
            return
        if now > start:
            segs.append([sounding, now - start])
        start, sounding = now, to

    for msg in mid:            # time is seconds since the previous message
        now += msg.time * scale
        if msg.type not in ('note_on', 'note_off') or msg.channel == 9:
            continue
        if msg.note in held:
            held.remove(msg.note)
        if msg.type == 'note_on' and msg.velocity > 0:
            held.append(msg.note)
        change(tuple(sorted(held[-voices:])) if held else None)
    change(None)

    plan = []
    for n, d in segs:
        if n is None and (not plan or d < MIN_REST_S):
            if plan:
                plan[-1][2] += d   # leading silence is dropped
            continue
        if n is None:
            plan.append(['R', [], d])
        else:
            notes = [k + transpose for k in n]
            plan.append(['+'.join(note_name(k) for k in notes),
                         [440.0 * 2 ** ((k - 69) / 12.0) for k in notes], d])
    return [tuple(p) for p in plan]


class Puck:
    """The handful of raw SDO accesses the player needs."""

    def __init__(self, node):
        self.sdo = node.sdo

    def u(self, index, sub, fmt='<H'):
        data = self.sdo.upload(index, sub)
        return struct.unpack(fmt, data.ljust(struct.calcsize(fmt), b'\0'))[0]

    def w(self, index, sub, value, fmt='<H'):
        self.sdo.download(index, sub, struct.pack(fmt, int(value)))

    def arm(self, mode, ms, params=None):
        """Stage *params* (0x3015 sub -> value), the dead-man, then the mode."""
        ms = int(round(min(ms, INJ_MAX_MS)))
        for sub, value in (params or {}).items():
            self.w(INJ, sub, value, '<B' if sub == INJ_AXIS else '<H')
        self.w(INJ, INJ_MS, ms)
        self.w(INJ, INJ_MODE, mode, '<B')

    def check(self, what):
        state = self.u(INJ, INJ_STATE, '<B')
        if state != ST_INJECTING:
            raise RuntimeError('{}: injection state {}{}'.format(
                what, state, ' (dead-man tripped)' if state == ST_TRIPPED
                else ''))

    def begin(self):
        """Enable in mode 12 at zero volts; the injection owns dq from here."""
        self.w(0x6040, 0, 0x80)     # fault reset
        self.w(0x6040, 0, 0x06)     # shutdown
        self.w(0x6040, 0, 0x0F)     # enable operation
        self.w(0x3010, 4, 0, '<h')  # Motor.ud
        self.w(0x3010, 5, 0, '<h')  # Motor.uq
        self.w(0x6060, 0, MODE_PHASE_VOLTAGE_ANGLE, '<b')

    def safe_stop(self):
        """Injection off first (it overrides every mode), then idle."""
        for args in ((INJ, INJ_MODE, INJ_OFF, '<B'), (0x3010, 4, 0, '<h'),
                     (0x3010, 5, 0, '<h'), (0x6071, 0, 0, '<h'),
                     (0x60FF, 0, 0, '<i'), (0x6060, 0, MODE_IDLE, '<b'),
                     (0x6040, 0, 0x06, '<H')):
            try:
                self.w(*args)
            except Exception:
                pass


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('midi', nargs='?', default=DEFAULT_MIDI)
    ap.add_argument('--channel', default='can2')
    ap.add_argument('--node', type=int, default=127)
    ap.add_argument('--bpm', type=float, help="override the file's tempo")
    ap.add_argument('--transpose', type=int, default=0, help='semitones')
    ap.add_argument('--bias', type=float, default=3.0, help='d hold, A')
    ap.add_argument('--iac', type=float, default=3.0, help='q sine, A peak')
    ap.add_argument('--gap', type=float, default=0.05, help='re-hold, s')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    plan = load_midi(args.midi, args.bpm, args.transpose)
    if not plan:
        raise SystemExit('no notes in {}'.format(args.midi))
    for n, fs, d in plan:
        print('{:13s} {:22s} {:.2f} s'.format(
            n, ' '.join('{:.1f}'.format(f) for f in fs) or '-', d))
    print('total {:.1f} s'.format(sum(d for _, _, d in plan)))
    width = max(len(fs) for _, fs, _ in plan)
    if args.dry_run:
        return

    net = canopen.Network()
    net.connect(interface='socketcan', channel=args.channel)
    p = Puck(net.add_node(args.node))
    try:
        i_peak = p.u(0x3011, 9)                          # mA
        u_max = p.u(0x3001, 9) / 10.0 / math.sqrt(3.0)   # V at F16 full scale
        rate = p.u(0x3001, 1, '<I') / PWM_PATTERNS_PER_CONTROL_CYCLE
        r_ph = p.u(0x3011, 5) / 2000.0                   # mOhm l-l -> ohm/ph
        lq = p.u(0x3011, 12)
        l_ph = (lq if 0 < lq < 0xFFFF else p.u(0x3011, 6)) / 2e6  # uH l-l
        vbus = p.u(0x3000, 1) / 10.0
        print('Vbus {:.1f} V, i_peak {:.1f} A, R {:.3f} ohm/ph, Lq {:.3f} '
              'mH/ph, control {:.0f} Hz'.format(vbus, i_peak / 1000.0, r_ph,
                                                l_ph * 1e3, rate))
        for n, fs, d in plan:
            if any(f > rate / 6 for f in fs):
                raise SystemExit('{} is above the firmware limit of control '
                                 'rate / 6 ({:.0f} Hz)'.format(n, rate / 6))
        # Subindex count 14 = voices 2 and 3 present.
        has_chords = p.u(INJ, 0, '<B') >= VOICE_SUBS[-1][1]
        voices = len(VOICE_SUBS) if has_chords else 1
        if width > voices:
            raise SystemExit('this song needs {} voices; the firmware has {}'
                             .format(width, voices))

        p.begin()
        hold = {INJ_CURRENT: round(args.bias * 1000), INJ_THETA: 0,
                INJ_AXIS: AXIS_D}
        # First hold: let the rotor snap to theta 0 and settle fully.
        p.arm(INJ_HOLD, 1000, hold)
        time.sleep(0.4)
        t = time.perf_counter()
        sine, settled = False, True   # the first hold has had its 0.4 s
        for n, fs, d in plan:
            if not fs:
                p.arm(INJ_HOLD, d * 1e3 + 250, hold)
                sine, settled = False, False
                t += d
                time.sleep(max(0, t - time.perf_counter()))
                continue
            if not sine and not settled:
                # A rest restarted the HOLD's integrator: let it settle.
                p.arm(INJ_HOLD, args.gap * 1e3 + 250, hold)
                time.sleep(max(0, t + args.gap - time.perf_counter()))
            # Every voice the firmware has is written on every note: voices
            # 2 and 3 persist until the injection stops, so a melody note
            # after a chord has to silence them itself.
            params, volts = {INJ_AXIS: AXIS_Q}, []
            for k, (sub_amp, sub_freq) in enumerate(VOICE_SUBS[:voices]):
                amp = 0
                if k < len(fs):
                    v = args.iac * math.hypot(r_ph, 2 * math.pi * fs[k] * l_ph)
                    amp = max(1, min(FRAC16_ONE, round(v / u_max * FRAC16_ONE)))
                    params[sub_freq] = round(fs[k])
                    volts.append(v)
                params[sub_amp] = amp
            p.arm(INJ_SINE, d * 1e3 + 250, params)
            sine = True
            print('{:13s} {:.2f} s  {} V'.format(
                n, d, ' '.join('{:.3f}'.format(v) for v in volts)), flush=True)
            t += d
            time.sleep(max(0, t - time.perf_counter()))
            p.check(n)
    finally:
        p.safe_stop()
        net.disconnect()


if __name__ == '__main__':
    main()
