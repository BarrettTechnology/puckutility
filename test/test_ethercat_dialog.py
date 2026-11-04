"""The EtherCAT connection dialog: interface list, Scan, selection.  The
NIC list and the slave scan are faked, so nothing touches a network
interface.  Needs a display (skipped without one)."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

try:
    import wx
except ImportError:                                     # pragma: no cover
    wx = None

NICS = [('enp3s0', 'enp3s0 (up)'), ('eth1', 'eth1 (down)')]
SLAVES = [dict(position=0, name='EK1100', vendor=2, product=0x044C2C52,
               revision=0, state='PREOP', bridge=False),
          dict(position=1, name='P4 Playground CAN Bridge', vendor=0x408,
               product=0xCA01, revision=1, state='PREOP', bridge=True)]


@unittest.skipIf(wx is None or not (os.environ.get('DISPLAY')
                                    or os.environ.get('WAYLAND_DISPLAY')),
                 'needs wxPython and a display')
class TestEtherCATDialog(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = wx.App.Get() or wx.App(False)

    def dialog(self, nics=NICS, **kw):
        from puckutility.gui import ethercat_dialog
        with mock.patch.object(ethercat_dialog.ethercat_bus, 'list_nics',
                               return_value=list(nics)):
            dlg = ethercat_dialog.EtherCATDialog(None, **kw)
        self.addCleanup(dlg.Destroy)
        return dlg, ethercat_dialog

    def test_title_and_preselected_interface(self):
        dlg, _mod = self.dialog(nic='eth1')
        self.assertEqual(dlg.GetTitle(), 'EtherCAT')
        self.assertEqual(dlg.choice_nic.GetItems(), ['enp3s0 (up)', 'eth1 (down)'])
        self.assertEqual(dlg.selected_nic(), 'eth1')

    def test_scan_lists_the_slaves(self):
        released = []
        dlg, mod = self.dialog(before_scan=lambda: released.append(True))
        with mock.patch.object(mod.ethercat_bus, 'scan_slaves',
                               return_value=SLAVES) as scan:
            dlg.on_scan(None)
        scan.assert_called_once_with('enp3s0')
        self.assertEqual(released, [True])
        self.assertEqual(dlg.list_slaves.GetItemCount(), 2)
        self.assertEqual(dlg.list_slaves.GetItemText(1, 1), 'P4 Playground CAN Bridge')
        self.assertEqual(dlg.list_slaves.GetItemText(1, 3), '0xCA01')
        self.assertIn('bridge at #1', dlg.text_status.GetLabel())

    def test_scan_failure_is_shown(self):
        dlg, mod = self.dialog()
        with mock.patch.object(mod.ethercat_bus, 'scan_slaves',
                               side_effect=OSError('permission denied')):
            dlg.on_scan(None)
        self.assertIn('permission denied', dlg.text_status.GetLabel())

    def test_no_interfaces_no_ok(self):
        dlg, _mod = self.dialog(nics=[])
        self.assertIsNone(dlg.selected_nic())
        self.assertFalse(dlg.FindWindowById(wx.ID_OK).IsEnabled())


if __name__ == '__main__':
    unittest.main()
