import wx
import wx.adv
import OnOffButton as _oob

# Saved before any monkey-patching so TallTextCtrl can always reach the real class.
_OrigTextCtrl = wx.TextCtrl


class TallTextCtrl(wx.Panel):
    """Drop-in for wx.TextCtrl that vertically centres text on Windows.

    On Windows, single-line EDIT controls draw text at the top of the client
    area regardless of control height.  This panel wrapper places a borderless
    inner TextCtrl inside a BORDER_THEME panel and uses vertical stretch spacers
    to centre it, matching the text position of WindowsFriendlyChoice.

    Only instantiated on Windows (monkey-patched in puckutilityapp.py).
    """

    _BORDER_MASK = (wx.BORDER_SUNKEN | wx.BORDER_RAISED | wx.BORDER_STATIC |
                    wx.BORDER_THEME | wx.BORDER_SIMPLE | wx.BORDER_NONE)
    _INNER_EVENTS = (wx.EVT_TEXT, wx.EVT_TEXT_ENTER,
                     wx.EVT_SET_FOCUS, wx.EVT_KILL_FOCUS)

    def __init__(self, parent, id=wx.ID_ANY, value='',
                 pos=wx.DefaultPosition, size=wx.DefaultSize,
                 style=0, validator=wx.DefaultValidator, name='', **kwargs):
        inner_style = (style & ~self._BORDER_MASK) | wx.BORDER_NONE
        super().__init__(parent, id, pos, size, style=wx.BORDER_THEME)
        self._inner = _OrigTextCtrl(self, wx.ID_ANY, value,
                                     style=inner_style, validator=validator)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.AddStretchSpacer(1)
        sizer.Add(self._inner, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 1)
        sizer.AddStretchSpacer(1)
        self.SetSizer(sizer)
        self.SetBackgroundColour(self._inner.GetBackgroundColour())
        self.Bind(wx.EVT_LEFT_DOWN, lambda e: self._inner.SetFocus())

    def GetValue(self):       return self._inner.GetValue()
    def SetValue(self, v):    self._inner.SetValue(v)
    def ChangeValue(self, v): self._inner.ChangeValue(v)
    def Clear(self):          self._inner.Clear()

    def SetMargins(self, left=wx.DefaultCoord, top=wx.DefaultCoord):
        # Vertical centering is handled by the stretch spacers; only forward
        # the horizontal margin so text isn't flush against the left border.
        self._inner.SetMargins(left)

    def Enable(self, enable=True):
        # No-op: disabling the panel paints a gray disabled-window background,
        # and disabling the inner TextCtrl makes Windows draw it with a gray
        # client area.  Read-only fields use TE_READONLY for non-editability
        # and should keep a white background, so we suppress Enable(False)
        # entirely on this wrapper.
        return True

    def SetFont(self, font):
        self._inner.SetFont(font)
        return super().SetFont(font)

    def SetForegroundColour(self, colour):
        self._inner.SetForegroundColour(colour)
        return super().SetForegroundColour(colour)

    def SetBackgroundColour(self, colour):
        self._inner.SetBackgroundColour(colour)
        return super().SetBackgroundColour(colour)

    def SetToolTip(self, tip):
        self._inner.SetToolTip(tip)
        return super().SetToolTip(tip)

    def Bind(self, event, handler, source=None, id=wx.ID_ANY, id2=wx.ID_ANY):
        if event in self._INNER_EVENTS:
            self._inner.Bind(event, handler)
        else:
            super().Bind(event, handler, source, id, id2)

    def Unbind(self, event, source=None, id=wx.ID_ANY,
               id2=wx.ID_ANY, handler=None):
        if event in self._INNER_EVENTS:
            return self._inner.Unbind(event, handler=handler)
        return super().Unbind(event, source, id, id2, handler)


class WindowsFriendlyChoice(wx.adv.OwnerDrawnComboBox):
    """Drop-in replacement for wx.Choice whose height matches wx.TextCtrl on Windows.

    On Windows, native CBS_DROPDOWNLIST (wx.Choice) is painted by the Windows GDI
    at the font height regardless of the window size, so SetMinSize height is
    visually ignored — combos render shorter than adjacent TextCtrls of the same
    requested height.  OwnerDrawnComboBox is a pure-wxPython control whose overall
    size IS honoured by the sizer, and whose content is rendered by OnDrawItem so
    text is centred within the full allocated height.

    Bind() transparently routes wx.EVT_CHOICE to wx.EVT_COMBOBOX so callers
    written against wx.Choice keep working without modification.

    Only instantiated on Windows (monkey-patched in puckutilityapp.py).
    """

    def __init__(self, parent, id=wx.ID_ANY, pos=wx.DefaultPosition,
                 size=wx.DefaultSize, choices=[], style=0, **kwargs):
        super().__init__(parent, id, value='',
                         pos=pos, size=size, choices=choices,
                         style=wx.CB_READONLY)

    def Bind(self, event, handler, source=None, id=wx.ID_ANY, id2=wx.ID_ANY):
        if event is wx.EVT_CHOICE:
            event = wx.EVT_COMBOBOX
        return super().Bind(event, handler, source, id, id2)

    def OnDrawItem(self, dc, rect, item, flags):
        if item == wx.NOT_FOUND:
            return
        dc.SetFont(self.GetFont())
        if (flags & wx.adv.ODCB_PAINTING_SELECTED and
                not (flags & wx.adv.ODCB_PAINTING_CONTROL)):
            dc.SetTextForeground(
                wx.SystemSettings.GetColour(wx.SYS_COLOUR_HIGHLIGHTTEXT))
        else:
            dc.SetTextForeground(
                wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOWTEXT))
        r = wx.Rect(rect.x + 3, rect.y, rect.width - 3, rect.height)
        dc.DrawLabel(self.GetString(item), r,
                     wx.ALIGN_CENTER_VERTICAL | wx.ALIGN_LEFT)

    def OnMeasureItem(self, item):
        return 24

    def OnMeasureItemWidth(self, item):
        return -1


class TransparentText(wx.Control):
    """
    Drop-in replacement for wx.StaticText that paints the frame background
    image behind itself before drawing text, so no white box appears.

    The parent frame must expose a `backgroundBMP` attribute (wx.Bitmap).
    If it is absent the control falls back to the system background colour.
    """

    def __init__(self, parent, id=wx.ID_ANY, label='',
                 pos=wx.DefaultPosition, size=wx.DefaultSize,
                 style=wx.ALIGN_LEFT, name='TransparentText'):
        wx.Control.__init__(self, parent, id, pos, size, style=wx.BORDER_NONE)
        self._label = label
        align = style & (wx.ALIGN_CENTER_HORIZONTAL | wx.ALIGN_RIGHT)
        self._align = align if align else wx.ALIGN_LEFT
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_ERASE_BACKGROUND, lambda e: None)

    def _on_paint(self, event):
        dc = wx.PaintDC(self)
        parent = self.GetParent()
        bmp = getattr(parent, 'backgroundBMP', None)
        if bmp:
            pos = parent.ScreenToClient(self.GetScreenPosition())
            dc.DrawBitmap(bmp, -pos.x, -pos.y)
        dc.SetFont(self.GetFont())
        dc.SetTextForeground(self.GetForegroundColour())
        dc.DrawLabel(self._label, self.GetClientRect(),
                     self._align | wx.ALIGN_CENTER_VERTICAL)

    def DoGetBestSize(self):
        dc = wx.ClientDC(self)
        dc.SetFont(self.GetFont())
        w, h = dc.GetTextExtent(self._label if self._label else ' ')
        return wx.Size(w + 2, h + 2)

    def SetLabel(self, label):
        self._label = label
        self.InvalidateBestSize()
        self.Refresh()

    def GetLabel(self):
        return self._label

    def SetForegroundColour(self, colour):
        wx.Control.SetForegroundColour(self, colour)
        self.Refresh()

    def SetFont(self, font):
        wx.Control.SetFont(self, font)
        self.InvalidateBestSize()
        self.Refresh()

    def AcceptsFocus(self):
        return False


class TransparentOnOffButton(wx.Control):
    """
    Transparent-background toggle switch.  Drop-in replacement for
    OnOffButton.OnOffButton that paints the frame background image behind
    itself and draws the toggle directly, so no opaque panel appears.

    Fires the same EVT_ON_OFF / EVT_ON / EVT_OFF events as OnOffButton.
    API matches the subset used by puckutilityapp: SetValue / GetValue /
    SetOnColour / SetOffColour / SetOnForegroundColour / SetOffForegroundColour.
    """

    def __init__(self, parent, id=wx.ID_ANY, pos=wx.DefaultPosition,
                 size=wx.DefaultSize, initial=0, border=False,
                 name='TransparentOnOffButton'):
        wx.Control.__init__(self, parent, id, pos, size,
                            style=wx.BORDER_NONE, name=name)
        self._value = int(bool(initial))
        self._border = border
        self._on_colour = wx.Colour('#FF7C1B')
        self._off_colour = wx.Colour('#253B92')
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_ERASE_BACKGROUND, lambda e: None)
        self.Bind(wx.EVT_LEFT_DOWN, self._on_click)

    def _find_background(self):
        p = self.GetParent()
        while p:
            if hasattr(p, 'backgroundBMP'):
                return p, p.backgroundBMP
            p = p.GetParent()
        return None, None

    def _on_paint(self, event):
        # The parent panel paints the frame background behind this control.
        # This handler only needs to draw the toggle pill and thumb.
        dc = wx.PaintDC(self)
        w, h = self.GetClientSize()

        gc = wx.GraphicsContext.Create(dc)
        if gc:
            colour = self._on_colour if self._value else self._off_colour
            pad = 3
            track_h = h - 2 * pad
            track_r = track_h / 2.0
            gc.SetPen(wx.TRANSPARENT_PEN)
            gc.SetBrush(gc.CreateBrush(wx.Brush(colour)))
            gc.DrawRoundedRectangle(pad, pad, w - 2 * pad, track_h, track_r)
            thumb_r = track_r - 2
            cx = (w - pad - track_r) if self._value else (pad + track_r)
            cy = h / 2.0
            gc.SetBrush(gc.CreateBrush(wx.Brush(wx.WHITE)))
            gc.DrawEllipse(cx - thumb_r, cy - thumb_r, thumb_r * 2, thumb_r * 2)
            del gc
        else:
            colour = self._on_colour if self._value else self._off_colour
            pad = 3
            track_h = h - 2 * pad
            track_r = track_h // 2
            dc.SetPen(wx.TRANSPARENT_PEN)
            dc.SetBrush(wx.Brush(colour))
            dc.DrawRoundedRectangle(pad, pad, w - 2 * pad, track_h, track_r)
            thumb_r = track_r - 2
            cx = int((w - pad - track_r) if self._value else (pad + track_r))
            cy = h // 2
            dc.SetBrush(wx.Brush(wx.WHITE))
            dc.DrawCircle(cx, cy, thumb_r)

    def DoGetBestSize(self):
        return wx.Size(50, 34)

    def GetValue(self):
        return bool(self._value)

    def SetValue(self, value):
        self._value = int(bool(value))
        self.Refresh()

    def SetOnColour(self, colour):
        self._on_colour = wx.Colour(colour)
        self.Refresh()

    def SetOffColour(self, colour):
        self._off_colour = wx.Colour(colour)
        self.Refresh()

    def SetOnForegroundColour(self, colour):
        pass

    def SetOffForegroundColour(self, colour):
        pass

    def _on_click(self, event):
        self._value ^= 1
        self.Refresh()
        for type_id in (_oob.oobEVT_ON_OFF,
                        _oob.oobEVT_ON if self._value else _oob.oobEVT_OFF):
            evt = _oob.OnOffEvent(type_id, self.GetId(), self._value)
            evt.SetEventObject(self)
            self.GetEventHandler().ProcessEvent(evt)


class TransparentBitmap(wx.Control):
    """
    Drop-in replacement for wx.StaticBitmap that paints the frame background
    image behind the bitmap, so no white/grey box appears when the bitmap has
    transparency or is smaller than its cell.

    API matches the subset used by puckutilityapp: SetBitmap / GetBitmap.
    """

    def __init__(self, parent, id=wx.ID_ANY, bitmap=wx.NullBitmap,
                 pos=wx.DefaultPosition, size=wx.DefaultSize,
                 name='TransparentBitmap'):
        wx.Control.__init__(self, parent, id, pos, size, style=wx.BORDER_NONE,
                            name=name)
        self._bitmap = bitmap
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_ERASE_BACKGROUND, lambda e: None)

    def _on_paint(self, event):
        dc = wx.PaintDC(self)
        w, h = self.GetClientSize()

        # Compose off-screen, then blit atomically to eliminate flicker.
        buf = wx.Bitmap(w, h)
        mdc = wx.MemoryDC(buf)

        parent = self.GetParent()
        bg = getattr(parent, 'backgroundBMP', None)
        if bg:
            pos = parent.ScreenToClient(self.GetScreenPosition())
            mdc.DrawBitmap(bg, -pos.x, -pos.y)
        else:
            mdc.SetBackground(wx.Brush(self.GetBackgroundColour()))
            mdc.Clear()

        if self._bitmap and self._bitmap.IsOk():
            bw = self._bitmap.GetWidth()
            bh = self._bitmap.GetHeight()
            x = (w - bw) // 2
            y = (h - bh) // 2
            gc = wx.GraphicsContext.Create(mdc)
            if gc:
                gc.DrawBitmap(self._bitmap, x, y, bw, bh)
                del gc
            else:
                mdc.DrawBitmap(self._bitmap, x, y, True)

        del mdc
        dc.DrawBitmap(buf, 0, 0)

    def DoGetBestSize(self):
        if self._bitmap and self._bitmap.IsOk():
            return wx.Size(self._bitmap.GetWidth(), self._bitmap.GetHeight())
        return wx.Size(60, 60)

    def SetBitmap(self, bitmap):
        if isinstance(bitmap, wx.Image):
            bitmap = wx.Bitmap(bitmap)
        self._bitmap = bitmap
        self.InvalidateBestSize()
        self.Refresh()

    def GetBitmap(self):
        return self._bitmap

    def AcceptsFocus(self):
        return False


class TransparentSlider(wx.Control):
    """
    Drop-in replacement for wx.Slider that draws its track and thumb centered
    within whatever height the sizer assigns, so it aligns with adjacent combo
    boxes regardless of wx.EXPAND.  Fires EVT_COMMAND_SCROLL_CHANGED on
    release, and supports GetValue/SetValue/SetRange matching wx.Slider.
    """

    _PAD = 12
    _TRACK_H = 4
    _THUMB_R = 8

    _BLUE       = wx.Colour(37, 59, 146)
    _BLUE_DARK  = wx.Colour(20, 40, 110)
    _TRACK_BG   = wx.Colour(200, 200, 200)
    _TRACK_FILL = _BLUE
    _THUMB_FILL = _BLUE
    _THUMB_EDGE = _BLUE_DARK

    def __init__(self, parent, id=wx.ID_ANY, value=0, minValue=0, maxValue=10,
                 pos=wx.DefaultPosition, size=wx.DefaultSize,
                 style=wx.SL_HORIZONTAL, name='TransparentSlider'):
        wx.Control.__init__(self, parent, id, pos, size, style=wx.BORDER_NONE)
        self._min = float(minValue)
        self._max = float(maxValue)
        self._value = float(value)
        self._dragging = False
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_ERASE_BACKGROUND, lambda e: None)
        self.Bind(wx.EVT_LEFT_DOWN, self._on_left_down)
        self.Bind(wx.EVT_LEFT_UP, self._on_left_up)
        self.Bind(wx.EVT_MOTION, self._on_motion)
        self.Bind(wx.EVT_MOUSE_CAPTURE_LOST,
                  lambda e: setattr(self, '_dragging', False))

    def DoGetBestSize(self):
        return wx.Size(300, 90)

    def SetMinSize(self, size):
        w = size[0] if hasattr(size, '__getitem__') else size.GetWidth()
        wx.Control.SetMinSize(self, wx.Size(w, self.DoGetBestSize().GetHeight()))

    def GetValue(self):
        return int(round(self._value))

    def SetValue(self, value):
        self._value = max(self._min, min(self._max, float(value)))
        self.Refresh()

    def GetMin(self):
        return int(self._min)

    def GetMax(self):
        return int(self._max)

    def SetRange(self, minVal, maxVal):
        self._min = float(minVal)
        self._max = float(maxVal)
        self._value = max(self._min, min(self._max, self._value))
        self.Refresh()

    def _thumb_x(self):
        w, _ = self.GetClientSize()
        track_w = w - 2 * self._PAD
        if self._max == self._min:
            return self._PAD + track_w // 2
        ratio = (self._value - self._min) / (self._max - self._min)
        return self._PAD + ratio * track_w

    def _value_from_x(self, x):
        w, _ = self.GetClientSize()
        track_w = max(1, w - 2 * self._PAD)
        ratio = max(0.0, min(1.0, (x - self._PAD) / track_w))
        return self._min + ratio * (self._max - self._min)

    def _on_paint(self, event):
        dc = wx.BufferedPaintDC(self)
        parent = self.GetParent()
        bmp = getattr(parent, 'backgroundBMP', None)
        if bmp:
            pos = parent.ScreenToClient(self.GetScreenPosition())
            dc.DrawBitmap(bmp, -pos.x, -pos.y)
        else:
            dc.SetBackground(wx.Brush(self.GetBackgroundColour()))
            dc.Clear()

        w, h = self.GetClientSize()
        cy = h // 2
        track_y = cy - self._TRACK_H // 2
        tx = int(self._thumb_x())

        dc.SetPen(wx.Pen(self._TRACK_BG))
        dc.SetBrush(wx.Brush(self._TRACK_BG))
        dc.DrawRoundedRectangle(self._PAD, track_y,
                                w - 2 * self._PAD, self._TRACK_H, 2)

        fill_w = tx - self._PAD
        if fill_w > 0:
            dc.SetPen(wx.Pen(self._TRACK_FILL))
            dc.SetBrush(wx.Brush(self._TRACK_FILL))
            dc.DrawRoundedRectangle(self._PAD, track_y, fill_w, self._TRACK_H, 2)

        dc.SetBrush(wx.Brush(self._THUMB_FILL))
        dc.SetPen(wx.Pen(self._THUMB_EDGE, 1))
        dc.DrawCircle(tx, cy, self._THUMB_R)

    def _on_left_down(self, event):
        self.CaptureMouse()
        self._dragging = True
        self._value = self._value_from_x(event.GetX())
        self.Refresh()

    def _on_left_up(self, event):
        if self._dragging:
            self._dragging = False
            if self.HasCapture():
                self.ReleaseMouse()
            self._value = self._value_from_x(event.GetX())
            self.Refresh()
            self._fire_changed()

    def _on_motion(self, event):
        if self._dragging and event.LeftIsDown():
            self._value = self._value_from_x(event.GetX())
            self.Refresh()

    def _fire_changed(self):
        evt = wx.CommandEvent(wx.EVT_SLIDER.typeId, self.GetId())
        evt.SetEventObject(self)
        self.GetEventHandler().ProcessEvent(evt)
