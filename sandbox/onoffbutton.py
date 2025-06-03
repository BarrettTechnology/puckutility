#!/usr/bin/env python3
'''
    onoffbutton.py

    A custom class On/Off clickable - meant as a drop-in replacement for CheckBox.

    Events: EVT_ON_OFF          Generic event on binary change

            EVT_ON              The control changed to On

            EVT_OFF             The control changed to Off

    Event Notes:
        All events return GetValue() i.e.

            def OnOff(self,event):
                current_value = event.GetValue()

    Functions:
        GetValue()              Returns numeric value in the control On (True) = 1 Off (False) = 0
        SetValue(int)           Set numeric value in the control
        GetLabel()              Get current label for the control
        SetLabel(str)           Set label for the control
        SetToolTip(str)         Set tooltip for the control
        SetForegroundColour     Set text colour
        SetBackgroundColour     Set background colour
        SetOnColour             Set On background colour
        SetOnForegroundColour   Set On foreground colour
        SetOffColour            Set Off background colour
        SetOffForegroundColour  Set Off foreground colour
        SetFont(font)           Set label font
        GetFont()               Get label font
        Enable(Bool)            Enable/Disable the control
                                On Disable the control value is frozen
        IsEnabled()             Is the control Enabled - Returns True/False
        SetSpacing(int)         Set size of space between control and label
        GetSpacing()            Get size of space between control and label

    Default Values:
        initial - 0 (False) Off
        label   - blank
        style   - wx.BORDER_NONE
        mono    - False
        border  - True
        size    - 24x16 (Almost as small as a CheckBox)

    Valid Styles:
        Window border styles e.g. wx.BORDER_NONE, wx.BORDER_SIMPLE etc
        wx.ALIGN_RIGHT  Align control to the Right of the Label

Author:     J Healey
Created:    14/01/2021
Copyright:  J Healey - 2021-2022
License:    GPL 3 or any later version
Version:    1.10.0
Email:      <rolfofsaxony@gmx.com>

Change Log:
1.10.0  Adds
        SetOnForegroundColour function
        SetOffForegroundColour function
        Default Off background colour changed from red to grey

1.9.0   Adds
        SetOnColour function
        SetOffColour function

1.8.0   Adds
        rectangle style On/Off control
        SetSpacing function
        Align Right style

1.7.0   First real release
'''

import wx

# Events OnOff, On and Off
oobEVT_ON_OFF = wx.NewEventType()
EVT_ON_OFF = wx.PyEventBinder(oobEVT_ON_OFF, 1)
oobEVT_ON = wx.NewEventType()
EVT_ON = wx.PyEventBinder(oobEVT_ON, 1)
oobEVT_OFF = wx.NewEventType()
EVT_OFF = wx.PyEventBinder(oobEVT_OFF, 1)


class OnOffEvent(wx.PyCommandEvent):
    """ Events sent from the :class:`OnOffButton` when the control changes.
        EVT_ON_OFF  The Control value has changed
        EVT_ON      The Control turned On
        EVT_OFF     The Control turned Off
    """

    def __init__(self, eventType, eventId=1, value=0):
        """
        Default class constructor.

        :param `eventType`: the event type;
        :param `eventId`: the event identifier.
        """

        wx.PyCommandEvent.__init__(self, eventType, eventId)
        self._eventType = eventType
        self.value = value

    def GetValue(self):
        """
        Retrieve the value of the control at the time
        this event was generated.
        """
        return self.value


class OnOffButton(wx.Control):

    def __init__(self, parent, id=wx.ID_ANY, label="", pos=wx.DefaultPosition, size=wx.DefaultSize, initial=0,\
                 style=wx.BORDER_NONE, mono=False, border=True, circle=True, name="OnOff_Button"):
        """
        Default class constructor.

        @param parent:  Parent window. Must not be None.
        @param id:      identifier. A value of -1 indicates a default value.
        @param label:   control label.
        @param pos:     Position. If the position (-1, -1) is specified
                        then a default position is chosen.
        @param size:    If the default size (-1, -1) is specified then the minimum size is chosen.
        @param initial: Initial value 0 False or 1 True
                        - default False.
        @param style:   wx.Border style and/or wx.ALIGN_RIGHT = Align control to the Right of the Label.
        @param mono:    True or False makes the image monochrome
                        - default False
        @param border:  True or False adds a border to the controls image
                        - default True
        @param circle:  True or False Control is a Rounded Rectangle or a Standard Rectangle
                        - default True
        @param name:    Widget name.
        """

        wx.Control.__init__(self, parent, id, pos=pos, size=size, style=style, name=name)
        self._initial = initial
        self._frozen_value = initial
        self._label = label
        self._pos = pos
        self._size = size
        self._name = name
        self._id = id
        self._mono = mono
        self._border = border
        self._style = style
        self._circle = circle
        self.OnClr = 'green'
        self.OffClr = 'grey'
        self.OnClrForeground = None
        self.OffClrForeground = None

        self._spacing = 1
        self._font = wx.SystemSettings.GetFont(wx.SYS_SYSTEM_FONT)
        if self._mono:
            self.OnClr = self.OffClr = 'white'
        if self._initial > 1:
            self._initial = 1
        if self._initial < 0:
            self._initial = 0

        self._Value = initial

        # Initialize images
        self.InitialiseBitmaps()
        self.onoff = wx.StaticBitmap(self, -1, bitmap=wx.Bitmap(self._img))
        self.spacer = wx.StaticText(self, -1, " ")
        self.label = wx.StaticText(self, -1, self._label)

        # Bind the event
        self.onoff.Bind(wx.EVT_LEFT_DOWN, self.OnOff)

        sizer = wx.BoxSizer(wx.HORIZONTAL)
        if self._style & wx.ALIGN_RIGHT:
            sizer.Add(self.label, 0, wx.ALIGN_CENTRE, 0)
            sizer.Add(self.spacer, 0, 0, 0)
            sizer.Add(self.onoff, 0, 0, 0)
        else:
            sizer.Add(self.onoff, 0, 0, 0)
            sizer.Add(self.spacer, 0, 0, 0)
            sizer.Add(self.label, 0, wx.ALIGN_CENTRE, 0)
        self.SetImage(self._initial)
        self.SetSizerAndFit(sizer)

    def InitialiseBitmaps(self):
        self._bitmaps = {
            "On": self._CreateBitmap("On"),
            "Off": self._CreateBitmap("Off"),
            "Disable": self._CreateBitmap("Disable"),
            }
        if self._initial <= 0:
            self._img = self._bitmaps['Off']
        elif self._initial >= 1:
            self._img = self._bitmaps['On']

    def _CreateBitmap(self, type):
        if type == "On":
            self.SetImageSize()
        bmp = wx.Bitmap(self.bmpw, self.bmph)
        dc = wx.MemoryDC(bmp)
        bg = self.GetBackgroundColour()
        fg = self.GetForegroundColour()
        if self.OnClrForeground is None:
            self.OnClrForeground = fg
        if self.OffClrForeground is None:
            self.OffClrForeground = fg
        brush = wx.Brush(bg)
        dc.SetBackground(brush)
        dc.SetPen(wx.Pen(fg))
        dc.Clear()
        dc.SetBrush(brush)

        if self._circle:
            if self._border:
                dc.DrawRoundedRectangle(
                    self.rrouterposx,
                    self.rrouterposy,
                    self.rrouterw,
                    self.rrouterh,
                    self.rrouterradius
                    )
            if type == "On":
                dc.SetBrush(wx.Brush(self.OnClr))
            elif type == "Off":
                dc.SetBrush(wx.Brush(self.OffClr))
            else:
                dc.SetBrush(wx.Brush('grey'))
            dc.SetPen(wx.Pen(bg))
            dc.DrawRoundedRectangle(
                self.rrinnerposx,
                self.rrinnerposy,
                self.rrinnerw,
                self.rrinnerh,
                self.rrinnerradius
                )
            dc.SetBrush(wx.Brush(self.OnClrForeground))
            if type == "On":
                dc.DrawCircle(self.circonpos[0], self.circonpos[1], self.circradius)
            else:
                dc.SetBrush(wx.Brush(self.OffClrForeground))
                dc.DrawCircle(self.circoffpos[0], self.circoffpos[1], self.circradius)

        else:
            if self._border:
                dc.DrawRectangle(
                    self.rrouterposx,
                    self.rrouterposy,
                    self.rrouterw,
                    self.rrouterh,
                    )
            if type == "On":
                dc.SetBrush(wx.Brush(self.OnClr))
            elif type == "Off":
                dc.SetBrush(wx.Brush(self.OffClr))
            else:
                dc.SetBrush(wx.Brush('grey'))
            dc.SetPen(wx.Pen(bg))
            dc.DrawRectangle(
                self.rrinnerposx,
                self.rrinnerposy,
                self.rrinnerw,
                self.rrinnerh,
                )
            dc.SetBrush(wx.Brush(self.OnClrForeground))
            if type == "On":
                dc.DrawRectangle(
                    self.rrinnerposx+int(self.rrouterw/2) - 2,
                    self.rrinnerposy,
                    int(self.rrinnerw/2),
                    self.rrinnerh
                    )
            else:
                dc.SetBrush(wx.Brush(self.OffClrForeground))
                dc.DrawRectangle(
                    self.rrinnerposx,
                    self.rrinnerposy,
                    int(self.rrinnerw/2),
                    self.rrinnerh
                    )

        bmp = dc.GetAsBitmap((0, 0, self.bmpw, self.bmph))
        return bmp

    def SetImageSize(self):
        w, h = self._size
        # Cater for only Width or only Height parameter given
        if h < 0 and w >= 0:
            h = int(w * 0.6667)
        if w < 0 and h >= 0:
            w = int(h / 0.6667)
        # Default minimum size
        w = max(w, 24)
        h = max(h, 16)
        # Size bitmap
        self.bmpw = w
        self.bmph = h
        # Outer rounded rectangle
        self.rrouterw = w - 2
        self.rrouterh = h - 2
        self.rrouterposx = 2
        self.rrouterposy = 2
        self.rrouterradius = (self.rrouterh/2)
        # Inner rounded rectangle
        self.rrinnerw = self.rrouterw - 4
        self.rrinnerh = self.rrouterh - 4
        self.rrinnerposx = self.rrouterposx + 2
        self.rrinnerposy = self.rrouterposy + 2
        self.rrinnerradius = self.rrinnerh/2
        # Circle position and size
        self.circradius = int(self.rrinnerradius - 1)
        self.circoffpos = (int(self.rrinnerradius+self.rrinnerposx), int(self.rrinnerposy + self.rrinnerradius))
        self.circonpos = (int(self.rrinnerw - (self.rrinnerradius-3)), int(self.rrinnerposy + self.rrinnerradius))

    def SetValue(self, value):
        if value > 1:
            value = 1
        if value < 0:
            value = 0
        self._Value = value
        self.SetImage(value)
        self.Update()

    def GetValue(self):
        if self._Value:
            return True
        else:
            return False

    def SetLabel(self, label):
        self.label.SetLabel(label)
        self.spacer.SetLabel(" "*self._spacing)
        w, h = self._font.GetPixelSize()
        # Allow for multi-line labels
        nls = len(label.split('\n'))  # Count newlines in label text
        lw = max([len(line) for line in label.split('\n')])  # Get the longest line in the label text
        w = w*lw  # Width = char pixel width x the longest line width
        h = max(nls*h, h)  # Height maximum of char pixel height x no of lines or char pixel height
        self.label.SetMinSize(wx.Size(w, h))
        self.label.SetSize(wx.Size(w, h))
        iw, ih = self._img.GetSize()
        # Control size image width+text width + Greater of image or text height
        self.SetMinSize(wx.Size(w+iw, max(ih, h)))
        self.Fit()
        self.Update()

    def GetLabel(self):
        return self.label.GetLabel()

    def IsEnabled(self):
        return wx.Control.IsEnabled(self)

    def Enable(self, value):
        if value and self.IsEnabled():  # If value = current state do nothing
            return
        if not value and not self.IsEnabled():
            return
        wx.Control.Enable(self, value)
        self.Update()
        if value:
            # Enable via callafter in case someone has been stabbing away on the disabled control
            wx.CallAfter(self.OnReset)
        else:
            # Disable - Freeze the controls Value and change bitmap
            self._frozen_value = self.GetValue()
            self._img = self._bitmaps['Disable']
            self.onoff.SetBitmap(wx.Bitmap(self._img))
            self.onoff.SetToolTip(self._name+"\n[Disabled]")

    def SetToolTip(self, tip):
        wx.Control.SetToolTip(self, tip)
        self.Refresh()

    def SetHelpText(self, text):
        wx.Control.SetHelpText(self, text)
        self.onoff.SetHelpText(text)
        self.Refresh()

    def SetForegroundColour(self, colour):
        wx.Control.SetForegroundColour(self, colour)
        self.InitialiseBitmaps()
        self.SetImage(self._Value)
        self.Refresh()

    def SetBackgroundColour(self, colour):
        wx.Control.SetBackgroundColour(self, colour)
        self.InitialiseBitmaps()
        self.SetImage(self._Value)
        self.Refresh()

    def SetOnColour(self, colour):
        self.OnClr = colour
        self.InitialiseBitmaps()
        self.SetImage(self._Value)
        self.Refresh()

    def SetOnForegroundColour(self, colour):
        self.OnClrForeground = colour
        self.InitialiseBitmaps()
        self.SetImage(self._Value)
        self.Refresh()

    def SetOffColour(self, colour):
        self.OffClr = colour
        self.InitialiseBitmaps()
        self.SetImage(self._Value)
        self.Refresh()

    def SetOffForegroundColour(self, colour):
        self.OffClrForeground = colour
        self.InitialiseBitmaps()
        self.SetImage(self._Value)
        self.Refresh()

    def SetFont(self, font):
        wx.Control.SetFont(self, font)
        self._font = font
        # Font changed reset label
        self.SetLabel(self._label)

    def GetFont(self):
        return self._font

    def SetSpacing(self, spacing):
        self._spacing = spacing
        self.SetLabel(self._label)
        self.Refresh()

    def GetSpacing(self):
        return self._spacing

    def OnReset(self):
        # Reset the control to the state it was in when it was Disabled
        self.SetValue(self._frozen_value)
        self.SetImage(self._frozen_value)

    def OnOff(self, event):
        state = self._Value
        if state == 0:
            state = 1
        else:
            state = 0

        self.SetImage(state)

        if state:
            self.SetValue(state)
            # event change
            event = OnOffEvent(oobEVT_ON_OFF, self.GetId(), state)
            event.SetEventObject(self)
            self.GetEventHandler().ProcessEvent(event)
            # event On
            event = OnOffEvent(oobEVT_ON, self.GetId(), state)
            event.SetEventObject(self)
            self.GetEventHandler().ProcessEvent(event)
        else:
            self.SetValue(state)
            # event change
            event = OnOffEvent(oobEVT_ON_OFF, self.GetId(), state)
            event.SetEventObject(self)
            self.GetEventHandler().ProcessEvent(event)
            # event Off
            event = OnOffEvent(oobEVT_OFF, self.GetId(), state)
            event.SetEventObject(self)
            self.GetEventHandler().ProcessEvent(event)

    def SetImage(self, value):
        # Set appropriate image and tooltip
        tp = self.GetToolTip()
        if not self.IsEnabled():
            self._img = self._bitmaps['Disable']
            if tp is not None:
                tt = tp.GetTip()+"\n[Currently Disabled]"
            else:
                tt = "[Disabled]"
        else:
            if value <= 0:
                self._img = self._bitmaps['Off']
                if tp is not None:
                    tt = tp.GetTip()+"\n[Currently Off]"
                else:
                    tt = "[ Off ]"
            else:
                self._img = self._bitmaps['On']
                if tp is not None:
                    tt = tp.GetTip()+"\n[Currently On]"
                else:
                    tt = "[ On ]"

        self.onoff.SetBitmap(wx.Bitmap(self._img))
        self.onoff.SetToolTip(tt)

if __name__ == '__main__':

    class MyFrame(wx.Frame):

        def __init__(self, parent):

            wx.Frame.__init__(self, parent, -1, "On/Off Demo", size=(-1, 550))

            panel = wx.Panel(self)
            panel.SetBackgroundColour("lightgreen")

            self.onoff = OnOffButton(panel, -1, label=" 80 X 48", pos=(10, 50), size=(160, 96), initial=1)
            self.onoff1 = OnOffButton(panel, -1, " 48 X 30", pos=(10, 200), size=(48, 30), initial=0, border=False)
            self.onoff2 = OnOffButton(panel, -1, " 40 x 24 Rectangle, Right && spacing", pos=(10, 250),\
                                      size=(40, 24), initial=1, circle=False, style=wx.BORDER_NONE | wx.ALIGN_RIGHT)
            self.onoff3 = OnOffButton(panel, -1, " 30 x 20 Mono Rectangle", pos=(10, 300),\
                                      size=(30, 20), initial=0, mono=True, circle=False)
            self.onoff4 = OnOffButton(panel, -1, " Minimum Mono No border", pos=(10, 350),\
                                      initial=0, mono=True, border=False)
            self.onoff5 = OnOffButton(panel, -1, " Minimum 20 X 16", pos=(10, 400), initial=0)
            self.onoff6 = OnOffButton(panel, -1, " Minimum 20 X 16 Mono", pos=(170, 400), initial=0, mono=True)
            self.onoff7 = OnOffButton(panel, -1, " Minimum 20 X 16 Rectangle", pos=(10, 450), initial=0, circle=False)
            self.onoff8 = OnOffButton(panel, -1, " Minimum 20 X 16 Mono Rectangle", pos=(10, 500),\
                                      initial=0, mono=True, circle=False)
            self.txt = wx.TextCtrl(panel, -1, "On", size=(50, 30), pos=(300, 50))
            self.txt1 = wx.TextCtrl(panel, -1, "Off", size=(50, 30), pos=(300, 200))
            self.onoff.Bind(EVT_ON_OFF, self.OnOff)
            self.Bind(EVT_ON, self.SwOn)
            self.Bind(EVT_OFF, self.SwOff)
            self.onoff1.Bind(EVT_ON_OFF, self.OnOff1)
            #self.onoff1.SetOnColour("white")
            #self.onoff1.SetOnForegroundColour("blue")
            #self.onoff1.SetOffColour("grey")
            # self.onoff1.SetOffForegroundColour("red")
            self.onoff2.SetOnColour("olivedrab")
            self.onoff2.SetOnForegroundColour("darkgreen")
            font = wx.SystemSettings.GetFont(wx.SYS_SYSTEM_FONT)
            font.SetPointSize(16)
            font.SetFaceName('FreeMono')
            self.onoff.SetFont(font)
            self.onoff2.SetSpacing(10)
            self.Show()
            self.onoff3.Enable(False)
            self.timer = wx.Timer(self)
            self.Bind(wx.EVT_TIMER, self.Toggle)
            self.timer.Start(5000)

        def Toggle(self, event):
            x = self.onoff3.IsEnabled()
            x = not x
            self.onoff3.Enable(x)

        def OnOff(self, event):
            if event.GetValue():
                self.txt.SetValue("On")
                self.onoff.SetLabel(" Label On")
            else:
                self.txt.SetValue("Off")
                self.onoff.SetLabel(" Label Off")
            event.Skip()

        def OnOff1(self, event):
            if event.GetValue():
                self.txt1.SetValue("On")
            else:
                self.txt1.SetValue("Off")
            event.Skip()

        def SwOn(self, event):
            obj = event.GetEventObject()
            print(obj.Name + " On")

        def SwOff(self, event):
            obj = event.GetEventObject()
            print(obj.Name + " Off")

    app = wx.App()
    frame = MyFrame(None)
    app.SetTopWindow(frame)
    frame.Show()
    app.MainLoop()
