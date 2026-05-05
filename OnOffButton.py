#!/usr/bin/env python

import wx

# Class for custom button!
# Events OnOff, On and Off
oobEVT_ON_OFF = wx.NewEventType()
EVT_ON_OFF = wx.PyEventBinder(oobEVT_ON_OFF, 1)
oobEVT_ON = wx.NewEventType()
EVT_ON = wx.PyEventBinder(oobEVT_ON, 1)
oobEVT_OFF = wx.NewEventType()
EVT_OFF = wx.PyEventBinder(oobEVT_OFF, 1)

# Internal indicator styles
OOB_CIRCLE = 0
OOB_ARROW = 1
OOB_RECTANGLE = 2
OOB_RADIO = 3

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
                 style=wx.BORDER_NONE, mono=False, border=True, circle=True, internal_style=OOB_CIRCLE,\
                 name="OnOff_Button"):
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
                        - When used with style OOB_RADIO the radio button is Circular or Square
                        - default True
        @param internal_style:   0 - 3 The style of On/Off indicator
                        - default 0 (Circle)
        @param name:    Widget name.
        """

        wx.Control.__init__(self, parent, id, pos=pos, size=size, style=style, name=name)
        self._initial = initial
        self._label = label
        self._pos = pos
        self._size = size
        self._name = name
        self._mono = mono
        self._border = border
        self._style = style
        self._circle = circle
        self._internal_style = internal_style
        if self._internal_style == OOB_RECTANGLE:
            self._circle = False
        self.OnClr = '#698B22' # olivedrab4
        self.OffClr = '#787878' # grey
        self.OnClrForeground = None
        self.OffClrForeground = None
        self.own_txt_colour = None
        self.mnemonic = False
        self._spacing = 0
        self._font = wx.SystemSettings.GetFont(wx.SYS_DEFAULT_GUI_FONT)
        if self._mono:
            self.OnClr = self.OffClr = '#ffffff' # white
        if self._internal_style == OOB_RADIO:
            self.OffClr = '#ffffff' # white
        if self._initial > 1:
            self._initial = 1
        if self._initial < 0:
            self._initial = 0

        self._Value = initial
        self._backgroundcolour = parent.GetBackgroundColour()
        self._foregroundcolour = parent.GetForegroundColour()

        # Initialize images
        self.InitialiseBitmaps()
        self.onoff = wx.StaticBitmap(self, -1, bitmap=wx.Bitmap(self._img))
        self.spacer = wx.StaticText(self, -1, "")
        self.label = wx.StaticText(self, -1, self._label)
        self.mnemonic = self.Mnemonic(self._label)

        # Bind the event
        self.onoff.Bind(wx.EVT_LEFT_DOWN, self.OnOff)
        self.label.Bind(wx.EVT_LEFT_DOWN, self.OnOff)
        self.Bind(wx.EVT_KEY_DOWN, self.OnKey)
        self.Bind(wx.EVT_KILL_FOCUS, self.OnKillFocus)

        self.sizer = wx.BoxSizer(wx.HORIZONTAL)
        if self._style & wx.ALIGN_RIGHT:
            self.sizer.Add(self.label, 0, wx.ALIGN_CENTER_VERTICAL|wx.RIGHT, 5)
            self.sizer.Add(self.spacer, 0, wx.ALIGN_CENTER_VERTICAL, 0)
            self.sizer.Add(self.onoff, 0, wx.ALIGN_CENTER_VERTICAL, 0)
        else:
            self.sizer.Add(self.onoff, 0,  wx.ALIGN_CENTER_VERTICAL, 0)
            self.sizer.Add(self.spacer, 0, wx.ALIGN_CENTER_VERTICAL, 0)
            self.sizer.Add(self.label, 0, wx.ALIGN_CENTER_VERTICAL|wx.LEFT, 5)
        if not self._spacing:
            self.sizer.Hide(self.spacer)
        if not self._label:
            self.sizer.Hide(self.label)
        self.SetImage(self._initial)
        self.SetSizerAndFit(self.sizer)

        self.SetBackgroundColour(self._backgroundcolour)
        self.SetForegroundColour(self._foregroundcolour)

        if self._label:
            self.SetLabel(self._label)
            self.SetFocus()

    def InitialiseBitmaps(self):
        self._bitmaps = {
            "On": self._CreateBitmap("On"),
            "Off": self._CreateBitmap("Off"),
            "DisableOff": self._CreateBitmap("DisableOff"),
            "DisableOn": self._CreateBitmap("DisableOn"),
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
        try:
            gcdc = wx.GCDC(dc) # Anti-aliased for Windows ?? Who knows? Not me!
        except Exception as e:
            gcdc = dc

        bg = self.GetBackgroundColour()
        fg = self.GetForegroundColour()
        if self.OnClrForeground is None:
            self.OnClrForeground = fg
        if self.OffClrForeground is None:
            self.OffClrForeground = fg
        brush = wx.Brush(bg)
        gcdc.SetBackground(brush)
        gcdc.SetPen(wx.Pen(fg))
        gcdc.Clear()
        gcdc.SetBrush(brush)

        if self._circle:
            if self._border:
                gcdc.DrawRoundedRectangle(
                    self.rrouterposx,
                    self.rrouterposy,
                    self.rrouterw,
                    self.rrouterh,
                    self.rrouterradius
                    )
            if type == "On":
                gcdc.SetBrush(wx.Brush(self.OnClr))
            elif type == "Off":
                gcdc.SetBrush(wx.Brush(self.OffClr))
            else:
                gcdc.SetBrush(wx.Brush('#787878'))
            if type[:7] == "Disable" or self._mono:
                gcdc.SetBrush(wx.Brush('#ffffff')) # white
            gcdc.SetPen(wx.Pen(bg))
            gcdc.DrawRoundedRectangle(
                self.rrinnerposx,
                self.rrinnerposy,
                self.rrinnerw,
                self.rrinnerh,
                self.rrinnerradius
                )
        else:
            if self._border:
                gcdc.DrawRectangle(
                    self.rrouterposx,
                    self.rrouterposy,
                    self.rrouterw,
                    self.rrouterh,
                    )
            if type == "On":
                gcdc.SetBrush(wx.Brush(self.OnClr))
            elif type == "Off":
                gcdc.SetBrush(wx.Brush(self.OffClr))
            else:
                gcdc.SetBrush(wx.Brush('#787878')) # grey
            if type[:7] == "Disable" or self._mono:
                gcdc.SetBrush(wx.Brush('#ffffff')) # white/grey
            gcdc.SetPen(wx.Pen(bg))
            gcdc.DrawRectangle(
                self.rrinnerposx,
                self.rrinnerposy,
                self.rrinnerw,
                self.rrinnerh,
                )
        if type == "On" or type == "DisableOn":
            gcdc.SetBrush(wx.Brush(self.OnClrForeground))
            if type == "DisableOn":
                gcdc.SetBrush(wx.Brush('#484848')) # grey
            if self._internal_style == OOB_ARROW:
                gcdc.DrawPolygon([(self.bmpw-4, int(self.bmph/2)),(int(self.bmpw/2),2),(int(self.bmpw/2),self.bmph-1)],0,0)
            elif self._internal_style == OOB_RECTANGLE:
                x = self.rrinnerposx+int(self.rrouterw/2) - 2
                y = self.rrinnerposy
                w = int(self.rrinnerw/2)
                h = self.rrinnerh
                gcdc.DrawRectangle(x, y, w, h)
            elif self._internal_style == OOB_RADIO and self._circle:
                gcdc.DrawCircle(int(self.circoffpos[0]), int(self.circoffpos[1]), int(self.circradius))
            elif self._internal_style == OOB_RADIO:
                gcdc.DrawRectangle(
                    self.rrinnerposx,
                    self.rrinnerposy,
                    self.rrinnerw,
                    self.rrinnerh,
                    )
            else:
                gcdc.DrawCircle(int(self.circonpos[0]), int(self.circonpos[1]), int(self.circradius))
        else:
            gcdc.SetBrush(wx.Brush(self.OffClrForeground))
            if type == "DisableOff":
                gcdc.SetBrush(wx.Brush('#484848')) # grey
            if self._internal_style == OOB_ARROW:
                gcdc.DrawPolygon([(4, int(self.bmph/2)),(int(self.bmpw/2),2),(int(self.bmpw/2),self.bmph-1)],0,0)
            elif self._internal_style == OOB_RECTANGLE:
                x = self.rrinnerposx
                y = self.rrinnerposy
                w = int(self.rrinnerw/2)
                h = self.rrinnerh
                gcdc.DrawRectangle(x, y, w, h)
            elif self._internal_style == OOB_RADIO and self._circle:
                gcdc.DrawCircle(int(self.circoffpos[0]), int(self.circoffpos[1]), int(0))
            elif self._internal_style == OOB_RADIO:
                gcdc.DrawRectangle(
                    self.rrinnerposx,
                    self.rrinnerposy,
                    0,
                    0,
                    )
            else:
                gcdc.DrawCircle(int(self.circoffpos[0]), int(self.circoffpos[1]), int(self.circradius))

        bmp = dc.GetAsBitmap((0, 0, self.bmpw, self.bmph))
        del dc, gcdc
        return bmp

    def DisableImage(self, bmp):
        bmp = bmp.ConvertToDisabled()
        return bmp

    def SetImageSize(self):
        w, h = self._size
        # Cater for only Width or only Height parameter given
        if h < 0 and w >= 0:
            h = int(w * 0.6667)
        if w < 0 and h >= 0:
            w = int(h / 0.6667)
        # Set Default minimum size, also caters for no size set (-1, -1)
        if self._internal_style == OOB_RADIO:
            w = max(self._size[0], 16)
            h = max(self._size[1], 16)
        else:
            w = max(w, 24)
            h = max(h, 16)

        if self._internal_style == OOB_RADIO: # force equal dimensions for radio
            m = max(w, h)
            if not m % 2: # Odd number centres properly
                m -= 1
            w = h = m

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
        self.circradius = round(self.rrinnerradius - 1)
        self.circoffpos = (round(self.rrinnerradius+self.rrinnerposx), round(self.rrinnerposy + self.rrinnerradius))
        self.circonpos = (round(self.rrinnerw - (self.rrinnerradius-3)), round(self.rrinnerposy + self.rrinnerradius))

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
        font = self.label.GetFont()
        if not font.IsOk(): # Invalid font - swap out for the system default
            font = wx.SystemSettings.GetFont(wx.SYS_DEFAULT_GUI_FONT)
        dc = wx.ClientDC(self)
        dc.SetFont(font)
        textW, textH = dc.GetTextExtent(label)
        spacingW, _ = dc.GetTextExtent(" "*self._spacing)
        imgW, imgH = self._img.GetSize()
        textW += 4
        imgW += 2
        maxW = textW + spacingW + imgW
        maxH = max(textH, imgH)
        best = wx.Size(maxW, maxH)
        #self.CacheBestSize(best)
        self.label.SetMinSize(wx.Size(textW, textH))
        self.label.SetSize(wx.Size(textW, textH))
        self.spacer.SetMinSize(wx.Size(spacingW, textH))
        self.spacer.SetSize(wx.Size(spacingW, textH))
        self.SetMinSize(best)
        self.Fit()
        self.mnemonic = self.Mnemonic(label)
        self._label = label
        _colour = self.GetBackgroundColour()
        self.txt_colour = self.GetBrightness(_colour)
        self.label.SetForegroundColour(self.txt_colour)

        if label:
        #    self.SetFocus()
            self.sizer.Show(self.label)
        self.Refresh()

    def GetBrightness(self, _colour):
        '''
        Set text colour based on the brightness of the current background colour
        '''
        brightness = wx.Colour(_colour).GetLuminance()
        if brightness < 0.5:
            txt_colour = wx.WHITE
        else:
            txt_colour = wx.BLACK
        if self.own_txt_colour:
            txt_colour = self.own_txt_colour
        return txt_colour

    def Mnemonic(self, label):
        mnemonic = label
        if "&&" in mnemonic: # remove literal & from the test
            mnemonic = mnemonic.replace('&&', '')
        if "&" in mnemonic: # find first &
            st, *end = mnemonic.split('&', 1)
            char = end[0][0]
            return ord(char.upper())
        else:
            return False

    def OnKey(self, event):
        keycode = event.GetKeyCode()
        mods = event.GetModifiers()
        if mods == 1 and keycode == self.mnemonic: # Alt + & marked label character
            self.OnOff(None)
        event.Skip(True)

    def GetLabel(self):
        return self.label.GetLabel()

    def IsEnabled(self):
        return wx.Control.IsEnabled(self)

    def Disable(self, value=True):
        self.Enable(not value)

    def Enable(self, value=True):
        wx.Control.Enable(self, value)
        self.SetImage(self.GetValue())
        if self.IsEnabled(): # force text change
            self.label.SetForegroundColour(self.txt_colour)
        else:
            self.label.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))
        self.Refresh()

    def SetToolTip(self, tip):
        wx.Control.SetToolTip(self, tip)
        self.Refresh()

    def SetHelpText(self, text):
        wx.Control.SetHelpText(self, text)
        self.onoff.SetHelpText(text)
        self.Refresh()

    def ShouldInheritColours(self):
        return True

    def SetForegroundColour(self, colour):
        wx.Control.SetForegroundColour(self, colour)
        self.InitialiseBitmaps()
        self.SetImage(self._Value)
        self.Refresh()

    def SetBackgroundColour(self, colour):
        wx.Control.SetBackgroundColour(self, colour)
        self.InitialiseBitmaps()
        self.SetImage(self._Value)
        self.txt_colour = self.GetBrightness(colour)
        self.label.SetForegroundColour(self.txt_colour)
        self.Refresh()

    def SetLabelTextColour(self, colour):
        self.label.SetForegroundColour(colour)
        self.own_txt_colour = colour
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
        state = self._Value
        if state:
            font.SetStyle(wx.FONTSTYLE_NORMAL)
        else:
            font.SetStyle(wx.FONTSTYLE_SLANT)
        wx.Control.SetFont(self, font)
        self.label.SetFont(font)
        self.spacer.SetFont(font)
        self._font = font
        # Font changed reset label
        self.SetLabel(self._label)

    def GetFont(self):
        return self._font

    def SetSpacing(self, spacing):
        self._spacing = spacing
        if self._spacing:
            self.sizer.Show(self.spacer)
        self.SetLabel(self._label)
        self.Refresh()

    def GetSpacing(self):
        return self._spacing

    def OnOff(self, event):
        # print('State Switch')
        # self.liveGraph = True
        state = self._Value
        if state == 0:
            state = 1
        else:
            state = 0

        self.SetImage(state)
        self.SetValue(state)
        # event change
        event = OnOffEvent(oobEVT_ON_OFF, self.GetId(), state)
        event.SetEventObject(self)
        self.GetEventHandler().ProcessEvent(event)

        if state:
            # event On
            event = OnOffEvent(oobEVT_ON, self.GetId(), state)
            event.SetEventObject(self)
            self.GetEventHandler().ProcessEvent(event)
        else:
            # event Off
            event = OnOffEvent(oobEVT_OFF, self.GetId(), state)
            event.SetEventObject(self)
            self.GetEventHandler().ProcessEvent(event)

    def SetImage(self, value):
        # Set appropriate image and tooltip
        tp = self.GetToolTip()
        if self.IsEnabled():
            if value <= 0:
                self._img = self._bitmaps['Off']
                self._font.SetStyle(wx.FONTSTYLE_SLANT)
                self.SetFont(self._font)
                if tp is not None:
                    tt = tp.GetTip()+"\n[ Off ]"
                else:
                    tt = "[ Off ]"
            else:
                self._img = self._bitmaps['On']
                self._font.SetStyle(wx.FONTSTYLE_NORMAL)
                self.SetFont(self._font)
                if tp is not None:
                    tt = tp.GetTip()+"\n[ On ]"
                else:
                    tt = "[ On ]"
        else: # Disabled
            if value <= 0:
                self._img = self._bitmaps['DisableOff']
            else:
                self._img = self._bitmaps['DisableOn']
            if tp is not None:
                tt = tp.GetTip()+"\n[ Disabled ]"
            else:
                tt = "[ Disabled ]"

        if hasattr(self, 'onoff'): # Not present if initially setting the parent backgroundcolour
            self.onoff.SetBitmap(wx.Bitmap(self._img))
            self.onoff.SetToolTip(tt)

    def OnKillFocus(self, event):
        event.Skip()
