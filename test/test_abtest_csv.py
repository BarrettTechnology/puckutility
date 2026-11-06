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

from abtest import csvaudit, makecsv                           # noqa: E402

AB = os.path.join(ROOT, 'abtest')
TEMPLATES = {'4.3.5': os.path.join(AB, 'templates', 'P4-42-AB-4.3.5.csv'),
             '5.x': os.path.join(AB, 'templates', 'P4-42-AB-5.x.csv')}
EDS = {'4.3.5': os.path.join(AB, 'eds', 'puck4-4.3.5.eds'),
       '5.x': os.path.join(AB, 'eds', 'puck4-5.0.5.eds')}


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

    def test_pdo_mapping_and_count(self):
        r = self.edited('4.3.5', 'RPDO1 Entries,,0x1600,0,UNSIGNED8,0',
                        'RPDO1 Entry 1,,0x1600,1,UNSIGNED32,0x60400010\n'
                        'RPDO1 Entries,,0x1600,0,UNSIGNED8,1')
        self.assertTrue(any('PDO mapping entry' in e for e in r['errors']))
        self.assertTrue(any('(1600,0): 1 but must be 0' in e for e in r['errors']))

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


if __name__ == '__main__':
    unittest.main()
