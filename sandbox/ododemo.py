#!/usr/bin/env python3

import wx
from odometer import Odometer

class DemoApp(wx.Frame):
    def __init__(self, parent, title):
        super().__init__(parent, title=title, size=(600, 200))

        panel = wx.Panel(self)
        panel.SetBackgroundColour("lightgray")

        wx.StaticText(panel, label="Odometer Control:", pos=(20, 20))

        # Create an odometer control with format ###,###.###
        self.odometer = Odometer(panel, pos=(20, 50), size=(500, 50), format="###.####", initial=0.0)

        # Create a text box to display the value
        self.text_box = wx.TextCtrl(panel, pos=(20, 120), size=(200, 30))

        # Periodically update the value field
        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.OnGetValue, self.timer)
        self.timer.Start(100)  # Update every 100ms

        self.Show()

    def OnGetValue(self, event):
        """
        Handles the event to display the current value of the odometer.
        """
        value = self.odometer.GetValue()
        self.text_box.SetValue(str(value))


if __name__ == "__main__":
    app = wx.App(False)
    frame = DemoApp(None, "Odometer Demo")
    app.MainLoop()