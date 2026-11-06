"""sandbox/testp4.py against a fake puck: the CiA 402 handling both firmware
lines need (fault reset, refused modes, the 4.3.5 mode numbers), the PDO
mapping and its restore, EMCY decoding, the cyclic loop and the runner."""

import contextlib
import io
import os
import struct
import sys
import unittest

from canopen.sdo.exceptions import SdoAbortedError

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'sandbox'))

import testp4 as T                                          # noqa: E402

SW_SOD, SW_RTSO, SW_OE, SW_FAULT, SW_FR = 0x0250, 0x0231, 0x0237, 0x0238, 0x021F


class Clock:
    def __init__(self):
        self.t = 0.0
        self.plant = None

    def now(self):
        return self.t

    def sleep(self, dt):
        self.t += max(dt, 1e-4)
        if self.plant:
            self.plant.tick(self.t)


class FakeSdo:
    """A puck's object dictionary by index, with just enough CiA 402: the
    state machine, mode entry in Operation Enabled only, pv, homing method
    37 and pp set-points that land at once."""

    def __init__(self, legacy=False):
        self.legacy = legacy
        self.store = {}
        self.writes = []
        self.fault_reaction_until = None
        self.cw = 0
        v = lambda fmt, x: struct.pack('<' + fmt, x)       # noqa: E731
        self.store.update({
            (0x100A, 0): v('I', 0x04000305) if legacy else b'5.0.5',
            (0x6502, 0): v('I', 0x0BAD if legacy else 0x307AD),
            (0x6041, 0): v('H', SW_SOD), (0x6060, 0): b'\0', (0x6061, 0): b'\0',
            (0x6064, 0): v('i', 0), (0x606C, 0): v('i', 0), (0x6078, 0): v('h', 0),
            (0x607A, 0): v('i', 0), (0x60FF, 0): v('i', 0), (0x6071, 0): v('h', 0),
            (0x607C, 0): v('i', 0), (0x6098, 0): v('b', 0), (0x60F4, 0): v('i', 0),
            (0x6081, 0): v('I', 1), (0x6083, 0): v('I', 1022803), (0x6084, 0): v('I', 1022803),
            (0x60C2, 1): v('B', 1), (0x60C2, 2): v('b', -3), (0x2406, 0): v('H', 0),
            (0x603F, 0): v('H', 0), (0x1001, 0): b'\0', (0x6040, 0): v('H', 0),
        })
        if not legacy:
            self.store[(0x3003, 0)] = v('I', 0x0be59679)
            self.store[(0x6067, 0)] = v('I', 100)
        for comm, mapping, cob in ((0x1400, 0x1600, 0x200), (0x1401, 0x1601, 0x300),
                                   (0x1800, 0x1A00, 0x180), (0x1801, 0x1A01, 0x280)):
            self.store[(comm, 1)] = v('I', cob + 4)
            self.store[(comm, 2)] = v('B', 255 if comm < 0x1800 else 1)
            self.store[(mapping, 0)] = v('B', 1)
            self.store[(mapping, 1)] = v('I', 0x60400010)

    # -- helpers ---------------------------------------------------------------
    def get(self, index, sub, fmt):
        return struct.unpack('<' + fmt, self.store[(index, sub)][:struct.calcsize('<' + fmt)])[0]

    def put(self, index, sub, fmt, x):
        self.store[(index, sub)] = struct.pack('<' + fmt, x)

    def sw(self):
        return self.get(0x6041, 0, 'H')

    def fault(self, code=0x8611, reaction=0.0, now=0.0):
        self.put(0x603F, 0, 'H', code)
        if reaction:
            self.put(0x6041, 0, 'H', SW_FR)
            self.fault_reaction_until = now + reaction
        else:
            self.put(0x6041, 0, 'H', SW_FAULT)

    def tick(self, now):
        if self.fault_reaction_until is not None and now >= self.fault_reaction_until:
            self.fault_reaction_until = None
            self.put(0x6041, 0, 'H', SW_FAULT)
        mode = struct.unpack('b', self.store[(0x6061, 0)])[0]
        if self.sw() == SW_OE and mode == 3:
            self.put(0x606C, 0, 'i', self.get(0x60FF, 0, 'i'))

    # -- the SDO server ----------------------------------------------------------
    def upload(self, index, sub):
        if (index, sub) not in self.store:
            raise SdoAbortedError(0x06020000)
        return self.store[(index, sub)]

    def download(self, index, sub, data, force_segment=False):
        if (index, sub) not in self.store and not 0x1400 <= index < 0x1C00:
            raise SdoAbortedError(0x06020000)
        self.writes.append((index, sub, bytes(data)))
        if index == 0x6040:
            return self._controlword(struct.unpack('<H', data[:2])[0])
        if index == 0x6060:
            if self.sw() & 0x6F != 0x27:
                raise SdoAbortedError(0x08000022)
            mode = struct.unpack('b', data[:1])[0]
            bit = 1 << (15 - mode if mode < 0 else mode - 1) if mode else ~0
            if not self.get(0x6502, 0, 'I') & bit:
                raise SdoAbortedError(0x05040001 if self.legacy else 0x06090030)
            self.store[(0x6061, 0)] = bytes(data[:1])
        self.store[(index, sub)] = bytes(data)

    def _controlword(self, cw):
        sw = self.sw()
        if sw & 0x08:                                   # FAULT or Fault Reaction
            rising = cw & 0x80 and not self.cw & 0x80
            if rising and sw == SW_FAULT:
                self.put(0x6041, 0, 'H', SW_SOD)
                self.put(0x603F, 0, 'H', 0)
            elif not cw & 0x80:
                raise SdoAbortedError(0x05040001 if self.legacy else 0x08000022)
            self.cw = cw
            return
        prev, self.cw = self.cw, cw
        if cw & 0x80:
            return
        if cw & 0x0F == 0x06:
            self.put(0x6041, 0, 'H', SW_RTSO)
        elif cw & 0x0F == 0x0F and sw in (SW_RTSO, SW_OE) or sw & 0x6F == 0x27:
            sw = SW_OE | (sw & 0x1400 if sw & 0x6F == 0x27 else 0)
            mode = struct.unpack('b', self.store[(0x6061, 0)])[0]
            new_setpoint = cw & 0x10 and not prev & 0x10
            if mode == 6 and new_setpoint:
                self.put(0x6064, 0, 'i', self.get(0x607C, 0, 'i'))
                sw |= T.SW_ACK
            if mode == 1:
                if new_setpoint:
                    target = self.get(0x607A, 0, 'i')
                    if cw & T.CW_RELATIVE:
                        target += self.get(0x6064, 0, 'i')
                    self.put(0x6064, 0, 'i', target)
                    sw |= T.SW_ACK | T.SW_TARGET_REACHED
                elif not cw & 0x10:
                    sw &= ~T.SW_ACK
            self.put(0x6041, 0, 'H', sw)
        elif cw & 0x0F == 0x00:
            self.put(0x6041, 0, 'H', SW_SOD)


class FakeNmt:
    def __init__(self):
        self.state = 'PRE-OPERATIONAL'


class FakeNode:
    def __init__(self, sdo, node_id=4):
        self.id = node_id
        self.sdo = sdo
        self.nmt = FakeNmt()


class FakeNetwork:
    """Records frames; a SYNC makes the fake puck send TPDO1 and TPDO2."""

    def __init__(self, sdo, node_id=4):
        self.sdo = sdo
        self.id = node_id
        self.sent = []
        self.subs = {}

    def subscribe(self, cob, cb):
        self.subs.setdefault(cob, []).append(cb)

    def send_message(self, cob, data):
        self.sent.append((cob, bytes(data)))
        if cob == 0x80:
            s = self.sdo
            t1 = struct.pack('<Hbi', s.sw(), struct.unpack('b', s.store[(0x6061, 0)])[0],
                             s.get(0x6064, 0, 'i'))
            t2 = struct.pack('<ih', s.get(0x606C, 0, 'i'), 0)
            for cob_, data_ in ((0x180 + self.id, t1), (0x280 + self.id, t2)):
                for cb in self.subs.get(cob_, []):
                    cb(cob_, data_, 0)

    def emit(self, cob, data):
        for cb in self.subs.get(cob, []):
            cb(cob, data, 0)


def make(legacy=False, argv=()):
    sdo = FakeSdo(legacy)
    clock = Clock()
    clock.plant = sdo
    node = FakeNode(sdo)
    net = FakeNetwork(sdo)
    lines = []
    d = T.Drive(net, node, T.parse_args(['--id', '4'] + list(argv)), clock=clock.now,
                sleep=clock.sleep, out=lines.append)
    d.identify()
    return d, sdo, net, clock, lines


class TestHelpers(unittest.TestCase):
    def test_correlate_finds_the_lag_and_the_gain(self):
        import math
        target = [math.sin(0.1 * k) for k in range(200)]
        actual = [0.0] * 3 + [0.5 * x for x in target[:-3]]
        corr, lag, gain = T.correlate(target, actual, 10)
        self.assertGreater(corr, 0.99)
        self.assertEqual(lag, 3)
        self.assertAlmostEqual(gain, 0.5, places=1)

    def test_select_keeps_the_registry_order_and_rejects_unknown_names(self):
        self.assertEqual([t[0] for t in T.select('csp,pv')], ['pv', 'csp'])
        self.assertEqual(len(T.select('all')), len(T.TESTS))
        with self.assertRaises(SystemExit):
            T.select('pv,nonesuch')

    def test_summary_exit_code(self):
        ok = [T.Result('pv', 'PASS', '', 1.0), T.Result('csi', 'SKIP', '', 0.0)]
        self.assertEqual(T.summary(ok, out=lambda *_: None), 0)
        self.assertEqual(T.summary(ok + [T.Result('pt', 'FAIL', 'x', 1.0)],
                                   out=lambda *_: None), 1)

    def test_list(self):
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            self.assertEqual(T.main(['--list']), 0)
        self.assertIn('following', buf.getvalue())


class TestIdentity(unittest.TestCase):
    def test_5x(self):
        d, *_ = make()
        self.assertEqual(d.version, '5.0.5')
        self.assertEqual(d.githash, 'be59679')
        self.assertFalse(d.legacy)
        self.assertTrue(d.offers(T.cia402.MODE_CYCLIC_SYNC_IMP))
        self.assertTrue(d.offers(T.cia402.MODE_CYCLIC_SYNC_TRQ_ANGLE))

    def test_435_has_no_githash_and_numbers_pva_12(self):
        d, *_ = make(legacy=True)
        self.assertEqual(d.version, '4.3.5')
        self.assertIsNone(d.githash)
        self.assertTrue(d.legacy)
        self.assertTrue(d.offers(T.cia402.MODE_PHASE_VOLTAGE_ANGLE))     # as 12
        self.assertFalse(d.offers(T.cia402.MODE_CYCLIC_SYNC_IMP))
        self.assertFalse(d.offers(T.cia402.MODE_CYCLIC_SYNC_TRQ_ANGLE))


class TestStateMachine(unittest.TestCase):
    def test_pva_is_written_as_12_on_435_and_as_ff_on_5x(self):
        for legacy, byte in ((True, b'\x0c'), (False, b'\xff')):
            d, sdo, *_ = make(legacy)
            d.enable()
            d.set_mode(T.cia402.MODE_PHASE_VOLTAGE_ANGLE)
            self.assertEqual(sdo.store[(0x6061, 0)], byte)

    def test_a_refused_mode_is_a_skip_on_both_lines(self):
        for legacy in (True, False):
            d, sdo, *_ = make(legacy)
            sdo.put(0x6502, 0, 'I', 0)
            d.enable()
            with self.assertRaises(T.TestSkip):
                d.set_mode(T.cia402.MODE_CYCLIC_SYNC_POS)

    def test_clear_fault_waits_out_the_fault_reaction(self):
        d, sdo, _, clock, _ = make()
        d.enable()
        sdo.fault(reaction=0.2, now=clock.now())
        self.assertTrue(d.clear_fault())
        self.assertEqual(sdo.sw(), SW_OE)
        self.assertGreaterEqual(clock.now(), 0.2)

    def test_clear_fault_ignores_the_abort_of_controlword_0_on_both_lines(self):
        for legacy in (True, False):
            d, sdo, *_ = make(legacy)
            d.enable()
            sdo.fault()
            self.assertTrue(d.clear_fault(enable=False))
            self.assertEqual(sdo.sw(), SW_RTSO)

    def test_a_fault_that_stays_is_reported(self):
        d, sdo, *_ = make()
        d.enable()
        sdo.fault()
        sdo._controlword = lambda cw: None if cw & 0x80 else (_ for _ in ()).throw(
            SdoAbortedError(0x08000022))
        self.assertFalse(d.clear_fault())

    def test_the_guard_disables_the_drive(self):
        d, sdo, *_ = make(argv=['--guard-rpm', '100'])
        d.enable()
        with self.assertRaises(T.GuardTrip):
            d.check(SW_OE, T.cts_s(150))
        self.assertEqual(sdo.sw(), SW_RTSO)


class TestParameters(unittest.TestCase):
    def test_change_and_restore(self):
        d, sdo, *_ = make()
        d.change(0x6081, 0, 10000, 'I')
        d.change(0x6081, 0, 20000, 'I')
        d.change(0x2406, 0, 100, 'H')
        self.assertEqual(sdo.get(0x6081, 0, 'I'), 20000)
        d.restore()
        self.assertEqual(sdo.get(0x6081, 0, 'I'), 1)
        self.assertEqual(sdo.get(0x2406, 0, 'H'), 0)
        self.assertEqual(d.saved, {})

    def test_map_and_unmap_pdos(self):
        d, sdo, *_ = make()
        before = dict(sdo.store)
        d.map_pdos()
        self.assertEqual(sdo.get(0x1A00, 0, 'B'), 3)
        self.assertEqual(sdo.get(0x1A00, 3, 'I'), 0x60640020)
        self.assertEqual(sdo.get(0x1601, 2, 'I'), 0x607A0020)
        self.assertEqual(sdo.get(0x1800, 2, 'B'), 1)
        self.assertTrue(sdo.get(0x1400, 1, 'I') & T.COB_INVALID)       # RPDOs off
        self.assertFalse(sdo.get(0x1800, 1, 'I') & T.COB_INVALID)
        self.assertEqual(sdo.get(0x1401, 2, 'B'), 255)                 # and async
        d.rpdos(True)
        self.assertFalse(sdo.get(0x1400, 1, 'I') & T.COB_INVALID)
        for comm in (0x1400, 0x1401):       # applied at SYNC while they run
            self.assertEqual(sdo.get(comm, 2, 'B'), 1, hex(comm))
        self.assertEqual(sdo.get(0x1601, 0, 'B'), 2)
        d.rpdos(False)
        for comm in (0x1400, 0x1401):       # 4.3.5 replays a SYNC type when invalid
            self.assertTrue(sdo.get(comm, 1, 'I') & T.COB_INVALID, hex(comm))
            self.assertEqual(sdo.get(comm, 2, 'B'), 255, hex(comm))
        d.unmap_pdos()
        for key, raw in before.items():
            if key[0] in range(0x1400, 0x1C00):
                self.assertEqual(sdo.store[key], raw, hex(key[0]))
        self.assertEqual(d.node.nmt.state, 'OPERATIONAL')


class TestEmcy(unittest.TestCase):
    def test_one_line_per_emcy(self):
        d, _, net, _, lines = make()
        net.subscribe(0x84, d.on_emcy)
        net.emit(0x84, bytes.fromhex('8081110000000000'))
        self.assertEqual(len(lines), 1)
        self.assertIn('node 4: 0x8180', lines[0])
        self.assertIn('register 0x11 (generic, communication)', lines[0])
        self.assertEqual(d.emcy_since(0, T.SYNC_TIMEOUT_CODES)[0][1], 0x8180)

    def test_435_reads_0x8611_as_a_warning(self):
        d, _, net, _, lines = make(legacy=True)
        net.subscribe(0x84, d.on_emcy)
        net.emit(0x84, bytes.fromhex('1186810000000000'))
        self.assertIn('position tracking warning', lines[0])
        d5, _, net5, _, lines5 = make()
        net5.subscribe(0x84, d5.on_emcy)
        net5.emit(0x84, bytes.fromhex('1186810000000000'))
        self.assertIn('position tracking fault', lines5[0])


class TestCyclic(unittest.TestCase):
    def test_rpdo1_rpdo2_then_sync_each_period(self):
        d, sdo, net, clock, _ = make()
        d.map_pdos()
        d.enable()
        net.sent.clear()
        t0 = clock.now()
        rows = d.cyclic(lambda t, k: (T.CW_ENABLE, T.cia402.MODE_CYCLIC_SYNC_POS, 0, 0, k),
                        100, 0.1)
        self.assertEqual(len(rows), 11)
        cobs = [c for c, _ in net.sent]
        self.assertEqual(cobs[:3], [0x204, 0x304, 0x80])
        self.assertEqual(struct.unpack('<ii', net.sent[4][1]), (0, 1))
        self.assertAlmostEqual(clock.now() - t0, 0.11, places=2)

    def test_a_stale_fault_does_not_stop_the_loop(self):
        d, sdo, net, *_ = make()
        d.map_pdos()
        d.enable()
        d.tpdo1 = (SW_FAULT, 8, 0)          # left by a test before
        rows = d.cyclic(lambda t, k: (T.CW_ENABLE, 8, 0, 0, 0), 100, 0.05)
        self.assertEqual(len(rows), 6)

    def test_a_fault_in_the_loop_raises(self):
        d, sdo, *_ = make()
        d.map_pdos()
        d.enable()

        def script(t, k):
            if k == 3:
                sdo.fault()
            return T.CW_ENABLE, 8, 0, 0, 0
        with self.assertRaises(T.DriveFault):
            d.cyclic(script, 100, 1.0)


class TestRunner(unittest.TestCase):
    def test_pv_home_and_pp_on_the_fake(self):
        d, sdo, net, _, lines = make()
        code = T.run(d, T.select('pv,ppiad,ppird,home'), out=lines.append)
        self.assertEqual(code, 0, '\n'.join(lines))
        self.assertEqual(sdo.get(0x6081, 0, 'I'), 1)            # profile put back
        self.assertEqual(sdo.get(0x607C, 0, 'i'), 0)
        self.assertEqual(sdo.get(0x1A00, 0, 'B'), 1)            # mapping put back

    def test_an_unoffered_mode_skips_before_the_test_touches_anything(self):
        d, sdo, *_ = make(legacy=True)
        sdo.writes.clear()
        r = T.run_test(d, 'csi', T.t_csi, T.cia402.MODE_CYCLIC_SYNC_IMP)
        self.assertEqual(r.status, 'SKIP')
        self.assertNotIn(0x2383, [w[0] for w in sdo.writes])

    def test_a_failure_still_stops_and_restores(self):
        d, sdo, *_ = make()

        def boom(d_):
            d_.enable()
            d_.change(0x2406, 0, 100, 'H')
            raise T.TestFail('no')
        r = T.run_test(d, 'boom', boom)
        self.assertEqual(r.status, 'FAIL')
        self.assertEqual(sdo.get(0x2406, 0, 'H'), 0)
        self.assertEqual(sdo.sw(), SW_RTSO)

    def test_csi_gains_come_from_the_drive_or_its_loop(self):
        d, sdo, *_ = make()
        sdo.put(0x2381, 1, 'f', 0.02)
        sdo.put(0x2382, 1, 'H', 50)
        sdo.put(0x2383, 1, 'f', 0.0)
        sdo.put(0x2383, 2, 'f', 0.0)
        kp, kd = T._csi_gains(d)
        self.assertAlmostEqual(kp, 1.0, places=5)
        self.assertAlmostEqual(kd, 0.02, places=6)
        d.restore()
        sdo.put(0x2383, 1, 'f', 1.5)
        sdo.put(0x2383, 2, 'f', 0.03)
        self.assertEqual(tuple(round(x, 5) for x in T._csi_gains(d)), (1.5, 0.03))
        d.restore()
        d.args.csi_kp = 2.0
        self.assertEqual(round(T._csi_gains(d)[0], 5), 2.0)
        d.restore()
        sdo.put(0x2381, 1, 'f', 0.0)
        sdo.put(0x2383, 1, 'f', 0.0)
        sdo.put(0x2383, 2, 'f', 0.0)
        d.args.csi_kp = None
        with self.assertRaises(T.TestFail):
            T._csi_gains(d)

    def test_heartbeat_skips_on_435(self):
        d, *_ = make(legacy=True)
        r = T.run_test(d, 'heartbeat', T.t_heartbeat, T.cia402.MODE_PROFILE_VEL)
        self.assertEqual(r.status, 'SKIP')


if __name__ == '__main__':
    unittest.main()
