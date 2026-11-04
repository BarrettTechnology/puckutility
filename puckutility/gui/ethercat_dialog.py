"""The EtherCAT connection dialog: pick the network interface the
EtherCAT-CAN bridge is wired to, and scan it for EtherCAT slaves.

Opened from the CAN Port choice ("EtherCAT..."); on OK the app connects to
the port ``EtherCAT:<nic>`` (see p4core.ethercat_bus).
"""

import wx

from p4core import ethercat_bus


class EtherCATDialog(wx.Dialog):
    """*nic* preselects an interface; *before_scan* is called before a scan
    opens the interface (the app releases its own EtherCAT link there)."""

    def __init__(self, parent, nic=None, before_scan=None):
        super().__init__(parent, wx.ID_ANY, 'EtherCAT',
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self._before_scan = before_scan
        self._nics = ethercat_bus.list_nics()

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(wx.StaticText(
            self, wx.ID_ANY,
            'Reach the pucks through the EtherCAT-CAN bridge.\n'
            'Choose the network interface wired to the bridge.'),
            0, wx.ALL, 8)

        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(self, wx.ID_ANY, 'Network interface:'),
                0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.choice_nic = wx.Choice(self, wx.ID_ANY,
                                    choices=[label for _name, label in self._nics])
        row.Add(self.choice_nic, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.button_scan = wx.Button(self, wx.ID_ANY, 'Scan')
        self.button_scan.SetToolTip('List the EtherCAT slaves on this interface')
        row.Add(self.button_scan, 0, wx.ALIGN_CENTER_VERTICAL)
        sizer.Add(row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        self.list_slaves = wx.ListCtrl(self, wx.ID_ANY, size=(460, 120),
                                       style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        for col, (title, width) in enumerate((('#', 30), ('Name', 190),
                                              ('Vendor', 80), ('Product', 80),
                                              ('State', 70))):
            self.list_slaves.InsertColumn(col, title, width=width)
        sizer.Add(self.list_slaves, 1, wx.EXPAND | wx.ALL, 8)

        self.text_status = wx.StaticText(self, wx.ID_ANY, '')
        sizer.Add(self.text_status, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        buttons = self.CreateSeparatedButtonSizer(wx.OK | wx.CANCEL)
        sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, 8)
        self.SetSizerAndFit(sizer)

        names = [name for name, _label in self._nics]
        if nic in names:
            self.choice_nic.SetSelection(names.index(nic))
        elif names:
            self.choice_nic.SetSelection(0)
        if not names:
            self.text_status.SetLabel('No network interfaces found.')
        self.FindWindowById(wx.ID_OK).Enable(bool(names))
        self.button_scan.Enable(bool(names))
        self.button_scan.Bind(wx.EVT_BUTTON, self.on_scan)

    def selected_nic(self):
        i = self.choice_nic.GetSelection()
        if i == wx.NOT_FOUND or i >= len(self._nics):
            return None
        return self._nics[i][0]

    def on_scan(self, event):
        nic = self.selected_nic()
        if not nic:
            return
        if self._before_scan is not None:
            self._before_scan()
        self.list_slaves.DeleteAllItems()
        self.text_status.SetLabel('Scanning {}...'.format(nic))
        self.text_status.Update()
        try:
            with wx.BusyCursor():
                slaves = ethercat_bus.scan_slaves(nic)
        except Exception as exc:
            self.text_status.SetLabel('Scan failed: {}'.format(exc))
            self.text_status.Wrap(self.list_slaves.GetSize().width)
            self.Fit()
            return
        for s in slaves:
            row = self.list_slaves.InsertItem(self.list_slaves.GetItemCount(),
                                              str(s['position']))
            self.list_slaves.SetItem(row, 1, s['name'])
            self.list_slaves.SetItem(row, 2, '0x{:X}'.format(s['vendor']))
            self.list_slaves.SetItem(row, 3, '0x{:X}'.format(s['product']))
            self.list_slaves.SetItem(row, 4, s['state'])
        bridges = [s for s in slaves if s['bridge']]
        if not slaves:
            status = 'No EtherCAT slaves found on {}.'.format(nic)
        elif bridges:
            status = '{} slave(s); EtherCAT-CAN bridge at #{}.'.format(
                len(slaves), bridges[0]['position'])
        else:
            status = ('{} slave(s), but no EtherCAT-CAN bridge (vendor 0x{:X}, '
                      'product 0x{:X}).'.format(len(slaves), ethercat_bus.VENDOR_ID,
                                                ethercat_bus.PRODUCT_CODE))
        self.text_status.SetLabel(status)
