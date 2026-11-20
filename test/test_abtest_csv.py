"""abtest/csvaudit.py and abtest/makecsv.py: the A/B templates audit clean,
the audit catches what the A/B CSVs must not have, makecsv keeps only the
template's rows."""

import io
import os
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, ROOT)

from abtest import csvaudit, csvdiff, makecsv                           # noqa: E402
from p4core import config_csv                                           # noqa: E402

AB = os.path.join(ROOT, 'abtest')
TEMPLATES = {'4.3.5': os.path.join(AB, 'templates', 'P4-42-AB-4.3.5.csv'),
             '5.x': os.path.join(AB, 'templates', 'P4-42-AB-5.x.csv')}
EDS = {'4.3.5': os.path.join(AB, 'eds', 'puck4-4.3.5.eds'),
       '5.x': os.path.join(AB, 'eds', 'puck4-5.0.5.eds')}


def _set_row(text, index, sub, value):
    """*text* (a CSV) with the data of row index,sub set to *value*."""
    tag = ',0x{:04X},{},'.format(index, sub)
    lines = text.split('\n')
    hits = [i for i, l in enumerate(lines) if tag in l]
    assert len(hits) == 1, tag
    cells = lines[hits[0]].split(',')
    cells[5] = str(value)
    lines[hits[0]] = ','.join(cells)
    return '\n'.join(lines)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def path(self, name):
        return os.path.join(self.tmp.name, name)

    def edited(self, line, old, new, append=''):
        with open(TEMPLATES[line]) as f:
            text = f.read()
        if old:
            self.assertIn(old, text)
            text = text.replace(old, new)
        out = self.path('edited.csv')
        with open(out, 'w') as f:
            f.write(text + append)
        return csvaudit.audit(out, EDS[line])


class TemplateTest(Base):
    def test_templates_audit_clean(self):
        for line in TEMPLATES:
            with self.subTest(line=line):
                r = csvaudit.audit(TEMPLATES[line], EDS[line])
                self.assertEqual((r['missing'], r['errors'], r['warnings']), ([], [], []))
                self.assertEqual(r['line'], 'legacy' if line == '4.3.5' else 'current')

    def test_shipped_csvs_audit_clean(self):
        shipped = sorted(os.listdir(os.path.join(AB, 'csv')))
        self.assertGreaterEqual(len(shipped), 4)
        for name in shipped:
            line = '4.3.5' if 'fw4.3.5' in name else '5.x'
            with self.subTest(csv=name):
                r = csvaudit.audit(os.path.join(AB, 'csv', name), EDS[line])
                self.assertEqual((r['missing'], r['errors']), ([], []))
                # A geared 5.x CSV that leaves settle ki at 0 (the 2026-11-06
                # ones): a warning only
                settle = [row.data for row in config_csv.read(os.path.join(AB, 'csv', name))
                          if row.kind == 'entry' and config_csv._key(row) == (0x2381, 4)]
                zero = line == '5.x' and bool(settle) and float(settle[0]) == 0
                self.assertEqual([w.split(': ', 1)[1].split(' on ')[0] for w in r['warnings']],
                                 ['settle ki is 0'] if zero else [])

    def test_main_exit_status(self):
        out = io.StringIO()
        sys_stdout, sys.stdout = sys.stdout, out
        try:
            self.assertEqual(csvaudit.main([TEMPLATES['5.x'], EDS['5.x']]), 0)
            self.assertEqual(csvaudit.main([TEMPLATES['5.x'], EDS['4.3.5']]), 1)
        finally:
            sys.stdout = sys_stdout


class AuditTest(Base):
    def test_stray_bias_row(self):
        r = self.edited('5.x', None, None, 'Bias,,0x3008,3,INTEGER16,2000\n')
        self.assertTrue(any('per-actuator calibration' in e for e in r['errors']))

    def test_identity_rows(self):
        ident = ('Product Code,,0x1018,2,UNSIGNED32,{}\n'
                 'Revision Number,,0x1018,3,UNSIGNED32,0x1\n')
        with open(TEMPLATES['5.x']) as f:
            text = f.read()
        cases = (('P4-42-x.csv', 5755, []), ('P4-32-x.csv', 5760, []),
                 ('P4-37-x.csv', 1950, []),
                 ('P4-32-x.csv', 5755, ['product code 5755 but a P4-32 is 5760']),
                 ('edited.csv', 5755, ['file name names no model']))
        for name, code, want in cases:
            out = self.path(name)
            with open(out, 'w') as f:
                f.write(text + ident.format(code))
            with self.subTest(name=name, code=code):
                errors = csvaudit.audit(out, EDS['5.x'])['errors']
                self.assertEqual(len(errors), len(want), errors)
                for w, e in zip(want, errors):
                    self.assertIn(w, e)
        # the serial and the end-user codes stay the board's
        for sub in (4, 5, 6):
            r = self.edited('5.x', None, None,
                            'Id,,0x1018,{},UNSIGNED32,1\n'.format(sub))
            with self.subTest(sub=sub):
                self.assertTrue(any('(1018,{}): identity must not'.format(sub) in e
                                    for e in r['errors']), r['errors'])
        # leaving them out is fine
        r = csvaudit.audit(TEMPLATES['5.x'], EDS['5.x'])
        self.assertIn((0x1018, 2), [k for k, *_ in r['excluded']])

    def test_pdo_mapping_and_count(self):
        r = self.edited('4.3.5', 'RPDO1 Entries,,0x1600,0,UNSIGNED8,0',
                        'RPDO1 Entry 1,,0x1600,1,UNSIGNED32,0x60400010\n'
                        'RPDO1 Entries,,0x1600,0,UNSIGNED8,1')
        self.assertTrue(any('PDO mapping entry' in e for e in r['errors']))
        self.assertTrue(any('(1600,0): 1 but must be 0' in e for e in r['errors']))

    STANDARD = ('RPDO1 Entry 1,,0x1600,1,UNSIGNED32,0x60400010\n'
                'RPDO1 Entry 2,,0x1600,2,UNSIGNED32,0x60600008\n'
                'RPDO1 Entry 3,,0x1600,3,UNSIGNED32,0x60710010\n'
                'RPDO1 Entries,,0x1600,0,UNSIGNED8,3\n'
                'RPDO2 Entry 1,,0x1601,1,UNSIGNED32,0x60ff0020\n'
                'RPDO2 Entry 2,,0x1601,2,UNSIGNED32,0x607a0020\n'
                'RPDO2 Entries,,0x1601,0,UNSIGNED8,2\n'
                'TPDO1 Entry 1,,0x1A00,1,UNSIGNED32,0x60410010\n'
                'TPDO1 Entry 2,,0x1A00,2,UNSIGNED32,0x60610008\n'
                'TPDO1 Entry 3,,0x1A00,3,UNSIGNED32,0x60640020\n'
                'TPDO1 Entries,,0x1A00,0,UNSIGNED8,3\n'
                'TPDO2 Entry 1,,0x1A01,1,UNSIGNED32,0x606c0020\n'
                'TPDO2 Entry 2,,0x1A01,2,UNSIGNED32,0x60780010\n'
                'TPDO2 Entries,,0x1A01,0,UNSIGNED8,2\n')

    def _standard(self, mapping):
        with open(TEMPLATES['5.x']) as f:
            text = f.read()
        out = self.path('standard.csv')
        with open(out, 'w') as f:
            f.write(text.rstrip('\n') + '\n' + mapping)
        return csvaudit.audit(out, EDS['5.x'], pdos='standard')

    def test_standard_pdos(self):
        r = self._standard(self.STANDARD)
        self.assertEqual(r['errors'], [])
        self.assertEqual(r['warnings'], [])      # count 0 first, then the count: no warning

    def test_standard_pdos_wrong_entry(self):
        r = self._standard(self.STANDARD.replace('0x606c0020', '0x60690020'))
        self.assertTrue(any('(1A01,1): 0x60690020 but the standard mapping is 0x606c0020' in e
                            for e in r['errors']), r['errors'])

    def test_standard_pdos_left_empty(self):
        r = csvaudit.audit(TEMPLATES['5.x'], EDS['5.x'], pdos='standard')
        self.assertTrue(any('(1600,0): 0 but must be 3' in e for e in r['errors']), r['errors'])
        self.assertTrue(any('1A01,2 must map 6078,0' in e for e in r['errors']), r['errors'])

    def test_standard_pdos_3_and_4_stay_empty(self):
        r = self._standard(self.STANDARD + 'RPDO3 Entry 1,,0x1602,1,UNSIGNED32,0x60400010\n')
        self.assertTrue(any('(1602,1): PDO mapping entry must not be in the CSV' in e
                            for e in r['errors']), r['errors'])

    def test_required_value(self):
        r = self.edited('5.x', 'Heartbeat period in ms,,0x1017,0,UNSIGNED16,0',
                        'Heartbeat period in ms,,0x1017,0,UNSIGNED16,1000')
        self.assertTrue(any('(1017,0): 1000 but must be 0' in e for e in r['errors']))

    def test_real32_as_hex(self):
        r = self.edited('5.x', 'Kp,,0x2381,1,REAL32,0', 'Kp,,0x2381,1,REAL32,0x3d5dd6a0')
        self.assertTrue(any('not in float form' in e for e in r['errors']))

    def test_type_from_the_eds(self):
        # 4.3.5 has the gains as UNSIGNED32 (float bits): REAL32 is wrong there
        with open(TEMPLATES['4.3.5']) as f:
            row = next(l for l in f if ',0x2381,1,' in l)
        r = self.edited('4.3.5', row, row.replace('UNSIGNED32', 'REAL32').rsplit(',', 1)[0]
                        + ',0.05\n')
        self.assertTrue(any('type REAL32 but the EDS says UNSIGNED32' in e for e in r['errors']))

    def test_csi_gains_left_at_zero_in_a_tuned_csv(self):
        r = self.edited('5.x', 'Kp,,0x2381,1,REAL32,0', 'Kp,,0x2381,1,REAL32,0.0125')
        self.assertEqual(sum('CSI kp is 0' in e for e in r['errors']), 1)
        self.assertEqual(sum('CSI kd is 0' in e for e in r['errors']), 1)
        with open(TEMPLATES['5.x']) as f:
            text = f.read().replace('Kp,,0x2381,1,REAL32,0', 'Kp,,0x2381,1,REAL32,0.0125')
        text = text.replace(',0x2383,1,REAL32,0', ',0x2383,1,REAL32,1.25')
        text = text.replace(',0x2383,2,REAL32,0', ',0x2383,2,REAL32,0.0125')
        out = self.path('csi.csv')
        with open(out, 'w') as f:
            f.write(text)
        self.assertEqual(csvaudit.audit(out, EDS['5.x'])['errors'], [])

    def test_settle_ki_0_on_a_gearhead_warns(self):
        with open(TEMPLATES['5.x']) as f:
            text = f.read()
        for ratio, ki, warned in (((264, 10), '0', True), ((264, 10), '0.0004', False),
                                  ((1, 1), '0', False)):
            t = text
            for sub, v in zip((1, 2), ratio):
                t = _set_row(t, 0x6091, sub, v)
            t = _set_row(t, 0x2381, 4, ki)
            out = self.path('ki.csv')
            with open(out, 'w') as f:
                f.write(t)
            r = csvaudit.audit(out, EDS['5.x'])
            with self.subTest(ratio=ratio, ki=ki):
                self.assertEqual(r['errors'], [])
                self.assertEqual(any('settle ki is 0 on a 26.4:1' in w
                                     for w in r['warnings']), warned)

    def test_missing_row(self):
        r = self.edited('5.x', 'Motor poles,,0x3011,3,UNSIGNED8,0\n', '')
        self.assertEqual([k for k, _ in r['missing']], [(0x3011, 3)])

    def test_unused_rows_need_not_be_there(self):
        r = csvaudit.audit(TEMPLATES['4.3.5'], EDS['4.3.5'])
        why = {k: w for k, _, w in r['excluded']}
        self.assertEqual(why[(0x607E, 0)], 'not used by this firmware')
        self.assertEqual(why[(0x3009, 3)], 'per-actuator calibration')


class MakeTest(Base):
    def test_values_from_a_csv_and_set(self):
        src = self.path('old.csv')
        with open(src, 'w') as f:
            f.write('Poles,,0x3011,3,UNSIGNED8,14\n'
                    'Kp,,0x2381,1,UNSIGNED32,0x3bb24a6b\n'
                    'Bias,,0x3008,3,INTEGER16,2000\n'
                    'RPDO1 Entry 1,,0x1600,1,UNSIGNED32,0x60400010\n'
                    'RPDO1 Entries,,0x1600,0,UNSIGNED8,1\n'
                    'Filter,,0x3013,6,UNSIGNED8,160\n')
        out = self.path('out.csv')
        log = io.StringIO()
        kept = makecsv.make(TEMPLATES['4.3.5'], out, [('csv', src)],
                            [makecsv.parse_set('0x3013,6=0x90')], version='0.1.0',
                            date='2026-11-05', out=log)
        with open(out) as f:
            text = f.read()
        self.assertIn(',0x3011,3,UNSIGNED8,14\n', text)
        self.assertIn(',0x2381,1,UNSIGNED32,0x3bb24a6b\n', text)
        self.assertIn(',0x3013,6,UNSIGNED8,144\n', text)
        self.assertIn('V0.1.0 (2026-11-05)', text)
        self.assertIn(',0x1600,0,UNSIGNED8,0\n', text)     # the template's 0 wins
        self.assertNotIn('0x3008,3', text)
        self.assertNotIn('0x1600,1', text)
        self.assertIn((0x3011, 5), kept)
        r = csvaudit.audit(out, EDS['4.3.5'])
        self.assertEqual((r['missing'], r['errors']), ([], []))

    def test_set_unknown_row(self):
        with self.assertRaises(ValueError):
            makecsv.make(TEMPLATES['5.x'], self.path('o.csv'),
                         sets=[makecsv.parse_set('0x3008,3=1')], out=io.StringIO())


class DiffTest(Base):
    def write(self, name, text):
        path = self.path(name)
        with open(path, 'w') as f:
            f.write(text)
        return path

    def test_equal_values_in_any_spelling(self):
        a = self.write('a.csv', 'Version,FILE_VERSION,,,STRING,V1.0.0 (2026-11-06)\n'
                       'Filter,,0x3013,6,UNSIGNED8,0x90\nKp,,0x2381,1,REAL32,0.5\n')
        b = self.write('b.csv', 'Version,FILE_VERSION,,,STRING,V9.9.9 (2026-11-07)\n'
                       'Filter,,0x3013,6,UNSIGNED8,144\nKp,,0x2381,1,REAL32,5e-1\n')
        self.assertEqual(csvdiff.diff(a, b), ([], []))
        self.assertEqual(csvdiff.main([a, b], out=io.StringIO()), 0)

    def test_a_clamped_row_and_a_missing_row(self):
        a = self.write('a.csv', 'Poles,,0x3011,3,UNSIGNED8,14\nPWM,,0x3001,1,UNSIGNED32,200000\n')
        b = self.write('b.csv', 'PWM,,0x3001,1,UNSIGNED32,160000\n')
        differ, missing = csvdiff.diff(a, b)
        self.assertEqual(differ, [((0x3001, 1), 'PWM', '200000', '160000')])
        self.assertEqual(missing, [((0x3011, 3), 'Poles')])
        self.assertEqual(csvdiff.main([a, b], out=io.StringIO()), 1)

    def test_the_puck_read_by_sdo(self):
        from canopen.sdo.exceptions import SdoAbortedError

        class Node:
            id = 13

            class sdo:
                store = {(0x3011, 3): b'\x0e', (0x3001, 1): (160000).to_bytes(4, 'little'),
                         (0x1017, 0): b'\x00\x00', (0x2381, 1): b'\x00\x00\x00\x3f'}

                @classmethod
                def upload(cls, index, sub):
                    if (index, sub) not in cls.store:
                        raise SdoAbortedError(0x06020000)
                    return cls.store[(index, sub)]
        a = self.write('a.csv', 'Poles,,0x3011,3,UNSIGNED8,14\nPWM,,0x3001,1,UNSIGNED32,200000\n'
                       'Heartbeat,,0x1017,0,UNSIGNED16,0\nKp,,0x2381,1,REAL32,0.5\n'
                       'Gone,,0x2406,0,UNSIGNED16,0\n')
        differ, missing = csvdiff.diff_puck(a, Node)
        self.assertEqual(differ, [((0x3001, 1), 'PWM', '200000', '160000')])
        self.assertEqual([k for k, _ in missing], [(0x2406, 0)])


if __name__ == '__main__':
    unittest.main()
