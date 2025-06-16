import wx

class Odometer(wx.Control):
    def __init__(self, parent, id=wx.ID_ANY, pos=wx.DefaultPosition, size=wx.DefaultSize, format="###,###.###", initial=0.0, name="Odometer"):
        """
        Custom widget resembling a car's odometer.

        :param parent: Parent window. Must not be None.
        :param id: Identifier. Default is wx.ID_ANY.
        :param pos: Position of the widget. Default is wx.DefaultPosition.
        :param size: Size of the widget. Default is wx.DefaultSize.
        :param format: Format of the odometer, e.g., "###,###.###".
        :param initial: Initial value of the odometer. Default is 0.0.
        :param name: Name of the widget. Default is "Odometer".
        """
        wx.Control.__init__(self, parent, id, pos=pos, size=size, name=name)

        self._format = format
        self._value = initial
        self._font = wx.SystemSettings.GetFont(wx.SYS_SYSTEM_FONT)
        self._font.SetPointSize(14)  # Set font size for better visibility

        self._positions = self._parse_format(format)
        self._cylinders = [int(d) for d in str(int(initial))] + [0] * (len(self._positions) - len(str(int(initial))))

        self.SetBackgroundColour("white")
        self.SetForegroundColour("black")

        self.Bind(wx.EVT_PAINT, self.OnPaint)
        self.Bind(wx.EVT_MOUSEWHEEL, self.OnMouseWheel)

    def _parse_format(self, format):
        """
        Parses the format string and returns a list of positions (digits, commas, or decimal points).
        """
        positions = []
        for char in format:
            if char == "#":
                positions.append("digit")
            elif char == ",":
                positions.append("comma")
            elif char == ".":
                positions.append("decimal")
        return positions

    def OnPaint(self, event):
        """
        Handles the paint event to draw the odometer.
        """
        dc = wx.PaintDC(self)
        dc.SetFont(self._font)
        dc.SetBackground(wx.Brush(self.GetBackgroundColour()))
        dc.Clear()

        w, h = self.GetSize()
        cylinder_width = w // len(self._positions)
        cylinder_height = h

        for i, position in enumerate(self._positions):
            x = i * cylinder_width

            # Draw vertical separator bars
            if i > 0:
                dc.SetPen(wx.Pen("gray", 1))
                dc.DrawLine(x, 0, x, h)

            if position == "digit":
                # Draw the current digit
                text = str(self._cylinders[i])
                tw, th = dc.GetTextExtent(text)
                dc.DrawText(text, x + (cylinder_width - tw) // 2, (cylinder_height - th) // 2)
            elif position == "comma":
                # Draw a comma
                text = ","
                tw, th = dc.GetTextExtent(text)
                dc.DrawText(text, x + (cylinder_width - tw) // 2, (cylinder_height - th) // 2)
            elif position == "decimal":
                # Draw a decimal point
                text = "."
                tw, th = dc.GetTextExtent(text)
                dc.DrawText(text, x + (cylinder_width - tw) // 2, (cylinder_height - th) // 2)

    def OnMouseWheel(self, event):
        """
        Handles the mouse wheel event to update the odometer value.
        """
        rotation = event.GetWheelRotation()
        x = event.GetX()

        # Determine which cylinder to scroll based on the x-axis position
        w, h = self.GetSize()
        cylinder_width = w // len(self._positions)
        cylinder_index = x // cylinder_width

        if self._positions[cylinder_index] == "digit":
            if rotation > 0:  # Scroll up
                self._increment_cylinder(cylinder_index)
            elif rotation < 0:  # Scroll down
                self._decrement_cylinder(cylinder_index)

        self.Refresh()  # Redraw the widget to reflect the updated value

    def _increment_cylinder(self, index):
        """
        Increments the value of the specified cylinder and handles carry-over.
        Prevents incrementing beyond 9 if all cylinders to the left are 9.
        """
        # Check if all cylinders to the left are 9
        if all(self._cylinders[i] == 9 for i in range(index)):
            if self._cylinders[index] < 9:
                self._cylinders[index] += 1
            return  # Prevent incrementing beyond 9

        if self._cylinders[index] == 9:
            if index > 0:
                self._cylinders[index] = 0
                # If the next cylinder is a digit, increment it
                if self._positions[index - 1] == "digit":
                    self._increment_cylinder(index - 1)
                else:
                    self._increment_cylinder(index - 2)
        else:
            self._cylinders[index] += 1

    def _decrement_cylinder(self, index):
        """
        Decrements the value of the specified cylinder and handles borrow.
        Prevents decrementing below 0 if all cylinders to the left are 0.
        """
        # Check if all cylinders to the left are 0
        if all(self._cylinders[i] == 0 for i in range(index)):
            if self._cylinders[index] > 0:
                self._cylinders[index] -= 1
            return  # Prevent decrementing below 0

        if self._cylinders[index] == 0:
            if index > 0:
                self._cylinders[index] = 9
                if self._positions[index - 1] == "digit":
                    self._decrement_cylinder(index - 1)
                else:
                    self._decrement_cylinder(index - 2)
        else:
            self._cylinders[index] -= 1

    def GetValue(self):
        """
        Returns the current value of the odometer as a floating-point number.
        """
        integer_part = "".join(str(self._cylinders[i]) for i, p in enumerate(self._positions) if p == "digit" and i < self._positions.index("decimal"))
        fractional_part = "".join(str(self._cylinders[i]) for i, p in enumerate(self._positions) if p == "digit" and i > self._positions.index("decimal"))
        return float(f"{integer_part}.{fractional_part}")

    def SetValue(self, value):
        """
        Sets the current value of the odometer.

        :param value: Floating-point value to set.
        """
        self._value = value
        integer_part, fractional_part = str(value).split(".")
        self._cylinders = [int(d) for d in integer_part] + [int(d) for d in fractional_part]
        self.Refresh()