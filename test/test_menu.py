"""The Menu menu's calibration part, in the generated frame and in the .wxg
it is generated from: Actuator Calibration (Active Puck, All Pucks), then
exactly the numbered per-actuator steps (p4core.actuator, in run order),
each on its calibrate_step_<key> handler, and none of the batch items or
the gain search (pucktuner's).  Read from the sources, so no display is
needed."""

import ast
import os
import re
import sys
import unittest
import xml.etree.ElementTree as ET

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, ROOT)

from p4core.actuator import plans  # noqa: E402

FRAME = os.path.join(ROOT, 'puckutility', 'gui', 'frame.py')
WXG = os.path.join(ROOT, 'configure-puckutilityapp.wxg')

def expected_steps():
    return [('{}. {}'.format(n, plans.title(key)), 'calibrate_step_' + key)
            for n, key in enumerate(plans.run_order(plans.ACTUATOR), 1)]


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
                     'iSense Balance Ripple',
                     '9. Velocity gains + filters (ripple)',
                     '10. Position kp + velff (settle)'):
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



class TestFirmwareGates(unittest.TestCase):
    """select_id's _apply_firmware_gates over fake menus built from
    frame.py's Menu menu: a 4.3.x puck's menus leave out the steps it cannot
    run and the runout switch (not greyed out: gone), numbering the rest
    from 1; a 4.5.0 puck gets them all back, in their places."""

    def setUp(self):
        try:
            import wx  # noqa: F401
        except ImportError as exc:
            self.skipTest(str(exc))

    class Item:
        ids = iter(range(1000, 2000))

        def __init__(self, label, sub=None):
            self.label, self.sub, self.id = label, sub, next(self.ids)

        def GetId(self):
            return self.id

        def GetItemLabelText(self):
            return self.label.split('\t')[0]

        def SetItemLabel(self, label):
            self.label = label

        def GetSubMenu(self):
            return self.sub

    class Menu:
        def __init__(self, items):
            self.items = list(items)

        def GetMenuItems(self):
            return list(self.items)

        def FindItemById(self, i):
            return next((it for it in self.items if it.GetId() == i), None)

        def Remove(self, item):
            self.items.remove(item)
            return item

        def Insert(self, pos, item):
            self.items.insert(pos, item)

    def frame(self):
        from unittest import mock
        from puckutility.gui import app
        items = []
        for label, v in frame_menu():
            sub = (self.Menu(self.Item(k) for k, _h in v)
                   if isinstance(v, list) else None)
            items.append(self.Item(label, sub))
        top = self.Menu(items)

        class Bar:
            def FindMenu(self, title):
                return 0 if title == 'Menu' else -1

            def GetMenu(self, i):
                return top

            def FindMenuItem(self, menu, label):
                return next(it.GetId() for it in items
                            if it.GetItemLabelText() == label)

            def FindItemById(self, i):
                return top.FindItemById(i)

        frame = mock.Mock(frame_menubar=Bar(), node=mock.Mock(),
                          _FIRMWARE_MENU_FEATURES=app.MyFrame._FIRMWARE_MENU_FEATURES)
        frame._menu_layout = lambda: app.MyFrame._menu_layout(frame)
        frame.getID.return_value = 5
        return frame, top

    def apply(self, frame, fw):
        from puckutility.gui import app
        frame.node._p4_firmware = fw
        app.MyFrame._apply_firmware_gates(frame, fw)

    def labels(self, menu):
        return [it.GetItemLabelText() for it in menu.GetMenuItems()]

    def steps(self, top):
        return self.labels(next(it.GetSubMenu() for it in top.GetMenuItems()
                                if it.label == 'Actuator Calibration Steps'))

    def test_legacy_then_current(self):
        from p4core import firmware
        frame, top = self.frame()
        before = self.labels(top)
        self.apply(frame, firmware.Firmware((4, 3, 5), '4.3.5',
                                            frozenset({'legacy_polarity'})))
        self.assertNotIn('Encoder Error Compensation', self.labels(top))
        self.assertIn('Actuator Calibration', self.labels(top))
        self.assertEqual(self.steps(top), [
            '1. Encoder filter + rest test', '2. Current-sense bias',
            '3. Current-sense gain', '4. Electrical zero + winding order'])
        self.apply(frame, firmware.Firmware(
            (4, 5, 0), '4.5.0',
            frozenset(firmware.FEATURES) - {'legacy_polarity'}))
        self.assertEqual(self.labels(top), before)
        self.assertEqual(self.steps(top), [k for k, _h in expected_steps()])


if __name__ == '__main__':
    unittest.main()
