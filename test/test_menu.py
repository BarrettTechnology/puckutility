"""The Menu menu's calibration part, in the generated frame and in the .wxg
it is generated from: Actuator Calibration (Active Puck, All Pucks), then
exactly the numbered per-actuator steps (p4sysid.plans.numbered) with
current-sense bias and gain on their own handlers, and none of the batch
items.  Read from the sources, so no display is needed."""

import ast
import os
import re
import sys
import unittest
import xml.etree.ElementTree as ET

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, ROOT)

from p4sysid import plans  # noqa: E402

FRAME = os.path.join(ROOT, 'puckutility', 'gui', 'frame.py')
WXG = os.path.join(ROOT, 'configure-puckutilityapp.wxg')

#: The step items' handlers: bias and gain keep cal_board's.
HANDLERS = {'bias': 'calibrate_ibias', 'gain': 'calibrate_igainfactor'}


def expected_steps():
    return [('{}. {}'.format(n, st.title),
             HANDLERS.get(st.key, 'calibrate_step_' + st.key))
            for n, _stage, st in plans.numbered(plans.ACTUATOR)]


def frame_menu():
    """The Menu menu from frame.py: [(label, handler or submenu items)],
    submenus as nested lists, separators as ('---', None)."""
    with open(FRAME, encoding='utf-8') as f:
        src = f.read()
    body = src[src.index('wxglade_tmp_menu = wx.Menu()'):
               src.index('self.frame_menubar.Append(wxglade_tmp_menu, "Menu")')]
    menus = {'wxglade_tmp_menu': []}
    pending = None
    for line in body.splitlines():
        line = line.strip()
        m = re.match(r'(\w+) = wx\.Menu\(\)$', line)
        if m:
            menus[m.group(1)] = []
            continue
        m = re.match(r'(?:item|[\w.]+) = (\w+)\.Append\(wx\.ID_ANY, (".*?"), '
                     r'""(?:, wx\.ITEM_RADIO)?\)$', line)
        if m:
            pending = [ast.literal_eval(m.group(2)), None]
            menus[m.group(1)].append(pending)
            continue
        m = re.match(r'self\.Bind\(wx\.EVT_MENU, self\.(\w+), ', line)
        if m:
            pending[1] = m.group(1)
            continue
        m = re.match(r'(\w+)\.Append\(wx\.ID_ANY, (".*?"), (\w+), ""\)$', line)
        if m:
            menus[m.group(1)].append([ast.literal_eval(m.group(2)),
                                      menus[m.group(3)]])
            continue
        m = re.match(r'(\w+)\.AppendSeparator\(\)$', line)
        if m:
            menus[m.group(1)].append(['---', None])
    return [tuple(x) for x in menus['wxglade_tmp_menu']]


def wxg_menu():
    """The same from the .wxg."""
    root = ET.parse(WXG).getroot()

    def walk(menu):
        out = []
        for child in menu:
            if child.tag == 'item':
                label = child.findtext('label')
                out.append((label, None if label == '---'
                            else child.findtext('handler')))
            elif child.tag == 'menu':
                out.append((child.get('label'), walk(child)))
        return out
    top = next(m for m in root.iter('menu') if m.get('label') == 'Menu')
    return walk(top)


def sub(menu, label):
    return dict((k, v) for k, v in menu if isinstance(v, list))[label]


class TestCalibrationMenu(unittest.TestCase):

    def check(self, menu):
        labels = [k for k, _v in menu]
        cal = sub(menu, 'Actuator Calibration')
        self.assertEqual([tuple(x) for x in cal], [
            ('Active Puck\tCtrl+C', 'calibrate_actuator'),
            ('All Pucks', 'calibrate_actuator_all_pucks')])
        steps = sub(menu, 'Actuator Calibration Steps')
        self.assertEqual([tuple(x) for x in steps], expected_steps())
        self.assertEqual(labels.index('Actuator Calibration Steps'),
                         labels.index('Actuator Calibration') + 1)
        # The batch items, the old sequences and the replaced single items
        # are gone; the non-calibration items stay.
        for gone in ('Calibrate', 'Current Sense Timing', 'Encoder Lag',
                     'Test Magnetic Encoder', 'Current Sense Slope',
                     'Encoder Zero', 'Encoder Direction',
                     'iSense Balance Ripple'):
            self.assertNotIn(gone, labels)
        self.assertEqual(labels.count('Encoder Error Compensation'), 1)
        self.assertIsInstance(sub(menu, 'Encoder Error Compensation'), list)
        for kept in ('Set User Direction...', 'Tune Gains...',
                     'Save to CSV...', 'Upload System Config', 'Help', 'Exit'):
            self.assertIn(kept, labels)

    def test_frame(self):
        self.check(frame_menu())

    def test_wxg_matches_the_frame(self):
        def norm(menu):
            return [(k, norm(v) if isinstance(v, list) else v)
                    for k, v in menu]
        self.check(wxg_menu())
        self.assertEqual(norm(wxg_menu()), norm(frame_menu()))

    def test_every_handler_exists_on_the_app(self):
        try:
            import wx  # noqa: F401
        except ImportError as exc:
            self.skipTest(str(exc))
        from puckutility.gui import app
        from puckutility.gui.frame import puckutilityapp_frame
        names = [h for _k, h in expected_steps()]
        names += ['calibrate_actuator', 'calibrate_actuator_all_pucks']
        for name in names:
            # The app's handler, not the generated stub.
            self.assertIsNot(getattr(app.MyFrame, name),
                             getattr(puckutilityapp_frame, name), name)
        from puckutility.gui.cal_board import BoardCal
        self.assertIs(app.MyFrame.calibrate_ibias, BoardCal.calibrate_ibias)
        self.assertIs(app.MyFrame.calibrate_igainfactor,
                      BoardCal.calibrate_igainfactor)


if __name__ == '__main__':
    unittest.main()
