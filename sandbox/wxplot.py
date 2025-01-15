# -*- coding: utf-8 -*-
import matplotlib
matplotlib.use('wxAgg')
from matplotlib.figure import Figure
import matplotlib.animation as animation
from matplotlib.backends.backend_wxagg import FigureCanvasWxAgg as FigureCanvas

import wx
import numpy as np

class Frame(wx.Frame):
    def __init__(self,*args,**kwargs):
        wx.Frame.__init__(self,*args,**kwargs)
        self.initCtrls()
        self.Centre(True)
        self.Show()
        
    def initCtrls(self):
        self.mainsz = wx.BoxSizer(wx.HORIZONTAL)
        self.figure = Figure()
        self.axes = self.figure.add_subplot(111)
        self.canvas = FigureCanvas(self, wx.ID_ANY, self.figure)
        
        # Combo-Box
        self.options = wx.ComboBox(self, wx.ID_ANY, choices=["Anim 01","Anim 02"]) 
        
        self.mainsz.Add(self.options, 1, wx.ALIGN_LEFT)
        self.mainsz.Add(self.canvas, 4, wx.EXPAND)
        self.SetSizer(self.mainsz)
        
        # Bind
        self.Bind(wx.EVT_COMBOBOX, self.OnSelect, self.options)
        
        # Plot line
        self.line, = self.axes.plot([],[], '-')
        self.anim = None 
        
    def OnSelect(self,event):
        if event.GetSelection()==0:
            self.initAnim_01()
        elif event.GetSelection()==1:
            self.initAnim_02()
        
    def initAnim_01(self):
        #data = np.loadtxt("example1.txt", delimiter=",")
        #self.x = data[:,0]
        #self.y = data[:,1]
        self.x = np.arange(-5,0,0.01)
        self.y = np.sin(self.x)
        self.line.set_xdata(self.x)
        self.line.set_ydata(self.y)
        self._initAnim()
        
    def initAnim_02(self):
        data = np.loadtxt("example2.txt", delimiter=",")
        self.x = data[:,0]
        self.y = data[:,1]
        self._initAnim()
    
    def _initAnim(self):
        if not self.anim is None:
            # Stop previous animation
            self.anim._stop()
        self.anim = animation.FuncAnimation(self.figure, self.animate, 
                                      interval=50, blit=True, repeat=False)
        self.canvas.draw()
        self.Layout() # Update
        
    def animate(self,i):
        self.y = np.roll(self.y,-1)
        self.axes.relim()
        self.axes.autoscale_view()
        return self.line,


if __name__ == "__main__":
    app = wx.App()
    fr = Frame(None, wx.ID_ANY, "Example", size=(600,400))
    app.MainLoop()