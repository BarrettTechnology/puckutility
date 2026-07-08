"""Fast bidirectional PDO path for the calibrations: stream id/iq IN via a TPDO and (optionally)
stream Theta_e/ud OUT via an RPDO, at SYNC rate, without per-sample SDO round-trips.

=====================================================================================
READ and WRITE are COUPLED: SYNC disables the drive unless RPDO1 is pre-armed
=====================================================================================
Confirmed on hardware (scripts/pdo_coherent_test.py): the "frozen read" was NOT a reception bug at
all -- reception worked (frames arrived every SYNC, RAWCAP == PDOMAP, both tracked). The problem was
that there was almost no current to read: turning SYNC ON to stream the TPDO makes the firmware
re-apply the UNPRIMED RPDO1 buffer (ControlWord=0, Disable Voltage) on EVERY SYNC, so the drive is
DISABLED during the very read burst (SDO |I| collapsed to ~10 mA while itiming needs ~300 mA). Same
root cause as the write-side "didn't spin". So to read a SYNC-driven TPDO with real current you MUST
keep the drive enabled during SYNC -- i.e. arm RPDO1 async + prime it (see RPDOWriter, below). FastPDO
now does this itself (keep_drive_enabled=True by default): __enter__ sets RPDO1 trans_type=255, and
sync_on() primes RPDO1 with ControlWord=OP_ENABLED+mode so SYNC can no longer re-disable the drive.
The original RPDO1 trans_type is restored on __exit__.

=====================================================================================
WHY THE `.raw` POLLING FROZE (root cause of the two previous rewrites)
=====================================================================================
Both earlier versions read the signal pair via canopen's PdoMap object:
`node.tpdo[4].wait_for_reception(t)` then `node.tpdo[4].map[i].raw`. On hardware the pair FROZE
(constant id/iq while the real current swept) even though the GUI's own dial keeps updating.

The tell is in how the *working* code reads PDOs:

  * The app dial (`puckutilityapp.getMonitor`) reads `node.tpdo[2]['CurrentFeedback'].raw` -- BUT it
    only runs because `tpdo2_callback` (a PER-FRAME PdoMap callback) fires and `wx.CallAfter`s it. The
    callback firing is what proves a frame landed; `.raw` is read immediately after, so it's fresh.
  * The proven ~1 kHz control loop (`calibrate_menu._PVCATorqueDialog`) does NOT touch PdoMap `.raw`
    at all. It subscribes a RAW callback straight onto the network -- `network.subscribe(tpdo1_cob,
    self._on_tpdo1)` -- and decodes the 8 bytes itself with `struct.unpack_from`.

canopen dispatch (canopen/network.py:236-253 `Network.notify`): for each received frame it calls every
callback registered for that COB-ID in `self.subscribers`. `PdoMap.on_message` (canopen/pdo/base.py:
300-311) is ONE such subscriber; it overwrites `self.data` and, crucially, only then runs the PdoMap's
own `callbacks`. `PdoVariable.raw`/`get_data` (base.py:565-585) slice `self.pdo_parent.data`.

FastPDO used to POLL `.raw` from the main thread while ALSO clearing the PdoMap callbacks. The moment a
frame's delivery to `PdoMap.on_message` is disturbed -- by the re-`save()`/re-`read()` re-subscribe
dance, by clearing callbacks, or by the main-thread `wait_for_reception` racing the notifier thread on
the same `receive_condition` -- `.data` stops advancing and every `.raw` reads the last buffered frame:
a frozen pair, exactly as seen. The PdoMap read path is fragile in this app; the RAW-subscribe path is
what actually works here.

=====================================================================================
THE FIX: capture via a raw network callback, decode the bytes ourselves (the PVCA path)
=====================================================================================
FastPDO now maps BOTH signals into ONE TPDO (coherent: one CAN frame = one SYNC instant) and then
reads them by SUBSCRIBING A RAW CALLBACK on that TPDO's COB-ID -- exactly like `_PVCATorqueDialog`.
`_on_frame()` runs in the notifier thread on every SYNC, `struct.unpack`s id+iq out of the frame, and
stores them (GIL-safe) as the latest pair plus a frame counter. `read()` waits for the counter to
advance (a genuinely fresh frame) and returns the captured pair -- it never touches PdoMap `.raw` or
`wait_for_reception`. This is coherent (one frame), fresh (counter-gated), and uses the reception path
this app is proven to deliver reliably.

Signal pairs (widths from puck4.eds; both fit one 8-byte frame):
  'idiq'      (default): Motor.id 0x3010:6 INT16 + CurrentFeedback 0x6078 INT16  -> frame '<hh' (4 B)
  'alphabeta'          : Alpha.Filtered 0x3008:2 U16 + Beta.Filtered 0x3009:2 U16 -> '<HH' (4 B)
read() returns [primary, secondary] raw, i.e. [id, iq] for 'idiq'.

SYNC gating: the cal's rapid back-to-back SDO burst collides with continuous SYNC (0x05040001). The
caller runs SYNC OFF (default) around SDO bursts and calls sync_on() only around a sampling burst.
read() returns None unless SYNC is on -> caller falls back to SDO. `ok`/`err` report setup status.

=====================================================================================
RPDO WRITE (RPDOWriter): why an open-loop Theta_e/ud stream "didn't spin", and the fix
=====================================================================================
Per `_PVCATorqueDialog` (calibrate_menu.py:37-45, 245-283): RPDO1 (0x200+id) defaults to trans_type=0,
so the firmware APPLIES RPDO1's buffer on EVERY SYNC. That startup buffer holds ControlWord=0 (Disable
Voltage), so every SYNC re-DISABLES the drive -- no matter what you set over SDO. An RPDO3 Theta_e/ud
stream therefore produces no current: the motor is being disabled 500-1000x/s.

The proven cure (RPDOWriter mirrors it exactly):
  1. PRE-OPERATIONAL. Set RPDO1 trans_type=255 (async: apply on receipt only, NOT every SYNC).
  2. Map RPDO3 = Theta_e(0x60EA:0,16b) + Motor.ud(0x3010:4,16b), trans_type=255 (apply on receipt).
  3. OPERATIONAL, clear-fault/shutdown/op-enable, mode = PHASE_VOLTAGE_ANGLE.
  4. PRIME RPDO1 ONCE via send_message with ControlWord=OP_ENABLED+mode -> drive latches enabled and
     stays enabled because SYNC no longer re-applies RPDO1.
  5. write(theta_e, ud) = send_message(rpdo3_cob, struct.pack('<hh', theta_e, ud)) -> applied on
     receipt, no ACK, no SDO round-trip. (SYNC only needs to run for the TPDO feedback; async RPDOs
     apply without it.)
"""
import struct
import time

# DS402 (mirror of canopen_runner.py; kept local so this module stays dependency-light).
CLEAR_FAULT, SHUTDOWN, OP_ENABLED = 0x80, 0x06, 0x0F
MODE_IDLE, MODE_PHASE_VOLTAGE_ANGLE = 0, 12

# (index-name, subindex-or-None) entries + the CAN-frame struct format + byte count, per pair.
_PAIRS = {
    'idiq':      dict(entries=[('Motor', 'id'), ('CurrentFeedback', None)], fmt='<hh', nbytes=4),
    'alphabeta':     dict(entries=[('Alpha', 'Filtered'), ('Beta', 'Filtered')], fmt='<HH', nbytes=4),
    # RAW (unfiltered) ADC — U16 Q12.0. The per-sample SCATTER of this exposes the switching ring
    # (steep dV/dt x ADC-trigger jitter -> big sample variation on the ring, small once settled), which
    # the Filtered IIR averages away. Used by the settling cal to detect where the ring actually clears.
    'alphabeta_raw': dict(entries=[('Alpha', 'Raw'), ('Beta', 'Raw')], fmt='<HH', nbytes=4),
}


class FastPDO:
    """Coherent, fresh id/iq (or alpha/beta) read stream over ONE TPDO, captured via a raw network
    callback (the proven reception path). Public API preserved: __enter__/__exit__, sync_on(),
    sync_off(), read()->[a,b] or None, .ok, .err."""

    def __init__(self, node, tpdo_n=4, sync_period_ms=2, pair='idiq',
                 mode=MODE_PHASE_VOLTAGE_ANGLE, keep_drive_enabled=True):
        self.node = node
        self.n = tpdo_n
        self.sync_period = sync_period_ms / 1000.0
        spec = _PAIRS[pair] if isinstance(pair, str) else pair
        self._entries = spec['entries']
        self._fmt = spec['fmt']
        self._nbytes = spec['nbytes']
        self.mode = mode
        # Keep the drive enabled during SYNC by arming RPDO1 async + priming OP_ENABLED (else SYNC
        # re-applies RPDO1's ControlWord=0 buffer and disables the drive -> nothing to read).
        self.keep_drive_enabled = keep_drive_enabled
        self._rpdo1_cob = 0x200 | (node.id & 0x7F)
        self._orig_rpdo1_trans = None
        self._tp = None
        self._cob = None
        self._sync_on = False
        self._cb = {}                 # silenced PdoMap GUI callbacks, restored on exit
        self._orig_map = None         # [(index, subindex, length), ...] to restore
        self._orig_trans = None
        self._orig_enabled = None
        # Captured-in-notifier-thread state (GIL-safe: notifier writes, main thread reads).
        self._latest = None
        self._latest_ts = 0.0
        self._frames = 0
        self.ok = False
        self.err = None

    # --- PDO (re)mapping (PRE-OPERATIONAL; PDO map arrays are only writable when not operational).
    #     Also sets RPDO1 trans_type in the same pre-op window so SYNC can't re-disable the drive. ---
    def _write_config(self, entries, trans_type, enabled, rpdo1_trans):
        n = self.node
        n.nmt.state = 'PRE-OPERATIONAL'
        n.tpdo.read()
        tp = n.tpdo[self.n]
        tp.clear()
        for e in entries:
            idx, sub = e[0], e[1]
            length = e[2] if len(e) > 2 else None
            if sub is None:
                tp.add_variable(idx) if length is None else tp.add_variable(idx, 0, length)
            else:
                tp.add_variable(idx, sub) if length is None else tp.add_variable(idx, sub, length)
        tp.trans_type = trans_type
        tp.enabled = enabled
        if rpdo1_trans is not None:
            try:
                n.sdo[0x1400][2].raw = rpdo1_trans        # RPDO1 transmission type (255=async)
            except Exception:
                pass
        n.tpdo.save()
        n.tpdo.read()
        n.nmt.state = 'OPERATIONAL'
        return n.tpdo[self.n]

    def __enter__(self):
        n = self.node
        try:
            n.tpdo.read()
            src = n.tpdo[self.n]
            self._orig_map = [(v.index, v.subindex, v.length) for v in src.map if v.length]
            self._orig_trans = src.trans_type
            self._orig_enabled = src.enabled
            if self.keep_drive_enabled:
                try:
                    self._orig_rpdo1_trans = n.sdo[0x1400][2].raw   # save to restore on exit
                except Exception:
                    self._orig_rpdo1_trans = None

            try:
                n.network.sync.stop()                 # known SYNC-off state
            except Exception:
                pass

            # Map BOTH signals into ONE TPDO (every SYNC) and, if requested, arm RPDO1 async.
            self._tp = self._write_config(
                self._entries, 1, True, 255 if self.keep_drive_enabled else None)
            self._cob = self._tp.cob_id
            if not self._cob:
                raise RuntimeError("TPDO{} has no COB-ID after remap".format(self.n))
            present = [(v.index, v.subindex) for v in self._tp.map if v.length]
            if len(present) < 2:
                raise RuntimeError("TPDO{} did not accept the coherent pair (got {})"
                                   .format(self.n, present))

            # Silence the app's per-frame PdoMap GUI callbacks (they'd wx.CallAfter every SYNC ->
            # flood the GUI). Proven safe for reception: puckutilityapp itself clears+re-adds these.
            for i in (1, 2, 3, 4):
                try:
                    self._cb[i] = list(n.tpdo[i].callbacks)
                    n.tpdo[i].callbacks.clear()
                except Exception:
                    pass

            # THE FIX: capture id/iq via a RAW network subscription + manual decode (the PVCA path),
            # not via PdoMap.raw. Runs in the notifier thread on every received TPDO frame.
            n.network.subscribe(self._cob, self._on_frame)
            self.ok = True
        except Exception as e:
            self.ok = False
            self.err = e
            self._teardown()
        return self

    def _on_frame(self, can_id, data, timestamp):
        """Notifier-thread callback: decode the coherent pair out of the raw frame and latch it."""
        if len(data) < self._nbytes:
            return
        try:
            self._latest = struct.unpack_from(self._fmt, data)
        except struct.error:
            return
        self._latest_ts = timestamp
        self._frames += 1                             # GIL-safe counter; read() gates on it

    # --- SYNC gating -----------------------------------------------------
    def sync_on(self):
        """Start continuous SYNC for a sampling burst. Call AFTER the SDO control writes are done."""
        if not self.ok or self._sync_on:
            return
        try:
            # Prime RPDO1 (async) so the drive stays ENABLED once SYNC starts ticking. Without this,
            # SYNC re-applies RPDO1's stale ControlWord=0 and the drive collapses -> no current.
            if self.keep_drive_enabled:
                try:
                    self.node.network.send_message(
                        self._rpdo1_cob, struct.pack('<HBh', OP_ENABLED, self.mode, 0))
                except Exception:
                    pass
            self.node.network.sync.start(self.sync_period)
            self._sync_on = True
            self._wait_fresh(self._frames, 1.0)       # prime: wait for the first streamed frame
        except Exception:
            self._sync_on = False

    def sync_off(self):
        """Stop SYNC before the next SDO burst (continuous SYNC + rapid SDO writes collide)."""
        if self._sync_on:
            try:
                self.node.network.sync.stop()
            except Exception:
                pass
            self._sync_on = False

    def _wait_fresh(self, start_count, timeout):
        """Block until the notifier captures a NEW frame (counter advances past start_count)."""
        deadline = time.monotonic() + timeout
        while self._frames == start_count and time.monotonic() < deadline:
            time.sleep(0.0005)
        return self._frames != start_count

    # --- read ------------------------------------------------------------
    def read(self, fresh=True, timeout=0.05):
        """[primary, secondary] raw from one freshly-captured frame (coherent, same SYNC), or None
        -> caller uses SDO. Only valid between sync_on()/sync_off(). For 'idiq' this is [id, iq]."""
        if not self.ok or not self._sync_on:
            return None
        try:
            if fresh and not self._wait_fresh(self._frames, timeout):
                return None                           # no fresh frame within a few SYNC periods
            v = self._latest
            return list(v) if v is not None else None
        except Exception:
            return None

    # --- cleanup ---------------------------------------------------------
    def _teardown(self):
        try:
            if self._cob is not None:
                self.node.network.unsubscribe(self._cob, self._on_frame)
        except Exception:
            pass
        if self._orig_map:
            try:
                self._write_config(self._orig_map, self._orig_trans, self._orig_enabled,
                                   self._orig_rpdo1_trans if self.keep_drive_enabled else None)
            except Exception:
                try:
                    self.node.nmt.state = 'OPERATIONAL'
                except Exception:
                    pass
            self._orig_map = None
        for i, cbs in self._cb.items():
            try:
                self.node.tpdo[i].callbacks.extend(cbs)
            except Exception:
                pass
        self._cb = {}

    def __exit__(self, *exc):
        self.sync_off()
        self._teardown()
        return False


class RPDOWriter:
    """Stream Theta_e + Motor.ud to the puck via RPDO3 (apply-on-receipt) while keeping the drive
    ENABLED -- by pre-arming RPDO1 async so SYNC can't re-disable it. Mirrors _PVCATorqueDialog.

    Usage:
        with RPDOWriter(node) as w:
            if w.ok:
                # continuous SYNC should be running (e.g. via a FastPDO.sync_on() or a SYNC thread)
                w.write(theta_e_raw, ud_raw)   # applied on receipt; no SDO, no ACK
    Idles the drive (ud=0, IDLE) on __exit__.
    """

    def __init__(self, node, mode=MODE_PHASE_VOLTAGE_ANGLE):
        self.node = node
        self.mode = mode
        nid = node.id & 0x7F
        self._rpdo1_cob = 0x200 | nid
        self._rpdo3_cob = 0x400 | nid
        self.ok = False
        self.err = None

    def __enter__(self):
        n = self.node
        try:
            n.nmt.state = 'PRE-OPERATIONAL'
            n.sdo[0x1800][2].raw = 0                       # TPDO1 every SYNC (feedback, optional)
            n.sdo[0x1400][2].raw = 255                     # RPDO1 async (NOT applied every SYNC)
            n.sdo[0x1402][1].raw = self._rpdo3_cob | 0x80000000   # disable RPDO3 while mapping
            n.sdo[0x1602][0].raw = 0                        # clear map entry count
            n.sdo[0x1602][1].raw = 0x60EA0010              # Theta_e  0x60EA:0, 16-bit
            n.sdo[0x1602][2].raw = 0x30100410              # Motor.ud 0x3010:4, 16-bit
            n.sdo[0x1602][0].raw = 2
            n.sdo[0x1402][2].raw = 255                     # RPDO3 async (apply on receipt)
            n.sdo[0x1402][1].raw = self._rpdo3_cob         # re-enable RPDO3

            n.nmt.state = 'OPERATIONAL'
            n.sdo["ControlWord"].raw = CLEAR_FAULT
            n.sdo["ControlWord"].raw = SHUTDOWN
            n.sdo["ControlWord"].raw = OP_ENABLED
            n.sdo["SetModeOfOperation"].raw = self.mode
            n.sdo['Theta_e'].raw = 0
            n.sdo['Motor']['ud'].raw = 0
            # PRIME RPDO1 once: ControlWord=OP_ENABLED + mode. RPDO1 is async now, so this latches the
            # drive enabled and SYNC never re-disables it (the whole reason the open-loop stream span).
            n.network.send_message(self._rpdo1_cob,
                                   struct.pack('<HBh', OP_ENABLED, self.mode, 0))
            n.network.send_message(self._rpdo3_cob, struct.pack('<hh', 0, 0))
            time.sleep(0.05)
            self.ok = True
        except Exception as e:
            self.ok = False
            self.err = e
        return self

    def write(self, theta_e, ud):
        """Send one RPDO3 frame (Theta_e, ud) -- applied by the puck on receipt. theta_e is wrapped
        into signed 16-bit so callers can pass a free-running 0..65535 electrical-angle counter."""
        self.node.network.send_message(self._rpdo3_cob, struct.pack('<hh', _wrap16(theta_e), int(ud)))

    def __exit__(self, *exc):
        try:
            self.node.network.send_message(self._rpdo3_cob, struct.pack('<hh', 0, 0))
        except Exception:
            pass
        try:
            self.node.sdo['Motor']['ud'].raw = 0
            self.node.sdo['Theta_e'].raw = 0
            self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE
        except Exception:
            pass
        return False


def _wrap16(v):
    """Fold an electrical-angle value into a signed 16-bit (0x60EA Theta_e), so 0..65535 == one full
    electrical cycle (verified on hardware: the alpha/beta field advances exactly 2*pi over that span)."""
    v = int(round(v)) & 0xFFFF
    return v - 65536 if v >= 32768 else v


def _solve3(A, B):
    """Solve a 3x3 linear system A x = B by Cramer's rule. Returns None if singular."""
    def det3(m):
        return (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
                - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
                + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))
    d = det3(A)
    if abs(d) < 1e-12:
        return None
    out = []
    for c in range(3):
        M = [row[:] for row in A]
        for r in range(3):
            M[r][c] = B[r]
        out.append(det3(M) / d)
    return out


def kasa_circle_fit(xs, ys):
    """Kasa algebraic circle fit (pure Python, no numpy). Fits x^2+y^2 = 2*cx*x + 2*cy*y + c.
    Returns (cx, cy, R): center = the current-sense OFFSET, radius R = the current magnitude |I|.
    Same model as calibrate_menu's slope-cal `_circle_fit`. Units follow the inputs (counts or mA)."""
    n = len(xs)
    if n < 3:
        return 0.0, 0.0, 0.0
    Sx = sum(xs); Sy = sum(ys)
    Sxx = sum(x * x for x in xs); Syy = sum(y * y for y in ys)
    Sxy = sum(xs[i] * ys[i] for i in range(n))
    z = [xs[i] * xs[i] + ys[i] * ys[i] for i in range(n)]
    Sz = sum(z); Sxz = sum(xs[i] * z[i] for i in range(n)); Syz = sum(ys[i] * z[i] for i in range(n))
    A = [[4 * Sxx, 4 * Sxy, 2 * Sx],
         [4 * Sxy, 4 * Syy, 2 * Sy],
         [2 * Sx,  2 * Sy,  float(n)]]
    sol = _solve3(A, [2 * Sxz, 2 * Syz, Sz])
    if sol is None:
        return 0.0, 0.0, 0.0
    cx, cy, c = sol
    R = max(c + cx * cx + cy * cy, 0.0) ** 0.5
    return cx, cy, R


def circle_rms_residual(xs, ys, cx, cy, R):
    """RMS radial error of the points about the fitted circle, as a fraction of R (0 = perfect)."""
    n = len(xs)
    if n == 0 or R <= 0:
        return float('inf')
    s = 0.0
    for i in range(n):
        d = ((xs[i] - cx) ** 2 + (ys[i] - cy) ** 2) ** 0.5 - R
        s += d * d
    return (s / n) ** 0.5 / R


class SpinScanner:
    """CONTINUOUS-SPIN current-circle scanner -- the shared fast primitive for the rotor-parking cals
    (slope now; itiming / gain / enczero later).

    It ramps the commanded electrical angle Theta_e via RPDO3 across one+ electrical cycles at a
    CONTROLLED, tunable rate while a FastPDO(pair='alphabeta') streams the STATOR current
    (Alpha.Filtered/Beta.Filtered) -- with NO per-angle SDO stop/settle (the settles that dominate
    every stepped rotor-parking cal). The rotor magnetically detents to and FOLLOWS the field, so the
    Park-frame id/iq stay ~d-axis (that is why the old Phase-4 id/iq check read "not rotating"); the
    STATOR alpha/beta trace the full current CIRCLE. Circle-fit -> center = sense offset, radius = |I|.

    Sweep-rate limit: the field must rotate slowly enough that the detenting rotor + current (bounded
    by R/L and rotor inertia/friction) keep up, else the circle degrades (magnitude droops / lags ->
    the algebraic center drifts). Use `scan_offset`'s residual (circle_rms_residual) to pick the
    fastest safe rate; the validation script sweeps rates and reports the residual for each.

    Usage (how a cal drives it):
        with SpinScanner(node) as s:
            if s.ok:
                off_a, off_b, meanI = s.scan_offset(ud, alpha_bias=ab, a_sens=as_, beta_bias=bb,
                                                     b_sens=bs, rate_hz=0.5, cycles=1.25)
                # off_a/off_b = circle center (sense offset); meanI = radius (|I|). Repeat per ud
                # level and fit off vs meanI for the slope, exactly like the stepped cal.
    Idles the drive and restores PDO config on __exit__.
    """

    def __init__(self, node, sync_period_ms=1, mode=MODE_PHASE_VOLTAGE_ANGLE):
        self.node = node
        self.mode = mode
        nid = node.id & 0x7F
        self._rpdo3_cob = 0x400 | nid
        self._rpdo1_cob = 0x200 | nid
        # Reuse the validated read path: alpha/beta over one TPDO + RPDO1 async-prime + SYNC gating.
        self._fp = FastPDO(node, pair='alphabeta', sync_period_ms=sync_period_ms,
                           mode=mode, keep_drive_enabled=True)
        self._orig_rpdo3 = None       # (cob_id_raw, trans_type) to restore
        self.ok = False
        self.err = None

    def __enter__(self):
        n = self.node
        try:
            # 1) Map RPDO3 = Theta_e + Motor.ud (async, apply-on-receipt) in a pre-op window.
            n.nmt.state = 'PRE-OPERATIONAL'
            try:
                self._orig_rpdo3 = (n.sdo[0x1402][1].raw, n.sdo[0x1402][2].raw)
            except Exception:
                self._orig_rpdo3 = None
            n.sdo[0x1402][1].raw = self._rpdo3_cob | 0x80000000   # disable while mapping
            n.sdo[0x1602][0].raw = 0
            n.sdo[0x1602][1].raw = 0x60EA0010                     # Theta_e  0x60EA:0, 16-bit
            n.sdo[0x1602][2].raw = 0x30100410                     # Motor.ud 0x3010:4, 16-bit
            n.sdo[0x1602][0].raw = 2
            n.sdo[0x1402][2].raw = 255                            # async
            n.sdo[0x1402][1].raw = self._rpdo3_cob                # enable
            n.nmt.state = 'OPERATIONAL'

            # 2) alpha/beta read path (its own pre-op: TPDO4->alphabeta + RPDO1 async).
            self._fp.__enter__()
            if not self._fp.ok:
                raise RuntimeError("alpha/beta read setup failed: {}".format(self._fp.err))

            # 3) Energise; uq=0 so PHASE_VOLTAGE_ANGLE takes the D-axis-stall (detent) branch.
            n.sdo["ControlWord"].raw = CLEAR_FAULT
            n.sdo["ControlWord"].raw = SHUTDOWN
            n.sdo["ControlWord"].raw = OP_ENABLED
            n.sdo["SetModeOfOperation"].raw = self.mode
            n.sdo['Motor']['uq'].raw = 0
            n.sdo['Theta_e'].raw = 0
            n.sdo['Motor']['ud'].raw = 0
            self.ok = True
        except Exception as e:
            self.ok = False
            self.err = e
            self._teardown()
        return self

    def _write3(self, theta_e, ud):
        self.node.network.send_message(self._rpdo3_cob, struct.pack('<hh', _wrap16(theta_e), int(ud)))

    def spin(self, ud, cycles=1.25, rate_hz=0.5, settle_s=0.4, dt=0.002):
        """Detent at theta=0 to establish current, then ramp Theta_e through `cycles` electrical
        cycles at `rate_hz` cycles/s (via RPDO3), capturing (theta_cmd, alpha, beta) samples every
        `dt`. NO per-angle SDO settle. Returns the sample list ([] if not ok). `rate_hz` is bounded by
        rotor/current dynamics -- too fast degrades the circle (check the residual)."""
        if not self.ok:
            return []
        self._fp.sync_on()                       # primes RPDO1, starts SYNC, alpha/beta streams
        self._write3(0, ud)                       # detent at 0 to establish current
        time.sleep(settle_s)
        samples = []
        total = max(1e-3, cycles) / max(1e-3, rate_hz)
        t0 = time.monotonic()
        while True:
            el = time.monotonic() - t0
            if el >= total:
                break
            theta = (el * rate_hz * 65536.0) % 65536.0
            self._write3(theta, ud)
            v = self._fp.read(fresh=False)        # latest captured alpha/beta (no per-sample wait)
            if v is not None:
                samples.append((theta, v[0], v[1]))
            time.sleep(dt)
        self._write3(0, 0)
        self._fp.sync_off()
        return samples

    def scan_offset(self, ud, alpha_bias=0.0, a_sens=1.0, beta_bias=0.0, b_sens=1.0, **spin_kw):
        """spin() + circle-fit. Converts captured alpha/beta counts to (Filtered-bias)/sens units and
        fits the circle. Returns (offset_alpha, offset_beta, meanI) = (center_x, center_y, radius), or
        None if too few samples. Default bias/sens -> raw counts. Also stashes the last fit residual
        on self.last_residual (fraction of R) for sweep-rate sanity."""
        s = self.spin(ud, **spin_kw)
        if len(s) < 6:
            self.last_residual = float('inf')
            self.last_samples = s
            return None
        xs = [(a - alpha_bias) / a_sens for (_t, a, _b) in s]
        ys = [(b - beta_bias) / b_sens for (_t, _a, b) in s]
        cx, cy, R = kasa_circle_fit(xs, ys)
        self.last_residual = circle_rms_residual(xs, ys, cx, cy, R)
        self.last_samples = s
        return cx, cy, R

    def _teardown(self):
        try:
            self._fp.__exit__(None, None, None)   # restores TPDO4 + RPDO1, stops SYNC
        except Exception:
            pass
        if self._orig_rpdo3 is not None:
            try:
                n = self.node
                n.nmt.state = 'PRE-OPERATIONAL'
                cob, tt = self._orig_rpdo3
                n.sdo[0x1402][1].raw = cob | 0x80000000
                n.sdo[0x1402][2].raw = tt
                n.sdo[0x1402][1].raw = cob         # restore original cob-id (valid bit and all)
                n.nmt.state = 'OPERATIONAL'
            except Exception:
                try:
                    self.node.nmt.state = 'OPERATIONAL'
                except Exception:
                    pass
            self._orig_rpdo3 = None
        try:
            self.node.sdo['Motor']['ud'].raw = 0
            self.node.sdo["SetModeOfOperation"].raw = MODE_IDLE
        except Exception:
            pass

    def __exit__(self, *exc):
        self._teardown()
        return False
