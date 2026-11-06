#!/usr/bin/env python3
"""Exercise every control mode and the fault paths of one P4 puck, on 4.3.5 and 5.x.

    testp4.py --can can0 --id 4                  every test the firmware offers
    testp4.py --can can0 --id 4 --test pv,csp    some of them
    testp4.py --list                             the tests, and what each one does
    Windows: --can 0 is the first CandleLight (or PCAN) adapter.

Free rotor only: every test uses a small command, and a speed guard (--guard-rpm)
disables the drive if the rotor runs away.  Everything is RAM: the PDO mapping and
each parameter a test changes are put back at the end, and nothing is saved.

Each test prints PASS, FAIL or SKIP (the firmware does not have the mode or object);
a summary table follows, and the exit code is 1 if any test failed, 2 if the puck
could not be set up.  EMCY frames print as they arrive, one line each.

The PDOs are mapped here, in PRE-OPERATIONAL, so the CSV's own PDOs do not matter:
  RPDO1 = 0x6040 controlword, 0x6060 mode, 0x6071 target torque   (event, 255)
  RPDO2 = 0x60FF target velocity, 0x607A target position           (event, 255)
  TPDO1 = 0x6041 statusword, 0x6061 mode display, 0x6064 position  (SYNC, 1)
  TPDO2 = 0x606C velocity, 0x6078 current                          (SYNC, 1)
The host sends SYNC only in the cyclic tests: RPDO1, RPDO2, then SYNC each period.
The RPDOs are invalid (COB-ID bit 31) outside those tests, so nothing replays.
0x60F4 (following error) does not fit a TPDO and is read by SDO.
"""

import argparse
import math
import os
import struct
import sys
import threading
import time
from collections import namedtuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))       # puckutility/, for its p4core

from p4core import cia402, firmware                       # noqa: E402
from p4core.calibrate import RAMP_ID_SATURATED, id_filter_settle_s, id_scale  # noqa: E402
from p4core.flash import read_version                     # noqa: E402
from p4core.nvjson import read_githash                    # noqa: E402

RES = 4096                  # encoder counts per revolution (both A/B CSVs)
SYNC_COB = 0x80
EMCY_BASE = 0x80
HEARTBEAT_BASE = 0x700
COB_INVALID = 0x80000000

# Controlword
CW_DISABLE_VOLTAGE = 0x00
CW_SHUTDOWN = cia402.SHUTDOWN
CW_ENABLE = cia402.OP_ENABLED
CW_FAULT_RESET = cia402.CLEAR_FAULT
CW_NEW_SETPOINT = 0x10      # pp: bit 4; homing: start
CW_IMMEDIATE = 0x20         # pp: bit 5, change set immediately
CW_RELATIVE = 0x40          # pp: bit 6
# Statusword
SW_FAULT = cia402.SW_FAULT
SW_TARGET_REACHED = 0x0400
SW_ACK = 0x1000             # pp: set-point acknowledge; homing: attained
SW_FOLLOWING_ERROR = 0x2000

# Mode-unsupported aborts: 4.3.5 refuses a mode with 0x05040001, 5.x 0x06090030.
UNSUPPORTED_ABORTS = (0x05040001, 0x06090030)
# EMCY codes a test provokes, either firmware line.
SYNC_TIMEOUT_CODES = (0x8180,)
HEARTBEAT_CODES = (0x8130,)
FOLLOWING_CODES = {True: (0x8614,), False: (0x8611,)}    # by legacy
# Where 4.3.5 means something else by a code than p4core's table says.
LEGACY_ERROR_CODES = {0x8611: 'position tracking warning',
                      0x8180: 'SYNC timeout'}

PDO_LAYOUT = {
    'rpdo1': (0x1400, 0x1600, 0x200, 255, ((0x6040, 0, 16), (0x6060, 0, 8), (0x6071, 0, 16))),
    'rpdo2': (0x1401, 0x1601, 0x300, 255, ((0x60FF, 0, 32), (0x607A, 0, 32))),
    'tpdo1': (0x1800, 0x1A00, 0x180, 1, ((0x6041, 0, 16), (0x6061, 0, 8), (0x6064, 0, 32))),
    'tpdo2': (0x1801, 0x1A01, 0x280, 1, ((0x606C, 0, 32), (0x6078, 0, 16))),
}

Result = namedtuple('Result', 'name status detail seconds')


class TestSkip(Exception):
    """The firmware does not offer what the test needs."""


class TestFail(Exception):
    """The drive did not do what the test asked."""


class GuardTrip(TestFail):
    """The rotor ran past the speed guard; the drive was disabled."""


class DriveFault(TestFail):
    """The drive faulted when the test did not expect it."""


class SdoAbort(Exception):
    def __init__(self, code):
        super().__init__('SDO abort {:#010x}'.format(code))
        self.code = code


def abort_code(exc):
    """The SDO abort code in *exc* (canopen's SdoAbortedError or ours), else None."""
    return getattr(exc, 'code', None)


def rpm(cts_s):
    return cts_s * 60.0 / RES


def cts_s(rpm_):
    return rpm_ * RES / 60.0


def correlate(target, actual, max_lag):
    """(best correlation, its lag in samples, actual/target amplitude) of two
    equal-length series, the actual lagging the target by 0..max_lag samples."""
    best = (-2.0, 0, 0.0)
    for lag in range(max_lag + 1):
        a = target[:len(target) - lag] if lag else target
        b = actual[lag:]
        n = len(a)
        if n < 4:
            break
        ma, mb = sum(a) / n, sum(b) / n
        da = [x - ma for x in a]
        db = [y - mb for y in b]
        sa = math.sqrt(sum(x * x for x in da))
        sb = math.sqrt(sum(y * y for y in db))
        if sa == 0 or sb == 0:
            continue
        c = sum(x * y for x, y in zip(da, db)) / (sa * sb)
        if c > best[0]:
            best = (c, lag, sb / sa)
    return best


# ---------------------------------------------------------------------------
# The drive: raw SDO by index, the PDOs, EMCY, the guard
# ---------------------------------------------------------------------------
class Drive:
    """One puck, addressed by index so the EDS does not matter (4.3.5 and 5.x
    differ).  *node* is a canopen RemoteNode (sdo.upload/download, nmt.state),
    *network* the canopen Network (send_message, subscribe)."""

    def __init__(self, network, node, args, clock=time.monotonic, sleep=time.sleep,
                 out=print):
        self.network = network
        self.node = node
        self.id = node.id
        self.args = args
        self.now = clock
        self.sleep = sleep
        self.out = out
        self.version = '?'
        self.githash = None
        self.legacy = False
        self.modes = 0
        self.saved = {}             # (index, sub) -> raw bytes, first value seen
        self.pdo_saved = []         # [(index, sub, raw)] to write back, in order
        self.emcy = []              # [(time, code, reg, mfg bytes)]
        self.lock = threading.Lock()
        self.tpdo1 = None           # (sw, mode, pos), the newest
        self.tpdo2 = None           # (vel, current)
        self.tpdo_count = [0, 0]
        self.guard = cts_s(args.guard_rpm)
        self._hb = None
        self._window = None

    # -- SDO -----------------------------------------------------------------
    def upload(self, index, sub):
        try:
            return bytes(self.node.sdo.upload(index, sub))
        except Exception as exc:        # noqa: BLE001 -- canopen raises its own
            code = abort_code(exc)
            if code is None:
                raise
            raise SdoAbort(code) from exc

    def download(self, index, sub, data):
        try:
            self.node.sdo.download(index, sub, bytes(data))
        except Exception as exc:        # noqa: BLE001
            code = abort_code(exc)
            if code is None:
                raise
            raise SdoAbort(code) from exc

    def r(self, index, sub, fmt):
        size = struct.calcsize('<' + fmt)
        return struct.unpack('<' + fmt, self.upload(index, sub)[:size].ljust(size, b'\0'))[0]

    def w(self, index, sub, value, fmt):
        self.download(index, sub, struct.pack('<' + fmt, value))

    def has(self, index, sub=0):
        try:
            self.upload(index, sub)
            return True
        except SdoAbort:
            return False

    def change(self, index, sub, value, fmt):
        """w(), remembering the value it replaces for restore()."""
        key = (index, sub)
        if key not in self.saved:
            self.saved[key] = self.upload(index, sub)
        self.w(index, sub, value, fmt)

    def restore(self):
        """Put back every value change() replaced, newest first."""
        for (index, sub), raw in reversed(list(self.saved.items())):
            try:
                self.download(index, sub, raw)
            except SdoAbort as exc:
                self.out('  restore {:04X},{} failed: {}'.format(index, sub, exc))
        self.saved.clear()

    # -- identity --------------------------------------------------------------
    def identify(self):
        self.version = read_version(self.node)
        self.githash = read_githash(self.node)
        self.legacy = firmware.detect(self.node).legacy
        self.modes = cia402.supported_modes(self.node)
        return self.version

    def offers(self, mode):
        """True when 0x6502 lists *mode* (a MODE_* constant)."""
        return bool(self.modes & cia402.mode_bit(cia402.drive_mode(self.node, mode)))

    def describe(self, code):
        if self.legacy and code in LEGACY_ERROR_CODES:
            return '{:#06x} {}'.format(code, LEGACY_ERROR_CODES[code])
        return cia402.describe_error(code)

    # -- EMCY ------------------------------------------------------------------
    def on_emcy(self, cob, data, timestamp=None):
        data = bytes(data).ljust(8, b'\0')
        code, reg = struct.unpack_from('<HB', data)
        mfg = data[3:8]
        with self.lock:
            self.emcy.append((self.now(), code, reg, mfg))
        if code == 0:
            text = 'error reset / no error'
        else:
            text = self.describe(code)
        bits = ', '.join(n for b, n in cia402.ERROR_REGISTER_BITS if reg & b) or 'none'
        self.out('  EMCY node {}: {} | register {:#04x} ({}) | mfg {}'.format(
            self.id, text, reg, bits, mfg.hex(' ')))

    def emcy_since(self, t0, codes=None):
        with self.lock:
            return [e for e in self.emcy if e[0] >= t0 and (codes is None or e[1] in codes)]

    # -- PDOs ------------------------------------------------------------------
    def on_tpdo1(self, cob, data, timestamp=None):
        sw, mode, pos = struct.unpack_from('<HbI', bytes(data).ljust(7, b'\0'))
        pos = pos - (1 << 32) if pos & 0x80000000 else pos
        with self.lock:
            self.tpdo1 = (sw, mode, pos)
            self.tpdo_count[0] += 1

    def on_tpdo2(self, cob, data, timestamp=None):
        vel, cur = struct.unpack_from('<ih', bytes(data).ljust(6, b'\0'))
        with self.lock:
            self.tpdo2 = (vel, cur)
            self.tpdo_count[1] += 1

    def _snapshot_pdo(self, comm, mapping, comm_subs):
        saved = []
        for sub in comm_subs:
            try:
                saved.append((comm, sub, self.upload(comm, sub)))
            except SdoAbort:
                pass
        n = self.upload(mapping, 0)[0]
        entries = [(mapping, s, self.upload(mapping, s)) for s in range(1, n + 1)]
        return saved, entries, n

    def map_pdos(self):
        """Map the PDOs for the tests (module docstring), in PRE-OPERATIONAL,
        after keeping the old mapping for unmap_pdos()."""
        self.node.nmt.state = 'PRE-OPERATIONAL'
        self.sleep(0.05)
        for name, (comm, mapping, base, trans, objects) in PDO_LAYOUT.items():
            tpdo = name.startswith('t')
            subs = (1, 2, 3, 5) if tpdo else (1, 2)
            saved, entries, n = self._snapshot_pdo(comm, mapping, subs)
            self.pdo_saved.append((comm, mapping, saved, entries, n))
            cob = base + self.id
            self.w(comm, 1, cob | COB_INVALID, 'I')
            self.w(comm, 2, trans, 'B')
            if tpdo:
                for sub in (3, 5):          # inhibit time, event timer: none
                    try:
                        self.w(comm, sub, 0, 'H')
                    except SdoAbort:
                        pass
            self.w(mapping, 0, 0, 'B')
            for k, (index, sub, bits) in enumerate(objects, 1):
                self.w(mapping, k, (index << 16) | (sub << 8) | bits, 'I')
            self.w(mapping, 0, len(objects), 'B')
            if tpdo:
                self.w(comm, 1, cob, 'I')       # RPDOs stay invalid: rpdos(True)
        self.network.subscribe(0x180 + self.id, self.on_tpdo1)
        self.network.subscribe(0x280 + self.id, self.on_tpdo2)
        self.node.nmt.state = 'OPERATIONAL'
        self.sleep(0.05)

    def rpdos(self, valid):
        """Make RPDO1/2 valid or invalid (COB-ID bit 31)."""
        for name in ('rpdo1', 'rpdo2'):
            comm, _, base, _, _ = PDO_LAYOUT[name]
            self.w(comm, 1, (base + self.id) | (0 if valid else COB_INVALID), 'I')

    def unmap_pdos(self):
        """Put the mapping map_pdos() found back (RAM)."""
        if not self.pdo_saved:
            return
        try:
            self.node.nmt.state = 'PRE-OPERATIONAL'
            self.sleep(0.05)
            for comm, mapping, saved, entries, n in reversed(self.pdo_saved):
                cob = next((raw for _, s, raw in saved if s == 1), None)
                if cob is not None:
                    self.w(comm, 1, struct.unpack('<I', cob[:4])[0] | COB_INVALID, 'I')
                self.w(mapping, 0, 0, 'B')
                for index, sub, raw in entries:
                    self.download(index, sub, raw)
                self.w(mapping, 0, n, 'B')
                for index, sub, raw in saved[::-1]:     # the COB-ID last
                    self.download(index, sub, raw)
        finally:
            self.pdo_saved = []
            self.node.nmt.state = 'OPERATIONAL'

    def send_rpdos(self, cw, mode, trq=0, vel=0, pos=0):
        self.network.send_message(0x200 + self.id, struct.pack(
            '<HBh', cw, cia402.mode_byte(cia402.drive_mode(self.node, mode)), int(trq)))
        self.network.send_message(0x300 + self.id, struct.pack('<ii', int(vel), int(pos)))

    def send_sync(self):
        self.network.send_message(SYNC_COB, b'')

    # -- heartbeat (the host as a producer, for 0x1016) ---------------------
    def heartbeat(self, on, host_id=None, period=0.1):
        if not on:
            if self._hb:
                self._hb[1].set()
                self._hb[0].join(1.0)
            self._hb = None
            return
        stop = threading.Event()

        def run():
            while not stop.is_set():
                self.network.send_message(HEARTBEAT_BASE + host_id, b'\x05')
                stop.wait(period)
        t = threading.Thread(target=run, daemon=True)
        self._hb = (t, stop)
        t.start()

    # -- CiA 402 ---------------------------------------------------------------
    def sw(self):
        return self.r(0x6041, 0, 'H')

    def cw(self, value):
        self.w(0x6040, 0, value, 'H')

    def enable(self):
        """Operation Enabled, or DriveFault saying why not."""
        if not self.clear_fault(enable=True):
            raise DriveFault('enable refused: ' + cia402.diagnose(self.node))

    def set_mode(self, mode):
        """0x6060 = *mode* (MODE_* numbering, mapped to 12/14 on 4.3.5).  The
        firmware takes it in Operation Enabled only.  TestSkip when refused."""
        value = cia402.drive_mode(self.node, mode)
        try:
            self.download(0x6060, 0, bytes([cia402.mode_byte(value)]))
        except SdoAbort as exc:
            if exc.code in UNSUPPORTED_ABORTS:
                raise TestSkip('mode {} refused ({:#010x})'.format(mode, exc.code)) from exc
            raise

    def clear_fault(self, enable=True, timeout=0.5):
        """Fault reset that works on both lines: bit 7 needs a rising edge
        (5.x), so the controlword goes to 0 first; while faulted any other
        controlword aborts (0x05040001 on 4.3.5, 0x08000022 on 5.x), which is
        ignored.  A reset during Fault Reaction Active (the stop after a trip,
        ~200 ms after a following error) is lost, so that ends first.  Then
        Shutdown, and Enable when *enable*.  True when the drive left FAULT."""
        end = self.now() + 2.0
        while self.sw() & 0x4F == 0x0F and self.now() < end:
            self.sleep(0.02)
        try:
            self.cw(CW_DISABLE_VOLTAGE)
        except SdoAbort:
            pass
        self.cw(CW_FAULT_RESET)
        end = self.now() + timeout
        sw = self.sw()
        while sw & SW_FAULT and self.now() < end:
            self.sleep(0.01)
            sw = self.sw()
        if sw & SW_FAULT:
            return False
        self.cw(CW_SHUTDOWN)
        if enable:
            self.cw(CW_ENABLE)
            sw = self.sw()
            if sw & SW_FAULT:
                return False
        return True

    def stop(self):
        """Mode idle and Shutdown, whatever state the drive is in."""
        try:
            sw = self.sw()
            if sw & SW_FAULT:
                self.clear_fault(enable=False)
            elif sw & cia402.SW_STATE_MASK == cia402.SW_OPERATION_ENABLED:
                self.set_mode(cia402.MODE_IDLE)
            self.cw(CW_SHUTDOWN)
        except (SdoAbort, TestSkip) as exc:
            self.out('  stop: {}'.format(exc))

    def disable(self):
        try:
            self.cw(CW_SHUTDOWN)
        except SdoAbort:
            pass

    # -- watching the rotor ----------------------------------------------------
    def sample(self):
        """(statusword, position, velocity) by SDO."""
        return self.sw(), self.r(0x6064, 0, 'i'), self.r(0x606C, 0, 'i')

    def check(self, sw, vel, expect_fault=False):
        if sw & SW_FAULT and not expect_fault:
            raise DriveFault('drive faulted: ' + cia402.diagnose(self.node))
        if abs(vel) > self.guard:
            self.disable()
            raise GuardTrip('{:.0f} rpm passed the {:.0f} rpm guard; drive disabled'.format(
                rpm(vel), self.args.guard_rpm))

    def watch(self, seconds, until=None, period=0.02):
        """Poll the drive for *seconds* or until *until*(sw, pos, vel) is true,
        checking the guard and faults; returns the samples [(t, sw, pos, vel)]
        and whether *until* was met."""
        t0 = self.now()
        out = []
        while True:
            sw, pos, vel = self.sample()
            t = self.now() - t0
            out.append((t, sw, pos, vel))
            self.check(sw, vel)
            if until is not None and until(sw, pos, vel):
                return out, True
            if t >= seconds:
                return out, False
            self.sleep(period)

    def cyclic(self, script, rate, seconds, hook=None):
        """The host side of a cyclic mode: each period *script*(t, k) returns
        (cw, mode, trq, vel, pos) for the RPDOs, then SYNC goes out.  None ends
        the loop early.  *hook*(t, k) runs before the RPDOs (an SDO write).
        Returns [(t, cmd, tpdo1, tpdo2)] and checks the guard on TPDO2."""
        period = 1.0 / rate
        rows = []
        with self.lock:             # a statusword from before the loop is stale
            self.tpdo1 = self.tpdo2 = None
        t0 = self.now()
        k = 0
        last_sw_poll = 0
        while True:
            t = k * period
            if t > seconds:
                break
            cmd = script(t, k)
            if cmd is None:
                break
            if hook:
                hook(t, k)
            self.send_rpdos(*cmd)
            self.send_sync()
            with self.lock:
                t1, t2 = self.tpdo1, self.tpdo2
            rows.append((t, cmd, t1, t2))
            vel = t2[0] if t2 else 0
            sw = t1[0] if t1 else 0
            if t1 is None and k - last_sw_poll >= 10:   # no TPDO1: poll
                sw, _, vel = self.sample()
                last_sw_poll = k
            self.check(sw, vel)
            k += 1
            wait = t0 + k * period - self.now()
            if wait > 0:
                self.sleep(wait)
        return rows

    def window(self):
        """How close counts as there: 0x6067 (5.x; the drive sets bit 10
        inside it), else --pos-tol."""
        if self._window is None:
            try:
                self._window = self.r(0x6067, 0, 'I') or self.args.pos_tol
            except SdoAbort:
                self._window = self.args.pos_tol
        return self._window

    def set_cyclic_period(self, rate):
        self.change(0x60C2, 1, int(round(1000.0 / rate)), 'B')
        self.change(0x60C2, 2, -3, 'b')


# ---------------------------------------------------------------------------
# The tests.  Each takes the Drive, runs in Operation Enabled and returns a
# detail string (PASS) or raises TestFail / TestSkip.  run_test() stops the
# drive and puts every changed parameter back after each one.
# ---------------------------------------------------------------------------
TESTS = []


def test(name, help_, mode=None, cyclic=False):
    def deco(fn):
        TESTS.append((name, fn, help_, mode, cyclic))
        return fn
    return deco


def enter(d, mode, rate=None):
    """Operation Enabled in *mode*.  A cyclic mode reads 0x60C2 as it is
    entered, so *rate* sets it first; the target position starts at the
    actual one, so nothing stale applies at entry."""
    if mode is not None and not d.offers(mode):
        raise TestSkip('0x6502 does not list mode {}'.format(mode))
    if rate:
        d.set_cyclic_period(rate)
    d.enable()
    if rate:
        d.w(0x607A, 0, d.r(0x6064, 0, 'i'), 'i')
    if mode is not None:
        d.set_mode(mode)


def home(d, offset=0):
    """Homing method 37 (here, no move): position becomes *offset*."""
    enter(d, cia402.MODE_HOMING)
    d.change(0x6098, 0, 37, 'b')
    d.change(0x607C, 0, offset, 'i')
    d.cw(CW_ENABLE)
    d.cw(CW_ENABLE | CW_NEW_SETPOINT)
    _, ok = d.watch(2.0, until=lambda sw, p, v: sw & SW_ACK)
    d.cw(CW_ENABLE)
    if not ok:
        raise TestFail('homing never attained (statusword bit 12)')
    return d.r(0x6064, 0, 'i')


def ramp_torque(t, cap, step=20, every=0.2):
    """The breakaway ramp: *step* per-mille every *every* s, up to *cap*.  The
    actuators differ by an order of magnitude in friction (a gearhead), so no
    one fixed torque both moves every rotor and stays gentle on a free one."""
    return min(cap, step * (1 + int(t / every)))


@test('pt', 'profile torque: a torque ramp each way until the rotor spins at --spin-rpm',
      cia402.MODE_PROFILE_TRQ)
def t_pt(d):
    enter(d, cia402.MODE_PROFILE_TRQ)
    target = cts_s(d.args.spin_rpm)
    got = []
    for sign in (1, -1):
        t0 = d.now()
        trq = 0

        def until(sw, p, v):
            nonlocal trq
            if v * sign > target:
                return True
            new = ramp_torque(d.now() - t0, d.args.torque)
            if new != trq:
                trq = new
                d.w(0x6071, 0, sign * trq, 'h')
            return False
        s, ok = d.watch(3.0, until=until)
        d.w(0x6071, 0, 0, 'h')
        if not ok:
            raise TestFail('{:+d} permille never reached {:+.0f} rpm (got {:+.0f})'.format(
                sign * trq, sign * d.args.spin_rpm, rpm(s[-1][3])))
        got.append('{:+d} permille -> {:+.0f} rpm in {:.2f} s'.format(
            sign * trq, rpm(s[-1][3]), s[-1][0]))
        d.watch(0.5)
    return ', '.join(got)


@test('pv', 'profile velocity: steps of 200 / 1000 / -1000 / 0 rpm', cia402.MODE_PROFILE_VEL)
def t_pv(d):
    enter(d, cia402.MODE_PROFILE_VEL)
    worst = 0.0
    for target in (200, 1000, -1000, 0):
        d.w(0x60FF, 0, int(cts_s(target)), 'i')
        s, _ = d.watch(1.2)
        tail = [rpm(v) for t, sw, p, v in s if t >= 0.8]
        mean = sum(tail) / len(tail)
        err = abs(mean - target)
        worst = max(worst, err)
        if err > 0.1 * abs(target) + 30:
            raise TestFail('{} rpm: settled at {:.0f}'.format(target, mean))
    return 'worst settled error {:.0f} rpm'.format(worst)


def _pp_move(d, base_cw, target, timeout=6.0, reach=None):
    """One pp set-point: wait for the buffer, new set-point, acknowledge; then,
    with *reach* (the absolute position it ends at), wait for it.  Bit 10 alone
    is not enough: it still holds the previous target's 'reached' just after
    the acknowledge."""
    _, ok = d.watch(timeout, until=lambda sw, p, v: not sw & SW_ACK)
    if not ok:
        raise TestFail('set-point acknowledge never cleared')
    d.w(0x607A, 0, int(target), 'i')
    d.cw(base_cw | CW_NEW_SETPOINT)
    _, ok = d.watch(1.0, until=lambda sw, p, v: sw & SW_ACK)
    d.cw(base_cw)
    if not ok:
        raise TestFail('set-point {} never acknowledged'.format(target))
    if reach is not None:
        tol = d.window()
        s, ok = d.watch(timeout, until=lambda sw, p, v: sw & SW_TARGET_REACHED
                        and abs(p - reach) <= tol)
        if not ok:
            raise TestFail('target {} not reached: at {}, statusword {:#06x}'.format(
                reach, s[-1][2], s[-1][1]))


def _pp_profile(d):
    d.change(0x6081, 0, 10000, 'I')
    d.change(0x6083, 0, 20000, 'I')
    d.change(0x6084, 0, 20000, 'I')
    if d.has(0x6082):
        d.change(0x6082, 0, 0, 'I')


def _pp_discrete(d, base_cw, moves, relative):
    home(d, 0)
    d.set_mode(cia402.MODE_PROFILE_POS)
    _pp_profile(d)
    d.cw(base_cw)
    expect = 0
    worst = 0
    for m in moves:
        expect = expect + m if relative else m
        _pp_move(d, base_cw, m, reach=expect)
        pos = d.r(0x6064, 0, 'i')
        worst = max(worst, abs(pos - expect))
        if abs(pos - expect) > d.window():
            raise TestFail('target {} reached at {}'.format(expect, pos))
    return '{} moves, worst end error {} cts, 0x60F4 {}'.format(
        len(moves), worst, d.r(0x60F4, 0, 'i'))


@test('ppiad', 'profile position, immediate absolute, discrete set-points',
      cia402.MODE_PROFILE_POS)
def t_ppiad(d):
    return _pp_discrete(d, CW_ENABLE | CW_IMMEDIATE, (4096, 8192, -4096, 0), False)


@test('ppird', 'profile position, immediate relative, discrete set-points',
      cia402.MODE_PROFILE_POS)
def t_ppird(d):
    return _pp_discrete(d, CW_ENABLE | CW_IMMEDIATE | CW_RELATIVE,
                        (2048, 2048, -2048, -2048), True)


def _pp_buffered(d, base_cw, moves, relative):
    """Queue each set-point as soon as the drive frees its buffer (bit 12
    low), without waiting for the previous one to finish."""
    home(d, 0)
    d.set_mode(cia402.MODE_PROFILE_POS)
    _pp_profile(d)
    d.cw(base_cw)
    expect = 0
    for m in moves:
        _pp_move(d, base_cw, m)
        expect = expect + m if relative else m
    _, ok = d.watch(10.0, until=lambda sw, p, v: sw & SW_TARGET_REACHED and not sw & SW_ACK
                    and abs(p - expect) <= d.window())
    pos = d.r(0x6064, 0, 'i')
    if not ok or abs(pos - expect) > d.window():
        raise TestFail('queued moves ended at {} (want {}, target reached {})'.format(
            pos, expect, ok))
    return '{} queued moves, ended at {} cts'.format(len(moves), pos)


@test('ppba', 'profile position, buffered absolute set-points', cia402.MODE_PROFILE_POS)
def t_ppba(d):
    return _pp_buffered(d, CW_ENABLE, (4096, 8192, -4096, 0), False)


@test('ppbr', 'profile position, buffered relative set-points', cia402.MODE_PROFILE_POS)
def t_ppbr(d):
    return _pp_buffered(d, CW_ENABLE | CW_RELATIVE, (2048, 2048, -2048, -2048), True)


def _pp_streamed(d, rate):
    """Set-points streamed over RPDO at *rate*: a 1 rev, 4 s sinusoid; the new
    set-point bit follows the acknowledge in TPDO1 (handshake, not a toggle)."""
    home(d, 0)
    d.set_mode(cia402.MODE_PROFILE_POS)
    _pp_profile(d)
    d.change(0x6081, 0, int(cts_s(300)), 'I')
    d.change(0x6083, 0, 200000, 'I')
    d.change(0x6084, 0, 200000, 'I')
    base = CW_ENABLE | CW_IMMEDIATE
    amp, per, run = RES, 4.0, 8.0
    state = {'cw': base, 'acks': 0}

    def script(t, k):
        tp = d.tpdo1
        ack = bool(tp and tp[0] & SW_ACK)
        if state['cw'] & CW_NEW_SETPOINT and ack:
            state['cw'] = base
            state['acks'] += 1
        elif not state['cw'] & CW_NEW_SETPOINT and not ack:
            state['cw'] = base | CW_NEW_SETPOINT
        target = amp * math.sin(2 * math.pi * t / per) if t < run else 0
        return state['cw'], cia402.MODE_PROFILE_POS, 0, 0, target

    d.rpdos(True)
    rows = d.cyclic(script, rate, run + 2.0)
    d.rpdos(False)
    pos = [r[2][2] for r in rows if r[2]]
    if not pos:
        raise TestFail('no TPDO1')
    swing = max(pos) - min(pos)
    end = d.r(0x6064, 0, 'i')
    if state['acks'] < rate * run / 4:
        raise TestFail('{} set-points acknowledged in {:.0f} s'.format(state['acks'], run))
    if swing < amp or abs(end) > d.window():
        raise TestFail('swing {} cts (want > {}), end {}'.format(swing, amp, end))
    return '{} set-points acknowledged, swing {} cts, end {} cts'.format(
        state['acks'], swing, end)


@test('ppias10', 'profile position, immediate absolute, streamed at 10 Hz',
      cia402.MODE_PROFILE_POS, cyclic=True)
def t_ppias10(d):
    return _pp_streamed(d, 10)


@test('ppias100', 'profile position, immediate absolute, streamed at 100 Hz',
      cia402.MODE_PROFILE_POS, cyclic=True)
def t_ppias100(d):
    return _pp_streamed(d, 100)


@test('home', 'homing method 37: the position becomes the homing offset', cia402.MODE_HOMING)
def t_home(d):
    got = []
    for offset in (1234, 0):
        pos = home(d, offset)
        got.append(pos)
        if abs(pos - offset) > 2:
            raise TestFail('offset {}: position {}'.format(offset, pos))
    return 'offset 1234 -> {}, 0 -> {}'.format(*got)


def _csi_gains(d):
    """The drive's CSI gains (from its CSV), or --csi-kp/--csi-kd.  Gains
    left at 0 get the usual start from the drive's own loop: kd = velocity
    Kp 0x2381,1, kp = position Kp 0x2382,1 x velocity Kp."""
    kp, kd = d.r(0x2383, 1, 'f'), d.r(0x2383, 2, 'f')
    vel_kp = d.r(0x2381, 1, 'f')
    if not kp:
        kp = d.r(0x2382, 1, 'H') * vel_kp
    if not kd:
        kd = vel_kp
    kp = d.args.csi_kp if d.args.csi_kp is not None else kp
    kd = d.args.csi_kd if d.args.csi_kd is not None else kd
    if not kp or not kd:
        raise TestFail('no CSI gains: 0x2383 and the loop gains are 0 (give --csi-kp/--csi-kd)')
    d.change(0x2383, 1, kp, 'f')
    d.change(0x2383, 2, kd, 'f')
    return kp, kd


def _cyclic_tracking(d, mode, series, amp, per, cycles, home_first=False, min_corr=0.9,
                     ratio=(0.7, 1.3)):
    """A sinusoid in *series* ('vel' or 'pos'), checked against TPDO feedback."""
    if home_first:
        home(d, 0)
    rate = d.args.rate
    gains = ''
    if mode == cia402.MODE_CYCLIC_SYNC_IMP:
        gains = ', kp {:.4g} kd {:.4g}'.format(*_csi_gains(d))
    enter(d, mode, rate)
    w = 2 * math.pi / per
    run = per * cycles

    def script(t, k):
        s = amp * math.sin(w * t) if t < run else 0.0
        ds = amp * w * math.cos(w * t) if t < run else 0.0
        if series == 'vel':
            return CW_ENABLE, mode, 0, s, 0
        return CW_ENABLE, mode, 0, ds if mode == cia402.MODE_CYCLIC_SYNC_IMP else 0, s

    d.rpdos(True)
    rows = d.cyclic(script, rate, run + 0.5)
    d.rpdos(False)
    pairs = [(r[1][3] if series == 'vel' else r[1][4],
              (r[3][0] if series == 'vel' else r[2][2]))
             for r in rows if r[0] < run and r[2] and r[3]]
    if len(pairs) < 0.8 * run * rate:
        raise TestFail('{} TPDO pairs for {:.0f} SYNCs'.format(len(pairs), run * rate))
    target = [p[0] for p in pairs]
    actual = [p[1] for p in pairs]
    corr, lag, gain = correlate(target, actual, int(0.25 * rate))
    detail = 'corr {:.3f}, lag {:.0f} ms, amplitude x{:.2f}, TPDO {}/{}'.format(
        corr, 1000.0 * lag / rate, gain, d.tpdo_count[0], d.tpdo_count[1])
    if corr < min_corr or not ratio[0] <= gain <= ratio[1]:
        raise TestFail(detail)
    return detail + gains


@test('cst', 'cyclic sync torque: a torque ramp each way until the rotor spins',
      cia402.MODE_CYCLIC_SYNC_TRQ, cyclic=True)
def t_cst(d):
    enter(d, cia402.MODE_CYCLIC_SYNC_TRQ, d.args.rate)
    target = cts_s(d.args.spin_rpm)
    st = {'phase': 0, 't': 0.0, 'trq': 0, 'reached': []}

    def script(t, k):
        vel = d.tpdo2[0] if d.tpdo2 else 0
        ph = st['phase']
        if ph in (0, 2):
            sign = 1 if ph == 0 else -1
            if vel * sign > target:
                st['reached'].append((sign * st['trq'], t - st['t']))
                st['phase'], st['t'] = ph + 1, t
            elif t - st['t'] > 3.0:
                return None
            else:
                st['trq'] = ramp_torque(t - st['t'], d.args.torque)
                return CW_ENABLE, cia402.MODE_CYCLIC_SYNC_TRQ, sign * st['trq'], 0, 0
        elif t - st['t'] > 0.5:
            if ph == 3:
                return None
            st['phase'], st['t'] = ph + 1, t
        return CW_ENABLE, cia402.MODE_CYCLIC_SYNC_TRQ, 0, 0, 0

    d.rpdos(True)
    d.cyclic(script, d.args.rate, 8.0)
    d.rpdos(False)
    if len(st['reached']) < 2:
        raise TestFail('{:+.0f} rpm reached with {} only (cap {} permille)'.format(
            d.args.spin_rpm, [t for t, _ in st['reached']] or 'neither sign', d.args.torque))
    return ', '.join('{:+d} permille -> {:.0f} rpm in {:.2f} s'.format(
        trq, d.args.spin_rpm, dt) for trq, dt in st['reached'])


@test('csv', 'cyclic sync velocity: a 500 rpm, 2 s sinusoid', cia402.MODE_CYCLIC_SYNC_VEL,
      cyclic=True)
def t_csv(d):
    return _cyclic_tracking(d, cia402.MODE_CYCLIC_SYNC_VEL, 'vel', cts_s(500), 2.0, 2)


@test('csp', 'cyclic sync position: a 1 rev, 2 s sinusoid', cia402.MODE_CYCLIC_SYNC_POS,
      cyclic=True)
def t_csp(d):
    return _cyclic_tracking(d, cia402.MODE_CYCLIC_SYNC_POS, 'pos', RES, 2.0, 2,
                            home_first=True)


@test('csi', 'cyclic sync impedance (-2): a 0.5 rev, 2 s sinusoid',
      cia402.MODE_CYCLIC_SYNC_IMP, cyclic=True)
def t_csi(d):
    return _cyclic_tracking(d, cia402.MODE_CYCLIC_SYNC_IMP, 'pos', RES // 2, 2.0, 2,
                            home_first=True, min_corr=0.8, ratio=(0.5, 1.5))


def _pole_pairs(d):
    pp = d.r(0x3011, 3, 'B') // 2
    if pp < 1:
        raise TestFail('0x3011,3 poles is {}'.format(pp * 2))
    return pp


def _follows(d, moved, erevs, pp):
    want = erevs * RES / pp
    if abs(abs(moved) - want) > 0.3 * want:
        raise TestFail('{} electrical revs moved {} cts (want {:.0f})'.format(
            erevs, moved, want))
    return moved


@test('cstca', 'cyclic sync torque at the host angle (11): the rotor follows 0x60EA',
      cia402.MODE_CYCLIC_SYNC_TRQ_ANGLE, cyclic=True)
def t_cstca(d):
    pp = _pole_pairs(d)
    enter(d, cia402.MODE_CYCLIC_SYNC_TRQ_ANGLE, d.args.rate)
    theta0 = d.r(0x60EA, 0, 'h')
    erevs, per = 2, 2.0     # 2 electrical revolutions forward, a 0.5 s rest, then back
    lead = 0.3              # at theta0 first, for the rotor to settle on the field
    mark = {}

    def angle(t):
        f = (min(t, per) - max(0.0, min(t - per - 0.5, per))) / per
        return int(theta0 + 65536 * erevs * f + 32768) % 65536 - 32768

    def hook(t, k):
        d.w(0x60EA, 0, angle(max(0.0, t - lead)), 'h')
        if 'start' not in mark and t >= lead:
            mark['start'] = d.r(0x6064, 0, 'i')
        if 'mid' not in mark and t >= lead + per + 0.4:
            mark['mid'] = d.r(0x6064, 0, 'i')

    def script(t, k):
        return CW_ENABLE, cia402.MODE_CYCLIC_SYNC_TRQ_ANGLE, d.args.torque, 0, 0

    d.rpdos(True)
    d.cyclic(script, d.args.rate, lead + 2 * per + 0.8, hook=hook)
    d.rpdos(False)
    end = d.r(0x6064, 0, 'i')
    fwd = _follows(d, mark['mid'] - mark['start'], erevs, pp)
    back = _follows(d, end - mark['mid'], erevs, pp)
    if fwd * back > 0:
        raise TestFail('the rotor went the same way both times ({} then {})'.format(fwd, back))
    return '{} pole pairs: {:+d} then {:+d} cts for {} electrical revs each way'.format(
        pp, fwd, back, erevs)


@test('pva', 'phase voltage and angle (-1): the rotor follows 0x60EA on a d-axis field',
      cia402.MODE_PHASE_VOLTAGE_ANGLE)
def t_pva(d):
    pp = _pole_pairs(d)
    try:
        base_ma, saturated = id_scale(d.node)
    except (KeyError, AttributeError):
        base_ma, saturated = d.r(0x3011, 9, 'H'), RAMP_ID_SATURATED
    i_cal = d.r(0x3011, 11, 'H')
    settle = max(0.02, id_filter_settle_s(d.node))     # 4.3.x's slow id filter
    want_ma = min(i_cal or base_ma, base_ma) * d.args.pva_fraction
    enter(d, cia402.MODE_PHASE_VOLTAGE_ANGLE)
    try:
        d.w(0x3010, 5, 0, 'h')          # uq 0: the firmware takes the host's angle
        theta = 0
        d.w(0x60EA, 0, theta, 'h')
        ud = 0
        id_ = 0
        while ud < d.args.pva_ud_max:
            ud = min(ud + 100, d.args.pva_ud_max)
            d.w(0x3010, 4, ud, 'h')
            d.sleep(settle)
            id_ = d.r(0x3010, 6, 'h')
            if id_ >= saturated or id_ / 1000.0 * base_ma >= want_ma:
                break
        d.sleep(0.3)
        steps = 32
        moved = []
        for direction in (1, -1):
            start = d.r(0x6064, 0, 'i')
            for _ in range(steps):
                theta += direction * 65536 // steps
                d.w(0x60EA, 0, (theta + 32768) % 65536 - 32768, 'h')
                d.sleep(0.03)
                sw, pos, vel = d.sample()
                d.check(sw, vel)
            d.sleep(0.2)
            moved.append(_follows(d, d.r(0x6064, 0, 'i') - start, 1, pp))
    finally:
        d.w(0x3010, 4, 0, 'h')
    if moved[0] * moved[1] > 0:
        raise TestFail('the rotor went the same way both times {}'.format(moved))
    return 'ud {} ({:.0f} mA d-axis): {:+d} then {:+d} cts per electrical rev'.format(
        ud, id_ / 1000.0 * base_ma, moved[0], moved[1])


# -- fault cases ---------------------------------------------------------------
def _expect_emcy(d, t0, codes, within):
    end = d.now() + within
    while d.now() < end:
        got = d.emcy_since(t0, codes)
        if got:
            return got[0]
        d.sleep(0.01)
    return None


@test('syncloss', 'SYNC stops in CSP with 0x2406 = 100 ms: trips, then clears with SYNC back',
      cia402.MODE_CYCLIC_SYNC_POS, cyclic=True)
def t_syncloss(d):
    if not d.has(0x2406):
        raise TestSkip('no 0x2406')
    home(d, 0)
    rate = d.args.rate
    enter(d, cia402.MODE_CYCLIC_SYNC_POS, rate)
    hold = d.r(0x6064, 0, 'i')

    def arm(t, k):
        # The firmware's SYNC-loss count runs whenever no SYNC comes and only a
        # SYNC zeroes it: 0x2406 set after a quiet spell trips at once, so it
        # is set (and later put back) with SYNC flowing.
        if k == int(0.2 * rate):
            d.change(0x2406, 0, 100, 'H')
    d.rpdos(True)
    d.cyclic(lambda t, k: (CW_ENABLE, cia402.MODE_CYCLIC_SYNC_POS, 0, 0, hold), rate, 1.0,
             hook=arm)
    d.rpdos(False)
    t0 = d.now()
    emcy = _expect_emcy(d, t0, SYNC_TIMEOUT_CODES, 1.0)
    tripped = d.now() - t0
    sw = d.sw()
    # SYNC back before anything else: a reset without it re-trips at once,
    # and 0x2406 goes back to 0 only while SYNC runs.
    stop = threading.Event()

    def sync_thread():
        while not stop.is_set():
            d.send_sync()
            stop.wait(1.0 / rate)
    th = threading.Thread(target=sync_thread, daemon=True)
    th.start()
    try:
        d.sleep(0.1)
        cleared = d.clear_fault(enable=False)
        d.restore()
    finally:
        stop.set()
        th.join(1.0)
    if emcy is None:
        raise TestFail('no SYNC-timeout EMCY in 1 s (statusword {:#06x})'.format(sw))
    if not sw & SW_FAULT:
        raise TestFail('{} but statusword {:#06x} is not FAULT'.format(
            d.describe(emcy[1]), sw))
    if not cleared:
        raise TestFail('the fault did not clear with SYNC back: ' + cia402.diagnose(d.node))
    return '{} {:.0f} ms after the last SYNC; cleared'.format(
        d.describe(emcy[1]), 1000 * (emcy[0] - t0) if emcy else tripped)


@test('heartbeat', 'host heartbeat stops with 0x1016 watching it: trips 0x8130, clears',
      cia402.MODE_PROFILE_VEL)
def t_heartbeat(d):
    if not d.has(0x1016, 1):
        raise TestSkip('no 0x1016 (heartbeat consumer)')
    behaviour = d.r(0x1029, 1, 'B') if d.has(0x1029, 1) else None
    hid = d.args.host_id
    enter(d, cia402.MODE_PROFILE_VEL)
    d.w(0x60FF, 0, 0, 'i')
    d.heartbeat(True, hid)
    try:
        d.sleep(0.3)
        d.change(0x1016, 1, (hid << 16) | 300, 'I')
        t0 = d.now()
        d.watch(1.0)
        early = d.emcy_since(t0, HEARTBEAT_CODES)
    finally:
        d.heartbeat(False)
    t1 = d.now()
    emcy = _expect_emcy(d, t1, HEARTBEAT_CODES, 1.5)
    sw = d.sw()
    d.restore()                         # consumer off before the reset
    state = d.node.nmt.state
    d.node.nmt.state = 'OPERATIONAL'
    cleared = d.clear_fault(enable=False)
    if early:
        raise TestFail('0x8130 while the heartbeat was still running')
    if emcy is None:
        raise TestFail('no 0x8130 in 1.5 s after the heartbeat stopped')
    if not cleared:
        raise TestFail('did not clear: ' + cia402.diagnose(d.node))
    return '0x8130 {:.0f} ms after the last heartbeat (300 ms window); statusword {:#06x}, ' \
           'NMT {} (0x1029,1 = {}); cleared'.format(1000 * (emcy[0] - t1), sw, state, behaviour)


@test('following', 'following error in CSP: 0x2120 window 20 cts, a 0.1 rev target jump',
      cia402.MODE_CYCLIC_SYNC_POS, cyclic=True)
def t_following(d):
    if not d.has(0x2120):
        raise TestSkip('no 0x2120 (following error window)')
    home(d, 0)
    rate = d.args.rate
    enter(d, cia402.MODE_CYCLIC_SYNC_POS, rate)
    d.change(0x2120, 0, 20, 'I')
    d.change(0x2121, 0, 2, 'H')
    hold = d.r(0x6064, 0, 'i')
    t0 = d.now()
    jump = RES // 10

    def script(t, k):
        if t > 0.3 and d.tpdo1 and d.tpdo1[0] & SW_FAULT:
            return None
        return CW_ENABLE, cia402.MODE_CYCLIC_SYNC_POS, 0, 0, hold + (jump if t >= 0.3 else 0)

    d.rpdos(True)
    try:
        d.cyclic(script, rate, 1.5)
    except DriveFault:
        pass
    finally:
        d.rpdos(False)
    emcy = _expect_emcy(d, t0, FOLLOWING_CODES[d.legacy], 0.5)
    sw = d.sw()
    d.restore()
    cleared = d.clear_fault(enable=False)
    if emcy is None:
        raise TestFail('no following-error EMCY ({}), statusword {:#06x}'.format(
            ', '.join('{:#06x}'.format(c) for c in FOLLOWING_CODES[d.legacy]), sw))
    if not sw & SW_FAULT:
        raise TestFail('{} but not FAULT (statusword {:#06x})'.format(d.describe(emcy[1]), sw))
    if not cleared:
        raise TestFail('did not clear: ' + cia402.diagnose(d.node))
    return '{}; cleared'.format(d.describe(emcy[1]))


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def run_test(d, name, fn, mode=None):
    t0 = d.now()
    try:
        if mode is not None and not d.offers(mode):
            raise TestSkip('0x6502 does not list mode {}'.format(mode))
        detail = fn(d)
        status = 'PASS'
    except TestSkip as exc:
        status, detail = 'SKIP', str(exc)
    except TestFail as exc:
        status, detail = 'FAIL', str(exc)
    except SdoAbort as exc:
        status, detail = 'FAIL', str(exc)
    finally:
        try:
            d.rpdos(False)
        except SdoAbort:
            pass
        d.stop()
        d.restore()
    return Result(name, status, detail, d.now() - t0)


def select(spec):
    names = [t[0] for t in TESTS]
    if spec in (None, 'all'):
        return list(TESTS)
    want = [s.strip() for s in spec.split(',') if s.strip()]
    bad = [w for w in want if w not in names]
    if bad:
        raise SystemExit('unknown test(s): {} (have: {})'.format(', '.join(bad), ', '.join(names)))
    return [t for t in TESTS if t[0] in want]


def summary(results, out=print):
    width = max([len(r.name) for r in results] + [4])
    out('')
    out('{:<{w}}  {:<4}  {:>6}  {}'.format('test', 'res', 'time', 'detail', w=width))
    for r in results:
        out('{:<{w}}  {:<4}  {:>5.1f}s  {}'.format(r.name, r.status, r.seconds, r.detail,
                                                  w=width))
    counts = {s: sum(r.status == s for r in results) for s in ('PASS', 'FAIL', 'SKIP')}
    out('{PASS} passed, {FAIL} failed, {SKIP} skipped'.format(**counts))
    return 1 if counts['FAIL'] else 0


def run(d, tests, out=print):
    """Map the PDOs, run *tests*, put everything back; the exit code."""
    out('node {}: firmware {}{}, 0x6502 = {:#x}{}'.format(
        d.id, d.version, ' ' + d.githash if d.githash else '', d.modes,
        ' (4.3.x line)' if d.legacy else ''))
    d.network.subscribe(EMCY_BASE + d.id, d.on_emcy)
    results = []
    try:
        d.map_pdos()
        for name, fn, help_, mode, _ in tests:
            out('-- {}: {}'.format(name, help_))
            r = run_test(d, name, fn, mode)
            out('   {} {}'.format(r.status, r.detail))
            results.append(r)
    except KeyboardInterrupt:
        out('interrupted')
        d.stop()
    finally:
        d.restore()
        d.unmap_pdos()
    return summary(results, out)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog=__doc__.split('\n', 1)[1])
    p.add_argument('--can', default=os.environ.get('P4_CAN', 'can0'),
                   help='CAN interface; on Windows the adapter index (default $P4_CAN or can0)')
    p.add_argument('--id', type=int, dest='node_id', help='node ID (required)')
    p.add_argument('--fd', action='store_true', help='CAN FD frames')
    p.add_argument('--test', default='all', help='NAME[,NAME...] or all (default)')
    p.add_argument('--list', action='store_true', help='list the tests and exit')
    p.add_argument('--rate', type=float, default=100.0,
                   help='SYNC rate in the cyclic tests, Hz (default 100)')
    p.add_argument('--guard-rpm', type=float, default=3000.0,
                   help='disable the drive above this speed (default 3000)')
    p.add_argument('--torque', type=int, default=300,
                   help='pt / cst: the torque ramp stops here; cstca: its torque '
                        '(per-mille of rated, default 300)')
    p.add_argument('--spin-rpm', type=float, default=300.0,
                   help='pt / cst: the speed each torque has to reach (default 300)')
    p.add_argument('--pos-tol', type=int, default=50,
                   help='pp end position tolerance where 0x6067 is missing (4.3.5), cts '
                        '(default 50)')
    p.add_argument('--pva-fraction', type=float, default=0.5,
                   help='pva d-axis current, fraction of i_cal (default 0.5)')
    p.add_argument('--pva-ud-max', type=int, default=4000,
                   help='pva: the most ud the ramp may use (default 4000 of 32767)')
    p.add_argument('--csi-kp', type=float,
                   help='csi stiffness 0x2383,1, per-mille/count, in RAM (default: the '
                        'drive\'s, or position Kp x velocity Kp when it is 0)')
    p.add_argument('--csi-kd', type=float,
                   help='csi damping 0x2383,2, per-mille/(count/s), in RAM (default: the '
                        'drive\'s, or velocity Kp when it is 0)')
    p.add_argument('--host-id', type=int, default=0x7D,
                   help='heartbeat test: the node ID the host produces as (default 125)')
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.list:
        for name, _, help_, _, _ in TESTS:
            print('{:<10} {}'.format(name, help_))
        return 0
    if args.node_id is None:
        raise SystemExit('--id is required')
    tests = select(args.test)
    import canopen
    from p4core.can_backend import make_network
    from p4core.session import P4Network
    try:
        network = make_network(args.can, fd=args.fd, data_bitrate=5_000_000 if args.fd else None,
                               network_cls=P4Network)
    except Exception as exc:        # noqa: BLE001
        print('could not open {}: {}'.format(args.can, exc))
        return 2
    if args.fd:
        network.can_fd = True
    try:
        node = network.add_node(args.node_id, canopen.ObjectDictionary())
        d = Drive(network, node, args)
        try:
            d.identify()
        except Exception as exc:    # noqa: BLE001
            print('node {} does not answer: {}'.format(args.node_id, exc))
            return 2
        return run(d, tests)
    finally:
        network.disconnect()


if __name__ == '__main__':
    sys.exit(main())
