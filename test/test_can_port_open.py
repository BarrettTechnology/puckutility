"""Nothing opens a CAN port until Scan Pucks.

Opening an adapter's port with nothing plugged into it can hang the machine,
so the GUI must not open one at startup (desktop or touchscreen), must not
reset the pucks at startup, and picking a port in the dropdown only chooses
the port the next Scan opens.  The behaviour tests run the real methods on a
stub frame, so no display is needed.
"""

import os
import sys
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, ROOT)


def _source(*parts):
    with open(os.path.join(ROOT, *parts), encoding='utf-8') as fh:
        return fh.read()


class TestSource(unittest.TestCase):

    def test_no_startup_connect(self):
        app = _source('puckutility', 'gui', 'app.py')
        self.assertNotIn('_startup_connect', app)
        self.assertNotIn('[0x81, 0]', app, 'no NMT reset-all broadcast')

    def test_dropdown_does_not_open_the_port(self):
        frame = _source('puckutility', 'gui', 'frame.py')
        self.assertIn('self.choice_port.Bind(wx.EVT_CHOICE, self.on_port_choice)',
                      frame)
        self.assertNotIn('self.can_port)', frame)
        wxg = _source('configure-puckutilityapp.wxg')
        self.assertIn('<handler event="EVT_CHOICE">on_port_choice</handler>', wxg)


try:
    import wx  # noqa: F401
    from puckutility.gui import app
except ImportError:          # the command-line-only install
    app = None


class _Choice:
    def __init__(self, port):
        self.port = port

    def GetStringSelection(self):
        return self.port

    def SetItems(self, items):
        pass

    def SetSelection(self, i):
        pass


def _frame(port='can0', open_port=None, network=None):
    f = mock.MagicMock()
    f.choice_port = _Choice(port)
    f.choice_id = _Choice('')
    f._open_port = open_port
    f.network = network
    f.node = None
    f.lastMode = 0
    f.ADC_ON = False
    f._reconnect_timer = None
    return f


@unittest.skipIf(app is None, 'needs wxPython')
class TestBehaviour(unittest.TestCase):

    def test_picking_a_port_opens_nothing(self):
        f = _frame('can1', open_port='can0')
        app.MyFrame.on_port_choice(f, None)
        f.can_port.assert_not_called()
        f._replace_network.assert_not_called()
        f.frame_statusbar.SetStatusText.assert_called_with(
            'Press Scan Pucks to connect to can1', 1)

    def test_can_port_refuses_without_a_port(self):
        f = _frame('')
        self.assertFalse(app.MyFrame.can_port(f, None))
        f._replace_network.assert_not_called()

    def test_scan_opens_the_dropdown_port_first(self):
        f = _frame('can2')
        f.can_port.return_value = False      # e.g. no adapter there
        app.MyFrame.scan_pucks(f, None)
        f.can_port.assert_called_once_with(None, skipADC=True)

    def test_scan_reopens_when_the_dropdown_moved(self):
        f = _frame('can1', open_port='can0', network=mock.MagicMock())
        f.can_port.return_value = False
        app.MyFrame.scan_pucks(f, None)
        f.can_port.assert_called_once_with(None, skipADC=True)

    def test_scan_keeps_the_open_port(self):
        net = mock.MagicMock()
        net.scanner.nodes = []
        f = _frame('can0', open_port='can0', network=net)
        f.can_port.return_value = False
        app.MyFrame.scan_pucks(f, None)
        net.scanner.search.assert_called_once()
        # Only the no-pucks fallback may reopen it, never before the scan.
        self.assertLessEqual(f.can_port.call_count, 1)
        for c in f.can_port.call_args_list:
            self.assertNotEqual(c.kwargs.get('skipADC'), True)

    def test_reconnect_needs_a_scanned_port(self):
        f = _frame('can0', open_port=None)
        f.node = object()
        f.Rescanning = False
        app.MyFrame._on_can_device_lost(f)
        self.assertIsNone(f._reconnect_timer)



@unittest.skipIf(app is None, 'needs wxPython')
class TestCanSetupCheck(unittest.TestCase):
    """After the port opens: the link's timing goes to the log, and another
    app's /etc CAN files (which override the installed timing) are warned
    about once a session."""

    def _check(self, f, shadow):
        with mock.patch.object(app.can_backend, 'iface_timing_text',
                               return_value='can0: 1000000 bit/s, sample point 0.750, SJW 20'), \
                mock.patch.object(app.can_backend, 'shadowed_can_files_message',
                                  return_value=shadow), \
                mock.patch.object(app.wx, 'MessageDialog') as dlg, \
                mock.patch('builtins.print') as out:
            app.MyFrame._check_can_setup(f)
        return dlg, ' '.join(str(c.args[0]) for c in out.call_args_list)

    def test_the_timing_is_logged(self):
        f = _frame(open_port='can0')
        f._can_setup_warned = False
        dlg, log = self._check(f, '')
        self.assertIn('sample point 0.750, SJW 20', log)
        dlg.assert_not_called()

    def test_shadowing_files_warn_once(self):
        f = _frame(open_port='can0')
        f._can_setup_warned = False
        dlg, log = self._check(f, 'Another program\'s CAN set-up overrides this one')
        dlg.assert_called_once()
        self.assertIn('WARNING', log)
        dlg, _ = self._check(f, 'Another program\'s CAN set-up overrides this one')
        dlg.assert_not_called()


@unittest.skipIf(app is None, 'needs wxPython')
class TestFaultText(unittest.TestCase):

    def _desc(self, code):
        e = mock.Mock(code=code)
        e.get_desc.return_value = 'generic'
        return app.puck_fault_desc(e)

    def test_bus_off_is_a_communication_fault_not_the_supply(self):
        text = self._desc(0x8140)
        self.assertIn('BUS-OFF', text)
        self.assertIn('NOT the supply', text)
        self.assertIn('sample point', text)

    def test_codes_the_app_table_lacks_come_from_p4core(self):
        from p4core import cia402
        self.assertEqual(self._desc(0x2392), cia402.ERROR_CODES[0x2392])
        self.assertEqual(self._desc(0x1234), 'generic')


if __name__ == '__main__':
    unittest.main()
